"""
Purpose:
    Build the injected-skill items of the unsafe-stimulus design: for each of the 12 candidate scenarios, ONE injected
    copy of the scenario's main capability skill (its area skill), made from the CURRENT skill file by inserting a
    single block (injection_design.py: strategy, wording, objective, attacker identity) at a seeded workflow
    subsection. Nothing is edited by hand, so the whole corpus is reproducible from the selection pipeline.

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
    Deterministic: the same inputs give byte-identical files.

Dependencies:
    select_scenarios.py (candidates and planned items), injection_design.py (rendering, placement).

Usage:
    python3 -B src/scripts/build_injected_items.py            # write the items and the manifest
    python3 -B src/scripts/build_injected_items.py --check    # fail if the files differ from a rebuild
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


def build_item(scenario: str, plan: dict, row: dict, facts_task: dict, seed: str) -> tuple[str, dict]:
    """Build one item: returns (SKILL.md text, manifest entry). Raises ItemError if a check fails."""
    skill = row["area"]
    base_path = ROOT / "src" / "data" / "skills" / skill / "SKILL.md"
    base_bytes = base_path.read_bytes()
    record = json.loads((ROOT / facts_task["v4b_run"]["file"]).read_text(encoding="utf-8"))
    recorded = next((s for s in record["skills"] if s["id"] == skill), None)
    if recorded is None or recorded["sha256"] != sha256_bytes(base_bytes):
        raise ItemError(f"{scenario}: skill {skill} differs from the skills-v4b run's recorded version")
    base = base_bytes.decode("utf-8")
    identity = design.identity_by_id(plan["identity_id"])
    block = design.render_block(plan["strategy"], plan["wording"], plan["objective"], identity)
    anchor_title, offset = design.pick_anchor(base, seed, plan["item_id"])
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
        "item_file": f"src/data/injected_skills/items/{plan['item_id']}/SKILL.md",
        "item_sha256": design.sha256_text(text),
        "status": "planned", "text_inspection": "pending",
    }
    return text, entry


def build_all(facts: dict) -> tuple[dict[str, str], dict]:
    """{relative path: text} for every item file and the manifest, from the selection result."""
    result = ss.select(facts)
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
        "wording_note": "W1 = AgentDojo template verbatim; W2 = this project's adaptation (not an AgentDojo or "
                        "SkillJect template). Strategies: important_instructions, tool_knowledge only.",
        "review_note": "Automated validation plus inspection by one researcher; no independent reviewer.",
        "items": entries,
    }
    return files, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    facts = json.loads(ss.INPUTS.read_text(encoding="utf-8"))
    files, manifest = build_all(facts)
    files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        stale = [p for p, t in files.items() if not (OUT_DIR / p).exists() or (OUT_DIR / p).read_text(encoding="utf-8") != t]
        extra = [str(p.relative_to(OUT_DIR)) for p in OUT_DIR.glob("items/*/SKILL.md") if str(p.relative_to(OUT_DIR)) not in files]
        if stale or extra:
            sys.exit(f"out of date: {stale + extra}")
        print("up to date")
        return
    for relative, text in files.items():
        path = OUT_DIR / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(manifest['items'])} items to {OUT_DIR}")


if __name__ == "__main__":
    main()
