"""Unit tests for ``smpte_leader_gen.timing`` (CONTRACT.md §11, SPEC.md §11.1, §11.3)."""
from __future__ import annotations

from fractions import Fraction

import pytest

from smpte_leader_gen.timing import (
    FPS_PRESETS,
    Cue,
    Schedule,
    build_schedule,
    end_align_tail_frames,
    frame_start_sample,
    frames_per_block,
    parse_fps,
    round_half_up,
    total_samples_for,
)


# ---------------------------------------------------------------------------
# parse_fps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        # Preset keys.
        ("23.976", Fraction(24000, 1001)),
        ("24", Fraction(24)),
        ("25", Fraction(25)),
        ("29.97", Fraction(30000, 1001)),
        ("30", Fraction(30)),
        ("50", Fraction(50)),
        ("59.94", Fraction(60000, 1001)),
        ("60", Fraction(60)),
        # Broadcast decimal aliases.
        ("23.98", Fraction(24000, 1001)),
        # num/den rationals.
        ("24000/1001", Fraction(24000, 1001)),
        ("30000/1001", Fraction(30000, 1001)),
        ("48/1", Fraction(48)),
        # bare integers (str and int).
        ("48", Fraction(48)),
        (24, Fraction(24)),
        (60, Fraction(60)),
        # Fraction passthrough.
        (Fraction(25), Fraction(25)),
        # Generic (non-broadcast) decimal -> exact decimal Fraction.
        ("12.5", Fraction(25, 2)),
    ],
)
def test_parse_fps_accepts(value, expected):
    assert parse_fps(value) == expected


def test_parse_fps_presets_match_table():
    # Every preset key round-trips through parse_fps to the table value.
    for key, frac in FPS_PRESETS.items():
        assert parse_fps(key) == frac


def test_parse_fps_broadcast_decimals_are_rational_not_exact_decimal():
    # Critically NOT Fraction("23.976") == 2997/125.
    assert parse_fps("23.976") == Fraction(24000, 1001)
    assert parse_fps("23.976") != Fraction("23.976")


@pytest.mark.parametrize(
    "bad",
    ["0", "-24", "0/1", "24/0", "abc", "", "1/2/3", -5, 0, Fraction(0)],
)
def test_parse_fps_rejects(bad):
    with pytest.raises(ValueError):
        parse_fps(bad)


def test_parse_fps_rejects_bool():
    with pytest.raises(ValueError):
        parse_fps(True)


# ---------------------------------------------------------------------------
# round_half_up / frames_per_block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "x,expected",
    [
        (0.0, 0),
        (0.5, 1),
        (1.5, 2),
        (2.5, 3),  # round half UP, not banker's rounding (would be 2)
        (23.976, 24),
        (29.97, 30),
        (1.4999, 1),
        (-0.5, 0),  # -0.5 + 0.5 = 0.0 -> floor 0
    ],
)
def test_round_half_up(x, expected):
    assert round_half_up(x) == expected


@pytest.mark.parametrize(
    "fps,expected_f",
    [
        (Fraction(24000, 1001), 24),
        (Fraction(24), 24),
        (Fraction(25), 25),
        (Fraction(30000, 1001), 30),
        (Fraction(30), 30),
        (Fraction(60000, 1001), 60),
    ],
)
def test_frames_per_block_default(fps, expected_f):
    assert frames_per_block(fps) == expected_f


def test_frames_per_block_override():
    assert frames_per_block(Fraction(24), override=12) == 12


def test_frames_per_block_rejects_below_one():
    with pytest.raises(ValueError):
        frames_per_block(Fraction(24), override=0)


# ---------------------------------------------------------------------------
# frame_start_sample / total_samples_for — drift-free integer math
# ---------------------------------------------------------------------------


def test_frame_start_sample_matches_round_formula():
    fps = Fraction(24000, 1001)
    sr = 48000
    num, den = fps.numerator, fps.denominator
    for i in (0, 1, 2, 23, 24, 100, 191, 192, 1000):
        # Reference: round-half-up of i*sr*den/num.
        expected = (i * sr * den + num // 2) // num
        assert frame_start_sample(i, sr, fps) == expected


def test_total_samples_equals_frame_start_at_total_frames():
    fps = Fraction(24000, 1001)
    sr = 48000
    total_frames = 8 * 24 + 2 * 24  # lead 8 + tail 2 at F=24
    assert total_samples_for(total_frames, sr, fps) == frame_start_sample(
        total_frames, sr, fps
    )


def test_frame_start_sample_drift_free_vs_naive_per_frame_accumulation():
    """Integer cumulative rounding must NOT drift the way the common naive mistake
    of accumulating a per-frame *integer* sample count does, and the exact total
    must equal the closed-form integer formula."""
    fps = Fraction(24000, 1001)
    # Use 44100 Hz so samples-per-frame is genuinely fractional (~1839.3375);
    # at 48000 it happens to be exactly 2002 (48000/24000 == 2) and would not drift.
    sr = 44100
    total_frames = 24 * 240  # ~10 leader-seconds worth, enough to expose drift

    # The realistic naive mistake: pick a single rounded samples-per-frame and
    # add it every frame. The rounded value loses a fraction of a sample per
    # frame and drifts steadily.
    samples_per_frame_int = round(sr * float(1001) / 24000.0)  # 1839
    naive_acc = 0
    saw_drift = False
    max_naive_err = 0
    for i in range(total_frames + 1):
        exact = frame_start_sample(i, sr, fps)
        err = abs(naive_acc - exact)
        max_naive_err = max(max_naive_err, err)
        if err >= 1:
            saw_drift = True
        naive_acc += samples_per_frame_int

    # The exact integer total matches the closed-form round formula precisely.
    num, den = fps.numerator, fps.denominator
    exact_total = (total_frames * sr * den + num // 2) // num
    assert frame_start_sample(total_frames, sr, fps) == exact_total

    # The naive accumulation drifted by many whole samples, demonstrating why we
    # must never accumulate a per-frame sample count.
    assert saw_drift
    assert max_naive_err > 100  # tens of samples of accumulated error by the end


def test_frame_zero_is_sample_zero():
    for fps in (Fraction(24), Fraction(24000, 1001), Fraction(25)):
        assert frame_start_sample(0, 48000, fps) == 0


# ---------------------------------------------------------------------------
# build_schedule — totals, 2-pop, beeps, flash
# ---------------------------------------------------------------------------


def _schedule(
    fps=Fraction(24000, 1001),
    sr=48000,
    *,
    f_override=None,
    lead=8,
    tail=48,
    preroll=0,
    beep_frames=1,
    persecond=True,
    twopop=True,
    count_style="full",
):
    return build_schedule(
        fps=fps,
        sample_rate=sr,
        frames_per_block_override=f_override,
        lead_seconds=lead,
        tail_frames=tail,
        preroll_frames=preroll,
        beep_frames=beep_frames,
        persecond_beep=persecond,
        twopop=twopop,
        count_style=count_style,
    )


def test_total_frames_math():
    sched = _schedule(lead=8, tail=48, preroll=0)
    assert sched.F == 24
    assert sched.total_countdown_frames == 8 * 24
    assert sched.total_frames == 0 + 8 * 24 + 48
    assert sched.countdown_start_frame == 0
    assert sched.ffop_frame == 8 * 24


def test_total_frames_math_with_preroll():
    sched = _schedule(lead=8, tail=48, preroll=24)
    assert sched.total_frames == 24 + 8 * 24 + 48
    assert sched.countdown_start_frame == 24
    assert sched.ffop_frame == 24 + 8 * 24


def test_total_samples_matches_total_frames():
    sched = _schedule(lead=8, tail=48, preroll=12)
    assert sched.total_samples == frame_start_sample(
        sched.total_frames, sched.sample_rate, sched.fps
    )


def test_twopop_frame_and_sample():
    sched = _schedule(lead=8, tail=48, preroll=0)
    F = sched.F
    expected_frame = sched.ffop_frame - 2 * F
    twopops = [c for c in sched.beep_cues if c.is_twopop]
    assert len(twopops) == 1
    pop = twopops[0]
    assert pop.frame == expected_frame
    assert pop.sample == frame_start_sample(expected_frame, sched.sample_rate, sched.fps)
    # The flagged 2-pop must also exist among the per-second beeps (no duplicate).
    frames = [c.frame for c in sched.beep_cues]
    assert frames.count(expected_frame) == 1


def test_twopop_with_preroll():
    sched = _schedule(lead=8, tail=48, preroll=30)
    F = sched.F
    pop = next(c for c in sched.beep_cues if c.is_twopop)
    assert pop.frame == sched.ffop_frame - 2 * F
    assert pop.frame == sched.preroll_frames + (sched.lead_seconds - 2) * F


def test_persecond_beep_count_and_positions():
    sched = _schedule(lead=8, tail=48, preroll=0)
    F = sched.F
    assert len(sched.beep_cues) == 8  # one per leader-second
    expected_frames = [s * F for s in range(8)]
    assert [c.frame for c in sched.beep_cues] == expected_frames
    # Exactly one is the 2-pop.
    assert sum(1 for c in sched.beep_cues if c.is_twopop) == 1


def test_no_persecond_only_twopop():
    sched = _schedule(persecond=False, twopop=True)
    F = sched.F
    assert len(sched.beep_cues) == 1
    only = sched.beep_cues[0]
    assert only.is_twopop is True
    assert only.frame == sched.ffop_frame - 2 * F


def test_no_persecond_no_twopop_is_empty():
    sched = _schedule(persecond=False, twopop=False)
    assert sched.beep_cues == ()
    assert sched.flash_frames == frozenset()
    assert sched.twopop_flash_frames == frozenset()


def test_no_twopop_keeps_persecond_unflagged():
    sched = _schedule(persecond=True, twopop=False)
    assert len(sched.beep_cues) == 8
    assert all(c.is_twopop is False for c in sched.beep_cues)
    assert sched.twopop_flash_frames == frozenset()


def test_flash_frames_union_multiframe_beep():
    sched = _schedule(lead=8, tail=48, preroll=0, beep_frames=3)
    F = sched.F
    # Each cue contributes [frame, frame+3).
    for c in sched.beep_cues:
        for f in range(c.frame, c.frame + 3):
            assert f in sched.flash_frames
    # twopop flash frames are exactly the 2-pop range.
    pop = next(c for c in sched.beep_cues if c.is_twopop)
    assert sched.twopop_flash_frames == frozenset(range(pop.frame, pop.frame + 3))


def test_flash_frames_clipped_to_total():
    # A 2-pop whose multi-frame flash would run past total_frames must be clipped.
    sched = _schedule(lead=8, tail=0, persecond=True, twopop=True, beep_frames=4)
    for f in sched.flash_frames:
        assert 0 <= f < sched.total_frames


def test_beep_cues_ascending_by_frame():
    sched = _schedule()
    frames = [c.frame for c in sched.beep_cues]
    assert frames == sorted(frames)


def test_smpte_requires_lead_8():
    with pytest.raises(ValueError):
        _schedule(lead=7, count_style="smpte")


def test_smpte_lead_8_ok():
    sched = _schedule(lead=8, count_style="smpte")
    assert sched.count_style == "smpte"


def test_schedule_is_frozen_and_picklable():
    import pickle

    sched = _schedule()
    with pytest.raises(Exception):
        sched.F = 99  # type: ignore[misc]
    restored = pickle.loads(pickle.dumps(sched))
    assert restored == sched


# ---------------------------------------------------------------------------
# number_at
# ---------------------------------------------------------------------------


def test_number_at_full_counts_l_to_one():
    sched = _schedule(lead=8, tail=48, preroll=0, count_style="full")
    F = sched.F
    # First frame of each block shows L, L-1, ..., 1.
    for s in range(8):
        assert sched.number_at(s * F) == 8 - s
    # Last countdown frame still shows 1.
    assert sched.number_at(8 * F - 1) == 1


def test_number_at_preroll_and_tail_none():
    sched = _schedule(lead=8, tail=48, preroll=24, count_style="full")
    assert sched.number_at(0) is None  # preroll
    assert sched.number_at(sched.ffop_frame) is None  # first tail frame
    assert sched.number_at(sched.total_frames - 1) is None  # last tail frame


def test_number_at_smpte_8_to_2_then_blank():
    sched = _schedule(lead=8, tail=48, preroll=0, count_style="smpte")
    F = sched.F
    # remaining blocks 8..1 across s=0..7; smpte shows only 8..2.
    assert sched.number_at(0) == 8
    assert sched.number_at(6 * F) == 2  # remaining == 2
    assert sched.number_at(7 * F) is None  # remaining == 1 -> blank


def test_number_at_smpte_lead_gt_8_blanks_early_blocks():
    sched = _schedule(lead=10, tail=48, preroll=0, count_style="smpte")
    F = sched.F
    # s=0 -> remaining 10 (None), s=1 -> 9 (None), s=2 -> 8, ... s=8 -> 2, s=9 -> 1 (None)
    assert sched.number_at(0) is None
    assert sched.number_at(1 * F) is None
    assert sched.number_at(2 * F) == 8
    assert sched.number_at(8 * F) == 2
    assert sched.number_at(9 * F) is None


# ---------------------------------------------------------------------------
# counter_at — wrap behavior across rates (SPEC §11.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fps",
    [
        Fraction(24000, 1001),  # 23.976 -> F=24
        Fraction(25),  # F=25
        Fraction(30000, 1001),  # 29.97 -> F=30
        Fraction(30),  # F=30
        Fraction(60000, 1001),  # 59.94 -> F=60
    ],
)
def test_counter_wrap_and_number_decrement(fps):
    sched = _schedule(fps=fps, lead=8, tail=48, preroll=0, count_style="full")
    F = sched.F
    # frame 0 -> (0, F-1)
    assert sched.counter_at(0) == (0, F - 1)
    # frame F-1 -> (F-1, F-1)
    assert sched.counter_at(F - 1) == (F - 1, F - 1)
    # frame F -> (0, F-1) and number decremented relative to frame 0.
    assert sched.counter_at(F) == (0, F - 1)
    assert sched.number_at(F) == sched.number_at(0) - 1


def test_counter_preroll_local_mod():
    sched = _schedule(lead=8, tail=48, preroll=24, count_style="full")
    F = sched.F
    # Pre-roll uses its own local mod (j = i % F).
    assert sched.counter_at(0) == (0, F - 1)
    assert sched.counter_at(F - 1) == (F - 1, F - 1)
    # First countdown frame resets to 0.
    assert sched.counter_at(sched.preroll_frames) == (0, F - 1)


def test_counter_denominator_always_f_minus_one():
    sched = _schedule()
    for i in (0, 5, sched.F, sched.total_frames - 1):
        assert sched.counter_at(i)[1] == sched.F - 1


# ---------------------------------------------------------------------------
# leader_second_at / remaining_frames_to_ffop
# ---------------------------------------------------------------------------


def test_leader_second_at():
    sched = _schedule(lead=8, tail=48, preroll=24, count_style="full")
    F = sched.F
    assert sched.leader_second_at(0) == -1  # preroll -> negative
    assert sched.leader_second_at(sched.preroll_frames) == 0
    assert sched.leader_second_at(sched.preroll_frames + F) == 1
    assert sched.leader_second_at(sched.ffop_frame) == sched.lead_seconds  # tail >= L


def test_remaining_frames_to_ffop():
    sched = _schedule(lead=8, tail=48, preroll=0)
    assert sched.remaining_frames_to_ffop(0) == sched.ffop_frame
    assert sched.remaining_frames_to_ffop(sched.ffop_frame) == 0
    assert sched.remaining_frames_to_ffop(sched.ffop_frame + 5) == -5


# ---------------------------------------------------------------------------
# Cue dataclass shape
# ---------------------------------------------------------------------------


def test_cue_fields_and_frozen():
    cue = Cue(frame=10, sample=20000, kind="beep", is_twopop=True)
    assert cue.frame == 10
    assert cue.sample == 20000
    assert cue.kind == "beep"
    assert cue.is_twopop is True
    with pytest.raises(Exception):
        cue.frame = 0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# end_align_tail_frames
# ---------------------------------------------------------------------------


def test_end_align_beat_extends_to_next_beat_integer_fps():
    # 24 fps, 120 bpm => 12 frames/beat. lead=8 (192), preroll=0, tail=50 => 242.
    # Next beat boundary >= 242 is 252 (beat 21), so tail grows 50 -> 60.
    tail = end_align_tail_frames(
        fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0, tail_frames=50,
        bpm=120.0, beats_per_bar=4, subdivision=1, metronome_start_frame=0,
        unit="beat",
    )
    assert tail == 60
    assert (0 + 8 * 24 + tail) % 12 == 0  # total lands on a beat


def test_end_align_bar_extends_to_next_bar():
    # Same setup, bar = 4 beats = 48 frames. 242 -> next multiple of 48 is 288.
    tail = end_align_tail_frames(
        fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0, tail_frames=50,
        bpm=120.0, beats_per_bar=4, subdivision=1, metronome_start_frame=0,
        unit="bar",
    )
    assert tail == 288 - 192  # == 96
    assert (192 + tail) % 48 == 0


def test_end_align_subdivision_finer_grid():
    # subdivision=2 => 6 frames per click. 242 -> next multiple of 6 is 246.
    tail = end_align_tail_frames(
        fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0, tail_frames=50,
        bpm=120.0, beats_per_bar=4, subdivision=2, metronome_start_frame=0,
        unit="subdivision",
    )
    assert tail == 246 - 192  # == 54
    assert (192 + tail) % 6 == 0


def test_end_align_never_shortens_when_already_aligned():
    # tail=48 => total 240, already a multiple of 12 (beat). Tail unchanged.
    tail = end_align_tail_frames(
        fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0, tail_frames=48,
        bpm=120.0, beats_per_bar=4, subdivision=1, metronome_start_frame=0,
        unit="beat",
    )
    assert tail == 48


def test_end_align_honors_metronome_anchor():
    # Grid anchored at frame 5: beat boundaries at 5, 17, 29, ... (every 12).
    # total nominal = 192 + 50 = 242; next boundary >= 242 from anchor 5 is
    # 5 + 12*20 = 245.  tail 50 -> 53.
    tail = end_align_tail_frames(
        fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0, tail_frames=50,
        bpm=120.0, beats_per_bar=4, subdivision=1, metronome_start_frame=5,
        unit="beat",
    )
    assert tail == 53
    assert (192 + tail - 5) % 12 == 0


def test_end_align_fractional_fps_is_frame_accurate():
    # 23.976 fps, 120 bpm => 11.988 frames/beat. Beats round to nearest frame.
    fps = Fraction(24000, 1001)
    tail = end_align_tail_frames(
        fps=fps, F=24, lead_seconds=8, preroll_frames=0, tail_frames=50,
        bpm=120.0, beats_per_bar=4, subdivision=1, metronome_start_frame=0,
        unit="beat",
    )
    # Total must be >= nominal and on the rounded beat grid (round(m*11.988)).
    total = 192 + tail
    assert total >= 242
    m = round_half_up(total / (60.0 * float(fps) / 120.0))
    assert total == round_half_up(m * (60.0 * float(fps) / 120.0))


def test_end_align_bad_unit_raises():
    with pytest.raises(ValueError):
        end_align_tail_frames(
            fps=Fraction(24), F=24, lead_seconds=8, preroll_frames=0,
            tail_frames=50, bpm=120.0, beats_per_bar=4, subdivision=1,
            metronome_start_frame=0, unit="measure",
        )
