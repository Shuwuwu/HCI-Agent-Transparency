"""
Purpose:
    Build the plan of an execution evaluation: the list of agent runs to execute, each fully specified (scenario,
    condition, skills, executor model, repetition), plus the way skills are shown to the agent. Keeping the plan in
    a file separates deciding what to run from running it, so the same runner can later execute more scenarios,
    models and repetitions without redesign, and the plan itself records the budget (a maximum number of runs).

    Conditions: "no-skills" (the agent gets AgentDojo's default system message only) and "mapped-skills" (the same
    message followed by the full text of every skill the taxonomy maps the scenario to, "oracle loading").

Inputs:
    src/data/skill_taxonomy.json (scenario map), src/data/skills/<skill>/SKILL.md and manifest.json, the
    recorded scenario prompts (through agentdojo_runs.py). Command-line choices: scenarios, conditions, models,
    repetitions, maximum number of runs.

Outputs:
    src/data/execution/plan.json (deterministic, no timestamps): benchmark and pipeline description, the skills block
    template, limits, and the runs. Run ids are `<scenario>__<condition>__<model>__r<repetition>`.

Dependencies:
    agentdojo_runs.py. Standard library only. Also provides render_skills_block and system_message, which the
    runner (run_executions.py) imports so the text the agent sees is built in one place.

Usage:
    python3 -B src/scripts/build_execution_plan.py --scenarios user_task_1 --models gpt-4o-2024-05-13
        [--conditions no-skills mapped-skills] [--repetitions 1] [--max-runs 2] [--out PATH]
"""

import argparse
import hashlib
import json
from pathlib import Path

import agentdojo_runs as runs

PLAN_VERSION = "execution_plan_v1"
SKILLS_BLOCK_VERSION = "skills_block_v1"
DEFAULT_PLAN = runs.REPO_ROOT / "src" / "data" / "execution" / "plan.json"
SKILLS_DIR = runs.REPO_ROOT / "src" / "data" / "skills"
TAXONOMY = runs.REPO_ROOT / "src" / "data" / "skill_taxonomy.json"
CONDITIONS = ("no-skills", "mapped-skills")
PINNED_COMMIT = "af7266303c748a9cf0442d71ceabc9d2488d7188"
DEFAULT_MAX_RUNS = 2  # the pilot budget; a larger evaluation must raise this on purpose

SKILLS_PREAMBLE = (
    "You have the following skills. Each describes how to carry out one kind of request with your tools. "
    "Use a skill when it matches the user's request."
)


def render_skills_block(skills: list[tuple[str, str]]) -> str:
    """The text appended to the system message for a list of (skill id, SKILL.md text) pairs (empty list: no text)."""
    if not skills:
        return ""
    blocks = [f'<skill name="{skill_id}">\n{text.strip()}\n</skill>' for skill_id, text in skills]
    return SKILLS_PREAMBLE + "\n\n" + "\n\n".join(blocks)


def system_message(default_message: str, skills: list[tuple[str, str]]) -> str:
    """AgentDojo's default system message, followed by the skills block when there are skills."""
    block = render_skills_block(skills)
    return default_message if not block else default_message.rstrip("\n") + "\n\n" + block


def run_id(scenario: str, condition: str, model: str, repetition: int) -> str:
    return f"{scenario}__{condition}__{model}__r{repetition}"


def skill_entries(skill_ids: list[str], skills_dir: Path) -> list[dict]:
    """The file and SHA-256 of each skill; a missing or empty skill is an error (the plan would be meaningless)."""
    entries = []
    for skill_id in skill_ids:
        path = skills_dir / skill_id / "SKILL.md"
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            raise FileNotFoundError(f"{path} is missing or empty; generate the skill first")
        entries.append({"id": skill_id, "file": f"src/data/skills/{skill_id}/SKILL.md",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return entries


def build_plan(scenarios: list[str], conditions: list[str], models: list[str], repetitions: int,
               max_runs: int, taxonomy: dict, skills_dir: Path = SKILLS_DIR) -> dict:
    """Return the plan. Raises ValueError for an unknown scenario or condition, or when the runs exceed max_runs."""
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        raise ValueError(f"unknown conditions {unknown}; choose from {list(CONDITIONS)}")
    missing = [s for s in scenarios if s not in taxonomy["scenario_map"]]
    if missing:
        raise ValueError(f"scenarios not in the taxonomy's scenario map: {missing}")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")
    planned = []
    for scenario in scenarios:
        prompt = runs.user_prompt(runs.load_run(scenario))
        for model in models:
            for repetition in range(1, repetitions + 1):
                for condition in conditions:
                    ids = taxonomy["scenario_map"][scenario]["skills"] if condition == "mapped-skills" else []
                    planned.append({
                        "run_id": run_id(scenario, condition, model, repetition), "scenario": scenario,
                        "task_prompt": prompt, "condition": condition, "model": model, "repetition": repetition,
                        "skills": skill_entries(ids, skills_dir),
                    })
    if len(planned) > max_runs:
        raise ValueError(f"the plan has {len(planned)} runs but the budget allows {max_runs}; raise --max-runs "
                         "only with approval")
    return {
        "plan_version": PLAN_VERSION,
        "benchmark": {
            "suite": "workspace", "agentdojo_commit": PINNED_COMMIT, "attack": "none",
            "pipeline": "AgentDojo default: system message, user query, OpenAI chat model with tools, tool loop "
                        "(no defence); temperature 0.0, at most 15 tool-loop iterations",
        },
        "skills_block": {"version": SKILLS_BLOCK_VERSION, "preamble": SKILLS_PREAMBLE,
                         "form": "oracle loading: the full text of every mapped skill is appended to the system "
                                 "message"},
        "limits": {"max_runs": max_runs},
        "runs": planned,
    }


def write_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the execution plan.")
    parser.add_argument("--scenarios", nargs="+", required=True, help="for example user_task_1")
    parser.add_argument("--conditions", nargs="+", default=list(CONDITIONS))
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-runs", type=int, default=DEFAULT_MAX_RUNS)
    parser.add_argument("--out", type=Path, default=DEFAULT_PLAN)
    args = parser.parse_args()
    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    try:
        plan = build_plan(args.scenarios, args.conditions, args.models, args.repetitions, args.max_runs, taxonomy)
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    write_plan(plan, args.out)
    print(f"Wrote {len(plan['runs'])} runs to {args.out}")
    for run in plan["runs"]:
        print(f"  {run['run_id']}  skills: {', '.join(s['id'] for s in run['skills']) or '-'}")


if __name__ == "__main__":
    main()
