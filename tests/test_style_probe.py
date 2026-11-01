"""Bounded geometry regressions; expected transforms come from KiCad 10.0.5."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import probe_geometry as geometry
import style_probe


# Native netlist exports observed these offsets for asymmetric local pins.
# Each row is angle, mirror, local (2,3), local (-4,1).
NATIVE_TRANSFORMS = [
    (0, None, (2, -3), (-4, -1)), (90, None, (-3, -2), (-1, 4)),
    (180, None, (-2, 3), (4, 1)), (270, None, (3, 2), (1, -4)),
    (0, "x", (2, 3), (-4, 1)), (90, "x", (-3, 2), (-1, -4)),
    (180, "x", (-2, -3), (4, -1)), (270, "x", (3, -2), (1, 4)),
    (0, "y", (-2, -3), (4, -1)), (90, "y", (3, -2), (1, 4)),
    (180, "y", (2, 3), (-4, 1)), (270, "y", (-3, 2), (-1, -4)),
]


def parse(content):
    return style_probe.parse_sexpr("(kicad_sch " + content + ")")


def pin(number="1", at=(2, 3, 0), length="1", shape="line", hidden=False):
    return ["pin", "passive", shape, ["at", *map(str, at)], ["length", str(length)],
            ["number", number], ["name", "P" + number], *(["hide"] if hidden else [])]


def instance(angle=0, mirror=None, lib="Test:Asymmetric", ref="U1", unit=1, body=1):
    return ["symbol", ["lib_id", lib], ["at", "20", "30", str(angle)],
            ["unit", str(unit)], ["body_style", str(body)], ["uuid", "symbol-uuid"],
            ["property", "Reference", ref], ["property", "Value", "Value",
             ["at", "20", "25", "0"], ["effects", ["font", ["size", "1", "1"]]]],
            *([["mirror", mirror]] if mirror else [])]


def wire(a, b, identifier="wire-uuid"):
    return ["wire", ["pts", ["xy", *map(str, a)], ["xy", *map(str, b)]], ["uuid", identifier]]


def schematic(symbol=None, pins=None):
    symbol = instance() if symbol is None else symbol
    pins = [pin()] if pins is None else pins
    definition = ["symbol", "Test:Asymmetric", ["symbol", "Asymmetric_1_1", *pins]]
    return ["kicad_sch", ["lib_symbols", definition], symbol]


def power_definition(kind, lib_id):
    # Exact standard graphics, independent of the recognizer's shape table.
    lines = {
        "arrow": '((xy -0.762 1.27) (xy 0 2.54))|((xy 0 2.54) (xy 0.762 1.27))|((xy 0 0) (xy 0 2.54))',
        "ground": '((xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27))',
        "flag": '((xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))',
    }[kind]
    graphics = " ".join('(polyline (pts ' + line[1:-1] + ') (stroke (width 0) (type default)) (fill (type none)))'
                        for line in lines.split("|"))
    pin_type = "power_out" if kind == "flag" else "power_in"
    return parse(f'''(symbol "{lib_id}" (power)
      (symbol "P_0_1" {graphics})
      (symbol "P_1_1" (pin {pin_type} line (at 0 0 90) (length 0) (name "P") (number "1"))))''')[1]


def power_schematic(kind="arrow", angle=0, mirror=None, lib="Custom:Power", ref="#PWR01"):
    symbol = instance(angle, mirror, lib, ref)
    root = ["kicad_sch", ["lib_symbols", power_definition(kind, lib)], symbol]
    fields = {p[1]: style_probe.field_info(p) for p in geometry.children(symbol, "property")}
    record = {"symbol_index": 1, "uuid": "symbol-uuid", "reference": ref, "lib_id": [lib],
              "unit": ["1"], "at_raw": geometry.tail(symbol, "at"),
              "mirror": geometry.tail(symbol, "mirror"), "fields": fields}
    return root, [record]


def sexpr(node):
    return "(" + " ".join(sexpr(n) if isinstance(n, list) else json.dumps(str(n)) for n in node) + ")"


class TransformTests(unittest.TestCase):
    def test_all_native_asymmetric_endpoint_mappings(self):
        for angle, mirror, first, second in NATIVE_TRANSFORMS:
            with self.subTest(angle=angle, mirror=mirror):
                transform = geometry.Transform(instance(angle, mirror))
                for local, expected in [((2, 3), first), ((-4, 1), second)]:
                    self.assertEqual(geometry.local_transform(local, angle, mirror), expected)
                    self.assertEqual(transform(local), (20 + expected[0], 30 + expected[1]))

    def test_negative_and_wrapped_angles(self):
        self.assertEqual(geometry.local_transform((2, 3), -90), (3, 2))
        self.assertEqual(geometry.local_transform((2, 3), 450), (-3, -2))
        self.assertEqual(geometry.Transform(instance(-180))((2, 3)), (18, 33))

    def test_unsupported_transform_is_not_silently_approximated(self):
        for angle, mirror in [(45, None), (0, "xy"), ("NaN", None)]:
            with self.subTest(angle=angle, mirror=mirror), self.assertRaises(ValueError):
                geometry.Transform(instance(angle, mirror))


class PinOverlapTests(unittest.TestCase):
    def test_rotated_mirrored_overlaps_use_native_endpoint_mappings(self):
        for angle, mirror, first, _ in NATIVE_TRANSFORMS:
            with self.subTest(angle=angle, mirror=mirror):
                symbol = instance(angle, mirror)
                root = schematic(symbol)
                # Stroke runs from local (2,3) towards (3,3); the independent
                # native offsets determine its page axis and direction.
                dx, dy = {(0, None): (1, 0), (90, None): (0, -1), (180, None): (-1, 0), (270, None): (0, 1),
                          (0, "x"): (1, 0), (90, "x"): (0, 1), (180, "x"): (-1, 0), (270, "x"): (0, -1),
                          (0, "y"): (-1, 0), (90, "y"): (0, -1), (180, "y"): (1, 0), (270, "y"): (0, 1)}[angle, mirror]
                start = (20 + first[0], 30 + first[1])
                end = (start[0] + dx, start[1] + dy)
                root.append(wire(start, end))
                report = geometry.inspect_wire_pin_overlaps(root)
                self.assertEqual(len(report["candidates"]), 1)
                candidate = report["candidates"][0]
                self.assertEqual(candidate["pin_endpoint_mm"], list(map(str, start)))
                self.assertEqual(candidate["pin_stroke_end_mm"], list(map(str, end)))
                self.assertEqual(candidate["overlap_length_mm"], "1")
                self.assertIn("roi_mm", candidate)
                self.assertIn("repair_hint", candidate)

    def test_endpoint_touch_and_perpendicular_crossing_are_not_overlaps(self):
        root = schematic()
        root.extend([wire((21, 27), (22, 27)), wire((23, 27), (24, 27)),
                     wire(("22.5", 26), ("22.5", 28))])
        self.assertEqual(geometry.inspect_wire_pin_overlaps(root)["candidates"], [])

    def test_fractional_positive_overlap_is_reported_exactly(self):
        root = schematic()
        root.append(wire(("22.9999", 27), (24, 27)))
        report = geometry.inspect_wire_pin_overlaps(root)
        self.assertEqual(report["candidates"][0]["overlap_length_mm"], "0.0001")

    def test_current_unit_body_and_common_groups_only(self):
        root = schematic(instance(unit=2, body=2))
        definition = geometry.child(geometry.child(root, "lib_symbols"), "symbol")
        definition[2:] = [["symbol", "A_0_0", pin("1")], ["symbol", "A_2_2", pin("2")],
                          ["symbol", "A_1_2", pin("3")], ["symbol", "A_2_1", pin("4")]]
        root.append(wire((22, 27), (23, 27)))
        report = geometry.inspect_wire_pin_overlaps(root)
        self.assertEqual({c["pin_number"] for c in report["candidates"]}, {"1", "2"})
        self.assertEqual(report["inactive_pin_definitions"], 2)

    def test_unsupported_pins_and_wires_are_individually_addressable(self):
        root = schematic(pins=[pin("1", hidden=True), pin("2", length=0),
                               pin("3", shape="inverted"), pin("4", at=(2, 3, 45))])
        root.extend([wire((0, 0), (1, 1), "diagonal"), wire((0, 0), (0, 0), "zero"),
                     wire(("NaN", 0), (0, 1), "invalid")])
        report = geometry.inspect_wire_pin_overlaps(root)
        self.assertEqual(report["checked_pins"], 0)
        self.assertEqual(len(report["skipped_objects"]), 7)
        self.assertEqual({r["pin_number"] for r in report["skipped_objects"] if r["object_kind"] == "pin"}, {"1", "2", "3", "4"})
        self.assertTrue(all("roi_mm" in r and "review_hint" in r for r in report["skipped_objects"]))

    def test_nonorthogonal_instance_retains_valid_anchor_roi(self):
        report = geometry.inspect_wire_pin_overlaps(schematic(instance(45)))
        self.assertEqual(report["skipped_objects"][0]["reason"], "nonorthogonal_instance_rotation")
        self.assertIsNotNone(report["skipped_objects"][0]["roi_mm"])

    def test_malformed_definition_group_does_not_crash(self):
        root = schematic()
        definition = geometry.child(geometry.child(root, "lib_symbols"), "symbol")
        definition.append(["symbol", pin("bad")])
        geometry.child(root, "lib_symbols").append(["symbol"])
        report = geometry.inspect_wire_pin_overlaps(root)
        self.assertEqual(report["skipped_pin_reasons"], {"unrecognized_definition_group": 1})


class PowerGeometryTests(unittest.TestCase):
    def test_exact_shape_recognition_is_independent_of_library_name(self):
        for kind, expected in [("arrow", "supply_arrow"), ("ground", "ground_triangle"), ("flag", "pwr_flag")]:
            root, _ = power_schematic(kind)
            self.assertEqual(geometry.recognized_power_shapes(root), {"Custom:Power": expected})
            definition = geometry.child(geometry.child(root, "lib_symbols"), "symbol")
            geometry.children(definition, "symbol")[0].append(["circle"])
            self.assertEqual(geometry.recognized_power_shapes(root), {})

    def test_supply_ground_orientation_all_rotations_and_mirrors(self):
        for angle, mirror, _, _ in NATIVE_TRANSFORMS:
            for kind in ("arrow", "ground"):
                with self.subTest(kind=kind, angle=angle, mirror=mirror):
                    root, records = power_schematic(kind, angle, mirror)
                    report = geometry.inspect_power_orientation(root, records)
                    # Arrow and ground start opposite; the forbidden flip is
                    # the same transformed vertical reversal for both.
                    forbidden = (angle == 180 and mirror != "x") or (angle == 0 and mirror == "x")
                    self.assertEqual(len(report["candidates"]), int(forbidden))
                    self.assertEqual(report["checked_instances"], 1)

    def test_upward_mirrored_arrow_value_is_screened(self):
        root, records = power_schematic(angle=180, mirror="x")
        records[0]["fields"]["Value"]["at_raw"] = ["23", "31", "0"]
        report = geometry.inspect_power_value_placement(root, records)
        self.assertEqual(report["checked_instances"], 1)
        self.assertEqual(len(report["candidates"][0]["reasons"]), 2)
        self.assertEqual(report["candidates"][0]["offset_mm"], {"dx": "3", "dy": "1"})

    def test_hidden_vertical_values_remain_unverified(self):
        for change, reason in [({"hidden": True}, "hidden_or_empty_value"),
                               ({"at_raw": ["20", "25", "90"]}, "nonhorizontal_value")]:
            root, records = power_schematic()
            records[0]["fields"]["Value"].update(change)
            report = geometry.inspect_power_value_placement(root, records)
            self.assertEqual(report["checked_instances"], 0)
            self.assertEqual(report["skipped_objects"][0]["reason"], reason)

    def test_value_rule_separates_known_inapplicable_from_unverified(self):
        root, records = ["kicad_sch", ["lib_symbols"]], []
        cases = [("ground", 0), ("flag", 0), ("arrow", 90), ("arrow", 180),
                 ("arrow", 0), ("arrow", 0), ("arrow", 0)]
        for index, (kind, angle) in enumerate(cases, 1):
            local, local_records = power_schematic(kind, angle, lib=f"Custom:P{index}", ref=f"#PWR{index}")
            definition = geometry.child(geometry.child(local, "lib_symbols"), "symbol")
            record = local_records[0]
            record["symbol_index"] = index
            if index == 6:
                record["fields"]["Value"]["hidden"] = True
            if index == 7:
                definition.append(["extends", "Unknown"])
            root[1].append(definition)
            root.append(geometry.children(local, "symbol")[0])
            records.append(record)
        report = geometry.inspect_power_value_placement(root, records)
        self.assertEqual(report["checked_instances"], 1)
        self.assertEqual(report["skipped_power_instances"], 6)
        self.assertEqual(report["skipped_power_instances_breakdown"], {"not_applicable": 4, "unverified": 2})
        self.assertEqual([r["reason"] for r in report["not_applicable"]],
                         ["not_upward_arrow_name_rule", "not_upward_arrow_name_rule",
                          "arrow_not_pointing_up", "arrow_not_pointing_up"])
        self.assertEqual({r["reason"] for r in report["skipped_objects"]},
                         {"hidden_or_empty_value", "unrecognized_power_geometry"})
        direction_candidates = geometry.inspect_power_orientation(root, records)["candidates"]
        self.assertEqual([(r["reference"], r["direction"]) for r in direction_candidates], [("#PWR4", "down")])

    def test_unknown_power_shape_is_not_a_success(self):
        root, records = power_schematic()
        geometry.child(geometry.child(root, "lib_symbols"), "symbol").append(["extends", "Unknown"])
        report = geometry.inspect_power_orientation(root, records)
        self.assertEqual(report["checked_instances"], 0)
        self.assertEqual(report["skipped_objects"][0]["reason"], "unrecognized_power_geometry")
        self.assertNotIn("status", report)

    def test_flag_diamond_intersection_excludes_tangent_and_stem(self):
        cases = [(((-2, "1.905"), (2, "1.905")), 1),
                 (((-2, "2.54"), (2, "2.54")), 0),
                 (((0, 0), (0, "1.27")), 0),
                 (((0, 0), (0, "1.905")), 1)]
        for angle, mirror, _, _ in NATIVE_TRANSFORMS:
            for (a, b), expected in cases:
                with self.subTest(angle=angle, mirror=mirror, a=a, b=b):
                    root, records = power_schematic("flag", angle, mirror)
                    transform = geometry.Transform(geometry.children(root, "symbol")[0])
                    root.append(wire(transform(a), transform(b)))
                    report = geometry.inspect_flag_wire_crossings(root, records)
                    self.assertEqual(len(report["candidates"]), expected)

    def test_custom_renamed_flag_with_unsupported_transform_is_listed(self):
        root, records = power_schematic("flag", 45, lib="Project:Marker", ref="#CUSTOM1")
        report = geometry.inspect_flag_wire_crossings(root, records)
        self.assertEqual(report["checked_instances"], 0)
        self.assertEqual(report["skipped_objects"][0]["shape"], "pwr_flag")


class GroundWireCrossingTests(unittest.TestCase):
    def test_ground_graphic_and_anchor_cases_all_rotations_and_mirrors(self):
        cases = [
            ((0, 3), (0, 0), False),
            ((-3, 0), (0, 0), False),
            ((-3, 0), (3, 0), False),
            ((0, 0), (0, "-3.81"), True),
            ((0, "-1.5"), (0, "-1.8"), True),
            ((-2, "-1.905"), (2, "-1.905"), True),
            (("0.635", "-1.27"), (2, "-1.27"), True),
            ((0, 0), (0, "-0.635"), True),
            ((-2, "-0.635"), (2, "-0.635"), True),
            ((-2, "-0.635"), (0, "-0.635"), True),
            ((-2, "-2.54"), (2, "-2.54"), False),
            (("1.27", "-1.27"), ("1.27", "-3.81"), False),
            ((0, "-2.54"), (0, "-3.81"), False),
            ((-2, -3), (2, -3), False),
            ((0, "-2.5399"), (0, "-2.54"), True),
        ]
        for angle, mirror, _, _ in NATIVE_TRANSFORMS:
            for a, b, expected in cases:
                for reverse in (False, True):
                    with self.subTest(angle=angle, mirror=mirror, a=a, b=b, reverse=reverse):
                        root, records = power_schematic("ground", angle, mirror)
                        transform = geometry.Transform(geometry.children(root, "symbol")[0])
                        ends = [transform(a), transform(b)]
                        root.append(wire(*ends[::-1] if reverse else ends))
                        report = geometry.inspect_ground_wire_crossings(root, records)
                        self.assertEqual(len(report["candidates"]), int(expected))
                        self.assertEqual(report["checked_instances"], 1)
                        self.assertEqual(report["skipped_objects"], [])
                        if expected:
                            candidate = report["candidates"][0]
                            self.assertEqual(candidate["reference"], "#PWR01")
                            self.assertEqual(candidate["wire_uuid"], "wire-uuid")
                            self.assertTrue(candidate["reasons"])
                            self.assertIsNotNone(candidate["roi_mm"])
                            self.assertIn("true anchor", candidate["repair_hint"])

    def test_actual_potentiometer_return_regression(self):
        # Native RV4 source: its low pin and GND share this anchor; the wiper
        # return continues through the triangle instead of joining above it.
        root, records = power_schematic("ground", lib="power:GND", ref="#PWR0042")
        symbol = geometry.children(root, "symbol")[0]
        geometry.child(symbol, "at")[1:] = ["184.15", "233.68", "0"]
        records[0]["at_raw"] = geometry.tail(symbol, "at")
        root.extend([
            wire(("184.15", "233.68"), ("184.15", "237.49"), "through-ground"),
            wire(("184.15", "237.49"), ("191.77", "237.49"), "below-ground"),
        ])
        report = geometry.inspect_ground_wire_crossings(root, records)
        self.assertEqual([c["wire_uuid"] for c in report["candidates"]], ["through-ground"])
        self.assertEqual(set(report["candidates"][0]["reasons"]),
                         {"wire_enters_ground_triangle", "wire_overlaps_ground_stem"})
        # Moving the ground below the final branch makes the same return safe.
        geometry.child(symbol, "at")[2] = "237.49"
        records[0]["at_raw"] = geometry.tail(symbol, "at")
        self.assertEqual(geometry.inspect_ground_wire_crossings(root, records)["candidates"], [])

    def test_coverage_distinguishes_other_shapes_and_unknown_geometry(self):
        for kind in ("arrow", "flag"):
            root, records = power_schematic(kind)
            report = geometry.inspect_ground_wire_crossings(root, records)
            self.assertEqual(report["checked_instances"], 0)
            self.assertEqual(report["skipped_objects"], [])
            self.assertEqual(report["not_applicable"][0]["reason"], "not_triangle_ground_rule")
        root, records = power_schematic("ground", lib="power:GND")
        geometry.child(geometry.child(root, "lib_symbols"), "symbol").append(["extends", "Unknown"])
        report = geometry.inspect_ground_wire_crossings(root, records)
        self.assertEqual(report["checked_instances"], 0)
        self.assertEqual(report["not_applicable"], [])
        self.assertEqual(report["skipped_objects"][0]["reason"], "unrecognized_power_geometry")
        self.assertNotIn("status", report)

    def test_unsupported_transform_and_wires_remain_addressable(self):
        root, records = power_schematic("ground", 45, lib="Project:Return", ref="#CUSTOM1")
        root.extend([wire((0, 0), (1, 1), "diagonal"), wire((0, 0), (0, 0), "zero")])
        report = geometry.inspect_ground_wire_crossings(root, records)
        self.assertEqual(report["checked_instances"], 0)
        self.assertEqual(report["checked_wires"], 0)
        self.assertEqual(report["skipped_wires"], 2)
        self.assertEqual({r["reason"] for r in report["skipped_objects"]},
                         {"nonorthogonal_instance_rotation", "nonorthogonal", "zero_length"})
        self.assertEqual(report["skipped_objects"][0]["shape"], "ground_triangle")
        self.assertTrue(all("roi_mm" in r and "review_hint" in r for r in report["skipped_objects"]))


class ProbeIntegrationTests(unittest.TestCase):
    def test_report_schema_hash_and_nonmutation(self):
        root = schematic()
        root.append(wire((22, 27), (23, 27)))
        root.append(["sheet", ["uuid", "sheet-uuid"], ["property", "Sheetfile", "child.kicad_sch"]])
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.kicad_sch"
            content = sexpr(root).encode()
            source.write_bytes(content)
            report = style_probe.inspect(source, details=True)
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(report["source_sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["report_kind"], "facts_and_candidates")
            self.assertEqual(len(report["wire_pin_overlaps"]["candidates"]), 1)
            self.assertIn("ground_wire_crossings", report)
            self.assertEqual(report["hierarchy_gaps"][0]["uuid"], "sheet-uuid")
            self.assertNotIn("status", report)
            json.dumps(report)

    def test_unsupported_wire_does_not_break_other_report_sections(self):
        root = schematic()
        root.append(wire(("NaN", 0), (0, 1)))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.kicad_sch"
            source.write_text(sexpr(root), encoding="utf-8")
            report = style_probe.inspect(source)
            self.assertEqual(report["wire_pin_overlaps"]["skipped_wires"], 1)

    def test_ground_crossing_is_exposed_by_full_probe_without_mutation(self):
        root, _ = power_schematic("ground")
        root.append(wire((20, 30), (20, "33.81")))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.kicad_sch"
            content = sexpr(root).encode()
            source.write_bytes(content)
            report = style_probe.inspect(source)
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(len(report["ground_wire_crossings"]["candidates"]), 1)

    def test_cli_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.kicad_sch"
            content = sexpr(schematic()).encode()
            source.write_bytes(content)
            result = subprocess.run([sys.executable, style_probe.__file__, str(source), "--output", str(source)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(source.read_bytes(), content)

    def test_parser_helpers_remain_compatible(self):
        root = parse('(wire (uuid "w")) (wire (uuid "v"))')
        self.assertEqual(len(style_probe.children(root, "wire")), 2)
        self.assertEqual(style_probe.tail(style_probe.child(root, "wire"), "uuid"), ["w"])


if __name__ == "__main__":
    unittest.main()
