"""
Heir - inheritance intelligence for your Umamusume account.

Decodes YOUR trained umas (load/index) and the BORROWABLE parents from friends
(pre_single_mode/index), with their SPARKS (blue=stat, pink=aptitude, green=unique,
white=skill/race) and their ancestor tree, using factor_map.json (965 factors)
and the game's master.mdb (names + affinity/相性 tables + G1 wins).

Commands:
    python heir.py scan  [trace.jsonl]     decode -> heir_data.json + spark inventory
    python heir.py breed --target <card_id> [--want speed,Long,...] [trace.jsonl]
        rank the best parent PAIRS (yours + 1 borrowable) to breed that uma,
        combining AFFINITY (relations + shared G1) and SPARK value.

Get your data with:  python capture.py   (reads it from the running game; see README).
It writes a trace to data/, which these commands read automatically.
"""

import argparse
import json
import os
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE                                # repo root (standalone)
sys.path.insert(0, str(HERE))

import master
import affinity

FACTOR_MAP_PATH = HERE / "factor_map.json"
DATA_DIR = HERE / "data"
OUT_PATH = HERE / "heir_data.json"

SPARK_TYPE = {
    "stat": "blue", "aptitude": "pink", "skill": "white",
    "race": "white", "unique": "green", "scenario": "scenario", "other": "other",
}
STAT_NAMES = {"Speed": "speed", "Stamina": "stamina", "Power": "power", "Guts": "guts", "Wit": "wiz"}


def ascii_safe(s):
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(s)).strip()


def _eden_trace_dirs():
    """No external trace dirs — Heaven is self-contained."""
    return []


def find_trace(arg):
    if arg:
        return Path(arg)
    all_dirs = [DATA_DIR] + _eden_trace_dirs()
    cands = []
    for d in all_dirs:
        if d and d.exists():
            cands.extend(d.glob("**/*.jsonl"))
    cands = sorted(set(cands), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def load_factor_map():
    return json.loads(FACTOR_MAP_PATH.read_text(encoding="utf-8"))


def decode_sparks(factor_info_array, fmap):
    out = []
    for f in factor_info_array or []:
        fid = f.get("factor_id")
        info = fmap.get(str(fid), {})
        cat = info.get("category", "other")
        out.append({
            "id": fid, "name": info.get("name", f"Unknown({fid})"),
            "stars": info.get("stars", (fid or 0) % 100),
            "category": cat, "type": SPARK_TYPE.get(cat, "other"),
        })
    return out


# ── In-game aptitude grades (S..G) ──────────────────────────────────────────
# The game stores each trained uma's aptitudes as proper_* integer ranks 1..8.
# These are the letter grades the player sees on the character screen — distinct
# from pink sparks (which are inheritance factors measured in stars).
APT_GRADE = {8: "S", 7: "A", 6: "B", 5: "C", 4: "D", 3: "E", 2: "F", 1: "G"}

# proper_* field -> (group, Global label). Labels match the inventory UI's
# APT_SURF / APT_DIST / APT_STYLE names exactly so the frontend can key by name.
APT_FIELDS = (
    ("proper_ground_turf",          "surface",  "Turf"),
    ("proper_ground_dirt",          "surface",  "Dirt"),
    ("proper_distance_short",       "distance", "Sprint"),
    ("proper_distance_mile",        "distance", "Mile"),
    ("proper_distance_middle",      "distance", "Medium"),
    ("proper_distance_long",        "distance", "Long"),
    ("proper_running_style_nige",   "style",    "Front Runner"),
    ("proper_running_style_senko",  "style",    "Pace Chaser"),
    ("proper_running_style_sashi",  "style",    "Late Surger"),
    ("proper_running_style_oikomi", "style",    "End Closer"),
)


def parse_aptitudes(entry):
    """Extract the 10 in-game aptitude grades from a trained_chara entry.
    Returns {label: {"grade": "S", "rank": 8, "group": "distance"}}.
    Empty dict for older captures that lack the proper_* fields."""
    out = {}
    for field, group, label in APT_FIELDS:
        v = entry.get(field)
        if isinstance(v, int) and 1 <= v <= 8:
            out[label] = {"grade": APT_GRADE[v], "rank": v, "group": group}
    return out


def parse_chara(entry, fmap, owner_name=None):
    return {
        "trained_chara_id": entry.get("trained_chara_id"),
        "owner_viewer_id": entry.get("owner_viewer_id"),
        "owner_name": owner_name,
        "card_id": entry.get("card_id"),
        "name": master.card_name(entry.get("card_id")),
        "rank": entry.get("rank"),
        "rank_score": entry.get("rank_score"),
        "fans": entry.get("fans"),
        "win_saddle_id_array": entry.get("win_saddle_id_array") or [],
        "race_result_list": entry.get("race_result_list") or [],
        "stats": {s: entry.get(s, 0) for s in ("speed", "stamina", "power", "guts", "wiz")},
        "apt": parse_aptitudes(entry),
        "own_sparks": decode_sparks(entry.get("factor_info_array"), fmap),
        "grandparents": [
            {"position_id": g.get("position_id"), "card_id": g.get("card_id"),
             "sparks": decode_sparks(g.get("factor_info_array"), fmap),
             "win_saddle_id_array": g.get("win_saddle_id_array") or []}
            for g in entry.get("succession_chara_array") or []
        ],
    }


def parse_pre_single_mode(inner, fmap):
    blk = inner.get("succession_trained_chara_data") or {}
    names = {u.get("viewer_id"): u.get("name") for u in blk.get("summary_user_info_array") or []}
    return [parse_chara(e, fmap, owner_name=names.get(e.get("viewer_id")))
            for e in blk.get("succession_trained_chara_array") or []]


def parse_load_index(inner, fmap):
    return [parse_chara(e, fmap) for e in inner.get("trained_chara") or []]


def _parse_trace_file(path, fmap):
    """Return (mine, rentable) found in a single trace file. Either can be []."""
    mine, rentable = [], []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("direction") != "RES":
            continue
        ep = rec.get("endpoint", "")
        inner = (rec.get("data") or {}).get("data") or rec.get("data") or {}
        if ep.endswith("pre_single_mode/index") and not rentable:
            rentable = parse_pre_single_mode(inner, fmap)
        if ep.endswith("load/index") and not mine:
            mine = parse_load_index(inner, fmap)
    return mine, rentable


def load_raw_trained_chara(path=None):
    """Return the RAW trained_chara entries from the trace (untouched game data).
    Used to re-export the user's umas into formats other tools accept (UmaExtractor
    data.json, hakuraku veteran import, etc).
    Scans Heir data/ + Eden trace dirs and picks the newest one with mine data."""
    all_dirs = [DATA_DIR] + _eden_trace_dirs()
    all_traces = []
    for d in all_dirs:
        if d and d.exists():
            all_traces.extend(d.glob("**/*.jsonl"))
    all_traces = sorted(set(all_traces), key=lambda p: p.stat().st_mtime, reverse=True)
    if path:
        all_traces = [Path(path)] + [p for p in all_traces if p != Path(path)]

    raw_mine = []
    raw_rentable = []
    for tp in all_traces:
        if raw_mine and raw_rentable:
            break
        try:
            for line in tp.read_text(encoding="utf-8", errors="ignore").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("direction") != "RES":
                    continue
                ep = rec.get("endpoint", "")
                inner = (rec.get("data") or {}).get("data") or rec.get("data") or {}
                if ep.endswith("load/index") and not raw_mine:
                    raw_mine = list(inner.get("trained_chara") or [])
                if ep.endswith("pre_single_mode/index") and not raw_rentable:
                    blk = inner.get("succession_trained_chara_data") or {}
                    raw_rentable = list(blk.get("succession_trained_chara_array") or [])
        except Exception:
            continue
    return raw_mine, raw_rentable


def load_dataset_from_trace(path, fmap):
    """Load mine+rentable from path, falling back to other traces for missing halves.
    Scans Heir data/ and Eden trace dirs so a fresh capture (rentable only) can
    complement an Eden trace that has mine, and vice-versa."""
    all_dirs = [DATA_DIR] + _eden_trace_dirs()
    all_traces = []
    for d in all_dirs:
        if d and d.exists():
            all_traces.extend(d.glob("**/*.jsonl"))
    all_traces = sorted(set(all_traces), key=lambda p: p.stat().st_mtime)
    # Requested path first, then rest newest-first (by mtime)
    others = [p for p in reversed(all_traces) if p != path]
    mine, rentable = _parse_trace_file(path, fmap)
    for p in others:
        if mine and rentable:
            break
        m, r = _parse_trace_file(p, fmap)
        if not mine and m:
            mine = m
        if not rentable and r:
            rentable = r
    return {"mine": mine, "rentable": rentable}


# ---------- agregados / scoring ----------


def spark_summary(charas):
    blue, pink, white = Counter(), Counter(), Counter()
    for c in charas:
        for sp in c["own_sparks"]:
            {"blue": blue, "pink": pink, "white": white}.get(sp["type"], Counter())[sp["name"]] += sp["stars"]
    return blue, pink, white


def total_own_stars(c):
    return sum(sp["stars"] for sp in c["own_sparks"])


def blue_strength(c):
    """Fuerza de stat heredada (azul): estrellas propias (1.0) + abuelos (0.4) por stat."""
    out = {v: 0.0 for v in STAT_NAMES.values()}
    for sp in c["own_sparks"]:
        if sp["type"] == "blue" and sp["name"] in STAT_NAMES:
            out[STAT_NAMES[sp["name"]]] += sp["stars"]
    for g in c["grandparents"]:
        for sp in g["sparks"]:
            if sp["type"] == "blue" and sp["name"] in STAT_NAMES:
                out[STAT_NAMES[sp["name"]]] += sp["stars"] * 0.4
    return out


def score_for_want(c, want_lower):
    def m(sparks, w):
        return sum(sp["stars"] * w for sp in sparks if sp["name"].lower() in want_lower)
    s = m(c["own_sparks"], 1.0)
    for g in c["grandparents"]:
        s += m(g["sparks"], 0.4)
    return s


def spark_richness(c):
    """Generico: estrellas azul+rosa (propias 1.0 + abuelos 0.4)."""
    def m(sparks, w):
        return sum(sp["stars"] * w for sp in sparks if sp["type"] in ("blue", "pink"))
    s = m(c["own_sparks"], 1.0)
    for g in c["grandparents"]:
        s += m(g["sparks"], 0.4)
    return s


def score_for_want_weighted(c, want_lower, skill_weights):
    """Like score_for_want but multiplies each spark by its quality weight.
    skill_weights: {name_lower: multiplier}.  Missing → 1.0.  Zero → excluded."""
    if not skill_weights:
        return score_for_want(c, want_lower)

    def m(sparks, depth_w):
        total = 0.0
        for sp in sparks:
            nl = sp["name"].lower()
            if nl not in want_lower:
                continue
            qw = skill_weights.get(nl, 1.0)
            if qw <= 0:
                continue
            total += sp["stars"] * depth_w * qw
        return total

    s = m(c["own_sparks"], 1.0)
    for g in c["grandparents"]:
        s += m(g["sparks"], 0.4)
    return s


def parent_spark_value(c, want_lower, skill_weights=None):
    if want_lower:
        return score_for_want_weighted(c, want_lower, skill_weights) if skill_weights else score_for_want(c, want_lower)
    return spark_richness(c)


# ── Proc-based scoring (10/10 model) ────────────────────────────────────
# Instead of  obj = w_aff × affinity + w_spark × star_count,
# we compute  obj = Σ P(≥1 proc) for each desired spark.
# Affinity is already INSIDE the proc chance — no separate weights needed.

# Base proc rates (shared with server.py — duplicated here to avoid circular import)
_INHERITANCE_BASE = {
    "blue":     {1: 0.70, 2: 0.80, 3: 0.90},
    "stat":     {1: 0.70, 2: 0.80, 3: 0.90},
    "aptitude": {1: 0.01, 2: 0.03, 3: 0.05},
    "unique":   {1: 0.05, 2: 0.10, 3: 0.15},
    "skill":    {1: 0.03, 2: 0.06, 3: 0.09},
    "scenario": {1: 0.03, 2: 0.06, 3: 0.09},
    "race":     {1: 0.01, 2: 0.02, 3: 0.03},
}
_EVENTS_FULL_RUN = 2

def _base_rate(cat: str, stars: int) -> float:
    if cat not in _INHERITANCE_BASE:
        return 0.0
    s = max(1, min(3, stars))
    return _INHERITANCE_BASE[cat].get(s, 0.0)


def expected_proc_score(p1: dict, p2: dict, target_card: int,
                        want_lower: set | None = None,
                        skill_weights: dict | None = None) -> dict:
    """Compute the expected proc score for a parent pair.

    Returns {
        "eproc":   float — sum of P(≥1) across wanted (or all non-blue) sparks,
        "eblue":   float — sum of P(≥1) for blue stat sparks,
        "detail":  [{name, cat, ge1_pct, stars_total}, ...],
        "affinity_total": int,
    }

    This is the TRUE objective: affinity is already inside each proc chance.
    No artificial w_affinity / w_spark needed.
    """
    import affinity as _aff

    # ── Affinity ───────────────────────────────────────────────
    comp = _aff.compatibility_from_parsed(target_card, p1, p2)
    indiv = _aff.individual_affinities_from_parsed(target_card, p1, p2)

    src_aff = {
        "p1":     indiv.get("p1", 0),
        "p1_gp1": indiv.get("p1_gp1", 0),
        "p1_gp2": indiv.get("p1_gp2", 0),
        "p2":     indiv.get("p2", 0),
        "p2_gp1": indiv.get("p2_gp1", 0),
        "p2_gp2": indiv.get("p2_gp2", 0),
    }

    # ── Collect all sparks from 6 sources ──────────────────────
    sparks = {}  # name -> {cat, type, sources: {src_key: stars}}

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

    # ── Compute proc for each spark ────────────────────────────
    n_events = _EVENTS_FULL_RUN
    detail = []
    eproc = 0.0
    eblue = 0.0

    for sp in sparks.values():
        cat = sp["cat"]
        rate_cat = "blue" if cat in ("stat", "blue") else cat
        is_blue = rate_cat == "blue"

        # Per-source proc rates
        source_rates = []
        for sk, stars in sp["sources"].items():
            if stars <= 0:
                continue
            base = _base_rate(rate_cat, stars)
            if base <= 0:
                continue
            aff = src_aff.get(sk, 0)
            rate = min(1.0, base * (1 + aff / 100))
            source_rates.append(rate)

        if not source_rates:
            continue

        # Combined P(≥1 proc over n_events)
        miss = 1.0
        for r in source_rates:
            miss *= (1 - r)
        combined = 1 - miss
        ge1 = 1 - (1 - combined) ** n_events

        total_stars = sum(sp["sources"].values())
        entry = {"name": sp["name"], "cat": rate_cat,
                 "ge1_pct": round(ge1 * 100, 2), "stars_total": total_stars}
        detail.append(entry)

        # Accumulate score
        if is_blue:
            eblue += ge1
        else:
            # Apply skill_weights if provided
            weight = 1.0
            if skill_weights:
                weight = skill_weights.get(sp["name"].lower(), 1.0)
            if want_lower:
                if sp["name"].lower() in want_lower:
                    eproc += ge1 * weight
            else:
                eproc += ge1 * weight

    # Count sparks for averaging
    n_proc = sum(1 for d in detail if d["cat"] != "blue")
    n_blue = sum(1 for d in detail if d["cat"] == "blue")

    return {
        "eproc": round(eproc, 4),          # sum of P(>=1) for wanted/all non-blue
        "eblue": round(eblue, 4),          # sum of P(>=1) for blue stats
        "eproc_n": n_proc,                 # how many non-blue sparks contributed
        "eblue_n": n_blue,                 # how many blue sparks contributed
        "eproc_avg": round(eproc / n_proc * 100, 1) if n_proc else 0,  # avg proc %
        "eblue_avg": round(eblue / n_blue * 100, 1) if n_blue else 0,  # avg blue proc %
        "detail": detail,
        "affinity_total": comp["total"],
        "affinity_detail": comp,
    }


def all_spark_names(ds):
    """Nombres unicos de spark por tipo (para autocompletado/desplegables en la UI).
    Scenario sparks (TS Climax, etc.) are bucketed under 'white' since they are
    white-type in the inheritance sense."""
    buckets = {"blue": set(), "pink": set(), "green": set(), "white": set()}
    for c in ds["mine"] + ds["rentable"]:
        sets = [c["own_sparks"]] + [g["sparks"] for g in c["grandparents"]]
        for sl in sets:
            for sp in sl:
                t = sp["type"]
                if t in buckets:
                    buckets[t].add(sp["name"])
                elif t == "scenario":
                    buckets["white"].add(sp["name"])
    return {k: sorted(v) for k, v in buckets.items()}


def chara_brief(c, want_lower):
    matched = [f"{sp['name']}*{sp['stars']}" for sp in c["own_sparks"]
               if not want_lower or sp["name"].lower() in want_lower]
    return {
        "trained_chara_id": c["trained_chara_id"],
        "card_id": c["card_id"],
        "name": c["name"],
        "rank": c["rank"],
        "owner_name": c.get("owner_name"),
        "matched": matched,
        "blue": {k: round(v, 1) for k, v in blue_strength(c).items() if v},
    }


def optimize_breed(ds, target, want, w_affinity=2.0, w_spark=1.0,
                   own_pool=60, rent_pool=40, top=15, allowed_ids=None,
                   skill_weights=None, use_eproc=True):
    """Devuelve {own:[...], rental:[...]} con las mejores parejas.

    When use_eproc=True (default), uses the expected-proc model:
        obj = eproc (sum of P(>=1) for wanted sparks) + eblue × 0.5
    Affinity is INSIDE the proc chances — no separate weights needed.

    When use_eproc=False, falls back to legacy weighted model:
        obj = w_affinity × total_affinity + w_spark × star_count
    """
    want_lower = {w.strip().lower() for w in want if w.strip()}
    mine = ds["mine"]
    if allowed_ids is not None:
        allowed = set(allowed_ids)
        mine = [c for c in mine if c["trained_chara_id"] in allowed]
    rentable = ds["rentable"]

    # los 2 padres NO pueden ser el mismo personaje (regla del juego)
    # y ningún padre puede ser el mismo personaje que el target
    chid = {id(c): master.chara_id_of(c["card_id"]) for c in mine + rentable}
    target_ch = master.chara_id_of(target)
    mine = [c for c in mine if chid[id(c)] != target_ch]
    rentable = [c for c in rentable if chid[id(c)] != target_ch]

    if use_eproc:
        # ── Expected-proc model (10/10) ───────────────────────────
        def evaluate(p1, p2):
            ep = expected_proc_score(p1, p2, target, want_lower, skill_weights)
            # Objective: average proc% across all sparks (weighted)
            obj = ep["eproc"] + ep["eblue"] * 0.5
            b1, b2 = blue_strength(p1), blue_strength(p2)
            blue = {k: round(b1[k] + b2[k], 1) for k in STAT_NAMES.values() if b1[k] + b2[k]}
            return {
                "obj": round(obj * 100, 1),
                "eproc": ep["eproc_avg"],          # avg proc % of non-blue sparks
                "eproc_n": ep["eproc_n"],           # how many sparks
                "eblue": ep["eblue_avg"],           # avg blue stat proc %
                "eblue_n": ep["eblue_n"],
                "affinity": ep["affinity_total"],
                "shared_g1": 0,
                "affinity_detail": ep["affinity_detail"],
                "spark": ep["eproc_avg"],
                "blue": blue,
                "p1": chara_brief(p1, want_lower),
                "p2": chara_brief(p2, want_lower),
            }

        # Pre-filter pool by individual spark relevance
        def _prescore(c):
            """Quick pre-score for pool ranking (without pair context)."""
            s = parent_spark_value(c, want_lower, skill_weights)
            return s + spark_richness(c) * 0.3
        sv = {id(c): _prescore(c) for c in mine + rentable}

    else:
        # ── Legacy weighted model ─────────────────────────────────
        sv = {id(c): parent_spark_value(c, want_lower, skill_weights) for c in mine + rentable}

        def evaluate(p1, p2):
            comp = affinity.compatibility_from_parsed(target, p1, p2)
            spark = sv[id(p1)] + sv[id(p2)]
            obj = w_affinity * comp["total"] + w_spark * spark
            b1, b2 = blue_strength(p1), blue_strength(p2)
            blue = {k: round(b1[k] + b2[k], 1) for k in STAT_NAMES.values() if b1[k] + b2[k]}
            return {
                "obj": round(obj, 1), "affinity": comp["total"],
                "shared_g1": 0,
                "affinity_detail": comp, "spark": round(spark, 1), "blue": blue,
                "p1": chara_brief(p1, want_lower), "p2": chara_brief(p2, want_lower),
            }

    own = []
    pool = sorted(mine, key=lambda c: sv[id(c)], reverse=True)[:own_pool]
    for a, b in combinations(pool, 2):
        if chid[id(a)] == chid[id(b)]:
            continue
        own.append(evaluate(a, b))
    own.sort(key=lambda x: x["obj"], reverse=True)

    rental = []
    if rentable:
        top_mine = sorted(mine, key=lambda c: sv[id(c)], reverse=True)[:rent_pool]
        for a in top_mine:
            for r in rentable:
                if chid[id(a)] == chid[id(r)]:
                    continue
                rental.append(evaluate(a, r))
        rental.sort(key=lambda x: x["obj"], reverse=True)

    return {"own": own[:top], "rental": rental[:top],
            "model": "eproc" if use_eproc else "legacy"}


# ---------- comandos ----------

def _get_dataset(args, fmap):
    path = find_trace(getattr(args, "trace", None))
    if not path or not path.exists():
        print("[-] No trace found. Capture one first:  python capture.py   (see README)")
        print("    or pass a path:  python heir.py scan <trace.jsonl>")
        return None
    print(f"[*] Trace: {path}")
    return load_dataset_from_trace(path, fmap)


def cmd_scan(args):
    fmap = load_factor_map()
    ds = _get_dataset(args, fmap)
    if ds is None:
        return 1
    print(f"[+] Tus umas: {len(ds['mine'])}  |  Padres prestables: {len(ds['rentable'])}")
    OUT_PATH.write_text(json.dumps(ds, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] Volcado completo en {OUT_PATH}")

    if ds["mine"]:
        blue, pink, white = spark_summary(ds["mine"])
        print("\n=== INVENTARIO DE SPARKS (tus umas, propios) ===")
        print("Azules (stat):", dict(blue.most_common()))
        print("Rosas (aptitud) top10:", [(ascii_safe(k), v) for k, v in pink.most_common(10)])
        print("Blancos (skill) top10:", [(ascii_safe(k), v) for k, v in white.most_common(10)])
        print("\n=== TUS UMAS top10 por estrellas de spark propias ===")
        for c in sorted(ds["mine"], key=total_own_stars, reverse=True)[:10]:
            blues = [f"{ascii_safe(sp['name'])}*{sp['stars']}" for sp in c["own_sparks"] if sp["type"] == "blue"]
            print(f"  {ascii_safe(c['name']):20s} rank {c['rank']} | {total_own_stars(c)} estrellas | azul: {blues}")
    return 0


def cmd_breed(args):
    fmap = load_factor_map()
    ds = _get_dataset(args, fmap)
    if ds is None:
        return 1
    target = int(args.target)
    want = [w.strip() for w in (args.want or "").split(",") if w.strip()]
    want_lower = {w.lower() for w in want}
    mine, rentable = ds["mine"], ds["rentable"]
    if not mine:
        print("[-] No hay umas tuyas en los datos.")
        return 1

    print(f"[*] Criar: {ascii_safe(master.card_name(target))} (card {target})")
    print(f"[*] Objetivo de sparks: {want or '(generico: riqueza de sparks)'}")
    print(f"[*] Pool: {len(mine)} tuyas, {len(rentable)} prestables")
    print(f"[*] Pesos: afinidad x{args.w_affinity}, spark x{args.w_spark}\n")

    res = optimize_breed(ds, target, want, args.w_affinity, args.w_spark, top=8)

    def show(combos, label):
        print(f"=== TOP {label} ===")
        for cb in combos:
            own = f" [{ascii_safe(cb['p2']['owner_name'])}]" if cb['p2'].get("owner_name") else ""
            print(f"  obj {cb['obj']:6.1f} | afin {cb['affinity']:>3} (G1 comp {cb['shared_g1']}) | spark {cb['spark']:4.1f}")
            print(f"      P1 {ascii_safe(cb['p1']['name']):18s} (r{cb['p1']['rank']})  +  "
                  f"P2 {ascii_safe(cb['p2']['name']):18s} (r{cb['p2']['rank']}){own}")
            print(f"      stat azul heredado: {cb['blue']}")
        print()

    show(res["own"], "PAREJAS 100% TUYAS")
    if res["rental"]:
        show(res["rental"], "TUYA + PADRE PRESTABLE (slot rental)")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Heir - Umamusume inheritance intelligence")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="decode trace -> spark inventory")
    s.add_argument("trace", nargs="?", default=None)
    s.set_defaults(func=cmd_scan)

    b = sub.add_parser("breed", help="rank best parent pairs to breed a uma")
    b.add_argument("--target", required=True, help="card_id of the uma to breed (for affinity)")
    b.add_argument("--want", default="", help="wanted sparks, e.g. speed,stamina,Long")
    b.add_argument("--w-affinity", type=float, default=2.0, dest="w_affinity")
    b.add_argument("--w-spark", type=float, default=1.0, dest="w_spark")
    b.add_argument("trace", nargs="?", default=None)
    b.set_defaults(func=cmd_breed)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
