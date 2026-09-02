"""Incremental per-page AI parse cache.

Stores one partial result per page so a multi-page parse can resume after
timeout/cancel without re-sending completed pages.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from cogo import Call

CACHE_DIR = Path.home() / ".deed_plotter" / "parse_cache"

# Bump when the parse prompt/schema changes so stale mid-run caches from an
# older prompt are not resumed into a mixed result.
PARSE_SCHEMA_VERSION = "4"

# Default max mid-run / resumable sessions kept (no time expiry). Overridable
# from Settings → parse_cache_max.
DEFAULT_PARSE_CACHE_MAX = 8

_CALL_FIELDS = {f.name for f in dataclasses.fields(Call)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def page_bytes_hash(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()[:16]


def make_cache_id(
    *,
    source_path: str,
    model: str,
    image_quality: str,
    extra_text: str,
    page_indices: list[int],
    page_hashes: list[str],
) -> str:
    """Stable id for this document + settings + selected pages + user text.

    ``extra_text`` is the combined paste-box + Settings always-append string
    sent with the parse (empty when neither is set).
    """
    payload = "\n".join(
        [
            PARSE_SCHEMA_VERSION,
            source_path or "",
            model or "",
            image_quality or "",
            (extra_text or "").strip(),
            ",".join(str(i) for i in page_indices),
            ",".join(page_hashes),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def prune_sessions(
    max_entries: int = DEFAULT_PARSE_CACHE_MAX,
    *,
    keep_ids: set[str] | frozenset[str] | None = None,
) -> None:
    """Keep the newest *max_entries* sessions by updated_at (perpetual, no TTL).

    *keep_ids* are never deleted (e.g. the session about to be resumed), even if
    that briefly exceeds *max_entries*.
    """
    must_keep = set(keep_ids or ())
    limit = max(1, int(max_entries))
    try:
        entries = list(CACHE_DIR.glob("*.json"))
    except OSError:
        return

    ranked: list[tuple[str, Path]] = []
    for path in entries:
        try:
            saved = ""
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    saved = str(
                        data.get("updated_at") or data.get("created_at") or ""
                    )
            except (OSError, json.JSONDecodeError, UnicodeError):
                saved = ""
            if not saved:
                saved = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).isoformat(timespec="seconds")
            ranked.append((saved, path))
        except OSError:
            continue

    ranked.sort(key=lambda t: t[0], reverse=True)
    selected: set[str] = set(must_keep)
    for _saved, path in ranked:
        if path.stem in selected:
            continue
        if len(selected) >= limit:
            break
        selected.add(path.stem)

    for _saved, path in ranked:
        if path.stem in selected:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def prune_stale_sessions(
    max_entries: int = DEFAULT_PARSE_CACHE_MAX,
    *,
    keep_ids: set[str] | frozenset[str] | None = None,
) -> None:
    """Backward-compatible alias for :func:`prune_sessions` (no time-based expiry)."""
    prune_sessions(max_entries, keep_ids=keep_ids)


def cache_path(cache_id: str) -> Path:
    return CACHE_DIR / f"{cache_id}.json"


def load_session(cache_id: str) -> dict | None:
    path = cache_path(cache_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_session(session: dict) -> None:
    cache_id = session.get("cache_id")
    if not cache_id:
        raise ValueError("session missing cache_id")
    session = dict(session)
    session["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _atomic_write(cache_path(cache_id), json.dumps(session, indent=1))


def new_session(
    *,
    cache_id: str,
    source_name: str,
    source_path: str,
    model: str,
    page_indices: list[int],
    page_hashes: list[str],
    extra_text: str = "",
    table_fingerprint: str = "",
) -> dict:
    pages = {
        str(idx): {
            "page_index": idx,
            "page_hash": page_hashes[i],
            "status": "pending",
            "result": None,
            "error": "",
        }
        for i, idx in enumerate(page_indices)
    }
    return {
        "cache_id": cache_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at": "",
        "source_name": source_name,
        "source_path": source_path,
        "model": model,
        "extra_text": extra_text,
        "page_indices": list(page_indices),
        "pages": pages,
        "status": "in_progress",
        "table_fingerprint": str(table_fingerprint or ""),
    }


def get_page_entry(session: dict, page_index: int) -> dict | None:
    pages = session.get("pages") or {}
    entry = pages.get(str(page_index))
    return entry if isinstance(entry, dict) else None


def mark_page_done(session: dict, page_index: int, result: dict) -> None:
    entry = get_page_entry(session, page_index)
    if entry is None:
        raise KeyError(f"page {page_index} not in session")
    payload = _serialize_result(result)
    entry["status"] = "done"
    entry["result"] = payload
    entry["error"] = ""
    save_session(session)


def mark_page_failed(session: dict, page_index: int, error: str) -> None:
    entry = get_page_entry(session, page_index)
    if entry is None:
        raise KeyError(f"page {page_index} not in session")
    entry["status"] = "failed"
    entry["error"] = error
    save_session(session)


def pending_page_indices(session: dict) -> list[int]:
    out = []
    for idx in session.get("page_indices") or []:
        entry = get_page_entry(session, int(idx))
        if entry is None or entry.get("status") != "done":
            out.append(int(idx))
    return out


def done_page_results(session: dict) -> list[tuple[int, dict]]:
    """Return (page_index, result_with_Call_objects) for completed pages, in order."""
    out: list[tuple[int, dict]] = []
    for idx in session.get("page_indices") or []:
        entry = get_page_entry(session, int(idx))
        if not entry or entry.get("status") != "done" or not entry.get("result"):
            continue
        out.append((int(idx), _deserialize_result(entry["result"])))
    return out


def clear_session(cache_id: str) -> None:
    path = cache_path(cache_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _serialize_result(result: dict) -> dict:
    payload = dict(result)
    payload["calls"] = [dataclasses.asdict(c) for c in result.get("calls", [])]
    payload["tie_calls"] = [dataclasses.asdict(c) for c in result.get("tie_calls", [])]
    return payload


def _dicts_to_calls(raw: list) -> list[Call]:
    calls = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            calls.append(Call(**{k: v for k, v in c.items() if k in _CALL_FIELDS}))
        except TypeError:
            continue
    return calls


def _deserialize_result(raw: dict) -> dict:
    result = dict(raw)
    result["calls"] = _dicts_to_calls(result.get("calls") or [])
    result["tie_calls"] = _dicts_to_calls(result.get("tie_calls") or [])
    result.setdefault("parse_warnings", [])
    return result


def _join_text(parts: list[str]) -> str:
    cleaned = [p.strip() for p in parts if (p or "").strip()]
    if not cleaned:
        return ""
    # Avoid duplicating identical blocks when a page re-states prior text.
    unique: list[str] = []
    for part in cleaned:
        if unique and (part in unique[-1] or unique[-1] in part):
            if len(part) > len(unique[-1]):
                unique[-1] = part
            continue
        unique.append(part)
    return "\n\n".join(unique)


def _merge_info(base: dict, extra: dict) -> dict:
    out = dict(base or {})
    for k, v in (extra or {}).items():
        if v is None or v == "":
            continue
        if not out.get(k):
            out[k] = str(v)
    return out


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _call_fingerprint(call: Call) -> str:
    return "|".join(
        [
            (call.call_type or "").lower(),
            re.sub(r"\s+", " ", (call.bearing or "").strip().upper()),
            f"{_num(call.distance):.4f}",
            (call.units or "").lower(),
            (call.curve_direction or "").lower(),
            f"{_num(call.radius):.4f}",
            f"{_num(call.arc_length):.4f}",
            f"{_num(call.chord_length):.4f}",
            re.sub(r"\s+", " ", (call.delta or "").strip().upper()),
        ]
    )


# How many trailing calls of the previous page to compare against the leading
# calls of the next page when trimming scan-overlap duplicates at the seam.
_SEAM_OVERLAP_WINDOW = 3


def _trim_seam_overlap(prev_calls: list[Call], next_calls: list[Call]) -> list[Call]:
    """Drop next-page leading calls that repeat the previous page's tail.

    Scanned pages sometimes overlap, so the AI can re-emit the last call(s) of
    page N at the top of page N+1. Only the seam is checked — identical calls
    elsewhere are legitimate deed geometry (jogs, parallel offsets) and kept.
    """
    if not prev_calls or not next_calls:
        return list(next_calls)
    tail = [_call_fingerprint(c) for c in prev_calls[-_SEAM_OVERLAP_WINDOW:]]
    # Find the longest suffix of `tail` matching the head of next_calls.
    max_k = min(len(tail), len(next_calls))
    for k in range(max_k, 0, -1):
        head = [_call_fingerprint(c) for c in next_calls[:k]]
        if tail[-k:] == head:
            return list(next_calls[k:])
    return list(next_calls)


def merge_page_results(page_results: list[tuple[int, dict]]) -> dict:
    """Combine per-page parse results into one deed result (document order)."""
    calls: list[Call] = []
    tie_calls: list[Call] = []
    legal_parts: list[str] = []
    note_parts: list[str] = []
    warnings: list[str] = []
    document_info: dict = {}
    point_of_beginning = ""
    pob_monument = ""
    pob_coordinates = None

    for page_index, result in page_results:
        legal = (result.get("legal_description") or "").strip()
        if legal:
            legal_parts.append(legal)
        notes = (result.get("general_notes") or "").strip()
        if notes:
            note_parts.append(f"[Page {page_index + 1}] {notes}")
        for w in result.get("parse_warnings") or []:
            tagged = f"[Page {page_index + 1}] {w}"
            if tagged not in warnings:
                warnings.append(tagged)
        document_info = _merge_info(document_info, result.get("document_info") or {})
        if not point_of_beginning and result.get("point_of_beginning"):
            point_of_beginning = result["point_of_beginning"]
        if not pob_monument and result.get("pob_monument"):
            pob_monument = result["pob_monument"]
        if pob_coordinates is None and result.get("pob_coordinates"):
            pob_coordinates = result["pob_coordinates"]

        page_ties = _trim_seam_overlap(tie_calls, list(result.get("tie_calls") or []))
        tie_calls.extend(page_ties)
        page_calls = _trim_seam_overlap(calls, list(result.get("calls") or []))
        if len(page_calls) < len(result.get("calls") or []):
            dropped = len(result.get("calls") or []) - len(page_calls)
            warnings.append(
                f"[Page {page_index + 1}] Dropped {dropped} call(s) repeated "
                "from the previous page (scan overlap)."
            )
        calls.extend(page_calls)

    from parse_structure_qa import (
        apply_stub_and_open_line_finalize,
        warn_possible_misfiled_boundary_tie,
    )

    legal_description = _join_text(legal_parts)
    notes = _join_text(note_parts)
    calls, tie_calls, notes, warnings = apply_stub_and_open_line_finalize(
        calls=calls,
        tie_calls=tie_calls,
        document_info=document_info,
        general_notes=notes,
        warnings=warnings,
        document_finalize=True,
    )
    for w in warn_possible_misfiled_boundary_tie(
        legal_description, calls, tie_calls, document_info=document_info,
    ):
        if w not in warnings:
            warnings.append(w)

    return {
        "calls": calls,
        "tie_calls": tie_calls,
        "document_info": document_info,
        "legal_description": legal_description,
        "point_of_beginning": point_of_beginning,
        "pob_coordinates": pob_coordinates,
        "pob_monument": pob_monument,
        "general_notes": notes,
        "parse_warnings": warnings,
    }


def _format_call_line(seq: int, c: Call) -> str:
    brg = c.chord_bearing or c.bearing
    dist = c.chord_length if c.call_type == "curve" and c.chord_length else c.distance
    bits = [f"  {seq}. {c.call_type} {brg} {dist} {c.units}"]
    if c.call_type == "curve" and c.curve_direction:
        bits.append(f"({c.curve_direction})")
    return " ".join(bits)


def prior_context_summary(session: dict) -> str:
    """Compact rolling summary of already-cached pages for the next page prompt.

    Bounded regardless of page count: totals + POB + ties + the LAST few calls
    (continuity matters most at the seam) + the tail of the legal text.
    """
    done = done_page_results(session)
    if not done:
        return ""

    all_calls: list[Call] = []
    all_ties: list[Call] = []
    pob = ""
    legal_tail = ""
    pages_seen: list[int] = []
    for page_index, result in done:
        pages_seen.append(page_index + 1)
        all_calls.extend(result.get("calls") or [])
        all_ties.extend(result.get("tie_calls") or [])
        if not pob and result.get("point_of_beginning"):
            pob = str(result["point_of_beginning"]).strip()
        legal = (result.get("legal_description") or "").strip()
        if legal:
            legal_tail = legal

    lines = [
        f"Pages already parsed: {', '.join(str(p) for p in pages_seen)}.",
        f"Boundary calls so far: {len(all_calls)}; tie calls so far: {len(all_ties)}.",
    ]
    if pob:
        lines.append(f"POB (already found — do not re-emit): {pob}")
    if all_ties:
        tie_preview = [_format_call_line(i, c) for i, c in enumerate(all_ties[:6], 1)]
        lines.append("Tie calls so far:\n" + "\n".join(tie_preview))
    if all_calls:
        last = all_calls[-8:]
        first_seq = len(all_calls) - len(last) + 1
        preview = [
            _format_call_line(first_seq + i, c) for i, c in enumerate(last)
        ]
        label = "Most recent boundary calls (continue after the last one):"
        if len(all_calls) > len(last):
            label = (
                f"Most recent boundary calls ({len(all_calls) - len(last)} "
                "earlier calls omitted — continue after the last one):"
            )
        lines.append(label + "\n" + "\n".join(preview))
    if legal_tail:
        if len(legal_tail) > 600:
            legal_tail = "…" + legal_tail[-600:]
        lines.append(f"End of legal text so far:\n{legal_tail}")
    return "\n".join(lines)
