"""Always-on chat pane: ask about the current deed or how to use Deed Plotter."""

from __future__ import annotations

import time

from PyQt6.QtCore import QUrl, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QKeyEvent, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy,
    QTextBrowser, QVBoxLayout, QWidget,
)

from chat_markdown import markdown_to_html_fragment
from shimmer_label import ShimmerLabel

_COMPOSER_H = 28  # reserved status footer — avoids layout jump

# QTextBrowser.openLinks defaults True. With openExternalLinks False, a click
# on a markdown URL navigates *inside* the transcript and wipes the chat.
_EXTERNAL_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


def chat_url_should_open_externally(url: QUrl | str) -> bool:
    """True for http(s)/mailto only — never file, javascript, or in-app hrefs."""
    parsed = url if isinstance(url, QUrl) else QUrl(str(url))
    return parsed.scheme().lower() in _EXTERNAL_LINK_SCHEMES


class _ChatInput(QPlainTextEdit):
    """Enter sends; Shift+Enter inserts a newline."""

    sendRequested = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                event.accept()
                self.sendRequested.emit()
            return
        super().keyPressEvent(event)


class ChatPanel(QWidget):
    """Side chat transcript + input. Emits sendRequested(message)."""

    sendRequested = pyqtSignal(str)
    clearRequested = pyqtSignal()
    cancelRequested = pyqtSignal()
    pendingRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        title_row = QHBoxLayout()
        title = QLabel("Chat")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #e0e0e0;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.pending_btn = QPushButton("Pending actions…")
        self.pending_btn.setToolTip(
            "Reopen chat-proposed changes you have not applied yet.\n"
            "Enabled when a proposal is waiting (Later / partial Apply)."
        )
        self.pending_btn.setEnabled(False)
        self.pending_btn.clicked.connect(self.pendingRequested.emit)
        title_row.addWidget(self.pending_btn)
        layout.addLayout(title_row)
        self._pending_count = 0

        hint = QLabel(
            "Ask about this deed or how to use the app. "
            "Request a change and you’ll review actions before they apply."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._on_chat_link)
        self.view.document().setDefaultStyleSheet(
            "body, p, li, td, th, span { color: #e0e0e0; }"
            "table { border-collapse: collapse; margin: 6px 0; }"
            "td, th { border: 1px solid #6a7684; padding: 4px 8px; }"
            "a { color: #64b5f6; }"
            "code { background: #2a3038; color: #ffe082; padding: 1px 4px; }"
            "pre { background: #2a3038; color: #e0e0e0; padding: 8px; }"
        )
        layout.addWidget(self.view, stretch=1)

        # Composer: input + reserved status footer (no insert/remove jump).
        self._composer = QWidget()
        self._composer.setObjectName("chatComposer")
        self._composer.setStyleSheet(
            "#chatComposer {"
            "  background: #1a1f25;"
            "  border: 1px solid #333940;"
            "  border-radius: 6px;"
            "}"
            "#chatComposer QPlainTextEdit {"
            "  border: none;"
            "  background: transparent;"
            "  color: #e0e0e0;"
            "  padding: 6px 8px 2px 8px;"
            "}"
            "#chatComposerStatus {"
            "  background: transparent;"
            "  border: none;"
            "}"
            "#chatComposerStatus[active=\"true\"] {"
            "  border-top: 1px solid #2a3038;"
            "}"
        )
        composer_lay = QVBoxLayout(self._composer)
        composer_lay.setContentsMargins(0, 0, 0, 0)
        composer_lay.setSpacing(0)

        self.input = _ChatInput()
        self.input.setPlaceholderText(
            "Ask a question or request a change… (Enter to send, Shift+Enter for a new line)"
        )
        self.input.setFixedHeight(72)
        self.input.sendRequested.connect(self._emit_send)
        composer_lay.addWidget(self.input)

        self._think_bar = QWidget()
        self._think_bar.setObjectName("chatComposerStatus")
        self._think_bar.setFixedHeight(_COMPOSER_H)
        think_row = QHBoxLayout(self._think_bar)
        think_row.setContentsMargins(10, 0, 6, 0)
        think_row.setSpacing(6)

        self.thinking_label = ShimmerLabel(self._think_bar)
        self.thinking_label.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self._think_stamp = QLabel("")
        self._think_stamp.setStyleSheet(
            "color: #6a737d; font-style: italic; font-size: 12px; "
            "border: none; background: transparent;"
        )
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setToolTip("Stop the current assistant reply.")
        self.cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_btn.setFlat(True)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            "  color: #9aa3ad; background: transparent;"
            "  border: none; border-radius: 3px;"
            "  padding: 2px 8px; font-size: 12px;"
            "}"
            "QPushButton:hover {"
            "  color: #e8eaed; background: #2a3038;"
            "}"
            "QPushButton:disabled {"
            "  color: #4a5560;"
            "}"
        )
        self.cancel_btn.clicked.connect(self.cancelRequested.emit)
        think_row.addWidget(self.thinking_label, 0, Qt.AlignmentFlag.AlignVCenter)
        think_row.addWidget(self._think_stamp, 0, Qt.AlignmentFlag.AlignVCenter)
        think_row.addStretch(1)
        think_row.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        composer_lay.addWidget(self._think_bar)
        layout.addWidget(self._composer)

        self._thinking = False
        self._stopping = False
        self._think_started = 0.0
        self._think_timer = QTimer(self)
        self._think_timer.setInterval(250)
        self._think_timer.timeout.connect(self._tick_thinking)
        self._set_status_idle()

        row = QHBoxLayout()
        self.send_btn = QPushButton("Send")
        self.send_btn.setObjectName("primaryBtn")
        self.send_btn.setToolTip("Send to the assistant (Enter). Shift+Enter for a new line.")
        self.send_btn.clicked.connect(self._emit_send)
        self.clear_btn = QPushButton("Clear chat")
        self.clear_btn.clicked.connect(self.clearRequested.emit)
        self.busy_label = QLabel("")
        self.busy_label.setStyleSheet("color: #9e9e9e;")
        row.addWidget(self.send_btn)
        row.addWidget(self.clear_btn)
        row.addStretch()
        row.addWidget(self.busy_label)
        layout.addLayout(row)

    def _set_status_active(self, active: bool) -> None:
        self._think_bar.setProperty("active", "true" if active else "false")
        style = self._think_bar.style()
        style.unpolish(self._think_bar)
        style.polish(self._think_bar)
        self._think_bar.update()

    def _set_status_idle(self) -> None:
        """Clear footer content but keep the reserved strip (no layout jump)."""
        self.thinking_label.stop()
        self.thinking_label.clear()
        self._think_stamp.clear()
        self.cancel_btn.hide()
        self.cancel_btn.setEnabled(True)
        self._set_status_active(False)

    def _emit_send(self):
        text = self.input.toPlainText().strip()
        if not text or not self.send_btn.isEnabled() or self.is_thinking():
            return
        self.sendRequested.emit(text)

    def is_thinking(self) -> bool:
        """True while a reply is in flight or Cancel is winding the worker down."""
        return self._thinking or self._stopping

    def set_pending_count(self, n: int, *, busy: bool = False) -> None:
        """Always-visible pending control; enabled only when n > 0 and not busy."""
        self._pending_count = max(0, int(n))
        self.pending_btn.setText(
            f"Pending actions… ({self._pending_count})"
            if self._pending_count else "Pending actions…"
        )
        self.pending_btn.setEnabled(
            self._pending_count > 0 and not busy and not self.is_thinking()
        )

    def set_busy(self, busy: bool, message: str = ""):
        """Disable input while *any* app job runs (parse/load/chat)."""
        thinking = self.is_thinking()
        self.send_btn.setEnabled(not busy and not thinking)
        self.clear_btn.setEnabled(not busy and not thinking)
        self.input.setReadOnly(busy or thinking)
        # Thinking/stopping strip owns the wait copy — keep busy_label clear.
        if thinking:
            self.busy_label.setText("")
        else:
            self.busy_label.setText(message if busy else "")
        self.pending_btn.setEnabled(
            self._pending_count > 0 and not busy and not thinking
        )

    def set_thinking(self, on: bool) -> None:
        """Show/hide the inline assistant-thinking indicator (chat only)."""
        self._stopping = False
        self._thinking = on
        if on:
            self._think_started = time.monotonic()
            self._set_status_active(True)
            self.cancel_btn.setEnabled(True)
            self.cancel_btn.show()
            self.thinking_label.setText("Thinking…")
            self.thinking_label.start()
            self.send_btn.setEnabled(False)
            self.clear_btn.setEnabled(False)
            self.input.setReadOnly(True)
            self.busy_label.setText("")
            self.pending_btn.setEnabled(False)
            self._tick_thinking()
            self._think_timer.start()
        else:
            self._think_timer.stop()
            self._set_status_idle()
            # Send/clear stay gated by the next set_busy(_app_busy) call when
            # a worker is still winding down after Cancel.
            self.send_btn.setEnabled(True)
            self.clear_btn.setEnabled(True)
            self.input.setReadOnly(False)
            self.pending_btn.setEnabled(self._pending_count > 0)

    def set_stopping(self) -> None:
        """Cancel clicked — keep the strip until the worker actually exits."""
        if not self.is_thinking():
            return
        self._thinking = False
        self._stopping = True
        self._think_timer.stop()
        secs = int(time.monotonic() - self._think_started) if self._think_started else 0
        stamp = self._format_think_stamp(secs)
        # Solid (no shimmer) while Cancel winds down.
        self._set_status_active(True)
        self.thinking_label.stop()
        self.thinking_label.setText("Stopping…")
        self._think_stamp.setText(f"·  {stamp}")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.show()
        self.send_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self.input.setReadOnly(True)
        self.busy_label.setText("")

    @staticmethod
    def _format_think_stamp(secs: int) -> str:
        secs = max(0, int(secs))
        mins, s = divmod(secs, 60)
        if mins:
            return f"{mins}m {s:02d}s"
        return f"{s:>2d}s"

    def _tick_thinking(self) -> None:
        if not self._thinking:
            return
        stamp = self._format_think_stamp(int(time.monotonic() - self._think_started))
        self.thinking_label.setText("Thinking…")
        self._think_stamp.setText(f"·  {stamp}")

    def _on_chat_link(self, url: QUrl) -> None:
        """Open http(s)/mailto in the OS browser; ignore other schemes."""
        if chat_url_should_open_externally(url):
            QDesktopServices.openUrl(url)

    def clear_input(self):
        self.input.clear()

    def clear_transcript(self):
        self.view.clear()

    def has_transcript(self) -> bool:
        return bool(self.view.toPlainText().strip())

    def append(self, role: str, text: str):
        colors = {
            "You": "#64b5f6",
            "Assistant": "#a5d6a7",
            "System": "#ffb74d",
            "Error": "#ef9a9a",
        }
        color = colors.get(role, "#e0e0e0")
        body = markdown_to_html_fragment(text)
        self.view.append(
            f"<p style='margin:8px 0 2px 0'><b style='color:{color}'>{role}</b></p>"
            f"<div style='margin:0 0 10px 0;color:#e0e0e0'>{body}</div>"
        )
        self.view.moveCursor(QTextCursor.MoveOperation.End)
