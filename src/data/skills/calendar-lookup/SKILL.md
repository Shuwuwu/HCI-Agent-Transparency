---
name: calendar-lookup
description: Answers questions about calendar events, daily schedules, event details, upcoming occurrences, and time between events without changing the calendar. Use when the user wants to look up or summarize calendar information.
---

## Overview

Answer calendar questions using returned event records. Resolve date references, retrieve matching events, and report details or calculations without changing the calendar.

## When to Use

Use for daily schedules, event counts, event details, the next occurrence of a repeated event, or gaps between events.

## Tools

- `get_current_day`: Choose when interpreting a relative or partial date requires the current day.
- `search_calendar_events`: Choose when locating events by title or description, with an optional date filter.
- `get_day_calendar_events`: Choose when listing, counting, summarizing, or comparing events for a day.

## Workflows

### Resolve Dates

1. Identify the date reference and whether it requires a current-day anchor. Call `get_current_day` for relative dates and partial dates that need that anchor.
2. Resolve the reference into an exact calendar date, or the dates covered by a requested period, using the request’s context.
3. Supply resolved dates as YYYY-MM-DD through `day` in `get_day_calendar_events` or `date` in `search_calendar_events`. When interpretation is ambiguous, choose the most reasonable reading and state it in the answer.

### Day Overview

1. Resolve the requested day, using `get_current_day` when needed.
2. Call `get_day_calendar_events` with the resolved `day`.
3. Use the returned records to list, count, or summarize events as requested. For a count, count event records and make any status-based filtering explicit.
4. Include relevant `title`, `start_time`, `end_time`, `location`, and `all_day` information. Preserve relevant distinctions in `status`.

### Find Event

1. Extract the requested title or keyword and resolve any date restriction using `get_current_day` when needed.
2. Call `search_calendar_events` with `query`, supplying `date` only when the request specifies a day.
3. Identify the matching event from the results. If several remain plausible, use the request’s context and state which interpretation you used.
4. Report the requested details from `participants`, `location`, `start_time`, `end_time`, and `description`, identifying the event by `title`.

### Next Occurrence

1. Call `get_current_day` to establish the starting day.
2. Call `search_calendar_events` with the repeated event’s title or keyword as `query`, leaving `date` unset to retrieve matches across dates.
3. Compare matching `start_time` values and select the earliest occurrence on or after the current day, respecting any status restriction in the request.
4. Report the occurrence’s date, time, and requested details. Make clear that the search looks forward from the current day.

### Time Between Events

1. Resolve the requested day, calling `get_current_day` when needed.
2. Call `get_day_calendar_events` with that `day` and identify the named events or the event paired with the supplied time.
3. Calculate the interval using the boundaries the request specifies. For an ordinary gap between events, use the earlier event’s `end_time` and the later event’s `start_time`, and state that reading.
4. Report the duration and boundaries used. Describe an overlap as an overlap, and respect `all_day` when interpreting times.

## Common Pitfalls

- Keep all actions read-only; do not repeat a data-changing call after an uncertain result.
- Do not assume search results are chronologically ordered.
- Do not omit canceled events without explaining the filtering or infer details beyond returned records.

## Output Requirements

Answer directly with the requested details, count, summary, or interval. State material date or event interpretations. For errors, report what could not be retrieved or calculated; for empty results, report that no matching events were returned rather than claiming success. Distinguish absent details from retrieval failures.
