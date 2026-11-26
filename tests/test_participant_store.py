"""Tests of participant_store.py: enrolment and opaque links, progress saved at each transition, resuming an
interrupted session from the database file, the refusal to skip or repeat a stage, and the fingerprint that stops
a participant being resumed into a changed stage sequence."""

import json
import sqlite3
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

    def test_an_older_database_gains_the_responses_table_and_keeps_its_data(self):
        path = temp_db()
        conn = store.connect(path)
        row = store.enrol(conn, CONFIG, FIRST, PAYLOAD)
        conn.executescript("DROP VIEW experimental_responses; DROP TABLE responses;")
        conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        conn = store.connect(path)
        self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"],
                         str(store.SCHEMA_VERSION))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM experimental_responses").fetchone()[0], 0)
        self.assertEqual(store.by_token(conn, row["token"])["participant_id"], FIRST)

    def test_a_version_3_database_gains_the_other_source_column_and_keeps_its_responses(self):
        """Databases already upgraded to version 3 (responses without sources_other) must not lose answers."""
        path = temp_db()
        conn = store.connect(path)
        store.enrol(conn, CONFIG, FIRST, PAYLOAD)
        conn.executescript("""
            DROP VIEW experimental_responses; DROP TABLE responses;
            CREATE TABLE responses (participant_id TEXT NOT NULL, stage_key TEXT NOT NULL, trial_id TEXT NOT NULL,
                practice INTEGER NOT NULL, stimulus TEXT NOT NULL, condition TEXT NOT NULL,
                performed_safely TEXT NOT NULL, confidence INTEGER NOT NULL, reasoning TEXT NOT NULL,
                sources TEXT NOT NULL, sources_offered TEXT NOT NULL, questions_sha256 TEXT NOT NULL,
                submitted_at TEXT NOT NULL, PRIMARY KEY (participant_id, stage_key));
            CREATE VIEW experimental_responses AS SELECT * FROM responses WHERE practice = 0;""")
        conn.execute("INSERT INTO responses VALUES (?, 'trial_01_assessment', 't', 0, 's', 'low', 'no', 4, 'r', "
                     "'[\"request\"]', '[\"request\"]', 'h', 'now')", (FIRST,))
        conn.execute("UPDATE meta SET value = '3' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        conn = store.connect(path)
        kept = store.response_of(conn, FIRST, "trial_01_assessment")
        self.assertEqual((kept["confidence"], kept["sources_other"]), (4, ""))
        self.assertEqual(conn.execute("SELECT sources_other FROM experimental_responses").fetchone()[0], "")

    def test_a_newer_schema_version_is_refused(self):
        path = temp_db()
        conn = store.connect(path)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(store.SCHEMA_VERSION + 1),))
        conn.commit()
        conn.close()
        with self.assertRaises(store.StoreError):
            store.connect(path)


class SchemaV5Tests(unittest.TestCase):
    """Schema version 5: the stage-sequence snapshot, the events table and the constraints on stored answers."""

    RESPONSE = {"trial_id": "P001-t01", "practice": False, "stimulus": "s", "condition": "low",
                "performed_safely": "yes", "confidence": 3, "reasoning": "r", "sources": ["request"],
                "sources_other": "", "sources_offered": ["request"], "questions_sha256": "0" * 64}

    def setUp(self):
        self.path = temp_db()
        self.conn = store.connect(self.path)
        store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)

    def test_enrolment_stores_the_whole_stage_sequence(self):
        plan = ep.plan_for(CONFIG, FIRST, PAYLOAD)
        rows = store.stored_plan_stages(self.conn, FIRST)
        self.assertEqual([r["stage_key"] for r in rows], [s["key"] for s in plan["stages"]])
        trial = next(r for r in rows if r["stage_key"] == "trial_01_scenario")
        source = next(s for s in plan["stages"] if s["key"] == "trial_01_scenario")
        self.assertEqual((trial["trial_id"], trial["stimulus"], trial["condition"], trial["stimulus_version"],
                          trial["practice"], trial["trial_ordinal"]),
                         (source["trial_id"], source["stimulus"], source["condition"], source["version"], 0, 1))
        self.assertEqual(next(r for r in rows if r["stage_key"] == "practice_scenario")["practice"], 1)
        self.assertIsNone(rows[0]["condition"])  # consent shows no scenario
        self.assertEqual(store.stored_plan_fingerprint(self.conn, FIRST), store.record(self.conn, FIRST)["plan_fingerprint"])

    def test_a_failed_snapshot_leaves_no_participant_behind(self):
        bad = {"participant": "PX", "condition": "low", "list": "A", "simulated": True, "trials": 1,
               "stages": [{"index": 0, "key": "consent", "kind": "nonsense"}]}
        original = ep.plan_for
        ep.plan_for = lambda *a, **k: bad
        try:
            with self.assertRaises(Exception):
                store.enrol(self.conn, CONFIG, "PX", PAYLOAD)
        finally:
            ep.plan_for = original
        self.assertIsNone(self.conn.execute("SELECT 1 FROM participants WHERE participant_id = 'PX'").fetchone())

    def test_reset_rewrites_the_snapshot_under_the_new_sequence(self):
        older = alternative_config(trials_per_participant=3)
        self.conn.execute("UPDATE participants SET plan_fingerprint = ? WHERE participant_id = ?",
                          (store.plan_fingerprint(ep.plan_for(older, FIRST, PAYLOAD)), FIRST))
        store.snapshot_plan(self.conn, FIRST, ep.plan_for(older, FIRST, PAYLOAD))
        self.conn.commit()
        store.reset(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(len(store.stored_plan_stages(self.conn, FIRST)),
                         len(ep.plan_for(CONFIG, FIRST, PAYLOAD)["stages"]))

    def test_out_of_range_answers_are_refused_by_the_database(self):
        for field, value in (("confidence", 9), ("performed_safely", "maybe"), ("condition", "medium")):
            with self.subTest(field=field), self.assertRaises(sqlite3.IntegrityError):
                store.record_response(self.conn, FIRST, "trial_01_assessment", {**self.RESPONSE, field: value})
            self.conn.rollback()
        self.assertIsNone(store.response_of(self.conn, FIRST, "trial_01_assessment"))
        with self.assertRaises(sqlite3.IntegrityError):  # record_response normalises the flag; the table checks it
            self.conn.execute("INSERT INTO responses VALUES (?, 'k', 't', 2, 's', 'low', 'no', 3, 'r', '[]', '[]', "
                              "'', 'h', 'now')", (FIRST,))

    def test_unknown_event_types_and_origins_are_refused_by_the_database(self):
        insert = ("INSERT INTO events (participant_id, trial_id, stage_key, practice, seq, type, origin, server_time) "
                  "VALUES (?, 'P001-t01', 'trial_01_scenario', 0, 0, ?, ?, 'now')")
        self.conn.execute(insert, (FIRST, "workspace_panel_open", "browser"))
        for kind, origin in (("keystroke", "browser"), ("workspace_panel_open", "elsewhere")):
            with self.subTest(kind=kind, origin=origin), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(insert, (FIRST, kind, origin))
        with self.assertRaises(sqlite3.IntegrityError):  # an event must belong to an enrolled participant
            self.conn.execute(insert, ("nobody", "workspace_panel_open", "browser"))

    def test_a_version_4_database_is_upgraded_with_its_answers_kept(self):
        self.conn.executescript("""
            DROP VIEW experimental_responses; DROP TABLE responses;
            CREATE TABLE responses (participant_id TEXT NOT NULL, stage_key TEXT NOT NULL, trial_id TEXT NOT NULL,
                practice INTEGER NOT NULL, stimulus TEXT NOT NULL, condition TEXT NOT NULL,
                performed_safely TEXT NOT NULL, confidence INTEGER NOT NULL, reasoning TEXT NOT NULL,
                sources TEXT NOT NULL, sources_offered TEXT NOT NULL, sources_other TEXT NOT NULL DEFAULT '',
                questions_sha256 TEXT NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY (participant_id, stage_key));
            CREATE VIEW experimental_responses AS SELECT * FROM responses WHERE practice = 0;
            DROP TABLE events; DROP TABLE plan_stages;""")
        self.conn.execute("INSERT INTO responses VALUES (?, 'trial_01_assessment', 't', 0, 's', 'high', 'no', 2, "
                          "'r', '[]', '[]', 'x', 'h', 'now')", (FIRST,))
        self.conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        self.conn.commit()
        self.conn.close()
        conn = store.connect(self.path)
        self.assertEqual(store.response_of(conn, FIRST, "trial_01_assessment")["sources_other"], "x")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM experimental_responses").fetchone()[0], 1)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'responses'").fetchone()[0]
        self.assertIn("CHECK (confidence BETWEEN 1 AND 5)", sql)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_a_version_4_row_that_breaks_a_constraint_stops_the_upgrade_unchanged(self):
        self.conn.executescript("""
            DROP VIEW experimental_responses; DROP TABLE responses;
            CREATE TABLE responses (participant_id TEXT NOT NULL, stage_key TEXT NOT NULL, trial_id TEXT NOT NULL,
                practice INTEGER NOT NULL, stimulus TEXT NOT NULL, condition TEXT NOT NULL,
                performed_safely TEXT NOT NULL, confidence INTEGER NOT NULL, reasoning TEXT NOT NULL,
                sources TEXT NOT NULL, sources_offered TEXT NOT NULL, sources_other TEXT NOT NULL DEFAULT '',
                questions_sha256 TEXT NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY (participant_id, stage_key));
            CREATE VIEW experimental_responses AS SELECT * FROM responses WHERE practice = 0;""")
        self.conn.execute("INSERT INTO responses VALUES (?, 'trial_01_assessment', 't', 0, 's', 'low', 'no', 7, "
                          "'r', '[]', '[]', '', 'h', 'now')", (FIRST,))
        self.conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
        self.conn.commit()
        self.conn.close()
        with self.assertRaises(store.StoreError):
            store.connect(self.path)
        raw = sqlite3.connect(self.path)
        self.assertEqual(raw.execute("SELECT confidence FROM responses").fetchone()[0], 7)  # nothing lost
        self.assertEqual(raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "4")


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
        self.assertEqual(nxt["key"], "practice_scenario")

    def test_a_stage_cannot_be_skipped(self):
        with self.assertRaises(store.StoreError):
            store.complete_stage(self.conn, CONFIG, FIRST, "questionnaire", PAYLOAD)
        self.assertEqual(store.completed_stages(self.conn, FIRST), [])

    def test_a_stage_outside_the_sequence_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.complete_stage(self.conn, CONFIG, FIRST, "debriefing", PAYLOAD)

    def test_progress_survives_reopening_the_database(self):
        for key in ("consent", "practice_scenario", "practice_assessment"):
            store.complete_stage(self.conn, CONFIG, FIRST, key, PAYLOAD)
        expected = store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"]
        self.conn.close()
        reopened = store.connect(self.path)
        self.assertEqual(store.completed_stages(reopened, FIRST),
                         ["consent", "practice_scenario", "practice_assessment"])
        self.assertEqual(store.current_stage(reopened, CONFIG, FIRST, PAYLOAD)["key"], expected)

    def test_reopening_the_link_mid_trial_returns_the_same_stage(self):
        for key in ("consent", "practice_scenario", "practice_assessment"):
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
        self.assertEqual(state["current_stage"], "practice_scenario")
        self.assertIs(state["plan_changed"], False)
        self.assertIsNone(state["completed_at"])


class ResetTests(unittest.TestCase):
    """--reset: a test participant can start again from consent, a study participant can never be erased."""

    def setUp(self):
        self.conn = store.connect(temp_db())
        self.row = store.enrol(self.conn, CONFIG, FIRST, PAYLOAD)
        for key in ("consent", "practice_scenario", "practice_assessment"):
            store.complete_stage(self.conn, CONFIG, FIRST, key, PAYLOAD)
        store.record_consent(self.conn, FIRST, "0" * 64, "consent text")

    def test_a_simulated_participant_restarts_at_consent_with_the_same_link(self):
        store.record_response(self.conn, FIRST, "practice_assessment", {
            "trial_id": "x", "practice": True, "stimulus": "s", "condition": "low", "performed_safely": "yes",
            "confidence": 3, "reasoning": "r", "sources": ["request"], "sources_other": "", "sources_offered": ["request"],
            "questions_sha256": "0" * 64})
        cleared = store.reset(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(cleared["stages_cleared"], 3)
        self.assertEqual(cleared["responses_cleared"], 1)  # redoing the trial must not keep the old answer
        self.assertTrue(cleared["consent_cleared"])
        self.assertEqual(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"], "consent")
        self.assertIsNone(store.consent_of(self.conn, FIRST))
        self.assertEqual(store.by_token(self.conn, self.row["token"])["participant_id"], FIRST)

    def test_a_study_participant_cannot_be_reset(self):
        self.conn.execute("UPDATE participants SET simulated = 0 WHERE participant_id = ?", (FIRST,))
        self.conn.commit()
        with self.assertRaises(store.StoreError):
            store.reset(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertEqual(len(store.completed_stages(self.conn, FIRST)), 3)
        self.assertIsNotNone(store.consent_of(self.conn, FIRST))


    def test_reset_reattaches_a_link_enrolled_under_an_older_stage_sequence(self):
        """A test link enrolled before the sequence changed is refused until reset, then starts at consent."""
        older = alternative_config(trials_per_participant=3)
        self.conn.execute("UPDATE participants SET plan_fingerprint = ? WHERE participant_id = ?",
                          (store.plan_fingerprint(ep.plan_for(older, FIRST, PAYLOAD)), FIRST))
        self.conn.commit()
        with self.assertRaises(store.StoreError):
            store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)
        self.assertTrue(store.reset(self.conn, CONFIG, FIRST, PAYLOAD)["plan_changed"])
        self.assertEqual(store.current_stage(self.conn, CONFIG, FIRST, PAYLOAD)["key"], "consent")

    def test_a_study_configuration_cannot_be_used_to_reset(self):
        with self.assertRaises(store.StoreError):
            store.reset(self.conn, alternative_config(simulated=False), FIRST, PAYLOAD)
        self.assertEqual(len(store.completed_stages(self.conn, FIRST)), 3)


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
