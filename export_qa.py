"""Pre-export QA checks: open traverse and low-confidence calls."""

from __future__ import annotations

from cogo import Call, TraverseResult
from closure_panel import is_traverse_closed
from parse_structure_qa import looks_like_open_line_survey


def collect_export_warnings(
    calls: list[Call],
    result: TraverseResult | None,
    *,
    tolerance_ft: float,
    document_info: dict | None = None,
) -> list[str]:
    """Return human-readable warnings; empty list means export looks clean."""
    warnings: list[str] = []
    if not calls:
        return warnings

    low = [i for i, c in enumerate(calls, start=1) if c.confidence.lower() == "low"]
    if low:
        listed = ", ".join(f"Call {i}" for i in low[:12])
        extra = f" (+{len(low) - 12} more)" if len(low) > 12 else ""
        warnings.append(f"Low-confidence call(s): {listed}{extra}")

    if result is None or not result.segments:
        warnings.append("Traverse has not been plotted — closure is unknown.")
        return warnings

    if result.errors:
        warnings.append(f"{len(result.errors)} call(s) failed geometry:")
        for err in result.errors[:6]:
            warnings.append(f"  • {err}")
        if len(result.errors) > 6:
            warnings.append(f"  • …and {len(result.errors) - 6} more")

    open_traverse = not is_traverse_closed(result, tolerance_ft)
    if open_traverse:
        if looks_like_open_line_survey(document_info):
            line = (
                "Open control/county line (based on document type) — "
                f"misclosure {result.closure_error:.2f} ft is expected; "
                "polyline export is normal."
            )
        else:
            line = (
                f"Traverse is OPEN — misclosure {result.closure_error:.2f} ft "
                f"(tolerance {tolerance_ft:.3f} ft)"
            )
            if result.closure_bearing:
                line += f", bearing {result.closure_bearing}"
        warnings.append(line)

    return warnings
