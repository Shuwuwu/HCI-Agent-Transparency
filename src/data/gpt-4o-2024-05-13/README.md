# AgentDojo gpt-4o-2024-05-13 workspace runs

Recorded agent executions copied, unmodified, from the AgentDojo benchmark. They are the raw
material for the scenario-selection scripts in `src/scripts/`.

## Source

- Repository: https://github.com/ethz-spylab/agentdojo
- Commit: `af7266303c748a9cf0442d71ceabc9d2488d7188` ("Update GPT-4o results", 2024-06-05)
- Path: `runs/gpt-4o-2024-05-13/workspace/`
- Reference: Debenedetti et al. 2024, *AgentDojo: A Dynamic Environment to Evaluate Prompt Injection
  Attacks and Defenses for LLM Agents*, NeurIPS 37.

## What is kept here (and what is not)

Tracked in git (520 files, 40 user tasks x 13 files):

- `workspace/user_task_N/none/none.json`: the run with no attack (1 file per task).
- `workspace/user_task_N/important_instructions/injection_task_M.json`: 6 files per task.
- `workspace/user_task_N/tool_knowledge/injection_task_M.json`: 6 files per task.

Present on disk but ignored by git (see the root `.gitignore`): the other attack folders
(`direct`, `dos`, `injecagent`, ...) and the `injection_task_*` folders, which this project does not use.

The kept files are byte-for-byte identical to AgentDojo's. Verify with:

    python3 src/scripts/verify_vendored_runs.py

## Run file format (fields this project relies on)

`utility`, `security`, `error`, `injections`, and `messages` (system, user, assistant, tool).
`utility` is AgentDojo's own task-success check; for attacked runs it also fails whenever the
environment changed unexpectedly, so it is not a clean measure of whether the user's request was met.

## Licence

AgentDojo is released under the MIT licence. The licence file was added to the AgentDojo repository on
2024-06-10, after the commit above, so it is quoted here from the repository's current `LICENSE`:

```
MIT License

Copyright (c) 2024 Edoardo Debenedetti, Jie Zhang, Mislav Balunovic, Luca Beurer-Kellner, Marc Fischer, and Florian Tramèr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
