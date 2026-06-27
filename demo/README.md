# Demo assets

Everything here is **self-contained and copyright-free** — the sample clips are
synthesised (solid colour + a generated label), so nothing needs licensing.

There are two kinds of demo material you might want for the project README:

1. **Output-preview GIF** — a short looping preview of the actual collage. The
   committed [`../assets/collage-demo.gif`](../assets/collage-demo.gif) was made
   this way; regenerate it with the steps below.
2. **Terminal GIF** — shows the tool running and printing its layout table.

## 0. Generate the sample clips (once)

```bash
cd demo
./make_samples.sh          # writes clips/ : 7 colour-coded, labelled clips
```

Each clip is a distinct solid colour with a moving label showing its source
resolution, so you can tell them apart in the collage. The seven aspect ratios
(wide, square, portrait, ultrawide) are what push the layout into **two rows**.

The label text needs either an ffmpeg built with `drawtext` (libfreetype) or
**ImageMagick** (`magick`/`convert`) on `PATH`; the script auto-detects and
falls back to plain colour fields if neither is present.

## 1. Output-preview GIF — via ffmpeg

Turn a finished collage into a small looping GIF for the README. (Uses a
two-pass palette for clean colours and scales down.) This is exactly how the
committed `../assets/collage-demo.gif` was produced:

```bash
# produce a collage first
python ../video_stitcher.py clips/ -o /tmp/collage.mp4 --preset ultrafast

# then: 13 fps, 960px wide, good palette
ffmpeg -y -i /tmp/collage.mp4 \
  -vf "fps=13,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer" \
  -loop 0 ../assets/collage-demo.gif
```

## 2. Terminal GIF — via VHS

[VHS](https://github.com/charmbracelet/vhs) records a scripted terminal session
straight to a GIF, so the result is reproducible — no manual screen capture.

```bash
brew install vhs          # one-time
cd demo
vhs demo.tape             # reads demo.tape → writes demo.gif
```

The recording flow lives in [`demo.tape`](demo.tape); tweak the `Sleep` timings
or commands there.

## Want real-looking footage instead?

The Blender open movies (Big Buck Bunny, Sintel, Tears of Steel, …) are
**CC BY** — free to use with attribution. Drop a few into `clips/` and add an
attribution line to the README if you use them.
