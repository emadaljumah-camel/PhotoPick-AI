from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps

from app.utils.file_utils import ensure_directory

LOGGER = logging.getLogger(__name__)


class PreviewGenerator:
    """Generate cached JPG previews for the UI."""

    def __init__(self, preview_root: Path, long_edge: int = 512) -> None:
        self.preview_root = ensure_directory(preview_root)
        self.long_edge = long_edge

    def build_preview(self, source_path: Path, cache_name: str) -> Path:
        """Create or reuse a preview image for the given source image."""

        target_path = self.preview_root / f"{cache_name}.jpg"
        if target_path.exists() and target_path.stat().st_mtime >= source_path.stat().st_mtime:
            return target_path

        try:
            with Image.open(source_path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((self.long_edge, self.long_edge), Image.Resampling.LANCZOS)
                image.save(target_path, format="JPEG", quality=88, optimize=True)
        except OSError:
            LOGGER.exception("Failed generating preview for %s", source_path)
        return target_path
