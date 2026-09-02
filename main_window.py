"""Main application window: document viewer, call table, boundary plot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QSize, QThread, QTimer, QSettings, QEventLoop, QEvent, pyqtSignal
from PyQt6.QtGui import QAction, QCloseEvent, QKeySequence
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSizePolicy, QSplitter, QStatusBar, QTabWidget, QTextEdit,
    QToolBar, QToolButton, QVBoxLayout, QWidget,
)
from PyQt6.sip import isdeleted

import history_store
import workspace_store
from busy_progress_dialog import BusyProgressDialog
from call_table import CallTable
from chat_context import build_deed_context
from chat_panel import ChatPanel
from chat_propose import (
    apply_call_actions,
    filter_out_of_range_actions,
    normalize_proposal,
    pending_after_apply,
    split_chat_proposal,
    wants_deed_proposal,
)
from chat_propose_dialog import ChatProposeDialog
from chat_propose_pending import (
    clear_pending as clear_pending_chat_actions,
    content_hash as chat_actions_content_hash,
    fingerprint_matches,
    load_pending as load_pending_chat_actions,
    restore_should_list_pending,
    save_pending as save_pending_chat_actions,
    sidecar_status as pending_sidecar_status,
    workspace_fingerprint,
)
from closure_panel import (
    ClosurePanel, area_is_reliable, is_traverse_closed, load_closure_tolerance,
)
from cogo import Call
from csv_calls import read_calls_csv, write_calls_csv
from details_panel import DetailsPanel
from document_load_worker import DocumentLoadWorker
from document_loader import format_quality_progress_line
from export_qa import collect_export_warnings
from legal_text_panel import LegalTextPanel
from multiline_field import MultilineField
from notes_panel import NotesPanel
from parse_orchestrator import (
    MERGE_STATUS,
    PER_PAGE_TIMEOUT_SEC,
    SequentialParseWorker,
    TEXT_ONLY_TIMEOUT_SEC,
    build_cache_id_for_parse,
    resumable_progress,
)
from parse_structure_qa import looks_like_open_line_survey, sync_open_line_parse_warnings
from pdf_viewer import DocumentViewer
from plot_widget import BoundaryPlot
from settings_dialog import SettingsDialog, load_settings
from toolbar_icons import make_undo_redo_icon

REPLOT_DEBOUNCE_MS = 200
AUTOSAVE_DEBOUNCE_MS = 800
PAGE_WARN_THRESHOLD = 15
_PLOT_DXF_TIP = (
    "Plot toggles only hide layers on screen — they do not change Export DXF."
)
API_KEY_ENV = "DEED_CURSOR_API_KEY"
SETTINGS_ORG = "DeedPlotter"
SETTINGS_APP = "DeedPlotter"


def _format_duration(seconds: float | int) -> str:
    """Human elapsed/budget stamp: ``45s`` or ``2m 05s``."""
    secs = max(0, int(seconds))
    mins, s = divmod(secs, 60)
    return f"{mins}m {s:02d}s" if mins else f"{s}s"


class DeedAIWorker(QThread):
    """Runs parse or chat in a subprocess (Cursor SDK cannot live on the Qt thread on Windows)."""

    finished_ok = pyqtSignal(dict, int)  # result, generation
    failed = pyqtSignal(str, int)

    def __init__(
        self,
        cfg: dict,
        job: dict,
        generation: int,
        *,
        images: list[bytes] | None = None,
    ):
        super().__init__()
        self.cfg = cfg
        self.job = dict(job)
        self.generation = generation
        self.images = list(images or [])
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self._kill_proc_tree()

    def _kill_proc_tree(self):
        """Kill the worker tree. Do not communicate() here — run() already waits."""
        if not self._proc or self._proc.poll() is not None:
            return
        if sys.platform == "win32":
            try:
                flags = subprocess.CREATE_NO_WINDOW
                subprocess.run(
                    ["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=flags,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
        else:
            try:
                self._proc.kill()
            except OSError:
                pass

    def run(self):
        script = Path(__file__).with_name("parse_worker.py")
        try:
            with tempfile.TemporaryDirectory(
                prefix="deed_job_", ignore_cleanup_errors=True
            ) as tmp:
                tmp_path = Path(tmp)
                disk_job = {k: v for k, v in self.job.items() if k != "api_key"}
                if self.images:
                    image_paths = []
                    for i, png in enumerate(self.images, start=1):
                        if self._cancelled:
                            return
                        p = tmp_path / f"page_{i:02d}.png"
                        p.write_bytes(png)
                        image_paths.append(str(p))
                    disk_job["image_paths"] = image_paths
                if self._cancelled:
                    return
                job_file = tmp_path / "job.json"
                job_file.write_text(json.dumps(disk_job), encoding="utf-8")

                if self._cancelled:
                    return
                env = os.environ.copy()
                env[API_KEY_ENV] = self.cfg.get("api_key", "")
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                self._proc = subprocess.Popen(
                    [sys.executable, str(script), str(job_file)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", creationflags=flags, env=env,
                )
                if self._cancelled:
                    self._kill_proc_tree()
                    return
                try:
                    stdout, stderr = self._proc.communicate(timeout=600)
                except subprocess.TimeoutExpired:
                    self._kill_proc_tree()
                    if not self._cancelled:
                        self.failed.emit("The AI job timed out after 10 minutes.", self.generation)
                    return
            if self._cancelled:
                return
            payload = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else None
            if payload is None:
                raise RuntimeError(stderr.strip()[-800:] or "Worker produced no output.")
            if not payload.get("ok"):
                self.failed.emit(payload.get("error", "Unknown AI error."), self.generation)
                return
            result = payload["result"]
            if str(self.job.get("operation") or "parse").lower() == "parse":
                result["calls"] = [Call(**c) for c in result["calls"]]
                result["tie_calls"] = [Call(**c) for c in result.get("tie_calls", [])]
                result.setdefault("parse_warnings", [])
            self.finished_ok.emit(result, self.generation)
        except Exception as exc:
            if not self._cancelled:
                self.failed.emit(f"Unexpected error: {exc}", self.generation)


ParseWorker = DeedAIWorker


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Deed Plotter — Metes & Bounds")
        self.resize(1680, 900)
        self._place_on_screen()
        self.worker: DeedAIWorker | SequentialParseWorker | None = None
        self._load_worker: DocumentLoadWorker | None = None
        self.progress: BusyProgressDialog | None = None
        self._load_generation = 0
        self._pending_load: dict | None = None
        self._pob_monument = ""
        self._pob_coordinates: dict | None = None
        self._tie_calls: list[Call] = []
        self._document_info: dict = {}
        self._point_of_beginning = ""
        self._general_notes = ""
        self._parse_warnings: list[str] = []
        self._last_result = None
        self._selected_pages: set[int] | None = None  # None = all pages
        self._source_name = ""
        self._source_path = ""
        self._source_paths: list[str] = []
        self._parse_model = ""
        self._parse_started = 0.0
        self._page_phase_started = 0.0  # per-page / chat-call clock
        self._page_timeout_sec = 0  # 0 = don't show page timeout budget
        self._show_page_clock = False
        self._progress_page_current: int | None = None
        self._loaded_image_quality = ""
        self._parse_generation = 0
        self._chat_generation = 0
        self._chat_stopping = False  # Cancel clicked; worker may still be dying
        self._propose_dialog_open = False  # modal review — block nested chat/pending
        self._undo_kind: list[str] = []
        self._redo_kind: list[str] = []
        self._undo_pending: list[dict | None] = []
        self._redo_pending: list[dict | None] = []
        self._chat_fail_generation: int | None = None
        self._job_path = ""
        self._chat_history: list[dict] = []
        self._apply_keep_side_actions: list[dict] = []
        self._history_available = history_store.has_entries()
        self._progress_page_note = ""
        self._progress_page_frac = ""
        # When Restore is Cancelled, keep workspace.json on disk until Discard,
        # Restore, or an explicit new document (Open Deed / Open Job /
        # successful Parse / History Load / Import CSV).
        self._preserve_workspace_file = False

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)

        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(REPLOT_DEBOUNCE_MS)
        self._replot_timer.timeout.connect(self.replot)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)
        self._autosave_timer.timeout.connect(self._autosave_workspace)

        self._build_toolbar()
        self._build_layout()
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Open a deed PDF or image to begin.")
        self._update_action_states()
        QTimer.singleShot(0, self._maybe_restore_workspace)

    def _place_on_screen(self):
        """Center on the usable desktop area and clamp size so it isn't off-screen."""
        from PyQt6.QtGui import QCursor

        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        margin = 48
        w = min(self.width(), max(640, avail.width() - margin))
        h = min(self.height(), max(480, avail.height() - margin))
        self.resize(w, h)
        x = avail.x() + max(0, (avail.width() - w) // 2)
        y = avail.y() + max(0, (avail.height() - h) // 2)
        self.move(x, y)

    # ---------- UI construction ----------

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)

        self.open_act = QAction("Open Deed…", self)
        self.open_act.setShortcut(QKeySequence.StandardKey.Open)
        self.open_act.setToolTip(
            "Load a deed PDF or image into the viewer.\n"
            "Multi-page PDFs show one page at a time on the left."
        )
        self.open_act.triggered.connect(self.open_document)
        tb.addAction(self.open_act)

        self.pages_act = QAction("Pages…", self)
        self.pages_act.setToolTip(
            "Choose which pages are sent to the AI when you parse.\n"
            "Uncheck cover sheets, signatures, or unrelated plats."
        )
        self.pages_act.triggered.connect(self.choose_pages)
        tb.addAction(self.pages_act)

        self.parse_act = QAction("Parse with AI", self)
        self.parse_act.setShortcut("Ctrl+R")
        self.parse_act.setToolTip(
            "Send the selected pages (and any pasted text) to the AI.\n"
            "Fills the call table, legal description, and deed details.\n"
            "Requires a Cursor API key in Settings."
        )
        self.parse_act.triggered.connect(self.run_parse)
        tb.addAction(self.parse_act)

        tb.addSeparator()

        self.csv_act = QAction("Export CSV…", self)
        self.csv_act.setToolTip(
            "Save the call table (bearings, distances, monuments, confidence) as a CSV file."
        )
        self.csv_act.triggered.connect(self.export_csv)
        tb.addAction(self.csv_act)

        self.import_csv_act = QAction("Import CSV…", self)
        self.import_csv_act.setToolTip(
            "Load boundary calls from a CSV into the call table.\n"
            "Asks before replacing an existing traverse."
        )
        self.import_csv_act.triggered.connect(self.import_csv)
        tb.addAction(self.import_csv_act)

        self.dxf_act = QAction("Export DXF…", self)
        self.dxf_act.setToolTip(
            "Export the plotted boundary as a DXF file for AutoCAD / Civil 3D.\n"
            "Includes linework, labels, monuments, ties, deed details, and a "
            "closure report.\n"
            "Plot checkboxes only hide layers on screen — they do not change "
            "this DXF."
        )
        self.dxf_act.triggered.connect(self.export_dxf)
        tb.addAction(self.dxf_act)

        self.view_dxf_act = QAction("View DXF…", self)
        self.view_dxf_act.setToolTip(
            "Open a DXF in the built-in viewer (CAD-style layers, pan/zoom).\n"
            "Use this to preview exports before Civil 3D."
        )
        self.view_dxf_act.triggered.connect(self.view_dxf)
        tb.addAction(self.view_dxf_act)

        tb.addSeparator()

        self.save_job_act = QAction("Save Job…", self)
        self.save_job_act.setShortcut(QKeySequence.StandardKey.Save)
        self.save_job_act.setToolTip(
            "Save the current traverse, ties, details, notes, paste, chat, and "
            "pending actions to a job file."
        )
        self.save_job_act.triggered.connect(self.save_job)
        tb.addAction(self.save_job_act)

        self.open_job_act = QAction("Open Job…", self)
        self.open_job_act.setToolTip("Open a previously saved job file into the workspace.")
        self.open_job_act.triggered.connect(self.open_job)
        tb.addAction(self.open_job_act)

        self.undo_act = QAction(make_undo_redo_icon(redo=False), "Undo", self)
        self.undo_act.setToolTip(
            "Undo last call-table, tie-table, or Chat Apply change (Ctrl+Z)"
        )
        self.undo_act.triggered.connect(self._undo_edit)
        tb.addAction(self.undo_act)
        undo_btn = tb.widgetForAction(self.undo_act)
        if isinstance(undo_btn, QToolButton):
            undo_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        self.redo_act = QAction(make_undo_redo_icon(redo=True), "Redo", self)
        self.redo_act.setToolTip(
            "Redo last undone call-table, tie-table, or Chat Apply change (Ctrl+Y)"
        )
        self.redo_act.triggered.connect(self._redo_edit)
        tb.addAction(self.redo_act)
        redo_btn = tb.widgetForAction(self.redo_act)
        if isinstance(redo_btn, QToolButton):
            redo_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)

        tb.addSeparator()

        self.history_act = QAction("History…", self)
        self.history_act.setToolTip(
            "Browse past AI parse results and reload one into the workspace.\n"
            "Right-click an entry to delete it."
        )
        self.history_act.triggered.connect(self.open_history)
        tb.addAction(self.history_act)

        self.settings_act = QAction("Settings…", self)
        self.settings_act.setToolTip(
            "Cursor API key, AI model, deed image quality, cache limits, and notes "
            "always appended to parses."
        )
        self.settings_act.triggered.connect(self.open_settings)
        tb.addAction(self.settings_act)

        # Chat toggle sits on the right, above the chat column.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.chat_act = QAction("Chat Pane", self)
        self.chat_act.setCheckable(True)
        self.chat_act.setChecked(True)
        self.chat_act.setShortcut("Ctrl+Shift+C")
        self.chat_act.setToolTip(
            "Show or hide the chat pane (Ctrl+Shift+C).\n"
            "While the assistant is thinking, the pane stays open so Cancel "
            "stays on screen."
        )
        self.chat_act.toggled.connect(self._toggle_chat_pane)
        tb.addAction(self.chat_act)

    def _build_layout(self):
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        left = QWidget()
        lv = QVBoxLayout(left)
        self.viewer = DocumentViewer()
        lv.addWidget(self.viewer, stretch=1)

        lv.addWidget(QLabel("Optional: paste legal description text / notes for the AI:"))
        self.text_input = MultilineField(
            title="AI paste / notes",
            max_height=100,
            placeholder=(
                "If you have the description as text (or want to add hints), "
                "paste it here…"
            ),
            popout_size=(720, 480),
        )
        self.text_input.setToolTip(
            "Optional text sent when you Parse with AI.\n"
            "With a deed open: sent as extra context with the page images.\n"
            "With no deed: parsed as the legal description / notes alone.\n"
            "Useful for OCR hints, partial descriptions, or text-only deeds.\n"
            "Use Pop out for a larger editing window."
        )
        self.text_input.textChanged.connect(self._update_action_states)
        self.text_input.textChanged.connect(self._schedule_autosave)
        self.text_input.set_on_popout_dirty_changed(self._update_action_states)
        lv.addWidget(self.text_input)
        splitter.addWidget(left)

        center = QWidget()
        cv = QVBoxLayout(center)
        self.tabs = QTabWidget()

        table_page = QWidget()
        tv = QVBoxLayout(table_page)
        self.table = CallTable()
        self.table.callsEdited.connect(self._replot_timer.start)
        self.table.callsEdited.connect(self._update_action_states)
        self.table.callsEdited.connect(self._schedule_autosave)
        self.table.undoPushed.connect(lambda: self._note_undo("call"))
        self.table.itemSelectionChanged.connect(self._sync_highlight)
        tv.addWidget(self.table)
        self.table.cellDoubleClicked.connect(self._maybe_edit_call)
        btns = QHBoxLayout()
        self.add_btn = QPushButton("Add Call")
        self.add_btn.setToolTip(
            "Append a 100-ft due-north course to the end of the call table."
        )
        self.add_btn.clicked.connect(self.table.add_blank_row)
        self.edit_btn = QPushButton("Edit Call…")
        self.edit_btn.setToolTip(
            "Open the call editor for the selected row.\n"
            "Quadrant bearing + DMS, curves, monument, and description.\n"
            "Tip: double-click the # column to edit too."
        )
        self.edit_btn.clicked.connect(self._edit_selected_call)
        self.del_btn = QPushButton("Delete Selected")
        self.del_btn.setToolTip("Remove the selected row(s) from the call table.")
        self.del_btn.clicked.connect(self.table.delete_selected_rows)
        btns.addWidget(self.add_btn)
        btns.addWidget(self.edit_btn)
        btns.addWidget(self.del_btn)
        btns.addStretch()
        tv.addLayout(btns)
        self.tabs.addTab(table_page, "Call Table")
        self._table_tab = table_page

        plot_page = QWidget()
        pv = QVBoxLayout(plot_page)
        controls = QHBoxLayout()
        self.chk_courses = QCheckBox("Bearing/distance labels")
        self.chk_courses.setChecked(True)
        self.chk_courses.setToolTip(
            "Show course labels on the plot: call number, bearing, and distance\n"
            f"beside each boundary line.\n{_PLOT_DXF_TIP}"
        )
        self.chk_corners = QCheckBox("Corner numbers")
        self.chk_corners.setChecked(True)
        self.chk_corners.setToolTip(
            "Number each corner (1, 2, 3…) at the boundary vertices.\n"
            f"POB is the green square (see legend).\n{_PLOT_DXF_TIP}"
        )
        self.chk_monuments = QCheckBox("Monuments")
        self.chk_monuments.setChecked(True)
        self.chk_monuments.setToolTip(
            "Show monument callouts (iron rods, concrete monuments, etc.)\n"
            "with leader lines at corners that have monument text.\n"
            f"{_PLOT_DXF_TIP}"
        )
        self.chk_ties = QCheckBox("Tie calls")
        self.chk_ties.setChecked(True)
        self.chk_ties.setToolTip(
            "Show the commencement-to-POB tie run as a dashed gray line,\n"
            f"with a POC marker at the start.\n{_PLOT_DXF_TIP}"
        )
        self.chk_grid = QCheckBox("Grid")
        self.chk_grid.setChecked(True)
        self.chk_grid.setToolTip("Show or hide the background easting/northing grid.")
        fit_btn = QPushButton("Fit View")
        fit_btn.setToolTip(
            "Zoom to fit the traverse (Ctrl+F on the Plot tab, or Ctrl+Shift+F anytime)."
        )
        controls.addWidget(self.chk_courses)
        controls.addWidget(self.chk_corners)
        controls.addWidget(self.chk_monuments)
        controls.addWidget(self.chk_ties)
        controls.addWidget(self.chk_grid)
        controls.addStretch()
        controls.addWidget(fit_btn)
        pv.addLayout(controls)
        self.plot = BoundaryPlot()
        self.chk_courses.toggled.connect(self.plot.set_show_courses)
        self.chk_corners.toggled.connect(self.plot.set_show_corners)
        self.chk_monuments.toggled.connect(self.plot.set_show_monuments)
        self.chk_ties.toggled.connect(self.plot.set_show_ties)
        self.chk_grid.toggled.connect(self.plot.set_show_grid)
        fit_btn.clicked.connect(self.plot.fit_view)
        self.plot.courseClicked.connect(self._on_course_clicked)
        pv.addWidget(self.plot, stretch=1)
        self.tabs.addTab(plot_page, "Plot")
        self._plot_tab = plot_page

        self.closure = ClosurePanel()
        self.tabs.addTab(self.closure, "Closure")
        self._closure_tab = self.closure

        self.legal_text = LegalTextPanel()
        self.tabs.addTab(self.legal_text, "Legal Description")

        self.details = DetailsPanel()
        self.details.tiesEdited.connect(self._on_ties_edited)
        self.details.documentInfoEdited.connect(self._on_document_info_edited)
        self.details.pobEdited.connect(self._on_pob_edited)
        self.details.undoPushed.connect(lambda: self._note_undo("tie"))
        self.tabs.addTab(self.details, "Deed Details")

        self.notes = NotesPanel()
        self.notes.callActivated.connect(self._jump_to_call)
        self.tabs.addTab(self.notes, "Notes && Warnings")
        self._notes_tab = self.notes

        cv.addWidget(self.tabs, stretch=1)

        self.report_label = QLabel("")
        self.report_label.setWordWrap(True)
        self.report_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cv.addWidget(self.report_label)
        splitter.addWidget(center)

        self.chat = ChatPanel()
        self.chat.sendRequested.connect(self._on_chat_send)
        self.chat.clearRequested.connect(self._on_chat_clear)
        self.chat.cancelRequested.connect(self._cancel_ai)
        self.chat.pendingRequested.connect(self._review_pending_chat_actions)
        self.chat.setMinimumWidth(280)
        splitter.addWidget(self.chat)

        self._main_splitter = splitter
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([480, 720, 360])

        # Ctrl+Shift+F always fits the plot. Ctrl+Z/Y/E/F and Delete are
        # handled in eventFilter so Chat and paste boxes keep those keys.
        fit_any_sc = QAction(self)
        fit_any_sc.setShortcut("Ctrl+Shift+F")
        fit_any_sc.triggered.connect(self.plot.fit_view)
        self.addAction(fit_any_sc)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        QTimer.singleShot(0, self._restore_ui_layout)

    # ---------- Button/action state ----------

    def _ai_busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _worker_is_chat(self) -> bool:
        return isinstance(self.worker, DeedAIWorker) and str(
            getattr(self.worker, "job", {}).get("operation") or "parse"
        ).lower() == "chat"

    def _load_busy(self) -> bool:
        return self._load_worker is not None and self._load_worker.isRunning()

    def _app_busy(self) -> bool:
        return (
            self._ai_busy()
            or self._load_busy()
            or bool(getattr(self, "_propose_dialog_open", False))
        )

    def _pages_action_enabled(self) -> bool:
        """Pages… when there are 2+ pages, or a 1-page deed with an empty subset."""
        n = len(self.viewer.pages)
        if n <= 0:
            return False
        if n > 1:
            return True
        return self._page_subset_clipped_empty(n)

    def _page_subset_clipped_empty(self, n_pages: int | None = None) -> bool:
        if self._selected_pages is None:
            return False
        n = len(self.viewer.pages) if n_pages is None else n_pages
        return not any(0 <= i < n for i in self._selected_pages)

    def _warn_no_pages_selected(self, *, while_parse: bool = False) -> None:
        if while_parse:
            QMessageBox.warning(
                self, "No Pages Selected",
                "Pages… has no pages in this document (the previous subset was "
                "clipped to zero).\n\n"
                "Use Pages… to choose pages before Parse. These images will not "
                "be sent until you do.",
            )
            return
        QMessageBox.warning(
            self, "No Pages Selected",
            "None of the previously selected Pages… subset exist in "
            "this document.\n\n"
            "Use Pages… to choose pages before Parse. Parse will not "
            "send these images until you do.",
        )

    def _warn_some_source_files_missing(self, missing: list[str], *, kind: str) -> None:
        lead = (
            "History restored, but some deed image(s) were not found:\n"
            if kind == "history" else
            "Workspace restored, but some deed image(s) were not found:\n"
        )
        QMessageBox.warning(
            self, "Some Source Files Missing",
            lead
            + "\n".join(missing)
            + "\n\nLoaded the files that are still available.",
        )

    def _update_action_states(self):
        busy = self._app_busy()
        has_input = (
            bool(self.viewer.pages)
            or bool(self.text_input.toPlainText().strip())
        )
        has_calls = self.table.rowCount() > 0
        has_plot = self._last_result is not None and bool(self._last_result.segments)
        has_selection = bool(self.table.selectedIndexes())
        has_work = self._workspace_busy()

        self.open_act.setEnabled(not busy)
        self.parse_act.setEnabled(has_input and not busy)
        self.pages_act.setEnabled(self._pages_action_enabled() and not busy)
        self.csv_act.setEnabled(has_calls and not busy)
        self.import_csv_act.setEnabled(not busy)
        self.dxf_act.setEnabled(has_plot and not busy)
        self.view_dxf_act.setEnabled(not busy)
        self.save_job_act.setEnabled(has_work and not busy)
        self.open_job_act.setEnabled(not busy)
        self.settings_act.setEnabled(not busy)
        can_undo = bool(self._undo_kind) or self.table.can_undo() or self.details.can_undo_ties()
        can_redo = bool(self._redo_kind) or self.table.can_redo() or self.details.can_redo_ties()
        self.undo_act.setEnabled(can_undo and not busy)
        self.redo_act.setEnabled(can_redo and not busy)
        self.history_act.setEnabled(not busy and self._history_available)
        chat_thinking = (
            self._worker_is_chat() and self._ai_busy() and not self._chat_stopping
        )
        self.chat_act.setEnabled(not chat_thinking)
        if hasattr(self, "_main_splitter"):
            self._main_splitter.setCollapsible(2, not chat_thinking)
            if chat_thinking:
                self.chat.setVisible(True)
                sizes = self._main_splitter.sizes()
                if len(sizes) >= 3 and sizes[2] < 200:
                    total = sum(sizes) or 1560
                    self._main_splitter.setSizes([
                        int(total * 0.30), int(total * 0.45), int(total * 0.25),
                    ])
        self._update_pending_actions_button(busy=busy)
        self.add_btn.setEnabled(not busy)
        self.edit_btn.setEnabled(has_selection and not busy)
        self.del_btn.setEnabled(has_selection and not busy)
        self.table.set_interactive(not busy)
        self.details.set_interactive(not busy)
        if hasattr(self, "chk_ties"):
            has_ties = (
                self.details.has_plottable_ties()
                if hasattr(self, "details") else False
            )
            self.chk_ties.setEnabled(has_ties)
            self.chk_ties.setToolTip(
                "Show the commencement-to-POB tie run as a dashed gray line,\n"
                f"with a POC marker at the start.\n{_PLOT_DXF_TIP}"
                if has_ties else
                "No tie calls in this deed — nothing to show."
            )
        if hasattr(self, "chat"):
            if not self._ai_busy():
                # Worker finished after Cancel — drop the Stopping strip.
                if self._chat_stopping:
                    self.chat.set_thinking(False)
                self._chat_stopping = False
            if self._ai_busy():
                # Stopping copy lives on the thinking strip; avoid a duplicate.
                if self._chat_stopping:
                    chat_msg = ""
                elif self._worker_is_chat():
                    chat_msg = "Waiting for assistant…"
                else:
                    chat_msg = ""
            elif self._load_busy():
                chat_msg = "Loading document…"
            else:
                chat_msg = ""
            self.chat.set_busy(busy, chat_msg)

    def _workspace_busy(self) -> bool:
        """True when autosave / restore should treat the workspace as occupied.

        Includes an open deed path/pages even before parse — so Discard→Open
        still gets a session file. Replace confirms use `_has_replaceable_content`.
        """
        if self._has_replaceable_content():
            return True
        # A loaded deed must be restored even before parse / with an empty table.
        if self._source_path or self.viewer.pages:
            return True
        return False

    def _has_replaceable_content(self) -> bool:
        """True when Parse / Import / Open Job would clobber user work.

        An open deed image alone does not count — otherwise Discard → Open →
        Parse shows a spurious Replace Traverse dialog. Chat history and
        pending proposals do count so Open Job cannot silently wipe them.
        """
        if self.table.rowCount() > 0:
            return True
        if self.details.get_tie_calls():
            return True
        if self.text_input.toPlainText().strip() or self.text_input.popout_has_edits():
            return True
        if self.legal_text.plain_text().strip():
            return True
        if self.details.get_document_info():
            return True
        pob_text, pob_mon, pob_coords = self.details.get_pob_info()
        if pob_text or pob_mon or pob_coords:
            return True
        if self._general_notes or self._parse_warnings:
            return True
        if self._chat_history:
            return True
        if load_pending_chat_actions():
            return True
        return False

    def _parse_would_replace_work(self) -> bool:
        """True when Parse would clobber table / ties / details / legal / notes.

        Paste and chat are not existing work for this confirm — Parse keeps both.
        """
        if self.table.rowCount() > 0:
            return True
        if self.details.get_tie_calls():
            return True
        if self.legal_text.plain_text().strip():
            return True
        if self.details.get_document_info():
            return True
        pob_text, pob_mon, pob_coords = self.details.get_pob_info()
        if pob_text or pob_mon or pob_coords:
            return True
        if self._general_notes or self._parse_warnings:
            return True
        return False

    def _confirm_replace_workspace(self, action: str, *, detail: str | None = None) -> bool:
        if not self._has_replaceable_content():
            return True
        body = detail or (
            f"{action} will replace the current call table, ties, deed details, "
            f"legal description, notes, paste, and chat.\n\n"
            "Unsaved job changes and pending parse or chat work will be discarded.\n\n"
            "Continue?"
        )
        return QMessageBox.question(self, action, body) == QMessageBox.StandardButton.Yes

    def _chat_actions_fingerprint(self) -> dict:
        ties = self.details.get_tie_calls() if hasattr(self, "details") else []
        calls = self.table.get_calls() if hasattr(self, "table") else []
        return workspace_fingerprint(
            n_calls=len(calls),
            n_ties=len(ties),
            source_path=self._source_path or "",
            content_hash=chat_actions_content_hash(calls, ties),
        )

    def _parse_table_fingerprint(self) -> str:
        calls = self.table.get_calls() if hasattr(self, "table") else []
        ties = self.details.get_tie_calls() if hasattr(self, "details") else []
        return chat_actions_content_hash(calls, ties)

    def _clear_pending_chat_actions(self, *, status: str | None = None) -> bool:
        try:
            clear_pending_chat_actions()
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            self._update_pending_actions_button()
            return False
        self._update_pending_actions_button()
        self._schedule_autosave()
        if status:
            self.statusBar().showMessage(status)
        return True

    def _save_pending_chat_actions(
        self,
        actions: list,
        skipped: list | None = None,
        *,
        fingerprint: dict | None = None,
    ) -> bool:
        try:
            save_pending_chat_actions(
                actions, skipped, fingerprint=fingerprint,
            )
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            return False
        return True

    def _update_pending_actions_button(self, *, busy: bool | None = None) -> None:
        if busy is None:
            busy = self._app_busy()
        n = 0
        data = load_pending_chat_actions()
        if data and data.get("actions"):
            current = self._chat_actions_fingerprint()
            if fingerprint_matches(data.get("fingerprint"), current):
                n = len(data["actions"])
            # Stale fingerprint: keep the sidecar but do not look reviewable.
            # Click-to-wipe stays in `_review_pending_chat_actions` as a safety net.
        if hasattr(self, "chat"):
            self.chat.set_pending_count(n, busy=busy)

    def _show_chat_propose_dialog(
        self,
        actions: list[dict],
        skipped: list[str],
        *,
        window_title: str = "Review proposed actions",
        from_pending: bool = False,
    ) -> None:
        """Save as pending, show review dialog, apply / keep remaining / discard."""
        resolved = self._resolve_pending_proposal_conflict(
            actions, skipped, from_pending=from_pending,
        )
        if resolved is None:
            return
        actions, skipped = resolved
        actions, range_notes = filter_out_of_range_actions(
            actions,
            n_boundary=len(self.table.get_calls()),
            n_ties=len(self.details.get_tie_calls()),
        )
        if range_notes:
            skipped = list(skipped) + range_notes
        if not actions:
            detail = "\n".join(f"• {s}" for s in skipped[:12]) if skipped else ""
            QMessageBox.warning(
                self,
                "No valid actions",
                "None of the proposed actions match the current call/tie table."
                + (f"\n\n{detail}" if detail else ""),
            )
            if from_pending:
                self._clear_pending_chat_actions()
                self._update_pending_actions_button()
                self._schedule_autosave()
            return
        fp = self._chat_actions_fingerprint()
        self._save_pending_chat_actions(actions, skipped, fingerprint=fp)
        self._update_pending_actions_button()

        def _persist_dialog_actions() -> bool:
            # Health parity: keep pending in sync with Edit / Delete / Include.
            ok = self._save_pending_chat_actions(
                dlg.snapshot_actions(), skipped, fingerprint=fp,
            )
            self._update_pending_actions_button()
            self._schedule_autosave()
            return ok

        dlg = ChatProposeDialog(
            actions, skipped, self, window_title=window_title,
            calls=self.table.get_calls(),
            tie_calls=self.details.get_tie_calls(),
            document_info=self.details.get_document_info(),
            on_actions_changed=_persist_dialog_actions,
            tolerance_ft=load_closure_tolerance(),
        )
        self._propose_dialog_open = True
        self._update_action_states()
        try:
            accepted = dlg.exec()
        finally:
            self._propose_dialog_open = False
            self._update_action_states()
        if not accepted:
            # Later / Esc — persist any in-dialog Edit / Delete / unchecks.
            ok = _persist_dialog_actions()
            n = len(dlg.snapshot_actions())
            if ok:
                self.statusBar().showMessage(
                    f"{n} proposed action(s) kept as pending."
                    if n else "No pending chat actions."
                )
            return
        if dlg.discarded:
            if not self._clear_pending_chat_actions(
                status="Discarded pending chat actions.",
            ):
                return
            self.chat.append("System", "Discarded proposed actions.")
            return
        if dlg.applied:
            parse_ok = self._apply_chat_actions(dlg.applied)
        else:
            parse_ok = True
        remaining, dropped_shifted = pending_after_apply(
            dlg.applied or [],
            dlg.remaining_actions(),
            None if parse_ok else self._apply_keep_side_actions,
        )
        new_fp = self._chat_actions_fingerprint()
        if remaining:
            # Rebind fingerprint to the post-apply table. Content updates change
            # content_hash but leftover indexes are still valid when counts match.
            ok = self._save_pending_chat_actions(remaining, skipped, fingerprint=new_fp)
            if ok:
                if parse_ok:
                    kept_msg = (
                        f"Applied some actions — {len(remaining)} still pending."
                    )
                else:
                    side = {
                        a.get("action") for a in self._apply_keep_side_actions
                    }
                    kind = (
                        "Export cancelled"
                        if side and side <= {"export_csv", "export_dxf"}
                        else "Parse cancelled"
                    )
                    kept_msg = (
                        f"{kind} — {len(remaining)} action(s) kept as pending."
                    )
                self.statusBar().showMessage(kept_msg)
            if dropped_shifted:
                self.chat.append(
                    "System",
                    "Some leftover proposed actions were cleared because add/delete "
                    "changed call numbers on that table — ask again for anything "
                    "still needed.",
                )
        else:
            self._clear_pending_chat_actions()
            if dropped_shifted:
                self.chat.append(
                    "System",
                    "Leftover proposed actions were cleared because add/delete "
                    "changed call numbers — ask again for anything still needed.",
                )
        self._update_pending_actions_button()
        self._schedule_autosave()

    def _resolve_pending_proposal_conflict(
        self,
        new_actions: list[dict],
        new_skipped: list[str],
        *,
        from_pending: bool,
    ) -> tuple[list[dict], list[str]] | None:
        """Health-style Replace / Merge / Cancel when unapplied pending exists."""
        if from_pending:
            return list(new_actions), list(new_skipped)
        existing = load_pending_chat_actions()
        if not existing or not existing.get("actions"):
            return list(new_actions), list(new_skipped)
        current = self._chat_actions_fingerprint()
        if not fingerprint_matches(existing.get("fingerprint"), current):
            # Stale Later batch is not reviewable — don't Merge it into this proposal.
            return list(new_actions), list(new_skipped)
        old_actions = list(existing["actions"])
        # Re-opening the same batch — no conflict.
        if new_actions == old_actions:
            return list(new_actions), list(new_skipped)

        box = QMessageBox(self)
        box.setWindowTitle("Pending actions")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            f"You already have {len(old_actions)} unapplied pending action(s)."
        )
        box.setInformativeText(
            "Replace them with this new batch, merge both, or cancel?"
        )
        replace_btn = box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        merge_btn = box.addButton("Merge", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(merge_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return None
        old_skipped = list(existing.get("skipped") or [])
        if clicked is merge_btn:
            merged = old_actions + list(new_actions)
            skipped = list(dict.fromkeys(old_skipped + list(new_skipped)))
            return merged, skipped
        _ = replace_btn
        return list(new_actions), list(new_skipped)

    def _review_pending_chat_actions(self) -> None:
        if self._app_busy():
            return
        data = load_pending_chat_actions()
        if not data or not data.get("actions"):
            self._update_pending_actions_button()
            QMessageBox.information(
                self, "No pending actions",
                "There are no unapplied chat proposals.",
            )
            return
        current = self._chat_actions_fingerprint()
        if not fingerprint_matches(data.get("fingerprint"), current):
            if self._clear_pending_chat_actions():
                QMessageBox.warning(
                    self, "Pending actions cleared",
                    "The call table or ties changed since these actions were proposed, "
                    "so their call numbers may no longer match.\n\n"
                    "Ask the assistant again if you still want the changes.",
                )
            return
        self._show_chat_propose_dialog(
            list(data["actions"]),
            list(data.get("skipped") or []),
            window_title="Review pending actions",
            from_pending=True,
        )

    # ---------- Document handling ----------

    def _make_busy_progress(
        self,
        *,
        title: str,
        label: str,
        cancel_slot,
        min_width: int = 360,
        maximum: int = 0,
        bar_text_visible: bool | None = None,
    ) -> BusyProgressDialog:
        """Progress dialog — maximum=0 busy (parse/chat); >0 determinate (load)."""
        dlg = BusyProgressDialog(
            label,
            title=title,
            parent=self,
            min_width=min_width,
            maximum=maximum,
            bar_text_visible=bar_text_visible,
        )
        dlg.canceled.connect(cancel_slot)
        return dlg

    def _start_document_load(
        self,
        path: str | None = None,
        *,
        paths: list[str] | None = None,
        purpose: str,
        **ctx,
    ) -> None:
        """Rasterize *path* (or *paths*) in a worker with a Page n/x progress dialog."""
        if self._app_busy():
            return
        file_paths = [str(p) for p in (paths or []) if str(p).strip()]
        if not file_paths and path:
            file_paths = [str(path)]
        if not file_paths:
            return
        cfg = load_settings()
        self._load_generation += 1
        gen = self._load_generation
        primary = file_paths[0]
        self._pending_load = {
            "path": primary,
            "paths": file_paths,
            "purpose": purpose,
            "generation": gen,
            "quality": cfg.get("image_quality"),
            **ctx,
        }

        if len(file_paths) == 1:
            name = Path(primary).name
        else:
            name = f"{Path(primary).name} + {len(file_paths) - 1} more"
        # Determinate from first paint (max=1 placeholder). Worker reports 0/N
        # from the same open used to rasterize — no separate peek open.
        self._progress_page_note = "Opening…"
        self._progress_page_frac = "—"
        self._progress_base = f"Loading document…\n{name}"
        # Full label (Page + Elapsed) from the first paint — avoids vertical jump.
        initial = (
            f"{self._progress_base}\n"
            f"Page {self._progress_page_frac} — {self._progress_page_note}\n"
            f"Elapsed: 0s"
        )
        self.progress = self._make_busy_progress(
            title="Loading Document",
            label=initial,
            cancel_slot=self._cancel_document_load,
            min_width=360,
            maximum=1,
            bar_text_visible=False,
        )
        self._parse_started = time.monotonic()
        self._page_phase_started = 0.0
        self._page_timeout_sec = 0
        self._show_page_clock = False
        self._elapsed_timer.start()

        self._load_worker = DocumentLoadWorker(
            paths=file_paths,
            quality=cfg.get("image_quality"),
            page_cache_max=cfg.get("page_cache_max"),
        )
        # BlockingQueued: forces the GUI to apply setValue/label before the
        # next page. Queued + label-only / polling sat at 0/N until the end.
        self._load_worker.page_progress.connect(
            self._on_load_page_progress,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._load_worker.finished_ok.connect(self._on_document_load_done)
        self._load_worker.failed.connect(self._on_document_load_failed)
        self._load_worker.cancelled_done.connect(self._on_document_load_cancelled)
        self._load_worker.finished.connect(self._on_load_worker_finished)
        self._load_worker.start()
        self.progress.show()
        QApplication.processEvents()
        self._update_action_states()

    def _on_load_page_progress(self, current: int, total: int) -> None:
        if self.progress is None:
            return
        if "Cancelling" in (self.progress.labelText() or ""):
            return
        # Miss / rasterize starts with (0, N). Do not call setValue while hidden —
        # QProgressDialog with minimumDuration(0) auto-shows on setValue.
        if current <= 0 and not self.progress.isVisible():
            self.progress.show()
        if not self.progress.isVisible():
            return
        if total > 0:
            if self.progress.maximum() != total:
                self.progress.setMaximum(total)
            self.progress.set_bar_text_visible(True)
            self.progress.setValue(current)
            self._progress_page_frac = f"{current}/{total}"
        self._progress_page_note = "Opening…" if current <= 0 else "Loading…"
        self.statusBar().showMessage(
            f"Loading document — Page {self._progress_page_frac}"
        )
        self._tick_elapsed()

    def _clear_viewer_pages_keep_traverse(self, *, status: str) -> None:
        """Drop raster pages so Parse cannot send old images under a new source.

        Used when history/workspace already applied calls/path but image reload
        was cancelled or failed.
        """
        if self.viewer.pages:
            self.viewer.set_pages([])
        self._loaded_image_quality = ""
        self.statusBar().showMessage(status)

    def _drop_unloaded_source_identity(self) -> None:
        """Forget deed name/path/Pages… when no page images are loaded.

        History/Restore cancel and fail leave the restored calls but no rasters.
        A later paste Parse must not be stored in History as that PDF.
        """
        if self.viewer.pages:
            return
        self._source_name = ""
        self._source_path = ""
        self._source_paths = []
        self._selected_pages = None
        self._job_path = ""
        self.details.set_export_stem("")

    def _on_load_worker_finished(self):
        w = self.sender()
        if not isinstance(w, DocumentLoadWorker):
            w = self._load_worker
        # Only clear the slot if this finished signal is for the current worker.
        if w is self._load_worker:
            self._load_worker = None
        if w is not None:
            w.deleteLater()
        self._update_action_states()

    def _cancel_document_load(self):
        if self._load_worker is not None:
            self._load_worker.cancel()
        if self.progress is not None:
            # QProgressDialog hides on Cancel; keep it up until the worker ends.
            self.progress.show()
            self.progress.setLabelText(
                f"{self._progress_base}\nCancelling…"
            )
            self.progress.setCancelButton(None)
        self._update_action_states()

    def _on_document_load_cancelled(self):
        # Cancel button already bumped generation / closed the dialog; this
        # only covers cooperative cancel that finishes after the dialog is gone.
        if self._pending_load is not None:
            pending = self._pending_load
            pending_gen = pending.get("generation")
            if pending_gen == self._load_generation:
                self._pending_load = None
                purpose = pending.get("purpose", "open")
                if purpose in ("history", "workspace"):
                    self._clear_viewer_pages_keep_traverse(
                        status=(
                            "Document load cancelled — page images cleared so they "
                            "cannot mismatch the restored calls. Open the deed "
                            "again before Parse."
                        ),
                    )
                    self._drop_unloaded_source_identity()
                else:
                    self.statusBar().showMessage("Document load cancelled.")
        if self.progress is not None:
            self._close_progress()
        self._update_action_states()

    def _on_document_load_failed(self, message: str):
        pending = self._pending_load
        # Ignore stale failures after Cancel (pending cleared / generation bumped).
        if pending is None or pending.get("generation") != self._load_generation:
            if self.progress is not None:
                self._close_progress()
            self._update_action_states()
            return
        self._pending_load = None
        self._close_progress()
        purpose = pending.get("purpose", "open")
        if purpose in ("history", "workspace"):
            self._clear_viewer_pages_keep_traverse(
                status="Document reload failed — page images cleared.",
            )
            self._drop_unloaded_source_identity()
        self._update_action_states()
        title = "Load Error" if purpose == "open" else "Could Not Reload Document"
        if purpose == "open":
            QMessageBox.critical(
                self, title, f"Could not load document:\n{message}")
        else:
            QMessageBox.warning(
                self, title,
                f"Restored, but the source file could not be opened:\n"
                f"{pending.get('path', '')}\n\n{message}\n\n"
                "Page images were cleared so Parse cannot use a mismatched deed. "
                "Open the document again if you need the pages.",
            )
        self.statusBar().showMessage("Document load failed.")

    def _on_document_load_done(self, pages: list):
        pending = self._pending_load
        self._pending_load = None
        self._close_progress()
        if pending is None or pending.get("generation") != self._load_generation:
            self._update_action_states()
            return
        purpose = pending.get("purpose", "open")
        path = pending.get("path", "")
        if purpose == "open":
            self._preserve_workspace_file = False
            if pending.get("clear_after_load"):
                self._clear_traverse()
            else:
                # Keep traverse: deed file changed — don't Save over the old job.
                self._job_path = ""
            if len(pages) >= PAGE_WARN_THRESHOLD:
                QMessageBox.information(
                    self, "Large Document",
                    f"This document has {len(pages)} pages.\n\n"
                    f"Use Pages… to uncheck cover sheets and unrelated plats before parsing "
                    f"— sending {len(pages)} pages is slower and more expensive.",
                )
            self.viewer.set_pages(pages)
            self._loaded_image_quality = str(pending.get("quality") or "")
            if pending.get("clear_after_load"):
                self._selected_pages = None
            elif self._selected_pages is not None:
                kept = {i for i in self._selected_pages if 0 <= i < len(pages)}
                self._selected_pages = kept
                if not kept:
                    self._warn_no_pages_selected()
            file_paths = pending.get("paths") or ([path] if path else [])
            if len(file_paths) > 1:
                names = [Path(p).name for p in file_paths]
                self._source_name = f"{names[0]} + {len(names) - 1} more"
                self._source_paths = [str(Path(p).resolve()) for p in file_paths]
                self._source_path = self._source_paths[0]
                stem = Path(names[0]).stem
            else:
                self._source_name = Path(path).name if path else ""
                self._source_path = str(Path(path).resolve()) if path else ""
                self._source_paths = [self._source_path] if self._source_path else []
                stem = Path(self._source_name).stem if self._source_name else ""
            self.details.set_export_stem(stem)
            shown = self._source_name or path
            if pending.get("clear_after_load"):
                self.statusBar().showMessage(f"Loaded {len(pages)} page(s) from {shown}")
            elif self._selected_pages is not None:
                n_sel = len(self._selected_pages)
                self.statusBar().showMessage(
                    f"Loaded {len(pages)} page(s) from {shown} — "
                    f"Pages… still {n_sel} of {len(pages)} (clipped to this file)."
                )
            else:
                self.statusBar().showMessage(f"Loaded {len(pages)} page(s) from {shown}")
            self._update_action_states()
            self._autosave_workspace()
            return
        # history / workspace: refresh viewer pages to match restored source
        self.viewer.set_pages(pages)
        self._loaded_image_quality = str(pending.get("quality") or "")
        if self._selected_pages is not None:
            kept = {i for i in self._selected_pages if 0 <= i < len(pages)}
            self._selected_pages = kept
            if not kept:
                self._warn_no_pages_selected()
        missing = [str(p) for p in (pending.get("missing_paths") or []) if str(p).strip()]
        if missing:
            kind = "history" if purpose == "history" else "workspace"
            self._warn_some_source_files_missing(missing, kind=kind)
        self.statusBar().showMessage(
            f"Loaded {len(pages)} page(s) from {Path(path).name}"
        )
        self._update_action_states()

    def open_document(self):
        if self._app_busy():
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Open Deed",
            filter=(
                "Deed documents (*.pdf *.png *.jpg *.jpeg *.tif *.tiff "
                "*.bmp *.webp *.heic *.heif);;All files (*)"
            ),
        )
        if not paths:
            return
        clear_after_load = False
        if self._has_replaceable_content() or self._selected_pages is not None:
            box = QMessageBox(self)
            box.setWindowTitle("Open Deed")
            box.setText(
                "The workspace already has calls, ties, deed details, notes, "
                "paste, chat, pending chat actions, or a Pages… subset.\n\n"
                "Keep traverse leaves those in place, keeps the Pages… subset "
                "(clipped to the new page count), and opens the new document.\n"
                "Clear traverse also drops chat, paste, legal text, pending "
                "actions, and the session file.\n\n"
                "Keep the current traverse, clear it, or cancel?"
            )
            keep_btn = box.addButton("Keep traverse", QMessageBox.ButtonRole.AcceptRole)
            clear_btn = box.addButton("Clear traverse", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is None or clicked == box.button(QMessageBox.StandardButton.Cancel):
                return
            if clicked == clear_btn:
                clear_after_load = True
            # keep_btn: leave table/paste as-is
            _ = keep_btn

        self._start_document_load(
            paths=paths, purpose="open", clear_after_load=clear_after_load)

    def choose_pages(self):
        from page_selector_dialog import PageSelectorDialog
        dlg = PageSelectorDialog(self.viewer.pages, self._selected_pages, self)
        if not dlg.exec():
            return
        picked = dlg.selected_indices()
        self._selected_pages = set(picked) if picked is not None else None
        if self._selected_pages is None:
            self.statusBar().showMessage("All pages will be sent to the AI parse.")
        else:
            self.statusBar().showMessage(
                f"{len(self._selected_pages)} of {len(self.viewer.pages)} pages "
                "will be sent to the AI parse."
            )
        self._update_action_states()
        self._schedule_autosave()

    def _clear_traverse(self):
        self.table.set_calls([])
        self.table.clear_edit_history()
        self._reset_undo_kinds()
        self._tie_calls = []
        self._document_info = {}
        self._pob_monument = ""
        self._pob_coordinates = None
        self._point_of_beginning = ""
        self._general_notes = ""
        self._parse_warnings = []
        self._last_result = None
        self.legal_text.set_text("")
        self.details.set_parse_info({}, [])
        self.details.set_pob_info("", "", None)
        self.notes.clear_parse_info()
        self.notes.update_content([], None, expected_open=False)
        self.closure.update_content(None, expected_open=False)
        self.report_label.setText("")
        self.text_input.close_popout(force=True)
        self.text_input.clear()
        self.plot.clear_plot()
        self.details.update_coordinates([], None)
        self._chat_history.clear()
        if hasattr(self, "chat"):
            self.chat.clear_transcript()
            self.chat.clear_input()
        self._preserve_workspace_file = False
        self._job_path = ""
        try:
            workspace_store.clear_workspace()
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
        self._clear_pending_chat_actions()
        self._update_action_states()
        self._schedule_autosave()

    # ---------- AI parsing ----------

    def run_parse(self) -> bool:
        if self._app_busy():
            return False
        cfg = load_settings()
        if not cfg["api_key"]:
            QMessageBox.warning(self, "API Key Needed", "Enter your API key in Settings first.")
            self.open_settings()
            return False
        # Empty Pages… subset: abort before Replace Traverse (table stays).
        if self.viewer.pages and self._page_subset_clipped_empty():
            self._warn_no_pages_selected(while_parse=True)
            return False
        if self._parse_would_replace_work():
            if QMessageBox.question(
                self, "Replace Traverse?",
                "Parsing will replace the current call table, ties, deed details, "
                "legal description, and notes.\n\n"
                "Any hand edits that are not saved to a job file will be lost.\n\n"
                "Continue?",
            ) != QMessageBox.StandardButton.Yes:
                return False

        # Parse uses the committed small box. OK copies Pop out back; X is not OK.
        paste = self.text_input.toPlainText().strip()
        always = (cfg.get("always_append") or "").strip()
        text_parts = [p for p in (paste, always) if p]
        text = "\n\n".join(text_parts)

        pages = self.viewer.pages
        if self._selected_pages is not None:
            pages = [p for i, p in enumerate(pages) if i in self._selected_pages]
        # Match Parse button enablement: need pages or paste. Settings
        # "always append" alone is not enough (it is supplemental context).
        if not pages and not paste:
            QMessageBox.information(self, "Nothing to Parse", "Open a deed document or paste description text first.")
            return False
        if pages and len(pages) >= PAGE_WARN_THRESHOLD:
            if QMessageBox.question(
                self, "Large Parse",
                f"You are about to parse {len(pages)} pages "
                "(one AI call per page).\n\n"
                "This may be slow and costly. Continue, or cancel and use Pages… first?",
            ) != QMessageBox.StandardButton.Yes:
                return False

        page_indices: list[int] = []
        resume = True
        if pages:
            if self._selected_pages is not None:
                page_indices = sorted(
                    i for i in self._selected_pages if i < len(self.viewer.pages)
                )
            else:
                page_indices = list(range(len(pages)))
            if len(page_indices) != len(pages):
                QMessageBox.warning(
                    self, "Page Selection Error",
                    "Selected pages do not match the loaded images. "
                    "Re-open the deed or use Pages… again.",
                )
                return False

        page_note = ""
        if self.viewer.pages and len(pages) < len(self.viewer.pages):
            page_note = f" ({len(pages)} of {len(self.viewer.pages)} pages)"
        self.statusBar().showMessage(f"Sending deed to AI for parsing…{page_note}")

        n_pages = len(pages) if pages else 0
        if n_pages:
            how = (
                f"One API call per page ({n_pages} page"
                f"{'s' if n_pages != 1 else ''}).\n"
                f"Each page times out after {PER_PAGE_TIMEOUT_SEC // 60} min; "
                "completed pages stay cached."
            )
            page_budget = PER_PAGE_TIMEOUT_SEC
        else:
            how = (
                f"Parsing pasted text (one API call, "
                f"{TEXT_ONLY_TIMEOUT_SEC // 60} min timeout)."
            )
            page_budget = TEXT_ONLY_TIMEOUT_SEC
        self._progress_page_note = "Preparing…"
        self._progress_page_frac = f"0/{n_pages}" if n_pages else "1/1"
        self._progress_page_current = None
        quality_line = ""
        if n_pages:
            used_q = self._loaded_image_quality or cfg.get("image_quality")
            quality_line = (
                f"\n{format_quality_progress_line(used_q, settings_id=cfg.get('image_quality'))}"
            )
        self._progress_base = (
            f"Parsing deed with AI…{page_note}\n"
            f"Model: {cfg['model']}"
            f"{quality_line}\n"
            f"{how}"
        )
        self._show_page_clock = True
        self._page_timeout_sec = page_budget
        budget = _format_duration(page_budget)
        initial = (
            f"{self._progress_base}\n"
            f"Page {self._progress_page_frac} — {self._progress_page_note}\n"
            f"Elapsed: 0s total · this page 0s / {budget}"
        )
        prepare_gen = self._parse_generation
        self.progress = self._make_busy_progress(
            title="AI Parsing",
            label=initial,
            cancel_slot=self._cancel_ai,
            min_width=400,
            maximum=n_pages,
        )
        self.progress.show()
        QApplication.processEvents()
        if self._parse_prepare_cancelled(prepare_gen):
            return False

        # Resume key must match the rasters in memory, not a Settings quality
        # that has not been re-opened yet.
        if pages:
            loaded_q = (self._loaded_image_quality or "").strip()
            if loaded_q:
                cfg = {**cfg, "image_quality": loaded_q}

        if pages:
            cache_id = build_cache_id_for_parse(
                cfg,
                source_path=self._source_path,
                source_name=self._source_name,
                text=text,
                page_indices=page_indices,
                images=pages,
            )
            # Hash ran on the GUI thread; deliver a Cancel click queued then.
            QApplication.processEvents()
            if self._parse_prepare_cancelled(prepare_gen):
                return False
            progress = resumable_progress(
                cache_id, table_fingerprint=self._parse_table_fingerprint(),
            )
            if progress is not None:
                done, total = progress
                box = QMessageBox(self)
                if done >= total:
                    box.setWindowTitle("Retry Merge?")
                    box.setText(
                        f"A previous parse finished all {total} page(s) but could "
                        f"not combine them.\n\n"
                        "Retry the merge from the cache, or start over and "
                        "re-parse every page?"
                    )
                    resume_btn = box.addButton(
                        "Retry merge", QMessageBox.ButtonRole.AcceptRole
                    )
                else:
                    box.setWindowTitle("Resume Parse?")
                    box.setText(
                        f"A previous parse of this deed stopped after {done} of "
                        f"{total} pages.\n\n"
                        "Resume from the cache, or start over and re-parse "
                        "every page?"
                    )
                    resume_btn = box.addButton(
                        "Resume", QMessageBox.ButtonRole.AcceptRole
                    )
                restart_btn = box.addButton(
                    "Start over", QMessageBox.ButtonRole.DestructiveRole
                )
                box.addButton(QMessageBox.StandardButton.Cancel)
                box.exec()
                clicked = box.clickedButton()
                if clicked is None or clicked == box.button(QMessageBox.StandardButton.Cancel):
                    self._close_progress()
                    return False
                resume = clicked == resume_btn
                if clicked == restart_btn:
                    resume = False

        if self._parse_prepare_cancelled(prepare_gen):
            return False

        self._progress_page_note = "Starting…"
        self._parse_started = time.monotonic()
        self._page_phase_started = self._parse_started
        self._elapsed_timer.start()

        self._parse_generation += 1
        self._parse_model = cfg["model"]
        table_fp = self._parse_table_fingerprint()
        if pages:
            self.worker = SequentialParseWorker(
                cfg,
                generation=self._parse_generation,
                images=pages,
                page_indices=page_indices,
                text=text,
                source_name=self._source_name,
                source_path=self._source_path,
                resume=resume,
                table_fingerprint=table_fp,
            )
            self.worker.page_progress.connect(
                self._on_parse_page_progress,
                Qt.ConnectionType.BlockingQueuedConnection,
            )
        else:
            self.worker = SequentialParseWorker(
                cfg,
                generation=self._parse_generation,
                images=[],
                page_indices=[],
                text=text,
                source_name="",
                source_path="",
            )
            self.worker.page_progress.connect(
                self._on_parse_page_progress,
                Qt.ConnectionType.BlockingQueuedConnection,
            )
        self.worker.finished_ok.connect(self._parse_done)
        self.worker.failed.connect(self._parse_failed)
        self.worker.finished.connect(self._on_ai_thread_finished)
        self.worker.start()
        self.progress.show()
        self._update_action_states()
        return True

    def _on_ai_thread_finished(self):
        """Close a leftover Cancelling… parse dialog once the worker actually exits."""
        w = self.sender()
        if w is self.worker and not w.isRunning():
            self.worker = None
        if not self._ai_busy() and not self._load_busy() and self.progress is not None:
            self._close_progress()
        self._update_action_states()

    def _parse_prepare_cancelled(self, prepare_gen: int) -> bool:
        """True when Cancel closed the Preparing dialog before the worker starts."""
        return self.progress is None or self._parse_generation != prepare_gen

    def _on_parse_page_progress(self, current: int, total: int, status: str):
        if self.progress is None:
            # Parse was cancelled/finished; ignore late signals from the
            # winding-down worker so they don't overwrite the status bar.
            return
        if "Cancelling" in (self.progress.labelText() or ""):
            return
        if total > 0 and self.progress.maximum() != total:
            self.progress.setMaximum(total)
        if total > 0:
            self.progress.setValue(min(current, total))
            self._progress_page_frac = f"{current}/{total}"
        prev_note = self._progress_page_note
        prev_current = self._progress_page_current
        self._progress_page_note = status
        if total > 0:
            self._progress_page_current = current
        parsing = status.startswith("Parsing") or "pasted text" in status.lower()
        # Restart when a new page's API call begins. Status stays "Parsing…"
        # across pages, so the page number — not the label — is the clock key.
        if parsing:
            entered_parse = not (prev_note or "").startswith("Parsing")
            page_changed = prev_current is not None and current != prev_current
            if entered_parse or page_changed or self._page_phase_started <= 0:
                self._page_phase_started = time.monotonic()
            self._show_page_clock = True
        elif status.startswith("Cached") or status == MERGE_STATUS:
            self._page_phase_started = 0.0
            self._show_page_clock = False
        frac = self._progress_page_frac or "?"
        self.statusBar().showMessage(f"AI parse — Page {frac} — {status}")
        self._tick_elapsed()

    def _tick_elapsed(self):
        if self.progress is None:
            self._elapsed_timer.stop()
            return
        if "Cancelling" in (self.progress.labelText() or ""):
            return
        total_stamp = _format_duration(time.monotonic() - self._parse_started)
        # Always keep the Page line when we have a frac placeholder so height
        # stays stable (load/parse seed frac before show).
        if self._progress_page_frac:
            page_line = f"\nPage {self._progress_page_frac}"
            if self._progress_page_note:
                page_line += f" — {self._progress_page_note}"
        else:
            page_line = f"\n{self._progress_page_note}" if self._progress_page_note else ""
        if self._show_page_clock and self._page_phase_started > 0:
            page_stamp = _format_duration(
                time.monotonic() - self._page_phase_started
            )
            if self._page_timeout_sec > 0:
                budget = _format_duration(self._page_timeout_sec)
                elapsed_line = (
                    f"\nElapsed: {total_stamp} total · "
                    f"this page {page_stamp} / {budget}"
                )
            else:
                elapsed_line = (
                    f"\nElapsed: {total_stamp} total · this page {page_stamp}"
                )
        else:
            elapsed_line = f"\nElapsed: {total_stamp}"
        self.progress.setLabelText(
            f"{self._progress_base}{page_line}{elapsed_line}"
        )

    def _close_progress(self):
        self._elapsed_timer.stop()
        dlg = self.progress
        self.progress = None
        if dlg is None:
            return
        for slot in (self._cancel_ai, self._cancel_document_load):
            try:
                dlg.canceled.disconnect(slot)
            except TypeError:
                pass
        dlg.prepare_close()
        # hide() first — close() alone has left this dialog visible after load.
        dlg.hide()
        dlg.close()
        dlg.deleteLater()

    def _cancel_ai(self):
        """Cancel in-flight parse (progress dialog) or chat (inline Cancel)."""
        is_chat = (
            self.worker is not None
            and self.worker.isRunning()
            and self._worker_is_chat()
        )
        was_multipage = (
            isinstance(self.worker, SequentialParseWorker)
            and len(getattr(self.worker, "images", []) or []) > 1
        )
        pages_done = int(getattr(self.worker, "pages_completed", 0) or 0)
        if self.worker is not None:
            self.worker.cancel()
        if is_chat:
            self._chat_generation += 1
            self._chat_stopping = True
            if self._chat_history and self._chat_history[-1].get("role") == "user":
                self._chat_history.pop()
            if hasattr(self, "chat"):
                self.chat.set_stopping()
                self.chat.append("System", "Cancelled.")
            self.statusBar().showMessage("Chat cancelled.")
            self._schedule_autosave()
        else:
            self._parse_generation += 1
            if was_multipage and pages_done > 0:
                self.statusBar().showMessage(
                    "Parse cancelled — completed pages remain in the parse cache."
                )
            elif was_multipage:
                self.statusBar().showMessage(
                    "Parse cancelled — no pages were cached yet."
                )
            else:
                self.statusBar().showMessage("Parse cancelled.")
            if self.worker is not None and self.worker.isRunning():
                if self.progress is not None:
                    self.progress.show()
                    self.progress.setLabelText(
                        f"{self._progress_base}\nCancelling…"
                    )
                    self.progress.setCancelButton(None)
            else:
                self._close_progress()
        self._update_action_states()

    def _cancel_parse(self):
        self._cancel_ai()

    def _parse_done(self, result: dict, generation: int):
        if generation != self._parse_generation:
            return
        self._close_progress()
        calls: list[Call] = result["calls"]
        if not self.viewer.pages:
            self._drop_unloaded_source_identity()
        try:
            history_store.add_entry(
                self._source_name,
                self._parse_model,
                result,
                self._source_path,
                source_paths=list(self._source_paths),
                selected_pages=(
                    sorted(self._selected_pages)
                    if self._selected_pages is not None else None
                ),
            )
            self._history_available = True
        except OSError as exc:
            QMessageBox.warning(
                self, "History",
                f"Parse succeeded, but history could not be saved:\n{exc}",
            )
        if not calls:
            QMessageBox.information(self, "No Calls Found", "The AI could not find any boundary calls in this document.")
            self._apply_parse_result(result)
            self.statusBar().showMessage("Parse complete — no calls found.")
            return
        self._apply_parse_result(result)
        self._select_post_parse_tab(calls)
        secs = int(time.monotonic() - self._parse_started)
        self.statusBar().showMessage(f"Parsed {len(calls)} call(s) in {secs}s.")

    def _select_post_parse_tab(self, calls: list[Call]):
        has_low = any(c.confidence.lower() == "low" for c in calls)
        has_medium = any(c.confidence.lower() == "medium" for c in calls)
        if has_low or has_medium:
            self.tabs.setCurrentWidget(self._table_tab)
            # Jump to first flagged call.
            for i, c in enumerate(calls):
                if c.confidence.lower() in ("low", "medium"):
                    self._jump_to_call(i, open_editor=False)
                    break
            return
        # Prefer Closure when the traverse is open.
        if self._last_result is not None:
            open_traverse = not is_traverse_closed(self._last_result)
            if open_traverse:
                self.tabs.setCurrentWidget(self._closure_tab)
                return
        self.tabs.setCurrentWidget(self._plot_tab)

    def _apply_parse_result(self, result: dict):
        """Populate all panels from a parse result (fresh or from history)."""
        from parse_structure_qa import warn_possible_misfiled_boundary_tie

        # Successful apply is the new document — not worker.start() / Cancel / fail.
        self._preserve_workspace_file = False
        self.table.set_calls(result["calls"])
        self.table.clear_edit_history()
        self._reset_undo_kinds()
        self._pob_monument = result.get("pob_monument", "")
        self._pob_coordinates = result.get("pob_coordinates") or None
        self._point_of_beginning = result.get("point_of_beginning", "")
        self._general_notes = result.get("general_notes", "")
        warnings = list(result.get("parse_warnings") or [])
        for w in warn_possible_misfiled_boundary_tie(
            result.get("legal_description", "") or "",
            result.get("calls") or [],
            result.get("tie_calls") or [],
            document_info=result.get("document_info") or {},
        ):
            if w not in warnings:
                warnings.append(w)
        self._parse_warnings = warnings
        self.legal_text.set_text(result.get("legal_description", ""))
        self._tie_calls = result.get("tie_calls", [])
        self._document_info = result.get("document_info", {})
        self.details.set_parse_info(self._document_info, self._tie_calls)
        self.details.set_pob_info(
            self._point_of_beginning, self._pob_monument, self._pob_coordinates)
        self.notes.set_parse_info(
            self._point_of_beginning,
            self._general_notes,
            self._pob_monument,
            self._parse_warnings,
        )
        self.replot()
        self._clear_pending_chat_actions()
        self._update_action_states()
        self._schedule_autosave()

    def _parse_failed(self, message: str, generation: int):
        if generation != self._parse_generation:
            return
        self._close_progress()
        first = (message or "").strip().splitlines()[0] if message else "Parse failed."
        self.statusBar().showMessage(first)
        self._update_action_states()
        QMessageBox.critical(self, "AI Parse Failed", message)

    def open_history(self):
        from history_dialog import HistoryDialog
        dlg = HistoryDialog(self)
        dlg.exec()
        if dlg.selected_result is not None:
            self._preserve_workspace_file = False
            self._job_path = ""
            self.text_input.close_popout(force=True)
            self.text_input.clear()
            self._apply_parse_result(dlg.selected_result)
            self._chat_history.clear()
            if hasattr(self, "chat"):
                self.chat.clear_transcript()
                self.chat.clear_input()
            self._clear_pending_chat_actions()
            source = dlg.selected_source or "pasted text"
            self._source_name = dlg.selected_source or ""
            self.details.set_export_stem(
                Path(self._source_name).stem if self._source_name else ""
            )
            paths = list(getattr(dlg, "selected_source_paths", None) or [])
            if not paths and dlg.selected_source_path:
                paths = [dlg.selected_source_path]
            pages_sel = getattr(dlg, "selected_pages", None)
            if pages_sel is not None:
                self._selected_pages = set(pages_sel)
            else:
                self._selected_pages = None
            if paths:
                self._source_paths = [str(Path(p).resolve()) if Path(p).exists() else p for p in paths]
                # Prefer resolved existing paths for reload; keep missing for messaging.
                existing = [p for p in self._source_paths if Path(p).is_file()]
                self._source_path = existing[0] if existing else self._source_paths[0]
                if existing:
                    missing = [p for p in self._source_paths if not Path(p).is_file()]
                    if missing:
                        self._selected_pages = None
                    self._start_document_load(
                        paths=existing, purpose="history",
                        missing_paths=missing,
                    )
                else:
                    self._clear_viewer_pages_keep_traverse(
                        status="History restored — source file missing; page images cleared.",
                    )
                    QMessageBox.warning(
                        self, "Source File Missing",
                        f"History restored, but the deed file was not found:\n"
                        f"{self._source_path}\n\n"
                        "Page images were cleared so Parse cannot use a mismatched "
                        "deed. Open the document again if you need the pages.",
                    )
                    self._drop_unloaded_source_identity()
            else:
                self._source_path = ""
                self._source_paths = []
                # Restored calls without a path — don't keep prior deed rasters.
                self._clear_viewer_pages_keep_traverse(
                    status="History restored (no source path) — page images cleared.",
                )
            self.statusBar().showMessage(f"Restored parse of {source} from history.")
            self.tabs.setCurrentWidget(self._table_tab)
            self._autosave_workspace()
        self._history_available = history_store.has_entries()
        self._update_action_states()

    # ---------- Workspace / jobs ----------

    def _current_state(self) -> dict:
        pages = None
        if self._selected_pages is not None:
            pages = sorted(self._selected_pages)
        # Always trust the details panel — empty ties / cleared meta must persist.
        self._tie_calls = self.details.get_tie_calls()
        self._document_info = self.details.get_document_info()
        self._point_of_beginning, self._pob_monument, self._pob_coordinates = (
            self.details.get_pob_info()
        )
        return workspace_store.pack_state(
            source_name=self._source_name,
            source_path=self._source_path,
            source_paths=list(self._source_paths),
            text_input=self.text_input.toPlainText(),
            calls=self.table.get_calls(),
            tie_calls=self._tie_calls,
            document_info=self._document_info,
            legal_description=self.legal_text.plain_text(),
            point_of_beginning=self._point_of_beginning,
            pob_monument=self._pob_monument,
            pob_coordinates=self._pob_coordinates,
            general_notes=self._general_notes,
            parse_warnings=self._parse_warnings,
            selected_pages=pages,
            chat_history=list(self._chat_history),
            pending_chat=load_pending_chat_actions(),
        )

    def _schedule_autosave(self):
        self._autosave_timer.start()

    def _autosave_workspace(self):
        try:
            if self._preserve_workspace_file:
                # Restore Cancel: keep the deferred session until Restore/Discard
                # or an explicit new document (Open Deed / Open Job /
                # successful Parse / History Load / Import CSV).
                return
            if self._workspace_busy():
                workspace_store.save_workspace(self._current_state())
            else:
                workspace_store.clear_workspace()
        except OSError as exc:
            self.statusBar().showMessage(f"Autosave failed: {exc}", 8000)

    def _maybe_restore_workspace(self):
        state = workspace_store.load_workspace()
        if state is None:
            return
        when = state.get("saved_at", "")
        n = len(state.get("calls") or [])
        source = state.get("source_name") or "untitled"
        bits = [f"{n} call(s) from {source}"]
        if state.get("tie_calls"):
            bits.append(f"{len(state['tie_calls'])} tie(s)")
        if (state.get("text_input") or "").strip():
            bits.append("paste / AI notes")
        if state.get("legal_description") or state.get("document_info") or state.get("point_of_beginning"):
            bits.append("deed details / legal text")
        if state.get("chat_history"):
            bits.append(f"{len(state['chat_history'])} chat turn(s)")
        if restore_should_list_pending(
            state.get("pending_chat"), workspace_state=state,
        ):
            bits.append("pending chat actions")
        msg = "Restore the previous workspace?\n\n" + " · ".join(bits)
        if when:
            msg += f"\nSaved {when}"
        box = QMessageBox(self)
        box.setWindowTitle("Restore Workspace")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(msg)
        restore_btn = box.addButton("Restore", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == discard_btn:
            try:
                workspace_store.clear_workspace()
            except OSError as exc:
                QMessageBox.critical(self, "Save Failed", str(exc))
                self._preserve_workspace_file = True
                return
            self._preserve_workspace_file = False
            self._clear_pending_chat_actions()
            self.statusBar().showMessage("Discarded previous workspace.")
            return
        if clicked != restore_btn:
            # Cancel: keep file for a later launch; don't let empty-UI autosave wipe it.
            # Drop orphan sidecar pending — it would make the empty UI look "busy" and
            # overwrite the deferred workspace on quit.
            self._preserve_workspace_file = True
            self._clear_pending_chat_actions()
            return
        self._preserve_workspace_file = False
        self._load_state(state, reload_source=True, prefer_pending_sidecar=True)
        self._autosave_workspace()
        self.statusBar().showMessage("Restored previous workspace.")

    def _load_state(
        self, state: dict, *, reload_source: bool = False,
        prefer_pending_sidecar: bool = False,
    ):
        self._preserve_workspace_file = False
        self.text_input.close_popout(force=True)
        self.text_input.setPlainText(state.get("text_input", ""))
        self._source_name = state.get("source_name", "")
        self._source_path = state.get("source_path", "")
        raw_paths = state.get("source_paths") or []
        if isinstance(raw_paths, list) and raw_paths:
            self._source_paths = [str(p) for p in raw_paths if str(p).strip()]
        elif self._source_path:
            self._source_paths = [self._source_path]
        else:
            self._source_paths = []
        self.details.set_export_stem(Path(self._source_name).stem if self._source_name else "")
        self._pob_monument = state.get("pob_monument", "")
        self._pob_coordinates = state.get("pob_coordinates")
        self._point_of_beginning = state.get("point_of_beginning", "")
        self._general_notes = state.get("general_notes", "")
        self._parse_warnings = list(state.get("parse_warnings") or [])
        self._document_info = dict(state.get("document_info") or {})
        self._tie_calls = list(state.get("tie_calls") or [])
        self.table.set_calls(list(state.get("calls") or []))
        self.table.clear_edit_history()
        self._reset_undo_kinds()
        legal = state.get("legal_description", "")
        self.legal_text.set_text(legal)
        self.details.set_parse_info(self._document_info, self._tie_calls)
        self.details.set_pob_info(
            self._point_of_beginning, self._pob_monument, self._pob_coordinates)
        self.notes.set_parse_info(
            self._point_of_beginning, self._general_notes,
            self._pob_monument, self._parse_warnings,
        )
        sel = state.get("selected_pages")
        self._selected_pages = set(sel) if isinstance(sel, list) else None
        self._restore_chat_session(state, prefer_sidecar=prefer_pending_sidecar)
        if reload_source and self._source_paths:
            existing = [p for p in self._source_paths if Path(p).is_file()]
            missing = [p for p in self._source_paths if not Path(p).is_file()]
            if existing:
                if missing:
                    self._selected_pages = None
                self._start_document_load(
                    paths=existing, purpose="workspace",
                    missing_paths=missing,
                )
            else:
                QMessageBox.warning(
                    self, "Source File Missing",
                    f"Workspace restored, but the deed file was not found:\n"
                    f"{self._source_path or self._source_paths[0]}\n\n"
                    "Page images were cleared so Parse cannot use a mismatched "
                    "deed. Open the document again if you need the page images.",
                )
                self._clear_viewer_pages_keep_traverse(
                    status="Workspace restored — source file missing; page images cleared.",
                )
                self._drop_unloaded_source_identity()
        elif reload_source and not self._source_path and self._source_name:
            # Older autosaves omitted the path when only a deed was open.
            self._clear_viewer_pages_keep_traverse(
                status="Workspace restored — no source path; page images cleared.",
            )
            QMessageBox.information(
                self, "Source Not Saved",
                "Workspace calls were restored, but no source file path was saved "
                "with this session.\n\n"
                "Use Open Deed… to reload the PDF/image.",
            )
        elif reload_source:
            # Paste-only / no-source job — don't keep the previous deed rasters.
            self._clear_viewer_pages_keep_traverse(
                status="Opened job has no deed file — page images cleared.",
            )
        self.replot()
        self._update_action_states()

    def _restore_chat_session(self, state: dict, *, prefer_sidecar: bool = False) -> None:
        """Restore chat transcript + pending actions from a job/workspace dict."""
        history = list(state.get("chat_history") or [])
        self._chat_history = history
        if hasattr(self, "chat"):
            self.chat.clear_transcript()
            self.chat.clear_input()
            for turn in history:
                role = turn.get("role")
                content = str(turn.get("content") or "")
                if role == "user":
                    self.chat.append("You", content)
                elif role == "assistant":
                    prose, _ = split_chat_proposal(content)
                    self.chat.append("Assistant", prose or content or "(No reply.)")

        snapshot = state.get("pending_chat")
        sidecar = load_pending_chat_actions()
        status = pending_sidecar_status()
        if prefer_sidecar:
            if status == "cleared":
                pending = None
            elif status == "actions" and sidecar:
                pending = sidecar
            else:
                pending = snapshot if snapshot and snapshot.get("actions") else None
        else:
            pending = snapshot if snapshot and snapshot.get("actions") else None

        if pending and pending.get("actions"):
            fp = dict(pending.get("fingerprint") or {})
            cur = self._chat_actions_fingerprint()
            if not fp.get("content_hash"):
                # Legacy / missing hash: stamp hash from the restored table but keep
                # stored counts so a mismatched blob is still cleared below.
                try:
                    n_calls = int(fp["n_calls"]) if "n_calls" in fp else int(cur["n_calls"])
                    n_ties = int(fp["n_ties"]) if "n_ties" in fp else int(cur["n_ties"])
                except (TypeError, ValueError):
                    n_calls, n_ties = int(cur["n_calls"]), int(cur["n_ties"])
                fp = workspace_fingerprint(
                    n_calls=n_calls,
                    n_ties=n_ties,
                    source_path=str(fp.get("source_path") or cur.get("source_path") or ""),
                    content_hash=str(cur["content_hash"]),
                )
            self._save_pending_chat_actions(
                list(pending["actions"]),
                list(pending.get("skipped") or []),
                fingerprint=fp,
            )
            # Table already loaded — drop pending if indexes no longer match.
            if not fingerprint_matches(fp, cur):
                self._clear_pending_chat_actions()
        else:
            self._clear_pending_chat_actions()
        self._update_pending_actions_button()

    def save_job(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Job",
            self._job_path or (self._source_name.rsplit(".", 1)[0] + ".dpjob" if self._source_name else "deed.dpjob"),
            "Deed Plotter jobs (*.dpjob *.json);;All files (*)",
        )
        if not path:
            return
        self.text_input.apply_popout_edits()
        try:
            workspace_store.save_job(path, self._current_state())
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            return
        self._job_path = path
        self._autosave_workspace()
        self.statusBar().showMessage(f"Saved job to {path}")

    def open_job(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Job",
            filter="Deed Plotter jobs (*.dpjob *.json);;All files (*)",
        )
        if not path:
            return
        if not self._confirm_replace_workspace("Open Job"):
            return
        try:
            state = workspace_store.load_job(path)
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
            QMessageBox.critical(self, "Open Failed", f"Could not open job:\n{exc}")
            return
        self._job_path = path
        self._load_state(state, reload_source=True)
        self._autosave_workspace()
        self.statusBar().showMessage(f"Opened job {path}")
        self.tabs.setCurrentWidget(self._table_tab)

    # ---------- Plot / report ----------

    def replot(self):
        calls = self.table.get_calls()
        self._tie_calls = self.details.get_tie_calls()
        self._document_info = self.details.get_document_info()
        expected_open = looks_like_open_line_survey(self._document_info)
        if not calls:
            self.plot.clear_plot()
            self.report_label.setText("")
            self.notes.update_content([], None, expected_open=False)
            self.closure.update_content(None, expected_open=False)
            self.details.update_coordinates([], None)
            self._last_result = None
            self._update_action_states()
            return
        result = self.plot.plot_calls(
            calls, self._pob_monument, self._tie_calls,
            pob_coordinates=self._pob_coordinates,
        )
        self._last_result = result
        self.notes.update_content(calls, result, expected_open=expected_open)
        self.details.update_coordinates(calls, result, self._pob_coordinates)
        self.closure.update_content(
            result,
            self._document_info.get("acreage_stated", ""),
            expected_open=expected_open,
        )
        tol = load_closure_tolerance()
        closed = is_traverse_closed(result, tol)
        if closed:
            status = "CLOSED"
        elif expected_open:
            status = "OPEN — expected for this document type"
        else:
            status = "OPEN"
        parts = [
            status,
            f"Perimeter: {result.perimeter:,.2f} ft",
            f"Misclosure: {result.closure_error:.3f} ft"
            + (f" bearing {result.closure_bearing}" if result.closure_bearing else "")
            + f" (tol {tol:.3f} ft)",
            f"Precision: {result.precision}",
        ]
        if area_is_reliable(result, tol):
            area_label = "Area" if closed else "Area (approx, open)"
            parts.insert(
                2,
                f"{area_label}: {result.area_sqft:,.0f} sq ft ({result.area_acres:.3f} ac)",
            )
        elif expected_open:
            parts.insert(2, "Area: n/a (open line)")
        else:
            parts.insert(2, "Area: withheld (open)")
        text = "   |   ".join(parts)
        if result.errors:
            text += "\nProblems: " + "; ".join(result.errors)
        tie_warns = [w for w in result.warnings if w.startswith("Tie:")]
        if tie_warns:
            text += "\nTie issues: " + "; ".join(tie_warns[:4])
            if len(tie_warns) > 4:
                text += f" (+{len(tie_warns) - 4} more)"
        # Structural parse tips (e.g. misfiled first side) — always visible under tabs,
        # not only on Notes (OPEN parses land on Closure).
        struct = [
            w for w in (self._parse_warnings or [])
            if "misfiled" in w.lower()
        ]
        if struct:
            text += "\n" + struct[0]
            if len(struct) > 1:
                text += f" (+{len(struct) - 1} more — see Notes)"
        self.report_label.setText(text)
        self._update_action_states()

    def _on_course_clicked(self, sequence: int):
        """Plot click near a course → select that call (stay on Plot tab)."""
        row = sequence - 1
        if not (0 <= row < self.table.rowCount()):
            return
        self.table.selectRow(row)
        self.plot.highlight_segment(sequence)

    def _confirm_export_qa(self, what: str) -> bool:
        """Warn if OPEN or low-confidence before export. Export anyway / Cancel."""
        # Refresh traverse so QA matches the current table.
        self.replot()
        calls = self.table.get_calls()
        issues = collect_export_warnings(
            calls,
            self._last_result,
            tolerance_ft=load_closure_tolerance(),
            document_info=self.details.get_document_info(),
        )
        if not issues:
            return True
        body = (
            f"QA checks before {what}:\n\n"
            + "\n".join(issues)
            + "\n\nExport anyway, or cancel to review?"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Export QA")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(body)
        export_btn = box.addButton("Export anyway", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() == export_btn

    def _suggested_export_name(self, kind: str, ext: str) -> str:
        """Default Save-As name from the loaded deed stem, e.g. deed_calls.csv."""
        stem = Path(self._source_name).stem.strip() if self._source_name else ""
        if not stem:
            return f"{kind}.{ext}"
        # Keep filenames Windows-safe.
        safe = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in stem)
        return f"{safe}_{kind}.{ext}"

    def export_csv(self) -> bool:
        calls = self.table.get_calls()
        if not calls:
            QMessageBox.information(self, "Nothing to Export", "The call table is empty.")
            return False
        if not self._confirm_export_qa("CSV export"):
            return False
        calls = self.table.get_calls()
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Calls", self._suggested_export_name("calls", "csv"), "CSV files (*.csv)",
        )
        if not path:
            return False
        try:
            write_calls_csv(path, calls)
        except OSError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))
            return False
        self.statusBar().showMessage(f"Exported {len(calls)} calls to {path}")
        return True

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Calls", filter="CSV files (*.csv);;All files (*)")
        if not path:
            return
        if not self._confirm_replace_workspace(
            "Import CSV",
            detail=(
                "Import CSV will replace the current call table only.\n"
                "Ties, deed details, legal text, and notes are left as they are.\n\n"
                "Unsaved call-table edits and pending parse or chat work will be discarded.\n\n"
                "Continue?"
            ),
        ):
            return
        try:
            calls = read_calls_csv(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Import Failed", str(exc))
            return
        if not calls:
            QMessageBox.information(self, "Import CSV", "No call rows found in that file.")
            return
        self._preserve_workspace_file = False
        self.table.set_calls(calls)
        self.table.clear_edit_history()
        self.details.clear_tie_history()
        self._reset_undo_kinds()
        self._clear_pending_chat_actions()
        self.replot()
        self.tabs.setCurrentWidget(self._table_tab)
        self.statusBar().showMessage(f"Imported {len(calls)} call(s) from {path}")
        self._schedule_autosave()
        self._update_action_states()

    def export_dxf(self) -> bool:
        if self.table.rowCount() == 0:
            QMessageBox.information(self, "Nothing to Export", "The call table is empty.")
            return False
        if not self._confirm_export_qa("DXF export"):
            return False
        if self._last_result is None or not self._last_result.segments:
            QMessageBox.information(self, "Nothing to Export", "Plot a traverse first.")
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "Export DXF", self._suggested_export_name("boundary", "dxf"), "DXF files (*.dxf)",
        )
        if not path:
            return False
        try:
            from dxf_export import export_traverse_dxf
            pob_text, pob_mon, _coords = self.details.get_pob_info()
            export_traverse_dxf(
                self._last_result,
                path,
                pob_mon,
                tie_calls=self.details.get_tie_calls() or None,
                document_info=self.details.get_document_info(),
                point_of_beginning=pob_text,
            )
        except Exception as exc:
            QMessageBox.critical(self, "DXF Export Failed", str(exc))
            return False
        self.statusBar().showMessage(f"Exported DXF to {path}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("DXF Exported")
        box.setText(f"Saved:\n{path}")
        box.setInformativeText("Open in DXF Viewer to preview layers and text?")
        open_btn = box.addButton("Open Viewer", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_dxf_viewer(path)
        return True

    def view_dxf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "View DXF", "", "DXF files (*.dxf);;All files (*)",
        )
        if path:
            self._open_dxf_viewer(path)

    def _open_dxf_viewer(self, path: str):
        from dxf_viewer_window import open_dxf_viewer

        # Keep a strong ref so modeless windows are not garbage-collected.
        # WA_DeleteOnClose destroys the C++ object on close — skip those.
        if not hasattr(self, "_dxf_viewer_windows"):
            self._dxf_viewer_windows = []
        self._prune_dxf_viewers()
        win = open_dxf_viewer(path, parent=self)
        if win is None or isdeleted(win) or not win.isVisible():
            return
        win.destroyed.connect(self._prune_dxf_viewers)
        self._dxf_viewer_windows.append(win)

    def _prune_dxf_viewers(self, *_args):
        windows = getattr(self, "_dxf_viewer_windows", [])
        self._dxf_viewer_windows = [
            w for w in windows
            if w is not None and not isdeleted(w) and w.isVisible()
        ]

    def _maybe_edit_call(self, row: int, col: int):
        if self._app_busy():
            return
        if col == 0:
            self._edit_call(row)

    @staticmethod
    def _widget_has_focus(widget: QWidget) -> bool:
        fw = QApplication.focusWidget()
        if fw is None or widget is None:
            return False
        return fw is widget or widget.isAncestorOf(fw)

    def _shortcut_delete(self):
        """Delete selected ties when the tie table is focused; otherwise calls."""
        if self._app_busy():
            return
        if self._widget_has_focus(self.details.tie_table):
            self.details.delete_selected_ties()
            return
        if self._widget_has_focus(self.table):
            self.table.delete_selected_rows()

    def _shortcut_edit(self):
        """Edit selected tie when the tie table is focused; otherwise a call."""
        if self._app_busy():
            return
        if self._widget_has_focus(self.details.tie_table):
            self.details.edit_selected_tie()
            return
        self._edit_selected_call()

    def _edit_selected_call(self):
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, "No Selection", "Select a call to edit.")
            return
        self._edit_call(min(rows))

    def _edit_call(self, row: int):
        from call_editor_dialog import CallEditorDialog
        calls = self.table.get_calls()
        if not (0 <= row < len(calls)):
            return
        dlg = CallEditorDialog(calls[row], self)
        if dlg.exec():
            self.table.replace_call(row, dlg.edited_call())
            self.replot()

    def _jump_to_call(self, row: int, open_editor: bool = False):
        if not (0 <= row < self.table.rowCount()):
            return
        self.tabs.setCurrentWidget(self._table_tab)
        self.table.selectRow(row)
        self.table.scrollToItem(self.table.item(row, 0))
        self.plot.highlight_segment(row + 1)
        if open_editor:
            self._edit_call(row)

    def _snapshot_pending(self) -> dict | None:
        data = load_pending_chat_actions()
        if not data or not data.get("actions"):
            return None
        return {
            "actions": list(data["actions"]),
            "skipped": list(data.get("skipped") or []),
            "fingerprint": dict(data.get("fingerprint") or {}),
        }

    def _restore_pending_snapshot(self, snap: dict | None) -> None:
        if snap and snap.get("actions"):
            self._save_pending_chat_actions(
                list(snap["actions"]),
                list(snap.get("skipped") or []),
                fingerprint=dict(snap.get("fingerprint") or {}),
            )
        else:
            self._clear_pending_chat_actions()
        self._update_pending_actions_button()

    def _note_undo(self, kind: str) -> None:
        self._undo_kind.append(kind)
        # Only Chat Apply should revive pending. Cell/tie edits must not
        # snapshot the sidecar — Discard then Ctrl+Z would resurrect it.
        self._undo_pending.append(
            self._snapshot_pending() if kind == "apply" else None
        )
        self._redo_kind.clear()
        self._redo_pending.clear()
        if kind == "call":
            self.details.clear_tie_redo()
        elif kind == "tie":
            self.table.clear_redo()
        elif kind == "apply":
            self.details.clear_tie_redo()
            self.table.clear_redo()
        self._update_action_states()

    def _reset_undo_kinds(self) -> None:
        self._undo_kind.clear()
        self._redo_kind.clear()
        self._undo_pending.clear()
        self._redo_pending.clear()

    def _undo_edit(self):
        if self._app_busy():
            return
        from_stack = bool(self._undo_kind)
        kind = self._undo_kind.pop() if from_stack else None
        pending_snap = self._undo_pending.pop() if from_stack else None
        if kind is None:
            if self.table.can_undo():
                kind = "call"
            elif self.details.can_undo_ties():
                kind = "tie"
        current_pending = self._snapshot_pending()
        if kind == "call":
            if self.table.undo():
                self._redo_kind.append("call")
                self._redo_pending.append(None)
                self.replot()
                self.statusBar().showMessage("Undid last call-table change.")
            elif from_stack:
                self._undo_pending.append(pending_snap)
        elif kind == "tie":
            if self.details.undo_ties():
                self._redo_kind.append("tie")
                self._redo_pending.append(None)
                self.statusBar().showMessage("Undid last tie-table change.")
            elif from_stack:
                self._undo_pending.append(pending_snap)
        elif kind == "apply":
            did_call = self.table.undo()
            did_tie = self.details.undo_ties()
            if did_call or did_tie:
                self._redo_kind.append("apply")
                self._redo_pending.append(current_pending)
                self._restore_pending_snapshot(pending_snap)
                if did_call:
                    self.replot()
                self.statusBar().showMessage("Undid last Apply.")
            elif from_stack:
                self._undo_pending.append(pending_snap)
        self._update_action_states()

    def _redo_edit(self):
        if self._app_busy():
            return
        from_stack = bool(self._redo_kind)
        kind = self._redo_kind.pop() if from_stack else None
        pending_snap = self._redo_pending.pop() if from_stack else None
        if kind is None:
            if self.table.can_redo():
                kind = "call"
            elif self.details.can_redo_ties():
                kind = "tie"
        current_pending = self._snapshot_pending()
        if kind == "call":
            if self.table.redo():
                self._undo_kind.append("call")
                self._undo_pending.append(None)
                self.replot()
                self.statusBar().showMessage("Redid last call-table change.")
            elif from_stack:
                self._redo_pending.append(pending_snap)
        elif kind == "tie":
            if self.details.redo_ties():
                self._undo_kind.append("tie")
                self._undo_pending.append(None)
                self.statusBar().showMessage("Redid last tie-table change.")
            elif from_stack:
                self._redo_pending.append(pending_snap)
        elif kind == "apply":
            did_call = self.table.redo()
            did_tie = self.details.redo_ties()
            if did_call or did_tie:
                self._undo_kind.append("apply")
                self._undo_pending.append(current_pending)
                self._restore_pending_snapshot(pending_snap)
                if did_call:
                    self.replot()
                self.statusBar().showMessage("Redid last Apply.")
            elif from_stack:
                self._redo_pending.append(pending_snap)
        self._update_action_states()

    def _undo_table(self):
        self._undo_edit()

    def _on_ties_edited(self):
        self._tie_calls = self.details.get_tie_calls()
        self.replot()
        self._update_action_states()
        self._schedule_autosave()

    def _on_document_info_edited(self):
        self._document_info = self.details.get_document_info()
        # Keep Parse Warnings in sync with live Type (soft UI already flips in replot).
        self._parse_warnings = sync_open_line_parse_warnings(
            self._parse_warnings, self._document_info,
        )
        self.notes.set_parse_info(
            self._point_of_beginning, self._general_notes,
            self._pob_monument, self._parse_warnings,
        )
        if self._last_result is not None:
            self.replot()
        self._schedule_autosave()
        self._update_action_states()

    def _on_pob_edited(self):
        text, monument, coords = self.details.get_pob_info()
        self._point_of_beginning = text
        self._pob_monument = monument
        self._pob_coordinates = coords
        self.notes.set_parse_info(
            self._point_of_beginning, self._general_notes,
            self._pob_monument, self._parse_warnings,
        )
        self.replot()
        self._schedule_autosave()
        self._update_action_states()

    def _sync_highlight(self):
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        self.plot.highlight_segment(min(rows) + 1 if rows else None)
        self._update_action_states()

    def _on_chat_send(self, message: str):
        if self._app_busy():
            return
        cfg = load_settings()
        if not cfg.get("api_key"):
            QMessageBox.warning(
                self, "API Key Required",
                "Open Settings and enter your Cursor API key before chatting.",
            )
            self.open_settings()
            return

        prior = list(self._chat_history)
        self.chat.append("You", message)
        self.chat.clear_input()
        self._chat_history.append({"role": "user", "content": message})
        self._schedule_autosave()

        context = build_deed_context(
            source_name=self._source_name,
            document_info=self.details.get_document_info(),
            point_of_beginning=self._point_of_beginning,
            pob_monument=self._pob_monument,
            general_notes=self._general_notes,
            parse_warnings=self._parse_warnings,
            legal_description=self.legal_text.plain_text(),
            calls=self.table.get_calls(),
            tie_calls=self.details.get_tie_calls(),
            result=self._last_result,
        )

        self.statusBar().showMessage("Asking assistant…")
        self._show_page_clock = False
        self._page_timeout_sec = 0
        self._page_phase_started = 0.0
        self._progress_page_note = ""
        self._progress_page_frac = ""

        self._chat_generation += 1
        job = {
            "operation": "chat",
            "model": cfg["model"],
            "deed_context": context,
            "history": prior,
            "message": message,
        }
        self.worker = DeedAIWorker(cfg, job, self._chat_generation)
        self.worker.finished_ok.connect(self._chat_done)
        self.worker.failed.connect(self._chat_failed)
        self.worker.finished.connect(self._on_ai_thread_finished)
        self.worker.start()
        self._chat_stopping = False
        self.chat.set_thinking(True)
        self._update_action_states()

    def _on_chat_clear(self):
        # Clearing mid-reply would orphan an Assistant turn (generation still live).
        if self.chat.is_thinking() or self._ai_busy():
            return
        if self._chat_history or self.chat.has_transcript():
            if QMessageBox.question(
                self, "Clear chat",
                "Clear the chat transcript? This cannot be undone.",
            ) != QMessageBox.StandardButton.Yes:
                return
        self._chat_history.clear()
        self.chat.clear_transcript()
        # Keep pending proposals (Health parity) — reopen via Pending actions…
        self._schedule_autosave()
        self.statusBar().showMessage("Chat cleared.")

    def _chat_done(self, result: dict, generation: int):
        if generation != self._chat_generation:
            return
        self._chat_stopping = False
        if hasattr(self, "chat"):
            self.chat.set_thinking(False)
        raw = str(result.get("text") or "")
        prose, fenced = split_chat_proposal(raw)
        structured = result.get("proposal")
        # Prefer non-empty tool/structured actions; else fenced JSON; else
        # empty structured (skipped-only) so normalize can report skips.
        struct_actions = (
            structured.get("actions")
            if isinstance(structured, dict)
            and isinstance(structured.get("actions"), list)
            else None
        )
        fenced_actions = (
            fenced.get("actions")
            if isinstance(fenced, dict) and isinstance(fenced.get("actions"), list)
            else None
        )
        if struct_actions:
            proposal = structured
            display = prose or raw or "(Proposed actions — see review.)"
        elif fenced_actions is not None:
            proposal = fenced
            display = prose or "(No reply.)"
        elif isinstance(structured, dict) and isinstance(structured.get("actions"), list):
            proposal = structured
            display = prose or raw or "(Proposed actions — see review.)"
        else:
            proposal = None
            display = prose or "(No reply.)"

        # History stores the proposal actually reviewed (tool preferred over fence).
        history_content = display
        if (
            isinstance(proposal, dict)
            and isinstance(proposal.get("actions"), list)
            and proposal["actions"]
        ):
            try:
                blob = json.dumps(
                    {
                        "actions": proposal.get("actions") or [],
                        "skipped": proposal.get("skipped") or [],
                    },
                    ensure_ascii=False,
                )
                history_content = (
                    (display.rstrip() + "\n\n") if display.strip() else ""
                ) + f"```json\n{blob}\n```"
            except (TypeError, ValueError):
                history_content = display
        elif raw.strip() and not display.strip():
            history_content = raw

        self.chat.append("Assistant", display)
        self._chat_history.append({"role": "assistant", "content": history_content})
        self.statusBar().showMessage("Chat reply ready.")
        self._update_action_states()
        self._schedule_autosave()

        if not proposal:
            # Model may have emitted broken JSON still visible in the Assistant turn.
            if '"actions"' in raw or "```json" in raw.lower():
                self.chat.append(
                    "System",
                    "Assistant reply looked like it included proposed actions, but "
                    "the JSON could not be read. Ask again if you still want changes.",
                )
            else:
                last_user = ""
                for turn in reversed(self._chat_history):
                    if turn.get("role") == "user":
                        last_user = str(turn.get("content") or "")
                        break
                if wants_deed_proposal(last_user):
                    self.chat.append(
                        "System",
                        "That sounded like a change request, but the assistant did "
                        "not return a reviewable action list. Try again, or be more "
                        "specific (e.g. “update call 2 to curve” / “propose actions”).",
                    )
            return
        actions, skipped = normalize_proposal(proposal)
        if not actions:
            if skipped:
                self.chat.append(
                    "System",
                    "Assistant proposed actions but none were valid:\n• "
                    + "\n• ".join(skipped[:8]),
                )
            else:
                self.chat.append(
                    "System",
                    "Assistant returned an empty action proposal — nothing to review.",
                )
            return
        self._show_chat_propose_dialog(actions, skipped)
        self._schedule_autosave()

    def _chat_failed(self, message: str, generation: int):
        if generation != self._chat_generation:
            return
        if self._chat_fail_generation == generation:
            return
        self._chat_fail_generation = generation
        self._chat_stopping = False
        if hasattr(self, "chat"):
            self.chat.set_thinking(False)
        # Drop the pending user turn so a retry can re-ask cleanly.
        if self._chat_history and self._chat_history[-1].get("role") == "user":
            self._chat_history.pop()
        self.chat.append("Error", message)
        self.statusBar().showMessage("Chat failed.")
        self._update_action_states()
        self._schedule_autosave()
        QMessageBox.critical(self, "Chat Failed", message)

    def _apply_chat_actions(self, actions: list[dict]):
        self._apply_keep_side_actions = []
        call_kinds = {"update_call", "set_confidence", "add_call", "delete_call"}
        boundary_acts = [
            a for a in actions
            if a.get("action") in call_kinds and a.get("target") != "tie"
        ]
        tie_acts = [
            a for a in actions
            if a.get("action") in call_kinds and a.get("target") == "tie"
        ]
        doc_acts = [a for a in actions if a.get("action") == "update_document_info"]
        side_acts = [
            a for a in actions
            if a.get("action") in ("run_parse", "export_csv", "export_dxf")
        ]

        errors: list[str] = []
        if boundary_acts or tie_acts:
            # Snapshot both sides so Apply Undo cannot pop a prior sibling edit
            # when this Apply only changed calls or only ties.
            self.table.push_undo(emit=False)
            self.details.push_tie_undo(emit=False)
            self._note_undo("apply")
        if boundary_acts:
            new_calls, errs = apply_call_actions(self.table.get_calls(), boundary_acts)
            errors.extend(errs)
            self.table.set_calls(new_calls)
            self.table.callsEdited.emit()
            self.replot()

        if tie_acts:
            new_ties, errs = apply_call_actions(self.details.get_tie_calls(), tie_acts)
            errors.extend([f"tie: {e}" for e in errs])
            self.details.set_tie_calls(new_ties)
            self._tie_calls = new_ties
            self.details.tiesEdited.emit()

        if doc_acts:
            info = dict(self.details.get_document_info())
            for act in doc_acts:
                info.update(act.get("fields") or {})
            self._document_info = info
            self.details.set_parse_info(
                info, self.details.get_tie_calls(), clear_tie_undo=False)
            self._parse_warnings = sync_open_line_parse_warnings(
                self._parse_warnings, info,
            )
            self.notes.set_parse_info(
                self._point_of_beginning, self._general_notes,
                self._pob_monument, self._parse_warnings,
            )
            if self._last_result is not None:
                self.replot()
            self._schedule_autosave()

        if errors:
            self.chat.append("System", "Some actions failed:\n• " + "\n• ".join(errors))
        elif boundary_acts or tie_acts or doc_acts:
            self.chat.append("System", "Applied selected table/document changes.")
            self.statusBar().showMessage("Applied chat actions.")

        # Parse is async — never export in the same Apply (file would be pre-parse
        # and a finishing parse can wipe chat edits just applied above).
        side_parse = [a for a in side_acts if a.get("action") == "run_parse"]
        side_export = [a for a in side_acts if a.get("action") in ("export_csv", "export_dxf")]
        if side_parse:
            if boundary_acts or tie_acts or doc_acts:
                self.chat.append(
                    "System",
                    "Skipped parse in this Apply — table or document edits were "
                    "already applied. Parse would replace them. Run Parse with AI "
                    "separately if you still want a fresh extract.",
                )
                if side_export:
                    self.chat.append(
                        "System",
                        "Skipped CSV/DXF export in this Apply — wait until you "
                        "choose to parse, then export so the file matches.",
                    )
            else:
                started = self.run_parse()
                if not started:
                    self._apply_keep_side_actions = list(side_parse) + list(side_export)
                if side_export:
                    self.chat.append(
                        "System",
                        "Skipped CSV/DXF export in this Apply — wait for parse to finish, "
                        "then export so the file matches the new traverse.",
                    )
                return started
        else:
            failed: list[dict] = []
            for act in side_export:
                if act["action"] == "export_csv":
                    wrote = self.export_csv()
                else:
                    wrote = self.export_dxf()
                if not wrote:
                    failed.append(act)
            self._apply_keep_side_actions = failed
            return not failed
        return True

    def _toggle_chat_pane(self, visible: bool):
        if (
            not visible
            and self._worker_is_chat()
            and self._ai_busy()
            and not self._chat_stopping
        ):
            self.chat_act.blockSignals(True)
            self.chat_act.setChecked(True)
            self.chat_act.blockSignals(False)
            self.chat.setVisible(True)
            return
        self.chat.setVisible(visible)
        if hasattr(self, "_main_splitter") and visible:
            sizes = self._main_splitter.sizes()
            if len(sizes) >= 3 and sizes[2] < 200:
                total = sum(sizes) or 1560
                self._main_splitter.setSizes([
                    int(total * 0.30), int(total * 0.45), int(total * 0.25),
                ])

    def _restore_ui_layout(self):
        s = QSettings(SETTINGS_ORG, SETTINGS_APP)
        geo = s.value("window_geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = s.value("splitter_state")
        if state is not None and hasattr(self, "_main_splitter"):
            self._main_splitter.restoreState(state)
        chat_on = s.value("chat_visible", True, bool)
        self.chat_act.blockSignals(True)
        self.chat_act.setChecked(chat_on)
        self.chat_act.blockSignals(False)
        self.chat.setVisible(chat_on)
        self.chk_courses.setChecked(s.value("plot_courses", True, bool))
        self.chk_corners.setChecked(s.value("plot_corners", True, bool))
        self.chk_monuments.setChecked(s.value("plot_monuments", True, bool))
        self.chk_ties.setChecked(s.value("plot_ties", True, bool))
        self.chk_grid.setChecked(s.value("plot_grid", True, bool))
        self.plot.set_show_grid(self.chk_grid.isChecked())
        self._update_action_states()

    def _save_ui_layout(self):
        s = QSettings(SETTINGS_ORG, SETTINGS_APP)
        s.setValue("window_geometry", self.saveGeometry())
        if hasattr(self, "_main_splitter"):
            s.setValue("splitter_state", self._main_splitter.saveState())
        s.setValue("chat_visible", self.chat.isVisible())
        s.setValue("plot_courses", self.chk_courses.isChecked())
        s.setValue("plot_corners", self.chk_corners.isChecked())
        s.setValue("plot_monuments", self.chk_monuments.isChecked())
        s.setValue("plot_ties", self.chk_ties.isChecked())
        s.setValue("plot_grid", self.chk_grid.isChecked())

    def open_settings(self):
        if self._app_busy():
            return
        before = load_settings().get("image_quality")
        before_tol = load_closure_tolerance()
        dlg = SettingsDialog(self)
        if not dlg.exec():
            return
        after = load_settings().get("image_quality")
        # Parse uses already-rasterized viewer.pages — quality only applies on re-open.
        if after != before and self.viewer.pages:
            self.statusBar().showMessage(
                "Deed image quality updated — Open the deed again (or restore the "
                "job) to re-rasterize pages before Parse with AI.",
                12000,
            )
        if load_closure_tolerance() != before_tol:
            self.replot()
        self._update_action_states()

    def _shortcut_target_is_text(self) -> bool:
        """True when the focused widget should keep Delete / Ctrl+Z / Ctrl+E / Ctrl+F."""
        fw = QApplication.focusWidget()
        if fw is None:
            return False
        w = fw
        while w is not None:
            if isinstance(w, QTextEdit) and w.isReadOnly():
                w = w.parentWidget()
                continue
            if isinstance(w, (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox)):
                return True
            if (
                isinstance(w, QAbstractItemView)
                and w.state() == QAbstractItemView.State.EditingState
            ):
                return True
            w = w.parentWidget()
        return False

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() != QEvent.Type.KeyPress:
            return False
        if QApplication.activeWindow() is not self:
            return False
        if self._shortcut_target_is_text():
            return False
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and key == Qt.Key.Key_Z and not shift:
            self._undo_edit()
            return True
        if (ctrl and key == Qt.Key.Key_Y) or (ctrl and shift and key == Qt.Key.Key_Z):
            self._redo_edit()
            return True
        if key == Qt.Key.Key_Delete:
            if self._widget_has_focus(self.details.tie_table) or self._widget_has_focus(self.table):
                self._shortcut_delete()
                return True
            return False
        if ctrl and key == Qt.Key.Key_E and not shift:
            if self._widget_has_focus(self.details.tie_table) or self._widget_has_focus(self.table):
                self._shortcut_edit()
                return True
            return False
        if ctrl and key == Qt.Key.Key_F and not shift:
            if self.tabs.currentWidget() is getattr(self, "_plot_tab", None):
                self.plot.fit_view()
                return True
            return False
        return False

    def closeEvent(self, event: QCloseEvent):
        # Commit pop-out text before close so quit-autosave does not drop it.
        self.text_input.apply_popout_edits()
        self.text_input.close_popout(force=True)
        self._save_ui_layout()
        self._autosave_workspace()
        load_w = self._load_worker
        if load_w is not None and load_w.isRunning():
            pending = self._pending_load
            purpose = (pending or {}).get("purpose", "open")
            self._load_generation += 1
            self._pending_load = None
            # Do NOT disconnect page_progress before the thread ends: a
            # BlockingQueued emit would hang forever waiting for a slot that
            # will never run. Cancel, pump events so blocked emits finish,
            # then detach.
            load_w.cancel()
            self._wait_thread_quit(load_w, timeout_ms=5000)
            if load_w.isRunning():
                # Staying open: don't leave old rasters under a restored source.
                if purpose in ("history", "workspace"):
                    self._clear_viewer_pages_keep_traverse(
                        status=(
                            "Close deferred — page images cleared while reload "
                            "finishes. Open the deed again before Parse."
                        ),
                    )
                event.ignore()
                self.statusBar().showMessage(
                    "Still rasterizing a page — close again when loading finishes.",
                    12000,
                )
                return
            for sig in ("finished_ok", "failed", "page_progress", "cancelled_done", "finished"):
                try:
                    getattr(load_w, sig).disconnect()
                except (AttributeError, TypeError):
                    pass
        ai_w = self.worker
        if ai_w is not None and ai_w.isRunning():
            # Invalidate generations and detach slots so a queued signal from
            # the dying worker cannot land on a destroyed window.
            self._parse_generation += 1
            self._chat_generation += 1
            if self._worker_is_chat():
                self._chat_stopping = True
                if hasattr(self, "chat"):
                    self.chat.set_stopping()
            ai_w.cancel()
            self._wait_thread_quit(ai_w, timeout_ms=5000)
            if ai_w.isRunning():
                event.ignore()
                self.statusBar().showMessage(
                    "Still stopping AI work — close again when it finishes.",
                    12000,
                )
                return
            for sig in ("finished_ok", "failed", "page_progress", "finished"):
                try:
                    getattr(ai_w, sig).disconnect()
                except (AttributeError, TypeError):
                    pass
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)

    def _wait_thread_quit(self, thread: QThread, *, timeout_ms: int) -> None:
        """Wait for *thread* while pumping events so BlockingQueued slots can run."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while thread.isRunning() and time.monotonic() < deadline:
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents,
                50,
            )
            thread.wait(50)
