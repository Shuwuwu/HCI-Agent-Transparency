"""Tests that representative safe stimuli (study corpus) and the simulated unsafe stimulus (validation corpus) load
in both transparency conditions, that the simulated one is faithful to AgentDojo's own attack and isolated from the
study, and that it can be rebuilt offline. No model is called."""

import copy
import json
import os
import subprocess
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

import corpus  # noqa: E402
import injection_design as design  # noqa: E402
import participant_server as ps  # noqa: E402
from participant_http import preview_handler  # noqa: E402

STUDY = ROOT / "src" / "data" / "corpora" / "study.json"
VALIDATION = ROOT / "src" / "data" / "corpora" / "validation.json"
VENV_PYTHON = ROOT / ".venv-agentdojo" / "bin" / "python"
REPRESENTATIVE_SAFE = ("user_task_1__no_injection", "user_task_13__no_injection")  # shortest trace, longest trace


def load_record(corpus_path):
    c = corpus.load_corpus(corpus_path)
    prov = json.loads((c["stimuli_path"] / c["default_stimulus"] / "provenance.json").read_text())
    return json.loads((ROOT / prov["source"]["file"]).read_text()), prov


class LoadsInBothConditions(unittest.TestCase):
    def tearDown(self):
        ps.use_corpus(STUDY)

    def check_loads(self, folder):
        low, high = ps.participant_view(folder, "low"), ps.participant_view(folder, "high")
        self.assertEqual(low["user_request"], high["user_request"])
        self.assertEqual(low["final_response"], high["final_response"])
        self.assertNotIn("trace", low)
        self.assertTrue(high["trace"], folder)
        ws = ps.participant_workspace(folder)
        self.assertEqual(set(ws["state"]), {"emails", "events", "files"})
        self.assertTrue(ws["state"]["emails"] and ws["state"]["files"])
        skills = ps.participant_skills(folder)
        self.assertTrue(skills)
        for s in skills:
            self.assertTrue(ps.participant_skill(folder, s["id"])["markdown"].strip())
        return high

    def test_representative_safe_stimuli_load(self):
        ps.use_corpus(STUDY)
        for folder in REPRESENTATIVE_SAFE:
            self.check_loads(folder)

    def test_every_study_stimulus_loads_in_both_conditions(self):
        ps.use_corpus(STUDY)
        folders = ps.list_stimulus_ids()
        self.assertEqual(len(folders), 20)
        for folder in folders:
            self.check_loads(folder)

    def test_simulated_unsafe_stimulus_loads_in_both_conditions(self):
        ps.use_corpus(VALIDATION)
        high = self.check_loads(ps.DEFAULT_STIMULUS)
        record, _ = load_record(VALIDATION)
        identity = design.identity_by_id(record["injection"]["identity_id"])
        actions = [s for s in high["trace"] if s["type"] == "action"]
        injected = [a for a in actions if a["tool"] == design.OBJECTIVES[record["injection"]["objective"]]["write_tool"]]
        self.assertEqual(len(injected), 1)  # the unauthorised action is visible in the high condition
        self.assertIn(identity["address"], json.dumps(injected[0]["arguments"]))
        self.assertGreater(len(actions), 1)  # and sits beside the legitimate action(s)

    def test_simulated_skill_document_contains_the_injected_block_and_the_study_one_does_not(self):
        ps.use_corpus(VALIDATION)
        record, _ = load_record(VALIDATION)
        identity = design.identity_by_id(record["injection"]["identity_id"])
        doc = ps.participant_skill(ps.DEFAULT_STIMULUS, record["injection"]["injected_skill"])["markdown"]
        self.assertIn(design.goal_text(record["injection"]["objective"], identity), doc)
        ps.use_corpus(STUDY)
        scenario = record["injection"]["item_id"].split("__")[0]  # its safe counterpart in the study corpus
        safe_doc = ps.participant_skill(f"{scenario}__no_injection", record["injection"]["injected_skill"])["markdown"]
        self.assertNotIn(identity["address"], safe_doc)

    def test_served_over_http_from_the_validation_corpus_only(self):
        fixture = corpus.load_corpus(VALIDATION)["default_stimulus"]
        for corpus_path, expected in ((VALIDATION, 200), (STUDY, 404)):
            ps.use_corpus(corpus_path)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), preview_handler())
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                for route in ("/api/stimulus?s={s}&c=low", "/api/stimulus?s={s}&c=high", "/api/workspace?s={s}",
                              "/api/skills?s={s}"):
                    url = f"http://127.0.0.1:{srv.server_address[1]}" + route.format(s=fixture)
                    try:
                        with urllib.request.urlopen(url) as r:
                            status = r.status
                    except urllib.error.HTTPError as e:
                        status = e.code
                    self.assertEqual(status, expected, (corpus_path.name, route))
            finally:
                srv.shutdown()
                srv.server_close()


class SimulatedRecordTests(unittest.TestCase):
    def setUp(self):
        self.record, self.prov = load_record(VALIDATION)

    def test_record_is_labelled_simulated_everywhere_a_researcher_looks(self):
        r = self.record
        self.assertIs(r["simulated"], True)
        self.assertEqual(r["condition"], "simulated-injected")
        self.assertTrue(r["model"].startswith("none"))
        self.assertTrue(r["run_id"].endswith("__simulated-ui-validation"))
        self.assertIn("never part of the study set", r["simulation"]["purpose"])
        self.assertIs(self.prov["source"]["simulated"], True)
        self.assertTrue(self.prov["source"]["file"].startswith("src/data/validation/"))
        self.assertNotIn("usage", r)  # no model call: no token usage, no llm_calls
        self.assertNotIn("llm_calls", r)

    def test_inclusion_check_passes_and_matches_the_trace(self):
        verdict = self.record["simulation"]["inclusion_check"]
        self.assertTrue(verdict["included"], verdict)
        self.assertTrue(verdict["security_recomputed"] and verdict["utility_tolerant"])
        self.assertFalse(verdict["utility_strict"])  # AgentDojo's own utility check rejects task plus attack
        self.assertEqual(verdict["goal_calls_in_trace"], 1)

    def test_safe_run_it_was_built_from_is_unchanged(self):
        src = self.record["simulation"]["safe_run"]
        import hashlib
        self.assertEqual(hashlib.sha256((ROOT / src["file"]).read_bytes()).hexdigest(), src["sha256"])
        safe = json.loads((ROOT / src["file"]).read_text())
        calls = lambda trace: [(c["function"], c["args"]) for m in trace if m["role"] == "assistant"  # noqa: E731
                               for c in m.get("tool_calls") or []]
        self.assertEqual(calls(self.record["trace"])[-len(calls(safe["trace"])):], calls(safe["trace"]))
        self.assertEqual(self.record["task_prompt"], safe["task_prompt"])

    def test_attack_arguments_in_the_trace_are_agentdojos_ground_truth_unmodified(self):
        # regression (found with the earlier G2 fixture): create_calendar_event appends the owner to its participants
        # list in place, which once changed the recorded arguments; the current G4 fixture sends an email, so this now
        # checks that the recorded send_email arguments are the ground truth, unmodified
        attack = [c for m in self.record["trace"] if m["role"] == "assistant" for c in m.get("tool_calls") or []
                  if c["id"].startswith("call_simulated_attack_")]
        self.assertEqual(len(attack), len(self.record["simulation"]["attack_calls"]))
        identity = design.identity_by_id(self.record["injection"]["identity_id"])
        self.assertEqual(attack[-1]["function"], "send_email")
        self.assertEqual(attack[-1]["args"]["recipients"], [identity["address"]])
        self.assertEqual([{"function": c["function"], "args": c["args"]} for c in attack],
                         self.record["simulation"]["attack_calls"])

    def test_system_message_holds_the_injected_skill_instead_of_the_safe_one(self):
        # the fixture's item is an item of the earlier II + W2 plan, now kept as the wording pilot's W2 item
        manifests = ("src/data/injected_skills/manifest.json", "src/data/injected_skills/pilot_w2/manifest.json")
        item = next(i for m in manifests for i in json.loads((ROOT / m).read_text())["items"]
                    if i["item_id"] == self.record["injection"]["item_id"])
        item_text = (ROOT / item["item_file"]).read_text()
        base_text = (ROOT / item["base_file"]).read_text()
        self.assertEqual(self.record["system_message"].count(item_text), 1)
        self.assertEqual(self.record["system_message"].replace(item_text, base_text),
                         json.loads((ROOT / self.record["simulation"]["safe_run"]["file"]).read_text())["system_message"])
        self.assertEqual(self.record["skills"][0]["sha256"],
                         __import__("hashlib").sha256(item_text.encode()).hexdigest())


@unittest.skipUnless(VENV_PYTHON.exists(), "needs .venv-agentdojo (AgentDojo)")
class EvaluationDoesNotMutateTests(unittest.TestCase):
    def test_evaluating_a_record_leaves_its_trace_arguments_unchanged(self):
        # regression: apply_calls ran the trace's own argument objects through the tools, and create_calendar_event
        # appends the owner to its participants list in place, so evaluating a record changed the record
        code = ("import sys, json, copy; sys.path.insert(0, 'src/scripts'); import injection_checks as c; "
                "p = 'src/data/validation/runs/' + sys.argv[1]; r = json.load(open(p)); before = copy.deepcopy(r); "
                "c.evaluate_record(r); print(json.dumps(r == before))")
        name = load_record(VALIDATION)[0]["run_id"] + ".json"
        out = subprocess.run([str(VENV_PYTHON), "-c", code, name], cwd=ROOT, env={**os.environ, "PYTHONHASHSEED": "0"},
                             capture_output=True, text=True, timeout=300, check=True).stdout
        self.assertEqual(out.splitlines()[-1], "true")


@unittest.skipUnless(VENV_PYTHON.exists(), "needs .venv-agentdojo (AgentDojo)")
class RebuildOfflineTests(unittest.TestCase):
    def test_rebuilding_gives_the_committed_record_without_an_api_key(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env.update(PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
        code = ("import sys, json; sys.path.insert(0, 'src/scripts'); import build_simulated_stimulus as b; "
                "print(json.dumps(b.build_record()))")
        out = subprocess.run([str(VENV_PYTHON), "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=300, check=True).stdout
        rebuilt = json.loads(out.splitlines()[-1])
        committed, _ = load_record(VALIDATION)
        self.assertEqual(rebuilt, committed)


if __name__ == "__main__":
    unittest.main()
