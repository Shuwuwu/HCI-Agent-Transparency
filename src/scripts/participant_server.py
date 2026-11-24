"""
Purpose:
    Serve the participant-facing workspace-agent chatbot prototype. It shows one
    generated stimulus in one transparency condition (low or high) using the same
    chat layout. This is separate from Invariant Explorer, which stays the
    researcher-facing tool for raw executions.

Inputs:
    <stimuli_dir of the active corpus>/<stimulus folder>/low.json and high.json (written by
    build_stimuli.py; default corpus: src/data/stimuli). Folder names are descriptive development identifiers; the
    server only requires that they match the corpus's id pattern. The stimulus
    id "stim_<hash>" stays inside the files and provenance only. Only these two
    files are ever sent to participants.
    <stimulus folder>/provenance.json and the execution record it points to
    (src/data/execution/runs/<run_id>.json) are read ONLY to decide eligibility
    (see is_eligible); nothing from them is sent to the browser.
    src/data/workspace/<stimulus folder>/workspace.json (written by
    build_workspace_snapshots.py): only its "before" state is ever served; the
    "after" state stays in the file for researcher verification. Its sibling
    provenance.json is never opened.
    The execution record of the stimulus (its "skills" list, with file paths and
    SHA-256) and src/data/skills/<skill>/SKILL.md: the skill documentation the
    agent was given, served only on request and only if the file still matches
    the recorded hash.
    Static front-end files in src/participant_ui/ (index.html, app.js, scenario_view.js,
    style.css, markdown.js, previews.js, workspace.js, skills.js, interaction.js).

Outputs:
    A local HTTP server (default http://127.0.0.1:8765). Routes:
        /                                  landing page linking both conditions
        /chat?s=<folder>&c=low|high   the chatbot page
        /api/stimulus?s=<folder>&c=low|high   sanitized JSON for that condition
        /api/workspace?s=<folder>          read-only workspace BEFORE the agent ran (same for
                                           both conditions: no condition parameter)
        /api/skills?s=<folder>             the agent's skills for this task: id, name, description
        /api/skill?s=<folder>&k=<skill>    one skill's documentation (Markdown), on request
        POST /api/log                      one interaction event (see interaction_log.py); written
                                           to the separate log folder, never read back by a route
        /static/<file>                     whitelisted front-end files

Dependencies:
    Python standard library only. Reads the files produced by build_stimuli.py. interaction_log.py (event
    validation and storage), corpus.py (corpus configuration and eligibility rules).

Usage:
    python3 src/scripts/participant_server.py [--port 8765] [--host 127.0.0.1] [--log-dir PATH] [--corpus PATH]
    --corpus selects which stimuli are served and which are eligible (default src/data/corpora/study.json; the
    simulated interface-validation stimulus is only in src/data/corpora/validation.json).
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

import corpus as corpus_cfg
import interaction_log as ilog

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "src" / "participant_ui"
STATIC_FILES = {"app.js": "text/javascript", "style.css": "text/css", "markdown.js": "text/javascript",
                "previews.js": "text/javascript", "workspace.js": "text/javascript",
                "skills.js": "text/javascript", "interaction.js": "text/javascript",
                "scenario_view.js": "text/javascript"}
# Workspace fields a participant may receive (same list as build_workspace_snapshots.py).
WORKSPACE_FIELDS = {
    "emails": ("id_", "sender", "recipients", "cc", "bcc", "subject", "body", "status", "timestamp", "attachments"),
    "events": ("id_", "title", "description", "start_time", "end_time", "location", "participants", "all_day",
               "status"),
    "files": ("id_", "filename", "content", "owner", "last_modified", "shared_with", "size"),
}
CONDITIONS = ("low", "high")

# The active corpus (src/data/corpora/*.json) decides where stimuli live and which ones are eligible. The module
# globals below mirror it so existing callers (and tests) can keep reading ps.STIMULI_DIR etc.; use_corpus() swaps it.
CORPUS = None
STIMULI_DIR = WORKSPACE_DIR = DEFAULT_STIMULUS = STIMULUS_ID_RE = None


def use_corpus(path=corpus_cfg.DEFAULT_CORPUS):
    """Make the corpus file at `path` the active one (folders, id pattern, default stimulus, eligibility rules).

    Clears the eligibility cache. Returns the loaded corpus. Default: the study corpus.
    """
    global CORPUS, STIMULI_DIR, WORKSPACE_DIR, DEFAULT_STIMULUS, STIMULUS_ID_RE
    CORPUS = corpus_cfg.load_corpus(path, ROOT)
    STIMULI_DIR, WORKSPACE_DIR = CORPUS["stimuli_path"], CORPUS["workspace_path"]
    DEFAULT_STIMULUS, STIMULUS_ID_RE = CORPUS["default_stimulus"], CORPUS["id_regex"]
    is_eligible.cache_clear()
    return CORPUS


@functools.lru_cache(maxsize=None)
def is_eligible(stimulus_id, stimuli_dir=None, root=ROOT):
    """True only if the stimulus belongs to the active corpus and its execution record passes one of its rules.

    Reads the stimulus provenance and the execution record it cites. Eligible when the corpus's optional "include"
    list (if any) names the folder, the source is an execution record (not an AgentDojo trace), its file matches
    its recorded sha256, the provenance's stimulus_folder equals the folder name, and the record satisfies one of
    the corpus's eligibility rules (corpus.py: run id suffix, condition, status, utility, simulated flag, inclusion
    verdict). Anything missing or unreadable is ineligible. Results are cached until use_corpus() is called.
    """
    stimuli_dir = STIMULI_DIR if stimuli_dir is None else stimuli_dir
    include = CORPUS.get("include")
    if include is not None and stimulus_id not in include:
        return False
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
        return rec["run_id"] == src["run_id"] and corpus_cfg.record_matches_any_rule(CORPUS, rec)
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


def participant_view(stimulus_id, condition, stimuli_dir=None):
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
    path = Path(STIMULI_DIR if stimuli_dir is None else stimuli_dir) / stimulus_id / f"{condition}.json"
    if not path.is_file():
        raise FileNotFoundError(stimulus_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    view = {"user_request": data["user_request"], "final_response": data["final_response"]}
    if condition == "high":
        steps = (_clean_step(e) for e in data["action_log"])
        view["trace"] = [s for s in steps if s is not None]
    return view


def participant_workspace(stimulus_id, stimuli_dir=None, workspace_dir=None):
    """Return the read-only workspace as it was BEFORE the agent ran, for an eligible stimulus.

    The result has "account" (email, current_day) and "state" with "emails", "events" and "files", reduced to
    WORKSPACE_FIELDS. The state after the execution is never returned (it would show what the agent changed).
    It is the same for the low and the high condition: there is no condition argument. Raises ValueError for an
    invalid id and FileNotFoundError when the stimulus is not eligible or has no workspace snapshot yet.
    """
    if not STIMULUS_ID_RE.match(stimulus_id or ""):
        raise ValueError("invalid stimulus id")
    path = Path(WORKSPACE_DIR if workspace_dir is None else workspace_dir) / stimulus_id / "workspace.json"
    if not is_eligible(stimulus_id, stimuli_dir) or not path.is_file():
        raise FileNotFoundError(stimulus_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    state = data["before"]
    return {"account": {"email": data["account"]["email"], "current_day": data["account"]["current_day"]},
            "state": {kind: [{f: item[f] for f in fields} for item in state[kind]]
                      for kind, fields in WORKSPACE_FIELDS.items()}}


def _split_front_matter(text):
    """Split a SKILL.md into ({"name", "description"}, markdown body); the front matter is `key: value` lines."""
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def _skill_entries(stimulus_id, stimuli_dir):
    """The skills of the execution record behind an eligible stimulus: [{"id", "file", "sha256"}]."""
    if not STIMULUS_ID_RE.match(stimulus_id or ""):
        raise ValueError("invalid stimulus id")
    if not is_eligible(stimulus_id, stimuli_dir):
        raise FileNotFoundError(stimulus_id)
    base = STIMULI_DIR if stimuli_dir is None else stimuli_dir
    prov = json.loads((Path(base) / stimulus_id / "provenance.json").read_text(encoding="utf-8"))
    record = json.loads((ROOT / prov["source"]["file"]).read_text(encoding="utf-8"))
    return record["skills"]


def participant_skills(stimulus_id, stimuli_dir=None):
    """The skills the agent had for this stimulus, as [{"id", "name", "description"}] (no documentation text).

    Same for both conditions. Raises ValueError / FileNotFoundError as participant_view does.
    """
    out = []
    for entry in _skill_entries(stimulus_id, stimuli_dir):
        meta, _ = _split_front_matter(skill_text(entry))
        out.append({"id": entry["id"], "name": meta.get("name", entry["id"]), "description": meta.get("description", "")})
    return out


def skill_text(entry):
    """Text of a skill file, only if it still matches the SHA-256 the execution recorded (else FileNotFoundError)."""
    path = ROOT / entry["file"]
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise FileNotFoundError(entry["id"])
    return raw.decode("utf-8")


def participant_skill(stimulus_id, skill_id, stimuli_dir=None):
    """One skill's documentation: {"id", "name", "description", "markdown"}.

    Only skills of this stimulus's execution can be requested. Raises ValueError for an invalid id and
    FileNotFoundError for an unknown stimulus, a skill the agent did not have, or a file that changed.
    """
    for entry in _skill_entries(stimulus_id, stimuli_dir):
        if entry["id"] == skill_id:
            meta, body = _split_front_matter(skill_text(entry))
            return {"id": skill_id, "name": meta.get("name", skill_id), "description": meta.get("description", ""),
                    "markdown": body}
    raise FileNotFoundError(skill_id)


def list_stimulus_ids(stimuli_dir=None):
    """Return eligible stimulus ids that have both condition files, default one first."""
    stimuli_dir = STIMULI_DIR if stimuli_dir is None else stimuli_dir
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
    """Routes GET requests and POST /api/log; everything else is rejected."""

    log_dir = ilog.DEFAULT_LOG_DIR

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
        if url.path == "/api/workspace":
            try:
                view = participant_workspace(q.get("s", ""))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except FileNotFoundError:
                return self._send(404, json.dumps({"error": "not found"}), "application/json")
            return self._send(200, json.dumps(view), "application/json")
        if url.path in ("/api/skills", "/api/skill"):
            try:
                if url.path == "/api/skills":
                    view = participant_skills(q.get("s", ""))
                else:
                    view = participant_skill(q.get("s", ""), q.get("k", ""))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}), "application/json")
            except (FileNotFoundError, KeyError):
                return self._send(404, json.dumps({"error": "not found"}), "application/json")
            return self._send(200, json.dumps(view), "application/json")
        if url.path.startswith("/static/") and url.path[len("/static/"):] in STATIC_FILES:
            name = url.path[len("/static/"):]
            return self._send(200, (UI_DIR / name).read_bytes(), STATIC_FILES[name])
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/api/log":
            return self._send(404, "not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length > ilog.MAX_EVENT_BYTES:
            return self._send(413, json.dumps({"error": "too large"}), "application/json")
        try:
            ilog.append_event(json.loads(self.rfile.read(length) or b"null"), self.log_dir)
        except (ilog.LogError, ValueError) as e:
            return self._send(400, json.dumps({"error": str(e)}), "application/json")
        self.send_response(204)  # nothing is sent back: the log is write-only for the browser
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("[participant] " + (fmt % args) + "\n")


use_corpus()  # the study corpus is active until a caller (main, a test) switches it


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-dir", type=Path, default=ilog.DEFAULT_LOG_DIR)
    parser.add_argument("--corpus", type=Path, default=corpus_cfg.DEFAULT_CORPUS,
                        help="corpus file (src/data/corpora/*.json); validation.json serves the simulated fixture")
    args = parser.parse_args()
    use_corpus(args.corpus)
    Handler.log_dir = args.log_dir
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
