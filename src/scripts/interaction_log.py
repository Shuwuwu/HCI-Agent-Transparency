"""
Purpose:
    Define, validate and summarise the interaction events of the participant flow: what a participant does in a
    trial besides reading the conversation. That covers the Mail, Calendar and Drive tabs chosen and for how long,
    the items opened in them and how long each item stayed on screen, the Skills tab and each skill document
    opened and for how long, the activity cards expanded, collapsed and how long each stayed open (where the view
    shows them), and how long the conversation was visible before the participant continued.

    The workspace panel is always on screen beside the conversation, and its default Mail list is neither logged
    nor timed, so every workspace event is something the participant chose to do. `workspace_panel_open` and
    `workspace_panel_close` belong to the earlier interface, in which the panel was opened and closed with a
    button: the current pages never send them, and they stay accepted only so that records made with that
    interface remain valid and are summarised as they were recorded.

    The events are stored in the participant database (`events` table, participant_store.record_event), against
    the participant, trial and stage they belong to; this module never writes anything. It decides which events
    may be stored: unknown types, unknown or wrongly typed fields and oversized values are refused, so nothing
    but the listed interaction facts can reach the database.

    Two types are written by the server only and refused from the browser: `session_start` (the first time a
    trial's scenario is shown) and `stage_load` (every time the flow page asks for the current stage, so reloads
    and resumed sessions are visible in the record).

Inputs:
    Event dicts as the browser posts them (participant_server.py -> POST /api/log, via
    participant_flow.log_event): {"trial_id", "type", "client_ms"?, ...the type's fields}. The link token that
    comes with them is checked by participant_flow before validate_event is called.

Outputs:
    validate_event: the event reduced to its allowed fields. summarize: the measures of one trial from its stored
    event rows. The command line prints those measures for every trial in a database.

Dependencies:
    participant_store.py (EVENT_TYPES and the database), experiment_plan.py (through it: which database a
    configuration names). Python standard library only.
    Used by participant_flow.py; tests/test_interaction_log.py.

Usage:
    python3 -B src/scripts/interaction_log.py --config src/data/experiment_configs/development.json [--participant ID]
    (per-trial measures as JSON lines; --db PATH reads another database file)
"""

import argparse
import datetime
import json
import re
from pathlib import Path

import participant_store as store

TRIAL_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
CARD_RE = re.compile(r"^step-\d{1,4}$")  # scenario_view.js names a card after its displayed step number
MAX_EVENTS_PER_TRIAL = 2000
MAX_EVENT_BYTES = 2000
TABS = ("mail", "calendar", "drive")

# event type -> {field: validator}. `client_ms` (browser clock, ms since epoch) is allowed on every event.
_tab = lambda v: v in TABS  # noqa: E731
_token = lambda v: isinstance(v, str) and bool(TOKEN_RE.match(v))  # noqa: E731
_card = lambda v: isinstance(v, str) and bool(CARD_RE.match(v))  # noqa: E731
_ms = lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**13  # noqa: E731
EVENT_FIELDS = {
    "session_start": {},
    "stage_load": {},
    "scenario_visible": {"duration_ms": _ms},
    "workspace_panel_open": {},  # earlier interface only (see the module docstring)
    "workspace_panel_close": {"duration_ms": _ms},  # earlier interface only
    "workspace_tab_open": {"tab": _tab},
    "workspace_tab_view": {"tab": _tab, "duration_ms": _ms},
    "workspace_item_open": {"tab": _tab, "item_id": _token},
    # One continuous display of a Mail/Calendar/Drive item (workspace.js); an item shown again later, or after
    # the panel is reopened on it, gets another event, so its viewing time is the sum of its events.
    "workspace_item_view": {"tab": _tab, "item_id": _token, "duration_ms": _ms},
    "skills_tab_open": {},
    "skills_tab_view": {"duration_ms": _ms},
    "skill_doc_open": {"skill_id": _token},
    "skill_doc_view": {"skill_id": _token, "duration_ms": _ms},
    "trace_card_expand": {"card_id": _card},
    "trace_card_collapse": {"card_id": _card},
    "trace_card_view": {"card_id": _card, "duration_ms": _ms},
}
SERVER_ONLY = ("session_start", "stage_load")
# Events that only a deliberate action in the workspace panel produces. workspace_panel_open is the earlier
# interface's button (see the module docstring); the current panel cannot be opened, so it never sends it.
INSPECTION_EVENTS = ("workspace_panel_open", "workspace_tab_open", "workspace_item_open", "skills_tab_open",
                     "skill_doc_open")
FIELD_COLUMNS = ("tab", "item_id", "skill_id", "card_id", "duration_ms")  # the event columns of the database


class LogError(ValueError):
    """The posted event is not an allowed interaction record."""


def validate_event(event, origin: str = "browser") -> dict:
    """Return the event reduced to {"trial_id", "type", "client_ms"?, ...its fields}, or raise LogError.

    `trial_id` and `type` are required, and every field the type lists. A server-only type is refused unless
    `origin` is "server". Whether the trial is the one the participant is on is checked by the caller
    (participant_flow.log_event), which knows the participant.
    """
    if not isinstance(event, dict):
        raise LogError("event must be an object")
    if len(json.dumps(event)) > MAX_EVENT_BYTES:
        raise LogError("event too large")
    trial, kind = event.get("trial_id"), event.get("type")
    if not isinstance(trial, str) or not TRIAL_RE.match(trial):
        raise LogError("invalid trial id")
    if kind not in EVENT_FIELDS or (kind in SERVER_ONLY and origin != "server"):
        raise LogError("unknown event type")
    allowed = EVENT_FIELDS[kind]
    extra = set(event) - set(allowed) - {"trial_id", "type", "client_ms"}
    if extra:
        raise LogError(f"unexpected fields: {sorted(extra)}")
    clean = {"trial_id": trial, "type": kind}
    for name, ok in {**allowed, "client_ms": _ms}.items():
        if name in event:
            if not ok(event[name]):
                raise LogError(f"invalid value for {name}")
            clean[name] = event[name]
    for name in allowed:
        if name not in clean:
            raise LogError(f"missing field {name}")
    return clean


def summarize(events: list[dict]) -> dict:
    """The measures of one trial, from its event rows (participant_store.trial_events) in arrival order.

    skills_opened: any skill document opened; first_skill_open_s: seconds from the trial's session_start to the
    first skill_doc_open (None if either is missing); skills_viewed_ms: {skill id: total ms} from skill_doc_view;
    skills_tab_opened / skills_tab_ms: the Skills tab was selected / total ms on it; workspace_inspected: the
    participant did anything in the workspace panel (chose a tab, opened an item, the Skills tab or a skill
    document; in records of the earlier interface, also opened the panel) — the panel being on screen by default
    never counts; workspace_tabs_viewed_ms: {tab: total ms} on Mail, Calendar and Drive after the participant
    chose them (the default Mail list is not timed); workspace_items_opened: count;
    workspace_items_viewed_ms: {tab: {item id: total ms}} from workspace_item_view (nested by tab because item ids
    are only unique within their tab: an email and a calendar event can share an id);
    trace_cards_expanded: distinct cards expanded; trace_cards_viewed_ms: {card id: total ms};
    scenario_visible_ms: total visible time of the conversation (summed over reloads); stage_loads: how often the
    page asked for the stage while in this trial. Durations are measured in the browser while the page is visible.
    """
    def total(kind):
        return sum(e["duration_ms"] or 0 for e in events if e["type"] == kind)

    def by(kind, key):
        out = {}
        for e in events:
            if e["type"] == kind:
                out[e[key]] = out.get(e[key], 0) + (e["duration_ms"] or 0)
        return out

    items = {}
    for e in events:
        if e["type"] == "workspace_item_view":
            per_tab = items.setdefault(e["tab"], {})
            per_tab[e["item_id"]] = per_tab.get(e["item_id"], 0) + (e["duration_ms"] or 0)

    start = next((e for e in events if e["type"] == "session_start"), None)
    first_open = next((e for e in events if e["type"] == "skill_doc_open"), None)
    seconds = None
    if start and first_open:
        fmt = datetime.datetime.fromisoformat
        seconds = round((fmt(first_open["server_time"]) - fmt(start["server_time"])).total_seconds(), 3)
    return {
        "skills_tab_opened": any(e["type"] == "skills_tab_open" for e in events),
        "skills_tab_ms": total("skills_tab_view"),
        "skills_opened": first_open is not None,
        "first_skill_open_s": seconds,
        "skills_viewed_ms": by("skill_doc_view", "skill_id"),
        "workspace_inspected": any(e["type"] in INSPECTION_EVENTS for e in events),
        "workspace_tabs_viewed_ms": by("workspace_tab_view", "tab"),
        "workspace_items_opened": sum(e["type"] == "workspace_item_open" for e in events),
        "workspace_items_viewed_ms": items,
        "trace_cards_expanded": len({e["card_id"] for e in events if e["type"] == "trace_card_expand"}),
        "trace_cards_viewed_ms": by("trace_card_view", "card_id"),
        "scenario_visible_ms": total("scenario_visible"),
        "stage_loads": sum(e["type"] == "stage_load" for e in events),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Print the interaction measures of every trial in a database.")
    parser.add_argument("--config", type=Path, default=store.ep.CONFIG_PATH,
                        help="experiment configuration whose database to read (default: the study one)")
    parser.add_argument("--db", type=Path, default=None, help="read this database file instead")
    parser.add_argument("--participant", help="only this participant")
    args = parser.parse_args()
    config = store.ep.load_config(args.config)
    conn = store.connect(args.db or config["database_path"], config["environment"])
    trials = conn.execute("SELECT participant_id, trial_id, practice, stimulus, condition FROM plan_stages "
                          "WHERE kind = 'scenario' AND (? IS NULL OR participant_id = ?) "
                          "ORDER BY participant_id, stage_index", (args.participant, args.participant)).fetchall()
    for row in trials:
        measures = summarize(store.trial_events(conn, row["participant_id"], row["trial_id"]))
        print(json.dumps({"participant": row["participant_id"], "trial_id": row["trial_id"],
                          "practice": bool(row["practice"]), "stimulus": row["stimulus"],
                          "condition": row["condition"], **measures}))


if __name__ == "__main__":
    main()
