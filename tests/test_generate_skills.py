"""
Purpose:
    Check generate_safe_skills.py without calling OpenAI: the generation flow (accept, repair, retry, reject,
    skip, force, attempt numbering, logging) runs against a fake client that replays prepared answers, and the
    HTTP code (headers, retries, error handling, key redaction) runs against a small local test server. The tests
    also confirm that the API key never reaches a file, that a dry run sends nothing, and that the call budget holds.

Inputs:
    The committed prompt files in src/data/skill_generation/prompts/, the context packages, and the good fixture
    tests/fixtures/skills/user_task_24_good.md (used as a model answer that passes validation).

Outputs:
    Pass/fail results from unittest. Temporary directories are used for every file the script writes.

Dependencies:
    src/scripts/generate_safe_skills.py, validate_skills.py, build_skill_prompts.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import build_skill_prompts as prompts  # noqa: E402
import generate_safe_skills as gen  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "skills"
GOOD_24 = (FIXTURES / "user_task_24_good.md").read_text(encoding="utf-8")
TASK = "user_task_24"
SECRET = "sk-test-SECRET-1234567890"


def api_response(text: str | None, status: str = "completed", model: str = "gpt-6.1-sol-test") -> dict:
    """A reply shaped like a Responses API reply."""
    message = [{"type": "message", "content": [{"type": "output_text", "text": text}]}] if text is not None else []
    return {
        "id": "resp_test", "model": model, "status": status, "created_at": 1,
        "output": [{"type": "reasoning", "summary": []}, *message],
        "usage": {"input_tokens": 100, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 20}},
    }


class FakeClient:
    """Replays the given answers in order and records every request it receives."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, request: dict) -> dict:
        self.requests.append(request)
        if not self.answers:
            raise AssertionError("the generator made more calls than expected")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, dict) else api_response(answer)


class Workspace:
    """Temporary experiment and runs folders for one test."""

    def __init__(self, skill_text: str = ""):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.experiment = root / "experiment"
        self.runs = root / "runs"
        (self.experiment / TASK).mkdir(parents=True)
        self.skill = self.experiment / TASK / "SKILL_safe.md"
        self.skill.write_text(skill_text, encoding="utf-8")

    def run(self, client, **options):
        return gen.run_task(TASK, client, prompts.DEFAULT_OUT_DIR, self.experiment, self.runs, **options)

    def attempts(self) -> list[dict]:
        files = sorted((self.runs / TASK).glob("attempt_*.json"), key=lambda p: int(p.stem.split("_")[1]))
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def cleanup(self):
        self.tmp.cleanup()


class HelperTest(unittest.TestCase):
    def test_response_text_joins_output_text_parts_and_ignores_reasoning(self):
        response = {"output": [
            {"type": "reasoning", "summary": [{"text": "hidden"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "A"}, {"type": "output_text", "text": "B"}]},
        ]}
        self.assertEqual(gen.response_text(response), "AB")
        self.assertEqual(gen.response_text({"output": []}), "")

    def test_clean_output_strips_whitespace_and_one_surrounding_fence(self):
        self.assertEqual(gen.clean_output("  hi\n\n"), ("hi\n", []))
        text, notes = gen.clean_output("```markdown\n---\nname: a\n---\nbody\n```")
        self.assertEqual(text, "---\nname: a\n---\nbody\n")
        self.assertEqual(len(notes), 1)
        self.assertEqual(gen.clean_output("   "), ("", []))

    def test_clean_output_keeps_fences_inside_the_text(self):
        inside = "---\nname: a\n---\ntext\n```\ncode\n```\nmore"
        self.assertEqual(gen.clean_output(inside), (inside + "\n", []))

    def test_redact_hides_the_key(self):
        self.assertNotIn(SECRET, gen.redact(f"bad key {SECRET}", SECRET))
        self.assertEqual(gen.redact("nothing", None), "nothing")

    def test_repair_request_keeps_settings_and_appends_attempt_and_problems(self):
        base = gen.load_prompt(prompts.DEFAULT_OUT_DIR, TASK)["request"]
        repaired = gen.repair_request(base, "bad skill text", ["S1 (frontmatter): missing", "A2 (links): URL"])
        self.assertEqual({k: v for k, v in repaired.items() if k != "input"}, {k: v for k, v in base.items() if k != "input"})
        self.assertTrue(repaired["input"].startswith(base["input"]))
        for expected in ("## Previous attempt", "bad skill text", "- S1 (frontmatter): missing", "- A2 (links): URL"):
            self.assertIn(expected, repaired["input"])

    def test_edited_prompt_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = json.loads((prompts.DEFAULT_OUT_DIR / f"{TASK}.json").read_text(encoding="utf-8"))
            record["request"]["input"] += " tampered"
            (Path(tmp) / f"{TASK}.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(gen.GenerationError):
                gen.load_prompt(Path(tmp), TASK)


class GenerationFlowTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def test_a_valid_first_answer_is_accepted_and_saved(self):
        client = FakeClient(GOOD_24)
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"], outcome["accepted_attempt"]), ("accepted", 1, 1))
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD_24)
        self.assertEqual(len(client.requests), 1)
        saved = gen.load_prompt(prompts.DEFAULT_OUT_DIR, TASK)
        self.assertEqual(client.requests[0], saved["request"])  # sent exactly as saved by 6a

    def test_the_attempt_log_records_everything_needed_to_trace_the_skill(self):
        self.ws.run(FakeClient(GOOD_24))
        (log,) = self.ws.attempts()
        self.assertEqual(log["kind"], "initial")
        self.assertTrue(log["accepted"])
        self.assertEqual(log["reported_model"], "gpt-6.1-sol-test")
        self.assertEqual(log["usage"], {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20})
        self.assertEqual(log["skill_text"], GOOD_24)
        self.assertEqual(log["output_text"], GOOD_24)
        self.assertTrue(log["validation"]["passed"])
        self.assertEqual(log["request_sha256"], prompts.canonical_hash(log["request"]))
        self.assertEqual(log["base_prompt_sha256"], log["request_sha256"])
        self.assertEqual(log["response"]["id"], "resp_test")
        self.assertEqual(
            set(log), {"user_task_id", "attempt", "kind", "template_version", "base_prompt_sha256", "request_sha256",
                       "requested_at", "request", "response", "response_status", "reported_model", "usage",
                       "output_text", "skill_text", "normalisation", "validation", "accepted"})

    def test_a_failing_answer_triggers_a_repair_with_the_problems_attached(self):
        client = FakeClient("this is not a skill", GOOD_24)
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"], outcome["accepted_attempt"]), ("accepted", 2, 2))
        first, second = self.ws.attempts()
        self.assertEqual((first["kind"], second["kind"]), ("initial", "repair"))
        self.assertFalse(first["accepted"])
        self.assertIn("this is not a skill", client.requests[1]["input"])
        self.assertIn("S1 (frontmatter parses", client.requests[1]["input"])
        self.assertEqual(second["base_prompt_sha256"], first["base_prompt_sha256"])
        self.assertNotEqual(second["request_sha256"], first["request_sha256"])
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD_24)

    def test_repairs_stop_after_two_and_the_skill_file_stays_empty(self):
        client = FakeClient("bad one", "bad two", "bad three")
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"]), ("rejected", 3))
        self.assertTrue(outcome["errors"])
        self.assertEqual(len(self.ws.attempts()), 3)
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), "")
        self.assertFalse(any(a["accepted"] for a in self.ws.attempts()))

    def test_max_repairs_zero_means_a_single_call(self):
        client = FakeClient("bad one")
        outcome = self.ws.run(client, max_repairs=0)
        self.assertEqual((outcome["status"], outcome["attempts"]), ("rejected", 1))

    def test_an_answer_with_no_text_is_retried_with_the_original_request(self):
        client = FakeClient(api_response(None, status="incomplete"), GOOD_24)
        outcome = self.ws.run(client)
        self.assertEqual(outcome["status"], "accepted")
        first, second = self.ws.attempts()
        self.assertEqual(first["response_status"], "incomplete")
        self.assertIsNone(first["validation"])
        self.assertEqual(second["kind"], "retry")
        self.assertEqual(client.requests[1], client.requests[0])

    def test_a_wrapping_code_fence_is_removed_and_noted(self):
        client = FakeClient("```markdown\n" + GOOD_24 + "```")
        self.assertEqual(self.ws.run(client)["status"], "accepted")
        (log,) = self.ws.attempts()
        self.assertEqual(len(log["normalisation"]), 1)
        self.assertNotEqual(log["output_text"], log["skill_text"])
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD_24)

    def test_a_finished_skill_is_skipped_without_any_call(self):
        self.ws.skill.write_text("existing work\n", encoding="utf-8")
        client = FakeClient()
        outcome = self.ws.run(client)
        self.assertEqual(outcome["status"], "skipped")
        self.assertEqual(client.requests, [])
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), "existing work\n")
        self.assertFalse((self.ws.runs / TASK).exists())

    def test_force_regenerates_and_attempt_files_are_never_overwritten(self):
        self.ws.run(FakeClient(GOOD_24))
        first_log = (self.ws.runs / TASK / "attempt_1.json").read_bytes()
        outcome = self.ws.run(FakeClient(GOOD_24), force=True)
        self.assertEqual(outcome["accepted_attempt"], 2)
        self.assertEqual((self.ws.runs / TASK / "attempt_1.json").read_bytes(), first_log)
        self.assertEqual(len(self.ws.attempts()), 2)

    def test_missing_experiment_folder_is_an_error(self):
        with self.assertRaises(gen.GenerationError):
            gen.run_task("user_task_0", FakeClient(), prompts.DEFAULT_OUT_DIR, self.ws.experiment, self.ws.runs)

    def test_summary_is_rebuilt_from_the_logs(self):
        self.ws.run(FakeClient("bad", GOOD_24))
        (row,) = gen.summarize(self.ws.runs)["tasks"]
        self.assertEqual((row["user_task_id"], row["status"], row["attempts"], row["accepted_attempt"]),
                         (TASK, "accepted", 2, 2))
        self.assertEqual(row["reported_models"], ["gpt-6.1-sol-test"])
        self.assertEqual(row["usage_total"], {"input_tokens": 200, "output_tokens": 100, "reasoning_tokens": 40})
        self.assertEqual(row["last_attempt_errors"], [])

    def test_summary_lists_the_errors_of_a_rejected_task(self):
        self.ws.run(FakeClient("bad", "bad", "bad"))
        (row,) = gen.summarize(self.ws.runs)["tasks"]
        self.assertEqual((row["status"], row["accepted_attempt"]), ("rejected", None))
        self.assertTrue(any(line.startswith("S1") for line in row["last_attempt_errors"]))

    def test_the_call_budget_stops_a_run(self):
        calls = []
        original = gen.post_json
        gen.post_json = lambda url, request, key: calls.append(request) or api_response("bad")
        try:
            client = gen.make_client("key", max_calls=2)
            client({"a": 1})
            client({"a": 2})
            with self.assertRaises(gen.GenerationError):
                client({"a": 3})
        finally:
            gen.post_json = original
        self.assertEqual(len(calls), 2)


class _Handler(BaseHTTPRequestHandler):
    replies: list = []  # (status, body) in order
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                "type": self.headers.get("Content-Type"), "body": json.loads(self.rfile.read(length))})
        status, body = type(self).replies.pop(0)
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class LocalServer(HTTPServer):
    """HTTPServer without its reverse-DNS lookup of the host name, which can stall for many seconds."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.socket.getsockname()[1]


class TransportTest(unittest.TestCase):
    def setUp(self):
        _Handler.replies, _Handler.seen = [], []
        self.server = LocalServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/responses"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.pauses = []

    def post(self, body, retries=3):
        return gen.post_json(self.url, body, SECRET, timeout=5, retries=retries, sleep=self.pauses.append)

    def test_sends_json_with_bearer_header_and_returns_the_parsed_reply(self):
        _Handler.replies = [(200, {"ok": True})]
        self.assertEqual(self.post({"input": "héllo"}), {"ok": True})
        (seen,) = _Handler.seen
        self.assertEqual(seen["auth"], f"Bearer {SECRET}")
        self.assertEqual(seen["type"], "application/json")
        self.assertEqual(seen["body"], {"input": "héllo"})
        self.assertEqual(self.pauses, [])

    def test_retryable_errors_are_retried_with_increasing_pauses(self):
        _Handler.replies = [(500, {"error": "x"}), (429, {"error": "y"}), (200, {"ok": 1})]
        self.assertEqual(self.post({}), {"ok": 1})
        self.assertEqual(len(_Handler.seen), 3)
        self.assertEqual(self.pauses, [2, 4])

    def test_gives_up_after_the_retries_are_used(self):
        _Handler.replies = [(503, {"error": "down"})] * 3
        with self.assertRaises(gen.GenerationError) as caught:
            self.post({}, retries=2)
        self.assertEqual(len(_Handler.seen), 3)
        self.assertIn("giving up after 3 tries", str(caught.exception))

    def test_other_http_errors_are_not_retried_and_never_show_the_key(self):
        _Handler.replies = [(401, {"error": {"message": f"Incorrect API key provided: {SECRET}"}})]
        with self.assertRaises(gen.GenerationError) as caught:
            self.post({})
        self.assertEqual(len(_Handler.seen), 1)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.pauses, [])

    def test_a_refused_connection_is_retried_then_reported(self):
        with socket.socket() as probe:  # a port nothing listens on
            probe.bind(("127.0.0.1", 0))
            dead_url = f"http://127.0.0.1:{probe.getsockname()[1]}/v1/responses"
        with self.assertRaises(gen.GenerationError) as caught:
            gen.post_json(dead_url, {}, SECRET, timeout=5, retries=1, sleep=self.pauses.append)
        self.assertIn("network error", str(caught.exception))
        self.assertEqual(self.pauses, [2])


class CommandLineTest(unittest.TestCase):
    SCRIPT = str(REPO_ROOT / "src" / "scripts" / "generate_safe_skills.py")

    def run_cli(self, *args, key=None):
        env = {"PATH": os.environ.get("PATH", "")}
        if key:
            env["OPENAI_API_KEY"] = key
        return subprocess.run([sys.executable, "-B", self.SCRIPT, *args], capture_output=True, text=True, env=env)

    def test_dry_run_needs_no_key_sends_nothing_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            done = self.run_cli("--dry-run", "--tasks", "user_task_1", "user_task_24", "--runs-dir", str(runs_dir))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("user_task_24: model gpt-6.1-sol", done.stdout)
            self.assertIn("Dry run: nothing was sent.", done.stdout)
            self.assertFalse(runs_dir.exists())

    def test_dry_run_reports_that_a_finished_skill_would_be_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / TASK
            folder.mkdir()
            (folder / "SKILL_safe.md").write_text("done\n", encoding="utf-8")
            done = self.run_cli("--dry-run", "--tasks", TASK, "--experiment-dir", tmp)
            self.assertIn("skip", done.stdout)
            forced = self.run_cli("--dry-run", "--force", "--tasks", TASK, "--experiment-dir", tmp)
            self.assertIn("would write SKILL_safe.md", forced.stdout)

    def test_a_real_run_without_a_key_stops_before_doing_anything(self):
        done = self.run_cli("--tasks", TASK)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("OPENAI_API_KEY", done.stderr)

    def test_check_access_without_a_key_stops(self):
        done = self.run_cli("--check-access")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("OPENAI_API_KEY", done.stderr)


class SafetyOfTheScriptTest(unittest.TestCase):
    def test_the_key_is_only_read_from_the_environment(self):
        text = Path(gen.__file__).read_text(encoding="utf-8")
        self.assertIn('os.environ.get("OPENAI_API_KEY"', text)
        self.assertNotRegex(text, r"sk-[A-Za-z0-9_-]{16,}")

    def test_the_key_does_not_appear_in_any_log_written_by_a_run(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeClient("bad", GOOD_24))
        for path in ws.runs.rglob("*.json"):
            self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))

    def test_the_request_log_has_no_headers_only_the_body(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeClient(GOOD_24))
        (log,) = ws.attempts()
        self.assertEqual(set(log["request"]), {"model", "instructions", "input", "reasoning", "max_output_tokens"})

    def test_the_model_and_settings_are_the_ones_chosen_for_the_study(self):
        record = gen.load_prompt(prompts.DEFAULT_OUT_DIR, TASK)
        self.assertEqual(record["request"]["model"], "gpt-6.1-sol")
        self.assertEqual(record["request"]["reasoning"], {"effort": "high"})
        self.assertEqual(gen.MAX_REPAIRS, 2)
        self.assertEqual(gen.TRANSPORT_RETRIES, 3)
        self.assertEqual(gen.TIMEOUT_SECONDS, 300)


if __name__ == "__main__":
    unittest.main()
