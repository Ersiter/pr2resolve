#!/usr/bin/env python3
"""find_pr_xml — Scan a directory for Premiere Pro XML exports and .prproj files.

Usage:
    python find_pr_xml.py [directory]
    python find_pr_xml.py .              # scan current directory
    python find_pr_xml.py D:\\Projects    # scan specific directory
"""

from __future__ import annotations

import sys
import gzip
import xml.etree.ElementTree as ET
from pathlib import Path


def _scan_directory(directory: Path) -> list[dict]:
    """Scan a directory for PR XML exports and .prproj files.

    Args:
        directory: Path to scan

    Returns:
        List of dicts with keys: path, type, name, resolution, clips, issues
    """
    results = []

    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue

        entry = {
            "path": path,
            "type": "",
            "name": "",
            "resolution": "",
            "clips": 0,
            "issues": [],
        }

        try:
            if path.suffix.lower() == ".xml":
                _analyze_xml(path, entry)
            elif path.suffix.lower() == ".prproj":
                _analyze_prproj(path, entry)
            else:
                continue
        except Exception as e:
            entry["issues"].append(f"Parse error: {e}")
            entry["type"] = "error"

        results.append(entry)

    return results


def _analyze_xml(path: Path, entry: dict) -> None:
    """Analyze an FCP7 XML file.

    Args:
        path: Path to the XML file
        entry: Dict to populate with analysis results
    """
    tree = ET.parse(str(path))
    root = tree.getroot()

    if root.tag != "xmeml":
        entry["type"] = "unknown"
        entry["issues"].append("Not an FCP7 XML (root is not <xmeml>)")
        return

    entry["type"] = "FCP7 XML"

    version = root.get("version", "?")
    if version != "5":
        entry["issues"].append(f'xmeml version="{version}" (should be "5")')

    seq = root.find("sequence")
    if seq is None:
        entry["issues"].append("Missing <sequence>")
        return

    entry["name"] = seq.findtext("name", "(unnamed)")

    # Resolution
    w = seq.findtext("media/video/format/samplecharacteristics/width")
    h = seq.findtext("media/video/format/samplecharacteristics/height")
    if w and h:
        entry["resolution"] = f"{w}x{h}"

    # Clip count
    clips = list(root.iter("clipitem"))
    entry["clips"] = len(clips)

    # Quick checks
    for ci in clips:
        if ci.find("filter/effect/[effectid='Lumetri']") is not None:
            entry["issues"].append("Contains Lumetri filters")
            break

    for pu in root.iter("pathurl"):
        url = pu.text or ""
        if url and not url.startswith("file:///"):
            entry["issues"].append("Non-standard pathurl format")
            break


def _analyze_prproj(path: Path, entry: dict) -> None:
    """Analyze a .prproj file.

    Args:
        path: Path to the .prproj file
        entry: Dict to populate with analysis results
    """
    with gzip.open(str(path), "rb") as f:
        root = ET.fromstring(f.read())

    if root.tag != "PremiereData":
        entry["type"] = "unknown"
        return

    entry["type"] = ".prproj"

    # Find sequences
    sequences = root.findall("Sequence")
    if not sequences:
        entry["issues"].append("No sequences found")
        return

    # Use first sequence
    seq = sequences[0]
    entry["name"] = seq.findtext("Name", "(unnamed)")

    # Resolution from properties
    props = seq.find(".//Properties")
    if props is not None:
        w = h = ""
        for p in props:
            if "PreviewFrameSizeWidth" in p.tag:
                w = p.text or ""
            if "PreviewFrameSizeHeight" in p.tag:
                h = p.text or ""
        if w and h:
            entry["resolution"] = f"{w}x{h}"

    # Count VideoClipTrackItems
    count = 0
    for el in root:
        if el.tag == "VideoClipTrackItem":
            count += 1
    entry["clips"] = count

    # Count Lumetri filters
    lum_count = sum(1 for el in root if el.tag == "VideoFilterComponent")
    if lum_count > 0:
        entry["issues"].append(f"{lum_count} Lumetri filter components")


def main() -> int:
    """Main entry point."""
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")

    if not directory.is_dir():
        print(f"Error: Not a directory: {directory}")
        return 1

    print(f"Scanning: {directory}")
    print("=" * 70)

    results = _scan_directory(directory)

    if not results:
        print("  No PR XML or .prproj files found.")
        return 0

    for entry in results:
        issues_str = ""
        if entry["issues"]:
            issues_str = f"  [{'; '.join(entry['issues'][:3])}]"

        print(
            f"  {entry['type']:10s}  "
            f"{entry['resolution']:>10s}  "
            f"{entry['clips']:>3d} clips  "
            f"{entry['name']}{issues_str}"
        )
        print(f"             {entry['path']}")

    print()
    print(f"Found {len(results)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
