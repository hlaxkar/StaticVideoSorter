# StaticVideoSorter (`static-sorter`)

**StaticVideoSorter** automatically detects static-image videos (music visualizers, lyric videos, podcast clips, Instagram reposts) in your media libraries, categorizes them, and extracts the single best, sharpest frame as a lightweight still image.

---

## Key Features

- **3-Layer Intelligent Classification**:
  - *Layer 1*: Global inter-frame motion variance.
  - *Layer 2*: 6×6 spatial grid analysis (ignores localized lyric tickers, animated stickers, watermarks).
  - *Layer 3*: Metadata heuristics (aspect ratio, audio presence, duration scaling).
- **One-Shot End-to-End Pipeline**: Discover $\rightarrow$ Detect $\rightarrow$ Move $\rightarrow$ Extract best frames with one command.
- **Directory Structure Preservation**: When running with `-r/--recursive`, the original relative directory hierarchy is preserved across all categorized target folders (`static/`, `dynamic/`, `review/`, `extracted_frames/`).
- **Optimal Best Frame Selection**: Composite scoring combining Laplacian sharpness variance and neighborhood motion calmness (excluding fade-in/fade-out artifacts).
- **Metadata & EXIF Preservation**: Extracted stills retain original video timestamps (`DateTimeOriginal`), GPS location coordinates (map view), camera Make/Model, and container tags.
- **Embedded Tags & Keywords**: Automatically embeds keywords (`static-video`, `extracted-frame` or custom `--tags`) into EXIF `XPKeywords`, `UserComment`, and PNG text chunks for instant recognition in photo managers like Immich, Google Photos, and Apple Photos.
- **Fault-Tolerant & Resume-Safe**: Atomic background JSON/CSV checkpoints; clean SIGINT/SIGTERM handling.

---

## Installation

### 1. System Dependencies

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install ffmpeg

# Arch Linux
sudo pacman -S ffmpeg

# macOS (Homebrew)
brew install ffmpeg
```

### 2. Python Package

Install dependencies directly:
```bash
pip install -r requirements.txt
```

Or install `static-sorter` as a local CLI package:
```bash
pip install -e .
```

---

## Quick Start: One-Shot Pipeline

Run the end-to-end pipeline on any video collection:

```bash
# Classify, sort into subfolders, and extract still images with EXIF & tags
python -m static_sorter pipeline /path/to/videos -r
```

---

## Subcommands Reference

### 1. `pipeline` — Unified End-to-End Processing

```bash
static-sorter pipeline /path/to/videos [options]
```

| Option | Default | Description |
|---|---|---|
| `-r, --recursive` | off | Recursively scan subdirectories |
| `--no-move` | off | Perform frame extraction without moving videos |
| `--sensitivity {low,medium,high}` | `medium` | Classification aggressiveness |
| `--workers N` | auto | Parallel processing threads |
| `--format {jpg,png}` | `jpg` | Image output format |
| `--quality 1-100` | `95` | JPG output quality |
| `--tags TAGS` | `static-video,extracted-frame` | Comma-separated keywords/tags to embed into EXIF |
| `--fresh` | off | Ignore checkpoint and re-process everything |
| `--json` | off | Output structured JSON results to stdout |
| `-q, --quiet` | off | Suppress interactive progress output |

---

### 2. `detect` — Classify Videos

Classifies videos into `static/`, `dynamic/`, and `review/` categories and generates `detection_log.csv`.

```bash
static-sorter detect /path/to/videos [options]
```

| Option | Default | Description |
|---|---|---|
| `-r, --recursive` | off | Recursively search subdirectories |
| `--move` | off | Move classified videos into subfolders (preserving directory tree) |
| `--sensitivity {low,medium,high}` | `medium` | Detection sensitivity |
| `--report` | off | Print detailed per-video scoring breakdown table |
| `--fresh` | off | Clear checkpoint and re-analyze all files |

---

### 3. `extract` — Best Frame Extraction

Extracts the single highest quality frame from videos in a folder:

```bash
static-sorter extract /path/to/videos [options]
```

| Option | Default | Description |
|---|---|---|
| `-r, --recursive` | off | Recursively scan subdirectories |
| `--output-dir PATH` | `<folder>/extracted_frames/` | Output directory |
| `--format {jpg,png}` | `jpg` | Image format |
| `--quality 1-100` | `95` | JPG compression quality |
| `--tags TAGS` | `static-video,extracted-frame` | Comma-separated keywords/tags to embed into EXIF |
| `--skip-existing` | off | Skip videos whose image already exists |
| `--fresh` | off | Re-extract all frames |

---

### 4. `report` — Audit Log Inspection

View statistics and summaries from previous runs:

```bash
static-sorter report /path/to/videos
```

---

### 5. `check` — Diagnostics

Verify system binary and python module availability:

```bash
static-sorter check
```

---

## Recursive Directory Preservation Example

Given an input folder structure:
```
my_videos/
├── family/
│   └── 2024/
│       └── vacation_clip.mp4   (dynamic motion)
└── podcasts/
    └── episode12_visualizer.mp4 (static image + audio)
```

Running `static-sorter pipeline my_videos -r`:
```
my_videos/
├── dynamic/
│   └── family/
│       └── 2024/
│           └── vacation_clip.mp4
├── static/
│   └── podcasts/
│       ├── episode12_visualizer.mp4
│       └── episode12_visualizer.jpg   ← Extracted best still frame
├── detection_log.csv
└── checkpoint.json
```

---

## Legacy Scripts

`detect.py` and `extract.py` are preserved in the root directory for backward compatibility and seamlessly delegate to `static-sorter detect` and `static-sorter extract`.

---

## License

MIT
