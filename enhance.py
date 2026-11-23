"""
FPV DVR Stitcher - "Enhance video" export.

Cleans up analog FPV footage and exports an MP4 for sharing (upscaled, or at the original size):
  colour-speckle clean-up, grain denoise (processor or graphics card), sharpening,
  upscaling (standard Lanczos, or AI with Real-ESRGAN),
  optional smooth 60 fps (AI frame interpolation with RIFE),
  encoding on the graphics card (NVIDIA NVENC / AMD AMF / Intel QSV) with CPU fallbacks.

Repair: broken frames (torn / rolled / blown out by breakup) are replaced and short-lived
streak lines and colour-noise bands are filled in (repair.py), and an optional clean-up
model removes the rainbow colour noise of composite video (models.py, ONNX Runtime).

Speed: frames are prepared in parallel parts on all processor cores, and preparing,
AI work and encoding overlap instead of running one after another. Measured speeds
are remembered (speed.json) so time estimates fit the PC.

The AI tools (realesrgan-ncnn-vulkan, rife-ncnn-vulkan) run on any Vulkan GPU. They come
as an add-on: in the zip download next to the program, or downloaded on first use.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import queue
import re
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
import models as mdl
import repair

AI_ADDON_URL = (f"https://github.com/ShadowStacker/FPV-Video-Editor/releases/download/v{dc.APP_VERSION}/"
                f"FPV-DVR-Stitcher-{dc.APP_VERSION}-AI-addon.zip")
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
    fix_broken: bool = True      # replace broken frames (torn / rolled / blown out by breakup)
    fix_lines: bool = True       # fill in short-lived streak lines and colour-noise bands
    cleanup: str = "bleedout"    # clean-up models (models.CATALOG ids, comma separated; run in catalog order)
    ai_upscale: bool = True      # AI upscaling (else standard Lanczos)
    ai_model: str = "fast"       # fast | detailed (built-in) or an upscaler id from models.CATALOG
    keep_size: bool = False      # no upscaling: output at the recording's own size
    ai_res: str = "full"         # full: AI makes the output size | 1080: AI makes 1080p, then resize
    fps60: bool = False          # smooth 60 fps with RIFE frame interpolation
    fps_detail: str = "std"      # std | hd (RIFE high-resolution mode, slower)
    height: int = 1440           # output height; video sites encode 1440p uploads with more bitrate
    codec: str = "auto"          # auto | hevc | h264 | av1
    quality: int = 19            # constant quality (lower = better, bigger)
    overlap: bool = True         # prepare, AI-upscale and encode at the same time
    all_cores: bool = True       # prepare frames in parallel parts
    gpu_denoise: bool = True     # Medium/Strong denoise on the graphics card (Vulkan)


PRESETS = {
    "quick": dict(speckle=True, denoise=1, sharpen=1, fix_broken=True, fix_lines=True, cleanup="",
                  keep_size=False, ai_upscale=False, ai_model="fast", ai_res="full"),
    "balanced": dict(speckle=True, denoise=1, sharpen=0, fix_broken=True, fix_lines=True, cleanup="bleedout",
                     keep_size=False, ai_upscale=True, ai_model="fast", ai_res="full"),
    "best": dict(speckle=True, denoise=1, sharpen=0, fix_broken=True, fix_lines=True, cleanup="bleedout",
                 keep_size=False, ai_upscale=True, ai_model="detailed", ai_res="full"),
}
BUILTIN_UPSCALERS = {"fast": "Fast (built-in, anime-video model)", "detailed": "Detailed (built-in, Real-ESRGAN x4plus)"}
PRESET_ALIASES: dict[str, str] = {}   # old preset names -> current ones


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
               if uses_ai(es) or k not in ("ai_model", "ai_res")):
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
    es.cleanup = ",".join(cleanups(es))
    if es.ai_model not in BUILTIN_UPSCALERS and es.ai_model not in mdl.BY_ID:
        es.ai_model = "fast"
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
# Models and repair
# ---------------------------------------------------------------------------
def cleanups(es: EnhanceSettings) -> list[str]:
    """The clean-up models to run, in the order they run (the catalog's order)."""
    want = {x.strip() for x in (es.cleanup or "").split(",") if x.strip()}
    return [m.id for m in mdl.clean_models() if m.id in want]


def uses_ai(es: EnhanceSettings) -> bool:
    return es.ai_upscale and not es.keep_size


def ai_scale(es: EnhanceSettings) -> int:
    m = mdl.BY_ID.get(es.ai_model)
    return m.scale if m else 4


def uses_ncnn_upscaler(es: EnhanceSettings) -> bool:
    return uses_ai(es) and es.ai_model in BUILTIN_UPSCALERS


def needs_addon(es: EnhanceSettings) -> bool:
    """The AI add-on (Real-ESRGAN / RIFE programs) is needed for the built-in upscalers and 60 fps."""
    return uses_ncnn_upscaler(es) or es.fps60


def models_needed(es: EnhanceSettings) -> list[str]:
    ids = cleanups(es)
    if uses_ai(es) and es.ai_model in mdl.BY_ID:
        ids.append(es.ai_model)
    return ids


def missing_models(es: EnhanceSettings) -> list[str]:
    return [m for m in models_needed(es) if not mdl.installed(m)]


def needs_frames(es: EnhanceSettings) -> bool:
    """Does the export pass every frame through the program (repair / clean-up model)?"""
    return es.fix_broken or es.fix_lines or bool(cleanups(es))


def check_ready(es: EnhanceSettings) -> None:
    if needs_addon(es) and not ai_installed():
        raise RuntimeError("The built-in AI upscalers and smooth 60 fps need the AI add-on.\n"
                           "Use Enhance > Download AI add-on, or switch them off.")
    miss = missing_models(es)
    if miss:
        raise RuntimeError("Download the model first: " + ", ".join(mdl.BY_ID[m].name for m in miss))


# ---------------------------------------------------------------------------
# Sizes and filters
# ---------------------------------------------------------------------------
def output_size(s: dc.Session, height: int) -> tuple[int, int]:
    c = s.clips[0]
    sw, sh = (c.width or 640), (c.height or 480)
    return int(round(height * sw / sh / 2)) * 2, int(height)


def out_dims(s: dc.Session, es: EnhanceSettings) -> tuple[int, int]:
    """Size of the exported video: the recording's own size with 'No upscaling', else the chosen height."""
    if es.keep_size:
        c = s.clips[0]
        w, h = (c.width or 640), (c.height or 480)
        return w - w % 2, h - h % 2
    return output_size(s, es.height)


def ai_out_height(es: EnhanceSettings) -> int:
    return es.height if es.ai_res == "full" else min(es.height, 1080)


def work_size(s: dc.Session, es: EnhanceSettings) -> tuple[int, int]:
    """Resolution the frames are cleaned at (and handed to the AI tools)."""
    ow, oh = out_dims(s, es)
    if uses_ai(es):                         # the AI upscales 4x (2x for some models)
        k = ai_scale(es)
        ah = ai_out_height(es)
        aw = int(round(ah * ow / oh / 2)) * 2
        return int(round(aw / k / 2)) * 2, int(round(ah / k / 2)) * 2
    c = s.clips[0]
    w, h = (c.width or 640), (c.height or 480)
    if h > oh:
        return ow, oh
    return w - w % 2, h - h % 2


CLEAN_MAX_H = 720       # the clean-up model works on the recording's own size, up to 720p


def clean_size(s: dc.Session) -> tuple[int, int]:
    c = s.clips[0]
    w, h = (c.width or 640), (c.height or 480)
    if h > CLEAN_MAX_H:
        w, h = int(round(w * CLEAN_MAX_H / h / 2)) * 2, CLEAN_MAX_H
    return w - w % 2, h - h % 2


def stage_size(s: dc.Session, es: EnhanceSettings) -> tuple[int, int]:
    """Resolution the repair and clean-up step works at."""
    return clean_size(s) if cleanups(es) else work_size(s, es)


def pre_filters(s: dc.Session, es: EnhanceSettings, fps: float, gpu_dn: bool = False) -> list[str]:
    ww, wh = work_size(s, es)
    c = s.clips[0]
    f = ["setpts=PTS-STARTPTS", f"fps={fps:.6f}"]     # constant frame rate from the first frame (DVRs skip frames)
    if (ww, wh) != (c.width, c.height):
        f.append(f"scale={ww}:{wh}:flags=area")
    return f + clean_filters(es, gpu_dn)


def clean_filters(es: EnhanceSettings, gpu_dn: bool = False) -> list[str]:
    f = []
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
    ow, oh = out_dims(s, es)
    f = [f"scale={ow}:{oh}:flags=lanczos"]
    if es.sharpen == 1:
        f.append("cas=strength=0.35")
    elif es.sharpen >= 2:
        f.append("cas=strength=0.65")
    return f


def prep_parts(es: EnhanceSettings) -> int:
    """Parallel frame-preparation parts. Denoise on the graphics card shares it with the AI programs,
    so fewer parts run at once then (several at once can run the card out of memory)."""
    n = workers(es)
    if uses_gpu_denoise(es) and es.overlap and (es.fps60 or uses_ncnn_upscaler(es)):
        n = min(n, 2)
    return n


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
    rife_h = ai_out_height(es) if uses_ai(es) else wh
    rep = f"{int(es.fix_broken)}{int(es.fix_lines)}{es.cleanup or '-'}"
    oh = out_dims(s, es)[1]
    return {
        "prep": f"prep|{es.speckle}|{es.denoise}|{gpu}|{prep_parts(es)}|{wh}|{rep}",
        "clean": f"clean|{es.cleanup}|{stage_size(s, es)[1]}",
        "up": f"up|{es.ai_model}|{ai_out_height(es)}",
        "rife": f"rife|{es.fps_detail}|{rife_h}",
        "enc": f"enc|{enc}|{oh}",
        "quick": f"quick|{es.speckle}|{es.denoise}|{gpu}|{quick_parts(es, enc)}|{oh}|{es.sharpen}|{enc}|{rep}",
    }


def _defaults(s: dc.Session, es: EnhanceSettings) -> dict:
    """First-guess speeds (before anything was measured on this PC), sized for an RTX 4070 class GPU.
    Units: prep = frames/s, clean/up = s/frame, rife = s/output frame, enc = frames/s,
    quick = s per s of video."""
    ww, wh = work_size(s, es)
    pix = (ww * wh) / (640 * 360)
    cpu_cost = 1.0 + (0.3 if es.speckle else 0) + {0: 0, 1: 0.5, 2: 6.0, 3: 15.0}[min(3, es.denoise)]
    if uses_gpu_denoise(es):
        cpu_cost = 1.3 + (0.3 if es.speckle else 0)
    cpu_cost *= max(0.3, pix)
    if needs_frames(es):          # frames pass through the program (copying, repair)
        sw, sh = stage_size(s, es)
        cpu_cost += (0.4 + (0.8 if es.fix_lines else 0) + (0.2 if es.fix_broken else 0)) * (sw * sh) / (640 * 360)
    cpu_cost /= (2.5 if es.all_cores else 1.0)
    prep_fps = 10200 / (cpu_cost * 60)
    sw, sh = stage_size(s, es)
    clean = sum(mdl.BY_ID[m].cost for m in cleanups(es)) * sw * sh / 1e6
    ah = ai_out_height(es)
    um = mdl.BY_ID.get(es.ai_model)
    if um:
        up = um.cost * ww * wh / 1e6
    else:
        up = (0.3235 if es.ai_model == "detailed" else 0.047) * (ah / 1440) ** 2
    rh = ah if uses_ai(es) else wh
    rife = 0.0333 * (rh / 1440) ** 2 * (1.7 if es.fps_detail == "hd" else 1.0)
    oh = out_dims(s, es)[1]
    enc = 10200 / (2.0 * 60) / (oh / 1440) ** 2
    quick = max(0.35, cpu_cost * (2.5 if es.all_cores else 1.0) / 6 + 0.4 * (oh / 1440) ** 2)
    return {"prep": prep_fps, "clean": clean, "up": up, "rife": rife, "enc": enc, "quick": quick}


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

    clean = frames * sp("clean") if cleanups(es) else 0.0
    if not (uses_ai(es) or es.fps60):
        q = dur * sp("quick")
        t = max(q, clean * 1.05)                 # the model runs alongside the processor work
        gpu = ([("clean", 0, clean)] if clean else []) + [("enc", 0, t)]
        return {"total": t, "cpu": [("prep", 0, t)], "gpu": gpu, "measured": measured,
                "frames": frames, "batches": 1, "prep": t, "clean": clean, "up": 0.0, "rife": 0.0, "encode": t}
    prep = frames / sp("prep", counts=not es.overlap)    # with overlap, preparing hides behind the AI
    prep_all = max(prep, clean)
    up = frames * sp("up") if uses_ai(es) else 0.0
    rife = out_frames * sp("rife") if es.fps60 else 0.0
    encode = out_frames / sp("enc")
    nb = max(1, math.ceil(frames / CHUNK_FRAMES))
    lanes_clean = [("clean", 0, clean)] if clean else []
    if es.overlap:
        start = min(prep_all, prep_all / nb + 2.0)
        if clean:                                  # the clean-up model shares the graphics card
            up_end = max(prep_all, start + up + rife + clean * (1 - 1 / nb))
        else:
            up_end = max(prep_all, start + up + rife)
        total = up_end + encode / nb
        gpu = lanes_clean + [("ai", up_end - up - rife, up_end - rife), ("fps", up_end - rife, up_end),
                             ("enc", total - encode / nb, total)]
    else:
        total = prep_all + up + rife + encode
        gpu = lanes_clean + [("ai", prep_all, prep_all + up), ("fps", prep_all + up, prep_all + up + rife),
                             ("enc", prep_all + up + rife, total)]
    return {"total": total, "cpu": [("prep", 0, prep)], "gpu": gpu, "measured": measured,
            "frames": frames, "batches": nb, "prep": prep_all, "clean": clean, "up": up, "rife": rife,
            "encode": encode}


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
        text = (err[0] if err else b"").decode("utf-8", "replace")
        # ncnn tools (Real-ESRGAN, RIFE) list the graphics card first: not part of the error
        lines = [ln for ln in text.splitlines() if ln.strip() and not re.match(r"^\[\d+ .*\]\s", ln)]
        msg = "\n".join(lines)[-2500:] or ("It printed no error message, which usually means it crashed or ran out "
                                           "of graphics card memory.")
        raise RuntimeError(f"{what or os.path.basename(cmd[0])} failed ({exit_text(proc.returncode)}):\n{msg}")


_EXIT_CODES = {0xC0000005: "crashed (access violation)", 0xC0000409: "crashed (stack buffer overrun)",
               0xC00000FD: "crashed (stack overflow)", 0xC0000017: "out of memory",
               0xC000012D: "out of memory", 0xE06D7363: "crashed (unhandled exception)",
               0xC0000135: "a DLL it needs is missing", 0xC000001D: "crashed (illegal instruction)"}


def exit_text(code: int) -> str:
    u = code & 0xFFFFFFFF
    if u < 256:
        return f"exit code {u}"
    return f"exit code 0x{u:08X}" + (f": {_EXIT_CODES[u]}" if u in _EXIT_CODES else "")


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
    if es.ai_model in mdl.BY_ID:
        _upscale_onnx(src, dst, mdl.load(es.ai_model), cancel)
        return
    exe = find_ai("realesrgan")
    if exe is None:
        raise RuntimeError("AI upscaling needs the AI add-on (Enhance > Download AI add-on).")
    os.makedirs(dst, exist_ok=True)
    n = len(os.listdir(src))
    _run([exe, "-i", src, "-o", dst, "-n", AI_MODELS.get(es.ai_model, AI_MODELS["fast"]), "-s", "4",
          "-f", "jpg", "-m", os.path.join(os.path.dirname(exe), "models")], cancel, "AI upscaling (Real-ESRGAN)")
    _sequence(dst, n, "AI upscaling")


def _upscale_onnx(src: str, dst: str, model, cancel=None) -> None:
    """Upscale a folder of frames with an ONNX model; pictures are read and written on other
    threads while the graphics card works."""
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    os.makedirs(dst, exist_ok=True)
    files = sorted(f for f in os.listdir(src) if f.lower().endswith(".jpg"))

    def load(name):
        with Image.open(os.path.join(src, name)) as im:
            return np.asarray(im.convert("RGB"))

    def save(arr, k):
        Image.fromarray(arr).save(os.path.join(dst, f"{k:08d}.jpg"), quality=95)

    with ThreadPoolExecutor(max_workers=4) as io:
        ahead = 4
        loads = [io.submit(load, f) for f in files[:ahead]]
        saves = []
        for k in range(len(files)):
            if cancel is not None and cancel.is_set():
                raise dc.Cancelled()
            img = loads[k].result()
            loads[k] = None
            if k + ahead < len(files):
                loads.append(io.submit(load, files[k + ahead]))
            saves.append(io.submit(save, model.run(img), k + 1))
            while len(saves) > 8:
                saves.pop(0).result()
        for f in saves:
            f.result()


def _interpolate(src: str, dst: str, count: int, uhd: bool, cancel=None) -> None:
    exe = find_ai("rife")
    if exe is None:
        raise RuntimeError("Smooth 60 fps needs the AI add-on (Enhance > Download AI add-on).")
    os.makedirs(dst, exist_ok=True)
    files = sorted(f for f in os.listdir(src) if f.lower().endswith(".jpg"))
    if len(files) < 2:          # RIFE crashes on a folder with one picture: nothing to interpolate anyway
        for k in range(1, max(1, count) + 1):
            shutil.copyfile(os.path.join(src, files[0]), os.path.join(dst, f"{k:08d}.jpg"))
        return
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


def _src_cmd(lst: str, a: float, length: float, n_parts: int, fps: float, size: tuple[int, int]) -> list[str]:
    """Decode (part of) the export at a constant frame rate as raw RGB frames on stdout."""
    cmd = [dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin"]
    if n_parts > 1:
        cmd += ["-ss", f"{a:.4f}", "-t", f"{length:.4f}"]
    return cmd + ["-f", "concat", "-safe", "0", "-i", lst, "-map", "0:v:0", "-an", "-sn", "-dn",
                  "-vf", f"setpts=PTS-STARTPTS,fps={fps:.6f},scale={size[0]}:{size[1]}:flags=area,format=rgb24",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def _sink_cmd(size: tuple[int, int], fps: float, vf: list[str], gpu: bool) -> list[str]:
    """Take raw RGB frames on stdin and filter them (output options are added by the caller)."""
    cmd = [dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", *(VK_ARGS if gpu else []),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-framerate", f"{fps:.6f}",
           "-i", "pipe:0"]
    return cmd + (["-vf", ",".join(vf)] if vf else [])


class _FrameCtx:
    """Repair and clean-up shared by the parallel parts of one export."""

    def __init__(self, es: EnhanceSettings, fps: float, hints: np.ndarray | None, models: list):
        self.es, self.fps, self.hints, self.models = es, fps, hints, models
        self.chains: dict[int, _Chain] = {}
        self._sets: dict[int, tuple[int, set]] = {}

    def chain(self, part: int, start: float) -> "_Chain":
        j0 = int(round(start * self.fps))
        hints = self.hints

        def hint(i):
            j = j0 + i
            return True if hints is None or not 0 <= j < len(hints) else bool(hints[j])
        ch = _Chain(self.es, hint, self.models)
        self.chains[part] = ch
        return ch

    def runs(self, part: int) -> list:
        ch = self.chains.get(part)
        return list(ch.broken.runs) if ch is not None and ch.broken is not None else []

    def replaced(self, part: int, idx: int) -> bool:
        """Was frame idx (1-based, as saved) of a part replaced?"""
        runs = self.runs(part)
        n, st = self._sets.get(part, (0, set()))
        for first, r, _a, _b in runs[n:]:
            st.update(range(first + 1, first + r + 1))
        self._sets[part] = (len(runs), st)
        return idx in st

    def stats(self) -> tuple[int, int, int]:
        """(frames replaced, frames with lines repaired, frames through the model)"""
        rep = sum(c.broken.replaced for c in self.chains.values() if c.broken is not None)
        lines = sum(c.lines.fixed_frames for c in self.chains.values() if c.lines is not None)
        return rep, lines, sum(c.model_frames for c in self.chains.values())


class _Chain:
    def __init__(self, es: EnhanceSettings, hint, models: list):
        self.broken = repair.BrokenFrames(hint) if es.fix_broken else None
        self.lines = repair.LineRepair() if es.fix_lines else None
        self.models = models
        self.model_frames = 0

    def _finish(self, f: np.ndarray) -> np.ndarray:
        if self.lines is not None:
            f = self.lines(f)
        for m in self.models:            # clean-up models one after another
            f = m.run(f)
        if self.models:
            self.model_frames += 1
        return f

    def push(self, f: np.ndarray) -> list[np.ndarray]:
        return [self._finish(x) for x in (self.broken.push(f) if self.broken is not None else [f])]

    def flush(self) -> list[np.ndarray]:
        return [self._finish(x) for x in (self.broken.flush() if self.broken is not None else [])]


def _pump(src_cmd: list[str], sink_cmd: list[str], size: tuple[int, int], chain: _Chain, cancel,
          tick=None) -> int:
    """ffmpeg (decode) -> repair / clean-up in Python -> ffmpeg (filter, save or encode)."""
    w, h = size
    fsz = w * h * 3
    pa = subprocess.Popen(src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          bufsize=fsz * 2, **dc._popen_kwargs())
    pb = subprocess.Popen(sink_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          **dc._popen_kwargs())
    errs: dict[str, list] = {"a": [], "b": []}
    threads = [threading.Thread(target=lambda p=p, k=k: errs[k].append(p.stderr.read()), daemon=True)
               for p, k in ((pa, "a"), (pb, "b"))]
    for t in threads:
        t.start()
    n = 0
    broken_pipe = False

    def put(frames):
        for f in frames:
            pb.stdin.write(np.ascontiguousarray(f).data)

    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise dc.Cancelled()
            buf = pa.stdout.read(fsz)
            if not buf or len(buf) < fsz:
                break
            put(chain.push(np.frombuffer(buf, np.uint8).reshape(h, w, 3)))
            n += 1
            if tick:
                tick(n)
        put(chain.flush())
        pb.stdin.close()
        pa.wait()
        pb.wait()
    except BrokenPipeError:
        broken_pipe = True
    finally:
        for p in (pa, pb):
            if p.poll() is None:
                try:
                    p.kill()
                except OSError:
                    pass
                p.wait()
        for t in threads:
            t.join(timeout=5)
    if cancel is not None and cancel.is_set():
        raise dc.Cancelled()
    if broken_pipe or pa.returncode != 0 or pb.returncode != 0:
        msg = b"".join(errs["b"] + errs["a"]).decode("utf-8", "replace")[-2500:]
        raise RuntimeError("Preparing frames failed:\n" + msg)
    return n


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
    check_ready(es)
    cancel = cancel or threading.Event()
    src_fps = float(np.median([c.fps for c in s.clips]))
    out_fps = 60.0 if es.fps60 else src_fps
    enc = pick_encoder(es.codec)
    gpu_dn = uses_gpu_denoise(es)
    ow, oh = out_dims(s, es)
    t_start = time.time()
    tmp = dc._tmpdir_for(out_path)
    keys = _keys(s, es, enc)
    ctx = None
    info = {}
    try:
        lst = os.path.join(tmp, "concat.txt")
        duration = dc.write_concat_list(pieces, lst, tmp, cancel)
        est = estimate(s, es, enc, seconds=duration)
        if needs_frames(es):
            if progress and cleanups(es):
                progress(0.0, "Loading the clean-up models...")
            models = [mdl.load(m) for m in cleanups(es)]
            if models:
                info["cleanup_device"] = models[0].device
                busy0, frames0 = sum(m.busy for m in models), models[0].frames
            hints = dc.repair_hints(s, keep, src_fps, int(duration * src_fps) + 2) if es.fix_broken else None
            ctx = _FrameCtx(es, src_fps, hints, models)
        if not (uses_ai(es) or es.fps60):
            n = _quick(s, es, enc, lst, duration, src_fps, gpu_dn, out_path, tmp, progress, cancel, ctx)
            if n == quick_parts(es, enc):         # short previews run as one part: not comparable
                record_speed(keys["quick"], (time.time() - t_start) / max(1.0, duration))
        else:
            _check_space(tmp, s, es, duration, src_fps)
            _ai_pipeline(s, es, enc, lst, duration, src_fps, out_fps, gpu_dn, out_path, tmp, est,
                         keys, progress, cancel, ctx)
        if ctx is not None and ctx.models and ctx.models[0].frames > frames0:
            record_speed(keys["clean"], (sum(m.busy for m in ctx.models) - busy0) / (ctx.models[0].frames - frames0))
        if t_range is None:
            _add_chapters(s, out_path, tmp, cancel)
            dc.save_chapters_txt(s, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(1.0, "Done.")
    if ctx is not None:
        info["replaced"], info["lines"], _ = ctx.stats()
    return {"path": out_path, "encoder": ENCODER_NAMES.get(enc, enc), "size": (ow, oh),
            "fps": out_fps, "seconds": time.time() - t_start, "duration": duration,
            "gpu_denoise": gpu_dn, **info}


def _add_chapters(s: dc.Session, out_path: str, tmp: str, cancel) -> None:
    """Put the chapter markers into the finished MP4 (a quick copy, no re-encoding)."""
    meta = os.path.join(tmp, "chapters.txt")
    if dc.write_chapters(s, meta) == 0:
        return
    tmp_out = os.path.join(tmp, "with_chapters.mp4")
    try:
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", out_path, "-i", meta, "-map", "0",
              "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", tmp_out],
             cancel, "Adding chapters")
        os.replace(tmp_out, out_path)
    except RuntimeError:
        pass                    # chapters are a nice-to-have: keep the video without them


def quick_parts(es: EnhanceSettings, enc: str) -> int:
    return min(workers(es), 3) if is_hw_encoder(enc) else workers(es)


def _quick(s, es, enc, lst, duration, src_fps, gpu_dn, out_path, tmp, progress, cancel, ctx=None) -> int:
    """No AI: clean, resize and encode in one pass, split into parallel parts."""
    vf = pre_filters(s, es, src_fps, gpu_dn) + post_filters(s, es)
    n = quick_parts(es, enc)                 # consumer graphics cards limit parallel encodes
    parts = _parts(duration, n if duration > 20 else 1)
    fracs = [0.0] * len(parts)
    t0 = time.time()
    size = stage_size(s, es)
    ww, wh = work_size(s, es)
    vf_b = ([f"scale={ww}:{wh}:flags=area"] if size != (ww, wh) else []) + clean_filters(es, gpu_dn) \
        + post_filters(s, es)

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
        tail = [*encoder_args(enc, es.quality), "-movflags", "+faststart", out]
        if ctx is not None:
            expect = max(1.0, length * src_fps)

            def tick(i):
                fracs[k] = min(1.0, i / expect)
                if i % 10 == 0:
                    report()
            _pump(_src_cmd(lst, a, length, len(parts), src_fps, size),
                  _sink_cmd(size, src_fps, vf_b, gpu_dn) + tail, size, ctx.chain(k, a), cancel, tick)
            return out
        cmd = _part_cmd(lst, a, length, len(parts), vf, gpu_dn) + tail

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
                 progress, cancel, ctx=None) -> None:
    """Prepare frames (parallel parts) -> AI upscale / 60 fps in batches -> encode, overlapping."""
    ratio = out_fps / src_fps
    pr = _Progress(progress, cancel, est, es, ratio)
    parts = _parts(duration, prep_parts(es) if duration > 20 else 1)
    pr.prep_frac = [0.0] * len(parts)
    dirs = [os.path.join(tmp, f"prep{k:02d}") for k in range(len(parts))]
    done = [False] * len(parts)
    finished = [0.0] * len(parts)
    errors: list[BaseException] = []
    vf = pre_filters(s, es, src_fps, gpu_dn)
    size = stage_size(s, es)
    ww, wh = work_size(s, es)
    vf_b = ([f"scale={ww}:{wh}:flags=area"] if size != (ww, wh) else []) + clean_filters(es, gpu_dn)
    t_prep = time.time()
    rife_exe = find_ai("rife") if (ctx is not None and es.fix_broken) else None

    def prep_part(k):
        a, length = parts[k]
        os.makedirs(dirs[k], exist_ok=True)
        out = ["-q:v", "2", *dc.passthrough_args(), "-start_number", "1", os.path.join(dirs[k], "%08d.jpg")]
        try:
            if ctx is not None:
                expect = max(1.0, length * src_fps)
                _pump(_src_cmd(lst, a, length, len(parts), src_fps, size),
                      _sink_cmd(size, src_fps, vf_b, gpu_dn) + out, size, ctx.chain(k, a), cancel,
                      lambda i: pr.prep_frac.__setitem__(k, min(1.0, i / expect)))
            else:
                cmd = _part_cmd(lst, a, length, len(parts), vf, gpu_dn) + out[:2] + out[-3:]
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

    enc_busy = [False]

    def encoder_loop():
        while True:
            item = enc_q.get()
            if item is None:
                return
            try:
                if not cancel.is_set():
                    enc_busy[0] = True
                    encode(*item)
            except BaseException as e:   # noqa: BLE001
                errors.append(e)
                cancel.set()
            finally:
                enc_busy[0] = False

    def gpu_step(fn, dst, what, batch):
        """Run an AI step; if it fails, wait until the encoder has finished with the graphics card
        and try that batch once more on its own."""
        try:
            fn()
        except dc.Cancelled:
            raise
        except RuntimeError as first:
            if cancel.is_set():
                raise
            pr.say(f"{what} failed, trying batch {batch} again", batch)
            until = time.time() + 900
            while (not enc_q.empty() or enc_busy[0]) and time.time() < until:
                check()
                time.sleep(0.2)
            shutil.rmtree(dst, ignore_errors=True)
            try:
                fn()
            except RuntimeError as second:
                raise RuntimeError(f"{second}\n\nIt failed twice on batch {batch}. The first time:\n"
                                   f"{str(first)[:600]}") from None

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

        def in_run():
            """Is the batch's last frame replaced, or the good frame just before a replaced one?
            (batches never split a run from the good frames either side, so RIFE can redraw it)"""
            if ctx is None or not got_last:
                return False
            part, idx = got_last[-1]
            return ctx.replaced(part, idx) or ctx.replaced(part, idx + 1)

        got_last: list = []

        def take(n):
            nonlocal cur_part, cur_idx
            got = []
            last_say = 0.0
            while (len(got) < n or in_run()) and cur_part < len(parts):
                check()
                f = os.path.join(dirs[cur_part], f"{cur_idx:08d}.jpg")
                nxt = os.path.join(dirs[cur_part], f"{cur_idx + 1:08d}.jpg")
                if os.path.exists(f) and (done[cur_part] or os.path.exists(nxt)):
                    got.append((f, cur_part, cur_idx))
                    got_last[:] = [(cur_part, cur_idx)]
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
            pos = {}
            for i, (f, part, idx) in enumerate(batch, 1):
                os.replace(f, os.path.join(cur, f"{i:08d}.jpg"))
                pos[(part, idx)] = os.path.join(cur, f"{i:08d}.jpg")
            tb = time.time()
            if rife_exe:
                fills = []
                for part in sorted({p for _, p, _ in batch}):
                    for first, r, ga, gb in ctx.runs(part):
                        want = [(part, first + 1 + j) for j in range(r)] + [(part, ga + 1), (part, gb + 1)]
                        if all(w in pos for w in want):
                            fills.append(([pos[w] for w in want[:r]], pos[want[r]], pos[want[r + 1]]))
                if fills:
                    pr.say("redrawing broken frames", k)
                    repair.rife_fill(cdir, fills, rife_exe, os.path.join(os.path.dirname(rife_exe), RIFE_MODEL),
                                     _run, cancel)
            if uses_ai(es):
                pr.say("AI upscaling", k)
                t = time.time()
                gpu_step(lambda: _upscale(cur, os.path.join(cdir, "up"), es, cancel), os.path.join(cdir, "up"),
                         "AI upscaling", k)
                record_speed(keys["up"], (time.time() - t) / len(batch))
                shutil.rmtree(cur, ignore_errors=True)
                cur = os.path.join(cdir, "up")
            count = len(batch)
            if es.fps60:
                count = int(round(b * ratio)) - int(round(a * ratio))
                pr.say("smoothing to 60 fps", k)
                t = time.time()
                src_dir, n_out = cur, count
                gpu_step(lambda: _interpolate(src_dir, os.path.join(cdir, "fps"), n_out,
                                              uhd=es.fps_detail == "hd", cancel=cancel),
                         os.path.join(cdir, "fps"), "Smooth 60 fps", k)
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
        if len(parts) == prep_parts(es) and all(done):   # previews run as one part: not comparable
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
    ow, oh = out_dims(s, es)
    frames = duration * fps
    need = frames * ww * wh * 0.3 + 3 * CHUNK_FRAMES * ow * oh * 0.4 * (3 if es.fps60 else 1) + 2e9
    free = shutil.disk_usage(tmp).free
    if free < need:
        raise RuntimeError(f"Not enough free disk space next to the output file: about "
                           f"{need / 1e9:.0f} GB of temporary space is needed, {free / 1e9:.0f} GB is free.")


def _render_frame(s: dc.Session, t: float, es: EnhanceSettings, work: str, cancel=None) -> str:
    """One finished frame at moment t with these settings (PNG path, at the export's size)."""
    os.makedirs(work, exist_ok=True)
    gpu_dn = uses_gpu_denoise(es)
    src_fps = float(np.median([c.fps for c in s.clips]))
    keep = np.zeros(s.n, bool)
    keep[(s.t >= t - 0.5) & (s.t < t + 0.5)] = True
    pieces = dc.build_pieces(s, keep)
    if not pieces:
        raise RuntimeError("No picture at that moment.")
    lst = os.path.join(work, "concat.txt")
    dc.write_concat_list(pieces, lst, work, cancel)
    pre = os.path.join(work, "pre")
    os.makedirs(pre, exist_ok=True)
    if needs_frames(es):
        size = stage_size(s, es)
        ww, wh = work_size(s, es)
        vf_b = ([f"scale={ww}:{wh}:flags=area"] if size != (ww, wh) else []) + clean_filters(es, gpu_dn)
        ctx = _FrameCtx(es, src_fps, None, [mdl.load(m) for m in cleanups(es)])
        _pump(_src_cmd(lst, 0, 0, 1, src_fps, size),
              _sink_cmd(size, src_fps, vf_b, gpu_dn) + ["-q:v", "2", *dc.passthrough_args(), "-start_number",
                                                        "1", os.path.join(pre, "%08d.jpg")],
              size, ctx.chain(0, 0.0), cancel)
    else:
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", *(VK_ARGS if gpu_dn else []),
              "-f", "concat", "-safe", "0", "-i", lst,
              "-map", "0:v:0", "-an", "-vf", ",".join(pre_filters(s, es, src_fps, gpu_dn)), "-q:v", "2",
              "-start_number", "1", os.path.join(pre, "%08d.jpg")], cancel, "Preparing frame")
    names = sorted(os.listdir(pre))
    if not names:
        raise RuntimeError("No picture at that moment.")
    mid = names[len(names) // 2]          # temporal filters need neighbouring frames
    one = os.path.join(work, "one")
    os.makedirs(one, exist_ok=True)
    shutil.copy(os.path.join(pre, mid), os.path.join(one, "00000001.jpg"))
    cur = one
    if uses_ai(es):
        _upscale(one, os.path.join(work, "up"), es, cancel)
        cur = os.path.join(work, "up")
    after = os.path.join(work, "after.png")
    _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", os.path.join(cur, "00000001.jpg"),
          "-vf", ",".join(post_filters(s, es)), "-frames:v", "1", after], cancel, "Finishing frame")
    return after


def _original_frame(s: dc.Session, t: float, size: tuple[int, int], path: str, cancel=None) -> str:
    c, ts = s.locate(float(t))
    _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-ss", f"{max(0.0, ts):.4f}", "-i", c.path,
          "-frames:v", "1", "-vf", f"scale={size[0]}:{size[1]}:flags=bicubic", path], cancel, "Original frame")
    return path


def compare_frame(s: dc.Session, t: float, es: EnhanceSettings, cancel=None) -> bytes:
    """A before/after picture of one moment: full frames on top, a 100% crop below (PNG)."""
    check_ready(dataclasses.replace(es, fps60=False))
    os.makedirs(APP_DATA, exist_ok=True)
    work = os.path.join(APP_DATA, "compare")
    shutil.rmtree(work, ignore_errors=True)
    try:
        after = _render_frame(s, t, es, work, cancel)
        ow, oh = out_dims(s, es)
        before = _original_frame(s, t, (ow, oh), os.path.join(work, "before.png"), cancel)
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


TILE_W = 480


def compare_tile(s: dc.Session, t: float, es: EnhanceSettings | None, cancel=None, whole: bool = False) -> bytes:
    """The middle of one frame (the same part of the picture for every setup; the whole frame with
    whole=True), TILE_W wide, as PNG. es=None gives the original recording."""
    os.makedirs(APP_DATA, exist_ok=True)
    work = os.path.join(APP_DATA, f"tile_{threading.get_ident()}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        if es is None:
            c = s.clips[0]
            src = _original_frame(s, t, ((c.width or 640) // 2 * 2, (c.height or 480) // 2 * 2),
                                  os.path.join(work, "orig.png"), cancel)
        else:
            check_ready(dataclasses.replace(es, fps60=False))
            src = _render_frame(s, t, es, work, cancel)
        out = os.path.join(work, "tile.png")
        _run([dc.tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", src, "-vf",
              ("" if whole else "crop=iw*0.4:ih*0.4:iw*0.3:ih*0.3,") + f"scale={TILE_W}:-2:flags=bicubic",
              "-frames:v", "1", out],
             cancel, "Building comparison")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def preview_path() -> str:
    d = os.path.join(APP_DATA, "previews")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"preview_{time.strftime('%H%M%S')}.mp4")
