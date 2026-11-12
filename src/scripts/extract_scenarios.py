"""
Purpose:
    Build the list of the 40 AgentDojo workspace user-task scenarios from the recorded gpt-4o-2024-05-13
    no-attack runs, and flag which tasks qualify as "safe" study scenarios (the agent completed the task
    and nothing went wrong). This is the first step of scenario selection.

Inputs:
    src/data/gpt-4o-2024-05-13/workspace/user_task_N/none/none.json for every user task (read through
    agentdojo_runs.py). No network access is needed.

Outputs:
    src/data/gpt4o_workspace_scenarios.json with a "source" block and "User Task Scenarios": one entry
    per task with user_task_id, task_prompt, tools_called (grouped as email/calendar/drive), utility,
    error, is_combined_task, safe_candidate, and three complexity measures (n_tool_calls,
    n_failed_tool_calls, has_intermediate_messages). Output is deterministic (sorted by task number).

Dependencies:
    agentdojo_runs.py (shared run parsing and the tool-group table).

Usage:
    python3 src/scripts/extract_scenarios.py [--out PATH]
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs

DEFAULT_OUT = runs.REPO_ROOT / "src" / "data" / "gpt4o_workspace_scenarios.json"

SOURCE = {
    "repo": "https://github.com/ethz-spylab/agentdojo",
    "commit": "af7266303c748a9cf0442d71ceabc9d2488d7188",
    "pipeline": "gpt-4o-2024-05-13",
    "suite": "workspace",
    "attack": "none",
    "local_path": "src/data/gpt-4o-2024-05-13/workspace",
}


def build_scenario(user_task_id: str) -> dict:
    """Turn one no-attack run into a scenario entry.

    safe_candidate is true only when the run has no error and AgentDojo marked the task successful
    (utility is true). For no-attack runs `utility` is a clean measure of task success; the `security`
    value has no meaning without an attack, so it is deliberately not copied.
    """
    run = runs.load_run(user_task_id)
    prompt = runs.user_prompt(run)
    return {
        "user_task_id": run["user_task_id"],
        "task_prompt": prompt,
        "tools_called": runs.tools_called_by_group(run),
        "utility": run["utility"],
        "error": run["error"],
        "is_combined_task": prompt.startswith(runs.COMBINED_PREFIX),
        "safe_candidate": run["error"] is None and run["utility"] is True,
        "n_tool_calls": len(runs.tool_calls(run)),
        "n_failed_tool_calls": runs.failed_tool_call_count(run),
        "has_intermediate_messages": bool(runs.intermediate_agent_texts(run)),
    }


def build_output() -> dict:
    """Build the whole output document for every user task found locally."""
    return {
        "source": SOURCE,
        "User Task Scenarios": [build_scenario(task_id) for task_id in runs.list_user_task_ids()],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract workspace scenarios from local AgentDojo runs.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    output = build_output()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    scenarios = output["User Task Scenarios"]
    safe = sum(s["safe_candidate"] for s in scenarios)
    print(f"Wrote {len(scenarios)} scenarios ({safe} safe candidates) to {args.out}")


if __name__ == "__main__":
    main()
