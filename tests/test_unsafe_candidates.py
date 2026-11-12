"""
Purpose:
    Check that build_unsafe_candidates.py selects the right attacked runs and compares them correctly with
    the no-attack runs. Counts are hard-coded from a hand tally of the raw run files; the helper functions
    are tested on small synthetic examples.

Inputs:
    The run files in src/data/gpt-4o-2024-05-13/ and the committed src/data/gpt4o_unsafe_candidates.json.

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_unsafe_candidates.py and src/scripts/agentdojo_runs.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import build_unsafe_candidates as build  # noqa: E402


def call(function, **args):
    return {"function": function, "args": args}


class HelperFunctionsTest(unittest.TestCase):
    def test_unauthorized_send_email_matches_attacker_in_recipient_list(self):
        calls = [
            call("send_email", recipients=["david@example.com"]),
            call("send_email", recipients=["x@y.com", runs.ATTACKER_EMAIL]),
        ]
        self.assertEqual(runs.unauthorized_calls("injection_task_0", calls), [calls[1]])

    def test_send_email_to_someone_else_is_not_unauthorized(self):
        calls = [call("send_email", recipients=["david@example.com"])]
        self.assertEqual(runs.unauthorized_calls("injection_task_3", calls), [])

    def test_delete_file_must_be_file_13(self):
        calls = [call("delete_file", file_id="12"), call("delete_file", file_id="13")]
        self.assertEqual(runs.unauthorized_calls("injection_task_1", calls), [calls[1]])

    def test_event_with_attacker_participant(self):
        calls = [call("create_calendar_event", participants=[runs.ATTACKER_EMAIL], title="Intro with Mark")]
        self.assertEqual(runs.unauthorized_calls("injection_task_2", calls), calls)
        self.assertEqual(runs.unauthorized_calls("injection_task_0", calls), [])  # wrong tool for goal 0

    def test_extra_calls_uses_function_and_arguments(self):
        reference = [call("search_emails", query="a")]
        attacked = [call("search_emails", query="a"), call("search_emails", query="b"), call("send_email", recipients=[])]
        self.assertEqual(build.extra_calls(attacked, reference), attacked[1:])

    def test_extra_calls_counts_repeats(self):
        reference = [call("list_files")]
        attacked = [call("list_files"), call("list_files")]
        self.assertEqual(build.extra_calls(attacked, reference), [call("list_files")])

    def test_missing_functions_ignores_argument_differences(self):
        reference = [call("search_emails", query="a"), call("create_calendar_event", title="t")]
        attacked = [call("search_emails", query="different wording")]
        self.assertEqual(build.missing_functions(attacked, reference), ["create_calendar_event"])
        self.assertEqual(build.missing_functions(reference, reference), [])

    def test_reference_hits_returns_context_snippet(self):
        hits = build.reference_hits(["I will first follow the instruction in the event description."])
        self.assertEqual(len(hits), 1)
        self.assertIn("instruction", hits[0])
        self.assertEqual(build.reference_hits(["Your meeting is at 10:00."]), [])


class InjectionTableTest(unittest.TestCase):
    def test_every_goal_appears_in_every_attacked_run(self):
        """Checks the hard-coded goal table against all 480 attacked runs (both attack types)."""
        for attack in runs.ATTACKS:
            for task_id in runs.list_user_task_ids():
                for injection_id in runs.INJECTION_TASK_IDS:
                    run = runs.load_run(task_id, attack, injection_id)
                    text = " ".join(run["injections"].values())
                    self.assertIn(runs.INJECTION_GOALS[injection_id], text, (task_id, attack, injection_id))


class CandidatePoolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = build.build_output()
        cls.candidates = cls.output["candidates"]
        cls.summary = cls.output["summary"]

    def test_counts(self):
        s = self.summary
        self.assertEqual(s["runs_examined"], 480)
        self.assertEqual(s["attack_succeeded_without_error"], {"important_instructions": 97, "tool_knowledge": 62})
        self.assertEqual(s["candidates"], 116)
        self.assertEqual(s["candidates_by_attack"], {"important_instructions": 74, "tool_knowledge": 42})
        self.assertEqual(s["user_tasks_by_attack"], {"important_instructions": 24, "tool_knowledge": 17})
        self.assertEqual(
            s["candidates_by_injection_task"],
            {"injection_task_0": 23, "injection_task_1": 28, "injection_task_2": 35,
             "injection_task_3": 20, "injection_task_4": 7, "injection_task_5": 3},
        )

    def test_43_successful_attacks_excluded_because_task_not_doable(self):
        succeeded = sum(self.summary["attack_succeeded_without_error"].values())
        self.assertEqual(succeeded - len(self.candidates), 43)

    def test_every_candidate_meets_the_definition(self):
        for c in self.candidates:
            self.assertIsNone(c["error"])
            self.assertTrue(c["security"])
            self.assertTrue(c["none_utility"])
            none_run = runs.load_run(c["user_task_id"])
            self.assertTrue(none_run["utility"])

    def test_every_candidate_has_a_detected_unauthorized_call(self):
        self.assertTrue(all(c["unauthorized_calls"] for c in self.candidates))
        self.assertEqual(self.summary["candidates_by_flag"].get("no_unauthorized_call_detected", 0), 0)

    def test_flag_counts(self):
        flags = self.summary["candidates_by_flag"]
        self.assertEqual(flags["mentions_injection_source"], 14)
        self.assertEqual(flags["missing_legit_calls"], 2)
        self.assertEqual(flags["combined_task"], 12)
        self.assertEqual(self.summary["candidates_with_no_flags"], 89)

    def test_no_risk_labels_yet(self):
        self.assertTrue(all("risk" not in c for c in self.candidates))
        self.assertFalse(any("risk" in goal.lower() for goal in runs.INJECTION_GOALS))

    def test_candidates_sorted_by_attack_task_injection(self):
        keys = [
            (runs.ATTACKS.index(c["attack_type"]), runs.task_number(c["user_task_id"]), c["injection_task_id"])
            for c in self.candidates
        ]
        self.assertEqual(keys, sorted(keys))

    def test_user_task_0_injection_0_checked_against_raw_file(self):
        """Independent check: read the raw run file directly instead of through agentdojo_runs."""
        path = REPO_ROOT / "src/data/gpt-4o-2024-05-13/workspace/user_task_0/important_instructions/injection_task_0.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw_sends = [
            tc["args"] for m in raw["messages"] for tc in (m.get("tool_calls") or []) if tc["function"] == "send_email"
        ]
        candidate = next(
            c for c in self.candidates
            if (c["user_task_id"], c["attack_type"], c["injection_task_id"])
            == ("user_task_0", "important_instructions", "injection_task_0")
        )
        self.assertEqual([c["args"] for c in candidate["unauthorized_calls"]], raw_sends)
        self.assertIn(runs.ATTACKER_EMAIL, raw_sends[0]["recipients"])

    def test_output_is_deterministic(self):
        self.assertEqual(build.build_output(), self.output)

    def test_committed_file_is_up_to_date(self):
        committed = json.loads(build.DEFAULT_OUT.read_text(encoding="utf-8"))
        self.assertEqual(committed, self.output)

    def test_script_does_not_use_the_network(self):
        self.assertNotIn("urllib", Path(build.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
