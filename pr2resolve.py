#!/usr/bin/env python3
"""pr2resolve - Premiere Pro to DaVinci Resolve timeline converter.

Dual-entry (FCP7 XML / .prproj) -> Unified Timeline Model -> FCP7 XML / DRT output.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Optional
from xml.dom import minidom

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "1.0.0"

DEFAULT_FPS = 30.0


NTSC_RATES: list[float] = [23.976, 29.97, 59.94, 47.952]
PAL_RATES: list[float] = [25.0, 50.0]
FPS_TOLERANCE: float = 0.01

# FCP7 XML required version
FCP7_VERSION = "5"

# DOCTYPE declaration
FCP7_DOCTYPE = '<!DOCTYPE xmeml>'

# Severity levels
CRITICAL = "CRITICAL"
MAJOR = "MAJOR"
MINOR = "MINOR"

# Clipitem element order per FCP7 spec
FCP7_CLIPIITEM_ORDER: list[str] = [
    "masterclipid", "name", "enabled", "duration", "rate", "start", "end",
    "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "file",
    "sourcetrack", "filter", "logginginfo", "colorinfo", "labels", "link",
    "comments", "itemhistory",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ScaleIssue:
    """Detected scale mismatch between source and timeline resolution."""

    source_res: tuple[int, int]
    timeline_res: tuple[int, int]
    current_scale: float
    corrected_scale: float


@dataclass
class Issue:
    """A single diagnostics finding."""

    severity: str       # CRITICAL | MAJOR | MINOR
    rule_id: str        # e.g. "C1", "M7", "N1"
    message: str
    element: str        # xpath-like location
    fixed: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# XML Loader & Pre-flight
# ═══════════════════════════════════════════════════════════════════════════════

def load_xml(path: Path) -> ET.Element:
    """Parse an FCP7 XML file and return the root element.

    Args:
        path: Path to the .xml file

    Returns:
        Root <xmeml> element

    Raises:
        FileNotFoundError: File does not exist
        ET.ParseError: Invalid XML
        ValueError: Root tag is not <xmeml>
    """
    tree = ET.parse(str(path))
    root = tree.getroot()
    if root.tag != "xmeml":
        raise ValueError(f"Expected <xmeml> root, got <{root.tag}>")
    return root


def load_prproj(path: Path) -> ET.Element:
    """Parse a .prproj file (gzip-compressed XML) and return the root element.

    Args:
        path: Path to the .prproj file

    Returns:
        Root <PremiereData> element

    Raises:
        FileNotFoundError: File does not exist
        gzip.BadGzipFile: Not a valid gzip file
        ET.ParseError: Invalid XML
    """
    with gzip.open(str(path), "rb") as f:
        xml_bytes = f.read()
    root = ET.fromstring(xml_bytes)
    if root.tag != "PremiereData":
        raise ValueError(f"Expected <PremiereData> root, got <{root.tag}>")
    return root


# ═══════════════════════════════════════════════════════════════════════════════
# .prproj Parser — ObjectID Graph Traversal
# ═══════════════════════════════════════════════════════════════════════════════

# Adobe timebase conversion constant
_ADOBE_TIMEBASE_CONSTANT = 10594584000

# Lumetri parameter name mapping (Chinese → English)
_LUMETRI_PARAM_MAP: dict[str, str] = {
    "色温": "Temperature",
    "色彩": "Tint",
    "曝光": "Exposure",
    "对比度": "Contrast",
    "高光": "Highlights",
    "阴影": "Shadows",
    "白色": "Whites",
    "黑色": "Blacks",
    "饱和度": "Saturation",
    "强度": "Intensity",
    "淡化胶片": "Faded Film",
    "锐化": "Sharpness",
    "降噪": "Noise Reduction",
    "模糊": "Blur",
    "晕影": "Vignette",
}


@dataclass
class _PrprojIndex:
    """Index for fast ObjectID/ObjectUID lookups in a .prproj tree."""

    by_id: dict[str, ET.Element]
    by_uid: dict[str, ET.Element]

    @classmethod
    def build(cls, root: ET.Element) -> _PrprojIndex:
        """Build indices from a <PremiereData> root element.

        Args:
            root: The <PremiereData> root element

        Returns:
            A _PrprojIndex with populated by_id and by_uid dicts
        """
        by_id: dict[str, ET.Element] = {}
        by_uid: dict[str, ET.Element] = {}
        for el in root:
            oid = el.get("ObjectID")
            if oid:
                by_id[oid] = el
            uid = el.get("ObjectUID")
            if uid:
                by_uid[uid] = el
        return cls(by_id=by_id, by_uid=by_uid)

    def resolve_ref(self, ref: str) -> Optional[ET.Element]:
        """Resolve an ObjectRef to its target element.

        Args:
            ref: The ObjectRef string

        Returns:
            The referenced element, or None if not found
        """
        return self.by_id.get(ref)

    def resolve_uref(self, uref: str) -> Optional[ET.Element]:
        """Resolve an ObjectURef to its target element.

        Args:
            uref: The ObjectURef string

        Returns:
            The referenced element, or None if not found
        """
        return self.by_uid.get(uref)


def _prproj_adobe_timebase_to_fps(timebase: int) -> float:
    """Convert Adobe internal timebase to actual fps.

    Args:
        timebase: Adobe internal timebase value

    Returns:
        Actual frames per second
    """
    if timebase <= 0:
        return DEFAULT_FPS
    fps = round((_ADOBE_TIMEBASE_CONSTANT * 23.976) / timebase, 3)
    return fps if fps > 0 else DEFAULT_FPS


def _prproj_ticks_to_frames(ticks_str: str, fps: float) -> int:
    """Convert Adobe pproTicks to FCP7 frame count.

    Args:
        ticks_str: Ticks value as string
        fps: Actual fps

    Returns:
        Frame count (integer)
    """
    try:
        ticks = int(ticks_str)
    except (ValueError, TypeError):
        return 0
    if ticks <= 0:
        return 0
    # pproTicks per second = 254016000000
    ppro_ticks_per_sec = 254016000000
    seconds = ticks / ppro_ticks_per_sec
    return int(round(seconds * fps))


def _prproj_list_sequences(root: ET.Element, idx: _PrprojIndex) -> list[dict]:
    """List all sequences in a .prproj file.

    Args:
        root: The <PremiereData> root element
        idx: The prproj index

    Returns:
        List of dicts with keys: uid, name, width, height, clip_count
    """
    sequences = []
    for seq_el in root.findall("Sequence"):
        uid = seq_el.get("ObjectUID", "")
        name = seq_el.findtext("Name", "(unnamed)")

        # Resolution from MZ.Sequence.PreviewFrameSize*
        w = h = 0
        props = seq_el.find(".//Properties")
        if props is not None:
            for p in props:
                if "PreviewFrameSizeWidth" in p.tag:
                    w = int(p.text or "0")
                if "PreviewFrameSizeHeight" in p.tag:
                    h = int(p.text or "0")

        # Count clips in first video track
        clip_count = 0
        tg_section = seq_el.find("TrackGroups")
        if tg_section is not None:
            for tg_pair in tg_section.findall("TrackGroup"):
                second = tg_pair.find("Second")
                if second is not None:
                    ref = second.get("ObjectRef")
                    tg_el = idx.resolve_ref(ref) if ref else None
                    if tg_el is not None and tg_el.tag == "VideoTrackGroup":
                        tracks = tg_el.find(".//Tracks")
                        if tracks is not None:
                            first_track = tracks.find("Track")
                            if first_track is not None:
                                uref = first_track.get("ObjectURef")
                                track_el = idx.resolve_uref(uref) if uref else None
                                if track_el is not None:
                                    items = track_el.find(".//TrackItems")
                                    if items is not None:
                                        clip_count = len(items.findall("TrackItem"))
                        break

        sequences.append({
            "uid": uid,
            "name": name,
            "width": w,
            "height": h,
            "clip_count": clip_count,
        })
    return sequences


def _prproj_extract_lumetri(
    idx: _PrprojIndex,
    component_chain_id: str,
) -> dict[str, float]:
    """Extract Lumetri parameters from a VideoComponentChain.

    Args:
        idx: The prproj index
        component_chain_id: ObjectID of the VideoComponentChain

    Returns:
        Dict mapping parameter name (Chinese) to float value
    """
    params: dict[str, float] = {}
    chain = idx.resolve_ref(component_chain_id)
    if chain is None:
        return params

    # Components are nested inside ComponentChain/Components
    inner_cc = chain.find("ComponentChain")
    if inner_cc is None:
        return params
    comps = inner_cc.find("Components")
    if comps is None:
        return params

    for comp_ref in comps.findall("Component"):
        ref = comp_ref.get("ObjectRef")
        if not ref:
            continue
        comp = idx.resolve_ref(ref)
        if comp is None:
            continue

        # Check if this is a Lumetri filter
        match_name = comp.findtext("MatchName", "")
        if "Lumetri" not in match_name:
            continue

        # Extract params
        params_section = comp.find(".//Params")
        if params_section is None:
            continue

        for param_ref in params_section.findall("Param"):
            pref = param_ref.get("ObjectRef")
            if not pref:
                continue
            param = idx.resolve_ref(pref)
            if param is None:
                continue

            pname = param.findtext("Name", "")
            sk = param.findtext("StartKeyframe", "")
            if not pname or not sk:
                continue

            # Parse StartKeyframe: "ticks,value,..." — value is field[1]
            parts = sk.split(",")
            if len(parts) >= 2:
                try:
                    val = float(parts[1])
                    params[pname] = val
                except ValueError:
                    pass

    return params


def _prproj_extract_all_lumetri(
    prproj_root: ET.Element,
    sequence_uid: str,
) -> dict[str, dict[str, float]]:
    """Extract Lumetri parameters for all clips in a sequence.

    Walks the Sequence → TrackGroup → TrackItem → SubClip → ComponentChain
    reference chain and calls _prproj_extract_lumetri on each clip's chain.

    Args:
        prproj_root: The <PremiereData> root element
        sequence_uid: ObjectUID of the target sequence

    Returns:
        Dict mapping clip name → {param_name: value}
    """
    idx = _PrprojIndex.build(prproj_root)
    lumetri_data: dict[str, dict[str, float]] = {}

    seq_el = None
    for s in prproj_root.findall("Sequence"):
        if s.get("ObjectUID") == sequence_uid:
            seq_el = s
            break
    if seq_el is None:
        return lumetri_data

    tg_section = seq_el.find("TrackGroups")
    if tg_section is None:
        return lumetri_data

    for tg_pair in tg_section.findall("TrackGroup"):
        second = tg_pair.find("Second")
        if second is None:
            continue
        ref = second.get("ObjectRef")
        tg_el = idx.resolve_ref(ref) if ref else None
        if tg_el is None or tg_el.tag != "VideoTrackGroup":
            continue

        tracks_el = tg_el.find("TrackGroup/Tracks")
        if tracks_el is None:
            continue

        for track_ref in tracks_el.findall("Track"):
            uref = track_ref.get("ObjectURef")
            track_el = idx.resolve_uref(uref) if uref else None
            if track_el is None:
                continue

            ct = track_el.find("ClipTrack")
            if ct is None:
                continue
            ti_section = ct.find(".//TrackItems")
            if ti_section is None:
                continue

            for ti_ref in ti_section.findall("TrackItem"):
                ref = ti_ref.get("ObjectRef")
                ti_el = idx.resolve_ref(ref) if ref else None
                if ti_el is None:
                    continue

                cti = ti_el.find("ClipTrackItem")
                if cti is None:
                    continue

                # Get clip name from SubClip
                subclip = cti.find("SubClip")
                clip_name = "(unknown)"
                if subclip is not None:
                    sc_el = idx.resolve_ref(subclip.get("ObjectRef"))
                    if sc_el is not None:
                        clip_name = sc_el.findtext("Name", clip_name)

                # Get ComponentChain → Lumetri params
                co = cti.find("ComponentOwner")
                if co is not None:
                    comps = co.find("Components")
                    if comps is not None:
                        chain_ref = comps.get("ObjectRef")
                        if chain_ref:
                            params = _prproj_extract_lumetri(idx, chain_ref)
                            if params:
                                lumetri_data[clip_name] = params

    return lumetri_data


def _prproj_parse_sequence(
    prproj_root: ET.Element,
    sequence_uid: str,
    input_path: Path,
) -> ET.Element:
    """Convert a .prproj Sequence into an FCP7 XML <xmeml> tree.

    This is the core Entry B conversion: .prproj → unified FCP7 XML.

    Args:
        prproj_root: The <PremiereData> root element
        sequence_uid: ObjectUID of the sequence to convert
        input_path: Path to the .prproj file (for pathurl generation)

    Returns:
        An <xmeml> root element in FCP7 XML format
    """
    idx = _PrprojIndex.build(prproj_root)

    # Find the sequence
    seq_el = None
    for s in prproj_root.findall("Sequence"):
        if s.get("ObjectUID") == sequence_uid:
            seq_el = s
            break
    if seq_el is None:
        raise ValueError(f"Sequence not found: {sequence_uid}")

    seq_name = seq_el.findtext("Name", "(unnamed)")

    # Get resolution
    w, h = 1920, 1080
    props = seq_el.find(".//Properties")
    if props is not None:
        for p in props:
            if "PreviewFrameSizeWidth" in p.tag:
                w = int(p.text or "1920")
            if "PreviewFrameSizeHeight" in p.tag:
                h = int(p.text or "1080")

    # Get fps from first VideoTrackGroup
    fps = DEFAULT_FPS
    internal_tb = 0
    tg_section = seq_el.find("TrackGroups")
    video_tg = None
    audio_tg = None
    if tg_section is not None:
        for tg_pair in tg_section.findall("TrackGroup"):
            second = tg_pair.find("Second")
            if second is not None:
                ref = second.get("ObjectRef")
                tg_el = idx.resolve_ref(ref) if ref else None
                if tg_el is not None:
                    if tg_el.tag == "VideoTrackGroup":
                        video_tg = tg_el
                        fr = tg_el.find(".//FrameRate")
                        if fr is not None and fr.text:
                            try:
                                internal_tb = int(fr.text)
                                fps = _prproj_adobe_timebase_to_fps(internal_tb)
                            except ValueError:
                                pass
                    elif tg_el.tag == "AudioTrackGroup":
                        audio_tg = tg_el

    # NTSC detection: use actual fps with tolerance matching
    is_ntsc = _is_ntsc_fps(fps)
    timebase = int(round(fps))

    # Build FCP7 XML tree
    xmeml = ET.Element("xmeml")
    xmeml.set("version", FCP7_VERSION)

    sequence = ET.SubElement(xmeml, "sequence")
    sequence.set("id", "sequence-1")
    sequence.set("MZ.Sequence.PreviewFrameSizeWidth", str(w))
    sequence.set("MZ.Sequence.PreviewFrameSizeHeight", str(h))

    # Sequence duration — sum of first video track clip durations
    total_frames = 0

    dur_elem = ET.SubElement(sequence, "duration")
    rate_elem = ET.SubElement(sequence, "rate")
    tb_elem = ET.SubElement(rate_elem, "timebase")
    tb_elem.text = str(timebase)
    if is_ntsc:
        ntsc_elem = ET.SubElement(rate_elem, "ntsc")
        ntsc_elem.text = "TRUE"

    name_elem = ET.SubElement(sequence, "name")
    name_elem.text = seq_name

    # Timecode
    tc = ET.SubElement(sequence, "timecode")
    tc_rate = ET.SubElement(tc, "rate")
    tc_tb = ET.SubElement(tc_rate, "timebase")
    tc_tb.text = str(timebase)
    if is_ntsc:
        tc_ntsc = ET.SubElement(tc_rate, "ntsc")
        tc_ntsc.text = "TRUE"
    tc_str = ET.SubElement(tc, "string")
    tc_str.text = "00;00;00;00" if is_ntsc else "00:00:00:00"
    tc_frame = ET.SubElement(tc, "frame")
    tc_frame.text = "0"
    tc_df = ET.SubElement(tc, "displayformat")
    tc_df.text = "DF" if is_ntsc else "NDF"

    media = ET.SubElement(sequence, "media")
    video_section = ET.SubElement(media, "video")
    audio_section = ET.SubElement(media, "audio")

    # Video format
    vfmt = ET.SubElement(video_section, "format")
    vsc = ET.SubElement(vfmt, "samplecharacteristics")
    vrate = ET.SubElement(vsc, "rate")
    vtb = ET.SubElement(vrate, "timebase")
    vtb.text = str(timebase)
    if is_ntsc:
        vntsc = ET.SubElement(vrate, "ntsc")
        vntsc.text = "TRUE"
    vw = ET.SubElement(vsc, "width")
    vw.text = str(w)
    vh = ET.SubElement(vsc, "height")
    vh.text = str(h)

    # Audio format
    afmt = ET.SubElement(audio_section, "format")
    asc = ET.SubElement(afmt, "samplecharacteristics")
    asr = ET.SubElement(asc, "samplerate")
    asr.text = "48000"

    # Parse video tracks
    file_counter = [1]
    mc_counter = [1]

    def _next_file_id() -> str:
        fid = f"file-{file_counter[0]}"
        file_counter[0] += 1
        return fid

    def _next_mc_id() -> str:
        mid = f"masterclip-{mc_counter[0]}"
        mc_counter[0] += 1
        return mid

    if video_tg is not None:
        tracks_el = video_tg.find("TrackGroup/Tracks")
        if tracks_el is not None:
            for track_ref in tracks_el.findall("Track"):
                uref = track_ref.get("ObjectURef")
                track_el = idx.resolve_uref(uref) if uref else None
                if track_el is None:
                    continue

                fcp_track = ET.SubElement(video_section, "track")
                ct = track_el.find("ClipTrack")
                if ct is None:
                    continue
                ti_section = ct.find(".//TrackItems")
                if ti_section is None:
                    continue

                track_start = 0
                for ti_ref in ti_section.findall("TrackItem"):
                    ref = ti_ref.get("ObjectRef")
                    ti_el = idx.resolve_ref(ref) if ref else None
                    if ti_el is None:
                        continue

                    cti = ti_el.find("ClipTrackItem")
                    if cti is None:
                        continue

                    # Timeline position
                    ti_inner = cti.find("TrackItem")
                    start = track_start
                    end = 0
                    if ti_inner is not None:
                        s = ti_inner.findtext("Start")
                        e = ti_inner.findtext("End")
                        if s:
                            start = _prproj_ticks_to_frames(s, fps)
                        if e:
                            end = _prproj_ticks_to_frames(e, fps)
                    track_start = end
                    total_frames = max(total_frames, end)

                    # SubClip → MasterClip + Clip data
                    subclip = cti.find("SubClip")
                    mc_name = "(unknown)"
                    in_point = 0
                    out_point = 0
                    playback_speed = 100
                    media_path = ""
                    src_w, src_h = w, h  # default to timeline

                    if subclip is not None:
                        sc_ref = subclip.get("ObjectRef")
                        sc_el = idx.resolve_ref(sc_ref) if sc_ref else None
                        if sc_el is not None:
                            mc_name = sc_el.findtext("Name", mc_name)

                            # MasterClip → Media (ObjectUID lookup for file path + resolution)
                            mc_uref_el = sc_el.find("MasterClip")
                            if mc_uref_el is not None:
                                mc_uref = mc_uref_el.get("ObjectURef")
                                mc_el = idx.resolve_uref(mc_uref) if mc_uref else None
                                if mc_el is not None:
                                    # Get file path from Media element
                                    for media_el in prproj_root.findall("Media"):
                                        mfp = media_el.findtext("FilePath")
                                        if not mfp:
                                            continue
                                        # Match by filename: Media's FilePath vs SubClip's Name
                                        media_filename = PureWindowsPath(mfp).name.lower()
                                        subclip_name = sc_el.findtext("Name", "").lower()
                                        if media_filename == subclip_name:
                                            media_path = mfp
                                            break

                            # Clip → InPoint/OutPoint/PlaybackSpeed
                            clip_ref_el = sc_el.find("Clip")
                            if clip_ref_el is not None:
                                clip_ref = clip_ref_el.get("ObjectRef")
                                clip_el = idx.resolve_ref(clip_ref) if clip_ref else None
                                if clip_el is not None:
                                    # InPoint/OutPoint are inside nested <Clip> element
                                    inner_clip = clip_el.find("Clip")
                                    if inner_clip is not None:
                                        ip = inner_clip.findtext("InPoint")
                                        op = inner_clip.findtext("OutPoint")
                                        # InPoint=0 is valid (clip starts at media beginning)
                                        # None means "no trimming info" → use default 0
                                        if ip is not None:
                                            in_point = _prproj_ticks_to_frames(ip, fps)
                                        if op is not None:
                                            out_point = _prproj_ticks_to_frames(op, fps)
                                    ps = clip_el.findtext("PlaybackSpeed")
                                    if ps:
                                        try:
                                            playback_speed = int(float(ps))
                                        except ValueError:
                                            pass

                    # ComponentOwner → transform data
                    co = cti.find("ComponentOwner")
                    scale_val = 100.0
                    rotation_val = 0.0
                    has_motion = False
                    if co is not None:
                        comps = co.find("Components")
                        if comps is not None:
                            chain_ref = comps.get("ObjectRef")
                            chain = idx.resolve_ref(chain_ref) if chain_ref else None
                            if chain is not None:
                                dm = chain.find("DefaultMotion")
                                if dm is not None and dm.text == "false":
                                    has_motion = True
                                    # Extract actual transform params from VideoComponentParam
                                    chain_comps = chain.find("Components")
                                    if chain_comps is not None:
                                        for c in chain_comps.findall("Component"):
                                            c_ref = c.get("ObjectRef")
                                            c_el = idx.resolve_ref(c_ref) if c_ref else None
                                            if c_el is None:
                                                continue
                                            inner_comps = c_el.find(".//Params")
                                            if inner_comps is None:
                                                continue
                                            for p_ref in inner_comps.findall("Param"):
                                                p_el = idx.resolve_ref(p_ref.get("ObjectRef", "")) if p_ref.get("ObjectRef") else None
                                                if p_el is None:
                                                    continue
                                                pname = p_el.findtext("Name", "")
                                                sk = p_el.findtext("StartKeyframe", "")
                                                if not pname or not sk:
                                                    continue
                                                parts = sk.split(",")
                                                if len(parts) >= 2:
                                                    try:
                                                        val = float(parts[1])
                                                    except ValueError:
                                                        continue
                                                    if pname == "Scale":
                                                        scale_val = val
                                                        has_motion = True
                                                    elif pname == "Rotation":
                                                        rotation_val = val
                                                        has_motion = True

                    # Build clipitem
                    clipitem = ET.SubElement(fcp_track, "clipitem")
                    clipitem.set("id", f"clipitem-{file_counter[0]}")

                    mcid = ET.SubElement(clipitem, "masterclipid")
                    mcid.text = _next_mc_id()

                    nm = ET.SubElement(clipitem, "name")
                    nm.text = mc_name

                    en = ET.SubElement(clipitem, "enabled")
                    en.text = "TRUE"

                    dur = ET.SubElement(clipitem, "duration")
                    dur.text = str(out_point - in_point if out_point > in_point else end - start)

                    ci_rate = ET.SubElement(clipitem, "rate")
                    ci_tb = ET.SubElement(ci_rate, "timebase")
                    ci_tb.text = str(timebase)
                    if is_ntsc:
                        ci_ntsc = ET.SubElement(ci_rate, "ntsc")
                        ci_ntsc.text = "TRUE"

                    st_el = ET.SubElement(clipitem, "start")
                    st_el.text = str(start)
                    en_el = ET.SubElement(clipitem, "end")
                    en_el.text = str(end)
                    in_el = ET.SubElement(clipitem, "in")
                    in_el.text = str(in_point)
                    out_el = ET.SubElement(clipitem, "out")
                    out_el.text = str(out_point)
                    # Standard clipitem metadata
                    alpha_el = ET.SubElement(clipitem, "alphatype")
                    alpha_el.text = "none"
                    par_el = ET.SubElement(clipitem, "pixelaspectratio")
                    par_el.text = "square"
                    ana_el = ET.SubElement(clipitem, "anamorphic")
                    ana_el.text = "FALSE"

                    # File element
                    fid = _next_file_id()
                    file_el = ET.SubElement(clipitem, "file")
                    file_el.set("id", fid)
                    fn = ET.SubElement(file_el, "name")
                    fn.text = mc_name
                    if media_path:
                        pu = ET.SubElement(file_el, "pathurl")
                        pu.text = Path(media_path).as_uri()
                    else:
                        pu = ET.SubElement(file_el, "pathurl")
                        pu.text = f"file:///{mc_name}"

                    # File rate (source media timebase)
                    f_rate = ET.SubElement(file_el, "rate")
                    fr_tb = ET.SubElement(f_rate, "timebase")
                    fr_tb.text = str(timebase)
                    if is_ntsc:
                        fr_ntsc = ET.SubElement(f_rate, "ntsc")
                        fr_ntsc.text = "TRUE"

                    # File duration
                    fd = ET.SubElement(file_el, "duration")
                    fd.text = str(out_point - in_point if out_point > in_point else end - start)

                    # File timecode
                    f_tc = ET.SubElement(file_el, "timecode")
                    ftc_rate = ET.SubElement(f_tc, "rate")
                    ftc_tb = ET.SubElement(ftc_rate, "timebase")
                    ftc_tb.text = str(timebase)
                    if is_ntsc:
                        ftc_ntsc = ET.SubElement(ftc_rate, "ntsc")
                        ftc_ntsc.text = "TRUE"
                    ftc_str = ET.SubElement(f_tc, "string")
                    ftc_str.text = "00;00;00;00" if is_ntsc else "00:00:00:00"
                    ftc_frame = ET.SubElement(f_tc, "frame")
                    ftc_frame.text = "0"
                    ftc_df = ET.SubElement(f_tc, "displayformat")
                    ftc_df.text = "DF" if is_ntsc else "NDF"

                    # Media details (full structure matching PR FCP7 XML)
                    media_el = ET.SubElement(file_el, "media")

                    # Video
                    video_el = ET.SubElement(media_el, "video")
                    vsc = ET.SubElement(video_el, "samplecharacteristics")
                    # rate
                    vsc_rate = ET.SubElement(vsc, "rate")
                    vsc_tb = ET.SubElement(vsc_rate, "timebase")
                    vsc_tb.text = str(timebase)
                    if is_ntsc:
                        vsc_ntsc = ET.SubElement(vsc_rate, "ntsc")
                        vsc_ntsc.text = "TRUE"
                    # Resolution
                    vsc_w = ET.SubElement(vsc, "width")
                    vsc_w.text = str(src_w)
                    vsc_h = ET.SubElement(vsc, "height")
                    vsc_h.text = str(src_h)
                    vsc_ana = ET.SubElement(vsc, "anamorphic")
                    vsc_ana.text = "FALSE"
                    vsc_par = ET.SubElement(vsc, "pixelaspectratio")
                    vsc_par.text = "square"
                    vsc_fd = ET.SubElement(vsc, "fielddominance")
                    vsc_fd.text = "none"
                    vsc_cd = ET.SubElement(vsc, "colordepth")
                    vsc_cd.text = "24"

                    # Audio
                    ael = ET.SubElement(media_el, "audio")
                    asc = ET.SubElement(ael, "samplecharacteristics")
                    asr = ET.SubElement(asc, "samplerate")
                    asr.text = "48000"
                    ach = ET.SubElement(asc, "channelcount")
                    ach.text = "2"

                    # Sourcetrack
                    sourcetrack = ET.SubElement(clipitem, "sourcetrack")
                    stype = ET.SubElement(sourcetrack, "mediatype")
                    stype.text = "video"
                    stt = ET.SubElement(sourcetrack, "tracktype")
                    stt.text = "Video"

                    # Basic effect (transform) if non-default
                    if has_motion and (abs(scale_val - 100.0) > 0.01 or abs(rotation_val) > 0.01):
                        filt = ET.SubElement(clipitem, "filter")
                        eff = ET.SubElement(filt, "effect")
                        eid = ET.SubElement(eff, "effectid")
                        eid.text = "basic"
                        ename = ET.SubElement(eff, "name")
                        ename.text = "Basic Motion"
                        etype = ET.SubElement(eff, "effecttype")
                        etype.text = "motion"
                        mt = ET.SubElement(eff, "mediatype")
                        mt.text = "video"

                        sp = ET.SubElement(eff, "parameter")
                        sn = ET.SubElement(sp, "name")
                        sn.text = "Scale"
                        sv = ET.SubElement(sp, "value")
                        sv.text = str(scale_val)

                        rp = ET.SubElement(eff, "parameter")
                        rn = ET.SubElement(rp, "name")
                        rn.text = "Rotation"
                        rv = ET.SubElement(rp, "value")
                        rv.text = str(rotation_val)

    # Parse audio tracks
    if audio_tg is not None:
        a_tracks_el = audio_tg.find("TrackGroup/Tracks")
        if a_tracks_el is not None:
            for a_track_ref in a_tracks_el.findall("Track"):
                a_uref = a_track_ref.get("ObjectURef")
                a_track_el = idx.resolve_uref(a_uref) if a_uref else None
                if a_track_el is None:
                    continue

                fcp_a_track = ET.SubElement(audio_section, "track")
                a_ct = a_track_el.find("ClipTrack")
                if a_ct is None:
                    continue
                a_ti_section = a_ct.find(".//TrackItems")
                if a_ti_section is None:
                    continue

                a_track_start = 0
                for a_ti_ref in a_ti_section.findall("TrackItem"):
                    a_ref = a_ti_ref.get("ObjectRef")
                    a_ti_el = idx.resolve_ref(a_ref) if a_ref else None
                    if a_ti_el is None:
                        continue

                    a_cti = a_ti_el.find("ClipTrackItem")
                    if a_cti is None:
                        continue

                    # Timeline position
                    a_ti_inner = a_cti.find("TrackItem")
                    a_start = a_track_start
                    a_end = 0
                    if a_ti_inner is not None:
                        a_s = a_ti_inner.findtext("Start")
                        a_e = a_ti_inner.findtext("End")
                        if a_s:
                            a_start = _prproj_ticks_to_frames(a_s, fps)
                        if a_e:
                            a_end = _prproj_ticks_to_frames(a_e, fps)
                    a_track_start = a_end

                    # SubClip → name, media path, InPoint/OutPoint
                    a_subclip = a_cti.find("SubClip")
                    a_mc_name = "(unknown audio)"
                    a_media_path = ""
                    a_in = 0
                    a_out = 0

                    if a_subclip is not None:
                        a_sc_ref = a_subclip.get("ObjectRef")
                        a_sc_el = idx.resolve_ref(a_sc_ref) if a_sc_ref else None
                        if a_sc_el is not None:
                            a_mc_name = a_sc_el.findtext("Name", a_mc_name)

                            # Media path via filename match
                            a_mc_uref_el = a_sc_el.find("MasterClip")
                            if a_mc_uref_el is not None:
                                for media_el in prproj_root.findall("Media"):
                                    mfp = media_el.findtext("FilePath")
                                    if not mfp:
                                        continue
                                    if PureWindowsPath(mfp).name.lower() == a_mc_name.lower():
                                        a_media_path = mfp
                                        break

                            # Clip → InPoint/OutPoint
                            a_clip_ref_el = a_sc_el.find("Clip")
                            if a_clip_ref_el is not None:
                                a_clip_el = idx.resolve_ref(a_clip_ref_el.get("ObjectRef"))
                                if a_clip_el is not None:
                                    a_inner = a_clip_el.find("Clip")
                                    if a_inner is not None:
                                        a_ip = a_inner.findtext("InPoint")
                                        a_op = a_inner.findtext("OutPoint")
                                        if a_ip is not None:
                                            a_in = _prproj_ticks_to_frames(a_ip, fps)
                                        if a_op is not None:
                                            a_out = _prproj_ticks_to_frames(a_op, fps)

                    # Build audio clipitem
                    a_ci = ET.SubElement(fcp_a_track, "clipitem")
                    a_ci.set("id", f"clipitem-{file_counter[0]}")

                    a_mcid = ET.SubElement(a_ci, "masterclipid")
                    a_mcid.text = _next_mc_id()

                    a_nm = ET.SubElement(a_ci, "name")
                    a_nm.text = a_mc_name

                    a_en = ET.SubElement(a_ci, "enabled")
                    a_en.text = "TRUE"

                    a_dur = ET.SubElement(a_ci, "duration")
                    a_dur.text = str(a_out - a_in if a_out > a_in else a_end - a_start)

                    a_rate = ET.SubElement(a_ci, "rate")
                    a_rt = ET.SubElement(a_rate, "timebase")
                    a_rt.text = str(timebase)
                    if is_ntsc:
                        a_rn = ET.SubElement(a_rate, "ntsc")
                        a_rn.text = "TRUE"

                    a_st = ET.SubElement(a_ci, "start")
                    a_st.text = str(a_start)
                    a_en_el = ET.SubElement(a_ci, "end")
                    a_en_el.text = str(a_end)
                    a_in_el = ET.SubElement(a_ci, "in")
                    a_in_el.text = str(a_in)
                    a_out_el = ET.SubElement(a_ci, "out")
                    a_out_el.text = str(a_out)

                    # File element
                    a_fid = _next_file_id()
                    a_file = ET.SubElement(a_ci, "file")
                    a_file.set("id", a_fid)
                    a_fn = ET.SubElement(a_file, "name")
                    a_fn.text = a_mc_name
                    if a_media_path:
                        a_pu = ET.SubElement(a_file, "pathurl")
                        a_pu.text = Path(a_media_path).as_uri()
                    else:
                        a_pu = ET.SubElement(a_file, "pathurl")
                        a_pu.text = f"file:///{a_mc_name}"

                    # Sourcetrack (audio)
                    a_st_el = ET.SubElement(a_ci, "sourcetrack")
                    a_stype = ET.SubElement(a_st_el, "mediatype")
                    a_stype.text = "audio"
                    a_stt = ET.SubElement(a_st_el, "tracktype")
                    a_stt.text = "Stereo"

    # Set total duration
    dur_elem.text = str(total_frames if total_frames > 0 else end)

    return xmeml


def _build_file_index(root: ET.Element) -> dict[str, ET.Element]:
    """Build a mapping from file element id to the <file> element.

    Args:
        root: The <xmeml> root element

    Returns:
        Dict mapping file id string → <file> Element
    """
    index: dict[str, ET.Element] = {}
    for file_elem in root.iter("file"):
        fid = file_elem.get("id")
        if fid:
            index[fid] = file_elem
    return index


def _get_sequence_format(root: ET.Element) -> Optional[ET.Element]:
    """Get the sequence-level video format element.

    Args:
        root: The <xmeml> root element

    Returns:
        The <format> element under sequence/media/video, or None
    """
    seq = root.find("sequence")
    if seq is None:
        return None
    return seq.find("media/video/format")


def _get_sequence_resolution(root: ET.Element) -> tuple[int, int]:
    """Get the sequence (timeline) resolution.

    Args:
        root: The <xmeml> root element

    Returns:
        (width, height) tuple, or (0, 0) if not found
    """
    fmt = _get_sequence_format(root)
    if fmt is None:
        return (0, 0)
    sc = fmt.find("samplecharacteristics")
    if sc is None:
        return (0, 0)
    w = int(sc.findtext("width") or "0")
    h = int(sc.findtext("height") or "0")
    return (w, h)


def _is_ntsc_fps(fps: float) -> bool:
    """Check if an actual fps value is an NTSC rate using tolerance matching.

    PAL rates (25, 50) are excluded first to avoid false positives.

    Args:
        fps: Actual frames-per-second value (e.g. 29.97, 25.0, 30.0)

    Returns:
        True if fps matches an NTSC rate within FPS_TOLERANCE
    """
    for pal in PAL_RATES:
        if abs(fps - pal) < FPS_TOLERANCE:
            return False
    return any(abs(fps - rate) < FPS_TOLERANCE for rate in NTSC_RATES)


def _is_ntsc_timebase(timebase: float) -> bool:
    """Fallback: guess NTSC from integer timebase alone.

    Only use this when actual fps is unavailable (e.g. PR FCP7 XML export
    which only stores integer timebase like 24/30/60). Returns True for
    24/30/60 but warns that this is ambiguous:
    - 24 could be cinema (24.000) or NTSC (23.976)
    - 30 could be web (30.000) or NTSC (29.97)
    - 60 could be game (60.000) or NTSC (59.94)

    Prefer _is_ntsc_fps when .prproj data is available.

    Args:
        timebase: The integer timebase value

    Returns:
        True if timebase is a common NTSC indicator
    """
    return timebase in [24, 30, 60]


# ═══════════════════════════════════════════════════════════════════════════════
# Diagnostics Engine
# ═══════════════════════════════════════════════════════════════════════════════

def _scan(root: ET.Element) -> list[Issue]:
    """Scan the FCP7 XML tree for all known issues.

    Args:
        root: The <xmeml> root element

    Returns:
        List of Issue objects, sorted by severity (CRITICAL first)
    """
    issues: list[Issue] = []
    issues.extend(_scan_critical(root))
    issues.extend(_scan_major(root))
    issues.extend(_scan_minor(root))
    # Sort: CRITICAL > MAJOR > MINOR
    severity_order = {CRITICAL: 0, MAJOR: 1, MINOR: 2}
    issues.sort(key=lambda i: severity_order.get(i.severity, 3))
    return issues


def _scan_critical(root: ET.Element) -> list[Issue]:
    """Scan for CRITICAL issues (C1-C7).

    Args:
        root: The <xmeml> root element

    Returns:
        List of CRITICAL Issue objects
    """
    issues: list[Issue] = []

    # C0: xmeml version should be "5" for FCP7
    version = root.get("version", "")
    if version != FCP7_VERSION:
        issues.append(Issue(
            CRITICAL, "C0",
            f'xmeml version="{version}" should be "{FCP7_VERSION}"',
            "/xmeml/@version",
        ))

    # C1: Missing <format> under video
    seq = root.find("sequence")
    if seq is not None:
        vformat = seq.find("media/video/format")
        if vformat is None:
            issues.append(Issue(
                CRITICAL, "C1",
                "Missing <format> under sequence/media/video",
                "sequence/media/video",
            ))

        # C2: Missing <format> under audio
        aformat = seq.find("media/audio/format")
        if aformat is None:
            issues.append(Issue(
                CRITICAL, "C2",
                "Missing <format> under sequence/media/audio",
                "sequence/media/audio",
            ))

    # C3, C4: Rate issues — check all <rate> elements
    for rate_elem in root.iter("rate"):
        parent_tag = "unknown"
        # Try to get parent context
        # We'll check timebase and ntsc
        has_timebase = rate_elem.find("timebase") is not None
        has_ntsc = rate_elem.find("ntsc") is not None

        if not has_timebase:
            issues.append(Issue(
                CRITICAL, "C4",
                "<rate> missing <timebase>",
                f"rate (parent context unavailable in iter)",
            ))
        if not has_ntsc and has_timebase:
            tb_val = rate_elem.findtext("timebase")
            if tb_val and _is_ntsc_timebase(float(tb_val)):
                issues.append(Issue(
                    MAJOR, "C3",
                    "<rate> missing <ntsc> (NTSC timebase)",
                    f"rate (parent context unavailable in iter)",
                ))

    # C5: pathurl format — should be file:///
    for pathurl_elem in root.iter("pathurl"):
        url = pathurl_elem.text or ""
        if url and not url.startswith("file:///"):
            issues.append(Issue(
                CRITICAL, "C5",
                f'pathurl "{url[:60]}..." is not file:/// format',
                "file/pathurl",
            ))

    # C6: media child order (video before audio)
    seq = root.find("sequence")
    if seq is not None:
        media = seq.find("media")
        if media is not None:
            children = [c.tag for c in media]
            if "video" in children and "audio" in children:
                if children.index("video") > children.index("audio"):
                    issues.append(Issue(
                        CRITICAL, "C6",
                        "<media> children: video should come before audio",
                        "sequence/media",
                    ))

    # C7: Missing DOCTYPE — checked during load, but report it
    # We can't detect this from ElementTree (it strips DOCTYPE), so we always add it on output
    # Report as informational if the file was loaded without DOCTYPE
    # Actually we can't detect this post-parse. Skip for now — we always output DOCTYPE.

    return issues


def _scan_major(root: ET.Element) -> list[Issue]:
    """Scan for MAJOR issues (M1-M7, M0).

    Args:
        root: The <xmeml> root element

    Returns:
        List of MAJOR Issue objects
    """
    issues: list[Issue] = []
    file_index = _build_file_index(root)
    seq_format = _get_sequence_format(root)

    for clipitem in root.iter("clipitem"):
        ci_name = clipitem.findtext("name", "unknown")
        ci_id = clipitem.get("id", "?")
        location = f"clipitem[{ci_id}] ({ci_name})"

        # M1: Missing <masterclipid>
        if clipitem.find("masterclipid") is None:
            issues.append(Issue(
                MAJOR, "M1",
                "Missing <masterclipid>",
                location,
            ))

        # M2: Missing <sourcetrack>
        if clipitem.find("sourcetrack") is None:
            issues.append(Issue(
                MAJOR, "M2",
                "Missing <sourcetrack>",
                location,
            ))

        # M5: <file> missing media details
        file_elem = clipitem.find("file")
        if file_elem is not None:
            sc = file_elem.find("media/video/samplecharacteristics")
            if sc is None:
                issues.append(Issue(
                    MAJOR, "M5",
                    "<file> missing media/video/samplecharacteristics",
                    location,
                ))

        # M0: Lumetri filter present (XML path — should be removed)
        for filt in clipitem.findall("filter"):
            eff = filt.find("effect")
            if eff is not None and eff.findtext("effectid") == "Lumetri":
                issues.append(Issue(
                    MAJOR, "M0",
                    "Lumetri filter block present — DaVinci ignores it",
                    f"{location}/filter[effectid=Lumetri]",
                ))

        # M7: Scale auto-fit check
        if file_elem is not None and seq_format is not None:
            scale_issue = _detect_scale_mismatch(clipitem, file_elem, seq_format)
            if scale_issue is not None:
                issues.append(Issue(
                    MAJOR, "M7",
                    f"Scale mismatch: {scale_issue.source_res[0]}x{scale_issue.source_res[1]} "
                    f"in {scale_issue.timeline_res[0]}x{scale_issue.timeline_res[1]} "
                    f"timeline, scale={scale_issue.current_scale}% → "
                    f"should be {scale_issue.corrected_scale}%",
                    location,
                ))

    # M4: Missing <link> for clips sharing same masterclipid
    _mc_usage: dict[str, list[str]] = {}
    for ci in root.iter("clipitem"):
        mcid = ci.findtext("masterclipid", "")
        if mcid:
            _mc_usage.setdefault(mcid, []).append(ci.get("id", "?"))
    for mcid, ci_ids in _mc_usage.items():
        if len(ci_ids) < 2:
            continue
        for ci_id in ci_ids:
            ci = root.find(f".//clipitem/[@id='{ci_id}']")
            if ci is not None and ci.find("link") is None:
                issues.append(Issue(
                    MAJOR, "M4",
                    f"clipitem[{ci_id}] missing <link> (shared source {mcid})",
                    f"clipitem[{ci_id}]",
                ))

    # M6: Clipitem child element order
    _order_ref = [
        "masterclipid", "name", "enabled", "duration", "rate", "start", "end",
        "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "file",
        "sourcetrack", "filter", "logginginfo", "colorinfo", "labels", "link",
    ]
    _order_map = {t: i for i, t in enumerate(_order_ref)}
    for ci in list(root.iter("clipitem"))[:50]:
        ci_tags = [c.tag for c in ci if c.tag in _order_map]
        sorted_tags = sorted(ci_tags, key=lambda t: _order_map.get(t, 999))
        if ci_tags != sorted_tags:
            issues.append(Issue(
                MAJOR, "M6",
                f"clipitem[{ci.get('id', '?')}] children not in FCP7 order",
                f"clipitem[{ci.get('id', '?')}]",
            ))

    # M3: Duration semantic check — check sequence duration vs clip durations
    seq = root.find("sequence")
    if seq is not None:
        seq_dur = seq.findtext("duration")
        if seq_dur:
            # This is informational — the actual fix is more complex
            pass

    return issues


def _scan_minor(root: ET.Element) -> list[Issue]:
    """Scan for MINOR issues (N1-N7).

    Args:
        root: The <xmeml> root element

    Returns:
        List of MINOR Issue objects
    """
    issues: list[Issue] = []

    seq = root.find("sequence")
    if seq is None:
        return issues

    # N1: Missing sequence/timecode
    if seq.find("timecode") is None:
        issues.append(Issue(
            MINOR, "N1",
            "Missing <timecode> in sequence",
            "sequence",
        ))

    # N2: <timecode> missing <displayformat>
    for tc in root.iter("timecode"):
        if tc.find("displayformat") is None:
            issues.append(Issue(
                MINOR, "N2",
                "<timecode> missing <displayformat>",
                "timecode",
            ))

    # N3: <in>/<out> values of -1
    for clipitem in root.iter("clipitem"):
        ci_id = clipitem.get("id", "?")
        in_val = clipitem.findtext("in")
        out_val = clipitem.findtext("out")
        if in_val == "-1" or out_val == "-1":
            issues.append(Issue(
                MINOR, "N3",
                f"<in>/<out> value is -1 (in={in_val}, out={out_val})",
                f"clipitem[{ci_id}]",
            ))

    # N4, N7: Empty tracks, disabled/locked tracks — report but do NOT fix
    if seq is not None:
        media = seq.find("media")
        if media is not None:
            for track_type in ["video", "audio"]:
                track_group = media.find(track_type)
                if track_group is None:
                    continue
                for i, track in enumerate(track_group.findall("track"), 1):
                    clipitems = track.findall("clipitem")
                    enabled = track.find("enabled")
                    locked = track.find("locked")

                    if not clipitems:
                        issues.append(Issue(
                            MINOR, "N4",
                            f"Empty {track_type} track {i} (preserved, not removed)",
                            f"media/{track_type}/track[{i}]",
                        ))

                    if enabled is not None and enabled.text == "FALSE":
                        issues.append(Issue(
                            MINOR, "N7",
                            f"Disabled {track_type} track {i} (preserved, not removed)",
                            f"media/{track_type}/track[{i}]",
                        ))

                    if locked is not None and locked.text == "TRUE":
                        issues.append(Issue(
                            MINOR, "N7",
                            f"Locked {track_type} track {i} (preserved, not removed)",
                            f"media/{track_type}/track[{i}]",
                        ))

    # N5: NTSC frame rate inconsistency
    timebases: set[str] = set()
    for rate_elem in root.iter("rate"):
        tb = rate_elem.findtext("timebase")
        if tb:
            timebases.add(tb)
    if len(timebases) > 1:
        issues.append(Issue(
            MINOR, "N5",
            f"Multiple timebase values found: {timebases}",
            "various <rate> elements",
        ))

    # N6: Float precision errors
    for param in root.iter("parameter"):
        val_elem = param.find("value")
        if val_elem is not None and val_elem.text:
            try:
                val = float(val_elem.text)
                if 0 < abs(val) < 1e-6:
                    issues.append(Issue(
                        MINOR, "N6",
                        f"Near-zero float value: {val} (should be 0)",
                        f"parameter/{param.findtext('name', '?')}",
                    ))
            except ValueError:
                pass

    return issues


def _detect_scale_mismatch(
    clipitem: ET.Element,
    file_elem: ET.Element,
    seq_format: ET.Element,
) -> Optional[ScaleIssue]:
    """Check if source fit-to-frame scaling was lost during PR XML export.

    PR's "Scale to Frame Size" is NOT preserved in FCP7 XML export.
    When source resolution ≠ timeline resolution and scale=100%,
    the clip will appear at wrong size in DaVinci.

    Args:
        clipitem: The <clipitem> Element
        file_elem: The corresponding <file> Element with media details
        seq_format: The <sequence>/<media>/<video>/<format> Element

    Returns:
        ScaleIssue with corrected scale %, or None if no issue
    """
    # 1. Read source resolution
    src_w = int(file_elem.findtext("media/video/samplecharacteristics/width") or "0")
    src_h = int(file_elem.findtext("media/video/samplecharacteristics/height") or "0")
    if not src_w or not src_h:
        return None

    # 2. Read timeline resolution
    tl_w = int(seq_format.findtext("samplecharacteristics/width") or "0")
    tl_h = int(seq_format.findtext("samplecharacteristics/height") or "0")
    if not tl_w or not tl_h:
        return None

    # 3. Read current Scale from basic effect
    basic_filter = clipitem.find("filter/effect/[effectid='basic']")
    current_scale = 100.0  # Default when no basic effect exists
    if basic_filter is not None:
        scale_param = basic_filter.find("parameter/[name='Scale']")
        if scale_param is not None:
            current_scale = float(scale_param.findtext("value") or "100")

    # 4. Check for Rotation — if present, trust the user
    has_rotation = False
    if basic_filter is not None:
        rot_param = basic_filter.find("parameter/[name='Rotation']")
        if rot_param is not None:
            rot_val = float(rot_param.findtext("value") or "0")
            if abs(rot_val) > 0.01:
                has_rotation = True

    if has_rotation:
        return None  # User applied rotation — trust their setup

    # 5. If scale ≠ 100%, user manually set it — trust user
    if abs(current_scale - 100.0) > 0.01:
        return None

    # 6. If resolutions match, no issue
    if src_w == tl_w and src_h == tl_h:
        return None

    # 7. Calculate correct fit scale (fit by width — PR default)
    fit_scale = (tl_w / src_w) * 100.0

    # Threshold: < 0.5% difference is float noise
    if abs(fit_scale - 100.0) < 0.5:
        return None

    return ScaleIssue(
        source_res=(src_w, src_h),
        timeline_res=(tl_w, tl_h),
        current_scale=current_scale,
        corrected_scale=round(fit_scale, 1),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Fix Engine
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_fixes(root: ET.Element, issues: list[Issue]) -> int:
    """Apply fixes for all detected issues.

    Args:
        root: The <xmeml> root element (modified in place)
        issues: List of Issue objects from _scan()

    Returns:
        Number of fixes applied
    """
    fix_count = 0

    def _mark_fixed(rule_id: str, msg_substr: str) -> None:
        for issue in issues:
            if issue.rule_id == rule_id and msg_substr in issue.message:
                issue.fixed = True

    # ── CRITICAL fixes ──────────────────────────────────────────────────────

    # C0: xmeml version → "5"
    if root.get("version") != FCP7_VERSION:
        root.set("version", FCP7_VERSION)
        fix_count += 1
        _mark_fixed("C0", "version")

    # C1: Insert missing video format
    seq = root.find("sequence")
    if seq is not None:
        video = seq.find("media/video")
        if video is not None and video.find("format") is None:
            fmt = _create_video_format(root)
            video.insert(0, fmt)
            fix_count += 1
            _mark_fixed("C1", "video")

        # C2: Insert missing audio format
        audio = seq.find("media/audio")
        if audio is not None and audio.find("format") is None:
            fmt = _create_audio_format()
            audio.insert(0, fmt)
            fix_count += 1
            _mark_fixed("C2", "audio")

    # C3: Add missing <ntsc> to <rate> elements
    for rate_elem in root.iter("rate"):
        if rate_elem.find("ntsc") is None:
            tb = rate_elem.findtext("timebase")
            if tb and _is_ntsc_timebase(float(tb)):
                ntsc_elem = ET.SubElement(rate_elem, "ntsc")
                ntsc_elem.text = "TRUE"
                fix_count += 1
                _mark_fixed("C3", "<ntsc>")

    # C4: Add missing <timebase> to <rate> elements
    for rate_elem in root.iter("rate"):
        if rate_elem.find("timebase") is None:
            tb = ET.SubElement(rate_elem, "timebase")
            tb.text = str(int(DEFAULT_FPS))
            fix_count += 1
            _mark_fixed("C4", "<timebase>")

    # C5: Fix pathurl format
    for pathurl_elem in root.iter("pathurl"):
        url = pathurl_elem.text or ""
        if url and not url.startswith("file:///"):
            # Convert file://localhost/ or file:// to file:///
            if url.startswith("file://localhost/"):
                pathurl_elem.text = "file:///" + url[len("file://localhost/"):]
            elif url.startswith("file://"):
                pathurl_elem.text = "file:///" + url[len("file://"):]
            fix_count += 1
        _mark_fixed("C5", "pathurl")

    # C6: Reorder media children (video before audio)
    if seq is not None:
        media = seq.find("media")
        if media is not None:
            children = list(media)
            child_tags = [c.tag for c in children]
            if "video" in child_tags and "audio" in child_tags:
                if child_tags.index("video") > child_tags.index("audio"):
                    video_el = media.find("video")
                    audio_el = media.find("audio")
                    if video_el is not None and audio_el is not None:
                        media.remove(video_el)
                        # Insert video before audio
                        audio_idx = list(media).index(audio_el) if audio_el in media else 0
                        media.insert(audio_idx, video_el)
                        fix_count += 1
            _mark_fixed("C6", "video before audio")

    # ── MAJOR fixes ─────────────────────────────────────────────────────────

    # M0: Remove Lumetri filter blocks
    for clipitem in root.iter("clipitem"):
        lumetri_filters = []
        for filt in clipitem.findall("filter"):
            eff = filt.find("effect")
            if eff is not None and eff.findtext("effectid") == "Lumetri":
                lumetri_filters.append(filt)
        for filt in lumetri_filters:
            clipitem.remove(filt)
            fix_count += 1
    if any(i.rule_id == "M0" for i in issues):
        _mark_fixed("M0", "Lumetri")

    # M1: Add missing <masterclipid>
    _next_mc_id = 1
    for clipitem in root.iter("clipitem"):
        if clipitem.find("masterclipid") is None:
            mcid = ET.Element("masterclipid")
            mcid.text = f"masterclip-{_next_mc_id}"
            # Insert at position 0 (first child per FCP7 spec)
            clipitem.insert(0, mcid)
            _next_mc_id += 1
            fix_count += 1
    if any(i.rule_id == "M1" for i in issues):
        _mark_fixed("M1", "masterclipid")

    # M2: Add missing <sourcetrack>
    for clipitem in root.iter("clipitem"):
        if clipitem.find("sourcetrack") is None:
            st = ET.Element("sourcetrack")
            mediatype = ET.SubElement(st, "mediatype")
            # Determine type from parent track context
            # If clipitem is under video track → video, audio track → audio
            # Simple heuristic: check if there's a <file> with video media
            file_elem = clipitem.find("file")
            if file_elem is not None and file_elem.find("media/video") is not None:
                mediatype.text = "video"
                track_type = ET.SubElement(st, "tracktype")
                track_type.text = "Video"
            else:
                mediatype.text = "audio"
                track_type = ET.SubElement(st, "tracktype")
                track_type.text = "Stereo"
            # Insert after file element (or at end if no file)
            file_idx = _find_child_index(clipitem, "file")
            if file_idx >= 0:
                clipitem.insert(file_idx + 1, st)
            else:
                clipitem.append(st)
            fix_count += 1
    if any(i.rule_id == "M2" for i in issues):
        _mark_fixed("M2", "sourcetrack")

    # M4: Add <link> elements for same-source clips
    _mc_to_clips: dict[str, list[ET.Element]] = {}
    for ci in root.iter("clipitem"):
        mc = ci.find("masterclipid")
        if mc is not None and mc.text:
            _mc_to_clips.setdefault(mc.text, []).append(ci)
    for mcid, clips in _mc_to_clips.items():
        if len(clips) < 2:
            continue
        for ci in clips:
            if ci.find("link") is not None:
                continue
            for i, linked in enumerate(clips):
                if linked is ci:
                    continue
                link = ET.SubElement(ci, "link")
                lid = ET.SubElement(link, "linkclipref")
                lid.text = linked.get("id", "")
                lmc = ET.SubElement(link, "mediatype")
                # Determine mediatype from sourcetrack
                st = linked.find("sourcetrack/mediatype")
                lmc.text = st.text if st is not None else "video"
                lt = ET.SubElement(link, "trackindex")
                lt.text = "1"
                li = ET.SubElement(link, "clipindex")
                li.text = str(i + 1)
            fix_count += 1
    if any(i.rule_id == "M4" for i in issues):
        _mark_fixed("M4", "link")

    # M5: Fill missing file/media/samplecharacteristics
    sfmt = _get_sequence_format(root)
    for ci in root.iter("clipitem"):
        file_elem = ci.find("file")
        if file_elem is None:
            continue
        if file_elem.find("media/video/samplecharacteristics") is not None:
            continue
        if sfmt is not None:
            media_el = file_elem.find("media")
            if media_el is None:
                media_el = ET.SubElement(file_elem, "media")
            video_el = media_el.find("video")
            if video_el is None:
                video_el = ET.SubElement(media_el, "video")
            sc = ET.SubElement(video_el, "samplecharacteristics")
            # Copy from sequence format
            seq_sc = sfmt.find("samplecharacteristics")
            if seq_sc is not None:
                for child in seq_sc:
                    sc.append(copy.deepcopy(child))
            fix_count += 1
    if any(i.rule_id == "M5" for i in issues):
        _mark_fixed("M5", "samplecharacteristics")

    # M6: Reorder clipitem children per FCP7 spec
    _order_tags = [
        "masterclipid", "name", "enabled", "duration", "rate", "start", "end",
        "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "file",
        "sourcetrack", "filter", "logginginfo", "colorinfo", "labels", "link",
        "comments", "itemhistory",
    ]
    _order_map = {tag: i for i, tag in enumerate(_order_tags)}
    for ci in root.iter("clipitem"):
        children = list(ci)
        sorted_children = sorted(children, key=lambda c: _order_map.get(c.tag, 999))
        if [c.tag for c in children] != [c.tag for c in sorted_children]:
            for child in children:
                ci.remove(child)
            for child in sorted_children:
                ci.append(child)
            fix_count += 1
    if any(i.rule_id == "M6" for i in issues):
        _mark_fixed("M6", "order")

    # M7: Scale auto-fit
    seq_format = _get_sequence_format(root)
    if seq_format is not None:
        for clipitem in root.iter("clipitem"):
            file_elem = clipitem.find("file")
            if file_elem is None:
                continue
            scale_issue = _detect_scale_mismatch(clipitem, file_elem, seq_format)
            if scale_issue is None:
                continue

            # Apply the corrected scale
            basic_filter = clipitem.find("filter/effect/[effectid='basic']")
            if basic_filter is None:
                # Create a basic effect with the corrected scale
                basic_filter = _create_basic_effect(scale_issue.corrected_scale)
                # Insert before any other filters
                first_filter = clipitem.find("filter")
                if first_filter is not None:
                    clipitem.insert(list(clipitem).index(first_filter), basic_filter)
                else:
                    clipitem.append(basic_filter)
            else:
                # Update existing scale parameter
                scale_param = basic_filter.find("parameter/[name='Scale']")
                if scale_param is not None:
                    val_elem = scale_param.find("value")
                    if val_elem is not None:
                        val_elem.text = str(scale_issue.corrected_scale)
                else:
                    # Add scale parameter to existing basic effect
                    param = ET.SubElement(basic_filter, "parameter")
                    name_elem = ET.SubElement(param, "name")
                    name_elem.text = "Scale"
                    val_elem = ET.SubElement(param, "value")
                    val_elem.text = str(scale_issue.corrected_scale)
            fix_count += 1
    if any(i.rule_id == "M7" for i in issues):
        _mark_fixed("M7", "Scale")

    # ── MINOR fixes ─────────────────────────────────────────────────────────

    # N1: Add missing sequence timecode
    if seq is not None and seq.find("timecode") is None:
        tc = _create_timecode(root)
        # Insert after <name> or <rate> if present
        name_idx = _find_child_index(seq, "name")
        if name_idx >= 0:
            seq.insert(name_idx + 1, tc)
        else:
            seq.insert(0, tc)
        fix_count += 1
    if any(i.rule_id == "N1" for i in issues):
        _mark_fixed("N1", "timecode")

    # N2: Add missing <displayformat> to timecode elements
    for tc in root.iter("timecode"):
        if tc.find("displayformat") is None:
            # Determine DF/NDF from rate
            tc_tb = tc.findtext("rate/timebase", "30")
            tc_has_ntsc = tc.find("rate/ntsc") is not None
            df = ET.SubElement(tc, "displayformat")
            df.text = "DF" if tc_has_ntsc else "NDF"
            fix_count += 1
    if any(i.rule_id == "N2" for i in issues):
        _mark_fixed("N2", "displayformat")

    # N6: Fix near-zero float values
    for param in root.iter("parameter"):
        val_elem = param.find("value")
        if val_elem is not None and val_elem.text:
            try:
                val = float(val_elem.text)
                if 0 < abs(val) < 1e-6:
                    val_elem.text = "0"
                    fix_count += 1
            except ValueError:
                pass
    if any(i.rule_id == "N6" for i in issues):
        _mark_fixed("N6", "float")

    return fix_count


def _create_video_format(root: ET.Element) -> ET.Element:
    """Create a <format> element for video with resolution from sequence attributes.

    Args:
        root: The <xmeml> root element (to read PreviewFrameSize attributes)

    Returns:
        A <format> Element with samplecharacteristics
    """
    fmt = ET.Element("format")
    sc = ET.SubElement(fmt, "samplecharacteristics")

    # Try to get resolution from sequence attributes
    seq = root.find("sequence")
    width = "1920"
    height = "1080"
    if seq is not None:
        w = seq.get("MZ.Sequence.PreviewFrameSizeWidth")
        h = seq.get("MZ.Sequence.PreviewFrameSizeHeight")
        if w and h:
            width = w
            height = h

    rate = ET.SubElement(sc, "rate")
    tb = ET.SubElement(rate, "timebase")
    # Read actual sequence timebase, default to 30
    seq_tb = "30"
    seq_ntsc = "TRUE"
    if seq is not None:
        seq_rate = seq.find("rate")
        if seq_rate is not None:
            seq_tb = seq_rate.findtext("timebase", "30")
            seq_ntsc_elem = seq_rate.find("ntsc")
            if seq_ntsc_elem is not None:
                seq_ntsc = seq_ntsc_elem.text or "TRUE"
    tb.text = seq_tb
    ntsc = ET.SubElement(rate, "ntsc")
    ntsc.text = seq_ntsc

    w_elem = ET.SubElement(sc, "width")
    w_elem.text = width
    h_elem = ET.SubElement(sc, "height")
    h_elem.text = height
    anamorphic = ET.SubElement(sc, "anamorphic")
    anamorphic.text = "FALSE"
    par = ET.SubElement(sc, "pixelaspectratio")
    par.text = "square"

    return fmt


def _create_audio_format() -> ET.Element:
    """Create a <format> element for audio.

    Returns:
        A <format> Element with audio samplecharacteristics
    """
    fmt = ET.Element("format")
    sc = ET.SubElement(fmt, "samplecharacteristics")

    rate = ET.SubElement(sc, "rate")
    tb = ET.SubElement(rate, "timebase")
    tb.text = "48000"

    depth = ET.SubElement(sc, "depth")
    depth.text = "16"
    samplerate = ET.SubElement(sc, "samplerate")
    samplerate.text = "48000"
    channelcount = ET.SubElement(sc, "channelcount")
    channelcount.text = "2"

    return fmt


def _create_basic_effect(scale: float) -> ET.Element:
    """Create a <filter> with a basic effect containing the given scale.

    Args:
        scale: The scale percentage value

    Returns:
        A <filter> Element with basic effect
    """
    filt = ET.Element("filter")
    eff = ET.SubElement(filt, "effect")
    name = ET.SubElement(eff, "name")
    name.text = "Basic Motion"
    eid = ET.SubElement(eff, "effectid")
    eid.text = "basic"
    etype = ET.SubElement(eff, "effecttype")
    etype.text = "motion"
    mt = ET.SubElement(eff, "mediatype")
    mt.text = "video"

    # Scale parameter
    param = ET.SubElement(eff, "parameter")
    pname = ET.SubElement(param, "name")
    pname.text = "Scale"
    pval = ET.SubElement(param, "value")
    pval.text = str(scale)

    # Rotation (default 0)
    param2 = ET.SubElement(eff, "parameter")
    pname2 = ET.SubElement(param2, "name")
    pname2.text = "Rotation"
    pval2 = ET.SubElement(param2, "value")
    pval2.text = "0"

    return filt


def _create_timecode(root: ET.Element) -> ET.Element:
    """Create a <timecode> element based on sequence rate.

    Args:
        root: The <xmeml> root element

    Returns:
        A <timecode> Element
    """
    tc = ET.Element("timecode")

    # Read actual sequence timebase
    seq = root.find("sequence")
    seq_tb = "30"
    seq_ntsc = "TRUE"
    if seq is not None:
        seq_rate = seq.find("rate")
        if seq_rate is not None:
            seq_tb = seq_rate.findtext("timebase", "30")
            seq_ntsc_elem = seq_rate.find("ntsc")
            if seq_ntsc_elem is not None:
                seq_ntsc = seq_ntsc_elem.text or "TRUE"
    is_ntsc_tc = seq_ntsc == "TRUE"

    rate = ET.SubElement(tc, "rate")
    tb = ET.SubElement(rate, "timebase")
    tb.text = seq_tb
    ntsc = ET.SubElement(rate, "ntsc")
    ntsc.text = seq_ntsc

    string = ET.SubElement(tc, "string")
    string.text = "00;00;00;00" if is_ntsc_tc else "00:00:00:00"
    frame = ET.SubElement(tc, "frame")
    frame.text = "0"
    source = ET.SubElement(tc, "source")
    source.text = "source"
    df = ET.SubElement(tc, "displayformat")
    df.text = "DF" if is_ntsc_tc else "NDF"

    return tc


def _find_child_index(parent: ET.Element, tag: str) -> int:
    """Find the index of the first child with the given tag.

    Args:
        parent: Parent element
        tag: Child tag name to find

    Returns:
        Index of the child, or -1 if not found
    """
    for i, child in enumerate(parent):
        if child.tag == tag:
            return i
    return -1


# ═══════════════════════════════════════════════════════════════════════════════
# Validator
# ═══════════════════════════════════════════════════════════════════════════════

def _validate(root: ET.Element) -> list[Issue]:
    """Run 23 structural validation checks on the fixed XML.

    Args:
        root: The <xmeml> root element

    Returns:
        List of Issue objects for any remaining problems
    """
    issues: list[Issue] = []

    # V1: Root is <xmeml>
    if root.tag != "xmeml":
        issues.append(Issue(MAJOR, "V1", f"Root tag is <{root.tag}>, expected <xmeml>", "/"))

    # V2: version="5"
    if root.get("version") != FCP7_VERSION:
        issues.append(Issue(MAJOR, "V2", f'version="{root.get("version")}", expected "5"', "/xmeml"))

    # V3: Has <sequence>
    seq = root.find("sequence")
    if seq is None:
        issues.append(Issue(CRITICAL, "V3", "Missing <sequence>", "/xmeml"))
        return issues

    # V4: Sequence has <media>
    media = seq.find("media")
    if media is None:
        issues.append(Issue(CRITICAL, "V4", "Missing <media> in sequence", "sequence"))
        return issues

    # V5: Video section exists
    video = media.find("video")
    if video is None:
        issues.append(Issue(CRITICAL, "V5", "Missing <video> in media", "media"))

    # V6: Audio section exists
    audio = media.find("audio")
    if audio is None:
        issues.append(Issue(MAJOR, "V6", "Missing <audio> in media", "media"))

    # V7: Video format exists
    if video is not None and video.find("format") is None:
        issues.append(Issue(CRITICAL, "V7", "Missing <format> in video", "media/video"))

    # V8: Audio format exists
    if audio is not None and audio.find("format") is None:
        issues.append(Issue(MAJOR, "V8", "Missing <format> in audio", "media/audio"))

    # V9: Video format has samplecharacteristics
    if video is not None:
        vfmt = video.find("format")
        if vfmt is not None and vfmt.find("samplecharacteristics") is None:
            issues.append(Issue(MAJOR, "V9", "Missing <samplecharacteristics> in video format", "video/format"))

    # V10: Resolution is set
    if video is not None:
        sc = video.find("format/samplecharacteristics")
        if sc is not None:
            w = sc.findtext("width")
            h = sc.findtext("height")
            if not w or not h or int(w or 0) == 0 or int(h or 0) == 0:
                issues.append(Issue(MAJOR, "V10", "Invalid resolution in video format", "video/format"))

    # V11: Rate has timebase
    for rate_elem in root.iter("rate"):
        if rate_elem.find("timebase") is None:
            issues.append(Issue(MAJOR, "V11", "Rate missing <timebase>", "rate"))

    # V12: Rate has ntsc (for NTSC rates)
    for rate_elem in root.iter("rate"):
        tb = rate_elem.findtext("timebase")
        if tb and _is_ntsc_timebase(float(tb)) and rate_elem.find("ntsc") is None:
            issues.append(Issue(MAJOR, "V12", "NTSC rate missing <ntsc>", "rate"))

    # V13: All clipitems have masterclipid
    for ci in root.iter("clipitem"):
        if ci.find("masterclipid") is None:
            issues.append(Issue(MAJOR, "V13", f"clipitem missing <masterclipid>", ci.get("id", "?")))

    # V14: All clipitems have sourcetrack
    for ci in root.iter("clipitem"):
        if ci.find("sourcetrack") is None:
            issues.append(Issue(MINOR, "V14", f"clipitem missing <sourcetrack>", ci.get("id", "?")))

    # V15: pathurl uses file:/// format
    for pu in root.iter("pathurl"):
        url = pu.text or ""
        if url and not url.startswith("file:///"):
            issues.append(Issue(MAJOR, "V15", f"pathurl not file:/// format", "pathurl"))

    # V16: No Lumetri effects remain
    for eff in root.iter("effect"):
        if eff.findtext("effectid") == "Lumetri":
            issues.append(Issue(MINOR, "V16", "Lumetri effect still present", "effect"))
            break

    # V17: Media children order (video before audio)
    if media is not None:
        children = [c.tag for c in media]
        if "video" in children and "audio" in children:
            if children.index("video") > children.index("audio"):
                issues.append(Issue(MAJOR, "V17", "Media order: video should precede audio", "media"))

    # V18: Each clipitem has <file>
    for ci in root.iter("clipitem"):
        if ci.find("file") is None:
            issues.append(Issue(MAJOR, "V18", f"clipitem missing <file>", ci.get("id", "?")))

    # V19: Sequence has duration
    if seq.find("duration") is None:
        issues.append(Issue(MAJOR, "V19", "Sequence missing <duration>", "sequence"))

    # V20: Sequence has rate
    if seq.find("rate") is None:
        issues.append(Issue(MAJOR, "V20", "Sequence missing <rate>", "sequence"))

    # V21: Sequence has name
    if seq.find("name") is None:
        issues.append(Issue(MINOR, "V21", "Sequence missing <name>", "sequence"))

    # V22: No duplicate file ids (audio stereo pairs sharing file-id is valid)
    file_ids: dict[str, int] = {}
    for f in root.iter("file"):
        fid = f.get("id")
        if fid:
            file_ids[fid] = file_ids.get(fid, 0) + 1
    for fid, count in file_ids.items():
        if count > 2:
            issues.append(Issue(MAJOR, "V22", f"Duplicate file id: {fid} ({count}x)", "file"))
        # count == 2 is expected for stereo audio pairs

    # V23: Clipitem element order (check first few)
    for ci in list(root.iter("clipitem"))[:5]:
        ci_id = ci.get("id", "?")
        child_tags = [c.tag for c in ci]
        # Check that 'file' comes before 'filter'
        if "file" in child_tags and "filter" in child_tags:
            if child_tags.index("file") > child_tags.index("filter"):
                issues.append(Issue(MINOR, "V23", f"<file> should come before <filter>", f"clipitem[{ci_id}]"))

    return issues


# ═══════════════════════════════════════════════════════════════════════════════
# Output — XML Writer & Report Generator
# ═══════════════════════════════════════════════════════════════════════════════

def _write_fixed_xml(root: ET.Element, output_path: Path) -> None:
    """Write the fixed XML to disk with DOCTYPE declaration.

    Args:
        root: The fixed <xmeml> root element
        output_path: Path to write the output file
    """
    # Serialize to string
    xml_str = ET.tostring(root, encoding="unicode", xml_declaration=False)

    # Pretty-print
    try:
        dom = minidom.parseString(xml_str)
        pretty = dom.toprettyxml(indent="\t", encoding=None)
        # Remove minidom's own xml declaration (we'll add our own + DOCTYPE)
        lines = pretty.split("\n")
        # Remove the <?xml?> line minidom adds
        if lines and lines[0].startswith("<?xml"):
            lines = lines[1:]
        pretty = "\n".join(lines)
    except Exception:
        pretty = xml_str

    # Write with XML declaration + DOCTYPE
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(output_path), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(f"{FCP7_DOCTYPE}\n")
        f.write(pretty.strip() + "\n")


def _generate_report(
    scan_issues: list[Issue],
    validation_issues: list[Issue],
    fix_count: int,
    input_path: Path,
    output_path: Path,
    report_path: Path,
) -> None:
    """Generate a markdown fix report.

    Args:
        scan_issues: Issues found by _scan()
        validation_issues: Issues found by _validate()
        fix_count: Number of fixes applied
        input_path: Original input file path
        output_path: Fixed output file path
        report_path: Path to write the report
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"# Fix Report: {input_path.name}",
        "",
        f"> Generated: {now}",
        f"> Tool: pr2resolve v{VERSION}",
        "",
        "---",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Input | `{input_path.name}` |",
        f"| Output | `{output_path.name}` |",
        f"| Issues found | {len(scan_issues)} |",
        f"| Fixes applied | {fix_count} |",
        f"| Validation checks | {len(validation_issues)} remaining |",
        "",
    ]

    # Issues by severity
    for severity in [CRITICAL, MAJOR, MINOR]:
        sev_issues = [i for i in scan_issues if i.severity == severity]
        if not sev_issues:
            continue
        lines.append(f"## {severity} Issues ({len(sev_issues)})")
        lines.append("")
        lines.append("| Rule | Status | Description |")
        lines.append("|------|:------:|-------------|")
        for issue in sev_issues:
            status = "✅ Fixed" if issue.fixed else "⚠️ Unfixed"
            lines.append(f"| {issue.rule_id} | {status} | {issue.message} |")
        lines.append("")

    # Validation results
    if validation_issues:
        lines.append(f"## Validation ({len(validation_issues)} remaining)")
        lines.append("")
        lines.append("| Check | Severity | Description |")
        lines.append("|-------|:--------:|-------------|")
        for vi in validation_issues:
            lines.append(f"| {vi.rule_id} | {vi.severity} | {vi.message} |")
        lines.append("")
    else:
        lines.append("## Validation")
        lines.append("")
        lines.append("All 23 validation checks passed. ✅")
        lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(report_path), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ═══════════════════════════════════════════════════════════════════════════════
# Utilities
# ═══════════════════════════════════════════════════════════════════════════════

def _recycle(path: Path) -> None:
    """Move a file to the system recycle bin / trash. Never permanently delete.

    Platform support:
    - Windows: PowerShell shell API -> Recycle Bin
    - macOS: ~/.Trash
    - Linux: gio trash (GNOME/KDE) or ~/.local/share/Trash/files/ (XDG spec)

    Args:
        path: Path to the file to recycle
    """
    import subprocess
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            subprocess.run([
                "powershell", "-Command",
                "Add-Type -AssemblyName Microsoft.VisualBasic;"
                f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                f"'{path}','OnlyErrorDialogs','SendToRecycleBin')"
            ], capture_output=True, timeout=10)
        elif sys.platform == "darwin":
            trash = Path.home() / ".Trash"
            trash.mkdir(exist_ok=True)
            path.rename(trash / path.name)
        else:
            # Linux: try gio first (GNOME/KDE), fall back to XDG Trash spec
            result = subprocess.run(
                ["gio", "trash", str(path)],
                capture_output=True, timeout=10
            )
            if result.returncode != 0:
                # XDG Trash spec fallback
                trash_files = Path(os.environ.get(
                    "XDG_DATA_HOME",
                    str(Path.home() / ".local" / "share")
                )) / "Trash" / "files"
                trash_files.mkdir(parents=True, exist_ok=True)
                path.rename(trash_files / path.name)
    except Exception:
        pass  # best-effort, silently ignore failures


# ═══════════════════════════════════════════════════════════════════════════════
# DRT Output — DaVinci Resolve Scripting API Bridge
# ═══════════════════════════════════════════════════════════════════════════════

# DaVinci Resolve Scripting API module paths
_RESOLVE_API_PATHS: dict[str, list[str]] = {
    "win32": [
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    ],
    "darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    ],
    "linux": [
        "/opt/resolve/Developer/Scripting/Modules",
    ],
}

# DaVinci Resolve install directories — built dynamically from env vars, no hardcoded drives
def _build_resolve_candidates() -> list[str]:
    """Build list of possible DaVinci Resolve install directories.

    Uses environment variables (PROGRAMFILES, PROGRAMDATA, RESOLVE_INSTALL_DIR)
    and scans all fixed drives. No hardcoded drive letters.

    Returns:
        List of directory paths to check for fusionscript.dll
    """
    import string
    candidates: list[str] = []
    subdir = r"Blackmagic Design\DaVinci Resolve"

    env_override = os.environ.get("RESOLVE_INSTALL_DIR", "")
    if env_override:
        candidates.append(env_override)

    for env_var in ["PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"]:
        base = os.environ.get(env_var, "")
        if base:
            candidates.append(str(Path(base) / subdir))

    sys_drive = os.environ.get("SystemDrive", "C:")
    candidates.append(str(Path(sys_drive) / "Program Files" / subdir))
    candidates.append(str(Path(sys_drive) / "ProgramData" / subdir))

    for letter in string.ascii_uppercase:
        p = Path(f"{letter}:\\Program Files") / subdir
        if p.exists():
            candidates.append(str(p))

    return candidates


def _find_resolve_install_dir() -> Optional[Path]:
    """Find DaVinci Resolve installation directory by checking common paths.

    Returns:
        Path to DaVinci Resolve install dir (containing fusionscript.dll), or None
    """
    for candidate in _build_resolve_candidates():
        p = Path(candidate)
        if (p / "fusionscript.dll").exists():
            return p
    return None


def _is_resolve_running() -> bool:
    """Check if DaVinci Resolve (Resolve.exe) is running as a process.

    Returns:
        True if Resolve.exe is found in running processes
    """
    import subprocess
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        return "Resolve.exe" in result.stdout
    except Exception:
        return False


def _try_import_resolve() -> Any:
    """Attempt to import the DaVinciResolveScript module.

    Searches standard installation paths for the module, sets RESOLVE_SCRIPT_LIB
    to point to fusionscript.dll in the detected install directory, then imports.

    Returns:
        The Resolve object if successful, None otherwise
    """
    sys_name = sys.platform
    paths = _RESOLVE_API_PATHS.get(sys_name, [])

    # Check environment variable
    env_path = os.environ.get("RESOLVE_SCRIPT_API", "")
    if env_path:
        paths.insert(0, env_path)

    # Find install dir and set RESOLVE_SCRIPT_LIB for DaVinciResolveScript.py
    # (which hardcodes "C:\Program Files" on line 42)
    install_dir = _find_resolve_install_dir()
    if install_dir:
        dll_path = str(install_dir / "fusionscript.dll")
        if Path(dll_path).exists():
            os.environ["RESOLVE_SCRIPT_LIB"] = dll_path

    for p in paths:
        if Path(p).exists() and p not in sys.path:
            sys.path.append(p)

    try:
        import DaVinciResolveScript as dvr  # type: ignore[import-untyped]
        resolve = dvr.scriptapp("Resolve")
        return resolve
    except ImportError:
        return None
    except Exception:
        return None


def _check_resolve_running() -> Any:
    """Check if DaVinci Resolve is running and Scripting API is available.

    First checks if Resolve.exe process is running. If so, attempts to
    connect via Scripting API.

    Returns:
        The Resolve object if available, None otherwise
    """
    if not _is_resolve_running():
        return None
    resolve = _try_import_resolve()
    if resolve is None:
        return None
    try:
        pm = resolve.GetProjectManager()
        if pm is None:
            return None
        return resolve
    except Exception:
        return None


def _drt_sandbox_export(
    resolve: Any,
    xml_path: Path,
    output_path: Path,
    timeline_name: str = "Imported",
) -> bool:
    """Export DRT via a temporary sandbox project.

    Creates a temporary DaVinci project, imports the XML, exports DRT,
    then restores the user's original project. Never touches user's project.

    DaVinci API facts used:
    - DeleteTimeline() does NOT exist → sandbox project is the only clean path
    - DeleteProject() only works on unloaded projects
    - CloseProject() closes without saving
    - LoadProject(name) loads an existing project

    Args:
        resolve: The DaVinci Resolve object
        xml_path: Path to the FCP7 XML file
        output_path: Path to write the .drt file
        timeline_name: Name for the imported timeline

    Returns:
        True if successful, False otherwise
    """
    try:
        pm = resolve.GetProjectManager()
        original_project = pm.GetCurrentProject()
        original_name = original_project.GetName() if original_project else None

        # Create unique temp project name
        temp_name = f"pr2resolve_drt_{int(time.time())}"
        print(f"  Creating temporary project: \"{temp_name}\"")

        # Close current project first if one is open
        if original_project is not None:
            pm.SaveProject()
            pm.CloseProject(original_project)

        project = pm.CreateProject(temp_name)
        if project is None:
            print("  Error: Could not create temporary project")
            # Try to restore original
            if original_name:
                pm.LoadProject(original_name)
            return False

        media_pool = project.GetMediaPool()

        # Import with source clips first; fall back to skeleton import
        timeline = media_pool.ImportTimelineFromFile(
            str(xml_path),
            {
                "timelineName": timeline_name,
                "importSourceClips": True,
            },
        )
        if timeline is None:
            timeline = media_pool.ImportTimelineFromFile(
                str(xml_path),
                {
                    "timelineName": timeline_name,
                    "importSourceClips": False,
                },
            )
            if timeline is not None:
                print("  (imported timeline structure only, media offline)")

        if timeline is None:
            print("  Error: Failed to import timeline from XML")
            # Clean up and restore
            pm.CloseProject(project)
            if original_name:
                pm.LoadProject(original_name)
            return False

        print(f"  Timeline imported: {timeline.GetName()}")

        # Export DRT
        export_result = timeline.Export(
            str(output_path),
            resolve.EXPORT_DRT,
        )
        if not export_result:
            print("  Error: DRT export failed")
            pm.CloseProject(project)
            if original_name:
                pm.LoadProject(original_name)
            return False

        print(f"  DRT exported: {output_path}")

        # Restore user's original project
        pm.CloseProject(project)
        if original_name:
            pm.LoadProject(original_name)
            print(f"  Restored: \"{original_name}\"")
        else:
            print("  (no original project to restore)")

        # Clean up: delete temp project (only deletable when unloaded)
        try:
            pm.DeleteProject(temp_name)
        except Exception:
            pass  # best-effort cleanup

        return True

    except Exception as e:
        print(f"  Error during DRT sandbox: {e}")
        # Best-effort restore
        try:
            pm = resolve.GetProjectManager()
            current = pm.GetCurrentProject()
            if current is not None and original_name and (
                current.GetName() if hasattr(current, 'GetName') else ""
            ) != original_name:
                pm.CloseProject(current)
                pm.LoadProject(original_name)
        except Exception:
            pass
        return False


def _launch_resolve() -> bool:
    """Attempt to launch DaVinci Resolve automatically.

    Finds the install directory via _find_resolve_install_dir and starts
    Resolve.exe as a detached process. The user still needs to wait for
    DaVinci to finish initializing before the API becomes available
    (typically 10-30 seconds).

    Returns:
        True if the process was started, False otherwise
    """
    install_dir = _find_resolve_install_dir()
    if not install_dir:
        return False
    exe = install_dir / "Resolve.exe"
    if not exe.exists():
        return False
    try:
        import subprocess
        subprocess.Popen(
            [str(exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if sys.platform == "win32" else 0,  # DETACHED_PROCESS
        )
        print(f"  Launched: {exe}")
        return True
    except Exception:
        return False


def _drt_supplement_lumetri(
    resolve: Any,
    lumetri_data: dict[str, dict[str, float]],
) -> bool:
    """Supplement DaVinci timeline with Lumetri color data from .prproj.

    Maps PR Lumetri parameters to DaVinci Color Corrector nodes.

    Args:
        resolve: The DaVinci Resolve object
        lumetri_data: Dict mapping clip name → {param_name: value}

    Returns:
        True if at least one clip was updated
    """
    # Lumetri → DaVinci Color parameter mapping
    _LUMETRI_TO_DAVINCI: dict[str, str] = {
        "曝光": "Gain",        # Exposure → Gain wheel
        "对比度": "Contrast",
        "高光": "Highlights",
        "阴影": "Shadows",
        "白色": "Gain",        # Whites → Gain (not "Whites")
        "黑色": "Lift",        # Blacks → Lift (not "Blacks")
        "饱和度": "Saturation",
        "色温": "Temperature",
        "色彩": "Tint",
    }

    try:
        pm = resolve.GetProjectManager()
        project = pm.GetCurrentProject()
        if project is None:
            return False

        timeline = project.GetCurrentTimeline()
        if timeline is None:
            return False

        updated = 0
        for track_idx in range(1, timeline.GetTrackCount("video") + 1):
            clips = timeline.GetItemListInTrack("video", track_idx)
            if not clips:
                continue
            for clip in clips:
                clip_name = clip.GetName()
                if clip_name not in lumetri_data:
                    continue

                params = lumetri_data[clip_name]
                if not params:
                    continue
                try:
                    color = clip.GetColor()
                    if color is None:
                        continue
                    for pr_name, da_name in _LUMETRI_TO_DAVINCI.items():
                        if pr_name not in params:
                            continue
                        try:
                            color.SetCurrentParameterByName(da_name, params[pr_name])
                            updated += 1
                        except Exception:
                            pass
                except Exception:
                    pass

        print(f"  Lumetri data applied to {updated} parameters")
        return updated > 0

    except Exception as e:
        print(f"  Error supplementing Lumetri: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _print_banner() -> None:
    """Print the tool banner."""
    print("=" * 56)
    print(f"  pr2resolve v{VERSION}")
    print(f"  Premiere Pro to DaVinci Resolve Converter")
    print("=" * 56)
    print()


def _print_issues(issues: list[Issue]) -> None:
    """Print issues to stdout in a formatted table.

    Args:
        issues: List of Issue objects to display
    """
    if not issues:
        print("  No issues found.")
        return

    for severity in [CRITICAL, MAJOR, MINOR]:
        sev_issues = [i for i in issues if i.severity == severity]
        if not sev_issues:
            continue
        icon = {"CRITICAL": "[C]", "MAJOR": "[M]", "MINOR": "[N]"}.get(severity, "[?]")
        print(f"  {icon} {severity} ({len(sev_issues)}):")
        for issue in sev_issues:
            status = "[FIXED]" if issue.fixed else ""
            print(f"    {status} [{issue.rule_id}] {issue.message}")
        print()


def _run_pipeline(
    input_path: Path,
    output_dir: Optional[Path] = None,
    report: bool = False,
    diagnose_only: bool = False,
    sequence_name: Optional[str] = None,
    drt: bool = False,
) -> int:
    """Run the full fix pipeline on an input file.

    Args:
        input_path: Path to the input XML file
        output_dir: Directory for output files (default: same as input)
        report: Whether to generate a fix report
        diagnose_only: If True, only diagnose, don't fix

    Returns:
        Exit code (0 = success, 1 = error)
    """
    _print_banner()

    # Determine output paths
    if output_dir is None:
        output_dir = input_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    output_path = output_dir / f"{stem}_fixed{input_path.suffix}"
    report_path = output_dir / f"{stem}_fix_report.md"
    backup_path = input_path.with_suffix(input_path.suffix + ".bak")

    # Load
    print(f"  Loading: {input_path}")
    try:
        if input_path.suffix.lower() == ".prproj":
            prproj_root = load_prproj(input_path)
            print(f"  Format: .prproj (Premiere Pro project)")

            # List sequences
            idx = _PrprojIndex.build(prproj_root)
            sequences = _prproj_list_sequences(prproj_root, idx)
            if not sequences:
                print("  Error: No sequences found in .prproj")
                return 1

            # Auto-select: if only one non-empty sequence, use it
            non_empty = [s for s in sequences if s["clip_count"] > 0]
            if sequence_name:
                matching = [s for s in sequences if s["name"] == sequence_name]
                if matching:
                    selected = matching[0]
                else:
                    print(f"  Error: Sequence '{sequence_name}' not found")
                    print(f"  Available: {[s['name'] for s in sequences]}")
                    return 1
            elif len(non_empty) == 1:
                selected = non_empty[0]
            elif len(sequences) == 1:
                selected = sequences[0]
            else:
                print(f"  Found {len(sequences)} sequences:")
                for i, s in enumerate(sequences, 1):
                    marker = " <--" if s["clip_count"] > 0 else ""
                    print(f"    [{i}] {s['name']}  {s['width']}x{s['height']}  {s['clip_count']} clips{marker}")
                # Auto-select the one with most clips
                selected = max(sequences, key=lambda s: s["clip_count"])
                print(f"  Auto-selected: [{sequences.index(selected)+1}] {selected['name']}")

            print(f"  Sequence: {selected['name']} ({selected['width']}x{selected['height']}, {selected['clip_count']} clips)")
            print()

            # Convert to FCP7 XML
            print("  Converting .prproj to FCP7 XML...")
            root = _prproj_parse_sequence(prproj_root, selected["uid"], input_path)
            print("  Conversion complete.")
            print()

            # Extract Lumetri data for DRT output
            lumetri_data = _prproj_extract_all_lumetri(prproj_root, selected["uid"])
            if lumetri_data:
                print(f"  Lumetri params: {sum(len(v) for v in lumetri_data.values())} across {len(lumetri_data)} clips")

            # Update output path to .xml
            output_path = output_dir / f"{stem}.xml"
        else:
            root = load_xml(input_path)
            lumetri_data = {}
            print(f"  Format: FCP7 XML")
    except Exception as e:
        print(f"  Error loading file: {e}")
        return 1

    seq = root.find("sequence")
    seq_name = seq.findtext("name", "(unnamed)") if seq is not None else "(no sequence)"
    print(f"  Sequence: {seq_name}")
    print()

    # Diagnose
    print("  Scanning for issues...")
    scan_issues = _scan(root)
    _print_issues(scan_issues)

    if diagnose_only:
        print(f"  Diagnose-only mode. {len(scan_issues)} issues found.")
        return 0

    # Backup original
    if not backup_path.exists():
        import shutil
        shutil.copy2(str(input_path), str(backup_path))
        print(f"  Backup: {backup_path.name}")

    # Fix
    print("  Applying fixes...")
    fix_count = _apply_fixes(root, scan_issues)
    print(f"  Applied {fix_count} fixes.")
    print()

    # Validate
    print("  Validating...")
    validation_issues = _validate(root)
    if validation_issues:
        print(f"  ⚠️ {len(validation_issues)} validation issues remain:")
        for vi in validation_issues:
            print(f"    [{vi.rule_id}] {vi.message}")
    else:
        print("  ✅ All 23 validation checks passed.")
    print()

    # Write output
    print(f"  Writing: {output_path}")
    _write_fixed_xml(root, output_path)

    # Report
    if report:
        _generate_report(scan_issues, validation_issues, fix_count, input_path, output_path, report_path)
        print(f"  Report: {report_path}")

    # DRT output
    xml_written = output_path.exists()
    if drt:
        print()
        drt_path = output_dir / f"{stem}.drt"
        print("  DRT output requested. Checking DaVinci Resolve...")

        def _try_drt(resolve_obj: Any) -> bool:
            """Run sandbox DRT export. Returns True on success."""
            print("  DRT uses a temporary sandbox project to avoid")
            print("  touching your current project. It will briefly")
            print("  switch projects and restore afterward.")
            seq_name_drt = seq.findtext("name", "Imported") if seq is not None else "Imported"
            if not _drt_sandbox_export(resolve_obj, output_path, drt_path, seq_name_drt):
                return False
            if lumetri_data:
                _drt_supplement_lumetri(resolve_obj, lumetri_data)
            _recycle(output_path)
            print(f"  ✅ DRT: {drt_path}")
            print(f"     (intermediate XML moved to recycle bin)")
            return True

        resolve = _check_resolve_running()
        if resolve is not None:
            # DaVinci running → sandbox export
            print("  DaVinci Resolve detected.")
            if _try_drt(resolve):
                xml_written = False
        elif not xml_written:
            # No DaVinci AND no XML → nothing usable
            print("  ❌ DaVinci Resolve not detected, and XML output failed.")
            print("     DRT generation is not possible.")
        else:
            # No DaVinci BUT XML succeeded → prompt user
            print("  ❕ DaVinci Resolve not detected.")
            print("     XML was generated successfully (can be used as-is).")
            print("     DRT requires DaVinci Resolve Studio running.")
            print()
            print("      [A]uto-launch DaVinci  - start Resolve automatically")
            print("      [R]etry               - check again for DaVinci")
            print("      [L]eave               - continue without DRT (keep XML)")
            print()
            choice = input("  > ").strip().lower()
            if choice == "a":
                print("  Launching DaVinci Resolve...")
                if _launch_resolve():
                    print("  DaVinci is starting. This may take 10-30 seconds.")
                    print("  After it finishes loading, it will automatically create a new project if none is open.")
                else:
                    print("  Could not auto-launch. Please start DaVinci manually.")
                for attempt in range(1, 6):
                    print(f"  Checking DaVinci... (attempt {attempt}/5)")
                    time.sleep(3 if attempt <= 2 else 5)
                    resolve = _check_resolve_running()
                    if resolve is not None:
                        if _try_drt(resolve):
                            xml_written = False
                        break
                else:
                    print("  ❕ DaVinci still not accessible. XML kept.")
            elif choice == "r":
                for attempt in range(1, 4):
                    print(f"  Checking DaVinci... (attempt {attempt}/3)")
                    resolve = _check_resolve_running()
                    if resolve is not None:
                        if _try_drt(resolve):
                            xml_written = False
                        break
                    if attempt < 3:
                        print("  Still not detected. Press Enter to retry, or type 'l' to leave.")
                        if input("  > ").strip().lower() == "l":
                            break
                else:
                    print("  ❕ Continuing without DRT. XML kept.")
            else:
                print("  ❕ Continuing without DRT. XML kept.")

    print()
    if xml_written:
        print(f"  Done. {fix_count} fixes applied to {output_path.name}")
    elif drt:
        print(f"  Done. Output: {drt_path.name}")
    return 0


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        prog="pr2resolve",
        description="Fix Premiere Pro FCP7 XML for DaVinci Resolve compatibility.",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input file (.xml or .prproj)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output directory (default: same as input)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate a fix report (.md)",
    )
    parser.add_argument(
        "--drt",
        action="store_true",
        help="Generate DRT output via DaVinci Resolve Scripting API",
    )
    parser.add_argument(
        "--sequence",
        type=str,
        default=None,
        help="Sequence name to extract from .prproj (default: auto-select)",
    )
    parser.add_argument(
        "--diagnose-only",
        action="store_true",
        help="Only diagnose issues, do not fix",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 = success, 1 = error)
    """
    args = _parse_args()

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}")
        return 1

    return _run_pipeline(
        input_path=args.input,
        output_dir=args.output,
        report=args.report,
        diagnose_only=args.diagnose_only,
        sequence_name=args.sequence,
        drt=args.drt,
    )


if __name__ == "__main__":
    sys.exit(main())