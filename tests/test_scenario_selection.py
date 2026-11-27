"""Tests of select_scenarios.py: the rules give the documented outcome, the committed CSV and document are exactly what
the pipeline generates, the item allocation and the final-set / list choice satisfy their constraints, and stage 2
passes an item only when its simulated record passed the inclusion checks and its stimulus is manually reviewed (from a
current review record). The facts are refreshed with AgentDojo (--collect); one test (skipped without AgentDojo)
checks the committed facts are current."""

import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import injection_design as design  # noqa: E402
import review_stimuli as rv  # noqa: E402
import select_scenarios as ss  # noqa: E402

try:
    import function_calling_pi  # noqa: F401
    HAVE_AGENTDOJO = True
except Exception:
    HAVE_AGENTDOJO = False

FACTS = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
RESULT = ss.select(FACTS)
STUDY = ss.study_plan(FACTS)  # R7: task 24 withdrawn (study corpus "exclude") and replaced
ROWS = RESULT["rows"]


def by_status(status):
    return sorted((s for s in ROWS if ROWS[s]["status"] == status), key=ss.task_number)


def verdicts(status="reviewed", included=True, result=RESULT, **overrides):
    """Synthetic stage-2 input for the 12 items of `result` (default: the planned items; STUDY: the study items);
    overrides: {scenario: (included, review_status)}."""
    out = {}
    for s, a in ss.study_items(result).items():
        inc, st = overrides.get(s, (included, status))
        out[a["item_id"]] = {"included": inc, "review_status": st}
    return {"items": out}


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

    def test_review_sensitivity_agrees_with_stage_two(self):
        strict = ss.review_sensitivity(RESULT["allocation"], ROWS, RESULT["pairs"])
        relaxed = ss.review_sensitivity(RESULT["allocation"], ROWS, RESULT["pairs"], strict=False)
        self.assertEqual(strict["patterns"], 2 ** 12)
        self.assertTrue(0 < strict["feasible"] <= relaxed["feasible"] <= strict["patterns"])
        self.assertTrue(set(relaxed["essential"]) <= set(strict["essential"]))
        for s in RESULT["allocation"]:  # an essential item is exactly one whose rejection alone leaves no final set
            final = ss.stage2(RESULT, verdicts(**{s: (True, "rejected")}))["final"]
            self.assertEqual(final is None, s in strict["essential"], s)


class ArtifactTests(unittest.TestCase):
    def test_committed_csv_and_document_are_what_the_pipeline_generates(self):
        csv_text, doc_text = ss.build(FACTS, ss.load_verdicts())
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
                       "important_instructions", "may have influenced the picks", "constructed (simulated) traces",
                       "carried out the user's task but never", "The design is fragile"):
            self.assertIn(needle, doc)
        self.assertNotIn("{{", doc)

    def test_stage_two_fills_the_columns_when_every_item_is_reviewed(self):
        csv_text, doc_text = ss.build(FACTS, verdicts(result=STUDY))
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        final = [r for r in rows if r["final_set"] == "yes"]
        self.assertEqual(len(final), 8)
        self.assertEqual(sorted(r["participant_list"] for r in final), ["A"] * 4 + ["B"] * 4)
        study = [r for r in rows if r["scenario"] in ss.study_items(STUDY)]
        self.assertEqual(len(study), 12)
        self.assertTrue(all(r["simulated_included"] == "yes" and r["review_status"] == "reviewed"
                            and r["stage2_status"] == "passed" for r in study))
        withdrawn = next(r for r in rows if r["scenario"] == "user_task_24")
        self.assertEqual((withdrawn["replaced_by"], withdrawn["final_set"]), ("user_task_17", ""))
        self.assertIn("Final scenarios", doc_text)


class ReplacementTests(unittest.TestCase):
    """R7: a candidate withdrawn after manual review (its safe stimulus excluded by the study corpus) is replaced by
    the first spare that can take over its planned item and keep a valid final set; stage 1 is unchanged."""

    def test_task_24_is_withdrawn_and_task_17_takes_over_its_planned_item(self):
        self.assertEqual(set(STUDY["withdrawn"]), {"user_task_24"})
        (r,) = STUDY["replacements"]
        self.assertEqual((r["withdrawn"], r["replacement"], r["item_id"]),
                         ("user_task_24", "user_task_17", "user_task_17__g2_ii_w3v1"))
        old, new = RESULT["allocation"]["user_task_24"], ss.study_items(STUDY)["user_task_17"]
        self.assertEqual(ss.plan_fields(new), ss.plan_fields(old))
        self.assertEqual(new["anchor_item_id"], old["item_id"])
        self.assertEqual(list(ss.study_items(STUDY)), [("user_task_17" if s == "user_task_24" else s)
                                                        for s in RESULT["allocation"]])

    def test_the_first_spare_is_skipped_when_it_leaves_no_final_set(self):
        self.assertEqual(RESULT["spares"][0], "user_task_10")  # compatible with G2, but no valid final set
        self.assertIn("G2", ROWS["user_task_10"]["compatible"])
        trial = {("user_task_10" if s == "user_task_24" else s): a for s, a in RESULT["allocation"].items()}
        items = [{"scenario": s, "area": ROWS[s]["area"], "difficulty": ROWS[s]["difficulty"], "rank": ROWS[s]["rank"],
                  **ss.plan_fields(a)} for s, a in trial.items()]
        self.assertIsNone(ss.choose_final_set(items, RESULT["pairs"], True, ss.planned_wordings(trial), True,
                                              ss.planned_strategies(trial)))

    def test_nothing_withdrawn_leaves_the_plan_and_no_spare_fails_loudly(self):
        none = ss.replace_withdrawn(RESULT, {})
        self.assertEqual((none["study_allocation"], none["replacements"]), (RESULT["allocation"], []))
        with self.assertRaises(ValueError):  # every spare withdrawn too
            ss.replace_withdrawn(RESULT, {s: "x" for s in ["user_task_24", *RESULT["spares"]]})

    def test_withdrawn_candidates_are_the_excluded_safe_stimuli(self):
        self.assertEqual(set(ss.withdrawn_candidates()), {"user_task_24"})

    def test_stage_one_columns_are_unchanged_by_a_replacement(self):
        stage1 = ("scenario", "selection_status", "rule_id", "planned_item_id", "attacker_objective", "wording",
                  "attacker_identity", "seed_rank", "rationale")
        with_r7 = list(csv.DictReader(io.StringIO(ss.render_csv(ss.csv_rows(STUDY, ss.stage2(STUDY, None))))))
        without = list(csv.DictReader(io.StringIO(ss.render_csv(ss.csv_rows(RESULT, ss.stage2(RESULT, None))))))
        self.assertEqual([[r[c] for c in stage1] for r in with_r7], [[r[c] for c in stage1] for r in without])


class Stage2Tests(unittest.TestCase):
    def test_an_item_passes_only_when_included_and_reviewed(self):
        self.assertIsNone(ss.stage2(RESULT, verdicts("unreviewed"))["final"])
        st2 = ss.stage2(RESULT, verdicts(user_task_1=(True, "rejected"), user_task_2=(True, "stale"),
                                         user_task_8=(False, "reviewed")))
        self.assertFalse(any(st2["info"][s]["passing"] for s in ("user_task_1", "user_task_2", "user_task_8")))
        self.assertEqual(ss.stage2_status(st2["info"]["user_task_1"]), "rejected in review")
        self.assertEqual(ss.stage2_status(st2["info"]["user_task_2"]), "review out of date")
        self.assertEqual(ss.stage2_status(st2["info"]["user_task_8"]), "failed inclusion checks")
        self.assertEqual(ss.stage2_status(st2["info"]["user_task_9"]), "passed")

    def test_a_rejected_item_never_enters_the_final_set(self):
        final = ss.stage2(RESULT, verdicts(user_task_24=(True, "rejected")))["final"]
        if final:
            self.assertNotIn("user_task_24", final["scenarios"])

    def test_items_must_match_the_plan(self):
        v = verdicts()
        v["items"]["user_task_0__g1_ii_w1"] = {"included": True, "review_status": "reviewed"}
        with self.assertRaises(ss.Stage2Error):
            ss.stage2(RESULT, v)

    def test_committed_inputs_cover_the_study_items_and_the_withdrawn_one(self):
        v = ss.load_verdicts()
        self.assertEqual(set(v["items"]), {a["item_id"] for a in ss.study_items(STUDY).values()}
                         | {"user_task_24__g2_ii_w3v1"})
        self.assertTrue(all(i["included"] for i in v["items"].values()))
        self.assertLessEqual({i["review_status"] for i in v["items"].values()}, set(ss.STAGE2_STATUS))
        ss.stage2(STUDY, v)  # the withdrawn item is accepted and ignored

    def test_the_real_injected_runs_are_not_a_stage_two_input(self):
        self.assertFalse(hasattr(ss, "EVALUATION"))

    def test_a_changed_record_or_a_stale_review_stops_stage_two(self):
        manifest = json.loads(ss.SIMULATED_MANIFEST.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            changed = json.loads(json.dumps(manifest))
            changed["items"][0]["record"]["sha256"] = "0" * 64
            (Path(tmp) / "manifest.json").write_text(json.dumps(changed))
            with self.assertRaises(ss.Stage2Error):
                ss.load_verdicts(Path(tmp) / "manifest.json")
            review = json.loads((ROOT / "src/data/simulated/review.json").read_text(encoding="utf-8"))
            first = next(iter(review["stimuli"]))
            # Claimed without a recorded manual review: independent of whether the committed record has one.
            review["stimuli"][first]["status"] = "reviewed"
            review["stimuli"][first]["manual"] = rv.empty_manual()
            (Path(tmp) / "review.json").write_text(json.dumps(review))
            config = json.loads(ss.SIMULATED_CORPUS.read_text(encoding="utf-8"))
            config["review_file"] = str(Path(tmp) / "review.json")
            (Path(tmp) / "corpus.json").write_text(json.dumps(config))
            with self.assertRaises(ss.Stage2Error):
                ss.load_verdicts(corpus_path=Path(tmp) / "corpus.json")


@unittest.skipUnless(HAVE_AGENTDOJO, "needs AgentDojo (.venv-agentdojo/bin/python)")
class FactsAreCurrentTests(unittest.TestCase):
    def test_committed_facts_equal_a_fresh_collection(self):
        self.assertEqual(ss.collect_facts(), FACTS)


if __name__ == "__main__":
    unittest.main()
