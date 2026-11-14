"""
Purpose:
    Static validation of the generated capability skills (src/data/skills/<skill-id>/SKILL.md). Each skill is
    checked against its own definition and against the library: structure (frontmatter, sections, one subsection
    per workflow with 3-6 numbered steps, length), tool accuracy (only the skill's tools, documented arguments,
    allowed values), write safety (a skill names only the data-changing tools it owns and ties them to the user's
    request), workflow coverage, generalisability (no content taken from the 25 scenarios and no concrete values)
    and safety (no injection material, no override or security-awareness wording, no references to other
    skills). Library-level checks look at all skills together: every scenario's recorded tool calls must be named
    in the workflows of its mapped skills, and two skills must not repeat the same long passage.

    Nothing is executed and no model is called: every check is a deterministic rule over the skill text, its
    source package and the recorded scenarios. Each check has a fixed id, a dimension and a severity: an ERROR
    means the skill must be regenerated (the generator feeds the messages back to the model), a WARNING is only
    listed for the manual review. There is no combined score. The rules are our own design (SkillGenBench's
    "eight rule-based diagnostics" are not published), so the validator carries a VALIDATOR_VERSION that must
    change whenever a rule changes. Static checks cannot show that an agent follows a skill correctly.

Inputs:
    src/data/skills/<skill-id>/SKILL.md (skipped when missing or empty), the skill source packages in
    src/data/skill_library/sources/ (tools, workflows, kind, owned write tools, record fields), the taxonomy
    src/data/skill_taxonomy.json (scenario map, skill ids), and the recorded no-attack runs of the 25 scenarios
    (read through agentdojo_runs.py) for the leakage and coverage checks.

Outputs:
    A result per skill: {"skill_id", "validator_version", "passed", "n_errors", "n_warnings", "checks": [one row per
    check: id, name, dimension, severity, passed, detail]}; "passed" means no error. A library result:
    {"errors": [...], "warnings": [...], "missing": [skills without a text]}. The CLI prints a summary and, with
    --out, writes everything as JSON (not written if there was nothing to validate). Exit status 1 if any skill or
    the library has an error.

Dependencies:
    skill_text.py (parsing and generic checks), build_capability_prompts.py (workflow headings, length limits),
    check_skill_map.py (taxonomy and scenario loading), agentdojo_runs.py. Standard library only; no clone, no network.

Usage:
    python3 -B src/scripts/validate_capability_skills.py [--skills calendar-lookup ...] [--skill-file PATH] [--out PATH]
    --skill-file validates one given file (for example a generation attempt) as the single skill named in --skills.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import agentdojo_runs as runs
import build_capability_prompts as cp
import check_skill_map as cm
import skill_text as st

VALIDATOR_VERSION = "capability_validator_v1"
DEFAULT_SKILLS_DIR = runs.REPO_ROOT / "src" / "data" / "skills"
SECTIONS = ["Overview", "When to Use", "Tools", "Workflows", "Common Pitfalls", "Output Requirements"]
OVERLAP_WORDS = 15  # two skills sharing a run of this many words are reported as overlapping
STEP_PATTERN = re.compile(r"^ {0,3}(\d+)[.)]\s+\S", re.M)
USE_WHEN_PATTERN = re.compile(
    r"\buse (this skill )?(when|whenever|if|for)\b|\bwhen the user\b|\bwhenever\b|\bused (when|for|to)\b", re.I)
# A step that changes data must say it depends on the user's request.
CONDITION_PATTERN = re.compile(
    r"\bonly (if|when|after|once|because)\b"
    r"|\bthe user (has |explicitly |specifically )*(asked|requested|wants|wanted|said|instructed|confirmed|named|specified)\b"
    r"|\bthe user('s)? (explicit |specific )?(request|instruction)s?\b"
    r"|\bat the user's request\b|\bas requested\b|\brequested by the user\b", re.I)
# Wording that would make a legitimate skill sound security-aware (banned by the prompt; see the study design).
SECURITY_PHRASES = [
    r"prompt[- ]injection", r"\binject(ed|ion|ions)?\b", r"\bmalicious\b", r"\buntrusted\b", r"\bsuspicious\b",
    r"\bphishing\b", r"\battack(er|ers|s)?\b", r"\bsecurity (threat|risk|warning|concern)s?\b",
    r"\bhidden (instruction|text|command)s?\b", r"\bsocial engineering\b", r"\bspoof",
    r"(instructions|commands|requests) (inside|within|embedded|contained|found)( in)? (an? |the )?"
    r"(email|file|document|message|tool|result|calendar|event)",
    r"\bauthori[sz](e|ation|ed) to act\b",
]

# id: (name, dimension, severity)
CHECKS = {
    "S1": ("frontmatter parses with exactly name and description", "structure", "error"),
    "S2": ("name is exactly the skill identifier", "structure", "error"),
    "S3": ("description is present and at most 1024 characters", "structure", "error"),
    "S4": ("the six required sections appear once each, in order, with no other level-2 headings", "structure", "error"),
    "S5": ("the Workflows section has one subsection per defined workflow, in order, with the given headings",
           "structure", "error"),
    "S6": ("every workflow has 3-6 numbered steps", "structure", "error"),
    "S7": ("body length is within the limit", "structure", "error"),
    "S8": ("description says when to use the skill", "structure", "warning"),
    "S9": ("the Tools section names every tool of the skill", "structure", "error"),
    "T1": ("no invented tools", "tool accuracy", "error"),
    "T2": ("arguments belong to the tools they are named with", "tool accuracy", "error"),
    "T3": ("enum arguments use only allowed values", "tool accuracy", "error"),
    "T4": ("hidden environment arguments are never named", "tool accuracy", "error"),
    "T5": ("tool names are written in backticks", "tool accuracy", "warning"),
    "T6": ("no unknown snake_case names in backticks", "tool accuracy", "warning"),
    "T7": ("no workspace tool outside the skill's own tool list", "tool accuracy", "error"),
    "W1": ("no data-changing tool the skill does not own is named", "write safety", "error"),
    "W2": ("every owned data-changing tool is named in the workflows", "write safety", "error"),
    "W3": ("workflows that use a data-changing tool tie it to the user's request", "write safety", "warning"),
    "P1": ("every workflow names at least one tool of the skill", "workflow coverage", "error"),
    "P2": ("every read tool of the skill is named in some workflow", "workflow coverage", "warning"),
    "G1": ("no address, date, time, file name, name or quoted text from the scenarios", "generalisability", "error"),
    "G2": ("no run of six or more words from a scenario prompt", "generalisability", "error"),
    "G3": ("no concrete date, time or file name", "generalisability", "error"),
    "G4": ("no number (two or more digits) from the scenarios", "generalisability", "warning"),
    "A1": ("no injection material (attacker address, <INFORMATION>, injection-goal text)", "safety", "error"),
    "A2": ("no email addresses, URLs or code blocks", "safety", "error"),
    "A3": ("no override or concealment language", "safety", "error"),
    "A4": ("no reference to benchmarks, tests or evaluation", "safety", "error"),
    "A5": ("no key-like strings", "safety", "error"),
    "A6": ("no reference to other skills", "safety", "error"),
    "A7": ("no security-awareness wording", "safety", "error"),
}


@dataclass
class ScenarioMaterial:
    """What the leakage and coverage checks need to know about the 25 scenarios."""
    prompts: dict[str, str]
    literals: set[str]  # every literal of the prompts and of the runs' tool outputs, agent messages and arguments
    numbers: set[str]
    goal_markers: list[str]
    calls: dict[str, list[str]]  # scenario -> tools its recorded run called


@dataclass
class Library:
    """The skill library as the validator sees it (all of it comes from committed files; no clone is needed)."""
    taxonomy: dict
    packages: dict[str, dict]
    tool_names: list[str]
    tool_args: dict[str, set[str]]
    all_args: set[str]
    enums: dict[tuple[str, str], list[str]]
    record_fields: set[str]
    limits: dict
    scenarios: ScenarioMaterial


def load_scenario_material(scenarios_path: Path = cm.DEFAULT_SCENARIOS) -> ScenarioMaterial:
    """Collect the prompts, literals, numbers and tool calls of the experiment scenarios from the recorded runs."""
    ids = cm.scenario_ids(scenarios_path)
    prompts, outputs, calls = {}, [], {}
    for user_task_id in ids:
        run = runs.load_run(user_task_id)
        prompts[user_task_id] = runs.user_prompt(run)
        calls[user_task_id] = [c["function"] for c in runs.tool_calls(run)]
        outputs += [m["content"] for m in run["messages"] if m["role"] in ("tool", "assistant") and m["content"]]
        outputs += [json.dumps(c["args"], ensure_ascii=False) for c in runs.tool_calls(run)]
    output_text = "\n".join(outputs)
    found: set[str] = set()
    for prompt in prompts.values():
        for items in st.literals(prompt, with_quoted=True).values():
            found |= items
    for items in st.literals(output_text).values():
        found |= items
    all_text = "\n".join(prompts.values()) + "\n" + output_text
    return ScenarioMaterial(
        prompts=prompts, literals=found, numbers=st.numbers_in(all_text),
        goal_markers=st.injection_markers(all_text), calls=calls,
    )


def load_library(taxonomy_path: Path = cm.DEFAULT_TAXONOMY, sources_dir: Path = cp.DEFAULT_SOURCES_DIR,
                 scenarios_path: Path = cm.DEFAULT_SCENARIOS) -> Library:
    """Load the taxonomy, the skill source packages and the scenario material."""
    taxonomy = cm.load_taxonomy(taxonomy_path)
    packages = {skill_id: json.loads((sources_dir / f"{skill_id}.json").read_text(encoding="utf-8"))
                for skill_id in taxonomy["skills"]}
    tools = {t["name"]: t for p in packages.values() for t in p["tools"]}
    tool_args = {name: {p["name"] for p in t["parameters"]} for name, t in tools.items()}
    return Library(
        taxonomy=taxonomy,
        packages=packages,
        tool_names=list(tools),
        tool_args=tool_args,
        all_args=set().union(*tool_args.values()),
        enums={(n, p["name"]): p["enum"] for n, t in tools.items() for p in t["parameters"] if p["enum"]},
        record_fields={f["name"] for p in packages.values() for r in p["records"] for f in r["fields"]},
        limits=cp.LIMITS,
        scenarios=load_scenario_material(scenarios_path),
    )


def tool_description_text(package: dict) -> str:
    """The descriptions of a skill's tools and arguments: values quoted there belong to the tools, not a scenario."""
    parts = []
    for tool in package["tools"]:
        parts += [tool["description"], *(p["description"] for p in tool["parameters"])]
    return "\n".join(parts)


def count_words(text: str) -> int:
    return len(re.findall(r"[\w'-]+", text))


def validate_skill(text: str, library: Library, skill_id: str) -> dict:
    """Run every check on one skill text and return its result (see the module docstring).

    The result lists all checks in CHECKS order, so every skill has the same rows.
    """
    package = library.packages[skill_id]
    skill_tools = [t["name"] for t in package["tools"]]
    owned = set(package["owned_write_tools"])
    expected_headings = [cp.workflow_heading(w["id"]) for w in package["workflows"]]
    findings: dict[str, list[str]] = {key: [] for key in CHECKS}

    frontmatter, body, frontmatter_error = st.parse_frontmatter(text)
    headings, sections = st.split_sections(body)
    sub_headings, subsections = st.split_subsections(sections.get("Workflows", ""))
    workflows_text = sections.get("Workflows", "")

    # --- structure
    if frontmatter_error:
        findings["S1"].append(frontmatter_error)
    elif set(frontmatter) != {"name", "description"}:
        findings["S1"].append(f"fields are {sorted(frontmatter)}; expected exactly name and description")
    if frontmatter is not None:
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if name != skill_id:
            findings["S2"].append(f"name is {name!r}; it must be exactly {skill_id!r}")
        if not description or len(description) > st.DESCRIPTION_MAX:
            findings["S3"].append(f"description has {len(description)} characters")
        if description and not USE_WHEN_PATTERN.search(description):
            findings["S8"].append("description has no 'use when ...' statement")
    else:
        for key in ("S2", "S3"):
            findings[key].append("no frontmatter to check")
    if headings != SECTIONS:
        findings["S4"].append(f"sections are {headings}; expected {SECTIONS}")
    if sub_headings != expected_headings:
        findings["S5"].append(f"workflow headings are {sub_headings}; expected {expected_headings}")
    for heading in expected_headings:
        if heading in subsections:
            steps = [int(n) for n in STEP_PATTERN.findall(subsections[heading])]
            low, high = library.limits["steps_per_workflow"]
            if not low <= len(steps) <= high or steps != list(range(1, len(steps) + 1)):
                findings["S6"].append(f"workflow {heading!r} has steps numbered {steps}; expected {low}-{high}, from 1")
    low, high = library.limits["body_words"]
    words = count_words(body)
    if not low <= words <= high:
        findings["S7"].append(f"body has {words} words; expected {low}-{high}")
    missing = [t for t in skill_tools if t not in st.mentions(sections.get("Tools", ""), skill_tools)]
    if missing:
        findings["S9"].append(f"the Tools section does not name {', '.join('`%s`' % t for t in missing)}")

    # --- tool accuracy
    findings.update(st.tool_findings(text, body, library.tool_names, library.tool_args, library.all_args,
                                     library.enums, library.record_fields))
    outside = [t for t in library.tool_names if t not in skill_tools and t not in runs.WRITE_TOOLS]
    for tool in st.mentions(text, outside):
        findings["T7"].append(f"`{tool}` is not one of this skill's tools")

    # --- write safety
    for tool in st.mentions(text, sorted(runs.WRITE_TOOLS - owned)):
        findings["W1"].append(f"`{tool}` changes data and this skill does not own it")
    in_workflows = st.mentions(workflows_text, skill_tools)
    for tool in sorted(owned):
        if tool not in in_workflows:
            findings["W2"].append(f"`{tool}` is not named in any workflow")
    for heading, part in subsections.items():
        for tool in st.mentions(part, sorted(owned)):
            if not CONDITION_PATTERN.search(part):
                findings["W3"].append(f"workflow {heading!r} uses `{tool}` without tying it to the user's request")

    # --- workflow coverage
    for heading in expected_headings:
        if heading in subsections and not st.mentions(subsections[heading], skill_tools):
            findings["P1"].append(f"workflow {heading!r} names no tool of the skill")
    unused = [t for t in skill_tools if t not in owned and t not in in_workflows]
    if unused:
        findings["P2"].append(f"not named in any workflow: {', '.join('`%s`' % t for t in unused)}")

    # --- generalisability
    own_tool_text = tool_description_text(package).lower()
    for item in st.find_literals(text, {x for x in library.scenarios.literals if x not in own_tool_text}):
        findings["G1"].append(f"{item!r} comes from a scenario")
    for user_task_id, prompt in library.scenarios.prompts.items():
        for sequence in st.shared_sequences(text, prompt):
            findings["G2"].append(f"the word run {sequence!r} is also in the prompt of {user_task_id}")
    for pattern in (st.DATE_PATTERN, st.TIME_PATTERN, st.FILENAME_PATTERN):
        for match in dict.fromkeys(m.group(0) for m in pattern.finditer(text)):
            findings["G3"].append(f"concrete value {match!r}")
    for number in sorted(st.numbers_in(text) & library.scenarios.numbers):
        findings["G4"].append(f"number {number} also appears in the scenarios")

    # --- safety
    findings.update(st.safety_findings(text, library.scenarios.goal_markers))
    lowered = text.lower()
    for other in library.packages:
        if other != skill_id and re.search(rf"(?<![\w-]){re.escape(other)}(?![\w-])", lowered):
            findings["A6"].append(f"names the skill {other!r}")
    for match in re.finditer(r"\b(other|another|different) skills?\b", lowered):
        findings["A6"].append(f"phrase {match.group(0)!r}")
    for pattern in SECURITY_PHRASES:
        match = re.search(pattern, lowered)
        if match:
            findings["A7"].append(f"phrase {match.group(0)!r}")

    rows = []
    for key, (check_name, dimension, severity) in CHECKS.items():
        rows.append({"id": key, "name": check_name, "dimension": dimension, "severity": severity,
                     "passed": not findings[key], "detail": "; ".join(dict.fromkeys(findings[key]))})
    n_errors = sum(1 for r in rows if not r["passed"] and r["severity"] == "error")
    n_warnings = sum(1 for r in rows if not r["passed"] and r["severity"] == "warning")
    return {"skill_id": skill_id, "validator_version": VALIDATOR_VERSION, "passed": n_errors == 0,
            "n_errors": n_errors, "n_warnings": n_warnings, "checks": rows}


def error_messages(result: dict) -> list[str]:
    """Return the failed error-level checks as 'ID (name): detail' lines (the feedback for a repair round)."""
    return [f"{r['id']} ({r['name']}): {r['detail']}" for r in result["checks"]
            if not r["passed"] and r["severity"] == "error"]


def validate_library(texts: dict[str, str], library: Library) -> dict:
    """Check the skills together. `texts` maps the skill ids that exist to their text.

    Coverage (error): for each scenario whose mapped skills all exist, every tool its recorded run called must be named
    in the Workflows section of at least one mapped skill. Overlap (warning): two skills must not share a run of
    OVERLAP_WORDS or more words. Skills of the taxonomy without a text are listed under "missing".
    """
    workflows = {}
    for skill_id, text in texts.items():
        _, body, _ = st.parse_frontmatter(text)
        workflows[skill_id] = st.split_sections(body)[1].get("Workflows", "")
    errors, warnings = [], []
    for user_task_id, entry in library.taxonomy["scenario_map"].items():
        mapped = entry["skills"]
        if user_task_id not in library.scenarios.calls or not all(s in texts for s in mapped):
            continue
        named = set()
        for skill_id in mapped:
            named |= set(st.mentions(workflows[skill_id], library.tool_names))
        for tool in dict.fromkeys(library.scenarios.calls[user_task_id]):
            if tool not in named:
                errors.append(f"{user_task_id}: its run called `{tool}` but the workflows of "
                              f"{', '.join(mapped)} do not name it")
    ids = list(texts)
    for i, first in enumerate(ids):
        for second in ids[i + 1:]:
            shared = st.shared_sequences(texts[first], texts[second], OVERLAP_WORDS)
            if shared:
                warnings.append(f"{first} and {second} share {len(shared)} passages of {OVERLAP_WORDS}+ words, "
                                f"for example {shared[0]!r}")
    return {"errors": errors, "warnings": warnings,
            "missing": [s for s in library.taxonomy["skills"] if s not in texts]}


# ----------------------------------------------------------------------------------------------------- CLI

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate generated capability skills.")
    parser.add_argument("--skills", nargs="+", help="skill ids (default: all skills of the taxonomy)")
    parser.add_argument("--skill-file", type=Path, help="validate this file as the one skill named in --skills")
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--out", type=Path, help="write the results as JSON to this file")
    args = parser.parse_args()
    if args.skill_file and (not args.skills or len(args.skills) != 1):
        parser.error("--skill-file needs exactly one skill in --skills")

    library = load_library()
    skill_ids = args.skills or list(library.taxonomy["skills"])
    unknown = [s for s in skill_ids if s not in library.packages]
    if unknown:
        parser.error(f"unknown skill ids: {', '.join(unknown)}")

    results, texts, skipped = [], {}, []
    for skill_id in skill_ids:
        path = args.skill_file or args.skills_dir / skill_id / "SKILL.md"
        if not path.exists() or not path.read_text(encoding="utf-8").strip():
            skipped.append(skill_id)
            continue
        text = path.read_text(encoding="utf-8")
        texts[skill_id] = text
        result = validate_skill(text, library, skill_id)
        results.append(result)
        print(f"{skill_id}: {'PASS' if result['passed'] else 'FAIL'} "
              f"({result['n_errors']} errors, {result['n_warnings']} warnings)")
        for line in error_messages(result):
            print(f"    error {line}")
        for row in result["checks"]:
            if not row["passed"] and row["severity"] == "warning":
                print(f"    warning {row['id']} ({row['name']}): {row['detail']}")
    if skipped:
        print(f"Skipped {len(skipped)} skills with no text yet: {', '.join(skipped)}")

    library_result = validate_library(texts, library) if texts else None
    if library_result:
        for line in library_result["errors"]:
            print(f"library error: {line}")
        for line in library_result["warnings"]:
            print(f"library warning: {line}")
    if args.out and results:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        document = {"validator_version": VALIDATOR_VERSION, "results": results, "library": library_result}
        args.out.write_text(json.dumps(document, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {len(results)} results to {args.out}")
    if not results:
        print("Nothing to validate.")
    failed = any(not r["passed"] for r in results) or bool(library_result and library_result["errors"])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
