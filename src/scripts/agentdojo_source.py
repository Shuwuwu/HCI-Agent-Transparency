"""
Purpose:
    Read-only access to AgentDojo's original source files and recorded execution traces at the pinned
    commit, straight from a local clone of the AgentDojo repository. Nothing is copied into this
    repository: every read goes through git's object database (`git cat-file`), so the text is exactly what
    existed at the pinned commit no matter which branch the clone has checked out. (The clone's working
    tree is usually on `main`, whose workspace data, tools and attack checks differ from the pinned commit.)

    The module also writes a manifest that records, for every source file used, its path, git blob hash and
    SHA-256, so any later analysis can state exactly which version of which file it relied on.

Inputs:
    A local clone of https://github.com/ethz-spylab/agentdojo that contains the pinned commit.
    Its location is taken from the AGENTDOJO_REPO environment variable, or defaults to `../agentdojo`
    (a folder named `agentdojo` next to this repository). No absolute path is stored in the repository.

Outputs:
    Library calls return file text, parsed traces, hashes and file lists.
    The `manifest` command writes src/data/agentdojo_source_manifest.json (deterministic; no local paths).

Dependencies:
    Python standard library and the `git` command. The clone is never modified: only the git sub-commands
    in READ_ONLY_GIT_COMMANDS can be run, and no AgentDojo code is imported or executed. This module does
    not depend on the other project scripts.

Usage:
    python3 -B src/scripts/agentdojo_source.py list [PREFIX]
    python3 -B src/scripts/agentdojo_source.py show NAME_OR_PATH
    python3 -B src/scripts/agentdojo_source.py trace USER_TASK ATTACK [INJECTION_TASK]
    python3 -B src/scripts/agentdojo_source.py manifest [--out PATH]
    AGENTDOJO_REPO=/path/to/clone python3 -B src/scripts/agentdojo_source.py list

    From Python:
        import agentdojo_source as src
        text = src.read_source("workspace_user_tasks")
        run = src.read_trace("user_task_21", "important_instructions", "injection_task_0")
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

REPO_URL = "https://github.com/ethz-spylab/agentdojo"
PINNED_COMMIT = "af7266303c748a9cf0442d71ceabc9d2488d7188"  # "Update GPT-4o results", 2024-06-05
CLONE_ENV_VAR = "AGENTDOJO_REPO"
DEFAULT_CLONE = REPO_ROOT.parent / "agentdojo"
DEFAULT_MANIFEST = REPO_ROOT / "src" / "data" / "agentdojo_source_manifest.json"

# At this commit the Python package is still called function_calling_pi.
_PACKAGE = "src/function_calling_pi"
_WORKSPACE_SUITE = f"{_PACKAGE}/task_suites/workspace"
_WORKSPACE_DATA = f"{_PACKAGE}/data/suites/workspace"

# Path of the recorded runs inside the clone (the folder the vendored copies in src/data/ come from).
TRACES_ROOT = "runs/gpt-4o-2024-05-13/workspace"

# Short names for the source files used in scenario analysis -> path inside the repository at the pinned commit.
SOURCE_FILES = {
    # Task definitions and their success checks
    "workspace_user_tasks": f"{_WORKSPACE_SUITE}/user_tasks.py",
    "workspace_injection_tasks": f"{_WORKSPACE_SUITE}/injection_tasks.py",
    "workspace_task_suite": f"{_WORKSPACE_SUITE}/task_suite.py",
    "task_suite": f"{_PACKAGE}/task_suites/task_suite.py",
    "base_tasks": f"{_PACKAGE}/base_tasks.py",
    "task_combinators": f"{_PACKAGE}/task_suites/task_combinators.py",
    # Attack implementations
    "attack_base": f"{_PACKAGE}/attacks/base_attack.py",
    "attack_baseline": f"{_PACKAGE}/attacks/baseline_attacks.py",
    "attack_factory": f"{_PACKAGE}/attacks/make_attacker.py",
    # Injection entry points and the simulated environment
    "workspace_injection_vectors": f"{_WORKSPACE_DATA}/injection_vectors.yaml",
    "workspace_environment": f"{_WORKSPACE_DATA}/environment.yaml",
    "workspace_calendar_data": f"{_WORKSPACE_DATA}/include/calendar.yaml",
    "workspace_cloud_drive_data": f"{_WORKSPACE_DATA}/include/cloud_drive.yaml",
    "workspace_inbox_data": f"{_WORKSPACE_DATA}/include/inbox.yaml",
    # Tool implementations the agent called
    "tool_calendar_client": f"{_PACKAGE}/tools/calendar_client.py",
    "tool_cloud_drive_client": f"{_PACKAGE}/tools/cloud_drive_client.py",
    "tool_email_client": f"{_PACKAGE}/tools/email_client.py",
    "tool_types": f"{_PACKAGE}/tools/types.py",
}

# The only git sub-commands this module will run. All of them only read; nothing here can change the clone.
READ_ONLY_GIT_COMMANDS = frozenset({"cat-file", "ls-tree", "rev-parse", "show"})


class AgentDojoSourceError(RuntimeError):
    """The AgentDojo clone or a file inside it could not be used (message says how to fix it)."""


def clone_path() -> Path:
    """Return the AgentDojo clone location: $AGENTDOJO_REPO if set, otherwise `../agentdojo`.

    Raises:
        AgentDojoSourceError: if the folder is missing or is not a git repository.
    """
    path = Path(os.environ.get(CLONE_ENV_VAR) or DEFAULT_CLONE).expanduser().resolve()
    if not (path / ".git").exists():
        raise AgentDojoSourceError(
            f"No git repository at {path}. Clone {REPO_URL} next to this repository, "
            f"or set {CLONE_ENV_VAR} to the clone's location."
        )
    return path


def _git(*args: str) -> bytes:
    """Run one read-only git command in the clone and return its raw output.

    Raises:
        AgentDojoSourceError: for a sub-command outside READ_ONLY_GIT_COMMANDS (a safeguard against
            ever modifying the clone), or when git fails (for example, the path does not exist).
    """
    if not args or args[0] not in READ_ONLY_GIT_COMMANDS:
        raise AgentDojoSourceError(f"git {args[:1]} is not allowed; only {sorted(READ_ONLY_GIT_COMMANDS)}")
    result = subprocess.run(["git", "-C", str(clone_path()), *args], capture_output=True)
    if result.returncode != 0:
        raise AgentDojoSourceError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def check_pinned_commit() -> None:
    """Confirm the clone contains the pinned commit.

    Raises:
        AgentDojoSourceError: with instructions if the commit is missing (e.g. a shallow or outdated clone).
    """
    try:
        kind = _git("cat-file", "-t", PINNED_COMMIT).decode().strip()
    except AgentDojoSourceError:
        kind = ""
    if kind != "commit":
        raise AgentDojoSourceError(
            f"Commit {PINNED_COMMIT} is not in the clone at {clone_path()}. "
            "Run `git fetch --all` there (the clone must not be shallow)."
        )


def resolve_path(name_or_path: str) -> str:
    """Turn a short name from SOURCE_FILES into its repository path; any other string is used as a path."""
    return SOURCE_FILES.get(name_or_path, name_or_path)


def read_source_bytes(name_or_path: str) -> bytes:
    """Return the exact bytes of a file as it was at the pinned commit (read from git, not the working tree)."""
    check_pinned_commit()
    return _git("cat-file", "blob", f"{PINNED_COMMIT}:{resolve_path(name_or_path)}")


def read_source(name_or_path: str) -> str:
    """Return the complete text of a file at the pinned commit.

    Args:
        name_or_path: a key of SOURCE_FILES (e.g. "workspace_user_tasks") or a repository path such as
            "src/function_calling_pi/tools/web.py". Any file in the repository can be read.

    Raises:
        AgentDojoSourceError: if the clone or commit is unusable, or the path does not exist at the commit.
    """
    return read_source_bytes(name_or_path).decode("utf-8")


def blob_hash(name_or_path: str) -> str:
    """Return the git blob hash of a file at the pinned commit (the id git stores the file under)."""
    check_pinned_commit()
    return _git("rev-parse", f"{PINNED_COMMIT}:{resolve_path(name_or_path)}").decode().strip()


def list_sources(prefix: str = "") -> list[str]:
    """Return every file path in the repository at the pinned commit that starts with `prefix`, sorted."""
    check_pinned_commit()
    names = _git("ls-tree", "-r", "--name-only", PINNED_COMMIT).decode().splitlines()
    return sorted(n for n in names if n.startswith(prefix))


def trace_path(user_task_id: str, attack: str = "none", injection_task_id: str | None = None) -> str:
    """Return the repository path of one recorded run (the same naming as the vendored copies).

    Args:
        user_task_id: e.g. "user_task_21".
        attack: attack folder name ("none", "important_instructions", "tool_knowledge", "direct", ...).
        injection_task_id: required for any attack other than "none", e.g. "injection_task_0".
    """
    if attack == "none":
        filename = "none.json"
    elif injection_task_id is None:
        raise ValueError(f"attack {attack!r} needs an injection_task_id")
    else:
        filename = f"{injection_task_id}.json"
    return f"{TRACES_ROOT}/{user_task_id}/{attack}/{filename}"


def read_trace(user_task_id: str, attack: str = "none", injection_task_id: str | None = None) -> dict:
    """Return one recorded run, parsed, from the clone at the pinned commit.

    Unlike agentdojo_runs.load_run, which reads the copies vendored in src/data/, this can reach every run
    in the clone, including the attacks the project does not keep locally (e.g. "direct").
    """
    return json.loads(read_source(trace_path(user_task_id, attack, injection_task_id)))


def build_manifest() -> dict:
    """Describe every named source file and the trace folder, with hashes, for reproducibility.

    The result contains no local paths or timestamps of our own, so it is identical for everyone who has
    the pinned commit, and any difference means a file changed.
    """
    check_pinned_commit()
    files = []
    for name in sorted(SOURCE_FILES):
        data = read_source_bytes(name)
        files.append({
            "name": name,
            "path": SOURCE_FILES[name],
            "git_blob": blob_hash(name),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "lines": data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1),
        })

    trace_files = list_sources(TRACES_ROOT + "/")
    by_attack = Counter(p[len(TRACES_ROOT) + 1:].split("/")[1] for p in trace_files)
    info = _git("show", "-s", "--format=%H%n%cI%n%s", PINNED_COMMIT).decode().splitlines()
    return {
        "source": {
            "repo": REPO_URL,
            "commit": info[0],
            "committed": info[1],
            "subject": info[2],
            "access": "Read with `git cat-file blob <commit>:<path>` from a local clone; no file is copied into this repository.",
        },
        "traces": {
            "path": TRACES_ROOT,
            "git_tree": blob_hash(TRACES_ROOT),  # one hash that identifies every trace file below
            "file_count": len(trace_files),
            "files_by_attack": dict(sorted(by_attack.items())),
        },
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read AgentDojo files and traces at the pinned commit (read-only).")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="list repository paths at the pinned commit; with no PREFIX, list the named files")
    p_list.add_argument("prefix", nargs="?", default=None)
    p_show = sub.add_parser("show", help="print a file (short name from SOURCE_FILES, or a repository path)")
    p_show.add_argument("name_or_path")
    p_trace = sub.add_parser("trace", help="print one recorded run as JSON")
    p_trace.add_argument("user_task")
    p_trace.add_argument("attack")
    p_trace.add_argument("injection_task", nargs="?", default=None)
    p_manifest = sub.add_parser("manifest", help="write the source manifest")
    p_manifest.add_argument("--out", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()

    try:
        if args.command == "list":
            if args.prefix is None:
                for name in sorted(SOURCE_FILES):
                    print(f"{name:30} {blob_hash(name)[:12]}  {SOURCE_FILES[name]}")
            else:
                print("\n".join(list_sources(args.prefix)))
        elif args.command == "show":
            sys.stdout.write(read_source(args.name_or_path))
        elif args.command == "trace":
            print(json.dumps(read_trace(args.user_task, args.attack, args.injection_task), indent=2, ensure_ascii=False))
        else:
            manifest = build_manifest()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(manifest, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Wrote manifest of {len(manifest['files'])} source files and "
                  f"{manifest['traces']['file_count']} trace files to {args.out}")
    except AgentDojoSourceError as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
