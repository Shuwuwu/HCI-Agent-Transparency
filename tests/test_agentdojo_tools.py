"""
Purpose:
    Check that agentdojo_tools.py describes the 24 workspace tools as the agent saw them: the docstring parser
    behaves like AgentDojo's, every tool has the right group, description and documented arguments, environment
    dependencies are not arguments, and the one enum argument lists its allowed values.

Inputs:
    The AgentDojo clone at the pinned commit (the tool-description tests are skipped, with a message, if it is
    missing); the parser tests need no clone.

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/agentdojo_tools.py, agentdojo_runs.py, agentdojo_source.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import ast
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as source  # noqa: E402
import agentdojo_tools as tool_specs  # noqa: E402

TOTAL_DOCUMENTED_ARGUMENTS = 37  # summed by hand from the tool listing: 24 tools, 37 documented arguments


def clone_unavailable() -> str | None:
    try:
        source.check_pinned_commit()
    except source.AgentDojoSourceError as error:
        return str(error)
    return None


class DocstringParserTest(unittest.TestCase):
    """The parser must split docstrings the way AgentDojo's registration does (reST style)."""

    def test_short_long_and_params(self):
        parsed = tool_specs.parse_docstring(
            """Does a thing. Returns None when nothing happens.
            It also does a second thing.

            :param a: First argument.
            :param b: Second argument, which continues
            on a second line.
            """
        )
        self.assertEqual(parsed["short"], "Does a thing. Returns None when nothing happens.")
        self.assertEqual(parsed["long"], "It also does a second thing.")
        self.assertEqual(parsed["params"], {"a": "First argument.", "b": "Second argument, which continues\non a second line."})

    def test_no_params_and_no_long_description(self):
        parsed = tool_specs.parse_docstring("Lists everything.")
        self.assertEqual(parsed, {"short": "Lists everything.", "long": None, "params": {}})

    def test_colon_inside_a_description_is_kept(self):
        parsed = tool_specs.parse_docstring("Thing.\n\n:param t: Format: YYYY-MM-DD HH:MM.")
        self.assertEqual(parsed["params"], {"t": "Format: YYYY-MM-DD HH:MM."})

    def test_non_parameter_tags_are_ignored(self):
        parsed = tool_specs.parse_docstring("Thing.\n\n:param x: An x.\n:return: Something.")
        self.assertEqual(parsed["params"], {"x": "An x."})


class ToolDescriptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_unavailable()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")
        cls.tools = tool_specs.build_tools()
        cls.by_name = {t["name"]: t for t in cls.tools}

    def test_all_24_tools_in_agentdojos_order_with_the_right_groups(self):
        self.assertEqual([t["name"] for t in self.tools], list(runs.TOOL_CATEGORY))
        self.assertEqual(len(self.tools), 24)
        for tool in self.tools:
            self.assertEqual(tool["group"], runs.TOOL_CATEGORY[tool["name"]])

    def test_documented_argument_count_matches_an_independent_count(self):
        shown = sum(len(t["parameters"]) for t in self.tools)
        self.assertEqual(shown, TOTAL_DOCUMENTED_ARGUMENTS)
        # Independent route: count ":param" lines in the registered functions' docstrings with a plain regex.
        counted = 0
        for key in tool_specs.TOOL_FILES:
            for node in ast.parse(source.read_source(key)).body:
                if isinstance(node, ast.FunctionDef) and node.name in self.by_name:
                    counted += len(re.findall(r"^\s*:param \w+:", ast.get_docstring(node, clean=False) or "", re.M))
        self.assertEqual(counted, TOTAL_DOCUMENTED_ARGUMENTS)

    def test_environment_dependencies_are_not_arguments(self):
        hidden = {"calendar", "inbox", "cloud_drive"}
        for tool in self.tools:
            self.assertTrue(hidden.isdisjoint(p["name"] for p in tool["parameters"]), tool["name"])

    def test_create_calendar_event(self):
        tool = self.by_name["create_calendar_event"]
        self.assertEqual(tool["description"], "Creates a new calendar event with the given details and adds it to the calendar.")
        self.assertEqual(tool["extra_description"], "It also sends an email to the participants with the event details.")
        names = [p["name"] for p in tool["parameters"]]
        self.assertEqual(names, ["title", "start_time", "end_time", "description", "participants", "location"])
        required = {p["name"]: p["required"] for p in tool["parameters"]}
        self.assertEqual(required, {"title": True, "start_time": True, "end_time": True,
                                    "description": False, "participants": False, "location": False})
        by_arg = {p["name"]: p for p in tool["parameters"]}
        self.assertEqual(by_arg["description"]["default"], "")
        self.assertIsNone(by_arg["participants"]["default"])
        self.assertEqual(by_arg["participants"]["type"], "list[str] | None")
        self.assertEqual(by_arg["start_time"]["description"], "The start time of the event. Must be in format YYYY-MM-DD HH:MM.")

    def test_multi_line_argument_description_is_kept(self):
        end = {p["name"]: p for p in self.by_name["reschedule_calendar_event"]["parameters"]}["new_end_time"]
        self.assertTrue(end["description"].endswith("keep the event duration the same."))
        self.assertIn("\nIf `null`", end["description"])

    def test_agent_sees_only_the_first_line_of_the_description(self):
        tool = self.by_name["get_current_day"]
        self.assertEqual(tool["description"], "Returns the current day in ISO format, e.g. '2022-01-01'.")
        self.assertTrue(tool["extra_description"].startswith("It is useful to know"))
        self.assertEqual(tool["parameters"], [])

    def test_none_is_written_as_null_in_descriptions(self):
        for tool in self.tools:
            self.assertNotIn("None", tool["description"], tool["name"])

    def test_enum_argument(self):
        permission = {p["name"]: p for p in self.by_name["share_file"]["parameters"]}["permission"]
        self.assertEqual((permission["type"], permission["enum"]), ("SharingPermission", ["r", "rw"]))
        others = [p for t in self.tools for p in t["parameters"] if p is not permission]
        self.assertTrue(all(p["enum"] is None for p in others))

    def test_send_email_arguments(self):
        params = {p["name"]: p for p in self.by_name["send_email"]["parameters"]}
        self.assertEqual(list(params), ["recipients", "subject", "body", "attachments", "cc", "bcc"])
        self.assertEqual(params["attachments"]["type"], "list[dict] | None")
        self.assertTrue(params["recipients"]["required"])

    def test_tool_list_is_deterministic(self):
        self.assertEqual(tool_specs.build_tools(), self.tools)


if __name__ == "__main__":
    unittest.main()
