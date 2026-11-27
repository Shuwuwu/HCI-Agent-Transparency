"""
Purpose:
    Apply the researcher-decided presentation edits of src/data/presentation_edits.json to the text a stimulus is
    served with. An edit is an exact text replacement in one served field (for example a date in the final
    response) made when the stimulus is served, so the stimulus files, the workspace snapshots and the execution
    records stay exactly as built and recorded. Each edit states how often its text must occur; any other count
    is an error, so an edit can never silently miss or hit the wrong text after a stimulus is rebuilt.

Inputs:
    src/data/presentation_edits.json: {"description", "stimuli": {<stimulus folder>: {"decided", "reason",
    "edits": [{"field", "old", "new", "occurrences"}]}}}. "field" is one of EDITABLE_FIELDS.

Outputs:
    No files. apply() returns an edited copy of a dict of served fields.

Dependencies:
    Standard library only. Used by participant_server.py (participant_view, startup check) and review_stimuli.py
    (check A5, which compares the served final response with the execution record's).

Usage:
    import presentation_edits as pe
    view = pe.apply("user_task_29__no_injection", view)
"""

import json
from functools import lru_cache
from pathlib import Path

EDITS_FILE = Path(__file__).resolve().parents[2] / "src" / "data" / "presentation_edits.json"
# Only these served fields can be edited; both conditions carry them, so an edit keeps low and high identical.
EDITABLE_FIELDS = ("user_request", "final_response")


@lru_cache(maxsize=None)
def load(path: Path = EDITS_FILE) -> dict:
    """The edits per stimulus folder: {folder: [edit, ...]}. Raises ValueError for a malformed file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {}
    for folder, entry in data["stimuli"].items():
        if not (entry.get("reason") or "").strip() or not entry.get("decided"):
            raise ValueError(f"{path}: {folder} needs a reason and a decision date")
        for edit in entry["edits"]:
            if edit["field"] not in EDITABLE_FIELDS:
                raise ValueError(f"{path}: {folder}: field {edit['field']!r} cannot be edited")
            if not edit["old"] or edit["old"] == edit["new"] or int(edit["occurrences"]) < 1:
                raise ValueError(f"{path}: {folder}: an edit needs a non-empty, changed text and occurrences >= 1")
        out[folder] = entry["edits"]
    return out


def edits_for(folder: str) -> list[dict]:
    """The edits of one stimulus folder (empty for a stimulus without edits)."""
    return load().get(folder, [])


def apply(folder: str, fields: dict) -> dict:
    """A copy of `fields` with the folder's edits applied, in the order listed.

    `fields` is any dict holding the edited fields (a participant view, or {"final_response": ...} built from an
    execution record). An edit whose field is absent from `fields` is skipped. Raises ValueError when an edit's
    "old" text does not occur exactly "occurrences" times in its field.
    """
    out = dict(fields)
    for edit in edits_for(folder):
        if edit["field"] not in out:
            continue
        text = out[edit["field"]]
        found = text.count(edit["old"])
        if found != edit["occurrences"]:
            raise ValueError(f"presentation edit for {folder}: {edit['old']!r} occurs {found} times in "
                             f"{edit['field']}, expected {edit['occurrences']}")
        out[edit["field"]] = text.replace(edit["old"], edit["new"])
    return out
