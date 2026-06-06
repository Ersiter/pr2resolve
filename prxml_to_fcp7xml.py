#!/usr/bin/env python3
"""prxml2fcp7xml — Premiere Pro FCP7 XML fixer for DaVinci Resolve compatibility.

Dual-entry (PR FCP7 XML / .prproj) → Unified Timeline Model → FCP7 XML output.

Phase 1: FCP7 XML entry → Diagnostics → Fix → Validate → Output.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.dom import minidom

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "1.0.0"

DEFAULT_FPS = 30.0
MICROSECOND = 1_000_000

NTSC_RATES: list[float] = [23.976, 29.97, 59.94, 47.952]

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


def _is_ntsc(timebase: float) -> bool:
    """Check if a timebase value corresponds to an NTSC frame rate.

    Args:
        timebase: The timebase value (e.g. 30 for 29.97fps NTSC)

    Returns:
        True if the timebase is an NTSC rate
    """
    # NTSC rates are integer timebases that actually represent fractional fps
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
        if not has_ntsc:
            issues.append(Issue(
                CRITICAL, "C3",
                "<rate> missing <ntsc>",
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
    issue_map = {(i.rule_id, i.message): i for i in issues}

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
            if tb and _is_ntsc(float(tb)):
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
    tb.text = "30"
    ntsc = ET.SubElement(rate, "ntsc")
    ntsc.text = "TRUE"

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

    rate = ET.SubElement(tc, "rate")
    tb = ET.SubElement(rate, "timebase")
    tb.text = "30"
    ntsc = ET.SubElement(rate, "ntsc")
    ntsc.text = "TRUE"

    string = ET.SubElement(tc, "string")
    string.text = "00;00;00;00"
    frame = ET.SubElement(tc, "frame")
    frame.text = "0"
    source = ET.SubElement(tc, "source")
    source.text = "source"
    df = ET.SubElement(tc, "displayformat")
    df.text = "DF"

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
        if tb and _is_ntsc(float(tb)) and rate_elem.find("ntsc") is None:
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
        f"> Tool: prxml2fcp7xml v{VERSION}",
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
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def _print_banner() -> None:
    """Print the tool banner."""
    print("=" * 56)
    print(f"  prxml2fcp7xml v{VERSION}")
    print(f"  Premiere Pro FCP7 XML Fixer for DaVinci Resolve")
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
            root = load_prproj(input_path)
            print(f"  Format: .prproj (Premiere Pro project)")
        else:
            root = load_xml(input_path)
            print(f"  Format: FCP7 XML")
    except Exception as e:
        print(f"  ❌ Error loading file: {e}")
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

    print()
    print(f"  Done. {fix_count} fixes applied to {output_path.name}")
    return 0


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        prog="prxml2fcp7xml",
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
    )


if __name__ == "__main__":
    sys.exit(main())
