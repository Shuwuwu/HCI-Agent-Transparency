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

    Assessment responses. The answers a participant gives after each conversation (`responses`): one row per
    assessment stage, keyed by participant and stage, carrying the trial id, a `practice` flag, the stimulus and
    condition the trial showed, the five answers (whether the user's request was completed, `task_completion`;
    whether the assistant behaved safely; confidence; the reasoning; the information sources, with the description
    given with "Other" in `sources_other`, empty when "Other" was not chosen or, under a question that leaves it
    optional, left blank), the information sources that were offered, and the SHA-256 of the question text.
    `task_completion` was added in schema version 10: rows stored before then keep NULL there, meaning the
    question was not asked. The practice row is flagged so analysis can leave it out (the practice questions are
    now display-only and store nothing); the `experimental_responses` view is the responses without it. Only the
    first submission of a stage is kept.

    Stage sequence. At enrolment the participant's whole stage sequence is copied into `plan_stages` (scenario,
    stimulus, stimulus version, condition, practice flag and trial ids, in order), so the database alone says
    what each participant was shown and in what order. The flow itself still derives the sequence from the
    configuration and checks it against the fingerprint; the snapshot is the record.

    Interaction events (`events`, record_event): one row per event, with the participant, trial and stage it
    belongs to, its order of arrival within the trial (`seq`), the server's time and the browser's clock, and
    whether the browser or the server produced it. The allowed types are EVENT_TYPES; interaction_log.py validates
    their fields and participant_flow.log_event checks the trial is the participant's current one.

    Questionnaires (`questionnaire_responses`): the pre- and post-study questionnaire answers, one row per item,
    with the stage key of the questionnaire it belongs to ("pre_questionnaire" or "questionnaire") and the hash of
    the file the participant answered. Item ids are unique across both files (participant_flow.load_questionnaires
    checks it), and an item a participant was not asked (one shown in the other condition only) has no row. Only
    the first submission is kept.

Inputs:
    An experiment configuration and assignments payload (experiment_plan.py) when enrolling participants, and a
    database file: by default the one the configuration names (its "database" key: data/participants/
    development.sqlite3 or study.sqlite3, gitignored and never served: they hold research data); --db overrides.

    Environments. The development and the study configuration use separate databases, and each database is
    stamped with the environment it was first opened for ("development" or "study", in `meta`); opening it for
    the other one is refused, as is enrolling a configuration's participants into the other environment's
    database. A study database is append-only for recorded data: database triggers refuse changing or deleting
    answers, consents, events and questionnaire answers, and deleting participants or progress. The folder is
    private (0700) and the database files are readable by their owner only (0600).

Outputs:
    The SQLite database: `participants` (one row per enrolled participant: token, condition, list, trial count,
    configuration provenance, stage fingerprint, timestamps), `plan_stages` (their stage sequence, one row per
    stage), `progress` (one row per stage a participant has entered, with its completion time), `consents` (at
    most one row per participant: when they agreed, and the consent text and its hash), `responses` (one row per
    completed experimental assessment), `events` (interaction events), `questionnaire_responses` (both questionnaires, one row
    per item asked) and the view `experimental_responses` (responses without the practice). CHECK constraints
    refuse out-of-range answers, unknown event types and conditions. Researcher views (no link tokens):
    `participant_overview` (one row per participant: condition, consent, how far they are, counts), `trial_summary`
    (one row per trial: what it showed, viewing and response times, the answers) and `experimental_events` (events
    without the practice). The command line enrols participants, prints their links, and prints how far each
    participant has come, including whether they have consented.

Dependencies:
    experiment_plan.py (configuration, assignments and the derived stage sequence). Python standard library only.
    Used by the participant flow routes; tests/test_participant_store.py.

Usage:
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --enrol-all
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --links
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --status
    python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --reset P001 P002
    (--config defaults to the study configuration, so the study database is used unless development is named.)

    A link always reopens at the participant's current stage, so a test participant who has passed consent never
    sees it again. --reset sends simulated (development) participants back to the start: it clears their progress
    and consent but keeps their link, and stores their stage sequence as --config derives it now, so links enrolled
    before a change to the sequence work again. It refuses study participants and study configurations.
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
PARTICIPANT_DATA_DIR = ROOT / ep.PARTICIPANT_DATA_DIR  # private: gitignored, never served
ENVIRONMENTS = ("development", "study")
SIDECARS = ("-wal", "-shm", "-journal")  # files SQLite keeps beside a database; private like the database itself
TOKEN_BYTES = 24  # 32 URL-safe characters
# 2: the consents table; 3: responses and experimental_responses; 4: responses.sources_other;
# 5: plan_stages (the stage sequence each participant was enrolled under), events (interaction events, which
#    replace the JSON-lines log files) and CHECK constraints on responses; 6: questionnaire_responses;
# 7: the researcher views participant_overview, trial_summary and experimental_events (VIEWS);
# 8: questionnaire_responses.stage_key (the pre-study questionnaire is stored in the same table);
# 9: the event type workspace_item_view (the events table is rebuilt, since SQLite cannot change a CHECK);
# 10: responses.task_completion (NULL in rows stored before the question was asked) and trial_summary showing it.
SCHEMA_VERSION = 10
STAGE_KINDS = ("consent", "scenario", "assessment", "questionnaire", "completion")  # as experiment_plan.STAGE_KINDS
CONDITIONS = ("low", "high")
# The answers responses.task_completion accepts: the option ids of assessment.json's task_completion question
# (participant_flow.load_assessment refuses a file whose options are not among these), and '' for the question
# left unanswered where the file makes it optional.
TASK_COMPLETION_ANSWERS = ("yes", "no", "unsure")
# Every interaction event type the database accepts. interaction_log.EVENT_FIELDS validates the fields of each
# type and must name exactly these (tests/test_interaction_log.py checks it); session_start and stage_load are
# written by the server only.
EVENT_TYPES = (
    "session_start", "stage_load", "scenario_visible",
    "workspace_panel_open", "workspace_panel_close", "workspace_tab_open", "workspace_tab_view",
    "workspace_item_open", "workspace_item_view", "skills_tab_open", "skills_tab_view", "skill_doc_open", "skill_doc_view",
    "trace_card_expand", "trace_card_collapse", "trace_card_view",
)
EVENT_ORIGINS = ("browser", "server", "legacy")  # legacy: imported from the retired JSON-lines files


def _in(values) -> str:
    """An SQL list literal of fixed identifiers, for CHECK constraints (never used with participant input)."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


RESPONSES_TABLE = f"""
CREATE TABLE IF NOT EXISTS responses (
    participant_id   TEXT NOT NULL REFERENCES participants(participant_id),
    stage_key        TEXT NOT NULL,
    trial_id         TEXT NOT NULL,
    practice         INTEGER NOT NULL CHECK (practice IN (0, 1)),  -- 1 for the practice trial
    stimulus         TEXT NOT NULL,
    condition        TEXT NOT NULL CHECK (condition IN {_in(CONDITIONS)}),
    performed_safely TEXT NOT NULL CHECK (performed_safely IN ('yes', 'no')),
    confidence       INTEGER NOT NULL CHECK (confidence BETWEEN 1 AND 5),  -- the scale of assessment.json
    reasoning        TEXT NOT NULL,
    sources          TEXT NOT NULL,         -- JSON list of the selected source ids, in the order offered
    sources_offered  TEXT NOT NULL,         -- JSON list of the source ids this participant could choose from
    sources_other    TEXT NOT NULL DEFAULT '', -- the description given with "Other"; '' when "Other" not chosen
    questions_sha256 TEXT NOT NULL,         -- of src/study_materials/assessment.json as served
    submitted_at     TEXT NOT NULL,
    -- Added in version 10, last so a fresh table and an upgraded one have the same columns in the same order.
    -- NULL: a row stored before the question was asked.
    task_completion  TEXT CHECK (task_completion IS NULL OR task_completion IN {_in(('',) + TASK_COMPLETION_ANSWERS)}),
    PRIMARY KEY (participant_id, stage_key)
);"""
RESPONSE_COLUMNS = ("participant_id", "stage_key", "trial_id", "practice", "stimulus", "condition",
                    "performed_safely", "confidence", "reasoning", "sources", "sources_offered", "sources_other",
                    "questions_sha256", "submitted_at", "task_completion")
TASK_COMPLETION_COLUMN = ("task_completion", f"TEXT CHECK (task_completion IS NULL OR task_completion IN "
                                             f"{_in(('',) + TASK_COMPLETION_ANSWERS)})")

# The events table on its own, so the version 9 upgrade (_rebuild_events) can re-create it.
EVENTS_TABLE = f"""CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    trial_id       TEXT,                    -- NULL for an event outside a trial (a stage_load of consent)
    stage_key      TEXT NOT NULL,           -- the participant's stage when the event was received
    practice       INTEGER NOT NULL CHECK (practice IN (0, 1)),
    seq            INTEGER NOT NULL,        -- order of arrival within (participant, trial)
    type           TEXT NOT NULL CHECK (type IN {_in(EVENT_TYPES)}),
    origin         TEXT NOT NULL CHECK (origin IN {_in(EVENT_ORIGINS)}),
    tab            TEXT,
    item_id        TEXT,
    skill_id       TEXT,
    card_id        TEXT,
    duration_ms    INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
    client_ms      INTEGER,                 -- the browser's clock; never used for ordering
    server_time    TEXT NOT NULL
);"""
EVENTS_INDEX = "CREATE INDEX IF NOT EXISTS events_by_trial ON events (participant_id, trial_id, seq);"

SCHEMA = f"""
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
CREATE TABLE IF NOT EXISTS plan_stages (
    participant_id   TEXT NOT NULL REFERENCES participants(participant_id),
    stage_index      INTEGER NOT NULL,
    stage_key        TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN {_in(STAGE_KINDS)}),
    trial_id         TEXT,                  -- scenario and assessment stages only
    trial_ordinal    INTEGER,               -- 0 for the practice, 1.. for experimental trials
    practice         INTEGER NOT NULL CHECK (practice IN (0, 1)),
    scenario         TEXT,
    stimulus_version TEXT,                  -- practice, safe or unsafe: researcher-only
    stimulus         TEXT,
    condition        TEXT CHECK (condition IS NULL OR condition IN {_in(CONDITIONS)}),
    PRIMARY KEY (participant_id, stage_key),
    UNIQUE (participant_id, stage_index)
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
{RESPONSES_TABLE}
CREATE VIEW IF NOT EXISTS experimental_responses AS SELECT * FROM responses WHERE practice = 0;
{EVENTS_TABLE}
{EVENTS_INDEX}
CREATE TABLE IF NOT EXISTS questionnaire_responses (
    participant_id       TEXT NOT NULL REFERENCES participants(participant_id),
    item_id              TEXT NOT NULL,
    answer               TEXT NOT NULL,     -- '' for an optional item left unanswered
    questionnaire_sha256 TEXT NOT NULL,     -- of the questionnaire file as served (src/study_materials/)
    submitted_at         TEXT NOT NULL,
    -- pre_questionnaire or questionnaire (post-study). Added in version 8; the default is the stage every row
    -- written before then belongs to, since the post-study questionnaire was the only one.
    stage_key            TEXT NOT NULL DEFAULT 'questionnaire',
    PRIMARY KEY (participant_id, item_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Read-only views for researchers (DB Browser for SQLite, participant_data.py). They hold no data of their own and
# never show a link token. Times are seconds between the stored ISO timestamps.
VIEWS = """
CREATE VIEW IF NOT EXISTS participant_overview AS
SELECT p.participant_id, p.condition, p.list, p.trials, p.simulated, p.enrolled_at,
       c.agreed_at AS consented_at, c.consent_sha256,
       (SELECT COUNT(*) FROM plan_stages s WHERE s.participant_id = p.participant_id) AS stages_total,
       (SELECT COUNT(*) FROM progress g WHERE g.participant_id = p.participant_id
                                          AND g.completed_at IS NOT NULL) AS stages_completed,
       (SELECT s.stage_key FROM plan_stages s WHERE s.participant_id = p.participant_id AND NOT EXISTS (
            SELECT 1 FROM progress g WHERE g.participant_id = s.participant_id AND g.stage_key = s.stage_key
                                       AND g.completed_at IS NOT NULL)
        ORDER BY s.stage_index LIMIT 1) AS current_stage,
       p.completed_at,
       (SELECT COUNT(*) FROM responses r WHERE r.participant_id = p.participant_id AND r.practice = 0)
           AS experimental_responses,
       (SELECT COUNT(*) FROM events e WHERE e.participant_id = p.participant_id) AS events,
       (SELECT COUNT(*) FROM questionnaire_responses q WHERE q.participant_id = p.participant_id)
           AS questionnaire_items,
       p.config_path, p.plan_fingerprint
FROM participants p LEFT JOIN consents c ON c.participant_id = p.participant_id;

CREATE VIEW IF NOT EXISTS trial_summary AS
SELECT s.participant_id, s.trial_id, s.trial_ordinal,
       CASE s.practice WHEN 1 THEN 'practice' ELSE 'experimental' END AS trial_type,
       s.scenario, s.stimulus_version, s.stimulus, s.condition,
       sp.entered_at AS scenario_entered_at, sp.completed_at AS scenario_completed_at,
       ROUND((julianday(sp.completed_at) - julianday(sp.entered_at)) * 86400, 3) AS scenario_s,
       (SELECT SUM(e.duration_ms) FROM events e WHERE e.participant_id = s.participant_id
            AND e.trial_id = s.trial_id AND e.type = 'scenario_visible') AS scenario_visible_ms,
       ap.entered_at AS assessment_entered_at, ap.completed_at AS assessment_completed_at,
       ROUND((julianday(ap.completed_at) - julianday(ap.entered_at)) * 86400, 3) AS assessment_s,
       r.task_completion, r.performed_safely, r.confidence, r.reasoning, r.sources, r.sources_other, r.sources_offered,
       r.questions_sha256, r.submitted_at,
       (SELECT COUNT(*) FROM events e WHERE e.participant_id = s.participant_id AND e.trial_id = s.trial_id
            AND e.type = 'stage_load') AS stage_loads,
       (SELECT COUNT(*) FROM events e WHERE e.participant_id = s.participant_id AND e.trial_id = s.trial_id)
           AS events
FROM plan_stages s
LEFT JOIN progress sp ON sp.participant_id = s.participant_id AND sp.stage_key = s.stage_key
LEFT JOIN plan_stages a ON a.participant_id = s.participant_id AND a.trial_id = s.trial_id AND a.kind = 'assessment'
LEFT JOIN progress ap ON ap.participant_id = a.participant_id AND ap.stage_key = a.stage_key
LEFT JOIN responses r ON r.participant_id = s.participant_id AND r.stage_key = a.stage_key
WHERE s.kind = 'scenario';

CREATE VIEW IF NOT EXISTS experimental_events AS SELECT * FROM events WHERE practice = 0;
"""
def _study_triggers() -> str:
    """Triggers that make a study database's records append-only (see connect).

    Answers, consents, events, questionnaire answers and stage-sequence snapshots can be added but never changed
    or deleted; participants and progress rows can never be deleted, and may change only by being marked
    completed once. The environment stamp itself cannot be changed. Restoring a backup (participant_data.py)
    copies the file as a whole and is not affected. A real mistake in study data is corrected outside the app,
    deliberately, with its own record of why.
    """
    out = []
    for table in ("consents", "responses", "events", "questionnaire_responses", "plan_stages"):
        out.append(f"CREATE TRIGGER IF NOT EXISTS study_keep_{table}_u BEFORE UPDATE ON {table} "
                   f"BEGIN SELECT RAISE(ABORT, 'study records cannot be changed'); END;")
    for table in ("consents", "responses", "events", "questionnaire_responses", "plan_stages", "participants",
                  "progress"):
        out.append(f"CREATE TRIGGER IF NOT EXISTS study_keep_{table}_d BEFORE DELETE ON {table} "
                   f"BEGIN SELECT RAISE(ABORT, 'study records cannot be deleted'); END;")
    fixed = ("participant_id", "token", "condition", "list", "trials", "simulated", "config_path",
             "assignments_path", "plan_fingerprint", "enrolled_at")
    changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in fixed)
    out.append("CREATE TRIGGER IF NOT EXISTS study_keep_participants_u BEFORE UPDATE ON participants "
               f"WHEN OLD.completed_at IS NOT NULL OR {changed} "
               "BEGIN SELECT RAISE(ABORT, 'study participants can only be marked completed'); END;")
    out.append("CREATE TRIGGER IF NOT EXISTS study_keep_progress_u BEFORE UPDATE ON progress "
               "WHEN OLD.completed_at IS NOT NULL OR NEW.participant_id IS NOT OLD.participant_id "
               "OR NEW.stage_key IS NOT OLD.stage_key OR NEW.stage_index IS NOT OLD.stage_index "
               "OR NEW.entered_at IS NOT OLD.entered_at "
               "BEGIN SELECT RAISE(ABORT, 'study progress can only be marked completed'); END;")
    out.append("CREATE TRIGGER IF NOT EXISTS study_keep_environment BEFORE UPDATE ON meta WHEN OLD.key = 'environment' "
               "BEGIN SELECT RAISE(ABORT, 'the environment of a database cannot be changed'); END;")
    out.append("CREATE TRIGGER IF NOT EXISTS study_keep_environment_d BEFORE DELETE ON meta "
               "WHEN OLD.key = 'environment' "
               "BEGIN SELECT RAISE(ABORT, 'the environment of a database cannot be changed'); END;")
    return "\n".join(out)


STUDY_TRIGGERS = _study_triggers()


class StoreError(ValueError):
    """The requested flow transition or participant record is not valid."""


def now() -> str:
    """The current UTC time, to the millisecond, as the timestamps in the database are written."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def connect(path: Path, environment: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the participant database and apply the schema.

    Rows come back as sqlite3.Row. Write-ahead logging is enabled so the server can read while another request
    writes; every commit is synced to disk (synchronous = FULL), so an answer the server has acknowledged survives
    a crash or power loss. Foreign keys are enforced so a progress row cannot name an unknown participant. Applying
    the schema is idempotent, so an existing database is opened unchanged. Raises StoreError if the file was
    written by a newer schema version than this module knows.

    `environment` ("development" or "study", a configuration's "environment") is checked against the one stamped
    in the database (see _check_environment): a database is stamped the first time it is opened for one, and is
    refused for the other from then on. None opens any database without checking (tests, researcher tools that
    name the file themselves). A study database also refuses changes and deletions of the recorded data
    (STUDY_TRIGGERS). The folder is created private (0700) when missing and the files are made readable by
    their owner only (0600).
    """
    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    if path.parent == PARTICIPANT_DATA_DIR:
        PARTICIPANT_DATA_DIR.chmod(0o700)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The server opens one connection per request and handles requests on separate threads, so two writes can
    # overlap. Write-ahead logging lets readers run during a write, and the timeout waits instead of failing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA + VIEWS)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        conn.commit()
    elif int(row["value"]) > SCHEMA_VERSION:
        raise StoreError(f"{path}: schema version {row['value']} is newer than this module ({SCHEMA_VERSION})")
    elif int(row["value"]) < SCHEMA_VERSION:
        # New tables are made by the CREATE TABLE IF NOT EXISTS script above. A column added to a table that already
        # exists is not, so it is added here; a table the script has just created already has it. Existing rows are
        # untouched apart from taking the new column's default.
        _add_column_if_missing(conn, "responses", "sources_other", "TEXT NOT NULL DEFAULT ''")  # version 4
        _add_column_if_missing(conn, "questionnaire_responses", "stage_key",
                               "TEXT NOT NULL DEFAULT 'questionnaire'")  # version 8
        _add_column_if_missing(conn, "responses", *TASK_COMPLETION_COLUMN)  # version 10: NULL in existing rows
        conn.commit()
        if int(row["value"]) < 5:
            _rebuild_responses(conn, path)
        if int(row["value"]) < 9:
            _rebuild_events(conn, path)
        if int(row["value"]) < 10:
            _recreate_views(conn, path)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))
        conn.commit()
    _check_environment(conn, path, environment)
    if environment_of(conn) == "study":
        conn.executescript(STUDY_TRIGGERS)
    make_private(path)
    return conn


def private_dir(path: Path) -> Path:
    """Create `path` (and any missing parents) with mode 0700, and make sure every level inside
    PARTICIPANT_DATA_DIR is 0700 too (exports, backups and their sub-folders hold participant data)."""
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for level in [path, *path.parents]:
        if level == PARTICIPANT_DATA_DIR or PARTICIPANT_DATA_DIR in level.parents:
            level.chmod(0o700)
    return path


def make_private(path: Path) -> None:
    """Make a database file and its SQLite sidecar files readable and writable by their owner only."""
    for candidate in [Path(path)] + [Path(f"{path}{suffix}") for suffix in SIDECARS]:
        if candidate.exists():
            candidate.chmod(0o600)


def environment_of(conn: sqlite3.Connection) -> str | None:
    """The environment stamped in the database ("development" or "study"), or None if it was never stamped."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'environment'").fetchone()
    return row["value"] if row else None


def _check_environment(conn: sqlite3.Connection, path: Path, environment: str | None) -> None:
    """Stamp `environment` into a database that has none, or refuse a database stamped for the other one.

    Development and study data are kept in separate files (experiment_plan's "database" key); this stops a
    mistyped --db or configuration mixing them. A database is never stamped "study" while it holds a simulated
    participant, nor "development" while it holds a study participant. Raises StoreError.
    """
    if environment is None:
        return
    if environment not in ENVIRONMENTS:
        raise StoreError(f"unknown environment {environment!r}; expected one of {ENVIRONMENTS}")
    stamped = environment_of(conn)
    if stamped is None:
        foreign = conn.execute("SELECT COUNT(*) FROM participants WHERE simulated = ?",
                               (int(environment == "study"),)).fetchone()[0]
        if foreign:
            conn.close()
            raise StoreError(f"{path} holds {foreign} participant(s) of the other environment; "
                             f"it cannot become a {environment} database")
        conn.execute("INSERT INTO meta (key, value) VALUES ('environment', ?)", (environment,))
        conn.commit()
    elif stamped != environment:
        conn.close()
        raise StoreError(f"{path} is a {stamped} database; it cannot be opened for {environment} data")

def _rebuild_responses(conn: sqlite3.Connection, path: Path) -> None:
    """Version 5: re-create `responses` with its CHECK constraints, keeping every row (SQLite cannot add a CHECK
    to an existing table). All or nothing: a stored row that breaks a constraint raises StoreError and leaves the
    database as it was, so an upgrade never drops an answer silently."""
    try:
        conn.execute("BEGIN")
        conn.execute("DROP VIEW IF EXISTS experimental_responses")
        conn.execute("ALTER TABLE responses RENAME TO responses_v4")
        conn.execute(RESPONSES_TABLE)
        columns = ", ".join(RESPONSE_COLUMNS)
        conn.execute(f"INSERT INTO responses ({columns}) SELECT {columns} FROM responses_v4")
        conn.execute("DROP TABLE responses_v4")
        conn.execute("CREATE VIEW experimental_responses AS SELECT * FROM responses WHERE practice = 0")
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise StoreError(f"{path}: cannot upgrade the responses table to schema version 5: {exc}") from exc


def _rebuild_events(conn: sqlite3.Connection, path: Path) -> None:
    """Version 9: re-create `events` so its type CHECK accepts every type of EVENT_TYPES (the new one is
    workspace_item_view), keeping every row with its event_id unchanged (SQLite cannot change a CHECK in place).

    The views that read `events` are dropped and re-created around the copy, and the old table is renamed aside
    and dropped only after its rows are in the new one. Its triggers go with it: a study database's append-only
    triggers are re-created by connect() right after the upgrade, and dropping a table fires no trigger. All or
    nothing: on any error the database is left as it was and StoreError is raised.
    """
    views = [line.split()[5] for line in VIEWS.splitlines() if line.startswith("CREATE VIEW")]
    columns = ", ".join(row["name"] for row in conn.execute("PRAGMA table_info(events)"))
    try:
        conn.execute("BEGIN")
        for view in views:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute("DROP INDEX IF EXISTS events_by_trial")
        conn.execute("ALTER TABLE events RENAME TO events_v8")
        conn.execute(EVENTS_TABLE)
        conn.execute(f"INSERT INTO events ({columns}) SELECT {columns} FROM events_v8")
        conn.execute("DROP TABLE events_v8")
        conn.execute(EVENTS_INDEX)
        for statement in VIEWS.split(";"):  # the view definitions contain no semicolons of their own
            if statement.strip():
                conn.execute(statement)
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise StoreError(f"{path}: cannot upgrade the events table to schema version 9: {exc}") from exc


def _recreate_views(conn: sqlite3.Connection, path: Path) -> None:
    """Version 10: re-create the researcher views (VIEWS) so trial_summary shows responses.task_completion. A
    view keeps the definition it was created with, and CREATE VIEW IF NOT EXISTS leaves an existing one alone, so
    an upgraded database would otherwise keep the old column list. Views hold no data; nothing stored changes.
    All or nothing: on any error the database is left as it was and StoreError is raised."""
    views = [line.split()[5] for line in VIEWS.splitlines() if line.startswith("CREATE VIEW")]
    try:
        conn.execute("BEGIN")
        for view in views:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        for statement in VIEWS.split(";"):  # the view definitions contain no semicolons of their own
            if statement.strip():
                conn.execute(statement)
        conn.commit()
    except sqlite3.DatabaseError as exc:
        conn.rollback()
        raise StoreError(f"{path}: cannot upgrade the researcher views to schema version 10: {exc}") from exc


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    """Add `column` to `table` unless it is already there (used when upgrading an older database)."""
    if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def plan_fingerprint(plan: dict) -> str:
    """A short hash of what a participant's stage sequence consists of.

    Covers the ordered stage keys and, for scenario and assessment stages, the trial id, stimulus folder and
    condition: everything whose change would mean a participant is being shown a different experiment. Timestamps
    and anything outside the sequence are not covered.
    """
    material = [[s["key"], s.get("trial_id"), s.get("stimulus"), s.get("condition")] for s in plan["stages"]]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def snapshot_plan(conn: sqlite3.Connection, participant: str, plan: dict) -> int:
    """Store `plan`'s stage sequence as the participant's rows of `plan_stages`, replacing any earlier rows.

    The snapshot makes the database self-contained: which scenario, stimulus version and condition each stage
    showed, and in what order, can be read from it without the configuration or the assignments file it was
    derived from. It is written in the caller's transaction (no commit), at enrolment and when a simulated
    participant is reset. Returns the number of rows written.
    """
    conn.execute("DELETE FROM plan_stages WHERE participant_id = ?", (participant,))
    rows = [(participant, s["index"], s["key"], s["kind"], s.get("trial_id"), s.get("trial"),
             int(bool(s.get("practice"))), s.get("scenario"), s.get("version"), s.get("stimulus"),
             s.get("condition")) for s in plan["stages"]]
    conn.executemany("INSERT INTO plan_stages (participant_id, stage_index, stage_key, kind, trial_id, trial_ordinal, "
                     "practice, scenario, stimulus_version, stimulus, condition) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def stored_plan_stages(conn: sqlite3.Connection, participant: str) -> list[dict]:
    """The participant's `plan_stages` rows in stage order (empty if no snapshot was stored)."""
    return [dict(row) for row in conn.execute(
        "SELECT * FROM plan_stages WHERE participant_id = ? ORDER BY stage_index", (participant,))]


def stored_plan_fingerprint(conn: sqlite3.Connection, participant: str) -> str | None:
    """plan_fingerprint computed from the stored snapshot, or None without one.

    Equal to participants.plan_fingerprint when the snapshot is the sequence the participant was enrolled
    under; participant_data.py --check compares the two.
    """
    rows = stored_plan_stages(conn, participant)
    if not rows:
        return None
    return plan_fingerprint({"stages": [{"key": r["stage_key"], "trial_id": r["trial_id"], "stimulus": r["stimulus"],
                                         "condition": r["condition"]} for r in rows]})


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
    stamped = environment_of(conn)
    if stamped is not None and stamped != config["environment"]:
        raise StoreError(f"{participant}: {config['path']} is a {config['environment']} configuration; this is a "
                         f"{stamped} database")
    if conn.execute("SELECT 1 FROM participants WHERE participant_id = ?", (participant,)).fetchone():
        raise StoreError(f"{participant} is already enrolled")
    row = {"participant_id": participant, "token": token or secrets.token_urlsafe(TOKEN_BYTES),
           "condition": plan["condition"], "list": plan["list"], "trials": plan["trials"],
           "simulated": int(plan["simulated"]), "config_path": str(config["path"]),
           "assignments_path": str(config["assignments_path"]), "plan_fingerprint": plan_fingerprint(plan),
           "enrolled_at": now(), "completed_at": None}
    try:
        conn.execute(
            "INSERT INTO participants (participant_id, token, condition, list, trials, simulated, config_path, "
            "assignments_path, plan_fingerprint, enrolled_at, completed_at) "
            "VALUES (:participant_id, :token, :condition, :list, :trials, :simulated, :config_path, "
            ":assignments_path, :plan_fingerprint, :enrolled_at, :completed_at)", row)
        snapshot_plan(conn, participant, plan)  # same transaction: never a participant without their sequence
        conn.commit()
    except Exception:
        conn.rollback()
        raise
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


RESPONSE_FIELDS = ("trial_id", "practice", "stimulus", "condition", "task_completion", "performed_safely",
                   "confidence", "reasoning", "sources", "sources_other", "sources_offered", "questions_sha256")


def record_response(conn: sqlite3.Connection, participant: str, stage_key: str, response: dict,
                    commit: bool = True) -> dict:
    """Store one assessment response; `response` holds RESPONSE_FIELDS, already validated (participant_flow.py).

    `sources` and `sources_offered` are lists, stored as JSON. Only the first submission of a stage is kept, so a
    double click or a resubmitted form cannot change an answer after the fact. Pass commit=False to make this part
    of a larger transaction (participant_flow.advance commits it together with completing the assessment stage).
    Returns the stored response.
    """
    row = {name: response[name] for name in RESPONSE_FIELDS}
    row["practice"] = int(bool(row["practice"]))
    row["sources"], row["sources_offered"] = json.dumps(row["sources"]), json.dumps(row["sources_offered"])
    conn.execute(f"INSERT INTO responses (participant_id, stage_key, submitted_at, {', '.join(RESPONSE_FIELDS)}) "
                 f"VALUES (?, ?, ?, {', '.join('?' * len(RESPONSE_FIELDS))}) "
                 "ON CONFLICT (participant_id, stage_key) DO NOTHING",
                 (participant, stage_key, now(), *row.values()))
    if commit:
        conn.commit()
    return response_of(conn, participant, stage_key)


def response_of(conn: sqlite3.Connection, participant: str, stage_key: str) -> dict | None:
    """One stored response with its JSON lists decoded, or None."""
    row = conn.execute("SELECT * FROM responses WHERE participant_id = ? AND stage_key = ?",
                       (participant, stage_key)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["sources"], out["sources_offered"] = json.loads(out["sources"]), json.loads(out["sources_offered"])
    return out


EVENT_COLUMNS = ("tab", "item_id", "skill_id", "card_id", "duration_ms", "client_ms")


def record_event(conn: sqlite3.Connection, participant: str, stage_key: str, trial_id: str | None, practice: bool,
                 event: dict, origin: str, server_time: str | None = None, limit: int | None = None) -> dict:
    """Store one interaction event and commit; returns the stored row.

    `event` is {"type", ...} already validated (interaction_log.validate_event) with any of EVENT_COLUMNS. The
    event's position `seq` is the next number within (participant, trial), taken under an immediate write lock so
    two requests arriving together cannot get the same number. `server_time` defaults to now (the legacy import
    passes the original time). With `limit`, a trial that already holds that many events refuses the next with
    StoreError, so a misbehaving page cannot fill the database.
    """
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        count = conn.execute("SELECT COUNT(*), COALESCE(MAX(seq) + 1, 0) FROM events WHERE participant_id = ? "
                             "AND trial_id IS ?", (participant, trial_id)).fetchone()
        if limit is not None and count[0] >= limit:
            raise StoreError(f"{participant}: trial {trial_id} already holds {limit} events")
        values = {name: event.get(name) for name in EVENT_COLUMNS}
        cursor = conn.execute(
            "INSERT INTO events (participant_id, trial_id, stage_key, practice, seq, type, origin, tab, item_id, "
            "skill_id, card_id, duration_ms, client_ms, server_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (participant, trial_id, stage_key, int(bool(practice)), count[1], event["type"], origin,
             *values.values(), server_time or now()))
        if started:
            conn.commit()
    except Exception:
        if started:
            conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM events WHERE event_id = ?", (cursor.lastrowid,)).fetchone())


def trial_events(conn: sqlite3.Connection, participant: str, trial_id: str | None) -> list[dict]:
    """The events of one trial of one participant in arrival order (trial_id None: events outside any trial)."""
    return [dict(row) for row in conn.execute(
        "SELECT * FROM events WHERE participant_id = ? AND trial_id IS ? ORDER BY seq", (participant, trial_id))]


def record_questionnaire(conn: sqlite3.Connection, participant: str, answers: dict, questionnaire_sha256: str,
                         stage_key: str = ep.POST_QUESTIONNAIRE_KEY, commit: bool = True) -> dict:
    """Store one questionnaire: one row per item of `answers` ({item id: answer text}, already validated by
    participant_flow.py), labelled with the questionnaire's `stage_key` (post-study by default). Only the first
    submission is kept, as for the assessments. Pass commit=False to make it part of a larger transaction
    (participant_flow.advance commits it with the stage). Returns the stored answers of that stage."""
    stamp = now()
    conn.executemany("INSERT INTO questionnaire_responses (participant_id, item_id, answer, questionnaire_sha256, "
                     "submitted_at, stage_key) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT (participant_id, item_id) DO NOTHING",
                     [(participant, item, answer, questionnaire_sha256, stamp, stage_key)
                      for item, answer in answers.items()])
    if commit:
        conn.commit()
    return questionnaire_of(conn, participant, stage_key)


def questionnaire_stage_column(conn: sqlite3.Connection) -> str:
    """The SQL expression for a questionnaire answer's stage key: the stage_key column, or, in a database not yet
    upgraded to version 8 (one opened read-only, as preflight.py does), the constant 'questionnaire', the stage
    every answer stored before then belongs to."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(questionnaire_responses)")}
    return "stage_key" if "stage_key" in columns else f"'{ep.POST_QUESTIONNAIRE_KEY}'"


def questionnaire_of(conn: sqlite3.Connection, participant: str, stage_key: str | None = None) -> dict:
    """The participant's stored questionnaire answers, {item id: answer}, of one questionnaire stage or, with
    `stage_key` None, of both; empty when none are stored. Works on a database not yet upgraded to version 8
    (questionnaire_stage_column)."""
    column = questionnaire_stage_column(conn)
    return {row[0]: row[1] for row in conn.execute(
        f"SELECT item_id, answer FROM questionnaire_responses WHERE participant_id = ? AND (? IS NULL OR {column} = ?) "
        "ORDER BY rowid", (participant, stage_key, stage_key))}


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


def reset(conn: sqlite3.Connection, config: ep.Config, participant: str, payload: dict | None = None) -> dict:
    """Start a simulated participant over under `config`: delete their progress, consent and answers, keep their link.

    For re-testing the flow from consent with the same link, including after the stage sequence itself has changed:
    the participant's plan is derived again from `config` and its fingerprint stored, so a link that would otherwise
    be refused as "the stage sequence changed since enrolment" works again. That is safe only because all their
    progress is deleted at the same time, so no old progress can be resumed into the new sequence.

    Only a participant enrolled from a simulated (development) configuration can be reset, and only under a
    simulated configuration; anything else raises StoreError, so real participants' progress and consent can never
    be erased this way and a test participant cannot be attached to the study design. The token and the enrolment
    time are kept; their assessment responses, interaction events and questionnaire answers are deleted with their
    progress. Returns {"participant_id", "stages_cleared", "consent_cleared", "responses_cleared", "events_cleared",
    "questionnaire_cleared", "plan_changed"}. Raises KeyError for a participant who is not enrolled or not in the assignments.
    """
    row = record(conn, participant)
    if not row["simulated"]:
        raise StoreError(f"{participant}: enrolled from a study configuration ({row['config_path']}); "
                         "study participants cannot be reset")
    if not config["simulated"]:
        raise StoreError(f"{participant}: {config['path']} is a study configuration; reset under a simulated one")
    plan = ep.plan_for(config, participant, payload)
    fingerprint = plan_fingerprint(plan)
    stages = conn.execute("DELETE FROM progress WHERE participant_id = ?", (participant,)).rowcount
    consent = conn.execute("DELETE FROM consents WHERE participant_id = ?", (participant,)).rowcount
    # Their answers go too: otherwise redoing a trial would keep the old answer (only the first is stored).
    responses = conn.execute("DELETE FROM responses WHERE participant_id = ?", (participant,)).rowcount
    # And their interaction events, so a redone trial's measures are not mixed with the earlier attempt's.
    events = conn.execute("DELETE FROM events WHERE participant_id = ?", (participant,)).rowcount
    questionnaire = conn.execute("DELETE FROM questionnaire_responses WHERE participant_id = ?",
                                 (participant,)).rowcount
    conn.execute("UPDATE participants SET completed_at = NULL, plan_fingerprint = ?, config_path = ?, "
                 "assignments_path = ?, condition = ?, list = ?, trials = ? WHERE participant_id = ?",
                 (fingerprint, str(config["path"]), str(config["assignments_path"]), plan["condition"], plan["list"],
                  plan["trials"], participant))
    snapshot_plan(conn, participant, plan)
    conn.commit()
    return {"participant_id": participant, "stages_cleared": stages, "consent_cleared": bool(consent),
            "responses_cleared": responses, "events_cleared": events, "questionnaire_cleared": questionnaire,
            "plan_changed": fingerprint != row["plan_fingerprint"]}


def link(token: str, base_url: str = "") -> str:
    """The participant link for a token: `<base_url>/p/<token>` (a relative path when no base url is given)."""
    return f"{base_url.rstrip('/')}/p/{token}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=ep.CONFIG_PATH)
    parser.add_argument("--db", type=Path, default=None,
                        help="participant database (default: the one the configuration names)")
    parser.add_argument("--base-url", default="", help="prefix for the printed links, e.g. http://127.0.0.1:8765")
    parser.add_argument("--enrol-all", action="store_true", dest="enrol_all",
                        help="enrol every participant of the assignments file that is not enrolled yet")
    parser.add_argument("--links", action="store_true", help="print every enrolled participant's link")
    parser.add_argument("--status", action="store_true", help="print how far each enrolled participant has come")
    parser.add_argument("--reset", nargs="+", metavar="ID", default=[],
                        help="send these simulated participants back to the first stage (their links are kept)")
    args = parser.parse_args()
    config = ep.load_config(args.config)
    payload = ep.load_assignments(config)
    db = args.db or config["database_path"]
    if not db.exists() and not args.enrol_all:
        parser.error(f"{db} does not exist; it is created by the first --enrol-all")
    conn = connect(db, config["environment"])
    for participant in args.reset:
        try:
            print(json.dumps(reset(conn, config, participant, payload)))
        except (KeyError, StoreError) as exc:
            parser.error(f"--reset {participant}: {exc}")
    if args.enrol_all:
        added = enrol_all(conn, config, payload)
        print(f"enrolled {len(added)} new participant(s) in {db}")
    rows = conn.execute("SELECT participant_id, token FROM participants ORDER BY participant_id").fetchall()
    if args.links:
        for row in rows:
            print(f"{row['participant_id']}\t{link(row['token'], args.base_url)}")
    if args.status:
        for row in rows:
            print(json.dumps(status(conn, config, row["participant_id"], payload)))
    if not (args.enrol_all or args.links or args.status or args.reset):
        parser.error("choose --enrol-all, --links, --status or --reset")


if __name__ == "__main__":
    main()
