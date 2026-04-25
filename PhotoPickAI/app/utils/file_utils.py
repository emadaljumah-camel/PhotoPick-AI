from __future__ import annotations

import hashlib
from pathlib import Path


SUPPORTED_IMAGE_SUFFIXES = {".jpg", ".jpeg"}


def is_supported_image(path: Path) -> bool:
    """Return True when the path is a supported top-level image file."""

    return path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES


def ensure_directory(path: Path) -> Path:
    """Create a directory if needed and return it."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def file_id_for_path(path: Path) -> str:
    """Generate a stable image id from the full path."""

    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    return digest[:12]


def cache_key_for_folder(folder: Path) -> str:
    """Generate a stable cache key for a source folder."""

    digest = hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()
    return digest[:16]
