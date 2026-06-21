"""Unit tests for ``smpte_leader_gen.audio`` (CONTRACT.md §6/§11, SPEC.md §7/§11.2).

These tests are pure (no FFmpeg). They verify:
  * beep onset energy starts exactly at ``cue.sample`` for an isolated cue
    (``buffer[cue.sample-1] ~ 0`` and the window ``[cue.sample, cue.sample+k)`` is
    nonzero);
  * total audio length == ``round(total_frames * sr * den / num)``;
  * the mixed peak never exceeds -1 dBFS, including a metronome case that would
    otherwise clip.
"""

from __future__ import annotations

import math
import warnings
from fractions import Fraction

import numpy as np
import pytest

from smpte_leader_gen.audio import (
    FADE_MS,
    build_audio,
    dbfs_to_amp,
    metronome_click_samples,
    render_click,
    render_tone,
    synth_beep_track,
    synth_metronome,
    write_wav,
)
from smpte_leader_gen.config import build_config
from smpte_leader_gen.timing import (
    build_schedule,
    frame_start_sample,
    total_samples_for,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_config(**overrides):
    """Build a minimal valid Config, overridable per test."""
    base = dict(
        fps="24",
        resolution="128x72",
        output="out.mov",
    )
    base.update(overrides)
    return build_config(**base)


def make_schedule(config, **overrides):
    """Build a Schedule from a Config the way the pipeline adapter would."""
    kwargs = dict(
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
    kwargs.update(overrides)
    return build_schedule(**kwargs)


# ---------------------------------------------------------------------------
# dbfs_to_amp
# ---------------------------------------------------------------------------


def test_dbfs_to_amp_reference_points():
    assert dbfs_to_amp(0.0) == pytest.approx(1.0)
    assert dbfs_to_amp(-6.0) == pytest.approx(0.5011872336, rel=1e-9)
    assert dbfs_to_amp(-20.0) == pytest.approx(0.1, rel=1e-12)
    # -1 dBFS ceiling sanity.
    assert dbfs_to_amp(-1.0) == pytest.approx(10.0 ** (-1.0 / 20.0))


# ---------------------------------------------------------------------------
# render_tone
# ---------------------------------------------------------------------------


def test_render_tone_shape_and_onset():
    sr = 48000
    n = 480
    tone = render_tone(1000.0, n, sr, amp=0.5)
    assert tone.shape == (n,)
    assert tone.dtype == np.float64
    # First sample is at t=0 -> sin(0) == 0 exactly (fade does not move onset).
    assert tone[0] == pytest.approx(0.0, abs=1e-12)


def test_render_tone_peak_respects_amp():
    sr = 48000
    n = 4800  # long enough that the unfaded core reaches full peak
    amp = 0.25
    tone = render_tone(1000.0, n, sr, amp=amp)
    assert np.max(np.abs(tone)) <= amp + 1e-9
    # The middle (unfaded) region should reach close to the requested peak.
    mid = tone[n // 4 : 3 * n // 4]
    assert np.max(np.abs(mid)) == pytest.approx(amp, rel=0.02)


def test_render_tone_fades_to_low_at_ends():
    sr = 48000
    n = 4800
    tone = render_tone(1000.0, n, sr, amp=1.0)
    # Head and tail are inside the fade region -> near zero.
    assert abs(tone[0]) < 1e-6
    assert abs(tone[-1]) < 1e-2  # last sample within fade ramp, small


def test_render_tone_empty():
    assert render_tone(1000.0, 0, 48000).shape == (0,)
    assert render_tone(1000.0, -5, 48000).shape == (0,)


def test_render_tone_fade_clamped_for_short_tone():
    # Tone shorter than 2*fade: must not error and stays within amp.
    sr = 48000
    n = 5  # ~0.1 ms, far shorter than 2 * FADE_MS
    tone = render_tone(1000.0, n, sr, amp=1.0, fade_ms=FADE_MS)
    assert tone.shape == (n,)
    assert np.all(np.abs(tone) <= 1.0 + 1e-9)


# ---------------------------------------------------------------------------
# render_click
# ---------------------------------------------------------------------------


def test_render_click_window_and_timbre():
    sr = 48000
    click = render_click(2000.0, sr, amp=0.5, dur_ms=6.0)
    expected_n = int(round(6.0e-3 * sr))
    assert click.shape == (expected_n,)
    assert click.dtype == np.float64
    # Full Hann window -> both ends at ~0, distinct from a sustained tone.
    assert abs(click[0]) < 1e-9
    assert abs(click[-1]) < 1e-6
    assert np.max(np.abs(click)) <= 0.5 + 1e-9


# ---------------------------------------------------------------------------
# Beep onset alignment (the key acceptance test)
# ---------------------------------------------------------------------------


def test_beep_onset_lands_exactly_at_cue_sample_isolated():
    # Use an isolated cue: disable per-second beeps so only the 2-pop fires.
    config = make_config(
        fps="23.976",
        lead_seconds=8,
        no_persecond_beep=True,
        beep_frames=2,
    )
    schedule = make_schedule(config)
    # Exactly one cue (the 2-pop).
    assert len(schedule.beep_cues) == 1
    cue = schedule.beep_cues[0]
    assert cue.is_twopop

    buffer = synth_beep_track(schedule, config)
    assert buffer.shape == (schedule.total_samples,)

    # Sample just before onset is ~zero (silence before the cue).
    assert abs(buffer[cue.sample - 1]) < 1e-9

    # The very first sample of the tone sits at the onset boundary; because the
    # raised-cosine fade starts AT the boundary it is itself ~0, but the energy
    # window [cue.sample, cue.sample + k) must be nonzero.
    assert abs(buffer[cue.sample]) < 1e-9  # onset boundary sample
    window = buffer[cue.sample : cue.sample + 64]
    assert np.any(np.abs(window) > 1e-6)
    # Energy is contained at/after the boundary, not before it.
    pre = buffer[max(0, cue.sample - 64) : cue.sample]
    assert np.all(np.abs(pre) < 1e-9)


def test_beep_onset_alignment_multiple_rates():
    for fps in ["24", "25", "29.97", "30", "59.94"]:
        config = make_config(fps=fps, no_persecond_beep=True, beep_frames=1)
        schedule = make_schedule(config)
        cue = schedule.beep_cues[0]
        # cue.sample is exactly the drift-free boundary.
        assert cue.sample == frame_start_sample(
            cue.frame, schedule.sample_rate, schedule.fps
        )
        buffer = synth_beep_track(schedule, config)
        assert abs(buffer[cue.sample - 1]) < 1e-9
        assert np.any(np.abs(buffer[cue.sample : cue.sample + 32]) > 1e-6)


def test_persecond_beeps_count_and_twopop_merge():
    config = make_config(fps="24", lead_seconds=8)
    schedule = make_schedule(config)
    # 8 per-second cues, one of which is flagged the 2-pop (merged, not added).
    assert len(schedule.beep_cues) == 8
    assert sum(1 for c in schedule.beep_cues if c.is_twopop) == 1
    buffer = synth_beep_track(schedule, config)
    # Each cue contributes energy at its onset window.
    for cue in schedule.beep_cues:
        assert np.any(np.abs(buffer[cue.sample : cue.sample + 64]) > 1e-6)


# ---------------------------------------------------------------------------
# Total samples == round(total_frames * sr * den / num)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", ["23.976", "24", "25", "29.97", "30", "59.94", "60"])
def test_total_samples_matches_formula(fps):
    config = make_config(fps=fps, lead_seconds=8, tail_seconds=2.0)
    schedule = make_schedule(config)
    num = schedule.fps.numerator
    den = schedule.fps.denominator
    expected = (schedule.total_frames * schedule.sample_rate * den + num // 2) // num
    assert schedule.total_samples == expected
    assert schedule.total_samples == total_samples_for(
        schedule.total_frames, schedule.sample_rate, schedule.fps
    )

    beep = synth_beep_track(schedule, config)
    metro = synth_metronome(schedule, config)
    stereo, _ = build_audio(schedule, config)
    assert beep.shape == (expected,)
    assert metro.shape == (expected,)
    assert stereo.shape == (expected, 2)


def test_build_audio_length_exact_and_stereo_centered():
    config = make_config(fps="23.976", lead_seconds=8)
    schedule = make_schedule(config)
    stereo, peak_dbfs = build_audio(schedule, config)
    assert stereo.shape == (schedule.total_samples, 2)
    assert stereo.dtype == np.float64
    # Both channels identical (centered).
    assert np.array_equal(stereo[:, 0], stereo[:, 1])
    assert peak_dbfs <= -1.0 + 1e-9


# ---------------------------------------------------------------------------
# Metronome scheduling
# ---------------------------------------------------------------------------


def test_metronome_empty_without_bpm():
    config = make_config(fps="24")
    schedule = make_schedule(config)
    assert metronome_click_samples(schedule, config) == []
    metro = synth_metronome(schedule, config)
    assert metro.shape == (schedule.total_samples,)
    assert np.all(metro == 0.0)


def test_metronome_roles_and_cumulative_rounding():
    config = make_config(
        fps="24",
        lead_seconds=8,
        bpm=120.0,
        time_sig="4/4",
        subdivision=2,
        metronome_start="0",
    )
    schedule = make_schedule(config)
    clicks = metronome_click_samples(schedule, config)
    assert clicks, "expected metronome clicks"

    sr = schedule.sample_rate
    bpm = config.bpm
    subdivision = config.subdivision
    beats_per_bar = config.time_sig_beats
    denom = bpm * subdivision

    # Verify cumulative-rounding positions and role assignment for each click.
    expected = []
    k = 0
    while True:
        offset = int(round(k * sr * 60.0 / denom))
        sample = 0 + offset
        if sample >= schedule.total_samples:
            break
        if k % subdivision != 0:
            role = "sub"
        else:
            beat_index = k // subdivision
            role = "accent" if beat_index % beats_per_bar == 0 else "beat"
        expected.append((sample, role))
        k += 1
    assert clicks == expected

    # k=0 -> beat 0 -> accent at sample 0.
    assert clicks[0] == (0, "accent")
    # k=1 -> odd -> sub.
    assert clicks[1][1] == "sub"
    # k=2 -> beat 1 -> beat (not first of bar).
    assert clicks[2][1] == "beat"
    # k=8 -> beat 4 -> first of next bar -> accent.
    assert clicks[8][1] == "accent"


def test_metronome_start_t0_uses_ffop_sample():
    config = make_config(fps="24", lead_seconds=8, bpm=100.0, metronome_start="t0")
    schedule = make_schedule(config)
    clicks = metronome_click_samples(schedule, config)
    assert clicks
    ffop_sample = frame_start_sample(
        schedule.ffop_frame, schedule.sample_rate, schedule.fps
    )
    assert clicks[0][0] == ffop_sample


def test_metronome_start_numeric_seconds():
    config = make_config(fps="24", lead_seconds=8, bpm=100.0, metronome_start="0.5")
    schedule = make_schedule(config)
    clicks = metronome_click_samples(schedule, config)
    assert clicks
    assert clicks[0][0] == int(round(0.5 * schedule.sample_rate))


# ---------------------------------------------------------------------------
# Peak safety: mixed peak <= -1 dBFS, incl. a clipping metronome case
# ---------------------------------------------------------------------------


def test_mixed_peak_never_exceeds_minus_one_dbfs():
    config = make_config(fps="24", lead_seconds=8, beep_level_dbfs=-12.0)
    schedule = make_schedule(config)
    stereo, peak_dbfs = build_audio(schedule, config)
    ceiling = dbfs_to_amp(-1.0)
    assert np.max(np.abs(stereo)) <= ceiling + 1e-9
    assert peak_dbfs <= -1.0 + 1e-9


def test_peak_safety_scales_down_clipping_mix_with_warning():
    # Drive beep and metronome hot so their sum would exceed full scale and clip.
    config = make_config(
        fps="24",
        lead_seconds=8,
        beep_level_dbfs=0.0,
        bpm=600.0,            # dense clicks so they coincide with beeps
        subdivision=4,
        metronome_level_dbfs=0.0,
        metronome_click_freq=1000.0,   # align with beep to maximize summing
        metronome_accent_freq=1000.0,
        metronome_sub_freq=1000.0,
    )
    schedule = make_schedule(config)

    # The raw (unscaled) mono mix should exceed the -1 dBFS ceiling.
    raw_mono = synth_beep_track(schedule, config) + synth_metronome(schedule, config)
    ceiling = dbfs_to_amp(-1.0)
    assert np.max(np.abs(raw_mono)) > ceiling

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        stereo, peak_dbfs = build_audio(schedule, config)

    # A warning was emitted and the output is at/below the ceiling.
    assert any(issubclass(w.category, UserWarning) for w in caught)
    assert np.max(np.abs(stereo)) <= ceiling + 1e-9
    assert peak_dbfs == pytest.approx(-1.0, abs=1e-6)


def test_no_warning_when_below_ceiling():
    config = make_config(fps="24", lead_seconds=8, beep_level_dbfs=-12.0)
    schedule = make_schedule(config)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build_audio(schedule, config)
    assert not any(issubclass(w.category, UserWarning) for w in caught)


# ---------------------------------------------------------------------------
# WAV writing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bit_depth,subtype", [(24, "PCM_24"), (16, "PCM_16")])
def test_write_wav_roundtrip(tmp_path, bit_depth, subtype):
    import soundfile as sf

    config = make_config(fps="24", lead_seconds=8, bit_depth=bit_depth)
    schedule = make_schedule(config)
    stereo, _ = build_audio(schedule, config)

    out = tmp_path / f"audio_{bit_depth}.wav"
    write_wav(out, stereo, schedule.sample_rate, bit_depth)
    assert out.exists()

    info = sf.info(str(out))
    assert info.samplerate == schedule.sample_rate
    assert info.channels == 2
    assert info.frames == schedule.total_samples
    assert info.subtype == subtype

    data, sr = sf.read(str(out))
    assert sr == schedule.sample_rate
    assert data.shape == (schedule.total_samples, 2)


def test_write_wav_rejects_bad_bit_depth(tmp_path):
    buf = np.zeros((10, 2), dtype=np.float64)
    with pytest.raises(ValueError):
        write_wav(tmp_path / "bad.wav", buf, 48000, 32)
