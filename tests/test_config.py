"""Tests for ``smpte_leader_gen.config``.

Covers the input parsers (resolution, color), the derived ``@property`` helpers,
``build_config`` happy paths (Fraction/resolution/colors parsed, container
resolved), config-file loading, and the validation failures from CONTRACT §11's
``test_cli`` list that belong to config: invalid fps, odd dimensions with a 4:2:0
profile, ``smpte`` with ``lead < 8``, bad bpm, and ProRes in mp4.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from smpte_leader_gen.config import (
    RESOLUTION_PRESETS,
    Config,
    build_config,
    load_config_file,
    parse_color,
    parse_resolution,
)


# ---------------------------------------------------------------------------
# parse_resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("720p", (1280, 720)),
        ("1080p", (1920, 1080)),
        ("2kdci", (2048, 1080)),
        ("uhd4k", (3840, 2160)),
        ("1080P", (1920, 1080)),  # case-insensitive
    ],
)
def test_parse_resolution_presets(name: str, expected: tuple[int, int]) -> None:
    assert parse_resolution(name) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("640x480", (640, 480)),
        ("1920X1080", (1920, 1080)),
        ("128*72", (128, 72)),
        (" 320 x 240 ", (320, 240)),
    ],
)
def test_parse_resolution_explicit(value: str, expected: tuple[int, int]) -> None:
    assert parse_resolution(value) == expected


@pytest.mark.parametrize("bad", ["", "nope", "0x100", "100x0", "-10x20", "1920"])
def test_parse_resolution_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_resolution(bad)


def test_resolution_presets_table() -> None:
    assert RESOLUTION_PRESETS["1080p"] == (1920, 1080)


# ---------------------------------------------------------------------------
# parse_color
# ---------------------------------------------------------------------------


def test_parse_color_six_digit() -> None:
    assert parse_color("#202020") == pytest.approx((0x20 / 255,) * 3)
    assert parse_color("#FF3030") == pytest.approx((1.0, 0x30 / 255, 0x30 / 255))


def test_parse_color_three_digit_and_no_hash() -> None:
    # #RGB expands by doubling nibbles: f -> ff, 0 -> 00.
    assert parse_color("#f00") == pytest.approx((1.0, 0.0, 0.0))
    assert parse_color("E0E0E0") == pytest.approx((0xE0 / 255,) * 3)


@pytest.mark.parametrize("bad", ["", "#12", "#12345", "#GGGGGG", "#1234567"])
def test_parse_color_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_color(bad)


# ---------------------------------------------------------------------------
# build_config happy path
# ---------------------------------------------------------------------------


def _valid_kwargs(**overrides: object) -> dict:
    base: dict = {
        "fps": "23.976",
        "resolution": "1080p",
        "output": "out.mov",
    }
    base.update(overrides)
    return base


def test_build_config_parses_fps_resolution_colors(tmp_path: Path) -> None:
    cfg = build_config(
        fps="23.976",
        resolution="720p",
        output=tmp_path / "x.mov",
        bg_color="#101010",
        fg_color="#fff",
        accent_color="#FF3030",
    )
    assert cfg.fps == Fraction(24000, 1001)
    assert (cfg.width, cfg.height) == (1280, 720)
    assert cfg.bg_color == pytest.approx((0x10 / 255,) * 3)
    assert cfg.fg_color == pytest.approx((1.0, 1.0, 1.0))
    assert cfg.accent_color == pytest.approx((1.0, 0x30 / 255, 0x30 / 255))


def test_build_config_resolves_container_from_profile() -> None:
    # Default profile prores422hq -> container mov, never None.
    cfg = build_config(**_valid_kwargs(profile="prores422hq", output="out.mov"))
    assert cfg.container == "mov"

    # ffv1 -> mkv default.
    cfg2 = build_config(
        fps="24", resolution="1080p", output="out.mkv", profile="ffv1"
    )
    assert cfg2.container == "mkv"


def test_build_config_explicit_container_allowed() -> None:
    cfg = build_config(
        fps="24",
        resolution="1080p",
        output="out.mkv",
        profile="prores422hq",
        container="mkv",
    )
    assert cfg.container == "mkv"


def test_build_config_accepts_explicit_width_height() -> None:
    cfg = build_config(fps="25", width=640, height=480, output="o.mov")
    assert (cfg.width, cfg.height) == (640, 480)
    assert cfg.fps == Fraction(25)


def test_build_config_fps_rational_and_int() -> None:
    assert build_config(**_valid_kwargs(fps="24000/1001")).fps == Fraction(24000, 1001)
    assert build_config(**_valid_kwargs(fps="30")).fps == Fraction(30)
    assert build_config(**_valid_kwargs(fps=Fraction(60))).fps == Fraction(60)


def test_config_is_frozen() -> None:
    cfg = build_config(**_valid_kwargs())
    with pytest.raises((ValidationError, TypeError, AttributeError)):
        cfg.lead_seconds = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# derived property helpers
# ---------------------------------------------------------------------------


def test_property_helpers() -> None:
    cfg = build_config(
        fps="23.976",
        resolution="1080p",
        output="o.mov",
        tail_seconds=2.0,
        preroll_seconds=1.0,
        time_sig="3/4",
    )
    # F = round(23.976) = 24
    assert cfg.F == 24
    assert cfg.tail_frames == 48  # 2.0 * 24
    assert cfg.preroll_frames == 24  # 1.0 * 24
    assert cfg.time_sig_beats == 3
    assert cfg.time_sig_value == 4


def test_frames_per_block_override_property() -> None:
    cfg = build_config(**_valid_kwargs(frames_per_block=30))
    assert cfg.F == 30


# ---------------------------------------------------------------------------
# Validation failures (CONTRACT §11 test_cli list, config-owned)
# ---------------------------------------------------------------------------


def test_invalid_fps_raises() -> None:
    with pytest.raises(ValueError):
        build_config(fps="not-a-rate", resolution="1080p", output="o.mov")
    with pytest.raises(ValueError):
        build_config(fps="0", resolution="1080p", output="o.mov")
    with pytest.raises(ValueError):
        build_config(fps="-5", resolution="1080p", output="o.mov")


def test_odd_dimensions_with_h264_profile_raises() -> None:
    # h264 is yuv420p -> requires even dimensions.
    with pytest.raises(ValidationError):
        build_config(
            fps="24", resolution="1921x1080", output="o.mp4", profile="h264"
        )
    with pytest.raises(ValidationError):
        build_config(
            fps="24", resolution="1920x1081", output="o.mp4", profile="h264"
        )


def test_odd_dimensions_with_420_profile_message_mentions_even() -> None:
    with pytest.raises(ValidationError) as exc:
        build_config(
            fps="24", width=641, height=480, output="o.mp4", profile="h264"
        )
    assert "even" in str(exc.value).lower()


def test_odd_dimensions_allowed_for_444_profile() -> None:
    # x264lossless is yuv444p -> even dimensions NOT required.
    cfg = build_config(
        fps="24", resolution="641x481", output="o.mkv", profile="x264lossless"
    )
    assert (cfg.width, cfg.height) == (641, 481)


def test_smpte_with_lead_under_8_raises() -> None:
    with pytest.raises(ValidationError):
        build_config(
            **_valid_kwargs(count_style="smpte", lead_seconds=5)
        )


def test_smpte_with_lead_8_ok() -> None:
    cfg = build_config(**_valid_kwargs(count_style="smpte", lead_seconds=8))
    assert cfg.count_style == "smpte"
    assert cfg.lead_seconds == 8


def test_bad_bpm_raises() -> None:
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(bpm=5))  # below 10
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(bpm=1000))  # above 999


def test_valid_bpm_ok() -> None:
    cfg = build_config(**_valid_kwargs(bpm=120))
    assert cfg.bpm == 120


def test_prores_in_mp4_raises() -> None:
    # prores422hq allows mov,mkv only -> mp4 rejected.
    with pytest.raises(ValidationError):
        build_config(
            fps="24",
            resolution="1080p",
            output="o.mp4",
            profile="prores422hq",
            container="mp4",
        )


def test_other_validation_failures() -> None:
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(bit_depth=32))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(beep_frames=0))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(subdivision=0))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(time_sig="4-4"))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(lead_seconds=0))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(tail_seconds=-1))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(timer_format="nonsense"))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(count_style="weird"))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(profile="does-not-exist"))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(sample_rate=100))
    with pytest.raises(ValidationError):
        build_config(**_valid_kwargs(jobs=0))


# ---------------------------------------------------------------------------
# load_config_file
# ---------------------------------------------------------------------------


def test_load_config_file_falsy() -> None:
    assert load_config_file(None) == {}
    assert load_config_file("") == {}


def test_load_config_file_yaml_hyphen_keys(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "lead-seconds: 10\nfps: '24'\nno-twopop: true\ntime-sig: '3/4'\n",
        encoding="utf-8",
    )
    loaded = load_config_file(p)
    assert loaded == {
        "lead_seconds": 10,
        "fps": "24",
        "no_twopop": True,
        "time_sig": "3/4",
    }


def test_load_config_file_json(tmp_path: Path) -> None:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"lead-seconds": 4, "fps": "30"}), encoding="utf-8")
    assert load_config_file(p) == {"lead_seconds": 4, "fps": "30"}


def test_load_config_file_then_build(tmp_path: Path) -> None:
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "fps: '23.976'\nresolution: 720p\nlead-seconds: 4\n", encoding="utf-8"
    )
    data = load_config_file(p)
    data["output"] = tmp_path / "out.mov"
    cfg = build_config(**data)
    assert cfg.fps == Fraction(24000, 1001)
    assert (cfg.width, cfg.height) == (1280, 720)
    assert cfg.lead_seconds == 4
    assert cfg.container == "mov"


def test_load_config_file_non_mapping_raises(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config_file(p)
