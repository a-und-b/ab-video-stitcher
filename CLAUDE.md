# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that combines multiple video clips into one video, with two modes:

- **collage** (default): all clips play **simultaneously** in a packed grid; shorter clips loop (`-stream_loop -1`) until the longest finishes. Canvas size is selectable via `--canvas`: `4k` (3840×2160) or `5k` (5120×2160 ultrawide, default).
- **concat**: clips play **one after another** (linear), each scaled+padded to the largest clip's resolution.

Two entry points, both dependency-free (standard library only):

- [video_stitcher.py](video_stitcher.py) — the CLI and all the real logic (probing, layout, ffmpeg command building).
- [video_stitcher_gui.py](video_stitcher_gui.py) — a thin tkinter GUI that **imports** from `video_stitcher.py` (no logic duplication). Don't reimplement encoding logic here; add it to the CLI module and call it.

There is no package config, no test suite, no git — just these two scripts and some generated `stitched_4k_*.mp4` outputs.

## Running

```bash
python video_stitcher.py /path/to/folder                  # collage, auto layout → stitched_5k.mp4 (name follows --canvas)
python video_stitcher.py /path/to/folder -o out.mp4       # custom output
python video_stitcher.py /path/to/folder --cols 3         # force 3 columns (collage only)
python video_stitcher.py /path/to/folder --canvas 4k      # 3840x2160 collage (default: 5k)
python video_stitcher.py /path/to/folder --mode concat    # join clips end to end
python video_stitcher.py /path/to/folder --codec hevc     # force H.265 (default: auto)
python video_stitcher.py /path/to/folder --crf 18 --preset slow

python video_stitcher_gui.py                              # launch the GUI
```

Requires **Python 3.8+** and **ffmpeg/ffprobe on PATH** (both are invoked via `subprocess`). No third-party Python packages (tkinter ships with Python).

There is no automated test. To verify a change, run the script against a folder of clips and inspect the output video and the printed layout table. A folder of differing-size/duration clips is the useful case — it exercises layout packing (collage) and scale-to-largest padding (concat).

## Architecture (the pipeline)

`main()` runs in sequence:

1. **Discover & probe** — `discover_clips()` finds video files (`.mp4/.mov/.avi/.mkv`, case-variant suffixes) in the folder, then `probe_clip()` shells out to `ffprobe` per file to read width/height/duration into a `Clip`. Unreadable files are skipped with a warning, not fatal.
2. **Build the ffmpeg command** — branches on `--mode`:
   - **collage** → `compute_layout()` packs clips into a grid (see below), then `build_ffmpeg_cmd()` constructs one big `-filter_complex`: a black `color` base, one `scale`+`setsar` per cell, then a chain of `overlay` filters stacking each clip at its computed `(x, y)`. Trimmed to the **longest** clip's duration.
   - **concat** → target size = the largest clip by pixel area (even dims); `build_concat_cmd()` scales+pads every clip to that size (`force_original_aspect_ratio=decrease` + `pad` + `setsar=1` + `fps`) and joins them with the `concat` filter. Duration = **sum** of all clips.
3. **Encode** — a single `subprocess.run` executes the ffmpeg command.

Both modes are **no audio** (`-an`), and the whole render is **one ffmpeg process** — no intermediate files. Codec is chosen by `codec_output_args()` / `resolve_codec()` (see the H.264 gotcha below).

The GUI ([video_stitcher_gui.py](video_stitcher_gui.py)) reuses steps 1–2 directly, then runs ffmpeg in a **background thread**, streaming output to a log pane. The worker talks to the UI only through a `queue.Queue` drained by `root.after()` — the thread-safe tkinter pattern; don't touch widgets from the worker thread. ffmpeg progress uses `\r`, which the GUI normalizes to `\n` so the log scrolls.

## Layout algorithm — the core logic

`compute_layout()` is where the real work is. Key ideas to preserve when editing:

- It tries **every column count from 1..n**, lays clips into rows greedily (row breaks every `nc` clips), and picks the column count that **maximizes total filled pixels** (`fit_w * fit_h` summed). `--cols` bypasses the search.
- `compute_layout()` and `build_ffmpeg_cmd()` take `canvas_w`/`canvas_h` params (defaulting to the `5k` constants); `main()` resolves `--canvas` via the `CANVASES` dict and threads the chosen dimensions through both. Concat mode ignores the canvas entirely (it sizes to the largest clip).
- `_layout_rows()` gives each row a "natural height" of `canvas_w / sum(aspect ratios in row)` (the height at which the row's clips, placed side-by-side at equal height, exactly span the canvas width), then scales all rows so they fill `canvas_h`.
- Within a row, each clip's cell width is proportional to its aspect ratio; the clip is then fit inside its cell preserving aspect ratio (black bars fill leftover space).
- **Rounding is absorbed deliberately**: the last clip in a row takes all remaining width, and the last row takes all remaining height — don't "fix" these into even splits or you reintroduce gaps.
- `n == 1` is a special-cased centered single clip.

## Gotchas

- **Canvas sizes** live in the `CANVASES` dict (`4k` = 3840×2160, `5k` = 5120×2160). `5k` is the default (`DEFAULT_CANVAS`), preserving the original behavior; `CANVAS_W, CANVAS_H` are kept as back-compat default values for the function signatures. Add new sizes by extending the dict — `--canvas` choices and the GUI dropdown both read from it.
- **H.264 / macOS hardware-decode ceiling (the "plays then freezes" bug)**: macOS VideoToolbox hardware-decodes H.264 only up to ~Level 5.2 = **36864 macroblocks**. The 5k canvas (5120×2160 = 43200 MBs) exceeds this, so x264 tags it Level 6.0 and on a Mac it plays a couple seconds then freezes while the clock keeps moving (the decoder stalls). `resolve_codec()` handles this: `--codec auto` (default) keeps H.264 for frames within the limit (`h264_safe()`) and switches oversized frames to **HEVC**, which macOS decodes fine. HEVC in MP4 **must** be tagged `hvc1` (done in `codec_output_args`) or QuickTime/Finder won't play it. Don't "simplify" this back to always-libx264. `4k` stays H.264; `5k` becomes HEVC.
- **Even dimensions matter**: cell sizes are forced even before scaling because libx264/yuv420p require it. Keep this when changing the scale step.
- **`-thread_queue_size 512`** on every input is intentional (collage) — the default (8) starves frames when many streams decode at once, leaving most clips frozen on their first frame. Don't drop it.
- `overlay=...:shortest=0` keeps the output running to `max_duration` rather than ending at the shortest input.
- **concat requires uniform segments**: the `concat` filter only joins streams with identical dimensions, SAR, and frame rate — that's why every clip is scaled+padded to the target and gets `setsar=1` + `fps=…`. Don't remove the pad/setsar/fps normalization or concat will error or desync.
- `--cols` and `--canvas` are **collage-only**; the GUI disables both controls in concat mode.
