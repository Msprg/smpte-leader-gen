# AV-Sync Countdown Leader Generator — Design & Implementation Specification

**Status:** Draft for implementation
**Audience:** The engineering agent implementing this tool. Read this document in full before writing code. Where this doc says *MUST*, treat it as a hard requirement; *SHOULD* is a strong default that may be exposed as a parameter; *MAY* is optional/future.

---

## 1. Purpose & Scope

Build a command-line tool that renders an **audio/video synchronization test pattern** inspired by the **SMPTE Universal Countdown Leader** ("the countdown clock"). It is used to verify A/V sync, frame timing, and audio alignment across a playback or editing pipeline.

It is **not** a strict reproduction of the SMPTE 301 leader. It is an *AV-sync test generator* that borrows the leader's visual language (rotating clock + sweep wand, big descending numbers, a single-frame reference beep) and adds:

- a running **visual countdown timer**,
- an on-screen **per-second frame counter** in `x/N` form (e.g. `7/23`),
- a **flashing circle** that flashes on the exact frame an audio cue fires,
- a **single-frame 1 kHz sync beep** every leader-second, preserving the classic **"2-pop"** accent,
- an **optional metronome** (with subdivisions and an accented downbeat), visualised
  by a top-center swinging-pendulum animation locked to the audio clicks.

Frame and sample timing **MUST** be accurate enough for fractional rates such as **23.976 fps (24000/1001)**. See §4.

---

## 2. Locked Decisions (from requirements gathering)

These were explicitly chosen by the product owner. Do not revisit without asking.

| Area | Decision |
|---|---|
| Language / stack | **Python 3.11+ CLI**, frames rendered in-process, audio synthesized in-process, **muxed via FFmpeg** (render-then-mux pipeline). |
| Output codec/container | **Configurable / multiple profiles** with a sensible default (see §8). |
| Visual style | **Authentic SMPTE-style rotating clock + sweep wand** PLUS a **numeric overlay**: digital countdown timer and the `x/N` frame counter. |
| Frame counter semantics | **Per-second, `0 .. N-1`**, where `N = round(fps)`. Displayed as `index/(N-1)` (e.g. `0/23 … 23/23` at ~24 fps; `x/24` at 25 fps; `x/29` at 30 fps). |
| Frame rates | **Full broadcast preset set AND arbitrary/rational input**: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60 as exact rationals, plus any user-supplied integer or `num/den` value. |
| Resolution | **Preset list + custom**, default **1920×1080**. Presets: 720p, 1080p, 2K DCI (2048×1080), 4K UHD (3840×2160). |
| Beep / flash cadence | **Per-second beep + flash, with the classic single 2-pop accent** at 2 leader-seconds before the end. |
| Metronome | **Off by default; enabled when `--bpm` is given.** Supports `--time-sig` (accented downbeat) and `--subdivision`. Distinct timbre from the sync beep. Visualised by a top-center swinging-pendulum animation locked to the clicks. |
| Audio defaults | **48 kHz / 24-bit / stereo PCM**, **1 kHz sine** sync beep. |
| Channel routing | **Mixed stereo, both cues centered** (summed L/R) by default. Isolation/panning is a future flag. |
| End behavior | **2-pop, reach t=0, then a short black tail.** `t=0` = first frame of picture (the leader's reference zero). |
| Timecode | **Embed SMPTE timecode metadata, starting `00:00:00:00`.** Auto-set drop-frame flag for 29.97/59.94. |

---

## 3. Conceptual Model & Terminology

- **fps (media timebase):** the exact rational frame rate the file is encoded at, `fps = num/den` (e.g. `24000/1001`). This governs real-time playback duration and **MUST** be exact.
- **F (nominal frames-per-second-block):** `F = round(fps)`. This is the integer used for: the `x/N` counter denominator (`N = F`, displayed max index `F-1`), the number of frames per "leader-second," and one full revolution of the sweep wand.
- **Leader-second:** a block of exactly **F frames**. The countdown clock, the big number, the per-second beep/flash, and the wand revolution are all defined in **frames**, not wall-clock seconds. This is intentional and matches how film leaders are defined (in frames). See §4.1 for the rationale and the real-time consequence.
- **t=0 / First Frame of Picture (FFOP):** the reference instant the whole leader counts down to — the frame immediately after the countdown reaches zero. All timing (especially the 2-pop) is measured backward from here.
- **2-pop:** the reference sync beep that fires at exactly **2 leader-seconds (2·F frames) before t=0**, coinciding with the big number "2". It is one of the per-second beeps but is the canonical alignment marker.

---

## 4. Timing — the most important part

### 4.1 Frame model

- Total countdown frames: `L · F`, where `L` = configured number of leader-seconds (`--lead-seconds`, default 8). Plus `tail_frames` of black (`--tail-seconds`, default 2 → `2·F` frames). Plus optional pre-roll (default 0).
- Frames are 0-indexed `i = 0 .. total-1`.
- Leader-second index of frame `i`: `s = i // F`.
- Per-second frame counter value: `j = i % F`, displayed as `j/(F-1)`.
- Countdown big number: `number = L - s` (counts `L → 1`), unless `--count-style smpte` is set (see §6.4).

**Why frame-centric (F frames per leader-second) instead of wall-clock seconds:**
At fractional rates the number of *encoded* frames in a true wall-clock second is non-integer (≈23.976 at 23.976 fps), which would make the `x/N` counter and the wand revolution irregular (occasionally 23, occasionally 24 frames). Defining the leader-second as exactly `F` frames keeps every on-screen cue clean and deterministic. The file is still encoded at the exact rational `fps`, so **real-time duration is correct**; a "leader-second" at 23.976 fps simply lasts `F/fps = 24·1001/24000 ≈ 1.00100 s` of real time. This is the correct trade-off for a frame-alignment tool and matches film-leader convention.
> Expose `--frames-per-block` to override `F` if a user ever wants a different block size. Default `F = round(fps)`.

### 4.2 Sample model (audio) — avoid drift

- Sample rate `sr` (default 48000). Audio is **one contiguous PCM buffer** for the whole output.
- **Per-frame sample boundary MUST be computed by cumulative rounding, not by accumulating a non-integer `samples_per_frame`:**
  `frame_start_sample(i) = round(i · sr · den / num)`
  This pins every frame's start to a sample with **zero accumulated drift** over the whole file.
- Total audio samples: `total_samples = round(total_frames · sr · den / num)`. The audio length **MUST** match the video duration to within 1 sample.
- A cue scheduled "on frame `i`" starts at `frame_start_sample(i)`.

### 4.3 Acceptance timing checks (see §11)

- `ffprobe` reports `r_frame_rate == num/den` exactly and `nb_frames == total_frames`.
- The 2-pop onset sample equals `frame_start_sample(total_frames - tail_frames - 2·F)`.
- Audio/video durations differ by < 1 sample / < 1 frame.

---

## 5. Inputs — CLI

Use **`click`** (or argparse) for the CLI and **pydantic v2** (or a validated dataclass) for the resolved config. Support a `--config FILE` (YAML/JSON) whose keys mirror the flags; explicit flags override file values.

### 5.1 Core
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--lead-seconds` | int | `8` | Number of leader-seconds counted down. |
| `--tail-seconds` | float | `2` | Black tail after t=0 (in leader-seconds; may be 0). |
| `--preroll-seconds` | float | `0` | Optional black/slate before the countdown. |
| `--fps` | str | `24000/1001` | Accepts presets (`23.976`,`24`,`25`,`29.97`,`30`,`50`,`59.94`,`60`), integers, or `num/den`. `23.976`→`24000/1001`, `29.97`→`30000/1001`, `59.94`→`60000/1001`. |
| `--resolution` | str | `1080p` | Preset (`720p`,`1080p`,`2kdci`,`uhd4k`) or `WxH`. |
| `--output` / `-o` | path | required | Output file; extension may select container (overridable by `--container`). |

### 5.2 Encoding
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--profile` | enum | `prores422hq` | See §8 for the profile table. |
| `--container` | enum | from profile | `mov`/`mkv`/`mp4`. |
| `--start-timecode` | str | `00:00:00:00` | Embedded. Drop-frame auto for 29.97/59.94 (`;` separator). |

### 5.3 Audio
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--sample-rate` | int | `48000` | |
| `--bit-depth` | enum | `24` | `16`/`24`. (`32f` MAY be added.) |
| `--beep-freq` | float | `1000` | Sync beep frequency (Hz). |
| `--beep-frames` | int | `1` | Beep length in frames. |
| `--beep-level-dbfs` | float | `-12` | Peak level of beep. |
| `--no-persecond-beep` | flag | off | If set, only the 2-pop fires (classic). |
| `--no-twopop` | flag | off | Disable the 2-pop accent. |

### 5.4 Metronome (audio clicks + top-center pendulum; enabled by `--bpm`)
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--bpm` | float | none | Presence enables the metronome. |
| `--time-sig` | str | `4/4` | `beats/value`; first beat of each bar is accented. |
| `--subdivision` | int | `1` | Clicks per beat (1=quarter, 2=eighth, 4=sixteenth…). |
| `--metronome-start` | enum/float | `0` | When clicks begin: `0` (start), `t0` (at FFOP), or seconds offset. |
| `--metronome-level-dbfs` | float | `-14` | |
| `--metronome-accent-freq` / `--metronome-click-freq` / `--metronome-sub-freq` | float | `2000`/`1500`/`1200` | Distinct from the 1 kHz beep. |

### 5.5 Visual toggles
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--count-style` | enum | `full` | `full` (L→1) or `smpte` (fixed 8→2). |
| `--bg-color` / `--fg-color` / `--accent-color` | hex | `#202020`/`#E0E0E0`/`#FF3030` | |
| `--show-timecode-burnin` | flag | off | On-screen TC overlay (separate from embedded TC). |
| `--no-clock` / `--no-counter` / `--no-timer` / `--no-flash` | flags | off | Hide individual elements. |
| `--seed` | int | `0` | Reserved for any future randomization; keep output deterministic. |

CLI **MUST** validate: positive durations, sane fps, resolution parity (even W/H for 4:2:0 codecs), bpm within (e.g.) 10–999, etc. Fail fast with clear messages.

---

## 6. Visual Design

Coordinate system origin top-left; center `C = (W/2, H/2)`. Use **anti-aliased vector drawing**. Recommended renderer: **`pycairo`** (crisp lines/arcs, fast) producing an RGB24 buffer per frame; **Pillow `ImageDraw`** is an acceptable fallback. Scale all sizes from `min(W,H)` so layouts hold across resolutions.

### 6.1 Background & registration
- Solid `--bg-color` (default dark gray `#202020`).
- Full-frame **crosshair**: thin vertical + horizontal lines through `C` in `--fg-color`.
- Optional safe-area rectangles (action/title) — MAY, off by default.

### 6.2 Clock + sweep wand (the SMPTE element)
- A **circle** centered at `C`, radius `R ≈ 0.40·min(W,H)`, stroked in `--fg-color`.
- A **sweep wand**: a radial line from `C` to the circle edge that rotates **once per leader-second**, i.e. one full revolution over `F` frames, **clockwise, starting at 12 o'clock**.
  - Wand angle for frame `i`: `θ = 2π · (i % F) / F`, measured clockwise from straight up.
  - SHOULD also render the classic **wipe/fill**: the swept sector (from 12 o'clock to the wand) filled with a slightly different shade, so the circle "fills up" each second. This is visually the signature of the leader; implement it as a filled pie/arc behind the wand line.
- The wand and wipe **MUST** reset cleanly at each leader-second boundary (frame `i % F == 0`).

### 6.3 Big countdown number
- The current `number` (see §4.1/§6.4) drawn large and centered (height ≈ `0.5·R`), in `--fg-color`, behind or over the clock per legibility (draw number first, wand over it, or vice versa — pick one and keep consistent; SHOULD draw number under the wand so the wand reads on top).
- Use a bundled monospaced/condensed font; **MUST** bundle the font file and reference it by path so output is deterministic across machines.

### 6.4 Counting behavior
- `--count-style full` (default): `number = L - (i // F)`, counting `L → 1`. The final leader-second shows `1`; then t=0 → black tail.
- `--count-style smpte`: classic fixed sequence `8,7,6,5,4,3,2` regardless of `L` (requires `L ≥ 8`; if `L < 8`, error or clamp with a warning). The "2" second carries the 2-pop.

### 6.5 Frame counter `x/N`
- Text `"{i % F}/{F-1}"` (e.g. `7/23`). Position: **top-center** by default, monospaced, `--fg-color`. Updates every frame.

### 6.6 Digital countdown timer
- Remaining time to t=0, default format `S.fff` seconds or `MM:SS.fff` if `L ≥ 60`. Remaining frames = `total_countdown_frames - i`; remaining seconds = `remaining_frames · den / num` (real time). Also expose `--timer-format frames|seconds|tc`. Position: bottom-center.

### 6.7 Flashing circle (audio-alignment marker)
- A distinct **filled disc/ring** (separate from the clock), default near the clock edge or a fixed corner, in `--accent-color`.
- It is **visible for exactly the frame(s) the sync beep is active** (default 1 frame) on every per-second beep and on the 2-pop; otherwise hidden (or drawn as a dim outline so its location is known). This is the visual half of the A/V alignment check — the flash and the beep onset **MUST** be on the same frame.
- The 2-pop frame MAY render the flash larger or in a second accent color to distinguish the reference.

### 6.8 Optional end slate
- The black tail is plain black by default. `--end-slate` (MAY) can render a final card with fps, resolution, duration, beep/metronome settings, and tool version — useful for archival identification.

---

## 7. Audio Design

All synthesis in NumPy float64, summed into a stereo buffer, peak-checked, then written to a temp WAV (`pcm_s24le` or `pcm_s16le`) for FFmpeg. Both cues centered (equal L/R) per §2.

### 7.1 Sync beep + 2-pop
- Waveform: sine at `--beep-freq` (1 kHz), length `--beep-frames` frames → `round(beep_frames · sr · den/num)` samples.
- Onset at `frame_start_sample(i)` for each per-second beep frame: the **first frame of each leader-second** (`i % F == 0`) within the countdown region. (If `--no-persecond-beep`, emit only the 2-pop.)
- **2-pop:** at frame `i_2 = total_countdown_frames - 2·F` (i.e. 2 leader-seconds before t=0, the "2" number). It coincides with a per-second beep; flag it so the flash can accent it. Skip if `--no-twopop`.
- Apply a **very short raised-cosine fade** (≈1 ms) at head/tail to prevent clicks **without** softening the perceived onset; keep the first sample at the exact boundary. Document the fade length.
- Level normalized to `--beep-level-dbfs` peak.

### 7.2 Metronome (only if `--bpm`)
- Beat period in samples: `beat_samples = sr · 60 / bpm` (use cumulative rounding per click: `click_sample(k) = round(start_sample + k · sr · 60 / bpm / subdivision)` so subdivisions and beats don't drift).
- For `time-sig = b/v`: every `b`-th beat is a **downbeat (accent)**. Subdivision clicks fall between beats.
- Click timbre: short percussive blip (a few ms) — e.g. a windowed high sine or filtered noise — at `--metronome-accent-freq` (downbeat), `--metronome-click-freq` (beat), `--metronome-sub-freq` (subdivision), all **distinct from 1 kHz** so the metronome is audibly separable from the sync beep.
- Start per `--metronome-start` (`0` = file start, `t0` = at FFOP, or seconds). Metronome MAY run through the black tail.
- Level `--metronome-level-dbfs`.
- **Visual:** a top-center inverted-pendulum animation, drawn over non-tail frames whenever `--bpm` is set. The arm angle is `swing · cos(π · beats_elapsed)` from vertical (one beat per half-swing; the bob is at an extreme on each beat), where `beats_elapsed = (frame_start_sample − start_sample) / beat_samples`. The bob flashes on beats (brighter, with a ring, on the downbeat) and the pivot pulses on every click — driven from the same click onsets as the audio so the visual stays locked to it.

### 7.3 Mix & safety
- Sum beep + metronome; if peak > -1 dBFS, scale down and log a warning (do not silently clip).
- Final buffer length = `total_samples` (§4.2), zero-padded as needed so audio duration == video duration.

---

## 8. Encoding / Muxing (FFmpeg)

Pipeline: Python writes **raw RGB24 frames to FFmpeg stdin** (`-f rawvideo -pix_fmt rgb24 -s WxH -r num/den -i -`) and passes the temp **WAV** as a second input. Avoid PNG temp sequences for speed. One FFmpeg invocation muxes both.

**MUST** pass the exact rational rate to `-r`/`-framerate` (e.g. `24000/1001`, never `23.98`). Set color metadata `-color_primaries bt709 -color_trc bt709 -colorspace bt709` (HD).

### 8.1 Profile table (`--profile`)
| Profile | Video | Pix fmt | Container | Audio | Notes |
|---|---|---|---|---|---|
| `prores422hq` *(default)* | `prores_ks -profile:v 3` | `yuv422p10le` | mov | `pcm_s24le` | Editorial standard, intra-frame. |
| `prores4444` | `prores_ks -profile:v 4` | `yuva444p10le` | mov | `pcm_s24le` | If alpha / higher quality needed. |
| `ffv1` | `ffv1 -level 3` | `yuv422p10le` | mkv | `pcm_s24le` | Mathematically lossless, archival. |
| `x264lossless` | `libx264 -qp 0` | `yuv444p` | mkv | `pcm_s24le` | Lossless, smaller than FFV1 sometimes. |
| `h264` | `libx264 -crf 18 -preset slow` | `yuv420p` | mp4 | `aac 320k` or `pcm` | Max compatibility; note inter-frame slightly softens single-frame markers. |

Expose `--profile` and let `--container` override the extension when sensible. Reject incompatible combos (e.g. prores in mp4) with a clear error.

### 8.2 Timecode
- Pass `-timecode <start>`; for 29.97/59.94 default to **drop-frame** (`HH:MM:SS;FF`) unless `--no-drop-frame`. Verify it lands in the container with `ffprobe`.

### 8.3 FFmpeg discovery & version
- Locate `ffmpeg`/`ffprobe` on PATH; allow `--ffmpeg-path`. **MUST** require FFmpeg ≥ 6.0 (for stable `prores_ks`, rawvideo piping, timecode). Print the detected version in `--verbose`.

---

## 9. Architecture / Module Layout

```
smpte_leader_gen/
  __init__.py
  cli.py            # click entrypoint, arg parsing → Config
  config.py         # pydantic Config model, fps/resolution parsing, validation
  timing.py         # rational fps, frame↔sample math (cumulative rounding), cue schedule
  layout.py         # element positions/sizes derived from W,H (pure, testable)
  render.py         # per-frame drawing (pycairo) → RGB24 bytes; clock/wand/number/counter/timer/flash
  audio.py          # beep, 2-pop, metronome synthesis → stereo PCM buffer → temp WAV
  encode.py         # FFmpeg command construction (profiles, timecode, color), subprocess pipe
  pipeline.py       # orchestrates: build schedule → stream frames to ffmpeg → write audio → mux
  fonts/            # bundled deterministic font(s)
  profiles.py       # the §8.1 profile table
tests/
  test_timing.py    # drift-free sample math, 2-pop position, counts
  test_audio.py     # onset alignment, durations, no clipping
  test_layout.py    # element placement across resolutions
  test_render.py    # golden-frame hashes for fixed inputs
  test_cli.py       # parsing/validation
  test_integration.py  # tiny end-to-end render + ffprobe assertions (skipped if no ffmpeg)
pyproject.toml      # deps, console_scripts entrypoint
README.md
```

### 9.1 Key dependencies
`pycairo` (or Pillow), `numpy`, `click`, `pydantic>=2`, `soundfile` (WAV I/O). FFmpeg is an **external runtime dependency** (not pip). Pin versions in `pyproject.toml`.

### 9.2 Design rules
- `timing.py`, `layout.py`, `audio.py` synthesis **MUST** be pure/deterministic and unit-tested without FFmpeg.
- Streaming render: do not hold all frames in memory — generate frame `i`, write bytes to the ffmpeg pipe, discard. For speed, frame rendering MAY use a `multiprocessing` pool that returns ordered RGB buffers; keep ordering strict.
- All randomness gated by `--seed`; default output fully deterministic (same inputs → byte-identical frames, given same font/cairo version).

---

## 10. Performance Notes

- 1080p prores of an 8s leader at 24 fps ≈ ~192 frames — trivial. 4K at 60 fps for long leads is the heavy case; vectorized cairo + multiprocessing keeps it reasonable.
- Reuse static layers: the background + crosshair + clock circle are constant; cache them and composite only the moving wand/number/counter/flash per frame.
- Show a progress bar (frames encoded / total) in the CLI.

---

## 11. Verification & Acceptance Criteria

The implementation is **done** when:

1. **Timing math (unit):**
   - `total_frames == L·F + tail_frames (+ preroll)`.
   - `frame_start_sample(i) == round(i·sr·den/num)`; no drift at i = total (assert exact).
   - 2-pop sample == `frame_start_sample(total_countdown_frames - 2·F)`.
2. **Audio (unit):** beep onsets land exactly on their frame boundaries; total audio samples == `round(total_frames·sr·den/num)`; mixed peak ≤ -1 dBFS (no clip).
3. **Counter/number (unit):** for 23.976, frame 0→`0/23`, frame 23→`23/23`, frame 24→`0/23` with number decremented; verify across 25/29.97/30/59.94.
4. **Render (golden):** stable hash for a fixed frame (e.g. the "2"/2-pop frame at 1080p/24).
5. **Integration (needs ffmpeg):** produce a 2s test file per profile; `ffprobe` asserts `r_frame_rate`, `nb_frames`, sample rate, channel count, and embedded timecode (incl. drop-frame for 29.97).
6. **Manual editorial check (documented in README):** import a 23.976 ProRes output into an NLE; confirm the flash frame and beep onset coincide, and the 2-pop sits on the "2" frame exactly 2 leader-seconds before t=0.

Provide a `make verify` / `nox` / `pytest` target. Integration tests **MUST** skip gracefully when FFmpeg is absent.

---

## 12. Recommended Development & Test OS Environment

**Primary recommendation: Linux — Ubuntu 24.04 LTS** (22.04 LTS also fine).

Rationale: FFmpeg codec availability (`prores_ks`, `ffv1`, `libx264`) and behavior are most consistent on Linux, it matches CI, and it avoids Windows path/codec/encoding quirks. The code itself is cross-platform (pure Python + FFmpeg) and will still run on Windows/macOS — but **develop and test on Linux** for reproducible output.

Because the owner's workstation is Windows 11, the most practical setup is:

- **WSL2 with Ubuntu 24.04 LTS** as the dev/test environment (near-native speed, real apt FFmpeg, a true Linux shell), while editing from VS Code on Windows via the WSL remote. This satisfies "develop on Linux" without leaving the machine.
- Equivalent alternatives: a native Ubuntu install / VM, or macOS (Homebrew FFmpeg). Avoid making Windows-native the *primary* test target.

Concrete environment:
- Python **3.11+** in a `venv` (or `uv`/`poetry`); pinned `requirements`/`pyproject`.
- **FFmpeg ≥ 6.0** from apt or a static build (`ffmpeg -version` must show `prores_ks`, `libx264`, `ffv1`). Pin/record the version — encoder defaults can shift output.
- System libs for cairo: `libcairo2`, `pkg-config`, build tools (for `pycairo` wheels/builds).
- Quick playback verification: **`ffplay` / `mpv`** for spot checks; **DaVinci Resolve** (free) or Premiere for the editorial A/V-sync confirmation in §11.6.
- **CI:** GitHub Actions on `ubuntu-latest`, `apt-get install ffmpeg`, run unit + integration tests. Pin the FFmpeg version where possible for golden-file stability.

---

## 13. Open Items / Assumptions to Confirm With Owner

These were defaulted to keep momentum; flag them in the first implementation PR:

1. **Leader-second = F frames** model (§4.1) — confirm the frame-centric definition (vs strict wall-clock seconds) is acceptable. *(Recommended as specified.)*
2. **Default count style = `full` (L→1)** vs SMPTE's fixed 8→2. *(`--count-style smpte` provided either way.)*
3. **`--lead-seconds` default = 8**, `--tail-seconds` default = 2.
4. Exact **on-screen positions** of counter/timer/flash (top-center / bottom-center / clock-edge as drafted) — easy to tweak after a first visual.
5. **Metronome click timbre** (synth blip as specced) — confirm it's distinct enough; a sampled click could be added later.
6. Whether an **`--end-slate`** ID card is wanted now or deferred.

---

*End of specification.*
