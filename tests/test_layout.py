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
    assert layout.clock_radius / layout.unit == pytest.approx(0.22)


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
        layout.geometry_line_alpha,
        layout.rect_line_width,
        layout.border_rect_left,
        layout.border_rect_top,
        layout.border_rect_width,
        layout.border_rect_height,
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
        layout.syncinfo_font_px,
        layout.syncinfo_left_x,
        layout.syncinfo_top_y,
        layout.syncinfo_line_h,
        layout.metro_center_x,
        layout.metro_pivot_y,
        layout.metro_arm_len,
        layout.metro_bob_radius,
        layout.metro_arm_width,
        layout.metro_pivot_radius,
        layout.metro_dot_row_y,
        layout.metro_dot_row_gap,
        layout.metro_dot_radius,
        layout.metro_dot_accent_radius,
        layout.metro_dot_sub_radius,
        layout.metro_dot_spacing,
        layout.metro_dot_sub_spacing,
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
    # DECISION (CONTRACT.md §4): flash disc at fixed right-middle (0.90*W, 0.5*H),
    # nudged right to clear the inner aspect rectangle.
    assert layout.flash_center_x == pytest.approx(0.90 * width)
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
def test_border_rect_spans_full_frame(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # The path is inset by HALF a line width so the stroke's outer edge lands
    # exactly on the frame edge (no padding), with the whole stroke on-frame.
    assert layout.border_rect_left == pytest.approx(layout.rect_line_width / 2.0)
    assert layout.border_rect_top == pytest.approx(layout.rect_line_width / 2.0)
    assert layout.border_rect_width == pytest.approx(width - layout.rect_line_width)
    assert layout.border_rect_height == pytest.approx(height - layout.rect_line_width)
    # The stroke reaches both edges: outer edge at 0 / width, inner edge one line
    # width in.
    assert layout.border_rect_left - layout.rect_line_width / 2.0 == pytest.approx(0.0)
    assert (
        layout.border_rect_left + layout.border_rect_width + layout.rect_line_width / 2.0
        == pytest.approx(width)
    )
    assert layout.border_rect_width > layout.aspect_rect_width
    assert layout.border_rect_height > layout.aspect_rect_height


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_geometry_lines_semi_transparent(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    assert 0.0 < layout.geometry_line_alpha < 1.0


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_ruler_geometry(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Ruler is near the bottom and spans a centred horizontal band.
    assert layout.ruler_y > height * 0.5
    assert 0 < layout.ruler_left < layout.ruler_right < width
    # Labels sit below the baseline; major ticks are taller than minor.
    assert layout.ruler_label_y > layout.ruler_y
    assert layout.ruler_tick_major_h > layout.ruler_tick_minor_h


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_metronome_widget_in_top_band(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Centred horizontally.
    assert layout.metro_center_x == pytest.approx(width / 2.0)
    # The bob (highest when vertical) stays below the top readout row.
    bob_top = layout.metro_pivot_y - layout.metro_arm_len - layout.metro_bob_radius
    assert bob_top > 0.11 * height
    # The pivot stays above the clock's top edge (no overlap with the clock).
    clock_top = layout.cy - layout.clock_radius
    assert layout.metro_pivot_y + layout.metro_pivot_radius < clock_top


@pytest.mark.parametrize(("width", "height"), PRESETS)
def test_metronome_dot_rows_at_readout_level(width: int, height: int) -> None:
    layout = compute_layout(width, height)
    # Centred horizontally; the beat row sits at the top readout level (same as the
    # frame counter / timer, which are in the corners) so it reads as a header.
    assert layout.metro_center_x == pytest.approx(width / 2.0)
    assert layout.metro_dot_row_y == pytest.approx(layout.counter_baseline_y)
    assert layout.metro_dot_row_y == pytest.approx(0.085 * height)

    # Size hierarchy: downbeat (accent) > beat > subdivision dot.
    assert layout.metro_dot_accent_radius > layout.metro_dot_radius
    assert layout.metro_dot_radius > layout.metro_dot_sub_radius
    # Adjacent dots never touch at the default centre-to-centre spacing.
    assert layout.metro_dot_spacing > 2.0 * layout.metro_dot_accent_radius
    assert layout.metro_dot_sub_spacing > 2.0 * layout.metro_dot_sub_radius

    # The beat row clears the top frame edge above; the subdivision row hangs one
    # gap below it and still stays well above the inner aspect rectangle (0.17 h).
    assert layout.metro_dot_row_y - layout.metro_dot_accent_radius > 0.02 * height
    subs_y = layout.metro_dot_row_y + layout.metro_dot_row_gap
    assert subs_y + layout.metro_dot_sub_radius < layout.aspect_rect_top


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
