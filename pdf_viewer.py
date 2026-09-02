"""Zoomable, pannable document viewer for deed pages.

QGraphicsView-based: mouse wheel zooms toward the cursor, drag pans,
toolbar gives page navigation, zoom in/out, fit-width, fit-page, and 100%.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QPushButton, QSpinBox, QToolButton, QVBoxLayout,
    QWidget,
)

ZOOM_STEP = 1.25
ZOOM_MIN = 0.05
ZOOM_MAX = 12.0
_PIXMAP_KEEP = 3  # current page plus neighbors


class _PageSpin(QSpinBox):
    """Page spinner that reads like a document: the DOWN arrow moves down
    through the document (next page), UP moves back toward page 1. Both the
    step direction and the arrow enabled-states are inverted together."""

    def stepBy(self, steps: int):
        super().stepBy(-steps)

    def stepEnabled(self):
        flags = QAbstractSpinBox.StepEnabledFlag.StepNone
        if self.value() < self.maximum():  # can go to a later page -> DOWN active
            flags |= QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
        if self.value() > self.minimum():  # can go to an earlier page -> UP active
            flags |= QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
        return flags


class _PageView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(Qt.GlobalColor.darkGray)
        # Hand-drag pans via scroll; keep bars hidden — padded scene supplies range.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._item: QGraphicsPixmapItem | None = None

    def set_pixmap(self, pixmap: QPixmap):
        self.scene().clear()
        self._item = self.scene().addPixmap(pixmap)
        self._expand_scene_for_pan()

    def _expand_scene_for_pan(self):
        """Pad the scene so the page can be dragged even when it fits the view.

        Without padding, QGraphicsView has no scroll range at fit-width / fit-page
        and ScrollHandDrag feels locked to center until you zoom in far enough.
        """
        if self._item is None:
            return
        br = self._item.boundingRect()
        # Viewport size in scene coordinates (accounts for current zoom).
        top_left = self.mapToScene(0, 0)
        bottom_right = self.mapToScene(self.viewport().width(), self.viewport().height())
        vw = abs(bottom_right.x() - top_left.x())
        vh = abs(bottom_right.y() - top_left.y())
        # Enough slack to park any corner of the page under the opposite
        # viewport corner (and then some when zoomed out).
        pad_x = max(vw, br.width() * 0.5, 1.0)
        pad_y = max(vh, br.height() * 0.5, 1.0)
        self.scene().setSceneRect(br.adjusted(-pad_x, -pad_y, pad_x, pad_y))

    def zoom(self, factor: float):
        current = self.transform().m11()
        target = max(ZOOM_MIN, min(ZOOM_MAX, current * factor))
        if current > 0:
            self.scale(target / current, target / current)
        self._expand_scene_for_pan()

    def fit_page(self):
        if self._item:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)
            self._expand_scene_for_pan()

    def fit_width(self):
        if not self._item:
            return
        rect = self._item.boundingRect()
        margin = 4
        avail = self.viewport().width() - margin
        if rect.width() > 0 and avail > 0:
            current = self.transform().m11()
            target = max(ZOOM_MIN, min(ZOOM_MAX, avail / rect.width()))
            if current > 0:
                self.scale(target / current, target / current)
            self._expand_scene_for_pan()
            # Align page top to viewport top (centerOn(y=top) wrongly puts
            # the top edge at mid-viewport and bumps the image downward).
            top_left = self.mapToScene(0, 0)
            bottom_left = self.mapToScene(0, self.viewport().height())
            view_h = abs(bottom_left.y() - top_left.y())
            self.centerOn(rect.center().x(), rect.top() + view_h / 2.0)

    def actual_size(self):
        self.resetTransform()
        self._expand_scene_for_pan()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._expand_scene_for_pan()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.angleDelta().x()
        if delta == 0:
            event.ignore()
            return
        self.zoom(ZOOM_STEP if delta > 0 else 1 / ZOOM_STEP)
        event.accept()


class DocumentViewer(QWidget):
    pageChanged = pyqtSignal(int)  # 1-based

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pages: list[bytes] = []
        self._pixmaps: dict[int, QPixmap] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.prev_btn = QToolButton(text="\u25c0")
        self.prev_btn.setToolTip("Previous page of the loaded document")
        self.prev_btn.clicked.connect(lambda: self.page_spin.setValue(self.page_spin.value() - 1))
        self.page_spin = _PageSpin(minimum=1, maximum=1)
        self.page_spin.setToolTip(
            "Current page. Down arrow = next page, up arrow = previous page."
        )
        self.page_spin.valueChanged.connect(self._show_page)
        self.count_label = QLabel("of 0")
        self.next_btn = QToolButton(text="\u25b6")
        self.next_btn.setToolTip("Next page of the loaded document")
        self.next_btn.clicked.connect(lambda: self.page_spin.setValue(self.page_spin.value() + 1))
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.page_spin)
        bar.addWidget(self.count_label)
        bar.addWidget(self.next_btn)
        bar.addSpacing(16)

        zoom_out = QToolButton(text="\u2212")
        zoom_out.setToolTip("Zoom out — or scroll the mouse wheel")
        zoom_out.clicked.connect(lambda: self.view.zoom(1 / ZOOM_STEP))
        zoom_in = QToolButton(text="+")
        zoom_in.setToolTip("Zoom in — or scroll the mouse wheel")
        zoom_in.clicked.connect(lambda: self.view.zoom(ZOOM_STEP))
        fit_w = QPushButton("Fit Width")
        fit_w.setToolTip("Scale the page to fill the viewer width")
        fit_w.clicked.connect(lambda: self.view.fit_width())
        fit_p = QPushButton("Fit Page")
        fit_p.setToolTip("Scale the whole page to fit in the viewer")
        fit_p.clicked.connect(lambda: self.view.fit_page())
        full = QPushButton("100%")
        full.setToolTip("View at actual size (100% zoom). Drag to pan.")
        full.clicked.connect(lambda: self.view.actual_size())
        for w in (zoom_out, zoom_in, fit_w, fit_p, full):
            bar.addWidget(w)
        bar.addStretch()
        layout.addLayout(bar)

        self.view = _PageView()
        layout.addWidget(self.view, stretch=1)
        self._set_page_nav_enabled()

    def _set_page_nav_enabled(self):
        n = len(self.pages)
        multi = n > 1
        self.page_spin.setEnabled(multi)
        cur = self.page_spin.value()
        self.prev_btn.setEnabled(multi and cur > 1)
        self.next_btn.setEnabled(multi and cur < n)
        # Dim the "of N" label when paging isn't useful.
        self.count_label.setEnabled(multi)

    def set_pages(self, pages: list[bytes]):
        self.pages = pages
        self._pixmaps.clear()
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, len(pages)))
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.count_label.setText(f"of {len(pages)}")
        self._show_page(1)
        self._set_page_nav_enabled()
        self.view.fit_page()

    def _pixmap(self, idx: int) -> QPixmap:
        if idx not in self._pixmaps:
            img = QImage.fromData(self.pages[idx], "PNG")
            self._pixmaps[idx] = QPixmap.fromImage(img)
            self._evict_pixmaps(keep=idx)
        return self._pixmaps[idx]

    def _evict_pixmaps(self, keep: int) -> None:
        n = len(self.pages)
        keep_set = {keep}
        if keep > 0:
            keep_set.add(keep - 1)
        if keep + 1 < n:
            keep_set.add(keep + 1)
        while len(keep_set) < _PIXMAP_KEEP and n:
            # Prefer filling toward page 1, then the far end.
            extra = min(keep_set) - 1 if min(keep_set) > 0 else max(keep_set) + 1
            if 0 <= extra < n:
                keep_set.add(extra)
            else:
                break
        for k in list(self._pixmaps):
            if k not in keep_set:
                del self._pixmaps[k]

    def _show_page(self, page_num: int):
        if not self.pages:
            self._set_page_nav_enabled()
            return
        idx = max(0, min(page_num - 1, len(self.pages) - 1))
        self.view.set_pixmap(self._pixmap(idx))
        self.pageChanged.emit(idx + 1)
        self._set_page_nav_enabled()
