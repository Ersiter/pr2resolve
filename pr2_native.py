"""Write a codec-backed native Resolve package from one FCP7 sequence."""

from __future__ import annotations

import io
import base64
import json
import math
import os
import re
import struct
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

from pr2_constants import PP_LABEL_TO_RESOLVE_COLOR


TEMPLATE = Path(__file__).with_name("resources") / "resolve21_native_template.drp"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

# Resolve's static Basic Motion payloads captured from the clean Resolve 21
# template family.  They contain only transform data; source IDs and paths are
# not embedded.  Animated or otherwise unsupported values are rejected below.
_MOTION_PROFILES = {
    (100.0, 270.0): "00000002000000598128b52ffd20637d0200f4030a49080438144a0f082a1a0b0a09116abc74931804e23f4a0f082b004a004a004a0f082f00fcffffff7f56400a16082c381208085c1a040a0220034a004a00050035a5721780e43adb33c9730c",
    (146.0, 270.0): "00000002000000e88128b52ffd603b01ed0600140a0aef01080438144a0f082a1a0b0a09113e0ad7438941e43f4a0f082b004a0f08286d2fa1bd84f672bf4a0f082922efeeeeeeeeae3f4a9601082f00fcffffff7f5640528401ffffff804200000000000d003f5840697f562ac04a0ab602087638204a08083b1a040a0220024a9901083c529401ffffff7035e803e0bf4a8901083d0d009a9999999999e93fe803f0406fc00a0e082c38124a004a004a004a004a001900a1cc10f0400bc0452007e01006405a590381b30409011e306e54c09e336e5030381c828ba6842367824c19755e53b37d9a926c5e1a92a713",
}
_MOTION_PROFILE_GEOMETRY = (1920, 1080, 1080, 1920)

def _grade_manifest(grade: Any) -> dict[str, Any]:
    records = []
    for record in getattr(grade, "parameter_records", []) or []:
        records.append({
            "id": record.parameter_id,
            "name": record.name,
            "control_type": record.control_type,
            "encoding": record.encoding,
            "raw_value": record.raw_value,
            "lower_bound": record.lower_bound,
            "upper_bound": record.upper_bound,
            "is_time_varying": record.is_time_varying,
            "start_keyframe": record.start_keyframe,
            "start_keyframe_position": record.start_keyframe_position,
            "start_keyframe_value": record.start_keyframe_value,
            "binary_hash": record.binary_hash,
            "decoded_sha256": record.decoded_sha256,
            "representation": "manifest_only_native",
        })
    return {
        "clip": grade.clip_name,
        "track": grade.track_index,
        "start": grade.start_frame,
        "end": grade.end_frame,
        "track_item_id": grade.track_item_id,
        "parameters": records,
    }


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
        rate = float(node.findtext("rate/timebase") or "24")
        return rate * 1000 / 1001 if (node.findtext("rate/ntsc") or "").strip().upper() == "TRUE" else rate
    except ValueError:
        return 24.0


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


def _source_position_code(source_in: int, source_fps: float, timeline_fps: float) -> str:
    if source_in <= 0 or source_fps <= 0 or timeline_fps <= 0:
        return ""
    mapped = source_in / source_fps * timeline_fps
    whole, fraction = int(mapped), mapped - int(mapped)
    return str(whole) if abs(fraction) < 1e-12 else f"{whole}|{struct.pack('<d', fraction).hex()}"


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


def _rewrite_av_clone(node: ET.Element, full_path: str, preserve_audio_identity: bool = False, duration: int = 0, fps: float = 0.0, width: int = 0, height: int = 0) -> ET.Element:
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
        if info.tag != "BtAudioInfo" or preserve_audio_identity:
            continue
        old_id = info.get("DbId") or ""
        audio_id = _uid()
        info.set("DbId", audio_id)
        root_blob = node.find("FieldsBlob")
        if root_blob is None or not root_blob.text or not old_id:
            continue
        raw = _decode_wrapper_hex(root_blob.text)
        old_bytes, new_bytes = old_id.encode("utf-16le"), audio_id.encode("utf-16le")
        if old_bytes not in raw:
            continue
        raw = raw.replace(old_bytes, new_bytes)
        if b"H\x00" + new_bytes not in raw:
            raise ValueError("audio MediaRef missing after DbId rewrite")
        root_blob.text = _encode_wrapper_hex(raw)
    if duration > 0 or fps > 0 or width > 0 or height > 0:
        for info in node.findall(".//BtVideoInfo"):
            time_node = info.find("Time")
            if time_node is not None and time_node.text:
                original = bytes.fromhex(time_node.text)
                raw = bytearray(original[8:])
                marker = "NumFrames".encode("utf-16le")
                start = raw.find(marker)
                if start >= 0 and duration > 0:
                    raw[start + len(marker) + 4:start + len(marker) + 8] = duration.to_bytes(4, "big")
                marker = "FrameRate".encode("utf-16le")
                start = raw.find(marker)
                if start >= 0 and fps > 0:
                    raw[start + len(marker) + 8:start + len(marker) + 16] = struct.pack("<d", fps)
                time_node.text = (original[:8] + bytes(raw)).hex()
            geometry = info.find("Geometry")
            if geometry is not None and geometry.text and width > 0 and height > 0:
                original = bytes.fromhex(geometry.text)
                raw = bytearray(original[8:])
                marker = "Resolution".encode("utf-16le")
                start = raw.find(marker)
                if start >= 0:
                    offset = start + len(marker) + 8
                    raw[offset:offset + 8] = width.to_bytes(8, "big")
                    raw[offset + 8:offset + 16] = height.to_bytes(8, "big")
                    geometry.text = (original[:8] + bytes(raw)).hex()
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


def _rewrite_still_clone(node: ET.Element, full_path: str, preserve_audio_identity: bool = False, duration: int = 0, fps: float = 0.0, width: int = 0, height: int = 0) -> ET.Element:
    return _rewrite_av_clone(node, full_path, preserve_audio_identity, duration, fps, width, height)


def _rewrite_audio_clone(node: ET.Element, full_path: str, preserve_audio_identity: bool = False, duration: int = 0, fps: float = 0.0) -> ET.Element:
    return _rewrite_av_clone(node, full_path, preserve_audio_identity, duration, fps)


def _static_motion_payload(clip: ET.Element, source_geometry: tuple[int, int], timeline_geometry: tuple[int, int]) -> str | None:
    """Return a known static Basic Motion payload, or reject unsupported motion."""
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
        if parameter_id in ("scale", "rotation"):
            try:
                values[parameter_id] = float(value.text or "")
            except ValueError:
                raise ValueError(f"invalid Basic Motion {parameter_id}: {clip.findtext('name') or 'clip'}")
        elif parameter_id == "center":
            horiz, vert = value.findtext("horiz"), value.findtext("vert")
            if (horiz and float(horiz) != 0.0) or (vert and float(vert) != 0.0):
                raise ValueError(f"non-default Basic Motion center is unsupported: {clip.findtext('name') or 'clip'}")
    for effect in clip.findall("filter/effect"):
        if (effect.findtext("effectid") or "").strip().lower() != "opacity":
            continue
        value = next((p.find("value") for p in effect.findall("parameter") if p.findtext("parameterid") == "opacity"), None)
        if value is not None and value.text and float(value.text) != 100.0:
            raise ValueError(f"non-default opacity is unsupported: {clip.findtext('name') or 'clip'}")
    scale, rotation = values.get("scale", 100.0), values.get("rotation", 0.0)
    if scale == 100.0 and rotation == 0.0:
        return None
    if (*source_geometry, *timeline_geometry) != _MOTION_PROFILE_GEOMETRY:
        raise ValueError(f"unsupported Basic Motion geometry {source_geometry}->{timeline_geometry}: {clip.findtext('name') or 'clip'}")
    try:
        return _MOTION_PROFILES[(scale, rotation)]
    except KeyError as exc:
        raise ValueError(f"unsupported static Basic Motion ({scale:g}, {rotation:g}): {clip.findtext('name') or 'clip'}") from exc


def _validate_sequence_capabilities(root: ET.Element) -> None:
    """Reject FCP structures that the native writer cannot represent faithfully."""
    problems: list[str] = []
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
                    print(f"  Warning: {name}: Lumetri is preserved in the manifest; native grade is not applied")
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
                elif variable != 0:
                    problems.append(f"{name}: variable retime")
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


def _build_timeline(sequence: ET.Element | ET.ElementTree, grades: list[Any] | None = None) -> tuple[dict[str, bytes], ET.Element]:
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
    files, video_ids = {}, set()
    for branch in ("video", "audio"):
        for track in source.findall(f"media/{branch}/track"):
            for clip in track.findall("clipitem"):
                file_node = clip.find("file")
                if file_node is None or not file_node.get("id"):
                    continue
                key = file_node.get("id") or ""
                files.setdefault(key, file_node)
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
    clip_labels = _timeline_clip_labels(_timeline_state_manifest(root))
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
                input_fps = _rate(file_node) if file_node is not None else _rate(source)
                _set_child(node, "In", "86400" if suffix in IMAGE_SUFFIXES else _source_position_code(_int_text(clip, "in"), input_fps, _rate(source)))
                if file_node is not None:
                    file_id = file_node.get("id") or ""
                    _set_child(node, "MediaRef", refs.get(file_id, ""))
                    _set_child(node, "MediaFilePath", _native_path(file_node.findtext("pathurl") or ""))
                    speed = clip_speed(clip)
                    if abs(speed - 100.0) > 1e-9:
                        source_span = _int_text(clip, "out") - _int_text(clip, "in") + 1
                        seconds = max(source_span, 1) / _rate(file_node)
                    else:
                        seconds = (source_frames.get(file_id, 0) + 1) / _rate(file_node)
                    if kind == "video":
                        _set_child(node, "CurrentSelectorIdx", "0")
                        _set_child(node, "MediaFrameRate", struct.pack("<d", input_fps).hex() + "00" * 8)
                        outer = struct.pack(">d", seconds) + b"\0" * 8
                        middle = struct.pack(">d", seconds - 1 / 120) + b"\0" * 8
                        tail = struct.pack(">d", seconds)
                        _set_child(node, "MediaTimemapBA", "02" + (outer + middle + tail).hex())
                    else:
                        _set_child(node, "MediaTimemapBA", "02" + struct.pack(">d", seconds).hex())
                motion_payload = None
                if kind == "video":
                    sample = file_node.find("media/video/samplecharacteristics") if file_node is not None else None
                    source_geometry = (
                        _int_text(sample, "width") if sample is not None else 0,
                        _int_text(sample, "height") if sample is not None else 0,
                    )
                    motion_payload = _static_motion_payload(clip, source_geometry, timeline_geometry)
                if motion_payload is not None:
                    _set_child(node, "EffectFiltersBA", motion_payload)
                label_key = (
                    kind, track_index, clip.findtext("name") or "",
                    _int_text(clip, "start"), int(_clip_duration(clip)),
                )
                if label_key in clip_labels:
                    _set_native_clip_color(node, clip_labels[label_key])
                item = ET.SubElement(items, "Element")
                item.append(node)
            for field in ("FusionCompHolderItems", "UserDefinedName", "LayersVec"):
                _set_child(track, field, "")
            wrapper.append(track)
    members[seq_name] = _xml(native)
    return members, source


def export_native_drp(
    sequence: ET.Element | ET.ElementTree | list[ET.Element | ET.ElementTree],
    output_path: str | Path,
    project_name: str,
    grades: list[Any] | None = None,
    color_profiles: dict[str, Any] | None = None,
) -> Path:
    """Export one project package, optionally containing multiple sequences."""
    if isinstance(sequence, list):
        if not sequence:
            raise ValueError("native DRP requires at least one sequence")
        return _export_native_project(sequence, output_path, project_name, grades, color_profiles)
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"codec DRP template not found: {TEMPLATE}")
    root = sequence.getroot() if isinstance(sequence, ET.ElementTree) else sequence
    _validate_sequence_capabilities(root)
    timeline_state = _timeline_state_manifest(root)
    members, source = _build_timeline(sequence, grades)
    pool_name = "MediaPool/Master/MpFolder.xml"
    with zipfile.ZipFile(TEMPLATE) as archive:
        pool = archive.read(pool_name).decode("utf-8", "replace")
    video_templates = re.findall(r"(?is)<Element>\s*(<Sm2MpVideoClip\b.*?</Sm2MpVideoClip>)\s*</Element>", pool)
    audio_templates = re.findall(r"(?is)<Element>\s*(<Sm2MpAudioClip\b.*?</Sm2MpAudioClip>)\s*</Element>", pool)
    audio_fallback = audio_templates[0] if audio_templates else ""
    audio_by_ext = {Path(re.search(r"(?is)<Name>(.*?)</Name>", n).group(1)).suffix.lower(): n for n in audio_templates if re.search(r"(?is)<Name>neutral-audio\.[^<]+</Name>", n)}
    files, video_ids = {}, set()
    for branch in ("video", "audio"):
        for track in source.findall(f"media/{branch}/track"):
            for clip in track.findall("clipitem"):
                file_node = clip.find("file")
                if file_node is None or not file_node.get("id"):
                    continue
                key = file_node.get("id") or ""
                files.setdefault(key, file_node)
                if branch == "video":
                    video_ids.add(key)
    refs = {}
    for name, data in members.items():
        if name.startswith("SeqContainer/"):
            xml = ET.fromstring(data)
            for clip in xml.iter():
                if clip.tag in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
                    refs[(clip.findtext("Name") or "", clip.findtext("MediaFilePath") or "")] = clip.findtext("MediaRef") or ""
    def path_key(path: str, key: str) -> str:
        return path.casefold() if path else f"@{key}"
    video_paths = {path_key(_native_path(node.findtext("pathurl") or ""), key) for key, node in files.items() if key in video_ids}
    canonical, by_name, by_path = {}, {}, {}
    generated = []
    for key, file_node in files.items():
        path, name = _native_path(file_node.findtext("pathurl") or ""), file_node.findtext("name") or "media"
        media_key = path_key(path, key)
        if media_key in canonical:
            continue
        is_video, suffix = key in video_ids or media_key in video_paths, Path(name).suffix.lower()
        if is_video:
            chosen = (next((n for n in video_templates if "neutral-still.png" in n), video_templates[0]) if suffix in IMAGE_SUFFIXES else next((n for n in video_templates if "neutral-source.mp4" in n), video_templates[0]))
        else:
            chosen = audio_by_ext.get(suffix, audio_by_ext.get(".wav", audio_fallback))
        if not chosen:
            raise ValueError("codec template lacks required media node")
        item_id = refs.get((name, path)) or next((ref for (ref_name, ref_path), ref in refs.items() if ref and path_key(ref_path, key) == media_key), _uid())
        canonical[media_key] = item_id; by_name.setdefault(name.casefold(), item_id); by_path.setdefault(name.casefold(), path)
        node = ET.fromstring(chosen); node.set("DbId", item_id)
        unique = node.find("UniqueMediaPoolItemId")
        if unique is not None:
            unique.text = _uid()
        _set_child(node, "Name", name)
        sample = file_node.find("media/video/samplecharacteristics")
        duration = _int_text(file_node, "duration")
        fps = _rate(file_node)
        width = _int_text(sample, "width") if sample is not None else 0
        height = _int_text(sample, "height") if sample is not None else 0
        if is_video and suffix in IMAGE_SUFFIXES:
            _rewrite_still_clone(node, path, duration=duration, fps=fps, width=width, height=height)
        elif is_video:
            _rewrite_av_clone(node, path, duration=duration, fps=fps, width=width, height=height)
        else:
            _rewrite_audio_clone(node, path, preserve_audio_identity=suffix == ".mp3", duration=duration, fps=fps)
        generated.append("   <Element>\n" + ET.tostring(node, encoding="unicode") + "\n   </Element>")
    for name, data in list(members.items()):
        if not name.startswith("SeqContainer/"):
            continue
        xml = ET.fromstring(data)
        for clip in xml.iter():
            if clip.tag not in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
                continue
            clip_name, clip_path = clip.findtext("Name") or "", clip.findtext("MediaFilePath") or ""
            media_id = canonical.get(path_key(clip_path, clip_name)) or by_name.get(clip_name.casefold())
            if media_id:
                _set_child(clip, "MediaRef", media_id)
                if not clip_path:
                    _set_child(clip, "MediaFilePath", by_path.get(clip_name.casefold(), ""))
        members[name] = _xml(xml)
    pool = re.sub(r"(?is)<Element>\s*<(?:Sm2MpVideoClip|Sm2MpAudioClip)\b.*?</(?:Sm2MpVideoClip|Sm2MpAudioClip)>\s*</Element>", "", pool)
    pool = re.sub(r"(?is)(<MediaVec>)(.*?)(</MediaVec>)", lambda m: m.group(1) + m.group(2) + "\n" + "\n".join(generated) + m.group(3), pool, count=1)
    members[pool_name] = pool.encode("utf-8")
    color_plan = _configure_color(members, color_profiles)
    members["pr2/conversion.json"] = json.dumps({
        "format": 1,
        "color": color_plan,
        "lumetri": [_grade_manifest(grade) for grade in (grades or [])],
        "timeline_state": timeline_state,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sequence_name = source.findtext("name") or project_name
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target:
        for name, data in members.items():
            if name == "project.xml":
                text = data.decode("utf-8", "replace")
                text = text.replace("_mcp_pr2resolve_codec_template_20260915", project_name)
                text = re.sub(r"<ProjectName>.*?</ProjectName>", f"<ProjectName>{project_name}</ProjectName>", text, count=1)
                data = text.encode("utf-8")
            elif name == pool_name:
                text = data.decode("utf-8", "replace")
                text = re.sub(r"(?is)(<Sm2Timeline\b.*?<Name>).*?(</Name>)", rf"\g<1>{sequence_name}\g<2>", text)
                text = text.replace("<Name>Unified Native Template</Name>", f"<Name>{sequence_name}</Name>").replace("<Name>Codec Native Template</Name>", f"<Name>{sequence_name}</Name>")
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
        pool = ET.fromstring(members[pool_name])
        media_vec = pool.find("MediaVec")
        if media_vec is None:
            raise ValueError("native project has no media pool vector")
        project = ET.fromstring(members["project.xml"])
        conversion_manifests = []
        for package in packages:
            with zipfile.ZipFile(package) as archive:
                conversion_manifests.append(json.loads(archive.read("pr2/conversion.json")))
        for package in packages[1:]:
            with zipfile.ZipFile(package) as archive:
                extra_pool = ET.fromstring(archive.read(pool_name))
                extra_seq_name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                extra_seq = archive.read(extra_seq_name).decode("utf-8", "replace")
            timeline_clip = extra_pool.find(".//Sm2MpTimelineClip")
            timeline = extra_pool.find(".//Sm2Timeline")
            nested_sequence = timeline.find("Sequence/Sm2Sequence") if timeline is not None else None
            if timeline_clip is None or timeline is None or nested_sequence is None:
                raise ValueError("native timeline package has incomplete sequence topology")
            old_container = re.search(r"<Sm2SequenceContainer\b[^>]*\bDbId=\"([^\"]+)\"", extra_seq)
            if old_container is None:
                raise ValueError("native sequence container has no DbId")
            old_sequence = nested_sequence.get("DbId") or ""
            new_container, new_sequence = _uid(), _uid()
            new_timeline, new_timeline_clip = _uid(), _uid()
            new_sequence_unique, new_media_unique = _uid(), _uid()
            extra_seq = extra_seq.replace(old_container.group(1), new_container, 1)
            if old_sequence:
                extra_seq = re.sub(r"(<Sequence>)" + re.escape(old_sequence) + r"(</Sequence>)", rf"\g<1>{new_sequence}\g<2>", extra_seq)
            nested_sequence.set("DbId", new_sequence)
            sequence_unique = nested_sequence.find("UniqueSequenceId")
            if sequence_unique is not None:
                sequence_unique.text = new_sequence_unique
            if nested_sequence.find("Parent") is not None:
                nested_sequence.find("Parent").text = new_timeline
            timeline.set("DbId", new_timeline)
            timeline.find("MpTimelineClip").text = new_timeline_clip
            timeline_clip.set("DbId", new_timeline_clip)
            unique_media = timeline_clip.find("UniqueMediaPoolItemId")
            if unique_media is not None:
                unique_media.text = new_media_unique
            for element in extra_pool.find("MediaVec") or []:
                media_vec.append(element)
            members.pop(extra_seq_name, None)
            members[f"SeqContainer/{new_container}.xml"] = extra_seq.encode("utf-8")
        members[pool_name] = _xml(pool)
        merged_conversion = conversion_manifests[0]
        merged_conversion["lumetri"] = [grade for item in conversion_manifests for grade in item.get("lumetri", [])]
        merged_conversion["timeline_state"] = [item.get("timeline_state", {}) for item in conversion_manifests]
        merged_conversion["timelines"] = conversion_manifests
        members["pr2/conversion.json"] = json.dumps(merged_conversion, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        members["project.xml"] = _xml(project)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target:
            for name, data in members.items():
                target.writestr(name, data)
        return destination


__all__ = ["export_native_drp"]
