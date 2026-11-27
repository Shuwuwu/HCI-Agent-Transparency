# Participant data: storage, access and export

This guide is for researchers. How to host the study and run sessions is in [running_the_study.md](running_the_study.md). Participants never see any of it: nothing below is served by
`participant_server.py`.

## Where the data is

All participant data lives in **one SQLite database per environment**, in `data/participants/`:

| File | Environment | Configuration |
|---|---|---|
| `data/participants/development.sqlite3` | `development`: test participants only | `src/data/experiment_configs/development.json` |
| `data/participants/rehearsal.sqlite3` | `development`: rehearsal participants (unsafe trials shown as their safe scenario) | `src/data/experiment_configs/rehearsal.json` |
| `data/participants/study.sqlite3` | `study`: real participants | `src/data/experiment_configs/study.json` |

The database is the single source of truth for everything recorded about a participant:

- their enrolment;
- their condition and counterbalancing list;
- their stage sequence, which records the scenario order;
- consent;
- progress and timestamps;
- assessment answers;
- interaction events;
- the pre- and post-study questionnaires.

Exports and backups are derived from it and never edited. A database is stamped with its environment the first
time it is opened and refuses the other one. A study database is **append-only** for recorded data: SQLite
triggers refuse changing or deleting answers, consents, events and questionnaire answers, and deleting
participants or progress.

`data/participants/` is gitignored except its `README.md`, and nothing in it is ever served. The folder is private
to its owner (0700) and the files are readable by their owner only (0600). Never copy this folder to a shared or
synced location (OneDrive, email, Git). Store backups only where the ethics approval allows.

Every command below takes `--config`. Without it, commands use the study configuration.

## Quick commands

```bash
python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json --status
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json show P001
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json export
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json export --participant P001
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/development.json check
```

What each command does:

- `--status` shows how far each participant has come.
- `show ID` prints everything stored about one participant as JSON. It omits their link token. It includes the
  consent text only with `--consent-text`.
- `export` writes the CSV files to `data/participants/exports/<environment>/`. With `--participant ID` they go
  to `<...>/<ID>/` instead. Use `--out DIR` for another folder.
- `check` reports inconsistent records and exits with status 1 if it finds any.

## Inspecting the database with DB Browser for SQLite

[DB Browser for SQLite](https://sqlitebrowser.org/) is a free desktop viewer. On macOS, install it from that site
or with `brew install --cask db-browser-for-sqlite`.

1. **Open it read-only.** Choose *File → Open Database Read Only…* (⇧⌘O) and pick
   `data/participants/<environment>.sqlite3`. Read-only mode makes accidental edits impossible. It is safe while
   the participant server is running: SQLite's write-ahead log lets readers and the server work at the same time.
   Close DB Browser when you are done, so it does not hold the file open.
2. **Start with the views.** On the *Browse Data* tab, choose a table or view from the drop-down:
   - `participant_overview`: one row per participant, with condition, list, consent time, stages completed and
     total, current stage, completion time, and counts of answers, events and questionnaire items.
   - `trial_summary`: one row per trial (practice included, marked by `trial_type`). It holds the scenario,
     stimulus version (`practice`, `safe` or `unsafe`), stimulus folder and condition. It also has when the
     scenario and its assessment were entered and completed, plus `scenario_s`, `assessment_s` and
     `scenario_visible_ms`. Then come the four answers and the number of page loads and events.
   - `experimental_responses` and `experimental_events`: answers and events without the practice trial.
3. **Filter by participant.** Type the participant ID (for example `P001`) in the filter box above the
   `participant_id` column.
4. **Run queries.** On the *Execute SQL* tab, paste one of the queries below and press ⌘↩.

Everything about one participant, table by table:

```sql
SELECT * FROM participant_overview WHERE participant_id = 'P001';
SELECT * FROM plan_stages        WHERE participant_id = 'P001' ORDER BY stage_index;  -- what they were shown, in order
SELECT * FROM progress           WHERE participant_id = 'P001' ORDER BY stage_index;  -- when each stage was entered/completed
SELECT participant_id, agreed_at, consent_sha256 FROM consents WHERE participant_id = 'P001';
SELECT * FROM responses          WHERE participant_id = 'P001';
SELECT * FROM questionnaire_responses WHERE participant_id = 'P001';
SELECT * FROM events             WHERE participant_id = 'P001' ORDER BY trial_id, seq;
```

How each experimental trial went, for all participants:

```sql
SELECT participant_id, trial_ordinal, stimulus_version, condition, performed_safely, confidence,
       scenario_s, scenario_visible_ms, assessment_s
FROM trial_summary WHERE trial_type = 'experimental' ORDER BY participant_id, trial_ordinal;
```

Which skill documents were opened, and for how long:

```sql
SELECT participant_id, trial_id, skill_id, SUM(duration_ms) AS viewed_ms
FROM experimental_events WHERE type = 'skill_doc_view'
GROUP BY participant_id, trial_id, skill_id;
```

Which Mail, Calendar and Drive items were opened, and for how long:

```sql
SELECT participant_id, trial_id, tab, item_id, COUNT(*) AS views, SUM(duration_ms) AS viewed_ms
FROM experimental_events WHERE type = 'workspace_item_view'
GROUP BY participant_id, trial_id, tab, item_id;
```

Do not edit data in DB Browser. In a study database, the triggers refuse edits anyway.

## What is stored

| Table | One row per | Main columns |
|---|---|---|
| `participants` | enrolled participant | `participant_id` (pseudonymous), `token` (the link; never exported), `condition`, `list`, `trials`, `simulated`, configuration and stage fingerprint, `enrolled_at`, `completed_at` |
| `plan_stages` | stage of a participant's sequence | `stage_index`, `stage_key`, `kind`, `trial_id`, `trial_ordinal`, `practice`, `scenario`, `stimulus_version`, `stimulus`, `condition` |
| `progress` | stage a participant has entered | `entered_at` (first arrival), `completed_at` |
| `consents` | participant who agreed | `agreed_at`, `consent_sha256`, `consent_text` (the exact text agreed to) |
| `responses` | completed assessment (practice included, `practice` = 1) | `performed_safely`, `confidence` (1–5), `reasoning`, `sources` and `sources_offered` (JSON lists), `sources_other`, `questions_sha256`, `submitted_at` |
| `events` | interaction event | `trial_id`, `stage_key`, `practice`, `seq` (order of arrival within the trial), `type`, `origin` (`browser`, `server`, or `legacy` for events imported from the retired files), `tab`, `item_id`, `skill_id`, `card_id`, `duration_ms`, `client_ms` (browser clock), `server_time` |
| `questionnaire_responses` | participant and questionnaire item asked | `stage_key` (`pre_questionnaire` or `questionnaire`, the post-study one), `item_id`, `answer` (`''` for an optional item left blank), `questionnaire_sha256`, `submitted_at` |
| `meta` | setting | `schema_version`, `environment`, `legacy_import` |

Event types:

- **Server events.** `session_start` is written the first time a trial's conversation is shown. `stage_load` is
  written every time the page asks for the current stage, so reloads and resumed sessions show up.
- **Viewing time.** `scenario_visible` is the visible time before Continue, summed over reloads.
- **Workspace.** `workspace_panel_open` and `workspace_panel_close` (with its duration), `workspace_tab_open` and
  `workspace_tab_view`, and `workspace_item_open` and `workspace_item_view`. A `workspace_item_view` has the
  `tab` (`mail`, `calendar` or `drive`), the `item_id` and the `duration_ms` of one continuous display of that
  item. It ends when the participant goes back to the list, switches tab or closes the panel, and when the page
  is closed or the participant continues to the assessment (the panel is closed for them). Reopening the panel
  on the same item starts a new view. An item's total viewing time is therefore the sum of its views. Item ids
  are unique only within their tab, so group by `tab` and `item_id` together. Recorded from schema version 9;
  earlier trials have `workspace_item_open` only.
- **Skills.** `skills_tab_open` and `skills_tab_view`, and `skill_doc_open` and `skill_doc_view`.
- **Activity cards (high condition).** `trace_card_expand`, `trace_card_collapse` and `trace_card_view`.

Durations are measured in the browser and count only while the page is visible. Events that are sent at nearly the
same moment can arrive in a different order than they were produced; `client_ms` keeps the browser's order.

## CSV exports

`export` writes one consistent snapshot of the database. Every file has a header row, and the folder also gets
`export_manifest.json`, which records the source database, its environment, schema version, export time, and each
file's row count and SHA-256. Exports never contain link tokens or the consent text. They are rebuilt from scratch
on every run, so never edit them; change nothing but the database, and only through the application.

| File | One row per | Notes |
|---|---|---|
| `participants.csv` | participant | `participant_overview` |
| `trials.csv` | trial | `trial_summary`, plus the measures of `interaction_log.summarize`: Skills tab and skill-document use, workspace panel time, tabs and items (items opened, and `workspace_items_viewed_ms`, the viewing time per item as `{tab: {item_id: ms}}`), activity cards expanded and viewed. Dict-valued measures are JSON. `trial_type` is `practice` or `experimental`. |
| `progress.csv` | stage entered | stage kind, trial and `trial_type` |
| `events.csv` | event | with `trial_type` |
| `questionnaire.csv` | participant and item asked | long format, both questionnaires, with `stage_key`; `answer` coding below |

`questionnaire.csv` answers are stored as text, keyed by `item_id`. Item ids are unique across
`src/study_materials/pre_questionnaire.json` (`stage_key` `pre_questionnaire`, asked after consent) and
`src/study_materials/questionnaire.json` (`stage_key` `questionnaire`, after the last trial); the file the row's
`questionnaire_sha256` names holds the exact wording. Rows written before schema version 8 have no pre-study
questionnaire and are all `questionnaire`.

| `stage_key` | `item_id` | Answer |
|---|---|---|
| `pre_questionnaire` | `ai_chat_frequency` | `never`, `less_than_monthly`, `monthly`, `weekly`, `daily` |
| `pre_questionnaire` | `agent_experience` | `never`, `once_or_twice`, `occasionally`, `regularly`, `not_sure` |
| `questionnaire` | `unsafe_meaning`, `information_relied_on` | free text (required) |
| `questionnaire` | `information_sufficient` | `1` Not at all sufficient … `5` Completely sufficient |
| `questionnaire` | `skill_docs_usefulness` | `1` Not at all useful … `5` Extremely useful, or `did_not_consult` |
| `questionnaire` | `skill_docs_purpose_understood` | `1` Not at all … `5` Completely |
| `questionnaire` | `information_overload` | `1` Strongly disagree … `5` Strongly agree |
| `questionnaire` | `information_overload_explanation`, `additional_information_wanted` | free text; `''` when left blank |
| `questionnaire` | `activity_log_usefulness` | `1` Not at all useful … `5` Extremely useful; **high condition only** (low participants are never asked it and have no row) |

For analysis, filter `trial_type == "experimental"`. The practice trial is never part of the experimental analysis.

Free-text answers are exported exactly as participants typed them. A spreadsheet program may treat a cell that
starts with `=`, `+`, `-` or `@` as a formula, so load the files with an analysis tool (R, pandas) instead.

## Backups and recovery

A backup is a verified copy of a database at one moment. It is written with SQLite's online backup API, so making
one is safe while the server runs. Backups are written to `data/participants/backups/<environment>/` as one
self-contained file plus a checksum:

```
<environment>-<UTC time>-<label>.sqlite3
<environment>-<UTC time>-<label>.sqlite3.sha256
```

Both files are private (0600). Before a backup is accepted, it is checked three ways: SQLite's integrity check,
the environment stamp, and a row count of every table that must equal the source's.

| Label | Made by |
|---|---|
| `startup` | the participant server, when it starts |
| `shutdown` | the participant server, when it is stopped with Ctrl+C |
| `manual` | `participant_data.py backup` |
| `pre-restore` | `participant_data.py restore`, which saves the current contents before replacing them |

The server always makes its backups for the study database. `--no-backup` turns them off, for development only.

Make a backup by hand:

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/study.json backup
```

**During the study:** after every session, make a backup and copy the newest `study-*.sqlite3` and its `.sha256`
to the storage named in the ethics approval. Encrypted university storage, not a personal cloud folder.
Backups are never deleted automatically. Development backups accumulate in
`data/participants/backups/development/` and can be deleted by hand.

**Restoring** replaces the database with a backup. It refuses a backup that does not match its `.sha256`, that
fails the integrity check, or that belongs to the other environment.

1. Stop the participant server (Ctrl+C). A database that is in use is refused.
2. Run:

   ```bash
   python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/study.json restore data/participants/backups/study/study-<time>-<label>.sqlite3 --replace
   ```

   `--replace` is required when the database exists. Its current contents are first saved as a `pre-restore`
   backup, so the restore itself can be undone by restoring that file. If the database file is lost, leave out
   `--replace`; the backup is copied into place.
3. Run `participant_data.py check`, then start the server again.

A backup copied back from external storage must arrive with its `.sha256` file; that is how the restore knows the
copy arrived unchanged.

## Persistence

- Every answer and stage change is written in one transaction with the stage it completes and committed before
  the server replies. SQLite is in write-ahead-log mode with `synchronous = FULL`, so an acknowledged write
  survives a crash or power loss.
- The browser keeps no state. Reloading the page, closing the tab, or restarting the server resumes the
  participant at their first unfinished stage, and no stage can be completed twice.
- While a database is open, SQLite keeps `-wal` and `-shm` files beside it. They are part of the database: never
  delete them, and never copy the `.sqlite3` file on its own. Use a backup instead.
