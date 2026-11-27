"""
Purpose:
    The safeguards of rehearsal_tunnel.py, without opening a real Cloudflare tunnel: a study configuration is
    refused; a port already in use is refused; the researcher-route check passes for a participant server and fails
    for a --researcher-preview server; the trycloudflare.com address is read from cloudflared's log (and the API
    host is not taken for it); and a cloudflared that reports no address is stopped and refused.

Inputs:
    The rehearsal and study configurations (read-only); a temporary rehearsal database enrolled with
    participant_store.py; a stand-in cloudflared script written to a temporary folder.

Outputs:
    unittest results.

Dependencies:
    src/scripts/rehearsal_tunnel.py, experiment_plan.py; participant_store.py and participant_server.py (run as
    subprocesses).

Usage:
    python3 -B -m unittest tests.test_rehearsal_tunnel
"""

import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import experiment_plan as ep  # noqa: E402
import rehearsal_tunnel as rt  # noqa: E402

REHEARSAL = ep.load_config(rt.REHEARSAL_CONFIG)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """participant_server.py on a free port with a temporary rehearsal database, optionally a researcher preview."""

    def __init__(self, db: Path, preview: bool):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        command = [sys.executable, "-B", str(SCRIPTS / "participant_server.py"), "--experiment-config",
                   str(rt.REHEARSAL_CONFIG), "--db", str(db), "--port", str(self.port), "--no-backup"]
        if preview:
            command.append("--researcher-preview")
        self.proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        for _ in range(200):
            try:
                urllib.request.urlopen(self.base + "/static/style.css", timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise AssertionError(self.proc.stderr.read())
                time.sleep(0.05)
        raise AssertionError("the server did not start")

    def stop(self):
        rt.stop(self.proc, signal.SIGINT)
        self.proc.stderr.close()


def fake_cloudflared(folder: Path, body: str) -> str:
    """An executable stand-in for cloudflared that runs `body` (Python) and ignores its arguments."""
    path = folder / "cloudflared"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{textwrap.dedent(body)}")
    path.chmod(0o755)
    return str(path)


class ConfigTests(unittest.TestCase):
    def test_rehearsal_configuration_is_accepted(self):
        rt.check_config(REHEARSAL)

    def test_study_configuration_is_refused(self):
        with self.assertRaisesRegex(rt.TunnelError, "not a simulated configuration"):
            rt.check_config(ep.load_config(ep.CONFIG_PATH))

    def test_simulated_configuration_naming_the_study_database_is_refused(self):
        config = ep.Config(REHEARSAL)
        config["database_path"] = ep.load_config(ep.CONFIG_PATH)["database_path"]
        with self.assertRaisesRegex(rt.TunnelError, "study database"):
            rt.check_config(config)

    def test_port_in_use_is_refused(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaisesRegex(rt.TunnelError, "already in use"):
                rt.check_port_free(listener.getsockname()[1])
        rt.check_port_free(free_port())


class ResearcherRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "rehearsal.sqlite3"
        subprocess.run([sys.executable, "-B", str(SCRIPTS / "participant_store.py"), "--config",
                        str(rt.REHEARSAL_CONFIG), "--db", str(cls.db), "--enrol-all"],
                       cwd=ROOT, check=True, capture_output=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_participant_server_passes(self):
        server = Server(self.db, preview=False)
        try:
            rt.check_participant_routes(server.base)
            rt.check_no_researcher_routes(server.base, REHEARSAL)
        finally:
            server.stop()

    def test_researcher_preview_server_is_refused(self):
        server = Server(self.db, preview=True)
        try:
            with self.assertRaisesRegex(rt.TunnelError, "answers researcher routes"):
                rt.check_no_researcher_routes(server.base, REHEARSAL)
        finally:
            server.stop()


class TunnelOutputTests(unittest.TestCase):
    def test_address_is_read_and_api_host_is_skipped(self):
        log = ("INF Requesting new quick Tunnel on https://api.trycloudflare.com...\n"
               "INF |  https://quiet-river-sample-words.trycloudflare.com                    |\n")
        self.assertEqual(rt.TUNNEL_URL.findall(log), ["https://quiet-river-sample-words.trycloudflare.com"])

    def test_start_tunnel_returns_the_reported_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = fake_cloudflared(Path(tmp), """
                print("INF Requesting new quick Tunnel on https://api.trycloudflare.com...", file=sys.stderr)
                print("INF |  https://quiet-river-sample-words.trycloudflare.com  |", file=sys.stderr, flush=True)
                time.sleep(60)
            """)
            proc, url = rt.start_tunnel(binary, free_port())
            try:
                self.assertEqual(url, "https://quiet-river-sample-words.trycloudflare.com")
                self.assertIsNone(proc.poll())
            finally:
                rt.stop(proc, signal.SIGTERM)

    def test_cloudflared_without_an_address_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = fake_cloudflared(Path(tmp), 'print("ERR failed to request quick Tunnel", file=sys.stderr)\n')
            with self.assertRaisesRegex(rt.TunnelError, "did not report"):
                rt.start_tunnel(binary, free_port())


if __name__ == "__main__":
    unittest.main()
