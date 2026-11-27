"""
Purpose:
    Tests of preflight.py, the readiness check before a session: a ready rehearsal passes with only the expected
    warnings; damaged records, a database of the other environment, files others can read, a changed backup and a
    busy port are caught; the stimulus review records must be current and every stimulus shown marked reviewed
    (a failure for the study configuration, a warning otherwise); and the check never creates or changes a
    database.

Inputs:
    Temporary databases filled by tests/study_data_fixtures.py; the rehearsal configuration (read-only).

Outputs:
    unittest results.

Dependencies:
    src/scripts/preflight.py, participant_data.py, participant_store.py; tests/study_data_fixtures.py.

Usage:
    python3 -B -m unittest tests.test_preflight
"""

import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # Simulated unsafe stimuli are servable only once reviewed (corpus.py reviewed_in). The lookup is patched so the
        # test does not depend on how many items the committed review record marks reviewed.
        with mock.patch.object(preflight.ps.corpus_cfg, "_reviewed", return_value=False):
            preflight.ps.is_eligible.cache_clear()
            report = preflight.run(CONFIG["path"], self.folder / "none.sqlite3", free_port())
        preflight.ps.is_eligible.cache_clear()
        status = {check: status for status, check, _ in report.findings}
        self.assertEqual(status["stimuli"], preflight.FAIL)

    def test_consent_placeholders_are_bracketed_phrases_only(self):
        text = "Stored on [secure storage platform]. Contact [me@x.ca](mailto:me@x.ca).\n- [ ] I agree"
        self.assertEqual(preflight.CONSENT_PLACEHOLDER_RE.findall(text), ["secure storage platform"])

    def completion_finding(self, load_debriefing):
        report = preflight.Report()
        with mock.patch.object(preflight.flow, "load_debriefing", load_debriefing):
            preflight.check_materials(report)
        return next((status, detail) for status, check, detail in report.findings if check == "completion screen")

    def test_the_completion_screen_check_reads_the_debriefing(self):
        self.assertEqual(self.completion_finding(preflight.flow.load_debriefing),
                         (preflight.PASS, "debriefing complete"))
        status, detail = self.completion_finding(lambda: {"markdown": "You will receive [amount] by [date]."})
        self.assertEqual((status, detail), (preflight.WARN, "debriefing placeholders to fill: ['amount', 'date']"))

    def test_a_missing_debriefing_fails(self):
        def missing():
            raise FileNotFoundError("debriefing.md")
        self.assertEqual(self.completion_finding(missing)[0], preflight.FAIL)


class ReviewCheckTests(unittest.TestCase):
    """check_reviews: current review records and manually reviewed stimuli."""

    STUDY_CORPUS = ROOT / "src" / "data" / "corpora" / "study.json"

    def setUp(self):
        self.config = preflight.ep.load_config(REHEARSAL)

    def findings(self, config=None):
        report = preflight.Report()
        preflight.check_reviews(report, config or self.config, PAYLOAD)
        return [(status, check, detail) for status, check, detail in report.findings], report

    def test_the_study_corpus_depends_on_both_review_records(self):
        records = [r.relative_to(ROOT).as_posix() for r, _ in preflight.review_records(self.STUDY_CORPUS)]
        self.assertEqual(records, ["src/data/review/study_review.json", "src/data/simulated/review.json"])

    def shown_with_one_unreviewed(self, config=None):
        """check_reviews findings with a review record in which every stimulus shown is reviewed except one."""
        shown = sorted({s["stimulus"] for row in PAYLOAD["assignments"] for s in preflight.ep.scenario_stages(
            preflight.ep.plan_for(self.config, row["participant"], PAYLOAD), practice=True)})
        record = {"stimuli": {folder: {"status": "reviewed"} for folder in shown}}
        record["stimuli"][shown[0]]["status"] = "unreviewed"
        with mock.patch.object(preflight, "review_records", return_value=[(ROOT / "x.json", self.STUDY_CORPUS)]), \
                mock.patch.object(preflight.rv, "read_record", return_value=record), \
                mock.patch.object(preflight.rv, "review_corpus", side_effect=lambda saved: saved):
            return self.findings(config)

    def test_committed_records_are_current(self):
        found, report = self.findings()
        self.assertFalse(report.failed, found)
        self.assertEqual([f[0] for f in found if f[1] == "review records"], [preflight.PASS] * 2)

    def test_unreviewed_stimuli_only_warn_in_a_rehearsal(self):
        found, report = self.shown_with_one_unreviewed()
        self.assertFalse(report.failed, found)
        self.assertEqual([f[0] for f in found if f[1] == "manual review"], [preflight.WARN])

    def test_unreviewed_stimuli_fail_for_the_study_configuration(self):
        found, report = self.shown_with_one_unreviewed({**self.config, "simulated": False})
        self.assertTrue(report.failed)
        self.assertEqual([f[0] for f in found if f[1] == "manual review"], [preflight.FAIL])

    def test_a_stale_or_missing_review_record_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "review.json"
            record = json.loads((ROOT / "src/data/simulated/review.json").read_text())
            entry = next(iter(record["stimuli"].values()))
            entry["status"] = "reviewed"  # claimed without a manual review
            entry["manual"] = preflight.rv.empty_manual()
            stale.write_text(json.dumps(record))
            corpus = ROOT / "src" / "data" / "corpora" / "simulated.json"
            for records in ([(stale, corpus)], [(Path(tmp) / "absent.json", corpus)]):
                with mock.patch.object(preflight, "review_records", return_value=records):
                    found, report = self.findings()
                self.assertTrue(report.failed, records)
                self.assertEqual(found[0][:2], (preflight.FAIL, "review records"))

    def test_all_stimuli_reviewed_passes(self):
        statuses = {s["stimulus"] for row in PAYLOAD["assignments"] for s in preflight.ep.scenario_stages(
            preflight.ep.plan_for(self.config, row["participant"], PAYLOAD), practice=True)}
        record = {"stimuli": {folder: {"status": "reviewed"} for folder in statuses}}
        with mock.patch.object(preflight, "review_records", return_value=[(ROOT / "x.json", self.STUDY_CORPUS)]), \
                mock.patch.object(preflight.rv, "read_record", return_value=record), \
                mock.patch.object(preflight.rv, "review_corpus", side_effect=lambda saved: saved):
            found, report = self.findings({**self.config, "simulated": False})
        self.assertFalse(report.failed, found)
        self.assertEqual([f[0] for f in found if f[1] == "manual review"], [preflight.PASS])


if __name__ == "__main__":
    unittest.main()
