"""
Purpose:
    Describe AgentDojo's 24 workspace tools the way the agent saw them: name, tool group, description, and the
    arguments with type, default, allowed values and description. The descriptions are derived from the tools'
    source code at the pinned commit (their docstrings and signatures) by the same rules AgentDojo uses to build
    the schema it sends to the model, so they can be used as the tool specification in skill-generation prompts.

    AgentDojo shows the model only the first line of a tool's docstring and the arguments documented with
    ":param"; arguments that AgentDojo fills in from the environment (calendar, inbox, cloud drive) are not part
    of what the agent sees. Types are the signature's annotations, not the JSON schema OpenAI actually received
    (that needs AgentDojo to be run).

Inputs:
    AgentDojo's original files at the pinned commit, read through agentdojo_source.py (needs the local clone):
    the TOOLS list of the workspace suite, the three tool implementation files and tools/types.py.

Outputs:
    Python values only (no files): build_tools() returns a list of 24 dicts with the keys name, group, description,
    extra_description (the rest of the docstring, which the agent was NOT shown) and parameters (a list of dicts
    with name, type, required, default, enum, description).

Dependencies:
    agentdojo_source.py (reads the pinned files), agentdojo_runs.py (tool groups). Standard library only.
    AgentDojo itself is not imported or run.

Usage:
    Imported by the scripts that need tool specifications. Quick look:
    python3 -B -c "import sys; sys.path.insert(0, 'src/scripts'); import agentdojo_tools as t; print(t.build_tools()[0])"
"""

import ast
import inspect
import re

import agentdojo_runs as runs
import agentdojo_source as source

TOOL_FILES = ("tool_calendar_client", "tool_cloud_drive_client", "tool_email_client")
# Docstring tags that AgentDojo's docstring parser turns into tool arguments.
PARAM_TAGS = {"param", "parameter", "arg", "argument", "attribute", "key", "keyword"}


def parse_docstring(doc: str) -> dict:
    """Split a reST-style docstring the way AgentDojo's tool registration does (docstring_parser, reST style).

    Returns:
        {"short": first line of the description or None, "long": the rest of the description or None,
         "params": {argument name: description}}. Continuation lines of a parameter description are kept,
        joined by newlines. AgentDojo shows the agent only the short description and the parameters.
    """
    text = inspect.cleandoc(doc)
    first_tag = re.search(r"^:", text, flags=re.M)
    description = text[: first_tag.start()] if first_tag else text
    tags = text[first_tag.start():] if first_tag else ""

    parts = description.split("\n", 1)
    short = parts[0] or None
    long = (parts[1].strip() or None) if len(parts) > 1 else None

    params = {}
    for chunk in re.finditer(r"(^:.*?)(?=^:|\Z)", tags, flags=re.S | re.M):
        header, _, body = chunk.group(0).lstrip(":").partition(":")
        words = header.split()
        if words and words[0] in PARAM_TAGS:
            params[words[-1]] = body.strip()
    return {"short": short, "long": long, "params": params}


def workspace_tool_names() -> list[str]:
    """Return the names in the workspace suite's TOOLS list, in the order AgentDojo lists them."""
    text = source.read_source("workspace_task_suite")
    block = text[text.index("TOOLS = ["):]
    return re.findall(r"(\w+)\.__name__", block[: block.index("\n]")])


def enum_values() -> dict[str, list[str]]:
    """Return {enum class name: its values} for the string enums in tools/types.py (e.g. SharingPermission)."""
    enums = {}
    for node in ast.parse(source.read_source("tool_types")).body:
        if isinstance(node, ast.ClassDef) and any(ast.unparse(b) == "StrEnum" for b in node.bases):
            enums[node.name] = [
                stmt.value.value for stmt in node.body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
            ]
    return enums


def _tool_functions() -> dict[str, ast.FunctionDef]:
    """Return {tool name: function definition} for every @register_function function in the tool files."""
    found = {}
    for key in TOOL_FILES:
        for node in ast.parse(source.read_source(key)).body:
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Name) and d.id == "register_function" for d in node.decorator_list
            ):
                if node.name in found:
                    raise ValueError(f"tool {node.name!r} is defined twice")
                found[node.name] = node
    return found


def build_tools() -> list[dict]:
    """Describe all workspace tools as the agent saw them, in TOOLS order.

    The description is the docstring's first line with "None" written as "null" (as AgentDojo does). The
    arguments are the parameters documented in the docstring; arguments injected from the environment
    (calendar, inbox, cloud drive) are not part of what the agent sees and are left out. Types are the
    signature's annotations, not pydantic's generated JSON schema (that needs AgentDojo to be run).

    Raises:
        ValueError: if a tool is missing, a documented argument is missing from the signature, or a
            documented argument has no description (AgentDojo would reject such a tool as well).
    """
    functions = _tool_functions()
    enums = enum_values()
    tools = []
    for name in workspace_tool_names():
        node = functions.get(name)
        if node is None:
            raise ValueError(f"tool {name!r} from TOOLS was not found in the tool files")
        doc = parse_docstring(ast.get_docstring(node, clean=False) or "")
        if doc["short"] is None:
            raise ValueError(f"tool {name!r} has no description")

        arguments = node.args.posonlyargs + node.args.args
        defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(node.args.defaults)
        by_name = dict(zip((a.arg for a in arguments), zip(arguments, defaults)))
        parameters = []
        for arg_name, description in doc["params"].items():
            if arg_name not in by_name:
                raise ValueError(f"tool {name!r}: documented argument {arg_name!r} is not in the signature")
            if not description:
                raise ValueError(f"tool {name!r}: argument {arg_name!r} has no description")
            argument, default = by_name[arg_name]
            type_name = ast.unparse(argument.annotation)
            parameters.append({
                "name": arg_name,
                "type": type_name,
                "required": default is None,
                "default": ast.literal_eval(default) if default is not None else None,
                "enum": enums.get(type_name),
                "description": description,
            })
        tools.append({
            "name": name,
            "group": runs.TOOL_CATEGORY[name],
            "description": doc["short"].replace("None", "null"),
            "extra_description": doc["long"],
            "parameters": parameters,
        })
    return tools
