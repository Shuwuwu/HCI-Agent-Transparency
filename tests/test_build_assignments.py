"""Tests of build_assignments.py: Williams design balance, the constraints on every participant, equal scenario
distribution in both transparency conditions, determinism, and that the validator catches broken assignments."""

import copy
import itertools
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import build_assignments as ba  # noqa: E402
import select_scenarios as ss  # noqa: E402

FACTS = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
RESULT = ss.select(FACTS)
ITEMS = ba.planned_items(RESULT)
FINAL = ss.choose_final_set(list(ITEMS.values()), RESULT["pairs"])


class AssignmentTests(unittest.TestCase):
    def test_williams_rows_are_a_latin_square_balanced_for_neighbours(self):
        rows = ba.williams_rows()
        for column in zip(*rows):
            self.assertEqual(sorted(column), list(range(8)))
        pairs = [(r[i], r[i + 1]) for r in rows for i in range(7)]
        self.assertEqual(len(set(pairs)), 56)  # every ordered pair of different trials follows each other once

    def test_column_mapping_prevents_three_unsafe_in_a_row_in_every_row(self):
        scenarios = FINAL["scenarios"]
        mapping = ba.column_mapping(scenarios, set(FINAL["A"]), set(FINAL["B"]), "s")
        self.assertEqual(sorted(mapping), sorted(scenarios))
        for lst in ("A", "B"):
            unsafe = {i for i, s in enumerate(mapping) if s in FINAL[lst]}
            for row in ba.williams_rows():
                self.assertLessEqual(ba.max_unsafe_run(row, unsafe), 2)

    def test_generated_assignments_satisfy_every_constraint(self):
        for n in (8, 32, 40):
            a = ba.generate(FINAL, ITEMS, n)
            self.assertEqual(len(a), n)
            self.assertEqual(ba.validate(a, FINAL, ITEMS, RESULT["pairs"]), [], n)
            for p in a:
                self.assertEqual(sum(t["version"] == "unsafe" for t in p["trials"]), 4)
                self.assertTrue(all((t["item_id"] is not None) == (t["version"] == "unsafe") for t in p["trials"]))

    def test_complete_blocks_balance_lists_and_conditions_and_rows(self):
        a = ba.generate(FINAL, ITEMS, 32)
        cells = {}
        for p in a:
            cells.setdefault((p["list"], p["condition"]), []).append(p)
        self.assertEqual({k: len(v) for k, v in cells.items()}, {(l, c): 8 for l in "AB" for c in ("low", "high")})
        # in every cell the 8 participants use 8 different trial orders (a full Williams cycle)
        for group in cells.values():
            self.assertEqual(len({tuple(t["scenario"] for t in p["trials"]) for p in group}), 8)
        for cond in ("low", "high"):
            counts = {}
            for p in a:
                if p["condition"] == cond:
                    for t in p["trials"]:
                        counts[(t["scenario"], t["version"])] = counts.get((t["scenario"], t["version"]), 0) + 1
            self.assertEqual(set(counts.values()), {8})  # same scenario-version distribution in both conditions

    def test_deterministic_and_seed_dependent(self):
        self.assertEqual(ba.generate(FINAL, ITEMS, 16), ba.generate(FINAL, ITEMS, 16))
        self.assertNotEqual(ba.generate(FINAL, ITEMS, 16), ba.generate(FINAL, ITEMS, 16, seed="other"))

    def test_validator_detects_broken_assignments(self):
        good = ba.generate(FINAL, ITEMS, 8)
        cases = {}
        wrong_list = copy.deepcopy(good)
        wrong_list[0]["list"] = "B" if wrong_list[0]["list"] == "A" else "A"
        cases["unsafe trials are not those of list"] = wrong_list
        missing = copy.deepcopy(good)
        missing[1]["trials"].pop()
        cases["not the 8 final scenarios"] = missing
        unbalanced = copy.deepcopy(good)
        unbalanced[0]["condition"] = "high" if unbalanced[0]["condition"] == "low" else "low"
        cases["not equally frequent"] = unbalanced
        for needle, broken in cases.items():
            self.assertTrue(any(needle in p for p in ba.validate(broken, FINAL, ITEMS, RESULT["pairs"])), needle)
        repeated = [tuple(sorted(FINAL["scenarios"][:2]))]
        self.assertTrue(any("repeated legitimate task" in p for p in ba.validate(good, FINAL, ITEMS, repeated)))
        same_objective = copy.deepcopy(ITEMS)
        a, b = FINAL["A"][:2]
        same_objective[b]["objective"] = same_objective[a]["objective"]
        self.assertTrue(any("repeat an objective" in p for p in ba.validate(good, FINAL, same_objective, RESULT["pairs"])))


if __name__ == "__main__":
    unittest.main()
