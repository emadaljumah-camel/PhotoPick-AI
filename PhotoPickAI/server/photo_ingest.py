from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from app.core.models import ImageRecord
from app.core.sharpness import estimate_sharpness
from server.models import CreateJobRequest, JobPhoto


LOGGER = logging.getLogger(__name__)
# DateTimeOriginal and DateTimeDigitized are camera/scanner times.
# Generic DateTime (306) can be rewritten to preview export time.
EXIF_CAPTURE_TIME_TAGS = (36867, 36868)


def preview_path(job_dir: Path, photo: JobPhoto) -> Path:
    return job_dir / "previews" / photo.preview_filename


def build_image_records(job_dir: Path, request: CreateJobRequest) -> tuple[list[ImageRecord], list[dict]]:
    records: list[ImageRecord] = []
    skipped = list(request.skipped)
    for photo in request.photos:
        path = preview_path(job_dir, photo)
        if not path.exists():
            skipped.append({"photo_id": photo.photo_id, "error": "Missing uploaded preview"})
            continue
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                embedded_capture_time = _embedded_capture_time(image)
        except OSError as exc:
            skipped.append({"photo_id": photo.photo_id, "error": f"Cannot open preview: {exc}"})
            continue
        capture_time, capture_time_source = _capture_time(
            photo.capture_time,
            embedded_capture_time,
            path,
        )
        records.append(
            ImageRecord(
                id=photo.photo_id,
                filename=photo.filename or photo.preview_filename or photo.photo_id,
                path=str(path),
                file_size=path.stat().st_size,
                width=width,
                height=height,
                orientation="landscape" if width >= height else "portrait",
                capture_time=capture_time,
                capture_time_source=capture_time_source,
                preview_path=str(path),
                sharpness_score=estimate_sharpness(path),
            )
        )
    return records, skipped


def _capture_time(value: str, embedded_value: str, path: Path) -> tuple[str, str]:
    for candidate, source in (
        (value, "manifest"),
        (embedded_value, "embedded_exif"),
    ):
        parsed = _parse_capture_time(candidate)
        if parsed:
            return parsed, source

    LOGGER.warning(
        "No manifest or embedded EXIF capture time for preview=%s; "
        "capture time remains unknown",
        path,
    )
    return "", "unknown"


def _parse_capture_time(value: str) -> str:
    if not value:
        return ""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        pass

    try:
        return datetime.strptime(normalized, "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return ""


def _embedded_capture_time(image: Image.Image) -> str:
    exif = image.getexif()
    for tag in EXIF_CAPTURE_TIME_TAGS:
        value = exif.get(tag)
        if value:
            return str(value)
    return ""
