"""
Purpose:
    Create the Python environment in which AgentDojo runs, reproducibly and without touching the AgentDojo clone. The
    pinned AgentDojo commit (still called `function_calling_pi`) is exported from the clone with `git archive`
    (read-only) into a temporary folder, installed into a new virtual environment without its own dependency list
    (which asks for torch and Vertex AI), and the packages it really needs are installed at the versions in
    src/requirements_execution.txt, which are the versions the harness was verified with.

Inputs:
    A local AgentDojo clone containing the pinned commit (AGENTDOJO_REPO or ../agentdojo), python3.12 or newer on
    the PATH, network access to PyPI, src/requirements_execution.txt.

Outputs:
    The virtual environment (default .venv-agentdojo/, ignored by git). Nothing in the clone or in tracked files.

Dependencies:
    agentdojo_source.py (pinned commit, clone location). Standard library only; calls git and pip.

Usage:
    python3 -B src/scripts/setup_execution_env.py [--venv PATH] [--python python3.12]
    Then check it: .venv-agentdojo/bin/python -B src/scripts/run_executions.py --ground-truth-check user_task_1
"""

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import agentdojo_source as src

DEFAULT_VENV = src.REPO_ROOT / ".venv-agentdojo"
REQUIREMENTS = src.REPO_ROOT / "src" / "requirements_execution.txt"


def archive_commands(clone: Path, commit: str, destination: Path) -> list[list[str]]:
    """The commands that export the commit's files (read-only git) and unpack them into destination."""
    return [["git", "-C", str(clone), "archive", "--format=tar", "-o", str(destination / "agentdojo.tar"), commit]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the AgentDojo execution environment.")
    parser.add_argument("--venv", type=Path, default=DEFAULT_VENV)
    parser.add_argument("--python", default="python3.12")
    args = parser.parse_args()
    clone = src.clone_path()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for command in archive_commands(clone, src.PINNED_COMMIT, tmp_path):
            subprocess.run(command, check=True)
        source_dir = tmp_path / "agentdojo-src"
        with tarfile.open(tmp_path / "agentdojo.tar") as archive:
            archive.extractall(source_dir, filter="data")
        subprocess.run([args.python, "-m", "venv", str(args.venv)], check=True)
        pip = [str(args.venv / "bin" / "python"), "-m", "pip", "install", "--quiet"]
        subprocess.run([*pip, "--no-deps", str(source_dir)], check=True)
        subprocess.run([*pip, "-r", str(REQUIREMENTS)], check=True)
    print(f"Environment ready: {args.venv}")


if __name__ == "__main__":
    main()
