"""
Status:
    Part of the one-skill-per-scenario pipeline, which has been superseded by a library of capability skills
    (see README.md and archive/2026-11-14_per_scenario_pilot/README.md). Kept for now because it builds the
    context packages that build_skill_prompts.py and validate_skills.py still read; removed once the
    capability-skill pipeline replaces it.

Purpose:
    Build one "skill-generation context package" per experiment scenario: everything a model (or a person)
    needs to write a SKILL.md document for that scenario's task, gathered in one file. A package holds the
    original task prompt, the 24 workspace tools the agent could call (name, description, argument schema),
    the complete no-attack execution trace, and the initial environment data for the parts of the workspace
    (inbox, calendar, cloud drive) that the task used.

    The packages are inputs for a later skill-writing step. They deliberately contain no injection, and
    they do contain the agent's final answer (it is part of the trace), so whoever writes a skill from a
    package must be told to describe a reusable procedure, not to copy the answer.

Inputs:
    src/data/experiment/user_task_N/metadata.json and no_attack_execution_trace.json (written by
    build_experiment_stimuli.py): they define which scenarios get a package.
    AgentDojo's original files at the pinned commit, read through agentdojo_source.py (needs the local clone):
    the workspace TOOLS list, the tool implementations (docstrings and signatures), tool types, the
    environment data files and the injection-vector defaults.

Outputs:
    src/data/skill_context/user_task_N.json, one per scenario, with the keys:
        user_task_id, task_prompt, tools, execution_trace, environment, provenance.
    "tools" lists all 24 tools with: name, group, description (the text the agent was shown), extra_description
    (rest of the docstring, which the agent was NOT shown), and parameters (name, type, required, default,
    enum, description). "environment" holds the initial state: account owner, current day, and the raw text of
    the data files for the tool groups the no-attack run used (email tools -> inbox, calendar tools ->
    calendar, drive tools -> cloud drive), with AgentDojo's benign injection-slot defaults filled in exactly as
    in a run without attack. "provenance" gives the git blob hash of every AgentDojo file used.
    Output is deterministic and contains no local paths.

Dependencies:
    agentdojo_tools.py (tool specifications), agentdojo_source.py (reads AgentDojo files at the pinned commit),
    agentdojo_runs.py (tool groups),
    build_experiment_stimuli.py's output folders. Standard library only; AgentDojo is not imported or run.

Usage:
    python3 -B src/scripts/build_skill_context.py [--experiment-dir PATH] [--out-dir PATH]
    (set AGENTDOJO_REPO if the AgentDojo clone is not at ../agentdojo)
"""

import argparse
import json
import re
from pathlib import Path

import agentdojo_runs as runs
import agentdojo_source as source
import agentdojo_tools as tool_specs

DEFAULT_EXPERIMENT_DIR = runs.REPO_ROOT / "src" / "data" / "experiment"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "skill_context"

# Which environment data file belongs to each tool group.
GROUP_DATA = {
    "email_tools": ("inbox", "workspace_inbox_data"),
    "calendar_tools": ("calendar", "workspace_calendar_data"),
    "drive_tools": ("cloud_drive", "workspace_cloud_drive_data"),
}


def injection_defaults() -> dict[str, str]:
    """Return {injection vector id: default text}, parsed from injection_vectors.yaml.

    The defaults are the harmless text AgentDojo puts in every injection slot of the environment when no
    attack is run. The file is simple (id line, description line, default line), so no YAML library is needed.
    """
    defaults = {}
    current = None
    for line in source.read_source("workspace_injection_vectors").splitlines():
        if re.match(r"^\w+:\s*$", line):
            current = line.rstrip(": \t")
        elif current and (match := re.match(r'^  default: "(.*)"\s*$', line)):
            defaults[current] = match.group(1)
    return defaults


def fill_defaults(text: str, defaults: dict[str, str]) -> str:
    """Replace every {injection_vector_id} slot in an environment file with its benign default text."""
    for vector_id, default in defaults.items():
        text = text.replace("{" + vector_id + "}", default)
    return text


def build_environment(tool_groups_used: list[str]) -> tuple[dict, dict]:
    """Return (environment section, source file hashes) for the groups of tools a task used.

    The data files are included as raw text (initial state, before the agent acted). The account owner and
    current day are read from the calendar file's header lines.
    """
    defaults = injection_defaults()
    calendar = source.read_source("workspace_calendar_data")
    files, hashes = [], {}
    for group in ("email_tools", "calendar_tools", "drive_tools"):  # fixed order, whatever the task's order
        if group not in tool_groups_used:
            continue
        label, key = GROUP_DATA[group]
        files.append({
            "name": label,
            "source": source.SOURCE_FILES[key],
            "text": fill_defaults(source.read_source(key), defaults),
        })
        hashes[key] = source.blob_hash(key)
    hashes["workspace_calendar_data"] = source.blob_hash("workspace_calendar_data")
    environment = {
        "state": "initial (before the agent acted)",
        "account_email": re.search(r"^account_email: (.+)$", calendar, re.M).group(1).strip(),
        "current_day": re.search(r"^current_day: (.+)$", calendar, re.M).group(1).strip(),
        "files": files,
    }
    return environment, hashes


def build_package(metadata: dict, trace: dict, tools: list[dict]) -> dict:
    """Assemble the package for one scenario from its metadata, its no-attack trace and the shared tool list."""
    groups_used = [group for group, names in metadata["tools_called"].items() if names]
    environment, environment_hashes = build_environment(groups_used)
    used_sources = [*tool_specs.TOOL_FILES, "tool_types", "workspace_task_suite", "workspace_injection_vectors"]
    hashes = {key: source.blob_hash(key) for key in used_sources} | environment_hashes
    return {
        "user_task_id": metadata["user_task_id"],
        "task_prompt": metadata["task_prompt"],
        "tools": tools,
        "execution_trace": trace,
        "environment": environment,
        "provenance": {
            "repo": source.REPO_URL,
            "commit": source.PINNED_COMMIT,
            "source_files": {
                key: {"path": source.SOURCE_FILES[key], "git_blob": hashes[key]} for key in sorted(hashes)
            },
        },
    }


def build_all(experiment_dir: Path = DEFAULT_EXPERIMENT_DIR, out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write one package per experiment folder and return the files written, in task order."""
    source.check_pinned_commit()
    folders = sorted(experiment_dir.glob("user_task_*"), key=lambda p: runs.task_number(p.name))
    if not folders:
        raise FileNotFoundError(f"no user_task_* folders in {experiment_dir}; run build_experiment_stimuli.py first")
    tools = tool_specs.build_tools()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for folder in folders:
        metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        trace = json.loads((folder / "no_attack_execution_trace.json").read_text(encoding="utf-8"))
        package = build_package(metadata, trace, tools)
        path = out_dir / f"{metadata['user_task_id']}.json"
        path.write_text(json.dumps(package, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the skill-generation context packages.")
    parser.add_argument("--experiment-dir", type=Path, default=DEFAULT_EXPERIMENT_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        written = build_all(args.experiment_dir, args.out_dir)
    except source.AgentDojoSourceError as error:
        raise SystemExit(f"error: {error}")
    print(f"Wrote {len(written)} context packages to {args.out_dir}")


if __name__ == "__main__":
    main()
