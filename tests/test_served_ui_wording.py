"""
Purpose:
    Guard that the front-end files a participant's browser can fetch say nothing about the study design. Those
    files are served byte for byte, comments included (participant_server.py), so a comment naming the
    transparency conditions or the injected trials would be readable in the browser's developer tools.

Inputs:
    participant_server.STATIC_FILES (served at /static/<file> without --researcher-preview) and flow.html (served at
    /p/<token>), all read from src/participant_ui/.

    Also checks over HTTP that the researcher page script (app.js), which reads the condition from its URL, is
    served only with --researcher-preview.

Outputs:
    unittest results; a failure lists each file, line number and matched word.

Dependencies:
    src/scripts/participant_server.py (STATIC_FILES, RESEARCHER_STATIC_FILES, UI_DIR, Handler),
    tests/participant_http.py (preview_handler).

Usage:
    python3 -B -m unittest tests.test_served_ui_wording
"""

import re
import sys
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

# Words that describe the manipulation or the research questions. Matched case-insensitively as word prefixes, so
# "transparen" catches "transparency" and "transparent" alike; CSS's `transparent` colour keyword is allowed below.
STUDY_WORDS = re.compile(r"\b(transparen\w*|conditions?|inject\w*|unsafe|hypothes\w*|manipulat\w*|attack\w*|"
                         r"malicious)\b", re.IGNORECASE)
# Language keywords that happen to share a stem with STUDY_WORDS: CSS's colour keyword.
ALLOWED = {"style.css": {"transparent"}}


def participant_served_files():
    """The files of src/participant_ui/ a participant can fetch: the /static/ whitelist plus the flow page."""
    return sorted(set(ps.STATIC_FILES) | {"flow.html"})


class ServedWordingTest(unittest.TestCase):
    def test_served_files_do_not_describe_the_study(self):
        hits = []
        for name in participant_served_files():
            text = (ps.UI_DIR / name).read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                for match in STUDY_WORDS.finditer(line):
                    if match.group(0).lower() not in ALLOWED.get(name, set()):
                        hits.append(f"{name}:{number}: {match.group(0)}")
        self.assertEqual(hits, [], "study wording in files served to participants (see README, participant_ui)")

    def test_researcher_page_is_not_in_the_participant_whitelist(self):
        # app.js reads the condition from its URL by design, so it must only be served with --researcher-preview.
        self.assertNotIn("app.js", ps.STATIC_FILES)
        self.assertIn("app.js", ps.RESEARCHER_STATIC_FILES)


def serve(handler):
    """Start `handler` on a free local port; returns (server, base URL). Static routes need no database."""
    quiet = type("Quiet" + handler.__name__, (handler,), {"log_message": lambda self, fmt, *args: None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def status_of(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class ResearcherScriptServingTest(unittest.TestCase):
    """/static/app.js answers 404 on the participant server and 200 only with the researcher preview on."""

    def check(self, handler, expected):
        server, base = serve(handler)
        try:
            self.assertEqual(status_of(base + "/static/app.js"), expected)
            self.assertEqual(status_of(base + "/static/scenario_view.js"), 200)  # shared files stay served
        finally:
            server.shutdown()
            server.server_close()

    def test_participant_server_does_not_serve_app_js(self):
        self.check(type("ParticipantHandler", (ps.Handler,), {"researcher_preview": False}), 404)

    def test_researcher_preview_serves_app_js(self):
        self.check(preview_handler(), 200)


if __name__ == "__main__":
    unittest.main()
