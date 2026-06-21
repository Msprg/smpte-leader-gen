"""End-to-end integration tests (CONTRACT.md §11, SPEC.md §11.5).

These tests drive the full pipeline (:func:`smpte_leader_gen.pipeline.run`) for a
small, representative subset of encoding profiles, then inspect the produced file
with ``ffprobe`` to assert the contract-critical container facts:

  * the video stream's ``r_frame_rate`` equals the exact rational ``num/den``,
  * the audio stream has exactly two channels and the configured sample rate,
  * an SMPTE timecode is embedded in the container, and
  * the reported frame count equals ``schedule.total_frames``.

A dedicated 29.97 fps case (rendered with the fast ``ffv1`` profile) asserts that
the embedded timecode uses the drop-frame ``;`` separator.

All renders are intentionally tiny (128x72, two leader-seconds, a one-second tail)
so the suite stays fast. Every test skips gracefully when ``ffmpeg``/``ffprobe``
are not available on the system (``shutil.which``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from smpte_leader_gen import pipeline
from smpte_leader_gen.config import build_config
from smpte_leader_gen.encode import build_ffprobe_streams_cmd, discover_ffmpeg

# ---------------------------------------------------------------------------
# Skip handling: integration tests require a real FFmpeg/ffprobe toolchain.
# ---------------------------------------------------------------------------

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None
_HAVE_FFPROBE = shutil.which("ffprobe") is not None

pytestmark = pytest.mark.skipif(
    not (_HAVE_FFMPEG and _HAVE_FFPROBE),
    reason="ffmpeg/ffprobe not available on PATH",
)

# Tiny, fast render parameters shared across cases.
_WIDTH = 128
_HEIGHT = 72
_LEAD_SECONDS = 2
_TAIL_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ffprobe(path: Path, *, count_frames: bool = False) -> dict:
    """Run ``ffprobe`` on ``path`` and return the parsed JSON report.

    Args:
        path: Media file to probe.
        count_frames: When ``True``, add ``-count_frames`` so the video stream
            reports ``nb_read_frames`` (an exact decoded count). This is slower
            but reliable for containers that omit ``nb_frames``.

    Returns:
        The decoded ``ffprobe`` JSON document as a dict.
    """
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None  # guarded by pytestmark
    cmd = build_ffprobe_streams_cmd(ffprobe, path)
    if count_frames:
        # Insert the -count_frames flag right after the binary so it applies to
        # the stream enumeration.
        cmd = [cmd[0], "-count_frames", *cmd[1:]]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def _video_stream(report: dict) -> dict:
    """Return the first video stream from an ffprobe report."""
    for stream in report.get("streams", []):
        if stream.get("codec_type") == "video":
            return stream
    raise AssertionError("no video stream found in ffprobe output")


def _audio_stream(report: dict) -> dict:
    """Return the first audio stream from an ffprobe report."""
    for stream in report.get("streams", []):
        if stream.get("codec_type") == "audio":
            return stream
    raise AssertionError("no audio stream found in ffprobe output")


def _tags_timecode(tags: dict) -> list[str]:
    """Return any timecode values from a tag mapping (case-insensitive key).

    FFmpeg uses ``timecode`` for MOV/MP4 but Matroska stores it under the
    uppercase ``TIMECODE`` key, so the key is matched case-insensitively.

    Args:
        tags: A ``tags`` sub-mapping from an ffprobe stream or format object.

    Returns:
        The matching timecode value(s).
    """
    return [v for k, v in tags.items() if k.lower() == "timecode"]


def _collect_timecodes(report: dict) -> list[str]:
    """Gather every embedded timecode tag from the streams and the format.

    FFmpeg attaches the ``-timecode`` value as a format-level tag, a stream-level
    tag, or a dedicated timecode stream depending on the container, and the tag
    key casing differs between containers (``timecode`` vs ``TIMECODE``), so we
    scan all of them case-insensitively.

    Args:
        report: The ffprobe JSON report.

    Returns:
        A list of timecode strings found anywhere in the report.
    """
    found: list[str] = []
    found.extend(_tags_timecode(report.get("format", {}).get("tags", {})))
    for stream in report.get("streams", []):
        found.extend(_tags_timecode(stream.get("tags", {})))
    return found


def _reported_frame_count(report: dict, path: Path) -> int:
    """Return the container's video frame count, counting frames if needed.

    Prefers the cheap ``nb_frames`` field; falls back to a ``-count_frames``
    decode pass (``nb_read_frames``) for containers (notably Matroska) that do
    not store ``nb_frames`` in the header.

    Args:
        report: An ffprobe report already obtained for ``path``.
        path: The media file (re-probed with ``-count_frames`` if necessary).

    Returns:
        The integer frame count reported by the container.
    """
    vstream = _video_stream(report)
    nb_frames = vstream.get("nb_frames")
    if nb_frames not in (None, "N/A", "0"):
        return int(nb_frames)

    # Fall back to an exact decoded count.
    counted = _ffprobe(path, count_frames=True)
    cstream = _video_stream(counted)
    nb_read = cstream.get("nb_read_frames")
    assert nb_read not in (
        None,
        "N/A",
    ), "ffprobe reported neither nb_frames nor nb_read_frames"
    return int(nb_read)


def _run_and_probe(
    tmp_path: Path,
    *,
    profile: str,
    fps: str,
    output_name: str,
    sample_rate: int = 48000,
    container: str | None = None,
) -> tuple[dict, object, Path]:
    """Build a tiny config, run the pipeline, and ffprobe the result.

    Args:
        tmp_path: Per-test temporary directory (pytest fixture).
        profile: Encoding profile name.
        fps: Frame rate string (passed through ``build_config``).
        output_name: Output file name (its extension selects the container
            unless ``container`` overrides it).
        sample_rate: Audio sample rate.
        container: Optional explicit container override.

    Returns:
        A ``(report, schedule, output_path)`` tuple.
    """
    output_path = tmp_path / output_name
    kwargs: dict[str, object] = {
        "fps": fps,
        "resolution": f"{_WIDTH}x{_HEIGHT}",
        "output": str(output_path),
        "profile": profile,
        "lead_seconds": _LEAD_SECONDS,
        "tail_seconds": _TAIL_SECONDS,
        "sample_rate": sample_rate,
    }
    if container is not None:
        kwargs["container"] = container

    config = build_config(**kwargs)
    schedule = pipeline.build_schedule_from_config(config)

    result = pipeline.run(config, progress=False)
    assert result == output_path
    assert output_path.exists(), "pipeline.run did not produce the output file"
    assert output_path.stat().st_size > 0, "output file is empty"

    report = _ffprobe(output_path)
    return report, schedule, output_path


# ---------------------------------------------------------------------------
# Sanity: FFmpeg discovery works in this environment.
# ---------------------------------------------------------------------------


def test_discover_ffmpeg_present() -> None:
    """``discover_ffmpeg`` locates the toolchain and reports FFmpeg >= 6."""
    ffmpeg, ffprobe, version = discover_ffmpeg()
    assert ffmpeg
    assert ffprobe
    assert version[0] >= 6


# ---------------------------------------------------------------------------
# Per-profile end-to-end render + probe.
# ---------------------------------------------------------------------------


# (profile, output filename, fps string, expected exact Fraction)
_PROFILE_CASES = [
    pytest.param("ffv1", "leader.mkv", "24", Fraction(24), id="ffv1-24"),
    pytest.param("h264", "leader.mp4", "24", Fraction(24), id="h264-24"),
    pytest.param(
        "prores422hq", "leader.mov", "24", Fraction(24), id="prores422hq-24"
    ),
]


@pytest.mark.parametrize("profile, name, fps, expected_fps", _PROFILE_CASES)
def test_profile_end_to_end(
    tmp_path: Path,
    profile: str,
    name: str,
    fps: str,
    expected_fps: Fraction,
) -> None:
    """Render a tiny file per profile and ffprobe-assert the container facts."""
    sample_rate = 48000
    report, schedule, output_path = _run_and_probe(
        tmp_path,
        profile=profile,
        fps=fps,
        output_name=name,
        sample_rate=sample_rate,
    )

    # --- Video: exact rational frame rate (num/den, never a decimal). ---
    vstream = _video_stream(report)
    expected_rate = f"{expected_fps.numerator}/{expected_fps.denominator}"
    assert vstream["r_frame_rate"] == expected_rate, (
        f"r_frame_rate {vstream['r_frame_rate']!r} != {expected_rate!r}"
    )

    # --- Video: frame count == schedule.total_frames. ---
    frame_count = _reported_frame_count(report, output_path)
    assert frame_count == schedule.total_frames, (
        f"{profile}: container frame count {frame_count} != "
        f"schedule.total_frames {schedule.total_frames}"
    )

    # --- Audio: stereo at the configured sample rate. ---
    astream = _audio_stream(report)
    assert int(astream["channels"]) == 2, "expected exactly 2 audio channels"
    assert int(astream["sample_rate"]) == sample_rate, (
        f"sample_rate {astream['sample_rate']!r} != {sample_rate}"
    )

    # --- Timecode embedded somewhere in the container. ---
    timecodes = _collect_timecodes(report)
    assert timecodes, f"{profile}: no embedded timecode found in {output_path}"
    # Default start timecode is 00:00:00:00 (non-drop for integer rates).
    assert any(tc.startswith("00:00:00") for tc in timecodes), (
        f"{profile}: unexpected timecode(s) {timecodes!r}"
    )


# ---------------------------------------------------------------------------
# 23.976 (24000/1001): exact fractional rate survives to the container.
# ---------------------------------------------------------------------------


def test_fractional_rate_2398(tmp_path: Path) -> None:
    """A 23.976 render carries the exact 24000/1001 rate (ffv1, fast)."""
    report, schedule, output_path = _run_and_probe(
        tmp_path,
        profile="ffv1",
        fps="23.976",
        output_name="leader_2398.mkv",
    )
    vstream = _video_stream(report)
    assert vstream["r_frame_rate"] == "24000/1001"

    frame_count = _reported_frame_count(report, output_path)
    assert frame_count == schedule.total_frames


# ---------------------------------------------------------------------------
# 29.97 (30000/1001): drop-frame ';' timecode in the container metadata.
# ---------------------------------------------------------------------------


def test_dropframe_timecode_2997(tmp_path: Path) -> None:
    """A 29.97 render embeds drop-frame (``;``) timecode (ffv1, fast)."""
    report, schedule, output_path = _run_and_probe(
        tmp_path,
        profile="ffv1",
        fps="29.97",
        output_name="leader_2997.mkv",
    )

    # Exact rational rate preserved.
    vstream = _video_stream(report)
    assert vstream["r_frame_rate"] == "30000/1001"

    # Frame count matches the schedule.
    frame_count = _reported_frame_count(report, output_path)
    assert frame_count == schedule.total_frames

    # Stereo audio at the default sample rate.
    astream = _audio_stream(report)
    assert int(astream["channels"]) == 2

    # Drop-frame timecode uses the ';' separator before the frames field.
    timecodes = _collect_timecodes(report)
    assert timecodes, "no embedded timecode found for the 29.97 render"
    assert any(";" in tc for tc in timecodes), (
        f"expected drop-frame ';' timecode, got {timecodes!r}"
    )
