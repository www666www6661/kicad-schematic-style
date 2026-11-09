"""Stdlib regressions; KICAD_CLI enables native exports of TEMP-only fixtures."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import schematic_check as check


def uid(name):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "schematic-check-tests/" + name))


ROOT = uid("root")
SYMBOL = uid("resistor")
SHEET = uid("sheet")
CHILD = uid("child")


def definition(name="Check:R"):
    return f'''(symbol "{name}" (pin_names (offset 0)) (in_bom yes) (on_board yes)
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1 1))))
      (property "Value" "R" (at 0 0 0) (effects (font (size 1 1))))
      (symbol "R_0_1" (rectangle (start -1 -2) (end 1 2)
        (stroke (width 0) (type default)) (fill (type none))))
      (symbol "R_1_1"
        (pin passive line (at 0 3.81 270) (length 1.81)
          (name "~" (effects (font (size 1 1)))) (number "1" (effects (font (size 1 1)))))
        (pin passive line (at 0 -3.81 90) (length 1.81)
          (name "~" (effects (font (size 1 1)))) (number "2" (effects (font (size 1 1)))))
      ))'''


def schematic(value="1k", field_x=50, connect=True, nc=False, child=False, root=ROOT,
              instance_path=None, reference="R1", symbol=SYMBOL):
    binding = instance_path or "/" + root
    extra = ""
    if connect:
        extra += f'''(wire (pts (xy 50 46.19) (xy 50 53.81))
          (stroke (width 0) (type default)) (uuid "{uid('wire')}"))'''
    if nc:
        extra += f'(no_connect (at 50 46.19) (uuid "{uid("nc")}"))'
    if child:
        extra += sheet_node()
    return f'''(kicad_sch (version 20250114) (generator "eeschema")
      (uuid "{root}") (paper "A4") (lib_symbols {definition()})
      (symbol (lib_id "Check:R") (at 50 50 0) (unit 1) (in_bom yes) (on_board yes)
        (dnp no) (uuid "{symbol}")
        (property "Reference" "{reference}" (at {field_x} 50 0) (effects (font (size 1 1))))
        (property "Value" "{value}" (at {field_x} 52 0) (effects (font (size 1 1))))
        (pin "1" (uuid "{uid('pin1')}")) (pin "2" (uuid "{uid('pin2')}"))
        (instances (project "main" (path "{binding}" (reference "{reference}") (unit 1)))))
      {extra})'''


def pinless_definition(name="Check:MountingHole"):
    return f'''(symbol "{name}" (in_bom no) (on_board yes)
      (property "Reference" "H" (at 0 0 0) (effects (font (size 1 1))))
      (property "Value" "MountingHole" (at 0 2 0) (effects (font (size 1 1))))
      (symbol "MountingHole_0_1" (circle (center 0 0) (radius 1.27)
        (stroke (width 0.5) (type default)) (fill (type none)))))'''


def pinless_schematic(value="MountingHole", field_x=50):
    return f'''(kicad_sch (version 20250114) (generator "eeschema")
      (uuid "{ROOT}") (paper "A4") (lib_symbols {pinless_definition()})
      (symbol (lib_id "Check:MountingHole") (at 50 50 0) (unit 1) (in_bom no) (on_board yes)
        (dnp no) (uuid "{SYMBOL}")
        (property "Reference" "H1" (at {field_x} 50 0) (effects (font (size 1 1))))
        (property "Value" "{value}" (at {field_x} 52 0) (effects (font (size 1 1))))
        (instances (project "main" (path "/{ROOT}" (reference "H1") (unit 1))))))'''


def sheet_node(sheet=SHEET, filename="child.kicad_sch"):
    return f'''(sheet (at 80 40) (size 20 20) (uuid "{sheet}")
      (property "Sheetname" "Child" (at 80 39 0) (effects (font (size 1 1))))
      (property "Sheetfile" "{filename}" (at 80 61 0) (effects (font (size 1 1)))))'''


def empty_schematic(root=CHILD, extra=""):
    return f'(kicad_sch (version 20250114) (generator "eeschema") (uuid "{root}") (paper "A4") (lib_symbols) {extra})'


def labeled_pair(names, swapped=False):
    second = schematic(reference="R2", symbol=uid("resistor2"))
    second = second[second.index('      (symbol (lib_id'):].rstrip()[:-1]
    second = second.replace('(at 50 ', '(at 100 ').replace('(xy 50 ', '(xy 100 ')
    for name in ("wire", "pin1", "pin2"):
        second = second.replace(uid(name), uid(name + "-second"))
    labels = "".join(
        f'(label {json.dumps(name)} (at {x} 46.19 0) (effects (font (size 1 1))) (uuid "{uid("label-" + str(i))}"))'
        for i, (name, x) in enumerate(zip(names, (100, 50) if swapped else (50, 100))))
    return schematic().rstrip()[:-1] + second + labels + ")"


def fixture_netlist(model, split=False, automatic_name="Net-(R1-Pad1)"):
    root = ET.Element("export", version="E")
    design = ET.SubElement(root, "design")
    ET.SubElement(design, "source").text = model["sheets"]["/"]["file"]
    ET.SubElement(design, "tool").text = "Eeschema 10.0.5"
    for path in model["sheets"]:
        ET.SubElement(design, "sheet", tstamps=path)
    comps = ET.SubElement(root, "components")
    nets = ET.SubElement(root, "nets")
    records = list(model["records"].values())
    for record in records:
        comp = ET.SubElement(comps, "comp", ref=record["reference"])
        ET.SubElement(comp, "value").text = record["properties"]["Value"]
        ET.SubElement(comp, "sheetpath", tstamps=record["sheet_path"])
        ET.SubElement(comp, "tstamps").text = record["uuid"]
        pins = ET.SubElement(ET.SubElement(ET.SubElement(comp, "units"), "unit", name="A"), "pins")
        numbers = sorted({p["number"] for p in record["pins"]})
        for number in numbers:
            ET.SubElement(pins, "pin", num=number)
        groups = [[number] for number in numbers] if split else [numbers] if numbers else []
        for index, group in enumerate(groups):
            net = ET.SubElement(nets, "net", code=str(index + 1), name=automatic_name, **{"class": "Default"})
            for number in group:
                ET.SubElement(net, "node", ref=record["reference"], pin=number, pintype="passive", pinfunction="~")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def fixture_erc(model, violations=None):
    return {"$schema": "https://schemas.kicad.org/erc.v1.json", "kicad_version": "10.0.5",
            "source": Path(model["sheets"]["/"]["file"]).name,
            "ignored_checks": [], "included_severities": ["error", "warning", "exclusion"],
            "sheets": [{"uuid_path": "/" + model["root_uuid"] + (p if p != "/" else ""),
                        "violations": violations or []} for p in model["sheets"]]}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kicad-schematic-check-unit-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "main.kicad_sch"
        self.source.write_text(schematic(), encoding="utf-8")
        self.model, self.files = check.inspect_sources(self.source)
        self.assertEqual([], self.model["coverage_gaps"])

    def snapshot(self, name, model=None, xml=None, erc=None):
        model = deepcopy(model or self.model)
        folder = self.base / name
        folder.mkdir()
        (folder / "inputs").mkdir()
        data = self.source.read_bytes()
        (folder / "inputs/source.kicad_sch").write_bytes(data)
        (folder / "netlist.xml").write_bytes(xml or fixture_netlist(model))
        (folder / "erc.json").write_text(json.dumps(erc if erc is not None else fixture_erc(model)), encoding="utf-8")
        (folder / "source-model.json").write_text(json.dumps(model), encoding="utf-8")
        manifest = {"schema": 1, "source": str(self.source), "capture_status": "CAPTURED",
                    "kicad_version": "10.0.5", "kicad_cli_sha256": "test-binary",
                    "environment": {}, "coverage_gaps": [],
                    "inputs": [{"original": str(self.source), "stored": "inputs/source.kicad_sch",
                                "role": "schematic", "sha256": check.digest(data)}],
                    "artifacts": {n: check.digest((folder / n).read_bytes())
                                  for n in ("netlist.xml", "erc.json", "source-model.json")}}
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return folder

    def test_unchanged_with_existing_erc_violation_passes(self):
        violation = {"type": "pin_not_connected", "description": "Pin not connected", "severity": "error",
                     "items": [{"uuid": SYMBOL, "description": "Symbol R1 Pin 1 [~]"}]}
        before = self.snapshot("before", erc=fixture_erc(self.model, [violation]))
        report = check.compare(before, before)
        self.assertEqual("PASS", report["status"])
        self.assertEqual(1, report["dimensions"]["erc"]["before_count"])

    def test_display_field_move_is_equivalent(self):
        before = self.snapshot("before")
        self.source.write_text(schematic(field_x=65), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        after = self.snapshot("after", model=model)
        report = check.compare(before, after)
        self.assertEqual("PASS", report["status"])
        self.assertEqual("display_only", report["impact"]["category"])

    def test_pin_partition_split_is_detected_even_with_same_name(self):
        before = self.snapshot("before")
        after = self.snapshot("after", xml=fixture_netlist(self.model, split=True))
        self.assertEqual("DIFFERENT", check.compare(before, after)["dimensions"]["network_endpoint_sets"]["status"])

    def test_auto_net_names_and_codes_are_not_identity(self):
        before = self.snapshot("before")
        after = self.snapshot("after", xml=fixture_netlist(self.model, automatic_name="Net-(R1-Pad2)"))
        self.assertEqual("PASS", check.compare(before, after)["status"])

    def test_explicit_label_looking_like_auto_name_remains_significant(self):
        model = deepcopy(self.model)
        model["labels"] = [{"name": "Net-(R1-Pad1)", "sheet_path": "/", "uuid": uid("label"), "kind": "label", "shape": []}]
        before = self.snapshot("before", model=model)
        after = self.snapshot("after", model=model, xml=fixture_netlist(model, automatic_name="Net-(R1-Pad2)"))
        self.assertEqual("DIFFERENT", check.compare(before, after)["status"])

    def test_scoped_escaped_and_variable_labels_preserve_native_names(self):
        for index, raw_name in enumerate(("foo/Net-(A)", "/foo/Net-(A)", r"foo\/Net-(A)", "${NET_A}")):
            with self.subTest(raw_name=raw_name):
                model = deepcopy(self.model)
                model["labels"] = [{"name": raw_name, "sheet_path": "/", "uuid": uid("label"), "kind": "label", "shape": []}]
                xml = fixture_netlist(model, automatic_name="/foo/Net-(A)")
                before = self.snapshot(f"label-before-{index}", model=model, xml=xml)
                after = self.snapshot(f"label-after-{index}", model=model,
                                      xml=fixture_netlist(model, automatic_name="/bar/Net-(B)"))
                report = check.compare(before, after)
                self.assertEqual("DIFFERENT", report["dimensions"]["network_endpoint_sets"]["status"])
                self.assertEqual("PASS", report["dimensions"]["labels_and_scopes"]["status"])

    def test_power_hidden_and_sheet_pins_preserve_native_names(self):
        for mechanism in ("power", "hidden-pin", "sheet-pin"):
            with self.subTest(mechanism=mechanism):
                model = deepcopy(self.model)
                if mechanism == "power":
                    power = deepcopy(next(iter(model["records"].values())))
                    power.update(uuid=uid("power"), reference="#PWR01", virtual=True)
                    power["properties"]["Value"] = "${POWER_NET}"
                    model["records"]["/" + uid("power") + "/1"] = power
                elif mechanism == "hidden-pin":
                    pin = next(iter(model["records"].values()))["pins"][0]
                    pin.update(hidden=True, type="power_in", name="${POWER_NET}")
                else:
                    model["sheets"]["/"]["children"] = {SHEET: {"pins": [{"name": "${SHEET_NET}"}]}}
                before = self.snapshot(mechanism + "-before", model=model,
                                       xml=fixture_netlist(self.model, automatic_name="/Net-(A)"))
                after = self.snapshot(mechanism + "-after", model=model,
                                      xml=fixture_netlist(self.model, automatic_name="/Net-(B)"))
                self.assertEqual("DIFFERENT", check.compare(before, after)["dimensions"]["network_endpoint_sets"]["status"])

    def test_value_and_identity_changes_are_detected(self):
        before = self.snapshot("before")
        self.source.write_text(schematic(value="2k", reference="R2"), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        after = self.snapshot("after", model=model)
        report = check.compare(before, after)
        for name in ("identity", "properties_and_pin_definitions", "exported_component_properties"):
            self.assertEqual("DIFFERENT", report["dimensions"][name]["status"])

    def test_empty_child_root_identity_change_is_detected(self):
        model = deepcopy(self.model)
        model["sheets"]["/" + SHEET] = {"file": str(self.base / "child.kicad_sch"),
                                         "root_uuid": CHILD, "children": {}, "electrical_fingerprint": "same"}
        before = self.snapshot("before", model=model)
        model["sheets"]["/" + SHEET]["root_uuid"] = uid("changed-child")
        after = self.snapshot("after", model=model)
        self.assertEqual("DIFFERENT", check.compare(before, after)["dimensions"]["hierarchy"]["status"])

    def test_repeated_sheet_instances_remain_distinct(self):
        other = uid("other-sheet")
        self.source.write_text(empty_schematic(ROOT, sheet_node() + sheet_node(other)), encoding="utf-8")
        text = schematic(root=CHILD, instance_path=f"/{ROOT}/{SHEET}")
        text = text.replace('(reference "R1") (unit 1)))))',
                            f'(reference "R1") (unit 1)) (path "/{ROOT}/{other}" (reference "R2") (unit 1)))))')
        (self.base / "child.kicad_sch").write_text(text, encoding="utf-8")
        model, files = check.inspect_sources(self.source)
        self.assertEqual([], model["coverage_gaps"])
        self.assertEqual(2, len(model["records"]))
        self.assertEqual({"R1", "R2"}, {r["reference"] for r in model["records"].values()})
        self.assertEqual(2, len(files))

    def test_duplicate_sheet_and_cycle_are_unverified(self):
        self.source.write_text(empty_schematic(ROOT, sheet_node() + sheet_node()), encoding="utf-8")
        (self.base / "child.kicad_sch").write_text(empty_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        self.assertIn("duplicate_sheet_instance", {g["kind"] for g in model["coverage_gaps"]})
        self.source.write_text(empty_schematic(ROOT, sheet_node(filename="main.kicad_sch")), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        self.assertIn("hierarchy_cycle", {g["kind"] for g in model["coverage_gaps"]})

    def test_tampered_artifact_or_input_is_rejected(self):
        for name in ("netlist.xml", "inputs/source.kicad_sch"):
            with self.subTest(name=name):
                folder = self.snapshot(name.replace("/", "-").replace(".", "-"))
                (folder / name).write_text("tampered", encoding="utf-8")
                self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_missing_per_unit_inventory_is_unverified(self):
        xml = ET.fromstring(fixture_netlist(self.model))
        comp = xml.find("./components/comp")
        comp.remove(comp.find("units"))
        folder = self.snapshot("missing-units", xml=ET.tostring(xml))
        report = check.compare(folder, folder)
        self.assertEqual("UNVERIFIED", report["status"])
        self.assertIn("xml_pin_inventory_unavailable", {g["kind"] for g in report["coverage_gaps"]})

    def test_pinless_component_is_retained_and_compared(self):
        self.source.write_text(pinless_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        self.assertEqual([], model["coverage_gaps"])
        self.assertTrue(next(iter(model["records"].values()))["pinless"])
        before = self.snapshot("pinless-before", model=model)
        exported, gaps = check.read_netlist(before / "netlist.xml", model)
        self.assertEqual([], gaps)
        self.assertEqual(1, len(exported["components"]))
        self.assertEqual("H1", next(iter(exported["components"].values()))["reference"])
        self.assertEqual([], exported["nets"])
        self.source.write_text(pinless_schematic(field_x=65), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        after = self.snapshot("pinless-moved", model=model)
        self.assertEqual("PASS", check.compare(before, after)["status"])
        self.source.write_text(pinless_schematic(value="M3"), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        after = self.snapshot("pinless-changed", model=model)
        report = check.compare(before, after)
        self.assertEqual("DIFFERENT", report["status"])
        for name in ("properties_and_pin_definitions", "exported_component_properties"):
            self.assertEqual("DIFFERENT", report["dimensions"][name]["status"])

    def test_empty_xml_inventory_for_pinned_source_is_unverified(self):
        xml = ET.fromstring(fixture_netlist(self.model))
        xml.find("./components/comp/units/unit/pins").clear()
        xml.find("nets").clear()
        folder = self.snapshot("empty-pinned-inventory", xml=ET.tostring(xml))
        report = check.compare(folder, folder)
        self.assertEqual("UNVERIFIED", report["status"])
        self.assertIn("xml_pin_inventory_unavailable", {g["kind"] for g in report["coverage_gaps"]})

    def test_pinless_to_pinned_source_change_is_detected(self):
        self.source.write_text(pinless_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        before = self.snapshot("pinless-before-pins", model=model)
        text = pinless_schematic().replace(pinless_definition(),
                                          definition().replace('"Check:R"', '"Check:MountingHole"'))
        text = text.replace('(dnp no)', '(dnp no) ' + ' '.join(
            f'(pin "{number}" (uuid "{uid("added-pin-" + number)}"))' for number in ("1", "2")))
        self.source.write_text(text, encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        after = self.snapshot("pinless-after-pins", model=model)
        report = check.compare(before, after)
        self.assertEqual("DIFFERENT", report["status"])
        self.assertEqual("PASS", report["dimensions"]["identity"]["status"])
        for name in ("properties_and_pin_definitions", "exported_component_properties", "network_endpoint_sets"):
            self.assertEqual("DIFFERENT", report["dimensions"][name]["status"])

    def test_pinless_source_still_requires_complete_xml_inventory(self):
        self.source.write_text(pinless_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        for omission in ("units", "unit", "pins", "extra-unit"):
            with self.subTest(omission=omission):
                xml = ET.fromstring(fixture_netlist(model))
                comp = xml.find("./components/comp")
                if omission == "extra-unit":
                    ET.SubElement(comp.find("units"), "unit", name="B")
                else:
                    parent = {"units": comp, "unit": comp.find("units"),
                              "pins": comp.find("./units/unit")}[omission]
                    parent.remove(parent.find(omission))
                folder = self.snapshot("pinless-missing-" + omission, model=model, xml=ET.tostring(xml))
                report = check.compare(folder, folder)
                self.assertEqual("UNVERIFIED", report["status"])
                self.assertIn("xml_pin_inventory_unavailable", {g["kind"] for g in report["coverage_gaps"]})

    def test_pinless_source_proof_is_required(self):
        pin = f'(pin "1" (uuid "{uid("unexpected-instance-pin")}"))'
        pinned_definition = definition().replace('"Check:R"', '"Check:MountingHole"')
        candidates = {
            "instance-pin": pinless_schematic().replace('(dnp no)', '(dnp no) ' + pin),
            "library-pin": pinless_schematic().replace(pinless_definition(), pinned_definition),
            "invalid-unit": pinless_schematic().replace(pinless_definition(), pinned_definition).replace('(unit 1)', '(unit 2)'),
            "invalid-body": pinless_schematic().replace('MountingHole_0_1', 'MountingHole_0_0')
                .replace('(dnp no)', '(dnp no) (body_style 2)'),
            "unknown-group": pinless_schematic().replace('MountingHole_0_1', 'Unknown'),
            "missing-library": pinless_schematic().replace(pinless_definition(), ''),
            "inherited-library": pinless_schematic().replace('(symbol "Check:MountingHole"',
                                                             '(symbol "Check:MountingHole" (extends "Base")'),
            "direct-library-pin": pinless_schematic().replace('(symbol "Check:MountingHole"',
                '(symbol "Check:MountingHole" (pin passive line (at 0 0 0) (length 1) (name "~") (number "1"))'),
        }
        for case, source in candidates.items():
            with self.subTest(case=case):
                self.source.write_text(source, encoding="utf-8")
                model, _ = check.inspect_sources(self.source)
                self.assertFalse(next(iter(model["records"].values()))["pinless"])
                xml = ET.fromstring(fixture_netlist(model))
                xml.find("./components/comp/units/unit/pins").clear()
                xml.find("nets").clear()
                folder = self.snapshot("pinless-proof-" + case, model=model, xml=ET.tostring(xml))
                report = check.compare(folder, folder)
                self.assertEqual("UNVERIFIED", report["status"])
                self.assertIn("xml_pin_inventory_unavailable", {g["kind"] for g in report["coverage_gaps"]})

    def test_pinless_legacy_model_without_proof_is_unverified(self):
        self.source.write_text(pinless_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        next(iter(model["records"].values())).pop("pinless")
        folder = self.snapshot("pinless-legacy", model=model)
        report = check.compare(folder, folder)
        self.assertEqual("UNVERIFIED", report["status"])
        self.assertIn("xml_pin_inventory_unavailable", {g["kind"] for g in report["coverage_gaps"]})

    def test_pinless_component_with_net_endpoint_is_unverified(self):
        self.source.write_text(pinless_schematic(), encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        xml = ET.fromstring(fixture_netlist(model))
        net = ET.SubElement(xml.find("nets"), "net", code="1", name="unexpected")
        ET.SubElement(net, "node", ref="H1", pin="1")
        folder = self.snapshot("pinless-endpoint", model=model, xml=ET.tostring(xml))
        report = check.compare(folder, folder)
        self.assertEqual("UNVERIFIED", report["status"])
        self.assertIn("net_endpoint_inventory_mismatch", {g["kind"] for g in report["coverage_gaps"]})

    def test_duplicate_endpoint_is_unverified(self):
        xml = ET.fromstring(fixture_netlist(self.model))
        net = xml.find("./nets/net")
        net.append(deepcopy(net.find("node")))
        folder = self.snapshot("duplicate-endpoint", xml=ET.tostring(xml))
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_erc_same_count_but_changed_pin_is_different(self):
        violation = {"type": "pin_not_connected", "severity": "error", "items": [{"uuid": SYMBOL, "pin": "1"}]}
        before = self.snapshot("before", erc=fixture_erc(self.model, [violation]))
        violation["items"][0]["pin"] = "2"
        after = self.snapshot("after", erc=fixture_erc(self.model, [violation]))
        self.assertEqual("DIFFERENT", check.compare(before, after)["dimensions"]["erc"]["status"])

    def test_foreign_native_source_is_unverified(self):
        xml = ET.fromstring(fixture_netlist(self.model))
        xml.find("./design/source").text = str(self.base / "foreign.kicad_sch")
        folder = self.snapshot("foreign", xml=ET.tostring(xml))
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_erc_missing_sheet_is_unverified(self):
        erc = fixture_erc(self.model)
        erc["sheets"] = []
        folder = self.snapshot("missing-sheet", erc=erc)
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_relocation_is_unverified(self):
        before = self.snapshot("before")
        after = self.snapshot("after")
        path = after / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["source"] = str(self.base / "relocated.kicad_sch")
        manifest["inputs"][0]["original"] = manifest["source"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("UNVERIFIED", check.compare(before, after)["status"])

    def test_malformed_erc_cli_is_structured_unverified(self):
        folder = self.snapshot("malformed", erc=[])
        report_path = self.base / "malformed-report.json"
        out = io.StringIO()
        with redirect_stdout(out):
            code = check.main(["compare", str(folder), str(folder), "--output", str(report_path)])
        self.assertEqual(2, code)
        self.assertEqual("UNVERIFIED", json.loads(out.getvalue())["status"])
        self.assertEqual("UNVERIFIED", json.loads(report_path.read_text())["status"])

    def test_changed_erc_context_is_unverified_even_if_violations_match(self):
        before = self.snapshot("before")
        after = self.snapshot("after")
        path = after / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["environment"] = {"KICAD10_SYMBOL_DIR": "different"}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        report = check.compare(before, after)
        self.assertEqual("UNVERIFIED", report["status"])
        self.assertEqual("UNVERIFIED", report["dimensions"]["erc"]["status"])

    def test_unverified_capture_cannot_pass_with_empty_gap_list(self):
        folder = self.snapshot("unverified")
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["capture_status"] = "UNVERIFIED"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_snapshot_path_escape_is_rejected(self):
        folder = self.snapshot("escaped")
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["inputs"][0]["stored"] = "../main.kicad_sch"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_optional_artifact_hash_and_path_escape_are_checked(self):
        folder = self.snapshot("optional-artifact")
        pdf = folder / "preview.pdf"
        pdf.write_bytes(b"%PDF-synthetic-integrity-fixture")
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["artifacts"]["preview.pdf"] = check.digest(pdf.read_bytes())
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("PASS", check.compare(folder, folder)["status"])
        pdf.write_bytes(b"modified")
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])
        del manifest["artifacts"]["preview.pdf"]
        outside = self.base / "outside.pdf"
        outside.write_bytes(b"%PDF-outside-fixture")
        manifest["artifacts"]["../outside.pdf"] = check.digest(outside.read_bytes())
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual("UNVERIFIED", check.compare(folder, folder)["status"])

    def test_hidden_no_and_alternate_body_pin_inventory(self):
        text = schematic().replace('(at 0 3.81 270) (length 1.81)', '(at 0 3.81 270) (length 1.81) (hide no)')
        self.source.write_text(text, encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        self.assertFalse(next(iter(model["records"].values()))["pins"][0]["hidden"])
        text = text.replace('"R_1_1"', '"R_1_2"').replace('(unit 1) (in_bom yes)', '(unit 1) (convert 2) (in_bom yes)')
        self.source.write_text(text, encoding="utf-8")
        model, _ = check.inspect_sources(self.source)
        self.assertEqual(2, len(next(iter(model["records"].values()))["pins"]))

    def test_capture_detects_source_change_between_native_exports(self):
        cli = self.base / "fake-cli"
        cli.write_bytes(b"synthetic test CLI")
        def run(command, **kwargs):
            result = check.subprocess.CompletedProcess(command, 0, "10.0.5", "")
            if "--version" in command:
                return result
            target = Path(command[command.index("--output") + 1])
            if target.suffix == ".xml":
                target.write_bytes(fixture_netlist(self.model))
                self.source.write_text(schematic(value="changed-during-export"), encoding="utf-8")
            else:
                target.write_text(json.dumps(fixture_erc(self.model)), encoding="utf-8")
            return result
        with patch.object(check.subprocess, "run", side_effect=run), patch.object(
                check, "collect_dependencies", side_effect=lambda source, cli, version, model, files, extra:
                ({name: "schematic" for name in files}, {}, [])):
            result = check.capture(self.source, self.base / "captured", cli)
        self.assertEqual("UNVERIFIED", result["capture_status"])
        self.assertIn("source_changed_during_capture", {g["kind"] for g in result["coverage_gaps"]})

    def test_snapshot_refuses_existing_output_without_running_cli(self):
        with patch.object(check.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "must not already exist"):
                check.capture(self.source, self.base, Path("unused-cli"))
            run.assert_not_called()


@unittest.skipUnless(os.environ.get("KICAD_CLI"), "Set KICAD_CLI to enable native TEMP-only smoke tests")
class NativeTests(unittest.TestCase):
    def test_native_pinless_component_is_captured_and_retained(self):
        with tempfile.TemporaryDirectory(prefix="kicad-schematic-check-pinless-") as temp:
            base = Path(temp)
            source = base / "main.kicad_sch"
            source.write_text(pinless_schematic(), encoding="utf-8")
            (base / "Check.kicad_sym").write_text(
                '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor") '
                + pinless_definition("MountingHole") + ')', encoding="utf-8")
            (base / "sym-lib-table").write_text(
                '(sym_lib_table (version 7) (lib (name "Check") (type "KiCad") (uri "${KIPRJMOD}/Check.kicad_sym") (options "") (descr "")))', encoding="utf-8")
            snapshot = base / "snapshot"
            result = check.capture(source, snapshot, Path(os.environ["KICAD_CLI"]))
            self.assertEqual("CAPTURED", result["capture_status"], result["coverage_gaps"])
            xml = ET.parse(snapshot / "netlist.xml")
            self.assertEqual("H1", xml.find("./components/comp").get("ref"))
            self.assertIsNotNone(xml.find("./components/comp/units/unit/pins"))
            self.assertEqual([], xml.findall("./components/comp/units/unit/pins/pin"))
            report = check.compare(snapshot, snapshot)
            self.assertEqual("PASS", report["status"], report)

    def test_native_explicit_net_names_follow_swapped_label_endpoints(self):
        with tempfile.TemporaryDirectory(prefix="kicad-schematic-check-names-") as temp:
            base = Path(temp)
            source = base / "main.kicad_sch"
            (base / "Check.kicad_sym").write_text(
                '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor") ' + definition("R") + ')', encoding="utf-8")
            (base / "sym-lib-table").write_text(
                '(sym_lib_table (version 7) (lib (name "Check") (type "KiCad") (uri "${KIPRJMOD}/Check.kicad_sym") (options "") (descr "")))', encoding="utf-8")
            (base / "main.kicad_pro").write_text(json.dumps({"text_variables": {"NET_A": "Net-(A)", "NET_B": "Net-(B)"}}), encoding="utf-8")
            for case, names in (("slash", ("foo/Net-(A)", "bar/Net-(B)")),
                                ("variables", ("${NET_A}", "${NET_B}"))):
                with self.subTest(case=case):
                    for name, swapped in (("before", False), ("after", True)):
                        source.write_text(labeled_pair(names, swapped), encoding="utf-8")
                        result = check.capture(source, base / (case + "-" + name), Path(os.environ["KICAD_CLI"]))
                        self.assertEqual("CAPTURED", result["capture_status"], result["coverage_gaps"])
                    report = check.compare(base / (case + "-before"), base / (case + "-after"))
                    self.assertEqual("DIFFERENT", report["status"], report)
                    self.assertEqual("DIFFERENT", report["dimensions"]["network_endpoint_sets"]["status"])
                    self.assertEqual("PASS", report["dimensions"]["labels_and_scopes"]["status"])
                    self.assertEqual("PASS", report["dimensions"]["identity"]["status"])

    def test_native_display_value_connectivity_nc_and_hierarchy(self):
        with tempfile.TemporaryDirectory(prefix="kicad-schematic-check-native-") as temp:
            base = Path(temp)
            source = base / "main.kicad_sch"
            (base / "Check.kicad_sym").write_text(
                '(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor") ' + definition("R") + ')', encoding="utf-8")
            (base / "sym-lib-table").write_text(
                '(sym_lib_table (version 7) (lib (name "Check") (type "KiCad") (uri "${KIPRJMOD}/Check.kicad_sym") (options "") (descr "")))', encoding="utf-8")
            (base / "child.kicad_sch").write_text(empty_schematic(), encoding="utf-8")
            def capture(name, **options):
                source.write_text(schematic(child=True, **options), encoding="utf-8")
                if name == "before":
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        code = check.main(["snapshot", str(source), "--output", str(base / name),
                                           "--kicad-cli", os.environ["KICAD_CLI"], "--pdf"])
                    result = json.loads(stdout.getvalue())
                    self.assertEqual(0, code, result)
                    pdf = base / name / "preview.pdf"
                    self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
                    self.assertEqual(check.digest(pdf.read_bytes()), result["artifacts"]["preview.pdf"])
                else:
                    result = check.capture(source, base / name, Path(os.environ["KICAD_CLI"]))
                self.assertEqual("CAPTURED", result["capture_status"], result["coverage_gaps"])
                return base / name
            before = capture("before")
            display = capture("display", field_x=65)
            report = check.compare(before, display)
            self.assertEqual("PASS", report["status"], report)
            self.assertEqual("display_only", report["impact"]["category"])
            value = capture("value", value="2k")
            self.assertEqual("DIFFERENT", check.compare(before, value)["dimensions"]["properties_and_pin_definitions"]["status"])
            split = capture("split", connect=False)
            self.assertEqual("DIFFERENT", check.compare(before, split)["dimensions"]["network_endpoint_sets"]["status"])
            nc = capture("nc", connect=False, nc=True)
            report = check.compare(split, nc)
            self.assertEqual("DIFFERENT", report["status"], report)
            self.assertEqual("DIFFERENT", report["dimensions"]["pin_semantics_and_nc"]["status"], report)
            (base / "child.kicad_sch").write_text(empty_schematic(uid("changed-child")), encoding="utf-8")
            hierarchy = capture("hierarchy")
            self.assertEqual("DIFFERENT", check.compare(before, hierarchy)["dimensions"]["hierarchy"]["status"])
            (before / "preview.pdf").write_bytes(b"tampered-native-preview")
            report = check.compare(before, before)
            self.assertEqual("UNVERIFIED", report["status"])
            self.assertTrue(any(g.get("artifact") == "preview.pdf" for g in report["coverage_gaps"]))


if __name__ == "__main__":
    unittest.main()
