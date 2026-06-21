"""FFmpeg discovery, SMPTE timecode math, and command construction.

This module owns everything that touches FFmpeg argv construction and version
discovery, plus the SMPTE timecode helpers (drop-frame and non-drop-frame
formatting / parsing). It is otherwise pure: command builders return ``list[str]``
argv and never spawn a process; only :func:`discover_ffmpeg` runs a subprocess
(``ffmpeg -version``) to determine the installed version.

See CONTRACT.md §7 and SPEC.md §8 for the authoritative semantics.

Key timecode rules (SPEC §8.2):
  * Non-drop timecode uses a ``:`` separator: ``HH:MM:SS:FF``.
  * Drop-frame timecode uses a ``;`` separator: ``HH:MM:SS;FF``.
  * Drop-frame applies to the 29.97 (30000/1001) and 59.94 (60000/1001) families.
    The 29.97 algorithm drops 2 frame numbers at the start of each minute except
    minutes divisible by 10; 59.94 drops 4 frame numbers under the same rule.

Encode trusts that profile/container compatibility was validated by ``config.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .config import Config
from .profiles import Profile, get_profile  # noqa: F401  (get_profile re-export for callers)
from .timing import Schedule

__all__ = [
    "FFmpegError",
    "discover_ffmpeg",
    "is_drop_frame",
    "frames_to_tc",
    "parse_tc",
    "build_ffmpeg_command",
    "build_ffprobe_streams_cmd",
]


#: The exact rationals that trigger drop-frame timecode (29.97 and 59.94 families).
_DROP_FRAME_RATES: frozenset[Fraction] = frozenset(
    {Fraction(30000, 1001), Fraction(60000, 1001)}
)


class FFmpegError(RuntimeError):
    """Raised when FFmpeg is missing, too old, or fails at runtime."""


# ---------------------------------------------------------------------------
# Discovery / version
# ---------------------------------------------------------------------------


_VERSION_RE = re.compile(r"ffmpeg version\s+n?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def discover_ffmpeg(
    ffmpeg_path: str | None = None,
) -> tuple[str, str, tuple[int, int, int]]:
    """Locate ``ffmpeg`` and ``ffprobe`` and verify the FFmpeg major version.

    If ``ffmpeg_path`` is given it is used directly (and ``ffprobe`` is derived
    from the same directory when present there, otherwise from ``PATH``). When
    ``ffmpeg_path`` is ``None`` both binaries are resolved from ``PATH``.

    The installed version is parsed from ``ffmpeg -version`` and FFmpeg major
    version 6 or newer is required (SPEC §8.3).

    Args:
        ffmpeg_path: Explicit path to the ``ffmpeg`` binary, or ``None`` to search
            ``PATH``.

    Returns:
        A ``(ffmpeg, ffprobe, (major, minor, patch))`` tuple. ``ffmpeg`` and
        ``ffprobe`` are resolved executable paths/names.

    Raises:
        FFmpegError: If ``ffmpeg`` or ``ffprobe`` cannot be located, if
            ``ffmpeg -version`` fails or is unparseable, or if the major version
            is less than 6.
    """
    # Resolve ffmpeg.
    if ffmpeg_path:
        ffmpeg = ffmpeg_path
        ffmpeg_dir = Path(ffmpeg_path).parent
    else:
        resolved = shutil.which("ffmpeg")
        if resolved is None:
            raise FFmpegError("ffmpeg not found on PATH")
        ffmpeg = resolved
        ffmpeg_dir = Path(resolved).parent

    # Resolve ffprobe: prefer one alongside ffmpeg, fall back to PATH.
    ffprobe_sibling = ffmpeg_dir / "ffprobe"
    if ffmpeg_path and ffprobe_sibling.exists():
        ffprobe = str(ffprobe_sibling)
    else:
        resolved_probe = shutil.which("ffprobe")
        if resolved_probe is not None:
            ffprobe = resolved_probe
        elif ffprobe_sibling.exists():
            ffprobe = str(ffprobe_sibling)
        else:
            raise FFmpegError("ffprobe not found alongside ffmpeg or on PATH")

    # Run `ffmpeg -version` and parse the version triple.
    try:
        proc = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise FFmpegError(f"failed to run {ffmpeg!r}: {exc}") from exc

    if proc.returncode != 0:
        raise FFmpegError(
            f"{ffmpeg!r} -version exited with code {proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )

    match = _VERSION_RE.search(proc.stdout)
    if match is None:
        # Some builds put the version on stderr; check there too.
        match = _VERSION_RE.search(proc.stderr)
    if match is None:
        first_line = (proc.stdout or proc.stderr).splitlines()
        head = first_line[0] if first_line else ""
        raise FFmpegError(f"could not parse ffmpeg version from: {head!r}")

    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)

    if major < 6:
        raise FFmpegError(
            f"FFmpeg >= 6.0 is required, found {major}.{minor}.{patch}"
        )

    return (ffmpeg, ffprobe, (major, minor, patch))


# ---------------------------------------------------------------------------
# Timecode helpers
# ---------------------------------------------------------------------------


def is_drop_frame(fps: Fraction, no_drop_frame: bool) -> bool:
    """Return whether drop-frame timecode applies.

    Drop-frame applies to the 29.97 (30000/1001) and 59.94 (60000/1001) rate
    families, unless explicitly disabled via ``no_drop_frame``.

    Args:
        fps: The exact frame rate.
        no_drop_frame: When ``True``, force non-drop-frame timecode.

    Returns:
        ``True`` if drop-frame timecode should be used, else ``False``.
    """
    if no_drop_frame:
        return False
    return fps in _DROP_FRAME_RATES


def _nominal_fps(fps: Fraction) -> int:
    """Return the integer ("nominal") frame count used for timecode wrapping.

    Timecode counts whole frames per second using the rounded nominal rate
    (e.g. 30 for 30000/1001, 24 for 24000/1001, 25 for 25). Drop-frame timecode
    renumbers frames within this nominal count; it does not change how many
    frames occupy a second.

    Args:
        fps: The exact frame rate.

    Returns:
        The nominal integer frames-per-second for timecode.
    """
    import math

    return math.floor(float(fps) + 0.5)


def _drop_count(nominal: int) -> int:
    """Return how many frame numbers are dropped per drop-minute.

    Two for the 30 fps nominal (29.97), four for the 60 fps nominal (59.94).

    Args:
        nominal: The nominal integer frames-per-second (30 or 60).

    Returns:
        The number of frame numbers dropped at each non-tenth minute boundary.
    """
    # 2 per minute at 30 nominal, 4 per minute at 60 nominal: scales as nominal/15.
    return 2 if nominal == 30 else 4


def frames_to_tc(frame: int, fps: Fraction, drop_frame: bool) -> str:
    """Format an absolute frame count as an SMPTE timecode string.

    Non-drop output uses ``HH:MM:SS:FF``; drop-frame output uses ``HH:MM:SS;FF``.
    The drop-frame algorithm renumbers frames so that two (29.97) or four (59.94)
    frame numbers are skipped at the start of every minute except minutes
    divisible by ten, keeping the timecode close to real elapsed time.

    Args:
        frame: Absolute frame count (``>= 0``).
        fps: The exact frame rate.
        drop_frame: Whether to apply drop-frame numbering and the ``;`` separator.

    Returns:
        The formatted timecode string.

    Raises:
        ValueError: If ``frame`` is negative.
    """
    if frame < 0:
        raise ValueError(f"frame must be >= 0, got {frame}")

    nominal = _nominal_fps(fps)
    sep = ";" if drop_frame else ":"

    if drop_frame:
        drop = _drop_count(nominal)
        # Frames dropped per 10-minute interval: 9 drop-minutes * drop.
        frames_per_min = nominal * 60
        frames_per_10min = nominal * 600

        d, m = divmod(frame, frames_per_10min - 9 * drop)
        # 'm' is the offset within a 10-minute block on the renumbered timeline.
        # Add back the dropped frames to get the true total-frame count.
        if m < drop:
            # Inside the first (un-dropped) minute of the 10-min block region
            # where no add-back yet applies beyond the block base.
            frame_adj = frame + 9 * drop * d
        else:
            frame_adj = (
                frame
                + 9 * drop * d
                + drop * ((m - drop) // (frames_per_min - drop))
            )

        ff = frame_adj % nominal
        total_seconds = frame_adj // nominal
        ss = total_seconds % 60
        mm = (total_seconds // 60) % 60
        hh = (total_seconds // 3600) % 24
        return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"

    # Non-drop: straightforward division by the nominal rate.
    ff = frame % nominal
    total_seconds = frame // nominal
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = (total_seconds // 3600) % 24
    return f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"


_TC_RE = re.compile(r"^(\d+):(\d+):(\d+)[:;](\d+)$")


def parse_tc(tc: str, fps: Fraction) -> int:
    """Parse an SMPTE timecode string into an absolute frame count.

    Inverse of :func:`frames_to_tc`. A ``;`` separator before the frame field is
    interpreted as drop-frame; a ``:`` as non-drop. Drop-frame parsing accounts
    for the skipped frame numbers so the result is the true absolute frame index.

    Args:
        tc: A timecode string ``HH:MM:SS:FF`` or ``HH:MM:SS;FF``.
        fps: The exact frame rate.

    Returns:
        The absolute frame count at ``fps``.

    Raises:
        ValueError: If ``tc`` does not match the expected format.
    """
    text = tc.strip()
    match = _TC_RE.match(text)
    if match is None:
        raise ValueError(
            f"invalid timecode {tc!r}: expected 'HH:MM:SS:FF' or 'HH:MM:SS;FF'"
        )

    hh, mm, ss, ff = (int(g) for g in match.groups())
    drop_frame = ";" in text
    nominal = _nominal_fps(fps)

    if drop_frame:
        drop = _drop_count(nominal)
        total_minutes = hh * 60 + mm
        # Frames dropped so far: 'drop' per minute, except minutes divisible by 10.
        dropped = drop * (total_minutes - total_minutes // 10)
        return (
            (hh * 3600 + mm * 60 + ss) * nominal + ff - dropped
        )

    return (hh * 3600 + mm * 60 + ss) * nominal + ff


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


#: Map a resolved container name to the FFmpeg muxer (``-f``) name.
_CONTAINER_MUXER: dict[str, str] = {"mov": "mov", "mkv": "matroska", "mp4": "mp4"}


def _muxer_args(container: str | None) -> tuple[str, ...]:
    """Return ``("-f", <muxer>)`` for a known container, else ``()``.

    Forcing the muxer makes the *resolved* (validated) container govern the
    output, so ``--container`` actually overrides the output-file extension and
    an incompatible extension can never silently select a different muxer.
    """
    muxer = _CONTAINER_MUXER.get((container or "").lower())
    return ("-f", muxer) if muxer else ()


def _select_audio_codec(profile: Profile, bit_depth: int) -> tuple[str, tuple[str, ...]]:
    """Choose the effective audio codec and its options.

    The PCM codec is chosen from ``bit_depth`` (16 -> ``pcm_s16le``,
    24 -> ``pcm_s24le``) except for profiles whose declared ``audio_codec`` is
    ``aac`` (the ``h264`` profile), which uses ``aac`` with a 320k bitrate.

    Args:
        profile: The selected encoding profile.
        bit_depth: The audio bit depth (16 or 24).

    Returns:
        A ``(codec, opts)`` pair, where ``opts`` is a flat argv tuple.
    """
    if profile.audio_codec == "aac":
        return ("aac", ("-b:a", "320k"))

    codec = "pcm_s24le" if bit_depth == 24 else "pcm_s16le"
    return (codec, ())


def build_ffmpeg_command(
    *,
    ffmpeg: str,
    config: Config,
    schedule: Schedule,
    profile: Profile,
    audio_path: Path,
    output_path: Path,
) -> list[str]:
    """Construct the full FFmpeg argv (no shell) for the render-then-mux pipeline.

    Two inputs are used: raw RGB24 frames piped on stdin (input 0) and the
    pre-rendered WAV file (input 1). The exact rational frame rate is passed to
    ``-r`` as ``num/den`` (never a decimal). BT.709 color metadata is attached.
    The audio codec is chosen by :func:`_select_audio_codec`. The start timecode
    is normalized so that a drop-frame rate uses a ``;`` separator.

    Args:
        ffmpeg: Path/name of the ``ffmpeg`` binary.
        config: The resolved run configuration.
        schedule: The resolved timing plan (provides ``fps``).
        profile: The selected encoding profile.
        audio_path: Path to the temporary WAV input.
        output_path: Path to the muxed output file.

    Returns:
        The complete argv list to pass to ``subprocess.Popen``.
    """
    fps = schedule.fps
    num = fps.numerator
    den = fps.denominator
    rate = f"{num}/{den}"
    size = f"{config.width}x{config.height}"

    drop_frame = is_drop_frame(fps, config.no_drop_frame)
    # Normalize the start timecode to the correct separator for the rate by
    # round-tripping through the frame domain.
    start_frame = parse_tc(config.start_timecode, fps)
    timecode = frames_to_tc(start_frame, fps, drop_frame)

    audio_codec, audio_opts = _select_audio_codec(profile, config.bit_depth)

    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        # Input 0: raw RGB24 video on stdin.
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        size,
        "-r",
        rate,
        "-i",
        "-",
        # Input 1: the WAV audio.
        "-i",
        str(audio_path),
        # Stream mapping.
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        # Video codec + options + pixel format.
        "-c:v",
        profile.video_codec,
        *profile.video_opts,
        "-pix_fmt",
        profile.pix_fmt,
        # BT.709 color metadata.
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        # Audio codec + options.
        "-c:a",
        audio_codec,
        *audio_opts,
        # Embedded timecode (drop-frame uses ';').
        "-timecode",
        timecode,
        # Exact rational output rate.
        "-r",
        rate,
        # Force the muxer from the resolved container (overrides the extension).
        *_muxer_args(config.container),
        str(output_path),
    ]


def build_ffprobe_streams_cmd(ffprobe: str, path: Path) -> list[str]:
    """Build the ffprobe argv for inspecting streams and format as JSON.

    Used by integration tests to verify frame rate, frame count, sample rate,
    channel count, and embedded timecode.

    Args:
        ffprobe: Path/name of the ``ffprobe`` binary.
        path: Path to the media file to probe.

    Returns:
        The argv list for ``ffprobe -v error -show_streams -show_format -of json``.
    """
    return [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
