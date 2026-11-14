"""
Purpose:
    Check that build_skill_prompts.py builds correct, leak-free and reproducible skill-generation requests:
    one per scenario, with every tool listed, argument schemas for the tools the run used, the example
    execution in order, and none of the material the generator must not see (the agent's final answer and
    in-between messages, AgentDojo's system message, any injection text).

Inputs:
    The committed src/data/skill_generation/prompts/ files, src/data/skill_context/ packages and
    src/prompts/ templates; for the rebuild check also the AgentDojo clone (those tests skip without it).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_skill_prompts.py, build_skill_context.py, agentdojo_runs.py, agentdojo_source.py.

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

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as source  # noqa: E402
import build_skill_context as context  # noqa: E402
import build_skill_prompts as prompts  # noqa: E402

PROMPT_FILES = sorted(prompts.DEFAULT_OUT_DIR.glob("user_task_*.json"), key=lambda p: runs.task_number(p.stem))
PACKAGE_FILES = sorted(prompts.DEFAULT_CONTEXT_DIR.glob("user_task_*.json"), key=lambda p: runs.task_number(p.stem))


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clone_available() -> bool:
    try:
        source.check_pinned_commit()
    except source.AgentDojoSourceError:
        return False
    return True


class HelperTest(unittest.TestCase):
    def test_truncate_leaves_short_text_and_reports_cut_length(self):
        self.assertEqual(prompts.truncate("abc", 3), "abc")
        cut = prompts.truncate("abcdef", 4)
        self.assertTrue(cut.startswith("abcd"))
        self.assertIn("2 more characters", cut)

    def test_fence_is_longer_than_any_backtick_run_inside(self):
        self.assertTrue(prompts.fence("plain").startswith("```\n"))
        fenced = prompts.fence("a ```` b")
        self.assertTrue(fenced.startswith("`````\n") and fenced.endswith("\n`````"))

    def test_fill_template_is_single_pass(self):
        self.assertEqual(prompts.fill_template("{{a}} and {{b}}", {"a": "{{b}}", "b": "x"}), "{{b}} and x")

    def test_fill_template_rejects_mismatched_placeholders(self):
        with self.assertRaises(ValueError):
            prompts.fill_template("{{a}}", {"a": "1", "b": "2"})
        with self.assertRaises(ValueError):
            prompts.fill_template("{{a}} {{c}}", {"a": "1"})

    def test_describe_indents_continuation_lines_and_adds_allowed_values_sentence(self):
        self.assertEqual(prompts.describe("one\ntwo"), "one\n  two")
        self.assertEqual(prompts.describe("The status", ["a", "b"]), "The status. Allowed values: a, b.")
        self.assertEqual(prompts.describe("The status.", ["a"]), "The status. Allowed values: a.")

    def test_example_calls_pairs_results_by_call_id_and_rejects_missing_result(self):
        trace = {"user_task_id": "t", "messages": [
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"function": "f", "args": {"x": 1}, "id": "1"}, {"function": "g", "args": {}, "id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "out-g", "error": None},
            {"role": "tool", "tool_call_id": "1", "content": "out-f", "error": None},
        ]}
        calls = prompts.example_calls(trace)
        self.assertEqual([(c["function"], c["output"]) for c in calls], [("f", "out-f"), ("g", "out-g")])
        del trace["messages"][2]
        with self.assertRaises(ValueError):
            prompts.example_calls(trace)

    def test_render_example_execution_shows_errors_and_shortens_long_values(self):
        calls = [{"function": "f", "args": {"text": "x" * 1000}, "output": "y" * 2000, "error": "boom"}]
        text = prompts.render_example_execution(calls)
        self.assertIn("**Call 1:** `f(text=", text)
        self.assertIn("Error: boom", text)
        self.assertLess(len(text), 1400)
        self.assertIn("more characters", text)

    def test_canonical_hash_ignores_key_order(self):
        self.assertEqual(prompts.canonical_hash({"a": 1, "b": 2}), prompts.canonical_hash({"b": 2, "a": 1}))
        self.assertNotEqual(prompts.canonical_hash({"a": 1}), prompts.canonical_hash({"a": 2}))


class TemplateTest(unittest.TestCase):
    def test_user_template_placeholders_are_the_five_expected(self):
        text = prompts.USER_TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(
            set(prompts.PLACEHOLDER.findall(text)),
            {"example_request", "workspace_context", "tool_catalogue", "data_schema", "example_execution"},
        )

    def test_system_prompt_names_the_required_sections_in_order(self):
        text = prompts.SYSTEM_TEMPLATE.read_text(encoding="utf-8")
        headings = ["## Overview", "## When to Use", "## Tools", "## Procedure", "## Common Pitfalls",
                    "## Output Requirements"]
        positions = [text.index(f"`{h}`") for h in headings]
        self.assertEqual(positions, sorted(positions))

    def test_system_prompt_has_no_placeholders(self):
        self.assertNotIn("{{", prompts.SYSTEM_TEMPLATE.read_text(encoding="utf-8"))


class CommittedPromptsTest(unittest.TestCase):
    def test_one_prompt_per_context_package_for_25_tasks(self):
        self.assertEqual(len(PROMPT_FILES), 25)
        self.assertEqual([p.name for p in PROMPT_FILES], [p.name for p in PACKAGE_FILES])

    def test_record_keys_and_generation_settings(self):
        for path in PROMPT_FILES:
            record = load(path)
            self.assertEqual(
                list(record),
                ["user_task_id", "template_version", "prompt_sha256", "estimated_input_tokens", "source", "request"],
                path.name,
            )
            self.assertEqual(record["user_task_id"], path.stem)
            self.assertEqual(record["template_version"], "skill_safe_v1")
            request = record["request"]
            self.assertEqual(set(request), {"model", "instructions", "input", "reasoning", "max_output_tokens"})
            self.assertEqual(request["model"], "gpt-6.1-sol")
            self.assertEqual(request["reasoning"], {"effort": "high"})
            self.assertEqual(request["max_output_tokens"], 16384)

    def test_hash_matches_the_request_and_the_source_files(self):
        for path in PROMPT_FILES:
            record = load(path)
            self.assertEqual(record["prompt_sha256"], prompts.canonical_hash(record["request"]), path.name)
            package = (prompts.DEFAULT_CONTEXT_DIR / record["source"]["context_package"]).read_bytes()
            self.assertEqual(record["source"]["context_package_sha256"], hashlib.sha256(package).hexdigest())

    def test_system_prompt_is_the_template_and_user_prompt_is_fully_filled(self):
        system = prompts.SYSTEM_TEMPLATE.read_text(encoding="utf-8").strip()
        for path in PROMPT_FILES:
            request = load(path)["request"]
            self.assertEqual(request["instructions"], system)
            self.assertNotIn("{{", request["input"], path.name)
            self.assertTrue(request["input"].endswith("Return only the file."), path.name)

    def test_prompt_contains_the_task_prompt_and_context(self):
        for path in PROMPT_FILES:
            record, package = load(path), load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            text = record["request"]["input"]
            self.assertIn(package["task_prompt"], text, path.name)
            self.assertIn(package["environment"]["account_email"], text)
            self.assertIn(package["environment"]["current_day"], text)

    def test_every_prompt_lists_all_24_tools(self):
        names = [t["name"] for t in context.build_tools()] if clone_available() else None
        for path in PROMPT_FILES:
            package = load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            text = load(path)["request"]["input"]
            catalogue = text[text.index("## Tool catalogue"):text.index("## Data schema")]
            listed = re.findall(r"^(?:#### |- )`(\w+)` \((?:email|calendar|cloud drive)\)", catalogue, flags=re.M)
            self.assertEqual(sorted(listed), sorted(t["name"] for t in package["tools"]), path.name)
            self.assertEqual(len(listed), 24)
            if names:
                self.assertEqual(sorted(listed), sorted(names))

    def test_used_tools_have_full_arguments_and_unused_tools_do_not(self):
        for path in PROMPT_FILES:
            package = load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            used = {c["function"] for c in runs.tool_calls(package["execution_trace"])}
            text = load(path)["request"]["input"]
            catalogue = text[text.index("## Tool catalogue"):text.index("## Data schema")]
            full = set(re.findall(r"^#### `(\w+)`", catalogue, flags=re.M))
            self.assertEqual(full, used, path.name)
            for tool in package["tools"]:
                for parameter in tool["parameters"]:
                    present = f"- `{parameter['name']}` ({parameter['type']}," in catalogue
                    if tool["name"] in used:
                        self.assertTrue(present, f"{path.name} {tool['name']}.{parameter['name']}")
            # An unused tool's argument must not appear as an argument bullet under it.
            self.assertNotIn("Arguments:", catalogue[catalogue.index("### Other available tools"):], path.name)

    def test_enum_values_are_shown_for_share_file_permission(self):
        for path in PROMPT_FILES:
            package = load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            text = load(path)["request"]["input"]
            if "share_file" in {c["function"] for c in runs.tool_calls(package["execution_trace"])}:
                self.assertIn("Allowed values: r, rw.", text)

    def test_example_execution_has_one_block_per_tool_call_in_order(self):
        for path in PROMPT_FILES:
            package = load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            calls = runs.tool_calls(package["execution_trace"])
            text = load(path)["request"]["input"]
            execution = text[text.index("## Example execution"):]
            shown = re.findall(r"^\*\*Call (\d+):\*\* `(\w+)\(", execution, flags=re.M)
            self.assertEqual(shown, [(str(i), c["function"]) for i, c in enumerate(calls, start=1)], path.name)

    def test_long_tool_outputs_are_cut_and_nothing_exceeds_the_limit(self):
        longest = max(
            len(m["content"]) for p in PACKAGE_FILES for m in load(p)["execution_trace"]["messages"] if m["role"] == "tool"
        )
        self.assertGreater(longest, prompts.MAX_OUTPUT_CHARS)  # the check below is not vacuous
        for path in PROMPT_FILES:
            execution = load(path)["request"]["input"].split("## Example execution")[1]
            for block in re.findall(r"Output:\n\n(`{3,})\n(.*?)\n\1", execution, flags=re.S):
                body = re.sub(r"… \[cut: \d+ more characters\]$", "", block[1])
                self.assertLessEqual(len(body), prompts.MAX_OUTPUT_CHARS, path.name)

    def test_prompts_are_well_under_the_size_budget(self):
        for path in PROMPT_FILES:
            self.assertLess(load(path)["estimated_input_tokens"], 8000, path.name)

    def test_generator_is_not_shown_the_agents_own_messages(self):
        """The final answer and every in-between assistant message must be absent from the prompt."""
        checked = 0
        for path in PROMPT_FILES:
            package = load(prompts.DEFAULT_CONTEXT_DIR / path.name)
            text = load(path)["request"]["input"]
            for message in package["execution_trace"]["messages"]:
                if message["role"] == "assistant" and message["content"]:
                    checked += 1
                    self.assertNotIn(message["content"], text, path.name)
            system_message = package["execution_trace"]["messages"][0]["content"]
            self.assertNotIn(system_message[:60], text, path.name)
        self.assertGreaterEqual(checked, 25)  # at least every final answer was compared

    def test_no_injection_text_or_attacker_address_in_any_prompt(self):
        for path in PROMPT_FILES:
            request = load(path)["request"]
            for text in (request["instructions"], request["input"]):
                self.assertNotIn("<INFORMATION>", text, path.name)
                self.assertNotIn(runs.ATTACKER_EMAIL, text, path.name)
                self.assertNotIn("mark.black", text, path.name)

    def test_data_schema_lists_the_four_record_types_and_their_fields(self):
        for path in PROMPT_FILES:
            text = load(path)["request"]["input"]
            schema = text[text.index("## Data schema"):text.index("## Example execution")]
            for heading in ("### Email\n", "### EmailContact\n", "### CalendarEvent\n", "### CloudDriveFile\n"):
                self.assertIn(heading, schema, path.name)
            for field in ("`sender`", "`participants`", "`filename`", "`shared_with`"):
                self.assertIn(field, schema, path.name)
            self.assertNotIn("EmailID", schema)  # aliases are resolved to str
            self.assertNotIn("CloudDriveFileID", schema)


@unittest.skipUnless(clone_available(), "AgentDojo clone not available")
class RebuildTest(unittest.TestCase):
    def test_committed_prompts_equal_a_fresh_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = prompts.build_all(out_dir=Path(tmp))
            self.assertEqual(len(written), 25)
            for path in written:
                self.assertEqual(path.read_bytes(), (prompts.DEFAULT_OUT_DIR / path.name).read_bytes(), path.name)

    def test_types_blob_hash_matches_the_clone(self):
        for path in PROMPT_FILES:
            self.assertEqual(load(path)["source"]["types_git_blob"], source.blob_hash("tool_types"))

    def test_build_fails_without_context_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                prompts.build_all(context_dir=Path(tmp), out_dir=Path(tmp) / "out")

    def test_data_schema_fields_match_types_py(self):
        schema = prompts.data_schema()
        self.assertEqual(schema.count("\n- `"), 11 + 2 + 9 + 7)  # Email 11, EmailContact 2, CalendarEvent 9, CloudDriveFile 7


class NoNetworkTest(unittest.TestCase):
    def test_script_does_not_use_the_network(self):
        text = Path(prompts.__file__).read_text(encoding="utf-8")
        for word in ("urllib", "requests", "http.client", "socket"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
