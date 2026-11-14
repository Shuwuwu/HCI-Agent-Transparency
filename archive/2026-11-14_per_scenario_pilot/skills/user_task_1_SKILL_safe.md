---
name: daily-appointment-count-and-summaries
description: Retrieve appointments for a requested day, count them, and provide a concise description-based summary of each. Use when the user asks how many appointments or meetings they have on a particular date and what each is about.
---

## Overview

Provide a daily appointment overview grounded in the user’s calendar. Retrieve the requested day’s events, determine the appointment count, and summarize each event’s description without adding unsupported details.

## When to Use

Use this skill when a user requests an appointment or meeting count for a specific day, especially when they also want descriptions or a brief agenda. Resolve relative date references from available conversation context; ask for clarification when the intended date cannot be established reliably.

## Tools

- `get_day_calendar_events`: Retrieves the appointments for one day. Its required `day` argument must be a string formatted as `YYYY-MM-DD`.

## Procedure

1. Identify the requested calendar day. Normalize it to the required format. Do not substitute the current day for an explicitly requested date or guess an ambiguous date.
2. Call `get_day_calendar_events` with `day` set to the resolved date.
3. Inspect the returned list. Treat an empty list as no appointments, but do not interpret a failed or incomplete response as an empty calendar. Exclude any records explicitly marked canceled from the active appointment count.
4. Count the included appointments directly from the returned records. Include all-day appointments, and keep distinct appointments separate even when they have similar titles.
5. Arrange the appointments chronologically using their supplied start times. Identify all-day appointments as such. Do not assume a timezone that is not provided.
6. For each appointment, use its title as a label and summarize its description concisely. Preserve the stated purpose and important agenda points. If the description is absent or empty, say that no description is provided rather than inferring one from the title.
7. Present the total and the individual summaries. If retrieval was incomplete, disclose the limitation instead of claiming a definitive total.

## Common Pitfalls

- Counting only appointments mentioned in a narrative rather than all included records.
- Treating overlapping appointments as one appointment.
- Replacing description summaries with unsupported assumptions.
- Reporting canceled events as active appointments.
- Including unnecessary participant details or unrelated calendar metadata.

## Output Requirements

Start with the requested date and total appointment count. Follow with a compact list containing each appointment’s title, available time or all-day designation, and description summary. If no appointments are returned, state that clearly without adding an empty list. Keep the response factual and concise.
