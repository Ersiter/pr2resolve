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

VERSION = "1.0.0"
DEFAULT_FPS = 30.0
MICROSECOND = 1_000_000
NTSC_RATES: list[float] = [23.976, 29.97, 59.94, 47.952]
PAL_RATES: list[float] = [25.0, 50.0]
FPS_TOLERANCE: float = 0.01
FCP7_VERSION = "5"
FCP7_DOCTYPE = '<!DOCTYPE xmeml>'
CRITICAL = "CRITICAL"
MAJOR = "MAJOR"
MINOR = "MINOR"


def _is_ntsc(timebase: float) -> bool:
    """Check if a timebase value is an NTSC indicator (legacy fallback)."""
    return timebase in [24, 30, 60]


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
# Clipitem element order per FCP7 spec
# ═══════════════════════════════════════════════════════════════════════════════

FCP7_CLIPIITEM_ORDER: list[str] = [
    "masterclipid", "name", "enabled", "duration", "rate", "start", "end",
    "in", "out", "alphatype", "pixelaspectratio", "anamorphic", "file",
    "sourcetrack", "filter", "logginginfo", "colorinfo", "labels", "link",
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
