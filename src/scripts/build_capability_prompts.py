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
    src/prompts/skill_capability_system_<version>.md and skill_capability_user_<version>.md: the prompt templates
    (the user template has {{placeholders}} that this script fills). Two versions coexist (TEMPLATES):
      * skill_capability_v4 (default, stable): the version the published skills in src/data/skills/ were
        generated with;
      * skill_capability_v5 (experimental, not validated): adds general guidance for dates without a year,
        searching again after an empty search and answer formats; see
        src/data/skill_library/experimental/prompt_v5/README.md.

Outputs:
    For v4: src/data/skill_library/prompts/<skill-id>.json; for v5:
    src/data/skill_library/experimental/prompt_v5/prompts/<skill-id>.json. One file per source package, with the
    keys: skill_id, template_version, prompt_sha256, estimated_input_tokens, limits (the length rules the prompt states, for
    the validator), source (source package file name, its SHA-256 and the skill definition's SHA-256) and
    request (the request body to send as-is: model, instructions, input, reasoning, max_output_tokens).
    prompt_sha256 is the SHA-256 of the canonical JSON of "request". Output is deterministic and contains no local
    paths.

Dependencies:
    prompt_utils.py (settings, hashing, template filling, record rendering), agentdojo_runs.py. Standard library
    only. No network access, no AgentDojo clone and no model call are needed.

Usage:
    python3 -B src/scripts/build_capability_prompts.py [--sources-dir PATH] [--out-dir PATH]
    python3 -B src/scripts/build_capability_prompts.py --template-version skill_capability_v5
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

import agentdojo_runs as runs
import prompt_utils

PROMPTS_DIR = runs.REPO_ROOT / "src" / "prompts"
SKILL_LIBRARY_DIR = runs.REPO_ROOT / "src" / "data" / "skill_library"
DEFAULT_SOURCES_DIR = SKILL_LIBRARY_DIR / "sources"
DEFAULT_OUT_DIR = SKILL_LIBRARY_DIR / "prompts"
EXPERIMENTAL_V5_DIR = SKILL_LIBRARY_DIR / "experimental" / "prompt_v5"

# Each template version has its own template files and its own output folder, so an experimental version never
# overwrites the request files the published skills were generated from.
TEMPLATES = {
    "skill_capability_v4": {"system": PROMPTS_DIR / "skill_capability_system_v4.md",
                            "user": PROMPTS_DIR / "skill_capability_user_v4.md",
                            "out_dir": DEFAULT_OUT_DIR, "status": "stable"},
    "skill_capability_v5": {"system": PROMPTS_DIR / "skill_capability_system_v5.md",
                            "user": PROMPTS_DIR / "skill_capability_user_v5.md",
                            "out_dir": EXPERIMENTAL_V5_DIR / "prompts", "status": "experimental"},
}
TEMPLATE_VERSION = "skill_capability_v4"  # the default: the version of the published skills
SYSTEM_TEMPLATE = TEMPLATES[TEMPLATE_VERSION]["system"]
USER_TEMPLATE = TEMPLATES[TEMPLATE_VERSION]["user"]

# The length rules stated in the system prompt, recorded so the validator can use the same numbers (the prompt text
# itself is a file, so a test checks that the numbers there match).
LIMITS = {"body_words": [250, 600], "steps_per_workflow": [3, 6], "description_chars": 300}
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


def build_prompt(source_record: dict, source_bytes: bytes, system_prompt: str, user_template: str,
                 template_version: str = TEMPLATE_VERSION) -> dict:
    """Build the output record (request plus provenance) for one skill source package; template_version is only
    recorded (the caller passes the matching template texts)."""
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
        "template_version": template_version,
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


def build_all(sources_dir: Path = DEFAULT_SOURCES_DIR, out_dir: Path | None = None,
              template_version: str = TEMPLATE_VERSION) -> list[Path]:
    """Write one prompt file per source package with the given template version and return the files written,
    sorted by skill id. out_dir defaults to that version's own folder (TEMPLATES). Raises ValueError for an
    unknown version and FileNotFoundError when there are no source packages."""
    if template_version not in TEMPLATES:
        raise ValueError(f"unknown template version {template_version!r}; choose from {sorted(TEMPLATES)}")
    templates = TEMPLATES[template_version]
    out_dir = out_dir or templates["out_dir"]
    source_files = sorted(sources_dir.glob("*.json"))
    if not source_files:
        raise FileNotFoundError(f"no skill source packages in {sources_dir}; run build_skill_sources.py first")
    system_prompt = templates["system"].read_text(encoding="utf-8").strip()
    user_template = templates["user"].read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for source_file in source_files:
        source_bytes = source_file.read_bytes()
        record = build_prompt(json.loads(source_bytes), source_bytes, system_prompt, user_template, template_version)
        path = out_dir / source_file.name
        path.write_text(json.dumps(record, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the capability-skill generation prompts.")
    parser.add_argument("--sources-dir", type=Path, default=DEFAULT_SOURCES_DIR)
    parser.add_argument("--template-version", choices=sorted(TEMPLATES), default=TEMPLATE_VERSION)
    parser.add_argument("--out-dir", type=Path, help="default: the template version's own folder")
    args = parser.parse_args()
    written = build_all(args.sources_dir, args.out_dir, args.template_version)
    status = TEMPLATES[args.template_version]["status"]
    print(f"Wrote {len(written)} prompt files ({args.template_version}, {status}) to {written[0].parent}")


if __name__ == "__main__":
    main()
