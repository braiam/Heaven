"""mitmproxy addon that captures Uma Musume Team Trials network traffic.

Two log files are written to data/:
  - raw_capture.jsonl  : every umamusume endpoint, trimmed payload (for browsing)
  - raw_full.jsonl     : full untrimmed payloads for high-signal endpoints
                         (team_stadium/*, champions/*, race endpoints)

The udid (required to AES-decrypt payloads) is auto-detected on first capture:
  - Scans every umamusume request's headers for a 32-hex-char string
  - Tries each candidate by attempting to decrypt the first response
  - On success, persists to data/udid.txt so it's reused next session

If auto-detection fails, place your udid manually in data/udid.txt
(one line, 32 hex chars).

Run with:
    mitmdump -s discover_addon.py --listen-port 8080
"""
from __future__ import annotations

import base64
import json
import logging
import re
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from decoder import decode_response_body, load_udid, save_udid, decode_raw


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
LOG_PATH = DATA_DIR / "raw_capture.jsonl"
FULL_LOG_PATH = DATA_DIR / "raw_full.jsonl"

GAME_PATH_MARKER = "/umamusume/"

# Endpoints whose payload we want FULL (no trim) — high-signal data
FULL_PAYLOAD_PREFIXES = (
    "team_stadium/",
    "champion_race/",
    "champions/",
    "single_mode_free/race_",
)

_HEX32 = re.compile(r"\b[0-9a-fA-F]{32}\b")

_udid: str | None = None
_udid_candidates: list[str] = []
_stats = {"flows": 0, "decoded": 0, "logged": 0, "errors": 0, "udid_tries": 0}
_seen_endpoints: set = set()
_consec_fail = 0   # consecutive decode failures with the current _udid


# ── Auto-detection of udid from request headers/URL ─────────────────────────
def _scan_request_for_udid(flow) -> list[str]:
    """Returns 32-char hex candidates found anywhere in the request headers/URL."""
    candidates: list[str] = []
    for header_value in flow.request.headers.values():
        if header_value:
            candidates.extend(_HEX32.findall(header_value))
    candidates.extend(_HEX32.findall(flow.request.path))
    seen = set()
    out: list[str] = []
    for c in candidates:
        lc = c.lower()
        if lc not in seen:
            seen.add(lc)
            out.append(lc)
    return out


def _extract_udids_from_request_body(content: bytes) -> list[str]:
    """The device udid is embedded in the request's binary header, 16 bytes
    located a fixed offset before the end of the first blob (96 bytes when an
    auth_key is present, 48 on signup with none). This is far more reliable
    than scanning text headers (the udid never appears there in plain text).
    Returns candidate udid hex strings (32 chars)."""
    out: list[str] = []
    raws: list[bytes] = []
    try:
        ascii_b64 = content.decode("ascii", errors="strict").strip()
        raws.append(base64.b64decode(ascii_b64))
    except Exception:
        pass
    raws.append(content)
    for raw in raws:
        if len(raw) < 4:
            continue
        try:
            hlen = struct.unpack("<I", raw[:4])[0]
        except Exception:
            continue
        blob1_end = 4 + hlen
        if hlen < 64 or blob1_end > len(raw):
            continue
        for off in (96, 48):
            start = blob1_end - off
            if start < 0 or start + 16 > len(raw):
                continue
            udid_hex = raw[start:start + 16].hex()
            if len(udid_hex) == 32 and udid_hex not in out:
                out.append(udid_hex)
    return out


def _try_decrypt_with(body: bytes, candidate_udid: str) -> bool:
    """Returns True if decryption with this udid produces valid msgpack."""
    try:
        import base64
        try:
            ascii_b64 = body.decode("ascii", errors="strict").strip()
            raw = base64.b64decode(ascii_b64)
            decode_raw(raw, candidate_udid)
        except Exception:
            decode_raw(body, candidate_udid)
        return True
    except Exception:
        return False


# ── Helpers ─────────────────────────────────────────────────────────────────
def _endpoint_from_path(path: str) -> str | None:
    idx = path.find(GAME_PATH_MARKER)
    if idx < 0:
        return None
    ep = path[idx + len(GAME_PATH_MARKER):]
    q = ep.find("?")
    return ep[:q] if q >= 0 else ep


def _trim(v, depth=0):
    """Returns a smallish representation of the payload — keeps structure, trims big lists."""
    if depth > 8:
        return "<depth>"
    if isinstance(v, dict):
        return {str(k): _trim(val, depth + 1) for k, val in v.items()}
    if isinstance(v, list):
        if not v:
            return []
        head = [_trim(item, depth + 1) for item in v[:2]]
        if len(v) > 2:
            head.append(f"<+{len(v) - 2} more items>")
        return head
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    s = str(v)
    return s if len(s) < 200 else s[:197] + "..."


# ── mitmproxy hooks ─────────────────────────────────────────────────────────
def load(loader):
    global _udid
    _udid = load_udid()
    if _udid:
        logging.info(f"[capture] udid loaded ({_udid[:8]}...) from disk")
    else:
        logging.error("=" * 60)
        logging.error("[capture] NO UDID CONFIGURED")
        logging.error("=" * 60)
        logging.error("Without a udid the addon cannot decrypt the game's responses.")
        logging.error("Auto-detection from request headers is best-effort and usually fails.")
        logging.error("")
        logging.error("Put your udid in data/udid.txt (one line, 32 hex chars).")
        logging.error("See README section 'About the udid' for how to get it.")
        logging.error("=" * 60)
    logging.info(f"[capture] logs -> {LOG_PATH}  +  {FULL_LOG_PATH}")


def request(flow):
    """Collect udid candidates from every umamusume request. We do this even
    when a udid is already loaded, so that if the loaded one turns out to be
    stale (e.g. the device udid changed after a reroll) we can recover by
    re-detecting from live traffic."""
    ep = _endpoint_from_path(flow.request.path)
    if ep is None:
        return
    # Primary: udid embedded in the request body (reliable).
    if flow.request.content:
        for c in _extract_udids_from_request_body(flow.request.content):
            if c not in _udid_candidates:
                _udid_candidates.append(c)
    # Fallback: any 32-hex token in headers/URL (best-effort).
    for c in _scan_request_for_udid(flow):
        if c not in _udid_candidates:
            _udid_candidates.append(c)


def response(flow):
    _stats["flows"] += 1
    ep = _endpoint_from_path(flow.request.path)
    if ep is None:
        return

    global _udid, _consec_fail
    body = flow.response.content
    if not body:
        return
    size = len(body)

    decoded = None
    # 1) Try the udid we already have.
    if _udid:
        try:
            decoded = decode_response_body(body, _udid)
            _consec_fail = 0
        except Exception:
            decoded = None
            _consec_fail += 1
            # A correct udid never fails on valid responses. Repeated failures
            # mean the loaded udid is stale (device udid changed) — drop it and
            # fall through to re-detection from the request-body candidates.
            if _consec_fail >= 3:
                logging.warning(
                    f"[capture] loaded udid ({_udid[:8]}...) failed {_consec_fail}x "
                    "— discarding and re-detecting from request bodies")
                _udid = None
                _consec_fail = 0

    # 2) No (valid) udid: try each candidate harvested from request bodies.
    if decoded is None and not _udid and _udid_candidates:
        for cand in _udid_candidates:
            _stats["udid_tries"] += 1
            if _try_decrypt_with(body, cand):
                _udid = cand
                save_udid(_udid)
                logging.info(f"[capture] udid detected ({_udid[:8]}...) and saved to data/udid.txt")
                try:
                    decoded = decode_response_body(body, _udid)
                except Exception:
                    decoded = None
                break

    if decoded is None:
        _stats["errors"] += 1
        return
    _stats["decoded"] += 1

    is_new_ep = ep not in _seen_endpoints
    _seen_endpoints.add(ep)
    top_keys = list(decoded.get("data", decoded).keys()) if isinstance(decoded, dict) else []

    record = {
        "ts":       time.time(),
        "endpoint": ep,
        "is_new":   is_new_ep,
        "size":     size,
        "top_keys": top_keys[:30],
        "sample":   _trim(decoded),
    }
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        _stats["logged"] += 1
        if is_new_ep:
            logging.info(f"[capture] NEW endpoint: {ep}  ({size} bytes)")
    except Exception:
        _stats["errors"] += 1

    # Full payload dump for high-signal endpoints (no trim)
    if any(ep.startswith(p) for p in FULL_PAYLOAD_PREFIXES):
        try:
            full_record = {
                "ts":       time.time(),
                "endpoint": ep,
                "size":     size,
                "payload":  decoded,
            }
            with open(FULL_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(full_record, ensure_ascii=False, default=str) + "\n")
            logging.info(f"[capture] FULL dump: {ep}  ({size} bytes)")
        except Exception as e:
            logging.warning(f"[capture] full dump failed: {e}")


def done():
    logging.info(f"[capture] shutdown stats: {_stats}")
    logging.info(f"[capture] unique endpoints seen: {len(_seen_endpoints)}")


import threading

def _stats_loop():
    while True:
        time.sleep(30)
        if _stats["flows"] > 0:
            logging.info(f"[capture] {_stats}  unique_eps={len(_seen_endpoints)}")


threading.Thread(target=_stats_loop, daemon=True).start()
