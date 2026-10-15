"""Write a codec-backed native Resolve package from one FCP7 sequence."""

from __future__ import annotations

import io
import base64
import json
import math
import os
import posixpath
import re
import struct
import tempfile
import uuid
import zipfile
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from pr2_constants import DEFAULT_FPS, PP_LABEL_TO_RESOLVE_COLOR, ProjectBin, _build_file_index, _timeline_track_states
from pr2_native_project import update_sequence_settings, update_timeline_handles
from pr2_native_time import build_media_timemap, extract_fcp7_retime_alignment, retimed_source_in
from pr2_native_state import (
    append_lockable_blob,
    marker_blob_elements,
    remap_marker_blob,
    sequence_id_from_pool,
    sequence_lockable_blob,
    sequence_markers,
    unwritten_state_warnings,
)


TEMPLATE = Path(__file__).with_name("resources") / "resolve21_native_template.drp"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

def _profile_name(value: object) -> str:
    return str(value or "").casefold().replace("rec. ", "rec.")


def _configure_color(members: dict[str, bytes], profiles: dict[str, Any] | None) -> dict[str, Any]:
    """Apply verified Resolve 21 project color enums and return an audit plan."""
    blobs = {name: members.pop(f"pr2/setup_{name}.blob", None) for name in ("default", "acescct", "hlg")}
    plan: dict[str, Any] = {"source": profiles or {}, "native": {}, "unmapped": []}
    if any(value is None for value in blobs.values()):
        if profiles:
            plan["unmapped"].append("native setup resource missing")
        return plan
    working = _profile_name((profiles or {}).get("WorkingColorSpace") or (profiles or {}).get("WorkingColorSpaceConfiguration"))
    output = _profile_name((profiles or {}).get("OutputColorSpace"))
    selected = "default"
    if "acescct" in working:
        selected = "acescct"
        plan["native"] = {"colorScienceMode": "ACEScct"}
    elif "2100 hlg" in working or "2100 hlg" in output:
        selected = "hlg"
        plan["native"] = {"colorScienceMode": "DaVinci YRGB Color Managed", "timeline": "Rec.2100 HLG", "output": "Rec.2100 HLG"}
    elif working and not any(name in working for name in ("709", "display-referred")):
        plan["unmapped"].append((profiles or {}).get("WorkingColorSpace", working))
    if output and not any(name in output for name in ("709", "display-referred", "2100 hlg")):
        plan["unmapped"].append((profiles or {}).get("OutputColorSpace", output))
    text = members["project.xml"].decode("utf-8", "replace")
    pattern = r"(?s)(<SM_Config\b.*?<FieldsBlob>)[0-9a-fA-F]*(</FieldsBlob>)"
    text, count = re.subn(pattern, rf"\g<1>{(blobs[selected] or b'').hex()}\g<2>", text, count=1)
    if count != 1:
        raise ValueError("native project has no SM_Config FieldsBlob")
    members["project.xml"] = text.encode("utf-8")
    return plan


def _uid() -> str:
    return str(uuid.uuid4())


def _xml(root: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8", short_empty_elements=True) + b"\n"


def _set_child(node: ET.Element, name: str, value: object) -> None:
    child = node.find(name)
    if child is None:
        child = ET.SubElement(node, name)
    child.text = str(value)


def _int_text(node: ET.Element, name: str) -> int:
    try:
        return int(node.findtext(name) or "0")
    except ValueError:
        return 0


def _rate(node: ET.Element) -> float:
    try:
        rate = float(node.findtext("rate/timebase") or DEFAULT_FPS)
        return rate * 1000 / 1001 if (node.findtext("rate/ntsc") or "").strip().upper() == "TRUE" else rate
    except ValueError:
        return DEFAULT_FPS


def _path_key(path: str) -> str:
    # Windows media paths remain case insensitive when converting on Unix.
    windows = os.name == "nt" or bool(re.match(r"^[A-Za-z]:[/\\]", path)) or path.startswith("\\\\")
    return posixpath.normpath(path.replace("\\", "/")).casefold() if windows else posixpath.normpath(path)


def _clip_duration(node: ET.Element) -> str:
    start, end = _int_text(node, "start"), _int_text(node, "end")
    return str(max(0, end - start) if end or start else _int_text(node, "duration"))


def _native_path(pathurl: str) -> str:
    if not pathurl.startswith("file:"):
        return pathurl
    parsed = urlparse(pathurl)
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc != "localhost":
        path = f"//{parsed.netloc}{path}"
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path.replace("/", "\\") if os.name == "nt" else path


def _read_varint(data: bytes, position: int) -> tuple[int, int]:
    value, shift = 0, 0
    while True:
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if byte < 128:
            return value, position
        shift += 7


def _write_varint(value: int) -> bytes:
    output = bytearray()
    while value >= 128:
        output.append((value & 0x7F) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _protobuf_fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    fields, position = [], 0
    while position < len(data):
        key, position = _read_varint(data, position)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, position = _read_varint(data, position)
        elif wire == 1:
            value, position = data[position:position + 8], position + 8
        elif wire == 2:
            size, position = _read_varint(data, position)
            value, position = data[position:position + size], position + size
        elif wire == 5:
            value, position = data[position:position + 4], position + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _protobuf_encode(fields: list[tuple[int, int, bytes | int]]) -> bytes:
    output = bytearray()
    for number, wire, value in fields:
        output += _write_varint((number << 3) | wire)
        if wire == 0:
            output += _write_varint(int(value))
        elif wire in (1, 5):
            output += bytes(value)
        else:
            encoded = bytes(value)
            output += _write_varint(len(encoded)) + encoded
    return bytes(output)


def _decode_wrapper_hex(value: str) -> bytes:
    wrapped = bytes.fromhex(value)
    if wrapped[8:9] != b"\x80":
        raise ValueError("native clip requires an uncompressed 0x80 wrapper")
    return wrapped[9:]


def _encode_wrapper_hex(data: bytes) -> str:
    return (b"\0\0\0\2" + (len(data) + 1).to_bytes(4, "big") + b"\x80" + data).hex()


def _set_native_clip_color(node: ET.Element, resolve_color: int) -> None:
    """Patch only the verified nested clip-color enum in the template blob."""
    blob = node.find("FieldsBlob")
    if blob is None or not blob.text:
        raise ValueError("native clip has no FieldsBlob")
    root_fields = _protobuf_fields(_decode_wrapper_hex(blob.text))
    payload_index = next((i for i, (number, wire, _) in enumerate(root_fields)
                          if number == 1 and wire == 2), None)
    if payload_index is None:
        raise ValueError("native clip FieldsBlob has no payload")
    payload_fields = _protobuf_fields(bytes(root_fields[payload_index][2]))
    color_fields = [(number, wire, resolve_color) if number == 6 and wire == 0
                    else (number, wire, value)
                    for number, wire, value in payload_fields]
    if not any(number == 6 and wire == 0 for number, wire, _ in payload_fields):
        color_fields.append((6, 0, resolve_color))
    root_fields[payload_index] = (1, 2, _protobuf_encode(color_fields))
    blob.text = _encode_wrapper_hex(_protobuf_encode(root_fields))


def _timeline_clip_labels(state: dict[str, Any]) -> dict[tuple[str, int, str, int, int], int]:
    labels: dict[tuple[str, int, str, int, int], int] = {}
    for key, raw in state.get("unknown", []):
        if not str(key).startswith("clip-label:"):
            continue
        try:
            record = json.loads(raw)
            preset = str(record.get("preset", "")).rsplit(".", 1)[-1]
            label_index = int(preset)
            color = PP_LABEL_TO_RESOLVE_COLOR[label_index]
            media_type = str(record["media_type"])
            track_index = int(record["track_index"])
            name, start = str(record["name"]), int(record["start"])
            duration = int(record["end"]) - start
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        labels[(media_type, track_index, name, start, duration)] = color
    return labels


def _rewrite_av_clone(node: ET.Element, full_path: str, duration: int = 0, fps: float = 0.0, width: int = 0, height: int = 0, source_start: int = 0) -> ET.Element:
    path = Path(full_path)
    video_uuid = _uid()
    for info in node.findall(".//BtVideoInfo") + node.findall(".//BtAudioInfo"):
        clip = info.find("Clip")
        if clip is None or not clip.text:
            continue
        fields = _protobuf_fields(_decode_wrapper_hex(clip.text))
        rewritten = []
        for number, wire, value in fields:
            if wire == 2 and number == 1:
                value = str(path.parent).encode()
            elif wire == 2 and number == 2:
                value = path.name.encode()
            elif wire == 2 and number == 6 and info.tag == "BtVideoInfo":
                value = path.name.encode()
            elif wire == 2 and number == 7 and info.tag == "BtVideoInfo":
                value = video_uuid.encode()
            rewritten.append((number, wire, value))
        clip.text = _encode_wrapper_hex(_protobuf_encode(rewritten))
        old_id = info.get("DbId") or ""
        info_id = _uid()
        info.set("DbId", info_id)
        root_blob = node.find("FieldsBlob")
        if root_blob is None or not root_blob.text or not old_id:
            continue
        raw = _decode_wrapper_hex(root_blob.text)
        old_bytes, new_bytes = old_id.encode("utf-16le"), info_id.encode("utf-16le")
        if old_bytes not in raw:
            continue
        raw = raw.replace(old_bytes, new_bytes)
        if b"H\x00" + new_bytes not in raw:
            raise ValueError("codec MediaRef missing after DbId rewrite")
        root_blob.text = _encode_wrapper_hex(raw)
    if duration > 0 or fps > 0 or width > 0 or height > 0:
        for info in node.findall(".//BtVideoInfo"):
            time_node = info.find("Time")
            if time_node is not None and time_node.text:
                raw = bytearray(bytes.fromhex(time_node.text))
                for key, value_type, value in (
                    ("StartFrame", 2, source_start.to_bytes(4, "big", signed=True)),
                    ("NumFrames", 2, duration.to_bytes(4, "big", signed=True) if duration > 0 else None),
                    ("FrameRate", 12, struct.pack(">I", 16) + struct.pack("<d", fps) + b"\0" * 8 if fps > 0 else None),
                ):
                    marker = key.encode("utf-16be") + struct.pack(">IB", value_type, 0)
                    offset = raw.find(marker) + len(marker)
                    if offset >= len(marker) and value is not None:
                        raw[offset:offset + len(value)] = value
                time_node.text = raw.hex()
            geometry = info.find("Geometry")
            if geometry is not None and geometry.text and width > 0 and height > 0:
                raw = bytearray(bytes.fromhex(geometry.text))
                marker = "Resolution".encode("utf-16be") + struct.pack(">IBI", 12, 0, 16)
                offset = raw.find(marker) + len(marker)
                if offset >= len(marker):
                    raw[offset:offset + 8] = width.to_bytes(8, "big")
                    raw[offset + 8:offset + 16] = height.to_bytes(8, "big")
                    geometry.text = raw.hex()
    if duration > 0 and fps > 0:
        units = math.ceil((duration / fps) * 187.5 / 8) * 8
        marker = "Duration".encode("utf-16be")
        for tracks in node.findall(".//TracksBA"):
            if not tracks.text:
                continue
            raw = bytearray(bytes.fromhex(tracks.text))
            start = raw.find(marker)
            if start >= 0:
                value_start = start + len(marker) + 4
                encoded_units = units.to_bytes(8, "big")
                raw[value_start:value_start + 8] = encoded_units
            tracks.text = bytes(raw).hex()
    return node


def _static_motion_payload(clip: ET.Element, source_geometry: tuple[int, int], timeline_geometry: tuple[int, int]) -> str | None:
    """Author static Resolve 21 transform curves without captured clip state."""
    basic = next((effect for effect in clip.findall("filter/effect") if (effect.findtext("effectid") or "").strip().lower() == "basic"), None)
    if basic is None:
        return None
    if basic.find(".//keyframe") is not None:
        raise ValueError(f"animated Basic Motion is unsupported: {clip.findtext('name') or 'clip'}")
    values = {}
    for parameter in basic.findall("parameter"):
        parameter_id = parameter.findtext("parameterid") or ""
        value = parameter.find("value")
        if value is None:
            continue
        if parameter_id in ("scale", "rotation", "aspect"):
            try:
                values[parameter_id] = float(value.text or "")
            except ValueError:
                raise ValueError(f"invalid Basic Motion {parameter_id}: {clip.findtext('name') or 'clip'}")
        elif parameter_id == "center":
            values["pan_x"] = float(value.findtext("horiz") or 0)
            values["pan_y"] = -float(value.findtext("vert") or 0)
        elif parameter_id == "centerOffset":
            if any(float(value.findtext(axis) or 0) != 0 for axis in ("horiz", "vert")):
                raise ValueError(f"non-default anchor point is unsupported: {clip.findtext('name') or 'clip'}")
    for effect in clip.findall("filter/effect"):
        if (effect.findtext("effectid") or "").strip().lower() != "opacity":
            continue
        value = next((p.find("value") for p in effect.findall("parameter") if p.findtext("parameterid") == "opacity"), None)
        if value is not None and value.text and float(value.text) != 100.0:
            raise ValueError(f"non-default opacity is unsupported: {clip.findtext('name') or 'clip'}")
    scale, rotation = values.get("scale", 100.0), values.get("rotation", 0.0)
    if values.get("aspect", 0) != 0:
        raise ValueError(f"non-uniform Basic Motion scale is unsupported: {clip.findtext('name') or 'clip'}")
    if not all(math.isfinite(value) for value in values.values()) or scale < 0:
        raise ValueError(f"invalid Basic Motion values: {clip.findtext('name') or 'clip'}")
    if not all(value > 0 for value in (*source_geometry, *timeline_geometry)):
        if scale == 100 and rotation == 0 and not values.get("pan_x") and not values.get("pan_y"):
            return None
        raise ValueError(f"Basic Motion requires source and timeline dimensions: {clip.findtext('name') or 'clip'}")
    # XMEML scale operates on source pixels; Resolve's default sizing fits the
    # unrotated source into the timeline before the Inspector zoom is applied.
    fit = min(timeline_geometry[0] / source_geometry[0], timeline_geometry[1] / source_geometry[1])
    zoom = scale / 100.0 / fit
    parameters = {0: (42, zoom), 1: (43, zoom), 2: (40, values.get("pan_x", 0.0)),
                  3: (41, values.get("pan_y", 0.0)), 5: (47, -rotation)}
    fields: list[tuple[int, int, bytes | int]] = [(1, 0, 4), (7, 0, 20)]
    # Resolve's section has twelve ordered slots, including empty defaults.
    for slot in range(12):
        if slot not in parameters:
            fields.append((9, 2, b""))
            continue
        parameter_id, value = parameters[slot]
        keyframe = _protobuf_encode([(2, 1, struct.pack("<d", value))])
        curve = _protobuf_encode([(1, 2, keyframe)])
        parameter = _protobuf_encode([(1, 0, parameter_id), (3, 2, curve)])
        fields.append((9, 2, parameter))
    return _encode_wrapper_hex(_protobuf_encode([(1, 2, _protobuf_encode(fields))]))


def _validate_sequence_capabilities(root: ET.Element) -> None:
    """Reject FCP structures that the native writer cannot represent faithfully."""
    problems: list[str] = []
    for key, raw in _timeline_state_manifest(root).get("unknown", []):
        if not str(key).startswith("unconverted-source:"):
            continue
        record = json.loads(raw)
        identity = record.get("clip_name") or record.get("track_uid") or "sequence"
        feature = record.get("match_name") or record.get("kind") or "source feature"
        problems.append(f"{identity}: unconverted Premiere {feature}")
    if root.findall(".//transitionitem"):
        problems.append("transitionitem/transition effects")
    if root.findall(".//clipitem//sequence") or root.findall(".//generatoritem"):
        problems.append("nested sequence or generator item")
    allowed_video = {"basic", "crop", "opacity", "timeremap", "lumetri"}
    warned_lumetri: set[str] = set()
    for clip in root.findall(".//clipitem"):
        name = clip.findtext("name") or "unnamed clip"
        file_node = clip.find("file")
        if file_node is None or not file_node.get("id"):
            problems.append(f"{name}: generator or clip without file")
        for effect in clip.findall("filter/effect"):
            effect_id = (effect.findtext("effectid") or "").strip().lower()
            if effect_id == "lumetri":
                if name not in warned_lumetri:
                    print(f"  Warning: {name}: native Lumetri grade is not applied")
                    warned_lumetri.add(name)
                continue
            if effect_id not in allowed_video and (effect.findtext("mediatype") or "video").strip().lower() == "video":
                problems.append(f"{name}: unsupported video effect {effect_id or 'unnamed effect'}")
            values = {p.findtext("parameterid"): p.findtext("value") for p in effect.findall("parameter")}
            if effect_id == "crop" and any(float(values.get(key) or 0) != 0 for key in ("left", "right", "top", "bottom")):
                problems.append(f"{name}: non-default crop")
            if effect_id == "opacity" and values.get("opacity") and float(values["opacity"]) != 100:
                problems.append(f"{name}: non-default opacity")
            if effect_id == "timeremap":
                try:
                    speed = float(values.get("speed") or 100)
                    variable = float(values.get("variablespeed") or 0)
                except ValueError:
                    problems.append(f"{name}: non-numeric retime")
                    continue
                if speed <= 0 or (values.get("reverse") or "FALSE").upper() == "TRUE":
                    problems.append(f"{name}: reverse or non-positive retime")
                elif effect.find("parameter[parameterid='graphdict']/keyframe") is not None:
                    try:
                        extract_fcp7_retime_alignment(clip, _rate((root.findall('.//sequence') or [root])[0]))
                    except ValueError as exc:
                        problems.append(f"{name}: {exc}")
                elif variable != 0:
                    problems.append(f"{name}: variable retime without graphdict")
    if problems:
        raise ValueError("native DRP unsupported features: " + "; ".join(dict.fromkeys(problems)))


def _timeline_state_manifest(root: ET.Element) -> dict[str, Any]:
    """Read opaque Premiere timeline state carried beside the FCP7 sequence."""
    for node in root:
        if node.tag is not ET.Comment or not (node.text or "").strip().startswith("pr2resolve-timeline-state:"):
            continue
        encoded = (node.text or "").strip().split(":", 1)[1]
        try:
            value = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _build_timeline(sequence: ET.Element | ET.ElementTree, grades: list[Any] | None = None) -> tuple[dict[str, bytes], ET.Element, dict[str, str]]:
    root = sequence.getroot() if isinstance(sequence, ET.ElementTree) else sequence
    source = (root.findall(".//sequence") or [root])[0]
    timeline_sample = source.find("media/video/format/samplecharacteristics")
    timeline_geometry = (
        _int_text(timeline_sample, "width") if timeline_sample is not None else 0,
        _int_text(timeline_sample, "height") if timeline_sample is not None else 0,
    )
    with zipfile.ZipFile(TEMPLATE) as archive:
        members = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    seq_name = next((name for name in members if name.startswith("SeqContainer/") and name.endswith(".xml")), None)
    if seq_name is None:
        raise ValueError("codec template has no sequence container")
    native = ET.fromstring(members[seq_name])
    files, video_ids = _build_file_index(root), set()
    for branch in ("video", "audio"):
        for track in source.findall(f"media/{branch}/track"):
            for clip in track.findall("clipitem"):
                file_node = clip.find("file")
                if file_node is None or not file_node.get("id"):
                    continue
                key = file_node.get("id") or ""
                if branch == "video":
                    video_ids.add(key)
    source_frames = {}
    for key, file_node in files.items():
        total = _int_text(file_node, "duration")
        if total <= 0:
            total = max((_int_text(c, "out") for c in source.iter("clipitem") if c.find("file") is not None and c.find("file").get("id") == key), default=0)
        source_frames[key] = total
    def clip_speed(clip: ET.Element) -> float:
        speeds = []
        for effect in clip.findall("filter/effect"):
            if (effect.findtext("effectid") or "").strip().lower() != "timeremap":
                continue
            try:
                speeds.append(float(next((p.findtext("value") for p in effect.findall("parameter") if p.findtext("parameterid") == "speed"), "100")))
            except ValueError as exc:
                raise ValueError(f"non-numeric retime: {clip.findtext('name') or 'clip'}") from exc
        if not speeds:
            return 100.0
        if any(abs(value - speeds[0]) > 1e-9 for value in speeds[1:]):
            raise ValueError(f"variable retime: {clip.findtext('name') or 'clip'}")
        return speeds[0]
    refs = {key: _uid() for key in files}
    video_template = next((n for n in native.iter("Sm2TiVideoClip") if n.findtext("Name") == "neutral-source.mp4"), native.find(".//Sm2TiVideoClip"))
    still_template = next((n for n in native.iter("Sm2TiVideoClip") if n.findtext("Name") == "neutral-still.png"), video_template)
    audio_templates = {Path(n.findtext("Name") or "").suffix.lower(): n for n in native.iter("Sm2TiAudioClip") if (n.findtext("Name") or "").startswith("neutral-audio.")}
    audio_template = native.find(".//Sm2TiAudioClip")
    audio_only_template = audio_templates.get(".wav", audio_template)
    if video_template is None or still_template is None or audio_template is None or audio_only_template is None:
        raise ValueError("codec template lacks required timeline nodes")
    tracks = {kind: ET.fromstring(ET.tostring(native.find(f"{vec}/Element/Sm2TiTrack"))) for vec, kind in (("VideoTrackVec", "video"), ("AudioTrackVec", "audio"))}
    state_manifest = _timeline_state_manifest(root)
    clip_labels = _timeline_clip_labels(state_manifest)
    for warning in unwritten_state_warnings(source, state_manifest):
        print(f"  Warning: {warning}")
    track_states = {(track.type, track.index): track for track in _timeline_track_states(source)}
    for vector_name, kind, generic in (("VideoTrackVec", "video", video_template), ("AudioTrackVec", "audio", audio_template)):
        vector = native.find(vector_name)
        if vector is None or tracks[kind] is None:
            raise ValueError("codec template lacks required track")
        vector.clear()
        for track_index, fcp_track in enumerate(source.findall(f"media/{kind}/track"), 1):
            wrapper, track = ET.SubElement(vector, "Element"), ET.fromstring(ET.tostring(tracks[kind]))
            track.set("DbId", _uid())
            items = track.find("Items")
            if items is None:
                items = ET.SubElement(track, "Items")
            items.clear()
            for clip in fcp_track.findall("clipitem"):
                file_node = clip.find("file")
                if file_node is not None:
                    file_node = files.get(file_node.get("id") or "", file_node)
                file_name = file_node.findtext("name") if file_node is not None else ""
                suffix = Path(file_name or "").suffix.lower()
                if kind == "video":
                    chosen = still_template if suffix in IMAGE_SUFFIXES else video_template
                elif file_node is not None and (file_node.get("id") or "") not in video_ids:
                    chosen = audio_templates.get(suffix, audio_only_template)
                else:
                    chosen = generic
                node = ET.fromstring(ET.tostring(chosen))
                node.set("DbId", _uid())
                _set_child(node, "Name", clip.findtext("name") or "clip")
                _set_child(node, "Start", clip.findtext("start") or "0")
                _set_child(node, "Duration", _clip_duration(clip))
                input_fps = _rate(file_node) if file_node is not None and file_node.find("rate") is not None else _rate(source)
                clip_fps = _rate(clip) if clip.find("rate") is not None else _rate(source)
                alignment = (extract_fcp7_retime_alignment(clip, _rate(source))
                             if clip.find("filter/effect/parameter[parameterid='graphdict']/keyframe") is not None else None)
                speed = 100.0 if alignment else clip_speed(clip)
                source_in = (alignment["encoded_in"] if alignment else
                             retimed_source_in(_int_text(clip, "in"), clip_fps, _rate(source), speed))
                _set_child(node, "In", "86400" if suffix in IMAGE_SUFFIXES else source_in)
                if alignment:
                    residual = alignment["source_trim_end_residual_frames"]
                    print(f"  Warning: {clip.findtext('name')}: piecewise-linear retime; "
                          f"last-frame alignment residual={residual!r} clip frames; Resolve render unverified")
                if file_node is not None:
                    file_id = file_node.get("id") or ""
                    _set_child(node, "MediaRef", refs.get(file_id, ""))
                    _set_child(node, "MediaFilePath", _native_path(file_node.findtext("pathurl") or ""))
                    if kind == "video":
                        _set_child(node, "CurrentSelectorIdx", "0")
                        _set_child(node, "MediaFrameRate", struct.pack("<d", input_fps).hex() + "00" * 8)
                    # Still sources retain Resolve's virtual one-day source.
                    if suffix not in IMAGE_SUFFIXES:
                        _set_child(node, "MediaTimemapBA", build_media_timemap(
                            source_frames[file_id], input_fps, speed, audio=kind == "audio",
                            keyframes=alignment["points"] if alignment else None,
                        ))
                motion_payload = None
                if kind == "video":
                    sample = file_node.find("media/video/samplecharacteristics") if file_node is not None else None
                    source_geometry = (
                        _int_text(sample, "width") if sample is not None else 0,
                        _int_text(sample, "height") if sample is not None else 0,
                    )
                    motion_payload = _static_motion_payload(clip, source_geometry, timeline_geometry)
                _set_child(node, "EffectFiltersBA", motion_payload or "")
                label_key = (
                    kind, track_index, clip.findtext("name") or "",
                    _int_text(clip, "start"), int(_clip_duration(clip)),
                )
                if label_key in clip_labels:
                    _set_native_clip_color(node, clip_labels[label_key])
                item = ET.SubElement(items, "Element")
                item.append(node)
            for field in ("FusionCompHolderItems", "LayersVec"):
                _set_child(track, field, "")
            state = track_states[(kind, track_index)]
            _set_child(track, "UserDefinedName", state.name)
            # Resolve 21 DC export: lock=1, disabled=2; both=3. Clip flag
            # bits and label-color enums are unrelated to these track flags.
            flags = (_int_text(track, "Flags") & ~3) | int(state.locked) | (0 if state.enabled else 2)
            _set_child(track, "Flags", str(flags))
            wrapper.append(track)
    members[seq_name] = _xml(native)
    return members, source, refs


def export_native_drp(
    sequence: ET.Element | ET.ElementTree | list[ET.Element | ET.ElementTree],
    output_path: str | Path,
    project_name: str,
    grades: list[Any] | None = None,
    color_profiles: dict[str, Any] | None = None,
    bin_members: list[ProjectBin] | None = None,
) -> Path:
    """Export one project package, optionally containing multiple sequences."""
    if isinstance(sequence, list):
        if not sequence:
            raise ValueError("native DRP requires at least one sequence")
        return _export_native_project(sequence, output_path, project_name, grades, color_profiles, bin_members)
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"codec DRP template not found: {TEMPLATE}")
    root = sequence.getroot() if isinstance(sequence, ET.ElementTree) else sequence
    _validate_sequence_capabilities(root)
    members, source, refs = _build_timeline(sequence, grades)
    pool_name = "MediaPool/Master/MpFolder.xml"
    with zipfile.ZipFile(TEMPLATE) as archive:
        pool = archive.read(pool_name).decode("utf-8", "replace")
    video_templates = re.findall(r"(?is)<Element>\s*(<Sm2MpVideoClip\b.*?</Sm2MpVideoClip>)\s*</Element>", pool)
    audio_templates = re.findall(r"(?is)<Element>\s*(<Sm2MpAudioClip\b.*?</Sm2MpAudioClip>)\s*</Element>", pool)
    audio_fallback = audio_templates[0] if audio_templates else ""
    audio_by_ext = {Path(re.search(r"(?is)<Name>(.*?)</Name>", n).group(1)).suffix.lower(): n for n in audio_templates if re.search(r"(?is)<Name>neutral-audio\.[^<]+</Name>", n)}
    files, video_ids = _build_file_index(root), set()
    for branch in ("video", "audio"):
        for track in source.findall(f"media/{branch}/track"):
            for clip in track.findall("clipitem"):
                file_node = clip.find("file")
                if file_node is None or not file_node.get("id"):
                    continue
                key = file_node.get("id") or ""
                if branch == "video":
                    video_ids.add(key)
    video_paths = {_path_key(_native_path(node.findtext("pathurl") or "")) for key, node in files.items() if key in video_ids}
    canonical: dict[tuple[object, ...], str] = {}
    media_aliases: dict[str, str] = {}
    generated = []
    for key, file_node in files.items():
        path, name = _native_path(file_node.findtext("pathurl") or ""), file_node.findtext("name") or "media"
        if not path:
            raise ValueError(f"unresolved media file reference: {key} ({name})")
        sample = file_node.find("media/video/samplecharacteristics")
        duration = _int_text(file_node, "duration")
        fps = _rate(file_node) if file_node.find("rate") is not None else _rate(source)
        width = _int_text(sample, "width") if sample is not None else 0
        height = _int_text(sample, "height") if sample is not None else 0
        source_start = _int_text(file_node, "timecode/frame")
        media_key = (_path_key(path), fps, duration, width, height, source_start)
        if media_key in canonical:
            media_aliases[refs[key]] = canonical[media_key]
            continue
        is_video, suffix = key in video_ids or _path_key(path) in video_paths, Path(name).suffix.lower()
        if is_video:
            chosen = (next((n for n in video_templates if "neutral-still.png" in n), video_templates[0]) if suffix in IMAGE_SUFFIXES else next((n for n in video_templates if "neutral-source.mp4" in n), video_templates[0]))
        else:
            chosen = audio_by_ext.get(suffix, audio_by_ext.get(".wav", audio_fallback))
        if not chosen:
            raise ValueError("codec template lacks required media node")
        item_id = refs[key]
        canonical[media_key] = item_id
        node = ET.fromstring(chosen); node.set("DbId", item_id)
        unique = node.find("UniqueMediaPoolItemId")
        if unique is not None:
            unique.text = _uid()
        _set_child(node, "Name", name)
        _rewrite_av_clone(node, path, duration=duration, fps=fps, width=width, height=height,
                          source_start=source_start)
        generated.append("   <Element>\n" + ET.tostring(node, encoding="unicode") + "\n   </Element>")
    for name, data in list(members.items()):
        if not name.startswith("SeqContainer/"):
            continue
        xml = ET.fromstring(data)
        for clip in xml.iter():
            if clip.tag not in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
                continue
            media_id = media_aliases.get(clip.findtext("MediaRef") or "")
            if media_id:
                _set_child(clip, "MediaRef", media_id)
        members[name] = _xml(xml)
    pool = re.sub(r"(?is)<Element>\s*<(?:Sm2MpVideoClip|Sm2MpAudioClip)\b.*?</(?:Sm2MpVideoClip|Sm2MpAudioClip)>\s*</Element>", "", pool)
    pool = re.sub(r"(?is)(<MediaVec>)(.*?)(</MediaVec>)", lambda m: m.group(1) + m.group(2) + "\n" + "\n".join(generated) + m.group(3), pool, count=1)
    pool = update_sequence_settings(pool, source)
    members[pool_name] = pool.encode("utf-8")
    members["project.xml"] = update_timeline_handles(members["project.xml"], pool)
    markers = sequence_markers(source)
    if markers:
        project_xml = members["project.xml"].decode("utf-8", "replace")
        blob = sequence_lockable_blob(sequence_id_from_pool(pool), markers)
        members["project.xml"] = append_lockable_blob(project_xml, blob).encode("utf-8")
    _configure_color(members, color_profiles)
    sequence_name = source.findtext("name") or project_name
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            if name == "project.xml":
                text = data.decode("utf-8", "replace")
                text = text.replace("_mcp_pr2resolve_codec_template_20260915", escape(project_name))
                text = re.sub(r"<ProjectName>.*?</ProjectName>", lambda _: f"<ProjectName>{escape(project_name)}</ProjectName>", text, count=1)
                data = text.encode("utf-8")
            elif name == pool_name:
                text = data.decode("utf-8", "replace")
                text = re.sub(r"(?is)(<Sm2Timeline\b.*?<Name>).*?(</Name>)", lambda m: m[1] + escape(sequence_name) + m[2], text)
                text = re.sub(r"(?is)(<Sm2MpTimelineClip\b.*?<Name>).*?(</Name>)", lambda m: m[1] + escape(sequence_name) + m[2], text)
                text = text.replace("<Name>Unified Native Template</Name>", f"<Name>{escape(sequence_name)}</Name>").replace("<Name>Codec Native Template</Name>", f"<Name>{escape(sequence_name)}</Name>")
                data = text.encode("utf-8")
            target.writestr(name, data)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result.getvalue())
    return destination


def _export_native_project(
    sequences: list[ET.Element | ET.ElementTree],
    output_path: str | Path,
    project_name: str,
    grades: list[Any] | None = None,
    color_profiles: dict[str, Any] | None = None,
    bin_members: list[ProjectBin] | None = None,
) -> Path:
    """Combine native timeline packages into one Resolve project archive."""
    grade_sets = (grades if len(grades) == len(sequences) and all(isinstance(item, list) for item in grades)
                  else [None] * len(sequences)) if isinstance(grades, list) else [None] * len(sequences)
    with tempfile.TemporaryDirectory(prefix="pr2resolve-native-") as temp_dir:
        packages = [
            export_native_drp(item, Path(temp_dir) / f"timeline-{index}.drp", project_name, grade_sets[index], color_profiles)
            for index, item in enumerate(sequences)
        ]
        with zipfile.ZipFile(packages[0]) as first:
            members = {info.filename: first.read(info.filename) for info in first.infolist()}
        pool_name = "MediaPool/Master/MpFolder.xml"
        pool = members[pool_name].decode("utf-8", "replace")
        if "<MediaVec>" not in pool:
            raise ValueError("native project has no media pool vector")
        for package in packages[1:]:
            with zipfile.ZipFile(package) as archive:
                extra_pool = archive.read(pool_name).decode("utf-8", "replace")
                extra_project = archive.read("project.xml").decode("utf-8", "replace")
                extra_seq_name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                extra_seq = archive.read(extra_seq_name).decode("utf-8", "replace")
            timeline_match = re.search(r"(?s)<Element>\s*(<Sm2MpTimelineClip\b.*?</Sm2MpTimelineClip>)\s*</Element>", extra_pool)
            if timeline_match is None:
                raise ValueError("native timeline package has incomplete sequence topology")
            old_container = re.search(r"<Sm2SequenceContainer\b[^>]*\bDbId=\"([^\"]+)\"", extra_seq)
            if old_container is None:
                raise ValueError("native sequence container has no DbId")
            new_container, new_sequence = _uid(), _uid()
            new_timeline, new_timeline_clip = _uid(), _uid()
            new_sequence_unique, new_media_unique = _uid(), _uid()
            extra_seq = extra_seq.replace(old_container.group(1), new_container, 1)
            old_sequence = re.search(r"<Sequence>([^<]+)</Sequence>", extra_seq)
            if old_sequence is None:
                raise ValueError("native sequence container has no sequence reference")
            old_sequence_id = old_sequence.group(1)
            extra_seq = extra_seq.replace(old_sequence.group(1), new_sequence)
            extra_marker_blobs = marker_blob_elements(extra_project)
            if extra_marker_blobs:
                project_xml = members["project.xml"].decode("utf-8", "replace")
                for marker_blob in extra_marker_blobs:
                    project_xml = append_lockable_blob(
                        project_xml,
                        remap_marker_blob(marker_blob, old_sequence_id, new_sequence),
                    )
                members["project.xml"] = project_xml.encode("utf-8")
            timeline_block = timeline_match.group(1)
            timeline_block = re.sub(r'(<Sm2MpTimelineClip DbId=")[^"]+', rf'\g<1>{new_timeline_clip}', timeline_block, count=1)
            timeline_block = re.sub(r'(<UniqueMediaPoolItemId>)[^<]+', rf'\g<1>{new_media_unique}', timeline_block, count=1)
            timeline_block = re.sub(r'(<Sm2Timeline DbId=")[^"]+', rf'\g<1>{new_timeline}', timeline_block, count=1)
            timeline_block = re.sub(r'(<MpTimelineClip>)[^<]+', rf'\g<1>{new_timeline_clip}', timeline_block, count=1)
            timeline_block = re.sub(r'(<Sm2Sequence DbId=")[^"]+', rf'\g<1>{new_sequence}', timeline_block, count=1)
            timeline_block = re.sub(r'(<UniqueSequenceId>)[^<]+', rf'\g<1>{new_sequence_unique}', timeline_block, count=1)
            timeline_block = re.sub(r'(<Parent>)[^<]+', rf'\g<1>{new_timeline}', timeline_block, count=1)
            timeline_block = timeline_block.replace(
                old_container.group(1).encode("utf-16le").hex(),
                new_container.encode("utf-16le").hex(),
            )
            extra_media = re.search(r"(?s)<MediaVec>(.*?)</MediaVec>", extra_pool)
            if extra_media is None:
                raise ValueError("native timeline package has no media pool vector")
            extra_media_text = extra_media.group(1).replace(timeline_match.group(1), timeline_block, 1)
            pool = re.sub(r"(?s)(<MediaVec>.*?)(</MediaVec>)", lambda match: match.group(1) + "\n" + extra_media_text + match.group(2), pool, count=1)
            members[f"SeqContainer/{new_container}.xml"] = extra_seq.encode("utf-8")
        canonical_media: dict[tuple[str, ...], str] = {}
        media_refs: dict[str, str] = {}

        def keep_first_media(match: re.Match[str]) -> str:
            block = match.group(0)
            clip = match.group(1)
            tag = re.match(r"<(?P<tag>Sm2Mp(?:Video|Audio)Clip)\b", clip)
            item_id = re.search(r'\bDbId="([^"]+)"', clip)
            source = re.search(r"<Bt(?:Video|Audio)Info\b.*?<Clip>([^<]+)</Clip>", clip, re.S)
            if tag is None or item_id is None or source is None:
                return block
            fields = _protobuf_fields(_decode_wrapper_hex(source.group(1)))
            location = tuple(
                bytes(value).decode("utf-8", "replace")
                for number, wire, value in fields
                if wire == 2 and number in (1, 2)
            )
            if len(location) != 2:
                return block
            node = ET.fromstring(clip)
            key = (tag.group("tag"), _path_key("/".join(location)),
                   node.findtext(".//BtVideoInfo/Time") or "",
                   node.findtext(".//BtVideoInfo/Geometry") or "")
            if key not in canonical_media:
                canonical_media[key] = item_id.group(1)
                return block
            media_refs[item_id.group(1)] = canonical_media[key]
            return ""

        pool = re.sub(
            r"(?s)<Element>\s*(<Sm2Mp(?:Video|Audio)Clip\b.*?</Sm2Mp(?:Video|Audio)Clip>)\s*</Element>",
            keep_first_media,
            pool,
        )
        for name, data in list(members.items()):
            if not name.startswith("SeqContainer/"):
                continue
            text = data.decode("utf-8", "replace")
            for duplicate, canonical in media_refs.items():
                text = text.replace(f"<MediaRef>{duplicate}</MediaRef>", f"<MediaRef>{canonical}</MediaRef>")
            members[name] = text.encode("utf-8")
        members["project.xml"] = update_timeline_handles(members["project.xml"], pool)
        if bin_members:
            master = re.search(r'<Sm2MpFolder\b[^>]*\bDbId="([^"]+)".*?<MediaPool>([^<]+)</MediaPool>', pool, re.S)
            if master is None:
                raise ValueError("native project has no master media folder")
            master_id, media_pool_id = master.groups()
            item_pattern = r"(?s)<Element>\s*(<Sm2Mp(?:Video|Audio|Timeline)Clip\b.*?</Sm2Mp(?:Video|Audio|Timeline)Clip>)\s*</Element>"
            folder_blob = "0000000200000027800a061090d395a40d1890d395a40d2090d395a40d5a100a0c3030303030302030303030301200"
            folder_ids = {folder.uid: _uid() for folder in bin_members}
            folder_paths: dict[str, str] = {}
            bins_by_uid = {folder.uid: folder for folder in bin_members}
            sequence_bins = {uid: folder.uid for folder in bin_members for uid in folder.sequence_uids}
            media_bins: dict[str, list[str]] = {}
            for folder in bin_members:
                for path in folder.media_paths:
                    owners = media_bins.setdefault(_path_key(path), [])
                    if folder.uid not in owners:
                        owners.append(folder.uid)
            timeline_sources = {}
            timeline_blocks = re.findall(r"(?s)<Sm2MpTimelineClip\b.*?</Sm2MpTimelineClip>", pool)
            for source_tree, block in zip(sequences, timeline_blocks):
                source_root = source_tree.getroot() if isinstance(source_tree, ET.ElementTree) else source_tree
                source_sequence = (source_root.findall(".//sequence") or [source_root])[0]
                timeline_id = re.search(r'\bDbId="([^"]+)"', block)
                if timeline_id is not None:
                    timeline_sources[timeline_id[1]] = source_sequence.get("id", "")

            def bin_path(uid: str, visiting: frozenset[str] = frozenset()) -> str:
                if uid in visiting:
                    raise ValueError("cyclic Premiere bin hierarchy")
                if uid not in folder_paths:
                    folder = bins_by_uid[uid]
                    parent_path = bin_path(folder.parent_uid, visiting | {uid}) if folder.parent_uid in bins_by_uid else "MediaPool/Master"
                    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", folder.name)
                    folder_paths[uid] = f"{parent_path}/{len(folder_paths):03d}_{safe_name}"
                return folder_paths[uid]

            owned_items: dict[str, list[str]] = {folder.uid: [] for folder in bin_members}

            def assign_folder(match: re.Match[str]) -> str:
                block, item = match.group(0), match.group(1)
                node = ET.fromstring(item.replace("ListMgt::", "ListMgt__"))
                if node.tag == "Sm2MpTimelineClip":
                    owner = sequence_bins.get(timeline_sources.get(node.get("DbId", ""), ""))
                    owners = [owner] if owner is not None else []
                else:
                    source = node.find(".//BtVideoInfo/Clip")
                    if source is None:
                        source = node.find(".//BtAudioInfo/Clip")
                    location = {number: bytes(value).decode("utf-8") for number, wire, value in _protobuf_fields(_decode_wrapper_hex(source.text or "")) if wire == 2 and number in (1, 2)} if source is not None else {}
                    media_path = location.get(1, "").rstrip("/\\") + "/" + location.get(2, "")
                    owners = media_bins.get(_path_key(media_path), [])
                if not owners:
                    return block
                for index, owner in enumerate(owners):
                    owned_block = block
                    if index:
                        alias = ET.fromstring(ET.tostring(node))
                        alias.set("DbId", _uid())
                        _set_child(alias, "UniqueMediaPoolItemId", _uid())
                        _rewrite_av_clone(alias, media_path)
                        owned_block = "<Element>" + ET.tostring(alias, encoding="unicode") + "</Element>"
                    owned_items[owner].append(re.sub(r"(<MpFolder>)[^<]*(</MpFolder>)", lambda m: m[1] + folder_ids[owner] + m[2], owned_block, count=1))
                return ""

            pool = re.sub(item_pattern, assign_folder, pool)
            for folder in bin_members:
                folder_id, folder_name = folder_ids[folder.uid], folder.name
                moved = owned_items[folder.uid]
                folder_path = bin_path(folder.uid) + "/MpFolder.xml"
                members[folder_path] = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<Sm2MpFolder DbId="{folder_id}"><FieldsBlob>{folder_blob}</FieldsBlob>'
                    f'<Name>{escape(folder_name)}</Name><MpFolder>{folder_ids.get(folder.parent_uid, master_id)}</MpFolder>'
                    f'<UniqueMediaPoolItemId>{_uid()}</UniqueMediaPoolItemId><MediaVec>'
                    + "\n".join(moved)
                    + f'</MediaVec><MediaPool>{media_pool_id}</MediaPool><Folded>true</Folded>'
                    '<ColorTag>FOLDER_COLOR_NONE</ColorTag><LockSysId/><DbSavedTime>0</DbSavedTime></Sm2MpFolder>'
                ).encode("utf-8")
        members[pool_name] = pool.encode("utf-8")
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
            for name, data in members.items():
                target.writestr(name, data)
        return destination


__all__ = ["export_native_drp"]
