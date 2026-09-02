"""Sequential per-page AI parse orchestration (one API hang-up per page)."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

import parse_cache
from cogo import Call

API_KEY_ENV = "DEED_CURSOR_API_KEY"
# One dedicated subprocess hang-up per page — independent of total page count.
PER_PAGE_TIMEOUT_SEC = 300  # 5 minutes per page
TEXT_ONLY_TIMEOUT_SEC = 600
# Emitted via page_progress when all pages are done; the UI keys off this to
# fill the bar, so keep it a shared constant rather than a magic string.
MERGE_STATUS = "Merging page results…"

_CALL_FIELDS = {f.name for f in dataclasses.fields(Call)}


def _kill_proc_tree(proc: subprocess.Popen | None, *, reap: bool = False) -> None:
    """Kill the worker tree. Do not reap from the GUI thread — the worker's
    communicate() would then run concurrently with this one and freeze Qt."""
    if not proc or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            flags = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=flags,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        try:
            proc.kill()
        except OSError:
            pass
    if reap:
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass


_NUMERIC_CALL_FIELDS = ("distance", "radius", "arc_length", "chord_length")


def _result_from_payload(result: dict) -> dict:
    out = dict(result)

    def _calls(raw) -> list[Call]:
        calls = []
        for c in raw or []:
            if not isinstance(c, dict):
                continue
            kwargs = {k: v for k, v in c.items() if k in _CALL_FIELDS}
            # Nulls from the model must not survive into Call numerics — they
            # would crash the merge after every page is already cached.
            for field in _NUMERIC_CALL_FIELDS:
                try:
                    kwargs[field] = float(kwargs.get(field) or 0.0)
                except (TypeError, ValueError):
                    kwargs[field] = 0.0
            try:
                calls.append(Call(**kwargs))
            except TypeError:
                continue
        return calls

    out["calls"] = _calls(result.get("calls"))
    out["tie_calls"] = _calls(result.get("tie_calls"))
    out.setdefault("parse_warnings", [])
    return out


def build_cache_id_for_parse(
    cfg: dict,
    *,
    source_path: str,
    source_name: str,
    text: str,
    page_indices: list[int],
    images: list[bytes],
) -> str:
    page_hashes = [parse_cache.page_bytes_hash(png) for png in images]
    return parse_cache.make_cache_id(
        source_path=source_path or source_name,
        model=cfg.get("model", ""),
        image_quality=cfg.get("image_quality", "standard"),
        extra_text=text,
        page_indices=page_indices,
        page_hashes=page_hashes,
    )


def resumable_progress(
    cache_id: str, *, table_fingerprint: str | None = None,
) -> tuple[int, int] | None:
    """Return (done_count, total) if a mid-run cache exists; else None.

    *table_fingerprint* is the current call/tie hash. A stamped session that
    no longer matches (History Load / Open Job of a different extract) is not
    resumable.
    """
    session = parse_cache.load_session(cache_id)
    if not session:
        return None
    if table_fingerprint is not None and "table_fingerprint" in session:
        if str(session.get("table_fingerprint") or "") != str(table_fingerprint):
            return None
    total = len(session.get("page_indices") or [])
    if total <= 0:
        return None
    done = total - len(parse_cache.pending_page_indices(session))
    if done <= 0:
        return None
    return done, total


class SequentialParseWorker(QThread):
    """Parse one page at a time; cache each success; merge when finished."""

    finished_ok = pyqtSignal(dict, int)  # merged result, generation
    failed = pyqtSignal(str, int)
    page_progress = pyqtSignal(int, int, str)  # current_1based, total, status

    def __init__(
        self,
        cfg: dict,
        *,
        generation: int,
        images: list[bytes],
        page_indices: list[int],
        text: str = "",
        source_name: str = "",
        source_path: str = "",
        resume: bool = True,
        table_fingerprint: str = "",
    ):
        super().__init__()
        self.cfg = cfg
        self.generation = generation
        self.images = list(images)
        self.page_indices = list(page_indices)
        self.text = text or ""
        self.source_name = source_name
        self.source_path = source_path
        self.resume = resume
        self.table_fingerprint = str(table_fingerprint or "")
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self.cache_id = ""
        self.pages_completed = 0

    def cancel(self):
        self._cancelled = True
        _kill_proc_tree(self._proc)

    def run(self):
        try:
            if not self.images:
                self._run_text_only()
                return
            if len(self.images) != len(self.page_indices):
                raise ValueError(
                    f"images ({len(self.images)}) and page_indices "
                    f"({len(self.page_indices)}) length mismatch."
                )
            self._run_pages()
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(f"Unexpected error: {exc}", self.generation)

    def _run_text_only(self):
        if self._cancelled:
            return
        self.page_progress.emit(1, 1, "Parsing pasted text…")
        job = {
            "operation": "parse",
            "model": self.cfg.get("model", ""),
            "text": self.text,
            "image_paths": [],
        }
        result = self._run_one_job(job, timeout=TEXT_ONLY_TIMEOUT_SEC)
        if self._cancelled or result is None:
            return
        self.finished_ok.emit(result, self.generation)

    def _run_pages(self):
        if self._cancelled:
            return
        model = self.cfg.get("model", "")
        quality = self.cfg.get("image_quality", "standard")
        page_hashes = [parse_cache.page_bytes_hash(png) for png in self.images]
        if self._cancelled:
            return
        cache_id = parse_cache.make_cache_id(
            source_path=self.source_path or self.source_name,
            model=model,
            image_quality=quality,
            extra_text=self.text,
            page_indices=self.page_indices,
            page_hashes=page_hashes,
        )
        self.cache_id = cache_id

        if not self.resume:
            parse_cache.clear_session(cache_id)
        parse_max = int(self.cfg.get("parse_cache_max") or parse_cache.DEFAULT_PARSE_CACHE_MAX)
        parse_cache.prune_sessions(
            parse_max,
            keep_ids={cache_id} if self.resume else None,
        )

        session = parse_cache.load_session(cache_id) if self.resume else None
        if session is not None and "table_fingerprint" in session:
            stored = str(session.get("table_fingerprint") or "")
            if stored != str(self.table_fingerprint or ""):
                parse_cache.clear_session(cache_id)
                session = None
        if session is None:
            session = parse_cache.new_session(
                cache_id=cache_id,
                source_name=self.source_name,
                source_path=self.source_path,
                model=model,
                page_indices=self.page_indices,
                page_hashes=page_hashes,
                extra_text=self.text,
                table_fingerprint=self.table_fingerprint,
            )
            parse_cache.save_session(session)
            # Prune again so the new session does not leave max+1 on disk.
            parse_cache.prune_sessions(parse_max, keep_ids={cache_id})

        # Already-cached pages count for cancel messaging (don't wait for skips).
        self.pages_completed = len(parse_cache.done_page_results(session))

        total = len(self.page_indices)
        for step, (page_index, png) in enumerate(
            zip(self.page_indices, self.images), start=1
        ):
            if self._cancelled:
                return

            doc_page = page_index + 1
            entry = parse_cache.get_page_entry(session, page_index)
            if entry and entry.get("status") == "done" and entry.get("result"):
                self.page_progress.emit(step, total, "Cached — skipping…")
                continue

            self.page_progress.emit(step, total, "Parsing…")
            prior = parse_cache.prior_context_summary(session)
            job = {
                "operation": "parse_page",
                "model": model,
                "text": self.text,
                "page_number": step,
                "total_pages": total,
                "document_page": doc_page,
                "prior_context": prior,
            }
            # Before this call, only earlier steps can be cached. Page 1 of a
            # series has nothing to resume — don't claim otherwise.
            completed_before = step - 1
            if total <= 1:
                page_label = f"document page {doc_page}"
                recovery = ""
            elif completed_before <= 0:
                page_label = f"document page {doc_page} (step {step} of {total})"
                recovery = (
                    "Nothing was cached yet.\n"
                    "Run Parse again to retry from the start."
                )
            else:
                page_label = f"document page {doc_page} (step {step} of {total})"
                recovery = (
                    f"{completed_before} completed page(s) are in the parse cache — "
                    "run Parse again to resume, or choose Start over."
                )
            result = self._run_one_job(
                job,
                timeout=PER_PAGE_TIMEOUT_SEC,
                images=[png],
                page_label=page_label,
                recovery_hint=recovery,
            )
            if self._cancelled:
                return
            if result is None:
                return
            parse_cache.mark_page_done(session, page_index, result)
            self.pages_completed += 1
            session = parse_cache.load_session(cache_id) or session

        if self._cancelled:
            return

        page_results = parse_cache.done_page_results(session)
        if len(page_results) < total:
            pending = parse_cache.pending_page_indices(session)
            pending_docs = ", ".join(str(i + 1) for i in pending[:12])
            more = "" if len(pending) <= 12 else f", … (+{len(pending) - 12} more)"
            self.failed.emit(
                f"Parse incomplete — {len(pending)} page(s) still pending"
                f"{f' (document page(s) {pending_docs}{more})' if pending else ''}.\n\n"
                "Run Parse again to resume from the cache.",
                self.generation,
            )
            return

        self.page_progress.emit(total, total, MERGE_STATUS)
        try:
            merged = parse_cache.merge_page_results(page_results)
        except Exception as exc:
            # Keep the session so Start over is offered instead of a dead loop
            # of resume → merge crash.
            if not self._cancelled:
                self.failed.emit(
                    f"Could not merge cached page results: {exc}\n\n"
                    "Run Parse again and choose Start over.",
                    self.generation,
                )
            return
        if self._cancelled:
            # All pages are done; keep the cache so an immediate re-parse
            # resumes straight to the merge instead of re-billing pages.
            return
        session["status"] = "complete"
        parse_cache.save_session(session)
        parse_cache.clear_session(cache_id)
        self.finished_ok.emit(merged, self.generation)

    def _run_one_job(
        self,
        job: dict,
        *,
        timeout: int,
        images: list[bytes] | None = None,
        page_label: str = "",
        recovery_hint: str = "",
    ) -> dict | None:
        script = Path(__file__).with_name("parse_worker.py")
        # ignore_cleanup_errors: after a force-kill the child may briefly hold
        # page_XX.png on Windows; a cleanup PermissionError here would double-
        # emit failed via run()'s catch-all.
        with tempfile.TemporaryDirectory(
            prefix="deed_job_", ignore_cleanup_errors=True
        ) as tmp:
            tmp_path = Path(tmp)
            disk_job = {k: v for k, v in job.items() if k != "api_key"}
            if images:
                image_paths = []
                for i, png in enumerate(images, start=1):
                    if self._cancelled:
                        return None
                    p = tmp_path / f"page_{i:02d}.png"
                    p.write_bytes(png)
                    image_paths.append(str(p))
                disk_job["image_paths"] = image_paths
            else:
                disk_job.setdefault("image_paths", [])
            if self._cancelled:
                return None
            job_file = tmp_path / "job.json"
            job_file.write_text(json.dumps(disk_job), encoding="utf-8")

            env = os.environ.copy()
            env[API_KEY_ENV] = self.cfg.get("api_key", "")
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self._proc = subprocess.Popen(
                [sys.executable, str(script), str(job_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=flags,
                env=env,
            )
            # Close the cancel/Popen race: a cancel that fired between the
            # last _cancelled check and Popen saw _proc=None and killed
            # nothing — without this the worker would sit in communicate()
            # for the full page timeout.
            if self._cancelled:
                _kill_proc_tree(self._proc)
                self._proc = None
                return None
            try:
                stdout, stderr = self._proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_proc_tree(self._proc, reap=True)
                if not self._cancelled:
                    mins = max(1, timeout // 60)
                    detail = (
                        f"The AI page job timed out after {mins} minute(s)."
                    )
                    self.failed.emit(
                        self._format_page_failure(
                            page_label, detail, recovery_hint
                        ),
                        self.generation,
                    )
                return None
            finally:
                self._proc = None

            if self._cancelled:
                return None

            payload = (
                json.loads(stdout.strip().splitlines()[-1])
                if stdout.strip()
                else None
            )
            if payload is None:
                raise RuntimeError(stderr.strip()[-800:] or "Worker produced no output.")
            if not payload.get("ok"):
                err = payload.get("error", "Unknown AI error.")
                self.failed.emit(
                    self._format_page_failure(page_label, err, recovery_hint),
                    self.generation,
                )
                return None
            return _result_from_payload(payload["result"])

    @staticmethod
    def _format_page_failure(
        page_label: str, detail: str, recovery_hint: str = ""
    ) -> str:
        """Lead the error dialog with which document page failed."""
        if page_label:
            head = f"Failed on {page_label}."
            body = f"{head}\n\n{detail}"
        else:
            body = detail
        if recovery_hint:
            body = f"{body}\n\n{recovery_hint}"
        return body
