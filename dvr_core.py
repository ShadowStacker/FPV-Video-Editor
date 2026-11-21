"""
FPV DVR Stitcher - core engine (no GUI).

Analyses analog-FPV DVR clips, finds "no signal" snow (VTX off) versus
weak / breaking-up signal, and exports one stitched video with the dead
static removed, using stream copy (no re-encode, no quality loss).

Can be used on its own from the command line:
    python dvr_core.py VID00001.AVI VID00002.AVI ... -o stitched.avi
"""
from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

APP_NAME = "FPV DVR Stitcher"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Detection tuning.  Measured on real analog DVR footage (pure snow vs weak
# signal vs clean picture).  All values are for the 160x120 analysis frame.
# ---------------------------------------------------------------------------
AW, AH = 160, 120          # analysis resolution
BLOCK = 20                 # 8 x 6 grid of 20x20 blocks
NOISE_T = 16.0             # block mean |horizontal pixel diff| above this = "snowy" block
                           #   (pure snow measures 22-40, picture/weak signal 2-15)
SNOW_COVER_T = 0.85        # fraction of snowy blocks for a frame to count as pure snow
                           #   (pure snow ~1.00, weak signal <= 0.35, clean <= 0.31)
TEAR_T = 1.30              # vertical/horizontal detail ratio; >1.3 = horizontal tearing (weak signal)
PERSIST_T = 1.50           # temporal persistence ratio; pure snow 1.0-1.2, real picture under noise >1.8
SMOOTH_S = 0.25            # seconds - smoothing window for the snow flag
WEAK_SMOOTH_S = 0.5        # seconds - smoothing window for the weak flag
LOOKBACK_S = 3.0           # seconds of signal examined before each loss of signal

LABEL_GOOD, LABEL_WEAK, LABEL_SNOW = 0, 1, 2


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _popen_kwargs():
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW - no console pop-ups
    return kw


def find_tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in dirs:
        for sub in ("", "ffmpeg", os.path.join("ffmpeg", "bin")):
            p = os.path.join(d, sub, exe)
            if os.path.isfile(p):
                return p
    p = shutil.which(name)
    if p:
        return p
    raise FileNotFoundError(
        f"Could not find {exe}. Put ffmpeg.exe and ffprobe.exe in the same folder as "
        f"this program (or install FFmpeg so it is on the PATH).")


_TOOLS: dict[str, str] = {}


def tool(name: str) -> str:
    if name not in _TOOLS:
        _TOOLS[name] = find_tool(name)
    return _TOOLS[name]


_FPS_ARGS: list[str] | None = None


def passthrough_args() -> list[str]:
    """'-fps_mode passthrough' (FFmpeg 5.1+) or the older '-vsync 0'."""
    global _FPS_ARGS
    if _FPS_ARGS is None:
        p = subprocess.run([tool("ffmpeg"), "-v", "error", "-nostdin", "-f", "lavfi", "-i",
                            "color=s=16x16:d=0.1", "-fps_mode", "passthrough", "-f", "null", "-"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, **_popen_kwargs())
        _FPS_ARGS = ["-fps_mode", "passthrough"] if p.returncode == 0 else ["-vsync", "0"]
    return _FPS_ARGS


def run_text(cmd: list[str]) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, **_popen_kwargs())
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed:\n"
                           + p.stderr.decode("utf-8", "replace")[-2000:])
    return p.stdout.decode("utf-8", "replace")


def fmt_time(t: float, ms: bool = False) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    if ms:
        return f"{h}:{m:02d}:{s:06.3f}" if h else f"{m:02d}:{s:06.3f}"
    return f"{h}:{m:02d}:{int(s):02d}" if h else f"{m:02d}:{int(s):02d}"


def natural_key(path: str):
    name = os.path.basename(path).lower()
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", name)]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Clip:
    path: str
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pts: np.ndarray = None          # presentation time of every decoded frame (s)
    key_pts: np.ndarray = None      # keyframe times (s)
    intra_only: bool = True
    offset: float = 0.0             # session time of this clip's first frame
    feats: dict = field(default_factory=dict)
    seek_ok: bool | None = None     # can ffmpeg seek inside this file? (None = not tested yet)

    @property
    def nframes(self) -> int:
        return 0 if self.pts is None else len(self.pts)

    @property
    def frame_dur(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 1.0 / 30

    @property
    def length(self) -> float:
        if self.nframes == 0:
            return 0.0
        return float(self.pts[-1] - self.pts[0]) + self.frame_dur


@dataclass
class Segment:
    kind: str            # 'signal' or 'nosignal'
    f0: int              # first global frame index
    f1: int              # one past last global frame index
    t0: float
    t1: float
    action: str          # 'keep' or 'cut'
    label: str = ""
    warn: bool = False
    weak_pct: float = 0.0
    flight: int = 0

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


@dataclass
class Settings:
    min_cut: float = 5.0          # only cut static runs at least this long (s)
    pad: float = 0.5              # seconds of static kept either side of a cut
    faint_protect: bool = True    # keep "snow" that hides a persistent picture
    snow_cover: float = SNOW_COVER_T
    noise_t: float = NOISE_T


class Session:
    """All analysed clips laid end-to-end on one continuous timeline."""

    def __init__(self, clips: list[Clip]):
        self.clips = clips
        off = 0.0
        for c in clips:
            c.offset = off
            off += c.length
        self.duration = off
        n = sum(c.nframes for c in clips)
        self.n = n
        self.clip_idx = np.concatenate([np.full(c.nframes, i, np.int32) for i, c in enumerate(clips)]) if n else np.zeros(0, np.int32)
        self.frame_idx = np.concatenate([np.arange(c.nframes, dtype=np.int32) for c in clips]) if n else np.zeros(0, np.int32)
        self.t = np.concatenate([c.offset + (c.pts - c.pts[0]) for c in clips]) if n else np.zeros(0)
        self.fdur = np.concatenate([np.full(c.nframes, c.frame_dur) for c in clips]) if n else np.zeros(0)
        # fix per-frame duration from real timestamps where available
        if n > 1:
            d = np.diff(self.t)
            ok = (d > 0) & (d < 4 * self.fdur[:-1])
            self.fdur[:-1][ok] = d[ok]
        self.f = {}
        for k in clips[0].feats if clips else []:
            self.f[k] = np.concatenate([c.feats[k] for c in clips])
        self.labels = np.zeros(n, np.int8)
        self.segments: list[Segment] = []
        self.notes: list[str] = []
        self.settings = Settings()

    # ---- helpers ---------------------------------------------------------
    def frames_for(self, seconds: float) -> int:
        fps = np.median([c.fps for c in self.clips]) if self.clips else 30.0
        return max(1, int(round(seconds * fps)))

    def locate(self, t: float) -> tuple[Clip, float]:
        """Session time -> (clip, timestamp inside that clip's file)."""
        g = int(np.clip(np.searchsorted(self.t, t, side="right") - 1, 0, self.n - 1))
        c = self.clips[self.clip_idx[g]]
        fi = self.frame_idx[g]
        return c, float(c.pts[fi] + (t - self.t[g]))

    def keep_mask(self) -> np.ndarray:
        keep = np.zeros(self.n, bool)
        padf = self.frames_for(self.settings.pad)
        segs = self.segments
        for i, s in enumerate(segs):
            if s.action == "keep":
                keep[s.f0:s.f1] = True
            elif s.kind == "nosignal":
                prev_kept = i > 0 and segs[i - 1].action == "keep"
                next_kept = i + 1 < len(segs) and segs[i + 1].action == "keep"
                if prev_kept:
                    keep[s.f0:min(s.f1, s.f0 + padf)] = True
                if next_kept:
                    keep[max(s.f0, s.f1 - padf):s.f1] = True
        return keep

    def output_times(self, keep: np.ndarray | None = None) -> np.ndarray:
        """Output-video time at which every session frame would appear."""
        if keep is None:
            keep = self.keep_mask()
        d = self.fdur * keep
        return np.cumsum(d) - d

    def kept_duration(self) -> float:
        return float((self.fdur * self.keep_mask()).sum())


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def probe_clip(path: str) -> Clip:
    c = Clip(path=path)
    out = run_text([tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
                    "-of", "default=nw=1", path])
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if "codec_name" not in info:
        raise RuntimeError(f"No video stream found in {os.path.basename(path)}")
    c.codec = info.get("codec_name", "")
    c.width = int(info.get("width", 0) or 0)
    c.height = int(info.get("height", 0) or 0)

    def rate(s):
        try:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        except Exception:
            return 0.0
    c.fps = rate(info.get("avg_frame_rate", "0/0")) or rate(info.get("r_frame_rate", "0/0")) or 30.0

    out = run_text([tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "packet=pts_time,dts_time,size,flags",
                    "-of", "compact=p=0", path])
    pts, keys = [], []
    for line in out.splitlines():
        d = dict(kv.split("=", 1) for kv in line.split("|") if "=" in kv)
        try:
            if int(d.get("size", "0") or 0) <= 0:
                continue          # AVI "dropped frame" placeholders carry no picture
        except ValueError:
            continue
        ts = d.get("pts_time", "N/A")
        if ts in ("N/A", ""):
            ts = d.get("dts_time", "N/A")
        if ts in ("N/A", ""):
            continue
        v = float(ts)
        pts.append(v)
        if "K" in d.get("flags", ""):
            keys.append(v)
    c.pts = np.sort(np.array(pts, dtype=np.float64))
    c.key_pts = np.array(sorted(keys), dtype=np.float64)
    c.intra_only = c.codec in ("mjpeg", "mjpegb", "rawvideo", "ljpeg", "jpeg2000", "prores",
                               "dnxhd", "ffv1", "huffyuv", "utvideo", "png", "dvvideo") \
        or (len(keys) > 0 and len(keys) >= 0.98 * len(pts))
    if c.nframes == 0:
        raise RuntimeError(f"No video frames found in {os.path.basename(path)}")
    return c


def _frame_features(y: np.ndarray, u: np.ndarray, v: np.ndarray, noise_t: float):
    """y: (n,AH,AW) float32, u/v: (n,AH/2,AW/2). Returns dict of per-frame arrays."""
    n = y.shape[0]
    hd = np.abs(np.diff(y, axis=2))                       # (n,AH,AW-1)
    hd = np.concatenate([hd, hd[:, :, -1:]], axis=2)      # pad to AW
    blocks = hd.reshape(n, AH // BLOCK, BLOCK, AW // BLOCK, BLOCK).mean(axis=(2, 4))
    blocks = blocks.reshape(n, -1)
    vd = np.abs(np.diff(y, axis=1)).mean(axis=(1, 2))
    hdm = hd.mean(axis=(1, 2))
    chroma = (np.abs(u - 128).mean(axis=(1, 2)) + np.abs(v - 128).mean(axis=(1, 2))) / 2
    return {
        "snow": (blocks > noise_t).mean(axis=1).astype(np.float32),
        "noise": np.median(blocks, axis=1).astype(np.float32),
        "tear": (vd / (hdm + 1.0)).astype(np.float32),
        "chroma": chroma.astype(np.float32),
        "bright": y.mean(axis=(1, 2)).astype(np.float32),
    }


def analyze_clip(c: Clip, progress=None, cancel: threading.Event | None = None,
                 noise_t: float = NOISE_T, snow_cover: float = SNOW_COVER_T) -> Clip:
    ysz, csz = AW * AH, (AW // 2) * (AH // 2)
    fsz = ysz + 2 * csz
    win = max(8, int(round(c.fps)))            # ~1 second windows
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", c.path, "-map", "0:v:0",
           "-an", "-sn", "-dn", *passthrough_args(),
           "-vf", f"scale={AW}:{AH}:flags=area", "-pix_fmt", "yuv420p",
           "-f", "rawvideo", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, bufsize=fsz * win, **_popen_kwargs())
    errbuf = []
    t_err = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    t_err.start()

    parts = {k: [] for k in ("snow", "noise", "tear", "chroma", "bright", "motion", "persist")}
    prev_coarse = None
    done = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = proc.stdout.read(fsz * win)
            if not buf:
                break
            k = len(buf) // fsz
            if k == 0:
                break
            a = np.frombuffer(buf[:k * fsz], np.uint8).reshape(k, fsz)
            y = a[:, :ysz].reshape(k, AH, AW).astype(np.float32)
            u = a[:, ysz:ysz + csz].reshape(k, AH // 2, AW // 2).astype(np.float32)
            v = a[:, ysz + csz:].reshape(k, AH // 2, AW // 2).astype(np.float32)
            f = _frame_features(y, u, v, noise_t)
            for key in f:
                parts[key].append(f[key])

            # motion: coarse 40x30 frame-to-frame change
            coarse = y.reshape(k, 30, 4, 40, 4).mean(axis=(2, 4))
            prevs = np.concatenate([coarse[:1] if prev_coarse is None else prev_coarse[None], coarse[:-1]])
            parts["motion"].append(np.abs(coarse - prevs).mean(axis=(1, 2)).astype(np.float32))
            prev_coarse = coarse[-1]

            # persistence of the snow frames in this ~1 s window:
            # pure random snow averages away (ratio ~1); a real picture hiding
            # under heavy noise (or the Betaflight OSD) survives averaging (ratio >> 1)
            snowy = f["snow"] >= snow_cover
            ratio = np.nan
            if snowy.sum() >= 8:
                ys = y[snowy]
                ys = ys - ys.mean(axis=2, keepdims=True)        # drop per-line streaks/flicker
                m = ys.mean(axis=0)
                s = ys.std(axis=0).mean()
                ratio = float(m.std() / (s / math.sqrt(len(ys)) + 1e-6))
            parts["persist"].append(np.full(k, ratio, np.float32))

            done += k
            if progress:
                progress(k)
    finally:
        if proc.poll() is None:
            try:
                proc.stdout.close()
            except Exception:
                pass
            proc.kill()
        proc.wait()
        t_err.join(timeout=2)
    if cancel is not None and cancel.is_set():
        raise Cancelled()

    feats = {k: (np.concatenate(v) if v else np.zeros(0, np.float32)) for k, v in parts.items()}
    n = len(feats["snow"])
    if n == 0:
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace")
        raise RuntimeError(f"Could not decode {os.path.basename(c.path)}\n{err[-1500:]}")
    # align decoded frames with packet timestamps
    if n != c.nframes:
        if abs(n - c.nframes) <= max(3, 0.01 * n) and c.nframes > 0:
            m = min(n, c.nframes)
            c.pts = c.pts[:m]
            feats = {k: v[:m] for k, v in feats.items()}
            if n > m:     # decoder produced a few more frames than packets
                pass
        else:
            start = c.pts[0] if c.nframes else 0.0
            c.pts = start + np.arange(n) / c.fps
    c.feats = feats
    return c


def analyze_files(paths: list[str], progress=None, cancel: threading.Event | None = None,
                  settings: Settings | None = None, workers: int | None = None) -> Session:
    """progress(fraction, message)"""
    settings = settings or Settings()
    paths = list(paths)
    if not paths:
        raise ValueError("No files selected")
    if progress:
        progress(0.0, "Reading file information...")
    clips = []
    for i, p in enumerate(paths):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        clips.append(probe_clip(p))
        if progress:
            progress(0.02 * (i + 1) / len(paths), f"Reading file information... {i + 1}/{len(paths)}")
    total = sum(c.nframes for c in clips) or 1
    lock = threading.Lock()
    state = {"done": 0}

    def tick(k):
        with lock:
            state["done"] += k
            d = state["done"]
        if progress:
            progress(0.02 + 0.98 * min(1.0, d / total), f"Analysing video... {100 * d / total:4.1f}%")

    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(analyze_clip, c, tick, cancel, settings.noise_t, settings.snow_cover) for c in clips]
        try:
            for fu in futs:
                fu.result()
        except BaseException:
            if cancel is not None:
                cancel.set()
            raise
    s = Session(clips)
    s.settings = settings
    classify(s, settings)
    return s


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) == 0:
        return x.astype(np.float32)
    k = np.ones(n, np.float32) / n
    pad = n // 2
    xp = np.pad(x.astype(np.float32), (pad, n - 1 - pad), mode="edge")
    return np.convolve(xp, k, mode="valid")


def _runs(mask: np.ndarray):
    """Yield (start, end) of True runs."""
    if len(mask) == 0:
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def classify(s: Session, settings: Settings | None = None) -> None:
    """Label every frame good/weak/snow and build the segment list."""
    if settings is not None:
        s.settings = settings
    st = s.settings
    s.notes = []
    n = s.n
    if n == 0:
        s.segments = []
        return
    f = s.f
    raw_snow = f["snow"] >= st.snow_cover

    faint = np.zeros(n, bool)
    if st.faint_protect:
        pers = f["persist"]
        cand = raw_snow & np.isfinite(pers)
        persistent = cand & (pers > PERSIST_T)
        if cand.sum() > 0:
            share = persistent.sum() / cand.sum()
            if share > 0.6 and cand.sum() > s.frames_for(20):
                # Nearly *all* static looks "persistent" - the receiver is probably
                # drawing its own overlay on top of the snow. Ignore the check.
                s.notes.append("Faint-signal check disabled: the static itself contains a fixed "
                               "pattern (receiver overlay?).")
            else:
                faint = persistent

    snow_flag = raw_snow & ~faint
    snow = _smooth(snow_flag, s.frames_for(SMOOTH_S)) > 0.5
    weak_raw = (~snow) & ((f["tear"] > TEAR_T) |
                          ((f["snow"] > 0.30) & (f["snow"] < st.snow_cover)) | faint)
    # smooth only over frames that have a picture, so frames next to static aren't diluted
    wn = s.frames_for(WEAK_SMOOTH_S)
    weak = (_smooth(weak_raw, wn) > 0.5 * np.maximum(_smooth(~snow, wn), 1e-6)) & ~snow
    weak |= faint & ~snow
    labels = np.zeros(n, np.int8)
    labels[weak] = LABEL_WEAK
    labels[snow] = LABEL_SNOW
    s.labels = labels

    # ---- segments: alternate signal / no-signal runs ------------------------
    segs: list[Segment] = []
    bounds = np.flatnonzero(np.diff(snow.astype(np.int8))) + 1
    edges = np.concatenate([[0], bounds, [n]])
    for a, b in zip(edges[:-1], edges[1:]):
        a, b = int(a), int(b)
        t0 = float(s.t[a])
        t1 = float(s.t[b - 1] + s.fdur[b - 1])
        kind = "nosignal" if snow[a] else "signal"
        segs.append(Segment(kind, a, b, t0, t1, "keep"))

    lb = s.frames_for(LOOKBACK_S)
    for i, sg in enumerate(segs):
        first, last = i == 0, i == len(segs) - 1
        if sg.kind == "signal":
            sl = labels[sg.f0:sg.f1]
            sg.weak_pct = 100.0 * float((sl == LABEL_WEAK).mean())
            sg.action = "keep"
            continue

        # no-signal run
        long_enough = sg.duration >= st.min_cut
        sg.action = "cut" if (long_enough or first or last) else "keep"
        if first:
            sg.label = "No signal - recording started before VTX was on"
            continue
        # How did the picture end?  A VTX being unplugged goes from a clean
        # picture straight to snow.  Flying out of range (or a crash that
        # damages the antenna) degrades into weak, torn signal first.
        a = max(segs[i - 1].f0, sg.f0 - lb)
        pre = labels[a:sg.f0]
        weak_frac = float((pre == LABEL_WEAK).mean()) if len(pre) else 0.0
        faded = weak_frac > 0.3
        if not long_enough and not last:
            sg.label = (f"Brief dropout ({sg.duration:.1f}s) after weak signal - picture came back"
                        if faded else f"Brief dropout ({sg.duration:.1f}s) - picture came back")
            continue
        if faded:
            why = "signal faded first - out of range or crash?"
            sg.warn = True
        else:
            why = "clean cut-off - VTX powered off (landed / battery swap)"
        if last:
            sg.label = f"No signal to end of recording - {why}"
        else:
            sg.label = f"No signal - {why}"
    s.segments = segs
    renumber_flights(s)


def renumber_flights(s: Session) -> None:
    """Number the flights: a new flight starts after every real gap in the signal."""
    flight = 0
    for i, sg in enumerate(s.segments):
        if sg.kind != "signal":
            continue
        prev = s.segments[i - 1] if i > 0 else None
        if flight == 0 or (prev.kind == "nosignal" and
                           (prev.action == "cut" or prev.duration >= s.settings.min_cut)):
            flight += 1
            base = f"Flight {flight}"
        else:
            base = f"Flight {flight} (continued)"
        sg.flight = flight
        sg.label = base + (f"  -  {sg.weak_pct:.0f}% weak signal" if sg.weak_pct >= 0.5 else "")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
@dataclass
class Piece:
    clip: Clip
    i0: int   # first frame index in clip
    i1: int   # last frame index in clip (inclusive)


def build_pieces(s: Session, keep: np.ndarray | None = None) -> list[Piece]:
    if keep is None:
        keep = s.keep_mask()
    pieces: list[Piece] = []
    for a, b in _runs(keep):
        g = a
        while g < b:
            ci = int(s.clip_idx[g])
            c = s.clips[ci]
            # end of this run inside this clip
            clip_end = g + (c.nframes - int(s.frame_idx[g]))
            e = min(b, clip_end)
            i0, i1 = int(s.frame_idx[g]), int(s.frame_idx[e - 1])
            if not c.intra_only and len(c.key_pts):
                # stream copy must start on a keyframe: start a little earlier
                kp = c.key_pts[c.key_pts <= c.pts[i0] + 1e-6]
                if len(kp):
                    i0 = int(np.searchsorted(c.pts, kp[-1] - 1e-6))
            if pieces and pieces[-1].clip is c and i0 <= pieces[-1].i1 + 1:
                pieces[-1].i1 = max(pieces[-1].i1, i1)
            else:
                pieces.append(Piece(c, i0, i1))
            g = e
    return pieces


def _concat_path(p: str) -> str:
    p = os.path.abspath(p).replace("\\", "/")
    return "'" + p.replace("'", "'\\''") + "'"


def _seek_works(c: Clip) -> bool:
    """Can ffmpeg jump into the middle of this file?  A DVR clip cut short by the
    receiver being switched off has no index, and the concat demuxer then silently
    starts from the beginning of the file instead of the requested in-point."""
    if c.seek_ok is not None:
        return c.seek_ok
    ok = True
    if c.nframes > 10 and c.length > 4.0:
        t = float(c.pts[int(c.nframes * 0.6)])
        try:
            out = run_text([tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
                            "-read_intervals", f"{t:.3f}%+#1",
                            "-show_entries", "packet=pts_time,dts_time", "-of", "compact=p=0", c.path])
            first = None
            for line in out.splitlines():
                d = dict(kv.split("=", 1) for kv in line.split("|") if "=" in kv)
                v = d.get("pts_time", "N/A")
                v = d.get("dts_time", "N/A") if v in ("N/A", "") else v
                if v not in ("N/A", ""):
                    first = float(v)
                    break
            gap = float(np.max(np.diff(c.key_pts))) if len(c.key_pts) > 1 else 2.0
            ok = first is not None and first >= t - max(1.0, gap + 0.5)
        except Exception:
            ok = False
    c.seek_ok = ok
    return ok


def _seekable_copy(c: Clip, tmpdir: str, cancel=None) -> str:
    """Re-wrap an un-indexed clip into a temporary indexed file (stream copy, no re-encode)."""
    ext = ".avi" if c.path.lower().endswith(".avi") else ".mkv"
    dst = os.path.join(tmpdir, f"fixed_{abs(hash(c.path)) & 0xffffff:06x}{ext}")
    if not os.path.exists(dst):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        subprocess.run([tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-i", c.path, "-map", "0:v:0",
                        "-c", "copy", "-an", "-sn", "-dn", dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       **_popen_kwargs())
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            raise RuntimeError(f"Could not repair {os.path.basename(c.path)} for cutting.")
    return dst


def write_concat_list(pieces: list[Piece], path: str, tmpdir: str | None = None, cancel=None) -> float:
    eps = 0.001
    total = 0.0
    lines = ["ffconcat version 1.0"]
    for pc in pieces:
        c = pc.clip
        src = c.path
        if pc.i0 > 0 and tmpdir is not None and not _seek_works(c):
            src = _seekable_copy(c, tmpdir, cancel)
        lines.append(f"file {_concat_path(src)}")
        start = c.pts[pc.i0]
        end = c.pts[pc.i1] + c.frame_dur if pc.i1 + 1 >= c.nframes else c.pts[pc.i1 + 1]
        if pc.i0 > 0:
            lines.append(f"inpoint {start + eps:.6f}")
        if pc.i1 + 1 < c.nframes:
            lines.append(f"outpoint {end - eps:.6f}")
        total += end - start
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return total


def write_chapters(s: Session, path: str) -> None:
    keep = s.keep_mask()
    out_t = s.output_times(keep)
    total = float((s.fdur * keep).sum())
    starts = []
    segs = s.segments
    for i, sg in enumerate(segs):
        if sg.kind == "signal" and sg.action == "keep" and "(continued)" not in sg.label:
            j = sg.f0
            if i > 0 and segs[i - 1].kind == "nosignal":
                # start the chapter right at the cut, not after the kept bit of static
                gap = np.flatnonzero(~keep[segs[i - 1].f0:sg.f0])
                if len(gap):
                    j = segs[i - 1].f0 + int(gap[-1]) + 1
            t0 = 0.0 if not starts else float(out_t[min(j, s.n - 1)])
            starts.append((t0, f"Flight {sg.flight}"))
    lines = [";FFMETADATA1", f"title={APP_NAME} export"]
    for i, (t0, name) in enumerate(starts):
        t1 = starts[i + 1][0] if i + 1 < len(starts) else total
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(t0 * 1000)}",
                  f"END={int(max(t1, t0 + 0.001) * 1000)}", f"title={name}"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _run_ffmpeg_progress(cmd, total, progress, cancel, label):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, **_popen_kwargs())
    errbuf = []
    t_err = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    t_err.start()
    try:
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set():
                proc.kill()
                raise Cancelled()
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                    if progress and total > 0:
                        progress(min(1.0, us / 1e6 / total), label)
                except ValueError:
                    pass
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        t_err.join(timeout=5)
    if proc.returncode != 0:
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace")
        raise RuntimeError("ffmpeg failed:\n" + err[-2500:])


def _tmpdir_for(out_path: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                     "." + os.path.splitext(os.path.basename(out_path))[0] + "_tmp")
    os.makedirs(d, exist_ok=True)
    return d


def _concat_copy(s: Session, pieces: list[Piece], out_path: str, tmpdir: str, progress=None,
                 cancel=None, chapters: bool = False, label: str = "") -> float:
    lst = os.path.join(tmpdir, "concat.txt")
    if progress:
        progress(0.0, "Preparing...")
    total = write_concat_list(pieces, lst, tmpdir, cancel)
    ext = os.path.splitext(out_path)[1].lower()
    cmd = [tool("ffmpeg"), "-y", "-v", "error", "-nostdin", "-progress", "pipe:1", "-nostats",
           "-f", "concat", "-safe", "0", "-i", lst]
    if chapters and ext in (".mkv", ".mp4", ".mov"):
        meta = os.path.join(tmpdir, "chapters.txt")
        write_chapters(s, meta)
        cmd += ["-i", meta, "-map_metadata", "1", "-map_chapters", "1"]
    cmd += ["-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn", "-avoid_negative_ts", "make_zero"]
    if ext == ".avi":
        cmd += ["-fflags", "+genpts"]
    cmd += [out_path]
    _run_ffmpeg_progress(cmd, total, progress, cancel, label)
    return total


def export_video(s: Session, out_path: str, progress=None, cancel: threading.Event | None = None,
                 keep: np.ndarray | None = None, chapters: bool = True) -> dict:
    """Stream-copy the kept frames into one file. Returns stats."""
    pieces = build_pieces(s, keep)
    if not pieces:
        raise RuntimeError("Nothing to export - every part of the session is marked as cut.")
    tmpdir = _tmpdir_for(out_path)
    try:
        total = _concat_copy(s, pieces, out_path, tmpdir, progress, cancel, chapters,
                             "Writing video (lossless copy)...")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"pieces": len(pieces), "duration": total}


def export_clip(s: Session, t0: float, t1: float, out_path: str, cancel=None) -> None:
    """Lossless sub-clip of the session between two session times (for crash reports)."""
    keep = np.zeros(s.n, bool)
    keep[(s.t >= t0) & (s.t < t1)] = True
    pieces = build_pieces(s, keep)
    if not pieces:
        return
    tmpdir = _tmpdir_for(out_path)
    try:
        _concat_copy(s, pieces, out_path, tmpdir, None, cancel)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def grab_frame_png(s: Session, t: float, width: int = 320) -> bytes:
    c, ts = s.locate(t)
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, ts):.4f}", "-i", c.path,
           "-frames:v", "1", "-an", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.DEVNULL, **_popen_kwargs())
    return p.stdout


def last_clear_frame(s: Session, seg_index: int, any_picture: bool = False) -> int | None:
    """Global index of the last good-signal frame (or, with any_picture, the last
    frame showing any picture at all) before a no-signal segment."""
    sg = s.segments[seg_index]
    a = max(0, sg.f0 - s.frames_for(30))
    lab = s.labels[a:sg.f0]
    wants = ((LABEL_GOOD, LABEL_WEAK),) if any_picture else ((LABEL_GOOD,), (LABEL_WEAK,))
    for want in wants:
        idx = np.flatnonzero(np.isin(lab, want))
        if len(idx):
            j = int(idx[-1])
            if not any_picture and want == (LABEL_GOOD,):
                # labels are smoothed, so step back past the edge of the weak/static run
                back = s.frames_for(WEAK_SMOOTH_S) // 2 + 1
                k = j - back
                if k >= 0 and (lab[k:j + 1] == LABEL_GOOD).all():
                    j = k
            return a + j
    return None


def write_report(s: Session, video_path: str, progress=None, cancel=None) -> str:
    """HTML report of all signal losses, with snapshots and short clips for the suspicious ones."""
    base = os.path.splitext(video_path)[0]
    rdir = base + "_report"
    os.makedirs(rdir, exist_ok=True)
    keep = s.keep_mask()
    out_t = s.output_times(keep)
    ext = os.path.splitext(video_path)[1].lower() or ".avi"
    rows = []
    events = [(i, sg) for i, sg in enumerate(s.segments) if sg.kind == "nosignal" and i > 0]
    for k, (i, sg) in enumerate(events):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(k / max(1, len(events)), "Writing signal-loss report...")
        c, ts = s.locate(sg.t0)
        g = sg.f0
        out_pos = float(out_t[min(g, s.n - 1)])
        img_html = ""
        clip_html = ""
        lc = last_clear_frame(s, i) if sg.duration >= s.settings.min_cut or sg.warn else None
        if lc is not None:
            tag = f"loss{k + 1:02d}"
            png = grab_frame_png(s, float(s.t[lc]) + 0.001, width=c.width or 640)
            if png:
                with open(os.path.join(rdir, f"{tag}_last_clear_frame.png"), "wb") as fh:
                    fh.write(png)
                img_html += (f'<figure><a href="{tag}_last_clear_frame.png"><img src="{tag}_last_clear_frame.png" '
                             f'class="{"big" if sg.warn else "small"}"></a><figcaption>Last clear frame '
                             f'({fmt_time(float(s.t[lc]), True)} session time)</figcaption></figure>')
            lp = last_clear_frame(s, i, any_picture=True)
            if sg.warn and lp is not None and lp - lc > 2:
                png = grab_frame_png(s, float(s.t[lp]) + 0.001, width=c.width or 640)
                if png:
                    with open(os.path.join(rdir, f"{tag}_last_picture.png"), "wb") as fh:
                        fh.write(png)
                    img_html += (f'<figure><a href="{tag}_last_picture.png"><img src="{tag}_last_picture.png" '
                                 f'class="big"></a><figcaption>Very last picture before static '
                                 f'({fmt_time(float(s.t[lp]), True)} session time)</figcaption></figure>')
            if sg.warn:
                strip = []
                for j, back in enumerate((8, 6, 4, 2)):
                    tt = float(s.t[lc]) - back
                    if tt < s.segments[i - 1].t0:
                        continue
                    p2 = grab_frame_png(s, tt, width=240)
                    if p2:
                        fn = f"{tag}_minus{back}s.png"
                        with open(os.path.join(rdir, fn), "wb") as fh:
                            fh.write(p2)
                        strip.append(f'<figure><img src="{fn}"><figcaption>-{back}s</figcaption></figure>')
                img_html = '<div class="strip">' + "".join(strip) + "</div>" + img_html
                clip_name = f"{tag}_last_20s{ext}"
                try:
                    export_clip(s, sg.t0 - 20.0, sg.t0 + 2.0, os.path.join(rdir, clip_name), cancel)
                    clip_html = f'<a href="{clip_name}">{clip_name}</a>'
                except Exception as e:  # report should never fail the export
                    clip_html = f"(clip failed: {html.escape(str(e)[:200])})"
        rows.append(f"""<tr class="{'warn' if sg.warn else ''}">
<td>{k + 1}</td><td>{fmt_time(sg.t0, True)}</td><td>{fmt_time(out_pos, True)}</td>
<td>{html.escape(os.path.basename(c.path))} @ {fmt_time(ts, True)}</td>
<td>{sg.duration:.1f}s</td><td>{'Kept' if sg.action == 'keep' else 'Cut'}</td>
<td>{'&#9888; ' if sg.warn else ''}{html.escape(sg.label)}{('<br>' + clip_html) if clip_html else ''}{img_html}</td></tr>""")
    files = "".join(f"<li>{html.escape(os.path.basename(c.path))} - {fmt_time(c.length)}</li>" for c in s.clips)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Signal-loss report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;background:#fff}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ccc;padding:6px;vertical-align:top;font-size:14px}}
th{{background:#f0f0f0;text-align:left}} tr.warn td{{background:#fff4e0}}
img{{max-width:640px;display:block;margin-top:6px;border:1px solid #999}} img.small{{max-width:320px}}
.strip{{display:flex;gap:6px;flex-wrap:wrap}} .strip img{{width:200px}} figure{{margin:0}}
figcaption{{font-size:12px;color:#555;text-align:center}}
</style></head><body>
<h1>Signal-loss report</h1>
<p>Video: <b>{html.escape(os.path.basename(video_path))}</b><br>
Recorded footage: {fmt_time(s.duration)} &nbsp; Exported: {fmt_time(float((s.fdur * keep).sum()))}</p>
<p><b>Session time</b> = time across all original clips laid end to end. <b>Output time</b> = where it
appears in the exported video. Rows marked &#9888; did not look like a normal power-off: the last clear
frame and the seconds before it are shown, and a short lossless clip of the last 20 seconds is saved
next to this report.</p>
<table><tr><th>#</th><th>Session time</th><th>Output time</th><th>Original file @ time</th>
<th>Length</th><th>Static</th><th>What happened</th></tr>
{''.join(rows) or '<tr><td colspan="7">No losses of signal found.</td></tr>'}
</table>
<h3>Source files</h3><ul>{files}</ul>
{''.join(f'<p><i>{html.escape(nt)}</i></p>' for nt in s.notes)}
<p style="color:#777;font-size:12px">{APP_NAME} {APP_VERSION}</p>
</body></html>"""
    path = os.path.join(rdir, "report.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def summary_table(s: Session) -> str:
    lines = [f"{'#':>3}  {'start':>9}  {'end':>9}  {'len':>7}  {'action':6}  what"]
    for i, sg in enumerate(s.segments):
        lines.append(f"{i + 1:>3}  {fmt_time(sg.t0, True):>9}  {fmt_time(sg.t1, True):>9}  "
                     f"{sg.duration:6.1f}s  {sg.action.upper():6}  {'! ' if sg.warn else ''}{sg.label}")
    lines.append(f"Recorded {fmt_time(s.duration)}  ->  exported {fmt_time(s.kept_duration())}")
    lines += s.notes
    return "\n".join(lines)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=f"{APP_NAME} (command line)")
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--output", help="output video (.avi or .mkv); omit to only analyse")
    ap.add_argument("--min-cut", type=float, default=5.0)
    ap.add_argument("--pad", type=float, default=0.5)
    ap.add_argument("--no-faint-protect", action="store_true")
    ap.add_argument("--report", action="store_true", help="also write the signal-loss report")
    ap.add_argument("--dump", help="write per-frame measurements to this CSV (for tuning)")
    a = ap.parse_args(argv)
    files = sorted(a.files, key=natural_key)
    st = Settings(min_cut=a.min_cut, pad=a.pad, faint_protect=not a.no_faint_protect)

    def prog(frac, msg):
        print(f"\r{msg:<45} {frac * 100:5.1f}%", end="", file=sys.stderr, flush=True)
    s = analyze_files(files, prog, settings=st)
    print(file=sys.stderr)
    print(summary_table(s))
    if a.dump:
        with open(a.dump, "w") as fh:
            keys = list(s.f)
            fh.write("file,frame,session_t,label," + ",".join(keys) + "\n")
            for g in range(s.n):
                c = s.clips[s.clip_idx[g]]
                fh.write(f"{os.path.basename(c.path)},{s.frame_idx[g]},{s.t[g]:.3f},{s.labels[g]},"
                         + ",".join(f"{s.f[k][g]:.3f}" for k in keys) + "\n")
    if a.output:
        export_video(s, a.output, prog)
        print(file=sys.stderr)
        if a.report:
            print("report:", write_report(s, a.output, prog))
        print("wrote", a.output)


if __name__ == "__main__":
    main()
