"""pr2resolve shared constants and data models.

This module has NO internal dependencies. All other modules import from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "0.9.6"
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
class ClipData:
    """Extracted clip data from a .prproj ClipTrackItem chain.

    Covers clip identity, timeline position, source trimming,
    playback speed, motion effects, and media file metadata.
    """

    name: str                             # SubClip→MasterClip→Name
    media_path: str = ""                  # full local path to media file
    start: int = 0                        # timeline start (ticks→frames)
    end: int = 0                          # timeline end (ticks→frames)
    in_pt: int = 0                        # source in-point (Clip→InPoint)
    out_pt: int = 0                       # source out-point (Clip→OutPoint)
    playback_speed: int = 100             # 100 = normal speed
    source_tc: Optional[object] = None    # _SourceTCInfo — timecode metadata
    source_w: int = 0                     # Media→VideoStream→FrameRect width
    source_h: int = 0                     # Media→VideoStream→FrameRect height
    scale: float = 100.0                  # PR Motion Scale (StartKeyframe)
    rotation: float = 0.0                 # PR Motion Rotation (StartKeyframe)


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
    timecode: Optional[object] = None  # _SourceTCInfo — timecode metadata
    source_w: int = 0          # video width from source media
    source_h: int = 0          # video height from source media
    for_audio_only: bool = False


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

    media_name: str               # group key (same as ClipData.name)
    members: list[LinkMember]     # all linked clipitems


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
class TrackData:
    """One timeline track — decoupled from ET."""

    type: str             # "video" | "audio"
    index: int            # 1-based track index
    enabled: bool = True
    locked: bool = False


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

def _build_file_index(root: ET.Element) -> dict[str, ET.Element]:
    """Build mapping from file element id to the <file> element."""
    index: dict[str, ET.Element] = {}
    for file_elem in root.iter("file"):
        fid = file_elem.get("id")
        if fid:
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
    tree = ET.parse(str(path))
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
