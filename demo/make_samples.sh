#!/usr/bin/env bash
# Generate zero-copyright sample clips for the demo.
#
# Each clip is a solid background colour with a moving label showing the clip's
# source resolution — so in the collage you can tell every clip apart and see
# what got packed where. The mix of aspect ratios (wide, square, portrait,
# ultrawide) is what pushes the layout into multiple rows.
#
# Nothing here is licensed footage: colour + text are synthesised, so the
# output is free to use anywhere.
#
# Text is drawn with ffmpeg's drawtext when available, otherwise via an
# ImageMagick-rendered PNG overlaid by ffmpeg. If neither is present, clips are
# still generated as plain colour fields (no label).
#
# Usage: ./make_samples.sh [output_dir]   (default: ./clips)
#        FONT=/path/to/font.ttf ./make_samples.sh   (override the label font)
set -euo pipefail

out="${1:-clips}"
mkdir -p "$out"

# ── Pick a font file (drawtext and ImageMagick both want a real one) ─────────
FONT="${FONT:-}"
if [ -z "$FONT" ]; then
  for f in \
    /System/Library/Fonts/Helvetica.ttc \
    /System/Library/Fonts/Supplemental/Arial.ttf \
    /Library/Fonts/Arial.ttf \
    /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf; do
    [ -f "$f" ] && FONT="$f" && break
  done
fi

# ── Decide how to render text ────────────────────────────────────────────────
TEXT_MODE="none"
if ffmpeg -hide_banner -filters 2>/dev/null | grep -q ' drawtext ' && [ -n "$FONT" ]; then
  TEXT_MODE="drawtext"
elif command -v magick >/dev/null 2>&1; then
  TEXT_MODE="magick"; MAGICK="magick"
elif command -v convert >/dev/null 2>&1; then
  TEXT_MODE="magick"; MAGICK="convert"
fi
echo "Text rendering: $TEXT_MODE${FONT:+  (font: $FONT)}"
[ "$TEXT_MODE" = "none" ] && echo "  note: no drawtext/ImageMagick — clips will have no label." >&2

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# gen SIZE DURATION COLOR LABEL NAME
gen() {
  local size="$1" dur="$2" color="$3" label="$4" name="$5"
  local h="${size##*x}"
  local fs=$(( h / 9 ))          # font size relative to clip height
  local bw=$(( fs / 30 + 1 ))    # outline width (thin, so white stays white)
  # Horizontal ease back and forth, vertically centred, one sweep per clip.
  local x_expr_dt="(w-text_w)/2*(1+0.8*sin(2*PI*t/${dur}))"
  local x_expr_ov="(W-w)/2*(1+0.8*sin(2*PI*t/${dur}))"

  case "$TEXT_MODE" in
    drawtext)
      ffmpeg -v error -y -f lavfi -i "color=c=${color}:s=${size}:r=30" -t "$dur" \
        -vf "drawtext=fontfile='${FONT}':text='${label}':fontcolor=white:fontsize=${fs}:borderw=${bw}:bordercolor=black@0.6:x='${x_expr_dt}':y='(h-text_h)/2'" \
        -pix_fmt yuv420p "$out/$name"
      ;;
    magick)
      local png="$tmp/${name%.mp4}.png"
      "$MAGICK" -background none ${FONT:+-font "$FONT"} -pointsize "$fs" \
        -fill white -stroke black -strokewidth "$bw" "label:${label}" "$png"
      ffmpeg -v error -y -f lavfi -i "color=c=${color}:s=${size}:r=30" -loop 1 -i "$png" -t "$dur" \
        -filter_complex "[0:v][1:v]overlay=x='${x_expr_ov}':y='(H-h)/2'[v]" \
        -map "[v]" -pix_fmt yuv420p "$out/$name"
      ;;
    none)
      ffmpeg -v error -y -f lavfi -i "color=c=${color}:s=${size}:r=30" -t "$dur" \
        -pix_fmt yuv420p "$out/$name"
      ;;
  esac
  printf '  %-22s %-11s %s\n' "$name" "$size" "$color"
}

echo "Generating demo clips in '$out/' …"
gen 1280x720   6 crimson     "1280x720"  landscape_720p.mp4
gen 720x1280   6 teal        "720x1280"  portrait_hd.mp4
gen 1080x1080  6 darkorange  "1080x1080" square_1080.mp4
gen 1920x800   6 indigo      "1920x800"  ultrawide.mp4
gen 640x480    6 seagreen    "640x480"   sd_4x3.mp4
gen 864x1080   6 steelblue   "864x1080"  portrait_4x5.mp4
gen 1920x1080  6 hotpink     "1920x1080" wide_1080.mp4
echo "Done. Try:  python ../video_stitcher.py $out"
