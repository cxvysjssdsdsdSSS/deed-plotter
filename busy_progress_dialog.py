"""Shared progress dialog for load / parse / chat."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QProgressBar, QProgressDialog, QSizePolicy

# Match main.py Fusion dark palette so the first paint is never a white groove.
_BAR_STYLE = """
QProgressBar {
    border: 1px solid #3a4048;
    border-radius: 3px;
    background-color: #16191d;
    text-align: center;
    color: #e0e0e0;
    min-height: 18px;
    max-height: 18px;
}
QProgressBar::chunk {
    background-color: #4296fa;
    border-radius: 2px;
    margin: 0px;
}
"""


class BusyProgressDialog(QProgressDialog):
    """QProgressDialog used for load / parse / chat.

    Dark-styled bar before show() avoids the white-groove flash. The label is
    height-locked to the initial text so Page/Elapsed updates do not resize
    the dialog.
    """

    def __init__(
        self,
        label: str,
        *,
        title: str,
        parent=None,
        cancel_text: str = "Cancel",
        min_width: int = 360,
        maximum: int = 0,
        bar_text_visible: bool | None = None,
    ):
        super().__init__(label, cancel_text, 0, maximum, parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumDuration(0)
        self.setMinimumWidth(min_width)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        bar = QProgressBar(self)
        bar.setRange(0, maximum)
        bar.setValue(0)
        # Default: show n/m only when determinate. Callers can force off until
        # the real total is known (avoids a flash of "0 / 1" placeholder).
        show_text = (maximum > 0) if bar_text_visible is None else bool(bar_text_visible)
        bar.setTextVisible(show_text)
        bar.setFormat("%v / %m")
        bar.setStyleSheet(_BAR_STYLE)
        self.setBar(bar)

        if maximum == 0:
            self.setValue(0)

        self._lock_label_height(label, min_width)

    def _lock_label_height(self, text: str, min_width: int) -> None:
        """Keep vertical size stable when setLabelText updates Page/Elapsed."""
        lbl = self.findChild(QLabel)
        if lbl is None:
            return
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        # Width available for wrapping ≈ dialog min width minus margins/chrome.
        wrap_w = max(200, min_width - 48)
        fm = lbl.fontMetrics()
        bounds = fm.boundingRect(
            0, 0, wrap_w, 4000,
            int(Qt.TextFlag.TextWordWrap),
            text,
        )
        # One extra line of slack for slightly longer status strings.
        lbl.setMinimumHeight(bounds.height() + fm.lineSpacing() + 4)
        lbl.setMinimumWidth(wrap_w)

    def set_bar_text_visible(self, visible: bool) -> None:
        bar = self.findChild(QProgressBar)
        if bar is not None:
            bar.setTextVisible(bool(visible))

    def prepare_close(self) -> None:
        """No-op: kept so MainWindow close path stays uniform."""
