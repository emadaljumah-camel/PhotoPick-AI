from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from app.core.models import DuplicateGroup
from app.utils.file_utils import ensure_directory

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MoveSummary:
    """Summary of original photo moves after automated review."""

    selected_dir: str
    rejected_dir: str
    selected: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class PhotoMover:
    """Move reviewed original photos into selected/rejected folders."""

    def __init__(self, source_folder: Path) -> None:
        self.source_folder = source_folder.resolve()
        self.selected_dir = ensure_directory(self.source_folder / "Selected Photos")
        self.rejected_dir = ensure_directory(self.source_folder / "Rejected Photos")

    def move_grouped_photos(self, groups: list[DuplicateGroup]) -> MoveSummary:
        """Move selected grouped originals to Selected Photos and alternates to Rejected Photos."""

        summary = MoveSummary(str(self.selected_dir), str(self.rejected_dir))
        moved_paths: set[Path] = set()

        for group in groups:
            selected = set(group.user_selected)
            for image in group.images:
                source_path = Path(image.path).resolve()
                if source_path in moved_paths:
                    summary.skipped.append(image.filename)
                    continue
                if not self._is_safe_source(source_path):
                    summary.skipped.append(image.filename)
                    LOGGER.warning("Skipping unsafe source outside selected folder: %s", source_path)
                    continue
                if not source_path.exists():
                    summary.skipped.append(image.filename)
                    LOGGER.warning("Skipping missing source file: %s", source_path)
                    continue

                target_dir = self.selected_dir if image.filename in selected else self.rejected_dir
                target_path = self._unique_target_path(target_dir / source_path.name)
                shutil.move(str(source_path), str(target_path))
                image.path = str(target_path)
                moved_paths.add(source_path)
                if image.filename in selected:
                    summary.selected.append(str(target_path))
                else:
                    summary.rejected.append(str(target_path))

        return summary

    def _is_safe_source(self, path: Path) -> bool:
        try:
            path.relative_to(self.source_folder)
        except ValueError:
            return False
        return path.parent == self.source_folder

    def _unique_target_path(self, target_path: Path) -> Path:
        if not target_path.exists():
            return target_path

        stem = target_path.stem
        suffix = target_path.suffix
        for index in range(1, 10000):
            candidate = target_path.with_name(f"{stem}_{index:03d}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not create a unique filename for {target_path}")
