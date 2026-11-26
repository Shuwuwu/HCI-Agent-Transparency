# data/participants/ — private participant data

**Research data. Never commit, upload, share or serve anything in this folder except this README.**
Everything else here is ignored by Git (`.gitignore`) and is never served by `participant_server.py`. The folder
is kept private to its owner (mode 0700) and the databases are readable by their owner only (0600).

| Path | What it is |
|---|---|
| `development.sqlite3` | Development and test participants (`src/data/experiment_configs/development.json`). Simulated data only. |
| `rehearsal.sqlite3` | Simulated participants of the rehearsal (`src/data/experiment_configs/rehearsal.json`): the full procedure with planned unsafe trials shown as their safe scenario. Development environment; never study data. |
| `study.sqlite3` | Real study participants (`src/data/experiment_configs/study.json`). Created at the first study enrolment. Append-only for recorded data. |
| `*.sqlite3-wal`, `*.sqlite3-shm` | SQLite's write-ahead log and index while a database is open. Part of the database: never delete them while the server runs, and never copy a database file without them (make a backup instead). |
| `exports/<environment>/` | CSV files derived from a database for analysis. Rebuilt on every export; the database stays the source of truth. |
| `backups/<environment>/` | Verified copies of a database (`<environment>-<UTC time>-<label>.sqlite3` with a `.sha256` beside it; label `startup`, `shutdown`, `manual` or `pre-restore`). Made by `participant_data.py backup` and by the server when it starts and stops. |
| `backups/legacy/` | The archive of the retired storage (`src/data/participants/participants.db` and the `src/data/interaction_logs/` files) made by `migrate_legacy_storage.py`. |

Each database is the single source of truth for its environment: participants, condition assignments,
stage sequences, consent, progress, answers, interaction events and the questionnaire. The development and study
databases are stamped with their environment and refuse to be opened for the other one.

How to read the data, export it, back it up and restore it: `docs/participant_data.md`.
