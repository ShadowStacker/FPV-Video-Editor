FPV DVR STITCHER
================

Joins the 3-minute clips from your analog FPV goggles/receiver DVR into one
video, cuts out the dead static while the quad is powered off (battery swaps),
and keeps everything else - including weak, breaking-up signal, which is what
you need when looking for a crashed or lost quad.

The video is NOT re-encoded: the frames in the output are byte-for-byte the
same frames as in your original files, so quality is identical. Audio is
removed. A 30-minute session takes roughly a minute to analyse and a few
seconds to export.


GETTING IT (Windows 10/11)
--------------------------
Download from  https://github.com/ShadowStacker/FPV-Video-Editor/releases/latest
  FPV-DVR-Stitcher.exe          One file with everything built in - just run
                                it. Takes a few seconds to open.
  FPV-DVR-Stitcher-windows.zip  Same program, starts faster. Unzip and run
                                "FPV DVR Stitcher.exe", keeping ffmpeg.exe and
                                ffprobe.exe in the same folder as it.
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
     Export lane:   blue   = goes into the output,  grey = cut out
     (warning sign) = the signal faded out before it was lost: out of range
                      or a crash - worth a look.
   Click anywhere on the timeline or on a row to see a preview frame. For a
   loss of signal the preview shows the last clear frame before it.
   Double-click a row (or press Space) to switch it between KEEP and CUT.
4. Press "2. Export video..." and choose where to save.


SETTINGS
--------
Cut static longer than   Only static runs at least this long are removed.
  (default 5 s)          Shorter dropouts in the middle of a flight are kept
                         so the flight plays continuously.
Keep static at each cut  A short bit of static left either side of each cut
  (default 0.5 s)        as a visual break between flights. 0 = hard cut.
Static = snow covers     How much of the frame must be snow for it to count
  at least (default 85%) as "no signal". Pure static measures ~100%, weak
                         signal ~35% or less. Lower it if some static is not
                         being cut; raise it if weak signal gets marked red.
Protect faint picture    Static that still hides a steady picture (OSD or a
  hidden in static       scene showing through the noise - e.g. a crashed
                         quad lying in grass with a damaged antenna) is
                         treated as weak signal and kept.
Output AVI / MKV         AVI = same format as your originals.
                         MKV = same video, plus a chapter marker per flight
                         (VLC: Playback -> Chapter).
Signal-loss report       Saves a folder next to the video with report.html:
                         every loss of signal with its time in the original
                         files and in the exported video, the last clear frame,
                         and for suspicious losses the frames from 8 s before
                         plus a lossless clip of the last 20 seconds.


HOW IT TELLS STATIC FROM WEAK SIGNAL
------------------------------------
- Pure static (VTX off) is fine-grained random noise across the WHOLE frame.
  Every region of the picture is noisy.
- Weak signal tears and speckles, but parts of the frame still show the scene,
  and the Betaflight OSD stays in the same place. Horizontal tearing and
  partly-snowy frames are what get it labelled as weak signal.
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
For tuning on your own footage, the command-line version can dump the
per-frame measurements:
    python dvr_core.py VID00001.AVI VID00002.AVI --dump measurements.csv
