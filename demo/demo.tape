# VHS tape — scripted recording of the AB Video Stitcher terminal demo.
# VHS turns this file into a GIF with no manual screen-recording.
#   Install:  brew install vhs   (or: go install github.com/charmbracelet/vhs@latest)
#   Record:   cd demo && vhs demo.tape   →   produces demo.gif
#
# Run make_samples.sh once before recording so the clips already exist:
#   ./make_samples.sh

Output demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1200
Set Height 700
Set Padding 20
Set Theme "Catppuccin Mocha"

# Let the title breathe.
# NB: keep this an ASCII hyphen, not an em-dash — multibyte chars confuse
# VHS/ttyd column tracking and scramble the rest of the line.
Type "# AB Video Stitcher - pack clips into a 5k collage" Sleep 800ms Enter
Sleep 1s

# Show the inputs (different sizes + durations).
Type "ls clips/" Sleep 500ms Enter
Sleep 1500ms

# Run the stitcher — the printed layout table is the star of the demo.
Type "python ../video_stitcher.py clips/ --preset ultrafast" Sleep 600ms Enter
Sleep 6s

# Show the result file.
Type "ls -lh stitched_5k.mp4" Sleep 500ms Enter
Sleep 2500ms
