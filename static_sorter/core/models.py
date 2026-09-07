"""
Domain data models for video processing, detection, and extraction.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any


@dataclass(frozen=True)
class VideoItem:
    """Represents a discovered video file with respect to a root directory."""
    path: Path
    root_dir: Path
    rel_path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def rel_str(self) -> str:
        return str(self.rel_path)


@dataclass
class VideoMetadata:
    """Technical metadata probed from a video container/stream."""
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    aspect_ratio: str = "unknown"
    has_audio: bool = False
    codec: str = "unknown"
    filesize_bytes: int = 0


@dataclass
class DetectionResult:
    """Result of classifying a video file."""
    video: VideoItem
    metadata: VideoMetadata
    global_motion_score: float = 0.0
    active_zone_ratio: float = 0.0
    heuristic_score: float = 0.0
    final_confidence: float = 0.0
    decision: str = "dynamic"  # "static", "dynamic", "review"
    error: Optional[str] = None

    def to_log_dict(self) -> Dict[str, Any]:
        """Convert to dict for CSV / Checkpoint logging."""
        return {
            "filename": self.video.rel_str,
            "duration_s": f"{self.metadata.duration_s:.2f}",
            "width": self.metadata.width,
            "height": self.metadata.height,
            "aspect_ratio": self.metadata.aspect_ratio,
            "has_audio": "yes" if self.metadata.has_audio else "no",
            "global_motion_score": f"{self.global_motion_score:.2f}",
            "active_zone_ratio": f"{self.active_zone_ratio:.2f}",
            "heuristic_score": f"{self.heuristic_score:.2f}",
            "final_confidence": f"{self.final_confidence:.3f}",
            "decision": self.decision,
            "error": self.error or "",
        }

    @classmethod
    def from_log_dict(cls, video: VideoItem, data: Dict[str, Any]) -> "DetectionResult":
        """Reconstruct a DetectionResult instance from checkpoint/log dictionary."""
        def _safe_float(val: Any, default: float = 0.0) -> float:
            try:
                return float(val)
            except (ValueError, TypeError):
                return default

        def _safe_int(val: Any, default: int = 0) -> int:
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        meta = VideoMetadata(
            duration_s=_safe_float(data.get("duration_s")),
            width=_safe_int(data.get("width")),
            height=_safe_int(data.get("height")),
            aspect_ratio=str(data.get("aspect_ratio") or "unknown"),
            has_audio=str(data.get("has_audio", "")).lower() in ("yes", "true", "1"),
        )
        return cls(
            video=video,
            metadata=meta,
            global_motion_score=_safe_float(data.get("global_motion_score")),
            active_zone_ratio=_safe_float(data.get("active_zone_ratio")),
            heuristic_score=_safe_float(data.get("heuristic_score")),
            final_confidence=_safe_float(data.get("final_confidence")),
            decision=str(data.get("decision") or "dynamic"),
            error=data.get("error") or None,
        )


@dataclass
class ExtractionResult:
    """Result of extracting the best frame from a video."""
    video: VideoItem
    output_path: Optional[Path] = None
    status: str = "ok"  # "ok", "error", "skipped"
    best_frame_idx: int = 0
    total_frames_evaluated: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class SensitivityConfig:
    """Thresholds for detection sensitivity levels."""
    global_motion_static: float
    global_motion_review: float
    active_zone_ratio: float
    confidence_static: float
    confidence_review: float
