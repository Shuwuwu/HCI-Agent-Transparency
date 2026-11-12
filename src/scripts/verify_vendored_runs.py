"""
Purpose:
    Prove that the AgentDojo run files kept in this repository are byte-for-byte identical to the
    originals at the pinned AgentDojo commit. This is a one-off integrity check, not part of the
    data-processing pipeline; the other scripts never need the network.

Inputs:
    - The local run files under src/data/gpt-4o-2024-05-13/workspace/ (only the folders that git tracks:
      none/, important_instructions/, tool_knowledge/).
    - GitHub's tree API for ethz-spylab/agentdojo at the pinned commit (needs network access).
    - The `git` command, used to compute each local file's blob hash.

Outputs:
    A summary printed to stdout (files checked, matches, mismatches, missing). Exit code 0 only when
    every checked file matches; otherwise 1.

Dependencies:
    Python standard library only, plus `git` on the PATH. Does not import other project scripts.

Usage:
    python3 src/scripts/verify_vendored_runs.py
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

AGENTDOJO_REPO = "ethz-spylab/agentdojo"
AGENTDOJO_COMMIT = "af7266303c748a9cf0442d71ceabc9d2488d7188"
REPO_ROOT = Path(__file__).resolve().parents[2]
LOCAL_RUNS_DIR = REPO_ROOT / "src" / "data" / "gpt-4o-2024-05-13" / "workspace"
# Where the same files live inside the AgentDojo repository.
REMOTE_PREFIX = "runs/gpt-4o-2024-05-13/workspace"
# Only these attack folders are kept (the rest are git-ignored, see the root .gitignore).
KEPT_FOLDERS = ("none", "important_instructions", "tool_knowledge")


def remote_blob_hashes() -> dict[str, str]:
    """Return {path: blob hash} for every file in AgentDojo at the pinned commit.

    Uses the recursive tree API. GitHub marks the answer `truncated` when the tree is too large,
    in which case the comparison would be silently incomplete, so that is treated as an error.
    """
    url = f"https://api.github.com/repos/{AGENTDOJO_REPO}/git/trees/{AGENTDOJO_COMMIT}?recursive=1"
    with urllib.request.urlopen(url, timeout=60) as resp:
        tree = json.load(resp)
    if tree.get("truncated"):
        raise RuntimeError("GitHub returned a truncated tree; cannot verify completely")
    return {entry["path"]: entry["sha"] for entry in tree["tree"] if entry["type"] == "blob"}


def local_blob_hash(path: Path) -> str:
    """Return the git blob hash of a local file, computed by `git hash-object`."""
    result = subprocess.run(
        ["git", "hash-object", str(path)], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def kept_local_files() -> list[Path]:
    """List the local run files that this project tracks, sorted for stable output."""
    files = []
    for folder in KEPT_FOLDERS:
        files.extend(LOCAL_RUNS_DIR.glob(f"user_task_*/{folder}/*.json"))
    return sorted(files)


def main() -> int:
    remote = remote_blob_hashes()
    matches, mismatches, missing = 0, [], []
    files = kept_local_files()
    for path in files:
        remote_path = f"{REMOTE_PREFIX}/{path.relative_to(LOCAL_RUNS_DIR).as_posix()}"
        if remote_path not in remote:
            missing.append(remote_path)
        elif remote[remote_path] != local_blob_hash(path):
            mismatches.append(remote_path)
        else:
            matches += 1

    print(f"Checked {len(files)} files against AgentDojo commit {AGENTDOJO_COMMIT[:10]}")
    print(f"  matching:   {matches}")
    print(f"  mismatched: {len(mismatches)}")
    print(f"  not in AgentDojo at that commit: {len(missing)}")
    for p in mismatches + missing:
        print(f"    {p}")
    return 0 if files and not mismatches and not missing else 1


if __name__ == "__main__":
    sys.exit(main())
