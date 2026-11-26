"""Tests for the participant interface's skill documentation and interaction logging: identical access in both
conditions, hidden researcher data, and the events recorded when skills and workspace panels are used."""

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
import participant_server as ps  # noqa: E402
from participant_http import preview_handler  # noqa: E402

FOLDER = "user_task_13__no_injection"
SESSION = "11111111-2222-3333-4444-555555555555"


def event(kind, **fields):
    return {"session": SESSION, "type": kind, **fields}


class LogModuleTest(unittest.TestCase):
    def test_valid_events_are_stored_with_server_time_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ilog.append_event(event("session_start", stimulus=FOLDER, condition="high"), tmp)
            second = ilog.append_event(event("skill_doc_open", skill_id="email-lookup", client_ms=1700000000000), tmp)
            self.assertEqual((first["seq"], second["seq"]), (0, 1))
            self.assertIn("server_time", second)
            self.assertEqual(len(ilog.read_session(Path(tmp) / f"{SESSION}.jsonl")), 2)

    def test_invalid_events_are_rejected_and_nothing_is_written(self):
        bad = [event("unknown_type"), event("skill_doc_open"), event("skill_doc_open", skill_id="../x"),
               event("skill_doc_view", skill_id="a", duration_ms=-1), event("workspace_tab_view", tab="trash", duration_ms=1),
               event("session_start", stimulus=FOLDER, condition="medium"),
               event("workspace_panel_open", extra="field"), {"type": "workspace_panel_open"},
               {"session": "../../etc", "type": "workspace_panel_open"}, "not an object",
               event("session_start", stimulus="x" * 3000, condition="low")]
        with tempfile.TemporaryDirectory() as tmp:
            for e in bad:
                with self.assertRaises(ilog.LogError, msg=str(e)[:60]):
                    ilog.append_event(e, tmp)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_session_event_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            for _ in range(ilog.MAX_EVENTS_PER_SESSION):
                ilog.append_event(event("workspace_panel_open"), tmp)
            with self.assertRaises(ilog.LogError):
                ilog.append_event(event("workspace_panel_open"), tmp)

    def test_summary_measures(self):
        with tempfile.TemporaryDirectory() as tmp:
            for e in (event("session_start", stimulus=FOLDER, condition="low", pid="p1"),
                      event("workspace_panel_open"), event("skills_tab_open"),
                      event("skill_doc_open", skill_id="email-lookup"),
                      event("skill_doc_view", skill_id="email-lookup", duration_ms=4000),
                      event("skill_doc_view", skill_id="email-lookup", duration_ms=1000),
                      event("skills_tab_view", duration_ms=6000),
                      event("workspace_tab_open", tab="mail"),
                      event("workspace_item_open", tab="mail", item_id="33"),
                      event("workspace_tab_view", tab="mail", duration_ms=2500)):
                ilog.append_event(e, tmp)
            s = ilog.summarize(ilog.read_session(Path(tmp) / f"{SESSION}.jsonl"))
        self.assertEqual((s["condition"], s["pid"], s["skills_opened"], s["workspace_opened"]), ("low", "p1", True, True))
        self.assertEqual(s["skills_viewed_ms"], {"email-lookup": 5000})
        self.assertEqual((s["skills_tab_opened"], s["skills_tab_ms"]), (True, 6000))
        self.assertEqual(s["workspace_tabs_viewed_ms"], {"mail": 2500})
        self.assertEqual(s["workspace_items_opened"], 1)
        self.assertIsNotNone(s["first_skill_open_s"])

    def test_session_without_skill_use_is_summarised_as_not_opened(self):
        with tempfile.TemporaryDirectory() as tmp:
            ilog.append_event(event("session_start", stimulus=FOLDER, condition="high"), tmp)
            s = ilog.summarize(ilog.read_session(Path(tmp) / f"{SESSION}.jsonl"))
        self.assertFalse(s["skills_opened"])
        self.assertFalse(s["skills_tab_opened"])
        self.assertIsNone(s["first_skill_open_s"])
        self.assertFalse(s["workspace_opened"])


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        handler = preview_handler(log_dir=Path(cls.tmp.name))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

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
    def test_post_log_records_events_in_the_separate_folder(self):
        log_dir = Path(self.tmp.name)
        sid = "aaaaaaaa-1111-2222-3333-444444444444"
        for e in ({"type": "session_start", "stimulus": FOLDER, "condition": "high"},
                  {"type": "skills_tab_open"}, {"type": "skill_doc_open", "skill_id": "email-lookup"},
                  {"type": "skill_doc_view", "skill_id": "email-lookup", "duration_ms": 1234},
                  {"type": "workspace_panel_open"}, {"type": "workspace_tab_open", "tab": "drive"}):
            self.assertEqual(self.request("/api/log", {"session": sid, **e})[0], 204)
        records = ilog.read_session(log_dir / f"{sid}.jsonl")
        self.assertEqual([r["type"] for r in records], ["session_start", "skills_tab_open", "skill_doc_open",
                                                        "skill_doc_view", "workspace_panel_open", "workspace_tab_open"])
        self.assertEqual(self.request("/api/log", {"session": sid, "type": "bogus"})[0], 400)
        self.assertEqual(self.request("/api/log", "garbage")[0], 400)

    def test_log_is_write_only_for_participants(self):
        sid = "bbbbbbbb-1111-2222-3333-444444444444"
        self.request("/api/log", {"session": sid, "type": "workspace_panel_open"})
        for path in (f"/api/log?session={sid}", f"/static/{sid}.jsonl", f"/{sid}.jsonl", "/api/logs", "/data/interaction_logs/"):
            self.assertEqual(self.request(path)[0], 404, path)
        self.assertEqual(self.request("/other", {"x": 1})[0], 404)

    def test_log_folder_is_not_inside_any_served_or_tracked_participant_path(self):
        self.assertTrue(str(ilog.DEFAULT_LOG_DIR).endswith("src/data/interaction_logs"))
        self.assertIn("src/data/interaction_logs/", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
