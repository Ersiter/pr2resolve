"""Write a codec-backed native Resolve package from one FCP7 sequence."""

from __future__ import annotations

import io
import math
import os
import re
import struct
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse
from xml.etree import ElementTree as ET

TEMPLATE = Path(__file__).with_name("resources") / "resolve21_native_template.drp"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

_VIDEO_TRANSITIONS = {
    "cross dissolve": (
        "Cross Dissolve",
        "000000020000001b8012180000002c789c636660640001b6552273180800002fb40165",
        "000000020000003a800a37081238024a004a004a004a004a29088b015224ffffffe00000000000000c0000000000000000001c00000000000c00000000000000f03f",
    ),
    "non-additive dissolve": (
        "Non-Additive Dissolve",
        "0000000200000028800a0b1a09080410b0a991f5df0212180000002c789c636660640001b6552273180800002fb40165",
        "0000000200000038800a35086838024a004a004a004a29088b015224ffffffe00000000000000c0000000000000000001c00000000000c00000000000000f03f",
    ),
    "dip to color dissolve": (
        "Dip To Color Dissolve",
        "000000020000001b8012180000002c789c636660640001b6552273180800002fb40165",
        "000000020000003a800a37081638024a004a004a004a004a29088b015224ffffffe00000000000000c0000000000000000001c00000000000c00000000000000f03f",
    ),
}
_AUDIO_TRANSITION = (
    "Cross Fade 0DB",
    "0000000200000021800a04a801cf0512180000002c789c636660640001b69587341908000037da019f",
    "",
)


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


def _transition_node(transition: ET.Element, media_type: str) -> ET.Element:
    """Translate a common FCP transition into a native Resolve transition."""
    effect_name = (transition.findtext("effect/name") or "").strip().lower()
    display_name = (transition.findtext("name") or "").strip()
    if media_type == "audio":
        spec = _AUDIO_TRANSITION
    else:
        spec = next((value for key, value in _VIDEO_TRANSITIONS.items() if key in effect_name), None)
        if spec is None:
            raise ValueError(f"unsupported video transition: {display_name or effect_name or 'unnamed'}")
    node = ET.Element("Sm2TiTransition", {"DbId": _uid()})
    ET.SubElement(node, "FieldsBlob").text = spec[1]
    ET.SubElement(node, "PrettyType").text = spec[0]
    ET.SubElement(node, "Name").text = display_name or spec[0]
    start = _int_text(transition, "start")
    end = _int_text(transition, "end")
    ET.SubElement(node, "Start").text = str(start)
    ET.SubElement(node, "Duration").text = str(max(0, end - start))
    for field, value in (("LinkedItemSync", ""), ("WasDisbanded", "false"), ("MarkersBA", ""),
                         ("UiMemento", "0"), ("Flags", "0"), ("PriorityIndex", "0"),
                         ("EffectFiltersBA", spec[2]), ("ImportExportMetadataBA", ""),
                         ("RenderTextEnabled", "true"), ("RenderTextGanged", "true"),
                         ("RenderTextPrefixed", "true")):
        ET.SubElement(node, field).text = value
    alignment = (transition.findtext("alignment") or "center").strip().lower()
    alignment_type = {"start": "1", "center": "2", "end": "3", "start-black": "1", "end-black": "3"}.get(alignment, "2")
    ET.SubElement(node, "AlignmentType").text = alignment_type
    ET.SubElement(node, "Position").text = "2"
    return node


def _build_timeline(sequence: ET.Element | ET.ElementTree) -> tuple[dict[str, bytes], ET.Element]:
    root = sequence.getroot() if isinstance(sequence, ET.ElementTree) else sequence
    source = (root.findall(".//sequence") or [root])[0]
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
    refs = {key: _uid() for key in files}
    video_template = next((n for n in native.iter("Sm2TiVideoClip") if n.findtext("Name") == "neutral-source.mp4"), native.find(".//Sm2TiVideoClip"))
    still_template = next((n for n in native.iter("Sm2TiVideoClip") if n.findtext("Name") == "neutral-still.png"), video_template)
    audio_templates = {Path(n.findtext("Name") or "").suffix.lower(): n for n in native.iter("Sm2TiAudioClip") if (n.findtext("Name") or "").startswith("neutral-audio.")}
    audio_template = native.find(".//Sm2TiAudioClip")
    audio_only_template = audio_templates.get(".wav", audio_template)
    if video_template is None or still_template is None or audio_template is None or audio_only_template is None:
        raise ValueError("codec template lacks required timeline nodes")
    tracks = {kind: ET.fromstring(ET.tostring(native.find(f"{vec}/Element/Sm2TiTrack"))) for vec, kind in (("VideoTrackVec", "video"), ("AudioTrackVec", "audio"))}
    for vector_name, kind, generic in (("VideoTrackVec", "video", video_template), ("AudioTrackVec", "audio", audio_template)):
        vector = native.find(vector_name)
        if vector is None or tracks[kind] is None:
            raise ValueError("codec template lacks required track")
        vector.clear()
        for fcp_track in source.findall(f"media/{kind}/track"):
            wrapper, track = ET.SubElement(vector, "Element"), ET.fromstring(ET.tostring(tracks[kind]))
            track.set("DbId", _uid())
            items = track.find("Items")
            if items is None:
                items = ET.SubElement(track, "Items")
            items.clear()
            for clip in fcp_track:
                if clip.tag == "transitionitem":
                    media_type = "video" if kind == "video" else "audio"
                    item = ET.SubElement(items, "Element")
                    item.append(_transition_node(clip, media_type))
                    continue
                if clip.tag != "clipitem":
                    continue
                file_node = clip.find("file")
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
                item = ET.SubElement(items, "Element")
                item.append(node)
            for field in ("FusionCompHolderItems", "UserDefinedName", "LayersVec"):
                _set_child(track, field, "")
            wrapper.append(track)
    members[seq_name] = _xml(native)
    return members, source


def export_native_drp(sequence: ET.Element | ET.ElementTree, output_path: str | Path, project_name: str) -> Path:
    """Export one FCP7 sequence using the verified codec native template."""
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"codec DRP template not found: {TEMPLATE}")
    members, source = _build_timeline(sequence)
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


__all__ = ["export_native_drp"]
