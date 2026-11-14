"""
Purpose:
    Check the helpers in prompt_utils.py that both prompt builders use: canonical hashing, code fences,
    single-pass template filling, field descriptions and the rendering of record schemas.

Inputs:
    None for the pure helpers; the record-schema reading tests use the AgentDojo clone (skipped if missing).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/prompt_utils.py, agentdojo_source.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_source as source  # noqa: E402
import prompt_utils as pu  # noqa: E402


def clone_available() -> bool:
    try:
        source.check_pinned_commit()
    except source.AgentDojoSourceError:
        return False
    return True


class PureHelperTest(unittest.TestCase):
    def test_canonical_hash_ignores_key_order_and_detects_changes(self):
        self.assertEqual(pu.canonical_hash({"a": 1, "b": [1, 2]}), pu.canonical_hash({"b": [1, 2], "a": 1}))
        self.assertNotEqual(pu.canonical_hash({"a": 1}), pu.canonical_hash({"a": 2}))
        self.assertRegex(pu.canonical_hash({}), r"^[0-9a-f]{64}$")

    def test_canonical_hash_keeps_non_ascii_text_as_is(self):
        self.assertNotEqual(pu.canonical_hash({"a": "é"}), pu.canonical_hash({"a": "e"}))

    def test_fence_is_longer_than_any_backtick_run_inside(self):
        self.assertTrue(pu.fence("plain").startswith("```\n"))
        fenced = pu.fence("a ```` b")
        self.assertTrue(fenced.startswith("`````\n") and fenced.endswith("\n`````"))

    def test_fill_template_is_single_pass(self):
        self.assertEqual(pu.fill_template("{{a}} and {{b}}", {"a": "{{b}}", "b": "x"}), "{{b}} and x")

    def test_fill_template_rejects_mismatched_placeholders(self):
        with self.assertRaises(ValueError):
            pu.fill_template("{{a}}", {"a": "1", "b": "2"})
        with self.assertRaises(ValueError):
            pu.fill_template("{{a}} {{c}}", {"a": "1"})

    def test_describe_indents_continuation_lines_and_adds_allowed_values_as_a_sentence(self):
        self.assertEqual(pu.describe("one\ntwo"), "one\n  two")
        self.assertEqual(pu.describe("The status", ["a", "b"]), "The status. Allowed values: a, b.")
        self.assertEqual(pu.describe("The status.", ["a"]), "The status. Allowed values: a.")

    def test_render_record_schemas(self):
        records = [{"name": "Thing", "fields": [
            {"name": "id_", "type": "str", "description": "The id", "allowed_values": None},
            {"name": "state", "type": "State", "description": "The state", "allowed_values": ["on", "off"]},
        ]}, {"name": "Other", "fields": []}]
        self.assertEqual(
            pu.render_record_schemas(records),
            "### Thing\n- `id_` (str): The id\n- `state` (State): The state. Allowed values: on, off.\n\n### Other")

    def test_generation_settings_are_the_study_settings(self):
        self.assertEqual(pu.GENERATION_SETTINGS,
                         {"model": "gpt-6.1-sol", "reasoning": {"effort": "high"}, "max_output_tokens": 16384})
        self.assertNotIn("temperature", pu.GENERATION_SETTINGS)


@unittest.skipUnless(clone_available(), "AgentDojo clone not available")
class RecordSchemaTest(unittest.TestCase):
    def test_fields_types_and_enums(self):
        records = {r["name"]: r for r in pu.read_record_schemas(["Email", "CalendarEvent", "CloudDriveFile"])}
        email = {f["name"]: f for f in records["Email"]["fields"]}
        self.assertEqual(len(email), 11)
        self.assertEqual(email["id_"]["type"], "str")  # the alias EmailID is resolved
        self.assertEqual(email["status"]["allowed_values"], ["sent", "received", "draft"])
        self.assertEqual({f["name"]: f["type"] for f in records["CalendarEvent"]["fields"]}["location"], "str | None")
        shared = {f["name"]: f for f in records["CloudDriveFile"]["fields"]}["shared_with"]
        self.assertEqual(shared["allowed_values"], ["r", "rw"])

    def test_requested_order_is_kept_and_references_follow(self):
        names = [r["name"] for r in pu.read_record_schemas(["CloudDriveFile", "Email"], include_referenced=True)]
        self.assertEqual(names, ["CloudDriveFile", "Email", "CalendarEvent"])

    def test_a_missing_class_is_an_error(self):
        with self.assertRaises(ValueError):
            pu.read_record_schemas(["Nope"])


if __name__ == "__main__":
    unittest.main()
