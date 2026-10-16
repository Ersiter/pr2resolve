"""Exercise post-export retime correction through every API output entry."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as engine
from retime_output_tests import sequence_xml, source_xml, timeline_clip


class RetimeApiTests(unittest.TestCase):
    def test_drp_failure_in_later_timeline_does_not_leave_partial_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            first, second = folder / "first.xml", folder / "second.xml"
            first.write_bytes(source_xml())
            second.write_bytes(source_xml(bad_graph=True).replace(b"Mixed Rate", b"Another Timeline"))
            resolve = Mock()
            manager = resolve.GetProjectManager.return_value
            manager.GetCurrentProject.return_value = None
            manager.GetProjectListInCurrentFolder.return_value = []
            manager.CreateProject.return_value.SetSetting.return_value = True
            timeline = manager.CreateProject.return_value.GetMediaPool.return_value.ImportTimelineFromFile.return_value
            timeline.GetStartFrame.return_value = 0
            output = folder / "out.drp"
            original: list[bytes] = []

            def export(name: str, path: str, stills: bool) -> bool:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("MediaPool/Master/MpFolder.xml", b"<Sm2MpFolder>" + timeline_clip("Mixed Rate", "sequence-a", "timeline-a") + b"</Sm2MpFolder>")
                    archive.writestr("SeqContainer/api.xml", sequence_xml())
                original.append(Path(path).read_bytes())
                return True

            manager.ExportProject.side_effect = export
            with patch.object(engine, "_shutdown_resolve"), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(engine._drp_export(resolve, [first, second], output, "Project", ["Mixed Rate", "Another Timeline"]))
            self.assertEqual(output.read_bytes(), original[0])
            self.assertTrue(first.is_file() and second.is_file())

    def test_all_api_exports_correct_the_package_before_reporting_success(self) -> None:
        self._check_outputs(valid_archive=True)

    def test_failed_correction_preserves_source_and_does_not_report_success(self) -> None:
        self._check_outputs(valid_archive=False)

    def _check_outputs(self, valid_archive: bool) -> None:
        for kind in ("single-drt", "batch-drt", "drp"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                xml = folder / "source.xml"
                xml.write_bytes(source_xml())
                resolve = Mock()
                manager = resolve.GetProjectManager.return_value
                manager.GetCurrentProject.return_value = None
                manager.GetProjectListInCurrentFolder.return_value = []
                project = manager.CreateProject.return_value
                project.SetSetting.return_value = True
                timeline = project.GetMediaPool.return_value.ImportTimelineFromFile.return_value
                timeline.GetName.return_value = "Mixed Rate"
                timeline.GetStartFrame.return_value = 0
                outputs: list[Path] = []

                def export(*args: object) -> bool:
                    output = Path(str(args[1] if kind == "drp" else args[0]))
                    with zipfile.ZipFile(output, "w") as archive:
                        archive.writestr("project.xml", b"preserved-project")
                        if valid_archive:
                            archive.writestr(
                                "MediaPool/Master/MpFolder.xml",
                                b"<Sm2MpFolder><MediaVec><Element>"
                                + timeline_clip("Mixed Rate", "sequence-a", "timeline-a")
                                + b"</Element></MediaVec></Sm2MpFolder>",
                            )
                            archive.writestr("SeqContainer/api.xml", sequence_xml())
                    outputs.append(output)
                    return True

                timeline.Export.side_effect = export
                manager.ExportProject.side_effect = export
                with patch.object(engine, "_strip_file_elements_for_drt", return_value=(xml, False)), \
                     patch.object(engine, "_shutdown_resolve"), patch.object(engine, "_recycle") as recycle, \
                     contextlib.redirect_stdout(io.StringIO()) as log:
                    if kind == "single-drt":
                        success = engine._drt_sandbox_export(resolve, xml, folder / "out.drt")
                    elif kind == "batch-drt":
                        success = engine._drt_batch_export(resolve, [xml], folder, ["Mixed Rate"])[0][0]
                    else:
                        success = engine._drp_export(resolve, [xml], folder / "out.drp", "Project", ["Mixed Rate"])
                self.assertEqual(success, valid_archive, log.getvalue())
                self.assertEqual(len(outputs), 1)
                self.assertTrue(xml.is_file())
                if not valid_archive:
                    recycle.assert_not_called()
                with zipfile.ZipFile(outputs[0]) as archive:
                    self.assertEqual(archive.read("project.xml"), b"preserved-project")
                    if valid_archive:
                        written = archive.read("SeqContainer/api.xml")
                        self.assertIn(b"<In>260</In>", written)
                        self.assertNotIn(b"old-map-a", written)


if __name__ == "__main__":
    unittest.main()
