"""
Tests for video metadata and EXIF preservation on extracted images.
"""
import datetime
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from PIL.ExifTags import Base, IFD, GPS

from static_sorter.core.exif import (
    parse_iso6709_location,
    parse_iso_datetime,
    deg_to_dms,
    build_image_exif,
    build_png_info,
)
from static_sorter.core.extractor import extract_best_frame
from static_sorter.core.media import probe_video
from static_sorter.core.models import VideoItem, VideoMetadata


class TestMetadataPreservation(unittest.TestCase):

    def test_parse_iso6709_location(self):
        # Format: +lat-lon+alt/
        lat, lon, alt = parse_iso6709_location("+37.7749-122.4194+15.000000/")
        self.assertAlmostEqual(lat, 37.7749)
        self.assertAlmostEqual(lon, -122.4194)
        self.assertAlmostEqual(alt, 15.0)

        # Format: +lat+lon/ without altitude
        lat, lon, alt = parse_iso6709_location("+28.5355+077.3910/")
        self.assertAlmostEqual(lat, 28.5355)
        self.assertAlmostEqual(lon, 77.3910)
        self.assertIsNone(alt)

        # Format: Southern and Western hemisphere
        lat, lon, alt = parse_iso6709_location("-33.8688+151.2093-5.5/")
        self.assertAlmostEqual(lat, -33.8688)
        self.assertAlmostEqual(lon, 151.2093)
        self.assertAlmostEqual(alt, -5.5)

        # Invalid / Empty
        self.assertEqual(parse_iso6709_location(None), (None, None, None))
        self.assertEqual(parse_iso6709_location(""), (None, None, None))
        self.assertEqual(parse_iso6709_location("invalid"), (None, None, None))

    def test_parse_iso_datetime(self):
        exif_str, offset, dt = parse_iso_datetime("2023-08-14T10:15:30.000000Z")
        self.assertEqual(exif_str, "2023:08:14 10:15:30")
        self.assertIsNotNone(dt)

        exif_str, offset, dt = parse_iso_datetime("2023-08-14 10:15:30")
        self.assertEqual(exif_str, "2023:08:14 10:15:30")

        exif_str, offset, dt = parse_iso_datetime("2023-08-14T10:15:30+0530")
        self.assertEqual(exif_str, "2023:08:14 10:15:30")
        self.assertEqual(offset, "+05:30")

        self.assertEqual(parse_iso_datetime(None), (None, None, None))

    def test_deg_to_dms(self):
        dms = deg_to_dms(37.7749)
        self.assertEqual(dms[0], 37.0)
        self.assertEqual(dms[1], 46.0)
        self.assertAlmostEqual(dms[2], 29.64, places=2)

    def test_build_image_exif(self):
        meta = VideoMetadata(
            creation_time="2023-08-14T10:15:30Z",
            make="Apple",
            model="iPhone 14 Pro",
            latitude=37.7749,
            longitude=-122.4194,
            altitude=15.0,
            tags={"description": "Sunset view"},
        )
        exif = build_image_exif(meta)
        self.assertEqual(exif.get(Base.Make), "Apple")
        self.assertEqual(exif.get(Base.Model), "iPhone 14 Pro")
        self.assertEqual(exif.get(Base.DateTime), "2023:08:14 10:15:30")
        self.assertEqual(exif.get(Base.ImageDescription), "Sunset view")
        self.assertEqual(exif.get(Base.Orientation), 1)

        exif_ifd = exif.get_ifd(IFD.Exif)
        self.assertEqual(exif_ifd.get(Base.DateTimeOriginal), "2023:08:14 10:15:30")

        gps_ifd = exif.get_ifd(IFD.GPSInfo)
        self.assertEqual(gps_ifd.get(GPS.GPSLatitudeRef), "N")
        self.assertEqual(gps_ifd.get(GPS.GPSLongitudeRef), "W")
        self.assertEqual(gps_ifd.get(GPS.GPSAltitudeRef), b"\x00")

    def test_build_png_info(self):
        meta = VideoMetadata(
            creation_time="2023-08-14T10:15:30Z",
            make="Sony",
            model="A7 IV",
            tags={"description": "Landscape"},
        )
        png_info = build_png_info(meta)
        self.assertIsNotNone(png_info)

    def test_end_to_end_extraction_with_metadata(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg binary not found")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            video_file = tmp_path / "sample_tagged.mp4"
            out_jpg = tmp_path / "extracted.jpg"
            out_png = tmp_path / "extracted.png"

            # Create synthetic video with ffmpeg containing complete metadata
            cmd = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=navy:s=320x240:d=1",
                "-metadata", "creation_time=2023-08-14T10:15:30.000000Z",
                "-metadata", "make=Apple",
                "-metadata", "model=iPhone 14 Pro",
                "-metadata", "location=+37.7749-122.4194+15.000000/",
                "-metadata", "title=Test Video",
                "-metadata", "description=Test Description",
                "-movflags", "use_metadata_tags",
                "-c:v", "libx264",
                str(video_file),
            ]
            proc = subprocess.run(cmd, capture_output=True)
            self.assertEqual(proc.returncode, 0, f"Failed to create video: {proc.stderr.decode('utf-8', errors='ignore')}")

            # Verify probe_video captures the metadata
            meta = probe_video(video_file)
            self.assertEqual(meta.make, "Apple")
            self.assertEqual(meta.model, "iPhone 14 Pro")
            self.assertIsNotNone(meta.creation_time)
            self.assertAlmostEqual(meta.latitude, 37.7749, places=3)
            self.assertAlmostEqual(meta.longitude, -122.4194, places=3)

            # Test 1: Extract as JPEG
            v_item = VideoItem(path=video_file, root_dir=tmp_path, rel_path=Path("sample_tagged.mp4"))
            res_jpg = extract_best_frame(v_item, out_jpg, fmt="jpg", quality=95)
            self.assertEqual(res_jpg.status, "ok", f"Extraction failed: {res_jpg.error}")
            self.assertTrue(out_jpg.exists())

            # Verify EXIF on JPEG
            with Image.open(out_jpg) as img_jpg:
                exif_jpg = img_jpg.getexif()
                self.assertEqual(exif_jpg.get(Base.Make), "Apple")
                self.assertEqual(exif_jpg.get(Base.Model), "iPhone 14 Pro")
                self.assertEqual(exif_jpg.get(Base.DateTime), "2023:08:14 10:15:30")

                # Verify default XPKeywords tags (0x9C9E)
                raw_keywords = exif_jpg.get(0x9C9E)
                self.assertIsNotNone(raw_keywords)
                decoded_keywords = raw_keywords.decode("utf-16le").rstrip("\x00")
                self.assertIn("static-video", decoded_keywords)
                self.assertIn("extracted-frame", decoded_keywords)

                exif_ifd = exif_jpg.get_ifd(IFD.Exif)
                self.assertEqual(exif_ifd.get(Base.DateTimeOriginal), "2023:08:14 10:15:30")

                gps_ifd = exif_jpg.get_ifd(IFD.GPSInfo)
                self.assertEqual(gps_ifd.get(GPS.GPSLatitudeRef), "N")
                self.assertEqual(gps_ifd.get(GPS.GPSLongitudeRef), "W")

            # Verify filesystem modification timestamp
            stat_jpg = out_jpg.stat()
            expected_dt = datetime.datetime(2023, 8, 14, 10, 15, 30)
            self.assertEqual(
                datetime.datetime.fromtimestamp(stat_jpg.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                expected_dt.strftime("%Y-%m-%d %H:%M:%S"),
            )

            # Test 2: Extract as PNG with custom tags
            custom_tags = ["favorite", "vacation-2023", "best-frame"]
            res_png = extract_best_frame(v_item, out_png, fmt="png", tags=custom_tags)
            self.assertEqual(res_png.status, "ok", f"PNG extraction failed: {res_png.error}")
            self.assertTrue(out_png.exists())

            with Image.open(out_png) as img_png:
                exif_png = img_png.getexif()
                self.assertEqual(exif_png.get(Base.Make), "Apple")
                self.assertEqual(exif_png.get(Base.Model), "iPhone 14 Pro")

                # Verify custom tags in EXIF
                raw_kw = exif_png.get(0x9C9E)
                self.assertIsNotNone(raw_kw)
                decoded_kw = raw_kw.decode("utf-16le").rstrip("\x00")
                self.assertIn("favorite", decoded_kw)
                self.assertIn("vacation-2023", decoded_kw)

                # Verify custom tags in PNG text info
                self.assertIn("Keywords", img_png.info)
                self.assertIn("favorite", img_png.info["Keywords"])


if __name__ == "__main__":
    unittest.main()

