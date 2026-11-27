"""Tests for the participant interface's skill documentation and interaction events: identical access in both
conditions, hidden researcher data, the events that may be stored and the measures summarised from them. Storage
against a participant's current trial is tested over HTTP in tests/test_participant_flow.py."""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import interaction_log as ilog  # noqa: E402
import participant_store as store  # noqa: E402
import participant_server as ps  # noqa: E402
from participant_http import preview_handler  # noqa: E402

FOLDER = "user_task_13__no_injection"
TRIAL = "P001-t01"


def event(kind, **fields):
    return {"trial_id": TRIAL, "type": kind, **fields}


def row(kind, seq, server_time="2026-11-26T10:00:00.000+00:00", **fields):
    """An event row as participant_store.trial_events returns it."""
    base = {"type": kind, "seq": seq, "server_time": server_time, "tab": None, "item_id": None, "skill_id": None,
            "card_id": None, "duration_ms": None}
    return {**base, **fields}


class ValidationTest(unittest.TestCase):
    def test_the_event_vocabulary_is_the_one_the_database_accepts(self):
        self.assertEqual(set(ilog.EVENT_FIELDS), set(store.EVENT_TYPES))
        fields = {name for spec in ilog.EVENT_FIELDS.values() for name in spec}
        self.assertLessEqual(fields, set(ilog.FIELD_COLUMNS))

    def test_valid_events_are_reduced_to_their_fields(self):
        self.assertEqual(ilog.validate_event(event("skill_doc_open", skill_id="email-lookup", client_ms=1700000000000)),
                         {"trial_id": TRIAL, "type": "skill_doc_open", "skill_id": "email-lookup",
                          "client_ms": 1700000000000})
        self.assertEqual(ilog.validate_event(event("trace_card_view", card_id="step-3", duration_ms=5))["card_id"],
                         "step-3")
        self.assertEqual(ilog.validate_event(event("workspace_item_view", tab="drive", item_id="26", duration_ms=0)),
                         {"trial_id": TRIAL, "type": "workspace_item_view", "tab": "drive", "item_id": "26",
                          "duration_ms": 0})

    def test_invalid_events_are_rejected(self):
        bad = [event("unknown_type"), event("skill_doc_open"), event("skill_doc_open", skill_id="../x"),
               event("skill_doc_view", skill_id="a", duration_ms=-1), event("workspace_tab_view", tab="trash", duration_ms=1),
               event("skill_doc_view", skill_id="a", duration_ms=1.5), event("trace_card_expand", card_id="card-1"),
               event("trace_card_view", card_id="step-1"), event("scenario_visible"),
               event("workspace_panel_open", extra="field"), {"type": "workspace_panel_open"},
               {"trial_id": "../../etc", "type": "workspace_panel_open"}, "not an object",
               event("skill_doc_open", skill_id="x" * 3000), event("workspace_panel_open", pid="P001"),
               event("workspace_item_view", tab="mail", item_id="3"),  # no duration
               event("workspace_item_view", tab="skills", item_id="3", duration_ms=1),  # not an item tab
               event("workspace_item_view", tab="mail", item_id="a/b", duration_ms=1),
               event("workspace_item_view", item_id="3", duration_ms=1)]
        for e in bad:
            with self.assertRaises(ilog.LogError, msg=str(e)[:60]):
                ilog.validate_event(e)

    def test_server_events_cannot_come_from_the_browser(self):
        for kind in ilog.SERVER_ONLY:
            with self.assertRaises(ilog.LogError):
                ilog.validate_event(event(kind))
            self.assertEqual(ilog.validate_event(event(kind), "server")["type"], kind)


class SummaryTest(unittest.TestCase):
    def test_summary_measures(self):
        events = [row("session_start", 0, "2026-11-26T10:00:00.000+00:00"), row("workspace_panel_open", 1),
                  row("skills_tab_open", 2), row("skill_doc_open", 3, "2026-11-26T10:00:07.500+00:00", skill_id="email-lookup"),
                  row("skill_doc_view", 4, skill_id="email-lookup", duration_ms=4000),
                  row("skill_doc_view", 5, skill_id="email-lookup", duration_ms=1000),
                  row("skills_tab_view", 6, duration_ms=6000), row("workspace_tab_open", 7, tab="mail"),
                  row("workspace_item_open", 8, tab="mail", item_id="33"),
                  row("workspace_item_view", 8, tab="mail", item_id="33", duration_ms=1200),
                  row("workspace_item_view", 8, tab="mail", item_id="33", duration_ms=300),
                  row("workspace_item_view", 8, tab="calendar", item_id="33", duration_ms=50),
                  row("workspace_tab_view", 9, tab="mail", duration_ms=2500),
                  row("workspace_panel_close", 10, duration_ms=9000),
                  row("trace_card_expand", 11, card_id="step-1"), row("trace_card_expand", 12, card_id="step-1"),
                  row("trace_card_view", 13, card_id="step-1", duration_ms=300),
                  row("stage_load", 14), row("scenario_visible", 15, duration_ms=20000),
                  row("scenario_visible", 16, duration_ms=5000)]
        s = ilog.summarize(events)
        self.assertEqual((s["skills_opened"], s["workspace_opened"], s["first_skill_open_s"]), (True, True, 7.5))
        self.assertEqual(s["skills_viewed_ms"], {"email-lookup": 5000})
        self.assertEqual((s["skills_tab_opened"], s["skills_tab_ms"]), (True, 6000))
        self.assertEqual((s["workspace_tabs_viewed_ms"], s["workspace_panel_ms"]), ({"mail": 2500}, 9000))
        self.assertEqual(s["workspace_items_opened"], 1)
        # summed per item, kept apart per tab: an email and a calendar event may share an id
        self.assertEqual(s["workspace_items_viewed_ms"], {"mail": {"33": 1500}, "calendar": {"33": 50}})
        self.assertEqual((s["trace_cards_expanded"], s["trace_cards_viewed_ms"]), (1, {"step-1": 300}))
        self.assertEqual((s["scenario_visible_ms"], s["stage_loads"]), (25000, 1))

    def test_trial_without_skill_use_is_summarised_as_not_opened(self):
        s = ilog.summarize([row("session_start", 0)])
        self.assertFalse(s["skills_opened"])
        self.assertFalse(s["skills_tab_opened"])
        self.assertIsNone(s["first_skill_open_s"])
        self.assertFalse(s["workspace_opened"])
        self.assertEqual(s["workspace_items_viewed_ms"], {})


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = preview_handler()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    # -- skill documentation ---------------------------------------------------------------------------------
    def test_skills_are_those_of_the_execution_and_identical_for_both_conditions(self):
        record = json.loads((ROOT / json.loads((ROOT / "src/data/stimuli" / FOLDER / "provenance.json").read_text())
                             ["source"]["file"]).read_text())
        plain = self.request(f"/api/skills?s={FOLDER}")
        self.assertEqual(plain[0], 200)
        self.assertEqual([s["id"] for s in json.loads(plain[1])], [s["id"] for s in record["skills"]])
        for cond in ("low", "high"):
            self.assertEqual(self.request(f"/api/skills?s={FOLDER}&c={cond}"), plain)
        for skill in record["skills"]:
            status, text = self.request(f"/api/skill?s={FOLDER}&k={skill['id']}")
            doc = json.loads(text)
            self.assertEqual(status, 200)
            on_disk = (ROOT / skill["file"]).read_text()
            self.assertTrue(on_disk.endswith(doc["markdown"].rstrip("\n") + "\n") or doc["markdown"] in on_disk)
            self.assertNotIn("---\nname:", doc["markdown"])  # front matter is not part of the rendered body
            self.assertEqual(self.request(f"/api/skill?s={FOLDER}&k={skill['id']}&c=high"), (status, text))

    def test_other_skills_stimuli_and_paths_are_refused(self):
        for path in (f"/api/skill?s={FOLDER}&k=calendar-lookup",  # a skill this execution did not have
                     f"/api/skill?s={FOLDER}&k=../../skill_taxonomy", f"/api/skill?s=user_task_5__no_injection&k=email-lookup",
                     "/api/skills?s=stim_52669833c1f9", f"/api/skill?s={FOLDER}", "/api/skills"):
            self.assertIn(self.request(path)[0], (400, 404), path)

    def test_a_changed_skill_file_is_not_served(self):
        entry = {"id": "x", "file": "src/data/skills/email-lookup/SKILL.md", "sha256": "0" * 64}
        with self.assertRaises(FileNotFoundError):
            ps.skill_text(entry)

    def test_nothing_researcher_only_in_skill_or_workspace_responses(self):
        for path in (f"/api/skills?s={FOLDER}", f"/api/skill?s={FOLDER}&k=email-lookup", f"/api/workspace?s={FOLDER}"):
            blob = self.request(path)[1]
            for key in ('"sha256"', '"utility"', '"run_id"', '"provenance"', '"after"', '"changes"', '"replay"',
                        '"stimulus_id"', '"transparency"'):
                self.assertNotIn(key, blob, path)

    def test_participant_pages_are_identical_in_both_conditions(self):
        self.assertEqual(self.request(f"/chat?s={FOLDER}&c=low"), self.request(f"/chat?s={FOLDER}&c=high"))
        page = self.request(f"/chat?s={FOLDER}&c=low")[1]
        self.assertEqual(page.count("panel-toggle"), 1)  # one Workspace button; skills are a tab inside the panel
        self.assertNotIn("skills-toggle", page)
        self.assertNotIn('id="skills"', page)
        self.assertIn('aria-expanded="false"', page)  # nothing is open by default

    def test_only_the_trace_differs_between_conditions(self):
        low = json.loads(self.request(f"/api/stimulus?s={FOLDER}&c=low")[1])
        high = json.loads(self.request(f"/api/stimulus?s={FOLDER}&c=high")[1])
        self.assertEqual({k: v for k, v in high.items() if k != "trace"}, low)
        self.assertNotIn("trace", low)

    # -- logging endpoint ------------------------------------------------------------------------------------
    def test_a_researcher_preview_records_nothing(self):
        """Events are stored only for a participant link; a preview server has no participant database."""
        body = {"trial_id": "P001-t01", "type": "workspace_panel_open"}
        self.assertEqual(self.request("/api/log", body)[0], 404)
        self.assertEqual(self.request("/api/log", {"token": "x" * 32, **body})[0], 404)

    def test_log_is_write_only_for_participants(self):
        for path in ("/api/log?trial_id=P001-t01", "/api/log", "/api/logs", "/data/interaction_logs/",
                     "/data/participants/", "/src/data/participants/participants.db"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/other", {"x": 1})[0], 404)


if __name__ == "__main__":
    unittest.main()
