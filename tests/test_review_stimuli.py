"""Tests for review_stimuli.py: the automated checks pass on real data and fail on each kind of defect, manual
answers are tied to the content they reviewed, and the researcher-only review record never reaches a participant."""

import copy
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import corpus  # noqa: E402
import participant_server as ps  # noqa: E402
import review_stimuli as rv  # noqa: E402
from participant_http import preview_handler  # noqa: E402

STUDY = ROOT / "src" / "data" / "corpora" / "study.json"
VALIDATION = ROOT / "src" / "data" / "corpora" / "validation.json"
SIMULATED = ROOT / "src" / "data" / "corpora" / "simulated.json"
ALL_PASS = {k: "pass" for k in rv.MANUAL}


class RealDataTests(unittest.TestCase):
    def tearDown(self):
        ps.use_corpus(STUDY)

    def test_every_check_passes_for_every_stimulus_of_both_corpora(self):
        for path, count in ((STUDY, 20), (VALIDATION, 1), (SIMULATED, 12)):
            ps.use_corpus(path)
            record = rv.review_corpus()
            self.assertEqual(len(record["stimuli"]), count)
            for folder, entry in record["stimuli"].items():
                self.assertEqual(set(entry["automated"]), set(rv.AUTOMATED), folder)
                failed = {k: c["detail"] for k, c in entry["automated"].items() if not c["pass"]}
                self.assertEqual(failed, {}, folder)

    def test_classifications(self):
        ps.use_corpus(STUDY)
        self.assertEqual({e["classification"] for e in rv.review_corpus()["stimuli"].values()}, {"safe"})
        for path in (VALIDATION, SIMULATED):
            ps.use_corpus(path)
            self.assertEqual({e["classification"] for e in rv.review_corpus()["stimuli"].values()}, {"unsafe-simulated"})

    def test_committed_review_records_are_up_to_date(self):
        for path in (STUDY, VALIDATION, SIMULATED):
            ps.use_corpus(path)
            saved = rv.read_record(ps.CORPUS["review_path"])
            self.assertIsNotNone(saved, f"run review_stimuli.py --write for {path.name}")
            self.assertEqual(rv.review_corpus(saved), saved, f"stale review record for {path.name}")

    def test_review_is_deterministic(self):
        ps.use_corpus(STUDY)
        self.assertEqual(rv.dumps(rv.review_corpus()), rv.dumps(rv.review_corpus()))


class DefectDetectionTests(unittest.TestCase):
    """Each automated check must fail when its defect is introduced into an otherwise good context."""

    @classmethod
    def setUpClass(cls):
        ps.use_corpus(STUDY)
        cls.good = rv.load_context("user_task_13__no_injection")

    @classmethod
    def tearDownClass(cls):
        ps.use_corpus(STUDY)

    def failing(self, mutate):
        ctx = copy.deepcopy(self.good)
        mutate(ctx)
        return sorted(k for k, c in rv.run_checks(ctx).items() if not c["pass"])

    def test_the_unmodified_context_passes(self):
        self.assertEqual(self.failing(lambda c: None), [])

    def test_a1_missing_file(self):
        self.assertIn("A1", self.failing(lambda c: c["files"].update({"preview.txt": False})))
        self.assertIn("A1", self.failing(lambda c: c.update(workspace_file=False)))

    def test_a2_not_eligible_or_not_listed(self):
        self.assertIn("A2", self.failing(lambda c: c.update(eligible=False)))
        self.assertIn("A2", self.failing(lambda c: c.update(listed=False)))

    def test_a3_conditions_differ(self):
        self.assertIn("A3", self.failing(lambda c: c["low"].update(final_response="different")))
        self.assertIn("A3", self.failing(lambda c: c["low"].update(trace=c["high"]["trace"])))
        self.assertIn("A3", self.failing(lambda c: c["high"].update(trace=[])))

    def test_a4_action_without_outcome(self):
        def blank(c):
            next(s for s in c["high"]["trace"] if s["type"] == "action")["outcome"]["summary"] = ""
        self.assertIn("A4", self.failing(blank))

    def test_a5_text_differs_from_the_record(self):
        def change(c):
            c["low"]["final_response"] = c["high"]["final_response"] = "edited"
        self.assertIn("A5", self.failing(change))

    def test_a6_log_differs_from_the_record_tool_calls(self):
        def drop(c):
            c["high"]["trace"].remove(next(s for s in c["high"]["trace"] if s["type"] == "action"))
        self.assertIn("A6", self.failing(drop))

    def test_a7_extra_workspace_state_or_failed_replay(self):
        self.assertIn("A7", self.failing(lambda c: c["workspace"].update(after={})))
        self.assertIn("A7", self.failing(lambda c: c["ws_prov"]["replay"].update(all_match_saved_results=False)))

    def test_a8_empty_skill_documentation(self):
        def empty(c):
            next(iter(c["skill_docs"].values()))["markdown"] = "  "
        self.assertIn("A8", self.failing(empty))
        self.assertIn("A8", self.failing(lambda c: c.update(skills=[])))

    def test_a9_researcher_only_identifier_in_anything_served(self):
        folder, run_id = self.good["folder"], self.good["record"]["run_id"]
        self.assertIn("A9", self.failing(lambda c: c["low"].update(final_response="see " + folder)))
        self.assertIn("A9", self.failing(lambda c: c["workspace"]["account"].update(note=run_id)))
        self.assertIn("A9", self.failing(lambda c: next(iter(c["skill_docs"].values())).update(markdown="tool_knowledge")))

    def test_a10_record_differs_from_its_provenance_hash(self):
        self.assertIn("A10", self.failing(lambda c: c.update(record_sha256="0" * 64)))


class SimulatedProvenanceTests(unittest.TestCase):
    """A10 on a simulated stimulus: each part of a constructed record's provenance is required."""

    @classmethod
    def setUpClass(cls):
        ps.use_corpus(SIMULATED)
        cls.good = rv.load_context("user_task_9__g4_ii_w3v3")

    @classmethod
    def tearDownClass(cls):
        ps.use_corpus(STUDY)

    def problems(self, mutate):
        ctx = copy.deepcopy(self.good)
        mutate(ctx)
        return rv.provenance_problems(ctx)

    def test_the_unmodified_context_passes(self):
        self.assertEqual(self.problems(lambda c: None), [])
        self.assertEqual(rv.run_checks(copy.deepcopy(self.good))["A10"]["pass"], True)

    def test_each_missing_or_wrong_part_fails(self):
        sim = lambda c: c["record"]["simulation"]  # noqa: E731
        cases = {
            "safe run": lambda c: sim(c)["safe_run"].update(sha256="0" * 64),
            "skill file": lambda c: c["record"]["skills"][0].update(sha256="0" * 64),
            "attack calls": lambda c: sim(c)["attack_calls"].reverse(),
            "no attack calls": lambda c: sim(c).update(attack_calls=[]),
            "inclusion check": lambda c: sim(c)["inclusion_check"].update(included=False),
            "material kind": lambda c: c["record"].update(material_kind="recorded_execution"),
        }
        for name, mutate in cases.items():
            self.assertEqual(len(self.problems(mutate)), 1, name)

    def test_a9_flags_the_simulated_labels(self):
        ctx = copy.deepcopy(self.good)
        ctx["low"]["final_response"] += " simulated_trace"
        self.assertFalse(rv.run_checks(ctx)["A9"]["pass"])


class StatusAndMarkTests(unittest.TestCase):
    def entry(self, **changes):
        base = {"classification": "safe", "content_sha256": "h1", "automated_pass": True, "manual": rv.empty_manual()}
        base.update(changes)
        return base

    def setUp(self):
        ps.use_corpus(STUDY)
        self.record = {"stimuli": {"f": {**self.entry(), "automated": {}, "status": "unreviewed"}}}

    def test_status_values(self):
        self.assertEqual(rv.status_of(self.entry()), "unreviewed")
        self.assertEqual(rv.status_of(self.entry(automated_pass=False)), "auto_failed")
        reviewed = self.entry()
        reviewed["manual"].update(reviewer="R", reviewed_content_sha256="h1", items=dict(ALL_PASS))
        self.assertEqual(rv.status_of(reviewed), "reviewed")
        rejected = copy.deepcopy(reviewed)
        rejected["manual"]["items"]["M2"] = "fail"
        self.assertEqual(rv.status_of(rejected), "rejected")
        stale = copy.deepcopy(reviewed)
        stale["content_sha256"] = "h2"
        self.assertEqual(rv.status_of(stale), "stale")
        partial = copy.deepcopy(reviewed)
        partial["manual"]["items"]["M7"] = None
        self.assertEqual(rv.status_of(partial), "unreviewed")

    def test_mark_requires_every_item_a_valid_answer_and_a_reviewer(self):
        for answers in ({k: "pass" for k in list(rv.MANUAL)[:-1]}, {**ALL_PASS, "M1": "yes"}, {**ALL_PASS, "M8": "pass"}):
            with self.assertRaises(ValueError):
                rv.mark(self.record, "f", "R", answers, "", "2026-01-01")
        with self.assertRaises(ValueError):
            rv.mark(self.record, "f", " ", ALL_PASS, "", "2026-01-01")
        with self.assertRaises(ValueError):
            rv.mark(self.record, "missing", "R", ALL_PASS, "", "2026-01-01")

    def test_mark_ties_the_review_to_the_content_and_a_change_makes_it_stale(self):
        entry = rv.mark(self.record, "f", "R", ALL_PASS, "ok", "2026-01-01")
        self.assertEqual((entry["status"], entry["manual"]["reviewed_content_sha256"]), ("reviewed", "h1"))
        self.assertEqual(entry["manual"]["notes"], "ok")
        entry["content_sha256"] = "changed"
        self.assertEqual(rv.status_of(entry), "stale")

    def test_regenerating_keeps_manual_answers_and_flags_changed_content(self):
        record = rv.review_corpus()
        folder = "user_task_1__no_injection"
        rv.mark(record, folder, "R", ALL_PASS, "n", "2026-01-01")
        again = rv.review_corpus(record)
        self.assertEqual(again["stimuli"][folder]["status"], "reviewed")
        self.assertEqual(again["stimuli"][folder]["manual"], record["stimuli"][folder]["manual"])
        self.assertEqual(again["stimuli"]["user_task_2__no_injection"]["status"], "unreviewed")
        record["stimuli"][folder]["manual"]["reviewed_content_sha256"] = "an older hash"
        self.assertEqual(rv.review_corpus(record)["stimuli"][folder]["status"], "stale")


class ResearcherOnlyTests(unittest.TestCase):
    def test_no_participant_route_serves_the_review_records_or_their_content(self):
        ps.use_corpus(STUDY)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), preview_handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path):
            try:
                with urllib.request.urlopen(base + path) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()

        try:
            for path in ("/src/data/review/study_review.json", "/data/review/study_review.json", "/static/study_review.json",
                         "/static/../data/review/study_review.json", "/api/review", "/review"):
                self.assertEqual(get(path)[0], 404, path)
            folder = "user_task_1__no_injection"
            served = "".join(get(p)[1] for p in (f"/api/stimulus?s={folder}&c=low", f"/api/stimulus?s={folder}&c=high",
                                                   f"/api/workspace?s={folder}", f"/api/skills?s={folder}", "/"))
            for word in ("classification", "automated", "content_sha256", "reviewer"):
                self.assertNotIn(word, served)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_review_files_are_outside_the_folders_participants_are_served_from(self):
        for path in (STUDY, VALIDATION):
            c = corpus.load_corpus(path)
            self.assertNotIn(c["stimuli_path"], c["review_path"].parents)
            self.assertNotIn(c["workspace_path"], c["review_path"].parents)
            self.assertNotIn(ps.UI_DIR, c["review_path"].parents)


if __name__ == "__main__":
    unittest.main()
