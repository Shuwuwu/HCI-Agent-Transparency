"""
Purpose:
    The Tailscale-facing safeguards of funnel_server.py, without a real Tailscale or any public address: the
    hostname is read only from a running, signed-in Tailscale with a ts.net name; publishing is refused while
    Tailscale already shares something; the funnel is started and then closed with its configuration cleared; a
    funnel that never reports a configuration is stopped and refused; and the server is started without
    --researcher-preview, on 127.0.0.1, with backups, and with SIGINT restored, so it can always be stopped with a
    shutdown backup.

Inputs:
    A stand-in tailscale script written to a temporary folder, whose Serve/Funnel state is a JSON file beside it;
    the rehearsal configuration path (the server command is captured, not run).

Outputs:
    unittest results.

Dependencies:
    src/scripts/funnel_server.py, rehearsal_tunnel.py. The route checks themselves are tested in
    tests/test_rehearsal_tunnel.py.

Usage:
    python3 -B -m unittest tests.test_funnel_server
"""

import json
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "src" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import funnel_server as fs  # noqa: E402
import rehearsal_tunnel as rt  # noqa: E402

# The stand-in CLI: `status --json` prints STATUS (from the environment), `serve status --json` prints the state
# file, `funnel <port>` writes a share to it and waits (or exits at once if FAKE_FUNNEL_SILENT is set), and
# `funnel reset` empties it.
FAKE_TAILSCALE = r'''
import json, os, sys, time
from pathlib import Path
state = Path(__file__).with_name("state.json")
args = [a for a in sys.argv[1:] if a != "--json"]
if args == ["status"]:
    print(os.environ["FAKE_STATUS"])
elif args == ["serve", "status"]:
    print(state.read_text() if state.exists() else "{}")
elif args == ["funnel", "reset"]:
    state.write_text("{}")
elif args[0] == "funnel":
    try:
        if os.environ.get("FAKE_FUNNEL_SILENT"):
            time.sleep(60)
        state.write_text(json.dumps({"AllowFunnel": {"mac.example.ts.net:443": True}}))
        time.sleep(60)
    except KeyboardInterrupt:  # stopped with SIGINT, as the real CLI is
        pass
'''
RUNNING = json.dumps({"BackendState": "Running", "Self": {"DNSName": "mac.example.ts.net."}})


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeTailscaleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.cli = self.folder / "tailscale"
        self.cli.write_text(f"#!{sys.executable}\n{FAKE_TAILSCALE}")
        self.cli.chmod(0o755)
        self.state = self.folder / "state.json"
        self.env = mock.patch.dict("os.environ", {"FAKE_STATUS": RUNNING})
        self.env.start()
        self.recheck = mock.patch.object(fs, "CLOSE_RECHECK_SECONDS", 0)
        self.recheck.start()

    def tearDown(self):
        self.recheck.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_hostname_of_a_running_tailscale(self):
        self.assertEqual(fs.funnel_hostname(str(self.cli)), "mac.example.ts.net")

    def test_stopped_or_unnamed_tailscale_is_refused(self):
        for status, message in [({"BackendState": "NeedsLogin"}, "not running and signed in"),
                                ({"BackendState": "Running", "Self": {"DNSName": ""}}, "no ts.net name")]:
            with mock.patch.dict("os.environ", {"FAKE_STATUS": json.dumps(status)}):
                with self.assertRaisesRegex(fs.FunnelError, message):
                    fs.funnel_hostname(str(self.cli))

    def test_existing_share_is_refused(self):
        fs.check_nothing_shared(str(self.cli))
        self.state.write_text(json.dumps({"TCP": {"443": {"HTTPS": True}}, "Web": {}}))
        with self.assertRaisesRegex(fs.FunnelError, r"already shares something \(\['TCP'\]\)"):
            fs.check_nothing_shared(str(self.cli))

    def test_funnel_is_started_then_closed_and_cleared(self):
        proc = fs.start_funnel(str(self.cli), free_port())
        self.assertIsNone(proc.poll())
        self.assertTrue(fs.shared_now(str(self.cli)))
        fs.close_funnel(str(self.cli), proc)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(fs.shared_now(str(self.cli)), {})

    def test_leftover_configuration_without_a_process_is_cleared(self):
        """A funnel stopped while waiting for approval can leave a Foreground entry with no process behind."""
        leftover = {"Foreground": {"cce85d18": {"AllowFunnel": {"mac.example.ts.net:443": True}}}}
        self.state.write_text(json.dumps(leftover))
        fs.close_funnel(str(self.cli), None)
        self.assertEqual(fs.shared_now(str(self.cli)), {})

    def test_funnel_without_a_configuration_is_refused(self):
        with mock.patch.dict("os.environ", {"FAKE_FUNNEL_SILENT": "1"}):
            with self.assertRaisesRegex(fs.FunnelError, "no Funnel configuration"):
                fs.start_funnel(str(self.cli), free_port(), seconds=2)


class StudyMaterialTest(unittest.TestCase):
    def test_study_with_a_placeholder_is_refused_before_tailscale_is_touched(self):
        def one_placeholder(report):
            report.add(fs.preflight.WARN, "consent", "placeholders to fill: ['secure storage platform']")
        with mock.patch.object(fs.preflight, "check_materials", one_placeholder), \
                mock.patch.object(fs, "funnel_hostname") as hostname:
            with self.assertRaisesRegex(fs.FunnelError, "secure storage platform"):
                fs.run(fs.ep.CONFIG_PATH, free_port(), "/nonexistent/tailscale")
        hostname.assert_not_called()

    def test_material_problems_lists_only_what_does_not_pass(self):
        def mixed(report):
            report.add(fs.preflight.PASS, "assessment", "assessment_v1")
            report.add(fs.preflight.WARN, "completion screen", "still a placeholder")
        with mock.patch.object(fs.preflight, "check_materials", mixed):
            self.assertEqual(fs.material_problems(), ["completion screen: still a placeholder"])


class ServerStartTest(unittest.TestCase):
    def test_server_command_and_sigint_reset(self):
        """No --researcher-preview, no --db, no --no-backup, loopback only; and the child gets SIGINT back even if
        this script was started with SIGINT ignored (as a shell does for a background job)."""
        with mock.patch.object(fs.subprocess, "Popen") as popen:
            fs.start_server(rt.REHEARSAL_CONFIG, 8765)
        command, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        for flag in ("--researcher-preview", "--db", "--no-backup", "--allow-network"):
            self.assertNotIn(flag, command)
        self.assertEqual(command[command.index("--host") + 1], "127.0.0.1")
        self.assertTrue(kwargs["start_new_session"])
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            kwargs["preexec_fn"]()
            self.assertIs(signal.getsignal(signal.SIGINT), signal.SIG_DFL)
        finally:
            signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    unittest.main()
