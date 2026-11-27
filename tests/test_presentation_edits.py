"""Tests of the researcher-decided presentation edits (src/data/presentation_edits.json, presentation_edits.py):
task 29's two proposed activity dates are served as December 2024 in its safe and constructed unsafe stimuli, in
both conditions, while the stimulus files and execution records keep the recorded December 2023 dates, and no other
scenario is edited."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import participant_server as ps  # noqa: E402
import presentation_edits as pe  # noqa: E402

STUDY = ROOT / "src" / "data" / "corpora" / "study.json"
SIMULATED = ROOT / "src" / "data" / "corpora" / "simulated.json"
SAFE, UNSAFE = "user_task_29__no_injection", "user_task_29__g4_ii_w1"
RECORDED = ("**Date**: 2023-12-10", "**Date**: 2023-12-18")
SHOWN = ("**Date**: 2024-12-10", "**Date**: 2024-12-18")


class EditsFileTest(unittest.TestCase):
    def test_only_task_29s_two_stimuli_are_edited_and_only_their_two_dates(self):
        edits = pe.load()
        self.assertEqual(set(edits), {SAFE, UNSAFE})
        for folder in (SAFE, UNSAFE):
            self.assertEqual([(e["field"], e["old"], e["new"], e["occurrences"]) for e in edits[folder]],
                             [("final_response", old, new, 1) for old, new in zip(RECORDED, SHOWN)])

    def test_an_edit_that_does_not_match_its_count_is_refused(self):
        with self.assertRaises(ValueError):
            pe.apply(SAFE, {"final_response": "no dates here"})
        with self.assertRaises(ValueError):
            pe.apply(SAFE, {"final_response": RECORDED[0] * 2 + RECORDED[1]})

    def test_a_malformed_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edits.json"
            path.write_text(json.dumps({"stimuli": {SAFE: {"decided": "2026-11-27", "reason": "x", "edits": [
                {"field": "trace", "old": "a", "new": "b", "occurrences": 1}]}}}))
            with self.assertRaises(ValueError):
                pe.load(path)


class ServedDatesTest(unittest.TestCase):
    def tearDown(self):
        ps.use_corpus(STUDY)

    def check_served(self, folder):
        stim_dir = ps.stimulus_dir(folder) / folder
        for condition in ps.CONDITIONS:
            on_disk = json.loads((stim_dir / f"{condition}.json").read_text())["final_response"]
            served = ps.participant_view(folder, condition)["final_response"]
            for old, new in zip(RECORDED, SHOWN):
                self.assertEqual(on_disk.count(old), 1)  # the file keeps the recorded date
                self.assertNotIn(old, served)
                self.assertEqual(served.count(new), 1)
            self.assertEqual(served, on_disk.replace(RECORDED[0], SHOWN[0]).replace(RECORDED[1], SHOWN[1]))
        self.assertEqual(ps.participant_view(folder, "low")["final_response"],
                         ps.participant_view(folder, "high")["final_response"])

    def test_safe_stimulus_is_served_with_december_2024(self):
        ps.use_corpus(STUDY)
        self.check_served(SAFE)

    def test_constructed_unsafe_stimulus_is_served_with_december_2024(self):
        ps.use_corpus(SIMULATED)
        self.check_served(UNSAFE)

    def test_execution_records_keep_the_recorded_dates(self):
        for folder in (SAFE, UNSAFE):
            ps.use_corpus(STUDY if folder == SAFE else SIMULATED)
            prov = json.loads((ps.stimulus_dir(folder) / folder / "provenance.json").read_text())
            record = (ROOT / prov["source"]["file"]).read_text()
            for old in RECORDED:
                self.assertIn(old, record)
            for new in SHOWN:
                self.assertNotIn(new, record)

    def test_other_stimuli_are_served_as_built(self):
        for corpus in (STUDY, SIMULATED):
            ps.use_corpus(corpus)
            for folder in ps.list_stimulus_ids():
                if folder in (SAFE, UNSAFE):
                    continue
                for condition in ps.CONDITIONS:
                    data = json.loads((ps.stimulus_dir(folder) / folder / f"{condition}.json").read_text())
                    view = ps.participant_view(folder, condition)
                    self.assertEqual((view["user_request"], view["final_response"]),
                                     (data["user_request"], data["final_response"]), folder)

    def test_the_startup_check_passes_for_the_shipped_corpora(self):
        for corpus in (STUDY, SIMULATED):
            ps.use_corpus(corpus)
            ps.check_presentation_edits()


if __name__ == "__main__":
    unittest.main()
