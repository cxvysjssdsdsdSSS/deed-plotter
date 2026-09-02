"""Panel that summarizes AI observations and traverse warnings."""

from __future__ import annotations

import html
import re

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QTextBrowser

from cogo import Call, TraverseResult
from closure_panel import is_traverse_closed, load_closure_tolerance

_CSS = """
<style>
  body { font-family: 'Segoe UI', sans-serif; font-size: 10.5pt; color: #e0e0e0; }
  h3 { color: #4fc3f7; margin-bottom: 4px; }
  .warn { color: #ffb74d; }
  .err { color: #ef9a9a; }
  .ok { color: #a5d6a7; }
  a { color: #64b5f6; text-decoration: none; }
  a:hover { text-decoration: underline; }
  li { margin-bottom: 4px; }
  .obs { margin: 0 0 10px 0; line-height: 1.35; }
  .obs-page { color: #90caf9; font-weight: 600; }
</style>
"""

# Split merged per-page notes ("[Page 1] …\\n\\n[Page 2] …") into blocks.
_PAGE_NOTE_SPLIT = re.compile(r"(?=\[Page\s+\d+\])", re.IGNORECASE)
_PAGE_NOTE_HEAD = re.compile(r"^\[Page\s+(\d+)\]\s*", re.IGNORECASE)


def _observation_blocks(notes: str) -> list[tuple[str | None, str]]:
    """Return (page_label_or_None, body) chunks for HTML rendering."""
    text = (notes or "").strip()
    if not text:
        return []
    raw_parts = [p.strip() for p in _PAGE_NOTE_SPLIT.split(text) if p.strip()]
    if len(raw_parts) <= 1 and "\n\n" in text:
        raw_parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    blocks: list[tuple[str | None, str]] = []
    for part in raw_parts:
        m = _PAGE_NOTE_HEAD.match(part)
        if m:
            body = part[m.end():].strip()
            blocks.append((f"Page {m.group(1)}", body))
        else:
            blocks.append((None, part))
    return blocks


def _format_observations_html(notes: str, esc) -> str:
    blocks = _observation_blocks(notes)
    if not blocks:
        return ""
    chunks = ["<h3>AI Observations</h3>"]
    for label, body in blocks:
        # Preserve single newlines inside a page note; HTML would otherwise
        # collapse the whole merge into one wall of text.
        body_html = esc(body).replace("\n", "<br>")
        if label:
            chunks.append(
                f"<p class='obs'><span class='obs-page'>{esc(label)}</span>"
                f"<br>{body_html}</p>"
            )
        else:
            chunks.append(f"<p class='obs'>{body_html}</p>")
    return "".join(chunks)


class NotesPanel(QTextBrowser):
    """Read-only rich-text view of parse notes, low-confidence calls,
    and closure diagnostics. Click a 'Call N' link to jump to that row."""

    callActivated = pyqtSignal(int)  # 0-based row index

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._on_anchor)
        self._pob = ""
        self._pob_monument = ""
        self._general_notes = ""
        self._parse_warnings: list[str] = []
        self._show_placeholder()

    def _show_placeholder(self):
        self.setHtml(_CSS + "<body><i>Run an AI parse to see observations and warnings here.</i></body>")

    def set_parse_info(self, pob: str, general_notes: str, pob_monument: str = "",
                       parse_warnings: list[str] | None = None):
        self._pob = pob or ""
        self._general_notes = general_notes or ""
        self._pob_monument = pob_monument or ""
        self._parse_warnings = list(parse_warnings or [])

    def clear_parse_info(self):
        self._pob = ""
        self._pob_monument = ""
        self._general_notes = ""
        self._parse_warnings = []

    def _on_anchor(self, url):
        m = re.fullmatch(r"call:(\d+)", url.toString())
        if m:
            self.callActivated.emit(int(m.group(1)) - 1)

    def update_content(
        self,
        calls: list[Call],
        result: TraverseResult | None,
        *,
        expected_open: bool = False,
    ):
        if not calls and not self._pob and not self._general_notes and not self._parse_warnings:
            self._show_placeholder()
            return
        esc = html.escape
        parts = [_CSS, "<body>"]

        if self._pob:
            parts.append(f"<h3>Point of Beginning</h3><p>{esc(self._pob)}</p>")
            if self._pob_monument:
                parts.append(f"<p class='ok'>Monument at POB: {esc(self._pob_monument)}</p>")
            else:
                # Blank may mean "unstated" (common on old field notes) OR a
                # calculated POB reached by ties — don't assume a tie exists.
                parts.append(
                    "<p class='warn'>No physical monument at the POB is stated "
                    "in the deed.</p>"
                )
        if self._general_notes:
            parts.append(_format_observations_html(self._general_notes, esc))

        if self._parse_warnings:
            parts.append("<h3>Parse Warnings</h3><ul>")
            for w in self._parse_warnings:
                parts.append(f"<li class='warn'>{esc(w)}</li>")
            parts.append("</ul>")

        flagged = [
            (i, c) for i, c in enumerate(calls, start=1)
            if c.confidence.lower() in ("low", "medium")
        ]
        if flagged:
            parts.append("<h3>Calls Needing Review</h3><ul>")
            for i, c in flagged:
                cls = "err" if c.confidence.lower() == "low" else "warn"
                brg = c.chord_bearing or c.bearing
                length = c.chord_length if c.call_type == "curve" and c.chord_length else c.distance
                detail = f" &mdash; {esc(c.description)}" if c.description else ""
                parts.append(
                    f"<li class='{cls}'><a href='call:{i}'>Call {i}</a> "
                    f"({esc(c.confidence)} confidence): "
                    f"{esc(brg)} {length:g} {esc(c.units)}{detail}</li>"
                )
            parts.append("</ul>")

        if result is not None:
            if result.warnings:
                parts.append("<h3>Traverse Warnings</h3><ul>")
                for w in result.warnings:
                    parts.append(f"<li class='warn'>{esc(w)}</li>")
                parts.append("</ul>")

            parts.append("<h3>Closure Check</h3><ul>")
            if result.errors:
                for e in result.errors:
                    m = re.match(r"Call (\d+):", e)
                    if m:
                        n = m.group(1)
                        rest = esc(e[len(m.group(0)):].lstrip())
                        parts.append(
                            f"<li class='err'><a href='call:{n}'>Call {n}</a>: {rest}</li>"
                        )
                    else:
                        parts.append(f"<li class='err'>{esc(e)}</li>")
            tol = load_closure_tolerance()
            closed = is_traverse_closed(result, tol)
            brg = f" ({esc(result.closure_bearing)})" if result.closure_bearing else ""
            if closed:
                parts.append(
                    f"<li class='ok'>CLOSED — misclosure {result.closure_error:,.3f} ft{brg} "
                    f"within tolerance {tol:.3f} ft "
                    f"(precision {esc(result.precision)}). "
                    "See Closure tab for ΔX/ΔY and acreage check.</li>"
                )
            else:
                parts.append(
                    f"<li class='warn'>OPEN — misclosure {result.closure_error:,.3f} ft{brg} "
                    f"(tolerance {tol:.3f} ft, precision {esc(result.precision)}). "
                    + (
                        "Expected for this open control/county line document type; "
                        "closure and area are not meaningful."
                        if expected_open else
                        "See Closure tab for ΔX/ΔY; check bearings, distances, or missing calls."
                    )
                    + "</li>"
                )
            parts.append("</ul>")

        parts.append("</body>")
        self.setHtml("".join(parts))
