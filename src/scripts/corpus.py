"""
Purpose:
    Load a stimulus *corpus* configuration and decide, from data alone, whether a stimulus belongs to it. A corpus
    says where its stimuli and workspace snapshots live, which stimuli it contains, which identifier pattern is
    accepted in requests, and which execution records may back a stimulus (the eligibility rules). Nothing about a
    stimulus's identifier is hard-coded: the rules look at the execution record behind it (run id suffix,
    condition, status, utility, "simulated" flag, and, for injected runs, the inclusion verdict), so safe runs,
    injected runs and the offline simulated fixture are all handled by configuration.

    Corpora: `study` (the study stimuli), `validation` (one simulated stimulus for interface testing only),
    `simulated` (review only: the constructed unsafe stimuli, for their manual review) and `generated_pilot`
    (one WG inspection stimulus). Genuine and constructed stimuli are kept apart by folder: a corpus with a
    `simulated_source` reads constructed stimuli only from that second folder pair, and participant_server.py
    accepts a stimulus from the main folders only if its record is genuine and from the second only if it is
    simulated. The study corpus admits a simulated record only through a rule with `reviewed_in`, i.e. after its
    manual review.

Inputs:
    A corpus JSON file (default src/data/corpora/study.json), with keys:
        name, description,
        stimuli_dir, workspace_dir        (repository-relative folders),
        default_stimulus                  (folder name the landing page lists first),
        id_pattern                        (optional regular expression for accepted stimulus ids),
        include                           (optional list of stimulus folders; absent = every folder),
        exclude                           (optional {stimulus folder: reason}: folders removed from the corpus by a
                                           documented decision, e.g. rejected in manual review; never eligible,
                                           whatever the rules say. The files stay where they are, for provenance),
        review_file                       (optional path of the researcher-only review record, review_stimuli.py),
        simulated_source                  (optional {"stimuli_dir", "workspace_dir"}: a second, separate folder
                                           pair holding constructed (simulated) stimuli only),
        rules                             (list of eligibility rules; a stimulus is eligible if ANY rule matches).
    A rule is an object with "id" and any of: "run_id_suffix", "condition", "status", "utility", "simulated",
    "evaluation_file" (path of an injection_checks.py output in which the run must have "included": true),
    "reviewed_in" (path of a review_stimuli.py record in which the stimulus, named by the record's injection item
    id, must have status "reviewed").

Outputs:
    A `Corpus` (dict subclass) with resolved absolute paths ("stimuli_path", "workspace_path", "review_path",
    "simulated_stimuli_path" and "simulated_workspace_path", None without a simulated_source) and a compiled id
    pattern ("id_regex"); `rule_matches` for one record.

Dependencies:
    Python standard library only. Used by participant_server.py and review_stimuli.py.

Usage:
    Imported, not run. Example: corpus.load_corpus(corpus.DEFAULT_CORPUS)["stimuli_dir"]
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "src" / "data" / "corpora" / "study.json"
# Accepted when a corpus gives no pattern: a single safe path component (no slash, never starts with a dot).
GENERIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
RULE_KEYS = {"id", "run_id_suffix", "condition", "status", "utility", "simulated", "evaluation_file", "reviewed_in"}
REQUIRED_KEYS = ("name", "stimuli_dir", "workspace_dir", "default_stimulus", "rules")


class CorpusError(ValueError):
    """The corpus configuration is missing a key or has an invalid rule."""


class Corpus(dict):
    """A loaded corpus: the JSON keys plus "path", "stimuli_path", "workspace_path", "review_path", "id_regex"."""


def load_corpus(path: Path = DEFAULT_CORPUS, root: Path = ROOT) -> Corpus:
    """Read and validate a corpus file; relative folders are resolved against `root`."""
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in REQUIRED_KEYS:
        if key not in config:
            raise CorpusError(f"{path}: missing key {key!r}")
    if not config["rules"]:
        raise CorpusError(f"{path}: a corpus needs at least one eligibility rule")
    for rule in config["rules"]:
        unknown = set(rule) - RULE_KEYS
        if unknown or "id" not in rule:
            raise CorpusError(f"{path}: invalid rule {rule!r} (unknown keys {sorted(unknown)})")
    exclude = config.get("exclude", {})
    if not isinstance(exclude, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.strip()
                                                for k, v in exclude.items()):
        raise CorpusError(f"{path}: exclude must map each stimulus folder to a non-empty reason")
    corpus = Corpus(config)
    corpus["path"] = path
    corpus["stimuli_path"] = Path(root) / config["stimuli_dir"]
    corpus["workspace_path"] = Path(root) / config["workspace_dir"]
    corpus["id_regex"] = re.compile(config.get("id_pattern", GENERIC_ID_PATTERN))
    corpus["root"] = Path(root)
    corpus["review_path"] = Path(root) / config["review_file"] if config.get("review_file") else None
    source = config.get("simulated_source")
    corpus["simulated_stimuli_path"] = corpus["simulated_workspace_path"] = None
    if source is not None:
        if not isinstance(source, dict) or set(source) != {"stimuli_dir", "workspace_dir"}:
            raise CorpusError(f"{path}: simulated_source needs exactly stimuli_dir and workspace_dir")
        if {source["stimuli_dir"], source["workspace_dir"]} & {config["stimuli_dir"], config["workspace_dir"]}:
            raise CorpusError(f"{path}: simulated_source must use folders separate from the main ones")
        corpus["simulated_stimuli_path"] = Path(root) / source["stimuli_dir"]
        corpus["simulated_workspace_path"] = Path(root) / source["workspace_dir"]
    return corpus


def _evaluation_included(path: Path, run_id: str) -> bool:
    """True if the evaluation file holds this run with "included": true (a missing file means not included)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(e.get("run_id") == run_id and e.get("included") is True for e in data.get("evaluations", []))


def _reviewed(path: Path, record: dict) -> bool:
    """True if the review record marks this record's stimulus "reviewed". The stimulus is named by the record's
    injection item id (the folder name of an injected or simulated stimulus); a record without one, a missing or
    unreadable review file, or any other status (unreviewed, stale, rejected, auto_failed) means not reviewed.
    The status is the one the file holds: review_stimuli.py --check tells whether that file is current."""
    folder = (record.get("injection") or {}).get("item_id")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(folder) and (data.get("stimuli", {}).get(folder) or {}).get("status") == "reviewed"


def rule_matches(rule: dict, record: dict, root: Path = ROOT) -> bool:
    """True if an execution record satisfies every condition the rule states (conditions it omits are not checked).

    "simulated" compares with record.get("simulated", False), so a record without the flag counts as real.
    """
    run_id = record.get("run_id", "")
    if "run_id_suffix" in rule and not run_id.endswith(rule["run_id_suffix"]):
        return False
    if "condition" in rule and record.get("condition") != rule["condition"]:
        return False
    if "status" in rule and record.get("status") != rule["status"]:
        return False
    if "utility" in rule and record.get("utility") is not rule["utility"]:
        return False
    if "simulated" in rule and bool(record.get("simulated", False)) is not rule["simulated"]:
        return False
    if "evaluation_file" in rule and not _evaluation_included(Path(root) / rule["evaluation_file"], run_id):
        return False
    if "reviewed_in" in rule and not _reviewed(Path(root) / rule["reviewed_in"], record):
        return False
    return True


def record_matches_any_rule(corpus: Corpus, record: dict) -> bool:
    """True if at least one of the corpus's rules accepts the record."""
    return any(rule_matches(rule, record, corpus["root"]) for rule in corpus["rules"])
