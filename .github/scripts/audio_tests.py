"""Regressions for measured Fairlight strips and source-channel selection."""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from conversion_tests import sequence
from pr2_native import _decode_wrapper_hex, _protobuf_fields, export_native_drp
from pr2_native_audio import decode_fields, encode_fields, mono_channel_mapping, rebuild_track_model


def audio_source() -> ET.Element:
    root = sequence(name="Audio", width=640, height=360)
    audio = root.find("sequence/media/audio")
    for channel in (1, 2):
        track = ET.SubElement(audio, "track")
        ET.SubElement(track, "name").text = "Source " + str(channel)
        clip = ET.SubElement(track, "clipitem", id="audio-" + str(channel))
        for key, value in (("name", "tone.wav"), ("start", "0"), ("end", "50"), ("in", "0"), ("out", "50")):
            ET.SubElement(clip, key).text = value
        source = ET.SubElement(clip, "sourcetrack")
        ET.SubElement(source, "mediatype").text = "audio"
        ET.SubElement(source, "trackindex").text = str(channel)
        file = ET.SubElement(clip, "file", id="audio-source")
        if channel == 1:
            for key, value in (("name", "tone.wav"), ("pathurl", "file:///media/tone.wav"), ("duration", "150")):
                ET.SubElement(file, key).text = value
            rate = ET.SubElement(file, "rate")
            ET.SubElement(rate, "timebase").text = "25"
            ET.SubElement(rate, "ntsc").text = "FALSE"
            media = ET.SubElement(ET.SubElement(file, "media"), "audio")
            ET.SubElement(media, "channelcount").text = "2"
            sample = ET.SubElement(media, "samplecharacteristics")
            ET.SubElement(sample, "samplerate").text = "48000"
    return root


def template_sequence_fields() -> bytes:
    path = Path(__file__).resolve().parents[2] / "resources/resolve21_native_template.drp"
    with zipfile.ZipFile(path) as archive:
        pool = ET.fromstring(archive.read("MediaPool/Master/MpFolder.xml").replace(b"ListMgt::", b"ListMgt__"))
    return bytes.fromhex(next(pool.iter("Sm2Sequence")).findtext("FieldsBlob"))


class AudioTests(unittest.TestCase):
    def test_source_channel_maps_match_resolve_authored_bytes(self) -> None:
        prefix = "000000010000000200000014004300680061006e006e0065006c0073004200410000000c000000000c0000000200000001000040"
        suffix = "000000120041007500640069006f0054007900700065000000020000000001"
        self.assertEqual(mono_channel_mapping(1), prefix + "01" + suffix)
        self.assertEqual(mono_channel_mapping(2), prefix + "02" + suffix)
        with self.assertRaises(ValueError):
            mono_channel_mapping(0)

    def test_standalone_audio_cache_and_timeline_channel_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            path = export_native_drp(audio_source(), Path(temp) / "audio.drp", "Audio")
            with zipfile.ZipFile(path) as archive:
                pool = ET.fromstring(archive.read("MediaPool/Master/MpFolder.xml").replace(b"ListMgt::", b"ListMgt__"))
                media = next(pool.iter("Sm2MpAudioClip"))
                info = next(media.iter("BtAudioInfo"))
                physical = decode_fields(bytes.fromhex(info.findtext("TracksBA")))
                track = decode_fields(physical["0"][1])
                self.assertEqual(int.from_bytes(track["SampleRate"][1], "big"), 48000)
                self.assertEqual(int.from_bytes(track["NumChannels"][1], "big"), 2)
                self.assertEqual(int.from_bytes(track["Duration"][1], "big"), 288000)
                fields = dict((number, value) for number, _, value in _protobuf_fields(_decode_wrapper_hex(media.findtext("FieldsBlob"))))
                self.assertEqual(fields[2], bytes.fromhex("090000000000003940"))
                nested = dict((number, value) for number, _, value in _protobuf_fields(fields[1]))
                specs = decode_fields(nested[5])
                metadata = decode_fields(specs["0"][1])
                self.assertEqual(struct.unpack("<dd", metadata["MediaExtents"][1]), (0.0, 6.0))
                channels = decode_fields(metadata["ChannelVecBA"][1])
                self.assertEqual(set(channels), {"0", "1"})
                for index in (0, 1):
                    channel = decode_fields(channels[str(index)][1])
                    self.assertEqual(channel["MediaRef"][1].decode("utf-16be"), info.get("DbId"))
                    if index:
                        self.assertEqual(int.from_bytes(channel["ChannelIdx"][1], "big"), 1)
                name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
                container = ET.fromstring(archive.read(name))
                for index, native_track in enumerate(container.findall("AudioTrackVec/Element/Sm2TiTrack"), 1):
                    self.assertEqual(native_track.findtext("SubType"), "1")
                    clip = next(native_track.iter("Sm2TiAudioClip"))
                    self.assertEqual(clip.findtext("VirtualAudioTrackBA"), mono_channel_mapping(index))
                    self.assertEqual(struct.unpack("<d", bytes.fromhex(clip.findtext("MediaFrameRate"))[:8])[0], 25.0)

    def test_track_models_keep_other_sequence_settings_and_register_each_channel(self) -> None:
        seed = template_sequence_fields()
        original = decode_fields(seed)
        for tracks, names, strip_ids in (
            ([("mono", "One", True, False)], ["One"], [0]),
            ([("mono", "甲", True, False), ("mono", "乙", True, False), ("mono", "丙", True, False)], ["甲", "乙", "丙"], [0, 1, 2]),
            ([("stereo", "LeftRight", True, False), ("mono", "Voice", True, False)], ["LeftRight", "LeftRight", "Voice"], [0, 0xFFFFFFFF, 1]),
        ):
            with self.subTest(tracks=tracks):
                rebuilt = decode_fields(rebuild_track_model(seed, tracks))
                self.assertEqual({key: value for key, value in rebuilt.items() if key != "FLStudioModelBA"},
                                 {key: value for key, value in original.items() if key != "FLStudioModelBA"})
                wrapped = rebuilt["FLStudioModelBA"][1]
                data = zlib.decompress(wrapped[4:])
                self.assertEqual(struct.unpack_from(">I", wrapped)[0], len(data))
                self.assertEqual(struct.unpack_from("<I", data, 8)[0], len(data) - 12)
                name_record = struct.pack("<III", 1, 0xBC, len(names))
                name_record += b"".join(struct.pack("<I", len(name.encode("utf-8"))) + name.encode("utf-8") for name in names)
                self.assertEqual(data.count(name_record), 1)
                strip_record = struct.pack("<III", 1, 0x458, len(strip_ids)) + struct.pack("<" + str(len(strip_ids)) + "I", *strip_ids)
                self.assertIn(strip_record, data)

    def test_mute_and_lock_match_independently_authored_fairlight_fields(self) -> None:
        tracks = [("mono", "One", True, False), ("mono", "Two", False, True), ("mono", "Three", True, False)]
        fields = decode_fields(rebuild_track_model(template_sequence_fields(), tracks))
        data = zlib.decompress(fields["FLStudioModelBA"][1][4:])
        # Resolve 21 SetTrackEnable and SetTrackLock captures, controlled
        # independently on the Edit page: two mute records, one lock record.
        mute_record = bytes.fromhex("010000000200000003000000000000000100000000000000")
        lock_record = bytes.fromhex("01000000db00000003000000000000000100000000000000")
        self.assertEqual(data.count(mute_record), 2)
        self.assertIn(lock_record, data)

    def test_invalid_source_channel_preserves_existing_output(self) -> None:
        root = audio_source()
        root.find("sequence/media/audio/track/clipitem/sourcetrack/trackindex").text = "3"
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            output = Path(temp) / "audio.drp"
            output.write_bytes(b"previous output")
            with self.assertRaisesRegex(ValueError, "declared source layout"):
                export_native_drp(root, output, "Audio")
            self.assertEqual(output.read_bytes(), b"previous output")

    def test_audio_fields_roundtrip_and_truncated_values_are_rejected(self) -> None:
        seed = template_sequence_fields()
        self.assertEqual(encode_fields(decode_fields(seed)), seed)
        for data in (seed[:5], seed[:-1]):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                decode_fields(data)


if __name__ == "__main__":
    unittest.main()
