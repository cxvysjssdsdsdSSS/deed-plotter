"""Autosave workspace and named job files for the edited traverse."""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cogo import Call

DATA_DIR = Path.home() / ".deed_plotter"
WORKSPACE_FILE = DATA_DIR / "workspace.json"
JOB_VERSION = 2
JOB_FORMAT = "deed-plotter-job"

_CALL_FIELDS = {f.name for f in dataclasses.fields(Call)}


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file then replace (crash-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _calls_to_dicts(calls: list[Call]) -> list[dict]:
    return [dataclasses.asdict(c) for c in calls]


def _dicts_to_calls(raw: list) -> list[Call]:
    """Rebuild Calls, ignoring unknown keys (schema drift / hand edits)."""
    calls = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        try:
            calls.append(Call(**{k: v for k, v in c.items() if k in _CALL_FIELDS}))
        except TypeError:
            continue
    return calls


def _normalize_chat_history(raw) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for turn in raw:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        content = turn.get("content")
        if content is None:
            continue
        out.append({"role": role, "content": str(content)})
    return out


def _normalize_pending_chat(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    cleaned = []
    for a in actions:
        if isinstance(a, dict):
            cleaned.append({k: v for k, v in a.items() if k != "_chk"})
    if not cleaned:
        return None
    skipped = raw.get("skipped") or []
    if not isinstance(skipped, list):
        skipped = []
    fp = raw.get("fingerprint") if isinstance(raw.get("fingerprint"), dict) else {}
    return {
        "actions": cleaned,
        "skipped": [str(s) for s in skipped if s],
        "fingerprint": dict(fp),
    }


def pack_state(
    *,
    source_name: str = "",
    source_path: str = "",
    source_paths: list[str] | None = None,
    text_input: str = "",
    calls: list[Call] | None = None,
    tie_calls: list[Call] | None = None,
    document_info: dict | None = None,
    legal_description: str = "",
    point_of_beginning: str = "",
    pob_monument: str = "",
    pob_coordinates: dict | None = None,
    general_notes: str = "",
    parse_warnings: list[str] | None = None,
    selected_pages: list[int] | None = None,
    chat_history: list[dict] | None = None,
    pending_chat: dict | None = None,
) -> dict:
    pending = _normalize_pending_chat(pending_chat) if pending_chat else None
    paths = _normalize_source_paths(source_paths, source_path)
    return {
        "format": JOB_FORMAT,
        "version": JOB_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": source_name,
        "source_path": paths[0] if paths else source_path,
        "source_paths": paths,
        "text_input": text_input,
        "calls": _calls_to_dicts(calls or []),
        "tie_calls": _calls_to_dicts(tie_calls or []),
        "document_info": dict(document_info or {}),
        "legal_description": legal_description,
        "point_of_beginning": point_of_beginning,
        "pob_monument": pob_monument,
        "pob_coordinates": pob_coordinates,
        "general_notes": general_notes,
        "parse_warnings": list(parse_warnings or []),
        "selected_pages": selected_pages,
        "chat_history": _normalize_chat_history(chat_history),
        "pending_chat": pending,
    }


def _normalize_source_paths(
    source_paths: list | None,
    source_path: str = "",
) -> list[str]:
    """Dedupe path list; fall back to single *source_path* for older jobs."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(source_paths or []) + ([source_path] if source_path else []):
        p = str(raw or "").strip()
        if not p:
            continue
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def unpack_state(data: dict) -> dict:
    """Normalize a saved dict into Call objects and known keys."""
    if not isinstance(data, dict):
        raise ValueError("Job file is not a JSON object.")
    source_path = str(data.get("source_path", ""))
    source_paths = _normalize_source_paths(data.get("source_paths"), source_path)
    return {
        "source_name": str(data.get("source_name", "")),
        "source_path": source_paths[0] if source_paths else source_path,
        "source_paths": source_paths,
        "text_input": str(data.get("text_input", "")),
        "calls": _dicts_to_calls(data.get("calls") or []),
        "tie_calls": _dicts_to_calls(data.get("tie_calls") or []),
        "document_info": dict(data.get("document_info") or {}),
        "legal_description": str(data.get("legal_description", "")),
        "point_of_beginning": str(data.get("point_of_beginning", "")),
        "pob_monument": str(data.get("pob_monument", "")),
        "pob_coordinates": data.get("pob_coordinates"),
        "general_notes": str(data.get("general_notes", "")),
        "parse_warnings": [str(x) for x in (data.get("parse_warnings") or [])],
        "selected_pages": data.get("selected_pages"),
        "saved_at": str(data.get("saved_at", "")),
        "chat_history": _normalize_chat_history(data.get("chat_history")),
        "pending_chat": _normalize_pending_chat(data.get("pending_chat")),
    }


def save_workspace(state: dict) -> None:
    _atomic_write(WORKSPACE_FILE, json.dumps(state, indent=1))


def load_workspace() -> dict | None:
    try:
        data = json.loads(WORKSPACE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not (data.get("calls") or data.get("tie_calls") or data.get("text_input")
            or data.get("legal_description") or data.get("document_info")
            or data.get("point_of_beginning") or data.get("pob_monument")
            or data.get("general_notes") or data.get("pob_coordinates")
            or data.get("parse_warnings")
            or data.get("chat_history") or data.get("pending_chat")
            or data.get("source_path") or data.get("source_paths")
            or data.get("selected_pages")):
        return None
    try:
        return unpack_state(data)
    except (TypeError, ValueError, KeyError):
        return None


def clear_workspace() -> None:
    WORKSPACE_FILE.unlink(missing_ok=True)


def save_job(path: str | Path, state: dict) -> None:
    _atomic_write(Path(path), json.dumps(state, indent=2))


def load_job(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return unpack_state(data)
