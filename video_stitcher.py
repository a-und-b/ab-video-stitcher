#!/usr/bin/env python3
"""
AB Video Stitcher — Combine multiple video clips into a single video.

Two modes:
  collage (default): All input clips play simultaneously. Shorter clips loop
    until the longest one finishes. The layout algorithm packs videos to fill
    as many pixels as possible, respecting each clip's native aspect ratio
    (black bars fill any remaining space). Canvas is selectable with --canvas:
    4k (3840x2160) or 5k (5120x2160, ultrawide; default).
  concat: Clips play one after another (linear). Each clip is scaled and
    padded to the resolution of the largest clip in the batch so they join
    seamlessly.

Requirements: Python 3.8+, ffmpeg on PATH.

Usage:
    python video_stitcher.py /path/to/folder                 # collage → stitched_5k.mp4
    python video_stitcher.py /path/to/folder -o out.mp4      # custom output name
    python video_stitcher.py /path/to/folder --cols 3        # force 3 columns
    python video_stitcher.py /path/to/folder --canvas 4k     # 3840x2160 preset
    python video_stitcher.py /path/to/folder --canvas 1920x1080  # custom size
    python video_stitcher.py /path/to/folder --mode concat   # join clips end to end
    python video_stitcher.py /path/to/folder --codec hevc    # force H.265

Codec note: the default 5k canvas (5120x2160) exceeds H.264's Mac
hardware-decode limit, so its output is tagged Level 6.0 and "plays then
freezes" in QuickTime. --codec auto (the default) switches such oversized
frames to HEVC, which macOS decodes fine; smaller frames stay on H.264.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


# ── Constants ────────────────────────────────────────────────────────────────
# Named collage canvas presets, selectable via --canvas. "4k" is true UHD 16:9;
# "5k" is the original ultrawide (WUHD) canvas. --canvas also accepts an
# explicit WxH (e.g. 1920x1080) — see parse_canvas().
CANVASES = {
    "720p":     (1280, 720),    # HD 16:9
    "1080p":    (1920, 1080),   # Full HD 16:9
    "1440p":    (2560, 1440),   # QHD 16:9
    "4k":       (3840, 2160),   # UHD 16:9
    "5k":       (5120, 2160),   # WUHD ultrawide (original default)
    "square":   (1080, 1080),   # 1:1 (Instagram feed)
    "vertical": (1080, 1920),   # 9:16 (Stories / Reels / TikTok)
}
DEFAULT_CANVAS = "5k"
CANVAS_W, CANVAS_H = CANVASES[DEFAULT_CANVAS]  # back-compat defaults


def parse_canvas(value: str) -> Tuple[str, int, int]:
    """Resolve a --canvas value to ``(label, width, height)``.

    Accepts either a named preset from ``CANVASES`` (e.g. ``5k``) or an explicit
    ``WxH`` string such as ``1920x1080`` / ``1920X1080``. The label is the preset
    name or the normalised ``WxH`` string, and is used for the default output
    filename. Raises ``ValueError`` on malformed input or non-positive / odd
    dimensions (libx264 + yuv420p require even dimensions).
    """
    key = value.strip().lower()
    if key in CANVASES:
        w, h = CANVASES[key]
        return key, w, h
    m = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", key)
    if not m:
        raise ValueError(
            f"invalid canvas {value!r}: use a preset "
            f"({', '.join(CANVASES)}) or a WxH size like 1920x1080")
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise ValueError(f"canvas dimensions must be positive: {value!r}")
    if w % 2 or h % 2:
        raise ValueError(
            f"canvas dimensions must be even (yuv420p needs it): {value!r}")
    return f"{w}x{h}", w, h


def _canvas_arg(value: str) -> Tuple[str, int, int]:
    """argparse adapter: turn parse_canvas' ValueError into a clean CLI error."""
    try:
        return parse_canvas(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))

# H.264 Level 5.2 caps a frame at 36864 macroblocks (e.g. 4096×2304). macOS
# VideoToolbox hardware-decodes H.264 only up to ~Level 5.2, so anything larger
# (notably the 5120×2160 ultrawide canvas → 43200 MBs) is tagged Level 6.0 and
# plays for a second then freezes on Macs. HEVC has no such ceiling here.
MAX_H264_MACROBLOCKS = 36864


def h264_safe(width: int, height: int) -> bool:
    """True if a width×height frame fits within H.264's Mac-decodable limit."""
    return math.ceil(width / 16) * math.ceil(height / 16) <= MAX_H264_MACROBLOCKS


def resolve_codec(codec: str, width: int, height: int) -> str:
    """Map the user's --codec choice to a concrete codec ('h264' or 'hevc').

    'auto' picks h264 when the frame is small enough for Mac hardware H.264
    decode, otherwise hevc.
    """
    if codec == "auto":
        return "h264" if h264_safe(width, height) else "hevc"
    return codec


def codec_output_args(codec: str, crf: int, preset: str) -> List[str]:
    """ffmpeg output args for the chosen video codec."""
    if codec == "hevc":
        # -tag:v hvc1 is required for QuickTime/Finder to play HEVC in MP4.
        return [
            "-c:v", "libx265",
            "-crf", str(crf),
            "-preset", preset,
            "-tag:v", "hvc1",
            "-pix_fmt", "yuv420p",
        ]
    return [
        "-c:v", "libx264",
        "-crf", str(crf),
        "-preset", preset,
        "-pix_fmt", "yuv420p",
    ]


# ── Data ─────────────────────────────────────────────────────────────────────
@dataclass
class Clip:
    path: Path
    width: int
    height: int
    duration: float  # seconds

    @property
    def aspect(self) -> float:
        return self.width / self.height


# ── Probe helpers ────────────────────────────────────────────────────────────
def probe_clip(path: Path) -> Clip:
    """Use ffprobe to get width, height and duration of a video file."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)

    # Find the first video stream
    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video":
            w = int(stream["width"])
            h = int(stream["height"])
            # Duration: prefer stream duration, fall back to format duration
            dur = float(
                stream.get("duration")
                or info.get("format", {}).get("duration", "0")
            )
            return Clip(path=path, width=w, height=h, duration=dur)

    raise ValueError(f"No video stream found in {path}")


def discover_clips(folder: Path) -> List[Clip]:
    """Find all MP4 files in *folder* and probe them."""
    extensions = {".mp4", ".MP4", ".mov", ".MOV", ".avi", ".AVI", ".mkv", ".MKV"}
    files = sorted(
        p for p in folder.iterdir()
        if p.suffix in extensions and p.is_file()
    )
    if not files:
        sys.exit(f"Error: no video files found in {folder}")

    clips: List[Clip] = []
    for f in files:
        try:
            clips.append(probe_clip(f))
            print(f"  Found: {f.name}  ({clips[-1].width}x{clips[-1].height}, "
                  f"{clips[-1].duration:.1f}s)")
        except Exception as e:
            print(f"  Skipping {f.name}: {e}", file=sys.stderr)
    if not clips:
        sys.exit("Error: could not read any video files.")
    return clips


def colliding_input(output: Path, clips: List[Clip]) -> "Path | None":
    """Return the input clip path that *output* would overwrite in place, if any.

    ffmpeg cannot read from and write to the same file ("cannot edit in-place"),
    so writing the output on top of one of its own inputs is always fatal — no
    --force can rescue it. Compared by resolved absolute path.
    """
    out = output.resolve()
    for c in clips:
        if c.path.resolve() == out:
            return c.path
    return None


# ── Layout algorithm ─────────────────────────────────────────────────────────
@dataclass
class Cell:
    """A positioned rectangle on the 4K canvas."""
    x: int
    y: int
    w: int
    h: int
    clip_idx: int  # index into the clips list


def compute_layout(
    clips: List[Clip],
    cols: int | None = None,
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
) -> List[Cell]:
    """
    Compute an optimal grid layout for *clips* on a canvas_w × canvas_h canvas.

    Strategy:
    - Try every possible column count from 1..n.
    - For each column count, assign clips to rows greedily.
    - Within a row, clips share the same display height; each clip's
      width is proportional to its aspect ratio so it fills the row edge
      to edge with no wasted horizontal space.
    - The row height is derived from the row's combined aspect ratios so
      clips genuinely fill the available area (not just split evenly).
    - Pick the column count that maximises total filled pixels.
    - If *cols* is given, use that directly.
    """
    n = len(clips)

    if n == 1:
        # Single clip: centre it, preserving aspect ratio
        c = clips[0]
        scale = min(canvas_w / c.width, canvas_h / c.height)
        w = int(c.width * scale)
        h = int(c.height * scale)
        x = (canvas_w - w) // 2
        y = (canvas_h - h) // 2
        return [Cell(x, y, w, h, 0)]

    def _layout_rows(rows: List[List[int]]) -> Tuple[List[Cell], int]:
        """
        Given a list of rows (each row = list of clip indices), compute
        optimal row heights and positions.

        Each row's natural height is canvas_w / sum_of_aspect_ratios.
        We then scale all rows proportionally so they fill the canvas height.
        """
        # Compute natural (unconstrained) height for each row
        natural_heights: List[float] = []
        for r_clips in rows:
            row_aspect_sum = sum(clips[i].aspect for i in r_clips)
            # If all clips in this row were placed side-by-side at the same
            # height h, total width = h * row_aspect_sum. For that to equal
            # canvas_w: h = canvas_w / row_aspect_sum
            natural_heights.append(canvas_w / row_aspect_sum)

        total_natural = sum(natural_heights)
        scale = canvas_h / total_natural  # scale to fill canvas vertically

        cells: List[Cell] = []
        total_pixels = 0
        y_cursor = 0

        for r_idx, r_clips in enumerate(rows):
            if r_idx < len(rows) - 1:
                row_h = int(natural_heights[r_idx] * scale)
            else:
                row_h = canvas_h - y_cursor  # last row absorbs rounding

            aspects = [clips[i].aspect for i in r_clips]
            total_aspect = sum(aspects)

            x_cursor = 0
            for j, ci in enumerate(r_clips):
                if j == len(r_clips) - 1:
                    cell_w = canvas_w - x_cursor
                else:
                    cell_w = int(canvas_w * (aspects[j] / total_aspect))

                # Fit clip inside cell preserving its aspect ratio
                clip_ar = aspects[j]
                cell_ar = cell_w / row_h if row_h > 0 else 1

                if clip_ar > cell_ar:
                    fit_w = cell_w
                    fit_h = int(cell_w / clip_ar)
                else:
                    fit_h = row_h
                    fit_w = int(row_h * clip_ar)

                cx = x_cursor + (cell_w - fit_w) // 2
                cy = y_cursor + (row_h - fit_h) // 2

                cells.append(Cell(cx, cy, fit_w, fit_h, ci))
                total_pixels += fit_w * fit_h
                x_cursor += cell_w

            y_cursor += row_h

        return cells, total_pixels

    def layout_for_cols(nc: int) -> Tuple[List[Cell], int]:
        """Return (cells, total_filled_pixels) for a given number of columns."""
        rows: List[List[int]] = []
        row: List[int] = []
        for i in range(n):
            row.append(i)
            if len(row) == nc:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return _layout_rows(rows)

    if cols is not None:
        best_cells, _ = layout_for_cols(cols)
    else:
        best_cells: List[Cell] = []
        best_pixels = 0
        for nc in range(1, n + 1):
            cells, px = layout_for_cols(nc)
            if px > best_pixels:
                best_pixels = px
                best_cells = cells
    return best_cells


# ── FFmpeg command builder ───────────────────────────────────────────────────
def build_ffmpeg_cmd(
    clips: List[Clip],
    cells: List[Cell],
    output: Path,
    max_duration: float,
    crf: int = 18,
    preset: str = "medium",
    canvas_w: int = CANVAS_W,
    canvas_h: int = CANVAS_H,
    codec: str = "h264",
) -> List[str]:
    """
    Build a single ffmpeg command that:
    - Reads every clip (with -stream_loop -1 for looping)
    - Scales each to its cell size
    - Overlays them all onto a black canvas_w × canvas_h base
    - Trims to *max_duration*
    - Outputs with no audio (*codec*: 'h264' or 'hevc')
    """
    cmd: List[str] = ["ffmpeg", "-y"]

    # ── Inputs ────────────────────────────────────────────────────────────
    # -thread_queue_size 512: prevents frame starvation when many streams
    # are decoded simultaneously (default queue of 8 is too small for 10+
    # inputs, causing only the first few frames to actually move).
    for clip in clips:
        cmd += ["-thread_queue_size", "512", "-stream_loop", "-1", "-i", str(clip.path)]

    # ── Filter complex ────────────────────────────────────────────────────
    filters: List[str] = []

    # Base: black canvas
    filters.append(
        f"color=c=black:s={canvas_w}x{canvas_h}:d={max_duration}:r=30[base]"
    )

    # Scale each input to its exact cell size (we already computed
    # aspect-correct dimensions in the layout step, so just scale directly).
    # Ensure even dimensions for h264 compatibility.
    for i, cell in enumerate(cells):
        ci = cell.clip_idx
        # Make dimensions even (required by libx264 / most codecs)
        sw = cell.w if cell.w % 2 == 0 else cell.w - 1
        sh = cell.h if cell.h % 2 == 0 else cell.h - 1
        filters.append(
            f"[{ci}:v]scale={sw}:{sh}:force_original_aspect_ratio=disable,"
            f"setsar=1[v{i}]"
        )

    # Chain overlays
    prev = "base"
    for i, cell in enumerate(cells):
        out_label = f"tmp{i}" if i < len(cells) - 1 else "out"
        filters.append(
            f"[{prev}][v{i}]overlay=x={cell.x}:y={cell.y}:shortest=0[{out_label}]"
        )
        prev = out_label

    filter_str = ";\n".join(filters)
    cmd += ["-filter_complex", filter_str]

    # ── Output options ────────────────────────────────────────────────────
    cmd += [
        "-map", "[out]",
        "-an",                          # no audio
        "-t", str(max_duration),
    ]
    cmd += codec_output_args(codec, crf, preset)
    cmd += ["-movflags", "+faststart", str(output)]
    return cmd


def build_concat_cmd(
    clips: List[Clip],
    output: Path,
    target_w: int,
    target_h: int,
    crf: int = 18,
    preset: str = "medium",
    fps: int = 30,
    codec: str = "h264",
) -> List[str]:
    """
    Build a single ffmpeg command that concatenates *clips* end to end
    (linear mode): clip 1 plays, then clip 2, etc.

    Every clip is scaled to fit inside target_w x target_h (preserving its
    aspect ratio) and padded with black bars to exactly that size. The
    concat filter requires all segments to share identical dimensions, SAR
    and frame rate, so we normalise every clip the same way before joining.
    Outputs with no audio (matching the collage mode).
    """
    cmd: List[str] = ["ffmpeg", "-y"]

    for clip in clips:
        cmd += ["-i", str(clip.path)]

    filters: List[str] = []

    # Normalise each clip to the exact target size (scale-to-fit + pad).
    for i in range(len(clips)):
        filters.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps}[v{i}]"
        )

    # Join them in order.
    concat_inputs = "".join(f"[v{i}]" for i in range(len(clips)))
    filters.append(f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[out]")

    filter_str = ";\n".join(filters)
    cmd += ["-filter_complex", filter_str]

    cmd += [
        "-map", "[out]",
        "-an",                          # no audio
    ]
    cmd += codec_output_args(codec, crf, preset)
    cmd += ["-movflags", "+faststart", str(output)]
    return cmd


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stitch multiple video clips into a single video "
                    "(collage grid or linear concat).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "folder",
        type=Path,
        help="Folder containing input video files (.mp4, .mov, .avi, .mkv)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="Output file path (default: stitched_<canvas>.mp4 for collage, "
             "e.g. stitched_5k.mp4; stitched.mp4 for concat)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="collage",
        choices=["collage", "concat"],
        help="collage: all clips play at once in a grid (default). "
             "concat: clips play one after another, scaled/padded to the "
             "largest clip's resolution.",
    )
    parser.add_argument(
        "--canvas",
        type=_canvas_arg,
        default=DEFAULT_CANVAS,
        metavar="SIZE",
        help="Collage canvas size (collage mode only): a preset "
             "(4k = 3840x2160 UHD, 5k = 5120x2160 ultrawide) or an explicit "
             f"WxH like 1920x1080. Default: {DEFAULT_CANVAS}.",
    )
    parser.add_argument(
        "--cols",
        type=int,
        default=None,
        help="Force a specific number of columns (collage mode only; "
             "default: auto-optimise)",
    )
    parser.add_argument(
        "--codec",
        type=str,
        default="auto",
        choices=["auto", "h264", "hevc"],
        help="Video codec. auto (default): h264 for normal sizes, hevc when "
             "the frame is too large for Mac H.264 hardware decode (e.g. the "
             "5k canvas). hevc fixes 'plays then freezes' on macOS; h264 is "
             "the most universally compatible.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        help="CRF quality (lower=better; ~18 for h264, ~23 for hevc, default: 18)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default="medium",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"],
        help="x264 encoding preset (default: medium)",
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Overwrite the output file if it already exists "
             "(by default an existing output aborts the run).",
    )
    args = parser.parse_args()

    if not args.folder.is_dir():
        sys.exit(f"Error: {args.folder} is not a directory.")

    print(f"\n🎬 AB Video Stitcher ({args.mode}) — scanning {args.folder}\n")
    clips = discover_clips(args.folder)

    if args.mode == "concat":
        # Concat has no canvas, so the default name carries no size suffix.
        output = args.output or Path("stitched.mp4")
        # Linear: clips play one after another. Target size = the largest
        # clip in the batch (by pixel area), rounded down to even dims.
        biggest = max(clips, key=lambda c: c.width * c.height)
        target_w = biggest.width - (biggest.width % 2)
        target_h = biggest.height - (biggest.height % 2)
        total_duration = sum(c.duration for c in clips)

        print(f"\n  {len(clips)} clips found. "
              f"Target size: {target_w}x{target_h} (largest: {biggest.path.name})")
        print(f"\n  Order ({total_duration:.1f}s total):")
        for clip in clips:
            print(f"    [{clip.path.name}]  {clip.width}x{clip.height} "
                  f"→ {target_w}x{target_h}  ({clip.duration:.1f}s)")

        codec = resolve_codec(args.codec, target_w, target_h)
        cmd = build_concat_cmd(clips, output, target_w, target_h,
                               crf=args.crf, preset=args.preset, codec=codec)
        out_w, out_h, out_duration = target_w, target_h, total_duration
    else:
        canvas_label, canvas_w, canvas_h = args.canvas
        # Default name carries the canvas size so different canvases don't collide.
        output = args.output or Path(f"stitched_{canvas_label}.mp4")
        max_duration = max(c.duration for c in clips)
        print(f"\n  {len(clips)} clips found. Longest: {max_duration:.1f}s")

        # Compute layout
        cells = compute_layout(clips, cols=args.cols,
                               canvas_w=canvas_w, canvas_h=canvas_h)
        print(f"\n  Layout ({canvas_label}: {canvas_w}x{canvas_h}):")
        for cell in cells:
            clip = clips[cell.clip_idx]
            print(f"    [{clip.path.name}]  → {cell.w}x{cell.h} @ ({cell.x},{cell.y})")

        codec = resolve_codec(args.codec, canvas_w, canvas_h)
        cmd = build_ffmpeg_cmd(clips, cells, output, max_duration,
                               crf=args.crf, preset=args.preset,
                               canvas_w=canvas_w, canvas_h=canvas_h, codec=codec)
        out_w, out_h, out_duration = canvas_w, canvas_h, max_duration

    # Guard against clobbering: an output that is also an input is always fatal;
    # an existing output needs --force.
    clash = colliding_input(output, clips)
    if clash is not None:
        sys.exit(f"Error: output {output} is also an input clip ({clash.name}); "
                 f"ffmpeg can't write over a file it's reading. "
                 f"Choose a different name or output folder.")
    if output.exists() and not args.force:
        sys.exit(f"Error: {output} already exists. Pass --force to overwrite, "
                 f"or choose another name with -o.")

    codec_note = ""
    if args.codec == "auto" and codec == "hevc":
        codec_note = "  (auto-selected: too large for Mac H.264 hardware decode)"
    print(f"\n  Codec: {codec}{codec_note}")
    print(f"  Encoding to {output} …\n")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ ffmpeg failed (exit code {e.returncode}).", file=sys.stderr)
        print("  Tip: run with -v to see ffmpeg output.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        sys.exit("Error: ffmpeg not found. Please install ffmpeg and try again.")

    print(f"\n✅ Done! Output: {output}")
    print(f"   Resolution: {out_w}x{out_h}")
    print(f"   Codec:      {codec}")
    print(f"   Duration:   {out_duration:.1f}s")
    print(f"   Clips:      {len(clips)}")


if __name__ == "__main__":
    main()
