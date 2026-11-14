"""
Status:
    Part of the one-skill-per-scenario pipeline, which has been superseded by a library of capability skills
    (see README.md and archive/2026-11-14_per_scenario_pilot/README.md). Kept for now because
    generate_safe_skills.py and validate_skills.py still read its prompt files and constants; removed once
    the capability-skill pipeline replaces it.

Purpose:
    Build the exact request that will be sent to the model to write one scenario's legitimate SKILL.md. One
    request per scenario, built deterministically from the skill-generation context package and two fixed prompt
    templates, so that the generation step (a later slice) only has to send the saved files and the inputs of
    every generated skill are pinned by hash.

    What the model is shown: the task prompt, the account owner and current day, all 24 tools (full argument
    schemas only for the tools the no-attack run used), the field schema of the four record types the tools
    return, and the run's tool calls in order with their outputs cut short. What it is NOT shown: the agent's
    final answer, the agent's in-between messages, AgentDojo's system message, the raw environment files and
    the success checks. This keeps the generator from copying the answer into a "reusable" skill.

Inputs:
    src/data/skill_context/user_task_N.json (written by build_skill_context.py).
    src/prompts/skill_safe_system_v1.md and skill_safe_user_v1.md: the prompt templates (the user template has
    {{placeholders}} that this script fills).
    AgentDojo's tools/types.py at the pinned commit, read through agentdojo_source.py (needs the local clone),
    for the record-type schema.

Outputs:
    src/data/skill_generation/prompts/user_task_N.json, one per package, with the keys:
        user_task_id, template_version, prompt_sha256, estimated_input_tokens,
        source (context package file name and SHA-256, git blob of types.py),
        request (the request body to send as-is: model, instructions, input, reasoning, max_output_tokens).
    prompt_sha256 is the SHA-256 of the canonical JSON of "request". Output is deterministic and contains no
    local paths.

Dependencies:
    build_skill_context.py (context packages), agentdojo_tools.py (enum values), agentdojo_source.py, agentdojo_runs.py.
    Standard library only. No network access and no model call.

Usage:
    python3 -B src/scripts/build_skill_prompts.py [--context-dir PATH] [--out-dir PATH]
    (set AGENTDOJO_REPO if the AgentDojo clone is not at ../agentdojo)
"""

import argparse
import ast
import hashlib
import json
import math
import re
from pathlib import Path

import agentdojo_runs as runs
import agentdojo_source as source
import agentdojo_tools as tool_specs
import build_skill_context as context

TEMPLATE_VERSION = "skill_safe_v1"
PROMPTS_DIR = runs.REPO_ROOT / "src" / "prompts"
SYSTEM_TEMPLATE = PROMPTS_DIR / "skill_safe_system_v1.md"
USER_TEMPLATE = PROMPTS_DIR / "skill_safe_user_v1.md"
DEFAULT_CONTEXT_DIR = context.DEFAULT_OUT_DIR
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "skill_generation" / "prompts"

# Sent with every request. No temperature or seed: OpenAI does not document them for this reasoning model.
GENERATION_SETTINGS = {
    "model": "gpt-6.1-sol",
    "reasoning": {"effort": "high"},
    "max_output_tokens": 16384,
}
MAX_OUTPUT_CHARS = 800  # tool output shown to the model, per call
MAX_ARGUMENT_CHARS = 300  # each argument value shown to the model (agent-written text can be long)
DATA_CLASSES = ("Email", "EmailContact", "CalendarEvent", "CloudDriveFile")
GROUP_LABELS = {"email_tools": "email", "calendar_tools": "calendar", "drive_tools": "cloud drive"}
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def truncate(text: str, limit: int) -> str:
    """Cut text to at most `limit` characters and say how much was removed (text within the limit is unchanged)."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [cut: {len(text) - limit} more characters]"


def fence(text: str) -> str:
    """Wrap text in a Markdown code fence that is longer than any run of backticks inside the text."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    marks = "`" * max(3, longest + 1)
    return f"{marks}\n{text}\n{marks}"


def fill_template(template: str, values: dict[str, str]) -> str:
    """Replace every {{name}} placeholder in one pass, so inserted text is never re-interpreted.

    Raises:
        ValueError: if the template's placeholders and the given values do not match exactly.
    """
    names = set(PLACEHOLDER.findall(template))
    if names != set(values):
        raise ValueError(f"template placeholders {sorted(names)} do not match values {sorted(values)}")
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def describe(description: str, allowed_values: list[str] | None = None) -> str:
    """Return a field description ready for a Markdown list item: continuation lines are indented, and the
    allowed values (if any) are appended as a separate sentence."""
    text = description.replace("\n", "\n  ")
    if allowed_values:
        text = text if text.rstrip().endswith((".", "!", "?")) else text.rstrip() + "."
        text += f" Allowed values: {', '.join(allowed_values)}."
    return text


def data_schema() -> str:
    """Describe the fields of the record types the tools return (Email, EmailContact, CalendarEvent,
    CloudDriveFile), read from AgentDojo's tools/types.py at the pinned commit.

    Type aliases such as EmailID are resolved to their target (str); enum-typed fields list their allowed values.
    Only field names, types and descriptions are shown, never any data.

    Raises:
        ValueError: if a class is missing or a field has no description.
    """
    tree = ast.parse(source.read_source("tool_types"))
    aliases = {
        node.targets[0].id: ast.unparse(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Name)
    }
    enums = tool_specs.enum_values()
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    sections = []
    for class_name in DATA_CLASSES:
        if class_name not in classes:
            raise ValueError(f"class {class_name!r} not found in tools/types.py")
        lines = [f"### {class_name}"]
        for node in classes[class_name].body:
            if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
                continue
            type_name = ast.unparse(node.annotation)
            for alias, target in aliases.items():
                type_name = re.sub(rf"\b{alias}\b", target, type_name)
            description = next(
                (kw.value.value for kw in getattr(node.value, "keywords", []) if kw.arg == "description"), None
            )
            if description is None:
                raise ValueError(f"{class_name}.{node.target.id} has no description")
            allowed = next((v for name, v in enums.items() if re.search(rf"\b{name}\b", type_name)), None)
            lines.append(f"- `{node.target.id}` ({type_name}): {describe(description, allowed)}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def example_calls(trace: dict) -> list[dict]:
    """Return the run's tool calls in order as {"function", "args", "output", "error"}.

    Each call is paired with its tool result through the call id. The agent's own text messages are not read.

    Raises:
        ValueError: if a call has no result, or a result's content is not text.
    """
    results = {m["tool_call_id"]: m for m in trace["messages"] if m["role"] == "tool"}
    calls = []
    for message in trace["messages"]:
        for call in message.get("tool_calls") or []:
            result = results.get(call["id"])
            if result is None:
                raise ValueError(f"{trace['user_task_id']}: call {call['id']} has no tool result")
            if not isinstance(result["content"], str):
                raise ValueError(f"{trace['user_task_id']}: result of {call['id']} is not text")
            calls.append({
                "function": call["function"], "args": call["args"],
                "output": result["content"], "error": result.get("error"),
            })
    return calls


def render_example_execution(calls: list[dict]) -> str:
    """Render the tool calls and their (shortened) outputs as numbered Markdown blocks."""
    blocks = []
    for number, call in enumerate(calls, start=1):
        arguments = ", ".join(
            f"{name}={truncate(json.dumps(value, ensure_ascii=False), MAX_ARGUMENT_CHARS)}"
            for name, value in call["args"].items()
        )
        output = call["output"].rstrip() or "(empty output)"
        if call["error"]:
            output = f"Error: {call['error']}\n{output}"
        blocks.append(f"**Call {number}:** `{call['function']}({arguments})`\n\nOutput:\n\n"
                      f"{fence(truncate(output, MAX_OUTPUT_CHARS))}")
    return "\n\n".join(blocks)


def render_tool_catalogue(tools: list[dict], used: set[str]) -> str:
    """Render all tools: full argument lists for the used ones, one line each for the others (catalogue order)."""
    full, brief = [], []
    for tool in tools:
        group = GROUP_LABELS[tool["group"]]
        if tool["name"] not in used:
            brief.append(f"- `{tool['name']}` ({group}): {tool['description']}")
            continue
        lines = [f"#### `{tool['name']}` ({group})", tool["description"], "", "Arguments:"]
        for parameter in tool["parameters"]:
            if parameter["required"]:
                requirement = "required"
            else:
                requirement = f"optional, default {json.dumps(parameter['default'], ensure_ascii=False)}"
            lines.append(
                f"- `{parameter['name']}` ({parameter['type']}, {requirement}): "
                f"{describe(parameter['description'], parameter['enum'])}"
            )
        if not tool["parameters"]:
            lines.append("- (none)")
        full.append("\n".join(lines))
    parts = ["### Tools used in the example execution", *full] if full else []
    if brief:
        parts += ["### Other available tools (not used in the example execution)", "\n".join(brief)]
    return "\n\n".join(parts)


def canonical_hash(request: dict) -> str:
    """SHA-256 of the request as compact, key-sorted JSON (UTF-8)."""
    text = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompt(package: dict, package_bytes: bytes, schema: str, system_prompt: str, user_template: str) -> dict:
    """Build the output record (request plus provenance) for one context package."""
    calls = example_calls(package["execution_trace"])
    environment = package["environment"]
    workspace_context = (
        f"The assistant acts for one user, whose account is `{environment['account_email']}`. "
        f"The current date is {environment['current_day']}."
    )
    user_prompt = fill_template(user_template, {
        "example_request": package["task_prompt"],
        "workspace_context": workspace_context,
        "tool_catalogue": render_tool_catalogue(package["tools"], {call["function"] for call in calls}),
        "data_schema": schema,
        "example_execution": render_example_execution(calls),
    }).strip()
    request = {
        "model": GENERATION_SETTINGS["model"],
        "instructions": system_prompt,
        "input": user_prompt,
        "reasoning": GENERATION_SETTINGS["reasoning"],
        "max_output_tokens": GENERATION_SETTINGS["max_output_tokens"],
    }
    return {
        "user_task_id": package["user_task_id"],
        "template_version": TEMPLATE_VERSION,
        "prompt_sha256": canonical_hash(request),
        "estimated_input_tokens": math.ceil((len(system_prompt) + len(user_prompt)) / 4),  # rough: 4 chars/token
        "source": {
            "context_package": f"{package['user_task_id']}.json",
            "context_package_sha256": hashlib.sha256(package_bytes).hexdigest(),
            "types_git_blob": source.blob_hash("tool_types"),
        },
        "request": request,
    }


def build_all(context_dir: Path = DEFAULT_CONTEXT_DIR, out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write one prompt file per context package and return the files written, in task order."""
    source.check_pinned_commit()
    package_files = sorted(context_dir.glob("user_task_*.json"), key=lambda p: runs.task_number(p.stem))
    if not package_files:
        raise FileNotFoundError(f"no user_task_*.json in {context_dir}; run build_skill_context.py first")
    schema = data_schema()
    system_prompt = SYSTEM_TEMPLATE.read_text(encoding="utf-8").strip()
    user_template = USER_TEMPLATE.read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for package_file in package_files:
        package_bytes = package_file.read_bytes()
        record = build_prompt(json.loads(package_bytes), package_bytes, schema, system_prompt, user_template)
        path = out_dir / package_file.name
        path.write_text(json.dumps(record, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the skill-generation prompts.")
    parser.add_argument("--context-dir", type=Path, default=DEFAULT_CONTEXT_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        written = build_all(args.context_dir, args.out_dir)
    except source.AgentDojoSourceError as error:
        raise SystemExit(f"error: {error}")
    print(f"Wrote {len(written)} prompt files to {args.out_dir}")


if __name__ == "__main__":
    main()
