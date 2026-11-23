"""
FPV DVR Stitcher - the Chapters window (Edit > Chapters...).

One chapter per flight is added automatically; the user can add their own (right-click >
Add chapter here), rename any of them and copy the list in the format YouTube reads from a
video's description.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import dvr_core as dc

COL_OK = "#1e6b2f"
COL_WARN = "#9a5b00"


class ChaptersWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Chapters")
        self.transient(app.root)
        self.geometry("560x560")
        self.minsize(460, 420)
        self.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, wraplength=520, justify="left",
                  text="One chapter per flight is added automatically. Add your own with right-click > "
                       "Add chapter here. Click a name to rename it.").pack(fill="x")

        box = ttk.Frame(outer, borderwidth=1, relief="solid")
        box.pack(fill="both", expand=True, pady=(8, 6))
        self.cv = tk.Canvas(box, highlightthickness=0, bg="#ffffff", height=180)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=sb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.rows = tk.Frame(self.cv, bg="#ffffff")
        self.win = self.cv.create_window(0, 0, window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))

        ttk.Label(outer, text="For YouTube, paste this into the video's description:").pack(anchor="w")
        self.text = tk.Text(outer, height=6, wrap="none", font=("Consolas", 10))
        self.text.pack(fill="x", pady=(2, 4))
        self.msgs = ttk.Frame(outer)
        self.msgs.pack(fill="x")
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Copy for YouTube", command=self.copy).pack(side="left")
        self.copied = ttk.Label(btns, text="")
        self.copied.pack(side="left", padx=8)
        ttk.Button(btns, text="Close", command=self.close).pack(side="right")
        ttk.Label(outer, wraplength=520, justify="left", foreground="#555555",
                  text="The same list goes into MKV and YouTube MP4 exports as chapter markers, and is saved "
                       "next to the video as ..._chapters.txt.").pack(fill="x", pady=(8, 0))
        self.refresh()

    def close(self):
        self.app.chapters_win = None
        self.destroy()

    # --------------------------------------------------------------------
    def refresh(self):
        s = self.app.session
        for w in self.rows.winfo_children():
            w.destroy()
        for w in self.msgs.winfo_children():
            w.destroy()
        if s is None:
            return
        chs = dc.chapter_list(s)
        hdr = {"bg": "#ffffff", "font": ("Segoe UI", 9, "bold")}
        tk.Label(self.rows, text="In video", **hdr).grid(row=0, column=0, sticky="w", padx=(6, 10), pady=2)
        tk.Label(self.rows, text="Name", **hdr).grid(row=0, column=1, sticky="w", pady=2)
        self.rows.columnconfigure(1, weight=1)
        for i, c in enumerate(chs, 1):
            tk.Label(self.rows, text=dc.fmt_time(c["out"]), bg="#ffffff", font=("Consolas", 10)).grid(
                row=i, column=0, sticky="w", padx=(6, 10), pady=1)
            var = tk.StringVar(value=c["name"])
            e = ttk.Entry(self.rows, textvariable=var)
            e.grid(row=i, column=1, sticky="ew", pady=1)
            e.bind("<Return>", lambda ev, c=c, v=var: self.rename(c, v.get()))
            e.bind("<FocusOut>", lambda ev, c=c, v=var: self.rename(c, v.get()))
            tip = f"{dc.fmt_time(c['len'])} long"
            if c["moved"]:
                tip += " - moved to the next kept moment (its spot is cut)"
            tk.Label(self.rows, text="yours" if c["mine"] else "flight", bg="#ffffff",
                     fg="#6f42c1" if c["mine"] else COL_OK).grid(row=i, column=2, padx=6)
            if c["mine"]:
                ttk.Button(self.rows, text="✕", width=3, command=lambda c=c: self.delete(c)).grid(
                    row=i, column=3, padx=(0, 6))
            tk.Label(self.rows, text=tip, bg="#ffffff", fg="#777777", font=("Segoe UI", 8)).grid(
                row=i, column=4, sticky="w", padx=(0, 6))
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", dc.youtube_text(chs))
        for ok, m in dc.youtube_checks(chs) if chs else [(False, "No chapters: nothing is kept.")]:
            ttk.Label(self.msgs, text=("✓ " if ok else "⚠ ") + m, foreground=COL_OK if ok else COL_WARN,
                      wraplength=520, justify="left").pack(anchor="w")

    def rename(self, c: dict, name: str):
        s = self.app.session
        name = name.strip()
        if s is None or not name or name == c["name"]:
            return
        self.app.push_undo()
        if c["mine"]:
            for ch in s.chapters:
                if ch.id == c["id"]:
                    ch.name = name
        else:
            s.flight_names[c["flight"]] = name
        c["name"] = name
        self.app.after_edit(refresh_chapters=False)
        self.after_idle(self._update_text)

    def _update_text(self):
        s = self.app.session
        if s is None:
            return
        chs = dc.chapter_list(s)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", dc.youtube_text(chs))

    def delete(self, c: dict):
        s = self.app.session
        if s is None:
            return
        self.app.push_undo()
        s.chapters = [ch for ch in s.chapters if ch.id != c["id"]]
        self.app.after_edit()

    def copy(self):
        txt = self.text.get("1.0", "end").strip()
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.copied.configure(text="Copied.")
        except tk.TclError:
            self.text.tag_add("sel", "1.0", "end")
            self.copied.configure(text="Selected - press Ctrl+C.")
