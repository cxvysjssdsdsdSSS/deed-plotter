"""CAD-style DXF preview window for Deed Plotter exports.

Uses ezdxf's drawing Frontend + a PyQt6 backend so text, MTEXT, linetypes,
and ACI colors look closer to Civil 3D modelspace than a naive line dump.
"""

from __future__ import annotations

import math
from pathlib import Path

from ezdxf import recover
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import Configuration, TextPolicy
from ezdxf.addons.drawing.properties import LayerProperties, set_layers_state
from ezdxf.document import Drawing
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QKeySequence, QPainter, QWheelEvent
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGraphicsScene,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from dxf_viewer_backend import ROLE_LAYER, PyQt6Backend

# Modelspace-ish dark canvas (Civil 3D default feel).
_CANVAS_BG = "#0d0d0d"
_FG_HINT = "#e0e0e0"


class CadView(QGraphicsView):
    """Hand-pan, wheel-zoom, Y-up CAD view."""

    mouse_moved = pyqtSignal(QPointF)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._zoom = 1.0
        self._default_zoom = 1.0
        self._zoom_limits = (0.05, 200.0)
        self._view_buffer = 0.18
        self.setScene(QGraphicsScene())
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.scale(1, -1)  # +Y up (survey / CAD)

    def buffer_scene_rect(self) -> None:
        scene = self.scene()
        if scene is None:
            return
        r = scene.sceneRect()
        if r.isNull() or r.width() <= 0 or r.height() <= 0:
            items = scene.itemsBoundingRect()
            r = items if not items.isNull() else QRectF(-50, -50, 100, 100)
            scene.setSceneRect(r)
        bx = max(r.width() * self._view_buffer / 2.0, 10.0)
        by = max(r.height() * self._view_buffer / 2.0, 10.0)
        scene.setSceneRect(r.adjusted(-bx, -by, bx, by))

    def fit_to_scene(self) -> None:
        self.buffer_scene_rect()
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._default_zoom = _x_scale(self.transform())
        self._zoom = 1.0

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta_notches = event.angleDelta().y() / 120.0
        if delta_notches == 0:
            return
        direction = math.copysign(1.0, delta_notches)
        factor = (1.0 + 0.18 * direction) ** abs(delta_notches)
        resulting = self._zoom * factor
        if resulting < self._zoom_limits[0]:
            factor = self._zoom_limits[0] / self._zoom
        elif resulting > self._zoom_limits[1]:
            factor = self._zoom_limits[1] / self._zoom
        self.scale(factor, factor)
        self._zoom *= factor

    def mouseMoveEvent(self, event) -> None:
        super().mouseMoveEvent(event)
        self.mouse_moved.emit(self.mapToScene(event.position().toPoint()))


def _x_scale(t) -> float:
    return math.sqrt(t.m11() * t.m11() + t.m21() * t.m21())


class DxfViewerWindow(QMainWindow):
    """Standalone / embeddable DXF preview for Deed Plotter exports."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("DXF Viewer")
        self.resize(1100, 720)
        self.setAcceptDrops(True)

        self._path: Path | None = None
        self._doc: Drawing | None = None
        self._ctx: RenderContext | None = None
        self._backend = PyQt6Backend()
        self._visible_layers: set[str] = set()
        self._config = Configuration(text_policy=TextPolicy.FILLING)

        self.view = CadView()
        self.view.mouse_moved.connect(self._on_mouse_moved)
        self.view.setBackgroundBrush(QColor(_CANVAS_BG))

        self.layer_list = QListWidget()
        self.layer_list.setMinimumWidth(200)
        self.layer_list.setStyleSheet(
            "QListWidget { background: #1a1d22; color: #e0e0e0; border: none; }"
            "QCheckBox { color: #e0e0e0; padding: 2px 4px; }"
        )

        side = QWidget()
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(6, 6, 6, 6)
        side_lay.setSpacing(6)
        side_lay.addWidget(QLabel("Layers"))
        side_lay.addWidget(self.layer_list, 1)
        hint = QLabel("Wheel zoom · drag pan · F fit · F5 reload")
        hint.setStyleSheet(f"color: #9aa3ad; font-size: 11px;")
        hint.setWordWrap(True)
        side_lay.addWidget(hint)

        split = QSplitter()
        split.addWidget(self.view)
        split.addWidget(side)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        split.setSizes([820, 260])
        self.setCentralWidget(split)

        self._coord = QLabel("E —   N —")
        self._coord.setMinimumWidth(220)
        self._status_file = QLabel("No file")
        self.statusBar().addWidget(self._status_file, 1)
        self.statusBar().addPermanentWidget(self._coord)

        self._build_toolbar()
        self._apply_chrome()

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)

        open_act = QAction("Open…", self)
        open_act.setShortcut(QKeySequence.StandardKey.Open)
        open_act.triggered.connect(self.open_dialog)
        tb.addAction(open_act)

        fit_act = QAction("Fit", self)
        fit_act.setShortcut(QKeySequence("F"))
        fit_act.triggered.connect(self.view.fit_to_scene)
        tb.addAction(fit_act)

        reload_act = QAction("Reload", self)
        reload_act.setShortcut(QKeySequence("F5"))
        reload_act.triggered.connect(self.reload)
        tb.addAction(reload_act)

        tb.addSeparator()

        all_act = QAction("All layers", self)
        all_act.triggered.connect(lambda: self._set_all_layers(True))
        tb.addAction(all_act)

        none_act = QAction("No layers", self)
        none_act.triggered.connect(lambda: self._set_all_layers(False))
        tb.addAction(none_act)

        deed_act = QAction("Deed Plotter layers", self)
        deed_act.setToolTip("Show only DP-* layers from Deed Plotter export")
        deed_act.triggered.connect(self._show_deed_layers)
        tb.addAction(deed_act)

    def _apply_chrome(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background: #1e2126; color: {_FG_HINT}; }}
            QToolBar {{ background: #252a31; border: none; spacing: 6px; padding: 4px; }}
            QToolButton {{ color: {_FG_HINT}; padding: 4px 10px; }}
            QToolButton:hover {{ background: #3a4048; }}
            QStatusBar {{ background: #181b20; color: #b0b8c4; }}
            QSplitter::handle {{ background: #2a3038; width: 3px; }}
            QLabel {{ color: {_FG_HINT}; }}
            """
        )

    # ── public API ───────────────────────────────────────────────────

    def load_path(self, path: str | Path, *, fit: bool = True) -> bool:
        path = Path(path)
        if not path.is_file():
            QMessageBox.warning(self, "DXF Viewer", f"File not found:\n{path}")
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        dlg = None
        if self.isVisible():
            dlg = QProgressDialog("Opening DXF…", None, 0, 0, self)
            dlg.setWindowModality(Qt.WindowModality.WindowModal)
            dlg.setMinimumDuration(0)
            dlg.setCancelButton(None)
            dlg.setWindowTitle("DXF Viewer")
            dlg.show()
            QApplication.processEvents()
        try:
            doc, auditor = recover.readfile(str(path))
        except Exception as exc:
            if dlg is not None:
                dlg.close()
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "DXF Viewer", f"Could not open DXF:\n{exc}")
            return False
        if auditor.has_errors:
            msgs = "\n".join(str(e) for e in auditor.errors[:8])
            QMessageBox.warning(
                self,
                "DXF has structure issues",
                f"Opened with recover. Some entities may be missing.\n\n{msgs}",
            )
        self._path = path
        self._doc = doc
        self._status_file.setText(str(path))
        self.setWindowTitle(f"DXF Viewer — {path.name}")
        self._visible_layers = {
            layer.dxf.name for layer in doc.layers if _layer_on(layer)
        }
        self._rebuild_layer_list()
        if dlg is not None:
            dlg.setLabelText("Drawing…")
            QApplication.processEvents()
        try:
            self._redraw_unlocked(fit=fit)
        finally:
            if dlg is not None:
                dlg.close()
            QApplication.restoreOverrideCursor()
        return True

    def open_dialog(self) -> None:
        start = str(self._path.parent) if self._path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open DXF", start, "DXF files (*.dxf);;All files (*)"
        )
        if path:
            self.load_path(path)

    def reload(self) -> None:
        if self._path:
            self.load_path(self._path, fit=False)

    # ── drag / drop ──────────────────────────────────────────────────

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local.lower().endswith(".dxf"):
                self.load_path(local)
                break

    # ── layers ───────────────────────────────────────────────────────

    def _rebuild_layer_list(self) -> None:
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        if self._doc is None:
            self.layer_list.blockSignals(False)
            return
        names = sorted(
            (layer.dxf.name for layer in self._doc.layers),
            key=_layer_sort_key,
        )
        for name in names:
            item = QListWidgetItem(self.layer_list)
            cb = QCheckBox(name)
            cb.setChecked(name in self._visible_layers)
            color = _aci_to_qcolor(_layer_aci(self._doc, name))
            cb.setStyleSheet(
                f"QCheckBox {{ color: {color.name()}; }}"
                f"QCheckBox::indicator {{ width: 14px; height: 14px; }}"
            )
            cb.toggled.connect(lambda on, n=name: self._layer_toggled(n, on))
            self.layer_list.addItem(item)
            self.layer_list.setItemWidget(item, cb)
            item.setSizeHint(cb.sizeHint())
        self.layer_list.blockSignals(False)

    def _layer_toggled(self, name: str, on: bool) -> None:
        if on:
            self._visible_layers.add(name)
        else:
            self._visible_layers.discard(name)
        self._redraw(fit=False)

    def _set_all_layers(self, on: bool) -> None:
        if self._doc is None:
            return
        names = [layer.dxf.name for layer in self._doc.layers]
        self._visible_layers = set(names) if on else set()
        self._rebuild_layer_list()
        self._redraw(fit=False)

    def _show_deed_layers(self) -> None:
        if self._doc is None:
            return
        names = [layer.dxf.name for layer in self._doc.layers if layer.dxf.name.upper().startswith("DP-")]
        if not names:
            QMessageBox.information(
                self,
                "DXF Viewer",
                "No DP-* layers in this file (not a Deed Plotter export?).",
            )
            return
        self._visible_layers = set(names)
        self._rebuild_layer_list()
        self._redraw(fit=True)

    # ── render ───────────────────────────────────────────────────────

    def _make_context(self) -> RenderContext:
        assert self._doc is not None

        def override(layers: list[LayerProperties]) -> None:
            if self._visible_layers:
                set_layers_state(layers, self._visible_layers, state=True)
            else:
                for layer in layers:
                    layer.is_visible = False

        ctx = RenderContext(self._doc)
        ctx.set_layer_properties_override(override)
        return ctx

    def _redraw(self, *, fit: bool) -> None:
        if self._doc is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._redraw_unlocked(fit=fit)
        finally:
            QApplication.restoreOverrideCursor()

    def _redraw_unlocked(self, *, fit: bool) -> None:
        if self._doc is None:
            return
        layout = self._doc.modelspace()
        scene = QGraphicsScene()
        scene.setBackgroundBrush(QColor(_CANVAS_BG))
        self._backend.set_scene(scene)
        self._ctx = self._make_context()
        self._ctx.set_current_layout(layout)
        # Dark bg → ACI 7 (white/black) resolves to light foreground.
        try:
            self._ctx.current_layout_properties.set_colors(bg=_CANVAS_BG)
        except Exception:
            pass
        try:
            Frontend(self._ctx, self._backend, config=self._config).draw_layout(
                layout, finalize=True
            )
        except Exception as exc:
            QMessageBox.critical(self, "DXF Viewer", f"Render failed:\n{exc}")
            return
        self.view.setScene(scene)
        self.view.buffer_scene_rect()
        if fit:
            self.view.fit_to_scene()
        n = len(scene.items())
        self.statusBar().showMessage(f"{n} drawn items", 2500)

    def _on_mouse_moved(self, pos: QPointF) -> None:
        # Survey convention: X = Easting, Y = Northing
        self._coord.setText(f"E {pos.x():,.3f}   N {pos.y():,.3f}")


def _layer_aci(doc: Drawing, name: str) -> int:
    try:
        layer = doc.layers.get(name)
        return int(layer.color)
    except Exception:
        return 7


def _layer_on(layer) -> bool:
    try:
        return not layer.is_off()
    except Exception:
        return True


def _layer_sort_key(name: str):
    upper = name.upper()
    # Deed Plotter layers first, CALL-* grouped, then alpha.
    if upper.startswith("DP-"):
        return (0, upper)
    if upper.startswith("DP-CALL") or upper.startswith("DP_CALL"):
        return (1, upper)
    return (2, upper)


def _aci_to_qcolor(aci: int) -> QColor:
    # Minimal ACI → RGB for layer list tint (AutoCAD index subset).
    table = {
        1: "#ff0000",
        2: "#ffff00",
        3: "#00ff00",
        4: "#00ffff",
        5: "#0000ff",
        6: "#ff00ff",
        7: "#ffffff",
        8: "#808080",
    }
    return QColor(table.get(int(aci), "#cfd8dc"))


def open_dxf_viewer(
    path: str | Path | None = None,
    *,
    parent: QWidget | None = None,
) -> DxfViewerWindow | None:
    """Show a modeless DXF viewer; optionally load *path*.

    Returns None when *path* was given and could not be loaded (window closed).
    """
    win = DxfViewerWindow(parent)
    win.setWindowFlag(Qt.WindowType.Window, True)
    win.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    win.show()
    win.raise_()
    win.activateWindow()
    if path and not win.load_path(path):
        win.close()
        return None
    return win


def run_viewer_app(path: str | Path | None = None) -> int:
    """Standalone entry — creates QApplication if needed."""
    app = QApplication.instance()
    owns = app is None
    if owns:
        app = QApplication([])
        app.setStyle("Fusion")
    win = open_dxf_viewer(path)
    if owns:
        return app.exec()
    return 0
