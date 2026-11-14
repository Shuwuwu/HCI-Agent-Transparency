---
name: calendar-lookup
description: Answer read-only calendar questions by resolving dates, listing and counting a day's events, finding event details or the next occurrence, and calculating time between events.
---

## Overview

Use calendar records to answer questions about the user's schedule without changing anything. Learn about the user's calendar only from tool results. Resolve the requested date, retrieve relevant events, and report only supported details.

## When to Use

Use this skill for requests to interpret a relative or partial date, summarize a day's appointments, locate an event, find an upcoming occurrence of a repeated event, or calculate the interval between events or a stated time and an event.

## Tools

- `get_current_day`: Use when interpreting relative dates, resolving a missing year when necessary, or establishing the starting day for an upcoming-event search. It takes no arguments.
- `search_calendar_events`: Use to find events whose titles or descriptions match `query`. Supply `date` in YYYY-MM-DD format to restrict the search to a day; omit it or use null to search all dates.
- `get_day_calendar_events`: Use to retrieve appointments for `day`, supplied in YYYY-MM-DD format, for daily summaries, event disambiguation, or interval calculations.

## Workflows

### Resolve Dates

1. Identify whether the request supplies a complete date, a relative expression, a weekday, or a partial date. Call `get_current_day` whenever interpretation depends on the current day.
2. Use the result of `get_current_day` to resolve the requested calendar date or date range. Apply the user's context to missing components; ask for clarification when more than one interpretation remains plausible.
3. Pass resolved dates to `get_day_calendar_events` through `day`, or to `search_calendar_events` through `date`, using YYYY-MM-DD format. For a requested range, retrieve the relevant days individually as needed.
4. State the interpreted date or range in the answer. If `get_current_day` fails and the date cannot otherwise be resolved, report the limitation and request an explicit date.

### Day Overview

1. Resolve the requested day, calling `get_current_day` when it is relative or incomplete.
2. Call `get_day_calendar_events` with the resolved `day`.
3. Sort returned events by `start_time`. Use their titles, start and end times, `all_day` flags, and locations to list or summarize the schedule. Count canceled events separately from non-canceled events and make that distinction explicit.
4. Report an empty result as no events returned for that day. If `get_day_calendar_events` fails, explain that the day's schedule could not be retrieved rather than reporting an empty calendar.

### Find Event

1. Extract the title or keyword and any date constraint. Call `get_current_day` if resolving that constraint requires the current day.
2. Call `search_calendar_events` with `query` and, when applicable, the resolved `date`. Search without a date constraint only when the request does not specify one.
3. Review the returned matches. If several could satisfy the request, distinguish them by title and timing or ask which is intended. Use `get_day_calendar_events` only if the surrounding day's appointments would help identify the event.
4. Report the selected event's participants, location, start and end times, description, all-day designation, and cancellation status as relevant. Mark absent details as unavailable. Distinguish no matches from a tool error.

### Next Occurrence

1. Call `get_current_day` to establish the day from which to look forward.
2. Call `search_calendar_events` with the repeated event's title or keyword as `query`, leaving `date` unrestricted.
3. Compare returned matches by `start_time`, exclude canceled and earlier-date occurrences from the upcoming selection, and choose the earliest relevant occurrence on or after the current day. Do not infer additional occurrences from an assumed recurrence pattern.
4. Report the occurrence and its details. Because `get_current_day` supplies no current time, clarify whether a same-day occurrence has passed when that affects the answer. If none is returned, say no upcoming match was found in the results; report search errors separately.

### Time Between Events

1. Resolve the requested day, using `get_current_day` when needed, and identify the events or supplied time involved.
2. Call `get_day_calendar_events` with `day`, then identify the relevant records from the returned appointments. Ask for clarification if multiple events match.
3. Establish the calculation boundaries from the request and returned `start_time` and `end_time` values. Distinguish a gap from one event's end to another's start from a start-to-start interval.
4. Calculate and label the interval, respecting available timezone information. Report overlap rather than a positive gap when appropriate. If `get_day_calendar_events` fails, required events are absent, or all-day records lack suitable time boundaries, explain why a precise interval is unavailable.

## Common Pitfalls

- Do not assume the current date instead of calling `get_current_day`.
- Do not treat search results as proof of an entire recurring series.
- Do not confuse missing fields, empty results, and retrieval failures.
- Do not count canceled appointments as active commitments.
- Do not invent event details, timezone context, or precise times for all-day events.

## Output Requirements

Answer the requested question directly. Include the resolved date when relevant, order event lists chronologically, and clearly label counts, cancellations, and interval boundaries. State uncertainty or retrieval limitations without claiming unsupported completeness. Keep all actions read-only.
