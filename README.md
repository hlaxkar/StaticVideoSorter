# detect_static.py

Detects static-image videos (music videos, lyric videos, Instagram reposts) in a folder,
moves them to a separate directory, and extracts the single best frame from each.

---

## Dependencies

```bash
# System
sudo apt install ffmpeg

# Python
pip install opencv-python-headless numpy tqdm
```

---

## Usage

```bash
python detect_static.py /path/to/folder [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Simulate everything, move nothing |
| `--workers N` | CPU count | Parallel processing threads |
| `--sensitivity low\|medium\|high` | medium | Detection aggressiveness |
| `--output-format png\|jpg` | png | Extracted frame format |
| `--frame-quality 1-100` | 95 | JPG quality (ignored for PNG) |
| `--skip-extracted` | off | Skip videos already in the log |

---

## Output Structure

```
your_folder/
├── static_videos/        ← confirmed static videos (moved here)
├── extracted_frames/     ← best frame per video (PNG or JPG)
├── review/               ← borderline videos for manual check
└── detection_log.csv     ← full audit trail with scores
```

---

## How Detection Works

### Layer 1 — Global Motion Score
Samples ~24 frames evenly across the video and computes the mean pixel
difference between consecutive frames. A low score means very little
changes between frames → likely static.

### Layer 2 — Spatial Zone Analysis
Divides each frame into a 6×6 grid and checks which zones have motion.
If only a small fraction of zones are active (e.g. corner watermark,
scrolling text strip), the background is still considered static.
This catches Instagram stories with GIF stickers and lyric overlays.

### Layer 3 — Heuristics
Boosts confidence based on signals like:
- Portrait/square aspect ratio (Instagram story/post)
- Presence of audio stream (music video)
- Short duration
- Common repost codec (H.264)

### Final Decision
All three scores are combined into a confidence value [0–1]:
- **≥ threshold_static** → `static` (move + extract frame)
- **≥ threshold_review** → `review` (move to review folder)
- **below both** → `dynamic` (leave untouched)

---

## Best Frame Selection

Among sampled frames, the script picks the frame that scores best on:
- **Low local motion** (avoids frames mid-animation or with sticker overlay)
- **High sharpness** (Laplacian variance, avoids blurry or faded frames)
- First and last frames are excluded (fade-in / fade-out)

PNG output uses compression level 0 (no compression) for maximum quality.

---

## Sensitivity Guide

| Level | Use when |
|---|---|
| `low` | You want to be conservative; only catch very obvious static videos |
| `medium` | Good default; catches most music/lyric/story videos |
| `high` | Aggressive; catches more borderline cases (more review videos) |

Start with `--dry-run --sensitivity medium` to see what would be caught
before committing.

---

## Example Runs

```bash
# Dry run first to preview decisions
python detect_static.py ~/Videos --dry-run

# Real run with default settings
python detect_static.py ~/Videos

# More aggressive detection, JPG frames, 8 workers
python detect_static.py ~/Videos --sensitivity high --output-format jpg --workers 8

# Re-run on new additions only
python detect_static.py ~/Videos --skip-extracted
```

---

## Re-running Safely

- `--skip-extracted` reads the existing `detection_log.csv` and skips
  already-processed filenames, so you can safely re-run on a folder
  that had new videos added.
- Videos already moved to `static_videos/` or `review/` are excluded
  from scanning automatically.
