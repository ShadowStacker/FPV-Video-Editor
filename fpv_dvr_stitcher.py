"""
FPV DVR Stitcher - Windows desktop app (Tkinter).

Select the DVR clips from a flying session, let it find the dead static
between batteries, review the result on a timeline, and export one video.
"""
from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np

import dvr_core as dc

VIDEO_EXT = (".avi", ".mp4", ".mov", ".mkv", ".ts", ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv")

COL_GOOD = "#2e9e44"
COL_WEAK = "#f0a020"
COL_SNOW = "#c8352b"
COL_KEEP = "#2f6fd6"
COL_CUT = "#d9d9d9"
COL_BG = "#ffffff"


def _enable_dpi_awareness():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{dc.APP_NAME} {dc.APP_VERSION}")
        root.geometry("1180x820")
        root.minsize(900, 640)
        self.files: list[str] = []
        self.session: dc.Session | None = None
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.thumb = None
        self.sel_seg: int | None = None
        self.thumb_req = 0

        style = ttk.Style()
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 6))
        try:
            from tkinter import font as tkfont
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        except Exception:
            line = 16
        style.configure("Treeview", rowheight=max(22, line + 8))   # scales with Windows display zoom

        self._build()
        self.root.after(100, self._poll)
        self.root.after(300, self._check_ffmpeg)

    # ------------------------------------------------------------------ UI
    def _build(self):
        r = self.root
        top = ttk.Frame(r, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Button(top, text="Add files...", command=self.add_files).pack(side="left")
        ttk.Button(top, text="Add folder...", command=self.add_folder).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Remove", command=self.remove_files).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Clear", command=self.clear_files).pack(side="left", padx=(6, 0))
        self.files_lbl = ttk.Label(top, text="No files added")
        self.files_lbl.pack(side="left", padx=12)

        mid = ttk.Frame(r, padding=(10, 6))
        mid.pack(fill="x")
        # file list
        lf = ttk.LabelFrame(mid, text="DVR clips (played in name order)", padding=4)
        lf.pack(side="left", fill="both", expand=True)
        self.lb = tk.Listbox(lf, selectmode="extended", height=9, activestyle="none")
        sb = ttk.Scrollbar(lf, orient="vertical", command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")

        # settings
        sf = ttk.LabelFrame(mid, text="Settings", padding=8)
        sf.pack(side="left", fill="y", padx=8)
        self.v_min = tk.StringVar(value="5")
        self.v_pad = tk.StringVar(value="0.5")
        self.v_cover = tk.StringVar(value=str(int(dc.SNOW_COVER_T * 100)))
        self.v_fmt = tk.StringVar(value=".avi")
        self.v_report = tk.BooleanVar(value=True)
        self.v_faint = tk.BooleanVar(value=True)
        row = 0
        ttk.Label(sf, text="Cut static longer than").grid(row=row, column=0, sticky="w")
        e = ttk.Spinbox(sf, from_=1, to=120, increment=1, width=6, textvariable=self.v_min,
                        command=self.reapply)
        e.grid(row=row, column=1, padx=4)
        e.bind("<Return>", lambda ev: self.reapply())
        e.bind("<FocusOut>", lambda ev: self.reapply())
        ttk.Label(sf, text="seconds").grid(row=row, column=2, sticky="w")
        row += 1
        ttk.Label(sf, text="Keep static at each cut").grid(row=row, column=0, sticky="w")
        e = ttk.Spinbox(sf, from_=0, to=5, increment=0.25, width=6, textvariable=self.v_pad,
                        command=self.reapply)
        e.grid(row=row, column=1, padx=4)
        e.bind("<Return>", lambda ev: self.reapply())
        e.bind("<FocusOut>", lambda ev: self.reapply())
        ttk.Label(sf, text="seconds").grid(row=row, column=2, sticky="w")
        row += 1
        ttk.Label(sf, text="Static = snow covers at least").grid(row=row, column=0, sticky="w")
        e = ttk.Spinbox(sf, from_=50, to=100, increment=5, width=6, textvariable=self.v_cover,
                        command=self.reapply)
        e.grid(row=row, column=1, padx=4)
        e.bind("<Return>", lambda ev: self.reapply())
        e.bind("<FocusOut>", lambda ev: self.reapply())
        ttk.Label(sf, text="% of frame").grid(row=row, column=2, sticky="w")
        row += 1
        ttk.Checkbutton(sf, text="Protect faint picture hidden in static (crash finding)",
                        variable=self.v_faint, command=self.reapply).grid(row=row, column=0, columnspan=3,
                                                                           sticky="w", pady=(4, 0))
        row += 1
        ttk.Separator(sf).grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
        row += 1
        ttk.Label(sf, text="Output:").grid(row=row, column=0, sticky="w")
        row += 1
        ttk.Radiobutton(sf, text="AVI - same format as the originals", value=".avi",
                        variable=self.v_fmt).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Radiobutton(sf, text="MKV - adds a chapter marker for each flight", value=".mkv",
                        variable=self.v_fmt).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ttk.Checkbutton(sf, text="Also save signal-loss report (snapshots + clips)",
                        variable=self.v_report).grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # preview
        pf = ttk.LabelFrame(mid, text="Preview", padding=4)
        pf.pack(side="left", fill="y")
        # a blank image makes the label size itself in pixels (not text lines)
        self._blank = tk.PhotoImage(width=320, height=240)
        self.prev = tk.Label(pf, image=self._blank, width=320, height=240, bg="#202020", fg="#bbbbbb",
                             text="Click the timeline or\na segment to preview", compound="center")
        self.prev.pack()
        self.prev_cap = ttk.Label(pf, text="", width=46, anchor="center")
        self.prev_cap.pack(fill="x")

        # analyse / export bar
        bar = ttk.Frame(r, padding=(10, 2))
        bar.pack(fill="x")
        self.btn_an = ttk.Button(bar, text="1.  Analyse", style="Big.TButton", command=self.analyse)
        self.btn_an.pack(side="left")
        self.btn_ex = ttk.Button(bar, text="2.  Export video...", style="Big.TButton",
                                 command=self.export, state="disabled")
        self.btn_ex.pack(side="left", padx=8)
        self.btn_cancel = ttk.Button(bar, text="Cancel", command=self.do_cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        self.summary = ttk.Label(bar, text="", font=("Segoe UI", 10, "bold"))
        self.summary.pack(side="left", padx=14)

        # timeline
        tf = ttk.Frame(r, padding=(10, 4))
        tf.pack(fill="x")
        self.cv = tk.Canvas(tf, height=104, bg=COL_BG, highlightthickness=1, highlightbackground="#bbbbbb")
        self.cv.pack(fill="x")
        self.cv.bind("<Configure>", lambda e: self.draw_timeline())
        self.cv.bind("<Button-1>", self.on_timeline_click)
        leg = ttk.Frame(tf)
        leg.pack(fill="x", pady=(2, 0))
        for col, txt in ((COL_GOOD, "Good signal"), (COL_WEAK, "Weak / breaking up (always kept)"),
                         (COL_SNOW, "No signal (static)"), (COL_KEEP, "Exported"), (COL_CUT, "Cut out")):
            c = tk.Canvas(leg, width=14, height=14, highlightthickness=0)
            c.create_rectangle(1, 1, 13, 13, fill=col, outline="#888888")
            c.pack(side="left", padx=(8, 3))
            ttk.Label(leg, text=txt).pack(side="left")
        ttk.Label(leg, text="     ⚠ = signal faded before it was lost").pack(side="left")

        # segments table
        tb = ttk.Frame(r, padding=(10, 4, 10, 4))
        tb.pack(fill="both", expand=True)
        cols = ("n", "start", "end", "len", "action", "what", "src")
        self.tv = ttk.Treeview(tb, columns=cols, show="headings", selectmode="browse")
        for c, txt, w, anc in (("n", "#", 40, "e"), ("start", "Start", 90, "e"), ("end", "End", 90, "e"),
                               ("len", "Length", 70, "e"), ("action", "Action", 70, "center"),
                               ("what", "What happened  (double-click a row to switch Keep / Cut)", 520, "w"),
                               ("src", "Starts in file", 170, "w")):
            self.tv.heading(c, text=txt)
            self.tv.column(c, width=w, anchor=anc, stretch=(c == "what"))
        self.tv.tag_configure("cut", foreground="#888888")
        self.tv.tag_configure("warn", background="#ffe9c7")
        self.tv.tag_configure("weak", background="#fff7e0")
        sb2 = ttk.Scrollbar(tb, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb2.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb2.pack(side="left", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self.on_select)
        self.tv.bind("<Double-1>", self.on_toggle)
        self.tv.bind("<space>", self.on_toggle)

        # status
        st = ttk.Frame(r, padding=(10, 0, 10, 8))
        st.pack(fill="x")
        self.pb = ttk.Progressbar(st, mode="determinate", maximum=1000)
        self.pb.pack(fill="x")
        self.status = ttk.Label(st, text="Add your DVR clips, then press Analyse.")
        self.status.pack(fill="x")

    # --------------------------------------------------------------- files
    def _set_files(self, files):
        seen, out = set(), []
        for f in sorted(files, key=dc.natural_key):
            k = os.path.normcase(os.path.abspath(f))
            if k not in seen:
                seen.add(k)
                out.append(os.path.abspath(f))
        self.files = out
        self.lb.delete(0, "end")
        for f in self.files:
            self.lb.insert("end", os.path.basename(f) + "    (" + os.path.dirname(f) + ")")
        n = len(self.files)
        self.files_lbl.configure(text=f"{n} file{'s' if n != 1 else ''}" if n else "No files added")
        self._invalidate()

    def _invalidate(self):
        self.session = None
        self.sel_seg = None
        self.btn_ex.configure(state="disabled")
        self.tv.delete(*self.tv.get_children())
        self.summary.configure(text="")
        self.draw_timeline()

    def add_files(self):
        if self.busy:
            return
        fs = filedialog.askopenfilenames(
            title="Select DVR clips",
            filetypes=[("Video files", " ".join("*" + e for e in VIDEO_EXT) + " " +
                        " ".join("*" + e.upper() for e in VIDEO_EXT)), ("All files", "*.*")])
        if fs:
            self._set_files(self.files + list(fs))

    def add_folder(self):
        if self.busy:
            return
        d = filedialog.askdirectory(title="Select the folder with the DVR clips")
        if d:
            fs = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(VIDEO_EXT)]
            if not fs:
                messagebox.showinfo(dc.APP_NAME, "No video files found in that folder.")
            self._set_files(self.files + fs)

    def remove_files(self):
        if self.busy:
            return
        sel = set(self.lb.curselection())
        self._set_files([f for i, f in enumerate(self.files) if i not in sel])

    def clear_files(self):
        if not self.busy:
            self._set_files([])

    # ------------------------------------------------------------ settings
    def _settings(self) -> dc.Settings:
        def num(v, d, lo, hi):
            try:
                x = float(v.get())
            except ValueError:
                x = d
            x = min(hi, max(lo, x))
            return x
        return dc.Settings(min_cut=num(self.v_min, 5.0, 0.5, 600),
                           pad=num(self.v_pad, 0.5, 0.0, 10),
                           faint_protect=bool(self.v_faint.get()),
                           snow_cover=num(self.v_cover, 85, 30, 100) / 100.0)

    def reapply(self):
        if self.session is None or self.busy:
            return
        st = self._settings()
        old = self.session.settings
        if (st.min_cut, st.pad, st.faint_protect, st.snow_cover) == \
                (old.min_cut, old.pad, old.faint_protect, old.snow_cover):
            return
        dc.classify(self.session, st)
        self.fill_table()
        self.draw_timeline()
        self.status.configure(text="Settings applied (any manual Keep/Cut changes were reset).")

    # ------------------------------------------------------------- workers
    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_an.configure(state="disabled" if busy else "normal")
        self.btn_ex.configure(state="disabled" if busy or self.session is None else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.root.configure(cursor="watch" if busy else "")

    def do_cancel(self):
        self.cancel.set()
        self.status.configure(text="Cancelling...")

    def _check_ffmpeg(self):
        try:
            dc.tool("ffmpeg")
            dc.tool("ffprobe")
        except FileNotFoundError as e:
            messagebox.showerror(dc.APP_NAME, str(e) + "\n\nSee README.txt for how to get FFmpeg.")

    def analyse(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo(dc.APP_NAME, "Add some DVR clips first.")
            return
        self._invalidate()
        self.cancel.clear()
        self._set_busy(True)
        files, st = list(self.files), self._settings()

        def work():
            try:
                s = dc.analyze_files(files, lambda f, m: self.q.put(("progress", f, m)), self.cancel, st)
                self.q.put(("analysed", s))
            except dc.Cancelled:
                self.q.put(("cancelled",))
            except Exception as e:
                self.q.put(("error", f"{e}", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def export(self):
        s = self.session
        if self.busy or s is None:
            return
        if not s.keep_mask().any():
            messagebox.showinfo(dc.APP_NAME, "Everything is marked as Cut - nothing to export.")
            return
        ext = self.v_fmt.get()
        first = os.path.splitext(os.path.basename(s.clips[0].path))[0]
        last = os.path.splitext(os.path.basename(s.clips[-1].path))[0]
        name = f"{first}-{last}_stitched{ext}" if first != last else f"{first}_stitched{ext}"
        out = filedialog.asksaveasfilename(
            title="Save stitched video as", initialdir=os.path.dirname(s.clips[0].path),
            initialfile=name, defaultextension=ext,
            filetypes=[("AVI video", "*.avi"), ("Matroska video", "*.mkv")] if ext == ".avi"
            else [("Matroska video", "*.mkv"), ("AVI video", "*.avi")])
        if not out:
            return
        if os.path.splitext(out)[1].lower() not in (".avi", ".mkv"):
            out += ext
        srcs = {os.path.normcase(os.path.abspath(c.path)) for c in s.clips}
        if os.path.normcase(os.path.abspath(out)) in srcs:
            messagebox.showerror(dc.APP_NAME, "Choose a different file name - that is one of the original clips.")
            return
        self.cancel.clear()
        self._set_busy(True)
        want_report = bool(self.v_report.get())

        def work():
            try:
                info = dc.export_video(s, out, lambda f, m: self.q.put(("progress", f, m)), self.cancel)
                rep = None
                if want_report:
                    rep = dc.write_report(s, out, lambda f, m: self.q.put(("progress", f, m)), self.cancel)
                self.q.put(("exported", out, rep, info))
            except dc.Cancelled:
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except OSError:
                    pass
                self.q.put(("cancelled",))
            except Exception as e:
                self.q.put(("error", f"{e}", traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.pb.configure(value=int(msg[1] * 1000))
                    self.status.configure(text=msg[2])
                elif kind == "analysed":
                    self.session = msg[1]
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.fill_table()
                    self.draw_timeline()
                    s = self.session
                    n_warn = sum(1 for g in s.segments if g.warn)
                    txt = "Analysis done. Check the timeline, double-click rows to change Keep/Cut, then Export."
                    if n_warn:
                        txt += f"   ⚠ {n_warn} suspicious signal loss{'es' if n_warn > 1 else ''} found."
                    self.status.configure(text=txt)
                    warn_rows = [i for i, g in enumerate(s.segments) if g.warn]
                    if warn_rows:
                        self.tv.selection_set(str(warn_rows[0]))
                        self.tv.see(str(warn_rows[0]))
                elif kind == "exported":
                    _, out, rep, info = msg
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Saved {out}")
                    text = f"Saved:\n{out}\n\nLength {dc.fmt_time(info['duration'])}, no re-encoding (original quality)."
                    if rep:
                        text += f"\n\nSignal-loss report:\n{rep}"
                    text += "\n\nOpen the folder now?"
                    if messagebox.askyesno(dc.APP_NAME, text):
                        self._open_path(os.path.dirname(out))
                        if rep:
                            webbrowser.open("file:///" + rep.replace("\\", "/"))
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Cancelled.")
                elif kind == "error":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Error: " + msg[1].splitlines()[0][:150])
                    messagebox.showerror(dc.APP_NAME, msg[1][:3000])
                elif kind == "thumb":
                    _, req, png, cap = msg
                    if req == self.thumb_req:
                        self._show_thumb(png, cap)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    @staticmethod
    def _open_path(p):
        try:
            if os.name == "nt":
                os.startfile(p)  # noqa
            else:
                webbrowser.open("file://" + p)
        except Exception:
            pass

    # ------------------------------------------------------------- results
    def fill_table(self):
        s = self.session
        self.tv.delete(*self.tv.get_children())
        if s is None:
            return
        for i, sg in enumerate(s.segments):
            c, ts = s.locate(sg.t0)
            tags = []
            if sg.action == "cut":
                tags.append("cut")
            if sg.warn:
                tags.append("warn")
            elif sg.kind == "signal" and sg.weak_pct >= 10:
                tags.append("weak")
            what = ("⚠ " if sg.warn else "") + sg.label
            self.tv.insert("", "end", iid=str(i), tags=tags, values=(
                i + 1, dc.fmt_time(sg.t0, True), dc.fmt_time(sg.t1, True), f"{sg.duration:.1f}s",
                "KEEP" if sg.action == "keep" else "CUT", what,
                f"{os.path.basename(c.path)} @ {dc.fmt_time(ts)}"))
        self._update_summary()

    def _update_summary(self):
        s = self.session
        if s is None:
            self.summary.configure(text="")
            return
        flights = len({sg.flight for sg in s.segments if sg.kind == "signal" and sg.action == "keep"})
        cuts = sum(1 for sg in s.segments if sg.action == "cut")
        self.summary.configure(text=f"Recorded {dc.fmt_time(s.duration)}   →   output {dc.fmt_time(s.kept_duration())}"
                                    f"     ({flights} flight{'s' if flights != 1 else ''}, {cuts} cut{'s' if cuts != 1 else ''})")

    def on_toggle(self, event=None):
        s = self.session
        if s is None or self.busy:
            return
        sel = self.tv.selection()
        if not sel:
            return
        i = int(sel[0])
        sg = s.segments[i]
        sg.action = "cut" if sg.action == "keep" else "keep"
        dc.renumber_flights(s)
        self.fill_table()
        self.tv.selection_set(str(i))
        self.tv.see(str(i))
        self.draw_timeline()
        return "break"

    def on_select(self, event=None):
        s = self.session
        sel = self.tv.selection()
        if s is None or not sel:
            return
        i = int(sel[0])
        self.sel_seg = i
        self.draw_timeline()
        sg = s.segments[i]
        if sg.kind == "nosignal" and i > 0:
            lc = dc.last_clear_frame(s, i)
            if lc is not None:
                t = float(s.t[lc])
                self.request_thumb(t, f"Last clear frame before loss  -  {dc.fmt_time(t, True)}")
                return
        t = min(sg.t0 + min(1.0, sg.duration / 2), sg.t1 - 0.05)
        self.request_thumb(t, f"{dc.fmt_time(t, True)}")

    def request_thumb(self, t: float, caption: str):
        s = self.session
        if s is None:
            return
        self.thumb_req += 1
        req = self.thumb_req
        c, ts = s.locate(t)
        cap = f"{caption}\n{os.path.basename(c.path)} @ {dc.fmt_time(ts, True)}"

        def work():
            try:
                png = dc.grab_frame_png(s, t + 0.001, 320)
            except Exception:
                png = b""
            self.q.put(("thumb", req, png, cap))
        threading.Thread(target=work, daemon=True).start()

    def _show_thumb(self, png: bytes, cap: str):
        if png:
            try:
                self.thumb = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")
                self.prev.configure(image=self.thumb, text="", width=320, height=240)
            except tk.TclError:
                self.prev.configure(image=self._blank, text="(preview unavailable)")
        else:
            self.prev.configure(image=self._blank, text="(preview unavailable)")
        self.prev_cap.configure(text=cap)

    # ------------------------------------------------------------- timeline
    def _x_to_t(self, x):
        s = self.session
        w = max(1, self.cv.winfo_width() - 20)
        return min(max(0.0, (x - 10) / w * s.duration), s.duration - 1e-3)

    def on_timeline_click(self, ev):
        s = self.session
        if s is None or s.n == 0:
            return
        t = self._x_to_t(ev.x)
        for i, sg in enumerate(s.segments):
            if sg.t0 <= t < sg.t1:
                self.sel_seg = i
                self.tv.selection_set(str(i))
                self.tv.see(str(i))
                break
        g = int(np.clip(np.searchsorted(s.t, t, side="right") - 1, 0, s.n - 1))
        lab = ("good signal", "weak signal", "no signal")[int(s.labels[g])]
        self.root.after(30, lambda: self.request_thumb(t, f"{dc.fmt_time(t, True)}  -  {lab}"))

    def draw_timeline(self):
        cv = self.cv
        cv.delete("all")
        W = max(50, cv.winfo_width())
        x0, x1 = 10, W - 10
        y_sig, y_exp, h = 16, 60, 22
        s = self.session
        cv.create_text(x0, 8, text="Signal", anchor="w", fill="#555", font=("Segoe UI", 8))
        cv.create_text(x0, y_exp - 9, text="Exported video", anchor="w", fill="#555", font=("Segoe UI", 8))
        if s is None or s.n == 0:
            cv.create_rectangle(x0, y_sig, x1, y_sig + h, outline="#cccccc")
            cv.create_rectangle(x0, y_exp, x1, y_exp + h, outline="#cccccc")
            cv.create_text((x0 + x1) / 2, y_sig + h / 2, text="Run Analyse to see the session timeline",
                           fill="#999999")
            return
        dur = s.duration
        npx = x1 - x0
        ts = np.linspace(0, dur, npx, endpoint=False)
        gi = np.clip(np.searchsorted(s.t, ts, side="right") - 1, 0, s.n - 1)
        # take the "worst" label in each pixel so short dropouts stay visible
        labs = s.labels[gi].astype(np.int8)
        if s.n > npx:
            edges = np.searchsorted(s.t, np.linspace(0, dur, npx + 1))
            mx = np.maximum.reduceat(s.labels, np.clip(edges[:-1], 0, s.n - 1))
            labs = mx.astype(np.int8)
        keep = s.keep_mask()[gi]
        self._draw_runs(labs, x0, y_sig, h, {0: COL_GOOD, 1: COL_WEAK, 2: COL_SNOW})
        self._draw_runs(keep.astype(np.int8), x0, y_exp, h, {1: COL_KEEP, 0: COL_CUT})
        # file boundaries
        for c in s.clips[1:]:
            x = x0 + c.offset / dur * npx
            cv.create_line(x, y_sig - 3, x, y_exp + h + 3, fill="#444444", dash=(2, 2))
        for c in s.clips:
            x = x0 + c.offset / dur * npx
            if c.length / dur * npx > 70:
                cv.create_text(x + 3, y_exp + h + 10, text=os.path.splitext(os.path.basename(c.path))[0],
                               anchor="w", fill="#666666", font=("Segoe UI", 7))
        # warnings
        for sg in s.segments:
            if sg.warn:
                x = x0 + sg.t0 / dur * npx
                cv.create_text(x, y_sig + h / 2, text="⚠", fill="#000000", font=("Segoe UI", 11, "bold"))
        # selection
        if self.sel_seg is not None and self.sel_seg < len(s.segments):
            sg = s.segments[self.sel_seg]
            xa = x0 + sg.t0 / dur * npx
            xb = max(xa + 2, x0 + sg.t1 / dur * npx)
            cv.create_rectangle(xa, y_sig - 2, xb, y_exp + h + 2, outline="#000000", width=2)
        cv.create_text(x1, y_exp + h + 10, text=dc.fmt_time(dur), anchor="e", fill="#666666",
                       font=("Segoe UI", 7))

    def _draw_runs(self, vals, x0, y, h, colors):
        start = 0
        n = len(vals)
        for i in range(1, n + 1):
            if i == n or vals[i] != vals[start]:
                self.cv.create_rectangle(x0 + start, y, x0 + i, y + h, fill=colors[int(vals[start])], outline="")
                start = i


def selftest(report_path: str, clips: list[str]) -> int:
    """Used by the automated build: checks that the packaged .exe starts its window,
    finds FFmpeg, and can analyse + export the given clips. Writes the result to a file
    (a windowed .exe has no console)."""
    lines = []
    ok = True
    try:
        root = tk.Tk()
        app = App(root)
        root.update()
        lines.append(f"gui: ok ({dc.APP_NAME} {dc.APP_VERSION})")
        lines.append(f"ffmpeg: {dc.tool('ffmpeg')}")
        lines.append(f"ffprobe: {dc.tool('ffprobe')}")
        if clips:
            app._set_files(clips)
            s = dc.analyze_files(app.files, settings=app._settings())
            lines.append(dc.summary_table(s))
            out = os.path.join(os.path.dirname(os.path.abspath(report_path)), "selftest_export.mkv")
            info = dc.export_video(s, out)
            lines.append(f"export: {info['duration']:.2f}s in {info['pieces']} pieces -> {out}")
            ok = os.path.getsize(out) > 0 and sum(1 for g in s.segments if g.action == "cut") > 0
        root.destroy()
    except Exception:
        ok = False
        lines.append(traceback.format_exc())
    lines.append("SELFTEST " + ("OK" if ok else "FAILED"))
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3:]))
    _enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    app = App(root)
    dropped = [a for a in sys.argv[1:] if os.path.isfile(a)]   # clips dropped onto the .exe icon
    if dropped:
        app._set_files(dropped)
    root.mainloop()


if __name__ == "__main__":
    main()
