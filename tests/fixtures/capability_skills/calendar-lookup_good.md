---
name: calendar-lookup
description: Answers questions about the user's calendar without changing it: resolving dates, listing and counting a day's events, finding an event and reporting its details, locating the next occurrence of a repeated event, and working out the time between events. Use when the user asks what is in their calendar.
---

## Overview

This skill covers read-only questions about the user's calendar. It finds events, reads their details and summarizes what the calendar shows. It never creates, moves or removes anything.

## When to Use

Use it when the user asks how many events fall on a day, what an event is about, who is invited, where or when something takes place, when the next occurrence of a repeated event is, or how much time lies between two events.

## Tools

- `get_current_day`: returns today's date. Use it whenever a request depends on a relative date such as today, tomorrow or next week.
- `search_calendar_events`: searches events by a text query in titles and descriptions, optionally limited to one date.
- `get_day_calendar_events`: lists every event on one day, given as a date in the documented format.

## Workflows

### Resolve Dates

1. Read the request and decide whether it names an exact date or a relative one.
2. For a relative date, call `get_current_day` and compute the exact date from it.
3. For a date given without a year, use the year of the current day.
4. Write the date in the format the next tool expects before calling it.

### Day Overview

1. Resolve the day first, then call `get_day_calendar_events` with `day` set to that date.
2. Count the returned events and note each title, start and end time and description.
3. Leave out events marked as canceled when counting what the user still has.
4. Summarize each event in a sentence based only on the text the tool returned.

### Find Event

1. Call `search_calendar_events` with `query` set to the words the user used for the event; add `date` when the user named a day.
2. If several events match, compare their dates and titles with the request and keep the best match.
3. Report only the details the user asked for, such as the participants, the place, the time or the description.
4. If nothing matches, say that no such event was found.

### Next Occurrence

1. Call `get_current_day` to learn today's date.
2. Call `search_calendar_events` with `query` set to the event's name.
3. Among the results, pick the earliest event that starts after the current moment.
4. Report its start time in the format the user asked for.

### Time Between Events

1. Call `get_day_calendar_events` for the day in question.
2. Identify the two events, or the given time and the event, that the user is asking about.
3. Subtract the end of the earlier one from the start of the later one.
4. Give the result in the format the user requested.

## Common Pitfalls

- Do not assume today's date; always obtain it when a request is relative.
- Do not count canceled events as appointments.
- Do not describe an event with details that the tool result does not contain.
- Do not change the calendar; this skill only reads it.

## Output Requirements

Answer directly and briefly, in the format the user asked for. State the count, the details or the time first, then add a short list or summary if useful. Say clearly when the calendar has no matching event.
