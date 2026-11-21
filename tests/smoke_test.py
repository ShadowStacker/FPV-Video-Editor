"""
End-to-end self-test for FPV DVR Stitcher.

Builds a fake DVR session the way an analog receiver records it (MJPEG .AVI,
640x480, 27.25 fps, 8 kHz mono audio, split into numbered clips) containing
static, flights, a brief dropout and a battery swap. Then it runs detection
and export and checks that:
  * the static is found at the right times and the right parts are cut,
  * the exported video contains exactly the kept frames, byte-for-byte
    identical to the originals (no re-encoding),
  * MKV export gets one chapter per flight and the report is written.

Needs ffmpeg + ffprobe (next to dvr_core.py, in ./ffmpeg, or on the PATH).
Run:  python tests/smoke_test.py [folder-to-keep-the-fake-clips-in]
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import dvr_core as dc  # noqa: E402

FPS = "109/4"   # 27.25 fps, like the real receiver
# (kind, seconds) laid end to end
PLAN = [("snow", 6), ("good", 10), ("snow", 1.5), ("good", 5), ("snow", 9), ("good", 8), ("snow", 4)]
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


def ff(*args: str) -> None:
    subprocess.run([dc.tool("ffmpeg"), "-v", "error", "-nostdin", "-y", *args], check=True)


def make_session(d: str) -> list[str]:
    parts = []
    for i, (kind, sec) in enumerate(PLAN):
        p = os.path.join(d, f"part{i:02d}.avi")
        if kind == "snow":   # harsh black/white grain, new every frame, no colour - like real static
            src = (f"color=c=0x808080:s=320x240:r={FPS},noise=c0s=100:c0f=t+u,"
                   "lutyuv=y='if(gt(val,128),235,16)':u=128:v=128,scale=640:480:flags=neighbor")
        else:                # moving, coloured picture
            src = f"testsrc2=s=640x480:r={FPS}"
        ff("-f", "lavfi", "-i", src, "-t", str(sec), "-pix_fmt", "yuvj420p",
           "-c:v", "mjpeg", "-q:v", "3", p)
        parts.append(p)
    lst = os.path.join(d, "parts.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write(f"file '{p.replace(os.sep, '/')}'\n")
    clips = os.path.join(d, "clips")
    os.makedirs(clips)
    ff("-f", "concat", "-safe", "0", "-i", lst, "-f", "lavfi", "-i", "anoisesrc=r=8000:a=0.03",
       "-map", "0:v", "-map", "1:a", "-shortest", "-c:v", "copy", "-c:a", "pcm_s16le", "-ac", "1",
       "-f", "segment", "-segment_time", "12", "-reset_timestamps", "1",
       os.path.join(clips, "VID%05d.AVI"))
    return sorted((os.path.join(clips, f) for f in os.listdir(clips)), key=dc.natural_key)


def packet_hashes(path: str) -> list[str]:
    out = subprocess.run([dc.tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0",
                          "-c", "copy", "-f", "framemd5", "-"], capture_output=True, text=True, check=True).stdout
    return [ln.split(",")[-1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


def main() -> int:
    print(f"ffmpeg:  {dc.tool('ffmpeg')}\nffprobe: {dc.tool('ffprobe')}")
    with tempfile.TemporaryDirectory(prefix="dvr_smoke_") as d:
        print("Building fake DVR session...")
        files = make_session(d)
        print(f"  {len(files)} clips: " + ", ".join(os.path.basename(f) for f in files))
        if len(sys.argv) > 1:        # keep a copy of the fake clips (used to test the built .exe)
            os.makedirs(sys.argv[1], exist_ok=True)
            for f in files:
                shutil.copy2(f, sys.argv[1])

        print("Analysing...")
        s = dc.analyze_files(files)
        print(dc.summary_table(s))

        # ---- detection -----------------------------------------------------
        truth, t = [], 0.0
        for kind, sec in PLAN:
            truth.append((kind, t, t + sec))
            t += sec
        check(len(s.segments) == len(PLAN), f"{len(PLAN)} segments found (got {len(s.segments)})")
        if len(s.segments) == len(PLAN):
            for sg, (kind, t0, t1) in zip(s.segments, truth):
                want = "nosignal" if kind == "snow" else "signal"
                check(sg.kind == want and abs(sg.t0 - t0) < 0.25 and abs(sg.t1 - t1) < 0.25,
                      f"{want:8} {t0:5.1f}-{t1:5.1f}s detected as {sg.kind} {sg.t0:5.2f}-{sg.t1:5.2f}s")
            actions = [sg.action for sg in s.segments]
            check(actions == ["cut", "keep", "keep", "keep", "cut", "keep", "cut"],
                  f"keep/cut decisions {actions}")
            check(sum(1 for sg in s.segments if sg.kind == "signal" and "(continued)" not in sg.label) == 2,
                  "two flights (the 1.5 s dropout does not start a new flight)")
        check(not any(sg.warn for sg in s.segments), "no false crash warnings on clean power-offs")

        # ---- lossless export -------------------------------------------------
        keep = s.keep_mask()
        src = {f: packet_hashes(f) for f in files}
        expected = [src[s.clips[s.clip_idx[g]].path][s.frame_idx[g]] for g in range(s.n) if keep[g]]
        for ext in (".avi", ".mkv"):
            out = os.path.join(d, "stitched" + ext)
            print(f"Exporting {ext}...")
            dc.export_video(s, out)
            got = packet_hashes(out)
            check(got == expected, f"{ext}: {len(got)} frames, byte-identical to the {len(expected)} kept source frames")
        chap = subprocess.run([dc.tool("ffprobe"), "-v", "error", "-show_chapters", "-of", "csv=p=0",
                               os.path.join(d, "stitched.mkv")], capture_output=True, text=True).stdout
        check(len([c for c in chap.splitlines() if c.strip()]) == 2, "MKV has 2 flight chapters")

        print("Writing report...")
        rep = dc.write_report(s, os.path.join(d, "stitched.avi"))
        check(os.path.isfile(rep) and os.path.getsize(rep) > 500, "signal-loss report written")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
