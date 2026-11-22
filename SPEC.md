# AV-Sync Countdown Leader Generator — Design & Implementation Specification

**Status:** Implemented (v0.1.0). This spec was written before implementation and has since been revised to describe what the code actually does. Where the two disagree, treat the code as authoritative and correct the spec.
**Audience:** Engineers working on this tool. Where this doc says *MUST*, treat it as a hard requirement; *SHOULD* is a strong default that may be exposed as a parameter; *MAY* is optional/future.

---

## 1. Purpose & Scope

Build a command-line tool that renders an **audio/video synchronization test pattern** inspired by the **SMPTE Universal Countdown Leader** ("the countdown clock"). It is used to verify A/V sync, frame timing, and audio alignment across a playback or editing pipeline.

It is **not** a strict reproduction of the SMPTE 301 leader. It is an *AV-sync test generator* that borrows the leader's visual language (rotating clock + sweep wand, big descending numbers, a single-frame reference beep) and adds:

- a running **visual countdown timer**,
- an on-screen **per-second frame counter**: the zero-padded frame index within the leader-second over the actual fps (e.g. `07/23.976`, `07/25`),
- a **flashing circle** that lights on the exact frame each sync beep starts,
- a **single-frame 1 kHz sync beep** every leader-second; the beep on the "2" is the classic **"2-pop"**, accented on screen by a larger flash,
- a bottom **audio ruler**: per-frame ticks labelled in ms of A/V offset, with a marker that crosses 0 on each beep,
- **registration geometry**: a full-frame border, a centred rectangle with the frame's aspect ratio, corner-to-corner diagonals and a centre crosshair,
- an **optional metronome** (with subdivisions and an accented downbeat), visualised
  by a top-center animation locked to the audio clicks — a row of beat dots by
  default, or a swinging pendulum — plus a left-side readout of the bar length and
  the nudge needed to land butt-joined music on a downbeat,
- an optional **end-on-the-grid tail** (`--end-align`), so music butt-joined to the end of the leader continues the click grid.

Frame and sample timing **MUST** be accurate enough for fractional rates such as **23.976 fps (24000/1001)**. See §4.

---

## 2. Locked Decisions (from requirements gathering)

These were explicitly chosen by the product owner. Do not revisit without asking.

| Area | Decision |
|---|---|
| Language / stack | **Python 3.11+ CLI**, frames rendered in-process, audio synthesized in-process, **muxed via FFmpeg** (render-then-mux pipeline). |
| Output codec/container | **Configurable / multiple profiles** with a sensible default (see §8). |
| Visual style | **Authentic SMPTE-style rotating clock + sweep wand** PLUS a **numeric overlay**: digital countdown timer and the per-second frame counter. |
| Frame counter semantics | **Per-second, `0 .. F-1`**, where `F` = fps rounded half-up. Displayed as the zero-padded index over the exact fps as a decimal (e.g. `00/23.976 … 23/23.976`; `00/25 … 24/25`; `00/29.97 … 29/29.97`). *(Originally `index/(N-1)`; changed so rates that differ only by a fraction stay distinguishable.)* |
| Frame rates | **Full broadcast preset set AND arbitrary/rational input**: 23.976, 24, 25, 29.97, 30, 50, 59.94, 60 as exact rationals, plus any user-supplied integer or `num/den` value. |
| Resolution | **Preset list + custom**, default **1920×1080**. Presets: 720p, 1080p, 2K DCI (2048×1080), 4K UHD (3840×2160). |
| Beep / flash cadence | **Per-second beep + flash, with the classic single 2-pop accent** at 2 leader-seconds before the end. |
| Metronome | **Off by default; enabled when `--bpm` is given.** Supports `--time-sig` (accented downbeat) and `--subdivision`. Distinct timbre from the sync beep. Visualised by a top-center animation locked to the clicks: a row of beat dots by default, or a swinging pendulum (`--metronome-style`). |
| Audio defaults | **48 kHz / 24-bit / stereo PCM**, **1 kHz sine** sync beep. |
| Channel routing | **Mixed stereo, both cues centered** (summed L/R) by default. Isolation/panning is a future flag. |
| End behavior | **2-pop, reach t=0, then a short black tail.** `t=0` = first frame of picture (the leader's reference zero). The tail MAY be lengthened by `--end-align` (§5.4). |
| Timecode | **Embed SMPTE timecode metadata, starting `00:00:00:00`.** Auto-set drop-frame flag for 29.97/59.94. |

---

## 3. Conceptual Model & Terminology

- **fps (media timebase):** the exact rational frame rate the file is encoded at, `fps = num/den` (e.g. `24000/1001`). This governs real-time playback duration and **MUST** be exact.
- **F (nominal frames-per-second-block):** `F` = fps rounded half-up (`--frames-per-block` overrides it). This is the integer used for: the counter's index range `0 .. F-1` (`F-1` also sets its zero-padding width), the number of frames per "leader-second," and one full revolution of the sweep wand.
- **Leader-second:** a block of exactly **F frames**. The countdown clock, the big number, the per-second beep/flash, and the wand revolution are all defined in **frames**, not wall-clock seconds. This is intentional and matches how film leaders are defined (in frames). See §4.1 for the rationale and the real-time consequence.
- **t=0 / First Frame of Picture (FFOP):** the reference instant the whole leader counts down to — the frame immediately after the countdown reaches zero. All timing (especially the 2-pop) is measured backward from here.
- **2-pop:** the reference sync beep that fires at exactly **2 leader-seconds (2·F frames) before t=0**, coinciding with the big number "2". It is one of the per-second beeps but is the canonical alignment marker.

---

## 4. Timing — the most important part

### 4.1 Frame model

- Total frames: `P + L·F + tail_frames`.
  - `P` = pre-roll frames = `round_half_up(preroll_seconds · F)` (default 0), placed before the countdown.
  - `L` = number of leader-seconds (`--lead-seconds`, default 8), so the countdown is `L·F` frames.
  - `tail_frames = round_half_up(tail_seconds · F)` of black (`--tail-seconds`, default 2 → `2·F`), lengthened by `--end-align` (§5.4).
- Frames are 0-indexed `i = 0 .. total-1`. `ffop_frame = P + L·F`.
- Leader-second index of frame `i`: `s = (i - P) // F` (negative in the pre-roll, `≥ L` in the tail).
- Per-second frame counter value: `j = (i - P) % F` (pre-roll frames use `i % F`), shown as `j` zero-padded to the width of `F-1`, then `/`, then the exact fps as a trimmed decimal (e.g. `07/23.976`, `07/24`).
- Countdown big number: `number = L - s` for `P ≤ i < P + L·F` (counts `L → 1`), unless `--count-style smpte` is set (see §6.4).

**Why frame-centric (F frames per leader-second) instead of wall-clock seconds:**
At fractional rates the number of *encoded* frames in a true wall-clock second is non-integer (≈23.976 at 23.976 fps), which would make the frame counter and the wand revolution irregular (occasionally 23, occasionally 24 frames). Defining the leader-second as exactly `F` frames keeps every on-screen cue clean and deterministic. The file is still encoded at the exact rational `fps`, so **real-time duration is correct**; a "leader-second" at 23.976 fps simply lasts `F/fps = 24·1001/24000 ≈ 1.00100 s` of real time. This is the correct trade-off for a frame-alignment tool and matches film-leader convention.
> `--frames-per-block` overrides `F` for a different block size. Default: fps rounded half-up (12.5 fps → 13). The counter's denominator stays the actual fps, so with an override the numerator can exceed it (e.g. `29/24` with `--fps 24 --frames-per-block 30`).

### 4.2 Sample model (audio) — avoid drift

- Sample rate `sr` (default 48000). Audio is **one contiguous PCM buffer** for the whole output.
- **Per-frame sample boundary MUST be computed by cumulative rounding, not by accumulating a non-integer `samples_per_frame`:**
  `frame_start_sample(i) = round(i · sr · den / num)`
  This pins every frame's start to a sample with **zero accumulated drift** over the whole file.
- Total audio samples: `total_samples = round(total_frames · sr · den / num)`. The audio length **MUST** match the video duration to within 1 sample.
- A cue scheduled "on frame `i`" starts at `frame_start_sample(i)`.

### 4.3 Acceptance timing checks (see §11)

- `ffprobe` reports `r_frame_rate == num/den` exactly (always in MOV/MP4; Matroska stores an integer-nanosecond frame duration, so 59.94 in MKV reads approximately, e.g. `19001/317`) and a frame count == `total_frames` (`nb_frames`, or `-count_frames` for MKV).
- The 2-pop onset sample equals `frame_start_sample(total_frames - tail_frames - 2·F)`.
- The WAV holds exactly `total_samples`. In the container, audio and video durations differ by < 1 frame.

---

## 5. Inputs — CLI

The CLI uses **`click`**, and the resolved config is a **pydantic v2** model. `--config FILE` (YAML/JSON) supplies baseline values whose keys mirror the flags (hyphens or underscores); explicit flags override file values. `--output` MUST still be given on the command line (it is a required click option, so an `output` key in the file never takes effect). Unknown keys in the file are ignored silently.

### 5.1 Core
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--lead-seconds` | int | `8` | Number of leader-seconds counted down (≥ 1). |
| `--tail-seconds` | float | `2` | Black tail after t=0 (in leader-seconds; may be 0). |
| `--preroll-seconds` | float | `0` | Pre-roll before the countdown, in leader-seconds (`round_half_up(s·F)` frames). Shows the static background, ruler scale, left readout, counter, timer, burn-in and metronome animation; no number, wand, wipe, flash or ruler marker. |
| `--fps` | str | `24000/1001` | Accepts presets (`23.976`,`24`,`25`,`29.97`,`30`,`50`,`59.94`,`60`), integers, `num/den`, or other decimals. The exact strings `23.976`/`23.98`→`24000/1001`, `29.97`→`30000/1001`, `59.94`→`60000/1001`; any other decimal is taken as its exact fraction (`12.5`→`25/2`, `29.970`→`2997/100`). |
| `--resolution` | str | `1080p` | Preset (`720p`,`1080p`,`2kdci`,`uhd4k`) or `WxH`. |
| `--output` / `-o` | path | required | Output file. A `.mov`/`.mkv`/`.mp4` extension selects the container unless `--container` is given (§8.1). |

### 5.2 Encoding
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--profile` | enum | `prores422hq` | See §8 for the profile table. |
| `--container` | enum | from extension, else profile | `mov`/`mkv`/`mp4`; must be allowed by the profile. |
| `--start-timecode` | str | `00:00:00:00` | Embedded start timecode, `HH:MM:SS:FF` or `HH:MM:SS;FF`. It is parsed to a frame count (`;` = drop-frame, `:` = non-drop) and re-printed with the rate's separator, so at 29.97/59.94 a `:` value is re-labelled (`01:00:00:00` → `01:00:03;18` at 29.97, `01:00:03;36` at 59.94). Field ranges are not checked. |
| `--no-drop-frame` | flag | off | Disable the automatic drop-frame timecode at 29.97/59.94. |

### 5.3 Audio
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--sample-rate` | int | `48000` | 8000–192000. |
| `--bit-depth` | enum | `24` | `16`/`24`. (`32f` MAY be added.) |
| `--beep-freq` | float | `1000` | Sync beep frequency (Hz, > 0). |
| `--beep-frames` | int | `1` | Beep length in frames (≥ 1). |
| `--beep-level-dbfs` | float | `-12` | Beep peak before the master gain (§7.3); the output peak can be up to +6.02 dB higher. |
| `--no-persecond-beep` | flag | off | If set, only the 2-pop fires (classic). |
| `--no-twopop` | flag | off | Disable the 2-pop accent. |
| `--no-beep` | flag | off | Mute the whole beep track (per-second + 2-pop); the metronome and the visual flash are unaffected. |

### 5.4 Metronome (audio clicks + top-center animation; enabled by `--bpm`)
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--bpm` | float | none | Presence enables the metronome (10–999). |
| `--time-sig` | str | `4/4` | `beats/value`; `beats` sets the bar length, the accented downbeat and the number of dots. `value` is validated but does not affect timing: `--bpm` counts the `beats` whatever the note value. |
| `--subdivision` | int | `1` | Clicks per beat (≥ 1; e.g. 2 = two clicks per beat). |
| `--metronome-start` | enum/float | `0` | When clicks begin: `0` (file start), `t0` (at FFOP), or an offset in real (wall-clock) seconds from file start. |
| `--metronome-level-dbfs` | float | `-14` | Click peak before the master gain (§7.3). |
| `--metronome-accent-freq` / `--metronome-click-freq` / `--metronome-sub-freq` | float | `2000`/`1500`/`1200` | Distinct from the 1 kHz beep. |
| `--metronome-style` | enum | `dots` | Top-center visual: `dots` (row of beat lights) or `pendulum` (swinging arm). |
| `--end-align` | enum | none | `beat`/`bar`/`subdivision`; requires `--bpm`. Lengthens the black tail (never shortens it) so the total frame count lands on the next metronome grid point at or after the nominal end, counted from the metronome start. Exact when the unit spans whole frames, else within half a frame. Clicks at or after that boundary are dropped, so the file ends silently on it. |
| `--end-cue` | flag | off | Requires `--end-align`. Keeps the boundary click and adds `max(2, ceil(6 ms · fps) + 1)` frames so it sounds in full before the file ends. |

### 5.5 Visual toggles
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--count-style` | enum | `full` | `full` (L→1) or `smpte` (fixed 8→2, §6.4). |
| `--bg-color` / `--fg-color` / `--accent-color` | hex | `#202020`/`#E0E0E0`/`#FF3030` | `#RRGGBB` or `#RGB`. The left readout (§7.2) is a fixed turquoise. |
| `--show-timecode-burnin` | flag | off | On-screen timecode centred below the clock (§6.5). |
| `--no-clock` / `--no-counter` / `--no-timer` / `--no-flash` | flags | off | Hide individual elements. |
| `--frames-per-block` | int | none | Override `F` (≥ 1). Default: fps rounded half-up. |
| `--timer-format` | enum | `auto` | `auto`/`seconds`/`frames`/`tc` (§6.6). |
| `--seed` | int | `0` | Reserved for any future randomization; output is deterministic. |

### 5.6 Runtime
| Flag | Type | Default | Notes |
|---|---|---|---|
| `--jobs` | int | `1` | Parallel render workers (≥ 1); frame order stays strict. |
| `--ffmpeg-path` | path | none | Explicit `ffmpeg`; `ffprobe` is looked up alongside it. |
| `--config` | path | none | YAML/JSON baseline config (see above). |
| `--verbose` / `-v` | flag | off | Print the FFmpeg version and the resolved config; hides the progress bar. |
| `--version` / `-h`, `--help` | flag | | Print the version / usage. |

The CLI validates, failing fast with a clear `Error: …` message and exit status 2:
- `--lead-seconds ≥ 1`; tail and pre-roll `≥ 0`; fps `> 0` (no other bound).
- `--sample-rate` 8000–192000; `--bit-depth` 16/24; `--beep-frames ≥ 1`; `--beep-freq > 0`.
- `--bpm` 10–999; `--subdivision ≥ 1`; `--time-sig` `N/M` with both positive.
- `--start-timecode` matches `HH:MM:SS[:;]FF`; `--metronome-start` is `0`, `t0` or a number.
- `--frames-per-block ≥ 1`; `--jobs ≥ 1`.
- `--count-style smpte` needs `L ≥ 8`; the 2-pop needs `L ≥ 2` unless `--no-twopop`.
- `--end-align` needs `--bpm`; `--end-cue` needs `--end-align`.
- The container (explicit, or from the output extension) must be allowed by the profile.
- Width **and** height must be even for the chroma-subsampled profiles `prores422hq`, `ffv1` (4:2:2) and `h264` (4:2:0).

---

## 6. Visual Design

Coordinate system origin top-left; center `C = (W/2, H/2)`; `U = min(W,H)`. Drawing is **anti-aliased**: **pycairo** draws all vector elements into an RGB24 buffer per frame, and **Pillow** draws all text with the bundled fonts. Sizes scale from `U` (or from `W`/`H` where stated) so layouts hold across resolutions.

### 6.1 Background & registration
- Solid `--bg-color` (default dark gray `#202020`).
- Registration geometry in `--fg-color` on every non-tail frame (no flag hides it):
  - a crosshair through `C` and corner-to-corner diagonals, at 50% opacity, `max(1, 0.0015·U)` px;
  - an opaque full-frame border whose outer edge sits on the frame edge;
  - an opaque centred rectangle at 66% of W and H (so it shares the frame's aspect ratio and its corners lie on the diagonals), `max(2, 0.003·U)` px.
- Safe-area rectangles (action/title) are not implemented (MAY, future).

### 6.2 Clock + sweep wand (the SMPTE element)
- A **circle** centered at `C`, radius `R = 0.22·U`, stroked in `--fg-color` at `max(2, 0.004·U)` px.
- A **sweep wand**: a radial line from `C` to the circle edge that rotates **once per leader-second**, i.e. one full revolution over `F` frames, **clockwise, starting at 12 o'clock**.
  - Wand angle for frame `i`: `θ = 2π · ((i - P) % F) / F`, measured clockwise from straight up.
  - The classic **wipe/fill** is drawn: the swept sector (from 12 o'clock to the wand) is filled with a shade 25% of the way from the background to the foreground, behind the wand line, so the circle "fills up" each second.
- The wand and wipe **MUST** reset cleanly at each leader-second boundary.
- The circle is part of the static layer and also shows in the pre-roll; the wand and wipe are drawn only on countdown frames.

### 6.3 Big countdown number
- The current `number` (see §4.1/§6.4) drawn centred at `C` in bundled DejaVu Sans Mono Bold at font size `round(0.5·R)` px, in `--fg-color`.
- The number is drawn **over** the wand (all text is drawn after the vector layer), so the glyph covers the wand where they cross.
- The fonts are bundled in the package and referenced by path, so output is deterministic across machines.

### 6.4 Counting behavior
- `--count-style full` (default): `number = L - s`, counting `L → 1`. The final leader-second shows `1`; then t=0 → black tail.
- `--count-style smpte`: fixed `8 … 2` on the seven leader-seconds before the last one, regardless of `L`. `L < 8` is an error. Seconds before the `8` (when `L > 8`) and the final second show no number; clock, counter, timer, flash and per-second beeps still run. The "2" second carries the 2-pop.

### 6.5 Frame counter and timecode burn-in
- **Counter:** text `f"{j:0{w}d}/{fps}"`, where `w = len(str(F-1))` and `fps` is the exact rate as a trimmed decimal (integer rates print without a fraction), e.g. `07/23.976`, `07/24`, `07/29.97`. Position: **top-left**, left-anchored at `(0.06·W, 0.085·H)`, regular monospace at `0.045·U` px, `--fg-color`. Updates every frame, including the pre-roll; not drawn in the tail.
- **Burn-in** (`--show-timecode-burnin`): centred at `(W/2, H/2 + R + 0.035·H)`, below the clock. It shows the absolute frame index from the start of the file as non-drop `HH:MM:SS:FF`, with `FF` counted at `round(fps)`. It ignores `--start-timecode` and drop-frame, so it can differ from the embedded timecode.

### 6.6 Digital countdown timer
- Remaining time to t=0: frames = `ffop_frame - i` (includes any pre-roll), clamped ≥ 0; seconds = `frames · den / num` (real time).
- `--timer-format`:
  - `auto` (default) and `seconds`: both show `S.fff`, or `MM:SS.fff` when `L ≥ 60` (they currently behave the same);
  - `frames`: the remaining frame count;
  - `tc`: the remaining time as non-drop `HH:MM:SS:FF`, with `FF` counted at `round(fps)`.
- Position: **top-right**, right-anchored at `(0.94·W, 0.085·H)`, regular monospace at `0.045·U` px.

### 6.7 Flashing circle (audio-alignment marker)
- A filled disc in `--accent-color`, radius `0.05·U`, fixed at `(0.90·W, 0.50·H)`.
- It is filled on exactly the frames `[cue.frame, cue.frame + beep_frames)` of every per-second beep and the 2-pop, also when `--no-beep` mutes the audio. Metronome clicks do not light it. This is the visual half of the A/V alignment check — the flash and the beep onset **MUST** be on the same frame.
- Other countdown frames show a dim outline (accent at 30% over the background) so its location is known. The pre-roll shows neither.
- The 2-pop flash is the same colour at 1.3× the radius.

### 6.8 Black tail
- The tail is pure black (RGB 0,0,0, not `--bg-color`) with no overlays, starting at the FFOP frame. The only exception is the flash disc of a multi-frame beep that is still active after t=0.
- `--end-slate` (a final ID card with fps, resolution, duration, beep/metronome settings and tool version) is not implemented (MAY, future).

### 6.9 Audio ruler
- A scale at `y = 0.90·H` from `0.10·W` to `0.90·W` on every non-tail frame (no flag hides it), with `F + 1` per-frame ticks for offsets `-F//2 … F - F//2`. Offset 0 is the per-second beep frame, drawn as an accent-coloured centre tick. Every 5th tick is taller.
- Major ticks and the two ticks flanking 0 are labelled in signed ms, `round(offset · 1000 · den / num)` (e.g. `+42`); the centre is labelled `0` in the accent colour.
- On countdown frames an accent-coloured downward triangle marks the current frame's offset from the beep (`j` if `j ≤ F/2`, else `j - F`), so it crosses 0 on each beep frame. The pre-roll shows the scale without the marker.

---

## 7. Audio Design

All synthesis in NumPy float64. The beep and metronome tracks are summed to mono, passed through the master gain (§7.3), duplicated to equal L/R (both cues centered per §2), and written to a temp WAV (`pcm_s24le` or `pcm_s16le`) for FFmpeg.

### 7.1 Sync beep + 2-pop
- Waveform: sine at `--beep-freq` (1 kHz), length `--beep-frames` frames → `round(beep_frames · sr · den/num)` samples.
- Onset at `frame_start_sample(i)` for each per-second beep frame: the **first frame of each leader-second** (`(i - P) % F == 0`) within the countdown region. (If `--no-persecond-beep`, emit only the 2-pop.)
- **2-pop:** at frame `i_2 = ffop_frame - 2·F = P + L·F - 2·F` (i.e. 2 leader-seconds before t=0, the "2" number). It coincides with a per-second beep and sounds identical to it; it is flagged so the flash accents it (§6.7). Skip if `--no-twopop`.
- **`--no-beep`** silences the entire beep track (per-second **and** 2-pop) — useful to hear the metronome alone. Only the audio is muted; the schedule's beep frames (and hence the visual flash) are unchanged.
- Apply a **very short raised-cosine fade** (1 ms, `FADE_MS` in `audio.py`) at head/tail to prevent clicks **without** softening the perceived onset; keep the first sample at the exact boundary.
- Tone amplitude = `--beep-level-dbfs` as a peak, before the master gain (§7.3).

### 7.2 Metronome (only if `--bpm`)
- Beat period in samples: `beat_samples = sr · 60 / bpm` (use cumulative rounding per click: `click_sample(k) = round(start_sample + k · sr · 60 / bpm / subdivision)` so subdivisions and beats don't drift).
- For `time-sig = b/v`: every `b`-th beat is a **downbeat (accent)**. Subdivision clicks fall between beats.
- Click timbre: a 6 ms (`METRONOME_CLICK_MS`) sine blip under a full Hann window, at `--metronome-accent-freq` (downbeat), `--metronome-click-freq` (beat), `--metronome-sub-freq` (subdivision), all **distinct from 1 kHz** so the metronome is audibly separable from the sync beep. Only the time signature's `beats` affects timing; `value` is not used.
- Start per `--metronome-start` (`0` = file start, `t0` = at FFOP, or real seconds from file start). Clicks run through the black tail to the end of the file; with `--end-align` and no `--end-cue`, clicks at or after the aligned boundary are dropped.
- Click amplitude = `--metronome-level-dbfs` as a peak, before the master gain (§7.3).
- **Visual:** a top-center animation, drawn on pre-roll and countdown frames whenever `--bpm` is set (not in the black tail, even where clicks continue), in the style chosen by `--metronome-style`. Both styles flash from the same click onsets as the audio so the visual stays locked to it.
  - **`dots`** (default): a horizontal row of beat lights, one per beat of the bar (`time_sig` numerator), at the top readout level (between the corner frame-counter and timer). Each beat lights its dot toward the accent colour and it fades over a short fixed window (~0.12 s, capped to the onset interval) so each dot reads as a crisp flash, not a smear; the bar's downbeat is the larger **accent** dot. When `--subdivision > 1`, a second, smaller row just below tracks the subdivision **within the current beat**: every click lights the sub-pulse dot at `k mod subdivision` (the beat itself is sub-pulse 0) — so every audible click is represented (beats on top, sub-pulses below).
  - **`pendulum`**: an inverted-pendulum arm at angle `swing · cos(π · beats_elapsed)` from vertical (one beat per half-swing; the bob is at an extreme on each beat), where `beats_elapsed` is pinned to the beat-onset frames. The bob lights toward the accent colour on beats (larger, with a ring, on the downbeat) and the pivot pulses on every click (so subdivisions read as a finer tick).
- **Left readout** (whenever `--bpm` is set): static bold monospace text in a fixed turquoise `#40E0D0` (independent of the colour flags), left-aligned at `x = 0.0125·W` with the first line at `0.52·H`, `0.020·U` px, line pitch `0.024·H`, on every non-tail frame. It shows:
  - a header `{bpm} BPM  {time_sig}` (plus `  sub N` when subdividing);
  - the bar length as `bar = X.XX f` / `/ X.X ms` / `/ N smp`;
  - the nudge = `total_samples` minus the nearest bar-downbeat sample (counted from the metronome start, always bar-based, whatever `--end-align` unit is used). It reads `nudge leader RIGHT` or `nudge leader LEFT` followed by the size in f / ms / smp, or `seam aligned on the` / `bar downbeat (0 smp)` when the nudge is 0. With `--end-cue` it includes the ring-out frames added past the boundary.

### 7.3 Mix & safety
- Sum beep + metronome, then apply one master gain to the whole file: `g = min(2.0, 10^(-1/20) / peak)`. That is up to +6.02 dB of boost, capped so the output peak never exceeds -1 dBFS. If the raw mix is already above -1 dBFS, `g < 1` and a warning is printed (no silent clipping). With the defaults the beep alone peaks at about -6 dBFS.
- Final buffer length = `total_samples` (§4.2), zero-padded as needed so audio duration == video duration.

---

## 8. Encoding / Muxing (FFmpeg)

Pipeline: Python writes **raw RGB24 frames to FFmpeg stdin** (`-f rawvideo -pix_fmt rgb24 -s WxH -r num/den -i -`) and passes the temp **WAV** as a second input. Avoid PNG temp sequences for speed. One FFmpeg invocation muxes both.

**MUST** pass the exact rational rate to `-r`/`-framerate` (e.g. `24000/1001`, never `23.98`). Every output, at any resolution, is tagged `-color_primaries bt709 -color_trc bt709 -colorspace bt709`. (No conversion matrix is set, so FFmpeg's default RGB→YUV conversion applies.)

### 8.1 Profile table (`--profile`)
| Profile | Video | Pix fmt | Default container | Allowed containers | Audio | Notes |
|---|---|---|---|---|---|---|
| `prores422hq` *(default)* | `prores_ks -profile:v 3` | `yuv422p10le` | mov | mov, mkv | `pcm_s24le`/`pcm_s16le` | Editorial standard, intra-frame. |
| `prores4444` | `prores_ks -profile:v 4` | `yuva444p10le` | mov | mov, mkv | `pcm_s24le`/`pcm_s16le` | If alpha / higher quality needed. |
| `ffv1` | `ffv1 -level 3` | `yuv422p10le` | mkv | mkv | `pcm_s24le`/`pcm_s16le` | Mathematically lossless, archival. |
| `x264lossless` | `libx264 -qp 0` | `yuv444p` | mkv | mkv, mov | `pcm_s24le`/`pcm_s16le` | Lossless, sometimes smaller than FFV1. Uses x264's default long GOP (not intra-only). |
| `h264` | `libx264 -crf 18 -preset slow` | `yuv420p` | mp4 | mp4, mkv | `aac` 320k (always) | Max compatibility; inter-frame coding slightly softens single-frame markers. |

PCM profiles follow `--bit-depth` (24 → `pcm_s24le`, 16 → `pcm_s16le`). Container precedence: `--container` > a `.mov`/`.mkv`/`.mp4` output extension > the profile default; any other extension silently gets the profile default. The resolved container is forced with `-f`, so it governs the muxer regardless of the extension. A container the profile does not allow is rejected before rendering.

### 8.2 Timecode
- Pass `-timecode <start>`; for 29.97/59.94 default to **drop-frame** (`HH:MM:SS;FF`) unless `--no-drop-frame`. Drop-frame applies only to the exact rationals `30000/1001` and `60000/1001`. The start value is round-tripped through a frame count (§5.2). Verify it lands in the container with `ffprobe`.

### 8.3 FFmpeg discovery & version
- Locate `ffmpeg`/`ffprobe` on PATH; allow `--ffmpeg-path`. **MUST** require FFmpeg ≥ 6.0 (for stable `prores_ks`, rawvideo piping, timecode). A version string that cannot be parsed (e.g. git snapshot builds reporting `ffmpeg version N-…`) is also rejected. Print the detected version in `--verbose`.

---

## 9. Architecture / Module Layout

```
smpte_leader_gen/
  __init__.py
  cli.py            # click entrypoint, arg parsing → Config
  config.py         # pydantic Config model, fps/resolution parsing, validation
  timing.py         # rational fps, frame↔sample math (cumulative rounding), cue schedule
  layout.py         # element positions/sizes derived from W,H (pure, testable)
  render.py         # per-frame drawing (pycairo vectors + Pillow text) → RGB24 bytes
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
  test_config.py    # config parsing and validators
  test_profiles.py  # profile table and container rules
  test_encode.py    # FFmpeg command construction, timecode
  test_pipeline.py  # config→schedule adapter, worker plumbing, tiny end-to-end render
  test_review_fixes.py  # regression tests pinning confirmed review fixes
  test_integration.py  # tiny end-to-end render + ffprobe assertions (skipped if no ffmpeg)
scripts/
  acceptance.py     # heavier end-to-end ffprobe acceptance run (make acceptance)
pyproject.toml      # deps, console_scripts entrypoint
README.md
```

### 9.1 Key dependencies
`pycairo` (vectors), `pillow` (text), `numpy`, `click`, `pydantic>=2`, `soundfile` (WAV I/O), `pyyaml` (`--config`). FFmpeg is an **external runtime dependency** (not pip). `pyproject.toml` sets minimum versions.

### 9.2 Design rules
- `timing.py`, `layout.py`, `audio.py` synthesis **MUST** be pure/deterministic and unit-tested without FFmpeg.
- Streaming render: do not hold all frames in memory — generate frame `i`, write bytes to the ffmpeg pipe, discard. For speed, frame rendering MAY use a `multiprocessing` pool that returns ordered RGB buffers; keep ordering strict.
- All randomness gated by `--seed`; default output fully deterministic (same inputs → byte-identical frames, given same font/cairo version).

---

## 10. Performance Notes

- 1080p prores of an 8s leader at 24 fps ≈ ~192 frames — trivial. 4K at 60 fps for long leads is the heavy case; vectorized cairo + multiprocessing keeps it reasonable.
- Reuse static layers: the background, registration geometry, clock circle, ruler scale and labels, and the left readout are constant; they are cached and only the moving elements are drawn per frame.
- Show a progress bar (frames encoded / total) in the CLI; it is hidden under `--verbose`.

---

## 11. Verification & Acceptance Criteria

The implementation is **done** when:

1. **Timing math (unit):**
   - `total_frames == P + L·F + tail_frames`.
   - `frame_start_sample(i) == round(i·sr·den/num)`; no drift at i = total (assert exact).
   - 2-pop sample == `frame_start_sample(P + L·F - 2·F)`.
2. **Audio (unit):** beep onsets land exactly on their frame boundaries; total audio samples == `round(total_frames·sr·den/num)`; mixed peak ≤ -1 dBFS (no clip).
3. **Counter/number (unit):** for 23.976, frame 0→`00/23.976`, frame 23→`23/23.976`, frame 24→`00/23.976` with the number decremented (`counter_at` returns `(j, F-1)`); verify across 25/29.97/30/59.94.
4. **Render (golden):** stable hash for a fixed frame (e.g. the "2"/2-pop frame at 1080p/24).
5. **Integration (needs ffmpeg):**
   - pytest renders tiny files (128×72, 2 lead + 1 tail leader-seconds) for `ffv1`, `h264` and `prores422hq`, plus 23.976 and 29.97 drop-frame cases.
   - `make acceptance` (`scripts/acceptance.py`) renders all five profiles at 24 fps, plus 23.976, 25, 29.97 (DF and NDF) and 59.94 (MOV exact, MKV approximate) on `ffv1`/`prores422hq`, plus metronome feature combinations.
   - Both assert with `ffprobe`: `r_frame_rate`, frame count, sample rate, channel count and embedded timecode (incl. drop-frame for 29.97), with the 59.94-in-MKV rate caveat from §4.3.
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
4. Exact **on-screen positions** of counter/timer/flash. *(Resolved: counter top-left, timer top-right, flash at the right edge on the centre line, burn-in below the clock; see §6.)*
5. **Metronome click timbre** (synth blip as specced) — confirm it's distinct enough; a sampled click could be added later.
6. Whether an **`--end-slate`** ID card is wanted now or deferred. *(Deferred: not implemented.)*

---

*End of specification.*
