"""Toolbar icons for Deed Plotter.

Bundled light SVGs (Lucide-style, MIT) — readable on the dark Fusion toolbar.
Windows QIcon.fromTheme EditUndo/EditRedo exist but paint black; useless here.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

_ICONS_DIR = Path(__file__).resolve().parent / "icons"


def _render_svg(path: Path, px: int, *, opacity: float = 1.0) -> QPixmap:
    renderer = QSvgRenderer(str(path))
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)
    if not renderer.isValid():
        return pix
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if opacity < 1.0:
        p.setOpacity(opacity)
    renderer.render(p, QRectF(0, 0, px, px))
    p.end()
    return pix


def make_undo_redo_icon(*, redo: bool = False, size: int = 20) -> QIcon:
    """Sharp undo/redo icon for the dark toolbar (HiDPI-aware)."""
    filename = "redo.svg" if redo else "undo.svg"
    path = _ICONS_DIR / filename
    dpr = 1.0
    app = QApplication.instance()
    if app is not None:
        dpr = max(1.0, float(app.devicePixelRatio()))

    icon = QIcon()
    for logical in (size, size + 4, 24, 32):
        px = max(logical, int(round(logical * dpr)))
        normal = _render_svg(path, px)
        disabled = _render_svg(path, px, opacity=0.38)
        if dpr != 1.0:
            normal.setDevicePixelRatio(dpr)
            disabled.setDevicePixelRatio(dpr)
        icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.On)
        icon.addPixmap(normal, QIcon.Mode.Active, QIcon.State.Off)
        icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.Off)
        icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.On)
    return icon
