"""Focused checks for the standalone Resolve 21 media-time encoder."""

from __future__ import annotations

import math
import struct
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pr2_native_time import build_media_timemap, retimed_source_in


def _read_keyed_dict(data: bytes) -> dict[str, tuple[int, object]]:
    header, count = struct.unpack_from(">II", data)
    if header != 1:
        raise AssertionError("not a keyed dict")
    offset = 8
    entries: dict[str, tuple[int, object]] = {}
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
        entries[key] = (value_type, value)
    if offset != len(data):
        raise AssertionError("keyed dict has trailing bytes")
    return entries


def _read_keyframe_point(data: bytes) -> tuple[float, float]:
    if len(data) != 18 or data[0] != 0x09 or data[9] != 0x11:
        raise AssertionError("unexpected constant-speed protobuf point")
    return struct.unpack_from("<d", data, 1)[0], struct.unpack_from("<d", data, 10)[0]


class NativeTimeTests(unittest.TestCase):
    def test_identity_video_uses_last_frame_extent(self) -> None:
        blob = bytes.fromhex(build_media_timemap(101, 25.0, 100.0))
        self.assertEqual(blob[0], 0x02)
        self.assertEqual(struct.unpack(">5d", blob[1:]), (4.0, 0.0, 4.0, 0.0, 4.0))

    def test_identity_audio_uses_single_extent(self) -> None:
        blob = bytes.fromhex(build_media_timemap(101, 25.0, 100.0, audio=True))
        self.assertEqual(blob, b"\x02" + struct.pack(">d", 4.0))

    def test_constant_retime_uses_modern_protobuf_slope(self) -> None:
        blob = bytes.fromhex(build_media_timemap(101, 25.0, 50.0))
        outer = _read_keyed_dict(blob)
        self.assertEqual(outer["DbType"], (0x0A, "Sm2TimeMap"))
        self.assertEqual(outer["XMax"], (0x06, 8.0))
        self.assertEqual(outer["LastValidYOffset"], (0x06, 4.0))
        self.assertEqual(outer["YMin"], (0x06, -1.0))
        self.assertEqual(outer["YMax"], (0x06, -1.0))
        points = outer["KeyframesBA"][1]
        self.assertEqual(points[:5], b"\x80\x0a\x00\x0a\x12")
        record, source = _read_keyframe_point(points[5:])
        self.assertEqual((record, source), (8.0, 4.0))
        self.assertEqual(source / record, 0.5)
        self.assertEqual(len(outer["UniqueId"][1]), 36)

    def test_fast_constant_retime_has_requested_slope(self) -> None:
        outer = _read_keyed_dict(bytes.fromhex(build_media_timemap(101, 25.0, 200.0)))
        point = outer["KeyframesBA"][1][5:]
        record, source = _read_keyframe_point(point)
        self.assertTrue(math.isclose(source / record, 2.0))

    def test_audio_retime_preserves_requested_source_to_record_ratio(self) -> None:
        for speed, expected_record in ((50.0, 8.0), (200.0, 2.0)):
            with self.subTest(speed=speed):
                outer = _read_keyed_dict(bytes.fromhex(build_media_timemap(101, 25.0, speed, audio=True)))
                record, source = _read_keyframe_point(outer["KeyframesBA"][1][5:])
                self.assertEqual((record, source), (expected_record, 4.0))
                self.assertEqual(outer["XMax"], (0x06, expected_record))

    def test_audio_curve_keeps_record_and_source_extents(self) -> None:
        outer = _read_keyed_dict(bytes.fromhex(build_media_timemap(
            151, 25.0, audio=True, keyframes=((0.0, 0.0), (1.0, 1.0), (3.0, 5.0)))))
        self.assertEqual(outer["XMax"], (0x06, 3.0))
        self.assertEqual(outer["LastValidYOffset"], (0x06, 6.0))

    def test_retimed_in_point_is_record_domain(self) -> None:
        self.assertEqual(retimed_source_in(48, 24.0, 24.0, 50.0), "96")
        encoded = retimed_source_in(1, 24.0, 29.97, 50.0)
        whole, fraction_hex = encoded.split("|")
        fraction = struct.unpack("<d", bytes.fromhex(fraction_hex))[0]
        self.assertEqual(whole, "2")
        self.assertTrue(math.isclose(2 + fraction, 29.97 / 12))

    def test_invalid_domain_inputs_are_rejected(self) -> None:
        for frames in (0, -1, 1.5, True):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                build_media_timemap(frames, 24.0, 100.0)  # type: ignore[arg-type]
        for fps in (0.0, float("nan"), float("inf")):
            with self.subTest(fps=fps), self.assertRaises(ValueError):
                build_media_timemap(10, fps, 100.0)
        for speed in (0.0, -10.0, float("nan")):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                build_media_timemap(10, 24.0, speed)


def run() -> unittest.result.TestResult:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(NativeTimeTests)
    return unittest.TextTestRunner(verbosity=2).run(suite)


if __name__ == "__main__":
    result = run()
    raise SystemExit(not result.wasSuccessful())
