"""Team Trials race_scenario decoder — our own format RE.

The `race_scenario` field in a Team Trials result is `base64(gzip(<binary>))`.
The binary layout below was reverse-engineered from raw captured blobs, with every
field verified against known values from the result object (finish order/time,
owned skill ids, per-uma scores) across multiple races. No external spec is used.

Binary layout (all little-endian):

  HEADER
    +0x00  u32   header tag        (observed 4)
    +0x04  u32   magic/version     (observed 0x05F5E102)
    +0x08  f32   distance_diff_max
    +0x0c  i32   horse_num         (e.g. 12)
    +0x10  i32   horse_frame_size  (bytes per horse per frame, e.g. 12)
    +0x14  i32   horse_result_size (bytes per horse result, e.g. 31)
    +0x18  i32   pad_1             (skip pad_1 extra bytes)
    +..    i32   frame_count
    +..    i32   frame_size        (== 4 + horse_num*horse_frame_size)

  FRAMES        frame_count * frame_size bytes        (per-tick playback; skipped —
                we only need results + events)
    i32   pad_2 (skip pad_2 extra bytes)

  HORSE_RESULTS horse_num entries, horse_result_size bytes each
    +0x00  i32   finish_order   (0-indexed)
    +0x04  f32   finish_time    (seconds)
    +0x08  f32   finish_diff_time_from_prev
    +0x16  u8    running_style  (1 nige / 2 senko / 3 sashi / 4 oikomi)
    (other bytes: misc, not needed here)
    i32   pad_3 (skip pad_3 extra bytes)

  EVENTS
    i32   event_count
    then for each event:
      i16  event_size
      body (event_size bytes):
        +0x00  f32  frame_time
        +0x04  u8   type        (0 = SCORE, 3 = SKILL, others: positional/misc)
        +0x05  u8   (param marker)
        +0x06  i32  horse_index
        +0x0a  i32  param1      (SKILL: skill_id)
        +0x0e  i32  param2      (SCORE: points)

Public API mirrors what the importer needs:
  parse(blob_b64)               -> dict with horse_results + events
  activations_per_horse(parsed) -> {horse_index: [skill_id, ...]}
  scores_per_horse(parsed)      -> {horse_index: total_points}
"""
from __future__ import annotations

import base64
import gzip
import struct

EVENT_SCORE = 0
EVENT_SKILL = 3

# running_style code (game) -> name. Verified against the result object's
# per-uma running_style across all horses.
_RUNNING_STYLE = {1: "NIGE", 2: "SENKO", 3: "SASHI", 4: "OIKOMI"}


def parse(blob_b64: str) -> dict:
    """Decode a race_scenario blob into horse results + events."""
    data = gzip.decompress(base64.b64decode(blob_b64))
    o = 8  # skip header tag + magic

    distance_diff_max, horse_num, horse_frame_size, horse_result_size = struct.unpack_from(
        "<fiii", data, o
    )
    o += 16

    pad_1 = struct.unpack_from("<i", data, o)[0]
    o += 4 + pad_1

    frame_count, frame_size = struct.unpack_from("<ii", data, o)
    o += 8
    o += frame_count * frame_size  # frames are playback data; we don't need them

    pad_2 = struct.unpack_from("<i", data, o)[0]
    o += 4 + pad_2

    horse_results = []
    for _ in range(horse_num):
        finish_order = struct.unpack_from("<i", data, o)[0]
        finish_time = struct.unpack_from("<f", data, o + 4)[0]
        finish_diff = struct.unpack_from("<f", data, o + 8)[0]
        rs_code = data[o + 0x16] if o + 0x16 < len(data) else 0
        horse_results.append({
            "finish_order": finish_order,
            "finish_time": finish_time,
            "finish_diff_time_from_prev": finish_diff,
            "running_style": _RUNNING_STYLE.get(rs_code, rs_code),
        })
        o += horse_result_size

    pad_3 = struct.unpack_from("<i", data, o)[0]
    o += 4 + pad_3

    event_count = struct.unpack_from("<i", data, o)[0]
    o += 4

    events = []
    for _ in range(event_count):
        size = struct.unpack_from("<h", data, o)[0]
        o += 2
        body = data[o:o + size]
        o += size
        if len(body) < 18:
            continue
        events.append({
            "frame_time": struct.unpack_from("<f", body, 0)[0],
            "type": body[4],
            "horse_index": struct.unpack_from("<i", body, 6)[0],
            "param1": struct.unpack_from("<i", body, 10)[0],
            "param2": struct.unpack_from("<i", body, 14)[0],
        })

    return {
        "distance_diff_max": distance_diff_max,
        "horse_num": horse_num,
        "horse_results": horse_results,
        "event_count": event_count,
        "events": events,
    }


def activations_per_horse(parsed: dict) -> dict:
    """{horse_index: [skill_id, ...]} for SKILL events, in activation order."""
    out: dict = {}
    for ev in parsed.get("events") or []:
        if ev.get("type") == EVENT_SKILL:
            out.setdefault(ev["horse_index"], []).append(ev["param1"])
    return out


def scores_per_horse(parsed: dict) -> dict:
    """{horse_index: total_points} summed from SCORE events (raw, pre-bonus)."""
    out: dict = {}
    for ev in parsed.get("events") or []:
        if ev.get("type") == EVENT_SCORE:
            h = ev["horse_index"]
            out[h] = out.get(h, 0) + ev["param2"]
    return out
