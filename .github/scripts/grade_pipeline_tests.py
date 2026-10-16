"""Check that native clips and the API consume the same ordered DRX graph."""

from __future__ import annotations

import base64
import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as engine
import pr2_native as native
from conversion_tests import sequence
from grade_tests import _sample_project
from pr2_native_grade import extract_drx_body


def _ordered_project() -> ET.Element:
    project = _sample_project()
    ET.SubElement(project.find("VideoTrackGroup/TrackGroup"), "FrameRate").text = "10160640000"
    tracks = project.find("VideoTrackGroup/TrackGroup/Tracks")
    tracks.insert(0, ET.Element("Track", ObjectURef="empty"))
    ET.SubElement(ET.SubElement(project, "VideoClipTrack", ObjectUID="empty"), "ClipTrack")
    project.find(".//ClipTrackItem/TrackItem/Start").text = "0"
    project.find(".//ClipTrackItem/TrackItem/End").text = "254016000000"
    for raw in project.findall(".//StartKeyframeValue"):
        blob = ET.fromstring(base64.b64decode(raw.text))
        for keyframe in blob.findall("keyframe")[1:]:
            blob.remove(keyframe)
        raw.text = base64.b64encode(ET.tostring(blob)).decode("ascii")
    project.find(".//Param[@ObjectID='p11b']/IsTimeVarying").text = "false"
    return project


class GradePipelineTests(unittest.TestCase):
    def test_native_and_api_reuse_full_graph_with_empty_source_track_and_start_offset(self) -> None:
        grades = engine._prproj_extract_all_lumetri(_ordered_project(), "seq")
        self.assertEqual(len(grades), 1)
        grade = grades[0]
        self.assertEqual((grade.track_index, grade.start_frame, grade.end_frame, grade.node_count), (2, 0, 25, 2))
        self.assertEqual([r.component_ref for r in grade.parameter_records if r.parameter_id == "11"], ["first", "second"])
        root = sequence()
        video = root.find("sequence/media/video")
        video.insert(list(video).index(video.find("track")), ET.Element("track"))
        clip = video.find("track/clipitem")
        clip.find("end").text = "25"
        clip.find("name").text = grade.clip_name
        captured: list[bytes] = []
        item, timeline = Mock(), Mock()
        timeline.GetStartFrame.return_value = 90000
        timeline.GetItemListInTrack.return_value = [item]
        item.GetStart.return_value, item.GetEnd.return_value = 90000, 90025

        def apply_grade(path: str, mode: int) -> bool:
            self.assertEqual(mode, 0)
            captured.append(Path(path).read_bytes())
            return True

        item.GetNodeGraph.return_value.ApplyGradeFromDRX.side_effect = apply_grade
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            path = native.export_native_drp(root, Path(temp) / "grade.drp", "Grade", grades)
            result = engine._apply_lumetri_grade(timeline, grades)
            with zipfile.ZipFile(path) as archive:
                name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                written = ET.fromstring(archive.read(name).replace(b"ListMgt::", b"ListMgt__"))
        target = written.find("VideoTrackVec/Element[2]/Sm2TiTrack/Items/Element/Sm2TiVideoClip")
        self.assertIsNotNone(target)
        body = target.findtext(".//Body")
        self.assertEqual(bytes.fromhex(body), extract_drx_body(grade.drx_payload))
        self.assertEqual(captured, [grade.drx_payload])
        self.assertEqual(target.findtext(".//HasCorrection"), "true")
        self.assertEqual(result, {"applied": 1, "partial": 1, "skipped": 0, "failed": 0})
        timeline.GetItemListInTrack.assert_called_once_with("video", 2)

    def test_rejected_api_grade_is_counted_as_failed(self) -> None:
        grades = engine._prproj_extract_all_lumetri(_ordered_project(), "seq")
        timeline, item = Mock(), Mock()
        timeline.GetStartFrame.return_value = 0
        timeline.GetItemListInTrack.return_value = [item]
        item.GetStart.return_value, item.GetEnd.return_value = 0, 25
        item.GetNodeGraph.return_value.ApplyGradeFromDRX.return_value = False
        with contextlib.redirect_stdout(io.StringIO()):
            result = engine._apply_lumetri_grade(timeline, grades)
        self.assertEqual((result["applied"], result["failed"]), (0, 1))

    def test_drt_exports_do_not_report_success_after_grade_rejection(self) -> None:
        grades = engine._prproj_extract_all_lumetri(_ordered_project(), "seq")
        for batch in (False, True):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                xml = folder / "source.xml"
                ET.ElementTree(sequence()).write(xml, encoding="utf-8")
                resolve = Mock()
                manager = resolve.GetProjectManager.return_value
                manager.GetCurrentProject.return_value = None
                project = manager.CreateProject.return_value
                project.SetSetting.return_value = True
                timeline = project.GetMediaPool.return_value.ImportTimelineFromFile.return_value
                timeline.GetStartFrame.return_value = 0
                timeline.GetItemListInTrack.return_value = []
                with patch.object(engine, "_strip_file_elements_for_drt", return_value=(xml, False)), \
                     patch.object(engine, "_recycle") as recycle, contextlib.redirect_stdout(io.StringIO()):
                    if batch:
                        result = engine._drt_batch_export(resolve, [xml], folder, ["Grade"], [grades])
                        self.assertEqual(result, [(False, None)])
                    else:
                        self.assertFalse(engine._drt_sandbox_export(resolve, xml, folder / "out.drt", lumetri_grades=grades))
                timeline.Export.assert_not_called()
                recycle.assert_not_called()
                self.assertTrue(xml.is_file())


if __name__ == "__main__":
    unittest.main()
