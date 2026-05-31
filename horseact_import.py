"""Import Team Trials data from horseACT JSON dumps.

horseACT (https://github.com/ayaliz/horseACT) is a Hachimi plugin that hooks
the running game and saves each completed match to a JSON file under:
    <horseact_save_root>/Team trials/TT-YYYYMMDD_HHMMSS_mmm.json

This importer reads those JSON files, extracts the same data our mitmproxy
pipeline produces, and appends it to data/team_trials_history.jsonl. After
that, the dashboard works the same way as if the data had been captured
via mitmproxy.

Why use this instead of the mitmproxy capture?
- Skip the udid extraction step (CarrotJuicer not needed)
- Skip the mitmproxy cert install
- Skip every-session network capture
- Works retroactively against your existing horseACT archive

Usage:
    python horseact_import.py "<path to horseACT Team trials folder>"
    python horseact_import.py "<path to a single TT-*.json file>"

NOTE: This importer is best-effort because horseACT's JSON uses raw C#
backing-field names (e.g. `<SimDataBase64>k__BackingField`) and the exact
schema depends on the game's internal class definitions. The importer walks
the JSON heuristically, so minor field-name changes between horseACT
versions should not break it. If your dump produces 0 trials please open
an issue and attach one .json file so the field mapping can be updated.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import master
import race_scenario
import player_state


DATA_DIR = Path(__file__).parent / "data"
HISTORY_PATH = DATA_DIR / "team_trials_history.jsonl"


# ── Heuristic field finders ─────────────────────────────────────────────────
def _strip_backing(key: str) -> str:
    """Convert `<SimDataBase64>k__BackingField` -> `SimDataBase64`."""
    if key.startswith("<") and "k__BackingField" in key:
        end = key.index(">k__BackingField")
        return key[1:end]
    return key


def _normalised_dict(obj: dict) -> dict:
    """Returns a copy with both raw and stripped backing-field keys mapped to the same value."""
    if not isinstance(obj, dict):
        return {}
    out: dict = {}
    for k, v in obj.items():
        out[k] = v
        sk = _strip_backing(k)
        if sk != k:
            out[sk] = v
    return out


def _walk_all(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_all(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_all(v)


def _find_race_array(root: Any) -> list | None:
    """Find the 5-race list inside the dump. A race entry must have a
    race_scenario field (SimDataBase64 in Unity-backing-field naming) and
    a chara/horse result array."""
    SIM_TOKENS = ("simdatabase64", "race_scenario", "sim_data_base64")
    HORSE_TOKENS = ("chara_result_array", "race_horse", "raceresultarray",
                    "raceresult", "horseresult", "resultarray")
    for node in _walk_all(root):
        if isinstance(node, list) and len(node) >= 3:
            if not node or not isinstance(node[0], dict):
                continue
            head = _normalised_dict(node[0])
            lower_keys = [k.lower() for k in head]
            has_sim = any(any(t in k for t in SIM_TOKENS) for k in lower_keys)
            has_horses = any(any(t in k for t in HORSE_TOKENS) for k in lower_keys)
            if has_sim and has_horses:
                return node
    return None


def _find_user_roster(root: Any) -> list | None:
    """The user's roster of 15 umas with their skill_array. Looks for a list
    of dicts each having a trained_chara_id-like field AND a skill_array-like field."""
    for node in _walk_all(root):
        if isinstance(node, list) and len(node) >= 5:
            if not node or not isinstance(node[0], dict):
                continue
            head = _normalised_dict(node[0])
            has_tcid = any(k for k in head if "trained_chara_id" in k.lower() or "TrainedCharaId" in k)
            has_skills = any(k for k in head if "skill_array" in k.lower() or "SkillArray" in k)
            if has_tcid and has_skills:
                return node
    return None


def _gv(d: dict, *keys: str, default=None):
    """Get a value from a dict trying multiple key variants (raw + stripped backing field)."""
    norm = _normalised_dict(d)
    for k in keys:
        if k in norm:
            return norm[k]
    return default


def _race_scenario_blob(race_dict: dict) -> str | None:
    return _gv(race_dict, "SimDataBase64", "race_scenario", "sim_data_base64", "simDataBase64")


def _chara_result_array(race_dict: dict) -> list:
    val = _gv(race_dict, "CharaResultArray", "chara_result_array", "RaceHorse", "race_horse")
    return val if isinstance(val, list) else []


def _team_id(cr: dict) -> int | None:
    return _gv(cr, "team_id", "TeamId")


def _trained_chara_id(d: dict) -> int | None:
    return _gv(d, "trained_chara_id", "TrainedCharaId")


def _card_id(d: dict) -> int | None:
    return _gv(d, "card_id", "CardId")


def _finish_order(d: dict) -> int | None:
    return _gv(d, "finish_order", "FinishOrder")


def _finish_time(d: dict) -> float | None:
    raw = _gv(d, "finish_time", "FinishTime", "FinishTimeRaw", "finish_time_raw")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _score_array(d: dict) -> list:
    val = _gv(d, "score_array", "ScoreArray")
    return val if isinstance(val, list) else []


def _team_total_score(race: dict) -> int:
    val = _gv(race, "team_total_score", "TeamTotalScore")
    return int(val) if val is not None else 0


def _stats(c: dict) -> dict:
    # Note: horseACT dumps use "pow" in embedded rosters but "power" in veterans.json.
    # We try both.
    return {
        "speed":   _gv(c, "speed",   "Speed"),
        "stamina": _gv(c, "stamina", "Stamina"),
        "power":   _gv(c, "pow",   "power", "Power"),
        "guts":    _gv(c, "guts",    "Guts"),
        "wiz":     _gv(c, "wiz",     "Wiz"),
    }




def _skill_ids_from_array(arr: list) -> list[int]:
    out = []
    for s in arr or []:
        if isinstance(s, dict):
            sid = _gv(s, "skill_id", "SkillId")
            if sid:
                out.append(int(sid))
    return out


def _support_card_bonus(root: Any) -> tuple[int, float] | None:
    for node in _walk_all(root):
        if isinstance(node, dict):
            val = _gv(node, "support_card_bonus", "SupportCardBonus")
            if isinstance(val, int):
                return val, val / 100.0
    return None


# ── Trial processing ────────────────────────────────────────────────────────
def load_veterans(veterans_path: Path) -> dict[int, dict]:
    """Reads horseACT's veterans.json (full roster including stats and skills) and
    returns a {trained_chara_id: chara_entry} lookup.

    The TT dump itself does not include user_trained_chara_array (it is empty/null
    after horseACT's field filtering), so we need this side file to get stats and
    owned_skills per uma. The veterans file is written by horseACT on game launch."""
    if not veterans_path.exists():
        return {}
    try:
        data = json.loads(veterans_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[int, dict] = {}
    for v in data:
        if not isinstance(v, dict):
            continue
        tcid = v.get("trained_chara_id")
        if tcid is None:
            continue
        out[int(tcid)] = v
    return out


def import_trial_file(path: Path, veterans: dict[int, dict] | None = None) -> dict | None:
    """Reads one horseACT TT JSON, returns the analysis dict in our format or None on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"  ! cannot read {path.name}: {e}")
        return None

    try:
        root = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  ! invalid JSON in {path.name}: {e}")
        return None

    race_array = _find_race_array(root)
    if not race_array:
        print(f"  ! could not locate race array in {path.name}")
        return None
    if len(race_array) < 3:
        print(f"  ! race array in {path.name} has only {len(race_array)} entries (need >=3)")
        return None

    # Two sources for roster info, in priority order:
    #   1) embedded user_trained_chara_array in the dump (often empty after horseACT filtering)
    #   2) veterans.json side file (preferred when present)
    chara_skills: dict[int, list[int]] = {}
    chara_data: dict[int, dict] = {}

    if veterans:
        for tcid, v in veterans.items():
            chara_skills[int(tcid)] = _skill_ids_from_array(v.get("skill_array") or [])
            chara_data[int(tcid)] = v

    embedded_roster = _find_user_roster(root) or []
    for c in embedded_roster:
        if not isinstance(c, dict):
            continue
        tcid = _trained_chara_id(c)
        if tcid is None:
            continue
        # Embedded values override veterans (they are more recent)
        chara_skills[int(tcid)] = _skill_ids_from_array(_gv(c, "skill_array", "SkillArray", default=[]))
        chara_data[int(tcid)] = c

    bonus = _support_card_bonus(root)
    support_bonus_raw = bonus[0] if bonus else 0
    support_bonus_pct = bonus[1] if bonus else 0.0

    trial_id = f"ha_{path.stem}"   # stable per file
    results: list[dict] = []

    for race_idx, race in enumerate(race_array):
        if not isinstance(race, dict):
            continue
        scen = _race_scenario_blob(race)
        cra = _chara_result_array(race)
        if not scen or not cra:
            continue

        try:
            parsed = race_scenario.parse_race_scenario(scen)
        except Exception as e:
            print(f"  ! race {race_idx+1} in {path.name}: race_scenario parse failed: {e}")
            continue

        per_horse_acts = race_scenario.skill_activations_per_horse(parsed)
        per_horse_raws = race_scenario.scores_per_horse(parsed)
        per_horse_res = parsed["horse_results"]
        team_total = _team_total_score(race)
        team_raw_sum = sum(per_horse_raws.values())
        race_mult = (team_total / team_raw_sum) if team_raw_sum else 1.0

        for horse_idx, cr in enumerate(cra):
            if not isinstance(cr, dict):
                continue
            if _team_id(cr) != 1:
                continue
            tcid = _trained_chara_id(cr)
            if tcid is None:
                continue
            chara = chara_data.get(int(tcid), {})
            owned = chara_skills.get(int(tcid), [])
            activated = per_horse_acts.get(horse_idx, [])
            hr = per_horse_res[horse_idx] if horse_idx < len(per_horse_res) else {}

            owned_set = set(owned)
            activated_in_pool = [s for s in activated if s in owned_set]
            activated_extra = [s for s in activated if s not in owned_set]

            score_array_entries = _score_array(cr)
            display_score = sum(int(s.get("score", _gv(s, "Score", default=0)) or 0)
                                for s in score_array_entries if isinstance(s, dict))

            results.append({
                "trial_id":          trial_id,
                "race_idx":          race_idx,
                "distance_type":     _gv(race, "distance_type", "DistanceType"),
                "trained_chara_id":  tcid,
                "chara_id":          _card_id(chara),
                "chara_name":        master.chara_name_by_card_id(_card_id(chara)),
                "stats":             _stats(chara),
                "finish_order":      hr.get("finish_order") if hr.get("finish_order") is not None else _finish_order(cr),
                "finish_time":       hr.get("finish_time") if hr.get("finish_time") is not None else _finish_time(cr),
                "running_style":     hr.get("running_style"),
                "raw_score":         per_horse_raws.get(horse_idx, 0),
                "display_score":     display_score,
                "race_multiplier":   round(race_mult, 4),
                "team_total_score":  team_total,
                "support_bonus_raw": support_bonus_raw,
                "support_bonus_pct": support_bonus_pct,
                "owned_skills_n":    len(owned),
                "owned_skills":      owned,
                "activated_skills":  activated_in_pool,
                "activated_extra":   activated_extra,
                "activation_ratio":  (len(activated_in_pool) / len(owned)) if owned else 0,
                "source":            "horseact",
            })

    if not results:
        print(f"  ! no team-id=1 uma rows extracted from {path.name}")
        return None

    # Synthesise a per-trial player_state snapshot from what horseACT does dump.
    # horseACT strips fields like team_stadium_user / ranking / opponent_info, so
    # this snapshot will be sparse — only support bonus + rp_info + last opponent
    # vid (kept by horseACT for now) survive.
    try:
        synthetic_payload = {"data": root}
        ps_snapshot = player_state.extract_state({
            "team_stadium/start":      synthetic_payload,
            "team_stadium/all_race_end": synthetic_payload,
            "team_stadium/index":      synthetic_payload,
            "team_stadium/decide_frame_order": synthetic_payload,
        })
        player_state.append_state(ps_snapshot)
    except Exception as exc:
        print(f"  ! player_state extract failed (non-fatal): {exc}")

    return {"trial_id": trial_id, "per_uma": results}


def _already_imported() -> set[str]:
    if not HISTORY_PATH.exists():
        return set()
    out = set()
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                tid = row.get("trial_id")
                if tid:
                    out.add(tid)
            except Exception:
                continue
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Path not found: {target}")
        sys.exit(1)

    if target.is_dir():
        files = sorted(p for p in target.rglob("TT-*.json") if p.is_file())
        if not files:
            files = sorted(p for p in target.rglob("*.json") if p.is_file())
        search_root = target
    else:
        files = [target]
        search_root = target.parent

    if not files:
        print(f"No .json files found at {target}")
        return

    # Look for veterans.json upward from the search root (usually one level up:
    # <save_root>/Saved races/veterans.json and Team trials/ folder is at the
    # same level).
    veterans_candidates = [
        search_root / "veterans.json",
        search_root.parent / "veterans.json",
        search_root.parent.parent / "veterans.json",
    ]
    veterans_path = next((p for p in veterans_candidates if p.exists()), None)
    veterans: dict[int, dict] = {}
    if veterans_path:
        veterans = load_veterans(veterans_path)
        print(f"Loaded veterans.json with {len(veterans)} trained umas (for stats + skills)")
    else:
        print("No veterans.json found nearby - imported trials will not include stats or owned-skill data")

    DATA_DIR.mkdir(exist_ok=True)
    already = _already_imported()
    print(f"Found {len(files)} JSON file(s) in {target}")
    print(f"Already in history: {len(already)} trials")
    print()

    imported = 0
    skipped = 0
    for path in files:
        trial_id_guess = f"ha_{path.stem}"
        if trial_id_guess in already:
            skipped += 1
            continue
        result = import_trial_file(path, veterans=veterans)
        if not result:
            continue
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            for row in result["per_uma"]:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        print(f"  + imported {path.name}  ({len(result['per_uma'])} uma rows)")
        imported += 1

    print()
    print(f"Imported {imported} trial(s).  Skipped {skipped} already-imported.")
    if imported > 0:
        print("Next: python dashboard_server.py")


if __name__ == "__main__":
    main()
