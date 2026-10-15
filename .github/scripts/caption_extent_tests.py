"""Regress caption-derived sequence duration and timebase handling."""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr2_engine as engine
from caption_tests import _synthetic_formatted_text
from conversion_tests import motion_project
from pr2_captions import caption_records_from_xml
from pr2_constants import load_xml


_TICKS = 254_016_000_000


def project_with_caption(
    *, frame_rate_ticks: int, start_ticks: int, end_ticks: int, with_video: bool,
) -> ET.Element:
    if with_video:
        project = motion_project()
        sequence = project.find("Sequence")
    else:
        project = ET.Element("PremiereData")
        sequence = ET.SubElement(project, "Sequence", {"ObjectUID": "sequence"})
        ET.SubElement(sequence, "Name").text = "Caption only"
        ET.SubElement(sequence, "TrackGroups")
    group_pairs = sequence.find("TrackGroups")
    group_pair = ET.SubElement(group_pairs, "TrackGroup")
    ET.SubElement(group_pair, "Second", {"ObjectRef": "caption-group"})

    group = ET.SubElement(project, "DataTrackGroup", {"ObjectID": "caption-group"})
    track_group = ET.SubElement(group, "TrackGroup")
    ET.SubElement(track_group, "FrameRate").text = str(frame_rate_ticks)
    tracks = ET.SubElement(track_group, "Tracks")
    ET.SubElement(tracks, "Track", {"Index": "0", "ObjectURef": "caption-track"})
    track = ET.SubElement(project, "CaptionDataClipTrack", {"ObjectUID": "caption-track"})
    item_refs = ET.SubElement(track, "TrackItems")
    ET.SubElement(item_refs, "TrackItem", {"ObjectRef": "caption-item"})
    item = ET.SubElement(project, "CaptionDataClipTrackItem", {"ObjectID": "caption-item"})
    timing = ET.SubElement(
        ET.SubElement(ET.SubElement(item, "DataClipTrackItem"), "ClipTrackItem"),
        "TrackItem",
    )
    ET.SubElement(timing, "Start").text = str(start_ticks)
    ET.SubElement(timing, "End").text = str(end_ticks)
    block_vector = ET.SubElement(item, "BlockVector")
    ET.SubElement(block_vector, "BlockVectorItem", {"Index": "0", "ObjectRef": "caption-block"})
    payload, _ = _synthetic_formatted_text(("caption",))
    block = ET.SubElement(project, "Block", {"ObjectID": "caption-block"})
    text_data = ET.SubElement(block, "FormattedTextData", {"Encoding": "base64", "BinaryHash": "caption-hash"})
    text_data.text = base64.b64encode(payload).decode("ascii")
    return project


class CaptionExtentTests(unittest.TestCase):
    def parse(self, project: ET.Element) -> ET.Element:
        return engine._prproj_parse_sequence(project, "sequence", Path("caption.prproj"))

    def test_caption_only_sequence_uses_ntsc_caption_track_rate_and_extent(self) -> None:
        project = project_with_caption(
            frame_rate_ticks=10_594_584_000,
            start_ticks=0,
            end_ticks=30 * _TICKS,
            with_video=False,
        )
        root = self.parse(project)
        sequence = root.find("sequence")
        self.assertEqual((sequence.findtext("rate/timebase"), sequence.findtext("rate/ntsc")), ("24", "TRUE"))
        self.assertEqual(sequence.findtext("duration"), "719")
        self.assertEqual(len(caption_records_from_xml(root)), 1)
        self.assertEqual(engine._scan(root), [])

    def test_caption_extent_survives_shorter_video_media_tail_and_m3_fix(self) -> None:
        project = project_with_caption(
            frame_rate_ticks=10_160_640_000,
            start_ticks=2 * _TICKS,
            end_ticks=8 * _TICKS,
            with_video=True,
        )
        root = self.parse(project)
        sequence = root.find("sequence")
        self.assertEqual(sequence.findtext("rate/timebase"), "25")
        self.assertEqual(sequence.findtext("duration"), "200")
        records = caption_records_from_xml(root)
        self.assertEqual((records[0].start_ticks, records[0].end_ticks), (2 * _TICKS, 8 * _TICKS))

        sequence.find("duration").text = "25"
        issues = engine._scan(root)
        self.assertIn("M3", {issue.rule_id for issue in issues})
        engine._apply_fixes(root, issues)
        self.assertEqual(sequence.findtext("duration"), "200")
        self.assertNotIn("M3", {issue.rule_id for issue in engine._scan(root)})
        self.assertEqual((records[0].start_ticks, records[0].end_ticks), (2 * _TICKS, 8 * _TICKS))

    def test_source_caption_records_survive_xml_save_and_load(self) -> None:
        root = self.parse(project_with_caption(
            frame_rate_ticks=10_160_640_000,
            start_ticks=_TICKS,
            end_ticks=2 * _TICKS,
            with_video=False,
        ))
        before = caption_records_from_xml(root)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "caption.xml"
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            after = caption_records_from_xml(load_xml(path))
        self.assertEqual(after, before)

    def test_malformed_caption_state_is_rejected_instead_of_guessed(self) -> None:
        root = self.parse(project_with_caption(
            frame_rate_ticks=10_160_640_000,
            start_ticks=0,
            end_ticks=_TICKS,
            with_video=False,
        ))
        comment = next(node for node in root if node.tag is ET.Comment)
        comment.text = " pr2resolve-timeline-state:%%% "
        with self.assertRaisesRegex(ValueError, "invalid pr2resolve timeline-state caption data"):
            caption_records_from_xml(root)


if __name__ == "__main__":
    unittest.main()
