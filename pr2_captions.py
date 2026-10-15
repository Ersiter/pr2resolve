"""Extract Premiere caption records and encode plain Resolve subtitles.

Font strings are retained for audit only; the target writer does not claim to
transfer Premiere caption styling.
"""

from __future__ import annotations

import base64
import json
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, fields
from fractions import Fraction
from uuid import uuid4


_FORMATTED_TEXT_MAGIC = b"\x44\x33\x22\x11"
_FLATBUFFER_START = 12
_TEXT_WHITESPACE = "\n\r\t"
_ADOBE_TICKS_PER_SECOND = 254_016_000_000
_SUBTITLE_TRACK_FIELDS_BLOB = "000000010000000100000012004e0075006d004c00610079006500720073000000020000000000"


@dataclass(frozen=True)
class CaptionRecord:
    sequence_uid: str
    track_uid: str
    track_index: int
    item_object_id: str
    start_ticks: int | None
    end_ticks: int | None
    duration_ticks: int | None
    track_frame_rate_ticks: int | None
    text: str | None
    # Observed from the source font vector; never applied to target styling.
    font_candidates: tuple[str, ...]
    text_blocks: tuple[str, ...]
    decode_errors: tuple[str, ...]
    formatted_text_data: str
    formatted_text_hashes: tuple[str, ...]
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class CaptionTimingMapping:
    item_object_id: str
    source_start_ticks: int
    source_end_ticks: int
    target_start_frame: int
    target_end_frame: int
    start_error_ticks: Fraction
    end_error_ticks: Fraction


@dataclass(frozen=True)
class SubtitleTrackEncoding:
    element: ET.Element
    timing_mappings: tuple[CaptionTimingMapping, ...]


def _resolve_id(index: dict[str, ET.Element], object_id: str) -> ET.Element | None:
    return index.get(object_id)


def _read_u16(payload: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(payload):
        raise ValueError("truncated FormattedTextData uint16")
    return struct.unpack_from("<H", payload, offset)[0]


def _read_u32(payload: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(payload):
        raise ValueError("truncated FormattedTextData uint32")
    return struct.unpack_from("<I", payload, offset)[0]


def _read_u64(payload: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(payload):
        raise ValueError("truncated FormattedTextData uint64")
    return struct.unpack_from("<Q", payload, offset)[0]


def _read_i32(payload: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(payload):
        raise ValueError("truncated FormattedTextData int32")
    return struct.unpack_from("<i", payload, offset)[0]


def _flatbuffer_field(payload: bytes, table: int, slot: int) -> int | None:
    if table % 4:
        raise ValueError("misaligned FormattedTextData table")
    vtable = table - _read_i32(payload, table)
    if vtable < 0 or vtable % 2:
        raise ValueError("invalid FormattedTextData vtable offset")
    vtable_length = _read_u16(payload, vtable)
    object_length = _read_u16(payload, vtable + 2)
    if (
        vtable_length < 4
        or vtable_length % 2
        or vtable + vtable_length > len(payload)
        or object_length < 4
        or table + object_length > len(payload)
    ):
        raise ValueError("invalid FormattedTextData table bounds")
    slot_position = vtable + 4 + slot * 2
    if slot_position + 2 > vtable + vtable_length:
        return None
    relative = _read_u16(payload, slot_position)
    if relative == 0:
        return None
    if relative >= object_length:
        raise ValueError("invalid FormattedTextData field offset")
    return table + relative


def _flatbuffer_target(payload: bytes, field: int) -> int:
    relative = _read_u32(payload, field)
    target = field + relative
    if relative == 0 or target >= len(payload):
        raise ValueError("invalid FormattedTextData reference")
    return target


def _flatbuffer_string(payload: bytes, field: int) -> str:
    string = _flatbuffer_target(payload, field)
    length = _read_u32(payload, string)
    end = string + 4 + length
    if end >= len(payload) or payload[end] != 0:
        raise ValueError("invalid FormattedTextData string boundary")
    try:
        value = payload[string + 4 : end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("FormattedTextData string is not UTF-8") from error
    if not all(char.isprintable() or char in _TEXT_WHITESPACE for char in value):
        raise ValueError("FormattedTextData string contains non-text bytes")
    return value


def _flatbuffer_string_vector(payload: bytes, field: int) -> tuple[str, ...]:
    vector = _flatbuffer_target(payload, field)
    count = _read_u32(payload, vector)
    if count > 4096 or vector + 4 + count * 4 > len(payload):
        raise ValueError("invalid FormattedTextData string vector")
    return tuple(
        _flatbuffer_string(payload, vector + 4 + index * 4)
        for index in range(count)
    )


def _flatbuffer_table_vector(payload: bytes, field: int) -> tuple[int, ...]:
    vector = _flatbuffer_target(payload, field)
    count = _read_u32(payload, vector)
    if count > 4096 or vector + 4 + count * 4 > len(payload):
        raise ValueError("invalid FormattedTextData table vector")
    tables = tuple(
        _flatbuffer_target(payload, vector + 4 + index * 4)
        for index in range(count)
    )
    for table in tables:
        _flatbuffer_field(payload, table, 0)
    return tables


def _decode_formatted_text(payload: bytes) -> tuple[str, tuple[str, ...]]:
    """Decode the documented FormattedTextData FlatBuffer text/font vectors."""
    if len(payload) < 16 or payload[8:12] != _FORMATTED_TEXT_MAGIC:
        raise ValueError("unknown FormattedTextData encoding")
    if _read_u64(payload, 0) != len(payload) - _FLATBUFFER_START:
        raise ValueError("FormattedTextData byte length does not match its header")
    root = _FLATBUFFER_START + _read_u32(payload, _FLATBUFFER_START)
    root_document = _flatbuffer_field(payload, root, 0)
    if root_document is None:
        raise ValueError("FormattedTextData has no document table")
    document = _flatbuffer_target(payload, root_document)
    run_vector = _flatbuffer_field(payload, document, 0)
    if run_vector is None:
        raise ValueError("FormattedTextData has no run vector")
    runs = _flatbuffer_table_vector(payload, run_vector)
    if not runs:
        raise ValueError("FormattedTextData has no text runs")
    strings: list[str] = []
    for run in runs:
        text_field = _flatbuffer_field(payload, run, 0)
        if text_field is None:
            raise ValueError("FormattedTextData run has no text field")
        strings.append(_flatbuffer_string(payload, text_field))
    font_vector = _flatbuffer_field(payload, document, 1)
    fonts = _flatbuffer_string_vector(payload, font_vector) if font_vector is not None else ()
    return "".join(strings), fonts


def _int_text(parent: ET.Element, path: str) -> int | None:
    value = parent.findtext(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def extract_captions(prproj_root: ET.Element, sequence_uid: str) -> list[CaptionRecord]:
    """Return caption items in one Premiere sequence, preserving source timing and refs."""
    by_id = {
        element.get("ObjectID", ""): element
        for element in prproj_root.iter()
        if element.get("ObjectID")
    }
    by_uid = {
        element.get("ObjectUID", ""): element
        for element in prproj_root.iter()
        if element.get("ObjectUID")
    }
    sequence = next(
        (element for element in prproj_root.findall("Sequence") if element.get("ObjectUID") == sequence_uid),
        None,
    )
    if sequence is None:
        raise ValueError(f"Sequence not found: {sequence_uid}")

    payloads_by_hash: dict[str, set[bytes]] = {}
    for node in prproj_root.iter("FormattedTextData"):
        if node.text and node.text.strip() and node.get("BinaryHash"):
            payloads_by_hash.setdefault(node.get("BinaryHash", ""), set()).add(
                base64.b64decode(node.text.strip(), validate=True)
            )

    records: list[CaptionRecord] = []
    track_seen: set[tuple[str, str]] = set()
    for group_pair in sequence.findall("TrackGroups/TrackGroup"):
        group_ref = group_pair.find("Second")
        group = _resolve_id(by_id, group_ref.get("ObjectRef", "")) if group_ref is not None else None
        if group is None or group.tag not in {"DataTrackGroup", "CaptionTrackGroup"}:
            continue
        frame_rate_ticks = _int_text(group, "TrackGroup/FrameRate")
        track_refs = group.findall("TrackGroup/Tracks/Track")
        for track_ref in track_refs:
            track_uid = track_ref.get("ObjectURef", "")
            track = by_uid.get(track_uid)
            if track is None:
                continue
            for item_ref in track.findall(".//TrackItems/TrackItem"):
                item = _resolve_id(by_id, item_ref.get("ObjectRef", ""))
                if item is None or item.tag != "CaptionDataClipTrackItem":
                    continue
                key = (track_uid, item.get("ObjectID", ""))
                if key in track_seen:
                    continue
                track_seen.add(key)

                block_refs = sorted(
                    item.findall("BlockVector/BlockVectorItem"),
                    key=lambda block_ref: int(block_ref.get("Index", "0")),
                )
                blocks = [
                    by_id[ref]
                    for block_ref in block_refs
                    if (ref := block_ref.get("ObjectRef", "")) in by_id
                ]
                encoded: list[str] = []
                hashes: list[str] = []
                formatted_nodes_found = False
                block_payloads: list[tuple[ET.Element, list[bytes]]] = []
                decode_errors: list[str] = []
                for block in blocks:
                    payloads: list[bytes] = []
                    nodes = block.findall("FormattedTextData")
                    formatted_nodes_found = formatted_nodes_found or bool(nodes)
                    for node in nodes:
                        binary_hash = node.get("BinaryHash", "")
                        if binary_hash and binary_hash not in hashes:
                            hashes.append(binary_hash)
                        if node.text and node.text.strip():
                            encoded_value = node.text.strip()
                            encoded.append(encoded_value)
                            payloads.append(base64.b64decode(encoded_value, validate=True))
                        elif binary_hash and len(payloads_by_hash.get(binary_hash, set())) == 1:
                            recovered = next(iter(payloads_by_hash[binary_hash]))
                            encoded.append(base64.b64encode(recovered).decode("ascii"))
                            payloads.append(recovered)
                        else:
                            decode_errors.append(
                                f"block {block.get('ObjectID', '')}: payload is missing or ambiguous"
                            )
                    if nodes:
                        block_payloads.append((block, payloads))
                text_blocks: list[str] = []
                fonts_found: list[str] = []
                for block, payloads in block_payloads:
                    block_strings: list[str] = []
                    for payload in payloads:
                        try:
                            block_text, block_fonts = _decode_formatted_text(payload)
                        except ValueError as error:
                            decode_errors.append(f"block {block.get('ObjectID', '')}: {error}")
                            continue
                        block_strings.append(block_text)
                        fonts_found.extend(block_fonts)
                    if len(block_strings) == len(block.findall("FormattedTextData")):
                        text_blocks.append("".join(block_strings))
                if not formatted_nodes_found:
                    decode_errors.append("caption has no FormattedTextData node")
                text = "".join(text_blocks) if not decode_errors else None
                fonts = tuple(dict.fromkeys(fonts_found))

                timing = item.find("DataClipTrackItem/ClipTrackItem/TrackItem")
                start_ticks = _int_text(timing, "Start") if timing is not None else None
                end_ticks = _int_text(timing, "End") if timing is not None else None
                duration_ticks = (
                    end_ticks - start_ticks
                    if start_ticks is not None and end_ticks is not None
                    else None
                )
                source_refs = [
                    f"track:{track_uid}",
                    f"item:{item.get('ObjectID', '')}",
                ]
                source_refs.extend(f"block:{block.get('ObjectID', '')}" for block in blocks)
                source_refs.extend(
                    f"subclip:{subclip_ref}"
                    for subclip in item.findall("DataClipTrackItem/ClipTrackItem/SubClip")
                    if (subclip_ref := subclip.get("ObjectRef"))
                )
                records.append(CaptionRecord(
                    sequence_uid=sequence_uid,
                    track_uid=track_uid,
                    track_index=int(track_ref.get("Index", "0")),
                    item_object_id=item.get("ObjectID", ""),
                    start_ticks=start_ticks,
                    end_ticks=end_ticks,
                    duration_ticks=duration_ticks,
                    track_frame_rate_ticks=frame_rate_ticks,
                    text=text,
                    font_candidates=fonts,
                    text_blocks=tuple(text_blocks),
                    decode_errors=tuple(decode_errors),
                    formatted_text_data="\n".join(encoded),
                    formatted_text_hashes=tuple(hashes),
                    source_refs=tuple(source_refs),
                ))
    return records


def caption_records_from_xml(root: ET.Element) -> list[CaptionRecord]:
    """Read preserved source caption records from the converter's XML comment."""
    records: list[CaptionRecord] = []
    record_fields = {field.name for field in fields(CaptionRecord)}
    tuple_fields = {
        "font_candidates", "text_blocks", "decode_errors",
        "formatted_text_hashes", "source_refs",
    }
    integer_fields = {
        "track_index", "start_ticks", "end_ticks", "duration_ticks",
        "track_frame_rate_ticks",
    }

    for node in root.iter():
        if node.tag is not ET.Comment or not (node.text or "").strip().startswith("pr2resolve-timeline-state:"):
            continue
        encoded = (node.text or "").strip().split(":", 1)[1]
        try:
            state = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid pr2resolve timeline-state caption data") from error
        if not isinstance(state, dict):
            raise ValueError("invalid pr2resolve timeline-state caption data")
        unknown = state.get("unknown", [])
        if not isinstance(unknown, list):
            raise ValueError("invalid pr2resolve timeline-state caption data")
        for entry in unknown:
            if not isinstance(entry, list) or len(entry) != 2 or not all(isinstance(value, str) for value in entry):
                raise ValueError("invalid pr2resolve timeline-state source record")
            key, raw_record = entry
            if not key.startswith("caption-source:"):
                continue
            try:
                value = json.loads(raw_record)
            except json.JSONDecodeError as error:
                raise ValueError("invalid preserved caption record") from error
            if not isinstance(value, dict) or set(value) != record_fields:
                raise ValueError("preserved caption record has an unexpected shape")
            for name in integer_fields:
                item = value[name]
                if item is not None and (not isinstance(item, int) or isinstance(item, bool)):
                    raise ValueError(f"preserved caption field {name} is not an integer")
                if name == "track_index" and item is None:
                    raise ValueError("preserved caption field track_index is missing")
            for name in tuple_fields:
                item = value[name]
                if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
                    raise ValueError(f"preserved caption field {name} is not a string list")
                value[name] = tuple(item)
            for name in ("sequence_uid", "track_uid", "item_object_id", "formatted_text_data"):
                if not isinstance(value[name], str):
                    raise ValueError(f"preserved caption field {name} is not text")
            if value["text"] is not None and not isinstance(value["text"], str):
                raise ValueError("preserved caption field text is not text")
            records.append(CaptionRecord(**value))
    return records


def encode_subtitle_track(
    captions: list[CaptionRecord],
    target_sequence_uuid: str,
    target_fps: Fraction | float,
) -> SubtitleTrackEncoding:
    """Build a plain Resolve subtitle track and retain exact timing residuals."""
    if not captions:
        raise ValueError("at least one caption is required")
    fps = target_fps if isinstance(target_fps, Fraction) else Fraction(str(target_fps))
    if fps <= 0:
        raise ValueError("target_fps must be positive")
    ordered = sorted(
        captions,
        key=lambda caption: caption.start_ticks if caption.start_ticks is not None else -1,
    )
    cues: list[tuple[int, int, str, CaptionRecord]] = []
    mappings: list[CaptionTimingMapping] = []
    for caption in ordered:
        if caption.start_ticks is None or caption.end_ticks is None or caption.end_ticks <= caption.start_ticks:
            raise ValueError(f"caption {caption.item_object_id} has invalid source timing")
        if caption.text is None or not caption.text:
            reason = "; ".join(caption.decode_errors) or "decoded text is empty"
            raise ValueError(f"caption {caption.item_object_id} cannot be encoded: {reason}")
        source_start_frame = Fraction(caption.start_ticks, _ADOBE_TICKS_PER_SECOND) * fps
        source_end_frame = Fraction(caption.end_ticks, _ADOBE_TICKS_PER_SECOND) * fps
        start_frame = round(source_start_frame)
        end_frame = round(source_end_frame)
        if end_frame <= start_frame:
            raise ValueError(f"caption {caption.item_object_id} rounds to zero target frames")
        if cues and start_frame < cues[-1][0] + cues[-1][1]:
            raise ValueError("Resolve subtitle track cannot contain overlapping cues")
        cues.append((start_frame, end_frame - start_frame, caption.text, caption))
        mappings.append(CaptionTimingMapping(
            item_object_id=caption.item_object_id,
            source_start_ticks=caption.start_ticks,
            source_end_ticks=caption.end_ticks,
            target_start_frame=start_frame,
            target_end_frame=end_frame,
            start_error_ticks=Fraction(start_frame, 1) / fps * _ADOBE_TICKS_PER_SECOND - caption.start_ticks,
            end_error_ticks=Fraction(end_frame, 1) / fps * _ADOBE_TICKS_PER_SECOND - caption.end_ticks,
        ))

    vector_entry = ET.Element("Element")
    track = ET.SubElement(vector_entry, "Sm2TiTrack", {"DbId": str(uuid4())})
    ET.SubElement(track, "FieldsBlob").text = _SUBTITLE_TRACK_FIELDS_BLOB
    ET.SubElement(track, "Type").text = "2"
    ET.SubElement(track, "SubType").text = "0"
    ET.SubElement(track, "Flags").text = "0"
    ET.SubElement(track, "Sequence").text = target_sequence_uuid
    items = ET.SubElement(track, "Items")
    for start_frame, duration_frames, text, _caption in cues:
        element = ET.SubElement(items, "Element")
        generator = ET.SubElement(element, "Sm2TiGenerator", {"DbId": str(uuid4())})
        ET.SubElement(generator, "FieldsBlob")
        ET.SubElement(generator, "PrettyType").text = "Subtitle"
        ET.SubElement(generator, "Name").text = text
        ET.SubElement(generator, "Start").text = str(start_frame)
        ET.SubElement(generator, "Duration").text = str(duration_frames)
        ET.SubElement(generator, "LinkedItemSync")
        ET.SubElement(generator, "WasDisbanded").text = "false"
        ET.SubElement(generator, "MarkersBA")
        ET.SubElement(generator, "UiMemento").text = "0"
        ET.SubElement(generator, "Flags").text = "0"
        ET.SubElement(generator, "PriorityIndex").text = "0"
        ET.SubElement(generator, "EffectFiltersBA")
        ET.SubElement(generator, "ImportExportMetadataBA")
        ET.SubElement(generator, "RenderTextEnabled").text = "true"
        ET.SubElement(generator, "RenderTextGanged").text = "true"
        ET.SubElement(generator, "RenderTextPrefixed").text = "true"
        ET.SubElement(generator, "In")
    ET.SubElement(track, "UserDefinedName")
    ET.SubElement(track, "LayersVec")
    return SubtitleTrackEncoding(vector_entry, tuple(mappings))
