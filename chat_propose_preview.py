"""Open real editors to preview/edit chat action proposals (no workspace write)."""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox,
    QSpinBox, QVBoxLayout, QWidget,
)

from call_editor_dialog import CallEditorDialog
from chat_propose import action_summary, apply_call_actions
from cogo import Call
from details_panel import _INFO_FIELDS

_CALL_FIELD_KEYS = (
    "bearing", "distance", "units", "call_type", "curve_direction",
    "radius", "arc_length", "chord_length", "delta", "monument",
    "description", "confidence",
)


def _blank_call() -> Call:
    return Call(bearing='N 00°00\'00" E', distance=0.0, units="feet")


def _source_list(action: dict, calls: list[Call], ties: list[Call]) -> list[Call]:
    return list(ties if action.get("target") == "tie" else calls)


def _call_at(action: dict, calls: list[Call], ties: list[Call]) -> Call | None:
    src = _source_list(action, calls, ties)
    try:
        idx = int(action.get("call_index", 0)) - 1
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(src):
        return src[idx]
    return None


def _proposed_call(action: dict, calls: list[Call], ties: list[Call]) -> Call:
    """Call as it would look after this action (for the editor)."""
    kind = action.get("action")
    if kind == "add_call":
        new_list, _ = apply_call_actions([], [dict(action, include=True)])
        return new_list[0] if new_list else _blank_call()
    base = _call_at(action, calls, ties) or _blank_call()
    if kind in ("update_call", "set_confidence"):
        # Apply against a one-row table so call_index 1 maps cleanly.
        one = dict(action, include=True, call_index=1)
        if one.get("action") == "set_confidence":
            pass
        new_list, _ = apply_call_actions([base], [one])
        return new_list[0] if new_list else base
    return base


def _fields_from_call(call: Call) -> dict[str, Any]:
    out: dict[str, Any] = {
        "bearing": call.bearing,
        "distance": call.distance,
        "units": call.units or "feet",
        "call_type": call.call_type or "line",
        "monument": call.monument or "",
        "description": call.description or "",
        "confidence": call.confidence or "",
    }
    if (call.call_type or "").lower() == "curve":
        out["curve_direction"] = call.curve_direction or ""
        out["radius"] = call.radius
        out["arc_length"] = call.arc_length
        out["chord_length"] = call.chord_length
        out["delta"] = call.delta or ""
    return out


def _action_from_edited_call(action: dict, call: Call) -> dict:
    """Rewrite a call-mutating action from an edited Call."""
    out = {
        k: v for k, v in action.items()
        if k in ("action", "call_index", "after_index", "target", "include")
    }
    kind = out.get("action")
    if kind == "set_confidence":
        # Full editor — promote to update_call so bearing/etc. edits stick.
        out["action"] = "update_call"
        kind = "update_call"
    if kind == "delete_call":
        return out
    fields = _fields_from_call(call)
    # Drop keys we don't want to carry; keep only editor values.
    for key in _CALL_FIELD_KEYS:
        out.pop(key, None)
    out.update(fields)
    return out


class _DocumentInfoEditDialog(QDialog):
    """Edit fields for an update_document_info proposal."""

    def __init__(
        self,
        fields: dict[str, str],
        parent=None,
        *,
        title_suffix: str = "",
    ):
        super().__init__(parent)
        title = "Edit document info"
        if title_suffix:
            title = f"{title} — {title_suffix}"
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self._edits: dict[str, QLineEdit] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Review or change the document fields this action will set."
        ))
        form = QFormLayout()
        labels = {k: lab for k, lab in _INFO_FIELDS}
        # Show known fields first (proposed + empty known), then any extras.
        keys = [k for k, _ in _INFO_FIELDS]
        for k in fields:
            if k not in keys:
                keys.append(k)
        for key in keys:
            edit = QLineEdit(str(fields.get(key, "") or ""))
            form.addRow(f"{labels.get(key, key)}:", edit)
            self._edits[key] = edit
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def fields(self) -> dict[str, str]:
        out = {}
        for key, edit in self._edits.items():
            val = edit.text().strip()
            if val:
                out[key] = val
        return out


class _DeleteCallEditDialog(QDialog):
    """Confirm / retarget a delete_call proposal."""

    def __init__(
        self,
        action: dict,
        call: Call | None,
        parent=None,
        *,
        title_suffix: str = "",
    ):
        super().__init__(parent)
        label = "Tie" if action.get("target") == "tie" else "Call"
        title = f"Delete {label.lower()}"
        if title_suffix:
            title = f"{title} — {title_suffix}"
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        idx = int(action.get("call_index") or 1)
        if call is not None:
            brg = call.chord_bearing or call.bearing
            detail = (
                f"This action will remove {label} {idx}:\n\n"
                f"  {call.call_type}  {brg}  {call.distance:g} {call.units}\n"
            )
            if call.monument:
                detail += f"  monument: {call.monument}\n"
            if call.description:
                detail += f"  {call.description}\n"
        else:
            detail = (
                f"This action will remove {label} {idx}.\n\n"
                f"(No matching {label.lower()} in the current table — "
                "check the index.)"
            )
        layout.addWidget(QLabel(detail))
        form = QFormLayout()
        self.index_spin = QSpinBox()
        self.index_spin.setMinimum(1)
        self.index_spin.setMaximum(9999)
        self.index_spin.setValue(max(1, idx))
        form.addRow(f"{label} #:", self.index_spin)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Keep delete")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def call_index(self) -> int:
        return int(self.index_spin.value())


class _SideEffectViewDialog(QDialog):
    """Read-only explanation for parse / export proposals."""

    def __init__(self, action: dict, parent=None, *, title_suffix: str = ""):
        super().__init__(parent)
        title = action_summary(action)
        if title_suffix:
            title = f"{title} — {title_suffix}"
        self.setWindowTitle(title)
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        kind = action.get("action")
        if kind == "run_parse":
            body = (
                "This will ask the app to run Parse with AI.\n\n"
                "You will still get the usual Replace Traverse confirmation "
                "before anything is overwritten."
            )
        elif kind == "export_csv":
            body = (
                "This will open Export CSV for the current call table.\n\n"
                "Export QA warnings (open traverse, low confidence) still apply."
            )
        elif kind == "export_dxf":
            body = (
                "This will open Export DXF for the current plot.\n\n"
                "Export QA warnings (open traverse, low confidence) still apply."
            )
        else:
            body = action_summary(action)
        layout.addWidget(QLabel(body))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close:
            close.clicked.connect(self.accept)
        layout.addWidget(buttons)


def open_proposal_editor(
    action: dict,
    parent: QWidget | None = None,
    *,
    calls: list[Call] | None = None,
    tie_calls: list[Call] | None = None,
    document_info: dict | None = None,
    title_suffix: str = "",
) -> dict | None:
    """Open the real editor for *action*. Return updated action, or None if cancelled.

    Does not write to the workspace — only mutates the proposal dict.
    """
    del document_info  # reserved for future POB/context; doc fields come from action
    calls = list(calls or [])
    ties = list(tie_calls or [])
    kind = action.get("action")

    if kind in ("update_call", "set_confidence", "add_call"):
        snap = _proposed_call(action, calls, ties)
        dlg = CallEditorDialog(snap, parent)
        label = "tie" if action.get("target") == "tie" else "call"
        if kind == "add_call":
            dlg.setWindowTitle(
                f"Edit proposed new {label}"
                + (f" — {title_suffix}" if title_suffix else "")
            )
        else:
            idx = action.get("call_index", "?")
            dlg.setWindowTitle(
                f"Edit proposed {label} {idx}"
                + (f" — {title_suffix}" if title_suffix else "")
            )
        if not dlg.exec():
            return None
        return _action_from_edited_call(action, dlg.edited_call())

    if kind == "delete_call":
        victim = _call_at(action, calls, ties)
        dlg = _DeleteCallEditDialog(
            action, victim, parent, title_suffix=title_suffix,
        )
        if not dlg.exec():
            return None
        out = dict(action)
        out["call_index"] = dlg.call_index()
        return out

    if kind == "update_document_info":
        fields = dict(action.get("fields") or {})
        dlg = _DocumentInfoEditDialog(
            fields, parent, title_suffix=title_suffix,
        )
        if not dlg.exec():
            return None
        cleaned = dlg.fields()
        if not cleaned:
            QMessageBox.warning(
                parent, "Nothing to set",
                "Leave at least one document field filled, or Cancel.",
            )
            return None
        out = dict(action)
        out["fields"] = cleaned
        return out

    if kind in ("run_parse", "export_csv", "export_dxf"):
        dlg = _SideEffectViewDialog(action, parent, title_suffix=title_suffix)
        dlg.exec()
        return None  # view-only; Close/Esc must not count as Accept / advance

    QMessageBox.information(
        parent, "Cannot edit",
        f"No editor for action type {kind!r}.",
    )
    return None
