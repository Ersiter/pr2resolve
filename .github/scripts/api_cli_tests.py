"""Portable CLI regressions for API-export status and XML retention."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2resolve as cli
import pr2_engine as engine


def minimal_prproj() -> ET.Element:
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
      <MatchName>AE.ADBE Motion</MatchName><Params/></VideoFilterComponent></PremiereData>''')


class ApiCliTests(unittest.TestCase):
    def test_background_and_gui_drp_failures_return_nonzero_and_keep_intermediates(self) -> None:
        for option in ("drp_path", "drp_gui"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                kwargs = {option: folder / "requested.drp"}
                output = io.StringIO()
                with patch.object(cli, "load_prproj", return_value=minimal_prproj()), \
                     patch.object(cli, "_ensure_resolve_running", return_value=object()), \
                     patch.object(cli, "_drp_export", return_value=False) as export, \
                     patch.object(cli, "_shutdown_resolve") as shutdown, \
                     contextlib.redirect_stdout(output):
                    result = cli._run_pipeline(
                        folder / "sample.prproj", folder / "out", no_xml=True, **kwargs,
                    )
                self.assertEqual(result, 1)
                self.assertTrue(export.called)
                self.assertEqual(len(list((folder / "out").glob("*.xml"))), 1)
                self.assertIn("API export incomplete", output.getvalue())
                shutdown.assert_not_called()

    def test_successful_drp_keeps_requested_xml_and_cleans_duplicate_intermediate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(cli, "load_prproj", return_value=minimal_prproj()), \
                 patch.object(cli, "_ensure_resolve_running", return_value=object()), \
                 patch.object(cli, "_drp_export", return_value=True), \
                 patch.object(cli, "_recycle", side_effect=lambda path: Path(path).unlink()) as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(
                    folder / "sample.prproj", folder / "out", drp_path=folder / "project.drp",
                )
            self.assertEqual(result, 0)
            remaining_xml = list((folder / "out").glob("*.xml"))
            self.assertEqual(len(remaining_xml), 1)
            self.assertEqual(recycle.call_count, 1)

    def test_single_drt_failure_keeps_xml_with_and_without_no_xml(self) -> None:
        for no_xml in (False, True):
            with self.subTest(no_xml=no_xml), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                output = io.StringIO()
                with patch.object(cli, "_drt_sandbox_export", return_value=False), \
                     patch.object(cli, "_check_resolve_running", return_value=object()), \
                     patch.object(cli, "_recycle") as recycle, \
                     patch.object(cli, "_shutdown_resolve"), \
                     contextlib.redirect_stdout(output):
                    result = cli._run_pipeline(
                        Path(__file__).resolve().parents[2] / "examples" / "minimal_fcp7.xml",
                        folder / "out", drt=True, no_xml=no_xml,
                    )
                self.assertEqual(result, 1)
                self.assertEqual(len(list((folder / "out").glob("*.xml"))), 1)
                recycle.assert_not_called()
                self.assertIn("API export incomplete", output.getvalue())

    def test_retry_leave_is_reported_as_uncompleted_drt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            output = io.StringIO()
            with patch.object(cli, "_check_resolve_running", side_effect=[None, None]), \
                 patch.object(cli, "_drt_sandbox_export") as export, \
                 patch.object(cli, "_recycle") as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 patch("builtins.input", side_effect=["r", "l"]), \
                 contextlib.redirect_stdout(output):
                result = cli._run_pipeline(
                    Path(__file__).resolve().parents[2] / "examples" / "minimal_fcp7.xml",
                    folder / "out", drt=True,
                )
            self.assertEqual(result, 1)
            self.assertEqual(len(list((folder / "out").glob("*.xml"))), 1)
            export.assert_not_called()
            recycle.assert_not_called()
            self.assertIn("API export incomplete", output.getvalue())

    def test_batch_partial_drt_failure_keeps_every_xml_for_retry(self) -> None:
        project = minimal_prproj()
        second = ET.fromstring(ET.tostring(project.find("Sequence")))
        second.set("ObjectUID", "sequence-2")
        second.find("Name").text = "Second"
        project.append(second)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            output = io.StringIO()
            with patch.object(cli, "load_prproj", return_value=project), \
                 patch.object(cli, "_ensure_resolve_running", return_value=object()), \
                 patch.object(cli, "_drt_batch_export", return_value=[(True, folder / "ok.drt"), (False, None)]) as export, \
                 patch.object(cli, "_recycle") as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 contextlib.redirect_stdout(output):
                result = cli._run_pipeline(
                    folder / "sample.prproj", folder / "out", all_sequences=True,
                    drt=True, no_xml=True,
                )
            self.assertEqual(result, 1)
            self.assertEqual(len(export.call_args.args[1]), 2)
            self.assertTrue(all(path.is_file() for path in export.call_args.args[1]))
            self.assertEqual(len(list((folder / "out").glob("*.xml"))), 2)
            recycle.assert_not_called()
            self.assertIn("API export incomplete", output.getvalue())

    def test_successful_no_xml_drt_recycles_batch_and_single_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(cli, "_ensure_resolve_running", return_value=object()), \
                 patch.object(cli, "_drt_batch_export", return_value=[(True, folder / "one.drt"), (True, folder / "two.drt")]), \
                 patch.object(cli, "_recycle", side_effect=lambda path: Path(path).unlink()) as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 contextlib.redirect_stdout(io.StringIO()):
                project = minimal_prproj()
                second = ET.fromstring(ET.tostring(project.find("Sequence")))
                second.set("ObjectUID", "sequence-2")
                second.find("Name").text = "Second"
                project.append(second)
                with patch.object(cli, "load_prproj", return_value=project):
                    result = cli._run_pipeline(
                        folder / "sample.prproj", folder / "out", all_sequences=True,
                        drt=True, no_xml=True,
                    )
            self.assertEqual(result, 0)
            self.assertEqual(recycle.call_count, 2)
            self.assertEqual(list((folder / "out").glob("*.xml")), [])

        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(cli, "_drt_sandbox_export", return_value=True), \
                 patch.object(cli, "_check_resolve_running", return_value=object()), \
                 patch.object(cli, "_recycle", side_effect=lambda path: Path(path).unlink()) as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(
                    Path(__file__).resolve().parents[2] / "examples" / "minimal_fcp7.xml",
                    folder / "out", drt=True, no_xml=True,
                )
            self.assertEqual(result, 0)
            recycle.assert_called_once()
            self.assertEqual(list((folder / "out").glob("*.xml")), [])

    def test_successful_batch_drt_keeps_xml_when_requested(self) -> None:
        project = minimal_prproj()
        second = ET.fromstring(ET.tostring(project.find("Sequence")))
        second.set("ObjectUID", "sequence-2")
        second.find("Name").text = "Second"
        project.append(second)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with patch.object(cli, "load_prproj", return_value=project), \
                 patch.object(cli, "_ensure_resolve_running", return_value=object()), \
                 patch.object(cli, "_drt_batch_export", return_value=[(True, folder / "one.drt"), (True, folder / "two.drt")]), \
                 patch.object(cli, "_recycle") as recycle, \
                 patch.object(cli, "_shutdown_resolve"), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = cli._run_pipeline(
                    folder / "sample.prproj", folder / "out", all_sequences=True, drt=True,
                )
            self.assertEqual(result, 0)
            self.assertEqual(len(list((folder / "out").glob("*.xml"))), 2)
            recycle.assert_not_called()

    def test_batch_engine_leaves_successful_source_xml_to_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            xml = folder / "source.xml"
            xml.write_text("<xmeml/>", encoding="utf-8")
            resolve = Mock()
            project = resolve.GetProjectManager.return_value.CreateProject.return_value
            timeline = project.GetMediaPool.return_value.ImportTimelineFromFile.return_value
            timeline.Export.return_value = True
            with patch.object(engine, "_strip_file_elements_for_drt", return_value=(xml, False)), \
                 patch.object(engine, "_apply_fcp_timeline_state_to_resolve", return_value=[]), \
                 patch.object(engine, "apply_timeline_state_to_archive", return_value=0), \
                 patch.object(engine, "_recycle") as recycle, \
                 contextlib.redirect_stdout(io.StringIO()):
                result = engine._drt_batch_export(resolve, [xml], folder, ["Sample"])
            self.assertTrue(result[0][0])
            self.assertTrue(xml.is_file())
            recycle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
