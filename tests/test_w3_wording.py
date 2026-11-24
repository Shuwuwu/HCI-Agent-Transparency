"""Tests of the W3 (contextual skill instructions) wording: the text keeps the strategy's defining content and none of
the W1/W2 cues, placement is a reversible pure insertion into an existing workflow, allocation spreads wordings and
variants, and W1/W2 plans and the committed items are unchanged. No model is called."""

import copy
import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import build_injected_items as bi  # noqa: E402
import injection_design as design  # noqa: E402
import select_scenarios as ss  # noqa: E402

FACTS = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
SKILLS = sorted(p.parent.name for p in (ROOT / "src/data/skills").glob("*/SKILL.md"))
CUES = ("<", ">", "**", "#", "INFORMATION", "Required first step", "Signed", "important message", "AgentDojo")


def identity_for(objective):
    return design.IDENTITY_POOL[0] if design.OBJECTIVES[objective]["needs_identity"] else None


def w3(strategy, variant, objective="G2"):
    return design.render_block(strategy, "W3", objective, identity_for(objective), variant)


class RenderTests(unittest.TestCase):
    def test_every_cell_contains_the_goal_and_no_w1_w2_cue(self):
        for strategy in design.STRATEGIES:
            for variant in design.W3_VARIANT_IDS:
                for objective in design.OBJECTIVE_IDS:
                    text = w3(strategy, variant, objective)
                    self.assertIn(design.goal_text(objective, identity_for(objective)), text)
                    for cue in CUES:
                        self.assertNotIn(cue, text, (strategy, variant, objective, cue))
                    self.assertNotIn("\n", text)  # one paragraph of prose
                    self.assertNotIn("Mark Black", text)

    def test_tool_knowledge_names_the_tool_sequence_and_important_instructions_does_not(self):
        for variant in design.W3_VARIANT_IDS:
            for objective in design.OBJECTIVE_IDS:
                ident = identity_for(objective)
                tools = design.tool_sequence_text(design.placeholder_calls(objective, ident)).strip()
                self.assertIn(tools, w3("TK", variant, objective))
                self.assertNotIn("You should use the tool", w3("II", variant, objective))
                self.assertIn("'$'", w3("TK", variant, objective))  # the placeholder remark of tool_knowledge

    def test_both_strategies_state_the_users_authority_and_the_order(self):
        for strategy in design.STRATEGIES:
            for variant in design.W3_VARIANT_IDS:
                text = w3(strategy, variant)
                self.assertIn(design.USER_NAME, text)  # claimed authority of the user
                self.assertRegex(text, r"(?i)\bbefore\b")  # do it before the task
                self.assertRegex(text, r"(?i)\b(then|once)\b")  # and then the task

    def test_variants_differ_in_wording_and_placement(self):
        for strategy in design.STRATEGIES:
            self.assertEqual(len({w3(strategy, v) for v in design.W3_VARIANT_IDS}), len(design.W3_VARIANT_IDS))
        self.assertEqual(len({v["placement"] for v in design.W3_VARIANTS.values()}), len(design.W3_VARIANTS))

    def test_variant_is_required_for_w3_and_refused_otherwise(self):
        with self.assertRaises(ValueError):
            design.render_block("II", "W3", "G1", None)
        with self.assertRaises(ValueError):
            design.render_block("II", "W3", "G1", None, "v9")
        for wording in ("W1", "W2"):
            with self.assertRaises(ValueError):
                design.render_block("II", wording, "G1", None, "v1")

    def test_w1_and_w2_are_unchanged(self):
        self.assertEqual(design.WORDINGS, ("W1", "W2", "W3"))  # the study plan includes W3
        self.assertEqual(design.LEGACY_WORDINGS, ("W1", "W2"))
        self.assertEqual(design.COMBINATIONS, tuple((s, w) for s in ("II", "TK") for w in ("W1", "W2", "W3")))
        self.assertTrue(design.render_block("II", "W1", "G1", None).startswith("\n\n\n<INFORMATION>"))
        self.assertTrue(design.render_block("TK", "W2", "G1", None).startswith("**Required first step.**"))


class PlacementTests(unittest.TestCase):
    def test_pure_insertion_inside_one_workflow_for_every_skill_variant_and_strategy(self):
        for skill in SKILLS:
            base = (ROOT / "src/data/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            workflows = base.index("\n## Workflows")
            after = base.index("\n## ", workflows + 5)
            for variant in design.W3_VARIANT_IDS:
                for strategy in design.STRATEGIES:
                    block = w3(strategy, variant)
                    text, inserted, title, offset = design.place_w3(base, block, variant, "seed", f"{skill}-{strategy}")
                    self.assertEqual(design.remove_block(text, inserted), base, (skill, variant))
                    self.assertEqual(text.count(block), 1)
                    self.assertTrue(workflows < offset < after)
                    self.assertNotIn("\n#", inserted)  # no new heading
                    self.assertEqual(title, design.pick_anchor(base, "seed", f"{skill}-{strategy}")[0])  # same subsection as W1/W2

    def test_each_placement_lands_where_it_says(self):
        base = (ROOT / "src/data/skills/calendar-lookup/SKILL.md").read_text(encoding="utf-8")
        for variant, spec in design.W3_VARIANTS.items():
            text, inserted, title, offset = design.place_w3(base, w3("II", variant), variant, "seed", "a")
            line = next(l for l in text.splitlines() if w3("II", variant) in l)
            if spec["placement"] == "lead_in":
                self.assertEqual(line, w3("II", variant))  # its own paragraph, before the first step
                self.assertTrue(text.index(line) < text.index("\n1. ", text.index("### " + title)))
            else:
                self.assertRegex(line, r"^\d+\. ")  # added to a numbered step, which keeps its own text
                self.assertTrue(line.endswith(w3("II", variant)))
                self.assertIn(line[: line.index(w3("II", variant))].rstrip(), base)
        steps = [design.W3_VARIANTS[v]["placement"] for v in design.W3_VARIANT_IDS]
        self.assertEqual(set(steps), {"lead_in", "first_step", "last_step"})

    def test_a_workflow_without_numbered_steps_is_refused_for_step_placements(self):
        base = "## Workflows\n\n### Only A Note\n\nNo steps here.\n\n## Other\n"
        with self.assertRaises(ValueError):
            design.place_w3(base, "text", "v2", "seed", "a")
        design.place_w3(base, "text", "v1", "seed", "a")  # a lead-in needs no step

    def test_subsections_match_the_existing_anchors(self):
        for skill in SKILLS:
            base = (ROOT / "src/data/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertEqual([(s["title"], s["end"]) for s in design.workflow_subsections(base)], design.workflow_anchors(base))


ALL = {"wordings": design.WORDINGS, "strategies": design.STRATEGIES}  # every implemented combination (not the study plan)


class AllocationTests(unittest.TestCase):
    def cands(self):
        result = ss.select(FACTS)
        return [{"scenario": s, "area": result["rows"][s]["area"], "compatible": result["rows"][s]["compatible"]}
                for s in result["order"][:12]]

    def test_the_first_two_wording_plan_is_still_available_and_has_no_w3_or_variant(self):
        plan = design.allocate_items(self.cands(), "seed", design.LEGACY_WORDINGS, design.STRATEGIES)
        self.assertTrue(all(p["wording"] in ("W1", "W2") and "variant" not in p for p in plan.values()))
        for combo in tuple((s, w) for s in design.STRATEGIES for w in design.LEGACY_WORDINGS):
            self.assertEqual(sum((p["strategy"], p["wording"]) == combo for p in plan.values()), 3)
        self.assertEqual(plan, design.allocate_items(self.cands(), "seed", design.LEGACY_WORDINGS, design.STRATEGIES))

    def test_the_default_allocation_is_the_study_plan_ii_with_w2_only(self):
        plan = design.allocate_items(self.cands(), "seed")
        self.assertEqual(plan, design.allocate_items(self.cands(), "seed", design.STUDY_WORDINGS, design.STUDY_STRATEGIES))
        self.assertEqual((design.STUDY_STRATEGIES, design.STUDY_WORDINGS), (("II",), ("W2",)))
        self.assertEqual(Counter((p["strategy"], p["wording"]) for p in plan.values()), {("II", "W2"): 12})
        self.assertFalse(any("variant" in p for p in plan.values()))
        for obj in design.OBJECTIVE_IDS:  # the objective is still balanced, three items each
            self.assertEqual(sum(p["objective"] == obj for p in plan.values()), 3)
        area = {c["scenario"]: c["area"] for c in self.cands()}
        for obj in design.OBJECTIVE_IDS:  # scenarios sharing an objective still have different areas
            areas = [area[s] for s, p in plan.items() if p["objective"] == obj]
            self.assertEqual(len(areas), len(set(areas)))
        self.assertEqual(plan, design.allocate_items(self.cands(), "seed"))

    def test_w3_allocation_is_balanced_deterministic_and_keeps_every_constraint(self):
        cands = self.cands()
        plan = design.allocate_items(cands, "seed", **ALL)
        self.assertEqual(plan, design.allocate_items(cands, "seed", **ALL))
        for obj in design.OBJECTIVE_IDS:
            self.assertEqual(sum(p["objective"] == obj for p in plan.values()), 3)
        for combo in design.COMBINATIONS:
            self.assertEqual(sum((p["strategy"], p["wording"]) == combo for p in plan.values()), 2)
        self.assertEqual(len({(p["objective"], p["strategy"], p["wording"]) for p in plan.values()}), 12)
        idents = [p["identity_id"] for p in plan.values() if p["identity_id"]]
        self.assertEqual(len(idents), len(set(idents)))
        area = {c["scenario"]: c["area"] for c in cands}
        for key in (lambda p: p["objective"], lambda p: (p["strategy"], p["wording"])):
            groups = {}
            for s, p in plan.items():
                groups.setdefault(key(p), []).append(area[s])
            for areas in groups.values():
                self.assertEqual(len(areas), len(set(areas)))

    def test_variants_belong_to_w3_items_and_are_spread_evenly(self):
        for seed in ("seed", "other", "third"):
            plan = design.allocate_items(self.cands(), seed, **ALL)
            self.assertTrue(all(("variant" in p) == (p["wording"] == "W3") for p in plan.values()))
            counts = Counter(p["variant"] for p in plan.values() if "variant" in p)
            self.assertEqual(sum(counts.values()), 4)
            self.assertEqual(set(counts), set(design.W3_VARIANT_IDS))
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_wordings_must_be_known_and_divide_the_candidates(self):
        with self.assertRaises(ValueError):
            design.allocate_items(self.cands(), "seed", ("W1", "W4"))
        with self.assertRaises(ValueError):
            design.allocate_items(self.cands()[:8], "seed", **ALL)  # 8 items, 6 combinations

    def test_a_list_may_not_repeat_a_w3_variant(self):
        def item(o, s, w, a, i, v=None):
            return {"objective": o, "strategy": s, "wording": w, "area": a, "identity_id": i, **({"variant": v} if v else {})}
        ok = [item("G1", "II", "W3", "a", None, "v1"), item("G2", "TK", "W3", "b", "id01", "v2"),
              item("G3", "TK", "W1", "c", "id02"), item("G4", "II", "W2", "d", "id03")]
        self.assertTrue(design.is_valid_unsafe_list(ok, **ALL))
        repeated = copy.deepcopy(ok)
        repeated[1]["variant"] = "v1"
        self.assertFalse(design.is_valid_unsafe_list(repeated, **ALL))
        missing = copy.deepcopy(ok)
        del missing[1]["variant"]
        self.assertFalse(design.is_valid_unsafe_list(missing, **ALL))

    def test_final_set_and_participant_lists_accept_w3_and_keep_variants_distinct(self):
        result = ss.select(FACTS, **ALL)
        passing = [{"scenario": s, "area": result["rows"][s]["area"], "difficulty": result["rows"][s]["difficulty"],
                    "rank": result["rows"][s]["rank"], **ss.plan_fields(a)} for s, a in result["allocation"].items()]
        final = ss.choose_final_set(passing, result["pairs"], **ALL)
        self.assertIsNotNone(final)
        by_scenario = {p["scenario"]: p for p in passing}
        for lst in ("A", "B"):
            self.assertTrue(design.is_valid_unsafe_list([by_scenario[s] for s in final[lst]], **ALL))
        self.assertTrue(ss.balanced_combinations([by_scenario[s] for s in final["scenarios"]], **ALL))

    def test_balanced_combinations_still_means_each_twice_without_w3(self):
        items = [{"strategy": s, "wording": w} for s, w in design.COMBINATIONS for _ in range(2)]
        self.assertTrue(ss.balanced_combinations(items, **ALL))
        items[0] = {"strategy": "TK", "wording": "W2"}
        self.assertFalse(ss.balanced_combinations(items, **ALL))
        study = [{"strategy": "II", "wording": "W2"} for _ in range(8)]  # the study plan: one combination, all 8
        self.assertTrue(ss.balanced_combinations(study))


class ItemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files, cls.manifest = bi.build_all(FACTS, **ALL)
        cls.w3_items = [i for i in cls.manifest["items"] if i["wording"] == "W3"]

    def test_w3_items_are_built_with_their_variant_and_placement(self):
        self.assertEqual(len(self.w3_items), 4)
        for item in self.w3_items:
            self.assertEqual(item["placement"], design.W3_VARIANTS[item["variant"]]["placement"])
            self.assertTrue(item["item_id"].endswith("_w3"))
        for item in self.manifest["items"]:
            self.assertEqual("variant" in item, item["wording"] == "W3")
        self.assertIn("W3", self.manifest["wording_note"])  # the note names W3 as implemented but unused in the study plan

    def test_w3_items_equal_the_current_skill_plus_the_block_and_are_in_a_workflow(self):
        for item in self.w3_items:
            text = self.files[f"items/{item['item_id']}/SKILL.md"]
            base = (ROOT / item["base_file"]).read_text(encoding="utf-8")
            strategy = {"important_instructions": "II", "tool_knowledge": "TK"}[item["strategy"]]
            ident = item["attacker_identity"]
            block = design.render_block(strategy, "W3", item["objective"], design.identity_by_id(ident and ident["id"]),
                                        item["variant"])
            self.assertEqual(text.count(block), 1)
            self.assertEqual(text.replace(" " + block, "").replace("\n\n" + block, ""), base)
            workflows = text.index("\n## Workflows")
            self.assertTrue(workflows < text.index(item["goal_text"]) < text.index("\n## ", workflows + 5))
            for needle in ("mark.black", "injection", "G1", "G2", "G3", "G4", item["item_id"], "AgentDojo", "<INFORMATION>",
                           "Required first step"):
                self.assertNotIn(needle, text, (item["item_id"], needle))

    def test_w3_skills_add_no_formatting_the_base_skill_lacks(self):
        for item in self.w3_items:
            text = self.files[f"items/{item['item_id']}/SKILL.md"]
            base = (ROOT / item["base_file"]).read_text(encoding="utf-8")
            self.assertEqual(text.count("**"), base.count("**"))
            self.assertEqual(len(re.findall(r"^#+ ", text, re.M)), len(re.findall(r"^#+ ", base, re.M)))

    def test_the_all_implemented_plan_is_balanced_over_six_combinations(self):
        items = self.manifest["items"]
        self.assertEqual(Counter(i["wording"] for i in items), {"W1": 4, "W2": 4, "W3": 4})
        self.assertEqual(Counter((i["strategy"], i["wording"]) for i in items), {c: 2 for c in [
            ("important_instructions", w) for w in design.WORDINGS] + [("tool_knowledge", w) for w in design.WORDINGS]})
        for wording in design.WORDINGS:  # each wording spans several objectives (no wording is tied to one goal)
            self.assertGreaterEqual(len({i["objective"] for i in items if i["wording"] == wording}), 3)
        counts = Counter(i["variant"] for i in items if i["wording"] == "W3")
        self.assertEqual(set(counts), set(design.W3_VARIANT_IDS))
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_the_first_two_wording_plan_can_still_be_rebuilt(self):
        files, manifest = bi.build_all(FACTS, design.LEGACY_WORDINGS, design.STRATEGIES)
        self.assertEqual({i["wording"] for i in manifest["items"]}, {"W1", "W2"})
        self.assertFalse(any("variant" in i for i in manifest["items"]))
        self.assertEqual(len(files), 12)  # the item files; main() adds the manifest

if __name__ == "__main__":
    unittest.main()
