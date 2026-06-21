"""Encoding profile definitions for ``smpte_leader_gen``.

This module is PURE (no FFmpeg, no I/O). It declares the fixed table of
encoding profiles described in SPEC.md §8.1 and CONTRACT.md §2. Each
:class:`Profile` declares *intent* (video codec, options, pixel format, default
container, allowed containers, default audio codec, etc.). The final audio codec
decision is owned by ``encode.py`` based on bit depth and container; profiles
only declare the default.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    """A single immutable encoding profile.

    Attributes:
        name: Profile identifier (matches its key in :data:`PROFILES`).
        video_codec: FFmpeg video encoder name, e.g. ``"prores_ks"``.
        video_opts: Codec-specific flags as a flat tuple, e.g. ``("-profile:v", "3")``.
        pix_fmt: Output pixel format, e.g. ``"yuv422p10le"``.
        container: Default container extension WITHOUT a leading dot.
        audio_codec: Declared default audio codec (``"pcm_s24le"`` or ``"aac"``).
        audio_opts: Audio flags as a flat tuple, e.g. ``("-b:a", "320k")``; ``()`` for PCM.
        allowed_containers: Container extensions this profile may be muxed into.
        note: Human-readable description.
        requires_even_dimensions: True for chroma-subsampled (4:2:0 / 4:2:2) pixel
            formats whose dimensions MUST be even.
    """

    name: str
    video_codec: str
    video_opts: tuple[str, ...]
    pix_fmt: str
    container: str
    audio_codec: str
    audio_opts: tuple[str, ...]
    allowed_containers: tuple[str, ...]
    note: str
    requires_even_dimensions: bool


PROFILES: dict[str, Profile] = {
    "prores422hq": Profile(
        name="prores422hq",
        video_codec="prores_ks",
        video_opts=("-profile:v", "3"),
        pix_fmt="yuv422p10le",
        container="mov",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mov", "mkv"),
        note="Editorial standard, intra-frame.",
        requires_even_dimensions=True,
    ),
    "prores4444": Profile(
        name="prores4444",
        video_codec="prores_ks",
        video_opts=("-profile:v", "4"),
        pix_fmt="yuva444p10le",
        container="mov",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mov", "mkv"),
        note="If alpha / higher quality needed.",
        requires_even_dimensions=False,
    ),
    "ffv1": Profile(
        name="ffv1",
        video_codec="ffv1",
        video_opts=("-level", "3"),
        pix_fmt="yuv422p10le",
        container="mkv",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mkv",),
        note="Mathematically lossless, archival.",
        requires_even_dimensions=True,
    ),
    "x264lossless": Profile(
        name="x264lossless",
        video_codec="libx264",
        video_opts=("-qp", "0"),
        pix_fmt="yuv444p",
        container="mkv",
        audio_codec="pcm_s24le",
        audio_opts=(),
        allowed_containers=("mkv", "mov"),
        note="Lossless, smaller than FFV1 sometimes.",
        requires_even_dimensions=False,
    ),
    "h264": Profile(
        name="h264",
        video_codec="libx264",
        video_opts=("-crf", "18", "-preset", "slow"),
        pix_fmt="yuv420p",
        container="mp4",
        audio_codec="aac",
        audio_opts=("-b:a", "320k"),
        allowed_containers=("mp4", "mkv"),
        note="Max compatibility; inter-frame slightly softens single-frame markers.",
        requires_even_dimensions=True,
    ),
}

DEFAULT_PROFILE = "prores422hq"


def get_profile(name: str) -> Profile:
    """Return the :class:`Profile` registered under ``name``.

    Args:
        name: The profile key (one of :data:`PROFILES`).

    Returns:
        The matching :class:`Profile`.

    Raises:
        ValueError: If ``name`` is not a known profile. The message lists the
            valid profile names.
    """
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"Unknown profile {name!r}. Valid profiles: {valid}."
        ) from None


def validate_profile_container(profile: Profile, container: str | None) -> str:
    """Resolve and validate the effective container for ``profile``.

    Args:
        profile: The selected profile.
        container: Requested container extension WITHOUT a dot, or ``None`` to
            use the profile's default container.

    Returns:
        The effective container extension (without a dot).

    Raises:
        ValueError: If ``container`` is given but not in
            ``profile.allowed_containers``. The message lists the allowed
            containers.
    """
    if container is None:
        return profile.container
    if container not in profile.allowed_containers:
        allowed = ", ".join(profile.allowed_containers)
        raise ValueError(
            f"Container {container!r} is not allowed for profile "
            f"{profile.name!r}. Allowed containers: {allowed}."
        )
    return container
