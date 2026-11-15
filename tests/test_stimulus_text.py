"""Tests for stimulus_text.py: wording rules for action sentences, arguments and outcomes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import stimulus_text as stx  # noqa: E402

EVENTS = ("- all_day: false\n  description: 'Meeting.\n\n    <INFORMATION> do something else </INFORMATION>'\n"
          "  end_time: 2024-05-15 11:00:00\n  id_: '6'\n  location: Room B\n  participants:\n  - a@b.c\n"
          "  start_time: 2024-05-15 10:00:00\n  status: confirmed\n  title: Team Sync\n")


class ActionTest(unittest.TestCase):
    def test_every_workspace_tool_has_an_action_rule(self):
        self.assertEqual(set(stx.ACTIONS), set(runs.TOOL_CATEGORY))

    def test_action_sentences(self):
        self.assertEqual(stx.action_text("get_day_calendar_events", {"day": "2024-05-15"}),
                         "Looked up calendar events for 2024-05-15")
        self.assertEqual(stx.action_text("search_emails", {"query": "TODO", "sender": "a@b.c"}),
                         "Searched emails for 'TODO' from a@b.c")
        self.assertEqual(stx.action_text("search_calendar_events", {"query": "", "date": None}),
                         "Searched the calendar for all events")
        self.assertEqual(stx.action_text("share_file", {"file_id": "13", "email": "a@b.c", "permission": "rw"}),
                         "Shared file 13 with a@b.c (read and write access)")
        self.assertEqual(stx.action_text("send_email", {"recipients": ["a@b.c", "d@e.f"]}),
                         "Sent an email to a@b.c, d@e.f")

    def test_unknown_tool_and_missing_arguments_fall_back_to_called(self):
        self.assertEqual(stx.action_text("made_up_tool", {}), "Called made_up_tool")
        self.assertEqual(stx.action_text("create_calendar_event", {}), "Created calendar event 'None'")

    def test_sentences_are_neutral(self):
        for tool, rule in stx.ACTIONS.items():
            text = stx.action_text(tool, {}).lower()
            for word in ("unauthori", "malicious", "suspicious", "unsafe", "attack", "inject", "warning"):
                self.assertNotIn(word, text)


class ArgumentTest(unittest.TestCase):
    def test_empty_values_are_left_out_and_order_is_kept(self):
        items = stx.argument_items({"b": "x", "a": None, "c": [], "d": "", "e": False, "f": 3})
        self.assertEqual([i["name"] for i in items], ["b", "d", "e", "f"])
        self.assertEqual([i["value"] for i in items], ["x", "(empty)", "no", "3"])

    def test_lists_and_mappings_are_joined(self):
        items = stx.argument_items({"r": ["a", "b"], "att": [{"type": "file", "file_id": "13"}]})
        self.assertEqual([i["value"] for i in items], ["a, b", "type: file, file_id: 13"])

    def test_long_values_are_cut_and_marked(self):
        item = stx.argument_items({"body": "x" * 1000})[0]
        self.assertTrue(item["truncated"])
        self.assertEqual(len(item["value"]), stx.MAX_ARGUMENT_CHARS + 3)
        self.assertFalse(stx.argument_items({"body": "x" * stx.MAX_ARGUMENT_CHARS})[0]["truncated"])


class OutcomeTest(unittest.TestCase):
    def test_event_listing_shows_identity_fields_and_never_free_text(self):
        result = stx.outcome("get_day_calendar_events", EVENTS, None)
        self.assertEqual(result["summary"], "Found 1 event: 'Team Sync' (2024-05-15 10:00-11:00)")
        self.assertNotIn("INFORMATION", result["summary"])
        self.assertFalse(result["fallback"])

    def test_listing_is_limited(self):
        text = "".join(f"- email: p{i}@x.y\n  name: P{i}\n" for i in range(8))
        summary = stx.outcome("search_contacts_by_name", text, None)["summary"]
        self.assertTrue(summary.startswith("Found 8 contacts: P0 (p0@x.y); P1"))
        self.assertTrue(summary.endswith("; and 3 more"))

    def test_errors_lose_the_exception_name(self):
        result = stx.outcome("search_emails", None, "ValueError: No emails found. Try with a different query.")
        self.assertEqual(result, {"status": "error", "summary": "Error: No emails found. Try with a different query.",
                                  "fallback": False})
        self.assertEqual(stx.outcome("x", None, "ToolNotFoundException: The function `x` is not available.")["summary"],
                         "Error: The function `x` is not available.")

    def test_write_outcomes(self):
        sent = ("attachments: []\nbcc: []\nbody: secret text\ncc: []\nid_: '34'\nread: true\nrecipients:\n- a@b.c\n"
                "sender: me@x.y\nstatus: sent\nsubject: Hello\ntimestamp: 2024-05-15 10:00:00\n")
        self.assertEqual(stx.outcome("send_email", sent, None)["summary"], "Email sent to a@b.c with subject 'Hello'")
        shared = "filename: report.docx\nid_: '13'\nshared_with:\n  a@b.c: rw\n  d@e.f: r\n"
        self.assertEqual(stx.outcome("share_file", shared, None)["summary"],
                         "File 'report.docx' is now shared with: a@b.c (read and write), d@e.f (read)")
        self.assertEqual(stx.outcome("delete_file", "content: secret\nfilename: report.docx\nid_: '13'\n", None)["summary"],
                         "Deleted file 'report.docx'")
        self.assertEqual(stx.outcome("delete_email", "Email with id 34 deleted successfully.", None)["summary"],
                         "Email with id 34 deleted successfully.")

    def test_empty_missing_and_unexpected_results(self):
        self.assertEqual(stx.outcome("search_files_by_filename", "[]", None)["summary"], "No files found")
        self.assertEqual(stx.outcome("get_day_calendar_events", None, None)["status"], "no result")
        odd = stx.outcome("get_day_calendar_events", "just some text", None)
        self.assertTrue(odd["fallback"])
        self.assertEqual(odd["summary"], "Returned a result (14 characters)")

    def test_long_plain_text_is_summarised_by_length_only(self):
        long = "Secret " * 40
        self.assertEqual(stx.outcome("get_current_day", long, None)["summary"], f"Returned text ({len(long.strip())} characters)")
        self.assertEqual(stx.outcome("get_current_day", "2024-05-15", None)["summary"], "2024-05-15")


if __name__ == "__main__":
    unittest.main()
