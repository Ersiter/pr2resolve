"""pr2engine - pr2resolve core engine.

Consolidated module: diagnostics, fix engine, validator, output,
prproj parser, and DRT bridge.
"""

from __future__ import annotations

import copy
import gzip
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from xml.dom import minidom

from pr2_constants import (
    VERSION, DEFAULT_FPS, MICROSECOND, NTSC_RATES, PAL_RATES, FPS_TOLERANCE,
    FCP7_VERSION, FCP7_DOCTYPE, FCP7_CLIPIITEM_ORDER,
    CRITICAL, MAJOR, MINOR,
    _ORDER_MAP,
    Issue, ScaleIssue, ClipData,
    _build_file_index, _get_sequence_format, _get_sequence_resolution,
    _is_ntsc, load_xml, load_prproj,
)


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
        filepath: Absolute Windows path (e.g. ``E:\\HW\\...``)

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
        lines.append("All 23 validation checks passed.")
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
    if ticks <= 0:
        return 0
    # pproTicks per second = 254016000000
    ppro_ticks_per_sec = 254016000000
    seconds = ticks / ppro_ticks_per_sec
    return int(round(seconds * fps))


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
        fp = media_el.findtext("FilePath", "")
        if not fp:
            continue
        if Path(fp.replace("\\", "/")).name.lower() != mc_lower:
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
                w = int(parts[2])
                h = int(parts[3])
                if w > 0 and h > 0:
                    return (w, h)
        break
    return (0, 0)


def _prproj_frames_to_timecode_string(total_frames: int, fps: float, is_ntsc: bool) -> str:
    """Convert absolute frame count to timecode string at given frame rate.

    Uses non-drop-frame (NDF) calculation. For fractional NTSC rates
    (23.976, 29.97, 59.94), NDF is the standard FCP7 XML convention.

    Args:
        total_frames: Absolute frame number (0-based)
        fps: Actual frames per second (e.g. 59.94)
        is_ntsc: Use ';' separator for NTSC, ':' for integer rates

    Returns:
        Timecode string like '13:01:15:00' or '00;00;00;00'
    """
    display_fps = int(round(fps))
    if display_fps <= 0:
        display_fps = 30
    hours = total_frames // (3600 * display_fps)
    remainder = total_frames % (3600 * display_fps)
    minutes = remainder // (60 * display_fps)
    remainder = remainder % (60 * display_fps)
    seconds = remainder // display_fps
    frames = remainder % display_fps
    sep = ";" if is_ntsc else ":"
    return f"{hours:02d}{sep}{minutes:02d}{sep}{seconds:02d}{sep}{frames:02d}"


@dataclass
class _SourceTCInfo:
    """Extracted source media timecode and frame rate info."""
    media_fps: float = 30.0               # actual source FPS (e.g. 59.94)
    is_ntsc: bool = False                  # whether source rate is NTSC
    timecode_frame: int = 0                # timecode start in source-rate frames
    timecode_string: str = "00:00:00:00"   # formatted TC string
    full_duration_frames: int = 0          # full file duration in source-rate frames
    resolved: bool = False                 # True if real TC data was found


def _prproj_extract_source_tc_info(
    mc_el: ET.Element,
    idx: _PrprojIndex,
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
    if not li_ref:
        return info
    cli = idx.resolve_ref(li_ref)
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
        except (ValueError, TypeError):
            pass
    info.is_ntsc = _is_ntsc_fps(info.media_fps)

    # Extract MediaInPoint (pproTicks → timecode)
    mip_text = cli.findtext("MediaInPoint")
    if mip_text and mip_text.strip():
        try:
            mip_ticks = int(mip_text)
            if mip_ticks > 0:
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
        except (ValueError, TypeError):
            pass

    return info


def _ffprobe_read_timecode(filepath: str) -> _SourceTCInfo:
    """Read source timecode and frame rate from a media file using ffprobe.

    Tries three approaches in order:
    1. stream=timecode (professional cameras write this)
    2. format_tags=timecode (DJI MOV wrapper)
    3. format_tags=creation_time → time-of-day TC (DJI MP4)

    Also reads r_frame_rate for actual source FPS and duration for
    full file length. All errors/timeouts are silently caught — the
    caller checks info.resolved to decide whether to use the result.

    Args:
        filepath: Absolute path to the media file

    Returns:
        _SourceTCInfo with ffprobe-extracted values, or defaults on failure
    """
    info = _SourceTCInfo()
    try:
        # 1. Try stream-level timecode first
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=timecode",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=15
        )
        tc_str = result.stdout.strip()
        if tc_str and result.returncode == 0:
            info.timecode_string = tc_str
            info.resolved = True

        # 2. Get frame rate
        result2 = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=15
        )
        fps_str = result2.stdout.strip()
        if fps_str and "/" in fps_str:
            try:
                num, den = fps_str.split("/", 1)
                if int(den) > 0:
                    info.media_fps = round(int(num) / int(den), 3)
            except (ValueError, ZeroDivisionError):
                pass
        elif fps_str:
            try:
                info.media_fps = float(fps_str)
            except ValueError:
                pass
        info.is_ntsc = _is_ntsc_fps(info.media_fps)

        # 3. If stream timecode not found, try format tags
        if not info.resolved:
            result3 = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format_tags=timecode",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=15
            )
            tc_str2 = result3.stdout.strip()
            if tc_str2:
                info.timecode_string = tc_str2
                info.resolved = True

        # 4. Last resort: use creation_time as time-of-day TC
        if not info.resolved:
            result4 = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format_tags=creation_time",
                 "-of", "default=noprint_wrappers=1:nokey=1", filepath],
                capture_output=True, text=True, timeout=15
            )
            ct = result4.stdout.strip()
            if ct:
                # Parse ISO 8601 creation time → timecode string
                # e.g. "2026-05-30T11:57:12.000000Z" → "11:57:12:00"
                try:
                    dt_str = ct.replace("Z", "+00:00")
                    dt = datetime.fromisoformat(dt_str)
                    display_fps = int(round(info.media_fps)) or 30
                    tc_seconds = dt.hour * 3600 + dt.minute * 60 + dt.second
                    tc_frames = int(round(dt.microsecond / 1_000_000 * display_fps))
                    sep = ";" if info.is_ntsc else ":"
                    info.timecode_string = (
                        f"{dt.hour:02d}{sep}{dt.minute:02d}{sep}"
                        f"{dt.second:02d}{sep}{tc_frames:02d}"
                    )
                    info.resolved = True
                except (ValueError, IndexError):
                    pass

        # 5. Get duration
        result5 = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=15
        )
        dur_str = result5.stdout.strip()
        if dur_str:
            try:
                dur_secs = float(dur_str)
                info.full_duration_frames = int(round(dur_secs * info.media_fps))
            except ValueError:
                pass

    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # ffprobe not installed, or file inaccessible
        pass

    return info


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
                    sc_ref = subclip.get("ObjectRef")
                    sc_el = idx.resolve_ref(sc_ref) if sc_ref else None
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

    # ─── Build XML skeleton (DC format: name→duration→rate→in→out→timecode→media) ───
    xmeml = ET.Element("xmeml")
    xmeml.set("version", FCP7_VERSION)

    sequence = ET.SubElement(xmeml, "sequence")
    total_frames = 0

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
    audio_section = ET.SubElement(media, "audio")

    # ─── Shared state ────────────────────────────────────────────────
    _id_counter = [0]  # single global counter — DC uses unique IDs per-name
    _file_ids: dict[str, str] = {}  # media_name → file-id
    _link_groups: dict[str, list[tuple[str, str, int, int]]] = {}  # name → [(ci_id, mediatype, t_idx, c_idx)]

    def _next_ci_id(name: str) -> str:
        cid = f"{name} {_id_counter[0]}"
        _id_counter[0] += 1
        return cid

    def _next_fi_id(name: str) -> str:
        fid = f"{name} {_id_counter[0]}"
        _id_counter[0] += 1
        return fid

    # ─── Helper: extract clip data from .prproj chain ────────────────
    def _extract_clip(cti: ET.Element) -> ClipData:
        """Returns (name, media_path, start, end, in_pt, out_pt, speed, tc, sw, sh, scale, rot)."""
        subclip = cti.find("SubClip")
        mc_name = "(unknown)"
        media_path = ""
        in_point = 0
        out_point = 0
        playback_speed = 100
        source_tc = _SourceTCInfo()
        src_w, src_h = w, h
        scale_val = 100.0
        rotation_val = 0.0

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
                        for media_el in prproj_root.findall("Media"):
                            mfp = media_el.findtext("FilePath")
                            if not mfp:
                                continue
                            if Path(mfp.replace("\\", "/")).name.lower() == mc_name.lower():
                                media_path = mfp
                                break
                        source_tc = _prproj_extract_source_tc_info(mc_el, idx)
                        if not source_tc.resolved and media_path:
                            local = Path(media_path)
                            if local.exists():
                                source_tc = _ffprobe_read_timecode(str(local))
                        sr = _prproj_get_source_resolution(prproj_root, mc_name, idx)
                        if sr[0] > 0 and sr[1] > 0:
                            src_w, src_h = sr
                clip_ref_el = sc_el.find("Clip")
                if clip_ref_el is not None:
                    clip_ref = clip_ref_el.get("ObjectRef")
                    clip_el = idx.resolve_ref(clip_ref) if clip_ref is not None else None
                    if clip_el is not None:
                        inner = clip_el.find("Clip")
                        if inner is not None:
                            ip = inner.findtext("InPoint")
                            op = inner.findtext("OutPoint")
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

        # Motion data
        co = cti.find("ComponentOwner")
        if co is not None:
            comps = co.find("Components")
            if comps is not None:
                chain = idx.resolve_ref(comps.get("ObjectRef", ""))
                if chain is not None and chain.findtext("DefaultMotion") == "false":
                    chain_comps = chain.find("Components")
                    if chain_comps is not None:
                        for c in chain_comps.findall("Component"):
                            c_el = idx.resolve_ref(c.get("ObjectRef", ""))
                            if c_el is None:
                                continue
                            inner_c = c_el.find(".//Params")
                            if inner_c is None:
                                continue
                            for p_ref in inner_c.findall("Param"):
                                p_el = idx.resolve_ref(p_ref.get("ObjectRef", ""))
                                if p_el is None:
                                    continue
                                pname = p_el.findtext("Name", "")
                                sk = p_el.findtext("StartKeyframe", "")
                                if not pname or not sk:
                                    continue
                                parts = sk.split(",")
                                if len(parts) < 2:
                                    continue
                                try:
                                    val = float(parts[1])
                                except ValueError:
                                    continue
                                if pname == "Scale":
                                    scale_val = val
                                elif pname == "Rotation":
                                    rotation_val = val

        return ClipData(
            name=mc_name, media_path=media_path,
            start=start, end=end, in_pt=in_point, out_pt=out_point,
            playback_speed=playback_speed, source_tc=source_tc,
            source_w=src_w, source_h=src_h, scale=scale_val, rotation=rotation_val,
        )

    # ─── Helper: build a DC-format file element ─────────────────────
    def _build_file(name: str, path: str, dur: int, tc_info: _SourceTCInfo,
                    sw: int, sh: int, for_audio_only: bool = False) -> ET.Element:
        fid = _next_fi_id(name)
        _file_ids.setdefault(name, fid)
        fe = ET.Element("file")
        fe.set("id", fid)
        src_tb = int(round(tc_info.media_fps))
        src_ntsc = tc_info.is_ntsc
        ET.SubElement(fe, "duration").text = str(dur)
        fr = ET.SubElement(fe, "rate")
        ET.SubElement(fr, "timebase").text = str(src_tb)
        ET.SubElement(fr, "ntsc").text = "TRUE" if src_ntsc else "FALSE"
        ET.SubElement(fe, "name").text = name
        ET.SubElement(fe, "pathurl").text = _to_fcp7_pathurl(path) if path else f"file://localhost/{name}"
        ftc = ET.SubElement(fe, "timecode")
        ET.SubElement(ftc, "string").text = tc_info.timecode_string
        ET.SubElement(ftc, "displayformat").text = "DF" if src_ntsc else "NDF"
        ftcr = ET.SubElement(ftc, "rate")
        ET.SubElement(ftcr, "timebase").text = str(src_tb)
        ET.SubElement(ftcr, "ntsc").text = "TRUE" if src_ntsc else "FALSE"
        fmedia = ET.SubElement(fe, "media")
        if not for_audio_only:
            fv = ET.SubElement(fmedia, "video")
            ET.SubElement(fv, "duration").text = str(dur)
            fvsc = ET.SubElement(fv, "samplecharacteristics")
            ET.SubElement(fvsc, "width").text = str(sw)
            ET.SubElement(fvsc, "height").text = str(sh)
        fa = ET.SubElement(fmedia, "audio")
        ET.SubElement(fa, "channelcount").text = "2"
        return fe

    # ─── Helper: build DC-format filters ────────────────────────────
    def _add_video_filters(ci: ET.Element, dur: int, scale: float, rotation: float, speed: int):
        # Basic Motion
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","0"),("end", str(dur))]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Basic Motion"),("effectid","basic"),("effecttype","motion"),
                         ("mediatype","video"),("effectcategory","motion")]:
            ET.SubElement(eff, tag).text = val
        for pname, pid, pval, pmin, pmax in [
            ("Scale","scale", str(scale),"1","10000"),
            ("Center","center","","",""),
            ("Rotation","rotation", str(rotation),"-100000","100000"),
            ("Anchor Point","centerOffset","","","")]:
            param = ET.SubElement(eff, "parameter")
            ET.SubElement(param, "name").text = pname
            ET.SubElement(param, "parameterid").text = pid
            if pname in ("Center","Anchor Point"):
                cv = ET.SubElement(param, "value")
                ET.SubElement(cv, "horiz").text = "0"
                ET.SubElement(cv, "vert").text = "0"
            else:
                ET.SubElement(param, "value").text = pval
            if pmin:
                ET.SubElement(param, "valuemin").text = pmin
                ET.SubElement(param, "valuemax").text = pmax
        # Crop
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","0"),("end", str(dur))]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Crop"),("effectid","crop"),("effecttype","motion"),
                         ("mediatype","video"),("effectcategory","motion")]:
            ET.SubElement(eff, tag).text = val
        for pname in ("left","right","top","bottom"):
            param = ET.SubElement(eff, "parameter")
            ET.SubElement(param, "name").text = pname
            ET.SubElement(param, "parameterid").text = pname
            ET.SubElement(param, "value").text = "0"
            ET.SubElement(param, "valuemin").text = "0"
            ET.SubElement(param, "valuemax").text = "100"
        # Opacity
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","0"),("end", str(dur))]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Opacity"),("effectid","opacity"),("effecttype","motion"),
                         ("mediatype","video"),("effectcategory","motion")]:
            ET.SubElement(eff, tag).text = val
        param = ET.SubElement(eff, "parameter")
        ET.SubElement(param, "name").text = "opacity"
        ET.SubElement(param, "parameterid").text = "opacity"
        ET.SubElement(param, "value").text = "100"
        ET.SubElement(param, "valuemin").text = "0"
        ET.SubElement(param, "valuemax").text = "100"
        # Time Remap
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","-1"),("end","-1")]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Time Remap"),("effectid","timeremap"),("effecttype","motion"),
                         ("mediatype","video"),("effectcategory","motion")]:
            ET.SubElement(eff, tag).text = val
        for pname, pid, pval, pmin, pmax in [
            ("speed","speed", str(speed),"-10000","10000"),
            ("reverse","reverse","FALSE","",""),
            ("frameblending","frameblending","FALSE","",""),
            ("variablespeed","variablespeed","0","0","1")]:
            param = ET.SubElement(eff, "parameter")
            ET.SubElement(param, "name").text = pname
            ET.SubElement(param, "parameterid").text = pid
            ET.SubElement(param, "value").text = pval
            if pmin:
                ET.SubElement(param, "valuemin").text = pmin
                ET.SubElement(param, "valuemax").text = pmax

    def _add_audio_filters(ci: ET.Element, dur: int, speed: int):
        # Time Remap (if speed != 100)
        if speed != 100:
            filt = ET.SubElement(ci, "filter")
            for tag, val in [("enabled","TRUE"),("start","-1"),("end","-1")]:
                ET.SubElement(filt, tag).text = val
            eff = ET.SubElement(filt, "effect")
            for tag, val in [("name","Time Remap"),("effectid","timeremap"),("effecttype","motion"),
                             ("mediatype","audio"),("effectcategory","motion")]:
                ET.SubElement(eff, tag).text = val
            for pname, pid, pval, pmin, pmax in [
                ("speed","speed", str(speed),"-10000","10000"),
                ("reverse","reverse","FALSE","",""),
                ("frameblending","frameblending","FALSE","",""),
                ("variablespeed","variablespeed","0","0","1")]:
                param = ET.SubElement(eff, "parameter")
                ET.SubElement(param, "name").text = pname
                ET.SubElement(param, "parameterid").text = pid
                ET.SubElement(param, "value").text = pval
                if pmin:
                    ET.SubElement(param, "valuemin").text = pmin
                    ET.SubElement(param, "valuemax").text = pmax
        # Audio Levels
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","0"),("end", str(dur))]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Audio Levels"),("effectid","audiolevels"),
                         ("effecttype","audiolevels"),("mediatype","audio"),
                         ("effectcategory","audiolevels")]:
            ET.SubElement(eff, tag).text = val
        param = ET.SubElement(eff, "parameter")
        ET.SubElement(param, "name").text = "Level"
        ET.SubElement(param, "parameterid").text = "level"
        ET.SubElement(param, "value").text = "1"
        ET.SubElement(param, "valuemin").text = "0"
        ET.SubElement(param, "valuemax").text = "3.98109"
        # Audio Pan
        filt = ET.SubElement(ci, "filter")
        for tag, val in [("enabled","TRUE"),("start","0"),("end", str(dur))]:
            ET.SubElement(filt, tag).text = val
        eff = ET.SubElement(filt, "effect")
        for tag, val in [("name","Audio Pan"),("effectid","audiopan"),
                         ("effecttype","audiopan"),("mediatype","audio"),
                         ("effectcategory","audiopan")]:
            ET.SubElement(eff, tag).text = val
        param = ET.SubElement(eff, "parameter")
        ET.SubElement(param, "name").text = "Pan"
        ET.SubElement(param, "parameterid").text = "pan"
        ET.SubElement(param, "value").text = "0"
        ET.SubElement(param, "valuemin").text = "-1"
        ET.SubElement(param, "valuemax").text = "1"

    # ─── Helper: build DC-format transition ─────────────────────────
    def _build_transition(start_frame: int, end_frame: int) -> ET.Element:
        """Build a DC-format Cross Dissolve transition item."""
        ti = ET.Element("transitionitem")
        tr = ET.SubElement(ti, "rate")
        ET.SubElement(tr, "timebase").text = str(timebase)
        ET.SubElement(tr, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
        ET.SubElement(ti, "start").text = str(start_frame)
        ET.SubElement(ti, "end").text = str(end_frame)
        ET.SubElement(ti, "alignment").text = "center"
        eff = ET.SubElement(ti, "effect")
        for tag, val in [
            ("name", "Cross Dissolve"), ("effectid", "Cross Dissolve"),
            ("effecttype", "transition"), ("mediatype", "video"),
            ("effectcategory", "Dissolve"),
        ]:
            ET.SubElement(eff, tag).text = val
        for tag, val in [("startratio", "0"), ("endratio", "1"), ("reverse", "FALSE")]:
            ET.SubElement(eff, tag).text = val
        return ti

    # ═══════════════════════════════════════════════════════════════════
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
                if ti_section is None:
                    continue
                # Only create track if it has items (DC: no empty tracks)
                track_items = ti_section.findall("TrackItem")
                if not track_items:
                    continue
                fcp_track = ET.SubElement(video_section, "track")
                track_start = 0
                for vc_idx, ti_ref in enumerate(track_items, 1):
                    ti_el = idx.resolve_ref(ti_ref.get("ObjectRef", ""))
                    if ti_el is None:
                        continue
                    cti = ti_el.find("ClipTrackItem")
                    if cti is None:
                        continue
                    cl = _extract_clip(cti)
                    start = track_start  # override with contiguous track position
                    track_start = cl.end
                    total_frames = max(total_frames, cl.end)
                    clip_dur = cl.out_pt - cl.in_pt if cl.out_pt > cl.in_pt else cl.end - start
                    file_dur_val = cl.source_tc.full_duration_frames if cl.source_tc.full_duration_frames > 0 else clip_dur
                    file_fe = _build_file(cl.name, cl.media_path, file_dur_val, cl.source_tc, cl.source_w, cl.source_h)

                    ci = ET.SubElement(fcp_track, "clipitem")
                    ci_id = _next_ci_id(cl.name)
                    ci.set("id", ci_id)
                    _link_groups.setdefault(cl.name, []).append((ci_id, "video", vt_idx, vc_idx))

                    ET.SubElement(ci, "name").text = cl.name
                    ET.SubElement(ci, "duration").text = str(clip_dur)
                    cir = ET.SubElement(ci, "rate")
                    ET.SubElement(cir, "timebase").text = str(timebase)
                    ET.SubElement(cir, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
                    ET.SubElement(ci, "start").text = str(start)
                    ET.SubElement(ci, "end").text = str(cl.end)
                    ET.SubElement(ci, "enabled").text = "TRUE"
                    ET.SubElement(ci, "in").text = str(cl.in_pt)
                    ET.SubElement(ci, "out").text = str(cl.out_pt)
                    ci.append(file_fe)
                    ET.SubElement(ci, "compositemode").text = "normal"
                    _add_video_filters(ci, clip_dur, cl.scale, cl.rotation, cl.playback_speed)
                    ET.SubElement(ci, "comments")

                # Insert transitions between adjacent clips on this track
                clipitems_in_track = fcp_track.findall("clipitem")
                if len(clipitems_in_track) >= 2:
                    overlap = timebase  # 1 second default transition
                    for j in range(len(clipitems_in_track) - 1):
                        ci_a = clipitems_in_track[j]
                        ci_b = clipitems_in_track[j + 1]
                        end_a = int(ci_a.findtext("end", "0"))
                        # Transition overlaps: last N frames of A, first N frames of B
                        trans_start = max(0, end_a - overlap)
                        trans_end = end_a + overlap
                        tr = _build_transition(trans_start, trans_end)
                        # Insert at position of ci_b (before it in the track)
                        insert_idx = list(fcp_track).index(ci_b)
                        fcp_track.insert(insert_idx, tr)

                ET.SubElement(fcp_track, "enabled").text = "TRUE"
                ET.SubElement(fcp_track, "locked").text = "FALSE"

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
                if a_ti_section is None:
                    continue
                # Only create track if it has items (DC: no empty tracks)
                a_track_items = a_ti_section.findall("TrackItem")
                if not a_track_items:
                    continue
                fcp_a_track = ET.SubElement(audio_section, "track")
                a_track_start = 0
                for ac_idx, a_ti_ref in enumerate(a_track_items, 1):
                    a_ti_el = idx.resolve_ref(a_ti_ref.get("ObjectRef", ""))
                    if a_ti_el is None:
                        continue
                    a_cti = a_ti_el.find("ClipTrackItem")
                    if a_cti is None:
                        continue
                    cl = _extract_clip(a_cti)
                    start = a_track_start
                    a_track_start = cl.end
                    total_frames = max(total_frames, cl.end)
                    clip_dur = cl.out_pt - cl.in_pt if cl.out_pt > cl.in_pt else cl.end - start

                    a_ci = ET.SubElement(fcp_a_track, "clipitem")
                    ci_id = _next_ci_id(cl.name)
                    a_ci.set("id", ci_id)
                    _link_groups.setdefault(cl.name, []).append((ci_id, "audio", at_idx, ac_idx))

                    ET.SubElement(a_ci, "name").text = cl.name
                    ET.SubElement(a_ci, "duration").text = str(clip_dur)
                    cir = ET.SubElement(a_ci, "rate")
                    ET.SubElement(cir, "timebase").text = str(timebase)
                    ET.SubElement(cir, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
                    ET.SubElement(a_ci, "start").text = str(start)
                    ET.SubElement(a_ci, "end").text = str(cl.end)
                    ET.SubElement(a_ci, "enabled").text = "TRUE"
                    ET.SubElement(a_ci, "in").text = str(cl.in_pt)
                    ET.SubElement(a_ci, "out").text = str(cl.out_pt)

                    # File: shared ref or full standalone
                    vid_fid = _file_ids.get(cl.name)
                    if vid_fid:
                        a_file = ET.SubElement(a_ci, "file")
                        a_file.set("id", vid_fid)
                    else:
                        file_dur_val = cl.source_tc.full_duration_frames if cl.source_tc.full_duration_frames > 0 else clip_dur
                        a_file = _build_file(cl.name, cl.media_path, file_dur_val, cl.source_tc, cl.source_w, cl.source_h, for_audio_only=True)
                        a_ci.append(a_file)

                    # Sourcetrack (audio only — video clipitems in DC format have none)
                    st_el = ET.SubElement(a_ci, "sourcetrack")
                    ET.SubElement(st_el, "mediatype").text = "audio"
                    ET.SubElement(st_el, "trackindex").text = "1"

                    _add_audio_filters(a_ci, clip_dur, cl.playback_speed)
                    ET.SubElement(a_ci, "comments")

                ET.SubElement(fcp_a_track, "enabled").text = "TRUE"
                ET.SubElement(fcp_a_track, "locked").text = "FALSE"

    # ═══════════════════════════════════════════════════════════════════
    # PASS 3: Link elements (DC convention: linkclipref + mediatype for audio)
    # ═══════════════════════════════════════════════════════════════════
    for name, members in _link_groups.items():
        if len(members) < 2:
            continue
        # Find the first video member (for mediatype marking on audio links)
        first_video_ref = None
        for ref_id, mtype, _, _ in members:
            if mtype == "video":
                first_video_ref = ref_id
                break
        links: list[ET.Element] = []
        for ref_id, _, _, _ in members:
            link = ET.Element("link")
            ET.SubElement(link, "linkclipref").text = ref_id
            links.append(link)
        for ref_id, mtype, _, _ in members:
            ci = None
            if mtype == "video":
                ci = video_section.find(f".//clipitem[@id='{ref_id}']")
            else:
                ci = audio_section.find(f".//clipitem[@id='{ref_id}']")
            if ci is None:
                continue
            for old in ci.findall("link"):
                ci.remove(old)
            # Insert links AFTER filters (DC convention: filter→link→comments)
            ins_pos = len(list(ci))
            for i, child in enumerate(ci):
                if child.tag == "comments":
                    ins_pos = i
                    break
            for j, link in enumerate(links):
                # Always deepcopy — XML Element can only have ONE parent
                link_copy = copy.deepcopy(link)
                # DC: first link in audio clipitem gets <mediatype>video</mediatype>
                if mtype == "audio" and j == 0 and first_video_ref is not None:
                    mt = ET.Element("mediatype")
                    mt.text = "video"
                    link_copy.insert(1, mt)
                ci.insert(ins_pos, link_copy)
                ins_pos += 1

    # ─── Finalize ────────────────────────────────────────────────────
    dur_elem.text = str(total_frames if total_frames > 0 else 0)

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

        # Force timeline start timecode to 00:00:00:00
        # (DaVinci defaults to 01:00:00:00 regardless of XML sequence timecode)
        try:
            timeline.SetSetting("timelineStartTimecode", "00:00:00:00")
        except Exception:
            pass  # best-effort, silently ignore API limitations

        drt_path = output_dir / f"{seq_name}_resolve.drt"
        # Dedup: append -1, -2, … if file already exists
        if drt_path.exists():
            for _n in range(1, 100):
                candidate = output_dir / f"{seq_name}_resolve-{_n}.drt"
                if not candidate.exists():
                    drt_path = candidate
                    break
        if timeline.Export(str(drt_path), resolve.EXPORT_DRT):
            print(f"    DRT: {drt_path.name}")
            results.append((True, drt_path))
            _recycle(xml_path)
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

    # ── Step 1: Extract and import individual media files ──
    all_files: set[str] = set()
    for xml_path in xml_paths:
        all_files.update(_extract_media_files(xml_path))
    if all_files:
        imported = _import_media_to_pool(media_pool, sorted(all_files))
        print(f"  Media pool: {imported}/{len(all_files)} files imported")
    else:
        print("  Media pool: no local media found (offline mode)")

    # ── Step 2: Recreate bin structure from PR project ──
    if prproj_root is not None:
        bins = _prproj_get_bin_structure(prproj_root)
        if bins:
            _create_bin_structure(media_pool, bins)
            print(f"  Bins: {len(bins)} folder(s) created")

    # ── Step 3: Import timelines ──
    # importSourceClips=False — media already in pool from Step 1
    # True causes failure when some pathurls are unreachable
    for xml_path, seq_name in zip(xml_paths, sequence_names):
        timeline = media_pool.ImportTimelineFromFile(
            str(xml_path),
            {"timelineName": seq_name, "importSourceClips": False},
        )
        if timeline is not None:
            print(f"  Timeline: {timeline.GetName()}")
            try:
                timeline.SetSetting("timelineStartTimecode", "00:00:00:00")
            except Exception:
                pass
        else:
            print(f"  Timeline import FAILED: {seq_name}")

    # ── Step 4: Export DRP ──
    try:
        pm.SaveProject()
    except Exception:
        pass
    drp_result = False
    try:
        drp_result = pm.ExportProject(final_name, str(output_path), False)
    except Exception as e:
        print(f"  DRP export error: {e}")

    if drp_result:
        print(f"  DRP exported: {output_path}")
    else:
        print(f"  DRP export via API failed (project may still be in database).")
        print(f"  Project \"{final_name}\" created in DaVinci database.")
        print(f"  To export manually: File -> Export Project -> {output_path.name}")

    # ── Step 5: Cleanup based on mode ──
    if gui:
        print(f"  Project \"{final_name}\" ready in DaVinci Resolve.")
    else:
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
    from urllib.parse import unquote
    seen: set[str] = set()
    result: list[str] = []
    try:
        tree = ET.parse(str(xml_path))
        for pu in tree.iter("pathurl"):
            url = pu.text or ""
            local = ""
            if url.startswith("file://localhost/"):
                local = unquote(url[len("file://localhost/"):]).replace("/", "\\")
            elif url.startswith("file:///"):
                local = unquote(url[8:]).replace("/", "\\")
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
        from urllib.parse import unquote
        for pu in tree.iter("pathurl"):
            url = pu.text or ""
            if url.startswith("file://localhost/"):
                local_path = unquote(url[len("file://localhost/"):]).replace("/", "\\")
                if Path(local_path).exists():
                    has_local_media = True
            elif url.startswith("file:///"):
                local_path = url[8:].replace("/", "\\")
                if Path(local_path).exists():
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


def _drt_supplement_lumetri(
    resolve: Any,
    lumetri_data: dict[str, dict[str, float]],
) -> bool:
    """Supplement DaVinci timeline with Lumetri color data from .prproj.

    Maps PR Lumetri parameters to DaVinci Color Corrector nodes.

    Args:
        resolve: The DaVinci Resolve object
        lumetri_data: Dict mapping clip name -> {param_name: value}

    Returns:
        True if at least one clip was updated
    """
    # Lumetri -> DaVinci Color parameter mapping
    _LUMETRI_TO_DAVINCI: dict[str, str] = {
        "曝光": "Exposure",    # Exposure (DaVinci Color Page Exposure)
        "对比度": "Contrast",
        "高光": "Highlights",
        "阴影": "Shadows",
        "白色": "Gain",        # Whites -> Gain wheel
        "黑色": "Lift",        # Blacks -> Lift wheel
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

        if updated > 0:
            print(f"  Lumetri data applied to {updated} parameters")
        else:
            print("  (Lumetri skipped: skeleton import, no source media to attach Color nodes)")
        return updated > 0

    except Exception as e:
        print(f"  Error supplementing Lumetri: {e}")
        return False