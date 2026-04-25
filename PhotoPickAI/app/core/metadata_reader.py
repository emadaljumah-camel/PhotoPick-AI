from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from app.utils.time_utils import parse_capture_time, to_iso_string

LOGGER = logging.getLogger(__name__)


class MetadataReader:
    """Read EXIF-like metadata using ExifTool when available."""

    def __init__(self) -> None:
        self.exiftool_path = shutil.which("exiftool")
        self._warned_missing = False

    def read_metadata(self, image_paths: list[Path]) -> dict[Path, dict[str, Any]]:
        """Read metadata for a list of image paths."""

        if not image_paths:
            return {}
        if self.exiftool_path:
            try:
                return self._read_with_exiftool(image_paths)
            except Exception:  # pragma: no cover - defensive for subprocess variability
                LOGGER.exception("ExifTool metadata extraction failed; falling back to Pillow")
        elif not self._warned_missing:
            LOGGER.warning("ExifTool not found on PATH; using Pillow/file metadata fallback")
            self._warned_missing = True
        return {path: self._fallback_metadata(path) for path in image_paths}

    def _read_with_exiftool(self, image_paths: list[Path]) -> dict[Path, dict[str, Any]]:
        command = [self.exiftool_path, "-j", "-n", *[str(path) for path in image_paths]]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
        raw_items = json.loads(completed.stdout)
        metadata_map: dict[Path, dict[str, Any]] = {}
        for item in raw_items:
            source_path = Path(item["SourceFile"])
            fallback = self._fallback_metadata(source_path)
            capture_dt = (
                parse_capture_time(item.get("DateTimeOriginal"))
                or parse_capture_time(item.get("CreateDate"))
                or datetime.fromtimestamp(source_path.stat().st_mtime)
            )
            width = int(item.get("ImageWidth") or fallback["width"])
            height = int(item.get("ImageHeight") or fallback["height"])
            orientation = self._normalize_orientation(item.get("Orientation"), width, height)
            metadata_map[source_path] = {
                "capture_time": to_iso_string(capture_dt),
                "width": width,
                "height": height,
                "orientation": orientation,
                "modified_time": fallback["modified_time"],
            }
        return metadata_map

    def _fallback_metadata(self, image_path: Path) -> dict[str, Any]:
        try:
            with Image.open(image_path) as image:
                width, height = image.size
        except OSError:
            LOGGER.exception("Failed reading image dimensions for %s", image_path)
            width, height = 0, 0
        modified = datetime.fromtimestamp(image_path.stat().st_mtime)
        orientation = self._normalize_orientation(None, width, height)
        return {
            "capture_time": to_iso_string(modified),
            "width": width,
            "height": height,
            "orientation": orientation,
            "modified_time": to_iso_string(modified),
        }

    def _normalize_orientation(self, raw_orientation: Any, width: int, height: int) -> str:
        """Normalize orientation to portrait/landscape/square."""

        if width == height and width > 0:
            return "square"
        if isinstance(raw_orientation, str):
            value = raw_orientation.lower()
            if "portrait" in value:
                return "portrait"
            if "landscape" in value:
                return "landscape"
        return "landscape" if width >= height else "portrait"
