"""
Purpose:
    Turn one recorded agent execution (an AgentDojo trace) into the participant-facing material of the experiment, in
    two transparency conditions made from the SAME execution by fixed rules:
        low   the user's request and the agent's final response;
        high  the user's request, a log of what the agent did, and the agent's final response.
    The log lists, in the order they happened, every tool call (an action sentence, the tool name, the arguments
    the agent gave, and the outcome) and every message the agent wrote between calls. It says nothing about whether
    any action was appropriate: no labels, no warnings, no highlighting, and the same wording rules apply to every
    execution. The two conditions share the same request and final response, character for character.

    Works on any execution in AgentDojo's trace format: a run record written by run_executions.py, a recorded run of
    the benchmark (no attack or attacked), or a future unsafe execution. The system message and everything about how the
    run was set up (condition, skills, attack) stay out of the participant files; they go to the provenance file.

Inputs:
    One or more files: run records (src/data/execution/runs/*.json) or raw AgentDojo traces (for example
    src/data/experiment/user_task_N/no_attack_execution_trace.json). They are only read, never changed.

Outputs:
    <out-dir>/<stimulus_id>/ for each execution (default src/data/stimuli/):
        low.json         participant-facing: {stimulus_id, transparency, user_request, final_response}.
        high.json        participant-facing: the same plus action_log.
        provenance.json  researcher-only: where the stimulus came from (file, SHA-256, run id, condition, attack
                         fields if the source has them), which source message each log entry comes from, rules and
                         limits used, summaries that fell back to generic text, truncated arguments, output hashes.
        preview.txt      both conditions as plain text, for reading and checking.
    The stimulus id is opaque (a hash of the source file), so file names reveal nothing about the run. Output is
    deterministic: the same source always gives byte-identical files.

Dependencies:
    stimulus_text.py (wording rules), tool_result_parser.py, agentdojo_runs.py. Standard library only. No network.

Usage:
    python3 -B src/scripts/build_stimuli.py SOURCE [SOURCE ...] [--out-dir PATH]
    python3 -B src/scripts/build_stimuli.py src/data/execution/runs/*.json
"""

import argparse
import hashlib
import json
from pathlib import Path

import stimulus_text as stx

BUILDER_VERSION = "stimulus_builder_v1"
DEFAULT_OUT_DIR = Path(__file__).resolve().parents[2] / "src" / "data" / "stimuli"
REPO_ROOT = Path(__file__).resolve().parents[2]


class StimulusError(ValueError):
    """The source cannot be turned into a stimulus (for example, the run has no final response)."""


def load_source(path: Path) -> dict:
    """Read a run record or a raw AgentDojo trace and return {"messages", "source"} (source = provenance facts)."""
    raw = path.read_bytes()
    data = json.loads(raw)
    try:
        shown = str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        shown = path.name
    source = {"file": shown, "sha256": hashlib.sha256(raw).hexdigest()}
    if "record_version" in data:  # a run record of run_executions.py
        source.update(kind="execution record", run_id=data["run_id"], scenario=data["scenario"],
                      condition=data["condition"], model=data["model"], status=data["status"])
        return {"messages": data["trace"], "source": source}
    if "messages" in data:  # a raw AgentDojo trace
        source.update(kind="AgentDojo trace", scenario=data.get("user_task_id"), attack=data.get("attack_type"),
                      injection_task=data.get("injection_task_id"), pipeline=data.get("pipeline_name"))
        return {"messages": data["messages"], "source": source}
    raise StimulusError(f"{path}: neither a run record nor an AgentDojo trace")


def build_entries(messages: list[dict]) -> dict:
    """Pull the request, final response and ordered log entries out of the messages, checking the structure.

    A tool result belongs to the call with the same id; if a run reuses an id (some recorded runs do), the results
    are matched to the calls with that id in order. Raises StimulusError when the run has no single user message,
    no final assistant response, or a tool result that answers no call.
    """
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(users) != 1:
        raise StimulusError(f"expected one user message, found {len(users)}")
    last = messages[-1]
    if last["role"] != "assistant" or last.get("tool_calls") or not (last.get("content") or "").strip():
        raise StimulusError("the run does not end with a final assistant response")
    results: dict[str, list] = {}
    for i, m in enumerate(messages):
        if m["role"] == "tool":
            results.setdefault(m["tool_call_id"], []).append((i, m))
    entries, step = [], 0
    for i, m in enumerate(messages[:-1]):
        if m["role"] != "assistant":
            continue
        if (m.get("content") or "").strip():
            entries.append({"type": "agent_message", "text": m["content"].strip(), "_from": [i]})
        for call in m.get("tool_calls") or []:
            step += 1
            index, result = results[call["id"]].pop(0) if results.get(call["id"]) else (None, None)
            outcome = stx.outcome(call["function"], result["content"] if result else None,
                                  result["error"] if result else None)
            entries.append({
                "type": "action", "step": step, "action": stx.action_text(call["function"], call["args"]),
                "tool": call["function"], "arguments": stx.argument_items(call["args"]),
                "outcome": {"status": outcome["status"], "summary": outcome["summary"]},
                "_from": [i] + ([index] if index is not None else []), "_fallback": outcome["fallback"],
            })
    orphans = [call_id for call_id, left in results.items() if left]
    if orphans:
        raise StimulusError(f"tool results without a call: {orphans}")
    return {"user_request": messages[users[0]]["content"], "final_response": last["content"], "entries": entries}


def stimulus_id(source_sha256: str) -> str:
    return "stim_" + hashlib.sha256(("stimulus:" + source_sha256).encode()).hexdigest()[:12]


def build_stimulus(path: Path) -> dict:
    """Build everything for one source file: {"id", "low", "high", "provenance", "preview"}."""
    loaded = load_source(path)
    parts = build_entries(loaded["messages"])
    sid = stimulus_id(loaded["source"]["sha256"])
    log = []
    for number, entry in enumerate(parts["entries"], start=1):
        shown = {"entry": number, **{k: v for k, v in entry.items() if not k.startswith("_")}}
        log.append(shown)
    low = {"stimulus_id": sid, "transparency": "low", "user_request": parts["user_request"],
           "final_response": parts["final_response"]}
    high = {"stimulus_id": sid, "transparency": "high", "user_request": parts["user_request"],
            "action_log": log, "final_response": parts["final_response"]}
    provenance = {
        "stimulus_id": sid, "builder_version": BUILDER_VERSION, "source": loaded["source"],
        "rules": {"max_argument_chars": stx.MAX_ARGUMENT_CHARS, "max_listed_items": stx.MAX_LISTED,
                  "max_plain_result_chars": stx.MAX_PLAIN_RESULT_CHARS},
        "entries": [{"entry": i, "type": e["type"], "source_message_indexes": e["_from"]}
                    for i, e in enumerate(parts["entries"], start=1)],
        "n_actions": sum(e["type"] == "action" for e in parts["entries"]),
        "n_agent_messages": sum(e["type"] == "agent_message" for e in parts["entries"]),
        "fallback_entries": [i for i, e in enumerate(parts["entries"], start=1) if e.get("_fallback")],
        "truncated_arguments": [{"entry": i, "argument": a["name"]} for i, e in enumerate(parts["entries"], start=1)
                                for a in e.get("arguments", []) if a["truncated"]],
    }
    return {"id": sid, "low": low, "high": high, "provenance": provenance, "preview": render_preview(low, high)}


def render_text(stimulus: dict) -> str:
    """One condition as plain text, in the order a participant reads it."""
    sections = [f"USER REQUEST\n{stimulus['user_request']}"]
    if "action_log" in stimulus:
        lines = ["ACTION LOG"]
        for entry in stimulus["action_log"]:
            if entry["type"] == "agent_message":
                lines.append(f"  [agent message] {entry['text']}")
                continue
            lines.append(f"  {entry['step']}. {entry['action']} ({entry['tool']})")
            lines += [f"       {a['name']}: {a['value']}" for a in entry["arguments"]]
            lines.append(f"       -> {entry['outcome']['summary']}")
        sections.append("\n".join(lines))
    sections.append(f"FINAL RESPONSE\n{stimulus['final_response']}")
    return "\n\n".join(sections) + "\n"


def render_preview(low: dict, high: dict) -> str:
    return "=== LOW TRANSPARENCY ===\n\n" + render_text(low) + "\n=== HIGH TRANSPARENCY ===\n\n" + render_text(high)


def write_stimulus(built: dict, out_dir: Path) -> Path:
    """Write the four files of one stimulus into <out_dir>/<id>/ and return that folder."""
    folder = out_dir / built["id"]
    folder.mkdir(parents=True, exist_ok=True)
    texts = {name: json.dumps(built[name], indent=4, ensure_ascii=False) + "\n" for name in ("low", "high")}
    built["provenance"]["outputs"] = {f"{name}.json": hashlib.sha256(text.encode("utf-8")).hexdigest()
                                      for name, text in texts.items()}
    for name, text in texts.items():
        (folder / f"{name}.json").write_text(text, encoding="utf-8")
    (folder / "provenance.json").write_text(
        json.dumps(built["provenance"], indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    (folder / "preview.txt").write_text(built["preview"], encoding="utf-8")
    return folder


def main() -> None:
    parser = argparse.ArgumentParser(description="Build low/high transparency stimuli from execution traces.")
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    failed = 0
    for path in args.sources:
        try:
            built = build_stimulus(path)
        except StimulusError as error:
            failed += 1
            print(f"skipped {path}: {error}")
            continue
        folder = write_stimulus(built, args.out_dir)
        p = built["provenance"]
        print(f"{path.name}: {folder.name}, {p['n_actions']} actions, {p['n_agent_messages']} agent messages, "
              f"{len(p['fallback_entries'])} fallback summaries")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
