"""
Purpose:
    Create the folder of experiment stimuli: one sub-folder per safe-candidate user task, holding the task's
    metadata, its original no-attack execution trace, and the list of capability skills the task uses. (The
    participant-facing action log is no longer a placeholder here: it is built per execution into
    src/data/stimuli/ by build_stimuli.py.)

Inputs:
    src/data/gpt4o_workspace_scenarios.json (written by extract_scenarios.py): the tasks flagged
    safe_candidate are the ones used.
    src/data/gpt-4o-2024-05-13/workspace/user_task_N/none/none.json: the recorded no-attack run of each task,
    read through agentdojo_runs.py.
    src/data/skill_taxonomy.json: the scenario map, which says which skills each task uses (checked against
    the traces by check_skill_map.py).

Outputs:
    src/data/experiment/user_task_N/ for each safe-candidate task, containing:
        metadata.json                    the task's entry from gpt4o_workspace_scenarios.json, unchanged.
        no_attack_execution_trace.json   a byte-for-byte copy of the task's none.json (so its git hash equals
                                         AgentDojo's own file at the pinned commit).
        skills.json                      {"taxonomy_version", "skills": [skill ids]} from the scenario map. The
                                         skills themselves live in src/data/skills/ (once generated), shared by
                                         all tasks; they are not copied into the task folders.
    All three files are derived data and are rewritten on every run. (Earlier versions also created an empty
    action_log.json placeholder in each folder; it was superseded by src/data/stimuli/ and removed, see
    archive/README.md.)

Dependencies:
    agentdojo_runs.py (location of the run files), extract_scenarios.py's output file, skill_taxonomy.json.

Usage:
    python3 -B src/scripts/build_experiment_stimuli.py [--scenarios PATH] [--out-dir PATH] [--taxonomy PATH]
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs

DEFAULT_SCENARIOS = runs.REPO_ROOT / "src" / "data" / "gpt4o_workspace_scenarios.json"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "experiment"
DEFAULT_TAXONOMY = runs.REPO_ROOT / "src" / "data" / "skill_taxonomy.json"

METADATA_FILENAME = "metadata.json"
TRACE_FILENAME = "no_attack_execution_trace.json"
SKILLS_FILENAME = "skills.json"


def safe_scenarios(scenarios_path: Path) -> list[dict]:
    """Return the scenario entries flagged safe_candidate, in numeric task order, from the scenarios file."""
    document = json.loads(scenarios_path.read_text(encoding="utf-8"))
    safe = [s for s in document["User Task Scenarios"] if s["safe_candidate"]]
    return sorted(safe, key=lambda s: runs.task_number(s["user_task_id"]))


def scenario_skills(taxonomy: dict, user_task_id: str) -> dict:
    """Return the skills.json content of one task: the taxonomy version and the skill ids the task uses.

    Raises:
        ValueError: if the task is not in the taxonomy's scenario map.
    """
    entry = taxonomy["scenario_map"].get(user_task_id)
    if entry is None:
        raise ValueError(f"{user_task_id} is not in the scenario map of the skill taxonomy")
    return {"taxonomy_version": taxonomy["version"], "skills": list(entry["skills"])}


def build_task_folder(scenario: dict, out_dir: Path, taxonomy: dict) -> None:
    """Write one task's folder; metadata.json, the trace and skills.json are always (re)written from their sources."""
    task_id = scenario["user_task_id"]
    folder = out_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)

    (folder / METADATA_FILENAME).write_text(
        json.dumps(scenario, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Raw bytes, not parsed and re-dumped, so the copy is identical to the recorded file.
    (folder / TRACE_FILENAME).write_bytes((runs.RUNS_DIR / task_id / "none" / "none.json").read_bytes())
    (folder / SKILLS_FILENAME).write_text(
        json.dumps(scenario_skills(taxonomy, task_id), indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def build_all(scenarios_path: Path = DEFAULT_SCENARIOS, out_dir: Path = DEFAULT_OUT_DIR,
              taxonomy_path: Path = DEFAULT_TAXONOMY) -> list[dict]:
    """Build the folder for every safe-candidate task. Returns the scenarios used."""
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    scenarios = safe_scenarios(scenarios_path)
    for scenario in scenarios:
        build_task_folder(scenario, out_dir, taxonomy)
    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the experiment stimuli folders for the safe-candidate tasks.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    args = parser.parse_args()

    scenarios = build_all(args.scenarios, args.out_dir, args.taxonomy)
    print(f"Wrote {len(scenarios)} task folders to {args.out_dir}")


if __name__ == "__main__":
    main()
