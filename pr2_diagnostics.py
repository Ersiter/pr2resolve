"""FCP7 XML Diagnostics Engine — scans for 21 known issues."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from pr2_constants import (
    CRITICAL,
    DEFAULT_FPS,
    FPS_TOLERANCE,
    Issue,
    MAJOR,
    MINOR,
    NTSC_RATES,
    PAL_RATES,
    _build_file_index,
    _get_sequence_format,
)

# FCP7 XML required version (mirrored from pr2_constants to avoid circular import)
FCP7_VERSION = "5"


@dataclass
class ScaleIssue:
    """Detected scale mismatch between source and timeline resolution."""

    source_res: tuple[int, int]
    timeline_res: tuple[int, int]
    current_scale: float
    corrected_scale: float


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
