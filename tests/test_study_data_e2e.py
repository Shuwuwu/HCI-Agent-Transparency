"""
Purpose:
    End-to-end tests of participant data collection over real HTTP, the way the browser drives the server: two
    simulated participants walk the whole experiment at the same time (consent, practice, eight trials with
    interaction events, the questionnaire), with page reloads, repeated submissions and a server restart in the
    middle. Afterwards the database must hold every record exactly once, for the right participant, consistent
    with itself (participant_data.find_problems) and with its CSV export; and no participant-facing route may
    reach the data.

Inputs:
    A temporary development database (tests/study_data_fixtures.py) served by participant_server.Handler.

Outputs:
    unittest results.

Dependencies:
    src/scripts/participant_server.py, participant_store.py, participant_data.py, experiment_plan.py,
    participant_flow.py (which questionnaire items each condition is asked);
    tests/study_data_fixtures.py.

Usage:
    python3 -B -m unittest tests.test_study_data_e2e
"""

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import experiment_plan as ep  # noqa: E402
import participant_data as pdata  # noqa: E402
import participant_flow as flow  # noqa: E402
import participant_server as ps  # noqa: E402
import participant_store as store  # noqa: E402
from study_data_fixtures import CONFIG, PAYLOAD, PARTICIPANTS, new_database, submission  # noqa: E402

FIRST, SECOND = PARTICIPANTS[0], PARTICIPANTS[1]
BROWSER_EVENTS = ({"type": "workspace_panel_open"}, {"type": "workspace_tab_open", "tab": "mail"},
                  {"type": "workspace_tab_view", "tab": "mail", "duration_ms": 1200},
                  {"type": "workspace_panel_close", "duration_ms": 2000},
                  {"type": "scenario_visible", "duration_ms": 15000})


class Server:
    """participant_server on a free port over `db_path`, as main() would configure it for development."""

    def __init__(self, db_path: Path):
        class Handler(ps.Handler):
            def log_message(self, fmt, *args):
                pass

        Handler.db_path, Handler.environment = db_path, "development"
        Handler.config, Handler.payload = CONFIG, PAYLOAD
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class Participant:
    """One simulated participant's browser: holds only the link token, as flow.js does."""

    def __init__(self, server: Server, token: str):
        self.server, self.token = server, token

    def stage(self):
        status, body = self.server.request(f"/api/stage?t={self.token}")
        assert status == 200, (status, body)
        return json.loads(body)["stage"]

    def log(self, trial_id, event):
        return self.server.request("/api/log", {"token": self.token, "trial_id": trial_id, "client_ms": 1, **event})[0]

    def complete(self, stage, **override):
        body = {"token": self.token, "stage_key": stage["key"], **submission(stage), **override}
        return self.server.request("/api/stage/complete", body)

    def walk(self, until=None, stage=None):
        """Continue from the current stage (as the page does: one GET, then each answer carries the next stage)
        until `until` is current or the sequence ends. Scenario stages get BROWSER_EVENTS; the last two (panel
        close and visible time) are sent after Continue, as the page sends them while the next stage loads."""
        stage = stage or self.stage()
        while stage is not None and stage["key"] != until:
            if stage["kind"] == "scenario":
                for event in BROWSER_EVENTS[:3]:
                    assert self.log(stage["trial_id"], event) == 204
            status, body = self.complete(stage)
            assert status == 200, (stage["key"], status, body)
            if stage["kind"] == "scenario":
                for event in BROWSER_EVENTS[3:]:
                    assert self.log(stage["trial_id"], event) == 204
            stage = json.loads(body)["stage"]
        return stage


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        conn, cls.path = new_database(cls.folder)
        cls.tokens = {r["participant_id"]: r["token"] for r in conn.execute("SELECT participant_id, token "
                                                                              "FROM participants")}
        conn.close()
        cls.server = Server(cls.path)
        first, second = (Participant(cls.server, cls.tokens[p]) for p in (FIRST, SECOND))
        cls.plan = {p: ep.plan_for(CONFIG, p, PAYLOAD) for p in (FIRST, SECOND)}

        # 1. Both start at the same time and get partway (concurrent requests on separate threads).
        errors = []

        def run(participant, until):
            try:
                participant.walk(until=until)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(first, "trial_03_scenario")),
                   threading.Thread(target=run, args=(second, "trial_05_assessment"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors

        # 2. FIRST reloads the page three times in the middle of trial 3, and an answer is submitted twice.
        cls.entered_before = cls.entered(FIRST, "trial_03_scenario")
        cls.reloads = [first.stage()["key"] for _ in range(3)]
        assessment = second.stage()
        cls.first_submit = second.complete(assessment)
        cls.second_submit = second.complete(assessment, answers={**submission(assessment)["answers"], "confidence": 5})

        # 3. The server is stopped and a new one started on the same database file.
        cls.server.stop()
        cls.server = Server(cls.path)
        first.server = second.server = cls.server
        cls.resumed = {p.token: p.stage()["key"] for p in (first, second)}

        # 4. Both finish.
        first.walk()
        second.walk()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.folder, ignore_errors=True)

    @classmethod
    def entered(cls, participant, stage_key):
        conn = store.connect(cls.path, "development")
        try:
            return conn.execute("SELECT entered_at FROM progress WHERE participant_id = ? AND stage_key = ?",
                                (participant, stage_key)).fetchone()[0]
        finally:
            conn.close()

    def setUp(self):
        self.conn = store.connect(self.path, "development")

    def tearDown(self):
        self.conn.close()

    def rows(self, sql, *params):
        return [dict(r) for r in self.conn.execute(sql, params)]

    # -- completeness ------------------------------------------------------------------------------------------
    def test_every_record_is_there_exactly_once_for_each_participant(self):
        for participant in (FIRST, SECOND):
            stages = self.plan[participant]["stages"]
            assessments = [s for s in stages if s["kind"] == "assessment"]
            scenarios = [s for s in stages if s["kind"] == "scenario"]
            with self.subTest(participant=participant):
                self.assertIsNotNone(store.record(self.conn, participant)["completed_at"])
                self.assertIsNotNone(store.consent_of(self.conn, participant))
                self.assertEqual(store.completed_stages(self.conn, participant), [s["key"] for s in stages])
                responses = self.rows("SELECT * FROM responses WHERE participant_id = ?", participant)
                self.assertEqual(sorted(r["stage_key"] for r in responses), sorted(s["key"] for s in assessments))
                self.assertEqual(sum(r["practice"] for r in responses), 1)
                condition = self.plan[participant]["condition"]
                for key, spec in flow.load_questionnaires().items():
                    self.assertEqual(list(store.questionnaire_of(self.conn, participant, key)),
                                     [i["id"] for i in flow.offered_items(spec, condition)], key)
                for scenario in scenarios:
                    events = store.trial_events(self.conn, participant, scenario["trial_id"])
                    types = [e["type"] for e in events]
                    self.assertEqual(types.count("session_start"), 1, scenario["trial_id"])
                    for event in BROWSER_EVENTS:
                        self.assertEqual(types.count(event["type"]), 1, (scenario["trial_id"], event["type"]))
                    self.assertTrue(all(e["practice"] == int(scenario["practice"]) for e in events))
                    self.assertEqual([e["seq"] for e in events], list(range(len(events))))
        self.assertEqual(pdata.find_problems(self.conn), [])

    def test_late_events_join_their_own_trial_at_its_assessment(self):
        late = self.rows("SELECT stage_key FROM events WHERE participant_id = ? AND type = 'scenario_visible'", FIRST)
        self.assertTrue(late and all(r["stage_key"].endswith("_assessment") for r in late))

    def test_the_export_matches_the_database(self):
        out = self.folder / "export"
        manifest = pdata.write_export(self.conn, out, self.path)
        for name, sql in (("participants.csv", "participants"), ("progress.csv", "progress"),
                          ("events.csv", "events"), ("questionnaire.csv", "questionnaire_responses")):
            self.assertEqual(manifest["files"][name]["rows"], self.rows(f"SELECT COUNT(*) n FROM {sql}")[0]["n"])
        self.assertEqual(manifest["files"]["trials.csv"]["rows"],
                         self.rows("SELECT COUNT(*) n FROM plan_stages WHERE kind = 'scenario'")[0]["n"])

    # -- persistence -------------------------------------------------------------------------------------------
    def test_reloading_keeps_the_stage_and_its_first_arrival_and_is_recorded(self):
        self.assertEqual(self.reloads, ["trial_03_scenario"] * 3)
        self.assertEqual(self.entered(FIRST, "trial_03_scenario"), self.entered_before)
        loads = self.rows("SELECT * FROM events WHERE participant_id = ? AND type = 'stage_load' "
                          "AND trial_id = ?", FIRST, f"{FIRST}-t03")
        self.assertGreaterEqual(len(loads), 3)

    def test_a_repeated_submission_changes_nothing(self):
        self.assertEqual(self.first_submit[0], 200)
        self.assertEqual(self.second_submit[0], 200)
        kept = store.response_of(self.conn, SECOND, "trial_05_assessment")
        self.assertEqual(kept["confidence"], 2)  # the first answer, not the resubmitted 5
        self.assertEqual(len(self.rows("SELECT * FROM progress WHERE participant_id = ? AND stage_key = ?",
                                       SECOND, "trial_05_assessment")), 1)

    def test_a_restarted_server_resumes_both_participants_where_they_were(self):
        self.assertEqual(self.resumed, {self.tokens[FIRST]: "trial_03_scenario",
                                        self.tokens[SECOND]: "trial_06_scenario"})

    # -- isolation and protection ------------------------------------------------------------------------------
    def test_one_participants_data_never_names_the_other(self):
        for me, other in ((FIRST, SECOND), (SECOND, FIRST)):
            blob = json.dumps(pdata.participant_data(self.conn, me))
            self.assertNotIn(other, blob)
            self.assertNotIn(self.tokens[other], blob)
            self.assertNotIn(self.tokens[me], blob)

    def test_events_for_the_other_participant_are_refused(self):
        other_trial = f"{SECOND}-t08"
        before = self.rows("SELECT COUNT(*) n FROM events")[0]["n"]
        body = {"token": self.tokens[FIRST], "trial_id": other_trial, "type": "workspace_panel_open"}
        self.assertEqual(self.server.request("/api/log", body)[0], 409)  # FIRST has finished: no current trial
        self.assertEqual(self.rows("SELECT COUNT(*) n FROM events")[0]["n"], before)

    def test_no_participant_route_reaches_the_data(self):
        name = self.path.name
        for path in (f"/data/participants/{name}", f"/static/../../data/participants/{name}",
                     f"/static/%2e%2e/%2e%2e/data/participants/{name}", "/data/participants/exports/",
                     "/data/participants/backups/", "/api/log", f"/p/{self.tokens[FIRST]}/../../data/participants/{name}",
                     "/src/data/participants/participants.db", "/src/data/interaction_logs/"):
            status, body = self.server.request(path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn("SQLite format", body, path)

    def test_git_ignores_every_kind_of_participant_data_file(self):
        paths = ["data/participants/study.sqlite3", "data/participants/development.sqlite3-wal",
                 "data/participants/study.sqlite3-shm", "data/participants/exports/study/trials.csv",
                 "data/participants/exports/study/export_manifest.json",
                 "data/participants/backups/study/study-20260101T000000000000Z-manual.sqlite3",
                 "data/participants/backups/study/study-20260101T000000000000Z-manual.sqlite3.sha256",
                 "data/participants/backups/legacy/legacy-storage-x.tar.gz", "elsewhere/scratch.sqlite3"]
        result = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(sorted(result.stdout.split()), sorted(paths))
        tracked = subprocess.run(["git", "check-ignore", "--no-index", "data/participants/README.md"], cwd=ROOT,
                                 capture_output=True, text=True)
        self.assertEqual(tracked.stdout, "")  # the folder's README is the only file that may be committed


if __name__ == "__main__":
    unittest.main()
