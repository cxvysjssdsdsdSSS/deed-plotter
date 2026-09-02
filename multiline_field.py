"""Compact multiline editor with an optional larger pop-out window."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget,
)

from popout_support import PopOutController


class MultilineField(QWidget):
    """Small multiline box with a Pop out button for a larger editing window."""

    textChanged = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        title: str = "Notes",
        label: str | None = None,
        max_height: int | None = 90,
        min_height: int | None = None,
        placeholder: str = "",
        read_only: bool = False,
        popout_size: tuple[int, int] = (720, 480),
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._read_only = read_only
        self._popout_size = popout_size

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._edit = QPlainTextEdit()
        if placeholder:
            self._edit.setPlaceholderText(placeholder)
        if max_height is not None:
            self._edit.setMaximumHeight(max_height)
        if min_height is not None:
            self._edit.setMinimumHeight(min_height)
        if read_only:
            self._edit.setReadOnly(True)
        self._edit.textChanged.connect(self._on_text_changed)

        if label:
            field_label = QLabel(label)
            field_label.setObjectName("fieldLabel")
            layout.addWidget(field_label)

        layout.addWidget(self._edit)

        self._popout = PopOutController(
            self,
            title=title,
            read_only=read_only,
            size=popout_size,
            has_content=self._has_popout_content,
            create_editor=self._create_popout_editor,
            refresh_editor=self._refresh_popout_editor,
            apply_editor=self._apply_popout_editor,
            editor_state=lambda editor: editor.toPlainText(),
        )
        btn_row = QHBoxLayout()
        self._popout.attach_to_action_row(btn_row)
        layout.addLayout(btn_row)

    @property
    def _popout_btn(self):
        """Compat alias used by tests."""
        return self._popout._button

    def editor(self) -> QPlainTextEdit:
        return self._edit

    def title(self) -> str:
        return self._title

    def popout_size(self) -> tuple[int, int]:
        return self._popout_size

    def is_read_only(self) -> bool:
        return self._read_only

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.strip()

    def _has_popout_content(self) -> bool:
        return bool(self._normalize_text(self._edit.toPlainText()))

    def _create_popout_editor(self, parent: QWidget) -> QPlainTextEdit:
        editor = QPlainTextEdit(parent)
        editor.setReadOnly(self._read_only)
        return editor

    def _refresh_popout_editor(self, editor: QPlainTextEdit) -> None:
        editor.blockSignals(True)
        editor.setPlainText(self._edit.toPlainText())
        editor.blockSignals(False)

    def _apply_popout_editor(self, editor: QPlainTextEdit) -> None:
        if self._read_only:
            return
        self._edit.setPlainText(editor.toPlainText())

    def toPlainText(self) -> str:
        return self._edit.toPlainText()

    def setPlainText(self, text: str) -> None:
        self._edit.setPlainText(text)

    def clear(self) -> None:
        self._edit.clear()

    def setPlaceholderText(self, text: str) -> None:
        self._edit.setPlaceholderText(text)

    def setMaximumHeight(self, height: int) -> None:
        self._edit.setMaximumHeight(height)

    def setMinimumHeight(self, height: int) -> None:
        self._edit.setMinimumHeight(height)

    def setReadOnly(self, read_only: bool) -> None:
        self._read_only = read_only
        self._edit.setReadOnly(read_only)

    def _on_text_changed(self) -> None:
        self.sync_open_popout()
        self.textChanged.emit()

    def sync_open_popout(self) -> None:
        self._popout.on_owner_changed()

    def popout_has_edits(self) -> bool:
        return self._popout.has_unapplied_edits()

    def set_on_popout_dirty_changed(self, callback) -> None:
        self._popout.set_on_dirty_changed(callback)

    def apply_popout_edits(self) -> None:
        self._popout.apply_pending_edits()

    def _open_popout(self) -> None:
        self._popout.open()

    def close_popout(self, *, force: bool = True) -> None:
        self._popout.close(force=force)
