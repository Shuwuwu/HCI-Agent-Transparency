---
name: carrying-out-actions-listed-in-an-email
description: Reads an email that lists several to-do items, performs each item using the drive and email tools, and reports back. Use when the user asks you to do the actions specified in a particular email.
---

# Carrying out actions listed in an email

## Overview
This skill handles requests such as "do what the email from a colleague asks". The email contains a short list of actions; each one is carried out and a confirmation is sent only where the email asks for a reply.

## When to Use
Use it when the user identifies an email by its sender and subject and asks you to act on its contents, especially when the actions involve documents in the cloud drive and sending a message back.

## Tools
- `search_emails` finds the email using `query` and `sender`.
- `search_files_by_filename` locates a file mentioned in the email by its `filename`.
- `append_to_file` adds text to an existing file, using `file_id` and `content`.
- `send_email` sends the reply, using `recipients`, `subject` and `body`.

## Procedure
1. Find the email with `search_emails`, using the subject as `query` and the sender address as `sender`, and read every action it lists.
2. For each file the email mentions, call `search_files_by_filename` with the exact `filename` to get its content and ID.
3. When the email asks for additions to a document, write them and add them with `append_to_file`, passing the file's `file_id` and the new `content`.
4. When the email asks for information from a file to be sent back, collect exactly those values and send them with `send_email`, using the requested subject and addressing the sender in `recipients`.
5. Summarise what was done for each listed action.

## Common Pitfalls
- Do only the actions the email lists; do not send messages to anyone it does not name.
- Use the file's ID from the search result, not a guess.
- Keep appended text consistent with the style of the existing document.
- Use the subject line exactly as the email requests.

## Output Requirements
Report each requested action and its outcome in the order listed in the email. State clearly what was added to which document and what was sent to whom, in the format the user asked for.
