#!/usr/bin/env python3
"""Tests for pr2resolve."""
from __future__ import annotations
import sys, xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pr2_constants import (
    CRITICAL, _build_file_index, _get_sequence_format,
    _get_sequence_resolution, load_xml,
)
from pr2_engine import _scan, _detect_scale_mismatch, _is_ntsc_fps, _apply_fixes, _validate


def _s():
    return load_xml(Path(__file__).resolve().parent.parent / "test" / "序列 01_pr_direct.xml")


def test_load():
    r = _s()
    assert r.tag == "xmeml" and r.get("version") == "4"


def test_ver_det():
    assert len([i for i in _scan(_s()) if i.rule_id == "C0"]) == 1


def test_lum_det():
    assert len([i for i in _scan(_s()) if i.rule_id == "M0"]) == 9


def test_st_det():
    # M2 disabled — DC format: video clipitems don't need sourcetrack
    assert len([i for i in _scan(_s()) if i.rule_id == "M2"]) == 0


def test_pu_det():
    # C5 no longer fires on file://localhost/ (now accepted as valid PR format)
    assert len([i for i in _scan(_s()) if i.rule_id == "C5"]) == 0


def test_scale_no_rot():
    sf = _get_sequence_format(_s())
    c = ET.fromstring(
        '<clipitem id="t"><file id="f"><media><video>'
        '<samplecharacteristics><width>1920</width><height>1080</height>'
        '</samplecharacteristics></video></media></file></clipitem>'
    )
    r = _detect_scale_mismatch(c, c.find("file"), sf)
    assert r is not None and abs(r.corrected_scale - 56.3) < 0.2


def test_scale_rot():
    sf = _get_sequence_format(_s())
    c = ET.fromstring(
        '<clipitem id="t"><filter><effect><effectid>basic</effectid>'
        '<parameter><name>Scale</name><value>100</value></parameter>'
        '<parameter><name>Rotation</name><value>270</value></parameter>'
        '</effect></filter>'
        '<file id="f"><media><video>'
        '<samplecharacteristics><width>1920</width><height>1080</height>'
        '</samplecharacteristics></video></media></file></clipitem>'
    )
    assert _detect_scale_mismatch(c, c.find("file"), sf) is None


def test_scale_manual():
    sf = _get_sequence_format(_s())
    c = ET.fromstring(
        '<clipitem id="t"><filter><effect><effectid>basic</effectid>'
        '<parameter><name>Scale</name><value>75.5</value></parameter>'
        '</effect></filter>'
        '<file id="f"><media><video>'
        '<samplecharacteristics><width>1920</width><height>1080</height>'
        '</samplecharacteristics></video></media></file></clipitem>'
    )
    assert _detect_scale_mismatch(c, c.find("file"), sf) is None


def test_ver_fix():
    r = _s()
    _apply_fixes(r, _scan(r))
    assert r.get("version") == "5"


def test_lum_fix():
    r = _s()
    _apply_fixes(r, _scan(r))
    assert sum(1 for e in r.iter("effect") if e.findtext("effectid") == "Lumetri") == 0


def test_st_fix():
    r = _s()
    _apply_fixes(r, _scan(r))
    # DC format: sourcetrack only expected on audio clipitems, not video
    for ci in r.iter("clipitem"):
        fe = ci.find("file")
        if fe is not None and fe.find("media/video") is None:
            assert ci.find("sourcetrack") is not None, f"Audio clipitem missing sourcetrack"


def test_pu_fix():
    r = _s()
    _apply_fixes(r, _scan(r))
    for p in r.iter("pathurl"):
        if p.text:
            assert p.text.startswith("file://localhost/")  # PR format preserved


def test_valid():
    r = _s()
    _apply_fixes(r, _scan(r))
    assert len([v for v in _validate(r) if v.severity == CRITICAL]) == 0


def test_fidx():
    assert len(_build_file_index(_s())) > 0


def test_ntsc():
    assert _is_ntsc_fps(29.97) and _is_ntsc_fps(23.976) and _is_ntsc_fps(59.94)
    assert not _is_ntsc_fps(25.0) and not _is_ntsc_fps(30.0) and not _is_ntsc_fps(24.0)


def test_res():
    w, h = _get_sequence_resolution(_s())
    assert w == 1080 and h == 1920


def test_tracks():
    r = _s()
    _apply_fixes(r, _scan(r))
    assert len(r.find("sequence").find("media/video").findall("track")) == 3


def test_fcount():
    assert _apply_fixes(_s(), _scan(_s())) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Integration tests — run full pipeline on real samples
# ═══════════════════════════════════════════════════════════════════════════════

_TEST_DIR = Path(__file__).resolve().parent.parent / "test"
_OUT_DIR = Path(__file__).resolve().parent.parent / "_test_output"


def _cleanup():
    """Remove test output directory."""
    import shutil
    if _OUT_DIR.exists():
        shutil.rmtree(_OUT_DIR)


def test_e2e_xml_sample():
    """Run full pipeline on PR FCP7 XML sample, verify output structure."""
    _cleanup()
    from pr2_engine import _write_fixed_xml, _generate_report
    root = load_xml(_TEST_DIR / "序列 01_pr_direct.xml")
    issues = _scan(root)
    fix_count = _apply_fixes(root, issues)
    validation = _validate(root)
    assert len([v for v in validation if v.severity == CRITICAL]) == 0
    assert fix_count > 0

    _OUT_DIR.mkdir(exist_ok=True)
    out_xml = _OUT_DIR / "xml_sample_fixed.xml"
    _write_fixed_xml(root, out_xml)
    assert out_xml.exists()

    # Verify output structure
    with open(out_xml, "r", encoding="utf-8") as f:
        content = f.read()
    assert '<?xml version="1.0" encoding="UTF-8"?>' in content
    assert "<!DOCTYPE xmeml>" in content
    assert 'version="5"' in content

    # Verify all pathurls are file:/// or file://localhost/
    out_root = ET.parse(str(out_xml)).getroot()
    for pu in out_root.iter("pathurl"):
        assert pu.text and (pu.text.startswith("file:///") or pu.text.startswith("file://localhost/")), f"Bad pathurl: {pu.text}"

    # Verify no Lumetri remains
    for eff in out_root.iter("effect"):
        assert eff.findtext("effectid") != "Lumetri", "Lumetri not removed"

    _cleanup()


def test_e2e_prproj_sample():
    """Run full pipeline on .prproj sample, verify output structure."""
    _cleanup()
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence, _write_fixed_xml
    from pr2_constants import load_prproj

    prproj_path = _TEST_DIR / "荷花.prproj"
    if not prproj_path.exists():
        return  # skip if sample not available

    root = load_prproj(prproj_path)
    idx = _PrprojIndex.build(root)
    seqs = root.findall("Sequence")
    assert len(seqs) > 0, "No sequences in .prproj"

    selected_uid = seqs[0].get("ObjectUID")
    fcp = _prproj_parse_sequence(root, selected_uid, prproj_path)

    issues = _scan(fcp)
    fix_count = _apply_fixes(fcp, issues)
    validation = _validate(fcp)
    assert len([v for v in validation if v.severity == CRITICAL]) == 0

    _OUT_DIR.mkdir(exist_ok=True)
    out_xml = _OUT_DIR / "prproj_sample.xml"
    _write_fixed_xml(fcp, out_xml)
    assert out_xml.exists()

    # Verify output structure
    out_root = ET.parse(str(out_xml)).getroot()
    assert out_root.get("version") == "5"
    clips = list(out_root.iter("clipitem"))
    assert len(clips) >= 9, f"Expected >= 9 clips, got {len(clips)}"

    # Verify audio tracks exist
    audio_tracks = out_root.findall(".//media/audio/track")
    assert len(audio_tracks) > 0, "No audio tracks in output"

    # Verify pathurls are file:/// or file://localhost/
    for pu in out_root.iter("pathurl"):
        assert pu.text and (pu.text.startswith("file:///") or pu.text.startswith("file://localhost/")), f"Bad pathurl: {pu.text}"

    # DC format: sourcetrack on audio clipitems only
    for ci in clips:
        fe = ci.find("file")
        if fe is not None and fe.find("media/video") is None:
            assert ci.find("sourcetrack") is not None, f"Audio clipitem missing sourcetrack in {ci.get('id')}"

    _cleanup()


def test_e2e_prproj_drt_readiness():
    """Verify .prproj output would produce valid DRT (structure check only)."""
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    prproj_path = _TEST_DIR / "荷花.prproj"
    if not prproj_path.exists():
        return

    root = load_prproj(prproj_path)
    seqs = root.findall("Sequence")
    fcp = _prproj_parse_sequence(root, seqs[0].get("ObjectUID"), prproj_path)

    # Verify Lumetri data was extracted
    from pr2_engine import _prproj_extract_all_lumetri
    lum = _prproj_extract_all_lumetri(root, seqs[0].get("ObjectUID"))
    assert len(lum) == 9, f"Expected 9 Lumetri clips, got {len(lum)}"
    assert sum(len(v) for v in lum.values()) > 100, "Too few Lumetri params"

    # Verify first clip has expected param values
    first_params = list(lum.values())[0]
    assert "曝光" in first_params or "Exposure" in first_params


def test_clipdata_regression():
    """ClipData migration regression: verify XML attributes driven by ClipData fields.

    Tests via the second video clipitem (known to be '2026.5.29 荷花.mp4' with
    scale=56.3, speed=100).  All assertions use encoding-neutral numeric checks.
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return  # skip if sample not available

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    assert len(seqs) > 0, "No sequences in .prproj"

    # Use the primary sequence (name = "序列 01", has 70 clips)
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s
            break
    if primary is None:
        return  # skip if primary sequence not found
    fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # Second video clipitem is the known "lotus" clip
    video_track = fcp.find(".//media/video/track")
    clipitems = video_track.findall("clipitem")
    assert len(clipitems) >= 2, f"Need >= 2 clips, got {len(clipitems)}"
    lotus = clipitems[1]  # index 1 = second clip

    # Clip-level attributes (encoding-neutral numeric checks)
    assert lotus.findtext("name") is not None and len(lotus.findtext("name")) > 0
    dur = int(lotus.findtext("duration", "0"))
    assert dur > 0, f"Expected positive duration, got {dur}"
    assert lotus.findtext("enabled") == "TRUE"
    assert lotus.find("compositemode") is not None

    # in/out — source trimming (should be 0 → full clip for this one)
    in_val = int(lotus.findtext("in", "-1"))
    out_val = int(lotus.findtext("out", "-1"))
    assert in_val >= 0, f"Expected in >= 0, got {in_val}"
    assert out_val > in_val, f"Expected out > in, got out={out_val}, in={in_val}"

    # 4 filters per DC convention
    filters = lotus.findall("filter")
    assert len(filters) == 4, f"Expected 4 video filters, got {len(filters)}"

    # Basic Motion → Scale (should be a non-default value, > 1)
    bm = filters[0].find("effect")
    assert bm.findtext("effectid", "") == "basic"
    scale_p = bm.find(".//parameter[parameterid='scale']")
    assert scale_p is not None, "Scale parameter not found"
    scale_val = float(scale_p.findtext("value", "100"))
    assert scale_val > 1, f"Scale should be > 1 (auto-fit applied), got {scale_val}"

    # Time Remap → Speed (should be 100 = normal speed)
    tr = filters[3].find("effect")
    assert tr.findtext("effectid", "") == "timeremap"
    speed_p = tr.find(".//parameter[parameterid='speed']")
    assert speed_p is not None, "Speed parameter not found"
    assert speed_p.findtext("value", "") == "100"


def test_filedata_regression():
    """FileData migration regression: verify <file> element structure.

    Covers: id format, children order, timecode (no <frame>), pathurl,
    media (video: width/height, audio: channelcount).
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s
            break
    if primary is None:
        return

    fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # First file element in video section
    first_file = fcp.find(".//media/video/track/clipitem/file")
    assert first_file is not None, "No <file> element found"
    fid = first_file.get("id", "")
    assert fid, "File element missing id attribute"

    # DC-format file children order: duration→rate→name→pathurl→timecode→media
    children = [c.tag for c in first_file]
    assert "duration" in children
    assert "rate" in children
    assert "name" in children
    assert "pathurl" in children
    assert "timecode" in children
    assert "media" in children

    # Timecode: string→displayformat→rate (NO <frame>)
    tc = first_file.find("timecode")
    assert tc is not None, "Missing <timecode>"
    assert tc.find("string") is not None, "Missing timecode/string"
    assert tc.find("displayformat") is not None, "Missing timecode/displayformat"
    assert tc.find("rate") is not None, "Missing timecode/rate"
    assert tc.find("frame") is None, "timecode should NOT have <frame> element"

    # Media: video (width/height) + audio (channelcount)
    media = first_file.find("media")
    assert media is not None, "Missing <media>"
    video = media.find("video")
    assert video is not None, "Missing media/video"
    sc = video.find("samplecharacteristics")
    assert sc is not None, "Missing video samplecharacteristics"
    w = int(sc.findtext("width", "0"))
    h = int(sc.findtext("height", "0"))
    assert w > 0 and h > 0, f"Invalid dimensions: {w}x{h}"
    audio = media.find("audio")
    assert audio is not None, "Missing media/audio"
    assert audio.find("channelcount") is not None, "Missing audio/channelcount"

    # Audio clipitem: file references existing id (self-closing or standalone)
    audio_track = fcp.find(".//media/audio/track")
    if audio_track is not None:
        audio_ci = audio_track.find("clipitem")
        if audio_ci is not None:
            a_file = audio_ci.find("file")
            assert a_file is not None, "Audio clipitem missing <file>"
            a_fid = a_file.get("id", "")
            assert a_fid, "Audio file element missing id"


def test_filterspec_regression():
    """FilterSpec migration: verify filter structure survives parameter extraction.

    Tests via the real .prproj → XML pipeline.  Video clipitems must have
    4 filters; audio clipitems must have 2-3 (Time Remap only if speed≠100).
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s
            break
    if primary is None:
        return

    fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # ── Video clipitem filter structure ──
    video_track = fcp.find(".//media/video/track")
    video_clip = video_track.findall("clipitem")[1]  # second clip (known lotus)

    v_filters = video_clip.findall("filter")
    assert len(v_filters) == 4, f"Expected 4 video filters, got {len(v_filters)}"

    # Filter 1: Basic Motion
    f1 = v_filters[0].find("effect")
    assert f1.findtext("effectid") == "basic"
    assert f1.findtext("name") == "Basic Motion"
    assert f1.findtext("effecttype") == "motion"
    assert f1.findtext("mediatype") == "video"
    params1 = {p.findtext("parameterid"): p.findtext("value") for p in f1.findall("parameter")}
    assert "scale" in params1
    assert "rotation" in params1

    # Filter 2: Crop
    f2 = v_filters[1].find("effect")
    assert f2.findtext("effectid") == "crop"
    assert f2.findtext("effecttype") == "motion"
    params2 = f2.findall("parameter")
    assert len(params2) == 4, f"Crop should have 4 params, got {len(params2)}"

    # Filter 3: Opacity
    f3 = v_filters[2].find("effect")
    assert f3.findtext("effectid") == "opacity"
    assert f3.findtext("effecttype") == "motion"

    # Filter 4: Time Remap
    f4 = v_filters[3].find("effect")
    assert f4.findtext("effectid") == "timeremap"
    assert f4.findtext("effecttype") == "motion"
    assert f4.findtext("mediatype") == "video"
    tr_params = {p.findtext("parameterid"): p.findtext("value") for p in f4.findall("parameter")}
    assert "speed" in tr_params
    assert tr_params["speed"] == "100"

    # ── Audio clipitem filter structure ──
    audio_track = fcp.find(".//media/audio/track")
    if audio_track is not None:
        audio_clips = audio_track.findall("clipitem")
        a_filters = audio_clips[0].findall("filter")
        assert len(a_filters) >= 2, f"Expected >=2 audio filters, got {len(a_filters)}"

        # First audio filter: Audio Levels (or Time Remap if speed≠100)
        af1 = a_filters[0].find("effect")
        aid1 = af1.findtext("effectid")
        assert aid1 in ("audiolevels", "timeremap"), f"Unexpected first audio filter: {aid1}"

        # Last audio filter: Audio Pan
        af_last = a_filters[-1].find("effect")
        assert af_last.findtext("effectid") == "audiopan"
        assert af_last.findtext("effecttype") == "audiopan"


def test_trackdata_regression():
    """TrackData+TransitionData regression: verify track/transition structure.

    Video tracks must have enabled/locked tags.  Transitions must be
    Cross Dissolve with center alignment and rate/start/end/effect children.
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s
            break
    if primary is None:
        return

    fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # ── Track structure ──
    video = fcp.find(".//media/video")
    v_tracks = video.findall("track")
    assert len(v_tracks) >= 1, f"Expected >=1 video tracks, got {len(v_tracks)}"
    for t in v_tracks:
        assert t.find("enabled") is not None, "Track missing <enabled>"
        assert t.find("locked") is not None, "Track missing <locked>"
        assert t.findtext("enabled") == "TRUE"
        assert t.findtext("locked") == "FALSE"

    audio = fcp.find(".//media/audio")
    a_tracks = audio.findall("track")
    for t in a_tracks:
        assert t.find("enabled") is not None
        assert t.find("locked") is not None

    # ── Transition structure (only when transitions are present) ──
    transitions = fcp.findall(".//transitionitem")
    if transitions:
        tr = transitions[0]
        assert tr.findtext("alignment") == "center"
        assert tr.find("start") is not None
        assert tr.find("end") is not None
        assert tr.find("rate/timebase") is not None

        effect = tr.find("effect")
        assert effect is not None, "Transition missing <effect>"
        assert effect.findtext("name") == "Cross Dissolve"
        assert effect.findtext("effectid") == "Cross Dissolve"
        assert effect.findtext("effecttype") == "transition"
        assert effect.findtext("mediatype") == "video"
        assert effect.findtext("startratio") == "0"
        assert effect.findtext("endratio") == "1"
        assert effect.findtext("reverse") == "FALSE"


def _load_test_fcp():
    """Shared helper: parse the real .prproj and return FCP7 XML root."""
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return None

    root = load_prproj(test_proj)
    for s in root.findall("Sequence"):
        if s.findtext("Name", "") == "序列 01":
            return _prproj_parse_sequence(root, s.get("ObjectUID"), test_proj)
    return None


def test_clipitem_video_structure():
    """Video clipitem: compositemode=True, sourcetrack=False, 4 filters."""
    fcp = _load_test_fcp()
    if fcp is None:
        return

    video_track = fcp.find(".//media/video/track")
    v_clip = video_track.findall("clipitem")[1]

    assert v_clip.find("compositemode") is not None, "Video clipitem MUST have compositemode"
    assert v_clip.findtext("compositemode") == "normal"
    assert v_clip.find("sourcetrack") is None, "Video clipitem must NOT have sourcetrack"

    filters = v_clip.findall("filter")
    assert len(filters) == 4, f"Video clipitem must have 4 filters, got {len(filters)}"

    assert v_clip.find("comments") is not None, "Video clipitem must have comments"


def test_clipitem_audio_structure():
    """Audio clipitem: sourcetrack=True, compositemode=False, 2-3 filters."""
    fcp = _load_test_fcp()
    if fcp is None:
        return

    audio_track = fcp.find(".//media/audio/track")
    assert audio_track is not None, "Expected audio track"
    a_clip = audio_track.findall("clipitem")[0]

    st = a_clip.find("sourcetrack")
    assert st is not None, "Audio clipitem MUST have sourcetrack"
    assert st.findtext("mediatype") == "audio"
    assert st.findtext("trackindex") == "1"

    assert a_clip.find("compositemode") is None, "Audio clipitem must NOT have compositemode"

    filters = a_clip.findall("filter")
    assert len(filters) in (2, 3), f"Audio clipitem must have 2-3 filters, got {len(filters)}"

    assert a_clip.find("comments") is not None, "Audio clipitem must have comments"


def test_clipitem_element_order():
    """Child element order must match DC format exactly.

    DC: name→duration→rate→start→end→enabled→in→out→file
        →compositemode(video)|sourcetrack(audio)→filter*→link*→comments
    """
    fcp = _load_test_fcp()
    if fcp is None:
        return

    # ── Video clipitem order ──
    video_track = fcp.find(".//media/video/track")
    v_clip = video_track.findall("clipitem")[1]
    v_order = [c.tag for c in v_clip]

    # Mandatory prefix
    assert v_order.index("name") == 0, f"name at index {v_order.index('name')}, expected 0"
    assert v_order.index("name") < v_order.index("duration"), "name must be before duration"
    assert v_order.index("duration") < v_order.index("rate"), "duration must be before rate"
    assert v_order.index("rate") < v_order.index("start"), "rate must be before start"
    assert v_order.index("start") < v_order.index("end"), "start must be before end"
    assert v_order.index("end") < v_order.index("enabled"), "end must be before enabled"
    assert v_order.index("enabled") < v_order.index("in"), "enabled must be before in"
    assert v_order.index("in") < v_order.index("out"), "in must be before out"
    assert v_order.index("out") < v_order.index("file"), "out must be before file"

    # file → compositemode → filter* → link* → comments
    assert v_order.index("file") < v_order.index("compositemode"), "file must be before compositemode"
    assert v_order.index("compositemode") < v_order.index("comments"), "compositemode before comments"

    first_filter = v_order.index("filter")
    assert v_order.index("compositemode") < first_filter, "compositemode must be before filters"

    if "link" in v_order:
        last_filter = max(i for i, t in enumerate(v_order) if t == "filter")
        first_link = v_order.index("link")
        assert last_filter < first_link, "filters must be before links"
        assert first_link < v_order.index("comments"), "links must be before comments"

    assert v_order[-1] == "comments", f"last child must be comments, got {v_order[-1]}"

    # ── Audio clipitem order ──
    audio_track = fcp.find(".//media/audio/track")
    if audio_track is not None:
        a_clip = audio_track.findall("clipitem")[0]
        a_order = [c.tag for c in a_clip]

        assert a_order.index("name") == 0
        assert a_order.index("name") < a_order.index("duration")
        assert a_order.index("duration") < a_order.index("rate")
        assert a_order.index("rate") < a_order.index("start")
        assert a_order.index("start") < a_order.index("end")
        assert a_order.index("end") < a_order.index("enabled")
        assert a_order.index("enabled") < a_order.index("in")
        assert a_order.index("in") < a_order.index("out")

        # Audio: file → sourcetrack → filter* → link* → comments
        assert a_order.index("out") < a_order.index("file"), "out must be before file"
        assert a_order.index("file") < a_order.index("sourcetrack"), "file before sourcetrack"
        assert a_order.index("sourcetrack") < a_order.index("comments")

        a_first_filter = a_order.index("filter")
        assert a_order.index("sourcetrack") < a_first_filter, "sourcetrack before filters"

        assert a_order[-1] == "comments"


def test_link_mediatype_audio_only():
    """Video clipitem links must NEVER have <mediatype>.

    Historical bug: shared ET list mutation leaked <mediatype>video</mediatype>
    from audio processing into video link elements, breaking DaVinci import.
    """
    fcp = _load_test_fcp()
    if fcp is None:
        return

    video = fcp.find(".//media/video")
    for ci in video.iter("clipitem"):
        for link in ci.findall("link"):
            mt = link.find("mediatype")
            assert mt is None, \
                f"Video clipitem {ci.get('id')} has mediatype={mt.text if mt is not None else '??'}"


def test_link_independence():
    """Mutating clipitem A's links must not affect clipitem B's links.

    Historical bug: shared ET.Element objects (deepcopy omission) caused
    link removal in one clipitem to silently remove links in another.
    """
    fcp = _load_test_fcp()
    if fcp is None:
        return

    audio_track = fcp.find(".//media/audio/track")
    if audio_track is None:
        return
    clips = audio_track.findall("clipitem")
    if len(clips) < 2:
        return

    a, b = clips[0], clips[1]
    count_b_before = len(b.findall("link"))

    # Mutate A
    for l in a.findall("link"):
        a.remove(l)
    assert len(a.findall("link")) == 0, "A should have no links after removal"

    # B must be unaffected
    assert len(b.findall("link")) == count_b_before, \
        f"B's link count changed from {count_b_before} to {len(b.findall('link'))}"


def test_link_group_connectivity():
    """Every member in a link group references every other member.

    A link group = all clipitems sharing the same source media file.
    Each member's <link> list must contain <linkclipref> entries
    for ALL other members in the group.
    """
    fcp = _load_test_fcp()
    if fcp is None:
        return

    # Collect all clipitem IDs by media name
    all_cis: dict[str, list[str]] = {}
    for ci in fcp.iter("clipitem"):
        name = ci.findtext("name", "")
        if name:
            all_cis.setdefault(name, []).append(ci.get("id", ""))

    for name, ids in all_cis.items():
        if len(ids) < 2:
            continue
        # Every clipitem in this group must link to every other
        for ci_id in ids:
            ci = fcp.find(f".//clipitem[@id='{ci_id}']")
            assert ci is not None, f"Clipitem {ci_id} not found"
            linked_refs = {l.findtext("linkclipref", "") for l in ci.findall("link")}
            for other_id in ids:
                assert other_id in linked_refs, \
                    f"Clipitem {ci_id} ({name}) missing link to {other_id}"


def test_link_order_in_clipitem():
    """Link elements must sit between filters and comments.

    DC convention for ALL clipitems: filter* → link* → comments (last)
    """
    fcp = _load_test_fcp()
    if fcp is None:
        return

    for clipitem in fcp.iter("clipitem"):
        tags = [c.tag for c in clipitem]

        if "link" not in tags:
            continue

        # All filters must be before all links
        last_filter = max(i for i, t in enumerate(tags) if t == "filter")
        first_link = tags.index("link")
        assert last_filter < first_link, \
            f"Clipitem {clipitem.get('id')}: filters must be before links"

        # All links must be before comments
        last_link = max(i for i, t in enumerate(tags) if t == "link")
        comments_pos = tags.index("comments")
        assert last_link < comments_pos, \
            f"Clipitem {clipitem.get('id')}: links must be before comments"


def test_sequence_skeleton_regression():
    """Extract sequence skeleton: verify top-level FCP7 XML structure.

    Checks xmeml version, sequence child order, timecode format,
    and media section layout (video before audio).
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    for s in root.findall("Sequence"):
        if s.findtext("Name", "") == "序列 01":
            fcp = _prproj_parse_sequence(root, s.get("ObjectUID"), test_proj)
            break
    else:
        return

    # Top-level
    assert fcp.tag == "xmeml"
    assert fcp.get("version") == "5"

    seq = fcp.find("sequence")
    seq_children = [c.tag for c in seq]
    assert seq_children[0] == "name"
    assert seq_children[1] == "duration"
    assert seq_children[2] == "rate"
    assert seq_children[3] == "in"
    assert seq_children[4] == "out"
    assert seq_children[5] == "timecode"
    assert seq_children[6] == "media"

    # In/out = -1 (DC convention)
    assert seq.findtext("in") == "-1"
    assert seq.findtext("out") == "-1"

    # Timecode
    tc = seq.find("timecode")
    assert tc.find("string") is not None
    assert tc.find("frame") is not None
    assert tc.find("displayformat") is not None
    assert tc.find("rate") is not None

    # Media: video before audio
    media = seq.find("media")
    m_children = [c.tag for c in media]
    assert m_children[0] == "video", "video must be before audio in media"
    assert m_children[1] == "audio"

    # Video has tracks
    video = media.find("video")
    assert video.find("track") is not None, "video section must have tracks"

    # Audio has tracks
    audio = media.find("audio")
    assert audio.find("track") is not None, "audio section must have tracks"


def test_extract_clip_regression():
    """Extract _extract_clip to module scope: verify ClipData→XML mapping.

    Tests via public pipeline — ClipData values should produce
    the expected XML attributes in the output.
    """
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    for s in root.findall("Sequence"):
        if s.findtext("Name", "") == "序列 01":
            fcp = _prproj_parse_sequence(root, s.get("ObjectUID"), test_proj)
            break
    else:
        return

    # Verify the primary sequence has at least 70 clips (known count)
    all_clips = list(fcp.iter("clipitem"))
    assert len(all_clips) >= 70, \
        f"Expected >= 70 clipitems, got {len(all_clips)}"

    # Verify every clipitem has a non-empty name (from ClipData.name)
    for ci in all_clips:
        name = ci.findtext("name", "")
        assert name, "Clipitem must have non-empty name"

    # Verify video/audio file IDs are consistent (ClipData.name = FileData.name)
    video_track = fcp.find(".//media/video/track")
    v_clips = video_track.findall("clipitem")
    assert len(v_clips) >= 2
    second = v_clips[1]
    second_name = second.findtext("name", "")
    second_file_id = second.find("file").get("id", "")
    assert second_name in second_file_id, \
        f"File ID '{second_file_id}' should contain clip name '{second_name}'"


def test_resolve_pathurl_consistency():
    """_resolve_pathurl() must handle %-encoded chars identically for both formats.

    Historical bug: _extract_media_files() used unquote() but
    _strip_file_elements_for_drt() did NOT, causing false offline detection
    for paths with spaces (%20) or CJK characters.
    """
    from pr2_engine import _resolve_pathurl

    # file:/// with %20 (space)
    assert _resolve_pathurl("file:///D:/My%20Videos/test.mov") == "D:\\My Videos\\test.mov"

    # file://localhost/ with %20 (space) — should resolve same way
    result1 = _resolve_pathurl("file://localhost/D:/My%20Videos/test.mov")
    result2 = _resolve_pathurl("file:///D:/My%20Videos/test.mov")
    assert result1 == result2, f"Format mismatch: '{result1}' vs '{result2}'"

    # CJK characters (荷花 = %E8%8D%B7%E8%8A%B1)
    cjk1 = _resolve_pathurl("file:///E:/HW/%E8%8D%B7%E8%8A%B1/video.mov")
    cjk2 = _resolve_pathurl("file://localhost/E:/HW/%E8%8D%B7%E8%8A%B1/video.mov")
    assert cjk1 == cjk2, f"CJK mismatch: '{cjk1}' vs '{cjk2}'"
    assert "荷花" in cjk1, f"CJK not decoded: '{cjk1}'"

    # Empty/None
    assert _resolve_pathurl("") is None
    assert _resolve_pathurl(None) is None


def test_pathurl_import():
    """End-to-end: _extract_media_files and _strip_file_elements_for_drt
    must both correctly detect online media for a real XML file.

    Creates a minimal XML with percent-encoded paths and verifies both
    functions produce correct results.
    """
    import xml.etree.ElementTree as ET
    import tempfile, os
    from pr2_engine import _extract_media_files, _strip_file_elements_for_drt

    # Create a temp XML with a real path to an existing file
    tmp_dir = Path(tempfile.gettempdir())
    test_xml = tmp_dir / "_pr2resolve_test_pathurl.xml"
    existing_file = Path(__file__).resolve()  # any existing file

    # Quote spaces to test %-decoding
    raw_path = str(existing_file)
    encoded = raw_path.replace(" ", "%20").replace("\\", "/")
    if encoded[1:2] == ":":
        encoded = "file://localhost/" + encoded

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5"><sequence><media><video><track>
<clipitem id="test"><file id="test"><pathurl>{encoded}</pathurl></file></clipitem>
</track></video></media></sequence></xmeml>"""
    test_xml.write_text(xml_content, encoding="utf-8")

    try:
        # Both functions must detect the file as online
        files = _extract_media_files(test_xml)
        assert len(files) > 0, "_extract_media_files: failed to detect online media"

        result_path, is_skeleton = _strip_file_elements_for_drt(test_xml)
        assert not is_skeleton, \
            f"_strip_file_elements_for_drt: incorrectly classified as offline"
    finally:
        test_xml.unlink(missing_ok=True)


def test_fps_inpoint_regression():
    """in/out ticks→frames must use source fps (not timeline fps).

    PR stores InPoint/OutPoint ticks at fixed rate 254016000000/sec.
    These express SOURCE clip trim — not timeline position.

    This test proves the FPS conversion dependency:
    same ticks → different frame counts at different fps values.
    The _extract_clip fix uses source_tc.media_fps (not sequence fps)
    when source TC is resolved.
    """
    from pr2_engine import _prproj_ticks_to_frames

    ticks_per_frame = 254016000000 / 59.94
    in_ticks_f100 = int(100 * ticks_per_frame)

    seq_fps = _prproj_ticks_to_frames(str(in_ticks_f100), 24.0)
    src_fps = _prproj_ticks_to_frames(str(in_ticks_f100), 59.94)
    ratio = abs(seq_fps - src_fps) / max(src_fps, 1)

    # Using wrong fps produces >50% error — this IS the bug for mixed-fps timelines
    assert ratio > 0.5, \
        f"fps sensitivity not proven: seq_fps={seq_fps} src_fps={src_fps} ratio={ratio:.1%}"


def test_drp_import_source_clips_folders():
    """RED: _drp_export must pass sourceClipsFolders to ImportTimelineFromFile.

    Without sourceClipsFolders, importSourceClips=False produces timeline clips
    with GetMediaPoolItem()==null — red timeline, relink fails.
    """
    import inspect as _inspect
    from pr2_engine import _drp_export

    source = _inspect.getsource(_drp_export)
    assert "sourceClipsFolders" in source, (
        "sourceClipsFolders must be passed to ImportTimelineFromFile "
        "in _drp_export"
    )


def test_source_tc_cache_hit():
    """PR4-A: _SourceTCCache caches per file path, avoiding redundant probes."""
    from pr2_engine import _SourceTCCache, _SourceTCInfo

    _SourceTCCache.clear()

    fake = _SourceTCInfo()
    fake.timecode_string = "17:00:53:52"
    fake.media_fps = 59.94
    fake.is_ntsc = True
    fake.resolved = True

    path = r"D:\test\DJI_0208.MP4"
    _SourceTCCache[path] = fake

    assert path in _SourceTCCache
    assert _SourceTCCache[path] is fake
    assert _SourceTCCache[path].timecode_string == "17:00:53:52"
    assert _SourceTCCache[path].resolved is True

    _SourceTCCache.clear()


def test_dji_tmcd_direct_read():
    """PR4-B: _read_dji_tmcd_timecode returns Pool TC for DJI MP4 files."""
    from pr2_engine import _read_dji_tmcd_timecode

    djipath = r"E:\HW\2026.5.29 荷花\Video\DJI_20260528184922_0208_D.MP4"
    if not __import__("os").path.exists(djipath):
        return

    info = _read_dji_tmcd_timecode(djipath)
    assert info is not None, "tmcd read should succeed for DJI MP4"
    assert info.resolved, "TC should be resolved from tmcd"
    assert info.timecode_string == "17:00:53:52", (
        f"Expected '17:00:53:52', got '{info.timecode_string}'"
    )
    assert info.media_fps == 60.0, f"Expected 60.0 fps, got {info.media_fps}"


def test_ffprobe_tc_priority_over_mediainpoint():
    """PR2: ffprobe source TC must take priority over MediaInPoint-derived TC.

    Also patches Path.exists so the .prproj media path (from the author's
    machine) appears to exist on the test machine, allowing the ffprobe
    mock to be reached.
    """
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj, _SourceTCInfo

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe(filepath):
        info = _SourceTCInfo()
        info.media_fps = 59.94
        info.is_ntsc = True
        info.timecode_string = "17:00:53;52"
        info.timecode_frame = 3663640
        info.resolved = True
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe), \
         patch("pathlib.Path.exists", return_value=True):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "DJI_20260528184922_0208_D.MP4":
            tc_str = ci.findtext("file/timecode/string", "")
            assert tc_str in ("17:00:53;52", "17:00:53:52"), (
                f"Expected TC '17:00:53:52', got '{tc_str}'"
            )
            return

    assert False, "DJI_0208 clipitem not found"


def test_mediainpoint_fallback_without_ffprobe():
    """PR2: without ffprobe, MediaInPoint-derived TC is the fallback."""
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj, _SourceTCInfo

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe_unavailable(filepath):
        info = _SourceTCInfo()
        info.resolved = False
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe_unavailable):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    dji_found = False
    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "DJI_20260528184922_0208_D.MP4":
            tc_str = ci.findtext("file/timecode/string", "")
            assert tc_str != "00:00:00:00", (
                f"Expected non-zero MIP fallback TC, got '{tc_str}'"
            )
            dji_found = True
            break

    assert dji_found, "DJI_0208 clipitem not found"


def test_mvi_zero_tc_regression():
    """PR2: MVI clips with MIP=0 must produce 00:00:00:00 TC (no SMPTE TC)."""
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence, _SourceTCCache
    from pr2_constants import load_prproj, _SourceTCInfo

    _SourceTCCache.clear()

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe_none(filepath):
        info = _SourceTCInfo()
        info.resolved = False
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe_none), \
         patch("pr2_engine._read_mov_creation_time_tc", return_value=None), \
         patch("pathlib.Path.exists", return_value=True):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and "MVI" in (fn.text or ""):
            tc_str = ci.findtext("file/timecode/string", "")
            assert tc_str == "00:00:00:00", (
                f"MVI clip {fn.text} TC should be 00:00:00:00, got '{tc_str}'"
            )


def test_dji_timecode_priority():
    """PR2: format_tags=timecode must take priority over creation_time."""
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj, _SourceTCInfo

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe_dji(filepath):
        info = _SourceTCInfo()
        info.media_fps = 59.94
        info.is_ntsc = True
        info.timecode_string = "17:00:53;52"
        info.resolved = True
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe_dji), \
         patch("pathlib.Path.exists", return_value=True):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "DJI_20260528184922_0208_D.MP4":
            tc_str = ci.findtext("file/timecode/string", "")
            assert tc_str in ("17:00:53;52", "17:00:53:52"), (
                f"Expected TC '17:00:53:52', got '{tc_str}'"
            )
            return

    assert False, "DJI_0208 clipitem not found"


def test_no_metadata_fallback():
    """PR2: no SMPTE TC and no creation_time → fallback to MediaInPoint."""
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence, _SourceTCCache
    from pr2_constants import load_prproj, _SourceTCInfo

    _SourceTCCache.clear()

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe_no_metadata(filepath):
        info = _SourceTCInfo()
        info.resolved = False
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe_no_metadata), \
         patch("pr2_engine._read_mov_creation_time_tc", return_value=None), \
         patch("pathlib.Path.exists", return_value=True):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)
    found_mvi = False
    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "MVI_1688.MOV":
            tc_str = ci.findtext("file/timecode/string", "")
            assert tc_str == "00:00:00:00", (
                f"Expected fallback TC '00:00:00:00', got '{tc_str}'"
            )
            found_mvi = True

    assert found_mvi, "MVI_1688 clipitem not found"


def test_tmcd_stco_validation_rejects_a001():
    """PR5: tmcd with stco >= video_first_stco must be rejected (A001 garbage).

    DJI: tmcd_stco (~60924) < video_first_stco (~60928) → valid
    A001: tmcd_stco (~16M) >> video_first_stco (16384) → invalid
    """
    from pr2_engine import _read_dji_tmcd_timecode

    # DJI_0208: valid tmcd
    djipath = r"E:\HW\2026.5.29 荷花\Video\DJI_20260528184922_0208_D.MP4"
    if __import__("os").path.exists(djipath):
        dji = _read_dji_tmcd_timecode(djipath)
        assert dji is not None, "DJI_0208 must resolve via valid tmcd"
        assert dji.resolved, "DJI_0208 tmcd must be resolved"
        assert dji.timecode_string == "17:00:53:52", (
            f"Expected 17:00:53:52, got {dji.timecode_string}"
        )

    # A001_C002: invalid tmcd (stco points into video frame data)
    a001path = r"E:\HW\2026.5.29 荷花\Video\Media\A001_05301301_C002.mov"
    if __import__("os").path.exists(a001path):
        a001 = _read_dji_tmcd_timecode(a001path)
        assert a001 is None, (
            "A001 must return None — tmcd stco is garbage inside video frames"
        )


def test_final_tc_priority_chain():
    """PR5: Final TC priority — cache → tmcd(validated) → ffprobe → 00:00:00:00.

    DJI: tmcd valid → cache → "17:00:53:52"
    A001: tmcd invalid → ffprobe → "13:01:15:00"
    MVI: no tmcd → ffprobe → resolved=False → MIP=0 → "00:00:00:00"
    """
    from unittest.mock import patch
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence, _SourceTCCache
    from pr2_constants import load_prproj, _SourceTCInfo

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    _SourceTCCache.clear()
    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    def fake_ffprobe(filepath):
        """Simulate ffprobe: returns correct TC for DJI, creation_time=empty for MVI."""
        info = _SourceTCInfo()
        info.media_fps = 59.94
        info.is_ntsc = True
        # This ffprobe returns correct TC for all known files
        basename = __import__("os").path.basename(filepath)
        if "DJI_0208" in basename:
            info.timecode_string = "17:00:53:52"
        elif "DJI_0209" in basename:
            info.timecode_string = "17:01:42:30"
        elif "A001_C002" in basename or "A001_05301301" in basename:
            info.timecode_string = "13:01:15:00"
        elif "A001_C005" in basename or "A001_05301303" in basename:
            info.timecode_string = "13:03:40:00"
        elif "A001_C019" in basename or "A001_05301327" in basename:
            info.timecode_string = "13:27:24:00"
        elif "MVI" in basename:
            info.resolved = False  # MVI has no TC metadata
        else:
            info.resolved = False
        if info.timecode_string != "00:00:00:00":
            info.resolved = True
        return info

    with patch("pr2_engine._ffprobe_read_timecode", side_effect=fake_ffprobe), \
         patch("pathlib.Path.exists", return_value=True):
        fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # DJI_0208: must get "17:00:53:52"
    found_dji = False
    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "DJI_20260528184922_0208_D.MP4":
            tc = ci.findtext("file/timecode/string", "")
            assert tc == "17:00:53:52", f"DJI_0208 expected 17:00:53:52, got {tc}"
            found_dji = True
    assert found_dji, "DJI_0208 not found"

    # MVI_1688: must get "00:00:00:00" (ffprobe resolved=False → MIP=0)
    found_mvi = False
    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is not None and fn.text == "MVI_1688.MOV":
            tc = ci.findtext("file/timecode/string", "")
            assert tc == "00:00:00:00", f"MVI_1688 expected 00:00:00:00, got {tc}"
            found_mvi = True
    assert found_mvi, "MVI_1688 not found"

    _SourceTCCache.clear()


def test_scaletoframe_off_skips_autofit():
    """PR6: ShouldScaleMedia=false must skip M7 scale auto-fit.

    DJI_0229 (1728x3072) on 1920x1080 timeline with ShouldScaleMedia=false
    should retain 100% scale, NOT be rewritten to 35.2%.
    """
    import gzip, xml.etree.ElementTree as ET2
    from pr2_engine import _PrprojIndex, _prproj_parse_sequence
    from pr2_constants import load_prproj

    test_proj = Path(__file__).resolve().parent.parent / "test" / "Pr test" / "黑哥们的语言是不通的.prproj"
    if not test_proj.exists():
        return

    root = load_prproj(test_proj)
    seqs = root.findall("Sequence")
    primary = None
    for s in seqs:
        if s.findtext("Name", "") == "序列 01":
            primary = s; break
    if primary is None:
        return

    # Verify ShouldScaleMedia is false in the .prproj
    ssm = root.findtext("ProjectSettings/ShouldScaleMedia", "")
    assert ssm == "false", (
        f"Expected ShouldScaleMedia='false', got '{ssm}'"
    )

    fcp = _prproj_parse_sequence(root, primary.get("ObjectUID"), test_proj)

    # DJI_0229: source 1728x3072, timeline 1920x1080
    # Should retain 100% scale (no auto-fit) because ShouldScaleMedia=false
    for ci in fcp.iter("clipitem"):
        fn = ci.find("file/name")
        if fn is None or fn.text != "DJI_20260530122217_0229_D.MP4":
            continue
        basic = ci.find("filter/effect/[effectid='basic']")
        if basic is not None:
            scale_p = basic.find("parameter/[name='Scale']")
            if scale_p is not None:
                scale_val = float(scale_p.findtext("value", "100"))
                assert abs(scale_val - 100.0) < 0.1, (
                    f"ShouldScaleMedia=false => scale must stay at 100%, "
                    f"got {scale_val}"
                )


def test_drp_export_sets_color_science():
    """PR6: _drp_export must call SetSetting for color science mode.

    Currently _drp_export only sets timelineStartTimecode.  It must also
    set colorScience and timelineColorSpace to prevent DaVinci defaults
    from diverging from Premiere's BT.709 display-referred workflow.
    """
    import inspect as _inspect
    from pr2_engine import _drp_export

    source = _inspect.getsource(_drp_export)
    assert "colorScience" in source or "color_profile" in source, (
        "_drp_export must set colorScience on the Resolve project"
    )


if __name__ == "__main__":
    import inspect
    ts = sorted(
        (n, f) for n, f in inspect.getmembers(sys.modules[__name__], inspect.isfunction)
        if n.startswith("test_")
    )
    p = f2 = 0
    for n, fn in ts:
        try:
            fn()
            print(f"  OK {n}")
            p += 1
        except (AssertionError, Exception) as e:
            print(f"  FAIL {n}: {e}")
            f2 += 1
    print(f"\n  {p} passed, {f2} failed")
    sys.exit(1 if f2 else 0)