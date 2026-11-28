"""
Purpose:
    Shared helper for the tests of researcher data access: walks simulated participants through the experiment
    flow in-process (participant_flow.stage_payload / advance / log_event, the same calls the server makes), so
    a test database holds realistic records — consent, progress, answers, events, questionnaire — without a
    server. Not a test module.

Inputs:
    The development configuration and its assignments (read-only); a database path chosen by the caller.
    RESERVE_PAYLOAD is those assignments with one reserve participant per original (build_assignments
    .mirror_reserves), built in memory whatever the file itself holds.

Outputs:
    Python helpers only.

Dependencies:
    src/scripts/participant_flow.py, participant_store.py, experiment_plan.py, build_assignments.py.

Usage:
    from study_data_fixtures import CONFIG, PAYLOAD, new_database, walk      (tests add tests/ to sys.path)
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import build_assignments as ba  # noqa: E402
import experiment_plan as ep  # noqa: E402
import participant_flow as flow  # noqa: E402
import participant_store as store  # noqa: E402

CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
PAYLOAD = ep.load_assignments(CONFIG)
PARTICIPANTS = [row["participant"] for row in PAYLOAD["assignments"]]
_ORIGINALS = [row for row in PAYLOAD["assignments"] if row.get(ep.RESERVE_KEY) is None]
RESERVE_PAYLOAD = {**PAYLOAD, "assignments": _ORIGINALS + ba.mirror_reserves(_ORIGINALS)}


def new_database(folder: Path | None = None) -> tuple:
    """A new development database in a temporary folder with every development participant enrolled.
    Returns (connection, path)."""
    path = Path(folder or tempfile.mkdtemp()) / "development.sqlite3"
    conn = store.connect(path, "development")
    store.enrol_all(conn, CONFIG, PAYLOAD)
    return conn, path


def submission(stage: dict) -> dict:
    """What the page would send to complete `stage`: the consent agreement, a trial's assessment answers or
    questionnaire answers (valid ones), or nothing for the other stages (the practice questions are display-only,
    so the page sends nothing with them)."""
    if stage["kind"] == "consent":
        return {"consent": {"agreed": True, "sha256": stage["consent"]["sha256"]}}
    if stage["kind"] == "assessment" and not stage["practice"]:
        questions = {q["id"]: q for q in stage["assessment"]["questions"]}
        return {"answers": {"task_completion": "unsure", "performed_safely": "no", "confidence": 2,
                            "reasoning": "Fixture answer.",
                            "sources": [questions["sources"]["options"][0]["id"]],
                            "sha256": stage["assessment"]["sha256"]}}
    if stage["kind"] == "questionnaire":
        items = {}
        for item in stage["questionnaire"]["items"]:
            if item["type"] == "scale":
                items[item["id"]] = item["max"]
            elif item["type"] == "choice":
                items[item["id"]] = item["options"][-1]["id"]
            elif item["required"]:
                items[item["id"]] = "Fixture text."
        return {"answers": {"sha256": stage["questionnaire"]["sha256"], "items": items}}
    return {}


def walk(conn, participant: str, until: str | None = None, events: bool = True, payload: dict = PAYLOAD) -> list[str]:
    """Advance `participant` the way the page does until the stage `until` is current (or the end); returns the
    stage keys completed. With `events`, each scenario gets a few browser events (a workspace tab chosen and
    viewed, a card expanded and viewed, the visible time), sent while it is the current stage. `payload` is the
    assignments the participant was enrolled from (RESERVE_PAYLOAD for a reserve participant)."""
    walked = []
    stage = flow.stage_payload(conn, CONFIG, participant, payload)["stage"]
    while stage is not None and stage["key"] != until:
        if events and stage["kind"] == "scenario":
            for event in ({"type": "workspace_tab_open", "tab": "calendar"},
                          {"type": "workspace_tab_view", "tab": "calendar", "duration_ms": 1500},
                          {"type": "trace_card_expand", "card_id": "step-1"},
                          {"type": "trace_card_view", "card_id": "step-1", "duration_ms": 700},
                          {"type": "scenario_visible", "duration_ms": 9000}):
                flow.log_event(conn, CONFIG, participant, {"trial_id": stage["trial_id"], **event}, payload)
        extra = submission(stage)
        walked.append(stage["key"])
        stage = flow.advance(conn, CONFIG, participant, stage["key"], payload, consent=extra.get("consent"),
                             answers=extra.get("answers"))["stage"]
    return walked
