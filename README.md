# CS889 HCI Agent Transparency Project

A user study on whether showing a workspace agent's action log helps people notice unauthorized behaviour. The
stimuli come from recorded `gpt-4o-2024-05-13` runs of AgentDojo's **workspace** suite (email, calendar, cloud
drive), pinned to AgentDojo commit `af7266303c748a9cf0442d71ceabc9d2488d7188`. Participants judge whether the
agent's behaviour matches the user's request, with or without the execution trace, and can read the SKILL.md
documents the agent is said to have followed.

## Repository layout

| Path | Contents |
|---|---|
| `src/scripts/` | The data-processing scripts (see below). Python 3, standard library only. |
| `src/data/gpt-4o-2024-05-13/workspace/` | Recorded AgentDojo runs (no-attack, `important_instructions`, `tool_knowledge`), unmodified. |
| `src/data/gpt4o_workspace_scenarios.json` | All 40 user tasks with the tools gpt-4o called and a `safe_candidate` flag (25 tasks). |
| `src/data/gpt4o_unsafe_candidates.json` | The pool of 116 attacked runs that completed the task and achieved the injection goal. |
| `src/data/agentdojo_source_manifest.json` | Paths and git hashes of the AgentDojo files and traces used (provenance). |
| `src/data/skill_taxonomy.json` | The capability skill library: 6 skills, the Yang et al. subtopics they cover, their workflows, and the map from the 25 scenarios to skills. |
| `src/data/skill_library/` | Inputs of the capability skills: `sources/<skill>.json` (definition, tool specs, record schemas, provenance) and `prompts/<skill>.json` (the exact request to send to the skill-writing model). |
| `src/data/skill_library/review/` | The manual review sheet for the six skills (CSV files for two raters and a guide); see `review_guide.md`. |
| `src/data/execution/` | The execution evaluation: `plan.json` (the pilot's runs) and `plan_mapped_skills_v4b.json` (batch of the 25 mapped scenarios with the current skills; run ids end in `__skills-v4b`), `runs/<run_id>.json` (one record per run: system message, AgentDojo trace, utility, model calls and tokens), `model_access.json`, `summary.json`. |
| `src/data/stimuli/user_task_<N>__no_injection/` | The 20 **active** stimuli: one per successful (`utility: true`) run of the `skills-v4b` batch. Participant-facing `low.json` (request + final response) and `high.json` (adds the action log), plus researcher-only `provenance.json` (which keeps the original `stim_<hash>` id and the source run's path and SHA-256) and `preview.txt`. The folder name is `user_task_<N>__<variant>`; an attacked run would be `user_task_<N>__<attack>_<injection task>`. The builder refuses to overwrite a folder holding a different stimulus id. |
| `src/data/workspace/user_task_<N>__no_injection/` | Workspace (mail, calendar, drive) before and after one execution: `workspace.json` (participants only ever receive its `before` state; `after` is for researcher verification) and researcher-only `provenance.json` (source run + SHA-256, per-call replay check, list of changes). Built for all 20 active stimuli (every no-injection scenario starts from the same AgentDojo environment, so the `before` state is identical across them; only the `after` state differs). |
| `src/data/interaction_logs/` | Gitignored. One `<session>.jsonl` per browser session written by the participant server (`interaction_log.py`): skill documentation opened and for how long, workspace tabs used. Never served to participants. |
| `src/participant_ui/` | Front end of the participant chatbot prototype (served by `participant_server.py`): chat (`app.js`, `markdown.js`) the read-only workspace sidebar (`workspace.js`, `previews.js`), the Skills tab of the workspace panel (`skills.js`) and interaction logging (`interaction.js`). |
| `src/data/explorer/` | Gitignored. Output of `export_explorer_traces.py`: `hci-stimuli-poc.jsonl` (traces plus stimulus annotations, for upload to a local Invariant Explorer) and `comparison.md` (original vs Explorer vs stimulus). |
| `src/data/skills/<skill>/SKILL.md` | The generated capability skills (prompt v4, validator v5), with `manifest.json` tying each file to the request, response and validator version that produced it. Attempt logs are in `src/data/skill_library/runs/`. |
| `src/data/experiment/user_task_N/` | Inputs of each of the 25 safe-candidate scenarios: `metadata.json`, `no_attack_execution_trace.json`, `skills.json`. |
| `archive/` | Historical and superseded material kept as provenance, with an inventory and the reason for each item in `archive/README.md`. Not read by any active script. |
| `Skills collection/` | Original and prompt-injected skill examples from SkillJect (see `Option_A/description.txt`). |
| `tests/` | Unit tests, one file per script. |

AgentDojo itself is not copied into this repository. The scripts read its files at the pinned commit from a local
clone (default `../agentdojo`, or set `AGENTDOJO_REPO`) through `git cat-file`, so the clone is never modified.

## Scripts

Scenario selection and AgentDojo access:

| Script | What it does |
|---|---|
| `agentdojo_runs.py` | Shared parsing of recorded runs and the constants that describe them (tool groups, write tools, injection goals). |
| `agentdojo_source.py` | Read-only access to AgentDojo's source files and traces at the pinned commit; writes the source manifest. |
| `agentdojo_tools.py` | The 24 workspace tools as the agent saw them (descriptions and argument schemas), derived from AgentDojo's source. |
| `extract_scenarios.py` | Builds `gpt4o_workspace_scenarios.json` from the recorded runs. |
| `build_unsafe_candidates.py` | Builds `gpt4o_unsafe_candidates.json` from the attacked runs. |
| `verify_vendored_runs.py` | Checks the vendored run files against AgentDojo's repository over the network. |
| `build_experiment_stimuli.py` | Creates `src/data/experiment/` for the 25 safe-candidate scenarios. |

Capability skills:

| Script | What it does |
|---|---|
| `check_skill_map.py` | Checks the taxonomy: write-tool ownership, scenario map against each recorded trace, and that the skill definitions contain nothing taken from the scenarios. |
| `build_skill_sources.py` | Builds the source package of each skill (definition, full tool specs, record schemas) from the taxonomy's `skills` section and AgentDojo's tool code. Never reads the scenario map. |
| `build_capability_prompts.py` | Builds the exact request for each skill from its source package and the templates in `src/prompts/skill_capability_*_v4.md`. Needs neither the clone nor the network. |
| `prompt_utils.py` | Helpers shared by the source builder, the prompt builder and the generator (settings, hashing, templates, record schemas). |
| `skill_text.py` | Text analysis shared by the validator and the taxonomy checker: parsing a skill file, finding tool names and specific values, comparing texts, and the generic tool-accuracy and safety checks. |
| `generate_skills.py` | Sends each skill's saved request to the OpenAI Responses API (`gpt-6.1-sol`), validates the answer, repairs it up to twice, logs every call, and writes `SKILL.md`, the manifest and a summary. Needs `OPENAI_API_KEY`; has `--dry-run` and `--check-access`. |
| `validate_capability_skills.py` | Static validator for generated capability skills: 34 checks (structure, tool accuracy, write safety, workflow coverage, agent behaviour, generalisability, safety) plus library-level coverage and overlap checks. Versioned (`VALIDATOR_VERSION`). |
| `build_review_sheet.py` | Builds the manual review sheet (`src/data/skill_library/review/`): 6 criteria per skill (instruction accuracy, tool coverage, generalizability, safety) and one row per scenario and mapped skill, with the recorded run's calls and empty rater columns. |
| `setup_execution_env.py` | Creates `.venv-agentdojo/`: exports the pinned AgentDojo commit from the clone with `git archive` (read-only) and installs it with the packages in `src/requirements_execution.txt`. |
| `build_execution_plan.py` | Builds `src/data/execution/plan.json`: which scenarios, conditions (`no-skills`, `mapped-skills`), models and repetitions to run, with a run budget (default 2). Also builds the system message with the skills appended. |
| `run_executions.py` | Runs the plan in AgentDojo (default pipeline, no defence) with the environment's Python; one record per run; stops at the budget; never repeats a finished or failed run without `--force`. Also `--check-access MODEL...` and `--ground-truth-check SCENARIO` (no model call). |
| `summarize_executions.py` | Puts the runs of each scenario side by side (utility, tool calls, differences from each other and from the recorded gpt-4o run, tokens). |
| `build_stimuli.py` | Turns one execution trace (a run record or an AgentDojo trace, safe or attacked) into the low- and high-transparency stimuli by fixed rules; writes `src/data/stimuli/<user_task_N__variant>/`. |
| `export_explorer_traces.py` | Researcher-side only: exports traces to Invariant Explorer's JSONL format, annotating each tool call and result with the stimulus log entry derived from it, and writes a comparison of original, Explorer view and stimulus. Needs the stimulus to exist (run `build_stimuli.py` first). No API calls. |
| `build_workspace_snapshots.py` | Builds `src/data/workspace/`: loads AgentDojo's workspace environment at the pinned commit (default injection text, task init), replays the execution's saved tool calls through AgentDojo's own tools (no model call), and requires every replayed result to equal the saved one. Run it with `.venv-agentdojo/bin/python`. |
| `interaction_log.py` | Validates, stores and summarises participant interaction events (`python3 -B src/scripts/interaction_log.py [LOG_DIR]` prints per-session measures). |
| `participant_server.py` | Participant-facing chatbot prototype (`src/participant_ui/`): serves one stimulus in the low or high condition from `low.json`/`high.json` only. Also serves the workspace before the agent ran, the skill documentation of the execution (hash-checked) and `POST /api/log`. Lists and serves only eligible stimuli (successful `skills-v4b` runs, checked against the provenance and execution record). Run it with `python3 src/scripts/participant_server.py`. |
| `stimulus_text.py` | The wording rules of the action log (action sentence, shown arguments, outcome summary per tool); never copies free text from results. |
| `tool_result_parser.py` | Reads AgentDojo's YAML-style tool result text with the standard library only; checked against PyYAML on every recorded result. |

The first, one-skill-per-scenario pipeline was removed once the capability pipeline replaced it; its pilot outputs are kept in
`archive/2026-11-14_per_scenario_pilot/`, and its code can be recovered from commit `1547329`.

## Skill pipeline: status

The skills are a small library of reusable **capability skills** (calendar lookup, calendar scheduling, email lookup,
email correspondence, drive file management, document content), not one skill per scenario. Each skill covers one
workflow family, a scenario may use several, and each of the 10 data-changing tools belongs to exactly one skill. The
categories come from Yang et al. 2025 (arXiv 2512.07828, Table 1, topic Productivity & Workflow). The first pilot
generated one skill per scenario and was found to produce task-shaped skills that leak scenario content; see
`archive/2026-11-14_per_scenario_pilot/README.md`.

Done: taxonomy, scenario map and checker; scenario stimuli folders with `skills.json`; per-skill source packages and
generation prompts; the per-skill static validator; the generator. The prompts are task-agnostic: the generator is shown a skill's scope, workflows, tool
specifications and record schemas, and no scenario, example request, trace or answer.

All six skills are generated and pass the validator (`capability_validator_v5`, generated with prompt `skill_capability_v4`). The review sheet is built and waiting for the two raters. The execution pipeline is built and has run once as a pilot (one scenario, two runs; see `src/data/execution/summary.json`); the pilot records and `plan.json` refer to the previous skills (prompt v3), recoverable from the attempt logs. The mapped-skills batch `skills-v4b` (25 scenarios, `gpt-4o-2024-08-06`, one run each, current skills) has been executed; its stimuli are built and its traces exported to `src/data/explorer/mapped_skills_v4b/`. The trace-to-stimulus pipeline is built and tested; the 20 successful runs of the batch are the active stimuli (the stimuli of the 5 failed runs, the pilot runs and a recorded attacked run were archived; their run records stay in `src/data/execution/runs/`; see `archive/README.md`). Not implemented yet: selecting the experiment's scenarios and unsafe executions, injected skill variants, the experiment app.

## Running things

```
python3 -B -m unittest discover -s tests          # all tests (some skip without the AgentDojo clone)
python3 -B src/scripts/check_skill_map.py          # check the skill taxonomy against the 25 recorded runs
python3 -B src/scripts/build_experiment_stimuli.py # rebuild src/data/experiment/
python3 -B src/scripts/build_skill_sources.py      # rebuild src/data/skill_library/sources/ (needs the clone)
python3 -B src/scripts/build_capability_prompts.py # rebuild src/data/skill_library/prompts/
python3 -B src/scripts/validate_capability_skills.py # validate src/data/skills/*/SKILL.md (skips skills not written yet)
python3 -B src/scripts/build_review_sheet.py         # rebuild the review sheet (needs all six skills)
python3 -B src/scripts/setup_execution_env.py       # once: AgentDojo environment in .venv-agentdojo/ (needs the clone, PyPI)
python3 -B src/scripts/build_execution_plan.py --scenarios user_task_1 --models gpt-4o-2024-08-06
python3 -B src/scripts/build_execution_plan.py --scenarios ... --conditions mapped-skills --models gpt-4o-2024-08-06 --max-runs 25 --batch skills-v4b --out src/data/execution/plan_mapped_skills_v4b.json
.venv-agentdojo/bin/python -B src/scripts/run_executions.py --dry-run     # then without --dry-run (needs OPENAI_API_KEY)
python3 -B src/scripts/summarize_executions.py
python3 -B src/scripts/build_stimuli.py src/data/execution/runs/*__skills-v4b.json   # the active stimuli (no API call); other runs of a task would collide on the folder name
python3 src/scripts/participant_server.py              # participant prototype at http://127.0.0.1:8765/
.venv-agentdojo/bin/python -B src/scripts/build_workspace_snapshots.py --folders $(ls src/data/stimuli)   # workspace data for all 20 stimuli (no API call)
node tests/test_markdown_js.js                         # tests of the prototype's Markdown renderer
node tests/test_previews_js.js                         # tests of the workspace preview helpers
python3 -B src/scripts/export_explorer_traces.py SOURCE ...   # Explorer upload file + comparison.md in src/data/explorer/
python3 -B src/scripts/generate_skills.py --dry-run  # show what would be sent (sends nothing, needs no key)
OPENAI_API_KEY=... python3 -B src/scripts/generate_skills.py --skills calendar-lookup email-correspondence  # pilot run
```

Use `python3 -B` so that no `__pycache__` folders are created. Scripts that call the OpenAI API read the key from the
`OPENAI_API_KEY` environment variable only; it is never written to a file or log.
