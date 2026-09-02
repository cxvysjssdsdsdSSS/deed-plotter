"""Cursor SDK bridge bootstrap patches.

Windows: cursor-sdk polls bridge stderr with ``selectors.select()``, which
fails (WinError 10038) because ``select()`` only supports sockets there. This
replaces discovery polling with a sleep-based loop. Also hides the blank
``cmd.exe`` flash by running ``node.exe`` + the bridge ``.js`` with
``CREATE_NO_WINDOW``.

All platforms: ``secrets.token_urlsafe`` can mint a callback auth token that
starts with ``-``; the bridge CLI then rejects it as ``Missing value for
--tool-callback-auth-token``. Regenerate until the token is safe.
"""

from __future__ import annotations

import codecs
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_PATCHED = False
_PATCH_LOCK = threading.Lock()


def _normalize_bridge_argv(argv: Sequence[str]) -> list[str]:
    """Prefer node.exe + .js over cursor-sdk-bridge.cmd (avoids cmd.exe)."""
    if not argv:
        return list(argv)
    first = Path(argv[0])
    if first.suffix.lower() != ".cmd":
        return list(argv)
    node = first.parent / "node.exe"
    js = first.parent.parent / "dist" / "bin" / "cursor-sdk-bridge.js"
    if node.is_file() and js.is_file():
        return [str(node), str(js), *list(argv)[1:]]
    return list(argv)


def _win_no_window_flags(existing: int = 0) -> int:
    return int(existing or 0) | subprocess.CREATE_NO_WINDOW


def _safe_auth_token() -> str:
    """token_urlsafe can start with '-'; bridge takeValue rejects that."""
    token = secrets.token_urlsafe(32)
    while token.startswith("-"):
        token = secrets.token_urlsafe(32)
    return token


def _patch_auth_token_generators() -> None:
    import cursor_sdk._store_callback as store_callback
    import cursor_sdk._tool_callback as tool_callback

    tool_callback._new_auth_token = _safe_auth_token
    store_callback._new_auth_token = _safe_auth_token


def ensure_cursor_bridge_windows_patch() -> None:
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return

        _patch_auth_token_generators()

        if sys.platform != "win32":
            _PATCHED = True
            return

        import asyncio

        import cursor_sdk._async_bridge as async_bridge_mod
        import cursor_sdk._bridge as bridge_mod
        from cursor_sdk.errors import CursorSDKError

        def _read_discovery(process, timeout: float) -> Mapping[str, Any]:
            if process.stderr is None:
                raise CursorSDKError("Bridge process stderr is unavailable")
            stderr_fd = process.stderr.fileno()
            was_blocking = os.get_blocking(stderr_fd)
            os.set_blocking(stderr_fd, False)

            try:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                deadline = time.monotonic() + timeout
                stderr_lines: list[str] = []
                pending = ""

                def drain_available() -> Mapping[str, Any] | None:
                    nonlocal pending
                    while True:
                        try:
                            chunk = os.read(stderr_fd, 8192)
                        except BlockingIOError:
                            return None
                        if not chunk:
                            final_text = decoder.decode(b"", final=True)
                            if final_text:
                                pending += final_text
                            if pending:
                                line = pending
                                pending = ""
                                stderr_lines.append(line)
                                return bridge_mod.parse_discovery_line(line)
                            return None
                        pending += decoder.decode(chunk)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            line += "\n"
                            stderr_lines.append(line)
                            discovery = bridge_mod.parse_discovery_line(line)
                            if discovery is not None:
                                return discovery

                while time.monotonic() < deadline:
                    discovery = drain_available()
                    if discovery is not None:
                        return discovery
                    exit_code = process.poll()
                    if exit_code is not None:
                        discovery = drain_available()
                        if discovery is not None:
                            return discovery
                        raise CursorSDKError(
                            f"Bridge exited before discovery with status {exit_code}: "
                            + "".join(stderr_lines)
                            + pending
                        )
                    time.sleep(0.05)
                raise CursorSDKError("Timed out waiting for bridge discovery")
            finally:
                os.set_blocking(stderr_fd, was_blocking)

        bridge_mod._read_discovery = _read_discovery

        _real_popen = bridge_mod.subprocess.Popen

        def _popen_no_window(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)):
                argv = _normalize_bridge_argv([str(a) for a in argv])
            kwargs["creationflags"] = _win_no_window_flags(
                kwargs.get("creationflags", 0))
            return _real_popen(argv, *args, **kwargs)

        bridge_mod.subprocess.Popen = _popen_no_window

        _real_exec = async_bridge_mod.asyncio.create_subprocess_exec

        async def _exec_no_window(*argv, **kwargs):
            argv = _normalize_bridge_argv([str(a) for a in argv])
            kwargs["creationflags"] = _win_no_window_flags(
                kwargs.get("creationflags", 0))
            return await _real_exec(*argv, **kwargs)

        async_bridge_mod.asyncio.create_subprocess_exec = _exec_no_window
        # Keep the top-level asyncio name in sync (same module object).
        asyncio.create_subprocess_exec = _exec_no_window

        _PATCHED = True
