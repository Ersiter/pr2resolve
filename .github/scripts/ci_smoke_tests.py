#!/usr/bin/env python3
"""Public synthetic CI smoke tests for pr2resolve.

These tests intentionally avoid private fixtures, real media files, and a real
ffprobe binary. They exercise behavior that must stay available in GitHub
Actions with only the standard library.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pr2_engine as pe  # noqa: E402
from pr2_constants import _SourceTCInfo  # noqa: E402


class _Completed:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _with_fake_ffprobe(stdout: str, returncode: int = 0) -> Callable[[], _SourceTCInfo]:
    original = pe.subprocess.run

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True, timeout: int = 15) -> _Completed:
        assert cmd[:4] == ["ffprobe", "-v", "error", "-of"]
        assert "json" in cmd
        assert "creation_time" not in " ".join(cmd)
        return _Completed(stdout=stdout, returncode=returncode)

    def run() -> _SourceTCInfo:
        pe.subprocess.run = fake_run
        try:
            return pe._ffprobe_read_timecode("synthetic.mov")
        finally:
            pe.subprocess.run = original

    return run


def test_ffprobe_fps_duration_without_timecode() -> None:
    run = _with_fake_ffprobe(json.dumps({
        "streams": [{
            "codec_type": "video",
            "r_frame_rate": "60000/1001",
            "duration": "2.0",
        }],
        "format": {},
    }))

    info = run()

    assert info.resolved is False
    assert info.media_fps_resolved is True
    assert info.duration_resolved is True
    assert info.media_fps == 59.94
    assert info.full_duration_frames == 120


def test_ffprobe_video_timecode_priority_and_nb_frames() -> None:
    run = _with_fake_ffprobe(json.dumps({
        "streams": [
            {
                "codec_type": "video",
                "r_frame_rate": "30000/1001",
                "duration": "10.0",
                "nb_frames": "123",
                "timecode": "01:02:03:04",
            },
            {
                "codec_type": "data",
                "tags": {"timecode": "09:09:09:09"},
            },
        ],
        "format": {
            "duration": "20.0",
            "tags": {"timecode": "08:08:08:08"},
        },
    }))

    info = run()

    assert info.resolved is True
    assert info.timecode_string == "01:02:03:04"
    assert info.media_fps == 29.97
    assert info.full_duration_frames == 123


def test_ffprobe_invalid_json_returns_default() -> None:
    info = _with_fake_ffprobe("{not-json")()

    assert info == _SourceTCInfo()


def test_ffprobe_file_not_found_warns_and_returns_default() -> None:
    original = pe.subprocess.run
    original_flag = pe._ffprobe_missing_warned

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True, timeout: int = 15) -> _Completed:
        raise FileNotFoundError("ffprobe")

    pe.subprocess.run = fake_run
    pe._ffprobe_missing_warned = False
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            info = pe._ffprobe_read_timecode("synthetic.mov")
    finally:
        pe.subprocess.run = original
        pe._ffprobe_missing_warned = original_flag

    assert info == _SourceTCInfo()
    assert "Warning: ffprobe not found on PATH." in buf.getvalue()


def test_source_tc_merge_preserves_baseline_timecode() -> None:
    baseline = _SourceTCInfo(
        media_fps=25.0,
        media_fps_resolved=True,
        timecode_frame=250,
        timecode_string="00:00:10:00",
        full_duration_frames=100,
        duration_resolved=True,
        resolved=True,
    )
    overlay = _SourceTCInfo(
        media_fps=59.94,
        media_fps_resolved=True,
        is_ntsc=True,
        full_duration_frames=240,
        duration_resolved=True,
        resolved=False,
    )

    merged = pe._merge_source_tc_info(baseline, overlay)

    assert merged.resolved is True
    assert merged.timecode_string == "00:00:10:00"
    assert merged.media_fps == 59.94
    assert merged.duration_resolved is True
    assert merged.full_duration_frames == 240


def main() -> int:
    tests = [
        test_ffprobe_fps_duration_without_timecode,
        test_ffprobe_video_timecode_priority_and_nb_frames,
        test_ffprobe_invalid_json_returns_default,
        test_ffprobe_file_not_found_warns_and_returns_default,
        test_source_tc_merge_preserves_baseline_timecode,
    ]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"{len(tests)} smoke tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
