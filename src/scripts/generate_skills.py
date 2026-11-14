"""
Purpose:
    Generate the capability skills (src/data/skills/<skill-id>/SKILL.md) by sending each skill's saved request to
    the OpenAI Responses API, validating the answer with validate_capability_skills.py and, when a check at
    error level fails, asking again with the problems appended (at most 2 repair rounds). Every API call is
    logged in full, and a manifest ties each published skill to the exact request, response and validator version
    that produced it.

    The model is fixed in the request files (gpt-6.1-sol, reasoning effort high, 16384 output tokens). OpenAI
    publishes no dated snapshot and no seed for it, so outputs cannot be reproduced bit for bit: the logs and the
    accepted files are the record. The generator is task-agnostic: it sends only what build_capability_prompts.py
    saved, which contains no scenario. Nothing is sent without a real run (no --dry-run), and a skill file that
    already has content is never overwritten without --force.

Inputs:
    src/data/skill_library/prompts/<skill-id>.json (build_capability_prompts.py): the request to send.
    The skill library and scenario material used by the validator (see validate_capability_skills.py).
    The environment variable OPENAI_API_KEY (never written to any file or log; not needed for --dry-run).

Outputs:
    src/data/skill_library/runs/<skill-id>/attempt_K.json, one per API call, with: kind (initial, repair or retry),
    request, base_prompt_sha256, definition_sha256, validator_version, requested_at, response (the full API
    response), output_text (the raw answer), skill_text (the answer after removing a wrapping code fence),
    normalisation notes, validation (the validator's result) and accepted. Attempt files are never overwritten.
    src/data/skills/<skill-id>/SKILL.md: the accepted skill (its attempt has accepted: true). If all attempts fail,
    no file is written and the skill is reported as rejected.
    src/data/skills/manifest.json: one row per published skill (SHA-256 of the file, accepted attempt, reported
    model, request and definition hashes, validator version), rebuilt from the files and logs on every run.
    src/data/skill_library/generation_summary.json: one row per skill, rebuilt from the attempt files.

Dependencies:
    validate_capability_skills.py, build_capability_prompts.py, prompt_utils.py, agentdojo_runs.py. Standard
    library only (urllib for the API call).

Usage:
    OPENAI_API_KEY=... python3 -B src/scripts/generate_skills.py --check-access
    python3 -B src/scripts/generate_skills.py --dry-run
    OPENAI_API_KEY=... python3 -B src/scripts/generate_skills.py --skills calendar-lookup email-correspondence
    OPENAI_API_KEY=... python3 -B src/scripts/generate_skills.py            (all skills; skips finished ones)
"""

import argparse
import hashlib
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
import build_capability_prompts as cp
import prompt_utils
import validate_capability_skills as validator

API_URL = "https://api.openai.com/v1/responses"
TIMEOUT_SECONDS = 300
TRANSPORT_RETRIES = 3  # extra tries after a network error or a retryable HTTP status (not counted as attempts)
BACKOFF_SECONDS = (2, 4, 8)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_REPAIRS = 2
DEFAULT_PROMPTS_DIR = cp.DEFAULT_OUT_DIR
DEFAULT_SKILLS_DIR = validator.DEFAULT_SKILLS_DIR
DEFAULT_RUNS_DIR = runs.REPO_ROOT / "src" / "data" / "skill_library" / "runs"
SKILL_FILENAME = "SKILL.md"
MANIFEST_FILENAME = "manifest.json"
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
        f"{base_request['input']}\n\n## Previous attempt\n\n{prompt_utils.fence(previous_text.rstrip())}\n\n"
        "## Problems found by automatic checks\n\n" + "\n".join(f"- {line}" for line in errors) + "\n\n"
        "Rewrite the SKILL.md so that all of these problems are fixed. Keep everything that was already correct. "
        "Return only the file."
    )
    return {**base_request, "input": feedback}


# --------------------------------------------------------------------------------------------------- one skill

def load_prompt(prompts_dir: Path, skill_id: str) -> dict:
    """Load a saved request record and check that its request still matches the recorded hash."""
    record = json.loads((prompts_dir / f"{skill_id}.json").read_text(encoding="utf-8"))
    if prompt_utils.canonical_hash(record["request"]) != record["prompt_sha256"]:
        raise GenerationError(f"{skill_id}: the request no longer matches its recorded hash; rebuild the prompts")
    return record


def next_attempt_number(skill_runs: Path) -> int:
    """Return the number after the highest existing attempt_K.json (1 if none), so attempts are never overwritten."""
    numbers = [int(re.fullmatch(r"attempt_(\d+)\.json", p.name).group(1)) for p in skill_runs.glob("attempt_*.json")]
    return max(numbers, default=0) + 1


def usage_summary(response: dict) -> dict:
    usage = response.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
    }


def run_skill(skill_id: str, client: Callable[[dict], dict], library: validator.Library, prompts_dir: Path,
              skills_dir: Path, runs_dir: Path, max_repairs: int = MAX_REPAIRS, force: bool = False) -> dict:
    """Generate, validate and (if valid) save one skill. Returns {skill_id, status, attempts, ...}.

    status is "skipped" (the skill file already has content and force is off), "accepted" or "rejected".
    An attempt that returns no text is repeated with the original request ("retry"); one that fails the
    validator is repeated with the problems appended ("repair"). At most 1 + max_repairs calls are made.
    """
    record = load_prompt(prompts_dir, skill_id)
    skill_path = skills_dir / skill_id / SKILL_FILENAME
    if skill_path.exists() and skill_path.read_text(encoding="utf-8").strip() and not force:
        return {"skill_id": skill_id, "status": "skipped", "attempts": 0, "accepted_attempt": None}

    skill_runs = runs_dir / skill_id
    skill_runs.mkdir(parents=True, exist_ok=True)
    number = next_attempt_number(skill_runs)
    request, kind = record["request"], "initial"
    result = None

    for round_index in range(1 + max_repairs):
        requested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        response = client(request)
        output_text = response_text(response)
        skill_text, notes = clean_output(output_text)
        result = validator.validate_skill(skill_text, library, skill_id) if skill_text else None
        accepted = bool(result and result["passed"])
        log = {
            "skill_id": skill_id,
            "attempt": number,
            "kind": kind,
            "template_version": record["template_version"],
            "base_prompt_sha256": record["prompt_sha256"],
            "definition_sha256": record["source"]["definition_sha256"],
            "validator_version": validator.VALIDATOR_VERSION,
            "request_sha256": prompt_utils.canonical_hash(request),
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
        (skill_runs / f"attempt_{number}.json").write_text(
            json.dumps(log, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        if accepted:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(skill_text, encoding="utf-8")
            return {"skill_id": skill_id, "status": "accepted", "attempts": round_index + 1,
                    "accepted_attempt": number, "warnings": result["n_warnings"]}
        if round_index == max_repairs:
            break
        if result is None:
            request, kind = record["request"], "retry"
        else:
            request, kind = repair_request(record["request"], skill_text, validator.error_messages(result)), "repair"
        number += 1
    return {"skill_id": skill_id, "status": "rejected", "attempts": 1 + max_repairs, "accepted_attempt": None,
            "errors": validator.error_messages(result) if result else ["no text returned"]}


# --------------------------------------------------------------------------------------------------- records

def load_attempts(skill_runs: Path) -> list[dict]:
    """Load a skill's attempt logs in attempt order."""
    files = sorted(skill_runs.glob("attempt_*.json"), key=lambda p: int(p.stem.split("_")[1]))
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def summarize(runs_dir: Path) -> dict:
    """Build the per-skill summary from the attempt files (so it always agrees with the logs)."""
    rows = []
    for skill_dir in sorted(d for d in runs_dir.glob("*") if d.is_dir()):
        attempts = load_attempts(skill_dir)
        if not attempts:
            continue
        accepted = [a["attempt"] for a in attempts if a["accepted"]]
        totals = {key: sum((a["usage"][key] or 0) for a in attempts)
                  for key in ("input_tokens", "output_tokens", "reasoning_tokens")}
        last = attempts[-1]
        rows.append({
            "skill_id": skill_dir.name,
            "attempts": len(attempts),
            "accepted_attempt": accepted[-1] if accepted else None,
            "status": "accepted" if accepted else "rejected",
            "reported_models": sorted({a["reported_model"] for a in attempts if a["reported_model"]}),
            "usage_total": totals,
            "last_attempt_errors": [] if accepted else (
                validator.error_messages(last["validation"]) if last["validation"] else ["no text returned"]),
        })
    return {"skills": rows}


def build_manifest(skills_dir: Path, runs_dir: Path) -> dict:
    """List every published skill with the facts that tie it to its request, response and validator version.

    A skill file is matched to the accepted attempt whose cleaned text is identical to it; if none matches (the
    file was edited or came from elsewhere), accepted_attempt is null and the other fields are null.
    """
    rows = []
    for path in sorted(skills_dir.glob(f"*/{SKILL_FILENAME}")):
        data = path.read_bytes()
        if not data.strip():
            continue
        text = data.decode("utf-8")
        matches = [a for a in load_attempts(runs_dir / path.parent.name) if a["accepted"] and a["skill_text"] == text]
        attempt = matches[-1] if matches else None
        rows.append({
            "skill_id": path.parent.name,
            "skill_sha256": hashlib.sha256(data).hexdigest(),
            "accepted_attempt": attempt["attempt"] if attempt else None,
            "reported_model": attempt["reported_model"] if attempt else None,
            "template_version": attempt["template_version"] if attempt else None,
            "prompt_sha256": attempt["base_prompt_sha256"] if attempt else None,
            "definition_sha256": attempt["definition_sha256"] if attempt else None,
            "validator_version": attempt["validator_version"] if attempt else None,
        })
    return {"skills": rows}


def stale_skills(manifest: dict) -> list[str]:
    """The published skills that were not accepted under the current validator version (or match no attempt)."""
    return [row["skill_id"] for row in manifest["skills"] if row["validator_version"] != validator.VALIDATOR_VERSION]


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


def write_records(skills_dir: Path, runs_dir: Path) -> None:
    """Rebuild the summary and the manifest from the files on disk and print any stale skills."""
    if runs_dir.exists() and any(runs_dir.glob("*/attempt_*.json")):
        summary_path = runs_dir.parent / SUMMARY_FILENAME
        summary_path.write_text(json.dumps(summarize(runs_dir), indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Summary written to {summary_path}")
    manifest = build_manifest(skills_dir, runs_dir)
    if manifest["skills"]:
        path = skills_dir / MANIFEST_FILENAME
        path.write_text(json.dumps(manifest, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Manifest written to {path}")
        stale = stale_skills(manifest)
        if stale:
            print(f"warning: not accepted under validator {validator.VALIDATOR_VERSION}: {', '.join(stale)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the capability skills with the OpenAI API.")
    parser.add_argument("--skills", nargs="+", help="skill ids (default: all with a prompt file)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be sent; send nothing")
    parser.add_argument("--check-access", action="store_true", help="send one tiny request to test the key and model")
    parser.add_argument("--force", action="store_true", help="regenerate even if the skill file already has content")
    parser.add_argument("--max-repairs", type=int, default=MAX_REPAIRS)
    parser.add_argument("--max-calls", type=int, default=30, help="stop after this many API calls in this run")
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR)
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.check_access:
        if not api_key:
            raise SystemExit("error: set OPENAI_API_KEY first")
        try:
            check_access(api_key, prompt_utils.GENERATION_SETTINGS["model"])
        except GenerationError as error:
            raise SystemExit(f"error: {error}")
        return

    skill_ids = args.skills or sorted(p.stem for p in args.prompts_dir.glob("*.json"))
    if args.dry_run:
        for skill_id in skill_ids:
            record = load_prompt(args.prompts_dir, skill_id)
            path = args.skills_dir / skill_id / SKILL_FILENAME
            finished = path.exists() and path.read_text(encoding="utf-8").strip()
            action = "skip (skill file has content)" if finished and not args.force else f"write {path.name}"
            print(f"{skill_id}: model {record['request']['model']}, about {record['estimated_input_tokens']} input "
                  f"tokens, request {record['prompt_sha256'][:12]}; would {action}")
        print("Dry run: nothing was sent.")
        return

    if not api_key:
        raise SystemExit("error: set OPENAI_API_KEY (or use --dry-run)")
    library = validator.load_library()
    client = make_client(api_key, args.max_calls)
    try:
        for skill_id in skill_ids:
            outcome = run_skill(skill_id, client, library, args.prompts_dir, args.skills_dir, args.runs_dir,
                                args.max_repairs, args.force)
            detail = f" at attempt {outcome['accepted_attempt']}" if outcome["status"] == "accepted" else ""
            print(f"{skill_id}: {outcome['status']}{detail} ({outcome['attempts']} calls)")
            for line in outcome.get("errors", []):
                print(f"    error {line}")
    except GenerationError as error:
        print(f"error: {redact(str(error), api_key)}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        write_records(args.skills_dir, args.runs_dir)


if __name__ == "__main__":
    main()
