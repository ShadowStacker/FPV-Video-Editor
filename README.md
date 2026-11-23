# FPV DVR Stitcher

A Windows app for **analog FPV DVR footage**. Add the clips your goggles or handheld receiver
recorded during a session (SD 3-minute clips or one long 720p HD-mode file) and it turns them
into one clean video:

- **Joins the clips** in order, with no seams between them.
- **Cuts out the dead static** recorded while the quad was powered off (before the first pack,
  battery swaps, after the last one) and the **post-flight Betaflight STATS screen**.
- **Keeps weak, breaking-up signal**, which is exactly what you need when looking for a crashed
  or lost quad, and **flags suspicious signal losses** (faded out = out of range or crash).
- **Keeps the original quality**: the normal export copies frames byte-for-byte, no re-encoding.
  Audio is removed.
- **Lets you fine-tune everything**: drag or type the cuts, split parts, play the result as it
  will be exported, undo.
- **Chapters**: one per flight plus your own, written into MKV / MP4 exports and saved as a list
  ready to paste into a video description.
- **Enhance video**: an optional second export that repairs breakup, cleans up the analog
  noise with AI models made for analog video, upscales (or keeps the original size), smooths to
  60 fps and encodes on the graphics card.

![Main window](docs/screenshot.png)

## Contents

- [Download](#download)
- [Quick start](#quick-start)
- [The main window](#the-main-window)
- [Editing, playing and chapters](#editing-playing-and-chapters)
- [Settings](#settings)
- [Export video (lossless)](#export-video-lossless)
- [Enhance video](#enhance-video)
- [The AI models](#the-ai-models)
- [How static is told apart from weak signal](#how-static-is-told-apart-from-weak-signal)
- [Troubleshooting](#troubleshooting)
- [Building it yourself](#building-it-yourself)
- [Licences](#licences)

## Download

Go to **[Releases](../../releases/latest)**. The file names carry the version (for example `1.4.0`):

| File | What it is |
|---|---|
| **`FPV-DVR-Stitcher-<version>.exe`** | One file with everything built in. Download and run it. It takes a few seconds to open because it unpacks FFmpeg each time. |
| **`FPV-DVR-Stitcher-<version>-windows.zip`** | The same program with FFmpeg and the AI add-on as separate files. Starts faster. Unzip it and run `FPV DVR Stitcher.exe`, keeping the other files and the `ai` folder next to it. |
| `FPV-DVR-Stitcher-<version>-AI-addon.zip`, `model-*.onnx` | Downloaded by the program itself when needed; you don't need to download these. |

No install and no Python needed. Windows 10 or 11, 64-bit. Windows SmartScreen may warn the first
time because the .exe isn't code-signed: click **More info → Run anyway**.

The AI parts download themselves the first time they're used: the **AI add-on** (about 70 MB,
Real-ESRGAN and RIFE) for the built-in AI upscalers and smooth 60 fps, and each **model** (1-45 MB)
when you tick it. AI features need a graphics card with up-to-date drivers (Vulkan for the add-on,
DirectX 12 for the models); an NVIDIA RTX card is ideal.

## Quick start

1. **Add files...** (all the `VIDxxxxx.AVI` clips from the session) or **Add folder...**. Clips play
   in file-name order. You can also drag clips onto the .exe.
2. **1. Analyse.** A 30-minute session takes about a minute.
3. Check the timeline and the list. Press **Space** to play the result, drag a cut if you want it
   elsewhere.
4. **2. Export video...** for the original-quality video, and/or **3. Enhance video...** for a
   cleaned-up MP4 ready to upload.

## The main window

- **Timeline.** The top lane is the signal: green = good, orange = weak / breaking up (always
  kept), red = no signal (static), purple = post-flight STATS screen. The lower lane shows what goes
  into the export: blue = kept, grey = cut. ⚠ marks a signal that faded before it was lost. Flags
  above the lanes are chapters. The thin black line is the playhead (the frame in the preview).
- **List.** One row per part: start, end, length, Keep/Cut, what happened, and where it starts in
  the original files. Double-click a row (or press **X**) to switch it between Keep and Cut. For a
  loss of signal, selecting the row shows the last clear frame before it.
- **Preview.** A small player (see below).
- The summary next to the buttons shows the recorded length, the exported length, the number of
  flights and cuts.

## Editing, playing and chapters

**Preview player**
- **Space** or ▶ plays; **|◀ ▶|** step one frame (arrow keys too, Shift = 1 second).
- Speed **½× to 8×**. **As exported** plays only the kept parts, joined exactly like the export;
  **Original** plays the whole recording.
- **⤢** (or double-click the picture) opens a larger window.
- The list and the timeline follow the playback; a zoomed timeline scrolls along.

**Moving cuts**
- Select a part: a black box marks it on the timeline. **Drag the box's left or right edge**; the
  neighbouring part follows and the preview shows the frame at the cut. Afterwards the arrow keys
  nudge that cut by one frame (Shift: 1 second); Esc stops.
- Or **double-click a Start or End time** in the list and type it (like `14:35.200`), Enter to apply.
  Times outside what's possible are refused with the allowed range.
- **Mouse wheel** zooms the timeline around the mouse; **Shift+wheel** moves along it.

**Edit menu** (also on right-click, on the timeline or the list). Everything uses the playhead:

| Command | Key | |
|---|---|---|
| Set start here / Set end here | I / O | Move the selected part's start or end to the playhead |
| Split here | S | Split the part in two (then cut either half) |
| Cut this part / Keep this part | X | |
| Play around this cut | P | Plays 3 s either side of the nearest cut, as it will look in the export |
| Add chapter here | C | |
| Chapters... | | The Chapters window |
| Zoom to this part / Show the whole recording | | |
| Undo | Ctrl+Z | Every edit can be undone |
| Reset to detected cuts | | Back to what the analysis found (Undo brings your edits back) |

**Chapters.** Every flight gets a chapter automatically (green flags); your own are purple.
**Edit > Chapters...** lists them with their time in the exported video: rename any of them, delete
your own. Video sites read chapters only from the video's **description**, not from markers inside
the file, so **Copy chapter list** copies the list in that format (`00:00 Flight 1` ...) and the window
checks the usual rules: the first at 00:00, at least 3 chapters, each at least 10 s long. A chapter
placed in a part you cut moves to the next kept moment. MKV and enhanced MP4 exports get the chapter
markers, and every export saves the list next to the video as `..._chapters.txt`.

**Help > Keyboard shortcuts** lists all the keys.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Cut static longer than | 1 s | Only static runs at least this long are removed. Static of 5 s or more counts as the VTX being off (a new flight starts after it). |
| Keep static at each cut | 0.0 s | A short bit of static left either side of each cut as a visual break. |
| Static = snow covers at least | 95% | How much of the frame must be snow to count as no signal. Lower it if some static isn't cut. |
| Protect faint picture hidden in static | on | Static that still hides a steady picture (OSD, or a crashed quad seen through the noise) is kept as weak signal. |
| Trim post-flight STATS screen | on | Cuts the Betaflight statistics screen between disarming and unplugging. The layout is learned from the recording itself. |
| Output | AVI | AVI = same format as the originals. MKV = the same video plus chapter markers. |
| Also save signal-loss report | off | A `report.html` with every loss of signal, the last clear frame, and for suspicious losses the frames from 8 s before and a lossless clip of the last 20 seconds. |

Changing a setting re-runs the detection; your own cut changes are reset and **Undo** brings them back.

## Export video (lossless)

**2. Export video...** copies the kept frames into one AVI or MKV with FFmpeg stream copy: no
re-encoding, byte-identical frames, done in seconds. Use this export when looking for a crashed
quad: it shows exactly what the receiver recorded.

## Enhance video

![Enhance window](docs/enhance_window.png)

**3. Enhance video...** makes a cleaned-up MP4. Every option has a hover explanation (or click its
**?**) with what it does, its effect on quality and speed, and the recommended setting.

**Presets**

| Preset | What it does |
|---|---|
| Quick | No AI. Repair, colour-speckle clean-up, light denoise, sharpening, standard resize to 1440p. About real time. |
| **Balanced** (default) | Repair, the **BleedOut** clean-up model, then the **Fast** AI upscaler. The recommended choice for uploads. |
| Best quality | The same with the **Detailed** AI upscaler. Sharpest, slowest. |

Changing anything switches the preset to Custom.

**Repair breakup** (only touches places where the signal broke up; no change on clean footage)
- **Replace broken frames.** Frames that came out torn, rolled or blown out, while the frames either
  side are fine, are found by comparing each frame with its neighbours (only where the analysis saw
  weak signal) and replaced: redrawn as in-between frames with RIFE in AI / 60 fps exports, otherwise
  by the nearest good frame. Breakups longer than 6 frames are real and stay in, so a crash still shows.
- **Repair lines.** Short-lived streak lines and bands of rainbow colour noise across the picture are
  filled in from the rows above and below. Colour bands keep their brightness and detail; the OSD and
  anything that stays in place are left alone.

**Clean-up models** (every frame). Tick none, one or several; ticked models run one after another in
the order shown (the blue numbers), on the recording's own size, before anything else. See
[The AI models](#the-ai-models).

**Upscaling** (pick one)
- **No upscaling - keep the original size** (e.g. 1280×720). Repair, clean-up, denoise, sharpening and
  60 fps still apply. Doesn't need the AI add-on.
- **Standard resize** (no AI), **Fast** / **Detailed** (built-in AI, AI add-on), **General x4 - smooth**,
  **General x4 - keeps texture**, **VHS to HD x2** (downloaded when used).
- **AI resolution:** full size, or 1080p then a standard resize to the output size (about 45% less AI work).

**Picture:** clean colour speckles, grain denoise (Off / Light / Medium / Strong), sharpen,
**Smooth 60 fps** (RIFE; also in the Enhance menu) and **60 fps detail** (Standard or High-detail).

**Output:** 1080p / **1440p** (recommended: video sites encode 1440p uploads with more bitrate, even
for analog footage) / 4K, and the codec (Auto, HEVC, H.264, AV1). Encoding uses NVIDIA NVENC, AMD AMF
or Intel QSV when available, with CPU fallbacks.

**Speed** (no effect on quality): **work on several steps at once** (frames are prepared, AI-processed
and encoded in parallel), **prepare frames on all processor cores**, **denoise on the graphics card**
(Medium / Strong via Vulkan; fewer parts run at once while the AI programs work, so the card doesn't
run out of memory).

**Estimated time** shows how long the export will take and what runs on the processor and on the
graphics card. Every preview and export measures this PC's speed, so the estimate (and the time left
while exporting) becomes accurate; run one **Render 10 s preview** first.

**Compare frame...** opens a window with the moment shown in the main window's preview: the original
recording and your setup side by side. Tick more setups to add them (no clean-up, each clean-up model
on its own, any upscaler, no upscaling), switch between the middle of the picture and the whole
frame, and click a tile to use that setup.

**Render 10 s preview** renders 10 seconds from that moment and plays it. **Export enhanced video...**
renders the whole session; the MP4 gets the chapter markers and `..._chapters.txt` is saved next to it.

If an AI step fails on a batch (for example the graphics card ran out of memory), that batch is tried
once more after the encoder has finished; if it fails again the message says how the program ended
(crashed, out of memory, ...).

Enhanced footage is for sharing: AI can invent detail, so use the lossless export when looking for a
crashed quad.

## The AI models

There are no AI models trained on FPV drone footage. Analog FPV video has the same faults as
other composite / analog video (rainbow colour noise, colour bleeding, dot crawl, grain, interference
lines), so these are free community models made for analog / VHS restoration, plus the standard
Real-ESRGAN models. They're compared on real FPV frames before being added.

**How they run.** The built-in Fast / Detailed upscalers and RIFE run with
[ncnn](https://github.com/Tencent/ncnn) on Vulkan from the AI add-on. The downloadable models run
with [ONNX Runtime](https://onnxruntime.ai) and DirectML on any DirectX 12 graphics card (half
precision for speed, full precision as a fallback; on the processor if there's no usable card, which
is very slow). The build converts each model from its original PyTorch file to ONNX with
[spandrel](https://github.com/chaiNNer-org/spandrel) (`tools/convert_models.py`), checks the result
against PyTorch, and attaches `model-<name>.onnx` files to each release.

### Clean-up models (1x, every frame)

| Model | What it does | Best for | Architecture | Author, licence | Download |
|---|---|---|---|---|---|
| **Rainbows & colour bleed** ([1x-BleedOut-Compact](https://openmodeldb.info/models/1x-BleedOut-Compact)) | Removes rainbow colour noise, colour bleeding and colour smear of composite video; keeps detail | **Every analog FPV export (recommended)** | Real-ESRGAN Compact | zarxrax, WTFPL | 1.2 / 2.4 MB |
| **Dot crawl & rainbows** ([1x-Dotzilla-Compact](https://openmodeldb.info/models/1x-Dotzilla-Compact)) | Removes dot crawl (crawling dots along edges and around OSD text) and rainbow patterns | Footage where edges and the OSD crawl. Trained on animation: can soften fine texture a little | Real-ESRGAN Compact | zarxrax, WTFPL | 1.2 / 2.4 MB |
| **Heavy clean-up** ([1x-Archivist_AntiLines](https://github.com/Loganavter/Archivist-Project-Denoiser)) | Strong removal of analog noise, interference lines and compression blocks | Very noisy, breaking-up footage. Smooths detail (slightly painted look) | Real-ESRGAN Compact | Loganavter, MIT | 0.7 / 1.4 MB |
| **Heavy colour noise** ([1x-Archivist_RGB](https://github.com/Loganavter/Archivist-Project-Denoiser)) | Strong removal of colour noise | Heavy colour noise. Smooths detail like Heavy clean-up | Real-ESRGAN Compact | Loganavter, MIT | 0.7 / 1.4 MB |

### Upscalers

| Model | What it does | Best for | Architecture | Author, licence | Download |
|---|---|---|---|---|---|
| **Fast** (built-in, realesr-animevideov3) | Small x4 model made for video; clean, slightly smooth look | Most uploads | Real-ESRGAN Compact (ncnn) | Xintao Wang et al., BSD-3-Clause | AI add-on |
| **Detailed** (built-in, [realesrgan-x4plus](https://github.com/xinntao/Real-ESRGAN)) | The strongest general x4 model; sharpest detail | Your best flights | RRDB / ESRGAN (ncnn) | Xintao Wang et al., BSD-3-Clause | AI add-on |
| **General x4 - smooth** ([realesr-general-x4v3](https://github.com/xinntao/Real-ESRGAN)) | Real-ESRGAN's general model for real-world video; removes noise, smooth clean picture | Noisy footage you want clean | Real-ESRGAN Compact | Xintao Wang et al., BSD-3-Clause | 2.4 / 4.9 MB |
| **General x4 - keeps texture** (realesr-general-wdn-x4v3) | The same model with less noise removal: keeps grass, trees and gravel | Good-signal footage | Real-ESRGAN Compact | Xintao Wang et al., BSD-3-Clause | 2.4 / 4.9 MB |
| **VHS to HD x2** ([2x-VHS2HD-RealPLKSR](https://openmodeldb.info/models/2x-VHS2HD-RealPLKSR)) | Restores VHS / analog video at twice the size; keeps detail and a natural look | 720p HD-mode recordings exported at 1440p. Much slower than the others | RealPLKSR | asterixcool, CC-BY-SA-4.0 | 15 / 30 MB |

Download sizes are half / full precision; both are downloaded.

### Frame interpolation

| Model | What it does | Author, licence |
|---|---|---|
| **RIFE v4.6** ([rife-ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan), from [RIFE](https://github.com/hzwer/ECCV2022-RIFE)) | Makes in-between frames for **Smooth 60 fps** (the receiver records about 28 fps), and redraws replaced broken frames | Zhewei Huang et al. (RIFE), nihui (ncnn port); MIT |

## How static is told apart from weak signal

Measured on real analog DVR recordings:

| | Pure static (VTX off) | Weak signal | Good signal |
|---|---|---|---|
| Frame regions that are fine-grained noise | 92-100% | partial | partial (detailed scenes) |
| Colour | none | receiver drops it | yes |
| About 1 s of frames averaged together | averages away (ratio ~1.0) | scene and OSD survive (~2) | strong (>10) |

A frame counts as **no signal** only when snow covers almost all of it. Weak signal is marked by
colour loss, because analog receivers drop the colour when the signal gets weak. The averaging test
is a safety net: a picture or OSD hiding under heavy noise, such as a crashed quad in the grass with a
broken antenna, is kept even if every frame looks like snow. If the picture faded through weak signal
before turning to static, the loss gets a warning; an unplugged VTX goes from a clean picture straight
to static.

The STATS screen is found from a map of OSD-like text in each frame: the program learns the STATS
layout from the frames just before the power-offs in the recording (with a built-in Betaflight pattern
as a fallback) and trims the stretch where it is showing.

Long HD-mode files are analysed in parallel chunks, which gives exactly the same result as one pass.

## Troubleshooting

| Problem | What to do |
|---|---|
| "Could not find ffmpeg.exe" | Use the single-file .exe, or keep `ffmpeg.exe` and `ffprobe.exe` next to the program. |
| Static not cut / weak signal cut | Adjust **Static = snow covers at least**, or switch individual rows with a double-click. |
| Detection looks wrong on your footage | After Analyse, **Help > Save diagnostics...** saves one zip (usually 10-25 MB) with the detector's measurements for every frame and original frames from around each start and stop of the signal, no full videos. Share it to get the detector tuned to your receiver. |
| AI upscaling / 60 fps fails | Update the graphics card driver (Vulkan is needed). Turning off **Denoise on the graphics card** frees memory for the AI. The Quick preset and No upscaling need no AI add-on. |
| Clean-up model is very slow | It's running on the processor: the Enhance window says where models run. Update the graphics card driver (DirectX 12 is needed). |
| Model or add-on download fails | Check the internet connection; they come from this project's GitHub releases. |
| No chapters after uploading | Paste the chapter list (Edit > Chapters... > **Copy chapter list**, or `..._chapters.txt`) into the video's description. |

## Building it yourself

- `build.bat` (Windows, needs Python 3) builds `dist\FPV DVR Stitcher.exe` and downloads FFmpeg next
  to it. The AI add-on and models are downloaded by the app when first used.
- `run.bat` runs it straight from source.
- `python dvr_core.py VID*.AVI -o out.avi --report` is a command-line version. Add
  `--diagnostics diag.zip` (or `--dump frames.csv`) to save the measurements for tuning.
- `python tests/smoke_test.py` runs the end-to-end self-test. It builds a fake DVR session with
  static, flights, dropouts, STATS screens and a broken frame, then checks detection, chunked analysis,
  byte-identical export, editing, chapters, diagnostics, broken-frame replacement, the ONNX model path
  (with `pip install onnx`), No upscaling, comparison tiles and the enhance export.

Every push to `main` builds and tests everything on GitHub Actions (download the builds from the run's
**Artifacts**). To publish a version, create a release whose tag matches `APP_VERSION` in
`dvr_core.py` (for example `v1.4.0`); the build attaches the downloads and the models to it.

| File | Contents |
|---|---|
| `dvr_core.py` | Analysis, classification, STATS detection, editing, chapters, lossless export (FFmpeg stream copy), report, diagnostics |
| `fpv_dvr_stitcher.py` | The desktop app: timeline editing, menus, shortcuts, self-test |
| `player.py` | The preview player |
| `chapters_ui.py` | The Chapters window |
| `enhance.py` | Enhance video pipeline: repair stage, clean-up models, AI upscaling, 60 fps, GPU encoding, parallel stages, speed estimates, comparison tiles |
| `enhance_ui.py` | The Enhance video window and the Compare window |
| `repair.py` | Broken-frame replacement and line repair |
| `models.py` | AI model catalog, downloads, ONNX Runtime runner |
| `tools/convert_models.py` | Converts the catalog's models from PyTorch to ONNX (used by the build) |
| `tests/smoke_test.py` | End-to-end self-test |
| `.github/workflows/build.yml` | Model conversion, then the Windows build: test, build both .exe versions, assemble the AI add-on, test the .exe files (including a real model), attach to releases |

## Licences

The bundled `ffmpeg.exe` / `ffprobe.exe` come from [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)
(LGPL build); their licence is in the zip download as `FFMPEG-LICENSE.txt`. The AI add-on contains
[Real-ESRGAN ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN) (BSD-3-Clause),
[RIFE ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan) (MIT) and [ncnn](https://github.com/Tencent/ncnn)
(BSD-3-Clause). The program uses [ONNX Runtime](https://github.com/microsoft/onnxruntime) (MIT),
[NumPy](https://numpy.org) (BSD-3-Clause) and [Pillow](https://python-pillow.org) (MIT-CMU). The
downloadable models keep their authors' licences, listed in [The AI models](#the-ai-models) and in
`MODELS-NOTICE.txt` on each release.
