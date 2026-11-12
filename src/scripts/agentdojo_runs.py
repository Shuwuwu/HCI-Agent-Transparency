"""
Purpose:
    Shared helpers for reading the recorded AgentDojo gpt-4o-2024-05-13 workspace runs that are stored
    in this repository, so every script parses run files the same way.

Inputs:
    Run files under src/data/gpt-4o-2024-05-13/workspace/user_task_N/<attack>/<file>.json
    (see the README in that folder for their source and format).

Outputs:
    None. This is a library module: it returns parsed run dictionaries and small derived values.

Dependencies:
    Python standard library only. Imported by extract_scenarios.py (and by later selection scripts).

Usage:
    Imported, not executed:
        from agentdojo_runs import load_run, tool_calls
        run = load_run("user_task_0")
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "src" / "data" / "gpt-4o-2024-05-13" / "workspace"

# AgentDojo builds combined tasks by joining two user prompts with this prefix
# (task_combinators.py at commit af72663). Combined tasks are recognised from the prompt alone
# because the local run files do not record which tasks they were built from.
COMBINED_PREFIX = "Perform the following tasks in the order they are given:"

# Which of our three tool groups each workspace tool belongs to. Copied from the TOOLS list in AgentDojo's
# workspace task_suite.py at commit af72663 (24 tools); update only if that source list changes.
TOOL_CATEGORY = {
    # Email tools
    "send_email": "email_tools",
    "delete_email": "email_tools",
    "get_unread_emails": "email_tools",
    "get_sent_emails": "email_tools",
    "get_received_emails": "email_tools",
    "get_draft_emails": "email_tools",
    "search_emails": "email_tools",
    "search_contacts_by_name": "email_tools",
    "search_contacts_by_email": "email_tools",
    # Calendar tools
    "get_current_day": "calendar_tools",
    "search_calendar_events": "calendar_tools",
    "get_day_calendar_events": "calendar_tools",
    "create_calendar_event": "calendar_tools",
    "cancel_calendar_event": "calendar_tools",
    "reschedule_calendar_event": "calendar_tools",
    "add_calendar_event_participants": "calendar_tools",
    # Drive tools
    "append_to_file": "drive_tools",
    "search_files_by_filename": "drive_tools",
    "create_file": "drive_tools",
    "delete_file": "drive_tools",
    "get_file_by_id": "drive_tools",
    "list_files": "drive_tools",
    "share_file": "drive_tools",
    "search_files": "drive_tools",
}


def task_number(user_task_id: str) -> int:
    """Return N for 'user_task_N', so tasks sort numerically (user_task_2 before user_task_10)."""
    return int(user_task_id.rsplit("_", 1)[1])


def list_user_task_ids() -> list[str]:
    """Return every user task that has a no-attack run, sorted numerically."""
    ids = [p.parent.parent.name for p in RUNS_DIR.glob("user_task_*/none/none.json")]
    return sorted(ids, key=task_number)


def load_run(user_task_id: str, attack: str = "none", injection_task_id: str | None = None) -> dict:
    """Load one recorded run.

    Args:
        user_task_id: e.g. "user_task_21".
        attack: attack folder name; "none" for the no-attack run.
        injection_task_id: e.g. "injection_task_0"; required for any attack other than "none".

    Returns:
        The parsed run file (keys include utility, security, error, injections, messages).

    Raises:
        ValueError: if an attack is given without an injection task.
        FileNotFoundError: if the run file does not exist.
    """
    if attack == "none":
        filename = "none.json"
    elif injection_task_id is None:
        raise ValueError(f"attack {attack!r} needs an injection_task_id")
    else:
        filename = f"{injection_task_id}.json"
    path = RUNS_DIR / user_task_id / attack / filename
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def user_prompt(run: dict) -> str:
    """Return the user's request. Fails loudly unless the run has exactly one user message."""
    prompts = [m["content"] for m in run["messages"] if m["role"] == "user"]
    if len(prompts) != 1:
        raise ValueError(f"{run['user_task_id']}: expected 1 user message, found {len(prompts)}")
    return prompts[0]


def tool_calls(run: dict) -> list[dict]:
    """Return every tool call the agent made as {"function", "args"}, in order.

    Parallel calls inside one assistant message are included in the order they appear.
    """
    calls = []
    for message in run["messages"]:
        for call in message.get("tool_calls") or []:
            calls.append({"function": call["function"], "args": call["args"]})
    return calls


def failed_tool_call_count(run: dict) -> int:
    """Count tool results that carry an error (e.g. 'No events found'); the agent usually retries."""
    return sum(1 for m in run["messages"] if m["role"] == "tool" and m.get("error") is not None)


def intermediate_agent_texts(run: dict) -> list[str]:
    """Return the agent's non-empty text written before the final response.

    These are assistant messages that also carry tool calls (the agent "thinking out loud" or
    answering part of the request mid-run); the final response is excluded.
    """
    return [
        m["content"]
        for m in run["messages"][:-1]
        if m["role"] == "assistant" and m.get("content")
    ]


def final_response(run: dict) -> str:
    """Return the agent's final answer: the last message, which must be assistant text without tool calls."""
    last = run["messages"][-1]
    if last["role"] != "assistant" or last.get("tool_calls"):
        raise ValueError(f"{run['user_task_id']}: last message is not a plain assistant response")
    return last["content"]


def tools_called_by_group(run: dict) -> dict[str, list[str]]:
    """Group the distinct tools the agent called into email/calendar/drive lists.

    Each tool appears once, at its first call, so order within a group is first-use order.
    Raises ValueError for a tool missing from TOOL_CATEGORY (a sign the table needs updating).
    """
    grouped = {"email_tools": [], "calendar_tools": [], "drive_tools": []}
    for call in tool_calls(run):
        name = call["function"]
        if name not in TOOL_CATEGORY:
            raise ValueError(f"{run['user_task_id']}: unknown tool {name!r}")
        if name not in grouped[TOOL_CATEGORY[name]]:
            grouped[TOOL_CATEGORY[name]].append(name)
    return grouped
