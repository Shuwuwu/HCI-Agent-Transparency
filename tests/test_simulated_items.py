"""Tests of build_simulated_items.py and its outputs in src/data/simulated/: every simulated unsafe stimulus is
labelled as constructed, traceable to hashed inputs, differs from its genuine safe run only by the attack, passes the
inclusion checks, cites only real reference recordings, and is reachable only through the review corpus
(src/data/corpora/simulated.json, named by no experiment configuration) or, once marked reviewed there, through the
study corpus's separate simulated_source folder. Rebuilding the records needs
AgentDojo (.venv-agentdojo); that test is skipped without it. No model is called."""

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import build_simulated_items as items  # noqa: E402

VENV_PYTHON = ROOT / ".venv-agentdojo" / "bin" / "python"
SIM = ROOT / "src" / "data" / "simulated"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class SimulatedItemsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load(SIM / "manifest.json")
        cls.study = {i["item_id"]: i for i in items.manifest_items()}  # the 12 planned items and R7 replacements
        cls.records = {e["item_id"]: load(e["record"]["file"]) for e in cls.manifest["items"]}
        cls.inputs = {t["scenario"]: t for t in load("src/data/scenario_selection_inputs.json")["tasks"]}

    def test_manifest_covers_the_twelve_planned_items_and_the_replacement_in_order(self):
        self.assertEqual([e["item_id"] for e in self.manifest["items"]], items.study_item_ids())
        self.assertEqual(len(self.manifest["items"]), 13)
        replacement = self.manifest["items"][-1]  # R7: task 17 takes over task 24's item; task 24's stays built
        self.assertEqual((replacement["item_id"], replacement["replaces"]),
                         ("user_task_17__g2_ii_w3v1", "user_task_24__g2_ii_w3v1"))
        self.assertIn("user_task_24__g2_ii_w3v1", self.records)
        self.assertEqual(self.manifest["material_kind"], "simulated_trace")
        self.assertTrue(all(e["status"] == "built_unreviewed" for e in self.manifest["items"]))

    def test_every_hash_matches_its_file(self):
        for path, digest in self.manifest["sources"].items():
            self.assertEqual(sha(ROOT / path), digest, path)
        for e in self.manifest["items"]:
            self.assertEqual(sha(ROOT / e["record"]["file"]), e["record"]["sha256"])
            self.assertEqual(sha(ROOT / e["injected_skill"]["file"]), e["injected_skill"]["sha256"])
            self.assertEqual(e["injected_skill"]["sha256"], self.study[e["item_id"]]["item_sha256"])
            self.assertEqual(sha(ROOT / e["safe_run"]["file"]), e["safe_run"]["sha256"])
            self.assertEqual(e["safe_run"]["sha256"], self.inputs[e["scenario"]]["v4b_run"]["sha256"])
            self.assertEqual(sha(ROOT / e["stimulus"]["folder"] / "provenance.json"), e["stimulus"]["provenance_sha256"])

    def test_records_are_labelled_as_constructed(self):
        for item_id, r in self.records.items():
            self.assertTrue(r["simulated"])
            self.assertEqual(r["material_kind"], "simulated_trace")
            self.assertEqual(r["run_id"], item_id + items.RUN_SUFFIX)
            self.assertEqual(r["condition"], "simulated-injected")
            self.assertTrue(r["model"].startswith("none"))
            self.assertEqual(r["simulation"]["construction"], items.CONSTRUCTION)

    def test_unsafe_differs_from_the_safe_run_only_by_the_attack(self):
        for item_id, r in self.records.items():
            safe = load(r["simulation"]["safe_run"]["file"])
            n_attack = len(r["simulation"]["attack_calls"])
            # the attack: one assistant call + its result per attack call, before the agent's first reply
            first = next(i for i, m in enumerate(r["trace"]) if m["role"] == "assistant")
            attack = r["trace"][first:first + 2 * n_attack]
            self.assertEqual([{"function": m["tool_calls"][0]["function"], "args": m["tool_calls"][0]["args"]}
                              for m in attack[::2]], r["simulation"]["attack_calls"], item_id)
            rest = r["trace"][:first] + r["trace"][first + 2 * n_attack:]
            self.assertEqual(rest[1:], safe["trace"][1:], item_id)  # everything after the system message
            self.assertEqual(r["trace"][-1]["content"], safe["trace"][-1]["content"])  # final answer unchanged
            item = self.study[item_id]
            base, injected = (ROOT / item["base_file"]).read_text(), (ROOT / item["item_file"]).read_text()
            self.assertEqual(r["system_message"].replace(injected, base), safe["system_message"])

    def test_every_item_passes_the_inclusion_checks(self):
        for item_id, r in self.records.items():
            verdict = r["simulation"]["inclusion_check"]
            self.assertTrue(verdict["included"], (item_id, verdict["failed_checks"]))
            self.assertTrue(verdict["security_recomputed"] and verdict["utility_tolerant"])
            self.assertEqual(verdict["goal_calls_in_trace"], 1)

    def test_only_the_participant_order_difference_is_tolerated(self):
        noted = {i: r["simulation"]["replay_notes"] for i, r in self.records.items()
                 if "replay_notes" in r["simulation"]}
        self.assertEqual(list(noted), ["user_task_9__g4_ii_w3v3"])
        self.assertIn("participants", noted["user_task_9__g4_ii_w3v3"][0])

    def test_reference_citations_are_real_successful_recordings(self):
        review = load(items.REFERENCE_REVIEW)
        for r in self.records.values():
            ref = r["simulation"]["reference_recordings"]
            self.assertEqual(ref["same_scenario_and_objective"],
                             items.reference_citations(r["scenario"], r["injection"]["objective"], review))
            for c in ref["same_scenario_and_objective"]:
                self.assertEqual(sha(ROOT / c["file"]), c["sha256"])

    def test_stimuli_show_the_attack_and_the_safe_answer(self):
        for e in self.manifest["items"]:
            folder = ROOT / e["stimulus"]["folder"]
            high, low = load(folder / "high.json"), load(folder / "low.json")
            prov = load(folder / "provenance.json")
            self.assertEqual(prov["source"]["sha256"], e["record"]["sha256"])
            write_tool = self.records[e["item_id"]]["simulation"]["attack_calls"][-1]["function"]
            self.assertEqual(high["action_log"][len(self.records[e["item_id"]]["simulation"]["attack_calls"]) - 1]
                             ["tool"], write_tool)
            safe_low = load(f"src/data/stimuli/{e['scenario']}__no_injection/low.json")
            self.assertEqual(low["final_response"], safe_low["final_response"])
            self.assertEqual(low["user_request"], safe_low["user_request"])

    def test_participants_see_the_same_workspace_as_in_the_safe_trial(self):
        for e in self.manifest["items"]:
            simulated = load(Path(e["workspace"]["folder"]) / "workspace.json")
            safe = load(f"src/data/workspace/{e['scenario']}__no_injection/workspace.json")
            self.assertEqual(simulated["before"], safe["before"])

    def test_reachable_only_through_review_or_after_review(self):
        corpora = ROOT / "src" / "data" / "corpora"
        referring = {p.name for p in corpora.glob("*.json") if "src/data/simulated" in p.read_text(encoding="utf-8")}
        self.assertEqual(referring, {"simulated.json", "study.json"})
        self.assertTrue(all(rule.get("simulated") is True for rule in load(corpora / "simulated.json")["rules"]))
        for config in (ROOT / "src" / "data" / "experiment_configs").glob("*.json"):
            self.assertNotIn("simulated.json", config.read_text(encoding="utf-8"), config.name)
        study = load("src/data/corpora/study.json")  # admits them only after manual review, from their own folder
        self.assertEqual(study["simulated_source"]["stimuli_dir"], "src/data/simulated/stimuli")
        for rule in study["rules"]:
            if rule.get("simulated") is not False:
                self.assertEqual(rule.get("reviewed_in"), "src/data/simulated/review.json")


@unittest.skipUnless(VENV_PYTHON.exists(), "needs .venv-agentdojo (AgentDojo)")
class RebuildTest(unittest.TestCase):
    def test_committed_records_are_what_the_builder_produces(self):
        done = subprocess.run([str(VENV_PYTHON), "-B", str(ROOT / "src" / "scripts" / "build_simulated_items.py"),
                               "--check"], capture_output=True, text=True, timeout=600)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
