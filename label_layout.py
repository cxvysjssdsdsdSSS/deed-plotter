"""Label placement helpers for the boundary plot and DXF export.

Framework-agnostic: works in screen-pixel space, so the plot converts
data coordinates to pixels before asking for placements. Includes
monument text cleanup (strip legal boilerplate, keep the physical
monument phrase), word wrapping, and a collision-avoiding offset search.
"""

from __future__ import annotations

import math
import re

_MONUMENT_LEAD_IN = re.compile(
    r"^(?:thence\s+to\s+(?:a|an|the)\s+|thence\s+to\s+|to\s+(?:a|an|the)\s+|(?:a|an|the)\s+)",
    re.IGNORECASE,
)
_CARDINAL = (
    r"north|south|east|west|n\.?w\.?|n\.?e\.?|s\.?w\.?|s\.?e\.?"
    r"|n\.|s\.|e\.|w\.|ne|nw|se|sw|center|middle"
)
_MONUMENT_LOCATION_CUT = re.compile(
    r"\s+(?:"
    rf"in the\s+(?:{_CARDINAL})\b"
    rf"|on the\s+(?:{_CARDINAL})\b"
    rf"|at the\s+(?:{_CARDINAL}|point|margin)\b"
    r"|of section\s+\d"
    rf"|for the\s+(?:{_CARDINAL})\b"
    r"|for the\s+[\w.]+\s+corner"
    r"|for corner"  # keeps 'found'/'set' — the recovery status matters
    r"|of said\s+\w+"
    r")",
    re.IGNORECASE,
)
_MONUMENT_BOILERPLATE = re.compile(
    r"^(?:place of beginning|point of beginning|pob)\b", re.IGNORECASE
)
_MONUMENT_BEGIN_PREFIX = re.compile(
    r"^(?:point of beginning|place of beginning|true point of beginning|pob"
    r"|point of commencement|commencing|commencement|beginning)"
    r"[\s,:;-]*(?:at|on|from|being|is)?[\s,]*",
    re.IGNORECASE,
)


_MONUMENT_WITNESS_CUT = re.compile(
    r"\s*;?\s*(?:from which|whence|witness(?:es)?)\b",
    re.IGNORECASE,
)

def extract_monument_core(text: str, *, keep_witness: bool = False) -> str:
    """Keep the physical monument phrase, dropping legal/location tail text.

    Plot callouts pass keep_witness=False so "from which … bears" tails
    do not blow up on-screen labels. DXF export keeps witnesses.
    """
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return ""
    cleaned = _MONUMENT_BEGIN_PREFIX.sub("", cleaned).strip()
    # Strip lead-ins repeatedly so "thence to a …" loses both "thence to" and "a".
    for _ in range(4):
        nxt = _MONUMENT_LEAD_IN.sub("", cleaned).strip()
        if nxt == cleaned:
            break
        cleaned = nxt
    if not cleaned or _MONUMENT_BOILERPLATE.match(cleaned):
        return ""
    if not keep_witness:
        # Drop witness-tree / "from which … bears" tails (plot callouts stay short).
        wit = _MONUMENT_WITNESS_CUT.search(cleaned)
        if wit:
            cleaned = cleaned[: wit.start()].strip(" ,;")
    match = _MONUMENT_LOCATION_CUT.search(cleaned)
    if match:
        # Don't cut inside a kept witness clause.
        if keep_witness:
            wit = _MONUMENT_WITNESS_CUT.search(cleaned)
            if wit and match.start() >= wit.start():
                match = None
        if match:
            cleaned = cleaned[: match.start()].strip()
    cleaned = re.sub(r"\s+for pob\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = cleaned.rstrip(" ,;.")
    if not cleaned or _MONUMENT_BOILERPLATE.match(cleaned):
        return ""
    return cleaned


def format_monument_dxf_label(text: str) -> str:
    """CAD export label: light cleanup, keep witness trees (single line)."""
    return extract_monument_core(text, keep_witness=True)

def wrap_text(text: str, max_chars: int = 30) -> str:
    """Word-wrap without truncating; avoids a single orphaned word on the last line."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        extra = len(word) + (1 if current else 0)
        if current and current_len + extra > max_chars:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += extra
    if current:
        lines.append(" ".join(current))
    if len(lines) >= 2 and " " not in lines[-1]:
        prev_words = lines[-2].split()
        if len(prev_words) >= 2:
            candidate = prev_words[-1] + " " + lines[-1]
            if len(candidate) <= max_chars:
                lines[-2] = " ".join(prev_words[:-1])
                lines[-1] = candidate
    return "\n".join(lines)


def format_monument_label(
    text: str, max_chars: int = 32, max_lines: int = 3,
) -> str:
    core = extract_monument_core(text)
    if not core:
        return ""
    if len(core) <= max_chars:
        wrapped = core
    else:
        wrapped = wrap_text(core, max_chars=max_chars)
    lines = wrapped.split("\n")
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip(" …")
        if len(last) > max_chars - 1:
            last = last[: max_chars - 1]
        lines[-1] = last.rstrip() + "…"
    return "\n".join(lines)


def estimate_label_size_px(text: str, font_px: float) -> tuple[float, float]:
    """Conservative bounding box of a multi-line label in pixels.

    Biased high on purpose: under-sizing is what lets callouts stack on courses.
    """
    lines = text.split("\n") or [""]
    max_line = max((len(line) for line in lines), default=0)
    # Segoe/UI proportional ≈ 0.55–0.62em; use 0.65 + pad for fill/border.
    width = max_line * font_px * 0.65 + 20
    height = len(lines) * font_px * 1.45 + 14
    return width, height


def _rects_overlap(a, b, margin: float = 6.0) -> bool:
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def _overlap_count(bbox, placed) -> int:
    return sum(1 for existing in placed if _rects_overlap(bbox, existing))


def point_box(cx: float, cy: float, radius_px: float) -> tuple[float, float, float, float]:
    """Reserved square box around a marker, in pixels."""
    return (cx - radius_px, cy - radius_px, cx + radius_px, cy + radius_px)


def _orient(ax, ay, bx, by, cx, cy) -> float:
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


def _segments_cross(
    a1: tuple[float, float], a2: tuple[float, float],
    b1: tuple[float, float], b2: tuple[float, float],
    *, eps: float = 1e-6,
) -> bool:
    """True if segments properly intersect (near-miss / T-hits count)."""
    o1 = _orient(*a1, *a2, *b1)
    o2 = _orient(*a1, *a2, *b2)
    o3 = _orient(*b1, *b2, *a1)
    o4 = _orient(*b1, *b2, *a2)

    def _nz(v: float) -> float:
        return 0.0 if abs(v) <= eps else v

    o1, o2, o3, o4 = _nz(o1), _nz(o2), _nz(o3), _nz(o4)
    if o1 == 0 and o2 == 0 and o3 == 0 and o4 == 0:
        return False

    def on_seg(p, q, r):
        return (
            min(p[0], q[0]) - eps <= r[0] <= max(p[0], q[0]) + eps
            and min(p[1], q[1]) - eps <= r[1] <= max(p[1], q[1]) + eps
        )

    if o1 == 0 or o2 == 0 or o3 == 0 or o4 == 0:
        if o1 == 0 and on_seg(a1, a2, b1):
            return True
        if o2 == 0 and on_seg(a1, a2, b2):
            return True
        if o3 == 0 and on_seg(b1, b2, a1):
            return True
        if o4 == 0 and on_seg(b1, b2, a2):
            return True
        return False
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def _point_in_poly(pt: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    """Ray-cast inclusion; boundary treated as outside (ok for leader midpoints)."""
    if len(poly) < 3:
        return False
    x, y = pt
    inside = False
    j = len(poly) - 1
    for i, (xi, yi) in enumerate(poly):
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _leader_stub(
    anchor: tuple[float, float], tip: tuple[float, float], trim_px: float = 6.0,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Shrink leader ends slightly so markers / own label don't false-positive."""
    dx = tip[0] - anchor[0]
    dy = tip[1] - anchor[1]
    length = math.hypot(dx, dy)
    if length < 10:
        return None
    t = min(0.2, trim_px / length)
    t1 = 1.0 - t
    if t1 <= t:
        return None
    return (
        (anchor[0] + dx * t, anchor[1] + dy * t),
        (anchor[0] + dx * t1, anchor[1] + dy * t1),
    )


def _segment_hits_box(
    p1: tuple[float, float], p2: tuple[float, float],
    box: tuple[float, float, float, float],
) -> bool:
    x0, y0, x1, y1 = box
    if min(p1[0], p2[0]) > x1 or max(p1[0], p2[0]) < x0:
        return False
    if min(p1[1], p2[1]) > y1 or max(p1[1], p2[1]) < y0:
        return False
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    for i in range(4):
        if _segments_cross(p1, p2, corners[i], corners[(i + 1) % 4]):
            return True
    # Segment fully inside the box.
    return x0 <= p1[0] <= x1 and y0 <= p1[1] <= y1


def _aabb_overlap(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    margin: float = 2.0,
) -> bool:
    return not (
        a[2] + margin < b[0]
        or b[2] + margin < a[0]
        or a[3] + margin < b[1]
        or b[3] + margin < a[1]
    )


def _seg_aabb(
    p1: tuple[float, float], p2: tuple[float, float],
) -> tuple[float, float, float, float]:
    return (
        min(p1[0], p2[0]), min(p1[1], p2[1]),
        max(p1[0], p2[0]), max(p1[1], p2[1]),
    )


def find_offset_px(
    anchor_px: tuple[float, float],
    text: str,
    preferred_dir: tuple[float, float],
    placed: list[tuple[float, float, float, float]],
    *,
    font_px: float,
    anchor_clearance_px: float = 10.0,
    preferred_offset: tuple[float, float] | None = None,
    size_px: tuple[float, float] | None = None,
    leaders: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    polygon_px: list[tuple[float, float]] | None = None,
    record_leader: bool = True,
    max_turn_deg: float | None = None,
) -> tuple[float, float]:
    """Find a pixel offset from *anchor_px* whose label box avoids *placed*.

    Also avoids leader lines that cut through other labels, cross existing
    leaders, or run through the traverse interior when *polygon_px* is set.
    *max_turn_deg* limits fan-out from *preferred_dir* (e.g. 95 for course
    labels so they never flip to the opposite side of the boundary).
    Appends the chosen box (and leader, when recorded) and returns the offset.
    """
    if size_px is not None:
        width, height = size_px
    else:
        width, height = estimate_label_size_px(text, font_px)
    half_diag = math.hypot(width * 0.5, height * 0.5)
    min_dist = half_diag + anchor_clearance_px
    pref_len = math.hypot(*preferred_dir) or 1.0
    pref_unit = (preferred_dir[0] / pref_len, preferred_dir[1] / pref_len)
    leader_aabbs = [_seg_aabb(a, b) for a, b in leaders] if leaders else []

    def bbox_for(offset: tuple[float, float]):
        cx = anchor_px[0] + offset[0]
        cy = anchor_px[1] + offset[1]
        return (
            cx - width * 0.55, cy - height * 0.55,
            cx + width * 0.55, cy + height * 0.55,
        )

    def hard_score(offset: tuple[float, float], box, *, limit: int) -> int:
        """Collision / geometry penalties only (no length term). Stop early if >= limit."""
        tip = (anchor_px[0] + offset[0], anchor_px[1] + offset[1])
        score = _overlap_count(box, placed) * 100
        if score >= limit:
            return score
        outward = offset[0] * pref_unit[0] + offset[1] * pref_unit[1]
        if outward < 0:
            # With a traverse polygon, inward placement is almost always wrong.
            score += 220 if polygon_px else 70
        elif outward < min_dist * 0.35:
            score += 15
        if score >= limit:
            return score
        stub = _leader_stub(anchor_px, tip)
        if stub is None:
            return score
        s0, s1 = stub
        stub_box = _seg_aabb(s0, s1)
        for existing in placed:
            cx = (existing[0] + existing[2]) * 0.5
            cy = (existing[1] + existing[3]) * 0.5
            if math.hypot(cx - anchor_px[0], cy - anchor_px[1]) < 22:
                continue
            if _segment_hits_box(s0, s1, existing):
                score += 55
                if score >= limit:
                    return score
        if leaders:
            for other, other_box in zip(leaders, leader_aabbs):
                if not _aabb_overlap(stub_box, other_box):
                    continue
                if _segments_cross(s0, s1, other[0], other[1]):
                    score += 280
                    if score >= limit:
                        return score
        if polygon_px and len(polygon_px) >= 3:
            # Sample along the leader — a chord across the figure has mid inside.
            for t in (0.3, 0.5, 0.7):
                pt = (s0[0] + (s1[0] - s0[0]) * t, s0[1] + (s1[1] - s0[1]) * t)
                if _point_in_poly(pt, polygon_px):
                    score += 400
                    break
            else:
                if _point_in_poly(tip, polygon_px):
                    score += 200
        return score

    base_angle = math.atan2(preferred_dir[1], preferred_dir[0])
    dist_steps = sorted({
        min_dist,
        min_dist * 1.35,
        min_dist * 1.8,
        min_dist * 2.4,
        min_dist * 3.2,
        min_dist * 4.2,
        40.0, 64.0, 96.0, 140.0, 200.0, 280.0,
    })
    raw_angles = (
        0, 20, -20, 40, -40, 60, -60, 80, -80, 100, -100,
        120, -120, 140, -140, 160, -160, 180,
    )
    if max_turn_deg is not None:
        angle_steps = tuple(a for a in raw_angles if abs(a) <= max_turn_deg)
        if not angle_steps:
            angle_steps = (0,)
    else:
        angle_steps = raw_angles

    candidates: list[tuple[float, float]] = []
    if preferred_offset is not None:
        candidates.append(preferred_offset)
    for dist in dist_steps:
        for delta_deg in angle_steps:
            angle = base_angle + math.radians(delta_deg)
            candidates.append((dist * math.cos(angle), dist * math.sin(angle)))

    best_offset = None
    best_box = None
    best_hard = 10_000_000
    best_soft = 10_000_000
    seen_off: set[tuple[int, int]] = set()
    for offset in candidates:
        key = (round(offset[0]), round(offset[1]))
        if key in seen_off:
            continue
        seen_off.add(key)
        box = bbox_for(offset)
        hard = hard_score(offset, box, limit=best_hard if best_hard < 10_000_000 else 10_000_000)
        if hard > best_hard:
            continue
        soft = hard + int(math.hypot(offset[0], offset[1]) * 0.02)
        if hard < best_hard or (hard == best_hard and soft < best_soft):
            best_hard = hard
            best_soft = soft
            best_offset = offset
            best_box = box
            if hard == 0:
                break

    if best_offset is None or best_box is None:
        best_offset = (min_dist * pref_unit[0], min_dist * pref_unit[1])
        best_box = bbox_for(best_offset)

    placed.append(best_box)
    if leaders is not None and record_leader:
        tip = (anchor_px[0] + best_offset[0], anchor_px[1] + best_offset[1])
        stub = _leader_stub(anchor_px, tip, trim_px=4.0)
        if stub is not None:
            leaders.append(stub)
    return best_offset
