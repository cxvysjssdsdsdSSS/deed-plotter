"""Dry-run closure for chat action proposals before Apply."""

from __future__ import annotations

from dataclasses import dataclass, field

from chat_propose import apply_call_actions
from cogo import Call, compute_traverse
from closure_panel import is_traverse_closed, load_closure_tolerance
from parse_structure_qa import looks_like_open_line_survey

_BOUNDARY_MUTATORS = frozenset({
    "update_call", "set_confidence", "add_call", "delete_call",
})


@dataclass
class ProposalClosurePreview:
    """Result of applying included actions to a copy of the call table."""

    status: str = "N/A"  # CLOSED | OPEN | OPEN (expected) | N/A | ERROR
    misclosure_ft: float = 0.0
    misclosure_bearing: str = ""
    n_calls_after: int = 0
    apply_errors: list[str] = field(default_factory=list)
    expected_open: bool = False
    has_boundary_edits: bool = False

    @property
    def summary(self) -> str:
        if not self.has_boundary_edits:
            return (
                "Dry-run closure: unchanged — no boundary call edits in the "
                "current selection."
            )
        if self.status == "ERROR":
            errs = "; ".join(self.apply_errors[:3]) or "apply failed"
            return f"Dry-run closure: could not build a traverse ({errs})."

        line = (
            f"Dry-run closure: {self.status} — "
            f"misclosure {self.misclosure_ft:.3f} ft"
        )
        if self.misclosure_bearing:
            line += f" {self.misclosure_bearing}"
        line += (
            f" ({self.n_calls_after} call"
            f"{'' if self.n_calls_after == 1 else 's'})"
        )
        if self.apply_errors:
            line += " | apply notes: " + "; ".join(self.apply_errors[:2])
        if self.status == "OPEN" and not self.expected_open:
            line += " — figure would still be OPEN after Apply"
        elif self.status == "CLOSED":
            line += " — would close within tolerance"
        elif self.expected_open:
            line += " — open is expected for this document type"
        return line


def preview_proposal_closure(
    actions: list[dict],
    *,
    calls: list[Call],
    document_info: dict | None = None,
    tolerance_ft: float | None = None,
) -> ProposalClosurePreview:
    """Apply included boundary actions to a copy of *calls* and compute closure.

    Does not mutate the workspace. Tie / export actions are ignored for the
    traverse (boundary closure only). Included ``update_document_info`` fields
    are merged into the open-line check so a same-batch type change is honored.
    """
    included = [a for a in actions if a.get("include", True)]
    boundary = [
        a for a in included
        if a.get("action") in _BOUNDARY_MUTATORS and a.get("target") != "tie"
    ]
    if not boundary:
        return ProposalClosurePreview(has_boundary_edits=False)

    tol = (
        float(tolerance_ft)
        if tolerance_ft is not None
        else load_closure_tolerance()
    )
    info = dict(document_info or {})
    for act in included:
        if act.get("action") == "update_document_info":
            fields = act.get("fields")
            if isinstance(fields, dict):
                info.update(fields)
    expected_open = looks_like_open_line_survey(info)
    new_calls, errs = apply_call_actions(list(calls), boundary)
    preview = ProposalClosurePreview(
        has_boundary_edits=True,
        apply_errors=list(errs),
        n_calls_after=len(new_calls),
        expected_open=expected_open,
    )
    if not new_calls:
        preview.status = "ERROR"
        if not preview.apply_errors:
            preview.apply_errors = ["no boundary calls remain after proposed actions"]
        return preview

    result = compute_traverse(new_calls)
    preview.misclosure_ft = float(result.closure_error)
    preview.misclosure_bearing = str(result.closure_bearing or "")
    if result.errors and not result.segments:
        preview.status = "ERROR"
        preview.apply_errors = list(preview.apply_errors) + list(result.errors[:3])
        return preview

    closed = is_traverse_closed(result, tol)
    if expected_open and not closed:
        preview.status = "OPEN (expected)"
    elif closed:
        preview.status = "CLOSED"
    else:
        preview.status = "OPEN"
    return preview
