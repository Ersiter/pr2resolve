"""Offline checks for verified native Resolve timeline-state blobs."""

from __future__ import annotations

import struct
import sys
import re
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pr2_native import export_native_drp
from pr2_native_state import (
    append_lockable_blob,
    encode_timeline_markers,
    sequence_id_from_pool,
    sequence_lockable_blob,
    sequence_markers,
    unwritten_state_warnings,
    remap_marker_blob,
)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7


def _fields(data: bytes) -> list[tuple[int, int, bytes | int]]:
    result = []
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            value = data[offset:offset + size]
            offset += size
        else:
            raise AssertionError(f"unexpected test protobuf wire {wire}")
        result.append((number, wire, value))
    return result


def _decode_marker_blob(fields_hex: str) -> list[dict[str, object]]:
    fields = bytes.fromhex(fields_hex)
    header, count = struct.unpack_from(">II", fields)
    if header != 1 or count != 1:
        raise AssertionError("unexpected FieldsBlob keyed-dict header")
    key_length = struct.unpack_from(">I", fields, 8)[0]
    offset = 12 + key_length
    value_type, subtype, value_length = struct.unpack_from(">IBI", fields, offset)
    if value_type != 12 or subtype != 0:
        raise AssertionError("BlobData is not a bytes keyed-dict value")
    blob_data = fields[offset + 9:offset + 9 + value_length]
    version, framed_length = struct.unpack_from(">II", blob_data)
    if version != 10001 or blob_data[8] != 0x80 or framed_length != len(blob_data) - 8:
        raise AssertionError("unexpected marker blob framing")
    root = _fields(blob_data[9:])
    entries = next(value for number, wire, value in root if number == 2 and wire == 2)
    entries = next(value for number, wire, value in _fields(entries) if number == 2 and wire == 2)
    decoded = []
    for _, _, entry_data in _fields(entries):
        entry_fields = _fields(entry_data)
        frame_number = next(value for number, wire, value in entry_fields if number == 1 and wire == 0)
        wrapped = next(value for number, wire, value in entry_fields if number == 2 and wire == 2)
        version, length = struct.unpack_from(">II", wrapped)
        if version != 2 or length != len(wrapped) - 8:
            raise AssertionError("unexpected marker entry wrapper")
        marker_payload = next(value for number, wire, value in _fields(wrapped[8:]) if number == 1 and wire == 2)
        marker_fields = _fields(marker_payload)
        color = next(value for number, wire, value in marker_fields if number == 1 and wire == 0)
        strings = [value.decode("utf-8") for number, wire, value in marker_fields if number == 3 and wire == 2]
        decoded.append({"frame": frame_number, "color": color, "note": strings[0], "duration": int(strings[1]), "name": strings[2]})
    return decoded


class NativeStateTests(unittest.TestCase):
    def test_multi_timeline_archive_keeps_each_marker_with_its_sequence(self) -> None:
        sources = []
        for number in (1, 2):
            sources.append(ET.fromstring(f'''<xmeml><sequence id="source-{number}">
                <name>Sequence {number}</name><duration>25</duration>
                <rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate>
                <media><video><track><clipitem><name>shot.mov</name><start>0</start><end>25</end>
                <in>0</in><out>25</out><file id="media"><name>shot.mov</name>
                <pathurl>shot.mov</pathurl><duration>25</duration></file></clipitem>
                </track></video></media><marker><name>Marker {number}</name><in>{number}</in>
                <out>{number + 1}</out><comment>Note {number}</comment></marker>
                </sequence></xmeml>'''))
        with tempfile.TemporaryDirectory() as temp:
            path = export_native_drp(sources, Path(temp) / "markers.drp", "Markers")
            with zipfile.ZipFile(path) as archive:
                project = ET.fromstring(archive.read("project.xml").decode("utf-8").replace("ListMgt::", "ListMgt__"))
                pool = ET.fromstring(archive.read("MediaPool/Master/MpFolder.xml").decode("utf-8").replace("ListMgt::", "ListMgt__"))
        records = project.findall(".//Sm2SequenceLockableBlob")
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record.get("DbId") for record in records}), 2)
        self.assertEqual({record.findtext("BlobOwner") for record in records},
                         {node.get("DbId") for node in pool.iter("Sm2Sequence")})
        decoded = [marker for record in records for marker in _decode_marker_blob(record.findtext("FieldsBlob"))]
        self.assertEqual(sorted((marker["frame"], marker["name"], marker["note"]) for marker in decoded),
                         [(1, "Marker 1", "Note 1"), (2, "Marker 2", "Note 2")])

    def test_resolve_marker_blob_round_trips_through_independent_decoder(self) -> None:
        markers = [
            {"frame": 0, "duration": 1, "name": "Intro", "note": "First frame", "color": "Blue"},
            {"frame": 120, "duration": 24, "name": "Cut", "note": "Review this", "color": "Green"},
        ]
        decoded = _decode_marker_blob(encode_timeline_markers(markers))
        self.assertEqual(decoded, [
            {"frame": 120, "color": 8, "note": "Review this", "duration": 24, "name": "Cut"},
            {"frame": 0, "color": 2, "note": "First frame", "duration": 1, "name": "Intro"},
        ])

    def test_sequence_markers_attach_to_matching_pool_sequence(self) -> None:
        pool = '<Sm2MpTimelineClip><Sm2Sequence DbId="seq-1" /></Sm2MpTimelineClip>'
        source = ET.fromstring(
            '<sequence><marker><name>Scene</name><in>42</in><out>50</out><comment>Keep</comment></marker></sequence>'
        )
        markers = sequence_markers(source)
        self.assertEqual(markers, [{"frame": 42, "duration": 8, "name": "Scene", "note": "Keep", "color": "Blue"}])
        record = sequence_lockable_blob(sequence_id_from_pool(pool), markers)
        project = append_lockable_blob("<LockableBlobMap><LocableBlobSet></LocableBlobSet></LockableBlobMap>", record)
        self.assertIn("<BlobOwner>seq-1</BlobOwner>", project)
        self.assertEqual(_decode_marker_blob(project.split("<FieldsBlob>", 1)[1].split("</FieldsBlob>", 1)[0]), [
            {"frame": 42, "color": 2, "note": "Keep", "duration": 8, "name": "Scene"},
        ])

    def test_sequence_marker_owner_is_remapped_when_project_merges(self) -> None:
        record = sequence_lockable_blob("old-sequence", [{"frame": 10, "name": "Marker"}])
        original_id = re.search(r'Sm2SequenceLockableBlob DbId="([^"]+)', record).group(1)
        remapped = remap_marker_blob(record, "old-sequence", "new-sequence")
        new_id = re.search(r'Sm2SequenceLockableBlob DbId="([^"]+)', remapped).group(1)
        self.assertNotEqual(new_id, original_id)
        self.assertIn("<BlobOwner>new-sequence</BlobOwner>", remapped)

    def test_default_state_is_quiet_and_explicit_unwritten_state_is_reported(self) -> None:
        source = ET.fromstring("<sequence><in>-1</in><out>-1</out></sequence>")
        self.assertEqual(unwritten_state_warnings(source, {}), [])
        source = ET.fromstring("<sequence><in>10</in><out>20</out></sequence>")
        warnings = unwritten_state_warnings(source, {
            "work_area": [10, 20],
            "unknown": [("track-state:video:1", '{"muted": true, "locked": true}')],
            "markers": [{"scope": "track"}, {"scope": "sequence", "color_index": 1}],
        })
        self.assertEqual(len(warnings), 7)


if __name__ == "__main__":
    unittest.main()
