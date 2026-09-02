"""Shared pop-out button and expanded editor dialog."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)
from PyQt6.sip import isdeleted


def _alive(obj) -> bool:
    """True when the underlying C++ object still exists."""
    return obj is not None and not isdeleted(obj)


def _clear_focus_if_descendant(*widgets: QWidget) -> None:
    """Clear focus when it sits on *widgets* or a child about to be hidden."""
    focus = QApplication.focusWidget()
    if focus is None:
        return
    for widget in widgets:
        if widget is None:
            continue
        if focus is widget or widget.isAncestorOf(focus):
            focus.clearFocus()
            return


class PopOutController:
    """Pop-out button + modeless expanded editor for a host widget."""

    BUTTON_WIDTH = 76

    def __init__(
        self,
        owner: QWidget,
        *,
        title: str,
        read_only: bool = False,
        size: tuple[int, int] = (800, 520),
        has_content: Callable[[], bool],
        create_editor: Callable[[QWidget], QWidget],
        refresh_editor: Callable[[QWidget], None],
        apply_editor: Callable[[QWidget], None] | None = None,
        editor_state: Callable[[QWidget], object] | None = None,
        on_dirty_changed: Callable[[], None] | None = None,
    ) -> None:
        self._owner = owner
        self._title = title
        self._read_only = read_only
        self._size = size
        self._has_content = has_content
        self._create_editor = create_editor
        self._refresh_editor = refresh_editor
        self._apply_editor = apply_editor
        self._editor_state = editor_state
        self._on_dirty_changed = on_dirty_changed
        self._baseline: object = None
        self.context_provider: Callable[[], str] | None = None
        self._dialog: _PopOutDialog | None = None
        self._button = QPushButton("Pop out")
        self._button.setFixedWidth(self.BUTTON_WIDTH)
        self._button.clicked.connect(self.open)
        self._update_button()

    def attach_footer(self, layout: QVBoxLayout) -> None:
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._button)
        layout.addLayout(row)

    def attach_to_action_row(self, row: QHBoxLayout) -> None:
        row.addStretch(1)
        row.addWidget(self._button)

    def set_on_dirty_changed(
            self, callback: Callable[[], None] | None) -> None:
        self._on_dirty_changed = callback

    def _sync_editor(self, editor: QWidget) -> None:
        """Push owner content into the editor and re-baseline it as clean."""
        self._refresh_editor(editor)
        if self._editor_state is not None:
            self._baseline = self._editor_state(editor)

    def on_owner_changed(self) -> None:
        if not _alive(self._owner):
            self._dialog = None
            return
        self._update_button()
        if not self._has_content() and self.has_unapplied_edits():
            self.apply_pending_edits()
        if self._dialog is not None and self._dialog.isVisible():
            if not _alive(self._dialog.editor):
                self.close(force=True)
            elif not self._popout_editor_dirty():
                # Only mirror owner changes into a pristine pop-out; a dirty
                # pop-out keeps the user's unapplied edits.
                self._sync_editor(self._dialog.editor)

    def _popout_editor_dirty(self, editor: QWidget | None = None) -> bool:
        """True when the user edited the pop-out since the last owner sync."""
        if self._read_only or self._editor_state is None:
            return False
        if editor is None:
            if self._dialog is None or not _alive(self._dialog):
                return False
            editor = self._dialog.editor
        return self._editor_state(editor) != self._baseline

    def has_unapplied_edits(self) -> bool:
        return (self._dialog is not None and self._dialog.isVisible()
                and _alive(self._dialog.editor)
                and self._popout_editor_dirty())

    def apply_pending_edits(self) -> None:
        """Push unapplied pop-out edits into the owner widget."""
        if self.has_unapplied_edits() and self._apply_editor is not None:
            editor = self._dialog.editor
            if self._editor_state is not None:
                self._baseline = self._editor_state(editor)
            self._apply_editor(editor)
            self._notify_dirty_if_needed()

    def _notify_dirty_if_needed(self) -> None:
        if self._on_dirty_changed is not None:
            self._on_dirty_changed()
        if self._dialog is not None and _alive(self._dialog):
            self._dialog._sync_action_buttons()

    def _wire_editor_dirty(self, editor: QWidget) -> None:
        text_changed = getattr(editor, "textChanged", None)
        if text_changed is not None and hasattr(text_changed, "connect"):
            text_changed.connect(self._notify_dirty_if_needed)

    def open(self) -> None:
        if self._dialog is not None and self._dialog.isVisible():
            if not self._popout_editor_dirty():
                self._sync_editor(self._dialog.editor)
            self._dialog._sync_action_buttons()
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._dialog = _PopOutDialog(self)
        self._wire_editor_dirty(self._dialog.editor)
        self._dialog.show()

    def close(self, *, force: bool = False) -> None:
        if self._dialog is None:
            return
        dlg = self._dialog
        self._dialog = None
        if dlg.isVisible():
            dlg.try_close(force=force)
        dlg.deleteLater()

    def clear_dialog_ref(self) -> None:
        self._dialog = None

    def _update_button(self) -> None:
        self._button.setEnabled(True)
        self._button.setToolTip(
            f"Open {self._title} in a larger window.")


class _PopOutDialog(QDialog):
    def __init__(self, controller: PopOutController) -> None:
        super().__init__(controller._owner.window())
        self._controller = controller
        context = ""
        if controller.context_provider is not None:
            context = (controller.context_provider() or "").strip()
        if context:
            self.setWindowTitle(f"{controller._title} — {context}")
        else:
            self.setWindowTitle(f"{controller._title} — Expanded")
        self.setMinimumSize(640, 400)
        self.resize(*controller._size)
        self.setModal(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        if controller._read_only:
            note = QLabel("Read-only view. Close when finished.")
        else:
            note = QLabel(
                "OK appears when you change something; it copies edits back "
                "to the paste box. Cancel discards pop-out changes."
            )
        note.setObjectName("dimLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.editor = controller._create_editor(self)
        layout.addWidget(self.editor, 1)
        controller._sync_editor(self.editor)

        if controller._read_only:
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(lambda: self._finish_close(apply=False))
        else:
            buttons = QDialogButtonBox()
            self._ok_btn = buttons.addButton(
                QDialogButtonBox.StandardButton.Ok)
            self._cancel_btn = buttons.addButton(
                QDialogButtonBox.StandardButton.Cancel)
            self._close_btn = buttons.addButton(
                QDialogButtonBox.StandardButton.Close)
            self._ok_btn.clicked.connect(lambda: self._finish_close(apply=True))
            self._cancel_btn.clicked.connect(
                lambda: self._finish_close(apply=False))
            self._close_btn.clicked.connect(
                lambda: self._finish_close(apply=False))
            self._buttons = buttons
        layout.addWidget(buttons)
        self._close_handled = False
        if not controller._read_only:
            self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        if self._controller._read_only:
            return
        dirty = self._controller._popout_editor_dirty(self.editor)
        if dirty:
            _clear_focus_if_descendant(self._close_btn)
        else:
            _clear_focus_if_descendant(self._ok_btn, self._cancel_btn)
        self._ok_btn.setVisible(dirty)
        self._cancel_btn.setVisible(dirty)
        self._close_btn.setVisible(not dirty)
        if dirty:
            self._ok_btn.setToolTip("Copy edits to the paste box")
            self._cancel_btn.setToolTip("Discard pop-out changes")
        else:
            self._close_btn.setToolTip("Close")

    def _finish_close(self, *, apply: bool) -> None:
        self.try_close(apply=apply)
        self._close_handled = True
        self.close()

    def closeEvent(self, event) -> None:
        if not self._close_handled:
            ctrl = self._controller
            dirty = (
                not ctrl._read_only
                and ctrl._popout_editor_dirty(self.editor)
            )
            if dirty:
                box = QMessageBox(self)
                box.setWindowTitle("Discard pop-out edits?")
                box.setIcon(QMessageBox.Icon.Question)
                box.setText("This pop-out has unapplied edits. Discard them?")
                discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
                box.addButton("Keep open", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is not discard:
                    event.ignore()
                    return
            self.try_close(apply=False)
        event.accept()

    def try_close(self, *, force: bool = False, apply: bool = False) -> bool:
        ctrl = self._controller
        should_apply = apply and not force
        if (
            should_apply
            and not ctrl._read_only
            and ctrl._apply_editor is not None
            and ctrl._editor_state is not None
            and ctrl._editor_state(self.editor) != ctrl._baseline
        ):
            ctrl._apply_editor(self.editor)
            ctrl._baseline = ctrl._editor_state(self.editor)
        ctrl.clear_dialog_ref()
        ctrl._notify_dirty_if_needed()
        return True
