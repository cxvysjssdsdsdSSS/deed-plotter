"""Application entry point."""

import sys
from pathlib import Path

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

from main_window import MainWindow

_ICONS = Path(__file__).resolve().parent / "icons"


def _qss_url(name: str) -> str:
    """Absolute path for QSS url(), forward slashes (required on Windows)."""
    return (_ICONS / name).as_posix()


def apply_dark_theme(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    bg = QColor(30, 33, 38)
    base = QColor(22, 25, 29)
    text = QColor(224, 224, 224)
    accent = QColor(66, 150, 250)
    p.setColor(QPalette.ColorRole.Window, bg)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(28, 31, 36))
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, bg)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, base)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, accent)
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 140))
    disabled = QColor(120, 120, 120)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(28, 31, 36))
    app.setPalette(p)

    up = _qss_url("scroll_up.png")
    down = _qss_url("scroll_down.png")
    left = _qss_url("scroll_left.png")
    right = _qss_url("scroll_right.png")

    # Fusion drops hover chrome on checked toolbuttons (e.g. Chat Pane when on).
    # Scrollbars: light thumb + real arrow PNGs (border-triangle QSS draws as squares).
    app.setStyleSheet(f"""
        QToolButton:hover {{
            background-color: #3a4048;
        }}
        QToolButton:checked {{
            background-color: #2a455f;
        }}
        QToolButton:checked:hover {{
            background-color: #355a78;
        }}
        QToolButton:pressed,
        QToolButton:checked:pressed {{
            background-color: #1e3a52;
        }}
        QScrollBar:vertical {{
            background: #1a1d22;
            width: 16px;
            margin: 16px 0 16px 0;
            border: none;
        }}
        QScrollBar::handle:vertical {{
            background: #8b939e;
            min-height: 28px;
            border-radius: 5px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: #b0b8c4;
        }}
        QScrollBar::handle:vertical:pressed {{
            background: #4296fa;
        }}
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {{
            background: #8b939e;
            height: 16px;
            width: 16px;
            border: none;
            border-radius: 3px;
            margin: 1px;
            subcontrol-origin: margin;
        }}
        QScrollBar::add-line:vertical {{
            subcontrol-position: bottom;
        }}
        QScrollBar::sub-line:vertical {{
            subcontrol-position: top;
        }}
        QScrollBar::add-line:vertical:hover,
        QScrollBar::sub-line:vertical:hover {{
            background: #b0b8c4;
        }}
        QScrollBar::add-line:vertical:pressed,
        QScrollBar::sub-line:vertical:pressed {{
            background: #4296fa;
        }}
        QScrollBar::up-arrow:vertical {{
            image: url("{up}");
            width: 11px;
            height: 11px;
        }}
        QScrollBar::down-arrow:vertical {{
            image: url("{down}");
            width: 11px;
            height: 11px;
        }}
        QScrollBar::add-page:vertical,
        QScrollBar::sub-page:vertical {{
            background: none;
        }}
        QScrollBar:horizontal {{
            background: #1a1d22;
            height: 16px;
            margin: 0 16px 0 16px;
            border: none;
        }}
        QScrollBar::handle:horizontal {{
            background: #8b939e;
            min-width: 28px;
            border-radius: 5px;
            margin: 2px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: #b0b8c4;
        }}
        QScrollBar::handle:horizontal:pressed {{
            background: #4296fa;
        }}
        QScrollBar::add-line:horizontal,
        QScrollBar::sub-line:horizontal {{
            background: #8b939e;
            width: 16px;
            height: 16px;
            border: none;
            border-radius: 3px;
            margin: 1px;
            subcontrol-origin: margin;
        }}
        QScrollBar::add-line:horizontal {{
            subcontrol-position: right;
        }}
        QScrollBar::sub-line:horizontal {{
            subcontrol-position: left;
        }}
        QScrollBar::add-line:horizontal:hover,
        QScrollBar::sub-line:horizontal:hover {{
            background: #b0b8c4;
        }}
        QScrollBar::add-line:horizontal:pressed,
        QScrollBar::sub-line:horizontal:pressed {{
            background: #4296fa;
        }}
        QScrollBar::left-arrow:horizontal {{
            image: url("{left}");
            width: 11px;
            height: 11px;
        }}
        QScrollBar::right-arrow:horizontal {{
            image: url("{right}");
            width: 11px;
            height: 11px;
        }}
        QScrollBar::add-page:horizontal,
        QScrollBar::sub-page:horizontal {{
            background: none;
        }}
        QAbstractScrollArea::corner {{
            background: #1a1d22;
            border: none;
        }}
    """)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Deed Plotter")
    apply_dark_theme(app)
    from wheel_guard import enable_no_wheel_on_selectors
    enable_no_wheel_on_selectors(app)
    win = MainWindow()
    win.show()
    win._place_on_screen()  # again after show — Windows sometimes ignores pre-show move
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
