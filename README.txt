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

You can fine-tune every cut on the timeline (drag it, or type a time), play
the result in the preview, and add your own chapters, which go into MKV/MP4
exports and into a chapter list ready to paste into a video description.

"Enhance video" is a separate export that repairs broken frames and streaks
from breakup, cleans up the picture (rainbow colour noise, grain, optionally
with clean-up AI models), upscales it (optionally with AI on the graphics
card) or keeps the original size, can smooth it to 60 fps, and saves an MP4.


GETTING IT (Windows 10/11)
--------------------------
Download from  https://github.com/ShadowStacker/FPV-Video-Editor/releases/latest
(the file names carry the version, for example 1.5.1)
  FPV-DVR-Stitcher-1.5.1.exe          One file with everything built in - just
                                      run it. Takes a few seconds to open.
                                      Downloads the AI add-on (~70 MB) the
                                      first time you use AI.
  FPV-DVR-Stitcher-1.5.1-windows.zip  Same program, starts faster, AI add-on
                                      included.
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
   Double-click a row (or select it and press X) to switch it between KEEP
   and CUT.
4. Fine-tune the cuts if you like (see EDITING below).
5. Press "2. Export video..." for the original-quality video, and/or
   "3. Enhance video..." for a cleaned-up MP4 ready to upload.


EDITING AND PLAYING
-------------------
Preview player   Space (or the play button) plays the preview. |< and >| step
                 one frame. Speed: 1/2x to 8x. "As exported" plays only the kept
                 parts, joined like the export; "Original" plays the recording.
                 The larger-window button (or double-click the picture) opens a
                 bigger preview. The list and the timeline follow the playback.
Moving a cut     Select a row: a black box marks it on the timeline. Drag the
                 box's left or right edge; the preview shows the frame at the
                 cut, and the neighbouring part follows. After dragging, the
                 arrow keys move that cut by one frame (Shift: 1 second); Esc
                 stops. Or double-click a Start or End time in the list and type
                 a new one (like 14:35.200), then press Enter.
Zoom             Mouse wheel over the timeline zooms in around the mouse;
                 Shift+wheel moves sideways. Right-click > "Show the whole
                 recording" zooms back out.
Edit menu        Also on right-click (timeline or list). Uses the playhead (the
(right-click)    thin black line = the frame in the preview):
                   Set start here (I) / Set end here (O)   move the selected
                                                           part's start / end
                   Split here (S)          split the part in two
                   Cut / Keep this part (X)
                   Play around this cut (P) plays 3 s either side of the cut,
                                            as it will look in the export
                   Add chapter here (C), Chapters...
                   Zoom to this part, Show the whole recording
                   Undo (Ctrl+Z), Reset to detected cuts
Chapters         Every flight gets a chapter automatically (green flags above
                 the timeline); your own are purple. Edit > Chapters... lists
                 them with their time in the exported video: rename them, delete
                 your own, and "Copy chapter list" copies the list in the
                 format video sites read from a video's DESCRIPTION (they ignore
                 chapter markers inside the file). They need the first at
                 00:00, at least 3 chapters and each at least 10 s long; the
                 window says if any of that is not met. The same chapters go
                 into MKV and enhanced MP4 exports, and every export saves them
                 next to the video as ..._chapters.txt.
Changing a setting on the left re-runs the detection: your own cut changes
are reset, and Edit > Undo brings them back.


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
                         MKV = same video, plus the chapter markers (one per
                         flight + your own; VLC: Playback -> Chapter).
Save chapter list next  Saves ..._chapters.txt (ready to paste into a video
  to exports (default on) description) next to every export. Remembered.
Signal-loss report       Saves a folder next to the video with report.html:
  (default off)          every loss of signal with its time in the original
                         files and in the exported video, the last clear frame,
                         and for suspicious losses the frames from 8 s before
                         plus a lossless clip of the last 20 seconds.


ENHANCE VIDEO
-------------
Open it with "3. Enhance video..." (or the Enhance menu) after Analyse.
Hover the mouse over any option (or click its ?) for what it does, how it
affects quality and speed, and the recommended setting.
Presets:
  Quick            No AI. Repairs, cleans colour speckles and grain, sharpens
                   and upscales to 1440p. About as fast as real time.
  Balanced         Repair, the BleedOut clean-up model, then the Fast AI model
  (default)        on the graphics card. The recommended choice for uploads.
  Best quality     The same with the Detailed AI model: sharpest, slower.
Repair (built in; frames without damage are left exactly as they are):
  Replace broken frames  (only where the signal broke up) Frames that came out torn, rolled or blown out when the
                   signal broke up (while the frames either side are fine) are
                   replaced: redrawn as in-between frames with RIFE (AI add-on,
                   in AI or 60 fps exports) or by the nearest good frame. Only
                   spots where the analysis saw weak signal are checked, and
                   breakups longer than 6 frames stay in.
  Repair lines     (every frame) Rebuilds streak lines and bands of rainbow
                   colour noise, including lines that last several frames or
                   roll through the picture, from the frames before and after
                   (lined up with this frame first), or fills them from the
                   rows above and below when the neighbours have the line too.
                   Anything fixed on screen (OSD, black border) is left alone;
                   on a completely still picture a steady line is only fixed
                   for half a second (it may be part of the scene).
Clean-up models (run on every frame; tick any number, they run in order):
                     Rainbows & colour bleed (BleedOut) - recommended
                     Dot crawl & rainbows (Dotzilla)
                     Heavy clean-up (Archivist AntiLines) - smooths detail
                     Heavy colour noise (Archivist RGB) - smooths detail
                     Balanced clean-up (Archivist Medium) - milder
                     Dot crawl & NTSC colour (AzuMPEG)
                   There are no models made for FPV footage; these are free
                   community models made for analog / VHS video, which has the
                   same faults. Each is downloaded (1-4 MB) the first time it is
                   used, and runs on the graphics card (DirectX 12).
Upscaling (pick one):
  No upscaling     Keeps the recording's own size (e.g. 1280x720). Repair,
                   clean-up, denoise, sharpening and 60 fps still apply.
  Standard resize  Ordinary high-quality resize, no AI.
  AI models        Built-in (AI add-on): Fast, Detailed. Downloaded when used:
                   General x4 - smooth, General x4 - keeps texture (Real-ESRGAN),
                   VHS to HD x2 (best on 720p HD-mode recordings; slow),
                   Live action video x2 (fast, 720p -> 1440p; personal use
                   only), Photo x4 - fast, UltraSharp x4 (most detail;
                   personal use only). "Personal use only" models have
                   non-commercial licences: not for monetised videos.
  AI resolution    Full size (best), or 1080p then scaled up (~45% less AI work).
Picture options: colour speckles, grain denoise, sharpen, and
  Smooth 60 fps    AI frame interpolation (RIFE). Also in the Enhance menu.
  60 fps model     RIFE 4.26 (better on fast motion) or RIFE 4.6. If 4.26
                   fails on your PC, 4.6 takes over by itself. Older AI
                   add-ons don't have 4.26: the window offers an update.
  60 fps detail    Standard, or High-detail (better on very fast motion, slower).
Speed options (all on by default, no effect on quality):
  Work on several steps at once   prepares, AI-upscales and encodes in parallel
  Prepare frames on all cores     splits the preparation into parallel parts
  Denoise on the graphics card    Medium/Strong denoise on the GPU
Size: 1440p is recommended - video sites encode 1440p uploads better than 1080p.
The "Estimated time" panel shows how long the export will take. Each preview
and export measures this PC's speed, so the estimate gets accurate; run one
"Render 10 s preview" first. Your settings are remembered between runs.
Before a long export:
  Compare frame...     the moment shown in the main window's preview with
                       several setups side by side: the original, yours, and
                       any you add (each clean-up model on its own, other
                       upscalers, no upscaling). Click one to use it.
  Render 10 s preview  renders 10 seconds from that moment and plays it.
The AI tools (Real-ESRGAN and RIFE) work on any modern NVIDIA, AMD or Intel
graphics card, and so do the downloaded models (ONNX Runtime with DirectML).
Enhanced footage is for sharing - AI can invent detail, so use the normal
export when looking for a crashed quad. The finished MP4 has the chapter
markers, and the chapter list is saved next to it as ..._chapters.txt.


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
Clean-up model is very slow  -> it is running on the processor: the Enhance
                                window says where models run. Update the
                                graphics card driver (DirectX 12 is needed).
Model download fails         -> check the internet connection; the models come
                                from this program's GitHub releases page.
No chapters after uploading -> paste the chapter list (Edit > Chapters... >
                                Copy chapter list, or ..._chapters.txt) into the
                                video's description.
Detection looks wrong on your footage -> after Analyse, use Help > Save
    diagnostics... and send the zip it makes (usually 10-25 MB). It holds the
    detector's measurements for every frame plus a selection of original
    frames around each start/stop of the signal - no full videos - so the
    detector can be tuned to your receiver.
