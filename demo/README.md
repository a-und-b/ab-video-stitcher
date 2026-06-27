# Demo assets

Everything here is **self-contained and copyright-free** — the sample clips are
generated synthetically by ffmpeg (`testsrc2`), so nothing needs licensing.

There are two kinds of demo material you might want for the project README:

1. **Terminal GIF** — shows the tool running and printing its layout table.
2. **Output-preview GIF** — a short looping preview of the actual collage video.

## 0. Generate the sample clips (once)

```bash
cd demo
./make_samples.sh          # writes clips/ : landscape, square, portrait, 1080p
```

These deliberately differ in size and duration, which is what shows off the
collage packing and the concat scale-to-largest behaviour.

## 1. Terminal GIF (recommended) — via VHS

[VHS](https://github.com/charmbracelet/vhs) records a scripted terminal session
straight to a GIF, so the result is reproducible — no manual screen capture.

```bash
brew install vhs          # one-time
cd demo
vhs demo.tape             # reads demo.tape → writes demo.gif
```

The recording flow lives in [`demo.tape`](demo.tape); tweak the `Sleep` timings
or commands there.

## 2. Output-preview GIF — via ffmpeg

Turn a finished collage into a small looping GIF for the README. (Uses a
two-pass palette for clean colours, scales down, and caps the length.)

```bash
# produce a collage first
python ../video_stitcher.py clips/ -o collage.mp4 --preset ultrafast

# then: first 6s, 12 fps, 960px wide, good palette
ffmpeg -y -t 6 -i collage.mp4 \
  -vf "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 preview.gif
```

## Want real-looking footage instead?

The Blender open movies (Big Buck Bunny, Sintel, Tears of Steel, …) are
**CC BY** — free to use with attribution. Drop a few into `clips/` and add an
attribution line to the README if you use them.
