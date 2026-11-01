#!/usr/bin/env python3
"""Check optional root-sheet ownership and port geometry; never infer design intent."""

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from style_probe import child, children, parse_sexpr, tail


def point(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Expected a two-number point")
    try:
        result = tuple(Decimal(str(v)) for v in value)
    except InvalidOperation as error:
        raise ValueError("Invalid coordinate") from error
    if not all(v.is_finite() for v in result):
        raise ValueError("Nonfinite coordinate")
    return result


def atom(node, key, default=None):
    return next(iter(tail(node, key)), default)


def validate(contract, root):
    findings, entities, ownership = [], {}, {}
    checked_ports = []

    def issue(code, location, detail, unverified=False):
        findings.append({"status": "UNVERIFIED" if unverified else "DIFFERENT",
                         "code": code, "location": location, "detail": detail})

    def shape(obj, allowed, location):
        if not isinstance(obj, dict):
            issue("invalid_object", location, "Expected an object")
            return False
        extra = sorted(set(obj) - set(allowed))
        if extra:
            issue("unknown_fields", location, extra, True)
        return True

    if not shape(contract, {"version", "scope", "cores", "support_groups", "read_chains",
                            "keep_together", "ports"}, "contract"):
        return {"status": "DIFFERENT", "findings": findings}
    if type(contract.get("version")) is not int or contract["version"] != 1:
        issue("unsupported_version", "version", contract.get("version"), True)
    scope = contract.get("scope", "listed")
    if scope not in ("listed", "sheet"):
        issue("invalid_scope", "scope", scope)
    if not isinstance(contract.get("cores"), list) or not contract["cores"]:
        issue("missing_cores", "cores", "At least one core is required")

    records = []
    definitions = {n[1]: n for n in children(child(root, "lib_symbols"), "symbol")}
    for index, node in enumerate(children(root, "symbol")):
        props = {p[1]: p[2] for p in children(node, "property")}
        ref, lib = props.get("Reference", ""), atom(node, "lib_id", "")
        definition = definitions.get(lib, [])
        power = bool(child(definition, "power")) or lib.startswith("power:") or ref.startswith("#")
        records.append({"index": index, "node": node, "ref": ref,
                        "unit": atom(node, "unit", "1"), "uuid": atom(node, "uuid"),
                        "definition": definition, "power": power})

    def select(selector, location):
        if not shape(selector, {"ref", "unit", "sheet_path"}, location):
            return None
        if selector.get("sheet_path", "/") != "/":
            issue("hierarchy_not_expanded", location, selector, True)
            return None
        ref = selector.get("ref")
        if not isinstance(ref, str) or not ref:
            issue("missing_reference", location, selector)
            return None
        unit = selector.get("unit")
        if unit is not None and (not isinstance(unit, int) or isinstance(unit, bool) or unit < 1):
            issue("invalid_unit", location, unit)
            return None
        matches = [r for r in records if r["ref"] == ref and (unit is None or str(unit) == r["unit"])]
        if len(matches) != 1:
            issue("missing_member" if not matches else "ambiguous_member", location,
                  {"selector": selector, "matches": len(matches)}, bool(matches))
            return None
        if matches[0]["power"]:
            issue("power_not_physical_member", location, selector)
            return None
        return matches[0]

    def listing(name):
        result = contract.get(name, [])
        if not isinstance(result, list):
            issue("invalid_list", name, "Expected a list")
            return []
        return result

    for kind in ("cores", "support_groups"):
        for index, obj in enumerate(listing(kind)):
            loc = f"{kind}[{index}]"
            allowed = {"id", "members", "physical_constraints"}
            if kind == "support_groups":
                allowed |= {"serves", "serves_pins"}
            if not shape(obj, allowed, loc):
                continue
            entity_id = obj.get("id")
            if not isinstance(entity_id, str) or not entity_id or entity_id in entities:
                issue("invalid_or_duplicate_id", loc, entity_id)
                continue
            members = obj.get("members")
            if not isinstance(members, list) or not members:
                issue("empty_members", loc, entity_id)
                continue
            entities[entity_id] = {"kind": kind, "object": obj, "records": []}
            constraints = obj.get("physical_constraints", [])
            if not isinstance(constraints, list) or any(not isinstance(c, str) or not c for c in constraints):
                issue("invalid_constraints", loc, "Use a list of nonempty existing constraint descriptions")
            for j, member in enumerate(members):
                record = select(member, f"{loc}.members[{j}]")
                if record is None:
                    continue
                key = record["index"]
                if key in ownership:
                    issue("duplicate_owner", loc, {"ref": record["ref"], "unit": record["unit"],
                                                  "owners": [ownership[key], entity_id]})
                else:
                    ownership[key] = entity_id
                entities[entity_id]["records"].append(record)

    for entity_id, entity in entities.items():
        if entity["kind"] != "support_groups":
            continue
        obj, loc = entity["object"], f"support_groups.{entity_id}"
        serves = obj.get("serves")
        if not isinstance(serves, str) or entities.get(serves, {}).get("kind") != "cores":
            issue("missing_service_core", loc, serves)
        pins = obj.get("serves_pins", [])
        if not isinstance(pins, list):
            issue("invalid_serves_pins", loc, "Expected a list")
        else:
            for j, pin in enumerate(pins):
                if not isinstance(pin, dict) or not isinstance(pin.get("pin"), str) or not pin["pin"]:
                    issue("invalid_serves_pin", loc, pin)
                    continue
                record = select({k: v for k, v in pin.items() if k != "pin"}, f"{loc}.serves_pins[{j}]")
                if record and ownership.get(record["index"]) != serves:
                    issue("service_pin_wrong_owner", loc, pin)
                if record and not active_pins(record, pin["pin"]):
                    issue("service_pin_not_resolved", loc, pin, True)

    if scope == "sheet":
        for r in records:
            if not r["power"] and r["index"] not in ownership:
                issue("unassigned_unit", "scope", {"ref": r["ref"], "unit": r["unit"], "uuid": r["uuid"]})
        if children(root, "sheet"):
            issue("hierarchy_not_expanded", "scope", "Root-sheet coverage excludes child sheets", True)

    for i, chain in enumerate(listing("read_chains")):
        if not isinstance(chain, list) or len(chain) < 2 or any(not isinstance(x, str) for x in chain):
            issue("invalid_chain", f"read_chains[{i}]", chain)
        else:
            for name in chain:
                if name not in entities:
                    issue("unknown_chain_member", f"read_chains[{i}]", name)

    keep_ids = set()
    for i, group in enumerate(listing("keep_together")):
        loc = f"keep_together[{i}]"
        if not shape(group, {"id", "members", "cross_owner_reason"}, loc):
            continue
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id or group_id in keep_ids:
            issue("invalid_or_duplicate_keep_id", loc, group_id)
        else:
            keep_ids.add(group_id)
        members = group.get("members")
        if not isinstance(members, list) or len(members) < 2:
            issue("invalid_keep_group", loc, "Expected at least two members")
            continue
        owners = set()
        for j, member in enumerate(members):
            record = select(member, f"{loc}.members[{j}]")
            if record is None:
                continue
            owner = ownership.get(record["index"])
            if not owner:
                issue("unowned_keep_member", loc, member)
            else:
                entity = entities[owner]
                effective = entity["object"].get("serves") if entity["kind"] == "support_groups" else owner
                owners.add(str(effective))
        reason = group.get("cross_owner_reason")
        if len(owners) > 1 and not (isinstance(reason, str) and reason.strip()):
            issue("mixed_keep_ownership", loc, sorted(owners))

    labels = [n for kind in ("label", "global_label", "hierarchical_label") for n in children(root, kind)]
    port_ids = set()
    for i, port in enumerate(listing("ports")):
        loc = f"ports[{i}]"
        if not shape(port, {"id", "owner", "anchor", "net", "role", "connection", "direction",
                            "origin_mm", "local_mm", "grid_mm"}, loc):
            continue
        port_id, owner = port.get("id"), port.get("owner")
        if not isinstance(port_id, str) or not port_id or port_id in port_ids:
            issue("invalid_or_duplicate_port_id", loc, port_id)
        else:
            port_ids.add(port_id)
        if not isinstance(owner, str) or owner not in entities:
            issue("unknown_port_owner", loc, owner)
        for key, allowed in [("role", {"input", "output", "bidirectional", "tri_state", "passive"}),
                             ("connection", {"wire", "label"}), ("direction", {"left", "right", "up", "down"})]:
            if not isinstance(port.get(key), str) or port[key] not in allowed:
                issue("invalid_port_" + key, loc, port.get(key))
        if not isinstance(port.get("net"), str) or not port["net"]:
            issue("missing_port_net", loc, port.get("net"))
        anchor, actual, resolved, shape_value = port.get("anchor"), None, None, None
        if isinstance(anchor, dict) and set(anchor) == {"uuid"}:
            matches = [n for n in labels if atom(n, "uuid") == anchor["uuid"]]
            if len(matches) != 1:
                issue("label_anchor_not_unique", loc, anchor, True)
            else:
                resolved = matches[0]
                if port.get("connection") != "label":
                    issue("label_anchor_connection_mismatch", loc, "Label anchors require connection=label")
                actual = point(tail(resolved, "at")[:2])
                if resolved[1] != port.get("net"):
                    issue("label_net_mismatch", loc, {"actual": resolved[1], "declared": port.get("net")})
                shape_value = atom(resolved, "shape")
                if shape_value and shape_value != port.get("role"):
                    issue("label_role_mismatch", loc, {"actual": shape_value, "declared": port.get("role")})
        elif isinstance(anchor, dict) and isinstance(anchor.get("pin"), str) and anchor["pin"]:
            record = select({k: v for k, v in anchor.items() if k != "pin"}, loc + ".anchor")
            if record:
                if ownership.get(record["index"]) != owner:
                    issue("port_anchor_wrong_owner", loc, anchor)
                pins = active_pins(record, anchor["pin"])
                if len(pins) != 1:
                    issue("pin_anchor_not_unique", loc, anchor, True)
                else:
                    try:
                        actual = pin_endpoint(record, pins[0])
                    except (ValueError, ImportError) as error:
                        issue("pin_geometry_unsupported", loc, str(error), True)
        else:
            issue("invalid_anchor", loc, anchor)
        try:
            origin, local = point(port.get("origin_mm")), point(port.get("local_mm"))
            expected = tuple(a + b for a, b in zip(origin, local))
            grid = port.get("grid_mm")
            if grid is not None:
                grid = Decimal(str(grid))
                if not grid.is_finite() or grid <= 0:
                    raise ValueError("grid_mm must be finite and positive")
                if any(v % grid for v in local):
                    issue("declared_grid_mismatch", loc, "Local endpoint is off the explicitly declared grid")
            if actual is not None and actual != expected:
                issue("port_position_mismatch", loc, {"actual_mm": list(map(str, actual)),
                                                      "expected_mm": list(map(str, expected))})
            checked_ports.append({"id": port_id, "actual_mm": list(map(str, actual)) if actual else None,
                                  "net_role_checked": "label_text_and_shape" if shape_value else
                                                      "label_text_only" if resolved else "declaration_only"})
        except (ValueError, InvalidOperation) as error:
            issue("invalid_port_coordinates", loc, str(error))

    status = "DIFFERENT" if any(f["status"] == "DIFFERENT" for f in findings) else "UNVERIFIED" if findings else "PASS"
    return {"status": status, "scope": scope, "assigned_units": len(ownership), "entities": len(entities),
            "ports": checked_ports, "findings": findings,
            "limitations": ["Root-sheet source only; no repeated hierarchy expansion.",
                            "PASS means declared ownership, references and port geometry are consistent, not that intent is correct.",
                            "Reading chains, service relationships, physical constraints and departure directions are declarations, not inferred or geometrically scored.",
                            "Pin-port network/role and wire connectivity are not verified; use native netlist comparison separately.",
                            "keep_together records membership; it does not impose distance, coordinates or automatic regrouping."]}


def active_pins(record, number):
    definition = record["definition"]
    if not definition or child(definition, "extends"):
        return []
    unit = int(record["unit"])
    body = int(atom(record["node"], "body_style", "1"))
    result = []
    for group in children(definition, "symbol"):
        match = re.search(r"_(\d+)_(\d+)$", group[1])
        if match and int(match[1]) in (0, unit) and int(match[2]) in (0, body):
            result.extend(p for p in children(group, "pin") if atom(p, "number") == number)
    return result


def pin_endpoint(record, pin):
    from probe_geometry import Transform
    return Transform(record["node"])(point(tail(pin, "at")[:2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", type=Path)
    parser.add_argument("schematic", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() in {args.contract.resolve(), args.schematic.resolve()}:
        parser.error("Output must not overwrite either input")
    try:
        source = args.schematic.read_bytes()
        document = args.contract.read_bytes()
        report = validate(json.loads(document.decode("utf-8-sig")), parse_sexpr(source.decode("utf-8-sig")))
        report.update({"source": str(args.schematic.resolve()), "source_sha256": hashlib.sha256(source).hexdigest(),
                       "contract_sha256": hashlib.sha256(document).hexdigest()})
    except (OSError, ValueError, TypeError, IndexError, KeyError) as error:
        report = {"status": "UNVERIFIED", "findings": [{"code": "input_error", "detail": str(error)}]}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    raise SystemExit({"PASS": 0, "DIFFERENT": 1, "UNVERIFIED": 2}[report["status"]])


if __name__ == "__main__":
    main()
