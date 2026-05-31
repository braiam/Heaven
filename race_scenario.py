"""Decoder for Uma Musume race_scenario binary format.

Format reverse-engineered by SSHZ-ORG/hakuraku.
race_scenario field in payloads is base64(gzip(custom_binary)).
Binary uses LE primitives + fixed-size structs.

Layout:
  Header(8) + max_length bytes
  float distance_diff_max
  int32 horse_num
  int32 horse_frame_size
  int32 horse_result_size
  int32 padding_1; bytes padding_1
  int32 frame_count
  int32 frame_size
  Frame[frame_count] (each = time(float) + horse_num * horse_frame_size)
  int32 padding_2; bytes padding_2
  HorseResult[horse_num] (each horse_result_size bytes)
  int32 padding_3; bytes padding_3
  int32 event_count
  Event[event_count] (each = int16 event_size + variable struct)

EVENT TYPES (RaceSimulateEventData.SimulateEventType):
  0 SCORE
  1 CHALLENGE_MATCH_POINT
  3 SKILL                       <- this is what we care about
  4 COMPETE_TOP
  5 COMPETE_FIGHT
  6 RELEASE_CONSERVE_POWER
  7 STAMINA_LIMIT_BREAK_BUFF
  8 COMPETE_BEFORE_SPURT
  9 STAMINA_KEEP
  10 SECURE_LEAD

For SKILL events: param[0] = horse_index, param[1] = skill_id (typically).
"""
from __future__ import annotations

import base64
import gzip
import struct
from typing import Any


EVENT_TYPE = {
    0:  "SCORE",
    1:  "CHALLENGE_MATCH_POINT",
    2:  "NOUSE_2",
    3:  "SKILL",
    4:  "COMPETE_TOP",
    5:  "COMPETE_FIGHT",
    6:  "RELEASE_CONSERVE_POWER",
    7:  "STAMINA_LIMIT_BREAK_BUFF",
    8:  "COMPETE_BEFORE_SPURT",
    9:  "STAMINA_KEEP",
    10: "SECURE_LEAD",
}

TEMPTATION_MODE = {
    0: "NULL", 1: "POSITION_SASHI", 2: "POSITION_SENKO",
    3: "POSITION_NIGE", 4: "BOOST",
}

RUNNING_STYLE = {
    0: "NONE", 1: "NIGE", 2: "SENKO", 3: "SASHI", 4: "OIKOMI",
}


def _read_header(b: bytes, offset: int) -> tuple[dict, int]:
    max_length, version = struct.unpack_from("<ii", b, offset)
    return {"max_length": max_length, "version": version}, offset + 4 + max_length


def _read_horse_frame(b: bytes, offset: int) -> dict:
    dist, lane, speed, hp, temptation, block_front = struct.unpack_from("<fHHHbb", b, offset)
    return {
        "distance":          dist,
        "lane_position":     lane,
        "speed":             speed,
        "hp":                hp,
        "temptation_mode":   TEMPTATION_MODE.get(temptation, str(temptation)),
        "block_front_horse": block_front,
    }


def _read_frame(b: bytes, offset: int, horse_num: int, horse_frame_size: int) -> tuple[dict, int]:
    time_val = struct.unpack_from("<f", b, offset)[0]
    offset += 4
    horses = []
    for _ in range(horse_num):
        horses.append(_read_horse_frame(b, offset))
        offset += horse_frame_size
    return {"time": time_val, "horses": horses}, offset


def _read_horse_result(b: bytes, offset: int, size: int) -> dict:
    finish_order, finish_time, finish_diff_time, start_delay_time, guts_order, wiz_order, last_spurt_dist, running_style, defeat, finish_time_raw = struct.unpack_from(
        "<ifffBBfBif", b, offset
    )
    return {
        "finish_order":         finish_order,
        "finish_time":          finish_time,
        "finish_diff_time":     finish_diff_time,
        "start_delay_time":     start_delay_time,
        "guts_order":           guts_order,
        "wiz_order":            wiz_order,
        "last_spurt_start_distance": last_spurt_dist,
        "running_style":        RUNNING_STYLE.get(running_style, str(running_style)),
        "defeat":               defeat,
        "finish_time_raw":      finish_time_raw,
    }


def _read_event(b: bytes, offset: int) -> dict:
    frame_time, event_type, param_count = struct.unpack_from("<fbb", b, offset)
    offset += 6
    params = []
    for _ in range(param_count):
        params.append(struct.unpack_from("<i", b, offset)[0])
        offset += 4
    return {
        "frame_time": frame_time,
        "type":       EVENT_TYPE.get(event_type, str(event_type)),
        "type_id":    event_type,
        "params":     params,
    }


def parse_race_scenario(blob_b64: str) -> dict:
    """Top-level entry: takes the raw race_scenario field, returns parsed dict."""
    raw = base64.b64decode(blob_b64)
    b = gzip.decompress(raw)

    header, offset = _read_header(b, 0)
    distance_diff_max, horse_num, horse_frame_size, horse_result_size = struct.unpack_from(
        "<fiii", b, offset
    )
    offset += 16

    pad_1 = struct.unpack_from("<i", b, offset)[0]
    offset += 4 + pad_1

    frame_count, frame_size = struct.unpack_from("<ii", b, offset)
    offset += 8

    frames = []
    for _ in range(frame_count):
        frame, offset = _read_frame(b, offset, horse_num, horse_frame_size)
        frames.append(frame)

    pad_2 = struct.unpack_from("<i", b, offset)[0]
    offset += 4 + pad_2

    horse_results = []
    for _ in range(horse_num):
        horse_results.append(_read_horse_result(b, offset, horse_result_size))
        offset += horse_result_size

    pad_3 = struct.unpack_from("<i", b, offset)[0]
    offset += 4 + pad_3

    event_count = struct.unpack_from("<i", b, offset)[0]
    offset += 4

    events = []
    for _ in range(event_count):
        event_size = struct.unpack_from("<h", b, offset)[0]
        offset += 2
        events.append(_read_event(b, offset))
        offset += event_size

    return {
        "header":             header,
        "distance_diff_max":  distance_diff_max,
        "horse_num":          horse_num,
        "horse_frame_size":   horse_frame_size,
        "horse_result_size":  horse_result_size,
        "frame_count":        frame_count,
        "frame_size":         frame_size,
        "frames":             frames,
        "horse_results":      horse_results,
        "event_count":        event_count,
        "events":             events,
    }


def extract_skill_activations(parsed: dict) -> list[dict]:
    """Filter events to just SKILL type (3). Each entry: {frame_time, horse_index, skill_id}."""
    out = []
    for ev in parsed.get("events") or []:
        if ev.get("type") != "SKILL":
            continue
        params = ev.get("params") or []
        if len(params) >= 2:
            out.append({
                "frame_time":  ev["frame_time"],
                "horse_index": params[0],
                "skill_id":    params[1],
                "raw_params":  params,
            })
    return out


def skill_activations_per_horse(parsed: dict) -> dict:
    """Returns {horse_index: [skill_id, ...]} for SKILL events."""
    out: dict = {}
    for act in extract_skill_activations(parsed):
        out.setdefault(act["horse_index"], []).append(act["skill_id"])
    return out


def scores_per_horse(parsed: dict) -> dict:
    """Returns {horse_index: total_score} summed from SCORE events.
    These are the raw scoring contributions before client-side display bonuses."""
    out: dict = {}
    for ev in parsed.get("events") or []:
        if ev.get("type") != "SCORE":
            continue
        params = ev.get("params") or []
        if len(params) >= 3:
            hidx = params[0]
            out[hidx] = out.get(hidx, 0) + params[2]
    return out


def score_events_per_horse(parsed: dict) -> dict:
    """Returns {horse_index: [(frame_time, event_id, points), ...]} for SCORE events."""
    out: dict = {}
    for ev in parsed.get("events") or []:
        if ev.get("type") != "SCORE":
            continue
        params = ev.get("params") or []
        if len(params) >= 3:
            hidx = params[0]
            out.setdefault(hidx, []).append((ev["frame_time"], params[1], params[2]))
    return out
