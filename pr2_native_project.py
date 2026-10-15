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


__all__ = ["update_sequence_settings", "update_timeline_handles"]
