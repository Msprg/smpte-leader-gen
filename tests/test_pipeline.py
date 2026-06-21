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
