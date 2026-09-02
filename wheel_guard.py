"""Block mouse-wheel value changes on combos and spin boxes.

Wheel events often land on the inner QLineEdit of a spin/combo — resolve those
to the owning selector.

Policy: wheel never changes selector values (use arrows, typing, or the
dropdown). Events are forwarded to a parent scroll area when present so the
page still scrolls. Combo popup lists (QAbstractItemView) still scroll.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)


def _selector_for_wheel(obj: object) -> QWidget | None:
    """Owning combo/spin for ``obj`` (itself or an inner child like QLineEdit)."""
    if not isinstance(obj, QWidget):
        return None
    # Open dropdown / list views must keep wheel scrolling.
    if isinstance(obj, QAbstractItemView):
        return None
    w: QWidget | None = obj
    while w is not None:
        if isinstance(w, QAbstractItemView):
            return None
        if isinstance(w, (QComboBox, QAbstractSpinBox)):
            return w
        w = w.parentWidget()
    return None


def _forward_wheel_to_scroll_area(start: QWidget, event: QEvent) -> None:
    parent = start.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            QApplication.sendEvent(parent.viewport(), event)
            return
        parent = parent.parentWidget()


class _NoWheelOnSelectorsFilter(QObject):
    def eventFilter(self, obj, event):  # noqa: N802 — Qt API
        if event.type() != QEvent.Type.Wheel:
            return False
        selector = _selector_for_wheel(obj)
        if selector is None:
            return False
        _forward_wheel_to_scroll_area(selector, event)
        return True


def enable_no_wheel_on_selectors(app: QApplication) -> None:
    """Install an app-wide filter so selectors ignore the mouse wheel."""
    # Replace any prior filter (e.g. older "unless focused" build in-process).
    old = app.property("_no_wheel_filter")
    if old is not None:
        app.removeEventFilter(old)
    filt = _NoWheelOnSelectorsFilter(app)
    app.setProperty("_no_wheel_filter", filt)
    app.installEventFilter(filt)
