from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.config import AppPaths, AppSettings, save_settings
from app.core.duplicate_grouper import DuplicateGrouper
from app.core.metadata_reader import MetadataReader
from app.core.models import ReviewDecision
from app.core.photo_mover import PhotoMover
from app.core.preview_generator import PreviewGenerator
from app.core.review_store import ReviewStore
from app.core.scanner import FolderScanner
from app.core.similarity_engine import SimilarityEngine
from app.core.vision_llm_reviewer import VisionLLMReviewer
from app.ui.duplicate_review_widget import DuplicateReviewWidget
from app.ui.settings_panel import SettingsPanel
from app.utils.file_utils import cache_key_for_folder, ensure_directory

LOGGER = logging.getLogger(__name__)


class ProcessingWorker(QObject):
    """Background worker for scanning and grouping."""

    progressChanged = Signal(int, str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, folder: str, settings: AppSettings, paths: AppPaths) -> None:
        super().__init__()
        self.folder = Path(folder)
        self.settings = settings
        self.paths = paths

    def run(self) -> None:
        try:
            cache_key = cache_key_for_folder(self.folder)
            preview_dir = ensure_directory(self.paths.previews_dir / cache_key)
            metadata_store = ReviewStore(self.paths.metadata_dir)
            similarity_store = ReviewStore(self.paths.similarity_dir)
            review_store = ReviewStore(self.paths.review_dir)

            metadata_reader = MetadataReader()
            preview_generator = PreviewGenerator(preview_dir, self.settings.preview_long_edge)
            scanner = FolderScanner(metadata_reader, preview_generator)
            similarity_engine = SimilarityEngine(self.settings)
            grouper = DuplicateGrouper(self.settings)

            self.progressChanged.emit(5, "Scanning folder")
            records = scanner.scan_folder(self.folder, self._scanner_progress)
            metadata_store.write_metadata_cache(cache_key, [record.to_dict() for record in records])

            self.progressChanged.emit(55, "Computing similarity")
            edges = similarity_engine.analyze_records(records, self._analysis_progress)

            self.progressChanged.emit(90, "Building groups")
            groups = grouper.build_groups(records, edges, self._grouping_progress)
            saved_decisions = review_store.read_review_json(f"{cache_key}_review")
            groups = review_store.restore_group_review_state(groups, saved_decisions)
            similarity_store.write_groups(f"{cache_key}_groups", groups)

            self.progressChanged.emit(100, "Processing complete")
            self.finished.emit(
                {
                    "folder": str(self.folder),
                    "cache_key": cache_key,
                    "records": records,
                    "groups": groups,
                }
            )
        except Exception as exc:  # pragma: no cover - UI error surface
            LOGGER.exception("Processing failed")
            self.failed.emit(str(exc))

    def _scanner_progress(self, index: int, total: int, message: str) -> None:
        progress = 5 + int((index / total) * 45)
        self.progressChanged.emit(progress, message)

    def _analysis_progress(self, index: int, total: int, message: str) -> None:
        progress = 55 + int((index / total) * 30)
        self.progressChanged.emit(progress, message)

    def _grouping_progress(self, index: int, total: int, message: str) -> None:
        progress = 90 + int((index / total) * 9)
        self.progressChanged.emit(progress, message)


class ProcessPhotosWorker(QObject):
    """Background worker for scan, Vision LLM review, persistence, and moving originals."""

    progressChanged = Signal(int, str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, folder: str, settings: AppSettings, paths: AppPaths) -> None:
        super().__init__()
        self.folder = Path(folder)
        self.settings = settings
        self.paths = paths

    def run(self) -> None:
        try:
            cache_key = cache_key_for_folder(self.folder)
            preview_dir = ensure_directory(self.paths.previews_dir / cache_key)
            metadata_store = ReviewStore(self.paths.metadata_dir)
            similarity_store = ReviewStore(self.paths.similarity_dir)
            review_store = ReviewStore(self.paths.review_dir)

            metadata_reader = MetadataReader()
            preview_generator = PreviewGenerator(preview_dir, self.settings.preview_long_edge)
            scanner = FolderScanner(metadata_reader, preview_generator)
            similarity_engine = SimilarityEngine(self.settings)
            grouper = DuplicateGrouper(self.settings)

            self.progressChanged.emit(5, "Scanning folder")
            records = scanner.scan_folder(self.folder, self._scanner_progress)
            metadata_store.write_metadata_cache(cache_key, [record.to_dict() for record in records])

            self.progressChanged.emit(45, "Computing similarity")
            edges = similarity_engine.analyze_records(records, self._analysis_progress)

            self.progressChanged.emit(68, "Building groups")
            groups = grouper.build_groups(records, edges, self._grouping_progress)

            if groups:
                reviewer = VisionLLMReviewer(self.settings)
                multi_image_groups = [group for group in groups if len(group.images) > 1]
                total = len(multi_image_groups) or 1
                reviewed_count = 0
                for group in groups:
                    if len(group.images) == 1:
                        filename = group.images[0].filename
                        group.user_selected = [filename]
                        group.vision_llm_selected = [filename]
                        group.vision_llm_reason = "Single-photo group selected automatically; Vision LLM was not needed."
                        group.vision_llm_subgroups = [
                            {
                                "name": "Single photo",
                                "images": [filename],
                                "representative": filename,
                                "reason": "Only one photo in this group.",
                            }
                        ]
                        group.status = "Reviewed"
                        continue

                    reviewed_count += 1
                    self.progressChanged.emit(
                        72 + int((reviewed_count - 1) / total * 20),
                        f"Vision LLM reviewing {group.group_id}",
                    )
                    try:
                        result = reviewer.review_group(group)
                        group.vision_llm_selected = result.selected_filenames
                        group.vision_llm_reason = result.reason
                        group.vision_llm_subgroups = result.subgroups
                        group.user_selected = result.selected_filenames
                    except Exception as exc:
                        self._apply_vision_fallback(group, str(exc))
                    group.status = "Reviewed"

            decisions = [ReviewDecision.from_group(group) for group in groups]
            review_store.write_review_json(f"{cache_key}_review", decisions)
            review_store.write_review_csv(f"{cache_key}_review", decisions)

            self.progressChanged.emit(95, "Moving reviewed group photos")
            move_summary = PhotoMover(self.folder).move_grouped_photos(groups)
            similarity_store.write_groups(f"{cache_key}_groups", groups)

            self.progressChanged.emit(100, "Process Photos complete")
            self.finished.emit(
                {
                    "folder": str(self.folder),
                    "cache_key": cache_key,
                    "records": records,
                    "groups": groups,
                    "move_summary": move_summary,
                }
            )
        except Exception as exc:  # pragma: no cover - UI error surface
            LOGGER.exception("Process Photos failed")
            self.failed.emit(str(exc))

    def _scanner_progress(self, index: int, total: int, message: str) -> None:
        progress = 5 + int((index / total) * 35)
        self.progressChanged.emit(progress, message)

    def _analysis_progress(self, index: int, total: int, message: str) -> None:
        progress = 45 + int((index / total) * 20)
        self.progressChanged.emit(progress, message)

    def _grouping_progress(self, index: int, total: int, message: str) -> None:
        progress = 68 + int((index / total) * 3)
        self.progressChanged.emit(progress, message)

    def _apply_vision_fallback(self, group, error_message: str) -> None:
        selected = group.suggested_best or (group.images[0].filename if group.images else "")
        group.user_selected = [selected] if selected else []
        group.vision_llm_selected = list(group.user_selected)
        reason = f"Vision LLM failed for this group; used suggested/local fallback. {error_message}"
        if len(reason) > 500:
            reason = f"{reason[:500]}..."
        group.vision_llm_reason = reason
        group.vision_llm_subgroups = [
            {
                "name": "Fallback",
                "images": [image.filename for image in group.images],
                "representative": selected,
                "reason": reason,
            }
        ] if selected else []


class ProcessLoadedGroupsWorker(QObject):
    """Background worker for reviewing already loaded groups and moving originals."""

    progressChanged = Signal(int, str)
    finished = Signal(dict)
    failed = Signal(str)

    def __init__(self, folder: str, cache_key: str, groups: list, settings: AppSettings, paths: AppPaths) -> None:
        super().__init__()
        self.folder = Path(folder)
        self.cache_key = cache_key
        self.groups = groups
        self.settings = settings
        self.paths = paths

    def run(self) -> None:
        try:
            self.progressChanged.emit(5, "Preparing loaded groups")
            groups_to_review = [group for group in self.groups if len(group.images) > 1 and not group.user_selected]
            for group in self.groups:
                if group.user_selected:
                    group.status = "Reviewed"
                    continue
                if len(group.images) == 1 and not group.user_selected:
                    filename = group.images[0].filename
                    group.user_selected = [filename]
                    group.vision_llm_selected = [filename]
                    group.vision_llm_reason = "Single-photo group selected automatically; Vision LLM was not needed."
                    group.vision_llm_subgroups = [
                        {
                            "name": "Single photo",
                            "images": [filename],
                            "representative": filename,
                            "reason": "Only one photo in this group.",
                        }
                    ]
                    group.status = "Reviewed"

            if groups_to_review:
                reviewer = VisionLLMReviewer(self.settings)
                total = len(groups_to_review)
                for index, group in enumerate(groups_to_review, start=1):
                    self.progressChanged.emit(10 + int((index - 1) / total * 75), f"Vision LLM reviewing {group.group_id}")
                    try:
                        result = reviewer.review_group(group)
                        group.vision_llm_selected = result.selected_filenames
                        group.vision_llm_reason = result.reason
                        group.vision_llm_subgroups = result.subgroups
                        group.user_selected = result.selected_filenames
                    except Exception as exc:
                        self._apply_vision_fallback(group, str(exc))
                    group.status = "Reviewed"

            review_store = ReviewStore(self.paths.review_dir)
            similarity_store = ReviewStore(self.paths.similarity_dir)
            decisions = [ReviewDecision.from_group(group) for group in self.groups]
            review_store.write_review_json(f"{self.cache_key}_review", decisions)
            review_store.write_review_csv(f"{self.cache_key}_review", decisions)

            self.progressChanged.emit(90, "Moving selected and rejected photos")
            move_summary = PhotoMover(self.folder).move_grouped_photos(self.groups)
            similarity_store.write_groups(f"{self.cache_key}_groups", self.groups)

            self.progressChanged.emit(100, "Process Photos complete")
            self.finished.emit(
                {
                    "folder": str(self.folder),
                    "cache_key": self.cache_key,
                    "groups": self.groups,
                    "move_summary": move_summary,
                }
            )
        except Exception as exc:  # pragma: no cover - UI error surface
            LOGGER.exception("Process loaded groups failed")
            self.failed.emit(str(exc))

    def _apply_vision_fallback(self, group, error_message: str) -> None:
        selected = group.suggested_best or (group.images[0].filename if group.images else "")
        group.user_selected = [selected] if selected else []
        group.vision_llm_selected = list(group.user_selected)
        reason = f"Vision LLM failed for this group; used suggested/local fallback. {error_message}"
        if len(reason) > 500:
            reason = f"{reason[:500]}..."
        group.vision_llm_reason = reason
        group.vision_llm_subgroups = [
            {
                "name": "Fallback",
                "images": [image.filename for image in group.images],
                "representative": selected,
                "reason": reason,
            }
        ] if selected else []


class MainWindow(QMainWindow):
    """Primary application window."""

    def __init__(self, settings: AppSettings, paths: AppPaths) -> None:
        super().__init__()
        self.settings = settings
        self.paths = paths
        self.selected_folder: str = ""
        self.latest_cache_key: str = ""
        self.current_decisions: list[Any] = []
        self._last_progress_message = ""
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None

        self.setWindowTitle("PhotoPick AI")
        self.resize(1280, 820)

        self.select_button = QPushButton("Select Folder")
        self.select_button.clicked.connect(self.select_folder)

        self.settings_button = QPushButton("Settings")
        self.settings_button.clicked.connect(self.open_settings)

        self.find_button = QPushButton("Find Similar Photos")
        self.find_button.clicked.connect(self.find_similar_photos)

        self.vision_llm_all_button = QPushButton("Run Vision LLM All")
        self.vision_llm_all_button.clicked.connect(self.run_vision_llm_all)

        self.process_button = QPushButton("Process Photos")
        self.process_button.clicked.connect(self.process_photos)

        self.resume_button = QPushButton("Resume Saved Review")
        self.resume_button.clicked.connect(self.resume_saved_review)
        self.resume_button.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(self.select_button)
        controls.addWidget(self.settings_button)
        controls.addWidget(self.find_button)
        controls.addWidget(self.vision_llm_all_button)
        controls.addWidget(self.process_button)
        controls.addWidget(self.resume_button)
        controls.addStretch(1)

        self.folder_label = QLabel("No folder selected.")
        self.status_label = QLabel("Ready.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumHeight(140)

        self.review_widget = DuplicateReviewWidget(self.settings)
        self.review_widget.decisionsChanged.connect(self.save_review_decisions)
        self.review_widget.visionStatusChanged.connect(self._on_vision_status_changed)
        self.review_widget.visionReviewFinished.connect(self._on_vision_review_finished)

        landing = QWidget()
        landing_layout = QVBoxLayout(landing)
        landing_layout.addWidget(QLabel("Select a folder, then run local near-duplicate detection."))
        landing_layout.addWidget(QLabel("Only JPG/JPEG files in the chosen folder are scanned."))
        landing_layout.addStretch(1)

        self.stack = QStackedWidget()
        self.stack.addWidget(landing)
        self.stack.addWidget(self.review_widget)

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addLayout(controls)
        root_layout.addWidget(self.folder_label)
        root_layout.addWidget(self.status_label)
        root_layout.addWidget(self.progress_bar)
        root_layout.addWidget(self.stack, stretch=1)
        root_layout.addWidget(self.log_box)

        self.setCentralWidget(root)

    def select_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select photo folder")
        if not folder:
            return
        self.selected_folder = folder
        self.folder_label.setText(f"Folder: {folder}")
        self.append_log(f"Selected folder: {folder}")
        self._refresh_resume_state()

    def open_settings(self) -> None:
        dialog = SettingsPanel(self.settings, self)
        if dialog.exec():
            self.settings = dialog.updated_settings()
            self.review_widget.set_settings(self.settings)
            save_settings(self.settings, self.paths)
            self.append_log(f"Saved settings: {asdict(self.settings)}")

    def find_similar_photos(self) -> None:
        if not self.selected_folder:
            QMessageBox.warning(self, "Select Folder", "Choose a folder with JPG/JPEG files first.")
            return

        self._set_processing_state(True)
        self._last_progress_message = ""
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.status_label.setText("Processing selected folder...")
        self.append_log("Starting scan and similarity analysis")

        self._thread = QThread(self)
        self._worker = ProcessingWorker(self.selected_folder, self.settings, self.paths)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def process_photos(self) -> None:
        if not self.selected_folder:
            QMessageBox.warning(self, "Select Folder", "Choose a folder with JPG/JPEG files first.")
            return
        if self.review_widget.is_vision_llm_running():
            QMessageBox.information(self, "Process Photos", "Vision LLM review is still running. Please wait for it to finish first.")
            return

        confirmation = QMessageBox.question(
            self,
            "Process Photos",
            "This will use the current groups if they already exist, run Vision LLM only for groups that still need a selection, then move "
            "selected originals to 'Selected Photos' and all other originals to 'Rejected Photos'. If no groups are loaded, it will scan first. Continue?",
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return

        self._set_processing_state(True)
        self._last_progress_message = ""
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.status_label.setText("Processing and moving photos...")
        self.append_log("Starting Process Photos workflow")

        self._thread = QThread(self)
        if self.review_widget.groups and self.latest_cache_key:
            self._worker = ProcessLoadedGroupsWorker(
                self.selected_folder,
                self.latest_cache_key,
                self.review_widget.groups,
                self.settings,
                self.paths,
            )
        else:
            self._worker = ProcessPhotosWorker(self.selected_folder, self.settings, self.paths)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progressChanged.connect(self._on_progress)
        self._worker.finished.connect(self._on_process_photos_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()

    def run_vision_llm_all(self) -> None:
        if not self.review_widget.groups:
            QMessageBox.information(self, "Run Vision LLM All", "Run Find Similar Photos first so there are groups to review.")
            return
        if self.review_widget.is_vision_llm_running():
            QMessageBox.information(self, "Run Vision LLM All", "Vision LLM review is already running.")
            return
        self.append_log("Starting Vision LLM review for all groups")
        self.status_label.setText("Vision LLM reviewing all groups...")
        self.review_widget.run_vision_llm_for_all_groups()

    def _on_vision_status_changed(self, message: str) -> None:
        self.status_label.setText(message)
        self.append_log(message)

    def _on_vision_review_finished(self, reviewed_count: int) -> None:
        self.progress_bar.setValue(100)
        self.progress_bar.setFormat("100%")
        self.status_label.setText(f"Vision LLM review complete for {reviewed_count} group(s)")
        self.append_log(f"Vision LLM review complete for {reviewed_count} group(s)")

    def _on_progress(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(f"{value}%")
        self.status_label.setText(message)
        if message != self._last_progress_message:
            self.append_log(message)
            self._last_progress_message = message

    def _on_finished(self, payload: dict) -> None:
        self._set_processing_state(False)
        self.latest_cache_key = payload["cache_key"]
        groups = payload["groups"]
        self.review_widget.load_groups(groups)
        self.stack.setCurrentIndex(1)
        self.status_label.setText(f"Found {len(groups)} photo groups")
        self.append_log(f"Processing complete for {payload['folder']}: {len(groups)} photo groups found")
        self.save_review_decisions(self.review_widget.review_decisions())
        self._refresh_resume_state()
        self._cleanup_worker()

    def _on_process_photos_finished(self, payload: dict) -> None:
        self._set_processing_state(False)
        self.latest_cache_key = payload["cache_key"]
        groups = payload["groups"]
        move_summary = payload["move_summary"]
        self.review_widget.load_groups(groups)
        self.stack.setCurrentIndex(1)
        self.status_label.setText(
            f"Processed {len(groups)} groups; moved {len(move_summary.selected)} selected and {len(move_summary.rejected)} rejected photos"
        )
        self.append_log(
            f"Process Photos complete for {payload['folder']}: "
            f"{len(move_summary.selected)} selected -> {move_summary.selected_dir}; "
            f"{len(move_summary.rejected)} rejected -> {move_summary.rejected_dir}; "
            f"{len(move_summary.skipped)} skipped"
        )
        self.save_review_decisions(self.review_widget.review_decisions())
        self._refresh_resume_state()
        self._cleanup_worker()

    def _on_failed(self, message: str) -> None:
        self._set_processing_state(False)
        QMessageBox.critical(self, "Processing failed", message)
        self.status_label.setText("Processing failed.")
        self.append_log(f"Processing failed: {message}")
        self._cleanup_worker()

    def save_review_decisions(self, decisions) -> None:
        self.current_decisions = decisions
        if not self.latest_cache_key:
            return
        store = ReviewStore(self.paths.review_dir)
        store.write_review_json(f"{self.latest_cache_key}_review", decisions)
        store.write_review_csv(f"{self.latest_cache_key}_review", decisions)

    def append_log(self, message: str) -> None:
        self.log_box.append(message)

    def _set_processing_state(self, running: bool) -> None:
        self.select_button.setEnabled(not running)
        self.settings_button.setEnabled(not running)
        self.find_button.setEnabled(not running)
        self.vision_llm_all_button.setEnabled(not running)
        self.process_button.setEnabled(not running)
        self.resume_button.setEnabled((not running) and self._has_saved_review())

    def _cleanup_worker(self) -> None:
        if self._thread is not None:
            if self._thread.isRunning():
                self._thread.quit()
                self._thread.wait(1500)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None

    def resume_saved_review(self) -> None:
        if not self.selected_folder:
            QMessageBox.warning(self, "Select Folder", "Choose a folder first.")
            return

        cache_key = cache_key_for_folder(Path(self.selected_folder))
        similarity_store = ReviewStore(self.paths.similarity_dir)
        review_store = ReviewStore(self.paths.review_dir)
        groups = similarity_store.read_groups(f"{cache_key}_groups")
        if not groups:
            QMessageBox.information(self, "No Saved Review", "No saved duplicate groups were found for this folder yet.")
            self._refresh_resume_state()
            return

        decisions = review_store.read_review_json(f"{cache_key}_review")
        groups = review_store.restore_group_review_state(groups, decisions)
        self.latest_cache_key = cache_key
        self.review_widget.load_groups(groups)
        self.stack.setCurrentIndex(1)
        self.status_label.setText(f"Resumed saved review with {len(groups)} groups")
        self.append_log(f"Resumed saved review for {self.selected_folder}")

    def _has_saved_review(self) -> bool:
        if not self.selected_folder:
            return False
        cache_key = cache_key_for_folder(Path(self.selected_folder))
        return (self.paths.similarity_dir / f"{cache_key}_groups.json").exists()

    def _refresh_resume_state(self) -> None:
        self.resume_button.setEnabled(self._has_saved_review() and self._worker is None)
