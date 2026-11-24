"""Tests of build_injected_items.py and the committed injected-skill corpus: reproducible, built on the current skills
(the ones the skills-v4b runs used), differs from its base only by the declared block, no researcher-only wording."""

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import build_injected_items as bi  # noqa: E402
import injection_design as design  # noqa: E402
import select_scenarios as ss  # noqa: E402

FACTS = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
MANIFEST = json.loads((bi.OUT_DIR / "manifest.json").read_text(encoding="utf-8"))
ITEMS = MANIFEST["items"]


def text_of(entry):
    return (ROOT / entry["item_file"]).read_text(encoding="utf-8")


class InjectedItemsTests(unittest.TestCase):
    def test_committed_files_equal_a_rebuild(self):
        files, manifest = bi.build_all(FACTS)
        self.assertEqual(manifest, MANIFEST)
        for relative, text in files.items():
            self.assertEqual((bi.OUT_DIR / relative).read_text(encoding="utf-8"), text, relative)
        on_disk = {str(p.relative_to(bi.OUT_DIR)) for p in bi.OUT_DIR.glob("items/*/SKILL.md")}
        self.assertEqual(on_disk, set(files))

    def test_one_item_per_candidate_with_the_planned_design(self):
        result = ss.select(FACTS)
        self.assertEqual({i["scenario"] for i in ITEMS}, set(result["allocation"]))
        self.assertEqual(len(ITEMS), 12)
        for item in ITEMS:
            plan = result["allocation"][item["scenario"]]
            self.assertEqual((item["objective"], item["wording"], item["item_id"]),
                             (plan["objective"], plan["wording"], plan["item_id"]))
            self.assertIn(item["strategy"], ("important_instructions", "tool_knowledge"))

    def test_items_are_the_current_skill_plus_exactly_the_block(self):
        for item in ITEMS:
            base = (ROOT / item["base_file"]).read_bytes()
            self.assertEqual(hashlib.sha256(base).hexdigest(), item["base_sha256"])
            record = json.loads((ROOT / item["base_matches_v4b_run"]).read_text(encoding="utf-8"))
            recorded = next(s for s in record["skills"] if s["id"] == item["injected_skill"])
            self.assertEqual(recorded["sha256"], item["base_sha256"], "item must be built on the skill the safe run used")
            text = text_of(item)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), item["item_sha256"])
            identity = item["attacker_identity"]
            block = design.render_block({"important_instructions": "II", "tool_knowledge": "TK"}[item["strategy"]],
                                        item["wording"], item["objective"], design.identity_by_id(identity and identity["id"]),
                                        item.get("variant"))
            self.assertEqual(text.count(block.strip("\n")), 1)
            # a W1/W2 block is a paragraph after the subsection; a W3 block is prose added inside it
            inserted = ("\n\n" if item["wording"] != "W3" or item["placement"] == "lead_in" else " ") + block.strip("\n")
            self.assertEqual(design.remove_block(text, inserted).encode(), base)

    def test_no_default_attacker_no_label_leak_and_injection_is_inside_workflows(self):
        for item in ITEMS:
            text = text_of(item)
            for needle in ("mark.black", "Mark", "injection", "G1", "G2", "G3", "G4", item["item_id"], "AgentDojo"):
                self.assertNotIn(needle, text, (item["item_id"], needle))
            workflows = text.index("\n## Workflows")
            self.assertTrue(workflows < text.index(item["goal_text"]) < text.index("\n## ", workflows + 5))

    def test_attacker_identities_are_unique_across_items_and_goal_text_matches(self):
        idents = [i["attacker_identity"]["id"] for i in ITEMS if i["attacker_identity"]]
        self.assertEqual(len(idents), len(set(idents)))
        self.assertEqual(sum(i["attacker_identity"] is None for i in ITEMS), sum(i["objective"] == "G1" for i in ITEMS))
        for item in ITEMS:
            self.assertEqual(item["goal_text"], design.goal_text(item["objective"], design.identity_by_id(
                item["attacker_identity"] and item["attacker_identity"]["id"])))

    def test_old_corpus_is_archived_and_not_in_the_active_tree(self):
        self.assertTrue((ROOT / "archive/injected_skills_pre_e5e53ed").is_dir())
        self.assertEqual(len(list((ROOT / "archive/injected_skills_pre_e5e53ed").rglob("SKILL.md"))), 181)
        self.assertFalse(any((bi.OUT_DIR).glob("user_task_*")))

    def test_a_changed_base_skill_is_rejected(self):
        task = next(t for t in FACTS["tasks"] if t["scenario"] == ITEMS[0]["scenario"])
        bad = json.loads(json.dumps(task))
        bad["v4b_run"]["file"] = "src/data/execution/runs/user_task_1__no-skills__gpt-4o-2024-08-06__r1.json"
        result = ss.select(FACTS)
        with self.assertRaises((bi.ItemError, StopIteration)):
            bi.build_item(ITEMS[0]["scenario"], result["allocation"][ITEMS[0]["scenario"]],
                          result["rows"][ITEMS[0]["scenario"]], bad, ss.SEED)


if __name__ == "__main__":
    unittest.main()
