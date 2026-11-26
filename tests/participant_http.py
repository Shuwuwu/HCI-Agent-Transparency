"""
Purpose:
    Shared helper for the tests that serve participant_server.py over HTTP and use its researcher routes
    (/ and /chat, and the s=<folder> form of the content routes). Those routes are opt-in on the server —
    Handler.researcher_preview, set by --researcher-preview — because the participant-facing server must not
    expose the stimulus index or let a URL name a stimulus and a condition. Mutating participant_server.Handler
    would leak into the tests that serve the participant flow, which must see the flag off, so each caller gets
    its own throwaway subclass. Not a test module.

Inputs:
    Nothing. Callers pass any Handler class attributes they want set (db_path, config, payload).

Outputs:
    Python classes only.

Dependencies:
    src/scripts/participant_server.py.

Usage:
    from participant_http import preview_handler            (tests add tests/ to sys.path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), preview_handler())
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import participant_server as ps  # noqa: E402


def preview_handler(**attrs):
    """A participant_server.Handler subclass with the researcher routes served, plus `attrs` as class attributes.

    Each call returns a new class, so no two test modules can affect one another through it. Any Handler class
    attribute may be passed.
    """
    return type("PreviewHandler", (ps.Handler,), {"researcher_preview": True, **attrs})
