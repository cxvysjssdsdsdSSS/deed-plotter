"""Popup dialog for editing one boundary call with a structured bearing editor."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from cogo import (
    Call,
    QUADRANT_OPTIONS,
    curve_can_derive_chord,
    curve_direction_is_left,
    decompose_quadrant_bearing,
    format_quadrant_bearing,
    newly_derivable_copied_chord,
    parse_bearing,
)

UNIT_OPTIONS = ("feet", "chains", "rods", "poles", "varas", "links", "meters")


class BearingEditor(QWidget):
    """Quadrant + DMS spinners with live preview, and a free-text fallback
    for azimuths or archaic wording."""

    def __init__(self, initial: str = "", parent=None):
        super().__init__(parent)
        self._text_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._structured = QWidget()
        grid = QGridLayout(self._structured)
        grid.setContentsMargins(0, 0, 0, 0)
        self.quadrant = QComboBox()
        self.quadrant.addItems(QUADRANT_OPTIONS)
        self.quadrant.setToolTip("NE = North\u2013East, NW = North\u2013West, "
                                 "SE = South\u2013East, SW = South\u2013West.")
        self.deg = QSpinBox(maximum=90)
        self.mnt = QSpinBox(maximum=59)
        self.sec = QSpinBox(maximum=59)
        for col, (label, widget) in enumerate(
            (("Quadrant", self.quadrant), ("Deg", self.deg), ("Min", self.mnt), ("Sec", self.sec))
        ):
            grid.addWidget(QLabel(label), 0, col * 2)
            grid.addWidget(widget, 0, col * 2 + 1)
        grid.setColumnStretch(8, 1)

        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("Free-form bearing (e.g. azimuth '123.5' or archaic wording)")
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        text_layout.addWidget(self.text_edit)
        text_layout.addStretch()

        # Same reserved height for both modes so the Edit Call dialog does not
        # jump when switching quadrant ↔ text.
        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._stack.addWidget(self._structured)
        self._stack.addWidget(text_page)
        layout.addWidget(self._stack)

        self.preview = QLabel("Preview: \u2014")
        self.preview.setStyleSheet("color: #9e9e9e;")
        layout.addWidget(self.preview)

        self.mode_btn = QPushButton("Edit as text")
        self.mode_btn.setFlat(True)
        self.mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.mode_btn.setStyleSheet(
            "QPushButton { color: #64b5f6; text-align: left; border: none; padding: 0px; }"
        )
        self.mode_btn.clicked.connect(self._toggle_mode)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(self.mode_btn, 0, Qt.AlignmentFlag.AlignLeft)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        self.set_bearing(initial)
        self._lock_mode_height()
        for spin in (self.deg, self.mnt, self.sec):
            spin.valueChanged.connect(self._refresh_preview)
        self.quadrant.currentIndexChanged.connect(self._refresh_preview)
        self.text_edit.textChanged.connect(self._refresh_preview)

    def set_bearing(self, text: str):
        stripped = (text or "").strip()
        parts = decompose_quadrant_bearing(stripped) if stripped else None
        if parts is not None:
            self._text_mode = False
            quadrant, d, m, s = parts
            self.quadrant.setCurrentText(quadrant)
            self.deg.setValue(d)
            self.mnt.setValue(m)
            self.sec.setValue(s)
        else:
            self._text_mode = True
            self.text_edit.setText(stripped)
        self._show_mode()
        self._refresh_preview()

    def _toggle_mode(self):
        if self._text_mode:
            parts = decompose_quadrant_bearing(self.text_edit.text())
            if parts is not None:
                quadrant, d, m, s = parts
                self.quadrant.setCurrentText(quadrant)
                self.deg.setValue(d)
                self.mnt.setValue(m)
                self.sec.setValue(s)
            # Always return to the quadrant editor. Empty / azimuth / archaic
            # text cannot fill DMS; keep the last spinner values.
            self._text_mode = False
        else:
            # Always copy current DMS so a Deg edit is not hidden by a leftover
            # string from a previous trip through text mode.
            self.text_edit.setText(self.get_bearing())
            self._text_mode = True
        self._show_mode()
        self._refresh_preview()

    def _show_mode(self):
        self._stack.setCurrentIndex(1 if self._text_mode else 0)
        self.mode_btn.setText("Use quadrant editor" if self._text_mode else "Edit as text")
        self.mode_btn.adjustSize()

    def _lock_mode_height(self):
        self._structured.ensurePolished()
        self.text_edit.ensurePolished()
        h = max(
            self._structured.sizeHint().height(),
            self._structured.minimumSizeHint().height(),
            self.text_edit.sizeHint().height(),
        )
        self._stack.setFixedHeight(h)

    def _refresh_preview(self):
        text = self.get_bearing()
        if not text:
            self.preview.setText("Preview: \u2014")
            self.preview.setStyleSheet("color: #9e9e9e;")
            return
        try:
            parse_bearing(text)
            self.preview.setText(f"Preview: {text}")
            self.preview.setStyleSheet("color: #a5d6a7;")
        except ValueError as exc:
            self.preview.setText(f"Preview: {exc}")
            self.preview.setStyleSheet("color: #ef9a9a;")

    def get_bearing(self) -> str:
        if self._text_mode:
            return self.text_edit.text().strip()
        return format_quadrant_bearing(
            self.quadrant.currentText(), self.deg.value(), self.mnt.value(), self.sec.value()
        )


class CallEditorDialog(QDialog):
    """Edit a single Call. Returns the edited Call via edited_call()."""

    def __init__(self, call: Call, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Call")
        self.setMinimumWidth(520)
        self._original = call

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.type_combo = QComboBox()
        self.type_combo.addItems(["line", "curve"])
        self.type_combo.setCurrentText(call.call_type if call.call_type in ("line", "curve") else "line")
        self.type_combo.currentTextChanged.connect(self._sync_curve_visibility)
        form.addRow("Type:", self.type_combo)

        self.bearing_editor = BearingEditor(call.chord_bearing or call.bearing)
        form.addRow("Bearing / chord brg:", self.bearing_editor)

        dist_row = QHBoxLayout()
        self.distance = QDoubleSpinBox(maximum=1e9, decimals=4)
        self.distance.setValue(call.distance)
        self.units = QComboBox()
        self.units.setEditable(True)
        self.units.addItems(UNIT_OPTIONS)
        self.units.setCurrentText(call.units or "feet")
        dist_row.addWidget(self.distance, stretch=1)
        dist_row.addWidget(self.units)
        dist_wrap = QWidget()
        dist_wrap.setLayout(dist_row)
        form.addRow("Distance:", dist_wrap)

        self.curve_group = QGroupBox("Curve data")
        cf = QFormLayout(self.curve_group)
        self.direction = QComboBox()
        self.direction.addItems(["", "right", "left"])
        if curve_direction_is_left(call.curve_direction):
            self.direction.setCurrentText("left")
        elif (call.curve_direction or "").strip():
            self.direction.setCurrentText("right")
        self.radius = QDoubleSpinBox(maximum=1e9, decimals=4)
        self.radius.setValue(call.radius)
        self.arc_length = QDoubleSpinBox(maximum=1e9, decimals=4)
        self.arc_length.setValue(call.arc_length)
        self.chord_length = QDoubleSpinBox(maximum=1e9, decimals=4)
        self.chord_length.setValue(call.chord_length)
        self.delta = QLineEdit(call.delta)
        self.delta.setPlaceholderText("e.g. 12\u00b030'00\" or 12.5")
        cf.addRow("Direction:", self.direction)
        cf.addRow("Radius:", self.radius)
        cf.addRow("Arc length:", self.arc_length)
        cf.addRow("Chord length:", self.chord_length)
        cf.addRow("Delta:", self.delta)
        form.addRow(self.curve_group)

        self.monument = QPlainTextEdit(call.monument)
        self.monument.setPlaceholderText("e.g. 1/2\" iron rod found bears N 33\u00b006' W, 1.0 foot")
        self.monument.setFixedHeight(64)
        self.monument.setTabChangesFocus(True)
        form.addRow("Monument at end:", self.monument)
        self.description = QPlainTextEdit(call.description)
        self.description.setPlaceholderText("Source call text, e.g. 'along the north line of Lot 4'")
        self.description.setFixedHeight(64)
        self.description.setTabChangesFocus(True)
        form.addRow("Description:", self.description)

        self.confidence = QComboBox()
        self.confidence.addItems(["", "high", "medium", "low"])
        conf = (call.confidence or "").lower()
        if conf in ("high", "medium", "low"):
            self.confidence.setCurrentText(conf)
        form.addRow("Confidence:", self.confidence)

        self.mark_reviewed = QCheckBox("Mark as reviewed (clear low/medium highlight)")
        self.mark_reviewed.setToolTip(
            "After you verify this call against the deed, check this to clear the review flag."
        )
        form.addRow("", self.mark_reviewed)

        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._syncing_len = False
        self._could_derive = False
        self._sync_curve_visibility(self.type_combo.currentText())
        self.distance.valueChanged.connect(self._on_distance_changed)
        self.chord_length.valueChanged.connect(self._on_chord_changed)
        self.radius.valueChanged.connect(self._on_radius_changed)
        self.arc_length.valueChanged.connect(self._on_radius_changed)
        self.delta.textChanged.connect(self._on_radius_changed)

    def _sync_curve_visibility(self, call_type: str):
        self.curve_group.setVisible(call_type == "curve")
        self.adjustSize()
        self._refresh_could_derive()

    def _refresh_could_derive(self) -> None:
        if not hasattr(self, "radius"):
            return
        self._could_derive = (
            self.type_combo.currentText() == "curve"
            and curve_can_derive_chord(
                self.radius.value(), self.delta.text(), self.arc_length.value(),
            )
        )

    def _curve_lengths_linked(self) -> bool:
        """Dist↔Chord copy is only for Dist-only curves (no Radius, no Delta)."""
        return (
            self.type_combo.currentText() == "curve"
            and not curve_can_derive_chord(
                self.radius.value(), self.delta.text(), self.arc_length.value(),
            )
        )

    def _on_distance_changed(self, value: float):
        if self._syncing_len or not self._curve_lengths_linked():
            return
        self._syncing_len = True
        self.chord_length.setValue(value)
        self._syncing_len = False

    def _on_chord_changed(self, value: float):
        if self._syncing_len or not self._curve_lengths_linked():
            return
        self._syncing_len = True
        self.distance.setValue(value)
        self._syncing_len = False

    def _on_radius_changed(self, _value: float):
        if self._syncing_len or self.type_combo.currentText() != "curve":
            return
        derive_now = curve_can_derive_chord(
            self.radius.value(), self.delta.text(), self.arc_length.value(),
        )
        if newly_derivable_copied_chord(
            derive_now, self._could_derive,
            self.distance.value(), self.chord_length.value(),
        ):
            self._syncing_len = True
            self.chord_length.setValue(0.0)
            self._syncing_len = False
        self._could_derive = derive_now

    def edited_call(self) -> Call:
        call_type = self.type_combo.currentText()
        bearing = self.bearing_editor.get_bearing()
        is_curve = call_type == "curve"
        if self.mark_reviewed.isChecked():
            confidence = "high"
        else:
            confidence = self.confidence.currentText()
        dist = self.distance.value()
        chord = self.chord_length.value() if is_curve else 0.0
        orig = self._original
        extra: dict = {}
        if orig.input_distance and dist == 0:
            extra["input_distance"] = orig.input_distance
        if orig.input_radius and is_curve and self.radius.value() == 0:
            extra["input_radius"] = orig.input_radius
        if orig.input_arc_length and is_curve and self.arc_length.value() == 0:
            extra["input_arc_length"] = orig.input_arc_length
        if orig.input_chord_length and is_curve and chord == 0:
            extra["input_chord_length"] = orig.input_chord_length
        if extra:
            extra["input_error"] = orig.input_error
        return Call(
            call_type=call_type,
            bearing=bearing,
            distance=dist,
            units=self.units.currentText().strip() or "feet",
            curve_direction=self.direction.currentText() if is_curve else "",
            radius=self.radius.value() if is_curve else 0.0,
            arc_length=self.arc_length.value() if is_curve else 0.0,
            chord_bearing=bearing if is_curve else "",
            chord_length=chord,
            delta=self.delta.text().strip() if is_curve else "",
            monument=self.monument.toPlainText().strip(),
            description=self.description.toPlainText().strip(),
            confidence=confidence,
            **extra,
        )
