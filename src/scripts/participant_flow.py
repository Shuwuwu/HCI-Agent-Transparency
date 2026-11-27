"""
Purpose:
    The participant-facing experiment flow: turn an opaque link token into the one stage that participant is to
    be shown now, and advance them one stage at a time. This is the only place that maps a token to a stimulus,
    so nothing a participant can edit (a URL, a form field, a request body) decides which scenario or
    transparency condition they are shown.

    The stage descriptors sent to the browser deliberately carry no stimulus folder, scenario name or transparency
    condition. The folder name encodes whether a trial is safe or injected (user_task_9__no_injection versus
    user_task_9__g2_ii_wg), so a participant reading the page source or the network panel would learn what to
    expect from the trial; the condition would tell them that other participants see something different. Both are
    resolved on the server and only the resulting content is sent.

    Consent. The consent stage cannot be passed by clicking through: completing it requires the agreement and the
    hash of the consent text the page showed, and the agreement is stored in the same transaction as the stage
    completion (participant_store.record_consent). Anyone past the consent stage therefore has a consent record.

Inputs:
    A participant database connection (participant_store.py), an experiment configuration and the assignments
    payload it names (experiment_plan.py), and a link token taken from the request.
    src/study_materials/assessment.json: the questions asked after each conversation (see load_assessment), read
    on every request like the consent text.
    src/study_materials/consent.md: the consent text, in Markdown, whose last line is the agreement checkbox
    written as a task-list item ("- [ ] I agree to ..."). It is read on every request, so the text served and the
    text recorded are always the current file.

    Assessment. Every assessment stage, practice and experimental alike, asks the same four questions in the same
    order: whether the assistant performed the task safely, confidence (1-5, no default), the reasoning, and the
    information sources that influenced the judgment. The sources offered are those the participant's view
    actually contains (the assistant's activity only where the action log is shown). The answers are validated
    here, never trusted from the browser, and stored in the same transaction as the stage completion
    (participant_store.record_response), against the participant id and the trial id, with the practice flagged.

    Questionnaire. The post-study questionnaire (src/study_materials/questionnaire.json, read on every request
    like the other study materials) is sent with the questionnaire stage; its answers are validated here and
    stored in the same transaction as the stage completion (participant_store.record_questionnaire). Every
    participant gets the same questionnaire, whatever their condition.

    Practice presentation. The configuration may hide some action-log cards of the practice stimulus
    (practice.omit_steps, with its reason in practice.omit_reason). They are removed, and the rest renumbered, when
    the practice stage is served (scenario_target, omit_trace_steps); the stimulus files and the execution record
    are unchanged, and experimental trials are always served exactly as built.

    Interaction events. The browser posts each event with the participant's link token and the trial id it
    belongs to; log_event accepts it only for the trial the participant is on now (its scenario or, for events
    sent while the page moves on, its assessment), so no page can write into another participant's or another
    trial's record. The server adds two events itself: `session_start` the first time a trial's scenario is
    shown, and `stage_load` every time the page asks for the current stage, which makes reloads and resumed
    sessions visible. Every event row carries the practice flag of its stage, so practice events (including
    everything clicked during the practice tour) are told apart from experimental ones by a column, not a folder.

Outputs:
    Stage descriptors and progress counts for the browser (no stimulus identity; the consent stage also carries
    the consent text, its checkbox statement and its hash); the (stimulus folder, condition) pair the scenario
    endpoints resolve on the server; a stored consent when the consent stage is completed; and interaction events
    in the participant database (the trial's stimulus and condition are in its plan_stages rows, not repeated in
    the events).

Dependencies:
    participant_store.py (participant records and stage progress), experiment_plan.py (the derived stage
    sequence), interaction_log.py (event validation). Used by participant_server.py.

Usage:
    Imported; it has no command line of its own. The flow is served by:
        python3 -B src/scripts/participant_server.py \
            --experiment-config src/data/experiment_configs/development.json
"""

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import experiment_plan as ep
import interaction_log as ilog
import participant_store as store

CONSENT_PATH = ep.ROOT / "src" / "study_materials" / "consent.md"
ASSESSMENT_PATH = ep.ROOT / "src" / "study_materials" / "assessment.json"
QUESTIONNAIRE_PATH = ep.ROOT / "src" / "study_materials" / "questionnaire.json"
QUESTIONNAIRE_TYPES = ("scale", "choice", "text")
QUESTIONS = ("performed_safely", "confidence", "reasoning", "sources")  # the order they are asked in
# Which conditions show each optional part of the interface; a source marked "shown_with" one of these parts is
# offered only to participants whose view contains it.
SHOWN_WITH = {"activity_log": ("high",)}
CHECKBOX_RE = re.compile(r"^- \[ \] (\S.*)$")  # the agreement line that ends the consent file


class ConsentError(ValueError):
    """The consent stage was submitted without a valid agreement to the current consent text."""


def load_consent(path=CONSENT_PATH) -> dict:
    """The consent material: {"markdown", "checkbox", "sha256", "text"}.

    `text` is the whole file and `sha256` its hash: that is what a participant's consent records. `markdown` is the
    file without its last line, which must be the agreement checkbox written as a task-list item
    ("- [ ] I agree to ..."); `checkbox` is that statement, shown next to the real checkbox. Raises ValueError if
    the last line is not such an item, and OSError if the file cannot be read.
    """
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.rstrip().split("\n")
    match = CHECKBOX_RE.match(lines[-1].strip())
    if not match:
        raise ValueError(f"{path}: the last line must be the agreement checkbox, '- [ ] <statement>'")
    return {"markdown": "\n".join(lines[:-1]).rstrip(), "checkbox": match.group(1).strip(),
            "sha256": hashlib.sha256(raw).hexdigest(), "text": text}


class AssessmentError(ValueError):
    """An assessment was submitted with a missing, malformed or unavailable answer."""


def load_assessment(path=ASSESSMENT_PATH) -> dict:
    """The assessment questions: the parsed file plus its "sha256", which each stored response records.

    Raises ValueError if a question is missing, the confidence scale is not a range of whole numbers with anchors
    inside it, an option list is empty or repeats an id, or a source names an unknown "shown_with" part, so a
    broken file stops the server at startup (participant_server.main) instead of at a participant's first trial.
    """
    raw = Path(path).read_bytes()
    spec = json.loads(raw)
    missing = [q for q in QUESTIONS if q not in spec]
    if missing:
        raise ValueError(f"{path}: missing questions {missing}")
    scale = spec["confidence"]
    if not (isinstance(scale.get("min"), int) and isinstance(scale.get("max"), int) and scale["min"] < scale["max"]
            and all(scale["min"] <= int(k) <= scale["max"] for k in scale.get("anchors", {}))):
        raise ValueError(f"{path}: confidence needs whole-number min < max and anchors inside that range")
    for question in ("performed_safely", "sources"):
        ids = [option["id"] for option in spec[question].get("options", [])]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError(f"{path}: {question} needs options with distinct ids")
    unknown = {o["shown_with"] for o in spec["sources"]["options"] if "shown_with" in o} - set(SHOWN_WITH)
    if unknown:
        raise ValueError(f"{path}: unknown shown_with {sorted(unknown)}")
    specify = [o for o in spec["sources"]["options"] if o.get("specify")]
    if len(specify) > 1 or any(not isinstance(o.get("max_chars"), int) or o["max_chars"] < 1
                               or not isinstance(o.get("specify_required", True), bool) for o in specify):
        raise ValueError(f"{path}: at most one source may ask to specify; it needs a positive max_chars and, if "
                         "given, a true/false specify_required")
    spec["sha256"] = hashlib.sha256(raw).hexdigest()
    return spec


def offered_sources(spec: dict, condition: str) -> list[dict]:
    """The information-source options of `spec` a participant in `condition` sees, in the file's order."""
    return [o for o in spec["sources"]["options"]
            if "shown_with" not in o or condition in SHOWN_WITH[o["shown_with"]]]


def assessment_for(stage: dict, spec: dict | None = None) -> dict:
    """The assessment as sent to the browser for one assessment stage: the questions in order, with only the
    sources this stage's participant can see, and the hash the submission must echo. Carries no condition."""
    spec = spec or load_assessment()
    sources = {**spec["sources"], "options": [
        {"id": o["id"], "label": o["label"],
         **({"specify": True, "specify_required": o.get("specify_required", True), "max_chars": o["max_chars"]}
            if o.get("specify") else {})}
        for o in offered_sources(spec, stage["condition"])]}
    return {"intro": spec["intro"], "sha256": spec["sha256"],
            "questions": [{"id": q, **(sources if q == "sources" else spec[q])} for q in QUESTIONS]}


def validate_answers(answers, spec: dict, condition: str) -> dict:
    """The submitted answers, checked and normalised, or AssessmentError.

    `answers` is what the browser sent: {"performed_safely", "confidence", "reasoning", "sources", "sources_other",
    "sha256"}.
    performed_safely must be one of the options; confidence a whole number on the scale (a bool, a string or a
    missing value is refused, so nothing is ever recorded by default); reasoning text no longer than max_chars
    (surrounding whitespace removed); sources distinct ids from the sources offered in `condition`, returned in
    the order offered; sources_other the description of the source marked "specify" ("Other"): required when
    that source is chosen only if it is marked "specify_required" (the default; the shipped questions set it to
    false, so "Other" is saved with or without a description), and refused when the source is not chosen, so a
    stored description always belongs to a chosen "Other". A required question left unanswered is refused. The hash must match the current questions,
    so an answer is never stored against wording the participant did not see.
    """
    if not isinstance(answers, dict):
        raise AssessmentError("answers must be an object")
    if answers.get("sha256") != spec["sha256"]:
        raise AssessmentError("the questions changed after the page was loaded")
    safely = answers.get("performed_safely")
    if safely is None and not spec["performed_safely"]["required"]:
        safely = ""
    elif safely not in {o["id"] for o in spec["performed_safely"]["options"]}:
        raise AssessmentError("performed_safely must be one of the options")
    scale, confidence = spec["confidence"], answers.get("confidence")
    if not (isinstance(confidence, int) and not isinstance(confidence, bool)
            and scale["min"] <= confidence <= scale["max"]):
        raise AssessmentError(f"confidence must be a whole number from {scale['min']} to {scale['max']}")
    reasoning = answers.get("reasoning", "")
    if not isinstance(reasoning, str):
        raise AssessmentError("reasoning must be text")
    reasoning = reasoning.strip()
    if spec["reasoning"]["required"] and not reasoning:
        raise AssessmentError("reasoning is required")
    if len(reasoning) > spec["reasoning"]["max_chars"]:
        raise AssessmentError("reasoning is too long")
    chosen = answers.get("sources", [])
    offered = [o["id"] for o in offered_sources(spec, condition)]
    if (not isinstance(chosen, list) or len(set(map(str, chosen))) != len(chosen)
            or not set(chosen) <= set(offered)):
        raise AssessmentError("sources must be distinct ids from the sources offered")
    if spec["sources"]["required"] and not chosen:
        raise AssessmentError("at least one source is required")
    other = answers.get("sources_other", "")
    if not isinstance(other, str):
        raise AssessmentError("sources_other must be text")
    other = other.strip()
    specify = next((o for o in offered_sources(spec, condition) if o.get("specify")), None)
    if specify and specify["id"] in chosen:
        if not other and specify.get("specify_required", True):
            raise AssessmentError(f"{specify['id']} needs the other information specified")
        if len(other) > specify["max_chars"]:
            raise AssessmentError("the other information is too long")
    elif other:
        raise AssessmentError("sources_other is only accepted with the source that asks to specify")
    return {"performed_safely": safely, "confidence": confidence, "reasoning": reasoning,
            "sources": [s for s in offered if s in chosen], "sources_other": other, "sources_offered": offered}


class QuestionnaireError(ValueError):
    """The post-study questionnaire was submitted with a missing, malformed or unknown answer."""


def load_questionnaire(path=QUESTIONNAIRE_PATH) -> dict:
    """The post-study questionnaire: the parsed file plus its "sha256", which each stored answer records.

    Items are {"id", "type", "text", "required"} plus, by type: "scale" a whole-number "min" < "max" with optional
    "anchors" inside that range and an optional "not_applicable" {"id", "label"}, an answer off the scale (such as
    "Did not consult"); "choice" a list of "options" with distinct ids; "text" a positive "max_chars".
    Raises ValueError for anything else (or repeated item ids), so a broken file stops the server at startup.
    """
    raw = Path(path).read_bytes()
    spec = json.loads(raw)
    items = spec.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError(f"{path}: needs a non-empty items list")
    ids = [item.get("id") for item in items]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and re.match(r"^[a-z0-9_]{1,64}$", i) for i in ids):
        raise ValueError(f"{path}: item ids must be distinct lower-case identifiers")
    for item in items:
        kind = item.get("type")
        if kind not in QUESTIONNAIRE_TYPES or not isinstance(item.get("required"), bool) or not item.get("text"):
            raise ValueError(f"{path}: item {item['id']} needs a type of {QUESTIONNAIRE_TYPES}, text and required")
        if kind == "scale" and not (isinstance(item.get("min"), int) and isinstance(item.get("max"), int)
                                    and item["min"] < item["max"]
                                    and all(item["min"] <= int(k) <= item["max"] for k in item.get("anchors", {}))):
            raise ValueError(f"{path}: scale {item['id']} needs whole-number min < max and anchors inside it")
        if kind == "scale" and "not_applicable" in item:
            na = item["not_applicable"]
            # A string id so it can never be mistaken for a point on the scale.
            if not (isinstance(na, dict) and isinstance(na.get("id"), str) and re.match(r"^[a-z_]{1,64}$", na["id"])
                    and na.get("label")):
                raise ValueError(f"{path}: scale {item['id']} not_applicable needs an id of lower-case letters and "
                                 f"a label")
        if kind == "choice":
            options = [o.get("id") for o in item.get("options", [])]
            if not options or len(set(options)) != len(options):
                raise ValueError(f"{path}: choice {item['id']} needs options with distinct ids")
        if kind == "text" and not (isinstance(item.get("max_chars"), int) and item["max_chars"] > 0):
            raise ValueError(f"{path}: text {item['id']} needs a positive max_chars")
    spec["sha256"] = hashlib.sha256(raw).hexdigest()
    return spec


def questionnaire_for(spec: dict | None = None) -> dict:
    """The questionnaire as sent to the browser: intro, items, whether it is still a placeholder, and the hash the
    submission must echo. The same for every participant."""
    spec = spec or load_questionnaire()
    return {"intro": spec.get("intro", ""), "placeholder": bool(spec.get("placeholder")), "sha256": spec["sha256"],
            "items": spec["items"]}


def validate_questionnaire(answers, spec: dict) -> dict:
    """The submitted questionnaire answers, checked and normalised to {item id: text}, or QuestionnaireError.

    `answers` is {"sha256", "items": {item id: answer}}. A scale answer must be a whole number on its scale (or,
    where the scale has one, its not_applicable id, stored as that id), a choice one of its option ids, a text at most max_chars (surrounding whitespace removed). A required item left
    unanswered is refused; an optional one is stored as ''. Unknown item ids are refused, and the hash must match
    the current file, so an answer is never stored against wording the participant did not see.
    """
    if not isinstance(answers, dict) or answers.get("sha256") != spec["sha256"]:
        raise QuestionnaireError("the questionnaire changed after the page was loaded")
    given = answers.get("items")
    if not isinstance(given, dict):
        raise QuestionnaireError("items must be an object")
    known = {item["id"]: item for item in spec["items"]}
    if set(given) - set(known):
        raise QuestionnaireError(f"unknown items {sorted(set(given) - set(known))}")
    out = {}
    for item_id, item in known.items():
        value = given.get(item_id)
        if value is None or value == "":
            if item["required"]:
                raise QuestionnaireError(f"{item_id} is required")
            out[item_id] = ""
        elif item["type"] == "scale" and value == item.get("not_applicable", {}).get("id"):
            out[item_id] = value
        elif item["type"] == "scale":
            if not (isinstance(value, int) and not isinstance(value, bool) and item["min"] <= value <= item["max"]):
                raise QuestionnaireError(f"{item_id} must be a whole number from {item['min']} to {item['max']}")
            out[item_id] = str(value)
        elif item["type"] == "choice":
            if value not in {o["id"] for o in item["options"]}:
                raise QuestionnaireError(f"{item_id} must be one of the options")
            out[item_id] = value
        else:
            if not isinstance(value, str) or len(value.strip()) > item["max_chars"]:
                raise QuestionnaireError(f"{item_id} must be text of at most {item['max_chars']} characters")
            if item["required"] and not value.strip():
                raise QuestionnaireError(f"{item_id} is required")
            out[item_id] = value.strip()
    return out


# Shape of a link token as participant_store.enrol writes it (secrets.token_urlsafe): URL-safe base64 characters.
# Checked before the database is touched so a malformed path cannot become a query.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def participant_for(conn: sqlite3.Connection, token: str) -> dict:
    """The participant record whose link token this is.

    Raises ValueError for a token that is not even the right shape (never a database lookup) and
    FileNotFoundError for a well-formed token that belongs to nobody, so a guessed link is answered the same way
    as a missing page.
    """
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        raise ValueError("invalid token")
    row = store.by_token(conn, token)
    if row is None:
        raise FileNotFoundError("unknown token")
    return row


def _plan_and_stage(conn: sqlite3.Connection, config: ep.Config, participant: str,
                    payload: dict | None = None) -> tuple[dict, dict | None, list[str]]:
    """(plan, current stage or None, completed stage keys) for one participant.

    The plan is verified against the fingerprint stored at enrolment, so a configuration or assignment change
    since this participant started raises participant_store.StoreError rather than resuming them into a
    different experiment.
    """
    plan = store.checked_plan(conn, config, participant, payload)
    done = store.completed_stages(conn, participant)
    stage = next((s for s in plan["stages"] if s["key"] not in set(done)), None)
    return plan, stage, done


def descriptor(stage: dict, plan: dict) -> dict:
    """The participant-safe description of one stage.

    Carries what the browser needs to draw a screen — the stage key, its kind, its position, and for a trial the
    trial id (which is also the interaction-log session id) and whether it is the practice trial. It deliberately
    omits the stimulus folder, the scenario name and the transparency condition: see the module docstring.
    """
    out = {"key": stage["key"], "kind": stage["kind"], "index": stage["index"]}
    if "trial_id" in stage:
        out["trial_id"] = stage["trial_id"]
        out["practice"] = bool(stage.get("practice"))
        if not stage.get("practice"):
            out["trial"] = stage.get("trial")
            out["trials"] = sum(1 for s in plan["stages"] if s["kind"] == "scenario" and not s.get("practice"))
    return out


def _progress(plan: dict, done: list[str]) -> dict:
    """How far through the sequence the participant is, for a progress indicator."""
    return {"completed": len(done), "total": len(plan["stages"])}


def _server_event(conn: sqlite3.Connection, participant: str, stage: dict, kind: str) -> None:
    """Store an event the server produces itself (session_start, stage_load) against `stage`.

    Validation needs a trial id; a stage outside any trial (consent, the questionnaire) is stored with NULL.
    """
    store.record_event(conn, participant, stage["key"], stage.get("trial_id"), bool(stage.get("practice")),
                       ilog.validate_event({"trial_id": stage.get("trial_id") or "none", "type": kind}, "server"),
                       "server")


def _enter(conn: sqlite3.Connection, config: ep.Config, participant: str, payload: dict | None,
           plan: dict, stage: dict) -> dict:
    """Record the participant's arrival at `stage`, record a trial's session_start, and return its descriptor.

    Both ways a participant can reach a stage go through here. A participant reaches almost every stage by
    continuing from the previous one rather than by reopening their link, so recording the arrival only on a
    page load would lose the arrival time of nearly every trial — and with it the reading and response times
    measured from it — and would leave those trials with no session_start.
    """
    # Whether this is the first arrival must be read before enter_stage inserts the row.
    first = conn.execute("SELECT 1 FROM progress WHERE participant_id = ? AND stage_key = ?",
                         (participant, stage["key"])).fetchone() is None
    store.enter_stage(conn, config, participant, payload)
    if first and stage["kind"] == "scenario":
        _server_event(conn, participant, stage, "session_start")
    out = descriptor(stage, plan)
    if stage["kind"] == "consent":
        material = load_consent()
        out["consent"] = {key: material[key] for key in ("markdown", "checkbox", "sha256")}
    if stage["kind"] == "assessment":
        out["assessment"] = assessment_for(stage)
    if stage["kind"] == "questionnaire":
        out["questionnaire"] = questionnaire_for()
    return out


def stage_payload(conn: sqlite3.Connection, config: ep.Config, participant: str,
                  payload: dict | None = None) -> dict:
    """Enter and describe the stage this participant is to be shown now, recording a `stage_load` event.

    Returns {"stage": descriptor or None, "progress": {...}}; the stage is None once every stage has been
    completed. Called on every page load of the flow, so it is also the resume path: it always returns the same
    stage until that stage is completed, and the arrival time it records is the first one.
    """
    plan, stage, done = _plan_and_stage(conn, config, participant, payload)
    if stage is None:
        return {"stage": None, "progress": _progress(plan, done)}
    out = _enter(conn, config, participant, payload, plan, stage)
    _server_event(conn, participant, stage, "stage_load")
    return {"stage": out, "progress": _progress(plan, done)}


class EventRefused(ValueError):
    """An interaction event for a trial the participant is not on now."""


def log_event(conn: sqlite3.Connection, config: ep.Config, participant: str, event,
              payload: dict | None = None) -> dict:
    """Validate and store one interaction event the browser posted for `participant`; returns the stored row.

    `event` is the posted body without the token: {"trial_id", "type", "client_ms"?, ...}. Raises
    interaction_log.LogError for an event that is not an allowed record, EventRefused when its trial is not the
    one the participant is on now (the scenario stage or the assessment that follows it: the page sends the last
    viewing times as it moves on), and participant_store.StoreError when the trial already holds
    MAX_EVENTS_PER_TRIAL events. The stage, trial and practice flag stored with the event come from the plan.
    """
    clean = ilog.validate_event(event)
    _, stage, _ = _plan_and_stage(conn, config, participant, payload)
    if stage is None or stage.get("trial_id") != clean["trial_id"]:
        raise EventRefused("the event is not for the participant's current trial")
    return store.record_event(conn, participant, stage["key"], stage["trial_id"], bool(stage.get("practice")),
                              clean, "browser", limit=ilog.MAX_EVENTS_PER_TRIAL)


def scenario_target(conn: sqlite3.Connection, config: ep.Config, participant: str,
                    payload: dict | None = None) -> tuple[str, str, tuple[int, ...]]:
    """The (stimulus folder, condition, steps to omit) the scenario endpoints are to serve for this participant now.

    The steps to omit are the configuration's practice.omit_steps on the practice stage and () on every other
    stage, so an experimental trial is always shown its stimulus exactly as built (see omit_trace_steps).

    Raises FileNotFoundError when the participant's current stage shows no scenario — consent, an
    assessment, the questionnaire, or the end of the sequence — so a stage that is not a scenario cannot be used
    to fetch scenario content, and a participant cannot reach the next trial's content early or the previous
    trial's content again.
    """
    _, stage, _ = _plan_and_stage(conn, config, participant, payload)
    if stage is None or stage["kind"] != "scenario":
        raise FileNotFoundError("no scenario at this stage")
    omit = tuple(config["practice"].get("omit_steps", ())) if stage.get("practice") else ()
    return stage["stimulus"], stage["condition"], omit


def omit_trace_steps(view: dict, steps) -> dict:
    """A copy of a participant view without the action-log cards numbered `steps`, the rest renumbered from 1.

    `view` is what participant_server.participant_view returns (its trace holds action entries only; any other
    entry type would be kept in place unnumbered). A view without a trace (the low condition) and an empty
    `steps` come back unchanged. The stimulus files are never touched: this is a presentation change applied when
    the practice is served (scenario_target), so the files keep matching a fresh build of the execution record.
    Raises ValueError if a step to omit is not an action of the view, so a mistyped configuration cannot hide the
    wrong card silently.
    """
    steps = set(steps)
    if not steps or "trace" not in view:
        return view
    present = {entry["step"] for entry in view["trace"] if entry["type"] == "action"}
    if not steps <= present:
        raise ValueError(f"steps {sorted(steps - present)} are not actions of this stimulus")
    trace, number = [], 0
    for entry in view["trace"]:
        if entry["type"] == "action":
            if entry["step"] in steps:
                continue
            number += 1
            entry = {**entry, "step": number}
        trace.append(entry)
    return {**view, "trace": trace}


def _complete_consent(conn: sqlite3.Connection, config: ep.Config, participant: str, stage_key: str,
                      payload: dict | None, consent) -> None:
    """Record the participant's agreement and complete the consent stage, both or neither.

    `consent` is what the browser sent: {"agreed": true, "sha256": <hash of the consent text it showed>}. Raises
    ConsentError if the box was not ticked, or if the text has changed since the page was loaded — recording an
    agreement to a text the participant never saw would be worse than asking them to reload.
    """
    material = load_consent()
    if not isinstance(consent, dict) or consent.get("agreed") is not True:
        raise ConsentError("the consent checkbox was not ticked")
    if consent.get("sha256") != material["sha256"]:
        raise ConsentError("the consent text changed after the page was loaded")
    try:
        store.record_consent(conn, participant, material["sha256"], material["text"], commit=False)
        store.complete_stage(conn, config, participant, stage_key, payload)  # commits both
    except Exception:
        conn.rollback()  # never a consent without the stage, or the stage without a consent
        raise


def _complete_assessment(conn: sqlite3.Connection, config: ep.Config, participant: str, stage: dict,
                         payload: dict | None, answers) -> None:
    """Store the participant's answers and complete the assessment stage, both or neither.

    The response is keyed by participant and stage and carries the stage's trial id, practice flag, stimulus and
    condition from the plan, so nothing that identifies the trial comes from the browser. Raises AssessmentError
    for answers validate_answers refuses.
    """
    spec = load_assessment()
    clean = validate_answers(answers, spec, stage["condition"])
    response = {"trial_id": stage["trial_id"], "practice": bool(stage.get("practice")), "stimulus": stage["stimulus"],
                "condition": stage["condition"], "questions_sha256": spec["sha256"], **clean}
    try:
        store.record_response(conn, participant, stage["key"], response, commit=False)
        store.complete_stage(conn, config, participant, stage["key"], payload)  # commits both
    except Exception:
        conn.rollback()  # never an answer without the stage, or the stage without an answer
        raise


def _complete_questionnaire(conn: sqlite3.Connection, config: ep.Config, participant: str, stage_key: str,
                            payload: dict | None, answers) -> None:
    """Store the questionnaire answers and complete the questionnaire stage, both or neither. Raises
    QuestionnaireError for answers validate_questionnaire refuses."""
    spec = load_questionnaire()
    clean = validate_questionnaire(answers, spec)
    try:
        store.record_questionnaire(conn, participant, clean, spec["sha256"], commit=False)
        store.complete_stage(conn, config, participant, stage_key, payload)  # commits both
    except Exception:
        conn.rollback()
        raise


def advance(conn: sqlite3.Connection, config: ep.Config, participant: str, stage_key: str,
            payload: dict | None = None, consent=None, answers=None) -> dict:
    """Complete `stage_key`, enter the next stage, and return it the same shape stage_payload returns it.

    The stage key must be the participant's current stage: participant_store.complete_stage ignores a repeated
    submission of a stage already completed (so a double click or a reloaded form cannot record a trial twice)
    and refuses a later one (so no stage can be skipped). Completing the consent stage also needs `consent`
    (see _complete_consent), and completing an assessment or the questionnaire stage needs `answers` (see
    _complete_assessment, _complete_questionnaire); a
    repeated submission of a stage already completed needs neither, and changes nothing. The next stage is entered here because the answer to this call is what the browser draws — it does
    not ask for the stage again — so this is the participant's arrival at it.
    """
    plan = store.checked_plan(conn, config, participant, payload)
    done = set(store.completed_stages(conn, participant))
    stage = next((s for s in plan["stages"] if s["key"] not in done), None)
    # Answers are looked at only for the participant's current stage; any other key goes straight to
    # complete_stage, which ignores a stage already completed and refuses a later one (409) before anything
    # submitted with it is validated.
    pending = stage is not None and stage["key"] == stage_key
    if pending and stage["kind"] == "consent":
        _complete_consent(conn, config, participant, stage_key, payload, consent)
    elif pending and stage["kind"] == "assessment":
        _complete_assessment(conn, config, participant, stage, payload, answers)
    elif pending and stage["kind"] == "questionnaire":
        _complete_questionnaire(conn, config, participant, stage_key, payload, answers)
    else:
        store.complete_stage(conn, config, participant, stage_key, payload)
    plan, stage, done = _plan_and_stage(conn, config, participant, payload)
    return {"stage": _enter(conn, config, participant, payload, plan, stage) if stage else None,
            "progress": _progress(plan, done)}
