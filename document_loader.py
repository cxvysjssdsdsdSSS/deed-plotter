"""Load multipage PDFs and image files into per-page PNG bytes.

Raster settings use a named quality preset (DPI + max long edge). Defaults
target Cursor Composer 2.5 vision: sharp enough for deeds, capped so pages
are not wasted on oversized rasters the model will downscale anyway.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp",
    ".heic", ".heif", ".heifs",
}
# Camera containers: use the first frame only (MPO stereo / HEIC stacks).
_SINGLE_FRAME_FORMATS = frozenset({"MPO", "HEIF", "HEIC", "HEICS"})

_HEIF_REGISTERED = False


@dataclass(frozen=True)
class ImageQuality:
    id: str
    dpi: int
    max_dim: int
    label: str
    tip: str


# Presets keep DPI and max edge paired (high DPI + tiny max edge wastes work).
IMAGE_QUALITY_PRESETS: dict[str, ImageQuality] = {
    "fast": ImageQuality(
        id="fast",
        dpi=150,
        max_dim=1600,
        label="Fast (150 DPI)",
        tip="Smaller pages — quicker/cheaper on large multipage PDFs.",
    ),
    "standard": ImageQuality(
        id="standard",
        dpi=200,
        max_dim=2200,
        label="Standard (200 DPI)",
        tip="Default for Composer 2.5 — good balance for most deeds.",
    ),
    "fine": ImageQuality(
        id="fine",
        dpi=300,
        max_dim=2800,
        label="Fine print (300 DPI)",
        tip="Small handwriting / dense image PDFs. Slower; re-open the deed after changing.",
    ),
}
DEFAULT_IMAGE_QUALITY = "standard"


def resolve_image_quality(preset_id: str | None) -> ImageQuality:
    key = (preset_id or DEFAULT_IMAGE_QUALITY).strip().lower()
    return IMAGE_QUALITY_PRESETS.get(key, IMAGE_QUALITY_PRESETS[DEFAULT_IMAGE_QUALITY])


def format_quality_progress_line(
    used_id: str | None,
    *,
    settings_id: str | None = None,
) -> str:
    """Raster quality line for the AI Parsing progress dialog."""
    used = resolve_image_quality(used_id)
    line = f"Image: {used.label} used (max {used.max_dim} px)"
    if settings_id is None:
        return line
    selected = resolve_image_quality(settings_id)
    if selected.id == used.id:
        return line
    return (
        f"{line}; Settings selected {selected.label} — re-open deed to apply"
    )


def _ensure_heif_support() -> None:
    """Register pillow-heif once when installed (optional dependency)."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    _HEIF_REGISTERED = True


def _downscale_png(png_bytes: bytes, max_dim: int) -> bytes:
    img = Image.open(io.BytesIO(png_bytes))
    if max(img.size) <= max_dim:
        return png_bytes
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def pdf_zoom_for_page(page_width_pt: float, page_height_pt: float, dpi: int, max_dim: int) -> float:
    """Zoom so the rendered long edge never exceeds *max_dim*.

    Large scan PDFs often store pages as huge point boxes (thousands of pt).
    Applying DPI first, then downscaling, allocates multi-hundred-MB pixmaps
    and can OOM. Cap the matrix up front instead.
    """
    zoom = dpi / 72.0
    long_edge = max(float(page_width_pt), float(page_height_pt))
    if long_edge <= 0:
        return zoom
    return min(zoom, max_dim / long_edge)


class DocumentLoadCancelled(Exception):
    """Raised when a cooperative cancel is requested mid-load."""


def _raster_image_file(
    path: Path,
    *,
    use_max: int,
    on_progress,
    check_cancel,
    progress_base: int = 0,
    progress_total: int | None = None,
) -> list[bytes]:
    """Rasterize one image file to PNG page bytes (RGB)."""
    _ensure_heif_support()
    img = None
    try:
        img = Image.open(path)
    except OSError as exc:
        ext = path.suffix.lower()
        if ext in {".heic", ".heif", ".heifs"}:
            raise ValueError(
                f"Cannot open {path.name}: install pillow-heif "
                f"(pip install pillow-heif) or convert to JPEG/PNG first."
            ) from exc
        raise ValueError(f"Cannot open image {path.name}: {exc}") from exc

    try:
        fmt = (img.format or "").upper()
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        if fmt in _SINGLE_FRAME_FORMATS:
            n_frames = 1
        total = progress_total if progress_total is not None else n_frames
        pages: list[bytes] = []
        check_cancel()
        if progress_base == 0:
            on_progress(0, total)
        for i in range(n_frames):
            check_cancel()
            on_progress(progress_base + i + 1, total)
            if n_frames > 1:
                img.seek(i)
            frame = img.convert("RGB")
            if max(frame.size) > use_max:
                frame.thumbnail((use_max, use_max), Image.LANCZOS)
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            pages.append(buf.getvalue())
            check_cancel()
        return pages
    finally:
        if img is not None:
            img.close()


def load_document(
    path: str,
    *,
    quality: str | None = None,
    dpi: int | None = None,
    max_dim: int | None = None,
    on_progress=None,
    is_cancelled=None,
) -> list[bytes]:
    """Return one PNG byte string per page of the given PDF or image file.

    *quality* is a preset id (``fast`` / ``standard`` / ``fine``). Explicit
    *dpi* / *max_dim* override the preset when provided.

    *on_progress(current_1based, total)* is called with ``(0, total)`` once
    the file is open, then again before each page is rasterized
    (``1..total``). *is_cancelled()* if true raises ``DocumentLoadCancelled``.
    """
    return load_documents(
        [path],
        quality=quality,
        dpi=dpi,
        max_dim=max_dim,
        on_progress=on_progress,
        is_cancelled=is_cancelled,
    )


def load_documents(
    paths: list[str] | tuple[str, ...],
    *,
    quality: str | None = None,
    dpi: int | None = None,
    max_dim: int | None = None,
    on_progress=None,
    is_cancelled=None,
) -> list[bytes]:
    """Rasterize one or more deed files into a single page list.

    Multiple paths must all be images (phone photos as pages). A single PDF
    or image uses the normal one-file path. Multi-file PDF mixes are rejected.
    """
    q = resolve_image_quality(quality)
    use_dpi = int(dpi) if dpi is not None else q.dpi
    use_max = int(max_dim) if max_dim is not None else q.max_dim
    use_dpi = max(72, min(use_dpi, 600))
    use_max = max(800, min(use_max, 5000))

    normalized = [Path(p) for p in paths if str(p).strip()]
    if not normalized:
        raise ValueError("No files selected.")

    def _check_cancel() -> None:
        if is_cancelled is not None and is_cancelled():
            raise DocumentLoadCancelled()

    def _report(current: int, total: int) -> None:
        if on_progress is not None:
            on_progress(current, total)

    if len(normalized) == 1:
        p = normalized[0]
        ext = p.suffix.lower()
        if ext == ".pdf":
            pages = []
            try:
                with fitz.open(str(p)) as doc:
                    total = int(doc.page_count)
                    _check_cancel()
                    _report(0, total)
                    for i, page in enumerate(doc, start=1):
                        _check_cancel()
                        _report(i, total)
                        rect = page.rect
                        zoom = pdf_zoom_for_page(rect.width, rect.height, use_dpi, use_max)
                        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                        try:
                            pages.append(_downscale_png(pix.tobytes("png"), use_max))
                        finally:
                            pix = None
                        _check_cancel()
            except Exception as exc:
                # Prefer a file-named message for corrupt / encrypted PDFs.
                if isinstance(exc, DocumentLoadCancelled):
                    raise
                raise ValueError(f"Cannot open PDF {p.name}: {exc}") from exc
            return pages
        if ext in IMAGE_EXTS:
            return _raster_image_file(
                p, use_max=use_max, on_progress=_report, check_cancel=_check_cancel,
            )
        raise ValueError(f"Unsupported file type: {ext}")

    # Multi-file: images only, one page (or TIFF frames) each, concatenated.
    bad = [p for p in normalized if p.suffix.lower() not in IMAGE_EXTS]
    if bad:
        names = ", ".join(p.name for p in bad)
        raise ValueError(
            "When opening multiple files, select image files only "
            f"(not PDF). Unsupported: {names}"
        )

    # Progress total ≈ one step per file (multi-frame TIFF still expands).
    # Peek frame counts cheaply where possible.
    frame_counts: list[int] = []
    _ensure_heif_support()
    for p in normalized:
        _check_cancel()
        try:
            with Image.open(p) as img:
                fmt = (img.format or "").upper()
                n = int(getattr(img, "n_frames", 1) or 1)
                if fmt in _SINGLE_FRAME_FORMATS:
                    n = 1
                frame_counts.append(n)
        except OSError:
            frame_counts.append(1)
    total_frames = sum(frame_counts) or len(normalized)
    _report(0, total_frames)

    pages: list[bytes] = []
    base = 0
    for p, n in zip(normalized, frame_counts):
        part = _raster_image_file(
            p,
            use_max=use_max,
            on_progress=_report,
            check_cancel=_check_cancel,
            progress_base=base,
            progress_total=total_frames,
        )
        pages.extend(part)
        base += max(n, len(part))
    return pages
