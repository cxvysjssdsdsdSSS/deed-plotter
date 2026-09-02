"""Closure details tab: misclosure components, tolerance pass/fail, area
checks against the deed's stated acreage, and a unit conversion reference."""

from __future__ import annotations

import re

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QLabel, QTextBrowser, QVBoxLayout, QWidget

from cogo import FEET_PER_METER, FEET_PER_VARA, TraverseResult

ORG = "DeedPlotter"
APP = "DeedPlotter"
DEFAULT_TOLERANCE_FT = 0.01
# Stated vs computed acreage disagreement worth flagging (deeds round to ~3 dp).
ACREAGE_FLAG_PCT = 1.0
# Show computed area when closed, or when misclosure is still "small" even if OPEN.
AREA_RELIABLE_FT = 1.0

_UNIT_TABLE = (
    ("chain", "66 ft"),
    ("rod / pole / perch", "16.5 ft"),
    ("link (Gunter's)", "0.66 ft"),
    ("yard", "3 ft"),
    ("vara (Texas)", f"33\u2153 in = {FEET_PER_VARA:.4f} ft"),
    ("meter", f"{FEET_PER_METER:.8f} ft (US survey)"),
)


def load_closure_tolerance() -> float:
    return QSettings(ORG, APP).value("closure_tolerance_ft", DEFAULT_TOLERANCE_FT, float)


def is_traverse_closed(
    result: TraverseResult | None,
    tolerance_ft: float | None = None,
) -> bool:
    """Same CLOSED rule as the Closure tab (no geometry errors + within tolerance)."""
    if result is None or not result.segments:
        return False
    tol = load_closure_tolerance() if tolerance_ft is None else float(tolerance_ft)
    return (not result.errors) and result.closure_error <= tol


def area_is_reliable(
    result: TraverseResult | None,
    tolerance_ft: float | None = None,
) -> bool:
    """Match Closure tab: show area if CLOSED or misclosure < 1 ft.

    Geometry errors (skipped calls) always withhold area — a small misclosure
    after a dropped leg is not trustworthy acreage.
    """
    if result is None or not result.segments or result.errors:
        return False
    return is_traverse_closed(result, tolerance_ft) or result.closure_error < AREA_RELIABLE_FT


def parse_stated_acres(text: str) -> float | None:
    """Pull the acre figure out of e.g. '16.715 acres' / '0.2567 of an acre'.

    Thousands separators are stripped like POB / call distances, so
    '1,234.56 acres' is 1234.56, not 234.56.
    """
    cleaned = str(text or "").replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:of an\s+)?acres?\b", cleaned, re.IGNORECASE)
    if m:
        return float(m.group(1))
    try:
        return float(cleaned.strip())
    except ValueError:
        return None


class ClosurePanel(QWidget):
    """Closure details; tolerance is edited in Settings (closure_tolerance_ft)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: TraverseResult | None = None
        self._stated_acreage = ""
        self._expected_open = False

        layout = QVBoxLayout(self)

        self.status_label = QLabel("\u2014")
        self.status_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        layout.addWidget(self.status_label)

        self.body = QTextBrowser()
        self.body.setOpenExternalLinks(False)
        layout.addWidget(self.body, stretch=1)
        self._render()

    def update_content(
        self,
        result: TraverseResult | None,
        stated_acreage: str = "",
        *,
        expected_open: bool = False,
    ):
        self._result = result
        self._stated_acreage = stated_acreage
        self._expected_open = bool(expected_open)
        self._render()

    # ---------- rendering ----------

    def _render(self):
        r = self._result
        if r is None or not r.segments:
            self.status_label.setText("\u2014")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #9e9e9e;")
            self.body.setHtml(self._units_html(
                "<p style='color:#9e9e9e'>Plot a traverse to see closure details.</p>"))
            return

        tol = load_closure_tolerance()
        complete = not r.errors
        closed = is_traverse_closed(r, tol)
        if closed:
            self.status_label.setText("CLOSED")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #81c784;")
        elif getattr(self, "_expected_open", False):
            self.status_label.setText("OPEN \u2014 expected for this document type")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #ffb74d;")
        else:
            self.status_label.setText("OPEN \u2014 review required")
            self.status_label.setStyleSheet("font-size: 16px; font-weight: 700; color: #e57373;")

        rows = []

        def row(label, value, color=""):
            style = f" style='color:{color}'" if color else ""
            rows.append(f"<tr><td style='padding:2px 14px 2px 0;color:#90a4ae'>{label}</td>"
                        f"<td{style}>{value}</td></tr>")

        if not complete:
            row("Complete", f"no \u2014 {len(r.errors)} call(s) failed (see below)", "#e57373")
        row("Tolerance", f"{tol:.3f} ft <span style='color:#90a4ae'>(Settings)</span>")
        row("\u0394X (misclosure east)", f"{r.misclosure_x:+.4f} ft")
        row("\u0394Y (misclosure north)", f"{r.misclosure_y:+.4f} ft")
        mis_color = (
            "#81c784" if closed
            else ("#ffb74d" if getattr(self, "_expected_open", False) else "#e57373")
        )
        row("Linear misclosure", f"{r.closure_error:.4f} ft"
            + (f" &nbsp;bearing {r.closure_bearing}" if r.closure_bearing else ""),
            mis_color)
        row("Misclosure ratio", r.precision or "n/a")
        row("Perimeter", f"{r.perimeter:,.2f} ft")

        if area_is_reliable(r, tol):
            area_label = "Area" if closed else "Area (approx, open)"
            row(
                area_label,
                f"{r.area_sqft:,.0f} sq ft &nbsp;=&nbsp; {r.area_acres:.4f} acres",
            )
        elif getattr(self, "_expected_open", False):
            row(
                "Area",
                "not applicable (open control/county line \u2014 not a closed tract)",
                "#ffb74d",
            )
        else:
            row("Area", "withheld (open traverse \u2014 area would be unreliable)", "#ffb74d")

        stated = parse_stated_acres(self._stated_acreage) if self._stated_acreage else None
        if stated is not None and stated > 0 and area_is_reliable(r, tol):
            diff_pct = abs(r.area_acres - stated) / stated * 100.0
            ok = diff_pct <= ACREAGE_FLAG_PCT
            row("Deed states", f"{stated:g} acres \u2014 computed differs by {diff_pct:.2f}%"
                + (" \u2713" if ok else " \u26a0 check calls for a misread"),
                "#81c784" if ok else "#ffb74d")

        html = f"<table style='font-size:13px'>{''.join(rows)}</table>"

        if r.warnings:
            items = "".join(f"<li>{w}</li>" for w in r.warnings)
            html += f"<p style='color:#ffb74d;margin-bottom:0'><b>Warnings</b></p><ul>{items}</ul>"
        if r.errors:
            items = "".join(f"<li>{e}</li>" for e in r.errors)
            html += f"<p style='color:#e57373;margin-bottom:0'><b>Failed calls</b></p><ul>{items}</ul>"

        self.body.setHtml(self._units_html(html))

    @staticmethod
    def _units_html(main: str) -> str:
        unit_rows = "".join(
            f"<tr><td style='padding:1px 14px 1px 0;color:#90a4ae'>{name}</td><td>{factor}</td></tr>"
            for name, factor in _UNIT_TABLE
        )
        return (
            f"<html><body style='color:#e0e0e0'>{main}"
            f"<hr style='border-color:#333'>"
            f"<p style='color:#90a4ae;margin-bottom:2px'><b>Unit conversions used</b> "
            f"(all traverse math runs in US survey feet)</p>"
            f"<table style='font-size:12px'>{unit_rows}</table>"
            f"</body></html>"
        )
