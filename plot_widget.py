"""pyqtgraph plot of the traversed boundary, styled like a survey plat.

Courses get stacked, boxed labels (#, bearing, distance) kept horizontal for
readability and placed with collision avoidance; corners get monument callouts
with leader lines pointing at the corner; plus a north arrow, scale bar,
POB/OPEN markers, and per-segment highlighting driven by the call table.
"""

from __future__ import annotations

import math

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QPointF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QWidget

from cogo import (
    Call, Segment, TraverseResult, azimuth_to_bearing, compute_traverse,
    curve_direction_is_left, stated_pob_xy, traverse_start_from_pob,
)
from closure_panel import is_traverse_closed
from label_layout import (
    find_offset_px,
    format_monument_label,
    point_box,
)
from plot_scale_bar import PlotScaleBar

pg.setConfigOptions(antialias=True)

UNIT_ABBR = {
    "feet": "'", "ft": "'", "chains": " ch", "chain": " ch", "rods": " rd",
    "rod": " rd", "poles": " po", "pole": " po", "varas": " vr", "vara": " vr",
    "meters": " m", "meter": " m", "m": " m", "links": " lk", "link": " lk",
}

COLOR_LINE = "#4fc3f7"
COLOR_CURVE = "#ff8a65"  # distinct warm stroke vs cool boundary cyan
COLOR_HIGHLIGHT = "#ff9800"
COLOR_CLOSURE = "#ce93d8"  # misclose ≠ OPEN vertex red
COLOR_CORNER = "#ffca28"
COLOR_COURSE_TEXT = "#cfe8fc"
COLOR_MONUMENT_TEXT = "#ffe082"
COLOR_MONUMENT_LEADER = "#d7ccc8"
COLOR_POB = "#66bb6a"
COLOR_END = "#ef5350"
COLOR_GAP = "#ff6e40"
COLOR_TIE = "#cfd8dc"
COLOR_TIE_TEXT = "#eceff1"

LABEL_FILL = pg.mkBrush(20, 26, 32, 215)
LABEL_BORDER = pg.mkPen(90, 110, 125, 160, width=1)
MONUMENT_FILL = pg.mkBrush(45, 38, 20, 225)
MONUMENT_BORDER = pg.mkPen(196, 160, 60, 170, width=1)

LINE_WIDTH = 2
CURVE_WIDTH = 2.5
HIGHLIGHT_WIDTH = 4
COURSE_FONT_PX = 11
MONUMENT_FONT_PX = 10
LABEL_REFLOW_MS = 140


def curve_turn_letter(direction: str) -> str:
    """Map deed/AI direction text to L/R. Trailing * means assumed right."""
    d = (direction or "").strip()
    if not d:
        return "R*"
    if curve_direction_is_left(d):
        return "L"
    compact = d.lower().replace("_", " ").replace("-", " ").replace(" ", "")
    if compact in ("cw", "clockwise") or d.lower().strip() in ("right", "r", "clock wise"):
        return "R"
    if d.lower().strip().startswith("r"):
        return "R"
    return "R*"


class NorthArrow(QWidget):
    """Screen-fixed north arrow painted in the plot's top-right corner."""

    SIZE = 64

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE + 16)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.SIZE, self.SIZE
        cx = w / 2
        pen = QPen(QColor("#e0e0e0"), 1.6)
        p.setPen(pen)
        p.drawLine(QPointF(cx, h - 10), QPointF(cx, 14))
        head = QPolygonF([QPointF(cx, 4), QPointF(cx + 8, 22), QPointF(cx, 17)])
        p.setBrush(QColor("#e0e0e0"))
        p.drawPolygon(head)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([QPointF(cx, 4), QPointF(cx - 8, 22), QPointF(cx, 17)]))
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        p.setFont(font)
        p.drawText(self.rect().adjusted(0, h - 4, 0, 0), Qt.AlignmentFlag.AlignHCenter, "N")
        p.end()


class PlotLegend(QWidget):
    """Compact key for glyphs that are present on the current plot."""

    _ROW_H = 16
    _PAD_TOP = 14
    _PAD_BOTTOM = 10
    _WIDTH = 168

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedWidth(self._WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._rows: list[tuple[str, str, str]] = []
        self.hide()

    def set_contents(
        self,
        *,
        pob: bool = False,
        open_end: bool = False,
        corner: bool = False,
        ties: bool = False,
        boundary: bool = False,
        curve: bool = False,
        misclose: bool = False,
    ) -> None:
        rows: list[tuple[str, str, str]] = []
        if pob:
            rows.append((COLOR_POB, "s", "POB"))
        if open_end:
            rows.append((COLOR_END, "x", "OPEN end"))
        if corner:
            rows.append((COLOR_CORNER, "o", "Corner"))
        if ties:
            rows.append((COLOR_TIE, "t", "POC / ties"))
        if boundary:
            rows.append((COLOR_LINE, "-", "Boundary"))
        if curve:
            rows.append((COLOR_CURVE, "=", "Curve"))
        if misclose:
            rows.append((COLOR_CLOSURE, "~", "Misclose"))
        self._rows = rows
        if not rows:
            self.hide()
            return
        h = self._PAD_TOP + len(rows) * self._ROW_H + self._PAD_BOTTOM
        self.setFixedHeight(h)
        self.show()
        self.update()

    def clear_contents(self) -> None:
        self._rows = []
        self.hide()

    def paintEvent(self, _event):
        if not self._rows:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = self.rect().adjusted(1, 1, -2, -2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(16, 20, 24, 230))
        p.drawRoundedRect(box, 6, 6)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(120, 140, 155, 200), 1.2))
        p.drawRoundedRect(box, 6, 6)
        font = QFont("Segoe UI", 8)
        p.setFont(font)
        y = self._PAD_TOP
        for color, kind, label in self._rows:
            p.setPen(QPen(QColor(color), 2))
            if kind == "-":
                p.drawLine(10, y, 28, y)
            elif kind == "=":
                # Same single stroke as plot curves (color distinguishes from Boundary).
                p.drawLine(10, y, 28, y)
            elif kind == "~":
                pen = QPen(QColor(color), 2, Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawLine(10, y, 28, y)
            elif kind == "s":
                p.setBrush(QColor(color))
                p.drawRect(14, y - 5, 10, 10)
            elif kind == "x":
                p.drawLine(14, y - 5, 24, y + 5)
                p.drawLine(24, y - 5, 14, y + 5)
            elif kind == "o":
                p.setBrush(QColor(color))
                p.drawEllipse(14, y - 5, 10, 10)
            else:
                p.setBrush(QColor(color))
                p.drawPolygon(QPolygonF([
                    QPointF(19, y - 6), QPointF(24, y + 5), QPointF(14, y + 5),
                ]))
            p.setPen(QColor("#e0e0e0"))
            p.drawText(34, y + 4, label)
            y += self._ROW_H
        p.end()


_AXIS_EAST_LOCAL = "Easting (ft, local ~5000) — need both POB axes"
_AXIS_NORTH_LOCAL = "Northing (ft, local ~5000) — need both POB axes"
_AXIS_EAST_POB = "Easting (ft) — both POB axes"
_AXIS_NORTH_POB = "Northing (ft) — both POB axes"


class BoundaryPlot(pg.PlotWidget):
    """Boundary plat. Emits courseClicked(1-based call #) on near-click."""

    courseClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent, background="#101418")
        self.setAspectLocked(True)
        self.showGrid(x=True, y=True, alpha=0.10)
        self._show_grid = True
        self.setLabel("bottom", _AXIS_EAST_LOCAL)
        self.setLabel("left", _AXIS_NORTH_LOCAL)
        self.plotItem.setMenuEnabled(False)
        # Do not clipToView: long 2-point boundary segments vanish when zoomed
        # into the midspan (both endpoints off-screen but the chord crosses view).

        vb = self.plotItem.getViewBox()
        vb.setMouseMode(vb.PanMode)  # left-drag pan (matches document viewer)

        self._course_labels: list = []
        self._corner_labels: list = []
        self._monument_items: list = []
        self._segment_items: dict[int, pg.PlotDataItem] = {}
        self._segments: list[Segment] = []
        self._segments_by_seq: dict[int, Segment] = {}
        self._highlighted: int | None = None
        self._show_courses = True
        self._show_corners = True
        self._show_monuments = True
        self._show_ties = True
        self._tie_items: list = []  # dashed geometry + POC marker
        self._tie_label_items: list = []  # tie course labels / leaders / POC text
        self._has_plotted = False
        self._label_state: dict | None = None
        self._empty_hint: pg.TextItem | None = None
        self._suppress_label_reflow = False
        self._labels_deferred = False
        self._deferred_finish_pending = False
        self._deferred_needs_fit = True

        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.setInterval(LABEL_REFLOW_MS)
        self._reflow_timer.timeout.connect(self._reflow_labels)
        vb.sigRangeChanged.connect(self._on_range_changed)

        self.scene().sigMouseClicked.connect(self._on_scene_click)

        self._north = NorthArrow(self)
        self._legend = PlotLegend(self)
        self._scalebar = PlotScaleBar()
        self._scalebar.attach(self)
        self._scalebar.hide()
        self._show_empty_hint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_north"):
            self._north.move(self.width() - self._north.width() - 14, 10)
        if hasattr(self, "_legend"):
            self._legend.move(10, 10)
        if hasattr(self, "_labels_deferred"):
            self._try_finish_deferred_labels()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_labels_deferred"):
            self._try_finish_deferred_labels()

    def _on_range_changed(self, *_args):
        if self._has_plotted:
            self._refresh_scale_bar()
        if self._suppress_label_reflow:
            return
        if self._labels_deferred:
            self._try_finish_deferred_labels()
            return
        if self._label_state is not None:
            self._reflow_timer.start()

    def _refresh_scale_bar(self):
        vb = self.plotItem.getViewBox()
        self._scalebar.attach(self)
        self._scalebar.update_from_view(vb.viewRange())

    def _view_ready_for_labels(self) -> bool:
        """Hidden / zero-size tabs yield bad px scale and a later label jump."""
        vb = self.plotItem.getViewBox()
        return self.isVisible() and vb.width() >= 40 and vb.height() >= 40

    def _try_finish_deferred_labels(self):
        if not self._labels_deferred or not self._label_state:
            return
        if not self._view_ready_for_labels():
            return
        if self._deferred_finish_pending:
            return
        # Layout may still be settling on first show — run after the event.
        self._deferred_finish_pending = True
        QTimer.singleShot(0, self._finish_deferred_labels)

    def _finish_deferred_labels(self):
        self._deferred_finish_pending = False
        if not self._labels_deferred or not self._label_state:
            return
        if not self._view_ready_for_labels():
            return
        self._labels_deferred = False
        self._suppress_label_reflow = True
        try:
            vb = self.plotItem.getViewBox()
            # Only fit when the deferred replot had no preserved range
            # (restore / first plot). Keep zoom after call-table edits.
            if self._deferred_needs_fit:
                vb.autoRange(padding=0.14)
            self._refresh_scale_bar()
            self._reflow_timer.stop()
            self._clear_label_items()
            state = self._label_state
            self._place_labels(
                state["result"], state["pob_monument"], state["is_open"],
                state["tie_segments"],
            )
        finally:
            # Keep suppress briefly — aspect-lock may emit one more range tweak.
            self._reflow_timer.stop()
            QTimer.singleShot(80, self._release_reflow_suppress)

    def _release_reflow_suppress(self):
        self._suppress_label_reflow = False
        self._reflow_timer.stop()

    def _clear_label_items(self):
        for it in (
            self._course_labels
            + self._corner_labels
            + self._monument_items
            + self._tie_label_items
        ):
            try:
                self.removeItem(it)
            except Exception:
                pass
        self._course_labels.clear()
        self._corner_labels.clear()
        self._monument_items.clear()
        self._tie_label_items.clear()

    def _show_empty_hint(self):
        if self._empty_hint is not None:
            return
        hint = pg.TextItem(
            "No traverse plotted\nOpen a deed and Parse, or add calls",
            color="#90a4ae", anchor=(0.5, 0.5),
        )
        hint.setFont(QFont("Segoe UI", 11))
        hint.setPos(0, 0)
        self.addItem(hint, ignoreBounds=True)
        self._empty_hint = hint

    def _hide_empty_hint(self):
        if self._empty_hint is None:
            return
        try:
            self.removeItem(self._empty_hint)
        except Exception:
            pass
        self._empty_hint = None

    def clear_plot(self):
        """Wipe geometry and labels (empty call table / clear traverse)."""
        self.clear()
        self._course_labels.clear()
        self._corner_labels.clear()
        self._monument_items.clear()
        self._segment_items.clear()
        self._tie_items.clear()
        self._tie_label_items.clear()
        self._segments = []
        self._segments_by_seq = {}
        self._highlighted = None
        self._label_state = None
        self._labels_deferred = False
        self._deferred_finish_pending = False
        self._deferred_needs_fit = True
        self._has_plotted = False
        self._scalebar.hide()
        self._legend.clear_contents()
        # clear() already removed the hint item; drop the stale ref so we can recreate it.
        self._empty_hint = None
        self._show_empty_hint()
        self.setLabel("bottom", _AXIS_EAST_LOCAL)
        self.setLabel("left", _AXIS_NORTH_LOCAL)

    # ---------- layer toggles (no recompute) ----------

    def set_show_courses(self, on: bool):
        self._show_courses = on
        for it in self._course_labels:
            it.setVisible(on)

    def set_show_corners(self, on: bool):
        self._show_corners = on
        for it in self._corner_labels:
            it.setVisible(on)

    def set_show_monuments(self, on: bool):
        self._show_monuments = on
        for it in self._monument_items:
            it.setVisible(on)

    def set_show_ties(self, on: bool):
        self._show_ties = on
        for it in self._tie_items:
            it.setVisible(on)
        for it in self._tie_label_items:
            it.setVisible(on)
        self._refresh_legend()

    def _refresh_legend(self) -> None:
        """Show only glyphs that are currently drawn on the plot."""
        state = self._label_state
        if not self._has_plotted or not state:
            self._legend.clear_contents()
            return
        result = state["result"]
        is_open = state["is_open"]
        tie_segments = state["tie_segments"]
        segs = result.segments
        has_line = any(s.kind != "curve" for s in segs)
        has_curve = any(s.kind == "curve" for s in segs)
        corners = result.segment_endpoints
        has_corners = len(corners) > 2
        has_misclose = is_open and result.closure_error > 1e-9
        self._legend.set_contents(
            pob=True,
            open_end=is_open,
            corner=has_corners,
            ties=bool(tie_segments) and self._show_ties,
            boundary=has_line,
            curve=has_curve,
            misclose=has_misclose,
        )

    def set_show_grid(self, on: bool):
        self._show_grid = on
        if on:
            self.showGrid(x=True, y=True, alpha=0.10)
        else:
            self.showGrid(x=False, y=False, alpha=0.0)

    def fit_view(self):
        """Fit all visible geometry (ViewBox skips invisible items, including ties)."""
        if not self._has_plotted:
            return
        self._suppress_label_reflow = True
        try:
            self.plotItem.getViewBox().autoRange(padding=0.14)
            self._reflow_timer.stop()
        finally:
            self._reflow_timer.stop()
            QTimer.singleShot(80, self._release_reflow_suppress)
        if self._label_state and self._view_ready_for_labels():
            self._reflow_labels()

    # ---------- highlighting ----------

    def highlight_segment(self, sequence: int | None):
        """Emphasize one course (1-based call number), or clear with None."""
        self._highlighted = sequence
        for seq, item in self._segment_items.items():
            seg = self._segments_by_seq.get(seq)
            if seg and seg.kind == "curve":
                base, width = COLOR_CURVE, CURVE_WIDTH
            else:
                base, width = COLOR_LINE, LINE_WIDTH
            if sequence is not None and seq == sequence:
                item.setPen(pg.mkPen(COLOR_HIGHLIGHT, width=HIGHLIGHT_WIDTH))
            else:
                item.setPen(pg.mkPen(base, width=width))

    # ---------- label text ----------

    @staticmethod
    def _length_in_call_units(length_ft: float, units: str) -> float:
        from cogo import FEET_PER_UNIT
        factor = FEET_PER_UNIT.get((units or "feet").lower().strip(), 1.0)
        if factor <= 0:
            return length_ft
        return length_ft / factor

    @classmethod
    def _course_text(cls, seg: Segment) -> str:
        call = seg.call
        unit = UNIT_ABBR.get(call.units.lower().strip(), f" {call.units}")
        header = f"#{seg.sequence}"
        if seg.kind == "line":
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            brg = call.bearing or azimuth_to_bearing(math.degrees(math.atan2(dx, dy)) % 360.0)
            return f"{header}\n{brg}\n{call.distance:g}{unit}"
        turn = curve_turn_letter(call.curve_direction)
        parts = [f"{header}  Curve {turn}"]
        if call.radius:
            parts.append(f"R={call.radius:g}{unit}")
        if call.arc_length:
            parts.append(f"L={call.arc_length:g}{unit}")
        brg = call.chord_bearing or call.bearing
        # Stated chord wins. Otherwise use the drawn chord (seg.length_ft),
        # never Dist — Parse often stores arc length there with Chord Len empty.
        chord = call.chord_length
        if not chord and seg.length_ft > 0:
            chord = cls._length_in_call_units(seg.length_ft, call.units)
        if brg:
            parts.append(f"CB {brg}")
        if chord:
            parts.append(f"C={chord:g}{unit}")
        return "\n".join(parts)

    # ---------- main entry ----------

    def plot_calls(
        self,
        calls: list[Call],
        pob_monument: str = "",
        tie_calls: list[Call] | None = None,
        *,
        fit: bool = False,
        pob_coordinates: dict | None = None,
    ) -> TraverseResult:
        vb = self.plotItem.getViewBox()
        prev_range = None
        if self._has_plotted and not fit:
            prev_range = vb.viewRange()
        vb.disableAutoRange()
        self.clear()
        self._hide_empty_hint()
        self._course_labels.clear()
        self._corner_labels.clear()
        self._monument_items.clear()
        self._segment_items.clear()
        self._tie_items.clear()
        self._tie_label_items.clear()
        self._label_state = None
        self._labels_deferred = False
        self._deferred_finish_pending = False
        self._deferred_needs_fit = True

        result = compute_traverse(calls, start=traverse_start_from_pob(pob_coordinates))
        if stated_pob_xy(pob_coordinates) is not None:
            self.setLabel("bottom", _AXIS_EAST_POB)
            self.setLabel("left", _AXIS_NORTH_POB)
        else:
            self.setLabel("bottom", _AXIS_EAST_LOCAL)
            self.setLabel("left", _AXIS_NORTH_LOCAL)
        self._segments = result.segments
        self._segments_by_seq = {s.sequence: s for s in result.segments}
        if len(result.points) < 2 and not result.gaps:
            self._has_plotted = False
            self._scalebar.hide()
            self._legend.clear_contents()
            self._show_empty_hint()
            return result

        xs = [p[0] for p in result.points]
        ys = [p[1] for p in result.points]

        for seg in result.segments:
            color = COLOR_CURVE if seg.kind == "curve" else COLOR_LINE
            width = CURVE_WIDTH if seg.kind == "curve" else LINE_WIDTH
            item = pg.PlotDataItem(
                [p[0] for p in seg.path], [p[1] for p in seg.path],
                pen=pg.mkPen(color, width=width),
            )
            self.addItem(item)
            self._segment_items[seg.sequence] = item

        for seq, vertex in result.gaps:
            self.addItem(pg.ScatterPlotItem(
                [vertex[0]], [vertex[1]], size=16, brush=pg.mkBrush(COLOR_GAP),
                pen=pg.mkPen("#fff", width=1.5), symbol="t", pxMode=True,
            ))
            miss = pg.TextItem(f"CALL {seq} MISSING", color=COLOR_GAP, anchor=(0.5, 1.2))
            miss.setFont(QFont("Segoe UI", 8))
            miss.setPos(*vertex)
            self.addItem(miss, ignoreBounds=True)

        start = result.points[0]
        end = result.points[-1]
        is_open = not is_traverse_closed(result)

        if is_open and result.closure_error > 1e-9:
            gap = pg.PlotDataItem(
                [end[0], start[0]], [end[1], start[1]],
                pen=pg.mkPen(COLOR_CLOSURE, width=1.5, style=Qt.PenStyle.DashLine),
            )
            self.addItem(gap)
            # Annotate misclose near midpoint of the gap.
            mid = ((end[0] + start[0]) / 2.0, (end[1] + start[1]) / 2.0)
            gap_txt = f"MISCLOSE {result.closure_error:.3f}'"
            if result.closure_bearing:
                gap_txt += f"\n{result.closure_bearing}"
            gap_label = pg.TextItem(gap_txt, color=COLOR_CLOSURE, anchor=(0.5, 0.5))
            gap_label.setFont(QFont("Segoe UI", 8))
            gap_label.setPos(*mid)
            self.addItem(gap_label, ignoreBounds=True)

        corners = result.segment_endpoints
        cx = [p[0] for p in corners[1:-1]] if len(corners) > 2 else []
        cy = [p[1] for p in corners[1:-1]] if len(corners) > 2 else []
        if cx:
            self.addItem(pg.ScatterPlotItem(
                cx, cy, size=7, brush=pg.mkBrush(COLOR_CORNER),
                pen=pg.mkPen("#333"), symbol="o", pxMode=True,
            ))
        self.addItem(pg.ScatterPlotItem(
            [start[0]], [start[1]], size=11, brush=pg.mkBrush(COLOR_POB),
            pen=pg.mkPen("#fff", width=1), symbol="s", pxMode=True,
        ))
        if is_open:
            self.addItem(pg.ScatterPlotItem(
                [end[0]], [end[1]], size=11, brush=pg.mkBrush(COLOR_END),
                pen=pg.mkPen("#fff", width=1), symbol="x", pxMode=True,
            ))

        tie_segments: list[Segment] = []
        if tie_calls:
            tie_res = compute_traverse(tie_calls, start=(0.0, 0.0))
            for err in tie_res.errors:
                result.warnings.append(f"Tie: {err}")
            for warn in tie_res.warnings:
                result.warnings.append(f"Tie: {warn}")
            if tie_res.segments:
                last = tie_res.segments[-1].end
                shift = (start[0] - last[0], start[1] - last[1])
                for seg in tie_res.segments:
                    path = [(p[0] + shift[0], p[1] + shift[1]) for p in seg.path]
                    item = pg.PlotDataItem(
                        [p[0] for p in path], [p[1] for p in path],
                        pen=pg.mkPen(COLOR_TIE, width=2.0, style=Qt.PenStyle.DashLine),
                    )
                    item.setVisible(self._show_ties)
                    # Always participate in Fit bounds; ViewBox.childrenBounds
                    # skips invisible items, so hiding ties is enough.
                    self.addItem(item)
                    self._tie_items.append(item)
                    tie_segments.append(Segment(
                        sequence=seg.sequence, kind=seg.kind,
                        start=(seg.start[0] + shift[0], seg.start[1] + shift[1]),
                        end=(seg.end[0] + shift[0], seg.end[1] + shift[1]),
                        path=path, length_ft=seg.length_ft, call=seg.call,
                    ))
                poc = tie_segments[0].start
                marker = pg.ScatterPlotItem(
                    [poc[0]], [poc[1]], size=9, brush=pg.mkBrush(COLOR_TIE),
                    pen=pg.mkPen("#fff", width=1), symbol="t1", pxMode=True,
                )
                marker.setVisible(self._show_ties)
                self.addItem(marker)
                self._tie_items.append(marker)

        self._label_state = {
            "result": result,
            "pob_monument": pob_monument,
            "is_open": is_open,
            "tie_segments": tie_segments,
        }
        self._has_plotted = True
        self._refresh_legend()

        # autoRange fires sigRangeChanged; suppress so we don't place twice
        # (immediate + 140ms reflow = visible label jump).
        self._suppress_label_reflow = True
        try:
            needs_fit = bool(fit or prev_range is None)
            self._deferred_needs_fit = needs_fit
            if needs_fit:
                vb.autoRange(padding=0.14)
            else:
                vb.setRange(xRange=prev_range[0], yRange=prev_range[1], padding=0)
            self._refresh_scale_bar()
            self._reflow_timer.stop()
            if self._view_ready_for_labels():
                self._place_labels(result, pob_monument, is_open, tie_segments)
            else:
                # Restore / replot while Plot tab is hidden — place once on show.
                self._labels_deferred = True
        finally:
            self._reflow_timer.stop()
            QTimer.singleShot(80, self._release_reflow_suppress)

        self.highlight_segment(self._highlighted)
        return result

    def _reflow_labels(self):
        state = self._label_state
        if not state:
            return
        if not self._view_ready_for_labels():
            self._labels_deferred = True
            return
        self._clear_label_items()
        self._place_labels(
            state["result"], state["pob_monument"], state["is_open"],
            state["tie_segments"],
        )

    def _on_scene_click(self, event):
        if not event.double():
            # Single click near a course → select that call.
            if event.button() != Qt.MouseButton.LeftButton:
                return
        vb = self.plotItem.getViewBox()
        try:
            pos = event.scenePos()
            mouse = vb.mapSceneToView(pos)
            click = (mouse.x(), mouse.y())
        except Exception:
            return
        best_seq = None
        best_dist = float("inf")
        (x0, x1), (y0, y1) = vb.viewRange()
        thresh = max(x1 - x0, y1 - y0) * 0.02
        for seg in self._segments:
            d = self._dist_point_to_segment(click, seg)
            if d < best_dist:
                best_dist = d
                best_seq = seg.sequence
        if best_seq is not None and best_dist <= thresh:
            self.courseClicked.emit(best_seq)

    @staticmethod
    def _dist_point_to_segment(pt, seg: Segment) -> float:
        # Sample path polyline for curves.
        pts = seg.path if len(seg.path) >= 2 else [seg.start, seg.end]
        best = float("inf")
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            if length2 < 1e-18:
                dist = math.hypot(pt[0] - ax, pt[1] - ay)
            else:
                t = max(0.0, min(1.0, ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / length2))
                dist = math.hypot(pt[0] - (ax + t * dx), pt[1] - (ay + t * dy))
            if dist < best:
                best = dist
        return best

    # ---------- label placement ----------

    def _px_scale(self) -> float:
        vb = self.plotItem.getViewBox()
        (x0, x1), _ = vb.viewRange()
        width_px = max(vb.width(), 1.0)
        span = max(x1 - x0, 1e-9)
        return width_px / span

    def _text_size_px(self, text: str, font: QFont) -> tuple[float, float]:
        """Paint-accurate label size (fill/border padding included)."""
        fm = QFontMetrics(font)
        lines = (text or "").split("\n") or [""]
        width = max((fm.horizontalAdvance(line) for line in lines), default=0) + 18
        height = fm.height() * len(lines) + 12
        return float(max(width, 24)), float(max(height, 18))

    def _place_labels(self, result: TraverseResult, pob_monument: str, is_open: bool,
                      tie_segments: list[Segment] | None = None):
        scale = self._px_scale()
        if scale <= 0:
            return

        def to_px(pt):
            return (pt[0] * scale, -pt[1] * scale)

        def from_px_offset(off):
            return (off[0] / scale, -off[1] / scale)

        placed: list[tuple[float, float, float, float]] = []
        leaders: list[tuple[tuple[float, float], tuple[float, float]]] = []
        corners = result.segment_endpoints
        polygon_px = [to_px(p) for p in corners] if len(corners) >= 3 else None

        for pt in corners:
            px = to_px(pt)
            placed.append(point_box(px[0], px[1], 12))

        # Reserve corner # / OPEN tags so callouts don't sit on them.
        tag_font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        for seg in result.segments:
            if not is_open and seg is result.segments[-1]:
                continue
            px = to_px(seg.end)
            w, h = self._text_size_px(str(seg.sequence), QFont("Segoe UI", 9))
            placed.append((px[0] - w / 2, px[1] - h * 1.6, px[0] + w / 2, px[1] - 2))
        if is_open and len(corners) > 1:
            px = to_px(corners[-1])
            w, h = self._text_size_px("OPEN", tag_font)
            placed.append((px[0] - w / 2, px[1] - h * 1.8, px[0] + w / 2, px[1] - 2))

        centroid = self._centroid(corners)

        # Courses first, locked to the outward half-plane so dotted leaders
        # never flip through the traverse to dodge monuments.
        course_font = QFont("Segoe UI", 9)
        for seg in result.segments:
            mid = self._segment_mid(seg)
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            length = math.hypot(dx, dy)
            if length < 1e-9:
                continue
            screen_len = length * scale
            text = self._course_text(seg)
            if screen_len < 40:
                text = f"#{seg.sequence}"
            # Perp away from centroid so course leaders stay outside the figure.
            perp = (dy / length, -dx / length)
            to_out = (mid[0] - centroid[0], mid[1] - centroid[1])
            if perp[0] * to_out[0] + perp[1] * to_out[1] < 0:
                perp = (-perp[0], -perp[1])
            # find_offset uses screen-Y flip via to_px preferred_dir
            perp_px = (perp[0], -perp[1])
            size = self._text_size_px(text, course_font)
            n_lines = text.count("\n") + 1
            pref_dist = 14 + n_lines * COURSE_FONT_PX * 0.75 + size[1] * 0.15
            preferred = (perp_px[0] * pref_dist, perp_px[1] * pref_dist)
            offset_px = find_offset_px(
                to_px(mid), text, perp_px, placed,
                font_px=COURSE_FONT_PX, preferred_offset=preferred, size_px=size,
                leaders=leaders, polygon_px=polygon_px, max_turn_deg=95.0,
            )
            data_off = from_px_offset(offset_px)
            label_pos = (mid[0] + data_off[0], mid[1] + data_off[1])
            if math.hypot(*offset_px) > 30:
                leader = pg.PlotDataItem(
                    [mid[0], label_pos[0]], [mid[1], label_pos[1]],
                    pen=pg.mkPen(140, 160, 175, 220, width=2.0, style=Qt.PenStyle.DotLine),
                )
                leader.setVisible(self._show_courses)
                self.addItem(leader, ignoreBounds=True)
                self._course_labels.append(leader)
            # Keep boxed multi-line labels horizontal — rotating along N/S
            # courses made bearings unreadable (sideways).
            label = pg.TextItem(
                text, color=COLOR_COURSE_TEXT, anchor=(0.5, 0.5),
                fill=LABEL_FILL, border=LABEL_BORDER,
            )
            label.setFont(course_font)
            label.setPos(*label_pos)
            label.setVisible(self._show_courses)
            self.addItem(label, ignoreBounds=True)
            self._course_labels.append(label)

        # Monuments after courses — swing around exterior course leaders.
        monument_font = QFont("Segoe UI", MONUMENT_FONT_PX)
        seen: dict[tuple[float, float], set[str]] = {}
        corner_mon_count: dict[tuple[float, float], int] = {}
        for seg in result.segments:
            if not seg.call.monument:
                continue
            text = format_monument_label(seg.call.monument)
            if not text:
                continue
            key = (round(seg.end[0], 2), round(seg.end[1], 2))
            norm = text.lower()
            if norm in seen.setdefault(key, set()):
                continue
            seen[key].add(norm)
            fan = corner_mon_count.get(key, 0)
            corner_mon_count[key] = fan + 1
            self._add_monument_callout(
                seg.end, text, centroid, placed, to_px, from_px_offset, monument_font,
                leaders=leaders, polygon_px=polygon_px, fan_index=fan,
            )
        if pob_monument:
            text = format_monument_label(pob_monument)
            if text:
                key = (round(corners[0][0], 2), round(corners[0][1], 2))
                norm = text.lower()
                if norm not in seen.setdefault(key, set()):
                    seen[key].add(norm)
                    fan = corner_mon_count.get(key, 0)
                    corner_mon_count[key] = fan + 1
                    self._add_monument_callout(
                        corners[0], text, centroid, placed, to_px, from_px_offset,
                        monument_font,
                        leaders=leaders, polygon_px=polygon_px, fan_index=fan,
                    )

        # Corner tags drawn last (small); space already reserved above.
        # POB is the green square only — no text (legend covers it).
        for seg in result.segments:
            if not is_open and seg is result.segments[-1]:
                continue
            tag = pg.TextItem(str(seg.sequence), color="#eeeeee", anchor=(0.5, 1.4))
            tag.setFont(QFont("Segoe UI", 9))
            tag.setPos(seg.end[0], seg.end[1])
            tag.setVisible(self._show_corners)
            self.addItem(tag, ignoreBounds=True)
            self._corner_labels.append(tag)
        if is_open and len(corners) > 1:
            tag = pg.TextItem("OPEN", color=COLOR_END, anchor=(0.5, 1.5))
            tag.setFont(tag_font)
            tag.setPos(corners[-1][0], corners[-1][1])
            tag.setVisible(self._show_corners)
            self.addItem(tag, ignoreBounds=True)
            self._corner_labels.append(tag)

        for seg in tie_segments or []:
            mid = self._segment_mid(seg)
            dx = seg.end[0] - seg.start[0]
            dy = seg.end[1] - seg.start[1]
            length = math.hypot(dx, dy)
            if length < 1e-9:
                continue
            perp = (dy / length, -dx / length)
            to_out = (mid[0] - centroid[0], mid[1] - centroid[1])
            if perp[0] * to_out[0] + perp[1] * to_out[1] < 0:
                perp = (-perp[0], -perp[1])
            perp_px = (perp[0], -perp[1])
            call = seg.call
            unit = UNIT_ABBR.get(call.units.lower().strip(), f" {call.units}")
            if seg.kind == "curve":
                text = self._course_text(seg).replace(f"#{seg.sequence}", "TIE", 1)
            else:
                brg = call.chord_bearing or call.bearing or azimuth_to_bearing(
                    math.degrees(math.atan2(dx, dy)) % 360.0)
                label_len = call.distance
                if not label_len and seg.length_ft > 0:
                    label_len = self._length_in_call_units(seg.length_ft, call.units)
                text = f"TIE\n{brg}\n{label_len:g}{unit}"
            size = self._text_size_px(text, QFont("Segoe UI", 8))
            offset_px = find_offset_px(
                to_px(mid), text, perp_px, placed,
                font_px=MONUMENT_FONT_PX, size_px=size,
                leaders=leaders, polygon_px=polygon_px, max_turn_deg=95.0,
            )
            data_off = from_px_offset(offset_px)
            label_pos = (mid[0] + data_off[0], mid[1] + data_off[1])
            if math.hypot(*offset_px) > 30:
                leader = pg.PlotDataItem(
                    [mid[0], label_pos[0]], [mid[1], label_pos[1]],
                    pen=pg.mkPen(COLOR_TIE, width=2.0, style=Qt.PenStyle.DotLine),
                )
                leader.setVisible(self._show_ties)
                self.addItem(leader, ignoreBounds=True)
                self._tie_label_items.append(leader)
            label = pg.TextItem(
                text, color=COLOR_TIE_TEXT, anchor=(0.5, 0.5),
                fill=LABEL_FILL, border=LABEL_BORDER,
            )
            label.setFont(QFont("Segoe UI", 8))
            label.setPos(*label_pos)
            label.setVisible(self._show_ties)
            self.addItem(label, ignoreBounds=True)
            self._tie_label_items.append(label)

        if tie_segments:
            poc = tie_segments[0].start
            away = (poc[0] - corners[0][0], -(poc[1] - corners[0][1]))
            norm = math.hypot(*away)
            away = (away[0] / norm, away[1] / norm) if norm > 1e-9 else (0.0, 1.0)
            size = self._text_size_px("POC", QFont("Segoe UI", 9, QFont.Weight.Bold))
            offset_px = find_offset_px(
                to_px(poc), "POC", away, placed,
                font_px=MONUMENT_FONT_PX, anchor_clearance_px=14, size_px=size,
                leaders=leaders, polygon_px=polygon_px, record_leader=False,
            )
            data_off = from_px_offset(offset_px)
            tag = pg.TextItem("POC", color=COLOR_TIE_TEXT, anchor=(0.5, 0.5))
            tag.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            tag.setPos(poc[0] + data_off[0], poc[1] + data_off[1])
            tag.setVisible(self._show_ties)
            self.addItem(tag, ignoreBounds=True)
            self._tie_label_items.append(tag)

    def _add_monument_callout(
        self, anchor, text, centroid, placed, to_px, from_px_offset, font=None,
        *, leaders=None, polygon_px=None, fan_index: int = 0,
    ):
        font = font or QFont("Segoe UI", MONUMENT_FONT_PX)
        ox = anchor[0] - centroid[0]
        oy = anchor[1] - centroid[1]
        length = math.hypot(ox, oy)
        outward = (ox / length, -oy / length) if length > 1e-9 else (1.0, 0.0)
        # Fan multiple callouts at the same corner so leaders don't stack.
        if fan_index:
            step = 40 + 30 * ((fan_index - 1) // 2)
            sign = 1 if fan_index % 2 else -1
            ang = math.atan2(outward[1], outward[0]) + math.radians(sign * step)
            outward = (math.cos(ang), math.sin(ang))
        size = self._text_size_px(text, font)
        offset_px = find_offset_px(
            to_px(anchor), text, outward, placed,
            font_px=MONUMENT_FONT_PX, anchor_clearance_px=18, size_px=size,
            leaders=leaders, polygon_px=polygon_px,
        )
        data_off = from_px_offset(offset_px)
        label_pos = (anchor[0] + data_off[0], anchor[1] + data_off[1])

        leader = pg.PlotDataItem(
            [anchor[0], label_pos[0]], [anchor[1], label_pos[1]],
            pen=pg.mkPen(COLOR_MONUMENT_LEADER, width=1),
        )
        leader.setVisible(self._show_monuments)
        self.addItem(leader, ignoreBounds=True)
        self._monument_items.append(leader)

        label = pg.TextItem(
            text, color=COLOR_MONUMENT_TEXT, anchor=(0.5, 0.5),
            fill=MONUMENT_FILL, border=MONUMENT_BORDER,
        )
        label.setFont(font)
        label.setPos(*label_pos)
        label.setVisible(self._show_monuments)
        self.addItem(label, ignoreBounds=True)
        self._monument_items.append(label)

    @staticmethod
    def _segment_mid(seg: Segment) -> tuple[float, float]:
        if seg.kind == "curve" and len(seg.path) > 2:
            return seg.path[len(seg.path) // 2]
        return ((seg.start[0] + seg.end[0]) / 2.0, (seg.start[1] + seg.end[1]) / 2.0)

    @staticmethod
    def _centroid(points) -> tuple[float, float]:
        if not points:
            return (0.0, 0.0)
        return (sum(p[0] for p in points) / len(points),
                sum(p[1] for p in points) / len(points))
