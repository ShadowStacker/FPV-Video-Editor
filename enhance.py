"""
FPV DVR Stitcher - "Enhance for YouTube" export.

Cleans up analog FPV footage and exports a high-resolution MP4 for upload:
  colour-speckle clean-up, grain denoise, sharpening (FFmpeg filters),
  upscaling (standard Lanczos, or AI with Real-ESRGAN),
  optional smooth 60 fps (AI frame interpolation with RIFE),
  encoding on the graphics card (NVIDIA NVENC / AMD AMF / Intel QSV) with CPU fallbacks.

The AI tools (realesrgan-ncnn-vulkan, rife-ncnn-vulkan) run on any Vulkan GPU. They come
as an add-on: in the zip download next to the program, or downloaded on first use.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, fields

import numpy as np

import dvr_core as dc

AI_ADDON_URL = ("https://github.com/ShadowStacker/FPV-Video-Editor/releases/latest/download/"
                "FPV-DVR-Stitcher-AI-addon.zip")
APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"),
                        "FPV DVR Stitcher")
AI_TOOLS = {"realesrgan": "realesrgan-ncnn-vulkan", "rife": "rife-ncnn-vulkan"}
AI_MODELS = {"detailed": "realesrgan-x4plus", "fast": "realesr-animevideov3"}
RIFE_MODEL = "rife-v4.6"
CHUNK_FRAMES = 420          # frames per AI batch (~15 s); keeps temporary files small
OUTPUT_HEIGHTS = (1080, 1440, 2160)


@dataclass
class EnhanceSettings:
    preset: str = "youtube"      # quick | youtube | custom
    speckle: bool = True         # clean the rainbow colour speckles of analog video
    denoise: int = 1             # grain denoise: 0 off, 1 light, 2 medium, 3 strong
    sharpen: int = 0             # 0 off, 1 light, 2 medium
    ai_upscale: bool = True      # Real-ESRGAN (else standard Lanczos)
    ai_model: str = "detailed"   # detailed (realesrgan-x4plus) | fast (realesr-animevideov3)
    fps60: bool = False          # smooth 60 fps with RIFE frame interpolation
    height: int = 1440           # output height; 1440p gets YouTube's better encoding
    codec: str = "auto"          # auto | hevc | h264 | av1
    quality: int = 19            # constant quality (lower = better, bigger)


PRESETS = {
    "quick": dict(speckle=True, denoise=1, sharpen=1, ai_upscale=False, ai_model="detailed", height=1440),
    "youtube": dict(speckle=True, denoise=1, sharpen=0, ai_upscale=True, ai_model="detailed", height=1440),
}


def apply_preset(es: EnhanceSettings, name: str) -> None:
    for k, v in PRESETS.get(name, {}).items():
        setattr(es, k, v)
    es.preset = name


def settings_dict(es: EnhanceSettings) -> dict:
    return {f.name: getattr(es, f.name) for f in fields(es)}


# ---------------------------------------------------------------------------
# AI add-on
# ---------------------------------------------------------------------------
def ai_dirs() -> list[str]:
    dirs = []
    if os.environ.get("FPV_AI_DIR"):
        dirs.append(os.environ["FPV_AI_DIR"])
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(sys.executable), "ai"))
    dirs.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai"))
    dirs.append(os.path.join(APP_DATA, "ai"))
    return dirs


def find_ai(name: str) -> str | None:
    exe = AI_TOOLS[name] + (".exe" if os.name == "nt" else "")
    for d in ai_dirs():
        p = os.path.join(d, name, exe)
        if os.path.isfile(p):
            return p
    return None


def ai_installed() -> bool:
    return find_ai("realesrgan") is not None and find_ai("rife") is not None


def download_ai_addon(progress=None, cancel: threading.Event | None = None) -> str:
    """Download the AI add-on from the project's latest release into the user's app data."""
    os.makedirs(APP_DATA, exist_ok=True)
    tmp = os.path.join(APP_DATA, "ai-addon.zip.part")
    req = urllib.request.Request(AI_ADDON_URL, headers={"User-Agent": f"{dc.APP_NAME}/{dc.APP_VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise dc.Cancelled()
            block = r.read(1 << 20)
            if not block:
                break
            fh.write(block)
            got += len(block)
            if progress:
                progress(got / total if total else 0.0,
                         f"Downloading AI add-on... {got / 1e6:.0f}" + (f" of {total / 1e6:.0f} MB" if total else " MB"))
    dest = os.path.join(APP_DATA, "ai")
    root = os.path.realpath(APP_DATA)
    with zipfile.ZipFile(tmp) as z:
        for m in z.infolist():
            name = m.filename.replace("\\", "/")
            if not name.startswith("ai/") or name.endswith("/"):
                continue
            target = os.path.realpath(os.path.join(APP_DATA, *name.split("/")))
            if not target.startswith(root + os.sep):
                continue                                  # never write outside the app folder
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(m) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if os.name != "nt" and target.endswith(tuple(AI_TOOLS.values())):
                os.chmod(target, 0o755)
    os.remove(tmp)
    if not ai_installed():
        raise RuntimeError("The AI add-on was downloaded but its programs were not found inside it.")
    return dest


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------
_ENCODER_OK: dict[str, bool] = {}
CANDIDATES = {
    "auto": ["hevc_nvenc", "h264_nvenc", "hevc_amf", "h264_amf", "hevc_qsv", "h264_qsv",
             "libx264", "libsvtav1", "libopenh264", "mpeg4"],
    "hevc": ["hevc_nvenc", "hevc_amf", "hevc_qsv", "libx265"],
    "h264": ["h264_nvenc", "h264_amf", "h264_qsv", "libx264", "libopenh264"],
    "av1": ["av1_nvenc", "av1_amf", "av1_qsv", "libsvtav1", "libaom-av1"],
}
ENCODER_NAMES = {
    "hevc_nvenc": "NVIDIA HEVC", "h264_nvenc": "NVIDIA H.264", "av1_nvenc": "NVIDIA AV1",
    "hevc_amf": "AMD HEVC", "h264_amf": "AMD H.264", "av1_amf": "AMD AV1",
    "hevc_qsv": "Intel HEVC", "h264_qsv": "Intel H.264", "av1_qsv": "Intel AV1",
    "libx264": "x264 (CPU)", "libx265": "x265 (CPU)", "libsvtav1": "SVT-AV1 (CPU)",
    "libaom-av1": "AOM AV1 (CPU)", "libopenh264": "OpenH264 (CPU)", "mpeg4": "MPEG-4 (CPU)",
}


def encoder_works(name: str) -> bool:
    if name not in _ENCODER_OK:
        try:
            p = subprocess.run([dc.tool("ffmpeg"), "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                                "color=c=gray:s=320x240:r=30", "-frames:v", "5", "-c:v", name,
                                *(["-pix_fmt", "nv12"] if name.endswith("_qsv") else ["-pix_fmt", "yuv420p"]),
                                "-f", "null", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                               timeout=30, **dc._popen_kwargs())
            _ENCODER_OK[name] = p.returncode == 0
        except Exception:
            _ENCODER_OK[name] = False
    return _ENCODER_OK[name]


def pick_encoder(codec: str = "auto") -> str:
    for name in CANDIDATES.get(codec, CANDIDATES["auto"]) + CANDIDATES["auto"]:
        if encoder_works(name):
            return name
    raise RuntimeError("No working video encoder found in FFmpeg.")


def encoder_args(name: str, q: int) -> list[str]:
    if name.endswith("_nvenc"):
        a = ["-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
             "-maxrate", "120M", "-bufsize", "240M", "-spatial-aq", "1", "-pix_fmt", "yuv420p"]
    elif name.endswith("_amf"):
        a = ["-quality", "quality", "-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q + 2), "-pix_fmt", "yuv420p"]
        if name.startswith("h264"):
            a += ["-qp_b", str(q + 4)]
    elif name.endswith("_qsv"):
        a = ["-preset", "slower", "-global_quality", str(q + 3), "-pix_fmt", "nv12"]
    elif name == "libx264":
        a = ["-preset", "slow", "-crf", str(q - 1), "-pix_fmt", "yuv420p"]
    elif name == "libx265":
        a = ["-preset", "medium", "-crf", str(q + 2), "-pix_fmt", "yuv420p"]
    elif name == "libsvtav1":
        a = ["-preset", "7", "-crf", str(q + 12), "-pix_fmt", "yuv420p"]
    elif name == "libaom-av1":
        a = ["-cpu-used", "6", "-crf", str(q + 12), "-b:v", "0", "-pix_fmt", "yuv420p"]
    elif name == "libopenh264":
        a = ["-b:v", "40M", "-pix_fmt", "yuv420p"]
    else:
        a = ["-q:v", "2", "-pix_fmt", "yuv420p"]
    if name.startswith("hevc") or name == "libx265":
        a += ["-tag:v", "hvc1"]
    return ["-c:v", name, *a]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def output_size(s: dc.Session, height: int) -> tuple[int, int]:
    c = s.clips[0]
    sw, sh = (c.width or 640), (c.height or 480)
    return int(round(height * sw / sh / 2)) * 2, int(height)


def work_size(s: dc.Session, es: EnhanceSettings) -> tuple[int, int]:
    """Resolution the frames are cleaned at (and handed to the AI tools)."""
    ow, oh = output_size(s, es.height)
    if es.ai_upscale:                       # the AI upscales 4x to the output size
        return int(round(ow / 4 / 2)) * 2, int(round(oh / 4 / 2)) * 2
    c = s.clips[0]
    w, h = (c.width or 640), (c.height or 480)
    if h > oh:
        return ow, oh
    return w - w % 2, h - h % 2


def pre_filters(s: dc.Session, es: EnhanceSettings, fps: float) -> list[str]:
    ww, wh = work_size(s, es)
    c = s.clips[0]
    f = [f"fps={fps:.6f}"]                  # constant frame rate (DVRs skip frames)
    if (ww, wh) != (c.width, c.height):
        f.append(f"scale={ww}:{wh}:flags=area")
    if es.speckle:                           # composite-video rainbow speckles live in the colour planes
        f += ["gblur=sigma=1.2:planes=6", "atadenoise=p=6:s=9:1a=0.06:1b=0.15:2a=0.06:2b=0.15"]
    if es.denoise == 1:
        f.append("atadenoise=p=1:s=5:0a=0.02:0b=0.05")
    elif es.denoise == 2:
        f += ["atadenoise=p=1:s=7:0a=0.03:0b=0.07", "nlmeans=s=1.5:p=5:r=9"]
    elif es.denoise >= 3:
        f += ["atadenoise=p=1:s=9:0a=0.04:0b=0.09", "nlmeans=s=3.0:p=7:r=11"]
    return f


def post_filters(s: dc.Session, es: EnhanceSettings) -> list[str]:
    ow, oh = output_size(s, es.height)
    f = [f"scale={ow}:{oh}:flags=lanczos"]
    if es.sharpen == 1:
        f.append("cas=strength=0.35")
    elif es.sharpen >= 2:
        f.append("cas=strength=0.65")
    return f


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def _run(cmd: list[str], cancel=None, what: str = "") -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, **dc._popen_kwargs())
    err = []
    t = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    t.start()
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait()
            raise dc.Cancelled()
        time.sleep(0.2)
    t.join(timeout=5)
    if proc.returncode != 0:
        msg = (err[0] if err else b"").decode("utf-8", "replace")[-2500:]
        raise RuntimeError(f"{what or os.path.basename(cmd[0])} failed:\n{msg}")


def _sequence(folder: str, expected: int, what: str) -> None:
    """Check a tool produced the expected number of frames and name them 00000001.jpg, ..."""
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")))
    if len(files) != expected:
        raise RuntimeError(f"{what} produced {len(files)} frames, expected {expected}.")
    for i, f in enumerate(files, 1):
        want = f"{i:08d}{os.path.splitext(f)[1].lower()}"
        if f != want:
            os.replace(os.path.join(folder, f), os.path.join(folder, want + ".tmp"))
    for f in os.listdir(folder):
        if f.endswith(".tmp"):
            os.replace(os.path.join(folder, f), os.path.join(folder, f[:-4]))


def _upscale(src: str, dst: str, es: EnhanceSettings, cancel=None) -> None:
    exe = find_ai("realesrgan")
    if exe is None:
        raise RuntimeError("AI upscaling needs the AI add-on (Enhance > Download AI add-on).")
    os.makedirs(dst, exist_ok=True)
    n = len(os.listdir(src))
    _run([exe, "-i", src, "-o", dst, "-n", AI_MODELS.get(es.ai_model, AI_MODELS["detailed"]), "-s", "4",
          "-f", "jpg", "-m", os.path.join(os.path.dirname(exe), "models")], cancel, "AI upscaling (Real-ESRGAN)")
    _sequence(dst, n, "AI upscaling")


def _interpolate(src: str, dst: str, count: int, uhd: bool, cancel=None) -> None:
    exe = find_ai("rife")
    if exe is None:
        raise RuntimeError("Smooth 60 fps needs the AI add-on (Enhance > Download AI add-on).")
    os.makedirs(dst, exist_ok=True)
    cmd = [exe, "-i", src, "-o", dst, "-n", str(count), "-m", os.path.join(os.path.dirname(exe), RIFE_MODEL),
           "-f", "%08d.jpg"]
    if uhd:
        cmd.append("-u")
    _run(cmd, cancel, "Frame interpolation (RIFE)")
    _sequence(dst, count, "Frame interpolation")


def _fmt_eta(sec: float) -> str:
    if not math.isfinite(sec) or sec <= 0:
        return ""
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m left" if h else (f"{m}m {s:02d}s left" if m else f"{s}s left")


def enhance(s: dc.Session, out_path: str, es: EnhanceSettings, progress=None,
            cancel: threading.Event | None = None, t_range: tuple[float, float] | None = None) -> dict:
    """Export the kept footage (or just t_range of the session) enhanced, as an MP4."""
    keep = s.keep_mask()
    if t_range is not None:
        keep &= (s.t >= t_range[0]) & (s.t < t_range[1])
    pieces = dc.build_pieces(s, keep)
    if not pieces:
        raise RuntimeError("Nothing to export in that range.")
    if (es.ai_upscale or es.fps60) and not ai_installed():
        raise RuntimeError("AI upscaling and smooth 60 fps need the AI add-on.\n"
                           "Use Enhance > Download AI add-on, or switch them off.")
    src_fps = float(np.median([c.fps for c in s.clips]))
    out_fps = 60.0 if es.fps60 else src_fps
    enc = pick_encoder(es.codec)
    ow, oh = output_size(s, es.height)
    t_start = time.time()

    def prog(frac, msg):
        if cancel is not None and cancel.is_set():
            raise dc.Cancelled()
        if progress:
            el = time.time() - t_start
            eta = _fmt_eta(el / frac - el) if frac > 0.02 else ""
            progress(min(1.0, frac), f"{msg}  {eta}".rstrip())

    tmp = dc._tmpdir_for(out_path)
    try:
        lst = os.path.join(tmp, "concat.txt")
        duration = dc.write_concat_list(pieces, lst, tmp, cancel)
        src = ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-an", "-sn", "-dn"]
        if not (es.ai_upscale or es.fps60):
            # one pass: clean, upscale, encode
            cmd = [dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
                   *src, "-vf", ",".join(pre_filters(s, es, src_fps) + post_filters(s, es)),
                   *encoder_args(enc, es.quality), "-movflags", "+faststart", out_path]
            dc._run_ffmpeg_progress(cmd, duration, lambda f, m: prog(f, m), cancel,
                                    f"Enhancing ({ENCODER_NAMES.get(enc, enc)})...")
        else:
            _check_space(tmp, s, es, duration, src_fps)
            frames = os.path.join(tmp, "frames")
            os.makedirs(frames)
            prog(0.0, "Preparing frames...")
            cmd = [dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
                   *src, "-vf", ",".join(pre_filters(s, es, src_fps)), "-q:v", "2", "-start_number", "1",
                   os.path.join(frames, "%08d.jpg")]
            dc._run_ffmpeg_progress(cmd, duration, lambda f, m: prog(0.08 * f, m), cancel, "Preparing frames...")
            names = sorted(os.listdir(frames))
            n = len(names)
            if n == 0:
                raise RuntimeError("No frames to enhance.")
            segs = []
            edges = list(range(0, n, CHUNK_FRAMES)) + [n]
            for k, (a, b) in enumerate(zip(edges[:-1], edges[1:])):
                cdir = os.path.join(tmp, f"chunk{k:04d}")
                cur = os.path.join(cdir, "in")
                os.makedirs(cur)
                for i, nm in enumerate(names[a:b], 1):
                    os.replace(os.path.join(frames, nm), os.path.join(cur, f"{i:08d}.jpg"))
                label = f"Batch {k + 1} of {len(edges) - 1}"
                base = 0.08 + 0.9 * a / n
                span = 0.9 * (b - a) / n
                if es.ai_upscale:
                    prog(base, f"{label}: AI upscaling...")
                    _upscale(cur, os.path.join(cdir, "up"), es, cancel)
                    shutil.rmtree(cur, ignore_errors=True)
                    cur = os.path.join(cdir, "up")
                count = b - a
                if es.fps60:
                    count = int(round(b * out_fps / src_fps)) - int(round(a * out_fps / src_fps))
                    prog(base + span * (0.6 if es.ai_upscale else 0.0), f"{label}: smoothing to 60 fps...")
                    _interpolate(cur, os.path.join(cdir, "fps"), count, uhd=es.ai_upscale and oh >= 1440,
                                 cancel=cancel)
                    shutil.rmtree(cur, ignore_errors=True)
                    cur = os.path.join(cdir, "fps")
                prog(base + span * 0.9, f"{label}: encoding ({ENCODER_NAMES.get(enc, enc)})...")
                seg = os.path.join(tmp, f"seg{k:04d}.mp4")
                _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-framerate", f"{out_fps:.6f}",
                      "-start_number", "1", "-i", os.path.join(cur, "%08d.jpg"),
                      "-vf", ",".join(post_filters(s, es)), *encoder_args(enc, es.quality), seg],
                     cancel, "Encoding")
                shutil.rmtree(cdir, ignore_errors=True)
                segs.append(seg)
            prog(0.99, "Joining...")
            seglist = os.path.join(tmp, "segments.txt")
            with open(seglist, "w", encoding="utf-8") as fh:
                fh.write("ffconcat version 1.0\n" + "".join(f"file {dc._concat_path(p)}\n" for p in segs))
            _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", seglist,
                  "-c", "copy", "-movflags", "+faststart", out_path], cancel, "Joining")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(1.0, "Done.")
    return {"path": out_path, "encoder": ENCODER_NAMES.get(enc, enc), "size": (ow, oh),
            "fps": out_fps, "seconds": time.time() - t_start, "duration": duration}


def _check_space(tmp: str, s: dc.Session, es: EnhanceSettings, duration: float, fps: float) -> None:
    ww, wh = work_size(s, es)
    ow, oh = output_size(s, es.height)
    frames = duration * fps
    need = frames * ww * wh * 0.3 + CHUNK_FRAMES * ow * oh * 0.4 * (3 if es.fps60 else 1) + 2e9
    free = shutil.disk_usage(tmp).free
    if free < need:
        raise RuntimeError(f"Not enough free disk space next to the output file: about "
                           f"{need / 1e9:.0f} GB of temporary space is needed, {free / 1e9:.0f} GB is free.")


def compare_frame(s: dc.Session, t: float, es: EnhanceSettings, cancel=None) -> bytes:
    """A before/after picture of one moment: full frames on top, a 100% crop below (PNG)."""
    if es.ai_upscale and not ai_installed():
        raise RuntimeError("AI upscaling needs the AI add-on (Enhance > Download AI add-on).")
    os.makedirs(APP_DATA, exist_ok=True)
    work = os.path.join(APP_DATA, "compare")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        src_fps = float(np.median([c.fps for c in s.clips]))
        keep = np.zeros(s.n, bool)
        keep[(s.t >= t - 0.5) & (s.t < t + 0.5)] = True
        pieces = dc.build_pieces(s, keep)
        if not pieces:
            raise RuntimeError("No picture at that moment.")
        lst = os.path.join(work, "concat.txt")
        dc.write_concat_list(pieces, lst, work, cancel)
        pre = os.path.join(work, "pre")
        os.makedirs(pre)
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", lst,
              "-map", "0:v:0", "-an", "-vf", ",".join(pre_filters(s, es, src_fps)), "-q:v", "2",
              "-start_number", "1", os.path.join(pre, "%08d.jpg")], cancel, "Preparing frame")
        names = sorted(os.listdir(pre))
        if not names:
            raise RuntimeError("No picture at that moment.")
        mid = names[len(names) // 2]          # temporal filters need neighbouring frames
        one = os.path.join(work, "one")
        os.makedirs(one)
        shutil.copy(os.path.join(pre, mid), os.path.join(one, "00000001.jpg"))
        cur = one
        if es.ai_upscale:
            _upscale(one, os.path.join(work, "up"), es, cancel)
            cur = os.path.join(work, "up")
        after = os.path.join(work, "after.png")
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", os.path.join(cur, "00000001.jpg"),
              "-vf", ",".join(post_filters(s, es)), "-frames:v", "1", after], cancel, "Finishing frame")
        ow, oh = output_size(s, es.height)
        c, ts = s.locate(float(t))
        before = os.path.join(work, "before.png")
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-ss", f"{max(0.0, ts):.4f}", "-i", c.path,
              "-frames:v", "1", "-vf", f"scale={ow}:{oh}:flags=bicubic", before], cancel, "Original frame")
        cw, ch = min(640, ow), min(360, oh)
        graph = (f"[0:v]split[b0][b1];[1:v]split[a0][a1];"
                 f"[b0]scale=640:-2[bf];[a0]scale=640:-2[af];"
                 f"[b1]crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2[bc];[a1]crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2[ac];"
                 f"[bf][af][bc][ac]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0")
        out = os.path.join(work, "compare.png")
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", before, "-i", after,
              "-filter_complex", graph, "-frames:v", "1", out], cancel, "Building comparison")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def preview_path() -> str:
    d = os.path.join(APP_DATA, "previews")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"preview_{time.strftime('%H%M%S')}.mp4")
