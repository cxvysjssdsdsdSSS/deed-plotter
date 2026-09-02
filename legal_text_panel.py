"""Tab showing the AI-transcribed, cleaned-up legal description."""

from __future__ import annotations

import html
import re

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QMessageBox, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget,
)

_THENCE_RE = re.compile(r"^(thence\b|beginning\b|commencing\b)", re.IGNORECASE)

_CSS = """
<style>
  body { font-family: 'Segoe UI', sans-serif; font-size: 10.5pt;
         color: #e0e0e0; line-height: 1.5; }
  p { margin: 0 0 10px 0; }
  .call { margin: 0 0 8px 18px; text-indent: -18px; }
  .kw { color: #4fc3f7; font-weight: 600; }
</style>
"""


def _format_html(text: str) -> str:
    """Render the description with THENCE/BEGINNING keywords highlighted."""
    blocks = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        escaped = html.escape(line)
        if _THENCE_RE.match(line):
            escaped = re.sub(
                r"^(\w+)", r"<span class='kw'>\1</span>", escaped, count=1
            )
            blocks.append(f"<p class='call'>{escaped}</p>")
        else:
            blocks.append(f"<p>{escaped}</p>")
    return _CSS + "<body>" + "".join(blocks) + "</body>"


class LegalTextPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""

        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.copy_btn = QPushButton("Copy Text")
        self.copy_btn.setToolTip("Copy the full legal description to the clipboard.")
        self.copy_btn.clicked.connect(self._copy)
        self.save_btn = QPushButton("Save as TXT…")
        self.save_btn.setToolTip("Save the legal description as a plain text file.")
        self.save_btn.clicked.connect(self._save)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.save_btn)
        bar.addStretch()
        layout.addLayout(bar)

        self.view = QTextBrowser()
        layout.addWidget(self.view, stretch=1)
        self.set_text("")

    def set_text(self, text: str):
        self._text = (text or "").strip()
        has_text = bool(self._text)
        self.copy_btn.setEnabled(has_text)
        self.save_btn.setEnabled(has_text)
        if has_text:
            self.view.setHtml(_format_html(self._text))
        else:
            self.view.setHtml(
                _CSS + "<body><i>Run an AI parse to see the transcribed "
                "legal description here.</i></body>"
            )

    def plain_text(self) -> str:
        return self._text

    def _copy(self):
        if self._text:
            QGuiApplication.clipboard().setText(self._text)

    def _save(self):
        if not self._text:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Legal Description", "legal_description.txt", "Text files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._text + "\n")
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
