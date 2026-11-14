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
| `src/data/skills/<skill>/SKILL.md` | The generated capability skills (empty until generated), with `manifest.json` tying each file to the request, response and validator version that produced it. Attempt logs are in `src/data/skill_library/runs/`. |
| `src/data/experiment/user_task_N/` | Stimuli of each of the 25 safe-candidate scenarios: `metadata.json`, `no_attack_execution_trace.json`, `skills.json`, `action_log.json` (placeholder). |
| `archive/` | Superseded research outputs kept as provenance (not read by any script). |
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
| `build_capability_prompts.py` | Builds the exact request for each skill from its source package and the templates in `src/prompts/skill_capability_*_v2.md`. Needs neither the clone nor the network. |
| `prompt_utils.py` | Helpers shared by the source builder, the prompt builder and the generator (settings, hashing, templates, record schemas). |
| `skill_text.py` | Text analysis shared by the validator and the taxonomy checker: parsing a skill file, finding tool names and specific values, comparing texts, and the generic tool-accuracy and safety checks. |
| `generate_skills.py` | Sends each skill's saved request to the OpenAI Responses API (`gpt-6.1-sol`), validates the answer, repairs it up to twice, logs every call, and writes `SKILL.md`, the manifest and a summary. Needs `OPENAI_API_KEY`; has `--dry-run` and `--check-access`. |
| `validate_capability_skills.py` | Static validator for generated capability skills: 32 checks (structure, tool accuracy, write safety, workflow coverage, generalisability, safety) plus library-level coverage and overlap checks. Versioned (`VALIDATOR_VERSION`). |

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

Not run yet: the generator has never called the API (needs your key). Not implemented yet (planned, in order): a manual review sheet; execution-based evaluation in AgentDojo; injected skill variants.

## Running things

```
python3 -B -m unittest discover -s tests          # all tests (some skip without the AgentDojo clone)
python3 -B src/scripts/check_skill_map.py          # check the skill taxonomy against the 25 recorded runs
python3 -B src/scripts/build_experiment_stimuli.py # rebuild src/data/experiment/
python3 -B src/scripts/build_skill_sources.py      # rebuild src/data/skill_library/sources/ (needs the clone)
python3 -B src/scripts/build_capability_prompts.py # rebuild src/data/skill_library/prompts/
python3 -B src/scripts/validate_capability_skills.py # validate src/data/skills/*/SKILL.md (skips skills not written yet)
python3 -B src/scripts/generate_skills.py --dry-run  # show what would be sent (sends nothing, needs no key)
OPENAI_API_KEY=... python3 -B src/scripts/generate_skills.py --skills calendar-lookup email-correspondence  # pilot run
```

Use `python3 -B` so that no `__pycache__` folders are created. Scripts that call the OpenAI API read the key from the
`OPENAI_API_KEY` environment variable only; it is never written to a file or log.
