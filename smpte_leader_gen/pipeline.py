"""End-to-end orchestration: schedule -> audio -> streamed frames -> FFmpeg mux.

This module ties the pure/deterministic building blocks together and drives the
single FFmpeg subprocess that muxes the rendered video and the synthesized audio
into the final file. It is the only place (besides :mod:`encode`'s version probe)
that spawns FFmpeg.

Streaming contract (SPEC §9.2, §10): frames are generated one at a time and
written straight to the FFmpeg ``stdin`` pipe, then discarded -- the whole frame
sequence is never held in memory. When ``config.jobs > 1`` the per-frame render is
fanned out across a :class:`concurrent.futures.ProcessPoolExecutor`, but results
are written to FFmpeg in strict ascending frame order so the decoded frame
content and ordering are identical regardless of the worker count. (The muxed
container bytes may still differ run to run because FFmpeg writes some
non-deterministic muxer metadata; the rendered frames themselves do not.)

See CONTRACT.md §8 and SPEC.md §9, §10 for the authoritative semantics.
"""

from __future__ import annotations

import os
import tempfile
import threading
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import click

from .audio import METRONOME_CLICK_MS, build_audio, write_wav
from .config import Config
from .encode import FFmpegError, build_ffmpeg_command, discover_ffmpeg
from .profiles import get_profile
from .render import FrameRenderer
from .timing import Schedule, build_schedule, end_align_tail_frames, round_half_up

__all__ = [
    "build_schedule_from_config",
    "run",
]


# ---------------------------------------------------------------------------
# Config -> Schedule adapter
# ---------------------------------------------------------------------------


def build_schedule_from_config(config: Config) -> Schedule:
    """Build the :class:`Schedule` for a run from a resolved :class:`Config`.

    This adapter maps configuration fields onto the keyword arguments of
    :func:`smpte_leader_gen.timing.build_schedule`. Notably it inverts the two
    "no-" flags into the positive forms the timing layer expects:
    ``persecond_beep = not config.no_persecond_beep`` and
    ``twopop = not config.no_twopop``. Frame-derived quantities (``F``,
    ``tail_frames``, ``preroll_frames``) come from the config's ``@property``
    helpers so the same rounding rules are applied everywhere.

    When ``config.end_align`` is set (and the metronome is enabled), the black
    tail is extended so the whole output ends on the next metronome grid boundary
    (beat / bar / subdivision) at or after the nominal end -- see
    :func:`_resolve_tail_frames`.

    Args:
        config: The fully resolved, validated run configuration.

    Returns:
        The resolved timing plan for the whole output.
    """
    return build_schedule(
        fps=config.fps,
        sample_rate=config.sample_rate,
        frames_per_block_override=config.frames_per_block,
        lead_seconds=config.lead_seconds,
        tail_frames=_resolve_tail_frames(config),
        preroll_frames=config.preroll_frames,
        beep_frames=config.beep_frames,
        persecond_beep=not config.no_persecond_beep,
        twopop=not config.no_twopop,
        count_style=config.count_style,
    )


def _resolve_tail_frames(config: Config) -> int:
    """Resolve the tail length, extending it for ``--end-align`` when requested.

    Without ``config.end_align`` (or without a metronome) this is just
    ``config.tail_frames``. Otherwise the tail is grown so the total output ends
    on the next metronome grid boundary, using the metronome's own anchor and
    tempo so the alignment matches the audible clicks. When ``config.end_cue`` is
    also set, a short ring-out is appended past the boundary so the boundary beat
    (the "first beat of the next period") sounds as an audio cue and the file ends
    just after it -- instead of ending silently exactly on the boundary.

    Args:
        config: The resolved run configuration.

    Returns:
        The (possibly extended) tail length in frames.
    """
    if config.end_align is None or config.bpm is None:
        return config.tail_frames

    F = config.F
    ffop_frame = config.preroll_frames + config.lead_seconds * F

    # The metronome grid anchor, in frames (mirrors audio._resolve_metronome_start
    # but in the frame domain): "0" -> 0, "t0" -> FFOP, otherwise seconds.
    raw = config.metronome_start.strip().lower()
    if raw == "0":
        start_frame = 0
    elif raw == "t0":
        start_frame = ffop_frame
    else:
        start_frame = round_half_up(float(raw) * float(config.fps))

    tail = end_align_tail_frames(
        fps=config.fps,
        F=F,
        lead_seconds=config.lead_seconds,
        preroll_frames=config.preroll_frames,
        tail_frames=config.tail_frames,
        bpm=config.bpm,
        beats_per_bar=config.time_sig_beats,
        subdivision=config.subdivision,
        metronome_start_frame=start_frame,
        unit=config.end_align,
    )

    if config.end_cue:
        # End-align lands the total exactly on the boundary, where the metronome
        # click is suppressed (its onset sample == total_samples). Append a short
        # ring-out so that click is included and fully audible, ending the file
        # right after it. The ring covers the click length plus one frame of
        # margin for the sub-frame jitter between the click onset and frame grid.
        import math

        click_frames = math.ceil(METRONOME_CLICK_MS * 1e-3 * float(config.fps))
        tail += max(2, click_frames + 1)

    return tail


# ---------------------------------------------------------------------------
# Multiprocessing worker (module-level, picklable)
# ---------------------------------------------------------------------------

#: Per-worker :class:`FrameRenderer`, lazily initialized by :func:`_init_worker`.
#: Kept at module scope so worker processes can reach it through their inherited
#: globals (the renderer itself is rebuilt in each worker; only the cheap,
#: picklable ``Config`` and ``Schedule`` cross the process boundary).
_WORKER_RENDERER: FrameRenderer | None = None


def _init_worker(config: Config, schedule: Schedule) -> None:
    """Initialize the per-worker :class:`FrameRenderer` (pool ``initializer``).

    Builds one renderer per worker process and stashes it in the module-level
    :data:`_WORKER_RENDERER` global so :func:`_render_one` can reuse it across many
    frames without re-paying the static-layer setup cost per frame.

    Args:
        config: The resolved run configuration (picklable).
        schedule: The resolved timing plan (picklable).
    """
    global _WORKER_RENDERER
    _WORKER_RENDERER = FrameRenderer(config, schedule)


def _render_one(i: int) -> bytes:
    """Render global frame ``i`` using this worker's :class:`FrameRenderer`.

    Args:
        i: Global frame index to render.

    Returns:
        The packed RGB24 bytes for frame ``i`` (length ``width * height * 3``).

    Raises:
        RuntimeError: If the worker was not initialized via :func:`_init_worker`.
    """
    if _WORKER_RENDERER is None:  # pragma: no cover - defensive guard
        raise RuntimeError("worker renderer not initialized")
    return _WORKER_RENDERER.render_frame(i)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------


def run(config: Config, *, progress: bool = True) -> Path:
    """Render the leader and mux it to ``config.output``, returning that path.

    Steps (SPEC §9):
      1. Discover FFmpeg (honoring ``config.ffmpeg_path``); print versions when
         ``config.verbose``.
      2. Build the schedule from the config.
      3. Synthesize the audio and write it to a temporary WAV file.
      4. Build the FFmpeg argv and spawn it with ``stdin``/``stderr`` pipes.
      5. Stream every frame's RGB24 bytes to ``stdin`` in strict ascending order
         (single-process, or a :class:`ProcessPoolExecutor` ordered map when
         ``config.jobs > 1``), behind a :func:`click.progressbar` when requested.
      6. Close ``stdin``, wait for FFmpeg; on a nonzero exit raise
         :class:`~smpte_leader_gen.encode.FFmpegError` with the captured stderr.
      7. Remove the temporary WAV in a ``finally`` block.

    Frames are never all held in memory; the rendered frame content and ordering
    are deterministic and independent of ``config.jobs`` (FFmpeg muxer metadata
    may still make the raw container bytes vary run to run).

    Args:
        config: The fully resolved, validated run configuration.
        progress: When ``True``, display a CLI progress bar over the frames.

    Returns:
        The path to the written output file (``config.output``).

    Raises:
        FFmpegError: If FFmpeg is missing/too old, or exits with a nonzero code.
    """
    # 1. Discover ffmpeg (and verify version).
    ffmpeg, ffprobe, version = discover_ffmpeg(config.ffmpeg_path)
    if config.verbose:
        major, minor, patch = version
        click.echo(
            f"Using ffmpeg {major}.{minor}.{patch} at {ffmpeg!r} "
            f"(ffprobe at {ffprobe!r})",
            err=True,
        )

    # 2. Schedule.
    schedule = build_schedule_from_config(config)

    # 3. Audio synthesis (peak warning is emitted by build_audio via warnings).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        stereo, peak_dbfs = build_audio(schedule, config)
    for w in caught:
        click.echo(str(w.message), err=True)
    if config.verbose:
        click.echo(f"Audio mix peak: {peak_dbfs:.2f} dBFS", err=True)

    # 4. Write the audio to a temporary WAV (delete=False so ffmpeg can read it;
    #    cleaned up in finally).
    tmp_wav = tempfile.NamedTemporaryFile(
        prefix="smpte_leader_", suffix=".wav", delete=False
    )
    tmp_wav.close()
    audio_path = Path(tmp_wav.name)

    output_path = config.output

    try:
        write_wav(audio_path, stereo, config.sample_rate, config.bit_depth)

        # 5. Build the ffmpeg command and stream frames into it.
        profile = get_profile(config.profile)
        cmd = build_ffmpeg_command(
            ffmpeg=ffmpeg,
            config=config,
            schedule=schedule,
            profile=profile,
            audio_path=audio_path,
            output_path=output_path,
        )
        if config.verbose:
            click.echo("ffmpeg command: " + " ".join(cmd), err=True)

        _stream_render(config, schedule, cmd, progress=progress)

        return output_path
    finally:
        # 7. Always clean up the temp WAV.
        try:
            os.unlink(audio_path)
        except OSError:
            pass


def _stream_render(
    config: Config,
    schedule: Schedule,
    cmd: list[str],
    *,
    progress: bool,
) -> None:
    """Spawn FFmpeg and stream all frames to its stdin in strict order.

    Splits the single- vs multi-process strategies but keeps a single place that
    owns the subprocess lifecycle, the progress bar, and the nonzero-exit error
    handling. Frames are written one at a time; nothing is buffered beyond what a
    single in-flight frame (or the pool's chunk) requires.

    Args:
        config: The resolved run configuration (provides ``jobs``).
        schedule: The resolved timing plan (provides ``total_frames``).
        cmd: The FFmpeg argv to spawn.
        progress: Whether to display a progress bar.

    Raises:
        FFmpegError: If FFmpeg exits with a nonzero return code. The captured
            stderr is included in the message.
    """
    import subprocess

    total_frames = schedule.total_frames

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None  # for type checkers; PIPE guarantees it

    # Drain ffmpeg's stderr concurrently with frame writing. FFmpeg emits
    # progress/stats to stderr; if left unread, its ~64 KiB pipe buffer fills on
    # long renders, ffmpeg blocks writing stderr, stops reading stdin, and the
    # frame writer blocks on stdin -> classic deadlock. A reader thread keeps the
    # pipe empty and accumulates the text for the error message.
    stderr_chunks: list[bytes] = []

    def _drain(stream) -> None:
        try:
            for chunk in iter(lambda: stream.read(65536), b""):
                stderr_chunks.append(chunk)
        except (OSError, ValueError):
            pass

    stderr_thread: threading.Thread | None = None
    if proc.stderr is not None:
        stderr_thread = threading.Thread(
            target=_drain, args=(proc.stderr,), daemon=True
        )
        stderr_thread.start()

    try:
        if config.jobs > 1:
            _feed_frames_parallel(
                proc, config, schedule, total_frames, progress=progress
            )
        else:
            _feed_frames_serial(
                proc, config, schedule, total_frames, progress=progress
            )
    except BrokenPipeError:
        # FFmpeg died early; fall through to collect stderr and report it.
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # Closing stdin lets ffmpeg finish and close stderr (EOF), which ends the
        # drain thread; join it before reaping so all stderr is captured.
        if stderr_thread is not None:
            stderr_thread.join()
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass
        proc.wait()

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with code {proc.returncode}:\n{stderr_text.strip()}"
        )


def _feed_frames_serial(
    proc,  # subprocess.Popen
    config: Config,
    schedule: Schedule,
    total_frames: int,
    *,
    progress: bool,
) -> None:
    """Render and write every frame in-process, in order.

    Args:
        proc: The running FFmpeg process (its ``stdin`` receives the frames).
        config: The resolved run configuration.
        schedule: The resolved timing plan.
        total_frames: The number of frames to render.
        progress: Whether to show a progress bar.
    """
    renderer = FrameRenderer(config, schedule)
    with _progressbar(total_frames, progress) as bar:
        for i in range(total_frames):
            proc.stdin.write(renderer.render_frame(i))
            bar.update(1)


def _feed_frames_parallel(
    proc,  # subprocess.Popen
    config: Config,
    schedule: Schedule,
    total_frames: int,
    *,
    progress: bool,
) -> None:
    """Render frames across a process pool and write them in strict frame order.

    ``executor.map`` preserves input ordering, so iterating its results yields
    frames ``0, 1, 2, ...`` in sequence regardless of which worker produced each
    one -- producing the same frame content in the same order as the serial path
    (the decoded video/audio is identical; raw container bytes need not be, due
    to FFmpeg muxer metadata).

    Args:
        proc: The running FFmpeg process (its ``stdin`` receives the frames).
        config: The resolved run configuration (provides ``jobs``).
        schedule: The resolved timing plan.
        total_frames: The number of frames to render.
        progress: Whether to show a progress bar.
    """
    workers = min(config.jobs, max(1, total_frames))
    # A modest chunksize amortizes IPC overhead without holding many frames.
    chunksize = max(1, min(8, total_frames // (workers * 4) or 1))

    with _progressbar(total_frames, progress) as bar:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(config, schedule),
        ) as executor:
            for frame_bytes in executor.map(
                _render_one, range(total_frames), chunksize=chunksize
            ):
                proc.stdin.write(frame_bytes)
                bar.update(1)


def _progressbar(total_frames: int, progress: bool):
    """Return a context-managed progress bar, or a no-op when ``progress`` is False.

    Args:
        total_frames: The total number of frames (the bar's length).
        progress: Whether to display a real progress bar.

    Returns:
        A context manager yielding an object with an ``update(n)`` method.
    """
    if progress:
        return click.progressbar(
            length=total_frames, label="Rendering frames"
        )
    return _NullProgress()


class _NullProgress:
    """A no-op stand-in for :func:`click.progressbar` (same ``update`` API)."""

    def __enter__(self) -> "_NullProgress":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def update(self, n: int) -> None:
        """Ignore progress updates."""
        return None
