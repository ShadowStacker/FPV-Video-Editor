"""The "Enhance video" window of FPV DVR Stitcher."""
from __future__ import annotations

import dataclasses
import io
import os
import queue
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

import dvr_core as dc
import enhance as en
import models as mdl

DENOISE = ["Off", "Light", "Medium", "Strong"]
SHARPEN = ["Off", "Light", "Medium"]
SIZES = {1080: "1080p", 1440: "1440p - recommended for uploading", 2160: "2160p (4K) - slow"}
CODECS = {"auto": "Auto (best available)", "hevc": "HEVC / H.265", "h264": "H.264 (most compatible)",
          "av1": "AV1 (smallest files)"}
# every way to size the picture, in the order shown
UPSCALERS = {"none": "No upscaling - keep the original size", "std": "Standard resize (no AI)",
             "fast": "Fast (built-in AI)", "detailed": "Detailed (built-in AI, slower)",
             **{m.id: m.name for m in mdl.upscale_models()}}
AIRES = {"full": "Full size (best detail)", "1080": "1080p, then scale up (faster)"}
FPSDETAIL = {"std": "Standard", "hd": "High-detail (slower)"}
PRESET_LABELS = {
    "quick": "Quick - no AI, about real time",
    "balanced": "Balanced - clean-up + fast AI (recommended)",
    "best": "Best quality - clean-up + detailed AI",
    "custom": "Custom",
}

# colours of the estimate bar
SEG = {"prep": "#8aa4bf", "clean": "#d08a2c", "ai": "#005fb8", "fps": "#6f42c1", "enc": "#2e9e44"}

# hover help: title, what it does, quality, speed, recommended
TIPS = {
    "quick": ("Quick (no AI)", "Repairs breakup, cleans speckles and grain, sharpens and upscales with a standard "
              "resize. Runs without the AI add-on.", "Good; fine detail stays soft", "Fastest, about real time",
              "Checking a session, or very long sessions"),
    "balanced": ("Balanced", "Repair, the BleedOut clean-up model, then the Fast AI model at full size, light "
                 "denoise, all speed options on.",
                 "Very good; slightly smoother fine textures than Best quality",
                 "Several times faster than Best quality",
                 "Uploads, if the Fast model looks right in Compare frame"),
    "best": ("Best quality", "Repair, the BleedOut clean-up model, then the Detailed AI model at full size, "
             "light denoise, all speed options on.",
             "Best; sharpest detail", "Slowest; the AI takes most of the time",
             "Your best flights, when time doesn't matter"),
    "custom": ("Custom", "Your own mix of options. The preset switches to Custom as soon as you change "
               "something that differs from a preset.", None, None, None),
    "repair": ("Repair breakup", "These two only touch the places where the signal broke up. On clean footage "
               "they change nothing. They are built in (no download) and run on the processor.", None, None,
               "Both on"),
    "fix_broken": ("Replace broken frames", "Frames that came out torn, rolled or blown out when the signal broke "
                   "up, while the frames either side are fine, are replaced. With the AI add-on they are redrawn as "
                   "in-between frames (RIFE) in AI or 60 fps exports; otherwise the nearest good frame is shown "
                   "twice. Only places where the analysis saw weak signal are checked, and breakups longer than "
                   "6 frames stay in, so a crash still shows.",
                   "Removes single-frame flashes of garbage", "Almost free", "On"),
    "fix_lines": ("Repair lines", "Fills in short-lived streak lines and bands of rainbow colour noise across the "
                  "picture from the rows above and below. Colour bands keep their brightness and detail. The OSD and "
                  "anything that stays in place are left alone.",
                  "Cleaner frames during breakup; no change on clean footage", "Small (processor)", "On"),
    "cleanup": ("Clean-up models", "AI models that clean every frame before anything else, at the recording's own "
                "size. Tick none, one or several: ticked ones run one after another, in the order shown by the "
                "numbers. Each is downloaded the first time it is used (1-4 MB) and runs on the graphics card.",
                "BleedOut makes the biggest difference on analog FPV footage; the Heavy ones also smooth detail. "
                "Use Compare frame to try combinations.",
                "Each one adds a little to an export", "Rainbows & colour bleed (BleedOut) on its own"),
    "upscale": ("Upscaling", "How the picture is enlarged to the output size. No upscaling keeps the recording's "
                "own size; everything else (repair, clean-up, denoise, sharpening, 60 fps) still applies. "
                "The built-in AI models need the AI add-on; the others are downloaded when first used.",
                "AI looks much clearer after uploading", "AI takes far longer and needs the graphics card",
                "Fast or Detailed for uploads; Compare frame shows them side by side"),
    "u:none": ("No upscaling", "Keeps the recording's own size (for example 1280x720 for HD mode). Repair, "
               "clean-up models, denoise, sharpening and 60 fps still apply.", "Original resolution, cleaned up",
               "Fastest", "When you want the cleaned-up video at its own size"),
    "u:std": ("Standard resize", "A high-quality ordinary resize (Lanczos) to the output size.",
              "Soft compared with AI", "About real time", "Quick checks"),
    "u:fast": ("Fast (built-in AI)", "A small Real-ESRGAN model made for video (realesr-animevideov3). Needs the "
               "AI add-on.", "Very good; slightly smooth on grass and gravel", "Fast", "Most uploads"),
    "u:detailed": ("Detailed (built-in AI)", "Real-ESRGAN x4plus, the strongest general model. Needs the AI add-on.",
                   "Sharpest detail", "Roughly 5-10x slower than Fast", "Your best flights"),
    "ai_res": ("AI resolution", "Full size: the AI makes the final picture (2560x1440 at 1440p). "
               "1080p, then scale up: the AI makes 1920x1080 and a standard resize takes it to the output "
               "size.", "Full size has a little more fine detail", "The 1080p option is about 45% less AI work",
               "Full size with the Fast model; 1080p with Detailed when time matters"),
    "speckle": ("Clean colour speckles", "Removes the rainbow-coloured noise analog video puts on fine detail "
                "like trees, fences and gravel. It only touches colour, so detail stays sharp.",
                "Big improvement on most FPV footage", "Almost free", "On"),
    "denoise": ("Grain denoise", "Smooths the grainy noise of analog video before upscaling.",
                "Light keeps texture. Medium and Strong smooth more and can look plastic, especially with the "
                "Detailed AI model, which removes grain itself.",
                "Light is nearly free. Medium and Strong are slow on the processor, fast with "
                "'Denoise on the graphics card'.",
                "Light with AI upscaling; Light or Medium without"),
    "sharpen": ("Sharpen", "Adds edge contrast at the end (contrast-adaptive sharpening).",
                "Helps a standard resize or no upscaling; with AI it exaggerates noise", "Almost free",
                "Off with AI, Light otherwise"),
    "fps60": ("Smooth 60 fps", "AI frame interpolation (RIFE) creates in-between frames so motion is smooth at "
              "60 fps. Your receiver records about 28 fps. Also in the Enhance menu.",
              "Much smoother motion; very fast flips can show slight warping",
              "Adds about 40-60% to an AI export", "On for sharing"),
    "fps_detail": ("60 fps detail", "High-detail uses RIFE's high-resolution mode, which handles very fast "
                   "motion at 1440p a little better.",
                   "Same on most FPV clips; High-detail helps on fast flips close to objects",
                   "High-detail is about 70% slower", "Standard"),
    "overlap": ("Work on several steps at once", "Prepares frames, AI-upscales and encodes at the same time "
                "instead of one after another. The AI starts within seconds instead of waiting for every frame "
                "to be prepared.", "Identical", "Removes the preparing wait and most of the encoding time",
                "On. Uses 1-2 GB more temporary disk space."),
    "all_cores": ("Prepare frames on all processor cores", "Splits frame preparation (and the Quick export) "
                  "into parallel parts.", "Identical",
                  "Several times faster preparation; matters most with Medium/Strong denoise and Quick",
                  "On. Turn off only if the PC must stay responsive for other work."),
    "gpu_denoise": ("Denoise on the graphics card", "Runs Medium and Strong grain denoise on the graphics card "
                    "instead of the processor. Falls back to the processor automatically if the card can't. "
                    "While the AI programs are working, fewer denoise parts run at once so the card doesn't run "
                    "out of memory.",
                    "Same filter, very similar result", "Around 10x faster for Medium and Strong", "On"),
    "size": ("Size", "Output resolution (not used with No upscaling).",
             "1440p looks clearly better than 1080p after uploading, because video sites encode 1440p uploads "
             "with a better codec and more bitrate, even for analog footage. 4K is only marginally better.",
             "4K takes about twice as long as 1440p", "1440p"),
    "codec": ("Video codec", "Auto uses your graphics card's encoder when there is one. H.264 plays everywhere; "
              "AV1 makes the smallest files (NVIDIA RTX 40 and newer).",
              "No visible difference at these settings",
              "All about the same on the graphics card", "Auto"),
    "estimate": ("Time estimate", "Uses how fast each stage actually ran on this PC (preparing, clean-up models, "
                 "AI upscaling, 60 fps, encoding). The bar shows what runs on the processor (top) and on the "
                 "graphics card (bottom). Until something has been measured it is a rough first guess; one 10 s "
                 "preview measures this PC's speed.", None, None, None),
    "compare": ("Compare frame", "Opens a window with the moment shown in the main window's preview: the "
                "original next to your setup. Add other setups (each clean-up model on its own, other upscalers, "
                "no upscaling) to see them side by side, and click one to use it.", None, None,
                "Use it to choose models before a long export"),
    "preview": ("Render 10 s preview", "Renders 10 seconds from the moment shown in the main window's preview "
                "and plays them. It also measures this PC's speed, so the estimate gets accurate.",
                None, None, None),
    "export": ("Export enhanced video", "Renders the whole session with these settings into an MP4, with the "
               "chapter markers.", None, None, None),
}
for _m in mdl.CATALOG:
    TIPS[("c:" if _m.kind == "clean" else "u:") + _m.id] = (
        _m.name, f"{_m.what} By {_m.author}, licence {_m.licence}.", _m.good,
        {"fast": "Quick on the graphics card", "medium": "Medium", "slow": "Slow"}[_m.speed], None)


def open_path(p: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(p)  # noqa
        else:
            webbrowser.open("file://" + p)
    except Exception:
        pass


class HoverHelp:
    """Explanation pop-ups for options: hover with the mouse, or tab to the control."""

    def __init__(self, root):
        self.root = root
        self.win = None
        self.job = None

    def attach(self, widget, key):
        widget.bind("<Enter>", lambda e: self._later(widget, key), add="+")
        widget.bind("<Leave>", lambda e: self.hide(), add="+")
        widget.bind("<FocusIn>", lambda e: self.show(widget, key), add="+")
        widget.bind("<FocusOut>", lambda e: self.hide(), add="+")

    def _later(self, widget, key):
        self.cancel_job()
        self.job = self.root.after(350, lambda: self.show(widget, key))

    def cancel_job(self):
        if self.job is not None:
            self.root.after_cancel(self.job)
            self.job = None

    def show(self, widget, key):
        self.cancel_job()
        self.hide()
        if key not in TIPS or not widget.winfo_exists():
            return
        title, what, q, sp, rec = TIPS[key]
        bg, fg, sub, line = "#ffffff", "#1b1b1b", "#55606b", "#c5cdd6"
        win = tk.Toplevel(self.root)
        win.wm_overrideredirect(True)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        box = tk.Frame(win, bg=bg, highlightthickness=1, highlightbackground=line)
        box.pack()
        tk.Label(box, text=title, bg=bg, fg=fg, font=("Segoe UI", 10, "bold"), anchor="w").pack(
            fill="x", padx=10, pady=(8, 2))
        tk.Label(box, text=what, bg=bg, fg=fg, wraplength=360, justify="left", anchor="w").pack(
            fill="x", padx=10)
        if q or sp:
            g = tk.Frame(box, bg=bg)
            g.pack(fill="x", padx=10, pady=(6, 0))
            for r, (lbl, txt) in enumerate((("Quality", q), ("Speed", sp))):
                if txt:
                    tk.Label(g, text=lbl, bg=bg, fg=sub, font=("Segoe UI", 9, "bold")).grid(
                        row=r, column=0, sticky="nw", padx=(0, 8))
                    tk.Label(g, text=txt, bg=bg, fg=fg, wraplength=300, justify="left").grid(
                        row=r, column=1, sticky="w")
        if rec:
            tk.Frame(box, bg=line, height=1).pack(fill="x", padx=10, pady=(6, 4))
            tk.Label(box, text=f"Recommended: {rec}", bg=bg, fg=fg, wraplength=360, justify="left",
                     anchor="w").pack(fill="x", padx=10)
        tk.Frame(box, bg=bg, height=8).pack()
        win.update_idletasks()
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        if x + w > sw - 8:
            x = max(8, sw - w - 8)
        if y + h > sh - 8:
            y = max(8, widget.winfo_rooty() - h - 4)
        win.geometry(f"+{x}+{y}")
        self.win = win

    def hide(self):
        self.cancel_job()
        if self.win is not None:
            try:
                self.win.destroy()
            except tk.TclError:
                pass
            self.win = None


def model_note(mid: str) -> tuple[str, bool]:
    """(what to show next to a model, needs a download?)"""
    if mid in ("none", "std"):
        return "", False
    if mid in ("fast", "detailed"):
        return ("AI add-on" if en.ai_installed() else "needs the AI add-on"), not en.ai_installed()
    m = mdl.BY_ID[mid]
    if mdl.installed(mid):
        return "downloaded", False
    return f"{m.size_mb:.0f} MB, not downloaded", True


class EnhanceDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.es: en.EnhanceSettings = app.enh_settings
        self.title("Enhance video")
        self.resizable(False, False)
        self.transient(app.root)
        self.q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.enc_name = "auto"
        self.cmp_win = None
        self.help = HoverHelp(self)
        self._loading = True
        st = ttk.Style()
        self.panel_bg = st.lookup("TFrame", "background") or "#f0f0f0"
        st.configure("Note.TLabel", foreground="#5f5f5f")
        st.configure("Dl.TLabel", foreground="#9a5b00")
        self._build()
        self._load()
        self._loading = False
        self._changed()
        self.after(100, self._poll)
        threading.Thread(target=self._detect, args=(self.es.codec,), daemon=True).start()
        self._fps_trace = app.v_fps60.trace_add("write", lambda *a: self._changed())
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ------------------------------------------------------------------ UI
    def _q(self, parent, key):
        """A small (?) that explains an option."""
        c = tk.Canvas(parent, width=17, height=17, highlightthickness=0, bg=self.panel_bg, takefocus=1,
                      cursor="question_arrow")
        c.create_oval(1, 1, 16, 16, outline="#8794a1", fill="#ffffff")
        c.create_text(8.5, 9, text="?", fill="#3f4c59", font=("Segoe UI", 8, "bold"))
        self.help.attach(c, key)
        c.bind("<Button-1>", lambda e: self.help.show(c, key))
        return c

    def _row(self, parent, r, label, key, widget_fn, indent=False):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=r, column=0, sticky="w", padx=(18 if indent else 0, 6), pady=2)
        w = widget_fn(parent)
        w.grid(row=r, column=1, sticky="w", pady=2)
        self._q(parent, key).grid(row=r, column=3, sticky="e", padx=4)
        self.help.attach(lbl, key)
        self.help.attach(w, key)
        return lbl, w

    def _combo(self, values, var, width):
        def make(parent):
            cb = ttk.Combobox(parent, values=values, textvariable=var, state="readonly", width=width)
            cb.bind("<<ComboboxSelected>>", lambda e: self._changed())
            return cb
        return make

    def _check(self, parent, r, text, var, key):
        cb = ttk.Checkbutton(parent, text=text, variable=var, command=self._changed)
        cb.grid(row=r, column=0, columnspan=3, sticky="w", pady=2)
        self._q(parent, key).grid(row=r, column=3, sticky="e", padx=4)
        self.help.attach(cb, key)
        return cb

    def _group(self, parent, title, key=None, **pack):
        f = ttk.LabelFrame(parent, text=title, padding=(10, 4))
        f.pack(fill="x", **pack)
        f.columnconfigure(2, weight=1)
        if key:
            self.help.attach(f, key)
        return f

    def _build(self):
        pad = {"padx": 10, "pady": 3}
        # everything sits in a scrolling area that only scrolls when the screen is too small for it
        self.cv = tk.Canvas(self, highlightthickness=0, bg=self.panel_bg)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview)
        self.cv.configure(yscrollcommand=self.vsb.set)
        self.cv.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(self.cv, padding=(10, 6))
        self.cv.create_window(0, 0, window=top, anchor="nw")
        top.bind("<Configure>", lambda e: self._fit())
        self.bind_all("<MouseWheel>", self._wheel, add="+")

        pf = ttk.LabelFrame(top, text="Preset", padding=(10, 4))
        pf.pack(fill="x", **pad)
        self.v_preset = tk.StringVar()
        for i, (val, txt) in enumerate(PRESET_LABELS.items()):
            rb = ttk.Radiobutton(pf, text=txt, value=val, variable=self.v_preset, command=self._preset_changed)
            rb.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=1)
            self.help.attach(rb, val)

        cols = ttk.Frame(top)
        cols.pack(fill="x", **pad)
        left = ttk.Frame(cols)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = ttk.Frame(cols)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))

        # ---- left: repair, clean-up models, picture
        rf = self._group(left, "Repair breakup (only where the signal broke up)", "repair")
        self.v_broken = tk.BooleanVar()
        self.v_lines = tk.BooleanVar()
        self._check(rf, 0, "Replace broken frames", self.v_broken, "fix_broken")
        self._check(rf, 1, "Repair lines", self.v_lines, "fix_lines")

        cf = self._group(left, "Clean-up models (every frame, run in this order)", "cleanup", pady=(8, 0))
        self.v_clean: dict[str, tk.BooleanVar] = {}
        self.clean_rows: dict[str, tuple] = {}
        for r, m in enumerate(mdl.clean_models()):
            v = tk.BooleanVar()
            self.v_clean[m.id] = v
            num = tk.Label(cf, text="", width=2, font=("Segoe UI", 8, "bold"), fg="#ffffff", bg=self.panel_bg)
            num.grid(row=r, column=0, sticky="w", padx=(0, 4))
            cb = ttk.Checkbutton(cf, text=m.name, variable=v, command=self._changed)
            cb.grid(row=r, column=1, sticky="w", pady=1)
            note = ttk.Label(cf, text="", style="Note.TLabel")
            note.grid(row=r, column=2, sticky="e", padx=(8, 0))
            self._q(cf, "c:" + m.id).grid(row=r, column=3, sticky="e", padx=4)
            self.help.attach(cb, "c:" + m.id)
            self.clean_rows[m.id] = (num, note)

        pic = self._group(left, "Picture", pady=(8, 0))
        self.v_speckle = tk.BooleanVar()
        self.v_denoise = tk.StringVar()
        self.v_sharpen = tk.StringVar()
        self.v_fpsdetail = tk.StringVar()
        r = 0
        self._check(pic, r, "Clean colour speckles (rainbow artifacts)", self.v_speckle, "speckle")
        r += 1
        self._row(pic, r, "Grain denoise", "denoise", self._combo(DENOISE, self.v_denoise, 10))
        r += 1
        self.tip3 = tk.Label(pic, text="With AI upscaling, Light usually looks more natural: the AI removes grain "
                                       "on its own, and Strong can look plastic.",
                             bg="#fff4d6", fg="#3b2e00", wraplength=380, justify="left", padx=8, pady=4,
                             highlightthickness=1, highlightbackground="#e6c36a")
        self.tip3_row = r
        r += 1
        self._row(pic, r, "Sharpen", "sharpen", self._combo(SHARPEN, self.v_sharpen, 10))
        r += 1
        self._check(pic, r, "Smooth 60 fps (AI frame interpolation)", self.app.v_fps60, "fps60")
        r += 1
        _, self.cb_fpsdetail = self._row(pic, r, "60 fps detail", "fps_detail",
                                         self._combo(list(FPSDETAIL.values()), self.v_fpsdetail, 20), indent=True)

        # ---- right: upscaling, output, speed
        uf = self._group(right, "Upscaling", "upscale")
        self.v_up = tk.StringVar()
        self.up_notes: dict[str, ttk.Label] = {}
        c = self.app.session.clips[0] if self.app.session else None
        orig = f"{(c.width or 640) // 2 * 2}x{(c.height or 480) // 2 * 2}" if c else ""
        for r, (val, txt) in enumerate(UPSCALERS.items()):
            rb = ttk.Radiobutton(uf, text=txt, value=val, variable=self.v_up, command=self._changed)
            rb.grid(row=r, column=0, columnspan=2, sticky="w", pady=1)
            note = ttk.Label(uf, text=orig if val == "none" else "", style="Note.TLabel")
            note.grid(row=r, column=2, sticky="e", padx=(8, 0))
            self._q(uf, "u:" + val).grid(row=r, column=3, sticky="e", padx=4)
            self.help.attach(rb, "u:" + val)
            self.up_notes[val] = note
        self.v_aires = tk.StringVar()
        _, self.cb_aires = self._row(uf, len(UPSCALERS), "AI resolution", "ai_res",
                                     self._combo(list(AIRES.values()), self.v_aires, 26), indent=True)

        of = self._group(right, "Output", pady=(8, 0))
        self.v_size = tk.StringVar()
        self.v_codec = tk.StringVar()
        _, self.cb_size = self._row(of, 0, "Size", "size", self._combo(list(SIZES.values()), self.v_size, 30))
        self.lbl_orig = tk.Label(of, text=f"Original size: {orig}", bg="#fff4d6", fg="#3b2e00", padx=6,
                                 highlightthickness=1, highlightbackground="#e6c36a")
        self._row(of, 1, "Video codec", "codec", self._combo(list(CODECS.values()), self.v_codec, 30))
        self.lbl_enc = ttk.Label(of, text="Encoder: checking...", foreground="#555555")
        self.lbl_enc.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))

        sf = self._group(right, "Speed", pady=(8, 0))
        self.v_overlap = tk.BooleanVar()
        self.v_cores = tk.BooleanVar()
        self.v_gpudn = tk.BooleanVar()
        self._check(sf, 0, "Work on several steps at once", self.v_overlap, "overlap")
        self._check(sf, 1, "Prepare frames on all processor cores", self.v_cores, "all_cores")
        self._check(sf, 2, "Denoise on the graphics card", self.v_gpudn, "gpu_denoise")
        self.lbl_gpudn = ttk.Label(sf, text="", foreground="#666666")
        self.lbl_gpudn.grid(row=3, column=0, columnspan=3, sticky="w", padx=(22, 0))

        # ---- estimate
        ef = ttk.LabelFrame(top, text="Estimated time", padding=(10, 4))
        ef.pack(fill="x", **pad)
        head = ttk.Frame(ef)
        head.pack(fill="x")
        self.lbl_total = ttk.Label(head, text="", font=("Segoe UI", 15, "bold"))
        self.lbl_total.pack(side="left")
        self.lbl_for = ttk.Label(head, text="", foreground="#555555")
        self.lbl_for.pack(side="left", padx=10)
        self._q(head, "estimate").pack(side="left")
        self.help.attach(self.lbl_total, "estimate")
        self.bar = tk.Canvas(ef, height=34, highlightthickness=0, bg="#e9ecef")
        self.bar.pack(fill="x", pady=(4, 3))
        self.bar.bind("<Configure>", lambda e: self._draw_bar())
        leg = ttk.Frame(ef)
        leg.pack(fill="x")
        for key, txt in (("prep", "Preparing frames (processor)"), ("clean", "Clean-up models"),
                         ("ai", "AI upscaling"), ("fps", "60 fps"), ("enc", "Encoding")):
            sw = tk.Canvas(leg, width=11, height=11, highlightthickness=0, bg=self.panel_bg)
            sw.create_rectangle(0, 0, 11, 11, fill=SEG[key], outline="")
            sw.pack(side="left", padx=(0, 4))
            ttk.Label(leg, text=txt, foreground="#555555").pack(side="left", padx=(0, 12))
        self.lbl_quality = ttk.Label(ef, text="", foreground="#333333")
        self.lbl_quality.pack(anchor="w", pady=(3, 0))
        self.lbl_measured = ttk.Label(ef, text="", foreground="#666666")
        self.lbl_measured.pack(anchor="w")

        af = ttk.Frame(top)
        af.pack(fill="x", padx=10, pady=(4, 0))
        self.lbl_ai = ttk.Label(af, text="")
        self.lbl_ai.pack(side="left")
        self.btn_dl = ttk.Button(af, text="Download AI add-on...", command=self.download_ai)
        self.btn_dl.pack(side="left", padx=8)
        mf = ttk.Frame(top)
        mf.pack(fill="x", padx=10, pady=(2, 0))
        self.lbl_models = ttk.Label(mf, text="")
        self.lbl_models.pack(side="left")
        self.btn_dlm = ttk.Button(mf, text="Download models", command=self.download_models)
        self.btn_dlm.pack(side="left", padx=8)

        bf = ttk.Frame(top)
        bf.pack(fill="x", padx=10, pady=(6, 0))
        self.btn_cmp = ttk.Button(bf, text="Compare frame...", command=self.compare)
        self.btn_cmp.pack(side="left")
        self.btn_prev = ttk.Button(bf, text="Render 10 s preview", command=self.preview)
        self.btn_prev.pack(side="left", padx=6)
        self.btn_go = ttk.Button(bf, text="Export enhanced video...", style="Big.TButton", command=self.export)
        self.btn_go.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bf, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(bf, text="Close", command=self.close).pack(side="right")
        for b, k in ((self.btn_cmp, "compare"), (self.btn_prev, "preview"), (self.btn_go, "export")):
            self.help.attach(b, k)

        self.pb = ttk.Progressbar(top, mode="determinate", maximum=1000, length=700)
        self.pb.pack(fill="x", padx=10, pady=(6, 2))
        self.status = ttk.Label(top, text="Hover over an option, or its ?, for what it does and the best setting.")
        self.status.pack(anchor="w", padx=10)

    def _fit(self):
        top = self.cv.winfo_children()[0] if self.cv.winfo_children() else None
        if top is None:
            return
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        maxh = max(400, self.winfo_screenheight() - 110)
        self.cv.configure(width=w, height=min(h, maxh), scrollregion=(0, 0, w, h))
        if h > maxh:
            self.vsb.pack(side="right", fill="y", before=self.cv)
        else:
            self.vsb.pack_forget()

    def _wheel(self, ev):
        try:
            if ev.widget.winfo_toplevel() is self and self.vsb.winfo_ismapped() and \
                    ev.widget.winfo_class() not in ("TCombobox", "Listbox"):
                self.cv.yview_scroll(-1 if ev.delta > 0 else 1, "units")
        except (tk.TclError, AttributeError):
            pass

    # ------------------------------------------------------------ settings
    def _load(self):
        es = self.es
        self.v_preset.set(es.preset if es.preset in PRESET_LABELS else "custom")
        self.v_speckle.set(es.speckle)
        self.v_broken.set(es.fix_broken)
        self.v_lines.set(es.fix_lines)
        on = set(en.cleanups(es))
        for mid, v in self.v_clean.items():
            v.set(mid in on)
        self.v_denoise.set(DENOISE[max(0, min(3, es.denoise))])
        self.v_sharpen.set(SHARPEN[max(0, min(2, es.sharpen))])
        self.v_up.set("none" if es.keep_size else ("std" if not es.ai_upscale else
                                                   es.ai_model if es.ai_model in UPSCALERS else "fast"))
        self.v_aires.set(AIRES.get(es.ai_res, AIRES["full"]))
        self.v_fpsdetail.set(FPSDETAIL.get(es.fps_detail, FPSDETAIL["std"]))
        self.v_overlap.set(es.overlap)
        self.v_cores.set(es.all_cores)
        self.v_gpudn.set(es.gpu_denoise)
        self.v_size.set(SIZES.get(es.height, SIZES[1440]))
        self.v_codec.set(CODECS.get(es.codec, CODECS["auto"]))

    def _read(self):
        es = self.es
        es.speckle = bool(self.v_speckle.get())
        es.fix_broken = bool(self.v_broken.get())
        es.fix_lines = bool(self.v_lines.get())
        es.cleanup = ",".join(m for m, v in self.v_clean.items() if v.get())
        es.denoise = DENOISE.index(self.v_denoise.get()) if self.v_denoise.get() in DENOISE else 1
        es.sharpen = SHARPEN.index(self.v_sharpen.get()) if self.v_sharpen.get() in SHARPEN else 0
        up = self.v_up.get() or "fast"
        es.keep_size = up == "none"
        es.ai_upscale = up not in ("none", "std")
        if es.ai_upscale:
            es.ai_model = up
        es.ai_res = next((k for k, v in AIRES.items() if v == self.v_aires.get()), "full")
        es.fps60 = bool(self.app.v_fps60.get())
        es.fps_detail = next((k for k, v in FPSDETAIL.items() if v == self.v_fpsdetail.get()), "std")
        es.overlap = bool(self.v_overlap.get())
        es.all_cores = bool(self.v_cores.get())
        es.gpu_denoise = bool(self.v_gpudn.get())
        es.height = next((k for k, v in SIZES.items() if v == self.v_size.get()), 1440)
        es.codec = next((k for k, v in CODECS.items() if v == self.v_codec.get()), "auto")

    def apply(self, es: en.EnhanceSettings):
        """Use these settings (from a comparison tile)."""
        self.es.__dict__.update(dataclasses.asdict(es))
        self._loading = True
        self._load()
        self._loading = False
        self._changed()

    def _preset_changed(self):
        name = self.v_preset.get()
        if name in en.PRESETS:
            self._read()
            en.apply_preset(self.es, name)
            self._loading = True
            self._load()
            self._loading = False
        self._changed(keep_preset=True)

    def _changed(self, keep_preset: bool = False):
        if self._loading:
            return
        self._read()
        if not keep_preset or self.v_preset.get() != "custom":
            self.es.preset = en.match_preset(self.es)
            self.v_preset.set(self.es.preset)
        else:
            self.es.preset = "custom"
        es = self.es
        ai = en.uses_ai(es)
        self.cb_aires.configure(state="readonly" if ai else "disabled")
        self.cb_fpsdetail.configure(state="readonly" if es.fps60 else "disabled")
        if es.keep_size:
            self.cb_size.grid_remove()
            self.lbl_orig.grid(row=0, column=1, sticky="w", pady=2)
        else:
            self.lbl_orig.grid_remove()
            self.cb_size.grid()
        if ai and es.denoise >= 2:
            self.tip3.grid(row=self.tip3_row, column=0, columnspan=4, sticky="we", pady=(2, 4))
        else:
            self.tip3.grid_forget()
        self._refresh_models()
        en.save_settings(es)
        self._refresh_ai()
        self._update_estimate()

    def _refresh_models(self):
        order = en.cleanups(self.es)
        for mid, (num, note) in self.clean_rows.items():
            if mid in order:
                num.configure(text=str(order.index(mid) + 1), bg="#005fb8")
            else:
                num.configure(text="", bg=self.panel_bg)
            txt, dl = model_note(mid)
            note.configure(text=txt, style="Dl.TLabel" if dl else "Note.TLabel")
        for val, note in self.up_notes.items():
            if val == "none":
                continue
            txt, dl = model_note(val)
            note.configure(text=txt, style="Dl.TLabel" if dl else "Note.TLabel")

    # ------------------------------------------------------------- estimate
    def _update_estimate(self):
        s = self.app.session
        if s is None:
            return
        try:
            e = en.estimate(s, self.es, self.enc_name)
        except Exception:
            return
        self.est = e
        self.lbl_total.configure(text=f"about {en.fmt_minutes(e['total'])}")
        self.lbl_for.configure(text=f"for {dc.fmt_time(s.kept_duration())} of footage")
        es = self.es
        steps = []
        if es.fix_broken or es.fix_lines:
            steps.append("repair")
        n = len(en.cleanups(es))
        if n:
            steps.append(f"{n} clean-up model{'s' if n > 1 else ''}")
        if es.keep_size:
            steps.append("no upscaling")
        elif not es.ai_upscale:
            steps.append("standard resize")
        else:
            steps.append(UPSCALERS.get(es.ai_model, es.ai_model).split(" (")[0].split(" - ")[0] + " AI")
        ow, oh = en.out_dims(s, es)
        self.lbl_quality.configure(text=f"Steps: {' → '.join(steps)}   ·   motion: "
                                        f"{'smooth 60 fps' if es.fps60 else 'original frame rate'}   ·   "
                                        f"output: {ow}x{oh}")
        self.lbl_measured.configure(
            text="Based on speeds measured on this PC." if e["measured"] else
            "Rough first guess. A 10 s preview measures this PC's speed and makes it accurate.")
        self._draw_bar()

    def _draw_bar(self):
        c = self.bar
        c.delete("all")
        e = getattr(self, "est", None)
        if not e or e["total"] <= 0:
            return
        w = max(50, c.winfo_width())
        total = e["total"]
        for lane, (y0, y1) in (("cpu", (3, 16)), ("gpu", (18, 31))):
            for k, a, b in e[lane]:
                if b - a <= 0:
                    continue
                xa = a / total * w
                xb = max(xa + 2, b / total * w)
                c.create_rectangle(xa, y0, xb, y1, fill=SEG[k], outline="")

    def _detect(self, codec: str):
        try:
            name = en.pick_encoder(codec)
            self.q.put(("enc", name))
        except Exception as e:
            self.q.put(("enc_fail", str(e)))
        self.q.put(("gpudn", en.gpu_denoise_works()))

    def _refresh_ai(self):
        if en.ai_installed():
            self.lbl_ai.configure(text="AI add-on: installed")
            self.btn_dl.pack_forget()
        else:
            self.lbl_ai.configure(text="AI add-on: not installed (needed for the built-in AI models and "
                                       "smooth 60 fps)")
        need = en.models_needed(self.es)
        miss = en.missing_models(self.es)
        where = next((m.device for m in mdl._LOADED.values()), None)     # known once a model has run
        if miss:
            size = sum(mdl.BY_ID[m].size_mb for m in miss)
            self.lbl_models.configure(text="Models: " + ", ".join(mdl.BY_ID[m].name for m in miss)
                                           + f" not downloaded yet ({size:.0f} MB)")
            self.btn_dlm.pack(side="left", padx=8)
        else:
            self.lbl_models.configure(text=("Models: downloaded" + (f", running on the {where}" if where else ""))
                                      if need else "")
            self.btn_dlm.pack_forget()
        self._refresh_models()

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
        for b in (self.btn_prev, self.btn_go, self.btn_dl, self.btn_dlm):
            b.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.app.lock_for_enhance(busy)

    def _start(self, fn, done_kind):
        self._read()
        en.save_settings(self.es)
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

    def _ensure_ready(self, then) -> bool:
        """Download the AI add-on / models these settings need (asks first), then run `then`."""
        self._read()
        addon = en.needs_addon(self.es) and not en.ai_installed()
        miss = en.missing_models(self.es)
        if not addon and not miss:
            return True
        what = (["AI add-on (about 70 MB)"] if addon else []) + \
               [f"{mdl.BY_ID[m].name} ({mdl.BY_ID[m].size_mb:.0f} MB)" for m in miss]
        if not messagebox.askyesno("Enhance video", "These settings need:\n  " + "\n  ".join(what) +
                                   "\n\nDownload now from this program's GitHub releases?", parent=self):
            return False

        def job():
            if addon:
                en.download_ai_addon(self._progress, self.cancel)
            for m in miss:
                mdl.download(m, self._progress, self.cancel)
            return then
        self._start(job, "downloaded")
        return False

    def download_models(self):
        if self.busy:
            return
        miss = en.missing_models(self.es)
        if miss:
            self._start(lambda: [mdl.download(m, self._progress, self.cancel) for m in miss] and None, "downloaded")

    def compare(self):
        if self.cmp_win is not None and self.cmp_win.winfo_exists():
            self.cmp_win.lift()
            self.cmp_win.refresh()
            return
        self._read()
        self.cmp_win = CompareWindow(self, self._moment())

    def preview(self):
        if self.busy or not self._ensure_ready(self.preview):
            return
        t = self._moment()
        s = self.app.session
        out = en.preview_path()
        self._start(lambda: en.enhance(s, out, self.es, self._progress, self.cancel, t_range=(t, t + 10.0)),
                    "preview")

    def export(self):
        if self.busy or not self._ensure_ready(self.export):
            return
        s = self.app.session
        first = os.path.splitext(os.path.basename(s.clips[0].path))[0]
        last = os.path.splitext(os.path.basename(s.clips[-1].path))[0]
        name = f"{first}-{last}_enhanced.mp4" if first != last else f"{first}_enhanced.mp4"
        out = filedialog.asksaveasfilename(parent=self, title="Save enhanced video as",
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
        if self.cmp_win is not None and self.cmp_win.winfo_exists():
            self.cmp_win.close()
        try:
            self.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self._read()
        en.save_settings(self.es)
        self.help.hide()
        try:
            self.app.v_fps60.trace_remove("write", self._fps_trace)
        except (tk.TclError, ValueError):
            pass
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
                    self.enc_name = msg[1]
                    self.lbl_enc.configure(text=f"Encoder: {en.ENCODER_NAMES.get(msg[1], msg[1])}")
                    self._update_estimate()
                elif kind == "enc_fail":
                    self.lbl_enc.configure(text=f"Encoder: none found ({msg[1]})")
                elif kind == "gpudn":
                    self.lbl_gpudn.configure(text="" if msg[1] else
                                             "(not available on this PC - the processor is used)")
                    self._update_estimate()
                elif kind == "preview":
                    self._set_busy(False)
                    r = msg[1]
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Preview rendered in {en.fmt_minutes(r['seconds'])} "
                                               f"({r['encoder']}). The estimate now uses this PC's speed.")
                    self._update_estimate()
                    open_path(r["path"])
                elif kind == "exported":
                    self._set_busy(False)
                    r = msg[1]
                    self.pb.configure(value=1000)
                    self.status.configure(text=f"Saved {r['path']}")
                    self._update_estimate()
                    rep = ""
                    if r.get("replaced") or r.get("lines"):
                        rep = (f"\nRepaired: {r.get('replaced', 0)} broken frame(s) replaced, lines fixed in "
                               f"{r.get('lines', 0)} frame(s).")
                    if r.get("cleanup_device"):
                        rep += f"\nClean-up models ran on the {r['cleanup_device']}."
                    if messagebox.askyesno(
                            "Enhance video",
                            f"Saved:\n{r['path']}\n\n{r['size'][0]}x{r['size'][1]}, {r['fps']:.0f} fps, "
                            f"{r['encoder']}, took {en.fmt_minutes(r['seconds'])}.{rep}\n\nOpen the folder now?",
                            parent=self):
                        open_path(os.path.dirname(r["path"]))
                elif kind == "downloaded":
                    self._set_busy(False)
                    self.pb.configure(value=1000)
                    self.status.configure(text="Downloaded.")
                    self._refresh_ai()
                    if callable(msg[1]):
                        self.after(50, msg[1])          # carry on with what was asked for
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Cancelled.")
                elif kind == "error":
                    self._set_busy(False)
                    self.pb.configure(value=0)
                    self.status.configure(text="Error: " + msg[1].splitlines()[0][:150])
                    messagebox.showerror("Enhance video", msg[1][:3000], parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)


# ---------------------------------------------------------------------------
# Compare window
# ---------------------------------------------------------------------------
def _short(mid: str) -> str:
    if mid in mdl.BY_ID:
        m = mdl.BY_ID[mid]
        return m.name.split(" (")[-1].rstrip(")") if m.kind == "clean" else m.name.split(" (")[0]
    return {"none": "No upscaling", "std": "Standard resize", "fast": "Fast AI", "detailed": "Detailed AI"}[mid]


class CompareWindow(tk.Toplevel):
    """The same moment with several setups side by side. Tiles show the middle of the frame, so
    every tile shows the same part of the picture at the same size."""

    def __init__(self, dlg: EnhanceDialog, t: float):
        super().__init__(dlg)
        self.dlg, self.t = dlg, t
        self.title(f"Compare - {dc.fmt_time(t, True)}")
        self.transient(dlg)
        self.cancel = threading.Event()
        self.q: queue.Queue = queue.Queue()
        self.cache: dict[tuple, bytes | str] = {}
        self.pending: set = set()
        self.imgs: list = []
        self.worker = None
        self.protocol("WM_DELETE_WINDOW", self.close)
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Add setups to compare with yours. Each tile is the same frame, the middle part of "
                              "the picture. Click a tile to use its setup.").pack(anchor="w")
        self.v_whole = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text="Show the whole frame instead of the middle", variable=self.v_whole,
                        command=self.refresh).pack(anchor="w", pady=(4, 0))
        self.chips = ttk.Frame(outer)
        self.chips.pack(fill="x", pady=(6, 4))
        self.v_add: dict[str, tk.BooleanVar] = {}
        rows = [("Clean-up:", ["c:none"] + ["c:" + m.id for m in mdl.clean_models()]),
                ("Upscaling:", ["u:" + k for k in UPSCALERS])]
        for r, (lbl, keys) in enumerate(rows):
            ttk.Label(self.chips, text=lbl).grid(row=r, column=0, sticky="w", padx=(0, 6), pady=2)
            for c, k in enumerate(keys, 1):
                v = tk.BooleanVar(value=k in ("c:none",))
                self.v_add[k] = v
                text = "No clean-up" if k == "c:none" else _short(k.split(":", 1)[1]) + (" only" if k[0] == "c" else "")
                ttk.Checkbutton(self.chips, text=text, variable=v,
                                command=self.refresh).grid(row=r, column=c, sticky="w", padx=(2, 10), pady=2)
        self.grid_f = ttk.Frame(outer)
        self.grid_f.pack(fill="both", expand=True, pady=(6, 0))
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(6, 0))
        self.status = ttk.Label(bottom, text="")
        self.status.pack(side="left")
        ttk.Button(bottom, text="Close", command=self.close).pack(side="right")
        self.tile_w = 400 if self.winfo_screenwidth() >= 1300 else 300
        self.after(100, self._poll)
        self.refresh()

    # which setups, with their settings
    def variants(self) -> list[tuple[str, str, en.EnhanceSettings | None]]:
        self.dlg._read()
        es = dataclasses.replace(self.dlg.es, fps60=False)
        out = [("orig", "Original recording", None), ("yours", "Your setup", es)]
        for k, v in self.v_add.items():
            if not v.get():
                continue
            kind, mid = k.split(":", 1)
            if kind == "c":
                if mid == "none":
                    if en.cleanups(es):
                        out.append((k, "Your setup without clean-up", dataclasses.replace(es, cleanup="")))
                elif en.cleanups(es) != [mid]:
                    out.append((k, f"Clean-up: {_short(mid)} only", dataclasses.replace(es, cleanup=mid)))
            else:
                v_es = dataclasses.replace(es, keep_size=mid == "none", ai_upscale=mid not in ("none", "std"),
                                           ai_model=mid if mid not in ("none", "std") else es.ai_model)
                if en.out_dims(self.dlg.app.session, v_es) == en.out_dims(self.dlg.app.session, es) and \
                        (v_es.keep_size, en.uses_ai(v_es), v_es.ai_model) == (es.keep_size, en.uses_ai(es), es.ai_model):
                    continue                       # the same as your setup
                out.append((k, f"Upscaling: {_short(mid)}", v_es))
        return out

    @staticmethod
    def _sig(es) -> tuple:
        return () if es is None else tuple(sorted(dataclasses.asdict(es).items()))

    def refresh(self):
        whole = bool(self.v_whole.get())
        for w in self.grid_f.winfo_children():
            w.destroy()
        self.imgs = []
        todo = []
        cols = 3
        for i, (key, label, es) in enumerate(self.variants()):
            ck = (self._sig(es), whole)
            cell = ttk.Frame(self.grid_f)
            cell.grid(row=i // cols, column=i % cols, padx=3, pady=3, sticky="n")
            got = self.cache.get(ck)
            if isinstance(got, bytes):
                im = Image.open(io.BytesIO(got)).convert("RGB")
                im = im.resize((self.tile_w, max(1, round(im.height * self.tile_w / im.width))), Image.LANCZOS)
                ph = ImageTk.PhotoImage(im)
                self.imgs.append(ph)
                pic = tk.Label(cell, image=ph, bd=0, cursor="hand2" if es is not None else "")
            else:
                text = got if isinstance(got, str) else "Working..."
                pic = tk.Label(cell, text=text, width=self.tile_w // 8, height=12, bg="#202020", fg="#dddddd",
                               wraplength=self.tile_w - 20)
                if got is None:
                    todo.append((ck, es))
            pic.pack()
            ttk.Label(cell, text=label).pack(anchor="w")
            if es is not None and key != "yours":
                pic.bind("<Button-1>", lambda e, es=es, label=label: self.use(es, label))
        self._render(todo)

    def use(self, es, label):
        self.dlg.apply(dataclasses.replace(es, fps60=self.dlg.es.fps60))
        self.status.configure(text=f"Using: {label}.")
        self.refresh()

    def _render(self, todo):
        todo = [(ck, es) for ck, es in todo if ck not in self.pending]
        if not todo:
            return
        for ck, _ in todo:
            self.pending.add(ck)
        s = self.dlg.app.session
        t = self.t

        def work():
            for ck, es in todo:
                if self.cancel.is_set():
                    return
                try:
                    if es is not None:
                        if en.needs_addon(es) and not en.ai_installed():
                            raise RuntimeError("Needs the AI add-on (Enhance > Download AI add-on).")
                        for m in en.missing_models(es):
                            self.q.put(("status", f"Downloading {mdl.BY_ID[m].name}..."))
                            mdl.download(m, None, self.cancel)
                    self.q.put(("status", "Rendering..."))
                    png = en.compare_tile(s, t, es, self.cancel, whole=ck[1])
                    self.q.put(("tile", ck, png))
                except dc.Cancelled:
                    return
                except Exception as e:      # noqa: BLE001 - shown in the tile
                    self.q.put(("tile", ck, f"Could not render this setup:\n{str(e)[:300]}"))
            self.q.put(("status", ""))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _poll(self):
        changed = False
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "tile":
                    self.cache[msg[1]] = msg[2]
                    self.pending.discard(msg[1])
                    changed = True
                elif msg[0] == "status":
                    self.status.configure(text=msg[1])
        except queue.Empty:
            pass
        if changed:
            self.refresh()
            self.dlg._refresh_ai()
        if self.winfo_exists():
            self.after(150, self._poll)

    def close(self):
        self.cancel.set()
        self.dlg.cmp_win = None
        self.destroy()
