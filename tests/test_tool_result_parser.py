"""Tests for tool_result_parser.py: the YAML subset AgentDojo's tool results use. The decisive accuracy test compares
it with PyYAML on every recorded tool result of the 25 selected scenarios (needs the AgentDojo environment)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import tool_result_parser as trp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv-agentdojo" / "bin" / "python"


class ParseTest(unittest.TestCase):
    def test_list_of_records_with_a_nested_list(self):
        text = ("- all_day: false\n  id_: '6'\n  participants:\n  - a@b.c\n  - d@e.f\n  title: Team Sync\n"
                "- all_day: true\n  id_: '9'\n  participants: []\n  title: Lunch\n")
        self.assertEqual(trp.parse(text), [
            {"all_day": False, "id_": "6", "participants": ["a@b.c", "d@e.f"], "title": "Team Sync"},
            {"all_day": True, "id_": "9", "participants": [], "title": "Lunch"}])

    def test_single_record_and_nulls(self):
        self.assertEqual(trp.parse("location: null\nstart_time: 2024-05-15 10:00:00\nattachments: []\nshared: {}\n"),
                         {"location": None, "start_time": "2024-05-15 10:00:00", "attachments": [], "shared": {}})

    def test_plain_scalar_at_top_level(self):
        self.assertEqual(trp.parse("2024-05-15"), "2024-05-15")
        self.assertEqual(trp.parse("Email with id 34 deleted successfully."), "Email with id 34 deleted successfully.")

    def test_single_quoted_text_over_several_lines_with_blank_lines_and_escaped_quotes(self):
        # YAML folding: a line break becomes a space, and n blank lines in a row become n newlines
        text = "body: 'Hi Emma,\n\n\n    I''m planning a party\n    at six.\n\n    Best'\nid_: '1'\n"
        self.assertEqual(trp.parse(text), {"body": "Hi Emma,\n\nI'm planning a party at six.\nBest", "id_": "1"})

    def test_double_quoted_text_with_escapes_and_line_continuations(self):
        text = 'content: "Plan\\nObjective is to out\\\n  \\ line the work\\n\\\n  \\  - item"\nfilename: a.txt\n'
        self.assertEqual(trp.parse(text), {"content": "Plan\nObjective is to out line the work\n  - item",
                                           "filename": "a.txt"})

    def test_escaped_backslash_before_a_continuation(self):
        # three backslashes at the end of a line: an escaped backslash, then a line continuation
        self.assertEqual(trp.parse('content: "a\\\\\\\n  b"\n'), {"content": "a\\b"})

    def test_mapping_inside_a_mapping_and_quoted_keys(self):
        text = "shared_with:\n  a@b.c: rw\n  d@e.f: r\nid_: '13'\n"
        self.assertEqual(trp.parse(text), {"shared_with": {"a@b.c": "rw", "d@e.f": "r"}, "id_": "13"})

    def test_empty_text_is_none(self):
        self.assertIsNone(trp.parse(""))
        self.assertIsNone(trp.parse("\n\n"))

    def test_unsupported_input_is_refused(self):
        for text in ("content: |\n  block\n", "x: [1, 2]\n", "x: 'never closed\n", "x: &anchor 1\n"):
            with self.assertRaises(trp.ParseError, msg=text):
                trp.parse(text)


@unittest.skipUnless(VENV_PYTHON.exists(), "needs PyYAML from the AgentDojo environment (setup_execution_env.py)")
class AgreesWithPyYamlTest(unittest.TestCase):
    CODE = r"""
import sys, json, yaml, datetime
sys.path.insert(0, sys.argv[1] + "/src/scripts")
import tool_result_parser as trp, check_skill_map as cm, agentdojo_runs as r
def norm(v):
    if isinstance(v, dict): return {str(k): norm(x) for k, x in v.items()}
    if isinstance(v, list): return [norm(x) for x in v]
    if isinstance(v, (datetime.date, datetime.datetime)): return str(v)
    if isinstance(v, (int, float)) and not isinstance(v, bool): return str(v)
    return v
checked = bad = 0
for task in cm.scenario_ids():
    for m in r.load_run(task)["messages"]:
        if m["role"] == "tool" and not m["error"] and m["content"]:
            checked += 1
            if norm(yaml.safe_load(m["content"])) != trp.parse(m["content"]): bad += 1
print(json.dumps({"checked": checked, "bad": bad}))
"""

    def test_every_tool_result_of_the_25_scenarios_parses_like_pyyaml(self):
        done = subprocess.run([str(VENV_PYTHON), "-B", "-c", self.CODE, str(ROOT)], capture_output=True, text=True,
                              timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertGreater(result["checked"], 40)
        self.assertEqual(result["bad"], 0)


if __name__ == "__main__":
    unittest.main()
