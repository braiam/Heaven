"""
Unified master.mdb reader for Heaven.
Provides: uma names, skill names, relation/affinity tables, G1 wins, rank labels.
Caches everything in memory on first access.
"""

import glob
import os
import sqlite3
from functools import lru_cache

_MDB_GLOBS = [
    r"%LOCALAPPDATA%/../LocalLow/Cygames/Umamusume/master/master.mdb",
    r"%USERPROFILE%/AppData/LocalLow/Cygames/Umamusume/master/master.mdb",
]


def find_mdb():
    for g in _MDB_GLOBS:
        hits = glob.glob(os.path.expandvars(g))
        if hits:
            return hits[0]
    return None


@lru_cache(maxsize=1)
def _conn():
    path = find_mdb()
    if not path:
        raise FileNotFoundError("No encuentro master.mdb (Umamusume no instalado en este equipo?)")
    return sqlite3.connect(path, check_same_thread=False)


@lru_cache(maxsize=1)
def _card_to_chara():
    cur = _conn().cursor()
    return {row[0]: row[1] for row in cur.execute("select id, chara_id from card_data")}


@lru_cache(maxsize=1)
def _chara_names():
    cur = _conn().cursor()
    return {row[0]: row[1] for row in
            cur.execute('select "index", text from text_data where category=6')}


def chara_id_of(card_id):
    cid = _card_to_chara().get(int(card_id))
    if cid is not None:
        return cid
    return int(card_id) // 100          # fallback: card 100101 -> chara 1001


def card_name(card_id):
    name = _chara_names().get(chara_id_of(card_id))
    return name or f"card {card_id}"


def chara_name(chara_id):
    return _chara_names().get(int(chara_id)) or f"chara {chara_id}"


@lru_cache(maxsize=1)
def relation_members():
    """relation_type -> set(chara_id)."""
    cur = _conn().cursor()
    out = {}
    for rtype, chara in cur.execute("select relation_type, chara_id from succession_relation_member"):
        out.setdefault(rtype, set()).add(chara)
    return out


@lru_cache(maxsize=1)
def relation_points():
    """relation_type -> relation_point."""
    cur = _conn().cursor()
    return {row[0]: row[1] for row in cur.execute("select relation_type, relation_point from succession_relation")}


@lru_cache(maxsize=1)
def chara_relation_types():
    """chara_id -> set(relation_type)  (inverso de relation_members, para velocidad)."""
    out = {}
    for rtype, members in relation_members().items():
        for c in members:
            out.setdefault(c, set()).add(rtype)
    return out


@lru_cache(maxsize=1)
def relation_rank_table():
    """[(rank, min, max), ...] ordenado."""
    cur = _conn().cursor()
    rows = list(cur.execute(
        "select relation_rank, rank_value_min, rank_value_max from succession_relation_rank order by relation_rank"))
    return rows


def rank_of(points):
    for rank, lo, hi in relation_rank_table():
        if lo <= points <= hi:
            return rank
    return 1


@lru_cache(maxsize=1)
def program_grade():
    """single_mode_program.id -> race grade (100=G1, 200=G2, 300=G3, ...)."""
    cur = _conn().cursor()
    inst = {r[0]: r[1] for r in cur.execute("select id, race_id from race_instance")}
    grade = {r[0]: r[1] for r in cur.execute("select id, grade from race")}
    out = {}
    for pid, riid in cur.execute("select id, race_instance_id from single_mode_program"):
        rid = inst.get(riid)
        if rid is not None and rid in grade:
            out[pid] = grade[rid]
    return out


@lru_cache(maxsize=1)
def saddle_to_race_ids():
    """saddle_id -> set of race_ids it covers."""
    cur = _conn().cursor()
    ri_to_race = {r[0]: r[1] for r in cur.execute("select id, race_id from race_instance")}
    out = {}
    for row in cur.execute(
        "select id, race_instance_id_1, race_instance_id_2, race_instance_id_3, "
        "race_instance_id_4, race_instance_id_5, race_instance_id_6, "
        "race_instance_id_7, race_instance_id_8 from single_mode_wins_saddle"
    ):
        sid = row[0]
        rids = set()
        for ri in row[1:]:
            if ri and ri in ri_to_race:
                rids.add(ri_to_race[ri])
        if rids:
            out[sid] = rids
    return out


_RANK_LETTERS = [
    "G", "G+", "F", "F+", "E", "E+", "D", "D+",
    "C", "C+", "B", "B+", "A", "A+", "S", "S+",
    "SS", "SS+", "UG", "UG+", "UF", "UF+", "UE", "UE+",
    "UD", "UD+", "UC", "UC+", "UB", "UB+", "UA", "UA+",
    "US", "US+", "USS", "USS+",
]

def rank_label(rank_id):
    if not rank_id:
        return "?"
    idx = int(rank_id) - 1
    if 0 <= idx < len(_RANK_LETTERS):
        return _RANK_LETTERS[idx]
    return f"R{rank_id}"


@lru_cache(maxsize=1)
def race_instance_names():
    cur = _conn().cursor()
    return {r[0]: r[1] for r in cur.execute(
        'select "index", text from text_data where category = 29'
    )}


_TITLE_SADDLE_NAMES = {
    1: "Classic Triple Crown",
    2: "Triple Tiara",
    3: "Senior Spring Triple Crown",
    4: "Senior Autumn Triple Crown",
    5: "Tenno Sweep",
    6: "Dual Grand Prix",
    7: "Dual Miles",
    8: "Dual Sprints",
    9: "Dual Dirts",
}


def saddle_name(saddle_id):
    if saddle_id in _TITLE_SADDLE_NAMES:
        return _TITLE_SADDLE_NAMES[saddle_id]
    cur = _conn().cursor()
    rows = list(cur.execute(
        "select race_instance_id_1, race_instance_id_2, race_instance_id_3 "
        "from single_mode_wins_saddle where id = ?", (saddle_id,)
    ))
    if not rows or not rows[0]:
        return f"Saddle {saddle_id}"
    names = race_instance_names()
    ri_to_name = []
    for ri in rows[0]:
        if ri and ri in names:
            ri_to_name.append(names[ri])
    if not ri_to_name:
        return f"Saddle {saddle_id}"
    return " + ".join(ri_to_name) if len(ri_to_name) > 1 else ri_to_name[0]


@lru_cache(maxsize=1)
def saddle_types_map():
    cur = _conn().cursor()
    return {r[0]: r[1] for r in cur.execute(
        "select id, win_saddle_type from single_mode_wins_saddle"
    )}


@lru_cache(maxsize=1)
def g1_saddle_ids():
    cur = _conn().cursor()
    ri_grades = dict(cur.execute(
        "select ri.id, race.grade from race_instance ri join race on race.id = ri.race_id"
    ))
    g1 = set()
    for row in cur.execute(
        "select id, race_instance_id_1, race_instance_id_2, race_instance_id_3, "
        "race_instance_id_4, race_instance_id_5, race_instance_id_6, "
        "race_instance_id_7, race_instance_id_8 from single_mode_wins_saddle "
        "where win_saddle_type > 0"
    ):
        ris = [r for r in row[1:] if r and r > 0]
        grades = [ri_grades.get(ri, 0) for ri in ris]
        if grades and all(g == 100 for g in grades):
            g1.add(row[0])
    return g1


def count_g1_wins(race_result_list, win_saddle_id_array=None):
    if win_saddle_id_array:
        g1 = g1_saddle_ids()
        return sum(1 for s in win_saddle_id_array if int(s) in g1)
    pg = program_grade()
    seen = set()
    for r in race_result_list or []:
        if int(r.get("result_rank") or 0) == 1:
            pid = int(r.get("program_id") or 0)
            if pg.get(pid) == 100:
                seen.add(pid)
    return len(seen)


# ── Skill name lookups (used by TT analysis modules) ─────────────────────────

@lru_cache(maxsize=1)
def _skill_names():
    cur = _conn().cursor()
    return {row[0]: row[1] for row in
            cur.execute('select "index", text from text_data where category=47')}


@lru_cache(maxsize=1)
def _chara_names_cat170():
    """Chara names from category 170 (used by TT analyzer for card_id-based lookups)."""
    cur = _conn().cursor()
    return {row[0]: row[1] for row in
            cur.execute('select "index", text from text_data where category=170')}


def skill_name(skill_id: int) -> str:
    return _skill_names().get(skill_id, f"skill#{skill_id}")


def chara_name_by_card_id(card_id: int) -> str:
    return _chara_names_cat170().get((card_id or 0) // 100, f"chara#{card_id}")


@lru_cache(maxsize=1)
def skill_costs():
    """skill_id -> SP cost from single_mode_skill_need_point."""
    cur = _conn().cursor()
    return {int(sid): int(cost or 0) for sid, cost in
            cur.execute("SELECT id, need_skill_point FROM single_mode_skill_need_point")}


def skill_cost(skill_id: int) -> int | None:
    return skill_costs().get(skill_id)
