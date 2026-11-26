"""Coverage tests: every one of the 20 active study scenarios has its own workspace (before state) and its mapped
skills, served identically in both transparency conditions, with nothing from the after state or from the
researcher-only files. Complements test_workspace_snapshots.py (deep checks of user_task_13)."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import agentdojo_runs as runs  # noqa: E402
import participant_server as ps  # noqa: E402
import tool_result_parser as trp  # noqa: E402
from participant_http import preview_handler  # noqa: E402

FOLDERS = sorted(p.name for p in (ROOT / "src/data/stimuli").iterdir() if p.is_dir())
KIND_KEY = {"emails": "subject", "events": "title", "files": "filename"}


def load(folder):
    prov = json.loads((ROOT / "src/data/stimuli" / folder / "provenance.json").read_text())
    record = json.loads((ROOT / prov["source"]["file"]).read_text())
    wdir = ROOT / "src/data/workspace" / folder
    return (prov, record, json.loads((wdir / "workspace.json").read_text()),
            json.loads((wdir / "provenance.json").read_text()))


def norm(value):
    """Compare-friendly form: tool results print datetimes as 'YYYY-MM-DD HH:MM:SS' (the snapshot stores ISO
    'YYYY-MM-DDTHH:MM:SS') and the stdlib parser returns some scalars as text."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):  # tool_result_parser keeps some scalars as text (for example sizes)
        return str(value)
    if isinstance(value, str):
        return value.replace("T", " ")
    if isinstance(value, list):
        return [norm(v) for v in value]
    if isinstance(value, dict):
        return {k: norm(v) for k, v in value.items()}
    return value


def records_read_before_first_write(record):
    """Parsed record dicts from tool results that came before the agent's first data-changing call."""
    out, pending, written = [], {}, False
    for m in record["trace"]:
        for c in m.get("tool_calls") or []:
            pending.setdefault(c["id"], []).append(c["function"])
            written = written or c["function"] in runs.WRITE_TOOLS
        if m["role"] == "tool" and pending.get(m["tool_call_id"]):
            tool = pending[m["tool_call_id"]].pop(0)
            if written or tool in runs.WRITE_TOOLS or not m["content"]:
                continue
            try:
                parsed = trp.parse(m["content"])
            except Exception:  # plain-text results (a day, a message) carry no records
                continue
            out += [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict) and "id_" in p]
    return out


class CoverageTest(unittest.TestCase):
    def test_exactly_the_20_active_stimuli_have_a_workspace(self):
        self.assertEqual(len(FOLDERS), 20)
        self.assertEqual(sorted(p.name for p in (ROOT / "src/data/workspace").iterdir()), FOLDERS)
        self.assertEqual(sorted(ps.list_stimulus_ids()), FOLDERS)

    def test_each_workspace_belongs_to_its_stimulus_and_replay_matched(self):
        for folder in FOLDERS:
            prov, record, ws, wprov = load(folder)
            self.assertEqual(wprov["stimulus_folder"], folder)
            self.assertEqual(wprov["stimulus_id"], prov["stimulus_id"])
            self.assertEqual(wprov["source"], prov["source"], folder)
            self.assertEqual(wprov["agentdojo_commit"], record["agentdojo_commit"], folder)
            self.assertTrue(wprov["replay"]["all_match_saved_results"], folder)
            n_calls = sum(len(m.get("tool_calls") or []) for m in record["trace"])
            self.assertEqual(wprov["replay"]["calls"], n_calls, folder)
            self.assertEqual(set(ws), {"account", "before", "after"})

    def test_before_state_agrees_with_every_record_the_agent_read_before_changing_anything(self):
        checked = 0
        for folder in FOLDERS:
            _, record, ws, _ = load(folder)
            for item in records_read_before_first_write(record):
                kind = next((k for k, key in KIND_KEY.items() if key in item), None)
                if kind is None:
                    continue
                mine = next((i for i in ws["before"][kind] if i["id_"] == str(item["id_"])), None)
                self.assertIsNotNone(mine, f"{folder}: {kind} {item['id_']} read by the agent is missing")
                for field, value in item.items():
                    if field in mine:  # `read` and other bookkeeping fields are not part of the snapshot
                        self.assertEqual(norm(mine[field]), norm(value), f"{folder} {kind} {item['id_']} {field}")
                checked += 1
        self.assertGreater(checked, 30)  # a real cross-check, not an empty loop

    def test_mapped_skills_are_the_executions_and_the_taxonomys(self):
        for folder in FOLDERS:
            _, record, _, _ = load(folder)
            task = record["scenario"]
            taxonomy = json.loads((ROOT / "src/data/experiment" / task / "skills.json").read_text())["skills"]
            self.assertEqual([s["id"] for s in record["skills"]], taxonomy, folder)
            self.assertEqual([s["id"] for s in ps.participant_skills(folder)], taxonomy, folder)
            for entry in record["skills"]:
                self.assertEqual(ps.skill_text(entry), (ROOT / entry["file"]).read_text(), folder)

    def test_served_before_state_hides_every_change_the_agent_made(self):
        for folder in FOLDERS:
            _, _, ws, wprov = load(folder)
            served = ps.participant_workspace(folder)["state"]
            self.assertEqual(served, ws["before"], folder)
            after = {k: {i["id_"]: i for i in ws["after"][k]} for k in KIND_KEY}
            for change in wprov["changes"]:
                kind, id_ = change["kind"], change["id"]
                ids = [i["id_"] for i in served[kind]]
                if change["change"] == "added":
                    self.assertNotIn(id_, ids, f"{folder}: {kind} {id_} exists only after")
                else:  # modified or removed items are served as they were before
                    self.assertIn(id_, ids, folder)
                    if change["change"] == "modified":
                        shown = next(i for i in served[kind] if i["id_"] == id_)
                        self.assertNotEqual(shown, after[kind][id_], folder)


class AllScenariosHttpTest(unittest.TestCase):
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

    def test_every_scenario_serves_identical_workspace_and_skills_in_both_conditions(self):
        for folder in FOLDERS:
            ws = self.get(f"/api/workspace?s={folder}")
            skills = self.get(f"/api/skills?s={folder}")
            self.assertEqual((ws[0], skills[0]), (200, 200), folder)
            ids = [s["id"] for s in json.loads(skills[1])]
            self.assertTrue(ids, folder)
            for cond in ("low", "high"):
                self.assertEqual(self.get(f"/api/workspace?s={folder}&c={cond}"), ws, folder)
                self.assertEqual(self.get(f"/api/skills?s={folder}&c={cond}"), skills, folder)
                for k in ids:
                    self.assertEqual(self.get(f"/api/skill?s={folder}&k={k}&c={cond}"),
                                     self.get(f"/api/skill?s={folder}&k={k}"), (folder, k))
                self.assertEqual(self.get(f"/chat?s={folder}&c={cond}"), self.get(f"/chat?s={folder}&c=low"))

    def test_only_the_trace_differs_between_conditions_in_every_scenario(self):
        for folder in FOLDERS:
            low = json.loads(self.get(f"/api/stimulus?s={folder}&c=low")[1])
            high = json.loads(self.get(f"/api/stimulus?s={folder}&c=high")[1])
            self.assertEqual({k: v for k, v in high.items() if k != "trace"}, low, folder)
            self.assertNotIn("trace", low)
            self.assertTrue(high["trace"], folder)

    def test_no_researcher_data_in_any_participant_response(self):
        bad = ('"after"', '"changes"', '"replay"', '"provenance"', '"sha256"', '"utility"', '"run_id"', '"stimulus_id"',
               '"stimulus_folder"', '"agentdojo_commit"', '"security"', '"injection')
        for folder in FOLDERS:
            _, _, _, wprov = load(folder)
            paths = [f"/api/workspace?s={folder}", f"/api/skills?s={folder}"]
            paths += [f"/api/skill?s={folder}&k={s['id']}" for s in json.loads(self.get(paths[1])[1])]
            for path in paths:
                blob = self.get(path)[1]
                for key in bad:
                    self.assertNotIn(key, blob, path)
            self.assertEqual(self.get(f"/data/workspace/{folder}/provenance.json")[0], 404)
            self.assertEqual(self.get(f"/api/workspace?s={folder}&st=after")[1], self.get(f"/api/workspace?s={folder}")[1])


if __name__ == "__main__":
    unittest.main()
