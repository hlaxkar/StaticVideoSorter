"""
EXIF and metadata preservation engine for extracted image frames.
Constructs standard EXIF IFDs (0th IFD, Exif IFD, GPS IFD), PNG chunks,
and synchronizes filesystem timestamps.
"""
import datetime
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image
from PIL.ExifTags import Base, IFD, GPS
from PIL.PngImagePlugin import PngInfo

from static_sorter.core.config import DEFAULT_IMAGE_TAGS
from static_sorter.core.models import VideoMetadata


def parse_iso6709_location(loc_str: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Parses ISO 6709 geographic location string into (latitude, longitude, altitude).
    Handles formats like:
      +37.7749-122.4194/
      +37.7749-122.4194+015.000/
      +28.5355+077.3910/
      -33.8688+151.2093-5.5/
    """
    if not loc_str or not isinstance(loc_str, str):
        return None, None, None

    cleaned = loc_str.strip().rstrip("/")
    if not cleaned:
        return None, None, None

    # Matches (+/-lat)(+/-lon)(+/-alt optional)
    pattern = r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?"
    match = re.match(pattern, cleaned)
    if not match:
        return None, None, None

    try:
        lat = float(match.group(1))
        lon = float(match.group(2))
        alt = float(match.group(3)) if match.group(3) is not None else None
        return lat, lon, alt
    except (ValueError, TypeError):
        return None, None, None


def parse_iso_datetime(dt_str: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[datetime.datetime]]:
    """
    Parses an ISO 8601 or common video creation timestamp into:
      (exif_formatted_str 'YYYY:MM:DD HH:MM:SS', tz_offset_str '+HH:MM', datetime_obj)
    """
    if not dt_str or not isinstance(dt_str, str):
        return None, None, None

    cleaned = dt_str.strip()
    if not cleaned:
        return None, None, None

    # Formats to try
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.datetime.strptime(cleaned, fmt)
            exif_str = dt.strftime("%Y:%m:%d %H:%M:%S")
            offset = None
            if dt.tzinfo is not None:
                offset_raw = dt.strftime("%z")
                if offset_raw and len(offset_raw) == 5:
                    offset = f"{offset_raw[:3]}:{offset_raw[3:]}"
            return exif_str, offset, dt
        except ValueError:
            pass

    # Regex fallback for YYYY-MM-DD HH:MM:SS
    match = re.match(r"(\d{4})[-:.](\d{2})[-:.](\d{2})[T ](\d{2}):(\d{2}):(\d{2})", cleaned)
    if match:
        y, m, d, hh, mm, ss = match.groups()
        exif_str = f"{y}:{m}:{d} {hh}:{mm}:{ss}"
        try:
            dt_obj = datetime.datetime(int(y), int(m), int(d), int(hh), int(mm), int(ss))
        except ValueError:
            dt_obj = None
        return exif_str, None, dt_obj

    return None, None, None


def deg_to_dms(deg_float: float) -> Tuple[float, float, float]:
    """Converts decimal degrees to EXIF GPS tuple (degrees, minutes, seconds)."""
    deg = abs(deg_float)
    d = int(deg)
    m_float = (deg - d) * 60.0
    m = int(m_float)
    s = (m_float - m) * 60.0
    return (float(d), float(m), round(s, 4))


def build_image_exif(
    meta: VideoMetadata,
    video_path: Optional[Path] = None,
    tags: Optional[List[str]] = None,
) -> Image.Exif:
    """
    Constructs a Pillow Exif object populated with camera make/model,
    creation datetime, GPS coordinates, orientation, software, description,
    and keyword tags (XPKeywords, UserComment).
    """
    exif = Image.Exif()

    # Orientation: 1 (Normal / Top-Left) since extracted frame is already decoded right-side up
    exif[Base.Orientation] = 1

    # Camera Make & Model
    meta_tags = meta.tags or {}
    make = (
        meta.make
        or meta_tags.get("make")
        or meta_tags.get("com.apple.quicktime.make")
        or meta_tags.get("camera_make")
        or meta_tags.get("android.manufacturer")
    )
    if make:
        exif[Base.Make] = str(make).strip()

    model = (
        meta.model
        or meta_tags.get("model")
        or meta_tags.get("com.apple.quicktime.model")
        or meta_tags.get("camera_model")
        or meta_tags.get("android.model")
    )
    if model:
        exif[Base.Model] = str(model).strip()

    # Software / Encoder
    software = (
        meta_tags.get("software")
        or meta_tags.get("encoder")
        or meta_tags.get("com.apple.quicktime.software")
        or "StaticVideoSorter"
    )
    if software:
        exif[Base.Software] = str(software).strip()

    # Keyword tags: XPKeywords (0x9C9E), XPSubject (0x9C9B), XPComment (0x9C9C)
    keyword_list: List[str] = list(tags if tags is not None else DEFAULT_IMAGE_TAGS)
    if "keywords" in meta_tags:
        for kw in str(meta_tags["keywords"]).split(","):
            cleaned_kw = kw.strip()
            if cleaned_kw and cleaned_kw not in keyword_list:
                keyword_list.append(cleaned_kw)

    # Description & Copyright
    description = (
        meta_tags.get("description")
        or meta_tags.get("title")
        or meta_tags.get("comment")
        or meta_tags.get("synopsis")
    )
    
    # Populate ImageDescription (displayed directly in Immich / photo manager Description box)
    if description:
        exif[Base.ImageDescription] = str(description).strip()
    elif keyword_list:
        exif[Base.ImageDescription] = ", ".join(keyword_list)

    copyright_info = meta_tags.get("copyright") or meta_tags.get("artist") or meta_tags.get("author")
    if copyright_info:
        exif[Base.Copyright] = str(copyright_info).strip()

    if keyword_list:
        kw_str = "; ".join(keyword_list)
        exif[0x9C9E] = kw_str.encode("utf-16le") + b"\x00\x00"
        exif[0x9C9B] = kw_str.encode("utf-16le") + b"\x00\x00"

    if description:
        exif[0x9C9C] = str(description).encode("utf-16le") + b"\x00\x00"
    elif keyword_list:
        exif[0x9C9C] = (", ".join(keyword_list)).encode("utf-16le") + b"\x00\x00"

    # Creation Time
    creation_raw = (
        meta.creation_time
        or meta_tags.get("creation_time")
        or meta_tags.get("com.apple.quicktime.creationdate")
        or meta_tags.get("date")
    )
    exif_dt, tz_offset, dt_obj = parse_iso_datetime(creation_raw)

    exif_ifd = exif.get_ifd(IFD.Exif)
    if exif_dt:
        exif[Base.DateTime] = exif_dt
        exif_ifd[Base.DateTimeOriginal] = exif_dt
        exif_ifd[Base.DateTimeDigitized] = exif_dt
        if tz_offset:
            exif_ifd[Base.OffsetTime] = tz_offset
            exif_ifd[Base.OffsetTimeOriginal] = tz_offset
            exif_ifd[Base.OffsetTimeDigitized] = tz_offset

    # UserComment in Exif IFD
    user_comment_text = description or (", ".join(keyword_list) if keyword_list else "StaticVideoSorter")
    exif_ifd[Base.UserComment] = b"UNICODE\x00" + str(user_comment_text).encode("utf-8")

    # GPS Info
    lat = meta.latitude
    lon = meta.longitude
    alt = meta.altitude

    if lat is None or lon is None:
        loc_str = meta.location or meta_tags.get("location") or meta_tags.get("location-eng") or meta_tags.get("com.apple.quicktime.location.ISO6709")
        if loc_str:
            p_lat, p_lon, p_alt = parse_iso6709_location(loc_str)
            if p_lat is not None and p_lon is not None:
                lat, lon = p_lat, p_lon
                if alt is None and p_alt is not None:
                    alt = p_alt

    if lat is not None and lon is not None:
        gps_ifd = exif.get_ifd(IFD.GPSInfo)
        gps_ifd[GPS.GPSLatitudeRef] = "N" if lat >= 0 else "S"
        gps_ifd[GPS.GPSLatitude] = deg_to_dms(lat)
        gps_ifd[GPS.GPSLongitudeRef] = "E" if lon >= 0 else "W"
        gps_ifd[GPS.GPSLongitude] = deg_to_dms(lon)

        if alt is not None:
            gps_ifd[GPS.GPSAltitudeRef] = b"\x00" if alt >= 0 else b"\x01"
            gps_ifd[GPS.GPSAltitude] = float(abs(alt))

        if dt_obj is not None:
            gps_ifd[GPS.GPSDateStamp] = dt_obj.strftime("%Y:%m:%d")
            gps_ifd[GPS.GPSTimeStamp] = (
                float(dt_obj.hour),
                float(dt_obj.minute),
                float(dt_obj.second),
            )

    return exif


def build_png_info(
    meta: VideoMetadata,
    video_path: Optional[Path] = None,
    tags: Optional[List[str]] = None,
) -> PngInfo:
    """Constructs PNG textual metadata chunks including keyword tags."""
    png_info = PngInfo()
    meta_tags = meta.tags or {}

    creation_raw = (
        meta.creation_time
        or meta_tags.get("creation_time")
        or meta_tags.get("com.apple.quicktime.creationdate")
        or meta_tags.get("date")
    )
    if creation_raw:
        png_info.add_text("Creation Time", str(creation_raw))

    make = meta.make or meta_tags.get("make") or meta_tags.get("com.apple.quicktime.make")
    if make:
        png_info.add_text("Make", str(make))

    model = meta.model or meta_tags.get("model") or meta_tags.get("com.apple.quicktime.model")
    if model:
        png_info.add_text("Model", str(model))

    software = meta_tags.get("software") or meta_tags.get("encoder") or "StaticVideoSorter"
    if software:
        png_info.add_text("Software", str(software))

    desc = meta_tags.get("description") or meta_tags.get("title") or meta_tags.get("comment")
    if desc:
        png_info.add_text("Description", str(desc))

    keyword_list: List[str] = list(tags if tags is not None else DEFAULT_IMAGE_TAGS)
    if keyword_list:
        png_info.add_text("Keywords", ", ".join(keyword_list))
        png_info.add_text("Comment", ", ".join(keyword_list))

    return png_info


def run_exiftool_copy(source_video: Path, dest_image: Path) -> bool:
    """
    Supplementary pass using exiftool if available on the system to copy
    deep vendor-specific maker notes. Returns True if successful.
    """
    if shutil.which("exiftool") is None:
        return False

    try:
        cmd = [
            "exiftool",
            "-tagsFromFile",
            str(source_video),
            "-all:all>all:all",
            "-overwrite_original",
            str(dest_image),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
        return proc.returncode == 0
    except Exception:
        return False


def build_xmp_packet(meta: VideoMetadata, tags: Optional[List[str]] = None) -> bytes:
    """Constructs an Adobe XMP packet with Dublin Core subject and description for Immich / Lightroom."""
    keyword_list: List[str] = list(tags if tags is not None else DEFAULT_IMAGE_TAGS)
    meta_tags = meta.tags or {}
    if "keywords" in meta_tags:
        for kw in str(meta_tags["keywords"]).split(","):
            cleaned_kw = kw.strip()
            if cleaned_kw and cleaned_kw not in keyword_list:
                keyword_list.append(cleaned_kw)

    desc_text = (
        meta_tags.get("description")
        or meta_tags.get("title")
        or (", ".join(keyword_list) if keyword_list else "")
    )

    def _escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    bag_items = "".join(f"<rdf:li>{_escape(t)}</rdf:li>" for t in keyword_list)
    xmp_str = f"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:lr="http://lightroom.adobe.com/xap/1.0/">
   <dc:description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{_escape(str(desc_text))}</rdf:li>
    </rdf:Alt>
   </dc:description>
   <dc:subject>
    <rdf:Bag>
     {bag_items}
    </rdf:Bag>
   </dc:subject>
   <lr:hierarchicalSubject>
    <rdf:Bag>
     {bag_items}
    </rdf:Bag>
   </lr:hierarchicalSubject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""
    return xmp_str.encode("utf-8")


def save_image_with_metadata(
    frame_bgr: np.ndarray,
    output_path: Path,
    source_video_path: Path,
    meta: VideoMetadata,
    fmt: str = "jpg",
    quality: int = 95,
    tags: Optional[List[str]] = None,
) -> bool:
    """
    Saves extracted OpenCV frame (BGR numpy array) to output_path with:
    1. EXIF metadata (Make, Model, DateTimeOriginal, GPSInfo, Orientation, Software, XPKeywords, ImageDescription)
    2. Adobe XMP packet (dc:subject, dc:description, lr:hierarchicalSubject) for Immich / Lightroom
    3. PNG textual metadata chunks if output format is PNG
    4. Filesystem timestamps (os.utime) synced to source video
    5. Optional exiftool supplementary copy if installed
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert BGR (OpenCV) to RGB (Pillow)
    if len(frame_bgr.shape) == 3:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    else:
        frame_rgb = frame_bgr

    pil_img = Image.fromarray(frame_rgb)
    exif = build_image_exif(meta, source_video_path, tags=tags)
    xmp_bytes = build_xmp_packet(meta, tags=tags)

    fmt_lower = fmt.lower()
    if fmt_lower in ("jpg", "jpeg"):
        pil_img.save(
            str(output_path),
            format="JPEG",
            quality=int(quality),
            exif=exif,
            xmp=xmp_bytes,
            subsampling=0,
        )
    elif fmt_lower == "png":
        png_info = build_png_info(meta, source_video_path, tags=tags)
        pil_img.save(
            str(output_path),
            format="PNG",
            exif=exif,
            pnginfo=png_info,
            compress_level=3,
        )
    elif fmt_lower == "webp":
        pil_img.save(
            str(output_path),
            format="WEBP",
            quality=int(quality),
            exif=exif,
        )
    else:
        pil_img.save(str(output_path), exif=exif)

    # Supplementary pass with exiftool if available
    run_exiftool_copy(source_video_path, output_path)

    # Synchronize filesystem timestamps (mtime and atime)
    try:
        src_stat = source_video_path.stat()
        creation_raw = (
            meta.creation_time
            or (meta.tags or {}).get("creation_time")
            or (meta.tags or {}).get("com.apple.quicktime.creationdate")
        )
        _, _, dt_obj = parse_iso_datetime(creation_raw)
        if dt_obj is not None:
            ts = dt_obj.timestamp()
            os.utime(output_path, (src_stat.st_atime, ts))
        else:
            os.utime(output_path, (src_stat.st_atime, src_stat.st_mtime))
    except Exception:
        pass

    return output_path.exists() and output_path.stat().st_size > 0
