"""
Purpose:
    Check the capability taxonomy in src/data/skill_taxonomy.json: that the skill library is internally
    consistent, that every one of the 25 experiment scenarios is mapped to skills that really cover what the
    scenario's recorded no-attack run did, and that the skill definitions (the text a skill generator will be
    shown) contain nothing taken from the scenarios.

    Library checks: every tool is known, every one of the 10 write tools is owned by exactly one skill, read skills
    own no write tool and write skills own at least one, the Yang et al. subtopics and tasks named by a skill
    exist in the Table 1 excerpt kept in the file, workflows are present and unique, every one of the 24 tools
    belongs to some skill.
    Map checks, per scenario: it is mapped to known skills with a rationale; every tool its run called belongs to
    a mapped skill; every data-changing call is owned by a mapped skill; every mapped skill is used (at least
    one of its tools was called). Across the map: no scenario is missing or extra, and every skill is mapped.
    Leakage check: the skill definitions share no literal (name, address, date, time, file name, quoted text)
    and no run of six or more words with any scenario prompt.
    The script also lists, as notes and not as failures, the tools of each skill that none of the 40 recorded
    no-attack runs called, so the workflows that no recorded run validates are visible.

Inputs:
    src/data/skill_taxonomy.json (hand-written), src/data/gpt4o_workspace_scenarios.json (which tasks are the
    25 safe candidates), and the recorded no-attack runs read through agentdojo_runs.py.

Outputs:
    A report on standard output; exit status 1 if any check fails. No files are written.

Dependencies:
    agentdojo_runs.py (TOOL_CATEGORY, WRITE_TOOLS, run loading) and validate_skills.py (literal extraction).
    Standard library only.

Usage:
    python3 -B src/scripts/check_skill_map.py [--taxonomy PATH] [--scenarios PATH]
"""

import argparse
import json
import re
import sys
from pathlib import Path

import agentdojo_runs as runs
import validate_skills as validator

DEFAULT_TAXONOMY = runs.REPO_ROOT / "src" / "data" / "skill_taxonomy.json"
DEFAULT_SCENARIOS = runs.REPO_ROOT / "src" / "data" / "gpt4o_workspace_scenarios.json"
SKILL_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_KEYS = {"kind", "summary", "yang", "workflows", "read_tools", "owned_write_tools"}
SEQUENCE_WORDS = 6  # a run of this many words shared with a scenario prompt counts as leakage


def load_taxonomy(path: Path = DEFAULT_TAXONOMY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def skill_tools(skill: dict) -> set[str]:
    """All tools a skill is defined with (read tools and owned write tools)."""
    return set(skill["read_tools"]) | set(skill["owned_write_tools"])


def scenario_ids(scenarios_path: Path = DEFAULT_SCENARIOS) -> list[str]:
    """The experiment scenarios: the tasks flagged safe_candidate, in numeric order."""
    document = json.loads(scenarios_path.read_text(encoding="utf-8"))
    ids = [s["user_task_id"] for s in document["User Task Scenarios"] if s["safe_candidate"]]
    return sorted(ids, key=runs.task_number)


# ---------------------------------------------------------------------------------------------- library checks

def check_taxonomy(taxonomy: dict) -> list[str]:
    """Return the problems found in the library of skills (an empty list means it is consistent)."""
    problems = []
    for key in ("version", "source", "yang_reference", "skills", "scenario_map"):
        if key not in taxonomy:
            problems.append(f"missing top-level key {key!r}")
    if problems:
        return problems

    reference, skills = taxonomy["yang_reference"], taxonomy["skills"]
    owners: dict[str, list[str]] = {}
    covered: set[str] = set()
    for skill_id, skill in skills.items():
        where = f"skill {skill_id!r}"
        if not SKILL_ID.match(skill_id):
            problems.append(f"{where}: id must be lowercase words joined by hyphens")
        if set(skill) != SKILL_KEYS:
            problems.append(f"{where}: keys are {sorted(skill)}, expected {sorted(SKILL_KEYS)}")
            continue
        if skill["kind"] not in ("read", "write"):
            problems.append(f"{where}: kind must be 'read' or 'write'")
        if not skill["summary"].strip():
            problems.append(f"{where}: empty summary")
        for tool in skill_tools(skill):
            if tool not in runs.TOOL_CATEGORY:
                problems.append(f"{where}: unknown tool {tool!r}")
        for listed in (skill["read_tools"], skill["owned_write_tools"]):
            if len(set(listed)) != len(listed):
                problems.append(f"{where}: a tool is listed twice")
        for tool in skill["read_tools"]:
            if tool in runs.WRITE_TOOLS:
                problems.append(f"{where}: write tool {tool!r} listed as a read tool")
        for tool in skill["owned_write_tools"]:
            if tool not in runs.WRITE_TOOLS:
                problems.append(f"{where}: {tool!r} is not a write tool but is listed as owned")
            owners.setdefault(tool, []).append(skill_id)
        if (skill["kind"] == "write") != bool(skill["owned_write_tools"]):
            problems.append(f"{where}: a write skill must own write tools and a read skill must own none")
        covered |= skill_tools(skill)

        if not skill["yang"]:
            problems.append(f"{where}: no Yang et al. subtopic")
        for entry in skill["yang"]:
            if entry["subtopic"] not in reference:
                problems.append(f"{where}: subtopic {entry['subtopic']!r} is not in the Yang et al. reference")
                continue
            for task in entry["tasks"]:
                if task not in reference[entry["subtopic"]]:
                    problems.append(f"{where}: task {task!r} is not listed under {entry['subtopic']!r}")
        workflow_ids = [w.get("id", "") for w in skill["workflows"]]
        if not skill["workflows"]:
            problems.append(f"{where}: no workflows")
        if len(set(workflow_ids)) != len(workflow_ids) or not all(workflow_ids):
            problems.append(f"{where}: workflow ids must be present and unique")
        if any(not w.get("description", "").strip() for w in skill["workflows"]):
            problems.append(f"{where}: a workflow has no description")

    for tool in sorted(runs.WRITE_TOOLS):
        if len(owners.get(tool, [])) != 1:
            problems.append(f"write tool {tool!r} must be owned by exactly one skill, found {owners.get(tool, [])}")
    for tool in sorted(set(runs.TOOL_CATEGORY) - covered):
        problems.append(f"tool {tool!r} belongs to no skill")
    return problems


# ---------------------------------------------------------------------------------------------- map checks

def check_scenario(taxonomy: dict, user_task_id: str, called: list[str]) -> list[str]:
    """Return the problems with one scenario's mapping, given the tools its recorded run called."""
    skills = taxonomy["skills"]
    entry = taxonomy["scenario_map"].get(user_task_id)
    if entry is None:
        return [f"{user_task_id}: not in the scenario map"]
    problems = []
    mapped = entry.get("skills", [])
    if not mapped or len(set(mapped)) != len(mapped):
        problems.append(f"{user_task_id}: needs one or more distinct skills, found {mapped}")
    if not entry.get("rationale", "").strip():
        problems.append(f"{user_task_id}: no rationale")
    unknown = [s for s in mapped if s not in skills]
    for skill_id in unknown:
        problems.append(f"{user_task_id}: unknown skill {skill_id!r}")
    known = [skills[s] for s in mapped if s in skills]
    if unknown:
        return problems

    available = set().union(*(skill_tools(s) for s in known)) if known else set()
    for tool in sorted(set(called) - available):
        problems.append(f"{user_task_id}: tool {tool!r} is called but no mapped skill has it")
    for tool in sorted(set(called) & runs.WRITE_TOOLS):
        owner = next((sid for sid, s in skills.items() if tool in s["owned_write_tools"]), None)
        if owner not in mapped:
            problems.append(f"{user_task_id}: data-changing call {tool!r} needs skill {owner!r}")
    for skill_id in mapped:
        if not set(called) & skill_tools(skills[skill_id]):
            problems.append(f"{user_task_id}: mapped skill {skill_id!r} is not used (none of its tools was called)")
    return problems


def check_map(taxonomy: dict, ids: list[str], calls_by_scenario: dict[str, list[str]]) -> list[str]:
    """Check the whole scenario map against the expected scenarios and their recorded tool calls."""
    problems = []
    mapped_ids = set(taxonomy["scenario_map"])
    for user_task_id in sorted(set(ids) - mapped_ids, key=runs.task_number):
        problems.append(f"{user_task_id}: scenario has no mapping")
    for user_task_id in sorted(mapped_ids - set(ids), key=runs.task_number):
        problems.append(f"{user_task_id}: mapped but not one of the experiment scenarios")
    for user_task_id in ids:
        if user_task_id in mapped_ids:
            problems += check_scenario(taxonomy, user_task_id, calls_by_scenario[user_task_id])
    used = {s for entry in taxonomy["scenario_map"].values() for s in entry.get("skills", [])}
    for skill_id in taxonomy["skills"]:
        if skill_id not in used:
            problems.append(f"skill {skill_id!r} is not used by any scenario")
    return problems


def unvalidated_tools(taxonomy: dict, called_anywhere: set[str]) -> dict[str, list[str]]:
    """For each skill, the tools no recorded no-attack run called (workflows only these tools support are untested)."""
    return {
        skill_id: sorted(skill_tools(skill) - called_anywhere)
        for skill_id, skill in taxonomy["skills"].items()
        if skill_tools(skill) - called_anywhere
    }


# ---------------------------------------------------------------------------------------------- leakage lint

def definition_text(taxonomy: dict) -> str:
    """All text of the skills section a generator would be shown (summaries and workflow descriptions)."""
    parts = []
    for skill_id, skill in taxonomy["skills"].items():
        parts += [skill_id.replace("-", " "), skill["summary"]]
        parts += [w["description"] for w in skill["workflows"]]
    return "\n".join(parts)


def shared_sequences(text: str, other: str, length: int = SEQUENCE_WORDS) -> list[str]:
    """Return the runs of `length` consecutive words (lower-cased, punctuation ignored) that occur in both texts."""
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9']+", value.lower())

    def runs_of(items: list[str]) -> set[tuple[str, ...]]:
        return {tuple(items[i:i + length]) for i in range(len(items) - length + 1)}

    return sorted(" ".join(run) for run in runs_of(words(text)) & runs_of(words(other)))


def lint_definitions(taxonomy: dict, prompts: dict[str, str]) -> list[str]:
    """Return the scenario content found in the skill definitions (an empty list means none)."""
    text = definition_text(taxonomy)
    problems = []
    for user_task_id, prompt in prompts.items():
        for category, items in validator.literals(prompt, with_quoted=True).items():
            for item in validator.find_literals(text, items):
                problems.append(f"definitions contain {category} {item!r} from {user_task_id}")
        for sequence in shared_sequences(text, prompt):
            problems.append(f"definitions share the word run {sequence!r} with {user_task_id}")
    return problems


# ---------------------------------------------------------------------------------------------- command line

def load_recorded_calls(ids: list[str]) -> dict[str, list[str]]:
    """Map each scenario id to the tools its recorded no-attack run called, in order."""
    return {i: [c["function"] for c in runs.tool_calls(runs.load_run(i))] for i in ids}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the skill taxonomy and its scenario map.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    args = parser.parse_args()

    taxonomy = load_taxonomy(args.taxonomy)
    ids = scenario_ids(args.scenarios)
    problems = check_taxonomy(taxonomy)
    if problems:  # the map checks need a consistent library
        print("\n".join(f"problem: {p}" for p in problems))
        sys.exit(1)
    calls = load_recorded_calls(ids)
    prompts = {i: runs.user_prompt(runs.load_run(i)) for i in ids}
    problems = check_map(taxonomy, ids, calls) + lint_definitions(taxonomy, prompts)

    for user_task_id in ids:
        entry = taxonomy["scenario_map"].get(user_task_id, {"skills": []})
        print(f"{user_task_id:13} {', '.join(entry['skills']):62} {', '.join(dict.fromkeys(calls[user_task_id]))}")
    usage = {s: sum(s in e["skills"] for e in taxonomy["scenario_map"].values()) for s in taxonomy["skills"]}
    print("\nScenarios per skill:", ", ".join(f"{s} {n}" for s, n in usage.items()))

    all_called = {c["function"] for i in runs.list_user_task_ids() for c in runs.tool_calls(runs.load_run(i))}
    for skill_id, tools in unvalidated_tools(taxonomy, all_called).items():
        print(f"note: {skill_id}: no recorded run called {', '.join(tools)}")
    if problems:
        print("\n" + "\n".join(f"problem: {p}" for p in problems))
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
