"""Fetch the Cursor models available to an API key, for the settings UI."""

from __future__ import annotations

from dataclasses import dataclass

from cursor_bridge_patch import ensure_cursor_bridge_windows_patch


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str


def merge_model_options(fetched: list[ModelOption], *saved_ids: str) -> list[ModelOption]:
    """Fetched models plus any saved ids not in the list (marked '(saved)')."""
    by_id = {option.id: option for option in fetched if option.id}
    for model_id in saved_ids:
        cleaned = (model_id or "").strip()
        if not cleaned or cleaned in by_id:
            continue
        by_id[cleaned] = ModelOption(id=cleaned, label=f"{cleaned} (saved)")
    return sorted(by_id.values(), key=lambda option: option.id.lower())


def fetch_model_options(api_key: str) -> tuple[list[ModelOption], str | None]:
    """Return models available to *api_key*, or (empty, error message)."""
    key = (api_key or "").strip()
    if not key:
        return [], "Enter an API key to load models."

    ensure_cursor_bridge_windows_patch()
    try:
        from cursor_sdk import Cursor
    except ImportError:
        return [], "cursor-sdk is not installed."

    try:
        models = Cursor.models.list(api_key=key)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return [], f"Could not list models: {detail}"

    options = []
    for model in models:
        if not model.id:
            continue
        display = (getattr(model, "display_name", "") or "").strip()
        label = "Auto" if model.id.lower() == "default" and display.lower() == "auto" else model.id
        options.append(ModelOption(id=model.id, label=label))
    if not options:
        return [], "No models returned for this API key."
    return options, None
