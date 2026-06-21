"""Tests for :mod:`smpte_leader_gen.pipeline`.

Covers the pure config->schedule adapter and the multiprocessing worker plumbing
without FFmpeg, plus a tiny real end-to-end render that is skipped when FFmpeg is
absent. The end-to-end test produces a small FFV1/MKV file and verifies the frame
rate and frame count via ffprobe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction

import pytest

from smpte_leader_gen.config import build_config
from smpte_leader_gen.encode import build_ffprobe_streams_cmd, discover_ffmpeg
from smpte_leader_gen.pipeline import (
    _init_worker,
    _render_one,
    build_schedule_from_config,
    run,
)


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _tiny_config(output, **overrides):
    base = dict(
        resolution="64x64",
        fps="24",
        lead_seconds=2,
        tail_seconds=1.0,
        profile="ffv1",
        output=str(output),
    )
    base.update(overrides)
    return build_config(**base)


def test_build_schedule_from_config_maps_flags(tmp_path):
    cfg = _tiny_config(tmp_path / "out.mkv")
    sched = build_schedule_from_config(cfg)

    assert sched.fps == Fraction(24)
    assert sched.F == 24
    assert sched.lead_seconds == 2
    assert sched.tail_frames == 24  # 1.0 s * 24
    assert sched.total_frames == 2 * 24 + 24  # lead*F + tail
    # Per-second beeps (2) merge with the 2-pop at the '2' second.
    assert len(sched.beep_cues) == 2
    assert any(c.is_twopop for c in sched.beep_cues)


def test_end_align_extends_tail_to_beat(tmp_path):
    # 24 fps, 120 bpm => 12 frames/beat. lead=2 (48), tail=1.5s=36 => 84.
    # Next beat boundary >= 84 is 84 itself (84/12=7), so no change here; bump
    # tail to 1.6s (38) => total 86, next beat boundary is 96 (beat 8).
    cfg = _tiny_config(
        tmp_path / "out.mkv", lead_seconds=2, tail_seconds=1.6,
        bpm=120.0, end_align="beat",
    )
    sched = build_schedule_from_config(cfg)
    assert sched.total_frames == 96
    assert sched.total_frames % 12 == 0  # lands on a beat
    # Audio length lands on the beat sample too (48000*60/120 = 24000 / beat).
    assert sched.total_samples % 24000 == 0


def test_end_align_bar_uses_time_sig(tmp_path):
    # bar = 4 beats = 48 frames. total nominal 48+38=86 -> next bar boundary 96.
    cfg = _tiny_config(
        tmp_path / "out.mkv", lead_seconds=2, tail_seconds=1.6,
        bpm=120.0, time_sig="4/4", end_align="bar",
    )
    sched = build_schedule_from_config(cfg)
    assert sched.total_frames == 96
    assert sched.total_frames % 48 == 0


def test_no_end_align_leaves_tail_untouched(tmp_path):
    cfg = _tiny_config(
        tmp_path / "out.mkv", lead_seconds=2, tail_seconds=1.6, bpm=120.0,
    )
    sched = build_schedule_from_config(cfg)
    assert sched.tail_frames == 38  # round(1.6 * 24), unchanged


def test_end_cue_extends_past_boundary_and_sounds_final_beat(tmp_path):
    from smpte_leader_gen.audio import metronome_click_samples

    # Without the cue: total lands exactly on the boundary (96 frames); the beat
    # click there is suppressed (onset sample == total_samples).
    base = dict(
        lead_seconds=2, tail_seconds=1.6, bpm=120.0, end_align="beat",
    )
    aligned = build_schedule_from_config(
        _tiny_config(tmp_path / "a.mkv", **base)
    )
    boundary_sample = aligned.total_samples
    clicks_aligned = metronome_click_samples(aligned, _tiny_config(tmp_path / "a.mkv", **base))
    assert all(s < boundary_sample for s, _ in clicks_aligned)  # boundary excluded

    # With the cue: the file extends a little past the boundary so the boundary
    # beat now falls inside [0, total_samples) and is sounded.
    cued_cfg = _tiny_config(tmp_path / "b.mkv", end_cue=True, **base)
    cued = build_schedule_from_config(cued_cfg)
    assert cued.total_frames > aligned.total_frames
    clicks_cued = metronome_click_samples(cued, cued_cfg)
    assert any(s == boundary_sample for s, _ in clicks_cued)  # boundary now sounds
    # ...and it is the final click, near the very end.
    last_sample = max(s for s, _ in clicks_cued)
    assert last_sample == boundary_sample
    assert last_sample < cued.total_samples


def test_no_flags_invert_correctly(tmp_path):
    cfg = _tiny_config(
        tmp_path / "out.mkv", no_persecond_beep=True, no_twopop=False
    )
    sched = build_schedule_from_config(cfg)
    # Only the 2-pop remains.
    assert len(sched.beep_cues) == 1
    assert sched.beep_cues[0].is_twopop is True


def test_worker_renders_frame(tmp_path):
    cfg = _tiny_config(tmp_path / "out.mkv")
    sched = build_schedule_from_config(cfg)
    _init_worker(cfg, sched)
    data = _render_one(0)
    assert isinstance(data, bytes)
    assert len(data) == cfg.width * cfg.height * 3


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not available")
def test_run_produces_readable_file(tmp_path):
    out = tmp_path / "leader.mkv"
    cfg = _tiny_config(out)
    sched = build_schedule_from_config(cfg)

    result = run(cfg, progress=False)
    assert result == out
    assert out.exists() and out.stat().st_size > 0

    _, ffprobe, _ = discover_ffmpeg()
    proc = subprocess.run(
        build_ffprobe_streams_cmd(ffprobe, out),
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(proc.stdout)
    streams = {s["codec_type"]: s for s in info["streams"]}
    assert "video" in streams and "audio" in streams
    assert streams["video"]["r_frame_rate"] == "24/1"
    assert int(streams["audio"]["channels"]) == 2
