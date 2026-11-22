FPV DVR STITCHER
================

Joins the clips from your analog FPV goggles/receiver DVR into one video, cuts
out the dead static while the quad is powered off (battery swaps) and the
post-flight STATS screen, and keeps everything else - including weak,
breaking-up signal, which is what you need when looking for a crashed or lost
quad. Works with SD recordings (3-minute clips) and HD-mode recordings (one
long 720p file).

The normal export is NOT re-encoded: the frames in the output are byte-for-byte
the same frames as in your original files, so quality is identical. Audio is
removed. A 30-minute session takes roughly a minute to analyse and a few
seconds to export.

"Enhance for YouTube" is a separate export that cleans up the picture
(colour speckles, grain), upscales it (optionally with AI on the graphics
card), can smooth it to 60 fps, and saves a 1440p MP4 ready to upload.


GETTING IT (Windows 10/11)
--------------------------
Download from  https://github.com/ShadowStacker/FPV-Video-Editor/releases/latest
  FPV-DVR-Stitcher.exe          One file with everything built in - just run
                                it. Takes a few seconds to open. Downloads the
                                AI add-on (~70 MB) the first time you use AI.
  FPV-DVR-Stitcher-windows.zip  Same program, starts faster, AI add-on included.
                                Unzip and run "FPV DVR Stitcher.exe", keeping
                                the other files and the "ai" folder next to it.
No install or Python needed. You can make a desktop shortcut to the .exe.

Windows SmartScreen may warn the first time (the .exe isn't code-signed):
click "More info" -> "Run anyway".

Building it yourself instead: install Python 3 (python.org, tick "Add
python.exe to PATH"), then double-click build.bat in the source folder. It
creates dist\FPV DVR Stitcher.exe and downloads FFmpeg next to it.
run.bat starts the program straight from the source without building.


USING IT
--------
1. "Add files..." (select all the VIDxxxxx.AVI clips from the session) or
   "Add folder...". Clips play in file-name order. You can also drag clips
   onto the .exe icon.
2. Press "1. Analyse".
3. Check the timeline and the list:
     Signal lane:   green  = good signal
                    orange = weak / breaking-up signal (always kept)
                    red    = no signal (pure static)
                    purple = post-flight STATS screen
     Export lane:   blue   = goes into the output,  grey = cut out
     (warning sign) = the signal faded out before it was lost: out of range
                      or a crash - worth a look.
   Click anywhere on the timeline or on a row to see a preview frame. For a
   loss of signal the preview shows the last clear frame before it.
   Double-click a row (or press Space) to switch it between KEEP and CUT.
4. Press "2. Export video..." for the original-quality video, and/or
   "3. Enhance for YouTube..." for an upload-ready 1440p MP4.


SETTINGS
--------
Cut static longer than   Only static runs at least this long are removed.
  (default 1 s)          Shorter blips are kept. Static of 5 s or more counts
                         as the VTX being off (a new flight starts after it).
Keep static at each cut  A short bit of static left either side of each cut
  (default 0.0 s)        as a visual break between flights. 0 = hard cut.
Static = snow covers     How much of the frame must be snow for it to count
  at least (default 95%) as "no signal". Pure static measures ~100%. Lower it
                         if some static is not being cut.
Protect faint picture    Static that still hides a steady picture (OSD or a
  hidden in static       scene showing through the noise - e.g. a crashed
                         quad lying in grass with a damaged antenna) is
                         treated as weak signal and kept.
Trim post-flight STATS   Cuts the Betaflight statistics screen shown between
  screen (default on)    disarming and unplugging the battery. The program
                         learns what your STATS screen looks like from the
                         recording itself. Each one gets its own row in the
                         list, so you can keep any of them with a double-click.
Output AVI / MKV         AVI = same format as your originals.
                         MKV = same video, plus a chapter marker per flight
                         (VLC: Playback -> Chapter).
Signal-loss report       Saves a folder next to the video with report.html:
  (default off)          every loss of signal with its time in the original
                         files and in the exported video, the last clear frame,
                         and for suspicious losses the frames from 8 s before
                         plus a lossless clip of the last 20 seconds.


ENHANCE FOR YOUTUBE
-------------------
Open it with "3. Enhance for YouTube..." (or the Enhance menu) after Analyse.
Hover the mouse over any option (or click its ?) for what it does, how it
affects quality and speed, and the recommended setting.
Presets:
  Quick            No AI. Cleans colour speckles and grain, sharpens and
                   upscales to 1440p. About as fast as real time.
  Balanced         Same clean-up, then the Fast AI model on the graphics card.
  (default)        The recommended choice for YouTube.
  Best quality     The Detailed AI model: sharpest, several times slower.
Picture options:
  AI resolution    Full size (best), or 1080p then scaled up (~45% less AI work).
  Smooth 60 fps    AI frame interpolation (RIFE). Also in the Enhance menu.
  60 fps detail    Standard, or High-detail (better on very fast motion, slower).
Speed options (all on by default, no effect on quality):
  Work on several steps at once   prepares, AI-upscales and encodes in parallel
  Prepare frames on all cores     splits the preparation into parallel parts
  Denoise on the graphics card    Medium/Strong denoise on the GPU
Size: 1440p is recommended - YouTube encodes 1440p uploads better than 1080p.
The "Estimated time" panel shows how long the export will take. Each preview
and export measures this PC's speed, so the estimate gets accurate; run one
"Render 10 s preview" first. Your settings are remembered between runs.
Before a long export:
  Compare frame        before/after picture of the moment shown in the main
                       window's preview (whole frame + 100% detail).
  Render 10 s preview  renders 10 seconds from that moment and plays it.
The AI tools (Real-ESRGAN and RIFE) work on any modern NVIDIA, AMD or Intel
graphics card. Enhanced footage is for sharing - AI can invent detail, so use
the normal export when looking for a crashed quad.


HOW IT TELLS STATIC FROM WEAK SIGNAL
------------------------------------
- Pure static (VTX off) is fine-grained random noise across the WHOLE frame.
  Every region of the picture is noisy, and there is no colour.
- Weak signal tears and speckles, but parts of the frame still show the scene.
  Analog receivers drop the colour when the signal gets weak, so colour loss
  is what marks footage as weak signal.
- As a safety net, about a second of "static" frames is averaged together:
  random snow averages away to nothing, while a real picture (or the OSD)
  hidden under the noise survives the averaging.
- Normal power-off (battery swap) goes from a clean picture straight to snow.
  Flying out of range fades through weak signal first - those get the warning.


TROUBLESHOOTING
---------------
"Could not find ffmpeg.exe"  -> put ffmpeg.exe and ffprobe.exe in the same
                                folder as the program.
Static not cut / weak signal cut -> adjust "Static = snow covers at least",
                                or flip individual rows with a double-click.
AI upscaling / 60 fps fails  -> update your graphics card driver (the AI tools
                                need Vulkan support), or use the Quick preset.
Detection looks wrong on your footage -> after Analyse, use Help > Save
    diagnostics... and send the zip it makes (usually 10-25 MB). It holds the
    detector's measurements for every frame plus a selection of original
    frames around each start/stop of the signal - no full videos - so the
    detector can be tuned to your receiver.
