"""Standalone DXF viewer for Deed Plotter exports.

Usage:
    python dxf_viewer.py
    python dxf_viewer.py path\\to\\boundary.dxf
"""

from __future__ import annotations

import sys
from pathlib import Path

from dxf_viewer_window import run_viewer_app


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else None
    if path is not None and not path.is_file():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    return run_viewer_app(path)


if __name__ == "__main__":
    raise SystemExit(main())
