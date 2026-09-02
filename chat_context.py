"""Build deed + app-help context for the assistant chat."""

from __future__ import annotations

from cogo import (
    Call,
    TraverseResult,
    chord_copied_from_distance,
    curve_can_derive_chord,
)
from closure_panel import area_is_reliable, is_traverse_closed, load_closure_tolerance
from parse_structure_qa import looks_like_open_line_survey

APP_HELP = """\
Deed Plotter — how to operate:
- Open Deed…: load a PDF or image into the left viewer.
- Pages…: choose which pages are sent to AI Parse (uncheck covers/plats).
  Parse runs one API call per page and caches each page so a timeout can resume.
- Parse with AI (Ctrl+R): extract metes-and-bounds into the Call Table
  (works for deeds and similar course-and-distance sources).
- Call Table: edit bearings/distances; double-click # or Edit Call… for the editor.
- Plot / Closure: live traverse and OPEN/CLOSED vs tolerance.
- Notes & Warnings: click Call N links to jump to that row.
- Export CSV / Export DXF: hand off to CAD (warns if OPEN or low-confidence).
- Save Job… / Open Job…: persist the edited workspace (.dpjob).
- History…: reload a past raw AI parse snapshot (not hand edits).
- Settings…: Cursor API key, model, always-append notes for every parse.
- Undo (Ctrl+Z) / Redo (Ctrl+Y): call-table, tie-table, and Chat Apply edits.
- Chat (right pane): ask about the current document or request table/export changes
  (review before apply). Toggle with Chat Pane / Ctrl+Shift+C.
"""


def _fmt_call(i: int, c: Call) -> str:
    brg = c.chord_bearing or c.bearing
    bits = [f"{i}. {c.call_type}", brg, f"{c.distance:g} {c.units}".strip()]
    if c.call_type == "curve":
        extra = []
        if c.radius:
            extra.append(f"R={c.radius:g}")
        if c.arc_length:
            extra.append(f"L={c.arc_length:g}")
        if c.chord_length:
            copied = chord_copied_from_distance(c.distance, c.chord_length)
            stated = curve_can_derive_chord(c.radius, c.delta, c.arc_length)
            if not copied or stated:
                extra.append(f"C={c.chord_length:g}")
        if c.delta:
            extra.append(f"Δ={c.delta}")
        if c.curve_direction:
            extra.append(c.curve_direction)
        if extra:
            bits.append("(" + ", ".join(extra) + ")")
    if c.monument:
        bits.append(f"mon={c.monument}")
    if c.confidence:
        bits.append(f"[{c.confidence}]")
    if c.description:
        bits.append(f"— {c.description}")
    return " ".join(x for x in bits if x)


def build_deed_context(
    *,
    source_name: str = "",
    document_info: dict | None = None,
    point_of_beginning: str = "",
    pob_monument: str = "",
    general_notes: str = "",
    parse_warnings: list[str] | None = None,
    legal_description: str = "",
    calls: list[Call] | None = None,
    tie_calls: list[Call] | None = None,
    result: TraverseResult | None = None,
) -> str:
    """Compact text dump of the current workspace for the chat agent."""
    info = document_info or {}
    open_line = looks_like_open_line_survey(info)
    parts: list[str] = [APP_HELP, "", "=== CURRENT WORKSPACE ==="]
    if source_name:
        parts.append(f"Source: {source_name}")
    if open_line:
        parts.append(
            "Note: document_type indicates an open control/county/road line survey "
            "(not a closed tract). Misclosure and area are not meaningful for this class."
        )
    if info:
        parts.append("Document info:")
        for k, v in info.items():
            if v:
                parts.append(f"  {k}: {v}")
    if point_of_beginning:
        parts.append(f"POB: {point_of_beginning}")
    if pob_monument:
        parts.append(f"POB monument: {pob_monument}")
    if general_notes:
        parts.append(f"AI notes: {general_notes}")
    if parse_warnings:
        parts.append("Parse warnings:")
        for w in parse_warnings:
            parts.append(f"  - {w}")

    ties = tie_calls or []
    if ties:
        parts.append(f"Tie calls ({len(ties)}):")
        for i, c in enumerate(ties, start=1):
            parts.append("  " + _fmt_call(i, c))
    else:
        parts.append("Tie calls: (none)")

    calls = calls or []
    if calls:
        parts.append(f"Boundary calls ({len(calls)}):")
        for i, c in enumerate(calls, start=1):
            parts.append("  " + _fmt_call(i, c))
    else:
        parts.append("Boundary calls: (empty — parse a document or import CSV first)")

    if result is not None and result.segments:
        tol = load_closure_tolerance()
        closed = is_traverse_closed(result, tol)
        if open_line and not closed:
            area_bit = "area not applicable (open line)"
            status = "OPEN (expected for this document type)"
        else:
            area_bit = (
                f"area {result.area_acres:.4f} ac"
                if area_is_reliable(result, tol)
                else "area withheld (open)"
            )
            status = "CLOSED" if closed else "OPEN"
        parts.append(
            f"Closure: {status} | "
            f"misclosure {result.closure_error:.2f} ft"
            + (f" {result.closure_bearing}" if result.closure_bearing else "")
            + f" | precision {result.precision} | "
            f"perimeter {result.perimeter:,.2f} ft | "
            f"{area_bit} | tolerance {tol:.3f} ft"
        )
        if result.errors:
            parts.append("Geometry errors:")
            for e in result.errors:
                parts.append(f"  - {e}")
        if result.warnings:
            parts.append("Traverse warnings:")
            for w in result.warnings:
                parts.append(f"  - {w}")
    else:
        parts.append("Closure: (not plotted)")

    legal = (legal_description or "").strip()
    if legal:
        # Cap so the prompt stays usable.
        if len(legal) > 4000:
            legal = legal[:4000] + "\n…[truncated]"
        parts.append("Legal description (transcribed):")
        parts.append(legal)

    return "\n".join(parts)
