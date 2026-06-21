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

import numpy as np
import pytest

from smpte_leader_gen.config import build_config, Config
from smpte_leader_gen.render import (
    FONT_BOLD,
    FrameRenderer,
    _format_fps,
    _SYNCINFO_COLOR255,
)
from smpte_leader_gen.timing import Schedule, build_schedule

# sha256 of the packed RGB24 bytes of the 2-pop ("2") frame, 1920x1080 @ 24 fps,
# lead=8, tail=2, preroll=0, count_style=full. Baked from this environment.
GOLDEN_2POP_SHA256 = (
    "abafc2b4b138e74dcc7fe33445e80793af6e4649814c210ab45a32b9c2471df0"
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


@pytest.mark.parametrize(
    ("num", "den", "expected"),
    [
        (24, 1, "24"),
        (25, 1, "25"),
        (30, 1, "30"),
        (24000, 1001, "23.976"),
        (30000, 1001, "29.97"),
        (60000, 1001, "59.94"),
    ],
)
def test_format_fps_distinguishes_fractional(num: int, den: int, expected: str) -> None:
    """The counter denominator shows fractional rates distinctly."""
    assert _format_fps(num, den) == expected


def test_counter_uses_fractional_fps_denominator() -> None:
    """A 23.976 render shows the fractional fps as the counter denominator."""
    cfg = build_config(fps="23.976", resolution="720p", output="o.mov",
                       lead_seconds=8, tail_seconds=1.0)
    renderer = FrameRenderer(cfg, _make_schedule(cfg))
    assert renderer._fps_display == "23.976"  # noqa: SLF001


def test_ruler_marker_moves_within_leader_second(renderer: FrameRenderer) -> None:
    """The ruler marker (and wand) move frame to frame within a leader-second."""
    sched = renderer.schedule
    base = sched.countdown_start_frame  # frame 0 of the first leader-second
    # Two frames in the same leader-second (same big number) differ in bytes
    # because the playhead marker and wand have advanced.
    assert sched.number_at(base + 3) == sched.number_at(base + 8)
    assert renderer.render_frame(base + 3) != renderer.render_frame(base + 8)


def test_static_layer_has_ruler_near_bottom(renderer: FrameRenderer) -> None:
    """The cached static layer is non-background in the ruler band (ticks drawn)."""
    static = renderer._static  # noqa: SLF001
    lay = renderer.layout
    y0 = int(lay.ruler_y)
    y1 = int(lay.ruler_label_y) + 1
    band = static[y0:y1, :, :]
    # The ruler band is not uniformly the background color.
    assert band.std() > 0


def test_sync_info_absent_without_metronome(renderer: FrameRenderer) -> None:
    """The lower-left nudge readout is None unless the metronome (--bpm) is on."""
    assert renderer._sync_info_lines is None  # noqa: SLF001


def _sched_for(cfg: Config) -> Schedule:
    return build_schedule(
        fps=cfg.fps, sample_rate=cfg.sample_rate,
        frames_per_block_override=cfg.frames_per_block,
        lead_seconds=cfg.lead_seconds, tail_frames=cfg.tail_frames,
        preroll_frames=cfg.preroll_frames, beep_frames=cfg.beep_frames,
        persecond_beep=not cfg.no_persecond_beep, twopop=not cfg.no_twopop,
        count_style=cfg.count_style,
    )


def test_sync_info_present_with_metronome_only() -> None:
    """The readout shows whenever --bpm is set, even without --end-align, and it
    is drawn into the static layer's lower-left band (never the black tail)."""
    cfg = build_config(
        fps="23.976", resolution="720p", output="o.mov",
        lead_seconds=4, tail_seconds=2.0, bpm=128, time_sig="2/4",
        subdivision=2,
    )
    r = FrameRenderer(cfg, _sched_for(cfg))
    lines = r._sync_info_lines  # noqa: SLF001
    assert lines is not None and len(lines) == 3
    assert "METRONOME" in lines[0] and "128 BPM" in lines[0] and "2/4" in lines[0]
    assert lines[1].startswith("bar = ")  # always references the bar
    assert lines[2].startswith(("nudge leader", "seam aligned"))
    lay = r.layout
    y0 = int(lay.syncinfo_top_y - lay.syncinfo_line_h)
    y1 = int(lay.syncinfo_top_y + 3 * lay.syncinfo_line_h)
    band = r._static[y0:y1, : int(lay.cx), :]  # noqa: SLF001
    assert band.std() > 0


def test_sync_info_references_bar_not_end_align_unit() -> None:
    """Even with --end-align beat, the readout's period line is the bar."""
    cfg = build_config(
        fps="24", resolution="720p", output="o.mov", lead_seconds=4,
        tail_seconds=2.0, bpm=120, time_sig="4/4", end_align="beat",
    )
    r = FrameRenderer(cfg, _sched_for(cfg))
    lines = r._sync_info_lines  # noqa: SLF001
    assert lines is not None
    # 4/4 @ 120 bpm @ 24 fps: bar = 4 beats = 2.0 s = 48 frames = 96000 samples.
    assert "48.00 f" in lines[1]
    assert "96000 smp" in lines[1]


def _label_pixels_at(r: FrameRenderer, offset: int, *, row: str) -> int:
    """Count non-background pixels in the label box for ``offset`` on ``row``.

    ``row`` is ``"major"`` (the main label row) or ``"incr"`` (the +-1 row). The
    box is centred on the tick's x and the row's baseline.
    """
    lay = r.layout
    static = r._static.astype(np.int16)  # noqa: SLF001
    bg = np.array([round(c * 255) for c in r.config.bg_color], dtype=np.int16)
    if row == "incr":
        cy, fpx = lay.ruler_incr_label_y, lay.ruler_incr_label_font_px
    else:
        cy, fpx = lay.ruler_label_y, lay.ruler_label_font_px
    y0, y1 = int(cy - fpx * 0.7), int(cy + fpx * 0.7)
    x = r._ruler_tick_x(offset)  # noqa: SLF001
    x0, x1 = int(x - fpx * 1.2), int(x + fpx * 1.2)
    region = static[y0:y1, x0:x1]
    return int((np.abs(region - bg).sum(axis=2) > 40).sum())


def test_ruler_pm1_labels_on_dedicated_row(renderer: FrameRenderer) -> None:
    """The +-1 increment labels render on their own (lower) row, not the main row;
    the main row still carries the centre 0 and the +-5 majors (24 fps fixture)."""
    # +-1 labelled on the increment row, absent from the main label row.
    assert _label_pixels_at(renderer, 1, row="incr") > 0
    assert _label_pixels_at(renderer, -1, row="incr") > 0
    assert _label_pixels_at(renderer, 1, row="major") == 0
    assert _label_pixels_at(renderer, -1, row="major") == 0
    # Main row keeps the centre 0 and the major ticks; +-2 stays bare everywhere.
    assert _label_pixels_at(renderer, 0, row="major") > 0
    assert _label_pixels_at(renderer, 5, row="major") > 0
    assert _label_pixels_at(renderer, 2, row="incr") == 0
    assert _label_pixels_at(renderer, 2, row="major") == 0


def test_ruler_centre_zero_stays_legible_at_high_fps() -> None:
    """Moving the +-1 labels to their own row keeps the centre 0 uncrowded even at
    60 fps (dense per-frame ticks). The 0's main-row label box renders identically
    at 24 and 60 fps -- proof that no +-1 ink bleeds into it -- while the +-1
    increment labels still render on the dedicated lower row.

    (At 60 fps the +-1 ticks sit ~26 px from centre, so a box around them overlaps
    the 0 itself; comparing the 0-box across fps is the artifact-free check that the
    same-row crowding regression is gone.)
    """
    def make(fps: str) -> FrameRenderer:
        cfg = build_config(fps=fps, resolution="1080p", output="o.mov",
                           lead_seconds=4, tail_seconds=1.0, bpm=120)
        return FrameRenderer(cfg, _sched_for(cfg))

    r24, r60 = make("24"), make("60")
    zero24 = _label_pixels_at(r24, 0, row="major")
    zero60 = _label_pixels_at(r60, 0, row="major")
    assert zero60 > 0
    assert zero60 == zero24  # the 0 box is uncontaminated by the +-1 labels
    # The per-frame increment is still shown, on the dedicated lower row.
    assert _label_pixels_at(r60, 1, row="incr") > 0
    assert _label_pixels_at(r60, -1, row="incr") > 0


def test_sync_info_is_bold_turquoise() -> None:
    """The metronome readout is drawn with the bold font in turquoise, hugging the
    lower-left border and sitting above the ruler."""
    cfg = build_config(
        fps="24", resolution="1080p", output="o.mov", lead_seconds=4,
        tail_seconds=2.0, bpm=120, time_sig="4/4",
    )
    r = FrameRenderer(cfg, _sched_for(cfg))
    assert r._sync_info_lines is not None  # noqa: SLF001
    assert r._font_syncinfo.path == FONT_BOLD  # noqa: SLF001

    target = np.array(_SYNCINFO_COLOR255, dtype=np.int16)
    band = r._static.astype(np.int16)  # noqa: SLF001
    ys, xs = np.where(np.abs(band - target).sum(axis=2) < 60)
    assert len(ys) > 0  # turquoise text present
    # Hugs the left border at the raised position: ink starts near the new
    # syncinfo_left_x (0.025 w), tighter than the old 0.045 w, and the block sits
    # higher than the old 0.812 h bottom. Bounds chosen to FAIL if either the x or
    # y move were reverted, while staying clear of the ruler.
    assert xs.min() < cfg.width * 0.04          # new ~0.026 w passes; old 0.045 w fails
    assert ys.max() < cfg.height * 0.85         # new bottom ~0.84 h passes; old ~0.87 h fails
    assert ys.max() < r.layout.ruler_y          # never reaches the ruler
    assert abs(xs.min() - r.layout.syncinfo_left_x) < cfg.width * 0.01


def test_cairo_surface_to_rgb_shape(renderer: FrameRenderer) -> None:
    """The static layer round-trips to a contiguous HxWx3 uint8 RGB array."""
    static = renderer._static  # noqa: SLF001 (test inspects internals)
    cfg = renderer.config
    assert static.shape == (cfg.height, cfg.width, 3)
    assert static.dtype.name == "uint8"
    assert static.flags["C_CONTIGUOUS"]
