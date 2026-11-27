# Running the study

How to host the experiment, give participants their links, run sessions, and keep the data safe. For where the
data is and how to read, export, back up and restore it, see [participant_data.md](participant_data.md).

## Status

| Part | State |
|---|---|
| Participant flow: consent, practice tour, 8 trials with assessments, questionnaire, completion | Ready; tested end to end in both conditions |
| Data recording, privacy, exports, backups | Ready |
| Rehearsal with simulated participants (`rehearsal.json`) | Ready. Its development assignments were rebuilt after task 24 was replaced by task 17 (rule R7): participants enrolled earlier must be reset (`participant_store.py --config src/data/experiment_configs/rehearsal.json --reset P001 ...`) to get the new trial sequence. |
| Unsafe stimuli | **Not ready.** The 12 unsafe stimuli are built as simulated (constructed) traces in `src/data/simulated/` but not yet manually reviewed. Task 24 is withdrawn after manual review and excluded from the study corpus; by rule R7 spare task 17 takes over its planned item (G2, W3 v1, the same injected skill), built as `user_task_17__g2_ii_w3v1` (see `docs/scenario_selection.md`). The study configuration needs `src/data/participant_assignments.json`, written by `build_assignments.py --write N` once enough of them are marked reviewed for stage 2 to find a final set. |
| Manual stimulus review | **Not done.** Not every one of the 19 served safe and 12 simulated unsafe study stimuli is marked reviewed yet (task 24 is excluded; `review_stimuli.py` lists the status of each). `preflight.py` fails the study configuration until every stimulus a participant will see is marked reviewed and both review records are current. |
| Consent text | **One placeholder left:** `[secure storage platform]` |
| Post-study questionnaire | Ready: 11 items, the same in both conditions (`src/study_materials/questionnaire.json`) |
| Completion screen | **Placeholder** debriefing and compensation text (`src/participant_ui/flow.js`) |

`preflight.py` reports each of these. Do not run real participants or a pilot with real people until it shows no
failures and no placeholder warnings.

## Hosting model

The study is run **in person, on one computer**. That computer runs the participant server and its browser
shows the participant's link. The server listens on `127.0.0.1` only, so no other machine can reach it, and nothing
needs the internet.

- **Requirements:** Python 3.10 or newer (standard library only; no packages to install), git, and a desktop
  browser (Chrome or Firefox). Use a laptop or desktop screen; mobile devices are out of scope.
- **Where the data goes:** the repository's `data/participants/` folder on that computer. Keep the repository on
  the computer's own disk, not in a synced folder (OneDrive, iCloud, Dropbox), so participant data is never
  uploaded. Copy backups to approved storage yourself (see "After each session").
- **No public deployment.** The server speaks plain HTTP and is built for a single local computer. It refuses any
  `--host` other than this computer unless `--allow-network` is given. Use that only on a network your ethics
  approval covers, with access controlled, and never together with `--researcher-preview`, which the server
  refuses off `127.0.0.1` anyway. A link's token travels unencrypted over such a network.
- **Protections already in place:**
  - every response forbids caching, referrers and framing, and allows only the server's own files;
  - participants never see stimulus names, versions or conditions;
  - researcher routes are off unless `--researcher-preview` is passed;
  - nothing under `data/participants/` is ever served.

## One-time setup for the study

1. **Review the simulated unsafe stimuli, then write the study assignments** for N participants (the plan is
   16–20). Mark each item after reading it in the researcher preview
   (`participant_server.py --researcher-preview --corpus src/data/corpora/simulated.json`), regenerate the selection
   so stage 2 picks the final set, then write the assignments (refused until a final set exists):

   ```bash
   python3 -B src/scripts/review_stimuli.py --corpus src/data/corpora/simulated.json --mark ITEM --reviewer NAME --set M1=pass M2=pass M3=pass M4=pass M5=pass M6=pass M7=pass
   python3 -B src/scripts/select_scenarios.py
   ```

   ```bash
   python3 -B src/scripts/build_assignments.py --write 20
   ```

2. **Check the design and the stimuli.** This must report no unservable stimuli:

   ```bash
   python3 -B src/scripts/experiment_plan.py --config src/data/experiment_configs/study.json --check
   ```

3. **Enrol the participants and print their links.** This creates `data/participants/study.sqlite3` and makes it
   append-only:

   ```bash
   python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/study.json --enrol-all --links --base-url http://127.0.0.1:8765
   ```

4. Run the full test suite once, `python3 -B -m unittest discover -s tests`.

## Participant links

Each participant has one link, `http://127.0.0.1:8765/p/<token>`. The token is the only thing that identifies
them to the server. Anyone holding a link could continue that participant's session, so treat links as secrets:

- Print them again at any time with `participant_store.py --config ... --links`. They never change.
- Open the participant's link yourself in the study computer's browser at the start of their session; never email
  or post it.
- The participant ID (`P001`, …) is pseudonymous. Keep the list linking IDs to people (if you need one at all)
  apart from the computer and the data, as the ethics approval specifies.

## Before each session

1. Stop any participant server that is still running. `preflight.py` warns when its port is taken: a server
   started before a code change keeps running the old code.
2. Run the readiness check. It must end with no `FAIL`:

   ```bash
   python3 -B src/scripts/preflight.py --config src/data/experiment_configs/study.json
   ```

3. Start the server. It backs up the database as it starts:

   ```bash
   python3 -B src/scripts/participant_server.py --experiment-config src/data/experiment_configs/study.json
   ```

4. Open the participant's link in a new **private** browser window, maximised. The private window keeps no
   history of the link on the shared computer.

## During a session

- Reloading the page, closing the tab, or restarting the browser, the server or the computer loses nothing:
  reopening the link resumes at the first unfinished screen, and an answered screen is never asked again.
- "This link is not valid" means the token is mistyped. Reprint the links.
- "This session cannot be continued; please contact the researcher" means the configuration or assignments
  changed after the participant enrolled. The server's terminal shows the reason. Do not edit the database;
  restore the configuration they were enrolled under.

## After each session

1. Stop the server with Ctrl+C. It writes a verified `shutdown` backup to `data/participants/backups/study/`.
2. Copy the newest `study-*.sqlite3` and its `.sha256` to the storage named in the ethics approval.
3. Optionally, run `participant_data.py --config src/data/experiment_configs/study.json check`, and see how far
   everyone has come with `participant_store.py --config src/data/experiment_configs/study.json --status`.

## At the end of the study

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/study.json check
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/study.json export
```

```bash
python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/study.json backup
```

Analyse the CSV files in `data/participants/exports/study/`, filtered to `trial_type == "experimental"`. Keep the
final backup and the export manifest with the analysis.

## Rehearsing the procedure

`src/data/experiment_configs/rehearsal.json` runs the complete procedure now, with the development assignments.
Each planned unsafe trial shows its scenario's safe stimulus, recorded as version `rehearsal-safe`. It uses its own
database, `data/participants/rehearsal.sqlite3`, which is never study data.

1. Enrol the rehearsal participants and print their links:

   ```bash
   python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/rehearsal.json --enrol-all --links --base-url http://127.0.0.1:8765
   ```

2. Start the server. `--no-backup` is optional, for repeated rehearsals:

   ```bash
   python3 -B src/scripts/participant_server.py --experiment-config src/data/experiment_configs/rehearsal.json
   ```

3. Walk through a link, then inspect what was recorded:

   ```bash
   python3 -B src/scripts/participant_data.py --config src/data/experiment_configs/rehearsal.json show P001
   ```

4. To start a rehearsal participant over, keeping their link:

   ```bash
   python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/rehearsal.json --reset P001
   ```

The same complete procedure runs automatically in `tests/test_pilot_procedure.py`: two participants, one per
condition, with the server stopped and restarted midway.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `preflight` fails "review records ... is out of date" | A stimulus or the checks changed after the record was written, or a status was edited by hand. Run the `review_stimuli.py --corpus ... --write` it names (manual answers are kept; a review of changed content becomes `stale`). |
| `preflight` fails "manual review" | A stimulus shown to participants is not marked `reviewed`. Review it and record it with `review_stimuli.py --mark`. |
| `preflight` warns the port is taken | An older server is still running; stop it (Ctrl+C in its terminal), or use `--port`. |
| `... is a development database; it cannot be opened for study data` | The wrong `--db` or configuration. Each configuration names its own database; leave out `--db`. |
| `... does not exist: enrol participants first` | Run `participant_store.py --config ... --enrol-all`. |
| `This conversation could not be loaded` | The stimulus of that trial cannot be served. `experiment_plan.py --check` names it. |
| A database or backup looks damaged | Stop the server, run `participant_data.py restore` with the newest verified backup (see participant_data.md). |
