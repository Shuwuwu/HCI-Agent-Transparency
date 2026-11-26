"""
Purpose:
    The participant-facing experiment flow: turn an opaque link token into the one stage that participant is to
    be shown now, and advance them one stage at a time. This is the only place that maps a token to a stimulus,
    so nothing a participant can edit (a URL, a form field, a request body) decides which scenario or
    transparency condition they are shown.

    The stage descriptors sent to the browser deliberately carry no stimulus folder, scenario name or transparency
    condition. The folder name encodes whether a trial is safe or injected (user_task_9__no_injection versus
    user_task_9__g2_ii_wg), so a participant reading the page source or the network panel would learn what to
    expect from the trial; the condition would tell them that other participants see something different. Both are
    resolved on the server and only the resulting content is sent.

Inputs:
    A participant database connection (participant_store.py), an experiment configuration and the assignments
    payload it names (experiment_plan.py), and a link token taken from the request.

Outputs:
    Stage descriptors and progress counts for the browser (no stimulus identity); the (stimulus folder, condition)
    pair the scenario endpoints resolve on the server; and a `session_start` interaction event, stamped with the
    true stimulus and condition, the first time a scenario stage is entered.

Dependencies:
    participant_store.py (participant records and stage progress), experiment_plan.py (the derived stage
    sequence), interaction_log.py (the session_start event). Used by participant_server.py.

Usage:
    Imported; it has no command line of its own. The flow is served by:
        python3 -B src/scripts/participant_server.py \
            --experiment-config src/data/experiment_configs/development.json
"""

import re
import sqlite3
import sys

import experiment_plan as ep
import interaction_log as ilog
import participant_store as store

# Shape of a link token as participant_store.enrol writes it (secrets.token_urlsafe): URL-safe base64 characters.
# Checked before the database is touched so a malformed path cannot become a query.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def participant_for(conn: sqlite3.Connection, token: str) -> dict:
    """The participant record whose link token this is.

    Raises ValueError for a token that is not even the right shape (never a database lookup) and
    FileNotFoundError for a well-formed token that belongs to nobody, so a guessed link is answered the same way
    as a missing page.
    """
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ValueError("invalid token")
    row = store.by_token(conn, token)
    if row is None:
        raise FileNotFoundError("unknown token")
    return row


def _plan_and_stage(conn: sqlite3.Connection, config: ep.Config, participant: str,
                    payload: dict | None = None) -> tuple[dict, dict | None, list[str]]:
    """(plan, current stage or None, completed stage keys) for one participant.

    The plan is verified against the fingerprint stored at enrolment, so a configuration or assignment change
    since this participant started raises participant_store.StoreError rather than resuming them into a
    different experiment.
    """
    plan = store.checked_plan(conn, config, participant, payload)
    done = store.completed_stages(conn, participant)
    stage = next((s for s in plan["stages"] if s["key"] not in set(done)), None)
    return plan, stage, done


def descriptor(stage: dict, plan: dict) -> dict:
    """The participant-safe description of one stage.

    Carries what the browser needs to draw a screen — the stage key, its kind, its position, and for a trial the
    trial id (which is also the interaction-log session id) and whether it is the practice trial. It deliberately
    omits the stimulus folder, the scenario name and the transparency condition: see the module docstring.
    """
    out = {"key": stage["key"], "kind": stage["kind"], "index": stage["index"]}
    if "trial_id" in stage:
        out["trial_id"] = stage["trial_id"]
        out["practice"] = bool(stage.get("practice"))
        if not stage.get("practice"):
            out["trial"] = stage.get("trial")
            out["trials"] = sum(1 for s in plan["stages"] if s["kind"] == "scenario" and not s.get("practice"))
    return out


def _progress(plan: dict, done: list[str]) -> dict:
    """How far through the sequence the participant is, for a progress indicator."""
    return {"completed": len(done), "total": len(plan["stages"])}


def _log_session_start(stage: dict, log_dir) -> None:
    """Record the trial's true stimulus and condition against its trial id, server side.

    The browser is never told either value, so it cannot report them; stamping the event here keeps the
    interaction log authoritative while the page stays free of the trial's identity. A logging failure must never
    stop a participant mid-study, so it is reported to stderr and the stage is still served.
    """
    try:
        ilog.append_event({"session": stage["trial_id"], "type": "session_start",
                           "stimulus": stage["stimulus"], "condition": stage["condition"]}, log_dir)
    except (ilog.LogError, OSError, ValueError) as exc:
        sys.stderr.write(f"[flow] session_start not logged for {stage['trial_id']}: {exc}\n")


def _enter(conn: sqlite3.Connection, config: ep.Config, participant: str, payload: dict | None,
           plan: dict, stage: dict, log_dir) -> dict:
    """Record the participant's arrival at `stage`, stamp a trial's session_start, and return its descriptor.

    Both ways a participant can reach a stage go through here. A participant reaches almost every stage by
    continuing from the previous one rather than by reopening their link, so recording the arrival only on a
    page load would lose the arrival time of nearly every trial — and with it the reading and response times
    measured from it — and would leave those trials with no session_start to tie their interaction events to.
    """
    # Whether this is the first arrival must be read before enter_stage inserts the row.
    first = conn.execute("SELECT 1 FROM progress WHERE participant_id = ? AND stage_key = ?",
                         (participant, stage["key"])).fetchone() is None
    store.enter_stage(conn, config, participant, payload)
    if first and stage["kind"] == "scenario" and log_dir is not None:
        _log_session_start(stage, log_dir)
    return descriptor(stage, plan)


def stage_payload(conn: sqlite3.Connection, config: ep.Config, participant: str, payload: dict | None = None,
                  log_dir=None) -> dict:
    """Enter and describe the stage this participant is to be shown now.

    Returns {"stage": descriptor or None, "progress": {...}}; the stage is None once every stage has been
    completed. Called on every page load of the flow, so it is also the resume path: it always returns the same
    stage until that stage is completed, and the arrival time it records is the first one.
    """
    plan, stage, done = _plan_and_stage(conn, config, participant, payload)
    if stage is None:
        return {"stage": None, "progress": _progress(plan, done)}
    return {"stage": _enter(conn, config, participant, payload, plan, stage, log_dir),
            "progress": _progress(plan, done)}


def scenario_target(conn: sqlite3.Connection, config: ep.Config, participant: str,
                    payload: dict | None = None) -> tuple[str, str]:
    """The (stimulus folder, condition) the scenario endpoints are to serve for this participant, right now.

    Raises FileNotFoundError when the participant's current stage shows no scenario — consent, instructions, an
    assessment, the questionnaire, or the end of the sequence — so a stage that is not a scenario cannot be used
    to fetch scenario content, and a participant cannot reach the next trial's content early or the previous
    trial's content again.
    """
    _, stage, _ = _plan_and_stage(conn, config, participant, payload)
    if stage is None or stage["kind"] != "scenario":
        raise FileNotFoundError("no scenario at this stage")
    return stage["stimulus"], stage["condition"]


def advance(conn: sqlite3.Connection, config: ep.Config, participant: str, stage_key: str,
            payload: dict | None = None, log_dir=None) -> dict:
    """Complete `stage_key`, enter the next stage, and return it the same shape stage_payload returns it.

    The stage key must be the participant's current stage: participant_store.complete_stage ignores a repeated
    submission of a stage already completed (so a double click or a reloaded form cannot record a trial twice)
    and refuses a later one (so no stage can be skipped). The next stage is entered here because the answer to
    this call is what the browser draws — it does not ask for the stage again — so this is the participant's
    arrival at it.
    """
    store.complete_stage(conn, config, participant, stage_key, payload)
    plan, stage, done = _plan_and_stage(conn, config, participant, payload)
    return {"stage": _enter(conn, config, participant, payload, plan, stage, log_dir) if stage else None,
            "progress": _progress(plan, done)}
