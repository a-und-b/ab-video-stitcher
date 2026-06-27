#!/usr/bin/env bash
# Generate zero-copyright sample clips for the demo, using ffmpeg's built-in
# synthetic sources (testsrc2). Different sizes + durations on purpose — that's
# what exercises the collage packing and the concat scale-to-largest padding.
#
# Usage: ./make_samples.sh [output_dir]   (default: ./clips)
set -euo pipefail

out="${1:-clips}"
mkdir -p "$out"

# size            duration  label
gen() {
  local size="$1" dur="$2" name="$3"
  ffmpeg -v error -y \
    -f lavfi -i "testsrc2=size=${size}:rate=30" \
    -t "$dur" -pix_fmt yuv420p "$out/$name"
  echo "  wrote $out/$name  (${size}, ${dur}s)"
}

echo "Generating synthetic sample clips in '$out/' …"
gen 1280x720   3 landscape_720p.mp4
gen 1080x1080  5 square.mp4
gen 720x1280   4 portrait.mp4
gen 1920x1080  2 landscape_1080p.mp4
echo "Done. Try:  python ../video_stitcher.py $out"
