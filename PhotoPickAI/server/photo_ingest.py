from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from app.core.models import ImageRecord
from app.core.sharpness import estimate_sharpness
from server.models import CreateJobRequest, JobPhoto


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
        except OSError as exc:
            skipped.append({"photo_id": photo.photo_id, "error": f"Cannot open preview: {exc}"})
            continue
        capture_time = _capture_time(photo.capture_time, path)
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
                preview_path=str(path),
                sharpness_score=estimate_sharpness(path),
            )
        )
    return records, skipped


def _capture_time(value: str, path: Path) -> str:
    if value:
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
