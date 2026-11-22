"""
End-to-end self-test for FPV DVR Stitcher.

Builds a fake DVR session the way an analog receiver records it (MJPEG .AVI,
640x480, 27.25 fps, 8 kHz mono audio, split into numbered clips) containing
static, flights with an OSD, a brief dropout, battery swaps, and a Betaflight-style
post-flight STATS screen before two of the three power-offs. Then it checks that:
  * the static and STATS screens are found at the right times and cut correctly,
  * chunked (parallel) analysis gives exactly the same result as one pass,
  * the exported video contains exactly the kept frames, byte-for-byte
    identical to the originals (no re-encoding),
  * MKV export gets one chapter per flight and the report is written,
  * the diagnostics zip is complete and its sample frames are exact copies,
  * "Enhance for YouTube" produces a video of the right size and length
    (and, off Windows, that the AI path works with stand-in AI tools).

Needs ffmpeg + ffprobe (next to dvr_core.py, in ./ffmpeg, or on the PATH).
Run:  python tests/smoke_test.py [folder-to-keep-the-fake-clips-in]
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import numpy as np  # noqa: E402

import dvr_core as dc  # noqa: E402
import enhance as en  # noqa: E402

FPS = "109/4"   # 27.25 fps, like the real receiver
# (kind, seconds) laid end to end
PLAN = [("snow", 6), ("good", 10), ("snow", 1.5), ("good", 5), ("stats", 3), ("snow", 9),
        ("good", 8), ("snow", 6), ("good", 6), ("stats", 3), ("snow", 4)]
EXPECT_ACTIONS = ["cut", "keep", "cut", "keep", "cut", "cut", "keep", "cut", "keep", "cut", "cut"]
FAILURES: list[str] = []


def check(cond: bool, what: str) -> None:
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


def ff(*args: str, data: bytes | None = None) -> None:
    cmd = [dc.tool("ffmpeg"), "-v", "error", "-y"] + (["-nostdin"] if data is None else [])
    subprocess.run(cmd + list(args), check=True, input=data)


def osd_overlay(path: str, cells: dict, w: int = 640, h: int = 480) -> None:
    """Draw OSD-like text (white strokes with a black outline) into the given grid cells."""
    img = np.zeros((h, w, 4), np.uint8)
    cw, ch = w / 32, h / 18
    for (r, c), dense in cells.items():
        x0, y0 = int(c * cw), int(r * ch)
        rows = [0.25, 0.5, 0.75] if dense else [0.5]
        strokes = [(x0 + 3, y0 + int(ch * f), x0 + int(cw) - 3, y0 + int(ch * f) + 2) for f in rows]
        strokes.append((x0 + int(cw / 2) - 1, y0 + 4, x0 + int(cw / 2) + 1, y0 + int(ch) - 4))
        for xa, ya, xb, yb in strokes:
            img[max(0, ya - 2):yb + 2, max(0, xa - 2):xb + 2] = (0, 0, 0, 255)
        for xa, ya, xb, yb in strokes:
            img[ya:yb, xa:xb] = (255, 255, 255, 255)
    ff("-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{w}x{h}", "-i", "-", "-frames:v", "1", path,
       data=img.tobytes())


def make_session(d: str) -> list[str]:
    # STATS screen: drawn after the layout of Betaflight's post-flight statistics
    t = np.frombuffer(bytes.fromhex(dc._STATS_TEMPLATE_HEX), np.uint8).reshape(18, 32).astype(float)
    osd_overlay(os.path.join(d, "stats.png"), {(r, c): t[r, c] >= 36 for r in range(18) for c in range(32)
                                               if t[r, c] > 18})
    # normal flight OSD: RSSI / battery / timer in the corners
    osd_overlay(os.path.join(d, "osd.png"), {(r, c): False for r in (15, 16) for c in (1, 2, 3, 4, 26, 27, 28, 29, 30)}
                | {(1, c): False for c in (27, 28, 29, 30)})
    parts = []
    for i, (kind, sec) in enumerate(PLAN):
        p = os.path.join(d, f"part{i:02d}.avi")
        if kind == "snow":   # harsh black/white grain, new every frame, no colour - like real static
            src = ["-f", "lavfi", "-i", f"color=c=0x808080:s=320x240:r={FPS},noise=c0s=100:c0f=t+u,"
                   "lutyuv=y='if(gt(val,128),235,16)':u=128:v=128,scale=640:480:flags=neighbor"]
        else:                # moving, coloured picture with the flight OSD or the STATS screen on top
            src = ["-f", "lavfi", "-i", f"testsrc2=s=640x480:r={FPS}",
                   "-i", os.path.join(d, "stats.png" if kind == "stats" else "osd.png"),
                   "-filter_complex", "[0:v][1:v]overlay"]
        ff(*src, "-t", str(sec), "-pix_fmt", "yuvj420p", "-c:v", "mjpeg", "-q:v", "3", p)
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
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c:v", "copy", os.path.join(d, "single.avi"))
    return sorted((os.path.join(clips, f) for f in os.listdir(clips)), key=dc.natural_key)


def packet_hashes(path: str) -> list[str]:
    out = subprocess.run([dc.tool("ffmpeg"), "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0",
                          "-c", "copy", "-f", "framemd5", "-"], capture_output=True, text=True, check=True).stdout
    return [ln.split(",")[-1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


def probe(path: str) -> dict:
    out = subprocess.run([dc.tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,avg_frame_rate:format=duration", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    st = j["streams"][0]
    a, b = st["avg_frame_rate"].split("/")
    return {"w": st["width"], "h": st["height"], "fps": float(a) / float(b), "dur": float(j["format"]["duration"])}


def fake_ai_tools(d: str) -> str:
    """Stand-ins with the same command lines as realesrgan-ncnn-vulkan / rife-ncnn-vulkan."""
    root = os.path.join(d, "fakeai")
    py = sys.executable
    for name, body in {
        "realesrgan": (
            "import sys, os, subprocess\n"
            "a = sys.argv[1:]; g = lambda k, v=None: a[a.index(k) + 1] if k in a else v\n"
            "i, o, sc, fmt = g('-i'), g('-o'), int(g('-s', '4')), g('-f', 'png')\n"
            "os.makedirs(o, exist_ok=True)\n"
            f"subprocess.run([{dc.tool('ffmpeg')!r}, '-v', 'error', '-y', '-start_number', '1', '-i',"
            " os.path.join(i, '%08d.jpg'), '-vf', f'scale=iw*{sc}:ih*{sc}', '-q:v', '2', '-start_number', '1',"
            " os.path.join(o, '%08d.' + fmt)], check=True)\n"),
        "rife": (
            "import sys, os, shutil\n"
            "a = sys.argv[1:]; g = lambda k, v=None: a[a.index(k) + 1] if k in a else v\n"
            "i, o, pat = g('-i'), g('-o'), g('-f', '%08d.png')\n"
            "src = sorted(os.listdir(i)); n = int(g('-n', str(2 * len(src))))\n"
            "os.makedirs(o, exist_ok=True)\n"
            "for k in range(n): shutil.copy(os.path.join(i, src[min(len(src) - 1, k * len(src) // n)]),"
            " os.path.join(o, pat % (k + 1)))\n"),
    }.items():
        folder = os.path.join(root, name)
        os.makedirs(os.path.join(folder, "models"), exist_ok=True)
        os.makedirs(os.path.join(folder, en.RIFE_MODEL), exist_ok=True)
        with open(os.path.join(folder, "impl.py"), "w") as fh:
            fh.write(body)
        exe = os.path.join(folder, en.AI_TOOLS[name])
        with open(exe, "w") as fh:
            fh.write(f"#!/bin/sh\nexec {py} {os.path.join(folder, 'impl.py')} \"$@\"\n")
        os.chmod(exe, 0o755)
    return root


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
        check(s.settings.min_cut == 1.0 and s.settings.pad == 0.0 and s.settings.snow_cover == 0.95
              and s.settings.trim_stats, "default settings: cut >1 s, keep 0.0 s, 95% snow, trim STATS")

        # ---- detection -----------------------------------------------------
        truth, t = [], 0.0
        for kind, sec in PLAN:
            truth.append((kind, t, t + sec))
            t += sec
        check(len(s.segments) == len(PLAN), f"{len(PLAN)} segments found (got {len(s.segments)})")
        if len(s.segments) == len(PLAN):
            for sg, (kind, t0, t1) in zip(s.segments, truth):
                want = {"snow": "nosignal", "good": "signal", "stats": "stats"}[kind]
                check(sg.kind == want and abs(sg.t0 - t0) < 0.25 and abs(sg.t1 - t1) < 0.25,
                      f"{want:8} {t0:5.1f}-{t1:5.1f}s detected as {sg.kind} {sg.t0:5.2f}-{sg.t1:5.2f}s")
            actions = [sg.action for sg in s.segments]
            check(actions == EXPECT_ACTIONS, f"keep/cut decisions {actions}")
            check(sum(1 for sg in s.segments if sg.kind == "signal" and "(continued)" not in sg.label) == 3,
                  "three flights (the 1.5 s dropout does not start a new flight)")
        check(not any(sg.warn for sg in s.segments), "no false crash warnings on clean power-offs")
        check(float((s.labels == dc.LABEL_WEAK).mean()) < 0.01, "colourful clean footage is not labelled weak")

        # ---- chunked analysis == one pass ----------------------------------
        single = os.path.join(d, "single.avi")
        old = dc.CHUNK_MIN_S
        dc.CHUNK_MIN_S = 10.0
        try:
            a = dc.analyze_files([single], chunking=False)
            b = dc.analyze_files([single], chunking=True, workers=3)
            nchunks = len(dc._chunks_for(b.clips[0], 3) or [])
        finally:
            dc.CHUNK_MIN_S = old
        same = all(np.array_equal(a.f[k], b.f[k], equal_nan=True) for k in a.f) and np.array_equal(a.osd, b.osd)
        check(nchunks >= 2 and same and np.array_equal(a.labels, b.labels),
              f"chunked analysis ({nchunks} chunks) identical to one pass")

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
        check(len([c for c in chap.splitlines() if c.strip()]) == 3, "MKV has 3 flight chapters")

        print("Writing report...")
        rep = dc.write_report(s, os.path.join(d, "stitched.avi"))
        check(os.path.isfile(rep) and os.path.getsize(rep) > 500, "signal-loss report written")

        print("Saving diagnostics...")
        dz = os.path.join(d, "diagnostics.zip")
        info = dc.save_diagnostics(s, dz)
        with zipfile.ZipFile(dz) as z:
            names = z.namelist()
            check(all(n in names for n in ("info.txt", "README.txt", "measurements.csv", "blocks.npy", "osd.npy")),
                  "diagnostics zip has info, measurements, block grid and OSD map")
            rows = z.read("measurements.csv").decode().splitlines()
            check(len(rows) == s.n + 1, f"measurements.csv has a row for every frame ({len(rows) - 1} of {s.n})")
            blocks = np.load(io.BytesIO(z.read("blocks.npy")))
            check(blocks.shape == (s.n, 48), f"blocks.npy shape {blocks.shape}")
            frames = [n for n in names if n.startswith("frames/")]
            check(len(frames) == info["frames"] and len(frames) >= 20, f"{len(frames)} sample frames saved")
            by_name = {os.path.splitext(os.path.basename(f))[0]: f for f in files}
            exact = 0
            for n in frames:
                stem = os.path.basename(n)[:-4]
                clip, fi = stem.rsplit("_", 2)[-2], int(stem.rsplit("_f", 1)[1])
                if hashlib.md5(z.read(n)).hexdigest() == src[by_name[clip]][fi]:
                    exact += 1
            check(exact == len(frames), f"sample frames byte-identical to the originals ({exact}/{len(frames)})")

        # ---- enhance for YouTube -------------------------------------------
        print("Enhance (quick, no AI)...")
        es = en.EnhanceSettings()
        en.apply_preset(es, "quick")
        es.height = 1080                      # smaller than the default 1440p to keep the test quick
        r = en.enhance(s, os.path.join(d, "youtube.mp4"), es)
        p = probe(r["path"])
        check(p["w"] == 1440 and p["h"] == 1080 and abs(p["dur"] - s.kept_duration()) < 0.2,
              f"enhanced video {p['w']}x{p['h']}, {p['dur']:.2f}s (kept {s.kept_duration():.2f}s) via {r['encoder']}")
        png = en.compare_frame(s, s.segments[1].t0 + 2, es)
        check(png[:8] == b"\x89PNG\r\n\x1a\n", "before/after comparison picture made")

        if os.name != "nt":
            print("Enhance (AI upscale + 60 fps, stand-in AI tools)...")
            os.environ["FPV_AI_DIR"] = fake_ai_tools(d)
            es = en.EnhanceSettings()
            en.apply_preset(es, "youtube")
            es.fps60, es.height = True, 1080
            t0 = s.segments[1].t0 + 1
            r = en.enhance(s, os.path.join(d, "youtube_ai.mp4"), es, t_range=(t0, t0 + 4))
            p = probe(r["path"])
            check(p["w"] == 1440 and p["h"] == 1080 and abs(p["fps"] - 60) < 0.01 and abs(p["dur"] - 4) < 0.15,
                  f"AI path: {p['w']}x{p['h']} at {p['fps']:.0f} fps, {p['dur']:.2f}s")
            del os.environ["FPV_AI_DIR"]

    print()
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
