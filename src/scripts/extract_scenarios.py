"""Extract gpt-4o-2024-05-13 workspace user-task scenarios from AgentDojo.

Reads AgentDojo's recorded no-attack runs (runs/<pipeline>/workspace/user_task_N/none/none.json)
and the workspace task-suite source, pinned to the commit of those runs, and writes one JSON file
of scenarios. Uses only the standard library; no AgentDojo code is imported or executed
(the source files are parsed with `ast`).

Fields per scenario (slice 1 — direct fields only):
  user_task_id, task_prompt, tools_called, task_difficulty, utility,
  is_combined_task, component_tasks

Usage:
  python3 src/scripts/extract_scenarios.py [--out PATH]
"""

import argparse
import ast
import json
import urllib.request
from pathlib import Path

AGENTDOJO_REPO = "ethz-spylab/agentdojo"
# Commit af72663 "Update GPT-4o results" (2024-06-05): the last commit that changed the
# gpt-4o-2024-05-13 run files, so task definitions are read as they were when gpt-4o was evaluated.
AGENTDOJO_COMMIT = "af7266303c748a9cf0442d71ceabc9d2488d7188"
PIPELINE = "gpt-4o-2024-05-13"
SUITE = "workspace"
# At this commit the package was still named `function_calling_pi` (later renamed `agentdojo`,
# where this suite became default_suites/v1/workspace).
SUITE_SRC = "src/function_calling_pi/task_suites/workspace"

# Tool-module name in AgentDojo -> key in our tools_called object.
TOOL_MODULE_TO_CATEGORY = {
    "email_client": "email_tools",
    "calendar_client": "calendar_tools",
    "cloud_drive_client": "drive_tools",
}

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "src" / "data" / "gpt4o_workspace_scenarios.json"


def fetch_text(path: str) -> str:
    url = f"https://raw.githubusercontent.com/{AGENTDOJO_REPO}/{AGENTDOJO_COMMIT}/{path}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def tool_categories(task_suite_src: str) -> dict[str, str]:
    """Map each workspace tool name to its category, from the suite's tool imports."""
    mapping = {}
    for node in ast.walk(ast.parse(task_suite_src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module.rsplit(".", 1)[-1]
            category = TOOL_MODULE_TO_CATEGORY.get(module)
            if category:
                for alias in node.names:
                    if alias.name[0].islower():  # skip classes like Inbox, Calendar
                        mapping[alias.name] = category
    return mapping


def task_metadata(user_tasks_src: str) -> dict[str, dict]:
    """Difficulty and combined-task info for every user task defined in user_tasks.py."""
    tasks = {}
    for node in ast.walk(ast.parse(user_tasks_src)):
        # Regular tasks: class UserTaskN(...): DIFFICULTY = TaskDifficulty.X
        if isinstance(node, ast.ClassDef) and node.name.startswith("UserTask"):
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "DIFFICULTY" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Attribute)
                ):
                    task_id = "user_task_" + node.name.removeprefix("UserTask")
                    tasks[task_id] = {
                        "task_difficulty": stmt.value.attr.lower(),
                        "is_combined_task": False,
                        "component_tasks": [],
                    }
        # Combined tasks: task_combinator.create_combined_task("UserTaskN", suite.user_tasks["a"], ...["b"])
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_combined_task"
        ):
            name, *components = node.args
            task_id = "user_task_" + name.value.removeprefix("UserTask")
            tasks[task_id] = {
                # TaskCombinator hard-codes DIFFICULTY = TaskDifficulty.MEDIUM for combined tasks.
                "task_difficulty": "medium",
                "is_combined_task": True,
                "component_tasks": [c.slice.value for c in components],
            }
    return tasks


def scenario_from_run(run: dict, categories: dict[str, str], meta: dict) -> dict:
    messages = run["messages"]
    user_msgs = [m for m in messages if m["role"] == "user"]
    if len(user_msgs) != 1:
        raise ValueError(f"{run['user_task_id']}: expected 1 user message, got {len(user_msgs)}")

    tools_called = {"email_tools": [], "calendar_tools": [], "drive_tools": []}
    for m in messages:
        for call in m.get("tool_calls") or []:
            name = call["function"]
            if name not in categories:
                raise ValueError(f"{run['user_task_id']}: unknown tool {name!r}")
            bucket = tools_called[categories[name]]
            if name not in bucket:  # actual call order, no repeats
                bucket.append(name)

    return {
        "user_task_id": run["user_task_id"],
        "task_prompt": user_msgs[0]["content"],
        "tools_called": tools_called,
        "task_difficulty": meta["task_difficulty"],
        "utility": run["utility"],
        "is_combined_task": meta["is_combined_task"],
        "component_tasks": meta["component_tasks"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    categories = tool_categories(fetch_text(f"{SUITE_SRC}/task_suite.py"))
    metadata = task_metadata(fetch_text(f"{SUITE_SRC}/user_tasks.py"))
    task_ids = sorted(metadata, key=lambda t: int(t.rsplit("_", 1)[1]))

    scenarios = []
    for task_id in task_ids:
        run = json.loads(fetch_text(f"runs/{PIPELINE}/{SUITE}/{task_id}/none/none.json"))
        if run["attack_type"] is not None or run["injection_task_id"] is not None:
            raise ValueError(f"{task_id}: expected a no-attack run")
        scenarios.append(scenario_from_run(run, categories, metadata[task_id]))
        print(f"  {task_id}: ok")

    output = {
        "source": {
            "repo": f"https://github.com/{AGENTDOJO_REPO}",
            "commit": AGENTDOJO_COMMIT,
            "pipeline": PIPELINE,
            "suite": SUITE,
            "attack": "none",
            "task_suite_source": SUITE_SRC,
        },
        "User Task Scenarios": scenarios,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=4, ensure_ascii=False) + "\n")
    print(f"Wrote {len(scenarios)} scenarios to {args.out}")


if __name__ == "__main__":
    main()
