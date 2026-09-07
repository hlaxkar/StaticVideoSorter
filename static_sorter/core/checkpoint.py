"""
Thread-safe atomic checkpoint and CSV audit logging manager.
"""
import csv
import json
import queue
import threading
from pathlib import Path
from typing import Dict, Any, Set, Optional

from static_sorter.core.config import (
    LOG_FILENAME,
    CHECKPOINT_FILENAME,
    LOG_FIELDS,
)
from static_sorter.core.models import DetectionResult


class CheckpointManager:
    """
    Manages persistent state and CSV audit logs.
    A dedicated background writer thread handles all file IO to eliminate race conditions.
    """

    def __init__(self, folder: Path, enable_csv: bool = True):
        self.folder = folder.resolve()
        self.ckpt_path = self.folder / CHECKPOINT_FILENAME
        self.log_path = self.folder / LOG_FILENAME
        self.enable_csv = enable_csv

        self._data: Dict[str, Any] = {"completed": {}, "meta": {}}
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()

        # Load existing checkpoint if present
        if self.ckpt_path.exists():
            try:
                loaded = json.loads(self.ckpt_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data["completed"] = loaded.get("completed", {})
                    self._data["meta"] = loaded.get("meta", {})
            except Exception:
                pass

        # Initialize CSV log with header if not exists
        if self.enable_csv and not self.log_path.exists():
            try:
                with open(self.log_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
                    writer.writeheader()
            except Exception:
                pass

        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="CheckpointWriter",
            daemon=True,
        )
        self._writer_thread.start()

    def _writer_loop(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            action, payload = item
            if action == "meta":
                self._data["meta"].update(payload)
                self._flush_json()
            elif action == "record":
                rel_key, row_dict = payload
                self._data["completed"][rel_key] = row_dict
                self._flush_json()
                if self.enable_csv:
                    self._append_csv(row_dict)

            self._queue.task_done()

    def _flush_json(self):
        tmp_path = self.ckpt_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp_path.replace(self.ckpt_path)
        except Exception:
            pass

    def _append_csv(self, row_dict: Dict[str, Any]):
        try:
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
                writer.writerow(row_dict)
        except Exception:
            pass

    def save_meta(self, **kwargs):
        """Update run metadata."""
        self._queue.put(("meta", kwargs))

    def record_detection(self, result: DetectionResult):
        """Record detection result to checkpoint and CSV log."""
        self._queue.put(("record", (result.video.rel_str, result.to_log_dict())))

    def get_completed_keys(self) -> Set[str]:
        """Return set of completed relative path keys."""
        return set(self._data.get("completed", {}).keys())

    def get_all_completed(self) -> Dict[str, Dict[str, Any]]:
        """Return copy of all completed records keyed by relative path."""
        return dict(self._data.get("completed", {}))

    def get_completed_record(self, rel_str: str) -> Optional[Dict[str, Any]]:
        """Return the dictionary record for a given relative path if present in checkpoint."""
        return self._data.get("completed", {}).get(rel_str)

    def clear(self):
        """Clear active checkpoint data and files."""
        self._data = {"completed": {}, "meta": {}}
        if self.ckpt_path.exists():
            try:
                self.ckpt_path.unlink()
            except OSError:
                pass
        if self.log_path.exists():
            try:
                self.log_path.unlink()
            except OSError:
                pass

    def flush_and_stop(self):
        """Block until all queue items are written and shutdown writer."""
        self._stop.set()
        if self._writer_thread.is_alive():
            self._writer_thread.join(timeout=3.0)
        self._flush_json()
