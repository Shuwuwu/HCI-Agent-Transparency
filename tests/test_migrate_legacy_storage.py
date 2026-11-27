"""
Purpose:
    Tests of migrate_legacy_storage.py on a synthetic legacy store (never the real one): a schema-version-4
    database and JSON-lines event files laid out as the retired storage wrote them. Checks that a dry run writes
    nothing, that --apply archives every legacy file verifiably and reproduces every row and event, that it runs
    only once, and that inconsistent legacy data stops it without leaving a partial database.

Inputs:
    Temporary files only; the development configuration and its assignments (read-only).

Outputs:
    unittest results.

Dependencies:
    src/scripts/migrate_legacy_storage.py, participant_store.py, experiment_plan.py.

Usage:
    python3 -B -m unittest tests.test_migrate_legacy_storage
"""

import json
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import experiment_plan as ep  # noqa: E402
import migrate_legacy_storage as mig  # noqa: E402
import participant_store as store  # noqa: E402

CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
PAYLOAD = ep.load_assignments(CONFIG)
FIRST, SECOND = (row["participant"] for row in PAYLOAD["assignments"][:2])


def legacy_store(folder: Path) -> tuple[Path, Path]:
    """A legacy database (schema version 4) and log folder: two participants, some progress, one consent, one
    answer, events for FIRST's practice (split over the top level and practice/, as the old server left them) and
    trial 1, and one researcher-preview file."""
    db = folder / "legacy" / "participants.db"
    db.parent.mkdir(parents=True)
    conn = store.connect(db)
    for participant in (FIRST, SECOND):
        store.enrol(conn, CONFIG, participant, PAYLOAD)
    store.record_consent(conn, FIRST, "0" * 64, "consent text")
    for key in ("consent", "pre_questionnaire", "practice_scenario"):
        store.complete_stage(conn, CONFIG, FIRST, key, PAYLOAD)
    store.record_response(conn, FIRST, "practice_assessment", {
        "trial_id": f"{FIRST}-p00", "practice": True, "stimulus": "user_task_13__no_injection",
        "condition": "low", "performed_safely": "yes", "confidence": 4, "reasoning": "r", "sources": ["request"],
        "sources_other": "", "sources_offered": ["request"], "questions_sha256": "0" * 64})
    # Back to the version-4 layout: no snapshot, events or questionnaire tables, no environment stamp.
    conn.executescript("DROP TABLE plan_stages; DROP TABLE events; DROP TABLE questionnaire_responses; "
                       "DELETE FROM meta WHERE key = 'environment'; UPDATE meta SET value = '4' "
                       "WHERE key = 'schema_version';")
    conn.close()
    logs = folder / "legacy" / "interaction_logs"
    (logs / "practice").mkdir(parents=True)
    plan = ep.plan_for(CONFIG, FIRST, PAYLOAD)
    practice = next(s for s in plan["stages"] if s.get("trial_id") == f"{FIRST}-p00")
    trial = next(s for s in plan["stages"] if s.get("trial_id") == f"{FIRST}-t01")

    def write(path, records):
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    t = lambda second: f"2026-11-26T10:00:{second:02d}.000+00:00"  # noqa: E731
    write(logs / f"{FIRST}-p00.jsonl", [
        {"seq": 0, "server_time": t(0), "session": f"{FIRST}-p00", "type": "session_start",
         "stimulus": practice["stimulus"], "condition": practice["condition"]}])
    write(logs / "practice" / f"{FIRST}-p00.jsonl", [
        {"seq": 0, "server_time": t(5), "session": f"{FIRST}-p00", "type": "workspace_panel_open", "client_ms": 1},
        {"seq": 1, "server_time": t(9), "session": f"{FIRST}-p00", "type": "workspace_panel_close",
         "duration_ms": 4000, "client_ms": 2}])
    write(logs / f"{FIRST}-t01.jsonl", [
        {"seq": 0, "server_time": t(20), "session": f"{FIRST}-t01", "type": "session_start",
         "stimulus": trial["stimulus"], "condition": trial["condition"]},
        {"seq": 1, "server_time": t(21), "session": f"{FIRST}-t01", "type": "skill_doc_open",
         "skill_id": "email-lookup", "client_ms": 3}])
    write(logs / "aaaaaaaa-1111-2222-3333-444444444444.jsonl", [
        {"seq": 0, "server_time": t(30), "session": "aaaaaaaa-1111-2222-3333-444444444444",
         "type": "workspace_panel_open"}])
    return db, logs


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.db, self.logs = legacy_store(self.folder)
        self.target = self.folder / "participants" / "development.sqlite3"
        self.archive_dir = self.folder / "participants" / "backups" / "legacy"

    def run_migration(self, apply=True):
        return mig.migrate(self.db, self.logs, CONFIG, self.target, self.archive_dir, apply)

    def test_a_dry_run_verifies_and_writes_nothing(self):
        report = self.run_migration(apply=False)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["events_imported"], 5)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.archive_dir.exists())

    def test_apply_reproduces_every_row_and_event(self):
        report = self.run_migration()
        self.assertEqual(report["legacy_rows"], {"participants": 2, "progress": 3, "consents": 1, "responses": 1})
        self.assertEqual(report["not_imported"]["files"], 1)  # the researcher preview
        conn = store.connect(self.target, "development")
        self.addCleanup(conn.close)
        for table, count in report["legacy_rows"].items():
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count, table)
        practice = store.trial_events(conn, FIRST, f"{FIRST}-p00")
        self.assertEqual([e["type"] for e in practice], ["session_start", "workspace_panel_open", "workspace_panel_close"])
        self.assertEqual([e["seq"] for e in practice], [0, 1, 2])
        self.assertTrue(all(e["origin"] == "legacy" and e["practice"] == 1 for e in practice))
        self.assertEqual(practice[0]["server_time"], "2026-11-26T10:00:00.000+00:00")  # original times kept
        self.assertEqual(practice[2]["duration_ms"], 4000)
        self.assertEqual(store.trial_events(conn, FIRST, f"{FIRST}-t01")[1]["skill_id"], "email-lookup")
        self.assertEqual(store.response_of(conn, FIRST, "practice_assessment")["confidence"], 4)
        self.assertEqual(store.consent_of(conn, FIRST)["consent_text"], "consent text")
        for participant in (FIRST, SECOND):
            self.assertEqual(store.stored_plan_fingerprint(conn, participant),
                             store.record(conn, participant)["plan_fingerprint"])
        self.assertEqual(json.loads(conn.execute("SELECT value FROM meta WHERE key = 'legacy_import'").fetchone()[0])
                         ["legacy_db_sha256"], mig.sha256_of(self.db))
        # The resumed flow continues where the participant was.
        self.assertEqual(store.current_stage(conn, CONFIG, FIRST, PAYLOAD)["key"], "practice_assessment")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_the_archive_holds_every_legacy_file_byte_for_byte_and_the_sources_stay(self):
        report = self.run_migration()
        archive = self.archive_dir / Path(report["archive"]["archive"]).name
        sha_line = Path(f"{archive}.sha256").read_text().split()
        self.assertEqual(sha_line[0], mig.sha256_of(archive))
        with tarfile.open(archive) as tar:
            names = {m.name for m in tar.getmembers() if m.isfile()}
        sources = [p for p in self.db.parent.iterdir() if p.name.startswith(self.db.name)]  # with its sidecars
        sources += [p for p in self.logs.rglob("*") if p.is_file()]
        self.assertEqual(len(names), len(sources))
        self.assertTrue(all(p.exists() for p in sources))  # nothing legacy is deleted
        self.assertEqual(self.archive_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_it_runs_only_once(self):
        self.run_migration()
        with self.assertRaises(mig.MigrationError):
            self.run_migration()

    def test_a_session_start_that_contradicts_the_plan_stops_it_and_leaves_no_database(self):
        path = self.logs / f"{FIRST}-t01.jsonl"
        records = mig.read_jsonl(path)
        records[0]["condition"] = "high" if records[0]["condition"] == "low" else "low"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        with self.assertRaises(mig.MigrationError):
            self.run_migration()
        self.assertFalse(self.target.exists())

    def test_a_study_participant_in_the_legacy_database_stops_it(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE participants SET simulated = 0 WHERE participant_id = ?", (SECOND,))
        conn.commit()
        conn.close()
        with self.assertRaises(mig.MigrationError):
            self.run_migration(apply=False)


if __name__ == "__main__":
    unittest.main()
