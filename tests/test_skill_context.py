"""
Purpose:
    Check that build_skill_context.py builds correct skill-generation context packages: the docstring parser
    behaves like AgentDojo's, all 24 tools are described as the agent saw them, the environment data has its
    benign injection-slot defaults filled in, each package holds the right trace and data, and nothing in a
    package contains an injection or a local path.

Inputs:
    The committed src/data/skill_context/ and src/data/experiment/ folders; for the tests that rebuild from
    source, the AgentDojo clone at the pinned commit (those tests are skipped, with a message, if it is missing).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_skill_context.py, agentdojo_runs.py, agentdojo_source.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as source  # noqa: E402
import build_skill_context as context  # noqa: E402

UTILITY_FALSE = {7, 11, 14, 18, 19, 20, 25, 30, 31, 32, 33, 34, 36, 37, 38}
SAFE_TASKS = sorted(set(range(40)) - UTILITY_FALSE)
TOTAL_DOCUMENTED_ARGUMENTS = 37  # summed by hand from the tool listing: 24 tools, 37 documented arguments


def load_package(number: int) -> dict:
    return json.loads((context.DEFAULT_OUT_DIR / f"user_task_{number}.json").read_text(encoding="utf-8"))


def clone_unavailable() -> str | None:
    try:
        source.check_pinned_commit()
    except source.AgentDojoSourceError as error:
        return str(error)
    return None


class DocstringParserTest(unittest.TestCase):
    """The parser must split docstrings the way AgentDojo's registration does (reST style)."""

    def test_short_long_and_params(self):
        parsed = context.parse_docstring(
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
        parsed = context.parse_docstring("Lists everything.")
        self.assertEqual(parsed, {"short": "Lists everything.", "long": None, "params": {}})

    def test_colon_inside_a_description_is_kept(self):
        parsed = context.parse_docstring("Thing.\n\n:param t: Format: YYYY-MM-DD HH:MM.")
        self.assertEqual(parsed["params"], {"t": "Format: YYYY-MM-DD HH:MM."})

    def test_non_parameter_tags_are_ignored(self):
        parsed = context.parse_docstring("Thing.\n\n:param x: An x.\n:return: Something.")
        self.assertEqual(parsed["params"], {"x": "An x."})

    def test_default_slots_are_filled(self):
        self.assertEqual(context.fill_defaults("a {v1} b {v2} {v1}", {"v1": "X", "v2": "Y"}), "a X b Y X")
        self.assertEqual(context.fill_defaults("keep {other}", {"v1": "X"}), "keep {other}")


class ToolDescriptionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_unavailable()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")
        cls.tools = context.build_tools()
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
        for key in context.TOOL_FILES:
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
        self.assertEqual(context.build_tools(), self.tools)


class EnvironmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_unavailable()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    def test_sixteen_injection_defaults(self):
        defaults = context.injection_defaults()
        self.assertEqual(len(defaults), 16)
        self.assertEqual(
            defaults["calendar_networking_event_injection_email"],
            "Don't miss this opportunity to expand your professional network!",
        )

    def test_every_slot_in_the_data_files_has_a_default(self):
        defaults = context.injection_defaults()
        slots = set()
        for _, key in context.GROUP_DATA.values():
            slots |= set(re.findall(r"\{([a-z_]+)\}", source.read_source(key)))
        self.assertTrue(slots)
        self.assertLessEqual(slots, set(defaults))

    def test_filled_text_matches_what_the_agent_saw(self):
        """The default text we fill in appears in the recorded tool output of task 0 (ignoring line wrapping)."""
        environment, _ = context.build_environment(["calendar_tools"])
        default = context.injection_defaults()["calendar_networking_event_injection_email"]
        self.assertIn(default, environment["files"][0]["text"])
        trace = runs.load_run("user_task_0")
        tool_text = " ".join(m["content"] for m in trace["messages"] if m["role"] == "tool")
        self.assertIn(default, " ".join(tool_text.split()))

    def test_account_and_day(self):
        environment, hashes = context.build_environment(["calendar_tools"])
        self.assertEqual(environment["account_email"], "emma.johnson@bluesparrowtech.com")
        self.assertEqual(environment["current_day"], "2024-05-15")
        self.assertEqual(sorted(hashes), ["workspace_calendar_data"])

    def test_groups_map_to_files_in_fixed_order(self):
        environment, _ = context.build_environment(["drive_tools", "email_tools"])
        self.assertEqual([f["name"] for f in environment["files"]], ["inbox", "cloud_drive"])
        self.assertEqual(context.build_environment([])[0]["files"], [])


class CommittedPackagesTest(unittest.TestCase):
    """Properties of the committed packages (no clone needed)."""

    def test_one_package_per_experiment_scenario(self):
        found = sorted(runs.task_number(p.stem) for p in context.DEFAULT_OUT_DIR.glob("user_task_*.json"))
        self.assertEqual(found, SAFE_TASKS)
        self.assertEqual(len(found), 25)

    def test_package_keys(self):
        for number in SAFE_TASKS:
            self.assertEqual(
                list(load_package(number)),
                ["user_task_id", "task_prompt", "tools", "execution_trace", "environment", "provenance"],
                number,
            )

    def test_prompt_and_trace_are_the_scenarios_own(self):
        for number in SAFE_TASKS:
            folder = context.DEFAULT_EXPERIMENT_DIR / f"user_task_{number}"
            metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            trace = json.loads((folder / "no_attack_execution_trace.json").read_text(encoding="utf-8"))
            package = load_package(number)
            self.assertEqual(package["user_task_id"], f"user_task_{number}")
            self.assertEqual(package["task_prompt"], metadata["task_prompt"])
            self.assertEqual(package["execution_trace"], trace)
            self.assertEqual(runs.user_prompt(package["execution_trace"]), package["task_prompt"])

    def test_all_packages_list_the_same_24_tools(self):
        reference = load_package(SAFE_TASKS[0])["tools"]
        self.assertEqual(len(reference), 24)
        for number in SAFE_TASKS:
            self.assertEqual(load_package(number)["tools"], reference, number)

    def test_environment_covers_exactly_the_tool_groups_used(self):
        wanted = {"email_tools": "inbox", "calendar_tools": "calendar", "drive_tools": "cloud_drive"}
        for number in SAFE_TASKS:
            metadata = json.loads(
                (context.DEFAULT_EXPERIMENT_DIR / f"user_task_{number}" / "metadata.json").read_text(encoding="utf-8")
            )
            used = sorted(wanted[g] for g, names in metadata["tools_called"].items() if names)
            shown = sorted(f["name"] for f in load_package(number)["environment"]["files"])
            self.assertEqual(shown, used, number)
        # Hand-checked examples.
        self.assertEqual([f["name"] for f in load_package(21)["environment"]["files"]], ["calendar"])
        self.assertEqual([f["name"] for f in load_package(35)["environment"]["files"]], ["cloud_drive"])
        self.assertEqual([f["name"] for f in load_package(13)["environment"]["files"]], ["inbox", "cloud_drive"])

    def test_no_injection_and_no_unfilled_slots_anywhere(self):
        for number in SAFE_TASKS:
            text = (context.DEFAULT_OUT_DIR / f"user_task_{number}.json").read_text(encoding="utf-8")
            self.assertNotIn("<INFORMATION>", text, number)
            self.assertNotIn(runs.ATTACKER_EMAIL, text, number)
            for env_file in load_package(number)["environment"]["files"]:
                self.assertIsNone(re.search(r"\{[a-z_]+\}", env_file["text"]), (number, env_file["name"]))

    def test_no_local_paths(self):
        for number in SAFE_TASKS:
            text = (context.DEFAULT_OUT_DIR / f"user_task_{number}.json").read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text)
            self.assertNotIn(str(REPO_ROOT), text)

    def test_provenance_names_the_pinned_commit(self):
        for number in SAFE_TASKS:
            provenance = load_package(number)["provenance"]
            self.assertEqual(provenance["commit"], source.PINNED_COMMIT)
            self.assertTrue({"tool_email_client", "workspace_task_suite", "workspace_injection_vectors"} <= set(provenance["source_files"]))
            for entry in provenance["source_files"].values():
                self.assertRegex(entry["git_blob"], r"^[0-9a-f]{40}$")

    def test_script_does_not_use_the_network(self):
        self.assertNotIn("urllib", Path(context.__file__).read_text(encoding="utf-8"))


class RebuildFromSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_unavailable()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    def test_committed_packages_are_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = context.build_all(out_dir=Path(tmp))
            self.assertEqual([p.name for p in written], [f"user_task_{n}.json" for n in SAFE_TASKS])
            for path in written:
                self.assertEqual(path.read_bytes(), (context.DEFAULT_OUT_DIR / path.name).read_bytes(), path.name)

    def test_provenance_hashes_match_the_clone(self):
        for number in (0, 13):
            for key, entry in load_package(number)["provenance"]["source_files"].items():
                self.assertEqual(entry["git_blob"], source.blob_hash(key), (number, key))
                self.assertEqual(entry["path"], source.SOURCE_FILES[key])

    def test_missing_experiment_folders_give_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                context.build_all(experiment_dir=Path(tmp), out_dir=Path(tmp) / "out")


if __name__ == "__main__":
    unittest.main()
