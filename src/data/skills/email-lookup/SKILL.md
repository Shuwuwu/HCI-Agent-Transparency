---
name: email-lookup
description: Answers questions from the user's mailbox by finding emails, extracting facts, summarizing mail, and looking up contacts. Use when the user wants mailbox information without making changes.
---

## Overview

Use mailbox and contact results to answer the user's question. Keep the workflow read-only and limit retrieval and reporting to the requested information.

## When to Use

Use for email searches, specific facts within emails, mailbox lists or summaries, relative-date references, and contact names or email addresses.

## Tools

- `search_emails`: Choose for inbox searches by subject, body keywords, or sender.
- `get_unread_emails`: Choose for unread inbox mail.
- `get_received_emails`: Choose for received inbox mail.
- `get_sent_emails`: Choose for sent mail.
- `get_draft_emails`: Choose for draft mail.
- `search_contacts_by_name`: Choose when the supplied contact identifier is a name.
- `search_contacts_by_email`: Choose when the supplied contact identifier is an email address.
- `get_current_day`: Choose to anchor relative dates and weekday references.

## Workflows

### Find Email

1. Identify the supplied sender, subject, or keywords. If only the sender's name is known, call `search_contacts_by_name` with `query` set to that name.
2. Call `search_emails` using subject or body terms in `query` and the identified address in `sender` when relevant. Use an empty `query` for a sender-only search.
3. Inspect returned `sender`, `subject`, and `body` values to identify matching emails. Return enough context to distinguish the matches.

### Extract Fact

1. Locate the relevant email with `search_emails`, resolving a named sender through `search_contacts_by_name` when needed.
2. Read the returned `body` and other relevant fields for the requested fact. Distinguish the email's `timestamp` from dates or times mentioned in its content.
3. Return the fact directly, preserving codes and links exactly. Include brief sender or subject context when needed to identify its source.

### List Mailbox

1. Select the requested category: unread, received, sent, or draft.
2. Call the corresponding tool: `get_unread_emails`, `get_received_emails`, `get_sent_emails`, or `get_draft_emails`.
3. Apply the user's requested filters to the returned records. List or summarize relevant subjects, senders or recipients, and content without broadening the category.

### Resolve Recent

1. Call `get_current_day` to anchor relative-date or weekday language.
2. Retrieve candidate emails with `search_emails` for content or sender constraints, or the relevant mailbox-listing tool for a requested category.
3. Use each email's `timestamp` and the current day to interpret the reference. Choose the most reasonable reading of ambiguous wording and state that reading in the answer.

### Find Contact

1. Determine whether the user supplied a contact name or email address.
2. Call `search_contacts_by_name` for a supplied name or `search_contacts_by_email` for a supplied address, placing the supplied identifier in `query`.
3. Return the requested `name` or `email` from matching records. If several records match, present the relevant matches rather than combining them.

## Common Pitfalls

- Use `sender` for sender filtering; `query` searches email subjects and bodies.
- Ground mailbox and contact details in tool results.
- Do not treat mentioned dates as email timestamps.
- Do not repeat a data-changing call after an uncertain result.

## Output Requirements

Answer directly with the requested facts, matches, or summary. State any interpretation used for ambiguity. For empty results, report that no matching information was found. For errors, report what could not be retrieved or established instead of claiming success or treating the error as an empty result.
