"""
Purpose:
    Let people on other computers walk through a rehearsal of the study over HTTPS, through a temporary Cloudflare
    Quick Tunnel (a random https://<words>.trycloudflare.com address that lasts only while this script runs). It
    starts the participant server for a simulated configuration on this computer, checks that the server does not
    serve the researcher routes, opens the tunnel to that server alone, checks the server can be reached through
    the tunnel, and prints the rehearsal participants' links with the tunnel's address. Ctrl+C stops both.

    participant_server.py refuses --researcher-preview on any address but 127.0.0.1, but a tunnel reaches the
    server over 127.0.0.1 too, so that check cannot stop a tunnel to a preview server. This script is the guard
    instead: it starts its own server without --researcher-preview, on a port nothing else is listening on, and
    opens the tunnel only after confirming that /, /chat, /static/app.js and the s= content routes answer 404.

Inputs:
    --experiment-config   a simulated (development) experiment configuration; default
                          src/data/experiment_configs/rehearsal.json. A study configuration is refused, and the
                          database is always the one the configuration names (there is no --db).
    --port                the local port for the server (default 8780); refused if anything already listens on it.
    --no-backup           passed on to participant_server.py (allowed only for simulated configurations).
    --cloudflared         the cloudflared binary (default: the one on PATH, e.g. from `brew install cloudflared`).
    The configuration's participant database must already have enrolled participants (participant_store.py
    --enrol-all).

Outputs:
    Two child processes for as long as the script runs: participant_server.py on 127.0.0.1:<port> and
    `cloudflared tunnel --url http://127.0.0.1:<port>`. Printed to the terminal: the tunnel address and one
    participant link per enrolled rehearsal participant (<tunnel>/p/<token>). Nothing is written by this script
    itself; the server records rehearsal data in the configuration's database and writes its usual startup and
    shutdown backups (unless --no-backup).

Dependencies:
    experiment_plan.py (load_config), participant_store.py (connect, link), participant_server.py (run as a
    subprocess, unchanged), and the cloudflared binary. No Cloudflare account is needed for a Quick Tunnel.

Usage:
    python3 -B src/scripts/rehearsal_tunnel.py
    python3 -B src/scripts/rehearsal_tunnel.py --port 8781 --no-backup
    Stop it with Ctrl+C in the same terminal: the tunnel closes first, then the server (which writes its
    shutdown backup). The trycloudflare.com address stops working at once and is not reused.
"""

import argparse
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import experiment_plan as ep
import participant_store as store

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "src" / "scripts"
REHEARSAL_CONFIG = ROOT / "src" / "data" / "experiment_configs" / "rehearsal.json"
DEFAULT_PORT = 8780
# cloudflared logs the Quick Tunnel's address in a banner on stderr. api.trycloudflare.com is the service it asks
# for the tunnel, never the tunnel itself.
TUNNEL_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
SERVER_START_SECONDS = 30
TUNNEL_START_SECONDS = 60
# A new trycloudflare.com name can take a few seconds to exist in DNS after cloudflared reports it. Looking it up
# at once can get a "no such name" answer that the local resolver then caches for a while, so the first check
# through the tunnel waits REMOTE_FIRST_CHECK_DELAY seconds.
REMOTE_FIRST_CHECK_DELAY = 10
REMOTE_CHECK_SECONDS = 120


class TunnelError(RuntimeError):
    """A safeguard refused to open the tunnel, or a child process failed."""


def check_config(config: ep.Config) -> None:
    """Refuse any configuration that is not simulated, so a tunnel never serves the study or its database.

    A simulated configuration's environment is "development", and participant_store.connect refuses a database
    stamped for the other environment, so the server cannot open the study database under it either.
    """
    if not config["simulated"] or config["environment"] != "development":
        raise TunnelError(f"{config['path']} is not a simulated configuration; the tunnel is for rehearsals with "
                          f"simulated participants only, never the study")
    study_db = ep.load_config(ep.CONFIG_PATH)["database_path"].resolve()
    if config["database_path"].resolve() == study_db:
        raise TunnelError(f"{config['path']} names the study database {study_db}")


def check_port_free(port: int) -> None:
    """Refuse a port something already listens on (a researcher preview server, or an older participant server)."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise TunnelError(f"port {port} is already in use; stop what is listening there or choose --port")


def enrolled_tokens(config: ep.Config) -> list[tuple[str, str]]:
    """(participant_id, token) for every participant enrolled in the configuration's own database."""
    db = config["database_path"]
    if not db.is_file():
        raise TunnelError(f"{db} does not exist: enrol the rehearsal participants first (participant_store.py "
                          f"--config {config['path']} --enrol-all)")
    conn = store.connect(db, config["environment"])
    try:
        rows = conn.execute("SELECT participant_id, token FROM participants ORDER BY participant_id").fetchall()
    finally:
        conn.close()
    return [(row["participant_id"], row["token"]) for row in rows]


def http_status(url: str, timeout: float = 10) -> int:
    """The HTTP status code of a GET of `url` (an HTTP error is a status, not an exception)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rehearsal-tunnel-check"}),
                                    timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def researcher_probe_paths(config: ep.Config) -> list[str]:
    """Paths that only a --researcher-preview server answers. Each names a real stimulus (the practice one), so a
    preview server would answer them 200 rather than 404 for an unknown stimulus."""
    folder = config["practice"]["stimulus"]
    paths = ["/", f"/chat?s={folder}&c=high", "/static/app.js"]
    paths += [f"/api/{route}?s={folder}&c=high" for route in ("stimulus", "workspace", "skills")]
    return paths


def check_no_researcher_routes(base_url: str, config: ep.Config) -> None:
    """Raise TunnelError unless every researcher route answers 404 at `base_url`."""
    served = {path: status for path in researcher_probe_paths(config)
              if (status := http_status(base_url + path)) != 404}
    if served:
        raise TunnelError(f"the server at {base_url} answers researcher routes {served}; refusing to open a tunnel")


def check_participant_routes(base_url: str) -> None:
    """Raise TunnelError unless `base_url` serves the participant front end (a whitelisted static file)."""
    status = http_status(base_url + "/static/style.css")
    if status != 200:
        raise TunnelError(f"{base_url}/static/style.css answered {status}, expected 200")


def start_server(config_path: Path, port: int, backup: bool) -> subprocess.Popen:
    """Start participant_server.py for `config_path` on 127.0.0.1:`port`, never with --researcher-preview.

    Its output is passed through to this terminal, so its startup lines (configuration, database, backup) and its
    request log are shown as when it is run on its own.
    """
    command = [sys.executable, "-B", str(SCRIPTS / "participant_server.py"), "--experiment-config", str(config_path),
               "--host", "127.0.0.1", "--port", str(port)]
    if not backup:
        command.append("--no-backup")
    # Its own session, so Ctrl+C in this terminal reaches only this script, which then stops the server once.
    return subprocess.Popen(command, cwd=ROOT, start_new_session=True)


def wait_for_server(proc: subprocess.Popen, base_url: str, seconds: float = SERVER_START_SECONDS) -> None:
    """Wait until the server answers, or raise TunnelError if it exits or does not answer in time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise TunnelError(f"participant_server.py exited with code {proc.returncode}")
        try:
            check_participant_routes(base_url)
            return
        except (OSError, TunnelError):
            time.sleep(0.2)
    raise TunnelError(f"participant_server.py did not answer at {base_url} within {seconds:.0f} s")


def start_tunnel(cloudflared: str, port: int) -> tuple[subprocess.Popen, str]:
    """Start a Quick Tunnel to 127.0.0.1:`port` and return the process and its https://….trycloudflare.com address.

    cloudflared's log is read on a background thread for as long as it runs (so its pipe never fills); lines at
    error level are passed through, the rest are dropped. Raises TunnelError if it exits or reports no address.
    """
    # --grace-period: on Ctrl+C, wait at most 5 s for requests in flight rather than cloudflared's default 30 s.
    proc = subprocess.Popen([cloudflared, "tunnel", "--no-autoupdate", "--grace-period", "5s",
                             "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
    found = threading.Event()
    address = []

    def read_log():
        for line in proc.stderr:
            if not found.is_set() and (match := TUNNEL_URL.search(line)):
                address.append(match.group(0))
                found.set()
            elif " ERR " in line:
                print(f"cloudflared: {line.rstrip()}", file=sys.stderr)

    threading.Thread(target=read_log, daemon=True).start()
    deadline = time.monotonic() + TUNNEL_START_SECONDS
    while not found.wait(0.5):
        if proc.poll() is not None or time.monotonic() > deadline:
            stop(proc, signal.SIGTERM)
            raise TunnelError("cloudflared did not report a trycloudflare.com address")
    return proc, address[0]


def wait_for_remote(tunnel_url: str, config: ep.Config, seconds: float = REMOTE_CHECK_SECONDS) -> None:
    """Check through the tunnel that the participant front end is served and the researcher routes are not."""
    time.sleep(REMOTE_FIRST_CHECK_DELAY)
    deadline = time.monotonic() + seconds
    while True:
        try:
            check_participant_routes(tunnel_url)
            break
        except (OSError, TunnelError) as exc:
            if time.monotonic() > deadline:
                raise TunnelError(f"the tunnel did not reach the server within {seconds:.0f} s: {exc}") from exc
            time.sleep(2)
    check_no_researcher_routes(tunnel_url, config)


def stop(proc: subprocess.Popen | None, sig: int, timeout: float = 30) -> None:
    """Send `sig` to a running child and wait for it; kill it if it does not stop within `timeout` seconds."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(sig)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run(config_path: Path, port: int, backup: bool, cloudflared: str) -> int:
    """Check, start the server and the tunnel, print the links, and wait for Ctrl+C or a child to exit."""
    config = ep.load_config(config_path)
    check_config(config)
    tokens = enrolled_tokens(config)
    check_port_free(port)
    local = f"http://127.0.0.1:{port}"
    server = tunnel = None
    # Both children run in their own sessions, so Ctrl+C reaches only this script; the finally block then closes
    # the tunnel first and stops the server with one SIGINT, which it handles by writing its shutdown backup.
    try:
        server = start_server(config_path, port, backup)
        wait_for_server(server, local)
        check_no_researcher_routes(local, config)
        tunnel, url = start_tunnel(cloudflared, port)
        wait_for_remote(url, config)
        print(f"\nTunnel:     {url} -> {local}  (rehearsal only; simulated participants)")
        print("Checked:    participant files served through the tunnel; researcher routes answer 404")
        print("Links (rehearsal participants):")
        for participant, token in tokens:
            print(f"  {participant}\t{store.link(token, url)}")
        print("Press Ctrl+C to close the tunnel and stop the server.\n", flush=True)
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(0.5)
        which = "participant_server.py" if server.poll() is not None else "cloudflared"
        print(f"{which} stopped unexpectedly; closing the other.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        stop(tunnel, signal.SIGTERM)
        stop(server, signal.SIGINT)
        print("Tunnel closed and server stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--experiment-config", type=Path, default=REHEARSAL_CONFIG, dest="experiment_config",
                        help="a simulated experiment configuration (default: rehearsal.json)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"local port for the rehearsal server (default {DEFAULT_PORT})")
    parser.add_argument("--no-backup", action="store_false", dest="backup",
                        help="do not back the rehearsal database up when the server starts and stops")
    parser.add_argument("--cloudflared", default=shutil.which("cloudflared"),
                        help="path to the cloudflared binary (default: the one on PATH)")
    args = parser.parse_args()
    if not args.cloudflared:
        parser.error("cloudflared is not installed (brew install cloudflared), or pass --cloudflared PATH")
    try:
        sys.exit(run(args.experiment_config, args.port, args.backup, args.cloudflared))
    except (TunnelError, ep.ConfigError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
