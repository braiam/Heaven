"""Stadium round tracker — collects per-round race/track/weather data
from team_stadium/start payloads and persists deduped observations.

For each Team Trial captured, every round contains:
  - track / distance / surface / handedness / inout
  - weather, ground condition, season
  - random_seed
  - which gate (frame_order) each of your umas started in

Output:
  data/stadium_observations.jsonl   one line per unique trial
                                    (5 rounds wrapped in an observation)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import master

DATA_DIR = Path(__file__).parent / "data"
OBSERVATIONS_PATH = DATA_DIR / "stadium_observations.jsonl"
# Public community seed shipped with the repo (track/weather/condition only —
# no rosters, no account data). Loaded alongside your private local file.
COMMUNITY_SEED_PATH = DATA_DIR / "community_seed.jsonl"


# ── Static label maps (also live in Nguyen's helper; defined here so this
#    module stays self-contained and doesn't depend on a 750KB file). ───
WEATHER_MAP          = {1: "Sunny",   2: "Cloudy", 3: "Rain",  4: "Snow"}
GROUND_CONDITION_MAP = {1: "Firm",    2: "Good",   3: "Soft",  4: "Heavy"}
SEASON_MAP           = {1: "Spring",  2: "Summer", 3: "Autumn", 4: "Winter",
                        5: "Spring"}  # in-game "Early Spring" rolls up into Spring
SURFACE_MAP          = {1: "Turf",    2: "Dirt"}
HANDEDNESS_MAP       = {1: "Right",   2: "Left",   4: "Straight"}
INOUT_MAP            = {1: "Inner",   2: "Outer",  3: "Standard"}


# ── master.mdb enrichment ───────────────────────────────────────────────
_course_cache: dict[int, dict] = {}


def _load_course_map() -> dict[int, dict]:
    """For each race_instance_id, resolve track + distance + surface + etc.
    via the chain: race_instance -> race -> race_course_set -> race_track.
    Cached after first call."""
    if _course_cache:
        return _course_cache
    p = master.find_mdb()
    if not p:
        return _course_cache

    db = sqlite3.connect(str(p))
    try:
        # Track names (cat 35) and race names (cat 28 long, 29 short)
        track_name = {int(i): t for i, t in
                      db.execute('SELECT "index", text FROM text_data WHERE category=35')}
        race_name_long = {int(i): t for i, t in
                          db.execute('SELECT "index", text FROM text_data WHERE category=28')}
        race_name_short = {int(i): t for i, t in
                           db.execute('SELECT "index", text FROM text_data WHERE category=29')}

        # course_set -> attrs
        course_sets = {}
        for cs_id, track_id, dist, ground, inout, turn in db.execute(
            "SELECT id, race_track_id, distance, ground, inout, turn FROM race_course_set"
        ):
            course_sets[cs_id] = {
                "race_track_id":  track_id,
                "track":          track_name.get(track_id, f"track#{track_id}"),
                "distance":       dist,
                "surface_id":     ground,
                "surface":        SURFACE_MAP.get(ground, f"surface#{ground}"),
                "inout_id":       inout,
                "inout":          INOUT_MAP.get(inout, str(inout)),
                "handedness_id":  turn,
                "handedness":     HANDEDNESS_MAP.get(turn, f"turn#{turn}"),
            }

        # race -> course_set, grade
        races = {}
        for rid, grade, cs_id, entry_num in db.execute(
            "SELECT id, grade, course_set, entry_num FROM race"
        ):
            races[rid] = {
                "race_id":       rid,
                # Many Team Trial races have no displayed name (matchmaking
                # tier races, not championships). Leave blank for those.
                "race_name":     race_name_long.get(rid, race_name_short.get(rid, "")),
                "grade":         grade,
                "course_set_id": cs_id,
                "entry_num":     entry_num,
            }

        # race_instance -> race_id
        for ri_id, race_id in db.execute("SELECT id, race_id FROM race_instance"):
            race = races.get(race_id, {})
            cs = course_sets.get(race.get("course_set_id"), {})
            _course_cache[int(ri_id)] = {
                **cs,
                "race_id":     race.get("race_id"),
                "race_name":   race.get("race_name", ""),
                "grade":       race.get("grade", ""),
                "entry_num":   race.get("entry_num", ""),
            }
    finally:
        db.close()
    return _course_cache


# ── Extraction from raw payloads ───────────────────────────────────────
def _walk_dicts(node: Any):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def _find_rounds(payload: Any) -> list[dict] | None:
    """Walk to find race_start_params_array (a list of 5 round dicts)."""
    for obj in _walk_dicts(payload):
        rsa = obj.get("race_start_params_array")
        if isinstance(rsa, list) and rsa and isinstance(rsa[0], dict) \
           and "race_instance_id" in rsa[0]:
            return rsa
    return None


def _round_key(rounds: list[dict]) -> str:
    """Stable hash across the 5 rounds so we can dedupe duplicate captures."""
    parts = [{
        "round":             r.get("round"),
        "race_instance_id":  r.get("race_instance_id"),
        "weather":           r.get("weather"),
        "ground_condition":  r.get("ground_condition"),
        "random_seed":       r.get("random_seed"),
    } for r in rounds]
    return hashlib.sha1(
        json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def distance_category(distance: int | None, surface: str | None) -> str:
    """Standard Uma Musume distance bucket.
    Dirt races collapse into their own bucket regardless of length;
    Turf races split into Sprint / Mile / Medium / Long by yardage."""
    if not distance:
        return "Unknown"
    if surface == "Dirt":
        return "Dirt"
    if distance <= 1400:
        return "Sprint"
    if distance <= 1800:
        return "Mile"
    if distance <= 2400:
        return "Medium"
    return "Long"


CATEGORY_ORDER = ["Sprint", "Mile", "Medium", "Long", "Dirt"]


def enrich_round(raw_round: dict, my_viewer_id: int | None = None) -> dict:
    """Produce a UI-friendly enriched round dict."""
    course_map = _load_course_map()
    ri_id = raw_round.get("race_instance_id")
    course = course_map.get(ri_id, {})

    # My umas' frame_order (starting gate). Each round has 12 horses; mine are
    # the ones whose viewer_id matches the user's. We don't store opponent
    # PII — only my own umas' gates.
    my_gates = []
    if my_viewer_id:
        for h in raw_round.get("race_horse_data_array", []) or []:
            if h.get("viewer_id") == my_viewer_id:
                my_gates.append({
                    "trained_chara_id": h.get("trained_chara_id"),
                    "chara_id":         h.get("chara_id"),
                    "card_id":          h.get("card_id"),
                    "running_style":    h.get("running_style"),
                    "frame_order":      h.get("frame_order"),
                })
        my_gates.sort(key=lambda g: g.get("frame_order") or 99)

    weather_id = raw_round.get("weather")
    ground_id = raw_round.get("ground_condition")
    season_id = raw_round.get("season")

    dist = course.get("distance")
    surf = course.get("surface", "")
    return {
        "round":                raw_round.get("round"),
        "race_instance_id":     ri_id,
        "race_id":              course.get("race_id"),
        "race_name":            course.get("race_name", ""),
        "track":                course.get("track", ""),
        "distance":             dist,
        "distance_category":    distance_category(dist, surf),
        "surface":              surf,
        "handedness":           course.get("handedness", ""),
        "inout":                course.get("inout", ""),
        "weather":              WEATHER_MAP.get(weather_id, f"weather#{weather_id}"),
        "ground_condition":     GROUND_CONDITION_MAP.get(ground_id, f"cond#{ground_id}"),
        "season":               SEASON_MAP.get(season_id, f"season#{season_id}"),
        "random_seed":          raw_round.get("random_seed"),
        "grade":                course.get("grade"),
        "entry_num":            course.get("entry_num"),
        "my_gates":             my_gates,
    }


# ── Persistence ─────────────────────────────────────────────────────────
def _load_seen_keys() -> set[str]:
    """Packet keys already known — across both the community seed and the
    local file — so ingestion / import never duplicates an existing trial."""
    seen = set()
    for path in (COMMUNITY_SEED_PATH, OBSERVATIONS_PATH):
        for item in _read_jsonl(path):
            k = item.get("packet_key")
            if k:
                seen.add(k)
    return seen


def ingest_payloads(payloads: list[dict], my_viewer_id: int | None = None) -> int:
    """Scan a list of decoded payload dicts (each {"endpoint":..., "payload":...})
    for team_stadium/start responses and append new observations.
    Returns the number of new observations saved."""
    OBSERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen = _load_seen_keys()
    saved = 0

    for entry in payloads:
        if entry.get("endpoint") != "team_stadium/start":
            continue
        rounds = _find_rounds(entry.get("payload", {}))
        if not rounds:
            continue
        key = _round_key(rounds)
        if key in seen:
            continue

        enriched = [enrich_round(r, my_viewer_id) for r in rounds]
        observation = {
            "packet_key":     key,
            "collected_at":   time.strftime("%Y-%m-%d %H:%M:%S"),
            "trial_ts":       entry.get("ts"),
            "round_count":    len(enriched),
            "rounds":         enriched,
        }
        with OBSERVATIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(observation, ensure_ascii=False, separators=(",", ":")) + "\n")
        seen.add(key)
        saved += 1

    return saved


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_observations() -> list[dict]:
    """Combine the public community seed with the user's local observations,
    deduped by packet_key (local wins on collision)."""
    seed = _read_jsonl(COMMUNITY_SEED_PATH)
    local = _read_jsonl(OBSERVATIONS_PATH)
    by_key: dict[str, dict] = {}
    for obs in seed + local:   # local after seed so it overrides duplicates
        k = obs.get("packet_key")
        if k:
            by_key[k] = obs
        else:
            by_key[id(obs)] = obs
    return list(by_key.values())


# ── Aggregate stats for the dashboard ──────────────────────────────────
def flatten_rounds() -> list[dict]:
    """Return one row per round (across all observations) for table display."""
    rows = []
    for obs in load_observations():
        for r in obs.get("rounds", []):
            rows.append({
                "trial_ts":         obs.get("trial_ts"),
                "collected_at":     obs.get("collected_at"),
                **r,
            })
    return rows


def summary(category: str | None = None) -> dict:
    """Frequency aggregates over collected rounds.
    If `category` is given (Sprint/Mile/Medium/Long/Dirt), the aggregates are
    scoped to only that subset; otherwise everything is included.
    The `categories` field always reflects the FULL dataset so the UI can
    show distance-category percentages while another category is filtered.
    """
    all_rows = flatten_rounds()
    rows = [r for r in all_rows
            if not category or r.get("distance_category") == category]
    total = len(rows)
    grand_total = len(all_rows)

    # Category breakdown (always over the full unfiltered set)
    cat_counts: dict = {}
    for r in all_rows:
        c = r.get("distance_category") or "Unknown"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    categories = [
        {"value": c, "count": cat_counts.get(c, 0),
         "pct": round(cat_counts.get(c, 0) * 100 / grand_total, 2) if grand_total else 0}
        for c in CATEGORY_ORDER if cat_counts.get(c, 0) > 0
    ]
    # Append any unexpected categories (shouldn't happen but just in case)
    for c, n in cat_counts.items():
        if c not in CATEGORY_ORDER:
            categories.append({"value": c, "count": n,
                               "pct": round(n * 100 / grand_total, 2) if grand_total else 0})

    if total == 0:
        return {"total_rounds": 0, "grand_total_rounds": grand_total,
                "category_filter": category, "categories": categories,
                "tracks": [], "weather": [], "ground": [], "distance": [],
                "surface": [], "handedness": [], "season": [], "gates": []}

    # Distance tile groups by (distance, category) pair so the same yardage on
    # turf vs dirt does not collapse into one row.
    dist_combo: dict = {}
    for r in rows:
        d = r.get("distance")
        cat = r.get("distance_category") or ""
        if d in (None, ""):
            continue
        key = (d, cat)
        dist_combo[key] = dist_combo.get(key, 0) + 1
    distance_rows = [
        {"value": f"{k[0]}m" + (f" · {k[1]}" if k[1] else ""),
         "distance": k[0], "category": k[1], "count": v,
         "pct": round(v * 100 / total, 2)}
        for k, v in sorted(dist_combo.items(), key=lambda x: -x[1])
    ]

    def freq(field: str) -> list[dict]:
        c: dict = {}
        for r in rows:
            v = r.get(field)
            if v in (None, ""):
                continue
            c[v] = c.get(v, 0) + 1
        return [{"value": k, "count": v, "pct": round(v * 100 / total, 2)}
                for k, v in sorted(c.items(), key=lambda x: -x[1])]

    # Track + distance + surface fused for nicer top list
    track_combo: dict = {}
    for r in rows:
        key = (r.get("track", ""), r.get("distance"), r.get("surface", ""))
        if not key[0]:
            continue
        track_combo[key] = track_combo.get(key, 0) + 1
    tracks = [{"track": k[0], "distance": k[1], "surface": k[2],
               "count": v, "pct": round(v * 100 / total, 2)}
              for k, v in sorted(track_combo.items(), key=lambda x: -x[1])]

    # Gate frequency for my umas (across all rounds where I appear)
    gate_counts: dict = {}
    gate_rounds = 0
    for r in rows:
        for g in r.get("my_gates", []) or []:
            fo = g.get("frame_order")
            if fo is None:
                continue
            gate_counts[fo] = gate_counts.get(fo, 0) + 1
            gate_rounds += 1
    gates = [{"gate": k, "count": v,
              "pct": round(v * 100 / gate_rounds, 2) if gate_rounds else 0}
             for k, v in sorted(gate_counts.items())]

    return {
        "total_rounds":       total,
        "grand_total_rounds": grand_total,
        "category_filter":    category,
        "categories":         categories,
        "tracks":             tracks,
        "weather":            freq("weather"),
        "ground":             freq("ground_condition"),
        "distance":           distance_rows,
        "surface":            freq("surface"),
        "handedness":         freq("handedness"),
        "season":             freq("season"),
        "gates":              gates,
    }


# ── CSV export ──────────────────────────────────────────────────────────
def to_csv() -> str:
    """Return all rounds as a CSV string for download."""
    import csv
    import io
    rows = flatten_rounds()
    fields = ["trial_ts", "collected_at", "round", "distance_category",
              "race_name", "track", "distance", "surface", "handedness",
              "inout", "weather", "ground_condition", "season", "random_seed",
              "race_instance_id", "race_id", "grade", "entry_num", "my_gate_1",
              "my_gate_2", "my_gate_3", "my_chara_1", "my_chara_2", "my_chara_3"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        gates = r.get("my_gates", []) or []
        flat = {**r}
        for i, g in enumerate(gates[:3]):
            flat[f"my_gate_{i+1}"] = g.get("frame_order")
            flat[f"my_chara_{i+1}"] = g.get("trained_chara_id")
        w.writerow(flat)
    return buf.getvalue()
