"""Tests for the workspace snapshots (build_workspace_snapshots.py output) and the participant workspace API.

The committed user_task_13 snapshot is checked against the saved execution record, independently of the replay that
produced it: original email and files against what the agent's own tool results showed, the changes against the
agent's write calls. The replay itself needs AgentDojo (.venv-agentdojo) and is covered by the provenance check.
"""

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

import build_workspace_snapshots as bws  # noqa: E402
import participant_server as ps  # noqa: E402
import tool_result_parser as trp  # noqa: E402
from participant_http import preview_handler  # noqa: E402

FOLDER = "user_task_13__no_injection"
WS_DIR = ROOT / "src" / "data" / "workspace" / FOLDER
WS = json.loads((WS_DIR / "workspace.json").read_text())
PROV = json.loads((WS_DIR / "provenance.json").read_text())
STIM_PROV = json.loads((ROOT / "src/data/stimuli" / FOLDER / "provenance.json").read_text())
RECORD = json.loads((ROOT / STIM_PROV["source"]["file"]).read_text())


def calls(tool):
    return [c for m in RECORD["trace"] if m.get("tool_calls") for c in m["tool_calls"] if c["function"] == tool]


def results(tool):
    """Parsed saved results of every call of a tool, in order."""
    out, pending = [], {}
    for m in RECORD["trace"]:
        for c in m.get("tool_calls") or []:
            pending.setdefault(c["id"], []).append(c["function"])
        if m["role"] == "tool" and pending.get(m["tool_call_id"]):
            if pending[m["tool_call_id"]].pop(0) == tool and m["content"]:
                out.append(trp.parse(m["content"]))
    return out


def find(state, kind, **match):
    return [i for i in state[kind] if all(i[k] == v for k, v in match.items())]


class SnapshotDataTest(unittest.TestCase):
    def test_replay_was_checked_against_every_saved_call(self):
        n_calls = len(calls("search_emails")) + len(calls("search_files_by_filename")) + len(calls("append_to_file")) \
            + len(calls("search_contacts_by_name")) + len(calls("send_email"))
        self.assertEqual(PROV["replay"]["calls"], n_calls)
        self.assertTrue(PROV["replay"]["all_match_saved_results"])
        self.assertEqual(PROV["source"]["sha256"], STIM_PROV["source"]["sha256"])

    def test_original_email_matches_what_the_agent_read(self):
        saved = results("search_emails")[0][0]
        mine = find(WS["before"], "emails", id_=saved["id_"])[0]
        self.assertEqual(mine["sender"], "david.smith@bluesparrowtech.com")
        self.assertEqual((mine["subject"], mine["body"], mine["recipients"]), (saved["subject"], saved["body"], saved["recipients"]))
        self.assertEqual(mine["status"], "received")

    def test_original_files_match_what_the_agent_read(self):
        for call_result in results("search_files_by_filename"):
            for saved in call_result:
                mine = find(WS["before"], "files", id_=saved["id_"])[0]
                self.assertEqual((mine["filename"], mine["content"]), (saved["filename"], saved["content"]))

    def test_the_docx_is_updated_by_exactly_the_appended_text(self):
        appended = calls("append_to_file")[0]["args"]
        file_id = appended["file_id"]
        before = find(WS["before"], "files", id_=file_id)[0]
        after = find(WS["after"], "files", id_=file_id)[0]
        self.assertEqual(before["filename"], "team-building-activities.docx")
        self.assertEqual(after["content"], before["content"] + appended["content"])
        self.assertIn("Virtual Escape Room", after["content"])
        self.assertNotIn("Virtual Escape Room", before["content"])

    def test_feedback_xlsx_is_unchanged(self):
        before = find(WS["before"], "files", filename="feedback.xlsx")[0]
        after = find(WS["after"], "files", filename="feedback.xlsx")[0]
        self.assertEqual(before, after)
        self.assertIn("Olivia Smith,olivia.smith@fastmail.com,4", before["content"])

    def test_the_sent_email_exists_only_after_and_equals_the_send_call(self):
        sent_call = calls("send_email")[0]["args"]
        self.assertEqual(find(WS["before"], "emails", subject="Feedback scores"), [])
        mine = find(WS["after"], "emails", subject="Feedback scores")
        self.assertEqual(len(mine), 1)
        self.assertEqual((mine[0]["recipients"], mine[0]["body"], mine[0]["status"]),
                         (sent_call["recipients"], sent_call["body"], "sent"))
        self.assertEqual(mine[0]["sender"], WS["account"]["email"])

    def test_the_only_changes_are_the_two_the_agent_made(self):
        self.assertEqual(PROV["changes"], [{"kind": "emails", "id": "34", "change": "added"},
                                          {"kind": "files", "id": "3", "change": "modified",
                                           "fields": ["content", "last_modified", "size"]}])
        self.assertEqual(WS["before"]["events"], WS["after"]["events"])

    def test_workspace_json_holds_only_participant_fields(self):
        for state in ("before", "after"):
            for kind, fields in ps.WORKSPACE_FIELDS.items():
                for item in WS[state][kind]:
                    self.assertEqual(set(item), set(fields))
        self.assertEqual(set(WS), {"account", "before", "after"})
        blob = json.dumps(WS)
        for word in ("run_id", "utility", "provenance", "sha256", "stim_", "injection_task", "attack"):
            self.assertNotIn(word, blob)

    def test_diff_and_clock_helpers(self):
        a = {"emails": [{"id_": "1", "x": 1}], "events": [], "files": [{"id_": "2", "c": "a"}]}
        b = {"emails": [{"id_": "1", "x": 1}, {"id_": "3", "x": 2}], "events": [], "files": [{"id_": "2", "c": "b"}]}
        self.assertEqual(bws.diff_snapshots(a, b), [{"kind": "emails", "id": "3", "change": "added"},
                                                    {"kind": "files", "id": "2", "change": "modified", "fields": ["c"]}])
        self.assertEqual(str(bws.saved_clock("send_email", "id_: '1'\ntimestamp: 2026-11-17 22:16:19.913805")),
                         "2026-11-17 22:16:19.913805")


class WorkspaceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), preview_handler())
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_workspace_is_identical_whatever_the_condition(self):
        # The endpoint has no condition parameter, so a condition in the URL cannot change the answer.
        plain = self.get(f"/api/workspace?s={FOLDER}")
        for cond in ("low", "high"):
            self.assertEqual(self.get(f"/api/workspace?s={FOLDER}&c={cond}"), plain)
        self.assertEqual(plain[0], 200)
        view = json.loads(plain[1])
        self.assertEqual(set(view), {"account", "state"})
        self.assertEqual(view["state"], WS["before"])  # the state BEFORE the agent ran, nothing else

    def test_after_state_is_never_served(self):
        blob = self.get(f"/api/workspace?s={FOLDER}")[1]
        self.assertNotIn('"after"', blob)
        # Content that exists only after the execution: the sent email and the appended activities.
        self.assertEqual([e for e in json.loads(blob)["state"]["emails"] if e["subject"] == "Feedback scores"], [])
        self.assertNotIn("Virtual Escape Room", blob)
        self.assertNotIn("Charity Volunteering Day", blob)
        for path in (f"/api/workspace?s={FOLDER}&st=after", f"/api/workspace?s={FOLDER}&state=after"):
            self.assertNotIn("Virtual Escape Room", self.get(path)[1])
        # the after state exists on disk for researchers
        self.assertIn("Virtual Escape Room", json.dumps(WS["after"]))

    def test_workspace_has_no_trace_or_researcher_data(self):
        blob = self.get(f"/api/workspace?s={FOLDER}")[1]
        self.assertEqual(set(json.loads(blob)), {"account", "state"})  # no changes/replay/provenance keys
        for key in ('"action_log"', '"tool_calls"', '"sha256"', '"utility"', '"changes"', '"replay"', '"run_id"'):
            self.assertNotIn(key, blob)

    def test_missing_ineligible_and_invalid_requests_fail(self):
        # user_task_5 is a failed run, not an active stimulus
        for sid in ("user_task_5__no_injection", "user_task_13__no_injection/..", "stim_52669833c1f9", "", "../x"):
            self.assertIn(self.get(f"/api/workspace?s={sid}")[0], (400, 404), sid)
        for path in (f"/data/workspace/{FOLDER}/provenance.json", f"/static/../data/workspace/{FOLDER}/workspace.json",
                     "/static/workspace.json", "/static/provenance.json"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_ineligible_stimulus_is_refused_even_with_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / FOLDER).mkdir()
            (Path(tmp) / FOLDER / "workspace.json").write_text(json.dumps(WS))
            ps.is_eligible.cache_clear()
            with self.assertRaises(FileNotFoundError):  # no stimulus provenance in the empty stimuli folder
                ps.participant_workspace(FOLDER, stimuli_dir=Path(tmp), workspace_dir=Path(tmp))
            ps.is_eligible.cache_clear()

    def test_chat_page_scripts_are_served(self):
        for name in ("workspace.js", "previews.js"):
            self.assertEqual(self.get(f"/static/{name}")[0], 200)
        page = self.get(f"/chat?s={FOLDER}&c=low")[1]
        self.assertIn("/static/workspace.js", page)


if __name__ == "__main__":
    unittest.main()
