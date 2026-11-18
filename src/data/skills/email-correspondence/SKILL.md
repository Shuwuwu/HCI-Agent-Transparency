---
name: email-correspondence
description: Sends emails on the user's behalf, resolves named recipients, replies to senders, and deletes identified emails. Use when the user asks to send, reply to, or delete an email.
---

## Overview

Write and send requested emails, resolve recipients, reply to an identified email's sender, and delete identified emails. Use the user's instructions for content and tool results for the user's data.

## When to Use

Use for requests to send an email, address it to a named person, reply to an existing email, or delete an email. Include copies and attachments only when requested.

## Tools

- `search_emails`: Choose to find the email being answered or deleted.
- `search_contacts_by_name`: Choose to resolve a named recipient or sender to an address.
- `search_contacts_by_email`: Choose when a supplied address needs matching to a contact.
- `send_email`: Choose when the user asks to send an email or reply.
- `delete_email`: Choose when the user asks to delete an identified email.

## Workflows

### Compose And Send

1. Extract the requested recipients, subject, and body. Preserve specified wording and use the user's intent when composing unspecified wording.
2. Follow Address Recipients, using `search_contacts_by_name` where needed, and prepare any requested copies or attachments.
3. Once the recipients are identified and the user has asked for sending, call `send_email` once with the prepared message.
4. Report the outcome returned by `send_email`, identifying the recipients and subject.

### Address Recipients

1. Use supplied addresses directly. When the user names a person instead, call `search_contacts_by_name` with that name as `query` and select the matching contact's `email`.
2. Resolve the requested recipients and populate `cc` and `bcc` only when requested. Use the returned contact details to distinguish matches.
3. Prepare attachments only when requested. For file attachments, use `type` set to `file` with `file_id`; for event attachments, use `type` set to `event` with `event_details`. Base these values on supplied details or tool results.
4. Pass the prepared values to the sending workflow's `send_email` call; do not send a separate email during recipient preparation.

### Reply To Sender

1. If identifying the target requires a named sender's address, resolve it with `search_contacts_by_name`.
2. Call `search_emails` with a `query` based on the user's description and, when appropriate, the known `sender`. Identify the matching email using its returned details.
3. Address the reply to the matched email's `sender`. Prepare the subject and body from the requested reply and email context; follow Address Recipients for requested copies or attachments.
4. Once the target email and reply recipient are identified and the user has asked for the reply, call `send_email` once. Report the returned outcome.

### Delete Email

1. Call `search_emails` using the user's identifying details. If a sender constraint requires resolving a name, call `search_contacts_by_name` first.
2. Identify the requested email from the returned records and take its `id_`.
3. Once the target is identified and the user has asked for deletion, call `delete_email` once with that value as `email_id`. Report what the result says.

## Common Pitfalls

- `search_emails` searches subject and body; use `sender` for a sender constraint.
- Do not automatically copy the original email's recipients or attachments into a reply.
- Do not invent addresses, attachment references, or dates.
- Do not repeat a data-changing call after an uncertain result.

## Output Requirements

State completed actions only when supported by tool results. For errors or empty results, report what failed or could not be found instead of claiming success. If a request is ambiguous, use the most reasonable reading and state that reading in the answer.
