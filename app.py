"""
app.py — FastAPI web server for StaticVideoSorter GUI.

Run:
    python app.py
    # Open http://localhost:7860 in your browser
"""

import csv
import json
import os
import uuid
import asyncio
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread, Event
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from core import (
    ENV, SENSITIVITY_PRESETS, VIDEO_EXTENSIONS,
    LOG_FILENAME, CHECKPOINT_FILENAME, LOG_FIELDS,
    Checkpoint, detect_video, extract_one_frame,
    safe_move, estimate_space_saved, probe_video,
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

APP_DIR      = Path(__file__).resolve().parent
CONFIG_PATH  = APP_DIR / "config.json"

DEFAULT_CONFIG = {
    "sensitivity":   "medium",
    "workers":       ENV["max_workers"],
    "output_format": "jpg",
    "quality":       95,
}

IS_TERMUX = ENV["is_termux"]


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text("utf-8"))}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# ─────────────────────────────────────────────
# JOB SYSTEM
# ─────────────────────────────────────────────

@dataclass
class Job:
    id: str
    type: str                    # "detect" | "extract"
    folder: Path
    status: str = "running"      # "running" | "done" | "cancelled" | "error"
    progress: int = 0
    total: int = 0
    results: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    cancel_event: Event = field(default_factory=Event)
    error: str = ""


jobs: dict[str, Job] = {}

# ─────────────────────────────────────────────
# DETECTION JOB THREAD
# ─────────────────────────────────────────────

def _run_detect_job(job: Job, sensitivity: str, workers: int, fresh: bool):
    try:
        thresholds = SENSITIVITY_PRESETS[sensitivity]
        folder     = job.folder

        ckpt_path = folder / CHECKPOINT_FILENAME
        ckpt      = Checkpoint(ckpt_path)
        if fresh:
            ckpt.clear()

        all_videos = sorted([
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ])

        if not all_videos:
            job.status = "done"
            job.summary = {"static": 0, "dynamic": 0, "review": 0,
                           "errors": 0, "space_saved_mb": 0}
            ckpt.flush_and_stop()
            return

        to_detect = [
            vp for vp in all_videos
            if not ckpt.is_done(vp.name) or ckpt.is_error(vp.name)
        ]

        # Pre-fill results from checkpoint for already-processed videos
        for vp in all_videos:
            if ckpt.is_done(vp.name) and not ckpt.is_error(vp.name):
                row = ckpt.get(vp.name)
                if row:
                    job.results.append({
                        "done": job.progress + 1,
                        "total": len(all_videos),
                        "filename": row.get("filename", vp.name),
                        "decision": row.get("decision", "unknown"),
                        "confidence": row.get("final_confidence", "0"),
                    })

        job.total = len(all_videos)
        job.progress = len(all_videos) - len(to_detect)

        if not to_detect:
            # Build summary from checkpoint
            _finalize_detect_job(job, ckpt, folder, all_videos)
            return

        capped_workers = min(workers, ENV["max_workers"])

        with ThreadPoolExecutor(max_workers=capped_workers) as executor:
            futures = {}
            for vp in to_detect:
                if job.cancel_event.is_set():
                    break
                futures[executor.submit(detect_video, vp, thresholds)] = vp

            for future in as_completed(futures):
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    break

                vp = futures[future]
                try:
                    row = future.result()
                except Exception as e:
                    row = {f: "" for f in LOG_FIELDS}
                    row["filename"] = vp.name
                    row["decision"] = f"error: {e}"

                ckpt.record(vp.name, row)
                job.progress += 1
                job.results.append({
                    "done":       job.progress,
                    "total":      job.total,
                    "filename":   row.get("filename", vp.name),
                    "decision":   row.get("decision", "unknown"),
                    "confidence": row.get("final_confidence", "0"),
                })

        ckpt.wait_for_writes()

        if job.status == "running":
            _finalize_detect_job(job, ckpt, folder, all_videos)

        ckpt.flush_and_stop()

        # Write CSV log
        log_path = folder / LOG_FILENAME
        all_rows = ckpt.all_rows()
        with open(log_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows)

    except Exception as e:
        job.status = "error"
        job.error  = str(e)


def _finalize_detect_job(job: Job, ckpt: Checkpoint, folder: Path, all_videos: list[Path]):
    all_rows = ckpt.all_rows()
    counts: dict[str, int] = {}
    for r in all_rows:
        d = r.get("decision", "unknown")
        bucket = d if d in ("static", "dynamic", "review") else "errors"
        counts[bucket] = counts.get(bucket, 0) + 1

    static_paths = [
        folder / r["filename"] for r in all_rows
        if r.get("decision") == "static" and (folder / r["filename"]).exists()
    ]
    saved = estimate_space_saved(static_paths)

    job.summary = {
        "static":         counts.get("static", 0),
        "dynamic":        counts.get("dynamic", 0),
        "review":         counts.get("review", 0),
        "errors":         counts.get("errors", 0),
        "space_saved_mb": round(saved / (1024 * 1024), 1),
    }
    job.status = "done"

# ─────────────────────────────────────────────
# EXTRACTION JOB THREAD
# ─────────────────────────────────────────────

def _run_extract_job(job: Job, output_dir: Path, fmt: str,
                     quality: int, workers: int, skip_existing: bool):
    try:
        folder = job.folder
        output_dir.mkdir(parents=True, exist_ok=True)

        video_files = sorted([
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        ])

        if not video_files:
            job.status  = "done"
            job.summary = {"extracted": 0, "skipped": 0, "errors": 0}
            return

        if skip_existing:
            to_process = [
                vp for vp in video_files
                if not (output_dir / (vp.stem + f".{fmt}")).exists()
            ]
            skipped_count = len(video_files) - len(to_process)
        else:
            to_process    = video_files
            skipped_count = 0

        job.total    = len(to_process)
        job.progress = 0

        capped_workers = min(workers, ENV["max_workers"])
        extracted = 0
        errors    = 0

        with ThreadPoolExecutor(max_workers=capped_workers) as executor:
            futures = {}
            for vp in to_process:
                if job.cancel_event.is_set():
                    break
                out_path = output_dir / (vp.stem + f".{fmt}")
                futures[executor.submit(
                    extract_one_frame, vp, out_path, fmt, quality
                )] = vp

            for future in as_completed(futures):
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    break

                vp = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    result = {"file": vp.name, "status": f"error: {e}", "output": ""}

                job.progress += 1
                if result["status"] == "ok":
                    extracted += 1
                elif result["status"].startswith("error"):
                    errors += 1

                job.results.append({
                    "done":     job.progress,
                    "total":    job.total,
                    "filename": result["file"],
                    "status":   result["status"],
                })

        if job.status == "running":
            job.summary = {
                "extracted": extracted,
                "skipped":   skipped_count,
                "errors":    errors,
            }
            job.status = "done"

    except Exception as e:
        job.status = "error"
        job.error  = str(e)

# ─────────────────────────────────────────────
# PATH VALIDATION
# ─────────────────────────────────────────────

def validate_path(path_str: str) -> Path:
    if not path_str:
        raise HTTPException(400, "Path is empty")
    path = Path(path_str).resolve()
    if not path.exists():
        raise HTTPException(400, f"Path does not exist: {path}")
    return path

# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────

app = FastAPI(title="StaticVideoSorter")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# ─── Page ───

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ─── Browse ───

@app.get("/api/browse")
async def browse(path: str = ""):
    if not path:
        path = str(Path.home())
    target = Path(path).resolve()
    if not target.exists() or not target.is_dir():
        return {"dirs": [], "current": path}
    try:
        dirs = sorted([
            e.name for e in target.iterdir()
            if e.is_dir() and not e.name.startswith(".")
        ])
    except PermissionError:
        dirs = []
    return {"dirs": dirs, "current": str(target)}

# ─── Detect ───

class DetectRequest(BaseModel):
    folder: str
    sensitivity: str = "medium"
    workers: int = 4
    fresh: bool = False

@app.post("/api/detect")
async def start_detect(req: DetectRequest):
    folder = validate_path(req.folder)
    if not folder.is_dir():
        raise HTTPException(400, "Path is not a directory")

    job_id = str(uuid.uuid4())
    job    = Job(id=job_id, type="detect", folder=folder)
    jobs[job_id] = job

    thread = Thread(target=_run_detect_job, args=(job, req.sensitivity, req.workers, req.fresh), daemon=True)
    thread.start()

    return {"job_id": job_id}


@app.get("/api/detect/{job_id}/stream")
async def detect_stream(job_id: str):
    async def event_generator():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
            return

        last_sent = 0
        while job.status == "running":
            new_results = job.results[last_sent:]
            for result in new_results:
                yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"
                last_sent += 1
            await asyncio.sleep(0.2)

        # Flush remaining
        for result in job.results[last_sent:]:
            yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"

        if job.status == "done":
            yield f"data: {json.dumps({'type': 'summary', **job.summary})}\n\n"
        elif job.status == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': job.error})}\n\n"
        elif job.status == "cancelled":
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job cancelled'})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/detect/{job_id}/cancel")
async def cancel_detect(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.cancel_event.set()
    return {"ok": True}


@app.get("/api/detect/{job_id}/status")
async def detect_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "status":   job.status,
        "progress": job.progress,
        "total":    job.total,
        "results":  job.results,
        "summary":  job.summary,
    }


# ─── Checkpoint info ───

@app.get("/api/checkpoint")
async def get_checkpoint(folder: str):
    target = Path(folder).resolve()
    ckpt_path = target / CHECKPOINT_FILENAME
    if not ckpt_path.exists():
        return {"exists": False, "count": 0}
    try:
        data = json.loads(ckpt_path.read_text("utf-8"))
        count = len(data.get("completed", {}))
        return {"exists": True, "count": count}
    except Exception:
        return {"exists": False, "count": 0}

# ─── Review ───

@app.get("/api/review")
async def review_list(folder: str):
    target = Path(folder).resolve()
    review_dir = target / "review"
    if not review_dir.is_dir():
        return {"videos": []}

    # Try to load detection_log.csv for metadata
    log_data = {}
    log_path = target / LOG_FILENAME
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    log_data[row.get("filename", "")] = row
        except Exception:
            pass

    videos = []
    for vf in sorted(review_dir.iterdir()):
        if not vf.is_file() or vf.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        meta = log_data.get(vf.name, {})
        videos.append({
            "filename":            vf.name,
            "path":                str(vf),
            "confidence":          meta.get("final_confidence", ""),
            "global_motion_score": meta.get("global_motion_score", ""),
            "duration_s":          meta.get("duration_s", ""),
            "width":               meta.get("width", ""),
            "height":              meta.get("height", ""),
        })

    return {"videos": videos}


class ReviewDecision(BaseModel):
    path: str
    decision: str       # "static" | "dynamic" | "skip"
    base_folder: str

@app.post("/api/review/decide")
async def review_decide(req: ReviewDecision):
    video_path = Path(req.path).resolve()
    if not video_path.is_file():
        raise HTTPException(400, "Video file not found")

    if req.decision == "skip":
        return {"ok": True, "moved_to": str(video_path)}

    base = Path(req.base_folder).resolve()
    dest_dir = base / req.decision
    dest_dir.mkdir(parents=True, exist_ok=True)

    moved_to = safe_move(video_path, dest_dir)

    # Update detection_log.csv
    log_path = base / LOG_FILENAME
    if log_path.exists():
        try:
            rows = []
            with open(log_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    if row.get("filename") == video_path.name:
                        row["decision"] = req.decision
                    rows.append(row)
            with open(log_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        except Exception:
            pass

    return {"ok": True, "moved_to": str(moved_to)}

# ─── Extract ───

class ExtractRequest(BaseModel):
    folder: str
    output_dir: str | None = None
    format: str = "jpg"
    quality: int = 95
    workers: int = 4
    skip_existing: bool = False

@app.post("/api/extract")
async def start_extract(req: ExtractRequest):
    folder = validate_path(req.folder)
    if not folder.is_dir():
        raise HTTPException(400, "Path is not a directory")

    output_dir = Path(req.output_dir).resolve() if req.output_dir else folder / "extracted_frames"

    job_id = str(uuid.uuid4())
    job    = Job(id=job_id, type="extract", folder=folder)
    jobs[job_id] = job

    thread = Thread(
        target=_run_extract_job,
        args=(job, output_dir, req.format, req.quality, req.workers, req.skip_existing),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


@app.get("/api/extract/{job_id}/stream")
async def extract_stream(job_id: str):
    async def event_generator():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
            return

        last_sent = 0
        while job.status == "running":
            new_results = job.results[last_sent:]
            for result in new_results:
                yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"
                last_sent += 1
            await asyncio.sleep(0.2)

        for result in job.results[last_sent:]:
            yield f"data: {json.dumps({'type': 'progress', **result})}\n\n"

        if job.status == "done":
            yield f"data: {json.dumps({'type': 'done', **job.summary})}\n\n"
        elif job.status == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': job.error})}\n\n"
        elif job.status == "cancelled":
            yield f"data: {json.dumps({'type': 'error', 'message': 'Job cancelled'})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/extract/{job_id}/cancel")
async def cancel_extract(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.cancel_event.set()
    return {"ok": True}

# ─── File Serving ───

@app.get("/api/video")
async def serve_video(path: str):
    video_path = Path(path).resolve()
    if not video_path.is_file():
        raise HTTPException(404, "Video not found")
    media_type = mimetypes.guess_type(str(video_path))[0] or "video/mp4"
    return FileResponse(str(video_path), media_type=media_type)


@app.get("/api/image")
async def serve_image(path: str):
    image_path = Path(path).resolve()
    if not image_path.is_file():
        raise HTTPException(404, "Image not found")
    media_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
    return FileResponse(str(image_path), media_type=media_type)


@app.get("/api/frames")
async def list_frames(folder: str):
    target = Path(folder).resolve()
    frames_dir = target / "extracted_frames"
    if not frames_dir.is_dir():
        return {"frames": []}

    frames = []
    for f in sorted(frames_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png"):
            frames.append({
                "filename": f.name,
                "path":     str(f),
            })

    return {"frames": frames}

# ─── Config ───

@app.get("/api/config")
async def get_config():
    return load_config()

@app.post("/api/config")
async def set_config(request: Request):
    body = await request.json()
    cfg  = load_config()
    cfg.update(body)
    save_config(cfg)
    return {"ok": True}

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = 7860
    print(f"\n  StaticVideoSorter GUI")
    print(f"  {'─' * 30}")
    print(f"  Open in browser: http://localhost:{port}")
    if IS_TERMUX:
        print(f"  Running in Termux mode")
    print()
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_level="warning")
