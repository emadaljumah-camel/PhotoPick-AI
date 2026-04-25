from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from app.config import get_app_paths, load_settings
from app.ui.main_window import MainWindow
from app.utils.file_utils import ensure_directory


def configure_logging() -> None:
    """Configure simple application logging."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def ensure_workspace(paths) -> None:
    """Create required workspace directories."""

    ensure_directory(paths.workspace_dir)
    ensure_directory(paths.previews_dir)
    ensure_directory(paths.metadata_dir)
    ensure_directory(paths.similarity_dir)
    ensure_directory(paths.review_dir)


def main() -> int:
    """Application entry point."""

    configure_logging()
    paths = get_app_paths()
    ensure_workspace(paths)
    settings = load_settings(paths)

    app = QApplication(sys.argv)
    window = MainWindow(settings, paths)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
