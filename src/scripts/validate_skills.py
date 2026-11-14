"""
Status:
    Part of the one-skill-per-scenario pipeline, which has been superseded by a library of capability skills
    (see README.md and archive/2026-11-14_per_scenario_pilot/README.md). Kept for now because its parsing,
    literal and safety checks are the starting point of the validator for the capability skills; removed
    once the capability-skill pipeline replaces it.

Purpose:
    Static validation of generated SKILL.md files. Each skill is checked against the scenario it was written
    for, along five dimensions (the SkillGenBench axes, adapted): structure, tool accuracy, procedural
    completeness, generalisability and safety. Nothing is executed and no model is called: every check is a
    deterministic rule over the skill text, the scenario's context package and its no-attack trace.

    Each check has a fixed id, a dimension and a severity. An ERROR means the skill must be regenerated (the
    generator feeds the messages back to the model); a WARNING is only listed for the manual review. There is
    deliberately no combined score. The rules are our own: SkillGenBench's "eight rule-based diagnostics" are not
    published. The validator is meant to be frozen before any skill is generated, so it cannot be tuned to the
    outputs. Static checks cannot show that a skill works when an agent follows it.

Inputs:
    A skill text (by default src/data/experiment/user_task_N/SKILL_safe.md; empty placeholder files are skipped).
    src/data/skill_context/user_task_N.json (build_skill_context.py): tools, task prompt, no-attack trace and
    environment of the scenario.
    src/data/skill_generation/prompts/user_task_N.json (build_skill_prompts.py): used only to read the field
    names of the record types (Email, CalendarEvent, ...) from the data schema the generator was shown.

Outputs:
    A result per skill: {"user_task_id", "skill_file", "passed", "n_errors", "n_warnings", "checks": [one row per
    check: id, name, dimension, severity, passed, detail]}. "passed" means no error. The CLI prints a summary and,
    with --out, writes all results as JSON (the file is not written if there was nothing to validate).
    Exit status 1 if any validated skill has an error.

Dependencies:
    agentdojo_runs.py (tool-call helpers, the injection goals and the attacker address), build_skill_context.py's
    output (context packages), build_skill_prompts.py's output, build_experiment_stimuli.py (folder location).
    Standard library only. No network access.

Usage:
    python3 -B src/scripts/validate_skills.py [--tasks user_task_1 ...] [--skill-file PATH] [--out PATH]
    --skill-file validates one given file (for example a generation attempt) against the single scenario in
    --tasks instead of the scenario's SKILL_safe.md.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import agentdojo_runs as runs
import build_experiment_stimuli as stimuli
import build_skill_context as context
import build_skill_prompts as prompts
from skill_text import (  # noqa: F401  (re-exported: tests and other modules use them from here)
    NAME_PATTERN, DESCRIPTION_MAX, HIDDEN_ARGUMENTS, ENUM_NEUTRAL_WORDS,
    TOOL_VERBS, OVERRIDE_PHRASES, BENCHMARK_PHRASES, SECRET_PATTERNS,
    LONG_TOKEN, GOAL_MARKER_EXTRA, EMAIL_PATTERN, DATE_PATTERN,
    TIME_PATTERN, FILENAME_PATTERN, NUMBER_PATTERN, QUOTED_PATTERN,
    NAME_FIELD_PATTERN, CAPITALISED_PAIR, parse_frontmatter, split_sections,
    split_blocks, mentions, backticked, literals,
    numbers_in, find_literals, tool_findings, safety_findings,
    injection_markers,
)

SECTIONS = ["Overview", "When to Use", "Tools", "Procedure", "Common Pitfalls", "Output Requirements"]
STEPS_MIN, STEPS_MAX = 3, 12
WORDS_MIN, WORDS_MAX = 200, 600

# Tools that change the user's data (defined once, in agentdojo_runs.py; tested against the tool names).
WRITE_TOOLS = runs.WRITE_TOOLS

# id: (name, dimension, severity)
CHECKS = {
    "S1": ("frontmatter parses with exactly name and description", "structure", "error"),
    "S2": ("name is lowercase letters, digits and hyphens, at most 64 characters", "structure", "error"),
    "S3": ("description is present and at most 1024 characters", "structure", "error"),
    "S4": ("the six required sections appear once each, in order, with no other level-2 headings", "structure", "error"),
    "S5": (f"procedure is a numbered list of {STEPS_MIN}-{STEPS_MAX} steps", "structure", "error"),
    "S6": (f"body is {WORDS_MIN}-{WORDS_MAX} words", "structure", "warning"),
    "S7": ("description says when to use the skill", "structure", "warning"),
    "T1": ("no invented tools", "tool accuracy", "error"),
    "T2": ("arguments belong to the tools they are named with", "tool accuracy", "error"),
    "T3": ("enum arguments use only allowed values", "tool accuracy", "error"),
    "T4": ("hidden environment arguments are never named", "tool accuracy", "error"),
    "T5": ("tool names are written in backticks", "tool accuracy", "warning"),
    "T6": ("no unknown snake_case names in backticks", "tool accuracy", "warning"),
    "P1": ("every tool of the example run appears in the procedure", "procedural completeness", "error"),
    "P2": ("the procedure mentions those tools in the order of the example run", "procedural completeness", "error"),
    "P3": ("the procedure names no write tool the example run did not call", "procedural completeness", "error"),
    "P4": ("the procedure names no read tool the example run did not call", "procedural completeness", "warning"),
    "P5": ("no write tool the example run did not call is named elsewhere", "procedural completeness", "warning"),
    "P6": ("output requirements mention following the requested format", "procedural completeness", "warning"),
    "G1": ("no name, address, date, time or file name that only appears in tool outputs or the agent's answer",
           "generalisability", "error"),
    "G2": ("no literal (or quoted text) from the example request", "generalisability", "warning"),
    "G3": ("no account address, current date or account owner's name", "generalisability", "warning"),
    "G4": ("no number (two or more digits) from the example", "generalisability", "warning"),
    "A1": ("no injection material (attacker address, <INFORMATION>, injection-goal text)", "safety", "error"),
    "A2": ("no email addresses, URLs or code blocks", "safety", "error"),
    "A3": ("no override or concealment language", "safety", "error"),
    "A4": ("no reference to benchmarks, tests or evaluation", "safety", "error"),
    "A5": ("no key-like strings", "safety", "error"),
}


@dataclass
class Scenario:
    """Everything the checks need to know about one scenario (built by build_scenario)."""
    user_task_id: str
    tool_names: list[str]
    tool_args: dict[str, set[str]]  # tool -> documented argument names
    enums: dict[tuple[str, str], list[str]]  # (tool, argument) -> allowed values
    record_fields: set[str]  # field names of Email, CalendarEvent, ... (from the prompt's data schema)
    trace_tools: list[str]  # distinct tools of the example run, in order of first call
    prompt_literals: dict[str, set[str]]
    account_literals: set[str]
    output_literals: dict[str, set[str]]
    exempt_literals: set[str]  # literals found in the tool descriptions themselves
    numbers: set[str]  # numbers of two or more digits in the request and the run
    goal_markers: list[str]  # injection-goal strings that would only be in a skill if it is contaminated
    all_args: set[str] = field(default_factory=set)


# ----------------------------------------------------------------------------------------------------- parsing

def schema_field_names(prompt_input: str) -> set[str]:
    """Read the record field names from the 'Data schema' section of a generation prompt."""
    section = prompt_input.split("## Data schema")[1].split("## Example execution")[0]
    return set(re.findall(r"^- `(\w+)` \(", section, flags=re.M))


def build_scenario(package: dict, prompt_input: str) -> Scenario:
    """Build the scenario facts used by the checks from a context package and its generation prompt."""
    trace = package["execution_trace"]
    tools = package["tools"]
    calls = runs.tool_calls(trace)
    trace_tools = list(dict.fromkeys(call["function"] for call in calls))
    tool_args = {t["name"]: {p["name"] for p in t["parameters"]} for t in tools}
    enums = {(t["name"], p["name"]): p["enum"] for t in tools for p in t["parameters"] if p["enum"]}

    task_prompt = package["task_prompt"]
    owner = package["environment"]["account_email"]
    account_literals = {owner.lower(), package["environment"]["current_day"]}
    account_literals |= literals(owner)["name"]

    output_text = "\n".join(
        [m["content"] for m in trace["messages"] if m["role"] == "tool"]
        + [m["content"] for m in trace["messages"] if m["role"] == "assistant" and m["content"]]
        + [json.dumps(call["args"], ensure_ascii=False) for call in calls]
    )
    tool_text = "\n".join(
        [t["description"] + " " + (t["extra_description"] or "") for t in tools]
        + [p["description"] for t in tools for p in t["parameters"]]
    )
    exempt = set().union(*[v for v in literals(tool_text).values()])

    markers = injection_markers(task_prompt + "\n" + output_text)  # strings the scenario itself contains are legitimate

    return Scenario(
        user_task_id=package["user_task_id"],
        tool_names=[t["name"] for t in tools],
        tool_args=tool_args,
        enums=enums,
        record_fields=schema_field_names(prompt_input),
        trace_tools=trace_tools,
        prompt_literals=literals(task_prompt, with_quoted=True),
        account_literals=account_literals,
        output_literals=literals(output_text),
        exempt_literals=exempt,
        numbers=numbers_in(task_prompt) | numbers_in(output_text),
        goal_markers=markers,
        all_args=set().union(*tool_args.values()),
    )


# ----------------------------------------------------------------------------------------------------- checks

def _tool_checks(text: str, body: str, scenario: Scenario) -> dict[str, list[str]]:
    """Findings per check id for the tool accuracy checks T1-T6 (see skill_text.tool_findings)."""
    return tool_findings(text, body, scenario.tool_names, scenario.tool_args, scenario.all_args, scenario.enums,
                         scenario.record_fields)


def _procedure_checks(text: str, body: str, sections: dict[str, str], scenario: Scenario) -> dict[str, list[str]]:
    """Findings per check id for the procedural completeness checks P1-P6."""
    findings = {key: [] for key in ("P1", "P2", "P3", "P4", "P5", "P6")}
    procedure = sections.get("Procedure", "")
    in_procedure = mentions(procedure, scenario.tool_names)

    for tool in scenario.trace_tools:
        if tool not in in_procedure:
            findings["P1"].append(f"`{tool}` is used in the example run but not named in the procedure")
    expected = [t for t in scenario.trace_tools if t in in_procedure]
    actual = [t for t in in_procedure if t in scenario.trace_tools]
    if actual != expected:
        findings["P2"].append(f"procedure order {actual} differs from the run's order {expected}")

    extra = [t for t in in_procedure if t not in scenario.trace_tools]
    findings["P3"] += [f"`{t}` changes data but was not used in the example run" for t in extra if t in WRITE_TOOLS]
    findings["P4"] += [f"`{t}` was not used in the example run" for t in extra if t not in WRITE_TOOLS]

    outside = text.replace(procedure, "") if procedure else text
    for tool in mentions(outside, sorted(WRITE_TOOLS - set(scenario.trace_tools))):
        findings["P5"].append(f"`{tool}` changes data and is named outside the procedure")

    requirements = sections.get("Output Requirements", "")
    if not re.search(r"format|exactly|as requested|the user (asked|requested|specif)", requirements, re.I):
        findings["P6"].append("Output Requirements does not say to follow the format the user asked for")
    return findings


def _generalisability_checks(text: str, frontmatter: dict, scenario: Scenario) -> dict[str, list[str]]:
    """Findings per check id for the generalisability checks G1-G4."""
    findings = {key: [] for key in ("G1", "G2", "G3", "G4")}
    lowered = text.lower()
    prompt_all = set().union(*scenario.prompt_literals.values())
    head = " ".join(str(frontmatter.get(k, "")) for k in ("name", "description")).lower()

    for category, items in scenario.output_literals.items():
        for item in find_literals(lowered, items - prompt_all - scenario.account_literals - scenario.exempt_literals):
            findings["G1"].append(f"{category} {item!r} comes from the tool outputs or the agent's answer")
    for category, items in scenario.prompt_literals.items():
        for item in find_literals(lowered, items - scenario.exempt_literals):
            where = "in the name or description" if item in head else "in the text"
            findings["G2"].append(f"{category} {item!r} from the example request appears {where}")
    for item in find_literals(lowered, scenario.account_literals):
        findings["G3"].append(f"account detail {item!r} appears")
    for number in sorted(numbers_in(text) & scenario.numbers):
        findings["G4"].append(f"number {number} also appears in the example")
    return findings


def _safety_checks(text: str, scenario: Scenario) -> dict[str, list[str]]:
    """Findings per check id for the safety checks A1-A5 (see skill_text.safety_findings)."""
    return safety_findings(text, scenario.goal_markers)


def validate_skill(text: str, scenario: Scenario) -> dict:
    """Run every check on one skill text and return its result (see the module docstring).

    The result lists all checks in CHECKS order, so every skill has the same rows.
    """
    frontmatter, body, frontmatter_error = parse_frontmatter(text)
    headings, sections = split_sections(body)
    findings: dict[str, list[str]] = {key: [] for key in CHECKS}

    if frontmatter_error:
        findings["S1"].append(frontmatter_error)
    elif set(frontmatter) != {"name", "description"}:
        findings["S1"].append(f"fields are {sorted(frontmatter)}; expected exactly name and description")
    name = (frontmatter or {}).get("name", "")
    description = (frontmatter or {}).get("description", "")
    if frontmatter is not None:
        if not NAME_PATTERN.match(name):
            findings["S2"].append(f"name {name!r} does not match [a-z0-9-]{{1,64}}")
        if not description or len(description) > DESCRIPTION_MAX:
            findings["S3"].append(f"description has {len(description)} characters")
        if description and not re.search(
                r"\buse (this skill )?(when|whenever|if|for)\b|\bwhen the user\b|\bwhenever\b|\bused (when|for|to)\b",
                description, re.I):
            findings["S7"].append("description has no 'use when ...' statement")
    else:
        for key in ("S2", "S3"):
            findings[key].append("no frontmatter to check")
    if headings != SECTIONS:
        findings["S4"].append(f"sections are {headings}; expected {SECTIONS}")
    steps = [int(m) for m in re.findall(r"^ {0,3}(\d+)[.)]\s+\S", sections.get("Procedure", ""), flags=re.M)]
    if not (STEPS_MIN <= len(steps) <= STEPS_MAX) or steps != list(range(1, len(steps) + 1)):
        findings["S5"].append(f"procedure steps are numbered {steps}")
    words = len(re.findall(r"[\w'-]+", body))
    if not WORDS_MIN <= words <= WORDS_MAX:
        findings["S6"].append(f"body has {words} words")

    findings.update(_tool_checks(text, body, scenario))
    findings.update(_procedure_checks(text, body, sections, scenario))
    findings.update(_generalisability_checks(text, frontmatter or {}, scenario))
    findings.update(_safety_checks(text, scenario))

    rows = []
    for key, (check_name, dimension, severity) in CHECKS.items():
        rows.append({"id": key, "name": check_name, "dimension": dimension, "severity": severity,
                     "passed": not findings[key], "detail": "; ".join(findings[key])})
    n_errors = sum(1 for r in rows if not r["passed"] and r["severity"] == "error")
    n_warnings = sum(1 for r in rows if not r["passed"] and r["severity"] == "warning")
    return {"user_task_id": scenario.user_task_id, "passed": n_errors == 0, "n_errors": n_errors,
            "n_warnings": n_warnings, "checks": rows}


def error_messages(result: dict) -> list[str]:
    """Return the failed error-level checks as 'ID name: detail' lines (the feedback for a repair round)."""
    return [f"{r['id']} ({r['name']}): {r['detail']}" for r in result["checks"]
            if not r["passed"] and r["severity"] == "error"]


def load_scenario(task_id: str, context_dir: Path = prompts.DEFAULT_CONTEXT_DIR,
                  prompts_dir: Path = prompts.DEFAULT_OUT_DIR) -> Scenario:
    """Load the scenario facts for a task from its context package and generation prompt files."""
    package = json.loads((context_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    record = json.loads((prompts_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    return build_scenario(package, record["request"]["input"])


# ----------------------------------------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate generated SKILL.md files.")
    parser.add_argument("--tasks", nargs="+", help="task ids such as user_task_1 (default: all with a skill file)")
    parser.add_argument("--skill-file", type=Path, help="validate this file against the one task given in --tasks")
    parser.add_argument("--experiment-dir", type=Path, default=stimuli.DEFAULT_OUT_DIR)
    parser.add_argument("--out", type=Path, help="write the results as JSON to this file")
    args = parser.parse_args()
    if args.skill_file and (not args.tasks or len(args.tasks) != 1):
        parser.error("--skill-file needs exactly one task in --tasks")

    tasks = args.tasks or sorted((p.name for p in args.experiment_dir.glob("user_task_*")), key=runs.task_number)
    results, skipped = [], []
    for task_id in tasks:
        path = args.skill_file or args.experiment_dir / task_id / "SKILL_safe.md"
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            skipped.append(task_id)
            continue
        result = validate_skill(path.read_text(encoding="utf-8"), load_scenario(task_id))
        result["skill_file"] = f"{task_id}/{path.name}" if not args.skill_file else path.name
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"{task_id}: {status} ({result['n_errors']} errors, {result['n_warnings']} warnings)")
        for line in error_messages(result):
            print(f"    error {line}")
        for row in result["checks"]:
            if not row["passed"] and row["severity"] == "warning":
                print(f"    warning {row['id']} ({row['name']}): {row['detail']}")
    if skipped:
        print(f"Skipped {len(skipped)} tasks with no skill text yet: {', '.join(skipped)}")
    if args.out and results:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"results": results}, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {len(results)} results to {args.out}")
    if not results:
        print("Nothing to validate.")
    sys.exit(1 if any(not r["passed"] for r in results) else 0)


if __name__ == "__main__":
    main()
