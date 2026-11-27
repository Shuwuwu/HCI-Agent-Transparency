"""Tests of participant_flow.py and the participant routes of participant_server.py, over real HTTP.

Three things are checked, and nothing else: that no participant-facing response reveals which stimulus or which
transparency condition a trial holds (the folder name says whether a trial is injected, so leaking it would tell
a participant what to expect), that the flow cannot lose, duplicate or reorder a trial, and that no participant
gets past the consent stage without their agreement to the exact consent text being recorded. The assessment and
questionnaire tests also check that every answer is validated and stored, including that the questionnaire
item asked only alongside the activity log is asked of, and accepted from, only those participants.

Each test server is its own Handler subclass rather than a mutation of participant_server.Handler, whose class
attributes are global: the tests can then run in any order alongside tests/test_participant_server.py.
"""

import json
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import experiment_plan as ep  # noqa: E402
import participant_flow as flow  # noqa: E402
import participant_server as ps  # noqa: E402
import participant_store as store  # noqa: E402

CONFIG = ep.load_config(ep.DEVELOPMENT_CONFIG)
PAYLOAD = ep.load_assignments(CONFIG)
CONTENT_ROUTES = ("/api/stimulus", "/api/workspace", "/api/skills", "/api/skill")


def participant_with(condition):
    """The first participant of the development assignments in `condition` ("low" or "high")."""
    return next(row["participant"] for row in PAYLOAD["assignments"] if row["condition"] == condition)


class FlowServer:
    """A server on a throwaway database with every development participant enrolled.

    `tokens` maps participant id to link token. `handler` is the Handler subclass actually serving, so a test can
    build a variant of it (preview on, a different configuration) without touching the shared class.
    """

    def __init__(self, config=CONFIG, researcher_preview=False):
        self.folder = Path(tempfile.mkdtemp())
        conn = store.connect(self.folder / "participants.db")
        self.tokens = {row["participant_id"]: row["token"] for row in store.enrol_all(conn, CONFIG, PAYLOAD)}
        conn.close()

        class Handler(ps.Handler):
            db_path = self.folder / "participants.db"

            def log_message(self, fmt, *args):
                pass  # the access log and the flow reasons would bury the test output

        Handler.config = config
        Handler.payload = PAYLOAD
        Handler.researcher_preview = researcher_preview
        self.handler = Handler
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    # -- convenience on top of the routes --
    def stage(self, participant):
        status, body = self.get(f"/api/stage?t={self.tokens[participant]}")
        assert status == 200, (status, body)
        return json.loads(body)

    def complete(self, participant, stage_key, **extra):
        return self.post("/api/stage/complete",
                         {"token": self.tokens[participant], "stage_key": stage_key, **extra})

    def agree(self, participant):
        """Tick the consent box and continue, the way the consent page does: the agreement carries the hash of
        the consent text the stage was served with."""
        stage = self.stage(participant)["stage"]
        assert stage["kind"] == "consent", stage
        return self.complete(participant, stage["key"],
                             consent={"agreed": True, "sha256": stage["consent"]["sha256"]})

    @staticmethod
    def answers_for(stage, **overrides):
        """A complete, valid set of answers for an assessment stage, as the form would send them."""
        questions = {q["id"]: q for q in stage["assessment"]["questions"]}
        answers = {"performed_safely": questions["performed_safely"]["options"][0]["id"], "confidence": 3,
                   "reasoning": "A test answer.", "sources": [questions["sources"]["options"][0]["id"]],
                   "sha256": stage["assessment"]["sha256"]}
        answers.update(overrides)
        return answers

    @staticmethod
    def questionnaire_answers(stage, **overrides):
        """A complete, valid questionnaire submission for the questionnaire stage, as the form would send it."""
        items = {}
        for item in stage["questionnaire"]["items"]:
            if item["type"] == "scale":
                items[item["id"]] = item["min"]
            elif item["type"] == "choice":
                items[item["id"]] = item["options"][0]["id"]
            elif item["required"]:
                items[item["id"]] = "A test answer."
        items.update(overrides)
        return {"sha256": stage["questionnaire"]["sha256"], "items": items}

    def answer(self, participant, **overrides):
        """Answer the participant's current assessment stage the way the form does."""
        stage = self.stage(participant)["stage"]
        assert stage["kind"] == "assessment", stage
        return self.complete(participant, stage["key"], answers=self.answers_for(stage, **overrides))

    def walk(self, participant, until=None):
        """Advance the way the browser does, stopping when `until` is the current stage (or the sequence ends).

        One GET to find where the participant is, then POST after POST: each answer carries the next stage, and
        the page draws it without asking for it again. Walking by re-fetching /api/stage between steps would
        exercise a path a participant never takes.
        """
        walked = []
        stage = self.stage(participant)["stage"]
        while stage is not None and stage["key"] != until:
            if stage["kind"] == "consent":
                answer = self.agree(participant)
            elif stage["kind"] == "assessment":
                answer = self.complete(participant, stage["key"], answers=self.answers_for(stage))
            elif stage["kind"] == "questionnaire":
                answer = self.complete(participant, stage["key"], answers=self.questionnaire_answers(stage))
            else:
                answer = self.complete(participant, stage["key"])
            assert answer[0] == 200, answer
            walked.append(stage["key"])
            stage = json.loads(answer[1])["stage"]
        return walked


class FlowTestCase(unittest.TestCase):
    """Base case holding one participant-facing server for the whole class."""

    @classmethod
    def setUpClass(cls):
        cls.srv = FlowServer()

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()


class ExposureTests(FlowTestCase):
    """What a participant can see or ask for."""

    def test_no_response_of_the_whole_flow_names_a_stimulus_or_a_condition(self):
        """The one test that protects the manipulation: walking every stage must never disclose which scenario a
        trial is, whether it is the injected version, or which transparency condition the participant is in."""
        participant = participant_with("high")  # the condition whose responses do carry a trace
        plan = ep.plan_for(CONFIG, participant, PAYLOAD)
        secrets = {s["stimulus"] for s in plan["stages"] if "stimulus" in s}
        secrets |= {s["scenario"] for s in plan["stages"] if "scenario" in s}
        # The safe/unsafe label of a trial is a secret too, but as a JSON value ("safe"), not as a word: the
        # consent text asks whether the assistant acted "safely". "practice", the practice trial's version, is
        # not a secret — participants are told which trial is the practice one, and the descriptor says so.
        secrets |= {json.dumps(s["version"]) for s in plan["stages"] if s.get("version") in ("safe", "unsafe")}
        self.assertTrue(secrets)

        page = self.srv.get(f"/p/{self.srv.tokens[participant]}")[1]
        seen = [page]
        while True:
            payload = self.srv.stage(participant)
            seen.append(json.dumps(payload))
            stage = payload["stage"]
            if stage is None:
                break
            self.assertNotIn("condition", stage)
            self.assertNotIn("stimulus", stage)
            self.assertNotIn("scenario", stage)
            answer = (self.srv.agree(participant) if stage["kind"] == "consent"
                      else self.srv.complete(participant, stage["key"], answers=self.srv.answers_for(stage))
                      if stage["kind"] == "assessment"
                      else self.srv.complete(participant, stage["key"], answers=self.srv.questionnaire_answers(stage))
                      if stage["kind"] == "questionnaire" else self.srv.complete(participant, stage["key"]))
            self.assertEqual(answer[0], 200)
        for text in seen:
            for secret in secrets:
                self.assertNotIn(secret, text)

    def test_the_condition_cannot_be_changed_by_asking_for_the_other_one(self):
        """A low participant must not be able to obtain the high view, which is the whole manipulation. The
        transparency condition comes from their record, so a c= parameter on the request is ignored."""
        low, high = participant_with("low"), participant_with("high")
        srv = FlowServer()
        try:
            for participant in (low, high):
                srv.walk(participant, until="practice_scenario")
            low_view = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[low]}")[1])
            high_view = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[high]}")[1])
            self.assertNotIn("trace", low_view)
            self.assertIn("trace", high_view)
            for suffix in ("&c=high", "&c=low&s=user_task_1__no_injection"):
                forced = json.loads(srv.get(f"/api/stimulus?t={srv.tokens[low]}{suffix}")[1])
                self.assertEqual(forced, low_view, suffix)
        finally:
            srv.close()

    def test_scenario_content_is_refused_at_every_stage_that_shows_no_scenario(self):
        """Consent, an assessment and either questionnaire must not be usable to fetch a trial's
        content: that is how a participant could otherwise read the next trial early or the last one again."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            token = srv.tokens[participant]
            for stage_key in ("consent", "pre_questionnaire", "practice_assessment", "questionnaire"):
                srv.walk(participant, until=stage_key)
                self.assertEqual(srv.stage(participant)["stage"]["key"], stage_key)
                for route in CONTENT_ROUTES:
                    self.assertEqual(srv.get(f"{route}?t={token}")[0], 404, (stage_key, route))
        finally:
            srv.close()

    def test_an_unknown_or_malformed_token_reaches_nothing(self):
        for token in ("", "short", "../../etc/passwd", "a" * 200, "notarealtoken000000000000000000"):
            self.assertIn(self.srv.get(f"/p/{token}")[0], (400, 404), token)
            self.assertIn(self.srv.get(f"/api/stage?t={token}")[0], (400, 404), token)
            for route in CONTENT_ROUTES:
                self.assertIn(self.srv.get(f"{route}?t={token}")[0], (400, 404), (token, route))
            self.assertIn(self.srv.post("/api/stage/complete", {"token": token, "stage_key": "consent"})[0],
                          (400, 404), token)

    def test_the_researcher_routes_are_absent_unless_the_server_is_started_for_a_researcher(self):
        """A participant-facing server must not serve the stimulus index or the s= form of the content routes;
        with --researcher-preview the same server does."""
        paths = ("/", "/chat", "/api/stimulus?s=user_task_1__no_injection&c=high",
                 "/api/workspace?s=user_task_1__no_injection", "/api/skills?s=user_task_1__no_injection")
        for path in paths:
            self.assertEqual(self.srv.get(path)[0], 404, path)
        preview = FlowServer(researcher_preview=True)
        try:
            for path in paths:
                self.assertEqual(preview.get(path)[0], 200, path)
        finally:
            preview.close()


class IntegrityTests(FlowTestCase):
    """What the flow records, and what it refuses to record."""

    def test_a_participant_walks_the_whole_sequence_once_and_is_marked_complete(self):
        srv = FlowServer()
        try:
            participant = participant_with("low")
            expected = [s["key"] for s in ep.plan_for(CONFIG, participant, PAYLOAD)["stages"]]
            walked = srv.walk(participant)
            self.assertEqual(walked, expected)
            self.assertIsNone(srv.stage(participant)["stage"])
            conn = store.connect(srv.handler.db_path)
            try:
                self.assertIsNotNone(store.record(conn, participant)["completed_at"])
                repeated = conn.execute("SELECT stage_key FROM progress WHERE participant_id = ? "
                                        "GROUP BY stage_key HAVING COUNT(*) > 1", (participant,)).fetchall()
                self.assertEqual(repeated, [])
                self.assertEqual(len(store.completed_stages(conn, participant)), len(expected))
            finally:
                conn.close()
        finally:
            srv.close()

    def test_reopening_the_link_returns_the_same_stage_and_records_one_arrival(self):
        """The resume path. Reloading mid-trial must not move the participant on or start the trial again."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            srv.walk(participant, until="trial_01_scenario")
            first = srv.stage(participant)
            for _ in range(4):
                self.assertEqual(srv.get(f"/p/{srv.tokens[participant]}")[0], 200)
                self.assertEqual(srv.stage(participant), first)
            conn = store.connect(srv.handler.db_path)
            try:
                rows = conn.execute("SELECT entered_at FROM progress WHERE participant_id = ? AND stage_key = ?",
                                    (participant, "trial_01_scenario")).fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                conn.close()
        finally:
            srv.close()

    def test_a_later_stage_cannot_be_completed_and_progress_is_untouched(self):
        srv = FlowServer()
        try:
            participant = participant_with("low")
            before = srv.stage(participant)
            for stage_key in ("questionnaire", "trial_04_scenario", "trial_02_assessment", "completion"):
                self.assertEqual(srv.complete(participant, stage_key)[0], 409, stage_key)
            for stage_key in ("debriefing", "trial_99_scenario"):
                self.assertEqual(srv.complete(participant, stage_key)[0], 409, stage_key)
            self.assertEqual(srv.stage(participant), before)
        finally:
            srv.close()

    def test_submitting_the_same_stage_twice_advances_only_once(self):
        """A double click or a resubmitted form must not consume the following stage."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            self.assertEqual(srv.stage(participant)["stage"]["key"], "consent")
            first = srv.agree(participant)
            second = srv.complete(participant, "consent")  # a replay needs no agreement and changes nothing
            self.assertEqual((first[0], second[0]), (200, 200))
            self.assertEqual(json.loads(first[1])["stage"]["key"], "pre_questionnaire")
            self.assertEqual(json.loads(second[1])["stage"]["key"], "pre_questionnaire")
            self.assertEqual(srv.stage(participant)["stage"]["key"], "pre_questionnaire")
        finally:
            srv.close()

    def test_two_participants_keep_separate_progress(self):
        srv = FlowServer()
        try:
            first, second = PAYLOAD["assignments"][0]["participant"], PAYLOAD["assignments"][1]["participant"]
            srv.walk(first, until="trial_02_scenario")
            self.assertEqual(srv.stage(second)["stage"]["key"], "consent")
            self.assertEqual(srv.stage(second)["progress"]["completed"], 0)
            self.assertEqual(srv.stage(first)["stage"]["key"], "trial_02_scenario")
        finally:
            srv.close()

    def events(self, srv, participant):
        conn = store.connect(srv.handler.db_path)
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE participant_id = ? ORDER BY event_id",
                                                  (participant,))]
        finally:
            conn.close()

    def test_each_trial_starts_with_one_server_event_under_its_own_trial_id(self):
        """The browser is never told the stimulus or the condition; the server records when each trial started,
        against the trial id whose plan_stages row says what it showed."""
        srv = FlowServer()
        try:
            participant = participant_with("high")
            plan = ep.plan_for(CONFIG, participant, PAYLOAD)
            scenarios = ep.scenario_stages(plan, practice=True)
            srv.walk(participant)
            starts = [e for e in self.events(srv, participant) if e["type"] == "session_start"]
            self.assertEqual([e["trial_id"] for e in starts], [s["trial_id"] for s in scenarios])
            self.assertEqual([e["practice"] for e in starts], [int(s["practice"]) for s in scenarios])
            self.assertTrue(all(e["origin"] == "server" and e["stage_key"].endswith("_scenario") for e in starts))

            # Every stage must also carry an arrival time: the trial's reading and response times are measured
            # from it, and a stage reached by continuing is the one the participant actually arrived at.
            conn = store.connect(srv.handler.db_path)
            try:
                missing = conn.execute("SELECT stage_key FROM progress WHERE participant_id = ? "
                                       "AND entered_at IS NULL", (participant,)).fetchall()
                self.assertEqual(missing, [])
                entered = {row["stage_key"] for row in
                           conn.execute("SELECT stage_key FROM progress WHERE participant_id = ?", (participant,))}
                self.assertEqual(entered, {s["key"] for s in plan["stages"]})
            finally:
                conn.close()
        finally:
            srv.close()

    def test_every_stage_request_is_recorded_so_reloads_are_visible(self):
        srv = FlowServer()
        try:
            participant = participant_with("low")
            srv.walk(participant, until="trial_01_scenario")
            for _ in range(3):
                srv.stage(participant)  # the page was reloaded three times mid-trial
            loads = [e for e in self.events(srv, participant) if e["type"] == "stage_load"]
            self.assertEqual(loads[0]["stage_key"], "consent")
            self.assertIsNone(loads[0]["trial_id"])
            self.assertEqual(sum(e["trial_id"] == f"{participant}-t01" for e in loads), 3)
            starts = [e for e in self.events(srv, participant)
                      if e["type"] == "session_start" and e["trial_id"] == f"{participant}-t01"]
            self.assertEqual(len(starts), 1)  # reloading does not start the trial again
        finally:
            srv.close()

    def test_browser_events_are_stored_against_the_current_trial_with_the_practice_flagged(self):
        srv = FlowServer()
        try:
            participant = participant_with("high")
            token = srv.tokens[participant]
            srv.walk(participant, until="practice_assessment")
            practice_id = f"{participant}-p00"
            for event in ({"type": "workspace_panel_open"}, {"type": "skills_tab_open"},
                          {"type": "trace_card_expand", "card_id": "step-2"},
                          {"type": "trace_card_view", "card_id": "step-2", "duration_ms": 900},
                          {"type": "scenario_visible", "duration_ms": 12000}):
                answer = srv.post("/api/log", {"token": token, "trial_id": practice_id, "client_ms": 1, **event})
                self.assertEqual(answer[0], 204, answer)  # sent while the assessment loads: still accepted
            stored = [e for e in self.events(srv, participant) if e["origin"] == "browser"]
            self.assertEqual([e["type"] for e in stored], ["workspace_panel_open", "skills_tab_open",
                                                           "trace_card_expand", "trace_card_view", "scenario_visible"])
            self.assertTrue(all(e["practice"] == 1 and e["trial_id"] == practice_id for e in stored))
            self.assertEqual(stored[3]["card_id"], "step-2")
            self.assertEqual(stored[3]["duration_ms"], 900)
            seqs = [e["seq"] for e in self.events(srv, participant) if e["trial_id"] == practice_id]
            self.assertEqual(seqs, list(range(len(seqs))))
        finally:
            srv.close()

    def test_events_for_another_trial_or_another_participant_are_refused(self):
        srv = FlowServer()
        try:
            first, second = participant_with("low"), participant_with("high")
            srv.walk(first, until="trial_01_scenario")
            srv.walk(second, until="trial_01_scenario")
            before = len(self.events(srv, first)) + len(self.events(srv, second))
            refused = [
                ({"token": srv.tokens[first], "trial_id": f"{second}-t01"}, 409),   # someone else's trial
                ({"token": srv.tokens[first], "trial_id": f"{first}-t02"}, 409),    # a later trial
                ({"token": srv.tokens[first], "trial_id": f"{first}-p00"}, 409),    # an earlier trial
                ({"token": "x" * 32, "trial_id": f"{first}-t01"}, 404),             # nobody's token
                ({"trial_id": f"{first}-t01"}, 400),                                 # no token at all
                ({"token": srv.tokens[first], "trial_id": f"{first}-t01", "type": "session_start"}, 400),
                ({"token": srv.tokens[first], "trial_id": f"{first}-t01", "type": "bogus"}, 400),
            ]
            for body, status in refused:
                body = {"type": "workspace_panel_open", **body}
                self.assertEqual(srv.post("/api/log", body)[0], status, body)
            self.assertEqual(len(self.events(srv, first)) + len(self.events(srv, second)), before)
            self.assertEqual(srv.post("/api/log", "garbage")[0], 400)
        finally:
            srv.close()

    def test_a_changed_stage_sequence_stops_the_participant_instead_of_resuming_them(self):
        """Enrolled under eight trials, served a three-trial configuration: the flow must refuse rather than mix
        two different experiments in one participant's record."""
        srv = FlowServer()
        try:
            participant = participant_with("low")
            srv.walk(participant, until="trial_01_scenario")
            raw = json.loads(ep.DEVELOPMENT_CONFIG.read_text(encoding="utf-8"))
            raw["trials_per_participant"] = 3
            path = Path(tempfile.mkdtemp()) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            srv.handler.config = ep.load_config(path)
            token = srv.tokens[participant]
            self.assertEqual(srv.get(f"/api/stage?t={token}")[0], 409)
            self.assertEqual(srv.get(f"/api/stimulus?t={token}")[0], 409)
            self.assertEqual(srv.complete(participant, "trial_01_scenario")[0], 409)
        finally:
            srv.close()


class AssessmentTests(unittest.TestCase):
    """The answers after each conversation: stored against the right trial, practice kept apart, nothing
    recorded that the participant did not actively give."""

    def setUp(self):
        self.srv = FlowServer()

    def tearDown(self):
        self.srv.close()

    def rows(self, participant):
        conn = store.connect(self.srv.handler.db_path)
        try:
            return {r["stage_key"]: store.response_of(conn, participant, r["stage_key"])
                    for r in conn.execute("SELECT stage_key FROM responses WHERE participant_id = ?", (participant,))}
        finally:
            conn.close()

    def experimental_view(self, participant):
        conn = store.connect(self.srv.handler.db_path)
        try:
            return [r["stage_key"] for r in conn.execute(
                "SELECT stage_key FROM experimental_responses WHERE participant_id = ?", (participant,))]
        finally:
            conn.close()

    def test_responses_are_stored_against_the_trial_and_the_practice_is_kept_out_of_analysis(self):
        participant = participant_with("high")
        plan = {s["key"]: s for s in ep.plan_for(CONFIG, participant, PAYLOAD)["stages"]}
        self.srv.walk(participant, until="practice_assessment")
        self.assertEqual(self.srv.answer(participant, performed_safely="no", confidence=2, reasoning="  why  ",
                                         sources=["activity", "request"])[0], 200)
        self.srv.walk(participant, until="trial_01_assessment")
        self.assertEqual(self.srv.answer(participant, confidence=5)[0], 200)
        rows = self.rows(participant)
        practice, trial = rows["practice_assessment"], rows["trial_01_assessment"]
        for key, row in (("practice_assessment", practice), ("trial_01_assessment", trial)):
            self.assertEqual(row["trial_id"], plan[key]["trial_id"])
            self.assertEqual(row["stimulus"], plan[key]["stimulus"])
            self.assertEqual(row["condition"], plan[key]["condition"])
            self.assertEqual(row["questions_sha256"], flow.load_assessment()["sha256"])
        self.assertEqual((practice["practice"], trial["practice"]), (1, 0))
        self.assertEqual((practice["performed_safely"], practice["confidence"], practice["reasoning"]), ("no", 2, "why"))
        self.assertEqual(practice["sources"], ["request", "activity"])  # stored in the order offered
        self.assertEqual(practice["sources_offered"], ["request", "activity", "response", "workspace", "skills", "other"])
        self.assertEqual(practice["sources_other"], "")
        self.assertEqual(trial["confidence"], 5)
        self.assertEqual(self.experimental_view(participant), ["trial_01_assessment"])

    def test_an_incomplete_or_malformed_answer_is_refused_and_nothing_is_stored(self):
        participant = participant_with("low")
        self.srv.walk(participant, until="practice_assessment")
        for bad in ({"confidence": None}, {"confidence": 0}, {"confidence": 6}, {"confidence": "3"},
                    {"confidence": True}, {"confidence": 2.5}, {"performed_safely": None},
                    {"performed_safely": "maybe"}, {"reasoning": "   "}, {"reasoning": "x" * 4001},
                    {"sources": []}, {"sources": ["request", "request"]}, {"sources": ["nonsense"]},
                    {"sha256": "0" * 64}):
            with self.subTest(bad=bad):
                self.assertEqual(self.srv.answer(participant, **bad)[0], 400)
        self.assertEqual(self.srv.complete(participant, "practice_assessment")[0], 400)  # no answers at all
        self.assertEqual(self.srv.stage(participant)["stage"]["key"], "practice_assessment")
        self.assertEqual(self.rows(participant), {})

    def test_only_the_sources_in_the_participants_own_view_are_offered_or_accepted(self):
        offered = {}
        for condition in ("low", "high"):
            participant = participant_with(condition)
            self.srv.walk(participant, until="practice_assessment")
            stage = self.srv.stage(participant)["stage"]
            question = next(q for q in stage["assessment"]["questions"] if q["id"] == "sources")
            offered[condition] = [o["id"] for o in question["options"]]
        self.assertNotIn("activity", offered["low"])
        self.assertIn("activity", offered["high"])
        low = participant_with("low")
        self.assertEqual(self.srv.answer(low, sources=["activity"])[0], 400)
        self.assertEqual(self.rows(low), {})

    def test_other_is_offered_last_and_saved_with_or_without_a_description(self):
        for condition in ("low", "high"):
            participant = participant_with(condition)
            self.srv.walk(participant, until="practice_assessment")
            stage = self.srv.stage(participant)["stage"]
            options = next(q for q in stage["assessment"]["questions"] if q["id"] == "sources")["options"]
            self.assertEqual((options[-1]["id"], options[-1]["label"]), ("other", "Other (optional description)"))
        low, high = participant_with("low"), participant_with("high")
        # A description is never stored without "Other", and it keeps its length limit.
        for bad in ({"sources": ["request"], "sources_other": "text without Other"},
                    {"sources": ["other"], "sources_other": "x" * 501}, {"sources": ["other"], "sources_other": 5}):
            with self.subTest(bad=bad):
                self.assertEqual(self.srv.answer(low, **bad)[0], 400)
        self.assertEqual(self.rows(low), {})
        # "Other" alone (an empty or blank description) is a complete answer and is saved.
        self.assertEqual(self.srv.answer(low, sources=["other"], sources_other="   ")[0], 200)
        row = self.rows(low)["practice_assessment"]
        self.assertEqual((row["sources"], row["sources_other"]), (["other"], ""))
        # A description, when given, is saved with it.
        self.assertEqual(self.srv.answer(high, sources=["other", "skills"], sources_other="  a news article  ")[0], 200)
        row = self.rows(high)["practice_assessment"]
        self.assertEqual((row["sources"], row["sources_other"]), (["skills", "other"], "a news article"))
        self.assertEqual(row["sources_offered"][-1], "other")

    def test_submitting_an_assessment_again_keeps_the_first_answers(self):
        participant = participant_with("low")
        self.srv.walk(participant, until="practice_assessment")
        stage = self.srv.stage(participant)["stage"]
        self.assertEqual(self.srv.answer(participant, confidence=1)[0], 200)
        again = self.srv.complete(participant, stage["key"], answers=FlowServer.answers_for(stage, confidence=5))
        self.assertEqual(again[0], 200)
        self.assertEqual(self.rows(participant)["practice_assessment"]["confidence"], 1)


class QuestionnaireTests(unittest.TestCase):
    """The post-study questionnaire: stored with the participant, validated, and saved once; one item is asked
    only of participants who see the activity log."""

    def setUp(self):
        self.srv = FlowServer()
        self.participant = participant_with("low")
        self.srv.walk(self.participant, until="questionnaire")
        self.stage = self.srv.stage(self.participant)["stage"]

    def tearDown(self):
        self.srv.close()

    def stored(self, participant=None, stage_key="questionnaire"):
        conn = store.connect(self.srv.handler.db_path)
        try:
            return store.questionnaire_of(conn, participant or self.participant, stage_key)
        finally:
            conn.close()

    def high_stage(self):
        high = participant_with("high")
        self.srv.walk(high, until="questionnaire")
        return high, self.srv.stage(high)["stage"]

    def test_the_stage_carries_the_questionnaire_file(self):
        spec = flow.load_questionnaire()
        self.assertEqual(self.stage["questionnaire"]["sha256"], spec["sha256"])
        self.assertEqual(self.stage["questionnaire"]["title"], spec["title"])
        self.assertEqual([i["id"] for i in self.stage["questionnaire"]["items"]],
                         [i["id"] for i in spec["items"] if "shown_with" not in i])

    def test_the_items_are_the_planned_post_study_questions_in_order(self):
        _, high = self.high_stage()
        shared = ["unsafe_meaning", "information_sufficient", "skill_docs_usefulness", "skill_docs_purpose_understood",
                  "information_overload", "information_overload_explanation", "information_relied_on",
                  "additional_information_wanted"]
        self.assertEqual([i["id"] for i in self.stage["questionnaire"]["items"]], shared)
        self.assertEqual([i["id"] for i in high["questionnaire"]["items"]], shared + ["activity_log_usefulness"])
        optional = {i["id"] for i in high["questionnaire"]["items"] if not i["required"]}
        self.assertEqual(optional, {"information_overload_explanation", "additional_information_wanted"})
        for item in high["questionnaire"]["items"]:
            if item["type"] == "scale":  # every point of every scale is labelled
                self.assertEqual(sorted(map(int, item["anchors"])), list(range(item["min"], item["max"] + 1)), item)

    def test_answers_are_stored_per_item_with_the_file_hash_and_the_participant_finishes(self):
        answers = self.srv.questionnaire_answers(self.stage)
        status, body = self.srv.complete(self.participant, "questionnaire", answers=answers)
        self.assertEqual(status, 200, body)
        stored = self.stored()
        spec = flow.load_questionnaire()
        self.assertEqual(list(stored), [i["id"] for i in flow.offered_items(spec, "low")])
        optional = [i["id"] for i in spec["items"] if not i["required"]]
        self.assertTrue(all(stored[i] == "" for i in optional if i not in answers["items"]))
        conn = store.connect(self.srv.handler.db_path)
        try:
            labels = {tuple(r) for r in conn.execute(
                "SELECT questionnaire_sha256, stage_key FROM questionnaire_responses "
                "WHERE participant_id = ? AND item_id IN (%s)" % ",".join("?" * len(stored)),
                (self.participant, *stored))}
        finally:
            conn.close()
        self.assertEqual(labels, {(spec["sha256"], "questionnaire")})
        self.assertEqual(json.loads(body)["stage"]["kind"], "completion")

    def test_missing_unknown_or_out_of_range_answers_are_refused_and_nothing_is_stored(self):
        spec = flow.load_questionnaire()
        scale = next(i for i in spec["items"] if i["type"] == "scale")
        good = self.srv.questionnaire_answers(self.stage)
        bad = [
            {**good, "sha256": "0" * 64},
            {**good, "items": {k: v for k, v in good["items"].items() if k != scale["id"]}},
            {**good, "items": {**good["items"], scale["id"]: scale["max"] + 1}},
            {**good, "items": {**good["items"], scale["id"]: True}},
            {**good, "items": {**good["items"], "not_an_item": "x"}},
            None,
        ]
        for answers in bad:
            self.assertEqual(self.srv.complete(self.participant, "questionnaire", answers=answers)[0], 400, answers)
        self.assertEqual(self.stored(), {})
        self.assertEqual(self.srv.stage(self.participant)["stage"]["key"], "questionnaire")

    def test_shared_items_are_identical_in_both_conditions_and_it_is_not_a_placeholder(self):
        _, high = self.high_stage()
        low_q, high_q = self.stage["questionnaire"], high["questionnaire"]
        self.assertEqual(high_q["items"][:len(low_q["items"])], low_q["items"])
        self.assertEqual({k: v for k, v in high_q.items() if k != "items"},
                         {k: v for k, v in low_q.items() if k != "items"})
        self.assertFalse(low_q["placeholder"])

    def test_the_activity_log_item_is_asked_and_stored_only_in_the_condition_that_shows_it(self):
        high, stage = self.high_stage()
        self.assertNotIn("activity_log_usefulness", [i["id"] for i in self.stage["questionnaire"]["items"]])
        self.assertTrue(all("shown_with" not in i for i in stage["questionnaire"]["items"]))  # the mark is not sent
        # A low participant cannot answer it, even by adding it to the submission.
        smuggled = self.srv.questionnaire_answers(self.stage, activity_log_usefulness=3)
        self.assertEqual(self.srv.complete(self.participant, "questionnaire", answers=smuggled)[0], 400)
        # A high participant must answer it.
        missing = self.srv.questionnaire_answers(stage)
        del missing["items"]["activity_log_usefulness"]
        self.assertEqual(self.srv.complete(high, "questionnaire", answers=missing)[0], 400)
        answers = self.srv.questionnaire_answers(stage, activity_log_usefulness=4)
        self.assertEqual(self.srv.complete(high, "questionnaire", answers=answers)[0], 200)
        self.assertEqual(self.stored(high)["activity_log_usefulness"], "4")
        self.assertEqual(self.srv.complete(self.participant, "questionnaire",
                                           answers=self.srv.questionnaire_answers(self.stage))[0], 200)
        self.assertNotIn("activity_log_usefulness", self.stored())

    def test_did_not_consult_is_stored_as_its_id_and_only_where_the_scale_offers_it(self):
        items = self.stage["questionnaire"]["items"]
        with_na = next(i for i in items if i.get("not_applicable"))
        without_na = next(i for i in items if i["type"] == "scale" and not i.get("not_applicable"))
        na = with_na["not_applicable"]["id"]
        wrong = self.srv.questionnaire_answers(self.stage, **{without_na["id"]: na})
        self.assertEqual(self.srv.complete(self.participant, "questionnaire", answers=wrong)[0], 400)
        right = self.srv.questionnaire_answers(self.stage, **{with_na["id"]: na})
        self.assertEqual(self.srv.complete(self.participant, "questionnaire", answers=right)[0], 200)
        self.assertEqual(self.stored()[with_na["id"]], na)

    def test_a_malformed_questionnaire_file_stops_the_loading(self):
        spec = json.loads(flow.QUESTIONNAIRE_PATH.read_text())
        path = Path(tempfile.mkdtemp()) / "questionnaire.json"
        scale_index = next(n for n, i in enumerate(spec["items"]) if i["type"] == "scale")
        breakages = [("not_applicable", {"id": "3", "label": "x"}), ("not_applicable", {"id": "skipped"}),
                     ("not_applicable", "skipped"), ("shown_with", "trace_panel")]
        for key, value in breakages:
            broken = json.loads(json.dumps(spec))
            broken["items"][scale_index][key] = value
            path.write_text(json.dumps(broken))
            with self.assertRaises(ValueError, msg=(key, value)):
                flow.load_questionnaire(path)
        path.write_text(json.dumps({**spec, "title": ""}))
        with self.assertRaises(ValueError):
            flow.load_questionnaire(path)

    def test_an_item_id_shared_by_the_two_questionnaires_stops_the_loading(self):
        pre = json.loads(flow.PRE_QUESTIONNAIRE_PATH.read_text())
        pre["items"][0]["id"] = flow.load_questionnaire()["items"][0]["id"]
        path = Path(tempfile.mkdtemp()) / "pre_questionnaire.json"
        path.write_text(json.dumps(pre))
        with mock.patch.dict(flow.QUESTIONNAIRES, {ep.PRE_QUESTIONNAIRE_KEY: path}):
            with self.assertRaises(ValueError):
                flow.load_questionnaires()
        self.assertEqual(set(flow.load_questionnaires()), set(ep.QUESTIONNAIRE_STAGES))

    def test_submitting_again_keeps_the_first_answers(self):
        first = self.srv.questionnaire_answers(self.stage)
        self.srv.complete(self.participant, "questionnaire", answers=first)
        kept = self.stored()
        scale = next(i for i in self.stage["questionnaire"]["items"] if i["type"] == "scale")
        again = self.srv.questionnaire_answers(self.stage, **{scale["id"]: scale["max"]})
        self.assertEqual(self.srv.complete(self.participant, "questionnaire", answers=again)[0], 200)
        self.assertEqual(self.stored(), kept)


class PreQuestionnaireTests(unittest.TestCase):
    """The pre-study questionnaire: right after consent, the same in both conditions, validated, and stored under
    its own stage key before the practice can start."""

    def setUp(self):
        self.srv = FlowServer()
        self.participant = participant_with("low")
        self.agreed = self.srv.agree(self.participant)
        self.stage = json.loads(self.agreed[1])["stage"]

    def tearDown(self):
        self.srv.close()

    def stored(self, stage_key):
        conn = store.connect(self.srv.handler.db_path)
        try:
            return store.questionnaire_of(conn, self.participant, stage_key)
        finally:
            conn.close()

    def test_consent_leads_to_the_pre_study_questionnaire(self):
        self.assertEqual(self.agreed[0], 200)
        self.assertEqual((self.stage["key"], self.stage["kind"]), ("pre_questionnaire", "questionnaire"))
        spec = flow.load_questionnaire(flow.PRE_QUESTIONNAIRE_PATH)
        self.assertEqual(self.stage["questionnaire"]["sha256"], spec["sha256"])
        self.assertEqual([(i["id"], i["type"]) for i in self.stage["questionnaire"]["items"]],
                         [("ai_chat_frequency", "choice"), ("agent_experience", "choice")])
        self.assertTrue(all(i["required"] for i in self.stage["questionnaire"]["items"]))
        self.assertNotEqual(self.stage["questionnaire"]["title"], flow.load_questionnaire()["title"])

    def test_both_conditions_get_the_same_pre_study_questionnaire(self):
        high = participant_with("high")
        high_stage = json.loads(self.srv.agree(high)[1])["stage"]
        self.assertEqual(high_stage["questionnaire"], self.stage["questionnaire"])

    def test_the_practice_cannot_start_before_it_is_answered_and_an_unknown_option_is_refused(self):
        self.assertEqual(self.srv.complete(self.participant, "practice_scenario")[0], 409)
        self.assertEqual(self.srv.complete(self.participant, "pre_questionnaire")[0], 400)  # no answers
        wrong = self.srv.questionnaire_answers(self.stage, ai_chat_frequency="hourly")
        self.assertEqual(self.srv.complete(self.participant, "pre_questionnaire", answers=wrong)[0], 400)
        post = flow.load_questionnaire()  # the other questionnaire's hash is not accepted here
        stale = {**self.srv.questionnaire_answers(self.stage), "sha256": post["sha256"]}
        self.assertEqual(self.srv.complete(self.participant, "pre_questionnaire", answers=stale)[0], 400)
        self.assertEqual(self.stored("pre_questionnaire"), {})
        self.assertEqual(self.srv.stage(self.participant)["stage"]["key"], "pre_questionnaire")

    def test_answers_are_stored_under_its_stage_key_and_lead_to_the_practice(self):
        answers = self.srv.questionnaire_answers(self.stage, ai_chat_frequency="weekly", agent_experience="not_sure")
        status, body = self.srv.complete(self.participant, "pre_questionnaire", answers=answers)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["stage"]["key"], "practice_scenario")
        self.assertEqual(self.stored("pre_questionnaire"),
                         {"ai_chat_frequency": "weekly", "agent_experience": "not_sure"})
        self.assertEqual(self.stored("questionnaire"), {})
        # Both questionnaires end up side by side, each under its own key.
        self.srv.walk(self.participant)
        self.assertEqual(list(self.stored("pre_questionnaire")), ["ai_chat_frequency", "agent_experience"])
        self.assertTrue(self.stored("questionnaire"))
        self.assertFalse(set(self.stored("pre_questionnaire")) & set(self.stored("questionnaire")))


class ConsentTests(unittest.TestCase):
    """The electronic consent: it must be recorded, per participant, before anything else can happen."""

    def setUp(self):
        self.srv = FlowServer()
        self.participant = participant_with("low")

    def tearDown(self):
        self.srv.close()

    def consent_row(self, participant=None):
        conn = store.connect(self.srv.handler.db_path)
        try:
            return store.consent_of(conn, participant or self.participant)
        finally:
            conn.close()

    def test_the_consent_stage_serves_the_consent_file(self):
        material = flow.load_consent()
        stage = self.srv.stage(self.participant)["stage"]
        self.assertEqual(stage["consent"], {key: material[key] for key in ("markdown", "checkbox", "sha256")})
        self.assertEqual(stage["consent"]["checkbox"], "I agree to participate in this research study.")

    def test_consent_cannot_be_passed_without_a_valid_agreement(self):
        """Clicking through without the box ticked, with a forged or stale hash, or with a truthy non-true value
        must leave the participant at consent with nothing recorded."""
        sha = self.srv.stage(self.participant)["stage"]["consent"]["sha256"]
        for consent in (None, {}, {"agreed": False, "sha256": sha}, {"agreed": "yes", "sha256": sha},
                        {"agreed": 1, "sha256": sha}, {"agreed": True}, {"agreed": True, "sha256": "0" * 64}):
            extra = {} if consent is None else {"consent": consent}
            self.assertEqual(self.srv.complete(self.participant, "consent", **extra)[0], 400, consent)
            self.assertEqual(self.srv.stage(self.participant)["stage"]["key"], "consent", consent)
        self.assertIsNone(self.consent_row())

    def test_agreeing_records_the_time_and_the_exact_text_for_this_participant_only(self):
        material = flow.load_consent()
        self.assertEqual(self.srv.agree(self.participant)[0], 200)
        row = self.consent_row()
        self.assertEqual(row["consent_text"], material["text"])
        self.assertEqual(row["consent_sha256"], material["sha256"])
        self.assertIsNotNone(row["agreed_at"])
        others = [p for p in self.srv.tokens if p != self.participant]
        self.assertTrue(all(self.consent_row(p) is None for p in others))

    def test_the_first_agreement_is_kept_through_replays_and_reopened_links(self):
        self.srv.agree(self.participant)
        first = self.consent_row()
        sha = first["consent_sha256"]
        self.srv.complete(self.participant, "consent", consent={"agreed": True, "sha256": sha})
        self.srv.complete(self.participant, "consent")
        self.srv.get(f"/p/{self.srv.tokens[self.participant]}")
        self.assertEqual(self.consent_row(), first)
        self.assertEqual(self.srv.stage(self.participant)["stage"]["key"], "pre_questionnaire")


class ConsentSchemaTests(unittest.TestCase):
    """Participant databases created before consent was stored must open and gain the consents table."""

    def test_a_version_1_database_is_upgraded_in_place_and_keeps_its_participants(self):
        folder = Path(tempfile.mkdtemp())
        try:
            path = folder / "participants.db"
            conn = store.connect(path)
            row = store.enrol(conn, CONFIG, participant_with("low"), PAYLOAD)
            conn.execute("DROP TABLE consents")
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
            conn.commit()
            conn.close()

            conn = store.connect(path)
            version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
            self.assertEqual(version, str(store.SCHEMA_VERSION))
            self.assertEqual(store.by_token(conn, row["token"])["participant_id"], row["participant_id"])
            self.assertIsNone(store.consent_of(conn, row["participant_id"]))
            conn.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class PracticePresentationTests(unittest.TestCase):
    """The practice hides the action-log cards its configuration lists; nothing else is ever shortened."""

    def test_only_the_practice_stage_hides_its_omitted_cards(self):
        """A high participant's practice shows five renumbered cards without the failed contact lookups, while an
        experimental trial is served exactly as built and the practice stimulus file on disk keeps all eight."""
        srv = FlowServer()
        try:
            participant = participant_with("high")
            token = srv.tokens[participant]
            srv.walk(participant, until="practice_scenario")
            practice = json.loads(srv.get(f"/api/stimulus?t={token}")[1])
            actions = [e for e in practice["trace"] if e["type"] == "action"]
            self.assertEqual([e["step"] for e in actions], [1, 2, 3, 4, 5])
            self.assertNotIn("search_contacts_by_name", [e["tool"] for e in actions])
            full = ps.participant_view(CONFIG["practice"]["stimulus"], "high")
            self.assertEqual(len([e for e in full["trace"] if e["type"] == "action"]), 8)
            self.assertEqual(practice["final_response"], full["final_response"])

            # The first experimental trial that can be served (planned unsafe items have no folder yet).
            servable = set(ps.list_stimulus_ids())
            stage = next(s for s in ep.scenario_stages(ep.plan_for(CONFIG, participant, PAYLOAD))
                         if s["stimulus"] in servable)
            srv.walk(participant, until=stage["key"])
            served = json.loads(srv.get(f"/api/stimulus?t={token}")[1])
            self.assertEqual(served, ps.participant_view(stage["stimulus"], stage["condition"]))
        finally:
            srv.close()

    def test_a_step_that_is_not_an_action_of_the_practice_stimulus_stops_the_server(self):
        config = dict(CONFIG, practice={**CONFIG["practice"], "omit_steps": [5, 9]})
        with self.assertRaises(ValueError):
            ps.check_practice_presentation(config)
        ps.check_practice_presentation(CONFIG)  # the shipped configuration passes


class ResolutionTests(unittest.TestCase):
    """participant_flow.py without a server: the token resolution and the stage descriptor."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.conn = store.connect(self.folder / "participants.db")
        self.participant = participant_with("low")
        self.row = store.enrol(self.conn, CONFIG, self.participant, PAYLOAD)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_a_token_resolves_to_its_own_participant_only(self):
        self.assertEqual(flow.participant_for(self.conn, self.row["token"])["participant_id"], self.participant)
        with self.assertRaises(FileNotFoundError):
            flow.participant_for(self.conn, "A" * 32)  # right shape, nobody's token
        for bad in ("", "short", None, "has/slash/and spaces"):
            with self.subTest(token=bad), self.assertRaises(ValueError):
                flow.participant_for(self.conn, bad)

    def test_the_descriptor_keeps_the_trial_id_and_drops_the_stimulus_identity(self):
        plan = ep.plan_for(CONFIG, self.participant, PAYLOAD)
        for stage in plan["stages"]:
            out = flow.descriptor(stage, plan)
            self.assertEqual(set(out) - {"key", "kind", "index", "trial_id", "practice", "trial", "trials"}, set())
            if stage["kind"] in ("scenario", "assessment"):
                self.assertEqual(out["trial_id"], stage["trial_id"])
            if out.get("trials"):
                self.assertEqual(out["trials"], CONFIG["trials_per_participant"])


if __name__ == "__main__":
    unittest.main()
