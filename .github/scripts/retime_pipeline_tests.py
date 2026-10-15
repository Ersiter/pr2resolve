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


class RetimePipelineTests(unittest.TestCase):
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
