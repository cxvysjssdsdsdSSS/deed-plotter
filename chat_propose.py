"""Chat action proposals: gate, split JSON, normalize, apply to workspace."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any

from cogo import (
    Call,
    chord_copied_from_distance,
    curve_can_derive_chord,
    newly_derivable_copied_chord,
    normalize_call_type,
)

_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

# UX-only detector (schema is always in the chat prompt). Strong verbs alone are
# enough; soft phrasing ("make…", "I'd like…") also needs a deed/edit noun so
# advice like "I'd like to understand closure" does not nag.
_STRONG_ACTION_RE = re.compile(
    r"\b(?:change|set|update|fix|edit|insert|delete|remove|mark|"
    r"clear|export|parse|reparse|re-parse|adjust|correct|revise|"
    r"modify|increase|decrease|lengthen|shorten|extend|convert|"
    r"replace|swap|rename|apply|propose|proposal)\b"
    r"|\b(?:into|to)\s+curves?\b"
    r"|\bcurves?\b.*\b(?:call|side|line)s?\b"
    r"|\b(?:call|side|line)s?\b.*\bcurves?\b",
    re.IGNORECASE,
)
_SOFT_ACTION_RE = re.compile(
    r"\b(?:make|add|turn)\b"
    r"|\bi(?:'|\u2019)?d\s+like\b"
    r"|\bi\s+(?:want|would\s+like)\b",
    re.IGNORECASE,
)
_EDIT_NOUN_RE = re.compile(
    r"\b(?:call|calls|side|sides|line|lines|curve|curves|tie|ties|"
    r"distance|bearing|radius|chord|delta|monument|action|actions|"
    r"proposal|proposals|pending|export|parse|dxf|csv|document|"
    r"grantor|grantee|table)\b",
    re.IGNORECASE,
)

_CONFIDENCE_OK = frozenset({"high", "medium", "low", ""})

ACTION_HELP = """\
- update_call: call_index (1-based CURRENT table row), optional fields: bearing,
  distance, units, call_type, curve_direction, radius, arc_length, chord_length,
  delta, monument, description, confidence (high|medium|low|""). For curves:
  Radius plus distance is enough. Leave chord_length empty unless a chord is
  already stated. Do not copy distance into chord_length.
- set_confidence: call_index, confidence
- add_call: optional after_index (0=before first, N=after call N, omit/-1=append);
  fields as update_call. Several adds with the same after_index keep proposal order.
- delete_call: call_index
- update_tie / add_tie / delete_tie: same as update/add/delete_call but use
  tie_index (1-based) for commencement/tie calls in Deed Details
- update_document_info: fields object with any of document_type, county, state,
  date, grantor, grantee, surveyor, surveyor_license, volume_page,
  acreage_stated, basis_of_bearings
- run_parse: {{}} — ask the app to run Parse with AI (user still confirms)
- export_csv: {{}} — ask the app to export CSV (QA warn still applies)
- export_dxf: {{}} — ask the app to export DXF (QA warn still applies)
"""


def wants_deed_proposal(message: str) -> bool:
    """True when the user looks like they want a workspace change or export.

    Used for UX hints when the model skips JSON. The proposal *schema* is
    always injected into chat prompts regardless of this gate.
    """
    q = (message or "").strip()
    if not q:
        return False
    # Pure advice questions ("what is…", "how do I…") — not Apply requests.
    if re.match(r"^(?:what|why|how|when|where|who|which)\b", q, re.IGNORECASE):
        return False
    # "Add more detail…" is elaboration, not a workspace edit.
    if re.search(r"\badd\s+more\b", q, re.IGNORECASE):
        return False
    if _STRONG_ACTION_RE.search(q):
        return True
    return (
        _SOFT_ACTION_RE.search(q) is not None
        and _EDIT_NOUN_RE.search(q) is not None
    )


def chat_propose_prompt_section() -> str:
    return f"""\
=== PROPOSALS (required for edits) ===
You have a structured tool **propose_deed_actions** (preferred — like IDE tool
calls). When the user wants any workspace change — including informal phrasing
like "make the longer sides curves", "fix call 2", "propose an action", or
"export DXF" — you MUST either:
  A) Call propose_deed_actions with {{"actions": [...], "skipped": [...]}}, OR
  B) Append ONE fenced JSON block with the same shape (legacy fallback).

The app only opens the review popup when actions are submitted that way; prose
alone never applies changes.

1. Answer briefly in plain prose first (what you will propose).
2. Then call the tool (preferred) or append:
```json
{{"actions": [{{"action": "update_call", "call_index": 1, "distance": 120.5}}], "skipped": []}}
```

Allowed actions:
{ACTION_HELP}

Rules:
- Advice-only / how-to questions: answer in prose, NO tool call and NO JSON.
- Any request to change calls, ties, document info, re-parse, or export: ALWAYS
  submit actions via the tool or JSON. Do not stop at explaining what you would do.
- Vague targets ("the two longer sides", "those lines"): pick the best-matching
  call_index values from CONTEXT and put uncertainty in "skipped" if needed —
  still emit an actions array when you can identify at least one call.
- Put unclear items in "skipped" with a reason instead of guessing.
- Do NOT claim changes were applied — the app shows a review table; the user must Apply.
- call_index is 1-based from the CURRENT Call Table in CONTEXT (before this batch).
  after_index for add_call is different: 0 = before first, N = after call N, omit/-1
  = append. Several add_call rows with the same after_index insert in proposal
  order after that call. The app applies updates/deletes first, then adds — never
  update_call/delete_call using post-insert numbers. If the table has 4 calls,
  only call_index 1–4 are valid for update/delete. Use add_call to insert new
  courses; do not invent updates for indices that do not exist yet.
- Prefer the smallest change that satisfies the request.
- For curve add/update: do not copy distance into chord_length. Radius plus
  distance is enough; omit chord_length unless the table or deed already
  states a chord.
"""


def split_chat_proposal(text: str) -> tuple[str, dict | None]:
    """Return (prose_for_display, proposal_dict_or_None).

    Accepts a fenced ```json``` block (preferred) or a trailing bare
    ``{"actions": ...}`` object (Health-style fallback when the model
    skips fences), including pretty-printed forms.
    """
    raw = (text or "").strip()
    if not raw:
        return "", None

    candidate = None
    m = _FENCE_RE.search(raw)
    if m:
        candidate = m.group(1)
        prose = (raw[: m.start()] + raw[m.end():]).strip()
    else:
        start = _bare_actions_object_start(raw)
        if start < 0:
            return raw, None
        depth = 0
        end = -1
        for i in range(start, len(raw)):
            ch = raw[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            return raw, None
        candidate = raw[start:end]
        prose = raw[:start].strip()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return raw, None
    if not isinstance(data, dict):
        return prose or raw, None
    actions = data.get("actions")
    if not isinstance(actions, list):
        return prose or raw, None
    skipped = data.get("skipped") if isinstance(data.get("skipped"), list) else []
    return prose or "(Proposed actions — see review.)", {
        "actions": [a for a in actions if isinstance(a, dict)],
        "skipped": skipped,
    }


def _bare_actions_object_start(raw: str) -> int:
    """Index of `{` starting a bare proposal object, or -1.

    Handles compact ``{"actions":`` and pretty-printed ``{\\n  "actions":``.
    """
    key = raw.rfind('"actions"')
    if key < 0:
        return -1
    brace = raw.rfind("{", 0, key)
    if brace < 0:
        return -1
    between = raw[brace + 1:key]
    if any(c not in " \t\r\n" for c in between):
        return -1
    return brace


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def normalize_action(raw: dict) -> dict | None:
    """Return a cleaned action dict or None if invalid."""
    action = str(raw.get("action", "") or "").strip().lower()
    # Allow update_tie etc. by normalizing to the call_* pipeline with tie_index.
    tie_map = {
        "update_tie": "update_call",
        "add_tie": "add_call",
        "delete_tie": "delete_call",
    }
    is_tie = action in tie_map
    if is_tie:
        action = tie_map[action]
        if "tie_index" in raw and "call_index" not in raw:
            raw = {**raw, "call_index": raw.get("tie_index")}

    if action not in (
        "update_call", "set_confidence", "add_call", "delete_call",
        "update_document_info", "run_parse", "export_csv", "export_dxf",
    ):
        return None

    out: dict[str, Any] = {"action": action, "include": True}
    if is_tie:
        out["target"] = "tie"
    else:
        out["target"] = "boundary"

    if action in ("update_call", "set_confidence", "delete_call"):
        try:
            idx = int(raw.get("call_index"))
        except (TypeError, ValueError):
            return None
        if idx < 1:
            return None
        out["call_index"] = idx

    if action == "set_confidence":
        conf = str(raw.get("confidence", "") or "").lower()
        if conf not in _CONFIDENCE_OK:
            return None
        out["confidence"] = conf

    if action in ("update_call", "add_call"):
        for key in (
            "bearing", "units", "call_type", "curve_direction", "delta",
            "monument", "description",
        ):
            if key in raw and raw[key] is not None:
                out[key] = str(raw[key]).strip()
        if "confidence" in raw and raw["confidence"] is not None:
            conf = str(raw["confidence"]).strip().lower()
            if conf not in _CONFIDENCE_OK:
                return None
            out["confidence"] = conf
        for key in ("distance", "radius", "arc_length", "chord_length"):
            if key in raw:
                n = _num(raw.get(key))
                if n is not None:
                    out[key] = n
        if action == "add_call":
            try:
                after = int(raw.get("after_index", -1))
            except (TypeError, ValueError):
                after = -1
            out["after_index"] = after  # -1 = append

    if action == "update_document_info":
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            # Allow flat keys on the action itself.
            fields = {
                k: raw[k] for k in (
                    "document_type", "county", "state", "date", "grantor",
                    "grantee", "surveyor", "surveyor_license", "volume_page",
                    "acreage_stated", "basis_of_bearings",
                ) if k in raw and raw[k] is not None
            }
        cleaned = {str(k): str(v).strip() for k, v in fields.items() if str(v).strip()}
        if not cleaned:
            return None
        out["fields"] = cleaned

    return out


def normalize_proposal(proposal: dict) -> tuple[list[dict], list[str]]:
    actions: list[dict] = []
    skipped: list[str] = []
    for item in proposal.get("skipped") or []:
        if isinstance(item, str):
            skipped.append(item)
        elif isinstance(item, dict):
            skipped.append(str(item.get("reason") or item))
    for raw in proposal.get("actions") or []:
        if not isinstance(raw, dict):
            skipped.append("Skipped non-object action.")
            continue
        norm = normalize_action(raw)
        if norm is None:
            skipped.append(f"Skipped invalid action: {raw.get('action', '?')}")
            continue
        actions.append(norm)
    return actions, skipped


def action_summary(action: dict) -> str:
    a = action.get("action")
    target = action.get("target") or "boundary"
    label = "Tie" if target == "tie" else "Call"
    if a == "update_call":
        fields = [k for k in action if k not in ("action", "include", "call_index", "target")]
        return f"Update {label} {action['call_index']}: {', '.join(fields) or '(no fields)'}"
    if a == "set_confidence":
        return f"Set {label} {action['call_index']} confidence → {action.get('confidence')!r}"
    if a == "add_call":
        where = "append" if action.get("after_index", -1) < 0 else f"after {action['after_index']}"
        return f"Add {'tie' if target == 'tie' else 'call'} ({where})"
    if a == "delete_call":
        return f"Delete {label} {action['call_index']}"
    if a == "update_document_info":
        keys = ", ".join((action.get("fields") or {}).keys())
        return f"Update document info: {keys}"
    if a == "run_parse":
        return "Run Parse with AI"
    if a == "export_csv":
        return "Export CSV"
    if a == "export_dxf":
        return "Export DXF"
    return str(a)


def filter_out_of_range_actions(
    actions: list[dict],
    *,
    n_boundary: int,
    n_ties: int = 0,
) -> tuple[list[dict], list[str]]:
    """Remove update/delete/set_confidence targeting missing rows.

    call_index is validated against the CURRENT table size (before adds in this
    batch). Returns (kept_actions, skip_notes).
    """
    kept: list[dict] = []
    notes: list[str] = []
    n_boundary = max(0, int(n_boundary))
    n_ties = max(0, int(n_ties))
    for act in actions:
        a = act.get("action")
        if a not in ("update_call", "set_confidence", "delete_call"):
            kept.append(act)
            continue
        target = act.get("target") or "boundary"
        n = n_ties if target == "tie" else n_boundary
        label = "Tie" if target == "tie" else "Call"
        try:
            idx = int(act.get("call_index"))
        except (TypeError, ValueError):
            notes.append(f"{a}: missing or invalid {label.lower()} index.")
            continue
        if idx < 1 or idx > n:
            notes.append(
                f"{a}: {label} {idx} out of range "
                f"(table has {n}; use current indices before adds in this batch)."
            )
            continue
        kept.append(act)
    return kept, notes


_INDEXED_ACTIONS = ("update_call", "set_confidence", "add_call", "delete_call")


def indexed_table_target(act: dict) -> str | None:
    """Which table's 1-based indexes this action uses, or None if unindexed."""
    if act.get("action") not in _INDEXED_ACTIONS:
        return None
    return act.get("target") or "boundary"


def remaining_after_add_delete(
    applied: list[dict], remaining: list[dict],
) -> tuple[list[dict], bool]:
    """Drop leftover indexed actions on tables that add/delete shifted.

    Export/parse/document actions are unindexed and always kept. Returns
    (kept_remaining, dropped_any).
    """
    shifted = {
        indexed_table_target(a)
        for a in applied
        if a.get("action") in ("add_call", "delete_call")
    }
    shifted.discard(None)
    kept: list[dict] = []
    dropped = False
    for act in remaining:
        tgt = indexed_table_target(act)
        if tgt is not None and tgt in shifted:
            dropped = True
            continue
        kept.append(act)
    return kept, dropped


def pending_after_apply(
    applied: list[dict],
    remaining: list[dict],
    keep_side: list[dict] | None = None,
) -> tuple[list[dict], bool]:
    """Leftovers after Apply: drop shifted indexes, then prepend failed side rows.

    ``keep_side`` is the export/parse rows that did not succeed (Cancel / fail).
    Those are unindexed and must not be treated as leftovers.
    """
    remaining, dropped = remaining_after_add_delete(applied, remaining)
    if keep_side:
        remaining = list(keep_side) + remaining
    return remaining, dropped


def _act_has_curve_data(act: dict, existing: Call | None = None) -> bool:
    radius = float(act["radius"]) if "radius" in act else (
        float(existing.radius) if existing else 0.0
    )
    arc = float(act["arc_length"]) if "arc_length" in act else (
        float(existing.arc_length) if existing else 0.0
    )
    chord = float(act["chord_length"]) if "chord_length" in act else (
        float(existing.chord_length) if existing else 0.0
    )
    delta = str(act["delta"]) if "delta" in act else (
        existing.delta if existing else ""
    )
    direction = str(act["curve_direction"]) if "curve_direction" in act else (
        existing.curve_direction if existing else ""
    )
    return bool(
        (radius or 0.0) > 0
        or (arc or 0.0) > 0
        or (chord or 0.0) > 0
        or str(delta or "").strip()
        or str(direction or "").strip()
    )


def _resolve_apply_call_type(act: dict, existing: Call | None = None) -> str:
    has_curve = _act_has_curve_data(act, existing)
    if "call_type" in act:
        return normalize_call_type(act.get("call_type") or "", has_curve_data=has_curve)
    if existing is None:
        return normalize_call_type("", has_curve_data=has_curve)
    if has_curve and any(
        k in act for k in (
            "radius", "arc_length", "chord_length", "delta", "curve_direction",
        )
    ):
        return normalize_call_type("", has_curve_data=True)
    return normalize_call_type(existing.call_type, has_curve_data=has_curve)


def apply_call_actions(
    calls: list[Call],
    actions: list[dict],
) -> tuple[list[Call], list[str]]:
    """Apply call-mutating actions. Returns (new_calls, errors).

    Phase order (indices refer to the table at the start of this batch):
    1. update_call / set_confidence
    2. delete_call (high-index first)
    3. add_call
    """
    out = list(calls)
    orig_idx = list(range(1, len(out) + 1))
    errors: list[str] = []
    included = [a for a in actions if a.get("include", True)]
    updates = [a for a in included if a.get("action") in ("update_call", "set_confidence")]
    deletes = [a for a in included if a.get("action") == "delete_call"]
    adds = [a for a in included if a.get("action") == "add_call"]

    def _apply_one(act: dict) -> None:
        nonlocal out, orig_idx
        a = act["action"]
        try:
            if a == "delete_call":
                idx = act["call_index"] - 1
                if not (0 <= idx < len(out)):
                    errors.append(f"delete_call: Call {act['call_index']} out of range")
                    return
                out.pop(idx)
                orig_idx.pop(idx)
            elif a == "set_confidence":
                idx = act["call_index"] - 1
                if not (0 <= idx < len(out)):
                    errors.append(f"set_confidence: Call {act['call_index']} out of range")
                    return
                c = out[idx]
                out[idx] = replace(c, confidence=act.get("confidence", ""))
            elif a == "update_call":
                idx = act["call_index"] - 1
                if not (0 <= idx < len(out)):
                    errors.append(f"update_call: Call {act['call_index']} out of range")
                    return
                c = out[idx]
                call_type = _resolve_apply_call_type(act, c)
                if "bearing" in act:
                    bearing = str(act.get("bearing") or "").strip()
                else:
                    bearing = c.bearing
                dist = float(act["distance"]) if "distance" in act else c.distance
                chord = float(act["chord_length"]) if "chord_length" in act else c.chord_length
                input_kw: dict = {}
                if "distance" in act:
                    input_kw["input_distance"] = ""
                if "radius" in act:
                    input_kw["input_radius"] = ""
                if "arc_length" in act:
                    input_kw["input_arc_length"] = ""
                if "chord_length" in act:
                    input_kw["input_chord_length"] = ""
                if call_type == "line":
                    radius = 0.0
                    arc_length = 0.0
                    chord = 0.0
                    curve_direction = ""
                    delta = ""
                    input_kw["input_radius"] = ""
                    input_kw["input_arc_length"] = ""
                    input_kw["input_chord_length"] = ""
                else:
                    radius = float(act["radius"]) if "radius" in act else c.radius
                    arc_length = float(act["arc_length"]) if "arc_length" in act else c.arc_length
                    curve_direction = str(act.get("curve_direction", c.curve_direction) or "")
                    delta = str(act.get("delta", c.delta) or "")
                    # Dist↔Chord live copy: Dist-only updates sync Chord to the
                    # new Dist. Newly adding R/Δ clears that copy (same as the
                    # table), even when the action repeats chord_length matching
                    # Dist. If R/Δ were already on the row, Dist==Chord is a
                    # stated chord — leave it.
                    derive_now = curve_can_derive_chord(
                        radius, delta, arc_length,
                    )
                    derive_before = curve_can_derive_chord(
                        c.radius, c.delta, c.arc_length,
                    )
                    if derive_now and not derive_before:
                        if (
                            chord_copied_from_distance(dist, chord)
                            or chord_copied_from_distance(c.distance, chord)
                        ):
                            chord = 0.0
                    elif (
                        "chord_length" not in act
                        and not derive_now
                        and "distance" in act
                        and chord_copied_from_distance(c.distance, chord)
                    ):
                        chord = dist
                kept_inputs = (
                    input_kw.get("input_distance", c.input_distance),
                    input_kw.get("input_radius", c.input_radius),
                    input_kw.get("input_arc_length", c.input_arc_length),
                    input_kw.get("input_chord_length", c.input_chord_length),
                )
                if input_kw and not any(kept_inputs):
                    input_kw["input_error"] = ""
                out[idx] = replace(
                    c,
                    call_type=call_type,
                    bearing=bearing,
                    distance=dist,
                    units=str(act.get("units", c.units) or c.units),
                    curve_direction=curve_direction,
                    radius=radius,
                    arc_length=arc_length,
                    chord_bearing=bearing if call_type == "curve" else "",
                    chord_length=chord,
                    delta=delta,
                    monument=str(act.get("monument", c.monument) if "monument" in act else c.monument),
                    description=str(act.get("description", c.description) if "description" in act else c.description),
                    confidence=str(act.get("confidence", c.confidence) if "confidence" in act else c.confidence),
                    **input_kw,
                )
        except Exception as exc:
            errors.append(f"{a}: {exc}")

    for act in updates:
        _apply_one(act)
    for act in sorted(deletes, key=lambda a: int(a.get("call_index", 0)), reverse=True):
        _apply_one(act)

    # Adds: after_index N = after original call N (before this batch).
    prior_anchors: list[int | None] = []
    for act in adds:
        try:
            # Omit/blank bearing stays empty so a curve can use the previous
            # tangent. Do not invent due north (Add Call toolbar is separate).
            bearing = str(act.get("bearing") or "").strip()
            add_dist = float(act["distance"]) if "distance" in act else 0.0
            add_radius = float(act["radius"]) if "radius" in act else 0.0
            add_arc = float(act["arc_length"]) if "arc_length" in act else 0.0
            add_delta = str(act.get("delta") or "")
            if "chord_length" in act:
                add_chord = float(act["chord_length"])
            else:
                add_chord = 0.0
            call_type = _resolve_apply_call_type(act)
            if call_type == "line":
                add_chord = 0.0
            elif newly_derivable_copied_chord(
                curve_can_derive_chord(add_radius, add_delta, add_arc),
                False,
                add_dist,
                add_chord,
            ):
                # Same Dist-then-Radius as update_call: Dist==Chord + R/Δ is
                # an echoed live copy, not a stated chord.
                add_chord = 0.0
            new = Call(
                call_type=call_type,
                bearing=bearing,
                distance=add_dist,
                units=str(act.get("units") or "feet"),
                curve_direction=str(act.get("curve_direction") or ""),
                radius=add_radius,
                arc_length=add_arc,
                chord_bearing=bearing if call_type == "curve" else "",
                chord_length=add_chord,
                delta=add_delta,
                monument=str(act.get("monument") or ""),
                description=str(act.get("description") or ""),
                confidence=str(act.get("confidence") or ""),
            )
            try:
                after = int(act.get("after_index", -1))
            except (TypeError, ValueError):
                after = -1
            # after_index is 1-based on the CURRENT table before this batch
            # (same as call_index). Deletes do not renumber it.
            if after < 0:
                out.append(new)
                orig_idx.append(None)
                prior_anchors.append(None)
            else:
                base = sum(1 for o in orig_idx if o is not None and o <= after)
                shift = sum(
                    1 for pa in prior_anchors
                    if pa is not None and pa <= after
                )
                pos = base + shift
                out.insert(pos, new)
                orig_idx.insert(pos, None)
                prior_anchors.append(after)
        except Exception as exc:
            errors.append(f"add_call: {exc}")
    return out, errors
