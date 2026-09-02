"""Disk cache of rasterized deed page PNGs.

Skips PyMuPDF re-rasterization on Open / Restore / History / Open Job when the
source file stats and image quality are unchanged.

Verbatim PNG bytes only (no re-encode) — parse resume hashes these bytes via
``parse_cache.page_bytes_hash``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from document_loader import resolve_image_quality

CACHE_DIR = Path.home() / ".deed_plotter" / "page_cache"

# Bump when layout or key meaning changes so old dirs miss cleanly.
PAGE_CACHE_VERSION = "1"

# Multipage fine PNGs can be tens–hundreds of MB across this many entries.
# Overridable from Settings → page_cache_max.
MAX_CACHE_ENTRIES = 8
DEFAULT_PAGE_CACHE_MAX = MAX_CACHE_ENTRIES

_STAGING_PREFIX = ".publishing-"
_META_NAME = "meta.json"


def _utc_now() -> str:
    # Milliseconds avoid same-second LRU ties when several entries save together.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _norm_path(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _source_stat(path: Path) -> os.stat_result:
    return path.stat()


def _quality_bits(quality: str | None) -> tuple[str, int, int]:
    q = resolve_image_quality(quality)
    return q.id, int(q.dpi), int(q.max_dim)


def cache_key(path: str | Path, quality: str | None = None) -> str:
    """Stable dir name for this source file + raster settings."""
    p = Path(path)
    st = _source_stat(p)
    qid, dpi, max_dim = _quality_bits(quality)
    payload = "\n".join(
        [
            PAGE_CACHE_VERSION,
            _norm_path(p),
            str(st.st_size),
            str(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            qid,
            str(dpi),
            str(max_dim),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _entry_dir(key: str) -> Path:
    return CACHE_DIR / key


def _page_path(entry: Path, index: int) -> Path:
    return entry / f"page_{index:03d}.png"


def _read_meta(entry: Path) -> dict | None:
    meta_path = entry / _META_NAME
    try:
        raw = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _meta_matches_live(
    meta: dict,
    *,
    path: Path,
    quality: str | None,
) -> bool:
    if str(meta.get("schema_version") or "") != PAGE_CACHE_VERSION:
        return False
    qid, dpi, max_dim = _quality_bits(quality)
    try:
        st = _source_stat(path)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
    except OSError:
        return False
    try:
        return (
            str(meta.get("path") or "") == _norm_path(path)
            and int(meta.get("size") or -1) == int(st.st_size)
            and int(meta.get("mtime_ns") or -1) == mtime_ns
            and str(meta.get("quality") or "") == qid
            and int(meta.get("dpi") or -1) == dpi
            and int(meta.get("max_dim") or -1) == max_dim
            and int(meta.get("page_count") or -1) >= 1
        )
    except (TypeError, ValueError):
        return False


def _png_looks_readable(data: bytes) -> bool:
    """Reject truncated/corrupt PNGs without re-encoding (parse hashes need bytes)."""
    # signature (8) + IHDR chunk (4+4+13+4) + at least empty IDAT + IEND (12)
    if len(data) < 57 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    length = int.from_bytes(data[8:12], "big")
    if length != 13 or data[12:16] != b"IHDR":
        return False
    # Require a trailing IEND so body-truncated files with a valid IHDR miss.
    return data.endswith(b"IEND\xaeB`\x82") or b"IEND" in data[33:]


def _touch_saved_at(entry: Path, meta: dict) -> None:
    meta = dict(meta)
    meta["saved_at"] = _utc_now()
    try:
        text = json.dumps(meta, indent=1)
        tmp = entry / (_META_NAME + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, entry / _META_NAME)
    except OSError:
        pass


def try_load(path: str | Path, quality: str | None = None) -> list[bytes] | None:
    """Return cached page PNGs, or None on any miss/corruption.

    Never raises into the worker as a load failure.
    """
    try:
        p = Path(path)
        if not p.is_file():
            return None
        key = cache_key(p, quality)
        entry = _entry_dir(key)
        meta = _read_meta(entry)
        if meta is None or not _meta_matches_live(meta, path=p, quality=quality):
            return None
        n = int(meta["page_count"])
        pages: list[bytes] = []
        for i in range(n):
            data = _page_path(entry, i).read_bytes()
            if not _png_looks_readable(data):
                return None
            pages.append(data)
        _touch_saved_at(entry, meta)
        return pages
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _write_meta(entry: Path, meta: dict) -> None:
    (entry / _META_NAME).write_text(json.dumps(meta, indent=1), encoding="utf-8")


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def save(
    path: str | Path,
    pages: list[bytes],
    quality: str | None = None,
    *,
    is_cancelled=None,
    max_entries: int | None = None,
) -> None:
    """Cache *pages* verbatim. Fail-open: swallow OSError (never raise)."""
    if not pages:
        return
    try:
        p = Path(path)
        if not p.is_file():
            return
        if is_cancelled is not None and is_cancelled():
            return
        st = _source_stat(p)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        qid, dpi, max_dim = _quality_bits(quality)
        key = cache_key(p, quality)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        staging = CACHE_DIR / f"{_STAGING_PREFIX}{key}-{uuid.uuid4().hex[:8]}"
        dest = _entry_dir(key)
        try:
            staging.mkdir(parents=True, exist_ok=False)
            for i, png in enumerate(pages):
                if is_cancelled is not None and is_cancelled():
                    _rmtree(staging)
                    return
                if not isinstance(png, (bytes, bytearray)):
                    _rmtree(staging)
                    return
                _page_path(staging, i).write_bytes(bytes(png))
            meta = {
                "schema_version": PAGE_CACHE_VERSION,
                "path": _norm_path(p),
                "size": int(st.st_size),
                "mtime_ns": mtime_ns,
                "quality": qid,
                "dpi": dpi,
                "max_dim": max_dim,
                "page_count": len(pages),
                "saved_at": _utc_now(),
            }
            # Commit marker last — incomplete staging never looks like a hit.
            _write_meta(staging, meta)
            if is_cancelled is not None and is_cancelled():
                _rmtree(staging)
                return
            if dest.exists():
                _rmtree(dest)
            os.replace(staging, dest)
        except OSError:
            if staging.exists():
                _rmtree(staging)
            return
        limit = MAX_CACHE_ENTRIES if max_entries is None else int(max_entries)
        prune(max(1, limit))
    except OSError:
        return


def prune(max_entries: int = MAX_CACHE_ENTRIES, *, drop_staging: bool = True) -> None:
    """Keep newest complete entries by meta.saved_at; optionally drop staging leftovers."""
    try:
        if not CACHE_DIR.is_dir():
            return
        children = list(CACHE_DIR.iterdir())
    except OSError:
        return

    complete: list[tuple[str, Path]] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(_STAGING_PREFIX):
                if drop_staging:
                    _rmtree(child)
                continue
            meta = _read_meta(child)
            if meta is None:
                _rmtree(child)
                continue
            saved = str(meta.get("saved_at") or "")
            if not saved:
                _rmtree(child)
                continue
            complete.append((saved, child))
        except OSError:
            continue

    complete.sort(key=lambda t: t[0], reverse=True)
    for _, path in complete[max(0, int(max_entries)):]:
        try:
            _rmtree(path)
        except OSError:
            continue


def clear_all() -> None:
    """Delete the entire page cache directory (no Settings UI yet)."""
    if CACHE_DIR.exists():
        _rmtree(CACHE_DIR)
