"""
Purpose:
    Check the capability taxonomy (src/data/skill_taxonomy.json) and the checker in check_skill_map.py: the
    committed taxonomy must pass every check against the recorded runs of the 25 scenarios, and each check must
    be shown to fail when its rule is broken (by changing a copy of the taxonomy in one small way).

Inputs:
    The committed taxonomy, src/data/gpt4o_workspace_scenarios.json and the vendored no-attack runs.

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/check_skill_map.py, agentdojo_runs.py, validate_skills.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import check_skill_map as cm  # noqa: E402

# Tasks whose no-attack run failed the task; every other task is one of the 25 experiment scenarios.
UTILITY_FALSE = {7, 11, 14, 18, 19, 20, 25, 30, 31, 32, 33, 34, 36, 37, 38}
SCENARIOS = [f"user_task_{n}" for n in sorted(set(range(40)) - UTILITY_FALSE)]

# Table 1 of Yang et al. 2025 (arXiv 2512.07828), copied verbatim for the subtopics the skills use.
YANG_TABLE_1 = {
    "Calendar Management": ["Create/edit events", "Check availability", "Search/filter events", "Summarize/analyze events"],
    "Email Management": ["Search/filter emails", "Create/edit emails", "Send emails", "Delete/unsubscribe emails",
                         "Summarize/analyze emails"],
    "Document & Form Editing": ["Create/edit documents", "Search/filter documents", "Summarize/analyze documents"],
    "Spreadsheet & Data Editing": ["Create/edit spreadsheets", "Search/filter spreadsheets",
                                   "Summarize/analyze spreadsheets"],
    "Account Management": ["Register/log in to accounts", "Manage settings/profiles", "Manage files",
                           "Summarize/analyze account information"],
}

TAXONOMY = cm.load_taxonomy()
CALLS = cm.load_recorded_calls(SCENARIOS)
PROMPTS = {i: runs.user_prompt(runs.load_run(i)) for i in SCENARIOS}


def modified(change) -> dict:
    """Return a deep copy of the committed taxonomy after applying change(copy)."""
    taxonomy = copy.deepcopy(TAXONOMY)
    change(taxonomy)
    return taxonomy


def has(problems: list[str], *words: str) -> bool:
    return any(all(word in problem for word in words) for problem in problems)


class CommittedTaxonomyTest(unittest.TestCase):
    def test_the_library_is_consistent(self):
        self.assertEqual(cm.check_taxonomy(TAXONOMY), [])

    def test_the_map_matches_the_recorded_runs_of_the_25_scenarios(self):
        self.assertEqual(len(SCENARIOS), 25)
        self.assertEqual(cm.scenario_ids(), SCENARIOS)
        self.assertEqual(cm.check_map(TAXONOMY, SCENARIOS, CALLS), [])

    def test_the_skill_definitions_contain_nothing_from_the_scenarios(self):
        self.assertEqual(cm.lint_definitions(TAXONOMY, PROMPTS), [])

    def test_the_planned_library_of_six_skills(self):
        self.assertEqual(list(TAXONOMY["skills"]), [
            "calendar-lookup", "calendar-scheduling", "email-lookup", "email-correspondence",
            "drive-file-management", "document-content"])

    def test_scenarios_per_skill_match_the_plan(self):
        usage = {s: sum(s in e["skills"] for e in TAXONOMY["scenario_map"].values()) for s in TAXONOMY["skills"]}
        self.assertEqual(usage, {"calendar-lookup": 8, "calendar-scheduling": 7, "email-lookup": 8,
                                 "email-correspondence": 1, "drive-file-management": 3, "document-content": 3})

    def test_every_write_tool_is_owned_by_exactly_one_skill(self):
        owned = [t for s in TAXONOMY["skills"].values() for t in s["owned_write_tools"]]
        self.assertEqual(sorted(owned), sorted(runs.WRITE_TOOLS))
        self.assertEqual(len(owned), 10)

    def test_every_tool_belongs_to_some_skill(self):
        covered = set().union(*(cm.skill_tools(s) for s in TAXONOMY["skills"].values()))
        self.assertEqual(covered, set(runs.TOOL_CATEGORY))

    def test_the_yang_reference_is_the_table_1_excerpt(self):
        self.assertEqual(TAXONOMY["yang_reference"], YANG_TABLE_1)

    def test_every_skill_names_yang_subtopics_that_exist(self):
        for skill_id, skill in TAXONOMY["skills"].items():
            for entry in skill["yang"]:
                self.assertIn(entry["subtopic"], YANG_TABLE_1, skill_id)
                self.assertTrue(set(entry["tasks"]) <= set(YANG_TABLE_1[entry["subtopic"]]), skill_id)

    def test_calendar_and_email_are_split_into_read_and_write_skills(self):
        kinds = {s: v["kind"] for s, v in TAXONOMY["skills"].items()}
        self.assertEqual(kinds["calendar-lookup"], "read")
        self.assertEqual(kinds["email-lookup"], "read")
        self.assertEqual(kinds["calendar-scheduling"], "write")
        self.assertEqual(kinds["email-correspondence"], "write")

    def test_the_read_only_calendar_scenarios_use_only_the_read_skill(self):
        for user_task_id in ["user_task_0", "user_task_1", "user_task_2", "user_task_3", "user_task_5", "user_task_10"]:
            self.assertEqual(TAXONOMY["scenario_map"][user_task_id]["skills"], ["calendar-lookup"], user_task_id)
            self.assertFalse(runs.WRITE_TOOLS & set(CALLS[user_task_id]), user_task_id)

    def test_every_data_changing_scenario_maps_to_the_skill_that_owns_the_call(self):
        for user_task_id in SCENARIOS:
            for tool in set(CALLS[user_task_id]) & runs.WRITE_TOOLS:
                owner = next(s for s, v in TAXONOMY["skills"].items() if tool in v["owned_write_tools"])
                self.assertIn(owner, TAXONOMY["scenario_map"][user_task_id]["skills"], (user_task_id, tool))

    def test_file_is_formatted_deterministically(self):
        text = cm.DEFAULT_TAXONOMY.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), TAXONOMY)

    def test_unvalidated_tools_are_reported_not_hidden(self):
        called = {c["function"] for i in runs.list_user_task_ids() for c in runs.tool_calls(runs.load_run(i))}
        notes = cm.unvalidated_tools(TAXONOMY, called)
        self.assertIn("cancel_calendar_event", notes["calendar-scheduling"])
        self.assertIn("delete_email", notes["email-correspondence"])
        self.assertIn("share_file", notes["drive-file-management"])
        self.assertNotIn("calendar-lookup", notes)  # every tool of this skill is called by some recorded run


class LibraryCheckTest(unittest.TestCase):
    def test_a_missing_top_level_key(self):
        self.assertTrue(has(cm.check_taxonomy({"version": "x"}), "missing top-level key"))

    def test_an_unknown_tool(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["read_tools"].append("get_weather"))
        self.assertTrue(has(cm.check_taxonomy(bad), "unknown tool", "get_weather"))

    def test_a_write_tool_owned_twice(self):
        bad = modified(lambda t: t["skills"]["email-lookup"]["owned_write_tools"].append("send_email"))
        self.assertTrue(has(cm.check_taxonomy(bad), "send_email", "exactly one skill"))

    def test_a_write_tool_owned_by_nobody(self):
        bad = modified(lambda t: t["skills"]["email-correspondence"]["owned_write_tools"].remove("delete_email"))
        self.assertTrue(has(cm.check_taxonomy(bad), "delete_email", "exactly one skill"))

    def test_a_write_tool_listed_as_a_read_tool(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["read_tools"].append("create_calendar_event"))
        self.assertTrue(has(cm.check_taxonomy(bad), "write tool", "listed as a read tool"))

    def test_a_read_tool_listed_as_owned(self):
        bad = modified(lambda t: t["skills"]["document-content"]["owned_write_tools"].append("search_files"))
        self.assertTrue(has(cm.check_taxonomy(bad), "not a write tool"))

    def test_kind_must_agree_with_ownership(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"].update(kind="write"))
        self.assertTrue(has(cm.check_taxonomy(bad), "write skill must own"))
        bad = modified(lambda t: t["skills"]["calendar-scheduling"].update(kind="read"))
        self.assertTrue(has(cm.check_taxonomy(bad), "read skill must own none"))

    def test_a_tool_that_belongs_to_no_skill(self):
        bad = modified(lambda t: t["skills"]["drive-file-management"]["read_tools"].remove("list_files"))
        self.assertTrue(has(cm.check_taxonomy(bad), "list_files", "belongs to no skill"))

    def test_yang_subtopic_and_task_must_exist(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["yang"][0].update(subtopic="Weather"))
        self.assertTrue(has(cm.check_taxonomy(bad), "not in the Yang"))
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["yang"][0]["tasks"].append("Book flights"))
        self.assertTrue(has(cm.check_taxonomy(bad), "Book flights"))

    def test_workflows_are_required_and_unique(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"].update(workflows=[]))
        self.assertTrue(has(cm.check_taxonomy(bad), "no workflows"))
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["workflows"].append(
            dict(t["skills"]["calendar-lookup"]["workflows"][0])))
        self.assertTrue(has(cm.check_taxonomy(bad), "workflow ids"))
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["workflows"][0].update(description=" "))
        self.assertTrue(has(cm.check_taxonomy(bad), "no description"))

    def test_skill_keys_and_ids(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"].pop("summary"))
        self.assertTrue(has(cm.check_taxonomy(bad), "keys are"))
        bad = modified(lambda t: t["skills"].update({"Bad_Id": t["skills"].pop("calendar-lookup")}))
        self.assertTrue(has(cm.check_taxonomy(bad), "lowercase words"))


class MapCheckTest(unittest.TestCase):
    def check(self, taxonomy, ids=SCENARIOS, calls=CALLS):
        return cm.check_map(taxonomy, ids, calls)

    def test_a_scenario_without_a_mapping(self):
        bad = modified(lambda t: t["scenario_map"].pop("user_task_24"))
        self.assertTrue(has(self.check(bad), "user_task_24", "no mapping"))

    def test_a_mapping_for_a_task_that_is_not_an_experiment_scenario(self):
        bad = modified(lambda t: t["scenario_map"].update(user_task_7={"skills": ["calendar-scheduling"], "rationale": "x"}))
        self.assertTrue(has(self.check(bad), "user_task_7", "not one of the experiment scenarios"))

    def test_an_unknown_skill_id(self):
        bad = modified(lambda t: t["scenario_map"]["user_task_24"].update(skills=["inbox-reading"]))
        self.assertTrue(has(self.check(bad), "unknown skill"))

    def test_a_missing_rationale_or_skill_list(self):
        bad = modified(lambda t: t["scenario_map"]["user_task_24"].update(rationale=""))
        self.assertTrue(has(self.check(bad), "no rationale"))
        bad = modified(lambda t: t["scenario_map"]["user_task_24"].update(skills=[]))
        self.assertTrue(has(self.check(bad), "one or more distinct skills"))
        bad = modified(lambda t: t["scenario_map"]["user_task_24"].update(skills=["email-lookup", "email-lookup"]))
        self.assertTrue(has(self.check(bad), "distinct skills"))

    def test_a_called_tool_that_no_mapped_skill_has(self):
        bad = modified(lambda t: t["scenario_map"]["user_task_29"].update(skills=["email-lookup"]))
        problems = self.check(bad)
        self.assertTrue(has(problems, "user_task_29", "append_to_file", "no mapped skill has it"))

    def test_a_write_call_needs_the_skill_that_owns_it(self):
        bad = modified(lambda t: t["scenario_map"]["user_task_35"].update(skills=["document-content"]))
        self.assertTrue(has(self.check(bad), "user_task_35", "delete_file", "needs skill 'drive-file-management'"))

    def test_a_mapped_skill_that_is_not_used(self):
        bad = modified(lambda t: t["scenario_map"]["user_task_24"].update(skills=["email-lookup", "drive-file-management"]))
        self.assertTrue(has(self.check(bad), "user_task_24", "drive-file-management", "is not used"))

    def test_a_skill_no_scenario_uses(self):
        def change(t):
            t["scenario_map"]["user_task_13"]["skills"].remove("email-correspondence")
        problems = self.check(modified(change))
        self.assertTrue(has(problems, "email-correspondence", "not used by any scenario"))
        self.assertTrue(has(problems, "user_task_13", "send_email", "needs skill 'email-correspondence'"))

    def test_scenario_check_reports_nothing_for_a_good_mapping(self):
        self.assertEqual(cm.check_scenario(TAXONOMY, "user_task_21", CALLS["user_task_21"]), [])

    def test_scenario_check_for_an_unmapped_scenario(self):
        self.assertEqual(cm.check_scenario(TAXONOMY, "user_task_7", ["reschedule_calendar_event"]),
                         ["user_task_7: not in the scenario map"])

    def test_the_held_out_reschedule_task_is_covered_by_a_skill(self):
        # Task 7 is not an experiment scenario, but the library must be able to express it.
        taxonomy = modified(lambda t: t["scenario_map"].update(
            user_task_7={"skills": ["calendar-scheduling"], "rationale": "held-out check"}))
        called = ["get_current_day", "search_calendar_events", "reschedule_calendar_event"]
        self.assertEqual(cm.check_scenario(taxonomy, "user_task_7", called), [])


class LeakageLintTest(unittest.TestCase):
    def lint(self, taxonomy):
        return cm.lint_definitions(taxonomy, PROMPTS)

    def test_a_scenario_literal_in_a_definition_is_found(self):
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["workflows"][0].update(
            description="Resolve dates such as 2024-05-15."))
        self.assertTrue(has(self.lint(bad), "date", "2024-05-15"))
        bad = modified(lambda t: t["skills"]["email-lookup"]["workflows"][0].update(
            description="Find mail from david.smith@bluesparrowtech.com."))
        self.assertTrue(has(self.lint(bad), "david.smith@bluesparrowtech.com"))
        bad = modified(lambda t: t["skills"]["calendar-lookup"]["workflows"][0].update(
            description="Find the 'Networking event' and report it."))
        self.assertTrue(has(self.lint(bad), "networking event"))

    def test_a_copied_phrase_of_six_words_is_found(self):
        bad = modified(lambda t: t["skills"]["email-lookup"]["workflows"][0].update(
            description="Find it, it's in an email I received recently."))
        self.assertTrue(has(self.lint(bad), "word run"))

    def test_shared_sequences(self):
        self.assertEqual(cm.shared_sequences("A b c d e f g.", "x B c D e F y", 6), [])  # only 5 words in common
        self.assertEqual(cm.shared_sequences("one two three four five six seven", "zero one two three four five six", 6),
                         ["one two three four five six"])
        self.assertEqual(cm.shared_sequences("a b c", "a b c", 6), [])

    def test_definition_text_includes_summaries_and_workflows_but_not_the_scenario_map(self):
        text = cm.definition_text(TAXONOMY)
        self.assertIn(TAXONOMY["skills"]["calendar-lookup"]["summary"], text)
        self.assertIn(TAXONOMY["skills"]["document-content"]["workflows"][0]["description"], text)
        for entry in TAXONOMY["scenario_map"].values():
            self.assertNotIn(entry["rationale"], text)


class CommandLineTest(unittest.TestCase):
    SCRIPT = str(REPO_ROOT / "src" / "scripts" / "check_skill_map.py")

    def test_the_committed_taxonomy_passes_and_the_report_lists_every_scenario(self):
        done = subprocess.run([sys.executable, "-B", self.SCRIPT], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        for user_task_id in SCENARIOS:
            self.assertIn(user_task_id, done.stdout)
        self.assertIn("All checks passed.", done.stdout)
        self.assertIn("note: calendar-scheduling: no recorded run called cancel_calendar_event", done.stdout)

    def test_a_broken_taxonomy_exits_with_status_1(self):
        bad = modified(lambda t: t["scenario_map"].pop("user_task_24"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "taxonomy.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            done = subprocess.run([sys.executable, "-B", self.SCRIPT, "--taxonomy", str(path)],
                                  capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("user_task_24", done.stdout)
        self.assertIn("problem:", done.stdout)

    def test_the_script_writes_no_files_and_does_not_use_the_network(self):
        text = Path(cm.__file__).read_text(encoding="utf-8")
        for word in ("urllib", "write_text", "open(", "requests"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
