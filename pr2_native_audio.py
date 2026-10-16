"""Encode measured Resolve 21 audio channel maps and Fairlight track records."""

from __future__ import annotations

import re
import struct
import zlib


_FIXED_MODEL_COUNTS = {0x18, 0x1C, 0x2F6}
_MONO_DEFAULTS = {
    (0x4, 0): 0, (0x5, 1): 0, (0xB4, 0): 0, (0xB8, 0): 0,
    (0xB9, 0): 0, (0xBA, 0): 1, (0xF2, 0): 119,
    (0x334, 0): 85, (0x335, 0): 137, (0x4B5, 0): 0, (0x50D, 0): 5000,
}
_FieldMap = dict[str, tuple[int, bytes]]
_ModelRecord = tuple[int, int, list[int] | list[str]]
_TrackSpec = tuple[str, str, bool, bool]


def decode_fields(data: bytes) -> _FieldMap:
    if len(data) < 8 or struct.unpack_from(">I", data)[0] != 1:
        raise ValueError("unsupported audio FieldsBlob version")
    count = struct.unpack_from(">I", data, 4)[0]
    fields: _FieldMap = {}
    offset = 8
    for _ in range(count):
        if offset + 4 > len(data):
            raise ValueError("truncated audio field key")
        length = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if length % 2 or offset + length + 5 > len(data):
            raise ValueError("invalid audio field key bounds")
        key = data[offset:offset + length].decode("utf-16be")
        offset += length
        kind, null = struct.unpack_from(">IB", data, offset)
        offset += 5
        if null or key in fields:
            raise ValueError("unsupported null or duplicate audio field")
        sizes = {1: 1, 2: 4, 3: 4, 4: 8, 6: 8}
        if kind in (10, 12):
            if offset + 4 > len(data):
                raise ValueError("truncated audio field length")
            size = struct.unpack_from(">I", data, offset)[0]
            offset += 4
        elif kind in sizes:
            size = sizes[kind]
        else:
            raise ValueError(f"unsupported audio field type {kind}")
        if offset + size > len(data):
            raise ValueError("invalid audio field value bounds")
        fields[key] = kind, data[offset:offset + size]
        offset += size
    if offset != len(data):
        raise ValueError("audio FieldsBlob has trailing bytes")
    return fields


def encode_fields(fields: _FieldMap) -> bytes:
    parts = [struct.pack(">II", 1, len(fields))]
    for key, (kind, value) in fields.items():
        encoded = key.encode("utf-16be")
        parts.append(struct.pack(">I", len(encoded)) + encoded + struct.pack(">IB", kind, 0))
        if kind in (10, 12):
            parts.append(struct.pack(">I", len(value)))
        parts.append(value)
    return b"".join(parts)


def mono_channel_mapping(source_channel: int) -> str:
    """Encode a one-based FCP source channel in Resolve's version-2 map."""
    if not 1 <= source_channel <= 0x3FFF:
        raise ValueError("audio source channel is outside the encodable range")
    return encode_fields({
        "ChannelsBA": (12, struct.pack(">III", 2, 1, 0x4000 + source_channel)),
        "AudioType": (2, struct.pack(">I", 1)),
    }).hex()


def _model_records(data: bytes) -> tuple[int, int, list[_ModelRecord]]:
    section = data.find(b"LastClient") - 16
    if (section < 0 or data[section:section + 4] != bytes.fromhex("66886677")
            or data[section + 52:section + 56] != bytes.fromhex("44332211")
            or struct.unpack_from("<I", data, section + 68)[0] != 1):
        raise ValueError("unsupported Fairlight model section or main bus count")
    start = data.find(struct.pack("<III", 1, 1, 2), section + 100)
    if start < 0:
        raise ValueError("Fairlight template has no stereo channel records")
    offset, rows = start, []
    while offset + 12 <= len(data):
        flag, field, count = struct.unpack_from("<III", data, offset)
        if flag != 1 or not 0 < field < 0x2000 or count > 8192:
            break
        pos = offset + 12
        if field == 0xBC:
            names = []
            for _ in range(count):
                if pos + 4 > len(data):
                    raise ValueError("truncated Fairlight channel name")
                length = struct.unpack_from("<I", data, pos)[0]
                if pos + 4 + length > len(data):
                    raise ValueError("invalid Fairlight channel name bounds")
                names.append(data[pos + 4:pos + 4 + length].decode("utf-8"))
                pos += 4 + length
            values: list[int] | list[str] = names
        else:
            if pos + count * 4 > len(data):
                raise ValueError("invalid Fairlight channel value bounds")
            values = list(struct.unpack_from("<" + str(count) + "I", data, pos))
            pos += count * 4
        rows.append((field, count, values))
        offset = pos
    if (len(rows) != 494 or sum(field == 0xBC for field, _, _ in rows) != 1
            or any(count != (48 if field in _FIXED_MODEL_COUNTS else 2) for field, count, _ in rows)):
        raise ValueError("unsupported Fairlight template property layout")
    return start, offset, rows


def _rebuild_model(data: bytes, tracks: list[_TrackSpec]) -> bytes:
    start, end, rows = _model_records(data)
    channels = sum(1 if kind == "mono" else 2 for kind, _, _, _ in tracks)
    parts: list[bytes] = []
    occurrences: dict[int, int] = {}
    for field, count, values in rows:
        occurrence = occurrences.get(field, 0)
        occurrences[field] = occurrence + 1
        if field in _FIXED_MODEL_COUNTS:
            parts.append(struct.pack("<III", 1, field, count) + struct.pack("<" + str(count) + "I", *values))
            continue
        parts.append(struct.pack("<III", 1, field, channels))
        for track_index, (kind, name, enabled, locked) in enumerate(tracks):
            width = 1 if kind == "mono" else 2
            if field == 0xBC:
                encoded = name.encode("utf-8")
                parts.extend([struct.pack("<I", len(encoded)) + encoded] * width)
                continue
            for channel in range(width):
                value = int(values[channel])
                if kind == "mono":
                    value = _MONO_DEFAULTS.get((field, occurrence), value)
                    if 0x153 <= field <= 0x16A:
                        value = 0
                if field == 0x458:
                    value = track_index if channel == 0 else 0xFFFFFFFF
                elif field == 0x2:
                    value = int(not enabled)
                elif field == 0xDB:
                    value = int(locked)
                parts.append(struct.pack("<I", value))
    suffix = bytearray(data[end:])
    # Change only the strip links/counts measured across channel layouts;
    # remaining model properties stay byte-exact.
    for field, expected, updates in (
        (0x458, [2, 1], [len(tracks) + 1, len(tracks)]),
        (0x324, [2], [max(2, len(tracks))]),
        (0x32F, [1], [len(tracks)]),
    ):
        pattern = struct.pack("<III", 1, field, 1)
        offsets = [match.start() + 12 for match in re.finditer(re.escape(pattern), suffix)]
        actual = [struct.unpack_from("<I", suffix, offset)[0] for offset in offsets]
        if actual != expected:
            raise ValueError(f"unsupported Fairlight strip-reference layout {field:#x}")
        for offset, value in zip(offsets, updates):
            struct.pack_into("<I", suffix, offset, value)
    result = bytearray(data[:start] + b"".join(parts) + suffix)
    struct.pack_into("<I", result, 8, len(result) - 12)
    return bytes(result)


def rebuild_track_model(fields_blob: bytes, tracks: list[_TrackSpec]) -> bytes:
    """Rebuild channel strips in the measured one-bus Resolve 21 template."""
    if not tracks:
        return fields_blob
    if any(kind not in ("mono", "stereo") for kind, _, _, _ in tracks):
        raise ValueError("Fairlight track format has no verified encoder")
    fields = decode_fields(fields_blob)
    kind, wrapped = fields["FLStudioModelBA"]
    if kind != 12 or len(wrapped) < 4:
        raise ValueError("invalid Fairlight model envelope")
    payload = zlib.decompress(wrapped[4:])
    if struct.unpack_from(">I", wrapped)[0] != len(payload):
        raise ValueError("Fairlight model size does not match its envelope")
    generated = _rebuild_model(payload, tracks)
    fields["FLStudioModelBA"] = 12, struct.pack(">I", len(generated)) + zlib.compress(generated)
    return encode_fields(fields)
