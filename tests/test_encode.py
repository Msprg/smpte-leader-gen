"""Tests for ``smpte_leader_gen.encode`` (CONTRACT.md §7, §11).

Covers FFmpeg discovery (skipped if ffmpeg is absent), SMPTE timecode round-trips
for non-drop and drop-frame including a known drop-frame reference value, and
FFmpeg/ffprobe argv construction (exact rational ``-r``, per-profile pixel format,
``;`` drop-frame timecode, PCM codec by bit depth, and AAC for the h264 profile).
"""

from __future__ import annotations

import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from smpte_leader_gen.config import build_config
from smpte_leader_gen.encode import (
    FFmpegError,
    build_ffmpeg_command,
    build_ffprobe_streams_cmd,
    discover_ffmpeg,
    frames_to_tc,
    is_drop_frame,
    parse_tc,
)
from smpte_leader_gen.profiles import get_profile
from smpte_leader_gen.timing import build_schedule

FPS_2997 = Fraction(30000, 1001)
FPS_5994 = Fraction(60000, 1001)
FPS_2398 = Fraction(24000, 1001)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: object):
    """Build a minimal valid Config for command-building tests."""
    base: dict[str, object] = {
        "fps": "24",
        "resolution": "128x72",
        "output": "out.mov",
        "profile": "prores422hq",
        "lead_seconds": 8,
        "tail_seconds": 1.0,
    }
    base.update(overrides)
    return build_config(**base)


def _make_schedule(config):
    """Build a Schedule matching a Config (mirrors pipeline adapter logic)."""
    return build_schedule(
        fps=config.fps,
        sample_rate=config.sample_rate,
        frames_per_block_override=config.frames_per_block,
        lead_seconds=config.lead_seconds,
        tail_frames=config.tail_frames,
        preroll_frames=config.preroll_frames,
        beep_frames=config.beep_frames,
        persecond_beep=not config.no_persecond_beep,
        twopop=not config.no_twopop,
        count_style=config.count_style,
    )


# ---------------------------------------------------------------------------
# discover_ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_discover_ffmpeg_version_at_least_6() -> None:
    ffmpeg, ffprobe, version = discover_ffmpeg()
    assert isinstance(ffmpeg, str) and ffmpeg
    assert isinstance(ffprobe, str) and ffprobe
    assert len(version) == 3
    assert version[0] >= 6, f"expected major >= 6, got {version}"


def test_discover_ffmpeg_missing_path_raises() -> None:
    with pytest.raises(FFmpegError):
        discover_ffmpeg("/nonexistent/dir/definitely-not-ffmpeg")


# ---------------------------------------------------------------------------
# is_drop_frame
# ---------------------------------------------------------------------------


def test_is_drop_frame_families() -> None:
    assert is_drop_frame(FPS_2997, no_drop_frame=False) is True
    assert is_drop_frame(FPS_5994, no_drop_frame=False) is True
    # Non-drop families.
    assert is_drop_frame(Fraction(24), no_drop_frame=False) is False
    assert is_drop_frame(Fraction(25), no_drop_frame=False) is False
    assert is_drop_frame(Fraction(30), no_drop_frame=False) is False
    assert is_drop_frame(FPS_2398, no_drop_frame=False) is False
    # Explicit opt-out forces non-drop even for 29.97.
    assert is_drop_frame(FPS_2997, no_drop_frame=True) is False


# ---------------------------------------------------------------------------
# frames_to_tc / parse_tc round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", [Fraction(24), Fraction(25), Fraction(30), FPS_2398])
@pytest.mark.parametrize("frame", [0, 1, 23, 24, 25, 100, 1000, 86399])
def test_nondrop_roundtrip(fps: Fraction, frame: int) -> None:
    tc = frames_to_tc(frame, fps, drop_frame=False)
    assert ":" in tc and ";" not in tc
    assert parse_tc(tc, fps) == frame


@pytest.mark.parametrize("fps", [FPS_2997, FPS_5994])
@pytest.mark.parametrize(
    "frame",
    [0, 1, 29, 30, 60, 1799, 1800, 1801, 3599, 3600, 17981, 17982, 50000],
)
def test_dropframe_roundtrip(fps: Fraction, frame: int) -> None:
    tc = frames_to_tc(frame, fps, drop_frame=True)
    assert ";" in tc
    assert parse_tc(tc, fps) == frame


def test_dropframe_known_values_2997() -> None:
    """Canonical 29.97 drop-frame reference points."""
    # At the 1-minute boundary, frame numbers 00 and 01 are dropped, so frame
    # 1800 reads as ;02 (not ;00).
    assert frames_to_tc(1800, FPS_2997, drop_frame=True) == "00:01:00;02"
    # The last frame of nominal minute 0 reads normally.
    assert frames_to_tc(1799, FPS_2997, drop_frame=True) == "00:00:59;29"
    # No drop at minutes divisible by 10.
    assert frames_to_tc(17982, FPS_2997, drop_frame=True) == "00:10:00;00"
    # Frame 0.
    assert frames_to_tc(0, FPS_2997, drop_frame=True) == "00:00:00;00"


def test_dropframe_known_values_5994() -> None:
    """59.94 drops 4 frame numbers per drop-minute."""
    assert frames_to_tc(3600, FPS_5994, drop_frame=True) == "00:01:00;04"
    assert frames_to_tc(3599, FPS_5994, drop_frame=True) == "00:00:59;59"


def test_nondrop_separator_and_format() -> None:
    assert frames_to_tc(0, Fraction(24), drop_frame=False) == "00:00:00:00"
    assert frames_to_tc(24, Fraction(24), drop_frame=False) == "00:00:01:00"
    assert frames_to_tc(25, Fraction(24), drop_frame=False) == "00:00:01:01"


def test_parse_tc_accepts_both_separators() -> None:
    assert parse_tc("00:00:01:00", Fraction(24)) == 24
    assert parse_tc("00:01:00;02", FPS_2997) == 1800


def test_parse_tc_rejects_bad_format() -> None:
    with pytest.raises(ValueError):
        parse_tc("not-a-timecode", Fraction(24))
    with pytest.raises(ValueError):
        parse_tc("00:00:00", Fraction(24))


def test_frames_to_tc_rejects_negative() -> None:
    with pytest.raises(ValueError):
        frames_to_tc(-1, Fraction(24), drop_frame=False)


# ---------------------------------------------------------------------------
# build_ffmpeg_command
# ---------------------------------------------------------------------------


def _opt_value(cmd: list[str], flag: str) -> str:
    """Return the argument that follows the LAST occurrence of ``flag``."""
    idx = max(i for i, tok in enumerate(cmd) if tok == flag)
    return cmd[idx + 1]


def test_command_exact_rational_rate_2398() -> None:
    config = _make_config(fps="23.976")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("out.mov"),
    )
    # Exact rational, twice (input -r and output -r). Never a decimal.
    assert cmd.count("-r") == 2
    for i, tok in enumerate(cmd):
        if tok == "-r":
            assert cmd[i + 1] == "24000/1001"
    assert "23.976" not in cmd
    assert "23.98" not in cmd


def test_command_rate_for_30000_1001() -> None:
    config = _make_config(fps="29.97", profile="prores422hq")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("out.mov"),
    )
    rate_idx = cmd.index("-r")
    assert cmd[rate_idx + 1] == "30000/1001"


def test_command_structure_and_inputs() -> None:
    config = _make_config()
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("audio.wav"),
        output_path=Path("final.mov"),
    )
    assert cmd[0] == "ffmpeg"
    assert "-y" in cmd and "-hide_banner" in cmd
    # rawvideo rgb24 stdin input.
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "rawvideo"
    assert _opt_value(cmd, "-pix_fmt") == profile.pix_fmt  # last pix_fmt = output
    # stdin input dash and wav input.
    assert "-i" in cmd
    i_indices = [i for i, tok in enumerate(cmd) if tok == "-i"]
    assert cmd[i_indices[0] + 1] == "-"
    assert cmd[i_indices[1] + 1] == "audio.wav"
    # mapping.
    assert "-map" in cmd
    map_vals = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-map"]
    assert map_vals == ["0:v:0", "1:a:0"]
    # size matches resolution.
    assert _opt_value(cmd, "-s") == "128x72"
    # output path is last.
    assert cmd[-1] == "final.mov"


def test_command_bt709_color_metadata() -> None:
    config = _make_config()
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    assert _opt_value(cmd, "-color_primaries") == "bt709"
    assert _opt_value(cmd, "-color_trc") == "bt709"
    assert _opt_value(cmd, "-colorspace") == "bt709"


def test_command_video_codec_and_opts() -> None:
    config = _make_config(profile="prores422hq")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    assert _opt_value(cmd, "-c:v") == "prores_ks"
    # video_opts present in order.
    joined = " ".join(cmd)
    assert "-profile:v 3" in joined


def test_command_pix_fmt_per_profile() -> None:
    cases = {
        "prores422hq": "yuv422p10le",
        "prores4444": "yuva444p10le",
        "ffv1": "yuv422p10le",
        "x264lossless": "yuv444p",
        "h264": "yuv420p",
    }
    for profile_name, expected_pix in cases.items():
        # Even dims (128x72) satisfy the chroma-subsampled profiles too.
        config = _make_config(profile=profile_name, container=None, output="o")
        schedule = _make_schedule(config)
        profile = get_profile(config.profile)
        cmd = build_ffmpeg_command(
            ffmpeg="ffmpeg",
            config=config,
            schedule=schedule,
            profile=profile,
            audio_path=Path("a.wav"),
            output_path=Path("o." + profile.container),
        )
        # The (only) output pix_fmt for a single video output stream.
        assert _opt_value(cmd, "-pix_fmt") == expected_pix


def test_command_dropframe_timecode_for_2997() -> None:
    config = _make_config(fps="29.97", profile="prores422hq")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    tc = _opt_value(cmd, "-timecode")
    assert ";" in tc, f"expected drop-frame ';' separator, got {tc!r}"
    assert tc == "00:00:00;00"


def test_command_nondrop_timecode_when_disabled() -> None:
    config = _make_config(fps="29.97", no_drop_frame=True, profile="prores422hq")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    tc = _opt_value(cmd, "-timecode")
    assert ";" not in tc and ":" in tc
    assert tc == "00:00:00:00"


def test_command_nondrop_timecode_for_24fps() -> None:
    config = _make_config(fps="24")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    tc = _opt_value(cmd, "-timecode")
    assert ";" not in tc


def test_command_audio_pcm_s24le_by_bit_depth() -> None:
    config = _make_config(profile="prores422hq", bit_depth=24)
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    assert _opt_value(cmd, "-c:a") == "pcm_s24le"
    assert "-b:a" not in cmd


def test_command_audio_pcm_s16le_by_bit_depth() -> None:
    config = _make_config(profile="prores422hq", bit_depth=16)
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mov"),
    )
    assert _opt_value(cmd, "-c:a") == "pcm_s16le"


def test_command_audio_aac_for_h264_profile() -> None:
    # h264 profile declares aac; even with bit_depth 24 it must stay aac.
    config = _make_config(profile="h264", container="mp4", bit_depth=24, output="o.mp4")
    schedule = _make_schedule(config)
    profile = get_profile(config.profile)
    cmd = build_ffmpeg_command(
        ffmpeg="ffmpeg",
        config=config,
        schedule=schedule,
        profile=profile,
        audio_path=Path("a.wav"),
        output_path=Path("o.mp4"),
    )
    assert _opt_value(cmd, "-c:a") == "aac"
    assert _opt_value(cmd, "-b:a") == "320k"


# ---------------------------------------------------------------------------
# build_ffprobe_streams_cmd
# ---------------------------------------------------------------------------


def test_build_ffprobe_streams_cmd() -> None:
    cmd = build_ffprobe_streams_cmd("ffprobe", Path("/tmp/x.mov"))
    assert cmd[0] == "ffprobe"
    assert "-show_streams" in cmd
    assert "-show_format" in cmd
    assert cmd[cmd.index("-of") + 1] == "json"
    assert cmd[-1] == "/tmp/x.mov"
    assert cmd[cmd.index("-v") + 1] == "error"
