#!/usr/bin/env python3
"""Report placed schematic style facts without changing the KiCad source.

Reports bounded geometry candidates and per-object coverage gaps. This is not
a connectivity, font-overlap, general collision, or overall style validator.
Only Python's standard library is required.
"""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from probe_geometry import (
    hierarchy_gaps,
    inspect_flag_wire_crossings,
    inspect_ground_wire_crossings,
    inspect_power_orientation,
    inspect_power_value_placement,
    inspect_wire_pin_overlaps,
)


def parse_sexpr(text):
    stack = []
    roots = []
    position = 0
    for match in re.finditer(r'\s+|"(?:\\.|[^"\\])*"|[()]|[^\s()"]+', text):
        if match.start() != position:
            raise ValueError(f"Invalid token at offset {position}")
        position = match.end()
        token = match.group()
        if token.isspace():
            continue
        if token == "(":
            node = []
            (stack[-1] if stack else roots).append(node)
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unexpected closing parenthesis")
            stack.pop()
        else:
            if not stack:
                raise ValueError("Atom outside root")
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    if position != len(text) or stack or len(roots) != 1 or not roots[0] or roots[0][0] != "kicad_sch":
        raise ValueError("Expected one complete kicad_sch root")
    return roots[0]


def children(node, key):
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == key]


def child(node, key):
    return next(iter(children(node, key)), [])


def tail(node, key):
    return child(node, key)[1:]


def hidden(node):
    # KiCad versions use property-level (hide yes), effects-level (hide yes), or bare hide.
    for owner in (node, child(node, "effects")):
        if "hide" in owner or tail(owner, "hide") == ["yes"]:
            return True
    return False


def field_info(node):
    font = child(child(node, "effects"), "font")
    return {
        "text": node[2],
        "hidden": hidden(node),
        "at_raw": tail(node, "at"),
        "size_mm": tail(font, "size"),
        "justify_raw": tail(child(node, "effects"), "justify"),
    }


def decimal_point(values):
    try:
        point = tuple(Decimal(value) for value in values[:2])
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Invalid coordinate: {values}") from error
    if len(point) != 2 or not all(value.is_finite() for value in point):
        raise ValueError(f"Expected two finite coordinates: {values}")
    return point


def point_strings(point):
    return [format(value, "f") for value in point]


def inspect_redundancy(root, power_records):
    """Inspect root object geometry; exact decimal equality is not a net check."""
    duplicate_groups = defaultdict(list)
    line_groups = defaultdict(list)
    zero_length = []
    for index, wire in enumerate(children(root, "wire"), 1):
        pts = children(child(wire, "pts"), "xy")
        if len(pts) != 2:
            continue
        raw = [point[1:3] for point in pts]
        try:
            a, b = sorted(decimal_point(point) for point in raw)
        except ValueError:
            continue
        record = {"wire_index": index, "uuid": next(iter(tail(wire, "uuid")), None), "points_raw": raw}
        duplicate_groups[(a, b)].append(record)
        if a == b:
            zero_length.append(record)
        elif a[1] == b[1]:
            line_groups[("horizontal", a[1])].append((a[0], b[0], record))
        elif a[0] == b[0]:
            line_groups[("vertical", a[0])].append((a[1], b[1], record))

    overlaps = []
    for (axis, fixed), segments in line_groups.items():
        segments.sort(key=lambda segment: (segment[0], segment[1]))
        for index, (start, end, record) in enumerate(segments):
            for other_start, other_end, other in segments[index + 1:]:
                if other_start >= end:
                    break
                # Exact/reversed duplicates are reported separately, only once.
                if (start, end) == (other_start, other_end):
                    continue
                lo, hi = other_start, min(end, other_end)
                points = [(lo, fixed), (hi, fixed)] if axis == "horizontal" else [(fixed, lo), (fixed, hi)]
                overlaps.append({"wires": [record, other], "overlap_mm": [point_strings(point) for point in points]})

    junction_groups = defaultdict(list)
    skipped = []
    for index, junction in enumerate(children(root, "junction"), 1):
        raw = tail(junction, "at")
        record = {
            "junction_index": index, "uuid": next(iter(tail(junction, "uuid")), None), "at_raw": raw,
        }
        try:
            junction_groups[decimal_point(raw)].append(record)
        except ValueError:
            skipped.append({**record, "object_kind": "junction", "reason": "malformed_junction_position",
                            "roi_mm": None, "review_hint": "Locate this junction by UUID and inspect its native source/render."})
    power_groups = defaultdict(list)
    for record in power_records:
        if len(record["at_raw"]) >= 2:
            try:
                origin = decimal_point(record["at_raw"])
            except ValueError:
                continue
            power_groups[origin].append({
                key: record[key] for key in ("symbol_index", "uuid", "reference", "lib_id", "at_raw")
            })
    return {
        "duplicate_wires": [
            {"points_mm": [point_strings(point) for point in points], "wires": wires}
            for points, wires in duplicate_groups.items() if len(wires) > 1
        ],
        "overlapping_wires": overlaps,
        "zero_length_wires": zero_length,
        "duplicate_junctions": [
            {"at_mm": point_strings(point), "junctions": junctions}
            for point, junctions in junction_groups.items() if len(junctions) > 1
        ],
        "coincident_power_origins": [
            {"at_mm": point_strings(point), "candidates": records}
            for point, records in power_groups.items() if len(records) > 1
        ],
        "skipped_objects": skipped,
        "limitations": (
            "Root objects only; coordinates use exact Decimal equality without snapping or tolerance. "
            "Wire overlap covers positive-length, axis-aligned collinear pairs, excluding exact duplicates, "
            "endpoint contact and crossings. Power-origin coincidences are candidates only: no pin transforms, "
            "rendered collision check or judgment about electrical necessity, including PWR_FLAG. "
            "Indices are one-based within each root object kind; malformed non-two-point wires are skipped."
        ),
    }


def inspect(path, details=False):
    source = path.read_bytes()
    root = parse_sexpr(source.decode("utf-8-sig"))
    placed = children(root, "symbol")
    counts = Counter(item[0] for item in root[1:] if isinstance(item, list) and item)
    visible_fonts = Counter()
    records = []
    power_records = []
    power_lib_ids = {
        symbol[1]
        for symbol in children(child(root, "lib_symbols"), "symbol")
        if len(symbol) >= 2 and isinstance(symbol[1], str) and child(symbol, "power")
    }
    for index, symbol in enumerate(placed, 1):
        props = {p[1]: field_info(p) for p in children(symbol, "property") if len(p) >= 3}
        ref = props.get("Reference", {}).get("text", "")
        lib_id = tail(symbol, "lib_id")
        record = {
            "symbol_index": index,
            "uuid": next(iter(tail(symbol, "uuid")), None),
            "reference": ref,
            "lib_id": lib_id,
            "unit": tail(symbol, "unit"),
            "at_raw": tail(symbol, "at"),
            "mirror": tail(symbol, "mirror"),
            "fields": {k: v for k, v in props.items() if k in ("Reference", "Value")},
        }
        is_power = (
            ref.startswith(("#PWR", "#FLG"))
            or bool(lib_id and (lib_id[0].startswith("power:") or lib_id[0] in power_lib_ids))
        )
        if is_power:
            power_records.append(record)
            continue
        if ref.startswith("#"):
            continue
        for value in props.values():
            if not value["hidden"] and value["text"]:
                visible_fonts["x".join(value["size_mm"])] += 1
        records.append(record)
    diagonals = []
    lengths = Counter()
    for wire in children(root, "wire"):
        pts = children(child(wire, "pts"), "xy")
        if len(pts) != 2:
            continue
        try:
            (x1, y1), (x2, y2) = (decimal_point(p[1:]) for p in pts)
        except ValueError:
            continue
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx > 1e-6 and dy > 1e-6:
            diagonals.append([pts[0][1:], pts[1][1:]])
        lengths[f"{(dx * dx + dy * dy).sqrt():.4f}"] += 1
    resistor_records = [r for r in records if re.fullmatch(r"R\d+", r["reference"])]
    paper = tail(root, "paper")
    sizes = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
    dimensions = sizes.get(paper[0]) if paper else None
    if paper and paper[0] == "User" and len(paper) >= 3:
        dimensions = (float(paper[1]), float(paper[2]))
    if dimensions and "portrait" in paper:
        dimensions = dimensions[::-1]
    outside = []
    if dimensions:
        for record in records:
            if len(record["at_raw"]) >= 2:
                try:
                    x, y = decimal_point(record["at_raw"])
                except ValueError:
                    continue
                if not (0 <= x <= dimensions[0] and 0 <= y <= dimensions[1]):
                    outside.append({"reference": record["reference"], "at_raw": record["at_raw"]})
    result = {
        "schema_version": 2,
        "report_kind": "facts_and_candidates",
        "source": str(path.resolve()),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "paper": tail(root, "paper"),
        "root_counts": dict(counts),
        "non_power_placed_units": len(records),
        "unique_non_power_refs_on_this_sheet": len({r["reference"] for r in records}),
        "power_placed_units": len(power_records),
        "visible_power_references": [
            {"reference": r["reference"], "lib_id": r["lib_id"], "at_raw": r["at_raw"]}
            for r in power_records
            if r["reference"] and not r["fields"].get("Reference", {}).get("hidden", True)
        ],
        "resistor_units": len(resistor_records),
        "hidden_resistor_refs": [r["reference"] for r in resistor_records if r["fields"].get("Reference", {}).get("hidden", True)],
        "visible_property_fonts_mm": dict(visible_fonts),
        "wire_lengths_mm": dict(lengths.most_common()),
        "diagonal_wires": diagonals,
        "paper_dimensions_mm": dimensions,
        "off_page_symbol_anchors": outside,
        "power_value_placement": inspect_power_value_placement(root, power_records),
        "power_orientation": inspect_power_orientation(root, power_records),
        "flag_wire_crossings": inspect_flag_wire_crossings(root, power_records),
        "ground_wire_crossings": inspect_ground_wire_crossings(root, power_records),
        "redundancy": inspect_redundancy(root, power_records),
        "wire_pin_overlaps": inspect_wire_pin_overlaps(root),
        "hierarchy_gaps": hierarchy_gaps(root),
        "limitations": "One file only; child sheets are listed but not traversed. Bounded orthogonal pin/power geometry screening is described per section; unsupported objects are not passed. No connectivity, general body/text collision, glyph-fit, density or overall style verdict. Field coordinates are page coordinates; raw field angles alone are not screen angles. Page check covers symbol origins only, not complete bounds or drawing-sheet margins. ROI boxes are viewing aids with context, never permission to replace their contents.",
    }
    if details:
        result["placed_units"] = records
        result["power_units"] = power_records
        result["labels"] = [
            {"kind": kind, "text": label[1], "at_raw": tail(label, "at"), "shape": tail(label, "shape")}
            for kind in ("label", "global_label", "hierarchical_label")
            for label in children(root, kind)
        ]
        result["texts"] = [
            {"text": t[1], "at_raw": tail(t, "at"), "effects": child(t, "effects")}
            for t in children(root, "text")
        ]
        result["rectangles"] = children(root, "rectangle")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("schematics", type=Path, nargs="+")
    parser.add_argument("--details", action="store_true", help="Include placed fields, labels and section geometry")
    parser.add_argument("--output", type=Path, help="Write JSON report instead of stdout")
    args = parser.parse_args()
    try:
        result = [inspect(path, args.details) for path in args.schematics]
    except (OSError, ValueError, IndexError) as error:
        parser.exit(1, f"Cannot inspect schematic: {error}\n")
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        sources = {path.resolve() for path in args.schematics}
        if args.output.resolve() in sources:
            parser.error("Output must not overwrite an input schematic")
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
