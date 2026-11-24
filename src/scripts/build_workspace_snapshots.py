"""
Purpose:
    Build, for each active stimulus, the workspace (mail, calendar, drive) as the agent found it and as it was
    left, so the participant chatbot can offer a read-only workspace sidebar. Nothing is invented: the "before"
    state is AgentDojo's own workspace environment at the pinned commit (default injection-vector text, task
    initialisation, exactly as run_executions.py loads it), and the "after" state is that environment after the
    saved tool calls of the execution are replayed through AgentDojo's own tool functions. No model is called.

    Replay is checked: for every saved tool call, the result AgentDojo's tool returns during replay must equal the
    saved tool result character for character (clock-dependent timestamps are set to the saved ones for the three
    tools that stamp the time: send_email, create_file, append_to_file). A mismatch stops the build. One known
    exception: create_calendar_event stores participants via Python's set(), so their order differs between
    processes; the replay then accepts the result if it is equal apart from that order and keeps the SAVED order.

Inputs:
    src/data/stimuli/<folder>/provenance.json -> the execution record it cites (src/data/execution/runs/*.json,
    trace + scenario), checked against its recorded SHA-256. The AgentDojo package in .venv-agentdojo (installed
    from the pinned commit by setup_execution_env.py).

Outputs:
    <out-dir>/<folder>/workspace.json   PARTICIPANT-facing: {account, before, after}, each with emails, events,
                                        files. Only task-visible fields; no ids of runs, no labels, no diff.
    <out-dir>/<folder>/provenance.json  researcher-only: source run + SHA-256, AgentDojo commit, replay check per
                                        tool call, and the list of changes between before and after.
    Default out-dir: src/data/workspace/. Deterministic: the same inputs give byte-identical files.

Dependencies:
    Must run with the AgentDojo environment's Python (.venv-agentdojo/bin/python). Reads files written by
    build_stimuli.py. Served by participant_server.py (it reads workspace.json only).

Usage:
    .venv-agentdojo/bin/python -B src/scripts/build_workspace_snapshots.py [--folders user_task_13__no_injection ...]
        [--out-dir PATH] [--stimuli-dir PATH]        (default folder: user_task_13__no_injection; pass --folders $(ls src/data/stimuli) for all)
"""

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STIMULI_DIR = ROOT / "src" / "data" / "stimuli"
DEFAULT_OUT_DIR = ROOT / "src" / "data" / "workspace"
BUILDER_VERSION = "workspace_snapshot_v1"
CLOCK_TOOLS = {"send_email": "timestamp", "create_file": "last_modified", "append_to_file": "last_modified"}

# Fields a participant may see. `read` is dropped: it is bookkeeping and the tools never keep it consistent.
EMAIL_FIELDS = ("id_", "sender", "recipients", "cc", "bcc", "subject", "body", "status", "timestamp", "attachments")
EVENT_FIELDS = ("id_", "title", "description", "start_time", "end_time", "location", "participants", "all_day",
                "status")
FILE_FIELDS = ("id_", "filename", "content", "owner", "last_modified", "shared_with", "size")


class ReplayError(RuntimeError):
    """The replayed tool result differs from the saved one, so the workspace could not be reproduced faithfully."""


def pick(model, fields) -> dict:
    """JSON-ready dict with only the given fields of a pydantic model (datetimes as ISO strings)."""
    dumped = model.model_dump(mode="json")
    return {f: dumped[f] for f in fields}


def snapshot(env) -> dict:
    """Participant-visible state of an AgentDojo workspace environment: emails, calendar events and drive files.

    Items are ordered by numeric id so the output is stable. Emails of every status (received, sent, draft) are
    included; trash is not, because the workspace has no tool that fills or shows it.
    """
    def by_id(items):
        return sorted(items, key=lambda m: int(m.id_))
    return {
        "emails": [pick(e, EMAIL_FIELDS) for e in by_id(env.inbox.emails.values())],
        "events": [pick(e, EVENT_FIELDS) for e in by_id(env.calendar.events.values())],
        "files": [pick(f, FILE_FIELDS) for f in by_id(env.cloud_drive.files.values())],
    }


def diff_snapshots(before: dict, after: dict) -> list[dict]:
    """List of {"kind", "id", "change"} entries (added / removed / modified) between two snapshots (researcher-only)."""
    changes = []
    for kind in ("emails", "events", "files"):
        old = {i["id_"]: i for i in before[kind]}
        new = {i["id_"]: i for i in after[kind]}
        for id_ in sorted(set(old) | set(new), key=int):
            if id_ not in old:
                changes.append({"kind": kind, "id": id_, "change": "added"})
            elif id_ not in new:
                changes.append({"kind": kind, "id": id_, "change": "removed"})
            elif old[id_] != new[id_]:
                fields = sorted(k for k in old[id_] if old[id_][k] != new[id_][k])
                changes.append({"kind": kind, "id": id_, "change": "modified", "fields": fields})
    return changes


def saved_clock(tool: str, saved_content: str):
    """The time AgentDojo stamped in a saved tool result of a clock tool, or None if the result has none."""
    key = CLOCK_TOOLS[tool] + ": "
    for line in saved_content.splitlines():
        if line.startswith(key):
            return datetime.datetime.fromisoformat(line[len(key):].strip())
    return None


def same_apart_from_participant_order(saved: str, replayed: str) -> dict | None:
    """If two create_calendar_event results are equal except for the order of `participants`, return the saved
    event (parsed); otherwise None. AgentDojo builds that list with set(), so its order is not reproducible."""
    import yaml
    try:
        a, b = yaml.safe_load(saved), yaml.safe_load(replayed)
    except yaml.YAMLError:
        return None
    if not (isinstance(a, dict) and isinstance(b, dict) and sorted(a.get("participants") or []) ==
            sorted(b.get("participants") or [])):
        return None
    return a if {**a, "participants": None} == {**b, "participants": None} else None


def replay(env, trace: list[dict], tools_modules) -> list[dict]:
    """Apply the saved tool calls of `trace` to `env` in order; return one check record per call.

    Raises ReplayError if any replayed result (or error) differs from the saved one.
    """
    from function_calling_pi.agent_pipeline.tool_execution import tool_result_to_str
    from function_calling_pi.functions_engine.functions_engine import run_function_v2

    checks, i = [], 0
    while i < len(trace):
        message = trace[i]
        i += 1
        calls = message.get("tool_calls") if message["role"] == "assistant" else None
        if not calls:
            continue
        saved = trace[i:i + len(calls)]  # results follow their calls, in call order
        i += len(calls)
        for call, result_message in zip(calls, saved):
            stamp = saved_clock(call["function"], result_message["content"]) if call["function"] in CLOCK_TOOLS else None
            for module in tools_modules:
                module.set_clock(stamp)
            result, error = run_function_v2(env, call["function"], call["args"])
            content = tool_result_to_str(result)
            ok = content == result_message["content"] and error == result_message.get("error")
            note = None
            if not ok and call["function"] == "create_calendar_event" and error == result_message.get("error"):
                saved_event = same_apart_from_participant_order(result_message["content"], content)
                if saved_event is not None:
                    env.calendar.events[str(saved_event["id_"])].participants = list(saved_event["participants"])
                    ok, note = True, "participants listed in the saved order (AgentDojo builds them with set())"
            checks.append({"tool": call["function"], "matches_saved_result": ok, **({"note": note} if note else {})})
            if not ok:
                raise ReplayError(f"{call['function']}: replayed result differs from the saved one\n"
                                  f"saved:    {result_message['content'][:300]!r}\nreplayed: {content[:300]!r}")
    return checks


def install_clock():
    """Make the three clock-stamping tool modules read a settable clock; returns the patched modules."""
    import function_calling_pi.tools.cloud_drive_client as drive
    import function_calling_pi.tools.email_client as mail

    class FrozenDatetime(datetime.datetime):
        fixed = None

        @classmethod
        def now(cls, tz=None):
            return datetime.datetime.now(tz) if cls.fixed is None else cls.fixed

    patched = []
    for module in (drive, mail):
        shim = type("DatetimeShim", (), {"__getattr__": lambda self, name: getattr(datetime, name)})()
        shim.datetime = FrozenDatetime
        module.datetime = shim
        module.set_clock = lambda stamp: setattr(FrozenDatetime, "fixed", stamp)
        patched.append(module)
    return patched


def build_one(folder: str, out_dir: Path, stimuli_dir: Path = STIMULI_DIR) -> dict:
    """Build workspace.json and provenance.json for one stimulus folder; returns a short summary.

    stimuli_dir: the folder holding <folder>/provenance.json (default src/data/stimuli; the validation corpus
    uses src/data/validation/stimuli)."""
    from function_calling_pi import benchmark

    stim_prov = json.loads((stimuli_dir / folder / "provenance.json").read_text(encoding="utf-8"))
    source = stim_prov["source"]
    raw = (ROOT / source["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ReplayError(f"{source['file']} no longer matches the SHA-256 recorded in the stimulus provenance")
    record = json.loads(raw)
    modules = install_clock()

    suite = benchmark.SUITES["workspace"]
    task = suite.get_user_task_by_id(record["scenario"])
    # Same loading as TaskSuite.run_task_with_pipeline with injections={} (the no-injection run).
    env = task.init_environment(suite.load_and_inject_default_environment({}))
    before = snapshot(env)
    checks = replay(env, record["trace"], modules)
    after = snapshot(env)
    account = {"email": str(env.inbox.account_email), "current_day": env.calendar.current_day.isoformat()}

    workspace = {"account": account, "before": before, "after": after}
    provenance = {
        "stimulus_folder": folder, "stimulus_id": stim_prov["stimulus_id"], "builder_version": BUILDER_VERSION,
        "source": source, "agentdojo_commit": record["agentdojo_commit"],
        "environment": "AgentDojo workspace environment.yaml with default injection-vector values, "
                       "then the task's init_environment (no injection)",
        "replay": {"calls": len(checks), "all_match_saved_results": all(c["matches_saved_result"] for c in checks),
                   "checks": checks},
        "changes": diff_snapshots(before, after),
    }
    target = out_dir / folder
    target.mkdir(parents=True, exist_ok=True)
    texts = {"workspace.json": workspace, "provenance.json": provenance}
    for name, data in texts.items():
        (target / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"folder": folder, "calls": len(checks), "changes": provenance["changes"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build workspace before/after snapshots for active stimuli.")
    parser.add_argument("--folders", nargs="+", default=["user_task_13__no_injection"])
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--stimuli-dir", type=Path, default=STIMULI_DIR)
    args = parser.parse_args()
    for folder in args.folders:
        try:
            summary = build_one(folder, args.out_dir, args.stimuli_dir)
        except ReplayError as error:
            print(f"FAILED {folder}: {error}")
            sys.exit(1)
        print(f"{folder}: replayed {summary['calls']} tool calls, all matching; changes: {summary['changes']}")


if __name__ == "__main__":
    main()
