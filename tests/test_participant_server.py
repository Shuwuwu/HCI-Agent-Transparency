"""Tests for participant_server.py: sanitized views, condition parity, researcher-only isolation, HTTP routes."""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import participant_server as ps  # noqa: E402
from participant_http import preview_handler  # noqa: E402

SID = ps.DEFAULT_STIMULUS
assert SID == "user_task_1__no_injection"
# Strings that must never reach a participant: researcher-only files/fields.
FORBIDDEN = ("provenance", "source_message_indexes", "sha256", "system", "builder_version", "preview", "utility",
             "injection", "attack", "stimulus_id", "transparency", "entry")


def saved(cond):
    return json.loads((ps.STIMULI_DIR / SID / f"{cond}.json").read_text())


class EligibilityTest(unittest.TestCase):
    def test_exactly_20_eligible_all_from_v4b_successes(self):
        ids = ps.list_stimulus_ids()
        self.assertEqual(len(ids), 20)
        self.assertEqual(ids[0], SID)
        for sid in ids:
            prov = json.loads((ps.STIMULI_DIR / sid / "provenance.json").read_text())
            rec = json.loads((ps.ROOT / prov["source"]["file"]).read_text())
            self.assertTrue(rec["run_id"].endswith("__skills-v4b"))
            self.assertIs(rec["utility"], True)
            for cond in ps.CONDITIONS:
                ps.participant_view(sid, cond)

    def test_stimuli_folder_holds_exactly_the_20_active_stimuli(self):
        folders = sorted(p.name for p in ps.STIMULI_DIR.iterdir() if p.is_dir())
        self.assertEqual(folders, sorted(ps.list_stimulus_ids()))
        self.assertTrue(all(ps.STIMULUS_ID_RE.match(f) and f.endswith("__no_injection") for f in folders))

    def test_archived_stimuli_are_rejected_even_if_placed_in_the_stimuli_folder(self):
        """The eligibility rule itself, not just the folder layout, keeps excluded stimuli out."""
        archived = sorted((ps.ROOT / "archive" / "excluded_stimuli").iterdir())
        self.assertEqual(len(archived), 8)
        for source in archived:
            prov = json.loads((source / "provenance.json").read_text())
            with tempfile.TemporaryDirectory() as tmp:
                name = prov["source"]["scenario"] + "__no_injection"
                shutil.copytree(source, Path(tmp) / name)
                moved = json.loads((Path(tmp) / name / "provenance.json").read_text())
                moved["stimulus_folder"] = name  # even with a consistent folder name
                (Path(tmp) / name / "provenance.json").write_text(json.dumps(moved))
                ps.is_eligible.cache_clear()
                self.assertFalse(ps.is_eligible(name, Path(tmp)), source.name)
                for cond in ps.CONDITIONS:
                    with self.assertRaises(FileNotFoundError):
                        ps.participant_view(name, cond, Path(tmp))
        ps.is_eligible.cache_clear()

    def test_old_hash_ids_are_no_longer_valid_urls(self):
        for old in ("stim_52669833c1f9", "stim_170748905667"):
            with self.assertRaises(ValueError):
                ps.participant_view(old, "low")


class ViewTest(unittest.TestCase):
    def test_conditions_share_request_and_response(self):
        low, high = ps.participant_view(SID, "low"), ps.participant_view(SID, "high")
        self.assertEqual(low["user_request"], high["user_request"])
        self.assertEqual(low["final_response"], high["final_response"])
        self.assertEqual(low["user_request"], saved("low")["user_request"])
        self.assertEqual(low["final_response"], saved("low")["final_response"])

    def test_only_high_has_trace(self):
        self.assertNotIn("trace", ps.participant_view(SID, "low"))
        self.assertIn("trace", ps.participant_view(SID, "high"))

    def test_every_action_matches_saved_stimulus(self):
        for sid in ps.list_stimulus_ids():
            log = json.loads((ps.STIMULI_DIR / sid / "high.json").read_text())["action_log"]
            trace = ps.participant_view(sid, "high")["trace"]
            self.assertEqual(len(trace), len(log))
            for shown, src in zip(trace, log):
                self.assertEqual(shown["type"], src["type"])
                if src["type"] == "action":
                    for k in ("step", "action", "tool", "arguments", "outcome"):
                        self.assertEqual(shown[k], src[k] if k != "arguments" else
                                         [{**a, "truncated": bool(a["truncated"])} for a in src[k]])
                else:
                    self.assertEqual(shown["text"], src["text"])

    def test_no_researcher_fields_in_any_view(self):
        for sid in ps.list_stimulus_ids():
            for cond in ps.CONDITIONS:
                blob = json.dumps(ps.participant_view(sid, cond))
                for word in ('"provenance"', "source_message_indexes", "sha256", '"entry"', "stimulus_id",
                             '"transparency"', "builder_version"):
                    self.assertNotIn(word, blob)

    def test_invalid_input_rejected(self):
        for sid, cond in (("../x", "low"), ("stim_zzzzzzzzzzzz", "low"), ("user_task_1__no_injection/..", "low"), (SID, "medium"), ("", "low")):
            with self.assertRaises(ValueError):
                ps.participant_view(sid, cond)
        with self.assertRaises(FileNotFoundError):
            ps.participant_view("user_task_99__no_injection", "low")


class HttpTest(unittest.TestCase):
    """The researcher form of the server. / and /chat and the s= form of the content routes are opt-in
    (participant_server.py --researcher-preview); preview_handler() turns them on for these tests only."""

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

    def test_api_and_pages(self):
        status, body = self.get(f"/api/stimulus?s={SID}&c=low")
        self.assertEqual(status, 200)
        self.assertNotIn("trace", json.loads(body))
        self.assertEqual(self.get(f"/chat?s={SID}&c=high")[0], 200)
        self.assertEqual(self.get("/static/app.js")[0], 200)
        # The reusable scenario view is served and loaded by the chat page before app.js, which mounts it.
        self.assertEqual(self.get("/static/scenario_view.js")[0], 200)
        page = self.get("/chat")[1]
        self.assertLess(page.index("/static/scenario_view.js"), page.index("/static/app.js"))

    def test_researcher_files_not_reachable(self):
        for path in (f"/static/../data/stimuli/{SID}/provenance.json", f"/data/stimuli/{SID}/provenance.json",
                     f"/static/provenance.json", f"/api/stimulus?s={SID}/../{SID}&c=high",
                     "/api/stimulus?s=../../execution/runs/x&c=low", "/src/data/execution/runs/",
                     f"/stimuli/{SID}/preview.txt", "/static/%2e%2e/index.html"):
            self.assertIn(self.get(path)[0], (400, 404), path)

    def test_direct_urls_of_missing_or_archived_names_fail_and_index_lists_20(self):
        for sid in ("user_task_5__no_injection", "user_task_0__no_injection",  # tasks whose run failed
                    "user_task_1__important_instructions_injection_task_0", "stim_52669833c1f9"):
            for cond in ps.CONDITIONS:
                self.assertIn(self.get(f"/api/stimulus?s={sid}&c={cond}")[0], (400, 404), sid)
        index = self.get("/")[1]
        self.assertEqual(index.count("c=low"), 20)
        self.assertEqual(index.count("c=high"), 20)
        for sid in ps.list_stimulus_ids():
            for cond in ps.CONDITIONS:
                self.assertEqual(self.get(f"/api/stimulus?s={sid}&c={cond}")[0], 200)

    def test_post_rejected(self):
        req = urllib.request.Request(self.base + "/api/stimulus", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req)


if __name__ == "__main__":
    unittest.main()
