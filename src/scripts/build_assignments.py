"""
Purpose:
    Generate and validate the participant assignments of the unsafe-stimulus design: for each participant a list
    (A or B: which 4 of the final 8 scenarios are unsafe), a transparency condition (low or high) and a trial order.
    Both lists show every scenario once, swapping safe and unsafe, so the scenario distribution is the same in both
    transparency conditions whenever the participants fill complete blocks of four (list A/B x low/high).

    Constraints enforced and checked (see docs/scenario_selection.md, section 6):
      * each participant sees the 8 final scenarios once, 4 safe and 4 unsafe;
      * the 4 unsafe trials have different objectives, different areas (injected skills), different attacker
        identities and all three wordings of the plan (one wording twice, W3 variants different) - checked through
        injection_design.is_valid_unsafe_list;
      * in complete blocks the unsafe trials of every wording occur equally often in the low and the high condition
        (this follows from both lists being shown in both conditions equally often; validate() checks it, and
        diagnostics() reports the mean trial position of each wording per condition);
      * no repeated legitimate task (the final set contains no repeated-task pair);
      * conditions are assigned in blocks of 4 participants, one of each (list, condition) cell, in a seeded
        random order within the block;
      * trial order follows the rows of a balanced Latin square (Williams design) over the 8 scenarios, using the
        scenario-to-column mapping (searched with a seed) in which no row of either list has three unsafe trials in a
        row; the c-th participant of a cell receives row c mod 8.
    Nothing here is shown to participants; the output is a researcher file. Mapping assignments to opaque stimulus
    links is a separate step (not done here).

Inputs:
    The final set and lists chosen by select_scenarios.choose_final_set from the passing unsafe items (stage 2,
    select_scenarios.load_verdicts: a simulated item passes when its record passed the inclusion checks and its
    stimulus is marked "reviewed" in src/data/simulated/review.json), or --simulate / --write-simulated N with every
    planned item assumed to pass (no stage-2 input needed).

Outputs:
    --simulate: a validation summary on stdout (no file is written).
    --write: src/data/participant_assignments.json, the study assignments ("simulated": false). Here "simulated"
        means development assignments over planned items, not constructed traces: the study assignments show
        the constructed (simulated) unsafe stimuli and are still "simulated": false.
    --write-simulated: src/data/validation/participant_assignments.json, the same design over the *planned*
        (not yet passing) items, marked "simulated": true. It exists so that the participant flow can be built
        and tested before the injected runs pass; the study corpus and the study assignments never read it, and
        experiment_plan.py refuses a simulated assignments file unless its config says simulated: true.

Dependencies:
    select_scenarios.py (selection, final set), injection_design.py (list validity).
    Consumed by experiment_plan.py (which turns one assignment row into a participant's stage sequence).

Usage:
    python3 -B src/scripts/build_assignments.py --simulate 32          # dry run: validate the design with 32 participants
    python3 -B src/scripts/build_assignments.py --write-simulated 8    # development assignments (planned items)
    python3 -B src/scripts/build_assignments.py --write N              # after stage 2: write N assignments
"""

import argparse
import itertools
import json
from collections import Counter
import sys
from pathlib import Path

import injection_design as design
import select_scenarios as ss

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "src" / "data" / "participant_assignments.json"
# Development assignments over the planned (not yet passing) items; kept with the other simulated artifacts.
SIMULATED_OUT = ROOT / "src" / "data" / "validation" / "participant_assignments.json"
CONDITIONS = ("low", "high")
LISTS = ("A", "B")
ASSIGNMENT_VERSION = "participant_assignments_v1"
WILLIAMS_BASE = (0, 1, 7, 2, 6, 3, 5, 4)  # for 8 trials: every ordered neighbour pair occurs exactly once over 8 rows


def williams_rows() -> list[list[int]]:
    """The 8 rows of a Williams design for 8 trials (balanced for first-order carry-over)."""
    return [[(x + r) % 8 for x in WILLIAMS_BASE] for r in range(8)]


def max_unsafe_run(order: list[int], unsafe: set[int]) -> int:
    best = run = 0
    for index in order:
        run = run + 1 if index in unsafe else 0
        best = max(best, run)
    return best


def column_mapping(scenarios: list[str], unsafe_a: set[str], unsafe_b: set[str], seed: str) -> list[str]:
    """Scenario at each Williams column: the first permutation, in a seeded order, in which no row of either list
    has more than two unsafe trials in a row. Raises ValueError if there is none."""
    rows = williams_rows()
    ordered = sorted(itertools.permutations(scenarios), key=lambda p: design.rank_key(seed, "columns", *p))
    for perm in ordered:
        ua = {i for i, s in enumerate(perm) if s in unsafe_a}
        ub = {i for i, s in enumerate(perm) if s in unsafe_b}
        if all(max_unsafe_run(r, ua) <= 2 and max_unsafe_run(r, ub) <= 2 for r in rows):
            return list(perm)
    raise ValueError("no scenario order keeps unsafe trials from running three in a row")


def generate(final: dict, items: dict[str, dict], n: int, seed: str = ss.SEED + "-assign") -> list[dict]:
    """Assignments for n participants. final: {"scenarios", "A", "B"} (A/B = unsafe scenarios of each list);
    items: {scenario: planned item with item_id}. Cells are filled in seeded blocks of four."""
    scenarios = list(final["scenarios"])
    columns = column_mapping(scenarios, set(final["A"]), set(final["B"]), seed)
    rows = williams_rows()
    cells = [(lst, cond) for lst in LISTS for cond in CONDITIONS]
    seen = {cell: 0 for cell in cells}
    out = []
    for k in range(n):
        block, position = divmod(k, len(cells))
        cell = sorted(cells, key=lambda c: design.rank_key(seed, "block", str(block), *c))[position]
        lst, cond = cell
        row = rows[seen[cell] % 8]
        seen[cell] += 1
        unsafe = set(final[lst])
        trials = [{"trial": t + 1, "scenario": columns[i], "version": "unsafe" if columns[i] in unsafe else "safe",
                   "item_id": items[columns[i]]["item_id"] if columns[i] in unsafe else None}
                  for t, i in enumerate(row)]
        out.append({"participant": f"P{k + 1:03d}", "list": lst, "condition": cond, "trials": trials})
    return out


def validate(assignments: list[dict], final: dict, items: dict[str, dict], pairs: list) -> list[str]:
    """Problems found in a set of assignments (empty list = all constraints hold)."""
    problems = []
    repeated = {frozenset(p) for p in pairs}
    for a in assignments:
        who = a["participant"]
        scenarios = [t["scenario"] for t in a["trials"]]
        if sorted(scenarios) != sorted(final["scenarios"]):
            problems.append(f"{who}: not the 8 final scenarios once each")
        unsafe = [t for t in a["trials"] if t["version"] == "unsafe"]
        if len(unsafe) != 4 or {t["scenario"] for t in unsafe} != set(final[a["list"]]):
            problems.append(f"{who}: unsafe trials are not those of list {a['list']}")
        chosen = [dict(items[t["scenario"]], area=items[t["scenario"]]["area"]) for t in unsafe]
        if not design.is_valid_unsafe_list(chosen):
            problems.append(f"{who}: the 4 unsafe items repeat an objective, combination, area or identity")
        if any(frozenset(p) in repeated for p in itertools.combinations(scenarios, 2)):
            problems.append(f"{who}: repeated legitimate task")
        order = [i for i, t in enumerate(a["trials"]) if t["version"] == "unsafe"]
        if max_unsafe_run(list(range(8)), set(order)) > 2:
            problems.append(f"{who}: three unsafe trials in a row")
    full = len(assignments) // 4 * 4
    if full:  # wording balance across transparency conditions (complete blocks only)
        by_wording = {c: Counter(items[t["scenario"]]["wording"] for a in assignments[:full] if a["condition"] == c
                                 for t in a["trials"] if t["version"] == "unsafe") for c in CONDITIONS}
        if by_wording["low"] != by_wording["high"]:
            problems.append(f"unsafe wordings are not equally frequent in both conditions: "
                            f"{dict(by_wording['low'])} vs {dict(by_wording['high'])}")
    for cond in CONDITIONS:
        counts = {}
        for a in assignments[:full]:
            if a["condition"] == cond:
                for t in a["trials"]:
                    counts[(t["scenario"], t["version"])] = counts.get((t["scenario"], t["version"]), 0) + 1
        if full and len(set(counts.values())) > 1:
            problems.append(f"condition {cond}: scenario-version counts are not equal ({sorted(set(counts.values()))})")
    if full:
        by_cond = {c: sorted(sum(1 for a in assignments[:full] if a["condition"] == c and a["list"] == lst)
                             for lst in LISTS) for c in CONDITIONS}
        if by_cond["low"] != by_cond["high"]:
            problems.append(f"lists are not equally frequent in both conditions: {by_cond}")
    return problems


def diagnostics(assignments: list[dict], items: dict[str, dict] | None = None) -> dict:
    """Balance measures reported with the design: first-trial version per condition and, when the planned items are
    given, how often each wording is shown unsafe and its mean trial position (1-8) per condition."""
    out = {}
    for cond in CONDITIONS:
        group = [a for a in assignments if a["condition"] == cond]
        first = sum(a["trials"][0]["version"] == "unsafe" for a in group)
        out[cond] = {"participants": len(group), "first_trial_unsafe": first}
        if items:
            positions = {}
            for a in group:
                for t in a["trials"]:
                    if t["version"] == "unsafe":
                        positions.setdefault(items[t["scenario"]]["wording"], []).append(t["trial"])
            out[cond]["unsafe_by_wording"] = {w: len(v) for w, v in sorted(positions.items())}
            out[cond]["mean_position_by_wording"] = {w: round(sum(v) / len(v), 2) for w, v in sorted(positions.items())}
    return out


def planned_items(result: dict) -> dict[str, dict]:
    rows = result["rows"]
    return {s: {"scenario": s, "area": rows[s]["area"], "difficulty": rows[s]["difficulty"], "rank": rows[s]["rank"],
                **{k: a[k] for k in ("objective", "strategy", "wording", "identity_id", "item_id",
                                     *(("variant",) if "variant" in a else ()))}}
            for s, a in result["allocation"].items()}


def write_assignments(path: Path, final: dict, assignments: list[dict], simulated: bool) -> None:
    """Write an assignments file.

    `simulated` records whether the unsafe items are the *planned* ones (True: the injected runs have not been
    executed and judged, so the item ids may never become real stimuli) or the ones that passed stage 2 (False).
    The flag is part of the file so that no consumer can mistake a development file for the study design.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": ASSIGNMENT_VERSION, "seed": ss.SEED, "simulated": simulated, "final": final,
               "assignments": assignments}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(assignments)} {'simulated ' if simulated else ''}assignments to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--simulate", type=int, metavar="N")
    parser.add_argument("--write", type=int, metavar="N")
    parser.add_argument("--write-simulated", type=int, metavar="N", dest="write_simulated",
                        help=f"write N development assignments over the planned items to {SIMULATED_OUT}")
    args = parser.parse_args()
    facts = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
    result = ss.select(facts)
    items = planned_items(result)
    if args.simulate is not None:
        final = ss.choose_final_set(list(items.values()), result["pairs"])
        assignments = generate(final, items, args.simulate)
        problems = validate(assignments, final, items, result["pairs"])
        print(f"{len(assignments)} simulated participants; problems: {problems or 'none'}")
        print(json.dumps(diagnostics(assignments, items)))
        return
    if args.write_simulated is not None:
        # Same design as --simulate, written to the validation folder so the participant flow can be developed.
        final = ss.choose_final_set(list(items.values()), result["pairs"])
        assignments = generate(final, items, args.write_simulated)
        problems = validate(assignments, final, items, result["pairs"])
        if problems:
            sys.exit(f"invalid assignments: {problems}")
        write_assignments(SIMULATED_OUT, final, assignments, simulated=True)
        return
    if args.write is None:
        parser.error("choose --simulate N, --write-simulated N or --write N")
    verdicts = ss.load_verdicts()
    if verdicts is None:
        sys.exit("no simulated unsafe stimuli yet: run build_simulated_items.py")
    st2 = ss.stage2(result, verdicts)
    if not st2["final"]:
        passed = sum(i["passing"] for i in st2["info"].values())
        sys.exit(f"no valid final set with the items that passed ({passed} of {len(st2['info'])} included and "
                 "reviewed): review them with review_stimuli.py --corpus src/data/corpora/simulated.json --mark")
    final = st2["final"]
    assignments = generate(final, items, args.write)
    problems = validate(assignments, final, items, result["pairs"])
    if problems:
        sys.exit(f"invalid assignments: {problems}")
    write_assignments(OUT, final, assignments, simulated=False)


if __name__ == "__main__":
    main()
