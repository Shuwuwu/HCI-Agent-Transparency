"""
Purpose:
    Build, for each capability skill in the taxonomy, the source package a skill writer is given: the skill's
    definition (identifier, kind, scope, workflows), the full specification of every tool it is defined with
    (as the agent saw it), and the field schema of the record types those tools return. A package contains no
    scenario: it is built only from the "skills" section of the taxonomy and from AgentDojo's tool code, never
    from the scenario map, a scenario prompt or a recorded run. This is the "task-agnostic" setting of
    SkillGenBench: the generator sees what the skill is for, not any task that will use it.

Inputs:
    src/data/skill_taxonomy.json (hand-written; only its "skills" section is read).
    AgentDojo's tool code and tools/types.py at the pinned commit, through agentdojo_tools.py and
    prompt_utils.py (need the local clone).

Outputs:
    src/data/skill_library/sources/<skill-id>.json, one per skill, with the keys: skill_id, kind, summary, yang,
    workflows, owned_write_tools, tools (name, group, description, changes_data, parameters; the docstring's
    extra text, which the agent was never shown, is left out), records (field schema of the record types) and
    provenance (definition hash, repository, pinned commit, git blob of every AgentDojo file used).
    Output is deterministic and contains no local paths.

Dependencies:
    agentdojo_tools.py (tool specifications), prompt_utils.py (record schemas), agentdojo_source.py (blob hashes),
    agentdojo_runs.py (tool groups, write tools). Standard library only; AgentDojo is not imported or run.

Usage:
    python3 -B src/scripts/build_skill_sources.py [--taxonomy PATH] [--out-dir PATH]
    (set AGENTDOJO_REPO if the AgentDojo clone is not at ../agentdojo)
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs
import agentdojo_source as source
import agentdojo_tools as tool_specs
import prompt_utils

DEFAULT_TAXONOMY = runs.REPO_ROOT / "src" / "data" / "skill_taxonomy.json"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "skill_library" / "sources"

# The record types each tool group works with (tool group -> classes of tools/types.py), in a fixed order.
# Classes that these records mention in their field types (for example CalendarEvent inside Email) are added.
GROUP_RECORDS = {
    "email_tools": ["Email", "EmailContact"],
    "calendar_tools": ["CalendarEvent"],
    "drive_tools": ["CloudDriveFile"],
}
SOURCE_KEYS = (*tool_specs.TOOL_FILES, "tool_types", "workspace_task_suite")


def definition_hash(skill: dict) -> str:
    """SHA-256 of a skill's definition (its taxonomy entry), so a generated skill can be tied to the exact
    definition it was written from even if other parts of the taxonomy change."""
    return prompt_utils.canonical_hash(skill)


def skill_tool_specs(skill: dict, specs_by_name: dict[str, dict]) -> list[dict]:
    """Return the specifications of the skill's tools (read tools first, then owned write tools).

    The agent was shown only the first docstring line and the documented arguments, so the rest of the docstring
    (extra_description) is not part of the package. Each tool is marked changes_data when it is a write tool.

    Raises:
        KeyError: if the skill names a tool that does not exist in AgentDojo's workspace suite.
    """
    tools = []
    for name in [*skill["read_tools"], *skill["owned_write_tools"]]:
        spec = specs_by_name[name]
        tools.append({
            "name": spec["name"],
            "group": spec["group"],
            "description": spec["description"],
            "changes_data": name in runs.WRITE_TOOLS,
            "parameters": spec["parameters"],
        })
    return tools


def record_classes(tools: list[dict]) -> list[str]:
    """Return the record types for a set of tools: those of each tool group used, in GROUP_RECORDS order."""
    groups = {tool["group"] for tool in tools}
    return [name for group, names in GROUP_RECORDS.items() if group in groups for name in names]


def build_source(skill_id: str, skill: dict, specs_by_name: dict[str, dict]) -> dict:
    """Assemble the source package of one skill from its taxonomy entry and the tool specifications."""
    tools = skill_tool_specs(skill, specs_by_name)
    return {
        "skill_id": skill_id,
        "kind": skill["kind"],
        "summary": skill["summary"],
        "yang": skill["yang"],
        "workflows": skill["workflows"],
        "owned_write_tools": skill["owned_write_tools"],
        "tools": tools,
        "records": prompt_utils.read_record_schemas(record_classes(tools), include_referenced=True),
        "provenance": {
            "definition_sha256": definition_hash(skill),
            "repo": source.REPO_URL,
            "commit": source.PINNED_COMMIT,
            "source_files": {key: {"path": source.SOURCE_FILES[key], "git_blob": source.blob_hash(key)}
                             for key in sorted(SOURCE_KEYS)},
        },
    }


def build_all(taxonomy_path: Path = DEFAULT_TAXONOMY, out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write one source package per skill and return the files written, in taxonomy order."""
    source.check_pinned_commit()
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    specs_by_name = {spec["name"]: spec for spec in tool_specs.build_tools()}
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for skill_id, skill in taxonomy["skills"].items():
        path = out_dir / f"{skill_id}.json"
        path.write_text(
            json.dumps(build_source(skill_id, skill, specs_by_name), indent=4, ensure_ascii=False) + "\n",
            encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the per-skill source packages.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        written = build_all(args.taxonomy, args.out_dir)
    except source.AgentDojoSourceError as error:
        raise SystemExit(f"error: {error}")
    print(f"Wrote {len(written)} skill source packages to {args.out_dir}")


if __name__ == "__main__":
    main()
