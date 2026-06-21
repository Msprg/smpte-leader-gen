"""Audio synthesis and WAV output for the SMPTE leader generator.

This module is PURE and deterministic (no FFmpeg, no randomness, no wall-clock
time in output-affecting paths). It synthesizes the sync beep / 2-pop and the
optional metronome into a single contiguous stereo PCM buffer, applies a peak
safety limiter, and writes WAV files via :mod:`soundfile`.

All sample-position math defers to :mod:`smpte_leader_gen.timing` so that audio
onsets land on the same drift-free boundaries the video uses. Cue onsets are
pinned to exact frame boundaries (``cue.sample``); metronome clicks use the
cumulative-rounding rule per click so subdivisions and beats never drift.

See CONTRACT.md §6 and SPEC.md §7 for the authoritative semantics.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import soundfile as sf

from .config import Config
from .timing import Schedule, frame_start_sample

__all__ = [
    "FADE_MS",
    "dbfs_to_amp",
    "render_tone",
    "render_click",
    "synth_beep_track",
    "metronome_click_samples",
    "synth_metronome",
    "build_audio",
    "write_wav",
]


#: Raised-cosine fade length (milliseconds) applied at the head and tail of each
#: beep and click. ~1 ms is short enough to keep the perceived onset sharp (and
#: to leave the very first sample at the exact onset boundary) while removing the
#: spectral splatter of a hard discontinuity.
FADE_MS: float = 1.0

#: Peak-safety ceiling in linear amplitude (== -1 dBFS). The final mix is scaled
#: down to this peak if it would otherwise exceed it.
_PEAK_CEILING_DBFS: float = -1.0

#: Master gain applied to the final mix (2.0 == +6.02 dB). It is applied only to
#: the extent the available headroom allows: the effective gain is
#: ``min(_MASTER_GAIN, ceiling / peak)`` so the output never exceeds the ceiling.
_MASTER_GAIN: float = 2.0


def dbfs_to_amp(dbfs: float) -> float:
    """Convert a level in dBFS to a linear amplitude (full-scale peak = 1.0).

    Args:
        dbfs: Level in decibels relative to full scale (``0`` dBFS == ``1.0``).

    Returns:
        The linear amplitude ``10 ** (dbfs / 20.0)``.
    """
    return 10.0 ** (dbfs / 20.0)


def render_tone(
    freq: float,
    n_samples: int,
    sample_rate: int,
    *,
    fade_ms: float = FADE_MS,
    amp: float = 1.0,
) -> np.ndarray:
    """Render a mono sine tone with short raised-cosine fades at head and tail.

    The sine starts at phase 0 with its first sample at the exact onset boundary
    (index 0 corresponds to ``t = 0``); the fade is an amplitude envelope only and
    does not shift the onset. The fade length is clamped so that the head and tail
    fades never overlap (``2 * fade <= n_samples``).

    Args:
        freq: Tone frequency in Hz.
        n_samples: Length of the tone in samples (``<= 0`` yields an empty array).
        sample_rate: Audio sample rate in Hz.
        fade_ms: Raised-cosine fade length, in milliseconds, at each end.
        amp: Peak linear amplitude of the tone.

    Returns:
        A float64 array of shape ``(n_samples,)``.
    """
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float64)

    t = np.arange(n_samples, dtype=np.float64) / float(sample_rate)
    tone = amp * np.sin(2.0 * np.pi * freq * t)

    _apply_fade(tone, fade_ms, sample_rate)
    return tone


def render_click(
    freq: float,
    sample_rate: int,
    *,
    amp: float = 1.0,
    dur_ms: float = 6.0,
) -> np.ndarray:
    """Render a short percussive metronome click (windowed high sine).

    The click is a sine at ``freq`` multiplied by a full Hann window over its whole
    duration, giving a soft attack and decay with no sustain. This timbre is
    deliberately distinct from the sustained sync beep so the metronome is audibly
    separable. The first sample sits at the exact onset boundary.

    Args:
        freq: Click frequency in Hz.
        sample_rate: Audio sample rate in Hz.
        amp: Peak linear amplitude of the click.
        dur_ms: Click duration in milliseconds.

    Returns:
        A float64 array of the click waveform (mono).
    """
    n_samples = int(round(dur_ms * 1e-3 * sample_rate))
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float64)

    t = np.arange(n_samples, dtype=np.float64) / float(sample_rate)
    sine = np.sin(2.0 * np.pi * freq * t)
    # Full Hann window across the entire click for a percussive, decaying blip.
    window = _hann_window(n_samples)
    return amp * sine * window


def synth_beep_track(schedule: Schedule, config: Config) -> np.ndarray:
    """Synthesize the mono sync-beep / 2-pop track for the whole output.

    For every cue in ``schedule.beep_cues`` a sine tone is added at ``cue.sample``.
    The tone is ``round(beep_frames * sr * den / num)`` samples long (computed via
    drift-free frame math), at ``beep_freq`` with peak amplitude from
    ``beep_level_dbfs``. Tones that would run past the buffer end are clipped to
    fit. Overlapping tones are summed.

    Args:
        schedule: The resolved timing plan (provides cue onsets and length).
        config: The run configuration (beep frequency / length / level).

    Returns:
        A float64 mono array of length ``schedule.total_samples``.
    """
    buffer = np.zeros(schedule.total_samples, dtype=np.float64)

    # Beep length in samples, derived via the same cumulative-rounding rule used
    # for frame boundaries: round(beep_frames * sr * den / num).
    beep_len = frame_start_sample(config.beep_frames, schedule.sample_rate, schedule.fps)
    if beep_len <= 0:
        return buffer

    amp = dbfs_to_amp(config.beep_level_dbfs)
    tone = render_tone(
        config.beep_freq,
        beep_len,
        schedule.sample_rate,
        amp=amp,
    )

    for cue in schedule.beep_cues:
        _add_at(buffer, tone, cue.sample)

    return buffer


def metronome_click_samples(schedule: Schedule, config: Config) -> list[tuple[int, str]]:
    """Compute every metronome click onset and its role, drift-free.

    Click ``k`` lands at::

        click_sample(k) = start_sample + round(k * sr * 60 / (bpm * subdivision))

    (computed with round-half-up so subdivisions and beats never accumulate drift),
    for ``k = 0, 1, ...`` while ``click_sample < total_samples``. The role is:

      * ``"sub"`` when ``k % subdivision != 0`` (a subdivision click between beats);
      * ``"accent"`` when ``k`` is a beat (``k % subdivision == 0``) and the beat
        index is the first beat of a bar (``beat_index % time_sig_beats == 0``);
      * ``"beat"`` otherwise.

    The start sample is resolved from ``config.metronome_start``: ``"0"`` -> ``0``;
    ``"t0"`` -> the sample of ``schedule.ffop_frame``; a numeric seconds string ->
    ``round(seconds * sr)``.

    Args:
        schedule: The resolved timing plan (provides total length and FFOP).
        config: The run configuration (bpm / subdivision / time signature / start).

    Returns:
        A list of ``(sample_index, role)`` pairs, ascending by sample. Empty when
        ``config.bpm`` is ``None``.
    """
    if config.bpm is None:
        return []

    sr = schedule.sample_rate
    bpm = config.bpm
    subdivision = config.subdivision
    beats_per_bar = config.time_sig_beats
    start_sample = _resolve_metronome_start(schedule, config)

    total = schedule.total_samples
    clicks: list[tuple[int, str]] = []

    # samples per click = sr * 60 / (bpm * subdivision); accumulate via the
    # absolute index k (cumulative rounding), never a running float sum.
    denom = bpm * subdivision
    k = 0
    while True:
        offset = int(round(k * sr * 60.0 / denom))
        sample = start_sample + offset
        if sample >= total:
            break
        if sample >= 0:
            if k % subdivision != 0:
                role = "sub"
            else:
                beat_index = k // subdivision
                role = "accent" if beat_index % beats_per_bar == 0 else "beat"
            clicks.append((sample, role))
        k += 1

    return clicks


def synth_metronome(schedule: Schedule, config: Config) -> np.ndarray:
    """Synthesize the mono metronome track for the whole output.

    Each ``(sample, role)`` from :func:`metronome_click_samples` adds a
    :func:`render_click` at the role's frequency
    (``accent`` -> ``metronome_accent_freq``, ``beat`` -> ``metronome_click_freq``,
    ``sub`` -> ``metronome_sub_freq``), with peak amplitude from
    ``metronome_level_dbfs``. Clicks are summed; tails past the buffer end are
    clipped to fit.

    Args:
        schedule: The resolved timing plan.
        config: The run configuration (metronome frequencies / level).

    Returns:
        A float64 mono array of length ``schedule.total_samples`` (all zeros when
        ``config.bpm`` is ``None``).
    """
    buffer = np.zeros(schedule.total_samples, dtype=np.float64)

    clicks = metronome_click_samples(schedule, config)
    if not clicks:
        return buffer

    amp = dbfs_to_amp(config.metronome_level_dbfs)
    sr = schedule.sample_rate
    freq_by_role = {
        "accent": config.metronome_accent_freq,
        "beat": config.metronome_click_freq,
        "sub": config.metronome_sub_freq,
    }

    # Cache one click waveform per role (deterministic, identical per role).
    click_by_role = {
        role: render_click(freq, sr, amp=amp) for role, freq in freq_by_role.items()
    }

    for sample, role in clicks:
        _add_at(buffer, click_by_role[role], sample)

    return buffer


def build_audio(schedule: Schedule, config: Config) -> tuple[np.ndarray, float]:
    """Build the final stereo mix and report the applied peak level.

    The beep track and the metronome track are summed into a mono signal, then
    a **master gain** is applied: the mix is boosted by up to ``_MASTER_GAIN``
    (2.0 == +6 dB), but only as far as the available headroom permits -- the
    effective gain is ``min(_MASTER_GAIN, ceiling / peak)`` so the output peak
    never exceeds ``-1 dBFS``. If the *raw* mix already exceeds the ceiling the
    effective gain is below unity (the mix is scaled down to exactly ``-1 dBFS``)
    and a :class:`UserWarning` is emitted via :func:`warnings.warn` (do not clip
    silently). The mono signal is then duplicated into both channels (centered).
    The returned buffer is exactly ``schedule.total_samples`` long (the component
    tracks are already that length; this is enforced defensively).

    Args:
        schedule: The resolved timing plan (provides the exact total length).
        config: The run configuration.

    Returns:
        A ``(stereo, applied_peak_dbfs)`` pair where ``stereo`` is a float64 array
        of shape ``(total_samples, 2)`` and ``applied_peak_dbfs`` is the peak level
        (in dBFS) of the returned mix (``<= -1 dBFS``; ``-inf`` for pure silence).
    """
    total = schedule.total_samples

    beep = synth_beep_track(schedule, config)
    metro = synth_metronome(schedule, config)
    mono = beep + metro

    # Enforce exact length (zero-pad or truncate). Component tracks are already
    # total-length, so this is a defensive guard for any future divergence.
    mono = _fit_length(mono, total)

    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    ceiling = dbfs_to_amp(_PEAK_CEILING_DBFS)

    if peak > 0.0:
        # Boost by up to _MASTER_GAIN, but never past the ceiling. When the raw
        # mix already exceeds the ceiling this gain drops below unity (attenuation).
        headroom_gain = ceiling / peak
        gain = min(_MASTER_GAIN, headroom_gain)
        mono = mono * gain
        if headroom_gain < 1.0:
            # Raw mix was hotter than the ceiling: we attenuated to prevent
            # clipping -- surface that (do not clip silently).
            warnings.warn(
                f"audio mix peak {_amp_to_dbfs(peak):.2f} dBFS exceeded "
                f"{_PEAK_CEILING_DBFS:.1f} dBFS; scaled down by "
                f"{20.0 * np.log10(gain):.2f} dB to prevent clipping.",
                UserWarning,
                stacklevel=2,
            )
        applied_peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    else:
        applied_peak = peak

    stereo = np.column_stack((mono, mono))
    return stereo, _amp_to_dbfs(applied_peak)


def write_wav(
    path: str | Path,
    buffer: np.ndarray,
    sample_rate: int,
    bit_depth: int,
) -> None:
    """Write a float buffer to a WAV file with the requested PCM bit depth.

    Args:
        path: Output path (``str`` or :class:`~pathlib.Path`).
        buffer: Float audio in ``[-1, 1]``; shape ``(n,)`` (mono) or ``(n, ch)``.
        sample_rate: Audio sample rate in Hz.
        bit_depth: ``24`` -> ``PCM_24``; ``16`` -> ``PCM_16``.

    Raises:
        ValueError: If ``bit_depth`` is not 16 or 24.
    """
    if bit_depth == 24:
        subtype = "PCM_24"
    elif bit_depth == 16:
        subtype = "PCM_16"
    else:
        raise ValueError(f"bit_depth must be 16 or 24, got {bit_depth}")

    sf.write(str(path), buffer, sample_rate, subtype=subtype)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hann_window(n: int) -> np.ndarray:
    """Return an ``n``-point Hann window as float64 (0 at both ends)."""
    if n <= 1:
        return np.ones(max(n, 0), dtype=np.float64)
    k = np.arange(n, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * k / (n - 1))


def _apply_fade(signal: np.ndarray, fade_ms: float, sample_rate: int) -> None:
    """Apply an in-place raised-cosine fade-in/out to ``signal``.

    The fade length is clamped so the two fades never overlap. Index 0 stays at
    the onset boundary; the fade is purely an amplitude envelope. A half-Hann
    ramp (rising for the head, falling for the tail) is used.
    """
    n = signal.shape[0]
    if n == 0:
        return

    fade_len = int(round(fade_ms * 1e-3 * sample_rate))
    # Never let head + tail fades overlap.
    fade_len = min(fade_len, n // 2)
    if fade_len <= 0:
        return

    # Rising half of a Hann window: 0 .. 1 over fade_len samples.
    k = np.arange(fade_len, dtype=np.float64)
    ramp = 0.5 - 0.5 * np.cos(np.pi * k / fade_len)

    signal[:fade_len] *= ramp
    signal[n - fade_len :] *= ramp[::-1]


def _add_at(buffer: np.ndarray, chunk: np.ndarray, start: int) -> None:
    """Add ``chunk`` into ``buffer`` at ``start``, clipping at both ends in place."""
    if chunk.size == 0:
        return
    total = buffer.shape[0]
    if start >= total:
        return

    end = start + chunk.shape[0]
    src_start = 0
    dst_start = start
    if dst_start < 0:
        src_start = -dst_start
        dst_start = 0
    dst_end = min(end, total)
    src_end = src_start + (dst_end - dst_start)
    if src_end <= src_start:
        return
    buffer[dst_start:dst_end] += chunk[src_start:src_end]


def _fit_length(signal: np.ndarray, length: int) -> np.ndarray:
    """Return ``signal`` truncated or zero-padded to exactly ``length`` samples."""
    if signal.shape[0] == length:
        return signal
    if signal.shape[0] > length:
        return signal[:length]
    out = np.zeros(length, dtype=np.float64)
    out[: signal.shape[0]] = signal
    return out


def _resolve_metronome_start(schedule: Schedule, config: Config) -> int:
    """Resolve ``config.metronome_start`` to an absolute start sample index.

    ``"0"`` -> 0; ``"t0"`` -> sample of ``schedule.ffop_frame``; otherwise the
    value is parsed as seconds and converted to ``round(seconds * sr)``.
    """
    raw = config.metronome_start.strip().lower()
    if raw == "0":
        return 0
    if raw == "t0":
        return frame_start_sample(
            schedule.ffop_frame, schedule.sample_rate, schedule.fps
        )
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"metronome_start must be '0', 't0', or a number of seconds, "
            f"got {config.metronome_start!r}"
        ) from exc
    return int(round(seconds * schedule.sample_rate))


def _amp_to_dbfs(amp: float) -> float:
    """Convert a linear amplitude to dBFS (``-inf`` for amplitude 0)."""
    if amp <= 0.0:
        return float("-inf")
    return 20.0 * float(np.log10(amp))
