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
import io
import math
import os
import platform
import re
import copy
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import numpy as np

APP_NAME = "FPV DVR Stitcher"
APP_VERSION = "1.3.0"

# ---------------------------------------------------------------------------
# Detection tuning.  Measured on real analog DVR footage (pure snow vs weak
# signal vs clean picture).  All values are for the 160x120 analysis frame.
# ---------------------------------------------------------------------------
AW, AH = 160, 120          # analysis resolution
BLOCK = 20                 # 8 x 6 grid of 20x20 blocks
NOISE_T = 16.0             # block mean |horizontal pixel diff| above this = "snowy" block
                           #   (pure snow measures 22-40, picture/weak signal 2-15)
SNOW_COVER_T = 0.95        # fraction of snowy blocks for a frame to count as pure snow
                           #   (pure snow ~1.00; picture, even weak, well below)
TEAR_T = 1.30              # vertical/horizontal detail ratio; >1.3 = horizontal tearing (weak signal)
PERSIST_T = 1.50           # temporal persistence ratio; pure snow 1.0-1.2, real picture under noise >1.8
SMOOTH_S = 0.25            # seconds - smoothing window for the snow flag
WEAK_SMOOTH_S = 0.5        # seconds - smoothing window for the weak flag
LOOKBACK_S = 3.0           # seconds of signal examined before each loss of signal
CHUNK_MIN_S = 60.0         # long single recordings are analysed in parallel chunks of at least this
OSD_W, OSD_H, OSD_PH = 32, 18, 90   # OSD text map: 32x18 grid (from a 160x90 stroke picture)
STATS_MATCH_T = 0.6        # correlation with the STATS-screen pattern above this = STATS screen
STATS_MAX_S = 180.0        # never trim more than this before a power-off
FLIGHT_GAP_S = 5.0         # static at least this long = VTX was off (new flight); shorter = dropout
COLOUR_LOSS_T = 1.5        # receivers drop colour when the signal gets weak: chroma below this = weak

LABEL_GOOD, LABEL_WEAK, LABEL_SNOW = 0, 1, 2

# Betaflight post-flight STATS screen as seen in the OSD text map (32x18, 0-255), learned from
# real DVR footage. Used when a recording has too few power-offs to learn the pattern itself.
_STATS_TEMPLATE_HEX = (
    "01010201020203010406050408070e0a0907080607050306030606050102010102020202030203030603040208080b06"
    "0907080a0c0806040404050401010000030205040203050205040404090c0606030504060a0706040303030201010100"
    "0303030303040504031d1c1f18036947612c49290e1a1e210101030101010100042622272a4012073c2e3a1006020506"
    "0708070607030a0133310b132220000002121c231616341432292e201e0b050a0a0b06050704160238131b1d22110100"
    "031e242819064d43231b2e3d302e040a0a0c070305030f01450b303735130000053355393d415563460d060604020107"
    "05080706060215015f0f493b2f12010008191b1b1407374439270908080301030607070703000d012d20190200010201"
    "050f23251c062b2b322b1915151305090706030603000a023311190e05020003031e433727033c444d3250391a2b0207"
    "070b080306071a03300b32393b1c0301032647413a5906414f2f2b070908030809040906010719014945383019010101"
    "0124202b2b2f1204331517272a2f263801030203030308012e0105010d000002021611231a3326051d1821272e240c2e"
    "03020101010008012c010203020000000202030607020407070808070304030401010100010000010101010101000001"
    "040305050505050406060807040404020201020001000100000001010201010101020406070b06040707090906040303"
    "030303030203020200010100000001010102050804020206070205040402010204030202020102020000000001010701"
)



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
        dirs.append(os.path.dirname(sys.executable))   # ffmpeg.exe placed next to the .exe
        if getattr(sys, "_MEIPASS", None):
            dirs.append(sys._MEIPASS)                  # ffmpeg built into the single-file .exe
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
    pkt_pos: np.ndarray = None      # byte offset of every frame's packet (for diagnostics)
    pkt_size: np.ndarray = None
    blocks: np.ndarray = None       # (frames, 48) block noise grid, float16 (for diagnostics)
    osd: np.ndarray = None          # (frames, 32*18) map of OSD-like text strokes, uint8

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
class Chapter:
    t: float             # session time
    name: str
    id: int = 0


@dataclass
class Settings:
    min_cut: float = 1.0          # only cut static runs at least this long (s)
    pad: float = 0.0              # seconds of static kept either side of a cut
    faint_protect: bool = True    # keep "snow" that hides a persistent picture
    snow_cover: float = SNOW_COVER_T
    noise_t: float = NOISE_T
    trim_stats: bool = True       # cut the post-flight Betaflight STATS screen


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
        self.blocks = (np.concatenate([c.blocks for c in clips])
                       if clips and all(c.blocks is not None for c in clips) else None)
        self.osd = (np.concatenate([c.osd for c in clips])
                    if clips and all(c.osd is not None for c in clips) else None)
        self.labels = np.zeros(n, np.int8)
        self.segments: list[Segment] = []
        self.notes: list[str] = []
        self.settings = Settings()
        self.chapters: list[Chapter] = []        # chapters added by the user
        self.flight_names: dict[int, str] = {}   # flight chapters the user renamed
        self.edited = False                      # Keep/Cut or cut times changed by hand

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
        pad = self.settings.pad
        segs = self.segments
        for i, s in enumerate(segs):
            if s.action == "keep":
                keep[s.f0:s.f1] = True
            elif s.kind == "nosignal":
                prev_kept = i > 0 and segs[i - 1].action == "keep"
                next_kept = i + 1 < len(segs) and segs[i + 1].action == "keep"
                if prev_kept and pad > 0:
                    keep[s.f0:s.f1] |= self.t[s.f0:s.f1] < s.t0 + pad
                if next_kept and pad > 0:
                    keep[s.f0:s.f1] |= self.t[s.f0:s.f1] >= s.t1 - pad
        return keep

    def play_durations(self, keep: np.ndarray | None = None) -> np.ndarray:
        """How long each frame stays on screen in the export (0 for cut frames). Inside a
        kept stretch a frame lasts until the next one (DVR gaps included); the last frame
        before a cut lasts one normal frame, so no frozen frame is left at a cut."""
        if keep is None:
            keep = self.keep_mask()
        nxt = np.empty(self.n)
        if self.n:
            nxt[:-1] = np.diff(self.t)
            nxt[-1] = self.fdur[-1]
        cont = np.zeros(self.n, bool)
        cont[:-1] = keep[1:]
        d = np.where(cont, nxt, np.minimum(nxt, self.fdur))
        return d * keep

    def output_times(self, keep: np.ndarray | None = None) -> np.ndarray:
        """Output-video time at which every session frame would appear."""
        d = self.play_durations(keep)
        return np.cumsum(d) - d

    def kept_duration(self) -> float:
        return float(self.play_durations().sum())

    def frame_at(self, t: float) -> int:
        """Index of the frame on screen at session time t."""
        return int(np.clip(np.searchsorted(self.t, t + 1e-6, side="right") - 1, 0, max(0, self.n - 1)))

    def nearest_frame_start(self, t: float) -> int:
        """Index of the frame whose start is closest to session time t."""
        g = int(np.clip(np.searchsorted(self.t, t), 0, max(0, self.n - 1)))
        if g > 0 and abs(self.t[g - 1] - t) <= abs(self.t[g] - t):
            g -= 1
        return g

    def segment_at(self, t: float) -> int:
        g = self.frame_at(t)
        for i, sg in enumerate(self.segments):
            if sg.f0 <= g < sg.f1:
                return i
        return len(self.segments) - 1

    def out_to_session(self, o: float, keep: np.ndarray | None = None) -> float:
        """Output-video time -> session time (the kept frame showing at that moment)."""
        if keep is None:
            keep = self.keep_mask()
        idx = np.flatnonzero(keep)
        if not len(idx):
            return 0.0
        ot = self.output_times(keep)[idx]
        k = int(np.clip(np.searchsorted(ot, o + 1e-6, side="right") - 1, 0, len(idx) - 1))
        g = int(idx[k])
        return float(self.t[g] + min(max(0.0, o - ot[k]), self.fdur[g] * 0.999))

    def session_to_out(self, t: float, keep: np.ndarray | None = None) -> float:
        if keep is None:
            keep = self.keep_mask()
        g = self.frame_at(t)
        ot = self.output_times(keep)
        if keep[g]:
            return float(ot[g] + min(max(0.0, t - self.t[g]), self.play_durations(keep)[g]))
        nxt = np.flatnonzero(keep[g:])
        return float(ot[g + nxt[0]]) if len(nxt) else float(self.play_durations(keep).sum())


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
                    "-show_entries", "packet=pts_time,dts_time,size,flags,pos",
                    "-of", "compact=p=0", path])
    pts, keys, poss, sizes = [], [], [], []
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
        try:
            poss.append(int(d.get("pos", "-1")))
        except ValueError:
            poss.append(-1)
        sizes.append(int(d.get("size", "0") or 0))
        if "K" in d.get("flags", ""):
            keys.append(v)
    order = np.argsort(np.array(pts, dtype=np.float64), kind="stable")
    c.pts = np.array(pts, dtype=np.float64)[order]
    c.pkt_pos = np.array(poss, dtype=np.int64)[order]
    c.pkt_size = np.array(sizes, dtype=np.int64)[order]
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
        "hd": hdm.astype(np.float32),
        "vd": vd.astype(np.float32),
    }, blocks.astype(np.float16)


def _decode_features(c: Clip, i0: int = 0, count: int | None = None, progress=None,
                     cancel: threading.Event | None = None, noise_t: float = NOISE_T,
                     snow_cover: float = SNOW_COVER_T, lead: int = 0):
    """Decode frames i0 .. i0+count (all to the end if count is None) and measure them.
    `lead` leading frames (an overlap with the previous chunk) are measured on their own so
    the ~1 s windows that follow stay aligned with a single-pass analysis.
    Returns (features dict, block grid, OSD text map, number of frames decoded)."""
    ysz, csz = AW * AH, (AW // 2) * (AH // 2)
    oy = OSD_PH * AW                              # OSD map rows stacked under the analysis frame
    ocz = (OSD_PH // 2) * (AW // 2)
    fsz = ysz + oy + 2 * (csz + ocz)
    win = window_frames(c)                        # ~1 second windows
    # one decode gives two pictures stacked on top of each other:
    #   160x120 frame for the static / weak-signal measurements
    #   160x90 map of thin bright strokes (OSD text) for spotting the STATS screen
    graph = (f"[0:v]split=2[m][o];[m]scale={AW}:{AH}:flags=area,format=yuv420p[main];"
             f"[o]scale=320:180:flags=area,format=gray,split[a][b];[b]erosion,dilation[op];"
             f"[a][op]blend=all_mode=difference,lut=y='if(gt(val,40),255,0)',"
             f"scale={AW}:{OSD_PH}:flags=area,format=yuv420p[osd];[main][osd]vstack=inputs=2[out]")
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin"]
    if i0 > 0:
        # Every frame of an intra-only file is a keyframe, so seeking just past frame i0's
        # timestamp (without ffmpeg's own trimming) starts decoding exactly at frame i0.
        nxt = c.pts[i0 + 1] - c.pts[i0] if i0 + 1 < c.nframes else c.frame_dur
        cmd += ["-noaccurate_seek", "-ss", f"{c.pts[i0] + 0.25 * min(nxt, c.frame_dur):.6f}"]
    cmd += ["-i", c.path, "-filter_complex", graph, "-map", "[out]", "-an", "-sn", "-dn",
            *passthrough_args()]
    if count is not None:
        cmd += ["-frames:v", str(count)]
    cmd += ["-f", "rawvideo", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, bufsize=fsz * win, **_popen_kwargs())
    errbuf = []
    t_err = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    t_err.start()

    parts = {k: [] for k in ("snow", "noise", "tear", "chroma", "bright", "hd", "vd", "motion", "persist")}
    block_parts, osd_parts = [], []
    prev_coarse = None
    first = True
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            buf = proc.stdout.read(fsz * (lead if first and lead else win))
            first = False
            if not buf:
                break
            k = len(buf) // fsz
            if k == 0:
                break
            a = np.frombuffer(buf[:k * fsz], np.uint8).reshape(k, fsz)
            yall = a[:, :ysz + oy].reshape(k, AH + OSD_PH, AW)
            y = yall[:, :AH].astype(np.float32)
            cw = AW // 2
            uo = ysz + oy
            vo = uo + csz + ocz
            u = a[:, uo:uo + csz].reshape(k, AH // 2, cw).astype(np.float32)
            v = a[:, vo:vo + csz].reshape(k, AH // 2, cw).astype(np.float32)
            f, blk = _frame_features(y, u, v, noise_t)
            for key in f:
                parts[key].append(f[key])
            block_parts.append(blk)
            om = yall[:, AH:].astype(np.float32)
            osd = om.reshape(k, OSD_H, OSD_PH // OSD_H, OSD_W, AW // OSD_W).mean(axis=(2, 4))
            osd_parts.append(np.round(osd).astype(np.uint8).reshape(k, -1))

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
                sd = ys.std(axis=0).mean()
                ratio = float(m.std() / (sd / math.sqrt(len(ys)) + 1e-6))
            parts["persist"].append(np.full(k, ratio, np.float32))
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
    if n == 0 and count != 0:
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace")
        raise RuntimeError(f"Could not decode {os.path.basename(c.path)}\n{err[-1500:]}")
    blocks = np.concatenate(block_parts) if block_parts else np.zeros((0, 48), np.float16)
    osd = np.concatenate(osd_parts) if osd_parts else np.zeros((0, OSD_W * OSD_H), np.uint8)
    return feats, blocks, osd, n


def _store(c: Clip, feats, blocks, osd, n: int) -> None:
    """Attach measurements to a clip, lining decoded frames up with packet timestamps."""
    if n != c.nframes:
        if abs(n - c.nframes) <= max(3, 0.01 * n) and c.nframes > 0:
            m = min(n, c.nframes)
            c.pts, c.pkt_pos, c.pkt_size = c.pts[:m], c.pkt_pos[:m], c.pkt_size[:m]
            feats = {k: v[:m] for k, v in feats.items()}
            blocks, osd = blocks[:m], osd[:m]
        else:
            start = c.pts[0] if c.nframes else 0.0
            c.pts = start + np.arange(n) / c.fps
            c.pkt_pos = c.pkt_size = None
    c.feats, c.blocks, c.osd = feats, blocks, osd


def analyze_clip(c: Clip, progress=None, cancel: threading.Event | None = None,
                 noise_t: float = NOISE_T, snow_cover: float = SNOW_COVER_T) -> Clip:
    """Measure a whole clip in one pass."""
    _store(c, *_decode_features(c, 0, None, progress, cancel, noise_t, snow_cover))
    return c


def window_frames(c: Clip) -> int:
    return max(8, int(round(c.fps)))


def _chunks_for(c: Clip, workers: int) -> list[tuple[int, int]] | None:
    """Split one long clip into frame ranges that can be analysed in parallel, or None."""
    if workers < 2 or c.length < CHUNK_MIN_S * 2 or c.nframes < 200 or not c.intra_only:
        return None
    if abs(float(c.pts[0])) > 0.001 or not _seek_works(c):
        return None                         # exact seeking needs an indexed file starting at 0
    k = int(min(workers * 2, max(2, c.length // CHUNK_MIN_S)))
    w = window_frames(c)                    # chunk edges on window boundaries (same result as one pass)
    edges = np.unique(np.concatenate([[0], (np.linspace(0, c.nframes, k + 1)[1:-1] // w * w), [c.nframes]]).astype(int))
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def analyze_files(paths: list[str], progress=None, cancel: threading.Event | None = None,
                  settings: Settings | None = None, workers: int | None = None,
                  chunking: bool = True) -> Session:
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
            progress(0.02 + 0.98 * min(1.0, d / total), f"Analysing video... {min(100.0, 100 * d / total):4.1f}%")

    workers = workers or max(1, min(6, (os.cpu_count() or 2) - 1))
    # work units: whole clips, or chunks of long clips (one long HD recording -> parallel chunks)
    # (each chunk after the first also re-decodes the previous chunk's last frame, so the
    #  join can be checked: if that frame doesn't match exactly, the clip is redone in one pass)
    units = []
    for c in clips:
        ch = _chunks_for(c, workers) if chunking else None
        if ch:
            units += [(c, a, b - a) if a == 0 else (c, a - 1, b - a + 1) for a, b in ch]
        else:
            units.append((c, 0, None))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_decode_features, c, a, n, tick, cancel, settings.noise_t, settings.snow_cover,
                          0 if (n is None or a == 0) else 1)
                for c, a, n in units]
        try:
            results = [fu.result() for fu in futs]
        except BaseException:
            if cancel is not None:
                cancel.set()
            raise
    for c in clips:
        mine = [(u, r) for u, r in zip(units, results) if u[0] is c]
        if len(mine) == 1 and mine[0][0][2] is None:
            _store(c, *mine[0][1])
            continue
        ok = all(r[3] == u[2] for u, r in mine)
        for (_, prev), (_, cur) in zip(mine, mine[1:]):
            if not ok:
                break
            ok = all(np.array_equal(prev[0][k][-1:], cur[0][k][:1]) for k in ("bright", "noise", "chroma", "hd"))
        if not ok:                                       # chunks didn't line up: redo in one pass
            analyze_clip(c, None, cancel, settings.noise_t, settings.snow_cover)
            continue
        parts = [r if u[1] == 0 else ({k: v[1:] for k, v in r[0].items()}, r[1][1:], r[2][1:], r[3] - 1)
                 for u, r in mine]
        feats = {k: np.concatenate([r[0][k] for r in parts]) for k in parts[0][0]}
        _store(c, feats, np.concatenate([r[1] for r in parts]),
               np.concatenate([r[2] for r in parts]), c.nframes)
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
    pic = ~snow
    med_chroma = float(np.median(f["chroma"][pic])) if pic.any() else 0.0
    if med_chroma >= 3.0:
        # Analog receivers drop the colour when the signal gets weak, so colour loss is the
        # most reliable sign of breakup (detailed scenes fooled the older noise/tearing test).
        weak_raw = pic & ((f["chroma"] < max(COLOUR_LOSS_T, 0.2 * med_chroma)) | faint)
    else:   # black & white camera / receiver: fall back to tearing and partial snow
        s.notes.append("Footage has no colour: weak signal detected from tearing/partial snow.")
        weak_raw = pic & ((f["tear"] > TEAR_T) |
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

    for i, sg in enumerate(segs):
        first, last = i == 0, i == len(segs) - 1
        if sg.kind == "signal":
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
        a = max(segs[i - 1].f0, int(np.searchsorted(s.t, sg.t0 - LOOKBACK_S)))
        pre = labels[a:sg.f0]
        weak_frac = float((pre == LABEL_WEAK).mean()) if len(pre) else 0.0
        faded = weak_frac > 0.3
        if sg.duration < FLIGHT_GAP_S and not last:
            sg.label = (f"Dropout ({sg.duration:.1f}s) after weak signal - picture came back"
                        if faded else f"Dropout ({sg.duration:.1f}s) - picture came back")
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
    _split_stats(s)
    for sg in s.segments:
        if sg.kind == "signal":
            sg.weak_pct = 100.0 * float((labels[sg.f0:sg.f1] == LABEL_WEAK).mean())
    renumber_flights(s)


def _corr_rows(m: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Pearson correlation of every row of m with the vector t."""
    m = m.astype(np.float32)
    mc = m - m.mean(axis=1, keepdims=True)
    tc = (t - t.mean()).astype(np.float32)
    den = np.sqrt((mc * mc).sum(axis=1) * float((tc * tc).sum())) + 1e-6
    return (mc @ tc) / den


def _find_stats_screens(s: Session) -> dict[int, int]:
    """Find the Betaflight post-flight STATS screen shown between landing and unplugging.
    Returns {index of the power-off segment: first frame of the STATS screen before it}."""
    segs = s.segments
    if s.osd is None or s.n == 0:
        return {}
    offs = [i for i, sg in enumerate(segs)
            if sg.kind == "nosignal" and i > 0 and segs[i - 1].kind == "signal" and not sg.warn
            and (sg.duration >= FLIGHT_GAP_S or i == len(segs) - 1)]
    ends = {}
    for i in offs:            # OSD map averaged over the last second before each power-off
        a = max(segs[i - 1].f0, int(np.searchsorted(s.t, segs[i].t0 - 1.0)))
        if segs[i].f0 - a >= 3:
            ends[i] = s.osd[a:segs[i].f0].astype(np.float32).mean(axis=0)
    if not ends:
        return {}
    builtin = np.frombuffer(bytes.fromhex(_STATS_TEMPLATE_HEX), np.uint8).astype(np.float32)
    template, source = builtin, "built-in"
    keys = list(ends)
    if len(keys) >= 2:        # learn this pilot's STATS layout when several power-offs agree
        e = np.array([ends[k] for k in keys])
        cm = np.array([_corr_rows(e, row) for row in e])
        mid = int(np.argmax((cm > STATS_MATCH_T).sum(axis=1)))
        members = [keys[j] for j in range(len(keys)) if cm[mid, j] > STATS_MATCH_T]
        learned = np.mean([ends[k] for k in members], axis=0)
        # must look like a STATS screen, or a constant flight OSD could be "learned" instead
        if len(members) >= 2 and _corr_rows(learned[None], builtin)[0] > 0.5:
            template, source = learned, f"learned from {len(members)} power-offs"
    found = {}
    for i in keys:
        if _corr_rows(ends[i][None], template)[0] <= STATS_MATCH_T:
            continue
        flight = segs[i - 1]
        a = max(flight.f0, int(np.searchsorted(s.t, segs[i].t0 - STATS_MAX_S)))
        r = _corr_rows(s.osd[a:segs[i].f0], template)
        r = _smooth(r, 5)
        # walk back from the power-off while the STATS screen is showing (allow brief dips)
        g, below_since = len(r) - 1, None
        start = len(r)
        while g >= 0:
            if r[g] > STATS_MATCH_T - 0.15:
                start, below_since = g, None
            else:
                below_since = g if below_since is None else below_since
                if s.t[a + below_since] - s.t[a + g] > 0.5:
                    break
            g -= 1
        f_start = a + start
        if f_start >= segs[i].f0 or s.t[segs[i].f0 - 1] - s.t[f_start] < 0.5:
            continue
        if f_start <= flight.f0 and flight.duration > 20.0:
            continue          # a whole long "flight" can't be a STATS screen: don't touch it
        found[i] = f_start
    if found:
        s.notes.append(f"Post-flight STATS screen found before {len(found)} power-off(s) (pattern: {source}).")
    return found


def _split_stats(s: Session) -> None:
    """Give each post-flight STATS screen its own row (cut when trimming is on)."""
    found = _find_stats_screens(s)
    if not found:
        return
    out = []
    for i, sg in enumerate(s.segments):
        nxt = i + 1
        if sg.kind == "signal" and nxt in found:
            f_start = max(found[nxt], sg.f0)
            if f_start > sg.f0:
                out.append(Segment("signal", sg.f0, f_start, sg.t0, float(s.t[f_start]), "keep"))
            st = Segment("stats", f_start, sg.f1, float(s.t[f_start]), sg.t1,
                         "cut" if s.settings.trim_stats else "keep")
            st.label = f"Post-flight STATS screen ({st.duration:.1f}s)"
            out.append(st)
        else:
            out.append(sg)
    s.segments = out


def renumber_flights(s: Session) -> None:
    """Number the flights: a new flight starts after every real gap in the signal."""
    flight = 0
    for i, sg in enumerate(s.segments):
        if sg.kind != "signal":
            continue
        prev = s.segments[i - 1] if i > 0 else None
        if flight == 0 or (prev.kind == "nosignal" and prev.duration >= FLIGHT_GAP_S):
            flight += 1
            base = f"Flight {flight}"
        else:
            base = f"Flight {flight} (continued)"
        sg.flight = flight
        sg.label = base + (f"  -  {sg.weak_pct:.0f}% weak signal" if sg.weak_pct >= 0.5 else "")


# ---------------------------------------------------------------------------
# Editing the cuts by hand
# ---------------------------------------------------------------------------
MIN_PART_S = 0.2          # an edited part is never shorter than this


def _set_range(s: Session, sg: Segment, f0: int, f1: int) -> None:
    sg.f0, sg.f1 = f0, f1
    sg.t0 = float(s.t[f0])
    sg.t1 = float(s.t[f1 - 1] + s.fdur[f1 - 1])


def _refresh(s: Session) -> None:
    """Weak-signal share, flight numbers and lengths in the labels after an edit."""
    for sg in s.segments:
        if sg.kind == "signal":
            sg.weak_pct = 100.0 * float((s.labels[sg.f0:sg.f1] == LABEL_WEAK).mean()) if sg.f1 > sg.f0 else 0.0
        else:
            sg.label = re.sub(r"\((\d+(?:\.\d+)?)s\)", f"({sg.duration:.1f}s)", sg.label, count=1)
    renumber_flights(s)


def snapshot(s: Session):
    """Everything an edit can change (for Undo)."""
    return copy.deepcopy((s.segments, s.chapters, s.flight_names, s.edited))


def restore(s: Session, snap) -> None:
    s.segments, s.chapters, s.flight_names, s.edited = copy.deepcopy(snap)


def edge_limits(s: Session, i: int, which: str) -> tuple[float, float] | None:
    """Session-time range the start or end of segment i can be moved to (None = it can't move)."""
    segs = s.segments
    if (which == "start" and i == 0) or (which == "end" and i == len(segs) - 1):
        return None
    a, b = (segs[i - 1], segs[i]) if which == "start" else (segs[i], segs[i + 1])
    lo = s.t[min(s.n - 1, s.frame_at(a.t0 + MIN_PART_S) + 1)] if a.duration > MIN_PART_S else s.t[a.f0 + 1]
    hi = s.t[max(0, s.frame_at(b.t1 - MIN_PART_S))] if b.duration > MIN_PART_S else s.t[b.f1 - 1]
    return float(min(lo, hi)), float(max(lo, hi))


def move_edge(s: Session, i: int, which: str, t: float) -> float | None:
    """Move the start or end of segment i to the frame starting nearest to t (the
    neighbouring segment follows). Returns the new time, or None if it can't move."""
    lim = edge_limits(s, i, which)
    if lim is None:
        return None
    t = min(max(t, lim[0]), lim[1])
    a, b = (s.segments[i - 1], s.segments[i]) if which == "start" else (s.segments[i], s.segments[i + 1])
    g = s.nearest_frame_start(t)
    g = min(max(g, a.f0 + 1), b.f1 - 1)
    if g == b.f0:
        return float(s.t[g])
    _set_range(s, a, a.f0, g)
    _set_range(s, b, g, b.f1)
    s.edited = True
    _refresh(s)
    return float(s.t[g])


def split_segment(s: Session, i: int, t: float) -> bool:
    sg = s.segments[i]
    g = s.nearest_frame_start(t)
    if g <= sg.f0 or g >= sg.f1 or t - sg.t0 < MIN_PART_S - 1e-6 or sg.t1 - t < MIN_PART_S - 1e-6:
        return False
    new = copy.copy(sg)
    _set_range(s, sg, sg.f0, g)
    _set_range(s, new, g, new.f1)
    new.warn = False
    s.segments.insert(i + 1, new)
    s.edited = True
    _refresh(s)
    return True


def toggle_segment(s: Session, i: int) -> None:
    sg = s.segments[i]
    sg.action = "cut" if sg.action == "keep" else "keep"
    s.edited = True
    _refresh(s)


def reset_edits(s: Session) -> None:
    """Back to the detected cuts (chapters stay)."""
    classify(s, s.settings)
    s.edited = False


# ---------------------------------------------------------------------------
# Chapters
# ---------------------------------------------------------------------------
YT_MIN_CHAPTERS, YT_MIN_LEN = 3, 10.0


def add_chapter(s: Session, t: float, name: str = "") -> Chapter:
    cid = max((c.id for c in s.chapters), default=0) + 1
    ch = Chapter(float(s.t[s.frame_at(t)]), name or f"Chapter {len(s.chapters) + 1}", cid)
    s.chapters.append(ch)
    return ch


def chapter_list(s: Session, keep: np.ndarray | None = None) -> list[dict]:
    """Chapters of the export in output-video order:
    {"out": output time, "at": session time, "name", "mine": added by the user,
     "flight": flight number or 0, "id": user chapter id, "moved": was in a cut part}."""
    if s.n == 0:
        return []
    if keep is None:
        keep = s.keep_mask()
    if not keep.any():
        return []
    out_t = s.output_times(keep)
    segs = s.segments
    found = []
    last = 0
    for i, sg in enumerate(segs):
        if sg.kind != "signal" or sg.action != "keep" or sg.flight == last:
            continue
        last = sg.flight
        j = sg.f0
        if i > 0 and segs[i - 1].kind == "nosignal":
            # start the chapter right at the cut, not after the kept bit of static
            gap = np.flatnonzero(~keep[segs[i - 1].f0:sg.f0])
            if len(gap):
                j = segs[i - 1].f0 + int(gap[-1]) + 1
        found.append({"out": float(out_t[j]), "at": float(s.t[j]), "name": s.flight_names.get(sg.flight) or
                      f"Flight {sg.flight}", "mine": False, "flight": sg.flight, "id": 0, "moved": False})
    for ch in s.chapters:
        g = s.frame_at(ch.t)
        moved = not keep[g]
        if moved:
            nxt = np.flatnonzero(keep[g:])
            if not len(nxt):
                continue
            g += int(nxt[0])
        found.append({"out": float(out_t[g]), "at": float(s.t[g]), "name": ch.name, "mine": True,
                      "flight": 0, "id": ch.id, "moved": moved})
    found.sort(key=lambda c: (c["out"], 0 if c["mine"] else 1))
    merged: list[dict] = []
    for c in found:                    # one chapter per moment; a chapter of yours wins
        if merged and abs(merged[-1]["out"] - c["out"]) < 0.5:
            if c["mine"] and not merged[-1]["mine"]:
                merged[-1] = c
            continue
        merged.append(c)
    if merged:
        merged[0]["out"] = 0.0
    total = float(s.play_durations(keep).sum())
    for k, c in enumerate(merged):
        c["len"] = (merged[k + 1]["out"] if k + 1 < len(merged) else total) - c["out"]
    return merged


def youtube_text(chapters: list[dict]) -> str:
    return "\n".join(f"{fmt_time(c['out'])} {c['name']}" for c in chapters)


def youtube_checks(chapters: list[dict]) -> list[tuple[bool, str]]:
    """(ok, message) lines: does YouTube accept these as chapters?"""
    msgs = []
    if len(chapters) < YT_MIN_CHAPTERS:
        msgs.append((False, f"YouTube needs at least {YT_MIN_CHAPTERS} chapters; with {len(chapters)} it shows none."))
    for c in chapters:
        if c["len"] < YT_MIN_LEN:
            msgs.append((False, f'"{c["name"]}" is {c["len"]:.1f} s long; YouTube needs at least '
                                f'{YT_MIN_LEN:.0f} s per chapter.'))
        if c["moved"]:
            msgs.append((False, f'"{c["name"]}" was in a cut part, so it starts at the next kept moment.'))
    if not msgs:
        msgs.append((True, f"Ready for YouTube: {len(chapters)} chapters, first at 00:00, "
                           f"all at least {YT_MIN_LEN:.0f} s."))
    return msgs


def save_chapters_txt(s: Session, video_path: str, keep: np.ndarray | None = None) -> str | None:
    """<video>_chapters.txt: the chapter list ready to paste into a YouTube description."""
    ch = chapter_list(s, keep)
    if not ch:
        return None
    path = os.path.splitext(video_path)[0] + "_chapters.txt"
    text = youtube_text(ch) + "\n"
    notes = [m for ok, m in youtube_checks(ch) if not ok]
    if notes:
        text += "\n" + "\n".join("Note: " + m for m in notes) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def repair_hints(s: Session, keep: np.ndarray, fps: float, n_out: int) -> np.ndarray:
    """For every frame of a constant-frame-rate export of the kept frames: did the detector
    see anything suspicious there (weak signal, tearing, partial snow, colour loss, a jump)?"""
    hint = np.zeros(n_out, bool)
    idx = np.flatnonzero(keep)
    if not len(idx) or n_out == 0:
        return hint
    f = s.f
    bad = s.labels != LABEL_GOOD
    if "tear" in f:
        bad |= f["tear"] > TEAR_T
    if "snow" in f:
        bad |= f["snow"] > 0.25
    if "chroma" in f and keep.any():
        med = float(np.median(f["chroma"][keep])) if keep.any() else 0.0
        if med >= 3.0:
            bad |= f["chroma"] < 0.5 * med
    if "motion" in f and keep.any():
        mm = float(np.median(f["motion"][keep]))
        bad |= f["motion"] > max(25.0, 4.0 * mm)
    ot = s.output_times(keep)[idx]
    k = np.clip(np.searchsorted(ot, (np.arange(n_out) + 0.5) / fps, side="right") - 1, 0, len(idx) - 1)
    hint = bad[idx][k]
    r = max(3, int(round(0.15 * fps)))       # the frame rate conversion can shift frames a little
    return np.convolve(hint.astype(np.float32), np.ones(2 * r + 1), mode="same") > 0


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
        end = c.pts[pc.i1] + c.frame_dur
        if pc.i1 + 1 < c.nframes:
            end = min(c.pts[pc.i1 + 1], end)
        if pc.i0 > 0:
            lines.append(f"inpoint {start + eps:.6f}")
        if pc.i1 + 1 < c.nframes:
            lines.append(f"outpoint {end - eps:.6f}")
        total += end - start
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return total


def write_chapters(s: Session, path: str, keep: np.ndarray | None = None) -> int:
    """FFmpeg metadata file with the export's chapters. Returns the number of chapters."""
    if keep is None:
        keep = s.keep_mask()
    ch = chapter_list(s, keep)
    lines = [";FFMETADATA1", f"title={APP_NAME} export"]
    for c in ch:
        name = c["name"].replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;") \
            .replace("#", "\\#").replace("\n", " ")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(c['out'] * 1000))}",
                  f"END={int(round((c['out'] + max(c['len'], 0.001)) * 1000))}", f"title={name}"]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(ch)


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
    txt = save_chapters_txt(s, out_path, keep) if chapters and keep is None else None
    return {"pieces": len(pieces), "duration": total, "chapters_txt": txt}


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
Recorded footage: {fmt_time(s.duration)} &nbsp; Exported: {fmt_time(s.kept_duration())}</p>
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
# Diagnostics (for tuning the detector on real footage)
# ---------------------------------------------------------------------------
DIAG_FRAME_BUDGET = 16 * 1024 * 1024    # bytes of sample frames to put in the zip


def _read_frame_jpeg(c: Clip, fi: int) -> bytes | None:
    """The exact original frame of an MJPEG clip, read straight from its byte position."""
    if c.codec != "mjpeg" or c.pkt_pos is None or fi >= len(c.pkt_pos) or c.pkt_pos[fi] < 0:
        return None
    size = int(c.pkt_size[fi])
    try:
        with open(c.path, "rb") as fh:
            fh.seek(int(c.pkt_pos[fi]))
            data = fh.read(size + 32)
    except OSError:
        return None
    k = data.find(b"\xff\xd8", 0, 32)
    return data[k:k + size] if k >= 0 else None


def _encode_frame_jpeg(c: Clip, fi: int) -> bytes:
    """Fallback for non-MJPEG clips: decode the frame and save it as a high-quality JPEG."""
    cmd = [tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, c.pts[fi] + 0.001):.4f}",
           "-i", c.path, "-frames:v", "1", "-an", "-c:v", "mjpeg", "-q:v", "1", "-f", "mjpeg", "pipe:1"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, **_popen_kwargs())
    return p.stdout


def _diag_picks(s: Session) -> list[tuple[int, str]]:
    """Frames worth looking at, most useful first."""
    lab = s.labels
    snow = lab == LABEL_SNOW
    merged = _smooth(snow, s.frames_for(2.0)) > 0.5          # static with short blips merged in
    static_runs = [(a, b) for a, b in _runs(merged) if b - a >= s.frames_for(1.0)]
    static_runs.sort(key=lambda r: r[1] - r[0], reverse=True)

    def spread(idx, k):
        if len(idx) == 0:
            return []
        return [int(idx[i]) for i in np.unique(np.linspace(0, len(idx) - 1, min(k, len(idx))).astype(int))]

    inside, flights, edges = [], [], []
    for a, b in static_runs:
        # frames inside static that were NOT counted as static (these split the static up)
        inside += [(a + i, "in-static-not-snow") for i in spread(np.flatnonzero(lab[a:b] != LABEL_SNOW), 8)]
        inside.append(((a + b) // 2, "in-static"))
    for a, b in _runs(~merged):
        if b - a < s.frames_for(5.0):
            continue
        for want, name in ((LABEL_WEAK, "flight-weak"), (LABEL_GOOD, "flight-good")):
            flights += [(a + i, name) for i in spread(np.flatnonzero(lab[a:b] == want), 4)]
    step = s.frames_for(0.33)
    for a, b in static_runs:
        edges += [(g, "static-start") for g in range(a - s.frames_for(3.0), a + s.frames_for(1.0), step)]
        edges += [(g, "static-end") for g in range(b - s.frames_for(1.0), b + s.frames_for(3.0), step)]
    out, seen = [], set()
    for g, tag in inside + flights + edges:
        if 0 <= g < s.n and g not in seen:
            seen.add(g)
            out.append((g, tag))
    return out


def save_diagnostics(s: Session, zip_path: str, progress=None, cancel=None,
                     frame_budget: int = DIAG_FRAME_BUDGET) -> dict:
    """Write measurements + sample original frames into one zip that can be sent for tuning."""
    def prog(f, msg):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if progress:
            progress(f, msg)

    prog(0.0, "Saving diagnostics: measurements...")
    keep = s.keep_mask()
    labels_txt = ("good", "weak", "static")
    tmp = zip_path + ".part"
    n_frames = 0
    frame_bytes = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        # -- summary ----------------------------------------------------------
        try:
            ffv = run_text([tool("ffmpeg"), "-hide_banner", "-version"]).splitlines()[0]
        except Exception as e:
            ffv = f"(ffmpeg version unknown: {e})"
        st = s.settings
        lines = [f"{APP_NAME} {APP_VERSION} diagnostics",
                 f"created: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"system: {platform.platform()} / Python {platform.python_version()} / numpy {np.__version__}",
                 f"ffmpeg: {ffv}", "",
                 f"settings: min_cut={st.min_cut} pad={st.pad} faint_protect={st.faint_protect} "
                 f"snow_cover={st.snow_cover} noise_t={st.noise_t}",
                 f"constants: NOISE_T={NOISE_T} SNOW_COVER_T={SNOW_COVER_T} TEAR_T={TEAR_T} "
                 f"PERSIST_T={PERSIST_T} analysis={AW}x{AH} block={BLOCK}", "",
                 "clips:"]
        for c in s.clips:
            lines.append(f"  {os.path.basename(c.path)}  codec={c.codec} {c.width}x{c.height} fps={c.fps:.4f} "
                         f"frames={c.nframes} length={c.length:.2f}s intra={c.intra_only} "
                         f"seekable={c.seek_ok} first_pts={c.pts[0]:.4f} bytes={os.path.getsize(c.path)}")
        lines += ["", "label share: " + ", ".join(
            f"{labels_txt[k]}={100 * float((s.labels == k).mean()):.1f}%" for k in range(3)), "",
            summary_table(s)]
        z.writestr("info.txt", "\n".join(lines) + "\n")
        z.writestr("README.txt",
                   "Diagnostics from FPV DVR Stitcher - send this whole zip to whoever is tuning the detector.\n\n"
                   "info.txt          settings, clip details and the segment list\n"
                   "measurements.csv  every number the detector computed, for every frame\n"
                   "blocks.npy        per-frame noise level of each of the 8x6 regions (numpy float16)\n"
                   "osd.npy           per-frame map of OSD-like text strokes, 32x18 (numpy uint8)\n"
                   "frames/           sample frames, exact copies of the originals where possible;\n"
                   "                  file names give session time, why the frame was picked and its label\n")

        # -- per-frame measurements -------------------------------------------
        keys = list(s.f)
        buf = io.StringIO()
        buf.write("file,frame,pts,session_t,label,keep," + ",".join(keys) + "\n")
        names = [os.path.basename(c.path) for c in s.clips]
        cols = [s.f[k] for k in keys]
        for g in range(s.n):
            c = s.clips[s.clip_idx[g]]
            fi = s.frame_idx[g]
            buf.write(f"{names[s.clip_idx[g]]},{fi},{c.pts[fi]:.4f},{s.t[g]:.4f},{s.labels[g]},{int(keep[g])},"
                      + ",".join("nan" if not np.isfinite(col[g]) else f"{col[g]:.3f}" for col in cols) + "\n")
            if g % 5000 == 0:
                prog(0.15 * g / max(1, s.n), "Saving diagnostics: measurements...")
        z.writestr("measurements.csv", buf.getvalue())
        if s.blocks is not None:
            b = io.BytesIO()
            np.save(b, s.blocks.astype(np.float16))
            z.writestr("blocks.npy", b.getvalue())
        if s.osd is not None:
            b = io.BytesIO()
            np.save(b, s.osd)
            z.writestr("osd.npy", b.getvalue())

        # -- sample frames ------------------------------------------------------
        picks = _diag_picks(s)
        for i, (g, tag) in enumerate(picks):
            prog(0.2 + 0.8 * i / max(1, len(picks)), f"Saving diagnostics: frames {i + 1}/{len(picks)}...")
            c = s.clips[s.clip_idx[g]]
            fi = int(s.frame_idx[g])
            data = _read_frame_jpeg(c, fi)
            exact = data is not None
            if not exact:
                data = _encode_frame_jpeg(c, fi)
            if not data:
                continue
            if frame_bytes + len(data) > frame_budget:
                break
            name = (f"frames/{float(s.t[g]):08.2f}s_{tag}_{labels_txt[int(s.labels[g])]}_"
                    f"{os.path.splitext(names[s.clip_idx[g]])[0]}_f{fi:05d}{'' if exact else '_reenc'}.jpg")
            z.writestr(zipfile.ZipInfo(name, time.localtime()[:6]), data, compress_type=zipfile.ZIP_STORED)
            frame_bytes += len(data)
            n_frames += 1
    os.replace(tmp, zip_path)
    prog(1.0, "Diagnostics saved.")
    return {"path": zip_path, "size": os.path.getsize(zip_path), "frames": n_frames,
            "frames_offered": len(picks)}


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
    d = Settings()
    ap.add_argument("--min-cut", type=float, default=d.min_cut)
    ap.add_argument("--pad", type=float, default=d.pad)
    ap.add_argument("--cover", type=float, default=d.snow_cover * 100, help="snow coverage %% for static")
    ap.add_argument("--no-trim-stats", action="store_true", help="keep the post-flight STATS screen")
    ap.add_argument("--no-faint-protect", action="store_true")
    ap.add_argument("--report", action="store_true", help="also write the signal-loss report")
    ap.add_argument("--dump", help="write per-frame measurements to this CSV (for tuning)")
    ap.add_argument("--diagnostics", help="write a diagnostics zip (measurements + sample frames)")
    a = ap.parse_args(argv)
    files = sorted(a.files, key=natural_key)
    st = Settings(min_cut=a.min_cut, pad=a.pad, faint_protect=not a.no_faint_protect,
                  snow_cover=a.cover / 100.0, trim_stats=not a.no_trim_stats)

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
    if a.diagnostics:
        info = save_diagnostics(s, a.diagnostics, prog)
        print(file=sys.stderr)
        print(f"diagnostics: {info['path']} ({info['size'] / 1e6:.1f} MB, {info['frames']} frames)")
    if a.output:
        export_video(s, a.output, prog)
        print(file=sys.stderr)
        if a.report:
            print("report:", write_report(s, a.output, prog))
        print("wrote", a.output)


if __name__ == "__main__":
    main()
