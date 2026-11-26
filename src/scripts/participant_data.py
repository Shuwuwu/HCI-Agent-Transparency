"""
Purpose:
    Researcher access to the participant database (participant_store.py): everything stored about one
    participant, analysis-ready CSV exports derived from the database, and a consistency check of the database.
    Nothing here is reachable from the participant server; it reads the database file directly.

    The database stays the single source of truth: exports are rebuilt from it on every run and are never edited
    or read back. CSV files never contain link tokens or the consent text (only its hash); `show` prints the
    consent text only when asked.

Inputs:
    An experiment configuration (--config, default the study configuration), which names the database and its
    environment; or --db PATH for another database file of that environment (a backup copy, for instance).

Outputs:
    show ID     JSON on stdout: the participant's record (no token), stage sequence, progress, consent, answers,
                questionnaire and interaction events.
    export      CSV files in data/participants/exports/<environment>/ (or <...>/<ID>/ with --participant, or
                --out DIR), each with a header row, plus export_manifest.json (source database, schema version,
                environment, time, row count and SHA-256 of every file). Files are private (0600):
                  participants.csv    one row per participant (view participant_overview)
                  trials.csv          one row per trial, practice included and marked by trial_type (view
                                      trial_summary plus the interaction measures of interaction_log.summarize;
                                      dict-valued measures as JSON)
                  progress.csv        one row per stage entered, with its kind, trial and trial_type
                  events.csv          one row per interaction event, with trial_type
                  questionnaire.csv   one row per participant and item
    check       the problems found (see find_problems), one per line; exit status 1 if there are any.

Dependencies:
    participant_store.py (connection, views), experiment_plan.py (configuration), interaction_log.py
    (per-trial measures). Python standard library only.

Usage:
    python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json show P001
    python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json export
    python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json export --participant P001
    python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json check
    (Without --config the study configuration is used.)
"""

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import experiment_plan as ep
import interaction_log as ilog
import participant_store as store

EXPORTS_DIR = store.PARTICIPANT_DATA_DIR / "exports"


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    """The rows of a query as dicts."""
    return [dict(row) for row in conn.execute(sql, params)]


def participant_data(conn: sqlite3.Connection, participant: str, consent_text: bool = False) -> dict:
    """Everything stored about one participant, keyed by table. Raises KeyError if they are not enrolled.

    The link token is left out; the consent text only with consent_text=True (its hash is always there).
    Responses carry their source lists decoded.
    """
    record = store.record(conn, participant)
    record.pop("token")
    consent = store.consent_of(conn, participant)
    if consent and not consent_text:
        consent.pop("consent_text")
    responses = [store.response_of(conn, participant, row["stage_key"]) for row in conn.execute(
        "SELECT stage_key FROM responses r JOIN plan_stages s USING (participant_id, stage_key) "
        "WHERE participant_id = ? ORDER BY s.stage_index", (participant,))]
    where = "WHERE participant_id = ?"
    return {
        "participant": record,
        "overview": _rows(conn, f"SELECT * FROM participant_overview {where}", (participant,))[0],
        "plan_stages": store.stored_plan_stages(conn, participant),
        "progress": _rows(conn, f"SELECT * FROM progress {where} ORDER BY stage_index", (participant,)),
        "consent": consent,
        "responses": responses,
        "questionnaire": _rows(conn, f"SELECT item_id, answer, questionnaire_sha256, submitted_at "
                                     f"FROM questionnaire_responses {where} ORDER BY rowid", (participant,)),
        "trials": _rows(conn, f"SELECT * FROM trial_summary {where} ORDER BY trial_ordinal", (participant,)),
        "events": _rows(conn, f"SELECT * FROM events {where} ORDER BY event_id", (participant,)),
    }


def _table(conn: sqlite3.Connection, sql: str, params=()) -> tuple[list[str], list[dict]]:
    """(column names, rows) of a query; the names come from the query, so an empty result still has them."""
    cursor = conn.execute(sql, params)
    return [c[0] for c in cursor.description], [dict(row) for row in cursor]


def _trial_table(conn: sqlite3.Connection, where: str, params) -> tuple[list[str], list[dict]]:
    """trial_summary with the interaction measures of each trial (interaction_log.summarize) appended;
    dict-valued measures are written as JSON."""
    columns, rows = _table(conn, f"SELECT * FROM trial_summary {where} ORDER BY participant_id, trial_ordinal",
                           params)
    extra = [k for k in ilog.summarize([]) if k not in ("scenario_visible_ms", "stage_loads")]  # already columns
    for row in rows:
        measures = ilog.summarize(store.trial_events(conn, row["participant_id"], row["trial_id"]))
        row.update({k: json.dumps(measures[k]) if isinstance(measures[k], dict) else measures[k] for k in extra})
    return columns + extra, rows


def export_tables(conn: sqlite3.Connection, participant: str | None = None) -> dict:
    """{file name: (columns, rows)} for every export, read in one transaction so the files agree."""
    where, params = ("WHERE participant_id = ?", (participant,)) if participant else ("", ())
    trial_type = "CASE practice WHEN 1 THEN 'practice' ELSE 'experimental' END AS trial_type"
    conn.execute("BEGIN")  # one snapshot of the database for all files
    try:
        return {
            "participants.csv": _table(conn, f"SELECT * FROM participant_overview {where} ORDER BY participant_id",
                                       params),
            "trials.csv": _trial_table(conn, where, params),
            "progress.csv": _table(conn, "SELECT g.participant_id, g.stage_index, g.stage_key, s.kind, s.trial_id, "
                                         "CASE s.practice WHEN 1 THEN 'practice' ELSE 'experimental' END AS "
                                         "trial_type, g.entered_at, g.completed_at FROM progress g JOIN plan_stages "
                                         "s USING (participant_id, stage_key) "
                                         f"{where.replace('participant_id', 'g.participant_id')} "
                                         "ORDER BY g.participant_id, g.stage_index", params),
            "events.csv": _table(conn, f"SELECT event_id, participant_id, trial_id, stage_key, {trial_type}, seq, "
                                       "type, origin, tab, item_id, skill_id, card_id, duration_ms, client_ms, "
                                       f"server_time FROM events {where} ORDER BY participant_id, event_id", params),
            "questionnaire.csv": _table(conn, "SELECT participant_id, item_id, answer, questionnaire_sha256, "
                                              f"submitted_at FROM questionnaire_responses {where} "
                                              "ORDER BY participant_id, rowid", params),
        }
    finally:
        conn.rollback()


def write_export(conn: sqlite3.Connection, out_dir: Path, source: Path, participant: str | None = None) -> dict:
    """Write the CSV files and export_manifest.json into `out_dir` (created private); returns the manifest.

    Each file is written to a temporary name and renamed into place, so a reader never sees half a file. Files
    of an earlier export in the same folder are replaced. Raises KeyError for a participant who is not enrolled.
    """
    if participant is not None:
        store.record(conn, participant)
    store.private_dir(out_dir)
    files = {}
    for name, (columns, rows) in export_tables(conn, participant).items():
        path = out_dir / name
        temporary = out_dir / f".{name}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            os.chmod(temporary, 0o600)
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        temporary.replace(path)
        files[name] = {"rows": len(rows), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {
        "source": (str(source.resolve().relative_to(ep.ROOT)) if source.resolve().is_relative_to(ep.ROOT)
                   else str(source)),
        "environment": store.environment_of(conn),
        "schema_version": int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]),
        "participant": participant,
        "exported_at": store.now(),
        "files": files,
    }
    manifest_path = out_dir / "export_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


def find_problems(conn: sqlite3.Connection) -> list[str]:
    """Everything inconsistent in a participant database, as readable sentences; [] when it is consistent.

    Checks: SQLite's integrity and foreign keys; an environment stamp; per participant, a stage-sequence snapshot
    matching their fingerprint, progress only for stages of that sequence and completed in order, completed_at set
    exactly when every stage is completed, a consent exactly when consent is completed, a response exactly for
    each completed assessment (naming the trial, stimulus and condition of the plan), questionnaire answers
    exactly when the questionnaire is completed; per event, a trial and stage of the participant's sequence with
    the stage's practice flag, a contiguous `seq` per trial, at least one session_start per scenario entered and
    at most one written by this server per trial. Events imported from the retired storage (origin "legacy") may
    repeat a session_start (they include attempts before a reset) and need not have an entered stage.
    """
    problems = []
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        problems.append("SQLite integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append("rows refer to participants that do not exist (foreign_key_check)")
    if store.environment_of(conn) is None:
        problems.append("the database has no environment stamp")
    for participant in [r[0] for r in conn.execute("SELECT participant_id FROM participants ORDER BY 1")]:
        p = lambda text: problems.append(f"{participant}: {text}")  # noqa: E731
        stages = store.stored_plan_stages(conn, participant)
        if not stages:
            p("no stage-sequence snapshot (plan_stages)")
            continue
        if store.stored_plan_fingerprint(conn, participant) != store.record(conn, participant)["plan_fingerprint"]:
            p("plan_stages do not match the fingerprint they were enrolled under")
        by_key = {s["stage_key"]: s for s in stages}
        progress = _rows(conn, "SELECT * FROM progress WHERE participant_id = ?", (participant,))
        for row in progress:
            if row["stage_key"] not in by_key:
                p(f"progress for {row['stage_key']}, which is not in their sequence")
            elif row["stage_index"] != by_key[row["stage_key"]]["stage_index"]:
                p(f"progress for {row['stage_key']} has the wrong stage index")
        completed = {r["stage_key"] for r in progress if r["completed_at"]}
        entered = {r["stage_key"] for r in progress}
        order = [s["stage_key"] for s in stages]
        if [k for k in order if k in completed] != order[:len(completed)]:
            p("completed stages are not the first stages of their sequence")
        done = len(completed) == len(order)
        if done != bool(store.record(conn, participant)["completed_at"]):
            p("completed_at does not agree with their progress")
        if ("consent" in completed) != (store.consent_of(conn, participant) is not None):
            p("consent record and consent stage disagree")
        for stage in (s for s in stages if s["kind"] == "assessment"):
            response = store.response_of(conn, participant, stage["stage_key"])
            if (stage["stage_key"] in completed) != (response is not None):
                p(f"{stage['stage_key']}: response and completed stage disagree")
            elif response and (response["trial_id"], response["stimulus"], response["condition"],
                               response["practice"]) != (stage["trial_id"], stage["stimulus"], stage["condition"],
                                                         stage["practice"]):
                p(f"{stage['stage_key']}: response names another trial, stimulus or condition than the plan")
        questionnaire = [s for s in stages if s["kind"] == "questionnaire"]
        if questionnaire and (questionnaire[0]["stage_key"] in completed) != bool(store.questionnaire_of(conn, participant)):
            p("questionnaire answers and questionnaire stage disagree")
        trials = {s["trial_id"] for s in stages if s["trial_id"]}
        for event in _rows(conn, "SELECT * FROM events WHERE participant_id = ?", (participant,)):
            stage = by_key.get(event["stage_key"])
            if event["trial_id"] is not None and event["trial_id"] not in trials:
                p(f"event {event['event_id']} names trial {event['trial_id']}, which is not theirs")
            elif stage is None:
                p(f"event {event['event_id']} names stage {event['stage_key']}, which is not in their sequence")
            elif stage["trial_id"] != event["trial_id"] or stage["practice"] != event["practice"]:
                p(f"event {event['event_id']} disagrees with its stage on the trial or the practice flag")
            elif event["origin"] != "legacy" and event["stage_key"] not in entered:
                p(f"event {event['event_id']} belongs to a stage they never entered")
        for trial, seqs in _group(conn, "SELECT trial_id, seq FROM events WHERE participant_id = ? "
                                        "ORDER BY trial_id, seq", participant).items():
            if seqs != list(range(len(seqs))):
                p(f"events of {trial or 'no trial'} are not numbered 0..{len(seqs) - 1}")
        for stage in (s for s in stages if s["kind"] == "scenario" and s["stage_key"] in entered):
            starts = conn.execute("SELECT origin FROM events WHERE participant_id = ? AND trial_id = ? "
                                  "AND type = 'session_start'", (participant, stage["trial_id"])).fetchall()
            if not starts:
                p(f"{stage['trial_id']}: entered but has no session_start")
            if sum(r[0] == "server" for r in starts) > 1:
                p(f"{stage['trial_id']}: more than one session_start")
    return problems


def _group(conn: sqlite3.Connection, sql: str, participant: str) -> dict:
    """{trial id: [seq, ...]} from a query returning (trial_id, seq) rows in order."""
    out = {}
    for trial, seq in conn.execute(sql, (participant,)):
        out.setdefault(trial, []).append(seq)
    return out


def open_database(config: ep.Config, db: Path | None) -> tuple[sqlite3.Connection, Path]:
    """Open the configuration's database (or `db`) for its environment; exits with a message if it is missing."""
    path = Path(db or config["database_path"])
    if not path.is_file():
        sys.exit(f"{path} does not exist")
    return store.connect(path, config["environment"]), path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ep.CONFIG_PATH,
                        help="experiment configuration naming the database (default: the study one)")
    parser.add_argument("--db", type=Path, default=None, help="another database file of that environment")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print everything stored about one participant as JSON")
    show.add_argument("participant")
    show.add_argument("--consent-text", action="store_true", dest="consent_text",
                      help="include the consent text the participant agreed to")
    export = commands.add_parser("export", help="write the analysis CSV files")
    export.add_argument("--participant", help="only this participant (written to a sub-folder named after them)")
    export.add_argument("--out", type=Path, default=None, help="folder to write to instead of the default")
    commands.add_parser("check", help="report inconsistencies; exit status 1 if there are any")
    args = parser.parse_args()
    config = ep.load_config(args.config)
    conn, path = open_database(config, args.db)
    if args.command == "show":
        try:
            print(json.dumps(participant_data(conn, args.participant, args.consent_text), indent=2))
        except KeyError:
            sys.exit(f"{args.participant} is not enrolled in {path}")
    elif args.command == "export":
        out = args.out or EXPORTS_DIR / config["environment"]
        if args.participant and not args.out:
            out = out / args.participant
        try:
            manifest = write_export(conn, out, path, args.participant)
        except KeyError:
            sys.exit(f"{args.participant} is not enrolled in {path}")
        print(f"wrote {out}:")
        for name, info in manifest["files"].items():
            print(f"  {name}: {info['rows']} rows")
    elif args.command == "check":
        problems = find_problems(conn)
        for problem in problems:
            print(problem)
        print(f"{len(problems)} problem(s) in {path}")
        sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
