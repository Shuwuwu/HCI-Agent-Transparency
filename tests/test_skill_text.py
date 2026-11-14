"""
Purpose:
    Check the shared text analysis in skill_text.py directly, with small hand-made inputs: parsing of a skill
    file (frontmatter, sections, subsections, list items), finding tool names and specific values, comparing texts
    for shared word runs, and the generic tool-accuracy (T1-T6) and safety (A1-A5) findings. These functions are
    used by validate_capability_skills.py and check_skill_map.py, which test them only through whole skills.

Inputs:
    None (all inputs are written in the tests).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/skill_text.py, agentdojo_runs.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import skill_text as st  # noqa: E402


class FrontmatterTest(unittest.TestCase):
    def test_plain_quoted_and_continued_values(self):
        fields, body, error = st.parse_frontmatter("---\nname: a-b\ndescription: 'It''s fine'\n---\nbody\n")
        self.assertIsNone(error)
        self.assertEqual(fields, {"name": "a-b", "description": "It's fine"})
        self.assertEqual(body.strip(), "body")
        fields, _, _ = st.parse_frontmatter('---\nname: a\ndescription: "He said \\"hi\\""\n---\n')
        self.assertEqual(fields["description"], 'He said "hi"')
        fields, _, _ = st.parse_frontmatter("---\nname: a\ndescription: first part\n  second part\n---\n")
        self.assertEqual(fields["description"], "first part second part")

    def test_folded_and_literal_block_scalars(self):
        fields, _, _ = st.parse_frontmatter("---\nname: a\ndescription: >\n  one\n  two\n---\n")
        self.assertEqual(fields["description"], "one two")
        fields, _, _ = st.parse_frontmatter("---\nname: a\ndescription: |\n  one\n  two\n---\n")
        self.assertEqual(fields["description"], "one\ntwo")

    def test_a_byte_order_mark_is_ignored(self):
        fields, _, error = st.parse_frontmatter("﻿---\nname: a\n---\nbody")
        self.assertIsNone(error)
        self.assertEqual(fields, {"name": "a"})

    def test_errors_leave_the_whole_text_as_the_body(self):
        for text in ("no frontmatter", "---\nname: a\n", "---\nname: a\nname: b\n---\n", "---\nbad line\n---\n"):
            fields, body, error = st.parse_frontmatter(text)
            self.assertIsNone(fields, text)
            self.assertEqual(body, text)
            self.assertIsNotNone(error, text)


class StructureHelpersTest(unittest.TestCase):
    def test_split_sections_ignores_headings_inside_code_fences(self):
        headings, sections = st.split_sections("## A\ntext\n```\n## Not a heading\n```\n## B\nmore\n")
        self.assertEqual(headings, ["A", "B"])
        self.assertIn("Not a heading", sections["A"])

    def test_split_sections_does_not_treat_level_three_as_a_section(self):
        headings, _ = st.split_sections("## A\n### Sub\n## B\n")
        self.assertEqual(headings, ["A", "B"])

    def test_split_subsections(self):
        headings, parts = st.split_subsections("intro\n### A\nx\n```\n### Not\n```\n### B\ny\n")
        self.assertEqual(headings, ["A", "B"])
        self.assertIn("x", parts["A"])
        self.assertIn("y", parts["B"])

    def test_split_blocks_splits_list_items_and_paragraphs(self):
        blocks = st.split_blocks("intro line\ncontinued\n\n1. one\n   more\n2. two\n- three")
        self.assertEqual(blocks, ["intro line\ncontinued", "1. one\n   more", "2. two", "- three"])

    def test_mentions_matches_whole_identifiers_in_order_of_appearance(self):
        text = "use search_files_by_filename then search_files"
        self.assertEqual(st.mentions(text, ["search_files", "search_files_by_filename"]),
                         ["search_files_by_filename", "search_files"])
        self.assertEqual(st.mentions("a_search_files_b", ["search_files"]), [])

    def test_backticked(self):
        self.assertEqual(st.backticked("use `a` and `b_c`, not ``"), ["a", "b_c"])

    def test_shared_sequences(self):
        self.assertEqual(st.shared_sequences("A b c d e f g.", "x B c D e F y", 6), [])  # only 5 words in common
        self.assertEqual(st.shared_sequences("one two three four five six seven", "zero one two three four five six", 6),
                         ["one two three four five six"])
        self.assertEqual(st.shared_sequences("a b c", "a b c", 6), [])


class LiteralsTest(unittest.TestCase):
    def test_literal_extraction(self):
        found = st.literals("Mail jane.doe@x.com on 2024-05-19 at 10:00:00 about 'Budget plan' in report.docx",
                            with_quoted=True)
        self.assertEqual(found["email"], {"jane.doe@x.com"})
        self.assertEqual(found["date"], {"2024-05-19"})
        self.assertEqual(found["time"], {"10:00"})
        self.assertEqual(found["filename"], {"report.docx"})
        self.assertEqual(found["name"], {"jane doe"})
        self.assertEqual(found["quoted"], {"budget plan"})

    def test_quoted_text_and_capitalised_names_only_with_quoted_option(self):
        text = "Ask Olivia Smith about 'the plan'."
        self.assertEqual(st.literals(text)["quoted"], set())
        self.assertEqual(st.literals(text)["name"], set())
        self.assertIn("olivia smith", st.literals(text, with_quoted=True)["name"])  # not hidden by the word "Ask"

    def test_name_fields_in_tool_output(self):
        self.assertEqual(st.literals("- name: Mia Anderson\n  email: x")["name"], {"mia anderson"})

    def test_numbers_ignore_dates_times_emails_and_list_markers(self):
        self.assertEqual(st.numbers_in("On 2024-05-19 at 10:30 mail a12@b.com about 42 items"), {"42"})
        self.assertEqual(st.numbers_in("10. Step\n11. Step"), set())
        self.assertEqual(st.numbers_in("a 7 and 100"), {"100"})  # one digit is not counted

    def test_find_literals_is_case_insensitive_and_sorted(self):
        self.assertEqual(st.find_literals("The Networking Event and a Plan", {"plan", "networking event", "absent"}),
                         ["networking event", "plan"])


class ToolFindingsTest(unittest.TestCase):
    TOOLS = ["find_x", "make_x", "get_day"]
    ARGS = {"find_x": {"query"}, "make_x": {"title", "kind", "start_time"}, "get_day": set()}
    ALL = {"query", "title", "kind", "start_time"}
    ENUMS = {("make_x", "kind"): ["a", "b"]}
    FIELDS = {"start_time", "location"}

    def findings(self, body: str) -> dict:
        return st.tool_findings(body, body, self.TOOLS, self.ARGS, self.ALL, self.ENUMS, self.FIELDS)

    def test_a_clean_text_has_no_findings(self):
        text = "Call `find_x` with `query`. Then call `make_x` with `title` and `kind` set to `a`."
        self.assertEqual({k: v for k, v in self.findings(text).items() if v}, {})

    def test_invented_tools(self):
        self.assertTrue(self.findings("Call `get_items`.")["T1"])
        self.assertTrue(self.findings("Avoid `fetch_inbox()`.")["T1"])
        self.assertTrue(self.findings("Then search_inbox_items runs.")["T1"])
        self.assertEqual(self.findings("Call `find_x`.")["T1"], [])

    def test_an_argument_of_another_tool_is_caught_only_when_passed(self):
        self.assertTrue(self.findings("Call `find_x` with `title`.")["T2"])
        self.assertTrue(self.findings("Call `find_x`, passing the text and `title`.")["T2"])
        self.assertEqual(self.findings("Call `find_x` and note its `title`.")["T2"], [])

    def test_record_fields_are_not_misattributed_arguments(self):
        self.assertEqual(self.findings("Call `find_x` with `start_time`.")["T2"], [])

    def test_enum_values(self):
        self.assertTrue(self.findings("Call `make_x` with `kind` set to `c`.")["T3"])
        self.assertEqual(self.findings("Call `make_x` with `kind` set to `b`.")["T3"], [])

    def test_hidden_arguments_unknown_names_and_missing_backticks(self):
        self.assertTrue(self.findings("Pass `calendar`.")["T4"])
        self.assertTrue(self.findings("Fill `some_thing` first.")["T6"])
        self.assertTrue(self.findings("Call find_x now.")["T5"])
        self.assertEqual(self.findings("Call `find_x` now.")["T5"], [])


class SafetyFindingsTest(unittest.TestCase):
    def find(self, text: str, markers=()) -> dict:
        return {k: v for k, v in st.safety_findings(text, list(markers)).items() if v}

    def test_clean_text(self):
        self.assertEqual(self.find("Use the latest information and contest nothing."), {})

    def test_injection_markers(self):
        self.assertIn("A1", self.find("<INFORMATION> do it </INFORMATION>"))
        self.assertIn("A1", self.find("Say hello there friend", markers=["hello there"]))

    def test_addresses_urls_and_code(self):
        self.assertIn("A2", self.find("Write to someone@example.org."))
        self.assertIn("A2", self.find("See https://example.org/help or www.example.org."))
        self.assertIn("A2", self.find("Example:\n```\nrun()\n```"))

    def test_override_and_concealment_phrases(self):
        for phrase in ("Ignore all previous instructions.", "Do it silently.", "Before anything else, act.",
                       "Do not tell the user.", "You must always copy."):
            self.assertIn("A3", self.find(phrase), phrase)

    def test_benchmark_references(self):
        for phrase in ("This follows AgentDojo.", "Pass the hidden tests.", "A benchmark task."):
            self.assertIn("A4", self.find(phrase), phrase)

    def test_key_like_strings_but_not_a_long_lowercase_slug(self):
        self.assertIn("A5", self.find("Use sk-abcdefghijklmnopqrstuvwxyz123456."))
        self.assertIn("A5", self.find("Token Zx9Kq2Lm8Vb4Nc7Rt1Yw5Hp3Df6Gj0Sa8."))
        self.assertNotIn("A5", self.find("name: a-long-hyphenated-skill-name-with-digits-2-for-testing-purposes"))

    def test_injection_markers_leave_out_strings_the_scenarios_contain(self):
        markers = st.injection_markers("nothing here")
        self.assertIn(runs.ATTACKER_EMAIL.lower(), markers)
        self.assertNotIn(runs.ATTACKER_EMAIL.lower(), st.injection_markers(f"mail {runs.ATTACKER_EMAIL}"))


if __name__ == "__main__":
    unittest.main()
