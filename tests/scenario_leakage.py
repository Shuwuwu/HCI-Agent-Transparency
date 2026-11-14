"""
Purpose:
    Helpers for the tests that check that no scenario content has reached a skill source package or a prompt:
    they gather the material of the 25 experiment scenarios (prompts, tool outputs, agent messages) and find
    scenario literals (addresses, dates, times, file names, names, quoted text) in a text. Not a test module.

Inputs:
    The vendored no-attack runs, read through agentdojo_runs.py, and skill_text.py's literal extraction.

Outputs:
    Python values only.

Dependencies:
    src/scripts/agentdojo_runs.py, src/scripts/skill_text.py.

Usage:
    from scenario_leakage import leaked_literals, scenario_material, tool_text_of   (tests add tests/ to sys.path)
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import skill_text as validator  # noqa: E402

# Tasks whose no-attack run failed the task; every other task is one of the 25 experiment scenarios.
UTILITY_FALSE = {7, 11, 14, 18, 19, 20, 25, 30, 31, 32, 33, 34, 36, 37, 38}
SCENARIOS = [f"user_task_{n}" for n in sorted(set(range(40)) - UTILITY_FALSE)]


def scenario_material() -> tuple[dict[str, str], str]:
    """The 25 scenario prompts, and all tool outputs and agent messages of their no-attack runs as one text."""
    prompts = {}
    output_parts = []
    for user_task_id in SCENARIOS:
        run = runs.load_run(user_task_id)
        prompts[user_task_id] = runs.user_prompt(run)
        output_parts += [m["content"] for m in run["messages"] if m["role"] in ("tool", "assistant") and m["content"]]
    return prompts, "\n".join(output_parts)


def leaked_literals(text: str, prompts: dict[str, str], outputs: str, own_tool_text: str) -> list[str]:
    """Literals (addresses, dates, times, file names, names, quoted text) of the scenarios found in `text`.

    A literal that the tool descriptions themselves contain (for example a date format such as YYYY-MM-DD HH:MM,
    which is also quoted in some prompts) is not counted: it belongs to the tools, not to a scenario.
    """
    scenario_literals = set()
    for prompt in prompts.values():
        scenario_literals |= set().union(*validator.literals(prompt, with_quoted=True).values())
    scenario_literals |= set().union(*validator.literals(outputs).values())
    own = own_tool_text.lower()
    return validator.find_literals(text, {item for item in scenario_literals if item not in own})


def tool_text_of(package: dict) -> str:
    parts = []
    for tool in package["tools"]:
        parts += [tool["description"], *(p["description"] for p in tool["parameters"])]
    return "\n".join(parts)
