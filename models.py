"""
FPV DVR Stitcher - extra AI models, run with ONNX Runtime on the graphics card (DirectML).

Two kinds:
  clean-up (1x)  removes the rainbow colour noise, colour bleeding and dot crawl of analog
                 (composite) video before anything else is done to the picture;
  upscalers      more choices for AI upscaling next to the built-in Fast / Detailed models.

There are no models trained on FPV drone footage; these are the community models trained
for analog / VHS / composite video, which has the same faults. Every model is free to use
(licences below). The build converts them from PyTorch to ONNX (tools/convert_models.py)
and attaches them to each release; the app downloads one the first time it is used.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass

import numpy as np

import dvr_core as dc

REPO = "ShadowStacker/FPV-Video-Editor"
APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"),
                        "FPV DVR Stitcher")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    kind: str            # "clean" (1x) or "upscale"
    name: str            # shown in the Enhance window
    scale: int
    what: str            # hover help: what it does
    good: str            # hover help: when to use it
    speed: str           # "fast", "medium" or "slow"
    cost: float          # graphics-card seconds per megapixel of input on an RTX 4070 class card (first guess)
    size_mb: float       # download size (both precisions)
    author: str
    licence: str
    source: str          # original model file (converted by tools/convert_models.py)
    page: str            # where it is described
    tile: int = 0        # run in tiles of this size (0 = whole frame)
    personal: bool = False   # licence allows non-commercial use only
    sha256: str = ""     # of the original file (checked by tools/convert_models.py)
    src_name: str = ""   # file name for the original when the address has none (e.g. Google Drive)


CATALOG: list[ModelInfo] = [
    ModelInfo("bleedout", "clean", "Rainbows & colour bleed (BleedOut)", 1,
              "Removes the rainbow colour noise, colour bleeding and colour smear of composite "
              "video, and keeps the detail.",
              "Recommended for analog FPV footage. Use it on every export.",
              "fast", 0.035, 3.6, "zarxrax", "WTFPL",
              "https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax6ygfvpvzka/b/open-modeldb-files/"
              "o/1x-BleedOut-Compact.pth", "https://openmodeldb.info/models/1x-BleedOut-Compact"),
    ModelInfo("dotzilla", "clean", "Dot crawl & rainbows (Dotzilla)", 1,
              "Removes dot crawl (crawling dots along edges) and rainbow patterns of composite video.",
              "If edges and the OSD show crawling dots. Trained on cartoons: can soften fine texture a little.",
              "fast", 0.035, 3.6, "zarxrax", "WTFPL",
              "https://objectstorage.us-phoenix-1.oraclecloud.com/n/ax6ygfvpvzka/b/open-modeldb-files/"
              "o/1x-Dotzilla-Compact.pth", "https://openmodeldb.info/models/1x-Dotzilla-Compact"),
    ModelInfo("antilines", "clean", "Heavy clean-up (Archivist AntiLines)", 1,
              "Strong clean-up of analog noise, interference lines and compression blocks.",
              "For very noisy, breaking-up footage. Smooths detail (a slightly painted look).",
              "fast", 0.02, 2.0, "Loganavter", "MIT",
              "https://github.com/Loganavter/Archivist-Project-Denoiser/releases/download/v1.0/"
              "1x-Archivist_AntiLines.pth", "https://github.com/Loganavter/Archivist-Project-Denoiser"),
    ModelInfo("archivist_rgb", "clean", "Heavy colour noise (Archivist RGB)", 1,
              "Strong removal of colour noise.",
              "For footage with heavy colour noise. Smooths detail like Heavy clean-up.",
              "fast", 0.02, 2.0, "Loganavter", "MIT",
              "https://github.com/Loganavter/Archivist-Project-Denoiser/releases/download/v1.0/"
              "1x-Archivist_RGB.pth", "https://github.com/Loganavter/Archivist-Project-Denoiser"),
    ModelInfo("general", "upscale", "General x4 - smooth (Real-ESRGAN)", 4,
              "Real-ESRGAN's general model for real-world video: removes noise and gives a clean, "
              "smooth picture.",
              "Noisy footage you want clean. Grass and trees come out smooth.",
              "fast", 0.12, 7.3, "Xintao Wang (Real-ESRGAN)", "BSD-3-Clause",
              "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
              "https://github.com/xinntao/Real-ESRGAN"),
    ModelInfo("general_wdn", "upscale", "General x4 - keeps texture (Real-ESRGAN)", 4,
              "The same model with less noise removal: keeps more of the texture of grass, trees "
              "and gravel.",
              "Good signal footage where you want detail rather than smoothness.",
              "fast", 0.12, 7.3, "Xintao Wang (Real-ESRGAN)", "BSD-3-Clause",
              "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-wdn-x4v3.pth",
              "https://github.com/xinntao/Real-ESRGAN"),
    ModelInfo("vhs2hd", "upscale", "VHS to HD x2 (RealPLKSR)", 2,
              "Trained to restore VHS and other analog video at twice the size. Keeps detail and a "
              "natural look rather than smoothing.",
              "720p (HD mode) recordings exported at 1440p: it doubles them directly. "
              "Much slower than the other models.",
              "slow", 1.1, 45.0, "asterixcool", "CC-BY-SA-4.0",
              "https://github.com/starinspace/StarinspaceUpscale/releases/download/Models/2xVHS2HD-RealPLKSR.pth",
              "https://openmodeldb.info/models/2x-VHS2HD-RealPLKSR", tile=512),
    ModelInfo("archiver_medium", "clean", "Balanced clean-up (Archivist Medium)", 1,
              "Balanced removal of grain, dirt and analog noise. Milder than the Heavy models, so it keeps "
              "more detail. Tuned for 720p-1080p pictures.",
              "HD-mode recordings with grain or noise, when BleedOut alone isn't enough.",
              "fast", 0.03, 2.1, "Loganavter", "MIT",
              "https://github.com/Loganavter/Archivist-Project-Denoiser/releases/download/v1.0/"
              "1x-Archivist_Medium.pth", "https://openmodeldb.info/models/1x-Archiver-Medium",
              sha256="b94d1fac8873e6bc45cbe8174d0186217a0f2bd4a7a3a103535f6f01972a4b13"),
    ModelInfo("azumpeg", "clean", "Dot crawl & NTSC colour (AzuMPEG)", 1,
              "Removes dot crawl, colour bleed, NTSC colour errors and old-style video compression "
              "artifacts in one go.",
              "Analog footage with crawling dots and smeared colour. Trained on cartoons at low resolution.",
              "fast", 0.06, 2.4, "ezogaming", "CC0-1.0 (no restrictions)",
              "https://drive.usercontent.google.com/download?id=1ECmsVeo_mlS01jlpXyy2HHNCEmBKv_IT"
              "&export=download&confirm=t", "https://openmodeldb.info/models/1x-AzuMPEG",
              sha256="e83ca07920197b2a9577c62a0c1052287116fc3e0a9cc835d7e9de4acdfd5bde",
              src_name="1x-AzuMPEG.safetensors"),
    ModelInfo("liveaction", "upscale", "Live action video x2 (LiveActionV1)", 2,
              "Made for live-action video: fixes compression, smeared colour, blur from rescaling, "
              "deinterlacing jaggies and on-screen text while keeping the picture faithful (no heavy "
              "denoise, no colour shifts).",
              "720p HD-mode recordings exported at 1440p: it doubles them directly. Personal use only.",
              "fast", 0.12, 2.5, "jcj83429", "CC-BY-NC-SA-4.0 (non-commercial)",
              "https://raw.githubusercontent.com/jcj83429/upscaling/f73a3a02874360ec6ced18f8bdd8e43b5d7bba57/"
              "2xLiveActionV1_SPAN/2xLiveActionV1_SPAN_490000.pth",
              "https://openmodeldb.info/models/2x-LiveActionV1-SPAN", personal=True,
              sha256="8b166c75831ea7f694d9058ee9c8df8148af8cc1d2b57e69e6581b15cab572f7"),
    ModelInfo("lsdir_compact", "upscale", "Photo x4 - fast (LSDIRCompact v2)", 4,
              "A fast x4 upscaler trained on real photos with mild compression, noise and blur.",
              "Scenery (trees, grass, buildings) when the built-in Fast model looks too cartoon-like.",
              "fast", 0.12, 3.8, "Philip Hofmann (helaman)", "CC-BY-4.0",
              "https://drive.usercontent.google.com/download?id=1nnhovhW3XMsgnXhx5ILATFvzAhtT8R-2"
              "&export=download&confirm=t", "https://openmodeldb.info/models/4x-LSDIRCompact-v2",
              sha256="4f731443dedd83ccacafeafd2f3bf30efab6e6861e70af12a728460f072f3440",
              src_name="4xLSDIRCompactv2.pth"),
    ModelInfo("ultrasharp", "upscale", "UltraSharp x4 - most detail", 4,
              "The community's most popular detail model: adds a lot of texture and sharp edges.",
              "Good-signal footage. On noisy footage it can over-sharpen the noise: check with Compare "
              "frame. Personal use only.",
              "slow", 0.9, 101.0, "Kim2091", "CC-BY-NC-SA-4.0 (non-commercial)",
              "https://huggingface.co/Kim2091/UltraSharp/resolve/main/4x-UltraSharp.pth",
              "https://openmodeldb.info/models/4x-UltraSharp", personal=True,
              sha256="a5812231fc936b42af08a5edba784195495d303d5b3248c24489ef0c4021fe01"),
]
BY_ID = {m.id: m for m in CATALOG}


def clean_models() -> list[ModelInfo]:
    return [m for m in CATALOG if m.kind == "clean"]


def upscale_models() -> list[ModelInfo]:
    return [m for m in CATALOG if m.kind == "upscale"]


# ---------------------------------------------------------------------------
# Files and downloads
# ---------------------------------------------------------------------------
def file_names(mid: str) -> tuple[str, str]:
    """(half-precision file used on the graphics card, full-precision fallback)."""
    return f"model-{mid}-fp16.onnx", f"model-{mid}.onnx"


def model_dirs() -> list[str]:
    dirs = []
    if os.environ.get("FPV_MODEL_DIR"):
        dirs.append(os.environ["FPV_MODEL_DIR"])
    if getattr(sys, "frozen", False):
        dirs.append(os.path.join(os.path.dirname(sys.executable), "models"))
    dirs.append(os.path.join(APP_DATA, "models"))
    return dirs


def find(mid: str) -> dict[str, str]:
    """Downloaded files of a model: {"fp16": path, "fp32": path} (either may be missing)."""
    out = {}
    for key, name in zip(("fp16", "fp32"), file_names(mid)):
        for d in model_dirs():
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                out[key] = p
                break
    return out


def installed(mid: str) -> bool:
    return "fp32" in find(mid) or "fp16" in find(mid)


def _urls(name: str) -> list[str]:
    return [f"https://github.com/{REPO}/releases/download/v{dc.APP_VERSION}/{name}",
            f"https://github.com/{REPO}/releases/latest/download/{name}"]


def download(mid: str, progress=None, cancel: threading.Event | None = None) -> None:
    """Download a model from the program's releases into the user's app data."""
    info = BY_ID[mid]
    dest = os.path.join(APP_DATA, "models")
    os.makedirs(dest, exist_ok=True)
    names = file_names(mid)
    for k, name in enumerate(names):
        last_err = None
        for url in _urls(name):
            tmp = os.path.join(dest, name + ".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": f"{dc.APP_NAME}/{dc.APP_VERSION}"})
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
                            part = (k + (got / total if total else 0.0)) / len(names)
                            progress(part, f"Downloading {info.name}... {got / 1e6:.1f}"
                                     + (f" of {total / 1e6:.1f} MB" if total else " MB"))
                if total and got != total:
                    raise RuntimeError("download incomplete")
                os.replace(tmp, os.path.join(dest, name))
                last_err = None
                break
            except dc.Cancelled:
                _remove(tmp)
                raise
            except Exception as e:      # noqa: BLE001 - try the next address
                _remove(tmp)
                last_err = e
        if last_err is not None:
            raise RuntimeError(f"Could not download {info.name}: {last_err}")
    try:
        check(mid)
    except Exception:
        for name in names:
            _remove(os.path.join(dest, name))
        raise


def _remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def check(mid: str) -> None:
    """Load a downloaded model on the processor and check it scales a small picture correctly."""
    import onnxruntime as ort
    info = BY_ID[mid]
    for key, p in find(mid).items():
        s = ort.InferenceSession(p, providers=["CPUExecutionProvider"])
        y = s.run(None, {s.get_inputs()[0].name: np.full((1, 3, 8, 12), 0.5, np.float32)})[0]
        if y.shape != (1, 3, 8 * info.scale, 12 * info.scale) or not np.isfinite(y).all():
            raise RuntimeError(f"{info.name}: the downloaded file is not the expected model.")


# ---------------------------------------------------------------------------
# Running a model
# ---------------------------------------------------------------------------
_GPU_LOCK = threading.Lock()      # one model at a time on the graphics card


def providers() -> list[str]:
    try:
        import onnxruntime as ort
        have = ort.get_available_providers()
    except Exception:
        return []
    return [p for p in ("DmlExecutionProvider", "CUDAExecutionProvider") if p in have] + ["CPUExecutionProvider"]


def gpu_available() -> bool:
    return any(p != "CPUExecutionProvider" for p in providers())


class OnnxModel:
    """A loaded model. run() takes and returns RGB uint8 pictures (H, W, 3)."""

    def __init__(self, mid: str, path: str | None = None, scale: int | None = None, tile: int | None = None,
                 use_gpu: bool = True):
        import onnxruntime as ort
        info = BY_ID.get(mid)
        self.id = mid
        self.scale = scale or (info.scale if info else 1)
        self.tile = info.tile if (tile is None and info) else (tile or 0)
        files = {"fp32": path} if path else find(mid)
        if not files:
            raise RuntimeError(f"The model '{info.name if info else mid}' is not downloaded yet.")
        provs = providers() if use_gpu else ["CPUExecutionProvider"]
        gpu = provs[0] if provs[0] != "CPUExecutionProvider" else None
        # half precision on the graphics card (about twice as fast), full precision on the processor
        combos = ([(gpu, "fp16"), (gpu, "fp32")] if gpu else []) + \
            [("CPUExecutionProvider", "fp32"), ("CPUExecutionProvider", "fp16")]
        self.sess = None
        last = None
        for pv, key in combos:
            if key not in files:
                continue
            try:
                so = ort.SessionOptions()
                so.enable_mem_pattern = False                 # required by DirectML
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
                so.log_severity_level = 3
                sess = ort.InferenceSession(files[key], so, providers=[pv])
                if (sess.get_providers() or ["CPUExecutionProvider"])[0] != pv:
                    continue                # ONNX Runtime fell back by itself (no usable graphics card)
                self.sess, self.provider, self.precision = sess, pv, key
                if self._sane():
                    break
                self.sess = None
            except Exception as e:      # noqa: BLE001 - try the next combination
                last = e
                self.sess = None
        if self.sess is None:
            raise RuntimeError(f"Could not load the model: {last}")
        self.input = self.sess.get_inputs()[0].name
        self.busy = 0.0          # seconds the model has been running (for speed estimates)
        self.frames = 0
        self.device = {"DmlExecutionProvider": "graphics card (DirectML)",
                       "CUDAExecutionProvider": "graphics card (CUDA)"}.get(self.provider, "processor")

    def _sane(self) -> bool:
        x = np.random.default_rng(0).random((1, 3, 16, 16), dtype=np.float32) * 0.5 + 0.25
        y = self.sess.run(None, {self.sess.get_inputs()[0].name: x})[0]
        return bool(np.isfinite(y).all() and y.shape[2] == 16 * self.scale and 0.05 < float(y.mean()) < 0.95)

    @property
    def on_gpu(self) -> bool:
        return self.provider != "CPUExecutionProvider"

    def _infer(self, x: np.ndarray) -> np.ndarray:
        with _GPU_LOCK:
            t = time.perf_counter()
            y = self.sess.run(None, {self.input: x})[0]
            self.busy += time.perf_counter() - t
            return y

    def run(self, img: np.ndarray) -> np.ndarray:
        h, w = img.shape[:2]
        x = (img.astype(np.float32) * (1.0 / 255.0)).transpose(2, 0, 1)[None]
        t = self.tile
        if not t or (h <= t and w <= t):
            y = self._infer(np.ascontiguousarray(x))[0]
        else:
            y = self._tiled(x, h, w, t)
        self.frames += 1
        return np.clip(y.transpose(1, 2, 0) * 255.0 + 0.5, 0, 255).astype(np.uint8)

    def _tiled(self, x: np.ndarray, h: int, w: int, t: int, pad: int = 16) -> np.ndarray:
        """Run in overlapping tiles of a fixed size (the graphics card keeps one compiled shape)."""
        s = self.scale
        th, tw = min(t, h), min(t, w)
        out = np.zeros((3, h * s, w * s), np.float32)
        step_y, step_x = max(1, th - 2 * pad), max(1, tw - 2 * pad)
        ys = list(range(0, max(1, h - th) + 1, step_y))
        xs = list(range(0, max(1, w - tw) + 1, step_x))
        if ys[-1] + th < h:
            ys.append(h - th)
        if xs[-1] + tw < w:
            xs.append(w - tw)
        for y0 in ys:
            for x0 in xs:
                tile = np.ascontiguousarray(x[:, :, y0:y0 + th, x0:x0 + tw])
                r = self._infer(tile)[0]
                # keep each tile's middle; edges of the picture keep the tile's edge
                a = 0 if y0 == 0 else pad
                b = th if y0 + th >= h else th - pad
                c = 0 if x0 == 0 else pad
                d = tw if x0 + tw >= w else tw - pad
                out[:, (y0 + a) * s:(y0 + b) * s, (x0 + c) * s:(x0 + d) * s] = r[:, a * s:b * s, c * s:d * s]
        return out


_LOADED: dict[tuple, OnnxModel] = {}


def load(mid: str) -> OnnxModel:
    """Load a model once per run of the program (loading on the graphics card takes a moment)."""
    key = (mid, tuple(sorted(find(mid).items())))
    if key not in _LOADED:
        _LOADED[key] = OnnxModel(mid)
    return _LOADED[key]


def notice() -> str:
    lines = ["AI models used by FPV DVR Stitcher (downloaded when first used):", ""]
    for m in CATALOG:
        lines.append(f"{m.name}: by {m.author}, licence {m.licence}. {m.page}")
    lines += ["", "Models marked non-commercial may only be used for personal, non-commercial videos."]
    return "\n".join(lines)
