#!/usr/bin/env python3
"""Capture fresh KiCad evidence and compare schematic identities and nets.

Only the Python standard library and kicad-cli are required. Snapshots are
immutable evidence directories, not projects to edit. PASS means equivalence
in the reported dimensions, never correct circuit design or visual quality.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from style_probe import child, children, parse_sexpr, tail


SCHEMA = 1
UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
LIMITATIONS = [
    "Equivalence only: no design-intent, analog, timing, thermal, or PCB-layout validation.",
    "Changed identities are reported, not automatically remapped or authorized.",
    "No automatic repair; geometric/source changes require native visual review.",
    "Snapshots detect accidental artifact replacement, not adversarial manifest forgery.",
    "Capture before/after from the same project path and export context; relocated dependencies are not automatically remapped.",
    "Native XML must expose per-unit pin inventory; unsupported exports remain UNVERIFIED.",
    "When labels, power/virtual symbols, hidden pins, or sheet pins exist, all native net names are retained verbatim; generated-name drift can conservatively report DIFFERENT.",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def first(node, key, default=""):
    return next(iter(tail(node, key)), default)


def parse_any(text):
    return parse_sexpr(re.sub(r"^(\s*\()[^\s()]+", r"\1kicad_sch", text, count=1))


def path_key(raw, root_uuid=""):
    parts = [p for p in str(raw).split("/") if p]
    if parts and parts[0] == root_uuid:
        parts.pop(0)
    return "/" + "/".join(parts)


def sorted_nodes(nodes):
    return sorted(nodes, key=canonical)


def semantic_node(node):
    """Discard display-only nodes; retain coordinates of electrical objects."""
    kind = node[0]
    if kind in {"paper", "title_block", "text", "text_box", "polyline", "rectangle",
                "circle", "arc", "image", "generator", "generator_version", "embedded_fonts"}:
        return None
    if kind == "property":
        return node[:3]
    if kind in {"effects", "hide", "fields_autoplaced", "stroke", "color", "diameter"}:
        return None
    # Embedded definitions remain whole: a library edit is not a field-only edit.
    if kind == "lib_symbols":
        return node
    result = [x for x in node if not isinstance(x, list)]
    result.extend(sorted_nodes(v for x in node[1:] if isinstance(x, list)
                               if (v := semantic_node(x)) is not None))
    return result


def inspect_sources(source):
    """Expand sheet instances while reading each dependency once."""
    source = Path(source).resolve()
    raw_files, roots, gaps = {}, {}, []
    records, sheets, electrical_objects, labels = {}, {}, {}, []
    root_uuid = ""

    def load(path):
        path = path.resolve()
        if str(path) not in roots:
            data = path.read_bytes()
            raw_files[str(path)] = data
            roots[str(path)] = parse_sexpr(data.decode("utf-8-sig"))
        return roots[str(path)]

    def walk(path, instance_path, active):
        nonlocal root_uuid
        path = path.resolve()
        if path in active:
            gaps.append({"kind": "hierarchy_cycle", "path": str(path), "instance": instance_path})
            return
        try:
            root = load(path)
        except (OSError, ValueError, UnicodeError) as error:
            gaps.append({"kind": "source_unreadable", "path": str(path), "error": str(error)})
            return
        if not root_uuid:
            root_uuid = first(root, "uuid")
            if not UUID.fullmatch(root_uuid):
                gaps.append({"kind": "invalid_root_uuid", "value": root_uuid})
        elif not UUID.fullmatch(first(root, "uuid")):
            gaps.append({"kind": "invalid_sheet_root_uuid", "file": str(path)})
        full_path = "/" + root_uuid + (instance_path if instance_path != "/" else "")
        definitions = {n[1]: n for n in children(child(root, "lib_symbols"), "symbol")}
        if instance_path in sheets:
            gaps.append({"kind": "duplicate_sheet_instance", "instance": instance_path})
        sheets[instance_path] = {
            "file": str(path), "root_uuid": first(root, "uuid"),
            "electrical_fingerprint": digest(canonical(semantic_node(root)).encode()),
            "children": {first(sheet, "uuid"): {
                "properties": {p[1]: p[2] for p in children(sheet, "property")},
                "pins": sorted_nodes({"uuid": first(pin, "uuid"), "name": pin[1], "type": pin[2]}
                                     for pin in children(sheet, "pin")),
            } for sheet in children(root, "sheet")},
        }
        for index, symbol in enumerate(children(root, "symbol")):
            props = {p[1]: p[2] for p in children(symbol, "property")}
            uuid = first(symbol, "uuid")
            if not UUID.fullmatch(uuid):
                gaps.append({"kind": "invalid_symbol_uuid", "instance": instance_path, "index": index})
                continue
            unit = first(symbol, "unit", "1")
            paths = [p for project in children(child(symbol, "instances"), "project")
                     for p in children(project, "path") if p[1].rstrip("/") == full_path.rstrip("/")]
            bindings = {(first(p, "reference"), first(p, "unit", unit)) for p in paths}
            if len(bindings) != 1:
                gaps.append({"kind": "ambiguous_or_missing_instance_binding", "uuid": uuid,
                             "instance": instance_path, "bindings": sorted(bindings)})
                ref = props.get("Reference", "")
            else:
                ref, bound_unit = next(iter(bindings))
                if bound_unit != unit:
                    gaps.append({"kind": "instance_unit_mismatch", "uuid": uuid, "instance": instance_path})
            key = f"{instance_path.rstrip('/')}/{uuid}/{unit}"
            if key in records:
                gaps.append({"kind": "duplicate_instance", "identity": key})
            lib_id = first(symbol, "lib_id")
            definition = definitions.get(lib_id)
            pins = []
            virtual = ref.startswith("#") or bool(definition and child(definition, "power"))
            if definition is None or child(definition, "extends"):
                gaps.append({"kind": "unresolved_symbol", "identity": key, "lib_id": lib_id})
            else:
                body = first(symbol, "body_style", first(symbol, "convert", "1"))
                for group in children(definition, "symbol"):
                    match = re.search(r"_(\d+)_(\d+)$", group[1])
                    if not match:
                        if children(group, "pin"):
                            gaps.append({"kind": "unknown_pin_group", "identity": key, "group": group[1]})
                        continue
                    if match[1] not in ("0", unit) or match[2] not in ("0", body):
                        continue
                    for pin in children(group, "pin"):
                        pins.append({"number": first(pin, "number"), "name": first(pin, "name"),
                                     "type": pin[1], "shape": pin[2], "hidden": tail(pin, "hide") == ["yes"] or "hide" in pin,
                                     "at": tail(pin, "at"), "length": tail(pin, "length")})
            properties = {k: v for k, v in props.items() if k != "Reference"}
            records[key] = {
                "uuid": uuid, "sheet_path": instance_path, "reference": ref, "unit": unit,
                "lib_id": lib_id, "virtual": virtual, "properties": properties,
                "flags": {k: tail(symbol, k) for k in ("in_bom", "on_board", "dnp", "exclude_from_sim")},
                "pins": sorted_nodes(pins), "at": tail(symbol, "at"), "mirror": tail(symbol, "mirror"),
            }
        for kind in ("wire", "junction", "no_connect", "label", "global_label", "hierarchical_label", "bus", "bus_entry"):
            for n in children(root, kind):
                uuid = first(n, "uuid")
                if not UUID.fullmatch(uuid):
                    gaps.append({"kind": "invalid_electrical_object_uuid", "object": kind,
                                 "instance": instance_path})
                object_key = f"{instance_path.rstrip('/')}/{uuid}"
                if object_key in electrical_objects:
                    gaps.append({"kind": "duplicate_electrical_object", "identity": object_key})
                electrical_objects[object_key] = {"kind": kind, "data": semantic_node(n)}
                if kind.endswith("label"):
                    labels.append({"sheet_path": instance_path, "uuid": uuid, "kind": kind,
                                   "name": n[1], "shape": tail(n, "shape")})
        for sheet in children(root, "sheet"):
            values = {p[1]: p[2] for p in children(sheet, "property")}
            filename = values.get("Sheetfile") or values.get("Sheet file")
            uuid = first(sheet, "uuid")
            if not filename or not UUID.fullmatch(uuid):
                gaps.append({"kind": "unresolved_sheet", "file": str(path), "uuid": uuid})
                continue
            expanded = filename.replace("${KIPRJMOD}", str(source.parent))
            if "${" in expanded:
                gaps.append({"kind": "unresolved_sheet_variable", "filename": filename})
                continue
            child_path = Path(expanded)
            if not child_path.is_absolute():
                child_path = path.parent / child_path
            walk(child_path, instance_path.rstrip("/") + "/" + uuid, active + [path])

    walk(source, "/", [])
    physical_refs = Counter(r["reference"] for r in records.values() if not r["virtual"])
    for ref in physical_refs:
        matches = [r for r in records.values() if r["reference"] == ref]
        if not ref or "?" in ref or len({r["sheet_path"] for r in matches}) > 1 or len({r["unit"] for r in matches}) != len(matches):
            gaps.append({"kind": "ambiguous_physical_reference", "reference": ref})
    return {"root_uuid": root_uuid, "records": records, "sheets": sheets,
            "electrical_objects": electrical_objects, "labels": sorted_nodes(labels),
            "coverage_gaps": gaps}, raw_files


def collect_dependencies(source, cli, version, model, files, extra=()):
    """Capture project settings, referenced symbol libraries, and export context."""
    source, cli = Path(source).resolve(), Path(cli).resolve()
    gaps, roles, environment = [], {name: "schematic" for name in files}, {}
    variables = dict(os.environ)
    variables["KIPRJMOD"] = str(source.parent)
    major = version.split(".")[0]
    standard = cli.parent.parent / "share" / "kicad" / "symbols"
    variables.setdefault(f"KICAD{major}_SYMBOL_DIR", str(standard))
    config = Path(os.environ.get("KICAD_CONFIG_HOME", Path(os.environ.get("APPDATA", Path.home())) / "kicad" / (major + ".0")))

    def add(path, role, required=True):
        path = Path(path).resolve()
        if not path.is_file():
            if required:
                gaps.append({"kind": "missing_dependency", "path": str(path), "role": role})
            return
        files[str(path)] = path.read_bytes()
        roles[str(path)] = role

    def resolve(text, parent):
        def replace(match):
            key = match[1]
            value = variables.get(key)
            if value is not None:
                environment[key] = value
            return value if value is not None else match[0]
        expanded = re.sub(r"\$\{([^}]+)\}", replace, text)
        if "${" in expanded or re.match(r"^[a-z]+://", expanded):
            return None
        path = Path(expanded)
        return path if path.is_absolute() else parent / path

    common = config / "kicad_common.json"
    if common.exists():
        add(common, "erc_context")
        try:
            local = json.loads(files[str(common.resolve())]).get("environment", {}).get("vars", {})
            for key, value in local.items():
                variables.setdefault(key, str(value))
        except (ValueError, AttributeError):
            gaps.append({"kind": "unreadable_environment_settings", "path": str(common)})
    add(config / "eeschema.json", "erc_context", False)
    project = source.with_suffix(".kicad_pro")
    if project.exists():
        add(project, "project")
        try:
            settings = json.loads(files[str(project)])
            def referenced_paths(value):
                if isinstance(value, dict):
                    for v in value.values():
                        yield from referenced_paths(v)
                elif isinstance(value, list):
                    for v in value:
                        yield from referenced_paths(v)
                elif isinstance(value, str) and value.endswith((".kicad_wks", ".kicad_sym")):
                    yield value
            for text in referenced_paths(settings):
                path = resolve(text, source.parent)
                if path:
                    add(path, "project_dependency")
                else:
                    gaps.append({"kind": "unresolved_project_dependency", "path": text})
        except ValueError:
            gaps.append({"kind": "invalid_project_json", "path": str(project)})

    libraries, visited = {}, set()
    def table(path, required=False):
        path = path.resolve()
        if path in visited:
            return
        if not path.exists():
            if required:
                gaps.append({"kind": "missing_dependency", "path": str(path), "role": "nested_library_table"})
            return
        visited.add(path)
        add(path, "library_table")
        try:
            root = parse_any(files[str(path)].decode("utf-8-sig"))
            for lib in children(root, "lib"):
                uri = first(lib, "uri")
                resolved = resolve(uri, path.parent)
                if first(lib, "type") == "Table":
                    if resolved:
                        table(resolved, True)
                    else:
                        gaps.append({"kind": "unresolved_nested_table", "uri": uri})
                else:
                    libraries[first(lib, "name")] = resolved
        except (ValueError, UnicodeError) as error:
            gaps.append({"kind": "invalid_library_table", "path": str(path), "error": str(error)})
    table(config / "sym-lib-table")
    table(source.parent / "sym-lib-table")
    for library in sorted({r["lib_id"].split(":", 1)[0] for r in model["records"].values()}):
        path = libraries.get(library, standard / (library + ".kicad_sym"))
        if path:
            add(path, "symbol_library")
        else:
            gaps.append({"kind": "unresolved_library_uri", "library": library})
    for path in extra:
        add(path, "explicit_dependency")
    return roles, environment, gaps


def capture(source, output, cli, dependencies=(), pdf=False):
    source, output, cli = Path(source).resolve(), Path(output).resolve(), Path(cli).resolve()
    if output.exists():
        raise ValueError("Snapshot output must not already exist; use a new directory.")
    if source == output or source in output.parents:
        raise ValueError("Snapshot path cannot replace a source file.")
    version_result = subprocess.run([str(cli), "--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30)
    if version_result.returncode:
        raise ValueError("kicad-cli --version failed: " + version_result.stderr)
    version = version_result.stdout.strip()
    model, files = inspect_sources(source)
    roles, environment, gaps = collect_dependencies(source, cli, version, model, files, dependencies)
    output.mkdir(parents=True)
    (output / "inputs").mkdir()
    inputs = []
    for index, (name, data) in enumerate(sorted(files.items())):
        rel = f"inputs/{index:04d}{Path(name).suffix}"
        (output / rel).write_bytes(data)
        inputs.append({"original": name, "stored": rel, "sha256": digest(data), "role": roles[name]})
    manifest = {"schema": SCHEMA, "created_utc": datetime.now(timezone.utc).isoformat(),
                "source": str(source), "kicad_cli": str(cli), "kicad_version": version,
                "kicad_cli_sha256": digest(cli.read_bytes()),
                "inputs": inputs, "environment": environment, "artifacts": {},
                "coverage_gaps": model["coverage_gaps"] + gaps,
                "capture_status": "UNVERIFIED", "limitations": LIMITATIONS}
    commands = [
        ("netlist.xml", ["sch", "export", "netlist", "--format", "kicadxml"]),
        ("erc.json", ["sch", "erc", "--format", "json", "--severity-all"]),
    ]
    if pdf:
        commands.append(("preview.pdf", ["sch", "export", "pdf"]))
    for filename, arguments in commands:
        command = [str(cli), *arguments, "--output", str(output / filename), str(source)]
        try:
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=180)
            if run.returncode or not (output / filename).is_file():
                manifest["coverage_gaps"].append({"kind": "export_failed", "artifact": filename,
                                                "returncode": run.returncode, "stderr": run.stderr[-4000:],
                                                "stdout": run.stdout[-4000:]})
            else:
                manifest["artifacts"][filename] = digest((output / filename).read_bytes())
        except (OSError, subprocess.TimeoutExpired) as error:
            manifest["coverage_gaps"].append({"kind": "export_failed", "artifact": filename, "error": str(error)})
    if all(name in manifest["artifacts"] for name in ("netlist.xml", "erc.json")):
        try:
            _, net_gaps = read_netlist(output / "netlist.xml", model)
            _, erc_gaps = read_erc(output / "erc.json", model)
            manifest["coverage_gaps"].extend(net_gaps + erc_gaps)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, ET.ParseError) as error:
            manifest["coverage_gaps"].append({"kind": "unsupported_native_evidence", "error": str(error)})
    for item in inputs:
        path = Path(item["original"])
        if not path.is_file() or digest(path.read_bytes()) != item["sha256"]:
            manifest["coverage_gaps"].append({"kind": "source_changed_during_capture", "path": str(path)})
    # Detect newly appearing settings/dependencies as well as changed known inputs.
    end_model, end_files = inspect_sources(source)
    end_roles, end_environment, end_gaps = collect_dependencies(source, cli, version, end_model, end_files, dependencies)
    if (end_files != files or end_roles != roles or end_environment != environment
            or end_model["coverage_gaps"] != model["coverage_gaps"] or end_gaps != gaps
            or digest(cli.read_bytes()) != manifest["kicad_cli_sha256"]):
        manifest["coverage_gaps"].append({"kind": "capture_context_changed"})
    model_file = "source-model.json"
    (output / model_file).write_text(json.dumps(model, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest["artifacts"][model_file] = digest((output / model_file).read_bytes())
    manifest["capture_status"] = "CAPTURED" if not manifest["coverage_gaps"] else "UNVERIFIED"
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_snapshot(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError("Snapshot manifest must be a JSON object")
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unsupported snapshot schema")
    if not manifest.get("inputs") or manifest.get("capture_status") not in {"CAPTURED", "UNVERIFIED"}:
        raise ValueError("Incomplete snapshot manifest")
    if not any(x.get("role") == "schematic" and x.get("original") == manifest.get("source")
               for x in manifest["inputs"]):
        raise ValueError("Snapshot source is not among frozen inputs")
    gaps = list(manifest.get("coverage_gaps", []))
    for item in manifest.get("inputs", []):
        path = (folder / item["stored"]).resolve()
        if folder not in path.parents or not path.is_file() or digest(path.read_bytes()) != item["sha256"]:
            gaps.append({"kind": "input_integrity_failed", "path": item["stored"]})
    if manifest["capture_status"] != "CAPTURED" and not gaps:
        gaps.append({"kind": "snapshot_not_captured"})
    artifacts = manifest.get("artifacts", {})
    for name in sorted(set(artifacts) | {"netlist.xml", "erc.json", "source-model.json"}):
        path = (folder / name).resolve()
        expected = artifacts.get(name)
        if folder not in path.parents or not expected or not path.is_file() or digest(path.read_bytes()) != expected:
            gaps.append({"kind": "artifact_integrity_failed", "artifact": name})
    if gaps:
        return manifest, None, gaps
    model = json.loads((folder / "source-model.json").read_text(encoding="utf-8"))
    return manifest, model, []


def read_netlist(path, model):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != "export" or root.get("version") not in {"D", "E"}:
        raise ValueError("Unsupported native XML netlist schema")
    by_ref, gaps, components = {}, [], {}
    exported_source = root.findtext("./design/source")
    if not exported_source or Path(exported_source).resolve() != Path(model["sheets"]["/"]["file"]).resolve():
        gaps.append({"kind": "xml_source_mismatch", "source": exported_source})
    exported_sheets = [path_key(s.get("tstamps", ""), model["root_uuid"])
                       for s in root.findall("./design/sheet")]
    if Counter(exported_sheets) != Counter(model["sheets"].keys()):
        gaps.append({"kind": "xml_sheet_inventory_mismatch", "exported": exported_sheets,
                     "source": sorted(model["sheets"])})
    all_source = model["records"]
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        sheet = path_key(comp.find("sheetpath").get("tstamps", "/"), model["root_uuid"]) if comp.find("sheetpath") is not None else "/"
        stamps = sorted((comp.findtext("tstamps") or "").split())
        matches = {key: r for key, r in all_source.items() if not r["virtual"]
                   and r["reference"] == ref and r["sheet_path"] == sheet}
        if not stamps or set(stamps) != {r["uuid"] for r in matches.values()} or ref in by_ref:
            gaps.append({"kind": "xml_identity_mismatch", "reference": ref, "sheet": sheet, "tstamps": stamps})
        physical_id = sheet.rstrip("/") + "/" + "+".join(stamps)
        source_pins = {p["number"] for record in matches.values() for p in record["pins"]}
        xml_pins = {p.get("num") for p in comp.findall("./units/unit/pins/pin")}
        if not xml_pins:
            gaps.append({"kind": "xml_pin_inventory_unavailable", "reference": ref})
        elif source_pins != xml_pins:
            gaps.append({"kind": "xml_pin_inventory_mismatch", "reference": ref,
                         "source_pins": sorted(source_pins), "xml_pins": sorted(xml_pins)})
        by_ref[ref] = physical_id
        components[physical_id] = {"reference": ref, "pins": sorted(xml_pins),
                                  "value": comp.findtext("value") or "", "footprint": comp.findtext("footprint") or "",
                                  "properties": {f.get("name"): f.text or "" for f in comp.findall("./fields/field")},
                                  "exported_properties": {p.get("name"): p.get("value", "") for p in comp.findall("property")}}
    physical_refs = {r["reference"] for r in all_source.values() if not r["virtual"]}
    missing = physical_refs - set(by_ref)
    if missing:
        gaps.append({"kind": "source_components_missing_in_export", "references": sorted(missing)})
    # Scoped, escaped, and variable-expanded names cannot be identified from a
    # generated-looking suffix. Preserve all names when a naming mechanism exists.
    preserve_names = bool(model["labels"]) or any(
        r["virtual"] or any(pin["hidden"] for pin in r["pins"]) for r in all_source.values()
    ) or any(sheet.get("pins") for source_sheet in model["sheets"].values()
             for sheet in source_sheet.get("children", {}).values())
    nets, seen, endpoint_info = [], set(), {}
    for net in root.findall("./nets/net"):
        endpoints = []
        for node in net.findall("node"):
            ref, pin = node.get("ref"), node.get("pin")
            if not pin:
                gaps.append({"kind": "net_endpoint_without_pin", "reference": ref})
                continue
            if ref not in by_ref:
                gaps.append({"kind": "unknown_net_endpoint", "reference": ref, "pin": pin})
                continue
            endpoint = by_ref[ref] + ":" + pin
            if endpoint in seen:
                gaps.append({"kind": "duplicate_net_endpoint", "endpoint": endpoint})
            seen.add(endpoint)
            endpoints.append(endpoint)
            endpoint_info[endpoint] = {"pintype": node.get("pintype", ""), "pinfunction": node.get("pinfunction", "")}
        name = net.get("name", "")
        leaf = name.rsplit("/", 1)[-1]
        automatic = not preserve_names and (leaf.startswith("Net-(") or leaf.startswith("unconnected-("))
        nets.append({"endpoints": sorted(endpoints), "name": None if automatic else name,
                     "class": net.get("class", "")})
    expected = {identity + ":" + pin for identity, comp in components.items() for pin in comp["pins"]}
    if expected != seen:
        gaps.append({"kind": "net_endpoint_inventory_mismatch", "missing": sorted(expected - seen), "extra": sorted(seen - expected)})
    return {"components": components, "nets": sorted_nodes(nets), "endpoint_info": endpoint_info}, gaps


def read_erc(path, model):
    report = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(report, dict) or report.get("$schema") != "https://schemas.kicad.org/erc.v1.json" or not isinstance(report.get("sheets"), list):
        raise ValueError("Unsupported ERC schema: expected erc.v1 sheets")
    results, gaps = [], []
    if report.get("source") != Path(model["sheets"]["/"]["file"]).name:
        gaps.append({"kind": "erc_source_mismatch", "source": report.get("source")})
    seen_sheets = set()
    records = {(r["sheet_path"], r["uuid"]): r for r in model["records"].values()}
    for sheet in report["sheets"]:
        sheet_id = path_key(sheet.get("uuid_path", ""), model["root_uuid"])
        if not sheet.get("uuid_path") or sheet_id not in model["sheets"] or sheet_id in seen_sheets:
            gaps.append({"kind": "erc_sheet_identity_mismatch", "sheet": sheet_id})
        seen_sheets.add(sheet_id)
        for violation in sheet.get("violations", []):
            items = []
            for item in violation.get("items", []):
                uuid = item.get("uuid")
                pin = item.get("pin") or item.get("pin_number")
                if pin is None:
                    match = re.search(r"(?:\bpin\s+|引脚\s*)([^\s\[\],]+)", item.get("description", ""), re.I)
                    if match:
                        pin = match[1]
                if not uuid:
                    gaps.append({"kind": "erc_item_without_identity", "rule": violation.get("type"), "sheet": sheet_id})
                if pin is None and (sheet_id, uuid) in records and "pin" in violation.get("type", ""):
                    gaps.append({"kind": "erc_pin_identity_unresolved", "rule": violation.get("type"),
                                 "sheet": sheet_id, "uuid": uuid,
                                 "description": item.get("description", "")})
                items.append({"uuid": uuid, "pin": pin})
            if not violation.get("type") or not items:
                gaps.append({"kind": "erc_violation_not_identifiable", "sheet": sheet_id})
            results.append({"sheet": sheet_id, "rule": violation.get("type"),
                            "description": violation.get("description", ""),
                            "severity": violation.get("severity"), "items": sorted_nodes(items),
                            "excluded": violation.get("excluded", False)})
    if seen_sheets != set(model["sheets"]):
        gaps.append({"kind": "erc_sheet_inventory_mismatch", "missing": sorted(set(model["sheets"]) - seen_sheets)})
    config = {"ignored_checks": sorted(x.get("key", "") for x in report.get("ignored_checks", [])),
              "included_severities": sorted(report.get("included_severities", [])),
              "kicad_version": report.get("kicad_version")}
    return {"violations": sorted_nodes(results), "config": config}, gaps


def diff_mapping(before, after):
    return {"removed": {k: before[k] for k in sorted(before.keys() - after.keys())},
            "added": {k: after[k] for k in sorted(after.keys() - before.keys())},
            "changed": {k: {"before": before[k], "after": after[k]} for k in sorted(before.keys() & after.keys()) if before[k] != after[k]}}


def dimension(before, after):
    if isinstance(before, dict) and isinstance(after, dict):
        differences = diff_mapping(before, after)
        different = any(differences.values())
    else:
        a, b = Counter(map(canonical, before)), Counter(map(canonical, after))
        differences = {"removed": [json.loads(k) for k, count in (a - b).items() for _ in range(count)],
                       "added": [json.loads(k) for k, count in (b - a).items() for _ in range(count)]}
        different = any(differences.values())
    return {"status": "DIFFERENT" if different else "PASS", "differences": differences}


def compare(before, after):
    before, after = Path(before), Path(after)
    report = {"schema": SCHEMA, "before": str(before.resolve()), "after": str(after.resolve()),
              "status": "UNVERIFIED", "coverage_gaps": [],
              "dimensions": {}, "limitations": LIMITATIONS}
    try:
        am, a, ag = load_snapshot(before)
        bm, b, bg = load_snapshot(after)
        report["coverage_gaps"] = [{"snapshot": "before", **g} for g in ag] + [{"snapshot": "after", **g} for g in bg]
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
        report["coverage_gaps"].append({"kind": "unreadable_or_unsupported_snapshot", "error": str(error)})
        return report
    if ag or bg:
        return report
    if am["source"] != bm["source"]:
        report["coverage_gaps"].append({"kind": "source_path_changed", "before": am["source"], "after": bm["source"]})
    try:
        an, ng1 = read_netlist(before / "netlist.xml", a)
        bn, ng2 = read_netlist(after / "netlist.xml", b)
        ae, eg1 = read_erc(before / "erc.json", a)
        be, eg2 = read_erc(after / "erc.json", b)
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, ET.ParseError) as error:
        report["coverage_gaps"].append({"kind": "unreadable_or_unsupported_native_evidence", "error": str(error)})
        return report
    for label, gaps in [("before", ng1 + eg1), ("after", ng2 + eg2)]:
        report["coverage_gaps"].extend({"snapshot": label, **g} for g in gaps)
    identity_fields = ("uuid", "sheet_path", "reference", "unit", "lib_id", "virtual")
    identities = lambda m: {k: {f: v[f] for f in identity_fields} for k, v in m["records"].items()}
    semantics = lambda m: {k: {f: v[f] for f in ("properties", "flags", "pins")} for k, v in m["records"].items()}
    dims = report["dimensions"]
    dims["identity"] = dimension(identities(a), identities(b))
    sheet_identities = lambda m: {k: {f: v.get(f) for f in ("file", "root_uuid", "children")}
                                 for k, v in m["sheets"].items()}
    dims["hierarchy"] = dimension(sheet_identities(a), sheet_identities(b))
    if a["root_uuid"] != b["root_uuid"]:
        dims["identity"]["status"] = "DIFFERENT"
        dims["identity"]["root_uuid"] = {"before": a["root_uuid"], "after": b["root_uuid"]}
    dims["properties_and_pin_definitions"] = dimension(semantics(a), semantics(b))
    dims["exported_component_properties"] = dimension(an["components"], bn["components"])
    dims["network_endpoint_sets"] = dimension(an["nets"], bn["nets"])
    dims["pin_semantics_and_nc"] = dimension(an["endpoint_info"], bn["endpoint_info"])
    dims["labels_and_scopes"] = dimension(a["labels"], b["labels"])
    dims["erc"] = dimension(ae["violations"], be["violations"])
    dims["erc"]["before_count"] = len(ae["violations"])
    dims["erc"]["after_count"] = len(be["violations"])
    dims["erc"]["meaning"] = "PASS means unchanged violations, not zero violations."
    context = lambda m: {x["original"]: x["sha256"] for x in m["inputs"] if x["role"] != "schematic"}
    contexts = dimension(context(am), context(bm))
    if ae["config"] != be["config"] or am["kicad_version"] != bm["kicad_version"] or am.get("kicad_cli_sha256") != bm.get("kicad_cli_sha256") or contexts["status"] != "PASS" or am["environment"] != bm["environment"]:
        dims["erc"]["status"] = "UNVERIFIED"
        dims["erc"]["context_changes"] = contexts["differences"]
        report["coverage_gaps"].append({"kind": "erc_context_changed", "before_config": ae["config"], "after_config": be["config"]})
    fingerprints = lambda m: {k: v["electrical_fingerprint"] for k, v in m["sheets"].items()}
    source_changes = dimension(fingerprints(a), fingerprints(b))
    object_changes = diff_mapping(a["electrical_objects"], b["electrical_objects"])
    changed_symbols = diff_mapping(a["records"], b["records"])
    kind = "display_only" if source_changes["status"] == "PASS" and contexts["status"] == "PASS" else "electrical_or_identity"
    if report["coverage_gaps"]:
        kind = "unknown"
    report["impact"] = {"category": kind, "changed_electrical_objects": object_changes,
                        "changed_symbols": changed_symbols,
                        "required_checks": ["native local visual review"] if kind == "display_only" else
                        ["whole-project endpoint and identity comparison", "ERC differences", "affected and adjacent native visual review"],
                        "note": "Geometry of electrical objects triggers whole-net checks even when endpoints remain equivalent; screen ROI is not electrical impact scope."}
    report["status"] = "UNVERIFIED" if report["coverage_gaps"] else "DIFFERENT" if any(d["status"] == "DIFFERENT" for d in dims.values()) else "PASS"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="Capture immutable fresh native evidence")
    snap.add_argument("source", type=Path)
    snap.add_argument("--output", required=True, type=Path)
    snap.add_argument("--kicad-cli", required=True, type=Path)
    snap.add_argument("--dependency", action="append", default=[], type=Path)
    snap.add_argument("--pdf", action="store_true", help="Include a fresh native preview.pdf in the snapshot integrity checks")
    diff = commands.add_parser("compare", help="Compare two frozen snapshots")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            result = capture(args.source, args.output, args.kicad_cli, args.dependency, pdf=args.pdf)
            code = 0 if result["capture_status"] == "CAPTURED" else 2
        else:
            if args.output and any(p.resolve() == args.output.resolve() or p.resolve() in args.output.resolve().parents for p in (args.before, args.after)):
                raise ValueError("Comparison output must be outside snapshot directories.")
            result = compare(args.before, args.after)
            code = {"PASS": 0, "DIFFERENT": 1, "UNVERIFIED": 2}[result["status"]]
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.command == "compare" and args.output:
            args.output.write_text(text, encoding="utf-8")
            print(json.dumps({"status": result["status"], "report": str(args.output.resolve()),
                              "dimensions": {k: v["status"] for k, v in result.get("dimensions", {}).items()},
                              "coverage_gaps": len(result["coverage_gaps"])}, ensure_ascii=False))
        else:
            print(text, end="")
        return code
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, ET.ParseError, subprocess.SubprocessError) as error:
        result = {"schema": SCHEMA, "status": "UNVERIFIED", "error": str(error),
                  "coverage_gaps": [{"kind": "unreadable_or_unsupported_evidence", "error": str(error)}],
                  "dimensions": {}, "limitations": LIMITATIONS}
        print(json.dumps(result, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
