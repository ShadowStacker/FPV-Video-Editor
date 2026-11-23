"""
FPV DVR Stitcher - preview player.

Plays the session in the preview box: either "as exported" (the kept parts only, joined the
way the export will be) or the original recording. FFmpeg decodes at the size of the
preview and at about 30 pictures a second whatever the speed (faster speeds skip frames),
so playing at 8x costs no more than at 1x.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time

import numpy as np

import dvr_core as dc

DISPLAY_FPS = 30.0
SPEEDS = (0.5, 1.0, 2.0, 4.0, 8.0)


def player_dir() -> str:
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share"),
                     "FPV DVR Stitcher", "player")
    os.makedirs(d, exist_ok=True)
    return d


def fit(w: int, h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """Largest even size with the picture's shape that fits in the box."""
    k = min(box_w / max(1, w), box_h / max(1, h))
    return max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)


class Timeline:
    """Maps output-video time <-> session time for one way of playing."""

    def __init__(self, s: dc.Session, exported: bool):
        self.s = s
        self.exported = exported
        self.keep = s.keep_mask() if exported else np.ones(s.n, bool)
        self.idx = np.flatnonzero(self.keep)
        d = s.play_durations(self.keep)
        self.ot_all = np.cumsum(d) - d
        self.ot = self.ot_all[self.idx]
        self.total = float(d.sum())

    def to_session(self, o: float) -> float:
        if not len(self.idx):
            return 0.0
        k = int(np.clip(np.searchsorted(self.ot, o + 1e-6, side="right") - 1, 0, len(self.idx) - 1))
        g = int(self.idx[k])
        return float(self.s.t[g] + min(max(0.0, o - self.ot[k]), self.s.fdur[g] * 0.999))

    def to_out(self, t: float) -> float | None:
        """Output time of session time t (the next kept moment if t is cut; None past the end)."""
        g = self.s.frame_at(t)
        if self.keep[g]:
            return float(self.ot_all[g] + max(0.0, t - self.s.t[g]))
        nxt = np.flatnonzero(self.keep[g:])
        return float(self.ot_all[g + nxt[0]]) if len(nxt) else None


class Player:
    """Decodes frames on a background thread; the window asks for the frame due now."""

    def __init__(self, s: dc.Session, exported: bool, speed: float, size: tuple[int, int]):
        self.s, self.speed, self.size = s, speed, size
        self.tl = Timeline(s, exported)
        self.q: queue.Queue = queue.Queue(maxsize=8)
        self.proc = None
        self.thread = None
        self.stop_evt = threading.Event()
        self.o0 = 0.0
        self.wall0 = 0.0
        self.shown = -1
        self.error = ""

    def start(self, t_session: float) -> bool:
        """Start playing at session time t (or the next kept moment). False if nothing to play."""
        o = self.tl.to_out(t_session)
        if o is None or o >= self.tl.total - 0.02:
            return False
        keep = self.tl.keep
        pieces = dc.build_pieces(self.s, keep)
        if not pieces:
            return False
        d = player_dir()
        lst = os.path.join(d, f"play_{id(self)}.txt")
        dc.write_concat_list(pieces, lst, d)
        self.o0 = o
        w, h = self.size
        vf = (f"setpts=(PTS-STARTPTS)/{self.speed:g},fps={DISPLAY_FPS:g},"
              f"scale={w}:{h}:flags=bilinear,format=rgb24")
        cmd = [dc.tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{o:.4f}", "-f", "concat", "-safe", "0",
               "-i", lst, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24",
               "pipe:1"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, bufsize=w * h * 3 * 2, **dc._popen_kwargs())
        self.lst = lst
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.wall0 = 0.0
        return True

    def _read(self):
        w, h = self.size
        fsz = w * h * 3
        k = 0
        try:
            while not self.stop_evt.is_set():
                buf = self.proc.stdout.read(fsz)
                if not buf or len(buf) < fsz:
                    break
                while not self.stop_evt.is_set():
                    try:
                        self.q.put((k, buf), timeout=0.1)
                        break
                    except queue.Full:
                        continue
                k += 1
        except Exception as e:      # noqa: BLE001 - shown as "end of playback"
            self.error = str(e)
        finally:
            try:
                self.q.put((None, None), timeout=1.0)
            except queue.Full:
                pass

    def out_time(self, k: int) -> float:
        return self.o0 + k * self.speed / DISPLAY_FPS

    def due(self):
        """The newest decoded frame that is due now: (frame number, rgb bytes), "end", or None."""
        now = time.perf_counter()
        best = None
        while True:
            try:
                k, buf = self.q.queue[0]
            except IndexError:
                break
            if k is None:
                if best is None:
                    self.q.get_nowait()
                    return "end"
                break
            if self.wall0 == 0.0:
                self.wall0 = now - k / DISPLAY_FPS        # the clock starts with the first frame
            if self.wall0 + k / DISPLAY_FPS > now:
                break
            best = self.q.get_nowait()
        if best is not None:
            self.shown = best[0]
        return best

    def stop(self):
        self.stop_evt.set()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.remove(self.lst)
        except (OSError, AttributeError):
            pass
