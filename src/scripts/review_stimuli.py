"""
Purpose:
    A reproducible review of the stimuli of a corpus, and the record of its status. Two parts:
      * AUTOMATED checks (A1-A10), recomputed every time, that a stimulus is complete, consistent between the
        low and high transparency conditions, faithful to the execution record behind it, free of
        researcher-only information in anything a participant can receive, and traceable to hashed sources
        (for a simulated record also: its safe run, skills, attack calls and inclusion verdict);
      * a MANUAL checklist (M1-M7) that only a researcher can answer (is the final response consistent with the
        actions, is the wording neutral, is the classification right). This script never fills it in: answers are
        recorded with --mark, together with a hash of the stimulus content, so a stimulus that changes after its
        review is shown as "stale" instead of "reviewed".

    The review record is a RESEARCHER-ONLY file (default src/data/review/study_review.json for the study corpus). It
    contains the classification (safe / unsafe) of each stimulus; no participant route serves it
    (tests/test_stimulus_review.py checks this). Statuses: auto_failed, unreviewed (automated checks pass, manual
    checklist incomplete), reviewed, rejected (a manual item failed), stale (content changed since the review).

Inputs:
    A corpus file (src/data/corpora/*.json; default study.json), the stimuli, workspaces and execution records it
    names, and the existing review record (to keep manual answers). Only the corpus's main folder is reviewed: the
    stimuli of a simulated_source are reviewed in their own corpus (src/data/corpora/simulated.json), whose record
    the study corpus's reviewed_in rule reads.

Outputs:
    The review record (corpus "review_file"): the checklist, and per stimulus its classification, content hashes,
    automated results, manual answers and status. Automated parts are deterministic (no timestamps).
    A summary table on stdout.

Dependencies:
    corpus.py, participant_server.py (the same functions that serve participants are used to build the views that
    are checked). Python standard library only.

Usage:
    python3 -B src/scripts/review_stimuli.py                          # summary of the study corpus (writes nothing)
    python3 -B src/scripts/review_stimuli.py --write                  # (re)write the review record, keeping manual answers
    python3 -B src/scripts/review_stimuli.py --check                  # exit 1 if a check fails or the record is out of date
    python3 -B src/scripts/review_stimuli.py --corpus src/data/corpora/validation.json --write
    python3 -B src/scripts/review_stimuli.py --mark user_task_1__no_injection --reviewer NAME \\
        --set M1=pass M2=pass M3=pass M4=pass M5=pass M6=pass M7=pass --notes "text"
"""

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

import corpus as corpus_cfg
import participant_server as ps

REVIEW_VERSION = "stimulus_review_v1"
REQUIRED_FILES = ("low.json", "high.json", "provenance.json", "preview.txt")

AUTOMATED = {
    "A1": "All files exist: low.json, high.json, provenance.json, preview.txt and the workspace snapshot.",
    "A2": "The stimulus is eligible in this corpus and listed by the server.",
    "A3": "Low and high show the same request and final response; low has no trace, high has one.",
    "A4": "Every action in the high condition has an action sentence, a tool name and an outcome summary.",
    "A5": "Request and final response equal the execution record's task prompt and final assistant message.",
    "A6": "The high condition's actions equal the execution record's tool calls, in order.",
    "A7": "The served workspace is the before state only, and its replay matched every saved tool result.",
    "A8": "Every skill of the execution loads (hash-checked) and has documentation text.",
    "A9": "No researcher-only identifier (folder name, stimulus id, run id, item id, strategy or condition name) "
          "appears in anything served to a participant for this stimulus.",
    "A10": "Provenance: the execution record matches the SHA-256 in the stimulus provenance. For a simulated record "
           "also: its safe run and every skill file match their SHA-256, the recorded attack calls open the trace, "
           "its inclusion check passed, and its material kind (if given) is simulated_trace.",
}
MANUAL = {
    "M1": "The user request is clear on its own.",
    "M2": "The final response is consistent with the tool results in the action log (no contradictions such as "
          "'nothing found' next to a log that lists results).",
    "M3": "Action sentences, arguments and outcome summaries are accurate and neutral (no hint of safety).",
    "M4": "The workspace before state contains what a participant needs to check the actions and the response.",
    "M5": "The skill documentation reads as ordinary documentation; nothing is out of place apart from what the "
          "classification below accounts for.",
    "M6": "The classification recorded in this file (and, if unsafe, the unauthorised action) is correct.",
    "M7": "Nothing a participant sees reveals the classification or where the stimulus came from.",
}
ANSWERS = ("pass", "fail", "na")


# ------------------------------------------------------------------------------------------------ loading

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify(record: dict) -> str:
    """Researcher-only label: safe (no injection), unsafe (injected run) or unsafe-simulated (offline fixture)."""
    if record.get("simulated"):
        return "unsafe-simulated"
    return "unsafe" if record.get("injection") else "safe"


def load_context(folder: str) -> dict:
    """Everything the automated checks look at for one stimulus, from the ACTIVE corpus (ps.use_corpus first)."""
    stim_dir, ws_dir = ps.stimulus_dir(folder) / folder, ps.workspace_dir_of(folder) / folder
    ctx = {"folder": folder, "files": {n: (stim_dir / n).is_file() for n in REQUIRED_FILES},
           "workspace_file": (ws_dir / "workspace.json").is_file(), "eligible": ps.is_eligible(folder),
           "listed": folder in ps.list_stimulus_ids()}
    ctx["prov"] = json.loads((stim_dir / "provenance.json").read_text(encoding="utf-8"))
    record_bytes = (ps.ROOT / ctx["prov"]["source"]["file"]).read_bytes()
    ctx["record"] = json.loads(record_bytes)
    ctx["record_sha256"] = hashlib.sha256(record_bytes).hexdigest()
    ctx["ws_prov"] = (json.loads((ws_dir / "provenance.json").read_text(encoding="utf-8"))
                      if (ws_dir / "provenance.json").is_file() else None)
    ctx["low"], ctx["high"] = ps.participant_view(folder, "low"), ps.participant_view(folder, "high")
    ctx["workspace"] = ps.participant_workspace(folder)
    ctx["skills"] = ps.participant_skills(folder)
    ctx["skill_docs"] = {s["id"]: ps.participant_skill(folder, s["id"]) for s in ctx["skills"]}
    return ctx


def served_text(ctx: dict) -> str:
    """Everything a participant can receive for this stimulus, as one string (for the identifier scan)."""
    return json.dumps([ctx["low"], ctx["high"], ctx["workspace"], ctx["skills"], ctx["skill_docs"]], sort_keys=True)


def researcher_only_identifiers(ctx: dict) -> list[str]:
    """Strings that identify the source or the design of a stimulus and must never be served."""
    record, prov = ctx["record"], ctx["prov"]
    names = {ctx["folder"], prov.get("stimulus_id"), record.get("run_id"), record.get("condition"),
             (record.get("injection") or {}).get("item_id"), (record.get("injection") or {}).get("strategy"),
             "important_instructions", "tool_knowledge", "skills-v4b", "inj-v1", "simulated-ui-validation",
             "simulated-v1", "simulated_trace"}
    return sorted(n for n in names if n)


# ------------------------------------------------------------------------------------------------ checks

def _result(ok: bool, detail: str = "") -> dict:
    return {"pass": bool(ok), "detail": detail}


def file_matches(path: str | None, digest: str | None) -> bool:
    """True if a repository-relative file exists and has this SHA-256."""
    if not path or not digest or not (ps.ROOT / path).is_file():
        return False
    return hashlib.sha256((ps.ROOT / path).read_bytes()).hexdigest() == digest


def provenance_problems(ctx: dict) -> list[str]:
    """What A10 finds wrong with a stimulus's provenance (empty list: complete and verifiable).

    Every stimulus: its execution record must match the SHA-256 the stimulus provenance recorded. A simulated record
    (constructed, not executed) must also carry what makes it reproducible and honest: the genuine safe run it was
    built from and every skill file, each matching its SHA-256; the attack calls, which must be the first tool calls
    of the trace (the construction inserts them before the agent's first reply); an inclusion check that passed; and,
    when the record states a material kind, "simulated_trace"."""
    record, problems = ctx["record"], []
    if ctx.get("record_sha256") != ctx["prov"]["source"].get("sha256"):
        problems.append("the execution record does not match the SHA-256 in the stimulus provenance")
    if not record.get("simulated"):
        return problems
    simulation = record.get("simulation") or {}
    safe = simulation.get("safe_run") or {}
    if not file_matches(safe.get("file"), safe.get("sha256")):
        problems.append("the safe run is missing or does not match its SHA-256")
    if not record.get("skills") or not all(file_matches(s.get("file"), s.get("sha256")) for s in record["skills"]):
        problems.append("a skill file is missing or does not match its SHA-256")
    attack = simulation.get("attack_calls") or []
    calls = [{"function": c["function"], "args": c["args"]}
             for m in record["trace"] if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    if not attack or calls[:len(attack)] != attack:
        problems.append("the recorded attack calls are missing or do not open the trace")
    if (simulation.get("inclusion_check") or {}).get("included") is not True:
        problems.append("the inclusion check is missing or did not pass")
    if record.get("material_kind", "simulated_trace") != "simulated_trace":
        problems.append(f"material kind {record['material_kind']!r} on a simulated record")
    return problems


def run_checks(ctx: dict) -> dict:
    """The automated checks A1-A10 on a loaded context: {check id: {"pass", "detail"}}."""
    low, high, record = ctx["low"], ctx["high"], ctx["record"]
    trace = high.get("trace", [])
    actions = [s for s in trace if s["type"] == "action"]
    out = {}
    missing = sorted(n for n, ok in ctx["files"].items() if not ok) + ([] if ctx["workspace_file"] else ["workspace.json"])
    out["A1"] = _result(not missing, "missing: " + ", ".join(missing) if missing else "")
    out["A2"] = _result(ctx["eligible"] and ctx["listed"], f"eligible={ctx['eligible']}, listed={ctx['listed']}")
    out["A3"] = _result(low["user_request"] == high["user_request"] and low["final_response"] == high["final_response"]
                        and "trace" not in low and bool(trace),
                        "request/response differ, low has a trace, or high has none")
    bad = [s.get("step") for s in actions if not (s["action"] and s["tool"] and s["outcome"]["summary"])]
    out["A4"] = _result(not bad and bool(actions), f"actions with a missing part: {bad}" if bad else "")
    final = next((m["content"] for m in reversed(record["trace"]) if m["role"] == "assistant" and m.get("content")), None)
    out["A5"] = _result(high["user_request"] == record["task_prompt"] and high["final_response"] == final,
                        "request or final response differs from the execution record")
    calls = [c["function"] for m in record["trace"] if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    out["A6"] = _result([a["tool"] for a in actions] == calls, f"log tools {[a['tool'] for a in actions]} vs record {calls}")
    ws = ctx["workspace"]
    replay = (ctx["ws_prov"] or {}).get("replay", {})
    out["A7"] = _result(set(ws) == {"account", "state"} and set(ws["state"]) == {"emails", "events", "files"}
                        and replay.get("all_match_saved_results") is True,
                        "workspace view has extra keys, or the replay did not match every saved result")
    docs_ok = bool(ctx["skills"]) and all(d["markdown"].strip() for d in ctx["skill_docs"].values())
    out["A8"] = _result(docs_ok, "no skills, or an empty skill document")
    text = served_text(ctx)
    leaks = [n for n in researcher_only_identifiers(ctx) if n in text]
    out["A9"] = _result(not leaks, f"served text contains: {leaks}" if leaks else "")
    problems = provenance_problems(ctx)
    out["A10"] = _result(not problems, "; ".join(problems))
    return out


def content_hash(ctx: dict) -> str:
    """Hash of everything a reviewer looked at (both conditions, the before state, the skills, the label)."""
    payload = {"low": ctx["low"], "high": ctx["high"], "workspace": ctx["workspace"],
               "skills": {k: v["markdown"] for k, v in sorted(ctx["skill_docs"].items())},
               "classification": classify(ctx["record"])}
    return sha256_text(json.dumps(payload, sort_keys=True))


def empty_manual() -> dict:
    return {"reviewer": None, "reviewed_at": None, "reviewed_content_sha256": None,
            "items": {k: None for k in MANUAL}, "notes": ""}


def status_of(entry: dict) -> str:
    """auto_failed / stale / unreviewed / rejected / reviewed, from an entry's automated and manual parts."""
    if not entry["automated_pass"]:
        return "auto_failed"
    manual = entry["manual"]
    answers = list(manual["items"].values())
    if all(a is None for a in answers) and not manual["reviewer"]:
        return "unreviewed"
    if manual["reviewed_content_sha256"] != entry["content_sha256"]:
        return "stale"
    if any(a is None for a in answers) or not manual["reviewer"]:
        return "unreviewed"
    return "rejected" if "fail" in answers else "reviewed"


def review_corpus(previous: dict | None = None) -> dict:
    """The review record of the active corpus, keeping manual answers from `previous` (same folder names)."""
    previous = previous or {}
    entries = {}
    # A corpus's simulated_source stimuli are reviewed in their own corpus (the record its reviewed_in rule names),
    # so they are not reviewed a second time here.
    for folder in (f for f in ps.list_stimulus_ids() if ps.stimulus_source(f) == "main"):
        ctx = load_context(folder)
        checks = run_checks(ctx)
        manual = (previous.get("stimuli", {}).get(folder) or {}).get("manual") or empty_manual()
        entry = {"classification": classify(ctx["record"]), "content_sha256": content_hash(ctx),
                 "automated": checks, "automated_pass": all(c["pass"] for c in checks.values()), "manual": manual}
        entry["status"] = status_of(entry)
        entries[folder] = entry
    return {"review_version": REVIEW_VERSION, "corpus": ps.CORPUS["name"],
            "note": "RESEARCHER-ONLY: contains classifications. Never served to participants.",
            "checklist": {"automated": AUTOMATED, "manual": MANUAL}, "stimuli": dict(sorted(entries.items()))}


def mark(record: dict, folder: str, reviewer: str, answers: dict, notes: str, when: str) -> dict:
    """Record a manual review of one stimulus (all of M1-M7 must be answered), tied to its current content hash."""
    entry = record["stimuli"].get(folder)
    if entry is None:
        raise ValueError(f"{folder} is not in the review record; run --write first")
    if set(answers) != set(MANUAL) or any(a not in ANSWERS for a in answers.values()):
        raise ValueError(f"answer every item {sorted(MANUAL)} with one of {ANSWERS}")
    if not reviewer.strip():
        raise ValueError("a reviewer name is required")
    entry["manual"] = {"reviewer": reviewer.strip(), "reviewed_at": when, "reviewed_content_sha256": entry["content_sha256"],
                       "items": dict(sorted(answers.items())), "notes": notes}
    entry["status"] = status_of(entry)
    return entry


# ------------------------------------------------------------------------------------------------ command line

def summary_lines(record: dict) -> list[str]:
    lines = [f"corpus {record['corpus']}: {len(record['stimuli'])} stimuli"]
    for folder, e in record["stimuli"].items():
        failed = [k for k, c in e["automated"].items() if not c["pass"]]
        lines.append(f"  {folder:34} {e['classification']:17} auto {'PASS' if e['automated_pass'] else 'FAIL ' + ','.join(failed):9} "
                     f"-> {e['status']}")
    counts = {}
    for e in record["stimuli"].values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    lines.append("  status counts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return lines


def read_record(path: Path | None) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path and path.is_file() else None


def dumps(record: dict) -> str:
    return json.dumps(record, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, default=corpus_cfg.DEFAULT_CORPUS)
    parser.add_argument("--write", action="store_true", help="write the review record (keeps manual answers)")
    parser.add_argument("--check", action="store_true", help="exit 1 if a check fails or the record is out of date")
    parser.add_argument("--require-reviewed", action="store_true", help="with --check: also fail unless all are reviewed")
    parser.add_argument("--mark", metavar="FOLDER", help="record a manual review of one stimulus")
    parser.add_argument("--reviewer")
    parser.add_argument("--set", nargs="*", default=[], metavar="Mn=pass|fail|na")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    ps.use_corpus(args.corpus)
    path = ps.CORPUS["review_path"]
    saved = read_record(path)
    record = review_corpus(saved)
    status = 0
    if args.mark:
        answers = dict(item.split("=", 1) for item in args.set)
        record = review_corpus(saved)
        if saved is None:
            print("no review record yet: run --write first", file=sys.stderr)
            return 1
        mark(record, args.mark, args.reviewer or "", answers, args.notes, datetime.date.today().isoformat())
        path.write_text(dumps(record), encoding="utf-8")
    elif args.write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(record), encoding="utf-8")
    print("\n".join(summary_lines(record)))
    if args.check:
        if saved != record:
            print("review record is missing or out of date: run --write", file=sys.stderr)
            status = 1
        if any(not e["automated_pass"] for e in record["stimuli"].values()):
            print("automated checks failed", file=sys.stderr)
            status = 1
        if args.require_reviewed and any(e["status"] != "reviewed" for e in record["stimuli"].values()):
            print("not every stimulus is reviewed", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
