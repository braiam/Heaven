"""Skill activation stats from horseACT race dumps (heaven-races).

The in-game race exporter writes one JSON per race under
    <game>/heaven-races/<RaceType>/<...>.json
in horseACT/Hakuraku format (C# `<X>k__BackingField` fields). Each file holds
the full RaceInfo incl. the player horse's owned skills + the base64(gzip())
race simulation, which `tt_scenario` decodes into per-horse skill activations.

This module walks those files (incrementally) and, for the PLAYER's horse,
records one row per race:
    {chara_id, chara_name, running_style, distance_cat, owned_skills, activated_skills}
into `race_skill_history.jsonl`. Skill Lookup reads these rows (alongside the
Team Trials history) and can filter by running style and distance.

Career races give one data point each (only the player's horse is in the
dump's PlayerTeamMemberArray), but there are tens of thousands of them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import tt_scenario

# heaven-races lives next to the game exe.
_GAME_DIR = Path(os.environ.get(
    "HEAVEN_RACES_DIR",
    r"D:\Steam\steamapps\common\UmamusumePrettyDerby\heaven-races",
))

try:
    import safe_store
    _DATA_DIR = safe_store.ensure_migrated()
except Exception:
    _DATA_DIR = Path(__file__).parent / "data"

ROWS_PATH = _DATA_DIR / "race_skill_history.jsonl"
SEEN_PATH = _DATA_DIR / "race_skill_seen.txt"
# Imported community exports land here (one .jsonl per shared file). They're
# merged into the pool and deduped by (race_id, chara_id) at load time.
COMMUNITY_DIR = _DATA_DIR / "race_skill_community"

B = ">k__BackingField"
_RS = {1: "NIGE", 2: "SENKO", 3: "SASHI", 4: "OIKOMI"}
_DIST = {"short": "sprint", "sprint": "sprint", "mile": "mile",
         "medium": "medium", "middle": "medium", "long": "long"}


def _g(o: dict, key: str):
    return o.get("<" + key + B)


def _dist_cat(course_dist_type, ground) -> str:
    if ground == 2:          # dirt surface collapses into its own bucket
        return "dirt"
    return _DIST.get(str(course_dist_type).lower(), str(course_dist_type).lower())


def _extract_rows(obj: dict) -> list[dict]:
    """One row per the player's horse(s) in a race dump. Career = 1 horse;
    Champions / Room match = the player's whole team (each fully trained).
    PlayerTeamMemberArray holds only the player's horses (opponents/NPCs aren't
    in it), so every entry is yours."""
    horses = _g(obj, "PlayerTeamMemberArray") or []
    if not horses:
        return []

    # Parse the race sim once → activations keyed by sim horse index.
    acts_by_idx: dict = {}
    sim = _g(obj, "SimDataBase64")
    if sim:
        try:
            acts_by_idx = tt_scenario.activations_per_horse(tt_scenario.parse(sim))
        except Exception:
            acts_by_idx = {}

    rcs = _g(obj, "RaceCourseSet") or {}
    dist_cat = _dist_cat(_g(obj, "CourseDistanceType"), rcs.get("Ground"))
    # RandomSeed is unique per race → with chara_id it's a global dedup key so
    # merging community data never double-counts the same race/uma.
    race_id = _g(obj, "RandomSeed")

    out: list[dict] = []
    for horse in horses:
        rhd = horse.get("_responseHorseData") or {}
        owned = [s.get("skill_id") for s in (rhd.get("skill_array") or []) if s.get("skill_id")]
        if not owned:
            continue
        out.append({
            "race_id":         race_id,
            "chara_id":        rhd.get("card_id"),
            "chara_name":      _g(horse, "charaName") or "?",
            "running_style":   _RS.get(rhd.get("running_style"), str(rhd.get("running_style"))),
            "distance_cat":    dist_cat,
            "owned_skills":    owned,
            "activated_skills": acts_by_idx.get(horse.get("horseIndex"), []),
            "src":             "race",
        })
    return out


def _load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    return set(SEEN_PATH.read_text(encoding="utf-8").splitlines())


def import_races(types: tuple[str, ...] = ("Career",), limit: int | None = None,
                 flush_every: int = 500) -> dict:
    """Incrementally parse new race dumps and append their skill rows.
    `types` = subfolders of heaven-races to scan. Returns counts."""
    if not _GAME_DIR.exists():
        return {"ok": False, "error": f"heaven-races not found: {_GAME_DIR}"}

    seen = _load_seen()
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    for t in types:
        d = _GAME_DIR / t
        if d.exists():
            files.extend(d.glob("*.json"))
    todo = [f for f in files if f.name not in seen]
    if limit:
        todo = todo[:limit]

    new_rows: list[dict] = []
    new_seen: list[str] = []
    processed = 0
    errors = 0

    def _flush():
        nonlocal new_rows, new_seen
        if new_rows:
            with open(ROWS_PATH, "a", encoding="utf-8") as f:
                for r in new_rows:
                    f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
        if new_seen:
            with open(SEEN_PATH, "a", encoding="utf-8") as f:
                f.write("\n".join(new_seen) + "\n")
        new_rows, new_seen = [], []

    for fp in todo:
        try:
            obj = json.loads(fp.read_text(encoding="utf-8"))
            new_rows.extend(_extract_rows(obj))
        except Exception:
            errors += 1
        new_seen.append(fp.name)
        processed += 1
        if processed % flush_every == 0:
            _flush()
    _flush()

    total_rows = sum(1 for _ in open(ROWS_PATH, encoding="utf-8")) if ROWS_PATH.exists() else 0
    return {"ok": True, "processed": processed, "errors": errors,
            "remaining": len(files) - len(seen) - processed, "total_rows": total_rows}


def load_rows() -> list[dict]:
    """Your own race rows + any imported community files, MERGED and DEDUPED by
    (race_id, chara_id) so the same race/uma never counts twice (even across
    people or re-imports)."""
    out: list[dict] = []
    seen: set = set()
    paths = [ROWS_PATH]
    if COMMUNITY_DIR.exists():
        paths += sorted(COMMUNITY_DIR.glob("*.jsonl"))
    for p in paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                rid = r.get("race_id")
                if rid is not None:
                    key = (rid, r.get("chara_id"))
                    if key in seen:
                        continue
                    seen.add(key)
                out.append(r)
    return out


def import_community_file(src_bytes: bytes, label: str = "shared") -> dict:
    """Save an uploaded community export under COMMUNITY_DIR (so load_rows merges
    + dedupes it). Re-importing the same label overwrites, so it never piles up.
    Returns how many NEW (race_id, chara_id) pairs it adds over what we have."""
    COMMUNITY_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in label if c.isalnum() or c in "-_") or "shared"
    have = {(r.get("race_id"), r.get("chara_id")) for r in load_rows()
            if r.get("race_id") is not None}
    added = 0
    rows_in = 0
    for line in src_bytes.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        rows_in += 1
        k = (r.get("race_id"), r.get("chara_id"))
        if r.get("race_id") is not None and k not in have:
            have.add(k)
            added += 1
    (COMMUNITY_DIR / f"{safe}.jsonl").write_bytes(src_bytes)
    return {"ok": True, "rows_in_file": rows_in, "new_added": added}


if __name__ == "__main__":
    import sys
    types = tuple(sys.argv[1:]) or ("Career",)
    print(import_races(types))
