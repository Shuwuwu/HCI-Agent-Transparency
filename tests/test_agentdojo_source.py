"""
Purpose:
    Check that agentdojo_source.py gives reliable, read-only access to AgentDojo's original source files and
    recorded traces at the pinned commit. Expected hashes and counts are hard-coded from the repository itself,
    so a wrong commit, a corrupted clone or a parsing change makes a test fail.

Inputs:
    A local AgentDojo clone containing the pinned commit (AGENTDOJO_REPO, or ../agentdojo), the vendored run
    files in src/data/gpt-4o-2024-05-13/, and the committed src/data/agentdojo_source_manifest.json.
    Tests that need the clone are skipped, with a message, if it is not available; the others always run.

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/agentdojo_source.py and src/scripts/agentdojo_runs.py. Uses the git command (read-only calls).

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as src  # noqa: E402

# Git blob hashes of key files at the pinned commit (taken from the repository when the module was written).
PINNED_BLOBS = {
    "workspace_user_tasks": "020fca14cd785567454c0302bbd962f31a7189ae",
    "workspace_injection_tasks": "8ca9fef08a2a827ffdda2d95998b46e878042dbc",
    "workspace_task_suite": "b3943129dc70adfff5769d1e778c9f3f618d12c9",
    "attack_baseline": "1d0c53f6a7dde4f10cc81c6c58834ffb6a05da8d",
    "workspace_injection_vectors": "e1212b54134e63edbb6dcc73e184fc357a47f717",
    "workspace_environment": "8cce08c9b5242920aec9a676a839fc2dda0f472b",
}
PINNED_TRACE_TREE = "64896f795b9a5e515804792954aa5a2ff4d3f026"
TRACE_FILE_COUNT = 2646


def git_in_clone(*args, env_extra=None):
    """Run git in the clone for the tests' own checks (never a command that writes)."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env_extra or {})}  # no opportunistic index refresh
    out = subprocess.run(["git", "-C", str(src.clone_path()), *args], capture_output=True, check=True, env=env)
    return out.stdout


def clone_available() -> str | None:
    """Return None if the clone and pinned commit are usable, otherwise the reason they are not."""
    try:
        src.check_pinned_commit()
    except src.AgentDojoSourceError as error:
        return str(error)
    return None


class ConfigurationTest(unittest.TestCase):
    """Behaviour that does not need the clone."""

    def test_default_clone_is_sibling_folder_not_an_absolute_literal(self):
        self.assertEqual(src.DEFAULT_CLONE, REPO_ROOT.parent / "agentdojo")
        self.assertNotIn("/Users/", Path(src.__file__).read_text(encoding="utf-8"))

    def test_environment_variable_overrides_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            with mock.patch.dict(os.environ, {src.CLONE_ENV_VAR: tmp}):
                self.assertEqual(src.clone_path(), Path(tmp).resolve())

    def test_missing_clone_gives_a_helpful_error(self):
        with tempfile.TemporaryDirectory() as tmp:  # a folder that is not a git repository
            with mock.patch.dict(os.environ, {src.CLONE_ENV_VAR: tmp}):
                with self.assertRaises(src.AgentDojoSourceError) as caught:
                    src.clone_path()
        self.assertIn(src.CLONE_ENV_VAR, str(caught.exception))

    def test_only_read_only_git_commands_are_allowed(self):
        for command in ("checkout", "fetch", "reset", "clean", "stash", "config", "commit", "pull", "gc"):
            with self.assertRaises(src.AgentDojoSourceError, msg=command):
                src._git(command)
        self.assertEqual(src.READ_ONLY_GIT_COMMANDS, {"cat-file", "ls-tree", "rev-parse", "show"})

    def test_names_resolve_and_unknown_strings_are_treated_as_paths(self):
        self.assertTrue(src.resolve_path("workspace_user_tasks").endswith("task_suites/workspace/user_tasks.py"))
        self.assertEqual(src.resolve_path("src/function_calling_pi/tools/web.py"), "src/function_calling_pi/tools/web.py")

    def test_trace_path_matches_vendored_layout(self):
        self.assertEqual(src.trace_path("user_task_3"), "runs/gpt-4o-2024-05-13/workspace/user_task_3/none/none.json")
        self.assertEqual(
            src.trace_path("user_task_3", "tool_knowledge", "injection_task_5"),
            "runs/gpt-4o-2024-05-13/workspace/user_task_3/tool_knowledge/injection_task_5.json",
        )
        with self.assertRaises(ValueError):
            src.trace_path("user_task_3", "tool_knowledge")

    def test_committed_manifest_has_no_local_paths(self):
        text = src.DEFAULT_MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotIn(str(REPO_ROOT), text)


class SourceAccessTest(unittest.TestCase):
    """Reads of the original files; needs the clone."""

    @classmethod
    def setUpClass(cls):
        reason = clone_available()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    def test_pinned_commit_is_present(self):
        self.assertEqual(git_in_clone("rev-parse", f"{src.PINNED_COMMIT}^{{commit}}").decode().strip(), src.PINNED_COMMIT)

    def test_key_files_have_the_expected_blob_hashes(self):
        for name, expected in PINNED_BLOBS.items():
            self.assertEqual(src.blob_hash(name), expected, name)

    def test_returned_bytes_hash_back_to_the_blob_id(self):
        """The bytes we return are the stored file: git's own hashing of them reproduces the blob id."""
        for name in src.SOURCE_FILES:
            data = src.read_source_bytes(name)
            blob_id = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True)
            self.assertEqual(blob_id.stdout.decode().strip(), src.blob_hash(name), name)

    def test_files_are_complete(self):
        tasks = src.read_source("workspace_user_tasks")
        self.assertEqual(len(tasks.splitlines()), 1556)
        # 33 hand-written user tasks + 7 combined tasks = the 40 user tasks.
        self.assertEqual(len(re.findall(r"^class UserTask\d+\(", tasks, re.M)), 33)
        self.assertEqual(tasks.count("task_combinator.create_combined_task("), 7)
        injections = src.read_source("workspace_injection_tasks")
        self.assertEqual(len(re.findall(r"^class InjectionTask\d+\(", injections, re.M)), 6)
        self.assertEqual(len(src.read_source("workspace_injection_vectors").splitlines()), 51)

    def test_every_named_file_exists_and_is_readable_text(self):
        for name in src.SOURCE_FILES:
            self.assertTrue(src.read_source(name).strip(), name)

    def test_read_comes_from_git_not_the_working_tree(self):
        working_tree_copy = src.clone_path() / src.SOURCE_FILES["workspace_user_tasks"]
        if working_tree_copy.exists():
            self.skipTest("clone has the pinned layout checked out, so this check cannot tell the two apart")
        # The file is absent from the checked-out files (the clone is on a later layout) yet readable here.
        self.assertIn("class UserTask0", src.read_source("workspace_user_tasks"))

    def test_any_repository_path_can_be_listed_and_read(self):
        attacks = src.list_sources("src/function_calling_pi/attacks/")
        self.assertIn("src/function_calling_pi/attacks/baseline_attacks.py", attacks)
        self.assertEqual(attacks, sorted(attacks))
        self.assertIn("web", src.read_source("src/function_calling_pi/tools/web.py").lower())

    def test_missing_path_raises(self):
        with self.assertRaises(src.AgentDojoSourceError):
            src.read_source("src/function_calling_pi/does_not_exist.py")

    def test_attack_templates_are_the_ones_used_for_our_two_attacks(self):
        text = src.read_source("attack_baseline")
        self.assertIn("class ImportantInstructionsAttacker", text)
        self.assertIn("class ToolKnowledgeAttacker", text)
        self.assertIn("This is an important message from me, {user}, to you, {model}.", text)


class ConsistencyWithHandCopiedTablesTest(unittest.TestCase):
    """The tables copied by hand into agentdojo_runs.py must agree with the original source files."""

    @classmethod
    def setUpClass(cls):
        reason = clone_available()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    @staticmethod
    def _eval_fstring(node, constants):
        """Evaluate a GOAL f-string made of literal text and class-level constants."""
        if isinstance(node, ast.Constant):
            return node.value
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(piece.value)
            else:  # FormattedValue naming a class constant such as _RECIPIENT
                parts.append(constants[piece.value.id])
        return "".join(parts)

    def _goals_from_source(self):
        tree = ast.parse(src.read_source("workspace_injection_tasks"))
        goals = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.startswith("InjectionTask"):
                constants = {
                    stmt.targets[0].id: stmt.value.value
                    for stmt in node.body
                    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
                }
                goal = next(s for s in node.body if isinstance(s, ast.Assign) and s.targets[0].id == "GOAL")
                goals[f"injection_task_{node.name[len('InjectionTask'):]}"] = (
                    self._eval_fstring(goal.value, constants), constants,
                )
        return goals

    def test_injection_goals_match_injection_tasks_py(self):
        goals = self._goals_from_source()
        self.assertEqual({k: v[0] for k, v in goals.items()}, runs.INJECTION_GOALS)

    def test_attacker_address_and_file_id_match_injection_tasks_py(self):
        goals = self._goals_from_source()
        # Goals 0, 2, 3, 4, 5 target the attacker's address; goal 1 deletes a file and has no address.
        for injection_id, (_, constants) in goals.items():
            recipient = constants.get("_RECIPIENT") or constants.get("_PARTICIPANT")
            expected = None if injection_id == "injection_task_1" else runs.ATTACKER_EMAIL
            self.assertEqual(recipient, expected, injection_id)
        self.assertEqual(goals["injection_task_1"][1]["_FILE_ID"], runs.UNAUTHORIZED_ACTION["injection_task_1"][2])

    def test_tool_groups_match_task_suite_py(self):
        text = src.read_source("workspace_task_suite")
        block = text[text.index("TOOLS = ["):]
        block = block[:block.index("\n]")]
        heading, found = None, {}
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("#"):
                heading = {"# Email tools": "email_tools", "# Calendar tools": "calendar_tools", "# Drive tools": "drive_tools"}[line]
            elif ".__name__" in line:
                found[line.split(".__name__")[0]] = heading
        self.assertEqual(found, runs.TOOL_CATEGORY)

    def test_combined_task_prefix_matches_task_combinators_py(self):
        self.assertIn(runs.COMBINED_PREFIX, src.read_source("task_combinators"))


class TraceAccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_available()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    def test_trace_folder_is_the_expected_one(self):
        self.assertEqual(src.blob_hash(src.TRACES_ROOT), PINNED_TRACE_TREE)
        self.assertEqual(len(src.list_sources(src.TRACES_ROOT + "/")), TRACE_FILE_COUNT)

    def test_all_520_vendored_traces_equal_the_clones(self):
        """Every tracked run file in src/data/ has the same bytes as the clone's file at the pinned commit."""
        tracked = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "src/data/gpt-4o-2024-05-13/workspace"],
            capture_output=True, check=True, text=True,
        ).stdout.split()
        tracked = [p for p in tracked if p.endswith(".json")]
        self.assertEqual(len(tracked), 520)
        local = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "hash-object", "--stdin-paths"],
            input="\n".join(tracked), capture_output=True, check=True, text=True,
        ).stdout.split()
        for path, local_hash in zip(tracked, local):
            clone_path = f"{src.TRACES_ROOT}/{path.split('/workspace/', 1)[1]}"
            self.assertEqual(src.blob_hash(clone_path), local_hash, path)

    def test_read_trace_matches_the_vendored_loader(self):
        for task, attack, injection in [
            ("user_task_0", "none", None),
            ("user_task_21", "important_instructions", "injection_task_0"),
            ("user_task_39", "tool_knowledge", "injection_task_5"),
        ]:
            self.assertEqual(src.read_trace(task, attack, injection), runs.load_run(task, attack, injection))

    def test_traces_the_project_does_not_vendor_are_reachable(self):
        run = src.read_trace("user_task_0", "direct", "injection_task_0")
        self.assertEqual(run["attack_type"], "direct")
        self.assertEqual(run["injection_task_id"], "injection_task_0")

    def test_trace_counts_by_attack(self):
        counts = src.build_manifest()["traces"]["files_by_attack"]
        self.assertEqual(counts["important_instructions"], 240)
        self.assertEqual(counts["tool_knowledge"], 240)
        self.assertEqual(counts["none"], 46)  # 40 user tasks + 6 injection tasks run on their own
        self.assertEqual(sum(counts.values()), TRACE_FILE_COUNT)


class ManifestAndSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = clone_available()
        if reason:
            raise unittest.SkipTest(f"AgentDojo clone not available: {reason}")

    def test_manifest_contents(self):
        manifest = src.build_manifest()
        self.assertEqual(manifest["source"]["commit"], src.PINNED_COMMIT)
        self.assertEqual(manifest["source"]["subject"], "Update GPT-4o results")
        self.assertEqual([f["name"] for f in manifest["files"]], sorted(src.SOURCE_FILES))
        by_name = {f["name"]: f for f in manifest["files"]}
        for name, blob in PINNED_BLOBS.items():
            self.assertEqual(by_name[name]["git_blob"], blob)
        entry = by_name["workspace_user_tasks"]
        self.assertEqual(entry["sha256"], hashlib.sha256(src.read_source_bytes("workspace_user_tasks")).hexdigest())
        self.assertEqual((entry["bytes"], entry["lines"]), (54371, 1556))

    def test_manifest_is_deterministic(self):
        self.assertEqual(src.build_manifest(), src.build_manifest())

    def test_committed_manifest_is_up_to_date(self):
        committed = json.loads(src.DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(committed, src.build_manifest())

    def test_clone_is_not_modified_by_any_operation(self):
        """HEAD, branch, working-tree status and the index file are identical before and after using the module."""

        def snapshot():
            index = src.clone_path() / ".git" / "index"
            return (
                git_in_clone("rev-parse", "HEAD"),
                git_in_clone("rev-parse", "--abbrev-ref", "HEAD"),
                git_in_clone("status", "--porcelain"),
                hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None,
            )

        before = snapshot()
        src.build_manifest()
        src.list_sources("src/")
        src.read_trace("user_task_0", "none")
        for name in src.SOURCE_FILES:
            src.read_source(name)
        self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
