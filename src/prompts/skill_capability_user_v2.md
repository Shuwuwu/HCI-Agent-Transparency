## Skill definition

Identifier (use it as `name`): {{skill_id}}

{{kind_statement}}

Scope: {{scope}}

## Workflows to cover

Write one subsection under `## Workflows` for each of these, in this order, using the heading shown.

{{workflows}}

## Tools

{{tool_specs}}

## Data schema

The tools return records of these types, written out as YAML text.

{{record_schemas}}

## Conventions

- The assistant acts for one user and learns about that user's data only from tool results.
- When a request refers to a relative date (today, tomorrow, next week, a weekday), use a tool that returns the current day if one is listed above; do not assume the date.
- Use the date and time formats stated in the tool arguments' descriptions.
- A tool result can be an error or empty. Say what to report in that case instead of claiming success.

Write the SKILL.md for this skill. Return only the file.
