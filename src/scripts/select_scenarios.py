"""
Purpose:
    The scenario-selection pipeline of the unsafe-stimulus design, and the generator of its two documentation
    artifacts. One run applies the written rules to the 40 AgentDojo workspace user tasks, picks the 12 candidate
    and 3 spare scenarios, plans each candidate's injected item (objective, strategy, wording, attacker identity),
    and writes the results; nothing in them is typed by hand:
      * src/data/scenario_selection.csv   one row per user task, with status, rule id and reason;
      * docs/scenario_selection.md        the rules, tables, constraints, trade-offs and unavoidable repetitions,
                                          rendered from docs/scenario_selection.template.md plus the computed results.
    Stage 2 fills the stage-2 columns and picks the final 8 scenarios, split into two participant lists, from the
    candidate items that pass. The unsafe stimuli are constructed (simulated) traces (build_simulated_items.py), so
    an item passes when its simulated record passed the inclusion checks (injection_checks.py) AND the researcher
    marked its stimulus "reviewed" in src/data/simulated/review.json (review_stimuli.py). Until items are reviewed
    there is no final set. The real injected runs (src/data/execution/injected_evaluation.json) are not a stage-2
    input.

    The rules (ids are used in the CSV and the document):
      R1  pool: a taxonomy-mapped scenario whose skills-v4b run succeeded (the 20 eligible stimuli);
      R2  AgentDojo's own correct solution passes the task's check;
      R3a a compound task ("Perform the following tasks...") is removed when one of its numbered parts equals the
          full prompt of another pool scenario;
      R4  AgentDojo difficulty HARD is removed;
      R3b near-duplicate prompts (word-set Jaccard >= 0.5) keep only the lowest seeded rank;
      R5  area-by-area round-robin in seeded order, balancing Easy/Medium, gives 12 candidates then the spares;
      R6  (final set, stage 2) from the passing items (simulated, included, manually reviewed), the final 8 never
          contain both members of a repeated-task pair.
    Subjective decisions are not hidden: the document lists, per rule, whether it is systematic or a judgment.

Inputs:
    src/data/scenario_selection_inputs.json (facts about the tasks, produced by --collect, which needs AgentDojo),
    docs/scenario_selection.template.md, and for stage 2 src/data/simulated/manifest.json (the simulated items and
    their inclusion verdicts; each record's SHA-256 is checked) and src/data/simulated/review.json (their manual
    review status; it must be current, i.e. equal to a fresh review_stimuli.py review, or stage 2 stops).

Outputs:
    src/data/scenario_selection.csv, docs/scenario_selection.md (and with --collect the inputs file).

Dependencies:
    injection_design.py, agentdojo_runs.py, participant_server.py (the eligibility rule), build_workspace_snapshots.py
    (replay, only for --collect), review_stimuli.py (stage 2: checks the review record is current). --collect needs the AgentDojo environment; everything else is plain python3.

Usage:
    .venv-agentdojo/bin/python -B src/scripts/select_scenarios.py --collect     # refresh the facts (no model calls)
    python3 -B src/scripts/select_scenarios.py                                  # write the CSV and the document
    python3 -B src/scripts/select_scenarios.py --check                          # fail if the committed files differ
"""

import argparse
import csv
import hashlib
import io
import itertools
import json
import re
import sys
from pathlib import Path

import injection_design as design

ROOT = Path(__file__).resolve().parents[2]
INPUTS = ROOT / "src" / "data" / "scenario_selection_inputs.json"
CSV_PATH = ROOT / "src" / "data" / "scenario_selection.csv"
DOC_PATH = ROOT / "docs" / "scenario_selection.md"
TEMPLATE = ROOT / "docs" / "scenario_selection.template.md"
SIMULATED_MANIFEST = ROOT / "src" / "data" / "simulated" / "manifest.json"
SIMULATED_CORPUS = ROOT / "src" / "data" / "corpora" / "simulated.json"  # its review_file is the review record
TAXONOMY = ROOT / "src" / "data" / "skill_taxonomy.json"

SELECTION_VERSION = "scenario_selection_v1"
SEED = "cs889-selection-v1"
DUPLICATE_THRESHOLD = 0.5
N_CANDIDATES, N_FINAL = 12, 8
COMPOUND_PREFIX = "Perform the following tasks in the order they are given"

RULES = [
    ("R1", "Pool", "systematic",
     "Only a scenario the skill taxonomy maps to skills, whose skills-v4b run completed with `utility: true` and whose "
     "stimulus is eligible for the participant viewer, can be a stimulus (the 20 active stimuli)."),
    ("R2", "Ground truth", "systematic",
     "AgentDojo's own correct-solution calls must pass the task's check (`GroundTruthPipeline`, no model call)."),
    ("R3a", "Compound tasks", "systematic",
     "A task that begins 'Perform the following tasks in the order they are given' is removed when one of its numbered "
     "parts equals, word for word, the full prompt of another scenario of the pool."),
    ("R4", "Complexity", "systematic",
     "Tasks AgentDojo rates HARD are removed. (Cross-check: the same tasks have more than two correct-solution calls.)"),
    ("R3b", "Near-duplicates", "systematic after a judgment",
     f"Prompts whose word sets overlap by at least {DUPLICATE_THRESHOLD} (Jaccard) form a group; one scenario per "
     "group is kept, the one with the lowest seeded rank. The threshold was chosen after seeing the similarity values "
     "(see the sensitivity table): it is a judgment, but any value in the stated range gives the same groups."),
    ("R5", "Area-by-area round-robin", "systematic",
     "Areas and scenarios are ranked by sha256(seed + id). One scenario is taken from each area in turn; within an "
     "area the best-ranked scenario of the difficulty (Easy/Medium) that is currently under-represented is taken. "
     "The first 12 are candidates, the rest spares."),
    ("R6", "Final set (stage 2)", "systematic",
     "From the items that passed their runs, the final 8 scenarios never contain both members of a repeated-task pair "
     "(one task's correct solution contained in the other's and the safe run of one passing the other's check)."),
]

AREA_LABELS = {"calendar-lookup": "calendar lookup", "calendar-scheduling": "calendar scheduling",
               "email-lookup": "email lookup", "email-correspondence": "email correspondence",
               "drive-file-management": "drive file management", "document-content": "document content"}

COLUMNS = [
    "scenario", "task_number", "prompt", "mapped_skills", "task_category", "task_kind", "difficulty",
    "ground_truth_calls", "safe_trace_actions", "in_pool", "near_duplicate_group", "repeated_task_group",
    "compatible_objectives", "seed_rank", "selection_status", "rule_id", "rationale",
    "planned_item_id", "attacker_objective", "strategy", "wording", "wording_variant", "attacker_identity",
    "simulated_included", "review_status", "stage2_status", "final_set", "participant_list",
]


# ------------------------------------------------------------------------------------------------ collecting facts

def collect_facts() -> dict:
    """Facts about the 40 tasks from AgentDojo and the project's records (run with the AgentDojo Python).

    Per task: prompt, difficulty, correct-solution calls and whether they pass, taxonomy skills, the skills-v4b
    run (path, sha256, status, utility), eligibility (participant_server.is_eligible), and for eligible tasks the
    actions of the safe trace. Also the repeated-task pairs, computed by replaying every eligible safe run.
    """
    import copy

    import agentdojo_runs as runs
    import build_execution_plan as bp
    import build_workspace_snapshots as bw
    import participant_server as ps
    from function_calling_pi import benchmark
    from function_calling_pi.agent_pipeline import GroundTruthPipeline

    suite = benchmark.SUITES["workspace"]
    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    base_env = suite.load_and_inject_default_environment({})
    tasks, replays = [], {}
    modules = bw.install_clock()
    for task_id in runs.list_user_task_ids():
        task = suite.get_user_task_by_id(task_id)
        calls = [{"function": c["function"], "args": json.loads(json.dumps(c["args"], default=str))}
                 for c in task.ground_truth(base_env)]
        passes = bool(suite.run_task_with_pipeline(GroundTruthPipeline(task), task, None, {})[0])
        folder = f"{task_id}__no_injection"
        run_file = f"src/data/execution/runs/{task_id}__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json"
        entry = {"scenario": task_id, "prompt": runs.user_prompt(runs.load_run(task_id)),
                 "difficulty": task.DIFFICULTY.name, "ground_truth_calls": calls, "ground_truth_passes": passes,
                 "mapped_skills": (taxonomy["scenario_map"].get(task_id) or {}).get("skills"),
                 "v4b_run": None, "eligible": False, "safe_actions": None, "safe_write_tools": None}
        path = ROOT / run_file
        if path.exists():
            raw = path.read_bytes()
            record = json.loads(raw)
            entry["v4b_run"] = {"file": run_file, "sha256": hashlib.sha256(raw).hexdigest(),
                                "status": record["status"], "utility": record["utility"]}
            entry["eligible"] = bool(ps.is_eligible(folder))
        if entry["eligible"]:
            high = json.loads((ROOT / "src/data/stimuli" / folder / "high.json").read_text(encoding="utf-8"))
            actions = [e["tool"] for e in high["action_log"] if e["type"] == "action"]
            entry["safe_actions"] = actions
            entry["safe_write_tools"] = sorted({t for t in actions if t in runs.WRITE_TOOLS})
            env = task.init_environment(suite.load_and_inject_default_environment({}))
            pre = copy.deepcopy(env)
            bw.replay(env, record["trace"], modules)
            output = next((m["content"] for m in reversed(record["trace"])
                           if m["role"] == "assistant" and m.get("content")), "")
            replays[task_id] = (pre, env, output)
        tasks.append(entry)
    key = lambda t: {(c["function"], json.dumps(c["args"], sort_keys=True)) for c in t["ground_truth_calls"]
                     if c["function"] != "get_current_day"}
    by_id = {t["scenario"]: t for t in tasks}
    pairs = []
    for a, b in itertools.permutations(replays, 2):
        if key(by_id[b]) <= key(by_id[a]):
            pre, post, output = replays[a]
            if suite.get_user_task_by_id(b).utility(output, pre, post, strict=False) is True:
                pairs.append([a, b])
    return {"selection_version": SELECTION_VERSION, "agentdojo_commit": bp.PINNED_COMMIT,
            "taxonomy_sha256": hashlib.sha256(TAXONOMY.read_bytes()).hexdigest(),
            "skills": {k: {"owned_write_tools": v["owned_write_tools"], "read_tools": v["read_tools"]}
                       for k, v in taxonomy["skills"].items()},
            "tasks": tasks, "contained_task_pairs": sorted(pairs)}


# ------------------------------------------------------------------------------------------------ the rules

def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", text.lower()))


def jaccard(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def components(items: list[str], linked) -> list[list[str]]:
    """Connected components of `items` under the symmetric relation `linked(a, b)`, each sorted by task number."""
    seen, out = set(), []
    for start in items:
        if start in seen:
            continue
        stack, comp = [start], []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            stack.extend(o for o in items if o not in seen and linked(node, o))
        out.append(sorted(comp, key=task_number))
    return out


def task_number(scenario: str) -> int:
    return int(scenario.rsplit("_", 1)[1])


def numbered_parts(prompt: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?:^|\s)\d+\.\s+", prompt)[1:] if p.strip()]


def area_of(task: dict, skills: dict) -> str | None:
    """The mapped skill that owns a write tool of the safe trace; else the first mapped skill."""
    mapped = task["mapped_skills"]
    if not mapped:
        return None
    for skill in mapped:
        if set(task["safe_write_tools"] or []) & set(skills[skill]["owned_write_tools"]):
            return skill
    return mapped[0]


def written_resources(task: dict) -> set[str]:
    return {design.TOOL_RESOURCE[t] for t in task["safe_write_tools"] or []}


def duplicate_groups(pool: dict[str, dict], threshold: float) -> list[list[str]]:
    return [g for g in components(sorted(pool), lambda a, b: jaccard(pool[a]["prompt"], pool[b]["prompt"]) >= threshold)
            if len(g) > 1]


def threshold_range(pool: dict[str, dict], groups: list[list[str]]) -> tuple[float, float]:
    """The thresholds (steps of 0.01) giving exactly the chosen duplicate groups."""
    same = [t / 100 for t in range(1, 100) if duplicate_groups(pool, t / 100) == groups]
    return (min(same), max(same)) if same else (DUPLICATE_THRESHOLD, DUPLICATE_THRESHOLD)


def round_robin(rows_by_area: dict[str, list[dict]], seed: str) -> list[dict]:
    """Rule R5: all scenarios of the remaining pool in selection order (see the module docstring)."""
    area_order = sorted(rows_by_area, key=lambda a: design.rank_key(seed, "area", a))
    queues = {a: sorted(rows_by_area[a], key=lambda r: design.rank_key(seed, "scenario", r["scenario"]))
              for a in area_order}
    counts = {"EASY": 0, "MEDIUM": 0}
    order = []
    while any(queues.values()):
        for area in area_order:
            queue = queues[area]
            if not queue:
                continue
            under = min(counts, key=lambda d: (counts[d], d))  # the difficulty with fewer picks so far
            pick = next((r for r in queue if r["difficulty"] == under), queue[0])
            queue.remove(pick)
            counts[pick["difficulty"]] = counts.get(pick["difficulty"], 0) + 1
            order.append(pick)
    return order


def select(facts: dict, seed: str = SEED, wordings: tuple | None = None, strategies: tuple | None = None) -> dict:
    """Apply R1-R5 to the facts. With neither `wordings` nor `strategies` the study plan is allocated (II with W1, W2
    and W3 mixed over objectives kept from the II + W2 allocation: design.allocate_study_items); giving either
    allocates that combination space jointly instead (design.allocate_items), for example wordings=("W2",) with
    strategies=("II",) for the II + W2 plan. Returns {"rows": [...], "order": [...], "threshold_range", "groups", ...}."""
    tasks = {t["scenario"]: t for t in facts["tasks"]}
    rows = {}
    for s, t in tasks.items():
        rows[s] = {"scenario": s, "task_number": task_number(s), "prompt": t["prompt"],
                   "mapped_skills": t["mapped_skills"] or [], "difficulty": t["difficulty"],
                   "ground_truth_calls": len(t["ground_truth_calls"]), "in_pool": False, "status": None,
                   "rule": None, "reason": None, "area": None, "kind": "", "safe_actions": None,
                   "near_dup": "", "repeated": "", "compatible": [], "rank": None}

    def exclude(s, rule, reason):
        if rows[s]["status"] is None:
            rows[s].update(status="excluded", rule=rule, reason=reason)

    for s, t in tasks.items():
        if not t["mapped_skills"]:
            exclude(s, "R1", "No skills are mapped to this task in the skill taxonomy, so it has no skills-v4b run.")
        elif not t["eligible"]:
            run = t["v4b_run"] or {}
            exclude(s, "R1", f"Its skills-v4b run did not succeed (status {run.get('status')}, utility {run.get('utility')}).")
    pool = {s: tasks[s] for s in tasks if rows[s]["status"] is None}
    for s, t in pool.items():
        rows[s].update(in_pool=True, area=area_of(t, facts["skills"]), safe_actions=len(t["safe_actions"]))
        rows[s]["kind"] = ("writes " + "+".join(sorted(written_resources(t)))) if t["safe_write_tools"] else "read only"
    for s, t in pool.items():
        if not t["ground_truth_passes"]:
            exclude(s, "R2", "AgentDojo's correct solution does not pass the task's check.")
    full = {s: t for s, t in pool.items() if rows[s]["status"] is None}
    for s, t in full.items():
        if t["prompt"].startswith(COMPOUND_PREFIX):
            parts = numbered_parts(t["prompt"])
            matched = sorted((o for o in full if o != s and any(" ".join(p.split()) == " ".join(full[o]["prompt"].split())
                                                                  for p in parts)), key=task_number)
            if matched:
                exclude(s, "R3a", "Compound task: its parts are the prompts of " + " and ".join(matched) + ".")
    for s, t in full.items():
        if rows[s]["status"] is None and t["difficulty"] == "HARD":
            exclude(s, "R4", f"AgentDojo difficulty HARD ({len(t['ground_truth_calls'])} correct-solution calls).")
    after_3a = {s: t for s, t in full.items() if rows[s]["rule"] != "R3a"}
    groups = duplicate_groups(after_3a, DUPLICATE_THRESHOLD)
    for group in groups:
        for s in group:
            rows[s]["near_dup"] = "|".join(g.replace("user_task_", "") for g in group)
    for group in groups:
        alive = [s for s in group if rows[s]["status"] is None]
        keep = min(alive, key=lambda s: design.rank_key(seed, "scenario", s)) if alive else None
        for s in alive:
            if s != keep:
                exclude(s, "R3b", "Near-duplicate prompt of " + keep + " (" +
                        f"{jaccard(tasks[s]['prompt'], tasks[keep]['prompt']):.2f} word overlap); the lower seeded rank is kept.")
    remaining = [s for s in full if rows[s]["status"] is None]
    for s in remaining:
        rows[s]["compatible"] = design.compatible_objectives(written_resources(tasks[s]), tasks[s]["prompt"])
    by_area = {}
    for s in remaining:
        by_area.setdefault(rows[s]["area"], []).append(rows[s])
    order = [r["scenario"] for r in round_robin(by_area, seed)]
    for rank, s in enumerate(order, 1):
        rows[s]["rank"] = rank
        if rank <= N_CANDIDATES:
            rows[s].update(status="candidate", rule="R5", reason=f"Selection order {rank} of {len(order)} "
                           f"(area {AREA_LABELS[rows[s]['area']]}, {rows[s]['difficulty'].lower()}).")
        else:
            rows[s].update(status="spare", rule="R5", reason=f"Selection order {rank} of {len(order)}: "
                           "first scenario after the candidates.")
    # repeated-task pairs among the scenarios that survived R1-R4 and R3b (R6 uses them in stage 2); a scenario
    # already removed (such as a compound task) must not link two others
    keep = set(remaining)
    pairs = sorted({tuple(sorted(p, key=task_number)) for p in map(tuple, facts["contained_task_pairs"])
                    if set(p) <= keep}, key=lambda p: (task_number(p[0]), task_number(p[1])))
    for comp in components(sorted(keep, key=task_number), lambda a, b: tuple(sorted((a, b), key=task_number)) in pairs):
        if len(comp) > 1:
            for s in comp:
                rows[s]["repeated"] = "|".join(c.replace("user_task_", "") for c in comp)
    cands = [{"scenario": s, "area": rows[s]["area"], "compatible": rows[s]["compatible"]}
             for s in order[:N_CANDIDATES]]
    if wordings is None and strategies is None:
        allocation = design.allocate_study_items(cands, seed)
    else:
        allocation = design.allocate_items(cands, seed, wordings or design.STUDY_WORDINGS,
                                           strategies or design.STUDY_STRATEGIES)
    for s, plan in allocation.items():
        plan["item_id"] = design.item_id(s, plan["objective"], plan["strategy"], plan["wording"], plan.get("variant"))
    spares = [s for s in order if rows[s]["status"] == "spare"]
    return {"rows": rows, "order": order, "allocation": allocation, "groups": groups,
            "threshold_range": threshold_range(after_3a, groups), "spares": spares,
            "pairs": pairs,
            "similarity": {f"{a}|{b}": round(jaccard(after_3a[a]["prompt"], after_3a[b]["prompt"]), 2)
                           for g in groups for a, b in itertools.combinations(g, 2)}}


# ------------------------------------------------------------------------------------------------ stage 2

def partitions(items: list[dict]):
    """Splits of 8 items into two lists of 4, each split once (list A holds the first item)."""
    for first in itertools.combinations(range(len(items)), len(items) // 2):
        if 0 in first:
            yield [items[i] for i in first], [items[i] for i in range(len(items)) if i not in first]


def plan_fields(plan: dict) -> dict:
    """The design fields of a planned item; "variant" only for W3 items, so W1/W2 plans keep their exact shape."""
    return {k: plan[k] for k in ("objective", "strategy", "wording", "identity_id", *(("variant",) if "variant" in plan else ()))}


def planned_wordings(allocation: dict) -> tuple:
    """The wordings an allocation uses, in design order."""
    used = {a["wording"] for a in allocation.values()}
    return tuple(w for w in design.WORDINGS if w in used)


def planned_strategies(allocation: dict) -> tuple:
    """The strategies an allocation uses, in design order."""
    used = {a["strategy"] for a in allocation.values()}
    return tuple(s for s in design.STRATEGIES if s in used)


def balanced_combinations(subset: list[dict], wordings: tuple = design.STUDY_WORDINGS,
                          strategies: tuple = design.STUDY_STRATEGIES) -> bool:
    """True if the strategy-wording combinations of the 8 items are used as equally as possible: two or three times
    each with the three study combinations (II with W1, W2, W3); exactly twice with four; once or twice with six."""
    combos = tuple((s, w) for s in strategies for w in wordings)
    low, high = len(subset) // len(combos), -(-len(subset) // len(combos))
    return all(low <= sum((i["strategy"], i["wording"]) == c for i in subset) <= high for c in combos)


def choose_final_set(passing: list[dict], pairs: list[tuple[str, str]], strict: bool = True,
                     wordings: tuple = design.STUDY_WORDINGS,
                     exists_only: bool = False, strategies: tuple = design.STUDY_STRATEGIES) -> dict | None:
    """Rule R6 and the participant lists: from the passing items choose 8 scenarios and split them into lists A and B.

    passing: [{"scenario", "area", "difficulty", "rank", "objective", "strategy", "wording", "identity_id"}].
    A choice is valid when it holds no repeated-task pair, two items of every objective and strategy-wording
    combination, and splits into two lists that each satisfy design.is_valid_unsafe_list. Among valid choices the
    one with the Easy/Medium split closest to 4/4, then the smallest total selection rank, then the earliest
    scenario ids wins; among splits, the one whose objectives differ in wording across the two lists most often.
    strict=True requires four different strategy-wording combinations in each list and the combinations of the 8
    items used equally (the planned design); strict=False only requires every wording in each list and the wordings
    used equally (a relaxation, reported as an option). `wordings` and `strategies` are those of the plan (default: the
    study plan, II + W2, where the combination condition is trivially met). exists_only stops at
    the first valid choice. Returns {"scenarios": [...], "A": [...], "B": [...], "crossing": n} or None.
    """
    best, best_key = None, None
    repeated = {frozenset(p) for p in pairs}
    for subset in itertools.combinations(sorted(passing, key=lambda i: i["rank"]), N_FINAL):
        ids = [i["scenario"] for i in subset]
        if any(frozenset(p) in repeated for p in itertools.combinations(ids, 2)):
            continue
        if any(sum(i["objective"] == o for i in subset) != 2 for o in design.OBJECTIVE_IDS):
            continue
        if strict and not balanced_combinations(subset, wordings, strategies):
            continue
        counts = [sum(i["wording"] == w for i in subset) for w in wordings]
        if not strict and not all(N_FINAL // len(wordings) <= c <= -(-N_FINAL // len(wordings)) for c in counts):
            continue
        key = (abs(sum(i["difficulty"] == "EASY" for i in subset) - N_FINAL // 2), sum(i["rank"] for i in subset),
               tuple(sorted(ids, key=task_number)))
        if best_key is not None and key >= best_key:
            continue
        split = None
        for a, b in partitions(list(subset)):
            if design.is_valid_unsafe_list(a, strict, wordings, strategies) and design.is_valid_unsafe_list(b, strict, wordings, strategies):
                wording = {i["objective"]: i["wording"] for i in a}
                crossing = sum(wording[i["objective"]] != i["wording"] for i in b)
                if split is None or crossing > split[0]:
                    split = (crossing, a, b)
        if split:
            best_key, best = key, {"scenarios": sorted(ids, key=task_number),
                                   "A": sorted(i["scenario"] for i in split[1]),
                                   "B": sorted(i["scenario"] for i in split[2]), "crossing": split[0]}
            if exists_only:
                return best
    return best


def review_sensitivity(allocation: dict, rows: dict, pairs: list, strict: bool = True) -> dict:
    """How robust the design is to manual review: every planned item can only drop out by being rejected (the
    simulated items all pass their automated checks), so enumerate every reviewed/rejected pattern of the 12 items.

    Returns {"patterns": 2^n, "feasible": patterns that still allow a valid final set,
             "essential": [scenarios whose rejection alone leaves no valid final set]}. No pass rate is assumed.
    """
    wordings, strategies = planned_wordings(allocation), planned_strategies(allocation)
    items = [{"scenario": s, "area": rows[s]["area"], "difficulty": rows[s]["difficulty"], "rank": rows[s]["rank"],
              **plan_fields(a)} for s, a in allocation.items()]
    feasible = 0
    for mask in range(1 << len(items)):
        chosen = [items[i] for i in range(len(items)) if mask >> i & 1]
        feasible += len(chosen) >= N_FINAL and choose_final_set(chosen, pairs, strict, wordings, True, strategies) is not None
    essential = [i["scenario"] for i in items
                 if choose_final_set([j for j in items if j is not i], pairs, strict, wordings, True, strategies) is None]
    return {"patterns": 1 << len(items), "feasible": feasible, "essential": sorted(essential, key=task_number)}


class Stage2Error(RuntimeError):
    """The stage-2 inputs are inconsistent (a record changed, the review record is stale, or items do not match)."""


def load_verdicts(manifest_path: Path = SIMULATED_MANIFEST, corpus_path: Path = SIMULATED_CORPUS) -> dict | None:
    """The stage-2 input: {"items": {item_id: {"included": bool, "review_status": str}}}, or None when no simulated
    items exist yet.

    "included" is the inclusion verdict of the item's simulated record (its SHA-256 must match the manifest);
    "review_status" is the item's status in the review record of the simulated corpus (unreviewed, reviewed,
    rejected, stale, auto_failed; "missing" if absent). The review record must equal a fresh review
    (review_stimuli.py), so a status saved before the stimulus or the checks changed is never trusted.
    Raises Stage2Error otherwise."""
    if not manifest_path.exists():
        return None
    import participant_server as ps
    import review_stimuli as rv

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = {}
    for entry in manifest["items"]:
        record_path = ROOT / entry["record"]["file"]
        if hashlib.sha256(record_path.read_bytes()).hexdigest() != entry["record"]["sha256"]:
            raise Stage2Error(f"{entry['record']['file']} does not match its SHA-256 in {manifest_path.name}")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        items[entry["item_id"]] = {"included": record["simulation"]["inclusion_check"]["included"] is True}
    previous = ps.CORPUS
    try:
        ps.use_corpus(corpus_path)
        saved = rv.read_record(ps.CORPUS["review_path"])
        if saved is None or rv.review_corpus(saved) != saved:
            raise Stage2Error("the simulated review record is missing or out of date: run review_stimuli.py "
                              f"--corpus {corpus_path} --write")
    finally:
        ps.use_corpus(previous["path"]) if previous else ps.use_corpus()
    for item_id, verdict in items.items():
        verdict["review_status"] = (saved["stimuli"].get(item_id) or {}).get("status", "missing")
    return {"items": items}


def stage2(result: dict, verdicts: dict | None) -> dict:
    """Per candidate: whether its simulated item passed the inclusion checks, its review status and its stage-2
    status; and the final set (R6) from the passing items, i.e. included AND reviewed. `verdicts` is load_verdicts()
    output (None: no simulated items, stage 2 has not run). The verdicts must name exactly the planned items."""
    info = {s: {"included": None, "review_status": None, "passing": False} for s in result["allocation"]}
    final = None
    if verdicts:
        by_item = {a["item_id"]: s for s, a in result["allocation"].items()}
        if set(verdicts["items"]) != set(by_item):
            raise Stage2Error("the simulated items do not match the planned items: rebuild them "
                              "(build_simulated_items.py)")
        for item_id, verdict in verdicts["items"].items():
            s = by_item[item_id]
            info[s].update(included=verdict["included"], review_status=verdict["review_status"],
                           passing=verdict["included"] and verdict["review_status"] == "reviewed")
        passing = [{"scenario": s, "area": result["rows"][s]["area"], "difficulty": result["rows"][s]["difficulty"],
                    "rank": result["rows"][s]["rank"], **plan_fields(a)}
                   for s, a in result["allocation"].items() if info[s]["passing"]]
        final = choose_final_set(passing, result["pairs"], wordings=planned_wordings(result["allocation"]),
                                  strategies=planned_strategies(result["allocation"]))
    return {"info": info, "final": final, "ran": bool(verdicts)}


STAGE2_STATUS = {"reviewed": "passed", "unreviewed": "awaiting manual review", "rejected": "rejected in review",
                 "stale": "review out of date", "auto_failed": "automated checks failed", "missing": "not in review"}


def stage2_status(info: dict) -> str:
    """The CSV's stage-2 status of one candidate (see STAGE2_STATUS); inclusion failures come first."""
    if not info["included"]:
        return "failed inclusion checks"
    return STAGE2_STATUS.get(info["review_status"], info["review_status"])


# ------------------------------------------------------------------------------------------------ rendering

def csv_rows(result: dict, st2: dict) -> list[dict]:
    out = []
    for s in sorted(result["rows"], key=task_number):
        r = result["rows"][s]
        plan = result["allocation"].get(s)
        identity = design.identity_by_id(plan["identity_id"]) if plan else None
        status2, in_final, which = "", "", ""
        if plan and st2["ran"]:
            status2 = stage2_status(st2["info"][s])
            if st2["final"]:
                in_final = "yes" if s in st2["final"]["scenarios"] else "no"
                which = "A" if s in st2["final"]["A"] else ("B" if s in st2["final"]["B"] else "")
        out.append({
            "scenario": s, "task_number": r["task_number"], "prompt": r["prompt"],
            "mapped_skills": "|".join(r["mapped_skills"]), "task_category": r["area"] or "",
            "task_kind": r["kind"], "difficulty": r["difficulty"].lower(), "ground_truth_calls": r["ground_truth_calls"],
            "safe_trace_actions": "" if r["safe_actions"] is None else r["safe_actions"],
            "in_pool": "yes" if r["in_pool"] else "no", "near_duplicate_group": r["near_dup"],
            "repeated_task_group": r["repeated"], "compatible_objectives": "|".join(r["compatible"]),
            "seed_rank": "" if r["rank"] is None else r["rank"], "selection_status": r["status"], "rule_id": r["rule"],
            "rationale": r["reason"], "planned_item_id": plan["item_id"] if plan else "",
            "attacker_objective": plan["objective"] if plan else "",
            "strategy": design.STRATEGY_NAMES[plan["strategy"]] if plan else "", "wording": plan["wording"] if plan else "",
            "wording_variant": plan.get("variant", "") if plan else "",
            "attacker_identity": identity["name"] + " <" + identity["address"] + ">" if identity else
            ("none (file ID 13)" if plan else ""),
            "simulated_included": ("yes" if st2["info"][s]["included"] else "no") if plan and st2["ran"] else "",
            "review_status": st2["info"][s]["review_status"] if plan and st2["ran"] else "",
            "stage2_status": status2, "final_set": in_final, "participant_list": which})
    return out


def render_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def md_table(header: list[str], body: list[list]) -> str:
    cell = lambda v: str(v).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in body]
    return "\n".join(lines)


def sections(result: dict, st2: dict, facts: dict) -> dict[str, str]:
    """The computed pieces of the document, keyed by the {{placeholders}} of the template."""
    rows, alloc = result["rows"], result["allocation"]
    ordered = sorted(rows, key=task_number)
    count = lambda status: sum(rows[s]["status"] == status for s in rows)  # noqa: E731
    rule_counts = {}
    for s in rows:
        if rows[s]["status"] == "excluded":
            rule_counts[rows[s]["rule"]] = rule_counts.get(rows[s]["rule"], 0) + 1
    out = {}
    out["version"] = f"{SELECTION_VERSION}, seed `{SEED}`, AgentDojo commit `{facts['agentdojo_commit']}`"
    out["rules"] = md_table(["Rule", "Step", "Kind", "Definition"], [[i, n, k, d] for i, n, k, d in RULES])
    out["counts"] = (f"40 AgentDojo workspace user tasks: {sum(r['in_pool'] for r in rows.values())} in the R1 pool, "
                     f"{count('candidate')} candidates, {count('spare')} spares, {count('excluded')} excluded "
                     f"({', '.join(f'{k}: {v}' for k, v in sorted(rule_counts.items()))}).")
    out["selection_table"] = md_table(
        ["Task", "Category", "Kind", "Difficulty", "Safe actions", "Status", "Rule", "Reason"],
        [[rows[s]["task_number"], AREA_LABELS.get(rows[s]["area"], ""), rows[s]["kind"], rows[s]["difficulty"].lower(),
          "" if rows[s]["safe_actions"] is None else rows[s]["safe_actions"], rows[s]["status"], rows[s]["rule"],
          rows[s]["reason"]] for s in ordered])
    lo, hi = result["threshold_range"]
    out["duplicates"] = (f"Groups at threshold {DUPLICATE_THRESHOLD}: " +
                         "; ".join("{" + ", ".join(g) + "}" for g in result["groups"]) +
                         f". The same groups result for every threshold from {lo:.2f} to {hi:.2f}. Pair overlaps: " +
                         ", ".join(f"{k.replace('user_task_', '')} = {v}" for k, v in result["similarity"].items()) + ".")
    cand = [s for s in result["order"] if rows[s]["status"] == "candidate"]
    easy = sum(rows[s]["difficulty"] == "EASY" for s in cand)
    out["balance"] = (f"Candidates: {easy} easy and {len(cand) - easy} medium; areas: " +
                      ", ".join(f"{AREA_LABELS[a]} {sum(rows[s]['area'] == a for s in cand)}" for a in sorted(
                          {rows[s]['area'] for s in cand})) + f". Spares: {', '.join(result['spares']) or 'none'}.")
    out["repeated"] = ("; ".join("{" + ", ".join(sorted(p, key=task_number)) + "}" for p in result["pairs"]
                                 if all(rows[s]["in_pool"] for s in p) and all(rows[s]["status"] in ("candidate", "spare") for s in p))
                       or "none among the remaining scenarios")
    out["allocation_table"] = md_table(
        ["Scenario", "Category", "Objective", "Strategy", "Wording", "Attacker identity", "Item id"],
        [[s, AREA_LABELS[rows[s]["area"]], f"{a['objective']} ({design.OBJECTIVES[a['objective']]['summary']})",
          design.STRATEGY_NAMES[a["strategy"]], a["wording"] + (f" ({a['variant']})" if "variant" in a else ""),
          (design.identity_by_id(a["identity_id"])["name"] if a["identity_id"] else "none (file ID 13)"), a["item_id"]]
         for s, a in ((s, alloc[s]) for s in cand)])
    out["objectives_table"] = md_table(
        ["Id", "AgentDojo task", "Resource", "Observable unauthorised action", "Used"],
        [[o, m["agentdojo_task"], m["resource"], m["write_tool"] + " (" + m["summary"] + ")", "yes"]
         for o, m in design.OBJECTIVES.items()] + [[o, "injection_task_" + o[1], "", "", "no: " + why]
                                                   for o, why in design.EXCLUDED_OBJECTIVES.items()])
    out["identity_pool"] = ", ".join(f"{i['name']} ({i['address']})" for i in design.IDENTITY_POOL)
    email_slots = sum(design.OBJECTIVES[o]["write_tool"] == "send_email" for o in design.OBJECTIVE_IDS)
    out["unavoidable"] = (
        f"- **Email to an outsider appears in {email_slots} of every participant's 4 unsafe trials.** AgentDojo's four "
        "used objectives include two that send an email (G3, G4); the other two are a file deletion and a calendar "
        "event. The two email trials differ in content (a forwarded private email and a security code).\n"
        "- **Emma Johnson is named in every injected skill** (the authority claim is part of all three wordings), and "
        "every participant's 4 unsafe skills use all three wordings (one wording twice), so no single phrase is "
        "repeated four times. The wordings are not assumed to be equally easy to notice; the wording of every item is "
        "recorded and its effect is reported descriptively.\n"
        "- **Every unsafe trial involves a non-company address or the deletion of a company file**; safe traces of "
        "some scenarios also show non-company addresses in tool results, so this is not a perfect cue.\n"
        "- **A victim item recurs across participants** (Lily's email for G3, the Facebook-code email for G4, file 13 "
        "for G1), but never twice within one participant (one trial per objective).\n"
        "- **Skills repeat**: 8 scenarios use 5 areas, so a participant may see a skill clean in one trial and injected "
        "in another; each list has 4 different areas for its 4 unsafe trials.")
    all_items = [{"scenario": s, "area": rows[s]["area"], "difficulty": rows[s]["difficulty"], "rank": rows[s]["rank"],
                  **plan_fields(alloc[s])} for s in cand]
    full = choose_final_set(all_items, result["pairs"], wordings=planned_wordings(result["allocation"]),
                              strategies=planned_strategies(result["allocation"]))
    out["full_feasible"] = (
        "If all 12 candidate items are marked reviewed, a valid final set exists: scenarios " + ", ".join(full["scenarios"]) +
        "; list A unsafe " + ", ".join(full["A"]) + "; list B unsafe " + ", ".join(full["B"]) + "."
        if full else "**Even if all 12 candidate items are reviewed, no valid final set exists under the constraints.**")
    strict, relaxed = (review_sensitivity(alloc, rows, result["pairs"], strict=flag) for flag in (True, False))
    out["review_sensitivity"] = md_table(
        ["Design", "Review outcomes (each item reviewed or rejected)", "Outcomes that still allow a valid final set",
         "Items whose rejection alone leaves no valid final set"],
        [[name, r["patterns"], r["feasible"], ", ".join(r["essential"]) or "none"]
         for name, r in (("planned (3 different strategy-wording combinations in each list)", strict),
                         ("relaxed (every wording of the plan in each list)", relaxed))])
    if st2["ran"]:
        final, info = st2["final"], st2["info"].values()
        reviewed = sum(i["passing"] for i in info)
        included = sum(bool(i["included"]) for i in info)
        out["stage2"] = (f"Stage 2 (simulated unsafe stimuli): {included} of {len(st2['info'])} simulated items pass the "
                         f"inclusion checks and {reviewed} are marked reviewed. " +
                         ("Final scenarios " + ", ".join(final["scenarios"]) + "; list A unsafe: " + ", ".join(final["A"])
                          + "; list B unsafe: " + ", ".join(final["B"]) + "."
                          if final else "No final set yet: an item passes only once it is manually reviewed, and the "
                          "passing items do not yet allow a valid final set.")
                         + " The status of each candidate is in the CSV.")
    else:
        out["stage2"] = "Stage 2 has not run: no simulated unsafe stimuli exist yet; the stage-2 CSV columns are empty."
    return out


def render_document(result: dict, st2: dict, facts: dict) -> str:
    text = re.sub(r"\A<!--.*?-->\n", "", TEMPLATE.read_text(encoding="utf-8"), flags=re.S)  # drop the template note
    for key, value in sections(result, st2, facts).items():
        text = text.replace("{{" + key + "}}", value)
    left = re.findall(r"\{\{(\w+)\}\}", text)
    if left:
        raise ValueError(f"template placeholders without a value: {left}")
    return text


def build(facts: dict, verdicts: dict | None = None) -> tuple[str, str]:
    result = select(facts)
    st2 = stage2(result, verdicts)
    return render_csv(csv_rows(result, st2)), render_document(result, st2, facts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--collect", action="store_true", help="refresh the facts (needs AgentDojo)")
    parser.add_argument("--check", action="store_true", help="fail if the CSV or document differ from a rebuild")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if args.collect:
        INPUTS.write_text(json.dumps(collect_facts(), indent=2) + "\n", encoding="utf-8")
        print(f"facts written to {INPUTS}")
    facts = json.loads(INPUTS.read_text(encoding="utf-8"))
    csv_text, doc_text = build(facts, load_verdicts())
    if args.check:
        stale = [p.name for p, t in ((CSV_PATH, csv_text), (DOC_PATH, doc_text))
                 if not p.exists() or p.read_text(encoding="utf-8") != t]
        if stale:
            sys.exit(f"out of date, rerun select_scenarios.py: {stale}")
        print("up to date")
        return
    CSV_PATH.write_text(csv_text, encoding="utf-8")
    DOC_PATH.write_text(doc_text, encoding="utf-8")
    print(f"wrote {CSV_PATH} and {DOC_PATH}")


if __name__ == "__main__":
    main()
