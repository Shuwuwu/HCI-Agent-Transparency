"""
Purpose:
    Make the participant server reachable from participants' own computers over HTTPS, through Tailscale Funnel, for
    remote sessions of the study (or, for testing, a simulated configuration such as the rehearsal). Funnel gives
    this computer a fixed public address, https://<this computer>.<tailnet>.ts.net, so participant links printed
    once stay valid across restarts. The TLS connection ends on this computer (Tailscale's relays forward encrypted
    traffic and cannot read link tokens or answers). The server keeps listening on 127.0.0.1 only, and the
    participant database, its backups and its environment checks are exactly those of participant_server.py.

    Safeguards, in order (the route checks are those of rehearsal_tunnel.py, which is left unchanged):
      1. the configuration loads with its assignments; for the study (any non-simulated configuration), no
         placeholder is left in the consent text, the questionnaires or the debriefing of the completion screen
         (preflight.check_materials); and the configuration's database exists (enrol first);
      2. nothing listens on the chosen port, and Tailscale shares nothing yet (no Serve or Funnel configuration),
         so the funnel opened here is the only thing this computer publishes;
      3. participant_server.py is started without --researcher-preview, always with the configuration's own
         database (there is no --db) and, for the study, always with its startup and shutdown backups;
      4. before publishing: /, /chat, /static/app.js and the s= content routes answer 404 on 127.0.0.1;
      5. after publishing: the participant files are served at the public address and the researcher routes
         answer 404 there too. Any failure closes the funnel and stops the server.
    Link tokens are never printed here; the command that prints the links is shown instead.

Inputs:
    --experiment-config   default src/data/experiment_configs/study.json (the study); any configuration is
                          accepted, so a simulated one (rehearsal.json) can be used to test the setup first.
    --port                local port of the server (default 8765, the port the study's links use).
    --tailscale           the tailscale CLI (default: the one on PATH, or the macOS app's bundled CLI).
    The Tailscale app installed, signed in, and Funnel approved for the tailnet (first `tailscale funnel` run).

Outputs:
    For as long as it runs: participant_server.py on 127.0.0.1:<port>, and `tailscale funnel <port>`, which
    publishes https://<host>.ts.net/ -> http://127.0.0.1:<port>. Printed: the public address, the checks passed,
    and the participant_store.py command that prints the links for that address. On Ctrl+C the funnel is closed and
    its configuration removed, then the server is stopped with SIGINT so it writes its shutdown backup.

Dependencies:
    rehearsal_tunnel.py (route checks, port check, process stop), experiment_plan.py (load_config,
    load_assignments), preflight.py (check_materials), participant_server.py (run as a subprocess, unchanged),
    the tailscale CLI.

Usage:
    python3 -B src/scripts/funnel_server.py --experiment-config src/data/experiment_configs/rehearsal.json   # test
    python3 -B src/scripts/funnel_server.py                                                                # study
    Stop with Ctrl+C in the same terminal. Check nothing is still shared with `tailscale serve status --json`
    (it must print {}; plain `tailscale funnel status` does not show "Foreground" entries).
"""

import argparse
import json
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import experiment_plan as ep
import preflight
import rehearsal_tunnel as rt

ROOT = rt.ROOT
SCRIPTS = rt.SCRIPTS
DEFAULT_PORT = 8765
# The CLI bundled in the macOS Tailscale app, used when no `tailscale` is on PATH.
MAC_APP_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# Long enough to open and approve the link `tailscale funnel` prints the first time Funnel is used on a tailnet.
FUNNEL_START_SECONDS = 300
REMOTE_CHECK_SECONDS = 120
CLOSE_RECHECK_SECONDS = 2  # after the reset, before confirming nothing is shared


class FunnelError(rt.TunnelError):
    """A safeguard refused to publish the server, or Tailscale is not ready."""


def tailscale_json(tailscale: str, *args: str) -> dict:
    """Run `tailscale <args> --json` and return its JSON output ({} for empty output)."""
    result = subprocess.run([tailscale, *args, "--json"], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise FunnelError(f"`tailscale {' '.join(args)}` failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def funnel_hostname(tailscale: str) -> str:
    """This computer's MagicDNS name (e.g. mac.tailnet.ts.net), which is also its Funnel hostname.

    Raises FunnelError unless Tailscale is running and signed in.
    """
    status = tailscale_json(tailscale, "status")
    if status.get("BackendState") != "Running":
        raise FunnelError(f"Tailscale is not running and signed in (state {status.get('BackendState')!r}); open the "
                          f"Tailscale app and sign in")
    name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name.endswith(".ts.net"):
        raise FunnelError(f"this computer has no ts.net name ({name!r}); enable MagicDNS for the tailnet")
    return name


def shared_now(tailscale: str) -> dict:
    """The current Serve/Funnel configuration with its empty parts left out ({} when nothing is shared)."""
    return {key: value for key, value in tailscale_json(tailscale, "serve", "status").items() if value}


def check_nothing_shared(tailscale: str) -> None:
    """Refuse to start while Tailscale already shares anything, so this script's funnel is the only one and
    clearing the configuration on exit removes nothing else."""
    if shared := shared_now(tailscale):
        # `tailscale funnel status` does not list "Foreground" entries; the JSON form of serve status does.
        raise FunnelError(f"Tailscale already shares something ({sorted(shared)}); review it with `tailscale serve "
                          f"status --json` and remove it (`tailscale funnel reset`) before publishing")


def start_server(config_path: Path, port: int) -> subprocess.Popen:
    """Start participant_server.py on 127.0.0.1:`port`, never with --researcher-preview, always with backups.

    Unlike rehearsal_tunnel.start_server, SIGINT is reset to its default in the child: a shell that starts this
    script in the background makes its children ignore SIGINT, and then the server could not be stopped with the
    signal that makes it write its shutdown backup.
    """
    command = [sys.executable, "-B", str(SCRIPTS / "participant_server.py"), "--experiment-config", str(config_path),
               "--host", "127.0.0.1", "--port", str(port)]
    return subprocess.Popen(command, cwd=ROOT, start_new_session=True,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))


def start_funnel(tailscale: str, port: int, seconds: float = FUNNEL_START_SECONDS) -> subprocess.Popen:
    """Run `tailscale funnel <port>` in the foreground and wait until Tailscale reports a Funnel configuration.

    Its output (the address, or the link to approve Funnel for the tailnet on first use) is passed through.
    """
    proc = subprocess.Popen([tailscale, "funnel", str(port)], start_new_session=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise FunnelError(f"`tailscale funnel {port}` exited with code {proc.returncode}")
        if shared_now(tailscale):
            return proc
        time.sleep(0.5)
    rt.stop(proc, signal.SIGINT)
    raise FunnelError(f"Tailscale reported no Funnel configuration within {seconds:.0f} s (approve Funnel in the "
                      f"link it printed, then start again)")


def close_funnel(tailscale: str, proc: subprocess.Popen | None) -> None:
    """Stop the funnel process, then clear the Serve/Funnel configuration and confirm it is empty.

    The reset is unconditional: a `tailscale funnel` stopped while it waited for Funnel to be approved was seen to
    leave a "Foreground" entry behind (443 -> this port, Funnel on) with no process attached, which would publish
    whatever later listens on the port. There was no configuration before this script started
    (check_nothing_shared), so the reset removes nothing else.
    """
    rt.stop(proc, signal.SIGINT, timeout=10)
    try:
        subprocess.run([tailscale, "funnel", "reset"], capture_output=True, timeout=30)
        time.sleep(CLOSE_RECHECK_SECONDS)
        if shared := shared_now(tailscale):
            print(f"WARNING: Tailscale still shares {sorted(shared)}; run `tailscale funnel reset`", file=sys.stderr)
    except (OSError, FunnelError, subprocess.SubprocessError) as exc:
        print(f"WARNING: could not confirm the funnel is closed ({exc}); check `tailscale serve status --json`",
              file=sys.stderr)


def check_public(public_url: str, config: ep.Config, seconds: float = REMOTE_CHECK_SECONDS) -> None:
    """Through the public address: the participant files are served and every researcher route answers 404."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            rt.check_participant_routes(public_url)
            break
        except (OSError, rt.TunnelError) as exc:
            if time.monotonic() > deadline:
                raise FunnelError(f"{public_url} did not reach the server within {seconds:.0f} s: {exc}") from exc
            time.sleep(2)
    rt.check_no_researcher_routes(public_url, config)


def material_problems() -> list[str]:
    """What preflight.check_materials does not pass (placeholders left in the consent text, the questionnaires or
    the debriefing, or a material that does not load), as "check: detail" lines; [] when all pass."""
    report = preflight.Report()
    preflight.check_materials(report)
    return [f"{check}: {detail}" for status, check, detail in report.findings if status != preflight.PASS]


def run(config_path: Path, port: int, tailscale: str) -> int:
    """Check, start the server, publish it, verify, and wait for Ctrl+C or a child to exit."""
    config = ep.load_config(config_path)
    ep.load_assignments(config)
    # preflight.py only warns about placeholders; real participants must never be sent to one, so publishing the
    # study is refused outright (a simulated configuration may be published with them, for testing).
    if not config["simulated"] and (problems := material_problems()):
        raise FunnelError(f"the study cannot be published while participant-facing material is unfinished: "
                          f"{problems}")
    if not config["database_path"].is_file():
        raise FunnelError(f"{config['database_path']} does not exist: enrol participants first (participant_store.py "
                          f"--config {config_path} --enrol-all)")
    host = funnel_hostname(tailscale)
    check_nothing_shared(tailscale)
    rt.check_port_free(port)
    local, public = f"http://127.0.0.1:{port}", f"https://{host}"
    server = funnel = None
    try:
        server = start_server(config_path, port)
        rt.wait_for_server(server, local)
        rt.check_no_researcher_routes(local, config)
        funnel = start_funnel(tailscale, port)
        check_public(public, config)
        kind = "SIMULATED" if config["simulated"] else "STUDY"
        print(f"\nPublic:     {public} -> {local}  ({kind}: {config['database_path'].name})")
        print("Checked:    participant files served at the public address; researcher routes answer 404")
        print(f"Links:      python3 -B src/scripts/participant_store.py --config {config_path} --links "
              f"--base-url {public}")
        print("Press Ctrl+C to close the funnel and stop the server.\n", flush=True)
        while server.poll() is None and funnel.poll() is None:
            time.sleep(0.5)
        which = "participant_server.py" if server.poll() is not None else "tailscale funnel"
        print(f"{which} stopped unexpectedly; closing the other.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        close_funnel(tailscale, funnel)  # unpublish first, so no request arrives while the server shuts down
        rt.stop(server, signal.SIGINT)
        print("Funnel closed and server stopped.")


def default_tailscale() -> str | None:
    """The tailscale CLI on PATH, else the macOS app's bundled CLI if installed."""
    return shutil.which("tailscale") or (MAC_APP_CLI if Path(MAC_APP_CLI).is_file() else None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--experiment-config", type=Path, default=ep.CONFIG_PATH, dest="experiment_config",
                        help="experiment configuration (default: study.json; use rehearsal.json to test)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"local port (default {DEFAULT_PORT})")
    parser.add_argument("--tailscale", default=default_tailscale(), help="path to the tailscale CLI")
    args = parser.parse_args()
    if not args.tailscale:
        parser.error("the tailscale CLI was not found; install the Tailscale app, or pass --tailscale PATH")
    try:
        sys.exit(run(args.experiment_config, args.port, args.tailscale))
    except (rt.TunnelError, ep.ConfigError, OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
