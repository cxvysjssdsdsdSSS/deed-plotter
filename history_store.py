"""Persistence for AI parse history (JSON file in the user's home directory)."""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cogo import Call

HISTORY_FILE = Path.home() / ".deed_plotter" / "history.json"
MAX_ENTRIES = 100

_CALL_FIELDS = {f.name for f in dataclasses.fields(Call)}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


def _load_raw() -> list[dict]:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_raw(entries: list[dict]) -> None:
    _atomic_write(HISTORY_FILE, json.dumps(entries, indent=1))


def add_entry(
    source_name: str,
    model: str,
    result: dict,
    source_path: str = "",
    source_paths: list[str] | None = None,
    selected_pages: list[int] | None = None,
) -> None:
    """Store one successful parse. Newest entries first; capped at MAX_ENTRIES."""
    payload = dict(result)
    payload["calls"] = [dataclasses.asdict(c) for c in result.get("calls", [])]
    payload["tie_calls"] = [dataclasses.asdict(c) for c in result.get("tie_calls", [])]
    paths: list[str] = []
    seen: set[str] = set()
    for raw in list(source_paths or []) + ([source_path] if source_path else []):
        p = str(raw or "").strip()
        if not p:
            continue
        key = os.path.normcase(p)
        if key in seen:
            continue
        seen.add(key)
        paths.append(p)
    pages = None
    if selected_pages is not None:
        pages = sorted({int(i) for i in selected_pages if int(i) >= 0})
    entry = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": source_name,
        "source_path": paths[0] if paths else source_path,
        "source_paths": paths,
        "selected_pages": pages,
        "model": model,
        "num_calls": len(result.get("calls", [])),
        "result": payload,
    }
    entries = [entry] + _load_raw()
    _save_raw(entries[:MAX_ENTRIES])


def list_entries() -> list[dict]:
    return _load_raw()


def has_entries() -> bool:
    return bool(_load_raw())


def remove_entry(entry_id: str) -> None:
    _save_raw([e for e in _load_raw() if e.get("id") != entry_id])


def clear_all() -> None:
    _save_raw([])


def restore_result(entry: dict) -> dict:
    """Rebuild the parse-result dict (with Call objects) from a stored entry."""
    result = dict(entry.get("result", {}))
    result["calls"] = _dicts_to_calls(result.get("calls") or [])
    result["tie_calls"] = _dicts_to_calls(result.get("tie_calls") or [])
    result.setdefault("parse_warnings", [])
    return result
