from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QKeyEvent, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.config import AppSettings
from app.core.models import DuplicateGroup, ReviewDecision
from app.core.vision_llm_reviewer import VisionLLMReviewer, VisionLLMReviewResult
from app.ui.group_list_panel import GroupListPanel
from app.ui.preview_grid import PreviewGrid

LOGGER = logging.getLogger(__name__)


class VisionLLMWorker(QObject):
    """Run Vision LLM review outside the UI thread."""

    finished = Signal(list)
    failed = Signal(str)

    def __init__(self, settings: AppSettings, indexed_groups: list[tuple[int, DuplicateGroup]]) -> None:
        super().__init__()
        self.settings = settings
        self.indexed_groups = indexed_groups

    def run(self) -> None:
        reviewer = VisionLLMReviewer(self.settings)
        results: list[tuple[int, VisionLLMReviewResult]] = []
        failures: list[str] = []
        for index, group in self.indexed_groups:
            try:
                results.append((index, reviewer.review_group(group)))
            except Exception as exc:  # pragma: no cover - surfaced in UI
                fallback = self._fallback_result(group, str(exc))
                results.append((index, fallback))
                failures.append(f"{group.group_id}: {exc}")

        if failures:
            LOGGER.warning("Vision LLM completed with %s fallback result(s): %s", len(failures), "; ".join(failures[:5]))
        self.finished.emit(results)

    def _fallback_result(self, group: DuplicateGroup, error_message: str) -> VisionLLMReviewResult:
        selected = group.suggested_best or (group.images[0].filename if group.images else "")
        selected_filenames = [selected] if selected else []
        reason = f"Vision LLM failed for this group; used suggested/local fallback. {error_message}"
        if len(reason) > 500:
            reason = f"{reason[:500]}..."
        subgroups = [
            {
                "name": "Fallback",
                "images": [image.filename for image in group.images],
                "representative": selected,
                "reason": reason,
            }
        ] if selected else []
        return VisionLLMReviewResult(selected_filenames, reason, subgroups)


class DuplicateReviewWidget(QWidget):
    """Near-duplicate review screen."""

    decisionsChanged = Signal(list)
    visionStatusChanged = Signal(str)
    visionReviewFinished = Signal(int)

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.groups: list[DuplicateGroup] = []
        self.current_index = 0
        self._vision_thread: QThread | None = None
        self._vision_worker: VisionLLMWorker | None = None

        title = QLabel("Near-Duplicate Review")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")

        self.summary_label = QLabel("No groups loaded.")
        self.ai_label = QLabel("")
        self.ai_label.setWordWrap(True)
        self.ai_label.setMaximumHeight(120)
        self.ai_label.setStyleSheet("color: #0b5d7a; font-weight: 600;")
        self.shortcut_label = QLabel(
            "Shortcuts: 1..9 pick | Enter confirm | S skip | K suggested | V vision LLM | Right/Left move"
        )
        self.shortcut_label.setStyleSheet("color: #555555;")

        self.group_list = GroupListPanel()
        self.group_list.groupSelected.connect(self._set_current_group)

        self.preview_grid = PreviewGrid()
        self.preview_grid.winnerPicked.connect(self._set_single_winner)
        self.preview_grid.keepMultipleToggled.connect(self._toggle_alternate)

        self.previous_button = QPushButton("Previous Group")
        self.previous_button.clicked.connect(self.previous_group)

        self.skip_button = QPushButton("Skip Group")
        self.skip_button.clicked.connect(self.skip_group)

        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.clicked.connect(self.confirm_group)

        self.vision_llm_button = QPushButton("Run Vision LLM")
        self.vision_llm_button.clicked.connect(self.run_vision_llm_for_current_group)

        self.next_button = QPushButton("Next Group")
        self.next_button.clicked.connect(self.next_group)

        controls = QHBoxLayout()
        controls.addWidget(self.previous_button)
        controls.addWidget(self.skip_button)
        controls.addWidget(self.vision_llm_button)
        controls.addWidget(self.confirm_button)
        controls.addWidget(self.next_button)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.addWidget(title)
        right_layout.addWidget(self.summary_label)
        right_layout.addWidget(self.ai_label)
        right_layout.addWidget(self.shortcut_label)
        right_layout.addWidget(self.preview_grid, stretch=1)
        right_layout.addLayout(controls)

        splitter = QSplitter()
        splitter.addWidget(self.group_list)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter)

        self._install_shortcuts()

    def load_groups(self, groups: list[DuplicateGroup]) -> None:
        """Load new duplicate groups into the review screen."""

        self.groups = groups
        self.current_index = 0
        self.group_list.set_groups(groups)
        if groups:
            self._show_group(0)
        else:
            self.summary_label.setText("No near-duplicate groups found.")
        self._emit_decisions()

    def _install_shortcuts(self) -> None:
        for number in range(1, 10):
            shortcut = QShortcut(QKeySequence(str(number)), self)
            shortcut.activated.connect(lambda n=number: self.pick_by_number(n))
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self.confirm_group)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self).activated.connect(self.confirm_group)
        QShortcut(QKeySequence("S"), self).activated.connect(self.skip_group)
        QShortcut(QKeySequence("K"), self).activated.connect(self.pick_suggested)
        QShortcut(QKeySequence("V"), self).activated.connect(self.run_vision_llm_for_current_group)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self).activated.connect(self.next_group)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self).activated.connect(self.previous_group)

    def set_settings(self, settings: AppSettings) -> None:
        """Update review-time settings."""

        self.settings = settings

    def _set_current_group(self, index: int) -> None:
        if 0 <= index < len(self.groups):
            self._show_group(index)

    def _show_group(self, index: int) -> None:
        self.current_index = index
        group = self.groups[index]
        self.summary_label.setText(
            f"{group.group_id}: {group.group_type} | {len(group.images)} images | span {group.capture_span_seconds}s | status {group.status}"
        )
        self.ai_label.setText(self._ai_summary(group))
        self.preview_grid.set_group(group)
        self.group_list.setCurrentRow(index)

    def _set_single_winner(self, filename: str) -> None:
        group = self.groups[self.current_index]
        remaining = [item for item in group.user_selected if item != filename]
        group.user_selected = [filename] + [item for item in remaining if item != filename]
        self.preview_grid.mark_selection(group.user_selected)
        self._emit_decisions()

    def _toggle_alternate(self, filename: str, checked: bool) -> None:
        group = self.groups[self.current_index]
        winner = group.user_selected[0] if group.user_selected else group.suggested_best
        selected = [winner] if winner else []
        alternates = [item for item in group.user_selected[1:] if item != filename]
        if checked:
            alternates.append(filename)
        deduped = []
        for item in [*selected, *alternates]:
            if item and item not in deduped:
                deduped.append(item)
        group.user_selected = deduped
        self.preview_grid.mark_selection(group.user_selected)
        self._emit_decisions()

    def pick_by_number(self, number: int) -> None:
        if not self.groups:
            return
        group = self.groups[self.current_index]
        if 1 <= number <= len(group.images):
            self._set_single_winner(group.images[number - 1].filename)

    def confirm_group(self) -> None:
        if not self.groups:
            return
        group = self.groups[self.current_index]
        if not group.user_selected and group.suggested_best:
            group.user_selected = [group.suggested_best]
        group.status = "Reviewed"
        self.group_list.refresh_group(self.current_index, group)
        self.preview_grid.mark_selection(group.user_selected)
        self._emit_decisions()
        if self.current_index < len(self.groups) - 1:
            self.next_group()

    def skip_group(self) -> None:
        if not self.groups:
            return
        group = self.groups[self.current_index]
        group.status = "Skipped"
        self.group_list.refresh_group(self.current_index, group)
        self._emit_decisions()
        if self.current_index < len(self.groups) - 1:
            self.next_group()

    def next_group(self) -> None:
        if self.current_index < len(self.groups) - 1:
            self._show_group(self.current_index + 1)

    def previous_group(self) -> None:
        if self.current_index > 0:
            self._show_group(self.current_index - 1)

    def pick_suggested(self) -> None:
        if not self.groups:
            return
        group = self.groups[self.current_index]
        if group.suggested_best:
            self._set_single_winner(group.suggested_best)

    def review_decisions(self) -> list[ReviewDecision]:
        """Return normalized decisions for all groups."""

        decisions: list[ReviewDecision] = []
        for group in self.groups:
            winner = group.user_selected[0] if group.user_selected else None
            alternates = group.user_selected[1:] if len(group.user_selected) > 1 else []
            rejected = [image.filename for image in group.images if image.filename not in group.user_selected]
            decisions.append(
                ReviewDecision(
                    group_id=group.group_id,
                    winner=winner,
                    alternates=alternates,
                    rejected=rejected,
                    reviewed=group.status == "Reviewed",
                    group_key=group.group_key,
                    local_ai_selected=group.local_ai_selected,
                    local_ai_reason=group.local_ai_reason,
                    vision_llm_selected=group.vision_llm_selected,
                    vision_llm_reason=group.vision_llm_reason,
                    vision_llm_subgroups=group.vision_llm_subgroups,
                    status=group.status,
                )
            )
        return decisions

    def _emit_decisions(self) -> None:
        self.decisionsChanged.emit(self.review_decisions())

    def run_vision_llm_for_current_group(self) -> None:
        if not self.groups:
            return
        group = self.groups[self.current_index]
        if len(group.images) == 1:
            self._auto_select_single_group(self.current_index, group)
            self._show_group(self.current_index)
            self._emit_decisions()
            return
        self._start_vision_llm([(self.current_index, group)])

    def run_vision_llm_for_all_groups(self) -> None:
        if not self.groups:
            return
        indexed_groups: list[tuple[int, DuplicateGroup]] = []
        for index, group in enumerate(self.groups):
            if len(group.images) == 1:
                self._auto_select_single_group(index, group)
            else:
                indexed_groups.append((index, group))

        self._show_group(self.current_index)
        self._emit_decisions()
        if indexed_groups:
            self._start_vision_llm(indexed_groups)
        else:
            self.ai_label.setText("All single-photo groups were selected automatically; Vision LLM was not needed.")
            self.visionStatusChanged.emit("All single-photo groups were selected automatically.")
            self.visionReviewFinished.emit(0)

    def is_vision_llm_running(self) -> bool:
        """Return True while a Vision LLM worker is active."""

        return self._vision_thread is not None

    def _start_vision_llm(self, indexed_groups: list[tuple[int, DuplicateGroup]]) -> None:
        if self._vision_thread is not None:
            self.ai_label.setText("Vision LLM is already running.")
            return

        self._set_vision_buttons_enabled(False)
        self.ai_label.setText(f"Vision LLM is reviewing {len(indexed_groups)} group(s)...")
        self.visionStatusChanged.emit(f"Vision LLM is reviewing {len(indexed_groups)} group(s)...")
        self._vision_thread = QThread(self)
        self._vision_worker = VisionLLMWorker(self.settings, indexed_groups)
        self._vision_worker.moveToThread(self._vision_thread)
        self._vision_thread.started.connect(self._vision_worker.run)
        self._vision_worker.finished.connect(self._on_vision_llm_finished)
        self._vision_worker.failed.connect(self._on_vision_llm_failed)
        self._vision_worker.finished.connect(self._vision_thread.quit)
        self._vision_worker.failed.connect(self._vision_thread.quit)
        self._vision_thread.start()

    def _on_vision_llm_finished(self, results: list[tuple[int, VisionLLMReviewResult]]) -> None:
        for index, result in results:
            if not 0 <= index < len(self.groups):
                continue
            group = self.groups[index]
            group.vision_llm_selected = result.selected_filenames
            group.vision_llm_reason = result.reason
            group.vision_llm_subgroups = result.subgroups
            group.user_selected = result.selected_filenames
            group.status = "Reviewed"
            self.group_list.refresh_group(index, group)
        self._show_group(self.current_index)
        self._emit_decisions()
        self.visionStatusChanged.emit(f"Vision LLM review complete for {len(results)} group(s).")
        self.visionReviewFinished.emit(len(results))
        self._cleanup_vision_worker()

    def _on_vision_llm_failed(self, message: str) -> None:
        short_message = self._short_message(message)
        self.ai_label.setText(short_message)
        self.visionStatusChanged.emit(f"Vision LLM review failed: {short_message}")
        self._cleanup_vision_worker()

    def _cleanup_vision_worker(self) -> None:
        self._set_vision_buttons_enabled(True)
        if self._vision_thread is not None and self._vision_thread.isRunning():
            self._vision_thread.quit()
            self._vision_thread.wait(1500)
        if self._vision_worker is not None:
            self._vision_worker.deleteLater()
            self._vision_worker = None
        if self._vision_thread is not None:
            self._vision_thread.deleteLater()
            self._vision_thread = None

    def _set_vision_buttons_enabled(self, enabled: bool) -> None:
        self.vision_llm_button.setEnabled(enabled)

    def _auto_select_single_group(self, index: int, group: DuplicateGroup) -> None:
        if len(group.images) != 1:
            return
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
        self.group_list.refresh_group(index, group)

    def _ai_summary(self, group: DuplicateGroup) -> str:
        if group.vision_llm_subgroups:
            lines = [f"Vision LLM selected {len(group.vision_llm_selected)} representative photo(s):"]
            for subgroup in group.vision_llm_subgroups:
                representative = subgroup.get("representative", "")
                images = subgroup.get("images", [])
                name = subgroup.get("name", "Subgroup")
                lines.append(f"{name}: keep {representative} from {len(images)} similar photo(s)")
            if group.vision_llm_reason:
                lines.append(group.vision_llm_reason)
            return self._short_message("\n".join(lines), max_length=1200)
        return self._short_message(group.vision_llm_reason or group.local_ai_reason, max_length=1200)

    def _short_message(self, message: str, max_length: int = 700) -> str:
        if len(message) <= max_length:
            return message
        return f"{message[:max_length]}..."

    def keyPressEvent(self, event: QKeyEvent) -> None:
        super().keyPressEvent(event)
