#!/usr/bin/env python3
"""Build native KiCad schematics from explicit functional groups and pin anchors.

This is a bounded authoring tool, not a netlist autorouter or an importer for
existing schematics. The JSON recipe owns identity and placement relationships.
"""

import argparse
from collections import Counter
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

from probe_geometry import local_transform, normalized_angle, number, selected_groups
from sch_author_io import Atom, SymbolLibrary, child, children, dumps


ZERO = Decimal(0)
ROLES = {"input", "output", "bidirectional", "tri_state", "passive"}


def keys(obj, allowed, required=(), context="object"):
    if not isinstance(obj, dict):
        raise ValueError(f"{context}: expected an object")
    extra, missing = set(obj) - set(allowed), set(required) - set(obj)
    if extra or missing:
        raise ValueError(f"{context}: unknown={sorted(extra)}, missing={sorted(missing)}")


def xy(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Expected an XY pair")
    return tuple(number(str(v)) for v in value)


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def scalar(value):
    return Atom(format(number(str(value)), "f"))


def n(key, *values):
    return [Atom(key), *values]


def coords(key, position, angle=None):
    values = [scalar(v) for v in position]
    if angle is not None:
        values.append(scalar(angle))
    return n(key, *values)


def replace_node(node, key, value):
    node[:] = [item for item in node if not (isinstance(item, list) and item and item[0] == key)]
    if value is not None:
        node.append(value)


def text_id(value, context):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}: expected a nonempty string")
    return value


def on_segment(p, a, b):
    return ((a[0] == b[0] == p[0] and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))
            or (a[1] == b[1] == p[1] and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])))


def expected_pin_sets(recipe, pin_defs=None):
    expected = recipe.get("expected_nets")
    if expected is None:
        return None
    if not isinstance(expected, list):
        raise ValueError("expected_nets must be a list")
    refs = {s["id"]: s["ref"] for s in recipe["symbols"]}
    wanted, all_pins = [], set()
    for net in expected:
        keys(net, {"name", "pins"}, {"pins"}, "expected net")
        if "name" in net:
            text_id(net["name"], "expected net name")
        if not isinstance(net["pins"], list):
            raise ValueError("Expected net pins must be a list")
        ends = set()
        for pair in net["pins"]:
            if (not isinstance(pair, list) or len(pair) != 2 or
                    not isinstance(pair[0], str) or pair[0] not in refs or
                    isinstance(pair[1], bool) or not isinstance(pair[1], (str, int))):
                raise ValueError("Expected pin needs [known_symbol_id, pin_number]")
            sid, pin = pair[0], str(pair[1])
            if refs[sid].startswith("#") or (pin_defs is not None and pin not in pin_defs[sid]):
                raise ValueError(f"Expected endpoint must be an actual non-power pin: {sid}.{pin}")
            ends.add((refs[sid], pin))
        if not ends or all_pins & ends or len(ends) != len(net["pins"]):
            raise ValueError("Expected pin sets must be nonempty and disjoint")
        all_pins |= ends
        wanted.append((net.get("name"), frozenset(ends)))
    return wanted


class Drawing:
    def __init__(self, recipe, base_dir=None, library_paths=None):
        self.recipe = deepcopy(recipe)
        keys(recipe, {"schema_version", "name", "project_id", "libraries", "paper", "groups",
                      "symbols", "anchors", "wires", "labels", "junctions", "no_connects",
                      "supports", "read_chains", "expected_nets"},
             {"schema_version", "name", "project_id", "symbols"}, "recipe")
        if type(recipe["schema_version"]) is not int or recipe["schema_version"] != 1:
            raise ValueError("Unsupported recipe schema_version")
        self.name = text_id(recipe["name"], "name")
        if self.name in (".", "..") or any(c in self.name for c in '<>:"/\\|?*'):
            raise ValueError("name must be a portable file stem")
        self.namespace = uuid.UUID(text_id(recipe["project_id"], "project_id"))
        self.paper = recipe.get("paper", "A4")
        if self.paper not in {"A5", "A4", "A3", "A2", "A1", "A0", "Letter", "Legal", "Ledger"}:
            raise ValueError("Unsupported paper; paper does not drive placement")
        base_dir = Path(base_dir or ".")
        paths = dict(recipe.get("libraries", {}))
        paths.update(library_paths or {})
        self.library_paths = {k: str((base_dir / v).resolve()) for k, v in paths.items()}
        self.library = SymbolLibrary(self.library_paths)
        self.groups = self._index(recipe.get("groups", []), "group")
        for group in self.groups.values():
            keys(group, {"id", "origin", "parent"}, {"id", "origin"}, "group")
            xy(group["origin"])
        self.symbols = self._index(recipe["symbols"], "symbol")
        self.anchors = recipe.get("anchors", {})
        if not isinstance(self.anchors, dict):
            raise ValueError("anchors must be an object")
        self._active = set()
        for gid in self.groups:
            self.group_origin(gid)
        self.definitions, self.pin_defs = {}, {}
        self.warnings = []
        identities = set()
        for sid, spec in self.symbols.items():
            keys(spec, {"id", "ref", "lib_id", "value", "group", "at", "attach", "rotation",
                        "mirror", "unit", "body_style", "fields", "properties", "in_bom", "on_board", "dnp", "exclude_from_sim"},
                 {"id", "ref", "lib_id"}, f"symbol {sid}")
            if ("at" in spec) == ("attach" in spec):
                raise ValueError(f"{sid}: choose exactly one of at or attach")
            ref = text_id(spec["ref"], "reference")
            unit = spec.get("unit", 1)
            body = spec.get("body_style", 1)
            if isinstance(unit, bool) or not isinstance(unit, int) or unit < 1:
                raise ValueError("unit must be a positive integer")
            if isinstance(body, bool) or not isinstance(body, int) or body < 1:
                raise ValueError("body_style must be a positive integer")
            if (ref, unit) in identities:
                raise ValueError(f"Duplicate reference/unit: {ref}/{unit}")
            identities.add((ref, unit))
            self.group_origin(spec.get("group"))
            local_transform((0, 0), spec.get("rotation", 0), spec.get("mirror"))
            lib_id = text_id(spec["lib_id"], "lib_id")
            if lib_id not in self.definitions:
                self.definitions[lib_id] = self.library.load(lib_id)
            suffixes = [re.search(r"_(\d+)_(\d+)$", str(g[1]))
                        for g in children(self.definitions[lib_id], "symbol")]
            units = {int(m[1]) for m in suffixes if m and int(m[1]) > 0}
            styles = {int(m[2]) for m in suffixes if m and int(m[2]) > 0}
            if unit not in (units or {1}) or body not in (styles or {1}):
                raise ValueError(f"{sid}: absent unit/body style {unit}/{body}")
            active, unknown, _ = selected_groups(self.definitions[lib_id],
                                                ["symbol", ["unit", str(unit)], ["body_style", str(body)]])
            if unknown or not active:
                raise ValueError(f"{sid}: unsupported library unit/body groups")
            pins = {}
            for group in active:
                for pin in children(group, "pin"):
                    num = str(child(pin, "number")[1])
                    if num in pins:
                        raise ValueError(f"{sid}: ambiguous duplicate pin {num}")
                    pins[num] = pin
            self.pin_defs[sid] = pins
            for flag in ("in_bom", "on_board", "dnp", "exclude_from_sim"):
                if flag in spec and not isinstance(spec[flag], bool):
                    raise ValueError(f"{sid}.{flag} must be boolean")
            if "attach" in spec:
                keys(spec["attach"], {"pin", "to", "offset"}, {"pin", "to"}, "attach")
        for sid in self.symbols:
            self.origin(sid)
        for anchor in self.anchors:
            self.point({"anchor": anchor})
        self._validate_relations()
        expected_pin_sets(recipe, self.pin_defs)

    @staticmethod
    def _index(items, context):
        if not isinstance(items, list):
            raise ValueError(f"{context}s must be a list")
        result = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"{context} must be an object")
            ident = text_id(item.get("id"), f"{context} id")
            if ident in result:
                raise ValueError(f"Duplicate {context} id: {ident}")
            result[ident] = item
        return result

    def ident(self, category, ident):
        return str(uuid.uuid5(self.namespace, f"{category}:{ident}"))

    def _guard(self, token, callback):
        if token in self._active:
            raise ValueError(f"Cyclic placement dependency: {token}")
        self._active.add(token)
        try:
            return callback()
        finally:
            self._active.remove(token)

    def group_origin(self, gid):
        if gid is None:
            return ZERO, ZERO
        if gid not in self.groups:
            raise ValueError(f"Unknown group: {gid}")
        group = self.groups[gid]
        return self._guard(("group", gid), lambda: add(xy(group["origin"]), self.group_origin(group.get("parent"))))

    def _same_group(self, point, gid, seen=None):
        seen = set() if seen is None else seen
        if isinstance(point, (list, tuple)):
            return
        if not isinstance(point, dict):
            raise ValueError("Invalid attachment point")
        if "pin" in point:
            pair = point["pin"]
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("pin needs [symbol_id, pin_number]")
            sid = pair[0]
            if sid not in self.symbols or self.symbols[sid].get("group") != gid:
                raise ValueError("Attachments must remain inside one group; use wires between groups")
        if "at" in point and point.get("group", gid) != gid:
            raise ValueError("Attachment coordinate belongs to another group")
        if "anchor" in point:
            name = point["anchor"]
            if name in seen or name not in self.anchors:
                raise ValueError("Unknown or cyclic attachment anchor")
            self._same_group(self.anchors[name], gid, seen | {name})
        for axis in ("x", "y"):
            if axis in point:
                self._same_group(point[axis], gid, seen)

    def origin(self, sid):
        if sid not in self.symbols:
            raise ValueError(f"Unknown symbol: {sid}")
        spec = self.symbols[sid]
        def resolve():
            if "at" in spec:
                return add(self.group_origin(spec.get("group")), xy(spec["at"]))
            attach = spec["attach"]
            self._same_group(attach["to"], spec.get("group"))
            target = self.point(attach["to"], spec.get("group"))
            delta = self.pin_offset(sid, str(attach["pin"]))
            return sub(add(target, xy(attach.get("offset", [0, 0]))), delta)
        return self._guard(("symbol", sid), resolve)

    def pin_offset(self, sid, pin):
        if sid not in self.symbols or str(pin) not in self.pin_defs[sid]:
            raise ValueError(f"Unknown pin: {sid}.{pin}")
        spec = self.symbols[sid]
        at = child(self.pin_defs[sid][str(pin)], "at")
        return local_transform(at[1:3], spec.get("rotation", 0), spec.get("mirror"))

    def pin(self, sid, pin):
        return add(self.origin(sid), self.pin_offset(sid, str(pin)))

    def point(self, value, default_group=None):
        if isinstance(value, (list, tuple)):
            return add(xy(value), self.group_origin(default_group))
        keys(value, {"at", "group", "pin", "anchor", "x", "y", "offset"}, context="point")
        modes = int("at" in value) + int("pin" in value) + int("anchor" in value) + int("x" in value or "y" in value)
        if modes != 1 or ("group" in value and "at" not in value):
            raise ValueError("Point needs exactly one of at, pin, anchor, or x/y")
        if "at" in value:
            result = add(xy(value["at"]), self.group_origin(value.get("group", default_group)))
        elif "pin" in value:
            pair = value["pin"]
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("pin needs [symbol_id, pin_number]")
            result = self.pin(pair[0], str(pair[1]))
        elif "anchor" in value:
            name = value["anchor"]
            if name not in self.anchors:
                raise ValueError(f"Unknown anchor: {name}")
            result = self._guard(("anchor", name), lambda: self.point(self.anchors[name], default_group))
        else:
            if "x" not in value or "y" not in value:
                raise ValueError("Point projection requires both x and y")
            result = self.point(value["x"], default_group)[0], self.point(value["y"], default_group)[1]
        return add(result, xy(value.get("offset", [0, 0])))

    def _validate_relations(self):
        for support in self.recipe.get("supports", []):
            keys(support, {"id", "members", "serves", "role"}, {"id", "members", "serves", "role"}, "support")
            if not support["members"] or not support["serves"]:
                raise ValueError("Support needs members and service pins")
            text_id(support["role"], "support role")
            for sid in support["members"]:
                if sid not in self.symbols:
                    raise ValueError(f"Unknown support member: {sid}")
            for endpoint in support["serves"]:
                keys(endpoint, {"pin"}, {"pin"}, "service endpoint")
                self.point(endpoint)
        for chain in self.recipe.get("read_chains", []):
            if not isinstance(chain, list) or len(chain) < 2 or any(s not in self.symbols for s in chain):
                raise ValueError("read_chains must list at least two known symbol ids")
        for anchor in self.recipe.get("no_connects", []):
            keys(anchor, {"pin"}, {"pin"}, "no_connect")
            self.point(anchor)

    def _body_bounds(self, sid):
        spec = self.symbols[sid]
        active, _, _ = selected_groups(self.definitions[spec["lib_id"]],
                                       ["symbol", ["unit", str(spec.get("unit", 1))],
                                        ["body_style", str(spec.get("body_style", 1))]])
        points = []
        for group in active:
            for graphic in group[2:]:
                if not isinstance(graphic, list) or not graphic:
                    continue
                kind = str(graphic[0])
                if kind == "rectangle":
                    points.extend([child(graphic, "start")[1:3], child(graphic, "end")[1:3]])
                elif kind in ("polyline", "bezier"):
                    points.extend(p[1:3] for p in children(child(graphic, "pts", []), "xy"))
                elif kind == "circle":
                    center = xy(child(graphic, "center")[1:3])
                    radius = number(child(graphic, "radius")[1])
                    points.extend([(center[0]-radius, center[1]-radius), (center[0]+radius, center[1]+radius)])
                elif kind == "arc":
                    points.extend(child(graphic, k)[1:3] for k in ("start", "mid", "end"))
        if not points:
            raise ValueError(f"{sid}: field layout needs explicit offset for this symbol geometry")
        transformed = [local_transform(p, spec.get("rotation", 0), spec.get("mirror")) for p in points]
        xs, ys = zip(*transformed)
        return min(xs), min(ys), max(xs), max(ys)

    def _field(self, sid, original, overrides):
        spec = self.symbols[sid]
        prop = deepcopy(original)
        name = str(prop[1])
        cfg = overrides.get(name, {})
        keys(cfg, {"offset", "align", "orientation", "visible", "size", "value", "layout"}, context=f"field {name}")
        if "offset" in cfg and "layout" in cfg:
            raise ValueError("Choose field offset or layout")
        if name == "Reference":
            value = spec["ref"]
        elif name == "Value":
            value = spec.get("value", str(prop[2]))
        else:
            value = str(prop[2])
        value = spec.get("properties", {}).get(name, value)
        if name == "Reference" and ("value" in cfg or "Reference" in spec.get("properties", {})):
            raise ValueError("Reference is controlled by ref")
        prop[2] = str(cfg.get("value", value))
        angle = normalized_angle(str(spec.get("rotation", 0)))
        mirror = spec.get("mirror")
        lib_at = child(prop, "at", [None, "0", "0", "0"])
        offset = local_transform(lib_at[1:3], angle, mirror)
        align, orientation = cfg.get("align", "center"), cfg.get("orientation", "horizontal")
        if "layout" in cfg:
            layout = cfg["layout"]
            left, top, right, bottom = self._body_bounds(sid)
            cx, cy = (left+right)/2, (top+bottom)/2
            if layout == "inside":
                if spec["lib_id"] != "Device:R" or name != "Value":
                    raise ValueError("inside is limited to the standard Device:R Value; use explicit offsets otherwise")
                offset, align = (cx, cy), "center"
                orientation = "vertical" if bottom-top > right-left else "horizontal"
            elif layout in ("left", "right"):
                offset = (left-Decimal("1.27") if layout == "left" else right+Decimal("1.27"),
                          cy + Decimal("-1.27" if name == "Reference" else "1.27"))
                align = "right" if layout == "left" else "left"
            elif layout in ("above", "below"):
                offset = (cx, top-Decimal("3.81" if name == "Reference" else "1.27") if layout == "above"
                          else bottom+Decimal("1.27" if name == "Reference" else "3.81"))
                align = "center"
            else:
                raise ValueError(f"Unknown field layout: {layout}")
        if "offset" in cfg:
            offset = xy(cfg["offset"])
        if align not in {"left", "right", "center"} or orientation not in {"horizontal", "vertical"}:
            raise ValueError("Invalid field alignment or orientation")
        size = number(str(cfg.get("size", "1.27")))
        if size <= 0:
            raise ValueError("Field size must be positive")
        default_visible = name in {"Reference", "Value"}
        if spec["ref"].startswith("#") and (name == "Reference" or spec["lib_id"] == "power:PWR_FLAG"):
            default_visible = False
        visible = cfg.get("visible", default_visible)
        if not isinstance(visible, bool):
            raise ValueError("Field visible must be boolean")
        raw_align = align
        if (angle in (90, 180)) != (mirror == "y"):
            raw_align = {"left": "right", "right": "left", "center": "center"}[align]
        if orientation == "vertical" and align != "center":
            raise ValueError("Vertical fields currently require centered alignment")
        effects = n("effects", n("font", coords("size", (size, size))))
        if raw_align != "center":
            effects.append(n("justify", Atom(raw_align)))
        replace_node(prop, "effects", effects)
        replace_node(prop, "hide", None if visible else n("hide", Atom("yes")))
        # Property positions are page anchors; only their text angle/alignment is transformed.
        raw_angle = (angle % 180 + (90 if orientation == "vertical" else 0)) % 180
        replace_node(prop, "at", coords("at", add(self.origin(sid), offset), raw_angle))
        return prop

    def _symbol(self, sid):
        spec = self.symbols[sid]
        definition = self.definitions[spec["lib_id"]]
        overrides = spec.get("fields", {})
        if not isinstance(overrides, dict) or not isinstance(spec.get("properties", {}), dict):
            raise ValueError("fields and properties must be objects")
        # Private library fields are library metadata, not instance properties.
        props = {str(p[1]): deepcopy(p) for p in children(definition, "property")
                 if not (isinstance(p[1], Atom) and p[1] == "private")}
        for name in sorted({"Reference", "Value", "Footprint", "Datasheet", *overrides, *spec.get("properties", {})}):
            props.setdefault(name, n("property", name, "", coords("at", (0, 0), 0)))
        instance = n("symbol", n("lib_id", spec["lib_id"]),
                     coords("at", self.origin(sid), normalized_angle(str(spec.get("rotation", 0)))),
                     n("unit", scalar(spec.get("unit", 1))),
                     n("in_bom", Atom("yes" if spec.get("in_bom", not spec["ref"].startswith("#")) else "no")),
                     n("on_board", Atom("yes" if spec.get("on_board", not spec["ref"].startswith("#")) else "no")),
                     n("dnp", Atom("yes" if spec.get("dnp", False) else "no")),
                     n("exclude_from_sim", Atom("yes" if spec.get("exclude_from_sim", False) else "no")),
                     n("uuid", self.ident("symbol", sid)))
        if spec.get("mirror"):
            instance.append(n("mirror", Atom(spec["mirror"])))
        if spec.get("body_style", 1) != 1:
            instance.append(n("body_style", scalar(spec["body_style"])))
        instance.extend(self._field(sid, prop, overrides) for prop in props.values())
        instance.extend(n("pin", str(pin), n("uuid", self.ident("pin", sid+":"+str(pin)))) for pin in self.pin_defs[sid])
        instance.append(n("instances", n("project", self.name,
                          n("path", "/"+str(self.namespace), n("reference", spec["ref"]),
                            n("unit", scalar(spec.get("unit", 1)))))))
        return instance

    def _labels(self):
        result = []
        for lid, label in self._index(self.recipe.get("labels", []), "label").items():
            keys(label, {"id", "name", "at", "kind", "role", "side"}, {"id", "name", "at"}, "label")
            kind, side = label.get("kind", "global"), label.get("side", "right")
            if kind not in {"local", "global"} or side not in {"left", "right"}:
                raise ValueError("Labels support local/global and left/right")
            if kind == "global" and label.get("role") not in ROLES:
                raise ValueError("Global labels require an explicit electrical role")
            if kind == "local" and "role" in label:
                raise ValueError("Local labels have no shape/role")
            at = self.point(label["at"])
            obj = n("global_label" if kind == "global" else "label", text_id(label["name"], "label name"))
            if kind == "global":
                obj.append(n("shape", Atom(label["role"])))
            obj.extend([coords("at", at, 0 if side == "left" else 180) if kind == "global"
                        else coords("at", at, 180 if side == "left" else 0),
                        n("effects", n("font", coords("size", ("1.27", "1.27"))),
                          n("justify", Atom("right" if side == "left" else "left"))),
                        n("uuid", self.ident("label", lid))])
            if kind == "global":
                obj.append(n("property", "Intersheetrefs", "${INTERSHEET_REFS}", coords("at", at, 0),
                             n("hide", Atom("yes")), n("effects", n("font", coords("size", ("1.27", "1.27"))))))
            result.append(obj)
        return result

    def _wire_geometry(self):
        segments = []
        for wid, wire in self._index(self.recipe.get("wires", []), "wire").items():
            keys(wire, {"id", "points", "route"}, {"id", "points"}, "wire")
            points = [self.point(p) for p in wire["points"]]
            if len(points) < 2:
                raise ValueError("Wire needs at least two points")
            route = wire.get("route", "straight")
            if route not in {"straight", "hv", "vh"}:
                raise ValueError("Unknown route")
            index = 0
            for start, end in zip(points, points[1:]):
                path = [start, end]
                if start[0] != end[0] and start[1] != end[1]:
                    if route == "straight":
                        raise ValueError(f"{wid}: diagonal route; specify hv/vh or explicit orthogonal points")
                    path.insert(1, (end[0], start[1]) if route == "hv" else (start[0], end[1]))
                for a, b in zip(path, path[1:]):
                    if a != b:
                        segments.append((a, b, wid+":"+str(index)))
                    index += 1
        endpoints = {p for a, b, _ in segments for p in (a, b)}
        pins = Counter(self.pin(sid, pin) for sid in self.symbols for pin in self.pin_defs[sid])
        label_points = {self.point(label["at"]) for label in self.recipe.get("labels", [])}
        explicit = {self.point(p) for p in self.recipe.get("junctions", [])}
        cut_points = endpoints | set(pins) | label_points | explicit
        result, seen = [], set()
        for a, b, token in segments:
            cuts = sorted(p for p in cut_points if on_segment(p, a, b))
            if a > b:
                cuts.reverse()
            for index, (p, q) in enumerate(zip(cuts, cuts[1:])):
                edge = tuple(sorted((p, q)))
                if edge not in seen:
                    result.append((p, q, token+":"+str(index)))
                    seen.add(edge)
        degree = Counter(p for a, b, _ in result for p in (a, b))
        junctions = {p for p in set(degree) | set(pins) if degree[p] + pins[p] >= 3}
        for p in explicit:
            if degree[p] + pins[p] < 3:
                raise ValueError("Explicit junction must join at least three conductor branches/pins")
        junctions |= explicit
        nc = {self.point(p) for p in self.recipe.get("no_connects", [])}
        if any(pins[p] != 1 or degree[p] or p in label_points for p in nc):
            raise ValueError("No-connect marker is on a connected or ambiguous pin")
        return result, junctions

    def render(self):
        root = n("kicad_sch", n("version", Atom("20250114")), n("generator", "sch_author"),
                 n("uuid", str(self.namespace)), n("paper", self.paper),
                 n("lib_symbols", *deepcopy(list(self.definitions.values()))))
        instances = [self._symbol(sid) for sid in self.symbols]
        packages = {}
        for instance in instances:
            properties = {str(p[1]): str(p[2]) for p in children(instance, "property")}
            ref = properties["Reference"]
            signature = (str(child(instance, "lib_id")[1]),
                         tuple(properties.get(k, "") for k in ("Value", "Footprint", "Datasheet", "Description")),
                         tuple(str(child(instance, k)[1]) for k in ("in_bom", "on_board", "dnp", "exclude_from_sim")))
            if ref in packages and packages[ref] != signature:
                raise ValueError(f"{ref}: inconsistent package properties across units")
            packages[ref] = signature
        root.extend(instances)
        root.extend(self._labels())
        segments, junctions = self._wire_geometry()
        for a, b, token in segments:
            root.append(n("wire", n("pts", coords("xy", a), coords("xy", b)),
                          n("stroke", n("width", Atom("0")), n("type", Atom("default"))),
                          n("uuid", self.ident("wire", token))))
        # Junction identity follows incident objects, not a page coordinate.
        for p in sorted(junctions):
            incident = sorted(["wire:"+token for a, b, token in segments if p in (a, b)] +
                              ["pin:"+sid+":"+str(pin) for sid in self.symbols for pin in self.pin_defs[sid]
                               if self.pin(sid, pin) == p])
            root.append(n("junction", coords("at", p), n("diameter", Atom("0")),
                          n("color", *[Atom("0")]*4), n("uuid", self.ident("junction", "|".join(incident)))))
        for index, endpoint in enumerate(self.recipe.get("no_connects", [])):
            root.append(n("no_connect", coords("at", self.point(endpoint)), n("uuid", self.ident("nc", str(index)))))
        root.extend([n("sheet_instances", n("path", "/", n("page", "1"))), n("embedded_fonts", Atom("no"))])
        return dumps(root) + "\n"

    def report(self):
        self.render()
        return {"schema_version": 1, "project_id": str(self.namespace),
                "groups": {gid: {"origin_mm": list(map(str, self.group_origin(gid))),
                                  "members": [sid for sid, spec in self.symbols.items() if spec.get("group") == gid]}
                           for gid in self.groups},
                "symbols": {sid: {"ref": spec["ref"], "uuid": self.ident("symbol", sid),
                                    "origin_mm": list(map(str, self.origin(sid))),
                                    "pins_mm": {pin: list(map(str, self.pin(sid, pin))) for pin in self.pin_defs[sid]}}
                            for sid, spec in self.symbols.items()},
                "supports": self.recipe.get("supports", []), "read_chains": self.recipe.get("read_chains", []),
                "limitations": ["No automatic collision, text-fit, electrical-design or compactness acceptance.",
                                "Manhattan routing follows explicit waypoints; it does not avoid obstacles.",
                                "Only authored single-sheet recipes; no existing schematic import or live editor control."]}


def move_group(recipe, group_id, delta):
    result = deepcopy(recipe)
    matches = [g for g in result.get("groups", []) if g.get("id") == group_id]
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching group")
    matches[0]["origin"] = list(map(str, add(xy(matches[0]["origin"]), xy(delta))))
    return result


def verify_expected(recipe, netlist):
    wanted = expected_pin_sets(recipe)
    if wanted is None:
        return {"status": "UNVERIFIED", "reason": "No expected_nets supplied; native export alone is not intent validation"}
    tree = ET.parse(netlist).getroot()
    actual = []
    for net in tree.findall("./nets/net"):
        ends = frozenset((node.get("ref"), node.get("pin")) for node in net.findall("node") if not node.get("ref", "").startswith("#"))
        if ends:
            actual.append((net.get("name"), ends))
    missing = [{"name": name, "pins": sorted(ends)} for name, ends in wanted
               if not any(ends == got and (name is None or name == actual_name) for actual_name, got in actual)]
    extra = [{"name": name, "pins": sorted(ends)} for name, ends in actual
             if not any(ends == want and (expected_name is None or name == expected_name) for expected_name, want in wanted)]
    return {"status": "PASS" if not missing and not extra else "DIFFERENT", "missing": missing, "extra": extra,
            "scope": "Exact named/unnamed native non-power pin sets against supplied intent; not circuit-design approval"}


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_recipe(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def library_args(values):
    result = {}
    for value in values or []:
        name, separator, path = value.partition("=")
        if not separator or not name or not path or name in result:
            raise ValueError("Use --library Nickname=path, once per nickname")
        result[name] = str(Path(path).resolve())
    return result


def native_export(cli, source, output):
    commands = [(["sch", "export", "netlist", "--format", "kicadxml", "--output", str(output/"netlist.xml"), str(source)], "netlist"),
                (["sch", "erc", "--format", "json", "--output", str(output/"erc.json"), str(source)], "erc"),
                (["sch", "export", "pdf", "--output", str(output/"preview.pdf"), str(source)], "pdf")]
    for command, name in commands:
        result = subprocess.run([str(cli), *command], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        (output/(name+"-export.log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"KiCad {name} export failed ({result.returncode}); see saved log")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Create a new output bundle from a recipe")
    build.add_argument("recipe", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--library", action="append", default=[])
    build.add_argument("--kicad-cli", type=Path)
    build.add_argument("--png", action="store_true")
    move = commands.add_parser("move-group", help="Write a new recipe with one translated group")
    move.add_argument("recipe", type=Path)
    move.add_argument("--group", required=True)
    move.add_argument("--delta", nargs=2, required=True)
    move.add_argument("--output", type=Path, required=True)
    move.add_argument("--library", action="append", default=[])
    inspect = commands.add_parser("symbol", help="Inspect actual library pins before authoring")
    inspect.add_argument("lib_id")
    inspect.add_argument("--library", action="append", required=True)
    inspect.add_argument("--unit", type=int, default=1)
    args = parser.parse_args(argv)
    bundle_output, result = None, None
    try:
        if args.command == "symbol":
            definition = SymbolLibrary(library_args(args.library)).load(args.lib_id)
            groups, unknown, _ = selected_groups(definition, ["symbol", ["unit", str(args.unit)]])
            if unknown or not groups:
                raise ValueError("Unsupported or absent symbol unit")
            info = {"lib_id": args.lib_id, "unit": args.unit,
                    "pins": [{"number": str(child(p, "number")[1]), "name": str(child(p, "name")[1]),
                              "type": str(p[1]), "library_at": list(map(str, child(p, "at")[1:])),
                              "page_offset_at_zero": list(map(str, local_transform(child(p, "at")[1:3], 0)))}
                             for g in groups for p in children(g, "pin")]}
            print(json.dumps(info, indent=2))
            return 0
        recipe = read_recipe(args.recipe)
        if args.output.exists():
            raise ValueError("Output already exists; use a new path to preserve prior evidence")
        if args.command == "move-group":
            result = move_group(recipe, args.group, args.delta)
            # Relative libraries keep their meaning when the recipe moves directories.
            result["libraries"] = {name: str((args.recipe.parent/path).resolve()) for name, path in result.get("libraries", {}).items()}
            result["libraries"].update(library_args(args.library))
            Drawing(result, args.output.parent).render()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_json(args.output, result)
            print(str(args.output.resolve()))
            return 0
        if args.png and not args.kicad_cli:
            raise ValueError("--png requires --kicad-cli")
        drawing = Drawing(recipe, args.recipe.parent, library_args(args.library))
        rendered = drawing.render()
        args.output.mkdir(parents=True)
        bundle_output = args.output
        source = args.output/(drawing.name+".kicad_sch")
        source.write_text(rendered, encoding="utf-8")
        frozen_recipe = deepcopy(recipe)
        frozen_recipe["libraries"] = drawing.library_paths
        write_json(args.output/"recipe.json", frozen_recipe)
        write_json(args.output/"drawing.json", drawing.report())
        result = {"status": "BUILT", "source": str(source.resolve()),
                  "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "recipe_sha256": hashlib.sha256(args.recipe.read_bytes()).hexdigest(),
                  "native": "NOT_RUN", "visual": "UNVERIFIED"}
        if args.kicad_cli:
            native_export(args.kicad_cli, source, args.output)
            result["native"] = "EXPORTED"
            result["electrical_intent"] = verify_expected(recipe, args.output/"netlist.xml")
            if args.png:
                import pymupdf
                with pymupdf.open(args.output/"preview.pdf") as document:
                    for index, page in enumerate(document, 1):
                        page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(args.output/f"page-{index:03}.png")
            result["artifacts"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in args.output.iterdir() if p.is_file()}
        write_json(args.output/"result.json", result)
        print(json.dumps(result, indent=2))
        return 1 if result.get("electrical_intent", {}).get("status") == "DIFFERENT" else 0
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.TimeoutExpired, ImportError) as error:
        failure = {**(result or {}), "status": "ERROR", "error": str(error)}
        if bundle_output is not None:
            try:
                write_json(bundle_output/"result.json", failure)
            except OSError as manifest_error:
                failure["manifest_error"] = str(manifest_error)
        print(json.dumps(failure), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
