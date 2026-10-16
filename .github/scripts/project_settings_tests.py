"""Verify authored project defaults and their API export boundary."""

from __future__ import annotations

import contextlib
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, call, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as engine
import pr2_native as native
from conversion_tests import sequence
from grade_tests import _pb_fields
from pr2_native_project import sequence_project_settings, update_project_settings


def setup_payload(project: bytes) -> tuple[bytes, bytes]:
    root = ET.fromstring(project.replace(b"ListMgt::", b"ListMgt__"))
    fields = bytes.fromhex(root.findtext(".//SM_Config/FieldsBlob"))
    key_size = struct.unpack_from(">I", fields, 8)[0]
    offset = 12 + key_size
    kind, null, length = struct.unpack_from(">IBI", fields, offset)
    assert (kind, null) == (12, 0)
    value = fields[offset + 9:offset + 9 + length]
    assert struct.unpack_from(">II", value) == (1, len(value) - 8)
    assert value[8] == 128
    return value[9:], fields[offset + 9 + length:]


def project_fixture(payload: bytes, *, cache: bool) -> bytes:
    key = "SetupBA".encode("utf-16be")
    value = struct.pack(">II", 1, len(payload) + 1) + b"\x80" + payload
    fields = struct.pack(">III", 1, 2 if cache else 1, len(key)) + key
    fields += struct.pack(">IBI", 12, 0, len(value)) + value
    if cache:
        key = "LastModTimeForCache".encode("utf-16be")
        fields += struct.pack(">I", len(key)) + key + struct.pack(">IBq", 4, 0, 123)
    return b"<Project><SM_Config><FieldsBlob>" + fields.hex().encode() + b"</FieldsBlob></SM_Config><Keep>unchanged</Keep></Project>"


class ProjectSettingsTests(unittest.TestCase):
    def test_project_defaults_match_resolve_authored_fields_and_preserve_color(self) -> None:
        for ntsc, expected in ((False, bytes.fromhex("400e10800a18d0057832")), (True, bytes.fromhex("400e10800a18d005782e"))):
            with self.subTest(ntsc=ntsc):
                source = sequence(fps=24 if ntsc else 25, width=640, height=360).find("sequence")
                source.find("rate/ntsc").text = "TRUE" if ntsc else "FALSE"
                original = project_fixture(bytes.fromhex("400e"), cache=True)
                result = update_project_settings(original, source)
                payload, tail = setup_payload(result)
                # Project fields 2/3/15 are sint width/height/rate, measured via
                # Resolve 21 SetSetting followed by SaveProject/ExportProject.
                self.assertEqual(dict((number, value) for number, wire, value in _pb_fields(payload)),
                                 dict((number, value) for number, wire, value in _pb_fields(expected)))
                self.assertEqual(tail, setup_payload(original)[1])
                self.assertEqual(ET.fromstring(result).findtext("Keep"), "unchanged")
                self.assertEqual(sequence_project_settings(source)["timelineFrameRate"], "23.976" if ntsc else "25")

    def test_existing_defaults_are_replaced_without_duplicate_fields(self) -> None:
        source = sequence(fps=50, width=640, height=360).find("sequence")
        original = project_fixture(bytes.fromhex("10800a18d005400e7832"), cache=False)
        self.assertEqual(setup_payload(update_project_settings(original, source))[0], bytes.fromhex("10800a18d005400e7864"))

    def test_all_color_resources_keep_their_settings_when_project_defaults_change(self) -> None:
        source = sequence(width=640, height=360)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            for profile, resource in (({}, "default"), ({"WorkingColorSpace": "ACEScct"}, "acescct"), ({"WorkingColorSpace": "BT.2100 HLG"}, "hlg")):
                with self.subTest(profile=profile):
                    with zipfile.ZipFile(native.TEMPLATE) as archive:
                        baseline = archive.read(f"pr2/setup_{resource}.blob")
                    original, _ = setup_payload(b"<Project><SM_Config><FieldsBlob>" + baseline.hex().encode() + b"</FieldsBlob></SM_Config></Project>")
                    path = native.export_native_drp(source, Path(temp) / "project.drp", "Project", color_profiles=profile)
                    with zipfile.ZipFile(path) as archive:
                        payload, _ = setup_payload(archive.read("project.xml"))
                    values = {number: value for number, wire, value in _pb_fields(payload) if wire == 0}
                    self.assertEqual([values[number] for number in (2, 3, 15)], [1280, 720, 50])
                    self.assertEqual([field for field in _pb_fields(payload) if field[0] not in (2, 3, 15)],
                                     [field for field in _pb_fields(original) if field[0] not in (2, 3, 15)])

    def test_api_defaults_are_applied_before_media_import_and_rejections_abort(self) -> None:
        for kind in ("single-drt", "batch-drt", "drp"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                xml = folder / "source.xml"
                ET.ElementTree(sequence(width=640, height=360)).write(xml)
                resolve = Mock()
                manager = resolve.GetProjectManager.return_value
                manager.GetCurrentProject.return_value = None
                manager.GetProjectListInCurrentFolder.return_value = []
                project = manager.CreateProject.return_value
                project.SetSetting.return_value = False
                with patch.object(engine, "_shutdown_resolve"), contextlib.redirect_stdout(io.StringIO()):
                    if kind == "single-drt":
                        result = engine._drt_sandbox_export(resolve, xml, folder / "out.drt")
                    elif kind == "batch-drt":
                        result = engine._drt_batch_export(resolve, [xml], folder, ["Timeline"])[0][0]
                    else:
                        result = engine._drp_export(resolve, [xml], folder / "out.drp", "Project", ["Timeline"])
                self.assertFalse(result)
                project.GetMediaPool.assert_not_called()
                manager.ExportProject.assert_not_called()
                self.assertTrue(xml.exists())
                project.SetSetting.assert_has_calls([
                    call("timelineFrameRate", "25"), call("timelineResolutionWidth", "640"), call("timelineResolutionHeight", "360"),
                ])

    def test_unrecognized_setup_envelope_is_rejected_without_modifying_the_input(self) -> None:
        source = sequence().find("sequence")
        data = project_fixture(bytes.fromhex("400e"), cache=False)
        # Corrupt the raw marker directly; the original remains retryable.
        fields = bytearray.fromhex(ET.fromstring(data).findtext(".//FieldsBlob"))
        fields[43] = 129
        bad = ET.fromstring(data)
        bad.find(".//FieldsBlob").text = fields.hex()
        with self.assertRaisesRegex(ValueError, "raw envelope"):
            update_project_settings(ET.tostring(bad), source)
        self.assertEqual(setup_payload(data)[0], bytes.fromhex("400e"))


if __name__ == "__main__":
    unittest.main()
