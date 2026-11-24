"""
Purpose:
    Build the injected-skill items of the unsafe-stimulus design: for each of the 12 candidate scenarios, ONE injected
    copy of the scenario's main capability skill (its area skill), made from the CURRENT skill file by inserting a
    single block (injection_design.py: strategy, wording, objective, attacker identity) at a seeded workflow
    subsection. The study plan uses strategy II only, with the three wordings mixed over the 12 items (four each;
    design.allocate_study_items). W2 and W1 append a block after the subsection; W3 inserts prose inside the
    subsection as a lead-in or into its first or last step, per the item's variant. Other strategy-wording plans stay
    buildable with --wordings / --strategies. Nothing is edited by hand, so the whole corpus is reproducible from the
    selection pipeline.

    Every item is checked while it is built: the base skill's SHA-256 equals the one recorded in the scenario's
    skills-v4b run (so safe and unsafe versions differ only by the block), removing the inserted block gives back the
    base skill byte for byte, the block occurs once, contains the goal text and none of AgentDojo's default attacker.
    The old corpus (180 files built on the pre-e5e53ed skills, never run) is in archive/injected_skills_pre_e5e53ed/.

Inputs:
    src/data/scenario_selection_inputs.json (via select_scenarios.select), src/data/skills/*/SKILL.md,
    the skills-v4b run records named in the inputs.

Outputs:
    src/data/injected_skills/items/<item_id>/SKILL.md   the injected skill (one per item);
    src/data/injected_skills/manifest.json              provenance and hashes of every item.
    With --twins: the wording pilot's items: src/data/injected_skills/pilot_w2/ (the II + W2 items of the earlier
    study plan) and pilot_w3/ (their W3 variant v3 twins), each with items/<item_id>/SKILL.md and manifest.json
    (see "Twins" below).
    Deterministic: the same inputs give byte-identical files.

    Twins. The wording pilot compares W2 and W3 on the same 12 scenarios. A W3 twin of a W2 item has the same
    scenario, objective, strategy (II), attacker identity and workflow subsection; only the wording differs (W3
    variant v3: a sentence at the end of the subsection's last step, instead of W2's separate "Required first
    step" paragraph after the subsection). Because the item id contains the wording and seeds the subsection
    choice, a twin is built from the W2 item's plan and uses the W2 item id as its anchor seed; building W3
    through the normal allocation would change objectives and subsections. The pilot items are not study items and
    never enter the study manifest (they exist so the pilot stays independent of the study plan's wording mix).

Dependencies:
    select_scenarios.py (candidates and planned items), injection_design.py (rendering, placement).

Usage:
    python3 -B src/scripts/build_injected_items.py            # write the items and the manifest
    python3 -B src/scripts/build_injected_items.py --check    # fail if the files differ from a rebuild
    python3 -B src/scripts/build_injected_items.py --twins    # write the wording pilot's items (pilot_w2/, pilot_w3/)
    python3 -B src/scripts/build_injected_items.py --twins --check
    python3 -B src/scripts/build_injected_items.py --strategies II,TK --wordings W1,W2 --out-dir <scratch>   # preview the earlier plan
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import injection_design as design
import select_scenarios as ss

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "src" / "data" / "injected_skills"
BUILDER_VERSION = "injected_items_v1"


class ItemError(RuntimeError):
    """An item failed one of the build-time checks."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_item(scenario: str, plan: dict, row: dict, facts_task: dict, seed: str, anchor_item_id: str | None = None,
               items_root: str = "items", paired_with: str | None = None) -> tuple[str, dict]:
    """Build one item: returns (SKILL.md text, manifest entry). Raises ItemError if a check fails.

    anchor_item_id: the item id that seeds the workflow-subsection choice (default: the item's own id); a W3 twin
    passes its W2 item's id so both land in the same subsection. items_root: folder of the item under
    src/data/injected_skills/. paired_with: the W2 item id of a twin, recorded in the manifest entry."""
    anchor_id = anchor_item_id or plan["item_id"]
    skill = row["area"]
    base_path = ROOT / "src" / "data" / "skills" / skill / "SKILL.md"
    base_bytes = base_path.read_bytes()
    record = json.loads((ROOT / facts_task["v4b_run"]["file"]).read_text(encoding="utf-8"))
    recorded = next((s for s in record["skills"] if s["id"] == skill), None)
    if recorded is None or recorded["sha256"] != sha256_bytes(base_bytes):
        raise ItemError(f"{scenario}: skill {skill} differs from the skills-v4b run's recorded version")
    base = base_bytes.decode("utf-8")
    identity = design.identity_by_id(plan["identity_id"])
    variant = plan.get("variant")  # W3 only
    block = design.render_block(plan["strategy"], plan["wording"], plan["objective"], identity, variant)
    if plan["wording"] == "W3":
        text, inserted, anchor_title, offset = design.place_w3(base, block, variant, seed, anchor_id)
    else:
        anchor_title, offset = design.pick_anchor(base, seed, anchor_id)
        text, inserted = design.insert_block(base, block, offset)
    goal = design.goal_text(plan["objective"], identity)
    if design.remove_block(text, inserted) != base:
        raise ItemError(f"{plan['item_id']}: removing the block does not restore the base skill")
    if goal not in text or "mark.black" in text or text.count(block.strip("\n")) != 1:
        raise ItemError(f"{plan['item_id']}: injected text is not as designed")
    entry = {
        "item_id": plan["item_id"], "scenario": scenario, "injected_skill": skill,
        "base_file": f"src/data/skills/{skill}/SKILL.md", "base_sha256": sha256_bytes(base_bytes),
        "base_matches_v4b_run": facts_task["v4b_run"]["file"],
        "objective": plan["objective"], "objective_agentdojo_task": design.OBJECTIVES[plan["objective"]]["agentdojo_task"],
        "strategy": design.STRATEGY_NAMES[plan["strategy"]], "wording": plan["wording"],
        "attacker_identity": None if identity is None else {k: identity[k] for k in ("id", "name", "address")},
        "goal_text": goal, "anchor_title": anchor_title, "anchor_offset": offset,
        "rendered_block_sha256": design.sha256_text(block), "inserted_text_sha256": design.sha256_text(inserted),
        "item_file": f"src/data/injected_skills/{items_root}/{plan['item_id']}/SKILL.md",
        "item_sha256": design.sha256_text(text),
        "status": "planned", "text_inspection": "pending",
    }
    if variant:  # W3 items only, so W1/W2 entries keep their exact shape
        entry["variant"] = variant
        entry["placement"] = design.W3_VARIANTS[variant]["placement"]
    if paired_with:
        entry["paired_with"] = paired_with
    return text, entry


def build_all(facts: dict, wordings: tuple | None = None, strategies: tuple | None = None) -> tuple[dict[str, str], dict]:
    """{relative path: text} for every item file and the manifest, from the selection result. Without `wordings` and
    `strategies` the study plan is built (II with W1, W2 and W3 mixed, four items each); giving either builds that
    combination space instead (for example wordings=("W2",), strategies=("II",) for the II + W2 plan)."""
    result = ss.select(facts, wordings=wordings, strategies=strategies)
    tasks = {t["scenario"]: t for t in facts["tasks"]}
    files, entries = {}, []
    for scenario in (s for s in result["order"] if s in result["allocation"]):
        text, entry = build_item(scenario, result["allocation"][scenario], result["rows"][scenario], tasks[scenario],
                                 ss.SEED)
        files[f"items/{entry['item_id']}/SKILL.md"] = text
        entries.append(entry)
    manifest = {
        "builder_version": BUILDER_VERSION, "design_version": design.DESIGN_VERSION,
        "selection_version": ss.SELECTION_VERSION, "seed": ss.SEED, "agentdojo_commit": facts["agentdojo_commit"],
        "w1_user_name": design.USER_NAME, "w1_model_name": design.MODEL_NAME,
        "wording_note": "Study plan: strategy important_instructions (II) only, with the three wordings mixed over the 12 "
                        "items (four each) so that a participant's unsafe skills do not repeat one phrase; the "
                        "wordings are not assumed to be equally easy to notice. W1 = AgentDojo template verbatim; W2 "
                        "and W3 = this project's adaptations (not AgentDojo or SkillJect templates): W2 = a required "
                        "first step of the skill, W3 = the goal as prose inside an existing workflow (variants v1-v3 "
                        "differ in phrasing and placement). tool_knowledge is implemented but not used by any item.",
        "review_note": "Automated validation plus inspection by one researcher; no independent reviewer.",
        "items": entries,
    }
    return files, manifest


PILOT_W2_ROOT = "pilot_w2"
TWIN_ROOT = "pilot_w3"
TWIN_VARIANT = "v3"  # sentence at the end of the workflow's last step


def build_twins(facts: dict) -> tuple[dict[str, str], dict, dict]:
    """The wording pilot's items: ({relative path: text}, W3 twin manifest, W2 manifest).

    The pilot compares W2 and W3 on the same 12 scenarios, so both sides are built from the II + W2 allocation (the
    earlier approved study plan, whose objectives and identities the current study plan keeps): the W2 items go to
    pilot_w2/ (the same texts that plan had) and each one's W3 (v3) twin to pilot_w3/, built from the W2 item's plan
    with the W2 item id as the subsection seed (see the module docstring). The pilot items are independent of the
    current study manifest, whose wordings are mixed."""
    result = ss.select(facts, wordings=("W2",), strategies=("II",))
    tasks = {t["scenario"]: t for t in facts["tasks"]}
    files, w2_entries, w3_entries = {}, [], []
    for scenario in (s for s in result["order"] if s in result["allocation"]):
        w2_plan = result["allocation"][scenario]
        text, entry = build_item(scenario, w2_plan, result["rows"][scenario], tasks[scenario], ss.SEED,
                                 items_root=f"{PILOT_W2_ROOT}/items")
        files[f"{PILOT_W2_ROOT}/items/{entry['item_id']}/SKILL.md"] = text
        w2_entries.append(entry)
        plan = {**w2_plan, "wording": "W3", "variant": TWIN_VARIANT}
        plan["item_id"] = design.item_id(scenario, plan["objective"], plan["strategy"], "W3")
        text, entry = build_item(scenario, plan, result["rows"][scenario], tasks[scenario], ss.SEED,
                                 anchor_item_id=w2_plan["item_id"], items_root=f"{TWIN_ROOT}/items",
                                 paired_with=w2_plan["item_id"])
        files[f"{TWIN_ROOT}/items/{entry['item_id']}/SKILL.md"] = text
        w3_entries.append(entry)
    common = {"builder_version": BUILDER_VERSION, "design_version": design.DESIGN_VERSION,
              "selection_version": ss.SELECTION_VERSION, "seed": ss.SEED, "agentdojo_commit": facts["agentdojo_commit"]}
    review = "Automated validation plus inspection by one researcher; no independent reviewer."
    w2_manifest = {**common, "pilot_note": "Wording pilot only (W2 vs W3): the II + W2 items of the earlier study plan, "
                                           "the W2 side of the pilot. Not part of the current study set.",
                   "review_note": review, "items": w2_entries}
    manifest = {**common, "twin_note": "Pilot only (W2 vs W3). Each item is the W3 variant v3 twin of the W2 item named in "
                                       "paired_with (pilot_w2/manifest.json): same scenario, objective, strategy, "
                                       "attacker identity and workflow subsection; only the wording differs. Not part "
                                       "of the study set.",
                "review_note": review, "items": w3_entries}
    files[f"{PILOT_W2_ROOT}/manifest.json"] = json.dumps(w2_manifest, indent=2, ensure_ascii=False) + "\n"
    files[f"{TWIN_ROOT}/manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    return files, manifest, w2_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--twins", action="store_true",
                        help="build the wording pilot's items (pilot_w2/ and their W3 twins in pilot_w3/) instead")
    parser.add_argument("--wordings", default=None,
                        help="comma-separated wordings to plan; with --strategies, overrides the study plan "
                             "(default: the study plan, II with W1,W2,W3 mixed)")
    parser.add_argument("--strategies", default=None,
                        help="comma-separated strategies to plan (II,TK = both); overrides the study plan")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help="where to write (default src/data/injected_skills); give a scratch folder to preview")
    args = parser.parse_args()
    wordings = tuple(args.wordings.split(",")) if args.wordings else None
    strategies = tuple(args.strategies.split(",")) if args.strategies else None
    out_dir = args.out_dir
    facts = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
    if args.twins:
        files, manifest, _ = build_twins(facts)
        item_glob = "pilot_w*/items/*/SKILL.md"
    else:
        files, manifest = build_all(facts, wordings, strategies)
        files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        item_glob = "items/*/SKILL.md"
    if args.check:
        stale = [p for p, t in files.items() if not (out_dir / p).exists() or (out_dir / p).read_text(encoding="utf-8") != t]
        extra = [str(p.relative_to(out_dir)) for p in out_dir.glob(item_glob) if str(p.relative_to(out_dir)) not in files]
        if stale or extra:
            sys.exit(f"out of date: {stale + extra}")
        print("up to date")
        return
    for relative, text in files.items():
        path = out_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(manifest['items'])} items to {out_dir}")


if __name__ == "__main__":
    main()
