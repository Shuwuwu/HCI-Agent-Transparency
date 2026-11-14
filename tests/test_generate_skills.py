"""
Purpose:
    Check generate_skills.py without calling OpenAI: the generation flow (accept, repair, retry, reject, skip,
    force, attempt numbering, logging, manifest) runs against a fake client that replays prepared answers, and the
    HTTP code (headers, retries, error handling, key redaction) runs against a small local test server. The tests
    also confirm that the API key never reaches a file, that repair feedback never names a scenario, that a dry
    run sends nothing, and that the call budget holds.

Inputs:
    The committed prompt files in src/data/skill_library/prompts/, the skill library used by the validator, and the
    good fixture tests/fixtures/capability_skills/calendar-lookup_good.md (used as a model answer that passes).

Outputs:
    Pass/fail results from unittest. Temporary directories are used for every file the script writes.

Dependencies:
    src/scripts/generate_skills.py, validate_capability_skills.py, build_capability_prompts.py, prompt_utils.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import io
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

import build_capability_prompts as cp  # noqa: E402
import generate_skills as gen  # noqa: E402
import prompt_utils  # noqa: E402
import validate_capability_skills as validator  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "capability_skills"
GOOD = (FIXTURES / "calendar-lookup_good.md").read_text(encoding="utf-8")
SKILL = "calendar-lookup"
SECRET = "sk-test-SECRET-1234567890"
LIBRARY = validator.load_library()


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
    """Temporary skills and runs folders for one test."""

    def __init__(self, skill_text: str = ""):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.skills = root / "skills"
        self.runs = root / "runs"
        self.skill = self.skills / SKILL / "SKILL.md"
        if skill_text:
            self.skill.parent.mkdir(parents=True)
            self.skill.write_text(skill_text, encoding="utf-8")

    def run(self, client, skill_id=SKILL, **options):
        return gen.run_skill(skill_id, client, LIBRARY, cp.DEFAULT_OUT_DIR, self.skills, self.runs, **options)

    def attempts(self, skill_id=SKILL) -> list[dict]:
        return gen.load_attempts(self.runs / skill_id)

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
        self.assertEqual(gen.redact("a k b", "k"), "a k b")  # too short to be a real key

    def test_repair_request_keeps_settings_and_appends_attempt_and_problems(self):
        base = gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)["request"]
        repaired = gen.repair_request(base, "bad skill text", ["S1 (frontmatter): missing", "A2 (links): URL"])
        self.assertEqual({k: v for k, v in repaired.items() if k != "input"}, {k: v for k, v in base.items() if k != "input"})
        self.assertTrue(repaired["input"].startswith(base["input"]))
        for expected in ("## Previous attempt", "bad skill text", "- S1 (frontmatter): missing", "- A2 (links): URL"):
            self.assertIn(expected, repaired["input"])

    def test_edited_prompt_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = json.loads((cp.DEFAULT_OUT_DIR / f"{SKILL}.json").read_text(encoding="utf-8"))
            record["request"]["input"] += " tampered"
            (Path(tmp) / f"{SKILL}.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaises(gen.GenerationError):
                gen.load_prompt(Path(tmp), SKILL)

    def test_next_attempt_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(gen.next_attempt_number(folder), 1)
            (folder / "attempt_1.json").write_text("{}", encoding="utf-8")
            (folder / "attempt_10.json").write_text("{}", encoding="utf-8")
            self.assertEqual(gen.next_attempt_number(folder), 11)


class GenerationFlowTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def test_a_valid_first_answer_is_accepted_and_saved(self):
        client = FakeClient(GOOD)
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"], outcome["accepted_attempt"]), ("accepted", 1, 1))
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD)
        self.assertEqual(client.requests, [gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)["request"]])  # sent as saved

    def test_the_attempt_log_records_everything_needed_to_trace_the_skill(self):
        self.ws.run(FakeClient(GOOD))
        (log,) = self.ws.attempts()
        record = gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)
        self.assertEqual((log["kind"], log["accepted"], log["reported_model"]), ("initial", True, "gpt-6.1-sol-test"))
        self.assertEqual(log["usage"], {"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 20})
        self.assertEqual((log["skill_text"], log["output_text"]), (GOOD, GOOD))
        self.assertTrue(log["validation"]["passed"])
        self.assertEqual(log["validator_version"], validator.VALIDATOR_VERSION)
        self.assertEqual(log["definition_sha256"], record["source"]["definition_sha256"])
        self.assertEqual(log["base_prompt_sha256"], record["prompt_sha256"])
        self.assertEqual(log["request_sha256"], prompt_utils.canonical_hash(log["request"]))
        self.assertEqual(log["response"]["id"], "resp_test")
        self.assertEqual(
            set(log), {"skill_id", "attempt", "kind", "template_version", "base_prompt_sha256", "definition_sha256",
                       "validator_version", "request_sha256", "requested_at", "request", "response", "response_status",
                       "reported_model", "usage", "output_text", "skill_text", "normalisation", "validation",
                       "accepted"})

    def test_a_failing_answer_triggers_a_repair_with_the_problems_attached(self):
        client = FakeClient("this is not a skill", GOOD)
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"], outcome["accepted_attempt"]), ("accepted", 2, 2))
        first, second = self.ws.attempts()
        self.assertEqual((first["kind"], second["kind"]), ("initial", "repair"))
        self.assertFalse(first["accepted"])
        self.assertIn("this is not a skill", client.requests[1]["input"])
        self.assertIn("S1 (frontmatter parses", client.requests[1]["input"])
        self.assertEqual(second["base_prompt_sha256"], first["base_prompt_sha256"])
        self.assertNotEqual(second["request_sha256"], first["request_sha256"])
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD)

    def test_repair_feedback_never_names_a_scenario(self):
        leaky = GOOD.replace("## Common Pitfalls\n", "## Common Pitfalls\n- Please show me my unread emails.\n"
                             "- Do not confuse it with the Networking event.\n", 1)
        client = FakeClient(leaky, GOOD)
        self.ws.run(client)
        feedback = client.requests[1]["input"].split("## Problems found by automatic checks")[1]
        self.assertIn("G2", feedback)
        self.assertIn("G1", feedback)
        for word in ("user_task", "scenario", "Scenario"):
            self.assertNotIn(word, feedback)

    def test_repairs_stop_after_two_and_no_skill_file_is_written(self):
        client = FakeClient("bad one", "bad two", "bad three")
        outcome = self.ws.run(client)
        self.assertEqual((outcome["status"], outcome["attempts"]), ("rejected", 3))
        self.assertTrue(outcome["errors"])
        self.assertEqual(len(self.ws.attempts()), 3)
        self.assertFalse(self.ws.skill.exists())
        self.assertFalse(any(a["accepted"] for a in self.ws.attempts()))

    def test_max_repairs_zero_means_a_single_call(self):
        outcome = self.ws.run(FakeClient("bad one"), max_repairs=0)
        self.assertEqual((outcome["status"], outcome["attempts"]), ("rejected", 1))

    def test_an_answer_with_no_text_is_retried_with_the_original_request(self):
        client = FakeClient(api_response(None, status="incomplete"), GOOD)
        self.assertEqual(self.ws.run(client)["status"], "accepted")
        first, second = self.ws.attempts()
        self.assertEqual(first["response_status"], "incomplete")
        self.assertIsNone(first["validation"])
        self.assertEqual(second["kind"], "retry")
        self.assertEqual(client.requests[1], client.requests[0])

    def test_a_wrapping_code_fence_is_removed_and_noted(self):
        self.assertEqual(self.ws.run(FakeClient("```markdown\n" + GOOD + "```"))["status"], "accepted")
        (log,) = self.ws.attempts()
        self.assertEqual(len(log["normalisation"]), 1)
        self.assertNotEqual(log["output_text"], log["skill_text"])
        self.assertEqual(self.ws.skill.read_text(encoding="utf-8"), GOOD)

    def test_a_finished_skill_is_skipped_without_any_call(self):
        ws = Workspace("existing work\n")
        self.addCleanup(ws.cleanup)
        client = FakeClient()
        self.assertEqual(ws.run(client)["status"], "skipped")
        self.assertEqual(client.requests, [])
        self.assertEqual(ws.skill.read_text(encoding="utf-8"), "existing work\n")
        self.assertFalse((ws.runs / SKILL).exists())

    def test_force_regenerates_and_attempt_files_are_never_overwritten(self):
        self.ws.run(FakeClient(GOOD))
        first_log = (self.ws.runs / SKILL / "attempt_1.json").read_bytes()
        outcome = self.ws.run(FakeClient(GOOD), force=True)
        self.assertEqual(outcome["accepted_attempt"], 2)
        self.assertEqual((self.ws.runs / SKILL / "attempt_1.json").read_bytes(), first_log)
        self.assertEqual(len(self.ws.attempts()), 2)

    def test_every_skill_has_a_prompt_file(self):
        self.assertEqual(sorted(p.stem for p in cp.DEFAULT_OUT_DIR.glob("*.json")), sorted(LIBRARY.packages))

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


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.cleanup)

    def test_summary_is_rebuilt_from_the_logs(self):
        self.ws.run(FakeClient("bad", GOOD))
        (row,) = gen.summarize(self.ws.runs)["skills"]
        self.assertEqual((row["skill_id"], row["status"], row["attempts"], row["accepted_attempt"]),
                         (SKILL, "accepted", 2, 2))
        self.assertEqual(row["reported_models"], ["gpt-6.1-sol-test"])
        self.assertEqual(row["usage_total"], {"input_tokens": 200, "output_tokens": 100, "reasoning_tokens": 40})
        self.assertEqual(row["last_attempt_errors"], [])

    def test_summary_lists_the_errors_of_a_rejected_skill(self):
        self.ws.run(FakeClient("bad", "bad", "bad"))
        (row,) = gen.summarize(self.ws.runs)["skills"]
        self.assertEqual((row["status"], row["accepted_attempt"]), ("rejected", None))
        self.assertTrue(any(line.startswith("S1") for line in row["last_attempt_errors"]))

    def test_manifest_ties_the_file_to_its_attempt(self):
        self.ws.run(FakeClient("bad", GOOD))
        manifest = gen.build_manifest(self.ws.skills, self.ws.runs)
        (row,) = manifest["skills"]
        record = gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)
        self.assertEqual(row["skill_id"], SKILL)
        self.assertEqual(row["skill_sha256"], hashlib.sha256(GOOD.encode("utf-8")).hexdigest())
        self.assertEqual((row["accepted_attempt"], row["reported_model"]), (2, "gpt-6.1-sol-test"))
        self.assertEqual((row["prompt_sha256"], row["definition_sha256"]),
                         (record["prompt_sha256"], record["source"]["definition_sha256"]))
        self.assertEqual((row["validator_version"], row["template_version"]),
                         (validator.VALIDATOR_VERSION, "skill_capability_v2"))
        self.assertEqual(gen.stale_skills(manifest), [])

    def test_an_edited_skill_file_matches_no_attempt_and_is_reported_stale(self):
        self.ws.run(FakeClient(GOOD))
        self.ws.skill.write_text(GOOD + "\nEdited by hand.\n", encoding="utf-8")
        (row,) = gen.build_manifest(self.ws.skills, self.ws.runs)["skills"]
        self.assertIsNone(row["accepted_attempt"])
        self.assertIsNone(row["validator_version"])
        self.assertEqual(gen.stale_skills({"skills": [row]}), [SKILL])

    def test_a_skill_accepted_under_another_validator_version_is_stale(self):
        self.ws.run(FakeClient(GOOD))
        manifest = gen.build_manifest(self.ws.skills, self.ws.runs)
        manifest["skills"][0]["validator_version"] = "capability_validator_v0"
        self.assertEqual(gen.stale_skills(manifest), [SKILL])

    def test_manifest_is_empty_without_skills_and_ignores_empty_files(self):
        self.assertEqual(gen.build_manifest(self.ws.skills, self.ws.runs), {"skills": []})
        self.ws.skill.parent.mkdir(parents=True)
        self.ws.skill.write_text("", encoding="utf-8")
        self.assertEqual(gen.build_manifest(self.ws.skills, self.ws.runs), {"skills": []})

    def test_write_records_writes_summary_and_manifest_next_to_the_data(self):
        self.ws.run(FakeClient(GOOD))
        with contextlib.redirect_stdout(io.StringIO()):
            gen.write_records(self.ws.skills, self.ws.runs)
        self.assertTrue((self.ws.skills / "manifest.json").exists())
        self.assertTrue((self.ws.runs.parent / "generation_summary.json").exists())


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
    SCRIPT = str(REPO_ROOT / "src" / "scripts" / "generate_skills.py")

    def run_cli(self, *args):
        env = {"PATH": os.environ.get("PATH", "")}
        return subprocess.run([sys.executable, "-B", self.SCRIPT, *args], capture_output=True, text=True, env=env)

    def test_dry_run_needs_no_key_sends_nothing_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir, skills_dir = Path(tmp) / "runs", Path(tmp) / "skills"
            done = self.run_cli("--dry-run", "--skills", "calendar-lookup", "email-correspondence",
                                "--runs-dir", str(runs_dir), "--skills-dir", str(skills_dir))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("email-correspondence: model gpt-6.1-sol", done.stdout)
            self.assertIn("would write SKILL.md", done.stdout)
            self.assertIn("Dry run: nothing was sent.", done.stdout)
            self.assertFalse(runs_dir.exists())
            self.assertFalse(skills_dir.exists())

    def test_dry_run_lists_all_six_skills_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = self.run_cli("--dry-run", "--skills-dir", tmp)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(sum("would write SKILL.md" in line for line in done.stdout.splitlines()), 6)

    def test_dry_run_reports_that_a_finished_skill_would_be_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / SKILL
            folder.mkdir()
            (folder / "SKILL.md").write_text("done\n", encoding="utf-8")
            self.assertIn("skip", self.run_cli("--dry-run", "--skills", SKILL, "--skills-dir", tmp).stdout)
            self.assertIn("would write SKILL.md",
                          self.run_cli("--dry-run", "--force", "--skills", SKILL, "--skills-dir", tmp).stdout)

    def test_a_real_run_without_a_key_stops_before_doing_anything(self):
        done = self.run_cli("--skills", SKILL)
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

    def test_the_key_does_not_appear_in_any_file_written_by_a_run(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeClient("bad", GOOD))
        with contextlib.redirect_stdout(io.StringIO()):
            gen.write_records(ws.skills, ws.runs)
        for path in Path(ws.tmp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8"), str(path))

    def test_the_request_log_has_no_headers_only_the_body(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        ws.run(FakeClient(GOOD))
        (log,) = ws.attempts()
        self.assertEqual(set(log["request"]), {"model", "instructions", "input", "reasoning", "max_output_tokens"})

    def test_the_model_and_settings_are_the_ones_chosen_for_the_study(self):
        record = gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)
        self.assertEqual(record["request"]["model"], "gpt-6.1-sol")
        self.assertEqual(record["request"]["reasoning"], {"effort": "high"})
        self.assertEqual((gen.MAX_REPAIRS, gen.TRANSPORT_RETRIES, gen.TIMEOUT_SECONDS), (2, 3, 300))

    def test_the_generator_sends_nothing_but_the_saved_prompt_or_its_repairs(self):
        ws = Workspace()
        self.addCleanup(ws.cleanup)
        client = FakeClient("bad", GOOD)
        ws.run(client)
        base = gen.load_prompt(cp.DEFAULT_OUT_DIR, SKILL)["request"]
        self.assertEqual(client.requests[0], base)
        self.assertTrue(client.requests[1]["input"].startswith(base["input"]))
        self.assertEqual(client.requests[1]["instructions"], base["instructions"])


if __name__ == "__main__":
    unittest.main()
