from __future__ import annotations

import base64
import gzip
import pathlib
import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from pr2_captions import encode_subtitle_track, extract_captions


_MAGIC = b"\x44\x33\x22\x11"


def _align4(payload: bytearray) -> None:
    payload.extend(b"\0" * (-len(payload) % 4))


def _synthetic_formatted_text(
    texts: tuple[str, ...], fonts: tuple[str, ...] = ("Arial",)
) -> tuple[bytes, tuple[int, ...]]:
    if not texts or not fonts:
        raise ValueError("the synthetic schema requires text runs and font names")
    payload = bytearray(b"\0" * 16)
    payload[8:12] = _MAGIC

    def add_table(slot_count: int) -> int:
        _align4(payload)
        vtable = len(payload)
        object_size = 4 + 4 * slot_count
        payload.extend(struct.pack("<HH", 4 + 2 * slot_count, object_size))
        payload.extend(struct.pack("<" + "H" * slot_count, *(4 + 4 * i for i in range(slot_count))))
        _align4(payload)
        table = len(payload)
        payload.extend(struct.pack("<i", table - vtable))
        payload.extend(b"\0" * (4 * slot_count))
        return table

    def set_offset(field: int, target: int) -> None:
        if target <= field:
            raise AssertionError("FlatBuffer uoffset targets must follow their fields")
        struct.pack_into("<I", payload, field, target - field)

    def add_string(value: str) -> tuple[int, int]:
        _align4(payload)
        start = len(payload)
        data = value.encode("utf-8")
        payload.extend(struct.pack("<I", len(data)))
        payload.extend(data)
        terminator = len(payload)
        payload.append(0)
        _align4(payload)
        return start, terminator

    root = add_table(1)
    set_offset(12, root)
    document = add_table(2)
    set_offset(root + 4, document)

    _align4(payload)
    runs_vector = len(payload)
    payload.extend(struct.pack("<I", len(texts)))
    run_pointers = tuple(len(payload) + 4 * i for i in range(len(texts)))
    payload.extend(b"\0" * (4 * len(texts)))
    set_offset(document + 4, runs_vector)

    run_text_fields: list[tuple[int, str]] = []
    for pointer in run_pointers:
        run = add_table(1)
        set_offset(pointer, run)
        run_text_fields.append((run + 4, texts[len(run_text_fields)]))

    terminators: list[int] = []
    for field, value in run_text_fields:
        string, terminator = add_string(value)
        set_offset(field, string)
        terminators.append(terminator)

    _align4(payload)
    fonts_vector = len(payload)
    payload.extend(struct.pack("<I", len(fonts)))
    font_pointers = tuple(len(payload) + 4 * i for i in range(len(fonts)))
    payload.extend(b"\0" * (4 * len(fonts)))
    set_offset(document + 8, fonts_vector)
    for pointer, font in zip(font_pointers, fonts):
        string, _ = add_string(font)
        set_offset(pointer, string)

    struct.pack_into("<Q", payload, 0, len(payload) - 12)
    return bytes(payload), tuple(terminators)


def _caption_project(
    payloads: list[bytes],
    start_ticks: int = 254_016_000_000,
    end_ticks: int = 508_032_000_000,
) -> ET.Element:
    root = ET.Element("PremiereData")
    sequence = ET.SubElement(root, "Sequence", {"ObjectUID": "sequence-1"})
    groups = ET.SubElement(sequence, "TrackGroups")
    group_pair = ET.SubElement(groups, "TrackGroup")
    ET.SubElement(group_pair, "Second", {"ObjectRef": "group-1"})
    group = ET.SubElement(root, "DataTrackGroup", {"ObjectID": "group-1"})
    group_track = ET.SubElement(group, "TrackGroup")
    ET.SubElement(group_track, "FrameRate").text = "8467200000"
    tracks = ET.SubElement(group_track, "Tracks")
    ET.SubElement(tracks, "Track", {"Index": "0", "ObjectURef": "track-1"})
    track = ET.SubElement(root, "CaptionDataClipTrack", {"ObjectUID": "track-1"})
    item_refs = ET.SubElement(track, "TrackItems")
    ET.SubElement(item_refs, "TrackItem", {"ObjectRef": "item-1"})
    item = ET.SubElement(root, "CaptionDataClipTrackItem", {"ObjectID": "item-1"})
    clip_item = ET.SubElement(ET.SubElement(item, "DataClipTrackItem"), "ClipTrackItem")
    timing = ET.SubElement(clip_item, "TrackItem")
    ET.SubElement(timing, "Start").text = str(start_ticks)
    ET.SubElement(timing, "End").text = str(end_ticks)
    block_vector = ET.SubElement(item, "BlockVector")
    for index, payload in enumerate(payloads):
        block_id = f"block-{index}"
        ET.SubElement(block_vector, "BlockVectorItem", {"Index": str(index), "ObjectRef": block_id})
        block = ET.SubElement(root, "Block", {"ObjectID": block_id})
        encoded = ET.SubElement(
            block,
            "FormattedTextData",
            {"Encoding": "base64", "BinaryHash": f"hash-{index}"},
        )
        encoded.text = base64.b64encode(payload).decode("ascii")
    return root


class CaptionExtractionTests(unittest.TestCase):
    def test_decodes_short_cjk_english_digits_and_multiline_text(self) -> None:
        for expected in ("中", "Hi", "2026", "Hello", "Line 1\nLine 2"):
            with self.subTest(expected=expected):
                payload, _ = _synthetic_formatted_text((expected,))
                record = extract_captions(_caption_project([payload]), "sequence-1")[0]
                self.assertEqual(record.text, expected)
                self.assertEqual(record.text_blocks, (expected,))
                self.assertEqual(record.decode_errors, ())

    def test_decodes_multiple_runs_and_font_strings_from_schema_vectors(self) -> None:
        payload, _ = _synthetic_formatted_text(("Hello", "中"), ("Arial", "Noto Sans CJK"))
        record = extract_captions(_caption_project([payload]), "sequence-1")[0]
        self.assertEqual(record.text, "Hello中")
        self.assertEqual(record.font_candidates, ("Arial", "Noto Sans CJK"))

    def test_joins_multiple_formatted_text_blocks_in_vector_order(self) -> None:
        first, _ = _synthetic_formatted_text(("Hi",))
        second, _ = _synthetic_formatted_text(("中",))
        record = extract_captions(_caption_project([first, second]), "sequence-1")[0]
        self.assertEqual(record.text, "Hi中")
        self.assertEqual(record.text_blocks, ("Hi", "中"))

    def test_rejects_bad_offset_and_damaged_string_boundary(self) -> None:
        payload, terminators = _synthetic_formatted_text(("known text",))
        bad_offset = bytearray(payload)
        struct.pack_into("<I", bad_offset, 12, 0xFFFFFFFC)
        bad_boundary = bytearray(payload)
        bad_boundary[terminators[0]] = 0x41
        for damaged in (bytes(bad_offset), bytes(bad_boundary), payload[:-1]):
            with self.subTest(damaged_length=len(damaged)):
                record = extract_captions(_caption_project([damaged]), "sequence-1")[0]
                self.assertIsNone(record.text)
                self.assertTrue(record.decode_errors)

    def test_unknown_payload_is_preserved_but_not_guessed_or_encoded(self) -> None:
        record = extract_captions(_caption_project([b"unrecognized bytes"]), "sequence-1")[0]
        self.assertIsNone(record.text)
        self.assertTrue(record.decode_errors)
        self.assertTrue(record.formatted_text_data)
        with self.assertRaisesRegex(ValueError, "cannot be encoded"):
            encode_subtitle_track([record], "target", Fraction(30, 1))

    def test_quantizes_to_explicit_target_sequence_fps_and_reports_residual(self) -> None:
        payload, _ = _synthetic_formatted_text(("cue",))
        root = _caption_project(
            [payload],
            start_ticks=317_520_000_000,
            end_ticks=660_441_600_000,
        )
        record = extract_captions(root, "sequence-1")[0]
        encoded = encode_subtitle_track([record], "target", Fraction(24, 1))
        mapping = encoded.timing_mappings[0]
        self.assertEqual((mapping.target_start_frame, mapping.target_end_frame), (30, 62))
        self.assertEqual(mapping.start_error_ticks, Fraction(0))
        self.assertEqual(mapping.end_error_ticks, Fraction(-4_233_600_000))

    def test_rejects_caption_that_rounds_to_zero_target_frames(self) -> None:
        payload, _ = _synthetic_formatted_text(("short",))
        record = extract_captions(
            _caption_project([payload], start_ticks=0, end_ticks=1_000_000_000),
            "sequence-1",
        )[0]
        with self.assertRaisesRegex(ValueError, "zero target frames"):
            encode_subtitle_track([record], "target", Fraction(24, 1))

    def test_missing_sequence_is_reported(self) -> None:
        with self.assertRaisesRegex(ValueError, "Sequence not found"):
            extract_captions(ET.Element("PremiereData"), "missing")


class RealPremiereSampleIntegrationTests(unittest.TestCase):
    def test_extracts_28_cues_from_local_sample_and_encodes_track(self) -> None:
        sample = REPO_ROOT / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
        if not sample.is_file():
            self.skipTest("local Premiere integration sample is not present")
        root = ET.fromstring(gzip.decompress(sample.read_bytes()))
        records = [
            record
            for sequence in root.findall("Sequence")
            for record in extract_captions(root, sequence.get("ObjectUID", ""))
        ]
        self.assertEqual(len(records), 28)
        self.assertTrue(all(record.text is not None for record in records))
        self.assertTrue(all(not record.decode_errors for record in records))
        populated = records[0]
        self.assertEqual(populated.end_ticks, 1_862_784_000_000)
        self.assertEqual(populated.duration_ticks, populated.end_ticks - populated.start_ticks)
        self.assertTrue(populated.font_candidates)
        self.assertTrue(populated.formatted_text_data)
        self.assertTrue(populated.formatted_text_hashes)
        self.assertIn(f"item:{populated.item_object_id}", populated.source_refs)
        self.assertTrue(any(ref.startswith("block:") for ref in populated.source_refs))

        same_track = [record for record in records if record.track_uid == populated.track_uid]
        encoded = encode_subtitle_track(
            same_track,
            "00000000-0000-0000-0000-000000000001",
            Fraction(24000, 1001),
        )
        target_track = encoded.element
        target_generators = target_track.findall("Sm2TiTrack/Items/Element/Sm2TiGenerator")
        self.assertEqual(len(target_generators), len(same_track))
        self.assertEqual(target_track.findtext("Sm2TiTrack/Type"), "2")
        self.assertEqual(
            target_track.findtext("Sm2TiTrack/Sequence"),
            "00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(target_generators[0].findtext("Name"), populated.text)
        self.assertEqual(target_generators[0].findtext("Start"), "0")
        self.assertEqual(target_generators[0].findtext("Duration"), "176")
        self.assertNotEqual(encoded.timing_mappings[0].end_error_ticks, 0)


if __name__ == "__main__":
    unittest.main()
