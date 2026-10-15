"""Portable regressions for source identity and offline native conversion."""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as pe
import pr2_native as pn
import pr2resolve as cli
from pr2_constants import ProjectBin, load_xml


def sequence(name: str = "Timeline", uid: str = "source-sequence", fps: int = 25,
             width: int = 1080, height: int = 1920) -> ET.Element:
    root = ET.fromstring(f'''<xmeml version="5"><sequence id="{uid}">
        <duration>50</duration><rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>
        <media><video><format><samplecharacteristics><width>{width}</width>
        <height>{height}</height></samplecharacteristics></format><track>
        <clipitem id="clip-1"><name>duplicate.mov</name><start>0</start><end>50</end>
        <in>0</in><out>50</out><file id="media-1"><name>duplicate.mov</name>
        <pathurl>file://localhost/media/one/duplicate.mov</pathurl><duration>100</duration>
        <rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate><timecode><frame>250</frame></timecode>
        <media><video><samplecharacteristics><width>1920</width><height>1080</height>
        </samplecharacteristics></video></media></file></clipitem>
        </track></video><audio/></media></sequence></xmeml>''')
    ET.SubElement(root.find("sequence"), "name").text = name
    return root


def archive_xml(data: bytes) -> ET.Element:
    return ET.fromstring(data.decode("utf-8").replace("ListMgt::", "ListMgt__"))


def native_members(trees: list[ET.Element], bins: list[ProjectBin] | None = None) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory() as temp:
        output = pn.export_native_drp(trees, Path(temp) / "project.drp", r"Project & <review> \g", bin_members=bins)
        with zipfile.ZipFile(output) as archive:
            return {name: archive.read(name) for name in archive.namelist()}


def motion_project() -> ET.Element:
    return ET.fromstring('''<PremiereData><Sequence ObjectUID="sequence"><Name>Sample</Name>
      <TrackGroups><TrackGroup><Second ObjectRef="group"/></TrackGroup></TrackGroups></Sequence>
      <VideoTrackGroup ObjectID="group"><FrameRect>0,0,1920,1080</FrameRect><TrackGroup>
      <FrameRate>10160640000</FrameRate><Tracks><Track ObjectURef="track"/></Tracks></TrackGroup></VideoTrackGroup>
      <VideoClipTrack ObjectUID="track"><ClipTrack><Track><TrackItems><TrackItem ObjectRef="item"/>
      </TrackItems></Track></ClipTrack></VideoClipTrack>
      <VideoClipTrackItem ObjectID="item"><ClipTrackItem><TrackItem><Start>0</Start><End>254016000000</End></TrackItem>
      <ComponentOwner><Components ObjectRef="chain"/></ComponentOwner></ClipTrackItem></VideoClipTrackItem>
      <VideoComponentChain ObjectID="chain"><ComponentChain><Components><Component ObjectRef="motion"/>
      </Components></ComponentChain></VideoComponentChain><VideoFilterComponent ObjectID="motion">
      <MatchName>AE.ADBE Motion</MatchName><Params><Param ObjectRef="scale"/><Param ObjectRef="rotate"/>
      <Param ObjectRef="position"/></Params></VideoFilterComponent>
      <VideoComponentParam ObjectID="scale"><ParameterID>2</ParameterID><Name>缩放</Name>
      <StartKeyframe>0,146,0,0,0</StartKeyframe><Keyframes>0,100,0,0;254016000000,146,0,0;</Keyframes></VideoComponentParam>
      <VideoComponentParam ObjectID="rotate"><ParameterID>5</ParameterID><Name>旋转</Name>
      <StartKeyframe>0,270,0,0,0</StartKeyframe></VideoComponentParam>
      <VideoComponentParam ObjectID="position"><ParameterID>1</ParameterID><Name>位置</Name>
      <StartKeyframe>0,1.5:0.25,0,0,0</StartKeyframe></VideoComponentParam></PremiereData>''')


class SourceTests(unittest.TestCase):
    def test_fractional_playback_speed_is_not_rounded_to_whole_percent(self) -> None:
        project = motion_project()
        ET.SubElement(project.find(".//ClipTrackItem"), "SubClip", ObjectRef="subclip")
        subclip = ET.SubElement(project, "SubClip", ObjectID="subclip")
        ET.SubElement(subclip, "Clip", ObjectRef="video")
        video = ET.SubElement(project, "VideoClip", ObjectID="video")
        ET.SubElement(ET.SubElement(video, "Clip"), "PlaybackSpeed").text = "0.80125"
        clip, _ = pe._extract_clip(project.find(".//ClipTrackItem"), pe._PrprojIndex.build(project), project, 1920, 1080, 25.0)
        self.assertAlmostEqual(clip.playback_speed, 80.125)

    def test_inventory_includes_later_video_tracks_and_audio_only(self) -> None:
        root = ET.fromstring('''<PremiereData><Sequence ObjectUID="s"><Name>S</Name><TrackGroups>
          <TrackGroup><Second ObjectRef="v"/></TrackGroup><TrackGroup><Second ObjectRef="a"/></TrackGroup>
          </TrackGroups></Sequence><VideoTrackGroup ObjectID="v"><Tracks><Track ObjectURef="empty"/>
          <Track ObjectURef="later"/></Tracks></VideoTrackGroup><AudioTrackGroup ObjectID="a"><Tracks>
          <Track ObjectURef="audio"/></Tracks></AudioTrackGroup><VideoClipTrack ObjectUID="empty"/>
          <VideoClipTrack ObjectUID="later"><TrackItems><TrackItem ObjectRef="1"/></TrackItems></VideoClipTrack>
          <AudioClipTrack ObjectUID="audio"><TrackItems><TrackItem ObjectRef="2"/></TrackItems></AudioClipTrack></PremiereData>''')
        self.assertEqual(pe._prproj_list_sequences(root, pe._PrprojIndex.build(root))[0]["clip_count"], 2)
        root.remove(root.find("VideoTrackGroup"))
        self.assertEqual(pe._prproj_list_sequences(root, pe._PrprojIndex.build(root))[0]["clip_count"], 1)

    def test_localized_motion_and_outside_frame_position(self) -> None:
        root = motion_project()
        clip, motion = pe._extract_clip(root.find(".//ClipTrackItem"), pe._PrprojIndex.build(root), root, 1920, 1080, 25.0)
        self.assertEqual((clip.scale, clip.rotation), (146.0, 270.0))
        self.assertEqual(motion["center"], [1.0, -0.25])
        self.assertEqual([(point["when"], point["value"]) for point in motion["keyframes"]["scale"]], [(0, 100.0), (25, 146.0)])

    def test_animated_xml_keeps_curve_without_static_override(self) -> None:
        root = pe._prproj_parse_sequence(motion_project(), "sequence", Path("sample.prproj"))
        self.assertEqual(root.find("sequence").get("id"), "sequence")
        parameter = root.find(".//parameter[parameterid='scale']")
        self.assertIsNotNone(parameter)
        self.assertIsNone(parameter.find("value"))
        self.assertEqual([k.findtext("value") for k in parameter.findall("keyframe")], ["100.0", "146.0"])
        self.assertEqual(parameter.findtext("keyframe/interpolation/name"), "linear")
        self.assertIn("clip-motion:", str(pn._timeline_state_manifest(root)))
        before = ET.tostring(parameter)
        pe._apply_fixes(root, pe._scan(root))
        self.assertEqual(ET.tostring(parameter), before)

    def test_state_comment_survives_xml_save_and_load(self) -> None:
        root = pe._prproj_parse_sequence(motion_project(), "sequence", Path("sample.prproj"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "converted.xml"
            pe._write_fixed_xml(root, path)
            self.assertEqual(pn._timeline_state_manifest(load_xml(path)), pn._timeline_state_manifest(root))

    def test_unknown_connected_effect_survives_normalization_and_blocks_native(self) -> None:
        project = motion_project()
        component = ET.SubElement(project, "VideoFilterComponent", ObjectID="unsupported")
        ET.SubElement(component, "MatchName").text = "AE.ADBE Turbulent Displace"
        ET.SubElement(project.find("VideoComponentChain/ComponentChain/Components"), "Component", ObjectRef="unsupported")
        root = pe._prproj_parse_sequence(project, "sequence", Path("sample.prproj"))
        pe._apply_fixes(root, pe._scan(root))
        state = pn._timeline_state_manifest(root)
        self.assertIn("AE.ADBE Turbulent Displace", str(state))
        with self.assertRaisesRegex(ValueError, "unconverted Premiere AE.ADBE Turbulent Displace"):
            pn._validate_sequence_capabilities(root)

    def test_static_opacity_is_written_and_default_opacity_is_not_flagged(self) -> None:
        project = motion_project()
        ET.SubElement(project.find("VideoComponentChain/ComponentChain/Components"), "Component", ObjectRef="opacity")
        component = ET.SubElement(project, "VideoFilterComponent", ObjectID="opacity")
        ET.SubElement(component, "MatchName").text = "AE.ADBE Opacity"
        params = ET.SubElement(component, "Params")
        ET.SubElement(params, "Param", ObjectRef="opacity-value")
        parameter = ET.SubElement(project, "VideoComponentParam", ObjectID="opacity-value")
        ET.SubElement(parameter, "ParameterID").text = "1"
        start = ET.SubElement(parameter, "StartKeyframe")
        start.text = "0,54,0,0"
        root = pe._prproj_parse_sequence(project, "sequence", Path("sample.prproj"))
        self.assertEqual(root.findtext(".//effect[effectid='opacity']/parameter[parameterid='opacity']/value"), "54.0")
        start.text = "0,100,0,0"
        root = pe._prproj_parse_sequence(project, "sequence", Path("sample.prproj"))
        self.assertFalse(any(key.startswith("unconverted-source:") for key, _ in pn._timeline_state_manifest(root).get("unknown", [])))

    def test_nonempty_caption_tracks_are_preserved_as_unconverted_source(self) -> None:
        project = motion_project()
        group_ref = ET.SubElement(project.find("Sequence/TrackGroups"), "TrackGroup")
        ET.SubElement(group_ref, "Second", ObjectRef="data")
        group = ET.SubElement(project, "DataTrackGroup", ObjectID="data")
        ET.SubElement(ET.SubElement(group, "Tracks"), "Track", ObjectURef="captions")
        track = ET.SubElement(project, "CaptionDataClipTrack", ObjectUID="captions")
        ET.SubElement(ET.SubElement(track, "TrackItems"), "TrackItem", ObjectRef="caption")
        ET.SubElement(project, "CaptionDataClipTrackItem", ObjectID="caption")
        root = pe._prproj_parse_sequence(project, "sequence", Path("sample.prproj"))
        self.assertIn("unconverted-data-track", str(pn._timeline_state_manifest(root)))
        with self.assertRaisesRegex(ValueError, "unconverted-data-track"):
            pn._validate_sequence_capabilities(root)


class NativeProjectTests(unittest.TestCase):
    def test_native_retime_uses_source_in_in_record_domain(self) -> None:
        root = sequence()
        clip = root.find(".//clipitem")
        clip.find("in").text = "25"
        effect = ET.SubElement(ET.SubElement(clip, "filter"), "effect")
        ET.SubElement(effect, "effectid").text = "timeremap"
        parameter = ET.SubElement(effect, "parameter")
        ET.SubElement(parameter, "parameterid").text = "speed"
        ET.SubElement(parameter, "value").text = "50"
        members = native_members([root])
        clips = [c for name, data in members.items() if name.startswith("SeqContainer/") for c in archive_xml(data).iter("Sm2TiVideoClip")]
        self.assertEqual(clips[0].findtext("In"), "50")
        self.assertEqual(clips[0].findtext("Duration"), "50")
        self.assertTrue(clips[0].findtext("MediaTimemapBA").startswith("00000001"))
    def test_same_path_with_different_source_interpretations_is_not_deduplicated(self) -> None:
        first, second = sequence("One", "one", 25), sequence("Two", "two", 30)
        pool = archive_xml(native_members([first, second])["MediaPool/Master/MpFolder.xml"])
        media = list(pool.iter("Sm2MpVideoClip"))
        self.assertEqual(len(media), 2)
        self.assertEqual(len({n.findtext(".//BtVideoInfo/Time") for n in media}), 2)

    def test_static_motion_has_source_values_without_captured_pan_or_animation(self) -> None:
        clip = ET.fromstring('''<clipitem><filter><effect><effectid>basic</effectid>
            <parameter><parameterid>scale</parameterid><value>123.45</value></parameter>
            <parameter><parameterid>rotation</parameterid><value>37</value></parameter>
            <parameter><parameterid>center</parameterid><value><horiz>0</horiz><vert>0</vert></value></parameter>
            </effect></filter></clipitem>''')
        payload = pn._decode_wrapper_hex(pn._static_motion_payload(clip, (1920, 1080), (1920, 1080)))
        sections = pn._protobuf_fields(payload)
        self.assertEqual(len(sections), 1)
        values = {}
        for number, wire, raw in pn._protobuf_fields(sections[0][2]):
            if number != 9 or wire != 2 or not raw:
                continue
            fields = pn._protobuf_fields(raw)
            parameter_id = next(value for number, _, value in fields if number == 1)
            curve = next(value for number, _, value in fields if number == 3)
            keyframes = pn._protobuf_fields(curve)
            self.assertEqual(len(keyframes), 1)
            point = pn._protobuf_fields(keyframes[0][2])
            self.assertEqual([number for number, _, _ in point], [2])
            values[parameter_id] = struct.unpack("<d", point[0][2])[0]
        self.assertEqual(values, {42: 1.2345, 43: 1.2345, 40: 0.0, 41: 0.0, 47: -37.0})

    def test_nondefault_motion_requires_geometry_and_animation_is_not_flattened(self) -> None:
        clip = ET.fromstring('''<clipitem><filter><effect><effectid>basic</effectid>
            <parameter><parameterid>scale</parameterid><value>146</value></parameter>
            </effect></filter></clipitem>''')
        with self.assertRaisesRegex(ValueError, "dimensions"):
            pn._static_motion_payload(clip, (0, 0), (1920, 1080))
        ET.SubElement(clip.find(".//parameter"), "keyframe")
        with self.assertRaisesRegex(ValueError, "animated"):
            pn._static_motion_payload(clip, (1920, 1080), (1920, 1080))

    def test_selected_timelines_have_independent_settings_and_registered_handles(self) -> None:
        first, second = sequence(r"一 & <A> \g", "one", 25), sequence("Two", "two", 30, 675, 1200)
        members = native_members([first, second])
        pool = archive_xml(members["MediaPool/Master/MpFolder.xml"])
        timelines = list(pool.iter("Sm2MpTimelineClip"))
        self.assertEqual([n.findtext("Name") for n in timelines], [r"一 & <A> \g", "Two"])
        native = list(pool.iter("Sm2Sequence"))
        self.assertEqual([struct.unpack("<d", bytes.fromhex(n.findtext("FrameRate"))[:8])[0] for n in native], [25.0, 30.0])
        self.assertEqual([struct.unpack(">QQ", bytes.fromhex(n.findtext("Resolution"))) for n in native], [(1080, 1920), (675, 1200)])
        self.assertEqual([struct.unpack("<dd", bytes.fromhex(n.findtext("MediaExtents"))) for n in native], [(0.0, 2.0), (0.0, 50 / 30)])
        project = archive_xml(members["project.xml"])
        self.assertEqual([n.text for n in project.findall("TimelineHandleVec/Element")], [n.findtext("Parent") for n in native])
        sequence_ids = {n.get("DbId") for n in native}
        for name, data in members.items():
            if name.startswith("SeqContainer/"):
                self.assertTrue({t.findtext("Sequence") for t in archive_xml(data).iter("Sm2TiTrack")} <= sequence_ids)

    def test_id_only_file_references_and_duplicate_display_names(self) -> None:
        root = sequence()
        track = root.find("sequence/media/video/track")
        first = track.find("clipitem")
        second = ET.fromstring(ET.tostring(first))
        second.set("id", "other")
        second.find("file").set("id", "media-2")
        second.find("file/pathurl").text = "file://localhost/media/two/duplicate.mov"
        definition = first.find("file")
        first.remove(definition)
        ET.SubElement(first, "file", id="media-1")
        root.append(definition)
        track.append(second)
        members = native_members([root])
        pool = archive_xml(members["MediaPool/Master/MpFolder.xml"])
        media = list(pool.iter("Sm2MpVideoClip"))
        self.assertEqual(len(media), 2)
        self.assertEqual(len({n.get("DbId") for n in pool.iter() if n.tag == "BtVideoInfo"}), 2)
        clips = [c for name, data in members.items() if name.startswith("SeqContainer/") for c in archive_xml(data).iter("Sm2TiVideoClip")]
        self.assertEqual(len({c.findtext("MediaRef") for c in clips}), 2)
        self.assertEqual({c.findtext("MediaRef") for c in clips}, {c.get("DbId") for c in media})

    def test_nested_bins_use_paths_and_source_uid_instead_of_display_name(self) -> None:
        bins = [ProjectBin("parent", "Folder"), ProjectBin("child", "Folder", "parent", ["/media/one/duplicate.mov"], ["one"])]
        members = native_members([sequence("Same", "one"), sequence("Same", "two")], bins)
        folders = {name: archive_xml(data) for name, data in members.items() if name.endswith("MpFolder.xml")}
        child_path = next(name for name in folders if name.count("/") == 4)
        child = folders[child_path]
        parent = folders[child_path.rsplit("/", 2)[0] + "/MpFolder.xml"]
        self.assertEqual(child.findtext("MpFolder"), parent.get("DbId"))
        self.assertEqual(len(list(child.iter("Sm2MpVideoClip"))), 1)
        self.assertEqual(len(list(child.iter("Sm2MpTimelineClip"))), 1)
        master = folders["MediaPool/Master/MpFolder.xml"]
        self.assertEqual(len(list(master.iter("Sm2MpTimelineClip"))), 1)

    def test_repeated_media_membership_keeps_an_item_in_each_bin(self) -> None:
        path = "/media/one/duplicate.mov"
        bins = [ProjectBin("a", "A", media_paths=[path]), ProjectBin("b", "B", media_paths=[path])]
        members = native_members([sequence()], bins)
        folders = [archive_xml(data) for name, data in members.items() if name.endswith("MpFolder.xml") and name != "MediaPool/Master/MpFolder.xml"]
        media = [list(folder.iter("Sm2MpVideoClip")) for folder in folders]
        self.assertEqual([len(items) for items in media], [1, 1])
        self.assertEqual(len({items[0].get("DbId") for items in media}), 2)
        self.assertEqual(len({items[0].find(".//BtVideoInfo").get("DbId") for items in media}), 2)

    def test_cached_source_timecode_rate_and_geometry_are_written(self) -> None:
        pool = archive_xml(native_members([sequence()])["MediaPool/Master/MpFolder.xml"])
        info = pool.find(".//BtVideoInfo")
        time = bytes.fromhex(info.findtext("Time"))
        frame_marker = "StartFrame".encode("utf-16be") + struct.pack(">IB", 2, 0)
        offset = time.index(frame_marker) + len(frame_marker)
        self.assertEqual(struct.unpack_from(">i", time, offset)[0], 250)
        rate_marker = "FrameRate".encode("utf-16be") + struct.pack(">IBI", 12, 0, 16)
        self.assertEqual(struct.unpack_from("<d", time, time.index(rate_marker) + len(rate_marker))[0], 25.0)

    def test_native_only_cli_report_survives_failed_native_export(self) -> None:
        root = sequence()
        ET.SubElement(root.find(".//clipitem"), "generatoritem")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            input_path = folder / "input.xml"
            ET.ElementTree(root).write(input_path, encoding="utf-8")
            with patch.object(cli, "_shutdown_resolve", side_effect=AssertionError("desktop interaction")), contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(input_path, folder / "out", report=True, native_drp=True, no_xml=True)
            self.assertEqual(result, 1)
            reports = list((folder / "out").glob("*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("Conversion fidelity", reports[0].read_text(encoding="utf-8"))
            self.assertEqual(list((folder / "out").glob("*.xml")), [])

    def test_native_only_cli_avoids_resolve_lifecycle_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            input_path = folder / "input.xml"
            ET.ElementTree(sequence()).write(input_path, encoding="utf-8")
            with patch.object(cli, "_shutdown_resolve", side_effect=AssertionError("desktop interaction")), contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(input_path, folder / "out", report=True, native_drp=True, no_xml=True)
            self.assertEqual(result, 0)
            self.assertEqual(len(list((folder / "out").glob("*.drp"))), 1)
            self.assertEqual(len(list((folder / "out").glob("*.md"))), 1)
            self.assertEqual(list((folder / "out").glob("*.xml")), [])


class BatchTests(unittest.TestCase):
    def run_batch(self, *, diagnose: bool = False) -> list[str]:
        project = motion_project()
        other = ET.fromstring(ET.tostring(project.find("Sequence")))
        other.set("ObjectUID", "second")
        project.append(other)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(cli, "load_prproj", return_value=project), patch.object(cli, "_shutdown_resolve", side_effect=AssertionError("desktop interaction")), contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(folder / "sample.prproj", folder / "out", all_sequences=True, diagnose_only=diagnose, report=True)
            self.assertEqual(result, 0)
            return [p.name for p in (folder / "out").iterdir()]

    def test_batch_xml_and_reports_survive_with_duplicate_sequence_names(self) -> None:
        files = self.run_batch()
        self.assertEqual(len([name for name in files if name.endswith(".xml")]), 2)
        self.assertEqual(len([name for name in files if name.endswith(".md")]), 2)

    def test_batch_diagnose_only_does_not_write_conversion_files(self) -> None:
        self.assertEqual(self.run_batch(diagnose=True), [])

    def test_batch_drt_does_not_repeat_single_export_or_reuse_deleted_xml(self) -> None:
        project = motion_project()
        other = ET.fromstring(ET.tostring(project.find("Sequence")))
        other.set("ObjectUID", "second")
        project.append(other)

        def export_batch(resolve: object, paths: list[Path], *args: object) -> list[tuple[bool, str]]:
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(path.is_file() for path in paths))
            return [(True, "one.drt"), (True, "two.drt")]

        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            folder = Path(temp)
            stack.enter_context(patch.object(cli, "load_prproj", return_value=project))
            ensure = stack.enter_context(patch.object(cli, "_ensure_resolve_running", return_value=object()))
            stack.enter_context(patch.object(cli, "_drt_batch_export", side_effect=export_batch))
            stack.enter_context(patch.object(cli, "_check_resolve_running", side_effect=AssertionError("duplicate Resolve access")))
            stack.enter_context(patch.object(cli, "_shutdown_resolve"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = cli._run_pipeline(folder / "sample.prproj", folder / "out", all_sequences=True, drt=True, no_xml=True)
            self.assertEqual(result, 0)
            ensure.assert_called_once()
            self.assertEqual(list((folder / "out").glob("*.xml")), [])


if __name__ == "__main__":
    unittest.main()
