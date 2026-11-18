"""Tests for build_stimuli.py: the low/high transparency stimuli are accurate (every call, argument and outcome of the
execution, in order), consistent (same request and response in both conditions), neutral (no label, no setup
details, no text copied from results) and deterministic. Real data only; no model call."""

import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import build_stimuli as bs  # noqa: E402
import check_skill_map as cm  # noqa: E402
import stimulus_text as stx  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# The pilot's two records, named by the original plan; later batches add more records to the same folder.
_PILOT_PLAN = json.loads((ROOT / "src" / "data" / "execution" / "plan.json").read_text(encoding="utf-8"))
PILOT = sorted(ROOT / "src" / "data" / "execution" / "runs" / f"{r['run_id']}.json" for r in _PILOT_PLAN["runs"])
SCENARIO_TRACES = [ROOT / "src" / "data" / "experiment" / t / "no_attack_execution_trace.json" for t in cm.scenario_ids()]
UNSAFE = [ROOT / c["run_path"] for c in json.loads(
    (ROOT / "src" / "data" / "gpt4o_unsafe_candidates.json").read_text(encoding="utf-8"))["candidates"]]
BUILT = {p: bs.build_stimulus(p) for p in SCENARIO_TRACES}
FORBIDDEN = re.compile(r"unauthori|malicious|suspicious|injected|injection|attack|unsafe|warning|risk", re.I)


def messages_of(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["trace"] if "record_version" in data else data["messages"]


def call(name, args, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{"function": name, "args": args, "id": call_id}]}


def result(call_id, content, name="get_current_day", error=None):
    return {"role": "tool", "content": content, "tool_call_id": call_id, "error": error,
            "tool_call": {"function": name, "args": {}, "id": call_id}}


def base(*middle):
    return [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Do it."}, *middle,
            {"role": "assistant", "content": "Done.", "tool_calls": None}]


class ConditionsTest(unittest.TestCase):
    def test_low_has_only_request_and_response_and_high_adds_the_log(self):
        for path in PILOT:
            built = bs.build_stimulus(path)
            self.assertEqual(set(built["low"]), {"stimulus_id", "transparency", "user_request", "final_response"})
            self.assertEqual(set(built["high"]), set(built["low"]) | {"action_log"})
            self.assertEqual((built["low"]["transparency"], built["high"]["transparency"]), ("low", "high"))

    def test_both_conditions_share_request_and_final_response_exactly(self):
        for path, built in BUILT.items():
            messages = messages_of(path)
            request = [m["content"] for m in messages if m["role"] == "user"][0]
            for condition in ("low", "high"):
                self.assertEqual(built[condition]["user_request"], request)
                self.assertEqual(built[condition]["final_response"], messages[-1]["content"])
            self.assertEqual(built["low"]["stimulus_id"], built["high"]["stimulus_id"])

    def test_pilot_log_matches_the_recorded_calls(self):
        built = bs.build_stimulus(PILOT[0])
        record = json.loads(PILOT[0].read_text(encoding="utf-8"))
        self.assertEqual(built["low"]["user_request"], record["task_prompt"])
        calls = runs.tool_calls({"messages": record["trace"]})
        actions = [e for e in built["high"]["action_log"] if e["type"] == "action"]
        self.assertEqual([(a["tool"]) for a in actions], [c["function"] for c in calls])


class AccuracyTest(unittest.TestCase):
    """Across all 25 selected scenarios: nothing the agent did is lost, added or reordered."""

    def test_actions_are_the_recorded_calls_in_order_with_their_arguments(self):
        for path, built in BUILT.items():
            calls = runs.tool_calls({"messages": messages_of(path)})
            actions = [e for e in built["high"]["action_log"] if e["type"] == "action"]
            self.assertEqual([a["tool"] for a in actions], [c["function"] for c in calls], path.parent.name)
            self.assertEqual([a["step"] for a in actions], list(range(1, len(calls) + 1)))
            for action, recorded in zip(actions, shown_args(calls)):
                self.assertEqual({i["name"]: i["value"] for i in action["arguments"]}, recorded)

    def test_agent_messages_and_errors_are_preserved(self):
        for path, built in BUILT.items():
            run = {"user_task_id": path.parent.name, "messages": messages_of(path)}
            log = built["high"]["action_log"]
            self.assertEqual([e["text"] for e in log if e["type"] == "agent_message"],
                             [t.strip() for t in runs.intermediate_agent_texts(run)])
            self.assertEqual(sum(e["type"] == "action" and e["outcome"]["status"] == "error" for e in log),
                             runs.failed_tool_call_count(run))

    def test_log_order_follows_the_message_order(self):
        for path, built in BUILT.items():
            messages = messages_of(path)
            indexes = [e["source_message_indexes"][0] for e in built["provenance"]["entries"]]
            self.assertEqual(indexes, sorted(indexes))
            for entry, source in zip(built["high"]["action_log"], built["provenance"]["entries"]):
                first = messages[source["source_message_indexes"][0]]
                self.assertEqual(first["role"], "assistant")
                if entry["type"] == "action":
                    self.assertIn(entry["tool"], [c["function"] for c in first["tool_calls"]])
                    self.assertEqual(messages[source["source_message_indexes"][1]]["role"], "tool")

    def test_no_summary_needed_the_generic_fallback(self):
        for path, built in BUILT.items():
            self.assertEqual(built["provenance"]["fallback_entries"], [], path.parent.name)

    def test_outcomes_name_what_the_tool_returned(self):
        built = bs.build_stimulus(PILOT[0])
        self.assertTrue(built["high"]["action_log"][0]["outcome"]["summary"].startswith("Found 3 events: 'Team Sync'"))

    def test_every_recorded_run_of_the_benchmark_can_be_built(self):
        files = sorted((ROOT / "src" / "data" / "gpt-4o-2024-05-13" / "workspace").glob("user_task_*/*/*.json"))
        self.assertGreater(len(files), 2000)
        for path in files:
            built = bs.build_stimulus(path)
            self.assertEqual(built["provenance"]["fallback_entries"], [], str(path))


def shown_args(calls):
    for c in calls:
        yield {i["name"]: i["value"] for i in stx.argument_items(c["args"])}


class NeutralityTest(unittest.TestCase):
    def test_participant_files_do_not_reveal_the_setup(self):
        for path in PILOT:
            built = bs.build_stimulus(path)
            record = json.loads(path.read_text(encoding="utf-8"))
            for condition in ("low", "high"):
                text = json.dumps(built[condition])
                for secret in (record["run_id"], record["condition"], "calendar-lookup", "gpt-4o", record["scenario"],
                               "You are an AI language model", "<skill", "run_id", "provenance"):
                    self.assertNotIn(secret, text, secret)

    def test_the_generated_wording_never_labels_behaviour(self):
        for path in SCENARIO_TRACES + UNSAFE:
            for entry in bs.build_stimulus(path)["high"]["action_log"]:
                if entry["type"] == "action":
                    self.assertIsNone(FORBIDDEN.search(entry["outcome"]["summary"]), entry)
                    self.assertEqual(set(entry["outcome"]), {"status", "summary"})
                    self.assertEqual(set(entry), {"entry", "type", "step", "action", "tool", "arguments", "outcome"})

    def test_attacked_runs_keep_the_actions_but_not_text_from_results(self):
        self.assertEqual(len(UNSAFE), 116)
        sent_to_third_party = 0
        for path in UNSAFE:
            built = bs.build_stimulus(path)
            for entry in built["high"]["action_log"]:
                if entry["type"] == "action":
                    self.assertNotIn("<INFORMATION>", entry["outcome"]["summary"])
                    if entry["tool"] == "send_email" and runs.ATTACKER_EMAIL in json.dumps(entry["arguments"]):
                        sent_to_third_party += 1
                        self.assertIn(runs.ATTACKER_EMAIL, entry["action"])  # the recipient is shown, as for any email
                        self.assertEqual(entry["outcome"]["status"], "success")
        self.assertGreater(sent_to_third_party, 0)

    def test_attack_fields_are_only_in_the_provenance(self):
        built = bs.build_stimulus(UNSAFE[0])
        self.assertEqual(built["provenance"]["source"]["attack"], "important_instructions")
        self.assertTrue(built["provenance"]["source"]["injection_task"].startswith("injection_task_"))
        for condition in ("low", "high"):
            self.assertNotIn("injection", json.dumps(built[condition]).lower().replace("injection_task", ""))

    def test_the_stimulus_id_is_opaque_and_stable(self):
        built = bs.build_stimulus(PILOT[0])
        self.assertRegex(built["id"], r"^stim_[0-9a-f]{12}$")
        self.assertEqual(built["id"], bs.stimulus_id(built["provenance"]["source"]["sha256"]))
        self.assertNotEqual(built["id"], bs.build_stimulus(PILOT[1])["id"])


class StructureTest(unittest.TestCase):
    def write(self, messages):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "trace.json"
        path.write_text(json.dumps({"messages": messages, "user_task_id": "t"}), encoding="utf-8")
        return path

    def test_parallel_calls_keep_their_order_and_pair_with_their_results(self):
        messages = base({"role": "assistant", "content": "Checking.", "tool_calls": [
            {"function": "get_current_day", "args": {}, "id": "a"},
            {"function": "search_contacts_by_name", "args": {"query": "Lily"}, "id": "b"}]},
            result("b", "- email: l@x.y\n  name: Lily\n", "search_contacts_by_name"), result("a", "2024-05-15"))
        built = bs.build_stimulus(self.write(messages))
        log = built["high"]["action_log"]
        self.assertEqual([(e["type"], e.get("tool")) for e in log],
                         [("agent_message", None), ("action", "get_current_day"), ("action", "search_contacts_by_name")])
        self.assertEqual(log[1]["outcome"]["summary"], "2024-05-15")
        self.assertEqual(log[2]["outcome"]["summary"], "Found 1 contact: Lily (l@x.y)")

    def test_a_reused_call_id_pairs_results_in_order(self):
        messages = base(call("get_current_day", {}, "x"), result("x", "2024-05-15"),
                        call("get_current_day", {}, "x"), result("x", "2024-05-16"))
        log = bs.build_stimulus(self.write(messages))["high"]["action_log"]
        self.assertEqual([e["outcome"]["summary"] for e in log], ["2024-05-15", "2024-05-16"])

    def test_a_call_without_a_result_is_shown_as_such(self):
        log = bs.build_stimulus(self.write(base(call("get_current_day", {}, "x"))))["high"]["action_log"]
        self.assertEqual(log[0]["outcome"], {"status": "no result", "summary": "No result was recorded"})

    def test_runs_that_cannot_make_a_stimulus_are_refused(self):
        cases = {
            "no final response": [{"role": "user", "content": "q"}, call("get_current_day", {}, "x")],
            "two users": base({"role": "user", "content": "again"}),
            "orphan result": base(result("zzz", "2024-05-15")),
            "empty final response": [{"role": "user", "content": "q"}, {"role": "assistant", "content": " ", "tool_calls": None}],
        }
        for label, messages in cases.items():
            with self.assertRaises(bs.StimulusError, msg=label):
                bs.build_stimulus(self.write(messages))

    def test_a_file_that_is_not_a_trace_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(bs.StimulusError):
                bs.build_stimulus(path)


class OutputTest(unittest.TestCase):
    def test_output_is_deterministic_and_complete(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            for out in (a, b):
                for path in PILOT:
                    bs.write_stimulus(bs.build_stimulus(path), Path(out))
            files = sorted(p.relative_to(a) for p in Path(a).rglob("*") if p.is_file())
            self.assertEqual(len(files), 8)
            self.assertEqual({p.name for p in files}, {"low.json", "high.json", "provenance.json", "preview.txt"})
            for rel in files:
                self.assertEqual((Path(a) / rel).read_bytes(), (Path(b) / rel).read_bytes())

    def test_provenance_records_hashes_of_the_outputs_and_the_source(self):
        with tempfile.TemporaryDirectory() as out:
            folder = bs.write_stimulus(bs.build_stimulus(PILOT[0]), Path(out))
            provenance = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance["source"]["sha256"], hashlib.sha256(PILOT[0].read_bytes()).hexdigest())
            for name, digest in provenance["outputs"].items():
                self.assertEqual(hashlib.sha256((folder / name).read_bytes()).hexdigest(), digest)
            self.assertEqual(provenance["builder_version"], bs.BUILDER_VERSION)

    def test_sources_are_never_modified(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in PILOT + SCENARIO_TRACES[:3]}
        with tempfile.TemporaryDirectory() as out:
            for path in before:
                bs.write_stimulus(bs.build_stimulus(path), Path(out))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_committed_pilot_stimuli_match_a_fresh_build(self):
        for path in PILOT:
            built = bs.build_stimulus(path)
            folder = ROOT / "src" / "data" / "stimuli" / built["id"]
            for name in ("low", "high"):
                self.assertEqual(json.loads((folder / f"{name}.json").read_text(encoding="utf-8")), built[name])

    def test_preview_shows_every_log_entry(self):
        built = bs.build_stimulus(PILOT[0])
        preview = built["preview"]
        self.assertIn("=== LOW TRANSPARENCY ===", preview)
        self.assertEqual(preview.count("ACTION LOG"), 1)  # only the high condition has one
        for entry in built["high"]["action_log"]:
            self.assertIn(entry["action"], preview)
            self.assertIn(entry["outcome"]["summary"], preview)


if __name__ == "__main__":
    unittest.main()
