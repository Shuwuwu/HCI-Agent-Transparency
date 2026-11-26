"""Tests of participant_flow.py and the participant routes of participant_server.py, over real HTTP.

Two things are checked, and nothing else: that no participant-facing response reveals which stimulus or which
transparency condition a trial holds (the folder name says whether a trial is injected, so leaking it would tell
a participant what to expect), and that the flow cannot lose, duplicate or reorder a trial.

Each test server is its own Handler subclass rather than a mutation of participant_server.Handler, whose class
attributes are global: the tests can then run in any order alongside tests/test_participant_server.py.
"""

import json
import shutil
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

import experiment_plan as ep  # noqa: E402
import interaction_log as ilog  # noqa: E402
import participant_flow as flow  # noqa: E402
import participant_server as ps  # noqa: E402
import participant_store as store  # noqa: E402

CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
PAYLOAD = ep.load_assignments(CONFIG)
CONTENT_ROUTES = ("/api/stimulus", "/api/workspace", "/api/skills", "/api/skill")


def participant_with(condition):
    """The first participant of the development assignments in `condition` ("low" or "high")."""
    return next(row["participant"] for row in PAYLOAD["assignments"] if row["condition"] == condition)


class FlowServer:
    """A server on a throwaway database with every development participant enrolled.

    `tokens` maps participant id to link token. `handler` is the Handler subclass actually serving, so a test can
    build a variant of it (preview on, a different configuration) without touching the shared class.
    """

    def __init__(self, config=CONFIG, researcher_preview=False):
        self.folder = Path(tempfile.mkdtemp())
        self.logs = self.folder / "logs"
        conn = store.connect(self.folder / "participants.db")
        self.tokens = {row["participant_id"]: row["token"] for row in store.enrol_all(conn, CONFIG, PAYLOAD)}
        conn.close()

        class Handler(ps.Handler):
            db_path = self.folder / "participants.db"
            log_dir = self.logs

            def log_message(self, fmt, *args):
                pass  # the access log and the flow reasons would bury the test output

        Handler.config = config
        Handler.payload = PAYLOAD
        Handler.researcher_preview = researcher_preview
        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    # -- convenience on top of the routes --
    def stage(self, participant):
        status, body = self.get(f"/api/stage?t={self.tokens[participant]}")
        assert status == 200, (status, body)
        return json.loads(body)

    def complete(self, participant, stage_key):
        return self.post("/api/stage/complete", {"token": self.tokens[participant], "stage_key": stage_key})

    def walk(self, participant, until=None):
        """Advance the way the browser does, stopping when `until` is the current stage (or the sequence ends).

        One GET to find where the participant is, then POST after POST: each answer carries the next stage, and
        the page draws it without asking for it again. Walking by re-fetching /api/stage between steps would
        exercise a path a participant never takes.
        """
        walked = []
        stage = self.stage(participant)["stage"]
        while stage is not None and stage["key"] != until:
            answer = self.complete(participant, stage["key"])
            assert answer[0] == 200, answer
            walked.append(stage["key"])
            stage = json.loads(answer[1])["stage"]
        return walked


class FlowTestCase(unittest.TestCase):
    """Base case holding one participant-facing server for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.srv = FlowServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()


class ExposureTests(FlowTestCase):
    """What a participant can see or ask for."""

    def test_no_response_of_the_whole_flow_names_a_stimulus_or_a_condition(self):
        """The one test that protects the manipulation: walking every stage must never disclose which scenario a
        trial is, whether it is the injected version, or which transparency condition the participant is in."""
        participant = participant_with("high")  # the condition whose responses do carry a trace
        plan = ep.plan_for(CONFIG, participant, PAYLOAD)
        secrets = {s["stimulus"] for s in plan["stages"] if "stimulus" in s}
        secrets |= {s["scenario"] for s in plan["stages"] if "scenario" in s}
        # The safe/unsafe distinction is the secret; "practice", the practice trial's version, is not —
        # participants are told which trial is the practice one, and the descriptor says so.
        secrets |= {s["version"] for s in plan["stages"] if s.get("version") in ("safe", "unsafe")}
        self.assertTrue(secrets)

        page = self.srv.get(f"/p/{self.srv.tokens[participant]}")[1]
        seen = [page]
        while True:
            payload = self.srv.stage(participant)
            seen.append(json.dumps(payload))
            stage = payload["stage"]
            if stage is None:
                break
            self.assertNotIn("condition", stage)
            self.assertNotIn("stimulus", stage)
            self.assertNotIn("scenario", stage)
            self.assertEqual(self.srv.complete(participant, stage["key"])[0], 200)
        for text in seen:
            for secret in secrets:
                self.assertNotIn(secret, text)

    def test_the_condition_cannot_be_changed_by_asking_for_the_other_one(self):
        """A low participant must not be able to obtain the high view, which is the whole manipulation. The
        transparency condition comes from their record, so a c= parameter on the request is ignored."""
        low, high = participant_with("low"), participant_with("high")
        srv = FlowServer()
        try:
            for participant in (low, high):
                srv.walk(participant, until="practice_scenario")
            low_view = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[low]}")[1])
            high_view = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[high]}")[1])
            self.assertNotIn("trace", low_view)
            self.assertIn("trace", high_view)
            for suffix in ("&c=high", "&c=low&s=user_task_1__no_injection"):
                forced = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[low]}{suffix}")[1])
                self.assertEqual(forced, low_view, suffix)
        finally:
            srv.close()

    def test_scenario_content_is_refused_at_every_stage_that_shows_no_scenario(self):
        """Consent, the instructions, an assessment and the questionnaire must not be usable to fetch a trial's
        content: that is how a participant could otherwise read the next trial early or the last one again."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            token = srv.tokens[participant]
            for stage_key in ("consent", "instructions", "practice_assessment", "questionnaire"):
                srv.walk(participant, until=stage_key)
                self.assertEqual(srv.stage(participant)["stage"]["key"], stage_key)
                for route in CONTENT_ROUTES:
                    self.assertEqual(srv.get(f"{route}?t={token}")[0], 404, (stage_key, route))
        finally:
            srv.close()

    def test_an_unknown_or_malformed_token_reaches_nothing(self):
        for token in ("", "short", "../../etc/passwd", "a" * 200, "notarealtoken000000000000000000"):
            self.assertIn(self.srv.get(f"/p/{token}")[0], (400, 404), token)
            self.assertIn(self.srv.get(f"/api/stage?t={token}")[0], (400, 404), token)
            for route in CONTENT_ROUTES:
                self.assertIn(self.srv.get(f"{route}?t={token}")[0], (400, 404), (token, route))
            self.assertIn(self.srv.post("/api/stage/complete", {"token": token, "stage_key": "consent"})[0],
                          (400, 404), token)

    def test_the_researcher_routes_are_absent_unless_the_server_is_started_for_a_researcher(self):
        """A participant-facing server must not serve the stimulus index or the s= form of the content routes;
        with --researcher-preview the same server does."""
        paths = ("/", "/chat", "/api/stimulus?s=user_task_1__no_injection&c=high",
                 "/api/workspace?s=user_task_1__no_injection", "/api/skills?s=user_task_1__no_injection")
        for path in paths:
            self.assertEqual(self.srv.get(path)[0], 404, path)
        preview = FlowServer(researcher_preview=True)
        try:
            for path in paths:
                self.assertEqual(preview.get(path)[0], 200, path)
        finally:
            preview.close()


class IntegrityTests(FlowTestCase):
    """What the flow records, and what it refuses to record."""

    def test_a_participant_walks_the_whole_sequence_once_and_is_marked_complete(self):
        srv = FlowServer()
        try:
            participant = participant_with("low")
            expected = [s["key"] for s in ep.plan_for(CONFIG, participant, PAYLOAD)["stages"]]
            walked = srv.walk(participant)
            self.assertEqual(walked, expected)
            self.assertIsNone(srv.stage(participant)["stage"])
            conn = store.connect(srv.handler.db_path)
            try:
                self.assertIsNotNone(store.record(conn, participant)["completed_at"])
                repeated = conn.execute("SELECT stage_key FROM progress WHERE participant_id = ? "
                                        "GROUP BY stage_key HAVING COUNT(*) > 1", (participant,)).fetchall()
                self.assertEqual(repeated, [])
                self.assertEqual(len(store.completed_stages(conn, participant)), len(expected))
            finally:
                conn.close()
        finally:
            srv.close()

    def test_reopening_the_link_returns_the_same_stage_and_records_one_arrival(self):
        """The resume path. Reloading mid-trial must not move the participant on or start the trial again."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            srv.walk(participant, until="trial_01_scenario")
            first = srv.stage(participant)
            for _ in range(4):
                self.assertEqual(srv.get(f"/p/{srv.tokens[participant]}")[0], 200)
                self.assertEqual(srv.stage(participant), first)
            conn = store.connect(srv.handler.db_path)
            try:
                rows = conn.execute("SELECT entered_at FROM progress WHERE participant_id = ? AND stage_key = ?",
                                    (participant, "trial_01_scenario")).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()
        finally:
            srv.close()

    def test_a_later_stage_cannot_be_completed_and_progress_is_untouched(self):
        srv = FlowServer()
        try:
            participant = participant_with("low")
            before = srv.stage(participant)
            for stage_key in ("questionnaire", "trial_04_scenario", "completion"):
                self.assertEqual(srv.complete(participant, stage_key)[0], 409, stage_key)
            for stage_key in ("debriefing", "trial_99_scenario"):
                self.assertEqual(srv.complete(participant, stage_key)[0], 409, stage_key)
            self.assertEqual(srv.stage(participant), before)
        finally:
            srv.close()

    def test_submitting_the_same_stage_twice_advances_only_once(self):
        """A double click or a resubmitted form must not consume the following stage."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            self.assertEqual(srv.stage(participant)["stage"]["key"], "consent")
            first = srv.complete(participant, "consent")
            second = srv.complete(participant, "consent")
            self.assertEqual((first[0], second[0]), (200, 200))
            self.assertEqual(json.loads(first[1])["stage"]["key"], "instructions")
            self.assertEqual(json.loads(second[1])["stage"]["key"], "instructions")
            self.assertEqual(srv.stage(participant)["stage"]["key"], "instructions")
        finally:
            srv.close()

    def test_two_participants_keep_separate_progress(self):
        srv = FlowServer()
        try:
            first, second = PAYLOAD["assignments"][0]["participant"], PAYLOAD["assignments"][1]["participant"]
            srv.walk(first, until="trial_02_scenario")
            self.assertEqual(srv.stage(second)["stage"]["key"], "consent")
            self.assertEqual(srv.stage(second)["progress"]["completed"], 0)
            self.assertEqual(srv.stage(first)["stage"]["key"], "trial_02_scenario")
        finally:
            srv.close()

    def test_each_trial_is_logged_under_its_own_trial_id_with_the_true_stimulus_and_condition(self):
        """The browser is never told the stimulus or the condition, so the server stamps them: without this the
        interaction events of a trial could not be tied to what was actually shown."""
        srv = FlowServer()
        try:
            participant = participant_with("high")
            plan = ep.plan_for(CONFIG, participant, PAYLOAD)
            scenarios = ep.scenario_stages(plan, practice=True)
            srv.walk(participant)
            for stage in scenarios:
                path = srv.logs / f"{stage['trial_id']}.jsonl"
                self.assertTrue(path.is_file(), stage["trial_id"])
                events = ilog.read_session(path)
                self.assertEqual([e["type"] for e in events], ["session_start"])
                self.assertEqual(events[0]["stimulus"], stage["stimulus"])
                self.assertEqual(events[0]["condition"], stage["condition"])
            self.assertEqual(len({s["trial_id"] for s in scenarios}), len(scenarios))

            # Every stage must also carry an arrival time: the trial's reading and response times are measured
            # from it, and a stage reached by continuing is the one the participant actually arrived at.
            conn = store.connect(srv.handler.db_path)
            try:
                missing = conn.execute("SELECT stage_key FROM progress WHERE participant_id = ? "
                                       "AND entered_at IS NULL", (participant,)).fetchall()
                self.assertEqual(missing, [])
                entered = {row["stage_key"] for row in
                           conn.execute("SELECT stage_key FROM progress WHERE participant_id = ?", (participant,))}
                self.assertEqual(entered, {s["key"] for s in plan["stages"]})
            finally:
                conn.close()
        finally:
            srv.close()

    def test_a_changed_stage_sequence_stops_the_participant_instead_of_resuming_them(self):
        """Enrolled under eight trials, served a three-trial configuration: the flow must refuse rather than mix
        two different experiments in one participant's record."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            srv.walk(participant, until="trial_01_scenario")
            raw = json.loads(ep.DEVELOPMENT_CONFIG.read_text(encoding="utf-8"))
            raw["trials_per_participant"] = 3
            path = Path(tempfile.mkdtemp()) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            srv.handler.config = ep.load_config(path)
            token = srv.tokens[participant]
            self.assertEqual(srv.get(f"/api/stage?t={token}")[0], 409)
            self.assertEqual(srv.get(f"/api/stimulus?t={token}")[0], 409)
            self.assertEqual(srv.complete(participant, "trial_01_scenario")[0], 409)
        finally:
            srv.close()


class ResolutionTests(unittest.TestCase):
    """participant_flow.py without a server: the token resolution and the stage descriptor."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.conn = store.connect(self.folder / "participants.db")
        self.participant = participant_with("low")
        self.row = store.enrol(self.conn, CONFIG, self.participant, PAYLOAD)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_token_resolves_to_its_own_participant_only(self):
        self.assertEqual(flow.participant_for(self.conn, self.row["token"])["participant_id"], self.participant)
        with self.assertRaises(FileNotFoundError):
            flow.participant_for(self.conn, "A" * 32)  # right shape, nobody's token
        for bad in ("", "short", None, "has/slash/and spaces"):
            with self.subTest(token=bad), self.assertRaises(ValueError):
                flow.participant_for(self.conn, bad)

    def test_the_descriptor_keeps_the_trial_id_and_drops_the_stimulus_identity(self):
        plan = ep.plan_for(CONFIG, self.participant, PAYLOAD)
        for stage in plan["stages"]:
            out = flow.descriptor(stage, plan)
            self.assertEqual(set(out) - {"key", "kind", "index", "trial_id", "practice", "trial", "trials"}, set())
            if stage["kind"] in ("scenario", "assessment"):
                self.assertEqual(out["trial_id"], stage["trial_id"])
            if out.get("trials"):
                self.assertEqual(out["trials"], CONFIG["trials_per_participant"])


if __name__ == "__main__":
    unittest.main()
