# FPV DVR Stitcher

A Windows app for **analog FPV DVR footage**. Pick the clips your goggles or handheld
receiver recorded during a session (SD 3-minute clips or one long HD-mode file), and it:

- **joins them into one video**, in order, with no seams between clips;
- **cuts out the pure static** recorded while the quad was powered off
  (battery swaps, before the first pack, after the last one);
- **trims the post-flight Betaflight STATS screen** shown between disarming and
  unplugging the battery;
- **keeps weak, breaking-up signal**, which looks different from dead static
  and is exactly what you need when looking for a crashed or lost quad;
- **flags suspicious signal losses** (signal faded before it was lost = out of
  range or crash) and can save a report with the last clear frame, the frames
  before it, and a lossless clip of the last 20 seconds;
- **keeps the original quality**: frames are copied byte-for-byte, never
  re-encoded. Audio is removed;
- **Enhance for YouTube**: optional second export that cleans up colour speckles
  and grain, upscales to 1440p (standard or AI with Real-ESRGAN on the graphics
  card), can smooth to 60 fps (RIFE), and encodes on the GPU, with every stage
  running in parallel where it can.

![Screenshot](docs/screenshot.png)

## Download

Go to **[Releases](../../releases/latest)** and download:

- **`FPV-DVR-Stitcher.exe`**: a single file with everything built in. Download it and run it.
  It takes a few seconds to open because it unpacks FFmpeg each time. The AI add-on
  (about 70 MB) is downloaded the first time you use AI upscaling or 60 fps.
- **`FPV-DVR-Stitcher-windows.zip`**: the same program with FFmpeg and the AI add-on as
  separate files. It starts faster. Unzip it and run `FPV DVR Stitcher.exe`.

No install and no Python needed. Windows SmartScreen may warn the first time because
the .exe isn't code-signed: click **More info -> Run anyway**.

Every push to `main` also builds everything automatically. The builds can be downloaded
from the **Artifacts** section of the latest run on the [Actions](../../actions) tab (you
need to be signed in to GitHub). To publish a new version, go to
**Releases -> Draft a new release**, create a new tag such as `v1.1.0` and click
**Publish**. The build attaches the downloads to the release within a few minutes.

## Using it

1. **Add files...** (all the `VIDxxxxx.AVI` clips from the session) or **Add folder...**.
   Clips play in file-name order. You can also drag clips onto the .exe.
2. **Analyse**. A 30-minute session takes about a minute.
3. Check the timeline. Green is good signal, orange is weak signal (always kept),
   red is static and purple is the STATS screen. The lower lane shows what goes into
   the export. Click the timeline or a row to preview a frame, and double-click a row
   to switch it between Keep and Cut.
4. **Export video...** as AVI (same format as the originals) or MKV (adds a chapter
   marker for each flight), and/or **Enhance for YouTube...** for an upload-ready MP4.

Defaults: cut static longer than 1 s, keep 0.0 s of static at each cut, static = 95%
snow coverage, faint-picture protection on, STATS trim on, AVI output, report off.
See [README.txt](README.txt) for what each setting does.

If detection looks wrong on your footage, use **Help > Save diagnostics...** after
analysing. It saves one zip, usually 10-25 MB, containing the detector's measurements for
every frame and a selection of original frames from around each start and stop of the
signal. Sharing that zip is enough to tune the detector to your receiver.

## Enhance for YouTube

![Enhance window](docs/enhance_window.png)

| Preset | What it does |
|---|---|
| Quick | No AI. Colour-speckle clean-up, light denoise, sharpening, standard upscale to 1440p. About real time. |
| Balanced (default) | Same clean-up, then the **Fast** AI model (realesr-animevideov3) on the graphics card. Several times faster than Best quality. |
| Best quality | The **Detailed** AI model (Real-ESRGAN x4plus). Sharpest, slowest. |

Every option has a hover explanation (or click its **?**) with its quality and speed trade-off and the
recommended setting. Options:

- **Picture:** colour speckles, grain denoise (Off/Light/Medium/Strong), sharpen, AI or standard upscaling,
  AI model, **AI resolution** (full size, or 1080p then scaled up: about 45% less AI work),
  **Smooth 60 fps** (RIFE; also in the Enhance menu) and **60 fps detail** (Standard or the slower High-detail).
- **Speed:** **work on several steps at once** (frames are prepared, AI-upscaled and encoded in parallel),
  **prepare frames on all processor cores** (also splits the Quick export into parallel parts), and
  **denoise on the graphics card** (Medium/Strong via Vulkan, with automatic fallback to the processor).
- **Output:** 1080p / 1440p (recommended: YouTube encodes 1440p uploads better) / 4K, and the video codec.
  Encoding uses NVIDIA NVENC, AMD AMF or Intel QSV when available, with CPU fallbacks.

The **estimated time** panel shows how long the export will take and what runs on the processor and the
graphics card. The speed of each stage is measured on every preview and export and remembered, so the
estimate (and the time left while exporting) fits the PC. **Compare frame** shows a before/after picture;
**Render 10 s preview** renders a short sample (and measures the speed). Settings are remembered between runs.

## How static is told apart from weak signal

Measured on real analog DVR recordings:

| | Pure static (VTX off) | Weak signal | Good signal |
|---|---|---|---|
| Frame regions that are fine-grained noise | 92-100% | partial | partial (detailed scenes) |
| Colour | none | receiver drops it | yes |
| About 1 s of frames averaged together | averages away (ratio ~1.0) | scene and OSD survive (~2) | strong (>10) |

A frame counts as **no signal** only when snow covers almost all of it. Weak signal is
marked by colour loss, because analog receivers drop the colour when the signal gets
weak. The averaging test is a safety net: a picture or OSD hiding under heavy noise, such
as a crashed quad in the grass with a broken antenna, is kept even if every frame looks
like snow. If the picture faded through weak signal before turning to static, the loss
gets a warning; an unplugged VTX goes from a clean picture straight to static.

The STATS screen is found from a map of OSD-like text in each frame: the program learns
the STATS layout from the frames just before the power-offs in the recording (with a
built-in Betaflight pattern as a fallback) and trims the stretch where it is showing.

## Building it yourself

- `build.bat` (Windows, needs Python 3) builds `dist\FPV DVR Stitcher.exe` and
  downloads FFmpeg next to it. The AI add-on is downloaded by the app when first used.
- `run.bat` runs it straight from source.
- `python dvr_core.py VID*.AVI -o out.avi --report` is a command-line version.
  Add `--diagnostics diag.zip` (or `--dump frames.csv`) to save the measurements for tuning.
- `python tests/smoke_test.py` runs the end-to-end self-test. It builds a fake DVR
  session with static, flights, dropouts and STATS screens, then checks detection,
  chunked analysis, byte-identical export, diagnostics and the enhance export.

Files:

| File | Contents |
|---|---|
| `dvr_core.py` | Analysis, classification, STATS detection, lossless export (FFmpeg stream copy), report, diagnostics |
| `enhance.py` | Enhance for YouTube pipeline (filters, AI upscale, 60 fps, GPU encoding, parallel/overlapped stages, speed estimates) |
| `fpv_dvr_stitcher.py` | The Tkinter desktop app |
| `enhance_ui.py` | The Enhance for YouTube window (hover help, estimate panel) |
| `tests/smoke_test.py` | End-to-end self-test |
| `.github/workflows/build.yml` | Windows build: test, build both .exe versions, assemble the AI add-on, test the .exe files, attach to releases |

The bundled `ffmpeg.exe`/`ffprobe.exe` come from
[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (LGPL build); their license
is included in the zip download as `FFMPEG-LICENSE.txt`. The AI add-on contains
[Real-ESRGAN ncnn-vulkan](https://github.com/xinntao/Real-ESRGAN) (BSD-3-Clause) and
[RIFE ncnn-vulkan](https://github.com/nihui/rife-ncnn-vulkan) (MIT).
