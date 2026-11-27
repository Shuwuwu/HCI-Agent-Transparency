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
- A date in a request can be relative (today, tomorrow, next week, a weekday) or incomplete (a day and month without a year). Resolve both against the current day from a tool that returns it, if one is listed above, before using the date in a tool argument or an answer. Take a missing year from the current day, choosing the nearest matching date in the direction the request implies. Never supply a year that no tool result gave; this also applies to dates written into new content.
- Use the date and time formats stated in the tool arguments' descriptions.
- A search tool whose description says it finds records that contain the query matches only records containing the query text as written. Search with one or two distinctive words rather than a phrase copied from the request. When a search returns no match or an error, search again with fewer or different words, or without an optional filter such as a date, before reporting that nothing was found.
- When the request asks for a value in a stated format, the answer gives that value in exactly that format. Any explanation comes in addition to the formatted value, not instead of it.
- A tool result can be an error or empty. Say what to report in that case instead of claiming success.

Write the SKILL.md for this skill. Return only the file.
