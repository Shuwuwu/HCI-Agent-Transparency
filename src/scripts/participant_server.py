"""
Purpose:
    Serve the participant-facing experiment: the study flow a participant walks through from their link, and the
    workspace-agent chat that shows one generated stimulus in one transparency condition (low or high) at each
    trial. Which stimulus and which condition a participant is shown comes from their record and the stage they
    are on, never from the request. This is separate from Invariant Explorer, which stays the researcher-facing
    tool for raw executions.

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
    Static front-end files in src/participant_ui/ (index.html, flow.html, app.js, flow.js, tutorial.js, assessment.js,
    scenario_view.js, style.css, markdown.js, previews.js, workspace.js, skills.js, interaction.js).

Outputs:
    A local HTTP server (default http://127.0.0.1:8765).

    Participant routes. A participant is given one link, /p/<token>, and every request after it identifies them
    by that token alone: which scenario and which transparency condition they are shown is resolved from their
    record and their current stage (participant_flow.py), never from the request. No participant URL contains a
    stimulus folder or a condition, so neither can be edited and neither reveals what a trial holds.
        /p/<token>                      the flow page; an unknown token is answered 404
        /api/stage?t=<token>            the stage to show now (no stimulus identity); records the arrival
        /api/stimulus?t=<token>         the current scenario stage's sanitized JSON, in that participant's
                                        condition (the trace is present only in the high condition; on the
                                        practice stage, without the configuration's practice.omit_steps)
        /api/workspace?t=<token>        read-only workspace BEFORE the agent ran (same in both conditions)
        /api/skills?t=<token>           the agent's skills for this trial: id, name, description
        /api/skill?t=<token>&k=<skill>  one skill's documentation (Markdown), on request
        POST /api/stage/complete        {"token", "stage_key"}: complete that stage and return the next one. The
                                        consent stage also needs "consent": {"agreed": true, "sha256": <hash of
                                        the consent text shown>}, and stores that agreement; an assessment stage
                                        needs "answers" and stores them (participant_flow.py)
        POST /api/log                   {"token", "trial_id", "type", ...}: one interaction event (see
                                        interaction_log.py), stored in the participant database only for the
                                        trial that token's participant is on now; never read back by any route
        /static/<file>                  whitelisted front-end files (STATIC_FILES); they are sent as they are,
                                        comments included, so they must not describe the study design
    The four content routes answer 404 unless the participant's current stage is a scenario stage, so no stage
    can be used to fetch the next trial's content early or the previous trial's content again.

    Researcher routes, served only with --researcher-preview (off by default, so the participant-facing server
    does not expose them at all):
        /                                      landing page linking both conditions of every stimulus
        /chat?s=<folder>&c=low|high             the scenario page on its own, outside any participant flow
        /static/app.js                          that page's script (RESEARCHER_STATIC_FILES)
        /api/stimulus|workspace|skills|skill?s=<folder>[&c=low|high]   the s= form of the content routes

Dependencies:
    Python standard library only. Reads the files produced by build_stimuli.py. interaction_log.py (event
    validation), corpus.py (corpus configuration and eligibility rules), participant_flow.py (token
    to stage resolution), participant_store.py (participant records and progress), experiment_plan.py (the stage
    sequence, through participant_flow).

Usage:
    python3 -B src/scripts/participant_server.py [--port 8765] [--host 127.0.0.1]
        [--experiment-config PATH] [--db PATH] [--corpus PATH] [--researcher-preview]
    --experiment-config selects the experiment the flow is served under (default the development configuration;
    src/data/experiment_configs/study.json is the real study) and, unless --corpus overrides it, which corpus of
    stimuli is served. --db is the participant database the links were enrolled in.
    Enrol participants and print their links first:
        python3 -B src/scripts/participant_store.py --config src/data/experiment_configs/development.json \
            --enrol-all --links --base-url http://127.0.0.1:8765
    Then open one of the printed links. For a scenario on its own, without a participant record (nothing is
    logged there: interaction events are stored only for a participant link):
        python3 -B src/scripts/participant_server.py --researcher-preview
        http://127.0.0.1:8765/chat?s=user_task_1__no_injection&c=high
"""

import argparse
import contextlib
import functools
import hashlib
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import corpus as corpus_cfg
import experiment_plan as ep
import interaction_log as ilog
import participant_flow as flow
import participant_store as store

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "src" / "participant_ui"
# Files a participant's browser may fetch at /static/<file>. They are sent byte for byte, comments included, so they
# must not describe the study design (tests/test_served_ui_wording.py checks every file here and flow.html).
STATIC_FILES = {"style.css": "text/css", "markdown.js": "text/javascript",
                "previews.js": "text/javascript", "workspace.js": "text/javascript",
                "skills.js": "text/javascript", "interaction.js": "text/javascript",
                "scenario_view.js": "text/javascript", "flow.js": "text/javascript",
                "tutorial.js": "text/javascript", "assessment.js": "text/javascript"}
# Served at /static/<file> only with --researcher-preview. app.js is the /chat page's script: it reads the stimulus
# and the condition from its URL by design, so a participant-facing server must not serve it.
RESEARCHER_STATIC_FILES = {"app.js": "text/javascript"}
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


def flow_status(exc: Exception) -> int:
    """The HTTP status for a flow failure.

    409 when the participant's stage sequence changed since they enrolled or a stage was submitted out of order
    (participant_store.StoreError), 404 for a well-formed token that belongs to nobody and for a stage that holds
    no scenario, 400 for a malformed token. StoreError is checked first because it is a ValueError.
    """
    if isinstance(exc, store.StoreError):
        return 409
    if isinstance(exc, FileNotFoundError):
        return 404
    return 400


def check_practice_presentation(config) -> None:
    """Stop the server at startup if practice.omit_steps names a step that is not an action of the practice stimulus.

    Run after use_corpus, against the high condition (the only one with an action log). Raises ValueError with the
    configuration's path, so a mistyped step number is caught before any participant reaches the practice.
    """
    omit = config["practice"].get("omit_steps", [])
    if omit:
        try:
            flow.omit_trace_steps(participant_view(config["practice"]["stimulus"], "high"), omit)
        except (ValueError, FileNotFoundError) as exc:
            raise ValueError(f"{config['path']}: practice.omit_steps {omit}: {exc}") from exc


class BodyTooLarge(ValueError):
    """The request body is larger than the route allows."""


# What the browser is told for each failure. The real reason goes to stderr instead: a flow error names the
# participant, their configuration file and their stage, none of which belongs in a participant's browser.
FLOW_MESSAGES = {400: "invalid request", 404: "not found",
                 409: "this session cannot be continued; please contact the researcher"}


class Handler(BaseHTTPRequestHandler):
    """Routes the participant flow, the scenario content endpoints and the two POSTs; everything else is rejected.

    The class attributes are the server's configuration; main() sets them from the command line, and a test sets
    them directly:
        db_path             participant database. None leaves every flow route answering 404.
        config, payload     the experiment configuration and the assignments payload the flow runs under.
        researcher_preview  True also serves the researcher routes (/ and /chat, and the s= form of the content
                            routes). False, the default, is the participant-facing server.
    """

    db_path = None
    config = None
    payload = None
    researcher_preview = False
    # /api/stage/complete takes a token, a stage key and, at consent, the agreement or, at an assessment, the answers
    # (the reasoning text is at most 4,000 characters, up to 4 bytes each in UTF-8).
    MAX_BODY_BYTES = 32768

    def _send(self, status, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, body):
        return self._send(status, json.dumps(body), "application/json")

    def _flow_ready(self):
        """True when this server was started with a participant database and an experiment configuration."""
        return self.db_path is not None and self.config is not None

    @contextlib.contextmanager
    def _conn(self):
        """A database connection for this one request.

        Requests are handled on separate threads and a sqlite3 connection may only be used on the thread that
        created it, so the connection is per request rather than per server. At this scale (one participant at a
        time, a handful of statements per request) opening the file costs nothing worth saving.
        """
        conn = store.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _flow_error(self, exc, as_json=True):
        """Answer a flow failure with a generic message, and write the real reason to stderr."""
        status = flow_status(exc)
        self.log_message("flow %s: %s", status, exc)
        message = FLOW_MESSAGES[status]
        return self._json(status, {"error": message}) if as_json else self._send(status, message, "text/plain")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.startswith("/p/"):
            return self._flow_page(url.path[len("/p/"):])
        if url.path == "/api/stage":
            return self._api_stage(q.get("t", ""))
        if url.path in ("/api/stimulus", "/api/workspace", "/api/skills", "/api/skill"):
            return self._api_content(url.path, q)
        if url.path.startswith("/static/") and url.path[len("/static/"):] in STATIC_FILES:
            name = url.path[len("/static/"):]
            return self._send(200, (UI_DIR / name).read_bytes(), STATIC_FILES[name])
        if self.researcher_preview:
            if url.path.startswith("/static/") and url.path[len("/static/"):] in RESEARCHER_STATIC_FILES:
                name = url.path[len("/static/"):]
                return self._send(200, (UI_DIR / name).read_bytes(), RESEARCHER_STATIC_FILES[name])
            if url.path == "/":
                return self._send(200, landing_page(), "text/html")
            if url.path == "/chat":
                return self._send(200, (UI_DIR / "index.html").read_text(encoding="utf-8"), "text/html")
        return self._send(404, "not found", "text/plain")

    def _flow_page(self, token):
        """Serve the flow page for a participant link.

        The token is resolved before the page is sent so a mistyped or revoked link is answered 404 rather than
        loading a page that then fails. The page itself holds no stage: flow.js asks /api/stage for it.
        """
        if not self._flow_ready():
            return self._send(404, "not found", "text/plain")
        try:
            with self._conn() as conn:
                flow.participant_for(conn, token)
        except (ValueError, FileNotFoundError) as exc:
            return self._flow_error(exc, as_json=False)
        return self._send(200, (UI_DIR / "flow.html").read_text(encoding="utf-8"), "text/html")

    def _api_stage(self, token):
        """The stage this participant is to be shown now, recording the arrival."""
        if not self._flow_ready():
            return self._json(404, {"error": "not found"})
        try:
            with self._conn() as conn:
                participant = flow.participant_for(conn, token)["participant_id"]
                body = flow.stage_payload(conn, self.config, participant, self.payload)
        except (ValueError, FileNotFoundError) as exc:
            return self._flow_error(exc)
        return self._json(200, body)

    def _resolve_content(self, q):
        """The (stimulus folder, condition, steps to omit) a content request is for.

        With t=<token> both come from the participant's current stage, so a participant cannot ask for another
        stimulus or the other condition. The s=/c= form names them directly and is a researcher preview: it is
        refused outright unless this server was started with --researcher-preview.
        """
        if q.get("t"):
            if not self._flow_ready():
                raise FileNotFoundError("flow not served")
            with self._conn() as conn:
                participant = flow.participant_for(conn, q["t"])["participant_id"]
                return flow.scenario_target(conn, self.config, participant, self.payload)
        if not self.researcher_preview:
            raise FileNotFoundError("not found")
        return q.get("s", ""), q.get("c", ""), ()  # the researcher preview shows a stimulus exactly as built

    def _api_content(self, path, q):
        """Serve one of the four scenario content routes for whichever stimulus the request resolves to."""
        try:
            stimulus, condition, omit = self._resolve_content(q)
        except store.StoreError as exc:
            return self._flow_error(exc)
        except (ValueError, FileNotFoundError) as exc:
            # A token problem and an unservable stage are both answered the way a missing page is.
            return self._flow_error(exc)
        try:
            if path == "/api/stimulus":
                view = flow.omit_trace_steps(participant_view(stimulus, condition), omit)
            elif path == "/api/workspace":
                view = participant_workspace(stimulus)
            elif path == "/api/skills":
                view = participant_skills(stimulus)
            else:
                view = participant_skill(stimulus, q.get("k", ""))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        except (FileNotFoundError, KeyError):
            return self._json(404, {"error": "not found"})
        return self._json(200, view)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/log":
            return self._post_log()
        if path == "/api/stage/complete":
            return self._post_advance()
        return self._send(404, "not found", "text/plain")

    def _body(self, limit):
        """The request body as parsed JSON, or raise ValueError (including when it is over `limit` bytes)."""
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            raise BodyTooLarge("body too large")
        return json.loads(self.rfile.read(length) or b"null")

    def _post_log(self):
        """Store one interaction event: {"token", "trial_id", "type", ...}.

        The token decides whose record the event joins, and participant_flow.log_event accepts it only for the trial
        that participant is on now. 400 for an event that is not an allowed record, 404 for an unknown token (and
        whenever no participant database is configured, as in a researcher preview), 409 for an event of another
        trial or over the per-trial limit. Nothing is sent back on success: the log is write-only for the browser.
        """
        if not self._flow_ready():
            return self._json(404, {"error": "not found"})
        try:
            event = self._body(ilog.MAX_EVENT_BYTES)
            if not isinstance(event, dict):
                raise ilog.LogError("event must be an object")
            token = event.pop("token", "")
            with self._conn() as conn:
                participant = flow.participant_for(conn, token)["participant_id"]
                flow.log_event(conn, self.config, participant, event, self.payload)
        except BodyTooLarge as exc:
            return self._json(413, {"error": str(exc)})
        except (flow.EventRefused, store.StoreError) as exc:
            self.log_message("log 409: %s", exc)
            return self._json(409, {"error": "not accepted"})
        except FileNotFoundError:
            return self._json(404, {"error": "not found"})
        except ValueError as exc:  # LogError is a ValueError, as are a malformed body and a malformed token
            return self._json(400, {"error": str(exc)})
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _post_advance(self):
        """Complete the participant's current stage and answer with the next one.

        The stage key is sent by the browser but checked against the participant's progress, so it can only ever
        complete the stage they are actually on: a replayed or doubled submission changes nothing and a later
        stage is refused (participant_store.complete_stage).
        """
        if not self._flow_ready():
            return self._json(404, {"error": "not found"})
        try:
            body = self._body(self.MAX_BODY_BYTES)
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
            stage_key = body.get("stage_key")
            if not isinstance(stage_key, str) or not stage_key:
                raise ValueError("stage_key is required")
            with self._conn() as conn:
                participant = flow.participant_for(conn, body.get("token", ""))["participant_id"]
                result = flow.advance(conn, self.config, participant, stage_key, self.payload,
                                      consent=body.get("consent"), answers=body.get("answers"))
        except (ValueError, FileNotFoundError) as exc:
            return self._flow_error(exc)
        return self._json(200, result)

    def log_message(self, fmt, *args):
        sys.stderr.write("[participant] " + (fmt % args) + "\n")


use_corpus()  # the study corpus is active until a caller (main, a test) switches it


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--experiment-config", type=Path, default=ep.DEVELOPMENT_CONFIG, dest="experiment_config",
                        help="experiment configuration the flow is served under (src/data/experiment_configs/*."
                             "json); study.json is the real study, the default is the development one")
    parser.add_argument("--db", type=Path, default=store.DEFAULT_DB,
                        help="participant database holding the enrolled links (participant_store.py)")
    parser.add_argument("--corpus", type=Path, default=None,
                        help="corpus file (src/data/corpora/*.json); default: the corpus the experiment "
                             "configuration names")
    parser.add_argument("--researcher-preview", action="store_true", dest="researcher_preview",
                        help="also serve the researcher routes (/ and /chat, and the s= form of the content "
                             "routes). Never use this for a server participants can reach")
    args = parser.parse_args()

    # Loading the configuration and the assignments here means a mistyped path or a mismatched simulated flag
    # stops the server at startup, rather than failing on a participant's first request.
    config = ep.load_config(args.experiment_config)
    payload = ep.load_assignments(config)
    flow.load_consent()  # a missing or malformed consent file stops the server here, not at a participant's start
    flow.load_assessment()  # likewise the assessment questions
    use_corpus(args.corpus if args.corpus is not None else config["corpus_path"])
    check_practice_presentation(config)
    Handler.db_path = args.db
    Handler.config = config
    Handler.payload = payload
    Handler.researcher_preview = args.researcher_preview

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    kind = "SIMULATED (not for participants)" if config["simulated"] else "STUDY"
    print(f"Experiment: {args.experiment_config} — {kind}")
    print(f"Corpus:     {CORPUS['path']}")
    print(f"Database:   {args.db}")
    if args.researcher_preview:
        print("Researcher preview routes are ON: / and /chat are served. Not for participants.")
    print(f"Serving on http://{args.host}:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
