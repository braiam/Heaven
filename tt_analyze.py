"""Team Trials per-uma analyzer.

For your team (team_id=1) umas across captured Team Trials:
  - Skills owned (from skill_array in user_trained_chara_array_copy)
  - Skills activated this race (from race_scenario events)
  - Activation ratio
  - Cross-trial: avg finish position, % top-3, list of skills that
    NEVER activated across all observed runs.

Run after a Team Trials capture:
    python tt_analyze.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import master
import race_scenario
import player_state
import stadium_tracker


DATA_DIR = Path(__file__).parent / "data"
HISTORY_PATH = DATA_DIR / "team_trials_history.jsonl"


def skill_name(skill_id: int) -> str:
    return master.skill_name(skill_id)


def chara_name(chara_id: int) -> str:
    return master.chara_name(chara_id)


def chara_name_from_card_id(card_id: int | None) -> str:
    if not card_id:
        return "?"
    return master.chara_name_by_card_id(card_id)


def analyze_team_trial(payload_start: dict, payload_dfo: dict, trial_id: str = "") -> dict:
    """Combine team_stadium/start (race_result_array w/ scenarios) and decide_frame_order
    (full chara info including skill_array) into per-uma per-race breakdown."""
    sd = payload_start.get("data", payload_start)
    dfo = payload_dfo.get("data", payload_dfo)

    # Player-specific support card bonus (varies per player based on owned cards).
    # Encoded as int where 1425 = +14.25%. Used by the game's multiplier formula.
    support_bonus_raw = sd.get("support_card_bonus") or 0
    support_bonus_pct = support_bonus_raw / 100.0

    # Build lookup: trained_chara_id -> (chara_data, owned_skill_ids)
    chara_lookup: dict[int, dict] = {}
    for c in (dfo.get("user_trained_chara_array_copy") or []):
        tcid = c.get("trained_chara_id")
        if tcid is None:
            continue
        skills = [s.get("skill_id") for s in (c.get("skill_array") or []) if isinstance(s, dict)]
        chara_lookup[tcid] = {
            "chara_data":  c,
            "owned_skills": [s for s in skills if s],
        }

    results = []
    for race_idx, race in enumerate(sd.get("race_result_array") or []):
        cra = race.get("chara_result_array") or []
        scen = race.get("race_scenario")
        if not scen:
            continue
        parsed = race_scenario.parse_race_scenario(scen)
        per_horse_activations = race_scenario.skill_activations_per_horse(parsed)
        per_horse_scores = race_scenario.scores_per_horse(parsed)
        per_horse_results = parsed["horse_results"]

        # Per-race team multiplier (kept for backward compat / debugging).
        team_raw_sum = sum(per_horse_scores.values())
        team_total = race.get("team_total_score") or 0
        race_multiplier = (team_total / team_raw_sum) if team_raw_sum else 1.0

        for horse_idx, cr in enumerate(cra):
            if cr.get("team_id") != 1:   # YOUR team only
                continue
            tcid = cr.get("trained_chara_id")
            lookup = chara_lookup.get(tcid) or {}
            chara = lookup.get("chara_data") or {}
            owned = lookup.get("owned_skills") or []
            activated = per_horse_activations.get(horse_idx, [])
            hr = per_horse_results[horse_idx] if horse_idx < len(per_horse_results) else {}

            owned_set = set(owned)
            activated_in_pool = [s for s in activated if s in owned_set]
            activated_extra = [s for s in activated if s not in owned_set]   # debuffs/etc

            # Exact in-game display score per uma: sum of base scores in chara_result_array[i].score_array.
            # Each entry corresponds to a scoring event (raw_score_id). Bonuses are computed
            # separately and applied at team level, so the per-uma display is just sum(score).
            score_array = cr.get("score_array") or []
            display_score_exact = sum(s.get("score", 0) for s in score_array if isinstance(s, dict))

            results.append({
                "trial_id":      trial_id,
                "race_idx":      race_idx,
                "distance_type": race.get("distance_type"),
                "trained_chara_id": tcid,
                "chara_id":      chara.get("card_id"),
                "chara_name":    chara_name_from_card_id(chara.get("card_id")),
                "stats": {
                    "speed":   chara.get("speed"),
                    "stamina": chara.get("stamina"),
                    "power":   chara.get("power"),
                    "guts":    chara.get("guts"),
                    "wiz":     chara.get("wiz"),
                },
                "finish_order":      hr.get("finish_order"),
                "finish_time":       hr.get("finish_time"),
                "running_style":     hr.get("running_style"),
                "raw_score":         per_horse_scores.get(horse_idx, 0),
                "display_score":     display_score_exact,
                "race_multiplier":   round(race_multiplier, 4),
                "team_total_score":  team_total,
                "support_bonus_raw": support_bonus_raw,
                "support_bonus_pct": support_bonus_pct,
                "owned_skills_n":    len(owned),
                "owned_skills":      owned,
                "activated_skills":  activated_in_pool,
                "activated_extra":   activated_extra,
                "activation_ratio":  (len(activated_in_pool) / len(owned)) if owned else 0,
            })
    return {"per_uma": results, "support_bonus_pct": support_bonus_pct, "support_bonus_raw": support_bonus_raw}


def append_history(analysis: dict) -> None:
    """Append per-uma rows to history file for cross-trial summary."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        for row in analysis["per_uma"]:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def cross_trial_summary() -> dict:
    """Aggregate across all logged Team Trials."""
    if not HISTORY_PATH.exists():
        return {}
    by_chara: dict[int, list] = defaultdict(list)
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                by_chara[row["trained_chara_id"]].append(row)
            except Exception:
                continue
    summary: dict = {}
    for tcid, rows in by_chara.items():
        n = len(rows)
        finishes = [r["finish_order"] for r in rows if r.get("finish_order") is not None]
        avg_finish = sum(finishes) / len(finishes) if finishes else None
        top3 = sum(1 for f in finishes if f <= 2)  # 0-indexed: top3 = 0/1/2
        wins = sum(1 for f in finishes if f == 0)
        # Skill activation consistency: which skills NEVER activated across appearances
        never_activated: set = set()
        ever_activated: set = set()
        for r in rows:
            owned = set(r.get("owned_skills") or [])
            activated = set(r.get("activated_skills") or [])
            never_activated |= (owned - activated)
            ever_activated  |= activated
        never_activated -= ever_activated
        summary[tcid] = {
            "appearances":      n,
            "avg_finish":       avg_finish,
            "wins":             wins,
            "top3":             top3,
            "win_pct":          (wins / n * 100) if n else 0,
            "top3_pct":         (top3 / n * 100) if n else 0,
            "never_activated":  sorted(never_activated),
            "chara_name":       rows[0].get("chara_name"),
        }
    return summary


def _split_into_trials(records: list[dict]) -> list[list[dict]]:
    """Group records into separate trial groups.

    Real captured sequence per trial is:
      opponent_list -> decide_frame_order -> start -> replay_check -> all_race_end

    So we close a trial group AFTER all_race_end. Anything trailing (e.g. an
    opened opponent_list for the next trial that wasn't finished) becomes its
    own group too, but only kept if it has both start and decide_frame_order."""
    trials: list[list[dict]] = []
    current: list[dict] = []
    for r in records:
        current.append(r)
        if r.get("endpoint") == "team_stadium/all_race_end":
            trials.append(current)
            current = []
    if current:
        trials.append(current)
    return [
        t for t in trials
        if any(x.get("endpoint") == "team_stadium/start" for x in t)
        and any(x.get("endpoint") == "team_stadium/decide_frame_order" for x in t)
    ]


def _already_processed_trial_ids() -> set:
    """Read history and return the set of trial_ids already in there.
    Used to skip re-processing the same trial twice."""
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
    raw_full = DATA_DIR / "raw_full.jsonl"
    if not raw_full.exists():
        print(f"No data at {raw_full}")
        return
    with open(raw_full, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    trials = _split_into_trials(records)
    if not trials:
        print("No complete trials found (need both team_stadium/start and team_stadium/decide_frame_order)")
        return

    processed = _already_processed_trial_ids()
    print(f"Found {len(trials)} Team Trial(s) in raw_full.jsonl  ({len(processed)} already in history)")
    print()

    new_count = 0
    for i, trial_records in enumerate(trials, 1):
        start = next(r for r in trial_records if r["endpoint"] == "team_stadium/start")
        dfo_candidates = [r for r in trial_records if r["endpoint"] == "team_stadium/decide_frame_order"]
        dfo = dfo_candidates[-1]

        # Stable trial_id from the start payload's timestamp (unique per trial)
        trial_id = f"t_{int(start.get('ts', 0))}"

        if trial_id in processed:
            print(f"-----  TRIAL {i}/{len(trials)} already processed (trial_id={trial_id}), skipping  -----")
            continue

        print(f"{'#' * 5}  PROCESSING TRIAL {i}/{len(trials)} (trial_id={trial_id})  {'#' * 5}")

        # Snapshot player-specific state for this trial
        payloads_by_ep = {r["endpoint"]: r["payload"] for r in trial_records}
        ps = player_state.extract_state(payloads_by_ep)
        player_state.append_state(ps)

        analysis = analyze_team_trial(start["payload"], dfo["payload"], trial_id=trial_id)
        _print_and_save(analysis, i, len(trials))
        new_count += 1

    if new_count == 0:
        print("All trials in raw_full.jsonl were already processed. Nothing new added to history.")
    else:
        print(f"\nAdded {new_count} new trial(s) to history.")

    # Stadium tracker — record per-round track/weather/gate observations
    my_vid = None
    try:
        latest = player_state.latest_state()
        if latest:
            my_vid = latest.get("viewer_id")
    except Exception:
        pass
    saved = stadium_tracker.ingest_payloads(records, my_viewer_id=my_vid)
    if saved:
        print(f"\nStadium tracker: recorded {saved} new trial observation(s).")

    _print_cross_summary()


def _print_and_save(analysis: dict, trial_num: int = 1, total_trials: int = 1) -> None:
    print("=" * 80)
    print(f"Your team performance ({len(analysis['per_uma'])} uma usages)")
    print(f"Your support card bonus: +{analysis['support_bonus_pct']:.2f}%  (raw {analysis['support_bonus_raw']})")
    print("=" * 80)
    for row in analysis["per_uma"]:
        print(f"\nRace {row['race_idx']+1} (dist_type={row['distance_type']}) -"
              f"{row['chara_name']} (trained_chara_id={row['trained_chara_id']})")
        s = row["stats"]
        print(f"  Stats: SPD={s['speed']} STA={s['stamina']} POW={s['power']} GTS={s['guts']} WIT={s['wiz']}")
        print(f"  Finish #{row['finish_order']+1 if row['finish_order'] is not None else '?'} "
              f"time={row['finish_time']:.2f}s  style={row['running_style']}  "
              f"raw_score={row['raw_score']:,}")
        n_act = len(row["activated_skills"])
        n_own = row["owned_skills_n"]
        ratio = row["activation_ratio"]
        print(f"  Skills: {n_act}/{n_own} activated ({ratio*100:.0f}%)")
        if row["activated_skills"]:
            names = [skill_name(s) for s in row["activated_skills"][:8]]
            print(f"    Activated: {', '.join(names)}")
        not_activated = sorted(set(row["owned_skills"]) - set(row["activated_skills"]))
        if not_activated:
            names = [skill_name(s) for s in not_activated[:8]]
            print(f"    Not activated: {', '.join(names)}{'...' if len(not_activated) > 8 else ''}")
        if row["activated_extra"]:
            names = [skill_name(s) for s in row["activated_extra"][:5]]
            print(f"    (Extra/debuff activations: {', '.join(names)})")

    # Persist for cross-trial history
    append_history(analysis)


def _print_cross_summary() -> None:
    """Print the consistency summary across ALL logged trials."""
    summary = cross_trial_summary()
    if not any(s["appearances"] > 1 for s in summary.values()):
        return
    print()
    print("=" * 80)
    print("Cross-trial consistency summary (across all logged trials)")
    print("=" * 80)
    rows = sorted(summary.items(), key=lambda kv: -kv[1]["appearances"])
    for tcid, s in rows:
        if s["appearances"] < 2:
            continue
        print(f"\n{s['chara_name']} (id={tcid})  n={s['appearances']}  "
              f"avg_finish={s['avg_finish']+1:.1f}  wins={s['wins']}  top3={s['top3']}")
        if s["never_activated"]:
            names = [skill_name(sk) for sk in s["never_activated"][:10]]
            print(f"  Skills NEVER activated in {s['appearances']} runs: {', '.join(names)}")


if __name__ == "__main__":
    main()
