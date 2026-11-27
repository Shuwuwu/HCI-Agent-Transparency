"""
Purpose:
    Tests of participant_data.py, the researcher access to the participant database: everything about one
    participant and nobody else, CSV exports that reproduce the database (no tokens, practice marked, private
    files, a manifest that matches), the researcher views, and the consistency check catching damaged records.

Inputs:
    Temporary databases filled by tests/study_data_fixtures.py (the in-process flow).

Outputs:
    unittest results.

Dependencies:
    src/scripts/participant_data.py, participant_store.py; tests/study_data_fixtures.py.

Usage:
    python3 -B -m unittest tests.test_participant_data
"""

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import participant_data as pdata  # noqa: E402
import participant_store as store  # noqa: E402
from study_data_fixtures import PARTICIPANTS, new_database, walk  # noqa: E402

DONE, PARTWAY = PARTICIPANTS[0], PARTICIPANTS[1]


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class ResearcherDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn, cls.path = new_database()
        walk(cls.conn, DONE)
        walk(cls.conn, PARTWAY, until="trial_02_scenario")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def count(self, sql, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    # -- one participant ---------------------------------------------------------------------------------------
    def test_show_holds_everything_about_one_participant_and_nobody_else(self):
        data = pdata.participant_data(self.conn, DONE)
        self.assertNotIn("token", data["participant"])
        self.assertNotIn("consent_text", data["consent"])
        self.assertEqual(len(data["plan_stages"]), self.count("SELECT COUNT(*) FROM plan_stages WHERE participant_id = ?", DONE))
        self.assertEqual(len(data["responses"]), 8)  # the 8 trials; the practice questions store nothing
        self.assertEqual(len(data["trials"]), 9)
        self.assertEqual(len(data["questionnaire"]), len(store.questionnaire_of(self.conn, DONE)))
        self.assertEqual(len(data["events"]), self.count("SELECT COUNT(*) FROM events WHERE participant_id = ?", DONE))
        blob = json.dumps(data)
        self.assertNotIn(PARTWAY, blob)
        self.assertNotIn(store.record(self.conn, DONE)["token"], blob)
        self.assertIn("consent_text", pdata.participant_data(self.conn, DONE, consent_text=True)["consent"])
        with self.assertRaises(KeyError):
            pdata.participant_data(self.conn, "nobody")

    def test_the_views_summarise_progress_and_trials(self):
        overview = {r["participant_id"]: r for r in pdata._rows(self.conn, "SELECT * FROM participant_overview")}
        self.assertIsNotNone(overview[DONE]["completed_at"])
        self.assertEqual(overview[DONE]["stages_completed"], overview[DONE]["stages_total"])
        self.assertEqual(overview[PARTWAY]["current_stage"], "trial_02_scenario")
        self.assertNotIn("token", overview[DONE])
        trials = pdata._rows(self.conn, "SELECT * FROM trial_summary WHERE participant_id = ? ORDER BY trial_ordinal",
                             (DONE,))
        self.assertEqual([t["trial_type"] for t in trials], ["practice"] + ["experimental"] * 8)
        self.assertTrue(all(t["scenario_s"] is not None and t["scenario_s"] >= 0 for t in trials))
        self.assertTrue(all(t["assessment_s"] is not None for t in trials))
        self.assertTrue(all(t["scenario_visible_ms"] == 9000 for t in trials))
        self.assertEqual((trials[0]["task_completion"], trials[0]["performed_safely"], trials[0]["confidence"]),
                         (None, None, None))  # display-only practice
        self.assertTrue(all(t["task_completion"] == "unsure" and t["performed_safely"] == "no" and t["confidence"] == 2
                            for t in trials[1:]))
        self.assertEqual(self.count("SELECT COUNT(*) FROM experimental_events WHERE trial_id LIKE '%-p00'"), 0)

    # -- exports -----------------------------------------------------------------------------------------------
    def test_the_export_reproduces_the_database_without_tokens(self):
        out = Path(tempfile.mkdtemp()) / "export"
        manifest = pdata.write_export(self.conn, out, self.path)
        expected = {"participants.csv": "SELECT COUNT(*) FROM participants",
                    "trials.csv": "SELECT COUNT(*) FROM plan_stages WHERE kind = 'scenario'",
                    "progress.csv": "SELECT COUNT(*) FROM progress", "events.csv": "SELECT COUNT(*) FROM events",
                    "questionnaire.csv": "SELECT COUNT(*) FROM questionnaire_responses"}
        tokens = [r[0] for r in self.conn.execute("SELECT token FROM participants")]
        for name, sql in expected.items():
            rows = read_csv(out / name)
            self.assertEqual(len(rows), self.count(sql), name)
            self.assertEqual(manifest["files"][name]["rows"], len(rows))
            self.assertEqual(manifest["files"][name]["sha256"], hashlib.sha256((out / name).read_bytes()).hexdigest())
            self.assertEqual((out / name).stat().st_mode & 0o777, 0o600, name)
            text = (out / name).read_text(encoding="utf-8")
            self.assertFalse(any(token in text for token in tokens), name)
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual(manifest["environment"], "development")
        events = read_csv(out / "events.csv")
        self.assertEqual({e["trial_type"] for e in events if e["trial_id"].endswith("-p00")}, {"practice"})
        self.assertEqual({e["trial_type"] for e in events if "-t" in e["trial_id"]}, {"experimental"})
        trials = read_csv(out / "trials.csv")
        done = [t for t in trials if t["participant_id"] == DONE]
        self.assertTrue(all(t["trace_cards_expanded"] == "1" and t["workspace_inspected"] == "True" for t in done))
        self.assertEqual([t["task_completion"] for t in done], [""] + ["unsure"] * 8)  # practice: nothing stored
        self.assertTrue(all(json.loads(t["workspace_tabs_viewed_ms"]) == {"calendar": 1500} for t in done))
        self.assertNotIn("workspace_panel_ms", done[0])
        self.assertEqual(json.loads(done[0]["trace_cards_viewed_ms"]), {"step-1": 700})

    def test_a_participant_export_holds_only_that_participant(self):
        out = Path(tempfile.mkdtemp()) / "one"
        pdata.write_export(self.conn, out, self.path, PARTWAY)
        for name in ("participants.csv", "trials.csv", "progress.csv", "events.csv", "questionnaire.csv"):
            self.assertEqual({r["participant_id"] for r in read_csv(out / name)} - {PARTWAY}, set(), name)
        self.assertEqual(len(read_csv(out / "trials.csv")), 9)
        with self.assertRaises(KeyError):
            pdata.write_export(self.conn, Path(tempfile.mkdtemp()), self.path, "nobody")

    def test_an_empty_database_exports_files_with_their_header_rows(self):
        conn = store.connect(Path(tempfile.mkdtemp()) / "empty.sqlite3", "development")
        out = Path(tempfile.mkdtemp()) / "export"
        pdata.write_export(conn, out, Path("empty.sqlite3"))
        conn.close()
        for name in ("participants.csv", "trials.csv", "progress.csv", "events.csv", "questionnaire.csv"):
            header = (out / name).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(header), 1, name)
            self.assertIn("participant_id", header[0], name)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.conn, _ = new_database()
        walk(self.conn, DONE)
        walk(self.conn, PARTWAY, until="trial_01_assessment")

    def tearDown(self):
        self.conn.close()

    def test_a_database_written_by_the_flow_is_consistent(self):
        self.assertEqual(pdata.find_problems(self.conn), [])

    def test_a_database_not_yet_upgraded_to_version_8_can_be_checked(self):
        """preflight.py checks the database read-only, before the server upgrades it, so the check must also read
        a questionnaire_responses table without stage_key; every such row is a post-study answer."""
        self.conn.execute("SAVEPOINT old")
        self.conn.execute("DELETE FROM questionnaire_responses WHERE stage_key = 'pre_questionnaire'")
        self.conn.execute("ALTER TABLE questionnaire_responses DROP COLUMN stage_key")
        problems = pdata.find_problems(self.conn)
        self.assertTrue(store.questionnaire_of(self.conn, DONE, "questionnaire"))
        self.assertEqual(store.questionnaire_of(self.conn, DONE, "pre_questionnaire"), {})
        self.conn.execute("ROLLBACK TO old")
        self.conn.execute("RELEASE old")
        # The only problem is the one that is true of such a database: no pre-study answers for completed stages.
        self.assertTrue(problems)
        self.assertTrue(all("pre_questionnaire" in p for p in problems), problems)

    def test_damaged_records_are_reported(self):
        damage = [
            ("DELETE FROM responses WHERE participant_id = ? AND stage_key = 'trial_03_assessment'", "response"),
            ("UPDATE plan_stages SET stimulus = 'other' WHERE participant_id = ? AND stage_key = 'trial_01_scenario'",
             "fingerprint"),
            ("DELETE FROM consents WHERE participant_id = ?", "consent"),
            ("UPDATE participants SET completed_at = NULL WHERE participant_id = ?", "completed_at"),
            ("DELETE FROM questionnaire_responses WHERE participant_id = ?", "questionnaire"),
            ("DELETE FROM questionnaire_responses WHERE participant_id = ? AND stage_key = 'pre_questionnaire'",
             "pre_questionnaire"),
            ("UPDATE questionnaire_responses SET stage_key = 'debriefing' WHERE participant_id = ?",
             "not a questionnaire stage"),
            ("DELETE FROM events WHERE participant_id = ? AND trial_id LIKE '%-t02' AND type = 'session_start'",
             "session_start"),
        ]
        for sql, word in damage:
            with self.subTest(word=word):
                self.conn.execute("SAVEPOINT damage")
                self.conn.execute(sql, (DONE,))
                problems = pdata.find_problems(self.conn)
                self.conn.execute("ROLLBACK TO damage")
                self.conn.execute("RELEASE damage")
                self.assertTrue(any(word in p and DONE in p for p in problems), problems)

    def test_an_event_in_another_participants_trial_is_reported(self):
        store.record_event(self.conn, PARTWAY, "trial_01_assessment", f"{DONE}-t01", False,
                           {"type": "workspace_panel_open"}, "browser")
        self.assertTrue(any("not theirs" in p for p in pdata.find_problems(self.conn)))


if __name__ == "__main__":
    unittest.main()
