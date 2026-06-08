"""Output module — XML writer, fix report generator, and root cause hints."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.dom import minidom

from pr2_constants import CRITICAL, MAJOR, MINOR, FCP7_DOCTYPE, VERSION, Issue


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
        "C5": "PR on Windows writes file://localhost/ instead of file:///",
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
    }
    return hints.get(issue.rule_id, "See description")
