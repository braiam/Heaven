"""
Heaven — unified web UI.
Merges Heir (breeding optimizer, port 1620) and TTAnalyzer (Team Trials
dashboard, port 7434) into a single FastAPI server.

Run:
    python server.py            # -> http://127.0.0.1:1620
"""

import json
import os
import statistics
import sys
import threading
import traceback
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import heir
import master
import fetch as fetch_mod
import skill_planner
import player_state
import stadium_tracker
import tt_capture

from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

NOTES_PATH = HERE / "notes.json"
PORT = 1620

app = FastAPI(title="Heaven")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
STATE = {"ds": None, "fmap": None, "skills": None, "source": None}

# ── TTAnalyzer data paths ─────────────────────────────────────────────────
DATA_DIR = HERE / "data"
HISTORY_PATH = DATA_DIR / "team_trials_history.jsonl"

# ── Icon cache ────────────────────────────────────────────────────────────
ICON_CACHE = HERE / "data" / "icons"
ICON_CACHE.mkdir(parents=True, exist_ok=True)


def icon_urls(card_id: int) -> list[str]:
    chara_id = card_id // 100
    return [
        f"https://gametora.com/images/umamusume/characters/chara_stand_{chara_id}_{card_id}.png",
    ]


# ── TTAnalyzer act% cache ────────────────────────────────────────────────
import time as _time
import urllib.request as _urlreq

_ACT_CACHE: dict = {}          # key=(dist,style) -> {"data":{...}, "ts":float}
_ACT_CACHE_TTL = 300           # 5 min
_ACT_EXPORT: dict | None = None  # loaded from skill_export.json if available
ACT_EXPORT_PATH = HERE / "data" / "skill_export.json"

STYLE_KEYWORDS = {
    "Front Runner": ["front runner", "escape", "nige"],
    "Pace Chaser":  ["pace chaser", "pacer", "senkou"],
    "Late Surger":  ["late surger", "sashi"],
    "End Closer":   ["end closer", "closer", "oikomi"],
}
STYLE_TO_CODE = {
    "Front Runner": "NIGE", "Pace Chaser": "SENKO",
    "Late Surger": "SASHI", "End Closer": "OIKOMI",
}
DIST_TO_CODE = {"Sprint": 1, "Mile": 2, "Medium": 3, "Long": 4, "Dirt": 5}


def _load_act_export() -> dict | None:
    """Load skill_export.json (exported from TTAnalyzer) as fallback."""
    global _ACT_EXPORT
    if _ACT_EXPORT is not None:
        return _ACT_EXPORT
    if ACT_EXPORT_PATH.exists():
        try:
            _ACT_EXPORT = json.loads(ACT_EXPORT_PATH.read_text(encoding="utf-8"))
            print(f"[*] Loaded skill export: {len(_ACT_EXPORT.get('groups', []))} groups, "
                  f"{len(_ACT_EXPORT.get('universal', []))} universal")
            return _ACT_EXPORT
        except Exception:
            pass
    _ACT_EXPORT = {}
    return {}


def _act_rates_from_export(distance_type: int, running_style: str) -> dict:
    """Build act_rates response from the local export file."""
    export = _load_act_export()
    if not export:
        return {}
    # Find the matching group
    group = None
    for g in export.get("groups", []):
        if g["distance_type"] == distance_type and g["style_code"] == running_style:
            group = g
            break
    if not group:
        return {}
    skills = {s["name"]: s["act_pct"] for s in group.get("skills", [])}
    universal = {s["name"]: s["act_pct"] for s in export.get("universal", [])}
    return {"skills": skills, "universal": universal,
            "observations": group.get("observations", 0)}


def fetch_act_rates(distance_type: int, running_style: str) -> dict:
    """Fetch act% -- calls skill_planner DIRECTLY (no HTTP needed).
    Falls back to local export if no history data."""
    key = (distance_type, running_style)
    cached = _ACT_CACHE.get(key)
    if cached and _time.time() - cached["ts"] < _ACT_CACHE_TTL:
        return cached["data"]

    # Direct call to skill_planner (no HTTP needed!)
    rows = skill_planner.load_history()
    if not rows:
        # Fallback to local export
        data = _act_rates_from_export(distance_type, running_style)
        if data:
            _ACT_CACHE[key] = {"data": data, "ts": _time.time()}
        return data

    ds_stats = skill_planner.by_dist_style(rows).get((distance_type, running_style), {})
    skills = {}
    for sid, counts in ds_stats.items():
        if counts["owned"] < 5:
            continue
        pct = round((counts["activated"] / counts["owned"]) * 100, 1)
        skills[skill_planner.skill_name(sid)] = pct

    # Universal skills
    all_stats = skill_planner.by_all(rows).get("ALL", {})
    act_styles = skill_planner._activation_styles(rows)
    act_dists = skill_planner._activation_distances(rows)
    universal_races = max((s["owned"] for s in all_stats.values()), default=0)
    min_obs = max(5, universal_races // 5)
    universal = {}
    for sid, counts in all_stats.items():
        if counts["owned"] < min_obs:
            continue
        pct = round((counts["activated"] / counts["owned"]) * 100, 1)
        if (pct >= 70
                and len(act_styles.get(sid, set())) >= 3
                and len(act_dists.get(sid, set())) >= 3):
            universal[skill_planner.skill_name(sid)] = pct

    data = {"skills": skills, "universal": universal,
            "observations": sum(c["owned"] for c in ds_stats.values())}
    _ACT_CACHE[key] = {"data": data, "ts": _time.time()}
    return data


def compute_skill_weights(act_data: dict, style_name: str | None = None,
                          strict_pct: float | None = None) -> dict | None:
    """Build {skill_name_lower: weight} from TTAnalyzer act% data + style keywords.
    Returns None if no act_data (= fallback to unweighted scoring)."""
    skills_pct = act_data.get("skills", {})
    universal_pct = act_data.get("universal", {})
    if not skills_pct and not universal_pct:
        return None

    # Merge all known skills
    all_skills: dict[str, float] = {}  # name_lower -> act_pct
    for name, pct in skills_pct.items():
        all_skills[name.lower()] = pct
    for name, pct in universal_pct.items():
        nl = name.lower()
        if nl not in all_skills:
            all_skills[nl] = pct

    # Style keywords for matching
    style_kw = [k.lower() for k in STYLE_KEYWORDS.get(style_name or "", [])]

    weights: dict[str, float] = {}
    for name_lower, pct in all_skills.items():
        # Strict filter
        if strict_pct is not None and pct < strict_pct:
            weights[name_lower] = 0.0
            continue

        # Base weight from act%
        if pct >= 80:
            w = 1.5
        elif pct >= 60:
            w = 1.0
        elif pct >= 40:
            w = 0.5
        else:
            w = 0.3

        # Style-match multiplier
        if style_kw and any(kw in name_lower for kw in style_kw):
            w *= 2.5

        # Universal bonus (only if not already style-boosted)
        elif name_lower in {n.lower() for n in universal_pct}:
            w = max(w, 1.2)

        weights[name_lower] = round(w, 2)

    return weights


def load_notes():
    if NOTES_PATH.exists():
        try:
            return json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_notes(notes):
    NOTES_PATH.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_dataset(force=False):
    if STATE["ds"] is not None and not force:
        return STATE["ds"]
    STATE["fmap"] = heir.load_factor_map()
    path = heir.find_trace(None)
    if not path or not path.exists():
        STATE["ds"] = {"mine": [], "rentable": []}
        STATE["source"] = None
        return STATE["ds"]
    STATE["ds"] = heir.load_dataset_from_trace(path, STATE["fmap"])
    STATE["skills"] = heir.all_spark_names(STATE["ds"])
    STATE["source"] = str(path)
    return STATE["ds"]


_INHERITANCE_BASE = {
    # base inheritance odds per star (no compatibility) per Polaris / Cygames
    # patent, documented in Crazyfellow's Parenting & Gene guide (page 87).
    # multiplied by (1 + individual_compatibility / 100) at proc time.
    # Blue stats added per uma.moe & Hakuraku (can fail to proc, not guaranteed).
    "blue":     {1: 0.70, 2: 0.80, 3: 0.90},
    "stat":     {1: 0.70, 2: 0.80, 3: 0.90},    # alias
    "aptitude": {1: 0.01, 2: 0.03, 3: 0.05},
    "unique":   {1: 0.05, 2: 0.10, 3: 0.15},
    "skill":    {1: 0.03, 2: 0.06, 3: 0.09},
    "scenario": {1: 0.03, 2: 0.06, 3: 0.09},
    "race":     {1: 0.01, 2: 0.02, 3: 0.03},
}

# ── White spark generation chance (Hakuraku piecewise fit, p≥0.79) ───────
# Predicts probability a skill becomes a factor based on lineage count.
# Piecewise-at-2: smaller boost for first 1-2 copies, larger after.
_WHITE_SPARK_GEN = {
    "white":  {"base": 0.20, "boost_low": 0.020, "boost_high": 0.0275},
    "circle": {"base": 0.25, "boost_low": 0.025, "boost_high": 0.034375},
    "gold":   {"base": 0.40, "boost_low": 0.040, "boost_high": 0.055},
}

def white_spark_gen_chance(tier: str, lineage_count: int) -> float:
    """Probability a skill becomes a white spark, given lineage copies.
    tier: 'white' (plain), 'circle' (double circle), 'gold'.
    Uses Hakuraku's piecewise fit (chi² p≥0.79 on 100K+ observations)."""
    cfg = _WHITE_SPARK_GEN.get(tier, _WHITE_SPARK_GEN["white"])
    n = max(0, lineage_count)
    if n <= 2:
        return cfg["base"] + cfg["boost_low"] * n
    return cfg["base"] + cfg["boost_low"] * 2 + cfg["boost_high"] * (n - 2)

def inspiration_pct(cat, total_stars):
    """Approximate cumulative inheritance proc chance for a factor across
    the trainee + 2 parents (own + position-10 + position-20 grandparents).
    Uses Polaris-documented base rates and ignores compatibility (so this
    is the floor -- real chance scales with affinity)."""
    s = total_stars or 0
    if s <= 0 or cat not in _INHERITANCE_BASE:
        return None
    # split total stars into 1-3 contributing instances of up to 3 star each
    instances = max(1, min(3, (s + 2) // 3))
    per = max(1, min(3, s // instances))
    rate = _INHERITANCE_BASE[cat].get(per, _INHERITANCE_BASE[cat][3])
    cum = 1 - (1 - rate) ** instances
    return round(cum * 100, 1)


# ── Spark Proc Odds (hakuraku-style) ──────────────────────────────────────
# During breeding, the offspring gets 2 inheritance events at the start of the
# run plus additional random "inspiration" events.  Each parent contributes its
# own sparks.  For each factor on a parent, the per-event proc chance is:
#
#   p = base_rate(category, stars) x (1 + parent_compatibility / 100)
#
# where parent_compatibility is CR(trainee, parent) + WSB(parent).
# The game checks each factor independently on each event.
#
# "Over a full run" approximates ~8 total inheritance checks (2 main + ~6 random).
# "Only first inheritance" uses just the 2 initial events.
#
# We compute P(>=1 proc) = 1 - (1-p)^n  and P(>=2 procs) using binomial CDF.

_EVENTS_FULL_RUN = 2          # 2 inheritance events per run (confirmed from API traces)
_EVENTS_FIRST    = 1          # just the first inheritance

def _base_rate(cat: str, stars: int) -> float:
    """Per-event base proc rate for a factor given its category and star count."""
    if cat not in _INHERITANCE_BASE:
        return 0.0
    s = max(1, min(3, stars))
    return _INHERITANCE_BASE[cat].get(s, 0.0)


def _binom_ge(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p).  Only need k=1 and k=2."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    q = 1 - p
    if k <= 0:
        return 1.0
    if k == 1:
        return 1 - q ** n
    if k == 2:
        return 1 - q ** n - n * p * q ** (n - 1)
    # general case (not needed but safe)
    from math import comb
    total = 0
    for i in range(k):
        total += comb(n, i) * p ** i * q ** (n - i)
    return 1 - total


def compute_spark_odds(p1: dict, p2: dict, compatibility: dict,
                       trainee_card: int | None = None) -> dict:
    """Compute per-spark proc odds for a breeding pair.

    Improved model (Hakuraku/Crazyfellow individual inheritance theory):
      - Each of the 6 tree entities (P1, P1-GP1, P1-GP2, P2, P2-GP1, P2-GP2)
        has its own individual affinity.
      - Parent individual affinity  = CR(t,p) + WSB(p) + RL2(p1,p2)
      - GP individual affinity      = RL3(t, parent, gp) + |parent ∩ gp| saddles
      - Blue stat procs included (70/80/90% base, can fail).
      - Per-event proc clamped to min(100%) per Cygames patent.

    Args:
        p1, p2: parsed uma dicts (own_sparks, grandparents, etc.)
        compatibility: result of affinity.compatibility_from_parsed()
        trainee_card: card_id of the trainee (needed for individual affinities)

    Returns dict with lists for full_run and first_only.
    """
    import affinity as _aff

    # ── Individual affinities per entity ───────────────────────────────
    if trainee_card:
        indiv = _aff.individual_affinities_from_parsed(trainee_card, p1, p2)
    else:
        # Fallback: use parent chain + cross (old behavior, slightly less accurate)
        cross = compatibility.get("cross", 0)
        indiv = {
            "p1": compatibility.get("p1_chain", 0) + cross,
            "p2": compatibility.get("p2_chain", 0) + cross,
        }

    src_aff = {
        "p1":     indiv.get("p1", 0),
        "p1_gp1": indiv.get("p1_gp1", 0),
        "p1_gp2": indiv.get("p1_gp2", 0),
        "p2":     indiv.get("p2", 0),
        "p2_gp1": indiv.get("p2_gp1", 0),
        "p2_gp2": indiv.get("p2_gp2", 0),
    }

    # ── Collect sparks from all 6 sources ──────────────────────────────
    sparks = {}   # name -> {name, type, cat, sources: {src_key: stars}}

    def _add(sp, src_key):
        s = sparks.setdefault(sp["name"], {
            "name": sp["name"], "type": sp["type"],
            "cat": sp.get("category", sp["type"]),
            "sources": {}
        })
        s["sources"][src_key] = s["sources"].get(src_key, 0) + sp["stars"]

    for sp in p1.get("own_sparks", []):
        _add(sp, "p1")
    for sp in p2.get("own_sparks", []):
        _add(sp, "p2")

    for g in p1.get("grandparents", []):
        pid = g.get("position_id")
        key = "p1_gp1" if pid == 10 else "p1_gp2" if pid == 20 else None
        if key:
            for sp in g.get("sparks", []):
                _add(sp, key)

    for g in p2.get("grandparents", []):
        pid = g.get("position_id")
        key = "p2_gp1" if pid == 10 else "p2_gp2" if pid == 20 else None
        if key:
            for sp in g.get("sparks", []):
                _add(sp, key)

    # ── Compute odds ───────────────────────────────────────────────────
    results = {}
    for mode, n_events in [("full_run", _EVENTS_FULL_RUN), ("first_only", _EVENTS_FIRST)]:
        entries = []
        for sp in sparks.values():
            cat = sp["cat"]
            # Normalise blue alias
            rate_cat = "blue" if cat in ("stat", "blue") else cat

            # Per-source rate with individual affinity
            source_rates = []
            for sk, stars in sp["sources"].items():
                if stars <= 0:
                    continue
                base = _base_rate(rate_cat, stars)
                if base <= 0:
                    continue
                aff = src_aff.get(sk, 0)
                rate = min(1.0, base * (1 + aff / 100))   # clamp 100%
                source_rates.append(rate)

            if not source_rates:
                continue

            # Combined per-event: P(≥1 source procs) = 1 − Π(1−rᵢ)
            miss = 1.0
            for r in source_rates:
                miss *= (1 - r)
            combined = 1 - miss

            ge1 = _binom_ge(1, n_events, combined)
            ge2 = _binom_ge(2, n_events, combined)

            # Total stars grouped by parent side (for display)
            s_p1 = sum(sp["sources"].get(k, 0) for k in ("p1", "p1_gp1", "p1_gp2"))
            s_p2 = sum(sp["sources"].get(k, 0) for k in ("p2", "p2_gp1", "p2_gp2"))

            entries.append({
                "name": sp["name"],
                "type": sp["type"],
                "cat": rate_cat,
                "stars_p1": s_p1,
                "stars_p2": s_p2,
                "ge1_pct": round(ge1 * 100, 2),
                "ge2_pct": round(ge2 * 100, 2) if ge2 > 0.001 else None,
            })

        type_order = {"blue": -1, "aptitude": 0, "pink": 0, "unique": 1, "green": 1,
                      "skill": 2, "white": 2, "scenario": 2, "race": 3}
        entries.sort(key=lambda e: (type_order.get(e["cat"], 9), -e["ge1_pct"]))
        results[mode] = entries
    return results


def uma_ui(c, notes, src="mine"):
    tcid = str(c["trained_chara_id"])
    note = notes.get(tcid, {})
    whites = [sp for sp in c["own_sparks"] if sp["type"] == "white"]
    # uma.moe-style WHITE count = unique white/scenario spark names across the
    # full visible chain (own + the 2 direct parents). Each chip in the UI is
    # one entry -- same as agg below.
    chain_whites = set()
    chain_white_stars = 0
    for sp in c["own_sparks"]:
        if sp["type"] in ("white", "scenario"):
            if sp["name"] not in chain_whites:
                chain_white_stars += sp["stars"]
            chain_whites.add(sp["name"])
    for g in c["grandparents"]:
        if g.get("position_id") not in (10, 20):
            continue
        for sp in g["sparks"]:
            if sp["type"] in ("white", "scenario"):
                if sp["name"] not in chain_whites:
                    chain_white_stars += sp["stars"]
                chain_whites.add(sp["name"])
    # linaje de sparks BLANCAS + scenario (propias depth 0, padres depth 1, abuelos depth 2)
    # for the lineage filter; scenarios (TS Climax, etc) are bucketed under white.
    lineage_white = [{"name": sp["name"], "stars": sp["stars"], "depth": 0}
                     for sp in c["own_sparks"] if sp["type"] in ("white", "scenario")]
    for g in c["grandparents"]:
        d = 1 if g.get("position_id") in (10, 20) else 2
        for sp in g["sparks"]:
            if sp["type"] in ("white", "scenario"):
                lineage_white.append({"name": sp["name"], "stars": sp["stars"], "depth": d})

    # AGREGADO por factor para herencia: SOLO trainee (own) + los 2 padres (pos 10/20).
    # Los abuelos NO cuentan para el total de herencia.
    agg = {}
    def _add(sp, slot):
        a = agg.setdefault(sp["name"], {"name": sp["name"], "type": sp["type"],
                                        "cat": sp.get("category"), "own": 0, "parents": 0})
        a[slot] += sp["stars"]
    for sp in c["own_sparks"]:
        _add(sp, "own")
    parents = []
    for g in c["grandparents"]:
        if g.get("position_id") not in (10, 20):
            continue
        for sp in g["sparks"]:
            _add(sp, "parents")
        parents.append({
            "position_id": g["position_id"],
            "card_id": g["card_id"],
            "name": master.card_name(g["card_id"]),
            "stars": sum(sp["stars"] for sp in g["sparks"]),
            "sparks": [{"name": sp["name"], "stars": sp["stars"], "type": sp["type"]}
                       for sp in g["sparks"]],
        })
    parents.sort(key=lambda p: p["position_id"])
    for a in agg.values():
        a["total"] = a["own"] + a["parents"]
        a["pct"] = inspiration_pct(a["cat"], a["total"])
    agg_list = sorted(agg.values(), key=lambda a: -a["total"])

    return {
        "src": src,
        "g1": master.count_g1_wins(c.get("race_result_list"), c.get("win_saddle_id_array")),
        "lineage_white": lineage_white,
        "agg": agg_list,
        "parents": parents,
        "trained_chara_id": c["trained_chara_id"],
        "card_id": c["card_id"],
        "name": c["name"],
        "rank": c["rank"],
        "rank_label": master.rank_label(c["rank"]),
        "rank_score": c.get("rank_score"),
        "fans": c["fans"],
        "owner_name": c.get("owner_name"),
        "stats": c["stats"],
        "sparks": [{"name": sp["name"], "stars": sp["stars"], "type": sp["type"]}
                   for sp in c["own_sparks"]],
        "blue": {k: round(v, 1) for k, v in heir.blue_strength(c).items() if v},
        "white_count": len(chain_whites),
        "white_stars": chain_white_stars,
        "note": note.get("note", ""),
        "tags": note.get("tags", []),
    }


_IMAGES_DIRS = []
# 1. Explicit override via env var: HEIR_IMAGES_DIR=C:\path\to\images
if os.environ.get("HEIR_IMAGES_DIR"):
    _IMAGES_DIRS.append(Path(os.environ["HEIR_IMAGES_DIR"]))
# 2. Bundled in the repo
_IMAGES_DIRS.append(heir.ROOT / "data" / "images")


def _find_image(card_id: int):
    for d in _IMAGES_DIRS:
        p = d / f"{card_id}.png"
        if p.exists():
            return p
    return None


# ── TT history aggregation (from dashboard_server.py) ─────────────────────

def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    out = []
    with open(HISTORY_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def aggregate(rows: list[dict]) -> dict:
    """Compute per-uma stats across all trials.
    Returns {trained_chara_id: {...metrics...}}"""
    by_uma: dict[int, dict] = {}

    # We don't have trial_id in the row, but rows were appended in chunks of 15.
    # Assign trial_idx by sliding through the history file 15 at a time.
    for i, row in enumerate(rows):
        trial_idx = i // 15

        tcid = row.get("trained_chara_id")
        if tcid is None:
            continue
        entry = by_uma.setdefault(tcid, {
            "trained_chara_id": tcid,
            "chara_id":         row.get("chara_id"),
            "chara_name":       row.get("chara_name") or master.chara_name_by_card_id(row.get("chara_id") or 0),
            "stats":            row.get("stats") or {},
            "owned_skills":     list(row.get("owned_skills") or []),
            "trials":           [],
        })
        # Backward compat for rows written before display_score was tracked.
        # The exact value comes from raw_score * (team_total_score / sum_team_raw)
        # which is computed per-race. Approximate average ratio is ~2.75x.
        raw_s = row.get("raw_score") or 0
        display_s = row.get("display_score")
        if display_s is None or display_s == 0:
            display_s = round(raw_s * 2.75)
        entry["trials"].append({
            "trial_idx":         trial_idx,
            "race_idx":          row.get("race_idx"),
            "distance_type":     row.get("distance_type"),
            "finish_order":      row.get("finish_order"),
            "finish_time":       row.get("finish_time"),
            "running_style":     row.get("running_style"),
            "raw_score":         raw_s,
            "display_score":     display_s,
            "activated_skills":  row.get("activated_skills") or [],
        })
        # Update owned skills to union across trials (in case skills changed)
        entry["owned_skills"] = list(set(entry["owned_skills"]) | set(row.get("owned_skills") or []))
        # Use the latest stats observed
        if row.get("stats"):
            entry["stats"] = row.get("stats")

    # ── Collapse re-trained versions ─────────────────────────────────
    # When you re-train an uma, the game assigns a NEW trained_chara_id even
    # though it's the same character (same chara_id). Show only the most
    # recent version: drop any version whose last appearance is strictly
    # earlier than another version of the same character (i.e. it was
    # replaced). Two copies active in the same latest trial are both kept.
    def _last_idx(entry: dict) -> int:
        return max((t["trial_idx"] for t in entry["trials"]), default=-1)

    newest_idx_by_chara: dict = {}
    for e in by_uma.values():
        cid = e.get("chara_id")
        newest_idx_by_chara[cid] = max(newest_idx_by_chara.get(cid, -1), _last_idx(e))

    superseded_by_chara: dict = defaultdict(int)
    kept: dict[int, dict] = {}
    for tcid, e in by_uma.items():
        cid = e.get("chara_id")
        if _last_idx(e) < newest_idx_by_chara.get(cid, -1):
            superseded_by_chara[cid] += 1   # an older re-train, hide it
            continue
        kept[tcid] = e
    # Annotate the surviving version with how many older re-trains were hidden
    for tcid, e in kept.items():
        e["superseded_versions"] = superseded_by_chara.get(e.get("chara_id"), 0)
    by_uma = kept

    # Per-uma metrics  (uses display_score = ingame-matching number)
    for tcid, e in by_uma.items():
        scores = [t["display_score"] for t in e["trials"]]
        finishes = [t["finish_order"] for t in e["trials"] if t["finish_order"] is not None]
        n = len(scores)
        avg = statistics.mean(scores) if scores else 0
        if n >= 2:
            stdev = statistics.stdev(scores)
            cv = (stdev / avg * 100) if avg else 0
        else:
            stdev = 0
            cv = 0
        # Trimmed AVG: drop lowest if it's below 80% of avg
        trimmed_avg = avg
        if n >= 3 and scores and min(scores) < 0.8 * avg:
            trimmed = [s for s in scores if s != min(scores)]
            trimmed_avg = statistics.mean(trimmed) if trimmed else avg
        # Ceiling 90th / Floor 10th percentile
        if n >= 2:
            sorted_scores = sorted(scores)
            ceiling = sorted_scores[int(round(0.9 * (n - 1)))]
            floor = sorted_scores[int(round(0.1 * (n - 1)))]
        else:
            ceiling = scores[0] if scores else 0
            floor = scores[0] if scores else 0

        # Skill activation aggregation
        owned_set = set(e["owned_skills"])
        activated_counts: dict[int, int] = defaultdict(int)
        for t in e["trials"]:
            for sid in t["activated_skills"]:
                if sid in owned_set:
                    activated_counts[sid] += 1
        skill_activation_pct = {sid: (cnt / n * 100) for sid, cnt in activated_counts.items()}
        never_activated = sorted(owned_set - set(activated_counts.keys()))
        always_activated = [sid for sid, pct in skill_activation_pct.items() if pct >= 99.0]

        # Overall activation ratio: avg skills activated per race / total owned
        total_owned = len(e["owned_skills"])
        avg_activated_per_race = statistics.mean(
            [len(set(t["activated_skills"]) & owned_set) for t in e["trials"]]
        ) if e["trials"] else 0
        overall_act_pct = (avg_activated_per_race / total_owned * 100) if total_owned else 0

        e.update({
            "n_appearances":       n,
            "avg_score":           round(avg),
            "trimmed_avg":         round(trimmed_avg),
            "stdev":               round(stdev),
            "cv":                  round(cv, 1),
            "ceiling":             round(ceiling),
            "floor":               round(floor),
            "ceiling_floor_diff":  round(ceiling - floor),
            "avg_finish":          round(statistics.mean(finishes) + 1, 2) if finishes else None,
            "wins":                sum(1 for f in finishes if f == 0),
            "top3":                sum(1 for f in finishes if f <= 2),
            "owned_total":         total_owned,
            "avg_activated":       round(avg_activated_per_race, 1),
            "overall_act_pct":     round(overall_act_pct, 1),
            "skill_activation":    {
                str(sid): {
                    "name":  master.skill_name(sid),
                    "pct":   round(skill_activation_pct.get(sid, 0), 1),
                    "count": activated_counts.get(sid, 0),
                }
                for sid in sorted(owned_set, key=lambda s: -skill_activation_pct.get(s, 0))
            },
            "never_activated_n":   len(never_activated),
            "always_activated_n":  len(always_activated),
            "sv":                  master.compute_uma_sv(owned_set),
        })

    # Compute Gap to Top (avg score)
    if by_uma:
        top_avg = max(e["avg_score"] for e in by_uma.values())
        for e in by_uma.values():
            e["gap_to_top"] = top_avg - e["avg_score"]
            # Retrain priority: 85% avg + 15% consistency (lower CV = higher priority)
            # Normalize: avg_norm = avg/top_avg, consistency_norm = max(0, 100-cv)/100
            avg_norm = e["avg_score"] / top_avg if top_avg else 0
            cons_norm = max(0, 100 - e["cv"]) / 100
            e["retrain_priority"] = round((0.85 * (1 - avg_norm) + 0.15 * (1 - cons_norm)) * 100, 1)

    return by_uma


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Heir (breeding optimizer)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/")
def index():
    return FileResponse(
        str(HERE / "static" / "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@app.get("/img/{card_id}")
def img(card_id: int):
    p = _find_image(card_id)
    if p:
        return FileResponse(str(p))
    # Fallback: download from gametora and cache locally
    images_dir = HERE / "data" / "images"
    chara_id = card_id // 100
    urls = [
        f"https://gametora.com/images/umamusume/characters/chara_stand_{chara_id}_{card_id}.png",
        f"https://gametora.com/images/umamusume/characters/chara_stand_{chara_id}.png",
    ]
    for url in urls:
        try:
            req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0 Heaven-Dashboard"})
            with _urlreq.urlopen(req, timeout=5) as r:
                data = r.read()
            if data and len(data) > 500:
                dest = images_dir / f"{card_id}.png"
                dest.write_bytes(data)
                return FileResponse(str(dest), media_type="image/png")
        except Exception:
            continue
    return JSONResponse({"error": "no image"}, status_code=404)


@app.get("/icon/{card_id}")
def icon(card_id: int):
    """Serve a cached chara icon. Downloads from gametora on first request,
    so the dashboard works offline after first load."""
    cache_file = ICON_CACHE / f"{card_id}.png"
    miss_file = ICON_CACHE / f"{card_id}.miss"
    if cache_file.exists():
        return FileResponse(str(cache_file), media_type="image/png")
    if miss_file.exists():
        return Response(status_code=404)

    for url in icon_urls(card_id):
        try:
            req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0 Heaven-Dashboard"})
            with _urlreq.urlopen(req, timeout=5) as r:
                data = r.read()
            if data and len(data) > 200:    # crude sanity: a real PNG is bigger
                cache_file.write_bytes(data)
                return FileResponse(str(cache_file), media_type="image/png")
        except Exception:
            continue

    # All sources failed - record so we don't keep trying
    miss_file.touch()
    return Response(status_code=404)


@app.get("/api/data")
def api_data():
    ds = ensure_dataset()
    notes = load_notes()
    # umas objetivo: nombres unicos (de tu pool + prestables) para el desplegable
    targets = {}
    for c in ds["mine"] + ds["rentable"]:
        if c["card_id"] not in targets:
            targets[c["card_id"]] = {
                "card_id": c["card_id"],
                "name": c["name"],
                "win_saddle_ids": list(c.get("win_saddle_id_array") or []),
            }
    target_list = sorted(targets.values(), key=lambda x: x["name"])
    return {
        "mine": [uma_ui(c, notes, "mine") for c in ds["mine"]],
        "rentable": [uma_ui(c, notes, "friend") for c in ds["rentable"]],
        "skills": STATE["skills"] or {"blue": [], "pink": [], "white": []},
        "targets": target_list,
        "source": STATE["source"],
    }


class TargetAffinityReq(BaseModel):
    target: int
    target_wins: list[int] = []   # win_saddle_ids of the target uma (for race overlap)


@app.get("/api/uma/{trained_chara_id}/races")
def api_uma_races(trained_chara_id: int):
    """List race wins for a uma -- saddle id + display name + grade type.
    grade: 1=G1, 2=G2, 3=G3, 0=Title (Triple Crown, Tenno Sweep, etc)."""
    ds = ensure_dataset()
    uma = next((c for c in ds["mine"] + ds["rentable"]
                if c["trained_chara_id"] == trained_chara_id), None)
    if not uma:
        return JSONResponse({"error": "not found"}, status_code=404)
    types = master.saddle_types_map()
    out = []
    for sid in uma.get("win_saddle_id_array") or []:
        out.append({
            "saddle_id": sid,
            "name": master.saddle_name(sid),
            "type": types.get(sid, -1),
        })
    # Sort: titles first, then by grade asc (G1->G2->G3), then by name
    out.sort(key=lambda r: (0 if r["type"] == 0 else r["type"], r["name"]))
    return {"trained_chara_id": trained_chara_id, "name": uma["name"], "races": out}


@app.get("/api/export/umaextractor")
def api_export_umaextractor():
    """Export trained_chara entries in the flat-array format UmaExtractor
    produces (and hakuraku.moe/veterans can import). Includes BOTH your own
    umas and your friends' borrowable parents in a single array -- hakuraku
    treats them all as veterans. Each element is the raw game object."""
    raw_mine, raw_rentable = heir.load_raw_trained_chara()
    # Dedup by trained_chara_id in case mine and rentable overlap.
    seen, combined = set(), []
    for c in (raw_mine or []) + (raw_rentable or []):
        tcid = c.get("trained_chara_id")
        if tcid in seen:
            continue
        seen.add(tcid)
        combined.append(c)
    return JSONResponse(
        combined,
        headers={"Content-Disposition": 'attachment; filename="heir_export_data.json"'},
    )


@app.get("/api/export/full")
def api_export_full():
    """Export BOTH your umas and friends' borrowable parents in a single JSON,
    with the original game structure preserved (load/index + pre_single_mode/index).
    Useful for sharing with tools that accept either side."""
    raw_mine, raw_rentable = heir.load_raw_trained_chara()
    return JSONResponse({
        "trained_chara": raw_mine,
        "succession_trained_chara_array": raw_rentable,
        "_format": "heir-full-export-v1",
    }, headers={"Content-Disposition": 'attachment; filename="heir_export_full.json"'})


@app.get("/api/debug/affinity/{trained_chara_id}/{target_card_id}")
def api_debug_affinity(trained_chara_id: int, target_card_id: int):
    """Debug endpoint: show chara_relation breakdown by relation_type for a
    specific uma vs target, so we can compare individual link points against
    external tools (uma.moe, hakuraku, etc.)."""
    import affinity as _aff
    ds = ensure_dataset()
    uma = next((c for c in ds["mine"] + ds["rentable"]
                if c["trained_chara_id"] == trained_chara_id), None)
    if not uma:
        return JSONResponse({"error": "uma not found"}, status_code=404)
    t = master.chara_id_of(target_card_id)
    crt = master.chara_relation_types()
    pts = master.relation_points()

    def link_detail(card_id, wins):
        c = master.chara_id_of(card_id)
        if c == t:
            shared_groups = []
        else:
            shared_types = sorted(crt.get(t, set()) & crt.get(c, set()))
            shared_groups = [{"type": rt, "points": pts.get(rt, 0)} for rt in shared_types]
        return {
            "card_id": card_id,
            "chara_id": c,
            "chara_name": master.card_name(card_id),
            "chara_relation_total": sum(g["points"] for g in shared_groups),
            "shared_relation_types": shared_groups,
            "wins_count": len(wins or []),
        }

    own = link_detail(uma["card_id"], uma.get("win_saddle_id_array"))
    gps = []
    for g in uma.get("grandparents") or []:
        if g.get("position_id") in (10, 20) and g.get("card_id"):
            gp = link_detail(g["card_id"], g.get("win_saddle_id_array"))
            gp["position_id"] = g["position_id"]
            # also compute shared wins with the uma itself
            gp["shared_wins_with_uma"] = len(
                set(g.get("win_saddle_id_array") or []) & set(uma.get("win_saddle_id_array") or [])
            )
            gps.append(gp)

    # Exact formula breakdown
    chara = master.chara_id_of(uma["card_id"])
    gp_charas = [master.chara_id_of(g["card_id"]) for g in gps]
    cr = _aff.calc_relation(t, chara, gp_charas)
    wsb = _aff.wsb_from_parsed(uma)
    return {
        "target": {"card_id": target_card_id, "chara_id": t,
                   "chara_name": master.card_name(target_card_id)},
        "uma": {"trained_chara_id": trained_chara_id, "name": uma["name"],
                "card_id": uma["card_id"], "owner": uma.get("owner_name")},
        "own": own,
        "grandparents": gps,
        "computed_total": cr + wsb,
        "computed_breakdown": {
            "cr": cr, "cr_base": _aff.rl2(t, chara),
            "cr_gp": cr - _aff.rl2(t, chara), "wsb": wsb,
        },
    }


@app.post("/api/target_affinity")
def api_target_affinity(req: TargetAffinityReq):
    """Rank all umas by exact affinity for a target.
    Uses the confirmed formula: CR(t,p) = rl2 + rl3(gp1) + rl3(gp2), plus WSB offline."""
    import affinity as _aff

    ds = ensure_dataset()
    target_chara = master.chara_id_of(req.target)
    scores = {}
    breakdown = {}
    excluded = []
    for src, pool in (("m", ds["mine"]), ("f", ds["rentable"])):
        for c in pool:
            chara = master.chara_id_of(c["card_id"])
            if chara == target_chara:
                excluded.append(c["trained_chara_id"])
                continue
            # Resolve GPs + per-GP WSB contribution
            gps = []
            gp_names = []
            parent_saddles = set(c.get("win_saddle_id_array") or [])
            nodes = []
            for g in c.get("grandparents") or []:
                if g.get("position_id") in (10, 20) and g.get("card_id"):
                    gc = master.chara_id_of(g["card_id"])
                    gps.append(gc)
                    gp_names.append(master.chara_name(gc))
                    rl3_val = _aff.rl3(target_chara, chara, gc)
                    gp_wsb = len(parent_saddles & set(g.get("win_saddle_id_array") or []))
                    nodes.append({
                        "position_id": g["position_id"], "card_id": g["card_id"],
                        "total": rl3_val + gp_wsb,
                        "crel": rl3_val, "wsb": gp_wsb,
                    })
            # Exact CR
            cr_base = _aff.rl2(target_chara, chara)
            cr_gp_bonus = sum(_aff.rl3(target_chara, chara, gp) for gp in gps if gp > 0)
            cr = cr_base + cr_gp_bonus
            # WSB offline
            wsb = _aff.wsb_from_parsed(c)
            chain = cr + wsb

            # Use composite key (src prefix + tcid) to avoid collisions between
            # mine and rentable -- trained_chara_id is per-account, not global.
            key = f"{src}{c['trained_chara_id']}"
            scores[key] = chain
            breakdown[key] = {
                "total": chain,
                "cr": cr,
                "cr_base": cr_base,
                "cr_gp": cr_gp_bonus,
                "wsb": wsb,
                "wsb_known": True,
                "gp_names": gp_names,
                "own": cr_base,
                "nodes": nodes,
            }
    return {"target": req.target, "target_name": master.card_name(req.target),
            "scores": scores, "breakdown": breakdown, "excluded": excluded}


class BreedReq(BaseModel):
    target: int
    want: list[str] = []
    w_affinity: float = 2.0
    w_spark: float = 1.0
    allowed_ids: list[int] | None = None
    # Skill quality weighting (optional -- needs TTAnalyzer data)
    breed_style: str | None = None      # "Front Runner", "Pace Chaser", etc.
    breed_distance: str | None = None   # "Sprint", "Mile", "Medium", "Long"
    strict_act_pct: float | None = None # threshold, e.g. 60.0
    use_eproc: bool = True              # True=proc-based scoring, False=legacy weighted


@app.post("/api/breed")
def api_breed(req: BreedReq):
    ds = ensure_dataset()

    # Compute skill quality weights from act% data if style/distance provided
    sw = None
    if req.breed_style or req.breed_distance:
        dist_code = DIST_TO_CODE.get(req.breed_distance or "", 0)
        style_code = STYLE_TO_CODE.get(req.breed_style or "", "")
        if dist_code and style_code:
            act_data = fetch_act_rates(dist_code, style_code)
            sw = compute_skill_weights(act_data, req.breed_style, req.strict_act_pct)

    res = heir.optimize_breed(ds, req.target, req.want, req.w_affinity, req.w_spark,
                              top=15, allowed_ids=req.allowed_ids, skill_weights=sw,
                              use_eproc=req.use_eproc)
    if sw:
        res["weighted"] = True
    return res


@app.get("/api/act_rates")
def api_act_rates(style: str = "", distance: str = ""):
    """Act% data for frontend chip annotation."""
    dist_code = DIST_TO_CODE.get(distance, 0)
    style_code = STYLE_TO_CODE.get(style, "")
    if not dist_code or not style_code:
        return {"available": False, "skills": {}, "universal": {}}
    data = fetch_act_rates(dist_code, style_code)
    if not data:
        return {"available": False, "skills": {}, "universal": {}}
    return {"available": True, "skills": data.get("skills", {}),
            "universal": data.get("universal", {}),
            "observations": data.get("observations", 0)}


@app.post("/api/import_skill_export")
async def api_import_skill_export():
    """Generate skill export from local data and save to data/skill_export.json."""
    global _ACT_EXPORT
    try:
        data = skill_planner.export_for_heir()
    except Exception as e:
        return JSONResponse({"error": f"skill_planner error: {e}"}, status_code=500)

    groups = data.get("groups", [])
    universal = data.get("universal", [])
    total_skills = sum(len(g.get("skills", [])) for g in groups)

    ACT_EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACT_EXPORT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    _ACT_EXPORT = data
    _ACT_CACHE.clear()  # force re-read from new export

    return {"ok": True, "groups": len(groups), "universal": len(universal),
            "total_skills": total_skills,
            "path": str(ACT_EXPORT_PATH)}


class PairReq(BaseModel):
    target: int
    p1_id: int
    p2_id: int


@app.post("/api/pair")
def api_pair(req: PairReq):
    ds = ensure_dataset()
    notes = load_notes()
    by = {c["trained_chara_id"]: c for c in ds["mine"] + ds["rentable"]}
    p1, p2 = by.get(req.p1_id), by.get(req.p2_id)
    if not p1 or not p2:
        return JSONResponse({"error": "parent not found"}, status_code=404)
    import affinity as _aff
    comp = _aff.compatibility_from_parsed(req.target, p1, p2)
    # factores combinados que hereda la cria = sparks propios de los 2 padres
    agg = {}
    for c, slot in ((p1, "p1"), (p2, "p2")):
        for sp in c["own_sparks"]:
            a = agg.setdefault(sp["name"], {"name": sp["name"], "type": sp["type"],
                                            "cat": sp.get("category"), "p1": 0, "p2": 0})
            a[slot] += sp["stars"]
    for a in agg.values():
        a["total"] = a["p1"] + a["p2"]
        a["own"] = 0
        a["pct"] = inspiration_pct(a["cat"], a["total"])
    agg_list = sorted(agg.values(), key=lambda a: -a["total"])

    def brief(c):
        n = notes.get(str(c["trained_chara_id"]), {})
        return {"trained_chara_id": c["trained_chara_id"], "card_id": c["card_id"], "name": c["name"],
                "rank": c["rank"], "owner_name": c.get("owner_name"),
                "note": n.get("note", ""), "tags": n.get("tags", []),
                "sparks": [{"name": sp["name"], "stars": sp["stars"], "type": sp["type"]}
                           for sp in c["own_sparks"]]}
    # Flatten to legacy-compatible shape for the Breed tab frontend
    aff = {
        "total": comp["total"],
        "rel_trainee_p1": comp.get("cr1", 0),
        "rel_trainee_p2": comp.get("cr2", 0),
        "rel_p1_p2": comp.get("cross", 0),
        "shared_g1": 0,
        "win_bonus": comp.get("wsb_p1", 0) + comp.get("wsb_p2", 0),
    }
    # Spark proc odds (hakuraku-style, individual affinity per entity)
    spark_odds = compute_spark_odds(p1, p2, comp, trainee_card=req.target)

    return {"affinity": aff, "target_name": master.card_name(req.target),
            "p1": brief(p1), "p2": brief(p2), "agg": agg_list,
            "spark_odds": spark_odds}


class NoteReq(BaseModel):
    trained_chara_id: int
    note: str = ""
    tags: list[str] = []


@app.post("/api/notes")
def api_notes(req: NoteReq):
    notes = load_notes()
    tcid = str(req.trained_chara_id)
    if req.note or req.tags:
        notes[tcid] = {"note": req.note, "tags": req.tags}
    else:
        notes.pop(tcid, None)
    save_notes(notes)
    return {"ok": True, "saved": tcid}


@app.post("/api/reload")
def api_reload():
    ensure_dataset(force=True)
    ds = STATE["ds"]
    return {"ok": True, "mine": len(ds["mine"]), "rentable": len(ds["rentable"]), "source": STATE["source"]}


@app.get("/api/check_update")
def api_check_update():
    """Quietly fetch from origin and report how many commits we're behind.
    Used by the dashboard to badge the Update & restart button."""
    import subprocess
    try:
        subprocess.run(
            ["git", "-C", str(HERE), "fetch", "--quiet", "origin"],
            capture_output=True, text=True, timeout=15,
        )
        # commits on upstream that we don't have yet
        proc = subprocess.run(
            ["git", "-C", str(HERE), "rev-list", "--count", "HEAD..@{u}"],
            capture_output=True, text=True, timeout=10,
        )
        behind = int((proc.stdout or "0").strip() or 0)
        latest = ""
        if behind > 0:
            log = subprocess.run(
                ["git", "-C", str(HERE), "log", "--format=%s", "-1", "@{u}"],
                capture_output=True, text=True, timeout=5,
            )
            latest = (log.stdout or "").strip()
        return {"available": behind > 0, "behind": behind, "latest": latest}
    except Exception as e:
        return {"available": False, "behind": 0, "error": str(e)}


@app.post("/api/restart")
def api_restart():
    """Run `git pull` and re-exec the python process so the browser picks up
    code/dep changes without the user touching the terminal. If pull fails the
    server still restarts so the existing code keeps working."""
    import subprocess
    pull_out, pull_err, pull_ok = "", "", False
    try:
        proc = subprocess.run(
            ["git", "-C", str(HERE), "pull", "--ff-only"],
            capture_output=True, text=True, timeout=30,
        )
        pull_out = (proc.stdout or "").strip()
        pull_err = (proc.stderr or "").strip()
        pull_ok = proc.returncode == 0
    except FileNotFoundError:
        pull_err = "git not installed / not on PATH"
    except subprocess.TimeoutExpired:
        pull_err = "git pull timed out"
    except Exception as e:
        pull_err = f"git pull error: {e}"

    def _do():
        import time as _t
        _t.sleep(0.3)
        os.execv(sys.executable, [sys.executable, str(HERE / "server.py")])
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "pull_ok": pull_ok, "pull_out": pull_out, "pull_err": pull_err}


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Setup / Fetch dashboard
# ═══════════════════════════════════════════════════════════════════════════

SETUP_STATE = {
    "step": "idle",           # idle | capturing | captured | fetching | done | error
    "message": "",
    "viewer_id": None,
    "mine": 0,
    "rentable": 0,
    "captured": None,         # dict with auth_key etc once Frida hooks
    "_thread": None,
    "_session": None,
    "_cancel": False,
}
SETUP_LOCK = threading.Lock()


def _set_step(step, message=""):
    with SETUP_LOCK:
        SETUP_STATE["step"] = step
        SETUP_STATE["message"] = message


def _has_trace():
    p = heir.find_trace(None)
    return bool(p and p.exists())


def _has_auth():
    return bool(fetch_mod.load_saved_auth())


@app.get("/api/setup/status")
def setup_status():
    with SETUP_LOCK:
        return {
            "has_trace": _has_trace(),
            "has_auth": _has_auth(),
            "step": SETUP_STATE["step"],
            "message": SETUP_STATE["message"],
            "viewer_id": SETUP_STATE["viewer_id"],
            "mine": SETUP_STATE["mine"],
            "rentable": SETUP_STATE["rentable"],
        }


def _capture_worker():
    """Run Frida capture, populate SETUP_STATE.captured when creds arrive."""
    try:
        import frida
        import time as _time
    except ImportError:
        _set_step("error", "frida no instalado: pip install frida")
        return

    _set_step("capturing", "Lanzando Umamusume via Steam...")
    fetch_mod.launch_game()

    captured = {}

    def on_msg(msg, data):
        if msg.get("type") == "error":
            return
        p = msg.get("payload") or {}
        if p.get("type") == "creds" and p.get("app_ver") and p.get("res_ver"):
            captured.update(p)

    deadline = _time.time() + 240
    session = None
    _set_step("capturing", "Esperando al juego... (logueate y entra al menu principal)")
    while _time.time() < deadline:
        if SETUP_STATE["_cancel"]:
            _set_step("idle", "Captura cancelada")
            return
        try:
            session = frida.attach(fetch_mod.PROCESS_NAME)
            break
        except Exception:
            _time.sleep(1)

    if not session:
        _set_step("error", f"Timeout esperando a {fetch_mod.PROCESS_NAME}")
        return

    with SETUP_LOCK:
        SETUP_STATE["_session"] = session
    _set_step("capturing", "Frida attached. Cuando llegues al home con tus umas, capturo y sigo.")

    try:
        script = session.create_script(fetch_mod.FRIDA_JS)
        script.on("message", on_msg)
        script.load()
        while _time.time() < deadline:
            if SETUP_STATE["_cancel"]:
                _set_step("idle", "Captura cancelada")
                return
            if fetch_mod.fresh_auth(captured):
                _time.sleep(1)
                with SETUP_LOCK:
                    SETUP_STATE["captured"] = dict(captured)
                    SETUP_STATE["viewer_id"] = captured.get("viewer_id")
                _set_step("captured", f"Credenciales OK (viewer_id={captured.get('viewer_id')}). Introduce ahora tu cuenta Steam.")
                return
            _time.sleep(0.5)
        _set_step("error", "Timeout sin capturar credenciales validas.")
    except Exception as e:
        _set_step("error", f"Frida error: {e}")
    finally:
        try:
            session.detach()
        except Exception:
            pass


@app.post("/api/setup/start_capture")
def setup_start_capture():
    if _has_auth():
        return JSONResponse({"error": "auth ya guardado, salta directo al fetch"}, status_code=400)
    with SETUP_LOCK:
        if SETUP_STATE["step"] in ("capturing", "fetching"):
            return {"ok": True, "step": SETUP_STATE["step"], "message": SETUP_STATE["message"]}
        SETUP_STATE["_cancel"] = False
        SETUP_STATE["captured"] = None
        t = threading.Thread(target=_capture_worker, daemon=True)
        SETUP_STATE["_thread"] = t
        t.start()
    return {"ok": True, "step": "capturing"}


@app.post("/api/setup/cancel_capture")
def setup_cancel_capture():
    with SETUP_LOCK:
        SETUP_STATE["_cancel"] = True
    return {"ok": True}


class FetchReq(BaseModel):
    username: str = ""
    password: str = ""
    code: str = ""


def _fetch_worker(username, password, code):
    try:
        with SETUP_LOCK:
            captured = SETUP_STATE.get("captured")
        cfg = fetch_mod.load_saved_auth()
        if not cfg and captured:
            cfg = dict(captured)
            cfg.update(fetch_mod.get_hwid())
        if not cfg:
            _set_step("error", "Sin auth (ni guardado ni capturado).")
            return

        if username:
            cfg["steam_username"] = username
        if password:
            cfg["steam_password"] = password
        if not cfg.get("steam_username") or not cfg.get("steam_password"):
            _set_step("error", "Faltan credenciales de Steam.")
            return

        _set_step("fetching", "Generando Steam ticket...")
        try:
            sid, tkt = fetch_mod.get_steam_ticket(cfg["steam_username"], cfg["steam_password"], code)
        except RuntimeError as e:
            if "STEAM_GUARD_REQUIRED" in str(e):
                _set_step("captured", "Steam Guard necesario. Vuelve a enviar el form con el codigo 2FA.")
                return
            _set_step("error", f"Steam: {e}")
            return
        cfg["steam_id"] = sid
        cfg["steam_session_ticket"] = tkt
        fetch_mod.save_auth(cfg)

        _set_step("fetching", "Login al servidor del juego...")
        client = fetch_mod.UmaClient(cfg)
        load_res = client.login()
        _set_step("fetching", "Pidiendo padres prestables...")
        pre_res = client.pre_single_mode()

        out = fetch_mod.write_trace(load_res, pre_res)
        mine = len((load_res.get("data") or {}).get("trained_chara") or [])
        rent = len(((pre_res.get("data") or {}).get("succession_trained_chara_data") or {})
                   .get("succession_trained_chara_array") or [])
        ensure_dataset(force=True)
        with SETUP_LOCK:
            SETUP_STATE["mine"] = mine
            SETUP_STATE["rentable"] = rent
            SETUP_STATE["viewer_id"] = cfg.get("viewer_id")
        _set_step("done", f"{mine} umas tuyas + {rent} padres prestables. Trace: {out.name}")
    except Exception as e:
        _set_step("error", f"{e}\n{traceback.format_exc()[-300:]}")


@app.post("/api/setup/fetch")
def setup_fetch(req: FetchReq):
    with SETUP_LOCK:
        if SETUP_STATE["step"] in ("capturing", "fetching"):
            return {"ok": True, "step": SETUP_STATE["step"], "message": SETUP_STATE["message"]}
        SETUP_STATE["_cancel"] = False
        t = threading.Thread(target=_fetch_worker, args=(req.username, req.password, req.code), daemon=True)
        SETUP_STATE["_thread"] = t
        t.start()
    return {"ok": True, "step": "fetching"}


class ImportExtractorReq(BaseModel):
    umas: list


@app.post("/api/setup/import_extractor")
def setup_import_extractor(req: ImportExtractorReq):
    """Accept a data.json from UmaExtractor (an array of trained_chara objects)
    and write it as a heir_capture_*.jsonl so the rest of the app sees it."""
    if not isinstance(req.umas, list) or not req.umas:
        return JSONResponse({"error": "empty or invalid umas array"}, status_code=400)
    load_res = {"data": {"trained_chara": req.umas}, "data_headers": {"result_code": 1}}
    pre_res = {"data": {"succession_trained_chara_data": {
        "succession_trained_chara_array": [], "summary_user_info_array": []
    }}, "data_headers": {"result_code": 1}}
    out = fetch_mod.write_trace(load_res, pre_res)
    ensure_dataset(force=True)
    mine = len(req.umas)
    with SETUP_LOCK:
        SETUP_STATE["mine"] = mine
        SETUP_STATE["rentable"] = 0
    _set_step("done", f"{mine} umas imported from data.json. No friend parents (UmaExtractor doesn't read those).")
    return {"ok": True, "mine": mine, "trace": out.name}


@app.post("/api/setup/reset")
def setup_reset():
    """Used to dismiss error state and return to idle."""
    _set_step("idle", "")
    with SETUP_LOCK:
        SETUP_STATE["_cancel"] = False
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — WSB / Affinity endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/wsb/status")
def api_wsb_status():
    """WSB status -- now computed offline from win_saddle_id_array, no capture needed."""
    return {
        "online": True,
        "source": "offline",
        "method": "WSB = |parent intersect GP1| + |parent intersect GP2| (win_saddle_id_array intersection)",
        "note": "Computed from parsed data, no Frida/capture needed",
    }


def _uma_wsb_info(c):
    """Resolve WSB + GP charas for a single uma.
    Computes WSB offline from win_saddle_id_array."""
    import affinity as _aff
    chara = master.chara_id_of(c["card_id"])
    gps = []
    for g in c.get("grandparents") or []:
        if g.get("position_id") in (10, 20) and g.get("card_id"):
            gps.append(master.chara_id_of(g["card_id"]))
    # Compute WSB offline -- confirmed exact formula
    wsb = _aff.wsb_from_parsed(c)
    return chara, gps, wsb


@app.get("/api/wsb/coverage")
def api_wsb_coverage():
    """WSB coverage -- now all umas have WSB computed offline."""
    import affinity as _aff
    ds = ensure_dataset()
    all_umas = []
    for c in ds["mine"] + ds["rentable"]:
        chara = master.chara_id_of(c["card_id"])
        wsb = _aff.wsb_from_parsed(c)
        all_umas.append({"trained_chara_id": c["trained_chara_id"], "card_id": c["card_id"],
                         "name": c["name"], "chara_id": chara, "wsb": wsb})
    return {"total": len(all_umas), "known": len(all_umas),
            "source": "offline", "cached": all_umas, "missing": []}


class ExactAffinityReq(BaseModel):
    target: int  # card_id of the trainee


@app.post("/api/affinity/rank")
def api_affinity_rank(req: ExactAffinityReq):
    """Rank all umas by exact affinity for a target trainee.
    Each result includes CR (relation chain), WSB (computed offline), and combined chain score.
    Sort by chain descending (CR + WSB)."""
    import affinity as _aff
    ds = ensure_dataset()
    t = master.chara_id_of(req.target)
    results = []
    for c in ds["mine"] + ds["rentable"]:
        chara, gps, wsb = _uma_wsb_info(c)
        if chara == t:
            continue
        cr = _aff.calc_relation(t, chara, gps)
        base = _aff.rl2(t, chara)
        gp_bonus = cr - base
        results.append({
            "trained_chara_id": c["trained_chara_id"],
            "card_id": c["card_id"],
            "name": c["name"],
            "src": "friend" if c.get("owner_name") else "mine",
            "owner_name": c.get("owner_name"),
            "rank_label": master.rank_label(c.get("rank")),
            "chara_id": chara,
            "cr": cr,
            "cr_base": base,
            "cr_gp": gp_bonus,
            "wsb": wsb,
            "wsb_known": True,
            "chain": cr + wsb,
            "gp_charas": gps,
            "gp_names": [master.chara_name(g) for g in gps],
        })
    results.sort(key=lambda x: x["chain"], reverse=True)
    return {"target": req.target, "target_name": master.card_name(req.target),
            "target_chara": t, "results": results}


class ExactPairReq(BaseModel):
    target: int  # card_id
    p1_id: int   # trained_chara_id
    p2_id: int   # trained_chara_id


@app.post("/api/affinity/pair")
def api_affinity_pair(req: ExactPairReq):
    """Exact compatibility for a specific pair, using the confirmed formula:
    CalcRelationPoint(t,p1,p2) = CR(t,p1) + CR(t,p2) + RL2(p1,p2) + WSB(p1) + WSB(p2)
    WSB computed offline from win_saddle_id_array -- no capture needed."""
    import affinity as _aff
    ds = ensure_dataset()
    by = {c["trained_chara_id"]: c for c in ds["mine"] + ds["rentable"]}
    p1, p2 = by.get(req.p1_id), by.get(req.p2_id)
    if not p1 or not p2:
        return JSONResponse({"error": "parent not found"}, status_code=404)

    c1, gps1, wsb1 = _uma_wsb_info(p1)
    c2, gps2, wsb2 = _uma_wsb_info(p2)
    t = master.chara_id_of(req.target)

    result = _aff.compatibility(
        t,
        {"chara_id": c1, "gp_charas": gps1},
        {"chara_id": c2, "gp_charas": gps2},
        wsb_p1=wsb1, wsb_p2=wsb2,
    )

    # Enrich with display info
    result["target_name"] = master.card_name(req.target)
    result["p1_name"] = p1["name"]
    result["p2_name"] = p2["name"]
    result["p1_card"] = p1["card_id"]
    result["p2_card"] = p2["card_id"]
    result["p1_gp_names"] = [master.chara_name(g) for g in gps1]
    result["p2_gp_names"] = [master.chara_name(g) for g in gps2]
    result["rank_label"] = master.rank_label(result.get("rank", 1))
    return result


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Team Trials (from TTAnalyzer dashboard_server.py)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/team")
def api_team():
    rows = load_history()
    agg = aggregate(rows)
    # Sort by avg_score desc
    sorted_umas = sorted(agg.values(), key=lambda x: -x["avg_score"])

    # Pull the most recent support_card_bonus from history (per-player value,
    # may evolve over time as the player acquires more cards)
    support_bonus_pct = None
    support_bonus_raw = None
    bonus_history = []
    for row in reversed(rows):
        if row.get("support_bonus_pct") is not None:
            if support_bonus_pct is None:
                support_bonus_pct = row["support_bonus_pct"]
                support_bonus_raw = row.get("support_bonus_raw")
            bonus_history.append(row["support_bonus_pct"])
        if len(bonus_history) >= 75:
            break
    # Unique bonuses observed over time (deduped, latest first)
    seen = set()
    bonus_timeline = []
    for b in bonus_history:
        rounded = round(b, 2)
        if rounded not in seen:
            seen.add(rounded)
            bonus_timeline.append(rounded)

    return {
        "n_trials_estimated": (len(rows) // 15) if rows else 0,
        "n_rows":             len(rows),
        "support_bonus_pct":  support_bonus_pct,
        "support_bonus_raw":  support_bonus_raw,
        "bonus_timeline":     bonus_timeline,
        "umas":               sorted_umas,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Player state
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/player")
def api_player():
    """All per-player varying state: identity, class, support bonus, RP,
    last opponent, last trial result, plus timelines for trending values."""
    latest = player_state.latest_state()
    history = player_state.load_state_history()
    timelines: dict = {}
    if history:
        for field in ("personal_best_point", "current_rank", "team_class",
                      "support_card_bonus_pct", "current_best_point"):
            timelines[field] = player_state.summarize_changes(field, history)
    return {
        "latest":    latest or {},
        "timelines": timelines,
        "snapshots": len(history),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Skill Planner
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/skill_planner/options")
def api_skill_planner_options():
    """Returns the dropdown options (charas/distances/styles available in history)."""
    return skill_planner.available_combinations()


@app.get("/api/skill_planner/plan")
def api_skill_planner_plan(chara_id: int = 0, distance_type: int = 0, running_style: str = ""):
    """Returns recommendations for a given (chara_id, distance, style)."""
    if not (chara_id and distance_type and running_style):
        return JSONResponse({"error": "chara_id, distance_type, running_style all required"}, status_code=400)
    return skill_planner.plan(chara_id, distance_type, running_style)


@app.get("/api/skill_planner/skills")
def api_skill_planner_skills():
    """All skill_ids appearing in history (for autocomplete)."""
    return skill_planner.all_skill_names()


@app.get("/api/skill_planner/lookup")
def api_skill_planner_lookup(q: str = ""):
    """Returns overall + per-uma activation stats for a single skill."""
    q = q.strip()
    if not q:
        return JSONResponse({"error": "missing q"}, status_code=400)
    return skill_planner.skill_lookup(q)


@app.get("/api/skill_planner/act_rates")
def api_skill_planner_act_rates(distance_type: int = 0, running_style: str = ""):
    """Aggregate activation % by (distance_type, running_style), keyed by skill NAME.
    Used by breed optimizer to weight skills by real activation data."""
    running_style = running_style.strip()
    if not (distance_type and running_style):
        return JSONResponse({"error": "distance_type and running_style required"}, status_code=400)

    data = fetch_act_rates(distance_type, running_style)
    if not data:
        return {"distance_type": distance_type, "running_style": running_style,
                "observations": 0, "skills": {}, "universal": {}}
    return {
        "distance_type": distance_type,
        "running_style": running_style,
        "observations": data.get("observations", 0),
        "skills": data.get("skills", {}),
        "universal": data.get("universal", {}),
    }


@app.get("/api/skill_planner/export")
def api_skill_planner_export():
    """Export all skill act% data grouped by (distance, style) for import."""
    return skill_planner.export_for_heir()


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — TT Capture (mitmproxy control)
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/capture/status")
def api_capture_status():
    return tt_capture.status()


@app.post("/api/capture/start")
def api_capture_start():
    return tt_capture.start()


@app.post("/api/capture/stop")
def api_capture_stop():
    """Stop capture, restore proxy, run the analyzer, and report what was added."""
    return tt_capture.stop(run_analyze=True)


@app.post("/api/process")
def api_process():
    """Run the analyzer on whatever is already captured (no capture toggle)."""
    return tt_capture.run_analyzer()


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — Stadium tracker
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/stadium")
def api_stadium(category: str = ""):
    """All rounds (flattened) + summary aggregates for the Track & Condition tab.
    Optional ?category=Mile scopes the summary to that distance bucket."""
    cat = category.strip() or None
    if cat in ("All", "all"):
        cat = None
    return {
        "rows":    stadium_tracker.flatten_rounds(),
        "summary": stadium_tracker.summary(category=cat),
    }


@app.post("/api/stadium/import")
async def api_stadium_import(file: UploadFile = File(...), label: str = Form("upload")):
    """Upload a CSV of stadium rounds (Nguyen-format) and merge it in."""
    import import_external_stadium_csv as importer
    label = label.strip() or "upload"
    try:
        raw = await file.read()
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return JSONResponse({"error": f"could not read file: {e}"}, status_code=400)
    try:
        saved, duplicates, malformed = importer.import_csv_text(text, source_label=label)
    except Exception as e:
        return JSONResponse({"error": f"import failed: {e}"}, status_code=400)
    return {"saved": saved, "duplicates": duplicates, "malformed": malformed}


@app.get("/api/stadium/csv")
def api_stadium_csv():
    """CSV download of all stadium rounds."""
    csv_text = stadium_tracker.to_csv()
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=stadium_rounds.csv"},
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Startup
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print(f"[*] Heaven -> http://127.0.0.1:{PORT}")
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=PORT,
        log_level="warning",
        reload=True,
        reload_dirs=[str(HERE)],
    )
