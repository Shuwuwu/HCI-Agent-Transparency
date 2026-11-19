"""
Purpose:
    Export AgentDojo execution traces to the JSONL format of Invariant Explorer (a local trace viewer), so a
    researcher can inspect them visually and check each high-transparency stimulus against the execution it came
    from. This is a one-way, read-only export: it never changes the source traces, the stimuli or Explorer, and it
    makes no model calls. Explorer is a researcher-side tool; nothing here feeds the participant-facing materials.

    Each trace is converted to Explorer's OpenAI-style messages (tool calls become
    {"id", "type": "function", "function": {"name", "arguments"}}), and every entry of the matching stimulus's action
    log becomes an Explorer annotation on the tool call and the tool result it was derived from (using the source
    message indexes recorded in the stimulus's provenance.json). A second file, comparison.md, lists for each trace
    what the stimulus keeps, drops or represents differently compared with the original execution and with the
    Explorer view.

Inputs:
    One or more sources: AgentDojo run records (src/data/execution/runs/*.json) or raw AgentDojo traces
    (src/data/gpt-4o-2024-05-13/workspace/...). For each source the stimulus folder src/data/stimuli/user_task_<N>__<variant>/ built by
    build_stimuli.py must already exist (the script stops and names the command if it does not, or if the folder was
    built from a different version of the source file or from a different execution of the same task).

Outputs:
    <out-dir>/hci-stimuli-poc.jsonl  Explorer dataset: a metadata line, then one trace per source.
    <out-dir>/comparison.md          original vs Explorer vs stimulus, per trace.
    The default out-dir src/data/explorer/ is gitignored.

Dependencies:
    build_stimuli.py (source loading, stimulus ids), tool_result_parser.py (read tool results back),
    agentdojo_runs.py (injection goals and the unauthorized-action rule). Standard library only.

Usage:
    python3 -B src/scripts/export_explorer_traces.py SOURCE [SOURCE ...] [--stimuli-dir DIR] [--out-dir DIR]
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs
import build_stimuli as bs
import tool_result_parser as trp

EXPORTER_VERSION = "explorer_export_v1"
DEFAULT_OUT_DIR = bs.REPO_ROOT / "src" / "data" / "explorer"
DATASET_FILE = "hci-stimuli-poc.jsonl"
REPORT_FILE = "comparison.md"
EXPLORER_DISPLAY_LIMIT = 10000  # truncation_limit in Explorer's configs/explorer.local.yml


class ExportError(ValueError):
    """A source cannot be exported (for example, its stimulus has not been built)."""


# ---- conversion -----------------------------------------------------------------------------------------------

def convert_messages(messages: list[dict]) -> list[dict]:
    """AgentDojo messages -> Explorer messages. Order, roles, contents and arguments are copied unchanged.

    Changes: a tool call {"function", "args", "id"} becomes {"id", "type": "function", "function": {"name",
    "arguments"}}; a tool result gets "tool_name" (the name of the call it answers, which Explorer shows as a badge);
    the "tool_call" echo that AgentDojo stores in each tool result is dropped after checking that it equals the call
    it answers; "tool_calls": null on an assistant message is omitted; "error" is kept only when it is not null.
    Results are paired with calls by id, in order when a run reuses an id.
    """
    pending: dict[str, list[dict]] = {}
    converted = []
    for message in messages:
        out = {"role": message["role"], "content": message.get("content")}
        if message["role"] == "assistant" and message.get("tool_calls"):
            out["tool_calls"] = []
            for call in message["tool_calls"]:
                pending.setdefault(call["id"], []).append(call)
                out["tool_calls"].append({"id": call["id"], "type": "function",
                                          "function": {"name": call["function"], "arguments": call["args"]}})
        elif message["role"] == "tool":
            queue = pending.get(message["tool_call_id"])
            if not queue:
                raise ExportError(f"tool result {message['tool_call_id']} answers no call")
            call = queue.pop(0)
            if message.get("tool_call") is not None and message["tool_call"] != call:
                raise ExportError(f"tool result {message['tool_call_id']}: its echoed call differs from the call")
            out.update(tool_call_id=message["tool_call_id"], tool_name=call["function"])
            if message.get("error") is not None:
                out["error"] = message["error"]
        converted.append(out)
    return converted


# ---- annotations ----------------------------------------------------------------------------------------------

def _range(address: str, text: str) -> str:
    """Explorer shows an annotation only when its address ends in a character range such as ":0-12"."""
    return f"{address}:0-{len(text)}"


def build_annotations(messages: list[dict], high: dict, provenance: dict) -> list[dict]:
    """One annotation per stimulus element, placed on the message(s) it was derived from.

    The user request and final response get one each; every action entry gets one on the tool call's function name
    and one on the tool result's content; every agent message entry gets one on its content.
    """
    annotations = []

    def add(address: str, text: str, content: str) -> None:
        annotations.append({"address": _range(address, text), "content": content,
                            "extra_metadata": {"source": "stimulus", "stimulus_id": high["stimulus_id"]}})

    user_index = next(i for i, m in enumerate(messages) if m["role"] == "user")
    add(f"messages.{user_index}.content", messages[user_index]["content"], "Stimulus: user request (shown verbatim)")
    last = len(messages) - 1
    add(f"messages.{last}.content", messages[last]["content"], "Stimulus: final response (shown verbatim)")
    sources = {e["entry"]: e["source_message_indexes"] for e in provenance["entries"]}
    seen: dict[int, int] = {}
    for entry in high["action_log"]:
        indexes = sources[entry["entry"]]
        if entry["type"] == "agent_message":
            add(f"messages.{indexes[0]}.content", messages[indexes[0]]["content"],
                f"Stimulus log entry {entry['entry']}: agent message (shown verbatim)")
            continue
        # The builder emits the calls of one message in order, so the n-th entry from a message is its n-th call.
        call_number = seen.get(indexes[0], 0)
        seen[indexes[0]] = call_number + 1
        shown = "; ".join(f"{a['name']}: {a['value']}" for a in entry["arguments"]) or "no arguments"
        add(f"messages.{indexes[0]}.tool_calls.{call_number}.function.name", entry["tool"],
            f"Stimulus log entry {entry['entry']} (step {entry['step']}): {entry['action']} [{shown}]")
        if len(indexes) > 1:
            add(f"messages.{indexes[1]}.content", messages[indexes[1]]["content"],
                f"Stimulus log entry {entry['entry']} outcome ({entry['outcome']['status']}): "
                f"{entry['outcome']['summary']}")
    return annotations


# ---- stimulus lookup ------------------------------------------------------------------------------------------

def load_stimulus(source_path: Path, stimuli_dir: Path) -> tuple[dict, dict, dict]:
    """Return (loaded source, high.json, provenance.json) of the stimulus built from this source file."""
    loaded = bs.load_source(source_path)
    sid = bs.stimulus_id(loaded["source"]["sha256"])
    name = bs.stimulus_folder_name(loaded["source"])
    folder = stimuli_dir / name
    if not (folder / "high.json").exists():
        raise ExportError(f"no stimulus {name} for {source_path.name}; build it first with: "
                          f"python3 -B src/scripts/build_stimuli.py {source_path}")
    high = json.loads((folder / "high.json").read_text(encoding="utf-8"))
    provenance = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    if provenance["stimulus_id"] != sid or provenance["source"]["sha256"] != loaded["source"]["sha256"]:
        # Same folder name, different source: another run of the task, or an edited version of this file.
        raise ExportError(f"{name} was built from a different execution or version than {source_path.name}")
    return loaded, high, provenance


def trace_metadata(loaded: dict, high: dict) -> dict:
    """Facts about the execution and its stimulus, shown by Explorer next to the trace."""
    meta = {"stimulus_id": high["stimulus_id"], "source_file": loaded["source"]["file"],
            "source_sha256": loaded["source"]["sha256"], "source_kind": loaded["source"]["kind"],
            "scenario": loaded["source"].get("scenario")}
    raw = json.loads((bs.REPO_ROOT / loaded["source"]["file"]).read_text(encoding="utf-8")) \
        if (bs.REPO_ROOT / loaded["source"]["file"]).exists() else {}
    for key in ("run_id", "condition", "model", "status", "attack", "injection_task", "pipeline"):
        if loaded["source"].get(key) is not None:
            meta[key] = loaded["source"][key]
    for key in ("utility", "security", "error"):
        if key in raw:
            meta[key] = raw[key]
    if loaded["source"].get("injection_task") in runs.INJECTION_GOALS:
        meta["injection_goal"] = runs.INJECTION_GOALS[loaded["source"]["injection_task"]]
    return meta


def build_trace(source_path: Path, stimuli_dir: Path) -> dict:
    """One JSONL line: {"messages", "annotations", "metadata", "name"} plus the facts the report needs."""
    loaded, high, provenance = load_stimulus(source_path, stimuli_dir)
    messages = loaded["messages"]
    meta = trace_metadata(loaded, high)
    label = meta.get("run_id") or f"{meta['scenario']} | {meta.get('attack', 'none')} | {meta.get('injection_task')}"
    return {"line": {"messages": convert_messages(messages), "annotations": build_annotations(messages, high, provenance),
                     "metadata": meta, "name": label},
            "loaded": loaded, "high": high, "provenance": provenance}


# ---- comparison report ----------------------------------------------------------------------------------------

def _leaf_strings(value) -> list[tuple[str, str]]:
    """(field path, text) for every string, number or boolean inside a parsed tool result."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, item in node.items():
                walk(item, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for item in node:
                walk(item, path + "[]")
        elif node is not None and node != "":
            found.append((path, str(node)))

    walk(value, "")
    return found


def result_fields_not_in_summary(content: str, summary: str) -> list[str] | None:
    """Field names of a tool result whose value never appears in the outcome summary (None if unparseable)."""
    try:
        parsed = trp.parse(content)
    except trp.ParseError:
        return None
    lost = []
    for path, text in _leaf_strings(parsed):
        shown = text[:16] if len(text) > 16 and path.endswith(("time",)) else text  # times are shortened on purpose
        if shown not in summary and path not in lost:
            lost.append(path)
    return lost


def report_section(built: dict) -> str:
    """The comparison of one trace: original execution, Explorer view, stimulus."""
    line, loaded, high, prov = built["line"], built["loaded"], built["high"], built["provenance"]
    original, meta = loaded["messages"], line["metadata"]
    out = [f"## {line['name']}", "",
           f"- Source: `{meta['source_file']}` ({meta['source_kind']}); stimulus `{meta['stimulus_id']}`.",
           f"- Run facts: " + ", ".join(f"{k} = {meta[k]}" for k in ("utility", "security", "error", "model", "condition")
                                        if k in meta)]
    if "injection_goal" in meta:
        out.append(f"- Injection goal (AgentDojo): {meta['injection_goal']}")
    out += ["", "| # | Original message | Explorer view | Stimulus |", "|---|---|---|---|"]
    entry_of = {}
    for item in prov["entries"]:
        for index in item["source_message_indexes"]:
            entry_of.setdefault(index, []).append(item["entry"])
    log = {e["entry"]: e for e in high["action_log"]}
    for i, message in enumerate(original):
        role = message["role"]
        if role == "system":
            original_text, explorer, stim = f"system message, {len(message['content'])} characters", \
                "shown as a system message (identical)", "not shown (would reveal the condition and skills)"
        elif role == "user":
            original_text, explorer, stim = "user request", "shown (identical)", "user_request, verbatim"
        elif role == "tool":
            call_name = line["messages"][i]["tool_name"]
            entry = log.get(entry_of.get(i, [None])[0])
            fields = result_fields_not_in_summary(message["content"], entry["outcome"]["summary"]) if entry else None
            original_text = f"result of `{call_name}`, {len(message['content'])} characters"
            explorer = "shown in full with a tool badge; annotated with the log entry's outcome"
            stim = (f"entry {entry['entry']} outcome, {len(entry['outcome']['summary'])} characters"
                    + (f"; result values not in the summary: {', '.join(fields) or 'none'}" if fields is not None
                       else "; result could not be parsed") if entry else "not shown")
        elif message.get("tool_calls"):
            calls = ", ".join(f"`{c['function']}`" for c in message["tool_calls"])
            original_text = f"assistant calls {calls}" + (f" with text {len(message['content'])} chars"
                                                          if message.get("content") else "")
            explorer = "tool call(s) with structured arguments; annotated with the log entry"
            numbers = entry_of.get(i, [])
            stim = ("entries " if len(numbers) > 1 else "entry ") + ", ".join(map(str, numbers)) if numbers else "not shown"
        else:
            original_text, explorer = "assistant final response", "shown (identical)"
            stim = "final_response, verbatim"
        out.append(f"| {i} | {original_text} | {explorer} | {stim} |")
    out += ["", "**Represented differently or not shown in the stimulus**", ""]
    out += _difference_lines(built)
    return "\n".join(out) + "\n"


def _difference_lines(built: dict) -> list[str]:
    line, loaded, high, prov = built["line"], built["loaded"], built["high"], built["provenance"]
    original, meta = loaded["messages"], line["metadata"]
    lines = [f"- Explorer export: {len(original)} messages in, {len(line['messages'])} out; dropped fields: the "
             "`tool_call` echo inside each tool result (verified equal to its call), `tool_calls: null`; added: "
             "`tool_name` on tool results, call shape renamed (`function` -> `function.name`, `args` -> "
             "`function.arguments`). Contents and arguments are unchanged."]
    lines.append(f"- Stimulus truncation: {len(prov['truncated_arguments'])} truncated argument(s); "
                 f"{len(prov['fallback_entries'])} fallback outcome summary(ies).")
    goal = meta.get("injection_goal")
    if goal:
        wanted = " ".join(goal.split())
        for i, m in enumerate(original):
            if m["role"] != "tool":
                continue
            try:
                texts = [text for _, text in _leaf_strings(trp.parse(m["content"]))]
            except trp.ParseError:
                texts = [m["content"]]
            if any(wanted in " ".join(text.split()) for text in texts):
                lines.append(f"- Message {i} (tool result) contains the text of the injection goal; the stimulus does "
                             "not show result text, so a participant cannot see it.")
    longest = max((len(m["content"] or "") for m in original), default=0)
    lines.append(f"- Longest message content: {longest} characters (Explorer's local display limit is "
                 f"{EXPLORER_DISPLAY_LIMIT}; {'within it' if longest <= EXPLORER_DISPLAY_LIMIT else 'longer text is shortened in its view'}).")
    task = meta.get("injection_task")
    if task in runs.UNAUTHORIZED_ACTION:
        calls = [{"function": c["function"], "args": c["args"]} for m in original for c in (m.get("tool_calls") or [])]
        matched = runs.unauthorized_calls(task, calls)
        tool, arg, expected = runs.UNAUTHORIZED_ACTION[task]
        shown = [e for e in high["action_log"] if e["type"] == "action" and e["tool"] == tool
                 and any(expected == a["value"] or expected in str(a["value"]) for a in e["arguments"]
                         if a["name"] == arg)]
        cut = [t for t in prov["truncated_arguments"] if any(e["entry"] == t["entry"] for e in shown)]
        lines.append(f"- Injected action (`{tool}` with `{arg}` containing `{expected}`): {len(matched)} in the original; "
                     f"{len(shown)} visible in the high-transparency log (entry numbers: "
                     f"{', '.join(str(e['entry']) for e in shown) or 'none'}); truncated arguments among them: "
                     f"{len(cut)}.")
        mentioned = expected in high["final_response"] or "mark.black" in high["final_response"]
        lines.append(f"- Final response (both conditions) mentions the attacker's address: {'yes' if mentioned else 'no'}.")
    system = original[0]["content"] if original and original[0]["role"] == "system" else ""
    lines.append(f"- System message ({len(system)} characters) and any skills in it appear in Explorer, never in the stimulus.")
    return lines


def build_report(builts: list[dict]) -> str:
    head = ["# Original vs Explorer vs stimulus", "",
            f"Generated by `export_explorer_traces.py` ({EXPORTER_VERSION}). Researcher-side comparison; not for participants.",
            ""]
    return "\n".join(head) + "\n" + "\n".join(report_section(b) for b in builts)


# ---- output ---------------------------------------------------------------------------------------------------

def render_jsonl(builts: list[dict]) -> str:
    """Dataset text: a dataset-level metadata line, then one trace per line (Explorer's annotated format)."""
    first = {"metadata": {"exporter": EXPORTER_VERSION, "purpose": "HCI-Agent-Transparency stimulus validation",
                          "n_traces": len(builts)}}
    return "\n".join(json.dumps(x, ensure_ascii=False) for x in [first] + [b["line"] for b in builts]) + "\n"


def export(sources: list[Path], stimuli_dir: Path, out_dir: Path) -> dict:
    """Write the dataset and the report; return {"dataset": Path, "report": Path, "traces": int}."""
    builts = [build_trace(path, stimuli_dir) for path in sources]
    out_dir.mkdir(parents=True, exist_ok=True)
    dataset = out_dir / DATASET_FILE
    dataset.write_text(render_jsonl(builts), encoding="utf-8")
    report = out_dir / REPORT_FILE
    report.write_text(build_report(builts), encoding="utf-8")
    return {"dataset": dataset, "report": report, "traces": len(builts)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export traces and their stimulus annotations for Invariant Explorer.")
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--stimuli-dir", type=Path, default=bs.DEFAULT_OUT_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    try:
        result = export(args.sources, args.stimuli_dir, args.out_dir)
    except ExportError as error:
        raise SystemExit(f"error: {error}")
    print(f"Wrote {result['traces']} traces to {result['dataset']}")
    print(f"Wrote the comparison to {result['report']}")


if __name__ == "__main__":
    main()
