"""Tests of participant_store.py: enrolment and opaque links, progress saved at each transition, resuming an
interrupted session from the database file, the refusal to skip or repeat a stage, and the fingerprint that stops
a participant being resumed into a changed stage sequence."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import experiment_plan as ep  # noqa: E402
import participant_store as store  # noqa: E402

CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
PAYLOAD = ep.load_assignments(CONFIG)
FIRST = PAYLOAD["assignments"][0]["participant"]


def temp_db():
    return Path(tempfile.mkdtemp()) / "participants.db"


def alternative_config(**overrides):
    """A configuration file identical to the development one apart from `overrides`."""
    raw = json.loads(ep.DEVELOPMENT_CONFIG.read_text(encoding="utf-8"))
    raw.update(overrides)
    path = Path(tempfile.mkdtemp()) / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return ep.load_config(path)


class ConnectTests(unittest.TestCase):
    def test_connect_creates_the_schema_and_can_be_called_again(self):
        path = temp_db()
        conn = store.connect(path)
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual({"participants", "progress", "meta"}, tables)
        conn.close()
        again = store.connect(path)  # an existing database opens unchanged
        self.assertEqual(again.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"],
                         str(store.SCHEMA_VERSION))

    def test_a_newer_schema_version_is_refused(self):
        path = temp_db()
        conn = store.connect(path)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(store.SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
        with self.assertRaises(store.StoreError):
            store.connect(path)


class EnrolmentTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(temp_db())

    def test_enrol_stores_the_assignment_facts_and_an_opaque_token(self):
        row = store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)
        plan = ep.plan_for(CONFIG, FIRST, PAYLOAD)
        self.assertEqual(row["participant_id"], FIRST)
        self.assertEqual(row["condition"], plan["condition"])
        self.assertEqual(row["list"], plan["list"])
        self.assertEqual(row["trials"], plan["trials"])
        self.assertEqual(row["simulated"], 1)
        self.assertGreaterEqual(len(row["token"]), 32)
        self.assertNotIn(FIRST, row["token"])  # the link must not reveal the pseudonymous id

    def test_enrolling_twice_is_refused(self):
        store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)
        with self.assertRaises(store.StoreError):
            store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)

    def test_unknown_participant_is_a_key_error(self):
        with self.assertRaises(KeyError):
            store.enrol(self.conn, CONFIG, "nobody", PAYLOAD)

    def test_enrol_all_gives_every_participant_a_distinct_token_and_is_repeatable(self):
        added = store.enrol_all(self.conn, CONFIG, PAYLOAD)
        self.assertEqual(len(added), len(PAYLOAD["assignments"]))
        tokens = {row["token"] for row in added}
        self.assertEqual(len(tokens), len(added))
        before = {r["participant_id"]: r["token"] for r in
                  self.conn.execute("SELECT participant_id, token FROM participants")}
        self.assertEqual(store.enrol_all(self.conn, CONFIG, PAYLOAD), [])  # nothing new
        after = {r["participant_id"]: r["token"] for r in
                 self.conn.execute("SELECT participant_id, token FROM participants")}
        self.assertEqual(after, before)  # existing links keep working

    def test_by_token_finds_the_participant_and_unknown_tokens_are_none(self):
        row = store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(store.by_token(self.conn, row["token"])["participant_id"], FIRST)
        self.assertIsNone(store.by_token(self.conn, "not-a-token"))
        self.assertIsNone(store.by_token(self.conn, ""))

    def test_link_format(self):
        self.assertEqual(store.link("abc"), "/p/abc")
        self.assertEqual(store.link("abc", "http://127.0.0.1:8765/"), "http://127.0.0.1:8765/p/abc")


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_db()
        self.conn = store.connect(self.path)
        store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)
        self.plan = ep.plan_for(CONFIG, FIRST, PAYLOAD)

    def complete_all(self):
        """Walk the whole sequence the way a participant would; returns the stage keys completed in order."""
        walked = []
        while True:
            stage = store.enter_stage(self.conn, CONFIG, FIRST, PAYLOAD)
            if stage is None:
                return walked
            walked.append(stage["key"])
            store.complete_stage(self.conn, CONFIG, FIRST, stage["key"], PAYLOAD)

    def test_a_new_participant_starts_at_the_first_stage(self):
        self.assertEqual(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"], "consent")

    def test_entering_a_stage_keeps_the_first_arrival_time(self):
        store.enter_stage(self.conn, CONFIG, FIRST, PAYLOAD)
        first = self.conn.execute("SELECT entered_at FROM progress WHERE participant_id = ?",
                                  (FIRST,)).fetchone()["entered_at"]
        store.enter_stage(self.conn, CONFIG, FIRST, PAYLOAD)  # the link was reopened
        rows = self.conn.execute("SELECT entered_at FROM progress WHERE participant_id = ?", (FIRST,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["entered_at"], first)

    def test_completing_each_stage_walks_the_whole_sequence_once(self):
        walked = self.complete_all()
        self.assertEqual(walked, [s["key"] for s in self.plan["stages"]])
        self.assertIsNone(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD))
        self.assertIsNotNone(store.record(self.conn, FIRST)["completed_at"])

    def test_no_stage_is_recorded_twice(self):
        self.complete_all()
        rows = self.conn.execute("SELECT stage_key, COUNT(*) n FROM progress WHERE participant_id = ? "
                                 "GROUP BY stage_key HAVING n > 1", (FIRST,)).fetchall()
        self.assertEqual(rows, [])
        self.assertEqual(len(store.completed_stages(self.conn, FIRST)), len(self.plan["stages"]))

    def test_completing_the_same_stage_again_changes_nothing(self):
        store.complete_stage(self.conn, CONFIG, FIRST, "consent", PAYLOAD)
        before = store.completed_stages(self.conn, FIRST)
        nxt = store.complete_stage(self.conn, CONFIG, FIRST, "consent", PAYLOAD)
        self.assertEqual(store.completed_stages(self.conn, FIRST), before)
        self.assertEqual(nxt["key"], "instructions")

    def test_a_stage_cannot_be_skipped(self):
        with self.assertRaises(store.StoreError):
            store.complete_stage(self.conn, CONFIG, FIRST, "questionnaire", PAYLOAD)
        self.assertEqual(store.completed_stages(self.conn, FIRST), [])

    def test_a_stage_outside_the_sequence_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.complete_stage(self.conn, CONFIG, FIRST, "debriefing", PAYLOAD)

    def test_progress_survives_reopening_the_database(self):
        for key in ("consent", "instructions", "practice_scenario"):
            store.complete_stage(self.conn, CONFIG, FIRST, key, PAYLOAD)
        expected = store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"]
        self.conn.close()
        reopened = store.connect(self.path)
        self.assertEqual(store.completed_stages(reopened, FIRST),
                         ["consent", "instructions", "practice_scenario"])
        self.assertEqual(store.current_stage(reopened, CONFIG, FIRST, PAYLOAD)["key"], expected)

    def test_reopening_the_link_mid_trial_returns_the_same_stage(self):
        for key in ("consent", "instructions", "practice_scenario", "practice_assessment"):
            store.complete_stage(self.conn, CONFIG, FIRST, key, PAYLOAD)
        stage = store.enter_stage(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(stage["key"], "trial_01_scenario")
        for _ in range(3):
            self.assertEqual(store.enter_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"], "trial_01_scenario")
        self.assertEqual(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["trial_id"], stage["trial_id"])

    def test_status_counts_stages_and_names_the_current_one(self):
        store.complete_stage(self.conn, CONFIG, FIRST, "consent", PAYLOAD)
        state = store.status(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(state["stages_completed"], 1)
        self.assertEqual(state["stages"], len(self.plan["stages"]))
        self.assertEqual(state["current_stage"], "instructions")
        self.assertIs(state["plan_changed"], False)
        self.assertIsNone(state["completed_at"])


class PlanChangeTests(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(temp_db())
        store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)

    def test_a_changed_trial_count_stops_the_participant_being_resumed(self):
        changed = alternative_config(trials_per_participant=3)
        with self.assertRaises(store.StoreError):
            store.current_stage(self.conn, changed, FIRST, PAYLOAD)
        with self.assertRaises(store.StoreError):
            store.complete_stage(self.conn, changed, FIRST, "consent", PAYLOAD)

    def test_a_changed_practice_stimulus_stops_the_participant_being_resumed(self):
        changed = alternative_config(practice={"stimulus": "user_task_10__no_injection", "assessment": True})
        with self.assertRaises(store.StoreError):
            store.current_stage(self.conn, changed, FIRST, PAYLOAD)

    def test_status_reports_a_changed_plan_instead_of_failing(self):
        changed = alternative_config(trials_per_participant=3)
        state = store.status(self.conn, changed, FIRST, PAYLOAD)
        self.assertIs(state["plan_changed"], True)
        self.assertNotIn("current_stage", state)

    def test_the_unchanged_configuration_still_resumes(self):
        self.assertEqual(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"], "consent")


if __name__ == "__main__":
    unittest.main()
