"""Tests for ``smpte_leader_gen.profiles`` (CONTRACT.md §2, SPEC.md §8.1)."""

from __future__ import annotations

import dataclasses

import pytest

from smpte_leader_gen.profiles import (
    DEFAULT_PROFILE,
    PROFILES,
    Profile,
    get_profile,
    validate_profile_container,
)

# Expected table mirrors CONTRACT.md §2 exactly.
EXPECTED = {
    "prores422hq": dict(
        video_codec="prores_ks",
        video_opts=("-profile:v", "3"),
        pix_fmt="yuv422p10le",
        container="mov",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mov", "mkv"),
        requires_even_dimensions=True,
    ),
    "prores4444": dict(
        video_codec="prores_ks",
        video_opts=("-profile:v", "4"),
        pix_fmt="yuva444p10le",
        container="mov",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mov", "mkv"),
        requires_even_dimensions=False,
    ),
    "ffv1": dict(
        video_codec="ffv1",
        video_opts=("-level", "3"),
        pix_fmt="yuv422p10le",
        container="mkv",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mkv",),
        requires_even_dimensions=True,
    ),
    "x264lossless": dict(
        video_codec="libx264",
        video_opts=("-qp", "0"),
        pix_fmt="yuv444p",
        container="mkv",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mkv", "mov"),
        requires_even_dimensions=False,
    ),
    "h264": dict(
        video_codec="libx264",
        video_opts=("-crf", "18", "-preset", "slow"),
        pix_fmt="yuv420p",
        container="mp4",
        audio_codec="aac",
        audio_opts=("-b:a", "320k"),
        allowed_containers=("mp4", "mkv"),
        requires_even_dimensions=True,
    ),
}


def test_exactly_five_profiles() -> None:
    assert set(PROFILES) == set(EXPECTED)
    assert len(PROFILES) == 5


def test_default_profile() -> None:
    assert DEFAULT_PROFILE == "prores422hq"
    assert DEFAULT_PROFILE in PROFILES


def test_profile_is_frozen_dataclass() -> None:
    p = PROFILES["prores422hq"]
    assert isinstance(p, Profile)
    params = dataclasses.fields(Profile)
    assert {f.name for f in params} == {
        "name",
        "video_codec",
        "video_opts",
        "pix_fmt",
        "container",
        "audio_codec",
        "audio_opts",
        "allowed_containers",
        "note",
        "requires_even_dimensions",
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.name = "other"  # type: ignore[misc]


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_profile_fields(key: str) -> None:
    p = PROFILES[key]
    exp = EXPECTED[key]
    assert p.name == key
    assert p.video_codec == exp["video_codec"]
    assert p.video_opts == exp["video_opts"]
    assert isinstance(p.video_opts, tuple)
    assert p.pix_fmt == exp["pix_fmt"]
    assert p.container == exp["container"]
    assert p.audio_codec == exp["audio_codec"]
    assert p.audio_opts == exp["audio_opts"]
    assert isinstance(p.audio_opts, tuple)
    assert p.allowed_containers == exp["allowed_containers"]
    assert isinstance(p.allowed_containers, tuple)
    assert p.requires_even_dimensions == exp["requires_even_dimensions"]
    assert isinstance(p.note, str) and p.note  # non-empty note
    # Default container must itself be allowed.
    assert p.container in p.allowed_containers


def test_get_profile_returns_correct() -> None:
    assert get_profile("h264") is PROFILES["h264"]
    assert get_profile(DEFAULT_PROFILE) is PROFILES[DEFAULT_PROFILE]


def test_get_profile_unknown_raises_with_valid_list() -> None:
    with pytest.raises(ValueError) as exc:
        get_profile("nope")
    msg = str(exc.value)
    for key in PROFILES:
        assert key in msg


def test_validate_container_none_returns_default() -> None:
    for key, p in PROFILES.items():
        assert validate_profile_container(p, None) == p.container


def test_validate_container_accepts_allowed() -> None:
    for p in PROFILES.values():
        for c in p.allowed_containers:
            assert validate_profile_container(p, c) == c


def test_validate_container_rejects_disallowed() -> None:
    p = PROFILES["prores422hq"]
    # mp4 is not allowed for prores.
    with pytest.raises(ValueError) as exc:
        validate_profile_container(p, "mp4")
    assert "mp4" in str(exc.value)
    assert "prores422hq" in str(exc.value)


def test_validate_container_rejects_for_ffv1() -> None:
    p = PROFILES["ffv1"]
    for bad in ("mov", "mp4"):
        with pytest.raises(ValueError):
            validate_profile_container(p, bad)
    assert validate_profile_container(p, "mkv") == "mkv"
