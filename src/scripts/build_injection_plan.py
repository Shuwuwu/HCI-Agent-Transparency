"""
Purpose:
    Build the execution plan of the injected-skill batch: for every item of the injected corpus (build_injected_items.py)
    up to three attempts, each run with the scenario's skills exactly as in its skills-v4b run EXCEPT that the area
    skill is the item's injected version. Same model snapshot (gpt-4o-2024-08-06), same system-message builder and
    tools as the safe runs, so safe and unsafe stimuli differ only by the injected block.

    A plan is not an execution: it spends nothing. run_executions.py executes it, and its budget limits apply (it never
    starts a run beyond --max-runs, never repeats a finished run). The three attempts of an item are in the plan, but
    attempt k+1 is meant to be run only when attempts 1..k did not pass the inclusion rule (injection_checks.py);
    `--next-round` lists exactly which run ids to run next, so at most three attempts are ever made per item and every
    attempt, including failures, is kept as a record.

Inputs:
    src/data/injected_skills/manifest.json, src/data/scenario_selection_inputs.json (prompts, v4b runs),
    optionally an evaluation file (injection_checks.py) for --next-round.

Outputs:
    src/data/execution/plan_injected_v1.json (the plan), or on stdout the run ids of the next round.
    With --pilot: src/data/execution/plan_injected_wording_pilot.json, the wording-pilot plan (below).

    Wording pilot. To compare wording W2 and W3 the pilot runs every item of the II + W2 plan (pilot_w2/) and its
    W3 twin (pilot_w3/, build_injected_items.py --twins) three times each, ALL attempts, with no stopping at the first pass, so the two
    wordings are compared on the same number of tries: 12 + 12 items x 3 = 72 runs, batch `inj-pilot-w2w3`. Run ids
    differ from the study plan's (`__inj-pilot-w2w3`), so a pilot record is never confused with a study record.
    The pilot plan is a plan only: nothing runs until run_executions.py is started with approval.

Dependencies:
    build_execution_plan.py (system message, skills block, run id format), build_injected_items.py outputs.

Usage:
    python3 -B src/scripts/build_injection_plan.py                       # write the plan (no model call)
    python3 -B src/scripts/build_injection_plan.py --next-round [EVAL]   # run ids still needed, as --only arguments
    python3 -B src/scripts/build_injection_plan.py --pilot               # write the W2-vs-W3 wording pilot plan
    .venv-agentdojo/bin/python -B src/scripts/run_executions.py --plan src/data/execution/plan_injected_v1.json \\
        --dry-run                                                        # preview; a real run needs approval
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import build_execution_plan as bp
import select_scenarios as ss

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "src" / "data" / "injected_skills" / "manifest.json"
PLAN_PATH = ROOT / "src" / "data" / "execution" / "plan_injected_v1.json"
EVALUATION = ROOT / "src" / "data" / "execution" / "injected_evaluation.json"
MODEL = "gpt-4o-2024-08-06"
BATCH = "inj-v1"
PILOT_W2_MANIFEST = ROOT / "src" / "data" / "injected_skills" / "pilot_w2" / "manifest.json"
TWIN_MANIFEST = ROOT / "src" / "data" / "injected_skills" / "pilot_w3" / "manifest.json"
PILOT_PLAN_PATH = ROOT / "src" / "data" / "execution" / "plan_injected_wording_pilot.json"
PILOT_BATCH = "inj-pilot-w2w3"
CONDITION = "injected-skills"
MAX_ATTEMPTS = 3


def run_id(item_id: str, attempt: int, batch: str = BATCH) -> str:
    return f"{item_id}__{CONDITION}__{MODEL}__r{attempt}__{batch}"


def build_plan(manifest: dict, facts: dict, attempts: int = MAX_ATTEMPTS, batch: str = BATCH) -> dict:
    """The plan: {items x attempts} runs. Raises ValueError if an item's skills do not match its safe run."""
    tasks = {t["scenario"]: t for t in facts["tasks"]}
    planned = []
    for item in manifest["items"]:
        task = tasks[item["scenario"]]
        safe = json.loads((ROOT / task["v4b_run"]["file"]).read_text(encoding="utf-8"))
        skills, replaced = [], 0
        for entry in safe["skills"]:
            if entry["id"] == item["injected_skill"]:
                skills.append({"id": entry["id"], "file": item["item_file"], "sha256": item["item_sha256"]})
                replaced += 1
            else:
                skills.append(dict(entry))
        if replaced != 1:
            raise ValueError(f"{item['item_id']}: injected skill {item['injected_skill']} not among the safe run's skills")
        if hashlib.sha256((ROOT / item["item_file"]).read_bytes()).hexdigest() != item["item_sha256"]:
            raise ValueError(f"{item['item_id']}: the item file differs from the manifest; rebuild the items")
        identity = item["attacker_identity"]
        for attempt in range(1, attempts + 1):
            planned.append({
                "run_id": run_id(item["item_id"], attempt, batch), "scenario": item["scenario"],
                "task_prompt": task["prompt"], "condition": CONDITION, "model": MODEL, "repetition": attempt,
                "skills": skills,
                "injection": {"item_id": item["item_id"], "objective": item["objective"],
                              "strategy": item["strategy"], "wording": item["wording"],
                              "identity_id": identity["id"] if identity else None,
                              "injected_skill": item["injected_skill"]}})
    return {
        "plan_version": "execution_plan_v1",
        "benchmark": {"suite": "workspace", "agentdojo_commit": bp.PINNED_COMMIT, "attack": "none (the injection is in "
                      "the skill text of the system message, not in a tool result)",
                      "pipeline": "AgentDojo default: system message, user query, OpenAI chat model with tools, tool "
                                  "loop (no defence); temperature 0.0, at most 15 tool-loop iterations"},
        "skills_block": {"version": bp.SKILLS_BLOCK_VERSION, "preamble": bp.SKILLS_PREAMBLE,
                         "form": "oracle loading: the full text of every mapped skill is appended to the system "
                                 "message; the area skill is the injected item"},
        "limits": {"max_runs": len(manifest["items"]) * attempts, "max_attempts_per_item": attempts},
        "design": {"selection_version": manifest["selection_version"], "design_version": manifest["design_version"],
                   "seed": manifest["seed"], "rule": "attempt k+1 only when attempts 1..k did not pass the inclusion "
                                                    "rule; at most 3 attempts per item; every attempt is kept"},
        "runs": planned,
    }


def build_pilot_plan(w2: dict, twins: dict, facts: dict, attempts: int = MAX_ATTEMPTS) -> dict:
    """The W2-vs-W3 wording pilot: the pilot's W2 items (pilot_w2/) and their W3 twins (pilot_w3/), `attempts` runs
    each (all of them), batch PILOT_BATCH. Raises ValueError if a twin is not paired with exactly one W2 item of the
    same scenario, objective and identity, or if an item's skills do not match its safe run (build_plan)."""
    by_id = {i["item_id"]: i for i in w2["items"]}
    paired = [t["paired_with"] for t in twins["items"]]
    if sorted(paired) != sorted(by_id):
        raise ValueError("the twins are not exactly one per W2 item; rebuild with build_injected_items.py --twins")
    for t in twins["items"]:
        w = by_id[t["paired_with"]]
        if (w["scenario"], w["objective"], w["attacker_identity"]) != (t["scenario"], t["objective"], t["attacker_identity"]):
            raise ValueError(f"{t['item_id']} does not match its W2 item {w['item_id']}")
    combined = {**w2, "items": w2["items"] + twins["items"]}
    plan = build_plan(combined, facts, attempts, PILOT_BATCH)
    plan["design"]["rule"] = ("wording pilot: every item (the W2 items and their W3 twins) is run for all attempts, "
                              "with no stopping at the first pass, so W2 and W3 are compared on the same number of "
                              "tries; every attempt is kept")
    plan["design"]["pilot"] = {"batch": PILOT_BATCH, "compares": ["W2", "W3"],
                               "w2_manifest": "src/data/injected_skills/pilot_w2/manifest.json",
                               "twin_manifest": "src/data/injected_skills/pilot_w3/manifest.json"}
    return plan


def next_round(plan: dict, evaluation: dict | None, runs_dir: Path) -> list[str]:
    """Run ids to execute next: for each item not yet passing and with fewer than 3 attempts, the first attempt without
    a record. An attempt that was executed but not yet evaluated blocks the next one (evaluate first)."""
    passed, evaluated = set(), set()
    for ev in (evaluation or {}).get("evaluations", []):
        evaluated.add(ev["run_id"])
        if ev["included"]:
            passed.add(ev["item_id"])
    out = []
    items = {}
    for spec in plan["runs"]:
        items.setdefault(spec["injection"]["item_id"], []).append(spec)
    for item_id, specs in items.items():
        if item_id in passed:
            continue
        for spec in sorted(specs, key=lambda s: s["repetition"]):
            executed = (runs_dir / f"{spec['run_id']}.json").exists()
            if not executed:
                out.append(spec["run_id"])
                break
            if spec["run_id"] not in evaluated:
                break  # executed, not evaluated yet: do not start another attempt
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--next-round", nargs="?", const=str(EVALUATION), default=None, metavar="EVAL")
    parser.add_argument("--pilot", action="store_true", help="write the W2-vs-W3 wording pilot plan instead")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    facts = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
    if args.pilot:
        twins = json.loads(TWIN_MANIFEST.read_text(encoding="utf-8"))
        plan = build_pilot_plan(json.loads(PILOT_W2_MANIFEST.read_text(encoding="utf-8")), twins, facts)
        PILOT_PLAN_PATH.write_text(json.dumps(plan, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(plan['runs'])} planned runs ({len(plan['runs']) // MAX_ATTEMPTS} items x {MAX_ATTEMPTS}) "
              f"to {PILOT_PLAN_PATH}")
        return
    plan = build_plan(manifest, facts)
    if args.next_round is not None:
        path = Path(args.next_round)
        evaluation = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        print(" ".join(next_round(plan, evaluation, ROOT / "src" / "data" / "execution" / "runs")))
        return
    PLAN_PATH.write_text(json.dumps(plan, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(plan['runs'])} planned runs ({len(manifest['items'])} items x {MAX_ATTEMPTS}) to {PLAN_PATH}")


if __name__ == "__main__":
    main()
