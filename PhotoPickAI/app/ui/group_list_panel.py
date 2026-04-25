from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from app.core.models import DuplicateGroup


class GroupListPanel(QListWidget):
    """List panel showing duplicate groups and their review state."""

    groupSelected = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.currentRowChanged.connect(self.groupSelected.emit)

    def set_groups(self, groups: list[DuplicateGroup]) -> None:
        self.clear()
        for index, group in enumerate(groups, start=1):
            label = self._build_label(group)
            item = QListWidgetItem(label)
            item.setData(256, index - 1)
            self.addItem(item)
        if groups:
            self.setCurrentRow(0)

    def refresh_group(self, index: int, group: DuplicateGroup) -> None:
        item = self.item(index)
        if item is None:
            return
        item.setText(self._build_label(group))

    def _build_label(self, group: DuplicateGroup) -> str:
        return (
            f"{group.group_id} | {group.group_type} | {len(group.images)} images | "
            f"{group.capture_span_seconds}s | {group.status}"
        )
