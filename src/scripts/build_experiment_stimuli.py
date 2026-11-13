"""
Purpose:
    Create the folder of experiment stimuli: one sub-folder per safe-candidate user task, holding the task's
    metadata, its original no-attack execution trace, and empty placeholders for the files that still have
    to be designed (the participant-facing action log and the two SKILL.md documents).

Inputs:
    src/data/gpt4o_workspace_scenarios.json (written by extract_scenarios.py): the tasks flagged
    safe_candidate are the ones used.
    src/data/gpt-4o-2024-05-13/workspace/user_task_N/none/none.json: the recorded no-attack run of each task,
    read through agentdojo_runs.py.

Outputs:
    src/data/experiment/user_task_N/ for each safe-candidate task, containing:
        metadata.json                    the task's entry from gpt4o_workspace_scenarios.json, unchanged.
        no_attack_execution_trace.json   a byte-for-byte copy of the task's none.json (so its git hash equals
                                         AgentDojo's own file at the pinned commit).
        action_log.json                  EMPTY placeholder: the participant-facing log is still to be designed.
        SKILL_safe.md                    EMPTY placeholder: the legitimate-workflow skill is still to be written.
        SKILL_injected.md                EMPTY placeholder: the skill carrying the unauthorized instruction.
    metadata.json and the trace are derived data and are rewritten on every run. The three placeholders are
    only created when missing, so work done in them later is never overwritten by re-running this script.

Dependencies:
    agentdojo_runs.py (location of the run files), extract_scenarios.py's output file.

Usage:
    python3 -B src/scripts/build_experiment_stimuli.py [--scenarios PATH] [--out-dir PATH]
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs

DEFAULT_SCENARIOS = runs.REPO_ROOT / "src" / "data" / "gpt4o_workspace_scenarios.json"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "experiment"

METADATA_FILENAME = "metadata.json"
TRACE_FILENAME = "no_attack_execution_trace.json"
# Created empty, never overwritten (see module docstring).
PLACEHOLDER_FILENAMES = ("action_log.json", "SKILL_safe.md", "SKILL_injected.md")


def safe_scenarios(scenarios_path: Path) -> list[dict]:
    """Return the scenario entries flagged safe_candidate, in numeric task order, from the scenarios file."""
    document = json.loads(scenarios_path.read_text(encoding="utf-8"))
    safe = [s for s in document["User Task Scenarios"] if s["safe_candidate"]]
    return sorted(safe, key=lambda s: runs.task_number(s["user_task_id"]))


def build_task_folder(scenario: dict, out_dir: Path) -> list[Path]:
    """Write one task's folder and return the placeholder files that were newly created.

    metadata.json and the trace are always (re)written from their sources; a placeholder is created only
    if it does not exist yet, so a file that has since been filled in is left alone.
    """
    task_id = scenario["user_task_id"]
    folder = out_dir / task_id
    folder.mkdir(parents=True, exist_ok=True)

    (folder / METADATA_FILENAME).write_text(
        json.dumps(scenario, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Raw bytes, not parsed and re-dumped, so the copy is identical to the recorded file.
    (folder / TRACE_FILENAME).write_bytes((runs.RUNS_DIR / task_id / "none" / "none.json").read_bytes())

    created = []
    for filename in PLACEHOLDER_FILENAMES:
        placeholder = folder / filename
        if not placeholder.exists():
            placeholder.write_bytes(b"")
            created.append(placeholder)
    return created


def build_all(scenarios_path: Path = DEFAULT_SCENARIOS, out_dir: Path = DEFAULT_OUT_DIR) -> tuple[list[dict], list[Path]]:
    """Build the folder for every safe-candidate task. Returns (scenarios used, placeholders newly created)."""
    scenarios = safe_scenarios(scenarios_path)
    created = []
    for scenario in scenarios:
        created += build_task_folder(scenario, out_dir)
    return scenarios, created


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the experiment stimuli folders for the safe-candidate tasks.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    scenarios, created = build_all(args.scenarios, args.out_dir)
    print(f"Wrote {len(scenarios)} task folders to {args.out_dir} ({len(created)} empty placeholder files created)")


if __name__ == "__main__":
    main()
