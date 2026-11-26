"""
Purpose:
    Turn researcher configuration plus one participant's assignment row into the ordered sequence of stages that
    participant walks through: consent, study instructions, a practice scenario, then each assigned trial
    (scenario view followed by its assessment), the post-study questionnaire and the completion screen.

    The sequence is a pure function of the configuration and the assignment row, so deriving it again yields the
    same stage keys and the same trial ids. That is what makes resuming an interrupted session safe: the server
    stores only how far a participant has come (a stage key), and re-deriving the plan can never invent a new
    trial or renumber an existing one.

    Nothing here renders anything, stores anything or talks to a participant; this module only answers "what does
    participant P see, in what order, and under which identifiers". Serving the stages and persisting progress are
    separate steps of the participant flow.

Inputs:
    An experiment configuration file (src/data/experiment_configs/*.json; default study.json) with keys:
        version                   must equal CONFIG_VERSION,
        description               free text,
        corpus                    repository-relative corpus file deciding which stimuli may be served,
        assignments               repository-relative assignments file (build_assignments.py),
        simulated                 true only for a development configuration that accepts a simulated
                                  assignments file (planned, not yet passing, unsafe items),
        trials_per_participant    how many of the assignment's trials to use, counted from the first,
        safe_stimulus_suffix      appended to a scenario id to name its safe stimulus folder,
        practice                  {"stimulus": <folder>, "assessment": <bool>}.
    The assignments file it names: {"version", "seed", "simulated", "final", "assignments": [...]} as written by
    build_assignments.py --write (study) or --write-simulated (development).

Outputs:
    In-memory stage sequences (plan_for) and configuration problems (design_problems). The command line prints one
    participant's plan as JSON, or a check over every participant in the assignments file. No file is written.

Dependencies:
    build_assignments.py for the assignments file it reads. participant_server.py (imported only inside
    unresolved_stimuli) to decide whether a stage's stimulus can actually be served by the configured corpus.
    Used by the participant flow (links, progress, resume) and by tests/test_experiment_plan.py.

Usage:
    python3 -B src/scripts/experiment_plan.py --config src/data/experiment_configs/development.json --check
    python3 -B src/scripts/experiment_plan.py --config src/data/experiment_configs/development.json --participant P001
    python3 -B src/scripts/experiment_plan.py --check        # the study configuration (needs the stage-2 assignments)
"""

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "src" / "data" / "experiment_configs" / "study.json"
DEVELOPMENT_CONFIG = ROOT / "src" / "data" / "experiment_configs" / "development.json"
CONFIG_VERSION = "experiment_config_v1"
REQUIRED_KEYS = ("version", "corpus", "assignments", "simulated", "trials_per_participant",
                 "safe_stimulus_suffix", "practice")
CONDITIONS = ("low", "high")
# Stage kinds the participant flow knows. "scenario" shows one agent conversation, "assessment" the judgment that
# follows it; the rest are single screens whose content is study material.
STAGE_KINDS = ("consent", "instructions", "scenario", "assessment", "questionnaire", "completion")


class ConfigError(ValueError):
    """The experiment configuration is missing a key, has an invalid value, or contradicts the assignments file."""


class Config(dict):
    """A loaded configuration: the JSON keys plus "path", "corpus_path" and "assignments_path"."""


def load_config(path: Path = CONFIG_PATH, root: Path = ROOT) -> Config:
    """Read and validate an experiment configuration; relative paths are resolved against `root`.

    Raises ConfigError for a missing key, an unknown version, a non-positive trial count or a practice entry
    without a stimulus. Does not open the corpus or the assignments file (load_assignments does that), so a
    configuration can be validated before its data exists.
    """
    path = Path(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    for key in REQUIRED_KEYS:
        if key not in config:
            raise ConfigError(f"{path}: missing key {key!r}")
    if config["version"] != CONFIG_VERSION:
        raise ConfigError(f"{path}: version {config['version']!r} is not {CONFIG_VERSION!r}")
    count = config["trials_per_participant"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ConfigError(f"{path}: trials_per_participant must be a positive integer, not {count!r}")
    if not isinstance(config["simulated"], bool):
        raise ConfigError(f"{path}: simulated must be true or false")
    practice = config["practice"]
    if not isinstance(practice, dict) or not isinstance(practice.get("stimulus"), str) or not practice["stimulus"]:
        raise ConfigError(f"{path}: practice needs a stimulus folder")
    if not isinstance(config["safe_stimulus_suffix"], str) or not config["safe_stimulus_suffix"]:
        raise ConfigError(f"{path}: safe_stimulus_suffix must be a non-empty string")
    loaded = Config(config)
    loaded["path"] = path
    loaded["root"] = Path(root)
    loaded["corpus_path"] = Path(root) / config["corpus"]
    loaded["assignments_path"] = Path(root) / config["assignments"]
    return loaded


def load_assignments(config: Config) -> dict:
    """Read the assignments file the configuration names.

    A file marked "simulated": true (unsafe items that are only planned) is refused unless the configuration is
    itself marked simulated, so a development assignment set cannot be served as the study design. Raises
    ConfigError for a missing "assignments" list or a simulated/study mismatch, and OSError if the file is absent.
    """
    payload = json.loads(config["assignments_path"].read_text(encoding="utf-8"))
    if not isinstance(payload.get("assignments"), list) or not payload["assignments"]:
        raise ConfigError(f"{config['assignments_path']}: no assignments in the file")
    simulated = bool(payload.get("simulated", False))
    if simulated and not config["simulated"]:
        raise ConfigError(f"{config['assignments_path']}: simulated assignments, but {config['path']} is not a "
                          f"simulated configuration")
    if not simulated and config["simulated"]:
        raise ConfigError(f"{config['assignments_path']}: study assignments must not be used with the simulated "
                          f"configuration {config['path']}")
    return payload


def assignment_for(payload: dict, participant: str) -> dict:
    """The assignment row of one participant. Raises KeyError if the assignments file does not hold it."""
    for row in payload["assignments"]:
        if row["participant"] == participant:
            return row
    raise KeyError(participant)


def stimulus_folder(trial: dict, safe_suffix: str) -> str:
    """The stimulus folder a trial is shown from.

    A safe trial uses the scenario's safe stimulus (scenario id + `safe_suffix`); an unsafe trial uses the
    injected item's own folder, which is its item id. Raises ConfigError for an unsafe trial without an item id
    (an assignments file that was written before the item existed).
    """
    if trial["version"] == "safe":
        return f"{trial['scenario']}{safe_suffix}"
    if not trial.get("item_id"):
        raise ConfigError(f"unsafe trial {trial.get('trial')} of {trial.get('scenario')} has no item id")
    return trial["item_id"]


def trial_id(participant: str, ordinal: int, practice: bool = False) -> str:
    """A unique, stable identifier for one trial of one participant, e.g. "P001-t03" ("P001-p00" for practice).

    Derived rather than generated, so re-deriving a plan after an interruption reuses the same ids instead of
    creating duplicates. The format stays within the token pattern the interaction log accepts.
    """
    return f"{participant}-{'p' if practice else 't'}{ordinal:02d}"


PRACTICE_TRIAL_RE = re.compile(r"-p\d{2}$")  # the practice form of trial_id above


def is_practice_trial_id(value) -> bool:
    """True for a practice trial id such as "P001-p00" (see trial_id); False for anything else, including non-strings.

    Used to keep practice records apart from experimental ones wherever the trial id is all a record carries,
    such as an interaction event posted by the browser.
    """
    return isinstance(value, str) and bool(PRACTICE_TRIAL_RE.search(value))


def _scenario_of(folder: str) -> str:
    """The scenario id a stimulus folder belongs to: the part before the first "__" ("user_task_13")."""
    return folder.split("__", 1)[0]


def stage_sequence(config: Config, assignment: dict) -> list[dict]:
    """The ordered stages of one participant, derived from the configuration and their assignment row.

    Order: consent, instructions, the practice scenario (and its assessment unless practice.assessment is false),
    then for each assigned trial its scenario stage followed by its assessment stage, then the questionnaire and
    the completion screen. `trials_per_participant` takes the first N trials of the row, keeping the
    counterbalanced order; the condition is the row's and applies to every scenario stage, practice included, so
    the practice familiarises the participant with the interface they will actually use.

    Each stage is {"index", "key", "kind"}; scenario and assessment stages add "trial_id", "trial", "practice",
    "scenario", "version", "stimulus" and "condition". Stage keys are unique within a participant and are what
    stored progress refers to. Raises ConfigError if the row asks for more trials than it holds or its condition
    is not a known one.
    """
    condition = assignment["condition"]
    if condition not in CONDITIONS:
        raise ConfigError(f"{assignment.get('participant')}: unknown condition {condition!r}")
    wanted = config["trials_per_participant"]
    trials = sorted(assignment["trials"], key=lambda t: t["trial"])
    if len(trials) < wanted:
        raise ConfigError(f"{assignment.get('participant')}: {wanted} trials configured but the assignment holds "
                          f"{len(trials)}")
    participant = assignment["participant"]
    suffix = config["safe_stimulus_suffix"]
    stages: list[dict] = [{"key": "consent", "kind": "consent"}, {"key": "instructions", "kind": "instructions"}]

    def trial_stages(prefix: str, tid: str, ordinal: int, folder: str, version: str, with_assessment: bool,
                     practice: bool) -> list[dict]:
        shared = {"trial_id": tid, "trial": ordinal, "practice": practice, "scenario": _scenario_of(folder),
                  "version": version, "stimulus": folder, "condition": condition}
        out = [{"key": f"{prefix}_scenario", "kind": "scenario", **shared}]
        if with_assessment:
            out.append({"key": f"{prefix}_assessment", "kind": "assessment", **shared})
        return out

    practice_cfg = config["practice"]
    stages += trial_stages("practice", trial_id(participant, 0, practice=True), 0, practice_cfg["stimulus"],
                           "practice", practice_cfg.get("assessment", True), True)
    for trial in trials[:wanted]:
        ordinal = trial["trial"]
        stages += trial_stages(f"trial_{ordinal:02d}", trial_id(participant, ordinal), ordinal,
                               stimulus_folder(trial, suffix), trial["version"], True, False)
    stages += [{"key": "questionnaire", "kind": "questionnaire"}, {"key": "completion", "kind": "completion"}]
    return [dict(stage, index=i) for i, stage in enumerate(stages)]


def plan_for(config: Config, participant: str, payload: dict | None = None) -> dict:
    """One participant's whole plan: their assignment facts plus the stage sequence.

    `payload` is the already loaded assignments file; it is read from the configuration when omitted. Returns
    {"participant", "condition", "list", "simulated", "trials", "stages"} where "trials" is the number of
    experimental (non-practice) trials. Raises KeyError for an unknown participant.
    """
    payload = load_assignments(config) if payload is None else payload
    assignment = assignment_for(payload, participant)
    stages = stage_sequence(config, assignment)
    return {"participant": participant, "condition": assignment["condition"], "list": assignment["list"],
            "simulated": bool(payload.get("simulated", False)),
            "trials": config["trials_per_participant"], "stages": stages}


def scenario_stages(plan: dict, practice: bool = False) -> list[dict]:
    """The scenario stages of a plan, practice included only if `practice` is true."""
    return [s for s in plan["stages"] if s["kind"] == "scenario" and (practice or not s["practice"])]


def design_problems(config: Config, payload: dict | None = None) -> list[str]:
    """Problems that would break the flow for some participant (empty list = the configuration is usable).

    Checks that every participant's stages can be derived at all, that participant ids are unique, and that the
    practice scenario is not one of the final eight scenarios (a participant must not meet the same legitimate
    task twice, and practising on an experimental scenario would reveal its outcome).
    """
    problems = []
    try:
        payload = load_assignments(config) if payload is None else payload
    except (ConfigError, OSError, ValueError) as error:
        return [str(error)]
    ids = [row["participant"] for row in payload["assignments"]]
    if len(set(ids)) != len(ids):
        problems.append("the assignments file repeats a participant id")
    practice_scenario = _scenario_of(config["practice"]["stimulus"])
    final = payload.get("final", {}).get("scenarios", [])
    if practice_scenario in final:
        problems.append(f"the practice scenario {practice_scenario} is one of the experimental scenarios")
    for row in payload["assignments"]:
        try:
            stage_sequence(config, row)
        except (ConfigError, KeyError) as error:
            problems.append(f"{row.get('participant')}: {error}")
    return problems


def unresolved_stimuli(config: Config, plan: dict) -> list[str]:
    """Stimulus folders of a plan's scenario stages (practice included) that the configured corpus cannot serve.

    An empty list means every scenario stage has an eligible stimulus with both condition files. This is the one
    function that touches the stimulus files; it switches participant_server to the configured corpus and switches
    it back, so importing this module never changes which corpus a server process serves.
    """
    import participant_server as ps

    previous = ps.CORPUS["path"] if ps.CORPUS else None
    try:
        ps.use_corpus(config["corpus_path"])
        servable = set(ps.list_stimulus_ids())
    finally:
        if previous is not None:
            ps.use_corpus(previous)
    wanted = [s["stimulus"] for s in scenario_stages(plan, practice=True)]
    return sorted({folder for folder in wanted if folder not in servable})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--participant", help="print this participant's stage plan as JSON")
    parser.add_argument("--check", action="store_true",
                        help="check the configuration against every participant and report unservable stimuli")
    args = parser.parse_args()
    config = load_config(args.config)
    payload = load_assignments(config)
    if args.participant:
        plan = plan_for(config, args.participant, payload)
        print(json.dumps(plan, indent=2))
        missing = unresolved_stimuli(config, plan)
        if missing:
            print(f"unservable stimuli: {missing}")
        return
    if not args.check:
        parser.error("choose --check or --participant ID")
    problems = design_problems(config, payload)
    print(f"{config['path'].name}: {len(payload['assignments'])} participants, "
          f"{config['trials_per_participant']} trials each, "
          f"{'simulated' if payload.get('simulated') else 'study'} assignments")
    print(f"problems: {problems or 'none'}")
    missing = sorted({folder for row in payload["assignments"]
                      for folder in unresolved_stimuli(config, plan_for(config, row["participant"], payload))})
    print(f"unservable stimuli: {missing or 'none'}")


if __name__ == "__main__":
    main()
