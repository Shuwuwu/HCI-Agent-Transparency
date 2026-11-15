"""Tests for run_executions.py with a fake AgentDojo adapter: budget, no retries, records, key redaction, access check."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import build_execution_plan as bp  # noqa: E402
import generate_skills as gs  # noqa: E402
import run_executions as rx  # noqa: E402

KEY = "sk-test-secret-key-123456"
TAXONOMY = json.loads(bp.TAXONOMY.read_text(encoding="utf-8"))
TRACE = [{"role": "assistant", "content": None, "tool_calls": [{"function": "get_day_calendar_events",
                                                                 "args": {"day": "2024-05-15"}, "id": "c1"}]},
         {"role": "assistant", "content": "You have 2 appointments.", "tool_calls": None}]


class FakeAdapter:
    def __init__(self, fail=False):
        self.calls, self.fail, self.system_messages = [], fail, []

    def default_system_message(self):
        return "DEFAULT\n"

    def run(self, spec, system_message, max_llm_calls):
        self.calls.append(spec["run_id"])
        self.system_messages.append(system_message)
        if self.fail:
            raise RuntimeError(f"boom with key {KEY}")
        return {"messages": TRACE, "utility": True, "duration": 1.5, "error": None,
                "llm_calls": [{"requested_model": spec["model"], "reported_model": spec["model"],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}] * 2}


def plan(max_runs=2, repetitions=1):
    return bp.build_plan(["user_task_1"], ["no-skills", "mapped-skills"], ["gpt-4o-2024-08-06"], repetitions,
                         max_runs, TAXONOMY)


class ExecutePlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_plan(self, adapter=None, **kwargs):
        adapter = adapter or FakeAdapter()
        args = dict(plan=plan(), adapter=adapter, runs_dir=self.dir, max_runs=2, max_llm_calls=20, api_key=KEY,
                    log=lambda *_: None)
        args.update(kwargs)
        return adapter, rx.execute_plan(**args)

    def test_two_runs_are_recorded_with_their_system_messages(self):
        adapter, records = self.run_plan()
        self.assertEqual([r["condition"] for r in records], ["no-skills", "mapped-skills"])
        self.assertEqual(adapter.system_messages[0], "DEFAULT\n")
        self.assertIn('<skill name="calendar-lookup">', adapter.system_messages[1])
        record = json.loads((self.dir / f"{records[1]['run_id']}.json").read_text())
        self.assertEqual(record["status"], "completed")
        self.assertTrue(record["utility"])
        self.assertEqual(record["usage"], {"calls": 2, "prompt_tokens": 200, "completion_tokens": 20,
                                           "total_tokens": 220})
        self.assertEqual(record["reported_models"], ["gpt-4o-2024-08-06"])
        self.assertEqual(record["system_message_sha256"], rx.sha256_text(record["system_message"]))
        self.assertEqual(record["trace"], TRACE)

    def test_finished_runs_are_not_repeated(self):
        self.run_plan()
        adapter, records = self.run_plan()
        self.assertEqual((adapter.calls, records), ([], []))

    def test_failed_run_is_recorded_redacted_and_not_retried(self):
        adapter, records = self.run_plan(FakeAdapter(fail=True))
        self.assertEqual([r["status"] for r in records], ["error", "error"])
        self.assertNotIn(KEY, json.dumps(records))
        self.assertIn("[API KEY]", records[0]["error"])
        adapter2, again = self.run_plan(FakeAdapter())
        self.assertEqual(adapter2.calls, [])  # not repeated without --force

    def test_force_repeats_a_run(self):
        self.run_plan()
        adapter, records = self.run_plan(only=["user_task_1__no-skills__gpt-4o-2024-08-06__r1"], force=True)
        self.assertEqual(len(records), 1)

    def test_budget_stops_before_anything_runs(self):
        adapter = FakeAdapter()
        with self.assertRaisesRegex(ValueError, "pending but --max-runs is 1"):
            self.run_plan(adapter, max_runs=1)
        self.assertEqual(adapter.calls, [])
        too_big = plan(max_runs=2)
        too_big["limits"]["max_runs"] = 1
        with self.assertRaisesRegex(ValueError, "its own limit"):
            rx.execute_plan(too_big, adapter, self.dir, 5, 20)
        self.assertEqual(adapter.calls, [])

    def test_unknown_run_id_is_an_error(self):
        with self.assertRaises(ValueError):
            self.run_plan(only=["nope"])

    def test_changed_skill_file_is_refused(self):
        spec = plan()["runs"][1]
        spec["skills"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "changed since the plan"):
            rx.build_system_message(spec, "DEFAULT\n")

    def test_existing_record_is_never_overwritten_by_execute_run(self):
        spec = plan()["runs"][0]
        rx.execute_run(spec, FakeAdapter(), self.dir, 20)
        with self.assertRaises(FileExistsError):
            rx.execute_run(spec, FakeAdapter(), self.dir, 20)


class ModelAccessTest(unittest.TestCase):
    def test_accessible_and_deprecated_models(self):
        ok = rx.check_model_access("m", KEY, post=lambda *a, **k: {"model": "m-2024"})
        self.assertEqual((ok["accessible"], ok["reported_model"]), (True, "m-2024"))

        def refuse(*args, **kwargs):
            raise gs.GenerationError(f"HTTP 404: model deprecated {KEY}")
        bad = rx.check_model_access("m", KEY, post=refuse)
        self.assertFalse(bad["accessible"])
        self.assertNotIn(KEY, bad["error"])

    def test_access_check_never_retries(self):
        seen = {}
        rx.check_model_access("m", KEY, post=lambda url, body, key, retries: seen.update(retries=retries) or {})
        self.assertEqual(seen["retries"], 0)


VENV_PYTHON = Path(__file__).resolve().parents[1] / ".venv-agentdojo" / "bin" / "python"


@unittest.skipUnless(VENV_PYTHON.exists(), "the AgentDojo environment is not installed (setup_execution_env.py)")
class HarnessTest(unittest.TestCase):
    def test_ground_truth_of_a_scenario_passes_through_the_real_harness(self):
        import subprocess
        script = Path(rx.__file__)
        done = subprocess.run([str(VENV_PYTHON), "-B", str(script), "--ground-truth-check", "user_task_1"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("utility True", done.stdout)

    def test_default_system_message_equals_the_recorded_runs(self):
        import subprocess
        code = ("import sys,json;sys.path.insert(0,%r);import run_executions as r;"
                "print(json.dumps(r.AgentDojoAdapter().default_system_message()))" % str(Path(rx.__file__).parent))
        done = subprocess.run([str(VENV_PYTHON), "-B", "-c", code], capture_output=True, text=True, timeout=120)
        recorded = rx.runs.load_run("user_task_1")["messages"][0]["content"]
        self.assertEqual(json.loads(done.stdout.strip().splitlines()[-1]), recorded)


class UsageTest(unittest.TestCase):
    def test_calls_without_usage_count_as_zero_tokens(self):
        self.assertEqual(rx.usage_totals([{"usage": None}, {"usage": {"total_tokens": 5}}])["total_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
