"""
Purpose:
    Tests of preflight.py, the readiness check before a session: a ready rehearsal passes with only the expected
    warnings; damaged records, a database of the other environment, files others can read, a changed backup and a
    busy port are caught; and the check never creates or changes a database.

Inputs:
    Temporary databases filled by tests/study_data_fixtures.py; the rehearsal configuration (read-only).

Outputs:
    unittest results.

Dependencies:
    src/scripts/preflight.py, participant_data.py, participant_store.py; tests/study_data_fixtures.py.

Usage:
    python3 -B -m unittest tests.test_preflight
"""

import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import participant_data as pdata  # noqa: E402
import participant_store as store  # noqa: E402
import preflight  # noqa: E402
from study_data_fixtures import CONFIG, PAYLOAD, PARTICIPANTS  # noqa: E402

REHEARSAL = ROOT / "src" / "data" / "experiment_configs" / "rehearsal.json"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.db = self.folder / "rehearsal.sqlite3"
        conn = store.connect(self.db, "development")
        rehearsal = preflight.ep.load_config(REHEARSAL)
        store.enrol_all(conn, rehearsal, PAYLOAD)
        conn.close()
        pdata.backup(self.db, "development")

    def findings(self, port=None, db=None):
        report = preflight.run(REHEARSAL, db or self.db, port or free_port())
        return {check: (status, detail) for status, check, detail in report.findings}, report

    def test_a_ready_rehearsal_passes_with_only_the_expected_warnings(self):
        found, report = self.findings()
        self.assertFalse(report.failed, report.findings)
        for check in ("stimuli", "assignments", "database", "records", "enrolment", "permissions", "backup", "port"):
            self.assertEqual(found[check][0], preflight.PASS, (check, found[check]))
        self.assertEqual(found["configuration"][0], preflight.WARN)  # a rehearsal is never study data

    def test_damaged_records_fail(self):
        conn = store.connect(self.db)
        conn.execute("INSERT INTO progress (participant_id, stage_key, stage_index, entered_at, completed_at) "
                     "VALUES (?, 'consent', 0, 'now', 'now')", (PARTICIPANTS[0],))  # completed without a consent
        conn.commit()
        conn.close()
        found, report = self.findings()
        self.assertTrue(report.failed)
        self.assertEqual(found["records"][0], preflight.FAIL)

    def test_a_database_of_the_other_environment_fails(self):
        study = self.folder / "study.sqlite3"
        store.connect(study, "study").close()
        found, _ = self.findings(db=study)
        self.assertEqual(found["database"][0], preflight.FAIL)

    def test_files_others_can_read_fail(self):
        self.db.chmod(0o644)
        found, _ = self.findings()
        self.assertEqual(found["permissions"][0], preflight.FAIL)

    def test_a_changed_backup_fails(self):
        newest = sorted((self.folder / "backups" / "development").glob("*.sqlite3"))[-1]
        newest.write_bytes(newest.read_bytes() + b"x")
        found, _ = self.findings()
        self.assertEqual(found["backup"][0], preflight.FAIL)

    def test_a_busy_port_is_reported(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            found, _ = self.findings(port=busy.getsockname()[1])
        self.assertEqual(found["port"][0], preflight.WARN)

    def test_it_never_creates_or_changes_a_database(self):
        missing = self.folder / "missing.sqlite3"
        found, _ = self.findings(db=missing)
        self.assertEqual(found["database"][0], preflight.WARN)
        self.assertFalse(missing.exists())
        before = self.db.read_bytes()
        self.findings()
        self.assertEqual(self.db.read_bytes(), before)

    def test_the_development_configuration_fails_on_its_unservable_unsafe_stimuli(self):
        report = preflight.run(CONFIG["path"], self.folder / "none.sqlite3", free_port())
        status = {check: status for status, check, _ in report.findings}
        self.assertEqual(status["stimuli"], preflight.FAIL)

    def test_consent_placeholders_are_bracketed_phrases_only(self):
        text = "Stored on [secure storage platform]. Contact [me@x.ca](mailto:me@x.ca).\n- [ ] I agree"
        self.assertEqual(preflight.CONSENT_PLACEHOLDER_RE.findall(text), ["secure storage platform"])


if __name__ == "__main__":
    unittest.main()
