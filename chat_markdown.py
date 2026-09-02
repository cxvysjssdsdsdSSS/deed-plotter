"""Render chat Markdown to an HTML fragment for QTextBrowser."""

from __future__ import annotations

import re
import unicodedata

from PyQt6.QtGui import QTextDocument

_BODY_RE = re.compile(r"<body[^>]*>(.*)</body>", re.IGNORECASE | re.DOTALL)

# Vulgar fractions often lack glyphs in UI fonts → diamond "?" (or arrive as U+FFFD).
_VULGAR_FRACTIONS = {
    "\u00bc": "1/4",  # ¼
    "\u00bd": "1/2",  # ½
    "\u00be": "3/4",  # ¾
    "\u2150": "1/7",
    "\u2151": "1/9",
    "\u2152": "1/10",
    "\u2153": "1/3",  # ⅓  (Texas vara, etc.)
    "\u2154": "2/3",
    "\u2155": "1/5",
    "\u2156": "2/5",
    "\u2157": "3/5",
    "\u2158": "4/5",
    "\u2159": "1/6",
    "\u215a": "5/6",
    "\u215b": "1/8",
    "\u215c": "3/8",
    "\u215d": "5/8",
    "\u215e": "7/8",
}

# Exotic spaces that pair with fractions and confuse layout/fonts.
_ODD_SPACES = dict.fromkeys(
    (
        "\u00a0",  # nbsp
        "\u202f",  # narrow nbsp
        "\u2007",  # figure space
        "\u2008",  # punctuation space
        "\u2009",  # thin space
        "\u200a",  # hair space
        "\u200b",  # zero-width space
        "\ufeff",  # bom / zwnbsp
    ),
    " ",
)


def sanitize_chat_text(text: str) -> str:
    """Normalize chat text so QTextBrowser does not show diamond '?' glyphs."""
    if not text:
        return ""
    s = unicodedata.normalize("NFC", text)
    # Drop replacement chars left by upstream UTF-8 errors="replace".
    s = s.replace("\ufffd", "")
    for odd, repl in _ODD_SPACES.items():
        s = s.replace(odd, repl)
    # "33⅓" → "33 1/3" (space when glued to a digit).
    for frac, ascii_frac in _VULGAR_FRACTIONS.items():
        s = re.sub(
            rf"(\d){re.escape(frac)}",
            rf"\1 {ascii_frac}",
            s,
        )
        s = s.replace(frac, ascii_frac)
    # Collapse spaces left by stripping FFFD between digit and fraction.
    s = re.sub(r"(\d) +(\d/\d)", r"\1 \2", s)
    return s


def markdown_to_html_fragment(text: str) -> str:
    """Convert CommonMark/GFM-ish Markdown to an HTML body fragment.

    Uses Qt's QTextDocument (supports bold, lists, and pipe tables). Returns
    escaped plain paragraphs if Markdown conversion yields nothing useful.
    """
    raw = sanitize_chat_text(text or "").strip("\n")
    if not raw.strip():
        return ""

    doc = QTextDocument()
    # GitHub dialect when available — needed for pipe tables.
    features = getattr(QTextDocument, "MarkdownFeature", None)
    if features is not None and hasattr(features, "MarkdownDialectGitHub"):
        doc.setMarkdown(raw, features.MarkdownDialectGitHub)
    else:
        doc.setMarkdown(raw)

    html = doc.toHtml()
    match = _BODY_RE.search(html)
    frag = (match.group(1) if match else html).strip()
    # Drop Qt's empty outer paragraphs noise when possible.
    return frag or _plain_fallback(raw)


def _plain_fallback(text: str) -> str:
    safe = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br>")
    )
    return f"<p style='margin:0'>{safe}</p>"
