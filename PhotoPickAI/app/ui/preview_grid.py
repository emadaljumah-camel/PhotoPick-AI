from __future__ import annotations

from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.models import DuplicateGroup, ImageRecord
from app.utils.time_utils import format_display_time


class PreviewGrid(QWidget):
    """Preview grid for one duplicate group."""

    winnerPicked = Signal(str)
    keepMultipleToggled = Signal(str, bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._group: DuplicateGroup | None = None
        self._cards: dict[str, QFrame] = {}
        self._alternate_buttons: dict[str, QPushButton] = {}
        self._winner_pickers: dict[str, QRadioButton] = {}
        self._pixmap_cache: dict[tuple[str, int], QPixmap] = {}
        self._winner_buttons = QButtonGroup(self)
        self._winner_buttons.setExclusive(True)

        self._container = QWidget()
        self._layout = QGridLayout(self._container)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(12)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(self._container)

        root_layout = QVBoxLayout(self)
        root_layout.addWidget(self._scroll)

    def set_group(self, group: DuplicateGroup) -> None:
        self._group = group
        self._cards.clear()
        self._alternate_buttons.clear()
        self._winner_pickers.clear()
        for button in self._winner_buttons.buttons():
            self._winner_buttons.removeButton(button)
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for index, image in enumerate(group.images):
            card = self._build_card(image, index + 1, group)
            self._cards[image.filename] = card
            self._layout.addWidget(card, index // 3, index % 3)
        self._refresh_highlights()

    def _build_card(self, image: ImageRecord, shortcut_index: int, group: DuplicateGroup) -> QFrame:
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        card.setProperty("suggested", image.filename == group.suggested_best)
        layout = QVBoxLayout(card)

        thumbnail = QLabel()
        thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumbnail.setMinimumHeight(220)
        thumbnail.setPixmap(self._cached_pixmap(image.preview_path, 220))
        layout.addWidget(thumbnail)

        filename_label = QLabel(f"{shortcut_index}. {image.filename}")
        filename_label.setWordWrap(True)
        layout.addWidget(filename_label)

        capture_label = QLabel(format_display_time(image.capture_time))
        layout.addWidget(capture_label)

        sharpness_label = QLabel(f"Sharpness: {image.sharpness_score:.1f}")
        layout.addWidget(sharpness_label)

        if image.filename == group.suggested_best:
            suggested = QLabel("Suggested winner")
            suggested.setStyleSheet(
                "background: #e6f6ef; color: #0b6e4f; font-weight: 700; padding: 4px 8px; border-radius: 10px;"
            )
            layout.addWidget(suggested)

        controls = QHBoxLayout()
        pick_button = QRadioButton("Pick this")
        self._winner_buttons.addButton(pick_button)
        self._winner_pickers[image.filename] = pick_button
        pick_button.toggled.connect(partial(self._on_pick_toggled, image.filename))
        controls.addWidget(pick_button)

        keep_button = QPushButton("Keep multiple")
        keep_button.setCheckable(True)
        keep_button.toggled.connect(partial(self.keepMultipleToggled.emit, image.filename))
        controls.addWidget(keep_button)
        self._alternate_buttons[image.filename] = keep_button
        layout.addLayout(controls)

        if group.user_selected:
            pick_button.blockSignals(True)
            keep_button.blockSignals(True)
            pick_button.setChecked(group.user_selected[0] == image.filename)
            keep_button.setChecked(image.filename in group.user_selected[1:])
            pick_button.blockSignals(False)
            keep_button.blockSignals(False)
        elif group.suggested_best == image.filename:
            pick_button.blockSignals(True)
            pick_button.setChecked(True)
            pick_button.blockSignals(False)

        return card

    def _on_pick_toggled(self, filename: str, checked: bool) -> None:
        if checked:
            self.winnerPicked.emit(filename)

    def mark_selection(self, selected_filenames: list[str]) -> None:
        if self._group is None:
            return
        self._group.user_selected = selected_filenames
        selected = set(selected_filenames)
        primary = selected_filenames[0] if selected_filenames else None
        for filename, picker in self._winner_pickers.items():
            picker.blockSignals(True)
            picker.setChecked(filename == primary)
            picker.blockSignals(False)
        for filename, button in self._alternate_buttons.items():
            should_check = filename in selected and filename != primary
            button.blockSignals(True)
            button.setChecked(should_check)
            button.blockSignals(False)
        self._refresh_highlights()

    def _refresh_highlights(self) -> None:
        selected = set(self._group.user_selected if self._group else [])
        for filename, card in self._cards.items():
            style = "border: 2px solid #2a7fff; background: #eef6ff;" if filename in selected else ""
            if self._group and filename == self._group.suggested_best and filename not in selected:
                style = "border: 1px dashed #88b8a5; background: #f8fffb;"
            card.setStyleSheet(style)

    def _cached_pixmap(self, preview_path: str, size: int) -> QPixmap:
        cache_key = (preview_path, size)
        if cache_key in self._pixmap_cache:
            return self._pixmap_cache[cache_key]
        pixmap = QPixmap(str(Path(preview_path)))
        scaled = pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._pixmap_cache[cache_key] = scaled
        return scaled
