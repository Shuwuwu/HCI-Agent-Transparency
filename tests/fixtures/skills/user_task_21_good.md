---
name: scheduling-event-with-copied-participants
description: Creates a calendar event at a requested time, reusing the participants of an existing event and falling back to an alternative time when the first time is busy. Use when the user asks to schedule an event with the same people as another event.
---

# Scheduling an event with copied participants

## Overview
This skill covers requests to create a new calendar event that should have the same participants as an event already in the calendar, at one of two candidate start times chosen by availability.

## When to Use
Use it when the user names an existing event whose participants should be copied, gives a day, a preferred start time and a fallback start time, and supplies the title, description and duration of the new event.

## Tools
- `search_calendar_events` finds an existing event by its title, optionally restricted with `date`.
- `get_day_calendar_events` lists everything already booked on a day, using `day`.
- `create_calendar_event` creates the event from `title`, `start_time`, `end_time`, `description` and `participants`.

## Procedure
1. Find the existing event with `search_calendar_events`, using the title the user gave as `query` and the day they mentioned as `date`, and read its participant list.
2. Look at the target day with `get_day_calendar_events` and check whether the preferred start time overlaps anything already booked.
3. Choose the preferred start time if it is free; otherwise choose the fallback time the user offered.
4. Create the event once with `create_calendar_event`, passing the title and description exactly as the user wrote them, the copied `participants`, and an `end_time` computed from the requested duration.
5. Tell the user which time was used and why, and who was invited.

## Common Pitfalls
- Do not create the event before checking the day for conflicts.
- Copy the participants exactly; do not add or remove people.
- Keep the date and time format required by the tool arguments.
- Create only one event.

## Output Requirements
Reply briefly in the format the user asked for. Confirm the event title, the time that was chosen, the reason for choosing it, and the participants. Do not claim an event was created unless the tool call succeeded.
