"""
Purpose:
    Check that build_capability_prompts.py builds correct and leak-free requests for the capability skills: one
    per skill, containing the skill's scope, workflows with headings, and full tool specifications, with each
    data-changing tool shown only to the skill that owns it, no scenario content of any kind, no reference to
    other skills, and a rebuild that reproduces the committed files exactly (no AgentDojo clone needed).

Inputs:
    The committed src/data/skill_library/prompts/ and sources/ files, src/prompts/skill_capability_*_v2.md,
    src/data/skill_taxonomy.json and the vendored runs (for the leakage checks).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_capability_prompts.py, build_skill_sources.py, prompt_utils.py, check_skill_map.py,
    validate_skills.py, agentdojo_runs.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import agentdojo_runs as runs  # noqa: E402
import build_capability_prompts as cp  # noqa: E402
import build_skill_sources as sources  # noqa: E402
import check_skill_map as cm  # noqa: E402
import prompt_utils  # noqa: E402
from scenario_leakage import leaked_literals, scenario_material, tool_text_of  # noqa: E402

TAXONOMY = cm.load_taxonomy()
FILES = sorted(cp.DEFAULT_OUT_DIR.glob("*.json"))
RECORDS = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FILES}
PACKAGES = {s: json.loads((cp.DEFAULT_SOURCES_DIR / f"{s}.json").read_text(encoding="utf-8")) for s in RECORDS}
SYSTEM_TEXT = cp.SYSTEM_TEMPLATE.read_text(encoding="utf-8").strip()


def prompt_input(skill_id: str) -> str:
    return RECORDS[skill_id]["request"]["input"]


class CommittedPromptsTest(unittest.TestCase):
    def test_one_prompt_per_skill(self):
        self.assertEqual(sorted(RECORDS), sorted(TAXONOMY["skills"]))
        self.assertEqual(len(RECORDS), 6)

    def test_record_keys_and_generation_settings(self):
        for skill_id, record in RECORDS.items():
            self.assertEqual(list(record), ["skill_id", "template_version", "prompt_sha256",
                                            "estimated_input_tokens", "limits", "source", "request"], skill_id)
            self.assertEqual(record["skill_id"], skill_id)
            self.assertEqual(record["template_version"], "skill_capability_v2")
            request = record["request"]
            self.assertEqual(set(request), {"model", "instructions", "input", "reasoning", "max_output_tokens"})
            self.assertEqual((request["model"], request["reasoning"], request["max_output_tokens"]),
                             ("gpt-6.1-sol", {"effort": "high"}, 16384))

    def test_hashes_match_the_request_and_the_source_package(self):
        for skill_id, record in RECORDS.items():
            self.assertEqual(record["prompt_sha256"], prompt_utils.canonical_hash(record["request"]), skill_id)
            package_bytes = (cp.DEFAULT_SOURCES_DIR / record["source"]["skill_sources"]).read_bytes()
            self.assertEqual(record["source"]["skill_sources_sha256"], hashlib.sha256(package_bytes).hexdigest())
            self.assertEqual(record["source"]["definition_sha256"], PACKAGES[skill_id]["provenance"]["definition_sha256"])

    def test_system_prompt_is_the_template_and_user_prompt_is_fully_filled(self):
        for skill_id, record in RECORDS.items():
            self.assertEqual(record["request"]["instructions"], SYSTEM_TEXT)
            self.assertNotIn("{{", record["request"]["input"], skill_id)
            self.assertTrue(record["request"]["input"].endswith("Return only the file."), skill_id)

    def test_prompt_states_the_skill_identifier_scope_and_kind(self):
        for skill_id, package in PACKAGES.items():
            text = prompt_input(skill_id)
            self.assertIn(f"Identifier (use it as `name`): {skill_id}", text)
            self.assertIn(f"Scope: {package['summary']}", text)
            self.assertIn(cp.KIND_STATEMENTS[package["kind"]], text)
            other = cp.KIND_STATEMENTS["write" if package["kind"] == "read" else "read"]
            self.assertNotIn(other, text)

    def test_every_workflow_is_listed_with_its_heading_in_order(self):
        for skill_id, package in PACKAGES.items():
            text = prompt_input(skill_id)
            listed = re.findall(r"^- Heading `### (.+?)`: (.+)$", text, flags=re.M)
            expected = [(cp.workflow_heading(w["id"]), w["description"]) for w in package["workflows"]]
            self.assertEqual(listed, expected, skill_id)
            headings = [h for h, _ in listed]
            self.assertEqual(len(set(headings)), len(headings), skill_id)

    def test_every_tool_of_the_skill_is_specified_in_full_and_no_other_tool_is_shown(self):
        for skill_id, package in PACKAGES.items():
            text = prompt_input(skill_id)
            tools_section = text[text.index("## Tools"):text.index("## Data schema")]
            shown = re.findall(r"^### `(\w+)` \(", tools_section, flags=re.M)
            self.assertEqual(shown, [t["name"] for t in package["tools"]], skill_id)
            for tool in package["tools"]:
                for parameter in tool["parameters"]:
                    self.assertIn(f"- `{parameter['name']}` ({parameter['type']},", tools_section, (skill_id, tool["name"]))
            for name in runs.TOOL_CATEGORY:
                if name not in shown:
                    self.assertNotIn(name, text, (skill_id, name))

    def test_each_write_tool_is_shown_only_in_its_owners_prompt(self):
        for write_tool in runs.WRITE_TOOLS:
            owner = next(s for s, v in TAXONOMY["skills"].items() if write_tool in v["owned_write_tools"])
            for skill_id in RECORDS:
                self.assertEqual(write_tool in prompt_input(skill_id), skill_id == owner, (write_tool, skill_id))

    def test_the_changes_data_note_appears_exactly_for_write_tools(self):
        for skill_id, package in PACKAGES.items():
            self.assertEqual(prompt_input(skill_id).count("This tool changes the user's data."),
                             len(package["owned_write_tools"]), skill_id)

    def test_data_schema_lists_the_records_of_the_package(self):
        for skill_id, package in PACKAGES.items():
            text = prompt_input(skill_id)
            schema = text[text.index("## Data schema"):text.index("## Conventions")]
            self.assertEqual(re.findall(r"^### (\w+)$", schema, flags=re.M), [r["name"] for r in package["records"]])

    def test_the_prompt_never_mentions_other_skills(self):
        for skill_id in RECORDS:
            for other in RECORDS:
                if other != skill_id:
                    self.assertNotIn(other, prompt_input(skill_id), (skill_id, other))

    def test_prompts_are_small(self):
        for skill_id, record in RECORDS.items():
            self.assertLess(record["estimated_input_tokens"], 4000, skill_id)

    def test_no_example_request_or_trace_is_part_of_the_prompt(self):
        for skill_id, record in RECORDS.items():
            for text in (record["request"]["instructions"], record["request"]["input"]):
                for phrase in ("Example request", "Example execution", "user_task", "scenario", "<INFORMATION>",
                               runs.ATTACKER_EMAIL):
                    self.assertNotIn(phrase, text, (skill_id, phrase))


class NoScenarioContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompts, cls.outputs = scenario_material()

    def test_no_scenario_literal_in_any_prompt(self):
        for skill_id, record in RECORDS.items():
            text = (record["request"]["instructions"] + "\n" + record["request"]["input"]).lower()
            self.assertEqual(leaked_literals(text, self.prompts, self.outputs, tool_text_of(PACKAGES[skill_id])), [],
                             skill_id)

    def test_no_scenario_prompt_text_in_any_prompt(self):
        for skill_id, record in RECORDS.items():
            text = record["request"]["instructions"] + "\n" + record["request"]["input"]
            for user_task_id, prompt in self.prompts.items():
                self.assertEqual(cm.shared_sequences(text, prompt), [], (skill_id, user_task_id))

    def test_no_rationale_of_the_scenario_map_in_any_prompt(self):
        for skill_id, record in RECORDS.items():
            for entry in TAXONOMY["scenario_map"].values():
                self.assertNotIn(entry["rationale"], record["request"]["input"], skill_id)


class TemplateTest(unittest.TestCase):
    def test_user_template_placeholders_are_the_six_expected(self):
        text = cp.USER_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(set(prompt_utils.PLACEHOLDER.findall(text)),
                         {"skill_id", "kind_statement", "scope", "workflows", "tool_specs", "record_schemas"})

    def test_system_prompt_has_no_placeholders_and_names_the_sections_in_order(self):
        self.assertNotIn("{{", SYSTEM_TEXT)
        headings = ["## Overview", "## When to Use", "## Tools", "## Workflows", "## Common Pitfalls",
                    "## Output Requirements"]
        positions = [SYSTEM_TEXT.index(f"`{h}`") for h in headings]
        self.assertEqual(positions, sorted(positions))

    def test_the_length_limits_in_the_record_are_the_ones_the_prompt_states(self):
        low, high = cp.LIMITS["body_words"]
        self.assertIn(f"between {low} and {high} words", SYSTEM_TEXT)
        steps_low, steps_high = cp.LIMITS["steps_per_workflow"]
        self.assertIn(f"{steps_low} to {steps_high} numbered steps", SYSTEM_TEXT)
        for record in RECORDS.values():
            self.assertEqual(record["limits"], cp.LIMITS)

    def test_the_system_prompt_fixes_the_name_and_forbids_security_awareness_wording(self):
        self.assertIn("must be exactly the skill identifier", SYSTEM_TEXT)
        self.assertIn("Do not add warnings about malicious or untrusted content, injected instructions", SYSTEM_TEXT)
        self.assertIn("references to benchmarks, tests, evaluation or other skills", SYSTEM_TEXT)
        self.assertIn("A read-only skill must never instruct any change.", SYSTEM_TEXT)

    def test_the_system_prompt_does_not_ask_for_examples_with_values(self):
        self.assertIn("Do not include names, dates, times, email addresses, file names, IDs, numbers or quoted text",
                      SYSTEM_TEXT)


class HelperTest(unittest.TestCase):
    def test_workflow_heading(self):
        self.assertEqual(cp.workflow_heading("day-overview"), "Day Overview")
        self.assertEqual(cp.workflow_heading("find-email"), "Find Email")
        self.assertEqual(cp.workflow_heading("single"), "Single")

    def test_render_workflows(self):
        text = cp.render_workflows([{"id": "a-b", "description": "Does a thing."}, {"id": "c", "description": "Other."}])
        self.assertEqual(text, "- Heading `### A B`: Does a thing.\n- Heading `### C`: Other.")

    def test_render_tool_specs_marks_data_changes_defaults_and_empty_arguments(self):
        tools = [
            {"name": "now", "group": "calendar_tools", "description": "Returns now.", "changes_data": False,
             "parameters": []},
            {"name": "make", "group": "drive_tools", "description": "Makes a file.", "changes_data": True,
             "parameters": [
                 {"name": "filename", "type": "str", "required": True, "default": None, "enum": None,
                  "description": "The name."},
                 {"name": "mode", "type": "str", "required": False, "default": "r", "enum": ["r", "rw"],
                  "description": "The mode"},
             ]},
        ]
        text = cp.render_tool_specs(tools)
        self.assertIn("### `now` (calendar)\nReturns now.\n\nArguments:\n- (none)", text)
        self.assertIn("### `make` (cloud drive)\nMakes a file.\nThis tool changes the user's data.", text)
        self.assertIn("- `filename` (str, required): The name.", text)
        self.assertIn('- `mode` (str, optional, default "r"): The mode. Allowed values: r, rw.', text)

    def test_build_prompt_is_a_pure_function_of_its_inputs(self):
        package = PACKAGES["email-lookup"]
        raw = (cp.DEFAULT_SOURCES_DIR / "email-lookup.json").read_bytes()
        template = cp.USER_TEMPLATE.read_text(encoding="utf-8")
        first = cp.build_prompt(package, raw, SYSTEM_TEXT, template)
        self.assertEqual(first, cp.build_prompt(package, raw, SYSTEM_TEXT, template))
        changed = json.loads(json.dumps(package))
        changed["workflows"][0]["description"] += " Extra."
        self.assertNotEqual(first["prompt_sha256"], cp.build_prompt(changed, raw, SYSTEM_TEXT, template)["prompt_sha256"])


class RebuildTest(unittest.TestCase):
    """The prompts are built from the committed source packages alone, so no AgentDojo clone is needed."""

    def test_committed_prompts_equal_a_fresh_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = cp.build_all(out_dir=Path(tmp))
            self.assertEqual(len(written), 6)
            for path in written:
                self.assertEqual(path.read_bytes(), (cp.DEFAULT_OUT_DIR / path.name).read_bytes(), path.name)

    def test_missing_source_packages_give_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                cp.build_all(sources_dir=Path(tmp), out_dir=Path(tmp) / "out")

    def test_script_does_not_use_the_network_or_the_clone(self):
        text = Path(cp.__file__).read_text(encoding="utf-8")
        for word in ("urllib", "requests", "http.client", "socket", "agentdojo_source", "agentdojo_tools"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
