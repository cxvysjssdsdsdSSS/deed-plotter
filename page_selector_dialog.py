"""Dialog to choose which document pages are sent to the AI parse.

Split layout: thumbnail checklist on the left, large zoomable preview of the
clicked page on the right.
"""

from __future__ import annotations

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from pdf_viewer import _PageView, ZOOM_STEP

THUMB_WIDTH = 130
# Soft heads-up only — rasters this large can be slow/costly to send.
LARGE_PAGE_BYTES = 1_500_000


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


class _Thumb(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


def _png_reader(data: bytes) -> QImageReader:
    buf = QBuffer()
    buf.setData(QByteArray(data))
    buf.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buf, b"PNG")
    reader.setAutoTransform(False)
    # Keep the buffer alive on the reader for the duration of read().
    reader._buf = buf  # type: ignore[attr-defined]
    return reader


def _png_native_size(data: bytes) -> QSize:
    reader = _png_reader(data)
    sz = reader.size()
    return sz if sz.isValid() else QSize(0, 0)


def _png_thumb(data: bytes, width: int) -> QPixmap:
    reader = _png_reader(data)
    native = reader.size()
    if native.width() > width > 0:
        h = max(1, int(native.height() * width / native.width()))
        reader.setScaledSize(QSize(width, h))
    img = reader.read()
    if img.isNull():
        img = QImage.fromData(data, "PNG")
        if not img.isNull() and img.width() > width:
            img = img.scaledToWidth(width, Qt.TransformationMode.FastTransformation)
    return QPixmap.fromImage(img)


class PageSelectorDialog(QDialog):
    """Checkbox-per-page thumbnail list with a large preview pane.
    selected_indices() returns a sorted list of 0-based pages, or None when
    every page is selected."""

    def __init__(self, pages: list[bytes], selected: set[int] | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Pages for AI Parsing")
        self.resize(980, 700)
        self._pages = pages
        self._pixmaps: dict[int, QPixmap] = {}
        self._native_sizes: dict[int, QSize] = {}
        self._checks: list[QCheckBox] = []
        self._thumbs: list[_Thumb] = []
        self._current = -1
        self._ok_btn = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Checked pages are attached to the AI parse. "
            "Uncheck cover sheets, plats you don't want read, etc. "
            "Click a thumbnail to preview it on the right.\n"
            "Sizes are the rasterized images that will be sent (larger ≈ slower/costlier)."
        ))

        split = QSplitter(Qt.Orientation.Horizontal)

        # Left: thumbnail checklist.
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        for i in range(len(pages)):
            cell = QWidget()
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(4, 4, 4, 4)
            thumb = _Thumb()
            thumb.setPixmap(_png_thumb(pages[i], THUMB_WIDTH))
            thumb.setCursor(Qt.CursorShape.PointingHandCursor)
            thumb.clicked.connect(lambda idx=i: self._preview(idx))
            cv.addWidget(thumb)
            self._thumbs.append(thumb)

            nbytes = len(pages[i])
            native = _png_native_size(pages[i])
            self._native_sizes[i] = native
            size_txt = (
                f"{_format_bytes(nbytes)} · {native.width()}\u00d7{native.height()}"
            )
            chk = QCheckBox(f"Page {i + 1}")
            chk.setChecked(selected is None or i in selected)
            chk.setToolTip(
                f"Raster size sent to the AI: {size_txt}"
                + ("\nThis page is large — consider unchecking if it is a cover/plat."
                   if nbytes >= LARGE_PAGE_BYTES else "")
            )
            chk.toggled.connect(self._update_summary)
            cv.addWidget(chk)
            self._checks.append(chk)

            meta = QLabel(size_txt)
            meta.setStyleSheet(
                "color: #e6a23c; font-size: 11px;"
                if nbytes >= LARGE_PAGE_BYTES else
                "color: #9aa3ad; font-size: 11px;"
            )
            if nbytes >= LARGE_PAGE_BYTES:
                meta.setToolTip(
                    "Large page raster — may be slower or costlier to send."
                )
            cv.addWidget(meta)

            grid.addWidget(cell, i // 2, i % 2)
        grid.setRowStretch(grid.rowCount(), 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_host)
        scroll.setMinimumWidth(2 * (THUMB_WIDTH + 30))
        split.addWidget(scroll)

        # Right: large zoomable preview.
        preview_host = QWidget()
        pv = QVBoxLayout(preview_host)
        pv.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.preview_label = QLabel("Preview")
        bar.addWidget(self.preview_label)
        bar.addStretch()
        zoom_out = QToolButton(text="\u2212")
        zoom_out.setToolTip("Zoom out — or scroll the mouse wheel")
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / ZOOM_STEP))
        zoom_in = QToolButton(text="+")
        zoom_in.setToolTip("Zoom in — or scroll the mouse wheel")
        zoom_in.clicked.connect(lambda: self.view.zoom(ZOOM_STEP))
        fit_btn = QPushButton("Fit")
        fit_btn.clicked.connect(lambda: self.view.fit_page())
        for w in (zoom_out, zoom_in, fit_btn):
            bar.addWidget(w)
        pv.addLayout(bar)
        self.view = _PageView()
        pv.addWidget(self.view, stretch=1)
        split.addWidget(preview_host)

        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([2 * (THUMB_WIDTH + 30), 620])
        layout.addWidget(split, stretch=1)

        btns = QHBoxLayout()
        all_btn = QPushButton("Select All")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select None")
        none_btn.clicked.connect(lambda: self._set_all(False))
        btns.addWidget(all_btn)
        btns.addWidget(none_btn)
        btns.addStretch()
        self.summary = QLabel("")
        btns.addWidget(self.summary)
        layout.addLayout(btns)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_any)
        buttons.rejected.connect(self.reject)
        self._ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)

        self._update_summary()
        if pages:
            self._preview(0)

    def _pixmap(self, idx: int) -> QPixmap:
        if idx not in self._pixmaps:
            img = QImage.fromData(self._pages[idx], "PNG")
            self._pixmaps[idx] = QPixmap.fromImage(img)
        return self._pixmaps[idx]

    def _preview(self, idx: int):
        self._current = idx
        self.view.set_pixmap(self._pixmap(idx))
        self.view.fit_page()
        nbytes = len(self._pages[idx])
        native = self._native_sizes.get(idx) or _png_native_size(self._pages[idx])
        self.preview_label.setText(
            f"Preview \u2014 Page {idx + 1}  ·  "
            f"{_format_bytes(nbytes)}  ·  {native.width()}\u00d7{native.height()}"
        )
        for i, thumb in enumerate(self._thumbs):
            thumb.setStyleSheet(
                "border: 2px solid #4fc3f7;" if i == idx else "border: 1px solid #444;"
            )

    def _set_all(self, on: bool):
        for chk in self._checks:
            chk.setChecked(on)

    def _update_summary(self):
        picked = [i for i, c in enumerate(self._checks) if c.isChecked()]
        n = len(picked)
        total_bytes = sum(len(self._pages[i]) for i in picked)
        self.summary.setText(
            f"{n} of {len(self._checks)} selected  ·  {_format_bytes(total_bytes)} total"
        )
        if self._ok_btn is not None:
            self._ok_btn.setEnabled(n > 0)

    def _accept_if_any(self):
        if any(c.isChecked() for c in self._checks):
            self.accept()
        else:
            self.summary.setText("Select at least one page.")

    def selected_indices(self) -> list[int] | None:
        picked = [i for i, c in enumerate(self._checks) if c.isChecked()]
        return None if len(picked) == len(self._checks) else picked
