from __future__ import annotations

import ast
import builtins
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pr2_engine as pe
import pr2_native as pn


ROOT = Path(__file__).resolve().parent.parent


def test_tui_has_no_top_level_windows_imports() -> None:
    tree = ast.parse((ROOT / "tui.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
            assert "msvcrt" not in names
        if isinstance(node, ast.ImportFrom):
            assert node.module != "msvcrt"


def test_tui_import_does_not_require_msvcrt() -> None:
    original_import = builtins.__import__
    original_tui = sys.modules.pop("tui", None)

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        if name == "msvcrt":
            raise ModuleNotFoundError("No module named 'msvcrt'")
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import
    try:
        importlib.import_module("tui")
    finally:
        builtins.__import__ = original_import
        sys.modules.pop("tui", None)
        if original_tui is not None:
            sys.modules["tui"] = original_tui


def test_prproj_source_metadata_preserves_timecode_and_rate() -> None:
    root = pe.ET.fromstring(
        '<PremiereData><ClipLoggingInfo ObjectID="logging">'
        '<MediaFrameRate>10160640000</MediaFrameRate>'
        '<MediaInPoint>2540160000000</MediaInPoint>'
        '<MediaOutPoint>2794176000000</MediaOutPoint>'
        '</ClipLoggingInfo></PremiereData>'
    )
    index = pe._PrprojIndex.build(root)
    master_clip = pe.ET.fromstring('<MasterClip><LoggingInfo ObjectRef="logging"/></MasterClip>')

    info = pe._prproj_extract_source_tc_info(master_clip, index)

    assert info.media_fps_resolved is True
    assert info.media_fps == 25.0
    assert info.timecode_frame == 250
    assert info.timecode_string == "00:00:10:00"
    assert info.full_duration_frames == 25
    assert info.duration_resolved is True


def test_prproj_missing_source_metadata_remains_unresolved() -> None:
    root = pe.ET.fromstring('<PremiereData/>')
    info = pe._prproj_extract_source_tc_info(
        pe.ET.fromstring('<MasterClip/>'), pe._PrprojIndex.build(root)
    )

    assert info.resolved is False
    assert info.media_fps_resolved is False
    assert info.duration_resolved is False
    assert info.timecode_string == "00:00:00:00"


def test_native_clip_color_updates_verified_nested_enum() -> None:
    import zipfile

    with zipfile.ZipFile(pn.TEMPLATE) as archive:
        seq_path = next(name for name in archive.namelist()
                        if name.startswith("SeqContainer/") and name.endswith(".xml"))
        root = pe.ET.fromstring(archive.read(seq_path))
    clip = next(root.iter("Sm2TiVideoClip"))
    blob = clip.find("FieldsBlob")
    assert blob is not None and blob.text
    root_fields = pn._protobuf_fields(pn._decode_wrapper_hex(blob.text))
    payload_index = next(i for i, (number, wire, _) in enumerate(root_fields)
                         if number == 1 and wire == 2)
    payload_before = pn._protobuf_fields(bytes(root_fields[payload_index][2]))
    pn._set_native_clip_color(clip, 13)
    changed_root = pn._protobuf_fields(pn._decode_wrapper_hex(blob.text))
    changed_payload = pn._protobuf_fields(bytes(changed_root[payload_index][2]))

    assert [(number, value) for number, wire, value in changed_payload
            if number == 6 and wire == 0] == [(6, 13)]
    assert [(number, wire, value) for number, wire, value in changed_payload
            if number != 6] == payload_before
    assert pn.PP_LABEL_TO_RESOLVE_COLOR[1] == 13


def test_native_capability_check_handles_empty_and_multiple_effect_lists() -> None:
    empty = pe.ET.fromstring(
        '<xmeml><sequence><media><video><track><clipitem>'
        '<file id="f"/></clipitem></track></video></media></sequence></xmeml>'
    )
    pn._validate_sequence_capabilities(empty)

    multiple = pe.ET.fromstring(
        '<xmeml><sequence><media><video><track><clipitem><file id="f"/>'
        '<filter><effect><effectid>lumetri</effectid></effect></filter>'
        '<filter><effect><effectid>blur</effectid><mediatype>video</mediatype></effect></filter>'
        '</clipitem></track></video></media></sequence></xmeml>'
    )
    try:
        pn._validate_sequence_capabilities(multiple)
    except ValueError as exc:
        assert "unsupported video effect blur" in str(exc)
    else:
        raise AssertionError("unsupported second effect was not checked")


def test_effective_source_fps_uses_resolved_media_fps() -> None:
    source_tc = pe._SourceTCInfo(media_fps=23.976, media_fps_resolved=True)
    assert pe._effective_source_fps(source_tc, 30.0) == 23.976

    unresolved = pe._SourceTCInfo(media_fps=23.976, media_fps_resolved=False)
    assert pe._effective_source_fps(unresolved, 30.0) == 30.0
