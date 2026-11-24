"""Tests of select_scenarios.py: the rules give the documented outcome, the committed CSV and document are exactly what
the pipeline generates, the item allocation and the final-set / list choice satisfy their constraints. The facts are
refreshed with AgentDojo (--collect); one test (skipped without AgentDojo) checks the committed facts are current."""

import csv
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import injection_design as design  # noqa: E402
import select_scenarios as ss  # noqa: E402

try:
    import function_calling_pi  # noqa: F401
    HAVE_AGENTDOJO = True
except Exception:
    HAVE_AGENTDOJO = False

FACTS = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
RESULT = ss.select(FACTS)
ROWS = RESULT["rows"]


def by_status(status):
    return sorted((s for s in ROWS if ROWS[s]["status"] == status), key=ss.task_number)


def synthetic_items(allocation=None):
    """The planned items of the 12 candidates as 'passing' items."""
    alloc = allocation or RESULT["allocation"]
    return [{"scenario": s, "area": ROWS[s]["area"], "difficulty": ROWS[s]["difficulty"], "rank": ROWS[s]["rank"],
             **ss.plan_fields(a)} for s, a in alloc.items()]


class RuleOutcomeTests(unittest.TestCase):
    def test_every_task_has_one_status_rule_and_reason(self):
        self.assertEqual(len(ROWS), 40)
        for s, r in ROWS.items():
            self.assertIn(r["status"], {"candidate", "spare", "excluded"}, s)
            self.assertTrue(r["rule"] and r["reason"], s)

    def test_pool_is_the_twenty_active_stimuli(self):
        pool = {s for s, r in ROWS.items() if r["in_pool"]}
        folders = {p.name.replace("__no_injection", "") for p in (ROOT / "src/data/stimuli").iterdir() if p.is_dir()}
        self.assertEqual(pool, folders)
        self.assertEqual(len(pool), 20)

    def test_rule_outcomes(self):
        excluded = {s: r["rule"] for s, r in ROWS.items() if r["status"] == "excluded"}
        by_rule = {}
        for s, rule in excluded.items():
            by_rule.setdefault(rule, []).append(ss.task_number(s))
        self.assertEqual(len(by_rule["R1"]), 20)  # 15 never mapped + 5 failed v4b runs
        self.assertEqual(sorted(by_rule["R3a"]), [4, 39])
        self.assertEqual(sorted(by_rule["R4"]), [13, 21])
        self.assertEqual(len(by_rule["R3b"]), 1)
        self.assertIn(by_rule["R3b"][0], (9, 12))
        self.assertEqual(len(by_candidates := by_status("candidate")), 12)
        self.assertEqual(len(by_status("spare")), 3)
        self.assertEqual(len(by_candidates) + len(by_status("spare")) + len(excluded), 40)

    def test_candidates_are_balanced_and_cover_every_remaining_area(self):
        cands = by_status("candidate")
        self.assertEqual(sum(ROWS[s]["difficulty"] == "EASY" for s in cands), 6)
        self.assertEqual(len({ROWS[s]["area"] for s in cands}), 5)
        self.assertTrue(all(ROWS[s]["difficulty"] in ("EASY", "MEDIUM") for s in cands))

    def test_selection_is_deterministic_and_depends_on_the_seed_only_through_rank(self):
        self.assertEqual(ss.select(FACTS)["order"], RESULT["order"])
        other = ss.select(FACTS, seed="another seed")
        self.assertEqual(set(other["order"]), set(RESULT["order"]))  # same remaining pool
        self.assertNotEqual(other["order"], RESULT["order"])

    def test_recorded_attack_success_is_not_an_input(self):
        text = ROOT.joinpath("src/scripts/select_scenarios.py").read_text(encoding="utf-8")
        for forbidden in ("gpt4o_unsafe_candidates", "important_instructions.json", "security"):
            self.assertNotIn(f'"{forbidden}"', text)
        self.assertNotIn("recorded_attack", json.dumps(FACTS))

    def test_duplicate_groups_and_sensitivity(self):
        self.assertEqual(RESULT["groups"], [["user_task_9", "user_task_12", "user_task_21"]])
        lo, hi = RESULT["threshold_range"]
        self.assertLessEqual(lo, ss.DUPLICATE_THRESHOLD)
        self.assertGreaterEqual(hi, ss.DUPLICATE_THRESHOLD)

    def test_repeated_pairs_come_from_surviving_scenarios_only(self):
        self.assertEqual(RESULT["pairs"], [("user_task_1", "user_task_10"), ("user_task_26", "user_task_35")])
        for s in ROWS:
            if ROWS[s]["status"] == "excluded":
                self.assertEqual(ROWS[s]["repeated"], "", s)

    def test_compatibility_follows_the_resource_rule(self):
        for s in by_status("candidate") + by_status("spare"):
            for obj in ROWS[s]["compatible"]:
                self.assertTrue(design.OBJECTIVES[obj]["resource"] not in
                                {design.TOOL_RESOURCE[t] for t in
                                 next(t for t in FACTS["tasks"] if t["scenario"] == s)["safe_write_tools"]}, (s, obj))
        self.assertNotIn("G4", ROWS["user_task_16"]["compatible"])
        self.assertNotIn("G2", ROWS["user_task_8"]["compatible"])
        self.assertNotIn("G1", ROWS["user_task_35"]["compatible"])


class AllocationTests(unittest.TestCase):
    def test_every_candidate_has_a_compatible_planned_item(self):
        alloc = RESULT["allocation"]
        self.assertEqual(set(alloc), set(by_status("candidate")))
        for s, a in alloc.items():
            self.assertIn(a["objective"], ROWS[s]["compatible"])
            self.assertEqual(a["item_id"], design.item_id(s, a["objective"], a["strategy"], a["wording"], a.get("variant")))
        for obj in design.OBJECTIVE_IDS:
            self.assertEqual(sum(a["objective"] == obj for a in alloc.values()), 3)
        idents = [a["identity_id"] for a in alloc.values() if a["identity_id"]]
        self.assertEqual(len(idents), len(set(idents)))

    def test_all_twelve_passing_gives_a_valid_final_set(self):
        for strict in (True, False):
            final = ss.choose_final_set(synthetic_items(), RESULT["pairs"], strict)
            self.assertIsNotNone(final, strict)
            self.assertEqual(len(final["scenarios"]), 8)
            self.assertEqual(sorted(final["A"] + final["B"], key=ss.task_number), final["scenarios"])
            self.assertFalse({"user_task_1", "user_task_10"} <= set(final["scenarios"]))
            self.assertFalse({"user_task_26", "user_task_35"} <= set(final["scenarios"]))
            items = {i["scenario"]: i for i in synthetic_items()}
            for names in (final["A"], final["B"]):
                self.assertTrue(design.is_valid_unsafe_list([items[n] for n in names], strict))

    def test_final_set_choice_is_deterministic_and_none_when_infeasible(self):
        a = ss.choose_final_set(synthetic_items(), RESULT["pairs"])
        self.assertEqual(a, ss.choose_final_set(list(reversed(synthetic_items())), RESULT["pairs"]))
        no_g1 = [i for i in synthetic_items() if i["objective"] != "G1"]
        self.assertIsNone(ss.choose_final_set(no_g1, RESULT["pairs"]))
        one_g2 = [i for i in synthetic_items() if i["objective"] != "G2"] + [
            next(i for i in synthetic_items() if i["objective"] == "G2")]
        self.assertIsNone(ss.choose_final_set(one_g2, RESULT["pairs"]))  # a list needs two items per objective overall

    def test_attrition_table_is_monotone_and_relaxed_is_not_worse(self):
        strict = ss.feasibility_by_attrition(RESULT["allocation"], ROWS, RESULT["pairs"])
        relaxed = ss.feasibility_by_attrition(RESULT["allocation"], ROWS, RESULT["pairs"], strict=False)
        p = [r["probability"] for r in strict]
        self.assertEqual(p, sorted(p))
        for a, b in zip(strict, relaxed):
            self.assertLessEqual(a["probability"], b["probability"] + 1e-12)
            self.assertTrue(0 <= a["probability"] <= 1)


class ArtifactTests(unittest.TestCase):
    def test_committed_csv_and_document_are_what_the_pipeline_generates(self):
        evaluation = (json.loads(ss.EVALUATION.read_text(encoding="utf-8")) if ss.EVALUATION.exists() else None)
        csv_text, doc_text = ss.build(FACTS, evaluation)
        self.assertEqual(ss.CSV_PATH.read_text(encoding="utf-8"), csv_text,
                         "src/data/scenario_selection.csv is stale: run python3 -B src/scripts/select_scenarios.py")
        self.assertEqual(ss.DOC_PATH.read_text(encoding="utf-8"), doc_text,
                         "docs/scenario_selection.md is stale: run python3 -B src/scripts/select_scenarios.py")

    def test_csv_has_one_row_per_task_and_the_required_columns(self):
        rows = list(csv.DictReader(io.StringIO(ss.CSV_PATH.read_text(encoding="utf-8"))))
        self.assertEqual(len(rows), 40)
        self.assertEqual(list(rows[0]), ss.COLUMNS)
        for column in ("task_category", "difficulty", "selection_status", "attacker_objective", "rationale", "rule_id"):
            self.assertIn(column, rows[0])
        self.assertTrue(all(r["selection_status"] and r["rule_id"] and r["rationale"] for r in rows))
        planned = [r for r in rows if r["selection_status"] == "candidate"]
        self.assertTrue(all(r["attacker_objective"] in design.OBJECTIVE_IDS and r["wording"] in design.WORDINGS
                            and r["strategy"] in design.STRATEGY_NAMES.values() for r in planned))
        self.assertTrue(all(not r["attacker_objective"] for r in rows if r["selection_status"] != "candidate"))

    def test_document_states_the_decisions_it_must(self):
        doc = ss.DOC_PATH.read_text(encoding="utf-8")
        for needle in ("our adaptation", "not an AgentDojo or SkillJect template", "G0", "ambiguous",
                       "before any written criteria existed", "No independent reviewer", "tool_knowledge",
                       "important_instructions", "may have influenced the picks"):
            self.assertIn(needle, doc)
        self.assertNotIn("{{", doc)

    def test_stage_two_fills_the_columns_from_evaluations(self):
        evaluation = {"evaluations": [{"item_id": a["item_id"], "included": True} for a in RESULT["allocation"].values()]}
        csv_text, doc_text = ss.build(FACTS, evaluation)
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        final = [r for r in rows if r["final_set"] == "yes"]
        self.assertEqual(len(final), 8)
        self.assertEqual(sorted(r["participant_list"] for r in final), ["A"] * 4 + ["B"] * 4)
        self.assertTrue(all(r["attempts"] == "1" and r["passing_attempts"] == "1" for r in rows
                            if r["selection_status"] == "candidate"))
        self.assertIn("final scenarios", doc_text)


@unittest.skipUnless(HAVE_AGENTDOJO, "needs AgentDojo (.venv-agentdojo/bin/python)")
class FactsAreCurrentTests(unittest.TestCase):
    def test_committed_facts_equal_a_fresh_collection(self):
        self.assertEqual(ss.collect_facts(), FACTS)


if __name__ == "__main__":
    unittest.main()
