"""Persist unapplied chat action proposals for later review.

Deed-specific: call/tie indices are 1-based against the current tables, so
pending is cleared when the traverse shape/content changes (parse, history,
clear, …) or when the stored fingerprint no longer matches.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Sequence

DATA_DIR = Path.home() / ".deed_plotter"
PENDING_FILE = DATA_DIR / "pending_chat_actions.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _call_fingerprint_row(call: Any) -> str:
    """Stable per-row identity for pending index safety (not a full serialize)."""
    get = (lambda k, default="": getattr(call, k, default)) if not isinstance(call, dict) else (
        lambda k, default="": call.get(k, default)
    )
    parts = [
        str(get("bearing") or ""),
        str(get("chord_bearing") or ""),
        str(get("distance") or ""),
        str(get("units") or ""),
        str(get("call_type") or ""),
        str(get("curve_direction") or ""),
        str(get("radius") or ""),
        str(get("arc_length") or ""),
        str(get("chord_length") or ""),
        str(get("delta") or ""),
        str(get("monument") or ""),
        str(get("description") or ""),
        str(get("confidence") or ""),
    ]
    return "\x1f".join(parts)


def content_hash(
    calls: Sequence[Any] | None = None,
    ties: Sequence[Any] | None = None,
) -> str:
    """Hash call/tie rows so same-count edits invalidate pending indexes."""
    h = hashlib.sha256()
    for label, rows in (("c", calls or ()), ("t", ties or ())):
        h.update(label.encode())
        h.update(str(len(rows)).encode())
        for row in rows:
            h.update(_call_fingerprint_row(row).encode("utf-8", errors="replace"))
            h.update(b"\n")
    return h.hexdigest()[:24]


def workspace_fingerprint(
    *,
    n_calls: int,
    n_ties: int,
    source_path: str = "",
    content_hash: str = "",
) -> dict[str, Any]:
    return {
        "n_calls": int(n_calls),
        "n_ties": int(n_ties),
        "source_path": str(source_path or ""),
        "content_hash": str(content_hash or ""),
    }


def _clean_actions(actions: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in actions or []:
        if not isinstance(raw, dict):
            continue
        clean = {k: v for k, v in raw.items() if k != "_chk"}
        out.append(clean)
    return out


def save_pending(
    actions: list[dict[str, Any]],
    skipped: list[str] | None = None,
    *,
    fingerprint: dict[str, Any] | None = None,
    path: Path | None = None,
) -> None:
    cleaned = _clean_actions(actions)
    if not cleaned:
        clear_pending(path=path)
        return
    target = path or PENDING_FILE
    payload = {
        "actions": cleaned,
        "skipped": [str(s) for s in (skipped or []) if s],
        "fingerprint": dict(fingerprint or {}),
    }
    _atomic_write(target, json.dumps(payload, indent=2))


def load_pending(*, path: Path | None = None) -> dict[str, Any] | None:
    target = path or PENDING_FILE
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    actions = _clean_actions(data.get("actions") if isinstance(data.get("actions"), list) else [])
    if not actions:
        return None
    skipped = data.get("skipped") or []
    if not isinstance(skipped, list):
        skipped = []
    fp = data.get("fingerprint") if isinstance(data.get("fingerprint"), dict) else {}
    return {
        "actions": actions,
        "skipped": [str(s) for s in skipped if s],
        "fingerprint": fp,
    }


def clear_pending(*, path: Path | None = None) -> None:
    """Mark pending empty so Restore will not resurrect a lagging workspace snapshot."""
    target = path or PENDING_FILE
    payload = {"actions": [], "skipped": [], "fingerprint": {}, "cleared": True}
    try:
        _atomic_write(target, json.dumps(payload, indent=2))
    except OSError:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            raise


def fingerprint_from_packed_state(state: dict[str, Any]) -> dict[str, Any]:
    """Fingerprint call/tie tables in a loaded workspace/job dict."""
    calls = list(state.get("calls") or [])
    ties = list(state.get("tie_calls") or [])
    return workspace_fingerprint(
        n_calls=len(calls),
        n_ties=len(ties),
        source_path=str(state.get("source_path") or ""),
        content_hash=content_hash(calls, ties),
    )


def restore_should_list_pending(
    snapshot_pending: dict[str, Any] | None,
    *,
    status: str | None = None,
    path: Path | None = None,
    workspace_state: dict[str, Any] | None = None,
) -> bool:
    """True when Restore would actually restore pending (trust sidecar first).

    When ``workspace_state`` is given, sidecar actions are listed only if their
    fingerprint would still match that workspace after Restore.
    """
    st = sidecar_status(path=path) if status is None else status
    has_snap = bool(
        isinstance(snapshot_pending, dict) and snapshot_pending.get("actions")
    )
    if st == "cleared":
        return False
    if st == "actions":
        if workspace_state is None:
            return True
        data = load_pending(path=path)
        return sidecar_would_keep_after_restore(data, workspace_state)
    return has_snap


def sidecar_would_keep_after_restore(
    data: dict[str, Any] | None,
    workspace_state: dict[str, Any],
) -> bool:
    """Same keep/drop rule as ``_restore_chat_session`` after Restore."""
    if not data or not data.get("actions"):
        return False
    fp = dict(data.get("fingerprint") or {})
    cur = fingerprint_from_packed_state(workspace_state)
    if not fp.get("content_hash"):
        try:
            n_calls = int(fp["n_calls"]) if "n_calls" in fp else int(cur["n_calls"])
            n_ties = int(fp["n_ties"]) if "n_ties" in fp else int(cur["n_ties"])
        except (TypeError, ValueError):
            n_calls, n_ties = int(cur["n_calls"]), int(cur["n_ties"])
        fp = workspace_fingerprint(
            n_calls=n_calls,
            n_ties=n_ties,
            source_path=str(fp.get("source_path") or cur.get("source_path") or ""),
            content_hash=str(cur["content_hash"]),
        )
    return fingerprint_matches(fp, cur)


def sidecar_status(*, path: Path | None = None) -> str:
    """``missing`` | ``cleared`` | ``actions`` — Restore uses this vs workspace snapshot."""
    target = path or PENDING_FILE
    if not target.is_file():
        return "missing"
    data = load_pending(path=path)
    if data and data.get("actions"):
        return "actions"
    return "cleared"


def has_pending(*, path: Path | None = None) -> bool:
    data = load_pending(path=path)
    return bool(data and data.get("actions"))


def fingerprint_matches(
    stored: dict[str, Any] | None,
    current: dict[str, Any],
) -> bool:
    """True when call/tie counts and content still match the pending set.

    ``source_path`` is stored for diagnostics but not required to match — Open
    Deed → Keep traverse changes the path without shifting call indexes.
    """
    if not stored:
        return False
    try:
        return (
            int(stored.get("n_calls", -1)) == int(current.get("n_calls", -2))
            and int(stored.get("n_ties", -1)) == int(current.get("n_ties", -2))
            and str(stored.get("content_hash") or "")
            == str(current.get("content_hash") or "")
        )
    except (TypeError, ValueError):
        return False
