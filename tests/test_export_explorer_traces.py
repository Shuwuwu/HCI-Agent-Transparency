"""Tests of export_explorer_traces.py: Explorer-schema validity, fidelity to the source, annotations, determinism."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import build_stimuli as bs  # noqa: E402
import export_explorer_traces as ex  # noqa: E402

# Two active (v4b) runs, one of them with failed tool calls, and one attacked recorded run. The stimuli are built into
# a temporary folder (the attacked one is not an active stimulus); a separate test checks the committed ones.
ACTIVE = [ROOT / "src/data/execution/runs/user_task_1__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json",
          ROOT / "src/data/execution/runs/user_task_13__mapped-skills__gpt-4o-2024-08-06__r1__skills-v4b.json"]
ATTACKED = ROOT / "src/data/gpt-4o-2024-05-13/workspace/user_task_1/important_instructions/injection_task_0.json"
SOURCES = ACTIVE + [ATTACKED]


def explorer_message_problems(message: dict) -> list[str]:
    """Python version of the checks in Explorer's app-ui/src/lib/traceview/schema.ts."""
    problems = []
    if not isinstance(message.get("role"), str):
        problems.append("role must be a string")
    if not isinstance(message.get("content"), (str, list, type(None))):
        problems.append("content must be a string, list or null")
    if not isinstance(message.get("tool_call_id", None), (str, int, type(None))):
        problems.append("tool_call_id must be a string, number or null")
    calls = message.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list):
            problems.append("tool_calls must be a list")
        for call in calls or []:
            if not (isinstance(call.get("id"), str) and call.get("type") == "function"
                    and isinstance(call.get("function"), dict)
                    and isinstance(call["function"].get("name"), str) and "arguments" in call["function"]):
                problems.append(f"bad tool call {call}")
    return problems


def resolve(address: str, messages: list[dict]) -> str:
    """Follow an annotation address such as messages.2.tool_calls.0.function.name:0-4 to its text, checking the range."""
    path, _, span = address.rpartition(":")
    start, end = map(int, span.split("-"))
    node = {"messages": messages}
    for part in path.split("."):
        node = node[int(part)] if isinstance(node, list) else node[part]
    assert isinstance(node, str), address
    assert 0 <= start <= end <= len(node), address
    return node[start:end]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.stimuli_dir = Path(cls._tmp.name)
        for p in SOURCES:
            bs.write_stimulus(bs.build_stimulus(p), cls.stimuli_dir)
        cls.built = {p.name: ex.build_trace(p, cls.stimuli_dir) for p in SOURCES}

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_messages_satisfy_explorer_schema(self):
        for name, built in self.built.items():
            for message in built["line"]["messages"]:
                self.assertEqual(explorer_message_problems(message), [], name)

    def test_order_roles_contents_and_arguments_are_unchanged(self):
        for name, built in self.built.items():
            original, exported = built["loaded"]["messages"], built["line"]["messages"]
            self.assertEqual([m["role"] for m in original], [m["role"] for m in exported], name)
            for before, after in zip(original, exported):
                self.assertEqual(before.get("content"), after.get("content"), name)
                calls = [(c["id"], c["function"], c["args"]) for c in before.get("tool_calls") or []]
                shown = [(c["id"], c["function"]["name"], c["function"]["arguments"])
                         for c in after.get("tool_calls") or []]
                self.assertEqual(calls, shown, name)
                if before["role"] == "tool":
                    self.assertEqual(before["tool_call_id"], after["tool_call_id"])
                    self.assertEqual(before["error"], after.get("error"))

    def test_tool_name_matches_the_answered_call(self):
        for built in self.built.values():
            names = {}
            for m in built["line"]["messages"]:
                for c in m.get("tool_calls") or []:
                    names[c["id"]] = c["function"]["name"]
                if m["role"] == "tool":
                    self.assertEqual(m["tool_name"], names[m["tool_call_id"]])

    def test_every_annotation_address_resolves_to_a_string_range(self):
        for name, built in self.built.items():
            for annotation in built["line"]["annotations"]:
                self.assertTrue(resolve(annotation["address"], built["line"]["messages"]) is not None, name)

    def test_annotations_cover_each_log_entry_on_its_source_messages(self):
        for name, built in self.built.items():
            texts = {a["address"]: a["content"] for a in built["line"]["annotations"]}
            for entry in built["high"]["action_log"]:
                marker = f"Stimulus log entry {entry['entry']}"
                self.assertGreaterEqual(sum(marker in t for t in texts.values()), 1, name)
                if entry["type"] == "action":
                    # the call annotation sits on the function name and quotes the tool's displayed arguments
                    call_text = [t for a, t in texts.items() if a.count("function.name") and marker in t][0]
                    self.assertIn(entry["action"], call_text)
                    for argument in entry["arguments"]:
                        self.assertIn(f"{argument['name']}: {argument['value']}", call_text)
                    result_text = [t for a, t in texts.items() if marker in t and "outcome" in t][0]
                    self.assertIn(entry["outcome"]["summary"], result_text)

    def test_function_name_annotation_selects_the_called_tool(self):
        for built in self.built.values():
            for annotation in built["line"]["annotations"]:
                if "function.name" in annotation["address"]:
                    selected = resolve(annotation["address"], built["line"]["messages"])
                    self.assertIn(selected, {e["tool"] for e in built["high"]["action_log"] if e["type"] == "action"})

    def test_attacked_trace_is_a_confirmed_successful_attack(self):
        raw = json.loads(ATTACKED.read_text(encoding="utf-8"))
        self.assertIs(raw["security"], True)
        self.assertIsNone(raw["error"])
        meta = self.built[ATTACKED.name]["line"]["metadata"]
        self.assertIs(meta["security"], True)
        self.assertEqual(meta["injection_goal"], runs.INJECTION_GOALS["injection_task_0"])

    def test_injected_action_is_visible_in_the_high_log_and_annotated(self):
        built = self.built[ATTACKED.name]
        sends = [e for e in built["high"]["action_log"] if e["type"] == "action" and e["tool"] == "send_email"]
        self.assertEqual(len(sends), 1)
        self.assertIn(runs.ATTACKER_EMAIL, [a["value"] for a in sends[0]["arguments"] if a["name"] == "recipients"])
        self.assertFalse(any(a["truncated"] for a in sends[0]["arguments"]))
        self.assertTrue(any(f"log entry {sends[0]['entry']}" in a["content"] for a in built["line"]["annotations"]))

    def test_active_stimuli_are_the_committed_ones(self):
        for path in ACTIVE:
            built = self.built[path.name]
            # also proves the exporter finds the committed stimulus by its user_task_<N>__no_injection folder name
            committed = ex.load_stimulus(path, bs.DEFAULT_OUT_DIR)[1]
            self.assertEqual(built["high"], committed)
            self.assertTrue((bs.DEFAULT_OUT_DIR / f"{built['provenance']['source']['scenario']}__no_injection").is_dir())

    def test_other_run_of_the_same_task_is_not_mistaken_for_the_stimulus(self):
        pilot = ROOT / "src/data/execution/runs/user_task_1__no-skills__gpt-4o-2024-08-06__r1.json"
        with self.assertRaises(ex.ExportError) as caught:
            ex.load_stimulus(pilot, bs.DEFAULT_OUT_DIR)  # same folder name as the active user_task_1 stimulus
        self.assertIn("different execution", str(caught.exception))

    def test_jsonl_format_matches_explorer_import_rules(self):
        text = ex.render_jsonl(list(self.built.values()))
        lines = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(set(lines[0]), {"metadata"})  # the optional first line: metadata, no messages
        for line in lines[1:]:
            self.assertIn("messages", line)
            self.assertNotIn("index", line)  # indexes must be all present or all absent; we give none
            self.assertTrue(all({"address", "content"} <= set(a) for a in line["annotations"]))

    def test_output_is_deterministic_and_sources_are_untouched(self):
        before = {p: sha(p) for p in SOURCES}
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            ex.export(SOURCES, self.stimuli_dir, Path(one))
            ex.export(SOURCES, self.stimuli_dir, Path(two))
            for name in (ex.DATASET_FILE, ex.REPORT_FILE):
                self.assertEqual((Path(one) / name).read_bytes(), (Path(two) / name).read_bytes())
        self.assertEqual(before, {p: sha(p) for p in SOURCES})

    def test_missing_stimulus_stops_with_the_build_command(self):
        with tempfile.TemporaryDirectory() as empty, self.assertRaises(ex.ExportError) as caught:
            ex.build_trace(ATTACKED, Path(empty))
        self.assertIn("build_stimuli.py", str(caught.exception))

    def test_stale_stimulus_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = bs.build_stimulus(ATTACKED)
            built["provenance"]["source"]["sha256"] = "0" * 64
            folder = Path(tmp) / built["folder"]
            folder.mkdir()
            (folder / "high.json").write_text(json.dumps(built["high"]), encoding="utf-8")
            (folder / "provenance.json").write_text(json.dumps(built["provenance"]), encoding="utf-8")
            with self.assertRaises(ex.ExportError):
                ex.build_trace(ATTACKED, Path(tmp))

    def test_conversion_rejects_a_tool_call_echo_that_differs(self):
        messages = [{"role": "assistant", "content": None,
                     "tool_calls": [{"function": "get_current_day", "args": {}, "id": "a"}]},
                    {"role": "tool", "content": "x", "tool_call_id": "a", "error": None,
                     "tool_call": {"function": "other", "args": {}, "id": "a"}}]
        with self.assertRaises(ex.ExportError):
            ex.convert_messages(messages)

    def test_conversion_pairs_reused_ids_in_order(self):
        calls = [{"function": "get_current_day", "args": {}, "id": "a"},
                 {"function": "get_unread_emails", "args": {}, "id": "a"}]
        messages = [{"role": "assistant", "content": None, "tool_calls": calls},
                    {"role": "tool", "content": "1", "tool_call_id": "a", "error": None},
                    {"role": "tool", "content": "2", "tool_call_id": "a", "error": "boom"}]
        converted = ex.convert_messages(messages)
        self.assertEqual([m["tool_name"] for m in converted[1:]], ["get_current_day", "get_unread_emails"])
        self.assertNotIn("error", converted[1])
        self.assertEqual(converted[2]["error"], "boom")

    def test_report_names_the_injected_action_and_the_hidden_result_text(self):
        report = ex.build_report(list(self.built.values()))
        self.assertIn("contains the text of the injection goal", report)
        self.assertIn("1 visible in the high-transparency log", report)
        self.assertIn("mentions the attacker's address: no", report)


if __name__ == "__main__":
    unittest.main()
