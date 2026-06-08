"""FCP7 XML Validator — 23 structural compliance checks."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from pr2_constants import (
    CRITICAL,
    FCP7_VERSION,
    MAJOR,
    MINOR,
    Issue,
    _build_file_index,
    _get_sequence_format,
)
from pr2_diagnostics import _is_ntsc_timebase


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
