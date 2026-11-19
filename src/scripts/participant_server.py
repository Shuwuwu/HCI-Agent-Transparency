"""
Purpose:
    Serve the participant-facing workspace-agent chatbot prototype. It shows one
    generated stimulus in one transparency condition (low or high) using the same
    chat layout. This is separate from Invariant Explorer, which stays the
    researcher-facing tool for raw executions.

Inputs:
    src/data/stimuli/<stimulus folder>/low.json and high.json (written by
    build_stimuli.py). Folders are named user_task_<N>__no_injection; the stimulus
    id "stim_<hash>" stays inside the files and provenance only. Only these two
    files are ever sent to participants.
    <stimulus folder>/provenance.json and the execution record it points to
    (src/data/execution/runs/<run_id>.json) are read ONLY to decide eligibility
    (see is_eligible); nothing from them is sent to the browser.
    Static front-end files in src/participant_ui/ (index.html, app.js, style.css,
    markdown.js).

Outputs:
    A local HTTP server (default http://127.0.0.1:8765). Routes:
        /                                  landing page linking both conditions
        /chat?s=<folder>&c=low|high   the chatbot page
        /api/stimulus?s=<folder>&c=low|high   sanitized JSON for that condition
        /static/<file>                     whitelisted front-end files

Dependencies:
    Python standard library only. Reads the files produced by build_stimuli.py.

Usage:
    python3 src/scripts/participant_server.py [--port 8765] [--host 127.0.0.1]
    Then open http://127.0.0.1:8765/chat?s=user_task_1__no_injection&c=high
"""

import argparse
import functools
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
STIMULI_DIR = ROOT / "src" / "data" / "stimuli"
UI_DIR = ROOT / "src" / "participant_ui"
STATIC_FILES = {"app.js": "text/javascript", "style.css": "text/css", "markdown.js": "text/javascript"}
CONDITIONS = ("low", "high")
STIMULUS_ID_RE = re.compile(r"^user_task_\d+__[a-z0-9_]+$")  # folder name, e.g. user_task_1__no_injection
BATCH_SUFFIX = "__skills-v4b"  # run-id suffix of the latest mapped-skills batch
DEFAULT_STIMULUS = "user_task_1__no_injection"  # the validation stimulus


@functools.lru_cache(maxsize=None)
def is_eligible(stimulus_id, stimuli_dir=STIMULI_DIR, root=ROOT):
    """True only for a successful run of the latest mapped-skills-v4b batch.

    Reads the stimulus provenance and the execution record it cites. Eligible when
    the source is an execution record (not an AgentDojo trace), its file matches
    its recorded sha256, the provenance's stimulus_folder equals the folder name,
    the run id ends with BATCH_SUFFIX, condition is
    "mapped-skills", status is "completed" and utility is true. Anything missing
    or unreadable is ineligible. Results are cached for the server's lifetime.
    """
    try:
        prov = json.loads((Path(stimuli_dir) / stimulus_id / "provenance.json").read_text(encoding="utf-8"))
        src = prov["source"]
        if prov["stimulus_folder"] != stimulus_id:
            return False
        if src.get("kind") != "execution record":
            return False
        path = Path(root) / src["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != src["sha256"]:
            return False
        rec = json.loads(path.read_text(encoding="utf-8"))
        return (rec["run_id"].endswith(BATCH_SUFFIX) and rec["run_id"] == src["run_id"]
                and rec["condition"] == "mapped-skills" and rec["status"] == "completed"
                and rec["utility"] is True)
    except (OSError, KeyError, ValueError, TypeError):
        return False


def _clean_step(entry):
    """Copy only the participant-visible fields of one action-log entry.

    Whitelisting (rather than deleting known-bad keys) means a field added to the
    stimulus schema later stays hidden until it is deliberately exposed here.
    Returns None for entry types the UI does not know how to show.
    """
    if entry.get("type") == "action":
        return {
            "type": "action",
            "step": entry["step"],
            "action": entry["action"],
            "tool": entry["tool"],
            "arguments": [{"name": a["name"], "value": a["value"], "truncated": bool(a.get("truncated"))}
                          for a in entry.get("arguments", [])],
            "outcome": {"status": entry["outcome"]["status"], "summary": entry["outcome"]["summary"]},
        }
    if entry.get("type") == "agent_message":
        # Text the agent wrote between tool calls, shown in sequence as-is.
        return {"type": "agent_message", "text": entry["text"]}
    return None


def participant_view(stimulus_id, condition, stimuli_dir=STIMULI_DIR):
    """Return the participant-safe JSON for one stimulus and condition.

    Parameters:
        stimulus_id: folder name such as "user_task_1__no_injection"; must match STIMULUS_ID_RE.
        condition: "low" or "high".
        stimuli_dir: folder holding <folder name>/low.json and high.json.
    Returns a dict with user_request and final_response, plus "trace" (the ordered
    steps) only for the high condition. The low view is built from low.json alone
    and never contains a trace key. Raises ValueError for an invalid id/condition
    and FileNotFoundError when the stimulus does not exist.
    """
    if not STIMULUS_ID_RE.match(stimulus_id or ""):
        raise ValueError("invalid stimulus id")
    if not is_eligible(stimulus_id, stimuli_dir):
        raise FileNotFoundError(stimulus_id)  # same answer as a missing stimulus
    if condition not in CONDITIONS:
        raise ValueError("invalid condition")
    path = Path(stimuli_dir) / stimulus_id / f"{condition}.json"
    if not path.is_file():
        raise FileNotFoundError(stimulus_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    view = {"user_request": data["user_request"], "final_response": data["final_response"]}
    if condition == "high":
        steps = (_clean_step(e) for e in data["action_log"])
        view["trace"] = [s for s in steps if s is not None]
    return view


def list_stimulus_ids(stimuli_dir=STIMULI_DIR):
    """Return eligible stimulus ids that have both condition files, default one first."""
    ids = sorted(p.name for p in Path(stimuli_dir).iterdir()
                 if STIMULUS_ID_RE.match(p.name) and is_eligible(p.name, stimuli_dir) and all((p / f"{c}.json").is_file() for c in CONDITIONS))
    return sorted(ids, key=lambda i: i != DEFAULT_STIMULUS)


def landing_page():
    """HTML page listing both conditions of every stimulus (for the researcher)."""
    rows = "".join(
        f'<li><code>{i}</code> — <a href="/chat?s={i}&amp;c=low">low</a> · '
        f'<a href="/chat?s={i}&amp;c=high">high</a></li>' for i in list_stimulus_ids())
    return ("<!doctype html><meta charset=utf-8><title>Prototype</title>"
            "<body style='font:16px system-ui;max-width:640px;margin:2rem auto'>"
            f"<h1>Participant prototype</h1><ul>{rows}</ul></body>")


class Handler(BaseHTTPRequestHandler):
    """Routes GET requests; everything else is rejected."""

    def _send(self, status, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self._send(200, landing_page(), "text/html")
        if url.path == "/chat":
            return self._send(200, (UI_DIR / "index.html").read_text(encoding="utf-8"), "text/html")
        if url.path == "/api/stimulus":
            try:
                view = participant_view(q.get("s", ""), q.get("c", ""))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except FileNotFoundError:
                return self._send(404, json.dumps({"error": "not found"}), "application/json")
            return self._send(200, json.dumps(view), "application/json")
        if url.path.startswith("/static/") and url.path[len("/static/"):] in STATIC_FILES:
            name = url.path[len("/static/"):]
            return self._send(200, (UI_DIR / name).read_bytes(), STATIC_FILES[name])
        return self._send(404, "not found", "text/plain")

    def log_message(self, fmt, *args):
        sys.stderr.write("[participant] " + (fmt % args) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
