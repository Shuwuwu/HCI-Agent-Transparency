"""
Purpose:
    Execute the runs of an execution plan (build_execution_plan.py) in AgentDojo's workspace suite, at the pinned
    commit, and store one record per run. A run is one agent attempt at one user task, with or without skills, and
    AgentDojo itself decides whether the task was done (its `utility` check on the final answer and the environment).

    The runner is built so that more scenarios, models and repetitions need only a bigger plan:
      * runs are independent and identified by run id; a run that already has a record is skipped, and a run that
        failed is NOT repeated (no automatic retries; use --force on purpose);
      * a budget stops it: at most --max-runs runs per invocation (default 2), at most --max-llm-calls model calls
        per run, and no run starts if the plan holds more runs than the plan's own limit;
      * AgentDojo is reached through a small adapter, so the logic here is tested without AgentDojo, an API key
        or the network.

    Run it with the Python of the execution environment (see setup_execution_env.py), which has AgentDojo installed.

Inputs:
    src/data/execution/plan.json; the environment variable OPENAI_API_KEY (read by the OpenAI client; never written
    anywhere); the installed `function_calling_pi` package (AgentDojo at the pinned commit).

Outputs:
    src/data/execution/runs/<run_id>.json, one per run: the run's specification, the exact system message the agent
    saw (and its hash), AgentDojo's trace in the same format as the recorded runs, utility, duration, every model
    call (requested and reported model, token usage), the status and any error (key redacted). Also, with
    --check-access, src/data/execution/model_access.json.

Dependencies:
    build_execution_plan.py (system message text), generate_skills.py (HTTP helper and key redaction), agentdojo_runs.py.
    AgentDojo and the openai package only inside AgentDojoAdapter (imported when a real run starts).

Usage:
    .venv-agentdojo/bin/python -B src/scripts/run_executions.py --check-access gpt-4o-2024-05-13
    .venv-agentdojo/bin/python -B src/scripts/run_executions.py [--plan PATH] [--max-runs 2] [--max-llm-calls 20]
        [--dry-run] [--only RUN_ID ...] [--force]
    .venv-agentdojo/bin/python -B src/scripts/run_executions.py --ground-truth-check user_task_1   (no API call)
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

import agentdojo_runs as runs
import build_execution_plan as bp
import generate_skills as gs

EXECUTION_DIR = runs.REPO_ROOT / "src" / "data" / "execution"
RECORD_VERSION = "execution_record_v1"
DEFAULT_MAX_LLM_CALLS = 20  # the tool loop allows 15 rounds; one more per AgentDojo's own re-asks
CHAT_URL = "https://api.openai.com/v1/chat/completions"


class CallBudgetExceeded(RuntimeError):
    """Raised inside a run when the model-call cap of that run is reached."""


class Adapter(Protocol):
    """What the runner needs from AgentDojo. The real one is AgentDojoAdapter; tests use a fake."""

    def default_system_message(self) -> str: ...

    def run(self, spec: dict, system_message: str, max_llm_calls: int) -> dict:
        """Execute one run and return {"messages", "utility", "duration", "llm_calls", "error"}; may raise."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / f"{run_id}.json"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def usage_totals(llm_calls: list[dict]) -> dict:
    """Sum the token counts of the calls (calls without usage count as zero)."""
    totals = {"calls": len(llm_calls), "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for call in llm_calls:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] += (call.get("usage") or {}).get(key) or 0
    return totals


def pending_runs(plan: dict, runs_dir: Path, only: list[str] | None = None, force: bool = False) -> list[dict]:
    """The runs of the plan that have no record yet (all of the chosen ones with force)."""
    chosen = [r for r in plan["runs"] if not only or r["run_id"] in only]
    unknown = set(only or []) - {r["run_id"] for r in plan["runs"]}
    if unknown:
        raise ValueError(f"run ids not in the plan: {sorted(unknown)}")
    return [r for r in chosen if force or not record_path(runs_dir, r["run_id"]).exists()]


def build_system_message(spec: dict, default_message: str, skills_dir: Path | None = None) -> str:
    """The system message of a run: the default message plus the skills of the spec, read from the files whose
    hashes the plan recorded. A skill file that changed since the plan was built is an error."""
    base = skills_dir or runs.REPO_ROOT
    skills = []
    for entry in spec["skills"]:
        path = base / entry["file"]
        text = path.read_text(encoding="utf-8")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"{entry['file']} changed since the plan was built; rebuild the plan")
        skills.append((entry["id"], text))
    return bp.system_message(default_message, skills)


def execute_run(spec: dict, adapter: Adapter, runs_dir: Path, max_llm_calls: int, api_key: str | None = None,
                skills_dir: Path | None = None, force: bool = False) -> dict:
    """Execute one run, write its record and return it. An error inside the run is recorded, not raised, and the run
    is not repeated. Never overwrites an existing record unless force is set."""
    path = record_path(runs_dir, spec["run_id"])
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; a finished run is never repeated without --force")
    system_message = build_system_message(spec, adapter.default_system_message(), skills_dir)
    record = {
        "record_version": RECORD_VERSION, "run_id": spec["run_id"], "scenario": spec["scenario"],
        "condition": spec["condition"], "model": spec["model"], "repetition": spec["repetition"],
        "skills": spec["skills"], "task_prompt": spec["task_prompt"],
        "system_message": system_message, "system_message_sha256": sha256_text(system_message),
        "agentdojo_commit": bp.PINNED_COMMIT, "max_llm_calls": max_llm_calls, "started_at": utc_now(),
    }
    try:
        result = adapter.run(spec, system_message, max_llm_calls)
        record.update(status="completed" if not result.get("error") else "error", error=result.get("error"),
                      utility=result["utility"], duration_seconds=result["duration"], trace=result["messages"],
                      llm_calls=result["llm_calls"])
    except Exception as error:  # a failed run is a result: record it and stop, never retry here
        record.update(status="error", error=gs.redact(f"{type(error).__name__}: {error}", api_key), utility=None,
                      duration_seconds=None, trace=getattr(error, "partial_messages", []),
                      llm_calls=getattr(error, "partial_calls", []))
    record["reported_models"] = sorted({c["reported_model"] for c in record["llm_calls"] if c.get("reported_model")})
    record["usage"] = usage_totals(record["llm_calls"])
    record["finished_at"] = utc_now()
    runs_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def execute_plan(plan: dict, adapter: Adapter, runs_dir: Path, max_runs: int, max_llm_calls: int,
                 api_key: str | None = None, only: list[str] | None = None, force: bool = False,
                 skills_dir: Path | None = None, log=print) -> list[dict]:
    """Execute the pending runs of the plan within the budget and return their records.

    Raises ValueError, before anything runs, if the plan is larger than its own limit or more runs are pending than
    max_runs allows.
    """
    if len(plan["runs"]) > plan["limits"]["max_runs"]:
        raise ValueError(f"the plan has {len(plan['runs'])} runs but its own limit is {plan['limits']['max_runs']}")
    todo = pending_runs(plan, runs_dir, only, force)
    if len(todo) > max_runs:
        raise ValueError(f"{len(todo)} runs are pending but --max-runs is {max_runs}; nothing was run")
    records = []
    for spec in todo:
        log(f"running {spec['run_id']} ...")
        record = execute_run(spec, adapter, runs_dir, max_llm_calls, api_key, skills_dir, force)
        log(f"  {record['status']}, utility {record['utility']}, {record['usage']['calls']} model calls")
        records.append(record)
    return records


# ------------------------------------------------------------------------------------------ the real AgentDojo

class AgentDojoAdapter:
    """Runs a task in AgentDojo's workspace suite with its default pipeline and an OpenAI chat model.

    AgentDojo and openai are imported here, so importing this module needs neither. The pipeline is AgentDojo's own
    (make_pipeline without defence): system message, user query, model, tool loop. Only the system message differs
    between conditions. The OpenAI client is wrapped to count and log every call.
    """

    def __init__(self) -> None:
        import openai
        from function_calling_pi import benchmark as ad_benchmark
        from function_calling_pi.agent_pipeline import (AgentPipeline, InitQuery, OpenAILLM, SystemMessage,
                                                        ToolsExecutionLoop, ToolsExecutor)
        from function_calling_pi.agent_pipeline.make_pipeline import load_system_message
        from function_calling_pi.logging import OutputLogger

        self.openai, self.benchmark, self.OutputLogger = openai, ad_benchmark, OutputLogger
        self.parts = (AgentPipeline, InitQuery, OpenAILLM, SystemMessage, ToolsExecutionLoop, ToolsExecutor)
        self._load_system_message = load_system_message

    def default_system_message(self) -> str:
        return self._load_system_message(None)

    def _counting_client(self, max_llm_calls: int, calls: list[dict]):
        client = self.openai.OpenAI()
        original = client.chat.completions.create
        attempts = [0]

        def create(*args, **kwargs):
            attempts[0] += 1
            if attempts[0] > max_llm_calls:
                raise CallBudgetExceeded(f"more than {max_llm_calls} model calls in one run")
            response = original(*args, **kwargs)
            usage = getattr(response, "usage", None)
            calls.append({
                "requested_model": kwargs.get("model"), "reported_model": getattr(response, "model", None),
                "finish_reason": response.choices[0].finish_reason if response.choices else None,
                "usage": None if usage is None else {"prompt_tokens": usage.prompt_tokens,
                                                    "completion_tokens": usage.completion_tokens,
                                                    "total_tokens": usage.total_tokens},
            })
            return response

        client.chat.completions.create = create
        return client

    def run(self, spec: dict, system_message: str, max_llm_calls: int) -> dict:
        AgentPipeline, InitQuery, OpenAILLM, SystemMessage, ToolsExecutionLoop, ToolsExecutor = self.parts
        calls: list[dict] = []
        llm = OpenAILLM(self._counting_client(max_llm_calls, calls), spec["model"])
        pipeline = AgentPipeline([SystemMessage(system_message), InitQuery(), llm,
                                  ToolsExecutionLoop([ToolsExecutor(), llm])])
        pipeline.name = spec["run_id"]
        suite = self.benchmark.SUITES["workspace"]
        task = suite.get_user_task_by_id(spec["scenario"])
        with tempfile.TemporaryDirectory() as logdir:
            started = time.time()
            error = None
            utility = None
            try:
                with self.OutputLogger(logdir):
                    utility, _ = self.benchmark.run_task_without_injection_tasks(
                        suite, pipeline, task, logdir=Path(logdir), force_rerun=True)
            except Exception as exc:  # keep the partial trace, then report
                error = f"{type(exc).__name__}: {exc}"
            duration = time.time() - started
            files = list(Path(logdir).rglob("none.json"))
            trace = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
        if not error and trace.get("error"):
            error = trace["error"]
        return {"messages": trace.get("messages", []), "utility": utility, "duration": duration,
                "llm_calls": calls, "error": error}

    def ground_truth_check(self, scenario: str) -> tuple[bool, bool]:
        """Run the task's own ground-truth tool calls through the harness (no model, no network); utility must be True."""
        from function_calling_pi.agent_pipeline import GroundTruthPipeline
        suite = self.benchmark.SUITES["workspace"]
        task = suite.get_user_task_by_id(scenario)
        return suite.run_task_with_pipeline(GroundTruthPipeline(task), task, None, {})


# ------------------------------------------------------------------------------------------------ model access

def check_model_access(model: str, api_key: str, post=gs.post_json) -> dict:
    """Ask the chat completions API for a five-token reply from `model`; report whether it answered and as which model."""
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 5}
    try:
        reply = post(CHAT_URL, body, api_key, retries=0)
    except gs.GenerationError as error:
        return {"model": model, "accessible": False, "error": gs.redact(str(error), api_key)}
    return {"model": model, "accessible": True, "reported_model": reply.get("model"), "error": None}


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute an execution plan in AgentDojo.")
    parser.add_argument("--plan", type=Path, default=bp.DEFAULT_PLAN)
    parser.add_argument("--runs-dir", type=Path, default=EXECUTION_DIR / "runs")
    parser.add_argument("--max-runs", type=int, default=2)
    parser.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS)
    parser.add_argument("--only", nargs="+", help="run only these run ids")
    parser.add_argument("--force", action="store_true", help="repeat runs that already have a record")
    parser.add_argument("--dry-run", action="store_true", help="show the pending runs and the system messages; run nothing")
    parser.add_argument("--check-access", nargs="+", metavar="MODEL", help="check chat access for these models, in order")
    parser.add_argument("--ground-truth-check", metavar="SCENARIO", help="run the task's ground truth through the harness")
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")

    if args.ground_truth_check:
        utility, _ = AgentDojoAdapter().ground_truth_check(args.ground_truth_check)
        print(f"ground truth of {args.ground_truth_check}: utility {utility}")
        sys.exit(0 if utility else 1)
    if args.check_access:
        if not api_key:
            sys.exit("OPENAI_API_KEY is not set")
        results = [check_model_access(m, api_key) for m in args.check_access]
        for r in results:
            print(f"{r['model']}: {'accessible, reported as ' + str(r['reported_model']) if r['accessible'] else 'NOT accessible: ' + r['error']}")
        EXECUTION_DIR.mkdir(parents=True, exist_ok=True)
        (EXECUTION_DIR / "model_access.json").write_text(
            json.dumps({"checked_at": utc_now(), "results": results}, indent=4) + "\n", encoding="utf-8")
        return

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    todo = pending_runs(plan, args.runs_dir, args.only, args.force)
    print(f"{len(todo)} pending of {len(plan['runs'])} runs (budget: {args.max_runs} runs, "
          f"{args.max_llm_calls} model calls each)")
    if args.dry_run:
        for spec in todo:
            print(f"  {spec['run_id']}: skills {[s['id'] for s in spec['skills']] or '-'}")
        return
    if not api_key:
        sys.exit("OPENAI_API_KEY is not set")
    records = execute_plan(plan, AgentDojoAdapter(), args.runs_dir, args.max_runs, args.max_llm_calls, api_key,
                           args.only, args.force)
    print(f"{len(records)} runs recorded in {args.runs_dir}")


if __name__ == "__main__":
    main()
