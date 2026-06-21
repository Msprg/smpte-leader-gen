"""Unit tests for :mod:`smpte_leader_gen.layout` (CONTRACT.md §11).

Verify the four resolution presets: ``clock_radius / unit`` ~= 0.30, all sizes
positive, the big-number center equals ``(W/2, H/2)``, the counter sits top-left and
the timer top-right, the registration rectangle is centred with the frame's aspect
ratio, the bottom ruler is well-formed, and the layout is pure/deterministic.
"""
from __future__ import annotations

import dataclasses

import pytest

from smpte_leader_gen.layout import Layout, compute_layout

# (width, height) for 720p, 1080p, 2K DCI, 4K UHD.
PRESETS: list[tuple[int, int]] = [
    (1280, 720),
    (1920, 1080),
    (2048, 1080),
    (3840, 2160),
]


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_clock_radius_ratio(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    assert layout.clock_radius / layout.unit == pytest.approx(0.30)


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_unit_is_min_dimension(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    assert layout.unit == float(min(width, height))


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_center_is_frame_center(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    assert (layout.cx, layout.cy) == (width / 2.0, height / 2.0)
    # The big countdown number is centered at C.
    assert (layout.number_center_x, layout.number_center_y) == (
        width / 2.0,
        height / 2.0,
    )


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_all_sizes_positive(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    positive_fields = [
        layout.cx,
        layout.cy,
        layout.unit,
        layout.clock_radius,
        layout.clock_line_width,
        layout.crosshair_line_width,
        layout.number_font_px,
        layout.counter_font_px,
        layout.counter_baseline_y,
        layout.counter_center_x,
        layout.timer_font_px,
        layout.timer_baseline_y,
        layout.timer_center_x,
        layout.number_center_x,
        layout.number_center_y,
        layout.flash_radius,
        layout.flash_center_x,
        layout.flash_center_y,
        layout.twopop_flash_radius,
        layout.burnin_font_px,
        layout.burnin_baseline_y,
        layout.burnin_center_x,
        layout.wand_line_width,
        layout.geometry_line_width,
        layout.aspect_rect_left,
        layout.aspect_rect_top,
        layout.aspect_rect_width,
        layout.aspect_rect_height,
        layout.ruler_y,
        layout.ruler_left,
        layout.ruler_right,
        layout.ruler_line_width,
        layout.ruler_tick_minor_h,
        layout.ruler_tick_major_h,
        layout.ruler_label_font_px,
        layout.ruler_label_y,
        layout.ruler_marker_h,
        layout.ruler_marker_half_w,
    ]
    for value in positive_fields:
        assert value > 0


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_counter_top_left_timer_top_right(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Both readouts are in the top region now.
    assert layout.counter_baseline_y < height * 0.5
    assert layout.timer_baseline_y < height * 0.5
    assert layout.counter_baseline_y == pytest.approx(layout.timer_baseline_y)
    # Counter to the left of center, timer to the right.
    assert layout.counter_center_x < width / 2.0
    assert layout.timer_center_x > width / 2.0


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_documented_ratios(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    unit = float(min(width, height))
    assert layout.clock_line_width == max(2.0, 0.004 * unit)
    assert layout.wand_line_width == max(2.0, 0.006 * unit)
    assert layout.crosshair_line_width == max(1.0, 0.0015 * unit)
    assert layout.number_font_px == round(0.5 * layout.clock_radius)
    assert layout.counter_font_px == round(0.045 * unit)
    assert layout.timer_font_px == round(0.045 * unit)
    assert layout.flash_radius == pytest.approx(0.05 * unit)
    assert layout.twopop_flash_radius == pytest.approx(1.6 * layout.flash_radius)


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_flash_disc_fixed_right_middle(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # DECISION (CONTRACT.md §4): flash disc at fixed right-middle (0.85*W, 0.5*H).
    assert layout.flash_center_x == pytest.approx(0.85 * width)
    assert layout.flash_center_y == pytest.approx(0.5 * height)
    # twopop flash is larger than the regular flash.
    assert layout.twopop_flash_radius > layout.flash_radius


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_burnin_top_center(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Burn-in is centred in the top row (between the counter and timer).
    assert layout.burnin_center_x == pytest.approx(width / 2.0)
    assert layout.burnin_baseline_y == pytest.approx(0.085 * height)


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_aspect_rect_is_centered_with_frame_aspect(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Same aspect ratio as the frame.
    assert layout.aspect_rect_width / layout.aspect_rect_height == pytest.approx(
        width / height
    )
    # Centred: equal margins on each side.
    assert layout.aspect_rect_left == pytest.approx(width - layout.aspect_rect_left - layout.aspect_rect_width)
    assert layout.aspect_rect_top == pytest.approx(height - layout.aspect_rect_top - layout.aspect_rect_height)
    # Strictly inside the frame.
    assert 0 < layout.aspect_rect_left < width / 2.0
    assert layout.aspect_rect_width < width


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_ruler_geometry(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Ruler is near the bottom and spans a centred horizontal band.
    assert layout.ruler_y > height * 0.5
    assert 0 < layout.ruler_left < layout.ruler_right < width
    # Labels sit below the baseline; major ticks are taller than minor.
    assert layout.ruler_label_y > layout.ruler_y
    assert layout.ruler_tick_major_h > layout.ruler_tick_minor_h


def test_layout_is_frozen() -> None:
    layout = compute_layout(1920, 1080)
    assert dataclasses.is_dataclass(layout)
    with pytest.raises(dataclasses.FrozenInstanceError):
        layout.width = 0  # type: ignore[misc]


def test_layout_is_deterministic() -> None:
    a = compute_layout(1920, 1080)
    b = compute_layout(1920, 1080)
    assert a == b


@pytest.mark.parametrize(
    ("width", "height"),
    [(0, 1080), (1920, 0), (-1, 100), (100, -1), (0, 0)],
)
def test_non_positive_dimensions_raise(width: int, height: int) -> None:
    with pytest.raises(ValueError):
        compute_layout(width, height)


def test_portrait_uses_height_as_unit() -> None:
    # When height < width, unit must follow the smaller dimension.
    layout = compute_layout(1080, 1920)
    assert layout.unit == 1080.0
    assert isinstance(layout, Layout)
