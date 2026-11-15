"""
Purpose:
    The wording rules of the participant-facing action log: how one recorded tool call (tool name, arguments, result
    text or error) becomes a short action sentence, a list of argument values and an outcome summary. Every rule is
    a fixed template over the call's own fields, so the same call always gives the same text, and no rule looks at
    what the call means: nothing here knows or says whether a call was authorised.

    What is deliberately left out: free text found in results (an event description, an email body, a file's
    content), because a result can carry text written by someone else. Outcome summaries use only identifying fields
    (titles, subjects, senders, recipients, file names, times, counts). The agent's own arguments are shown, cut to
    MAX_ARGUMENT_CHARS characters and marked when cut.

Inputs:
    A tool name, its arguments (a dict) and its result (text or error text) as recorded by AgentDojo.

Outputs:
    action_text(tool, args) -> str; argument_items(args) -> list of {"name", "value", "truncated"};
    outcome(tool, content, error) -> {"status", "summary", "fallback"}.

Dependencies:
    tool_result_parser.py (reads the YAML-style result text). Standard library only.

Usage:
    import stimulus_text as stx
    stx.action_text("get_day_calendar_events", {"day": "2024-05-15"})   # "Looked up calendar events for 2024-05-15"
"""

import re

import tool_result_parser as trp

MAX_ARGUMENT_CHARS = 300  # an argument value longer than this is cut and marked
MAX_LISTED = 5            # a result listing names at most this many items, then "and N more"
MAX_PLAIN_RESULT_CHARS = 120  # a one-line text result up to this long is shown as it is
PERMISSIONS = {"r": "read", "rw": "read and write"}
ERROR_PREFIX = re.compile(r"^[A-Za-z_.]*(?:Error|Exception):\s*")
TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?::\d{2})?$")


# ----------------------------------------------------------------------------------------------- arguments

def display_value(value) -> str:
    """One argument value as text: lists joined by ", ", mappings as "key: value" pairs, booleans as yes/no."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        return value if value != "" else "(empty)"
    if isinstance(value, list):
        return ", ".join(display_value(v) for v in value)
    if isinstance(value, dict):
        return ", ".join(f"{k}: {display_value(v)}" for k, v in value.items())
    return str(value)


def argument_items(args: dict) -> list[dict]:
    """The arguments the agent gave, in the order given. Empty ones (null, [], {}) are left out; long values are cut."""
    items = []
    for name, value in args.items():
        if value is None or value == [] or value == {}:
            continue
        text = display_value(value)
        truncated = len(text) > MAX_ARGUMENT_CHARS
        items.append({"name": name, "value": text[:MAX_ARGUMENT_CHARS] + "..." if truncated else text,
                      "truncated": truncated})
    return items


# --------------------------------------------------------------------------------------------------- actions

def _q(value) -> str:
    return f"'{display_value(value)}'"


def _has(args: dict, name: str) -> bool:
    return args.get(name) not in (None, "", [], {})


ACTIONS = {
    "get_current_day": lambda a: "Checked the current date",
    "get_day_calendar_events": lambda a: f"Looked up calendar events for {display_value(a.get('day'))}",
    "search_calendar_events": lambda a: ("Searched the calendar for " + (_q(a["query"]) if _has(a, "query") else "all events")
                                         + (f" on {display_value(a['date'])}" if _has(a, "date") else "")),
    "create_calendar_event": lambda a: f"Created calendar event {_q(a.get('title'))}",
    "add_calendar_event_participants": lambda a: f"Added participants to calendar event {display_value(a.get('event_id'))}",
    "reschedule_calendar_event": lambda a: f"Rescheduled calendar event {display_value(a.get('event_id'))}",
    "cancel_calendar_event": lambda a: f"Cancelled calendar event {display_value(a.get('event_id'))}",
    "search_emails": lambda a: ("Searched emails for " + (_q(a["query"]) if _has(a, "query") else "all emails")
                                + (f" from {display_value(a['sender'])}" if _has(a, "sender") else "")),
    "get_unread_emails": lambda a: "Retrieved unread emails",
    "get_received_emails": lambda a: "Retrieved received emails",
    "get_sent_emails": lambda a: "Retrieved sent emails",
    "get_draft_emails": lambda a: "Retrieved draft emails",
    "send_email": lambda a: f"Sent an email to {display_value(a.get('recipients'))}",
    "delete_email": lambda a: f"Deleted email {display_value(a.get('email_id'))}",
    "search_contacts_by_name": lambda a: f"Looked up a contact by name {_q(a.get('query'))}",
    "search_contacts_by_email": lambda a: f"Looked up a contact by email address {_q(a.get('query'))}",
    "list_files": lambda a: "Listed all files in the cloud drive",
    "search_files": lambda a: f"Searched files for the text {_q(a.get('query'))}",
    "search_files_by_filename": lambda a: f"Searched files named {_q(a.get('filename'))}",
    "get_file_by_id": lambda a: f"Opened file {display_value(a.get('file_id'))}",
    "create_file": lambda a: f"Created file {_q(a.get('filename'))}",
    "append_to_file": lambda a: f"Added content to file {display_value(a.get('file_id'))}",
    "delete_file": lambda a: f"Deleted file {display_value(a.get('file_id'))}",
    "share_file": lambda a: (f"Shared file {display_value(a.get('file_id'))} with {display_value(a.get('email'))}"
                             + (f" ({PERMISSIONS.get(a['permission'], a['permission'])} access)" if _has(a, "permission") else "")),
}


def action_text(tool: str, args: dict) -> str:
    """The action sentence for a call; a tool without a rule gets "Called <tool>"."""
    rule = ACTIONS.get(tool)
    if rule is None:
        return f"Called {tool}"
    try:
        return rule(args)
    except (KeyError, TypeError, AttributeError):
        return f"Called {tool}"


# -------------------------------------------------------------------------------------------------- outcomes

def _span(start, end) -> str:
    """'2024-05-15 10:00-11:00' for a same-day span, otherwise '2024-05-15 10:00 to 2024-05-16 09:00'."""
    a, b = TIMESTAMP.match(str(start)), TIMESTAMP.match(str(end))
    if not (a and b):
        return f"{start} to {end}"
    return f"{a[1]} {a[2]}-{b[2]}" if a[1] == b[1] else f"{a[1]} {a[2]} to {b[1]} {b[2]}"


def _records(value) -> list[dict]:
    """A parsed result as a list of records (a single record becomes a list of one; null becomes empty)."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
        return value
    raise TypeError("result is not a record or a list of records")


def _event(r: dict) -> str:
    return f"{_q(r['title'])} ({_span(r['start_time'], r['end_time'])})"


def _email(r: dict) -> str:
    return f"{_q(r['subject'])} from {r['sender']}"


def _contact(r: dict) -> str:
    return f"{r['name']} ({r['email']})"


def _file(r: dict) -> str:
    return _q(r["filename"])


def _listing(noun: str, items: list[str], none: str) -> str:
    if not items:
        return none
    shown = "; ".join(items[:MAX_LISTED]) + (f"; and {len(items) - MAX_LISTED} more" if len(items) > MAX_LISTED else "")
    return f"Found {len(items)} {noun}{'' if len(items) == 1 else 's'}: {shown}"


def _list_outcome(noun: str, label, none: str):
    return lambda value: _listing(noun, [label(r) for r in _records(value)], none)


def _sent(value) -> str:
    r = _records(value)[0]
    text = f"Email sent to {', '.join(r['recipients'])}"
    for field in ("cc", "bcc"):
        if r.get(field):
            text += f"; {field}: {', '.join(r[field])}"
    text += f" with subject {_q(r['subject'])}"
    if r.get("attachments"):
        text += f" and {len(r['attachments'])} attachment{'s' if len(r['attachments']) != 1 else ''}"
    return text


def _event_result(verb: str):
    def make(value) -> str:
        r = _records(value)[0]
        people = ", ".join(r["participants"]) if r.get("participants") else "none"
        return f"{verb} {_event(r)}; participants: {people}"
    return make


def _shared(value) -> str:
    r = _records(value)[0]
    access = ", ".join(f"{who} ({PERMISSIONS.get(p, p)})" for who, p in (r.get("shared_with") or {}).items()) or "no one"
    return f"File {_file(r)} is now shared with: {access}"


OUTCOMES = {
    "get_day_calendar_events": _list_outcome("event", _event, "No events found"),
    "search_calendar_events": _list_outcome("event", _event, "No events found"),
    "create_calendar_event": _event_result("Created event"),
    "add_calendar_event_participants": _event_result("Updated event"),
    "reschedule_calendar_event": _event_result("Rescheduled event"),
    "cancel_calendar_event": _event_result("Cancelled event"),
    "search_emails": _list_outcome("email", _email, "No emails found"),
    "get_unread_emails": _list_outcome("email", _email, "No emails found"),
    "get_received_emails": _list_outcome("email", _email, "No emails found"),
    "get_sent_emails": _list_outcome("email", _email, "No emails found"),
    "get_draft_emails": _list_outcome("email", _email, "No emails found"),
    "send_email": _sent,
    "search_contacts_by_name": _list_outcome("contact", _contact, "No contacts found"),
    "search_contacts_by_email": _list_outcome("contact", _contact, "No contacts found"),
    "list_files": _list_outcome("file", _file, "No files found"),
    "search_files": _list_outcome("file", _file, "No files found"),
    "search_files_by_filename": _list_outcome("file", _file, "No files found"),
    "get_file_by_id": lambda v: f"Retrieved file {_file(_records(v)[0])}",
    "create_file": lambda v: f"Created file {_file(_records(v)[0])}",
    "append_to_file": lambda v: f"Appended to file {_file(_records(v)[0])}",
    "delete_file": lambda v: f"Deleted file {_file(_records(v)[0])}",
    "share_file": _shared,
}


def _plain(content: str) -> str:
    """Summary of a result that is plain text: short single lines are shown as they are, others only by length."""
    text = content.strip()
    if text and "\n" not in text and len(text) <= MAX_PLAIN_RESULT_CHARS:
        return text
    return f"Returned text ({len(text)} characters)"


def outcome(tool: str, content: str | None, error: str | None) -> dict:
    """The outcome of one call: {"status": "success"|"error"|"no result", "summary": text, "fallback": bool}.

    `fallback` is true when a generic summary was used because the result did not have the expected shape (so a
    researcher can find and check those cases); it is never shown to participants.
    """
    if error:
        return {"status": "error", "summary": "Error: " + (ERROR_PREFIX.sub("", error.strip()) or "the tool failed"),
                "fallback": False}
    if content is None:
        return {"status": "no result", "summary": "No result was recorded", "fallback": False}
    if content.strip() == "":
        return {"status": "success", "summary": "Returned nothing", "fallback": False}
    rule = OUTCOMES.get(tool)
    try:
        if rule is None:
            if tool in ("get_current_day", "delete_email"):
                return {"status": "success", "summary": _plain(content), "fallback": False}
            raise TypeError("no outcome rule for this tool")
        return {"status": "success", "summary": rule(trp.parse(content)), "fallback": False}
    except (trp.ParseError, KeyError, TypeError, AttributeError, IndexError):
        return {"status": "success", "summary": f"Returned a result ({len(content)} characters)", "fallback": True}
