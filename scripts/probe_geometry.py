"""Read-only, deliberately bounded KiCad schematic geometry screening.

Coordinates use Decimal. No connectivity, font, general collision, or layout
quality is inferred; unsupported objects are returned for directed review.
"""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import re


def children(node, key):
    return [n for n in node[1:] if isinstance(n, list) and n and n[0] == key]


def child(node, key):
    return next(iter(children(node, key)), [])


def tail(node, key):
    return child(node, key)[1:]


def number(value):
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("Invalid numeric geometry") from error
    if not result.is_finite():
        raise ValueError("Nonfinite numeric geometry")
    return result


def point(values):
    if len(values) < 2:
        raise ValueError("Expected two coordinates")
    return tuple(number(v) for v in values[:2])


def strings(values):
    return [format(v, "f") for v in values]


def normalized_angle(value):
    return (number(value) % 360 + 360) % 360


def hidden(node):
    return any("hide" in owner or tail(owner, "hide") == ["yes"]
               for owner in (node, child(node, "effects")))


def roi(points, margin="2.54"):
    """Viewing extent, not an area authorized for deletion or replacement."""
    points = list(points)
    if not points:
        return None
    margin = number(margin)
    xs, ys = zip(*points)
    return dict(zip(("left", "top", "right", "bottom"),
                    strings((min(xs) - margin, min(ys) - margin,
                             max(xs) + margin, max(ys) + margin))))


def instance_record(symbol, index):
    props = {p[1]: p[2] for p in children(symbol, "property") if len(p) >= 3}
    record = {
        "symbol_index": index,
        "reference": props.get("Reference", ""),
        "symbol_uuid": next(iter(tail(symbol, "uuid")), None),
        "lib_id": next(iter(tail(symbol, "lib_id")), ""),
        "unit": next(iter(tail(symbol, "unit")), "1"),
        "at_raw": tail(symbol, "at"),
        "mirror": tail(symbol, "mirror"),
    }
    try:
        record["roi_mm"] = roi((point(record["at_raw"]),))
    except ValueError:
        record["roi_mm"] = None
    return record


def local_transform(local, angle, mirror=None):
    """KiCad rotates library XY, flips Y, then mirrors in page axes.

    Order verified against native netlist export for all 12 orthogonal
    rotation/mirror combinations, using asymmetric pin coordinates.
    """
    x, y = map(number, local)
    angle = normalized_angle(angle)
    if angle == 0:
        dx, dy = x, -y
    elif angle == 90:
        dx, dy = -y, -x
    elif angle == 180:
        dx, dy = -x, y
    elif angle == 270:
        dx, dy = y, x
    else:
        raise ValueError("nonorthogonal_instance_rotation")
    if mirror == "x":
        dy = -dy
    elif mirror == "y":
        dx = -dx
    elif mirror is not None:
        raise ValueError("unsupported_mirror")
    return dx, dy


class Transform:
    def __init__(self, symbol):
        at = tail(symbol, "at")
        if len(at) != 3:
            raise ValueError("malformed_instance_position")
        self.origin, self.angle = point(at), normalized_angle(at[2])
        raw_mirror = tail(symbol, "mirror")
        if raw_mirror not in ([], ["x"], ["y"]):
            raise ValueError("unsupported_mirror")
        self.mirror = raw_mirror[0] if raw_mirror else None
        local_transform((0, 0), self.angle, self.mirror)

    def __call__(self, local):
        dx, dy = local_transform(local, self.angle, self.mirror)
        return self.origin[0] + dx, self.origin[1] + dy

    def to_local(self, page):
        dx, dy = (number(v) - o for v, o in zip(page, self.origin))
        ex = local_transform((1, 0), self.angle, self.mirror)
        ey = local_transform((0, 1), self.angle, self.mirror)
        return dx * ex[0] + dy * ex[1], dx * ey[0] + dy * ey[1]


def selected_groups(definition, symbol):
    try:
        unit = int(next(iter(tail(symbol, "unit")), "1"))
        body = int(next(iter(tail(symbol, "body_style")), "1"))
    except (ValueError, TypeError) as error:
        raise ValueError("invalid_unit_or_body_style") from error
    if unit < 1 or body < 1:
        raise ValueError("invalid_unit_or_body_style")
    selected, unknown, inactive = [], [], 0
    for group in children(definition, "symbol"):
        match = re.search(r"_(\d+)_(\d+)$", group[1]) if len(group) >= 2 and isinstance(group[1], str) else None
        if not match:
            unknown.append(group)
        elif int(match[1]) in (0, unit) and int(match[2]) in (0, body):
            selected.append(group)
        else:
            inactive += len(children(group, "pin"))
    return selected, unknown, inactive


def wire_records(root):
    records, skipped = [], []
    for index, wire in enumerate(children(root, "wire"), 1):
        record = {"wire_index": index, "wire_uuid": next(iter(tail(wire, "uuid")), None)}
        pts = children(child(wire, "pts"), "xy")
        try:
            if len(pts) != 2:
                raise ValueError("malformed_geometry")
            a, b = [point(p[1:]) for p in pts]
            record["wire_endpoints_mm"] = [strings(a), strings(b)]
            record["roi_mm"] = roi((a, b))
            if a == b:
                raise ValueError("zero_length")
            axis = 0 if a[1] == b[1] else 1 if a[0] == b[0] else None
            if axis is None:
                raise ValueError("nonorthogonal")
            records.append((a, b, axis, record))
        except ValueError as error:
            skipped.append({**record, "object_kind": "wire", "reason": str(error)})
    return records, skipped


def inspect_wire_pin_overlaps(root):
    definitions = {n[1]: n for n in children(child(root, "lib_symbols"), "symbol")
                   if len(n) >= 2 and isinstance(n[1], str)}
    records, skipped_wires = wire_records(root)
    indexed_wires = defaultdict(list)
    for a, b, axis, record in records:
        lo, hi = sorted((a[axis], b[axis]))
        indexed_wires[axis, a[1 - axis]].append((lo, hi, record))
    candidates, skipped_instances, skipped_pins = [], [], []
    checked_instances = checked_pins = inactive = 0
    directions = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}
    for index, symbol in enumerate(children(root, "symbol"), 1):
        record = instance_record(symbol, index)
        definition = definitions.get(record["lib_id"])
        try:
            transform = Transform(symbol)
            record["roi_mm"] = roi((transform.origin,))
            if definition is None or child(definition, "extends"):
                raise ValueError("missing_or_unresolved_embedded_definition")
            groups, unknown, unused = selected_groups(definition, symbol)
            inactive += unused
            if not groups:
                raise ValueError("no_active_definition_group")
        except ValueError as error:
            skipped_instances.append({**record, "object_kind": "symbol", "reason": str(error)})
            continue
        checked_instances += 1
        for group in unknown:
            for pin in children(group, "pin"):
                skipped_pins.append({**record, "object_kind": "pin", "definition_group": group[1] if len(group) >= 2 and isinstance(group[1], str) else None,
                                     "pin_number": next(iter(tail(pin, "number")), None),
                                     "reason": "unrecognized_definition_group"})
        for group in groups:
            for pin in children(group, "pin"):
                pin_record = {**record, "object_kind": "pin",
                              "pin_number": next(iter(tail(pin, "number")), None),
                              "pin_name": next(iter(tail(pin, "name")), None)}
                try:
                    at, raw_length = tail(pin, "at"), tail(pin, "length")
                    if len(at) != 3 or len(raw_length) != 1:
                        raise ValueError("malformed_pin_geometry")
                    local, angle, length = point(at), normalized_angle(at[2]), number(raw_length[0])
                    endpoint = transform(local)
                    pin_record["pin_endpoint_mm"] = strings(endpoint)
                    pin_record["roi_mm"] = roi((endpoint,))
                    if hidden(pin):
                        raise ValueError("hidden")
                    if len(pin) < 3 or pin[2] != "line":
                        raise ValueError("non_line_shape")
                    if length <= 0:
                        raise ValueError("zero_length" if length == 0 else "negative_length")
                    if angle not in directions:
                        raise ValueError("nonorthogonal_pin")
                    dx, dy = directions[angle]
                    stroke_end = transform((local[0] + dx * length, local[1] + dy * length))
                except ValueError as error:
                    skipped_pins.append({**pin_record, "reason": str(error)})
                    continue
                checked_pins += 1
                axis = 0 if endpoint[1] == stroke_end[1] else 1
                fixed = endpoint[1 - axis]
                lo, hi = sorted((endpoint[axis], stroke_end[axis]))
                for wire_lo, wire_hi, wire_record in indexed_wires.get((axis, fixed), []):
                    overlap_lo, overlap_hi = max(lo, wire_lo), min(hi, wire_hi)
                    if overlap_hi <= overlap_lo:
                        continue
                    overlap = ([(overlap_lo, fixed), (overlap_hi, fixed)] if axis == 0
                               else [(fixed, overlap_lo), (fixed, overlap_hi)])
                    candidates.append({
                        **pin_record, **wire_record,
                        "unit": int(record["unit"]),
                        "body_style": int(next(iter(tail(symbol, "body_style")), "1")),
                        "pin_stroke_end_mm": strings(stroke_end),
                        "overlap_mm": [strings(p) for p in overlap],
                        "overlap_length_mm": format(overlap_hi - overlap_lo, "f"),
                        "roi_mm": roi((endpoint, stroke_end, *map(point, wire_record["wire_endpoints_mm"]))),
                        "repair_hint": "Inspect the local route; connect at the true pin endpoint without retracing its stroke. Preserve branches and verify net endpoints before removing any wire.",
                    })
    skipped_objects = skipped_instances + skipped_pins + skipped_wires
    for record in skipped_objects:
        record.setdefault("roi_mm", None)
        record["review_hint"] = "Inspect this object and its connected neighbors in a native local render; unsupported geometry is unverified. Do not delete or redraw all objects inside the viewing ROI."
    return {
        "candidates": candidates,
        "checked_instances": checked_instances,
        "skipped_instances": len(skipped_instances),
        "skipped_instance_reasons": dict(Counter(r["reason"] for r in skipped_instances)),
        "checked_pins": checked_pins, "skipped_pins": len(skipped_pins),
        "skipped_pin_reasons": dict(Counter(r["reason"] for r in skipped_pins)),
        "inactive_pin_definitions": inactive,
        "checked_wires": len(records), "skipped_wires": len(skipped_wires),
        "skipped_wire_reasons": dict(Counter(r["reason"] for r in skipped_wires)),
        "skipped_objects": skipped_objects,
        "limitations": "Root sheet only; expanded embedded definitions and all orthogonal rotations with mirror x/y. Only visible positive-length straight pin strokes and orthogonal wires. Hidden, zero-length, decorated and unsupported pins are listed, not passed. Endpoint-only contact and perpendicular crossing are not overlaps. No net, text, general body collision or density inference. ROI is a viewing aid, never a replacement boundary.",
    }


def segment_set(polylines):
    result = set()
    for vertices in polylines:
        points = [point(p) for p in vertices]
        result.update(tuple(sorted((a, b))) for a, b in zip(points, points[1:]))
    return result


POWER_SHAPES = {
    "supply_arrow": {
        "lines": [[("-0.762", "1.27"), (0, "2.54")], [(0, "2.54"), ("0.762", "1.27")], [(0, 0), (0, "2.54")]],
        "pin_type": "power_in", "tip": (0, "2.54"),
    },
    "ground_triangle": {
        "lines": [[(0, 0), (0, "-1.27"), ("1.27", "-1.27"), (0, "-2.54"), ("-1.27", "-1.27"), (0, "-1.27")]],
        "pin_type": "power_in", "tip": (0, "-2.54"),
    },
    "pwr_flag": {
        "lines": [[(0, 0), (0, "1.27"), ("-1.016", "1.905"), (0, "2.54"), ("1.016", "1.905"), (0, "1.27")]],
        "pin_type": "power_out", "tip": (0, "2.54"),
    },
}


def recognized_power_shapes(root):
    """Match exact active strokes and origin pin; never guess from a net name."""
    recognized = {}
    for definition in children(child(root, "lib_symbols"), "symbol"):
        if len(definition) < 2 or not isinstance(definition[1], str) or not child(definition, "power") or child(definition, "extends"):
            continue
        try:
            groups, unknown, _ = selected_groups(definition, ["symbol", ["unit", "1"]])
            if unknown:
                continue
            pins = [p for g in groups for p in children(g, "pin")]
            if len(pins) != 1 or pins[0][2] != "line" or hidden(pins[0]):
                continue
            if point(tail(pins[0], "at")) != (0, 0) or list(map(number, tail(pins[0], "length"))) != [0]:
                continue
            graphics = [n for g in groups for n in g[2:] if isinstance(n, list) and n[0] != "pin"]
            if not graphics or any(n[0] != "polyline" for n in graphics):
                continue
            segments = segment_set([[p[1:] for p in children(child(n, "pts"), "xy")] for n in graphics])
            for shape, expected in POWER_SHAPES.items():
                if pins[0][1] == expected["pin_type"] and segments == segment_set(expected["lines"]):
                    recognized[definition[1]] = shape
        except (ValueError, IndexError, TypeError):
            continue
    return recognized


def power_geometry(root, records):
    shapes = recognized_power_shapes(root)
    symbols = children(root, "symbol")
    supported, skipped = [], []
    for record in records:
        common = {k: record[k] for k in ("symbol_index", "uuid", "reference", "lib_id", "at_raw", "mirror")}
        shape = shapes.get(next(iter(record["lib_id"]), ""))
        common["shape"] = shape
        try:
            common["roi_mm"] = roi((point(record["at_raw"]),))
        except ValueError:
            common["roi_mm"] = None
        try:
            symbol = symbols[record["symbol_index"] - 1]
            transform = Transform(symbol)
            common["roi_mm"] = roi((transform.origin,))
            if shape is None:
                raise ValueError("unrecognized_power_geometry")
            if record["unit"] not in ([], ["1"]) or tail(symbol, "body_style") not in ([], ["1"]):
                raise ValueError("unsupported_unit_or_body_style")
            expected = POWER_SHAPES[shape]
            vertices = [transform(point(p)) for line in expected["lines"] for p in line]
            supported.append((record, shape, transform, {**common, "shape": shape, "roi_mm": roi(vertices)}))
        except (ValueError, IndexError) as error:
            skipped.append({**common, "object_kind": "power_symbol", "reason": str(error),
                            "review_hint": "Review the actual power graphic, name and approach wire in a native local render; retain the connection anchor and net while correcting local geometry."})
    return supported, skipped


def inspect_power_orientation(root, records):
    supported, skipped = power_geometry(root, records)
    candidates, checked, excluded = [], 0, []
    for record, shape, transform, common in supported:
        if shape == "pwr_flag":
            excluded.append({**common, "reason": "flag_has_no_supply_ground_direction_rule"})
            continue
        checked += 1
        tip = transform(point(POWER_SHAPES[shape]["tip"]))
        dx, dy = tip[0] - transform.origin[0], tip[1] - transform.origin[1]
        direction = "right" if dx > 0 else "left" if dx < 0 else "down" if dy > 0 else "up"
        if (shape == "supply_arrow" and direction == "down") or (shape == "ground_triangle" and direction == "up"):
            candidates.append({**common, "direction": direction,
                               "repair_hint": "Keep the true connection anchor and net; reposition the whole power symbol with its name into the preferred orientation, then check the approach wire and native render."})
    return {"candidates": candidates, "checked_instances": checked,
            "skipped_objects": skipped, "not_applicable": excluded,
            "limitations": "Only exact embedded standard arrow and triangle-ground geometries, all orthogonal rotations and mirror x/y. Horizontal instances are allowed. Unknown power graphics are listed for native visual review; no text or connectivity judgment."}


def inspect_power_value_placement(root, records):
    supported, skipped = power_geometry(root, records)
    candidates, excluded, checked = [], [], 0
    threshold = Decimal("0.635")
    for record, shape, transform, common in supported:
        if shape != "supply_arrow":
            excluded.append({**common, "object_kind": "power_value", "reason": "not_upward_arrow_name_rule"})
            continue
        tip = transform(point(POWER_SHAPES[shape]["tip"]))
        if tip[0] != transform.origin[0] or tip[1] >= transform.origin[1]:
            excluded.append({**common, "object_kind": "power_value", "reason": "arrow_not_pointing_up"})
            continue
        value = record["fields"].get("Value", {})
        try:
            at = value.get("at_raw", [])
            if value.get("hidden", True) or not value.get("text"):
                raise ValueError("hidden_or_empty_value")
            if len(at) != 3:
                raise ValueError("malformed_value_position")
            value_at = point(at)
            if (transform.angle + number(at[2])) % 180:
                raise ValueError("nonhorizontal_value")
        except ValueError as error:
            skipped.append({**common, "object_kind": "power_value", "reason": str(error),
                            "review_hint": "Review the visible field with its power symbol and nearby wires in the native render; this rule has not established the field placement."})
            continue
        checked += 1
        dx, dy = (v - origin for v, origin in zip(value_at, transform.origin))
        reasons = []
        if dy >= 0:
            reasons.append("value_anchor_not_above_connection_origin")
        if abs(dx) > threshold:
            reasons.append("horizontal_anchor_offset_exceeds_screening_threshold")
        if reasons:
            candidates.append({**common, "value": value["text"], "value_at_raw": at,
                               "value_justify_raw": value.get("justify_raw", []),
                               "offset_mm": {"dx": format(dx, "f"), "dy": format(dy, "f")},
                               "reasons": reasons, "roi_mm": roi((transform.origin, tip, value_at)),
                               "repair_hint": "Place the visible power name above and centered on the upward arrow; move the whole symbol if its surroundings collide. Verify rendered glyphs and alignment."})
    return {"candidates": candidates, "checked_instances": checked,
            "skipped_power_instances": len(records) - checked, "skipped_objects": skipped,
            "not_applicable": excluded,
            "skipped_power_instances_breakdown": {"not_applicable": len(excluded), "unverified": len(skipped)},
            "supported_embedded_lib_ids": sorted(k for k, v in recognized_power_shapes(root).items() if v == "supply_arrow"),
            "horizontal_screening_threshold_mm": str(threshold),
            "limitations": "Exact embedded arrow geometry transformed through orthogonal rotations/mirror x/y; only actually upward arrows with visible horizontal Value. Known other shapes and arrows not pointing up are not_applicable to this name-placement rule; forbidden directions are separately reported by power_orientation. skipped_power_instances includes both not_applicable and unverified objects, with counts in skipped_power_instances_breakdown. Property coordinates are absolute page positions. Anchors only, not font bounds, arrow-tip clearance or centering effects. Threshold is screening tolerance, not a style allowance."}


def inspect_flag_wire_crossings(root, records):
    supported, skipped = power_geometry(root, records)
    wires, skipped_wires = wire_records(root)
    candidates, checked = [], 0
    skipped = [r for r in skipped if r.get("shape") == "pwr_flag" or r["reference"].startswith("#FLG") or any("PWR_FLAG" in s for s in r["lib_id"])]
    for record in skipped_wires:
        record.setdefault("roi_mm", None)
        record["review_hint"] = "Check this unsupported wire against nearby flag graphics in the native render."
    for record, shape, transform, common in supported:
        if shape != "pwr_flag":
            continue
        checked += 1
        center = transform((0, Decimal("1.905")))
        vx = transform((Decimal("1.016"), Decimal("1.905")))
        vy = transform((0, Decimal("2.54")))
        rx, ry = abs(vx[0] - center[0]) + abs(vy[0] - center[0]), abs(vx[1] - center[1]) + abs(vy[1] - center[1])
        for a, b, axis, wire_record in wires:
            fixed_delta = abs(a[1 - axis] - center[1 - axis])
            radius, other_radius = (rx, ry) if axis == 0 else (ry, rx)
            if fixed_delta >= other_radius:
                continue
            half_span = radius * (1 - fixed_delta / other_radius)
            lo, hi = sorted((a[axis], b[axis]))
            if min(hi, center[axis] + half_span) <= max(lo, center[axis] - half_span):
                continue
            candidates.append({**common, **wire_record,
                               "flag_anchor_mm": strings(transform.origin),
                               "diamond_center_mm": strings(center),
                               "roi_mm": roi((a, b, transform.origin, vx, vy)),
                               "repair_hint": "The external wire enters the diamond interior. Move the flag to a clear same-net anchor or change the approach direction; preserve a justified ERC flag and verify net endpoints."})
    return {"candidates": candidates, "checked_instances": checked,
            "skipped_objects": skipped + skipped_wires,
            "limitations": "Exact standard PWR_FLAG diamond only, orthogonal wires/rotations/mirror x/y. Detects positive-length wire passage inside the diamond, not tangent contact or the stem. Does not decide flag necessity, nets, other symbol bodies or text collisions."}


def ground_wire_conflicts(a, b):
    """Screen in standard triangle-ground library coordinates, before transforms."""
    base, tip, half_width = Decimal("-1.27"), Decimal("-2.54"), Decimal("1.27")
    reasons = []
    if a[1] == b[1]:
        lo, hi = sorted((a[0], b[0]))
        y = a[1]
        if tip < y < base:
            half_span = y - tip
            if min(hi, half_span) > max(lo, -half_span):
                reasons.append("wire_enters_ground_triangle")
        if y == base and min(hi, half_width) > max(lo, -half_width):
            reasons.append("wire_overlaps_ground_base")
        if base < y < 0 and lo <= 0 <= hi:
            reasons.append("wire_meets_ground_stem_away_from_anchor")
    else:
        lo, hi = sorted((a[1], b[1]))
        x = a[0]
        if abs(x) < half_width and min(hi, base) > max(lo, tip + abs(x)):
            reasons.append("wire_enters_ground_triangle")
        if x == 0 and min(hi, Decimal(0)) > max(lo, base):
            reasons.append("wire_overlaps_ground_stem")
    return reasons


def inspect_ground_wire_crossings(root, records):
    supported, skipped = power_geometry(root, records)
    wires, skipped_wires = wire_records(root)
    candidates, excluded, checked = [], [], 0
    for record in skipped_wires:
        record.setdefault("roi_mm", None)
        record["review_hint"] = "Inspect this unsupported wire against nearby ground graphics in the native render; its ground clearance is unverified."
    for record, shape, transform, common in supported:
        if shape != "ground_triangle":
            excluded.append({**common, "reason": "not_triangle_ground_rule"})
            continue
        checked += 1
        vertices = [transform(p) for p in (("-1.27", "-1.27"), ("1.27", "-1.27"), (0, "-2.54"))]
        for a, b, axis, wire_record in wires:
            reasons = ground_wire_conflicts(transform.to_local(a), transform.to_local(b))
            if not reasons:
                continue
            candidates.append({
                **common, **wire_record, "reasons": reasons,
                "ground_anchor_mm": strings(transform.origin),
                "triangle_vertices_mm": [strings(p) for p in vertices],
                "roi_mm": roi((a, b, transform.origin, *vertices)),
                "repair_hint": "The external wire enters or crosses the ground graphic. Join electrical branches before the true anchor, or move the whole ground symbol beyond the last junction. Preserve its net, then inspect the name and verify net endpoints; do not remove the return branch to clear this candidate.",
            })
    return {
        "candidates": candidates, "checked_instances": checked,
        "checked_wires": len(wires), "skipped_wires": len(skipped_wires),
        "skipped_objects": skipped + skipped_wires, "not_applicable": excluded,
        "limitations": "Exact embedded standard triangle-ground graphic only, with orthogonal wires/rotations and mirror x/y. Detects positive-length triangle interior passage, base/stem overlap, and a wire meeting the stem away from its endpoints. Normal contact at the true anchor and triangle tangent-only contact are not candidates. Unknown power graphics and unsupported transforms/wires remain unverified. No ground-name glyph, general symbol collision, electrical connectivity or density judgment.",
    }


def hierarchy_gaps(root):
    gaps = []
    for sheet in children(root, "sheet"):
        record = {"object_kind": "sheet", "uuid": next(iter(tail(sheet, "uuid")), None),
                  "properties": {p[1]: p[2] for p in children(sheet, "property") if len(p) >= 3},
                  "reason": "child_sheet_not_traversed", "roi_mm": None,
                  "review_hint": "Run the probe on the referenced child source and inspect its native render; this report only covers the current root sheet."}
        try:
            origin, size = point(tail(sheet, "at")), point(tail(sheet, "size"))
            record["roi_mm"] = roi((origin, tuple(a + b for a, b in zip(origin, size))))
        except ValueError:
            pass
        gaps.append(record)
    return gaps
