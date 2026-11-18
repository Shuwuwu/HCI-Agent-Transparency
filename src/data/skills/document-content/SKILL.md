---
name: document-content
description: Reads, extracts, summarizes, extends, and creates documents and spreadsheets in the cloud drive. Use when the user wants file content read, specific records extracted, a summary, appended content, or a new file.
---

## Overview

Work with document and spreadsheet `content` returned in cloud drive file records. Use the user's reference to identify the target, then perform only the requested reading or change.

## When to Use

Use for reading a file, extracting selected values or records, summarizing content, appending content to an existing file, or creating a file with requested content.

## Tools

- `search_files_by_filename`: Choose when the user identifies a file by filename.
- `search_files`: Choose when the user identifies a file by its content.
- `get_file_by_id`: Choose when the file ID is supplied.
- `append_to_file`: Choose only for a requested addition to an existing file.
- `create_file`: Choose only for a requested new file.

## Workflows

### Read Document

1. Retrieve the target with `search_files_by_filename` for a filename, `search_files` for a content description, or `get_file_by_id` for a supplied ID.
2. Identify the matching file from the returned records and read its `content`. Search results already include file content; use it directly.
3. Present the requested content, identifying the source file. Keep a request to read distinct from a request to summarize.

### Extract Records

1. Retrieve the source using `search_files_by_filename`, `search_files`, or `get_file_by_id`, according to the user's reference.
2. Read `content` and select the values or records matching the user's criteria. Use the document's labels and context to interpret matches.
3. Return the extracted values with enough source context to show what each value represents. Distinguish source values from any interpretation.

### Summarize Document

1. Retrieve the source using `search_files_by_filename`, `search_files`, or `get_file_by_id`, according to the user's reference.
2. Read `content` and identify the main points relevant to the requested focus.
3. Write a source-grounded summary at the requested level of detail. Preserve qualifications and distinguish stated facts from inferences.

### Extend Document

1. Identify the target using `search_files_by_filename`, `search_files`, or `get_file_by_id`, according to the user's reference. Use the matched record's `id_` as `file_id`.
2. Prepare the requested addition as `content`, preserving wording the user supplies and drafting only what the request calls for.
3. Once the target is identified and the user has asked for the addition, call `append_to_file` once with `file_id` and `content`.
4. Report what the result says about the append operation and identify the affected file.

### Create Document

1. Determine `filename` and prepare `content` from the user's request. If the filename is unspecified, choose one that reasonably describes the requested content.
2. Once the intended new file is identified and the user has asked for its creation, call `create_file` once with `filename` and `content`.
3. Report what the result says about creation, including returned file details relevant to locating it.

## Common Pitfalls

- Do not repeat a data-changing call after an uncertain result.
- Do not treat a matching filename as proof that every returned record is the target.
- When ambiguous, choose the most reasonable reading and state that reading in the answer.
- Keep appended content separate from existing content; do not submit the entire document as the addition.

## Output Requirements

Identify the source or affected file and provide the requested content, extraction, summary, or change result. If a tool returns an error or empty result, report that outcome and its effect on completing the request instead of claiming success.
