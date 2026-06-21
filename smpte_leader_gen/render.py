"""Per-frame rendering of the SMPTE-style leader (cairo vectors + Pillow text).

This module turns a global frame index into a packed RGB24 byte buffer. Vector
elements (background fill, crosshair, clock circle, swept-sector wipe, sweep wand,
flash disc) are drawn with cairo (``FORMAT_RGB24``); ALL text (big countdown
number, ``x/N`` frame counter, digital timer, optional timecode burn-in) is drawn
with Pillow using the bundled ``DejaVuSansMono`` fonts so the output is
byte-deterministic across machines that share the same cairo/Pillow versions.

The static layer (background + crosshair + clock circle) is constant across the
whole render, so it is computed once in :meth:`FrameRenderer.__init__` and cached
as a NumPy array; each frame composites only the moving elements over a copy.

See CONTRACT.md §5 and SPEC.md §6 for the authoritative behavior.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import cairo
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .audio import _resolve_metronome_start
from .config import Config
from .layout import Layout, compute_layout
from .timing import Schedule

__all__ = ["FrameRenderer", "FONT_REGULAR", "FONT_BOLD"]


# ---------------------------------------------------------------------------
# Bundled font paths (resolved relative to this file for determinism)
# ---------------------------------------------------------------------------

_FONT_DIR = Path(__file__).parent / "fonts"

#: Absolute path to the bundled regular monospace font (counter/timer/burn-in).
FONT_REGULAR = str(_FONT_DIR / "DejaVuSansMono.ttf")
#: Absolute path to the bundled bold monospace font (big countdown number).
FONT_BOLD = str(_FONT_DIR / "DejaVuSansMono-Bold.ttf")

#: Fixed turquoise fill for the lower-left metronome / nudge readout. Drawn bold in
#: this colour so the glyphs stay legible where they overlap the (near-white,
#: #E0E0E0) registration rectangle and diagonals -- the bold weight reads across the
#: thin stroke while the body of the text sits on the dark background. Independent of
#: the configurable fg/accent colours.
_SYNCINFO_COLOR255: tuple[int, int, int] = (64, 224, 208)


@lru_cache(maxsize=None)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """Load (and cache) a TrueType font at a given pixel size.

    Caching keeps rendering fast and deterministic: the same ``(path, size)``
    always returns an identically configured font object.

    Args:
        path: Absolute path to the ``.ttf`` file.
        size: Pixel size to load the font at (``>= 1``).

    Returns:
        The loaded :class:`PIL.ImageFont.FreeTypeFont`.
    """
    return ImageFont.truetype(path, max(1, int(size)))


def _rgbf_to_rgb255(color: tuple[float, float, float]) -> tuple[int, int, int]:
    """Convert an RGBf triple in ``[0, 1]`` to an ``(r, g, b)`` 0-255 int tuple."""
    return tuple(int(round(c * 255.0)) for c in color)  # type: ignore[return-value]


def _blend(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    t: float,
) -> tuple[float, float, float]:
    """Linearly interpolate between RGBf colors ``a`` and ``b`` by ``t`` in ``[0, 1]``."""
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


class FrameRenderer:
    """Render individual leader frames to packed RGB24 bytes.

    The renderer is constructed once per output and then asked for frames by
    global index via :meth:`render_frame`. It is deterministic: identical
    ``(config, schedule, i)`` always yields identical bytes.

    Attributes:
        config: The resolved run configuration.
        schedule: The resolved timing/cue schedule.
        layout: The geometry computed from the output resolution.
    """

    def __init__(self, config: Config, schedule: Schedule) -> None:
        """Precompute layout, colors, fonts and the cached static background.

        The cached static background (``self._static``, an ``HxWx3`` uint8 array)
        contains the constant elements: the ``bg_color`` fill, the full-frame
        crosshair, and the clock circle. Frames composite their moving elements
        over a copy of this array.

        Args:
            config: The resolved run configuration.
            schedule: The resolved timing/cue schedule.
        """
        self.config = config
        self.schedule = schedule
        self.layout: Layout = compute_layout(config.width, config.height)

        self._width = int(config.width)
        self._height = int(config.height)

        # Colors as RGBf (cairo-native) and pre-converted 0-255 (Pillow text).
        self._bg = config.bg_color
        self._fg = config.fg_color
        self._accent = config.accent_color
        self._fg255 = _rgbf_to_rgb255(config.fg_color)
        self._accent255 = _rgbf_to_rgb255(config.accent_color)

        # Fixed wipe-sector shade: blend fg toward bg at 75% (a quarter of the
        # way from bg to fg) so the swept area reads as a dim fill, documented
        # here for determinism (CONTRACT.md §5 wipe guidance).
        self._wipe_shade = _blend(self._bg, self._fg, 0.25)

        # Pre-load the fonts at the sizes we need.
        self._font_number = _load_font(FONT_BOLD, self.layout.number_font_px)
        self._font_counter = _load_font(FONT_REGULAR, self.layout.counter_font_px)
        self._font_timer = _load_font(FONT_REGULAR, self.layout.timer_font_px)
        self._font_burnin = _load_font(FONT_REGULAR, self.layout.burnin_font_px)
        self._font_ruler = _load_font(FONT_REGULAR, self.layout.ruler_label_font_px)
        self._font_ruler_incr = _load_font(
            FONT_REGULAR, self.layout.ruler_incr_label_font_px
        )

        # Frame-counter denominator: the actual fps as a decimal, so rates that
        # differ only by a fraction (e.g. 23.976 vs 24) are visually distinct.
        self._fps_display = _format_fps(
            schedule.fps.numerator, schedule.fps.denominator
        )
        # Major-tick / label interval for the bottom ruler (every 5 frames).
        self._ruler_major = 5
        # Duration of one frame in milliseconds, for the ruler's ms scale.
        self._frame_ms = 1000.0 * schedule.fps.denominator / schedule.fps.numerator

        # Lower-left metronome / nudge readout (None unless --bpm is set).
        # Bold so it stays readable where it overlaps the registration rectangle.
        self._font_syncinfo = _load_font(FONT_BOLD, self.layout.syncinfo_font_px)
        self._sync_info_lines = self._compute_sync_info()

        # Build and cache the static RGB background.
        self._static: np.ndarray = self._build_static()

        # A reusable all-black tail buffer (immutable bytes; safe to share).
        self._tail_bytes: bytes = bytes(self._width * self._height * 3)

    # ------------------------------------------------------------------ #
    # Static background
    # ------------------------------------------------------------------ #

    def _build_static(self) -> np.ndarray:
        """Draw the constant layer once (background, registration geometry, clock,
        and the audio-ruler scale + labels).

        Constant elements only -- the moving wand/wipe/flash and the ruler marker
        are composited per frame over a copy of this array.

        Returns:
            A C-contiguous ``HxWx3`` uint8 RGB array.
        """
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, self._width, self._height)
        ctx = cairo.Context(surface)

        lay = self.layout
        w = float(self._width)
        h = float(self._height)

        # Background fill.
        ctx.set_source_rgb(*self._bg)
        ctx.paint()

        # Registration geometry: full-frame diagonals (corner to corner), drawn
        # semi-transparent so they read as a faint reference, not a distraction.
        ctx.set_source_rgba(*self._fg, lay.geometry_line_alpha)
        ctx.set_line_width(lay.geometry_line_width)
        ctx.move_to(0.0, 0.0)
        ctx.line_to(w, h)
        ctx.stroke()
        ctx.move_to(w, 0.0)
        ctx.line_to(0.0, h)
        ctx.stroke()

        # Full-frame border marking the exact video extent (opaque; same style as
        # the inner rectangle) so the video size is unambiguous on screen.
        ctx.set_source_rgb(*self._fg)
        ctx.set_line_width(lay.rect_line_width)
        ctx.rectangle(
            lay.border_rect_left,
            lay.border_rect_top,
            lay.border_rect_width,
            lay.border_rect_height,
        )
        ctx.stroke()

        # Smaller centred rectangle sharing the frame's aspect ratio (corners on
        # the diagonals); a quick visual aspect/centering reference.
        ctx.rectangle(
            lay.aspect_rect_left,
            lay.aspect_rect_top,
            lay.aspect_rect_width,
            lay.aspect_rect_height,
        )
        ctx.stroke()

        # Full-frame crosshair (vertical + horizontal through center), also
        # semi-transparent to match the diagonals.
        ctx.set_source_rgba(*self._fg, lay.geometry_line_alpha)
        ctx.set_line_width(lay.crosshair_line_width)
        ctx.move_to(lay.cx, 0.0)
        ctx.line_to(lay.cx, h)
        ctx.stroke()
        ctx.move_to(0.0, lay.cy)
        ctx.line_to(w, lay.cy)
        ctx.stroke()

        # Clock circle (static; the wand/wipe are drawn per-frame on top).
        if not self.config.no_clock:
            ctx.set_source_rgb(*self._fg)
            ctx.set_line_width(lay.clock_line_width)
            ctx.arc(lay.cx, lay.cy, lay.clock_radius, 0.0, 2.0 * math.pi)
            ctx.stroke()

        # Audio-ruler scale (baseline + per-frame ticks); labels added below.
        self._draw_ruler_scale(ctx)

        surface.flush()
        arr = self._cairo_surface_to_rgb(surface)

        # Ruler frame-index labels (Pillow text on the static array, once).
        self._draw_ruler_labels(arr)

        # Lower-left metronome / nudge readout (static; only when --bpm is set).
        if self._sync_info_lines:
            self._draw_sync_info(arr)
        return arr

    def _ruler_tick_range(self) -> range:
        """Integer frame offsets spanned by the centred ruler: ``-F//2 .. F-F//2``.

        The range is symmetric about zero (always including the ``0`` centre tick)
        and contains ``F + 1`` ticks, mapping the full leader-second across the bar.
        """
        F = self.schedule.F
        return range(-(F // 2), F - (F // 2) + 1)

    def _ruler_tick_x(self, offset: int) -> float:
        """X position of the tick at frame ``offset`` from the centred zero.

        ``offset == 0`` maps to the middle of the bar (the beep position);
        ``offset == -F/2`` to ``ruler_left`` and ``offset == +F/2`` to
        ``ruler_right``.
        """
        lay = self.layout
        span = lay.ruler_right - lay.ruler_left
        return lay.ruler_left + ((offset + self.schedule.F / 2.0) / self.schedule.F) * span

    def _ruler_offset_for_frame(self, i: int) -> int:
        """Signed frame offset of frame ``i`` from the nearest beep (centred zero).

        Frames within ``[0, F/2]`` of a beep read positive (right of centre);
        the rest read negative (left of centre), so the marker passes through the
        centre exactly on the beep/2-pop frame.
        """
        F = self.schedule.F
        j = (i - self.schedule.preroll_frames) % F
        return j if j <= F / 2.0 else j - F

    def _draw_ruler_scale(self, ctx: cairo.Context) -> None:
        """Draw the ruler baseline and per-frame ticks (cairo vectors).

        Ticks run symmetrically about the centred zero; every fifth is a taller
        major tick. The centre tick (offset 0, where the per-second beep fires) is
        drawn in the accent color as the sync reference.
        """
        lay = self.layout

        # Baseline.
        ctx.set_source_rgb(*self._fg)
        ctx.set_line_width(lay.ruler_line_width)
        ctx.move_to(lay.ruler_left, lay.ruler_y)
        ctx.line_to(lay.ruler_right, lay.ruler_y)
        ctx.stroke()

        # Ticks below the baseline.
        for offset in self._ruler_tick_range():
            x = self._ruler_tick_x(offset)
            major = (offset % self._ruler_major) == 0
            tick_h = lay.ruler_tick_major_h if major else lay.ruler_tick_minor_h
            if offset == 0:
                ctx.set_source_rgb(*self._accent)  # sync origin (beep position)
            else:
                ctx.set_source_rgb(*self._fg)
            ctx.set_line_width(lay.ruler_line_width)
            ctx.move_to(x, lay.ruler_y)
            ctx.line_to(x, lay.ruler_y + tick_h)
            ctx.stroke()

    def _draw_ruler_labels(self, arr: np.ndarray) -> None:
        """Draw the ruler's millisecond labels (Pillow), in two rows.

        The main row labels the centred zero and every major (5-frame) tick with its
        A/V offset in milliseconds; the centre reads ``0`` in the accent color. The
        two ticks flanking zero (offsets ``+-1``) are labelled on a dedicated,
        slightly smaller row just below, so the per-frame increment (ms per frame) is
        always readable without crowding the centre ``0`` at high frame rates (where
        the per-frame ticks pack too tightly to label in line).
        """
        lay = self.layout
        image = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(image)

        # Main row: the centred zero and every major (5-frame) tick.
        for offset in self._ruler_tick_range():
            if (offset % self._ruler_major) != 0:
                continue
            x = self._ruler_tick_x(offset)
            if offset == 0:
                text = "0"
                color = self._accent255
            else:
                text = f"{round(offset * self._frame_ms):+d}"
                color = self._fg255
            draw.text(
                (x, lay.ruler_label_y),
                text,
                font=self._font_ruler,
                fill=color,
                anchor="mm",
            )

        # Increment row: the +-1 ticks on their own lower row (if on the ruler).
        tick_range = self._ruler_tick_range()
        for offset in (-1, 1):
            if offset not in tick_range:
                continue
            x = self._ruler_tick_x(offset)
            text = f"{round(offset * self._frame_ms):+d}"
            draw.text(
                (x, lay.ruler_incr_label_y),
                text,
                font=self._font_ruler_incr,
                fill=self._fg255,
                anchor="mm",
            )
        np.copyto(arr, np.asarray(image))

    def _compute_sync_info(self) -> list[str] | None:
        """Build the lower-left metronome / bar-downbeat nudge readout, or ``None``.

        Returns ``None`` unless the metronome is active (``--bpm``). Otherwise it
        always references the **bar downbeat** (regardless of ``--end-align``) and
        reports:

          * the bar period in frames / ms / samples, and
          * the signed nudge to apply to the **leader** so that, with the music
            butted to the leader's end, the music's downbeat lands on the
            metronome's bar grid (i.e. the leader's bar grid, extended, continues
            into the music).

        The nudge is ``end_sample - nearest_bar_sample`` (``+`` = move the leader
        right, ``-`` = left). With ``--end-align bar`` it is sub-frame (the leftover
        the frame grid could not represent); otherwise it can be up to half a bar,
        which tells you how far the seam sits from a downbeat.
        """
        cfg = self.config
        sched = self.schedule
        if cfg.bpm is None:
            return None

        sr = sched.sample_rate
        bpm = cfg.bpm
        fpsf = float(sched.fps)
        spf = sr / fpsf  # samples per frame

        # Always reference the bar (a full measure of the time signature).
        bar_samples = sr * 60.0 * cfg.time_sig_beats / bpm
        if bar_samples <= 0:
            return None

        start = _resolve_metronome_start(sched, cfg)
        end = sched.total_samples
        # Exact (sample-accurate) onset of the bar downbeat nearest the end.
        m = round((end - start) / bar_samples)
        boundary = start + round(m * bar_samples)

        nudge = end - boundary  # +right / -left so the downbeat lands on the grid
        nudge_f = nudge / spf
        nudge_ms = nudge * 1000.0 / sr

        line1 = f"METRONOME {bpm:g} BPM  {cfg.time_sig}  {self._fps_display} fps"
        line2 = (
            f"bar = {bar_samples / spf:.2f} f / "
            f"{bar_samples * 1000.0 / sr:.1f} ms / {round(bar_samples)} smp"
        )
        if nudge == 0:
            line3 = "seam aligned on the bar downbeat (0 smp)"
        else:
            direction = "RIGHT" if nudge > 0 else "LEFT"
            line3 = (
                f"nudge leader {direction} {abs(nudge_f):.2f} f / "
                f"{abs(nudge_ms):.1f} ms / {abs(nudge)} smp"
            )
        return [line1, line2, line3]

    def _draw_sync_info(self, arr: np.ndarray) -> None:
        """Draw the lower-left end-alignment / nudge block (Pillow, left-aligned)."""
        lines = self._sync_info_lines
        if not lines:
            return
        lay = self.layout
        image = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(image)
        for idx, text in enumerate(lines):
            y = lay.syncinfo_top_y + idx * lay.syncinfo_line_h
            draw.text(
                (lay.syncinfo_left_x, y),
                text,
                font=self._font_syncinfo,
                fill=_SYNCINFO_COLOR255,
                anchor="lm",
            )
        np.copyto(arr, np.asarray(image))

    # ------------------------------------------------------------------ #
    # Per-frame rendering
    # ------------------------------------------------------------------ #

    def render_frame(self, i: int) -> bytes:
        """Render global frame ``i`` to packed RGB24 bytes.

        The returned buffer has length ``width * height * 3`` with bytes in RGB
        order (suitable for piping to FFmpeg ``-pix_fmt rgb24``).

        Behavior by region:
          * **Tail** (``i >= ffop_frame + ... ``, i.e. ``i >= ffop_frame`` up to
            ``total_frames``): pure black (all-zero buffer).
          * **Pre-roll** (``i < preroll_frames``): background + crosshair + clock
            only -- no number, wand, wipe, or flash. The counter and timer may
            still be drawn (subject to their toggles).
          * **Countdown** (``preroll_frames <= i < ffop_frame``): full rendering
            -- wipe sector, wand, big number, flash disc, counter, timer, burn-in.

        Args:
            i: Global frame index in ``[0, total_frames)``.

        Returns:
            The packed RGB24 byte buffer for the frame.
        """
        sched = self.schedule

        # Tail region: literal black frames (SPEC §2 / §6.8 default). Exception:
        # a multi-frame beep (beep_frames > F) can extend past t=0, so its flash
        # must stay visible while the beep is audible (SPEC §6.7). Those frames
        # show only the flash disc over black; all other tail frames are black.
        if i >= sched.ffop_frame:
            if self.config.no_flash or i not in sched.flash_frames:
                return self._tail_bytes
            arr = np.zeros_like(self._static)
            surface = self._rgb_to_cairo_surface(arr)
            ctx = cairo.Context(surface)
            self._draw_flash(ctx, i)
            surface.flush()
            return self._cairo_surface_to_rgb(surface).tobytes()

        in_countdown = sched.preroll_frames <= i < sched.ffop_frame

        # Start from a fresh copy of the cached static background.
        arr = self._static.copy()

        # Draw dynamic vector elements (wipe, wand, flash) onto a cairo surface
        # initialized from the static layer, then read back to ``arr``.
        if in_countdown:
            arr = self._draw_dynamic_vectors(arr, i)

        # Text overlays (Pillow) drawn directly on the RGB array.
        self._draw_text(arr, i, in_countdown)

        return arr.tobytes()

    def _draw_dynamic_vectors(self, arr: np.ndarray, i: int) -> np.ndarray:
        """Composite the wipe sector, sweep wand and flash disc over ``arr``.

        Args:
            arr: The current ``HxWx3`` uint8 RGB frame (static background copy).
            i: Global frame index (known to be in the countdown region).

        Returns:
            The updated ``HxWx3`` uint8 RGB array (new contiguous copy).
        """
        lay = self.layout
        sched = self.schedule

        # Build a cairo surface seeded with the current pixels so vector
        # anti-aliasing blends correctly against the existing background.
        surface = self._rgb_to_cairo_surface(arr)
        ctx = cairo.Context(surface)

        # Clock fraction p in [0, 1): clockwise from 12 o'clock.
        p = ((i - sched.preroll_frames) % sched.F) / sched.F
        theta = 2.0 * math.pi * p

        cx, cy, R = lay.cx, lay.cy, lay.clock_radius

        if not self.config.no_clock:
            # Swept-sector wipe: filled pie from 12 o'clock to the current angle.
            # In cairo's y-down screen coords, 12 o'clock is angle -pi/2 and the
            # sweep goes clockwise (increasing cairo angle). Start = -pi/2,
            # end = -pi/2 + theta.
            if p > 0.0:
                start_a = -math.pi / 2.0
                end_a = start_a + theta
                ctx.set_source_rgb(*self._wipe_shade)
                ctx.move_to(cx, cy)
                ctx.arc(cx, cy, R, start_a, end_a)
                ctx.close_path()
                ctx.fill()

            # Sweep wand: radial line from center to the circle edge.
            wx = cx + R * math.sin(theta)
            wy = cy - R * math.cos(theta)
            ctx.set_source_rgb(*self._fg)
            ctx.set_line_width(lay.wand_line_width)
            ctx.set_line_cap(cairo.LINE_CAP_ROUND)
            ctx.move_to(cx, cy)
            ctx.line_to(wx, wy)
            ctx.stroke()

        # Flash disc (drawn last so it sits on top of the clock geometry).
        if not self.config.no_flash:
            self._draw_flash(ctx, i)

        # Audio-ruler playhead: marker on the current frame's tick.
        self._draw_ruler_marker(ctx, i)

        surface.flush()
        return self._cairo_surface_to_rgb(surface)

    def _draw_ruler_marker(self, ctx: cairo.Context, i: int) -> None:
        """Draw the ruler playhead (a downward triangle) for frame ``i``.

        The marker sits on the tick for the current frame's signed offset from the
        nearest beep, so it crosses the centred zero exactly on the beep/2-pop
        frame, giving a calibrated cue for measuring any A/V-sync offset (in the
        labelled milliseconds) against the audible beep.
        """
        lay = self.layout
        offset = self._ruler_offset_for_frame(i)
        mx = self._ruler_tick_x(offset)
        top = lay.ruler_y - lay.ruler_marker_h
        hw = lay.ruler_marker_half_w

        ctx.set_source_rgb(*self._accent)
        ctx.move_to(mx, lay.ruler_y)          # apex on the baseline/tick
        ctx.line_to(mx - hw, top)
        ctx.line_to(mx + hw, top)
        ctx.close_path()
        ctx.fill()

    def _draw_flash(self, ctx: cairo.Context, i: int) -> None:
        """Draw the alignment flash disc for frame ``i``.

        When ``i`` is an active beep frame the disc is filled in the accent color
        (larger and brighter for the 2-pop). Otherwise a dim outline marks its
        location so the user knows where to look (SPEC §6.7).

        Args:
            ctx: The cairo context to draw on.
            i: Global frame index.
        """
        lay = self.layout
        sched = self.schedule
        fx, fy = lay.flash_center_x, lay.flash_center_y

        if i in sched.twopop_flash_frames:
            # 2-pop accent: larger disc in a distinct (brighter accent) color.
            ctx.set_source_rgb(*self._accent)
            ctx.arc(fx, fy, lay.twopop_flash_radius, 0.0, 2.0 * math.pi)
            ctx.fill()
        elif i in sched.flash_frames:
            # Regular per-second flash.
            ctx.set_source_rgb(*self._accent)
            ctx.arc(fx, fy, lay.flash_radius, 0.0, 2.0 * math.pi)
            ctx.fill()
        else:
            # Dim outline so the flash location is known when inactive.
            dim = _blend(self._bg, self._accent, 0.30)
            ctx.set_source_rgb(*dim)
            ctx.set_line_width(lay.clock_line_width)
            ctx.arc(fx, fy, lay.flash_radius, 0.0, 2.0 * math.pi)
            ctx.stroke()

    def _draw_text(self, arr: np.ndarray, i: int, in_countdown: bool) -> None:
        """Draw all text overlays onto ``arr`` in-place using Pillow.

        Args:
            arr: The ``HxWx3`` uint8 RGB frame to draw on (modified in place).
            i: Global frame index.
            in_countdown: Whether ``i`` is within the countdown region.
        """
        lay = self.layout
        sched = self.schedule
        cfg = self.config

        # Wrap the numpy buffer in a PIL image that shares memory.
        image = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(image)

        # Big countdown number (only in the countdown region when a number
        # applies). The contract asks for the wand to read "on top" of the
        # number; since ALL text is Pillow-only and drawn after the cairo vector
        # layer, the glyphs are painted over the wand line at the exact center.
        # This is the closest faithful interpretation given the Pillow-text
        # constraint and is fully deterministic (see deviations note).
        if in_countdown:
            number = sched.number_at(i)
            if number is not None:
                self._draw_centered_text(
                    draw,
                    str(number),
                    self._font_number,
                    lay.number_center_x,
                    lay.number_center_y,
                    self._fg255,
                    anchor="mm",
                )

        # Frame counter, top-left, zero-padded numerator over the fractional fps
        # (e.g. "07/23.976") so near-identical rates stay distinguishable.
        if not cfg.no_counter:
            j, max_index = sched.counter_at(i)
            width = len(str(max_index))
            text = f"{j:0{width}d}/{self._fps_display}"
            self._draw_centered_text(
                draw,
                text,
                self._font_counter,
                lay.counter_center_x,
                lay.counter_baseline_y,
                self._fg255,
                anchor="lm",  # left-anchored in the top-left corner
            )

        # Digital (ms) timer, top-right.
        if not cfg.no_timer:
            text = self._format_timer(i)
            self._draw_centered_text(
                draw,
                text,
                self._font_timer,
                lay.timer_center_x,
                lay.timer_baseline_y,
                self._fg255,
                anchor="rm",  # right-anchored in the top-right corner
            )

        # Optional timecode burn-in (top-left region).
        if cfg.show_timecode_burnin:
            text = self._format_burnin_tc(i)
            self._draw_centered_text(
                draw,
                text,
                self._font_burnin,
                lay.burnin_center_x,
                lay.burnin_baseline_y,
                self._fg255,
                anchor="mm",
            )

        # Copy the rendered pixels back into the original buffer (PIL may have
        # detached the array depending on version; this guarantees ``arr`` holds
        # the result for ``tobytes()``).
        np.copyto(arr, np.asarray(image))

    @staticmethod
    def _draw_centered_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont,
        cx: float,
        cy: float,
        color: tuple[int, int, int],
        *,
        anchor: str = "mm",
    ) -> None:
        """Draw ``text`` anchored at ``(cx, cy)`` with the given anchor mode."""
        draw.text((cx, cy), text, font=font, fill=color, anchor=anchor)

    # ------------------------------------------------------------------ #
    # Timer / timecode formatting
    # ------------------------------------------------------------------ #

    def _format_timer(self, i: int) -> str:
        """Format the digital countdown timer text for frame ``i``.

        Honors ``config.timer_format`` (``"auto"``/``"seconds"``/``"frames"``/
        ``"tc"``). Remaining frames come from
        :meth:`Schedule.remaining_frames_to_ffop`; negative values (tail) are
        clamped to zero.

        Args:
            i: Global frame index.

        Returns:
            The formatted timer string.
        """
        sched = self.schedule
        cfg = self.config
        remaining = max(0, sched.remaining_frames_to_ffop(i))

        fmt = cfg.timer_format
        num = sched.fps.numerator
        den = sched.fps.denominator

        if fmt == "frames":
            return str(remaining)

        if fmt == "tc":
            return _frames_to_tc(remaining, num, den)

        # seconds-based formats ("auto" and "seconds" share the same numeric
        # rendering; only the MM:SS vs S.fff switch differs by lead length).
        seconds = remaining * den / num
        use_mmss = sched.lead_seconds >= 60
        if use_mmss:
            return _format_mmss(seconds)
        return f"{seconds:.3f}"

    def _format_burnin_tc(self, i: int) -> str:
        """Format the on-screen timecode burn-in for frame ``i``.

        This is the ABSOLUTE position timecode from the start of the file
        (frame ``i``) at the output frame rate, non-drop-frame -- a simple,
        deterministic reference distinct from the embedded container timecode.

        Args:
            i: Global frame index.

        Returns:
            ``HH:MM:SS:FF`` timecode for absolute frame ``i``.
        """
        sched = self.schedule
        num = sched.fps.numerator
        den = sched.fps.denominator
        return _frames_to_tc(max(0, i), num, den)

    # ------------------------------------------------------------------ #
    # cairo <-> numpy conversion
    # ------------------------------------------------------------------ #

    @staticmethod
    def _cairo_surface_to_rgb(surface: cairo.ImageSurface) -> np.ndarray:
        """Convert a cairo RGB24/ARGB32 surface to a contiguous ``HxWx3`` RGB array.

        cairo stores pixels as premultiplied BGRA in little-endian (so the byte
        order in memory is B, G, R, A). We view the buffer as ``HxWx4`` and
        select the ``[2, 1, 0]`` channels to obtain RGB, returning a fresh
        C-contiguous copy.

        Args:
            surface: The cairo image surface to read.

        Returns:
            A C-contiguous ``HxWx3`` uint8 RGB array.
        """
        surface.flush()
        width = surface.get_width()
        height = surface.get_height()
        stride = surface.get_stride()

        buf = np.ndarray(
            shape=(height, stride // 4, 4),
            dtype=np.uint8,
            buffer=surface.get_data(),
        )
        # Trim any stride padding, then BGRA -> RGB.
        rgb = buf[:, :width, [2, 1, 0]]
        return np.ascontiguousarray(rgb)

    def _rgb_to_cairo_surface(self, arr: np.ndarray) -> cairo.ImageSurface:
        """Create a cairo ``FORMAT_RGB24`` surface seeded from an RGB array.

        Args:
            arr: A ``HxWx3`` uint8 RGB array.

        Returns:
            A new cairo surface whose pixels match ``arr`` (BGRX byte order).
        """
        height, width = arr.shape[:2]
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        stride = surface.get_stride()

        dst = np.ndarray(
            shape=(height, stride // 4, 4),
            dtype=np.uint8,
            buffer=surface.get_data(),
        )
        # RGB -> BGRX (cairo little-endian RGB24: B,G,R,unused).
        dst[:, :width, 0] = arr[:, :, 2]
        dst[:, :width, 1] = arr[:, :, 1]
        dst[:, :width, 2] = arr[:, :, 0]
        dst[:, :width, 3] = 255
        surface.mark_dirty()
        return surface


# ---------------------------------------------------------------------------
# Module-level helpers (self-contained; deterministic)
# ---------------------------------------------------------------------------


def _format_fps(num: int, den: int) -> str:
    """Format a rational fps as a compact decimal for the counter denominator.

    Integer rates render without a fractional part (``24/1`` -> ``"24"``); broadcast
    rates keep enough precision to stay distinct (``24000/1001`` -> ``"23.976"``,
    ``30000/1001`` -> ``"29.97"``, ``60000/1001`` -> ``"59.94"``). Trailing zeros
    are trimmed.

    Args:
        num: Frame-rate numerator.
        den: Frame-rate denominator.

    Returns:
        The fps as a trimmed decimal string.
    """
    if den == 1:
        return str(num)
    text = f"{num / den:.3f}".rstrip("0").rstrip(".")
    return text


def _frames_to_tc(frames: int, num: int, den: int) -> str:
    """Format a non-negative frame count as non-drop-frame ``HH:MM:SS:FF``.

    The frames-per-second used for the ``FF`` field is ``round(num/den)`` so the
    representation is clean for fractional rates (e.g. 24 at 23.976). This is a
    deterministic, self-contained helper for the on-screen timer/burn-in; the
    embedded container timecode (with proper drop-frame) is owned by encode.py.

    Args:
        frames: Absolute non-negative frame count.
        num: Frame-rate numerator.
        den: Frame-rate denominator.

    Returns:
        A ``HH:MM:SS:FF`` timecode string.
    """
    fps_round = (num + den // 2) // den  # round-half-up of num/den
    if fps_round < 1:
        fps_round = 1
    frames = max(0, frames)

    ff = frames % fps_round
    total_seconds = frames // fps_round
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = (total_seconds // 3600) % 100
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def _format_mmss(seconds: float) -> str:
    """Format a non-negative number of seconds as ``MM:SS.fff``."""
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    rem = seconds - minutes * 60
    return f"{minutes:02d}:{rem:06.3f}"
