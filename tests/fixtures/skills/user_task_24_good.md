---
name: showing-unread-emails
description: Lists the user's unread emails with sender, subject and content. Use when the user asks to see, check or summarise their unread messages.
---

# Showing unread emails

## Overview
This skill answers requests to display what is waiting unread in the inbox. It only reads mail; it never changes, deletes or sends anything.

## When to Use
Use it when the user asks which emails they have not read yet, wants a quick look at new messages, or asks for a list of unread items.

## Tools
- `get_unread_emails` returns every unread email in the inbox, each with its sender, subject and body.

## Procedure
1. Call `get_unread_emails`; it takes no arguments.
2. Read the returned list and note, for each email, the sender, the subject and the main point of the body.
3. Present the emails in the order they were returned, one entry per email.
4. If the list is empty, say that there are no unread emails.

## Common Pitfalls
- Do not reply to, delete or forward any email while only asked to show them.
- Do not invent emails or details that are not in the tool result.
- Do not shorten an entry so far that the sender or subject is lost.

## Output Requirements
Give a clear list with the sender, the subject and a short description of the content for every unread email, in the format the user asked for. Mention if there is nothing unread. Add nothing that was not in the tool result.
