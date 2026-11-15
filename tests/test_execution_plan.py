"""Tests for build_execution_plan.py: skills block, run ids, conditions, budget, determinism."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import build_execution_plan as bp  # noqa: E402

TAXONOMY = json.loads(bp.TAXONOMY.read_text(encoding="utf-8"))


class SystemMessageTest(unittest.TestCase):
    def test_no_skills_leaves_the_default_message_unchanged(self):
        self.assertEqual(bp.system_message("DEFAULT\n", []), "DEFAULT\n")

    def test_skills_are_appended_in_order_with_their_names(self):
        message = bp.system_message("DEFAULT\n", [("a", "text a\n"), ("b", "text b")])
        self.assertTrue(message.startswith("DEFAULT\n\n" + bp.SKILLS_PREAMBLE))
        self.assertLess(message.index('<skill name="a">'), message.index('<skill name="b">'))
        self.assertIn('<skill name="a">\ntext a\n</skill>', message)


class PlanTest(unittest.TestCase):
    def plan(self, **kwargs):
        args = dict(scenarios=["user_task_1"], conditions=["no-skills", "mapped-skills"],
                    models=["gpt-4o-2024-08-06"], repetitions=1, max_runs=2, taxonomy=TAXONOMY)
        args.update(kwargs)
        return bp.build_plan(**args)

    def test_pilot_plan_has_two_runs_and_the_mapped_skill(self):
        plan = self.plan()
        self.assertEqual([r["run_id"] for r in plan["runs"]],
                         ["user_task_1__no-skills__gpt-4o-2024-08-06__r1",
                          "user_task_1__mapped-skills__gpt-4o-2024-08-06__r1"])
        self.assertEqual(plan["runs"][0]["skills"], [])
        self.assertEqual([s["id"] for s in plan["runs"][1]["skills"]], ["calendar-lookup"])
        self.assertEqual(len(plan["runs"][1]["skills"][0]["sha256"]), 64)
        self.assertEqual(plan["limits"]["max_runs"], 2)

    def test_task_prompt_comes_from_the_recorded_run(self):
        self.assertIn("May 15th", self.plan()["runs"][0]["task_prompt"])

    def test_budget_is_enforced(self):
        with self.assertRaisesRegex(ValueError, "budget allows 2"):
            self.plan(repetitions=2)
        with self.assertRaisesRegex(ValueError, "budget allows 2"):
            self.plan(scenarios=["user_task_1", "user_task_3"])

    def test_larger_plans_grow_by_scenario_model_and_repetition(self):
        plan = self.plan(scenarios=["user_task_1", "user_task_13"], models=["m1", "m2"], repetitions=3, max_runs=100)
        self.assertEqual(len(plan["runs"]), 2 * 2 * 3 * 2)
        self.assertEqual(len({r["run_id"] for r in plan["runs"]}), len(plan["runs"]))
        multi = [r for r in plan["runs"] if r["scenario"] == "user_task_13" and r["condition"] == "mapped-skills"][0]
        self.assertEqual([s["id"] for s in multi["skills"]], ["email-lookup", "document-content", "email-correspondence"])

    def test_unknown_inputs_are_errors(self):
        with self.assertRaises(ValueError):
            self.plan(conditions=["oracle"])
        with self.assertRaises(ValueError):
            self.plan(scenarios=["user_task_999"])
        with self.assertRaises(ValueError):
            self.plan(repetitions=0)

    def test_missing_skill_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(FileNotFoundError):
                self.plan(skills_dir=Path(empty))

    def test_plan_is_deterministic_and_written_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.json", Path(tmp) / "b.json"
            bp.write_plan(self.plan(), first)
            bp.write_plan(self.plan(), second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(json.loads(first.read_text())["plan_version"], bp.PLAN_VERSION)

    def test_committed_plan_matches_its_skill_files(self):
        plan = json.loads(bp.DEFAULT_PLAN.read_text(encoding="utf-8"))
        for run in plan["runs"]:
            for entry in run["skills"]:
                import hashlib
                self.assertEqual(hashlib.sha256((bp.runs.REPO_ROOT / entry["file"]).read_bytes()).hexdigest(),
                                 entry["sha256"])


if __name__ == "__main__":
    unittest.main()
