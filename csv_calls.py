"""Import/export boundary calls as CSV."""

from __future__ import annotations

import csv
from pathlib import Path

from cogo import Call, normalize_call_type

EXPORT_HEADER = [
    "type", "bearing", "distance", "units", "radius",
    "arc_length", "chord_length", "delta", "direction",
    "monument", "description", "confidence",
]

# Flexible header aliases → canonical field names.
_ALIASES = {
    "type": "type",
    "call_type": "type",
    "call type": "type",
    "bearing": "bearing",
    "chord bearing": "bearing",
    "bearing / chord brg": "bearing",
    "distance": "distance",
    "dist": "distance",
    "length": "distance",
    "units": "units",
    "unit": "units",
    "radius": "radius",
    "arc_length": "arc_length",
    "arc length": "arc_length",
    "arc len": "arc_length",
    "chord_length": "chord_length",
    "chord length": "chord_length",
    "chord len": "chord_length",
    "delta": "delta",
    "direction": "direction",
    "dir": "direction",
    "curve_direction": "direction",
    "monument": "monument",
    "monument at end": "monument",
    "description": "description",
    "desc": "description",
    "confidence": "confidence",
    "conf": "confidence",
}


def _num(text: str, *, field: str = "value") -> float:
    text = (text or "").strip().replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"Invalid {field} {text!r} — expected a number.") from None


def calls_to_rows(calls: list[Call]) -> list[list]:
    rows = []
    for c in calls:
        rows.append([
            c.call_type,
            c.chord_bearing or c.bearing,
            c.distance,
            c.units,
            c.radius,
            c.arc_length,
            c.chord_length,
            c.delta,
            c.curve_direction,
            c.monument,
            c.description,
            c.confidence,
        ])
    return rows


def write_calls_csv(path: str | Path, calls: list[Call]) -> None:
    # utf-8-sig (BOM) so Excel on Windows opens degree/quotes correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(EXPORT_HEADER)
        writer.writerows(calls_to_rows(calls))


def read_calls_csv(path: str | Path) -> list[Call]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError("CSV has no header row.")
        mapping: dict[str, str] = {}
        for raw in reader.fieldnames:
            key = _ALIASES.get((raw or "").strip().lower())
            if key:
                mapping[key] = raw
        if "bearing" not in mapping and "distance" not in mapping:
            raise ValueError(
                "CSV needs at least a bearing or distance column "
                "(recognized headers: type, bearing, distance, units, …)."
            )
        calls: list[Call] = []
        for row_num, row in enumerate(reader, start=2):
            def get(name: str) -> str:
                src = mapping.get(name)
                return (row.get(src) or "").strip() if src else ""

            call_type = normalize_call_type(
                get("type"),
                has_curve_data=bool(
                    get("radius") or get("arc_length") or get("chord_length")
                    or get("delta")
                ),
            )
            bearing = get("bearing")
            try:
                distance = _num(get("distance"), field="distance")
                radius = _num(get("radius"), field="radius")
                arc_length = _num(get("arc_length"), field="arc_length")
                chord_length = _num(get("chord_length"), field="chord_length")
            except ValueError as exc:
                raise ValueError(f"Row {row_num}: {exc}") from None
            if call_type == "line":
                chord_length = 0.0
            # Skip completely blank data rows (header-only trailing empties).
            if not any((call_type != "line", bearing, get("distance"), get("radius"),
                        get("arc_length"), get("chord_length"), get("monument"),
                        get("description"), get("delta"))):
                continue
            calls.append(Call(
                call_type=call_type,
                bearing=bearing,
                distance=distance,
                units=get("units") or "feet",
                radius=radius,
                arc_length=arc_length,
                chord_bearing=bearing if call_type == "curve" else "",
                chord_length=chord_length,
                delta=get("delta"),
                curve_direction=get("direction"),
                monument=get("monument"),
                description=get("description"),
                confidence=get("confidence"),
            ))
        return calls
