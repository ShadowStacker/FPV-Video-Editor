"""
FPV DVR Stitcher - Windows desktop app (Tkinter).

Select the DVR clips from a flying session, let it find the dead static
between batteries, review the result on a timeline, and export one video.
"""
from __future__ import annotations

import io
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageTk

import chapters_ui
import dvr_core as dc
import enhance as en
import enhance_ui
import player as pl

VIDEO_EXT = (".avi", ".mp4", ".mov", ".mkv", ".ts", ".m4v", ".3gp", ".mpg", ".mpeg", ".wmv")

COL_GOOD = "#2e9e44"
COL_WEAK = "#f0a020"
COL_SNOW = "#c8352b"
COL_KEEP = "#2f6fd6"
COL_CUT = "#d9d9d9"
COL_STATS = "#8e6cc9"
COL_BG = "#ffffff"
COL_FLAG = "#1e6b2f"
COL_FLAG_MINE = "#6f42c1"
COL_EDGE = "#005fb8"
PREVIEW_W, PREVIEW_H = 320, 240
X0 = 10                    # timeline margin (px)
Y_SIG, Y_EXP, LANE_H = 16, 60, 22
EDGE_GRAB = 6              # px either side of a cut that grabs it
SPEED_LABELS = {"½×": 0.5, "1×": 1.0, "2×": 2.0, "4×": 4.0, "8×": 8.0}
MODES = ("As exported", "Original")
EDIT_KEYS = {"i": "start", "o": "end", "s": "split", "x": "toggle", "p": "around", "c": "chapter"}


def parse_time(text: str) -> float | None:
    """'14:35.2', '1:02:03.5' or '95.5' -> seconds (None if it isn't a time)."""
    try:
        parts = [float(p) for p in text.strip().replace(",", ".").split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


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
        self.sel_seg: int | None = None
        self.thumb_req = 0
        self.preview_t: float | None = None          # moment shown in the preview (used by Enhance)
        self.play_t = 0.0                            # playhead (session time)
        self.view: tuple[float, float] | None = None # zoomed timeline range (None = whole recording)
        self.active_edge: str | None = None          # "start" / "end" of the selected part, nudged by arrows
        self.drag = None
        self.history: list = []                      # undo snapshots
        self.player: pl.Player | None = None
        self.stop_out: float | None = None           # "play around this cut" stops here (export time)
        self.pop = None                              # larger preview window
        self.chapters_win = None
        self._last_img: Image.Image | None = None
        self._img_main = None
        self._img_pop = None
        self._drag_preview_job = None
        self._chapters_cache = None
        self.time_edit = None
        self.enh_settings = en.load_settings()       # remembered between runs
        self.v_fps60 = tk.BooleanVar(value=self.enh_settings.fps60)   # "Smooth 60 fps" (menu + Enhance window)
        self.v_fps60.trace_add("write", self._fps60_changed)
        self.enh_dialog = None
        self.enh_busy = False

        style = ttk.Style()
        try:
            style.theme_use("vista" if os.name == "nt" else "clam")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 6))
        style.configure("Tool.TButton", padding=(2, 1))
        try:
            from tkinter import font as tkfont
            line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        except Exception:
            line = 16
        style.configure("Treeview", rowheight=max(22, line + 8))   # scales with Windows display zoom

        self._build()
        root.bind_all("<KeyPress>", self._on_key, add="+")
        root.bind_all("<Control-z>", lambda e: self._key_undo(e), add="+")
        root.bind_all("<Control-Z>", lambda e: self._key_undo(e), add="+")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(100, self._poll)
        self.root.after(300, self._check_ffmpeg)

    def quit(self):
        self.pause()
        shutil.rmtree(pl.player_dir(), ignore_errors=True)
        self.root.destroy()

    # ------------------------------------------------------------------ UI
    def _build_menu(self):
        mb = tk.Menu(self.root)
        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label="Add files...", command=self.add_files)
        fm.add_command(label="Add folder...", command=self.add_folder)
        fm.add_separator()
        fm.add_command(label="Exit", command=self.quit)
        mb.add_cascade(label="File", menu=fm)
        self.menu_edit = tk.Menu(mb, tearoff=0, postcommand=lambda: self._menu_items(self.menu_edit))
        self._fill_edit_menu(self.menu_edit)
        mb.add_cascade(label="Edit", menu=self.menu_edit)
        em = tk.Menu(mb, tearoff=0)
        em.add_command(label="Enhance video...", command=self.open_enhance)
        em.add_checkbutton(label="Smooth 60 fps (AI frame interpolation)", variable=self.v_fps60,
                           onvalue=True, offvalue=False)
        em.add_separator()
        em.add_command(label="Download AI add-on...", command=self.download_ai)
        mb.add_cascade(label="Enhance", menu=em)
        hm = tk.Menu(mb, tearoff=0)
        hm.add_command(label="Save diagnostics...", command=self.save_diag)
        hm.add_command(label="Keyboard shortcuts", command=self.show_keys)
        hm.add_command(label="About", command=lambda: messagebox.showinfo(
            dc.APP_NAME, f"{dc.APP_NAME} {dc.APP_VERSION}\n\nhttps://github.com/ShadowStacker/FPV-Video-Editor"))
        mb.add_cascade(label="Help", menu=hm)
        self.menu_enh, self.menu_help = em, hm
        self.ctx_menu = tk.Menu(self.root, tearoff=0, postcommand=lambda: self._menu_items(self.ctx_menu))
        self._fill_edit_menu(self.ctx_menu)
        self.root.config(menu=mb)

    def _fill_edit_menu(self, m: tk.Menu):
        m.add_command(label="Set start here", accelerator="I", command=lambda: self.act("start"))
        m.add_command(label="Set end here", accelerator="O", command=lambda: self.act("end"))
        m.add_command(label="Split here", accelerator="S", command=lambda: self.act("split"))
        m.add_command(label="Cut this part", accelerator="X", command=lambda: self.act("toggle"))
        m.add_command(label="Play around this cut", accelerator="P", command=lambda: self.act("around"))
        m.add_separator()
        m.add_command(label="Add chapter here", accelerator="C", command=lambda: self.act("chapter"))
        m.add_command(label="Chapters...", command=lambda: self.act("chapters"))
        m.add_separator()
        m.add_command(label="Zoom to this part", command=lambda: self.act("zoomsel"))
        m.add_command(label="Show the whole recording", command=lambda: self.act("zoomall"))
        m.add_separator()
        m.add_command(label="Undo", accelerator="Ctrl+Z", command=lambda: self.act("undo"))
        m.add_command(label="Reset to detected cuts", command=lambda: self.act("reset"))

    def _menu_items(self, m: tk.Menu):
        s = self.session
        ok = s is not None and not self.busy
        edit = ok and not self.enh_busy
        sg = s.segments[self.sel_seg] if ok and self.sel_seg is not None and self.sel_seg < len(s.segments) else None
        try:
            for i in range(m.index("end") + 1):
                if m.type(i) != "command":
                    continue
                label = m.entrycget(i, "label")
                if label in ("Cut this part", "Keep this part"):
                    m.entryconfigure(i, label="Keep this part" if sg is not None and sg.action == "cut"
                                     else "Cut this part")
                want = ok
                if label in ("Set start here", "Set end here", "Split here", "Cut this part", "Keep this part",
                             "Add chapter here", "Reset to detected cuts"):
                    want = edit and sg is not None
                elif label == "Undo":
                    want = edit and bool(self.history)
                elif label == "Show the whole recording":
                    want = ok and self.view is not None
                m.entryconfigure(i, state="normal" if want else "disabled")
        except tk.TclError:
            pass

    def _menu_states(self):
        ready = "normal" if (self.session is not None and not self.busy and not self.enh_busy) else "disabled"
        try:
            self.menu_enh.entryconfigure(0, state=ready)
            self.menu_help.entryconfigure(0, state=ready)
        except (tk.TclError, AttributeError):
            pass

    def _build(self):
        r = self.root
        self._build_menu()
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
        defaults = dc.Settings()
        self.v_min = tk.StringVar(value=f"{defaults.min_cut:g}")
        self.v_pad = tk.StringVar(value=f"{defaults.pad:.1f}")
        self.v_cover = tk.StringVar(value=f"{defaults.snow_cover * 100:.0f}")
        self.v_fmt = tk.StringVar(value=".avi")
        self.v_report = tk.BooleanVar(value=False)
        self.v_faint = tk.BooleanVar(value=defaults.faint_protect)
        self.v_trim = tk.BooleanVar(value=defaults.trim_stats)
        row = 0
        ttk.Label(sf, text="Cut static longer than").grid(row=row, column=0, sticky="w")
        e = ttk.Spinbox(sf, from_=0.5, to=120, increment=0.5, width=6, textvariable=self.v_min,
                        command=self.reapply)
        e.grid(row=row, column=1, padx=4)
        e.bind("<Return>", lambda ev: self.reapply())
        e.bind("<FocusOut>", lambda ev: self.reapply())
        ttk.Label(sf, text="seconds").grid(row=row, column=2, sticky="w")
        row += 1
        ttk.Label(sf, text="Keep static at each cut").grid(row=row, column=0, sticky="w")
        e = ttk.Spinbox(sf, from_=0, to=5, increment=0.1, width=6, textvariable=self.v_pad,
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
        ttk.Checkbutton(sf, text="Trim post-flight STATS screen (landed -> unplugged)",
                        variable=self.v_trim, command=self.reapply).grid(row=row, column=0, columnspan=3,
                                                                          sticky="w")
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

        # preview (a small player)
        pf = ttk.LabelFrame(mid, text="Preview", padding=4)
        pf.pack(side="left", fill="y")
        # a blank image makes the label size itself in pixels (not text lines)
        self._blank = tk.PhotoImage(width=PREVIEW_W, height=PREVIEW_H)
        self.prev = tk.Label(pf, image=self._blank, width=PREVIEW_W, height=PREVIEW_H, bg="#202020", fg="#bbbbbb",
                             text="Click the timeline or\na segment to preview", compound="center")
        self.prev.pack()
        self.prev.bind("<Double-Button-1>", lambda e: self.open_pop())
        ctl = ttk.Frame(pf)
        ctl.pack(fill="x", pady=(3, 0))
        ttk.Button(ctl, text="|◀", width=3, style="Tool.TButton", command=lambda: self.step(-1)).pack(side="left")
        self.btn_play = ttk.Button(ctl, text="▶", width=3, style="Tool.TButton", command=self.toggle_play)
        self.btn_play.pack(side="left", padx=2)
        ttk.Button(ctl, text="▶|", width=3, style="Tool.TButton", command=lambda: self.step(1)).pack(side="left")
        self.v_speed = tk.StringVar(value="1×")
        cb = ttk.Combobox(ctl, values=list(SPEED_LABELS), textvariable=self.v_speed, state="readonly", width=4)
        cb.pack(side="left", padx=(6, 2))
        cb.bind("<<ComboboxSelected>>", lambda e: self._restart_play())
        self.v_mode = tk.StringVar(value=MODES[0])
        cb = ttk.Combobox(ctl, values=MODES, textvariable=self.v_mode, state="readonly", width=11)
        cb.pack(side="left", padx=2)
        cb.bind("<<ComboboxSelected>>", lambda e: (self._restart_play(), self._caption()))
        ttk.Button(ctl, text="⤢", width=3, style="Tool.TButton", command=self.open_pop).pack(side="right")
        self.prev_cap = ttk.Label(pf, text="", width=46, anchor="center", justify="center")
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
        self.btn_enh = ttk.Button(bar, text="3.  Enhance video...", style="Big.TButton",
                                  command=self.open_enhance, state="disabled")
        self.btn_enh.pack(side="left", padx=(0, 8), before=self.btn_cancel)
        self.summary = ttk.Label(bar, text="", font=("Segoe UI", 10, "bold"))
        self.summary.pack(side="left", padx=14)

        # timeline
        tf = ttk.Frame(r, padding=(10, 4))
        tf.pack(fill="x")
        self.cv = tk.Canvas(tf, height=104, bg=COL_BG, highlightthickness=1, highlightbackground="#bbbbbb",
                            takefocus=1)
        self.cv.pack(fill="x")
        self.cv.bind("<Configure>", lambda e: self.draw_timeline())
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.cv.bind("<Motion>", self.on_hover)
        self.cv.bind("<Double-Button-1>", self.on_timeline_double)
        self.cv.bind("<Button-3>", self.on_timeline_menu)
        self.cv.bind("<MouseWheel>", self.on_wheel)
        self.cv.bind("<Shift-MouseWheel>", lambda e: self.on_wheel(e, pan=True))
        self.cv.bind("<Button-4>", lambda e: self.on_wheel(e, delta=120))
        self.cv.bind("<Button-5>", lambda e: self.on_wheel(e, delta=-120))
        self.cv.bind("<Shift-Button-4>", lambda e: self.on_wheel(e, delta=120, pan=True))
        self.cv.bind("<Shift-Button-5>", lambda e: self.on_wheel(e, delta=-120, pan=True))
        leg = ttk.Frame(tf)
        leg.pack(fill="x", pady=(2, 0))
        for col, txt in ((COL_GOOD, "Good signal"), (COL_WEAK, "Weak / breaking up (always kept)"),
                         (COL_SNOW, "No signal (static)"), (COL_STATS, "STATS screen"), (COL_KEEP, "Exported"),
                         (COL_CUT, "Cut out")):
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
                               ("what", "What happened  (double-click a row to switch Keep / Cut, "
                                        "a Start or End time to type it)", 520, "w"),
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
        self.tv.bind("<Double-1>", self.on_table_double)
        self.tv.bind("<Button-3>", self.on_table_menu)
        self.tv.bind("<space>", lambda e: (self.toggle_play(), "break")[1])

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
        self.pause()
        self.session = None
        self.sel_seg = None
        self.history = []
        self.view = None
        self.active_edge = None
        self.play_t = 0.0
        self._chapters_cache = None
        self.btn_ex.configure(state="disabled")
        self.btn_enh.configure(state="disabled")
        self._menu_states()
        self.tv.delete(*self.tv.get_children())
        self.summary.configure(text="")
        if self.chapters_win is not None:
            self.chapters_win.close()
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
        d = dc.Settings()
        return dc.Settings(min_cut=num(self.v_min, d.min_cut, 0.5, 600),
                           pad=num(self.v_pad, d.pad, 0.0, 10),
                           faint_protect=bool(self.v_faint.get()),
                           snow_cover=num(self.v_cover, d.snow_cover * 100, 30, 100) / 100.0,
                           trim_stats=bool(self.v_trim.get()))

    def reapply(self):
        if self.session is None or self.busy or self.enh_busy:
            return
        st = self._settings()
        old = self.session.settings
        if (st.min_cut, st.pad, st.faint_protect, st.snow_cover, st.trim_stats) == \
                (old.min_cut, old.pad, old.faint_protect, old.snow_cover, old.trim_stats):
            return
        had_edits = self.session.edited
        self.push_undo()
        dc.classify(self.session, st)
        self.session.edited = False
        self.after_edit()
        self.status.configure(text="Settings applied." + ("  Your own cut changes were reset (Edit > Undo brings "
                                                         "them back)." if had_edits else ""))

    # ------------------------------------------------------------- workers
    def _set_busy(self, busy: bool):
        self.busy = busy
        self.btn_an.configure(state="disabled" if busy else "normal")
        self.btn_ex.configure(state="disabled" if busy or self.session is None else "normal")
        self.btn_enh.configure(state="disabled" if busy or self.session is None else "normal")
        self._menu_states()
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

    def _fps60_changed(self, *_):
        self.enh_settings.fps60 = bool(self.v_fps60.get())
        en.save_settings(self.enh_settings)

    def open_enhance(self):
        if self.session is None or self.busy:
            messagebox.showinfo(dc.APP_NAME, "Analyse your clips first.")
            return
        if self.enh_dialog is not None and self.enh_dialog.winfo_exists():
            self.enh_dialog.lift()
            return
        self.enh_dialog = enhance_ui.EnhanceDialog(self)

    def lock_for_enhance(self, busy: bool):
        """While the Enhance window is working, the session must not change underneath it."""
        self.enh_busy = busy
        st = "disabled" if busy else "normal"
        self.btn_an.configure(state=st)
        self.btn_ex.configure(state="disabled" if busy or self.session is None else "normal")
        self._menu_states()

    def download_ai(self):
        if en.ai_installed():
            messagebox.showinfo(dc.APP_NAME, "The AI add-on is already installed.")
            return
        if self.session is None:
            messagebox.showinfo(dc.APP_NAME, "Open the Enhance window (after Analyse) to download the AI add-on.")
            return
        self.open_enhance()
        if self.enh_dialog is not None:
            self.enh_dialog.download_ai()

    def save_diag(self):
        """Measurements + sample frames in one zip, for tuning the detector on this footage."""
        s = self.session
        if self.busy or s is None:
            return
        first = os.path.splitext(os.path.basename(s.clips[0].path))[0]
        last = os.path.splitext(os.path.basename(s.clips[-1].path))[0]
        name = f"{first}-{last}_diagnostics.zip" if first != last else f"{first}_diagnostics.zip"
        out = filedialog.asksaveasfilename(
            title="Save diagnostics as", initialdir=os.path.dirname(s.clips[0].path),
            initialfile=name, defaultextension=".zip", filetypes=[("Zip file", "*.zip")])
        if not out:
            return
        self.cancel.clear()
        self._set_busy(True)

        def work():
            try:
                info = dc.save_diagnostics(s, out, lambda f, m: self.q.put(("progress", f, m)), self.cancel)
                self.q.put(("diag_saved", info))
            except dc.Cancelled:
                for f in (out, out + ".part"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
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
        self.pause()
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
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.show_session(msg[1])
                elif kind == "diag_saved":
                    info = msg[1]
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Saved {info['path']}")
                    if messagebox.askyesno(
                            dc.APP_NAME,
                            f"Diagnostics saved ({info['size'] / 1e6:.1f} MB, {info['frames']} sample frames):\n"
                            f"{info['path']}\n\nUpload this zip file in your chat with Claude so the "
                            f"detector can be tuned to your footage.\n\nOpen the folder now?"):
                        self._open_path(os.path.dirname(info["path"]))
                elif kind == "exported":
                    _, out, rep, info = msg
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Saved {out}")
                    text = f"Saved:\n{out}\n\nLength {dc.fmt_time(info['duration'])}, no re-encoding (original quality)."
                    if info.get("chapters_txt"):
                        text += f"\n\nChapter list (to paste into a video description):\n{info['chapters_txt']}"
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
                    _, req, png = msg
                    if req == self.thumb_req and self.player is None:
                        self._show_png(png)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)

    def show_session(self, s: dc.Session):
        self.session = s
        self._chapters_cache = None
        self._set_busy(False)
        self.fill_table()
        self.draw_timeline()
        n_warn = sum(1 for g in s.segments if g.warn)
        txt = ("Analysis done. Space plays the preview. Drag the black box's edges on the timeline, double-click "
               "a Start or End time to type it, or right-click for more.")
        if n_warn:
            txt = f"⚠ {n_warn} suspicious signal loss{'es' if n_warn > 1 else ''} found.  " + txt
        self.status.configure(text=txt)
        warn_rows = [i for i, g in enumerate(s.segments) if g.warn]
        first = warn_rows[0] if warn_rows else next((i for i, g in enumerate(s.segments) if g.action == "keep"), 0)
        if s.segments:
            self.tv.selection_set(str(first))
            self.tv.see(str(first))

    @staticmethod
    def _open_path(p):
        try:
            if os.name == "nt":
                os.startfile(p)  # noqa
            else:
                webbrowser.open("file://" + p)
        except Exception:
            pass

    def show_keys(self):
        messagebox.showinfo("Keyboard shortcuts", (
            "Space\tplay / pause the preview\n"
            "← →\tone frame back / forward (Shift: 1 second)\n"
            "\twhile a cut is highlighted blue (after dragging it), move that cut instead\n"
            "Esc\tstop moving the cut\n"
            "I / O\tset the start / end of the selected part at the playhead\n"
            "S\tsplit the selected part at the playhead\n"
            "X\tcut / keep the selected part\n"
            "P\tplay 3 seconds either side of the nearest cut\n"
            "C\tadd a chapter at the playhead\n"
            "Ctrl+Z\tundo\n\n"
            "Timeline: drag the black box's edges to move a cut, scroll to zoom, Shift+scroll to move "
            "sideways, right-click for the Edit menu."))

    # ------------------------------------------------------------- table
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
        if self.sel_seg is not None and self.sel_seg < len(s.segments):
            self._select_row(self.sel_seg)
        self._update_summary()

    def _select_row(self, i: int):
        """Select a row without moving the playhead (on_select ignores the row that is already sel_seg)."""
        self.tv.selection_set(str(i))
        self.tv.see(str(i))

    def _update_summary(self):
        s = self.session
        if s is None:
            self.summary.configure(text="")
            return
        flights = len({sg.flight for sg in s.segments if sg.kind == "signal" and sg.action == "keep"})
        cuts = sum(1 for sg in s.segments if sg.action == "cut")
        self.summary.configure(text=f"Recorded {dc.fmt_time(s.duration)}   →   output {dc.fmt_time(s.kept_duration())}"
                                    f"     ({flights} flight{'s' if flights != 1 else ''}, {cuts} cut{'s' if cuts != 1 else ''})")

    def on_select(self, event=None):
        s = self.session
        sel = self.tv.selection()
        if s is None or not sel:
            return
        i = int(sel[0])
        if i == self.sel_seg:
            return
        self.sel_seg = i
        self.active_edge = None
        self.pause()
        self.draw_timeline()
        sg = s.segments[i]
        if sg.kind == "nosignal" and i > 0:
            lc = dc.last_clear_frame(s, i)
            if lc is not None:
                self.show_at(float(s.t[lc]), "Last clear frame before loss")
                return
        self.show_at(min(sg.t0 + min(1.0, sg.duration / 2), sg.t1 - 0.05))

    def on_table_double(self, ev):
        s = self.session
        row = self.tv.identify_row(ev.y)
        if s is None or not row:
            return "break"
        col = self.tv.identify_column(ev.x)
        i = int(row)
        if col in ("#2", "#3"):
            self.edit_time(i, "start" if col == "#2" else "end")
        else:
            self.sel_seg = i
            self.act("toggle")
        return "break"

    def on_table_menu(self, ev):
        row = self.tv.identify_row(ev.y)
        if self.session is None or not row:
            return
        if int(row) != self.sel_seg:
            self.tv.selection_set(row)
            self.on_select()
        self.ctx_menu.tk_popup(ev.x_root, ev.y_root)

    def edit_time(self, i: int, which: str):
        s = self.session
        if self._locked():
            return
        lim = dc.edge_limits(s, i, which)
        if lim is None:
            self.status.configure(text="The recording's own start and end can't move.")
            return
        self.pause()
        self.sel_seg = i
        self.active_edge = None
        self._select_row(i)
        bbox = self.tv.bbox(str(i), "#2" if which == "start" else "#3")
        if not bbox:
            return
        x, y, w, h = bbox
        sg = s.segments[i]
        var = tk.StringVar(value=dc.fmt_time(sg.t0 if which == "start" else sg.t1, True))
        e = tk.Entry(self.tv, textvariable=var, font=("Consolas", 10), justify="right", relief="solid", bd=1)
        e.place(x=x, y=y, width=max(w, 100), height=h)
        e.focus_set()
        e.select_range(0, "end")
        self.time_edit = e
        self.status.configure(text="Type a time like 14:35.200 and press Enter (Esc cancels).")

        def done(apply: bool):
            if self.time_edit is not e:
                return
            if apply:
                t = parse_time(var.get())
                if t is None or t < lim[0] - 1e-3 or t > lim[1] + 1e-3:
                    e.configure(bg="#fff1f0", fg="#b3261e")
                    self.status.configure(text=(f"{which.capitalize()} must be between {dc.fmt_time(lim[0], True)} "
                                                f"and {dc.fmt_time(lim[1], True)}." if t is not None else
                                                "Type a time like 14:35.200 or 1:02:03.5"))
                    return
                self.push_undo()
                got = dc.move_edge(s, i, which, t)
                self.status.configure(text=f"{which.capitalize()} of row {i + 1} set to {dc.fmt_time(got, True)}.")
                self.show_edge(i, which)
            self.time_edit = None
            e.destroy()
            self.after_edit()

        e.bind("<Return>", lambda ev: (done(True), "break")[1])
        e.bind("<KP_Enter>", lambda ev: (done(True), "break")[1])
        e.bind("<Escape>", lambda ev: (done(False), "break")[1])
        e.bind("<FocusOut>", lambda ev: done(False))

    # ------------------------------------------------------------- editing
    def _locked(self) -> bool:
        if self.session is None or self.busy:
            return True
        if self.enh_busy:
            self.status.configure(text="Editing is locked while the Enhance window is exporting.")
            return True
        return False

    def push_undo(self):
        if self.session is not None:
            self.history.append(dc.snapshot(self.session))
            del self.history[:-80]

    def after_edit(self, refresh_chapters: bool = True):
        s = self.session
        if s is None:
            return
        if self.sel_seg is not None:
            self.sel_seg = min(self.sel_seg, len(s.segments) - 1)
        self._chapters_cache = None
        self.fill_table()
        self.draw_timeline()
        self._caption()
        if refresh_chapters and self.chapters_win is not None:
            self.chapters_win.refresh()
        if self.enh_dialog is not None:
            try:
                if self.enh_dialog.winfo_exists():
                    self.enh_dialog._update_estimate()
            except tk.TclError:
                pass

    def act(self, a: str):
        s = self.session
        if s is None or self.busy:
            return
        if a in ("chapters", "zoomsel", "zoomall", "around"):
            pass
        elif self._locked():
            return
        i = self.sel_seg if self.sel_seg is not None else s.segment_at(self.play_t)
        i = min(max(0, i), len(s.segments) - 1)
        self.sel_seg = i
        sg = s.segments[i]
        if a in ("start", "end"):
            lim = dc.edge_limits(s, i, a)
            if lim is None:
                self.status.configure(text="The recording's own start and end can't move.")
                return
            if not lim[0] - 1e-3 <= self.play_t <= lim[1] + 1e-3:
                self.status.configure(text=f"The playhead ({dc.fmt_time(self.play_t, True)}) is outside the range "
                                           f"this {a} can move to ({dc.fmt_time(lim[0], True)} - "
                                           f"{dc.fmt_time(lim[1], True)}). Click inside or next to the part first.")
                return
            self.pause()
            self.push_undo()
            got = dc.move_edge(s, i, a, self.play_t)
            self.active_edge = a
            self.status.configure(text=f"{a.capitalize()} of row {i + 1} at {dc.fmt_time(got, True)}. Arrow keys "
                                       f"nudge it one frame (Shift: 1 s); Esc to stop.")
            self.after_edit()
        elif a == "split":
            self.pause()
            self.push_undo()
            if dc.split_segment(s, i, self.play_t):
                self.sel_seg = i + 1
                self.active_edge = None
                self.status.configure(text=f"Split at {dc.fmt_time(s.segments[i + 1].t0, True)}. "
                                           f"Double-click either part to cut it.")
                self.after_edit()
            else:
                self.history.pop()
                self.status.configure(text="Put the playhead inside the selected part (not at its edge) to split it.")
        elif a == "toggle":
            self.push_undo()
            dc.toggle_segment(s, i)
            self.status.configure(text=f"Row {i + 1} is now {'kept' if sg.action == 'keep' else 'cut'}. "
                                       f"Output {dc.fmt_time(s.kept_duration())}.")
            self.after_edit()
        elif a == "around":
            self.play_around_cut()
        elif a == "chapter":
            self.push_undo()
            ch = dc.add_chapter(s, self.play_t)
            self.status.configure(text=f"Chapter added at {dc.fmt_time(ch.t, True)}. Rename it in Edit > Chapters...")
            self.after_edit()
        elif a == "chapters":
            self.open_chapters()
        elif a == "zoomsel":
            pad = max(1.0, sg.duration * 0.08)
            self.set_view(sg.t0 - pad, sg.t1 + pad)
        elif a == "zoomall":
            self.view = None
            self.draw_timeline()
        elif a == "undo":
            if not self.history:
                self.status.configure(text="Nothing to undo.")
                return
            self.pause()
            dc.restore(s, self.history.pop())
            self.active_edge = None
            self.status.configure(text="Undone.")
            self.after_edit()
        elif a == "reset":
            self.pause()
            self.push_undo()
            dc.reset_edits(s)
            self.active_edge = None
            self.status.configure(text="Back to the detected cuts (Undo brings your edits back).")
            self.after_edit()

    def open_chapters(self):
        if self.session is None:
            return
        if self.chapters_win is not None and self.chapters_win.winfo_exists():
            self.chapters_win.refresh()
            self.chapters_win.lift()
            return
        self.chapters_win = chapters_ui.ChaptersWindow(self)

    # ------------------------------------------------------------- keys
    def _typing(self, w) -> bool:
        try:
            cls = w.winfo_class()
        except (tk.TclError, AttributeError):
            return True
        return cls in ("Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "TCombobox", "Listbox")

    def _our_window(self, w) -> bool:
        try:
            top = w.winfo_toplevel()
        except (tk.TclError, AttributeError):
            return False
        return top is self.root or (self.pop is not None and top is self.pop)

    def _key_undo(self, ev):
        if self._typing(ev.widget) or not self._our_window(ev.widget):
            return None
        self.act("undo")
        return "break"

    def _on_key(self, ev):
        if self.session is None or not self._our_window(ev.widget):
            return None
        if ev.keysym == "Escape":
            if self.active_edge:
                self.active_edge = None
                self.draw_timeline()
            return None
        if self._typing(ev.widget) or ev.state & 0x4:        # Ctrl held: leave it to other shortcuts
            return None
        k = ev.keysym
        if k == "space":
            try:
                if ev.widget.winfo_class() in ("TButton", "Button", "TCheckbutton", "TRadiobutton"):
                    return None
            except (tk.TclError, AttributeError):
                return None
            self.toggle_play()
            return "break"
        if k in ("Left", "Right"):
            if ev.widget is self.tv:
                return None if not self.active_edge else self._nudge(k, ev)
            return self._nudge(k, ev)
        a = EDIT_KEYS.get(k.lower()) if len(k) == 1 else None
        if a:
            self.act(a)
            return "break"
        return None

    def _nudge(self, k, ev):
        s = self.session
        d = -1 if k == "Left" else 1
        big = bool(ev.state & 0x1)
        if self.active_edge and self.sel_seg is not None and not self._locked():
            self.pause()
            sg = s.segments[self.sel_seg]
            cur = sg.t0 if self.active_edge == "start" else sg.t1
            if big:
                target = cur + d
            else:
                g = s.nearest_frame_start(cur)
                g = int(np.clip(g + d, 0, s.n - 1))
                target = float(s.t[g])
            self.push_undo()
            got = dc.move_edge(s, self.sel_seg, self.active_edge, target)
            if got is not None:
                self.status.configure(text=f"{self.active_edge.capitalize()} of row {self.sel_seg + 1} at "
                                           f"{dc.fmt_time(got, True)}. Arrow keys nudge it one frame "
                                           f"(Shift: 1 s); Esc to stop.")
                self.after_edit()
                self.show_edge(self.sel_seg, self.active_edge)
        else:
            self.step(d * (int(round(s.frames_for(1.0))) if big else 1))
        return "break"

    # ------------------------------------------------------------- preview
    def _caption(self, note: str | None = None, out: tuple[float, float] | None = None):
        s = self.session
        if s is None:
            self.prev_cap.configure(text="")
            return
        t = self.play_t
        sg = s.segments[s.segment_at(t)]
        name = (note or sg.label.split("  ")[0]) + (" (cut)" if sg.action == "cut" and not note else "")
        if out is not None:                     # while playing "as exported": times from the player
            where = f"{dc.fmt_time(out[0])} / {dc.fmt_time(out[1])} in the export"
        elif self.v_mode.get() == MODES[0]:
            keep = s.keep_mask()
            where = (f"{dc.fmt_time(s.session_to_out(t, keep))} / {dc.fmt_time(s.kept_duration())} in the export"
                     if keep[s.frame_at(t)] else "not in the export")
        else:
            where = f"{dc.fmt_time(t)} / {dc.fmt_time(s.duration)} of the recording"
        c, ts = s.locate(t)
        self.prev_cap.configure(text=f"{dc.fmt_time(t, True)}  -  {name}\n{where}   ({os.path.basename(c.path)} "
                                     f"@ {dc.fmt_time(ts)})")
        if self.pop is not None:
            self.pop_cap.configure(text=f"{dc.fmt_time(t, True)}  -  {name}  -  {where}")

    def show_at(self, t: float, note: str | None = None):
        """Move the playhead to session time t and show that frame."""
        s = self.session
        if s is None:
            return
        self.play_t = float(min(max(0.0, t), s.duration - 1e-3))
        self.preview_t = self.play_t
        self._caption(note)
        self._draw_playhead()
        self.thumb_req += 1
        req = self.thumb_req
        width = self._pop_size()[0] if self.pop is not None else PREVIEW_W
        tt = self.play_t

        def work():
            try:
                png = dc.grab_frame_png(s, tt + 0.001, width)
            except Exception:
                png = b""
            self.q.put(("thumb", req, png))
        threading.Thread(target=work, daemon=True).start()

    def request_thumb(self, t: float, caption: str = ""):       # kept for older callers
        self.show_at(t)

    def show_edge(self, i: int, which: str):
        """Preview the first frame of a part (start) or the last frame before its end."""
        s = self.session
        sg = s.segments[i]
        t = sg.t0 if which == "start" else float(s.t[max(sg.f0, sg.f1 - 1)])
        if self._drag_preview_job is not None:
            self.root.after_cancel(self._drag_preview_job)
        self._drag_preview_job = self.root.after(60, lambda: (setattr(self, "_drag_preview_job", None),
                                                               self.show_at(t)))

    def _show_png(self, png: bytes):
        if not png:
            self.prev.configure(image=self._blank, text="(preview unavailable)")
            return
        try:
            self._show_img(Image.open(io.BytesIO(png)).convert("RGB"))
        except Exception:
            self.prev.configure(image=self._blank, text="(preview unavailable)")

    def _show_img(self, img: Image.Image):
        self._last_img = img
        w, h = pl.fit(img.width, img.height, PREVIEW_W, PREVIEW_H)
        small = img if (img.width, img.height) == (w, h) else img.resize((w, h), Image.BILINEAR)
        if self._img_main is not None and (self._img_main.width(), self._img_main.height()) == (w, h):
            self._img_main.paste(small)
        else:
            self._img_main = ImageTk.PhotoImage(small)
            self.prev.configure(image=self._img_main, text="", width=PREVIEW_W, height=PREVIEW_H)
        if self.pop is not None:
            pw, ph = self._pop_size()
            w2, h2 = pl.fit(img.width, img.height, pw, ph)
            big = img if (img.width, img.height) == (w2, h2) else img.resize((w2, h2), Image.BILINEAR)
            if self._img_pop is not None and (self._img_pop.width(), self._img_pop.height()) == (w2, h2):
                self._img_pop.paste(big)
            else:
                self._img_pop = ImageTk.PhotoImage(big)
                self.pop_img.configure(image=self._img_pop)

    def step(self, frames: int):
        s = self.session
        if s is None:
            return
        self.pause()
        g = s.frame_at(self.play_t)
        if self.v_mode.get() == MODES[0]:
            keep = s.keep_mask()
            kept = np.flatnonzero(keep)
            if len(kept):
                k = int(np.searchsorted(kept, g))
                k = int(np.clip(k + frames if (k < len(kept) and kept[k] == g) else k + (frames if frames < 0 else 0),
                                0, len(kept) - 1))
                g = int(kept[k])
        else:
            g = int(np.clip(g + frames, 0, s.n - 1))
        self.sel_seg = s.segment_at(float(s.t[g]))
        self._select_row(self.sel_seg)
        self.draw_timeline()
        self.show_at(float(s.t[g]))

    # ---- playing ---------------------------------------------------------
    def _play_size(self) -> tuple[int, int]:
        c = self.session.clips[0]
        bw, bh = self._pop_size() if self.pop is not None else (PREVIEW_W, PREVIEW_H)
        return pl.fit(c.width or 640, c.height or 480, bw, bh)

    def toggle_play(self):
        if self.player is not None:
            self.pause()
        else:
            self.play()

    def play(self, keep_stop: bool = False):
        s = self.session
        if s is None or self.busy:
            return
        self.pause(keep_stop=keep_stop)
        exported = self.v_mode.get() == MODES[0]
        t = self.play_t
        if t >= s.duration - 0.05:
            t = 0.0
        try:
            p = pl.Player(s, exported, SPEED_LABELS.get(self.v_speed.get(), 1.0), self._play_size())
            started = p.start(t)
            if not started and t > 0:
                started = p.start(0.0)
        except Exception as e:      # noqa: BLE001
            self.status.configure(text=f"Can't play: {e}")
            return
        if not started:
            self.status.configure(text="Nothing to play: every part is cut." if exported else "Nothing to play.")
            return
        self.player = p
        self.active_edge = None
        self.btn_play.configure(text="❚❚")
        if self.pop is not None:
            self.pop_btn.configure(text="❚❚")
        self.root.after(10, self._tick)

    def pause(self, keep_stop: bool = False):
        p = self.player
        self.player = None
        if not keep_stop:
            self.stop_out = None
        if p is not None:
            p.stop()
            self.btn_play.configure(text="▶")
            if self.pop is not None:
                self.pop_btn.configure(text="▶")
            self.draw_timeline()

    def _restart_play(self):
        if self.player is not None:
            self.play(keep_stop=True)

    def _tick(self):
        p = self.player
        if p is None:
            return
        r = p.due()
        if r == "end":
            self.pause()
            self.status.configure(text="Reached the end of the export." if p.tl.exported else
                                       "Reached the end of the recording.")
            return
        if r is not None:
            k, buf = r
            w, h = p.size
            self._show_img(Image.frombuffer("RGB", (w, h), buf, "raw", "RGB", 0, 1))
            o = p.out_time(k)
            s = self.session
            self.play_t = p.tl.to_session(o)
            self.preview_t = self.play_t
            i = s.segment_at(self.play_t)
            if i != self.sel_seg:
                self.sel_seg = i
                self._select_row(i)
                self.draw_timeline()
            if self.view is not None:
                a, b = self.view
                if not a <= self.play_t <= b:
                    span = b - a
                    self.set_view(self.play_t - 0.1 * span, self.play_t + 0.9 * span)
            self._draw_playhead()
            self._caption(out=(o, p.tl.total) if p.tl.exported else None)
            if self.stop_out is not None and o >= self.stop_out:
                self.pause()
                return
        self.root.after(8, self._tick)

    def play_around_cut(self):
        s = self.session
        i = self.sel_seg if self.sel_seg is not None else s.segment_at(self.play_t)
        sg = s.segments[i]
        if i == 0:
            edge = sg.t1
        elif i == len(s.segments) - 1:
            edge = sg.t0
        else:
            edge = sg.t0 if abs(self.play_t - sg.t0) <= abs(self.play_t - sg.t1) else sg.t1
        self.v_mode.set(MODES[0])
        tl = pl.Timeline(s, True)
        o = tl.to_out(edge)
        if o is None:
            o = tl.total
        self.pause()
        self.play_t = tl.to_session(max(0.0, o - 3.0))
        self.stop_out = min(tl.total, o + 3.0)
        self.status.configure(text=f"Playing 3 s either side of the cut at {dc.fmt_time(edge, True)} "
                                   f"({dc.fmt_time(o)} in the export).")
        self.play(keep_stop=True)

    # ---- larger preview window ------------------------------------------
    def _pop_size(self) -> tuple[int, int]:
        if self.pop is None:
            return PREVIEW_W, PREVIEW_H
        self.pop.update_idletasks()
        return max(320, self.pop_img.winfo_width() - 4), max(180, self.pop_img.winfo_height() - 4)

    def open_pop(self):
        if self.session is None:
            return
        if self.pop is not None:
            self.pop.lift()
            return
        top = tk.Toplevel(self.root)
        top.title("Preview")
        top.geometry("1000x640")
        top.configure(bg="#101010")
        self.pop_img = tk.Label(top, bg="#101010")
        self.pop_img.pack(fill="both", expand=True)
        bar = ttk.Frame(top, padding=4)
        bar.pack(fill="x")
        self.pop_btn = ttk.Button(bar, text="❚❚" if self.player else "▶", width=3, command=self.toggle_play)
        self.pop_btn.pack(side="left")
        self.pop_cap = ttk.Label(bar, text="")
        self.pop_cap.pack(side="left", padx=8)
        self.pop = top
        self._img_pop = None
        top.protocol("WM_DELETE_WINDOW", self.close_pop)
        self.pop_img.bind("<Double-Button-1>", lambda e: self.close_pop())
        top.bind("<Configure>", lambda e: self._pop_resized() if e.widget is top else None)
        self._caption()
        self.root.after(150, self._refresh_frame)

    def _pop_resized(self):
        if getattr(self, "_pop_job", None):
            self.root.after_cancel(self._pop_job)
        self._pop_job = self.root.after(250, self._refresh_frame)

    def _refresh_frame(self):
        self._pop_job = None
        if self.player is not None:
            self.play(keep_stop=True)            # decode at the new size
        elif self.session is not None:
            self.show_at(self.play_t)

    def close_pop(self):
        top = self.pop
        self.pop = None
        self._img_pop = None
        if top is not None:
            top.destroy()
        if self.player is not None:
            self.play(keep_stop=True)

    # ------------------------------------------------------------- timeline
    def _view(self) -> tuple[float, float]:
        s = self.session
        if self.view is None or s is None:
            return 0.0, (s.duration if s is not None else 1.0)
        return self.view

    def set_view(self, a: float, b: float):
        s = self.session
        if s is None:
            return
        span = min(s.duration, max(2.0, b - a))
        a = max(0.0, min(s.duration - span, a))
        self.view = None if span >= s.duration - 1e-6 else (a, a + span)
        self.draw_timeline()

    def _tx(self, t: float) -> float:
        a, b = self._view()
        w = max(1, self.cv.winfo_width() - 2 * X0)
        return X0 + (t - a) / max(1e-9, b - a) * w

    def _xt(self, x: float) -> float:
        a, b = self._view()
        w = max(1, self.cv.winfo_width() - 2 * X0)
        s = self.session
        return float(min(max(0.0, a + (x - X0) / w * (b - a)), s.duration - 1e-3))

    def _edge_at(self, x: float) -> str | None:
        s = self.session
        if s is None or self.sel_seg is None or self.sel_seg >= len(s.segments):
            return None
        sg = s.segments[self.sel_seg]
        ds = abs(x - self._tx(sg.t0)) if self.sel_seg > 0 else 1e9
        de = abs(x - self._tx(sg.t1)) if self.sel_seg < len(s.segments) - 1 else 1e9
        if min(ds, de) > EDGE_GRAB:
            return None
        return "start" if ds <= de else "end"

    def on_hover(self, ev):
        if self.drag is None:
            self.cv.configure(cursor="sb_h_double_arrow" if self._edge_at(ev.x) else "")

    def on_press(self, ev):
        s = self.session
        self.cv.focus_set()
        if s is None or s.n == 0:
            return
        edge = self._edge_at(ev.x)
        if edge and not self._locked():
            self.pause()
            self.push_undo()
            self.drag = ("edge", edge)
            self.active_edge = edge
            self.draw_timeline()
            return
        self.drag = ("scrub",)
        self.active_edge = None
        self._scrub(ev.x)

    def _scrub(self, x):
        s = self.session
        self.pause()
        t = self._xt(x)
        i = s.segment_at(t)
        if i != self.sel_seg:
            self.sel_seg = i
            self._select_row(i)
        self.play_t = t
        self.draw_timeline()
        if self._drag_preview_job is not None:
            self.root.after_cancel(self._drag_preview_job)
        g = s.frame_at(t)
        lab = ("good signal", "weak signal", "no signal")[int(s.labels[g])]
        self._drag_preview_job = self.root.after(40, lambda: (setattr(self, "_drag_preview_job", None),
                                                               self.show_at(t, None), self._caption_lab(lab)))

    def _caption_lab(self, lab):
        txt = self.prev_cap.cget("text")
        first, _, rest = txt.partition("\n")
        if " - " in first and lab not in first:
            self.prev_cap.configure(text=f"{first}  ({lab})\n{rest}")

    def on_drag(self, ev):
        s = self.session
        if s is None or self.drag is None:
            return
        if self.drag[0] == "edge":
            edge = self.drag[1]
            got = dc.move_edge(s, self.sel_seg, edge, self._xt(ev.x))
            if got is not None:
                self._chapters_cache = None
                sg = s.segments[self.sel_seg]
                self.status.configure(text=f"{edge.capitalize()} of row {self.sel_seg + 1} at "
                                           f"{dc.fmt_time(got, True)}. Arrow keys nudge it one frame "
                                           f"(Shift: 1 s); Esc to stop.")
                self.fill_table()
                self.draw_timeline()
                self.play_t = sg.t0 if edge == "start" else float(s.t[max(sg.f0, sg.f1 - 1)])
                self.show_edge(self.sel_seg, edge)
        else:
            self._scrub(ev.x)

    def on_release(self, ev):
        if self.drag is not None and self.drag[0] == "edge":
            self.after_edit()
        self.drag = None

    def on_timeline_double(self, ev):
        s = self.session
        if s is None or ev.y > Y_SIG:
            return
        for c in self._chapters():
            if abs(self._tx(c["at"]) - ev.x) < 7:
                self.open_chapters()
                return

    def on_timeline_menu(self, ev):
        s = self.session
        if s is None or s.n == 0:
            return
        self.active_edge = None
        self._scrub(ev.x)
        self.ctx_menu.tk_popup(ev.x_root, ev.y_root)

    def on_wheel(self, ev, delta=None, pan=False):
        s = self.session
        if s is None or s.n == 0:
            return
        d = delta if delta is not None else ev.delta
        if not d:
            return
        a, b = self._view()
        if pan or (ev.state & 0x1):
            shift = -(1 if d > 0 else -1) * 0.1 * (b - a)
            self.set_view(a + shift, b + shift)
        else:
            t = self._xt(ev.x)
            f = 0.8 if d > 0 else 1.25
            span = min(s.duration, max(2.0, (b - a) * f))
            na = t - (t - a) * span / (b - a)
            self.set_view(na, na + span)
        return "break"

    def _chapters(self) -> list[dict]:
        if self._chapters_cache is None and self.session is not None:
            self._chapters_cache = dc.chapter_list(self.session)
        return self._chapters_cache or []

    def _draw_playhead(self):
        cv = self.cv
        cv.delete("playhead")
        if self.session is None:
            return
        x = self._tx(self.play_t)
        if X0 - 1 <= x <= cv.winfo_width() - X0 + 1:
            cv.create_line(round(x) + 0.5, Y_SIG - 4, round(x) + 0.5, Y_EXP + LANE_H + 4, fill="#000000",
                           width=1, tags="playhead")

    def draw_timeline(self):
        cv = self.cv
        cv.delete("all")
        W = max(50, cv.winfo_width())
        x1 = W - X0
        s = self.session
        if s is None or s.n == 0:
            cv.create_text(X0, 8, text="Signal", anchor="w", fill="#555", font=("Segoe UI", 8))
            cv.create_text(X0, Y_EXP - 9, text="Exported video", anchor="w", fill="#555", font=("Segoe UI", 8))
            cv.create_rectangle(X0, Y_SIG, x1, Y_SIG + LANE_H, outline="#cccccc")
            cv.create_rectangle(X0, Y_EXP, x1, Y_EXP + LANE_H, outline="#cccccc")
            cv.create_text((X0 + x1) / 2, Y_SIG + LANE_H / 2, text="Run Analyse to see the session timeline",
                           fill="#999999")
            return
        a, b = self._view()
        npx = max(1, x1 - X0)
        ts = np.linspace(a, b, npx, endpoint=False)
        gi = np.clip(np.searchsorted(s.t, ts, side="right") - 1, 0, s.n - 1)
        labs = s.labels[gi].astype(np.int8)
        edges = np.clip(np.searchsorted(s.t, np.linspace(a, b, npx + 1)), 0, s.n - 1)
        if s.n > 1 and (edges[-1] - edges[0]) > npx:
            # take the "worst" label in each pixel so short dropouts stay visible
            mx = np.maximum.reduceat(s.labels, edges[:-1])
            labs = np.where(edges[1:] > edges[:-1], mx, labs).astype(np.int8)
        keep = s.keep_mask()[gi]
        cv.create_text(X0, Y_EXP - 9, text="Exported video", anchor="w", fill="#555", font=("Segoe UI", 8))
        self._draw_runs(labs, X0, Y_SIG, LANE_H, {0: COL_GOOD, 1: COL_WEAK, 2: COL_SNOW})
        for sg in s.segments:                  # post-flight STATS screens in their own colour
            if sg.kind == "stats" and sg.t1 > a and sg.t0 < b:
                cv.create_rectangle(max(X0, self._tx(sg.t0)), Y_SIG, max(self._tx(sg.t0) + 1,
                                    min(x1, self._tx(sg.t1))), Y_SIG + LANE_H, fill=COL_STATS, outline="")
        self._draw_runs(keep.astype(np.int8), X0, Y_EXP, LANE_H, {1: COL_KEEP, 0: COL_CUT})
        # file boundaries
        for c in s.clips[1:]:
            if a < c.offset < b:
                x = self._tx(c.offset)
                cv.create_line(x, Y_SIG - 3, x, Y_EXP + LANE_H + 3, fill="#444444", dash=(2, 2))
        if self.view is None:
            for c in s.clips:
                x = self._tx(c.offset)
                if c.length / s.duration * npx > 70:
                    cv.create_text(x + 3, Y_EXP + LANE_H + 10, text=os.path.splitext(os.path.basename(c.path))[0],
                                   anchor="w", fill="#666666", font=("Segoe UI", 7))
            cv.create_text(x1, Y_EXP + LANE_H + 10, text=dc.fmt_time(s.duration), anchor="e", fill="#666666",
                           font=("Segoe UI", 7))
        else:
            cv.create_text(X0, Y_EXP + LANE_H + 10, text=dc.fmt_time(a), anchor="w", fill="#666666",
                           font=("Segoe UI", 7))
            cv.create_text(x1, Y_EXP + LANE_H + 10, text=dc.fmt_time(b), anchor="e", fill="#666666",
                           font=("Segoe UI", 7))
            cv.create_text((X0 + x1) / 2, Y_EXP + LANE_H + 10, fill="#888888", font=("Segoe UI", 7),
                           text="zoomed in - scroll out, or right-click > Show the whole recording")
        # warnings
        for sg in s.segments:
            if sg.warn and a <= sg.t0 <= b:
                cv.create_text(self._tx(sg.t0), Y_SIG + LANE_H / 2, text="⚠", fill="#000000",
                               font=("Segoe UI", 11, "bold"))
        # chapter flags above the Signal lane
        cv.create_text(X0, 8, text="Signal", anchor="w", fill="#555", font=("Segoe UI", 8), tags="siglabel")
        bb = cv.bbox("siglabel")
        for c in self._chapters():
            x = self._tx(c["at"])
            if not X0 - 2 <= x <= x1 + 2:
                continue
            col = COL_FLAG_MINE if c["mine"] else COL_FLAG
            cv.create_line(round(x) + 0.5, 2, round(x) + 0.5, 15, fill=col, width=2)
            cv.create_polygon(x + 1.5, 2, x + 8, 5, x + 1.5, 8, fill=col, outline="")
            if self.view is not None or c["mine"]:
                name = c["name"] if len(c["name"]) <= 16 else c["name"][:15] + "…"
                cv.create_text(x + 10, 7, text=name, anchor="w", fill=col, font=("Segoe UI", 7))
        if bb:
            cv.create_rectangle(bb[0] - 2, bb[1], bb[2] + 2, bb[3], fill=COL_BG, outline="")
            cv.tag_raise("siglabel")
        # selection box with edges that can be dragged
        if self.sel_seg is not None and self.sel_seg < len(s.segments):
            sg = s.segments[self.sel_seg]
            xa = max(X0 - 3, self._tx(sg.t0))
            xb = max(xa + 2, min(x1 + 3, self._tx(sg.t1)))
            cv.create_rectangle(xa, Y_SIG - 2, xb, Y_EXP + LANE_H + 2, outline="#000000", width=2)
            if self.active_edge:
                xe = xa if self.active_edge == "start" else xb
                cv.create_line(xe, Y_SIG - 3, xe, Y_EXP + LANE_H + 3, fill=COL_EDGE, width=4)
        self._draw_playhead()

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
            app.show_session(s)
            root.update()
            # editing, chapters and the player, through the window's own actions
            first = next(i for i, g in enumerate(s.segments) if g.kind == "signal")
            app.sel_seg = first
            app.play_t = s.segments[first].t0 + 1.0
            app.act("start")
            app.act("chapter")
            app.act("undo")
            app.act("chapter")
            app.open_chapters()
            root.update()
            n_ch = len(dc.chapter_list(s))
            app.play()
            t_end = time.time() + 2.5
            while time.time() < t_end:
                root.update()
                time.sleep(0.01)
            played = app.play_t
            app.pause()
            lines.append(f"edit/chapters/player: {n_ch} chapters, playhead moved to {played:.2f}s")
            gui_ok = n_ch >= 2 and played > s.segments[first].t0 + 0.5 and app.history
            out = os.path.join(os.path.dirname(os.path.abspath(report_path)), "selftest_export.mkv")
            info = dc.export_video(s, out)
            lines.append(f"export: {info['duration']:.2f}s in {info['pieces']} pieces -> {out}")
            dz = os.path.join(os.path.dirname(os.path.abspath(report_path)), "selftest_diagnostics.zip")
            d = dc.save_diagnostics(s, dz)
            lines.append(f"diagnostics: {d['size']} bytes, {d['frames']} frames -> {dz}")
            es = en.EnhanceSettings()
            en.apply_preset(es, "quick")
            es.height = 1080
            ye = os.path.join(os.path.dirname(os.path.abspath(report_path)), "selftest_enhanced.mp4")
            t0 = next(g.t0 for g in s.segments if g.kind == "signal")
            r = en.enhance(s, ye, es, t_range=(t0, t0 + 4.0))
            lines.append(f"enhance: {r['size'][0]}x{r['size'][1]} via {r['encoder']} -> {ye}")
            lines.append(f"AI add-on: {'found' if en.ai_installed() else 'not installed'}")
            import models as mdl
            lines.append(f"ONNX Runtime providers: {', '.join(mdl.providers()) or 'none'}")
            if mdl.installed("bleedout"):           # the build puts the converted models here (FPV_MODEL_DIR)
                m = mdl.OnnxModel("bleedout")
                frame = np.asarray(Image.open(io.BytesIO(dc.grab_frame_png(s, t0 + 1.0, 640))).convert("RGB"))
                m.run(frame)
                t1 = time.perf_counter()
                y = m.run(frame)
                ms = (time.perf_counter() - t1) * 1000
                good = y.shape == frame.shape and float(np.abs(y.astype(int) - frame).mean()) < 40
                lines.append(f"clean-up model: {'ok' if good else 'WRONG OUTPUT'} on the {m.device} "
                             f"({m.precision}), {ms:.0f} ms for a {frame.shape[1]}x{frame.shape[0]} frame")
                gui_ok = gui_ok and good
            ok = (os.path.getsize(out) > 0 and sum(1 for g in s.segments if g.action == "cut") > 0
                  and d["frames"] > 0 and os.path.getsize(ye) > 0 and bool(gui_ok))
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
