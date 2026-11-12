"""
Purpose:
    Build the pool of candidate "unsafe" study scenarios: attacked runs where the injection succeeded
    on a task that gpt-4o can do when not attacked. Each candidate is compared with that task's no-attack
    run so that reviewers can later check, by hand, that the legitimate request was still completed.

    Why not use AgentDojo's `utility` for the attacked run? It also fails whenever the environment
    changed unexpectedly, so every successful attack looks like a failed task (see the README in
    src/data/gpt-4o-2024-05-13/). The no-attack run is the reference for "can gpt-4o do this task".

Inputs:
    Run files in src/data/gpt-4o-2024-05-13/workspace/ for the two kept attacks (important_instructions,
    tool_knowledge): 40 user tasks x 2 attacks x 6 injection tasks = 480 runs, plus each task's none.json.

Outputs:
    src/data/gpt4o_unsafe_candidates.json with a "summary" block (counts) and a "candidates" list.
    A run is a candidate ("auto_pass") when: error is null, security is true, and the same task's
    no-attack run has error null and utility true. Each candidate records both runs' tool calls and final
    responses, the calls that look unauthorized, and review flags. Risk levels are not assigned.
    Output is deterministic (sorted by attack, task number, injection number).

Dependencies:
    agentdojo_runs.py (run parsing, injection tables, unauthorized-call matcher).

Usage:
    python3 -B src/scripts/build_unsafe_candidates.py [--out PATH]
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import agentdojo_runs as runs

DEFAULT_OUT = runs.REPO_ROOT / "src" / "data" / "gpt4o_unsafe_candidates.json"
# Characters of context kept on each side of a pattern match in the review snippet.
SNIPPET_CONTEXT = 40


def call_key(call: dict) -> str:
    """Return a canonical string for a tool call (function plus arguments) so calls can be compared."""
    return json.dumps(call, sort_keys=True, ensure_ascii=False)


def extra_calls(attacked: list[dict], reference: list[dict]) -> list[dict]:
    """Return calls in the attacked run that the no-attack run did not make (function and arguments).

    Matching is multiset-based: if the attacked run makes the same call twice and the reference once,
    one copy is extra. This is informational only; the model's phrasing of arguments (e.g. search
    queries) varies between runs, so extra calls are not all attacker-driven.
    """
    remaining = Counter(call_key(c) for c in reference)
    extra = []
    for call in attacked:
        key = call_key(call)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            extra.append(call)
    return extra


def missing_functions(attacked: list[dict], reference: list[dict]) -> list[str]:
    """Return tool names the no-attack run used more often than the attacked run.

    Compared by tool name only (not arguments), so differences in wording do not count. A non-empty
    result hints that the attacked run skipped part of the legitimate task.
    """
    shortfall = Counter(c["function"] for c in reference) - Counter(c["function"] for c in attacked)
    return sorted(shortfall.elements())


def reference_hits(texts: list[str]) -> list[str]:
    """Return short snippets of agent text that match INJECTION_REFERENCE_PATTERN, for human review."""
    snippets = []
    for text in texts:
        for match in runs.INJECTION_REFERENCE_PATTERN.finditer(text):
            start = max(0, match.start() - SNIPPET_CONTEXT)
            end = min(len(text), match.end() + SNIPPET_CONTEXT)
            snippets.append(text[start:end].replace("\n", " "))
    return snippets


def build_candidate(user_task_id: str, attack: str, injection_task_id: str, run: dict, none_run: dict) -> dict:
    """Build one candidate entry from an attacked run and the same task's no-attack run."""
    attacked = runs.tool_calls(run)
    reference = runs.tool_calls(none_run)
    unauthorized = runs.unauthorized_calls(injection_task_id, attacked)
    hits = reference_hits(runs.intermediate_agent_texts(run) + [runs.final_response(run)])
    missing = missing_functions(attacked, reference)
    prompt = runs.user_prompt(run)

    flags = []
    if hits:
        flags.append("mentions_injection_source")
    if missing:
        flags.append("missing_legit_calls")
    if not unauthorized:
        flags.append("no_unauthorized_call_detected")
    if prompt.startswith(runs.COMBINED_PREFIX):
        flags.append("combined_task")

    return {
        "run_path": f"src/data/gpt-4o-2024-05-13/workspace/{user_task_id}/{attack}/{injection_task_id}.json",
        "user_task_id": user_task_id,
        "attack_type": attack,
        "injection_task_id": injection_task_id,
        "injection_goal": runs.INJECTION_GOALS[injection_task_id],
        "user_prompt": prompt,
        "error": run["error"],
        "utility": run["utility"],
        "security": run["security"],
        "none_utility": none_run["utility"],
        "n_attacked_calls": len(attacked),
        "n_none_calls": len(reference),
        "attacked_calls": attacked,
        "none_calls": reference,
        "extra_calls": extra_calls(attacked, reference),
        "missing_calls": missing,
        "unauthorized_calls": unauthorized,
        "injection_reference_hits": hits,
        "has_intermediate_messages": bool(runs.intermediate_agent_texts(run)),
        "final_response": runs.final_response(run),
        "none_final_response": runs.final_response(none_run),
        "flags": flags,
    }


def build_output() -> dict:
    """Examine all 480 attacked runs and return the summary plus the auto-pass candidates."""
    candidates = []
    examined = 0
    attack_succeeded = Counter()
    for attack in runs.ATTACKS:
        for user_task_id in runs.list_user_task_ids():
            none_run = runs.load_run(user_task_id)
            none_ok = none_run["error"] is None and none_run["utility"] is True
            for injection_task_id in runs.INJECTION_TASK_IDS:
                run = runs.load_run(user_task_id, attack, injection_task_id)
                examined += 1
                if run["error"] is None and run["security"] is True:
                    attack_succeeded[attack] += 1
                    if none_ok:
                        candidates.append(build_candidate(user_task_id, attack, injection_task_id, run, none_run))

    summary = {
        "runs_examined": examined,
        "attack_succeeded_without_error": dict(attack_succeeded),
        "candidates": len(candidates),
        "candidates_by_attack": dict(Counter(c["attack_type"] for c in candidates)),
        "user_tasks_by_attack": {
            attack: len({c["user_task_id"] for c in candidates if c["attack_type"] == attack})
            for attack in runs.ATTACKS
        },
        "candidates_by_injection_task": dict(sorted(Counter(c["injection_task_id"] for c in candidates).items())),
        "candidates_by_flag": dict(sorted(Counter(f for c in candidates for f in c["flags"]).items())),
        "candidates_with_no_flags": sum(1 for c in candidates if not c["flags"]),
    }
    return {
        "definition": (
            "Candidate = error is null, security is true, and the same user task's no-attack run has "
            "error null and utility true. A reviewer must still confirm the legitimate request was completed."
        ),
        "source_commit": "af7266303c748a9cf0442d71ceabc9d2488d7188",
        "summary": summary,
        "candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the unsafe scenario candidate pool.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    output = build_output()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(output["summary"], indent=2))
    print(f"Wrote {output['summary']['candidates']} candidates to {args.out}")


if __name__ == "__main__":
    main()
