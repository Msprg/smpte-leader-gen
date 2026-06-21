"""Rational frame timing and cue scheduling for the SMPTE leader generator.

This module is PURE: no FFmpeg, no cairo, no I/O, no global mutable state. Every
output-affecting computation is deterministic. All sample-position math uses
cumulative integer rounding so that audio/video boundaries never accumulate drift
over the length of the file (see :func:`frame_start_sample`).

See CONTRACT.md §1 and SPEC.md §3, §4 for the authoritative semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

__all__ = [
    "FPS_PRESETS",
    "parse_fps",
    "round_half_up",
    "frames_per_block",
    "frame_start_sample",
    "total_samples_for",
    "end_align_tail_frames",
    "Cue",
    "Schedule",
    "build_schedule",
]


# ---------------------------------------------------------------------------
# Frame-rate presets and parsing
# ---------------------------------------------------------------------------

#: Canonical broadcast frame rates as exact rationals, keyed by their string form.
FPS_PRESETS: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "24": Fraction(24),
    "25": Fraction(25),
    "29.97": Fraction(30000, 1001),
    "30": Fraction(30),
    "50": Fraction(50),
    "59.94": Fraction(60000, 1001),
    "60": Fraction(60),
}

#: Decimal strings that MUST map to a broadcast rational rather than their exact
#: decimal value (e.g. ``Fraction("23.976") == 2997/125`` would be wrong here).
_BROADCAST_DECIMALS: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "23.98": Fraction(24000, 1001),
    "29.97": Fraction(30000, 1001),
    "59.94": Fraction(60000, 1001),
}


def parse_fps(value: str | int | float | Fraction) -> Fraction:
    """Parse a frame-rate specification into an exact :class:`~fractions.Fraction`.

    Accepted forms:
      * A preset key (see :data:`FPS_PRESETS`), matched on its string form.
      * A bare integer, e.g. ``"24"`` or ``24``.
      * A ``"num/den"`` rational string, e.g. ``"24000/1001"``.
      * A broadcast decimal string -- ``"23.976"``/``"23.98"`` -> ``24000/1001``,
        ``"29.97"`` -> ``30000/1001``, ``"59.94"`` -> ``60000/1001``.
      * Any other decimal string -> the exact decimal as a ``Fraction``
        (e.g. ``"23.976"`` would be ``2997/125``, but that case is overridden above).
      * A :class:`~fractions.Fraction` or ``float`` directly.

    Args:
        value: The frame-rate specification to parse.

    Returns:
        The frame rate as an exact rational.

    Raises:
        ValueError: If the value is unparseable or not strictly positive.
    """
    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, bool):
        # bool is a subclass of int; reject explicitly to avoid surprises.
        raise ValueError(f"invalid fps value: {value!r}")
    elif isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, float):
        # Map known broadcast floats; otherwise use the exact-string form so the
        # caller's literal (e.g. 23.976) does not become a giant binary rational.
        text = repr(value).rstrip("0").rstrip(".") if "." in repr(value) else repr(value)
        result = _parse_fps_str(text)
    elif isinstance(value, str):
        result = _parse_fps_str(value.strip())
    else:
        raise ValueError(f"invalid fps value: {value!r}")

    if result <= 0:
        raise ValueError(f"fps must be positive, got {value!r}")
    return result


def _parse_fps_str(text: str) -> Fraction:
    """Parse the string form of an fps spec into a Fraction (no positivity check)."""
    if not text:
        raise ValueError("fps string is empty")

    # 1. Exact preset key.
    if text in FPS_PRESETS:
        return FPS_PRESETS[text]

    # 2. Broadcast-decimal mapping (must precede generic decimal handling).
    if text in _BROADCAST_DECIMALS:
        return _BROADCAST_DECIMALS[text]

    # 3. "num/den" rational form.
    if "/" in text:
        num_str, _, den_str = text.partition("/")
        try:
            num = int(num_str.strip())
            den = int(den_str.strip())
        except ValueError as exc:
            raise ValueError(f"unparseable fps rational: {text!r}") from exc
        if den == 0:
            raise ValueError(f"fps denominator is zero: {text!r}")
        return Fraction(num, den)

    # 4. Integer-like form.
    try:
        return Fraction(int(text))
    except ValueError:
        pass

    # 5. Generic decimal form -> exact Fraction of the decimal string.
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"unparseable fps value: {text!r}") from exc


# ---------------------------------------------------------------------------
# Rounding and frame/sample math
# ---------------------------------------------------------------------------


def round_half_up(x: float) -> int:
    """Round ``x`` to the nearest integer, with ties going away from zero is NOT
    used here; ties go toward positive infinity ("round half up").

    This avoids banker's-rounding surprises in on-screen counters and block
    sizing where the conventional schoolbook rule is expected.

    Args:
        x: The value to round.

    Returns:
        The rounded integer.
    """
    import math

    return math.floor(x + 0.5)


def frames_per_block(fps: Fraction, override: int | None = None) -> int:
    """Return the number of frames in one leader-second block (``F``).

    ``F`` is ``round_half_up(float(fps))`` unless ``override`` is supplied.

    Args:
        fps: The exact frame rate.
        override: An explicit block size, if the caller wants a non-default ``F``.

    Returns:
        The block size, guaranteed ``>= 1``.

    Raises:
        ValueError: If the resolved block size is less than 1.
    """
    f = override if override is not None else round_half_up(float(fps))
    if f < 1:
        raise ValueError(f"frames_per_block must be >= 1, got {f}")
    return f


def frame_start_sample(i: int, sample_rate: int, fps: Fraction) -> int:
    """Return the first audio sample index of global frame ``i``.

    Computed as ``round(i * sample_rate * den / num)`` using exact integer math
    (round-half-up): ``(i * sample_rate * den + num // 2) // num``. Because each
    boundary is computed from the absolute frame index rather than by accumulating
    a per-frame sample count, the result is drift-free over the whole file.

    Args:
        i: Global frame index (``>= 0``; ``total_frames`` is also valid and yields
            the total sample count).
        sample_rate: Audio sample rate in Hz.
        fps: The exact frame rate.

    Returns:
        The integer sample index at which frame ``i`` begins.
    """
    num = fps.numerator
    den = fps.denominator
    return (i * sample_rate * den + num // 2) // num


def total_samples_for(total_frames: int, sample_rate: int, fps: Fraction) -> int:
    """Return the total audio length in samples for ``total_frames`` frames.

    Equal to ``frame_start_sample(total_frames, sample_rate, fps)``; the audio
    duration matches the video duration to within one sample.

    Args:
        total_frames: Number of frames in the whole output.
        sample_rate: Audio sample rate in Hz.
        fps: The exact frame rate.

    Returns:
        The total number of audio samples.
    """
    return frame_start_sample(total_frames, sample_rate, fps)


def end_align_tail_frames(
    *,
    fps: Fraction,
    F: int,
    lead_seconds: int,
    preroll_frames: int,
    tail_frames: int,
    bpm: float,
    beats_per_bar: int,
    subdivision: int,
    metronome_start_frame: int,
    unit: str,
) -> int:
    """Return a (possibly larger) tail length so the file ends on a metronome grid point.

    The metronome lays a tempo grid anchored at ``metronome_start_frame`` (the same
    anchor its clicks use). This returns the smallest tail length ``>= tail_frames``
    such that the **total** frame count lands on the next grid boundary at or after
    the nominal end -- so when the leader's black tail is butt-joined to the music,
    the music's downbeat continues that grid seamlessly.

    Alignment is computed in the frame domain (the granularity an editor cuts on),
    so it is frame-accurate: exact when the chosen unit spans a whole number of
    frames (e.g. integer fps with a compatible tempo), and within half a frame
    otherwise.

    Args:
        fps: The exact frame rate.
        F: Frames per leader-second block.
        lead_seconds: Number of leader-seconds (``L``).
        preroll_frames: Pre-roll length in frames.
        tail_frames: The nominal black-tail length in frames (the floor; never
            shortened).
        bpm: Metronome tempo in beats per minute (``> 0``).
        beats_per_bar: Beats per bar (the time-signature numerator), for ``"bar"``.
        subdivision: Metronome clicks per beat, for ``"subdivision"``.
        metronome_start_frame: Frame the metronome grid is anchored at.
        unit: Grid granularity to align to -- ``"beat"``, ``"bar"`` or
            ``"subdivision"``.

    Returns:
        The aligned tail length in frames (``>= tail_frames``).

    Raises:
        ValueError: If ``bpm <= 0`` or ``unit`` is not a recognized granularity.
    """
    import math

    if bpm <= 0:
        raise ValueError(f"bpm must be positive for end alignment, got {bpm}")

    frames_per_beat = 60.0 * float(fps) / bpm
    if unit == "beat":
        unit_frames = frames_per_beat
    elif unit == "bar":
        unit_frames = frames_per_beat * beats_per_bar
    elif unit == "subdivision":
        unit_frames = frames_per_beat / subdivision
    else:
        raise ValueError(
            f"end-align unit must be 'beat', 'bar' or 'subdivision', got {unit!r}"
        )

    if unit_frames <= 0:
        return tail_frames

    countdown = lead_seconds * F
    nominal_total = preroll_frames + countdown + tail_frames

    def boundary(m: int) -> int:
        # m-th grid frame from the anchor (round-half-up, matching the click grid).
        return metronome_start_frame + round_half_up(m * unit_frames)

    # Smallest m >= 0 whose boundary frame is at or after the nominal end.
    if nominal_total <= metronome_start_frame:
        m = 0
    else:
        m = max(0, math.ceil((nominal_total - metronome_start_frame) / unit_frames))
    while boundary(m) < nominal_total:
        m += 1

    target_total = boundary(m)
    aligned_tail = target_total - preroll_frames - countdown
    return max(tail_frames, aligned_tail)


# ---------------------------------------------------------------------------
# Cue and schedule data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cue:
    """A single audio/visual cue (a beep) at a frame boundary.

    Attributes:
        frame: Global frame index where the cue's onset is.
        sample: ``frame_start_sample(frame)`` -- the audio onset sample.
        kind: Cue kind; currently always ``"beep"``.
        is_twopop: ``True`` only for the canonical 2-pop beep.
    """

    frame: int
    sample: int
    kind: str
    is_twopop: bool


@dataclass(frozen=True)
class Schedule:
    """The fully resolved timing plan for one leader render.

    All frames are 0-indexed over ``[0, total_frames)``. The schedule is pure
    data (JSON-free, picklable) so it can be passed to render workers.

    Attributes:
        fps: The exact frame rate.
        sample_rate: Audio sample rate in Hz.
        F: Frames per leader-second block.
        lead_seconds: Number of leader-seconds counted down (``L``).
        tail_frames: Black-tail length in frames.
        preroll_frames: Pre-roll length in frames (before the countdown).
        beep_frames: Beep length in frames (``>= 1``).
        count_style: ``"full"`` or ``"smpte"``.
        total_countdown_frames: ``L * F``.
        total_frames: ``preroll_frames + L*F + tail_frames``.
        total_samples: ``total_samples_for(total_frames, sr, fps)``.
        countdown_start_frame: First countdown frame (== ``preroll_frames``).
        ffop_frame: First Frame Of Picture (``preroll_frames + L*F``).
        beep_cues: All scheduled beeps, ascending by frame.
        flash_frames: Union of ``[cue.frame, cue.frame+beep_frames)`` for every cue.
        twopop_flash_frames: The flash-frame subset belonging to the 2-pop.
    """

    fps: Fraction
    sample_rate: int
    F: int
    lead_seconds: int
    tail_frames: int
    preroll_frames: int
    beep_frames: int
    count_style: str
    # derived:
    total_countdown_frames: int
    total_frames: int
    total_samples: int
    countdown_start_frame: int
    ffop_frame: int
    beep_cues: tuple[Cue, ...]
    flash_frames: frozenset[int]
    twopop_flash_frames: frozenset[int]

    def number_at(self, i: int) -> int | None:
        """Return the big countdown number for global frame ``i``, or ``None``.

        ``None`` is returned for pre-roll and tail frames (no number), and for
        countdown frames that show no number under the active count style.

        For ``count_style == "full"`` the number is ``L - s`` where
        ``s = (i - preroll) // F``, ranging ``L .. 1``.

        For ``count_style == "smpte"`` the classic fixed ``8..2`` sequence is
        mapped onto the last seven leader-seconds before FFOP: the number is the
        number of remaining blocks ``remaining = L - s`` when ``2 <= remaining <= 8``,
        otherwise ``None`` (the ``remaining == 1`` block and any leader-seconds
        before the ``8`` show no number).

        Args:
            i: Global frame index.

        Returns:
            The countdown number to display, or ``None`` if no number applies.
        """
        # Outside the countdown region: no number.
        if i < self.preroll_frames or i >= self.ffop_frame:
            return None

        s = (i - self.preroll_frames) // self.F
        remaining = self.lead_seconds - s  # L .. 1 across the countdown

        if self.count_style == "smpte":
            if 2 <= remaining <= 8:
                return remaining
            return None

        # "full": every countdown block shows its number L .. 1.
        return remaining

    def counter_at(self, i: int) -> tuple[int, int]:
        """Return the per-second frame counter ``(j, F-1)`` for global frame ``i``.

        For the countdown region (and tail), ``j = (i - preroll_frames) % F``.
        For pre-roll frames, the counter runs over its own local block:
        ``j = i % F``. The denominator is always ``F - 1``.

        Args:
            i: Global frame index.

        Returns:
            A ``(numerator, denominator)`` pair where the denominator is ``F - 1``.
        """
        if i >= self.preroll_frames:
            j = (i - self.preroll_frames) % self.F
        else:
            j = i % self.F
        return (j, self.F - 1)

    def leader_second_at(self, i: int) -> int:
        """Return the leader-second index of global frame ``i``.

        ``(i - preroll_frames) // F``: negative during pre-roll, ``0 .. L-1``
        during the countdown, and ``>= L`` during the tail.

        Args:
            i: Global frame index.

        Returns:
            The (possibly negative) leader-second index.
        """
        import math

        return math.floor((i - self.preroll_frames) / self.F)

    def remaining_frames_to_ffop(self, i: int) -> int:
        """Return the number of frames from ``i`` to the First Frame Of Picture.

        ``ffop_frame - i``; negative once ``i`` is in the tail.

        Args:
            i: Global frame index.

        Returns:
            Frames remaining until FFOP (can be negative).
        """
        return self.ffop_frame - i


# ---------------------------------------------------------------------------
# Schedule construction
# ---------------------------------------------------------------------------


def build_schedule(
    *,
    fps: Fraction,
    sample_rate: int,
    frames_per_block_override: int | None,
    lead_seconds: int,
    tail_frames: int,
    preroll_frames: int,
    beep_frames: int,
    persecond_beep: bool,
    twopop: bool,
    count_style: str,
) -> Schedule:
    """Build the full :class:`Schedule` for a leader render.

    Beep scheduling (SPEC §7.1):
      * Per-second beep onsets at the first frame of each leader-second within the
        countdown region -- ``preroll + s*F`` for ``s in 0 .. L-1`` -- but only
        when ``persecond_beep`` is ``True``.
      * The 2-pop beep at ``ffop_frame - 2*F`` (the "2" second), when ``twopop`` is
        ``True``. If a per-second cue already exists at that frame it is marked
        ``is_twopop=True`` rather than duplicated; if ``persecond_beep`` is ``False``
        the 2-pop is the only cue.
      * Each ``cue.sample`` is ``frame_start_sample(cue.frame, sr, fps)``.
      * ``flash_frames`` covers ``[frame, frame+beep_frames)`` for every cue, clipped
        to ``[0, total_frames)``. ``twopop_flash_frames`` covers only the 2-pop range.

    Args:
        fps: The exact frame rate.
        sample_rate: Audio sample rate in Hz.
        frames_per_block_override: Optional explicit ``F``.
        lead_seconds: Number of leader-seconds (``L``).
        tail_frames: Black-tail length in frames.
        preroll_frames: Pre-roll length in frames.
        beep_frames: Beep length in frames (``>= 1``).
        persecond_beep: When ``False``, only the 2-pop beep is scheduled.
        twopop: When ``False``, no 2-pop accent is added.
        count_style: ``"full"`` or ``"smpte"``.

    Returns:
        The resolved :class:`Schedule`.

    Raises:
        ValueError: If ``count_style == "smpte"`` and ``lead_seconds < 8``.
    """
    if count_style == "smpte" and lead_seconds < 8:
        raise ValueError(
            "count_style 'smpte' requires lead_seconds >= 8, "
            f"got {lead_seconds}"
        )

    F = frames_per_block(fps, frames_per_block_override)

    total_countdown_frames = lead_seconds * F
    total_frames = preroll_frames + total_countdown_frames + tail_frames
    total_samples = total_samples_for(total_frames, sample_rate, fps)
    countdown_start_frame = preroll_frames
    ffop_frame = preroll_frames + total_countdown_frames

    # Frame index of the 2-pop ("2" second), regardless of whether it is enabled.
    twopop_frame = ffop_frame - 2 * F

    # Collect cue onset frames keyed by frame so the 2-pop can merge with an
    # existing per-second cue rather than duplicate it.
    cue_frames: dict[int, bool] = {}  # frame -> is_twopop

    if persecond_beep:
        for s in range(lead_seconds):
            cue_frames[preroll_frames + s * F] = False

    # The 2-pop coincides with the big number "2" (2 leader-seconds before t=0),
    # which only exists for lead_seconds >= 2. For shorter leaders the computed
    # frame would fall in the pre-roll or before frame 0, so skip it rather than
    # schedule a meaningless/out-of-range marker. (config.py fails fast for the
    # user-facing case; this is a defensive guard for direct callers.)
    if twopop and twopop_frame >= countdown_start_frame:
        # Mark/insert the 2-pop. (When persecond_beep is True the frame exists.)
        cue_frames[twopop_frame] = True

    beep_cues = tuple(
        Cue(
            frame=frame,
            sample=frame_start_sample(frame, sample_rate, fps),
            kind="beep",
            is_twopop=is_twopop,
        )
        for frame, is_twopop in sorted(cue_frames.items())
    )

    def _flash_range(frame: int) -> range:
        start = max(0, frame)
        end = min(total_frames, frame + beep_frames)
        return range(start, end)

    flash_set: set[int] = set()
    twopop_flash_set: set[int] = set()
    for cue in beep_cues:
        rng = _flash_range(cue.frame)
        flash_set.update(rng)
        if cue.is_twopop:
            twopop_flash_set.update(rng)

    return Schedule(
        fps=fps,
        sample_rate=sample_rate,
        F=F,
        lead_seconds=lead_seconds,
        tail_frames=tail_frames,
        preroll_frames=preroll_frames,
        beep_frames=beep_frames,
        count_style=count_style,
        total_countdown_frames=total_countdown_frames,
        total_frames=total_frames,
        total_samples=total_samples,
        countdown_start_frame=countdown_start_frame,
        ffop_frame=ffop_frame,
        beep_cues=beep_cues,
        flash_frames=frozenset(flash_set),
        twopop_flash_frames=frozenset(twopop_flash_set),
    )
