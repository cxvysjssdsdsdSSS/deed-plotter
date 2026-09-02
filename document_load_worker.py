"""Background worker for document rasterization with page progress."""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from document_loader import DocumentLoadCancelled, load_documents
import page_cache


class DocumentLoadWorker(QThread):
    """Rasterize a deed PDF/image (or several images) off the GUI thread."""

    finished_ok = pyqtSignal(list)  # list[bytes] pages
    failed = pyqtSignal(str)
    page_progress = pyqtSignal(int, int)  # current_1based, total
    cancelled_done = pyqtSignal()

    def __init__(
        self,
        path: str | None = None,
        *,
        paths: list[str] | None = None,
        quality: str | None = None,
        page_cache_max: int | None = None,
    ):
        super().__init__()
        if paths:
            self.paths = [str(p) for p in paths]
        elif path:
            self.paths = [str(path)]
        else:
            self.paths = []
        self.path = self.paths[0] if self.paths else ""
        self.quality = quality
        self.page_cache_max = page_cache_max
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        if not self.paths:
            self.failed.emit("No files selected.")
            return

        # Disk cache is single-source only.
        if len(self.paths) == 1:
            cached = page_cache.try_load(self.paths[0], self.quality)
            if self._cancelled:
                self.cancelled_done.emit()
                return
            if cached is not None:
                n = len(cached)
                self.page_progress.emit(n, n)
                self.finished_ok.emit(cached)
                return

        try:
            pages = load_documents(
                self.paths,
                quality=self.quality,
                on_progress=lambda cur, tot: self.page_progress.emit(cur, tot),
                is_cancelled=lambda: self._cancelled,
            )
        except DocumentLoadCancelled:
            self.cancelled_done.emit()
            return
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(str(exc))
            else:
                self.cancelled_done.emit()
            return

        if len(self.paths) == 1:
            # Fail-open: disk errors must not block finished_ok.
            # Verbatim bytes — parse resume hashes these PNGs.
            # Cancel during save still keeps the in-memory rasters.
            page_cache.save(
                self.paths[0],
                pages,
                self.quality,
                is_cancelled=lambda: self._cancelled,
                max_entries=self.page_cache_max,
            )
        self.finished_ok.emit(pages)
