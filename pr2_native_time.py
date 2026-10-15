"""Build Resolve 21 per-clip media time maps and retimed source offsets.

The field layout follows measured Resolve exports and the independent decoder
at https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/resolve-advanced/vendor/drp-format/README.md.
This module is an independent stdlib implementation; it supports identity maps
and forward constant-speed video retimes only.
"""

from __future__ import annotations

import math
import struct
import uuid


_KEYED_DICT_DOUBLE = 0x06
_KEYED_DICT_STRING = 0x0A
_KEYED_DICT_BYTES = 0x0C


def _positive_finite(value: float, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return number


def _keyed_entry(key: str, value_type: int, value: str | bytes | float) -> bytes:
    key_bytes = key.encode("utf-16-be")
    result = bytearray(struct.pack(">I", len(key_bytes)))
    result.extend(key_bytes)
    result.extend(struct.pack(">IB", value_type, 0))
    if value_type == _KEYED_DICT_DOUBLE:
        result.extend(struct.pack(">d", float(value)))
    elif value_type == _KEYED_DICT_STRING:
        payload = str(value).encode("utf-16-be")
        result.extend(struct.pack(">I", len(payload)))
        result.extend(payload)
    elif value_type == _KEYED_DICT_BYTES:
        payload = bytes(value)
        result.extend(struct.pack(">I", len(payload)))
        result.extend(payload)
    else:
        raise ValueError(f"unsupported keyed-dict value type: {value_type}")
    return bytes(result)


def _encode_keyed_dict(entries: list[tuple[str, int, str | bytes | float]]) -> bytes:
    return struct.pack(">II", 1, len(entries)) + b"".join(
        _keyed_entry(key, value_type, value)
        for key, value_type, value in entries
    )


def _encode_constant_keyframes(record_seconds: float, source_seconds: float) -> bytes:
    point = (
        b"\x09" + struct.pack("<d", record_seconds)
        + b"\x11" + struct.pack("<d", source_seconds)
    )
    return b"\x80\x0a\x00\x0a\x12" + point


def _identity_timemap(end_seconds: float, *, audio: bool) -> str:
    if audio:
        payload = struct.pack(">d", end_seconds)
    else:
        payload = struct.pack(">5d", end_seconds, 0.0, end_seconds, 0.0, end_seconds)
    return (b"\x02" + payload).hex()


def build_media_timemap(
    source_frames: int,
    source_fps: float,
    speed_percent: float,
    *,
    audio: bool = False,
) -> str:
    """Return the MediaTimemapBA hex for an identity or forward video retime.

    Clip extents use the last source-frame index, ``(source_frames - 1) / fps``.
    Audio retiming is rejected because Resolve may accept its time map while
    rendering audio at normal speed.
    """
    if isinstance(source_frames, bool) or not isinstance(source_frames, int) or source_frames < 1:
        raise ValueError("source_frames must be a positive integer")
    fps = _positive_finite(source_fps, "source_fps")
    speed = _positive_finite(speed_percent, "speed_percent") / 100.0
    end_seconds = (source_frames - 1) / fps

    if speed_percent == 100:
        return _identity_timemap(end_seconds, audio=audio)
    if audio:
        raise ValueError("audio retiming is unsupported")
    if source_frames < 2:
        raise ValueError("retimed media requires at least two source frames")

    record_end = end_seconds / speed
    keyframes = _encode_constant_keyframes(record_end, end_seconds)
    entries: list[tuple[str, int, str | bytes | float]] = [
        ("YMin", _KEYED_DICT_DOUBLE, -1.0),
        ("YMax", _KEYED_DICT_DOUBLE, -1.0),
        ("XMax", _KEYED_DICT_DOUBLE, record_end),
        ("UniqueId", _KEYED_DICT_STRING, str(uuid.uuid4())),
        ("LastValidYOffset", _KEYED_DICT_DOUBLE, end_seconds),
        ("KeyframesBA", _KEYED_DICT_BYTES, keyframes),
        ("DbType", _KEYED_DICT_STRING, "Sm2TimeMap"),
    ]
    return _encode_keyed_dict(entries).hex()


def retimed_source_in(
    source_in: int,
    source_fps: float,
    timeline_fps: float,
    speed_percent: float,
) -> str:
    """Encode a source in-point as a Resolve record-domain frame position."""
    if isinstance(source_in, bool) or not isinstance(source_in, int) or source_in < 0:
        raise ValueError("source_in must be a non-negative integer")
    source_rate = _positive_finite(source_fps, "source_fps")
    timeline_rate = _positive_finite(timeline_fps, "timeline_fps")
    speed = _positive_finite(speed_percent, "speed_percent") / 100.0
    record_frames = source_in / source_rate / speed * timeline_rate
    whole = math.floor(record_frames)
    fraction = record_frames - whole
    if fraction == 0:
        return str(whole)
    return f"{whole}|{struct.pack('<d', fraction).hex()}"


__all__ = ["build_media_timemap", "retimed_source_in"]
