# StaticVideoSorter GUI — Implementation Plan

## Overview

Build a local web UI for StaticVideoSorter that works identically on:
- **PC** (Linux / Windows / Mac): `python app.py` → open `localhost:7860` in browser
- **Android Termux**: `python app.py` → open `localhost:7860` in phone browser

The server always processes files **local to wherever it is running**. No cross-device
file access. No cloud. No accounts. The browser is just a window into the server.

The existing `detect.py` and `extract.py` CLI scripts remain **fully independent and
unchanged**. The GUI reimplements the same core logic internally — it does not import,
call, or wrap the CLI scripts.

---

## Repository Structure

```
StaticVideoSorter/
├── detect.py               ← existing, unchanged
├── extract.py              ← existing, unchanged
├── app.py                  ← NEW: FastAPI server
├── core.py                 ← NEW: shared detection + extraction logic
│                                  (extracted from detect.py / extract.py)
├── templates/
│   └── index.html          ← NEW: single-page frontend
├── static/
│   ├── app.js              ← NEW: frontend logic
│   └── app.css             ← NEW: styles
├── config.json             ← auto-created on first run, stores user defaults
├── requirements.txt        ← add: fastapi, uvicorn, python-multipart
└── plan.md                 ← this file
```

---

## Stack Decisions

| Concern | Choice | Reason |
|---|---|---|
| Backend | FastAPI + Uvicorn | Lightweight, async, SSE support, runs on Termux |
| Frontend | Vanilla HTML + JS (no framework) | No build step, works everywhere, simple to maintain |
| Progress streaming | Server-Sent Events (SSE) | One-way server→browser, simpler than WebSocket |
| Styling | Plain CSS with CSS variables | No dependencies, full control |
| Video serving | FastAPI `FileResponse` with range support | HTML5 `<video>` needs range requests to seek |
| Folder navigation | Text input + server-side autocomplete | Works on Termux (no OS file picker dialog) |
| Job management | In-memory dict with UUID job IDs | Simple, no DB needed, jobs persist if browser closes |

---

## File: `core.py`

Extract all detection and extraction logic from `detect.py` and `extract.py` into
a shared `core.py`. Both the CLI scripts and `app.py` import from here.

**Functions to expose:**

```python
# Detection
def probe_video(path: Path) -> dict | None
def extract_analysis_frames(path, duration, n, debug=False) -> list
def layer1_global_motion(frames) -> float
def layer2_spatial_zones(frames) -> float
def layer3_heuristics(info) -> float
def compute_confidence(global_motion, zone_ratio, heuristic, T, duration) -> float
def detect_video(video_path, thresholds, debug=False) -> dict

# Extraction
def extract_full_res_frames(path, duration) -> list
def pick_best_frame(frames) -> int
def extract_one_frame(video_path, output_path, fmt, quality) -> bool

# Shared
def detect_environment() -> dict
def sample_count(duration) -> int
SENSITIVITY_PRESETS: dict
VIDEO_EXTENSIONS: set
```

**Important:** After creating `core.py`, update `detect.py` and `extract.py` to import
from it instead of duplicating the logic. Verify both CLI scripts still work identically
after refactor.

---

## File: `app.py`

FastAPI application. Handles HTTP requests, job management, file serving.

### Startup

```python
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=False)
```

Bind to `0.0.0.0` so it works on both localhost (PC) and Termux (phone browser at
`localhost:7860`). Print startup message with the URL.

### Job System

```python
import uuid
from dataclasses import dataclass, field
from threading import Thread, Event

@dataclass
class Job:
    id: str
    type: str                    # "detect" | "extract"
    folder: Path
    status: str                  # "running" | "done" | "cancelled" | "error"
    progress: int = 0
    total: int = 0
    results: list = field(default_factory=list)
    cancel_event: Event = field(default_factory=Event)
    error: str = ""

jobs: dict[str, Job] = {}        # job_id → Job
```

Jobs run in background threads. SSE endpoints read from the job object.

### Path Security

All file paths passed from the frontend must be validated:

```python
def validate_path(path_str: str) -> Path:
    path = Path(path_str).resolve()
    # Must be absolute, must exist, must be a directory (for folder ops)
    # No restriction on which directory — user is running this locally
    # and knows what they're doing. Just prevent empty/None paths.
    if not path_str or not path.exists():
        raise HTTPException(400, "Invalid path")
    return path
```

---

## API Endpoints

### Navigation

```
GET /
    → Serves index.html

GET /api/browse?path=/home/user
    → Returns list of subdirectories at given path for autocomplete
    → Response: { "dirs": ["Videos", "Downloads", "Pictures"], "current": "/home/user" }
    → If path is empty, returns home directory
    → On error (permission denied etc), returns empty list gracefully
```

### Detection

```
POST /api/detect
    Body: {
        "folder": "/path/to/videos",
        "sensitivity": "medium",
        "workers": 4,
        "fresh": false
    }
    → Starts detection job in background thread
    → Response: { "job_id": "uuid" }

GET /api/detect/{job_id}/stream
    → SSE stream. Each event is one of:
        data: {"type": "progress", "done": 5, "total": 100, "filename": "vid.mp4", "decision": "static", "confidence": 0.87}
        data: {"type": "summary", "static": 80, "dynamic": 15, "review": 5, "errors": 0, "space_saved_mb": 1240}
        data: {"type": "done"}
        data: {"type": "error", "message": "..."}
    → Client reconnects automatically if connection drops (SSE spec)

POST /api/detect/{job_id}/cancel
    → Sets cancel_event on the job
    → Response: { "ok": true }

GET /api/detect/{job_id}/status
    → Returns current job state (for reconnecting after browser refresh)
    → Response: { "status": "running", "progress": 45, "total": 100, "results": [...] }
```

### Review

```
GET /api/review?folder=/path/to/videos
    → Scans <folder>/review/ for video files
    → Returns list with metadata from detection_log.csv
    → Response: {
        "videos": [
          {
            "filename": "vid.mp4",
            "path": "/path/to/videos/review/vid.mp4",
            "confidence": 0.52,
            "global_motion_score": 6.3,
            "duration_s": 15.0,
            "width": 720,
            "height": 1280
          }
        ]
      }

POST /api/review/decide
    Body: {
        "path": "/path/to/videos/review/vid.mp4",
        "decision": "static",        // or "dynamic" or "skip"
        "base_folder": "/path/to/videos"
    }
    → Moves file to <base_folder>/static/ or <base_folder>/dynamic/
    → Updates detection_log.csv decision for that file
    → Response: { "ok": true, "moved_to": "/path/to/videos/static/vid.mp4" }
```

### Extraction

```
POST /api/extract
    Body: {
        "folder": "/path/to/videos/static",
        "output_dir": null,          // null = <folder>/extracted_frames/
        "format": "jpg",
        "quality": 95,
        "workers": 4,
        "skip_existing": false
    }
    → Starts extraction job in background thread
    → Response: { "job_id": "uuid" }

GET /api/extract/{job_id}/stream
    → SSE stream. Each event:
        data: {"type": "progress", "done": 5, "total": 80, "filename": "vid.mp4", "status": "ok"}
        data: {"type": "done", "extracted": 78, "skipped": 0, "errors": 2}
        data: {"type": "error", "message": "..."}

POST /api/extract/{job_id}/cancel
    → Response: { "ok": true }
```

### File Serving

```
GET /api/video?path=/absolute/path/to/video.mp4
    → Streams video file with HTTP range request support
    → Required for HTML5 <video> seeking to work
    → Use FastAPI's FileResponse or manual range handling

GET /api/image?path=/absolute/path/to/frame.jpg
    → Serves image file
    → Use FastAPI's FileResponse

GET /api/frames?folder=/path/to/videos/static
    → Returns list of extracted frames in <folder>/extracted_frames/
    → Response: { "frames": [{"filename": "vid.jpg", "path": "..."}] }
```

### Config

```
GET /api/config
    → Reads config.json, returns defaults
    → Response: {
        "sensitivity": "medium",
        "workers": 4,
        "output_format": "jpg",
        "quality": 95
      }

POST /api/config
    Body: { same structure }
    → Writes to config.json
    → Response: { "ok": true }
```

---

## Frontend: `index.html` + `app.js` + `app.css`

### Design Direction

**Aesthetic:** Dark, utilitarian, developer-tool feel. Think file manager meets terminal.
Not a consumer app. Dense information, no fluff. Color palette: dark grey background
(`#1a1a1a`), white text, a single accent color (amber `#f59e0b` works well for
progress/active states). Monospace font for filenames and scores. Clean sans-serif for
UI labels.

**NOT:** Gradients, rounded everything, pastel colors, marketing copy aesthetic.

### Layout

```
┌─────────────────────────────────────────────┐
│  StaticVideoSorter          [Detect][Review][Extract][Settings]  │
├─────────────────────────────────────────────┤
│                                             │
│              [active tab content]           │
│                                             │
└─────────────────────────────────────────────┘
```

Single HTML page. Tab switching is pure JS (show/hide divs), no page reloads.
The header with tab buttons is always visible.

---

### Tab 1: Detect

```
┌─ Detect ────────────────────────────────────┐
│                                             │
│  Folder  [/home/user/videos          ] [▶]  │
│          [autocomplete dropdown]            │
│                                             │
│  Sensitivity  ○ Low  ● Medium  ○ High       │
│  Workers      [4        ]                   │
│                                             │
│  [  Run Detection  ]   [ Fresh Start ]      │
│                                             │
│  ─────────────────────────────────────────  │
│                                             │
│  Detecting... ████████░░░░░░  45/100        │
│                                             │
│  vid001.mp4  →  static    0.89              │
│  vid002.mp4  →  dynamic   0.12              │
│  vid003.mp4  →  review    0.51              │
│  ...                                        │
│                                             │
│  ─────────────────────────────────────────  │
│  🖼  Static   80    🎥 Dynamic  15           │
│  🔍 Review    5     ❌ Errors    0           │
│  💾 Estimated space saved: 1.2 GB           │
│                                             │
│  [ Go to Review → ]                         │
└─────────────────────────────────────────────┘
```

**Behaviour:**
- Folder input: typing triggers `GET /api/browse?path=...` and shows a dropdown of
  matching subdirectories. Arrow keys navigate dropdown, Enter selects.
- Run Detection: POST to `/api/detect`, get job_id, open SSE stream.
- Live log: new rows prepend (newest at top) or append (newest at bottom) — append
  is simpler and fine.
- Progress bar: updates from SSE `progress` events.
- Summary card: appears when SSE sends `done` event.
- "Go to Review" button: only shows if review count > 0. Switches to Review tab and
  pre-fills the folder.
- If a checkpoint exists for the selected folder, show a banner:
  `"Checkpoint found — 45 videos already processed. Resume or Fresh Start?"`
- Cancel button appears while running, replaces Run button.

---

### Tab 2: Review

```
┌─ Review ────────────────────────────────────┐
│                                             │
│  Folder  [/home/user/videos          ]  [Load]  │
│                                             │
│  5 videos to review                         │
│  ─────────────────────────────────────────  │
│                                             │
│  ┌────────────────────────────────────┐     │
│  │                                    │     │
│  │         [  VIDEO PLAYER  ]         │     │
│  │                                    │     │
│  │  vid003.mp4                        │     │
│  │  Confidence: 0.51  Motion: 6.3     │     │
│  │  Duration: 15s  720×1280           │     │
│  │                                    │     │
│  │  [ Static ]  [ Dynamic ]  [ Skip ] │     │
│  └────────────────────────────────────┘     │
│                                             │
│  ◀ prev   3 / 5   next ▶                   │
│                                             │
│  Keyboard: S = Static  D = Dynamic  Space = Skip  │
└─────────────────────────────────────────────┘
```

**Behaviour:**
- Load button: GET `/api/review?folder=...` → populates the video queue.
- Video player: HTML5 `<video>` element. `src` points to `/api/video?path=...`.
  Loop enabled. Muted by default (autoplay policy). User can unmute.
- On Android: no autoplay. Show a large play button overlay instead.
- Decision buttons: POST `/api/review/decide`. On success, advance to next video.
  Card animates out (slide left for dynamic, slide right for static, fade for skip).
- Keyboard shortcuts: only active when not typing in an input field.
- When all videos are reviewed: show completion message with summary.
- Prev/next: allows going back to re-decide a video that was already moved.
  On revisit, show current location of the file (it may already be in static/ or dynamic/).

---

### Tab 3: Extract

```
┌─ Extract ───────────────────────────────────┐
│                                             │
│  Folder  [/home/user/videos/static   ] [▶]  │
│                                             │
│  Output  [<folder>/extracted_frames/ ]      │
│  Format  ● JPG  ○ PNG                       │
│  Quality [████████░░] 95                    │
│           (hidden if PNG selected)          │
│  Workers [4        ]                        │
│  ☐ Skip already extracted                  │
│                                             │
│  [  Run Extraction  ]                       │
│                                             │
│  ─────────────────────────────────────────  │
│                                             │
│  Extracting... ████████░░░░  45/80          │
│                                             │
│  vid001.mp4  →  ✅ ok                       │
│  vid002.mp4  →  ✅ ok                       │
│  vid099.mp4  →  ❌ error: no frames         │
│                                             │
│  ─────────────────────────────────────────  │
│  ✅ Extracted: 78   ⏭ Skipped: 0  ❌ Errors: 2  │
│                                             │
│  ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐ ┌──┐            │
│  │  │ │  │ │  │ │  │ │  │ │  │  ...        │
│  └──┘ └──┘ └──┘ └──┘ └──┘ └──┘            │
│  [click any frame to view full size]        │
└─────────────────────────────────────────────┘
```

**Behaviour:**
- Quality slider: only visible when JPG is selected. Updates numeric display live.
- Frame gallery: appears after extraction completes. Loads from
  `GET /api/frames?folder=...`. Images loaded lazily (`loading="lazy"`).
- Click frame: opens a lightbox overlay with full-size image. Click outside or
  press Escape to close.
- If the tab was pre-filled from Detect tab's "Go to Extract" button, folder is
  already populated.

---

### Tab 4: Settings

```
┌─ Settings ──────────────────────────────────┐
│                                             │
│  Defaults (used as pre-fills in other tabs) │
│  ─────────────────────────────────────────  │
│  Sensitivity    ○ Low  ● Medium  ○ High     │
│  Workers        [4        ]                 │
│  Output format  ● JPG  ○ PNG                │
│  JPG Quality    [████████░░] 95             │
│                                             │
│  [  Save Defaults  ]                        │
│                                             │
│  ─────────────────────────────────────────  │
│  About                                      │
│  StaticVideoSorter v1.0                            │
│  GitHub: github.com/hlaxkar/StaticVideoSorter      │
│                                             │
└─────────────────────────────────────────────┘
```

**Behaviour:**
- On load: fetch `GET /api/config` and pre-fill values.
- Save: POST `/api/config`. Show a brief "Saved ✓" confirmation.
- These defaults are used as initial values when other tabs load.

---

## SSE Implementation Pattern

Server side (FastAPI):

```python
from fastapi.responses import StreamingResponse
import asyncio
import json

@app.get("/api/detect/{job_id}/stream")
async def detect_stream(job_id: str):
    async def event_generator():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
            return

        last_sent = 0
        while job.status == "running":
            # Send any new results since last check
            new_results = job.results[last_sent:]
            for result in new_results:
                yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"
                last_sent += 1
            await asyncio.sleep(0.1)

        # Flush remaining
        for result in job.results[last_sent:]:
            yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"

        if job.status == "done":
            yield f"data: {json.dumps({'type': 'summary', **job.summary})}\n\n"
        elif job.status == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': job.error})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
```

Client side (JS):

```javascript
const es = new EventSource(`/api/detect/${jobId}/stream`);

es.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === 'progress') updateProgress(msg);
    if (msg.type === 'summary')  showSummary(msg);
    if (msg.type === 'done')     { es.close(); onComplete(); }
    if (msg.type === 'error')    { es.close(); showError(msg.message); }
};

es.onerror = () => {
    // SSE auto-reconnects by default — that's fine for mid-run browser refresh
};
```

---

## Video Range Request Serving

HTML5 `<video>` requires HTTP range requests to support seeking. FastAPI's
`FileResponse` handles this automatically for static files. Use it directly:

```python
from fastapi.responses import FileResponse

@app.get("/api/video")
async def serve_video(path: str):
    video_path = validate_path(path)
    if not video_path.is_file():
        raise HTTPException(404)
    return FileResponse(str(video_path), media_type="video/mp4")
```

Note: FastAPI's FileResponse supports range requests out of the box as of v0.95+.
For older versions, implement manual range handling.

---

## Termux-Specific Considerations

The code should detect Termux the same way `detect.py` does:

```python
IS_TERMUX = (
    os.environ.get("TERMUX_VERSION") is not None
    or Path("/data/data/com.termux").exists()
    or "com.termux" in os.environ.get("PREFIX", "")
)
```

When `IS_TERMUX` is True:
- Default workers: 2 (not 4)
- Default port: 7860 (same)
- Startup message: print `http://localhost:7860` clearly
- No OS file picker (text input + autocomplete only — same as PC, actually)

The frontend should detect touch capability and adjust:
```javascript
const IS_TOUCH = ('ontouchstart' in window) || navigator.maxTouchPoints > 0;
```

When touch is detected:
- Review tab: larger tap targets for Static/Dynamic/Skip buttons (min 48×48px)
- No keyboard shortcut hints shown
- Video does not autoplay (show tap-to-play overlay)
- Autocomplete dropdown items are taller

---

## `config.json` Format

Auto-created on first run if it doesn't exist:

```json
{
  "sensitivity": "medium",
  "workers": 4,
  "output_format": "jpg",
  "quality": 95
}
```

Stored in the same directory as `app.py`. If parsing fails, silently use defaults.

---

## Space Savings Calculation

After detection, estimate space saved by extracting static videos as images:

```python
def estimate_space_saved(static_video_paths: list[Path]) -> int:
    """Returns estimated bytes saved."""
    total = 0
    for p in static_video_paths:
        try:
            total += p.stat().st_size
        except Exception:
            pass
    # Assume extracted frame ≈ 300KB average for JPG
    # Actual saving = video_size - frame_size
    avg_frame_kb = 300 * 1024
    saved = total - (len(static_video_paths) * avg_frame_kb)
    return max(0, saved)
```

Show in the UI as: `"Estimated space savings: 1.2 GB"`

---

## Error Handling

Every API endpoint must return structured errors:

```python
# All errors follow this shape:
{"detail": "Human-readable error message"}

# FastAPI does this automatically with HTTPException:
raise HTTPException(status_code=400, detail="Folder does not exist")
```

Frontend error handling:
- Network error (server not reachable): show "Server not responding" banner
- Job error: show inline error message in the relevant tab
- File permission error: show specific message "Permission denied — check folder access"
- Never show a raw Python traceback to the user

---

## Running Instructions (to include in README)

```bash
# Install additional dependencies
pip install fastapi uvicorn python-multipart

# Run the GUI server
python app.py

# Open in browser
# PC:      http://localhost:7860
# Termux:  http://localhost:7860  (in phone browser)
```

---

## Implementation Order

Build in this sequence — each step is independently testable:

1. **`core.py`** — extract shared logic from `detect.py` and `extract.py`.
   Verify: `python detect.py` and `python extract.py` still work identically.

2. **`app.py` skeleton** — FastAPI app, static file serving, `GET /` returns
   a placeholder HTML page. Verify: server starts, browser loads page.

3. **`/api/browse` endpoint** — folder autocomplete.
   Verify: `curl "localhost:7860/api/browse?path=/home"` returns dirs.

4. **`/api/detect` + SSE stream** — detection job + streaming.
   Verify: run a detection job via curl, see SSE events in terminal.

5. **`/api/review` endpoints** — list + decide.
   Verify: curl the review list, curl a decide action, check file moved.

6. **`/api/extract` + SSE stream** — extraction job + streaming.
   Verify: run extraction via curl.

7. **`/api/video` and `/api/image`** — file serving.
   Verify: open a video URL directly in browser, check it plays and seeks.

8. **`index.html` + `app.js` + `app.css`** — full frontend.
   Build all 4 tabs. Wire up to all API endpoints.

9. **Termux testing** — run on Android, verify touch targets, video playback,
   path autocomplete with phone keyboard.

10. **Polish** — error states, edge cases (empty folders, permission errors,
    corrupt videos), loading states.

---

## Out of Scope (explicitly)

- User authentication
- Database (SQLite or otherwise)
- Docker / containerisation
- Mobile-optimised responsive layout (desktop-first, touch-friendly)
- Multiple concurrent users
- Remote file access / network drives
- Undo for file moves (once moved, moved — same as CLI behaviour)