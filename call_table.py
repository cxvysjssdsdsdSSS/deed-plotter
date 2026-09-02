"""Editable table widget for boundary calls."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QItemSelectionModel, QModelIndex, pyqtSignal
from PyQt6.QtGui import QColor, QFocusEvent
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from cogo import (
    Call,
    curve_can_derive_chord,
    newly_derivable_copied_chord,
)

COLUMNS = [
    "#", "Type", "Bearing / Chord Brg", "Distance", "Units",
    "Radius", "Arc Len", "Chord Len", "Delta", "Dir",
    "Monument at End", "Description", "Conf",
]
COL_NUM, COL_TYPE, COL_BEARING, COL_DIST, COL_UNITS = 0, 1, 2, 3, 4
COL_RADIUS, COL_ARC, COL_CHORD, COL_DELTA, COL_DIR = 5, 6, 7, 8, 9
COL_MONUMENT, COL_DESC, COL_CONF = 10, 11, 12

CONF_COLORS = {"low": QColor(255, 120, 120, 70), "medium": QColor(255, 200, 100, 60)}

# Hide the Fusion "current cell" chrome and mute selection when focus leaves
# the table (clicking Plot / Chat / paste box used to leave one blue cell).
_TABLE_STYLE = """
QTableWidget {
    outline: none;
}
QTableWidget::item:focus {
    outline: none;
    border: none;
}
QTableWidget:!focus::item:selected {
    background-color: transparent;
    color: palette(text);
}
"""


class CallTable(QTableWidget):
    callsEdited = pyqtSignal()
    undoPushed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_NUM, 36)
        header.resizeSection(COL_TYPE, 70)
        header.resizeSection(COL_BEARING, 140)
        header.resizeSection(COL_DIST, 80)
        header.resizeSection(COL_UNITS, 60)
        header.resizeSection(COL_RADIUS, 70)
        header.resizeSection(COL_ARC, 70)
        header.resizeSection(COL_CHORD, 80)
        header.resizeSection(COL_DELTA, 80)
        header.resizeSection(COL_DIR, 50)
        header.resizeSection(COL_CONF, 70)
        # Wide text columns get a fixed, user-resizable width so the table
        # scrolls horizontally instead of squeezing everything to fit.
        header.setSectionResizeMode(COL_MONUMENT, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_DESC, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(COL_MONUMENT, 260)
        header.resizeSection(COL_DESC, 300)
        header.setStretchLastSection(False)
        self.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setStyleSheet(_TABLE_STYLE)
        self._loading = False
        self._derive_by_row: list[bool] = []
        self._undo_stack: list[list[Call]] = []
        self._redo_stack: list[list[Call]] = []
        self.itemChanged.connect(self._on_item_changed)

    def focusOutEvent(self, event: QFocusEvent):
        # Drop the current-cell caret when focus leaves. Keep row selection so
        # Edit/Delete toolbar clicks still see selectedIndexes() — setCurrentIndex()
        # would clear selection and disable those buttons before click fires.
        # (!focus stylesheet mutes selection paint while unfocused.)
        super().focusOutEvent(event)
        if self.state() != QAbstractItemView.State.EditingState:
            sm = self.selectionModel()
            if sm is not None:
                sm.setCurrentIndex(
                    QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate
                )

    def mousePressEvent(self, event):
        # Click on empty table chrome → clear leftover selection.
        if not self.indexAt(event.position().toPoint()).isValid():
            self.clearSelection()
            self.setCurrentIndex(QModelIndex())
        super().mousePressEvent(event)

    def set_interactive(self, on: bool) -> None:
        """Lock cell editing while parse/chat/load is running."""
        if on:
            self.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked
                | QAbstractItemView.EditTrigger.EditKeyPressed
                | QAbstractItemView.EditTrigger.AnyKeyPressed
            )
        else:
            self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def clear_edit_history(self):
        self._undo_stack.clear()
        self._redo_stack.clear()

    def can_undo(self) -> bool:
        return bool(self._undo_stack)

    def can_redo(self) -> bool:
        return bool(self._redo_stack)

    def clear_redo(self) -> None:
        self._redo_stack.clear()

    def push_undo(self, *, emit: bool = True):
        """Snapshot current calls before an external batch edit (e.g. chat apply)."""
        self._undo_stack.append(self.get_calls())
        self._redo_stack.clear()
        if len(self._undo_stack) > 40:
            self._undo_stack = self._undo_stack[-40:]
        if emit:
            self.undoPushed.emit()

    def _push_undo(self):
        self.push_undo()

    def undo(self) -> bool:
        if not self._undo_stack:
            return False
        self._redo_stack.append(self.get_calls())
        prior = self._undo_stack.pop()
        self.set_calls(prior)
        self.callsEdited.emit()
        return True

    def redo(self) -> bool:
        if not self._redo_stack:
            return False
        self._undo_stack.append(self.get_calls())
        nxt = self._redo_stack.pop()
        self.set_calls(nxt)
        self.callsEdited.emit()
        return True

    def edit(self, index: QModelIndex, trigger, event) -> bool:
        # Snapshot only when an in-grid edit will actually start (skip # column, etc.).
        if (
            not self._loading
            and index.isValid()
            and self.state() != QAbstractItemView.State.EditingState
        ):
            item = self.itemFromIndex(index)
            if item is not None and (item.flags() & Qt.ItemFlag.ItemIsEditable):
                self._push_undo()
        return super().edit(index, trigger, event)

    def _row_can_derive(self, row: int) -> bool:
        return curve_can_derive_chord(
            self._num(self._text(row, COL_RADIUS)),
            self._text(row, COL_DELTA),
            self._num(self._text(row, COL_ARC)),
        )

    def _refresh_derive_flags(self) -> None:
        self._derive_by_row = [
            self._row_can_derive(r) for r in range(self.rowCount())
        ]

    def _set_derive_flag(self, row: int, value: bool) -> None:
        if 0 <= row < len(self._derive_by_row):
            self._derive_by_row[row] = value
        elif row == len(self._derive_by_row):
            self._derive_by_row.append(value)

    def _clear_invented_chord_if_radius(self, row: int) -> None:
        """Drop Dist→Chord copy when R/Δ newly become derivable (Dist-then-Radius)."""
        derive_now = self._row_can_derive(row)
        derive_before = (
            self._derive_by_row[row]
            if 0 <= row < len(self._derive_by_row)
            else False
        )
        if newly_derivable_copied_chord(
            derive_now, derive_before,
            self._num(self._text(row, COL_DIST)),
            self._num(self._text(row, COL_CHORD)),
        ):
            self._loading = True
            dst = self.item(row, COL_CHORD)
            if dst is None:
                dst = QTableWidgetItem("")
                self.setItem(row, COL_CHORD, dst)
            dst.setText("")
            self._loading = False
        self._set_derive_flag(row, derive_now)

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._loading:
            return
        row, col = item.row(), item.column()
        call_type = self._text(row, COL_TYPE).lower()
        if call_type == "curve" and col in (COL_DIST, COL_CHORD):
            # Dist is often the transcribed arc. Don't invent a stated chord
            # when Radius is present (R+Δ/arc, or Dist treated as the arc).
            if not curve_can_derive_chord(
                self._num(self._text(row, COL_RADIUS)),
                self._text(row, COL_DELTA),
                self._num(self._text(row, COL_ARC)),
            ):
                other = COL_CHORD if col == COL_DIST else COL_DIST
                self._loading = True
                dst = self.item(row, other)
                if dst is None:
                    dst = QTableWidgetItem("")
                    self.setItem(row, other, dst)
                dst.setText(item.text())
                self._loading = False
        elif call_type == "curve" and col in (COL_RADIUS, COL_DELTA, COL_ARC):
            self._clear_invented_chord_if_radius(row)
        elif col == COL_TYPE and call_type == "line":
            self._loading = True
            dst = self.item(row, COL_CHORD)
            if dst is None:
                dst = QTableWidgetItem("")
                self.setItem(row, COL_CHORD, dst)
            dst.setText("")
            self._loading = False
            self._set_derive_flag(row, False)
        elif col == COL_TYPE:
            self._set_derive_flag(row, self._row_can_derive(row))
        self.callsEdited.emit()

    def set_calls(self, calls: list[Call]):
        self._loading = True
        self.setRowCount(0)
        for i, c in enumerate(calls, start=1):
            row = self.rowCount()
            self.insertRow(row)
            # Curves: Dist is the transcribed length (often the arc). Chord Len
            # is separate; do not put chord in Dist when both exist.
            if c.call_type == "line":
                dist_cell = c.input_distance or (f"{c.distance:g}" if c.distance else "")
                chord_cell = ""
            else:
                if c.input_distance:
                    dist_cell = c.input_distance
                elif c.distance:
                    dist_cell = f"{c.distance:g}"
                else:
                    dist_cell = ""
                chord_cell = c.input_chord_length or (
                    f"{c.chord_length:g}" if c.chord_length else ""
                )
            values = [
                str(i), c.call_type, c.chord_bearing or c.bearing,
                dist_cell,
                c.units,
                c.input_radius or (f"{c.radius:g}" if c.radius else ""),
                c.input_arc_length or (f"{c.arc_length:g}" if c.arc_length else ""),
                chord_cell,
                c.delta, c.curve_direction, c.monument, c.description, c.confidence,
            ]
            for col, val in enumerate(values):
                item = QTableWidgetItem(val)
                if col in (COL_MONUMENT, COL_DESC) and val:
                    item.setToolTip(val)
                if col == COL_NUM:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                color = CONF_COLORS.get(c.confidence.lower())
                if color:
                    item.setBackground(color)
                self.setItem(row, col, item)
        self._refresh_derive_flags()
        self._loading = False

    def _text(self, row: int, col: int) -> str:
        item = self.item(row, col)
        return item.text().strip() if item else ""

    @staticmethod
    def _num(text: str) -> float:
        raw = (text or "").strip().replace(",", "")
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            return 0.0

    @staticmethod
    def _num_error(label: str, text: str) -> str:
        raw = (text or "").strip().replace(",", "")
        if not raw:
            return ""
        try:
            float(raw)
            return ""
        except ValueError:
            return f"{label} is not a number: {text.strip()!r}"

    def get_calls(self) -> list[Call]:
        calls = []
        for row in range(self.rowCount()):
            call_type = self._text(row, COL_TYPE).lower() or "line"
            bearing = self._text(row, COL_BEARING)
            dist_txt = self._text(row, COL_DIST)
            chord_txt = self._text(row, COL_CHORD)
            rad_txt = self._text(row, COL_RADIUS)
            arc_txt = self._text(row, COL_ARC)
            dist_err = self._num_error("Distance", dist_txt)
            rad_err = self._num_error("Radius", rad_txt)
            arc_err = self._num_error("Arc length", arc_txt)
            chord_err = self._num_error("Chord length", chord_txt)
            err = dist_err or rad_err or arc_err or chord_err
            dist = self._num(dist_txt)
            chord = self._num(chord_txt)
            calls.append(
                Call(
                    call_type=call_type,
                    bearing=bearing,
                    distance=dist,
                    units=self._text(row, COL_UNITS) or "feet",
                    radius=self._num(rad_txt),
                    arc_length=self._num(arc_txt),
                    chord_bearing=bearing if call_type == "curve" else "",
                    chord_length=chord if call_type == "curve" else 0.0,
                    delta=self._text(row, COL_DELTA),
                    curve_direction=self._text(row, COL_DIR),
                    monument=self._text(row, COL_MONUMENT),
                    description=self._text(row, COL_DESC),
                    confidence=self._text(row, COL_CONF),
                    input_error=err,
                    input_distance=dist_txt if dist_err else "",
                    input_radius=rad_txt if rad_err else "",
                    input_arc_length=arc_txt if arc_err else "",
                    input_chord_length=chord_txt if chord_err else "",
                )
            )
        return calls

    def add_blank_row(self):
        self._push_undo()
        calls = self.get_calls()
        calls.append(Call(call_type="line", bearing="N 00°00'00\" E", distance=100.0))
        self.set_calls(calls)
        self.callsEdited.emit()

    def delete_selected_rows(self):
        rows = sorted({idx.row() for idx in self.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._push_undo()
        calls = self.get_calls()
        for r in rows:
            if 0 <= r < len(calls):
                calls.pop(r)
        self.set_calls(calls)
        self.callsEdited.emit()

    def replace_call(self, row: int, call: Call):
        self._push_undo()
        calls = self.get_calls()
        if 0 <= row < len(calls):
            calls[row] = call
            self.set_calls(calls)
            self.callsEdited.emit()
