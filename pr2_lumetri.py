"""Lossless, ordered extraction of Premiere Lumetri component state."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Optional
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class LumetriParameter:
    order: int
    parameter_id: str
    name: str
    control_type: str
    raw_value: str
    encoding: str
    is_time_varying: bool
    lower_bound: str
    upper_bound: str
    start_keyframe_position: str
    start_keyframe_encoding: str
    start_value_encoding: str
    binary_hash: str
    raw_xml: str


@dataclass(frozen=True)
class ShaderParameter:
    order: int
    name: str
    value: str
    raw_xml: str


@dataclass(frozen=True)
class LumetriShader:
    order: int
    name: str
    custom_name: str
    visible: str
    locked_visible: str
    opacity: str
    mask: str
    vector_mask: str
    linked: str
    parameters: tuple[ShaderParameter, ...]
    raw_xml: str


@dataclass(frozen=True)
class LumetriKeyframe:
    order: int
    time: str
    tween: str
    ease_in: str
    ease_out: str
    selected_layer: str
    shaders: tuple[LumetriShader, ...]
    raw_xml: str


@dataclass(frozen=True)
class LumetriBlob:
    raw_base64: str
    decoded_xml: str
    guid: str
    version: str
    name: str
    keyframes: tuple[LumetriKeyframe, ...]
    parse_error: str = ""


@dataclass(frozen=True)
class LumetriEffect:
    order: int
    component_ref: str
    component_uid: str
    match_name: str
    display_name: str
    parameters: tuple[LumetriParameter, ...]
    blob: Optional[LumetriBlob]
    raw_xml: str


@dataclass(frozen=True)
class LumetriClip:
    sequence_uid: str
    clip_name: str
    track_index: int
    item_index: int
    track_item_ref: str
    track_item_uid: str
    start_ticks: str
    end_ticks: str
    effects: tuple[LumetriEffect, ...]


def _text(element: Optional[ET.Element], path: str, default: str = "") -> str:
    if element is None:
        return default
    return element.findtext(path, default)


def _raw(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode")


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _blob_atom(element: ET.Element, path: str) -> str:
    return _unquote(_text(element, path))


def _decode_blob(raw: str) -> LumetriBlob:
    try:
        decoded = base64.b64decode(raw)
        root = ET.fromstring(decoded)
        if root.tag != "Lumetri":
            raise ValueError(f"unexpected blob root {root.tag!r}")
        keyframes: list[LumetriKeyframe] = []
        for kf_order, kf in enumerate(root.findall("keyframe")):
            shaders: list[LumetriShader] = []
            for shader_order, shader in enumerate(kf.findall("shader")):
                params_el = shader.find("parameters")
                shader_params = tuple(
                    ShaderParameter(i, child.tag, _unquote(child.text or ""), _raw(child))
                    for i, child in enumerate(list(params_el) if params_el is not None else [])
                )
                shaders.append(LumetriShader(
                    shader_order, _blob_atom(shader, "name"), _blob_atom(shader, "customname"),
                    _blob_atom(shader, "visible"), _blob_atom(shader, "lockedvisible"),
                    _blob_atom(shader, "opacity"), _blob_atom(shader, "mask"),
                    _blob_atom(shader, "vectormask"), _blob_atom(shader, "linked"),
                    shader_params, _raw(shader),
                ))
            keyframes.append(LumetriKeyframe(
                kf_order, _blob_atom(kf, "time"), _blob_atom(kf, "tween"),
                _blob_atom(kf, "easein"), _blob_atom(kf, "easeout"),
                _blob_atom(kf, "selectedlayer"), tuple(shaders), _raw(kf),
            ))
        return LumetriBlob(
            raw, decoded.decode("utf-8", errors="replace"),
            _blob_atom(root, "guid"), _blob_atom(root, "version"), _blob_atom(root, "name"),
            tuple(keyframes),
        )
    except (binascii.Error, UnicodeError, ET.ParseError, ValueError) as exc:
        return LumetriBlob(raw, "", "", "", "", (), str(exc))


def _objects(root: ET.Element) -> tuple[dict[str, ET.Element], dict[str, ET.Element]]:
    by_id: dict[str, ET.Element] = {}
    by_uid: dict[str, ET.Element] = {}
    for element in root.iter():
        object_id = element.get("ObjectID")
        object_uid = element.get("ObjectUID")
        if object_id:
            by_id[object_id] = element
        if object_uid:
            by_uid[object_uid] = element
    return by_id, by_uid


def _effect_from_component(
    component: ET.Element, component_ref: str, order: int,
    by_id: dict[str, ET.Element],
) -> LumetriEffect:
    params_section = component.find(".//Params")
    parameters: list[LumetriParameter] = []
    blob: Optional[LumetriBlob] = None
    if params_section is not None:
        for param_order, param_ref in enumerate(params_section.findall("Param")):
            param = by_id.get(param_ref.get("ObjectRef", ""))
            if param is None:
                continue
            start = param.find("StartKeyframe")
            start_value = param.find("StartKeyframeValue")
            raw_value = ((start.text or "") if start is not None else "") or (
                (start_value.text or "") if start_value is not None else ""
            )
            parameters.append(LumetriParameter(
                param_order, _text(param, "ParameterID"), _text(param, "Name"),
                _text(param, "ParameterControlType"), raw_value,
                "blob" if start_value is not None else "scalar",
                (_text(param, "IsTimeVarying", "false").lower() == "true" or bool(_text(param, "Keyframes"))),
                _text(param, "LowerBound"), _text(param, "UpperBound"),
                _text(param, "StartKeyframePosition"),
                start.get("Encoding", "") if start is not None else "",
                start_value.get("Encoding", "") if start_value is not None else "",
                start_value.get("BinaryHash", "") if start_value is not None else "",
                _raw(param),
            ))
            if _text(param, "ParameterID") == "1" and start_value is not None:
                blob = _decode_blob(start_value.text or "")
    return LumetriEffect(
        order, component_ref, component.get("ObjectUID", ""),
        _text(component, "MatchName"),
        _text(component, "InstanceName") or _text(component, "Component/DisplayName") or _text(component, "Name"),
        tuple(parameters), blob, _raw(component),
    )


def _effects_for_clip(
    clip_item: ET.Element, by_id: dict[str, ET.Element],
) -> tuple[LumetriEffect, ...]:
    owner = clip_item.find("ComponentOwner/Components")
    chain = by_id.get(owner.get("ObjectRef", "")) if owner is not None else None
    component_refs = chain.findall("ComponentChain/Components/Component") if chain is not None else []
    result: list[LumetriEffect] = []
    for ref in component_refs:
        component_ref = ref.get("ObjectRef", "")
        component = by_id.get(component_ref)
        if component is not None and "Lumetri" in _text(component, "MatchName"):
            result.append(_effect_from_component(component, component_ref, len(result), by_id))
    return tuple(result)


def extract_lumetri_clips(root: ET.Element, sequence_uid: str) -> list[LumetriClip]:
    """Extract Lumetri-bearing clips in sequence, track, item, and effect order."""
    by_id, by_uid = _objects(root)
    sequence = next((el for el in root.findall("Sequence") if el.get("ObjectUID") == sequence_uid), None)
    if sequence is None:
        return []
    result: list[LumetriClip] = []
    for group_ref in sequence.findall("TrackGroups/TrackGroup"):
        second = group_ref.find("Second")
        track_group = by_id.get(second.get("ObjectRef", "")) if second is not None else None
        if track_group is None or track_group.tag != "VideoTrackGroup":
            continue
        for track_index, track_ref in enumerate(track_group.findall("TrackGroup/Tracks/Track"), 1):
            track = by_uid.get(track_ref.get("ObjectURef", ""))
            track_items = track.findall("ClipTrack/.//TrackItems/TrackItem") if track is not None else []
            if not track_items:
                continue
            for item_index, item_ref in enumerate(track_items):
                item_ref_id = item_ref.get("ObjectRef", "")
                item = by_id.get(item_ref_id)
                clip_item = item.find("ClipTrackItem") if item is not None else None
                if clip_item is None:
                    continue
                effects = _effects_for_clip(clip_item, by_id)
                if not effects:
                    continue
                subclip_el = clip_item.find("SubClip")
                subclip_ref = subclip_el.get("ObjectRef", "") if subclip_el is not None else ""
                subclip = by_id.get(subclip_ref)
                range_el = clip_item.find("TrackItem")
                result.append(LumetriClip(
                    sequence_uid, _text(subclip, "Name", "(unknown)"), track_index,
                    item_index, item_ref_id, item.get("ObjectUID", "") if item is not None else "",
                    _text(range_el, "Start"), _text(range_el, "End"), effects,
                ))
    return result
