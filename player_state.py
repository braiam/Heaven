"""Player-specific state extractor.

Every field that varies between players is collected here. The same Team Trials
session that we analyze for uma performance also reveals plenty about the
player themselves: support bonus, team class, personal best, RP, opponent
strength tier, etc.

These get persisted to data/player_state.jsonl with a timestamp so we can
show trends (rank up/down, bonus growth, RP cycles) on the dashboard.

To add a new player-specific field:
  1. Add an entry to FIELD_EXTRACTORS below with a path or extractor function
  2. The dashboard will surface it automatically in the "Your status" panel
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable


import safe_store
import jsonl_util

# Persist in the portable safe dir (%LOCALAPPDATA%\Heaven) like history/stadium,
# so the player's rank/RP/bonus history survives re-downloading the project.
STATE_PATH = safe_store.player_state_path()


def _get_nested(obj: Any, path: list[str]) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (IndexError, ValueError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


# Each entry: (label, endpoint, path, optional transform)
# Path traverses inside `payload["data"]`.
FIELD_EXTRACTORS: list[tuple[str, str, list[str], Callable | None]] = [
    # Identity
    ("viewer_id",                 "team_stadium/index",            ["team_stadium_user", "viewer_id"], None),
    ("player_name",               "team_stadium/index",            ["team_stadium_user", "name"], None),

    # Class / rank / score
    ("team_class",                "team_stadium/index",            ["ranking", "team_class"], None),
    ("personal_best_point",       "team_stadium/index",            ["team_stadium_user", "best_point"], None),
    ("current_rank",              "team_stadium/index",            ["ranking", "rank"], None),
    ("current_best_point",        "team_stadium/index",            ["ranking", "best_point"], None),

    # Promotion / demotion thresholds
    ("promotion_threshold",       "team_stadium/index",            ["border_line", "promotion_point"], None),
    ("demotion_threshold",        "team_stadium/index",            ["border_line", "demotion_point"], None),
    ("keep_threshold",            "team_stadium/index",            ["border_line", "keep_point"], None),

    # Resource (RP)
    ("current_rp",                "team_stadium/start",            ["rp_info", "current_rp"], None),
    ("max_rp",                    "team_stadium/start",            ["rp_info", "max_rp"], None),
    ("rp_recovery_seconds",       "team_stadium/start",            ["rp_info", "max_recovery_time"], None),

    # Support card bonus (raw int, /100 = pct)
    ("support_card_bonus_raw",    "team_stadium/start",            ["support_card_bonus"], None),
    ("support_card_bonus_pct",    "team_stadium/start",            ["support_card_bonus"], lambda v: v / 100.0 if v else None),

    # Opponent info this match
    ("opponent_viewer_id",        "team_stadium/decide_frame_order", ["opponent_info_copy", "opponent_viewer_id"], None),
    ("opponent_strength_tier",    "team_stadium/decide_frame_order", ["opponent_info_copy", "strength"], None),
    ("opponent_eval_point",       "team_stadium/decide_frame_order", ["opponent_info_copy", "evaluation_point"], None),
    ("opponent_best_eval_point",  "team_stadium/decide_frame_order", ["opponent_info_copy", "user_info", "best_team_evaluation_point"], None),

    # Final result of THIS trial
    ("trial_final_score",         "team_stadium/all_race_end",     ["total_score_info", "final_total_score"], None),
    ("trial_score_bonus",         "team_stadium/all_race_end",     ["total_score_info", "all_race_result_score_bonus"], None),
    ("trial_win_type",            "team_stadium/all_race_end",     ["final_win_type"], None),
    ("trial_mvp_chara_id",        "team_stadium/all_race_end",     ["mvp_chara_id"], None),
    ("trial_high_score_updated",  "team_stadium/all_race_end",     ["is_update_high_score"], None),
    ("trial_ranking_change",      "team_stadium/all_race_end",     ["ranking_rank"], None),
    ("trial_friend_point_gain",   "team_stadium/all_race_end",     ["add_friend_point"], None),
    ("trial_love_point_gain",     "team_stadium/all_race_end",     ["add_love_point"], None),
    ("trial_circle_point",        "team_stadium/all_race_end",     ["circle_point"], None),
]


def extract_state(payloads_by_endpoint: dict[str, dict]) -> dict:
    """Run all extractors against the payloads. Returns a flat dict of values.
    Unknown / missing fields are None — the dashboard hides them automatically."""
    out: dict[str, Any] = {"ts": time.time()}
    for label, endpoint, path, transform in FIELD_EXTRACTORS:
        payload = payloads_by_endpoint.get(endpoint)
        if not payload:
            continue
        data = payload.get("data", payload) if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        value = _get_nested(data, path)
        if transform and value is not None:
            try:
                value = transform(value)
            except Exception:
                value = None
        out[label] = value
    return out


def append_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # durable append (single write + fsync) instead of a bare write that can
    # leave a truncated last line on crash.
    jsonl_util.append_jsonl(STATE_PATH, [state], json_kwargs={"default": str})


def load_state_history() -> list[dict]:
    if not STATE_PATH.exists():
        return []
    out = []
    with open(STATE_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def latest_state() -> dict | None:
    """Most recent snapshot."""
    history = load_state_history()
    return history[-1] if history else None


def summarize_changes(field: str, history: list[dict]) -> list:
    """Returns list of distinct values (deduped consecutive) for a field over time."""
    out = []
    last = object()
    for h in history:
        v = h.get(field)
        if v is None:
            continue
        if v != last:
            out.append({"ts": h.get("ts"), "value": v})
            last = v
    return out
