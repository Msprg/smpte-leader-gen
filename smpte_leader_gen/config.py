"""Resolved configuration model, input parsing, and validation.

This module owns the user-facing configuration surface for the leader generator.
It parses raw CLI/file inputs (frame rate, resolution, hex colors) into their
internal forms and validates them against the rules in SPEC.md §5 and
CONTRACT.md §3. The resolved configuration is an immutable (frozen) pydantic v2
:class:`Config` model.

All validation fails fast with a clear :class:`ValueError` (pydantic wraps these
in :class:`pydantic.ValidationError`). The public helpers are deterministic:
identical inputs always yield an identical :class:`Config`.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .profiles import PROFILES, get_profile, validate_profile_container
from .timing import frames_per_block, parse_fps, round_half_up

__all__ = [
    "RESOLUTION_PRESETS",
    "parse_resolution",
    "parse_color",
    "Config",
    "build_config",
    "load_config_file",
]


# ---------------------------------------------------------------------------
# Resolution and color parsing
# ---------------------------------------------------------------------------

#: Named resolution presets mapping to ``(width, height)`` in pixels.
RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2kdci": (2048, 1080),
    "uhd4k": (3840, 2160),
}

#: Container extensions understood when deriving the muxer from an output path.
_KNOWN_CONTAINERS: frozenset[str] = frozenset({"mov", "mkv", "mp4"})


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse a resolution specification into a ``(width, height)`` pair.

    Accepts a preset name (see :data:`RESOLUTION_PRESETS`, case-insensitive) or an
    explicit ``"WxH"`` form. The separator may be ``x``, ``X`` or ``*``.

    Args:
        value: Preset name or ``"WxH"`` string.

    Returns:
        A ``(width, height)`` tuple of positive integers.

    Raises:
        ValueError: If the value is not a known preset and not a valid ``WxH``
            string with two positive integers.
    """
    if not isinstance(value, str):
        raise ValueError(f"resolution must be a string, got {value!r}")

    text = value.strip()
    if not text:
        raise ValueError("resolution string is empty")

    key = text.lower()
    if key in RESOLUTION_PRESETS:
        return RESOLUTION_PRESETS[key]

    # Normalize the explicit "WxH" separators to a single 'x'.
    normalized = key.replace("*", "x").replace("X", "x")
    if "x" not in normalized:
        valid = ", ".join(RESOLUTION_PRESETS)
        raise ValueError(
            f"Unknown resolution {value!r}. Use a preset ({valid}) or 'WxH'."
        )

    w_str, _, h_str = normalized.partition("x")
    try:
        width = int(w_str.strip())
        height = int(h_str.strip())
    except ValueError as exc:
        raise ValueError(f"unparseable resolution: {value!r}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(
            f"resolution dimensions must be positive, got {width}x{height}"
        )
    return (width, height)


def parse_color(value: str) -> tuple[float, float, float]:
    """Parse a hex color into an RGBf triple in ``[0.0, 1.0]``.

    Accepts ``"#RRGGBB"`` or ``"#RGB"`` (the leading ``#`` is optional). The
    shorthand ``#RGB`` form is expanded by doubling each nibble (``f`` -> ``ff``).

    Args:
        value: A hex color string.

    Returns:
        An ``(r, g, b)`` tuple with each component in ``[0.0, 1.0]``.

    Raises:
        ValueError: If the string is not a valid 3- or 6-digit hex color.
    """
    if not isinstance(value, str):
        raise ValueError(f"color must be a string, got {value!r}")

    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise ValueError(
            f"invalid color {value!r}: expected '#RRGGBB' or '#RGB'"
        )
    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError as exc:
        raise ValueError(f"invalid color {value!r}: non-hex digits") from exc

    return (r / 255.0, g / 255.0, b / 255.0)


# ---------------------------------------------------------------------------
# Configuration model
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """The fully resolved, validated, immutable run configuration.

    Field defaults mirror SPEC.md §5. Fields that are parsed at the edge
    (``fps``, colors) accept their raw string form via validators so that
    constructing a :class:`Config` directly from CLI values still works, but the
    recommended entry point is :func:`build_config`.

    The model is frozen; derived quantities are exposed as ``@property`` helpers
    rather than stored fields.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    # core
    lead_seconds: int = 8
    tail_seconds: float = 2.0
    preroll_seconds: float = 0.0
    fps: Fraction
    width: int
    height: int
    output: Path
    # encoding
    profile: str = "prores422hq"
    container: str | None = None
    start_timecode: str = "00:00:00:00"
    no_drop_frame: bool = False
    # audio
    sample_rate: int = 48000
    bit_depth: int = 24
    beep_freq: float = 1000.0
    beep_frames: int = 1
    beep_level_dbfs: float = -12.0
    no_persecond_beep: bool = False
    no_twopop: bool = False
    # metronome
    bpm: float | None = None
    time_sig: str = "4/4"
    subdivision: int = 1
    metronome_start: str = "0"
    metronome_level_dbfs: float = -14.0
    metronome_accent_freq: float = 2000.0
    metronome_click_freq: float = 1500.0
    metronome_sub_freq: float = 1200.0
    # visual
    count_style: str = "full"
    bg_color: tuple[float, float, float] = (0x20 / 255,) * 3
    fg_color: tuple[float, float, float] = (0xE0 / 255,) * 3
    accent_color: tuple[float, float, float] = (1.0, 0x30 / 255, 0x30 / 255)
    show_timecode_burnin: bool = False
    no_clock: bool = False
    no_counter: bool = False
    no_timer: bool = False
    no_flash: bool = False
    frames_per_block: int | None = None
    timer_format: str = "auto"
    seed: int = 0
    # runtime
    jobs: int = 1
    ffmpeg_path: str | None = None
    verbose: bool = False

    # ---- field-level coercion validators (parse raw forms) -------------------

    @field_validator("fps", mode="before")
    @classmethod
    def _coerce_fps(cls, value: object) -> Fraction:
        """Accept str/int/float/Fraction and coerce to an exact :class:`Fraction`."""
        if isinstance(value, Fraction):
            return value
        return parse_fps(value)  # type: ignore[arg-type]

    @field_validator("bg_color", "fg_color", "accent_color", mode="before")
    @classmethod
    def _coerce_color(cls, value: object) -> tuple[float, float, float]:
        """Accept a hex string or an existing RGBf triple."""
        if isinstance(value, str):
            return parse_color(value)
        return value  # type: ignore[return-value]

    @field_validator("output", mode="before")
    @classmethod
    def _coerce_output(cls, value: object) -> Path:
        """Accept str or Path for the output path."""
        if isinstance(value, Path):
            return value
        return Path(value)  # type: ignore[arg-type]

    # ---- field-level validation (SPEC §5 "MUST validate") -------------------

    @field_validator("lead_seconds")
    @classmethod
    def _check_lead_seconds(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"lead_seconds must be >= 1, got {v}")
        return v

    @field_validator("tail_seconds")
    @classmethod
    def _check_tail_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"tail_seconds must be >= 0, got {v}")
        return v

    @field_validator("preroll_seconds")
    @classmethod
    def _check_preroll_seconds(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"preroll_seconds must be >= 0, got {v}")
        return v

    @field_validator("fps")
    @classmethod
    def _check_fps_positive(cls, v: Fraction) -> Fraction:
        if v <= 0:
            raise ValueError(f"fps must be positive, got {v}")
        return v

    @field_validator("width", "height")
    @classmethod
    def _check_dimensions(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"dimensions must be positive, got {v}")
        return v

    @field_validator("sample_rate")
    @classmethod
    def _check_sample_rate(cls, v: int) -> int:
        if not (8000 <= v <= 192000):
            raise ValueError(
                f"sample_rate must be between 8000 and 192000, got {v}"
            )
        return v

    @field_validator("bit_depth")
    @classmethod
    def _check_bit_depth(cls, v: int) -> int:
        if v not in (16, 24):
            raise ValueError(f"bit_depth must be 16 or 24, got {v}")
        return v

    @field_validator("beep_frames")
    @classmethod
    def _check_beep_frames(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"beep_frames must be >= 1, got {v}")
        return v

    @field_validator("beep_freq")
    @classmethod
    def _check_beep_freq(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"beep_freq must be > 0, got {v}")
        return v

    @field_validator("bpm")
    @classmethod
    def _check_bpm(cls, v: float | None) -> float | None:
        if v is not None and not (10 <= v <= 999):
            raise ValueError(f"bpm must be between 10 and 999, got {v}")
        return v

    @field_validator("subdivision")
    @classmethod
    def _check_subdivision(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"subdivision must be >= 1, got {v}")
        return v

    @field_validator("time_sig")
    @classmethod
    def _check_time_sig(cls, v: str) -> str:
        import re

        m = re.fullmatch(r"(\d+)/(\d+)", v.strip())
        if not m:
            raise ValueError(
                f"time_sig must match 'beats/value' (e.g. '4/4'), got {v!r}"
            )
        beats, value = int(m.group(1)), int(m.group(2))
        if beats < 1 or value < 1:
            raise ValueError(
                f"time_sig beats and value must be positive, got {v!r}"
            )
        return v.strip()

    @field_validator("start_timecode")
    @classmethod
    def _check_start_timecode(cls, v: str) -> str:
        """Fail fast on a malformed start timecode (avoids a late FFmpeg error).

        Accepts ``HH:MM:SS:FF`` (non-drop) or ``HH:MM:SS;FF`` (drop-frame); the
        exact frame-field range is checked later against the rate in encode.py.
        """
        import re

        if not re.fullmatch(r"\d+:\d+:\d+[:;]\d+", v.strip()):
            raise ValueError(
                "start_timecode must be 'HH:MM:SS:FF' or 'HH:MM:SS;FF', "
                f"got {v!r}"
            )
        return v.strip()

    @field_validator("metronome_start")
    @classmethod
    def _check_metronome_start(cls, v: str) -> str:
        """Fail fast on an invalid metronome start (``0``, ``t0``, or seconds)."""
        text = str(v).strip()
        if text in ("0", "t0"):
            return text
        try:
            float(text)
        except ValueError:
            raise ValueError(
                "metronome_start must be '0', 't0', or a number of seconds, "
                f"got {v!r}"
            ) from None
        return text

    @field_validator("count_style")
    @classmethod
    def _check_count_style(cls, v: str) -> str:
        if v not in ("full", "smpte"):
            raise ValueError(
                f"count_style must be 'full' or 'smpte', got {v!r}"
            )
        return v

    @field_validator("timer_format")
    @classmethod
    def _check_timer_format(cls, v: str) -> str:
        if v not in ("auto", "seconds", "frames", "tc"):
            raise ValueError(
                "timer_format must be one of 'auto', 'seconds', 'frames', "
                f"'tc', got {v!r}"
            )
        return v

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, v: str) -> str:
        if v not in PROFILES:
            valid = ", ".join(sorted(PROFILES))
            raise ValueError(f"Unknown profile {v!r}. Valid profiles: {valid}.")
        return v

    @field_validator("frames_per_block")
    @classmethod
    def _check_frames_per_block(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"frames_per_block must be >= 1, got {v}")
        return v

    @field_validator("jobs")
    @classmethod
    def _check_jobs(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"jobs must be >= 1, got {v}")
        return v

    # ---- cross-field validation -------------------------------------------

    @model_validator(mode="after")
    def _check_cross_field(self) -> "Config":
        """Validate constraints that span multiple fields.

        - ``count_style == "smpte"`` requires ``lead_seconds >= 8``.
        - ``container`` (if set) must be allowed for the selected ``profile``.
        - chroma-subsampled profiles require even ``width``/``height``.
        """
        # smpte count style requires at least 8 leader-seconds.
        if self.count_style == "smpte" and self.lead_seconds < 8:
            raise ValueError(
                "count_style 'smpte' requires lead_seconds >= 8, "
                f"got {self.lead_seconds}"
            )

        # The 2-pop coincides with the big number '2', which only exists for
        # lead_seconds >= 2; otherwise the canonical marker would land in the
        # pre-roll or before frame 0. Require it explicitly (or --no-twopop).
        if not self.no_twopop and self.lead_seconds < 2:
            raise ValueError(
                "the 2-pop accent requires lead_seconds >= 2 (the '2' second); "
                f"got {self.lead_seconds}. Use --no-twopop or increase "
                "--lead-seconds."
            )

        profile = get_profile(self.profile)

        # container compatibility (raises ValueError with a clear message).
        validate_profile_container(profile, self.container)

        # even-dimension requirement for chroma-subsampled pixel formats.
        if profile.requires_even_dimensions:
            if self.width % 2 != 0 or self.height % 2 != 0:
                raise ValueError(
                    f"profile {profile.name!r} (pix_fmt {profile.pix_fmt}) "
                    "requires even width and height, got "
                    f"{self.width}x{self.height}"
                )

        return self

    # ---- derived helpers ---------------------------------------------------

    @property
    def F(self) -> int:
        """Frames per leader-second block (``F``).

        ``frames_per_block(self.fps, self.frames_per_block)``.
        """
        return frames_per_block(self.fps, self.frames_per_block)

    @property
    def tail_frames(self) -> int:
        """Black-tail length in frames: ``round_half_up(tail_seconds * F)``."""
        return round_half_up(self.tail_seconds * self.F)

    @property
    def preroll_frames(self) -> int:
        """Pre-roll length in frames: ``round_half_up(preroll_seconds * F)``."""
        return round_half_up(self.preroll_seconds * self.F)

    @property
    def time_sig_beats(self) -> int:
        """The numerator (beats per bar) of ``time_sig``."""
        return int(self.time_sig.split("/")[0])

    @property
    def time_sig_value(self) -> int:
        """The denominator (note value) of ``time_sig``."""
        return int(self.time_sig.split("/")[1])


# ---------------------------------------------------------------------------
# Builders / loaders
# ---------------------------------------------------------------------------


def build_config(**kwargs: object) -> Config:
    """Build a validated :class:`Config` from raw CLI/file values.

    Raw string inputs are parsed before construction:
      * ``fps`` (str/int/float/Fraction) -> :class:`Fraction`.
      * ``resolution`` (``"WxH"`` or preset) -> ``width`` + ``height``. May also
        pass ``width``/``height`` directly.
      * ``bg_color`` / ``fg_color`` / ``accent_color`` hex strings -> RGBf.

    After validation the effective container is resolved from the profile (when
    not explicitly given) and **stored** on ``Config.container``, so the returned
    config's ``container`` is never ``None``.

    Args:
        **kwargs: Raw configuration values keyed by field name. ``resolution``
            is accepted as a convenience alias that expands to ``width`` and
            ``height``.

    Returns:
        A validated, frozen :class:`Config` with a resolved ``container``.

    Raises:
        ValueError: If any input is invalid (propagated from the parsers and
            validators). pydantic wraps validator failures in
            :class:`pydantic.ValidationError`, a subclass of ``ValueError``.
    """
    data = dict(kwargs)

    # Expand a "resolution" alias into explicit width/height.
    resolution = data.pop("resolution", None)
    if resolution is not None:
        width, height = parse_resolution(str(resolution))
        data.setdefault("width", width)
        data.setdefault("height", height)

    # The field validators coerce fps/colors/output, so no pre-parsing needed
    # here -- but parse explicitly when not already in their target type so any
    # ValueError surfaces as a plain ValueError too (pydantic also wraps it).
    config = Config(**data)  # type: ignore[arg-type]

    # Resolve and store the effective container (never None afterward).
    # Precedence: explicit --container > output file extension > profile default.
    profile = get_profile(config.profile)
    explicit = data.get("container")
    if explicit is not None:
        effective = validate_profile_container(profile, str(explicit))
    else:
        suffix = config.output.suffix.lower().lstrip(".")
        if suffix in _KNOWN_CONTAINERS:
            # An incompatible extension (e.g. ProRes into .mp4) fails fast here
            # instead of crashing FFmpeg after the whole render.
            effective = validate_profile_container(profile, suffix)
        else:
            effective = profile.container
    if config.container != effective:
        config = config.model_copy(update={"container": effective})

    return config


def load_config_file(path: Path | str | None) -> dict:
    """Load a YAML or JSON config file into a flat dict of CLI-flag values.

    The file's keys mirror the CLI flag names; hyphens are converted to
    underscores so the result can be merged straight into :func:`build_config`
    kwargs. Selected by file extension (``.json`` -> JSON, else YAML, which is a
    superset of JSON).

    Args:
        path: Path to the config file, or a falsy value for "no file".

    Returns:
        A dict with underscore-keyed values, or ``{}`` if ``path`` is falsy.

    Raises:
        ValueError: If the file does not contain a top-level mapping.
        FileNotFoundError: If the path is given but does not exist.
    """
    if not path:
        return {}

    p = Path(path)
    text = p.read_text(encoding="utf-8")

    if p.suffix.lower() == ".json":
        loaded = json.loads(text)
    else:
        loaded = yaml.safe_load(text)

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(
            f"config file {p} must contain a top-level mapping, "
            f"got {type(loaded).__name__}"
        )

    return {str(k).replace("-", "_"): v for k, v in loaded.items()}
