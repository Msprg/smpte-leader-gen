# CONTRACT.md — Internal Interface Contract (authoritative)

This document is the **binding interface contract** for `smpte_leader_gen`. Every module
MUST implement exactly the public names, signatures, and data shapes defined here so that
modules written independently integrate without changes. SPEC.md is the product spec; this
file resolves all cross-module ambiguity. Where SPEC.md and CONTRACT.md ever conflict on a
*signature*, CONTRACT.md wins; on *behavior/semantics*, SPEC.md wins.

Python target: **3.11+** (dev on 3.12). Style: type hints everywhere, `from __future__ import
annotations` at top of every module, dataclasses `frozen=True` where stated, no global mutable
state, pure/deterministic where stated. Use 4-space indent. Keep imports sorted stdlib/3rd/local.

Installed runtime libs (already in the venv): `numpy`, `pycairo` (`import cairo`), `Pillow`
(`from PIL import Image, ImageDraw, ImageFont`), `click`, `pydantic` v2, `soundfile`, `pyyaml`.
FFmpeg ≥ 6.0 is an external runtime dependency (present: 6.1.1).

Package root: `smpte_leader_gen/`. Bundled fonts already exist:
- `smpte_leader_gen/fonts/DejaVuSansMono.ttf`
- `smpte_leader_gen/fonts/DejaVuSansMono-Bold.ttf`

Provide `smpte_leader_gen/fonts/__init__.py`? No — `fonts/` is data, not a package. Resolve font
paths via `importlib.resources` or `pathlib.Path(__file__).parent / "fonts" / name`.

---

## 0. Shared conventions

- **Colors** are represented internally as `tuple[float, float, float]` with each component in
  `[0.0, 1.0]` (call this an **RGBf**). CLI accepts `#RRGGBB` (or `#RGB`) hex; `config.py` parses
  to RGBf. Cairo uses RGBf directly; Pillow text uses `tuple[int,int,int]` 0–255 (convert via
  `tuple(round(c*255) for c in rgbf)`).
- **fps** is always a `fractions.Fraction` once parsed (exact rational). `num = fps.numerator`,
  `den = fps.denominator`.
- **dBFS → linear amplitude**: `amp = 10 ** (dbfs / 20.0)`. Full-scale sine peak = 1.0.
- All sample-position math uses the cumulative-rounding rule. Never accumulate floats.
- Frames are 0-indexed globally over the whole output `[0, total_frames)`.

---

## 1. `smpte_leader_gen/timing.py`  (PURE, no FFmpeg, no cairo)

```python
from __future__ import annotations
from dataclasses import dataclass
from fractions import Fraction

FPS_PRESETS: dict[str, Fraction]   # "23.976"->Fraction(24000,1001), "24"->24, "25"->25,
                                   # "29.97"->Fraction(30000,1001), "30"->30, "50"->50,
                                   # "59.94"->Fraction(60000,1001), "60"->60

def parse_fps(value: str | int | float | Fraction) -> Fraction:
    """Accept presets (see FPS_PRESETS keys, match on str form), bare integers ('24', 24),
    decimals ('23.976' -> nearest preset if it matches a known fractional, else exact decimal
    as Fraction), or 'num/den'. Rules:
      - If str equals a preset key exactly -> that Fraction.
      - '24000/1001' style -> Fraction(num, den).
      - integer-like -> Fraction(int).
      - decimal like '23.976','29.97','59.94' MUST map to the broadcast rational
        (24000/1001, 30000/1001, 60000/1001). Other decimals -> Fraction(str-value).limit?
        No: use Fraction from the decimal string exactly (Fraction('23.976')=2997/125) ONLY if
        not one of the three broadcast decimals. Implement broadcast-decimal mapping explicitly.
    Raise ValueError on <=0 or unparseable."""

def round_half_up(x: float) -> int:
    """Deterministic round-half-up (avoid banker's rounding surprises in counters)."""

def frames_per_block(fps: Fraction, override: int | None = None) -> int:
    """F = override if given else round_half_up(float(fps)). Must be >=1."""

def frame_start_sample(i: int, sample_rate: int, fps: Fraction) -> int:
    """round(i * sample_rate * den / num) using integer math:
       (i * sample_rate * den + num // 2) // num  -> exact, drift-free, round-half-up."""

def total_samples_for(total_frames: int, sample_rate: int, fps: Fraction) -> int:
    """= frame_start_sample(total_frames, ...). Audio length in samples."""


@dataclass(frozen=True)
class Cue:
    frame: int            # global frame index where the cue's onset is
    sample: int           # frame_start_sample(frame)
    kind: str             # "beep"
    is_twopop: bool       # True only for the canonical 2-pop beep


@dataclass(frozen=True)
class Schedule:
    fps: Fraction
    sample_rate: int
    F: int                       # frames per leader-second block
    lead_seconds: int            # L
    tail_frames: int
    preroll_frames: int
    beep_frames: int             # beep length in frames (>=1)
    count_style: str             # "full" | "smpte"
    # derived:
    total_countdown_frames: int  # L * F
    total_frames: int            # preroll_frames + L*F + tail_frames
    total_samples: int           # total_samples_for(total_frames, sr, fps)
    countdown_start_frame: int   # == preroll_frames (first countdown frame, global index)
    ffop_frame: int              # First Frame Of Picture = preroll_frames + total_countdown_frames
    beep_cues: tuple[Cue, ...]   # all per-second beeps (and/or just 2-pop), ascending by frame
    flash_frames: frozenset[int] # union of [cue.frame, cue.frame+beep_frames) for non-twopop+twopop
    twopop_flash_frames: frozenset[int]  # subset of flash_frames belonging to the 2-pop, for accent

    def number_at(self, i: int) -> int | None:
        """Big countdown number for global frame i, or None if i is in preroll or tail (no number).
           full:  number = L - ((i - preroll_frames)//F), valid for countdown region, range L..1.
           smpte: fixed 8..2 sequence mapped onto the LAST 7 leader-seconds before FFOP.
                  Specifically for s_from_end in 0..: the leader-second whose number is k in 8..2.
                  Define: within countdown, let s = (i-preroll)//F (0-based from countdown start),
                  remaining_blocks = L - s (L..1). For smpte, number shown = remaining_blocks if
                  2 <= remaining_blocks <= 8 else None? NO. Classic SMPTE counts 8..2 then 2-pop.
                  Implement smpte as: number = remaining_blocks when remaining_blocks in 2..8,
                  and the frames of remaining_blocks==1 show nothing (blank/clock only). Requires
                  L>=8 (validated upstream). For L>8, leader-seconds before the '8' show blank.
                  Concretely: number = remaining_blocks if 2 <= remaining_blocks <= 8 else None."""

    def counter_at(self, i: int) -> tuple[int, int]:
        """Returns (j, F-1) where j = (i - preroll_frames) % F for countdown region.
           For preroll/tail, still return ((i)%F, F-1)? Define: counter runs over the WHOLE
           output using j=(i - preroll_frames) % F clamped so preroll uses its own local mod.
           Simpler/required behavior: counter value j = (i - preroll_frames) % F for i>=preroll,
           and for preroll frames j = i % F. Denominator always F-1."""

    def leader_second_at(self, i: int) -> int:
        """(i - preroll_frames)//F within countdown; for preroll returns negative; for tail >= L."""

    def remaining_frames_to_ffop(self, i: int) -> int:
        """ffop_frame - i (can be negative in tail)."""


def build_schedule(
    *,
    fps: Fraction,
    sample_rate: int,
    frames_per_block_override: int | None,
    lead_seconds: int,
    tail_frames: int,
    preroll_frames: int,
    beep_frames: int,
    persecond_beep: bool,   # if False, only the 2-pop beep is scheduled
    twopop: bool,           # if False, no 2-pop accent (and if persecond_beep False -> no beeps)
    count_style: str,
) -> Schedule:
    """Build the full schedule. Beep scheduling rules (SPEC §7.1):
       - per-second beep onset at the FIRST frame of each leader-second within the COUNTDOWN
         region: frames preroll + s*F for s in 0..L-1. (Only when persecond_beep True.)
       - 2-pop beep at frame i2 = ffop_frame - 2*F (== preroll + (L-2)*F), i.e. the '2' second.
         When twopop True. If persecond_beep True, the cue at i2 already exists; mark it
         is_twopop=True instead of duplicating. If persecond_beep False and twopop True, the only
         cue is the 2-pop at i2.
       - Each cue.sample = frame_start_sample(cue.frame, sr, fps).
       - flash_frames covers [frame, frame+beep_frames) for each cue, clipped to total_frames.
       - twopop_flash_frames covers the 2-pop cue's frame range only.
       Validate internally: if count_style=='smpte' require lead_seconds>=8 (raise ValueError;
       CLI/config should have caught it first)."""
```

Notes:
- `parse_fps` decimal handling: map exactly these decimal strings to broadcast rationals:
  `"23.976"`,`"23.98"` → 24000/1001; `"29.97"` → 30000/1001; `"59.94"` → 60000/1001;
  `"47.95"`/`"48"`... not required. Anything else decimal → `Fraction(str)`.
- Keep `Schedule` JSON-free and picklable (it will be passed to render workers).

---

## 2. `smpte_leader_gen/profiles.py`  (PURE)

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Profile:
    name: str
    video_codec: str               # e.g. "prores_ks", "ffv1", "libx264"
    video_opts: tuple[str, ...]    # codec-specific flags, e.g. ("-profile:v","3")
    pix_fmt: str                   # e.g. "yuv422p10le"
    container: str                 # default container ext WITHOUT dot: "mov"/"mkv"/"mp4"
    audio_codec: str               # "pcm_s24le" | "aac"
    audio_opts: tuple[str, ...]    # e.g. ("-b:a","320k") for aac, () for pcm
    allowed_containers: tuple[str, ...]
    note: str
    requires_even_dimensions: bool # True for yuv420p/yuv422p chroma-subsampled

PROFILES: dict[str, Profile]   # keys: "prores422hq","prores4444","ffv1","x264lossless","h264"

DEFAULT_PROFILE = "prores422hq"

def get_profile(name: str) -> Profile: ...   # KeyError-safe -> raise ValueError with valid list

def validate_profile_container(profile: Profile, container: str | None) -> str:
    """Return the effective container. If container is None -> profile.container.
       If container not in profile.allowed_containers -> ValueError with message."""
```

Profile table (implement exactly; audio_codec default pcm_s24le unless noted). Use `pcm_s16le`
only when bit_depth==16 — NOTE: audio_codec here is the *default* for the profile; the actual
PCM codec is chosen by encode.py from bit_depth (16->pcm_s16le, 24->pcm_s24le) EXCEPT for the
`h264` profile whose audio_codec is `aac` (still may be overridden to pcm by container). Encode.py
owns the final audio codec decision (see §6). Profiles just declare intent:

| name | video_codec | video_opts | pix_fmt | container | audio_codec | allowed | even |
|---|---|---|---|---|---|---|---|
| prores422hq | prores_ks | -profile:v 3 | yuv422p10le | mov | pcm_s24le | mov,mkv | True |
| prores4444 | prores_ks | -profile:v 4 | yuva444p10le | mov | pcm_s24le | mov,mkv | False |
| ffv1 | ffv1 | -level 3 | yuv422p10le | mkv | pcm_s24le | mkv | True |
| x264lossless | libx264 | -qp 0 | yuv444p | mkv | pcm_s24le | mkv,mov | False |
| h264 | libx264 | -crf 18 -preset slow | yuv420p | mp4 | aac | mp4,mkv | True |

(`requires_even_dimensions` True for 420/422 pix fmts.)

---

## 3. `smpte_leader_gen/config.py`  (uses pydantic v2, imports timing + profiles)

```python
from __future__ import annotations
from fractions import Fraction
from pathlib import Path
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

RESOLUTION_PRESETS: dict[str, tuple[int,int]]  # "720p":(1280,720),"1080p":(1920,1080),
                                               # "2kdci":(2048,1080),"uhd4k":(3840,2160)

def parse_resolution(value: str) -> tuple[int,int]:
    """Preset name or 'WxH' (also accept 'W*H','WXH'). Raise ValueError if not positive ints."""

def parse_color(value: str) -> tuple[float,float,float]:
    """'#RRGGBB' or '#RGB' (also without '#'). Return RGBf in [0,1]. Raise ValueError."""

class Config(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    # core
    lead_seconds: int = 8
    tail_seconds: float = 2.0
    preroll_seconds: float = 0.0
    fps: Fraction                      # already parsed Fraction (validator accepts str too)
    width: int
    height: int
    output: Path
    # encoding
    profile: str = "prores422hq"
    container: str | None = None       # effective container resolved post-init
    start_timecode: str = "00:00:00:00"
    no_drop_frame: bool = False
    # audio
    sample_rate: int = 48000
    bit_depth: int = 24                # 16 | 24
    beep_freq: float = 1000.0
    beep_frames: int = 1
    beep_level_dbfs: float = -12.0
    no_persecond_beep: bool = False
    no_twopop: bool = False
    no_beep: bool = False              # mute the whole beep track (keep metronome + flash)
    # metronome
    bpm: float | None = None
    time_sig: str = "4/4"
    subdivision: int = 1
    metronome_start: str = "0"         # "0" | "t0" | float seconds (as str)
    metronome_level_dbfs: float = -14.0
    metronome_accent_freq: float = 2000.0
    metronome_click_freq: float = 1500.0
    metronome_sub_freq: float = 1200.0
    # visual
    count_style: str = "full"          # "full" | "smpte"
    bg_color: tuple[float,float,float] = (0x20/255,)*3
    fg_color: tuple[float,float,float] = (0xE0/255,)*3
    accent_color: tuple[float,float,float] = (1.0, 0x30/255, 0x30/255)
    show_timecode_burnin: bool = False
    no_clock: bool = False
    no_counter: bool = False
    no_timer: bool = False
    no_flash: bool = False
    frames_per_block: int | None = None     # override F
    timer_format: str = "auto"              # "auto"|"seconds"|"frames"|"tc"
    seed: int = 0
    # runtime
    jobs: int = 1
    ffmpeg_path: str | None = None
    verbose: bool = False

    # ---- derived helpers (computed, not stored as separate fields unless via @computed_field) ----
    @property
    def F(self) -> int: ...                       # timing.frames_per_block(self.fps, self.frames_per_block)
    @property
    def tail_frames(self) -> int: ...             # round_half_up(self.tail_seconds * self.F)
    @property
    def preroll_frames(self) -> int: ...          # round_half_up(self.preroll_seconds * self.F)
    @property
    def time_sig_beats(self) -> int: ...          # numerator of time_sig
    @property
    def time_sig_value(self) -> int: ...          # denominator of time_sig

    # validators enforce SPEC §5 "MUST validate":
    #  - lead_seconds>=1; tail_seconds>=0; preroll_seconds>=0
    #  - sample_rate in a sane range (8000..192000)
    #  - bit_depth in {16,24}
    #  - beep_frames>=1; beep_freq>0
    #  - bpm None or 10<=bpm<=999
    #  - subdivision>=1
    #  - time_sig matches r"\d+/\d+" with positive ints
    #  - count_style in {"full","smpte"}; if "smpte" then lead_seconds>=8 (else ValueError)
    #  - timer_format in {"auto","seconds","frames","tc"}
    #  - profile in PROFILES; container (if set) in profile.allowed_containers
    #  - if profile.requires_even_dimensions: width%2==0 and height%2==0
    #  - width,height positive
    #  - jobs>=1

def build_config(**kwargs) -> Config:
    """Helper used by CLI: takes raw CLI values (strings for fps/resolution/colors),
       parses them, resolves container default from profile, returns a validated Config.
       Accepts fps as str/Fraction, resolution as str, colors as hex str."""

def load_config_file(path: Path) -> dict:
    """Load YAML or JSON (by extension) into a flat dict whose keys mirror CLI flag names with
       hyphens converted to underscores. Returns {} if path falsy. Used by CLI before applying
       explicit flags (explicit flags override file values)."""
```

Add `effective_container(config) -> str` if convenient, OR resolve container inside build_config
and store on `Config.container`. **Decision: build_config resolves and sets `Config.container`
to the effective container string (never None after build_config).**

---

## 4. `smpte_leader_gen/layout.py`  (PURE, no cairo/PIL; just geometry)

```python
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    cx: float            # width/2
    cy: float            # height/2
    unit: float          # min(width,height) — scale base
    clock_radius: float  # 0.40*unit
    clock_line_width: float
    crosshair_line_width: float
    number_font_px: int          # ~0.5*clock_radius -> int
    counter_font_px: int         # top-center frame counter x/N
    counter_baseline_y: float    # y position (top region)
    counter_center_x: float
    timer_font_px: int           # bottom-center
    timer_baseline_y: float
    timer_center_x: float
    number_center_x: float
    number_center_y: float       # vertically centered (== cy), drawn under wand
    flash_radius: float          # accent disc radius near clock edge
    flash_center_x: float        # default position: top-right area or clock-edge; see below
    flash_center_y: float
    twopop_flash_radius: float   # larger, for 2-pop accent
    burnin_font_px: int          # timecode burn-in (if enabled), top-left or near counter
    burnin_baseline_y: float
    burnin_center_x: float
    wand_line_width: float

def compute_layout(width: int, height: int) -> Layout:
    """All sizes scale from unit=min(W,H). Concrete defaults (tune for legibility, keep ratios):
       clock_radius   = 0.40*unit
       clock_line_width = max(2.0, 0.004*unit)
       wand_line_width  = max(2.0, 0.006*unit)
       crosshair_line_width = max(1.0, 0.0015*unit)
       number_font_px = round(0.5*clock_radius)
       counter_font_px = round(0.05*unit); counter at top-center, baseline ~0.08*height
       timer_font_px = round(0.05*unit); timer at bottom-center, baseline ~0.93*height
       flash_radius = 0.05*unit; flash_center at clock top-right edge:
          flash_center_x = cx + clock_radius*cos(-45deg-ish) ... DECISION: place flash disc at a
          FIXED corner = (width*0.85, height*0.5) (right-middle, clearly separate from clock) OR
          at clock edge. Choose: right-middle fixed: flash_center=(0.85*W, 0.5*H). twopop radius
          = 1.3*flash_radius. Keep deterministic.
       burnin near top-left: (0.16*W, 0.08*H)."""
```

Layout MUST be pure & unit-tested across the four resolution presets (ratios stable).

---

## 5. `smpte_leader_gen/render.py`  (cairo vectors + Pillow text; deterministic)

```python
from __future__ import annotations
import cairo
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from .config import Config
from .layout import Layout, compute_layout
from .timing import Schedule

FONT_REGULAR = ".../fonts/DejaVuSansMono.ttf"     # resolve via Path(__file__).parent
FONT_BOLD    = ".../fonts/DejaVuSansMono-Bold.ttf"

class FrameRenderer:
    def __init__(self, config: Config, schedule: Schedule) -> None:
        """Precompute layout, colors, fonts (PIL ImageFont at the needed sizes), and a CACHED
           static RGB background (numpy HxWx3 uint8) containing bg fill + crosshair + clock
           circle (everything constant across frames). Store as self._static (np.ndarray)."""

    def render_frame(self, i: int) -> bytes:
        """Return RGB24 packed bytes (length width*height*3) for global frame i. Steps:
           1. Start from a COPY of the cached static background (or redraw dynamic-only onto a
              fresh cairo surface composited over static — see method below).
           2. Draw dynamic vector elements with cairo on a transparent/over surface:
              - wipe sector (filled pie from 12 o'clock to wand angle) — only in countdown region
              - wand line (12 o'clock + clockwise by theta=2*pi*((i-preroll)%F)/F) — countdown only
              - flash disc if i in schedule.flash_frames (accent_color; larger/2nd color if i in
                twopop_flash_frames) — unless config.no_flash
              - dim flash outline when not flashing (so location is known) — optional but SHOULD
           3. Composite cairo dynamic layer over the static background into one RGB numpy array.
           4. Draw TEXT with Pillow on the RGB array:
              - big countdown number (schedule.number_at(i)) centered, bold font — unless no number
              - frame counter "j/(F-1)" top-center — unless config.no_counter
              - digital timer bottom-center — unless config.no_timer
              - timecode burn-in if config.show_timecode_burnin
           5. Return arr.tobytes() in RGB order.
        Preroll frames: bg + crosshair + clock only (no number/wand/wipe), counter+timer MAY show.
        Tail frames: pure black (SPEC §2 'short black tail') -> return all-zeros buffer of right
           size (still width*height*3). (Respect bg? SPEC says plain black tail -> black.)
        DETERMINISTIC: identical inputs -> identical bytes.
        """

    @staticmethod
    def _cairo_surface_to_rgb(surface: cairo.ImageSurface) -> np.ndarray:
        """cairo FORMAT_RGB24/ARGB32 stores premultiplied BGRA in little-endian. Convert to a
           contiguous HxWx3 uint8 RGB array: buf = np.ndarray((H,W,4),uint8, surface.get_data());
           rgb = buf[:,:,[2,1,0]] (BGR->RGB), ensure C-contiguous copy."""
```

Implementation guidance (the implementer chooses details but MUST meet the contract):
- Use `cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)`. Build the static layer once.
- For each frame, either (a) `surface.get_data()` copy + draw dynamic on a clone, or (b) keep a
  reusable surface, repaint static (blit cached) then dynamic. Must not leak state across frames.
- Timer text: use `schedule.remaining_frames_to_ffop(i)` and config.timer_format:
  - "auto": seconds = remaining_frames*den/num; format "S.fff" if lead_seconds<60 else "MM:SS.fff".
  - "seconds": always "S.fff" (or "MM:SS.fff" if >=60s).
  - "frames": show remaining_frames as integer.
  - "tc": SMPTE timecode of remaining time at fps (use a helper; OK to reuse encode.frames_to_tc).
  Clamp negative (tail) to 0 / 00:00:00:00.
- Big number font size = layout.number_font_px (bold). Counter/timer = regular font.
- Wand drawn ON TOP of the number (SPEC §6.3: number under the wand).
- Angle measured clockwise from straight up: in cairo screen coords (y down), straight up is
  angle -pi/2; clockwise increasing. A point on the circle at clock-fraction p in [0,1):
  theta = 2*pi*p; x = cx + R*sin(theta); y = cy - R*cos(theta). (p=0 -> top; p=0.25 -> right.)
- Wipe sector: filled path center->arc from top (p=0) to current p, slightly different shade
  (e.g. blend fg_color toward bg at ~25% — pick a fixed shade, document it).

`compute_layout` is called inside `__init__`; expose `self.layout`.

---

## 6. `smpte_leader_gen/audio.py`  (PURE synthesis + WAV writer; numpy + soundfile)

```python
from __future__ import annotations
import numpy as np
from .config import Config
from .timing import Schedule

FADE_MS = 1.0   # raised-cosine fade length at head & tail of each beep/click (document this)

def dbfs_to_amp(dbfs: float) -> float: ...   # 10**(dbfs/20)

def render_tone(freq: float, n_samples: int, sample_rate: int, *, fade_ms: float = FADE_MS,
                amp: float = 1.0) -> np.ndarray:
    """Mono float64 sine of length n_samples at freq, with a raised-cosine (hann-half) fade-in and
       fade-out of fade_ms each (clamped so 2*fade <= n_samples), scaled to peak `amp`. The first
       sample stays at the exact onset boundary (fade multiplies amplitude but index 0 is t=0).
       Returns shape (n_samples,)."""

def render_click(freq: float, sample_rate: int, *, amp: float = 1.0,
                 dur_ms: float = 6.0) -> np.ndarray:
    """Short percussive metronome click: windowed high sine (hann window over dur_ms) at freq,
       peak amp. Distinct timbre from the sustained beep. Returns mono float64."""

def synth_beep_track(schedule: Schedule, config: Config) -> np.ndarray:
    """Mono float64 length schedule.total_samples. For each cue in schedule.beep_cues, add a tone
       of beep_freq, length = round(beep_frames * sr * den/num) samples, peak amp from
       beep_level_dbfs, placed at cue.sample (add, clipping the tail at buffer end). Returns mono."""

def metronome_click_samples(schedule: Schedule, config: Config) -> list[tuple[int,str]]:
    """Return [(sample_index, role)] where role in {"accent","beat","sub"} for each click, using
       cumulative rounding so beats/subdivisions don't drift:
         period over a beat (quarter) in samples handled via:
         click_sample(k) = start_sample + round(k * sr * 60 / (bpm * subdivision))
       k runs from 0 while click_sample < total_samples. Determine role:
         - k % subdivision != 0 -> "sub"
         - beat_index = k // subdivision; beat_index % time_sig_beats == 0 -> "accent" else "beat"
       start_sample from config.metronome_start: "0"->0, "t0"->schedule.ffop_frame's sample
         (frame_start_sample(ffop_frame)), numeric seconds -> round(sec*sr).
       Empty list if config.bpm is None."""

def synth_metronome(schedule: Schedule, config: Config) -> np.ndarray:
    """Mono float64 length total_samples. For each (sample,role) from metronome_click_samples,
       add render_click at the role's freq (accent->metronome_accent_freq, beat->click_freq,
       sub->sub_freq), peak amp from metronome_level_dbfs. Returns mono (zeros if no bpm)."""

def build_audio(schedule: Schedule, config: Config) -> tuple[np.ndarray, float]:
    """Build the final STEREO buffer shape (total_samples, 2), both channels identical (centered).
       Steps: beep = synth_beep_track; metro = synth_metronome; mono = beep + metro.
       Peak safety (SPEC §7.3): if max(|mono|) > 10**(-1/20) (= -1 dBFS), scale whole mix down to
       -1 dBFS peak and emit a warning via `warnings.warn(...)` (pipeline logs it). Return
       (stereo_float64, applied_peak_dbfs). Length MUST equal schedule.total_samples exactly
       (zero-pad/truncate as needed)."""

def write_wav(path, buffer: np.ndarray, sample_rate: int, bit_depth: int) -> None:
    """Write stereo float buffer to WAV via soundfile with subtype PCM_24 (bit_depth==24) or
       PCM_16 (bit_depth==16). buffer is float in [-1,1]. path: str|Path."""
```

All audio functions PURE & deterministic; unit-tested without FFmpeg. Do NOT clip silently.

---

## 7. `smpte_leader_gen/encode.py`  (FFmpeg command construction + discovery)

```python
from __future__ import annotations
from fractions import Fraction
from pathlib import Path
from .config import Config
from .profiles import Profile, get_profile
from .timing import Schedule

class FFmpegError(RuntimeError): ...

def discover_ffmpeg(ffmpeg_path: str | None = None) -> tuple[str, str, tuple[int,int,int]]:
    """Locate ffmpeg & ffprobe. If ffmpeg_path given, derive ffprobe alongside it (same dir) or
       fall back to PATH 'ffprobe'. Run `ffmpeg -version`, parse the version (major,minor,patch).
       Require major>=6 (raise FFmpegError otherwise). Returns (ffmpeg, ffprobe, version)."""

def is_drop_frame(fps: Fraction, no_drop_frame: bool) -> bool:
    """True when fps in {30000/1001, 60000/1001} (29.97/59.94 families) and not no_drop_frame."""

def frames_to_tc(frame: int, fps: Fraction, drop_frame: bool) -> str:
    """Format an absolute frame count as SMPTE TC 'HH:MM:SS:FF' (non-drop) or 'HH:MM:SS;FF'
       (drop-frame, with the standard 29.97 drop-frame algorithm; for 59.94 drop 4 frames/min).
       Used for -timecode start (parse start_timecode->frames first) and timer 'tc' format."""

def parse_tc(tc: str, fps: Fraction) -> int:
    """Parse 'HH:MM:SS:FF' or 'HH:MM:SS;FF' to absolute frame count at fps (inverse of above)."""

def build_ffmpeg_command(
    *, ffmpeg: str, config: Config, schedule: Schedule, profile: Profile,
    audio_path: Path, output_path: Path,
) -> list[str]:
    """Construct the full argv list (no shell). Layout:
       [ffmpeg, '-y', '-hide_banner',
        '-f','rawvideo','-pix_fmt','rgb24','-s',f'{W}x{H}','-r',f'{num}/{den}','-i','-',
        '-i', str(audio_path),
        '-map','0:v:0','-map','1:a:0',
        '-c:v', profile.video_codec, *profile.video_opts, '-pix_fmt', profile.pix_fmt,
        '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',
        '-c:a', <audio codec>, *<audio opts>,
        '-timecode', <start tc string with ; if drop-frame>,
        '-r', f'{num}/{den}',
        str(output_path)]
       Audio codec selection:
         - if profile.audio_codec == 'aac': use 'aac' + ('-b:a','320k')  [h264 profile]
         - else PCM by bit_depth: 24->'pcm_s24le', 16->'pcm_s16le'.
       Start timecode: take config.start_timecode; if drop-frame, ensure ';' separator
         (convert frames<->tc via parse_tc/frames_to_tc to normalize)."""

def build_ffprobe_streams_cmd(ffprobe: str, path: Path) -> list[str]:
    """argv for: ffprobe -v error -show_streams -show_format -of json <path>  (used by tests)."""
```

Reject incompatible profile/container combos via config validation (already done) — encode trusts
config. `-timecode` must be placed as an output option (before output path) so it lands in the
container. For mp4/mov it attaches to the file; verify in integration tests.

---

## 8. `smpte_leader_gen/pipeline.py`  (orchestration; subprocess pipe)

```python
from __future__ import annotations
from pathlib import Path
from .config import Config
from .timing import build_schedule, Schedule
from .audio import build_audio, write_wav
from .encode import discover_ffmpeg, build_ffmpeg_command
from .profiles import get_profile
from .render import FrameRenderer

def build_schedule_from_config(config: Config) -> Schedule:
    """Adapter: call timing.build_schedule(...) pulling fields from config (F override, beep flags,
       persecond_beep = not config.no_persecond_beep, twopop = not config.no_twopop, etc.)."""

def run(config: Config, *, progress: bool = True) -> Path:
    """Full pipeline:
       1. discover ffmpeg (config.ffmpeg_path); if config.verbose print versions.
       2. schedule = build_schedule_from_config(config)
       3. stereo, peak_dbfs = build_audio(schedule, config); warn handled.
       4. write WAV to a NamedTemporaryFile (delete=False) in a temp dir.
       5. profile = get_profile(config.profile); cmd = build_ffmpeg_command(...).
       6. Popen(cmd, stdin=PIPE). Create FrameRenderer(config, schedule).
       7. For i in range(total_frames): write renderer.render_frame(i) to proc.stdin.
          Show a click.progressbar over total_frames when progress and not verbose-quiet.
          If config.jobs>1, use a ProcessPoolExecutor with an initializer building a per-worker
          FrameRenderer; map ordered over indices (e.g. executor.map(_worker, range(...),
          chunksize=...)) and write results in strict order. Provide a module-level worker fn +
          initializer using globals for picklability.
       8. Close stdin, wait; if returncode != 0 raise FFmpegError with captured stderr.
       9. Clean up temp WAV (finally). Return output_path.
    Must stream (never hold all frames). Deterministic frame order guaranteed even with jobs>1."""
```

Worker model for multiprocessing (module-level, picklable):
```python
_WORKER_RENDERER: FrameRenderer | None = None
def _init_worker(config: Config, schedule: Schedule) -> None: global _WORKER_RENDERER; ...
def _render_one(i: int) -> bytes: return _WORKER_RENDERER.render_frame(i)
```

---

## 9. `smpte_leader_gen/cli.py`  (click entrypoint)

```python
from __future__ import annotations
import click

@click.command(context_settings={"help_option_names":["-h","--help"]})
# ... all options from SPEC §5 with the exact flag names ...
def main(**kwargs): ...

# Behavior:
#  - read --config file first (load_config_file), then overlay explicit CLI flags
#    (only flags the user actually passed override file values; use click's
#    ctx.get_parameter_source to detect COMMANDLINE vs DEFAULT).
#  - build Config via config.build_config(...) (parses fps/resolution/colors, resolves container).
#  - call pipeline.run(config). Catch ValueError/FFmpegError -> click.echo(err, err=True);
#    raise SystemExit(2) with a clear message. --verbose prints ffmpeg version & resolved config.
#  - On success print output path + summary (duration, total_frames, fps, profile).
```

Console entrypoint name: `smpte-leader-gen` -> `smpte_leader_gen.cli:main`.

Flag list (exact names; map hyphens to underscores in the function via click auto): `--lead-seconds`,
`--tail-seconds`, `--preroll-seconds`, `--fps`, `--resolution`, `--output/-o` (required),
`--profile`, `--container`, `--start-timecode`, `--no-drop-frame`, `--sample-rate`, `--bit-depth`,
`--beep-freq`, `--beep-frames`, `--beep-level-dbfs`, `--no-persecond-beep`, `--no-twopop`, `--no-beep`, `--bpm`,
`--time-sig`, `--subdivision`, `--metronome-start`, `--metronome-level-dbfs`,
`--metronome-accent-freq`, `--metronome-click-freq`, `--metronome-sub-freq`, `--count-style`,
`--bg-color`, `--fg-color`, `--accent-color`, `--show-timecode-burnin`, `--no-clock`,
`--no-counter`, `--no-timer`, `--no-flash`, `--frames-per-block`, `--timer-format`, `--seed`,
`--jobs`, `--ffmpeg-path`, `--config`, `--verbose/-v`.

`--bit-depth` is a click.Choice(["16","24"]) -> int. `--profile` click.Choice(list(PROFILES)).
`--count-style` Choice(["full","smpte"]). `--timer-format` Choice(["auto","seconds","frames","tc"]).
`--container` Choice(["mov","mkv","mp4"]) default None.

---

## 10. `smpte_leader_gen/__init__.py`

```python
__version__ = "0.1.0"
# Re-export nothing heavy at import time (keep CLI startup fast). May expose __version__ only.
```

---

## 11. Tests (`tests/`) — pytest, no network

- `test_timing.py`: parse_fps presets/rational/decimal; frame_start_sample drift-free
  (assert frame_start_sample(total_frames)==total_samples; assert no accumulated drift vs naive);
  2-pop frame == ffop_frame-2*F and its sample == frame_start_sample(that); counter wrap for
  23.976/25/29.97/30/59.94 (frame 0 ->(0,F-1), frame F-1 ->(F-1,F-1), frame F ->(0,F-1) with
  number decremented); total_frames == L*F+tail(+preroll).
- `test_audio.py`: beep onsets land exactly at frame_start_sample (find first nonzero sample near
  onset == cue.sample, allowing the raised-cosine fade still starts AT the boundary so sample at
  cue.sample is ~0 but the next is nonzero — assert energy window starts at boundary; better:
  assert buffer[cue.sample : cue.sample+few] not all zero AND buffer[cue.sample-1] ~ 0 for
  isolated cues); total samples == round(total_frames*sr*den/num); mixed peak <= -1 dBFS.
- `test_layout.py`: compute_layout for the 4 presets; ratios (clock_radius/unit≈0.40), positive
  sizes, centered coords.
- `test_render.py`: golden hash for a fixed frame (the '2'/2-pop frame at 1080p/24). Compute hash
  once in-repo and assert stability across runs (mark xfail-tolerant note: hashes depend on
  cairo/Pillow versions — store the hash this environment produces). Also assert render_frame
  returns exactly W*H*3 bytes and tail frame is all-zero.
- `test_cli.py`: parsing/validation — invalid fps, odd dimensions with 420 profile, smpte with
  lead<8, bad bpm, prores in mp4 -> errors; valid build_config returns expected Fraction/res/colors.
- `test_integration.py`: SKIP if ffmpeg missing (shutil.which). Render a tiny 1s file per profile
  at low res (e.g. 128x72 even; for prores use 128x72) and ffprobe-assert r_frame_rate==num/den,
  nb_frames (or nb_read_frames via -count_frames) == total_frames, sample_rate, channels==2, and
  timecode present (drop-frame ';' for 29.97). Keep durations tiny (lead 2, tail 1).

Test helpers may import internals freely. Use `tmp_path` fixture for outputs.

---

## 12. `pyproject.toml`

- `[build-system]` setuptools.
- `[project]` name `smpte-leader-gen`, version 0.1.0, python `>=3.11`, dependencies:
  `numpy`, `pycairo`, `pillow`, `click>=8.1`, `pydantic>=2`, `soundfile`, `pyyaml`.
  optional `[project.optional-dependencies] dev = ["pytest"]`.
- `[project.scripts] smpte-leader-gen = "smpte_leader_gen.cli:main"`.
- include package data: `smpte_leader_gen/fonts/*.ttf` (setuptools `[tool.setuptools.package-data]`).
- Add a `Makefile`/note: `make verify` -> runs `pytest -q`.

---

## 13. Determinism & cross-cutting rules (RESTATE — all implementers obey)

1. No `random` / `time` in output-affecting paths (seed reserved; default deterministic).
2. All sample math via cumulative rounding (`frame_start_sample`), never float accumulation.
3. Leader-second = F frames (F = round(fps) unless overridden). Counter denominator = F-1.
4. 2-pop at `ffop_frame - 2*F`; flash frame == beep onset frame (same frame), enforced by sharing
   `schedule.flash_frames` derived from `beep_cues`.
5. Audio length == video length to within 1 sample (`total_samples == frame_start_sample(total_frames)`).
6. Pass exact rational to ffmpeg `-r` (`num/den`), never a decimal.
7. Black tail = literal black frames.
8. Colors RGBf internally; convert at the edges.
9. Every module: `from __future__ import annotations`, full type hints, docstrings on public API.
