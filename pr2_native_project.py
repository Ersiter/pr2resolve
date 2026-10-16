"""Patch textual sequence settings and handles in Resolve project XML."""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET


_TIMELINE_BLOCK = re.compile(r"(?is)<Sm2MpTimelineClip\b.*?</Sm2MpTimelineClip>")
_TIMELINE_HANDLES = re.compile(r"(?is)(<TimelineHandleVec\b[^>]*>)(.*?)(</TimelineHandleVec>)")


def _parse_timeline_block(xml: str) -> ET.Element:
    return ET.fromstring(xml.replace("ListMgt::", "ListMgt__"))


def _serialize_timeline_block(node: ET.Element) -> str:
    return ET.tostring(node, encoding="unicode").replace("ListMgt__", "ListMgt::")


def _timeline_sequence(node: ET.Element) -> ET.Element:
    sequence = node.find("./TimelineSharedHandle/Sm2Timeline/Sequence/Sm2Sequence")
    if sequence is None:
        raise ValueError("timeline clip has no nested Sm2Sequence")
    return sequence


def _set_text(node: ET.Element, name: str, value: str) -> None:
    field = node.find(name)
    if field is None:
        raise ValueError(f"Sm2Sequence has no {name} field")
    field.text = value


def _source_rate(source: ET.Element) -> float | None:
    try:
        rate = float(source.findtext("rate/timebase") or "")
    except ValueError:
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    if (source.findtext("rate/ntsc") or "").strip().upper() == "TRUE":
        rate *= 1000 / 1001
    return rate


def _source_dimensions(source: ET.Element) -> tuple[int, int] | None:
    sample = source.find("media/video/format/samplecharacteristics")
    if sample is None:
        return None
    try:
        width, height = int(sample.findtext("width") or ""), int(sample.findtext("height") or "")
    except ValueError:
        return None
    return (width, height) if width > 0 and height > 0 else None


def sequence_project_settings(source: ET.Element) -> dict[str, str]:
    """Read project defaults shared by native and API exports."""
    settings = {}
    rate = _source_rate(source)
    if rate is not None:
        settings["timelineFrameRate"] = format(rate, ".3f").rstrip("0").rstrip(".")
    dimensions = _source_dimensions(source)
    if dimensions is not None:
        settings["timelineResolutionWidth"] = str(dimensions[0])
        settings["timelineResolutionHeight"] = str(dimensions[1])
    return settings


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("truncated native project setting")
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ValueError("oversized native project setting")


def _varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _patch_setup_defaults(data: bytes, updates: dict[int, int]) -> bytes:
    output = bytearray()
    offset = 0
    seen = set()
    while offset < len(data):
        start = offset
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        value_start = offset
        if wire == 0:
            _, offset = _read_varint(data, offset)
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            offset += length
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        else:
            raise ValueError("unsupported native project setting wire type")
        if not number or offset > len(data):
            raise ValueError("invalid native project setting bounds")
        if number in updates:
            if wire != 0 or number in seen:
                raise ValueError("ambiguous native project default setting")
            output.extend(data[start:value_start] + _varint(updates[number]))
            seen.add(number)
        else:
            output.extend(data[start:offset])
    for number, value in updates.items():
        if number not in seen:
            output.extend(_varint(number << 3) + _varint(value))
    return bytes(output)


def update_project_settings(project_xml: bytes, source: ET.Element) -> bytes:
    """Patch measured Resolve 21 SetupBA defaults without replacing color settings."""
    settings = sequence_project_settings(source)
    field_ids = {"timelineResolutionWidth": 2, "timelineResolutionHeight": 3, "timelineFrameRate": 15}
    updates = {field_ids[key]: int(float(value)) * 2 for key, value in settings.items()}
    if not updates:
        return project_xml
    match = re.search(rb"(?s)(<SM_Config\b.*?<FieldsBlob>)([0-9a-fA-F]+)(</FieldsBlob>)", project_xml)
    if match is None:
        raise ValueError("native project has no SM_Config FieldsBlob")
    fields = bytes.fromhex(match[2].decode("ascii"))
    key = "SetupBA".encode("utf-16be")
    first_field = struct.pack(">I", len(key)) + key + struct.pack(">IB", 12, 0)
    if (len(fields) < 8 + len(first_field) + 13
            or struct.unpack_from(">I", fields)[0] != 1
            or struct.unpack_from(">I", fields, 4)[0] < 1
            or fields[8:8 + len(first_field)] != first_field):
        raise ValueError("native setup resource has an unexpected FieldsBlob layout")
    prefix = fields[:8 + len(first_field)]
    length = struct.unpack_from(">I", fields, len(prefix))[0]
    value_end = len(prefix) + 4 + length
    wrapped = fields[len(prefix) + 4:value_end]
    if (length < 9 or value_end > len(fields) or wrapped[8] != 0x80
            or struct.unpack_from(">II", wrapped) != (1, len(wrapped) - 8)):
        raise ValueError("native setup resource requires a version-1 raw envelope")
    payload = _patch_setup_defaults(wrapped[9:], updates)
    wrapped = struct.pack(">II", 1, len(payload) + 1) + b"\x80" + payload
    fields = prefix + struct.pack(">I", len(wrapped)) + wrapped + fields[value_end:]
    return project_xml[:match.start(2)] + fields.hex().encode("ascii") + project_xml[match.end(2):]


def update_sequence_settings(pool_xml: str, source: ET.Element) -> str:
    """Copy known FCP sequence rate, geometry, and extent into its native timeline item."""
    matches = list(_TIMELINE_BLOCK.finditer(pool_xml))
    if len(matches) != 1:
        raise ValueError("expected exactly one Sm2MpTimelineClip in the source pool")

    match = matches[0]
    timeline = _parse_timeline_block(match.group())
    sequence = _timeline_sequence(timeline)

    rate = _source_rate(source)
    if rate is not None:
        _set_text(sequence, "FrameRate", (struct.pack("<d", rate) + b"\0" * 8).hex())

    dimensions = _source_dimensions(source)
    if dimensions is not None:
        width, height = dimensions
        _set_text(sequence, "Resolution", width.to_bytes(8, "big").hex() + height.to_bytes(8, "big").hex())

    try:
        duration = int(source.findtext("duration") or "")
    except ValueError:
        duration = -1
    if rate is not None and duration >= 0:
        _set_text(sequence, "MediaExtents", struct.pack("<dd", 0.0, duration / rate).hex())

    return pool_xml[:match.start()] + _serialize_timeline_block(timeline) + pool_xml[match.end():]


def update_timeline_handles(project_xml: bytes, pool_xml: str) -> bytes:
    """Set project timeline handles from each timeline sequence's Parent reference."""
    handles: list[str] = []
    for match in _TIMELINE_BLOCK.finditer(pool_xml):
        timeline = _parse_timeline_block(match.group())
        parent = _timeline_sequence(timeline).findtext("Parent")
        if not parent:
            raise ValueError("timeline sequence has no Parent handle")
        handles.append(parent)
    if not handles:
        raise ValueError("pool has no Sm2MpTimelineClip entries")

    text = project_xml.decode("utf-8")
    match = _TIMELINE_HANDLES.search(text)
    if match is None:
        raise ValueError("project XML has no TimelineHandleVec")

    vector = ET.Element("TimelineHandleVec")
    for handle in handles:
        ET.SubElement(vector, "Element").text = handle
    children = ET.tostring(vector, encoding="unicode")
    inner = children[len("<TimelineHandleVec>"):-len("</TimelineHandleVec>")]
    updated = text[:match.start()] + match.group(1) + inner + match.group(3) + text[match.end():]
    return updated.encode("utf-8")


__all__ = ["sequence_project_settings", "update_project_settings", "update_sequence_settings", "update_timeline_handles"]
