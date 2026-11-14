"""
Status:
    Part of the one-skill-per-scenario pipeline, which has been superseded by a library of capability skills
    (see README.md and archive/2026-11-14_per_scenario_pilot/README.md). Kept for now because its transport,
    repair and logging code is the starting point of the generator for the capability skills; removed once
    the capability-skill pipeline replaces it.

Purpose:
    Generate the legitimate skill (SKILL_safe.md) of each scenario by sending the saved 6a request to the OpenAI
    Responses API, validating the answer with validate_skills.py, and, when a check at error level fails, asking
    again with the problems appended (at most 2 repair rounds). Every API call is logged in full, so the
    published skill files can be traced back to the exact request and response that produced them.

    The model is fixed in the request files (gpt-6.1-sol, reasoning effort high, 16384 output tokens). OpenAI
    publishes no dated snapshot and no seed for it, so outputs cannot be reproduced bit for bit: the logs and
    the accepted files are the record. The script sends nothing unless it is run without --dry-run, and a
    skill file that already has content is never overwritten without --force.

Inputs:
    src/data/skill_generation/prompts/user_task_N.json (build_skill_prompts.py): the request to send.
    src/data/skill_context/ and the experiment folders, through validate_skills.py.
    The environment variable OPENAI_API_KEY (never written to any file or log; not needed for --dry-run).

Outputs:
    src/data/skill_generation/runs/user_task_N/attempt_K.json, one per API call, with: kind (initial, repair or
    retry), request, base_prompt_sha256, requested_at, response (the full API response), output_text (the raw
    answer), skill_text (the answer after removing a wrapping code fence), normalisation notes, validation (the
    validate_skills.py result) and accepted. Attempt files are never overwritten.
    src/data/experiment/user_task_N/SKILL_safe.md: the accepted skill (its attempt has accepted: true). If all
    attempts fail the placeholder stays empty and the task is reported as rejected.
    src/data/skill_generation/generation_summary.json: one row per task, rebuilt from the attempt files.

Dependencies:
    validate_skills.py, build_skill_prompts.py (request hash and fence), agentdojo_runs.py, and the output of
    build_experiment_stimuli.py. Standard library only (urllib for the API call).

Usage:
    OPENAI_API_KEY=... python3 -B src/scripts/generate_safe_skills.py --check-access
    python3 -B src/scripts/generate_safe_skills.py --dry-run
    OPENAI_API_KEY=... python3 -B src/scripts/generate_safe_skills.py --tasks user_task_1 user_task_24 user_task_13
    OPENAI_API_KEY=... python3 -B src/scripts/generate_safe_skills.py            (all tasks; skips finished ones)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import agentdojo_runs as runs
import build_experiment_stimuli as stimuli
import build_skill_prompts as prompts
import validate_skills as validator

API_URL = "https://api.openai.com/v1/responses"
TIMEOUT_SECONDS = 300
TRANSPORT_RETRIES = 3  # extra tries after a network error or a retryable HTTP status (not counted as attempts)
BACKOFF_SECONDS = (2, 4, 8)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_REPAIRS = 2
DEFAULT_RUNS_DIR = runs.REPO_ROOT / "src" / "data" / "skill_generation" / "runs"
SKILL_FILENAME = "SKILL_safe.md"
SUMMARY_FILENAME = "generation_summary.json"


class GenerationError(Exception):
    """A problem that stops the run (bad key, unknown model, edited prompt file, call budget reached, ...)."""


# --------------------------------------------------------------------------------------------------- API call

def redact(text: str, api_key: str | None) -> str:
    """Remove the API key from text that is about to be shown or stored (keys under 8 characters are not real keys)."""
    return text.replace(api_key, "[API KEY]") if api_key and len(api_key) >= 8 else text


def post_json(url: str, body: dict, api_key: str, timeout: int = TIMEOUT_SECONDS, retries: int = TRANSPORT_RETRIES,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """POST a JSON body with a bearer token and return the parsed JSON response.

    Network errors and the statuses in RETRYABLE_STATUS are tried again up to `retries` times with an increasing
    pause; any other HTTP error stops at once.

    Raises:
        GenerationError: on a non-retryable error, or when the retries are used up. The message never contains the key.
    """
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return json.loads(reply.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = redact(error.read().decode("utf-8", errors="replace")[:500], api_key)
            last_error = f"HTTP {error.code}: {detail}"
            if error.code not in RETRYABLE_STATUS:
                raise GenerationError(last_error) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = redact(f"network error: {error}", api_key)
        if attempt < retries:
            sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])
    raise GenerationError(f"giving up after {retries + 1} tries; last error: {last_error}")


def response_text(response: dict) -> str:
    """Return the text of a Responses API reply: all output_text parts of its message items, joined."""
    parts = [
        content.get("text", "")
        for item in response.get("output", []) if item.get("type") == "message"
        for content in item.get("content", []) if content.get("type") == "output_text"
    ]
    return "".join(parts)


def clean_output(text: str) -> tuple[str, list[str]]:
    """Normalise the model's answer into a skill file. Returns (skill text, notes about what was changed).

    Only two changes are made: surrounding whitespace is removed (a final newline is added), and a single code
    fence wrapped around the whole answer is removed. Nothing else is touched, so the validator sees what the model wrote.
    """
    notes = []
    stripped = text.strip()
    match = re.fullmatch(r"(`{3,}|~{3,})[A-Za-z]*\n(.*)\n\1", stripped, flags=re.S)
    if match:
        stripped = match.group(2).strip()
        notes.append("removed a code fence around the whole answer")
    return (stripped + "\n" if stripped else ""), notes


def repair_request(base_request: dict, previous_text: str, errors: list[str]) -> dict:
    """Build the request for a repair round: the original request plus the failed attempt and its problems."""
    feedback = (
        f"{base_request['input']}\n\n## Previous attempt\n\n{prompts.fence(previous_text.rstrip())}\n\n"
        "## Problems found by automatic checks\n\n" + "\n".join(f"- {line}" for line in errors) + "\n\n"
        "Rewrite the SKILL.md so that all of these problems are fixed. Keep everything that was already correct. "
        "Return only the file."
    )
    return {**base_request, "input": feedback}


# --------------------------------------------------------------------------------------------------- one task

def load_prompt(prompts_dir: Path, task_id: str) -> dict:
    """Load a saved request record and check that its request still matches the recorded hash."""
    record = json.loads((prompts_dir / f"{task_id}.json").read_text(encoding="utf-8"))
    if prompts.canonical_hash(record["request"]) != record["prompt_sha256"]:
        raise GenerationError(f"{task_id}: the request no longer matches its recorded hash; rebuild the prompts")
    return record


def next_attempt_number(task_runs: Path) -> int:
    """Return the number after the highest existing attempt_K.json (1 if none), so attempts are never overwritten."""
    numbers = [int(re.fullmatch(r"attempt_(\d+)\.json", p.name).group(1)) for p in task_runs.glob("attempt_*.json")]
    return max(numbers, default=0) + 1


def usage_summary(response: dict) -> dict:
    usage = response.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
    }


def run_task(task_id: str, client: Callable[[dict], dict], prompts_dir: Path, experiment_dir: Path, runs_dir: Path,
             max_repairs: int = MAX_REPAIRS, force: bool = False) -> dict:
    """Generate, validate and (if valid) save the skill of one task. Returns {user_task_id, status, attempts, ...}.

    status is "skipped" (the skill file already has content and force is off), "accepted" or "rejected".
    An attempt that returns no text is repeated with the original request ("retry"); one that fails the
    validator is repeated with the problems appended ("repair"). At most 1 + max_repairs calls are made.
    """
    record = load_prompt(prompts_dir, task_id)
    skill_path = experiment_dir / task_id / SKILL_FILENAME
    if not skill_path.parent.exists():
        raise GenerationError(f"{skill_path.parent} does not exist; run build_experiment_stimuli.py")
    if skill_path.exists() and skill_path.read_text(encoding="utf-8").strip() and not force:
        return {"user_task_id": task_id, "status": "skipped", "attempts": 0, "accepted_attempt": None}

    scenario = validator.load_scenario(task_id, prompts_dir=prompts_dir)
    task_runs = runs_dir / task_id
    task_runs.mkdir(parents=True, exist_ok=True)
    number = next_attempt_number(task_runs)
    request, kind = record["request"], "initial"

    for round_index in range(1 + max_repairs):
        requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        response = client(request)
        output_text = response_text(response)
        skill_text, notes = clean_output(output_text)
        result = validator.validate_skill(skill_text, scenario) if skill_text else None
        accepted = bool(result and result["passed"])
        log = {
            "user_task_id": task_id,
            "attempt": number,
            "kind": kind,
            "template_version": record["template_version"],
            "base_prompt_sha256": record["prompt_sha256"],
            "request_sha256": prompts.canonical_hash(request),
            "requested_at": requested_at,
            "request": request,
            "response": response,
            "response_status": response.get("status"),
            "reported_model": response.get("model"),
            "usage": usage_summary(response),
            "output_text": output_text,
            "skill_text": skill_text,
            "normalisation": notes,
            "validation": result,
            "accepted": accepted,
        }
        (task_runs / f"attempt_{number}.json").write_text(
            json.dumps(log, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        if accepted:
            skill_path.write_text(skill_text, encoding="utf-8")
            return {"user_task_id": task_id, "status": "accepted", "attempts": round_index + 1,
                    "accepted_attempt": number, "warnings": result["n_warnings"]}
        if round_index == max_repairs:
            break
        if result is None:
            request, kind = record["request"], "retry"
        else:
            request, kind = repair_request(record["request"], skill_text, validator.error_messages(result)), "repair"
        number += 1
    return {"user_task_id": task_id, "status": "rejected", "attempts": 1 + max_repairs, "accepted_attempt": None,
            "errors": validator.error_messages(result) if result else ["no text returned"]}


def summarize(runs_dir: Path) -> dict:
    """Build the per-task summary from the attempt files (so it always agrees with the logs)."""
    rows = []
    for task_dir in sorted((d for d in runs_dir.glob("user_task_*") if d.is_dir()),
                           key=lambda d: runs.task_number(d.name)):
        attempts = [json.loads(p.read_text(encoding="utf-8"))
                    for p in sorted(task_dir.glob("attempt_*.json"), key=lambda p: int(p.stem.split("_")[1]))]
        accepted = [a["attempt"] for a in attempts if a["accepted"]]
        totals = {key: sum((a["usage"][key] or 0) for a in attempts) for key in ("input_tokens", "output_tokens",
                                                                                "reasoning_tokens")}
        last = attempts[-1]
        rows.append({
            "user_task_id": task_dir.name,
            "attempts": len(attempts),
            "accepted_attempt": accepted[-1] if accepted else None,
            "status": "accepted" if accepted else "rejected",
            "reported_models": sorted({a["reported_model"] for a in attempts if a["reported_model"]}),
            "usage_total": totals,
            "last_attempt_errors": [] if accepted else (
                validator.error_messages(last["validation"]) if last["validation"] else ["no text returned"]),
        })
    return {"tasks": rows}


# --------------------------------------------------------------------------------------------------- CLI

def make_client(api_key: str, max_calls: int) -> Callable[[dict], dict]:
    """Return a function that sends a request to the API, refusing to exceed max_calls calls in this run."""
    calls = {"n": 0}

    def client(request: dict) -> dict:
        if calls["n"] >= max_calls:
            raise GenerationError(f"call budget of {max_calls} reached; raise --max-calls to continue")
        calls["n"] += 1
        return post_json(API_URL, request, api_key)

    return client


def check_access(api_key: str, model: str) -> None:
    """Send one tiny request to confirm the key can use the model, and print the model name the API reports."""
    request = {"model": model, "input": "Reply with the single word OK.", "reasoning": {"effort": "low"},
               "max_output_tokens": 256}
    response = post_json(API_URL, request, api_key)
    print(f"Access OK. Requested model: {model}. Reported model: {response.get('model')}. "
          f"Status: {response.get('status')}. Usage: {usage_summary(response)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the legitimate SKILL.md files with the OpenAI API.")
    parser.add_argument("--tasks", nargs="+", help="task ids such as user_task_1 (default: all with a prompt file)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    parser.add_argument("--check-access", action="store_true", help="send one tiny request to test the key and model")
    parser.add_argument("--force", action="store_true", help="regenerate even if the skill file already has content")
    parser.add_argument("--max-repairs", type=int, default=MAX_REPAIRS)
    parser.add_argument("--max-calls", type=int, default=80, help="stop after this many API calls in this run")
    parser.add_argument("--prompts-dir", type=Path, default=prompts.DEFAULT_OUT_DIR)
    parser.add_argument("--experiment-dir", type=Path, default=stimuli.DEFAULT_OUT_DIR)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.check_access:
        if not api_key:
            raise SystemExit("error: set OPENAI_API_KEY first")
        try:
            check_access(api_key, prompts.GENERATION_SETTINGS["model"])
        except GenerationError as error:
            raise SystemExit(f"error: {error}")
        return

    tasks = args.tasks or sorted((p.stem for p in args.prompts_dir.glob("user_task_*.json")), key=runs.task_number)
    if args.dry_run:
        for task_id in tasks:
            record = load_prompt(args.prompts_dir, task_id)
            path = args.experiment_dir / task_id / SKILL_FILENAME
            finished = path.exists() and path.read_text(encoding="utf-8").strip()
            action = "skip (skill file has content)" if finished and not args.force else f"write {path.name}"
            print(f"{task_id}: model {record['request']['model']}, about {record['estimated_input_tokens']} input "
                  f"tokens, request {record['prompt_sha256'][:12]}; would {action}")
        print("Dry run: nothing was sent.")
        return

    if not api_key:
        raise SystemExit("error: set OPENAI_API_KEY (or use --dry-run)")
    client = make_client(api_key, args.max_calls)
    try:
        for task_id in tasks:
            outcome = run_task(task_id, client, args.prompts_dir, args.experiment_dir, args.runs_dir,
                               args.max_repairs, args.force)
            detail = f" at attempt {outcome['accepted_attempt']}" if outcome["status"] == "accepted" else ""
            print(f"{task_id}: {outcome['status']}{detail} ({outcome['attempts']} calls)")
            for line in outcome.get("errors", []):
                print(f"    error {line}")
    except GenerationError as error:
        print(f"error: {redact(str(error), api_key)}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        if args.runs_dir.exists() and any(args.runs_dir.glob("user_task_*/attempt_*.json")):
            summary_path = args.runs_dir.parent / SUMMARY_FILENAME
            summary_path.write_text(
                json.dumps(summarize(args.runs_dir), indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Summary written to {summary_path}")


if __name__ == "__main__":
    main()
