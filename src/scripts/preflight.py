"""
Purpose:
    A readiness check to run before every session (and before pilot testing): is everything a participant will
    meet in place, and is their data going to be recorded, private and backed up? It checks the configuration,
    the assignments and every stimulus they need, the study materials, the participant database, the privacy of
    the data folder, the backups, and that nothing else (an old server) already holds the port.

    Read-only: it never creates, changes or upgrades a database, and writes no file. Each finding is PASS, WARN
    (works, but not ready for real participants, e.g. a placeholder or a rehearsal configuration) or FAIL (a
    participant session would break or their data would be at risk).

Inputs:
    --config (default the study configuration) and optionally --db (another database of that environment) and
    --port (default 8765, the server's default). Reads the configuration's assignments, corpus, stimuli, the
    study materials in src/study_materials/, src/participant_ui/flow.js (placeholder screens), the database
    file, data/participants/ and its backups, and asks git whether the database is ignored.

Outputs:
    One line per check on stdout ("PASS|WARN|FAIL  <check>: <detail>") and a summary; exit status 1 if any check
    fails, 0 otherwise.

Dependencies:
    experiment_plan.py (configuration, plans, servable stimuli), participant_flow.py (consent, assessment and
    questionnaire loaders), participant_server.py (practice presentation check), participant_store.py (schema,
    environment), participant_data.py (consistency check, backup verification). Python standard library and git.

Usage:
    python3 -B src/scripts/preflight.py --config src/data/experiment_configs/study.json
    python3 -B src/scripts/preflight.py --config src/data/experiment_configs/rehearsal.json --port 8765
"""

import argparse
import contextlib
import datetime
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import experiment_plan as ep
import participant_data as pdata
import participant_flow as flow
import participant_server as ps
import participant_store as store

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MIN_PYTHON = (3, 10)
BACKUP_MAX_AGE = datetime.timedelta(hours=24)
MIN_FREE_BYTES = 1 << 30
# A bracketed phrase that is neither the agreement checkbox ("[ ]") nor a Markdown link ("[text](url)").
CONSENT_PLACEHOLDER_RE = re.compile(r"\[(?! \])([^\]]+)\](?!\()")
COMPLETION_PLACEHOLDER = "The debriefing text and any compensation details go here."


class Report:
    """Collects (status, check, detail) findings in order."""

    def __init__(self):
        self.findings = []

    def add(self, status: str, check: str, detail: str) -> None:
        self.findings.append((status, check, detail))

    @property
    def failed(self) -> bool:
        return any(status == FAIL for status, _, _ in self.findings)


def check_environment(report: Report, data_dir: Path) -> None:
    """Python version and free disk space where the data is written."""
    version = sys.version_info[:2]
    report.add(PASS if version >= MIN_PYTHON else FAIL, "python",
               f"{sys.version.split()[0]} (needs {'.'.join(map(str, MIN_PYTHON))} or newer)")
    existing = next(p for p in [data_dir, *data_dir.parents] if p.exists())
    free = shutil.disk_usage(existing).free
    report.add(PASS if free >= MIN_FREE_BYTES else WARN, "disk space", f"{free / 2**30:.1f} GiB free")


def check_design(report: Report, config: ep.Config) -> dict | None:
    """The configuration, its assignments, and that every stimulus of every participant can be served.
    Returns the assignments payload, or None when they cannot be loaded."""
    kind = ("rehearsal (planned unsafe trials shown as their safe scenario; never study data)"
            if config.get("rehearsal") else "development (simulated)" if config["simulated"] else "study")
    report.add(PASS if not config["simulated"] else WARN, "configuration", f"{config['path'].name}: {kind}")
    try:
        payload = ep.load_assignments(config)
    except (ep.ConfigError, OSError, ValueError) as exc:
        report.add(FAIL, "assignments", str(exc))
        return None
    problems = ep.design_problems(config, payload)
    report.add(FAIL if problems else PASS, "assignments",
               "; ".join(problems) if problems else f"{len(payload['assignments'])} participants, "
               f"{config['trials_per_participant']} trials each")
    missing = sorted({folder for row in payload["assignments"]
                      for folder in ep.unresolved_stimuli(config, ep.plan_for(config, row["participant"], payload))})
    report.add(FAIL if missing else PASS, "stimuli",
               f"cannot be served: {', '.join(missing)}" if missing else "every trial of every participant is servable")
    try:
        ps.use_corpus(config["corpus_path"])
        ps.check_practice_presentation(config)
        report.add(PASS, "practice", f"{config['practice']['stimulus']}, hidden steps "
                   f"{config['practice'].get('omit_steps', []) or 'none'}")
    except (ValueError, FileNotFoundError) as exc:
        report.add(FAIL, "practice", str(exc))
    return payload


def check_materials(report: Report) -> None:
    """Consent, assessment and questionnaire load; placeholders still in them, or on the completion screen."""
    for name, loader in (("consent", flow.load_consent), ("assessment", flow.load_assessment),
                         ("questionnaire", flow.load_questionnaire)):
        try:
            material = loader()
        except (OSError, ValueError) as exc:
            report.add(FAIL, name, str(exc))
            continue
        if name == "consent":
            left = CONSENT_PLACEHOLDER_RE.findall(material["markdown"])
            report.add(WARN if left else PASS, name, f"placeholders to fill: {left}" if left else "complete")
        elif name == "questionnaire" and material.get("placeholder"):
            report.add(WARN, name, "still the placeholder questionnaire")
        else:
            report.add(PASS, name, f"{material.get('version', 'loaded')}")
    flow_js = (ps.UI_DIR / "flow.js").read_text(encoding="utf-8")
    report.add(WARN if COMPLETION_PLACEHOLDER in flow_js else PASS, "completion screen",
               "debriefing and compensation text are still a placeholder" if COMPLETION_PLACEHOLDER in flow_js
               else "no placeholder")


def check_database(report: Report, config: ep.Config, db_path: Path, payload: dict | None) -> None:
    """The database exists, belongs to this environment, is consistent, and holds the assigned participants.
    Opened read-only, so nothing is created, stamped or upgraded."""
    if not db_path.is_file():
        report.add(WARN, "database", f"{db_path} does not exist yet: enrol participants first "
                   "(participant_store.py --enrol-all)")
        return
    with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            version = int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
            environment = store.environment_of(conn)
        except (sqlite3.DatabaseError, TypeError) as exc:
            report.add(FAIL, "database", f"{db_path} is not a participant database: {exc}")
            return
        if environment != config["environment"]:
            report.add(FAIL, "database", f"{db_path} is stamped {environment!r}, the configuration is "
                       f"{config['environment']!r}")
            return
        report.add(PASS if version == store.SCHEMA_VERSION else WARN, "database",
                   f"{db_path.name}, {environment}, schema {version}"
                   + ("" if version == store.SCHEMA_VERSION else
                      f" (upgraded to {store.SCHEMA_VERSION} when the server next opens it)"))
        problems = pdata.find_problems(conn)
        report.add(FAIL if problems else PASS, "records",
                   f"{len(problems)} problem(s), first: {problems[0]}" if problems else "consistent")
        enrolled = {r[0] for r in conn.execute("SELECT participant_id FROM participants")}
    if payload is not None:
        assigned = {row["participant"] for row in payload["assignments"]}
        not_enrolled = sorted(assigned - enrolled)
        report.add(WARN if not_enrolled else PASS, "enrolment",
                   f"{len(enrolled)} enrolled; not yet enrolled: {', '.join(not_enrolled)}" if not_enrolled
                   else f"all {len(assigned)} assigned participants enrolled")


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def check_privacy(report: Report, db_path: Path) -> None:
    """The data folder and database files are private, ignored by git, and no participant data is tracked."""
    folder = store.PARTICIPANT_DATA_DIR
    loose = []
    if folder.exists():
        for path in [folder, *folder.rglob("*")]:
            if path.name == "README.md":
                continue
            if _mode(path) & 0o077:
                loose.append(f"{path.relative_to(ep.ROOT)} ({oct(_mode(path))})")
    for path in [db_path] + [Path(f"{db_path}{s}") for s in store.SIDECARS]:
        if path.exists() and _mode(path) & 0o077 and str(path) not in " ".join(loose):
            loose.append(f"{path} ({oct(_mode(path))})")
    report.add(FAIL if loose else PASS, "permissions",
               f"readable by others: {', '.join(loose[:5])}" if loose else "participant data readable by its owner only")
    try:
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", str(db_path)], cwd=ep.ROOT,
                                 capture_output=True).returncode == 0
        tracked = [line for line in subprocess.run(["git", "ls-files", "data/participants", "src/data/participants",
                                                    "src/data/interaction_logs"], cwd=ep.ROOT, capture_output=True,
                                                   text=True).stdout.split() if not line.endswith("README.md")]
    except OSError as exc:
        report.add(WARN, "git", f"git is not available: {exc}")
        return
    within = db_path.resolve().is_relative_to(ep.ROOT)
    if within and not ignored:
        report.add(FAIL, "git", f"{db_path} is not ignored by git")
    elif tracked:
        report.add(FAIL, "git", f"participant data tracked by git: {', '.join(tracked[:5])}")
    else:
        report.add(PASS, "git", "participant data is ignored and none is tracked")


def check_backups(report: Report, config: ep.Config, db_path: Path) -> None:
    """The newest backup of this database is recent and still verifies."""
    if not db_path.is_file():
        return
    folder = db_path.parent / "backups" / config["environment"]
    backups = sorted(folder.glob(f"{config['environment']}-*.sqlite3")) if folder.exists() else []
    if not backups:
        report.add(WARN, "backup", f"no backup in {folder} yet (the server makes one when it starts)")
        return
    newest = backups[-1]
    try:
        pdata.verify_backup(newest)
    except pdata.BackupError as exc:
        report.add(FAIL, "backup", str(exc))
        return
    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(newest.stat().st_mtime)
    hours = age.total_seconds() / 3600
    report.add(PASS if age <= BACKUP_MAX_AGE else WARN, "backup",
               f"newest {newest.name} verifies, {hours:.1f} h old ({len(backups)} in total)")


def check_port(report: Report, port: int) -> None:
    """Whether something already listens on the port the server will use (an old server still running)."""
    with socket.socket() as probe:
        probe.settimeout(0.5)
        busy = probe.connect_ex(("127.0.0.1", port)) == 0
    report.add(WARN if busy else PASS, "port",
               f"something already listens on 127.0.0.1:{port}: stop any older participant server first" if busy
               else f"127.0.0.1:{port} is free")


def run(config_path: Path, db: Path | None = None, port: int = 8765) -> Report:
    """Every check, in the order a researcher would fix them; returns the report."""
    report = Report()
    try:
        config = ep.load_config(config_path)
    except (ep.ConfigError, OSError, ValueError) as exc:
        report.add(FAIL, "configuration", str(exc))
        return report
    db_path = Path(db or config["database_path"])
    check_environment(report, db_path.parent)
    payload = check_design(report, config)
    check_materials(report)
    check_database(report, config, db_path, payload)
    check_privacy(report, db_path)
    check_backups(report, config, db_path)
    check_port(report, port)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ep.CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=None, help="another database of the configuration's environment")
    parser.add_argument("--port", type=int, default=8765, help="the port the server will listen on")
    args = parser.parse_args()
    report = run(args.config, args.db, args.port)
    for status, check, detail in report.findings:
        print(f"{status}  {check}: {detail}")
    counts = {s: sum(1 for f in report.findings if f[0] == s) for s in (PASS, WARN, FAIL)}
    verdict = "NOT READY" if report.failed else "ready, with warnings" if counts[WARN] else "ready"
    print(f"\n{counts[PASS]} passed, {counts[WARN]} warning(s), {counts[FAIL]} failed: {verdict}")
    sys.exit(1 if report.failed else 0)


if __name__ == "__main__":
    main()
