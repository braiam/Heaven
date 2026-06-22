"""Skill Planner - per-uma skill recommendations for Team Trials.

Aggregates the per-race skill activation data in
data/team_trials_history.jsonl and produces a ranked list of skills to
buy for a given (chara, distance, style) combination, taking SP cost
into account.

Two layers of recommendation:
  1. Chara-specific: same chara + distance + style observed in your history
  2. Cross-uma global: same distance + style across any chara (fallback
     for new charas or thin data)

Per-skill ranking uses:
  - activation_pct = activated / owned
  - sp_cost from master.mdb single_mode_skill_need_point
  - priority = activation_pct - (sp_cost / 200)
  - verdict tag based on both
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import master
import safe_store


DATA_DIR = Path(__file__).parent / "data"
# Safe (AppData) store — survives deleting the project folder (migrated on first use).
HISTORY_PATH = safe_store.history_path()

DIST_LABEL = {1: "Sprint", 2: "Mile", 3: "Medium", 4: "Long", 5: "Dirt"}
STYLE_LABEL = {"NIGE": "Front Runner", "SENKO": "Pace Chaser",
               "SASHI": "Late Surger", "OIKOMI": "End Closer"}


# ── Backward-compat wrappers (delegate to unified master module) ───────────
def skill_name(sid: int) -> str:
    return master.skill_name(sid)


def skill_cost(sid: int) -> int | None:
    """Base SP cost for a skill. Returns None if not found in master.mdb."""
    return master.skill_cost(sid)


def chara_name_by_card_id(card_id: int) -> str:
    return master.chara_name_by_card_id(card_id)


# ── History loader ──────────────────────────────────────────────────────────
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


# ── Combined rows + style/distance filtering ────────────────────────────────
# Team Trials history (distance_type 1-5) + horseACT race dumps (distance_cat
# string). running_style is the same NIGE/SENKO/SASHI/OIKOMI in both.
_TT_DIST = {1: "sprint", 2: "mile", 3: "medium", 4: "long", 5: "dirt"}


def combined_rows() -> list[dict]:
    """Team Trials history + horseACT race-dump rows, as one list."""
    rows = load_history()
    try:
        import race_skills
        rows = rows + race_skills.load_rows()
    except Exception:
        pass
    return rows


def _row_dist(row: dict):
    if row.get("distance_cat"):
        return row["distance_cat"]
    return _TT_DIST.get(row.get("distance_type"))


def _filter_rows(rows: list[dict], style: str | None, dist: str | None) -> list[dict]:
    """Keep rows matching the running style and/or distance bucket. Empty
    filters → all rows (so 0 or 1 filter uses just that slice of the data)."""
    if not style and not dist:
        return rows
    style = style.upper() if style else None
    dist = dist.lower() if dist else None
    out = []
    for r in rows:
        if style and (r.get("running_style") or "").upper() != style:
            continue
        if dist and _row_dist(r) != dist:
            continue
        out.append(r)
    return out


# ── Aggregation ─────────────────────────────────────────────────────────────
def _aggregate(rows: list[dict], group_key: callable) -> dict:
    """Aggregate skill stats grouping by whatever group_key returns.
    Returns: {group_key_value: {skill_id: {owned: int, activated: int}}}."""
    stats: dict = defaultdict(lambda: defaultdict(lambda: {"owned": 0, "activated": 0}))
    for row in rows:
        key = group_key(row)
        if key is None:
            continue
        owned = set(row.get("owned_skills") or [])
        activated = set(row.get("activated_skills") or [])
        for sid in owned:
            stats[key][sid]["owned"] += 1
            if sid in activated:
                stats[key][sid]["activated"] += 1
    return {k: dict(v) for k, v in stats.items()}


def by_chara_dist_style(rows: list[dict]) -> dict:
    """Aggregate per (card_id, distance_type, running_style)."""
    return _aggregate(rows, lambda r: (
        r.get("chara_id"), r.get("distance_type"), r.get("running_style")
    ) if r.get("chara_id") and r.get("distance_type") and r.get("running_style") else None)


def by_dist_style(rows: list[dict]) -> dict:
    """Aggregate per (distance_type, running_style) across all umas."""
    return _aggregate(rows, lambda r: (
        r.get("distance_type"), r.get("running_style")
    ) if r.get("distance_type") and r.get("running_style") else None)


def by_all(rows: list[dict]) -> dict:
    """Aggregate across every race regardless of chara / distance / style.
    Surfaces 'universal' skills (Tail Held High, Sympathy, Groundwork...)
    that proc in any race so they can be considered alongside the
    distance+style specific picks."""
    return _aggregate(rows, lambda r: "ALL")


def _activation_styles(rows: list[dict]) -> dict[int, set[str]]:
    """Per skill_id, the set of running_styles where the skill was
    observed activating. Used to discriminate truly universal skills
    (proc in any style) from style-locked ones (Front Runner Savvy)."""
    out: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        style = row.get("running_style")
        if not style:
            continue
        for sid in row.get("activated_skills") or []:
            out[sid].add(style)
    return out


def _activation_distances(rows: list[dict]) -> dict[int, set[int]]:
    """Per skill_id, the set of distance_types where the skill activated.
    Excludes distance-locked skills (Mile Straightaways, Sprint Corners)."""
    out: dict[int, set[int]] = defaultdict(set)
    for row in rows:
        d = row.get("distance_type")
        if not d:
            continue
        for sid in row.get("activated_skills") or []:
            out[sid].add(d)
    return out


# ── Recommendation ──────────────────────────────────────────────────────────
def _verdict(activation_pct: float, cost: int | None, observations: int) -> str:
    if observations < 5:
        return "NO DATA"
    if activation_pct < 50:
        return "SKIP"
    c = cost if cost is not None else 200
    if activation_pct >= 80 and c <= 200:
        return "MUST BUY"
    if activation_pct >= 60 and c <= 150:
        return "GOOD VALUE"
    if activation_pct >= 80 and c > 300:
        return "EXPENSIVE GAMBLE"
    if activation_pct >= 65:
        return "CONSIDER"
    return "MEH"


def recommend(stats: dict, top_n: int | None = None) -> list[dict]:
    """Given a per-skill stats dict (skill_id -> {owned, activated}),
    returns a sorted list of recommendation dicts."""
    out: list[dict] = []
    for sid, counts in stats.items():
        owned = counts["owned"]
        activated = counts["activated"]
        if owned == 0:
            continue
        pct = (activated / owned) * 100
        cost = skill_cost(sid)
        cost_for_priority = cost if cost is not None else 200
        # Priority blends activation % and cost penalty
        priority = pct - (cost_for_priority / 200) * 5
        verdict = _verdict(pct, cost, owned)
        cost_per_activation = (cost_for_priority / (pct / 100)) if pct > 0 else None
        out.append({
            "skill_id":            sid,
            "name":                skill_name(sid),
            "activation_pct":      round(pct, 1),
            "observations":        owned,
            "activated_count":     activated,
            "sp_cost":             cost,
            "priority":            round(priority, 2),
            "verdict":             verdict,
            "cost_per_activation": round(cost_per_activation, 1) if cost_per_activation else None,
        })
    out.sort(key=lambda x: -x["priority"])
    return out[:top_n] if top_n else out


def plan(chara_id: int, distance_type: int, running_style: str,
         rows: list[dict] | None = None) -> dict:
    """Top-level: return both specific and global recommendations for the
    requested combination.

    chara_id is the 6-digit card_id (e.g. 100501 for Oguri Cap base)."""
    if rows is None:
        rows = load_history()

    by_cds = by_chara_dist_style(rows)
    by_ds = by_dist_style(rows)
    by_a = by_all(rows)

    specific_key = (chara_id, distance_type, running_style)
    global_key = (distance_type, running_style)

    specific_stats = by_cds.get(specific_key, {})
    global_stats = by_ds.get(global_key, {})
    universal_stats = by_a.get("ALL", {})

    specific_obs = sum(s["owned"] for s in specific_stats.values()) if specific_stats else 0
    specific_races = max((s["owned"] for s in specific_stats.values()), default=0)
    global_races = max((s["owned"] for s in global_stats.values()), default=0)
    universal_races = max((s["owned"] for s in universal_stats.values()), default=0)

    # A skill is "universal" if it activates at a high rate across the entire
    # history regardless of distance/style/chara. Require min observations to
    # avoid noise, and a strong activation rate so we only surface real
    # always-on skills (Tail Held High etc.) rather than coincidences.
    # Truly universal = procs in at least 3 of 4 running styles, decent
    # sample size (>=20% of races), and a high overall activation rate.
    # The style filter excludes style-locked picks (Front Runner Savvy,
    # Late Surger Corners) while keeping Tail Held High / Sympathy /
    # Groundwork / Straightaway Adept etc.
    act_styles = _activation_styles(rows)
    act_dists = _activation_distances(rows)
    universal_recs = recommend(universal_stats)
    min_obs = max(5, universal_races // 5)
    universal_recs = [r for r in universal_recs
                      if r["observations"] >= min_obs
                      and r["activation_pct"] >= 70
                      and len(act_styles.get(r["skill_id"], set())) >= 3
                      and len(act_dists.get(r["skill_id"], set())) >= 3]

    return {
        "chara_id":          chara_id,
        "chara_name":        chara_name_by_card_id(chara_id),
        "distance_type":     distance_type,
        "distance_label":    DIST_LABEL.get(distance_type, str(distance_type)),
        "running_style":     running_style,
        "style_label":       STYLE_LABEL.get(running_style, running_style),
        "specific_races":    specific_races,
        "global_races":      global_races,
        "universal_races":   universal_races,
        "specific":          recommend(specific_stats),
        "global":            recommend(global_stats),
        "universal":         universal_recs,
    }


def skill_lookup(skill_query: str | int, rows: list[dict] | None = None,
                 style: str | None = None, dist: str | None = None) -> dict:
    """Look up a single skill by name (case-insensitive substring) or skill_id.
    Returns overall activation rate + per-uma breakdown across all rows.
    Optional `style` (NIGE/SENKO/SASHI/OIKOMI) and `dist`
    (sprint/mile/medium/long/dirt) restrict the data to that running style
    and/or distance bucket; no filter = all data.
    """
    if rows is None:
        rows = combined_rows()
    rows = _filter_rows(rows, style, dist)

    all_names = master._skill_names()

    # Resolve query to one or more skill_ids
    matches: list[int] = []
    if isinstance(skill_query, int) or (isinstance(skill_query, str) and skill_query.isdigit()):
        sid = int(skill_query)
        if sid in all_names:
            matches = [sid]
    else:
        q = skill_query.strip().lower()
        if not q:
            return {"error": "empty query"}
        for sid, name in all_names.items():
            if q in name.lower():
                matches.append(sid)
        # Prefer exact case-insensitive match if it exists
        exact = [sid for sid in matches if all_names[sid].lower() == q]
        if exact:
            matches = exact

    if not matches:
        return {"error": f"no skill matches '{skill_query}'"}

    # Group by display name. Several skill_ids can share the exact same name --
    # e.g. a skill's owned id (1xxxxx) and its inherited/variant id (9xxxxx).
    # Those are the SAME skill to the player, so merge them. Only when the
    # matches have genuinely DIFFERENT names (a partial search like "straight")
    # do we ask the user to disambiguate.
    distinct_names = {all_names[sid] for sid in matches}
    if len(distinct_names) > 1:
        # Limit applies to distinct NAMES, not raw ids (each skill can have
        # several same-name variant ids that we merge anyway).
        if len(distinct_names) > 20:
            return {"error": f"too many matches ({len(distinct_names)}) -- be more specific"}
        # Offer one candidate per distinct name (dedupe same-name variants),
        # carrying every id that shares that name so the pick stays merged.
        by_name: dict[str, list[int]] = {}
        for sid in matches:
            by_name.setdefault(all_names[sid], []).append(sid)
        return {
            "error": "ambiguous",
            "candidates": [{"id": ids[0], "ids": ids, "name": name}
                           for name, ids in sorted(by_name.items())],
        }

    # One logical skill (single name), possibly several variant ids -> merge.
    sid_group = set(matches)
    display_name = next(iter(distinct_names))
    sp_cost = next((c for c in (skill_cost(s) for s in sorted(sid_group)) if c is not None), None)
    primary_sid = min(sid_group)

    # Aggregate overall + per uma over the WHOLE id group (owning/activating
    # any variant counts once for that race).
    per_uma: dict[int, dict] = {}
    overall_owned = 0
    overall_act = 0
    for row in rows:
        owned = set(row.get("owned_skills") or [])
        if not (sid_group & owned):
            continue
        activated = bool(sid_group & set(row.get("activated_skills") or []))
        cid = row.get("chara_id")
        cname = row.get("chara_name") or chara_name_by_card_id(cid) if cid else "?"
        if cid not in per_uma:
            per_uma[cid] = {"chara_id": cid, "chara_name": cname, "owned": 0, "activated": 0}
        per_uma[cid]["owned"] += 1
        if activated:
            per_uma[cid]["activated"] += 1
        overall_owned += 1
        if activated:
            overall_act += 1

    if overall_owned == 0:
        return {
            "skill_id": primary_sid,
            "name": display_name,
            "sp_cost": sp_cost,
            "overall_owned": 0,
            "overall_activated": 0,
            "overall_pct": None,
            "umas": [],
            "note": "No uma in your history owned this skill yet.",
        }

    umas = []
    for cid, s in per_uma.items():
        pct = (s["activated"] / s["owned"]) * 100 if s["owned"] else 0
        umas.append({
            "chara_id":       cid,
            "chara_name":     s["chara_name"],
            "owned":          s["owned"],
            "activated":      s["activated"],
            "activation_pct": round(pct, 1),
        })
    umas.sort(key=lambda u: (-u["activation_pct"], -u["owned"]))

    return {
        "skill_id":          primary_sid,
        "name":              display_name,
        "sp_cost":           sp_cost,
        "overall_owned":     overall_owned,
        "overall_activated": overall_act,
        "overall_pct":       round((overall_act / overall_owned) * 100, 1),
        "umas":              umas,
    }


def all_skill_names(rows: list[dict] | None = None) -> list[dict]:
    """Returns all skill_ids appearing in history with their names for the
    autocomplete dropdown."""
    if rows is None:
        rows = combined_rows()
    names = master._skill_names()
    seen: set[int] = set()
    for r in rows:
        for sid in (r.get("owned_skills") or []):
            seen.add(sid)
    out = [{"id": sid, "name": names.get(sid, f"skill#{sid}")} for sid in seen]
    out.sort(key=lambda x: x["name"])
    return out


def export_for_heir(rows: list[dict] | None = None, min_obs: int = 5) -> dict:
    """Export ALL skill act% data grouped by (distance, style) for Heir import.

    Returns:
    {
      "groups": [
        {
          "distance": "Mile", "distance_type": 2,
          "style": "Front Runner", "style_code": "NIGE",
          "observations": 48,
          "skills": [
            {"name": "Pace Chaser Savvy", "act_pct": 92.3, "owned": 48, "activated": 44},
            ...
          ]
        },
        ...
      ],
      "universal": [
        {"name": "Corner Adept", "act_pct": 88.5, "owned": 200, "activated": 177},
        ...
      ]
    }
    """
    if rows is None:
        rows = load_history()

    ds_stats = by_dist_style(rows)
    all_stats = by_all(rows).get("ALL", {})
    act_styles = _activation_styles(rows)
    act_dists = _activation_distances(rows)

    # Per (distance, style) groups
    groups = []
    for (dist, style), skills in sorted(ds_stats.items()):
        skill_list = []
        for sid, counts in skills.items():
            if counts["owned"] < min_obs:
                continue
            pct = round((counts["activated"] / counts["owned"]) * 100, 1)
            skill_list.append({
                "name": skill_name(sid),
                "act_pct": pct,
                "owned": counts["owned"],
                "activated": counts["activated"],
            })
        skill_list.sort(key=lambda x: -x["act_pct"])
        groups.append({
            "distance": DIST_LABEL.get(dist, str(dist)),
            "distance_type": dist,
            "style": STYLE_LABEL.get(style, style),
            "style_code": style,
            "observations": max((c["owned"] for c in skills.values()), default=0),
            "skills": skill_list,
        })

    # Universal skills (same criteria as plan())
    universal_races = max((s["owned"] for s in all_stats.values()), default=0)
    uni_min = max(min_obs, universal_races // 5)
    universal = []
    for sid, counts in all_stats.items():
        if counts["owned"] < uni_min:
            continue
        pct = round((counts["activated"] / counts["owned"]) * 100, 1)
        if (pct >= 70
                and len(act_styles.get(sid, set())) >= 3
                and len(act_dists.get(sid, set())) >= 3):
            universal.append({
                "name": skill_name(sid),
                "act_pct": pct,
                "owned": counts["owned"],
                "activated": counts["activated"],
            })
    universal.sort(key=lambda x: -x["act_pct"])

    return {"groups": groups, "universal": universal}


def available_combinations(rows: list[dict] | None = None) -> dict:
    """Returns lists of available chara/distance/style options for the UI."""
    if rows is None:
        rows = load_history()
    charas: dict[int, str] = {}
    distances: set[int] = set()
    styles: set[str] = set()
    for r in rows:
        cid = r.get("chara_id")
        if cid:
            charas[cid] = r.get("chara_name") or chara_name_by_card_id(cid)
        d = r.get("distance_type")
        if d:
            distances.add(d)
        st = r.get("running_style")
        if st:
            styles.add(st)
    return {
        "charas":    sorted([{"id": k, "name": v} for k, v in charas.items()], key=lambda x: x["name"]),
        "distances": sorted([{"id": d, "label": DIST_LABEL.get(d, str(d))} for d in distances], key=lambda x: x["id"]),
        "styles":    sorted([{"id": s, "label": STYLE_LABEL.get(s, s)} for s in styles], key=lambda x: x["id"]),
    }
