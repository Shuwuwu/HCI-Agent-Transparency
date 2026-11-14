"""Tests for build_review_sheet.py: the review sheet's tables are complete, consistent with the taxonomy and the
skills, deterministic, and carry empty rater columns (nothing is scored automatically)."""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import build_review_sheet as rs  # noqa: E402
import validate_capability_skills as v  # noqa: E402

LIBRARY = v.load_library()
TABLES = rs.build_tables(library=LIBRARY)
SKILLS = list(LIBRARY.taxonomy["skills"])


class TableShapeTest(unittest.TestCase):
    def test_one_overview_row_per_skill(self):
        self.assertEqual([r["skill"] for r in TABLES["skill_overview"]], SKILLS)

    def test_every_skill_has_every_criterion_once(self):
        rows = TABLES["skill_criteria"]
        self.assertEqual(len(rows), len(SKILLS) * len(rs.CRITERIA))
        self.assertEqual({(r["skill"], r["criterion"]) for r in rows}, {(s, c) for s in SKILLS for c in rs.CRITERIA})

    def test_six_criteria_are_kept(self):
        self.assertEqual(list(rs.CRITERIA), ["IA1", "TC2", "GE1", "GE2", "SA1", "SA3"])

    def test_criteria_cover_the_four_requested_dimensions(self):
        self.assertEqual({d for d, _ in rs.CRITERIA.values()},
                         {"instruction accuracy", "tool coverage", "generalizability", "safety"})

    def test_one_scenario_row_per_scenario_and_mapped_skill(self):
        expected = {(u, s) for u, e in LIBRARY.taxonomy["scenario_map"].items() for s in e["skills"]}
        rows = TABLES["scenario_support"]
        self.assertEqual({(r["scenario"], r["skill"]) for r in rows}, expected)
        self.assertEqual(len(rows), len(expected))
        self.assertEqual(len({r["scenario"] for r in rows}), 25)

    def test_scenario_rows_are_sorted_numerically(self):
        numbers = [int(r["scenario"].split("_")[-1]) for r in TABLES["scenario_support"]]
        self.assertEqual(numbers, sorted(numbers))


class ContentTest(unittest.TestCase):
    def test_rater_columns_are_empty(self):
        for rows in TABLES.values():
            for row in rows:
                for key, value in row.items():
                    if key.startswith(rs.RATERS):
                        self.assertEqual(value, "", key)

    def test_overview_lists_each_scenario_of_the_skill(self):
        for row in TABLES["skill_overview"]:
            expected = [u.removeprefix("user_task_") for u, e in LIBRARY.taxonomy["scenario_map"].items()
                        if row["skill"] in e["skills"]]
            self.assertEqual(row["scenarios"].split(", "), expected)

    def test_validator_version_and_result_are_recorded(self):
        for row in TABLES["skill_overview"]:
            self.assertEqual(row["validator_version"], v.VALIDATOR_VERSION)
            self.assertTrue(row["validator_result"].startswith(("PASS", "FAIL")))

    def test_recorded_calls_match_the_run_and_split_between_skills(self):
        rows = {(r["scenario"], r["skill"]): r for r in TABLES["scenario_support"]}
        row = rows[("user_task_13", "email-correspondence")]
        self.assertIn("search_emails(query='TODOs for the week'", row["recorded_calls"])
        self.assertEqual(row["run_tools_in_this_skill"], "search_emails, send_email")
        self.assertEqual(rows[("user_task_13", "document-content")]["run_tools_of_other_mapped_skills"],
                         "search_emails, send_email")

    def test_read_skill_has_no_data_changing_columns_filled(self):
        for row in TABLES["scenario_support"]:
            if row["skill"] in ("calendar-lookup", "email-lookup"):
                self.assertEqual(row["skill_data_changing_tools_not_called"], "")

    def test_evidence_only_for_criteria_that_have_facts(self):
        facts = {"workflow_tools": {"Find": ["a", "b"]}, "owned": [], "write_lines": []}
        self.assertEqual(rs.criterion_evidence("TC2", facts), "Find: 2 tools (a, b)")
        self.assertIn("NA", rs.criterion_evidence("SA1", facts))
        self.assertEqual(rs.criterion_evidence("SA1", {**facts, "write_lines": ["- `x`: only on request"]}),
                         "- `x`: only on request")
        for criterion in ("IA1", "GE1", "GE2", "SA3"):
            self.assertEqual(rs.criterion_evidence(criterion, facts), "")

    def test_long_argument_values_are_cut(self):
        text = rs.tool_call_text({"function": "f", "args": {"a": "x" * 200, "b": 3}})
        self.assertIn("...", text)
        self.assertLess(len(text), 100)
        self.assertIn("b=3", text)


class OutputTest(unittest.TestCase):
    def test_files_are_written_and_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            paths = rs.write_review(Path(first), TABLES)
            rs.write_review(Path(second), TABLES)
            self.assertEqual({p.name for p in paths},
                             {"skill_overview.csv", "skill_criteria.csv", "scenario_support.csv", "review_guide.md"})
            for path in paths:
                self.assertEqual(path.read_bytes(), (Path(second) / path.name).read_bytes())
            with (Path(first) / "scenario_support.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 30)

    def test_missing_skill_is_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(FileNotFoundError):
                rs.build_tables(Path(empty), LIBRARY)

    def test_guide_lists_every_criterion(self):
        guide = rs.guide_text()
        for criterion in rs.CRITERIA:
            self.assertIn(f"| {criterion} |", guide)


if __name__ == "__main__":
    unittest.main()
