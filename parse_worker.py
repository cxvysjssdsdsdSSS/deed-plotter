"""Subprocess entry point for AI deed jobs (parse + chat).

The Cursor SDK's bridge needs its own process on Windows (it can't run
inside a Qt worker thread), so the GUI launches this script and reads the
result JSON from stdout.

Usage: python parse_worker.py <job.json>
Job file:
  Parse (legacy multi-page batch):
    {"operation": "parse", "model", "image_paths", "text"}
  Parse one page (preferred):
    {"operation": "parse_page", "model", "image_paths": [one],
     "page_number", "total_pages", "prior_context", "text"}
  Chat:
    {"operation": "chat", "model", "deed_context", "history", "message"}
  API key from DEED_CURSOR_API_KEY env only.
Output (stdout): {"ok": true, "result": {...}} or {"ok": false, "error": str}
  Chat result: {"text": str, "proposal": {"actions": [...], "skipped": [...]}|null}
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

from ai_client import AIClientError, DeedParserClient
from cursor_bridge_patch import ensure_cursor_bridge_windows_patch


def _serialize_parse_result(result: dict) -> dict:
    return {
        "calls": [dataclasses.asdict(c) for c in result["calls"]],
        "tie_calls": [dataclasses.asdict(c) for c in result["tie_calls"]],
        "document_info": result["document_info"],
        "legal_description": result["legal_description"],
        "point_of_beginning": result["point_of_beginning"],
        "pob_coordinates": result["pob_coordinates"],
        "pob_monument": result["pob_monument"],
        "general_notes": result["general_notes"],
        "parse_warnings": result.get("parse_warnings", []),
    }


def main() -> int:
    ensure_cursor_bridge_windows_patch()
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    api_key = os.environ.get("DEED_CURSOR_API_KEY", "") or job.get("api_key", "")
    client = DeedParserClient(api_key, job.get("model", ""))
    operation = str(job.get("operation") or "parse").lower()
    try:
        if operation == "chat":
            out = client.chat(
                job.get("deed_context", ""),
                list(job.get("history") or []),
                job.get("message", ""),
            )
            if not isinstance(out, dict):
                out = {"text": str(out), "proposal": None}
            payload = {
                "ok": True,
                "result": {
                    "text": str(out.get("text") or ""),
                    "proposal": out.get("proposal"),
                },
            }
        elif operation == "parse_page":
            paths = list(job.get("image_paths") or [])
            if len(paths) != 1:
                raise AIClientError(
                    f"parse_page expects exactly one image path, got {len(paths)}."
                )
            page_png = Path(paths[0]).read_bytes()
            result = client.parse_page(
                page_png,
                page_number=int(job.get("page_number") or 1),
                total_pages=int(job.get("total_pages") or 1),
                document_page=(
                    int(job["document_page"])
                    if job.get("document_page") is not None
                    else None
                ),
                prior_context=str(job.get("prior_context") or ""),
                extra_text=str(job.get("text") or ""),
            )
            payload = {"ok": True, "result": _serialize_parse_result(result)}
        else:
            images = [Path(p).read_bytes() for p in job.get("image_paths", [])]
            if images:
                result = client.parse_images(images, job.get("text", ""))
            else:
                result = client.parse_text(job.get("text", ""))
            payload = {"ok": True, "result": _serialize_parse_result(result)}
    except AIClientError as exc:
        payload = {"ok": False, "error": str(exc)}
    except Exception as exc:  # report anything else back to the GUI
        payload = {"ok": False, "error": f"Unexpected error: {exc}"}
    print(json.dumps(payload), flush=True)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
