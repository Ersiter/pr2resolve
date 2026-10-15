"""pr2engine - pr2resolve core engine.

Consolidated module: diagnostics, fix engine, validator, output,
prproj parser, and DRT bridge.
"""

from __future__ import annotations

import copy
import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from xml.dom import minidom

from pr2_constants import (
    VERSION, DEFAULT_FPS, MICROSECOND, NTSC_RATES, PAL_RATES, FPS_TOLERANCE,
    PP_LABEL_TO_RESOLVE_COLOR, RESOLVE_CLIP_COLORS,
    FCP7_VERSION, FCP7_DOCTYPE,
    CRITICAL, MAJOR, MINOR,
    Issue, ScaleIssue, ClipData, FileData, ProjectBin, LinkMember, LinkGroup,
    FilterParam, FilterSpec, TrackData, TransitionData, TimelineState, TimelineMarkerData,
    _SourceTCInfo, LumetriGrade, LumetriParameter,
    _build_file_index, _get_sequence_format, _get_sequence_resolution,
    _is_ntsc, _timeline_track_states, load_xml, load_prproj,
)
from pr2_lumetri import extract_lumetri_clips
from pr2_native_grade import build_lumetri_drx, build_lumetri_node_plan
from pr2_captions import extract_captions
from pr2_native_time import extract_fcp7_retime_alignment
from pr2_retime_output import apply_retimes_to_archive


# ═══════════════════════════════════════════════════════════════════════════════
# Source metadata is read from the .prproj object graph.  A project conversion
# must remain useful when the referenced media is offline or on another machine.
# ═══════════════════════════════════════════════════════════════════════════════

# Scale-to-frame preservation flag (read from .prproj ShouldScaleMedia)
_scaletoframe_enabled: bool = True  # default True for FCP7 XML path (no prproj)


# ─── From pr2_utils.py ─────────────────────────────────────────────

def _recycle(path: Path) -> None:
    """Move a file to the system recycle bin / trash. Never permanently delete.

    Platform support:
    - Windows: PowerShell shell API -> Recycle Bin
    - macOS: ~/.Trash
    - Linux: gio trash (GNOME/KDE) or ~/.local/share/Trash/files/ (XDG spec)

    Args:
        path: Path to the file to recycle
    """
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            backup_dir = Path.home() / "Desktop" / "Recycle_Bin_Backup"
            backup_dir.mkdir(parents=True, exist_ok=True)
            result = subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"Move-Item -LiteralPath '{path}' -Destination '{backup_dir}' -Force",
            ], capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                # Fallback: try Microsoft.VisualBasic
                subprocess.run([
                    "powershell", "-NoProfile", "-Command",
                    "Add-Type -AssemblyName Microsoft.VisualBasic;"
                    f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                    f"'{path}','OnlyErrorDialogs','SendToRecycleBin')",
                ], capture_output=True, text=True, timeout=10)
        elif sys.platform == "darwin":
            trash = Path.home() / ".Trash"
            trash.mkdir(exist_ok=True)
            path.rename(trash / path.name)
        else:
            result = subprocess.run(
                ["gio", "trash", str(path)],
                capture_output=True, timeout=10
            )
            if result.returncode != 0:
                trash_files = Path(os.environ.get(
                    "XDG_DATA_HOME",
                    str(Path.home() / ".local" / "share")
                )) / "Trash" / "files"
                trash_files.mkdir(parents=True, exist_ok=True)
                path.rename(trash_files / path.name)
    except Exception as e:
        print(f"  Warning: could not recycle {path.name}: {e}")

# ─── From pr2_diagnostics.py ─────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════════════
# NTSC Helpers
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


# _is_ntsc imported from pr2_constants — single source of truth


def _effective_source_fps(source_tc: _SourceTCInfo, timeline_fps: float) -> float:
    """Choose the most reliable fps for pproTicks -> frame conversion."""
    return source_tc.media_fps if source_tc.media_fps_resolved else timeline_fps


def _has_resolved_source_duration(source_tc: _SourceTCInfo) -> bool:
    """Check whether source duration is reliable enough for clamping/output."""
    return source_tc.duration_resolved and source_tc.full_duration_frames > 0


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

    # C1: Missing <format> under video (DC has full format with codec)
    seq = root.find("sequence")
    if seq is not None:
        vformat = seq.find("media/video/format")
        if vformat is None:
            issues.append(Issue(
                CRITICAL, "C1",
                "Missing <format> under sequence/media/video",
                "sequence/media/video",
            ))
    # C2: DISABLED — DC format: no <format> in audio section

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
            if tb_val and _is_ntsc(float(tb_val)):
                issues.append(Issue(
                    CRITICAL, "C3",
                    "<rate> missing <ntsc> (NTSC timebase)",
                    f"rate (parent context unavailable in iter)",
                ))

    # C5: pathurl format — accept file:/// or file://localhost/
    for pathurl_elem in root.iter("pathurl"):
        url = pathurl_elem.text or ""
        if url and not url.startswith("file:///") and not url.startswith("file://localhost/"):
            issues.append(Issue(
                CRITICAL, "C5",
                f'pathurl "{url[:60]}..." is not a valid file URI',
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

        # M1: DISABLED — DC format does not use <masterclipid>
        # (was: detect missing masterclipid)

        # M2: DISABLED — DC format: video clipitems don't have sourcetrack
        # Audio clipitems already have sourcetrack from the rewrite

        # M5: DISABLED — DC format: audio clipitems use self-closing <file> refs
        # (was: detect missing media/video/samplecharacteristics in <file>)

        file_elem = clipitem.find("file")

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
    # M6: DISABLED — DC format child order is stable from parser
    # (was: detect clipitem children not in FCP7 order)

    # M3: Sequence duration vs last clip end mismatch
    seq = root.find("sequence")
    if seq is not None:
        seq_dur_text = seq.findtext("duration", "")
        if seq_dur_text:
            try:
                seq_dur = int(seq_dur_text)
            except ValueError:
                seq_dur = 0
            # Find last clip end across all tracks
            last_end = 0
            for clipitem in root.iter("clipitem"):
                end_text = clipitem.findtext("end", "")
                if end_text:
                    try:
                        last_end = max(last_end, int(end_text))
                    except ValueError:
                        pass
            if last_end > 0 and last_end != seq_dur:
                issues.append(Issue(
                    MAJOR, "M3",
                    f"Sequence duration ({seq_dur}) != last clip end ({last_end})",
                    "sequence",
                ))

    return issues


def _scan_minor(root: ET.Element) -> list[Issue]:
    """Scan for MINOR issues (N1-N8).

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

    # N8: Zero file timecode on clips with local media (likely mismatch)
    # When file timecode is 00:00:00:00 but the actual media file has
    # embedded timecode (e.g. DJI drone footage at 13:01:15:00),
    # DaVinci Resolve warns about timecode mismatch on import.
    for file_elem in root.iter("file"):
        tc = file_elem.find("timecode")
        if tc is not None:
            tc_str = tc.findtext("string", "")
            pu = file_elem.findtext("pathurl", "")
            if tc_str in ("00:00:00:00", "00;00;00;00") and pu and pu.startswith("file:///"):
                issues.append(Issue(
                    MINOR, "N8",
                    f"Zero file timecode may mismatch source media timecode; "
                    f"consider re-generating from .prproj with source TC detection",
                    f"file[id={file_elem.get('id', '?')}]",
                ))

    return issues


def _detect_scale_mismatch(
    clipitem: ET.Element,
    file_elem: ET.Element,
    seq_format: ET.Element,
) -> Optional[ScaleIssue]:
    """Check if source fit-to-frame scaling was lost during PR XML export.

    PR's "Scale to Frame Size" is NOT preserved in FCP7 XML export.
    When source resolution != timeline resolution and scale=100%,
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
    if basic_filter is not None and basic_filter.findall(".//keyframe"):
        return None
    current_scale = 100.0  # Default when no basic effect exists
    if basic_filter is not None:
        scale_param = basic_filter.find("parameter/[name='Scale']")
        if scale_param is not None:
            current_scale = float(scale_param.findtext("value") or "100")

    # 3b. Respect Premiere "Scale to Frame Size" toggle
    # When the prproj has ShouldScaleMedia=false, the user explicitly
    # chose NOT to scale media. Auto-fit would change their intent.
    if not _scaletoframe_enabled:
        return None

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

    # 5. If scale != 100%, user manually set it — trust user
    if abs(current_scale - 100.0) > 0.01:
        return None

    # 6. If resolutions match, no issue
    if src_w == tl_w and src_h == tl_h:
        return None

    # 6b. If source already fits within timeline, skip (no upscaling needed)
    #     PR's "Scale to Frame Size" only downscales, never upscales.
    if src_w <= tl_w and src_h <= tl_h:
        return None

    # 7. Fit by smaller dimension (PR "Scale to Frame Size" behavior)
    #    Portrait source in landscape timeline? Fit by height.
    #    Landscape source in portrait timeline? Fit by width.
    fit_scale = min(tl_w / src_w, tl_h / src_h) * 100.0

    # 7b. Never upscale past 100%
    fit_scale = min(fit_scale, 100.0)

    # Threshold: < 0.5% difference is float noise
    if abs(fit_scale - 100.0) < 0.5:
        return None

    return ScaleIssue(
        source_res=(src_w, src_h),
        timeline_res=(tl_w, tl_h),
        current_scale=current_scale,
        corrected_scale=round(fit_scale, 1),
    )

# ─── From pr2_fix_engine.py ─────────────────────────────────────────────

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

    # C1: Insert missing video format (DC has full format with codec AFTER tracks)
    seq = root.find("sequence")
    if seq is not None:
        video = seq.find("media/video")
        if video is not None and video.find("format") is None:
            fmt = _create_video_format(root)
            video.append(fmt)  # DC: format comes AFTER all tracks
            fix_count += 1
            _mark_fixed(issues, "C1", "video")
    # C2: DISABLED — DC format: no <format> in audio
    if any(i.rule_id == "C2" for i in issues):
        _mark_fixed(issues, "C2", "audio")

    # C3: Add missing <ntsc> to <rate> elements
    for rate_elem in root.iter("rate"):
        if rate_elem.find("ntsc") is None:
            tb = rate_elem.findtext("timebase")
            if tb and _is_ntsc(float(tb)):
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

    # C5: Fix pathurl format — normalize cross-OS variants to PR-compatible format
    for pathurl_elem in root.iter("pathurl"):
        url = pathurl_elem.text or ""
        # Already correct: file://localhost/ (PR format) or file:/// (standard)
        if url.startswith("file://localhost/") or url.startswith("file:///"):
            continue
        if url.startswith("file://"):
            # Bare file:// → insert localhost/
            pathurl_elem.text = "file://localhost/" + url[len("file://"):]
            fix_count += 1
        elif not url.startswith("file://"):
            pathurl_elem.text = "file://localhost/" + url
            fix_count += 1
    if any(i.rule_id == "C5" for i in issues):
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

    # M1: DISABLED — DC not use masterclipid
    if any(i.rule_id == "M1" for i in issues):
        _mark_fixed(issues, "M1", "masterclipid")

    # M2: DISABLED — DC format handles sourcetrack in parser
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

    # M5: DISABLED — DC format: audio clipitems use self-closing <file> refs
    # Self-closing <file id="..."/> in audio clipitems is correct — DO NOT inject SC
    if any(i.rule_id == "M5" for i in issues):
        _mark_fixed(issues, "M5", "samplecharacteristics")

    # M6: DISABLED — DC format order is stable from parser
    if any(i.rule_id == "M6" for i in issues):
        _mark_fixed(issues, "M6", "order")

    # M3: Fix sequence duration to match last clip end
    if any(i.rule_id == "M3" for i in issues):
        seq = root.find("sequence")
        if seq is not None:
            last_end = 0
            for clipitem in root.iter("clipitem"):
                end_text = clipitem.findtext("end", "")
                if end_text:
                    try:
                        last_end = max(last_end, int(end_text))
                    except ValueError:
                        pass
            if last_end > 0:
                dur_elem = seq.find("duration")
                if dur_elem is not None:
                    dur_elem.text = str(last_end)
                    fix_count += 1
                    _mark_fixed(issues, "M3", "duration")

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

    # N1: Add missing or normalize non-zero sequence timecode
    tc_in = seq.find("timecode") if seq is not None else None
    if seq is not None and tc_in is None:
        tc_n = _create_timecode(root)
        name_idx = _find_child_index(seq, "name")
        if name_idx >= 0:
            seq.insert(name_idx + 1, tc_n)
        else:
            seq.insert(0, tc_n)
        fix_count += 1
    elif tc_in is not None:
        s = tc_in.findtext("string", "")
        f = tc_in.findtext("frame", "0")
        if s not in ("00:00:00:00", "00;00;00;00") or f != "0":
            si = tc_in.find("string")
            fi = tc_in.find("frame")
            if si is not None:
                nt = tc_in.find("rate/ntsc") is not None and tc_in.findtext("rate/ntsc", "") == "TRUE"
                si.text = "00;00;00;00" if nt else "00:00:00:00"
            if fi is not None:
                fi.text = "0"
            # Remove source=source if present (it was a bug)
            if tc_in.get("source"):
                del tc_in.attrib["source"]
            fix_count += 1
    if any(i.rule_id == "N1" for i in issues):
        _mark_fixed(issues, "N1", "timecode")

    # N2: Add missing <displayformat> to timecode elements
    for tc in root.iter("timecode"):
        if tc.find("displayformat") is None:
            # DF/NDF depends on actual frame rate, not just NTSC flag.
            # 23.976/24fps NDF (no drop-frame for film rates).
            # 29.97fps DF, 30.00fps NDF, 59.94fps DF, 60.00fps NDF.
            tc_tb = tc.findtext("rate/timebase", "30")
            tc_has_ntsc = tc.find("rate/ntsc") is not None
            is_df = False
            try:
                tb_val = float(tc_tb)
                # NTSC drop-frame: only fractional rates (23.976, 29.97, 59.94)
                if tc_has_ntsc and abs(tb_val - round(tb_val)) < 0.01:
                    # integer timebase with ntsc = check if fractional NTSC rate
                    # 24 -> 23.976 NDF, 30 -> 29.97 DF, 60 -> 59.94 DF
                    is_df = tb_val in [30, 60]
                elif abs(tb_val - 29.97) < 0.01:
                    is_df = True
                elif abs(tb_val - 59.94) < 0.01:
                    is_df = True
            except ValueError:
                pass
            df = ET.SubElement(tc, "displayformat")
            df.text = "DF" if is_df else "NDF"
            fix_count += 1
    if any(i.rule_id == "N2" for i in issues):
        _mark_fixed(issues, "N2", "displayformat")

    # N3: Fix -1 sentinel values in <in>/<out>
    for clipitem in root.iter("clipitem"):
        in_el = clipitem.find("in")
        out_el = clipitem.find("out")
        if in_el is not None and in_el.text == "-1":
            in_el.text = "0"
            fix_count += 1
        if out_el is not None and out_el.text == "-1":
            # -1 out means "end of media" — use clip duration
            dur_text = clipitem.findtext("duration", "")
            if dur_text:
                out_el.text = dur_text
            else:
                out_el.text = "0"
            fix_count += 1
    if any(i.rule_id == "N3" for i in issues):
        _mark_fixed(issues, "N3", "-1")

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
    ET.SubElement(sc, "pixelaspectratio").text = "square"

    # Codec (minimal — matches 5 of 6 DC reference files)
    codec = ET.SubElement(sc, "codec")
    appdata = ET.SubElement(codec, "appspecificdata")
    ET.SubElement(appdata, "appname").text = "Final Cut Pro"
    ET.SubElement(appdata, "appmanufacturer").text = "Apple Inc."
    data = ET.SubElement(appdata, "data")
    ET.SubElement(data, "qtcodec")

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
    ET.SubElement(param, "parameterid").text = "scale"
    pval = ET.SubElement(param, "value")
    pval.text = str(scale)

    # Rotation (default 0)
    param2 = ET.SubElement(eff, "parameter")
    pname2 = ET.SubElement(param2, "name")
    pname2.text = "Rotation"
    ET.SubElement(param2, "parameterid").text = "rotation"
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

# ─── From pr2_validator.py ─────────────────────────────────────────────

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
    # V8: DISABLED — DC format: no <format> in audio

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

    # V13: DISABLED — DC format does not use <masterclipid>
    # V14: DISABLED — DC format: video clipitems don't have <sourcetrack>

    # V15: pathurl uses file:/// or file://localhost/ format
    for pu in root.iter("pathurl"):
        url = pu.text or ""
        if url and not url.startswith("file:///") and not url.startswith("file://localhost/"):
            issues.append(Issue(MAJOR, "V15", f"pathurl not a file URI", "pathurl"))

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

    # V22: No excessive duplicate file ids (1 video + up to 2 audio refs = max 3)
    file_ids: dict[str, int] = {}
    for f in root.iter("file"):
        fid = f.get("id")
        if fid:
            file_ids[fid] = file_ids.get(fid, 0) + 1
    for fid, count in file_ids.items():
        if count > 3:
            issues.append(Issue(MAJOR, "V22", f"Duplicate file id: {fid} ({count}x)", "file"))

    # V23: Clipitem element order (check first few)
    for ci in list(root.iter("clipitem"))[:5]:
        ci_id = ci.get("id", "?")
        child_tags = [c.tag for c in ci]
        # Check that 'file' comes before 'filter'
        if "file" in child_tags and "filter" in child_tags:
            if child_tags.index("file") > child_tags.index("filter"):
                issues.append(Issue(MINOR, "V23", f"<file> should come before <filter>", f"clipitem[{ci_id}]"))

    return issues

# ─── From pr2_output.py ─────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# Output — XML Writer & Report Generator
# ═══════════════════════════════════════════════════════════════════════════════

def _make_output_name(seq_name: str, add_suffix: bool = True, suffix: str | None = None) -> str:
    """Build output filename from sequence name.

    Sanitizes the sequence name for filesystem safety and appends the
    configured suffix (default: OUTPUT_SUFFIX from pr2_constants).

    Args:
        seq_name: The timeline/sequence name from the project
        add_suffix: Whether to append the configured suffix
        suffix: Custom suffix override (defaults to OUTPUT_SUFFIX)

    Returns:
        Safe filename with .xml extension, e.g. '序列 01_pr2resolve.xml'
    """
    if suffix is None:
        from pr2_constants import OUTPUT_SUFFIX
        suffix = OUTPUT_SUFFIX
    # Replace filesystem-unsafe characters
    safe = seq_name.replace("/", "_").replace("\\", "_").replace(":", "_") \
                   .replace("*", "_").replace("?", "_").replace("\"", "_") \
                   .replace("<", "_").replace(">", "_").replace("|", "_")
    if add_suffix:
        return f"{safe}{suffix}.xml"
    return f"{safe}.xml"


def _to_fcp7_pathurl(filepath: str) -> str:
    """Convert a Windows path to FCP7 XML pathurl format.

    Matches DaVinci Resolve's own XML export format:
      file://localhost/E:/path/to/file.mov

    Args:
        filepath: Native absolute media path

    Returns:
        DaVinci-compatible file://localhost/ URI
    """
    from urllib.parse import quote
    path = filepath.replace("\\", "/")
    # DaVinci uses plain drive letter (E:/ not E%3a/), uppercase percent encoding
    import re
    encoded = quote(path, safe="/:")  # preserve / and :
    # Uppercase only percent-encoded hex sequences (DC convention: %E8 not %e8)
    encoded = re.sub(r'%[0-9a-f]{2}', lambda m: m.group(0).upper(), encoded)
    return f"file://localhost/{encoded}"


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
    root: ET.Element | None = None,
) -> None:
    """Generate an issue-tracker-ready markdown fix report.

    Args:
        scan_issues: Issues found by _scan()
        validation_issues: Issues found by _validate()
        fix_count: Number of fixes applied
        input_path: Original input file path
        output_path: Fixed output file path
        report_path: Path to write the report
        root: Optional fixed FCP7 XML root for timeline analysis
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"# Fix Report: {input_path.name}",
        "",
        f"> Generated: {now} | Tool: pr2resolve v{VERSION}",
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

    # Timeline summary if xml root provided
    if root is not None:
        seq = root.find("sequence")
        if seq is not None:
            dur = seq.findtext("duration", "?")
            w = seq.findtext("media/video/format/samplecharacteristics/width", "?")
            h = seq.findtext("media/video/format/samplecharacteristics/height", "?")
            tb = seq.findtext("rate/timebase", "?")
            ntsc = "DF" if seq.find("rate/ntsc") is not None else "NDF"
            v_clips = len(seq.findall(".//media/video/track/clipitem"))
            a_clips = len(seq.findall(".//media/audio/track/clipitem"))
            v_tracks = len(seq.findall(".//media/video/track") or [])
            a_tracks = len(seq.findall(".//media/audio/track") or [])
            lines += [
                "## Timeline",
                "",
                f"| Property | Value |",
                f"|----------|-------|",
                f"| Duration | {dur} frames |",
                f"| Resolution | {w}x{h} |",
                f"| Frame rate | {tb} fps ({ntsc}) |",
                f"| Video tracks | {v_tracks} ({v_clips} clip items) |",
                f"| Audio tracks | {a_tracks} ({a_clips} clip items) |",
                "",
            ]

            animated = sum(clip.find(".//keyframe") is not None for clip in seq.iter("clipitem"))
            retimed = sum(
                (parameter.findtext("value") or "100") not in ("100", "100.0")
                for effect in seq.findall(".//filter/effect") if effect.findtext("effectid") == "timeremap"
                for parameter in effect.findall("parameter") if parameter.findtext("parameterid") == "speed"
            )
            lines += [
                "## Conversion fidelity",
                "",
                "Structural validation does not verify Resolve import, rendered frames, or audio playback.",
                "",
                f"- Animated clips: {animated}. Premiere curves are linearized in FCP7 XML; raw records remain in the timeline-state comment. Native animated Motion is unsupported.",
                f"- Retimed clips: {retimed}. Native video supports forward constant speed and explicit monotone graphdict points as a linear-segment approximation. Audio retiming, holds, reverse, Bezier handles and transition offsets remain unsupported.",
                "- API DRT/DRP packages receive the same supported retime maps after export. A missing or ambiguous destination fails export validation; the live API timeline is unchanged. Import/render parity is unverified.",
                f"- Transitions: {len(seq.findall('.//transitionitem'))}; nested sequences: {len(seq.findall('.//clipitem/sequence'))}; generators: {len(seq.findall('.//generatoritem'))}. Native export rejects these structures.",
                "- Native sequence markers and track name/mute/lock have offline coverage; Resolve UI application is unverified. Marker color mapping, native in/out and work area, independent track colors, and clip flags are unsupported or unverified.",
                "- Lumetri: native and API use one shared DRX graph, with a serial node per source effect. Exposure, Contrast and Saturation are approximate; other controls, shader stages and animation remain source records. Resolve loading/rendering and visible source start-timecode application are unverified.",
                "",
            ]
            for clip in seq.iter("clipitem"):
                if clip.find("filter/effect/parameter[parameterid='graphdict']/keyframe") is None:
                    continue
                identity = (clip.findtext("name") or "clip").replace("\n", " ")
                try:
                    timeline_rate = float(tb) * (1000 / 1001 if seq.findtext("rate/ntsc", "FALSE").upper() == "TRUE" else 1)
                    alignment = extract_fcp7_retime_alignment(clip, timeline_rate)
                    lines.append(f"- Retime alignment {identity}: record In={alignment['record_in_frames']:.9g} timeline frames; source In={alignment['source_in_seconds']:.9g}s; last visible source={alignment['last_visible_sample']}; last-frame residual={alignment['source_trim_end_residual_frames']} clip frames. Timeline start/end are unchanged; the raw curve remains in XML.")
                except ValueError as exc:
                    lines.append(f"- Native retime rejected for {identity}: {exc}")
            lines.append("")
            source_records: list[dict[str, Any]] = []
            lumetri_records: list[dict[str, Any]] = []
            caption_records: list[dict[str, Any]] = []
            trim_records: list[dict[str, Any]] = []
            for comment in root:
                if comment.tag is not ET.Comment or not (comment.text or "").strip().startswith("pr2resolve-timeline-state:"):
                    continue
                state = json.loads(base64.b64decode(comment.text.strip().split(":", 1)[1]).decode("utf-8"))
                source_records.extend(json.loads(raw) for key, raw in state.get("unknown", [])
                                      if key.startswith("unconverted-source:"))
                lumetri_records.extend(json.loads(raw) for key, raw in state.get("unknown", []) if key.startswith("lumetri-source:"))
                caption_records.extend(json.loads(raw) for key, raw in state.get("unknown", []) if key.startswith("caption-source:"))
                trim_records.extend(json.loads(raw) for key, raw in state.get("unknown", []) if key.startswith("clip-trim:"))
            if trim_records:
                errors = [abs(record[key]) for record in trim_records for key in ("in_error_seconds", "out_error_seconds") if record.get(key) is not None]
                lines += [f"- Source trims: {len(trim_records)} raw tick records retained; maximum reported trim quantization/clamping error={max(errors, default=0):.9g}s.", ""]
            if lumetri_records:
                lines += ["### Lumetri source stacks", "", "Serial-node mapping is approximate; ordered raw shader XML, keyframes and parameters remain in the timeline-state comment.", ""]
                for record in lumetri_records:
                    effects = record.get("effects", [])
                    lines.append(f"- {record['clip_name']} on V{record['track_index']}: {len(effects)} ordered effects.")
                    for effect in effects:
                        keyframes = (effect.get("blob") or {}).get("keyframes", [])
                        stages = list(dict.fromkeys(shader.get("name", "") for keyframe in keyframes for shader in keyframe.get("shaders", [])))
                        lines.append(f"  - {effect.get('display_name') or effect['component_ref']}: {len(effect.get('parameters', []))} parameter records, {len(keyframes)} source keyframes; stages: {', '.join(stages)}. Only supported static scalar candidates are written.")
                lines.append("")
            if caption_records:
                decoded = sum(record.get("text") is not None and not record.get("decode_errors") for record in caption_records)
                lines += [f"- Captions: {decoded}/{len(caption_records)} text records decoded from FormattedTextData. Text runs, fonts, source ticks and raw payloads are retained; subtitle styling and native/API caption application remain incomplete.", ""]
            if source_records:
                lines += ["### Source features requiring conversion", "",
                          "These records were captured from the Premiere graph before XML normalization. Native export rejects them; raw records remain in the XML comment.", ""]
                for record in source_records:
                    identity = str(record.get("clip_name") or record.get("track_uid") or "sequence").replace("\n", " ")
                    feature = record.get("match_name") or record.get("kind")
                    representation = record.get("representation") or "not represented by FCP7 conversion"
                    lines.append(f"- {identity}: {feature} ({representation})")
                lines.append("")

    # Issues by severity with root cause hints
    for severity in [CRITICAL, MAJOR, MINOR]:
        sev_issues = [i for i in scan_issues if i.severity == severity]
        if not sev_issues:
            continue
        lines.append(f"## {severity} Issues ({len(sev_issues)})")
        lines.append("")
        lines.append("| Rule | Status | Description | Root Cause |")
        lines.append("|------|:------:|-------------|------------|")
        for issue in sev_issues:
            status = "Fixed" if issue.fixed else "Unfixed"
            cause = _root_cause_hint(issue)
            lines.append(f"| {issue.rule_id} | {status} | {issue.message} | {cause} |")
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
        lines.append("Structural XML checks passed. Resolve import/rendering is not verified.")
        lines.append("")

    # Footer
    lines += [
        "---",
        "",
        f"_Report generated by [pr2resolve](https://github.com) v{VERSION}._",
        f"_For issue submission, attach this report together with the input file._",
    ]

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(report_path), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _root_cause_hint(issue: Issue) -> str:
    """Return a one-line root cause hint for a known issue rule.

    Args:
        issue: An Issue instance whose rule_id is looked up in the hint table.

    Returns:
        A short root cause explanation, or ``"See description"`` if the
        rule_id has no registered hint.
    """
    hints = {
        "C0": "PR exports xmeml v4; FCP7 spec requires v5",
        "C1": "PR omits video <format> element in some export modes",
        "C2": "PR omits audio <format> element in some export modes",
        "C3": "PR exports integer timebase without <ntsc> flag for NTSC rates",
        "C4": "Malformed or missing <timebase> in rate element",
        "C5": "Pathurl uses file:/// or file://localhost/ (both accepted for DaVinci compat)",
        "C6": "PR exports audio section before video in <media>",
        "M0": "Lumetri is a PR-only effect; DaVinci has no equivalent plugin",
        "M1": "PR does not output FCP7-native <masterclipid>",
        "M2": "PR omits <sourcetrack> which DaVinci requires for track routing",
        "M4": "Linked audio/video from same source need <link> for sync",
        "M5": "PR exports <file> without per-clip samplecharacteristics",
        "M6": "PR exports clipitem children in non-standard order",
        "M7": "Scale to Frame Size is a PR display strategy, not stored in XML",
        "N1": "PR omits sequence-level <timecode> element",
        "N2": "Timecode missing <displayformat> (DF/NDF indicator)",
        "N3": "Transitions or gaps produce sentinel value -1 in <in>/<out>",
        "N4": "Empty tracks are preserved for creator intent fidelity",
        "N5": "Mixed frame rate timeline has multiple timebase values",
        "N6": "Floating-point arithmetic produces near-zero values (e.g. 2.18e-10)",
        "N7": "Disabled/locked tracks are preserved for creator intent fidelity",
        "N8": "Zero file timecode may mismatch embedded media timecode (DaVinci import warning)",
    }
    return hints.get(issue.rule_id, "See description")

# ─── From pr2_prproj_parser.py ─────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

# Adobe timebase conversion constant
_ADOBE_TIMEBASE_CONSTANT = 10594584000
_NTSC_FILM_RATE = 24000.0 / 1001.0  # 23.976023976… exact NTSC film rate

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

# _is_ntsc_fps imported above (single source of truth)

def _prproj_adobe_timebase_to_fps(timebase: int) -> float:
    """Convert Adobe internal timebase to actual fps.

    Args:
        timebase: Adobe internal timebase value

    Returns:
        Actual frames per second
    """
    if timebase <= 0:
        return DEFAULT_FPS
    fps = round((_ADOBE_TIMEBASE_CONSTANT * _NTSC_FILM_RATE) / timebase, 6)
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
    if ticks < 0:
        return 0
    # pproTicks per second = 254016000000
    ppro_ticks_per_sec = 254016000000
    seconds = ticks / ppro_ticks_per_sec
    return int(round(seconds * fps))


def _prproj_parse_marker_owner(
    owner: Optional[ET.Element], idx: _PrprojIndex, fps: float,
    *, scope: str = "sequence", track_type: str = "", track_index: int = 0,
) -> list[TimelineMarkerData]:
    """Resolve Premiere marker references while retaining source payloads."""
    if owner is None:
        return []
    markers_ref = owner.find("Markers")
    markers_obj = idx.resolve_ref(markers_ref.get("ObjectRef", "")) if markers_ref is not None else None
    if markers_obj is None:
        return []
    result: list[TimelineMarkerData] = []
    for marker_ref in markers_obj.findall(".//Marker/Second"):
        marker = idx.resolve_ref(marker_ref.get("ObjectRef", ""))
        if marker is None:
            continue
        payload = marker.findtext("DVAMarker", "")
        try:
            data = json.loads(payload)
            details = data.get("DVAMarker", {})
        except (json.JSONDecodeError, AttributeError):
            details = {}
        if not isinstance(details, dict):
            details = {}
        start_data = details.get("mStartTime", {})
        duration_data = details.get("mDuration", {})
        start = start_data.get("ticks") if isinstance(start_data, dict) else None
        duration = duration_data.get("ticks") if isinstance(duration_data, dict) else None
        try:
            start_frame = _prproj_ticks_to_frames(str(start), fps) if start is not None else 0
            end_frame = (start_frame + _prproj_ticks_to_frames(str(duration), fps)
                         if duration is not None else -1)
        except (TypeError, ValueError):
            start_frame, end_frame = 0, -1
        result.append(TimelineMarkerData(
            name=str(details.get("mName", "")),
            comment=str(details.get("mComments", details.get("mComment", ""))),
            start_frame=start_frame,
            end_frame=end_frame,
            marker_type=str(details.get("mType", "Comment")),
            color_index=(int(details["mColorIndex"]) if str(details.get("mColorIndex", "")).isdigit() else None),
            raw_payload=payload,
            scope=scope,
            track_type=track_type,
            track_index=track_index,
        ))
    return result


def _prproj_extract_timeline_state(
    sequence: ET.Element, idx: _PrprojIndex, fps: float,
) -> TimelineState:
    """Extract known timeline state and inventory untranslatable Premiere fields."""
    state = TimelineState(sequence_uid=sequence.get("ObjectUID", ""))
    for source_name, target_name in (
        ("MZ.InPoint", "in_frame"), ("MZ.OutPoint", "out_frame"),
        ("MZ.WorkInPoint", "work_in_frame"), ("MZ.WorkOutPoint", "work_out_frame"),
    ):
        raw = sequence.findtext(f"Node/Properties/{source_name}")
        if raw is None:
            continue
        setattr(state, f"raw_{target_name.replace('_frame', '_ticks')}", raw)
        try:
            ticks = int(raw)
        except ValueError:
            state.unknown.append((source_name, raw))
            continue
        if ticks >= 0:
            setattr(state, target_name, _prproj_ticks_to_frames(raw, fps))
        else:
            state.unknown.append((source_name, raw))
    state.markers.extend(_prproj_parse_marker_owner(sequence.find("MarkerOwner"), idx, fps))
    known = {"MZ.InPoint", "MZ.OutPoint", "MZ.WorkInPoint", "MZ.WorkOutPoint"}
    for element in sequence.iter():
        if element.tag.startswith("MZ.") and element.tag not in known and element.text:
            state.unknown.append((element.tag, element.text.strip()))
    for group_ref in sequence.findall("TrackGroups/TrackGroup/Second"):
        group = idx.resolve_ref(group_ref.get("ObjectRef", ""))
        tracks = group.find(".//Tracks") if group is not None else None
        if tracks is None:
            continue
        for track_index, track_ref in enumerate(tracks.findall("Track"), 1):
            track = idx.resolve_uref(track_ref.get("ObjectURef", ""))
            if track is None:
                continue
            track_type = "video" if "Video" in track.tag else "audio" if "Audio" in track.tag else "data"
            for element in track.findall(".//Node/Properties/*"):
                if element.tag.startswith("MZ.") and element.text:
                    state.unknown.append((f"track:{track_type}:{track_index}:{element.tag}", element.text.strip()))
            track_node = track.find("ClipTrack/Track")
            if track_node is not None:
                props = track_node.find("Node/Properties")
                track_state = {
                    "muted": (track_node.findtext("IsMuted", "false").lower() == "true"),
                    "locked": (track_node.findtext("IsLocked", "false").lower() == "true"),
                    "solo": (track_node.findtext("Solo", "0") not in ("", "0", "false")),
                    "name": props.findtext("MZ.TrackName", "") if props is not None else "",
                }
                state.unknown.append((f"track-state:{track_type}:{track_index}", json.dumps(track_state, sort_keys=True)))
            state.markers.extend(_prproj_parse_marker_owner(
                track.find("MarkerOwner"), idx, fps, scope="track",
                track_type=track_type, track_index=track_index,
            ))
    for marker in state.markers:
        if marker.raw_payload:
            try:
                details = json.loads(marker.raw_payload).get("DVAMarker", {})
            except (json.JSONDecodeError, AttributeError):
                details = {}
            known_marker_fields = {"mStartTime", "mDuration", "mType", "mName", "mComments", "mComment", "mColorIndex"}
            for key, value in details.items():
                if key not in known_marker_fields:
                    state.unknown.append((f"marker.{key}", json.dumps(value, ensure_ascii=False, sort_keys=True)))
    return state


def _prproj_get_source_resolution(
    prproj_root: ET.Element,
    mc_name: str,
    idx: _PrprojIndex,
) -> tuple[int, int]:
    """Extract source media resolution from .prproj VideoStream metadata.

    Walks: Media(filename) → VideoStream[ObjectRef]
           → root-level VideoStream[ObjectID] → FrameRect

    Args:
        prproj_root: The <PremiereData> root element
        mc_name: Media file basename to match
        idx: The prproj index for resolving ObjectRefs

    Returns:
        (width, height) from FrameRect, or (0, 0) on failure
    """
    mc_lower = mc_name.lower()
    for media_el in prproj_root.findall("Media"):
        media_names = (
            media_el.findtext("FilePath", ""),
            media_el.findtext("ActualMediaFilePath", ""),
            media_el.findtext("RelativePath", ""),
            media_el.findtext("Title", ""),
        )
        if not any(Path(value.replace("\\", "/")).name.lower() == mc_lower
                   for value in media_names if value):
            continue
        vs_el = media_el.find("VideoStream")
        if vs_el is None:
            continue
        vs_ref = vs_el.get("ObjectRef")
        if not vs_ref:
            continue
        resolved = idx.resolve_ref(vs_ref)
        if resolved is None:
            continue
        frame_rect = resolved.findtext("FrameRect", "")
        if frame_rect:
            parts = frame_rect.split(",")
            if len(parts) == 4:
                try:
                    w = int(parts[2])
                    h = int(parts[3])
                except ValueError:
                    continue
                if w > 0 and h > 0:
                    return (w, h)
        break
    return (0, 0)


def _prproj_frames_to_timecode_string(total_frames: int, fps: float, is_ntsc: bool) -> str:
    """Convert absolute frame count to timecode string at given frame rate.

    For 29.97 and 59.94 fps (is_ntsc=True), uses drop-frame (DF) labeling:
    frame numbers 00 and 01 are skipped at the start of every minute except
    every 10th minute.  23.976 (also is_ntsc=True) uses NDF with ';'
    separator per convention.

    Args:
        total_frames: Absolute (raw NDF) frame number (0-based)
        fps: Actual frames per second (e.g. 59.94)
        is_ntsc: Use ';' separator (see above for DF vs NDF logic)

    Returns:
        Timecode string like '17:00:23:16' (DF) or '00;00;01;00' (23.976 NDF)
    """
    display_fps = int(round(fps))
    if display_fps <= 0:
        display_fps = 30
    sep = ";" if is_ntsc else ":"

    # ── Drop-frame: apply only to 29.97/59.94, NOT 23.976 ──
    is_dropframe = is_ntsc and display_fps in (30, 60)

    if is_dropframe:
        # raw NDF frame count → DF label.
        # Each non-10th minute drops 2 frames (labels 00 and 01).
        # Per 10-minute block: 9 × (60×fps - 2) + 1 × (60×fps)
        #                    = 10 × 60 × fps - 18 labeled raw frames
        frames_per_minute = display_fps * 60
        frames_per_10min = 10 * frames_per_minute - 9 * 2

        total_minutes = 0
        remaining = total_frames

        blocks_10 = remaining // frames_per_10min
        total_minutes += blocks_10 * 10
        remaining = remaining % frames_per_10min

        for _ in range(9):  # minutes 0–8 (non-10th)
            needed = frames_per_minute - 2
            if remaining >= needed:
                remaining -= needed
                total_minutes += 1
            else:
                break

        if remaining >= frames_per_minute and total_minutes % 10 == 9:
            remaining -= frames_per_minute
            total_minutes += 1

        hours = total_minutes // 60
        minutes = total_minutes % 60
        seconds = remaining // display_fps
        frames = remaining % display_fps

        return f"{hours:02d}{sep}{minutes:02d}{sep}{seconds:02d}{sep}{frames:02d}"

    # ── NDF: unchanged arithmetic ──
    hours = total_frames // (3600 * display_fps)
    remainder = total_frames % (3600 * display_fps)
    minutes = remainder // (60 * display_fps)
    remainder = remainder % (60 * display_fps)
    seconds = remainder // display_fps
    frames = remainder % display_fps
    return f"{hours:02d}{sep}{minutes:02d}{sep}{seconds:02d}{sep}{frames:02d}"


def _prproj_extract_source_tc_info(
    mc_el: ET.Element,
    idx: _PrprojIndex,
    timing_fps: float | None = None,
) -> _SourceTCInfo:
    """Extract source media timecode and frame rate from a MasterClip element.

    Follows the MasterClip → LoggingInfo → ClipLoggingInfo reference chain
    in the .prproj object graph to read:
      - MediaFrameRate: Adobe internal timebase → actual fps
      - MediaInPoint:   pproTicks → source start timecode
      - MediaOutPoint:  pproTicks → compute full file duration

    Args:
        mc_el: The resolved MasterClip element
        idx: The prproj index for resolving ObjectRefs

    Returns:
        _SourceTCInfo with extracted values or defaults (resolved=False)
    """
    info = _SourceTCInfo()

    # Follow LoggingInfo → ClipLoggingInfo
    li = mc_el.find("LoggingInfo")
    if li is None:
        return info
    li_ref = li.get("ObjectRef")
    if li_ref:
        cli = idx.resolve_ref(li_ref)
    else:
        li_ref = li.get("ObjectURef")
        cli = idx.resolve_uref(li_ref) if li_ref else None
    if cli is None:
        return info

    # Extract MediaFrameRate (Adobe internal timebase → actual fps)
    mfr_text = cli.findtext("MediaFrameRate")
    if mfr_text and mfr_text.strip():
        try:
            mfr_ticks = int(mfr_text)
            # Skip sentinel values (max int64 = generated/nested sequences)
            if 0 < mfr_ticks < 9_000_000_000_000_000_000:
                info.media_fps = _prproj_adobe_timebase_to_fps(mfr_ticks)
                info.media_fps_resolved = True
        except (ValueError, TypeError):
            pass
    if timing_fps is not None and info.media_fps_resolved:
        # Audio-only LoggingInfo can report its sample clock as MediaFrameRate.
        # Decode all tick-based metadata in the sequence time domain while
        # retaining the original clock separately for audio metadata output.
        info.sample_rate = int(round(info.media_fps))
        info.media_fps = timing_fps
        info.media_fps_resolved = False
    info.is_ntsc = _is_ntsc_fps(info.media_fps)

    # Extract MediaInPoint (pproTicks → timecode)
    mip_text = cli.findtext("MediaInPoint")
    if mip_text and mip_text.strip():
        try:
            mip_ticks = int(mip_text)
            # Zero is a valid project timecode (for example camera clips
            # recorded from midnight); only a negative value is invalid.
            if mip_ticks >= 0:
                info.timecode_frame = _prproj_ticks_to_frames(
                    str(mip_ticks), info.media_fps
                )
                info.timecode_string = _prproj_frames_to_timecode_string(
                    info.timecode_frame, info.media_fps, info.is_ntsc
                )
                info.resolved = True
        except (ValueError, TypeError):
            pass

    # Extract full file duration from MediaOutPoint - MediaInPoint
    mop_text = cli.findtext("MediaOutPoint")
    if mop_text and mip_text and mip_text.strip():
        try:
            mop_ticks = int(mop_text)
            mip_ticks = int(mip_text)
            if mop_ticks > mip_ticks:
                info.full_duration_frames = _prproj_ticks_to_frames(
                    str(mop_ticks - mip_ticks), info.media_fps
                )
                info.duration_resolved = True
        except (ValueError, TypeError):
            pass

    return info


def _prproj_list_sequences(root: ET.Element, idx: _PrprojIndex) -> list[dict[str, Any]]:
    """List all sequences in a .prproj file.

    Args:
        root: The <PremiereData> root element
        idx: The prproj index

    Returns:
        List of dicts with source identity, geometry and media/data item counts.
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

        # Count every clip item across video and audio tracks.
        clip_count = 0
        data_item_count = 0
        tg_section = seq_el.find("TrackGroups")
        if tg_section is not None:
            for tg_pair in tg_section.findall("TrackGroup"):
                second = tg_pair.find("Second")
                if second is not None:
                    ref = second.get("ObjectRef")
                    tg_el = idx.resolve_ref(ref) if ref else None
                    if tg_el is not None and tg_el.tag in ("VideoTrackGroup", "AudioTrackGroup", "DataTrackGroup", "CaptionTrackGroup"):
                        tracks = tg_el.find(".//Tracks")
                        if tracks is not None:
                            for track_ref in tracks.findall("Track"):
                                uref = track_ref.get("ObjectURef")
                                track_el = idx.resolve_uref(uref) if uref else None
                                if track_el is not None:
                                    items = track_el.find(".//TrackItems")
                                    if items is not None:
                                        count = len(items.findall("TrackItem"))
                                        if tg_el.tag in ("VideoTrackGroup", "AudioTrackGroup"):
                                            clip_count += count
                                        else:
                                            data_item_count += count

        sequences.append({
            "uid": uid,
            "name": name,
            "width": w,
            "height": h,
            "clip_count": clip_count,
            "data_item_count": data_item_count,
            "content_count": clip_count + data_item_count,
        })
    return sequences


def _prproj_extract_all_lumetri(
    prproj_root: ET.Element,
    sequence_uid: str,
) -> list[LumetriGrade]:
    """Build a shared serial DRX graph without merging source effects."""
    idx = _PrprojIndex.build(prproj_root)
    sequence = idx.resolve_uref(sequence_uid)
    fps = DEFAULT_FPS
    width, height = 1920, 1080
    for reference in sequence.findall("TrackGroups/TrackGroup/Second") if sequence is not None else []:
        group = idx.resolve_ref(reference.get("ObjectRef", ""))
        if group is None or group.tag != "VideoTrackGroup":
            continue
        fps = _prproj_adobe_timebase_to_fps(int(group.findtext(".//FrameRate", "0")))
        rect = group.findtext("FrameRect", "").split(",")
        if len(rect) == 4:
            width, height = int(rect[2]), int(rect[3])
        break
    grades: list[LumetriGrade] = []
    for clip in extract_lumetri_clips(prproj_root, sequence_uid):
        plan = build_lumetri_node_plan(clip)
        warnings: list[str] = []
        records: list[LumetriParameter] = []
        for effect, node in zip(clip.effects, plan.nodes):
            warnings.extend(f"{node.label}: {note}" for note in node.approximation_notes)
            if node.unmapped_parameter_ids:
                warnings.append(f"{node.label}: unmapped ParameterIDs {', '.join(node.unmapped_parameter_ids)}")
            if node.unmapped_shader_names:
                warnings.append(f"{node.label}: unmapped stages {', '.join(node.unmapped_shader_names)}")
            for record in effect.parameters:
                candidate = record.parameter_id in {"11", "12", "20"} and record.parameter_id not in node.unmapped_parameter_ids
                records.append(LumetriParameter(
                    parameter_id=record.parameter_id, name=record.name,
                    control_type=record.control_type, raw_value=record.raw_value,
                    lower_bound=record.lower_bound, upper_bound=record.upper_bound,
                    encoding=record.encoding, is_time_varying=record.is_time_varying,
                    conversion="candidate" if candidate else "preserved",
                    reason="approximate DRX control" if candidate else "source record retained; not applied",
                    start_keyframe_position=record.start_keyframe_position,
                    start_keyframe_encoding=record.start_keyframe_encoding,
                    start_value_encoding=record.start_value_encoding,
                    binary_hash=record.binary_hash, component_ref=effect.component_ref,
                    effect_order=effect.order, raw_xml=record.raw_xml,
                ))
        grades.append(LumetriGrade(
            track_index=clip.track_index,
            start_frame=_prproj_ticks_to_frames(clip.start_ticks, fps),
            end_frame=_prproj_ticks_to_frames(clip.end_ticks, fps),
            track_item_id=clip.track_item_ref, clip_name=clip.clip_name,
            drx_payload=build_lumetri_drx(clip, width, height), node_count=len(plan.nodes),
            warnings=warnings, parameter_records=records,
        ))
    return grades


def _lumetri_conversion_summary(grades: list[LumetriGrade]) -> dict[str, int]:
    """Count the explicit conversion outcome of extracted Lumetri controls."""
    counts = {"candidate": 0, "identity": 0, "preserved": 0, "unsupported": 0, "invalid": 0}
    for grade in grades:
        for record in grade.parameter_records:
            outcome = record.conversion
            if outcome == "candidate":
                counts["candidate"] += 1
            elif outcome in counts:
                counts[outcome] += 1
            else:
                counts["preserved"] += 1
    return counts


def _prproj_find_media_for_clip(
    clip_el: ET.Element,
    idx: _PrprojIndex,
) -> tuple[str, str, ET.Element | None]:
    """Resolve a clip's media through its project object graph.

    VideoClip/AudioClip → Clip/Source → *MediaSource/Media* is the stable
    identity chain.  It remains valid when a file has been renamed or moved.
    """
    inner = clip_el.find("Clip")
    source_ref = inner.find("Source") if inner is not None else None
    source = idx.resolve_ref(source_ref.get("ObjectRef", "")) if source_ref is not None else None
    media_ref = source.find("MediaSource/Media") if source is not None else None
    if media_ref is None and source is not None:
        media_ref = source.find("Media/Media")
    media = None
    if media_ref is not None:
        media = idx.resolve_uref(media_ref.get("ObjectURef", ""))
        if media is None:
            media = idx.resolve_ref(media_ref.get("ObjectRef", ""))
    if media is None:
        return "", "", None
    path = media.findtext("FilePath") or media.findtext("ActualMediaFilePath")
    return path or media.findtext("RelativePath", ""), media.get("ObjectUID", ""), media


def _extract_clip(
    cti: ET.Element,
    idx: _PrprojIndex,
    prproj_root: ET.Element,
    default_w: int,
    default_h: int,
    fps: float,
    *,
    is_audio: bool = False,
) -> tuple[ClipData, dict[str, Any]]:
    """Extract ClipData from a .prproj ClipTrackItem chain.

    All external dependencies are explicit parameters (no closure capture).
    This function is pure data extraction — zero ET output created.
    """
    subclip = cti.find("SubClip")
    mc_name = "(unknown)"
    media_path = ""
    media_id = ""
    in_point = 0
    out_point = 0
    in_ticks = ""
    out_ticks = ""
    playback_speed = 100.0
    source_tc = _SourceTCInfo()
    src_w, src_h = default_w, default_h
    scale_val = 100.0
    rotation_val = 0.0
    label_name = ""
    label_raw_color = ""
    motion: dict[str, Any] = {"center": [0.0, 0.0], "centerOffset": [0.0, 0.0], "keyframes": {}}
    source_unsupported: list[dict[str, Any]] = []

    # Timeline position
    ti_inner = cti.find("TrackItem")
    if ti_inner is not None:
        s = ti_inner.findtext("Start")
        e = ti_inner.findtext("End")
        start = _prproj_ticks_to_frames(s, fps) if s else 0
        end = _prproj_ticks_to_frames(e, fps) if e else 0
    else:
        start, end = 0, 0

    if subclip is not None:
        sc_ref = subclip.get("ObjectRef")
        sc_el = idx.resolve_ref(sc_ref) if sc_ref else None
        if sc_el is not None:
            mc_name = sc_el.findtext("Name", mc_name)
            mc_uref_el = sc_el.find("MasterClip")
            if mc_uref_el is not None:
                mc_uref = mc_uref_el.get("ObjectURef")
                mc_el = idx.resolve_uref(mc_uref) if mc_uref else None
                if mc_el is not None:
                    source_tc = _prproj_extract_source_tc_info(mc_el, idx)
            clip_ref_el = sc_el.find("Clip")
            if clip_ref_el is not None:
                clip_ref = clip_ref_el.get("ObjectRef")
                clip_el = idx.resolve_ref(clip_ref) if clip_ref is not None else None
                if clip_el is not None:
                    label_name = clip_el.findtext(".//asl.clip.label.name", "")
                    label_raw_color = clip_el.findtext(".//asl.clip.label.color", "")
                    graph_path, graph_id, graph_media = _prproj_find_media_for_clip(clip_el, idx)
                    if graph_path:
                        media_path = graph_path
                    media_id = graph_id
                    pure_audio = (
                        is_audio
                        and graph_media is not None
                        and graph_media.find("VideoStream") is None
                    )
                    if pure_audio and source_tc.media_fps_resolved:
                        # Only audio-only media uses MediaFrameRate as a sample
                        # clock; linked audio keeps the video's source timing.
                        source_tc = _prproj_extract_source_tc_info(mc_el, idx, timing_fps=fps)
                    if graph_media is not None:
                        vs_ref = graph_media.find("VideoStream")
                        vs = idx.resolve_ref(vs_ref.get("ObjectRef", "")) if vs_ref is not None else None
                        rect = vs.findtext("FrameRect", "") if vs is not None else ""
                        parts = rect.split(",")
                        if len(parts) == 4:
                            try:
                                src_w, src_h = int(parts[2]), int(parts[3])
                            except ValueError:
                                pass
                    inner = clip_el.find("Clip")
                    if inner is not None:
                        ip = inner.findtext("InPoint")
                        op = inner.findtext("OutPoint")
                        if ip is not None:
                            in_ticks = ip
                            clip_fps = _effective_source_fps(source_tc, fps)
                            in_point = _prproj_ticks_to_frames(ip, clip_fps)
                        if op is not None:
                            out_ticks = op
                            clip_fps = _effective_source_fps(source_tc, fps)
                            out_point = _prproj_ticks_to_frames(op, clip_fps)
                        if not is_audio and _has_resolved_source_duration(source_tc):
                            in_point = min(in_point, source_tc.full_duration_frames)
                            out_point = min(out_point, source_tc.full_duration_frames)
                    ps = inner.findtext("PlaybackSpeed") if inner is not None else None
                    if ps is None:
                        ps = clip_el.findtext("PlaybackSpeed")
                    if ps:
                        try:
                            raw_speed = float(ps)
                            playback_speed = raw_speed * 100 if abs(raw_speed) <= 10 else raw_speed
                        except ValueError:
                            pass

    def _motion_value(raw: str) -> float | list[float] | None:
        parts = raw.strip().split(":")
        try:
            values = [float(part) for part in parts]
        except ValueError:
            return None
        return values[0] if len(values) == 1 else values[:2]

    def _map_motion_point(target: str, value: float | list[float]) -> float | list[float]:
        if target not in ("center", "centerOffset") or not isinstance(value, list) or len(value) < 2:
            return value
        return [value[0] - 0.5, value[1] - 0.5]

    def _parameter_record(param: ET.Element) -> dict[str, str | bool]:
        return {
            "parameter_id": param.findtext("ParameterID", ""),
            "name": param.findtext("Name", ""),
            "start_keyframe": param.findtext("StartKeyframe", ""),
            "keyframes": param.findtext("Keyframes", ""),
            "is_time_varying": param.findtext("IsTimeVarying", "false").lower() == "true",
            "xml": ET.tostring(param, encoding="unicode"),
        }

    def _start_value(param: ET.Element) -> str:
        parts = (param.findtext("StartKeyframe", "") or "").split(",")
        return parts[1].strip() if len(parts) > 1 else ""

    def _matches_default(raw: str, default: str) -> bool:
        if default in ("true", "false"):
            return raw.strip().lower() == default
        try:
            return math.isclose(float(raw), float(default), rel_tol=0.0, abs_tol=1e-9)
        except ValueError:
            return False

    # Premiere stores Motion names in the project UI language; ParameterID is stable.
    if not is_audio:
        co = cti.find("ComponentOwner")
        comps = co.find("Components") if co is not None else None
        chain = idx.resolve_ref(comps.get("ObjectRef", "")) if comps is not None else None
        chain_comps = chain.find("Components") if chain is not None else None
        if chain_comps is None and chain is not None:
            chain_comps = chain.find("ComponentChain/Components")
        if chain_comps is not None:
            for component_ref in chain_comps.findall("Component"):
                component = idx.resolve_ref(component_ref.get("ObjectRef", ""))
                if component is None:
                    continue
                match_name = component.findtext("MatchName", "")
                normalized_match = match_name.lower()
                params_section = component.find(".//Params")
                params = [
                    resolved for ref in (params_section.findall("Param") if params_section is not None else [])
                    if (resolved := idx.resolve_ref(ref.get("ObjectRef", ""))) is not None
                ]
                if "adbe motion" in normalized_match:
                    unrepresented: list[dict[str, str | bool]] = []
                    motion_defaults = {"3": "100", "4": "true", "7": "0", "8": "0", "9": "0", "10": "0", "11": "0"}
                    for p_el in params:
                        parameter_id = p_el.findtext("ParameterID", "")
                        target = {"1": "center", "2": "scale", "5": "rotation", "6": "centerOffset"}.get(parameter_id)
                        raw_keyframes = p_el.findtext("Keyframes", "") or ""
                        is_time_varying = p_el.findtext("IsTimeVarying", "false").lower() == "true"
                        if target is None:
                            default = motion_defaults.get(parameter_id)
                            start_value = _start_value(p_el)
                            if (raw_keyframes or is_time_varying or
                                    (default is not None and start_value and not _matches_default(start_value, default))):
                                unrepresented.append(_parameter_record(p_el))
                            continue
                        value = _motion_value(_start_value(p_el))
                        if value is not None:
                            if target == "scale" and isinstance(value, float):
                                scale_val = value
                            elif target == "rotation" and isinstance(value, float):
                                rotation_val = value
                            else:
                                motion[target] = _map_motion_point(target, value)
                        points: list[dict[str, Any]] = []
                        for raw_point in raw_keyframes.split(";"):
                            fields = raw_point.split(",")
                            if len(fields) < 2:
                                continue
                            try:
                                ticks = int(fields[0])
                            except ValueError:
                                continue
                            point_value = _motion_value(fields[1])
                            if point_value is None:
                                continue
                            points.append({
                                "when": _prproj_ticks_to_frames(str(ticks), fps),
                                "value": _map_motion_point(target, point_value),
                            })
                        if points:
                            motion["keyframes"][target] = points
                            motion.setdefault("source_keyframes", {})[target] = {
                                "parameter_id": parameter_id,
                                "start_keyframe": p_el.findtext("StartKeyframe", ""),
                                "keyframes": raw_keyframes,
                            }
                    if unrepresented:
                        source_unsupported.append({
                            "kind": "non-default-motion-properties",
                            "match_name": match_name,
                            "component_id": component.get("ObjectID", ""),
                            "component_class_id": component.get("ClassID", ""),
                            "parameters": unrepresented,
                        })
                    continue
                if "lumetri" in normalized_match:
                    continue
                if "adbe opacity" in normalized_match:
                    opacity_param = next((p for p in params if p.findtext("ParameterID", "") == "1"), None)
                    opacity_value = _start_value(opacity_param) if opacity_param is not None else ""
                    if opacity_value:
                        try:
                            motion["opacity"] = float(opacity_value)
                        except ValueError:
                            pass
                    opacity_defaults = {"1": "100", "2": "18", "3": "0"}
                    nondefault = any(
                        p.findtext("Keyframes", "")
                        or p.findtext("IsTimeVarying", "false").lower() == "true"
                        or (p.findtext("ParameterID", "") in opacity_defaults
                            and _start_value(p)
                            and not _matches_default(_start_value(p), opacity_defaults[p.findtext("ParameterID", "")]))
                        for p in params
                    )
                    if nondefault:
                        animated = any(
                            p.findtext("Keyframes", "")
                            or p.findtext("IsTimeVarying", "false").lower() == "true"
                            for p in params
                        )
                        extra_fields_changed = any(
                            p.findtext("ParameterID", "") in {"2", "3"}
                            and _start_value(p)
                            and not _matches_default(
                                _start_value(p), opacity_defaults[p.findtext("ParameterID", "")]
                            )
                            for p in params
                        )
                        source_unsupported.append({
                            "kind": "video-effect",
                            "match_name": match_name,
                            "component_id": component.get("ObjectID", ""),
                            "component_class_id": component.get("ClassID", ""),
                            "representation": (
                                "FCP7 initial opacity value only" if animated
                                else "FCP7 opacity value; additional fields unsupported" if extra_fields_changed
                                else "FCP7 static opacity value"
                            ),
                            "parameters": [_parameter_record(p) for p in params],
                        })
                    continue
                source_unsupported.append({
                    "kind": "unconverted-video-component",
                    "match_name": match_name,
                    "component_id": component.get("ObjectID", ""),
                    "component_class_id": component.get("ClassID", ""),
                    "parameters": [_parameter_record(p) for p in params],
                })

    if source_unsupported:
        motion["source_unsupported"] = source_unsupported

    return ClipData(
        name=mc_name, media_path=media_path, media_id=media_id,
        start=start, end=end, in_pt=in_point, out_pt=out_point,
        playback_speed=playback_speed, source_tc=source_tc,
        source_w=src_w, source_h=src_h, scale=scale_val, rotation=rotation_val,
        label_name=label_name, label_raw_color=label_raw_color,
        in_ticks=in_ticks, out_ticks=out_ticks,
    ), motion


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
    global _scaletoframe_enabled

    idx = _PrprojIndex.build(prproj_root)

    # Read Premiere "Scale to Frame Size" project toggle
    ssm = prproj_root.findtext("ProjectSettings/ShouldScaleMedia", "true")
    _scaletoframe_enabled = (ssm != "false")

    # ─── Resolve sequence metadata ───────────────────────────────────
    seq_el = None
    for s in prproj_root.findall("Sequence"):
        if s.get("ObjectUID") == sequence_uid:
            seq_el = s
            break
    if seq_el is None:
        raise ValueError(f"Sequence not found: {sequence_uid}")
    seq_name = seq_el.findtext("Name", "(unnamed)")

    w, h = 1920, 1080
    props = seq_el.find(".//Properties")
    if props is not None:
        for p in props:
            if "PreviewFrameSizeWidth" in p.tag:
                w = int(p.text or "1920")
            if "PreviewFrameSizeHeight" in p.tag:
                h = int(p.text or "1080")

    fps = DEFAULT_FPS
    video_tg = None
    audio_tg = None
    tg_section = seq_el.find("TrackGroups")
    if tg_section is not None:
        for tg_pair in tg_section.findall("TrackGroup"):
            second = tg_pair.find("Second")
            if second is not None:
                ref = second.get("ObjectRef")
                tg_el = idx.resolve_ref(ref) if ref else None
                if tg_el is not None:
                    if tg_el.tag == "VideoTrackGroup":
                        video_tg = tg_el
                        # PreviewFrameSize describes the monitor/render preview,
                        # while FrameRect is the actual sequence raster used by
                        # Premiere's VideoTrackGroup.
                        frame_rect = tg_el.findtext("FrameRect", "")
                        rect_parts = frame_rect.split(",")
                        if len(rect_parts) == 4:
                            try:
                                rect_w, rect_h = int(rect_parts[2]), int(rect_parts[3])
                                if rect_w > 0 and rect_h > 0:
                                    w, h = rect_w, rect_h
                            except ValueError:
                                pass
                        fr = tg_el.find(".//FrameRate")
                        if fr is not None and fr.text:
                            try:
                                fps = _prproj_adobe_timebase_to_fps(int(fr.text))
                            except ValueError:
                                pass
                    elif tg_el.tag == "AudioTrackGroup":
                        audio_tg = tg_el

    is_ntsc = _is_ntsc_fps(fps)
    timebase = int(round(fps))
    timeline_state = _prproj_extract_timeline_state(seq_el, idx, fps)
    for clip in extract_lumetri_clips(prproj_root, sequence_uid):
        timeline_state.unknown.append((
            f"lumetri-source:{clip.track_item_ref}",
            json.dumps(asdict(clip), ensure_ascii=False, sort_keys=True),
        ))
    for caption in extract_captions(prproj_root, sequence_uid):
        timeline_state.unknown.append((
            f"caption-source:{caption.item_object_id}",
            json.dumps(asdict(caption), ensure_ascii=False, sort_keys=True),
        ))
    if tg_section is not None:
        for group_index, group_ref in enumerate(tg_section.findall("TrackGroup"), 1):
            second = group_ref.find("Second")
            group = idx.resolve_ref(second.get("ObjectRef", "")) if second is not None else None
            if group is None or not ("Data" in group.tag or "Caption" in group.tag):
                continue
            tracks = group.find(".//Tracks")
            for track_index, track_ref in enumerate(tracks.findall("Track") if tracks is not None else [], 1):
                track_uid = track_ref.get("ObjectURef", "")
                track = idx.resolve_uref(track_uid) if track_uid else None
                items_section = track.find(".//TrackItems") if track is not None else None
                item_refs = items_section.findall("TrackItem") if items_section is not None else []
                raw_items = []
                for item_ref in item_refs:
                    item = idx.resolve_ref(item_ref.get("ObjectRef", ""))
                    if item is not None:
                        raw_items.append({
                            "object_id": item.get("ObjectID", ""),
                            "type": item.tag,
                            "xml": ET.tostring(item, encoding="unicode"),
                        })
                if raw_items:
                    timeline_state.unknown.append((
                        f"unconverted-source:track-data:{sequence_uid}:{group_index}:{track_index}",
                        json.dumps({
                            "kind": "unconverted-data-track",
                            "track_group": group.tag,
                            "track_type": track.tag if track is not None else "",
                            "track_uid": track_uid,
                            "items": raw_items,
                        }, ensure_ascii=False, sort_keys=True),
                    ))

    def _render_sequence_skeleton(seq_name: str, timebase: int, is_ntsc: bool) -> tuple[ET.Element, ET.Element, ET.Element, ET.Element]:
        """Build DC-format sequence skeleton. Returns (xmeml, dur_elem, video_et, audio_et)."""
        xmeml = ET.Element("xmeml")
        xmeml.set("version", FCP7_VERSION)
        sequence = ET.SubElement(xmeml, "sequence")
        ET.SubElement(sequence, "name").text = seq_name
        dur_elem = ET.SubElement(sequence, "duration")
        rate_elem = ET.SubElement(sequence, "rate")
        ET.SubElement(rate_elem, "timebase").text = str(timebase)
        ET.SubElement(rate_elem, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        ET.SubElement(sequence, "in").text = "-1"
        ET.SubElement(sequence, "out").text = "-1"
        tc = ET.SubElement(sequence, "timecode")
        ET.SubElement(tc, "string").text = "00;00;00;00" if is_ntsc else "00:00:00:00"
        ET.SubElement(tc, "frame").text = "0"
        ET.SubElement(tc, "displayformat").text = "DF" if is_ntsc else "NDF"
        tc_rate = ET.SubElement(tc, "rate")
        ET.SubElement(tc_rate, "timebase").text = str(timebase)
        ET.SubElement(tc_rate, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        media = ET.SubElement(sequence, "media")
        video_section = ET.SubElement(media, "video")
        video_format = ET.SubElement(video_section, "format")
        sample_chars = ET.SubElement(video_format, "samplecharacteristics")
        sample_rate = ET.SubElement(sample_chars, "rate")
        ET.SubElement(sample_rate, "timebase").text = str(timebase)
        ET.SubElement(sample_rate, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        ET.SubElement(sample_chars, "width").text = str(w)
        ET.SubElement(sample_chars, "height").text = str(h)
        ET.SubElement(sample_chars, "pixelaspectratio").text = "square"
        audio_section = ET.SubElement(media, "audio")
        return xmeml, dur_elem, video_section, audio_section

    total_frames = 0
    xmeml, dur_elem, video_section, audio_section = _render_sequence_skeleton(seq_name, timebase, is_ntsc)
    fcp_sequence = xmeml.find("sequence")
    if fcp_sequence is not None:
        fcp_sequence.set("id", sequence_uid)
        fcp_sequence.find("in").text = str(timeline_state.in_frame if timeline_state.in_frame is not None else -1)
        fcp_sequence.find("out").text = str(timeline_state.out_frame if timeline_state.out_frame is not None else -1)

    # ─── Shared state ────────────────────────────────────────────────
    _id_counter = [0]  # single global counter — DC uses unique IDs per-name
    _file_ids: dict[str, FileData] = {}  # media_name → FileData
    _clipitem_by_track_item: dict[str, LinkMember] = {}

    def _next_ci_id(name: str) -> str:
        cid = f"{name} {_id_counter[0]}"
        _id_counter[0] += 1
        return cid

    def _next_fi_id(name: str) -> str:
        fid = f"{name} {_id_counter[0]}"
        _id_counter[0] += 1
        return fid

    # ─── Helper: render a <file> element from FileData ─────────────
    def _render_file(fd: FileData) -> ET.Element:
        tc_info = fd.timecode
        if tc_info is None:
            tc_info = _SourceTCInfo()
        src_tb = int(round(tc_info.media_fps))
        src_ntsc = tc_info.is_ntsc
        fe = ET.Element("file")
        fe.set("id", fd.id)
        ET.SubElement(fe, "duration").text = str(fd.duration)
        fr = ET.SubElement(fe, "rate")
        ET.SubElement(fr, "timebase").text = str(src_tb)
        ET.SubElement(fr, "ntsc").text = "TRUE" if src_ntsc else "FALSE"
        ET.SubElement(fe, "name").text = fd.name
        ET.SubElement(fe, "pathurl").text = _to_fcp7_pathurl(fd.path) if fd.path else f"file://localhost/{fd.name}"
        ftc = ET.SubElement(fe, "timecode")
        ET.SubElement(ftc, "string").text = tc_info.timecode_string
        ET.SubElement(ftc, "displayformat").text = "DF" if src_ntsc else "NDF"
        ftcr = ET.SubElement(ftc, "rate")
        ET.SubElement(ftcr, "timebase").text = str(src_tb)
        ET.SubElement(ftcr, "ntsc").text = "TRUE" if src_ntsc else "FALSE"
        fmedia = ET.SubElement(fe, "media")
        if not fd.for_audio_only:
            fv = ET.SubElement(fmedia, "video")
            ET.SubElement(fv, "duration").text = str(fd.duration)
            fvsc = ET.SubElement(fv, "samplecharacteristics")
            ET.SubElement(fvsc, "width").text = str(fd.source_w)
            ET.SubElement(fvsc, "height").text = str(fd.source_h)
        fa = ET.SubElement(fmedia, "audio")
        ET.SubElement(fa, "channelcount").text = "2"
        if fd.for_audio_only and tc_info.sample_rate > 0:
            f_audio_sc = ET.SubElement(fa, "samplecharacteristics")
            ET.SubElement(f_audio_sc, "samplerate").text = str(tc_info.sample_rate)
        return fe

    # ─── Helper: build DC-format filters ────────────────────────────
    def _render_filters(ci: ET.Element, specs: list[FilterSpec], motion_data: dict[str, Any] | None = None) -> None:
        """Render a list of FilterSpec objects into <filter> ET elements."""
        for spec in specs:
            filt = ET.SubElement(ci, "filter")
            for tag, val in [("enabled", "TRUE"), ("start", spec.start), ("end", spec.end)]:
                ET.SubElement(filt, tag).text = val
            eff = ET.SubElement(filt, "effect")
            for tag, val in [("name", spec.name), ("effectid", spec.effect_id),
                             ("effecttype", spec.effect_type), ("mediatype", spec.media_type),
                             ("effectcategory", spec.effect_category)]:
                ET.SubElement(eff, tag).text = val
            for p in spec.params:
                param = ET.SubElement(eff, "parameter")
                ET.SubElement(param, "name").text = p.name
                ET.SubElement(param, "parameterid").text = p.parameterid
                points = (motion_data or {}).get("keyframes", {}).get(p.parameterid, [])
                if not points:
                    if p.is_composite:
                        cv = ET.SubElement(param, "value")
                        composite = (motion_data or {}).get(p.parameterid, [0.0, 0.0])
                        ET.SubElement(cv, "horiz").text = str(composite[0])
                        ET.SubElement(cv, "vert").text = str(composite[1])
                    else:
                        ET.SubElement(param, "value").text = p.value
                if p.valuemin:
                    ET.SubElement(param, "valuemin").text = p.valuemin
                    ET.SubElement(param, "valuemax").text = p.valuemax
                for point in points:
                    keyframe = ET.SubElement(param, "keyframe")
                    ET.SubElement(keyframe, "when").text = str(point["when"])
                    if p.is_composite:
                        value = ET.SubElement(keyframe, "value")
                        point_value = point["value"]
                        ET.SubElement(value, "horiz").text = str(point_value[0] if isinstance(point_value, list) else point_value)
                        ET.SubElement(value, "vert").text = str(point_value[1] if isinstance(point_value, list) else point_value)
                    else:
                        ET.SubElement(keyframe, "value").text = str(point["value"])
                    interpolation = ET.SubElement(keyframe, "interpolation")
                    ET.SubElement(interpolation, "name").text = "linear"

    def _build_video_filters(dur: int, scale: float, rotation: float, speed: float, opacity: float) -> list[FilterSpec]:
        """Build video filter specs from clip data. Pure data, no ET."""
        dur_s = str(dur)
        return [
            FilterSpec(effect_id="basic", name="Basic Motion", effect_type="motion",
                       media_type="video", effect_category="motion", start="0", end=dur_s,
                       params=[
                           FilterParam("Scale", "scale", str(scale), "1", "10000"),
                           FilterParam("Center", "center", "", "", "", is_composite=True),
                           FilterParam("Rotation", "rotation", str(rotation), "-100000", "100000"),
                           FilterParam("Anchor Point", "centerOffset", "", "", "", is_composite=True),
                       ]),
            FilterSpec(effect_id="crop", name="Crop", effect_type="motion",
                       media_type="video", effect_category="motion", start="0", end=dur_s,
                       params=[FilterParam(p, p, "0", "0", "100") for p in ("left", "right", "top", "bottom")]),
            FilterSpec(effect_id="opacity", name="Opacity", effect_type="motion",
                       media_type="video", effect_category="motion", start="0", end=dur_s,
                       params=[FilterParam("opacity", "opacity", str(opacity), "0", "100")]),
            FilterSpec(effect_id="timeremap", name="Time Remap", effect_type="motion",
                       media_type="video", effect_category="motion", start="-1", end="-1",
                       params=[
                           FilterParam("speed", "speed", str(speed), "-10000", "10000"),
                           FilterParam("reverse", "reverse", "FALSE"),
                           FilterParam("frameblending", "frameblending", "FALSE"),
                           FilterParam("variablespeed", "variablespeed", "0", "0", "1"),
                       ]),
        ]

    def _build_audio_filters(dur: int, speed: float) -> list[FilterSpec]:
        """Build audio filter specs. Pure data, no ET."""
        dur_s = str(dur)
        specs = []
        if speed != 100:
            specs.append(FilterSpec(effect_id="timeremap", name="Time Remap", effect_type="motion",
                                    media_type="audio", effect_category="motion", start="-1", end="-1",
                                    params=[
                                        FilterParam("speed", "speed", str(speed), "-10000", "10000"),
                                        FilterParam("reverse", "reverse", "FALSE"),
                                        FilterParam("frameblending", "frameblending", "FALSE"),
                                        FilterParam("variablespeed", "variablespeed", "0", "0", "1"),
                                    ]))
        specs.append(FilterSpec(effect_id="audiolevels", name="Audio Levels", effect_type="audiolevels",
                                media_type="audio", effect_category="audiolevels", start="0", end=dur_s,
                                params=[FilterParam("Level", "level", "1", "0", "3.98109")]))
        specs.append(FilterSpec(effect_id="audiopan", name="Audio Pan", effect_type="audiopan",
                                media_type="audio", effect_category="audiopan", start="0", end=dur_s,
                                params=[FilterParam("Pan", "pan", "0", "-1", "1")]))
        return specs

    def _file_duration(cl: ClipData, timeline_duration: int) -> int:
        """Return a source-rate file duration covering the trimmed clip."""
        source_fps = _effective_source_fps(cl.source_tc, fps)
        source_span = int(round(timeline_duration * source_fps / fps)) if fps > 0 else 0
        return max(cl.in_pt, cl.out_pt, source_span, 0)

    # ─── Helper: build DC-format transition ─────────────────────────
    def _render_transition(td: TransitionData) -> ET.Element:
        """Build a DC-format transition item from TransitionData."""
        ti = ET.Element("transitionitem")
        tr = ET.SubElement(ti, "rate")
        ET.SubElement(tr, "timebase").text = str(timebase)
        ET.SubElement(tr, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        ET.SubElement(ti, "start").text = str(td.start_frame)
        ET.SubElement(ti, "end").text = str(td.end_frame)
        ET.SubElement(ti, "alignment").text = td.alignment
        eff = ET.SubElement(ti, "effect")
        for tag, val in [
            ("name", td.effect_id), ("effectid", td.effect_id),
            ("effecttype", "transition"), ("mediatype", "video"),
            ("effectcategory", "Dissolve"),
        ]:
            ET.SubElement(eff, tag).text = val
        for tag, val in [("startratio", "0"), ("endratio", "1"), ("reverse", "FALSE")]:
            ET.SubElement(eff, tag).text = val
        return ti

    # ═══════════════════════════════════════════════════════════════════
    # ─── Helper: render a complete clipitem from data ──────────────
    def _render_clipitem(
        cl: ClipData, fd: FileData, clip_dur: int, start_frame: int,
        ci_id: str, mediatype: str,
        *,
        track_index: int = 0,
        is_shared_file: bool = False,
        motion_data: dict[str, Any] | None = None,
    ) -> ET.Element:
        """Build a DC-format <clipitem> from data. No side effects."""
        ci = ET.Element("clipitem")
        ci.set("id", ci_id)
        ET.SubElement(ci, "name").text = cl.name
        ET.SubElement(ci, "duration").text = str(clip_dur)
        cir = ET.SubElement(ci, "rate")
        ET.SubElement(cir, "timebase").text = str(timebase)
        ET.SubElement(cir, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        ET.SubElement(ci, "start").text = str(start_frame)
        ET.SubElement(ci, "end").text = str(cl.end)
        ET.SubElement(ci, "enabled").text = "TRUE"
        # FCP7 in/out use clipitem.rate, which is the sequence rate here.
        # Convert directly from ticks to avoid rounding through source frames.
        source_rate = _effective_source_fps(cl.source_tc, fps)
        trim_in = (_prproj_ticks_to_frames(cl.in_ticks, fps) if cl.in_ticks
                   else int(round(cl.in_pt * fps / source_rate)))
        trim_out = (_prproj_ticks_to_frames(cl.out_ticks, fps) if cl.out_ticks
                    else int(round(cl.out_pt * fps / source_rate)))
        if mediatype == "video" and _has_resolved_source_duration(cl.source_tc):
            source_end = int(round(cl.source_tc.full_duration_frames * fps / source_rate))
            trim_in, trim_out = min(trim_in, source_end), min(trim_out, source_end)
        ET.SubElement(ci, "in").text = str(trim_in)
        ET.SubElement(ci, "out").text = str(trim_out)
        if cl.in_ticks or cl.out_ticks:
            trim_record = {
                "source_in_ticks": cl.in_ticks, "source_out_ticks": cl.out_ticks,
                "clip_rate": fps, "in": trim_in, "out": trim_out,
            }
            for label, ticks, frame in (("in", cl.in_ticks, trim_in), ("out", cl.out_ticks, trim_out)):
                try:
                    trim_record[f"{label}_error_seconds"] = frame / fps - int(ticks) / 254016000000
                except ValueError:
                    trim_record[f"{label}_error_seconds"] = None
            timeline_state.unknown.append((f"clip-trim:{ci_id}", json.dumps(trim_record, sort_keys=True)))
        if is_shared_file:
            # Audio: self-closing ref to existing file
            fe = ET.SubElement(ci, "file")
            fe.set("id", fd.id)
        else:
            ci.append(_render_file(fd))
        if mediatype == "video":
            ET.SubElement(ci, "compositemode").text = "normal"
            opacity = float((motion_data or {}).get("opacity", 100.0))
            _render_filters(ci, _build_video_filters(clip_dur, cl.scale, cl.rotation, cl.playback_speed, opacity), motion_data)
            source_keyframes = (motion_data or {}).get("source_keyframes", {})
            if source_keyframes:
                timeline_state.unknown.append((f"clip-motion:{ci_id}", json.dumps({
                    "name": cl.name, "start": start_frame, "end": start_frame + clip_dur,
                    "parameter_keyframes": source_keyframes,
                    "interpolation_note": "FCP7 keyframes are emitted as linear; original Premiere records are retained here.",
                }, ensure_ascii=False, sort_keys=True)))
            for index, record in enumerate((motion_data or {}).get("source_unsupported", []), 1):
                timeline_state.unknown.append((f"unconverted-source:{ci_id}:{index}", json.dumps({
                    "clip_id": ci_id,
                    "clip_name": cl.name,
                    "start": start_frame,
                    "end": start_frame + clip_dur,
                    **record,
                }, ensure_ascii=False, sort_keys=True)))
        else:
            st_el = ET.SubElement(ci, "sourcetrack")
            ET.SubElement(st_el, "mediatype").text = "audio"
            ET.SubElement(st_el, "trackindex").text = "1"
            _render_filters(ci, _build_audio_filters(clip_dur, cl.playback_speed))
        ET.SubElement(ci, "comments")
        if cl.label_name or cl.label_raw_color:
            timeline_state.unknown.append((f"clip-label:{ci_id}", json.dumps({
                "name": cl.name, "start": start_frame, "media_type": mediatype,
                "end": start_frame + clip_dur, "track_index": track_index, "preset": cl.label_name,
                "raw_color": cl.label_raw_color,
            }, ensure_ascii=False, sort_keys=True)))
        return ci

    # PASS 1: Video tracks
    # ═══════════════════════════════════════════════════════════════════
    if video_tg is not None:
        tracks_el = video_tg.find("TrackGroup/Tracks")
        if tracks_el is not None:
            for vt_idx, track_ref in enumerate(tracks_el.findall("Track"), 1):
                uref = track_ref.get("ObjectURef")
                track_el = idx.resolve_uref(uref) if uref else None
                if track_el is None:
                    continue
                ct = track_el.find("ClipTrack")
                if ct is None:
                    continue
                ti_section = ct.find(".//TrackItems")
                track_items = ti_section.findall("TrackItem") if ti_section is not None else []
                # Preserve source topology so state indexes and later API writes stay aligned.
                fcp_track_index = vt_idx
                track_node = track_el.find("ClipTrack/Track")
                track_props = track_node.find("Node/Properties") if track_node is not None else None
                track_data = TrackData(
                    type="video", index=fcp_track_index,
                    enabled=(track_node is None or track_node.findtext("IsMuted", "false").lower() != "true"),
                    locked=(track_node is not None and track_node.findtext("IsLocked", "false").lower() == "true"),
                    solo=(track_node is not None and track_node.findtext("Solo", "0") not in ("", "0", "false")),
                    name=(track_props.findtext("MZ.TrackName", "") if track_props is not None else ""),
                )
                fcp_track = ET.SubElement(video_section, "track")
                if track_data.name:
                    ET.SubElement(fcp_track, "name").text = track_data.name
                for vc_idx, ti_ref in enumerate(track_items, 1):
                    ti_el = idx.resolve_ref(ti_ref.get("ObjectRef", ""))
                    if ti_el is None:
                        continue
                    cti = ti_el.find("ClipTrackItem")
                    if cti is None:
                        continue
                    cl, motion_data = _extract_clip(cti, idx, prproj_root, w, h, fps)
                    start = cl.start
                    total_frames = max(total_frames, cl.end)
                    # Timeline range and rendered clip trim share the sequence rate.
                    clip_dur = max(cl.end - start, 0)
                    file_dur_val = (cl.source_tc.full_duration_frames
                                    if _has_resolved_source_duration(cl.source_tc)
                                    else _file_duration(cl, clip_dur))
                    fd = FileData(
                        id=_next_fi_id(cl.name), name=cl.name, path=cl.media_path,
                        duration=file_dur_val, timecode=cl.source_tc,
                        source_w=cl.source_w, source_h=cl.source_h,
                    )
                    file_key = cl.media_id or cl.media_path or cl.name
                    _file_ids.setdefault(file_key, fd)
                    ci_id = _next_ci_id(cl.name)
                    if ti_el.get("ObjectID"):
                        _clipitem_by_track_item[ti_el.get("ObjectID", "")] = LinkMember(
                            clipitem_id=ci_id, mediatype="video", track_index=vt_idx, clip_index=vc_idx)
                    ci = _render_clipitem(cl, fd, clip_dur, start, ci_id, "video", track_index=fcp_track_index, motion_data=motion_data)
                    fcp_track.append(ci)

                ET.SubElement(fcp_track, "enabled").text = "TRUE" if track_data.enabled else "FALSE"
                ET.SubElement(fcp_track, "locked").text = "TRUE" if track_data.locked else "FALSE"

    # ═══════════════════════════════════════════════════════════════════
    # PASS 2: Audio tracks
    # ═══════════════════════════════════════════════════════════════════
    if audio_tg is not None:
        a_tracks_el = audio_tg.find("TrackGroup/Tracks")
        if a_tracks_el is not None:
            for at_idx, a_track_ref in enumerate(a_tracks_el.findall("Track"), 1):
                a_track_el = idx.resolve_uref(a_track_ref.get("ObjectURef", ""))
                if a_track_el is None:
                    continue
                a_ct = a_track_el.find("ClipTrack")
                if a_ct is None:
                    continue
                a_ti_section = a_ct.find(".//TrackItems")
                a_track_items = a_ti_section.findall("TrackItem") if a_ti_section is not None else []
                fcp_track_index = at_idx
                track_node = a_track_el.find("ClipTrack/Track")
                track_props = track_node.find("Node/Properties") if track_node is not None else None
                a_track_data = TrackData(
                    type="audio", index=fcp_track_index,
                    muted=(track_node is not None and track_node.findtext("IsMuted", "false").lower() == "true"),
                    locked=(track_node is not None and track_node.findtext("IsLocked", "false").lower() == "true"),
                    solo=(track_node is not None and track_node.findtext("Solo", "0") not in ("", "0", "false")),
                    name=(track_props.findtext("MZ.TrackName", "") if track_props is not None else ""),
                )
                fcp_a_track = ET.SubElement(audio_section, "track")
                if a_track_data.name:
                    ET.SubElement(fcp_a_track, "name").text = a_track_data.name
                for ac_idx, a_ti_ref in enumerate(a_track_items, 1):
                    a_ti_el = idx.resolve_ref(a_ti_ref.get("ObjectRef", ""))
                    if a_ti_el is None:
                        continue
                    a_cti = a_ti_el.find("ClipTrackItem")
                    if a_cti is None:
                        continue
                    cl, _ = _extract_clip(a_cti, idx, prproj_root, w, h, fps, is_audio=True)
                    start = cl.start
                    total_frames = max(total_frames, cl.end)
                    clip_dur = max(cl.end - start, 0)

                    ci_id = _next_ci_id(cl.name)
                    if a_ti_el.get("ObjectID"):
                        _clipitem_by_track_item[a_ti_el.get("ObjectID", "")] = LinkMember(
                            clipitem_id=ci_id, mediatype="audio", track_index=at_idx, clip_index=ac_idx)

                    # File: shared ref or full standalone
                    file_key = cl.media_id or cl.media_path or cl.name
                    existing_fd = _file_ids.get(file_key)
                    if existing_fd is not None:
                        fd = existing_fd
                        is_shared = True
                    else:
                        file_dur_val = (cl.source_tc.full_duration_frames
                                        if _has_resolved_source_duration(cl.source_tc)
                                        else _file_duration(cl, clip_dur))
                        fd = FileData(
                            id=_next_fi_id(cl.name), name=cl.name, path=cl.media_path,
                            duration=file_dur_val, timecode=cl.source_tc,
                            source_w=cl.source_w, source_h=cl.source_h, for_audio_only=True,
                        )
                        _file_ids[file_key] = fd
                        is_shared = False

                    a_ci = _render_clipitem(cl, fd, clip_dur, start, ci_id, "audio", track_index=fcp_track_index, is_shared_file=is_shared)
                    fcp_a_track.append(a_ci)

                ET.SubElement(fcp_a_track, "enabled").text = "FALSE" if a_track_data.muted else "TRUE"
                ET.SubElement(fcp_a_track, "locked").text = "TRUE" if a_track_data.locked else "FALSE"

    # ═══════════════════════════════════════════════════════════════════
    # PASS 3: Link elements (DC convention: linkclipref + mediatype for audio)
    # ═══════════════════════════════════════════════════════════════════
    def _resolve_project_links() -> list[LinkGroup]:
        """Resolve Premiere Link objects using their TrackItem references.

        Name based grouping is unsafe: two independently edited clips may
        share a filename.  The project Link object is the authoritative sync
        relationship, and only links whose track items are in this sequence
        are emitted.
        """
        result: list[LinkGroup] = []
        for link_el in prproj_root.findall("Link"):
            link_ref = link_el.get("ObjectRef")
            link = link_el
            if link.find("TrackItemGroup") is None:
                link = idx.resolve_ref(link_ref) if link_ref else None
            if link is None:
                continue
            members: list[LinkMember] = []
            tracks = link.find("TrackItemGroup/TrackItems")
            if tracks is None:
                continue
            for track_ref in tracks.findall("TrackItem"):
                ti_ref = track_ref.get("ObjectRef")
                member = _clipitem_by_track_item.get(ti_ref or "")
                if member is not None and member not in members:
                    members.append(member)
            if len(members) < 2:
                continue
            first_video = next((m.clipitem_id for m in members if m.mediatype == "video"), None)
            result.append(LinkGroup(
                media_name=f"project-link-{link_ref or len(result)}",
                members=members,
                first_video_id=first_video,
            ))
        return result

    def _render_links_for_clipitem(
        ci: ET.Element,
        group: LinkGroup,
        *,
        is_audio: bool = False,
    ) -> None:
        """Insert <link> elements into one clipitem. No shared ET objects.

        Creates every <link> fresh — no deepcopy needed.
        DC convention: audio clipitem's first link gets <mediatype>video</mediatype>.
        Video clipitem links never have mediatype.
        """
        for old in ci.findall("link"):
            ci.remove(old)
        ins_pos = len(list(ci))
        for i, child in enumerate(ci):
            if child.tag == "comments":
                ins_pos = i
                break
        for other_m in group.members:
            el = ET.Element("link")
            ET.SubElement(el, "linkclipref").text = other_m.clipitem_id
            # DC: only audio clipitems get <mediatype>video</mediatype>
            if is_audio and group.first_video_id and other_m.clipitem_id == group.first_video_id:
                mt = ET.Element("mediatype")
                mt.text = "video"
                el.insert(1, mt)
            ci.insert(ins_pos, el)
            ins_pos += 1

    for group in _resolve_project_links():
        for m in group.members:
            ci = None
            if m.mediatype == "video":
                ci = video_section.find(f".//clipitem[@id='{m.clipitem_id}']")
            else:
                ci = audio_section.find(f".//clipitem[@id='{m.clipitem_id}']")
            if ci is None:
                continue
            _render_links_for_clipitem(ci, group, is_audio=(m.mediatype == "audio"))

    # ─── Finalize ────────────────────────────────────────────────────
    dur_elem.text = str(total_frames if total_frames > 0 else 0)

    if fcp_sequence is not None:
        for marker in timeline_state.markers:
            if marker.scope != "sequence":
                continue
            marker_el = ET.SubElement(fcp_sequence, "marker")
            ET.SubElement(marker_el, "name").text = marker.name
            ET.SubElement(marker_el, "in").text = str(marker.start_frame)
            ET.SubElement(marker_el, "out").text = str(marker.end_frame)
            ET.SubElement(marker_el, "comment").text = marker.comment
        if (timeline_state.unknown or any(marker.raw_payload for marker in timeline_state.markers)
                or any((timeline_state.raw_in_ticks, timeline_state.raw_out_ticks,
                        timeline_state.raw_work_in_ticks, timeline_state.raw_work_out_ticks))):
            opaque = {
                "sequence_uid": timeline_state.sequence_uid,
                "work_area": [timeline_state.work_in_frame, timeline_state.work_out_frame],
                "source_ticks": {
                    "in": timeline_state.raw_in_ticks, "out": timeline_state.raw_out_ticks,
                    "work_in": timeline_state.raw_work_in_ticks,
                    "work_out": timeline_state.raw_work_out_ticks,
                },
                "unknown": timeline_state.unknown,
                "markers": [
                    {"scope": marker.scope, "type": marker.marker_type,
                     "color_index": marker.color_index, "raw": marker.raw_payload}
                    for marker in timeline_state.markers if marker.raw_payload
                ],
            }
            payload = base64.b64encode(json.dumps(opaque, ensure_ascii=False, sort_keys=True).encode("utf-8")).decode("ascii")
            xmeml.append(ET.Comment(f" pr2resolve-timeline-state:{payload} "))

    return xmeml

# ─── From pr2_drt_bridge.py ─────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# DRT Output — DaVinci Resolve Scripting API Bridge
# ═══════════════════════════════════════════════════════════════════════════════

# DaVinci Resolve Scripting API module paths
_RESOLVE_API_PATHS: dict[str, list[str]] = {
    "win32": [
        str(Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData"))
            / r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules"),
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
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        return "Resolve.exe" in result.stdout
    except Exception:
        return False


# ── Process lifecycle tracking ───────────────────────────────────────────
_resolve_process: Optional[subprocess.Popen[Any]] = None
"""The Popen handle for a Resolve process WE launched (None if reused)."""
_launch_mode: str = ""
"""Track how WE launched Resolve: 'headless', 'gui', or '' (not by us)."""


def _shutdown_resolve() -> None:
    """Terminate a Resolve process that we launched.

    - GUI mode: leaves Resolve running (user should see project)
    - Headless mode: kills the process
    - Pre-existing (not launched by us): no-op
    """
    global _resolve_process, _launch_mode
    if _launch_mode == "gui":
        print("  (DaVinci GUI left open — project ready for you)")
        _resolve_process = None
        _launch_mode = ""
        return
    if _launch_mode == "headless" and _resolve_process is not None:
        try:
            pid = _resolve_process.pid
            print(f"  Shutting down DaVinci Resolve (PID {pid})...")
            _resolve_process.terminate()
            try:
                _resolve_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _resolve_process.kill()
                _resolve_process.wait(timeout=5)
            print(f"  DaVinci Resolve stopped.")
        except Exception as e:
            print(f"  Note: DaVinci process cleanup failed ({e}) — may need manual kill.")
        finally:
            _resolve_process = None
    _launch_mode = ""


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
    if install_dir and "RESOLVE_SCRIPT_LIB" not in os.environ:
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


# --- Feature: Batch DRT Export ---

def _drt_batch_export(
    resolve: Any,
    xml_paths: list[Path],
    output_dir: Path,
    sequence_names: list[str],
    lumetri_grades: Optional[list[list[LumetriGrade]]] = None,
) -> list[tuple[bool, Optional[Path]]]:
    """Export multiple sequences as DRT files in one DaVinci session.

    Creates one sandbox project, imports all timelines, exports each
    as DRT, then cleans up. Much more efficient than calling
    _drt_sandbox_export multiple times.

    Args:
        resolve: DaVinci Resolve object
        xml_paths: List of paths to FCP7 XML files
        output_dir: Directory to write .drt files
        sequence_names: Names for each sequence (matching xml_paths order)

    Returns:
        List of (success, drt_path) tuples
    """
    results: list[tuple[bool, Optional[Path]]] = []
    pm = resolve.GetProjectManager()
    original_project = pm.GetCurrentProject()
    original_name = original_project.GetName() if original_project else None

    temp_name = f"pr2resolve_batch_{int(time.time())}"
    print(f"  Batch export: creating temp project \"{temp_name}\"")

    if original_project is not None:
        project_name = original_project.GetName() or ""
        if not project_name or project_name.startswith("Untitled"):
            pass
        else:
            try:
                pm.SaveProject()
            except Exception:
                pass
        pm.CloseProject(original_project)

    project = pm.CreateProject(temp_name)
    if project is None:
        print("  Error: Could not create temp project for batch export")
        if original_name:
            pm.LoadProject(original_name)
        return [(False, None)] * len(xml_paths)

    media_pool = project.GetMediaPool()

    for i, (xml_path, seq_name) in enumerate(zip(xml_paths, sequence_names)):
        print(f"  [{i+1}/{len(xml_paths)}] {seq_name}")
        # Smart media detection
        drt_xml, is_skeleton = _strip_file_elements_for_drt(xml_path)

        timeline = media_pool.ImportTimelineFromFile(
            str(drt_xml),
            {"timelineName": seq_name, "importSourceClips": not is_skeleton},
        )
        if is_skeleton and drt_xml.exists() and drt_xml != xml_path:
            drt_xml.unlink(missing_ok=True)

        if timeline is None:
            print(f"    Import FAILED")
            results.append((False, None))
            continue
        if lumetri_grades and i < len(lumetri_grades) and lumetri_grades[i]:
            grade_result = _apply_lumetri_grade(timeline, lumetri_grades[i])
            if grade_result["failed"] or grade_result["skipped"]:
                print("    Export skipped: Lumetri graph could not be applied")
                results.append((False, None))
                continue

        # Force timeline start timecode to 00:00:00:00
        # (DaVinci defaults to 01:00:00:00 regardless of XML sequence timecode)
        try:
            timeline.SetSetting("timelineStartTimecode", "00:00:00:00")
        except Exception:
            pass  # best-effort, silently ignore API limitations

        for warning in _apply_fcp_timeline_state_to_resolve(timeline, xml_path):
            print(f"    Warning: {warning}")

        drt_path = output_dir / f"{seq_name}_resolve.drt"
        # Dedup: append -1, -2, … if file already exists
        if drt_path.exists():
            for _n in range(1, 100):
                candidate = output_dir / f"{seq_name}_resolve-{_n}.drt"
                if not candidate.exists():
                    drt_path = candidate
                    break
        if timeline.Export(str(drt_path), resolve.EXPORT_DRT):
            try:
                retimed = apply_retimes_to_archive(drt_path, xml_path)
            except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
                print(f"    Export validation FAILED: {exc}; uncorrected package retained at {drt_path}")
                results.append((False, None))
                continue
            if retimed:
                print(f"    Exported package: {retimed} shared retime maps applied; live API timeline is unchanged")
            print(f"    DRT: {drt_path.name}")
            results.append((True, drt_path))
        else:
            print(f"    Export FAILED")
            results.append((False, None))

    # Restore
    pm.SaveProject()
    pm.CloseProject(project)
    try:
        pm.DeleteProject(temp_name)
    except Exception:
        pass
    if original_name:
        pm.LoadProject(original_name)
        print(f"  Restored: \"{original_name}\"")
    else:
        print("  (no original project to restore)")

    return results


# --- Feature: DRP Project Export ---

def _drp_export(
    resolve: Any,
    xml_paths: list[Path],
    output_path: Path,
    project_name: str,
    sequence_names: list[str],
    gui: bool = False,
    prproj_root: Optional[ET.Element] = None,
    lumetri_grades: Optional[list[list[LumetriGrade]]] = None,
) -> bool:
    """Export a full project as DRP with media pool, bin structure, and timelines.

    Faithfully reproduces the PR project in DaVinci:
    - Imports individual media files (NOT directories) into media pool
    - Recreates bin/folder structure from .prproj
    - Imports all timelines with full media linking

    Args:
        resolve: DaVinci Resolve object
        xml_paths: List of FCP7 XML paths for all sequences
        output_path: Path for the .drp file
        project_name: Name for the Resolve project
        sequence_names: Timeline names matching xml_paths
        gui: If True, leave project open in GUI (interactive mode).
             If False, clean up and shut down after export.
        prproj_root: Optional .prproj root for bin structure extraction.

    Returns:
        True if DRP exported successfully
    """
    pm = resolve.GetProjectManager()
    original_project = pm.GetCurrentProject()
    original_name = original_project.GetName() if original_project else None

    # Use original project name (DaVinci supports Chinese names)
    temp_name = project_name
    print(f"  DRP export: creating project \"{temp_name}\"")

    if original_project is not None:
        pname = original_project.GetName() or ""
        if pname and not pname.startswith("Untitled"):
            try:
                pm.SaveProject()
            except Exception:
                pass
        pm.CloseProject(original_project)

    # Find next available project name (don't delete existing projects)
    try:
        existing = pm.GetProjectListInCurrentFolder() or []
    except Exception:
        existing = []
    final_name = temp_name
    if temp_name in existing:
        for n in range(1, 100):
            candidate = f"{temp_name}-{n}"
            if candidate not in existing:
                final_name = candidate
                break
        if final_name != temp_name:
            print(f"  Project \"{temp_name}\" exists — using \"{final_name}\"")

    drp_result = False  # declared outside try so return below is safe
    project = None      # defensive init — if CreateProject throws, finally accesses safely
    try:
        project = pm.CreateProject(final_name)
        if project is None:
            # ── Retry strategy by scenario ──
            # Scenario A: BG mode, stale headless from previous run
            #   → Kill it, launch fresh, retry
            # Scenario B: ON mode, stale API connection to existing DaVinci
            #   → Cannot kill user's instance, try re-importing API module
            # Scenario C: ON mode, user's DaVinci, genuine failure
            #   → Report error, no retry (don't disturb user)
            if not gui:
                # BG mode: we own the instance — safe to kill and restart
                print("  CreateProject failed — restarting DaVinci (BG mode)...")
                _shutdown_resolve()
                # Force kill any leftover Resolve.exe
                if _is_resolve_running():
                    try:
                        subprocess.run(
                            ["taskkill", "/IM", "Resolve.exe", "/F"],
                            capture_output=True, timeout=10,
                        )
                        time.sleep(3)
                    except Exception:
                        pass
                resolve = _ensure_resolve_running(timeout=60, nogui=True)
                if resolve is None:
                    print("  Error: Could not restart DaVinci Resolve")
                    return False
                pm = resolve.GetProjectManager()
                project = pm.CreateProject(final_name)
            else:
                # ON mode: try refreshing API connection without killing DaVinci
                print("  CreateProject failed — refreshing API connection...")
                resolve = _try_import_resolve()
                if resolve is not None:
                    pm = resolve.GetProjectManager()
                    project = pm.CreateProject(final_name)

            if project is None:
                print("  Error: Could not create project for DRP export.")
                print("  Possible causes:")
                print("    - DaVinci API not accessible (check Preferences > External scripting)")
                print("    - Previous 'pr2resolve' project may need manual cleanup")
                print("    - DaVinci may need to be restarted manually")
                return False

        media_pool = project.GetMediaPool()

        # ── Set project color science from the first exported sequence ──
        profiles_by_uid: dict[str, dict[str, Any]] = {}
        if prproj_root is not None:
            index = _PrprojIndex.build(prproj_root)
            for sequence in _prproj_list_sequences(prproj_root, index):
                profiles_by_uid[sequence["uid"]] = _prproj_get_color_profiles(prproj_root, sequence["uid"])
        sequence_profiles: list[dict[str, Any]] = []
        for xml_path in xml_paths:
            source_sequence = ET.parse(xml_path).find("sequence")
            source_uid = source_sequence.get("id", "") if source_sequence is not None else ""
            sequence_profiles.append(profiles_by_uid.get(source_uid, {}))
        project_profiles = sequence_profiles[0] if sequence_profiles else {}
        if project_profiles:
            print(f"  Premiere color profile: output={project_profiles.get('OutputColorSpace', 'unknown')}; working={project_profiles.get('WorkingColorSpace', 'unknown')}; media={project_profiles.get('media', 'unknown')}")
        _apply_resolve_color_settings(project, project_profiles, "project")

        # ── Step 1: Recreate bin structure from PR project ──
        if prproj_root is not None:
            bins = _prproj_get_bin_structure(prproj_root)
            if bins:
                _create_bin_structure(media_pool, bins)
                print(f"  Bins: {len(bins)} folder(s) created")

        # ── Step 2: Import timelines and let Resolve bind source media ──
        import_failed = False
        failed_xmls: list[tuple[str, Path]] = []
        for seq_index, (xml_path, seq_name) in enumerate(zip(xml_paths, sequence_names)):
            print("=" * 60)
            print(f"  SEQ_NAME: {seq_name}")
            print(f"  XML: {xml_path}")
            print(f"  SIZE: {xml_path.stat().st_size:,} bytes")
            timeline = media_pool.ImportTimelineFromFile(
                str(xml_path),
                {"timelineName": seq_name, "importSourceClips": True},
            )
            if timeline is not None and sequence_profiles[seq_index]:
                try:
                    _apply_resolve_color_settings(timeline, sequence_profiles[seq_index], f'timeline "{seq_name}"')
                except (AttributeError, TypeError):
                    print(f"  Warning: timeline \"{seq_name}\" does not expose color settings in this Resolve version")
            print(f"  TIMELINE: {repr(timeline)}")
            if timeline is not None:
                print(f"  OK: {timeline.GetName()}")
                for warning in _apply_fcp_timeline_state_to_resolve(timeline, xml_path):
                    print(f"  Warning: {warning}")
                if lumetri_grades and seq_index < len(lumetri_grades):
                    grade_result = _apply_lumetri_grade(timeline, lumetri_grades[seq_index])
                    if grade_result["failed"] or grade_result["skipped"]:
                        import_failed = True
                try:
                    timeline.SetSetting("timelineStartTimecode", "00:00:00:00")
                except Exception:
                    pass
            else:
                print(f"  Timeline import FAILED: {seq_name}")
                failed_xmls.append((seq_name, xml_path))
                import_failed = True
        media_profiles = [item for profiles in sequence_profiles for item in profiles.get("media_profiles", [])]
        _apply_media_color_profiles(media_pool, media_profiles)
        if failed_xmls:
            print("=" * 60)
            print(f"  FAILED {len(failed_xmls)}/{len(xml_paths)} sequences:")
            for nm, xp in failed_xmls:
                print(f"    {nm}  ({xp.stat().st_size:,} bytes)  {xp}")

        # ── Step 3: Export DRP ──
        if import_failed:
            print("  DRP export ABORTED: timeline import or required grade application failed.")
        else:
            try:
                pm.SaveProject()
            except Exception:
                pass
            try:
                drp_result = pm.ExportProject(final_name, str(output_path), False)
            except Exception as e:
                print(f"  DRP export error: {e}")

            if drp_result:
                try:
                    retimed = apply_retimes_to_archive(output_path, xml_paths)
                    if retimed:
                        print(f"  Exported package: {retimed} shared retime maps applied; live API timeline is unchanged")
                except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as exc:
                    drp_result = False
                    print(f"  DRP validation FAILED: {exc}; uncorrected package retained at {output_path}")
                if drp_result:
                    print(f"  DRP exported: {output_path}")
            else:
                print(f"  DRP export via API failed (project may still be in database).")
                print(f"  Project \"{final_name}\" created in DaVinci database.")
                print(f"  To export manually: File -> Export Project -> {output_path.name}")

        if gui:
            print(f"  Project \"{final_name}\" ready in DaVinci Resolve.")

    finally:
        if not gui:
            try:
                pm.SaveProject()
            except Exception:
                pass
            try:
                pm.CloseProject(project)
            except Exception:
                pass
            try:
                pm.DeleteProject(final_name)
            except Exception:
                pass
            _shutdown_resolve()

    return drp_result


def _apply_fcp_timeline_state_to_resolve(timeline: Any, xml_path: Path) -> list[str]:
    """Apply timeline marks and markers through Resolve API when XML import omits them."""
    try:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        root = ET.parse(xml_path, parser=parser).getroot()
    except (ET.ParseError, OSError):
        return ["Could not read timeline XML state"]
    sequence = root.find("sequence")
    if sequence is None:
        return ["Timeline XML has no sequence state"]
    opaque: dict[str, Any] = {}
    for node in root:
        if node.tag is ET.Comment and (node.text or "").strip().startswith("pr2resolve-timeline-state:"):
            encoded = (node.text or "").strip().split(":", 1)[1]
            try:
                decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
                opaque = decoded if isinstance(decoded, dict) else {}
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                opaque = {}
            break
    in_element, out_element = sequence.find("in"), sequence.find("out")
    in_text = in_element.text if in_element is not None and in_element.text is not None else "-1"
    out_text = out_element.text if out_element is not None and out_element.text is not None else "-1"
    warnings: list[str] = []
    if in_element is not None or out_element is not None:
        if in_element is None or out_element is None:
            warnings.append("Sequence in/out state is incomplete; Resolve API cannot safely apply a partial pair")
        elif in_text == "-1" and out_text == "-1":
            if not hasattr(timeline, "ClearMarkInOut"):
                warnings.append("Resolve cannot clear explicitly unset sequence in/out marks")
            else:
                try:
                    if not timeline.ClearMarkInOut("all"):
                        warnings.append("Resolve rejected clearing sequence in/out marks")
                except (AttributeError, TypeError, RuntimeError) as exc:
                    warnings.append(f"Could not clear sequence in/out marks: {exc}")
        elif in_text != "-1" and out_text != "-1":
            if not hasattr(timeline, "SetMarkInOut"):
                warnings.append("Resolve cannot apply explicit sequence in/out marks")
            else:
                try:
                    if not timeline.SetMarkInOut(int(in_text), int(out_text), "all"):
                        warnings.append("Resolve rejected sequence in/out marks")
                except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
                    warnings.append(f"Could not apply sequence in/out marks: {exc}")
        else:
            warnings.append("Resolve API cannot safely apply a one-sided sequence in/out mark; source state remains in XML")
    markers = sequence.findall("marker")
    if markers:
        if not hasattr(timeline, "AddMarker"):
            warnings.append("Resolve cannot add sequence markers")
        elif not hasattr(timeline, "GetMarkers"):
            warnings.append("Cannot apply sequence markers because Resolve cannot report existing markers")
        else:
            try:
                existing = timeline.GetMarkers()
            except (AttributeError, RuntimeError, TypeError) as exc:
                warnings.append(f"Cannot read existing sequence markers: {exc}")
                existing = None
            if not isinstance(existing, dict):
                if existing is not None:
                    warnings.append("Cannot apply sequence markers because Resolve returned an invalid marker list")
                else:
                    warnings.append("Cannot apply sequence markers because Resolve returned no marker list")
            else:
                existing_markers: dict[int, dict[str, Any]] = {}
                occupied_frames: set[int] = set()
                for position, data in existing.items():
                    try:
                        frame = int(float(position))
                    except (TypeError, ValueError):
                        continue
                    occupied_frames.add(frame)
                    if isinstance(data, dict):
                        existing_markers[frame] = data
                opaque_markers = opaque.get("markers", [])
                source_markers = [item for item in opaque_markers
                                  if isinstance(item, dict) and item.get("scope") == "sequence"] if isinstance(opaque_markers, list) else []
                for marker_index, marker in enumerate(markers):
                    try:
                        frame = int(marker.findtext("in", "0"))
                        name = marker.findtext("name", "")
                        note = marker.findtext("comment", "")
                        raw_out = marker.findtext("out", "-1")
                        duration = max(int(raw_out) - frame, 1) if raw_out != "-1" else 1
                        if frame in occupied_frames:
                            imported = existing_markers.get(frame, {})
                            if (imported.get("name"), imported.get("note"), imported.get("duration")) != (name, note, duration):
                                warnings.append(f"Skipped sequence marker at frame {frame}: Resolve already has a different marker there")
                            continue
                        marker_data = source_markers[marker_index] if marker_index < len(source_markers) else {}
                        color_index = marker_data.get("color_index")
                        if color_index is not None:
                            warnings.append(
                                f"Premiere marker color index {color_index} is preserved in source state; "
                                "using Blue because its Resolve color mapping is unverified"
                            )
                        color = "Blue"
                        if not timeline.AddMarker(frame, color, name, note, duration):
                            warnings.append(f"Resolve rejected sequence marker at frame {frame}: {name}")
                        else:
                            existing_markers[frame] = {"name": name, "note": note, "duration": duration}
                        occupied_frames.add(frame)
                    except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
                        warnings.append(f"Could not apply sequence marker: {exc}")
    for state in _timeline_track_states(sequence):
        for method_name, value in (("SetTrackName", state.name), ("SetTrackEnable", state.enabled), ("SetTrackLock", state.locked)):
            if method_name == "SetTrackName" and not value:
                continue
            try:
                if not getattr(timeline, method_name)(state.type, state.index, value):
                    warnings.append(f"Resolve rejected {state.type} track {state.index} {method_name}")
            except (AttributeError, TypeError, RuntimeError) as exc:
                warnings.append(f"Could not apply {state.type} track {state.index} {method_name}: {exc}")
    label_records: list[dict[str, Any]] = []
    unknown_records = opaque.get("unknown", [])
    if not isinstance(unknown_records, list):
        unknown_records = []
    for entry in unknown_records:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        key, value = entry
        if not isinstance(key, str):
            continue
        if key.startswith("track-state:"):
            try:
                record = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                warnings.append(f"Could not read source track state: {key}")
                continue
            if isinstance(record, dict) and record.get("solo"):
                warnings.append(f"Solo track state is not applied: {key}")
            continue
        if not key.startswith("clip-label:"):
            continue
        try:
            record = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if record.get("preset"):
            label_records.append(record)
    item_indexes: dict[tuple[str, int], dict[tuple[str, int, int], list[Any]]] = {}
    for record in label_records:
        try:
            track_type = str(record["media_type"])
            track_index = int(record["track_index"])
            expected_name = str(record["name"])
            expected_start = int(record["start"])
            expected_duration = int(record["end"]) - expected_start
            track_key = (track_type, track_index)
            if track_key not in item_indexes:
                item_index: dict[tuple[str, int, int], list[Any]] = {}
                try:
                    if not hasattr(timeline, "GetStartFrame"):
                        raise AttributeError("GetStartFrame is unavailable")
                    timeline_start = int(timeline.GetStartFrame())
                except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
                    warnings.append(f"Cannot match clip labels without the timeline start frame: {exc}")
                    return warnings
                if not hasattr(timeline, "GetItemListInTrack"):
                    warnings.append("Cannot match clip labels because Resolve cannot list timeline track items")
                    return warnings
                for item in timeline.GetItemListInTrack(track_type, track_index) or []:
                    try:
                        item_key = (str(item.GetName()), int(item.GetStart()) - timeline_start, int(item.GetDuration()))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    item_index.setdefault(item_key, []).append(item)
                item_indexes[track_key] = item_index
            preset = str(record["preset"])
            suffix = preset.rsplit(".", 1)[-1]
            preset_index = int(suffix) if suffix.isdigit() else -1
            resolve_enum = PP_LABEL_TO_RESOLVE_COLOR.get(preset_index)
            color = RESOLVE_CLIP_COLORS.get(resolve_enum) if resolve_enum is not None else None
            if color is None:
                continue
            for item in item_indexes[track_key].get((expected_name, expected_start, expected_duration), []):
                if hasattr(item, "SetClipColor"):
                    if not item.SetClipColor(color):
                        warnings.append(f"Resolve rejected clip color for {expected_name} at frame {expected_start}")
                    break
        except (AttributeError, TypeError, ValueError, RuntimeError):
            continue
    return warnings


# --- Feature: Bin Structure Extraction ---

def _prproj_get_bin_structure(prproj_root: ET.Element) -> list[str]:
    """Extract bin folder names from a .prproj project.

    Args:
        prproj_root: <PremiereData> root element

    Returns:
        List of bin names (flat, in document order)
    """
    bins: list[str] = []
    for bin_el in prproj_root.findall("BinProjectItem"):
        name = bin_el.findtext("ProjectItem/Name") or bin_el.findtext("Name") or ""
        if name:
            bins.append(name)
    return bins


def _prproj_get_bin_members(prproj_root: ET.Element) -> list[ProjectBin]:
    """Return bins with direct media/sequence identities and parent links."""
    idx = _PrprojIndex.build(prproj_root)
    bin_elements = prproj_root.findall("BinProjectItem")

    def _resolve_item(item: ET.Element) -> ET.Element | None:
        uref = item.get("ObjectURef")
        if uref:
            return idx.resolve_uref(uref)
        ref = item.get("ObjectRef")
        return idx.resolve_ref(ref) if ref else None

    parent_by_uid: dict[str, str] = {}
    for parent in bin_elements:
        parent_uid = parent.get("ObjectUID", "")
        if not parent_uid:
            continue
        for item in parent.findall("ProjectItemContainer/Items/Item"):
            child = _resolve_item(item)
            child_uid = child.get("ObjectUID", "") if child is not None else ""
            if child is not None and child.tag == "BinProjectItem" and child_uid:
                parent_by_uid[child_uid] = parent_uid

    result: list[ProjectBin] = []
    for bin_el in bin_elements:
        uid = bin_el.get("ObjectUID", "")
        name = bin_el.findtext("ProjectItem/Name") or bin_el.findtext("Name") or ""
        if not uid or not name:
            continue
        project_bin = ProjectBin(uid=uid, name=name, parent_uid=parent_by_uid.get(uid))
        for item in bin_el.findall("ProjectItemContainer/Items/Item"):
            target = _resolve_item(item)
            if target is None or target.tag != "ClipProjectItem":
                continue
            master_ref = target.find("MasterClip")
            master = idx.resolve_uref(master_ref.get("ObjectURef", "")) if master_ref is not None else None
            if master is None and master_ref is not None and master_ref.get("ObjectRef"):
                master = idx.resolve_ref(master_ref.get("ObjectRef", ""))
            if master is None:
                continue
            for clip_ref in master.findall("Clips/Clip"):
                clip = idx.resolve_ref(clip_ref.get("ObjectRef", ""))
                if clip is None:
                    continue
                inner = clip.find("Clip")
                source_ref = inner.find("Source") if inner is not None else None
                source = idx.resolve_ref(source_ref.get("ObjectRef", "")) if source_ref is not None else None
                if source is None:
                    continue
                sequence_ref = source.find("SequenceSource/Sequence")
                if sequence_ref is not None:
                    sequence = idx.resolve_uref(sequence_ref.get("ObjectURef", ""))
                    if sequence is None and sequence_ref.get("ObjectRef"):
                        sequence = idx.resolve_ref(sequence_ref.get("ObjectRef", ""))
                    sequence_uid = sequence.get("ObjectUID", "") if sequence is not None else ""
                    if sequence_uid and sequence_uid not in project_bin.sequence_uids:
                        project_bin.sequence_uids.append(sequence_uid)
                    continue
                media_path, _, _ = _prproj_find_media_for_clip(clip, idx)
                if media_path and media_path not in project_bin.media_paths:
                    project_bin.media_paths.append(media_path)
        result.append(project_bin)
    return result


def _prproj_get_color_profiles(prproj_root: ET.Element, sequence_uid: str = "") -> dict[str, Any]:
    """Read sequence-scoped Premiere profiles by following its object graph."""
    import json as _json
    result: dict[str, Any] = {}
    scope: list[ET.Element]
    if sequence_uid:
        idx = _PrprojIndex.build(prproj_root)
        start = idx.by_uid.get(sequence_uid)
        scope, queue, seen = [], deque([start] if start is not None else []), set()
        while queue:
            current = queue.popleft()
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            scope.append(current)
            for child in current.iter():
                target = None
                if child.get("ObjectRef"):
                    target = idx.resolve_ref(child.get("ObjectRef", ""))
                elif child.get("ObjectURef"):
                    target = idx.resolve_uref(child.get("ObjectURef", ""))
                if target is not None and id(target) not in seen:
                    queue.append(target)
    else:
        scope = [prproj_root]

    def decoded_name(value: str, configuration: bool = False) -> str:
        try:
            data = _json.loads(value)
            if configuration:
                return str(data.get("workingSpaceID") or value)
            return str(data.get("baseColorProfile", {}).get("colorProfileName") or value)
        except (_json.JSONDecodeError, AttributeError):
            return value

    for key in ("WorkingColorSpace", "OutputColorSpace", "WorkingColorSpaceConfiguration"):
        value = next((element.text or "" for node in scope for element in node.iter(key) if element.text), "")
        if value:
            result[key] = decoded_name(value, key == "WorkingColorSpaceConfiguration")
    media_profiles: list[dict[str, str]] = []
    for node in scope:
        override = node.findtext("OverriddenColorSpace", "")
        original = node.findtext("OriginalColorSpace", "")
        chosen = override or original
        if chosen:
            object_id, object_uid = node.get("ObjectID"), node.get("ObjectUID")
            owner = next((candidate for candidate in scope if candidate.tag == "Media" and any(
                (object_id is not None and child.get("ObjectRef") == object_id)
                or (object_uid is not None and child.get("ObjectURef") == object_uid)
                for child in candidate.iter()
            )), None)
            path = owner.findtext("FilePath", "") if owner is not None else ""
            identity = ((owner.get("ObjectUID") or owner.get("ObjectID")) if owner is not None else None) or node.get("ObjectUID") or node.get("ObjectID") or ""
            media_profiles.append({
                "id": identity,
                "path": path,
                "name": Path(path).name if path else "",
                "profile": decoded_name(chosen),
                "source": "override" if override else "original",
            })
    if media_profiles:
        unique = list({(item["id"], item["profile"], item["source"]): item for item in media_profiles}.values())
        result["media_profiles"] = unique
        result["media"] = unique[0]["profile"]
    return result


def _resolve_color_settings(profiles: dict[str, Any]) -> dict[str, str]:
    """Build color setting requests; Resolve acceptance and readback are separate."""
    working = str(profiles.get("WorkingColorSpace") or profiles.get("WorkingColorSpaceConfiguration") or "").casefold()
    output = str(profiles.get("OutputColorSpace") or "").casefold()
    if "acescct" in working:
        return {"colorScienceMode": "acescct"}
    if "2100 hlg" in working or "2100 hlg" in output:
        return {
            "colorScienceMode": "davinciYRGBColorManaged",
            "isAutoColorManage": "0",
            "rcmPresetMode": "Custom",
            "colorSpaceTimeline": "Rec.2100 HLG",
            "colorSpaceOutput": "Rec.2100 HLG",
        }
    if not working or "709" in working or "display-referred" in working:
        return {"colorScienceMode": "davinciYRGB"}
    return {}


def _apply_resolve_color_settings(target: Any, profiles: dict[str, Any], label: str) -> None:
    settings = _resolve_color_settings(profiles)
    if not settings:
        print(f"  Warning: {label} color profile has no Resolve mapping: {profiles}")
        return
    failed = []
    for key, value in settings.items():
        try:
            if target.SetSetting(key, value) is not True:
                failed.append(key)
        except Exception:
            failed.append(key)
    if failed:
        print(f"  Warning: {label} rejected color setting(s): {', '.join(failed)}")


def _resolve_pathurl(url: str) -> Optional[str]:
    """Convert a FCP7 pathurl to a local filesystem path.

    Handles both formats: file://localhost/... and file:///...
    Applies percent-decoding (unquote) for both.
    Returns None for unrecognized or empty URLs.
    """
    from urllib.parse import unquote, urlparse
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme.casefold() != "file":
        return None
    path = unquote(parsed.path)
    host = parsed.netloc
    if host and host.casefold() != "localhost":
        if len(host) == 2 and host[1] == ":":
            path = f"{host}{path}"
        else:
            path = f"//{host}{path}"
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path.replace("/", "\\")


def _extract_media_files(xml_path: Path) -> list[str]:
    """Extract unique local media file paths from XML pathurls.

    Scans all <pathurl> elements, resolves to local paths, returns only
    files that exist on disk. Used by DRP export to import individual
    media files (NOT directories) into the DaVinci media pool.

    Args:
        xml_path: Path to the FCP7 XML file

    Returns:
        List of unique local file paths that exist
    """
    seen: set[str] = set()
    result: list[str] = []
    try:
        tree = ET.parse(str(xml_path))
        for pu in tree.iter("pathurl"):
            local = _resolve_pathurl(pu.text or "")
            if local and local not in seen:
                seen.add(local)
                if Path(local).exists():
                    result.append(local)
    except Exception:
        pass
    return result


def _import_media_to_pool(media_pool: Any, files: list[str]) -> int:
    """Import individual media files into DaVinci media pool.

    Imports at FILE level (not directory) to avoid DaVinci's recursive
    directory scan which causes memory explosion and hangs.

    Args:
        media_pool: DaVinci MediaPool object
        files: List of local file paths to import

    Returns:
        Number of items successfully imported
    """
    if not files:
        return 0
    try:
        items = media_pool.ImportMedia(files)
        return len(items) if items else 0
    except Exception as e:
        print(f"  Warning: media import failed ({e})")
        return 0


def _resolve_input_profile(name: str) -> str:
    normalized = name.casefold().replace("rec. ", "rec.")
    if "apple log" in normalized:
        return "Apple Log"
    if "2100 pq" in normalized or "st2084" in normalized:
        return "Rec.2100 ST2084"
    if "2100 hlg" in normalized:
        return "Rec.2100 HLG"
    if "display p3" in normalized or "p3-d65" in normalized:
        return "P3-D65"
    if "709" in normalized or "display-referred" in normalized:
        return "Rec.709 Gamma 2.4"
    return ""


def _apply_media_color_profiles(media_pool: Any, profiles: list[dict[str, str]]) -> None:
    """Apply Premiere media overrides after API import, keyed by full path."""
    requested = {
        os.path.normcase(os.path.normpath(item["path"])): _resolve_input_profile(item["profile"])
        for item in profiles if item.get("path") and _resolve_input_profile(item.get("profile", ""))
    }
    if not requested:
        return
    try:
        clips = media_pool.GetRootFolder().GetClipList() or []
    except Exception:
        print("  Warning: Resolve did not expose imported clips for input color-space assignment")
        return
    applied, rejected = 0, 0
    for clip in clips:
        try:
            path = clip.GetClipProperty("File Path") or clip.GetClipProperty("FilePath") or ""
            target = requested.get(os.path.normcase(os.path.normpath(path)))
            if target:
                if clip.SetClipProperty("Input Color Space", target) is True:
                    applied += 1
                else:
                    rejected += 1
        except Exception:
            rejected += 1
    print(f"  Media input color space: {applied} applied" + (f", {rejected} rejected" if rejected else ""))


def _create_bin_structure(media_pool: Any, bins: list[str]) -> None:
    """Create bin/folder structure in DaVinci media pool from PR project bins.

    Creates folders under the media pool root. Silently skips names that
    already exist (DaVinci returns existing folder if name conflicts).

    Args:
        media_pool: DaVinci MediaPool object
        bins: List of bin/folder names to create
    """
    if not bins:
        return
    root = media_pool.GetRootFolder()
    for name in bins:
        try:
            media_pool.AddSubFolder(root, name)
        except Exception:
            pass  # bin already exists or name conflict


def _strip_file_elements_for_drt(xml_path: Path) -> tuple[Path, bool]:
    """Prepare XML for DRT/DRP import — strip <file> only if ALL media is offline.

    If any media file referenced in the XML exists on disk, import with
    full media (importSourceClips: True) so DaVinci can link them.
    Only strip <file> elements when ALL media is offline — this prevents
    the <MediaFilePath> flash-crash on foreign machines while preserving
    real media on the author's machine.

    Args:
        xml_path: Path to the FCP7 XML file

    Returns:
        (path_to_use, needs_cleanup) tuple
    """
    try:
        tree = ET.parse(str(xml_path))
        if tree.find(".//file") is None:
            return (xml_path, False)

        # Check if ANY media file exists locally
        has_local_media = False
        for pu in tree.iter("pathurl"):
            local_path = _resolve_pathurl(pu.text or "")
            if local_path and Path(local_path).exists():
                has_local_media = True
                break

        if has_local_media:
            # Media exists — do NOT strip, let DaVinci link it
            return (xml_path, False)

        # All offline — strip <file> to prevent DRT corruption
        stripped = copy.deepcopy(tree.getroot())
        for ci in stripped.iter("clipitem"):
            fi = ci.find("file")
            if fi is not None:
                ci.remove(fi)
        temp = xml_path.parent / f"_pr2resolve_stripped_{int(time.time())}.xml"
        ET.ElementTree(stripped).write(str(temp), encoding="utf-8", xml_declaration=True)
        content = temp.read_text(encoding="utf-8")
        content = content.replace(
            '<?xml version="1.0" encoding="utf-8"?>',
            '<?xml version="1.0" encoding="UTF-8"?>\n' + FCP7_DOCTYPE
        )
        temp.write_text(content, encoding="utf-8")
        print("  (all media offline — skeleton import)")
        return (temp, True)
    except Exception:
        return (xml_path, False)


def _drt_sandbox_export(
    resolve: Any,
    xml_path: Path,
    output_path: Path,
    timeline_name: str = "Imported",
    lumetri_grades: Optional[list[LumetriGrade]] = None,
) -> bool:
    """Export DRT via a temporary sandbox project.

    Creates a temporary DaVinci project, imports the XML, exports DRT,
    then restores the user's original project. Never touches user's project.

    DaVinci API facts used:
    - DeleteTimeline() does NOT exist -> sandbox project is the only clean path
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
    original_name = None
    try:
        pm = resolve.GetProjectManager()
        original_project = pm.GetCurrentProject()
        original_name = original_project.GetName() if original_project else None

        # Create unique temp project name
        temp_name = f"pr2resolve_drt_{int(time.time())}"
        print(f"  Creating temporary project: \"{temp_name}\"")

        # Detach from current project if one is open.
        # Scenario 1: No project -> skip save/close
        # Scenario 2: User editing -> save before close
        # Scenario 3: Auto-load recent -> treat as scenario 1 (Untitled = no user work)
        if original_project is not None:
            project_name = original_project.GetName() or ""
            is_default = not project_name or project_name.startswith("Untitled")
            if not is_default:
                # User has real work open — save before switching
                try:
                    pm.SaveProject()
                except Exception:
                    pass
            pm.CloseProject(original_project)

        project = pm.CreateProject(temp_name)
        if project is None:
            print("  Error: Could not create temporary project")
            # Try to restore original
            if original_name:
                pm.LoadProject(original_name)
            return False

        media_pool = project.GetMediaPool()

        # Smart media detection: if local media exists, import with clips
        drt_import_xml, is_skeleton = _strip_file_elements_for_drt(xml_path)

        timeline = media_pool.ImportTimelineFromFile(
            str(drt_import_xml),
            {"timelineName": timeline_name, "importSourceClips": not is_skeleton},
        )
        if timeline is None and not is_skeleton:
            # Fallback: full import failed → retry skeleton
            timeline = media_pool.ImportTimelineFromFile(
                str(drt_import_xml),
                {"timelineName": timeline_name, "importSourceClips": False},
            )

        if is_skeleton and drt_import_xml.exists() and drt_import_xml != xml_path:
            drt_import_xml.unlink(missing_ok=True)

        if timeline is not None:
            print("  (timeline structure imported, media to be relinked on target machine)")

        if timeline is None:
            print("  Error: Failed to import timeline from XML")
            # Clean up and restore
            pm.CloseProject(project)
            if original_name:
                pm.LoadProject(original_name)
            return False

        print(f"  Timeline imported: {timeline.GetName()}")

        # Force timeline start timecode to 00:00:00:00
        try:
            timeline.SetSetting("timelineStartTimecode", "00:00:00:00")
        except Exception:
            pass

        for warning in _apply_fcp_timeline_state_to_resolve(timeline, xml_path):
            print(f"  Warning: {warning}")

        if lumetri_grades:
            grade_result = _apply_lumetri_grade(timeline, lumetri_grades)
            if grade_result["failed"] or grade_result["skipped"]:
                print("  Error: Lumetri graph could not be applied")
                pm.CloseProject(project)
                if original_name:
                    pm.LoadProject(original_name)
                return False

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

        retimed = apply_retimes_to_archive(output_path, xml_path)
        if retimed:
            print(f"  Exported package: {retimed} shared retime maps applied; live API timeline is unchanged")
        print(f"  DRT exported: {output_path}")

        # Save temp project before closing to avoid DaVinci save prompt,
        # then switch back to original. Delete temp afterward.
        try:
            pm.SaveProject()
        except Exception:
            pass
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


def _ensure_resolve_running(timeout: int = 60, nogui: bool = True) -> Any:
    """Ensure DaVinci Resolve is running and the Scripting API is available.

    If a GUI instance is already running, reuses it (never launches a
    second process). Otherwise starts headless (-nogui) by default.

    Args:
        timeout: Maximum seconds to wait for DaVinci to become ready
        nogui: If True, use headless mode (default). Ignored if GUI
               instance already running.

    Returns:
        The Resolve object if available, None otherwise
    """
    global _resolve_process, _launch_mode
    resolve = _check_resolve_running()
    if resolve is not None:
        # API already available — check mode compatibility
        if not nogui and _launch_mode == "headless":
            print("  DRP requires GUI — restarting DaVinci Resolve...")
            _shutdown_resolve()
            time.sleep(3)
            # Fall through to launch below
        else:
            return resolve

    # Check if a Resolve process is running but API not ready (stale or cold start)
    if _is_resolve_running():
        # API not available but process exists — may be stale headless from previous run
        if _launch_mode == "headless":
            print("  Stale headless DaVinci detected — restarting...")
            _shutdown_resolve()
            time.sleep(3)
            # Fall through to launch below
        else:
            print("  DaVinci is running. Waiting for API...")
            start = time.time()
            while time.time() - start < timeout:
                time.sleep(2)
                elapsed = int(time.time() - start)
                print(f"  Waiting for DaVinci API... ({elapsed}s)")
                resolve = _check_resolve_running()
                if resolve is not None:
                    print(f"  API ready after {elapsed}s.")
                    return resolve
            print(f"  API did not become available within {timeout}s.")
            return None

    # No instance at all — launch one
    mode = "headless" if nogui else "GUI"
    print(f"  Launching DaVinci Resolve ({mode})...")
    install_dir = _find_resolve_install_dir()
    if not install_dir:
        print("  Could not find DaVinci installation.")
        return None
    exe = install_dir / "Resolve.exe"
    if not exe.exists():
        print(f"  Resolve.exe not found at: {exe}")
        return None
    try:
        cmd = [str(exe)]
        if nogui:
            cmd.append("-nogui")
        _launch_mode = "headless" if nogui else "gui"
        _resolve_process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if sys.platform == "win32" else 0,
        )
        print(f"  Started: {exe}" + (" -nogui" if nogui else "") + f" (PID {_resolve_process.pid})")
    except Exception as e:
        print(f"  Failed to start: {e}")
        return None

    start = time.time()
    poll_interval = 2
    while time.time() - start < timeout:
        time.sleep(poll_interval)
        elapsed = int(time.time() - start)
        print(f"  Waiting for DaVinci... ({elapsed}s)")
        resolve = _check_resolve_running()
        if resolve is not None:
            print(f"  DaVinci ready after {elapsed}s.")
            return resolve
        if elapsed > 15:
            poll_interval = 5

    print(f"  DaVinci did not start within {timeout}s.")
    _shutdown_resolve()  # kill the process we just launched
    return None


def _apply_lumetri_grade(timeline: Any, grades: list[LumetriGrade]) -> dict[str, int]:
    """Apply the same DRX Body used by native output to each matched clip."""
    result = {"applied": 0, "partial": 0, "skipped": 0, "failed": 0}
    timeline_start = int(timeline.GetStartFrame())
    with tempfile.TemporaryDirectory(prefix="pr2resolve-grade-") as temp:
        for index, grade in enumerate(grades):
            if not grade.drx_payload:
                result["skipped"] += 1
                print(f"  Lumetri skipped: {grade.clip_name}: {grade.skipped_reason}")
                continue
            matches = [item for item in timeline.GetItemListInTrack("video", grade.track_index) or []
                       if item.GetStart() - timeline_start == grade.start_frame
                       and item.GetEnd() - timeline_start == grade.end_frame]
            if len(matches) != 1:
                result["skipped"] += 1
                print(f"  Lumetri skipped: {grade.clip_name} ({len(matches)} identity matches)")
                continue
            path = Path(temp) / f"grade-{index}.drx"
            path.write_bytes(grade.drx_payload)
            try:
                applied = matches[0].GetNodeGraph().ApplyGradeFromDRX(str(path), 0)
                if applied is not True:
                    result["failed"] += 1
                    print(f"  Lumetri failed: {grade.clip_name}: ApplyGradeFromDRX returned {applied!r}")
                    continue
                result["applied"] += 1
                if grade.warnings:
                    result["partial"] += 1
                    print(f"  Lumetri partial: {grade.clip_name}: {'; '.join(grade.warnings)}")
            except (AttributeError, TypeError, RuntimeError) as exc:
                result["failed"] += 1
                print(f"  Lumetri failed: {grade.clip_name}: {exc}")
    print("  Lumetri DRX: " + ", ".join(f"{key}={value}" for key, value in result.items()))
    return result
