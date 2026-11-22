"""
FPV DVR Stitcher - "Enhance for YouTube" export.

Cleans up analog FPV footage and exports a high-resolution MP4 for upload:
  colour-speckle clean-up, grain denoise (processor or graphics card), sharpening,
  upscaling (standard Lanczos, or AI with Real-ESRGAN),
  optional smooth 60 fps (AI frame interpolation with RIFE),
  encoding on the graphics card (NVIDIA NVENC / AMD AMF / Intel QSV) with CPU fallbacks.

Speed: frames are prepared in parallel parts on all processor cores, and preparing,
AI work and encoding overlap instead of running one after another. Measured speeds
are remembered (speed.json) so time estimates fit the PC.

The AI tools (realesrgan-ncnn-vulkan, rife-ncnn-vulkan) run on any Vulkan GPU. They come
as an add-on: in the zip download next to the program, or downloaded on first use.
"""
from __future__ import annotations

import json
import math
import os
import queue
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
VK_ARGS = ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]


@dataclass
class EnhanceSettings:
    preset: str = "balanced"     # quick | balanced | best | custom
    speckle: bool = True         # clean the rainbow colour speckles of analog video
    denoise: int = 1             # grain denoise: 0 off, 1 light, 2 medium, 3 strong
    sharpen: int = 0             # 0 off, 1 light, 2 medium
    ai_upscale: bool = True      # Real-ESRGAN (else standard Lanczos)
    ai_model: str = "fast"       # fast (realesr-animevideov3) | detailed (realesrgan-x4plus)
    ai_res: str = "full"         # full: AI makes the output size | 1080: AI makes 1080p, then resize
    fps60: bool = False          # smooth 60 fps with RIFE frame interpolation
    fps_detail: str = "std"      # std | hd (RIFE high-resolution mode, slower)
    height: int = 1440           # output height; 1440p gets YouTube's better encoding
    codec: str = "auto"          # auto | hevc | h264 | av1
    quality: int = 19            # constant quality (lower = better, bigger)
    overlap: bool = True         # prepare, AI-upscale and encode at the same time
    all_cores: bool = True       # prepare frames in parallel parts
    gpu_denoise: bool = True     # Medium/Strong denoise on the graphics card (Vulkan)


PRESETS = {
    "quick": dict(speckle=True, denoise=1, sharpen=1, ai_upscale=False, ai_model="fast", ai_res="full"),
    "balanced": dict(speckle=True, denoise=1, sharpen=0, ai_upscale=True, ai_model="fast", ai_res="full"),
    "best": dict(speckle=True, denoise=1, sharpen=0, ai_upscale=True, ai_model="detailed", ai_res="full"),
}
PRESET_ALIASES = {"youtube": "best"}


def apply_preset(es: EnhanceSettings, name: str) -> None:
    name = PRESET_ALIASES.get(name, name)
    for k, v in PRESETS.get(name, {}).items():
        setattr(es, k, v)
    if name in PRESETS:
        es.overlap = es.all_cores = es.gpu_denoise = True
    es.preset = name


def match_preset(es: EnhanceSettings) -> str:
    for name, vals in PRESETS.items():
        if all(getattr(es, k) == v for k, v in vals.items()
               if es.ai_upscale or k not in ("ai_model", "ai_res")):
            return name
    return "custom"


def settings_dict(es: EnhanceSettings) -> dict:
    return {f.name: getattr(es, f.name) for f in fields(es)}


def settings_from_dict(d: dict) -> EnhanceSettings:
    es = EnhanceSettings()
    for f in fields(es):
        v = d.get(f.name)
        if v is not None and isinstance(v, type(getattr(es, f.name))):
            setattr(es, f.name, v)
    es.preset = PRESET_ALIASES.get(es.preset, es.preset)
    return es


def _settings_file() -> str:
    return os.path.join(APP_DATA, "enhance_settings.json")


def load_settings() -> EnhanceSettings:
    try:
        with open(_settings_file(), encoding="utf-8") as fh:
            return settings_from_dict(json.load(fh))
    except Exception:
        return EnhanceSettings()


def save_settings(es: EnhanceSettings) -> None:
    try:
        os.makedirs(APP_DATA, exist_ok=True)
        with open(_settings_file(), "w", encoding="utf-8") as fh:
            json.dump(settings_dict(es), fh, indent=1)
    except Exception:
        pass


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
# Encoders and graphics-card denoise
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


def is_hw_encoder(name: str) -> bool:
    return name.endswith(("_nvenc", "_amf", "_qsv"))


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


_GPU_DN: bool | None = None


def gpu_denoise_works() -> bool:
    """Can FFmpeg run the non-local-means denoiser on the graphics card (Vulkan)?"""
    global _GPU_DN
    if _GPU_DN is None:
        try:
            p = subprocess.run([dc.tool("ffmpeg"), "-v", "error", "-nostdin", *VK_ARGS, "-f", "lavfi", "-i",
                                "testsrc2=s=160x96:r=10", "-frames:v", "3", "-vf",
                                "format=yuv420p,hwupload,nlmeans_vulkan,hwdownload,format=yuv420p", "-f", "null", "-"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                               timeout=60, **dc._popen_kwargs())
            _GPU_DN = p.returncode == 0
        except Exception:
            _GPU_DN = False
    return _GPU_DN


def uses_gpu_denoise(es: EnhanceSettings) -> bool:
    return es.gpu_denoise and es.denoise >= 2 and gpu_denoise_works()


# ---------------------------------------------------------------------------
# Sizes and filters
# ---------------------------------------------------------------------------
def output_size(s: dc.Session, height: int) -> tuple[int, int]:
    c = s.clips[0]
    sw, sh = (c.width or 640), (c.height or 480)
    return int(round(height * sw / sh / 2)) * 2, int(height)


def ai_out_height(es: EnhanceSettings) -> int:
    return es.height if es.ai_res == "full" else min(es.height, 1080)


def work_size(s: dc.Session, es: EnhanceSettings) -> tuple[int, int]:
    """Resolution the frames are cleaned at (and handed to the AI tools)."""
    ow, oh = output_size(s, es.height)
    if es.ai_upscale:                       # the AI upscales 4x
        ah = ai_out_height(es)
        aw = int(round(ah * ow / oh / 2)) * 2
        return int(round(aw / 4 / 2)) * 2, int(round(ah / 4 / 2)) * 2
    c = s.clips[0]
    w, h = (c.width or 640), (c.height or 480)
    if h > oh:
        return ow, oh
    return w - w % 2, h - h % 2


def pre_filters(s: dc.Session, es: EnhanceSettings, fps: float, gpu_dn: bool = False) -> list[str]:
    ww, wh = work_size(s, es)
    c = s.clips[0]
    f = [f"fps={fps:.6f}"]                  # constant frame rate (DVRs skip frames)
    if (ww, wh) != (c.width, c.height):
        f.append(f"scale={ww}:{wh}:flags=area")
    if es.speckle:                           # composite-video rainbow speckles live in the colour planes
        f += ["gblur=sigma=1.2:planes=6", "atadenoise=p=6:s=9:1a=0.06:1b=0.15:2a=0.06:2b=0.15"]
    nl = None
    if es.denoise == 1:
        f.append("atadenoise=p=1:s=5:0a=0.02:0b=0.05")
    elif es.denoise == 2:
        f.append("atadenoise=p=1:s=7:0a=0.03:0b=0.07")
        nl = "s=1.5:p=5:r=9"
    elif es.denoise >= 3:
        f.append("atadenoise=p=1:s=9:0a=0.04:0b=0.09")
        nl = "s=3.0:p=7:r=11"
    if nl:
        f += (["format=yuv420p", "hwupload", f"nlmeans_vulkan={nl}", "hwdownload", "format=yuv420p"]
              if gpu_dn else [f"nlmeans={nl}"])
    return f


def post_filters(s: dc.Session, es: EnhanceSettings) -> list[str]:
    ow, oh = output_size(s, es.height)
    f = [f"scale={ow}:{oh}:flags=lanczos"]
    if es.sharpen == 1:
        f.append("cas=strength=0.35")
    elif es.sharpen >= 2:
        f.append("cas=strength=0.65")
    return f


def workers(es: EnhanceSettings) -> int:
    if not es.all_cores:
        return 1
    return max(2, min(6, (os.cpu_count() or 2) // 2))


# ---------------------------------------------------------------------------
# Measured speeds (remembered between runs) and time estimates
# ---------------------------------------------------------------------------
_SPEED: dict | None = None
_SPEED_LOCK = threading.Lock()


def _speed_file() -> str:
    return os.path.join(APP_DATA, "speed.json")


def _speeds() -> dict:
    global _SPEED
    if _SPEED is None:
        try:
            with open(_speed_file(), encoding="utf-8") as fh:
                _SPEED = json.load(fh)
        except Exception:
            _SPEED = {}
    return _SPEED


def record_speed(key: str, value: float) -> None:
    if not (value > 0 and math.isfinite(value)):
        return
    with _SPEED_LOCK:
        sp = _speeds()
        old = sp.get(key)
        sp[key] = value if old is None else 0.5 * old + 0.5 * value
        try:
            os.makedirs(APP_DATA, exist_ok=True)
            with open(_speed_file(), "w", encoding="utf-8") as fh:
                json.dump(sp, fh, indent=1)
        except Exception:
            pass


def _speed(key: str, default: float) -> tuple[float, bool]:
    v = _speeds().get(key)
    return (float(v), True) if v else (default, False)


def _keys(s: dc.Session, es: EnhanceSettings, enc: str) -> dict:
    ww, wh = work_size(s, es)
    gpu = uses_gpu_denoise(es)
    rife_h = ai_out_height(es) if es.ai_upscale else wh
    return {
        "prep": f"prep|{es.speckle}|{es.denoise}|{gpu}|{workers(es)}|{wh}",
        "up": f"up|{es.ai_model}|{ai_out_height(es)}",
        "rife": f"rife|{es.fps_detail}|{rife_h}",
        "enc": f"enc|{enc}|{es.height}",
        "quick": f"quick|{es.speckle}|{es.denoise}|{gpu}|{quick_parts(es, enc)}|{es.height}|{es.sharpen}|{enc}",
    }


def _defaults(s: dc.Session, es: EnhanceSettings) -> dict:
    """First-guess speeds (before anything was measured on this PC), sized for an RTX 4070 class GPU.
    Units: prep = frames/s, up = s/frame, rife = s/output frame, enc = frames/s, quick = s per s of video."""
    ww, wh = work_size(s, es)
    pix = (ww * wh) / (640 * 360)
    cpu_cost = 1.0 + (0.3 if es.speckle else 0) + {0: 0, 1: 0.5, 2: 6.0, 3: 15.0}[min(3, es.denoise)]
    if uses_gpu_denoise(es):
        cpu_cost = 1.3 + (0.3 if es.speckle else 0)
    cpu_cost *= max(0.3, pix) / (2.5 if es.all_cores else 1.0)
    prep_fps = 10200 / (cpu_cost * 60)
    ah = ai_out_height(es)
    up = (0.3235 if es.ai_model == "detailed" else 0.047) * (ah / 1440) ** 2
    rh = ah if es.ai_upscale else wh
    rife = 0.0333 * (rh / 1440) ** 2 * (1.7 if es.fps_detail == "hd" else 1.0)
    enc = 10200 / (2.0 * 60) / (es.height / 1440) ** 2
    quick = max(0.35, cpu_cost / 6 + 0.4 * (es.height / 1440) ** 2)
    return {"prep": prep_fps, "up": up, "rife": rife, "enc": enc, "quick": quick}


def estimate(s: dc.Session, es: EnhanceSettings, enc: str = "auto", seconds: float | None = None) -> dict:
    """Estimated run time of an enhance export, with the parts that run on the processor and the
    graphics card, using speeds measured on this PC where available."""
    dur = s.kept_duration() if seconds is None else seconds
    src_fps = float(np.median([c.fps for c in s.clips]))
    frames = dur * src_fps
    out_frames = frames * (60.0 / src_fps if es.fps60 else 1.0)
    keys, dflt = _keys(s, es, enc), _defaults(s, es)
    measured = True

    def sp(name, counts=True):
        nonlocal measured
        v, m = _speed(keys[name], dflt[name])
        if counts:
            measured = measured and m
        return v

    if not (es.ai_upscale or es.fps60):
        t = dur * sp("quick")
        return {"total": t, "cpu": [("prep", 0, t)], "gpu": [("enc", 0, t)], "measured": measured,
                "frames": frames, "batches": 1, "prep": t, "up": 0.0, "rife": 0.0, "encode": t}
    prep = frames / sp("prep", counts=not es.overlap)    # with overlap, preparing hides behind the AI
    up = frames * sp("up") if es.ai_upscale else 0.0
    rife = out_frames * sp("rife") if es.fps60 else 0.0
    encode = out_frames / sp("enc")
    nb = max(1, math.ceil(frames / CHUNK_FRAMES))
    if es.overlap:
        start = min(prep, prep / nb + 2.0)
        end = max(prep, start + up + rife)
        total = end + encode / nb
        gpu = [("ai", start, start + up), ("fps", start + up, start + up + rife), ("enc", total - encode / nb, total)]
    else:
        total = prep + up + rife + encode
        gpu = [("ai", prep, prep + up), ("fps", prep + up, prep + up + rife), ("enc", prep + up + rife, total)]
    return {"total": total, "cpu": [("prep", 0, prep)], "gpu": gpu, "measured": measured,
            "frames": frames, "batches": nb, "prep": prep, "up": up, "rife": rife, "encode": encode}


def fmt_minutes(sec: float) -> str:
    if not math.isfinite(sec) or sec <= 0:
        return ""
    if sec < 90:
        return f"{max(1, int(round(sec)))} s"
    m = sec / 60
    if m < 60:
        return f"{int(round(m))} min"
    h = int(m // 60)
    return f"{h} h {int(round(m - 60 * h)):02d} min"


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
        time.sleep(0.1)
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
    _run([exe, "-i", src, "-o", dst, "-n", AI_MODELS.get(es.ai_model, AI_MODELS["fast"]), "-s", "4",
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


def _parts(duration: float, n: int) -> list[tuple[float, float]]:
    edges = np.linspace(0.0, duration, n + 1)
    return [(float(a), float(b - a)) for a, b in zip(edges[:-1], edges[1:]) if b - a > 0.05]


def _part_cmd(lst: str, a: float, length: float, n_parts: int, vf: list[str], gpu: bool) -> list[str]:
    cmd = [dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats"]
    if gpu:
        cmd += VK_ARGS
    if n_parts > 1:
        cmd += ["-ss", f"{a:.4f}", "-t", f"{length:.4f}"]
    return cmd + ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-an", "-sn", "-dn",
                  "-vf", ",".join(vf)]


class _Progress:
    """Measured time per stage -> progress messages with an honest time left."""

    def __init__(self, cb, cancel, est: dict, es: EnhanceSettings, ratio: float):
        self.cb, self.cancel, self.es, self.ratio = cb, cancel, es, ratio
        self.t0 = time.time()
        self.expected = max(1.0, est["frames"])
        self.nb = est["batches"]
        self.est = est
        self.prep_frac = [0.0]
        self.prep_done_at = None
        self.gpu_frames = 0
        self.gpu_time = 0.0
        self.batches_done = 0
        self.batch_times: list[float] = []
        self.enc_frames = 0
        self.enc_time = 0.0
        self.lock = threading.Lock()

    def prep(self) -> float:
        return float(np.mean(self.prep_frac)) if self.prep_frac else 1.0

    def left(self) -> float:
        el = time.time() - self.t0
        pf = self.prep()
        if self.prep_done_at is not None:
            prep_left = 0.0
        elif pf > 0.02:
            prep_left = el * (1 - pf) / pf
        else:
            prep_left = self.est["prep"]
        if self.gpu_frames > 0:
            per = self.gpu_time / self.gpu_frames
        else:
            per = (self.est["up"] + self.est["rife"]) / self.expected
        remaining = max(0.0, self.expected - self.gpu_frames)        # input frames the AI still has to do
        gpu_left = remaining * per
        out_total = self.expected * self.ratio
        enc_per = (self.enc_time / self.enc_frames if self.enc_frames
                   else self.est["encode"] / max(1.0, out_total))
        pending = max(0.0, self.gpu_frames * self.ratio - self.enc_frames) * enc_per   # done by the AI, not encoded
        if self.es.overlap:
            tail = min(CHUNK_FRAMES, remaining) * self.ratio * enc_per if remaining > 0 else pending
            return max(prep_left, gpu_left) + tail
        return prep_left + gpu_left + pending + remaining * self.ratio * enc_per

    def say(self, stage: str, batch: int | None = None):
        if self.cancel is not None and self.cancel.is_set():
            raise dc.Cancelled()
        if self.cb is None:
            return
        with self.lock:
            frac = 0.04 * self.prep() + 0.96 * min(1.0, self.gpu_frames / self.expected)
            left = self.left()
            if batch is None:
                msg = f"{stage} {100 * self.prep():.0f}%"
            else:
                msg = f"Batch {batch} of {max(batch, self.nb)}: {stage}"
                if self.batch_times:
                    msg += f"  ·  {fmt_minutes(float(np.mean(self.batch_times[-5:])))} per batch"
            if left > 0 and (self.batch_times or self.prep() > 0.03):
                msg += f"  ·  about {fmt_minutes(left)} left"
        self.cb(min(1.0, frac), msg)


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
    cancel = cancel or threading.Event()
    src_fps = float(np.median([c.fps for c in s.clips]))
    out_fps = 60.0 if es.fps60 else src_fps
    enc = pick_encoder(es.codec)
    gpu_dn = uses_gpu_denoise(es)
    ow, oh = output_size(s, es.height)
    t_start = time.time()
    tmp = dc._tmpdir_for(out_path)
    keys = _keys(s, es, enc)
    try:
        lst = os.path.join(tmp, "concat.txt")
        duration = dc.write_concat_list(pieces, lst, tmp, cancel)
        est = estimate(s, es, enc, seconds=duration)
        if not (es.ai_upscale or es.fps60):
            n = _quick(s, es, enc, lst, duration, src_fps, gpu_dn, out_path, tmp, progress, cancel)
            if n == quick_parts(es, enc):         # short previews run as one part: not comparable
                record_speed(keys["quick"], (time.time() - t_start) / max(1.0, duration))
        else:
            _check_space(tmp, s, es, duration, src_fps)
            _ai_pipeline(s, es, enc, lst, duration, src_fps, out_fps, gpu_dn, out_path, tmp, est,
                         keys, progress, cancel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(1.0, "Done.")
    return {"path": out_path, "encoder": ENCODER_NAMES.get(enc, enc), "size": (ow, oh),
            "fps": out_fps, "seconds": time.time() - t_start, "duration": duration,
            "gpu_denoise": gpu_dn}


def quick_parts(es: EnhanceSettings, enc: str) -> int:
    return min(workers(es), 3) if is_hw_encoder(enc) else workers(es)


def _quick(s, es, enc, lst, duration, src_fps, gpu_dn, out_path, tmp, progress, cancel) -> int:
    """No AI: clean, resize and encode in one pass, split into parallel parts."""
    vf = pre_filters(s, es, src_fps, gpu_dn) + post_filters(s, es)
    n = quick_parts(es, enc)                 # consumer graphics cards limit parallel encodes
    parts = _parts(duration, n if duration > 20 else 1)
    fracs = [0.0] * len(parts)
    t0 = time.time()

    def report():
        if progress:
            f = float(np.average(fracs, weights=[p[1] for p in parts]))
            el = time.time() - t0
            left = el * (1 - f) / f if f > 0.03 else 0
            progress(f, f"Enhancing ({ENCODER_NAMES.get(enc, enc)}, {len(parts)} at once)..."
                        + (f"  ·  about {fmt_minutes(left)} left" if left else ""))

    def one(k):
        a, length = parts[k]
        out = out_path if len(parts) == 1 else os.path.join(tmp, f"q{k:02d}.mp4")
        cmd = _part_cmd(lst, a, length, len(parts), vf, gpu_dn) + [*encoder_args(enc, es.quality),
                                                                    "-movflags", "+faststart", out]

        def cb(f, _m):
            fracs[k] = f
            report()
        dc._run_ffmpeg_progress(cmd, length, cb, cancel, "")
        return out

    if len(parts) == 1:
        one(0)
        return 1
    with _pool(len(parts)) as ex:
        segs = _gather([ex.submit(one, k) for k in range(len(parts))], cancel)
    _join(segs, out_path, tmp, cancel)
    return len(parts)


class _pool:
    def __init__(self, n):
        from concurrent.futures import ThreadPoolExecutor
        self.ex = ThreadPoolExecutor(max_workers=n)

    def __enter__(self):
        return self.ex

    def __exit__(self, *a):
        self.ex.shutdown(wait=True)


def _gather(futs, cancel):
    try:
        return [f.result() for f in futs]
    except BaseException:
        cancel.set()
        raise


def _join(segs: list[str], out_path: str, tmp: str, cancel) -> None:
    seglist = os.path.join(tmp, "segments.txt")
    with open(seglist, "w", encoding="utf-8") as fh:
        fh.write("ffconcat version 1.0\n" + "".join(f"file {dc._concat_path(p)}\n" for p in segs))
    _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-f", "concat", "-safe", "0", "-i", seglist,
          "-c", "copy", "-movflags", "+faststart", out_path], cancel, "Joining")


def _ai_pipeline(s, es, enc, lst, duration, src_fps, out_fps, gpu_dn, out_path, tmp, est, keys,
                 progress, cancel) -> None:
    """Prepare frames (parallel parts) -> AI upscale / 60 fps in batches -> encode, overlapping."""
    ratio = out_fps / src_fps
    pr = _Progress(progress, cancel, est, es, ratio)
    parts = _parts(duration, workers(es) if duration > 20 else 1)
    pr.prep_frac = [0.0] * len(parts)
    dirs = [os.path.join(tmp, f"prep{k:02d}") for k in range(len(parts))]
    done = [False] * len(parts)
    finished = [0.0] * len(parts)
    errors: list[BaseException] = []
    vf = pre_filters(s, es, src_fps, gpu_dn)
    t_prep = time.time()

    def prep_part(k):
        a, length = parts[k]
        os.makedirs(dirs[k], exist_ok=True)
        cmd = _part_cmd(lst, a, length, len(parts), vf, gpu_dn) + [
            "-q:v", "2", "-start_number", "1", os.path.join(dirs[k], "%08d.jpg")]
        try:
            dc._run_ffmpeg_progress(cmd, length, lambda f, m: pr.prep_frac.__setitem__(k, f), cancel, "")
            pr.prep_frac[k] = 1.0
        except BaseException as e:      # noqa: BLE001 - reported to the main loop
            errors.append(e)
            cancel.set()
        finally:
            finished[k] = time.time()
            done[k] = True
            if all(done):
                pr.prep_done_at = time.time()

    prep_threads = [threading.Thread(target=prep_part, args=(k,), daemon=True) for k in range(len(parts))]
    for t in prep_threads:
        t.start()

    def check():
        if errors:
            raise errors[0]
        if cancel.is_set():
            raise dc.Cancelled()

    # ---- encoder (runs alongside the AI when overlapping) ---------------------
    enc_q: queue.Queue = queue.Queue(maxsize=2)
    segs: list[str] = []

    def encode(folder, seg, cdir, nframes):
        t = time.time()
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-framerate", f"{out_fps:.6f}",
              "-start_number", "1", "-i", os.path.join(folder, "%08d.jpg"),
              "-vf", ",".join(post_filters(s, es)), *encoder_args(enc, es.quality), seg], cancel, "Encoding")
        shutil.rmtree(cdir, ignore_errors=True)
        with pr.lock:
            pr.enc_frames += nframes
            pr.enc_time += time.time() - t

    def encoder_loop():
        while True:
            item = enc_q.get()
            if item is None:
                return
            try:
                if not cancel.is_set():
                    encode(*item)
            except BaseException as e:   # noqa: BLE001
                errors.append(e)
                cancel.set()

    enc_thread = None
    if es.overlap:
        enc_thread = threading.Thread(target=encoder_loop, daemon=True)
        enc_thread.start()

    ok = False
    try:
        if not es.overlap:              # finish preparing every frame first
            while not all(done):
                check()
                pr.say("Preparing frames...")
                time.sleep(0.5)
        check()
        cur_part, cur_idx, consumed, k = 0, 1, 0, 0

        def take(n):
            nonlocal cur_part, cur_idx
            got = []
            last_say = 0.0
            while len(got) < n and cur_part < len(parts):
                check()
                f = os.path.join(dirs[cur_part], f"{cur_idx:08d}.jpg")
                nxt = os.path.join(dirs[cur_part], f"{cur_idx + 1:08d}.jpg")
                if os.path.exists(f) and (done[cur_part] or os.path.exists(nxt)):
                    got.append(f)
                    cur_idx += 1
                elif done[cur_part] and not os.path.exists(f):
                    cur_part, cur_idx = cur_part + 1, 1
                else:
                    if time.time() - last_say > 1.0:
                        pr.say("Preparing frames..." if not pr.batch_times else "waiting for frames...",
                               None if not pr.batch_times else k + 1)
                        last_say = time.time()
                    time.sleep(0.1)
            return got

        while True:
            batch = take(CHUNK_FRAMES)
            if not batch:
                break
            k += 1
            a, b = consumed, consumed + len(batch)
            cdir = os.path.join(tmp, f"chunk{k:04d}")
            cur = os.path.join(cdir, "in")
            os.makedirs(cur)
            for i, f in enumerate(batch, 1):
                os.replace(f, os.path.join(cur, f"{i:08d}.jpg"))
            tb = time.time()
            if es.ai_upscale:
                pr.say("AI upscaling", k)
                t = time.time()
                _upscale(cur, os.path.join(cdir, "up"), es, cancel)
                record_speed(keys["up"], (time.time() - t) / len(batch))
                shutil.rmtree(cur, ignore_errors=True)
                cur = os.path.join(cdir, "up")
            count = len(batch)
            if es.fps60:
                count = int(round(b * ratio)) - int(round(a * ratio))
                pr.say("smoothing to 60 fps", k)
                t = time.time()
                _interpolate(cur, os.path.join(cdir, "fps"), count,
                             uhd=es.fps_detail == "hd", cancel=cancel)
                record_speed(keys["rife"], (time.time() - t) / max(1, count))
                shutil.rmtree(cur, ignore_errors=True)
                cur = os.path.join(cdir, "fps")
            with pr.lock:
                pr.gpu_frames += len(batch)
                pr.gpu_time += time.time() - tb
                pr.batch_times.append(time.time() - tb)
            seg = os.path.join(tmp, f"seg{k:04d}.mp4")
            segs.append(seg)
            if es.overlap:
                while True:
                    check()
                    try:
                        enc_q.put((cur, seg, cdir, count), timeout=0.5)
                        break
                    except queue.Full:
                        pr.say("encoding", k)
            else:
                pr.say("encoding", k)
                encode(cur, seg, cdir, count)
            consumed = b
        if consumed == 0:
            raise RuntimeError("No frames to enhance.")
        if len(parts) == workers(es) and all(done):   # previews run as one part: not comparable
            record_speed(keys["prep"], consumed / max(0.1, max(finished) - t_prep))
        ok = True
    except BaseException:
        ok = False
        cancel.set()                    # stop the frame preparation still running in the background
        raise
    finally:
        if enc_thread is not None:
            # tell the encoder there is no more work: after the batches still queued (normal end),
            # or straight away (failure / cancel)
            while enc_thread.is_alive():
                if not ok:
                    _drain(enc_q)
                try:
                    enc_q.put(None, timeout=0.5)
                    break
                except queue.Full:
                    continue
            enc_thread.join(timeout=None if ok else 10)
        for t in prep_threads:
            t.join(timeout=10)
    check()
    if pr.enc_frames:
        record_speed(keys["enc"], pr.enc_frames / max(0.1, pr.enc_time))
    pr.say("joining", k)
    _join(segs, out_path, tmp, cancel)


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def _check_space(tmp: str, s: dc.Session, es: EnhanceSettings, duration: float, fps: float) -> None:
    ww, wh = work_size(s, es)
    ow, oh = output_size(s, es.height)
    frames = duration * fps
    need = frames * ww * wh * 0.3 + 3 * CHUNK_FRAMES * ow * oh * 0.4 * (3 if es.fps60 else 1) + 2e9
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
    gpu_dn = uses_gpu_denoise(es)
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
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", *(VK_ARGS if gpu_dn else []),
              "-f", "concat", "-safe", "0", "-i", lst,
              "-map", "0:v:0", "-an", "-vf", ",".join(pre_filters(s, es, src_fps, gpu_dn)), "-q:v", "2",
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
