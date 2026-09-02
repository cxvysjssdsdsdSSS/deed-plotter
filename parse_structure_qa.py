"""Structural QA on AI parse results.

Mostly soft warnings. One deterministic rewrite is allowed: drop junk stub
calls (blank bearing and zero distance) with a salvage warning/notes line.
"""

from __future__ import annotations

import re

from cogo import Call, compute_traverse, parse_bearing

_THENCE_RE = re.compile(r"\bthence\b", re.IGNORECASE)
_BEARING_TOL_DEG = 2.0
_LENGTH_REL_TOL = 0.02  # 2%
_LENGTH_ABS_TOL_FT = 2.0
_DIST_EPS = 1e-9

# document_type phrases (lowercase) for open-line / control surveys.
# Matched with word boundaries so "road survey" does not hit "Railroad Survey".
_OPEN_LINE_TYPE_PHRASES = (
    "survey report",
    "county line survey",
    "county boundary survey",
    "county boundary line",
    "control line",
    "road survey",
    "state line survey",
    "meander line survey",
)
_OPEN_LINE_TYPE_RES = tuple(
    re.compile(rf"\b{re.escape(p)}\b") for p in _OPEN_LINE_TYPE_PHRASES
)
# Easement / warranty titles stay hard-OPEN even if they mention a control line.
_OPEN_LINE_BLOCK_RE = re.compile(r"\b(easement|warranty)\b")

_MISSING_DIST_WARN_RE = re.compile(
    r"^(?:Tie[ -]?[Cc]all|Call) (\d+): distance missing/unreadable",
    re.IGNORECASE,
)

OPEN_LINE_PARSE_WARNING = (
    "This document type looks like an open control/county/road line survey "
    "(not a closed tract). Closure and area are not meaningful for this class."
)


def count_thence(legal_description: str) -> int:
    return len(_THENCE_RE.findall(legal_description or ""))


def _bearing_delta_deg(a: str, b: str) -> float | None:
    try:
        az_a = parse_bearing(a)
        az_b = parse_bearing(b)
    except ValueError:
        return None
    d = abs(az_a - az_b) % 360.0
    return min(d, 360.0 - d)


def looks_like_open_line_survey(document_info: dict | None) -> bool:
    """True when document_type looks like an open control/county/road line survey.

    Keyword match on document_type only — never on general_notes (deeds often
    say \"along the county line\"). Deed-like types stay False so OPEN stays alarming.
    """
    info = document_info or {}
    dtype = str(info.get("document_type") or "").strip().lower()
    if not dtype:
        return False
    if _OPEN_LINE_BLOCK_RE.search(dtype):
        return False
    return any(rx.search(dtype) for rx in _OPEN_LINE_TYPE_RES)


def is_junk_stub_line(call: Call) -> bool:
    """Line with no direction and no distance — not plottable."""
    if (call.call_type or "line").lower() != "line":
        return False
    brg = (call.bearing or "").strip()
    chord = (call.chord_bearing or "").strip()
    if brg or chord:
        return False
    try:
        dist = float(call.distance)
    except (TypeError, ValueError):
        dist = 0.0
    return abs(dist) <= _DIST_EPS


def drop_junk_stub_calls(
    calls: list[Call],
    *,
    warnings: list[str] | None = None,
    general_notes: str = "",
    label: str = "call",
) -> tuple[list[Call], list[str], str]:
    """Remove blank-bearing + zero-distance line stubs; salvage text to notes.

    Returns (kept_calls, extra_warnings, updated_general_notes).
    Also strips orphaned distance-missing warnings for dropped indices when
    *warnings* is provided (mutated in place).
    """
    kept: list[Call] = []
    dropped_idxs: list[int] = []
    salvage: list[str] = []
    for i, c in enumerate(calls, start=1):
        if is_junk_stub_line(c):
            dropped_idxs.append(i)
            bits = []
            if (c.description or "").strip():
                bits.append(c.description.strip())
            if (c.monument or "").strip():
                bits.append(f"monument: {c.monument.strip()}")
            if bits:
                salvage.append(f"Dropped stub {label} {i}: " + " — ".join(bits))
        else:
            kept.append(c)

    extra: list[str] = []
    if dropped_idxs:
        extra.append(
            f"Dropped {len(dropped_idxs)} junk stub {label}(s) "
            f"(no bearing and no distance)."
        )
        if warnings is not None:
            _strip_orphan_missing_distance_warnings(
                warnings, set(dropped_idxs), label=label,
            )

    notes = (general_notes or "").strip()
    if salvage:
        block = "\n".join(salvage)
        notes = f"{notes}\n{block}".strip() if notes else block
    return kept, extra, notes


def _strip_orphan_missing_distance_warnings(
    warnings: list[str],
    dropped_idxs: set[int],
    *,
    label: str,
) -> None:
    """Remove distance-missing warnings for dropped stub indices.

    Boundary stubs emit \"Call N:\"; tie stubs emit \"Tie call N:\" (see
    call_from_ai_dict). Only strip the flavor that matches *label*.
    """
    want_tie = label.lower().startswith("tie")
    keep: list[str] = []
    for w in warnings:
        bare = w
        if bare.startswith("[Page ") and "] " in bare:
            bare = bare.split("] ", 1)[1]
        m = _MISSING_DIST_WARN_RE.match(bare)
        if m and int(m.group(1)) in dropped_idxs:
            is_tie_warn = bare.lower().startswith("tie")
            if is_tie_warn == want_tie:
                continue
        keep.append(w)
    warnings[:] = keep


def warn_if_open_line_survey(document_info: dict | None) -> list[str]:
    if not looks_like_open_line_survey(document_info):
        return []
    return [OPEN_LINE_PARSE_WARNING]


def sync_open_line_parse_warnings(
    warnings: list[str] | None,
    document_info: dict | None,
) -> list[str]:
    """Add/remove the open-line parse warning to match live document_type."""
    out = list(warnings or [])
    has = any(OPEN_LINE_PARSE_WARNING in w for w in out)
    want = looks_like_open_line_survey(document_info)
    if want and not has:
        out.append(OPEN_LINE_PARSE_WARNING)
    elif not want and has:
        out = [w for w in out if OPEN_LINE_PARSE_WARNING not in w]
    return out


def warn_possible_misfiled_boundary_tie(
    legal_description: str,
    calls: list[Call],
    tie_calls: list[Call],
    *,
    document_info: dict | None = None,
) -> list[str]:
    """Flag when a sole tie looks like a missing boundary side.

    Triggers when:
    - legal THENCE count matches len(calls) + len(ties)
    - exactly one tie call
    - boundary misclosure ≈ that tie's length
    - misclosure bearing ≈ tie bearing

    Skipped for open-line survey types (advice fights expected-open UX).
    Does not move or delete calls — warning only for Notes.
    """
    if looks_like_open_line_survey(document_info):
        return []
    if len(tie_calls) != 1 or not calls:
        return []
    thence_n = count_thence(legal_description)
    if thence_n == 0:
        return []
    if thence_n != len(calls) + len(tie_calls):
        return []

    boundary = compute_traverse(calls)
    if not boundary.segments or boundary.closure_error < 1.0:
        return []

    tie = compute_traverse(tie_calls)
    if not tie.segments:
        return []
    tie_len = tie.perimeter
    if tie_len <= 0:
        return []

    abs_err = abs(boundary.closure_error - tie_len)
    rel = abs_err / tie_len
    if abs_err > _LENGTH_ABS_TOL_FT and rel > _LENGTH_REL_TOL:
        return []

    if not boundary.closure_bearing:
        return []
    tie_brg = tie_calls[0].chord_bearing or tie_calls[0].bearing
    if not tie_brg:
        return []
    delta = _bearing_delta_deg(boundary.closure_bearing, tie_brg)
    if delta is None or delta > _BEARING_TOL_DEG:
        return []

    return [
        "Possible misfiled boundary side: the single tie matches the boundary "
        f"misclosure ({boundary.closure_error:,.1f} ft, {boundary.closure_bearing}). "
        "If the deed closes back to the commencement / place of beginning, move "
        "that tie into the call table as the first course and clear ties."
    ]


def apply_stub_and_open_line_finalize(
    *,
    calls: list[Call],
    tie_calls: list[Call],
    document_info: dict | None,
    general_notes: str,
    warnings: list[str],
    document_finalize: bool,
) -> tuple[list[Call], list[Call], str, list[str]]:
    """Stub-drop always; open-line warn only when *document_finalize*."""
    notes = general_notes or ""
    calls, extra_c, notes = drop_junk_stub_calls(
        calls, warnings=warnings, general_notes=notes, label="call",
    )
    warnings.extend(extra_c)
    tie_calls, extra_t, notes = drop_junk_stub_calls(
        tie_calls, warnings=warnings, general_notes=notes, label="tie call",
    )
    warnings.extend(extra_t)
    if document_finalize:
        for w in warn_if_open_line_survey(document_info):
            if w not in warnings:
                warnings.append(w)
    return calls, tie_calls, notes, warnings
