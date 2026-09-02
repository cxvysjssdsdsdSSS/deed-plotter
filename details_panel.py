"""Deed Details tab: Document, POB & Ties, and Coordinates sub-tabs."""

from __future__ import annotations

import csv

from PyQt6.QtCore import QModelIndex, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from cogo import (
    Call, TraverseResult, curve_can_derive_chord,
    newly_derivable_copied_chord, stated_pob_xy,
)
from closure_panel import is_traverse_closed


def _is_plottable_tie(c: Call) -> bool:
    """True if the tie has a real length (blank Add Tie stubs don't count)."""
    if abs(c.distance or 0.0) > 1e-9:
        return True
    if abs(c.chord_length or 0.0) > 1e-9:
        return True
    if abs(c.radius or 0.0) > 1e-9 or abs(c.arc_length or 0.0) > 1e-9:
        return True
    return False


_INFO_FIELDS = (
    ("document_type", "Type"),
    ("county", "County"),
    ("state", "State"),
    ("date", "Date"),
    ("grantor", "Grantor"),
    ("grantee", "Grantee"),
    ("surveyor", "Surveyor"),
    ("surveyor_license", "License"),
    ("volume_page", "Vol/Page"),
    ("acreage_stated", "Stated acreage"),
    ("basis_of_bearings", "Basis of bearings"),
)

# Full Call fields so curve ties survive replot round-trips.
_TIE_COLUMNS = [
    "#", "Type", "Bearing", "Distance", "Units",
    "Radius", "Arc", "Chord", "Delta", "Dir", "Monument", "Description",
]
_COL_NUM, _COL_TYPE, _COL_BRG, _COL_DIST, _COL_UNITS = 0, 1, 2, 3, 4
_COL_RAD, _COL_ARC, _COL_CHORD, _COL_DELTA, _COL_DIR = 5, 6, 7, 8, 9
_COL_MON, _COL_DESC = 10, 11


def _header_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet("color: #4fc3f7; font-weight: 600; margin-top: 6px;")
    return label


class _UndoTable(QTableWidget):
    """QTableWidget that snapshots undo before an in-grid edit starts."""

    beginEdit = pyqtSignal()

    def edit(self, index: QModelIndex, trigger, event) -> bool:
        if (
            index.isValid()
            and self.state() != QAbstractItemView.State.EditingState
        ):
            item = self.itemFromIndex(index)
            if item is not None and (item.flags() & Qt.ItemFlag.ItemIsEditable):
                self.beginEdit.emit()
        return super().edit(index, trigger, event)


def _make_table(
    columns: list[str],
    *,
    editable: bool = False,
    wide_columns: list[int] | None = None,
    table_cls=QTableWidget,
) -> QTableWidget:
    """Build a details table that scrolls horizontally for long text.

    All columns size to contents (including Monument/Description). Stretching
    the last section or capping wide columns at a fixed width elides text
    *inside* the viewport with no scrollbar — avoid that.
    """
    table = table_cls(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(False)
    # wide_columns kept for call-site clarity; all cols already ResizeToContents.
    _ = wide_columns
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideNone)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
    table.setVerticalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
    table.setStyleSheet(
        "QTableWidget { outline: none; }"
        "QTableWidget::item:focus { outline: none; border: none; }"
        "QTableWidget:!focus::item:selected {"
        "  background-color: transparent; color: palette(text);"
        "}"
    )
    if not editable:
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    return table


def _refit_table_columns(table: QTableWidget) -> None:
    """Expand columns to full cell text so a horizontal scrollbar can appear."""
    table.resizeColumnsToContents()
    # Give long monument/description lines a little padding beyond font metrics.
    header = table.horizontalHeader()
    for col in range(table.columnCount()):
        header.resizeSection(col, header.sectionSize(col) + 12)


def _fmt_num(value: float) -> str:
    return f"{value:g}" if value else ""


def _fmt_pob_coord(value) -> str:
    """Keep survey precision. Default ``:g`` rounds state-plane values."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return ""
    text = f"{n:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


class DetailsPanel(QWidget):
    tiesEdited = pyqtSignal()
    documentInfoEdited = pyqtSignal()
    pobEdited = pyqtSignal()
    undoPushed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.sub_tabs = QTabWidget()
        layout.addWidget(self.sub_tabs)

        # --- Document ---
        doc_page = QWidget()
        doc_lay = QVBoxLayout(doc_page)
        doc_lay.addWidget(_header_label("Document Info (editable)"))
        hint = QLabel("Grantor, county, surveyor, and other deed metadata.")
        hint.setStyleSheet("color: #90a4ae;")
        hint.setWordWrap(True)
        doc_lay.addWidget(hint)
        self.info_table = _make_table(
            ["Field", "Value"], editable=True, wide_columns=[1],
        )
        self.info_table.itemChanged.connect(self._on_info_changed)
        doc_lay.addWidget(self.info_table, stretch=1)
        self.sub_tabs.addTab(doc_page, "Document")

        # --- POB & Ties ---
        loc_page = QWidget()
        loc_lay = QVBoxLayout(loc_page)
        loc_lay.addWidget(_header_label("Point of Beginning (editable)"))
        pob_form = QFormLayout()
        self.pob_text = QLineEdit()
        self.pob_text.setPlaceholderText("POB description from the deed")
        self.pob_text.setToolTip("Text describing the point of beginning.")
        self.pob_text.editingFinished.connect(self._on_pob_changed)
        self.pob_monument = QLineEdit()
        self.pob_monument.setPlaceholderText("e.g. 1/2\" iron rod found")
        self.pob_monument.setToolTip("Physical monument at the POB (shown on the plot).")
        self.pob_monument.editingFinished.connect(self._on_pob_changed)
        self.pob_northing = QLineEdit()
        self.pob_northing.setPlaceholderText("Northing (optional)")
        self.pob_northing.setToolTip(
            "Grid/state-plane northing for the POB.\n"
            "Plot and DXF use this only when Easting is also filled.\n"
            "One field alone stays at the local origin (~5000, 5000)."
        )
        self.pob_northing.editingFinished.connect(self._on_pob_changed)
        self.pob_easting = QLineEdit()
        self.pob_easting.setPlaceholderText("Easting (optional)")
        self.pob_easting.setToolTip(
            "Grid/state-plane easting for the POB.\n"
            "Plot and DXF use this only when Northing is also filled.\n"
            "One field alone stays at the local origin (~5000, 5000)."
        )
        self.pob_easting.editingFinished.connect(self._on_pob_changed)
        pob_form.addRow("POB text", self.pob_text)
        pob_form.addRow("POB monument", self.pob_monument)
        pob_form.addRow("Northing", self.pob_northing)
        pob_form.addRow("Easting", self.pob_easting)
        loc_lay.addLayout(pob_form)

        loc_lay.addWidget(_header_label("Tie Calls (locate the tract; not part of the boundary)"))
        tie_btns = QHBoxLayout()
        add_tie = QPushButton("Add Tie")
        add_tie.setToolTip(
            "Append a due-north 0-ft commencement/tie stub (Distance left empty)."
        )
        add_tie.clicked.connect(self._add_tie)
        self.add_tie_btn = add_tie
        self.edit_tie_btn = QPushButton("Edit Tie…")
        self.edit_tie_btn.setToolTip("Open the full call editor for the selected tie (curves, etc.).")
        self.edit_tie_btn.clicked.connect(self.edit_selected_tie)
        self.del_tie_btn = QPushButton("Delete Selected Tie")
        self.del_tie_btn.setToolTip("Remove the selected tie call row(s).")
        self.del_tie_btn.clicked.connect(self.delete_selected_ties)
        tie_btns.addWidget(add_tie)
        tie_btns.addWidget(self.edit_tie_btn)
        tie_btns.addWidget(self.del_tie_btn)
        tie_btns.addStretch()
        loc_lay.addLayout(tie_btns)
        self.tie_table = _make_table(
            _TIE_COLUMNS, editable=True,
            wide_columns=[_COL_MON, _COL_DESC],
            table_cls=_UndoTable,
        )
        self.tie_table.beginEdit.connect(self._on_tie_begin_edit)
        self.tie_table.itemChanged.connect(self._on_ties_changed)
        self.tie_table.cellDoubleClicked.connect(self._maybe_edit_tie)
        self.tie_table.itemSelectionChanged.connect(self._sync_tie_buttons)
        loc_lay.addWidget(self.tie_table, stretch=1)
        self.sub_tabs.addTab(loc_page, "POB && Ties")

        # --- Coordinates ---
        coord_page = QWidget()
        coord_lay = QVBoxLayout(coord_page)
        coord_row = QHBoxLayout()
        self.coord_header = _header_label("Corner Coordinates")
        coord_row.addWidget(self.coord_header)
        coord_row.addStretch()
        self.export_btn = QPushButton("Export Coordinates CSV…")
        self.export_btn.setEnabled(False)
        self.export_btn.setToolTip(
            "Export corner coordinates to CSV.\n"
            "Available when both POB Northing and Easting are filled."
        )
        self.export_btn.clicked.connect(self._export_coords)
        coord_row.addWidget(self.export_btn)
        coord_lay.addLayout(coord_row)
        self.coord_table = _make_table(
            ["Point", "Northing (ft)", "Easting (ft)", "Monument"],
            wide_columns=[3],
        )
        coord_lay.addWidget(self.coord_table, stretch=1)
        self.sub_tabs.addTab(coord_page, "Coordinates")

        self._coords: list[tuple[str, float, float, str]] = []
        self._loading = False
        self._info_keys: list[str] = []
        self._tie_undo: list[list[Call]] = []
        self._tie_redo: list[list[Call]] = []
        self._tie_derive_by_row: list[bool] = []
        self._interactive = True
        self._export_stem = ""
        self.set_parse_info({}, [])
        self.set_pob_info("", "", None)
        self._sync_tie_buttons()

    def set_interactive(self, on: bool) -> None:
        """Lock document/POB/tie edits while parse/chat/load is running."""
        self._interactive = on
        self.add_tie_btn.setEnabled(on)
        for w in (
            self.pob_text, self.pob_monument, self.pob_northing, self.pob_easting,
        ):
            w.setEnabled(on)
        triggers = (
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
            if on else QTableWidget.EditTrigger.NoEditTriggers
        )
        self.info_table.setEditTriggers(triggers)
        self.tie_table.setEditTriggers(triggers)
        self._sync_tie_buttons()

    def set_export_stem(self, stem: str):
        """Used for suggested coordinates CSV filenames."""
        self._export_stem = (stem or "").strip()

    # ---------- POB ----------

    def set_pob_info(
        self,
        point_of_beginning: str = "",
        pob_monument: str = "",
        pob_coordinates: dict | None = None,
    ):
        self._loading = True
        self.pob_text.setText(point_of_beginning or "")
        self.pob_monument.setText(pob_monument or "")
        if pob_coordinates:
            n = pob_coordinates.get("northing")
            e = pob_coordinates.get("easting")
            self.pob_northing.setText("" if n is None else _fmt_pob_coord(n))
            self.pob_easting.setText("" if e is None else _fmt_pob_coord(e))
        else:
            self.pob_northing.clear()
            self.pob_easting.clear()
        self._loading = False

    def get_pob_info(self) -> tuple[str, str, dict | None]:
        text = self.pob_text.text().strip()
        monument = self.pob_monument.text().strip()
        n_raw = self.pob_northing.text().strip().replace(",", "")
        e_raw = self.pob_easting.text().strip().replace(",", "")
        coords: dict = {}
        if n_raw:
            try:
                coords["northing"] = float(n_raw)
            except ValueError:
                pass
        if e_raw:
            try:
                coords["easting"] = float(e_raw)
            except ValueError:
                pass
        return text, monument, coords or None

    def _on_pob_changed(self):
        if not self._loading:
            self.pobEdited.emit()

    # ---------- population ----------

    def set_parse_info(
        self,
        document_info: dict,
        tie_calls: list[Call],
        *,
        clear_tie_undo: bool = True,
    ):
        """Populate document info + ties.

        clear_tie_undo=True (default) for fresh parses / history loads.
        Pass False when only refreshing document info so chat/doc edits do not
        wipe a just-pushed tie undo snapshot.
        """
        self._loading = True
        self.info_table.setRowCount(0)
        self._info_keys = []
        for key, label in _INFO_FIELDS:
            value = (document_info.get(key) or "").strip()
            row = self.info_table.rowCount()
            self.info_table.insertRow(row)
            key_item = QTableWidgetItem(label)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.info_table.setItem(row, 0, key_item)
            item = QTableWidgetItem(value)
            item.setToolTip(value)
            self.info_table.setItem(row, 1, item)
            self._info_keys.append(key)

        self.tie_table.setRowCount(0)
        for i, c in enumerate(tie_calls, start=1):
            self._insert_tie_row(i, c)
        _refit_table_columns(self.info_table)
        _refit_table_columns(self.tie_table)
        if clear_tie_undo:
            self._tie_undo.clear()
            self._tie_redo.clear()
        self._refresh_tie_derive_flags()
        self._loading = False

    def _insert_tie_row(self, num: int, c: Call):
        row = self.tie_table.rowCount()
        self.tie_table.insertRow(row)
        values = [
            str(num),
            c.call_type,
            c.chord_bearing or c.bearing,
            c.input_distance or (_fmt_num(c.distance) if c.distance else ""),
            c.units or "feet",
            c.input_radius or _fmt_num(c.radius),
            c.input_arc_length or _fmt_num(c.arc_length),
            "" if c.call_type == "line" else (
                c.input_chord_length or _fmt_num(c.chord_length)
            ),
            c.delta or "",
            c.curve_direction or "",
            c.monument or "",
            c.description or "",
        ]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == _COL_NUM:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if col in (_COL_MON, _COL_DESC) and val:
                item.setToolTip(val)
            self.tie_table.setItem(row, col, item)

    def get_document_info(self) -> dict:
        info = {}
        for row, key in enumerate(self._info_keys):
            item = self.info_table.item(row, 1)
            value = item.text().strip() if item else ""
            if value:
                info[key] = value
        return info

    def get_tie_calls(self) -> list[Call]:
        """Rebuild ties from the table — all curve fields included."""
        calls = []
        for row in range(self.tie_table.rowCount()):
            def text(col: int) -> str:
                item = self.tie_table.item(row, col)
                return item.text().strip() if item else ""

            def num(col: int) -> float:
                raw = text(col).replace(",", "")
                if not raw:
                    return 0.0
                try:
                    return float(raw)
                except ValueError:
                    return 0.0

            def num_err(label: str, col: int) -> str:
                raw = text(col).strip()
                if not raw:
                    return ""
                try:
                    float(raw.replace(",", ""))
                    return ""
                except ValueError:
                    return f"{label} is not a number: {raw!r}"

            call_type = (text(_COL_TYPE) or "line").lower()
            if call_type not in ("line", "curve"):
                call_type = "line"
            bearing = text(_COL_BRG)
            dist_txt = text(_COL_DIST)
            rad_txt = text(_COL_RAD)
            arc_txt = text(_COL_ARC)
            chord_txt = text(_COL_CHORD)
            dist_err = num_err("Distance", _COL_DIST)
            rad_err = num_err("Radius", _COL_RAD)
            arc_err = num_err("Arc length", _COL_ARC)
            chord_err = num_err("Chord length", _COL_CHORD)
            distance = num(_COL_DIST)
            radius = num(_COL_RAD)
            arc_length = num(_COL_ARC)
            chord_length = num(_COL_CHORD)
            err = dist_err or rad_err or arc_err or chord_err
            calls.append(Call(
                call_type=call_type,
                bearing=bearing,
                distance=distance,
                units=text(_COL_UNITS) or "feet",
                curve_direction=text(_COL_DIR),
                radius=radius,
                arc_length=arc_length,
                chord_bearing=bearing if call_type == "curve" else "",
                chord_length=chord_length if call_type == "curve" else 0.0,
                delta=text(_COL_DELTA),
                monument=text(_COL_MON),
                description=text(_COL_DESC),
                input_error=err,
                input_distance=dist_txt if dist_err else "",
                input_radius=rad_txt if rad_err else "",
                input_arc_length=arc_txt if arc_err else "",
                input_chord_length=chord_txt if chord_err else "",
            ))
        return calls

    def has_plottable_ties(self) -> bool:
        """True when at least one tie has a real length (not a blank Add Tie stub)."""
        return any(_is_plottable_tie(c) for c in self.get_tie_calls())

    def set_tie_calls(self, ties: list[Call]):
        self._loading = True
        self.tie_table.setRowCount(0)
        for i, c in enumerate(ties, start=1):
            self._insert_tie_row(i, c)
        _refit_table_columns(self.tie_table)
        self._refresh_tie_derive_flags()
        self._loading = False

    def push_tie_undo(self, *, emit: bool = True):
        """Snapshot current ties before an external batch edit (e.g. chat apply)."""
        self._tie_undo.append(self.get_tie_calls())
        self._tie_redo.clear()
        if len(self._tie_undo) > 40:
            self._tie_undo = self._tie_undo[-40:]
        if emit:
            self.undoPushed.emit()

    def _push_tie_undo(self):
        self.push_tie_undo()

    def _on_tie_begin_edit(self):
        if not self._loading:
            self._push_tie_undo()

    def clear_tie_history(self) -> None:
        self._tie_undo.clear()
        self._tie_redo.clear()

    def clear_tie_redo(self) -> None:
        self._tie_redo.clear()

    def can_undo_ties(self) -> bool:
        return bool(self._tie_undo)

    def can_redo_ties(self) -> bool:
        return bool(self._tie_redo)

    def undo_ties(self) -> bool:
        if not self._tie_undo:
            return False
        self._tie_redo.append(self.get_tie_calls())
        prior = self._tie_undo.pop()
        self.set_tie_calls(prior)
        self.tiesEdited.emit()
        return True

    def redo_ties(self) -> bool:
        if not self._tie_redo:
            return False
        self._tie_undo.append(self.get_tie_calls())
        nxt = self._tie_redo.pop()
        self.set_tie_calls(nxt)
        self.tiesEdited.emit()
        return True

    def _on_info_changed(self, _item):
        if not self._loading:
            self.documentInfoEdited.emit()
            _refit_table_columns(self.info_table)

    def _tie_row_can_derive(self, row: int) -> bool:
        def _t(c: int) -> str:
            it = self.tie_table.item(row, c)
            return it.text().strip() if it else ""

        def _n(c: int) -> float:
            raw = _t(c).replace(",", "")
            if not raw:
                return 0.0
            try:
                return float(raw)
            except ValueError:
                return 0.0

        return curve_can_derive_chord(_n(_COL_RAD), _t(_COL_DELTA), _n(_COL_ARC))

    def _refresh_tie_derive_flags(self) -> None:
        self._tie_derive_by_row = [
            self._tie_row_can_derive(r) for r in range(self.tie_table.rowCount())
        ]

    def _set_tie_derive_flag(self, row: int, value: bool) -> None:
        if 0 <= row < len(self._tie_derive_by_row):
            self._tie_derive_by_row[row] = value
        elif row == len(self._tie_derive_by_row):
            self._tie_derive_by_row.append(value)

    def _on_ties_changed(self, item):
        if self._loading:
            return
        row, col = item.row(), item.column()
        type_item = self.tie_table.item(row, _COL_TYPE)
        call_type = (type_item.text() if type_item else "").strip().lower()
        if call_type == "curve" and col in (
            _COL_DIST, _COL_CHORD, _COL_RAD, _COL_DELTA, _COL_ARC,
        ):
            def _t(c: int) -> str:
                it = self.tie_table.item(row, c)
                return it.text().strip() if it else ""

            def _n(c: int) -> float:
                raw = _t(c).replace(",", "")
                if not raw:
                    return 0.0
                try:
                    return float(raw)
                except ValueError:
                    return 0.0

            if col in (_COL_DIST, _COL_CHORD) and not curve_can_derive_chord(
                _n(_COL_RAD), _t(_COL_DELTA), _n(_COL_ARC),
            ):
                other = _COL_CHORD if col == _COL_DIST else _COL_DIST
                self._loading = True
                dst = self.tie_table.item(row, other)
                if dst is None:
                    dst = QTableWidgetItem("")
                    self.tie_table.setItem(row, other, dst)
                dst.setText(item.text())
                self._loading = False
            elif col in (_COL_RAD, _COL_DELTA, _COL_ARC):
                derive_now = curve_can_derive_chord(
                    _n(_COL_RAD), _t(_COL_DELTA), _n(_COL_ARC),
                )
                derive_before = (
                    self._tie_derive_by_row[row]
                    if 0 <= row < len(self._tie_derive_by_row)
                    else False
                )
                if newly_derivable_copied_chord(
                    derive_now, derive_before, _n(_COL_DIST), _n(_COL_CHORD),
                ):
                    self._loading = True
                    dst = self.tie_table.item(row, _COL_CHORD)
                    if dst is None:
                        dst = QTableWidgetItem("")
                        self.tie_table.setItem(row, _COL_CHORD, dst)
                    dst.setText("")
                    self._loading = False
                self._set_tie_derive_flag(row, derive_now)
        elif col == _COL_TYPE and call_type == "line":
            self._loading = True
            dst = self.tie_table.item(row, _COL_CHORD)
            if dst is None:
                dst = QTableWidgetItem("")
                self.tie_table.setItem(row, _COL_CHORD, dst)
            dst.setText("")
            self._loading = False
            self._set_tie_derive_flag(row, False)
        elif col == _COL_TYPE:
            self._set_tie_derive_flag(row, self._tie_row_can_derive(row))
        self._loading = True
        for r in range(self.tie_table.rowCount()):
            num_item = self.tie_table.item(r, 0)
            if num_item:
                num_item.setText(str(r + 1))
        self._loading = False
        self.tiesEdited.emit()
        _refit_table_columns(self.tie_table)

    def _add_tie(self):
        if not self._interactive:
            return
        self._push_tie_undo()
        self._loading = True
        self._insert_tie_row(
            self.tie_table.rowCount() + 1,
            Call(call_type="line", bearing="N 00°00'00\" E", distance=0.0),
        )
        self._refresh_tie_derive_flags()
        self._loading = False
        self.tiesEdited.emit()
        _refit_table_columns(self.tie_table)

    def _sync_tie_buttons(self):
        has_sel = bool(self.tie_table.selectedIndexes()) and self._interactive
        self.edit_tie_btn.setEnabled(has_sel)
        self.del_tie_btn.setEnabled(has_sel)

    def delete_selected_ties(self):
        if not self._interactive:
            return
        rows = sorted({idx.row() for idx in self.tie_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._push_tie_undo()
        self._loading = True
        for r in rows:
            self.tie_table.removeRow(r)
        for row in range(self.tie_table.rowCount()):
            item = self.tie_table.item(row, 0)
            if item:
                item.setText(str(row + 1))
        self._refresh_tie_derive_flags()
        self._loading = False
        self.tiesEdited.emit()
        _refit_table_columns(self.tie_table)

    def _maybe_edit_tie(self, row: int, col: int):
        if not self._interactive:
            return
        if col == _COL_NUM:
            self._edit_tie(row)

    def edit_selected_tie(self):
        if not self._interactive:
            return
        rows = {idx.row() for idx in self.tie_table.selectedIndexes()}
        if not rows:
            return
        self._edit_tie(min(rows))

    def _edit_tie(self, row: int):
        from call_editor_dialog import CallEditorDialog
        ties = self.get_tie_calls()
        if not (0 <= row < len(ties)):
            return
        dlg = CallEditorDialog(ties[row], self)
        if dlg.exec():
            self._push_tie_undo()
            ties[row] = dlg.edited_call()
            self.set_tie_calls(ties)
            self.tiesEdited.emit()

    def update_coordinates(self, calls: list[Call], result: TraverseResult | None,
                           pob_coordinates: dict | None = None):
        """List the traverse corners. Real northing/easting values are shown
        only when the deed explicitly stated POB coordinates; otherwise the
        coordinate columns show a dash (we don't invent a local system)."""
        self.coord_table.setRowCount(0)
        self._coords = []
        if result is None or not result.segment_endpoints:
            self.coord_header.setText("Corner Coordinates")
            self.export_btn.setEnabled(False)
            return
        stated = stated_pob_xy(pob_coordinates)
        have_real = stated is not None
        if have_real:
            self.coord_header.setText("Corner Coordinates (from POB Northing and Easting)")
            origin = result.segment_endpoints[0]
            shift_e = stated[0] - origin[0]
            shift_n = stated[1] - origin[1]
        else:
            self.coord_header.setText(
                "Corner Coordinates (fill both POB axes for grid values)"
            )

        corners = result.segment_endpoints
        segments = result.segments
        for i, pt in enumerate(corners):
            if i == 0:
                name = "POB"
                # Prefer explicit POB monument from the form when present.
                monument = self.pob_monument.text().strip()
            else:
                seg = segments[i - 1] if i - 1 < len(segments) else None
                monument = seg.call.monument if seg else ""
                seq = seg.sequence if seg else i
                if i == len(corners) - 1 and is_traverse_closed(result):
                    name = f"{seq} (back to POB)"
                else:
                    name = str(seq)
            if have_real:
                n, e = pt[1] + shift_n, pt[0] + shift_e
                n_text, e_text = f"{n:,.3f}", f"{e:,.3f}"
                self._coords.append((name, n, e, monument))
            else:
                n_text = e_text = "\u2014"
            row = self.coord_table.rowCount()
            self.coord_table.insertRow(row)
            for col, val in enumerate((name, n_text, e_text, monument)):
                item = QTableWidgetItem(val)
                if col in (1, 2):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 3 and val:
                    item.setToolTip(val)
                self.coord_table.setItem(row, col, item)
        _refit_table_columns(self.coord_table)
        self.export_btn.setEnabled(have_real)

    def _export_coords(self):
        if not self._coords:
            return
        stem = "".join(
            ch if ch not in '<>:"/\\|?*' else "_" for ch in self._export_stem
        )
        default = f"{stem}_coordinates.csv" if stem else "coordinates.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Coordinates", default, "CSV files (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["point", "northing_ft", "easting_ft", "monument"])
                for name, n, e, monument in self._coords:
                    writer.writerow([name, f"{n:.3f}", f"{e:.3f}", monument])
        except OSError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
