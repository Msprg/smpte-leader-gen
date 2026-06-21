"""Golden-frame and structural tests for :mod:`smpte_leader_gen.render`.

These tests build a real :class:`~smpte_leader_gen.config.Config` and
:class:`~smpte_leader_gen.timing.Schedule` so each test is self-contained
(CONTRACT.md §11).

The golden hash pins the exact bytes of the "2"/2-pop frame at 1920x1080 @ 24 fps.
It was computed by actually running the renderer in this environment; it depends on
the installed cairo/Pillow versions and the bundled DejaVuSansMono fonts. If those
change, the hash will need to be recomputed.
"""
from __future__ import annotations

import hashlib

import pytest

from smpte_leader_gen.config import build_config, Config
from smpte_leader_gen.render import FrameRenderer
from smpte_leader_gen.timing import Schedule, build_schedule

# sha256 of the packed RGB24 bytes of the 2-pop ("2") frame, 1920x1080 @ 24 fps,
# lead=8, tail=2, preroll=0, count_style=full. Baked from this environment.
GOLDEN_2POP_SHA256 = (
    "863df22d065cfe1748308a774e52cc514ed3d52044f7f01311b068e26e8f3acf"
)


def _make_config() -> Config:
    """Build the canonical golden-test configuration (1080p / 24 fps)."""
    return build_config(
        fps="24",
        resolution="1080p",
        output="golden.mov",
        lead_seconds=8,
        tail_seconds=2.0,
        preroll_seconds=0.0,
        count_style="full",
    )


def _make_schedule(config: Config) -> Schedule:
    """Build the schedule matching a :class:`Config` (mirrors pipeline adapter)."""
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


@pytest.fixture()
def renderer() -> FrameRenderer:
    config = _make_config()
    schedule = _make_schedule(config)
    return FrameRenderer(config, schedule)


def _twopop_frame(schedule: Schedule) -> int:
    """Global index of the 2-pop ("2") frame: ``ffop_frame - 2*F``."""
    return schedule.ffop_frame - 2 * schedule.F


def test_twopop_frame_is_the_two(renderer: FrameRenderer) -> None:
    """Sanity: the golden frame really is number '2' and a 2-pop flash frame."""
    sched = renderer.schedule
    i = _twopop_frame(sched)
    assert sched.number_at(i) == 2
    assert i in sched.twopop_flash_frames
    assert i in sched.flash_frames


def test_golden_hash_2pop_frame(renderer: FrameRenderer) -> None:
    """The 2-pop frame bytes hash to the baked constant (deterministic output)."""
    sched = renderer.schedule
    i = _twopop_frame(sched)
    buf = renderer.render_frame(i)
    digest = hashlib.sha256(buf).hexdigest()
    assert digest == GOLDEN_2POP_SHA256


def test_frame_length_is_w_h_3(renderer: FrameRenderer) -> None:
    """Every rendered frame is exactly ``width * height * 3`` bytes."""
    cfg = renderer.config
    expected = cfg.width * cfg.height * 3
    assert len(renderer.render_frame(_twopop_frame(renderer.schedule))) == expected
    assert len(renderer.render_frame(0)) == expected


def test_render_is_deterministic(renderer: FrameRenderer) -> None:
    """Rendering the same frame twice yields byte-identical buffers."""
    i = _twopop_frame(renderer.schedule)
    assert renderer.render_frame(i) == renderer.render_frame(i)


def test_tail_frame_is_all_zero(renderer: FrameRenderer) -> None:
    """A tail frame is pure black (all-zero buffer of the right size)."""
    sched = renderer.schedule
    cfg = renderer.config
    tail_i = sched.total_frames - 1
    assert tail_i >= sched.ffop_frame  # really in the tail
    buf = renderer.render_frame(tail_i)
    assert len(buf) == cfg.width * cfg.height * 3
    assert buf == bytes(cfg.width * cfg.height * 3)
    assert set(buf) == {0}


def test_preroll_has_no_number() -> None:
    """Pre-roll frames show no countdown number (bg + crosshair + clock only)."""
    config = build_config(
        fps="24",
        resolution="720p",
        output="preroll.mov",
        lead_seconds=8,
        tail_seconds=1.0,
        preroll_seconds=1.0,
        count_style="full",
    )
    schedule = _make_schedule(config)
    renderer = FrameRenderer(config, schedule)
    assert schedule.preroll_frames > 0
    # number_at returns None during pre-roll, so no big number is drawn.
    assert schedule.number_at(0) is None
    buf = renderer.render_frame(0)
    assert len(buf) == config.width * config.height * 3
    # Not all black (background fill + crosshair + clock are present).
    assert set(buf) != {0}


def test_cairo_surface_to_rgb_shape(renderer: FrameRenderer) -> None:
    """The static layer round-trips to a contiguous HxWx3 uint8 RGB array."""
    static = renderer._static  # noqa: SLF001 (test inspects internals)
    cfg = renderer.config
    assert static.shape == (cfg.height, cfg.width, 3)
    assert static.dtype.name == "uint8"
    assert static.flags["C_CONTIGUOUS"]
