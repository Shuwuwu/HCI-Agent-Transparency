# Postponed: skill-generation improvements (prompt v4, validator v5)

**Status: implemented on 2026-11-17** (prompt `skill_capability_v4`, validator `capability_validator_v5`). The six
skills in `src/data/skills/` were regenerated with `gpt-6.1-sol` under these rules and pass the validator. The
text below is the plan as it was written, kept as the reasoning behind the changes; where the implementation differs
from it, a note says so. The previous skills (prompt `skill_capability_v3`, validators v3/v4) stay in git history and
in the attempt logs of `src/data/skill_library/runs/` (attempts that were `accepted` before the regeneration).

Source: a review of our generation pipeline against Anthropic's *Skill authoring best practices*
(https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices). The page was read through a
sub-agent's digest, not directly; check the page before relying on a quoted limit.

Our goal differs from the page's: we want plausible, legitimate skills that participants read and that an agent
follows, not maximum task success. That is why conciseness and not inventing rules weigh more than the page's other
points. Evidence from our own output: the six skills have bodies of 772 to 830 words (limit 900) and 64 to 76 lines.

## Which recommendations our pipeline already follows

| Recommendation | Status | Evidence |
|---|---|---|
| Frontmatter has only `name` and `description`, within limits | Implemented | Checks S1 to S3 |
| Description says what the skill does and when to use it | Implemented | Prompt rule 1; check S8 |
| Body under 500 lines | Implemented | 64 to 76 lines |
| Clear sequential steps with verification | Implemented | 3 to 6 numbered steps; write workflows verify the target and report only confirmed results |
| No time-sensitive information | Implemented | Prompt rule 2 bans dates and versions |
| Description in third person | Missing | Five of six descriptions start with an imperative ("Answer", "Find", "Read", "Compose", "Inventory") |
| Description with specific key terms | Partial | Scope is stated; few words a user would use |
| Consistent terminology | Missing | Nothing asks for it ("appointment", "event" and "entry" can all appear) |
| Concise; assume a capable model | Weak | Tools section restates tool specs; error handling repeated in nearly every workflow |
| Appropriate degrees of freedom | Partial | Write tools get low freedom (rule 4); read workflows are unspecified; some invented conventions |
| One default route, not many options | Missing | Not stated |
| Concrete examples | Deliberately not done | Rule 2 forbids example values (task-agnostic, no leakage); conflicts with the page |
| Evaluations first, no-skill baseline, at least three, several models | Partial | R7 pilot: one scenario, one model |
| Progressive disclosure, reference files, scripts, packages, Windows paths, MCP names, visual analysis | Not relevant | One-file skills loaded whole; no code; plain function names |

## The eight proposed prompt changes (prompt v4)

All are edits to the system prompt, now `src/prompts/skill_capability_system_v4.md` (the user template is unchanged in
content and renamed `skill_capability_user_v4.md`; `TEMPLATE_VERSION` is `skill_capability_v4`). Implemented as rules 1 and
7 to 13 of the prompt; rule 12 names the Output Requirements section without backticks because a test checks the order
of the backticked section headings.

1. **Description voice.** "Third person, present tense, starting with a verb such as 'Answers' or 'Finds'; at most
   about 300 characters; use the words a user would use for these requests; end with 'Use when...'."
2. **Conciseness.** "Assume the assistant is capable and knows what email, calendars and files are. Do not restate tool
   descriptions or argument lists. In `## Tools`, write one short line per tool saying when to choose it. Spend words on
   what the tool descriptions do not settle: call order, which tool to prefer, how to read results, what to report."
3. **Degrees of freedom.** "Read-only workflows: give the goal and the default route and leave judgment to the
   assistant. Steps with a data-changing tool: be exact. Name the preconditions (target identified, user asked), call it
   once, do not repeat it after an uncertain result, and report what the result says."
4. **No invented conventions.** "State a convention (how to count, sort or interpret a weekday) only if the tool
   descriptions or record schemas support it. Otherwise leave it to the assistant. Do not add steps that verify more than
   the request needs." Targets the made-up rules seen in the pilot: "a same-day result counts as today", "next week
   means the following calendar week" and the always-check-before-create step in `calendar-scheduling`.
5. **One default route.** "Give one default route per workflow. Mention an alternative only where the tools return
   different things."
6. **Terminology.** "Use one word per concept, matching the tool and field names (for example always 'event' for
   calendar entries)."
7. **Error handling once.** "Put the general handling of errors and empty results in `## Output Requirements`. In a
   workflow, mention it only where that workflow differs."
8. **Length.** Lower the body limit from 300-900 words to 250-600 (`LIMITS["body_words"]`; the description limit of 300 characters is `LIMITS["description_chars"]`). Participants read these skills, so reading
   time per trial counts.

## Validator updates (version v5)

- `LIMITS["body_words"]` in `build_capability_prompts.py` is the single source of the length rule (read by the prompt
  and by check S7): change it there, rebuild the prompts, and update the tests that pin 300 and 900.
- New warnings (implemented as S10 and S11): description does not start in the third person (a first word ending in a
  single "s" is the heuristic); description longer than 300 characters.
- Keep every existing check. I checked the check list: no current check requires error handling to appear in every
  workflow (W3 ties a data-changing tool to the user's request, P1 and P2 are about naming tools, S9 requires every tool
  in the Tools section, which stays true with one-line entries), so change 7 does not conflict with any check.
- `VALIDATOR_VERSION` is `capability_validator_v5` (36 checks). `stale_skills` flagged all six skills until they were
  regenerated; the manifest records the version each skill was accepted under.
- Add tests: a mutation test for each new warning; the prompt test that pins the rule text; the limits.

## Regeneration and knock-on work

- Regenerate all six skills (about 6 to 12 API calls with `gpt-6.1-sol`: one call each, plus repairs). The first outputs
  of the current skills stay in the attempt logs; use `--force`; the logs are never overwritten.
- Rebuild `src/data/skill_library/prompts/` (every prompt hash changes), then re-run `build_review_sheet.py`.
- Do this **before** the two raters review the skills, so they score the final text. If the raters have started, restart
  their scoring for the changed skills.
- The execution plan (`plan.json`) and the pilot records were **not** rebuilt, on purpose: their skill hashes refer to
  the previous `calendar-lookup`, which stays recoverable from its accepted attempt log (a test checks this). A later
  evaluation needs a new plan built from the current skills.
- Re-run the execution pilot only with approval (new API calls).
- Review the new text for the issues the review already found: the `calendar-scheduling` check-before-create step and
  the invented conventions.

## Not recommended

- **Adding examples to skills**: conflicts with the no-leakage design. If wanted, test separately: a short hand-written,
  leak-linted example per workflow added to the skill definitions.
- **Rewriting skills to raise task success**: the 25 scenarios are the ones gpt-4o already completed, and the pilot's
  no-skill run also passed. The value of R7 is checking that skills do not harm or change behaviour.
