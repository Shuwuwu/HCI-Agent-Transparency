"""
Purpose:
    The complete study procedure, run the way a researcher runs it, with two simulated participants (one per
    transparency condition) on the rehearsal configuration: participants are enrolled and their links printed
    with participant_store.py, the participant server is started as its own process, each participant's browser
    is imitated request by request (stage, conversation, workspace, skills, a skill document, interaction events,
    answers), the server is stopped with Ctrl+C midway and started again, and afterwards the data is checked and
    exported with participant_data.py. This is the Block 5 validation before pilot testing.

    Checked: the high condition receives the activity log in every conversation and the low condition never does,
    with everything else in the conversation identical to the stimulus files; the workspace and the skills are
    the same in both conditions; no participant-facing response names a stimulus, scenario, version or condition;
    researcher routes and data files are unreachable; every participant's order and condition persist as
    assigned; the restart loses nothing; the consistency check passes; the export holds every record; and the
    server backed the database up at every start and stop.

Inputs:
    The rehearsal configuration and the development assignments (read-only); a temporary database and export
    folder.

Outputs:
    unittest results.

Dependencies:
    src/scripts/participant_store.py, participant_server.py, participant_data.py (run as subprocesses),
    experiment_plan.py, participant_flow.py (which questionnaire items each condition is asked);
    tests/study_data_fixtures.py (valid answers).

Usage:
    python3 -B -m unittest tests.test_pilot_procedure
"""

import csv
import json
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import experiment_plan as ep  # noqa: E402
import participant_flow as flow  # noqa: E402
import presentation_edits as pe  # noqa: E402
from study_data_fixtures import submission  # noqa: E402


def questionnaire_items(condition: str) -> int:
    """How many questionnaire rows a participant in `condition` leaves: every item of both questionnaires they are
    asked (an optional one left blank is stored as ''); an item shown only with a part of the interface their
    view lacks is never asked, so never stored."""
    return sum(len(flow.offered_items(spec, condition)) for spec in flow.load_questionnaires().values())


REHEARSAL = ROOT / "src" / "data" / "experiment_configs" / "rehearsal.json"
CONFIG = ep.load_config(REHEARSAL)
PAYLOAD = ep.load_assignments(CONFIG)
LOW = next(r["participant"] for r in PAYLOAD["assignments"] if r["condition"] == "low")
HIGH = next(r["participant"] for r in PAYLOAD["assignments"] if r["condition"] == "high")


def script(name, *args, check=True):
    """Run one of the project's scripts the way a researcher would; returns the completed process."""
    result = subprocess.run([sys.executable, "-B", str(SCRIPTS / name), *map(str, args)], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"{name} {args} failed ({result.returncode}): {result.stderr}")
    return result


class ServerProcess:
    """participant_server.py as its own process on a free port, stopped with Ctrl+C."""

    def __init__(self, db: Path):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "participant_server.py"),
                                      "--experiment-config", str(REHEARSAL), "--db", str(db), "--port", str(self.port)],
                                     cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            try:
                urllib.request.urlopen(self.base + "/static/style.css", timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise AssertionError(self.proc.stderr.read())
                time.sleep(0.05)
        raise AssertionError("the server did not start")

    def stop(self):
        self.proc.send_signal(signal.SIGINT)
        out, err = self.proc.communicate(timeout=30)
        assert self.proc.returncode == 0, err
        return out

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method="GET" if body is None else "POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()


class Browser:
    """One participant's browser: only the link, and every response it was ever sent."""

    def __init__(self, link: str):
        self.token = link.rsplit("/p/", 1)[1]
        self.server = None
        self.seen = []  # every participant-facing response body
        self.conversations = []  # (trial_id, /api/stimulus response)
        self.workspaces = []  # /api/workspace responses
        self.skills = []  # /api/skills responses

    def get(self, path):
        status, body = self.server.request(path)
        self.seen.append(body)
        return status, body

    def scenario(self, stage):
        """Load a conversation as scenario_view.js does, look at the workspace and a skill, and log events."""
        q = f"t={self.token}"
        status, body = self.get(f"/api/stimulus?{q}")
        assert status == 200, body
        self.conversations.append((stage["trial_id"], json.loads(body)))
        status, workspace = self.get(f"/api/workspace?{q}")
        assert status == 200
        self.workspaces.append(json.loads(workspace))
        status, skills = self.get(f"/api/skills?{q}")
        self.skills.append((stage["trial_id"], json.loads(skills)))
        skill = json.loads(skills)[0]["id"]
        assert self.get(f"/api/skill?{q}&k={skill}")[0] == 200
        for event in ({"type": "skills_tab_open"},
                      {"type": "skill_doc_open", "skill_id": skill},
                      {"type": "skill_doc_view", "skill_id": skill, "duration_ms": 3000},
                      {"type": "scenario_visible", "duration_ms": 20000}):
            status, body = self.server.request("/api/log", {"token": self.token, "trial_id": stage["trial_id"], **event})
            assert status == 204, body

    def walk(self, until=None):
        status, body = self.get(f"/p/{self.token}")
        assert status == 200
        status, body = self.get(f"/api/stage?t={self.token}")
        stage = json.loads(body)["stage"]
        while stage is not None and stage["key"] != until:
            if stage["kind"] == "scenario":
                self.scenario(stage)
            else:  # no conversation content outside a scenario stage
                assert self.server.request(f"/api/stimulus?t={self.token}")[0] == 404
            status, body = self.server.request("/api/stage/complete",
                                               {"token": self.token, "stage_key": stage["key"], **submission(stage)})
            assert status == 200, (stage["key"], body)
            self.seen.append(body)
            stage = json.loads(body)["stage"]
        return stage


class PilotProcedureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp())
        cls.db = cls.folder / "rehearsal.sqlite3"
        out = script("participant_store.py", "--config", REHEARSAL, "--db", cls.db, "--enrol-all", "--links",
                     "--base-url", "http://127.0.0.1:8765").stdout
        # id, link, and "reserve for <id>" on a reserve participant's line
        links = dict(line.split("\t")[:2] for line in out.splitlines() if "\t" in line)
        cls.browsers = {p: Browser(links[p]) for p in (LOW, HIGH)}

        cls.server = ServerProcess(cls.db)
        for browser in cls.browsers.values():
            browser.server = cls.server
        cls.browsers[LOW].walk(until="trial_04_scenario")
        cls.browsers[HIGH].walk(until="trial_02_assessment")
        cls.server_probe = {path: cls.server.request(path)[0] for path in (
            "/", "/chat", "/static/app.js", f"/api/stimulus?s={CONFIG['practice']['stimulus']}&c=high",
            f"/data/participants/{cls.db.name}", "/static/../../data/participants/", "/src/data/stimuli/")}
        cls.first_stop = cls.server.stop()

        cls.server = ServerProcess(cls.db)  # the next morning: same database, new process
        for browser in cls.browsers.values():
            browser.server = cls.server
        cls.resumed = {p: json.loads(cls.server.request(f"/api/stage?t={b.token}")[1])["stage"]["key"]
                       for p, b in cls.browsers.items()}
        for browser in cls.browsers.values():
            browser.walk()
        cls.second_stop = cls.server.stop()

        cls.check = script("participant_data.py", "--config", REHEARSAL, "--db", cls.db, "check", check=False)
        script("participant_data.py", "--config", REHEARSAL, "--db", cls.db, "export", "--out", cls.folder / "export")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def query(self, sql, *params):
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def plan(self, participant):
        return ep.plan_for(CONFIG, participant, PAYLOAD)

    def test_the_two_conditions_differ_only_by_the_activity_log(self):
        for participant, browser in self.browsers.items():
            scenarios = ep.scenario_stages(self.plan(participant), practice=True)
            self.assertEqual([t for t, _ in browser.conversations], [s["trial_id"] for s in scenarios])
            for stage, (_, view) in zip(scenarios, browser.conversations):
                stored = json.loads((ROOT / "src" / "data" / "stimuli" / stage["stimulus"] / "low.json").read_text())
                stored = pe.apply(stage["stimulus"], stored)  # the documented presentation edits, if any
                if participant == HIGH:
                    self.assertTrue(view.get("trace"), stage["trial_id"])
                else:
                    self.assertNotIn("trace", view, stage["trial_id"])
                for key in ("user_request", "final_response"):
                    self.assertEqual(view[key], stored[key], (stage["trial_id"], key))

    def test_the_workspace_and_skills_are_the_same_in_both_conditions(self):
        workspaces = {json.dumps(w, sort_keys=True) for b in self.browsers.values() for w in b.workspaces}
        self.assertEqual(len(workspaces), 1)  # every conversation starts from the same workspace, in both conditions
        # The skills offered depend on the scenario only: the practice is the same scenario for both participants.
        low_practice, high_practice = (self.browsers[p].skills[0][1] for p in (LOW, HIGH))
        self.assertEqual(low_practice, high_practice)

    def test_no_participant_response_names_what_a_trial_is(self):
        for participant, browser in self.browsers.items():
            stages = self.plan(participant)["stages"]
            secrets = {s["stimulus"] for s in stages if "stimulus" in s} | {s["scenario"] for s in stages if "scenario" in s}
            secrets |= {json.dumps(v) for v in ("safe", "unsafe", ep.REHEARSAL_VERSION, "low", "high")}
            secrets |= {'"condition"', '"stimulus"', '"version"', "rehearsal-"}
            for body in browser.seen:
                for secret in secrets:
                    self.assertNotIn(secret, body, (participant, secret))

    def test_researcher_routes_and_data_files_are_unreachable(self):
        self.assertEqual(set(self.server_probe.values()) - {400, 404}, set(), self.server_probe)

    def test_order_and_condition_persist_as_assigned(self):
        for participant in (LOW, HIGH):
            stored = self.query("SELECT stage_key, trial_id, stimulus, stimulus_version, condition FROM plan_stages "
                                "WHERE participant_id = ? ORDER BY stage_index", participant)
            derived = [(s["key"], s.get("trial_id"), s.get("stimulus"), s.get("version"), s.get("condition"))
                       for s in self.plan(participant)["stages"]]
            self.assertEqual([tuple(r.values()) for r in stored], derived)
            row = next(r for r in PAYLOAD["assignments"] if r["participant"] == participant)
            self.assertEqual(self.query("SELECT condition FROM participants WHERE participant_id = ?",
                                        participant)[0]["condition"], row["condition"])

    def test_the_restart_resumed_both_where_they_were_and_both_finished(self):
        self.assertEqual(self.resumed, {LOW: "trial_04_scenario", HIGH: "trial_02_assessment"})
        done = self.query("SELECT participant_id, completed_at, stages_completed, stages_total FROM participant_overview")
        self.assertEqual({r["participant_id"] for r in done if r["completed_at"]}, {LOW, HIGH})
        self.assertTrue(all(r["stages_completed"] == r["stages_total"] for r in done if r["participant_id"] in (LOW, HIGH)))

    def test_the_data_is_complete_consistent_and_exported(self):
        self.assertEqual(self.check.returncode, 0, self.check.stdout)
        for participant in (LOW, HIGH):
            trials = self.query("SELECT * FROM trial_summary WHERE participant_id = ?", participant)
            self.assertEqual(len(trials), 9)
            self.assertTrue(all(t["scenario_visible_ms"] == 20000 for t in trials))
            # Answers for every trial; none for the display-only practice questions.
            self.assertEqual([bool(t["performed_safely"]) for t in trials],
                             [t["trial_type"] == "experimental" for t in trials])
            condition = next(r["condition"] for r in PAYLOAD["assignments"] if r["participant"] == participant)
            self.assertEqual(len(self.query("SELECT * FROM questionnaire_responses WHERE participant_id = ?",
                                            participant)), questionnaire_items(condition))
        export = self.folder / "export"
        with open(export / "trials.csv", newline="", encoding="utf-8") as handle:
            rows = [r for r in csv.DictReader(handle) if r["participant_id"] in (LOW, HIGH)]
        self.assertEqual(len(rows), 18)
        self.assertTrue(all(r["skills_opened"] == "True" for r in rows))
        manifest = json.loads((export / "export_manifest.json").read_text())
        self.assertEqual(manifest["files"]["events.csv"]["rows"], self.query("SELECT COUNT(*) n FROM events")[0]["n"])

    def test_the_server_backed_up_at_every_start_and_stop(self):
        names = sorted(p.name for p in (self.folder / "backups" / "development").glob("*.sqlite3"))
        self.assertEqual([n.rsplit("-", 1)[1] for n in names],
                         ["startup.sqlite3", "shutdown.sqlite3", "startup.sqlite3", "shutdown.sqlite3"])
        self.assertIn("Backup:", self.first_stop + self.second_stop)


if __name__ == "__main__":
    unittest.main()
