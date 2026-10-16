"""Check that API color requests retain source sequence identity."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, call, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as engine
from conversion_tests import sequence


class ColorPipelineTests(unittest.TestCase):
    def test_media_input_requests_exclude_unselected_sequences(self) -> None:
        source = ET.fromstring('''<PremiereData>
          <Sequence ObjectUID="picked"><Name>Picked</Name><Media ObjectRef="picked-media"/></Sequence>
          <Sequence ObjectUID="other"><Name>Other</Name><Media ObjectRef="other-media"/></Sequence>
          <Media ObjectID="picked-media"><Color ObjectRef="picked-color"/></Media>
          <Media ObjectID="other-media"><Color ObjectRef="other-color"/></Media>
          <ColorSpace ObjectID="picked-color"><OriginalColorSpace>BT.709</OriginalColorSpace><OverriddenColorSpace>Apple Log/Rec. 2020</OverriddenColorSpace></ColorSpace>
          <ColorSpace ObjectID="other-color"><OriginalColorSpace>BT.2100 PQ</OriginalColorSpace></ColorSpace>
        </PremiereData>''')
        resolve = Mock()
        manager = resolve.GetProjectManager.return_value
        manager.GetCurrentProject.return_value = None
        manager.GetProjectListInCurrentFolder.return_value = []
        manager.CreateProject.return_value.SetSetting.return_value = True
        pool = manager.CreateProject.return_value.GetMediaPool.return_value
        pool.ImportTimelineFromFile.return_value.GetStartFrame.return_value = 0
        selected, other = Mock(), Mock()
        pool.GetRootFolder.return_value.GetClipList.return_value = [selected, other]
        pool.GetRootFolder.return_value.GetSubFolderList.return_value = []
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(temp)
            for media, item, name in zip(source.findall("Media"), (selected, other), ("picked.mov", "other.mov")):
                path = str(folder / name)
                ET.SubElement(media, "FilePath").text = path
                item.GetClipProperty.return_value = path
                item.SetClipProperty.return_value = True
            xml = folder / "picked.xml"
            ET.ElementTree(sequence("Picked", "picked")).write(xml, encoding="utf-8")
            # No retimes: the export callback is the external boundary here.
            manager.ExportProject.return_value = True
            with patch.object(engine, "_shutdown_resolve"):
                self.assertTrue(engine._drp_export(resolve, [xml], folder / "out.drp", "Project", ["Picked"], prproj_root=source))
        selected.SetClipProperty.assert_called_once_with("Input Color Space", "Apple Log")
        other.SetClipProperty.assert_not_called()

    def test_duplicate_names_use_each_source_uid_for_project_and_timeline_color(self) -> None:
        source = ET.fromstring('''<PremiereData>
          <Sequence ObjectUID="aces"><Name>Same</Name><WorkingColorSpace>ACEScct</WorkingColorSpace></Sequence>
          <Sequence ObjectUID="hlg"><Name>Same</Name><WorkingColorSpace>BT.2100 HLG RGB Full</WorkingColorSpace></Sequence>
        </PremiereData>''')
        resolve = Mock()
        manager = resolve.GetProjectManager.return_value
        manager.GetCurrentProject.return_value = None
        manager.GetProjectListInCurrentFolder.return_value = []
        project = manager.CreateProject.return_value
        aces, hlg = Mock(), Mock()
        for timeline in (aces, hlg):
            timeline.GetStartFrame.return_value = 0
            timeline.SetSetting.return_value = True
        project.SetSetting.return_value = True
        project.GetMediaPool.return_value.ImportTimelineFromFile.side_effect = [aces, hlg]

        def export(name: str, path: str, stills: bool) -> bool:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("project.xml", b"opaque project")
            return True

        manager.ExportProject.side_effect = export
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(temp)
            paths = [folder / "aces.xml", folder / "hlg.xml"]
            for path, uid in zip(paths, ("aces", "hlg")):
                ET.ElementTree(sequence("Same", uid)).write(path, encoding="utf-8")
            with patch.object(engine, "_shutdown_resolve"):
                self.assertTrue(engine._drp_export(resolve, paths, folder / "out.drp", "Project", ["Same", "Same"], prproj_root=source))
        self.assertEqual(project.SetSetting.call_args_list, [
            call("timelineFrameRate", "25"),
            call("timelineResolutionWidth", "1080"),
            call("timelineResolutionHeight", "1920"),
            call("colorScienceMode", "acescct"),
        ])
        self.assertEqual(aces.SetSetting.call_args_list, [
            call("colorScienceMode", "acescct"),
            call("timelineStartTimecode", "00:00:00:00"),
        ])
        hlg.SetSetting.assert_any_call("colorScienceMode", "davinciYRGBColorManaged")
        hlg.SetSetting.assert_any_call("colorSpaceTimeline", "Rec.2100 HLG")
        hlg.SetSetting.assert_any_call("colorSpaceOutput", "Rec.2100 HLG")
        self.assertEqual(hlg.SetSetting.call_args_list[-1], call("timelineStartTimecode", "00:00:00:00"))
        self.assertNotIn(call("timelineColorSpace", "Rec.709 Gamma 2.4"), hlg.SetSetting.call_args_list)


if __name__ == "__main__":
    unittest.main()
