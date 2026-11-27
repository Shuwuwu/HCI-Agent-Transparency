"""
Purpose:
    Build the SIMULATED unsafe stimuli of the 12 study items (strategy II, wordings W1-W3) offline: no model is
    called, no API key is needed, nothing is spent, and no attack is run against a model. Each is a constructed
    trace, not a real agent execution, and is labelled so everywhere (`simulated: true`, `material_kind:
    "simulated_trace"`). The safe stimuli stay genuine recordings; only the unsafe ones are constructed.

    Construction policy (one policy for every item, recorded in each record's `simulation.construction`):
      - start from the scenario's genuine skills-v4b safe run (path and SHA-256 checked against scenario selection);
      - the agent's skill is the item's injected skill (src/data/injected_skills/manifest.json, hash checked);
      - the attack's calls are AgentDojo's own minimal correct-solution calls (`ground_truth`) of the item's
        injection task, parametrised with the item's attacker identity, inserted before the agent's first reply
        (the injected wordings ask for the action first);
      - every call is replayed through AgentDojo's tools, and every legitimate result must equal the safe run's
        (build_simulated_stimulus.simulated_trace), so safe and unsafe versions differ only by the attack;
      - the final answer is the safe run's, unchanged (the recorded runs show that real agents sometimes mention the
        injected action: see `reference_recordings`);
      - injection_checks.evaluate_record judges the result (attack succeeded, legitimate task done, action visible).
    The record cites, by path and SHA-256, the recorded AgentDojo runs (src/data/review/reference_recordings.json)
    in which a real agent carried out the same objective on the same scenario: evidence of plausibility only.

    Isolation: everything goes under src/data/simulated/. Only the review corpus src/data/corpora/simulated.json
    opens these stimuli (review_stimuli.py, participant_server.py --researcher-preview); no experiment configuration
    names it, and the study corpus requires "simulated": false. The records' `purpose` text ("served by no corpus
    until reviewed and approved") predates the review corpus and means no participant-facing corpus. The validation
    fixture (build_simulated_stimulus.py) is unchanged.

Inputs:
    src/data/injected_skills/manifest.json and src/data/execution/plan_injected_v1.json (the 12 items and their
    skills), src/data/scenario_selection_inputs.json (path and SHA-256 of each safe run), the safe run records,
    src/data/review/reference_recordings.json (assess_reference_recordings.py), and AgentDojo (.venv-agentdojo).

Outputs:
    src/data/simulated/runs/<item>__simulated-v1.json            the simulated run record of each item;
    src/data/simulated/stimuli/<item>/{low,high,provenance}.json, preview.txt   (build_stimuli.py);
    src/data/simulated/workspace/<item>/{workspace,provenance}.json             (build_workspace_snapshots.py);
    src/data/simulated/manifest.json                             one entry per item: inputs, outputs and hashes,
                                                                 inclusion verdict, status "built_unreviewed".
    The records, stimuli and manifest are deterministic; the researcher-only `after` state of a calendar-writing
    task's workspace.json holds the wall-clock time of the invitation email AgentDojo sends (as in the study
    snapshots), so the manifest records no hash of workspace.json. Rebuilding replaces an item's previous outputs
    (recoverable from git).

Dependencies:
    build_simulated_stimulus.py (load_inputs, build_record: the replay and the inclusion verdict), build_stimuli.py,
    build_workspace_snapshots.py, injection_checks.py, AgentDojo at the pinned commit.

Usage:
    .venv-agentdojo/bin/python -B src/scripts/build_simulated_items.py                 # build all 12 items
    .venv-agentdojo/bin/python -B src/scripts/build_simulated_items.py --items user_task_1__g3_ii_w1
    .venv-agentdojo/bin/python -B src/scripts/build_simulated_items.py --check         # exit 1 if a record is stale
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import build_simulated_stimulus as sim
import build_stimuli as bs
import build_workspace_snapshots as bw

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "src" / "data" / "simulated"
STUDY_MANIFEST = sim.MANIFEST
STUDY_PLAN = sim.PLAN
REFERENCE_REVIEW = ROOT / "src" / "data" / "review" / "reference_recordings.json"
BUILDER_VERSION = "simulated_items_v1"
RUN_SUFFIX = "__simulated-v1"
STATUS = "built_unreviewed"
CONSTRUCTION = {
    "attack_calls": "AgentDojo's minimal correct-solution calls (ground_truth) of the item's injection task, "
                    "with the item's attacker identity",
    "attack_position": "before the agent's first reply",
    "final_answer": "the safe run's final answer, unchanged",
    "legitimate_calls": "the safe run's calls, replayed; every result must equal the safe run's",
}
PURPOSE = ("candidate unsafe study stimulus: a constructed trace, not a real agent execution; served by no corpus "
           "until reviewed and approved")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def study_item_ids() -> list[str]:
    """The item ids of the study manifest, in its order."""
    return [i["item_id"] for i in json.loads(STUDY_MANIFEST.read_text(encoding="utf-8"))["items"]]


def reference_citations(scenario: str, objective: str, review: dict) -> list[dict]:
    """The recorded AgentDojo runs in which the attack and the user's task both succeeded for this scenario and
    objective (path, SHA-256, attack). Evidence of plausibility only; may be empty."""
    return [{"file": e["recording"]["file"], "sha256": e["recording"]["sha256"], "attack": e["attack"]}
            for e in review["recordings"]
            if e["scenario"] == scenario and e["objective"] == objective
            and e["outcome"] == "attack_and_task_succeeded"]


def build_item_record(item_id: str, review: dict) -> dict:
    """The simulated record of one study item: build_simulated_stimulus.build_record on the study plan only, then
    labelled as a candidate study stimulus (run id, purpose, material kind, construction policy, citations).
    The added fields are metadata; the trace and the inclusion verdict are build_record's."""
    record = sim.build_record(item_id, item_sources=((STUDY_MANIFEST, STUDY_PLAN),))
    record["run_id"] = f"{item_id}{RUN_SUFFIX}"
    record["material_kind"] = "simulated_trace"
    simulation = record["simulation"]
    simulation["purpose"] = PURPOSE
    simulation["builder_version"] = BUILDER_VERSION
    simulation["construction"] = CONSTRUCTION
    simulation["reference_recordings"] = {
        "review_file": rel(REFERENCE_REVIEW), "review_sha256": sha256_file(REFERENCE_REVIEW),
        "same_scenario_and_objective": reference_citations(record["scenario"], record["injection"]["objective"],
                                                           review),
    }
    return record


def render(record: dict) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def write_item(record: dict, out_dir: Path = OUT_DIR) -> dict:
    """Write one record, its stimulus and its workspace snapshot under out_dir; returns its manifest entry."""
    item_id = record["injection"]["item_id"]
    for sub in ("stimuli", "workspace"):
        shutil.rmtree(out_dir / sub / item_id, ignore_errors=True)  # generated outputs only; previous ones are in git
    runs_dir = out_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    record_path = runs_dir / f"{record['run_id']}.json"
    record_path.write_text(render(record), encoding="utf-8")
    built = bs.build_stimulus(record_path)
    stimulus_dir = bs.write_stimulus(built, out_dir / "stimuli")
    snapshot = bw.build_one(built["folder"], out_dir / "workspace", stimuli_dir=out_dir / "stimuli")
    item = next(i for i in json.loads(STUDY_MANIFEST.read_text(encoding="utf-8"))["items"] if i["item_id"] == item_id)
    verdict = record["simulation"]["inclusion_check"]
    return {
        "item_id": item_id, "scenario": record["scenario"], "objective": record["injection"]["objective"],
        "wording": item["wording"], "variant": item.get("variant"),
        "injected_skill": {"file": item["item_file"], "sha256": item["item_sha256"]},
        "safe_run": record["simulation"]["safe_run"],
        "record": {"file": rel(record_path), "sha256": sha256_file(record_path)},
        "stimulus": {"folder": rel(stimulus_dir),
                     "provenance_sha256": sha256_file(stimulus_dir / "provenance.json")},
        "workspace": {"folder": rel(out_dir / "workspace" / built["folder"]), "replayed_calls": snapshot["calls"]},
        "tool_calls": verdict["tool_calls"], "included": verdict["included"],
        "failed_checks": verdict["failed_checks"],
        "reference_recordings": len(record["simulation"]["reference_recordings"]["same_scenario_and_objective"]),
        "replay_notes": record["simulation"].get("replay_notes", []),
        "status": STATUS,
    }


def write_manifest(entries: list[dict], out_dir: Path = OUT_DIR) -> Path:
    """Write the manifest, keeping the entries of items not rebuilt this time, in the study manifest's order."""
    path = out_dir / "manifest.json"
    previous = json.loads(path.read_text(encoding="utf-8"))["items"] if path.exists() else []
    by_id = {e["item_id"]: e for e in previous} | {e["item_id"]: e for e in entries}
    manifest = {
        "builder_version": BUILDER_VERSION, "material_kind": "simulated_trace",
        "note": ("Constructed (simulated) unsafe stimuli, not real agent executions. Opened only by the review "
                 "corpus src/data/corpora/simulated.json, which no experiment configuration names; each needs a "
                 "manual review (review.json) and an approved study corpus before any participant sees it."),
        "construction": CONSTRUCTION,
        "sources": {rel(p): sha256_file(p) for p in (STUDY_MANIFEST, STUDY_PLAN, REFERENCE_REVIEW)},
        "items": [by_id[i] for i in study_item_ids() if i in by_id],
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main() -> None:
    if os.environ.get("PYTHONHASHSEED") != "0":
        # AgentDojo builds participant lists with set(); pinning the hash seed keeps the attack's own results stable.
        os.execve(sys.executable, [sys.executable, "-B", *sys.argv], {**os.environ, "PYTHONHASHSEED": "0"})
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--items", nargs="+", default=None, help="item ids (default: all 12 study items)")
    parser.add_argument("--check", action="store_true", help="rebuild the records in memory and compare; write nothing")
    args = parser.parse_args()
    item_ids = args.items or study_item_ids()
    unknown = sorted(set(item_ids) - set(study_item_ids()))
    if unknown:
        parser.error(f"not study items: {', '.join(unknown)}")
    review = json.loads(REFERENCE_REVIEW.read_text(encoding="utf-8"))
    if args.check:
        stale = [i for i in item_ids if not (OUT_DIR / "runs" / f"{i}{RUN_SUFFIX}.json").exists()
                 or (OUT_DIR / "runs" / f"{i}{RUN_SUFFIX}.json").read_text(encoding="utf-8")
                 != render(build_item_record(i, review))]
        print("stale or missing: " + ", ".join(stale) if stale else f"{len(item_ids)} records are current")
        sys.exit(1 if stale else 0)
    entries = [write_item(build_item_record(i, review)) for i in item_ids]
    write_manifest(entries)
    for e in entries:
        print(f"{e['item_id']}: included={e['included']} calls={e['tool_calls']} references={e['reference_recordings']}"
              + (f" notes={len(e['replay_notes'])}" if e["replay_notes"] else ""))


if __name__ == "__main__":
    main()
