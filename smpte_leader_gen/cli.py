"""Command-line entry point for ``smpte_leader_gen``.

This module wires the user-facing ``click`` command (``smpte-leader-gen``) to the
configuration, scheduling, and rendering pipeline. Its responsibilities are
deliberately thin (SPEC §5, CONTRACT §9):

1. Declare every CLI flag with the exact long names from the contract.
2. Read an optional ``--config`` file first, then overlay *only* the flags the
   user explicitly passed on the command line (detected via
   :meth:`click.Context.get_parameter_source`) so explicit flags win over file
   values while defaults never clobber the file.
3. Build a validated :class:`~smpte_leader_gen.config.Config` via
   :func:`~smpte_leader_gen.config.build_config` (which parses fps/resolution/
   colors and resolves the container).
4. Run the pipeline (:func:`~smpte_leader_gen.pipeline.run`).
5. Translate :class:`ValueError` / ``FFmpegError`` into a clean stderr message
   and ``SystemExit(2)``.

In ``--verbose`` mode it prints the detected FFmpeg version and a resolved-config
summary before rendering; on success it prints the output path plus a summary
(duration, total frames, fps, profile).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import pydantic

from . import __version__
from .config import build_config, load_config_file
from .profiles import PROFILES

__all__ = ["main"]


# Flags whose CLI option name differs only by hyphen/underscore are handled by
# click automatically; this list is the set of click parameter names (already
# underscored) that map straight onto Config / build_config kwargs.
_PASSTHROUGH_PARAMS: tuple[str, ...] = (
    "lead_seconds",
    "tail_seconds",
    "preroll_seconds",
    "fps",
    "resolution",
    "output",
    "profile",
    "container",
    "start_timecode",
    "no_drop_frame",
    "sample_rate",
    "bit_depth",
    "beep_freq",
    "beep_frames",
    "beep_level_dbfs",
    "no_persecond_beep",
    "no_twopop",
    "bpm",
    "time_sig",
    "subdivision",
    "metronome_start",
    "metronome_level_dbfs",
    "metronome_accent_freq",
    "metronome_click_freq",
    "metronome_sub_freq",
    "metronome_style",
    "end_align",
    "end_cue",
    "count_style",
    "bg_color",
    "fg_color",
    "accent_color",
    "show_timecode_burnin",
    "no_clock",
    "no_counter",
    "no_timer",
    "no_flash",
    "frames_per_block",
    "timer_format",
    "seed",
    "jobs",
    "ffmpeg_path",
    "verbose",
)


def _fail(message: str) -> None:
    """Print an error to stderr and exit with status 2.

    Args:
        message: The error message to display (without a trailing newline).

    Raises:
        SystemExit: Always, with code ``2``.
    """
    click.echo(f"Error: {message}", err=True)
    raise SystemExit(2)


def _clean_validation_error(exc: pydantic.ValidationError) -> str:
    """Render a pydantic ``ValidationError`` as a concise one-line message.

    Strips pydantic's ``"Value error, "`` prefix and the input-dump / docs-URL
    noise, leaving only the human-readable reason(s) our validators raised.
    """
    msgs: list[str] = []
    for err in exc.errors():
        msg = str(err.get("msg", "")).strip()
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        if msg:
            msgs.append(msg)
    return "; ".join(msgs) if msgs else str(exc)


def _format_duration(total_frames: int, fps_num: int, fps_den: int) -> str:
    """Format the real-time duration of ``total_frames`` at ``num/den`` fps.

    Args:
        total_frames: Number of frames in the whole output.
        fps_num: Frame-rate numerator.
        fps_den: Frame-rate denominator.

    Returns:
        A ``"M:SS.fff"`` (or ``"S.fff s"`` when under a minute) human string.
    """
    seconds = total_frames * fps_den / fps_num
    if seconds < 60:
        return f"{seconds:.3f} s"
    minutes, rem = divmod(seconds, 60.0)
    return f"{int(minutes)}:{rem:06.3f}"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "--version", message="%(version)s")
# ---- core -----------------------------------------------------------------
@click.option("--lead-seconds", type=int, default=8, show_default=True,
              help="Number of leader-seconds counted down.")
@click.option("--tail-seconds", type=float, default=2.0, show_default=True,
              help="Black tail after t=0, in leader-seconds (may be 0).")
@click.option("--preroll-seconds", type=float, default=0.0, show_default=True,
              help="Optional black/slate before the countdown, in leader-seconds.")
@click.option("--fps", type=str, default="24000/1001", show_default=True,
              help="Frame rate: preset (23.976,24,25,29.97,30,50,59.94,60), "
                   "integer, or num/den.")
@click.option("--resolution", type=str, default="1080p", show_default=True,
              help="Resolution preset (720p,1080p,2kdci,uhd4k) or WxH.")
@click.option("--output", "-o", "output", type=click.Path(dir_okay=False),
              required=True, help="Output file path (required).")
# ---- encoding -------------------------------------------------------------
@click.option("--profile", type=click.Choice(list(PROFILES)),
              default="prores422hq", show_default=True,
              help="Encoding profile.")
@click.option("--container", type=click.Choice(["mov", "mkv", "mp4"]),
              default=None, help="Override container (default: from profile).")
@click.option("--start-timecode", type=str, default="00:00:00:00",
              show_default=True, help="Embedded SMPTE start timecode.")
@click.option("--no-drop-frame", is_flag=True, default=False,
              help="Disable drop-frame timecode for 29.97/59.94.")
# ---- audio ----------------------------------------------------------------
@click.option("--sample-rate", type=int, default=48000, show_default=True,
              help="Audio sample rate (Hz).")
@click.option("--bit-depth", type=click.Choice(["16", "24"]), default="24",
              callback=lambda ctx, param, value: int(value),
              show_default=True, help="Audio bit depth.")
@click.option("--beep-freq", type=float, default=1000.0, show_default=True,
              help="Sync beep frequency (Hz).")
@click.option("--beep-frames", type=int, default=1, show_default=True,
              help="Sync beep length in frames.")
@click.option("--beep-level-dbfs", type=float, default=-12.0, show_default=True,
              help="Peak level of the sync beep (dBFS).")
@click.option("--no-persecond-beep", is_flag=True, default=False,
              help="Emit only the 2-pop beep (classic).")
@click.option("--no-twopop", is_flag=True, default=False,
              help="Disable the 2-pop accent.")
# ---- metronome ------------------------------------------------------------
@click.option("--bpm", type=float, default=None,
              help="Enable the audio metronome at this tempo.")
@click.option("--time-sig", type=str, default="4/4", show_default=True,
              help="Metronome time signature (beats/value).")
@click.option("--subdivision", type=int, default=1, show_default=True,
              help="Metronome clicks per beat.")
@click.option("--metronome-start", type=str, default="0", show_default=True,
              help="When clicks begin: 0, t0, or seconds offset.")
@click.option("--metronome-level-dbfs", type=float, default=-14.0,
              show_default=True, help="Metronome peak level (dBFS).")
@click.option("--metronome-accent-freq", type=float, default=2000.0,
              show_default=True, help="Downbeat click frequency (Hz).")
@click.option("--metronome-click-freq", type=float, default=1500.0,
              show_default=True, help="Beat click frequency (Hz).")
@click.option("--metronome-sub-freq", type=float, default=1200.0,
              show_default=True, help="Subdivision click frequency (Hz).")
@click.option("--metronome-style", type=click.Choice(["dots", "pendulum"]),
              default="dots", show_default=True,
              help="Visual metronome style: 'dots' (a row of beat lights flashing "
                   "in turn, downbeat larger) or 'pendulum' (a swinging arm).")
@click.option("--end-align",
              type=click.Choice(["beat", "bar", "subdivision"]), default=None,
              help="Extend the black tail so the output ends exactly on the next "
                   "metronome beat/bar/subdivision (requires --bpm), so butt-joined "
                   "music continues the click grid.")
@click.option("--end-cue", is_flag=True, default=False,
              help="With --end-align, sound the final boundary beat as an audio "
                   "cue and end just after it (instead of ending silently on the "
                   "boundary).")
# ---- visual ---------------------------------------------------------------
@click.option("--count-style", type=click.Choice(["full", "smpte"]),
              default="full", show_default=True,
              help="Countdown style: full (L->1) or smpte (fixed 8->2).")
@click.option("--bg-color", type=str, default="#202020", show_default=True,
              help="Background color (hex).")
@click.option("--fg-color", type=str, default="#E0E0E0", show_default=True,
              help="Foreground color (hex).")
@click.option("--accent-color", type=str, default="#FF3030", show_default=True,
              help="Accent/flash color (hex).")
@click.option("--show-timecode-burnin", is_flag=True, default=False,
              help="Render an on-screen timecode burn-in.")
@click.option("--no-clock", is_flag=True, default=False,
              help="Hide the clock + sweep wand.")
@click.option("--no-counter", is_flag=True, default=False,
              help="Hide the x/N frame counter.")
@click.option("--no-timer", is_flag=True, default=False,
              help="Hide the digital countdown timer.")
@click.option("--no-flash", is_flag=True, default=False,
              help="Hide the flashing alignment disc.")
@click.option("--frames-per-block", type=int, default=None,
              help="Override F (frames per leader-second).")
@click.option("--timer-format",
              type=click.Choice(["auto", "seconds", "frames", "tc"]),
              default="auto", show_default=True,
              help="Digital timer format.")
@click.option("--seed", type=int, default=0, show_default=True,
              help="Reserved seed for future randomization.")
# ---- runtime --------------------------------------------------------------
@click.option("--jobs", type=int, default=1, show_default=True,
              help="Parallel render workers.")
@click.option("--ffmpeg-path", type=str, default=None,
              help="Explicit path to the ffmpeg binary.")
@click.option("--config", "config_path", type=click.Path(dir_okay=False),
              default=None, help="YAML/JSON config file (flags override it).")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Print FFmpeg version + resolved config; verbose output.")
@click.pass_context
def main(ctx: click.Context, *, config_path: str | None, **params: Any) -> None:
    """Render an AV-sync SMPTE-style countdown leader to a video file.

    Reads ``--config`` (if given) for baseline values, then overlays only the
    flags explicitly passed on the command line, builds a validated
    configuration, and runs the render/encode pipeline.
    """
    # 1. Load the config file (baseline values), if any.
    try:
        file_values: dict[str, Any] = load_config_file(
            Path(config_path) if config_path else None
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        _fail(str(exc))
        return  # pragma: no cover - _fail raises

    # 2. Overlay only explicitly-passed CLI flags over the file values.
    merged: dict[str, Any] = dict(file_values)
    for name in _PASSTHROUGH_PARAMS:
        source = ctx.get_parameter_source(name)
        if source is click.core.ParameterSource.COMMANDLINE:
            merged[name] = params[name]
        elif name not in merged:
            # No file value and not explicitly passed: use click's value
            # (its default, or an envvar/prompt resolution). This guarantees
            # required values like --output and sensible defaults are present.
            merged[name] = params[name]

    # 3. Build the validated Config (parses fps/resolution/colors, resolves
    #    the container). ValueError surfaces invalid inputs.
    try:
        config = build_config(**merged)
    except pydantic.ValidationError as exc:
        # Subclass of ValueError; format cleanly before the generic handler.
        _fail(_clean_validation_error(exc))
        return  # pragma: no cover - _fail raises
    except (ValueError, TypeError) as exc:
        _fail(str(exc))
        return  # pragma: no cover - _fail raises

    verbose = bool(config.verbose)

    # 4. Run the pipeline; translate domain errors to a clean exit(2).
    #    Imported lazily so CLI startup (and --help) stay fast and so the CLI
    #    can be imported even while heavy siblings are unavailable.
    from .encode import FFmpegError
    from .pipeline import build_schedule_from_config, run

    if verbose:
        _print_verbose_preamble(config)

    try:
        output_path = run(config, progress=not verbose)
    except (ValueError, FFmpegError) as exc:
        _fail(str(exc))
        return  # pragma: no cover - _fail raises

    # 5. Success summary.
    try:
        schedule = build_schedule_from_config(config)
        total_frames = schedule.total_frames
    except Exception:  # noqa: BLE001 - summary is best-effort, never fatal
        total_frames = None

    fps = config.fps
    num, den = fps.numerator, fps.denominator
    click.echo(f"Wrote: {output_path}")
    if total_frames is not None:
        duration = _format_duration(total_frames, num, den)
        click.echo(
            f"  duration={duration}  total_frames={total_frames}  "
            f"fps={num}/{den}  profile={config.profile}"
        )
    else:  # pragma: no cover - only if schedule construction fails
        click.echo(f"  fps={num}/{den}  profile={config.profile}")


def _print_verbose_preamble(config: Any) -> None:
    """Print the FFmpeg version and a resolved-config summary to stdout.

    Args:
        config: The resolved :class:`~smpte_leader_gen.config.Config`.
    """
    from .encode import FFmpegError, discover_ffmpeg

    try:
        ffmpeg, ffprobe, version = discover_ffmpeg(config.ffmpeg_path)
        major, minor, patch = version
        click.echo(f"ffmpeg: {ffmpeg} (version {major}.{minor}.{patch})")
        click.echo(f"ffprobe: {ffprobe}")
    except FFmpegError as exc:  # report but let pipeline raise the fatal one
        click.echo(f"ffmpeg: <not available: {exc}>")

    fps = config.fps
    click.echo("Resolved configuration:")
    click.echo(f"  output         = {config.output}")
    click.echo(f"  resolution     = {config.width}x{config.height}")
    click.echo(f"  fps            = {fps.numerator}/{fps.denominator}")
    click.echo(f"  F (frames/sec) = {config.F}")
    click.echo(f"  lead_seconds   = {config.lead_seconds}")
    click.echo(f"  tail_frames    = {config.tail_frames}")
    click.echo(f"  preroll_frames = {config.preroll_frames}")
    click.echo(f"  count_style    = {config.count_style}")
    click.echo(f"  bpm            = {config.bpm}")
    click.echo(f"  end_align      = {config.end_align}")
    click.echo(f"  end_cue        = {config.end_cue}")
    click.echo(f"  profile        = {config.profile}")
    click.echo(f"  container      = {config.container}")
    click.echo(f"  sample_rate    = {config.sample_rate}")
    click.echo(f"  bit_depth      = {config.bit_depth}")
    click.echo(f"  start_timecode = {config.start_timecode}")
    click.echo(f"  jobs           = {config.jobs}")


if __name__ == "__main__":  # pragma: no cover
    main()
