#!/usr/bin/env python3
"""Small public smoke tests for project-data parsing and native DRP export."""
from __future__ import annotations

import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pr2_engine as pe  # noqa: E402
from pr2_native import TEMPLATE, export_native_drp  # noqa: E402


def test_prproj_logging_info_metadata() -> None:
    project = ET.Element("PremiereData")
    master = ET.SubElement(project, "MasterClip", ObjectUID="master-1")
    ET.SubElement(master, "LoggingInfo", ObjectRef="logging-1")
    logging = ET.SubElement(project, "ClipLoggingInfo", ObjectID="logging-1")
    ET.SubElement(logging, "MediaFrameRate").text = "10584000000"
    ET.SubElement(logging, "MediaInPoint").text = "254016000000"
    ET.SubElement(logging, "MediaOutPoint").text = "508032000000"
    info = pe._prproj_extract_source_tc_info(master, pe._PrprojIndex.build(project))
    assert info.media_fps_resolved is True
    assert info.media_fps == 24.0
    assert info.timecode_frame == 24
    assert info.duration_resolved is True
    assert info.full_duration_frames == 24


def test_native_template_and_minimal_export() -> None:
    assert TEMPLATE.is_file()
    with zipfile.ZipFile(TEMPLATE) as archive:
        members = set(archive.namelist())
    assert "project.xml" in members
    assert "MediaPool/Master/MpFolder.xml" in members

    sequence = ET.fromstring(
        """<xmeml version="5"><sequence><name>Smoke</name>
        <duration>24</duration><rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
        <media><video><track><clipitem id="v1"><name>neutral.mov</name>
        <start>0</start><end>24</end><in>0</in><out>24</out>
        <file id="f1"><name>neutral.mov</name><pathurl>neutral.mov</pathurl>
        <duration>24</duration></file></clipitem></track></video></media>
        </sequence></xmeml>"""
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        output = export_native_drp(sequence, Path(temp_dir) / "smoke.drp", "Smoke")
        assert output.is_file()
        with zipfile.ZipFile(output) as archive:
            assert any(name.startswith("SeqContainer/") for name in archive.namelist())


def main() -> int:
    tests = [test_prproj_logging_info_metadata, test_native_template_and_minimal_export]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"{len(tests)} smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
