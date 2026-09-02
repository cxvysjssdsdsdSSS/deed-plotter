"""Review table for chat-proposed deed actions before apply."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from chat_propose import action_summary
from chat_propose_closure import ProposalClosurePreview, preview_proposal_closure
from chat_propose_preview import open_proposal_editor
from cogo import Call
from closure_panel import load_closure_tolerance

_ICONS = Path(__file__).resolve().parent / "icons"

_COL_INCLUDE = 0
_COL_EDIT = 1
_COL_DELETE = 2
_COL_ACTION = 3

# Compact Health list-import *size*; colors stay on the dark theme (not Health day white).
_CELL_BTN_H = 22
_CELL_BTN_STYLE = (
    "QPushButton {"
    "  background: #2a3038;"
    "  color: #c5ccd4;"
    "  border: 1px solid #4a5560;"
    "  border-radius: 6px;"
    "  padding: 1px 6px;"
    "  margin: 0px;"
    "  font-size: 12px;"
    "}"
    "QPushButton:hover {"
    "  background: #333940;"
    "  border-color: #6a7684;"
    "  color: #e8eaed;"
    "}"
    "QPushButton:pressed {"
    "  background: #1e2228;"
    "  border-color: #3a424a;"
    "}"
)


def _qss_url(name: str) -> str:
    """Absolute path for QSS url(), forward slashes (required on Windows)."""
    return (_ICONS / name).as_posix()


# Health-style indicators: visible empty box on dark rows (Fusion alone vanishes).
_CHECKBOX_QSS = f"""
QCheckBox {{
    color: #e0e0e0;
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid #8b939e;
    border-radius: 3px;
    background: #2a3038;
}}
QCheckBox::indicator:hover {{
    border-color: #64b5f6;
}}
QCheckBox::indicator:checked {{
    background: #4296fa;
    border-color: #4296fa;
    image: url("{_qss_url("checkbox_check.png")}");
}}
QCheckBox::indicator:checked:hover {{
    background: #5aa6ff;
    border-color: #5aa6ff;
}}
QCheckBox::indicator:disabled {{
    background: #1e2228;
    border-color: #4a5560;
}}
QCheckBox::indicator:checked:disabled {{
    background: #2a455f;
    border-color: #2a455f;
    image: url("{_qss_url("checkbox_check.png")}");
}}
"""


class ChatProposeDialog(QDialog):
    """Include/exclude proposed actions; Apply returns selected list.

    *discarded* is True when the user chooses Discard all (clear pending).
    Later / Esc leaves pending unchanged (caller already saved it).

    Edit opens the real call / document editor (Health-style). Accept walks
    forward to the next proposal; parse/export rows are view-only. Workspace
    changes wait for Apply selected.
    """

    def __init__(
        self,
        actions: list[dict],
        skipped: list[str] | None = None,
        parent=None,
        *,
        window_title: str = "Review proposed actions",
        calls: list[Call] | None = None,
        tie_calls: list[Call] | None = None,
        document_info: dict | None = None,
        on_actions_changed=None,
        tolerance_ft: float | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(window_title)
        self.resize(720, 460)
        self._actions = [dict(a) for a in actions]
        self._skipped = list(skipped or [])
        self._calls = list(calls or [])
        self._ties = list(tie_calls or [])
        self._document_info = dict(document_info or {})
        self._on_actions_changed = on_actions_changed
        self._tolerance_ft = (
            float(tolerance_ft)
            if tolerance_ft is not None
            else load_closure_tolerance()
        )
        self.applied: list[dict] = []
        self.discarded = False

        layout = QVBoxLayout(self)
        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Include", "Edit", "Delete", "Action"]
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(_COL_INCLUDE, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(_COL_EDIT, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(_COL_DELETE, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(_COL_ACTION, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(_COL_INCLUDE, 70)
        self.table.setColumnWidth(_COL_EDIT, 88)
        self.table.setColumnWidth(_COL_DELETE, 96)
        self.table.verticalHeader().setVisible(False)
        vhdr = self.table.verticalHeader()
        vhdr.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vhdr.setDefaultSectionSize(34)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Health list-import pattern: no row/cell selection chrome — Include /
        # Edit / Delete are the only interactions (avoids stuck Fusion blue).
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setStyleSheet(
            "QTableWidget { outline: none; }"
            "QTableWidget::item:selected { background: transparent; }"
            "QTableWidget::item:focus { outline: none; border: none; }"
        )
        layout.addWidget(self.table, stretch=1)

        self._skipped_lbl = QLabel("")
        self._skipped_lbl.setWordWrap(True)
        self._skipped_lbl.setStyleSheet("color: #ffb74d;")
        self._skipped_lbl.hide()
        layout.addWidget(self._skipped_lbl)

        self._closure_lbl = QLabel("")
        self._closure_lbl.setWordWrap(True)
        self._closure_lbl.setStyleSheet("color: #90caf9;")
        layout.addWidget(self._closure_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Apply selected")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Later")
        discard_btn = QPushButton("Discard all")
        discard_btn.setToolTip("Throw away this proposal list (nothing applied).")
        discard_btn.clicked.connect(self._discard)
        buttons.addButton(discard_btn, QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._fill_table()

    @staticmethod
    def _make_cell_button(label: str) -> QPushButton:
        """Compact Health-sized Edit/Delete control (does not fill the row)."""
        btn = QPushButton(label)
        btn.setObjectName("listImportActionBtn")
        btn.setStyleSheet(_CELL_BTN_STYLE)
        btn.setFixedSize(72, _CELL_BTN_H)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setAutoDefault(False)
        btn.setDefault(False)
        return btn

    @staticmethod
    def _center_in_cell(widget: QWidget) -> QWidget:
        """Keep the control intrinsic size — Fusion stretches bare cell widgets."""
        host = QWidget()
        host.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(host)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(widget, 0, Qt.AlignmentFlag.AlignCenter)
        return host

    def _fill_table(self, *, checked_indices: set[int] | None = None) -> None:
        self.table.setRowCount(0)
        for i, act in enumerate(self._actions):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 34)

            chk = QCheckBox()
            chk.setStyleSheet(_CHECKBOX_QSS)
            chk.blockSignals(True)
            if checked_indices is None:
                chk.setChecked(bool(act.get("include", True)))
            else:
                chk.setChecked(i in checked_indices)
            chk.blockSignals(False)
            chk.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            chk.stateChanged.connect(lambda *_a: self._notify_actions_changed())
            host = QWidget()
            host.setStyleSheet("background: transparent;")
            hl = QHBoxLayout(host)
            hl.setContentsMargins(18, 0, 0, 0)
            hl.addWidget(chk)
            hl.addStretch()
            self.table.setCellWidget(row, _COL_INCLUDE, host)
            act["_chk"] = chk

            edit_btn = self._make_cell_button("Edit")
            kind = str(act.get("action") or "")
            if kind in ("run_parse", "export_csv", "export_dxf"):
                edit_btn.setToolTip(
                    "View this proposal. Close does not change it or advance "
                    "to the next row. Workspace changes wait for Apply selected."
                )
            else:
                edit_btn.setToolTip(
                    "Open the real editor for this proposal. Accept walks "
                    "forward to the next proposal. Workspace changes wait "
                    "for Apply selected."
                )
            edit_btn.clicked.connect(
                lambda _checked=False, r=row: self._edit_row(r)
            )
            self.table.setCellWidget(row, _COL_EDIT, self._center_in_cell(edit_btn))

            del_btn = self._make_cell_button("Delete")
            del_btn.setToolTip("Remove this proposal from the list.")
            del_btn.clicked.connect(
                lambda _checked=False, r=row: self._delete_row(r)
            )
            self.table.setCellWidget(row, _COL_DELETE, self._center_in_cell(del_btn))

            item = QTableWidgetItem(action_summary(act))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, _COL_ACTION, item)

        n = len(self._actions)
        self._hint.setText(
            f"{n} proposed action(s). Use Edit to open the real call / document "
            "form (Accept walks to the next proposal). Parse/export rows are "
            "view-only (Close). Delete removes a proposal. "
            "Uncheck any you do not want, then Apply selected. Later keeps them "
            "under Pending actions…"
        )
        if self._skipped:
            self._skipped_lbl.setText(
                "Skipped by assistant:\n• "
                + "\n• ".join(str(s) for s in self._skipped[:12])
            )
            self._skipped_lbl.show()
        else:
            self._skipped_lbl.hide()
        self._refresh_closure_preview()

    def _closure_preview(self) -> ProposalClosurePreview:
        return preview_proposal_closure(
            self.snapshot_actions(),
            calls=self._calls,
            document_info=self._document_info,
            tolerance_ft=self._tolerance_ft,
        )

    def _refresh_closure_preview(self) -> None:
        preview = self._closure_preview()
        self._closure_lbl.setText(preview.summary)
        if preview.status == "CLOSED":
            self._closure_lbl.setStyleSheet("color: #81c784;")
        elif preview.status == "OPEN" and not preview.expected_open:
            self._closure_lbl.setStyleSheet("color: #ef9a9a;")
        elif preview.status == "ERROR":
            self._closure_lbl.setStyleSheet("color: #ffb74d;")
        else:
            self._closure_lbl.setStyleSheet("color: #90caf9;")

    def snapshot_actions(self) -> list[dict]:
        """Current proposal list (cleaned), including Include checkbox state."""
        out: list[dict] = []
        for act in self._actions:
            clean = self._clean(act)
            chk = act.get("_chk")
            if chk is not None:
                clean["include"] = bool(chk.isChecked())
            else:
                clean["include"] = bool(clean.get("include", True))
            out.append(clean)
        return out

    def _notify_actions_changed(self) -> None:
        self._refresh_closure_preview()
        if self._on_actions_changed is not None:
            self._on_actions_changed()

    def _clean(self, act: dict) -> dict:
        return {k: v for k, v in act.items() if k != "_chk"}

    def _checked_indices(self) -> set[int]:
        out: set[int] = set()
        for i, act in enumerate(self._actions):
            chk = act.get("_chk")
            if chk is not None and chk.isChecked():
                out.add(i)
        return out

    def _edit_row(self, row: int) -> None:
        if row < 0 or row >= len(self._actions):
            return
        total = len(self._actions)
        checked = self._checked_indices()
        # Edit from this row forward (Health parity: Accept advances).
        changed = False
        while 0 <= row < total:
            action = self._clean(self._actions[row])
            updated = open_proposal_editor(
                action,
                self,
                calls=self._calls,
                tie_calls=self._ties,
                document_info=self._document_info,
                title_suffix=f"proposal {row + 1} of {total}",
            )
            if updated is None:
                break
            self._actions[row] = updated
            changed = True
            self._fill_table(checked_indices=checked)
            row += 1
            total = len(self._actions)
        if changed:
            self._notify_actions_changed()

    def _delete_row(self, row: int) -> None:
        if row < 0 or row >= len(self._actions):
            return
        summary = action_summary(self._actions[row])
        if QMessageBox.question(
            self,
            "Delete proposal",
            f"Remove this proposed action from the list?\n\n{summary}",
        ) != QMessageBox.StandardButton.Yes:
            return
        checked = self._checked_indices()
        checked = {i if i < row else i - 1 for i in checked if i != row}
        del self._actions[row]
        if not self._actions:
            self.table.setRowCount(0)
            self._hint.setText(
                "No proposed actions left. Discard all, or Later / Esc to close."
            )
            self._refresh_closure_preview()
            self._notify_actions_changed()
            return
        self._fill_table(checked_indices=checked)
        self._notify_actions_changed()

    def remaining_actions(self) -> list[dict]:
        """Actions left unchecked after Apply (still pending)."""
        out = []
        for act in self._actions:
            chk = act.get("_chk")
            if chk is None or not chk.isChecked():
                clean = self._clean(act)
                clean["include"] = True
                out.append(clean)
        return out

    def _apply(self):
        self.discarded = False
        self.applied = []
        for act in self._actions:
            chk = act.get("_chk")
            if chk is not None and chk.isChecked():
                clean = self._clean(act)
                clean["include"] = True
                self.applied.append(clean)
        if not self.applied:
            QMessageBox.information(
                self,
                "Nothing selected",
                "Check at least one action to Apply, or use Later to keep them pending.",
            )
            return
        preview = preview_proposal_closure(
            self.applied,
            calls=self._calls,
            document_info=self._document_info,
            tolerance_ft=self._tolerance_ft,
        )
        if (
            preview.has_boundary_edits
            and preview.status == "OPEN"
            and not preview.expected_open
        ):
            if QMessageBox.warning(
                self,
                "Traverse would stay OPEN",
                preview.summary
                + "\n\nApply these actions anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                self.applied = []
                return
        self.accept()

    def _discard(self):
        n = len(self._actions)
        if n == 0:
            self.discarded = True
            self.applied = []
            self.accept()
            return
        word = "proposal" if n == 1 else "proposals"
        if QMessageBox.question(
            self,
            "Discard all proposals?",
            f"Discard all {n} {word} without applying?\n\n"
            "They will be removed from pending actions. This cannot be undone.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.discarded = True
        self.applied = []
        self.accept()
