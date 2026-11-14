"""
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
    agentdojo_source.py (reads AgentDojo files at the pinned commit), agentdojo_runs.py (tool groups),
    build_experiment_stimuli.py's output folders. Standard library only; AgentDojo is not imported or run.

Usage:
    python3 -B src/scripts/build_skill_context.py [--experiment-dir PATH] [--out-dir PATH]
    (set AGENTDOJO_REPO if the AgentDojo clone is not at ../agentdojo)
"""

import argparse
import ast
import inspect
import json
import re
from pathlib import Path

import agentdojo_runs as runs
import agentdojo_source as source

DEFAULT_EXPERIMENT_DIR = runs.REPO_ROOT / "src" / "data" / "experiment"
DEFAULT_OUT_DIR = runs.REPO_ROOT / "src" / "data" / "skill_context"

TOOL_FILES = ("tool_calendar_client", "tool_cloud_drive_client", "tool_email_client")
# Which environment data file belongs to each tool group.
GROUP_DATA = {
    "email_tools": ("inbox", "workspace_inbox_data"),
    "calendar_tools": ("calendar", "workspace_calendar_data"),
    "drive_tools": ("cloud_drive", "workspace_cloud_drive_data"),
}
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
    used_sources = [*TOOL_FILES, "tool_types", "workspace_task_suite", "workspace_injection_vectors"]
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
    tools = build_tools()
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
