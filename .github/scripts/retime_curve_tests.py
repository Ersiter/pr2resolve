"""Checks for piecewise record/source time maps without starting Resolve."""

from __future__ import annotations

import math
import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pr2_native_time import (
    build_media_timemap,
    extract_fcp7_retime_alignment,
    record_intervals_at_source,
    source_at_record,
)


def _read_keyed_dict(data: bytes) -> dict[str, object]:
    header, count = struct.unpack_from(">II", data)
    if header != 1:
        raise AssertionError("not a keyed dict")
    offset = 8
    entries: dict[str, object] = {}
    for _ in range(count):
        key_size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        key = data[offset:offset + key_size].decode("utf-16-be")
        offset += key_size
        value_type, subtype = struct.unpack_from(">IB", data, offset)
        offset += 5
        if subtype != 0:
            raise AssertionError("unexpected keyed-dict subtype")
        if value_type == 0x06:
            value = struct.unpack_from(">d", data, offset)[0]
            offset += 8
        elif value_type in (0x0A, 0x0C):
            size = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            raw = data[offset:offset + size]
            offset += size
            value = raw.decode("utf-16-be") if value_type == 0x0A else raw
        else:
            raise AssertionError(f"unexpected keyed-dict value type: {value_type}")
        entries[key] = value
    if offset != len(data):
        raise AssertionError("keyed dict has trailing bytes")
    return entries


def _read_protobuf_points(data: bytes) -> tuple[float, tuple[tuple[float, float], ...]]:
    if not data.startswith(b"\x80\x0a\x00"):
        raise AssertionError("unexpected KeyframesBA header")
    offset = 3
    origin_source = 0.0
    if data[offset:offset + 1] == b"\x11":
        origin_source = struct.unpack_from("<d", data, offset + 1)[0]
        offset += 9
    points: list[tuple[float, float]] = []
    while offset < len(data):
        if data[offset:offset + 2] != b"\x0a\x12":
            raise AssertionError("unexpected keyframe message")
        offset += 2
        point = data[offset:offset + 18]
        if len(point) != 18 or point[0] != 0x09 or point[9] != 0x11:
            raise AssertionError("unexpected keyframe point")
        points.append((struct.unpack_from("<d", point, 1)[0], struct.unpack_from("<d", point, 10)[0]))
        offset += 18
    return origin_source, tuple(points)


class RetimeCurveTests(unittest.TestCase):
    @staticmethod
    def _fcp7_clip(graph: str, *, variablespeed: str = "0") -> ET.Element:
        return ET.fromstring(f"""
            <clipitem>
              <name>DJI_20231122_163853_024_video.mp4</name>
              <rate><timebase>50</timebase><ntsc>FALSE</ntsc></rate>
              <duration>1371</duration><start>2646</start><end>2862</end>
              <in>260</in><out>476</out>
              <file><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><duration>446</duration></file>
              <filter><effect>
                <effectid>timeremap</effectid>
                <parameter><parameterid>speed</parameterid><value>65</value></parameter>
                <parameter><parameterid>variablespeed</parameterid><value>{variablespeed}</value></parameter>
                <parameter><parameterid>graphdict</parameterid>{graph}</parameter>
              </effect></filter>
            </clipitem>
        """)

    @staticmethod
    def _graphdict(*, include_handle: bool = False, nonmonotone: bool = False) -> str:
        terminal = 280.0 if nonmonotone else 891.7
        handle = "<outbez><horiz>0.2</horiz><vert>0.1</vert></outbez>" if include_handle else ""
        return f"""
          <keyframe><when>0</when><value>0</value><speedvirtualkf>TRUE</speedvirtualkf><speedkfstart>TRUE</speedkfstart></keyframe>
          <keyframe><when>260</when><value>169</value><speedkfin>TRUE</speedkfin></keyframe>
          <keyframe><when>476</when><value>309.4</value><speedkfout>TRUE</speedkfout></keyframe>
          <keyframe><when>1371</when><value>{terminal}</value><speedkfend>TRUE</speedkfend>{handle}</keyframe>
          <interpolation><name>FCPCurve</name></interpolation>
        """

    def test_fcp7_graphdict_extracts_alignment_without_changing_timeline(self) -> None:
        clip = self._fcp7_clip(self._graphdict())
        alignment = extract_fcp7_retime_alignment(clip, 50.0)
        self.assertEqual(alignment["clip_rate"], 50.0)
        self.assertEqual(alignment["file_rate"], 25.0)
        self.assertEqual(alignment["source_in_seconds"], 169 / 50)
        self.assertNotEqual(alignment["source_in_seconds"], 260 / 50)
        self.assertAlmostEqual(alignment["source_in_seconds"] * alignment["file_rate"], 84.5)
        self.assertAlmostEqual(alignment["record_in_seconds"], 260 / 50)
        self.assertAlmostEqual(alignment["record_in_frames"], 260.0)
        self.assertEqual(alignment["encoded_in"], "260")
        points = alignment["points"]
        self.assertEqual(points[1], (260 / 50, 169 / 50))
        self.assertEqual(alignment["first_mapped_sample"], {"record_seconds": 0.0, "source_seconds": 0.0})
        self.assertEqual(alignment["last_mapped_sample"], {"record_seconds": 1371 / 50, "source_seconds": 891.7 / 50})
        self.assertEqual(alignment["last_visible_sample"], {"record_seconds": 9.5, "source_seconds": 308.75 / 50})
        self.assertAlmostEqual(alignment["speedkfout_expected_last_source_seconds"], 308.75 / 50)
        self.assertAlmostEqual(alignment["source_trim_end_residual_seconds"], 0.0, delta=1e-12)
        self.assertEqual(alignment["timeline_start"], "2646")
        self.assertEqual(alignment["timeline_end"], "2862")
        self.assertEqual(alignment["clipitem_duration"], "1371")
        keyframes = alignment["raw_keyframes"]
        self.assertEqual(keyframes[1]["flags"]["speedkfin"], "TRUE")
        self.assertEqual(keyframes[2]["flags"]["speedkfout"], "TRUE")
        self.assertIn("FCPCurve", alignment["raw_graphdict_xml"])
        self.assertTrue(any("approximation" in warning for warning in alignment["warnings"]))
        self.assertEqual(alignment["source_in_definition"], "speedkfin.value")

    def test_sr_export_clip_is_parsed_with_mixed_rate_evidence(self) -> None:
        sample = Path(__file__).resolve().parents[2] / "test" / "SR_export-from-davinci-resolve.xml"
        if not sample.is_file():
            self.skipTest("local Resolve export fixture is unavailable")
        root = ET.parse(sample).getroot()
        clip = next(
            clipitem for clipitem in root.findall(".//track/clipitem")
            if clipitem.findtext("./name") == "DJI_20231122_163853_024_video.mp4"
            and clipitem.findtext("./in") == "260"
            and clipitem.find("./filter/effect/parameter[parameterid='graphdict']/keyframe") is not None
        )
        alignment = extract_fcp7_retime_alignment(clip, 50.0)
        self.assertEqual(alignment["clip_rate"], 50.0)
        self.assertEqual(alignment["file_rate"], 25.0)
        self.assertEqual(alignment["source_frames"], 446.0)
        self.assertAlmostEqual(alignment["source_media_duration_seconds"], 17.84)
        self.assertEqual(alignment["points"][1], (260 / 50, 169 / 50))
        self.assertEqual(alignment["source_in_frames"], 169.0)
        self.assertEqual(alignment["record_in_frames"], 260.0)
        self.assertEqual(alignment["last_visible_sample"], {"record_seconds": 9.5, "source_seconds": 308.75 / 50})
        self.assertAlmostEqual(alignment["source_trim_end_residual_seconds"], 0.0, delta=1e-12)

    def test_speedkfin_mismatch_rejects_transition_handle_offset(self) -> None:
        clip = self._fcp7_clip(self._graphdict()).find(".")
        self.assertIsNotNone(clip)
        clip.find("./in").text = "250"
        with self.assertRaisesRegex(ValueError, "transition/handle compensation"):
            extract_fcp7_retime_alignment(clip, 50.0)

    def test_missing_speedkfin_uses_graph_at_clipitem_in_with_warning(self) -> None:
        graph = self._graphdict().replace("<speedkfin>TRUE</speedkfin>", "")
        alignment = extract_fcp7_retime_alignment(self._fcp7_clip(graph), 50.0)
        self.assertEqual(alignment["source_in_seconds"], 169 / 50)
        self.assertEqual(alignment["record_in_seconds"], 260 / 50)
        self.assertTrue(any("speedkfin absent" in warning for warning in alignment["warnings"]))

    def test_fcp7_graphdict_is_required_even_for_static_speed_flag(self) -> None:
        for variable_speed in ("0", "1"):
            with self.subTest(variable_speed=variable_speed), self.assertRaisesRegex(
                ValueError, "no graphdict keyframes"
            ):
                extract_fcp7_retime_alignment(
                    self._fcp7_clip("", variablespeed=variable_speed), 50.0
                )

    def test_fcp7_graphdict_rejects_unverified_shapes(self) -> None:
        with self.assertRaisesRegex(ValueError, "Bezier"):
            extract_fcp7_retime_alignment(
                self._fcp7_clip(self._graphdict(include_handle=True)), 50.0
            )
        with self.assertRaisesRegex(ValueError, "nonmonotone"):
            extract_fcp7_retime_alignment(
                self._fcp7_clip(self._graphdict(nonmonotone=True)), 50.0
            )

    def test_piecewise_curve_evaluates_record_to_source(self) -> None:
        points = ((0.0, 3.0), (2.0, 5.0), (5.0, 4.0))
        self.assertEqual(source_at_record(points, 0.0), 3.0)
        self.assertEqual(source_at_record(points, 1.0), 4.0)
        self.assertEqual(source_at_record(points, 3.5), 4.5)
        self.assertEqual(source_at_record(points, 5.0), 4.0)

    def test_inverse_preserves_multiple_solutions_and_hold_intervals(self) -> None:
        points = ((0.0, 3.0), (2.0, 5.0), (4.0, 5.0), (6.0, 3.0))
        self.assertEqual(record_intervals_at_source(points, 5.0), ((2.0, 4.0),))
        self.assertEqual(record_intervals_at_source(points, 4.0), ((1.0, 1.0), (5.0, 5.0)))
        self.assertEqual(record_intervals_at_source(points, 8.0), ())

    def test_sm2_timemap_keeps_absolute_source_in_and_all_points(self) -> None:
        points = ((0.0, 2.0), (1.0, 4.0), (3.0, 5.0))
        outer = _read_keyed_dict(bytes.fromhex(build_media_timemap(241, 24.0, keyframes=points)))
        self.assertEqual(outer["DbType"], "Sm2TimeMap")
        self.assertEqual(outer["XMax"], 3.0)
        self.assertEqual(outer["LastValidYOffset"], 10.0)
        origin, encoded_points = _read_protobuf_points(outer["KeyframesBA"])
        self.assertEqual(origin, 2.0)
        self.assertEqual(encoded_points, points[1:])

    def test_resolve_xmeml_graph_uses_clipitem_timebase_for_both_axes(self) -> None:
        # Resolve SR_export clip: XML in=260, speedkfin=(260,169),
        # clip rate 50 fps, file rate 25 fps, speedkfout=(476,309.4).
        clip_rate, file_rate = 50.0, 25.0
        graph_frames = ((0.0, 0.0), (260.0, 169.0), (476.0, 309.4), (1371.0, 891.7))
        points = tuple((when / clip_rate, value / clip_rate) for when, value in graph_frames)
        source_in_seconds = 169 / clip_rate
        self.assertEqual(source_at_record(points, 260 / clip_rate), source_in_seconds)
        source_endpoint_frames = points[-1][1] * file_rate
        self.assertAlmostEqual(source_endpoint_frames, 445.85)
        self.assertLess(abs(source_endpoint_frames - round(source_endpoint_frames)), 0.5)
        self.assertLessEqual(points[-1][1], 446 / file_rate)
        endpoint_slope = (309.4 - 169) / (476 - 260)
        self.assertAlmostEqual(endpoint_slope, 0.65, delta=1e-12)

    def test_relink_constant_map_endpoint_is_subframe_accurate(self) -> None:
        # Observed in test/Pr test/*_convert_by_relink.drp: XMax=37.7043333333,
        # Y=30.1634666667, a 0.8 source/record slope for a 24 fps reference.
        record_end, source_end, fps = 37.70433333333333, 30.163466666666668, 24.0
        record_error_frames = abs(record_end * fps - round(record_end * fps))
        source_error_frames = abs(source_end * fps - round(source_end * fps))
        self.assertTrue(math.isclose(source_end / record_end, 0.8, abs_tol=1e-12))
        self.assertLess(record_error_frames, 0.5)
        self.assertLess(source_error_frames, 0.5)

    def test_record_axis_and_source_extent_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            source_at_record(((0.0, 0.0), (1.0, 1.0), (1.0, 2.0)), 0.5)
        with self.assertRaisesRegex(ValueError, "source times"):
            build_media_timemap(25, 24.0, keyframes=((0.0, 0.0), (1.0, 2.0)))
        with self.assertRaisesRegex(ValueError, "freeze map"):
            build_media_timemap(25, 24.0, keyframes=((0.0, 0.5), (1.0, 0.5)))
        with self.assertRaisesRegex(ValueError, "mixed forward/reverse"):
            build_media_timemap(
                25, 24.0, keyframes=((0.0, 0.5), (1.0, 1.0), (2.0, 0.75))
            )


def run() -> unittest.result.TestResult:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RetimeCurveTests)
    return unittest.TextTestRunner(verbosity=2).run(suite)


if __name__ == "__main__":
    result = run()
    raise SystemExit(not result.wasSuccessful())
