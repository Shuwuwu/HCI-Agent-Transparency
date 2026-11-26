"""Tests of experiment_plan.py: configuration validation, the stage sequence of one participant (order, unique
keys, stable trial ids), configurable trial counts, safe/unsafe stimulus resolution, the simulated/study
separation, and that resolving stimuli does not change which corpus a server would serve."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import experiment_plan as ep  # noqa: E402
import participant_server as ps  # noqa: E402

DEV_CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
DEV_PAYLOAD = ep.load_assignments(DEV_CONFIG)
FIRST = DEV_PAYLOAD["assignments"][0]["participant"]


def write_config(**overrides):
    """Load a copy of the development configuration with `overrides` applied, through a temporary file."""
    raw = json.loads(ep.DEVELOPMENT_CONFIG.read_text(encoding="utf-8"))
    raw.update(overrides)
    folder = tempfile.mkdtemp()
    path = Path(folder) / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return ep.load_config(path)


class ConfigTests(unittest.TestCase):
    def test_both_shipped_configurations_load(self):
        for path in (ep.CONFIG_PATH, ep.DEVELOPMENT_CONFIG):
            config = ep.load_config(path)
            self.assertEqual(config["version"], ep.CONFIG_VERSION)
            self.assertEqual(config["corpus_path"], ROOT / config["corpus"])
            self.assertEqual(config["assignments_path"], ROOT / config["assignments"])
        self.assertIs(ep.load_config(ep.CONFIG_PATH)["simulated"], False)
        self.assertIs(DEV_CONFIG["simulated"], True)

    def test_invalid_configurations_are_refused(self):
        for overrides in ({"version": "experiment_config_v0"}, {"trials_per_participant": 0},
                          {"trials_per_participant": True}, {"trials_per_participant": "8"},
                          {"simulated": "yes"}, {"practice": {}}, {"practice": {"stimulus": ""}},
                          {"practice": {"stimulus": "user_task_13__no_injection", "omit_steps": [5]}},  # no reason
                          {"practice": {"stimulus": "user_task_13__no_injection", "omit_steps": [5, 5],
                                        "omit_reason": "x"}},
                          {"practice": {"stimulus": "user_task_13__no_injection", "omit_steps": [0],
                                        "omit_reason": "x"}},
                          {"practice": {"stimulus": "user_task_13__no_injection", "omit_steps": ["5"],
                                        "omit_reason": "x"}},
                          {"safe_stimulus_suffix": ""}):
            with self.subTest(overrides=overrides), self.assertRaises(ep.ConfigError):
                write_config(**overrides)

    def test_missing_key_is_refused(self):
        raw = json.loads(ep.DEVELOPMENT_CONFIG.read_text(encoding="utf-8"))
        for key in ep.REQUIRED_KEYS:
            short = {k: v for k, v in raw.items() if k != key}
            path = Path(tempfile.mkdtemp()) / "config.json"
            path.write_text(json.dumps(short), encoding="utf-8")
            with self.subTest(key=key), self.assertRaises(ep.ConfigError):
                ep.load_config(path)


class SimulatedSeparationTests(unittest.TestCase):
    def test_simulated_assignments_need_a_simulated_configuration(self):
        config = write_config(simulated=False)
        with self.assertRaises(ep.ConfigError):
            ep.load_assignments(config)

    def test_study_assignments_are_refused_by_a_simulated_configuration(self):
        payload = copy.deepcopy(DEV_PAYLOAD)
        payload["simulated"] = False
        path = Path(tempfile.mkdtemp()) / "assignments.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        config = ep.load_config(ep.DEVELOPMENT_CONFIG)
        config["assignments_path"] = path
        with self.assertRaises(ep.ConfigError):
            ep.load_assignments(config)

    def test_plan_reports_the_simulated_flag_of_the_assignments(self):
        self.assertIs(ep.plan_for(DEV_CONFIG, FIRST, DEV_PAYLOAD)["simulated"], True)


class StimulusResolutionTests(unittest.TestCase):
    def test_safe_trial_uses_the_scenario_suffix_and_unsafe_the_item_folder(self):
        safe = {"trial": 1, "scenario": "user_task_9", "version": "safe", "item_id": None}
        unsafe = {"trial": 2, "scenario": "user_task_9", "version": "unsafe", "item_id": "user_task_9__g2_ii_wg"}
        self.assertEqual(ep.stimulus_folder(safe, "__no_injection"), "user_task_9__no_injection")
        self.assertEqual(ep.stimulus_folder(unsafe, "__no_injection"), "user_task_9__g2_ii_wg")

    def test_unsafe_trial_without_an_item_is_refused(self):
        with self.assertRaises(ep.ConfigError):
            ep.stimulus_folder({"trial": 2, "scenario": "user_task_9", "version": "unsafe", "item_id": None},
                               "__no_injection")

    def test_every_safe_stimulus_of_the_development_plan_is_servable(self):
        plan = ep.plan_for(DEV_CONFIG, FIRST, DEV_PAYLOAD)
        unresolved = set(ep.unresolved_stimuli(DEV_CONFIG, plan))
        safe = {s["stimulus"] for s in ep.scenario_stages(plan, practice=True) if s["version"] != "unsafe"}
        self.assertTrue(safe)
        self.assertEqual(safe & unresolved, set())

    def test_resolving_stimuli_restores_the_active_corpus(self):
        before = ps.CORPUS["path"]
        ep.unresolved_stimuli(DEV_CONFIG, ep.plan_for(DEV_CONFIG, FIRST, DEV_PAYLOAD))
        self.assertEqual(ps.CORPUS["path"], before)


class StageSequenceTests(unittest.TestCase):
    def setUp(self):
        self.plan = ep.plan_for(DEV_CONFIG, FIRST, DEV_PAYLOAD)
        self.stages = self.plan["stages"]

    def test_sequence_is_consent_instructions_practice_trials_questionnaire_completion(self):
        keys = [s["key"] for s in self.stages]
        trials = DEV_CONFIG["trials_per_participant"]
        expected = ["consent", "instructions", "practice_scenario", "practice_assessment"]
        for n in range(1, trials + 1):
            expected += [f"trial_{n:02d}_scenario", f"trial_{n:02d}_assessment"]
        expected += ["questionnaire", "completion"]
        self.assertEqual(keys, expected)

    def test_stage_keys_are_unique_and_indexes_are_contiguous(self):
        keys = [s["key"] for s in self.stages]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual([s["index"] for s in self.stages], list(range(len(self.stages))))

    def test_every_stage_kind_is_known(self):
        for stage in self.stages:
            self.assertIn(stage["kind"], ep.STAGE_KINDS)

    def test_trial_ids_are_unique_within_a_participant_and_shared_by_the_stage_pair(self):
        ids = [s["trial_id"] for s in self.stages if "trial_id" in s]
        self.assertEqual(len(ids), 2 * (DEV_CONFIG["trials_per_participant"] + 1))  # practice included
        self.assertEqual(len(set(ids)), DEV_CONFIG["trials_per_participant"] + 1)
        for stage in self.stages:
            if stage["kind"] == "assessment":
                scenario = next(s for s in self.stages if s["index"] == stage["index"] - 1)
                self.assertEqual(scenario["kind"], "scenario")
                self.assertEqual(scenario["trial_id"], stage["trial_id"])

    def test_trial_ids_are_unique_across_participants(self):
        ids = [s["trial_id"] for row in DEV_PAYLOAD["assignments"]
               for s in ep.plan_for(DEV_CONFIG, row["participant"], DEV_PAYLOAD)["stages"] if "trial_id" in s]
        self.assertEqual(len(set(ids)), len(DEV_PAYLOAD["assignments"]) * (DEV_CONFIG["trials_per_participant"] + 1))

    def test_deriving_the_plan_again_gives_the_identical_plan(self):
        self.assertEqual(ep.plan_for(DEV_CONFIG, FIRST, DEV_PAYLOAD), self.plan)

    def test_trial_ids_match_the_interaction_log_token_pattern(self):
        import interaction_log as ilog
        for stage in self.stages:
            if "trial_id" in stage:
                self.assertRegex(stage["trial_id"], ilog.TOKEN_RE)

    def test_practice_comes_first_and_is_marked_and_not_an_experimental_scenario(self):
        practice = [s for s in self.stages if s.get("practice")]
        self.assertEqual([s["key"] for s in practice], ["practice_scenario", "practice_assessment"])
        self.assertTrue(all(s["index"] < 4 for s in practice))
        self.assertNotIn(practice[0]["scenario"], DEV_PAYLOAD["final"]["scenarios"])

    def test_practice_assessment_can_be_switched_off(self):
        config = write_config(practice={"stimulus": "user_task_13__no_injection", "assessment": False})
        keys = [s["key"] for s in ep.plan_for(config, FIRST, DEV_PAYLOAD)["stages"]]
        self.assertIn("practice_scenario", keys)
        self.assertNotIn("practice_assessment", keys)

    def test_scenario_stages_follow_the_assignment_order_and_versions(self):
        row = ep.assignment_for(DEV_PAYLOAD, FIRST)
        stages = ep.scenario_stages(self.plan)
        self.assertEqual([s["scenario"] for s in stages], [t["scenario"] for t in row["trials"]])
        self.assertEqual([s["version"] for s in stages], [t["version"] for t in row["trials"]])

    def test_every_scenario_stage_uses_the_participants_condition(self):
        condition = ep.assignment_for(DEV_PAYLOAD, FIRST)["condition"]
        for stage in ep.scenario_stages(self.plan, practice=True):
            self.assertEqual(stage["condition"], condition)

    def test_trial_count_is_configurable_and_keeps_the_first_trials_in_order(self):
        config = write_config(trials_per_participant=3)
        plan = ep.plan_for(config, FIRST, DEV_PAYLOAD)
        stages = ep.scenario_stages(plan)
        self.assertEqual(len(stages), 3)
        self.assertEqual([s["trial"] for s in stages], [1, 2, 3])
        self.assertEqual([s["scenario"] for s in stages],
                         [t["scenario"] for t in ep.assignment_for(DEV_PAYLOAD, FIRST)["trials"][:3]])
        self.assertEqual(plan["stages"][-1]["key"], "completion")

    def test_more_trials_than_the_assignment_holds_is_refused(self):
        config = write_config(trials_per_participant=99)
        with self.assertRaises(ep.ConfigError):
            ep.plan_for(config, FIRST, DEV_PAYLOAD)

    def test_unknown_participant_raises_key_error(self):
        with self.assertRaises(KeyError):
            ep.plan_for(DEV_CONFIG, "nobody", DEV_PAYLOAD)

    def test_unknown_condition_is_refused(self):
        row = copy.deepcopy(ep.assignment_for(DEV_PAYLOAD, FIRST))
        row["condition"] = "medium"
        with self.assertRaises(ep.ConfigError):
            ep.stage_sequence(DEV_CONFIG, row)


class DesignProblemTests(unittest.TestCase):
    def test_the_development_configuration_has_no_problems(self):
        self.assertEqual(ep.design_problems(DEV_CONFIG, DEV_PAYLOAD), [])

    def test_practice_on_an_experimental_scenario_is_a_problem(self):
        experimental = DEV_PAYLOAD["final"]["scenarios"][0]
        config = write_config(practice={"stimulus": f"{experimental}__no_injection", "assessment": True})
        problems = ep.design_problems(config, DEV_PAYLOAD)
        self.assertTrue(any(experimental in p for p in problems), problems)

    def test_repeated_participant_id_is_a_problem(self):
        payload = copy.deepcopy(DEV_PAYLOAD)
        payload["assignments"].append(copy.deepcopy(payload["assignments"][0]))
        self.assertTrue(any("repeats a participant id" in p for p in ep.design_problems(DEV_CONFIG, payload)))

    def test_a_trial_without_an_item_is_reported_per_participant(self):
        payload = copy.deepcopy(DEV_PAYLOAD)
        for trial in payload["assignments"][0]["trials"]:
            if trial["version"] == "unsafe":
                trial["item_id"] = None
                break
        problems = ep.design_problems(DEV_CONFIG, payload)
        self.assertTrue(any(payload["assignments"][0]["participant"] in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
