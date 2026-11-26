"""
Purpose:
    Tests of the participant-database backups and restores (participant_data.backup, verify_backup, restore) and
    of the backups the participant server makes when it starts and stops: a backup is one verified, private,
    self-contained file equal to its source; a restore brings a database back to exactly a backup's state, keeps
    the replaced contents, and refuses damaged, foreign or in-use cases.

Inputs:
    Temporary databases filled by tests/study_data_fixtures.py; the participant server run as a subprocess on one.

Outputs:
    unittest results.

Dependencies:
    src/scripts/participant_data.py, participant_store.py, participant_server.py; tests/study_data_fixtures.py.

Usage:
    python3 -B -m unittest tests.test_participant_backup
"""

import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import participant_data as pdata  # noqa: E402
import participant_store as store  # noqa: E402
from study_data_fixtures import PARTICIPANTS, new_database, walk  # noqa: E402


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        conn, self.path = new_database(self.folder)
        walk(conn, PARTICIPANTS[0], until="trial_02_scenario")
        self.counts = pdata.table_counts(conn)
        conn.close()
        self.addCleanup(shutil.rmtree, self.folder, True)

    def current_counts(self):
        conn = store.connect(self.path, "development")
        try:
            return pdata.table_counts(conn)
        finally:
            conn.close()

    def test_a_backup_is_a_verified_private_single_file_equal_to_its_source(self):
        made = pdata.backup(self.path, "development")
        path = made["path"]
        self.assertEqual(path.parent, self.folder / "backups" / "development")
        self.assertTrue(path.name.startswith("development-") and path.name.endswith("-manual.sqlite3"))
        self.assertEqual(made["counts"], self.counts)
        self.assertEqual(pdata.verify_backup(path), {"environment": "development", "counts": self.counts})
        # One self-contained file and its checksum: no -wal or -shm beside the copy.
        self.assertEqual({p.name for p in path.parent.iterdir()}, {path.name, f"{path.name}.sha256"})
        for candidate in (path, Path(f"{path}.sha256"), path.parent):
            expected = 0o700 if candidate.is_dir() else 0o600
            self.assertEqual(candidate.stat().st_mode & 0o777, expected, candidate)

    def test_a_changed_or_damaged_backup_is_refused(self):
        path = pdata.backup(self.path, "development")["path"]
        with open(path, "r+b") as handle:  # one byte changed after the backup was written
            handle.seek(2000)
            byte = handle.read(1)
            handle.seek(2000)
            handle.write(bytes([byte[0] ^ 0xFF]))
        with self.assertRaises(pdata.BackupError):
            pdata.verify_backup(path)
        Path(f"{path}.sha256").unlink()
        with self.assertRaises(pdata.BackupError):
            pdata.verify_backup(path)

    def test_restore_brings_back_the_backup_and_keeps_what_it_replaced(self):
        path = pdata.backup(self.path, "development")["path"]
        conn = store.connect(self.path, "development")
        walk(conn, PARTICIPANTS[1], until="trial_01_scenario")  # newer data, after the backup
        newer = pdata.table_counts(conn)
        conn.close()
        with self.assertRaises(pdata.BackupError):
            pdata.restore(path, self.path, "development")  # exists: needs replace
        done = pdata.restore(path, self.path, "development", replace=True)
        self.assertEqual(self.current_counts(), self.counts)
        self.assertEqual(pdata.verify_backup(done["previous"])["counts"], newer)  # nothing is lost by restoring
        self.assertIn("pre-restore", done["previous"].name)
        conn = store.connect(self.path, "development")
        self.assertEqual(store.completed_stages(conn, PARTICIPANTS[1]), [])
        self.assertEqual(pdata.find_problems(conn), [])
        conn.close()

    def test_a_lost_database_can_be_restored_from_a_backup(self):
        path = pdata.backup(self.path, "development")["path"]
        for candidate in [self.path] + [Path(f"{self.path}{s}") for s in store.SIDECARS]:
            candidate.unlink(missing_ok=True)
        done = pdata.restore(path, self.path, "development")
        self.assertIsNone(done["previous"])
        self.assertEqual(self.current_counts(), self.counts)

    def test_a_backup_of_the_other_environment_is_refused(self):
        path = pdata.backup(self.path, "development")["path"]
        with self.assertRaises(pdata.BackupError):
            pdata.restore(path, self.folder / "study.sqlite3", "study")

    def test_a_database_in_use_is_not_restored_over(self):
        path = pdata.backup(self.path, "development")["path"]
        writer = sqlite3.connect(self.path)
        writer.execute("BEGIN IMMEDIATE")  # a server request in the middle of a write
        try:
            with self.assertRaises(pdata.BackupError):
                pdata.restore(path, self.path, "development", replace=True)
        finally:
            writer.rollback()
            writer.close()

    def test_a_study_backup_keeps_its_append_only_protection(self):
        study = self.folder / "study.sqlite3"
        store.connect(study, "study").close()
        restored = self.folder / "restored" / "study.sqlite3"
        pdata.restore(pdata.backup(study, "study")["path"], restored, "study")
        conn = store.connect(restored, "study")
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        conn.close()
        self.assertIn("study_keep_responses_u", triggers)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerBackupTests(unittest.TestCase):
    def test_the_server_backs_up_when_it_starts_and_when_it_is_stopped(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        conn, path = new_database(folder)
        conn.close()
        port = free_port()
        server = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "src" / "scripts" / "participant_server.py"), "--port", str(port),
             "--experiment-config", str(ROOT / "src" / "data" / "experiment_configs" / "development.json"),
             "--db", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):  # wait until it answers
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/static/style.css", timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            server.send_signal(signal.SIGINT)  # Ctrl+C
            out, err = server.communicate(timeout=20)
        finally:
            if server.poll() is None:
                server.kill()
        self.assertEqual(server.returncode, 0, err)
        backups = sorted(p.name for p in (folder / "backups" / "development").glob("*.sqlite3"))
        self.assertEqual([name.rsplit("-", 1)[1] for name in backups], ["startup.sqlite3", "shutdown.sqlite3"])
        self.assertIn("Backup:", out)


if __name__ == "__main__":
    unittest.main()
