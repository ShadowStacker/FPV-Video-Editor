"""
FPV DVR Stitcher - frame repair for analog FPV footage.

Broken frames
    When the signal breaks up, single frames (or a few in a row) come out rolled, torn,
    blown out or full of snow while the frames either side are fine. A frame is treated as
    broken when it is far from both the last good frame before it and the first good frame
    after it, while those two agree with each other. Only frames the detector already found
    suspicious (weak signal, tearing, snow, a sudden jump) are considered, and only runs of
    up to MAX_RUN frames: longer breakups are real and stay in the video.
    Broken frames are replaced by the nearest good frame; when the AI add-on is installed
    the enhance export then redraws them as in-between frames with RIFE (see rife_fill).

Line repair
    Analog breakup also leaves bands of rainbow colour noise and bright/dark streak lines
    across the picture for a single frame. Short-lived bands that cover most of the width are
    found row by row and rebuilt from the frames before and after, after lining them up with
    this frame (the camera moves between frames), so the real detail comes back. When neither
    neighbour lines up (very fast motion, or it is damaged too), the band is filled in from the
    rows above and below. Colour noise bands get their colour replaced (brightness and detail
    stay); streak lines are replaced completely.
    Anything that stays in place from frame to frame (the OSD, the horizon) is left alone.
"""
from __future__ import annotations

import os
import shutil
from collections import deque

import numpy as np

MAX_RUN = 6            # longest run of broken frames that is replaced
SIG_W, SIG_H = 48, 27  # size of the small grey picture frames are compared with
ALPHA, BETA = 1.3, 6.0 # broken: differs from both neighbours by > ALPHA * (their difference) + BETA


# ---------------------------------------------------------------------------
# Broken frames
# ---------------------------------------------------------------------------
def _area(a: np.ndarray, h: int, w: int) -> np.ndarray:
    """Area-average a 2-D array down to h x w (any input size)."""
    ys = np.linspace(0, a.shape[0], h + 1).astype(int)
    xs = np.linspace(0, a.shape[1], w + 1).astype(int)
    s = np.add.reduceat(np.add.reduceat(a, ys[:-1], axis=0), xs[:-1], axis=1)
    return s / np.outer(np.diff(ys), np.diff(xs))


def signature(frame: np.ndarray) -> np.ndarray:
    """Small grey version of an RGB frame for comparing frames."""
    f = frame[::2, ::2].astype(np.float32)
    g = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
    return _area(g, SIG_H, SIG_W).astype(np.float32)


def _d(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


class BrokenFrames:
    """Streaming broken-frame finder: push() frames in order, get frames back in the same
    order (broken ones replaced) after a look-ahead of MAX_RUN frames; flush() at the end.
    hint(i) says whether the detector found anything suspicious near frame i (0-based)."""

    def __init__(self, hint=None, max_run: int = MAX_RUN):
        self.hint = hint
        self.max_run = max_run
        self.buf: deque = deque()          # (index, frame, signature) not yet given back
        self.good = None                   # last good frame given back
        self.n = 0
        self.runs: list[tuple[int, int, int, int]] = []   # (first index, length, good before, good after)
        self.replaced = 0

    def push(self, frame: np.ndarray) -> list[np.ndarray]:
        self.buf.append((self.n, frame, signature(frame)))
        self.n += 1
        out: list[np.ndarray] = []
        while len(self.buf) > self.max_run:
            out += self._step()
        return out

    def flush(self) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        while self.buf:
            out += self._step()
        return out

    def _step(self) -> list[np.ndarray]:
        if self.good is None:
            e = self.buf.popleft()
            self.good = e
            return [e[1]]
        gi, gframe, gsig = self.good
        for r in range(1, min(self.max_run, len(self.buf) - 1) + 1):
            run = [self.buf[k] for k in range(r)]
            if self.hint is not None and not any(self.hint(e[0]) for e in run):
                continue
            ri, rframe, rsig = self.buf[r]
            limit = ALPHA * _d(gsig, rsig) + BETA
            if all(min(_d(gsig, e[2]), _d(e[2], rsig)) > limit for e in run):
                out = []
                for k in range(r):            # nearest good frame: first half <- before, second half <- after
                    out.append(gframe if (k + 1) * 2 <= r + 1 else rframe)
                    self.buf.popleft()
                self.runs.append((run[0][0], r, gi, ri))
                self.replaced += r
                return out
        e = self.buf.popleft()
        self.good = e
        return [e[1]]


def rife_fill(folder: str, runs: list[tuple[list[str], str, str]], rife_exe: str, model_dir: str,
              run_tool, cancel=None) -> int:
    """Redraw replaced frames as RIFE in-between frames.
    runs: (files of the replaced frames in order, good frame before, good frame after).
    RIFE's folder mode makes `n` frames evenly spread over its input folder, so the good
    frame pairs of all runs of the same length go in one folder: one RIFE call per length.
    Returns the number of frames redrawn."""
    by_len: dict[int, list] = {}
    for files, a, b in runs:
        by_len.setdefault(len(files), []).append((files, a, b))
    done = 0
    for r, group in sorted(by_len.items()):
        src = os.path.join(folder, f"pairs{r}")
        dst = os.path.join(folder, f"mid{r}")
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(src)
        os.makedirs(dst)
        k = 1
        for files, a, b in group:
            for f in (a, b):
                shutil.copyfile(f, os.path.join(src, f"{k:08d}.jpg"))
                k += 1
        count = 2 * len(group) * (r + 1)          # time step 1 / (r + 1) between every two inputs
        run_tool([rife_exe, "-i", src, "-o", dst, "-n", str(count), "-m", model_dir, "-f", "%08d.jpg"],
                 cancel, "Redrawing broken frames (RIFE)")
        for m, (files, a, b) in enumerate(group):
            for j, f in enumerate(files, 1):
                out = os.path.join(dst, f"{2 * m * (r + 1) + j + 1:08d}.jpg")
                if os.path.exists(out):
                    os.replace(out, f)
                    done += 1
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
    return done


# ---------------------------------------------------------------------------
# Line repair
# ---------------------------------------------------------------------------
def _running_median(x: np.ndarray, r: int) -> np.ndarray:
    """Median over [i-r, i+r] along axis 0 (edges padded)."""
    pad = [(r, r)] + [(0, 0)] * (x.ndim - 1)
    win = np.lib.stride_tricks.sliding_window_view(np.pad(x, pad, mode="edge"), 2 * r + 1, axis=0)
    return np.median(win, axis=-1)


def _bands(rows: np.ndarray, max_h: int, gap: int = 1) -> list[tuple[int, int]]:
    """Runs of True rows (allowing `gap` missing rows inside), at most max_h tall."""
    out = []
    idx = np.flatnonzero(rows)
    if not len(idx):
        return out
    a = b = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - b <= gap + 1:
            b = i
        else:
            out.append((a, b + 1))
            a = b = i
    out.append((a, b + 1))
    return [(a, b) for a, b in out if b - a <= max_h]


def _shift(g0: np.ndarray, g1: np.ndarray, known: np.ndarray, max_shift: int = 64) -> tuple[int, int]:
    """How far the picture in g1 has moved relative to g0 (rows marked not known are ignored):
    phase correlation, so it is fast and not fooled by brightness changes."""
    h, w = g0.shape
    win = np.outer(np.hanning(h) * known, np.hanning(w)).astype(np.float32)
    f0 = np.fft.rfft2((g0 - g0[known].mean()) * win)
    f1 = np.fft.rfft2((g1 - g1[known].mean()) * win)
    r = f0 * np.conj(f1)
    r /= np.abs(r) + 1e-6
    c = np.fft.irfft2(r, s=(h, w))
    dy, dx = np.unravel_index(int(np.argmax(c)), c.shape)
    dy = dy - h if dy > h // 2 else dy
    dx = dx - w if dx > w // 2 else dx
    if abs(dy) > max_shift or abs(dx) > max_shift * 2:
        return 0, 0
    return int(dy), int(dx)


class LineRepair:
    """Fills in short-lived horizontal colour-noise bands and streak lines: from the frames
    before and after, lined up with this one (rebuilds the real detail), or, when they don't
    line up, from the rows above and below."""
    CHROMA_K, CHROMA_C, CHROMA_COVER = 2.5, 3.0, 0.4   # band: colour noise > K x normal + C over 40% of width
    LUMA_T, LUMA_COVER = 30.0, 0.75                    # streak: brighter (or darker) by > 30 over 3/4 of the width

    ALIGN_ERR = 14.0      # a neighbour lines up if the rows around the band differ by less than this

    def __init__(self):
        self.prev = None          # rows that had a band in the previous frame (detection scale)
        self.fixed_frames = 0
        self.fixed_rows = 0
        self.aligned_bands = 0    # bands rebuilt from lined-up neighbouring frames
        self._q: list[np.ndarray] = []
        self._last = None

    def detect(self, frame: np.ndarray):
        f = frame[::2, ::2].astype(np.float32)          # detection at half size
        H2, W2 = f.shape[:2]
        y = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
        cb = f[..., 2] - y
        cr = f[..., 0] - y
        bw = max(4, W2 // 80)
        nb = max(1, (W2 - 1) // bw)
        e = np.abs(np.diff(cb, axis=1)) + np.abs(np.diff(cr, axis=1))
        E = e[:, :nb * bw].reshape(H2, nb, bw).mean(axis=2)
        rowE = E.mean(axis=1)
        ref = _running_median(rowE, max(8, H2 // 18))
        thr = self.CHROMA_K * ref + self.CHROMA_C
        chroma = (rowE > thr) & ((E > thr[:, None]).mean(axis=1) >= self.CHROMA_COVER)
        ym = y[:, :nb * bw].reshape(H2, nb, bw).mean(axis=2)
        refy = _running_median(ym, 3)
        dev = ym - refy
        luma = (((dev > self.LUMA_T).mean(axis=1) >= self.LUMA_COVER) |
                ((dev < -self.LUMA_T).mean(axis=1) >= self.LUMA_COVER))
        # grow each colour band over its fainter edges
        soft = rowE > 1.3 * ref + 1.5
        cb_ = []
        for a, b in _bands(chroma, H2):
            while a > 0 and soft[a - 1]:
                a -= 1
            while b < H2 and soft[b]:
                b += 1
            if b - a <= max(6, H2 // 16) and not (cb_ and a <= cb_[-1][1]):
                cb_.append((a, b))
                chroma[a:b] = True
        lb_ = _bands(luma, max(2, H2 // 120), gap=0)
        return cb_, lb_, chroma | luma

    # ---- streaming: each frame is repaired once the frame after it has arrived, so both
    # neighbours can lend their (lined-up) rows to a damaged band
    def push(self, frame: np.ndarray) -> list[np.ndarray]:
        self._q.append(frame)
        if len(self._q) < 2:
            return []
        cur, nxt = self._q
        out = self._repair(cur, self._last, nxt)
        self._last = cur
        self._q = [nxt]
        return [out]

    def flush(self) -> list[np.ndarray]:
        out = [self._repair(f, self._last, None) for f in self._q]
        self._q = []
        return out

    def __call__(self, frame: np.ndarray) -> np.ndarray:
        """One frame on its own (no neighbours): bands are filled from the rows above and below."""
        return self._repair(frame, None, None)

    def _repair(self, frame: np.ndarray, before: np.ndarray | None, after: np.ndarray | None) -> np.ndarray:
        cbands, lbands, rows = self.detect(frame)
        prev = self.prev
        if prev is not None:
            prev = np.convolve(prev.astype(np.float32), np.ones(7), mode="same") > 0
        self.prev = rows

        def transient(a, b):
            return prev is None or prev[a:b].mean() < 0.5

        cbands = [b for b in cbands if transient(*b)]
        lbands = [b for b in lbands if transient(*b)]
        if not cbands and not lbands:
            return frame
        out = frame.astype(np.float32)
        H = out.shape[0]
        for kind, bands in (("c", cbands), ("l", lbands)):
            for a, b in bands:
                top = max(0, 2 * a - 2)
                bot = min(H, 2 * b + 2)
                fill = self._from_neighbours(frame, top, bot, before, after)
                aligned = fill is not None
                if aligned:
                    self.aligned_bands += 1
                else:
                    above = out[top - 1] if top > 0 else out[bot] if bot < H else None
                    below = out[bot] if bot < H else above
                    if above is None:
                        continue
                    w = ((np.arange(top, bot) - (top - 1)) / (bot - top + 1))[:, None, None]
                    fill = (1 - w) * above[None] + w * below[None]
                if kind == "l" or aligned:  # real rows from the neighbours: use them whole
                    out[top:bot] = fill
                else:                       # keep the brightness, replace the colour
                    yb = out[top:bot] @ np.array([0.299, 0.587, 0.114], np.float32)
                    yf = fill @ np.array([0.299, 0.587, 0.114], np.float32)
                    out[top:bot] = fill + (yb - yf)[..., None]
                self.fixed_rows += bot - top
        self.fixed_frames += 1
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    def _from_neighbours(self, frame, top, bot, before, after):
        """The damaged rows taken from the frames before and after, lined up with this frame first
        (the camera moves between frames). None if neither lines up well enough."""
        H, W = frame.shape[:2]
        ctx = max(16, H // 18)
        a, b = max(0, top - ctx), min(H, bot + ctx)
        if (top - a) + (b - bot) < ctx:
            return None
        gray = lambda img: img[a:b].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        g0 = gray(frame)
        known = np.ones(b - a, bool)
        known[top - a:bot - a] = False          # the band itself can't be used for lining up
        fills = []
        for nb in (before, after):
            if nb is None or nb.shape != frame.shape:
                continue
            dy, dx = _shift(g0, gray(nb), known)
            ys = np.clip(np.arange(a, b) - dy, 0, H - 1)
            xs = np.clip(np.arange(W) - dx, 0, W - 1)
            moved = nb[ys][:, xs].astype(np.float32)
            err = float(np.abs(moved[known] @ np.array([0.299, 0.587, 0.114], np.float32) - g0[known]).mean())
            if err < self.ALIGN_ERR:
                fills.append(moved[top - a:bot - a])
        if not fills:
            return None
        return fills[0] if len(fills) == 1 else 0.5 * (fills[0] + fills[1])
