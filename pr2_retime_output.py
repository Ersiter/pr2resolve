"""Patch shared retime and mark fields in an API-exported Resolve ZIP."""

from __future__ import annotations

import copy
import math
import os
import tempfile
import zipfile
from pathlib import Path
from xml.parsers import expat
from xml.etree import ElementTree as ET

from pr2_constants import IMAGE_SUFFIXES
from pr2_native_project import update_sequence_marks
from pr2_native_state import sequence_marks
from pr2_native_time import AUDIO_RETIME_WARNING, STILL_SOURCE_IN, build_media_timemap, build_still_timemap, extract_fcp7_retime_alignment, retimed_source_in


def _local(tag: str) -> str:
    return tag.rsplit("::", 1)[-1].rsplit("__", 1)[-1]


def _fps(node: ET.Element | None, fallback: float | None = None) -> float:
    if node is None or node.find("./rate") is None:
        if fallback is None:
            raise ValueError("frame rate is missing")
        value = fallback
    else:
        rate = node.find("./rate")
        try:
            value = float(rate.findtext("./timebase") or "")
        except ValueError as exc:
            raise ValueError("frame rate is invalid") from exc
        if (rate.findtext("./ntsc") or "").strip().upper() == "TRUE":
            value *= 1000 / 1001
    if not math.isfinite(value) or value <= 0:
        raise ValueError("frame rate must be positive and finite")
    return value


def _frame_count(value: str | None, label: str) -> int:
    try:
        number = float(value or "")
    except ValueError as exc:
        raise ValueError(f"{label} is missing or invalid") from exc
    if not math.isfinite(number) or not number.is_integer() or number < 1:
        raise ValueError(f"{label} must be a positive integral frame count")
    return int(number)


def _clip_start_duration(clip: ET.Element) -> tuple[int, int]:
    try:
        start, end = int(clip.findtext("./start") or "0"), int(clip.findtext("./end") or "0")
        duration = max(0, end - start) if end or start else int(clip.findtext("./duration") or "0")
    except ValueError as exc:
        raise ValueError("source clip start/duration is invalid") from exc
    if duration <= 0:
        raise ValueError("source clip duration must be positive")
    return start, duration


def _retime_effects(clip: ET.Element) -> list[ET.Element]:
    return [e for e in clip.findall("./filter/effect") if (e.findtext("./effectid") or "").strip().lower() == "timeremap"]


def _is_retime(effect: ET.Element) -> tuple[bool, float, ET.Element | None]:
    params: dict[str, ET.Element] = {}
    for param in effect.findall("./parameter"):
        key = (param.findtext("./parameterid") or "").strip()
        if key in params:
            raise ValueError(f"ambiguous Time Remap {key} parameter")
        params[key] = param
    try:
        speed = float(params.get("speed").findtext("./value")) if params.get("speed") is not None else 100.0
    except (TypeError, ValueError) as exc:
        raise ValueError("Time Remap speed is invalid") from exc
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("reverse or non-positive Time Remap is unsupported")
    reverse = (params.get("reverse").findtext("./value") if params.get("reverse") is not None else "FALSE") or ""
    if reverse.strip().lower() in ("true", "1"):
        raise ValueError("reverse Time Remap is unsupported")
    graph = params.get("graphdict")
    has_graph = graph is not None and graph.find("./keyframe") is not None
    variable = params.get("variablespeed")
    variable_value = (variable.findtext("./value") or "").strip().lower() if variable is not None else ""
    if variable is not None and variable_value not in ("0", "1", "false", "true"):
        raise ValueError("Time Remap variablespeed flag is ambiguous")
    is_variable = variable_value in ("1", "true")
    if (is_variable or speed != 100.0) and not has_graph:
        if is_variable:
            raise ValueError("variable Time Remap has no graphdict")
        return True, speed, None
    return has_graph, speed, graph if has_graph else None


def _file_definitions(root: ET.Element) -> dict[str, ET.Element]:
    definitions: dict[str, ET.Element] = {}
    for node in root.iter("file"):
        file_id = node.get("id")
        if not file_id:
            continue
        score = sum(node.find(path) is not None for path in ("./rate", "./duration", "./pathurl"))
        previous = definitions.get(file_id)
        previous_score = sum(previous.find(path) is not None for path in ("./rate", "./duration", "./pathurl")) if previous is not None else -1
        if score > previous_score:
            definitions[file_id] = node
    return definitions


def _source_plans(root: ET.Element) -> list[dict[str, object]]:
    sequence = root.find("./sequence")
    if not any(_retime_effects(clip) for clip in root.iter("clipitem")):
        return []
    if sequence is None:
        raise ValueError("source XML has no sequence")
    media = sequence.find("./media")
    if media is None:
        return []
    candidates: list[tuple[str, int, ET.Element, float, ET.Element | None]] = []
    for kind in ("video", "audio"):
        for track_index, track in enumerate(media.findall(f"./{kind}/track"), 1):
            for clip in track.findall("./clipitem"):
                effects = _retime_effects(clip)
                if len(effects) > 1:
                    raise ValueError("multiple Time Remap effects on one clip are unsupported")
                if effects:
                    retimed, speed, graph = _is_retime(effects[0])
                    if retimed:
                        candidates.append((kind, track_index, clip, speed, graph))
    if not candidates:
        return []
    timeline_fps = _fps(sequence)
    files = _file_definitions(root)
    sequence_name = sequence.findtext("./name") or ""
    if not sequence_name.strip():
        raise ValueError("retimed source sequence has no name for Resolve timeline matching")
    plans: list[dict[str, object]] = []
    for kind, track_index, clip, speed, graph in candidates:
        clip_rate = _fps(clip, timeline_fps)
        file_node = clip.find("./file")
        file_id = file_node.get("id") if file_node is not None else None
        full_file = files.get(file_id or "")
        source_info = full_file if full_file is not None else file_node
        if source_info is None:
            raise ValueError("retimed clip has no resolvable source file")
        source_fps = _fps(source_info)
        source_frames = _frame_count(source_info.findtext("./duration"), "source file duration")
        start, duration = _clip_start_duration(clip)
        still = Path(source_info.findtext("./name") or "").suffix.lower() in IMAGE_SUFFIXES
        if graph is not None:
            alignment = extract_fcp7_retime_alignment(clip, timeline_fps)
            points = alignment.get("points")
            if not isinstance(points, tuple):
                raise ValueError("graphdict points were not decoded")
            in_value = STILL_SOURCE_IN if still else str(alignment["encoded_in"])
            timemap = build_still_timemap() if still else build_media_timemap(source_frames, source_fps, audio=kind == "audio", keyframes=points)
        else:
            try:
                source_in = int(clip.findtext("./in") or "")
            except ValueError as exc:
                raise ValueError("constant-speed source In is invalid") from exc
            in_value = STILL_SOURCE_IN if still else retimed_source_in(source_in, clip_rate, timeline_fps, speed)
            timemap = build_still_timemap() if still else build_media_timemap(source_frames, source_fps, speed, audio=kind == "audio")
        if kind == "audio":
            print(f"  Warning: {clip.findtext('name') or 'clip'}: {AUDIO_RETIME_WARNING}")
        plans.append({"kind": kind, "track": track_index, "start": start, "duration": duration, "in": in_value, "timemap": timemap, "sequence_name": sequence_name})
    return plans


def _xml_safe(data: bytes) -> bytes:
    return data.replace(b"<ListMgt::", b"<ListMgt__").replace(b"</ListMgt::", b"</ListMgt__")


def _tag_end(data: bytes, offset: int) -> int:
    quote = 0
    for i in range(offset, len(data)):
        byte = data[i]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in (34, 39):
            quote = byte
        elif byte == 62:
            return i + 1
    raise ValueError("malformed XML start tag")


def _field_spans(data: bytes) -> dict[str, dict[str, tuple[int, int, int, bytes]]]:
    safe = _xml_safe(data)
    clips: dict[str, dict[str, tuple[int, int, int, bytes]]] = {}
    stack: list[dict[str, object]] = []
    parser = expat.ParserCreate()

    def start(name: str, attrs: dict[str, str]) -> None:
        parent = stack[-1] if stack else None
        frame: dict[str, object] = {"start": parser.CurrentByteIndex, "open_end": _tag_end(safe, parser.CurrentByteIndex), "clip": None}
        if _local(name) in ("Sm2TiVideoClip", "Sm2TiAudioClip"):
            dbid = attrs.get("DbId")
            if dbid:
                if dbid in clips:
                    raise ValueError(f"duplicate native clip DbId {dbid}")
                frame["clip"] = dbid
                clips[dbid] = {}
        elif parent and parent.get("clip") and name in ("In", "MediaTimemapBA"):
            frame["clip"] = parent["clip"]
            frame["field"] = name
        stack.append(frame)

    def end(name: str) -> None:
        frame = stack.pop()
        dbid, field = frame.get("clip"), frame.get("field")
        if dbid and field:
            if str(field) in clips[str(dbid)]:
                raise ValueError(f"duplicate native clip {dbid} field {field}")
            begin = int(frame["start"])
            open_end = int(frame["open_end"])
            close_start = parser.CurrentByteIndex
            clips[str(dbid)][str(field)] = (begin, open_end, close_start, safe[begin:open_end])

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(safe, True)
    except expat.ExpatError as exc:
        raise ValueError(f"Resolve sequence XML is not parseable: {exc}") from exc
    return clips


def _timeline_sequence_id(contents: list[bytes], infos: list[zipfile.ZipInfo], sequence_name: str) -> str:
    sequence_ids: set[str] = set()
    for info, data in zip(infos, contents):
        if not info.filename.startswith("MediaPool/") or not info.filename.lower().endswith(".xml"):
            continue
        try:
            root = ET.fromstring(_xml_safe(data))
        except ET.ParseError as exc:
            raise ValueError(f"unparseable MediaPool XML {info.filename}") from exc
        for node in root.iter():
            if _local(node.tag) != "Sm2MpTimelineClip":
                continue
            name = node.findtext("./Name") or ""
            if name != sequence_name:
                continue
            timelines = [child for child in node.iter() if _local(child.tag) == "Sm2Timeline"]
            if len(timelines) != 1:
                raise ValueError(f"timeline {sequence_name!r} has an ambiguous Sm2Timeline reference")
            sequence_nodes = [child for child in timelines[0].iter() if _local(child.tag) == "Sm2Sequence"]
            if len(sequence_nodes) != 1 or not sequence_nodes[0].get("DbId"):
                raise ValueError(f"timeline {sequence_name!r} has no unique Sm2Sequence DbId")
            sequence_ids.add(sequence_nodes[0].get("DbId", ""))
    if len(sequence_ids) != 1:
        raise ValueError(f"source sequence {sequence_name!r} resolves to {len(sequence_ids)} MediaPool timelines")
    return next(iter(sequence_ids))


def _native_containers(data: bytes, kind: str) -> tuple[list[list[dict[str, object]]], dict[str, dict[str, tuple[int, int, int, bytes]]], set[str]]:
    root = ET.fromstring(_xml_safe(data))
    if _local(root.tag) != "Sm2SequenceContainer":
        raise ValueError("unknown SeqContainer XML schema")
    vector = "VideoTrackVec" if kind == "video" else "AudioTrackVec"
    clip_type = "Sm2TiVideoClip" if kind == "video" else "Sm2TiAudioClip"
    track_vec = next((child for child in root if _local(child.tag) == vector), None)
    if track_vec is None:
        raise ValueError(f"sequence container has no {vector}")
    tracks: list[list[dict[str, object]]] = []
    sequence_ids: set[str] = set()
    for wrapper in list(track_vec):
        native_track = next((node for node in wrapper.iter() if _local(node.tag) == "Sm2TiTrack"), None)
        if native_track is None:
            raise ValueError(f"unknown native {kind} track schema")
        sequence_id = (native_track.findtext("./Sequence") or "").strip()
        if not sequence_id:
            raise ValueError(f"native {kind} track has no Sequence reference")
        sequence_ids.add(sequence_id)
        clips: list[dict[str, object]] = []
        for node in native_track.iter():
            if _local(node.tag) != clip_type:
                continue
            dbid = node.get("DbId")
            if not dbid:
                raise ValueError(f"native {kind} clip has no DbId")
            try:
                start, duration = int(node.findtext("./Start") or ""), int(node.findtext("./Duration") or "")
            except ValueError as exc:
                raise ValueError(f"native {kind} clip start/duration is invalid") from exc
            if duration <= 0:
                raise ValueError(f"native {kind} clip duration must be positive")
            clips.append({"dbid": dbid, "start": start, "duration": duration})
        tracks.append(clips)
    return tracks, _field_spans(data), sequence_ids


def apply_timeline_state_to_archive(archive_path: Path, xml_paths: Path | list[Path]) -> int:
    """Apply source retimes and marks atomically; return the retimed clip count."""
    archive_path = Path(archive_path)
    sources = xml_paths if isinstance(xml_paths, list) else [xml_paths]
    roots = [ET.parse(path).getroot() for path in sources]
    plans = [plan for root in roots for plan in _source_plans(root)]
    mark_sources = {}
    for root in roots:
        sequence = root.find("sequence")
        if sequence is None or sequence_marks(sequence) is None:
            continue
        name = sequence.findtext("name") or ""
        if not name or name in mark_sources:
            raise ValueError("sequence marks require unique source timeline names")
        mark_sources[name] = sequence
    if not plans and not mark_sources:
        return 0
    identities = [(plan["sequence_name"], plan["kind"], plan["track"], plan["start"], plan["duration"]) for plan in plans]
    if len(set(identities)) != len(identities):
        raise ValueError("retimed source clips have ambiguous track/start/duration identities")
    with zipfile.ZipFile(archive_path, "r") as source_zip:
        archive_comment = source_zip.comment
        infos = source_zip.infolist()
        seq_members = [(index, info) for index, info in enumerate(infos) if info.filename.startswith("SeqContainer/") and info.filename.lower().endswith(".xml")]
        if not seq_members:
            raise ValueError("Resolve archive has no SeqContainer XML members")
        contents = [source_zip.read(info) for info in infos]
        sequence_ids = {
            name: _timeline_sequence_id(contents, infos, name)
            for name in dict.fromkeys(str(plan["sequence_name"]) for plan in plans)
        }
        candidates: list[list[tuple[int, str, dict[str, object], dict[str, dict[str, tuple[int, int, int, bytes]]]]]] = [[] for _ in plans]
        for member_index, info in seq_members:
            containers = {kind: _native_containers(contents[member_index], kind)
                          for kind in dict.fromkeys(str(plan["kind"]) for plan in plans)}
            for plan_index, plan in enumerate(plans):
                tracks, spans, native_sequence_ids = containers[str(plan["kind"])]
                if sequence_ids[str(plan["sequence_name"])] not in native_sequence_ids:
                    continue
                track_index = int(plan["track"]) - 1
                if track_index >= len(tracks):
                    continue
                for native in tracks[track_index]:
                    if native["start"] == plan["start"] and native["duration"] == plan["duration"]:
                        candidates[plan_index].append((member_index, str(native["dbid"]), plan, spans))
        if any(len(matches) != 1 for matches in candidates):
            counts = ", ".join(str(len(matches)) for matches in candidates)
            raise ValueError(f"retimed source clips must each match exactly one native clip; match counts: {counts}")
        patches: dict[int, list[tuple[int, int, bytes]]] = {}
        for matches in candidates:
            member_index, dbid, plan, spans = matches[0]
            fields = spans.get(dbid, {})
            updates = (("In", str(plan["in"])), ("MediaTimemapBA", str(plan["timemap"])))
            for field, value in updates:
                span = fields.get(field)
                if span is None:
                    raise ValueError(f"native clip {dbid} has no {field} field")
                begin, open_end, content_end, start_tag = span
                if start_tag.rstrip().endswith(b"/>"):
                    replacement = start_tag.rstrip()[:-2].rstrip() + b">" + value.encode("ascii") + b"</" + field.encode("ascii") + b">"
                    patches.setdefault(member_index, []).append((begin, open_end, replacement))
                else:
                    patches.setdefault(member_index, []).append((open_end, content_end, value.encode("ascii")))
        updated_contents = list(contents)
        for member_index, edits in patches.items():
            data = contents[member_index]
            for start, end, replacement in sorted(edits, reverse=True):
                data = data[:start] + replacement + data[end:]
            updated_contents[member_index] = data
            ET.fromstring(_xml_safe(data))
        matched_marks = []
        for member_index, info in enumerate(infos):
            if mark_sources and info.filename.startswith("MediaPool/") and info.filename.lower().endswith(".xml"):
                pool, matched = update_sequence_marks(contents[member_index].decode("utf-8"), mark_sources)
                if matched:
                    updated_contents[member_index] = pool.encode("utf-8")
                    matched_marks.extend(matched)
        if any(matched_marks.count(name) != 1 for name in mark_sources):
            raise ValueError("sequence marks must each match exactly one MediaPool timeline")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=archive_path.name + ".", suffix=".tmp", dir=archive_path.parent, delete=False) as temp:
            temp_name = temp.name
        with zipfile.ZipFile(temp_name, "w") as target_zip:
            target_zip.comment = archive_comment
            for info, payload in zip(infos, updated_contents):
                target_zip.writestr(copy.copy(info), payload)
        os.replace(temp_name, archive_path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return len(plans)


__all__ = ["apply_timeline_state_to_archive"]
