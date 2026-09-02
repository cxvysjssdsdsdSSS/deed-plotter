"""DXF export of the traverse — Civil 3D-friendly layers and survey labels.

Course annotation follows the common Civil 3D \"Bearing over Distance\" pattern:
bearing above the line, distance below, text rotated along the course and kept
readable (not upside-down). Monument / POB / END / POC callouts use the same
collision-aware offset search as the in-program plot so leaders do not stack.
Uses a TrueType text style so Civil 3D and the built-in viewer render filled
glyphs instead of stroked SHX outlines.
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from ezdxf.enums import MTextEntityAlignment, TextEntityAlignment

from cogo import Call, TraverseResult, compute_traverse
from closure_panel import area_is_reliable, is_traverse_closed, load_closure_tolerance
from label_layout import (
    estimate_label_size_px,
    find_offset_px,
    format_monument_dxf_label,
    point_box,
    wrap_text,
)

_DASHED_LINETYPE = "DP_DASHED"
_INSUNITS_US_SURVEY_FEET = 21  # AutoCAD 2017+ US survey foot
_TEXT_STYLE = "DP-TEXT"
_TEXT_FONT = "Arial.ttf"

_LAYERS = (
    ("DP-BOUNDARY", 1),
    ("DP-TIE", 8),
    ("DP-POB", 3),
    ("DP-END", 1),
    ("DP-VERTICES", 5),
    ("DP-MONUMENTS", 2),
    ("DP-LABELS", 7),
    ("DP-MISCLOSE", 1),
    ("DP-CLOSURE", 7),
    ("DP-DETAILS", 7),
)

# Layout units ≈ on-screen pixels so find_offset_px sees plot-like boxes.
_LAYOUT_FONT_PX = 12.0
_MONUMENT_WRAP_CHARS = 40

# Match Deed Details → Document tab field order (details_panel._INFO_FIELDS).
_DETAIL_FIELDS = (
    ("document_type", "Type"),
    ("county", "County"),
    ("state", "State"),
    ("date", "Date"),
    ("grantor", "Grantor"),
    ("grantee", "Grantee"),
    ("surveyor", "Surveyor"),
    ("surveyor_license", "License"),
    ("volume_page", "Vol/Page"),
    ("acreage_stated", "Stated acreage"),
    ("basis_of_bearings", "Basis of bearings"),
)


class _CalloutPlacer:
    """Model-space wrapper around find_offset_px (same search as the plot)."""

    def __init__(self, text_h: float, marker_r: float, corners: list[tuple[float, float]]):
        self.text_h = text_h
        self.marker_r = marker_r
        self.scale = _LAYOUT_FONT_PX / max(text_h * 0.55, 0.4)
        self.placed: list[tuple[float, float, float, float]] = []
        self.leaders: list[tuple[tuple[float, float], tuple[float, float]]] = []
        self.polygon = [self.to_px(p) for p in corners] if len(corners) >= 3 else None
        for pt in corners:
            self.reserve_point(pt, marker_r * 1.15)

    def to_px(self, pt: tuple[float, float]) -> tuple[float, float]:
        return (pt[0] * self.scale, pt[1] * self.scale)

    def off_to_ft(self, off: tuple[float, float]) -> tuple[float, float]:
        return (off[0] / self.scale, off[1] / self.scale)

    def reserve_point(self, pt: tuple[float, float], radius_ft: float) -> None:
        px = self.to_px(pt)
        self.placed.append(point_box(px[0], px[1], radius_ft * self.scale))

    def reserve_aabb_ft(self, box: tuple[float, float, float, float]) -> None:
        s = self.scale
        self.placed.append((box[0] * s, box[1] * s, box[2] * s, box[3] * s))

    def place(
        self,
        anchor: tuple[float, float],
        text: str,
        preferred_dir: tuple[float, float],
        *,
        font_h: float | None = None,
        record_leader: bool = True,
        max_turn_deg: float | None = None,
        fan_index: int = 0,
        clearance_ft: float | None = None,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        font_h = font_h if font_h is not None else self.text_h
        font_px = font_h * self.scale
        size = estimate_label_size_px(text, font_px)
        ox, oy = preferred_dir
        if fan_index:
            step = 40 + 30 * ((fan_index - 1) // 2)
            sign = 1 if fan_index % 2 else -1
            ang = math.atan2(oy, ox) + math.radians(sign * step)
            ox, oy = math.cos(ang), math.sin(ang)
        length = math.hypot(ox, oy) or 1.0
        pref = (ox / length, oy / length)
        clearance = (
            clearance_ft if clearance_ft is not None
            else (self.marker_r * 1.4 + font_h * 0.35)
        ) * self.scale
        offset_px = find_offset_px(
            self.to_px(anchor), text, pref, self.placed,
            font_px=font_px, anchor_clearance_px=clearance, size_px=size,
            leaders=self.leaders, polygon_px=self.polygon,
            record_leader=record_leader, max_turn_deg=max_turn_deg,
        )
        off_ft = self.off_to_ft(offset_px)
        return (anchor[0] + off_ft[0], anchor[1] + off_ft[1]), off_ft


def export_traverse_dxf(
    result: TraverseResult,
    output_path: str,
    pob_monument: str = "",
    tie_calls: list[Call] | None = None,
    *,
    document_info: dict | None = None,
    point_of_beginning: str = "",
) -> None:
    """Write linework, corners, monument callouts, ties, deed details, and closure."""
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = _INSUNITS_US_SURVEY_FEET
    doc.header["$MEASUREMENT"] = 0  # imperial
    _ensure_text_style(doc)
    for name, color in _LAYERS:
        doc.layers.add(name, color=color)
    if _DASHED_LINETYPE not in doc.linetypes:
        doc.linetypes.add(_DASHED_LINETYPE, pattern=[0.75, -0.5], description="Misclosure")
    msp = doc.modelspace()

    span = _extent(result)
    # Model-space text sized for a plat: readable, not microscopic.
    text_h = max(2.0, min(span * 0.012, 10.0))
    marker_r = max(text_h * 0.45, 0.6)
    label_h = text_h * 0.65
    label_gap = max(label_h * 1.4, span * 0.01)
    layout = _CalloutPlacer(text_h, marker_r, list(result.segment_endpoints))

    # Per-call layers and linework + Civil-style course labels.
    for seg in result.segments:
        layer = f"DP-CALL-{seg.sequence:02d}"
        if layer not in doc.layers:
            doc.layers.add(layer, color=5)
        if seg.kind == "line":
            msp.add_line(seg.start, seg.end, dxfattribs={"layer": layer, "lineweight": 25})
        else:
            msp.add_lwpolyline(
                seg.path, close=False,
                dxfattribs={"layer": layer, "lineweight": 25},
            )
        _add_bearing_distance_labels(msp, seg, label_h, gap=label_gap)
        _reserve_course_labels(layout, seg, label_h, label_gap)

    # Boundary polyline — do not close across a missing call.
    boundary = list(result.points)
    if boundary:
        closed = is_traverse_closed(result) and not result.gaps
        if closed:
            boundary = [*boundary, boundary[0]]
        msp.add_lwpolyline(
            boundary, close=False,
            dxfattribs={"layer": "DP-BOUNDARY", "color": 1, "lineweight": 50},
        )
    for seq, vertex in result.gaps:
        gap_text = f"CALL {seq} MISSING"
        pos, _off = layout.place(
            vertex, gap_text, (1.0, 1.0),
            font_h=text_h * 0.7, record_leader=False,
        )
        _add_plain_text(
            msp, gap_text, pos,
            height=text_h * 0.7, layer="DP-LABELS", color=1,
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    # Corner circles.
    for pt in result.segment_endpoints:
        msp.add_point(pt, dxfattribs={"layer": "DP-VERTICES"})
        msp.add_circle(
            pt, radius=marker_r * 0.35,
            dxfattribs={"layer": "DP-VERTICES", "color": 5},
        )

    # POB / END markers — tags use collision search; skip END text when the
    # open end sits on top of POB (tiny misclose) so callouts can spread.
    centroid = _centroid(result.segment_endpoints)
    if result.segment_endpoints:
        pob = result.segment_endpoints[0]
        msp.add_circle(pob, radius=marker_r, dxfattribs={"layer": "DP-POB", "color": 3})
        pob_pos, _off = layout.place(
            pob, "POB", _outward(pob, centroid),
            font_h=text_h * 0.75, record_leader=False,
            clearance_ft=marker_r * 1.6,
        )
        _add_plain_text(
            msp, "POB", pob_pos,
            height=text_h * 0.75, layer="DP-POB", color=3,
            align=TextEntityAlignment.MIDDLE_CENTER,
        )
        if not is_traverse_closed(result):
            end = result.segment_endpoints[-1]
            msp.add_circle(end, radius=marker_r, dxfattribs={"layer": "DP-END", "color": 1})
            if not _markers_overlap(pob, end, marker_r):
                end_pos, _off = layout.place(
                    end, "END", _outward(end, centroid),
                    font_h=text_h * 0.75, record_leader=False,
                    clearance_ft=marker_r * 1.6,
                )
                _add_plain_text(
                    msp, "END", end_pos,
                    height=text_h * 0.75, layer="DP-END", color=1,
                    align=TextEntityAlignment.MIDDLE_CENTER,
                )
            msp.add_line(end, pob, dxfattribs={
                "layer": "DP-MISCLOSE", "color": 1, "linetype": _DASHED_LINETYPE,
            })

        if tie_calls:
            _add_tie_run(msp, tie_calls, pob, label_h, layout, gap=label_gap)

    _add_monuments(msp, result, pob_monument, layout, text_h, centroid)
    _add_deed_details(
        msp, result, text_h,
        document_info=document_info,
        point_of_beginning=point_of_beginning,
        pob_monument=pob_monument,
    )
    _add_closure_report(msp, result, text_h)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(output_path)


def _ensure_text_style(doc) -> None:
    """TrueType style so glyphs fill cleanly in Civil 3D and our viewer."""
    if _TEXT_STYLE not in doc.styles:
        doc.styles.new(_TEXT_STYLE, dxfattribs={"font": _TEXT_FONT})
    try:
        std = doc.styles.get("Standard")
        std.dxf.font = _TEXT_FONT
    except Exception:
        pass


def _add_plain_text(
    msp,
    content: str,
    insert: tuple[float, float],
    *,
    height: float,
    layer: str,
    color: int | None = None,
    rotation: float = 0.0,
    align: TextEntityAlignment = TextEntityAlignment.LEFT,
) -> None:
    attribs = {
        "layer": layer,
        "height": height,
        "style": _TEXT_STYLE,
        "rotation": rotation,
    }
    if color is not None:
        attribs["color"] = color
    text = msp.add_text(content, dxfattribs=attribs)
    text.set_placement(insert, align=align)


def _readable_rotation_deg(dx: float, dy: float) -> float:
    """TEXT rotation along (dx,dy), flipped so the string reads upright."""
    ang = math.degrees(math.atan2(dy, dx))
    if ang > 90.0 or ang <= -90.0:
        ang += -180.0 if ang > 0.0 else 180.0
    return ang


def _text_up_vector(rotation_deg: float) -> tuple[float, float]:
    """Unit vector in the local +Y of rotated TEXT (away from the baseline)."""
    rad = math.radians(rotation_deg)
    return (-math.sin(rad), math.cos(rad))


def _outward(
    pt: tuple[float, float], centroid: tuple[float, float],
) -> tuple[float, float]:
    ox = pt[0] - centroid[0]
    oy = pt[1] - centroid[1]
    length = math.hypot(ox, oy)
    return (ox / length, oy / length) if length > 1e-9 else (1.0, 0.0)


def _markers_overlap(
    a: tuple[float, float], b: tuple[float, float], marker_r: float,
) -> bool:
    """True when POB/END circles would occupy the same visual cluster."""
    return math.hypot(a[0] - b[0], a[1] - b[1]) < max(marker_r * 3.0, 2.0)


def _rotated_aabb(
    cx: float, cy: float, width: float, height: float, rot_deg: float,
) -> tuple[float, float, float, float]:
    rad = math.radians(rot_deg)
    c, s = math.cos(rad), math.sin(rad)
    hw, hh = width * 0.5, height * 0.5
    xs: list[float] = []
    ys: list[float] = []
    for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        xs.append(cx + x * c - y * s)
        ys.append(cy + x * s + y * c)
    return (min(xs), min(ys), max(xs), max(ys))


def _reserve_course_labels(layout: _CalloutPlacer, seg, text_h: float, gap: float) -> None:
    """Keep monument leaders off Civil-style bearing/distance TEXT."""
    dx = seg.end[0] - seg.start[0]
    dy = seg.end[1] - seg.start[1]
    if math.hypot(dx, dy) < 1e-9:
        return
    mid = ((seg.start[0] + seg.end[0]) / 2.0, (seg.start[1] + seg.end[1]) / 2.0)
    rot = _readable_rotation_deg(dx, dy)
    up_x, up_y = _text_up_vector(rot)
    above, below = _course_parts(seg)
    char_w = text_h * 0.62
    h = text_h * 1.15

    def reserve(content: str, toward_up: float) -> None:
        if not content:
            return
        w = max(len(content) * char_w, text_h)
        pos = (mid[0] + up_x * toward_up, mid[1] + up_y * toward_up)
        # BOTTOM_CENTER sits at +toward_up; TOP_CENTER at -toward_up.
        half = h * 0.5 if toward_up >= 0 else -h * 0.5
        cx = pos[0] + up_x * half
        cy = pos[1] + up_y * half
        layout.reserve_aabb_ft(_rotated_aabb(cx, cy, w, h, rot))

    reserve(above, gap)
    reserve(below, -gap)


def _course_parts(seg) -> tuple[str, str]:
    """Return (above_line, below_line) for Bearing-over-Distance style."""
    call = seg.call
    if seg.kind == "line":
        bearing = (call.bearing or "").strip()
        dist = f"{call.distance:g} {call.units}".strip()
        return bearing, dist

    # Curves: chord bearing above; radius / arc / delta below.
    above = (call.chord_bearing or call.bearing or "").strip()
    if above:
        above = f"CB {above}"
    bits: list[str] = []
    if call.radius:
        bits.append(f"R={call.radius:g}")
    if call.arc_length:
        bits.append(f"L={call.arc_length:g}")
    elif call.chord_length:
        bits.append(f"CH={call.chord_length:g}")
    if call.delta:
        bits.append(f"Δ {call.delta}")
    return above, "  ".join(bits)


def _add_bearing_distance_labels(
    msp, seg, text_h: float, *, gap: float | None = None, layer: str = "DP-LABELS",
) -> None:
    """Civil 3D-style: bearing above the course, distance (or curve data) below."""
    dx = seg.end[0] - seg.start[0]
    dy = seg.end[1] - seg.start[1]
    mid = ((seg.start[0] + seg.end[0]) / 2.0, (seg.start[1] + seg.end[1]) / 2.0)
    rot = _readable_rotation_deg(dx, dy)
    up_x, up_y = _text_up_vector(rot)
    clearance = gap if gap is not None else max(text_h * 1.4, 2.0)
    above, below = _course_parts(seg)

    if above:
        pos = (mid[0] + up_x * clearance, mid[1] + up_y * clearance)
        _add_plain_text(
            msp, above, pos, height=text_h, layer=layer, rotation=rot,
            align=TextEntityAlignment.BOTTOM_CENTER,
        )
    if below:
        pos = (mid[0] - up_x * clearance, mid[1] - up_y * clearance)
        _add_plain_text(
            msp, below, pos, height=text_h, layer=layer, rotation=rot,
            align=TextEntityAlignment.TOP_CENTER,
        )


def _add_tie_run(
    msp, tie_calls: list[Call], pob: tuple[float, float], text_h: float,
    layout: _CalloutPlacer,
    *, gap: float | None = None,
):
    tie_res = compute_traverse(tie_calls, start=(0.0, 0.0))
    if not tie_res.segments:
        return
    last = tie_res.segments[-1].end
    shift = (pob[0] - last[0], pob[1] - last[1])
    for seg in tie_res.segments:
        path = [(p[0] + shift[0], p[1] + shift[1]) for p in seg.path]
        if len(path) < 2:
            continue
        msp.add_lwpolyline(
            path, close=False,
            dxfattribs={
                "layer": "DP-TIE", "color": 8, "linetype": _DASHED_LINETYPE,
                "lineweight": 18,
            },
        )

        class _Shifted:
            kind = seg.kind
            call = seg.call
            start = path[0]
            end = path[-1]

        _add_bearing_distance_labels(
            msp, _Shifted(), text_h * 0.9, gap=gap, layer="DP-TIE",
        )
        if gap is not None:
            _reserve_course_labels(layout, _Shifted(), text_h * 0.9, gap)
    poc = (
        tie_res.segments[0].start[0] + shift[0],
        tie_res.segments[0].start[1] + shift[1],
    )
    msp.add_circle(poc, radius=max(text_h * 0.35, 0.4), dxfattribs={"layer": "DP-TIE", "color": 8})
    away = (poc[0] - pob[0], poc[1] - pob[1])
    if math.hypot(*away) < 1e-9:
        away = (1.0, 1.0)
    poc_pos, _off = layout.place(
        poc, "POC", away,
        font_h=text_h * 1.1, record_leader=False, clearance_ft=text_h * 1.2,
    )
    _add_plain_text(
        msp, "POC", poc_pos,
        height=text_h * 1.1, layer="DP-TIE", color=8,
        align=TextEntityAlignment.MIDDLE_CENTER,
    )


def _extent(result: TraverseResult) -> float:
    if not result.points:
        return 100.0
    xs = [p[0] for p in result.points]
    ys = [p[1] for p in result.points]
    return max(max(xs) - min(xs), max(ys) - min(ys), 1.0)


def _add_monuments(
    msp,
    result: TraverseResult,
    pob_monument: str,
    layout: _CalloutPlacer,
    text_h: float,
    centroid: tuple[float, float],
):
    """Corner callouts with collision-aware leaders; nearby duplicate text is dropped."""
    seen: list[tuple[tuple[float, float], str]] = []
    corner_fan: dict[tuple[int, int], int] = {}
    snap = max(1.0, layout.marker_r * 2.5)
    height = text_h * 0.55

    def already(anchor: tuple[float, float], text: str) -> bool:
        norm = text.lower()
        return any(
            t == norm and math.hypot(pt[0] - anchor[0], pt[1] - anchor[1]) <= snap
            for pt, t in seen
        )

    def fan_key(anchor: tuple[float, float]) -> tuple[int, int]:
        return (round(anchor[0] / snap), round(anchor[1] / snap))

    def place(anchor: tuple[float, float], raw_text: str) -> None:
        text = format_monument_dxf_label(raw_text) or " ".join(raw_text.split())
        if not text or already(anchor, text):
            return
        seen.append((anchor, text.lower()))
        wrapped = wrap_text(text, max_chars=_MONUMENT_WRAP_CHARS)
        key = fan_key(anchor)
        fan = corner_fan.get(key, 0)
        corner_fan[key] = fan + 1
        label_pos, _off = layout.place(
            anchor, wrapped, _outward(anchor, centroid),
            font_h=height, record_leader=True, fan_index=fan,
            clearance_ft=layout.marker_r * 1.8,
        )
        msp.add_line(anchor, label_pos, dxfattribs={"layer": "DP-MONUMENTS", "color": 2})
        _add_monument_label(msp, wrapped, label_pos, height)

    for seg in result.segments:
        if seg.call.monument:
            place(seg.end, seg.call.monument)
    if pob_monument and result.segment_endpoints:
        place(result.segment_endpoints[0], pob_monument)


def _add_monument_label(msp, text: str, insert: tuple[float, float], height: float) -> None:
    """Centered TEXT, or MTEXT when the callout wraps."""
    if "\n" not in text:
        _add_plain_text(
            msp, text, insert,
            height=height, layer="DP-MONUMENTS", color=2,
            align=TextEntityAlignment.MIDDLE_CENTER,
        )
        return
    lines = text.split("\n")
    width = max(len(line) for line in lines) * height * 0.85
    ent = msp.add_mtext(
        text,
        dxfattribs={
            "layer": "DP-MONUMENTS",
            "char_height": height,
            "style": _TEXT_STYLE,
            "width": max(width, height * 8.0),
            "color": 2,
            "attachment_point": int(MTextEntityAlignment.MIDDLE_CENTER),
        },
    )
    ent.set_location(
        insert=insert,
        attachment_point=int(MTextEntityAlignment.MIDDLE_CENTER),
    )


def _add_mtext_block(
    msp,
    lines: list[str],
    insert: tuple[float, float],
    *,
    char_h: float,
    layer: str,
    color: int = 7,
    width_factor: float = 28.0,
) -> None:
    """Single Civil 3D-compatible MTEXT paragraph, left-aligned (TOP_LEFT)."""
    text = msp.add_mtext(
        "\n".join(lines),
        dxfattribs={
            "layer": layer,
            "char_height": char_h,
            "style": _TEXT_STYLE,
            "width": char_h * width_factor,
            "color": color,
            "attachment_point": int(MTextEntityAlignment.TOP_LEFT),
            "flow_direction": 1,  # MTEXT_LEFT_TO_RIGHT (normal paragraph lines)
        },
    )
    text.set_location(
        insert=insert,
        attachment_point=int(MTextEntityAlignment.TOP_LEFT),
    )


def _add_deed_details(
    msp,
    result: TraverseResult,
    text_h: float,
    *,
    document_info: dict | None,
    point_of_beginning: str = "",
    pob_monument: str = "",
) -> None:
    """MTEXT deed-details block left of the figure (layer DP-DETAILS)."""
    info = document_info or {}
    lines = ["DEED DETAILS"]
    for key, label in _DETAIL_FIELDS:
        value = str(info.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    pob = (point_of_beginning or "").strip()
    if pob:
        lines.append(f"POB: {pob}")
    mon = (pob_monument or "").strip()
    if mon:
        lines.append(f"POB monument: {mon}")
    if len(lines) == 1:
        lines.append("(no document fields filled in)")

    xs = [p[0] for p in result.points] or [0.0]
    ys = [p[1] for p in result.points] or [0.0]
    char_h = text_h * 0.7
    width = char_h * 28.0
    gap = char_h * 3.0
    insert = (min(xs) - gap - width, max(ys))
    _add_mtext_block(
        msp, lines, insert, char_h=char_h, layer="DP-DETAILS", color=7,
    )


def _add_closure_report(msp, result: TraverseResult, text_h: float):
    tol = load_closure_tolerance()
    closed = is_traverse_closed(result, tol)
    lines = [
        "CLOSURE REPORT",
        f"Status: {'CLOSED' if closed else 'OPEN'}",
        f"Tolerance: {tol:.3f} ft",
        f"Perimeter: {result.perimeter:,.2f} ft",
        f"Linear misclosure: {result.closure_error:.3f} ft",
        f"Precision: {result.precision}",
    ]
    if area_is_reliable(result, tol):
        lines.append(f"Area: {result.area_sqft:,.0f} sq ft ({result.area_acres:.3f} ac)")
    else:
        lines.append("Area: withheld (open traverse)")
    if result.closure_bearing:
        lines.insert(5, f"Misclosure bearing: {result.closure_bearing}")
    xs = [p[0] for p in result.points] or [0.0]
    ys = [p[1] for p in result.points] or [0.0]
    char_h = text_h * 0.7
    insert = (min(xs), min(ys) - char_h * 2.5)
    _add_mtext_block(
        msp, lines, insert, char_h=char_h, layer="DP-CLOSURE", color=7,
        width_factor=32.0,
    )


def _centroid(points) -> tuple[float, float]:
    if not points:
        return (0.0, 0.0)
    return (sum(p[0] for p in points) / len(points),
            sum(p[1] for p in points) / len(points))
