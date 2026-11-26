"""
Purpose:
    Keep the participant records and the flow progress of the experiment in one persistent database: which
    pseudonymous participant each unique link belongs to, which stage of the sequence that participant has
    reached, and when each stage was entered and completed. This is what makes an interrupted session resumable:
    reopening a participant link continues at the first stage the participant has not completed, and no stage can
    be completed twice or out of order, so a reopened link cannot duplicate or renumber a trial.

    Identifiers. The participant id is the pseudonymous id of the assignment row (e.g. "P001"); it carries nothing
    about the person. The link instead carries an opaque, unguessable token, so knowing one participant's link
    tells nobody another participant's link, and the pseudonymous id never has to appear in a URL. Trial ids are
    not stored here: experiment_plan.py derives them from the assignment, and this module records progress by
    stage key only.

    Integrity. A participant's record keeps a fingerprint of the stage sequence they were enrolled under. If the
    configuration later changes in a way that would change their sequence (a different trial count, a different
    practice stimulus, re-written assignments), resuming raises StoreError instead of silently showing a
    participant a different experiment than the one their stored progress belongs to.

    Consent. When a participant agrees to take part, the time, the exact consent text they were shown and its
    SHA-256 are stored with their record (`consents`). Keeping the text itself, not only a version label, means the
    database alone answers "what did this participant agree to?" even after the consent file has been revised.

    Apart from consent, this module stores flow state only. Questionnaire responses and interaction events are
    separate records of the data-collection step and are not written here.

Inputs:
    An experiment configuration and assignments payload (experiment_plan.py) when enrolling participants, and a
    database file (default src/data/participants/participants.db, gitignored: it holds research data).

Outputs:
    The SQLite database, with three tables: `participants` (one row per enrolled participant: token, condition,
    list, trial count, configuration provenance, stage fingerprint, timestamps), `progress` (one row per stage a
    participant has entered, with its completion time) and `consents` (at most one row per participant: when they
    agreed, and the consent text and its hash). The command line enrols participants, prints their links, and
    prints how far each participant has come, including whether they have consented.

Dependencies:
    experiment_plan.py (configuration, assignments and the derived stage sequence). Python standard library only.
    Used by the participant flow routes; tests/test_participant_store.py.

Usage:
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --enrol-all
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --links
    python3 -B src/scripts/participant_store.py --status
"""

import argparse
import datetime
import hashlib
import json
import secrets
import sqlite3
from pathlib import Path

import experiment_plan as ep

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "src" / "data" / "participants" / "participants.db"
TOKEN_BYTES = 24  # 32 URL-safe characters
SCHEMA_VERSION = 2  # 2: the consents table
SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
    participant_id   TEXT PRIMARY KEY,
    token            TEXT NOT NULL UNIQUE,
    condition        TEXT NOT NULL,
    list             TEXT NOT NULL,
    trials           INTEGER NOT NULL,
    simulated        INTEGER NOT NULL,
    config_path      TEXT NOT NULL,
    assignments_path TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    enrolled_at      TEXT NOT NULL,
    completed_at     TEXT
);
CREATE TABLE IF NOT EXISTS progress (
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    stage_key      TEXT NOT NULL,
    stage_index    INTEGER NOT NULL,
    entered_at     TEXT NOT NULL,
    completed_at   TEXT,
    PRIMARY KEY (participant_id, stage_key)
);
CREATE TABLE IF NOT EXISTS consents (
    participant_id TEXT PRIMARY KEY REFERENCES participants(participant_id),
    agreed_at      TEXT NOT NULL,
    consent_sha256 TEXT NOT NULL,
    consent_text   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class StoreError(ValueError):
    """The requested flow transition or participant record is not valid."""


def now() -> str:
    """The current UTC time, to the millisecond, as the timestamps in the database are written."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the participant database and apply the schema.

    Rows come back as sqlite3.Row. Write-ahead logging is enabled so the server can read while another request
    writes. Foreign keys are enforced so a progress row cannot name an unknown participant. Applying the schema
    is idempotent, so an existing database is opened unchanged. Raises StoreError if the file was written by a
    newer schema version than this module knows.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The server opens one connection per request and handles requests on separate threads, so two writes can
    # overlap. Write-ahead logging lets readers run during a write, and the timeout waits instead of failing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row["value"]) > SCHEMA_VERSION:
        raise StoreError(f"{path}: schema version {row['value']} is newer than this module ({SCHEMA_VERSION})")
    elif int(row["value"]) < SCHEMA_VERSION:
        # Every change so far only adds tables, which the CREATE TABLE IF NOT EXISTS script above has just made,
        # so an older database is upgraded by recording the new version. Existing rows are untouched.
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
        conn.commit()
    return conn


def plan_fingerprint(plan: dict) -> str:
    """A short hash of what a participant's stage sequence consists of.

    Covers the ordered stage keys and, for scenario and assessment stages, the trial id, stimulus folder and
    condition: everything whose change would mean a participant is being shown a different experiment. Timestamps
    and anything outside the sequence are not covered.
    """
    material = [[s["key"], s.get("trial_id"), s.get("stimulus"), s.get("condition")] for s in plan["stages"]]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def enrol(conn: sqlite3.Connection, config: ep.Config, participant: str, payload: dict | None = None,
          token: str | None = None) -> dict:
    """Enrol one participant and return their record.

    Derives their stage sequence to store its fingerprint, the condition, the list and the trial count, so a later
    resume can be checked against it. `token` is for tests and reproducible fixtures; by default an opaque token
    is generated. Raises StoreError if the participant is already enrolled, and KeyError if the assignments file
    does not hold them.
    """
    payload = ep.load_assignments(config) if payload is None else payload
    plan = ep.plan_for(config, participant, payload)
    if conn.execute("SELECT 1 FROM participants WHERE participant_id = ?", (participant,)).fetchone():
        raise StoreError(f"{participant} is already enrolled")
    row = {"participant_id": participant, "token": token or secrets.token_urlsafe(TOKEN_BYTES),
           "condition": plan["condition"], "list": plan["list"], "trials": plan["trials"],
           "simulated": int(plan["simulated"]), "config_path": str(config["path"]),
           "assignments_path": str(config["assignments_path"]), "plan_fingerprint": plan_fingerprint(plan),
           "enrolled_at": now(), "completed_at": None}
    conn.execute(
        "INSERT INTO participants (participant_id, token, condition, list, trials, simulated, config_path, "
        "assignments_path, plan_fingerprint, enrolled_at, completed_at) "
        "VALUES (:participant_id, :token, :condition, :list, :trials, :simulated, :config_path, "
        ":assignments_path, :plan_fingerprint, :enrolled_at, :completed_at)", row)
    conn.commit()
    return row


def enrol_all(conn: sqlite3.Connection, config: ep.Config, payload: dict | None = None) -> list[dict]:
    """Enrol every participant of the assignments file that is not enrolled yet; returns the new records.

    Safe to run again after adding participants to the assignments file: those already enrolled keep their token
    and their progress.
    """
    payload = ep.load_assignments(config) if payload is None else payload
    added = []
    for row in payload["assignments"]:
        try:
            added.append(enrol(conn, config, row["participant"], payload))
        except StoreError:
            continue  # already enrolled
    return added


def record(conn: sqlite3.Connection, participant: str) -> dict:
    """One participant's stored record. Raises KeyError if they are not enrolled."""
    row = conn.execute("SELECT * FROM participants WHERE participant_id = ?", (participant,)).fetchone()
    if row is None:
        raise KeyError(participant)
    return dict(row)


def by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    """The record a participant link belongs to, or None for an unknown token (never an error: links are public)."""
    if not token:
        return None
    row = conn.execute("SELECT * FROM participants WHERE token = ?", (token,)).fetchone()
    return dict(row) if row else None


def completed_stages(conn: sqlite3.Connection, participant: str) -> list[str]:
    """The stage keys this participant has completed, in the order they were completed."""
    rows = conn.execute("SELECT stage_key FROM progress WHERE participant_id = ? AND completed_at IS NOT NULL "
                        "ORDER BY stage_index", (participant,)).fetchall()
    return [row["stage_key"] for row in rows]


def record_consent(conn: sqlite3.Connection, participant: str, consent_sha256: str, consent_text: str,
                   commit: bool = True) -> dict:
    """Store that `participant` agreed to the consent text `consent_text` (whose SHA-256 is `consent_sha256`).

    Only the first agreement is kept: a repeated submission does not move the time or replace the text, so the
    record always shows what the participant agreed to before they started. Pass commit=False to make this part
    of a larger transaction (participant_flow.advance commits it together with completing the consent stage, so
    neither can be saved without the other). Returns the stored consent.
    """
    conn.execute("INSERT INTO consents (participant_id, agreed_at, consent_sha256, consent_text) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT (participant_id) DO NOTHING", (participant, now(), consent_sha256, consent_text))
    if commit:
        conn.commit()
    return consent_of(conn, participant)


def consent_of(conn: sqlite3.Connection, participant: str) -> dict | None:
    """The participant's stored consent ({participant_id, agreed_at, consent_sha256, consent_text}), or None."""
    row = conn.execute("SELECT * FROM consents WHERE participant_id = ?", (participant,)).fetchone()
    return dict(row) if row else None


def checked_plan(conn: sqlite3.Connection, config: ep.Config, participant: str,
                  payload: dict | None = None) -> dict:
    """The participant's plan, verified against the fingerprint stored at enrolment.

    Raises StoreError if the derived sequence no longer matches: the configuration or the assignments have changed
    since this participant started, and continuing would mix two different experiments in one record.
    """
    stored = record(conn, participant)
    plan = ep.plan_for(config, participant, payload)
    if plan_fingerprint(plan) != stored["plan_fingerprint"]:
        raise StoreError(f"{participant}: the stage sequence changed since enrolment "
                         f"(enrolled under {stored['config_path']}); their progress belongs to the old sequence")
    return plan


def current_stage(conn: sqlite3.Connection, config: ep.Config, participant: str,
                  payload: dict | None = None) -> dict | None:
    """The stage this participant is to be shown now: the first one they have not completed, or None when done.

    This is the resume point. It is derived from the plan and the stored progress alone, so reopening a link any
    number of times always lands on the same stage.
    """
    plan = checked_plan(conn, config, participant, payload)
    done = set(completed_stages(conn, participant))
    return next((stage for stage in plan["stages"] if stage["key"] not in done), None)


def enter_stage(conn: sqlite3.Connection, config: ep.Config, participant: str,
                payload: dict | None = None) -> dict | None:
    """Record that the participant has arrived at their current stage, and return that stage.

    The arrival time is kept only the first time a stage is entered, so reopening a link does not overwrite when
    the participant first saw it (the reading and response times of a trial are measured from that first arrival).
    Returns None when the participant has finished every stage.
    """
    stage = current_stage(conn, config, participant, payload)
    if stage is None:
        return None
    conn.execute("INSERT INTO progress (participant_id, stage_key, stage_index, entered_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT (participant_id, stage_key) DO NOTHING",
                 (participant, stage["key"], stage["index"], now()))
    conn.commit()
    return stage


def complete_stage(conn: sqlite3.Connection, config: ep.Config, participant: str, stage_key: str,
                   payload: dict | None = None) -> dict | None:
    """Complete `stage_key` and return the next stage (None when the participant has finished).

    Only the participant's current stage may be completed: completing an earlier one again is ignored (so a
    double submission or a reloaded form cannot record a trial twice), and completing a later one raises
    StoreError (so no stage can be skipped). When the last stage is completed, the participant is marked
    complete. Raises StoreError for a stage key that is not in their sequence.
    """
    plan = checked_plan(conn, config, participant, payload)
    keys = [stage["key"] for stage in plan["stages"]]
    if stage_key not in keys:
        raise StoreError(f"{participant}: {stage_key!r} is not one of their stages")
    done = set(completed_stages(conn, participant))
    current = next((stage for stage in plan["stages"] if stage["key"] not in done), None)
    if stage_key in done:
        return current  # already completed: a repeated submission changes nothing
    if current is None or stage_key != current["key"]:
        raise StoreError(f"{participant}: cannot complete {stage_key!r} while at "
                         f"{current['key'] if current else 'the end of the sequence'!r}")
    stamp = now()
    conn.execute("INSERT INTO progress (participant_id, stage_key, stage_index, entered_at, completed_at) "
                 "VALUES (?, ?, ?, ?, ?) ON CONFLICT (participant_id, stage_key) DO UPDATE SET completed_at = ?",
                 (participant, stage_key, current["index"], stamp, stamp, stamp))
    nxt = next((stage for stage in plan["stages"] if stage["key"] not in done | {stage_key}), None)
    if nxt is None:
        conn.execute("UPDATE participants SET completed_at = ? WHERE participant_id = ?", (stamp, participant))
    conn.commit()
    return nxt


def status(conn: sqlite3.Connection, config: ep.Config, participant: str,
           payload: dict | None = None) -> dict:
    """How far one participant has come: their record plus the stage count, completed count and current stage key.

    `plan_changed` is True instead of a stage when the stored fingerprint no longer matches the derived sequence,
    so a researcher can see the problem without the call failing.
    """
    stored = record(conn, participant)
    done = completed_stages(conn, participant)
    out = {"participant": participant, "condition": stored["condition"], "list": stored["list"],
           "simulated": bool(stored["simulated"]), "enrolled_at": stored["enrolled_at"],
           "completed_at": stored["completed_at"], "stages_completed": len(done), "plan_changed": False}
    consent = consent_of(conn, participant)
    out["consented_at"] = consent["agreed_at"] if consent else None
    try:
        plan = checked_plan(conn, config, participant, payload)
    except StoreError:
        out["plan_changed"] = True
        return out
    stage = next((s for s in plan["stages"] if s["key"] not in set(done)), None)
    out["stages"] = len(plan["stages"])
    out["current_stage"] = stage["key"] if stage else None
    return out


def link(token: str, base_url: str = "") -> str:
    """The participant link for a token: `<base_url>/p/<token>` (a relative path when no base url is given)."""
    return f"{base_url.rstrip('/')}/p/{token}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ep.CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--base-url", default="", help="prefix for the printed links, e.g. http://127.0.0.1:8765")
    parser.add_argument("--enrol-all", action="store_true", dest="enrol_all",
                        help="enrol every participant of the assignments file that is not enrolled yet")
    parser.add_argument("--links", action="store_true", help="print every enrolled participant's link")
    parser.add_argument("--status", action="store_true", help="print how far each enrolled participant has come")
    args = parser.parse_args()
    config = ep.load_config(args.config)
    payload = ep.load_assignments(config)
    conn = connect(args.db)
    if args.enrol_all:
        added = enrol_all(conn, config, payload)
        print(f"enrolled {len(added)} new participant(s) in {args.db}")
    rows = conn.execute("SELECT participant_id, token FROM participants ORDER BY participant_id").fetchall()
    if args.links:
        for row in rows:
            print(f"{row['participant_id']}\t{link(row['token'], args.base_url)}")
    if args.status:
        for row in rows:
            print(json.dumps(status(conn, config, row["participant_id"], payload)))
    if not (args.enrol_all or args.links or args.status):
        parser.error("choose --enrol-all, --links or --status")


if __name__ == "__main__":
    main()
