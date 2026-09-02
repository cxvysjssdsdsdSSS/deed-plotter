"""API settings dialog, persisted via QSettings."""

from __future__ import annotations

import json

from PyQt6.QtCore import QSettings, QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QWidget,
)

from model_catalog import ModelOption, fetch_model_options, merge_model_options
from document_loader import (
    DEFAULT_IMAGE_QUALITY,
    IMAGE_QUALITY_PRESETS,
    resolve_image_quality,
)
from page_cache import DEFAULT_PAGE_CACHE_MAX
import page_cache
from parse_cache import DEFAULT_PARSE_CACHE_MAX
import parse_cache
from closure_panel import DEFAULT_TOLERANCE_FT, load_closure_tolerance

ORG = "DeedPlotter"
APP = "DeedPlotter"
DEFAULT_MODEL = "composer-2.5"
CURSOR_API_KEY_URL = "https://cursor.com/dashboard?tab=integrations"
_CACHE_MAX_MIN = 1
_CACHE_MAX_MAX = 50


def _clamp_cache_max(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(_CACHE_MAX_MIN, min(_CACHE_MAX_MAX, n))


def load_settings() -> dict:
    s = QSettings(ORG, APP)
    quality = s.value("image_quality", DEFAULT_IMAGE_QUALITY, str)
    if quality not in IMAGE_QUALITY_PRESETS:
        quality = DEFAULT_IMAGE_QUALITY
    return {
        "api_key": s.value("api_key", "", str),
        "model": s.value("model", DEFAULT_MODEL, str),
        "always_append": s.value("always_append", "", str),
        "image_quality": quality,
        "page_cache_max": _clamp_cache_max(
            s.value("page_cache_max", DEFAULT_PAGE_CACHE_MAX),
            DEFAULT_PAGE_CACHE_MAX,
        ),
        "parse_cache_max": _clamp_cache_max(
            s.value("parse_cache_max", DEFAULT_PARSE_CACHE_MAX),
            DEFAULT_PARSE_CACHE_MAX,
        ),
        "closure_tolerance_ft": load_closure_tolerance(),
    }


def _load_cached_models() -> list[ModelOption]:
    raw = QSettings(ORG, APP).value("model_options", "", str)
    try:
        return [ModelOption(id=d["id"], label=d["label"]) for d in json.loads(raw)]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def _save_cached_models(options: list[ModelOption]) -> None:
    QSettings(ORG, APP).setValue(
        "model_options",
        json.dumps([{"id": o.id, "label": o.label} for o in options]),
    )


class _ModelFetchWorker(QThread):
    done = pyqtSignal(list, str)  # options, error ("" if ok)

    def __init__(self, api_key: str):
        super().__init__()
        self.api_key = api_key

    def run(self):
        options, error = fetch_model_options(self.api_key)
        self.done.emit(options, error or "")


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(840)
        cfg = load_settings()
        self._fetcher: _ModelFetchWorker | None = None
        self._fetch_gen = 0

        self.key_edit = QLineEdit(cfg["api_key"])
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key_edit, stretch=1)
        self.show_key = QCheckBox("Show")
        self.show_key.toggled.connect(self._toggle_key_visibility)
        key_layout.addWidget(self.show_key)
        get_key_btn = QPushButton("Get API key\u2026")
        get_key_btn.setToolTip("Open the Cursor dashboard (Integrations) in your browser.")
        get_key_btn.clicked.connect(self._open_api_key_page)
        key_layout.addWidget(get_key_btn)

        model_row = QWidget()
        row = QHBoxLayout(model_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMaximumWidth(420)
        self._set_model_options(_load_cached_models(), cfg["model"])
        row.addWidget(self.model_combo, stretch=1)
        self.refresh_btn = QPushButton("Refresh Models")
        self.refresh_btn.setToolTip("Query Cursor for the models available to your API key.")
        self.refresh_btn.clicked.connect(self._refresh_models)
        row.addWidget(self.refresh_btn)

        self.always_append = QPlainTextEdit(cfg.get("always_append", ""))
        self.always_append.setPlaceholderText(
            "Optional notes always sent with every AI parse "
            "(county conventions, OCR quirks, unit preferences…)."
        )
        self.always_append.setFixedHeight(80)
        self.always_append.setToolTip(
            "Appended to every Parse with AI request, in addition to the paste box."
        )

        self.quality_combo = QComboBox()
        self.quality_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.quality_combo.setMaximumWidth(280)
        for qid, preset in IMAGE_QUALITY_PRESETS.items():
            self.quality_combo.addItem(preset.label, qid)
        q_idx = self.quality_combo.findData(cfg.get("image_quality", DEFAULT_IMAGE_QUALITY))
        self.quality_combo.setCurrentIndex(q_idx if q_idx >= 0 else 1)
        self.quality_combo.setToolTip(
            "PDF/image raster size for the viewer and AI parse.\n"
            "Re-open the deed after changing (already-loaded pages keep the old size).\n"
            + "\n".join(
                f"• {p.label}: {p.tip} ({p.dpi} DPI, max {p.max_dim} px)"
                for p in IMAGE_QUALITY_PRESETS.values()
            )
        )
        self._quality_hint = QLabel("")
        self._quality_hint.setObjectName("dimLabel")
        self._quality_hint.setStyleSheet("color: #9e9e9e;")
        self._quality_hint.setWordWrap(True)
        self._quality_hint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        # Fixed slot so switching presets does not resize the dialog.
        self._quality_hint.setFixedHeight(48)
        self.quality_combo.currentIndexChanged.connect(self._update_quality_hint)
        self._update_quality_hint()

        self.page_cache_spin = QSpinBox()
        self.page_cache_spin.setRange(_CACHE_MAX_MIN, _CACHE_MAX_MAX)
        self.page_cache_spin.setValue(int(cfg.get("page_cache_max", DEFAULT_PAGE_CACHE_MAX)))
        self.page_cache_spin.setFixedWidth(72)
        self.page_cache_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.page_cache_spin.setToolTip(
            "Max cached deed page-image sets under ~/.deed_plotter/page_cache/.\n"
            "Oldest unused entries are dropped when you open a new deed."
        )
        self.parse_cache_spin = QSpinBox()
        self.parse_cache_spin.setRange(_CACHE_MAX_MIN, _CACHE_MAX_MAX)
        self.parse_cache_spin.setValue(int(cfg.get("parse_cache_max", DEFAULT_PARSE_CACHE_MAX)))
        self.parse_cache_spin.setFixedWidth(72)
        self.parse_cache_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.parse_cache_spin.setToolTip(
            "Max mid-parse resume sessions under ~/.deed_plotter/parse_cache/.\n"
            "Sessions are kept until this limit (no time expiry). Oldest drop first."
        )

        self.tol_spin = QDoubleSpinBox()
        self.tol_spin.setDecimals(3)
        self.tol_spin.setRange(0.001, 100.0)
        self.tol_spin.setSingleStep(0.01)
        self.tol_spin.setValue(float(cfg.get("closure_tolerance_ft", DEFAULT_TOLERANCE_FT)))
        self.tol_spin.setFixedWidth(96)
        self.tol_spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.tol_spin.setSuffix(" ft")
        self.tol_spin.setToolTip(
            "Maximum linear misclosure (feet) for a traverse to count as CLOSED.\n"
            "Used by the Closure tab, plot, chat proposals, notes, and DXF export.\n"
            "Default 0.01 ft. Increase for rougher or older descriptions."
        )

        self.status_label = QLabel("")
        self.status_label.setObjectName("dimLabel")
        self.status_label.setStyleSheet("color: #9e9e9e;")
        self.status_label.setWordWrap(True)

        form = QFormLayout(self)
        form.addRow("Cursor API key:", key_row)
        form.addRow("Model:", model_row)
        form.addRow("Always append to parse:", self.always_append)
        form.addRow("Deed image quality:", self._left_pack(self.quality_combo))
        form.addRow("", self._quality_hint)
        form.addRow("Page image cache limit:", self._left_pack(self.page_cache_spin))
        form.addRow("Parse resume cache limit:", self._left_pack(self.parse_cache_spin))
        form.addRow("Closure tolerance:", self._left_pack(self.tol_spin))
        form.addRow("", self.status_label)

        link_btn = QPushButton("cursor.com/dashboard \u2192 Integrations")
        link_btn.setFlat(True)
        link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        link_btn.setStyleSheet(
            "QPushButton { color: #64b5f6; text-align: left; border: none; padding: 0; }"
            "QPushButton:hover { color: #90caf9; text-decoration: underline; }"
        )
        link_btn.setToolTip("Open the Cursor dashboard to create or copy your API key.")
        link_btn.clicked.connect(self._open_api_key_page)
        form.addRow("", link_btn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.resize(840, max(self.sizeHint().height(), 280))

    @staticmethod
    def _left_pack(widget: QWidget) -> QWidget:
        """Keep compact controls left-aligned instead of QFormLayout stretch."""
        wrap = QWidget()
        lay = QHBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget)
        lay.addStretch(1)
        return wrap

    def _update_quality_hint(self):
        qid = self.quality_combo.currentData()
        preset = resolve_image_quality(str(qid) if qid else None)
        self._quality_hint.setText(
            f"{preset.tip}  ({preset.dpi} DPI, max edge {preset.max_dim} px)"
        )

    def _toggle_key_visibility(self, show: bool):
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self.key_edit.setEchoMode(mode)

    @staticmethod
    def _open_api_key_page():
        QDesktopServices.openUrl(QUrl(CURSOR_API_KEY_URL))

    def _set_model_options(self, options: list[ModelOption], current: str):
        merged = merge_model_options(options, current)
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for opt in merged:
            self.model_combo.addItem(opt.label, opt.id)
        idx = next((i for i, o in enumerate(merged) if o.id == current), -1)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        else:
            self.model_combo.setEditText(current)
        self.model_combo.blockSignals(False)

    def _refresh_models(self):
        key = self.key_edit.text().strip()
        if not key:
            self.status_label.setText("Enter an API key first, then refresh.")
            return
        self.refresh_btn.setEnabled(False)
        self.status_label.setText("Fetching models\u2026")
        self._fetch_gen += 1
        gen = self._fetch_gen
        self._fetcher = _ModelFetchWorker(key)
        self._fetcher.done.connect(lambda opts, err, g=gen: self._models_fetched(opts, err, g))
        self._fetcher.start()

    def _models_fetched(self, options: list, error: str, gen: int):
        if gen != self._fetch_gen:
            return
        self.refresh_btn.setEnabled(True)
        if error:
            self.status_label.setText(error)
            return
        current = self._selected_model_id()
        self._set_model_options(options, current)
        _save_cached_models(options)
        self.status_label.setText(f"{len(options)} models available to this key.")

    def _selected_model_id(self) -> str:
        idx = self.model_combo.currentIndex()
        data = self.model_combo.itemData(idx)
        text = self.model_combo.currentText().strip()
        if data and self.model_combo.itemText(idx) == text:
            return str(data)
        return text.removesuffix(" (saved)").strip()

    def _cleanup_fetcher(self):
        self._fetch_gen += 1
        if self._fetcher is not None and self._fetcher.isRunning():
            self._fetcher.wait(100)

    def reject(self):
        self._cleanup_fetcher()
        super().reject()

    def closeEvent(self, event):
        self._cleanup_fetcher()
        super().closeEvent(event)

    def accept(self):
        s = QSettings(ORG, APP)
        s.setValue("api_key", self.key_edit.text().strip())
        s.setValue("model", self._selected_model_id() or DEFAULT_MODEL)
        s.setValue("always_append", self.always_append.toPlainText().strip())
        qid = self.quality_combo.currentData()
        s.setValue("image_quality", str(qid) if qid else DEFAULT_IMAGE_QUALITY)
        s.setValue("page_cache_max", int(self.page_cache_spin.value()))
        s.setValue("parse_cache_max", int(self.parse_cache_spin.value()))
        s.setValue("closure_tolerance_ft", float(self.tol_spin.value()))
        # Apply new limits immediately (don't wait for next open/parse).
        try:
            page_cache.prune(int(self.page_cache_spin.value()))
            parse_cache.prune_sessions(int(self.parse_cache_spin.value()))
        except OSError:
            pass
        self._cleanup_fetcher()
        super().accept()
