"""
Purpose:
    Summarise the run records written by run_executions.py: for every scenario, model and repetition, put the
    no-skills run and the skills run side by side (did AgentDojo judge the task done, which tools were called,
    how that differs between conditions and from the recorded gpt-4o run, what it cost). It reports facts and
    differences; it does not decide that a skill "helped" from one run.

Inputs:
    src/data/execution/runs/*.json (run_executions.py) and, as a reference, the recorded no-attack run of each
    scenario (agentdojo_runs.py).

Outputs:
    src/data/execution/summary.json (deterministic) and a table on standard output. Per run: status, utility, tool
    calls in order, data-changing calls, final answer, model calls and tokens, reported model. Per comparison:
    the two conditions' utilities, whether their tool sequences are equal, tools only one of them called, and
    whether each sequence equals the recorded run's. Across all runs: counts and utility rates per condition.

Dependencies:
    agentdojo_runs.py. Standard library only.

Usage:
    python3 -B src/scripts/summarize_executions.py [--runs-dir PATH] [--out PATH]
"""

import argparse
import json
from pathlib import Path

import agentdojo_runs as runs

EXECUTION_DIR = runs.REPO_ROOT / "src" / "data" / "execution"


def tool_sequence(trace: list[dict]) -> list[dict]:
    """The tool calls of a trace ({"function", "args"}), in order."""
    return runs.tool_calls({"messages": trace})


def final_answer(trace: list[dict]) -> str | None:
    """The agent's last message when it is plain assistant text, else None (the run ended without an answer)."""
    try:
        return runs.final_response({"user_task_id": "", "messages": trace})
    except (ValueError, IndexError, KeyError):
        return None


def summarize_run(record: dict) -> dict:
    calls = tool_sequence(record["trace"])
    names = [c["function"] for c in calls]
    return {
        "run_id": record["run_id"], "scenario": record["scenario"], "condition": record["condition"],
        "model": record["model"], "repetition": record["repetition"], "status": record["status"],
        "error": record["error"], "utility": record["utility"], "skills": [s["id"] for s in record["skills"]],
        "tool_calls": names, "calls_with_arguments": calls,
        "data_changing_calls": [n for n in names if n in runs.WRITE_TOOLS],
        "final_answer": final_answer(record["trace"]), "usage": record["usage"],
        "reported_models": record["reported_models"], "duration_seconds": record["duration_seconds"],
        "system_message_sha256": record["system_message_sha256"],
    }


def compare(none: dict, skilled: dict, recorded: list[str]) -> dict:
    """Differences between the no-skills and skills run of one scenario, model and repetition."""
    return {
        "scenario": none["scenario"], "model": none["model"], "repetition": none["repetition"],
        "skills": skilled["skills"],
        "utility": {"no-skills": none["utility"], "mapped-skills": skilled["utility"]},
        "same_tool_sequence": none["calls_with_arguments"] == skilled["calls_with_arguments"],
        "same_tool_names": none["tool_calls"] == skilled["tool_calls"],
        "only_without_skills": sorted(set(none["tool_calls"]) - set(skilled["tool_calls"])),
        "only_with_skills": sorted(set(skilled["tool_calls"]) - set(none["tool_calls"])),
        "data_changing_calls": {"no-skills": none["data_changing_calls"],
                                "mapped-skills": skilled["data_changing_calls"]},
        "equals_recorded_tool_names": {"no-skills": none["tool_calls"] == recorded,
                                       "mapped-skills": skilled["tool_calls"] == recorded},
        "recorded_tool_names": recorded,
        "model_calls": {"no-skills": none["usage"]["calls"], "mapped-skills": skilled["usage"]["calls"]},
        "total_tokens": {"no-skills": none["usage"]["total_tokens"], "mapped-skills": skilled["usage"]["total_tokens"]},
    }


def summarize(runs_dir: Path) -> dict:
    records = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(runs_dir.glob("*.json"))]
    summaries = [summarize_run(r) for r in records]
    by_key = {}
    for s in summaries:
        by_key.setdefault((s["scenario"], s["model"], s["repetition"]), {})[s["condition"]] = s
    comparisons = []
    for (scenario, _, _), pair in sorted(by_key.items(), key=lambda kv: (runs.task_number(kv[0][0]), kv[0][1], kv[0][2])):
        if "no-skills" in pair and "mapped-skills" in pair:
            recorded = [c["function"] for c in runs.tool_calls(runs.load_run(scenario))]
            comparisons.append(compare(pair["no-skills"], pair["mapped-skills"], recorded))
    rates = {}
    for condition in ("no-skills", "mapped-skills"):
        mine = [s for s in summaries if s["condition"] == condition]
        rates[condition] = {"runs": len(mine), "completed": sum(s["status"] == "completed" for s in mine),
                            "utility_true": sum(s["utility"] is True for s in mine)}
    return {"runs": summaries, "comparisons": comparisons, "by_condition": rates}


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarise execution run records.")
    parser.add_argument("--runs-dir", type=Path, default=EXECUTION_DIR / "runs")
    parser.add_argument("--out", type=Path, default=EXECUTION_DIR / "summary.json")
    args = parser.parse_args()
    if not list(args.runs_dir.glob("*.json")):
        raise SystemExit(f"no run records in {args.runs_dir}")
    result = summarize(args.runs_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    for s in result["runs"]:
        print(f"{s['run_id']}: {s['status']}, utility {s['utility']}, tools {s['tool_calls']}, "
              f"{s['usage']['calls']} calls, {s['usage']['total_tokens']} tokens")
    for c in result["comparisons"]:
        print(f"{c['scenario']} ({c['model']}): utility {c['utility']}, same tool sequence {c['same_tool_sequence']}, "
              f"matches recorded {c['equals_recorded_tool_names']}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
