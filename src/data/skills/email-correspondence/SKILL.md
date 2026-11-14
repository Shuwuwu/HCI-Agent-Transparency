---
name: email-correspondence
description: Compose and send emails on the user's behalf, resolve named recipients, reply to identified senders, and delete identified emails when the user requests those actions.
---

## Overview

Handle outgoing correspondence and email deletion according to the user's instructions. Resolve recipients and target messages before acting, preserve the requested meaning, and base claims about existing contacts and messages on tool results. Send or delete only when the user's request asks for that change.

## When to Use

Use this skill when the user asks to send an email, identify a named recipient for correspondence, reply to an existing message, or delete an identified email. Include copies and attachments only when requested.

If essential details are missing or multiple contacts or messages fit, ask a focused clarification before taking the requested action.

## Tools

- `search_emails`: Find an inbox message using subject or body content through `query`, optionally restricted by `sender`. Use an empty `query` only when a broader inbox search is necessary.
- `search_contacts_by_name`: Resolve a person the user named to a contact address using `query`.
- `search_contacts_by_email`: Look up contact information for an already known address using `query` when confirmation is useful.
- `send_email`: Send requested correspondence with `recipients`, `subject`, and `body`, plus requested `cc`, `bcc`, or `attachments`. Use only when the user asks to send.
- `delete_email`: Delete an identified inbox message using `email_id`. Use only when the user asks for deletion.

## Workflows

### Compose And Send

1. Identify the intended recipients, subject, and body from the user's instructions. If a recipient is named without an address, call `search_contacts_by_name`. Clarify any essential missing content rather than inventing it.
2. Prepare `recipients`, `subject`, and `body` for `send_email`. Preserve the user's intent and requested tone. Include `cc`, `bcc`, and `attachments` only when requested and sufficiently specified.
3. Check any date-dependent details before calling `send_email`. No listed tool returns the current day; if a relative date must become a concrete date, ask the user rather than assume. Format calendar attachment times as documented.
4. Call `send_email` only because the user requested sending. Report the outcome supported by its result; if the result is an error or empty, do not claim the email was sent.

### Address Recipients

1. Call `search_contacts_by_name` with the person the user named as `query`. Use returned contact addresses rather than constructing or guessing an address.
2. If an address is already known and contact confirmation is needed, call `search_contacts_by_email`. If either contact search returns no match, an error, or ambiguous matches, explain the issue and ask for clarification.
3. Populate `recipients` for `send_email` from the resolved addresses. Resolve separately named copy recipients with `search_contacts_by_name`, assigning them to `cc` or `bcc` exactly as requested. Do not add copies implicitly.
4. Prepare `attachments` for `send_email` only when requested. File attachments require `type` and `file_id`; event attachments require `type` and `event_details` containing the documented event fields. No listed tool retrieves cloud files or calendar events, so request missing references or details instead of inventing them.
5. Call `send_email` only if the user also requested sending and all required details are available. Otherwise, report the resolved recipient information without sending.

### Reply To Sender

1. Call `search_emails` using identifying subject or body content as `query`. If the user identifies the sender by name, first call `search_contacts_by_name`, then use the resolved address as `sender`.
2. Inspect the returned messages to identify the intended email. If results are empty, erroneous, or ambiguous, report that or ask for clarification before calling `send_email`.
3. Prepare the reply for `send_email`, using the selected message's `sender` as the recipient and its subject as context. Write the requested response without automatically including other recipients or attachments.
4. Call `send_email` only when the user requested the reply be sent. This tool sends a message but has no documented threading argument; do not claim guaranteed thread placement. Report the actual outcome.

### Delete Email

1. Call `search_emails` with the user's identifying details. Resolve a named sender through `search_contacts_by_name` when needed before filtering by `sender`.
2. Inspect the returned messages and select the one matching the request. If there is no clear match, clarify rather than calling `delete_email`.
3. Only when deletion was requested, call `delete_email` with `email_id` set to the selected email's returned `id_`.
4. Confirm deletion only if the `delete_email` result supports success. Report an error or empty result without claiming completion.

## Common Pitfalls

- Do not use undocumented arguments or infer addresses from names.
- Do not mistake a contact match for authorization to send.
- Do not add copies, attachments, or reply-all recipients without instruction.
- Do not assume a failed or empty tool result means success.

## Output Requirements

Give a concise outcome: what was sent, which recipient was resolved, or which message was deleted. State unresolved details or failures plainly. Distinguish prepared correspondence from successfully sent correspondence, and avoid exposing unnecessary message content.
