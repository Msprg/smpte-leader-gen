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
    # registration geometry (diagonals + full-frame border + centered aspect rect)
    geometry_line_width: float
    geometry_line_alpha: float
    rect_line_width: float
    border_rect_left: float
    border_rect_top: float
    border_rect_width: float
    border_rect_height: float
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
    # bottom-left end-alignment / nudge info block (multi-line, left-aligned)
    syncinfo_font_px: int
    syncinfo_left_x: float
    syncinfo_top_y: float
    syncinfo_line_h: float
    # top-center metronome animation (inverted pendulum; only drawn with --bpm)
    metro_center_x: float
    metro_pivot_y: float
    metro_arm_len: float
    metro_bob_radius: float
    metro_arm_width: float
    metro_pivot_radius: float


def compute_layout(width: int, height: int) -> Layout:
    """Compute the :class:`Layout` for a given output resolution.

    All sizes scale from ``unit = min(width, height)``. Key ratios:

    * ``clock_radius = 0.22 * unit`` (a compact clock that leaves room for the
      surrounding registration geometry and the bottom ruler).
    * ``clock_line_width = max(2.0, 0.004 * unit)``;
      ``wand_line_width = max(2.0, 0.006 * unit)``;
      ``crosshair_line_width = max(1.0, 0.0015 * unit)``.
    * ``number_font_px = round(0.5 * clock_radius)`` -- centred at ``C`` (under the wand).
    * Top readout row at ``0.085 * height``: the ``x/N`` frame counter is at the
      **top-left** (``0.06 * width``, left-anchored by the renderer), the digital
      timer at the **top-right** (``0.94 * width``, right-anchored), and the optional
      timecode burn-in centred at ``cx``. All use ``round(0.045 * unit)``.
    * ``flash_radius = 0.05 * unit`` at the FIXED right-middle ``(0.90 * width,
      0.5 * height)`` -- clear of the inner aspect rectangle's right edge;
      ``twopop_flash_radius = 1.6 * flash_radius``.
    * **Registration geometry**: semi-transparent full-frame diagonals (drawn by the
      renderer from the corners) and centre crosshair, plus two opaque rectangles --
      a full-frame **border** marking the exact video extent and a smaller centred
      rectangle (``0.66`` of the frame) sharing the frame's aspect ratio (its corners
      land on the diagonals).
    * **Audio ruler** along the bottom (``ruler_y = 0.90 * height``, spanning
      ``0.10..0.90 * width``): a per-frame tick scale **centred on zero** (the beep
      position at the middle) with taller major ticks every five frames labelled in
      **milliseconds** of A/V offset (with the two ticks flanking zero, ``+-1``,
      also labelled inline at the same size so the per-frame increment is legible),
      plus a moving playhead marker that crosses the centre exactly on the beep: a
      linear, calibrated reference for spotting and measuring A/V-sync offset
      against the audible beep.
    * **Metronome animation** (only with ``--bpm``): an inverted-pendulum widget
      centred at the top (``metro_pivot_y = 0.25 * height``, arm ``0.108 * height``)
      that swings one beat per half-swing in the band between the top readout and
      the clock; the renderer animates and flashes it in lock-step with the clicks.

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

    clock_radius = 0.22 * unit
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
    flash_center_x = 0.90 * width
    flash_center_y = 0.5 * height
    twopop_flash_radius = 1.6 * flash_radius

    # Registration geometry. The diagonals (and the centre crosshair) are drawn
    # semi-transparent so they read as a faint background reference rather than a
    # distraction; the rectangles stay fully opaque because they mark real sizes.
    geometry_line_width = crosshair_line_width
    geometry_line_alpha = 0.5

    # The two registration rectangles are opaque and a touch heavier than the
    # hairline diagonals so the video extent is clearly visible.
    rect_line_width = max(2.0, 0.003 * unit)

    # Full-frame border rectangle marking the exact video extent. Inset by one
    # line width so the whole stroke stays visible (a rect at the literal edge
    # would have half its width clipped off-frame).
    border_inset = rect_line_width
    border_rect_left = border_inset
    border_rect_top = border_inset
    border_rect_width = width - 2.0 * border_inset
    border_rect_height = height - 2.0 * border_inset

    # Smaller concentric rectangle sharing the frame's aspect ratio (its corners
    # land on the diagonals); a quick visual aspect/centering reference.
    rect_scale = 0.66
    aspect_rect_width = rect_scale * width
    aspect_rect_height = rect_scale * height
    aspect_rect_left = (width - aspect_rect_width) / 2.0
    aspect_rect_top = (height - aspect_rect_height) / 2.0

    # Bottom audio ruler (kept below the inner aspect rectangle's bottom edge).
    ruler_y = 0.90 * height
    ruler_left = 0.10 * width
    ruler_right = 0.90 * width
    ruler_line_width = max(1.5, 0.002 * unit)
    ruler_tick_minor_h = 0.010 * height
    ruler_tick_major_h = 0.020 * height
    ruler_label_font_px = round(0.022 * unit)
    ruler_label_y = ruler_y + 0.045 * height
    ruler_marker_h = 0.022 * height
    ruler_marker_half_w = 0.010 * unit

    # End-alignment / nudge info: a small multi-line block hugging the left frame
    # border, with its first line just below the horizontal centre line (clear of
    # the clock/number to its right, which start well past where the text ends).
    syncinfo_font_px = round(0.020 * unit)
    syncinfo_left_x = 0.0125 * width
    syncinfo_top_y = 0.520 * height
    syncinfo_line_h = 0.024 * height

    # Top-center metronome animation: an inverted pendulum (pivot low, bob high,
    # like a mechanical Maelzel metronome) swinging in the free band between the
    # top readout row (~0.085 h) and the clock's top edge (~0.28 h). Only drawn
    # when the metronome is active (--bpm). The bob is highest (at ``metro_pivot_y
    # - metro_arm_len`` ~= 0.142 h) when vertical, staying clear of the readout.
    metro_center_x = cx
    metro_pivot_y = 0.250 * height
    metro_arm_len = 0.108 * height
    metro_bob_radius = 0.020 * unit
    metro_arm_width = max(2.0, 0.006 * unit)
    metro_pivot_radius = 0.012 * unit

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
        geometry_line_alpha=geometry_line_alpha,
        rect_line_width=rect_line_width,
        border_rect_left=border_rect_left,
        border_rect_top=border_rect_top,
        border_rect_width=border_rect_width,
        border_rect_height=border_rect_height,
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
        syncinfo_font_px=syncinfo_font_px,
        syncinfo_left_x=syncinfo_left_x,
        syncinfo_top_y=syncinfo_top_y,
        syncinfo_line_h=syncinfo_line_h,
        metro_center_x=metro_center_x,
        metro_pivot_y=metro_pivot_y,
        metro_arm_len=metro_arm_len,
        metro_bob_radius=metro_bob_radius,
        metro_arm_width=metro_arm_width,
        metro_pivot_radius=metro_pivot_radius,
    )
