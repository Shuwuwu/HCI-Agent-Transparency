"""
Purpose:
    Check that build_experiment_stimuli.py produces the right stimuli folders: one per safe-candidate task,
    with metadata, a faithful no-attack trace, the list of capability skills the task uses (from the skill
    taxonomy), an empty placeholder for the action log still to be designed, and a re-run that never overwrites
    work done in that placeholder.

Inputs:
    The committed src/data/experiment/ folder, src/data/gpt4o_workspace_scenarios.json,
    src/data/skill_taxonomy.json, the vendored run files, and (optionally) the AgentDojo clone for a hash comparison with AgentDojo's own files.

Outputs:
    Pass/fail results from unittest.

Dependencies:
    src/scripts/build_experiment_stimuli.py, agentdojo_runs.py, agentdojo_source.py.

Usage:
    python3 -B -m unittest discover -s tests -v
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "scripts"))

import agentdojo_runs as runs  # noqa: E402
import agentdojo_source as source  # noqa: E402
import build_experiment_stimuli as stimuli  # noqa: E402

# Tasks whose no-attack run failed the task (utility false); they are not safe candidates, so no folder.
UTILITY_FALSE = {7, 11, 14, 18, 19, 20, 25, 30, 31, 32, 33, 34, 36, 37, 38}
SAFE_TASKS = sorted(set(range(40)) - UTILITY_FALSE)
DERIVED_FILENAMES = (stimuli.METADATA_FILENAME, stimuli.TRACE_FILENAME, stimuli.SKILLS_FILENAME)
EXPECTED_FILES = {*DERIVED_FILENAMES, *stimuli.PLACEHOLDER_FILENAMES}
TAXONOMY = json.loads(stimuli.DEFAULT_TAXONOMY.read_text(encoding="utf-8"))


def folder_for(number: int, root: Path = stimuli.DEFAULT_OUT_DIR) -> Path:
    return root / f"user_task_{number}"


def derived_files(root: Path) -> dict:
    """Map 'task/file' -> bytes for the files the script derives (metadata, trace, skills) under `root`."""
    return {
        f"{d.name}/{name}": (d / name).read_bytes()
        for d in sorted(root.glob("user_task_*"))
        for name in DERIVED_FILENAMES
    }


class CommittedStimuliTest(unittest.TestCase):
    def test_one_folder_per_safe_candidate_task(self):
        found = sorted(runs.task_number(p.name) for p in stimuli.DEFAULT_OUT_DIR.glob("user_task_*"))
        self.assertEqual(found, SAFE_TASKS)
        self.assertEqual(len(found), 25)

    def test_tasks_gpt4o_failed_have_no_folder(self):
        for number in UTILITY_FALSE:
            self.assertFalse(folder_for(number).exists(), number)

    def test_every_folder_has_exactly_the_four_files(self):
        for number in SAFE_TASKS:
            names = {p.name for p in folder_for(number).iterdir()}
            self.assertEqual(names, EXPECTED_FILES, number)

    def test_metadata_is_the_scenario_entry_unchanged(self):
        scenarios = {
            s["user_task_id"]: s
            for s in json.loads(stimuli.DEFAULT_SCENARIOS.read_text(encoding="utf-8"))["User Task Scenarios"]
        }
        for number in SAFE_TASKS:
            metadata = json.loads((folder_for(number) / stimuli.METADATA_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(metadata, scenarios[f"user_task_{number}"], number)
            self.assertTrue(metadata["safe_candidate"])
            self.assertTrue(metadata["utility"])
            self.assertIsNone(metadata["error"])

    def test_trace_is_byte_identical_to_the_recorded_run(self):
        for number in SAFE_TASKS:
            original = runs.RUNS_DIR / f"user_task_{number}" / "none" / "none.json"
            self.assertEqual((folder_for(number) / stimuli.TRACE_FILENAME).read_bytes(), original.read_bytes(), number)

    def test_trace_matches_its_metadata_and_is_a_clean_run(self):
        for number in SAFE_TASKS:
            folder = folder_for(number)
            metadata = json.loads((folder / stimuli.METADATA_FILENAME).read_text(encoding="utf-8"))
            trace = json.loads((folder / stimuli.TRACE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(trace["user_task_id"], metadata["user_task_id"])
            self.assertEqual(runs.user_prompt(trace), metadata["task_prompt"])
            self.assertTrue(trace["utility"])
            self.assertIsNone(trace["error"])
            self.assertEqual(trace["injections"], {})
            self.assertIsNone(trace["attack_type"])
            self.assertEqual(len(runs.tool_calls(trace)), metadata["n_tool_calls"])

    def test_no_injection_text_in_any_trace(self):
        """The stimuli must be free of the attack: no injected block and no attacker address anywhere."""
        for number in SAFE_TASKS:
            text = (folder_for(number) / stimuli.TRACE_FILENAME).read_text(encoding="utf-8")
            self.assertNotIn("<INFORMATION>", text, number)
            self.assertNotIn(runs.ATTACKER_EMAIL, text, number)

    def test_skills_json_lists_the_skills_of_the_scenario_map(self):
        for number in SAFE_TASKS:
            skills = json.loads((folder_for(number) / stimuli.SKILLS_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(skills, {"taxonomy_version": TAXONOMY["version"],
                                      "skills": TAXONOMY["scenario_map"][f"user_task_{number}"]["skills"]}, number)
            self.assertTrue(skills["skills"], number)

    def test_the_skills_are_not_copied_into_the_task_folders(self):
        for number in SAFE_TASKS:
            self.assertEqual(list(folder_for(number).glob("SKILL*")), [], number)

    def test_placeholders_are_still_empty(self):
        """Fails once a placeholder is filled in, as a reminder to replace this test with real content checks."""
        for number in SAFE_TASKS:
            for name in stimuli.PLACEHOLDER_FILENAMES:
                self.assertEqual((folder_for(number) / name).stat().st_size, 0, f"{number}/{name}")

    def test_committed_derived_files_are_up_to_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            stimuli.build_all(out_dir=Path(tmp))
            self.assertEqual(derived_files(Path(tmp)), derived_files(stimuli.DEFAULT_OUT_DIR))

    def test_traces_equal_agentdojos_own_files_at_the_pinned_commit(self):
        try:
            source.check_pinned_commit()
        except source.AgentDojoSourceError as error:
            self.skipTest(f"AgentDojo clone not available: {error}")
        paths = [folder_for(n) / stimuli.TRACE_FILENAME for n in SAFE_TASKS]
        hashes = subprocess.run(
            ["git", "hash-object", "--stdin-paths"], input="\n".join(map(str, paths)),
            capture_output=True, check=True, text=True,
        ).stdout.split()
        for number, our_hash in zip(SAFE_TASKS, hashes):
            self.assertEqual(source.blob_hash(source.trace_path(f"user_task_{number}")), our_hash, number)


class BuilderBehaviourTest(unittest.TestCase):
    def test_rerun_creates_nothing_new_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _, created_first = stimuli.build_all(out_dir=out)
            before = derived_files(out)
            _, created_second = stimuli.build_all(out_dir=out)
            self.assertEqual(len(created_first), 25)
            self.assertEqual(created_second, [])
            self.assertEqual(derived_files(out), before)

    def test_rerun_never_overwrites_filled_placeholders(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stimuli.build_all(out_dir=out)
            filled = {"action_log.json": '[{"step": 1}]'}
            for name, text in filled.items():
                (folder_for(0, out) / name).write_text(text, encoding="utf-8")
            stimuli.build_all(out_dir=out)
            for name, text in filled.items():
                self.assertEqual((folder_for(0, out) / name).read_text(encoding="utf-8"), text, name)

    def test_rerun_repairs_edited_derived_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            stimuli.build_all(out_dir=out)
            (folder_for(0, out) / stimuli.METADATA_FILENAME).write_text("tampered", encoding="utf-8")
            (folder_for(0, out) / stimuli.TRACE_FILENAME).write_text("tampered", encoding="utf-8")
            (folder_for(0, out) / stimuli.SKILLS_FILENAME).write_text("tampered", encoding="utf-8")
            stimuli.build_all(out_dir=out)
            self.assertEqual(derived_files(out), derived_files(stimuli.DEFAULT_OUT_DIR))

    def test_only_safe_candidates_are_selected(self):
        scenarios = stimuli.safe_scenarios(stimuli.DEFAULT_SCENARIOS)
        self.assertEqual([runs.task_number(s["user_task_id"]) for s in scenarios], SAFE_TASKS)

    def test_a_task_missing_from_the_scenario_map_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = json.loads(json.dumps(TAXONOMY))
            del broken["scenario_map"]["user_task_24"]
            path = Path(tmp) / "taxonomy.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValueError):
                stimuli.build_all(out_dir=Path(tmp) / "out", taxonomy_path=path)

    def test_script_does_not_use_the_network(self):
        self.assertNotIn("urllib", Path(stimuli.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
