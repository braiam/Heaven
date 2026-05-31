"""
Afinidad / compatibilidad (相性) de herencia — formula EXACTA confirmada via Frida hooks.

Formula:
  CalcRelationPoint(trainee, p1, p2) =
      CR(t, p1) + CR(t, p2) + RL2(p1, p2) + WSB(p1) + WSB(p2)

  Donde:
    CR(t, p) = RL2(t, p_chara) + RL3(t, p_chara, gp1_chara) + RL3(t, p_chara, gp2_chara)
    RL2(a, b) = sum(relation_point for shared relation_types between a and b)
    RL3(a, b, c) = RL2(a, b) filtered by relation_types that ALSO include c
                   (but NO EXTRA point for the c membership itself)
    WSB(p)    = Win-Saddle Bonus = |parent_saddles ∩ GP1_saddles| + |parent_saddles ∩ GP2_saddles|
                Computed OFFLINE from the parent's and its GPs' win_saddle_id_array.
                Confirmed 261/261 exact match against the game's GetWinSaddleRelationBonus.

  Nota: RL2(t, p_chara) NO se calcula para trainee consigo mismo.
        El trainee es fresco (sin victorias), asi que WSB(trainee) = 0.
        El cross-term RL2(p1, p2) es solo base (sin GPs).
"""

import master

# ── Relation calculation (offline, from master.mdb) ──────────────────────────

def rl2(chara_a: int, chara_b: int) -> int:
    """RL2: Relation points between two charas (shared relation_type groups).
    Returns 0 if same chara (game doesn't grant self-affinity)."""
    if chara_a == chara_b or not chara_a or not chara_b:
        return 0
    crt = master.chara_relation_types()
    pts = master.relation_points()
    shared = crt.get(chara_a, set()) & crt.get(chara_b, set())
    return sum(pts.get(rt, 0) for rt in shared)


def rl3(chara_a: int, chara_b: int, chara_gp: int) -> int:
    """RL3: Relation points between a and b, filtered to types that also include gp.
    This is NOT rl2(a,b) + rl2(a,gp) — it's the subset of a×b types where gp is a member.
    No extra points for gp membership itself."""
    if chara_a == chara_b or not chara_a or not chara_b or not chara_gp:
        return 0
    crt = master.chara_relation_types()
    pts = master.relation_points()
    shared_ab = crt.get(chara_a, set()) & crt.get(chara_b, set())
    gp_types = crt.get(chara_gp, set())
    # Only types where GP is also a member
    filtered = shared_ab & gp_types
    return sum(pts.get(rt, 0) for rt in filtered)


def calc_relation(trainee: int, parent_chara: int, gp_charas: list[int]) -> int:
    """CR(trainee, parent) = RL2(t,p) + RL3(t,p,gp1) + RL3(t,p,gp2).
    gp_charas: list of 0-2 grandparent chara_ids."""
    base = rl2(trainee, parent_chara)
    gp_bonus = 0
    for gp in (gp_charas or []):
        if gp and gp > 0:
            gp_bonus += rl3(trainee, parent_chara, gp)
    return base + gp_bonus


# ── WSB (Win-Saddle Bonus) — computed OFFLINE ────────────────────────────────

def wsb_offline(parent_saddles: list[int],
                gp_saddles: list[list[int]]) -> int:
    """Compute WSB purely offline.

    Formula (confirmed 261/261 vs game's GetWinSaddleRelationBonus):
      WSB(parent) = |parent_saddles ∩ GP1_saddles| + |parent_saddles ∩ GP2_saddles|

    Args:
        parent_saddles: parent's win_saddle_id_array
        gp_saddles:     list of 0-2 arrays, each GP's win_saddle_id_array
    """
    ps = set(parent_saddles or [])
    total = 0
    for gp_wins in (gp_saddles or [])[:2]:  # only direct GPs (first 2)
        total += len(ps & set(gp_wins or []))
    return total


def wsb_from_parsed(uma: dict) -> int:
    """Compute WSB from a parse_chara dict (has win_saddle_id_array + grandparents)."""
    parent_saddles = uma.get("win_saddle_id_array") or []
    gp_saddles = [
        g.get("win_saddle_id_array") or []
        for g in uma.get("grandparents") or []
    ]
    return wsb_offline(parent_saddles, gp_saddles)


# ── Full compatibility ───────────────────────────────────────────────────────

def compatibility(trainee_chara: int, p1: dict, p2: dict,
                  wsb_p1: int | None = None, wsb_p2: int | None = None) -> dict:
    """Calcula affinity exacta.

    Args:
        trainee_chara: chara_id del trainee (e.g. 1006 = Oguri Cap)
        p1, p2: dicts con al menos:
            - chara_id: int (chara del parent)
            - gp_charas: list[int] (chara_ids de los 2 GPs, puede estar vacio)
        wsb_p1, wsb_p2: WSB values (None = unknown, will show as 0)

    Returns dict with full breakdown.
    """
    c1 = p1.get("chara_id", 0)
    c2 = p2.get("chara_id", 0)
    gps1 = p1.get("gp_charas", [])
    gps2 = p2.get("gp_charas", [])

    cr1 = calc_relation(trainee_chara, c1, gps1)
    cr2 = calc_relation(trainee_chara, c2, gps2)
    cross = rl2(c1, c2)

    w1 = wsb_p1 if wsb_p1 is not None else 0
    w2 = wsb_p2 if wsb_p2 is not None else 0

    total = cr1 + cr2 + cross + w1 + w2

    # Breakdown por parent
    base1 = rl2(trainee_chara, c1)
    base2 = rl2(trainee_chara, c2)
    gp_bonus1 = cr1 - base1
    gp_bonus2 = cr2 - base2

    return {
        "total": total,
        "cr1": cr1,
        "cr2": cr2,
        "cross": cross,
        "wsb_p1": w1,
        "wsb_p2": w2,
        "wsb_p1_known": wsb_p1 is not None,
        "wsb_p2_known": wsb_p2 is not None,
        # Per-parent detail
        "p1_base": base1,
        "p1_gp_bonus": gp_bonus1,
        "p1_chain": cr1 + w1,
        "p2_base": base2,
        "p2_gp_bonus": gp_bonus2,
        "p2_chain": cr2 + w2,
        # Rank (from master)
        "rank": master.rank_of(total),
    }


def compatibility_from_parsed(trainee_card: int, p1: dict, p2: dict) -> dict:
    """Convenience: takes parse_chara dicts (with card_id, grandparents).
    Computes WSB offline from the parsed data (no bridge needed).

    p1, p2: dicts from heir.parse_chara (card_id, grandparents with card_id + win_saddle_id_array).
    """
    t = master.chara_id_of(trainee_card)
    c1 = master.chara_id_of(p1["card_id"])
    c2 = master.chara_id_of(p2["card_id"])

    # Extract GP chara_ids
    gps1 = [master.chara_id_of(g["card_id"]) for g in p1.get("grandparents", []) if g.get("card_id")]
    gps2 = [master.chara_id_of(g["card_id"]) for g in p2.get("grandparents", []) if g.get("card_id")]

    # Compute WSB offline — no bridge needed!
    wsb1 = wsb_from_parsed(p1)
    wsb2 = wsb_from_parsed(p2)

    return compatibility(
        trainee_chara=t,
        p1={"chara_id": c1, "gp_charas": gps1},
        p2={"chara_id": c2, "gp_charas": gps2},
        wsb_p1=wsb1,
        wsb_p2=wsb2,
    )
