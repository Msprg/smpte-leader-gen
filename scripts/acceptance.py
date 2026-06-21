#!/usr/bin/env python3
"""End-to-end acceptance harness for the §11 criteria. Renders real files via the public
pipeline and asserts ffprobe-reported stream/format properties. Not part of the pytest suite
(it is heavier); run with the venv interpreter."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

from smpte_leader_gen.config import build_config
from smpte_leader_gen.pipeline import build_schedule_from_config, run

FAILS: list[str] = []
OKS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (OKS if cond else FAILS).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}{(' :: ' + detail) if detail else ''}")


def ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-count_frames", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def all_tags(meta: dict) -> dict:
    """Case-folded merge of format-level + per-stream tags."""
    tags: dict[str, str] = {}
    for k, v in (meta.get("format", {}).get("tags", {}) or {}).items():
        tags[k.lower()] = v
    for s in meta.get("streams", []):
        for k, v in (s.get("tags", {}) or {}).items():
            tags[k.lower()] = v
    return tags


def run_case(tmp: Path, *, label: str, profile: str, fps: str, ext: str,
             resolution: str = "128x72", lead: int = 2, tail: float = 1.0,
             expect_fps: Fraction, expect_df: bool = False, extra: dict | None = None,
             fps_exact: bool = True) -> None:
    print(f"\n=== {label} (profile={profile}, fps={fps}, {resolution}) ===")
    out = tmp / f"{label}.{ext}"
    kw = dict(profile=profile, fps=fps, resolution=resolution,
              lead_seconds=lead, tail_seconds=tail, output=out)
    if extra:
        kw.update(extra)
    cfg = build_config(**kw)
    sched = build_schedule_from_config(cfg)
    run(cfg, progress=False)

    check(f"{label}: file exists & non-empty", out.exists() and out.stat().st_size > 0,
          f"{out.stat().st_size if out.exists() else 0} bytes")

    meta = ffprobe(out)
    vstreams = [s for s in meta["streams"] if s["codec_type"] == "video"]
    astreams = [s for s in meta["streams"] if s["codec_type"] == "audio"]
    check(f"{label}: 1 video + 1 audio stream", len(vstreams) == 1 and len(astreams) == 1,
          f"v={len(vstreams)} a={len(astreams)}")
    v, a = vstreams[0], astreams[0]

    # r_frame_rate. mov/mp4 store exact rational timebases; Matroska re-derives r_frame_rate
    # from an integer-nanosecond DefaultDuration, which cannot represent 1001/60000 s exactly,
    # so ffprobe reports a near-approximation for 59.94 in MKV. We pass the exact rational to
    # `-r` regardless; for MKV+59.94 we assert closeness instead of bit-exactness.
    rfr = Fraction(v["r_frame_rate"])
    if fps_exact:
        check(f"{label}: r_frame_rate == {expect_fps}", rfr == expect_fps, f"got {v['r_frame_rate']}")
    else:
        err = abs(float(rfr) - float(expect_fps))
        check(f"{label}: r_frame_rate ~= {float(expect_fps):.5f} (MKV ns-precision)",
              err < 0.01, f"got {v['r_frame_rate']} (err {err:.2e})")

    # frame count
    nbf = v.get("nb_frames") or v.get("nb_read_frames")
    check(f"{label}: frame count == {sched.total_frames}", int(nbf) == sched.total_frames,
          f"got {nbf}")

    # audio
    check(f"{label}: sample_rate == {cfg.sample_rate}", int(a["sample_rate"]) == cfg.sample_rate,
          f"got {a['sample_rate']}")
    check(f"{label}: channels == 2", int(a["channels"]) == 2, f"got {a['channels']}")

    # audio/video duration parity (< 1 frame and < 1 sample of the ideal)
    vdur = float(v.get("duration") or meta["format"]["duration"])
    adur = float(a.get("duration") or meta["format"]["duration"])
    ideal = sched.total_frames * expect_fps.denominator / expect_fps.numerator
    frame_dur = expect_fps.denominator / expect_fps.numerator
    check(f"{label}: video duration ~ ideal (<1 frame)", abs(vdur - ideal) < frame_dur,
          f"v={vdur:.6f} ideal={ideal:.6f}")
    check(f"{label}: audio vs video duration (<1 frame)", abs(adur - vdur) < frame_dur,
          f"a={adur:.6f} v={vdur:.6f}")

    # embedded timecode
    tags = all_tags(meta)
    tc = tags.get("timecode")
    check(f"{label}: embedded timecode present", tc is not None, f"got {tc!r}")
    if tc is not None:
        if expect_df:
            check(f"{label}: drop-frame timecode (';')", ";" in tc, f"got {tc!r}")
        else:
            check(f"{label}: non-drop timecode (':')", ";" not in tc, f"got {tc!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        F1001_24 = Fraction(24000, 1001)
        F1001_30 = Fraction(30000, 1001)
        F1001_60 = Fraction(60000, 1001)

        # All five profiles at 24 fps.
        run_case(tmp, label="prores422hq", profile="prores422hq", fps="24", ext="mov",
                 expect_fps=Fraction(24))
        run_case(tmp, label="prores4444", profile="prores4444", fps="24", ext="mov",
                 resolution="127x71", expect_fps=Fraction(24))  # odd dims allowed (444)
        run_case(tmp, label="ffv1", profile="ffv1", fps="24", ext="mkv",
                 expect_fps=Fraction(24))
        run_case(tmp, label="x264lossless", profile="x264lossless", fps="24", ext="mkv",
                 resolution="127x71", expect_fps=Fraction(24))
        run_case(tmp, label="h264", profile="h264", fps="24", ext="mp4",
                 expect_fps=Fraction(24))

        # Fractional + drop-frame families (use ffv1 for speed).
        run_case(tmp, label="fps_23976", profile="ffv1", fps="23.976", ext="mkv",
                 expect_fps=F1001_24)
        run_case(tmp, label="fps_2997_df", profile="ffv1", fps="29.97", ext="mkv",
                 expect_fps=F1001_30, expect_df=True)
        # 59.94 exactness is checked on mov (exact rational timebase) ...
        run_case(tmp, label="fps_5994_df_mov", profile="prores422hq", fps="59.94", ext="mov",
                 expect_fps=F1001_60, expect_df=True)
        # ... and MKV's ns-precision approximation is verified within tolerance (informational).
        run_case(tmp, label="fps_5994_mkv", profile="ffv1", fps="59.94", ext="mkv",
                 expect_fps=F1001_60, expect_df=True, fps_exact=False)
        run_case(tmp, label="fps_2997_ndf", profile="ffv1", fps="29.97", ext="mkv",
                 expect_fps=F1001_30, expect_df=False, extra={"no_drop_frame": True})
        run_case(tmp, label="fps_25", profile="ffv1", fps="25", ext="mkv",
                 expect_fps=Fraction(25))

        # Feature combos.
        run_case(tmp, label="metronome", profile="ffv1", fps="24", ext="mkv",
                 expect_fps=Fraction(24), extra={"bpm": 120, "subdivision": 2, "time_sig": "3/4"})
        run_case(tmp, label="smpte_style", profile="ffv1", fps="24", ext="mkv",
                 lead=8, tail=1.0, expect_fps=Fraction(24), extra={"count_style": "smpte"})
        run_case(tmp, label="classic_2pop_only", profile="ffv1", fps="24", ext="mkv",
                 expect_fps=Fraction(24), extra={"no_persecond_beep": True})
        run_case(tmp, label="bitdepth16", profile="ffv1", fps="24", ext="mkv",
                 expect_fps=Fraction(24), extra={"bit_depth": 16})
        run_case(tmp, label="jobs2_parallel", profile="ffv1", fps="24", ext="mkv",
                 expect_fps=Fraction(24), extra={"jobs": 2})

    print(f"\n================ {len(OKS)} checks passed, {len(FAILS)} failed ================")
    if FAILS:
        print("FAILURES:")
        for f in FAILS:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
