"""
Purpose:
    Build the exact request that will be sent to the model to write each capability skill's SKILL.md. One request
    per skill, built deterministically from the skill's source package (build_skill_sources.py) and two fixed
    prompt templates, so that the generation step only has to send the saved files and every generated skill is
    tied to its inputs by hash.

    What the model is shown: the skill's identifier, kind (read-only or allowed to change data), scope and
    workflows with the heading to use for each, the full specification of its tools, the field schema of the
    records they return, and a few fixed conventions. What it is NOT shown: any scenario, example request, tool
    output, answer or trace. The request can therefore not leak scenario content. This is the task-agnostic
    setting of SkillGenBench.

Inputs:
    src/data/skill_library/sources/<skill-id>.json (build_skill_sources.py).
    src/prompts/skill_capability_system_v2.md and skill_capability_user_v2.md: the prompt templates (the user
    template has {{placeholders}} that this script fills).

Outputs:
    src/data/skill_library/prompts/<skill-id>.json, one per source package, with the keys: skill_id,
    template_version, prompt_sha256, estimated_input_tokens, limits (the length rules the prompt states, for
    the validator), source (source package file name, its SHA-256 and the skill definition's SHA-256) and
    request (the request body to send as-is: model, instructions, input, reasoning, max_output_tokens).
    prompt_sha256 is the SHA-256 of the canonical JSON of "request". Output is deterministic and contains no local
    paths.

Dependencies:
    prompt_utils.py (settings, hashing, template filling, record rendering), agentdojo_runs.py. Standard library
    only. No network access, no AgentDojo clone and no model call are needed.

Usage:
    python3 -B src/scripts/build_capability_prompts.py [--sources-dir PATH] [--out-dir PATH]
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import agentdojo_runs as runs
import prompt_utils

TEMPLATE_VERSION = "skill_capability_v2"
PROMPTS_DIR = runs.REPO_ROOT / "src" / "prompts"
SYSTEM_TEMPLATE = PROMPTS_DIR / "skill_capability_system_v2.md"
USER_TEMPLATE = PROMPTS_DIR / "skill_capability_user_v2.md"
DEFAULT_SOURCES_DIR = runs.REPO_ROOT / "src" / "data" / "skill_library" / "sources"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "skill_library" / "prompts"

# The length rules stated in the system prompt, recorded so the validator can use the same numbers.
LIMITS = {"body_words": [300, 900], "steps_per_workflow": [3, 6]}
GROUP_LABELS = {"email_tools": "email", "calendar_tools": "calendar", "drive_tools": "cloud drive"}
KIND_STATEMENTS = {
    "read": "This skill is read-only: it must never instruct the assistant to change, send, delete, share or create "
            "anything.",
    "write": "This skill may change the user's data, but only with the data-changing tools listed below and only when "
             "the user's request asks for that change.",
}


def workflow_heading(workflow_id: str) -> str:
    """The heading a workflow's subsection must use: its identifier with hyphens replaced by spaces and each word
    capitalised ("day-overview" -> "Day Overview"). The validator derives the same heading to check coverage."""
    return workflow_id.replace("-", " ").title()


def render_workflows(workflows: list[dict]) -> str:
    """List the workflows with the heading to use for each and what it covers."""
    return "\n".join(
        f"- Heading `### {workflow_heading(w['id'])}`: {w['description']}" for w in workflows
    )


def render_tool_specs(tools: list[dict]) -> str:
    """Render the skill's tools (all of them in full): description, arguments, and whether the tool changes data."""
    blocks = []
    for tool in tools:
        lines = [f"### `{tool['name']}` ({GROUP_LABELS[tool['group']]})", tool["description"]]
        if tool["changes_data"]:
            lines.append("This tool changes the user's data.")
        lines += ["", "Arguments:"]
        for parameter in tool["parameters"]:
            if parameter["required"]:
                requirement = "required"
            else:
                requirement = f"optional, default {json.dumps(parameter['default'], ensure_ascii=False)}"
            lines.append(
                f"- `{parameter['name']}` ({parameter['type']}, {requirement}): "
                f"{prompt_utils.describe(parameter['description'], parameter['enum'])}"
            )
        if not tool["parameters"]:
            lines.append("- (none)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_prompt(source_record: dict, source_bytes: bytes, system_prompt: str, user_template: str) -> dict:
    """Build the output record (request plus provenance) for one skill source package."""
    user_prompt = prompt_utils.fill_template(user_template, {
        "skill_id": source_record["skill_id"],
        "kind_statement": KIND_STATEMENTS[source_record["kind"]],
        "scope": source_record["summary"],
        "workflows": render_workflows(source_record["workflows"]),
        "tool_specs": render_tool_specs(source_record["tools"]),
        "record_schemas": prompt_utils.render_record_schemas(source_record["records"]),
    }).strip()
    request = {
        "model": prompt_utils.GENERATION_SETTINGS["model"],
        "instructions": system_prompt,
        "input": user_prompt,
        "reasoning": prompt_utils.GENERATION_SETTINGS["reasoning"],
        "max_output_tokens": prompt_utils.GENERATION_SETTINGS["max_output_tokens"],
    }
    return {
        "skill_id": source_record["skill_id"],
        "template_version": TEMPLATE_VERSION,
        "prompt_sha256": prompt_utils.canonical_hash(request),
        "estimated_input_tokens": math.ceil((len(system_prompt) + len(user_prompt)) / 4),  # rough: 4 chars/token
        "limits": LIMITS,
        "source": {
            "skill_sources": f"{source_record['skill_id']}.json",
            "skill_sources_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "definition_sha256": source_record["provenance"]["definition_sha256"],
        },
        "request": request,
    }


def build_all(sources_dir: Path = DEFAULT_SOURCES_DIR, out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write one prompt file per source package and return the files written, sorted by skill id."""
    source_files = sorted(sources_dir.glob("*.json"))
    if not source_files:
        raise FileNotFoundError(f"no skill source packages in {sources_dir}; run build_skill_sources.py first")
    system_prompt = SYSTEM_TEMPLATE.read_text(encoding="utf-8").strip()
    user_template = USER_TEMPLATE.read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for source_file in source_files:
        source_bytes = source_file.read_bytes()
        record = build_prompt(json.loads(source_bytes), source_bytes, system_prompt, user_template)
        path = out_dir / source_file.name
        path.write_text(json.dumps(record, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the capability-skill generation prompts.")
    parser.add_argument("--sources-dir", type=Path, default=DEFAULT_SOURCES_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    written = build_all(args.sources_dir, args.out_dir)
    print(f"Wrote {len(written)} prompt files to {args.out_dir}")


if __name__ == "__main__":
    main()
