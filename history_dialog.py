"""Parse history dialog: browse past AI parses, reload one, delete entries."""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import history_store

_TABLE_STYLE = """
QTableWidget {
    outline: none;
}
QTableWidget::item:focus {
    outline: none;
    border: none;
}
"""


def _fmt_when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class HistoryDialog(QDialog):
    """selected_result is set (a parse-result dict) when the user loads a run."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Parse History")
        self.resize(720, 480)
        self.selected_result: dict | None = None
        self.selected_source: str = ""
        self.selected_source_path: str = ""
        self.selected_source_paths: list[str] = []
        self.selected_pages: list[int] | None = None

        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["When", "Source", "Model", "Calls"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet(_TABLE_STYLE)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemDoubleClicked.connect(lambda _item: self._load_selected())
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        layout.addWidget(self.table, stretch=1)

        hint = QLabel("Double-click or press Load to restore a run. "
                      "Right-click an entry to delete it.")
        hint.setStyleSheet("color: gray;")
        layout.addWidget(hint)

        btns = QHBoxLayout()
        self.load_btn = QPushButton("Load Run")
        self.load_btn.setToolTip(
            "Load the selected parse into the call table and plot.\n"
            "Asks to confirm only if the workspace already has calls, details, "
            "paste, chat, or pending actions.\n"
            "Double-click a row to load quickly."
        )
        self.load_btn.clicked.connect(self._load_selected)
        self.clear_btn = QPushButton("Clear History…")
        self.clear_btn.setToolTip("Delete all saved parse history (asks for confirmation).")
        self.clear_btn.clicked.connect(self._clear_all)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btns.addWidget(self.load_btn)
        btns.addWidget(self.clear_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self._reload()
        # Don't leave a phantom current-cell highlight on row 0 when the dialog opens.
        close_btn.setFocus(Qt.FocusReason.OtherFocusReason)

    def _reload(self):
        self._entries = history_store.list_entries()
        self.table.setRowCount(0)
        for e in self._entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, val in enumerate((_fmt_when(e.get("created_at", "")),
                                       e.get("source_name", "") or "(pasted text)",
                                       e.get("model", ""),
                                       str(e.get("num_calls", "")))):
                self.table.setItem(row, col, QTableWidgetItem(val))
        self.table.clearSelection()
        self.table.setCurrentIndex(QModelIndex())
        self._sync_buttons()

    def _sync_buttons(self):
        self.load_btn.setEnabled(bool(self.table.selectionModel().selectedRows()))
        self.clear_btn.setEnabled(bool(self._entries))

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _load_selected(self):
        row = self._selected_row()
        if row is None or row >= len(self._entries):
            return
        entry = self._entries[row]
        parent = self.parent()
        has_work = bool(
            parent is not None
            and hasattr(parent, "_has_replaceable_content")
            and parent._has_replaceable_content()
        )
        if has_work:
            if QMessageBox.question(
                self, "Load Run",
                f"Replace the current call table, ties, deed details, legal description, "
                f"notes, paste, and chat with the run from {_fmt_when(entry.get('created_at', ''))}?\n\n"
                f"Unsaved job changes and pending parse or chat work will be discarded.",
            ) != QMessageBox.StandardButton.Yes:
                return
        self.selected_result = history_store.restore_result(entry)
        self.selected_source = entry.get("source_name", "")
        self.selected_source_path = entry.get("source_path", "") or ""
        raw_paths = entry.get("source_paths") or []
        paths: list[str] = []
        if isinstance(raw_paths, list):
            paths = [str(p).strip() for p in raw_paths if str(p).strip()]
        if not paths and self.selected_source_path:
            paths = [self.selected_source_path]
        self.selected_source_paths = paths
        raw_pages = entry.get("selected_pages")
        if isinstance(raw_pages, list):
            try:
                self.selected_pages = sorted({int(i) for i in raw_pages if int(i) >= 0})
            except (TypeError, ValueError):
                self.selected_pages = None
        else:
            self.selected_pages = None
        self.accept()

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self._entries):
            return
        menu = QMenu(self)
        delete_act = menu.addAction("Delete this entry")
        if menu.exec(self.table.viewport().mapToGlobal(pos)) == delete_act:
            if QMessageBox.question(
                self, "Delete history entry",
                "Delete this history entry? This cannot be undone.",
            ) != QMessageBox.StandardButton.Yes:
                return
            try:
                history_store.remove_entry(self._entries[row].get("id", ""))
            except OSError as exc:
                QMessageBox.critical(self, "Save Failed", str(exc))
                return
            self._reload()

    def _clear_all(self):
        if QMessageBox.question(
            self, "Clear History",
            f"Delete all {len(self._entries)} history entries? This cannot be undone.",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            history_store.clear_all()
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            return
        self._reload()
