import json
import shutil
import tempfile
import unittest
from pathlib import Path

from static_sorter.core.checkpoint import CheckpointManager
from static_sorter.core.models import VideoItem, VideoMetadata, DetectionResult
from static_sorter.core.pipeline import PipelineOrchestrator
from static_sorter.utils.file_ops import safe_move_relative


class TestCheckpointMoveFlow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_checkpoint_reconstruction(self):
        video = VideoItem(path=self.root / "clip.mp4", root_dir=self.root, rel_path=Path("clip.mp4"))
        meta = VideoMetadata(duration_s=12.5, width=1920, height=1080, aspect_ratio="16:9", has_audio=True)
        res = DetectionResult(
            video=video,
            metadata=meta,
            global_motion_score=1.2,
            active_zone_ratio=0.1,
            heuristic_score=0.9,
            final_confidence=0.88,
            decision="static",
        )
        d = res.to_log_dict()
        reconstructed = DetectionResult.from_log_dict(video, d)
        self.assertEqual(reconstructed.decision, "static")
        self.assertEqual(reconstructed.metadata.width, 1920)
        self.assertEqual(reconstructed.metadata.duration_s, 12.5)
        self.assertTrue(reconstructed.metadata.has_audio)
        self.assertAlmostEqual(reconstructed.final_confidence, 0.88)

    def test_safe_move_relative_nonexistent(self):
        non_existent = self.root / "missing.mp4"
        dst_category = self.root / "static"
        with self.assertRaises(FileNotFoundError):
            safe_move_relative(non_existent, self.root, dst_category)

    def test_run_detection_reads_checkpoint_and_moves(self):
        # Create a dummy video file
        video_file = self.root / "sample.mp4"
        video_file.write_bytes(b"dummy video data")

        # Manually populate checkpoint as if a previous non-move run occurred
        ckpt = CheckpointManager(self.root)
        v_item = VideoItem(path=video_file, root_dir=self.root, rel_path=Path("sample.mp4"))
        res = DetectionResult(
            video=v_item,
            metadata=VideoMetadata(duration_s=5.0, width=1280, height=720),
            decision="static",
            final_confidence=0.95,
        )
        ckpt.record_detection(res)
        ckpt.flush_and_stop()

        from static_sorter.core.config import CHECKPOINT_FILENAME
        # Verify checkpoint file exists and has the entry
        self.assertTrue((self.root / CHECKPOINT_FILENAME).exists())

        # Now run detection with move=True
        orch = PipelineOrchestrator()
        summary = orch.run_detection(folder=self.root, move=True)

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["cached"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["static"], 1)

        # Video should have moved to static/sample.mp4
        expected_dst = self.root / "static" / "sample.mp4"
        self.assertTrue(expected_dst.exists())
        self.assertFalse(video_file.exists())
        self.assertIn("sample.mp4", summary["moved_paths"])
        self.assertEqual(summary["moved_paths"]["sample.mp4"], expected_dst)

    def test_run_detection_handles_missing_file_safely(self):
        # Checkpoint exists for a file that does not exist on disk
        ckpt = CheckpointManager(self.root)
        v_item = VideoItem(path=self.root / "ghost.mp4", root_dir=self.root, rel_path=Path("ghost.mp4"))
        res = DetectionResult(
            video=v_item,
            metadata=VideoMetadata(),
            decision="static",
        )
        ckpt.record_detection(res)
        ckpt.flush_and_stop()

        # Run detection with move=True
        orch = PipelineOrchestrator()
        summary = orch.run_detection(folder=self.root, move=True)

        # Discovered total is 0 since ghost.mp4 is not on disk
        self.assertEqual(summary["total"], 0)
        self.assertEqual(len(summary["moved_paths"]), 0)

    def test_partial_cached_and_nested_subfolders(self):
        # Create nested folders and files
        sub = self.root / "vacation" / "2024"
        sub.mkdir(parents=True, exist_ok=True)
        v1 = sub / "video1.mp4"
        v1.write_bytes(b"video 1 content")

        v2 = sub / "video2.mp4"
        v2.write_bytes(b"video 2 content")

        # Cache video1 as dynamic
        ckpt = CheckpointManager(self.root)
        v1_item = VideoItem(path=v1, root_dir=self.root, rel_path=Path("vacation/2024/video1.mp4"))
        res1 = DetectionResult(
            video=v1_item,
            metadata=VideoMetadata(duration_s=10.0, width=1920, height=1080),
            decision="dynamic",
        )
        ckpt.record_detection(res1)
        ckpt.flush_and_stop()

        # Cache video2 as static
        ckpt2 = CheckpointManager(self.root)
        v2_item = VideoItem(path=v2, root_dir=self.root, rel_path=Path("vacation/2024/video2.mp4"))
        res2 = DetectionResult(
            video=v2_item,
            metadata=VideoMetadata(duration_s=8.0, width=1920, height=1080),
            decision="static",
        )
        ckpt2.record_detection(res2)
        ckpt2.flush_and_stop()

        orch = PipelineOrchestrator()
        summary = orch.run_detection(folder=self.root, recursive=True, move=True)

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["cached"], 2)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["dynamic"], 1)
        self.assertEqual(summary["static"], 1)

        # Check preserved folder structure
        self.assertTrue((self.root / "dynamic" / "vacation" / "2024" / "video1.mp4").exists())
        self.assertTrue((self.root / "static" / "vacation" / "2024" / "video2.mp4").exists())
        self.assertFalse(v1.exists())
        self.assertFalse(v2.exists())


if __name__ == "__main__":
    unittest.main()
