"""
Purpose:
    Check that build_skill_sources.py builds correct per-skill source packages: one per skill of the taxonomy,
    with exactly the skill's tools (in full, as the agent saw them), the right record schemas, hash-checked
    provenance, and no scenario content of any kind.

Inputs:
    The committed src/data/skill_library/sources/ files and src/data/skill_taxonomy.json; the rebuild and
    provenance tests also need the AgentDojo clone (they are skipped, with a message, if it is missing).

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_skill_sources.py, check_skill_map.py, prompt_utils.py, agentdojo_runs.py,
    agentdojo_source.py, agentdojo_tools.py, and tests/scenario_leakage.py (shared leakage helpers).

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as source  # noqa: E402
import agentdojo_tools as tool_specs  # noqa: E402
import build_skill_sources as sources  # noqa: E402
import check_skill_map as cm  # noqa: E402
import prompt_utils  # noqa: E402
from scenario_leakage import leaked_literals, scenario_material, tool_text_of  # noqa: E402

TAXONOMY = cm.load_taxonomy()
FILES = sorted(sources.DEFAULT_OUT_DIR.glob("*.json"))
PACKAGES = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FILES}
EXPECTED_RECORDS = {
    "calendar-lookup": ["CalendarEvent"],
    "calendar-scheduling": ["CalendarEvent"],
    "email-lookup": ["Email", "EmailContact", "CalendarEvent"],
    "email-correspondence": ["Email", "EmailContact", "CalendarEvent"],
    "drive-file-management": ["CloudDriveFile"],
    "document-content": ["CloudDriveFile"],
}


def clone_available() -> bool:
    try:
        source.check_pinned_commit()
    except source.AgentDojoSourceError:
        return False
    return True


class CommittedSourcesTest(unittest.TestCase):
    def test_one_package_per_skill_in_taxonomy_order_of_ids(self):
        self.assertEqual(sorted(PACKAGES), sorted(TAXONOMY["skills"]))
        self.assertEqual(len(PACKAGES), 6)

    def test_package_keys(self):
        for skill_id, package in PACKAGES.items():
            self.assertEqual(list(package), ["skill_id", "kind", "summary", "yang", "workflows", "owned_write_tools",
                                             "tools", "records", "provenance"], skill_id)
            self.assertEqual(package["skill_id"], skill_id)

    def test_definition_fields_equal_the_taxonomy_entry(self):
        for skill_id, package in PACKAGES.items():
            skill = TAXONOMY["skills"][skill_id]
            for key in ("kind", "summary", "yang", "workflows", "owned_write_tools"):
                self.assertEqual(package[key], skill[key], (skill_id, key))

    def test_tools_are_exactly_the_skills_tools_read_tools_first(self):
        for skill_id, package in PACKAGES.items():
            skill = TAXONOMY["skills"][skill_id]
            self.assertEqual([t["name"] for t in package["tools"]], skill["read_tools"] + skill["owned_write_tools"])

    def test_changes_data_marks_exactly_the_write_tools(self):
        for skill_id, package in PACKAGES.items():
            for tool in package["tools"]:
                self.assertEqual(tool["changes_data"], tool["name"] in runs.WRITE_TOOLS, (skill_id, tool["name"]))
                self.assertEqual(tool["group"], runs.TOOL_CATEGORY[tool["name"]])

    def test_each_write_tool_appears_in_exactly_one_package(self):
        for write_tool in runs.WRITE_TOOLS:
            holders = [s for s, p in PACKAGES.items() if write_tool in [t["name"] for t in p["tools"]]]
            self.assertEqual(len(holders), 1, write_tool)

    def test_read_only_packages_contain_no_write_tool(self):
        for skill_id, package in PACKAGES.items():
            if package["kind"] == "read":
                self.assertFalse(any(t["changes_data"] for t in package["tools"]), skill_id)

    def test_tool_specs_hold_what_the_agent_saw_and_nothing_more(self):
        for package in PACKAGES.values():
            for tool in package["tools"]:
                self.assertEqual(set(tool), {"name", "group", "description", "changes_data", "parameters"})
                self.assertNotIn("None", tool["description"])
                for parameter in tool["parameters"]:
                    self.assertEqual(set(parameter), {"name", "type", "required", "default", "enum", "description"})
                    self.assertNotIn(parameter["name"], ("calendar", "inbox", "cloud_drive"))

    def test_record_schemas(self):
        for skill_id, package in PACKAGES.items():
            self.assertEqual([r["name"] for r in package["records"]], EXPECTED_RECORDS[skill_id], skill_id)
            for record in package["records"]:
                self.assertTrue(record["fields"])
                for field in record["fields"]:
                    self.assertEqual(set(field), {"name", "type", "description", "allowed_values"})

    def test_share_permission_enum_is_in_the_drive_package(self):
        tools = {t["name"]: t for t in PACKAGES["drive-file-management"]["tools"]}
        permission = {p["name"]: p for p in tools["share_file"]["parameters"]}["permission"]
        self.assertEqual(permission["enum"], ["r", "rw"])

    def test_provenance_names_the_pinned_commit_and_the_definition_hash(self):
        for skill_id, package in PACKAGES.items():
            provenance = package["provenance"]
            self.assertEqual(provenance["commit"], source.PINNED_COMMIT)
            self.assertEqual(provenance["definition_sha256"], sources.definition_hash(TAXONOMY["skills"][skill_id]))
            self.assertEqual(sorted(provenance["source_files"]), sorted(sources.SOURCE_KEYS))
            for entry in provenance["source_files"].values():
                self.assertRegex(entry["git_blob"], r"^[0-9a-f]{40}$")

    def test_definition_hash_changes_when_the_definition_changes(self):
        skill = json.loads(json.dumps(TAXONOMY["skills"]["email-lookup"]))
        before = sources.definition_hash(skill)
        skill["workflows"][0]["description"] += " Also more."
        self.assertNotEqual(sources.definition_hash(skill), before)


class NoScenarioContentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prompts, cls.outputs = scenario_material()

    def test_no_scenario_literal_in_any_package(self):
        for skill_id, package in PACKAGES.items():
            text = json.dumps(package, ensure_ascii=False).lower()
            self.assertEqual(leaked_literals(text, self.prompts, self.outputs, tool_text_of(package)), [], skill_id)

    def test_no_scenario_prompt_text_in_any_package(self):
        for skill_id, package in PACKAGES.items():
            text = json.dumps(package, ensure_ascii=False)
            for user_task_id, prompt in self.prompts.items():
                self.assertEqual(cm.shared_sequences(text, prompt), [], (skill_id, user_task_id))

    def test_no_scenario_map_or_rationale_in_any_package(self):
        for skill_id, package in PACKAGES.items():
            text = json.dumps(package, ensure_ascii=False)
            self.assertNotIn("scenario", text.lower(), skill_id)
            self.assertNotIn("user_task", text, skill_id)
            for entry in TAXONOMY["scenario_map"].values():
                self.assertNotIn(entry["rationale"], text, skill_id)

    def test_the_checks_do_catch_planted_scenario_content(self):
        package = PACKAGES["calendar-lookup"]
        text = json.dumps(package, ensure_ascii=False).lower()
        planted = text + " david.smith@bluesparrowtech.com feedback.xlsx 2024-05-19 and the networking event"
        found = leaked_literals(planted, self.prompts, self.outputs, tool_text_of(package))
        for expected in ("david.smith@bluesparrowtech.com", "feedback.xlsx", "2024-05-19", "networking event"):
            self.assertIn(expected, found)
        prompt = self.prompts["user_task_24"]
        self.assertTrue(cm.shared_sequences(text + " " + prompt, prompt))

    def test_no_injection_material_in_any_package(self):
        for package in PACKAGES.values():
            text = json.dumps(package, ensure_ascii=False)
            self.assertNotIn("<INFORMATION>", text)
            self.assertNotIn(runs.ATTACKER_EMAIL, text)


@unittest.skipUnless(clone_available(), "AgentDojo clone not available")
class RebuildFromSourceTest(unittest.TestCase):
    def test_committed_packages_equal_a_fresh_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = sources.build_all(out_dir=Path(tmp))
            self.assertEqual(len(written), 6)
            for path in written:
                self.assertEqual(path.read_bytes(), (sources.DEFAULT_OUT_DIR / path.name).read_bytes(), path.name)

    def test_the_build_never_reads_the_scenario_map(self):
        stripped = {k: v for k, v in TAXONOMY.items() if k != "scenario_map"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "taxonomy.json"
            path.write_text(json.dumps(stripped), encoding="utf-8")
            written = sources.build_all(taxonomy_path=path, out_dir=Path(tmp) / "out")
            for out in written:
                self.assertEqual(out.read_bytes(), (sources.DEFAULT_OUT_DIR / out.name).read_bytes(), out.name)

    def test_tool_specs_equal_the_ones_derived_from_agentdojo(self):
        by_name = {t["name"]: t for t in tool_specs.build_tools()}
        for package in PACKAGES.values():
            for tool in package["tools"]:
                spec = by_name[tool["name"]]
                self.assertEqual((tool["group"], tool["description"], tool["parameters"]),
                                 (spec["group"], spec["description"], spec["parameters"]), tool["name"])

    def test_recorded_blob_hashes_match_the_clone(self):
        for package in PACKAGES.values():
            for key, entry in package["provenance"]["source_files"].items():
                self.assertEqual(entry["git_blob"], source.blob_hash(key), key)
                self.assertEqual(entry["path"], source.SOURCE_FILES[key])

    def test_unknown_tool_in_a_skill_is_an_error(self):
        skill = json.loads(json.dumps(TAXONOMY["skills"]["email-lookup"]))
        skill["read_tools"].append("get_weather")
        specs = {t["name"]: t for t in tool_specs.build_tools()}
        with self.assertRaises(KeyError):
            sources.build_source("email-lookup", skill, specs)

    def test_referenced_records_are_added_after_the_requested_ones(self):
        records = prompt_utils.read_record_schemas(["Email"], include_referenced=True)
        self.assertEqual([r["name"] for r in records], ["Email", "CalendarEvent"])
        self.assertEqual([r["name"] for r in prompt_utils.read_record_schemas(["Email"])], ["Email"])

    def test_missing_record_class_is_an_error(self):
        with self.assertRaises(ValueError):
            prompt_utils.read_record_schemas(["NoSuchClass"])


class HelperTest(unittest.TestCase):
    def test_record_classes_follow_the_tool_groups_in_a_fixed_order(self):
        tools = [{"group": "drive_tools"}, {"group": "email_tools"}]
        self.assertEqual(sources.record_classes(tools), ["Email", "EmailContact", "CloudDriveFile"])
        self.assertEqual(sources.record_classes([{"group": "calendar_tools"}]), ["CalendarEvent"])

    def test_script_does_not_use_the_network(self):
        text = Path(sources.__file__).read_text(encoding="utf-8")
        for word in ("urllib", "requests", "http.client", "socket"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
