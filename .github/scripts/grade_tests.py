"""Offline regression checks for ordered Lumetri extraction and grade planning."""

from __future__ import annotations

import base64
import copy
from dataclasses import replace
import gzip
import struct
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pr2_lumetri import LumetriClip, extract_lumetri_clips
from pr2_native_grade import attach_native_grade, build_lumetri_body, build_lumetri_drx, build_lumetri_node_plan, extract_drx_body


def _lumetri_blob() -> str:
    xml = b'''<Lumetri><guid>"g-1"</guid><version>"3"</version><name>""</name>
    <keyframe><time>"0"</time><tween>"0"</tween><easein>"1"</easein><easeout>"1"</easeout>
      <shader><name>"LUT"</name><customname>"Input LUT"</customname><visible>"1"</visible>
        <parameters><LUT>"example.cube"</LUT></parameters></shader>
      <shader><name>"BasicCorrection3"</name><customname>"Basic Correction"</customname><visible>"1"</visible>
        <parameters><Exposure>"D1"</Exposure><Contrast>"D20"</Contrast><Saturation>"D100"</Saturation></parameters></shader>
    </keyframe>
    <keyframe><time>"24"</time><tween>"2"</tween><easein>"1"</easein><easeout>"1"</easeout>
      <shader><name>"BasicCorrection3"</name><customname>"Basic Correction"</customname><visible>"1"</visible>
        <parameters><Exposure>"D2"</Exposure></parameters></shader>
    </keyframe></Lumetri>'''
    return base64.b64encode(xml).decode("ascii")


def _sample_project() -> ET.Element:
    blob = _lumetri_blob()
    xml = f'''<PremiereData>
      <Sequence ObjectUID="seq"><TrackGroups><TrackGroup><Second ObjectRef="tg"/></TrackGroup></TrackGroups></Sequence>
      <VideoTrackGroup ObjectID="tg"><TrackGroup><Tracks><Track ObjectURef="track"/></Tracks></TrackGroup></VideoTrackGroup>
      <VideoClipTrack ObjectUID="track"><ClipTrack><ClipItems><TrackItems><TrackItem ObjectRef="item"/></TrackItems></ClipItems></ClipTrack></VideoClipTrack>
      <TrackItem ObjectID="item" ObjectUID="item-u"><ClipTrackItem><SubClip ObjectRef="sub"/><TrackItem><Start>100</Start><End>200</End></TrackItem><ComponentOwner><Components ObjectRef="chain"/></ComponentOwner></ClipTrackItem></TrackItem>
      <SubClip ObjectID="sub"><Name>Clip A</Name></SubClip>
      <VideoComponentChain ObjectID="chain"><ComponentChain><Components>
        <Component ObjectRef="first"/><Component ObjectRef="second"/>
      </Components></ComponentChain></VideoComponentChain>
      <VideoFilterComponent ObjectID="first" ObjectUID="fx-1"><MatchName>AE.ADBE Lumetri</MatchName><InstanceName>First Grade</InstanceName><Params>
        <Param ObjectRef="p1"/><Param ObjectRef="p11"/><Param ObjectRef="p12"/><Param ObjectRef="p20"/><Param ObjectRef="p-extra"/>
      </Params></VideoFilterComponent>
      <VideoFilterComponent ObjectID="second" ObjectUID="fx-2"><MatchName>AE.ADBE Lumetri</MatchName><InstanceName>Second Grade</InstanceName><Params>
        <Param ObjectRef="p2"/><Param ObjectRef="p11b"/>
      </Params></VideoFilterComponent>
      <Param ObjectID="p1"><ParameterID>1</ParameterID><Name>Blob</Name><StartKeyframeValue>{blob}</StartKeyframeValue></Param>
      <Param ObjectID="p11"><ParameterID>11</ParameterID><Name>Exposure</Name><StartKeyframe>0,1</StartKeyframe><IsTimeVarying>false</IsTimeVarying></Param>
      <Param ObjectID="p12"><ParameterID>12</ParameterID><Name>Contrast</Name><StartKeyframe>0,20</StartKeyframe></Param>
      <Param ObjectID="p20"><ParameterID>20</ParameterID><Name>Saturation</Name><StartKeyframe>0,120</StartKeyframe></Param>
      <Param ObjectID="p-extra"><ParameterID>13</ParameterID><Name>Highlights</Name><StartKeyframe>0,4</StartKeyframe></Param>
      <Param ObjectID="p2"><ParameterID>1</ParameterID><Name>Blob</Name><StartKeyframeValue>{blob}</StartKeyframeValue></Param>
      <Param ObjectID="p11b"><ParameterID>11</ParameterID><Name>Exposure</Name><StartKeyframe>0,2</StartKeyframe><IsTimeVarying>true</IsTimeVarying></Param>
    </PremiereData>'''
    return ET.fromstring(xml)


def _static_clip() -> LumetriClip:
    clip = extract_lumetri_clips(_sample_project(), "seq")[0]
    first = clip.effects[0]
    assert first.blob is not None
    blob_root = ET.fromstring(base64.b64decode(first.blob.raw_base64))
    for keyframe in blob_root.findall("keyframe")[1:]:
        blob_root.remove(keyframe)
    static_xml = ET.tostring(blob_root, encoding="utf-8")
    static_blob = replace(
        first.blob, raw_base64=base64.b64encode(static_xml).decode("ascii"),
        decoded_xml=static_xml.decode("utf-8"), keyframes=first.blob.keyframes[:1],
    )
    return replace(clip, effects=(replace(first, blob=static_blob), *clip.effects[1:]))


def _pb_fields(data: bytes) -> list[tuple[int, int, object]]:
    fields: list[tuple[int, int, object]] = []
    offset = 0

    def varint() -> int:
        nonlocal offset
        value = shift = 0
        while True:
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value
            shift += 7

    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            length = varint()
            value = data[offset:offset + length]
            offset += length
        elif wire == 5:
            value = struct.unpack_from("<f", data, offset)[0]
            offset += 4
        elif wire == 1:
            value = data[offset:offset + 8]
            offset += 8
        else:
            raise AssertionError(f"unexpected protobuf wire type {wire}")
        fields.append((number, wire, value))
    return fields


def _field_values(fields: list[tuple[int, int, object]], number: int, wire: int | None = None) -> list[object]:
    return [value for field, field_wire, value in fields if field == number and (wire is None or field_wire == wire)]


def _decoded_grade_params(node: bytes) -> dict[int, float]:
    values: dict[int, float] = {}
    settings = _field_values(_pb_fields(node), 9, 2)
    for block in _field_values(_pb_fields(settings[0]), 1, 2) if settings else []:
        corrector = _pb_fields(block)
        block_type = _field_values(corrector, 1, 0)
        if not block_type or block_type[0] != 1:
            continue
        f6 = _field_values(corrector, 6, 2)
        container = _field_values(_pb_fields(f6[0]), 2, 2) if f6 else []
        for entry in _field_values(_pb_fields(container[0]), 3, 2) if container else []:
            entry_fields = _pb_fields(entry)
            ids = _field_values(entry_fields, 1, 0)
            encoded_value = _field_values(entry_fields, 2, 2)
            float_values = _field_values(_pb_fields(encoded_value[0]), 1, 5) if encoded_value else []
            if ids and float_values:
                values[int(ids[0])] = float(float_values[0])
    return values


class LumetriExtractionTests(unittest.TestCase):
    def test_keeps_effect_shader_keyframe_and_parameter_order(self) -> None:
        clips = extract_lumetri_clips(_sample_project(), "seq")
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertEqual([fx.component_ref for fx in clip.effects], ["first", "second"])
        self.assertEqual([p.parameter_id for p in clip.effects[0].parameters], ["1", "11", "12", "20", "13"])
        blob = clip.effects[0].blob
        self.assertIsNotNone(blob)
        assert blob is not None
        self.assertEqual([kf.time for kf in blob.keyframes], ["0", "24"])
        self.assertEqual([s.name for s in blob.keyframes[0].shaders], ["LUT", "BasicCorrection3"])
        self.assertEqual([p.name for p in blob.keyframes[0].shaders[1].parameters], ["Exposure", "Contrast", "Saturation"])
        self.assertIn('<name>"LUT"</name>', blob.keyframes[0].shaders[0].raw_xml)
        self.assertTrue(blob.raw_base64)
        self.assertIn("LUT", blob.decoded_xml)

    def test_basic_candidate_is_one_ordered_node_per_lumetri_with_losses(self) -> None:
        clip = _static_clip()
        plan = build_lumetri_node_plan(clip)
        self.assertEqual([node.source_component_ref for node in plan.nodes], ["first", "second"])
        self.assertEqual(plan.nodes[0].params, {"gain": {"master": 2.0}, "contrast": 1.2, "saturation": 60.0})
        self.assertEqual(plan.nodes[0].unmapped_parameter_ids, ("1", "13"))
        self.assertEqual(plan.nodes[0].unmapped_shader_names, ("LUT",))
        self.assertEqual(plan.nodes[1].params, {})
        self.assertIn("11", plan.nodes[1].unmapped_parameter_ids)

    def test_real_prproj_samples_match_existing_lumetri_inventory(self) -> None:
        root = Path(__file__).resolve().parents[2]
        sample = root / "test" / "荷花.prproj"
        if not sample.exists():
            self.skipTest("local sample is not present in this checkout")
        project = ET.fromstring(gzip.decompress(sample.read_bytes()))
        sequence = project.find("Sequence")
        assert sequence is not None
        clips = extract_lumetri_clips(project, sequence.get("ObjectUID", ""))
        self.assertEqual(len(clips), 9)
        first = clips[0].effects[0].blob
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.keyframes[0].shaders[2].name, "LUT")
        self.assertEqual(first.keyframes[0].shaders[4].name, "BasicCorrection3")

    def test_drx_body_has_serial_graph_and_decodable_values(self) -> None:
        clip = _static_clip()
        body = build_lumetri_body(clip, 1280, 720, timestamp=1700000000)
        self.assertEqual(body[0], 0x80)
        top = _pb_fields(body[1:])
        grade_root = _field_values(top, 1, 2)[0]
        container = _pb_fields(grade_root)
        nodes = _field_values(container, 7, 2)
        self.assertEqual(len(nodes), 2)
        node_fields = [_pb_fields(node) for node in nodes]
        self.assertEqual([_field_values(fields, 1, 0)[0] for fields in node_fields], [2, 3])
        self.assertEqual([_field_values(fields, 2, 0)[0] for fields in node_fields], [1, 2])
        self.assertEqual([_field_values(fields, 6, 2)[0].decode("utf-8") for fields in node_fields], ["First Grade", "Second Grade"])
        edges = [_pb_fields(edge) for edge in _field_values(container, 8, 2)]
        self.assertEqual(len(edges), 1)
        self.assertEqual((_field_values(edges[0], 1, 0)[0], _field_values(edges[0], 3, 0)[0]), (2, 3))
        self.assertEqual((_field_values(edges[0], 5, 0)[0], _field_values(edges[0], 6, 0)[0]), (64, 64))
        actual = _decoded_grade_params(nodes[0])
        self.assertEqual(set(actual), {100663328, 100663301, 2248147137})
        self.assertEqual(actual[100663328], 2.0)
        self.assertAlmostEqual(actual[100663301], 1.2)
        self.assertAlmostEqual(actual[2248147137], 1.2)
        self.assertEqual(_decoded_grade_params(nodes[1]), {})
        self.assertEqual(_field_values(container, 1, 0)[0], 3)
        input_markers = [_pb_fields(marker) for marker in _field_values(container, 9, 2)]
        output_markers = [_pb_fields(marker) for marker in _field_values(container, 10, 2)]
        self.assertEqual(_field_values(input_markers[0], 1, 0)[0], 8)
        self.assertEqual(_field_values(output_markers[0], 1, 0)[0], 10)

    def test_complete_drx_xml_and_native_body_share_exact_body_bytes(self) -> None:
        clip = _static_clip()
        payload = build_lumetri_drx(clip, 1280, 720, timestamp=1700000000)
        body = extract_drx_body(payload)
        self.assertEqual(body[0], 0x80)
        self.assertIn(b"<HasCorrection>true</HasCorrection>", payload)
        self.assertIn(b"<HasCorrection>false</HasCorrection>", payload)
        self.assertEqual(body, build_lumetri_body(clip, 1280, 720, timestamp=1700000000))

    def test_animated_blob_is_not_flattened_into_static_primary_values(self) -> None:
        clip = extract_lumetri_clips(_sample_project(), "seq")[0]
        plan = build_lumetri_node_plan(clip)
        self.assertEqual(plan.nodes[0].params, {})
        self.assertTrue(any("2 source keyframes" in note for note in plan.nodes[0].approximation_notes))

    def test_large_graph_node_ids_do_not_overlap_virtual_io_markers(self) -> None:
        clip = _static_clip()
        source_effect = clip.effects[0]
        for effect_count in (10, 20):
            effects = tuple(replace(source_effect, order=index) for index in range(effect_count))
            body = build_lumetri_body(replace(clip, effects=effects), timestamp=1700000000)
            container = _pb_fields(_field_values(_pb_fields(body[1:]), 1, 2)[0])
            node_bytes = _field_values(container, 7, 2)
            node_ids = [_field_values(_pb_fields(item), 1, 0)[0] for item in node_bytes]
            marker_ids = []
            for marker in _field_values(container, 9, 2) + _field_values(container, 10, 2):
                marker_fields = _pb_fields(marker)
                marker_ids.append(_field_values(marker_fields, 1, 0)[0])
                marker_inner = _field_values(marker_fields, 3, 2)[0]
                marker_ids.append(_field_values(_pb_fields(marker_inner), 1, 0)[0])
            self.assertEqual(len(node_ids), effect_count)
            self.assertEqual(len(node_ids), len(set(node_ids)))
            self.assertEqual(len(marker_ids), 4)
            self.assertEqual(len(marker_ids), len(set(marker_ids)))
            self.assertFalse(set(node_ids).intersection(marker_ids))
            edges = [_pb_fields(item) for item in _field_values(container, 8, 2)]
            self.assertEqual(len(edges), effect_count - 1)
            self.assertEqual(
                [(_field_values(edge, 1, 0)[0], _field_values(edge, 3, 0)[0]) for edge in edges],
                list(zip(node_ids, node_ids[1:])),
            )

    def test_attach_native_grade_creates_active_version_and_preserves_clip_fields(self) -> None:
        clip = _static_clip()
        drx_payload = build_lumetri_drx(clip, 1280, 720, timestamp=1700000000)
        native_clip = ET.fromstring('<Sm2TiVideoClip DbId="clip"><FieldsBlob>clip-original</FieldsBlob><Name>Shot</Name></Sm2TiVideoClip>')
        attach_native_grade(native_clip, drx_payload)
        native_xml = ET.tostring(native_clip, encoding="unicode").replace("ListMgt::", "ListMgt__")
        parsed = ET.fromstring(native_xml)
        self.assertEqual(parsed.findtext("FieldsBlob"), "clip-original")
        table = parsed.find("pLmVerTable/ListMgt__LmVersionTable")
        self.assertIsNotNone(table)
        assert table is not None
        self.assertEqual(table.findtext("VerType"), "0")
        fields_blob = bytes.fromhex(table.findtext("FieldsBlob", ""))
        self.assertEqual(int.from_bytes(fields_blob[:4], "big"), 1)
        self.assertEqual(int.from_bytes(fields_blob[4:8], "big"), 1)
        key_length = int.from_bytes(fields_blob[8:12], "big")
        self.assertEqual(key_length, len("ActiveVersionType".encode("utf-16-be")))
        key_start, key_end = 12, 12 + key_length
        self.assertEqual(fields_blob[key_start:key_end].decode("utf-16-be"), "ActiveVersionType")
        self.assertEqual(int.from_bytes(fields_blob[key_end:key_end + 4], "big"), 2)
        self.assertEqual(int.from_bytes(fields_blob[key_end + 4:key_end + 6], "big"), 0)
        local_versions = table.findall("Locals/Element/ListMgt__LmVersion")
        self.assertEqual(len(local_versions), 1)
        version = local_versions[0]
        self.assertEqual(table.findtext("pActive"), version.get("DbId"))
        self.assertEqual(version.findtext("HasCorrection"), "true")
        self.assertEqual(version.findtext("VerType"), "0")
        self.assertEqual(version.findtext("Body"), extract_drx_body(drx_payload).hex())

    def test_attach_grade_to_existing_version_table_preserves_prior_version_and_remote_ref(self) -> None:
        native_clip = ET.fromstring('''<Sm2TiVideoClip DbId="clip"><pLmVerTable><ListMgt__LmVersionTable DbId="table">
          <FieldsBlob>kept-fields</FieldsBlob><VerType>0</VerType><pRemoteTable>remote</pRemoteTable><pActive>old</pActive>
          <Locals><Element><ListMgt__LmVersion DbId="old"><HasCorrection>false</HasCorrection><Body>old-body</Body></ListMgt__LmVersion></Element></Locals>
        </ListMgt__LmVersionTable></pLmVerTable></Sm2TiVideoClip>''')
        clip = _static_clip()
        drx_payload = build_lumetri_drx(clip, timestamp=1700000000)
        attach_native_grade(native_clip, drx_payload)
        table = native_clip.find("pLmVerTable/ListMgt__LmVersionTable")
        self.assertIsNotNone(table)
        assert table is not None
        self.assertEqual(table.findtext("FieldsBlob"), "kept-fields")
        self.assertEqual(table.findtext("pRemoteTable"), "remote")
        versions = table.findall("Locals/Element/ListMgt__LmVersion")
        self.assertEqual([v.get("DbId") for v in versions[:1]], ["old"])
        self.assertEqual(versions[0].findtext("Body"), "old-body")
        self.assertEqual(table.findtext("pActive"), versions[-1].get("DbId"))
        self.assertEqual(versions[-1].findtext("Body"), extract_drx_body(drx_payload).hex())

    def test_attach_to_real_native_template_clip_scaffolds_version_table(self) -> None:
        project = Path(__file__).resolve().parents[2]
        template = project / "resources" / "resolve21_native_template.drp"
        with zipfile.ZipFile(template) as archive:
            seq_name = next(name for name in archive.namelist() if name.startswith("SeqContainer/") and name.endswith(".xml"))
            source_root = ET.fromstring(archive.read(seq_name).decode("utf-8").replace("ListMgt::", "ListMgt__"))
        target = next(clip for clip in source_root.iter("Sm2TiVideoClip") if clip.find("pLmVerTable") is None)
        clip_fields_blob = target.findtext("FieldsBlob", "")
        payload = build_lumetri_drx(_static_clip(), timestamp=1700000000)
        attach_native_grade(target, payload)
        reparsed = ET.fromstring(ET.tostring(source_root, encoding="unicode").replace("ListMgt::", "ListMgt__"))
        graded = next(clip for clip in reparsed.iter("Sm2TiVideoClip") if clip.get("DbId") == target.get("DbId"))
        self.assertEqual(graded.findtext("FieldsBlob", ""), clip_fields_blob)
        table = graded.find("pLmVerTable/ListMgt__LmVersionTable")
        self.assertIsNotNone(table)
        assert table is not None
        version = table.find("Locals/Element/ListMgt__LmVersion")
        self.assertIsNotNone(version)
        assert version is not None
        self.assertEqual(table.findtext("pActive"), version.get("DbId"))
        self.assertEqual(version.findtext("Body"), extract_drx_body(payload).hex())


if __name__ == "__main__":
    unittest.main()
