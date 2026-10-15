"""Keep data-only sequences in source inventory and conversion diagnosis."""

from __future__ import annotations

import contextlib
import gzip
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pr2_engine as engine
import pr2resolve as cli
from caption_tests import _caption_project, _synthetic_formatted_text


class CaptionInventoryTests(unittest.TestCase):
    def test_caption_only_sequences_are_not_empty(self) -> None:
        payload, _ = _synthetic_formatted_text(("Only caption",))
        for group_type in ("DataTrackGroup", "CaptionTrackGroup"):
            with self.subTest(group_type=group_type):
                root = _caption_project([payload])
                root.find("DataTrackGroup").tag = group_type
                item = engine._prproj_list_sequences(root, engine._PrprojIndex.build(root))[0]
                self.assertEqual((item["clip_count"], item["data_item_count"], item["content_count"]), (0, 1, 1))

    def test_unknown_data_items_remain_visible_in_inventory(self) -> None:
        payload, _ = _synthetic_formatted_text(("Data",))
        root = _caption_project([payload])
        root.find("CaptionDataClipTrackItem").tag = "UnknownDataClipTrackItem"
        item = engine._prproj_list_sequences(root, engine._PrprojIndex.build(root))[0]
        self.assertEqual(item["content_count"], 1)
        xml = engine._prproj_parse_sequence(root, "sequence-1", Path("data.prproj"))
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "unconverted-data-track"):
                cli.export_native_drp(xml, Path(temp) / "unused.drp", "Data")

    def test_caption_only_project_reaches_diagnosis_without_desktop_interaction(self) -> None:
        payload, _ = _synthetic_formatted_text(("Only caption",))
        root = _caption_project([payload])
        ET.SubElement(root.find("Sequence"), "Name").text = "Captions"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "captions.prproj"
            path.write_bytes(gzip.compress(ET.tostring(root)))
            with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()) as log:
                for method in ("_ensure_resolve_running", "_shutdown_resolve", "_recycle"):
                    stack.enter_context(patch.object(cli, method, side_effect=AssertionError("desktop interaction")))
                result = cli._run_pipeline(path, folder / "out", diagnose_only=True)
            self.assertEqual(result, 0, log.getvalue())
            self.assertNotIn("No non-empty sequences", log.getvalue())
            self.assertEqual(list((folder / "out").iterdir()), [])

    def test_caption_only_native_rejection_retains_a_source_report(self) -> None:
        payload, _ = _synthetic_formatted_text(("Only caption",))
        root = _caption_project([payload])
        ET.SubElement(root.find("Sequence"), "Name").text = "Captions"
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            path = folder / "captions.prproj"
            path.write_bytes(gzip.compress(ET.tostring(root)))
            with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()) as log:
                for method in ("_ensure_resolve_running", "_shutdown_resolve", "_recycle"):
                    stack.enter_context(patch.object(cli, method, side_effect=AssertionError("desktop interaction")))
                result = cli._run_pipeline(path, folder / "out", report=True, native_drp=True, no_xml=True)
            self.assertEqual(result, 1, log.getvalue())
            reports = list((folder / "out").glob("*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("Captions: 1/1 text records decoded", reports[0].read_text(encoding="utf-8"))
            self.assertEqual(list((folder / "out").glob("*.drp")), [])
            self.assertEqual(list((folder / "out").glob("*.xml")), [])


if __name__ == "__main__":
    unittest.main()
