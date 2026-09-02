"""PyQt6 render backend for ezdxf's drawing Frontend.

ezdxf ships a Qt backend, but it only binds PySide6 / PyQt5. This is a lean
PyQt6 port good enough for Deed Plotter exports (lines, arcs, text, MTEXT).
"""

from __future__ import annotations

import math
from typing import Iterable, Optional

from PyQt6.QtCore import QPointF, QRectF, QSizeF, Qt
from PyQt6.QtGui import QBrush, QColor, QImage, QPainter, QPen, QPixmap, QPolygonF, QTransform
from PyQt6.QtWidgets import (
    QAbstractGraphicsShapeItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsPolygonItem,
    QGraphicsScene,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ezdxf.addons.drawing.backend import Backend, BkPath2d, BkPoints2d, ImageData
from ezdxf.addons.drawing.config import Configuration
from ezdxf.addons.drawing.properties import BackendProperties
from ezdxf.addons.drawing.type_hints import Color
from ezdxf.math import Matrix44, Vec2

# Scene-item data roles (layer name for visibility toggles).
ROLE_LAYER = int(Qt.ItemDataRole.UserRole) + 10
ROLE_HANDLE = int(Qt.ItemDataRole.UserRole) + 11


class _PointItem(QAbstractGraphicsShapeItem):
    """Screen-constant point (like AutoCAD PDMODE nodes)."""

    def __init__(self, x: float, y: float, brush: QBrush):
        super().__init__()
        self.location = QPointF(x, y)
        self.radius = 1.0
        self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(brush)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: Optional[QWidget] = None,
    ) -> None:
        del option, widget
        view_scale = _x_scale(painter.transform())
        radius = self.radius / max(view_scale, 1e-9)
        painter.setBrush(self.brush())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(self.location, radius, radius)

    def boundingRect(self) -> QRectF:
        return QRectF(self.location, QSizeF(1, 1))


class PyQt6Backend(Backend):
    """ezdxf Backend that paints into a PyQt6 QGraphicsScene."""

    def __init__(self, scene: QGraphicsScene | None = None):
        super().__init__()
        self._scene = scene or QGraphicsScene()
        self._color_cache: dict[Color, QColor] = {}
        self._no_line = QPen(Qt.PenStyle.NoPen)
        self._no_fill = QBrush(Qt.BrushStyle.NoBrush)

    @property
    def scene(self) -> QGraphicsScene:
        return self._scene

    def set_scene(self, scene: QGraphicsScene) -> None:
        self._scene = scene

    def configure(self, config: Configuration) -> None:
        if config.min_lineweight is None:
            config = config.with_changes(min_lineweight=0.24)
        super().configure(config)

    def _add_item(self, item: QGraphicsItem, properties: BackendProperties) -> None:
        item.setData(ROLE_LAYER, properties.layer)
        item.setData(ROLE_HANDLE, properties.handle)
        self._scene.addItem(item)

    def _get_color(self, color: Color) -> QColor:
        try:
            return self._color_cache[color]
        except KeyError:
            pass
        if len(color) == 7:
            qt_color = QColor(color)
        elif len(color) == 9:
            qt_color = QColor(f"#{color[7:9]}{color[1:7]}")
        else:
            raise TypeError(color)
        self._color_cache[color] = qt_color
        return qt_color

    def _get_pen(self, properties: BackendProperties) -> QPen:
        px = properties.lineweight / 0.3527 * self.config.lineweight_scaling
        pen = QPen(self._get_color(properties.color), max(px, 0.5))
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        return pen

    def _get_fill_brush(self, color: Color) -> QBrush:
        return QBrush(self._get_color(color), Qt.BrushStyle.SolidPattern)

    def set_background(self, color: Color) -> None:
        self._scene.setBackgroundBrush(QBrush(self._get_color(color)))

    def draw_point(self, pos: Vec2, properties: BackendProperties) -> None:
        item = _PointItem(pos.x, pos.y, self._get_fill_brush(properties.color))
        self._add_item(item, properties)

    def draw_line(self, start: Vec2, end: Vec2, properties: BackendProperties) -> None:
        if start.isclose(end):
            self.draw_point(start, properties)
            return
        item = QGraphicsLineItem(start.x, start.y, end.x, end.y)
        item.setPen(self._get_pen(properties))
        self._add_item(item, properties)

    def draw_solid_lines(
        self,
        lines: Iterable[tuple[Vec2, Vec2]],
        properties: BackendProperties,
    ) -> None:
        pen = self._get_pen(properties)
        for start, end in lines:
            if start.isclose(end):
                self.draw_point(start, properties)
            else:
                item = QGraphicsLineItem(start.x, start.y, end.x, end.y)
                item.setPen(pen)
                self._add_item(item, properties)

    def draw_path(self, path: BkPath2d, properties: BackendProperties) -> None:
        if len(path) == 0:
            return
        qpath = _path_to_qpath(path, self.config.max_flattening_distance)
        item = QGraphicsPathItem(qpath)
        item.setPen(self._get_pen(properties))
        item.setBrush(self._no_fill)
        self._add_item(item, properties)

    def draw_filled_paths(
        self, paths: Iterable[BkPath2d], properties: BackendProperties
    ) -> None:
        from PyQt6.QtGui import QPainterPath

        qpath = QPainterPath()
        qpath.setFillRule(Qt.FillRule.OddEvenFill)
        for path in paths:
            if len(path) == 0:
                continue
            qpath.addPath(_path_to_qpath(path, self.config.max_flattening_distance))
        if qpath.isEmpty():
            return
        item = QGraphicsPathItem(qpath)
        # Fill only — stroking glyph outlines draws "travel" lines between contours.
        item.setPen(self._no_line)
        item.setBrush(self._get_fill_brush(properties.color))
        self._add_item(item, properties)

    def draw_filled_polygon(
        self, points: BkPoints2d, properties: BackendProperties
    ) -> None:
        polygon = QPolygonF([QPointF(p.x, p.y) for p in points.vertices()])
        item = QGraphicsPolygonItem(polygon)
        item.setPen(self._no_line)
        item.setBrush(self._get_fill_brush(properties.color))
        self._add_item(item, properties)

    def draw_image(self, image_data: ImageData, properties: BackendProperties) -> None:
        import numpy as np

        image = image_data.image
        height, width, depth = image.shape
        assert depth == 4
        image = np.ascontiguousarray(np.flip(image, axis=0))
        qimage = QImage(
            image.data,
            width,
            height,
            width * depth,
            QImage.Format.Format_RGBA8888,
        )
        item = QGraphicsPixmapItem(QPixmap.fromImage(qimage))
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setTransform(_matrix_to_qtransform(image_data.transform))
        self._add_item(item, properties)

    def clear(self) -> None:
        self._scene.clear()

    def finalize(self) -> None:
        super().finalize()
        self._scene.setSceneRect(self._scene.itemsBoundingRect())


def _path_to_qpath(path: BkPath2d, distance: float):
    """Convert an ezdxf path to QPainterPath, preserving MOVE_TO subpaths.

    Naively lining through flattened vertices draws pen-up \"travel\" chords
    between glyph contours (the diagonal slash through O/D/etc.).
    """
    from PyQt6.QtGui import QPainterPath
    from ezdxf.path import Command

    qpath = QPainterPath()
    # Prefer command stream so holes/subpaths stay separate.
    try:
        codes = list(path.command_codes())
        verts = list(path.vertices())
    except Exception:
        codes, verts = [], []

    if codes and verts:
        vi = 0
        qpath.moveTo(verts[0].x, verts[0].y)
        vi = 1
        for cmd in codes:
            if cmd == Command.LINE_TO:
                qpath.lineTo(verts[vi].x, verts[vi].y)
                vi += 1
            elif cmd == Command.CURVE3_TO:
                qpath.quadTo(
                    verts[vi].x, verts[vi].y,
                    verts[vi + 1].x, verts[vi + 1].y,
                )
                vi += 2
            elif cmd == Command.CURVE4_TO:
                qpath.cubicTo(
                    verts[vi].x, verts[vi].y,
                    verts[vi + 1].x, verts[vi + 1].y,
                    verts[vi + 2].x, verts[vi + 2].y,
                )
                vi += 3
            elif cmd == Command.MOVE_TO:
                qpath.moveTo(verts[vi].x, verts[vi].y)
                vi += 1
        return qpath

    # Fallback: flatten each sub-path independently.
    subs = list(path.sub_paths()) if path.has_sub_paths else [path]
    for sub in subs:
        pts = list(sub.flattening(distance=distance))
        if not pts:
            continue
        qpath.moveTo(pts[0].x, pts[0].y)
        for p in pts[1:]:
            qpath.lineTo(p.x, p.y)
    return qpath


def _x_scale(t: QTransform) -> float:
    return math.sqrt(t.m11() * t.m11() + t.m21() * t.m21())


def _matrix_to_qtransform(matrix: Matrix44) -> QTransform:
    return QTransform(*matrix.get_2d_transformation())
