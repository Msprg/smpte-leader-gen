"""Regression tests for issues found by the adversarial review pass.

Each test pins one confirmed fix so it cannot silently regress:
  1. 2-pop requires lead_seconds >= 2 (else the canonical marker is lost).
  2. The resolved container governs the muxer (``--container`` override works;
     an incompatible output extension is rejected up front).
  3. ffmpeg stderr is drained concurrently and captured on failure.
  4. A multi-frame flash that spills into the tail (beep_frames > F) stays visible.
  5. CLI validation errors print a clean one-line message (no pydantic dump).
  6. start_timecode / metronome_start fail fast at config time.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pydantic
import pytest
from click.testing import CliRunner

from smpte_leader_gen.cli import main
from smpte_leader_gen.config import build_config
from smpte_leader_gen.encode import FFmpegError, build_ffmpeg_command
from smpte_leader_gen.profiles import get_profile
from smpte_leader_gen.render import FrameRenderer
from smpte_leader_gen.timing import build_schedule
from smpte_leader_gen import pipeline
from smpte_leader_gen.pipeline import build_schedule_from_config

_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --- Fix 1: 2-pop requires lead_seconds >= 2 -------------------------------


def test_twopop_requires_lead_seconds_2(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError, match="2-pop"):
        build_config(profile="ffv1", fps="24", resolution="128x72",
                     lead_seconds=1, output=tmp_path / "o.mkv")


def test_lead_1_ok_with_no_twopop(tmp_path: Path) -> None:
    cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                       lead_seconds=1, no_twopop=True, output=tmp_path / "o.mkv")
    assert cfg.lead_seconds == 1


def test_build_schedule_skips_out_of_range_twopop() -> None:
    # Direct (non-config) call: must not schedule a negative/pre-roll 2-pop.
    sched = build_schedule(
        fps=__import__("fractions").Fraction(24), sample_rate=48000,
        frames_per_block_override=None, lead_seconds=1, tail_frames=24,
        preroll_frames=0, beep_frames=1, persecond_beep=True, twopop=True,
        count_style="full",
    )
    assert all(c.frame >= sched.countdown_start_frame for c in sched.beep_cues)
    assert not any(c.is_twopop for c in sched.beep_cues)
    assert sched.twopop_flash_frames == frozenset()


# --- Fix 2: container governs the muxer ------------------------------------


@pytest.mark.parametrize("profile_name,muxer", [
    ("prores422hq", "mov"), ("ffv1", "matroska"), ("h264", "mp4"),
])
def test_build_command_forces_muxer(tmp_path: Path, profile_name: str, muxer: str) -> None:
    profile = get_profile(profile_name)
    out = tmp_path / f"o.{profile.container}"
    cfg = build_config(profile=profile_name, fps="24", resolution="128x72",
                       lead_seconds=2, tail_seconds=1, output=out)
    sched = build_schedule_from_config(cfg)
    cmd = build_ffmpeg_command(ffmpeg="ffmpeg", config=cfg, schedule=sched,
                               profile=profile, audio_path=tmp_path / "a.wav",
                               output_path=out)
    # The muxer is forced right before the output path.
    assert cmd[-3:] == ["-f", muxer, str(out)]


def test_container_override_changes_muxer(tmp_path: Path) -> None:
    out = tmp_path / "leader.mkv"  # .mkv extension ...
    cfg = build_config(profile="prores422hq", fps="24", resolution="128x72",
                       lead_seconds=2, tail_seconds=1, container="mov", output=out)
    assert cfg.container == "mov"  # ... but --container mov wins
    cmd = build_ffmpeg_command(ffmpeg="ffmpeg", config=cfg, schedule=build_schedule_from_config(cfg),
                               profile=get_profile("prores422hq"),
                               audio_path=tmp_path / "a.wav", output_path=out)
    assert "-f" in cmd and cmd[cmd.index("-f", cmd.index("-i")) + 1] == "mov"


def test_incompatible_output_extension_rejected(tmp_path: Path) -> None:
    # ProRes into .mp4 (no --container) must fail fast, not crash ffmpeg later.
    with pytest.raises(ValueError, match="mp4"):
        build_config(profile="prores422hq", fps="24", resolution="128x72",
                     lead_seconds=2, output=tmp_path / "o.mp4")


def test_output_extension_derives_container(tmp_path: Path) -> None:
    # ffv1 allows mkv; .mkv extension resolves to mkv without --container.
    cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                       lead_seconds=2, output=tmp_path / "o.mkv")
    assert cfg.container == "mkv"


# --- Fix 3: stderr drained + captured on failure ---------------------------


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not available")
def test_ffmpeg_failure_captures_stderr(tmp_path: Path) -> None:
    cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                       lead_seconds=2, tail_seconds=1, output=tmp_path / "x.mkv")
    sched = build_schedule_from_config(cfg)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", "128x72", "-r", "24/1", "-i", "-", "-c:v", "no_such_encoder",
           str(tmp_path / "err.mkv")]
    with pytest.raises(FFmpegError) as ei:
        pipeline._stream_render(cfg, sched, cmd, progress=False)
    assert "no_such_encoder" in str(ei.value) or "Unknown encoder" in str(ei.value)


# --- Fix 4: multi-frame flash overlapping the tail stays visible -----------


def test_tail_flash_when_beep_longer_than_block(tmp_path: Path) -> None:
    cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                       lead_seconds=8, tail_seconds=2, beep_frames=30,
                       output=tmp_path / "o.mkv")
    sched = build_schedule_from_config(cfg)
    renderer = FrameRenderer(cfg, sched)
    tail_flash = sorted(f for f in sched.flash_frames if f >= sched.ffop_frame)
    assert tail_flash, "expected the long beep to spill flash frames into the tail"
    for f in tail_flash:
        arr = np.frombuffer(renderer.render_frame(f), dtype=np.uint8)
        assert arr.max() > 0, f"tail flash frame {f} rendered black"
    # A non-flash tail frame is still pure black.
    non_flash = next(i for i in range(sched.ffop_frame, sched.total_frames)
                     if i not in sched.flash_frames)
    assert np.frombuffer(renderer.render_frame(non_flash), dtype=np.uint8).max() == 0


def test_tail_flash_respects_no_flash(tmp_path: Path) -> None:
    cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                       lead_seconds=8, tail_seconds=2, beep_frames=30,
                       no_flash=True, output=tmp_path / "o.mkv")
    sched = build_schedule_from_config(cfg)
    renderer = FrameRenderer(cfg, sched)
    for f in (f for f in sched.flash_frames if f >= sched.ffop_frame):
        assert np.frombuffer(renderer.render_frame(f), dtype=np.uint8).max() == 0


# --- Fix 5: clean CLI validation messages ----------------------------------


def test_cli_validation_message_is_clean(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(main, [
        "-o", str(tmp_path / "o.mov"), "--profile", "prores422hq",
        "--resolution", "127x71", "--fps", "24",
    ])
    assert result.exit_code == 2
    assert "even width and height" in result.output
    # No raw pydantic noise.
    assert "input_value" not in result.output
    assert "errors.pydantic.dev" not in result.output
    assert "validation error for Config" not in result.output


# --- Fix 6: fail-fast for timecode / metronome start -----------------------


def test_start_timecode_validated_early(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError, match="start_timecode"):
        build_config(profile="ffv1", fps="24", resolution="128x72",
                     lead_seconds=2, start_timecode="not-a-tc",
                     output=tmp_path / "o.mkv")


def test_metronome_start_validated_early(tmp_path: Path) -> None:
    with pytest.raises(pydantic.ValidationError, match="metronome_start"):
        build_config(profile="ffv1", fps="24", resolution="128x72",
                     lead_seconds=2, bpm=120, metronome_start="banana",
                     output=tmp_path / "o.mkv")


def test_metronome_start_accepts_valid_forms(tmp_path: Path) -> None:
    for val in ("0", "t0", "1.5", "3"):
        cfg = build_config(profile="ffv1", fps="24", resolution="128x72",
                           lead_seconds=2, bpm=120, metronome_start=val,
                           output=tmp_path / "o.mkv")
        assert cfg.metronome_start == val
