"""Conservative Lumetri-to-Resolve plans, DRX payloads, and native grade attachment."""

from __future__ import annotations

import math
import re
import struct
import time
import uuid
from dataclasses import dataclass
from html import escape
from xml.etree import ElementTree as ET

from pr2_lumetri import LumetriClip, LumetriEffect


_MAPPED_IDS = {"11": "exposure", "12": "contrast", "20": "saturation"}
_MAPPED_LIMITS = {"11": (-5.0, 5.0), "12": (-100.0, 100.0), "20": (0.0, 200.0)}
_DRX_GAIN_MASTER_ID = 100663328
_DRX_SATURATION_ID = 100663301
_DRX_LOG_CONTRAST_ID = 2248147137
_VERSION_TABLE_FIELDS_BLOB = "00000001000000010000002200410063007400690076006500560065007200730069006f006e0054007900700065000000020000"


@dataclass(frozen=True)
class GradeNodePlan:
    effect_order: int
    source_component_ref: str
    source_component_uid: str
    label: str
    params: dict[str, object]
    source_values: tuple[tuple[str, str], ...]
    approximation_notes: tuple[str, ...]
    unmapped_parameter_ids: tuple[str, ...]
    unmapped_shader_names: tuple[str, ...]


@dataclass(frozen=True)
class LumetriGradePlan:
    clip_name: str
    track_index: int
    item_index: int
    start_ticks: str
    end_ticks: str
    nodes: tuple[GradeNodePlan, ...]


def _scalar(raw: str) -> float | None:
    pieces = raw.split(",")
    value = pieces[1] if len(pieces) > 1 else raw
    try:
        number = float(value.strip())
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _component_values(effect: LumetriEffect) -> tuple[dict[str, float], set[str], bool]:
    candidates: dict[str, list[float]] = {key: [] for key in _MAPPED_IDS}
    seen: set[str] = set()
    blob_animated = effect.blob is not None and len(effect.blob.keyframes) > 1
    for record in effect.parameters:
        pid = record.parameter_id
        if pid not in _MAPPED_IDS:
            continue
        seen.add(pid)
        number = _scalar(record.raw_value) if not blob_animated and record.encoding == "scalar" and not record.is_time_varying else None
        if number is not None and _MAPPED_LIMITS[pid][0] <= number <= _MAPPED_LIMITS[pid][1]:
            candidates[pid].append(number)
    mapped = {pid: values[0] for pid, values in candidates.items()
              if len(values) == 1 and sum(record.parameter_id == pid for record in effect.parameters) == 1}
    return mapped, seen, blob_animated


def build_lumetri_node_plan(clip: LumetriClip) -> LumetriGradePlan:
    """Make one ordered serial-node candidate per source Lumetri component.

    Only the current exposure, contrast, and saturation candidate mapping is
    emitted. Every unsupported control and source shader remains represented
    as a loss entry on the corresponding node.
    """
    nodes: list[GradeNodePlan] = []
    for effect in clip.effects:
        values, seen, blob_animated = _component_values(effect)
        source_values = tuple((record.parameter_id, record.raw_value) for record in effect.parameters)
        params: dict[str, object] = {}
        notes: list[str] = []
        if "11" in values:
            params["gain"] = {"master": 2.0 ** values["11"]}
            notes.append("Exposure -> master gain 2^stops; approximate cross-application mapping")
        if "12" in values:
            params["contrast"] = 1.0 + values["12"] / 100.0
            notes.append("Contrast -> Resolve contrast 1 + Premiere slider/100; approximate")
        if "20" in values:
            params["saturation"] = values["20"] / 2.0
            notes.append("Premiere saturation 100 -> Resolve UI saturation 50; approximate")
        active_ids = tuple(pid for pid in _MAPPED_IDS if pid in seen)
        unmapped_ids = tuple(
            record.parameter_id for record in effect.parameters
            if record.parameter_id not in _MAPPED_IDS
            or record.parameter_id in active_ids and (
                blob_animated or record.is_time_varying or record.encoding != "scalar"
                or len([r for r in effect.parameters if r.parameter_id == record.parameter_id]) != 1
                or _scalar(record.raw_value) is None
                    or not (_MAPPED_LIMITS[record.parameter_id][0] <= _scalar(record.raw_value) <= _MAPPED_LIMITS[record.parameter_id][1])
            )
        )
        shader_names: list[str] = []
        if effect.blob is not None:
            for keyframe in effect.blob.keyframes:
                shader_names.extend(shader.name for shader in keyframe.shaders)
        unmapped_shaders = tuple(dict.fromkeys(name for name in shader_names if name != "BasicCorrection3"))
        if any(name == "BasicCorrection3" for name in shader_names):
            notes.append("BasicCorrection3 maps only supported scalar ParameterID candidates; blob shader values retained separately")
        if effect.blob is not None and effect.blob.parse_error:
            notes.append(f"Lumetri blob could not be parsed: {effect.blob.parse_error}")
        elif blob_animated:
            notes.append(f"{len(effect.blob.keyframes)} source keyframes retained, but Lumetri keyframes are not encoded into this DRX node")
        nodes.append(GradeNodePlan(
            effect.order, effect.component_ref, effect.component_uid,
            effect.display_name or f"Lumetri {effect.order + 1}", params,
            source_values, tuple(notes), unmapped_ids, unmapped_shaders,
        ))
    return LumetriGradePlan(
        clip.clip_name, clip.track_index, clip.item_index, clip.start_ticks,
        clip.end_ticks, tuple(nodes),
    )


def _varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("protobuf varint cannot be negative")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _wire_varint(field: int, value: int) -> bytes:
    return _varint((field << 3) | 0) + _varint(value)


def _wire_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _wire_float(field: int, value: float) -> bytes:
    return _varint((field << 3) | 5) + struct.pack("<f", value)


def _grade_parameter(parameter_id: int, value: float) -> bytes:
    value_message = _wire_float(1, value)
    entry = _wire_varint(1, parameter_id) + _wire_bytes(2, value_message)
    return _wire_bytes(3, entry)


def _primary_corrector(parameters: dict[str, object]) -> bytes:
    entries: list[bytes] = []
    gain = parameters.get("gain")
    if isinstance(gain, dict) and isinstance(gain.get("master"), (int, float)):
        value = float(gain["master"])
        if value != 1.0:
            entries.append(_grade_parameter(_DRX_GAIN_MASTER_ID, value))
    saturation = parameters.get("saturation")
    if isinstance(saturation, (int, float)) and float(saturation) != 50.0:
        entries.append(_grade_parameter(_DRX_SATURATION_ID, float(saturation) / 50.0))
    contrast = parameters.get("contrast")
    if isinstance(contrast, (int, float)) and float(contrast) != 1.0:
        entries.append(_grade_parameter(_DRX_LOG_CONTRAST_ID, float(contrast)))
    if not entries:
        return b""
    param_container = _wire_varint(1, 1) + b"".join(entries)
    corrector_parameters = _wire_bytes(2, param_container)
    corrector = _wire_varint(1, 1) + _wire_varint(3, 1) + _wire_bytes(6, corrector_parameters)
    return _wire_bytes(1, corrector)


def _node(node_id: int, index: int, label: str, params: dict[str, object], timestamp: int) -> bytes:
    fields = [
        _wire_varint(1, node_id), _wire_varint(2, index),
        _wire_varint(4, 190 + (index - 1) * 270), _wire_varint(5, 180),
        _wire_bytes(6, label.encode("utf-8")), _wire_varint(7, 1), _wire_varint(8, 44),
    ]
    primary = _primary_corrector(params)
    if primary:
        # DRX's simple-primary convention carries a primary corrector, a type-2
        # companion marker, and the packed sub-corrector presence list.
        companion_param = _wire_bytes(3, _wire_varint(1, 2284847105) + _wire_bytes(2, _wire_varint(2, 0)))
        companion_container = _wire_varint(1, 1) + companion_param
        companion = _wire_bytes(1, _wire_varint(1, 2) + _wire_varint(3, 1) + _wire_bytes(6, _wire_bytes(2, companion_container)))
        node_settings = primary + companion + _wire_bytes(2, bytes((3, 4, 5, 6, 18)))
        fields.append(_wire_bytes(9, node_settings))
    tool = _wire_bytes(1, _wire_varint(1, 0xC0000001) + _wire_bytes(2, _wire_varint(2, 2)))
    fields.extend((_wire_bytes(10, tool), _wire_varint(12, timestamp)))
    return _wire_bytes(7, b"".join(fields))


def _resolution(width: int, height: int) -> bytes:
    body = b"".join((
        _wire_varint(1, width), _wire_varint(2, height), _wire_float(3, 1.0),
        _wire_varint(4, width), _wire_varint(5, height), _wire_float(6, 1.0),
        _wire_varint(7, width), _wire_varint(8, height), _wire_varint(9, 0xFFFFFFFF),
    ))
    return _wire_bytes(3, body)


def _edge(source: int, target: int, index: int) -> bytes:
    body = b"".join((
        _wire_varint(1, source), _wire_varint(3, target), _wire_varint(5, 64),
        _wire_varint(6, 64), _wire_varint(7, index),
    ))
    return _wire_bytes(8, body)


def _input_output_markers(first_id: int, last_id: int) -> tuple[bytes, bytes]:
    input_id = first_id + 6
    input_inner = b"".join((
        _wire_varint(1, first_id + 22), _wire_varint(2, 64),
        _wire_varint(3, input_id), _wire_varint(4, first_id),
    ))
    input_marker = _wire_bytes(9, _wire_varint(1, input_id) + _wire_varint(2, 80) + _wire_bytes(3, input_inner))
    output_id = last_id + 7
    output_inner = b"".join((
        _wire_varint(1, last_id + 23), _wire_varint(2, 64),
        _wire_varint(3, output_id), _wire_varint(4, last_id),
    ))
    output_marker = _wire_bytes(10, _wire_varint(1, output_id) + _wire_varint(2, 64) + _wire_bytes(3, output_inner))
    return input_marker, output_marker


def _root_position(timestamp: int) -> bytes:
    metadata = _wire_varint(1, 0xC0000001) + _wire_bytes(2, _wire_varint(2, 2))
    inner = b"".join((
        _wire_varint(4, 190), _wire_varint(5, 180), _wire_varint(7, 1),
        _wire_varint(8, 84), _wire_bytes(9, b""), _wire_bytes(10, _wire_bytes(1, metadata)),
        _wire_varint(12, timestamp),
    ))
    return _wire_bytes(3, inner)


def _stored_body(proto: bytes) -> bytes:
    """Emit Resolve's stored Body variant (0x80 + raw protobuf)."""
    return b"\x80" + proto


def _encode_grade_body(plan: LumetriGradePlan, width: int, height: int, timestamp: int) -> bytes:
    node_count = len(plan.nodes)
    if not node_count:
        raise ValueError("cannot build a DRX grade with no Lumetri effects")
    ids: list[int] = []
    candidate = 2
    first_node_id = 2
    input_inner_marker_id = first_node_id + 22
    input_marker_ids = {first_node_id + 6, input_inner_marker_id}
    # Output marker F1 is lastNodeId+7; reserving 17 prevents it colliding
    # with the fixed input inner marker ID 24 (17+7).
    reserved_node_ids = input_marker_ids | {input_inner_marker_id - 7}
    while len(ids) < node_count:
        if candidate not in reserved_node_ids:
            ids.append(candidate)
        candidate += 1
    marker_ids = {ids[0] + 6, ids[0] + 22, ids[-1] + 7, ids[-1] + 23}
    if len(marker_ids) != 4 or marker_ids.intersection(ids):
        raise ValueError("DRX marker IDs collide with a grading-node ID")
    fields = [_wire_varint(1, ids[-1]), _wire_varint(2, 1), _resolution(width, height)]
    fields.extend(_node(node_id, index, node.label, node.params, timestamp) for index, (node_id, node) in enumerate(zip(ids, plan.nodes), 1))
    fields.extend(_edge(ids[i], ids[i + 1], 3 + i) for i in range(node_count - 1))
    fields.extend(_input_output_markers(ids[0], ids[-1]))
    fields.extend((_wire_varint(11, 1), _wire_varint(12, timestamp)))
    return _stored_body(_wire_bytes(1, b"".join(fields)) + _root_position(timestamp))


def _encode_track_version_body(width: int, height: int, timestamp: int) -> bytes:
    resolution = _resolution(width, height)
    input_marker = _wire_bytes(9, _wire_varint(1, 2) + _wire_varint(2, 64))
    output_marker = _wire_bytes(10, _wire_varint(1, 3) + _wire_varint(2, 64))
    grade = _wire_bytes(1, b"".join((
        _wire_varint(1, 0xFFFFFFFF), _wire_varint(2, 1), resolution,
        _wire_varint(4, 310722), input_marker, output_marker,
        _wire_varint(11, 1), _wire_varint(12, timestamp),
    )))
    position = _wire_bytes(3, b"".join((
        _wire_varint(4, 190), _wire_varint(5, 180), _wire_varint(7, 1),
        _wire_varint(8, 85), _wire_bytes(9, b""),
    )))
    return _stored_body(grade + position)


def build_lumetri_body(
    clip: LumetriClip, width: int = 1920, height: int = 1080, timestamp: int | None = None,
) -> bytes:
    """Build the shared DRX Body bytes from ordered Lumetri effects."""
    if width <= 0 or height <= 0:
        raise ValueError("DRX dimensions must be positive")
    plan = build_lumetri_node_plan(clip)
    return _encode_grade_body(plan, width, height, int(time.time()) if timestamp is None else timestamp)


def build_lumetri_drx(
    clip: LumetriClip, width: int = 1920, height: int = 1080, timestamp: int | None = None,
) -> bytes:
    """Return a complete DRX XML payload suitable for ApplyGradeFromDRX."""
    ts = int(time.time()) if timestamp is None else timestamp
    body = build_lumetri_body(clip, width, height, ts).hex()
    track_body = _encode_track_version_body(width, height, ts).hex()
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts))
    still_id = str(uuid.uuid4())
    clip_version_id = str(uuid.uuid4())
    track_version_id = str(uuid.uuid4())
    label = escape(f"Lumetri - {clip.clip_name}", quote=True)
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!--DbAppVer="19.1.3.0007" DbPrjVer="14"-->
<Gallery::GyStill DbId="{still_id}">
 <FieldsBlob/><SrcHint>{escape(clip.clip_name)}</SrcHint><SrcType>1</SrcType><GalleryPath/>
 <Label>{label}</Label><RecTC>01:00:00:00</RecTC><SrcTC>00:00:00:00</SrcTC><DpxDescriptor>50</DpxDescriptor>
 <Width>{width}</Width><Height>{height}</Height><BitDepth>10</BitDepth><PAR>1</PAR><Endianship>1</Endianship><CreateTime>{now}</CreateTime>
 <pClipFullVer><ListMgt::LmVersion DbId="{clip_version_id}"><FieldsBlob/><Name/><HasCorrection>true</HasCorrection><VerType>0</VerType><ImplVersion>1</ImplVersion><IncludedInRecording>true</IncludedInRecording><FlatPassEnabled>false</FlatPassEnabled><RGBAOutputEnabled>false</RGBAOutputEnabled><Body>{body}</Body><UseVersionClipProcParams>true</UseVersionClipProcParams></ListMgt::LmVersion></pClipFullVer>
 <pTrackVer><ListMgt::LmVersion DbId="{track_version_id}"><FieldsBlob/><Name/><HasCorrection>false</HasCorrection><VerType>1</VerType><ImplVersion>1</ImplVersion><IncludedInRecording>true</IncludedInRecording><FlatPassEnabled>false</FlatPassEnabled><RGBAOutputEnabled>false</RGBAOutputEnabled><Body>{track_body}</Body><UseVersionClipProcParams>true</UseVersionClipProcParams></ListMgt::LmVersion></pTrackVer>
 <PrimaryCCMode>0</PrimaryCCMode>
</Gallery::GyStill>'''
    return xml.encode("utf-8")


def extract_drx_body(payload: bytes | str) -> bytes:
    """Extract the hex Body from DRX XML, or validate and return raw Body bytes."""
    if isinstance(payload, bytes) and payload[:1] in (b"\x80", b"\x81"):
        return payload
    text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    match = re.search(r"<Body>([0-9a-fA-F]+)</Body>", text)
    if match is None or len(match.group(1)) % 2:
        raise ValueError("DRX payload has no valid hex Body")
    body = bytes.fromhex(match.group(1))
    if not body or body[0] not in (0x80, 0x81):
        raise ValueError("DRX Body has an unsupported format marker")
    return body


def attach_native_grade(node: ET.Element, drx_payload: bytes) -> None:
    """Attach the DRX Body as an active clip-grade version on a native clip.

    The version-table FieldsBlob contains the observed ActiveVersionType=0
    enum. The UUID pointer itself lives in ``pActive`` and is updated there.
    """
    if node.tag not in {"Sm2TiVideoClip", "Sm2VideoClip"}:
        raise ValueError(f"native grade target must be a video clip, got {node.tag!r}")
    body_hex = extract_drx_body(drx_payload).hex()
    version_id = str(uuid.uuid4())
    table_root = node.find("pLmVerTable")
    if table_root is None:
        table_root = ET.SubElement(node, "pLmVerTable")
    table = next((child for child in table_root if child.tag in {"ListMgt::LmVersionTable", "ListMgt__LmVersionTable"}), None)
    version_tag = "ListMgt__LmVersion" if table is not None and table.tag == "ListMgt__LmVersionTable" else "ListMgt::LmVersion"
    if table is None:
        table = ET.SubElement(table_root, "ListMgt::LmVersionTable", {"DbId": str(uuid.uuid4())})
        ET.SubElement(table, "FieldsBlob").text = _VERSION_TABLE_FIELDS_BLOB
        ET.SubElement(table, "VerType").text = "0"
    else:
        if table.find("FieldsBlob") is None:
            fields_blob = ET.Element("FieldsBlob")
            table.insert(0, fields_blob)
            fields_blob.text = _VERSION_TABLE_FIELDS_BLOB
        table_type = table.find("VerType")
        if table_type is None:
            table_type = ET.SubElement(table, "VerType")
        table_type.text = "0"

    active = table.find("pActive")
    if active is None:
        active = ET.Element("pActive")
        version_type = table.find("VerType")
        table.insert(list(table).index(version_type) + 1 if version_type is not None else 0, active)
    active.text = version_id

    locals_element = table.find("Locals")
    if locals_element is None:
        locals_element = ET.SubElement(table, "Locals")
    version_number = sum(1 for elem in locals_element.findall("Element")) + 1
    version_element = ET.SubElement(locals_element, "Element")
    version = ET.SubElement(version_element, version_tag, {"DbId": version_id})
    ET.SubElement(version, "FieldsBlob")
    ET.SubElement(version, "Name").text = f"Version {version_number}"
    ET.SubElement(version, "HasCorrection").text = "true"
    ET.SubElement(version, "VerType").text = "0"
    ET.SubElement(version, "ImplVersion").text = "1"
    ET.SubElement(version, "IncludedInRecording").text = "true"
    ET.SubElement(version, "FlatPassEnabled").text = "false"
    ET.SubElement(version, "RGBAOutputEnabled").text = "false"
    ET.SubElement(version, "Body").text = body_hex
    ET.SubElement(version, "UseVersionClipProcParams").text = "true"
