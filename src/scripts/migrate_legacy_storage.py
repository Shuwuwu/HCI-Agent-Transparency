"""
Purpose:
    Move the participant data of the retired storage into the development database, once, without losing or
    changing anything. Before this change the participant records lived in src/data/participants/participants.db
    (schema version 4: participants, progress, consents, responses) and the interaction events in JSON-lines files
    under src/data/interaction_logs/ (one file per trial, practice trials in practice/, plus files of researcher
    previews named by a random id). The participant database is now the single source of truth, in
    data/participants/<environment>.sqlite3 (participant_store.py); this script copies the old records into it.

    Only development (simulated) participants exist in the old storage, so the target is the development
    database. The script refuses a legacy participant who is not simulated.

    Steps, all verified:
      1. Archive both legacy folders, byte for byte, into one tar.gz under data/participants/backups/legacy/, with
         a .sha256 beside it, and check that every archived file reads back identical to its source. The legacy
         folders themselves are left in place: deleting them is a separate, explicitly approved step.
      2. Create the target database (refused if it already exists) and copy every row of the four legacy tables.
      3. Store each participant's stage sequence (plan_stages), derived from --config and required to match the
         fingerprint they were enrolled under.
      4. Import the events of every participant trial file (top level and practice/) in order of their original
         server time, with origin "legacy" and their original server time and browser clock. A legacy
         session_start's stimulus and condition must equal the trial's plan_stages row; they are then dropped (the
         row holds them). Researcher-preview files (random ids) are not participant data: they are archived but
         not imported.
      5. Verify: the row count of every legacy table and the event count of every trial file reappear in the
         target, every snapshot matches its fingerprint, and SQLite's integrity and foreign-key checks pass.

    Known property of the legacy events: a participant reset with --reset kept their old log files, so events of
    an attempt before a reset are imported together with the later attempt's (origin "legacy" marks all of them).

Inputs:
    --legacy-db (default src/data/participants/participants.db), --legacy-logs (default src/data/interaction_logs),
    --config (default the development configuration: assignments and the target database), --target (default the
    configuration's database), --archive-dir (default data/participants/backups/legacy).

Outputs:
    Without --apply (the default): nothing is written in the repository; the migration is built in a temporary
    database outside it and the verification report is printed. With --apply: the archive and its .sha256, the
    target database, and the same report (JSON on stdout). The target's meta table records the import
    (`legacy_import`: archive name and hash, legacy database hash, time).

Dependencies:
    participant_store.py (schema, snapshot_plan, record_event), experiment_plan.py (configuration, plans),
    interaction_log.py (event validation). Python standard library only.

Usage:
    python3 -B src/scripts/migrate_legacy_storage.py            # dry run: build in a temporary file, verify, report
    python3 -B src/scripts/migrate_legacy_storage.py --apply    # archive, write data/participants/development.sqlite3
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

import experiment_plan as ep
import interaction_log as ilog
import participant_store as store

ROOT = ep.ROOT
LEGACY_DB = ROOT / "src" / "data" / "participants" / "participants.db"
LEGACY_LOGS = ROOT / "src" / "data" / "interaction_logs"
ARCHIVE_DIR = store.PARTICIPANT_DATA_DIR / "backups" / "legacy"
LEGACY_TABLES = ("participants", "progress", "consents", "responses")
PARTICIPANT_TRIAL_RE = re.compile(r"^(?P<participant>[A-Za-z0-9]+)-[pt]\d{2}$")  # experiment_plan.trial_id
LEGACY_ONLY_FIELDS = ("session", "seq", "server_time", "stimulus", "condition", "pid")


class MigrationError(RuntimeError):
    """The legacy data cannot be migrated without loss or inconsistency; nothing is left half-written."""


def sha256_of(path: Path) -> str:
    """SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(legacy_db: Path, legacy_logs: Path) -> dict:
    """What the legacy storage holds: row counts per table, and the event files split into participant trials
    ({trial id: [files]}, top level and practice/) and other files (researcher previews)."""
    if not legacy_db.is_file():
        raise MigrationError(f"{legacy_db} does not exist")
    with contextlib.closing(sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)) as conn:
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in LEGACY_TABLES}
    trials, other = {}, []
    files = sorted(legacy_logs.glob("*.jsonl")) + sorted((legacy_logs / "practice").glob("*.jsonl"))
    for path in files:
        if PARTICIPANT_TRIAL_RE.match(path.stem):
            trials.setdefault(path.stem, []).append(path)
        else:
            other.append(path)
    lines = {trial: sum(len(read_jsonl(p)) for p in paths) for trial, paths in trials.items()}
    return {"schema_version": int(version[0]) if version else None, "rows": counts, "trial_files": trials,
            "trial_events": lines, "other_files": other}


def read_jsonl(path: Path) -> list[dict]:
    """The records of one legacy event file, in file order."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def archive(legacy_db: Path, legacy_logs: Path, archive_dir: Path, stamp: str) -> dict:
    """Write every legacy file into one tar.gz and verify that it reads back identical.

    Returns {"archive", "sha256", "files"}. Raises MigrationError if a member differs from its source.
    """
    store.private_dir(archive_dir)
    target = archive_dir / f"legacy-storage-{stamp}.tar.gz"
    if target.exists():
        raise MigrationError(f"{target} already exists")
    sources = [legacy_db] + [Path(f"{legacy_db}{s}") for s in store.SIDECARS if Path(f"{legacy_db}{s}").exists()]
    sources += sorted(p for p in legacy_logs.rglob("*") if p.is_file())
    # Member names are relative to the folder holding both legacy locations (src/data/ for the real storage).
    base = Path(os.path.commonpath([legacy_db.parent.resolve(), legacy_logs.resolve()])).parent
    expected = {str(p.resolve().relative_to(base)): sha256_of(p) for p in sources}
    with tarfile.open(target, "w:gz") as tar:
        for path in sources:
            tar.add(path, arcname=str(path.resolve().relative_to(base)), recursive=False)
    with tarfile.open(target, "r:gz") as tar:
        found = {m.name: hashlib.sha256(tar.extractfile(m).read()).hexdigest() for m in tar.getmembers() if m.isfile()}
    if found != expected:
        raise MigrationError(f"{target} does not read back identical to its sources")
    digest = sha256_of(target)
    Path(f"{target}.sha256").write_text(f"{digest}  {target.name}\n", encoding="utf-8")
    for path in (target, Path(f"{target}.sha256")):
        path.chmod(0o600)
    return {"archive": str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target),
            "sha256": digest, "files": len(sources)}


def copy_tables(legacy_db: Path, conn: sqlite3.Connection) -> None:
    """Copy every row of the four legacy tables into `conn` (not committed). Refuses a study participant."""
    with contextlib.closing(sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)) as legacy:
        legacy.row_factory = sqlite3.Row
        if legacy.execute("SELECT COUNT(*) FROM participants WHERE simulated = 0").fetchone()[0]:
            raise MigrationError("the legacy database holds study participants; this migration is for "
                                 "development data only")
        for table in LEGACY_TABLES:
            rows = legacy.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            columns = rows[0].keys()
            conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                             [tuple(row) for row in rows])


def snapshot_plans(conn: sqlite3.Connection, config: ep.Config, payload: dict) -> dict:
    """Store every participant's plan_stages (not committed); returns {participant: plan}. Raises MigrationError
    when a derived plan does not match the fingerprint the participant was enrolled under."""
    plans = {}
    for row in conn.execute("SELECT participant_id, plan_fingerprint FROM participants").fetchall():
        plan = ep.plan_for(config, row["participant_id"], payload)
        if store.plan_fingerprint(plan) != row["plan_fingerprint"]:
            raise MigrationError(f"{row['participant_id']}: {config['path']} no longer derives the stage sequence "
                                 "they were enrolled under")
        store.snapshot_plan(conn, row["participant_id"], plan)
        plans[row["participant_id"]] = plan
    return plans


def import_events(conn: sqlite3.Connection, trials: dict, plans: dict) -> dict:
    """Import the events of every participant trial file; returns {trial id: events imported}.

    Each trial's files are merged and ordered by their original server time (file order breaks ties). Raises
    MigrationError for an event of an unknown participant or trial, an event the current vocabulary refuses, or a
    session_start whose stimulus or condition differs from the trial's plan.
    """
    counts = {}
    for trial_id, paths in sorted(trials.items()):
        participant = PARTICIPANT_TRIAL_RE.match(trial_id)["participant"]
        if participant not in plans:
            raise MigrationError(f"{trial_id}: {participant} is not enrolled in the legacy database")
        stage = next((s for s in plans[participant]["stages"] if s.get("trial_id") == trial_id
                      and s["kind"] == "scenario"), None)
        if stage is None:
            raise MigrationError(f"{trial_id} is not a trial of {participant}")
        records = [r for path in paths for r in read_jsonl(path)]
        records.sort(key=lambda r: r["server_time"])  # stable: file order breaks ties
        for record in records:
            if record.get("session") != trial_id:
                raise MigrationError(f"{trial_id}: holds an event of session {record.get('session')!r}")
            if record["type"] == "session_start" and (record.get("stimulus"), record.get("condition")) != (
                    stage["stimulus"], stage["condition"]):
                raise MigrationError(f"{trial_id}: session_start shows {record.get('stimulus')}/"
                                     f"{record.get('condition')}, the plan {stage['stimulus']}/{stage['condition']}")
            event = {k: v for k, v in record.items() if k not in LEGACY_ONLY_FIELDS}
            try:
                clean = ilog.validate_event({"trial_id": trial_id, **event}, "server")
            except ilog.LogError as exc:
                raise MigrationError(f"{trial_id}: event {record.get('seq')} ({record['type']}): {exc}") from exc
            store.record_event(conn, participant, stage["key"], trial_id, bool(stage["practice"]), clean, "legacy",
                               server_time=record["server_time"])
        counts[trial_id] = len(records)
    return counts


def verify(conn: sqlite3.Connection, found: dict, plans: dict) -> list[str]:
    """Problems found comparing the target with the legacy inventory; an empty list means a complete migration."""
    problems = []
    for table, expected in found["rows"].items():
        got = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if got != expected:
            problems.append(f"{table}: {got} rows, legacy has {expected}")
    for trial_id, expected in found["trial_events"].items():
        participant = PARTICIPANT_TRIAL_RE.match(trial_id)["participant"]
        got = conn.execute("SELECT COUNT(*) FROM events WHERE participant_id = ? AND trial_id = ? AND origin = "
                           "'legacy'", (participant, trial_id)).fetchone()[0]
        if got != expected:
            problems.append(f"{trial_id}: {got} events, legacy files hold {expected}")
    for participant, plan in plans.items():
        if store.stored_plan_fingerprint(conn, participant) != store.plan_fingerprint(plan):
            problems.append(f"{participant}: plan_stages do not match the fingerprint")
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        problems.append("integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append("foreign_key_check found orphan rows")
    return problems


def migrate(legacy_db: Path, legacy_logs: Path, config: ep.Config, target: Path, archive_dir: Path | None,
            apply: bool) -> dict:
    """Run the migration (see the module docstring) and return its report.

    With apply=False the target is a temporary file outside the repository and no archive is written. Raises
    MigrationError on any problem; with apply=True a target that fails verification is removed again, so either
    the complete, verified database exists or none does.
    """
    found = inventory(legacy_db, legacy_logs)
    if found["schema_version"] != 4:
        raise MigrationError(f"{legacy_db}: schema version {found['schema_version']}, expected 4")
    payload = ep.load_assignments(config)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"apply": apply, "legacy_db": str(legacy_db), "legacy_db_sha256": sha256_of(legacy_db),
              "legacy_rows": found["rows"], "trial_files": sum(len(p) for p in found["trial_files"].values()),
              "trial_events": sum(found["trial_events"].values()),
              "not_imported": {"files": len(found["other_files"]),
                               "reason": "researcher-preview sessions (random ids), archived only"}}
    scratch = None
    if apply:
        if target.exists():
            raise MigrationError(f"{target} already exists; the migration runs once, into a new database")
        report["archive"] = archive(legacy_db, legacy_logs, archive_dir, stamp)
    else:
        scratch = tempfile.TemporaryDirectory()
        target = Path(scratch.name) / "dry-run.sqlite3"
    conn = store.connect(target, "development")
    try:
        conn.execute("BEGIN")
        copy_tables(legacy_db, conn)
        plans = snapshot_plans(conn, config, payload)
        imported = import_events(conn, found["trial_files"], plans)
        conn.execute("INSERT INTO meta (key, value) VALUES ('legacy_import', ?)", (json.dumps({
            "archive": report.get("archive", {}).get("archive"), "archive_sha256": report.get("archive", {}).get("sha256"),
            "legacy_db_sha256": report["legacy_db_sha256"], "imported_at": store.now()}),))
        conn.commit()
        problems = verify(conn, found, plans)
        report.update({"target": str(target) if apply else "(temporary, removed)", "events_imported": sum(imported.values()),
                       "plan_stages": conn.execute("SELECT COUNT(*) FROM plan_stages").fetchone()[0],
                       "problems": problems})
        if problems:
            raise MigrationError("verification failed: " + "; ".join(problems))
    except Exception:
        conn.close()
        if apply:
            for path in [target] + [Path(f"{target}{s}") for s in store.SIDECARS]:
                path.unlink(missing_ok=True)
        raise
    finally:
        if scratch is not None:
            conn.close()
            scratch.cleanup()
    conn.close()
    store.make_private(target)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--legacy-db", type=Path, default=LEGACY_DB, dest="legacy_db")
    parser.add_argument("--legacy-logs", type=Path, default=LEGACY_LOGS, dest="legacy_logs")
    parser.add_argument("--config", type=Path, default=ep.DEVELOPMENT_CONFIG)
    parser.add_argument("--target", type=Path, default=None, help="default: the configuration's database")
    parser.add_argument("--archive-dir", type=Path, default=ARCHIVE_DIR, dest="archive_dir")
    parser.add_argument("--apply", action="store_true", help="archive and write the target (default: dry run)")
    args = parser.parse_args()
    config = ep.load_config(args.config)
    if not config["simulated"]:
        parser.error("the legacy data is development data: use a simulated (development) configuration")
    try:
        report = migrate(args.legacy_db, args.legacy_logs, config, args.target or config["database_path"],
                         args.archive_dir, args.apply)
    except MigrationError as exc:
        sys.exit(f"migration refused: {exc}")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
