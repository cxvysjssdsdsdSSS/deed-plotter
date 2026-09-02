"""Left-to-right shimmer text (Cursor-style thinking indicator)."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath
from PyQt6.QtWidgets import QSizePolicy, QWidget


class ShimmerLabel(QWidget):
    """Muted text with a bright band that sweeps left → right."""

    def __init__(
        self,
        parent=None,
        *,
        base_color: str = "#7a848e",
        highlight_color: str = "#f0f3f6",
        italic: bool = True,
        point_size: int = 12,
        period_ms: int = 1600,
    ):
        super().__init__(parent)
        self._text = ""
        self._base = QColor(base_color)
        self._highlight = QColor(highlight_color)
        self._phase = 0.0  # 0..1
        self._shimmering = False
        self._period_ms = max(400, int(period_ms))

        font = QFont(self.font())
        font.setPointSize(point_size)
        font.setItalic(italic)
        self.setFont(font)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._timer = QTimer(self)
        self._timer.setInterval(32)  # ~30 fps
        self._timer.timeout.connect(self._advance)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:  # noqa: N802 — QLabel-compatible
        text = text or ""
        if text == self._text:
            return
        self._text = text
        self.updateGeometry()
        self.update()

    def clear(self) -> None:
        self.setText("")

    def start(self) -> None:
        self._shimmering = True
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop(self) -> None:
        self._shimmering = False
        self._timer.stop()
        self._phase = 0.0
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        w = fm.horizontalAdvance(self._text) if self._text else 40
        h = max(fm.height(), 16)
        return QSize(w + 4, h + 2)

    def minimumSizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(20, max(fm.height(), 16))

    def _advance(self) -> None:
        step = self._timer.interval() / self._period_ms
        self._phase = (self._phase + step) % 1.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt override
        del event
        if not self._text:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        font = self.font()
        fm = QFontMetrics(font)
        y = (self.height() + fm.ascent() - fm.descent()) / 2
        path = QPainterPath()
        path.addText(0.0, float(y), font, self._text)

        if not self._shimmering:
            p.fillPath(path, self._base)
            return

        text_w = max(1.0, float(fm.horizontalAdvance(self._text)))
        # Soft highlight band travels left → right across the glyphs.
        band = max(28.0, text_w * 0.55)
        travel = text_w + band * 2
        center = -band + self._phase * travel

        grad = QLinearGradient(center - band, 0.0, center + band, 0.0)
        grad.setColorAt(0.0, self._base)
        grad.setColorAt(0.35, self._base)
        grad.setColorAt(0.5, self._highlight)
        grad.setColorAt(0.65, self._base)
        grad.setColorAt(1.0, self._base)
        p.fillPath(path, grad)
