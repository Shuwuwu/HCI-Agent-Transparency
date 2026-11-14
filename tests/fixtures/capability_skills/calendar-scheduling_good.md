---
name: calendar-scheduling
description: Changes the user's calendar when asked: checking whether a time is free, creating events, reusing or adding participants, rescheduling and canceling. Use when the user asks to schedule, move, cancel or update an event.
---

## Overview

This skill covers requests that change the user's calendar. It first checks what is already booked, then makes only the change the user asked for and reports exactly what was done.

## When to Use

Use it when the user wants to create an event, add people to an event, move an event to another time, cancel an event, or find out whether a time slot is free before booking it.

## Tools

- `get_current_day`: returns today's date, needed for relative dates.
- `search_calendar_events`: finds existing events by text, optionally on one date.
- `get_day_calendar_events`: lists the events of one day, used to check availability.
- `create_calendar_event`: creates a new event. It changes the user's data.
- `add_calendar_event_participants`: adds people to an existing event. It changes the user's data.
- `reschedule_calendar_event`: moves an existing event. It changes the user's data.
- `cancel_calendar_event`: cancels an existing event. It changes the user's data.

## Workflows

### Check Availability

1. Resolve the requested day, using `get_current_day` when the request is relative.
2. Call `get_day_calendar_events` for that day.
3. Compare the requested start and end with every returned event; the slot is free only if nothing overlaps.
4. If the user named an alternative slot for the case that the first is busy, run the same comparison for it.

### Create Event

1. Check availability first when the user made the booking depend on it.
2. Collect the title, start time, end time and, if given, the description, participants and location.
3. Call `create_calendar_event` only when the user asked for the event to be created, using the documented time format.
4. Report the title, time and participants of the event that was created.

### Copy Participants

1. Find the existing event with `search_calendar_events`, using the words the user gave as `query` and the date if known.
2. Read its participant list from the result.
3. Pass that list as `participants` when you create the new event, only if the user asked for the same people.
4. Do not add or remove anyone the user did not mention.

### Add Participants

1. Find the event with `search_calendar_events` and note its `event_id`.
2. Gather the addresses the user wants added.
3. Call `add_calendar_event_participants` with `event_id` and `participants`, only because the user asked for these people to be added.
4. Report who was added.

### Reschedule Event

1. Find the event with `search_calendar_events` and note its `event_id`.
2. Work out the new start time, and the new end time if the user gave one.
3. Call `reschedule_calendar_event` with `event_id`, `new_start_time` and `new_end_time`, only when the user asked for the move.
4. Report the old and the new time.

### Cancel Event

1. Find the event with `search_calendar_events` and confirm that it is the one the user means.
2. Call `cancel_calendar_event` with its `event_id`, only when the user asked for it to be canceled.
3. Report which event was canceled.

## Common Pitfalls

- Do not create, move or cancel anything the user did not ask for.
- Do not skip the availability check when the request depends on it.
- Do not guess an event when several match; compare dates and titles first.
- Do not claim a change was made unless the tool result confirms it.

## Output Requirements

Report briefly what was checked and what was changed, in the format the user asked for. Name the event, the time and the people involved, and say clearly when the requested slot was busy or when no matching event was found.
