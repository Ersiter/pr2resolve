"""Build Resolve 21 per-clip media time maps and retimed source offsets.

The field layout follows measured Resolve exports and the independent decoder
at https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/resolve-advanced/vendor/drp-format/README.md.
This module is an independent stdlib implementation; it supports identity,
constant-speed, and explicit piecewise-linear audio/video time maps.
"""

from __future__ import annotations

import math
import struct
import uuid
from collections.abc import Sequence
import xml.etree.ElementTree as ET


_KEYED_DICT_DOUBLE = 0x06
_KEYED_DICT_STRING = 0x0A
_KEYED_DICT_BYTES = 0x0C
STILL_SOURCE_IN = "86400"
AUDIO_RETIME_WARNING = "audio retiming uses Resolve pitch correction; source pitch policy is not mapped"


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


def _encode_keyframe_points(points: Sequence[tuple[float, float]]) -> bytes:
    _, first_source = points[0]
    fields = bytearray(b"\x80\x0a\x00")
    if first_source != 0:
        fields.extend(b"\x11" + struct.pack("<d", first_source))
    for record_seconds, source_seconds in points[1:]:
        point = (
            b"\x09" + struct.pack("<d", record_seconds)
            + b"\x11" + struct.pack("<d", source_seconds)
        )
        fields.extend(b"\x0a" + bytes((len(point),)) + point)
    return bytes(fields)


def _validated_retime_points(
    points: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    for point in points:
        if len(point) != 2:
            raise ValueError("each retime point must contain record and source seconds")
        record_seconds, source_seconds = float(point[0]), float(point[1])
        if not math.isfinite(record_seconds) or not math.isfinite(source_seconds):
            raise ValueError("retime point coordinates must be finite")
        if result and record_seconds <= result[-1][0]:
            raise ValueError("retime record times must be strictly increasing")
        result.append((record_seconds, source_seconds))
    if len(result) < 2:
        raise ValueError("retime map requires at least two points")
    if result[0][0] != 0:
        raise ValueError("retime map must start at record time zero")
    return tuple(result)


def source_at_record(
    points: Sequence[tuple[float, float]], record_seconds: float,
) -> float:
    """Evaluate a piecewise-linear record-seconds to source-seconds map."""
    curve = _validated_retime_points(points)
    record = float(record_seconds)
    if not math.isfinite(record) or record < curve[0][0] or record > curve[-1][0]:
        raise ValueError("record time is outside the retime map")
    for (r0, s0), (r1, s1) in zip(curve, curve[1:]):
        if record <= r1:
            return s0 + (s1 - s0) * ((record - r0) / (r1 - r0))
    return curve[-1][1]


def record_intervals_at_source(
    points: Sequence[tuple[float, float]], source_seconds: float,
) -> tuple[tuple[float, float], ...]:
    """Return every record-time point or interval that maps to source time."""
    curve = _validated_retime_points(points)
    source = float(source_seconds)
    if not math.isfinite(source):
        raise ValueError("source time must be finite")
    matches: list[tuple[float, float]] = []
    for (r0, s0), (r1, s1) in zip(curve, curve[1:]):
        if s0 == s1:
            if math.isclose(source, s0, rel_tol=1e-12, abs_tol=1e-12):
                matches.append((r0, r1))
        elif min(s0, s1) - 1e-12 <= source <= max(s0, s1) + 1e-12:
            record = r0 + (source - s0) * ((r1 - r0) / (s1 - s0))
            matches.append((record, record))
    merged: list[tuple[float, float]] = []
    for start, end in sorted(matches):
        if merged and start <= merged[-1][1] + 1e-12:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _encode_frame_position(record_frames: float) -> str:
    whole = math.floor(record_frames)
    fraction = record_frames - whole
    if math.isclose(fraction, 0.0, rel_tol=0.0, abs_tol=1e-12):
        return str(whole)
    if math.isclose(fraction, 1.0, rel_tol=0.0, abs_tol=1e-12):
        return str(whole + 1)
    return f"{whole}|{struct.pack('<d', fraction).hex()}"


def _xml_rate(element: ET.Element | None, fallback: float | None = None) -> float | None:
    if element is None:
        return _positive_finite(fallback, "timeline_fps") if fallback is not None else None
    rate = element.find("./rate")
    if rate is None:
        return _positive_finite(fallback, "timeline_fps") if fallback is not None else None
    try:
        timebase = float(rate.findtext("./timebase") or "")
    except ValueError as exc:
        raise ValueError("clipitem rate is invalid") from exc
    ntsc = (rate.findtext("./ntsc") or "").strip().upper() == "TRUE"
    return _positive_finite(timebase * (1000 / 1001 if ntsc else 1), "clipitem rate")


def _read_graphdict_keyframes(graphdict: ET.Element) -> tuple[list[dict[str, object]], tuple[tuple[float, float], ...]]:
    raw_keyframes: list[dict[str, object]] = []
    frame_points: list[tuple[float, float]] = []
    flags = ("speedvirtualkf", "speedkfin", "speedkfout", "speedkfstart", "speedkfend")
    for keyframe in graphdict.findall("./keyframe"):
        when_text = keyframe.findtext("./when")
        value_text = keyframe.findtext("./value")
        try:
            when_value = float(when_text or "")
            mapped_value = float(value_text or "")
        except ValueError as exc:
            raise ValueError("graphdict keyframe has a missing or invalid when/value") from exc
        if not math.isfinite(when_value) or not math.isfinite(mapped_value):
            raise ValueError("graphdict keyframe coordinates must be finite")
        if not when_value.is_integer():
            raise ValueError("graphdict when coordinates must be integral")
        raw_keyframes.append({
            "when": when_value,
            "value": mapped_value,
            "flags": {name: (keyframe.findtext(f"./{name}") or "").strip().upper() for name in flags},
            "metadata_xml": ET.tostring(keyframe, encoding="unicode"),
        })
        frame_points.append((when_value, mapped_value))
    if len(frame_points) < 2:
        raise ValueError("graphdict requires at least two keyframes")
    for (when_a, value_a), (when_b, value_b) in zip(frame_points, frame_points[1:]):
        if when_b <= when_a:
            raise ValueError("graphdict record coordinates must be strictly increasing")
        if value_b <= value_a:
            raise ValueError("nonmonotone, reverse, or hold graphdict curves are unsupported")
    return raw_keyframes, tuple(frame_points)


def extract_fcp7_retime_alignment(
    clip: ET.Element,
    timeline_fps: float,
) -> dict[str, object]:
    """Parse an XMEML Time Remap graph and read its special In/Out markers.

    FCP7 graphdict frame coordinates are normalized by the clipitem rate on
    both axes. The returned piecewise-linear map is approximate; speedkfin
    supplies record/source In, while speedkfout is corrected per Apple's
    1-based last-frame convention.
    """
    target_rate = _positive_finite(timeline_fps, "timeline_fps")
    clip_rate = _xml_rate(clip, target_rate)
    if clip_rate is None:
        raise ValueError("clipitem rate is unavailable")
    effects = [
        effect for effect in clip.findall("./filter/effect")
        if (effect.findtext("./effectid") or "").strip().lower() == "timeremap"
    ]
    if len(effects) != 1:
        raise ValueError("clipitem must have exactly one Time Remap effect")
    effect = effects[0]
    parameter_nodes = effect.findall("./parameter")
    parameter_ids = [(parameter.findtext("./parameterid") or "").strip() for parameter in parameter_nodes]
    for required_id in ("speed", "variablespeed", "graphdict"):
        if parameter_ids.count(required_id) > 1:
            raise ValueError(f"Time Remap has ambiguous {required_id} parameters")
    parameters = dict(zip(parameter_ids, parameter_nodes))
    variable_parameter = parameters.get("variablespeed")
    if variable_parameter is None or variable_parameter.findtext("./value") is None:
        raise ValueError("Time Remap has no explicit variablespeed flag")
    variable_value = (variable_parameter.findtext("./value") or "").strip()
    if variable_value not in ("0", "1", "FALSE", "TRUE", "false", "true"):
        raise ValueError("Time Remap variablespeed flag is ambiguous")
    graphdict = parameters.get("graphdict")
    if graphdict is None or graphdict.find("./keyframe") is None:
        raise ValueError("Time Remap has no graphdict keyframes; speed is not inferred from the speed parameter")

    raw_keyframes, frame_points = _read_graphdict_keyframes(graphdict)
    if frame_points[0][0] != 0:
        raise ValueError("graphdict must preserve its zero-origin virtual point")
    for keyframe in graphdict.iter("keyframe"):
        if any(keyframe.find(name) is not None for name in ("inbez", "outbez", "inscale", "outscale")):
            raise ValueError("Bezier graphdict handles are unsupported")
        for name in ("hadbezierin", "hadbezierout"):
            if (keyframe.findtext(name) or "").strip().upper() == "TRUE":
                raise ValueError("Bezier graphdict history is unsupported")

    interpolation_names = [
        (node.findtext("./name") or "").strip()
        for node in graphdict.findall("./interpolation")
    ]
    if any(name not in ("FCPCurve", "Linear", "linear") for name in interpolation_names):
        raise ValueError("unknown graphdict interpolation mode")
    points = tuple((when / clip_rate, value / clip_rate) for when, value in frame_points)
    points = _validated_retime_points(points)
    is_variable = variable_value.lower() in ("1", "true")
    if not is_variable:
        in_markers = [
            keyframe for keyframe in raw_keyframes
            if keyframe["flags"]["speedkfin"] == "TRUE"
        ]
        out_markers = [
            keyframe for keyframe in raw_keyframes
            if keyframe["flags"]["speedkfout"] == "TRUE"
        ]
        active_points = frame_points
        if len(out_markers) == 1:
            active_in = (
                in_markers[0]["when"] if len(in_markers) == 1
                else float(clip.findtext("./in") or "nan")
            )
            active_points = tuple(
                point for point in frame_points
                if active_in <= point[0] <= out_markers[0]["when"]
            )
        slopes = [
            (right[1] - left[1]) / (right[0] - left[0])
            for left, right in zip(active_points, active_points[1:])
        ]
        if any(not math.isclose(slope, slopes[0], rel_tol=1e-6, abs_tol=1e-9) for slope in slopes[1:]):
            raise ValueError("variablespeed=false conflicts with a non-linear graphdict")
    if any(keyframe.find("anchoroffset") is not None for keyframe in graphdict.findall("./keyframe")):
        raise ValueError("graphdict anchoroffset is unsupported")

    try:
        clip_in_frames = float(clip.findtext("./in") or "")
        source_out_frames = float(clip.findtext("./out") or "")
    except ValueError as exc:
        raise ValueError("clipitem in/out trim is missing or invalid") from exc
    if not math.isfinite(clip_in_frames) or not math.isfinite(source_out_frames):
        raise ValueError("clipitem in/out trim must be finite")
    if clip_in_frames < 0 or source_out_frames <= clip_in_frames:
        raise ValueError("clipitem source trim is invalid")
    source_out_seconds = source_out_frames / clip_rate

    in_markers = [
        keyframe for keyframe in raw_keyframes
        if keyframe["flags"]["speedkfin"] == "TRUE"
    ]
    if len(in_markers) > 1:
        raise ValueError("graphdict has ambiguous speedkfin markers")
    if in_markers:
        in_marker = in_markers[0]
        if in_marker["when"] != clip_in_frames:
            raise ValueError(
                "speedkfin differs from clipitem In; transition/handle compensation is unsupported"
            )
        record_in_frames = float(in_marker["when"])
        source_in_frames = float(in_marker["value"])
        source_inference = "speedkfin.value"
    else:
        record_in_frames = clip_in_frames
        record_in_seconds_candidate = record_in_frames / clip_rate
        if not points[0][0] <= record_in_seconds_candidate <= points[-1][0]:
            raise ValueError("missing speedkfin and clipitem In is outside graphdict")
        source_in_frames = source_at_record(points, record_in_seconds_candidate) * clip_rate
        source_inference = "graphdict interpolation at clipitem In (speedkfin absent)"
    source_in_seconds = source_in_frames / clip_rate
    record_in_seconds = record_in_frames / clip_rate
    file_element = clip.find("./file")
    file_rate = _xml_rate(file_element)
    file_duration_frames_text = file_element.findtext("./duration") if file_element is not None else None
    try:
        file_duration_frames = float(file_duration_frames_text) if file_duration_frames_text else None
    except ValueError as exc:
        raise ValueError("file duration is invalid") from exc
    if file_duration_frames is not None and (
        not math.isfinite(file_duration_frames) or file_duration_frames <= 0
    ):
        raise ValueError("file duration must be positive and finite")
    source_media_duration_seconds = (
        file_duration_frames / file_rate
        if file_duration_frames is not None and file_rate is not None
        else None
    )
    if source_media_duration_seconds is not None and any(
        source < 0 or source > source_media_duration_seconds
        for _, source in points
    ):
        raise ValueError("graphdict source position exceeds file duration")
    speed_percent_raw = (
        parameters.get("speed").findtext("./value")
        if parameters.get("speed") is not None else None
    )
    try:
        speed_percent = float(speed_percent_raw) if speed_percent_raw is not None else None
    except ValueError:
        speed_percent = None
    mapped_source_in = source_at_record(points, record_in_seconds)
    if not math.isclose(mapped_source_in, source_in_seconds, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("speedkfin does not match the graphdict at its record position")
    encoded_in = _encode_frame_position(record_in_seconds * target_rate)

    first_mapped = {"record_seconds": points[0][0], "source_seconds": points[0][1]}
    last_mapped = {"record_seconds": points[-1][0], "source_seconds": points[-1][1]}
    out_markers = [
        (index, keyframe) for index, keyframe in enumerate(raw_keyframes)
        if keyframe["flags"]["speedkfout"] == "TRUE"
    ]
    if len(out_markers) > 1:
        raise ValueError("graphdict has ambiguous speedkfout markers")
    speedkfout_expected = None
    speedkfout_when = None
    if out_markers:
        out_index, out_marker = out_markers[0]
        if out_index == 0:
            raise ValueError("speedkfout has no preceding graphdict keyframe")
        previous = raw_keyframes[out_index - 1]
        slope = (out_marker["value"] - previous["value"]) / (out_marker["when"] - previous["when"])
        speedkfout_expected = (out_marker["value"] - slope) / clip_rate
        speedkfout_when = out_marker["when"]

    try:
        timeline_start = float(clip.findtext("./start") or "")
        timeline_end = float(clip.findtext("./end") or "")
    except ValueError:
        timeline_start = timeline_end = -1.0
    visible_last_record = None
    visible_last_source = None
    source_trim_end_residual = None
    if (
        math.isfinite(timeline_start) and math.isfinite(timeline_end)
        and timeline_start >= 0 and timeline_end > timeline_start
    ):
        visible_frames = timeline_end - timeline_start
        visible_last_record = record_in_seconds + (visible_frames - 1) / target_rate
        if points[0][0] <= visible_last_record <= points[-1][0]:
            visible_last_source = source_at_record(points, visible_last_record)
            if speedkfout_expected is not None:
                source_trim_end_residual = visible_last_source - speedkfout_expected
    warnings = [
        "linear-segment approximation; original graphdict and interpolation metadata are retained",
    ]
    if source_inference != "speedkfin.value":
        warnings.append(source_inference)
    if out_markers and visible_last_source is None:
        warnings.append("visible last-frame sample could not be compared with speedkfout")
    return {
        "clip_rate": clip_rate,
        "timeline_fps": target_rate,
        "file_rate": file_rate,
        "source_frames": file_duration_frames,
        "source_media_duration_seconds": source_media_duration_seconds,
        "speed_percent": speed_percent,
        "speed_percent_raw": speed_percent_raw,
        "variable_speed": is_variable,
        "points": points,
        "raw_keyframes": tuple(raw_keyframes),
        "interpolation": tuple(interpolation_names),
        "raw_graphdict_xml": ET.tostring(graphdict, encoding="unicode"),
        "raw_effect_xml": ET.tostring(effect, encoding="unicode"),
        "source_in_seconds": source_in_seconds,
        "source_in_frames": source_in_frames,
        "xml_clip_in_frames": clip_in_frames,
        "source_in_definition": source_inference,
        "record_in_seconds": record_in_seconds,
        "record_in_frames": record_in_seconds * target_rate,
        "encoded_in": encoded_in,
        "source_in_mapping_residual_seconds": source_at_record(points, record_in_seconds) - source_in_seconds,
        "first_mapped_sample": first_mapped,
        "last_mapped_sample": last_mapped,
        "last_visible_sample": (
            {"record_seconds": visible_last_record, "source_seconds": visible_last_source}
            if visible_last_source is not None else None
        ),
        "speedkfout_when_frames": speedkfout_when,
        "speedkfout_expected_last_source_seconds": speedkfout_expected,
        "timeline_start": clip.findtext("./start"),
        "timeline_end": clip.findtext("./end"),
        "clipitem_duration": clip.findtext("./duration"),
        "xml_clip_out_seconds": source_out_seconds,
        "source_trim_end_residual_seconds": source_trim_end_residual,
        "source_trim_end_residual_frames": (
            source_trim_end_residual * clip_rate
            if source_trim_end_residual is not None else None
        ),
        "source_trim_end_residual_definition": (
            "visible last-frame graphdict source minus Apple speedkfout value corrected by preceding slope"
        ),
        "warnings": tuple(warnings),
    }


def _identity_timemap(end_seconds: float, *, audio: bool) -> str:
    if audio:
        payload = struct.pack(">d", end_seconds)
    else:
        payload = struct.pack(">5d", end_seconds, 0.0, end_seconds, 0.0, end_seconds)
    return (b"\x02" + payload).hex()


def build_still_timemap() -> str:
    """Encode the Resolve 21 template's 60000-second hold at source zero."""
    extent = 60000.0
    return _encode_keyed_dict([
        ("YMin", _KEYED_DICT_DOUBLE, -1.0),
        ("YMax", _KEYED_DICT_DOUBLE, -1.0),
        ("XMax", _KEYED_DICT_DOUBLE, extent),
        ("UniqueId", _KEYED_DICT_STRING, str(uuid.uuid4())),
        ("KeyframesBA", _KEYED_DICT_BYTES, b"\x80\x0a\x00\x0a\x09\x09" + struct.pack("<d", extent)),
        ("DbType", _KEYED_DICT_STRING, "Sm2TimeMap"),
    ]).hex()


def build_media_timemap(
    source_frames: int,
    source_fps: float,
    speed_percent: float = 100.0,
    *,
    audio: bool = False,
    keyframes: Sequence[tuple[float, float]] | None = None,
) -> str:
    """Return the MediaTimemapBA hex for an identity or piecewise-linear map.

    Keyframe points use (record seconds, absolute source seconds), beginning at
    record time zero. Clip extents use the last source-frame index,
    ``(source_frames - 1) / fps``.
    Resolve 21 audio shares the keyed time map after its media cache and
    Fairlight channel strips are registered. Audio identity has one extent.
    """
    if isinstance(source_frames, bool) or not isinstance(source_frames, int) or source_frames < 1:
        raise ValueError("source_frames must be a positive integer")
    fps = _positive_finite(source_fps, "source_fps")
    end_seconds = (source_frames - 1) / fps

    if keyframes is not None:
        points = _validated_retime_points(keyframes)
        source_duration = source_frames / fps
        if any(source < 0 or source > source_duration for _, source in points):
            raise ValueError("retime source times must stay within the source extent")
        source_deltas = [
            right[1] - left[1] for left, right in zip(points, points[1:])
        ]
        if any(delta == 0 for delta in source_deltas):
            raise ValueError("hold retiming requires the dedicated freeze map")
        if any(delta < 0 for delta in source_deltas) and any(delta > 0 for delta in source_deltas):
            raise ValueError("mixed forward/reverse retiming is unsupported")
        record_end = points[-1][0]
        entries: list[tuple[str, int, str | bytes | float]] = [
            ("YMin", _KEYED_DICT_DOUBLE, -1.0),
            ("YMax", _KEYED_DICT_DOUBLE, -1.0),
            ("XMax", _KEYED_DICT_DOUBLE, record_end),
            ("UniqueId", _KEYED_DICT_STRING, str(uuid.uuid4())),
            ("LastValidYOffset", _KEYED_DICT_DOUBLE, end_seconds),
            ("KeyframesBA", _KEYED_DICT_BYTES, _encode_keyframe_points(points)),
            ("DbType", _KEYED_DICT_STRING, "Sm2TimeMap"),
        ]
        return _encode_keyed_dict(entries).hex()

    speed = _positive_finite(speed_percent, "speed_percent") / 100.0
    if speed_percent == 100:
        return _identity_timemap(end_seconds, audio=audio)
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


__all__ = [
    "STILL_SOURCE_IN",
    "build_still_timemap",
    "build_media_timemap",
    "extract_fcp7_retime_alignment",
    "record_intervals_at_source",
    "retimed_source_in",
    "source_at_record",
]
