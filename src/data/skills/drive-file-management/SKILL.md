---
name: drive-file-management
description: Inventory and find cloud-drive files, report their properties, and delete or share identified files when requested. Use when the user asks to inspect, locate, delete, or share files in their cloud drive.
---

## Overview

Use cloud-drive records to answer questions about files and carry out requested deletion or sharing. Ground file identities, properties, and action outcomes in tool results. Change the user’s data only when their request asks for that change.

## When to Use

Use this skill for file inventories; questions about size, ownership, sharing, or modification; filename and content searches; and requests to delete or share an identified file.

Interpret ambiguous wording reasonably and state the interpretation used. Do not ask follow-up questions. If essential details cannot be established, report what remains unresolved rather than claiming an action occurred.

## Tools

- `list_files`: Retrieve all files for inventories, property comparisons, or locating candidates across the drive.
- `search_files`: Find files by content, using `query` for the requested keyword or phrase.
- `search_files_by_filename`: Find candidate files by name, using `filename`.
- `get_file_by_id`: Retrieve a specific file using `file_id` to inspect its identity and properties.
- `delete_file`: Delete a file only when the user requests deletion. Its `file_id` argument takes the filename, as documented.
- `share_file`: Share a file only when the user requests sharing. Supply its identifier through `file_id`, the recipient through `email`, and the access level through `permission`.

## Workflows

### File Inventory

1. Call `list_files` to retrieve the drive inventory. If it returns an error, report that the inventory could not be retrieved; if it is empty, report that no files were returned.
2. Examine each relevant record’s `filename`, `size`, `owner`, `shared_with`, and `last_modified`. Call `get_file_by_id` with `file_id` taken from `id_` when a particular file needs closer inspection.
3. Apply the user’s requested filtering, sorting, or aggregation to returned records. Treat `size` as bytes and distinguish owners from sharing recipients. Preserve timestamp context and do not invent a current-day anchor for relative-date filtering.
4. Present the requested inventory or property answer. Base totals and comparisons on the records retrieved, and identify any retrieval errors that limit the answer.

### Find File

1. Determine whether the request supplies a filename, content keywords, or a file identifier. Call `search_files_by_filename` with `filename` for a name, `search_files` with `query` for content, or `get_file_by_id` with `file_id` for an identifier.
2. Inspect returned candidates against the user’s constraints. When both name and content matter, use `search_files_by_filename` and `search_files` as appropriate and compare the returned records.
3. Call `get_file_by_id` for a promising candidate when additional inspection is needed. Resolve ambiguity using the stated constraints and returned properties, and disclose the reading used.
4. Report matching files with distinguishing properties. For an empty result, say no matching files were returned; for an error, report that the search failed rather than concluding that the file does not exist.

### Delete File

1. Establish that the user requested deletion, then locate the intended file with `search_files_by_filename`, `search_files`, or `get_file_by_id`, according to the identifying information supplied.
2. Inspect the candidate’s filename and relevant properties. Call `get_file_by_id` when needed to verify the target. Use the most reasonable interpretation of ambiguous wording, but proceed only when the target is reliably identified.
3. Only for the deletion the user requested, call `delete_file`. Pass the verified filename through `file_id`; this argument is documented as accepting the filename, not the record’s identifier.
4. Report deletion only if the result confirms success. If lookup fails, identity remains unresolved, or deletion returns an error or inconclusive result, describe that outcome without claiming successful deletion.

### Share File

1. Establish that the user requested sharing. Locate the file with `search_files_by_filename`, `search_files`, or `get_file_by_id`, using the identifying details supplied.
2. Inspect the candidate and, when needed, call `get_file_by_id` to verify its identity and existing `shared_with` entries. Determine the recipient and requested access from the request and available results.
3. Only for the sharing the user requested, call `share_file` with the record’s `id_` as `file_id`, the recipient as `email`, and the requested access as `permission`. Use `r` for read access and `rw` for read/write access. If essential details remain unresolved, report that sharing was not performed.
4. Report the file, recipient, and permission granted only when the tool confirms success. Describe errors or inconclusive results accurately.

## Common Pitfalls

- Treating content search as filename search.
- Confusing the deletion tool’s filename argument with the sharing tool’s identifier argument.
- Selecting the first search result without comparing identifying details.
- Inferring public access from an empty `shared_with` mapping.
- Claiming success from empty, failed, or inconclusive results.

## Output Requirements

Answer the user’s request directly. Include relevant filenames and properties, distinguish findings from completed changes, and state any interpretation used. Report errors, unresolved details, and empty results plainly. Do not invent file properties or action outcomes.
