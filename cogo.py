"""Coordinate geometry for metes-and-bounds traverses.

Handles quadrant bearing parsing, line and curve calls, traverse
computation, and closure analysis.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

# Conversion factors to US survey feet (the customary basis for state plane
# systems and older deeds). Values are exact definitions, not rounded floats.
FEET_PER_METER = 3937.0 / 1200.0  # US survey foot definition
FEET_PER_VARA = 100.0 / 36.0  # Texas vara: 33 1/3 inches

FEET_PER_UNIT = {
    "feet": 1.0,
    "foot": 1.0,
    "ft": 1.0,
    "chains": 66.0,
    "chain": 66.0,
    "ch": 66.0,
    "rods": 16.5,
    "rod": 16.5,
    "poles": 16.5,
    "pole": 16.5,
    "perches": 16.5,
    "perch": 16.5,
    "yards": 3.0,
    "yard": 3.0,
    "yd": 3.0,
    "varas": FEET_PER_VARA,
    "vara": FEET_PER_VARA,
    "vrs": FEET_PER_VARA,
    "meters": FEET_PER_METER,
    "meter": FEET_PER_METER,
    "metres": FEET_PER_METER,
    "metre": FEET_PER_METER,
    "m": FEET_PER_METER,
    "links": 0.66,  # Gunter's link: 100 links per chain
    "link": 0.66,
    "lk": 0.66,
}

# Curve delta/arc/chord agreement is checked in feet of arc, not degrees: a fixed
# degree tolerance is too strict on large radii and too loose on small ones.
_ARC_CONFLICT_FLOOR_FT = 0.15
_ARC_CONFLICT_REL = 0.005


def dms_to_decimal(deg: float, minutes: float = 0.0, seconds: float = 0.0) -> float:
    return deg + minutes / 60.0 + seconds / 3600.0


_QUADRANT_WORDS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W"}

_BEARING_RE = re.compile(
    r"""^\s*
    (?P<ns>[NS])\s*
    (?P<deg>\d{1,3}(?:\.\d+)?)
    (?:\s*[°d:\s]\s*(?P<min>\d{1,2}(?:\.\d+)?))?
    (?:\s*['m:\s]\s*(?P<sec>\d{1,2}(?:\.\d+)?))?
    \s*['"s°]*\s*
    (?P<ew>[EW])\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def _normalize_bearing_text(text: str) -> str:
    """Normalize deed bearing spellings: quadrant words, deg/min/sec words,
    hyphenated DMS ('N 45-30-15 E'), unicode marks."""
    cleaned = " ".join(text.strip().split())
    cleaned = cleaned.replace("º", "°").replace("\u2019", "'").replace("\u201d", '"')
    cleaned = cleaned.replace("\u2032", "'").replace("\u2033", '"')
    # Deed forms like "N. 45° E." — period after a quadrant letter.
    cleaned = re.sub(r"\b([NS])\.\s*", r"\1 ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*([EW])\.\s*$", r" \1", cleaned, flags=re.IGNORECASE)
    # Hyphens between digits are DMS separators.
    cleaned = re.sub(r"(?<=\d)\s*-\s*(?=\d)", " ", cleaned)
    for word, letter in _QUADRANT_WORDS.items():
        cleaned = re.sub(rf"\b{word}\b", letter, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdegrees?\b", "°", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bminutes?\b", "'", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bseconds?\b", '"', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bdeg\b\.?", "°", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bmin\b\.?", "'", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsec\b\.?", '"', cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def parse_bearing(text: str) -> float:
    """Parse a quadrant bearing (or plain numeric azimuth) into an azimuth
    in degrees clockwise from north."""
    cleaned = _normalize_bearing_text(text)
    if not cleaned:
        raise ValueError("Bearing is empty")

    # Plain azimuth like "123.5" or "123°30'15\"" without quadrant letters.
    az_match = re.fullmatch(
        r"(\d{1,3}(?:\.\d+)?)\s*(?:[°d]\s*(\d{1,2}(?:\.\d+)?))?\s*(?:['m]\s*(\d{1,2}(?:\.\d+)?))?\s*['\"s]*",
        cleaned,
    )
    if az_match:
        minutes = float(az_match.group(2) or 0)
        seconds = float(az_match.group(3) or 0)
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"Minutes/seconds must be 0-59 in bearing: {text!r}")
        az = dms_to_decimal(
            float(az_match.group(1)),
            minutes,
            seconds,
        )
        return az % 360.0

    m = _BEARING_RE.match(cleaned)
    if not m:
        raise ValueError(f"Unrecognized bearing: {text!r}")
    ns = m.group("ns").upper()
    ew = m.group("ew").upper()
    minutes = float(m.group("min") or 0)
    seconds = float(m.group("sec") or 0)
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"Minutes/seconds must be 0-59 in bearing: {text!r}")
    angle = dms_to_decimal(float(m.group("deg")), minutes, seconds)
    if angle > 90.0001:
        raise ValueError(f"Bearing angle exceeds 90 degrees: {text!r}")
    if ns == "N" and ew == "E":
        az = angle
    elif ns == "S" and ew == "E":
        az = 180.0 - angle
    elif ns == "S" and ew == "W":
        az = 180.0 + angle
    else:  # N ... W
        az = 360.0 - angle
    return az % 360.0


def azimuth_to_bearing(az: float) -> str:
    """Format an azimuth as a quadrant bearing string."""
    az = az % 360.0
    if az <= 90.0:
        ns, ew, ang = "N", "E", az
    elif az <= 180.0:
        ns, ew, ang = "S", "E", 180.0 - az
    elif az <= 270.0:
        ns, ew, ang = "S", "W", az - 180.0
    else:
        ns, ew, ang = "N", "W", 360.0 - az
    total_sec = round(ang * 3600)
    d, rem = divmod(total_sec, 3600)
    mnt, s = divmod(rem, 60)
    return f"{ns} {int(d):02d}\u00b0{int(mnt):02d}'{int(s):02d}\" {ew}"


QUADRANT_OPTIONS = ("NE", "NW", "SE", "SW")
_QUADRANT_LETTERS = {"NE": ("N", "E"), "NW": ("N", "W"), "SE": ("S", "E"), "SW": ("S", "W")}


def decompose_quadrant_bearing(text: str) -> Optional[tuple[str, int, int, int]]:
    """Split a bearing into (quadrant, degrees, minutes, seconds), or None if
    it isn't a standard quadrant bearing."""
    cleaned = _normalize_bearing_text(text)
    if not cleaned:
        return None
    m = _BEARING_RE.match(cleaned)
    if not m:
        return None
    quadrant = f"{m.group('ns').upper()}{m.group('ew').upper()}"
    if quadrant not in _QUADRANT_LETTERS:
        return None
    deg_f = float(m.group("deg"))
    min_f = float(m.group("min") or 0)
    sec_f = float(m.group("sec") or 0)
    # Fractional DMS belongs in the text editor — do not round 30.5' to 31'.
    if deg_f != int(deg_f) or min_f != int(min_f) or sec_f != int(sec_f):
        return None
    deg = int(deg_f)
    mnt = int(min_f)
    sec = int(sec_f)
    if sec >= 60:
        carry, sec = divmod(sec, 60)
        mnt += carry
    if mnt >= 60:
        carry, mnt = divmod(mnt, 60)
        deg += carry
    if deg > 90:
        return None
    return (quadrant, deg, mnt, sec)


def format_quadrant_bearing(quadrant: str, deg: int, mnt: int, sec: int) -> str:
    q1, q2 = _QUADRANT_LETTERS[quadrant]
    return f"{q1} {deg:02d}\u00b0{mnt:02d}'{sec:02d}\" {q2}"


@dataclass
class Call:
    """One traverse call: a straight line or a circular curve."""

    call_type: str = "line"  # "line" or "curve"
    bearing: str = ""  # quadrant bearing text for lines; chord bearing for curves
    distance: float = 0.0  # feet; chord length ignored for curves if arc data given
    units: str = "feet"
    # Curve fields
    curve_direction: str = ""  # "left" or "right"
    radius: float = 0.0
    arc_length: float = 0.0
    chord_bearing: str = ""
    chord_length: float = 0.0
    delta: str = ""  # central angle, DMS text or decimal degrees
    monument: str = ""  # corner monumentation at the END of this call, e.g. "1/2\" iron rod found"
    description: str = ""  # source call text
    confidence: str = ""
    # Non-empty when a table cell could not be parsed (e.g. "100 ft" in Distance).
    input_error: str = ""
    # Original cell text when that field failed to parse (survives set_calls).
    input_distance: str = ""
    input_radius: str = ""
    input_arc_length: str = ""
    input_chord_length: str = ""

    def distance_feet(self) -> float:
        return self.distance * FEET_PER_UNIT.get(self.units.lower().strip(), 1.0)

    def unit_factor(self) -> float:
        return FEET_PER_UNIT.get(self.units.lower().strip(), 1.0)


@dataclass
class Segment:
    """A computed traverse leg with geometry ready for plotting/export."""

    sequence: int  # 1-based call number
    kind: str  # "line" or "curve"
    start: tuple[float, float]
    end: tuple[float, float]
    path: list[tuple[float, float]]  # includes start and end; sampled for curves
    length_ft: float  # line length or curve chord length (plot distance basis)
    call: Call


@dataclass
class TraverseResult:
    points: list[tuple[float, float]] = field(default_factory=list)  # (E, N)
    segments: list[Segment] = field(default_factory=list)
    segment_endpoints: list[tuple[float, float]] = field(default_factory=list)
    closure_error: float = 0.0
    closure_bearing: str = ""
    misclosure_x: float = 0.0  # end-to-POB delta east (signed, ft)
    misclosure_y: float = 0.0  # end-to-POB delta north (signed, ft)
    perimeter: float = 0.0
    precision: str = ""
    area_sqft: float = 0.0
    area_acres: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # (1-based call number, vertex) for invalid rows — later courses still run
    # from this vertex; plot/DXF draw a gap marker instead of a fake side.
    gaps: list[tuple[int, tuple[float, float]]] = field(default_factory=list)


def curve_direction_is_left(direction: str) -> bool:
    """True when deed/AI text means a left / CCW turn."""
    d = (direction or "").strip().lower().replace("_", " ").replace("-", " ")
    if not d:
        return False
    compact = d.replace(" ", "")
    if compact in ("ccw", "counterclockwise") or d.startswith("counter"):
        return True
    if compact in ("cw", "clockwise") or d == "clock wise":
        return False
    return d.startswith("l")


def stated_pob_xy(pob_coordinates: dict | None) -> tuple[float, float] | None:
    """Both-axes stated POB (easting, northing), or None if either axis is missing."""
    if not pob_coordinates:
        return None
    try:
        e = pob_coordinates.get("easting")
        n = pob_coordinates.get("northing")
        if e is None or n is None:
            return None
        return (float(e), float(n))
    except (TypeError, ValueError):
        return None


def traverse_start_from_pob(pob_coordinates: dict | None) -> tuple[float, float]:
    """Stated POB easting/northing, or the local (5000, 5000) origin."""
    xy = stated_pob_xy(pob_coordinates)
    return xy if xy is not None else (5000.0, 5000.0)


def _parse_delta(text: str) -> Optional[float]:
    if not text:
        return None
    t = _normalize_bearing_text(text.strip().replace("º", "°"))
    m = re.match(
        r"^\s*(\d{1,3}(?:\.\d+)?)\s*[°d:\s]\s*(\d{1,2}(?:\.\d+)?)?\s*['m:\s]?\s*(\d{1,2}(?:\.\d+)?)?",
        t,
    )
    if m:
        minutes = float(m.group(2) or 0)
        seconds = float(m.group(3) or 0)
        if minutes >= 60 or seconds >= 60:
            return None  # treat as unreadable; caller falls through to arc/chord
        return dms_to_decimal(float(m.group(1)), minutes, seconds)
    bare = re.sub(r"[°d]\s*$", "", t, flags=re.IGNORECASE).strip()
    try:
        return float(bare)
    except ValueError:
        return None


def curve_can_derive_chord(
    radius: float, delta: str = "", arc_length: float = 0.0,
) -> bool:
    """True when Dist must not be copied or plotted as a stated chord.

    Any positive radius: chord from R+Δ/arc, or Dist treated as the arc.
    A readable Delta (even with R empty): Dist/Arc are the arc, or Chord+Δ
    already recovers R — do not Dist↔Chord copy over a stated chord.
    Dist-only curves (no R, no Δ) still Dist-as-chord so they plot.
    ``arc_length`` is accepted for existing call sites.
    """
    if (radius or 0.0) > 0:
        return True
    return _parse_delta(delta) is not None


def chord_copied_from_distance(distance: float, chord_length: float) -> bool:
    """True when Chord Len matches Dist (the Dist-only live copy)."""
    d = float(distance or 0.0)
    c = float(chord_length or 0.0)
    if d <= 0.0 or c <= 0.0:
        return False
    return abs(c - d) <= max(1e-4, 1e-6 * max(abs(c), abs(d)))


def newly_derivable_copied_chord(
    derive_now: bool,
    derive_before: bool,
    distance: float,
    chord_length: float,
) -> bool:
    """True when Dist==Chord should be cleared because R/Δ just became usable."""
    return (
        bool(derive_now)
        and not derive_before
        and chord_copied_from_distance(distance, chord_length)
    )


def normalize_call_type(raw: str, *, has_curve_data: bool = False) -> str:
    """Map a type string to ``line`` or ``curve``.

    Strips padding (``"curve "``). ``arc`` / ``circular`` are curves.
    An empty or unknown type with radius/Δ/arc/chord is a curve so Import
    and Parse do not flatten those rows to straight Dist.
    """
    t = str(raw or "").strip().lower()
    if t in ("line", "curve"):
        return t
    if t in ("arc", "circular") or t.startswith("curve"):
        return "curve"
    return "curve" if has_curve_data else "line"


def _asin_half_chord(radius: float, chord_len: float) -> Optional[float]:
    """chord/(2R) for asin, clamped to 1 so a slightly long chord still draws."""
    if radius <= 0 or chord_len <= 0:
        return None
    half = chord_len / (2.0 * radius)
    if half < 1e-15:
        return None
    return 1.0 if half > 1.0 else half


def _resolve_curve_delta(call: Call, seq: int, warnings: list[str]) -> Optional[float]:
    """Best delta (degrees) from delta/arc/chord, or Dist as arc when those are empty."""
    unit = call.unit_factor()
    radius = call.radius * unit
    candidates: list[tuple[str, float]] = []
    delta = _parse_delta(call.delta)
    if (call.delta or "").strip() and delta is None:
        warnings.append(
            f"Call {seq}: delta {call.delta!r} is unreadable; using arc/chord."
        )
    if delta is not None:
        candidates.append(("delta", delta))
    if radius > 0 and call.arc_length > 0:
        candidates.append(("arc length", math.degrees(call.arc_length * unit / radius)))
    if radius > 0 and call.chord_length > 0:
        half = _asin_half_chord(radius, call.chord_length * unit)
        if half is not None:
            minor = math.degrees(2.0 * math.asin(half))
            chord_delta = minor
            if candidates:
                stated = candidates[0][1]
                major = 360.0 - minor
                if abs(stated - major) < abs(stated - minor):
                    chord_delta = major
            candidates.append(("chord length", chord_delta))
    # Dist is the transcribed arc when Δ/Arc/Chord are empty, and also when it
    # is filled alongside them — unless Dist==Chord (the Dist↔Chord live copy
    # / stated equal chord, not an arc).
    if radius > 0 and call.distance > 0:
        if not candidates or not chord_copied_from_distance(
            call.distance, call.chord_length
        ):
            candidates.append(
                ("distance", math.degrees(call.distance * unit / radius))
            )
    if not candidates:
        return None

    primary_name, primary = candidates[0]
    if radius > 0:
        primary_arc = math.radians(primary) * radius
        for name, value in candidates[1:]:
            arc = math.radians(value) * radius
            tol = max(_ARC_CONFLICT_FLOOR_FT, _ARC_CONFLICT_REL * max(abs(arc), abs(primary_arc)))
            if abs(arc - primary_arc) > tol:
                warnings.append(
                    f"Call {seq}: curve {primary_name} and {name} disagree "
                    f"({primary:.4f}\u00b0 vs {value:.4f}\u00b0). "
                    f"Drawing the arc from the stated chord and radius."
                )
    return primary


def _implied_delta_from_chord(radius: float, chord_len: float) -> Optional[float]:
    """Minor central angle (degrees) of the circular arc through a chord of *chord_len*."""
    half = _asin_half_chord(radius, chord_len)
    if half is None:
        return None
    return math.degrees(2.0 * math.asin(half))


def _plot_delta_from_chord(
    radius: float, chord_len: float, stated_delta: Optional[float],
) -> Optional[float]:
    """Implied delta through the chord ends; prefer the major arc when stated Δ > 180°."""
    minor = _implied_delta_from_chord(radius, chord_len)
    if minor is None:
        return stated_delta
    if stated_delta is None:
        return minor
    major = 360.0 - minor
    if abs(stated_delta - major) < abs(stated_delta - minor):
        return major
    return minor


def _curve_geometry(
    call: Call,
    start: tuple[float, float],
    seq: int,
    incoming_azimuth: Optional[float],
    warnings: list[str],
) -> tuple[tuple[float, float], list[tuple[float, float]], float, Optional[float], float]:
    """Compute curve end, path, chord length, outgoing azimuth, and arc length.

    Uses chord bearing + chord geometry when the deed provides one; otherwise
    falls back to tangent continuity from the previous course. Arc length is
    for perimeter / precision; chord remains the plot distance basis.

    When Delta (or arc) disagrees with Chord, ends stay on the stated chord
    and the bulge uses Radius (implied delta). Chord+radius has two solutions
    (Δ and 360−Δ); honor a stated major Δ. Table radius is not rewritten
    when R is recovered from chord+delta for drawing only.
    """
    unit = call.unit_factor()
    radius = call.radius * unit
    delta = _resolve_curve_delta(call, seq, warnings)
    dir_raw = (call.curve_direction or "").strip()
    if not dir_raw:
        warnings.append(
            f"Call {seq}: curve has no left/right direction; "
            f"assuming right (arc side and area may be wrong)."
        )
    is_left = curve_direction_is_left(dir_raw)
    turn_sign = -1.0 if is_left else 1.0

    chord_bearing_text = call.chord_bearing or call.bearing
    if chord_bearing_text:
        chord_az = parse_bearing(chord_bearing_text)
    elif incoming_azimuth is not None and delta is not None:
        # Tangent continuity: chord bisects the swept angle.
        chord_az = (incoming_azimuth + turn_sign * delta / 2.0) % 360.0
        warnings.append(
            f"Call {seq}: curve has no chord bearing; derived it from the previous "
            f"course's tangent and a {delta:.2f}\u00b0 delta."
        )
    else:
        raise ValueError("Curve is missing a chord bearing (and no tangent to derive it from)")

    chord_len = (call.chord_length or 0.0) * unit
    if chord_len <= 0:
        if radius > 0 and delta:
            chord_len = 2.0 * radius * math.sin(math.radians(delta) / 2.0)
        elif radius <= 0 and delta:
            # No stated chord: Dist/Arc are the transcribed arc; R = arc/θ.
            arc_ft = 0.0
            if call.arc_length > 0:
                arc_ft = call.arc_length * unit
            elif call.distance > 0:
                arc_ft = call.distance * unit
            theta = math.radians(delta)
            if arc_ft > 0 and abs(theta) > 1e-15:
                radius = arc_ft / abs(theta)
                chord_len = 2.0 * radius * math.sin(abs(theta) / 2.0)
        if chord_len <= 0:
            if call.distance > 0:
                chord_len = call.distance * unit
            else:
                raise ValueError("Curve is missing chord length and radius/delta")

    # Deed omitted R but gave chord + delta — recover R for the bulge only.
    if radius <= 0 and chord_len > 0 and delta:
        half = math.sin(math.radians(delta) / 2.0)
        if abs(half) > 1e-12:
            radius = chord_len / (2.0 * abs(half))

    az_rad = math.radians(chord_az)
    end = (start[0] + chord_len * math.sin(az_rad), start[1] + chord_len * math.cos(az_rad))
    if radius > 0 and chord_len > 2.0 * radius:
        warnings.append(
            f"Call {seq}: chord length exceeds the curve's diameter; "
            f"drawing a semicircle through the stated chord ends."
        )
    plot_delta = _plot_delta_from_chord(radius, chord_len, delta)
    outgoing = (
        (chord_az + turn_sign * plot_delta / 2.0) % 360.0
        if plot_delta is not None else None
    )

    # Sample the arc for plotting (implied delta through the stated chord ends).
    path = [start, end]
    if radius > 0 and plot_delta and plot_delta > 0.05:
        half_delta = math.radians(plot_delta) / 2.0
        mid = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        sagitta = radius * (1.0 - math.cos(half_delta))
        perp_az = az_rad + (math.pi / 2.0 if is_left else -math.pi / 2.0)
        center = (
            mid[0] - (radius - sagitta) * math.sin(perp_az),
            mid[1] - (radius - sagitta) * math.cos(perp_az),
        )
        a0 = math.atan2(start[0] - center[0], start[1] - center[1])
        sweep = math.radians(plot_delta) * turn_sign
        n = max(8, int(abs(plot_delta) / 2) + 2)
        path = [
            (
                center[0] + radius * math.sin(a0 + sweep * t / n),
                center[1] + radius * math.cos(a0 + sweep * t / n),
            )
            for t in range(n + 1)
        ]
        path[0] = start
        path[-1] = end

    # Perimeter uses the drawn arc; segment.length_ft stays chord (plot/label).
    # An undrawn Arc Len (no R/Δ to bulge) must not inflate Perimeter.
    if radius > 0 and plot_delta is not None and plot_delta > 0:
        arc_len = radius * math.radians(plot_delta)
    else:
        arc_len = chord_len
    return end, path, chord_len, outgoing, arc_len


def _skip_distance_input_error(call: Call) -> bool:
    """True when Dist is unreadable but the curve can still draw from other fields."""
    err = (call.input_error or "")
    if not err.startswith("Distance is not a number"):
        return False
    if (call.call_type or "").lower() != "curve":
        return False
    if call.input_radius or call.input_arc_length or call.input_chord_length:
        return False
    r = float(call.radius or 0.0)
    chord = float(call.chord_length or 0.0)
    arc = float(call.arc_length or 0.0)
    has_delta = _parse_delta(call.delta) is not None
    if chord > 0:
        return True
    if r > 0 and (has_delta or arc > 0):
        return True
    if arc > 0 and has_delta:
        return True
    return False


def compute_traverse(calls: list[Call], start: tuple[float, float] = (5000.0, 5000.0)) -> TraverseResult:
    """Run the traverse from a starting point and compute closure/area."""
    result = TraverseResult()
    pos = start
    result.points.append(pos)
    result.segment_endpoints.append(pos)
    incoming_azimuth: Optional[float] = None

    for i, call in enumerate(calls, start=1):
        try:
            if call.input_error:
                if _skip_distance_input_error(call):
                    shown = call.input_distance or call.input_error
                    result.warnings.append(
                        f"Call {i}: distance {shown!r} is unreadable; "
                        f"using chord/radius."
                    )
                else:
                    raise ValueError(call.input_error)
            if call.call_type.lower() == "curve":
                end, path, chord, outgoing, arc_len = _curve_geometry(
                    call, pos, i, incoming_azimuth, result.warnings
                )
                result.points.extend(path[1:])
                result.segments.append(
                    Segment(sequence=i, kind="curve", start=pos, end=end,
                            path=path, length_ft=chord, call=call)
                )
                result.perimeter += arc_len
                incoming_azimuth = outgoing if outgoing is not None else incoming_azimuth
                pos = end
            else:
                az = parse_bearing(call.bearing)
                dist = call.distance_feet()
                if dist <= 0:
                    raise ValueError("Distance must be positive")
                rad = math.radians(az)
                end = (pos[0] + dist * math.sin(rad), pos[1] + dist * math.cos(rad))
                result.points.append(end)
                result.segments.append(
                    Segment(sequence=i, kind="line", start=pos, end=end,
                            path=[pos, end], length_ft=dist, call=call)
                )
                result.perimeter += dist
                incoming_azimuth = az
                pos = end
            result.segment_endpoints.append(pos)
        except ValueError as exc:
            result.errors.append(f"Call {i}: {exc}")
            result.gaps.append((i, pos))

    if any(call.units.lower().strip().startswith("var") or call.units.lower().strip() == "vrs"
           for call in calls):
        result.warnings.append(
            "Distances in varas were converted at the Texas vara "
            "(33\u2153 in = 2.7778 ft). Verify: vara length varies by state/era."
        )

    unknown_units = sorted({
        (call.units or "").strip()
        for call in calls
        if (call.units or "").strip()
        and (call.units or "").lower().strip() not in FEET_PER_UNIT
    })
    if unknown_units:
        shown = ", ".join(repr(u) for u in unknown_units[:5])
        result.warnings.append(
            f"Unrecognized unit(s) {shown} treated as feet. "
            f"Use feet, chains, rods, varas, meters, or links."
        )

    dx = start[0] - pos[0]
    dy = start[1] - pos[1]
    result.misclosure_x = pos[0] - start[0]
    result.misclosure_y = pos[1] - start[1]
    result.closure_error = math.hypot(dx, dy)
    if result.closure_error > 1e-9:
        result.closure_bearing = azimuth_to_bearing(math.degrees(math.atan2(dx, dy)) % 360.0)
    if result.closure_error > 1e-6 and result.perimeter > 0:
        result.precision = f"1:{result.perimeter / result.closure_error:,.0f}"
    else:
        result.precision = "exact"

    # Shoelace area over the sampled boundary (closing back to start).
    pts = result.points + [start]
    area2 = sum(pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1] for i in range(len(pts) - 1))
    result.area_sqft = abs(area2) / 2.0
    result.area_acres = result.area_sqft / 43560.0
    return result
