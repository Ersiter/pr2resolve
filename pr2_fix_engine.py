"""FCP7 XML Fix Engine — auto-repairs known issues by severity."""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from typing import Optional

from pr2_constants import (
    CRITICAL,
    DEFAULT_FPS,
    FCP7_VERSION,
    Issue,
    MAJOR,
    MINOR,
    ScaleIssue,
    _get_sequence_format,
)
from pr2_diagnostics import _detect_scale_mismatch, _is_ntsc_timebase


# ═══════════════════════════════════════════════════════════════════════════════
# Fix Engine
# ═══════════════════════════════════════════════════════════════════════════════

def _mark_fixed(issues: list[Issue], rule_id: str, msg_substr: str) -> None:
    """Mark matching issues as fixed.

    Args:
        issues: List of Issue objects to search
        rule_id: The rule ID to match (e.g. 'C1')
        msg_substr: Substring that must appear in the issue message
    """
    for issue in issues:
        if issue.rule_id == rule_id and msg_substr in issue.message:
            issue.fixed = True


def _apply_fixes(root: ET.Element, issues: list[Issue]) -> int:
    """Apply fixes for all detected issues.

    Args:
        root: The <xmeml> root element (modified in place)
        issues: List of Issue objects from _scan()

    Returns:
        Number of fixes applied
    """
    fix_count = 0

    # ── CRITICAL fixes ──────────────────────────────────────────────────────

    # C0: xmeml version → "5"
    if root.get("version") != FCP7_VERSION:
        root.set("version", FCP7_VERSION)
        fix_count += 1
        _mark_fixed(issues, "C0", "version")

    # C1: Insert missing video format
    seq = root.find("sequence")
    if seq is not None:
        video = seq.find("media/video")
        if video is not None and video.find("format") is None:
            fmt = _create_video_format(root)
            video.insert(0, fmt)
            fix_count += 1
            _mark_fixed(issues, "C1", "video")

        # C2: Insert missing audio format
        audio = seq.find("media/audio")
        if audio is not None and audio.find("format") is None:
            fmt = _create_audio_format()
            audio.insert(0, fmt)
            fix_count += 1
            _mark_fixed(issues, "C2", "audio")

    # C3: Add missing <ntsc> to <rate> elements
    for rate_elem in root.iter("rate"):
        if rate_elem.find("ntsc") is None:
            tb = rate_elem.findtext("timebase")
            if tb and _is_ntsc_timebase(float(tb)):
                ntsc_elem = ET.SubElement(rate_elem, "ntsc")
                ntsc_elem.text = "TRUE"
                fix_count += 1
                _mark_fixed(issues, "C3", "<ntsc>")

    # C4: Add missing <timebase> to <rate> elements
    for rate_elem in root.iter("rate"):
        if rate_elem.find("timebase") is None:
            tb = ET.SubElement(rate_elem, "timebase")
            tb.text = str(int(DEFAULT_FPS))
            fix_count += 1
            _mark_fixed(issues, "C4", "<timebase>")

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
        _mark_fixed(issues, "C5", "pathurl")

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
            _mark_fixed(issues, "C6", "video before audio")

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
        _mark_fixed(issues, "M0", "Lumetri")

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
        _mark_fixed(issues, "M1", "masterclipid")

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
        _mark_fixed(issues, "M2", "sourcetrack")

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
        _mark_fixed(issues, "M4", "link")

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
        _mark_fixed(issues, "M5", "samplecharacteristics")

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
        _mark_fixed(issues, "M6", "order")

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
        _mark_fixed(issues, "M7", "Scale")

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
        _mark_fixed(issues, "N1", "timecode")

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
        _mark_fixed(issues, "N2", "displayformat")

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
        _mark_fixed(issues, "N6", "float")

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
