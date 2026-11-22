"""The "Enhance for YouTube" window of FPV DVR Stitcher."""
from __future__ import annotations

import base64
import os
import queue
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import dvr_core as dc
import enhance as en

DENOISE = ["Off", "Light", "Medium", "Strong"]
SHARPEN = ["Off", "Light", "Medium"]
SIZES = {1080: "1080p", 1440: "1440p - recommended for YouTube", 2160: "2160p (4K) - slow"}
CODECS = {"auto": "Auto (best available)", "hevc": "HEVC / H.265", "h264": "H.264 (most compatible)",
          "av1": "AV1 (smallest files)"}
MODELS = {"detailed": "Detailed (slower)", "fast": "Fast (smoother look)"}


def open_path(p: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(p)  # noqa
        else:
            webbrowser.open("file://" + p)
    except Exception:
        pass


class EnhanceDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.es: en.EnhanceSettings = app.enh_settings
        self.title("Enhance for YouTube")
        self.resizable(False, False)
        self.transient(app.root)
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.cmp_img = None
        self._loading = True
        self._build()
        self._load()
        self._loading = False
        self.after(100, self._poll)
        threading.Thread(target=self._detect_encoder, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        top = ttk.Frame(self, padding=10)
        top.pack(fill="both", expand=True)

        pf = ttk.LabelFrame(top, text="Preset", padding=8)
        pf.pack(fill="x", **pad)
        self.v_preset = tk.StringVar()
        for val, txt in (("quick", "Quick - no AI, about real time"),
                         ("youtube", "YouTube quality - AI upscaling on the graphics card"),
                         ("custom", "Custom")):
            ttk.Radiobutton(pf, text=txt, value=val, variable=self.v_preset,
                            command=self._preset_changed).pack(anchor="w")

        cf = ttk.LabelFrame(top, text="Picture", padding=8)
        cf.pack(fill="x", **pad)
        self.v_speckle = tk.BooleanVar()
        ttk.Checkbutton(cf, text="Clean colour speckles (rainbow artifacts of analog video)",
                        variable=self.v_speckle, command=self._changed).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(cf, text="Grain denoise").grid(row=1, column=0, sticky="w", pady=3)
        self.v_denoise = tk.StringVar()
        cb = ttk.Combobox(cf, values=DENOISE, textvariable=self.v_denoise, state="readonly", width=10)
        cb.grid(row=1, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Label(cf, text="Sharpen").grid(row=1, column=2, sticky="w", padx=(18, 0))
        self.v_sharpen = tk.StringVar()
        cb = ttk.Combobox(cf, values=SHARPEN, textvariable=self.v_sharpen, state="readonly", width=10)
        cb.grid(row=1, column=3, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Label(cf, text="Upscaling").grid(row=2, column=0, sticky="w", pady=3)
        self.v_ai = tk.BooleanVar()
        uf = ttk.Frame(cf)
        uf.grid(row=2, column=1, columnspan=3, sticky="w", padx=6)
        ttk.Radiobutton(uf, text="AI (Real-ESRGAN)", value=True, variable=self.v_ai,
                        command=self._changed).pack(side="left")
        ttk.Radiobutton(uf, text="Standard", value=False, variable=self.v_ai,
                        command=self._changed).pack(side="left", padx=8)
        ttk.Label(uf, text="AI model").pack(side="left", padx=(12, 4))
        self.v_model = tk.StringVar()
        self.cb_model = ttk.Combobox(uf, values=list(MODELS.values()), textvariable=self.v_model,
                                     state="readonly", width=18)
        self.cb_model.pack(side="left")
        self.cb_model.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Checkbutton(cf, text="Smooth 60 fps (AI frame interpolation - roughly doubles the time)",
                        variable=self.app.v_fps60, command=self._changed).grid(row=3, column=0, columnspan=4,
                                                                               sticky="w", pady=(4, 0))

        of = ttk.LabelFrame(top, text="Output", padding=8)
        of.pack(fill="x", **pad)
        ttk.Label(of, text="Size").grid(row=0, column=0, sticky="w")
        self.v_size = tk.StringVar()
        cb = ttk.Combobox(of, values=list(SIZES.values()), textvariable=self.v_size, state="readonly", width=32)
        cb.grid(row=0, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ttk.Label(of, text="Video codec").grid(row=1, column=0, sticky="w", pady=3)
        self.v_codec = tk.StringVar()
        cb = ttk.Combobox(of, values=list(CODECS.values()), textvariable=self.v_codec, state="readonly", width=32)
        cb.grid(row=1, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: (self._changed(), threading.Thread(
            target=self._detect_encoder, daemon=True).start()))
        self.lbl_enc = ttk.Label(of, text="Encoder: checking...")
        self.lbl_enc.grid(row=1, column=2, sticky="w", padx=6)

        af = ttk.Frame(top)
        af.pack(fill="x", **pad)
        self.lbl_ai = ttk.Label(af, text="")
        self.lbl_ai.pack(side="left")
        self.btn_dl = ttk.Button(af, text="Download AI add-on...", command=self.download_ai)
        self.btn_dl.pack(side="left", padx=8)

        bf = ttk.Frame(top)
        bf.pack(fill="x", **pad)
        self.btn_cmp = ttk.Button(bf, text="Compare frame", command=self.compare)
        self.btn_cmp.pack(side="left")
        self.btn_prev = ttk.Button(bf, text="Render 10 s preview", command=self.preview)
        self.btn_prev.pack(side="left", padx=6)
        self.btn_go = ttk.Button(bf, text="Export for YouTube...", style="Big.TButton", command=self.export)
        self.btn_go.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(bf, text="Close", command=self.close).pack(side="right")
        ttk.Label(top, text="Compare frame and the preview use the moment last shown in the main window's preview.",
                  foreground="#666666").pack(anchor="w", padx=10)

        self.pb = ttk.Progressbar(top, mode="determinate", maximum=1000, length=640)
        self.pb.pack(fill="x", padx=10, pady=(8, 2))
        self.status = ttk.Label(top, text="")
        self.status.pack(anchor="w", padx=10)
        self._refresh_ai()

    def _load(self):
        es = self.es
        self.v_preset.set(es.preset)
        self.v_speckle.set(es.speckle)
        self.v_denoise.set(DENOISE[max(0, min(3, es.denoise))])
        self.v_sharpen.set(SHARPEN[max(0, min(2, es.sharpen))])
        self.v_ai.set(es.ai_upscale)
        self.v_model.set(MODELS.get(es.ai_model, MODELS["detailed"]))
        self.v_size.set(SIZES.get(es.height, SIZES[1440]))
        self.v_codec.set(CODECS.get(es.codec, CODECS["auto"]))
        self._sync_state()

    def _read(self):
        es = self.es
        es.speckle = bool(self.v_speckle.get())
        es.denoise = DENOISE.index(self.v_denoise.get()) if self.v_denoise.get() in DENOISE else 1
        es.sharpen = SHARPEN.index(self.v_sharpen.get()) if self.v_sharpen.get() in SHARPEN else 0
        es.ai_upscale = bool(self.v_ai.get())
        es.ai_model = next((k for k, v in MODELS.items() if v == self.v_model.get()), "detailed")
        es.height = next((k for k, v in SIZES.items() if v == self.v_size.get()), 1440)
        es.codec = next((k for k, v in CODECS.items() if v == self.v_codec.get()), "auto")
        es.fps60 = bool(self.app.v_fps60.get())

    def _preset_changed(self):
        name = self.v_preset.get()
        if name in en.PRESETS:
            en.apply_preset(self.es, name)
            self._loading = True
            self._load()
            self._loading = False
        else:
            self.es.preset = "custom"
        self._sync_state()

    def _changed(self):
        if self._loading:
            return
        self._read()
        # still matches a preset?  otherwise it's custom
        name = "custom"
        for p, vals in en.PRESETS.items():
            if all(getattr(self.es, k) == v for k, v in vals.items()):
                name = p
        self.es.preset = name
        self.v_preset.set(name)
        self._sync_state()

    def _sync_state(self):
        self.cb_model.configure(state="readonly" if self.v_ai.get() else "disabled")

    def _refresh_ai(self):
        if en.ai_installed():
            self.lbl_ai.configure(text="AI add-on: installed")
            self.btn_dl.pack_forget()
        else:
            self.lbl_ai.configure(text="AI add-on: not installed (needed for AI upscaling and smooth 60 fps)")

    def _detect_encoder(self):
        self._read()
        try:
            name = en.pick_encoder(self.es.codec)
            self.q.put(("enc", f"Encoder: {en.ENCODER_NAMES.get(name, name)}"))
        except Exception as e:
            self.q.put(("enc", f"Encoder: none found ({e})"))

    # -------------------------------------------------------------- actions
    def _moment(self) -> float:
        s = self.app.session
        t = self.app.preview_t
        keep = s.keep_mask()
        if t is None or not keep[int(min(s.n - 1, max(0, s.t.searchsorted(t) - 1)))]:
            idx = keep.nonzero()[0]
            t = float(s.t[idx[min(len(idx) - 1, len(idx) // 3)]]) if len(idx) else 0.0
        return float(t)

    def _set_busy(self, busy: bool):
        self.busy = busy
        for b in (self.btn_cmp, self.btn_prev, self.btn_go, self.btn_dl):
            b.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.app.lock_for_enhance(busy)

    def _start(self, fn, done_kind):
        self._read()
        self.cancel.clear()
        self._set_busy(True)

        def work():
            try:
                self.q.put((done_kind, fn()))
            except dc.Cancelled:
                self.q.put(("cancelled",))
            except Exception as e:
                self.q.put(("error", str(e), traceback.format_exc()))
        threading.Thread(target=work, daemon=True).start()

    def _progress(self, f, m):
        self.q.put(("progress", f, m))

    def compare(self):
        if self.busy:
            return
        t = self._moment()
        s = self.app.session
        self.status.configure(text=f"Building comparison at {dc.fmt_time(t, True)}...")
        self._start(lambda: (t, en.compare_frame(s, t, self.es, self.cancel)), "compare")

    def preview(self):
        if self.busy:
            return
        t = self._moment()
        s = self.app.session
        out = en.preview_path()
        self._start(lambda: en.enhance(s, out, self.es, self._progress, self.cancel, t_range=(t, t + 10.0)),
                    "preview")

    def export(self):
        if self.busy:
            return
        s = self.app.session
        first = os.path.splitext(os.path.basename(s.clips[0].path))[0]
        last = os.path.splitext(os.path.basename(s.clips[-1].path))[0]
        name = f"{first}-{last}_youtube.mp4" if first != last else f"{first}_youtube.mp4"
        out = filedialog.asksaveasfilename(parent=self, title="Save YouTube video as",
                                           initialdir=os.path.dirname(s.clips[0].path), initialfile=name,
                                           defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if not out:
            return
        if not out.lower().endswith(".mp4"):
            out += ".mp4"
        self._start(lambda: en.enhance(s, out, self.es, self._progress, self.cancel), "exported")

    def download_ai(self):
        if self.busy:
            return
        if not messagebox.askyesno("AI add-on", "Download the AI add-on (Real-ESRGAN upscaler and RIFE frame "
                                   "interpolation, about 70 MB) from this program's GitHub releases?",
                                   parent=self):
            return
        self._start(lambda: en.download_ai_addon(self._progress, self.cancel), "downloaded")

    def close(self):
        if self.busy:
            if not messagebox.askyesno("Enhance", "Stop the running job and close?", parent=self):
                return
            self.cancel.set()
        self._read()
        self.destroy()

    # ---------------------------------------------------------------- queue
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    self.pb.configure(value=int(msg[1] * 1000))
                    self.status.configure(text=msg[2])
                elif kind == "enc":
                    self.lbl_enc.configure(text=msg[1])
                elif kind == "compare":
                    self._set_busy(False)
                    t, png = msg[1]
                    self.status.configure(text=f"Comparison at {dc.fmt_time(t, True)} "
                                               f"(top: whole frame, bottom: 100% detail).")
                    self._show_compare(png, t)
                elif kind == "preview":
                    self._set_busy(False)
                    r = msg[1]
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Preview rendered in {r['seconds']:.0f} s ({r['encoder']}).")
                    open_path(r["path"])
                elif kind == "exported":
                    self._set_busy(False)
                    r = msg[1]
                    self.pb.configure(value=1000)
                    mins = r["seconds"] / 60
                    self.status.configure(text=f"Saved {r['path']}")
                    if messagebox.askyesno(
                            "Enhance for YouTube",
                            f"Saved:\n{r['path']}\n\n{r['size'][0]}x{r['size'][1]}, {r['fps']:.0f} fps, "
                            f"{r['encoder']}, took {mins:.0f} min.\n\nOpen the folder now?", parent=self):
                        open_path(os.path.dirname(r["path"]))
                elif kind == "downloaded":
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.status.configure(text="AI add-on installed.")
                    self._refresh_ai()
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Cancelled.")
                elif kind == "error":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Error: " + msg[1].splitlines()[0][:150])
                    messagebox.showerror("Enhance for YouTube", msg[1][:3000], parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def _show_compare(self, png: bytes, t: float):
        win = tk.Toplevel(self)
        win.title(f"Before (left) / after (right) - {dc.fmt_time(t, True)}")
        try:
            img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")
            sw = self.winfo_screenwidth() - 80
            if img.width() > sw:
                img = img.subsample(2)
            self.cmp_img = img
            tk.Label(win, image=img).pack()
        except tk.TclError as e:
            tk.Label(win, text=f"Could not show the comparison: {e}").pack(padx=20, pady=20)
        ttk.Label(win, text="Top: whole frame.   Bottom: 100% detail from the centre.   "
                            "Left: original (plain resize).   Right: enhanced.").pack(pady=4)
