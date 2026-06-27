#!/usr/bin/env python3
"""
AB Video Stitcher GUI — a simple tkinter front-end for video_stitcher.py.

Pick an input folder, choose collage or concat mode, tweak a couple of
options, and hit Stitch. Encoding runs in a background thread and ffmpeg's
output is streamed into the log pane, so the window stays responsive.

No third-party packages — just the standard library (tkinter ships with
Python). Requires ffmpeg/ffprobe on PATH, same as the CLI.

    python video_stitcher_gui.py
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from video_stitcher import (
    CANVASES,
    DEFAULT_CANVAS,
    build_concat_cmd,
    build_ffmpeg_cmd,
    compute_layout,
    discover_clips,
    parse_canvas,
    resolve_codec,
)

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast",
           "medium", "slow", "slower", "veryslow"]

# Output names we generated automatically — safe to overwrite when the canvas
# or mode changes. Matches stitched.mp4, stitched_5k.mp4, stitched_1920x1080.mp4,
# … ; a name outside this shape means the user typed their own.
AUTO_OUTPUT_RE = re.compile(r"^stitched(_.+)?\.mp4$")

# Canvas dropdown shows "name (WxH)" so the presets are self-explanatory, but the
# value handed to parse_canvas is just the name (or "custom").
CANVAS_CHOICES = [f"{name} ({w}x{h})" for name, (w, h) in CANVASES.items()] + ["custom"]
DEFAULT_CANVAS_CHOICE = next(
    c for c in CANVAS_CHOICES if c.split(" (", 1)[0] == DEFAULT_CANVAS)


def _canvas_token(display: str) -> str:
    """Strip the "(WxH)" hint: 'square (1080x1080)' -> 'square'; 'custom' stays."""
    return display.split(" (", 1)[0]


# ffmpeg prints "time=HH:MM:SS.xx" on its progress line; we turn that into a
# fraction of the known output duration to drive a real progress bar.
_FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
PROGRESS_MAX = 1000  # progress bar resolution (finer than 100 for smoothness)


class StitcherGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("AB Video Stitcher")
        root.minsize(640, 520)

        # Thread → UI message channel.
        self.msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None

        self._build_widgets()
        self.root.after(100, self._drain_queue)

    # ── Layout ────────────────────────────────────────────────────────────
    def _build_widgets(self) -> None:
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)

        # Input folder
        ttk.Label(frm, text="Input folder:").grid(row=0, column=0, sticky="w", **pad)
        self.folder_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Browse…", command=self._pick_folder).grid(row=0, column=2, **pad)

        # Output file
        ttk.Label(frm, text="Output file:").grid(row=1, column=0, sticky="w", **pad)
        self.output_var = tk.StringVar(value=f"stitched_{DEFAULT_CANVAS}.mp4")
        ttk.Entry(frm, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Save as…", command=self._pick_output).grid(row=1, column=2, **pad)

        # Mode
        ttk.Label(frm, text="Mode:").grid(row=2, column=0, sticky="w", **pad)
        self.mode_var = tk.StringVar(value="collage")
        mode_frm = ttk.Frame(frm)
        mode_frm.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(mode_frm, text="Collage (all at once)", value="collage",
                        variable=self.mode_var, command=self._sync_mode).pack(side="left")
        ttk.Radiobutton(mode_frm, text="Concat (one after another)", value="concat",
                        variable=self.mode_var, command=self._sync_mode).pack(side="left", padx=12)

        # Canvas / layout options (first row)
        opts = ttk.Frame(frm)
        opts.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(opts, text="Canvas:").pack(side="left")
        self.canvas_var = tk.StringVar(value=DEFAULT_CANVAS_CHOICE)
        # Presets (shown as "name (WxH)") + a "custom" entry that unlocks the
        # WxH field beside it.
        self.canvas_combo = ttk.Combobox(
            opts, textvariable=self.canvas_var,
            values=CANVAS_CHOICES, width=18, state="readonly")
        self.canvas_combo.pack(side="left", padx=(4, 16))
        self.canvas_combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_canvas())

        ttk.Label(opts, text="Custom (WxH):").pack(side="left")
        self.custom_var = tk.StringVar(value="1920x1080")
        self.custom_entry = ttk.Entry(opts, textvariable=self.custom_var, width=12)
        self.custom_entry.pack(side="left", padx=(4, 16))
        for evt in ("<FocusOut>", "<Return>"):
            self.custom_entry.bind(evt, lambda _e: self._sync_output_default())

        ttk.Label(opts, text="Columns:").pack(side="left")
        self.cols_var = tk.StringVar(value="auto")
        self.cols_entry = ttk.Entry(opts, textvariable=self.cols_var, width=6)
        self.cols_entry.pack(side="left", padx=(4, 16))

        # Encoder options (second row)
        opts2 = ttk.Frame(frm)
        opts2.grid(row=4, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(opts2, text="CRF:").pack(side="left")
        self.crf_var = tk.StringVar(value="18")
        ttk.Spinbox(opts2, from_=0, to=51, textvariable=self.crf_var, width=5).pack(side="left", padx=(4, 16))

        ttk.Label(opts2, text="Preset:").pack(side="left")
        self.preset_var = tk.StringVar(value="medium")
        ttk.Combobox(opts2, textvariable=self.preset_var, values=PRESETS,
                     width=10, state="readonly").pack(side="left", padx=(4, 16))

        ttk.Label(opts2, text="Codec:").pack(side="left")
        self.codec_var = tk.StringVar(value="auto")
        ttk.Combobox(opts2, textvariable=self.codec_var, values=["auto", "h264", "hevc"],
                     width=6, state="readonly").pack(side="left", padx=4)

        # Action buttons
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        self.run_btn = ttk.Button(btns, text="Stitch", command=self._start)
        self.run_btn.pack(side="left")
        self.reveal_btn = ttk.Button(btns, text="Reveal output", command=self._reveal, state="disabled")
        self.reveal_btn.pack(side="left", padx=8)

        # Progress + log
        self.progress = ttk.Progressbar(frm, mode="indeterminate")
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)

        self.log = scrolledtext.ScrolledText(frm, height=14, wrap="word", state="disabled")
        self.log.grid(row=7, column=0, columnspan=3, sticky="nsew", **pad)
        frm.rowconfigure(7, weight=1)

        self._sync_mode()

    def _sync_mode(self) -> None:
        # Canvas + columns only apply to collage mode.
        collage = self.mode_var.get() == "collage"
        self.cols_entry.configure(state="normal" if collage else "disabled")
        self.canvas_combo.configure(state="readonly" if collage else "disabled")
        self._sync_canvas()

    def _sync_canvas(self) -> None:
        # The custom WxH field is only live when collage + "custom" is selected.
        custom = (self.mode_var.get() == "collage"
                  and _canvas_token(self.canvas_var.get()) == "custom")
        self.custom_entry.configure(state="normal" if custom else "disabled")
        self._sync_output_default()

    def _effective_canvas(self) -> str:
        """The canvas string to hand to parse_canvas: the custom WxH when
        'custom' is selected, otherwise the chosen preset name."""
        token = _canvas_token(self.canvas_var.get())
        if self.mode_var.get() == "collage" and token == "custom":
            return self.custom_var.get().strip()
        return token

    def _sync_output_default(self) -> None:
        """Keep the default output name in step with the canvas/mode, unless
        the user has typed a custom one."""
        if not AUTO_OUTPUT_RE.match(self.output_var.get().strip()):
            return
        if self.mode_var.get() != "collage":
            self.output_var.set("stitched.mp4")
            return
        try:
            label = parse_canvas(self._effective_canvas())[0]
        except ValueError:
            label = DEFAULT_CANVAS
        self.output_var.set(f"stitched_{label}.mp4")

    # ── File pickers ──────────────────────────────────────────────────────
    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="Select folder with video clips")
        if path:
            self.folder_var.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save output as", defaultextension=".mp4",
            initialfile=Path(self.output_var.get()).name,
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    # ── Logging helpers ───────────────────────────────────────────────────
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ── Run ───────────────────────────────────────────────────────────────
    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        folder = Path(self.folder_var.get()).expanduser()
        if not folder.is_dir():
            messagebox.showerror("AB Video Stitcher", "Please choose a valid input folder.")
            return

        output = Path(self.output_var.get()).expanduser()
        if not output.is_absolute():
            output = folder / output

        mode = self.mode_var.get()
        try:
            crf = int(self.crf_var.get())
        except ValueError:
            messagebox.showerror("AB Video Stitcher", "CRF must be a whole number (0–51).")
            return

        cols = None
        if mode == "collage":
            raw = self.cols_var.get().strip().lower()
            if raw and raw != "auto":
                try:
                    cols = int(raw)
                except ValueError:
                    messagebox.showerror("AB Video Stitcher", "Columns must be a number or 'auto'.")
                    return

        preset = self.preset_var.get()
        canvas = self._effective_canvas()
        codec = self.codec_var.get()

        if mode == "collage":
            try:
                parse_canvas(canvas)
            except ValueError as e:
                messagebox.showerror("AB Video Stitcher", f"Canvas: {e}")
                return

        # Lock UI.
        self.run_btn.configure(state="disabled")
        self.reveal_btn.configure(state="disabled")
        self.progress.configure(mode="determinate", maximum=PROGRESS_MAX, value=0)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.last_output = output

        self.worker = threading.Thread(
            target=self._run_job,
            args=(folder, output, mode, cols, crf, preset, canvas, codec),
            daemon=True,
        )
        self.worker.start()

    def _run_job(self, folder: Path, output: Path, mode: str,
                 cols: "int | None", crf: int, preset: str, canvas: str,
                 codec: str) -> None:
        """Runs in a background thread; talks to the UI only via msg_queue."""
        def emit(line: str) -> None:
            self.msg_queue.put(("log", line))

        try:
            emit(f"🎬 {mode} — scanning {folder}\n")
            clips = discover_clips(folder)
            for c in clips:
                emit(f"  Found: {c.path.name}  ({c.width}x{c.height}, {c.duration:.1f}s)\n")

            if mode == "concat":
                biggest = max(clips, key=lambda c: c.width * c.height)
                tw = biggest.width - (biggest.width % 2)
                th = biggest.height - (biggest.height % 2)
                total = sum(c.duration for c in clips)
                emit(f"\n  Target size: {tw}x{th} (largest: {biggest.path.name}), "
                     f"{total:.1f}s total\n")
                chosen = resolve_codec(codec, tw, th)
                cmd = build_concat_cmd(clips, output, tw, th,
                                       crf=crf, preset=preset, codec=chosen)
                out_duration = total
            else:
                canvas_label, canvas_w, canvas_h = parse_canvas(canvas)
                max_dur = max(c.duration for c in clips)
                cells = compute_layout(clips, cols=cols,
                                       canvas_w=canvas_w, canvas_h=canvas_h)
                emit(f"\n  Layout ({canvas_label}: {canvas_w}x{canvas_h}), {max_dur:.1f}s:\n")
                for cell in cells:
                    name = clips[cell.clip_idx].path.name
                    emit(f"    [{name}] → {cell.w}x{cell.h} @ ({cell.x},{cell.y})\n")
                chosen = resolve_codec(codec, canvas_w, canvas_h)
                cmd = build_ffmpeg_cmd(clips, cells, output, max_dur,
                                       crf=crf, preset=preset,
                                       canvas_w=canvas_w, canvas_h=canvas_h,
                                       codec=chosen)
                out_duration = max_dur

            note = "  (auto → HEVC for Mac playback)" if codec == "auto" and chosen == "hevc" else ""
            emit(f"\n  Codec: {chosen}{note}\n")
            emit(f"  Encoding to {output} …\n\n")
            self._stream_ffmpeg(cmd, emit, out_duration)
            self.msg_queue.put(("done", str(output)))

        except FileNotFoundError:
            self.msg_queue.put(("error", "ffmpeg/ffprobe not found. Install ffmpeg and try again."))
        except SystemExit as e:  # discover_clips uses sys.exit on empty folders
            self.msg_queue.put(("error", str(e)))
        except Exception as e:  # noqa: BLE001 — surface anything to the user
            self.msg_queue.put(("error", str(e)))

    def _stream_ffmpeg(self, cmd: list[str], emit,
                       total_duration: float = 0.0) -> None:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None
        # ffmpeg uses \r for progress; normalise so the log scrolls sensibly.
        buf = b""
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            buf = buf.replace(b"\r", b"\n")
            *lines, buf = buf.split(b"\n")
            for line in lines:
                text = line.decode("utf-8", "replace")
                emit(text + "\n")
                self._report_progress(text, total_duration)
        if buf:
            emit(buf.decode("utf-8", "replace") + "\n")
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f"ffmpeg exited with code {code}.")

    def _report_progress(self, text: str, total_duration: float) -> None:
        """Turn an ffmpeg 'time=…' line into a progress-bar fraction."""
        if total_duration <= 0:
            return
        m = _FFMPEG_TIME_RE.search(text)
        if not m:
            return
        h, mnt, sec = m.groups()
        elapsed = int(h) * 3600 + int(mnt) * 60 + float(sec)
        pct = int(PROGRESS_MAX * elapsed / total_duration)
        self.msg_queue.put(("progress", max(0, min(PROGRESS_MAX, pct))))

    # ── UI message pump ───────────────────────────────────────────────────
    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "progress":
                    self.progress.configure(value=payload)
                elif kind == "done":
                    self.progress.configure(value=PROGRESS_MAX)
                    self.run_btn.configure(state="normal")
                    self.reveal_btn.configure(state="normal")
                    self._log(f"\n✅ Done! Output: {payload}\n")
                elif kind == "error":
                    self.progress.configure(value=0)
                    self.run_btn.configure(state="normal")
                    self._log(f"\n❌ {payload}\n")
                    messagebox.showerror("AB Video Stitcher", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _reveal(self) -> None:
        if not self.last_output or not self.last_output.exists():
            return
        target = str(self.last_output)
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", target])
        elif os.name == "nt":
            subprocess.run(["explorer", "/select,", target])
        else:
            subprocess.run(["xdg-open", str(self.last_output.parent)])


def main() -> None:
    root = tk.Tk()
    StitcherGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
