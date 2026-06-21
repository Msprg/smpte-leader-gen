"""Tests for the ``click`` CLI entry point (CONTRACT §9, §11).

These exercise the CLI's own responsibilities: option parsing, config-file
overlay, validation-error -> ``SystemExit(2)`` translation, and the
end-to-end "valid invocation produces a file" path. The heavy render/encode
work is covered elsewhere; the file-producing test is skipped when FFmpeg or the
pipeline module is unavailable.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from smpte_leader_gen.cli import main


# ---------------------------------------------------------------------------
# Availability helpers
# ---------------------------------------------------------------------------

_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_PIPELINE = importlib.util.find_spec("smpte_leader_gen.pipeline") is not None

_needs_render = pytest.mark.skipif(
    not (_FFMPEG and _PIPELINE),
    reason="requires ffmpeg/ffprobe and the pipeline module",
)


@pytest.fixture()
def runner() -> CliRunner:
    """A fresh ``CliRunner`` with stdout/stderr captured together."""
    return CliRunner()


# ---------------------------------------------------------------------------
# --help / --version
# ---------------------------------------------------------------------------


def test_help_succeeds(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    # A representative sample of the exact long flag names from the contract.
    for flag in (
        "--lead-seconds",
        "--fps",
        "--resolution",
        "--output",
        "--profile",
        "--container",
        "--bit-depth",
        "--count-style",
        "--timer-format",
        "--no-persecond-beep",
        "--no-twopop",
        "--bpm",
        "--config",
        "--verbose",
    ):
        assert flag in result.output, flag
    assert "-o," in result.output  # short output flag
    assert "-v," in result.output  # short verbose flag


def test_version_flag(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


# ---------------------------------------------------------------------------
# Validation errors -> nonzero exit with a message
# ---------------------------------------------------------------------------


def test_missing_output_required(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--fps", "24"])
    assert result.exit_code != 0
    assert "output" in result.output.lower()


def test_invalid_fps_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "leader.mov"
    result = runner.invoke(
        main,
        ["--fps", "totally-not-a-rate", "-o", str(out),
         "--resolution", "128x72"],
    )
    assert result.exit_code == 2
    assert result.output.strip()  # has a message
    assert "Error" in result.output


def test_prores_in_mp4_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "leader.mp4"
    result = runner.invoke(
        main,
        ["--profile", "prores422hq", "--container", "mp4",
         "--fps", "24", "--resolution", "128x72", "-o", str(out)],
    )
    assert result.exit_code == 2
    assert "mp4" in result.output.lower()


def test_smpte_with_short_lead_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    out = tmp_path / "leader.mov"
    result = runner.invoke(
        main,
        ["--count-style", "smpte", "--lead-seconds", "4",
         "--fps", "24", "--resolution", "128x72", "-o", str(out)],
    )
    assert result.exit_code == 2
    assert "smpte" in result.output.lower() or "lead" in result.output.lower()


def test_bad_bpm_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "leader.mov"
    result = runner.invoke(
        main,
        ["--bpm", "5", "--fps", "24", "--resolution", "128x72", "-o", str(out)],
    )
    assert result.exit_code == 2
    assert "bpm" in result.output.lower()


def test_odd_dims_with_420_profile_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    out = tmp_path / "leader.mp4"
    result = runner.invoke(
        main,
        ["--profile", "h264", "--resolution", "129x72",
         "--fps", "24", "-o", str(out)],
    )
    assert result.exit_code == 2
    assert "even" in result.output.lower()


def test_invalid_bit_depth_choice(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "leader.mov"
    result = runner.invoke(
        main,
        ["--bit-depth", "32", "--fps", "24", "--resolution", "128x72",
         "-o", str(out)],
    )
    # click.Choice rejection -> usage error (exit 2).
    assert result.exit_code != 0
    assert "32" in result.output


def test_invalid_profile_choice(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "leader.mov"
    result = runner.invoke(
        main,
        ["--profile", "nosuchprofile", "-o", str(out)],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Config-file overlay behavior (does not need ffmpeg)
# ---------------------------------------------------------------------------


def test_config_file_value_used_then_overridden(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File values are baseline; explicit CLI flags override; defaults don't."""
    captured: dict[str, object] = {}

    def fake_run(config, *, progress=True):  # type: ignore[no-untyped-def]
        captured["config"] = config
        return Path(config.output)

    # Patch the pipeline entry points the CLI imports lazily.
    import smpte_leader_gen.pipeline as pipeline_mod  # type: ignore

    monkeypatch.setattr(pipeline_mod, "run", fake_run, raising=False)

    cfg = tmp_path / "leader.yaml"
    cfg.write_text(
        "fps: '25'\n"
        "lead-seconds: 3\n"
        "resolution: 128x72\n"
        "profile: ffv1\n",
        encoding="utf-8",
    )
    out = tmp_path / "leader.mkv"

    # CLI overrides lead-seconds; fps/resolution/profile come from the file.
    result = runner.invoke(
        main,
        ["--config", str(cfg), "--lead-seconds", "5", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    config = captured["config"]
    from fractions import Fraction

    assert config.fps == Fraction(25)         # from file
    assert config.lead_seconds == 5            # CLI override beats file
    assert (config.width, config.height) == (128, 72)  # from file
    assert config.profile == "ffv1"            # from file


def test_build_config_parses_fps_resolution_colors(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid invocation yields a Config with parsed Fraction/res/colors."""
    captured: dict[str, object] = {}

    def fake_run(config, *, progress=True):  # type: ignore[no-untyped-def]
        captured["config"] = config
        return Path(config.output)

    import smpte_leader_gen.pipeline as pipeline_mod  # type: ignore

    monkeypatch.setattr(pipeline_mod, "run", fake_run, raising=False)

    out = tmp_path / "leader.mkv"
    result = runner.invoke(
        main,
        ["--fps", "23.976", "--resolution", "720p", "--profile", "ffv1",
         "--bg-color", "#102030", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    config = captured["config"]
    from fractions import Fraction

    assert config.fps == Fraction(24000, 1001)
    assert (config.width, config.height) == (1280, 720)
    assert config.bg_color == pytest.approx((0x10 / 255, 0x20 / 255, 0x30 / 255))
    # Container resolved (never None after build_config).
    assert config.container == "mkv"
    assert "Wrote:" in result.output


def test_verbose_does_not_crash(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(config, *, progress=True):  # type: ignore[no-untyped-def]
        return Path(config.output)

    import smpte_leader_gen.pipeline as pipeline_mod  # type: ignore

    monkeypatch.setattr(pipeline_mod, "run", fake_run, raising=False)

    out = tmp_path / "leader.mkv"
    result = runner.invoke(
        main,
        ["--verbose", "--fps", "24", "--resolution", "128x72",
         "--profile", "ffv1", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Resolved configuration" in result.output
    assert "ffmpeg:" in result.output


# ---------------------------------------------------------------------------
# Full produce-a-file path (skipped without ffmpeg / pipeline)
# ---------------------------------------------------------------------------


@_needs_render
def test_valid_invocation_produces_file(runner: CliRunner, tmp_path: Path) -> None:
    """A tiny ffv1/mkv render at low res actually writes an output file."""
    out = tmp_path / "leader.mkv"
    result = runner.invoke(
        main,
        [
            "--profile", "ffv1",
            "--fps", "24",
            "--resolution", "128x72",
            "--lead-seconds", "2",
            "--tail-seconds", "1",
            "--count-style", "full",
            "-o", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert out.stat().st_size > 0
    assert "Wrote:" in result.output
