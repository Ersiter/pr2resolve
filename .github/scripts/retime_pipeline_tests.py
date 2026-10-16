"""Verify native retime output against source positions and timeline boundaries."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_native as native
from conversion_tests import sequence
import retime_curve_tests as curves
from retime_output_tests import decode_keyed_map, source_xml
from pr2_retime_output import apply_timeline_state_to_archive


class RetimePipelineTests(unittest.TestCase):
    def test_native_and_corrected_api_package_share_trim_and_complete_maps(self) -> None:
        self._compare_packages(still=False)

    def test_retimed_single_frame_still_uses_same_virtual_hold_in_both_packages(self) -> None:
        self._compare_packages(still=True)

    def _compare_packages(self, still: bool) -> None:
        root = ET.fromstring(source_xml())
        seq = root.find("sequence")
        ET.SubElement(seq, "duration").text = "4000"
        video = seq.find("media/video")
        video.insert(0, sequence().find("sequence/media/video/format"))
        for file in root.findall("file"):
            is_still = still and file.get("id") == "srcB"
            ET.SubElement(file, "name").text = file.get("id") + (".PNG" if is_still else ".mov")
            if is_still:
                file.find("duration").text = "1"
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(temp)
            xml = folder / "source.xml"
            ET.ElementTree(root).write(xml, encoding="utf-8")
            path = native.export_native_drp(root, folder / "native.drp", "Parity")
            with zipfile.ZipFile(path) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            name = next(name for name in members if name.startswith("SeqContainer/"))
            before = ET.fromstring(members[name])
            clips = before.findall("VideoTrackVec/Element/Sm2TiTrack/Items/Element/Sm2TiVideoClip")
            expected = [
                (clip.findtext("In"), decode_keyed_map(clip.findtext("MediaTimemapBA").encode("ascii")))
                for clip in clips[:2]
            ]
            if still:
                self.assertEqual(expected[1][0], "86400")
                self.assertEqual(expected[1][1]["XMax"], 60000)
                self.assertNotIn("LastValidYOffset", expected[1][1])
                self.assertEqual(expected[1][1]["KeyframesBA"], bytes.fromhex("800a000a090900000000004ced40"))
            for clip in clips[:2]:
                clip.find("In").text = "0"
                clip.find("MediaTimemapBA").text = "discarded-import-map"
            members[name] = ET.tostring(before, encoding="utf-8")
            api_path = folder / "api.drt"
            with zipfile.ZipFile(api_path, "w") as archive:
                for member, payload in members.items():
                    archive.writestr(member, payload)
            self.assertEqual(apply_timeline_state_to_archive(api_path, xml), 2)
            with zipfile.ZipFile(api_path) as archive:
                after = ET.fromstring(archive.read(name))
        targets = after.findall("VideoTrackVec/Element/Sm2TiTrack/Items/Element/Sm2TiVideoClip")
        for target, (in_point, native_map) in zip(targets, expected):
            self.assertEqual(target.findtext("In"), in_point)
            api_map = decode_keyed_map(target.findtext("MediaTimemapBA").encode("ascii"))
            native_map.pop("UniqueId")
            api_map.pop("UniqueId")
            self.assertEqual(api_map, native_map)
        self.assertEqual(targets[2].findtext("MediaTimemapBA"), clips[2].findtext("MediaTimemapBA"))

    def test_full_graph_preserves_source_in_and_timeline_edit(self) -> None:
        root = sequence(fps=50)
        track = root.find("sequence/media/video/track")
        track.remove(track.find("clipitem"))
        clip = curves.RetimeCurveTests._fcp7_clip(curves.RetimeCurveTests._graphdict())
        file = clip.find("file")
        file.set("id", "curve-source")
        ET.SubElement(file, "name").text = "mixed.mov"
        ET.SubElement(file, "pathurl").text = "file://localhost/media/mixed.mov"
        track.append(clip)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            path = native.export_native_drp(root, Path(temp) / "curve.drp", "Curve")
            with zipfile.ZipFile(path) as archive:
                name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                written = ET.fromstring(archive.read(name))
        target = written.find(".//Sm2TiVideoClip")
        self.assertEqual((target.findtext("Start"), target.findtext("Duration")), ("2646", "216"))
        self.assertEqual(target.findtext("In"), "260")
        time_map = curves._read_keyed_dict(bytes.fromhex(target.findtext("MediaTimemapBA")))
        origin, points = curves._read_protobuf_points(time_map["KeyframesBA"])
        self.assertEqual(origin, 0.0)
        self.assertEqual(points, ((260 / 50, 169 / 50), (476 / 50, 309.4 / 50), (1371 / 50, 891.7 / 50)))
        self.assertAlmostEqual(time_map["LastValidYOffset"], 445 / 25)
        self.assertEqual(clip.findtext("filter/effect/parameter[parameterid='graphdict']/interpolation/name"), "FCPCurve")


if __name__ == "__main__":
    unittest.main()
