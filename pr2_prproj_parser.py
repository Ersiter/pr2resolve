"""Premiere Pro .prproj parser — ObjectID graph traversal and FCP7 XML conversion."""

from __future__ import annotations

import copy
import gzip
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pr2_constants import DEFAULT_FPS, FCP7_VERSION, FCP7_DOCTYPE, Issue, NTSC_RATES, PAL_RATES, FPS_TOLERANCE

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
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


# ═══════════════════════════════════════════════════════════════════════════════
# Index Class
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
# Parser Functions
# ═══════════════════════════════════════════════════════════════════════════════

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


def _prproj_list_sequences(root: ET.Element, idx: _PrprojIndex) -> list[dict[str, Any]]:
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
                                        media_filename = Path(mfp.replace("\\", "/")).name.lower()
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
                                    if Path(mfp.replace("\\", "/")).name.lower() == a_mc_name.lower():
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
