from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
)

from app.config import AppSettings


class SettingsPanel(QDialog):
    """Dialog for editing processing settings."""

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self._settings = settings

        self.preview_spin = QSpinBox()
        self.preview_spin.setRange(128, 2048)
        self.preview_spin.setValue(settings.preview_long_edge)

        self.window_spin = QSpinBox()
        self.window_spin.setRange(1, 300)
        self.window_spin.setValue(settings.comparison_time_window_seconds)

        self.burst_window_spin = QSpinBox()
        self.burst_window_spin.setRange(1, 120)
        self.burst_window_spin.setValue(settings.burst_time_window_seconds)

        self.similarity_spin = QDoubleSpinBox()
        self.similarity_spin.setRange(0.5, 1.0)
        self.similarity_spin.setDecimals(3)
        self.similarity_spin.setSingleStep(0.005)
        self.similarity_spin.setValue(settings.similarity_threshold)

        self.exact_spin = QDoubleSpinBox()
        self.exact_spin.setRange(0.5, 1.0)
        self.exact_spin.setDecimals(3)
        self.exact_spin.setSingleStep(0.005)
        self.exact_spin.setValue(settings.exact_duplicate_threshold)

        self.auto_suggest_check = QCheckBox("Auto-suggest winner")
        self.auto_suggest_check.setChecked(settings.auto_suggest_winner)

        self.orientation_check = QCheckBox("Prefer same orientation")
        self.orientation_check.setChecked(settings.same_orientation_preference)

        self.vision_endpoint_edit = QLineEdit(settings.vision_llm_endpoint)
        self.vision_model_edit = QLineEdit(settings.vision_llm_model)

        self.vision_timeout_spin = QSpinBox()
        self.vision_timeout_spin.setRange(10, 900)
        self.vision_timeout_spin.setValue(settings.vision_llm_timeout_seconds)

        form = QFormLayout(self)
        form.addRow("Preview long edge", self.preview_spin)
        form.addRow("Time window (sec)", self.window_spin)
        form.addRow("Burst window (sec)", self.burst_window_spin)
        form.addRow("Similarity threshold", self.similarity_spin)
        form.addRow("Exact duplicate threshold", self.exact_spin)
        form.addRow("", self.auto_suggest_check)
        form.addRow("", self.orientation_check)
        form.addRow("Vision LLM endpoint", self.vision_endpoint_edit)
        form.addRow("Vision LLM model", self.vision_model_edit)
        form.addRow("Vision LLM timeout (sec)", self.vision_timeout_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def updated_settings(self) -> AppSettings:
        """Return the edited settings instance."""

        return AppSettings(
            preview_long_edge=self.preview_spin.value(),
            comparison_time_window_seconds=self.window_spin.value(),
            burst_time_window_seconds=self.burst_window_spin.value(),
            similarity_threshold=self.similarity_spin.value(),
            exact_duplicate_threshold=self.exact_spin.value(),
            auto_suggest_winner=self.auto_suggest_check.isChecked(),
            same_orientation_preference=self.orientation_check.isChecked(),
            vision_llm_endpoint=self.vision_endpoint_edit.text().strip(),
            vision_llm_model=self.vision_model_edit.text().strip(),
            vision_llm_timeout_seconds=self.vision_timeout_spin.value(),
        )
