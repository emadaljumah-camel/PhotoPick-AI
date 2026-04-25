from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from app.core.metadata_reader import MetadataReader
from app.core.models import ImageRecord
from app.core.preview_generator import PreviewGenerator
from app.utils.file_utils import file_id_for_path, is_supported_image

LOGGER = logging.getLogger(__name__)


class FolderScanner:
    """Scan a single folder for supported image files."""

    def __init__(self, metadata_reader: MetadataReader, preview_generator: PreviewGenerator) -> None:
        self.metadata_reader = metadata_reader
        self.preview_generator = preview_generator

    def scan_folder(
        self,
        folder: Path,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[ImageRecord]:
        """Scan only the selected folder and return image records."""

        folder = folder.resolve()
        image_paths = sorted(path for path in folder.iterdir() if is_supported_image(path))
        metadata_map = self.metadata_reader.read_metadata(image_paths)
        records: list[ImageRecord] = []

        total = len(image_paths) or 1
        for index, image_path in enumerate(image_paths, start=1):
            metadata = metadata_map.get(image_path) or self.metadata_reader._fallback_metadata(image_path)
            image_id = file_id_for_path(image_path)
            preview_path = self.preview_generator.build_preview(image_path, image_id)
            record = ImageRecord(
                id=image_id,
                filename=image_path.name,
                path=str(image_path),
                file_size=image_path.stat().st_size,
                width=int(metadata["width"]),
                height=int(metadata["height"]),
                orientation=str(metadata["orientation"]),
                capture_time=str(metadata["capture_time"]),
                preview_path=str(preview_path),
                modified_time=str(metadata["modified_time"]),
            )
            records.append(record)
            if progress_callback:
                progress_callback(index, total, f"Scanned {image_path.name}")

        LOGGER.info("Scanned %s JPG/JPEG files from %s", len(records), folder)
        return records
