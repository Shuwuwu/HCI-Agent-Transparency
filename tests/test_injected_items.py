"""Tests of build_injected_items.py and the committed injected-skill corpus: reproducible, built on the current skills
(the ones the skills-v4b runs used), differs from its base only by the declared block, no researcher-only wording."""

import hashlib
from collections import Counter
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

class StudyPlanTests(unittest.TestCase):
    """The study plan: strategy II only, the three wordings mixed (four items each), objectives kept."""

    def test_four_items_per_wording_one_strategy_and_each_objective_once_per_wording(self):
        self.assertEqual(Counter(i["wording"] for i in ITEMS), {"W1": 4, "W2": 4, "W3": 4})
        self.assertEqual({i["strategy"] for i in ITEMS}, {"important_instructions"})
        for wording in ("W1", "W2", "W3"):
            self.assertEqual(sorted(i["objective"] for i in ITEMS if i["wording"] == wording), ["G1", "G2", "G3", "G4"])
            areas = [i["injected_skill"] for i in ITEMS if i["wording"] == wording]
            self.assertEqual(len(areas), len(set(areas)))
        self.assertEqual(len({i["item_id"] for i in ITEMS}), 12)

    def test_objectives_and_identities_are_those_of_the_ii_w2_plan(self):
        pilot = {i["scenario"]: i for i in json.loads((bi.OUT_DIR / "pilot_w2" / "manifest.json").read_text())["items"]}
        for item in ITEMS:
            w2 = pilot[item["scenario"]]
            self.assertEqual((item["objective"], item["attacker_identity"]), (w2["objective"], w2["attacker_identity"]))
            self.assertEqual(item["base_sha256"], w2["base_sha256"])

    def test_each_wording_looks_like_its_definition_and_w3_variants_are_spread(self):
        for item in ITEMS:
            text = text_of(item)
            self.assertEqual("<INFORMATION>" in text, item["wording"] == "W1")
            self.assertEqual("**Required first step.**" in text, item["wording"] == "W2")
            self.assertEqual("variant" in item, item["wording"] == "W3")
            self.assertEqual(item["item_id"].endswith("_w3" + item.get("variant", "")), item["wording"] == "W3")
            self.assertIn("Emma Johnson", text)  # the authority claim is in every wording
        variants = Counter(i["variant"] for i in ITEMS if i["wording"] == "W3")
        self.assertEqual(set(variants), set(design.W3_VARIANT_IDS))
        self.assertLessEqual(max(variants.values()) - min(variants.values()), 1)

    def test_a_participant_list_of_four_can_hold_all_three_wordings(self):
        items = [{"scenario": i["scenario"], "area": i["injected_skill"], "difficulty": "EASY", "rank": n, "objective": i["objective"],
                  "strategy": "II", "wording": i["wording"], "identity_id": (i["attacker_identity"] or {}).get("id"),
                  **({"variant": i["variant"]} if "variant" in i else {})} for n, i in enumerate(ITEMS)]
        final = ss.choose_final_set(items, [])
        self.assertIsNotNone(final)
        by = {i["scenario"]: i for i in items}
        for lst in ("A", "B"):
            self.assertEqual({by[s]["wording"] for s in final[lst]}, {"W1", "W2", "W3"})


class TwinTests(unittest.TestCase):
    """The W3 (v3) twins of the wording pilot: same item, only the wording differs."""

    @classmethod
    def setUpClass(cls):
        cls.files, cls.manifest, cls.w2_manifest = bi.build_twins(FACTS)
        cls.w2_items = cls.w2_manifest["items"]
        cls.by_w2 = {i["paired_with"]: i for i in cls.manifest["items"]}

    def test_committed_twins_equal_a_rebuild(self):
        for root, manifest in (("pilot_w3", self.manifest), ("pilot_w2", self.w2_manifest)):
            committed = json.loads((bi.OUT_DIR / root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(committed, manifest, root)
        for relative, text in self.files.items():
            self.assertEqual((bi.OUT_DIR / relative).read_text(encoding="utf-8"), text, relative)
        on_disk = {str(p.relative_to(bi.OUT_DIR)) for r in ("pilot_w2", "pilot_w3") for p in (bi.OUT_DIR / r).rglob("SKILL.md")}
        self.assertEqual(on_disk, {p for p in self.files if p.endswith("SKILL.md")})

    def test_one_twin_per_w2_item_differing_only_in_the_wording(self):
        self.assertEqual(sorted(self.by_w2), sorted(i["item_id"] for i in self.w2_items))
        for w2 in self.w2_items:
            self.assertEqual(w2["wording"], "W2")
            twin = self.by_w2[w2["item_id"]]
            for key in ("scenario", "objective", "objective_agentdojo_task", "strategy", "attacker_identity", "goal_text",
                        "anchor_title", "injected_skill", "base_file", "base_sha256"):
                self.assertEqual(twin[key], w2[key], (w2["item_id"], key))
            self.assertEqual((w2["wording"], twin["wording"]), ("W2", "W3"))
            self.assertEqual((twin["variant"], twin["placement"]), ("v3", "last_step"))
            self.assertTrue(twin["item_id"].endswith("_ii_w3") and twin["item_file"].startswith(
                "src/data/injected_skills/pilot_w3/items/"))

    def test_a_twin_is_the_base_skill_plus_one_sentence_in_the_same_workflow(self):
        for w2 in self.w2_items:
            twin = self.by_w2[w2["item_id"]]
            text = (ROOT / twin["item_file"]).read_text(encoding="utf-8")
            base = (ROOT / twin["base_file"]).read_text(encoding="utf-8")
            block = design.render_block("II", "W3", twin["objective"], design.identity_by_id(
                twin["attacker_identity"] and twin["attacker_identity"]["id"]), "v3")
            self.assertEqual(text.count(block), 1)
            self.assertEqual(text.replace(" " + block, "", 1), base)  # a pure insertion at the end of a step
            self.assertNotIn("Required first step", text)
            self.assertNotIn("**", text)
            # the same workflow subsection as its W2 item
            w2_text = (ROOT / w2["item_file"]).read_text(encoding="utf-8")
            heading = "### " + w2["anchor_title"]
            nxt = lambda t: t.find("\n### ", t.index(heading) + 1)  # noqa: E731
            self.assertIn(block, text[text.index(heading): nxt(text) if nxt(text) > 0 else len(text)])
            self.assertIn(w2["goal_text"], w2_text[w2_text.index(heading):])

    def test_pilot_items_are_not_study_items(self):
        self.assertFalse({t["item_id"] for t in self.manifest["items"]} & {i["item_id"] for i in ITEMS})  # twins: no id clash
        self.assertFalse(any((bi.OUT_DIR / "items").glob("*_w3")))
        # the pilot's W2 items are byte-identical to the items of the earlier II + W2 study plan (commit 4ea015b)
        # for every scenario whose study wording is still W2
        study = {i["scenario"]: i for i in ITEMS}
        for w2 in self.w2_items:
            if study[w2["scenario"]]["wording"] == "W2":
                self.assertEqual(study[w2["scenario"]]["item_sha256"], w2["item_sha256"])
                self.assertEqual(study[w2["scenario"]]["item_id"], w2["item_id"])



if __name__ == "__main__":
    unittest.main()
