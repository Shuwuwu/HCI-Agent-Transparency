"""
Purpose:
    Record and summarise what participants do in the prototype besides reading the conversation: whether and when
    they open the agent's skill documentation, which skills, for how long, and which workspace tabs they use.
    The Skills tab is one of four equal tabs of the workspace panel (Mail, Calendar, Drive, Skills); opening the
    workspace, selecting the Skills tab and opening an individual skill document are three separate events.
    The records are research data, kept apart from everything participants are shown: they are written to a
    separate folder, one JSON-lines file per browser session, and no participant-facing endpoint reads them.

Inputs:
    Event dicts posted by the browser (participant_server.py -> POST /api/log). Every event is checked against
    EVENT_FIELDS: unknown event types, unknown or wrongly typed fields and oversized values are rejected, so
    nothing but the listed interaction facts can be stored.

Outputs:
    <log-dir>/<session id>.jsonl (default src/data/interaction_logs/, gitignored); the participant server writes
    the practice trial's events to <log-dir>/practice/ instead (participant_flow.log_dir_for). One line per event:
    {"seq", "server_time" (UTC, stamped here, not trusted from the browser), "session", "type", ...fields}.
    summarize() turns one session's events into the measures asked for by the research questions.

Dependencies:
    Python standard library only. Used by participant_server.py; tests/test_interaction_log.py.

Usage:
    python3 -B src/scripts/interaction_log.py [LOG_DIR]     # print a summary of every session in the folder
"""

import datetime
import json
import re
import sys
import threading
from pathlib import Path

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "src" / "data" / "interaction_logs"
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_EVENTS_PER_SESSION = 2000
MAX_EVENT_BYTES = 2000
TABS = ("mail", "calendar", "drive")

# event type -> {field: validator}. `client_ms` (browser clock, ms since epoch) is allowed on every event.
_tab = lambda v: v in TABS  # noqa: E731
_token = lambda v: isinstance(v, str) and bool(TOKEN_RE.match(v))  # noqa: E731
_ms = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 1e13  # noqa: E731
EVENT_FIELDS = {
    "session_start": {"stimulus": _token, "condition": lambda v: v in ("low", "high"), "pid": _token},
    "workspace_panel_open": {},
    "workspace_panel_close": {"duration_ms": _ms},
    "workspace_tab_open": {"tab": _tab},
    "workspace_tab_view": {"tab": _tab, "duration_ms": _ms},
    "workspace_item_open": {"tab": _tab, "item_id": _token},
    "skills_tab_open": {},
    "skills_tab_view": {"duration_ms": _ms},
    "skill_doc_open": {"skill_id": _token},
    "skill_doc_view": {"skill_id": _token, "duration_ms": _ms},
}
_lock = threading.Lock()


class LogError(ValueError):
    """The posted event is not an allowed interaction record."""


def validate_event(event) -> dict:
    """Return the event reduced to its allowed fields, or raise LogError. `session` and `type` are required."""
    if not isinstance(event, dict):
        raise LogError("event must be an object")
    if len(json.dumps(event)) > MAX_EVENT_BYTES:
        raise LogError("event too large")
    session, kind = event.get("session"), event.get("type")
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise LogError("invalid session id")
    if kind not in EVENT_FIELDS:
        raise LogError("unknown event type")
    allowed = EVENT_FIELDS[kind]
    extra = set(event) - set(allowed) - {"session", "type", "client_ms"}
    if extra:
        raise LogError(f"unexpected fields: {sorted(extra)}")
    clean = {"session": session, "type": kind}
    for name, ok in {**allowed, "client_ms": _ms}.items():
        if name in event:
            if not ok(event[name]):
                raise LogError(f"invalid value for {name}")
            clean[name] = event[name]
    for name in allowed:
        if name not in clean and name != "pid":
            raise LogError(f"missing field {name}")
    return clean


def append_event(event, log_dir: Path = DEFAULT_LOG_DIR, now=None) -> dict:
    """Validate and append one event to its session file; returns the stored record.

    The server time is stamped here. A session may hold at most MAX_EVENTS_PER_SESSION events (LogError after).
    """
    clean = validate_event(event)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{clean['session']}.jsonl"
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(timespec="milliseconds")
    with _lock:
        seq = len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0
        if seq >= MAX_EVENTS_PER_SESSION:
            raise LogError("too many events in this session")
        record = {"seq": seq, "server_time": stamp, **clean}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    return record


def read_session(path: Path) -> list[dict]:
    """All records of one session file, in order."""
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize(events: list[dict]) -> dict:
    """The measures of one session (events as read by read_session).

    skills_opened: any skill document opened; first_skill_open_s: seconds from session start to the first
    skill_doc_open (None if never); skills_viewed_ms: {skill id: total ms} from skill_doc_view;
    skills_tab_opened / skills_tab_ms: the Skills tab was selected / total ms on it;
    workspace_opened; workspace_tabs_viewed_ms: {tab: total ms}; workspace_items_opened: count.
    """
    start = next((e for e in events if e["type"] == "session_start"), None)

    def seconds(a, b):
        fmt = datetime.datetime.fromisoformat
        return round((fmt(b) - fmt(a)).total_seconds(), 3)

    first_open = next((e for e in events if e["type"] == "skill_doc_open"), None)
    skills, tabs = {}, {}
    for e in events:
        if e["type"] == "skill_doc_view":
            skills[e["skill_id"]] = skills.get(e["skill_id"], 0) + e["duration_ms"]
        if e["type"] == "workspace_tab_view":
            tabs[e["tab"]] = tabs.get(e["tab"], 0) + e["duration_ms"]
    return {
        "stimulus": start and start["stimulus"], "condition": start and start["condition"],
        "pid": start and start.get("pid"),
        "skills_tab_opened": any(e["type"] == "skills_tab_open" for e in events),
        "skills_tab_ms": sum(e["duration_ms"] for e in events if e["type"] == "skills_tab_view"),
        "skills_opened": first_open is not None,
        "first_skill_open_s": seconds(start["server_time"], first_open["server_time"]) if start and first_open else None,
        "skills_viewed_ms": skills,
        "workspace_opened": any(e["type"] == "workspace_panel_open" for e in events),
        "workspace_tabs_viewed_ms": tabs,
        "workspace_items_opened": sum(e["type"] == "workspace_item_open" for e in events),
    }


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG_DIR
    for path in sorted(folder.glob("*.jsonl")):
        print(path.stem, json.dumps(summarize(read_session(path))))


if __name__ == "__main__":
    main()
