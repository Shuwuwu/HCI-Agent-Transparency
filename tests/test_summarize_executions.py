"""Tests for summarize_executions.py on synthetic run records."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import summarize_executions as se  # noqa: E402


def call(name, **args):
    return {"role": "assistant", "content": None, "tool_calls": [{"function": name, "args": args, "id": "x"}]}


def answer(text):
    return {"role": "assistant", "content": text, "tool_calls": None}


def record(condition, trace, utility=True, status="completed"):
    return {"run_id": f"user_task_1__{condition}__m__r1", "scenario": "user_task_1", "condition": condition,
            "model": "m", "repetition": 1, "status": status, "error": None, "utility": utility,
            "skills": [] if condition == "no-skills" else [{"id": "calendar-lookup"}], "trace": trace,
            "usage": {"calls": 2, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "reported_models": ["m"], "duration_seconds": 1.0, "system_message_sha256": "h"}


class SummaryTest(unittest.TestCase):
    def write(self, records):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for r in records:
            (Path(tmp.name) / f"{r['run_id']}.json").write_text(json.dumps(r))
        return Path(tmp.name)

    def test_pair_with_same_calls_as_the_recorded_run(self):
        trace = [call("get_day_calendar_events", day="2024-05-15"), answer("Two.")]
        result = se.summarize(self.write([record("no-skills", trace), record("mapped-skills", trace)]))
        comparison = result["comparisons"][0]
        self.assertTrue(comparison["same_tool_sequence"])
        self.assertEqual(comparison["recorded_tool_names"], ["get_day_calendar_events"])
        self.assertEqual(comparison["equals_recorded_tool_names"], {"no-skills": True, "mapped-skills": True})
        self.assertEqual(result["by_condition"]["no-skills"], {"runs": 1, "completed": 1, "utility_true": 1})

    def test_differences_and_data_changing_calls_are_reported(self):
        none = [call("get_day_calendar_events", day="2024-05-15"), answer("Two.")]
        skilled = [call("get_current_day"), call("get_day_calendar_events", day="2024-05-15"),
                   call("send_email", recipients=["a@b.c"]), answer("Two.")]
        result = se.summarize(self.write([record("no-skills", none), record("mapped-skills", skilled, utility=False)]))
        c = result["comparisons"][0]
        self.assertFalse(c["same_tool_sequence"])
        self.assertEqual(c["only_with_skills"], ["get_current_day", "send_email"])
        self.assertEqual(c["data_changing_calls"]["mapped-skills"], ["send_email"])
        self.assertEqual(c["utility"], {"no-skills": True, "mapped-skills": False})

    def test_run_without_a_final_answer_has_none(self):
        self.assertIsNone(se.final_answer([call("get_current_day")]))
        self.assertEqual(se.final_answer([answer("done")]), "done")

    def test_unpaired_runs_are_summarised_but_not_compared(self):
        result = se.summarize(self.write([record("no-skills", [answer("x")])]))
        self.assertEqual((len(result["runs"]), result["comparisons"]), (1, []))


if __name__ == "__main__":
    unittest.main()
