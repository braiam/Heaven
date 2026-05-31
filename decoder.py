"""Minimal Uma Musume payload decoder.

Wire format:
    base64( AES_CBC(msgpack_len[4] + msgpack_payload + padding, key, iv=udid[:16]) + key[32] )

The udid is the device identifier the game generates locally on first launch.
It is needed as the IV for AES-CBC decryption.

The decoder looks for the udid in (first match wins):
  1. data/udid.txt          (plain text, one line, 32 hex chars)
  2. data/auth_config.json  (JSON with a "udid" field)
  3. udid.txt at the project root

If none are found load_udid() returns None and the addon will skip decoding
until it can auto-detect the udid from a captured request header.
"""
from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

import msgpack
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


_BASE = Path(__file__).parent
_UDID_PATH_CANDIDATES = [
    _BASE / "data" / "udid.txt",
    _BASE / "data" / "auth_config.json",
    _BASE / "udid.txt",
]


def load_udid() -> str | None:
    """Reads the udid from one of the candidate paths. Returns None if not found."""
    for p in _UDID_PATH_CANDIDATES:
        if not p.exists():
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
            if not text:
                continue
            # JSON file?
            if text.startswith("{"):
                try:
                    return json.loads(text).get("udid")
                except Exception:
                    continue
            return text.splitlines()[0].strip()
        except Exception:
            continue
    return None


def save_udid(udid: str) -> None:
    """Persists the udid to data/udid.txt so future runs don't need to detect it again."""
    target = _BASE / "data" / "udid.txt"
    target.parent.mkdir(exist_ok=True)
    target.write_text(udid.strip(), encoding="utf-8")


def _iv_from_udid(udid: str) -> bytes:
    """First 16 hex chars of udid (no dashes), lowercase."""
    return udid.replace("-", "").lower()[:16].encode()


def decode_raw(raw: bytes, udid: str) -> Any:
    if not raw or len(raw) < 36:
        raise ValueError(f"body too short: {len(raw) if raw else 0} bytes")
    key = raw[-32:]
    cipher = raw[:-32]
    if len(cipher) % 16 != 0:
        raise ValueError(f"cipher length {len(cipher)} not multiple of 16")
    plain_padded = AES.new(key, AES.MODE_CBC, _iv_from_udid(udid)).decrypt(cipher)
    plain = unpad(plain_padded, 16)
    if len(plain) < 4:
        raise ValueError("decrypted payload too short")
    msgpack_len = struct.unpack("<I", plain[:4])[0]
    if msgpack_len <= 0 or 4 + msgpack_len > len(plain):
        raise ValueError(f"msgpack length {msgpack_len} out of range")
    payload = plain[4 : 4 + msgpack_len]
    return msgpack.unpackb(payload, raw=False, strict_map_key=False)


def decode_response_body(body: bytes, udid: str) -> Any:
    """Decode an HTTP response body (server -> client)."""
    try:
        ascii_b64 = body.decode("ascii", errors="strict").strip()
        raw = base64.b64decode(ascii_b64)
        return decode_raw(raw, udid)
    except Exception:
        return decode_raw(body, udid)
