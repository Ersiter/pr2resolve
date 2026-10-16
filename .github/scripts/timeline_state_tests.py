"""Offline checks for Premiere timeline state extraction and Resolve application."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine
import pr2_native


class _TimelineItem:
    def __init__(self, name: str, start: int, duration: int) -> None:
        self.name = name
        self.start = start
        self.duration = duration
        self.color_calls: list[str] = []

    def GetName(self) -> str:
        return self.name

    def GetStart(self) -> int:
        return self.start

    def GetDuration(self) -> int:
        return self.duration

    def SetClipColor(self, color: str) -> bool:
        self.color_calls.append(color)
        return True


class _Timeline:
    def __init__(self) -> None:
        self.markers: dict[int, dict[str, str]] = {}
        self.add_marker_calls: list[tuple[int, str, str, str, int]] = []
        self.mark_calls: list[tuple[int, int, str]] = []
        self.clear_calls: list[str] = []
        self.start_frame = 0
        self.items: dict[tuple[str, int], list[_TimelineItem]] = {}
        self.add_marker_result = True
        self.mark_result = True

    def GetMarkers(self) -> dict[int, dict[str, str]]:
        return self.markers

    def AddMarker(self, frame: int, color: str, name: str, note: str, duration: int) -> bool:
        self.add_marker_calls.append((frame, color, name, note, duration))
        return self.add_marker_result

    def SetMarkInOut(self, mark_in: int, mark_out: int, mark_type: str) -> bool:
        self.mark_calls.append((mark_in, mark_out, mark_type))
        return self.mark_result

    def ClearMarkInOut(self, mark_type: str) -> bool:
        self.clear_calls.append(mark_type)
        return self.mark_result

    def GetStartFrame(self) -> int:
        return self.start_frame

    def GetItemListInTrack(self, track_type: str, track_index: int) -> list[_TimelineItem]:
        return self.items.get((track_type, track_index), [])


def _write_xml(sequence: ET.Element, opaque: dict[str, Any] | None = None) -> Path:
    root = ET.Element("xmeml")
    root.append(sequence)
    if opaque is not None:
        payload = base64.b64encode(json.dumps(opaque).encode("utf-8")).decode("ascii")
        root.append(ET.Comment(f" pr2resolve-timeline-state:{payload} "))
    handle = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)
    path = Path(handle.name)
    handle.close()
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


class TimelineStateTests(unittest.TestCase):
    def test_track_name_enable_and_lock_match_native_and_api(self) -> None:
        root = ET.fromstring('''<xmeml><sequence><name>Tracks</name><duration>25</duration>
            <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><media><video/>
            <audio/></media></sequence></xmeml>''')
        source = root.find("sequence")
        expected = [("Open", True, False, 0), ("Locked", True, True, 1),
                    ("Muted", False, False, 2), ("Both", False, True, 3)]
        for kind in ("video", "audio"):
            for name, enabled, locked, _ in expected:
                track = ET.SubElement(source.find(f"media/{kind}"), "track")
                ET.SubElement(track, "name").text = name
                ET.SubElement(track, "enabled").text = str(enabled).upper()
                ET.SubElement(track, "locked").text = str(locked).upper()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            xml = folder / "source.xml"
            ET.ElementTree(root).write(xml, encoding="utf-8")
            timeline = Mock()
            self.assertEqual(pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, xml), [])
            native_path = pr2_native.export_native_drp(root, folder / "native.drp", "Tracks")
            with zipfile.ZipFile(native_path) as archive:
                container_path = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                native = ET.fromstring(archive.read(container_path))
        for kind, vector in (("video", "VideoTrackVec"), ("audio", "AudioTrackVec")):
            tracks = native.findall(f"{vector}/Element/Sm2TiTrack")
            self.assertEqual(len(tracks), 4)
            for index, (name, enabled, locked, flags) in enumerate(expected, 1):
                with self.subTest(kind=kind, index=index):
                    track = tracks[index - 1]
                    self.assertEqual(track.findtext("UserDefinedName"), name)
                    self.assertIsNone(track.find("Name"))
                    self.assertEqual(int(track.findtext("Flags")), flags)
                    timeline.SetTrackName.assert_any_call(kind, index, name)
                    timeline.SetTrackEnable.assert_any_call(kind, index, enabled)
                    timeline.SetTrackLock.assert_any_call(kind, index, locked)

    def test_single_and_batch_drt_apply_state_before_export(self) -> None:
        for batch in (False, True):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                path = folder / "source.xml"
                path.write_text('''<xmeml><sequence><in>10</in><out>30</out>
                    <marker><name>Review</name><in>12</in><out>14</out>
                    <comment>Keep</comment></marker></sequence></xmeml>''', encoding="utf-8")
                resolve = Mock()
                manager = resolve.GetProjectManager.return_value
                manager.GetCurrentProject.return_value = None
                project = manager.CreateProject.return_value
                timeline = project.GetMediaPool.return_value.ImportTimelineFromFile.return_value
                timeline.GetMarkers.return_value = {}

                def export(*args: object) -> bool:
                    timeline.SetMarkInOut.assert_called_once_with(10, 29, "all")
                    timeline.AddMarker.assert_called_once_with(12, "Blue", "Review", "Keep", 2)
                    return True

                timeline.Export.side_effect = export
                with patch.object(pr2_engine, "_strip_file_elements_for_drt", return_value=(path, False)), \
                     patch.object(pr2_engine, "apply_timeline_state_to_archive", return_value=0), \
                     patch.object(pr2_engine, "_recycle"), contextlib.redirect_stdout(io.StringIO()):
                    if batch:
                        result = pr2_engine._drt_batch_export(resolve, [path], folder, ["Review"])
                        self.assertTrue(result[0][0])
                    else:
                        self.assertTrue(pr2_engine._drt_sandbox_export(resolve, path, folder / "review.drt"))
                self.assertTrue(path.is_file())

    def test_one_sided_sequence_mark_is_not_fabricated(self) -> None:
        sequence = ET.Element("sequence")
        ET.SubElement(sequence, "in").text = "-1"
        ET.SubElement(sequence, "out").text = "40"
        path = _write_xml(sequence)
        timeline = _Timeline()
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
        finally:
            path.unlink()
        self.assertEqual(timeline.mark_calls, [])
        self.assertEqual(timeline.clear_calls, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("exported package", warnings[0])

    def test_explicitly_unset_marks_clear_but_absent_marks_do_not(self) -> None:
        unset_sequence = ET.Element("sequence")
        ET.SubElement(unset_sequence, "in").text = "-1"
        ET.SubElement(unset_sequence, "out").text = "-1"
        unset_path = _write_xml(unset_sequence)
        unset_timeline = _Timeline()
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(unset_timeline, unset_path)
        finally:
            unset_path.unlink()
        self.assertEqual(unset_timeline.clear_calls, ["all"])
        self.assertEqual(warnings, [])

        absent_path = _write_xml(ET.Element("sequence"))
        absent_timeline = _Timeline()
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(absent_timeline, absent_path)
        finally:
            absent_path.unlink()
        self.assertEqual(absent_timeline.clear_calls, [])
        self.assertEqual(warnings, [])

    def test_explicit_marks_without_api_method_are_reported(self) -> None:
        class TimelineWithoutMarkApi:
            pass

        sequence = ET.Element("sequence")
        ET.SubElement(sequence, "in").text = "1"
        ET.SubElement(sequence, "out").text = "5"
        path = _write_xml(sequence)
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(TimelineWithoutMarkApi(), path)
        finally:
            path.unlink()
        self.assertTrue(any("cannot apply explicit" in warning.lower() for warning in warnings))

    def test_sequence_marker_without_resolve_api_is_reported(self) -> None:
        class TimelineWithoutMarkerApi:
            pass

        sequence = ET.Element("sequence")
        marker = ET.SubElement(sequence, "marker")
        ET.SubElement(marker, "name").text = "chapter"
        ET.SubElement(marker, "in").text = "12"
        path = _write_xml(sequence)
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(TimelineWithoutMarkerApi(), path)
        finally:
            path.unlink()
        self.assertTrue(any("cannot add sequence markers" in warning.lower() for warning in warnings))

    def test_unverified_premiere_marker_color_uses_blue_with_warning(self) -> None:
        sequence = ET.Element("sequence")
        marker = ET.SubElement(sequence, "marker")
        ET.SubElement(marker, "name").text = "chapter"
        ET.SubElement(marker, "in").text = "12"
        ET.SubElement(marker, "out").text = "14"
        timeline = _Timeline()
        opaque = {"markers": [{"scope": "sequence", "color_index": 5}]}
        path = _write_xml(sequence, opaque)
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
        finally:
            path.unlink()
        self.assertEqual(timeline.add_marker_calls[0][1], "Blue")
        self.assertTrue(any("mapping is unverified" in warning for warning in warnings))

    def test_rejected_mark_and_same_frame_markers_are_reported_once(self) -> None:
        sequence = ET.Element("sequence")
        ET.SubElement(sequence, "in").text = "10"
        ET.SubElement(sequence, "out").text = "30"
        for name in ("conflict", "second at same frame", "rejected"):
            marker = ET.SubElement(sequence, "marker")
            ET.SubElement(marker, "name").text = name
            ET.SubElement(marker, "in").text = "10" if name != "rejected" else "20"
            ET.SubElement(marker, "out").text = "12"
            ET.SubElement(marker, "comment").text = "note"
        path = _write_xml(sequence)
        timeline = _Timeline()
        timeline.markers = {10: {"name": "imported marker"}}
        timeline.mark_result = False
        timeline.add_marker_result = False
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
        finally:
            path.unlink()
        self.assertEqual(timeline.mark_calls, [(10, 29, "all")])
        self.assertEqual([call[0] for call in timeline.add_marker_calls], [20])
        self.assertEqual(len(warnings), 4)
        self.assertTrue(any("rejected sequence in/out" in warning for warning in warnings))
        self.assertTrue(any("already has a different marker" in warning for warning in warnings))
        self.assertTrue(any("rejected sequence marker" in warning for warning in warnings))

    def test_imported_marker_conflict_compares_note_and_duration(self) -> None:
        sequence = ET.fromstring('''<sequence><marker><name>Review</name><in>12</in>
            <out>14</out><comment>Keep</comment></marker></sequence>''')
        for note, duration, conflicts in (("Keep", 2, False), ("Other", 2, True), ("Keep", 8, True)):
            with self.subTest(note=note, duration=duration):
                path = _write_xml(sequence)
                timeline = _Timeline()
                timeline.markers = {12: {"name": "Review", "note": note, "duration": duration}}
                try:
                    warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
                finally:
                    path.unlink()
                self.assertEqual(timeline.add_marker_calls, [])
                self.assertEqual(bool(warnings), conflicts)

    def test_clip_label_match_uses_resolve_timeline_start_frame(self) -> None:
        sequence = ET.Element("sequence")
        item = _TimelineItem("shot.mov", 100_125, 40)
        timeline = _Timeline()
        timeline.start_frame = 100_000
        timeline.items[("video", 1)] = [item]
        opaque = {"unknown": [("clip-label:clip-1", json.dumps({
            "name": "shot.mov", "start": 125, "end": 165, "media_type": "video",
            "track_index": 1, "preset": "BE.Prefs.LabelColors.1",
        }))]}
        path = _write_xml(sequence, opaque)
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
        finally:
            path.unlink()
        self.assertEqual(len(item.color_calls), 1)
        self.assertEqual(warnings, [])

    def test_clip_label_is_skipped_when_timeline_start_is_unavailable(self) -> None:
        sequence = ET.Element("sequence")
        item = _TimelineItem("shot.mov", 125, 40)
        timeline = _Timeline()
        timeline.start_frame = None  # type: ignore[assignment]
        timeline.items[("video", 1)] = [item]
        opaque = {"unknown": [("clip-label:clip-1", json.dumps({
            "name": "shot.mov", "start": 125, "end": 165, "media_type": "video",
            "track_index": 1, "preset": "BE.Prefs.LabelColors.1",
        }))]}
        path = _write_xml(sequence, opaque)
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
        finally:
            path.unlink()
        self.assertEqual(item.color_calls, [])
        self.assertTrue(any("without the timeline start frame" in warning for warning in warnings))

    def test_clip_label_requires_one_destination_and_known_preset(self) -> None:
        for count, preset, message in ((0, "1", "0 items"), (2, "1", "2 items"), (1, "99", "Unmapped")):
            with self.subTest(count=count, preset=preset):
                timeline = _Timeline()
                timeline.items[("video", 1)] = [_TimelineItem("shot.mov", 125, 40) for _ in range(count)]
                opaque = {"unknown": [("clip-label:clip-1", json.dumps({
                    "name": "shot.mov", "start": 125, "end": 165, "media_type": "video",
                    "track_index": 1, "preset": preset,
                }))]}
                path = _write_xml(ET.Element("sequence"), opaque)
                try:
                    warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(timeline, path)
                finally:
                    path.unlink()
                self.assertTrue(any(message in warning for warning in warnings), warnings)
                self.assertTrue(all(not item.color_calls for item in timeline.items[("video", 1)]))

    def test_unapplied_track_marker_is_reported_by_api(self) -> None:
        path = _write_xml(ET.Element("sequence"), {"markers": [{"scope": "track", "raw": "source marker"}]})
        try:
            warnings = pr2_engine._apply_fcp_timeline_state_to_resolve(_Timeline(), path)
        finally:
            path.unlink()
        self.assertTrue(any("Track or clip markers" in warning for warning in warnings), warnings)

    def test_prproj_in_and_out_keep_unset_as_none(self) -> None:
        root = ET.fromstring("""<PremiereData>
          <Sequence ObjectUID="sequence-1"><Node><Properties>
            <MZ.InPoint>-1</MZ.InPoint><MZ.OutPoint>2540160000000</MZ.OutPoint>
          </Properties></Node></Sequence>
        </PremiereData>""")
        sequence = root.find("Sequence")
        assert sequence is not None
        state = pr2_engine._prproj_extract_timeline_state(sequence, pr2_engine._PrprojIndex.build(root), 24.0)
        self.assertIsNone(state.in_frame)
        self.assertEqual(state.out_frame, 240)
        self.assertEqual(state.raw_in_ticks, "-1")

    def test_prproj_sequence_marker_payload_is_extracted(self) -> None:
        root = ET.fromstring("""<PremiereData>
          <Sequence ObjectUID="sequence-1"><MarkerOwner><Markers ObjectRef="2" /></MarkerOwner></Sequence>
          <Markers ObjectID="2"><Marker><Second ObjectRef="3" /></Marker></Markers>
          <Marker ObjectID="3"><DVAMarker>{"DVAMarker":{"mStartTime":{"ticks":2540160000000},
            "mDuration":{"ticks":508032000000},"mName":"chapter","mComments":"note",
            "mType":"Comment","mColorIndex":5}}</DVAMarker></Marker>
        </PremiereData>""")
        sequence = root.find("Sequence")
        assert sequence is not None
        state = pr2_engine._prproj_extract_timeline_state(sequence, pr2_engine._PrprojIndex.build(root), 24.0)
        self.assertEqual(len(state.markers), 1)
        self.assertEqual(state.markers[0].start_frame, 240)
        self.assertEqual(state.markers[0].end_frame, 288)
        self.assertEqual(state.markers[0].color_index, 5)


if __name__ == "__main__":
    unittest.main()
