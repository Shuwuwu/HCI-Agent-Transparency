"""Tests for corpus.py and the corpus-driven eligibility of participant_server.py: configuration validation,
each rule condition, the optional include list, the isolation of simulated records from the study corpus's main
folder, and the separate simulated_source folder from which the study corpus admits a constructed stimulus only after
its manual review (reviewed_in)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import corpus  # noqa: E402
import participant_server as ps  # noqa: E402

STUDY = ROOT / "src" / "data" / "corpora" / "study.json"
VALIDATION = ROOT / "src" / "data" / "corpora" / "validation.json"
SIM_ITEM = "user_task_1__g3_ii_w1"  # a simulated unsafe stimulus (src/data/simulated/)


def write_config(tmp, **changes):
    config = json.loads(STUDY.read_text())
    config.update(changes)
    path = Path(tmp) / "corpus.json"
    path.write_text(json.dumps(config))
    return path


class CorpusFileTests(unittest.TestCase):
    def test_both_corpora_load_with_resolved_paths(self):
        for path, name in ((STUDY, "study"), (VALIDATION, "validation")):
            c = corpus.load_corpus(path)
            self.assertEqual(c["name"], name)
            self.assertTrue(c["stimuli_path"].is_dir(), c["stimuli_path"])
            self.assertTrue(c["workspace_path"].is_dir(), c["workspace_path"])
            self.assertTrue(c["review_path"].is_absolute())
            self.assertTrue(c["id_regex"].match(c["default_stimulus"]))

    def test_corpora_are_disjoint_folders(self):
        study, validation = corpus.load_corpus(STUDY), corpus.load_corpus(VALIDATION)
        self.assertNotEqual(study["stimuli_path"], validation["stimuli_path"])
        self.assertNotEqual(study["review_path"], validation["review_path"])
        self.assertFalse(str(validation["stimuli_path"]).startswith(str(study["stimuli_path"]) + "/"))

    def test_study_admits_a_simulated_record_only_after_review_and_validation_only_simulated_ones(self):
        study = corpus.load_corpus(STUDY)
        for rule in study["rules"]:
            if rule["simulated"] is not False:
                self.assertEqual(rule["reviewed_in"], "src/data/simulated/review.json", rule["id"])
        self.assertEqual(study["simulated_stimuli_path"], ROOT / "src" / "data" / "simulated" / "stimuli")
        self.assertEqual(study["simulated_workspace_path"], ROOT / "src" / "data" / "simulated" / "workspace")
        self.assertTrue(all(r["simulated"] is True for r in corpus.load_corpus(VALIDATION)["rules"]))
        self.assertIsNone(corpus.load_corpus(VALIDATION)["simulated_stimuli_path"])

    def test_invalid_simulated_source_is_rejected(self):
        config = json.loads(STUDY.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            for broken in ({"stimuli_dir": "x"}, {"stimuli_dir": config["stimuli_dir"], "workspace_dir": "y"}, "x"):
                (Path(tmp) / "c.json").write_text(json.dumps({**config, "simulated_source": broken}))
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_corpus(Path(tmp) / "c.json")

    def test_reviewed_in_requires_status_reviewed_for_the_records_item(self):
        record = {"injection": {"item_id": SIM_ITEM}}
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            rule = {"id": "r", "reviewed_in": str(review)}
            self.assertFalse(corpus.rule_matches(rule, record))  # no review file
            for status, expected in (("reviewed", True), ("unreviewed", False), ("stale", False), ("rejected", False)):
                review.write_text(json.dumps({"stimuli": {SIM_ITEM: {"status": status}}}))
                self.assertIs(corpus.rule_matches(rule, record), expected, status)
            self.assertFalse(corpus.rule_matches(rule, {}))  # a record without an item id

    def test_invalid_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(STUDY.read_text())
            for broken in ({"rules": []}, {"rules": [{"id": "x", "bogus": 1}]}, {"rules": [{"run_id_suffix": "x"}]}):
                (Path(tmp) / "c.json").write_text(json.dumps({**config, **broken}))
                with self.assertRaises(corpus.CorpusError):
                    corpus.load_corpus(Path(tmp) / "c.json")
            del config["stimuli_dir"]
            (Path(tmp) / "c.json").write_text(json.dumps(config))
            with self.assertRaises(corpus.CorpusError):
                corpus.load_corpus(Path(tmp) / "c.json")

    def test_generic_id_pattern_accepts_any_safe_name_and_rejects_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = json.loads(STUDY.read_text())
            del config["id_pattern"]
            (Path(tmp) / "c.json").write_text(json.dumps(config))
            rx = corpus.load_corpus(Path(tmp) / "c.json")["id_regex"]
        for ok in ("practice_1", "A-b_c", "scenario07"):
            self.assertTrue(rx.match(ok), ok)
        for bad in ("", "..", "../x", "a/b", ".hidden", "a b"):
            self.assertFalse(rx.match(bad), bad)


class RuleTests(unittest.TestCase):
    safe = {"run_id": "t__x__skills-v4b", "condition": "mapped-skills", "status": "completed", "utility": True}

    def test_each_condition_is_checked_only_when_the_rule_states_it(self):
        rule = {"id": "r", "run_id_suffix": "__skills-v4b", "condition": "mapped-skills", "status": "completed",
                "utility": True, "simulated": False}
        self.assertTrue(corpus.rule_matches(rule, self.safe))
        for change in ({"run_id": "t__x__other"}, {"condition": "no-skills"}, {"status": "error"},
                       {"utility": False}, {"simulated": True}):
            self.assertFalse(corpus.rule_matches(rule, {**self.safe, **change}), change)
        self.assertTrue(corpus.rule_matches({"id": "r"}, {}))  # no conditions: everything matches

    def test_injected_run_needs_an_included_evaluation(self):
        record = {"run_id": "t__g1_ii_w1__injected-skills__m__r1__inj-v1", "condition": "injected-skills",
                  "status": "completed", "utility": False}  # strict utility is false after any successful attack
        with tempfile.TemporaryDirectory() as tmp:
            rule = {"id": "i", "run_id_suffix": "__inj-v1", "evaluation_file": "ev.json"}
            self.assertFalse(corpus.rule_matches(rule, record, tmp))  # no evaluation file yet
            for included, expected in ((False, False), (True, True)):
                (Path(tmp) / "ev.json").write_text(json.dumps(
                    {"evaluations": [{"run_id": record["run_id"], "included": included}]}))
                self.assertIs(corpus.rule_matches(rule, record, tmp), expected)
            (Path(tmp) / "ev.json").write_text(json.dumps({"evaluations": [{"run_id": "other", "included": True}]}))
            self.assertFalse(corpus.rule_matches(rule, record, tmp))

    def test_a_record_without_the_simulated_flag_counts_as_real(self):
        self.assertTrue(corpus.rule_matches({"id": "r", "simulated": False}, {}))
        self.assertFalse(corpus.rule_matches({"id": "r", "simulated": True}, {}))


class ServerCorpusTests(unittest.TestCase):
    def tearDown(self):
        ps.use_corpus(STUDY)

    def test_switching_corpus_changes_what_is_served_and_back(self):
        ps.use_corpus(STUDY)
        self.assertEqual(len(ps.list_stimulus_ids()), 20)
        self.assertEqual(ps.DEFAULT_STIMULUS, "user_task_1__no_injection")
        ps.use_corpus(VALIDATION)
        self.assertEqual(ps.list_stimulus_ids(), [ps.DEFAULT_STIMULUS])
        self.assertFalse(ps.is_eligible("user_task_1__no_injection"))
        ps.use_corpus(STUDY)
        self.assertTrue(ps.is_eligible("user_task_1__no_injection"))

    def test_include_list_restricts_listing_and_direct_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            ps.use_corpus(write_config(tmp, include=["user_task_1__no_injection", "user_task_2__no_injection"]))
            self.assertEqual(sorted(ps.list_stimulus_ids()), ["user_task_1__no_injection", "user_task_2__no_injection"])
            with self.assertRaises(FileNotFoundError):
                ps.participant_view("user_task_4__no_injection", "low")

    def test_study_corpus_refuses_the_simulated_record_even_inside_the_study_folder(self):
        v = corpus.load_corpus(VALIDATION)
        fixture = v["default_stimulus"]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(v["stimuli_path"] / fixture, Path(tmp) / fixture)  # same provenance, same record
            ps.use_corpus(VALIDATION)
            self.assertTrue(ps.is_eligible(fixture, Path(tmp)))  # sanity: the validation corpus accepts it
            ps.use_corpus(STUDY)
            self.assertFalse(ps.is_eligible(fixture, Path(tmp)))
            for cond in ps.CONDITIONS:
                with self.assertRaises(FileNotFoundError):
                    ps.participant_view(fixture, cond, Path(tmp))

    def test_study_corpus_serves_a_simulated_stimulus_only_once_reviewed_and_from_its_own_folder(self):
        sim_dir = corpus.load_corpus(STUDY)["simulated_stimuli_path"]
        with tempfile.TemporaryDirectory() as tmp:
            review = Path(tmp) / "review.json"
            config = json.loads(STUDY.read_text())
            config["rules"][-1]["reviewed_in"] = str(review)  # the simulated rule, pointed at a temporary review
            path = Path(tmp) / "corpus.json"
            path.write_text(json.dumps(config))
            ps.use_corpus(path)
            self.assertFalse(ps.is_eligible(SIM_ITEM))  # not reviewed: refused
            self.assertEqual(len(ps.list_stimulus_ids()), 20)
            review.write_text(json.dumps({"stimuli": {SIM_ITEM: {"status": "reviewed"}}}))
            ps.use_corpus(path)  # eligibility is cached per corpus load
            self.assertTrue(ps.is_eligible(SIM_ITEM))
            self.assertEqual(ps.stimulus_source(SIM_ITEM), "simulated")
            self.assertIn(SIM_ITEM, ps.list_stimulus_ids())
            high = json.loads((sim_dir / SIM_ITEM / "high.json").read_text())
            self.assertEqual(ps.participant_view(SIM_ITEM, "high")["user_request"], high["user_request"])
            self.assertEqual(len(ps.participant_view(SIM_ITEM, "high")["trace"]), len(high["action_log"]))
            self.assertIn("state", ps.participant_workspace(SIM_ITEM))
            self.assertEqual([s["id"] for s in ps.participant_skills(SIM_ITEM)], ["calendar-lookup"])
            import review_stimuli as rv  # the study review never reviews it a second time
            self.assertNotIn(SIM_ITEM, rv.review_corpus()["stimuli"])

    def test_a_genuine_stimulus_in_the_simulated_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty, sim = Path(tmp) / "main", Path(tmp) / "sim"
            empty.mkdir()
            shutil.copytree(corpus.load_corpus(STUDY)["stimuli_path"] / "user_task_1__no_injection",
                            sim / "user_task_1__no_injection")
            ps.use_corpus(write_config(tmp, stimuli_dir=str(empty), simulated_source={
                "stimuli_dir": str(sim), "workspace_dir": str(Path(tmp) / "ws")}))
            self.assertFalse(ps.is_eligible("user_task_1__no_injection"))

    def test_a_name_in_both_folders_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = Path(tmp) / "sim"
            shutil.copytree(corpus.load_corpus(STUDY)["stimuli_path"] / "user_task_1__no_injection",
                            sim / "user_task_1__no_injection")
            ps.use_corpus(write_config(tmp, simulated_source={"stimuli_dir": str(sim),
                                                              "workspace_dir": str(Path(tmp) / "ws")}))
            self.assertFalse(ps.is_eligible("user_task_1__no_injection"))
            self.assertTrue(ps.is_eligible("user_task_2__no_injection"))

    def test_study_stimuli_folder_never_holds_a_simulated_stimulus(self):
        for folder in corpus.load_corpus(STUDY)["stimuli_path"].iterdir():
            prov = json.loads((folder / "provenance.json").read_text())
            self.assertFalse(prov["source"].get("simulated"), folder.name)
            self.assertNotIn("validation", prov["source"]["file"], folder.name)


if __name__ == "__main__":
    unittest.main()
