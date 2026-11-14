"""
Purpose:
    Small helpers shared by the scripts that build and send requests for the skill-writing model: the fixed generation
    settings, canonical hashing of a request, Markdown code fences, single-pass template filling, field
    descriptions, and the field schema of AgentDojo's record types (Email, CalendarEvent, ...).

    The record schemas are read in two steps so that later steps need no AgentDojo clone: read_record_schemas()
    reads tools/types.py at the pinned commit and returns plain data, render_record_schemas() turns that data
    into Markdown. Only field names, types and descriptions are handled, never any data.

Inputs:
    For read_record_schemas(): AgentDojo's tools/types.py at the pinned commit, through agentdojo_source.py
    (needs the local clone). Everything else works on the values passed in.

Outputs:
    Python values and strings only; no files.

Dependencies:
    agentdojo_source.py and agentdojo_tools.py (enum values). Standard library only.

Usage:
    Imported by build_skill_sources.py, build_capability_prompts.py and generate_skills.py.
"""

import ast
import hashlib
import json
import re

import agentdojo_source as source
import agentdojo_tools as tool_specs

# Sent with every request. No temperature or seed: OpenAI does not document them for this reasoning model.
GENERATION_SETTINGS = {
    "model": "gpt-6.1-sol",
    "reasoning": {"effort": "high"},
    "max_output_tokens": 16384,
}
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


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


def canonical_hash(request: dict) -> str:
    """SHA-256 of the request as compact, key-sorted JSON (UTF-8)."""
    text = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_record_schemas(class_names, include_referenced: bool = False) -> list[dict]:
    """Read the fields of record types from AgentDojo's tools/types.py at the pinned commit.

    Returns a list of {"name", "fields": [{"name", "type", "description", "allowed_values"}]} in the order of
    class_names. Type aliases such as EmailID are resolved to their target (str); "allowed_values" lists the
    values of an enum-typed field, else None. With include_referenced, record classes that the fields' types
    mention (for example CalendarEvent inside Email.attachments) are added after the requested ones.

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
    queue = list(class_names)
    records, seen = [], set()
    while queue:
        class_name = queue.pop(0)
        if class_name in seen:
            continue
        seen.add(class_name)
        if class_name not in classes:
            raise ValueError(f"class {class_name!r} not found in tools/types.py")
        fields = []
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
            fields.append({"name": node.target.id, "type": type_name, "description": description,
                           "allowed_values": allowed})
            if include_referenced:
                queue += [name for name, other in classes.items()
                          if name != class_name and re.search(rf"\b{name}\b", type_name)
                          and any(isinstance(b, ast.Name) and b.id == "BaseModel" for b in other.bases)]
        records.append({"name": class_name, "fields": fields})
    return records


def render_record_schemas(records: list[dict]) -> str:
    """Render the output of read_record_schemas() as Markdown: one section per record type, one bullet per field."""
    sections = []
    for record in records:
        lines = [f"### {record['name']}"]
        for field in record["fields"]:
            lines.append(
                f"- `{field['name']}` ({field['type']}): {describe(field['description'], field['allowed_values'])}"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
