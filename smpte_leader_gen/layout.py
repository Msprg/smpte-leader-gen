"""Pure geometry for the leader's on-screen elements.

This module computes the positions and sizes of every visual element (clock, sweep
wand, big countdown number, frame counter, digital timer, flash disc, timecode
burn-in) purely from the output resolution. It has NO dependency on cairo or PIL;
it only produces a frozen :class:`Layout` value object that the renderer consumes.

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


def compute_layout(width: int, height: int) -> Layout:
    """Compute the :class:`Layout` for a given output resolution.

    All sizes scale from ``unit = min(width, height)`` using the ratios documented in
    CONTRACT.md §4:

    * ``clock_radius = 0.40 * unit``
    * ``clock_line_width = max(2.0, 0.004 * unit)``
    * ``wand_line_width = max(2.0, 0.006 * unit)``
    * ``crosshair_line_width = max(1.0, 0.0015 * unit)``
    * ``number_font_px = round(0.5 * clock_radius)``
    * ``counter_font_px = timer_font_px = round(0.05 * unit)``; counter baseline at
      ``0.08 * height`` (top-center), timer baseline at ``0.93 * height``
      (bottom-center), both centred horizontally at ``cx``.
    * the big number is centred at ``C = (width / 2, height / 2)`` (drawn under the wand).
    * ``flash_radius = 0.05 * unit``; the flash disc sits at the FIXED right-middle
      position ``(0.85 * width, 0.5 * height)`` so it reads clearly separate from the
      clock. ``twopop_flash_radius = 1.6 * flash_radius``.
    * timecode burn-in near the top-left at ``(0.16 * width, 0.08 * height)`` using
      the same font size as the counter.

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

    clock_radius = 0.40 * unit
    clock_line_width = max(2.0, 0.004 * unit)
    wand_line_width = max(2.0, 0.006 * unit)
    crosshair_line_width = max(1.0, 0.0015 * unit)

    number_font_px = round(0.5 * clock_radius)

    counter_font_px = round(0.05 * unit)
    counter_baseline_y = 0.08 * height
    counter_center_x = cx

    timer_font_px = round(0.05 * unit)
    timer_baseline_y = 0.93 * height
    timer_center_x = cx

    number_center_x = cx
    number_center_y = cy

    flash_radius = 0.05 * unit
    flash_center_x = 0.85 * width
    flash_center_y = 0.5 * height
    twopop_flash_radius = 1.6 * flash_radius

    burnin_font_px = round(0.05 * unit)
    burnin_baseline_y = 0.08 * height
    burnin_center_x = 0.16 * width

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
    )
