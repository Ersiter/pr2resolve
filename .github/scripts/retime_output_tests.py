from __future__ import annotations

import hashlib
import contextlib
import io
import re
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pr2_retime_output import apply_timeline_state_to_archive
from pr2_native import export_native_drp
from conversion_tests import sequence


def source_xml(*, bad_graph: bool = False) -> bytes:
    graph_name = "Bezier" if bad_graph else "FCPCurve"
    return f'''<?xml version="1.0"?>
<xmeml version="5"><sequence><name>Mixed Rate</name><rate><timebase>50</timebase><ntsc>FALSE</ntsc></rate><media><video>
<track><clipitem id="variable"><start>1000</start><end>1010</end><duration>10</duration><in>260</in><out>476</out><rate><timebase>50</timebase><ntsc>FALSE</ntsc></rate><file id="srcA"/><filter><effect><effectid>timeremap</effectid><parameter><parameterid>speed</parameterid><value>65</value></parameter><parameter><parameterid>variablespeed</parameterid><value>1</value></parameter><parameter><parameterid>graphdict</parameterid><keyframe><when>0</when><value>0</value><speedvirtualkf>true</speedvirtualkf></keyframe><keyframe><when>260</when><value>169</value><speedkfin>true</speedkfin></keyframe><keyframe><when>476</when><value>309.4</value><speedkfout>true</speedkfout></keyframe><keyframe><when>1371</when><value>890.15</value></keyframe><interpolation><name>{graph_name}</name></interpolation></parameter></effect></filter></clipitem></track>
<track><clipitem id="constant"><start>2000</start><end>2030</end><duration>30</duration><in>25</in><out>55</out><rate><timebase>50</timebase><ntsc>FALSE</ntsc></rate><file id="srcB"/><filter><effect><effectid>timeremap</effectid><parameter><parameterid>speed</parameterid><value>50</value></parameter><parameter><parameterid>variablespeed</parameterid><value>0</value></parameter></effect></filter></clipitem></track>
<track><clipitem id="untouched"><start>3000</start><end>3010</end><duration>10</duration><in>0</in><out>10</out><file id="srcA"/></clipitem></track>
</video><audio><track/></audio></media></sequence>
<file id="srcA"><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><duration>500</duration><pathurl>file:///media/a.mov</pathurl></file>
<file id="srcB"><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><duration>500</duration><pathurl>file:///media/b.mov</pathurl></file>
</xmeml>'''.encode()


def sequence_xml(*, sequence_id: str = "sequence-a", selfclosing: bool = False) -> bytes:
    def clip(dbid: str, start: int, duration: int, in_value: str, map_value: str) -> str:
        in_tag = "<In/>" if selfclosing and dbid == "v1" else "<In />" if selfclosing and dbid == "v2" else f"<In>{in_value}</In>"
        return f'<Sm2TiVideoClip DbId="{dbid}"><Start>{start}</Start><Duration>{duration}</Duration>{in_tag}<MediaTimemapBA>{map_value}</MediaTimemapBA><Name>keep</Name></Sm2TiVideoClip>'

    tracks = (
        f'<Element><Sm2TiTrack DbId="tr1"><Sequence>{sequence_id}</Sequence><Items><Element>{clip("v1", 1000, 10, "old-a", "old-map-a")}</Element></Items></Sm2TiTrack></Element>'
        f'<Element><Sm2TiTrack DbId="tr2"><Sequence>{sequence_id}</Sequence><Items><Element>{clip("v2", 2000, 30, "old-b", "old-map-b")}</Element></Items></Sm2TiTrack></Element>'
        f'<Element><Sm2TiTrack DbId="tr3"><Sequence>{sequence_id}</Sequence><Items><Element>{clip("v3", 3000, 10, "stay-in", "stay-map")}</Element></Items></Sm2TiTrack></Element>'
    )
    return f'<?xml version="1.0"?><Sm2SequenceContainer DbId="seq"><Name>Mixed Rate</Name><VideoTrackVec>{tracks}</VideoTrackVec></Sm2SequenceContainer>'.encode()


def timeline_clip(name: str, sequence_id: str, dbid: str) -> bytes:
    return f'<Sm2MpTimelineClip DbId="{dbid}"><Name>{name}</Name><TimelineSharedHandle><Sm2Timeline DbId="timeline-{dbid}"><Name>{name}</Name><Sequence><Sm2Sequence DbId="{sequence_id}"/></Sequence></Sm2Timeline></TimelineSharedHandle></Sm2MpTimelineClip>'.encode()


def decode_keyed_map(encoded_hex: bytes) -> dict[str, object]:
    data = bytes.fromhex(encoded_hex.decode("ascii"))
    version, count = struct.unpack_from(">II", data)
    if version != 1:
        raise AssertionError(f"unexpected keyed-dict version {version}")
    offset, values = 8, {}
    for _ in range(count):
        key_size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        key = data[offset:offset + key_size].decode("utf-16-be")
        offset += key_size
        value_type = struct.unpack_from(">I", data, offset)[0]
        offset += 5
        if value_type == 0x06:
            values[key] = struct.unpack_from(">d", data, offset)[0]
            offset += 8
        elif value_type in (0x0A, 0x0C):
            size = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            raw = data[offset:offset + size]
            offset += size
            values[key] = raw.decode("utf-16-be") if value_type == 0x0A else raw
        else:
            raise AssertionError(f"unexpected keyed-dict type {value_type}")
    return values


class RetimeOutputTests(unittest.TestCase):
    def test_native_and_api_packages_share_pair_partial_clear_and_absent_marks(self) -> None:
        cases = (("Pair", 5, 40, ("5", "39")), ("SingleFrame", 0, 1, ("0", "0")),
                 ("InOnly", 5, -1, ("5", "")), ("OutOnly", -1, 40, ("", "39")),
                 ("Cleared", -1, -1, ("", "")), ("Absent", None, None, ("", "")))
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(directory)
            trees, xml_paths = [], []
            for name, mark_in, mark_out, expected in cases:
                tree = sequence(name=name)
                source = tree.find("sequence")
                if mark_in is not None:
                    ET.SubElement(source, "in").text = str(mark_in)
                    ET.SubElement(source, "out").text = str(mark_out)
                path = folder / (name + ".xml")
                ET.ElementTree(tree).write(path)
                trees.append(tree)
                xml_paths.append(path)
            path = export_native_drp(trees, folder / "native.drp", "Marks")
            with zipfile.ZipFile(path) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            pools = [name for name in members if name.startswith("MediaPool/") and name.endswith(".xml")]
            for pool_name in pools:
                pool = ET.fromstring(members[pool_name].replace(b"ListMgt::", b"ListMgt__"))
                for timeline in pool.iter("Sm2MpTimelineClip"):
                    case = next(case for case in cases if case[0] == timeline.findtext("Name"))
                    for endpoint, expected in zip(("In", "Out"), case[3]):
                        for kind in ("Audio", "Video"):
                            field = timeline.find("Mark" + endpoint + kind)
                            self.assertEqual(field.text or "", expected)
                            if case[0] != "Absent":
                                field.text = "99"
                members[pool_name] = ET.tostring(pool).replace(b"ListMgt__", b"ListMgt::")
            api = folder / "api.drt"
            with zipfile.ZipFile(api, "w") as archive:
                for name, data in members.items():
                    archive.writestr(name, data)
            self.assertEqual(apply_timeline_state_to_archive(api, xml_paths), 0)
            with zipfile.ZipFile(api) as archive:
                for name, original in members.items():
                    if name not in pools:
                        self.assertEqual(archive.read(name), original)
                        continue
                    pool = ET.fromstring(archive.read(name).replace(b"ListMgt::", b"ListMgt__"))
                    for timeline in pool.iter("Sm2MpTimelineClip"):
                        case = next(case for case in cases if case[0] == timeline.findtext("Name"))
                        for endpoint, expected in zip(("In", "Out"), case[3]):
                            for kind in ("Audio", "Video"):
                                self.assertEqual(timeline.findtext("Mark" + endpoint + kind), expected)

    def test_missing_mark_destination_leaves_all_archive_bytes_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = folder / "marks.drp"
            self.archive(archive)
            source = folder / "source.xml"
            source.write_text('<xmeml><sequence><name>Missing</name><in>5</in><out>40</out></sequence></xmeml>')
            before = archive.read_bytes()
            with self.assertRaisesRegex(ValueError, "exactly one MediaPool"):
                apply_timeline_state_to_archive(archive, source)
            self.assertEqual(archive.read_bytes(), before)

    def archive(self, path: Path, *, duplicate: bool = False, other_timeline: bool = False, selfclosing: bool = False) -> dict[str, bytes]:
        members = {"project.xml": b"opaque project bytes\x00\xff", "MediaPool/Master/MpFolder.xml": b'<Sm2MpFolder DbId="folder"><MediaVec><Element>' + timeline_clip("Mixed Rate", "sequence-a", "timeline-a") + b'</Element></MediaVec></Sm2MpFolder>', "SeqContainer/seq.xml": sequence_xml(selfclosing=selfclosing), "Other/keep.bin": b"other member\x00bytes"}
        if duplicate:
            members["MediaPool/Other/MpFolder.xml"] = b'<Sm2MpFolder><MediaVec><Element>' + timeline_clip("Mixed Rate", "sequence-b", "timeline-b") + b'</Element></MediaVec></Sm2MpFolder>'
            members["SeqContainer/same-name.xml"] = sequence_xml(sequence_id="sequence-b")
        if other_timeline:
            members["MediaPool/Other/MpFolder.xml"] = b'<Sm2MpFolder><MediaVec><Element>' + timeline_clip("Another Timeline", "sequence-b", "timeline-b") + b'</Element></MediaVec></Sm2MpFolder>'
            members["SeqContainer/other.xml"] = sequence_xml(sequence_id="sequence-b")
        with zipfile.ZipFile(path, "w") as archive:
            archive.comment = b"archive comment"
            for name, data in members.items():
                info = zipfile.ZipInfo(name, date_time=(2020, 2, 3, 4, 5, 6))
                info.comment = b"member metadata"
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o640 << 16
                archive.writestr(info, data)
        return members

    def test_mixed_rate_graph_and_constant_maps_only_touch_requested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "api.drp"
            xml_path = Path(directory) / "source.xml"
            original = self.archive(archive_path, other_timeline=True, selfclosing=True)
            xml_path.write_bytes(source_xml())
            self.assertEqual(apply_timeline_state_to_archive(archive_path, xml_path), 2)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.comment, b"archive comment")
                self.assertEqual(archive.namelist(), list(original))
                self.assertEqual(archive.read("project.xml"), original["project.xml"])
                self.assertEqual(archive.read("Other/keep.bin"), original["Other/keep.bin"])
                self.assertEqual(archive.read("SeqContainer/other.xml"), original["SeqContainer/other.xml"])
                result = archive.read("SeqContainer/seq.xml")
                self.assertIn(b"<In>260</In>", result)
                self.assertIn(b"<In>50</In>", result)
                self.assertIn(b"<In>stay-in</In><MediaTimemapBA>stay-map</MediaTimemapBA>", result)
                self.assertNotIn(b"old-map-a", result)
                self.assertNotIn(b"old-map-b", result)
                self.assertNotIn(b"<In>old-a</In>", result)
                self.assertNotIn(b"<In>old-b</In>", result)
                self.assertIn(b"<Name>keep</Name>", result)
                timemaps = [decode_keyed_map(value) for value in re.findall(rb"<MediaTimemapBA>([0-9a-f]+)</MediaTimemapBA>", result)]
                graph_map, constant_map = timemaps[:2]
                self.assertAlmostEqual(graph_map["XMax"], 1371 / 50)
                self.assertAlmostEqual(graph_map["LastValidYOffset"], 499 / 25)
                self.assertAlmostEqual(constant_map["LastValidYOffset"], 499 / 25)
                self.assertAlmostEqual(constant_map["XMax"], (499 / 25) / 0.5)
                info = archive.getinfo("Other/keep.bin")
                self.assertEqual(info.date_time, (2020, 2, 3, 4, 5, 6))
                self.assertEqual(info.comment, b"member metadata")
                self.assertEqual(info.external_attr, 0o640 << 16)

    def test_ambiguous_timeline_match_rejects_without_changing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "ambiguous.drp"
            xml_path = Path(directory) / "source.xml"
            self.archive(archive_path, duplicate=True)
            xml_path.write_bytes(source_xml())
            before = hashlib.sha256(archive_path.read_bytes()).digest()
            with self.assertRaisesRegex(ValueError, "MediaPool timelines"):
                apply_timeline_state_to_archive(archive_path, xml_path)
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).digest(), before)

    def test_markers_only_xml_returns_without_requiring_media_or_fps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "markers.drp"
            xml_path = Path(directory) / "markers.xml"
            self.archive(archive_path)
            xml_path.write_bytes(b'<xmeml version="5"><sequence><name>Markers only</name><marker/></sequence></xmeml>')
            before = hashlib.sha256(archive_path.read_bytes()).digest()
            self.assertEqual(apply_timeline_state_to_archive(archive_path, xml_path), 0)
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).digest(), before)

    def test_empty_audio_only_timeline_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "audio.drp"
            xml_path = Path(directory) / "audio.xml"
            self.archive(archive_path)
            xml_path.write_bytes(b'<xmeml version="5"><sequence><name>Audio only</name><media><audio><track/></audio></media></sequence></xmeml>')
            self.assertEqual(apply_timeline_state_to_archive(archive_path, xml_path), 0)

    def test_same_coordinates_in_other_named_timeline_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "two-timelines.drp"
            xml_path = Path(directory) / "source.xml"
            original = self.archive(archive_path, other_timeline=True)
            xml_path.write_bytes(source_xml())
            self.assertEqual(apply_timeline_state_to_archive(archive_path, xml_path), 2)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read("SeqContainer/other.xml"), original["SeqContainer/other.xml"])
                target = archive.read("SeqContainer/seq.xml")
                self.assertIn(b"<In>260</In>", target)

    def test_timeline_names_preserve_significant_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "names.drp"
            xml_path = Path(directory) / "source.xml"
            members = self.archive(archive_path, other_timeline=True)
            members["MediaPool/Master/MpFolder.xml"] = members["MediaPool/Master/MpFolder.xml"].replace(b"Mixed Rate", b" Mixed Rate ")
            members["MediaPool/Other/MpFolder.xml"] = members["MediaPool/Other/MpFolder.xml"].replace(b"Another Timeline", b"Mixed Rate")
            with zipfile.ZipFile(archive_path, "w") as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
            xml_path.write_bytes(source_xml().replace(b"<name>Mixed Rate</name>", b"<name> Mixed Rate </name>"))
            self.assertEqual(apply_timeline_state_to_archive(archive_path, xml_path), 2)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read("SeqContainer/other.xml"), members["SeqContainer/other.xml"])

    def test_duplicate_clip_fields_reject_without_changing_archive(self) -> None:
        for field in ("In", "MediaTimemapBA"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                archive_path = Path(directory) / "duplicate.drp"
                xml_path = Path(directory) / "source.xml"
                members = self.archive(archive_path)
                closing = f"</{field}>".encode()
                members["SeqContainer/seq.xml"] = members["SeqContainer/seq.xml"].replace(closing, closing + f"<{field}>duplicate</{field}>".encode(), 1)
                with zipfile.ZipFile(archive_path, "w") as archive:
                    for name, payload in members.items():
                        archive.writestr(name, payload)
                xml_path.write_bytes(source_xml())
                before = archive_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "duplicate native clip"):
                    apply_timeline_state_to_archive(archive_path, xml_path)
                self.assertEqual(archive_path.read_bytes(), before)

    def test_unsupported_graph_fails_before_archive_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "unsupported.drp"
            xml_path = Path(directory) / "source.xml"
            self.archive(archive_path)
            xml_path.write_bytes(source_xml(bad_graph=True))
            before = hashlib.sha256(archive_path.read_bytes()).digest()
            with self.assertRaisesRegex(ValueError, "interpolation"):
                apply_timeline_state_to_archive(archive_path, xml_path)
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).digest(), before)

    def test_multiple_timelines_with_same_coordinates_are_corrected_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "multi.drp"
            first, second = Path(directory) / "first.xml", Path(directory) / "second.xml"
            self.archive(archive_path, other_timeline=True)
            first.write_bytes(source_xml())
            second.write_bytes(source_xml().replace(b"Mixed Rate", b"Another Timeline"))
            self.assertEqual(apply_timeline_state_to_archive(archive_path, [first, second]), 4)
            with zipfile.ZipFile(archive_path) as archive:
                for name in ("SeqContainer/seq.xml", "SeqContainer/other.xml"):
                    result = archive.read(name)
                    self.assertIn(b"<In>260</In>", result)
                    self.assertNotIn(b"old-map-a", result)

    def test_failure_in_later_timeline_leaves_entire_archive_unchanged(self) -> None:
        for failure in ("graph", "destination"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                archive_path = Path(directory) / "atomic.drp"
                first, second = Path(directory) / "first.xml", Path(directory) / "second.xml"
                self.archive(archive_path, other_timeline=True)
                first.write_bytes(source_xml())
                second.write_bytes(source_xml(bad_graph=failure == "graph").replace(
                    b"Mixed Rate", b"Missing Timeline" if failure == "destination" else b"Another Timeline",
                ))
                before = archive_path.read_bytes()
                with self.assertRaises(ValueError):
                    apply_timeline_state_to_archive(archive_path, [first, second])
                self.assertEqual(archive_path.read_bytes(), before)

    def test_missing_destination_match_fails_without_archive_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "missing.drp"
            xml_path = Path(directory) / "source.xml"
            self.archive(archive_path)
            xml_path.write_bytes(source_xml().replace(b"<start>1000</start>", b"<start>9000</start>", 1).replace(b"<end>1010</end>", b"<end>9010</end>", 1))
            before = hashlib.sha256(archive_path.read_bytes()).digest()
            with self.assertRaisesRegex(ValueError, "match counts"):
                apply_timeline_state_to_archive(archive_path, xml_path)
            self.assertEqual(hashlib.sha256(archive_path.read_bytes()).digest(), before)


if __name__ == "__main__":
    unittest.main()
