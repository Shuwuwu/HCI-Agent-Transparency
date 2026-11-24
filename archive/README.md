# Archive

Historical and superseded material, kept as research provenance. **Nothing in here is read by an active script or
test.** Active material lives in `src/`; see the top-level `README.md`.

Each entry says what it is and why it was archived. Files that were replaced in place are not copied here: use Git
history (`git log --follow -- <path>`).

## Inventory

| Path | What it is | Why it was archived |
|---|---|---|
| `2026-11-14_per_scenario_pilot/` | Raw outputs of the first, one-skill-per-scenario skill-generation pilot (own `README.md`). | Superseded by the capability-skill library; outputs cannot be regenerated. Archived earlier (commit `7278b8d`). |
| `excluded_stimuli/` (8 folders, below) | Stimuli that were built but are not in the active set. Moved unchanged (`low.json`, `high.json`, `provenance.json`, `preview.txt`), keeping their original `stim_<hash>` folder names. | The active set (`src/data/stimuli/`) is exactly the 20 successful `skills-v4b` runs. |
| `docs/postponed_skill_generation_improvements.md` | Plan for skill-generation improvements (prompt v4, validator v5). | Marked "implemented on 2026-11-17"; the reasoning is kept, but it no longer describes open work. Moved from `docs/`. |
| `injected_skills_pre_e5e53ed/` | The first injected-skill corpus: 6 skills x 6 AgentDojo injection tasks, copied into 25 per-scenario folders (180 files, 36 distinct, plus a sample `SKILL.md` and the six `injection_task_N.txt` templates). | Built on the capability skills from before commit `e5e53ed` (the current skills differ by 26-38 lines each), all 180 files contain AgentDojo's `tool_knowledge` text verbatim at the same line (line 7) and 150 of them the same attacker address, which would let participants recognise it; never executed and referenced by no script. Superseded by `src/data/injected_skills/` (see `docs/scenario_selection.md`). Added in commit `85c66dc`; moved unchanged. |

### `excluded_stimuli/`

| Folder | Task | Source execution | Reason |
|---|---|---|---|
| `stim_0043ee40e207/` | user_task_23 | `user_task_23__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json` | `skills-v4b` run with `utility: false` (the agent did not complete the task). |
| `stim_0548d88a8cf1/` | user_task_3 | `user_task_3__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json` | `skills-v4b` run with `utility: false` (the agent did not complete the task). |
| `stim_170748905667/` | user_task_5 | `user_task_5__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json` | `skills-v4b` run with `utility: false` (the agent did not complete the task). |
| `stim_2a477c0b14f5/` | user_task_22 | `user_task_22__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json` | `skills-v4b` run with `utility: false` (the agent did not complete the task). |
| `stim_3be9d4683f8f/` | user_task_1 | `injection_task_0.json` | Recorded AgentDojo trace with an `important_instructions` attack (`injection_task_0`), not a run of this project; the active set holds no-injection runs only. |
| `stim_8fcfb5b3735d/` | user_task_1 | `user_task_1__no-skills__gpt-4o-2024-08-06__r1.json` | Pilot run (no-skills, earlier skills prompt v3), not part of the `skills-v4b` batch. |
| `stim_d037089fd99f/` | user_task_1 | `user_task_1__mapped-skills__gpt-4o-2024-08-06__r1.json` | Pilot run (mapped-skills, earlier skills prompt v3), not part of the `skills-v4b` batch. |
| `stim_fcdf76e4ae4c/` | user_task_0 | `user_task_0__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json` | `skills-v4b` run with `utility: false` (the agent did not complete the task). |

Their `provenance.json` still points to the source files, which stay in place and unchanged:
`src/data/execution/runs/` (all 27 run records, including the pilot and the five `utility: false` runs, with their
original hashes) and `src/data/gpt-4o-2024-05-13/workspace/` (the recorded attacked trace). The pilot records,
`plan.json` and `summary.json` were **not** moved: they are historical execution records, `plan.json` is the default
of `build_execution_plan.py` and `run_executions.py`, and the archived stimuli cite the records by path.

## Removed, recoverable from Git history

| Removed | Why |
|---|---|
| `src/data/experiment/user_task_N/action_log.json` (25 empty files) | Placeholders for an action log "still to be designed". Superseded by `src/data/stimuli/`; the builder no longer creates them. Last present at commit `85c66dc`. |

## Renamed, not archived

The 20 active stimulus folders were renamed from `stim_<hash>` to `user_task_<N>__no_injection`. Their `low.json`,
`high.json` and `preview.txt` are byte-identical to before; `provenance.json` gained one field, `stimulus_folder`,
and still holds the original `stimulus_id` (`stim_<hash>`) and the source run's path and SHA-256. To find the new
name of an old id, search `src/data/stimuli/*/provenance.json` for it.

## No scripts archived

Every script in `src/scripts/` is either used by the active pipeline or documented in the README as part of the
workflow (for example `build_unsafe_candidates.py` feeds the still-unimplemented unsafe-execution selection).
