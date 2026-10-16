"""Encode Resolve 21 marker blobs from measured export fields.

Marker payload layout also follows the independent reference at
https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/resolve-advanced/vendor/drp-format/timeline-markers-blob.js.
Resolve 21 uses a raw envelope and an extra container around that payload.
"""

from __future__ import annotations

import json
import re
import struct
import uuid
from typing import Any
from xml.etree import ElementTree as ET

from pr2_constants import PP_LABEL_TO_RESOLVE_COLOR

_MARKER_COLOR_BITS = {
    "Blue": 2,
    "Cyan": 4,
    "Green": 8,
    "Yellow": 16,
    "Red": 32,
    "Pink": 64,
    "Purple": 128,
    "Fuchsia": 512,
    "Rose": 1024,
    "Lavender": 2048,
    "Sky": 4096,
    "Mint": 8192,
    "Lemon": 16384,
    "Sand": 32768,
    "Cocoa": 65536,
    "Cream": 131072,
}


def _varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _length_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def encode_timeline_markers(markers: list[dict[str, Any]]) -> str:
    """Encode Resolve 21 sequence markers in its raw-protobuf envelope."""
    entries = []
    for marker in sorted(markers, key=lambda item: int(item["frame"])):
        frame = int(marker["frame"])
        color = str(marker.get("color") or "Blue")
        if frame < 0:
            raise ValueError("timeline marker frame must be non-negative")
        if color not in _MARKER_COLOR_BITS:
            raise ValueError(f"unsupported Resolve marker color: {color}")
        strings = (
            str(marker.get("note") or ""),
            str(max(1, int(marker.get("duration") or 1))),
            str(marker.get("name") or ""),
        )
        body = _varint(8) + _varint(_MARKER_COLOR_BITS[color])
        body += b"".join(_length_field(3, value.encode("utf-8")) for value in strings)
        custom_data = str(marker.get("custom_data") or "")
        if custom_data:
            body += _length_field(6, custom_data.encode("utf-8"))
        marker_message = _length_field(1, body)
        wrapped = struct.pack(">II", 2, len(marker_message)) + marker_message
        key = _length_field(1, _varint(8) + _varint(frame * 2))
        entries.append(_length_field(2, key + _length_field(2, wrapped)))
    protobuf = _length_field(2, b"".join(entries))
    blob_data = struct.pack(">II", 10001, len(protobuf) + 1) + b"\x80" + protobuf
    key = "BlobData".encode("utf-16-be")
    fields_blob = (
        struct.pack(">II", 1, 1)
        + struct.pack(">I", len(key)) + key
        + struct.pack(">IBI", 12, 0, len(blob_data)) + blob_data
    )
    return fields_blob.hex()


def sequence_markers(source: ET.Element) -> list[dict[str, Any]]:
    """Read sequence-level FCP7 markers; color mapping remains explicitly Blue."""
    markers = []
    for node in source.findall("marker"):
        try:
            frame = int(node.findtext("in") or "0")
            out_frame = int(node.findtext("out") or "-1")
        except ValueError:
            continue
        markers.append({
            "frame": frame,
            "duration": max(1, out_frame - frame) if out_frame >= 0 else 1,
            "name": node.findtext("name") or "",
            "note": node.findtext("comment") or "",
            "color": "Blue",
        })
    return markers


def sequence_marks(source: ET.Element) -> tuple[int | None, int | None] | None:
    """Convert FCP7's exclusive Out to Resolve's inclusive frame index."""
    if source.find("in") is None and source.find("out") is None:
        return None
    values = []
    for field in ("in", "out"):
        try:
            value = int(source.findtext(field) or "-1")
        except ValueError as exc:
            raise ValueError(f"sequence {field} mark must be an integer") from exc
        if value < -1:
            raise ValueError(f"sequence {field} mark must be -1 or non-negative")
        values.append(None if value == -1 else value)
    mark_in, mark_out = values
    if mark_out is not None:
        if mark_out <= (mark_in if mark_in is not None else 0):
            raise ValueError("sequence Out must follow In and include at least one frame")
        mark_out -= 1
    return mark_in, mark_out


def write_sequence_marks(timeline: ET.Element, marks: tuple[int | None, int | None]) -> None:
    """Write measured Resolve 21 timeline-clip fields, including one-sided marks."""
    for endpoint, value in zip(("In", "Out"), marks):
        for kind in ("Audio", "Video"):
            field = timeline.find("Mark" + endpoint + kind)
            if field is None:
                raise ValueError(f"native timeline clip has no Mark{endpoint}{kind} field")
            field.text = str(value) if value is not None else ""


def sequence_id_from_pool(pool_xml: str) -> str:
    timeline = re.search(r"(?s)<Sm2MpTimelineClip\b.*?</Sm2MpTimelineClip>", pool_xml)
    if timeline is None:
        raise ValueError("native media pool has no timeline clip")
    sequence = re.search(r"<Sm2Sequence\b[^>]*\bDbId=\"([^\"]+)\"", timeline.group(0))
    if sequence is None:
        raise ValueError("native timeline has no sequence owner")
    return sequence.group(1)


def sequence_lockable_blob(sequence_id: str, markers: list[dict[str, Any]]) -> str:
    """Build one Resolve lockable blob record for a sequence marker collection."""
    fields_blob = encode_timeline_markers(markers)
    blob_id = str(uuid.uuid4())
    return (
        "<Element><Sm2SequenceLockableBlob DbId=\"" + blob_id + "\">"
        "<FieldsBlob>" + fields_blob + "</FieldsBlob>"
        "<BlobOwner>" + sequence_id + "</BlobOwner><DbSavedTime>0</DbSavedTime>"
        "</Sm2SequenceLockableBlob></Element>"
    )


def clip_color_lockable_blob(clip_id: str, color: int) -> str:
    """Write Resolve's authoritative item color, not the clip's cached enum."""
    protobuf = _varint(5 << 3) + _varint(color * 2)
    blob_data = struct.pack(">II", 10001, len(protobuf) + 1) + b"\x80" + protobuf
    key = "BlobData".encode("utf-16-be")
    fields_blob = (
        struct.pack(">II", 1, 1)
        + struct.pack(">I", len(key)) + key
        + struct.pack(">IBI", 12, 0, len(blob_data)) + blob_data
    ).hex()
    return (
        '<Element><Sm2TiItemLockableBlob DbId="' + str(uuid.uuid4()) + '">'
        '<FieldsBlob>' + fields_blob + '</FieldsBlob>'
        '<BlobOwner>' + clip_id + '</BlobOwner><DbSavedTime>0</DbSavedTime>'
        '</Sm2TiItemLockableBlob></Element>'
    )


def item_blob_elements(project_xml: str, owners: set[str]) -> list[str]:
    """Copy only item records whose clips survive a multi-timeline merge."""
    return [
        record for record in re.findall(
            r"(?s)<Element>\s*<Sm2TiItemLockableBlob\b.*?</Sm2TiItemLockableBlob>\s*</Element>",
            project_xml,
        ) if ET.fromstring(record).findtext("Sm2TiItemLockableBlob/BlobOwner") in owners
    ]


def append_lockable_blob(project_xml: str, blob_element: str) -> str:
    """Append a lockable blob to the project's Resolve lockable map."""
    updated, count = re.subn(
        r"(?s)(<LocableBlobSet>)(.*?)(</LocableBlobSet>)",
        lambda match: match.group(1) + match.group(2) + blob_element + match.group(3),
        project_xml,
        count=1,
    )
    if count != 1:
        raise ValueError("native project has no lockable blob set")
    return updated


def marker_blob_elements(project_xml: str) -> list[str]:
    """Return serialized sequence marker records from a per-sequence package."""
    return re.findall(
        r"(?s)<Element>\s*<Sm2SequenceLockableBlob\b.*?</Sm2SequenceLockableBlob>\s*</Element>",
        project_xml,
    )


def remap_marker_blob(blob_element: str, old_sequence_id: str, new_sequence_id: str) -> str:
    """Assign a copied sequence marker blob a fresh identity and owner."""
    blob_id = str(uuid.uuid4())
    updated, count = re.subn(r'(Sm2SequenceLockableBlob DbId=\")[^\"]+', rf"\g<1>{blob_id}", blob_element, count=1)
    if count != 1:
        raise ValueError("native sequence marker record has no DbId")
    updated, count = re.subn(
        rf"<BlobOwner>{re.escape(old_sequence_id)}</BlobOwner>",
        f"<BlobOwner>{new_sequence_id}</BlobOwner>",
        updated,
        count=1,
    )
    if count != 1:
        raise ValueError("native sequence marker record has no matching owner")
    return updated


def unwritten_state_warnings(source: ET.Element, state: dict[str, Any]) -> list[str]:
    """Describe explicit timeline state that has no verified native writer."""
    warnings = []
    work_area = state.get("work_area")
    if isinstance(work_area, list) and any(
        str(value).lstrip("-").isdigit() and int(value) >= 0 for value in work_area if value is not None
    ):
        warnings.append("sequence work area is not applied in native DRP")
    for key, raw in state.get("unknown", []):
        if str(key).startswith("clip-label:"):
            try:
                record = json.loads(raw)
                preset = str(record.get("preset", "")).rsplit(".", 1)[-1]
                if not preset.isdigit() or int(preset) not in PP_LABEL_TO_RESOLVE_COLOR:
                    warnings.append(f"Unmapped source clip-label preset {preset!r}; raw color remains in source state")
            except (TypeError, ValueError):
                warnings.append("Could not read source clip-label state")
        if not str(key).startswith("track-state:"):
            continue
        try:
            track = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if track.get("solo"):
            warnings.append("solo track state is not applied in native DRP")
    markers = state.get("markers", [])
    if any(item.get("scope") in ("track", "clip") for item in markers if isinstance(item, dict)):
        warnings.append("track or clip markers are not applied in native DRP")
    if any(item.get("scope") == "sequence" and item.get("color_index") is not None
           for item in markers if isinstance(item, dict)):
        warnings.append("Premiere sequence marker colors are not mapped; Resolve markers use Blue")
    return list(dict.fromkeys(warnings))
