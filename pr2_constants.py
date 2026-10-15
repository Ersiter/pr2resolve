"""pr2resolve shared constants and data models.

This module has NO internal dependencies. All other modules import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "1.6.0"
DEFAULT_FPS = 30.0
MICROSECOND = 1_000_000
NTSC_RATES: list[float] = [23.976, 29.97, 59.94, 47.952]
PAL_RATES: list[float] = [25.0, 50.0]
FPS_TOLERANCE: float = 0.01
OUTPUT_SUFFIX = "_pr2resolve"
FCP7_VERSION = "5"
FCP7_DOCTYPE = '<!DOCTYPE xmeml>'
CRITICAL = "CRITICAL"
MAJOR = "MAJOR"
MINOR = "MINOR"
LUMETRI_POINTWISE_IDS = frozenset({
    "7", "8", "11", "12", "13", "14", "15", "16", "20", "28", "30", "31",
    "32", "33", "39", "42", "47", "63", "64", "65", "66", "85", "86", "87",
    "88", "89", "90", "106", "108", "110", "112",
})
LUMETRI_SPATIAL_IDS = frozenset({"29", "51", "52", "53", "54", "81", "82"})
PP_LABEL_TO_RESOLVE_COLOR = {
    0: 15, 1: 13, 2: 11, 3: 5, 4: 12, 5: 9, 6: 8, 7: 10,
    8: 4, 9: 3, 10: 2, 11: 14, 12: 6, 13: 1, 14: 7, 15: 16,
}
RESOLVE_CLIP_COLORS = {
    1: "Green", 2: "Teal", 3: "Blue", 4: "Purple", 5: "Pink",
    6: "Tan", 7: "Brown", 8: "Apricot", 9: "Beige", 10: "Chocolate",
    11: "Lime", 12: "Navy", 13: "Olive", 14: "Orange", 15: "Violet",
    16: "Yellow",
}


def _is_ntsc(timebase: float) -> bool:
    """Check if a timebase value is an NTSC indicator (legacy fallback)."""
    # 24 is NOT an NTSC indicator — cinema 24.000fps uses timebase 24.
    # Only 30 and 60 are common NTSC timebases.
    return timebase in [30, 60]


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
class _SourceTCInfo:
    """Extracted source media timecode and frame rate info."""

    media_fps: float = 30.0               # actual source FPS (e.g. 59.94)
    media_fps_resolved: bool = False      # True if source FPS is known
    sample_rate: int = 0                  # audio sample rate when source is audio-only
    is_ntsc: bool = False                  # whether source rate is NTSC
    timecode_frame: int = 0                # timecode start in source-rate frames
    timecode_string: str = "00:00:00:00"   # formatted TC string
    full_duration_frames: int = 0          # full file duration in source-rate frames
    duration_resolved: bool = False        # True if source duration is known
    resolved: bool = False                 # True if real TC data was found


@dataclass
class ClipData:
    """Extracted clip data from a .prproj ClipTrackItem chain.

    Covers clip identity, timeline position, source trimming,
    playback speed, motion effects, and media file metadata.
    """

    name: str                             # SubClip→MasterClip→Name
    media_path: str = ""                  # full local path to media file
    media_id: str = ""                    # stable project Media ObjectUID
    start: int = 0                        # timeline start (ticks→frames)
    end: int = 0                          # timeline end (ticks→frames)
    in_pt: int = 0                        # source in-point (Clip→InPoint)
    out_pt: int = 0                       # source out-point (Clip→OutPoint)
    playback_speed: float = 100.0         # 100 = normal speed
    source_tc: Optional["_SourceTCInfo"] = None
    source_w: int = 0                     # Media→VideoStream→FrameRect width
    source_h: int = 0                     # Media→VideoStream→FrameRect height
    scale: float = 100.0                  # PR Motion Scale (StartKeyframe)
    rotation: float = 0.0                 # PR Motion Rotation (StartKeyframe)
    label_name: str = ""                  # Premiere label preset name, if present
    label_raw_color: str = ""              # Premiere color payload, encoding not assumed
    in_ticks: str = ""                    # unquantized Premiere source trim
    out_ticks: str = ""


@dataclass
class FileData:
    """Media file metadata for one <file> element in FCP7 XML.

    Decoupled from ElementTree.  Created once per unique media file
    in the parser pass, then referenced by audio clipitems sharing
    the same source media.
    """

    id: str                    # DC-format file id: "{name} {counter}"
    name: str                  # media filename (base name)
    path: str = ""             # local absolute file path
    duration: int = 0          # full media duration in frames
    timecode: Optional["_SourceTCInfo"] = None
    source_w: int = 0          # video width from source media
    source_h: int = 0          # video height from source media
    for_audio_only: bool = False


@dataclass
class ProjectBin:
    """One Premiere bin and the directly owned media or sequence identities."""

    uid: str
    name: str
    parent_uid: str | None = None
    media_paths: list[str] = field(default_factory=list)
    sequence_uids: list[str] = field(default_factory=list)


@dataclass
class LinkMember:
    """One clipitem in a link group (clips sharing the same source media)."""

    clipitem_id: str   # "{media_name} {counter}" — DC clipitem id
    mediatype: str     # "video" | "audio"
    track_index: int   # 1-based track index
    clip_index: int    # within-track clip index


@dataclass
class LinkGroup:
    """All clipitems sharing the same source media file."""

    media_name: str                        # group key (same as ClipData.name)
    members: list[LinkMember]              # all linked clipitems
    first_video_id: Optional[str] = None   # first video member's clipitem_id


@dataclass
class FilterParam:
    """One parameter inside a filter <effect> element — decoupled from ET."""

    name: str               # e.g. "Scale", "Level"
    parameterid: str        # e.g. "scale", "level"
    value: str              # pre-formatted string
    valuemin: str = ""
    valuemax: str = ""
    is_composite: bool = False  # True → value rendered as <horiz>/<vert>


@dataclass
class FilterSpec:
    """One <filter> element's complete content — decoupled from ET."""

    effect_id: str          # e.g. "basic", "crop", "timeremap"
    name: str               # e.g. "Basic Motion", "Crop"
    effect_type: str        # "motion" | "audiolevels" | "audiopan"
    media_type: str         # "video" | "audio"
    effect_category: str    # "motion" | "audiolevels" | "audiopan"
    start: str              # filter range start ("0" or "-1")
    end: str                # filter range end (str(dur) or "-1")
    params: list[FilterParam]


@dataclass
class LumetriGrade:
    """One shared DRX node graph identified by its source timeline clip."""

    track_index: int
    start_frame: int
    end_frame: int
    track_item_id: str
    clip_name: str
    drx_payload: bytes = b""
    node_count: int = 0
    skipped_reason: str = ""
    warnings: list[str] = field(default_factory=list)
    parameter_records: list["LumetriParameter"] = field(default_factory=list)


@dataclass
class LumetriParameter:
    """Lossless project record for one Lumetri parameter."""

    parameter_id: str
    name: str
    control_type: str
    raw_value: str
    lower_bound: str = ""
    upper_bound: str = ""
    encoding: str = "scalar"
    blob_hash: str = ""
    is_time_varying: bool = False
    conversion: str = "preserved"
    reason: str = ""
    start_keyframe: str = ""
    start_keyframe_position: str = ""
    start_keyframe_value: str = ""
    binary_hash: str = ""
    start_keyframe_encoding: str = ""
    start_value_encoding: str = ""
    decoded_sha256: str = ""
    component_ref: str = ""
    effect_order: int = 0
    raw_xml: str = ""


@dataclass
class TrackData:
    """One timeline track — decoupled from ET."""

    type: str             # "video" | "audio"
    index: int            # 1-based track index
    enabled: bool = True
    locked: bool = False
    muted: bool = False
    solo: bool = False
    name: str = ""


@dataclass
class TimelineMarkerData:
    """Marker attached to a sequence, track, or clip in source timeline."""

    name: str = ""
    comment: str = ""
    start_frame: int = 0
    end_frame: int = -1
    marker_type: str = "Comment"
    color_index: Optional[int] = None
    raw_payload: str = ""
    scope: str = "sequence"
    track_type: str = ""
    track_index: int = 0


@dataclass
class TimelineState:
    """Timeline metadata with explicit records for fields lacking a safe mapping."""

    sequence_uid: str = ""
    in_frame: Optional[int] = None
    out_frame: Optional[int] = None
    work_in_frame: Optional[int] = None
    work_out_frame: Optional[int] = None
    raw_in_ticks: str = ""
    raw_out_ticks: str = ""
    raw_work_in_ticks: str = ""
    raw_work_out_ticks: str = ""
    markers: list[TimelineMarkerData] = field(default_factory=list)
    unknown: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class TransitionData:
    """One cross-dissolve transition — decoupled from ET."""

    start_frame: int
    end_frame: int
    alignment: str = "center"
    effect_id: str = "Cross Dissolve"


@dataclass
class Issue:
    """A single diagnostics finding."""
    severity: str       # CRITICAL | MAJOR | MINOR
    rule_id: str        # e.g. "C1", "M7", "N1"
    message: str
    element: str        # xpath-like location
    fixed: bool = False


# ═══════════════════════════════════════════════════════════════════════════════
# Clipitem element order per FCP7 spec
# ═══════════════════════════════════════════════════════════════════════════════

FCP7_CLIPIITEM_ORDER: list[str] = [
    "name", "masterclipid", "duration", "rate", "start", "end",
    "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "file",
    "sourcetrack", "link", "filter", "logginginfo", "colorinfo", "labels",
    "comments", "itemhistory",
]

# Derived lookup for clipitem child ordering
_ORDER_MAP: dict[str, int] = {tag: i for i, tag in enumerate(FCP7_CLIPIITEM_ORDER)}


# ═══════════════════════════════════════════════════════════════════════════════
# Shared Helpers (no internal deps)
# ═══════════════════════════════════════════════════════════════════════════════

def _timeline_track_states(sequence: ET.Element) -> list[TrackData]:
    """Read the same track semantics for native and API output."""
    states: list[TrackData] = []
    for kind in ("video", "audio"):
        for index, track in enumerate(sequence.findall(f"media/{kind}/track"), 1):
            enabled = (track.findtext("enabled") or "TRUE").strip().upper() != "FALSE"
            states.append(TrackData(
                type=kind, index=index, enabled=enabled, muted=not enabled,
                locked=(track.findtext("locked") or "FALSE").strip().upper() == "TRUE",
                name=track.findtext("name") or "",
            ))
    return states


def _build_file_index(root: ET.Element) -> dict[str, ET.Element]:
    """Build mapping from file element id to the <file> element."""
    index: dict[str, ET.Element] = {}
    for file_elem in root.iter("file"):
        fid = file_elem.get("id")
        if fid and (fid not in index or file_elem.find("pathurl") is not None):
            index[fid] = file_elem
    return index


def _get_sequence_format(root: ET.Element) -> Optional[ET.Element]:
    """Get the sequence-level video format element."""
    seq = root.find("sequence")
    if seq is None:
        return None
    return seq.find("media/video/format")


def _get_sequence_resolution(root: ET.Element) -> tuple[int, int]:
    """Get the sequence (timeline) resolution."""
    fmt = _get_sequence_format(root)
    if fmt is None:
        return (0, 0)
    sc = fmt.find("samplecharacteristics")
    if sc is None:
        return (0, 0)
    w = int(sc.findtext("width") or "0")
    h = int(sc.findtext("height") or "0")
    return (w, h)


def load_xml(path: Path) -> ET.Element:
    """Parse an FCP7 XML file."""
    tree = ET.parse(str(path), parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    root = tree.getroot()
    if root.tag != "xmeml":
        raise ValueError(f"Expected <xmeml> root, got <{root.tag}>")
    return root


def load_prproj(path: Path) -> ET.Element:
    """Parse a .prproj file (gzip-compressed XML)."""
    import gzip
    with gzip.open(str(path), "rb") as f:
        xml_bytes = f.read()
    root = ET.fromstring(xml_bytes)
    if root.tag != "PremiereData":
        raise ValueError(f"Expected <PremiereData> root, got <{root.tag}>")
    return root
