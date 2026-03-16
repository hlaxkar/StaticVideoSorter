# StaticVideoSorter

Detect static-image videos (music visualizers, lyric videos, Instagram reposts) in a folder,
sort them into categorized subfolders, and extract the single best frame from each.

Works on **Linux** and **Android Termux** (auto-detected at runtime).

---

## Tools

| Script | Purpose |
|---|---|
| `detect.py` | Classify videos as **static** / **dynamic** / **review** |
| `extract.py` | Extract the best frame from every video in a folder |

These two scripts are designed to work together but are fully independent —
you can use either one on its own.

---

## Dependencies

### System

```bash
# Linux
sudo apt install ffmpeg

# Termux (Android)
pkg install ffmpeg
```

### Python

```bash
pip install -r requirements.txt
```

Or install manually:

```bash
pip install opencv-python-headless numpy tqdm
```

> `tqdm` is optional — a built-in fallback progress bar is used if it's not installed.

---

## Quick Start

```bash
# Step 1 — Classify videos (dry run, nothing is moved)
python detect.py /path/to/videos

# Step 2 — Inspect the detection_log.csv, then move when happy
python detect.py /path/to/videos --move

# Step 3 — Extract frames from the static folder
python extract.py /path/to/videos/static
```

---

## detect.py — Video Classification

```bash
python detect.py /path/to/folder [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--move` | off | Move classified videos into `static/` `dynamic/` `review/` subfolders |
| `--sensitivity low\|medium\|high` | `medium` | Detection aggressiveness |
| `--workers N` | auto | Parallel processing threads |
| `--fresh` | off | Ignore checkpoint, re-detect everything |
| `--report` | off | Print detailed per-video breakdown table after run |
| `--debug` | off | Print ffmpeg stderr for failed frame extractions |

### Output Structure

```
your_folder/
├── static/               ← confirmed static videos (moved with --move)
├── dynamic/              ← confirmed dynamic videos
├── review/               ← borderline videos for manual check
├── detection_log.csv     ← full audit trail with scores
└── checkpoint.json       ← progress checkpoint (resume-safe)
```

### Checkpoint & Resume

- Progress is saved to `checkpoint.json` after each video.
- If interrupted (Ctrl+C), re-run the same command to **resume** where you left off.
- Use `--fresh` to ignore the checkpoint and start from scratch.
- A second Ctrl+C force quits immediately.

---

## extract.py — Best Frame Extraction

```bash
python extract.py /path/to/folder [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--output-dir PATH` | `<folder>/extracted_frames/` | Where to save extracted frames |
| `--format png\|jpg` | `jpg` | Output image format |
| `--quality 1-100` | `95` | JPG quality (ignored for PNG) |
| `--workers N` | auto | Parallel processing threads |
| `--skip-existing` | off | Skip videos whose frame already exists |
| `--fresh` | off | Re-extract everything, ignoring skip list |

### Output

Frames are saved as `<video_stem>.jpg` (or `.png`) in the output directory.

---

## How Detection Works

### Layer 1 — Global Motion Score

Samples frames evenly across the video (2–16 depending on duration) and computes
the mean pixel difference between consecutive frames. A low score means very
little changes frame-to-frame → likely a static image.

### Layer 2 — Spatial Zone Analysis

Divides each frame into a **6×6 grid** and checks which zones have motion above
a threshold. If only a small fraction of zones are active (e.g. a corner watermark
or a scrolling text strip), the background is still considered static.
This catches Instagram stories with GIF stickers and lyric overlays.

### Layer 3 — Heuristics

Boosts confidence based on metadata signals:
- **Portrait/square aspect ratio** — Instagram story/post
- **Audio stream present** — music video
- **Short duration** — videos under 5s and 10s receive reduced confidence to avoid false positives;
  videos under 10 minutes get a moderate boost
- **Common repost codec** — H.264 / HEVC

### Final Decision

All three layer scores are combined into a single confidence value (0–1) using
weighted averaging (50% motion, 30% zones, 20% heuristics):

| Confidence | Decision |
|---|---|
| `≥ threshold_static` | **static** — move + extract frame |
| `≥ threshold_review` | **review** — move to review folder |
| Below both | **dynamic** — leave untouched |

---

## Best Frame Selection

When extracting frames, the script picks the frame that scores best on:

- **Low local motion** — avoids frames mid-animation or with a sticker overlay
- **High sharpness** — Laplacian variance; avoids blurry or faded frames
- First and last frames are **excluded** to dodge fade-in / fade-out

Full-resolution frames are extracted using PNG internally (no compression artifacts),
then saved in the requested output format.

---

## Sensitivity Guide

| Level | Use when |
|---|---|
| `low` | Conservative — only catch very obvious static videos |
| `medium` | Good default — catches most music/lyric/story videos |
| `high` | Aggressive — catches more borderline cases (more review videos) |

---

## Example Runs

```bash
# Preview decisions without moving anything
python detect.py ~/Videos

# Detailed per-video report
python detect.py ~/Videos --report

# Move classified videos into subfolders
python detect.py ~/Videos --move

# Aggressive detection with 8 workers
python detect.py ~/Videos --sensitivity high --workers 8 --move

# Extract frames from static videos as PNG
python extract.py ~/Videos/static --format png

# Extract from review folder into a custom output directory
python extract.py ~/Videos/review --output-dir ~/Videos/review_frames

# Re-extract only new additions
python extract.py ~/Videos/static --skip-existing
```

---

## Termux (Android) Support

Both scripts auto-detect Termux and adjust:
- **Workers** capped at 2 to avoid memory issues
- **Frame extraction** limited to 12 frames (vs 20 on Linux) to reduce RAM usage
- **Timeouts** extended for slower storage (SD card / FUSE)

Install dependencies in Termux:

```bash
pkg install ffmpeg python
pip install opencv-python-headless numpy tqdm
```

---

## License

MIT
