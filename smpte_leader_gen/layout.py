"""Pure geometry for the leader's on-screen elements.

This module computes the positions and sizes of every visual element (clock, sweep
wand, big countdown number, frame counter, digital timer, flash disc, timecode
burn-in, registration geometry, and the bottom audio ruler) purely from the output
resolution. It has NO dependency on cairo or PIL; it only produces a frozen
:class:`Layout` value object that the renderer consumes.

All sizes scale from ``unit = min(width, height)`` so the layout holds across the
supported resolution presets (720p, 1080p, 2K DCI, 4K UHD) and any custom size.
See CONTRACT.md §4 and SPEC.md §6 for the authoritative ratios and decisions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Layout:
    """Resolved geometry for a single output resolution.

    All coordinate values use the top-left origin screen convention (x right, y down)
    matching cairo/PIL. Distances are in device pixels (floats), font sizes are ints.
    The instance is immutable so it can be safely shared across render workers.
    """

    width: int
    height: int
    cx: float
    cy: float
    unit: float
    clock_radius: float
    clock_line_width: float
    crosshair_line_width: float
    number_font_px: int
    counter_font_px: int
    counter_baseline_y: float
    counter_center_x: float
    timer_font_px: int
    timer_baseline_y: float
    timer_center_x: float
    number_center_x: float
    number_center_y: float
    flash_radius: float
    flash_center_x: float
    flash_center_y: float
    twopop_flash_radius: float
    burnin_font_px: int
    burnin_baseline_y: float
    burnin_center_x: float
    wand_line_width: float
    # registration geometry (diagonals + centered aspect-ratio rectangle)
    geometry_line_width: float
    aspect_rect_left: float
    aspect_rect_top: float
    aspect_rect_width: float
    aspect_rect_height: float
    # bottom audio ruler (calibrated frame scale + moving playhead marker)
    ruler_y: float
    ruler_left: float
    ruler_right: float
    ruler_line_width: float
    ruler_tick_minor_h: float
    ruler_tick_major_h: float
    ruler_label_font_px: int
    ruler_label_y: float
    ruler_marker_h: float
    ruler_marker_half_w: float


def compute_layout(width: int, height: int) -> Layout:
    """Compute the :class:`Layout` for a given output resolution.

    All sizes scale from ``unit = min(width, height)``. Key ratios:

    * ``clock_radius = 0.30 * unit`` (three-quarters of the original 0.40 to leave
      room for the surrounding registration geometry and the bottom ruler).
    * ``clock_line_width = max(2.0, 0.004 * unit)``;
      ``wand_line_width = max(2.0, 0.006 * unit)``;
      ``crosshair_line_width = max(1.0, 0.0015 * unit)``.
    * ``number_font_px = round(0.5 * clock_radius)`` -- centred at ``C`` (under the wand).
    * Top readout row at ``0.085 * height``: the ``x/N`` frame counter is at the
      **top-left** (``0.06 * width``, left-anchored by the renderer), the digital
      timer at the **top-right** (``0.94 * width``, right-anchored), and the optional
      timecode burn-in centred at ``cx``. All use ``round(0.045 * unit)``.
    * ``flash_radius = 0.05 * unit`` at the FIXED right-middle ``(0.85 * width,
      0.5 * height)``; ``twopop_flash_radius = 1.6 * flash_radius``.
    * **Registration geometry**: full-frame diagonals (drawn by the renderer from the
      corners) plus a centred rectangle sharing the frame's aspect ratio, inset to
      ``0.92`` of the frame (so its corners land on the diagonals).
    * **Audio ruler** along the bottom (``ruler_y = 0.85 * height``, spanning
      ``0.10..0.90 * width``): a per-frame tick scale with taller major ticks every
      five frames and small frame-index labels, plus a moving playhead marker that
      sits on the current frame's tick -- a linear, frame-calibrated reference for
      spotting and measuring A/V-sync offset against the audible beep.

    Args:
        width: Output frame width in pixels (must be positive).
        height: Output frame height in pixels (must be positive).

    Returns:
        A frozen :class:`Layout` with every element placed deterministically.

    Raises:
        ValueError: If ``width`` or ``height`` is not a positive integer.
    """
    if width <= 0 or height <= 0:
        raise ValueError(
            f"width and height must be positive, got {width}x{height}"
        )

    cx = width / 2.0
    cy = height / 2.0
    unit = float(min(width, height))

    clock_radius = 0.30 * unit
    clock_line_width = max(2.0, 0.004 * unit)
    wand_line_width = max(2.0, 0.006 * unit)
    crosshair_line_width = max(1.0, 0.0015 * unit)

    number_font_px = round(0.5 * clock_radius)

    # Top readout row: counter (left), burn-in (center), timer (right).
    top_row_y = 0.085 * height
    readout_font_px = round(0.045 * unit)

    counter_font_px = readout_font_px
    counter_baseline_y = top_row_y
    counter_center_x = 0.06 * width

    timer_font_px = readout_font_px
    timer_baseline_y = top_row_y
    timer_center_x = 0.94 * width

    burnin_font_px = readout_font_px
    burnin_baseline_y = top_row_y
    burnin_center_x = cx

    number_center_x = cx
    number_center_y = cy

    flash_radius = 0.05 * unit
    flash_center_x = 0.85 * width
    flash_center_y = 0.5 * height
    twopop_flash_radius = 1.6 * flash_radius

    # Registration geometry: a concentric rectangle with the frame's aspect ratio.
    geometry_line_width = crosshair_line_width
    rect_scale = 0.92
    aspect_rect_width = rect_scale * width
    aspect_rect_height = rect_scale * height
    aspect_rect_left = (width - aspect_rect_width) / 2.0
    aspect_rect_top = (height - aspect_rect_height) / 2.0

    # Bottom audio ruler.
    ruler_y = 0.85 * height
    ruler_left = 0.10 * width
    ruler_right = 0.90 * width
    ruler_line_width = max(1.5, 0.002 * unit)
    ruler_tick_minor_h = 0.010 * height
    ruler_tick_major_h = 0.020 * height
    ruler_label_font_px = round(0.022 * unit)
    ruler_label_y = ruler_y + 0.045 * height
    ruler_marker_h = 0.022 * height
    ruler_marker_half_w = 0.010 * unit

    return Layout(
        width=width,
        height=height,
        cx=cx,
        cy=cy,
        unit=unit,
        clock_radius=clock_radius,
        clock_line_width=clock_line_width,
        crosshair_line_width=crosshair_line_width,
        number_font_px=number_font_px,
        counter_font_px=counter_font_px,
        counter_baseline_y=counter_baseline_y,
        counter_center_x=counter_center_x,
        timer_font_px=timer_font_px,
        timer_baseline_y=timer_baseline_y,
        timer_center_x=timer_center_x,
        number_center_x=number_center_x,
        number_center_y=number_center_y,
        flash_radius=flash_radius,
        flash_center_x=flash_center_x,
        flash_center_y=flash_center_y,
        twopop_flash_radius=twopop_flash_radius,
        burnin_font_px=burnin_font_px,
        burnin_baseline_y=burnin_baseline_y,
        burnin_center_x=burnin_center_x,
        wand_line_width=wand_line_width,
        geometry_line_width=geometry_line_width,
        aspect_rect_left=aspect_rect_left,
        aspect_rect_top=aspect_rect_top,
        aspect_rect_width=aspect_rect_width,
        aspect_rect_height=aspect_rect_height,
        ruler_y=ruler_y,
        ruler_left=ruler_left,
        ruler_right=ruler_right,
        ruler_line_width=ruler_line_width,
        ruler_tick_minor_h=ruler_tick_minor_h,
        ruler_tick_major_h=ruler_tick_major_h,
        ruler_label_font_px=ruler_label_font_px,
        ruler_label_y=ruler_label_y,
        ruler_marker_h=ruler_marker_h,
        ruler_marker_half_w=ruler_marker_half_w,
    )
