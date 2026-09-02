"""Survey-style scale bar for the deed plot (matches Main Checker left plot).

Draws a bottom-center ``|---|`` bar in data coordinates with a feet label
above it, updating on pan/zoom to a nice length (~1/3 of the view width).
"""

from __future__ import annotations

import math

import pyqtgraph as pg


def nice_scale_feet(view_width_feet: float, *, target_fraction: float = 1 / 2.86) -> float | None:
    """Pick a 1/2/5×10ⁿ length near *target_fraction* of the view width."""
    if view_width_feet <= 0:
        return None
    target = view_width_feet * target_fraction
    if target < 0.001:
        return 0.001
    exp = math.floor(math.log10(target))
    base = 10 ** exp
    candidates = [1 * base, 2 * base, 5 * base, 10 * base]
    chosen = max((c for c in candidates if c <= target), default=0.0)
    return chosen if chosen > 0 else None


def format_scale_label(scale_feet: float) -> str:
    if scale_feet < 1:
        if scale_feet < 0.001:
            text = f"{scale_feet:.6f}"
        elif scale_feet < 0.01:
            text = f"{scale_feet:.5f}"
        else:
            text = f"{scale_feet:.3f}"
        return text.rstrip("0").rstrip(".") + " ft"
    return f"{scale_feet:.0f} ft"


class PlotScaleBar:
    """Owns the curve + label items; re-attach after PlotWidget.clear()."""

    def __init__(self, color: str = "#e0e0e0", width: float = 2):
        pen = pg.mkPen(color, width=width)
        self._bar = pg.PlotCurveItem(pen=pen, connect="finite", antialias=True)
        self._bar.setZValue(25)
        self._text = pg.TextItem(color=color, anchor=(0.5, 1.0))
        self._text.setZValue(26)
        self._plot: pg.PlotWidget | None = None

    def attach(self, plot: pg.PlotWidget) -> None:
        self._plot = plot
        self._ensure_items()

    def _ensure_items(self) -> None:
        if self._plot is None:
            return
        # PlotWidget instances wrap PlotItem.addItem (supports ignoreBounds).
        pi = (
            self._plot.getPlotItem()
            if hasattr(self._plot, "getPlotItem")
            else self._plot
        )
        vb = pi.getViewBox()
        for item in (self._bar, self._text):
            if item not in pi.items:
                # Must ignoreBounds — otherwise Fit View autoRange includes the
                # bar (parked in the view margin) and zooms out every click.
                pi.addItem(item, ignoreBounds=True)
            elif item in vb.addedItems:
                # Recover if an older attach left the item in auto-range bounds.
                vb.addedItems.remove(item)

    def hide(self) -> None:
        self._bar.hide()
        self._text.hide()

    def update_from_view(self, ranges) -> None:
        """Refresh geometry from ViewBox.viewRange()-style *(xRange, yRange)*."""
        if self._plot is None:
            return
        self._ensure_items()
        x_range, y_range = ranges
        view_width = x_range[1] - x_range[0]
        if view_width <= 0:
            self.hide()
            return

        scale_feet = nice_scale_feet(view_width)
        if scale_feet is None:
            self.hide()
            return

        # Plot coords are feet (same as Main Checker with normalization_factor=1).
        scale_plot = scale_feet
        margin_y = (y_range[1] - y_range[0]) * 0.04
        pos_x = x_range[0] + (x_range[1] - x_range[0]) / 2 - scale_plot / 2
        pos_y = y_range[0] + margin_y
        height_plot = view_width * 0.01

        xs = [
            pos_x, pos_x, float("nan"),
            pos_x + scale_plot, pos_x + scale_plot, float("nan"),
            pos_x, pos_x + scale_plot,
        ]
        ys = [
            pos_y, pos_y + height_plot, float("nan"),
            pos_y, pos_y + height_plot, float("nan"),
            pos_y, pos_y,
        ]
        self._bar.setData(xs, ys)
        self._bar.show()

        y_span = y_range[1] - y_range[0]
        label_y = pos_y + height_plot + max(y_span * 0.002, 0.0)
        self._text.setText(format_scale_label(scale_feet))
        self._text.setPos(pos_x + scale_plot / 2, label_y)
        self._text.show()
