"""Recipe behavior and native endpoint checks for schematic authoring."""

from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sch_author import Drawing, move_group, verify_expected
from sch_author_io import Atom, child, children, dumps, loads


ASSETS = Path(__file__).resolve().parents[1] / "references" / "calibration-candidates"
CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
PROJECT_ID = "6afaa668-583c-491f-921d-7784259dc401"
D = Decimal


def xy(values):
    return tuple(D(str(value)) for value in values[:2])


def at(node):
    return xy(child(node, "at")[1:])


def properties(symbol):
    return {prop[1]: prop for prop in children(symbol, "property")}


def instances(root):
    return {properties(symbol)["Reference"][2]: symbol
            for symbol in children(root, "symbol")}


def segments(root):
    return {frozenset(xy(point[1:]) for point in children(child(wire, "pts"), "xy"))
            for wire in children(root, "wire")}


def all_uuids(node):
    if not isinstance(node, list):
        return []
    if node and node[0] == "uuid":
        return [str(node[1])]
    return [value for item in node for value in all_uuids(item)]


def transformed_pin(local, rotation, mirror):
    # Independent endpoint table; do not use the author's transform as its oracle.
    x, y = xy(local)
    dx, dy = {0: (x, -y), 90: (-y, -x),
              180: (-x, y), 270: (y, x)}[rotation]
    return (-dx if mirror == "y" else dx,
            -dy if mirror == "x" else dy)


class AuthorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sch-author-tests-")
        cls.directory = Path(cls.temporary.name)
        symbols = []
        for kind, case in (("R", "resistor_short_inside"), ("C", "caps_compact")):
            source = loads((ASSETS / f"{case}.kicad_sch").read_text(encoding="utf-8"))
            definition = deepcopy(children(child(source, "lib_symbols"), "symbol")[0])
            definition[1] = kind
            symbols.append(definition)
        library = [Atom("kicad_symbol_lib"), [Atom("version"), Atom("20241209")],
                   [Atom("generator"), "sch_author_test"], *symbols]
        cls.library = cls.directory / "Device.kicad_sym"
        cls.library.write_text(dumps(library), encoding="utf-8")

        asymmetric = deepcopy(symbols[0])
        asymmetric[1] = "Asym"
        properties(asymmetric)["Reference"][2] = "U"
        for group in children(asymmetric, "symbol"):
            group[1] = group[1].replace("R_", "Asym_", 1)
            for pin in children(group, "pin"):
                number = child(pin, "number")[1]
                coordinates = ("-2.54", "5.08") if number == "1" else ("3.81", "-1.27")
                child(pin, "at")[1:3] = list(map(Atom, coordinates))
        dual = deepcopy(asymmetric)
        dual[1] = "Dual"
        for group in children(dual, "symbol"):
            group[1] = group[1].replace("Asym_", "Dual_", 1)
        second_unit = deepcopy(next(group for group in children(dual, "symbol") if group[1] == "Dual_1_1"))
        second_unit[1] = "Dual_2_1"
        for pin in children(second_unit, "pin"):
            child(pin, "number")[1] = str(int(child(pin, "number")[1]) + 2)
        dual.append(second_unit)
        cls.asymmetric_library = cls.directory / "Test.kicad_sym"
        cls.asymmetric_library.write_text(dumps(library[:3] + [asymmetric, dual]), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def recipe(self):
        return {"schema_version": 1, "name": "sample", "project_id": PROJECT_ID,
                "libraries": {"Device": str(self.library), "Test": str(self.asymmetric_library)},
                "symbols": []}

    def resistor(self, ref="R1", **overrides):
        result = {"id": ref, "ref": ref, "lib_id": "Device:R", "value": "10k",
                  "at": [40, 40], "rotation": 0}
        result.update(overrides)
        return result

    def compile(self, recipe):
        return loads(Drawing(recipe).render())

    def multiunit(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor("U1", id="U1A", lib_id="Test:Dual", value="Dual", unit=1),
                             self.resistor("U1", id="U1B", lib_id="Test:Dual", value="Dual", unit=2,
                                           at=[60, 40])]
        return recipe

    def series(self):
        recipe = self.recipe()
        recipe["groups"] = [{"id": "stage", "origin": [40, 40]}]
        recipe["symbols"] = [
            self.resistor(group="stage", at=[0, 0], rotation=90,
                          fields={"Value": {"layout": "inside"},
                                  "Reference": {"visible": False}}),
            self.resistor("R2", group="stage", rotation=90, value="22k",
                          fields={"Value": {"offset": [0, "2.54"], "align": "center",
                                            "orientation": "horizontal"}}),
        ]
        recipe["symbols"][1].pop("at")
        recipe["symbols"][1]["attach"] = {
            "pin": "1", "to": {"pin": ["R1", "2"]}, "offset": ["5.08", 0]}
        recipe["wires"] = [{"id": "middle", "points": [
            {"pin": ["R1", "2"]}, {"pin": ["R2", "1"]}]}]
        recipe["labels"] = [
            {"id": "input", "name": "IN", "at": {"pin": ["R1", "1"]},
             "kind": "global", "role": "input", "side": "left"},
            {"id": "output", "name": "OUT", "at": {"pin": ["R2", "2"]},
             "kind": "global", "role": "output", "side": "right"},
        ]
        return recipe

    def test_all_orthogonal_rotations_and_mirrors_resolve_asymmetric_pins(self):
        for rotation in (0, 90, 180, 270):
            for mirror in (None, "x", "y"):
                with self.subTest(rotation=rotation, mirror=mirror):
                    recipe = self.recipe()
                    symbol = self.resistor("U1", lib_id="Test:Asym", rotation=rotation)
                    if mirror is not None:
                        symbol["mirror"] = mirror
                    recipe["symbols"] = [symbol]
                    drawing = Drawing(recipe)
                    self.assertEqual(drawing.origin("U1"), (D(40), D(40)))
                    for number, local in (("1", ("-2.54", "5.08")),
                                          ("2", ("3.81", "-1.27"))):
                        offset = transformed_pin(local, rotation, mirror)
                        self.assertEqual(drawing.pin("U1", number),
                                         tuple(D(40) + coordinate for coordinate in offset))

    def test_horizontal_field_alignment_compensates_for_native_transform(self):
        for rotation in (0, 90, 180, 270):
            for mirror in (None, "x", "y"):
                for alignment in ("left", "center", "right"):
                    with self.subTest(rotation=rotation, mirror=mirror, alignment=alignment):
                        recipe = self.recipe()
                        symbol = self.resistor(rotation=rotation, fields={"Value": {
                            "offset": ["2.54", "-1.27"], "orientation": "horizontal",
                            "align": alignment, "size": "1.27"}})
                        if mirror is not None:
                            symbol["mirror"] = mirror
                        recipe["symbols"] = [symbol]
                        value = properties(instances(self.compile(recipe))["R1"])["Value"]
                        self.assertEqual(at(value), (D("42.54"), D("38.73")))
                        self.assertEqual(D(child(value, "at")[3]), D(rotation % 180))
                        effects = child(value, "effects")
                        justify = child(effects, "justify", ["justify"])[1:]
                        expected = alignment
                        if alignment != "center" and ((rotation in (90, 180)) != (mirror == "y")):
                            expected = "right" if alignment == "left" else "left"
                        self.assertEqual([item for item in justify if item in ("left", "right")],
                                         [] if expected == "center" else [expected])
                        self.assertEqual(child(child(effects, "font"), "size")[1:],
                                         ["1.27", "1.27"])

    def test_inside_value_is_centered_and_hidden_reference_is_retained(self):
        root = self.compile(self.series())
        fields = properties(instances(root)["R1"])
        self.assertEqual(fields["Reference"][2], "R1")
        hidden = child(fields["Reference"], "hide") or child(child(fields["Reference"], "effects"), "hide")
        self.assertEqual(hidden, ["hide", "yes"])
        self.assertEqual(at(fields["Value"]), at(instances(root)["R1"]))
        self.assertFalse({"left", "right"}.intersection(
            child(child(fields["Value"], "effects"), "justify", ["justify"])[1:]))

    def test_simulation_exclusion_is_explicit_and_accepts_boolean_override(self):
        for override, expected in (({}, "no"), ({"exclude_from_sim": False}, "no"),
                                   ({"exclude_from_sim": True}, "yes")):
            with self.subTest(override=override):
                recipe = self.recipe()
                recipe["symbols"] = [self.resistor(**override)]
                symbol = instances(self.compile(recipe))["R1"]
                self.assertEqual(child(symbol, "exclude_from_sim"), ["exclude_from_sim", expected])

    def test_attachment_positions_actual_pin_not_symbol_center(self):
        drawing = Drawing(self.series())
        self.assertEqual(drawing.pin("R2", "1"), (D("48.89"), D(40)))
        self.assertEqual(drawing.origin("R2"), (D("52.70"), D(40)))
        self.assertIsInstance(drawing.report(), dict)

    def test_group_move_translates_complete_local_geometry_and_retains_identity(self):
        recipe = self.series()
        before_recipe = deepcopy(recipe)
        delta = (D("12.70"), D("-5.08"))
        shifted = move_group(recipe, "stage", [str(value) for value in delta])
        self.assertEqual(recipe, before_recipe)
        self.assertIsNot(shifted, recipe)
        before, after = Drawing(recipe), Drawing(shifted)
        for ref in ("R1", "R2"):
            for number in ("1", "2"):
                self.assertEqual(after.pin(ref, number), tuple(
                    coordinate + distance for coordinate, distance in zip(before.pin(ref, number), delta)))
        old, new = loads(before.render()), loads(after.render())
        self.assertEqual(all_uuids(old), all_uuids(new))
        self.assertEqual(child(old, "lib_symbols"), child(new, "lib_symbols"))
        self.assertEqual(segments(new), {frozenset(tuple(coordinate + distance
                         for coordinate, distance in zip(point, delta)) for point in segment)
                         for segment in segments(old)})
        for old_symbol, new_symbol in zip(instances(old).values(), instances(new).values()):
            for name, old_field in properties(old_symbol).items():
                self.assertEqual(at(properties(new_symbol)[name]), tuple(
                    coordinate + distance for coordinate, distance in zip(at(old_field), delta)))
        for old_label, new_label in zip(children(old, "global_label"), children(new, "global_label")):
            self.assertEqual(at(new_label), tuple(
                coordinate + distance for coordinate, distance in zip(at(old_label), delta)))

    def test_nested_groups_are_relative_and_parent_move_propagates(self):
        recipe = self.recipe()
        recipe["groups"] = [{"id": "outer", "origin": [10, 20]},
                            {"id": "inner", "parent": "outer", "origin": [30, 40]}]
        recipe["symbols"] = [self.resistor(group="inner", at=["1.27", "2.54"])]
        before = Drawing(recipe)
        self.assertEqual(before.group_origin("inner"), (D(40), D(60)))
        self.assertEqual(before.origin("R1"), (D("41.27"), D("62.54")))
        after = Drawing(move_group(recipe, "outer", ["2.54", "-1.27"]))
        self.assertEqual(after.origin("R1"), (D("43.81"), D("61.27")))

    def test_project_name_and_coordinates_do_not_define_object_uuids(self):
        recipe = self.series()
        old = all_uuids(self.compile(recipe))
        recipe["name"] = "renamed-project"
        self.assertEqual(all_uuids(self.compile(recipe)), old)
        recipe["project_id"] = "c6b6d7cc-8ec0-42da-a578-b4f37ad59685"
        self.assertTrue(set(old).isdisjoint(all_uuids(self.compile(recipe))))

    def test_cross_group_attachment_is_rejected(self):
        recipe = self.series()
        recipe["groups"].append({"id": "other", "origin": [100, 100]})
        recipe["symbols"][1]["group"] = "other"
        with self.assertRaises(ValueError):
            self.compile(recipe)

    def test_cross_group_attachment_cannot_hide_behind_named_anchor(self):
        recipe = self.series()
        recipe["groups"].append({"id": "other", "origin": [100, 100]})
        recipe["symbols"][1]["group"] = "other"
        recipe["anchors"] = {"source": {"pin": ["R1", "2"]}}
        recipe["symbols"][1]["attach"]["to"] = {"anchor": "source"}
        with self.assertRaises(ValueError):
            self.compile(recipe)

    def test_library_paths_resolve_against_recipe_directory_and_allow_override(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor()]
        recipe["libraries"] = {"Device": "Device.kicad_sym"}
        relative = Drawing(recipe, base_dir=self.directory)
        recipe["libraries"] = {"Device": "missing.kicad_sym"}
        override = Drawing(recipe, library_paths={"Device": str(self.library)})
        self.assertEqual(relative.render(), override.render())

    def test_named_mixed_axis_anchors_and_orthogonal_route(self):
        recipe = self.recipe()
        recipe["groups"] = [{"id": "g", "origin": [20, 30]}]
        recipe["symbols"] = [self.resistor(group="g", at=[0, 0])]
        recipe["anchors"] = {"corner": {"at": [10, 10], "group": "g"},
                             "shifted": {"anchor": "corner", "offset": ["1.27", 0]}}
        recipe["wires"] = [{"id": "route", "route": "hv", "points": [
            {"pin": ["R1", "1"]},
            {"x": {"anchor": "shifted"}, "y": {"anchor": "corner"}, "offset": [0, "1.27"]}]}]
        self.assertEqual(segments(self.compile(recipe)), {
            frozenset(((D(20), D("26.19")), (D("31.27"), D("26.19")))),
            frozenset(((D("31.27"), D("26.19")), (D("31.27"), D("41.27")))),
        })

    def test_t_junction_splits_bus_and_inserts_dot(self):
        recipe = self.recipe()
        recipe["wires"] = [{"id": "bus", "points": [[0, 0], ["10.16", 0]]},
                           {"id": "branch", "points": [["5.08", 0], ["5.08", "5.08"]]}]
        root = self.compile(recipe)
        self.assertEqual(len(children(root, "wire")), 3)
        self.assertEqual([at(node) for node in children(root, "junction")], [(D("5.08"), D(0))])
        self.assertTrue(all((D("5.08"), D(0)) in segment for segment in segments(root)))

    def test_bus_endpoint_junctions_with_coincident_pins_have_distinct_uuids(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor("R1", at=[40, "36.19"]),
                             self.resistor("R2", at=[40, "43.81"]),
                             self.resistor("R3", at=["60.32", "36.19"]),
                             self.resistor("R4", at=["60.32", "43.81"])]
        recipe["wires"] = [{"id": "bus", "points": [[40, 40], ["60.32", 40]]}]
        root = self.compile(recipe)
        junctions = children(root, "junction")
        self.assertEqual(len(children(root, "wire")), 1)
        self.assertEqual({at(node) for node in junctions}, {(D(40), D(40)), (D("60.32"), D(40))})
        self.assertEqual(len({child(node, "uuid")[1] for node in junctions}), 2)
        self.assertEqual(len(all_uuids(root)), len(set(all_uuids(root))))

    def test_three_coincident_pins_form_junction_without_any_wire(self):
        recipe = self.recipe()
        recipe["groups"] = [{"id": "direct", "origin": [40, 40]}]
        recipe["symbols"] = [self.resistor("R1", group="direct", at=[0, "-3.81"]),
                             self.resistor("R2", group="direct", at=[0, "3.81"]),
                             self.resistor("R3", group="direct", at=["3.81", 0], rotation=90)]
        root = self.compile(recipe)
        junctions = children(root, "junction")
        self.assertEqual(children(root, "wire"), [])
        self.assertEqual([at(node) for node in junctions], [(D(40), D(40))])
        self.assertEqual(len(all_uuids(root)), len(set(all_uuids(root))))
        moved = self.compile(move_group(recipe, "direct", ["2.54", "5.08"]))
        self.assertEqual([at(node) for node in children(moved, "junction")], [(D("42.54"), D("45.08"))])
        self.assertEqual(all_uuids(root), all_uuids(moved))

    def test_crossing_is_unjoined_until_junction_is_explicit(self):
        recipe = self.recipe()
        recipe["wires"] = [{"id": "horizontal", "points": [[0, 5], [10, 5]]},
                           {"id": "vertical", "points": [[5, 0], [5, 10]]}]
        plain = self.compile(recipe)
        self.assertEqual(len(children(plain, "wire")), 2)
        self.assertEqual(children(plain, "junction"), [])
        recipe["junctions"] = [[5, 5]]
        joined = self.compile(recipe)
        self.assertEqual(len(children(joined, "wire")), 4)
        self.assertEqual([at(node) for node in children(joined, "junction")], [(D(5), D(5))])

    def test_pin_in_bus_interior_is_emitted_as_wire_endpoint(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor(at=["5.08", "3.81"])]
        recipe["wires"] = [{"id": "bus", "points": [[0, 0], ["10.16", 0]]}]
        root = self.compile(recipe)
        self.assertEqual(len(children(root, "wire")), 2)
        self.assertTrue(all((D("5.08"), D(0)) in segment for segment in segments(root)))

    def test_zero_length_wire_legs_are_omitted(self):
        recipe = self.recipe()
        recipe["wires"] = [{"id": "zero", "points": [[1, 2], [1, 2]]},
                           {"id": "route", "route": "hv", "points": [[3, 4], [3, 8]]}]
        root = self.compile(recipe)
        self.assertEqual(len(children(root, "wire")), 1)
        self.assertEqual(segments(root), {frozenset(((D(3), D(4)), (D(3), D(8))))})

    def test_no_connect_uses_resolved_pin_anchor(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor(rotation=90, mirror="y")]
        recipe["no_connects"] = [{"pin": ["R1", "1"]}]
        root = self.compile(recipe)
        self.assertEqual([at(node) for node in children(root, "no_connect")], [(D("43.81"), D(40))])

    def test_no_connect_cannot_mask_connected_pin(self):
        for number in ("1", "2"):
            with self.subTest(pin=number):
                recipe = self.series()
                recipe["no_connects"] = [{"pin": ["R1", number]}]
                with self.assertRaises(ValueError):
                    self.compile(recipe)

    def test_cycles_and_missing_references_fail(self):
        cases = []
        recipe = self.series()
        recipe["groups"][0]["parent"] = "stage"
        cases.append(("group cycle", recipe))
        recipe = self.recipe()
        recipe["groups"] = [{"id": "unused", "parent": "unused", "origin": [0, 0]}]
        cases.append(("unused group cycle", recipe))
        recipe = self.recipe()
        recipe["groups"] = [{"id": "unused", "parent": "missing", "origin": [0, 0]}]
        cases.append(("unused group unknown parent", recipe))
        recipe = self.series()
        recipe["symbols"][0].pop("at")
        recipe["symbols"][0]["attach"] = {"pin": "1", "to": {"pin": ["R2", "2"]}}
        cases.append(("attachment cycle", recipe))
        recipe = self.recipe()
        recipe["anchors"] = {"a": {"anchor": "b"}, "b": {"anchor": "a"}}
        recipe["labels"] = [{"id": "label", "name": "A", "kind": "local", "at": {"anchor": "a"}}]
        cases.append(("anchor cycle", recipe))
        for name, point in (("unknown pin", {"pin": ["R1", "404"]}),
                            ("unknown symbol", {"pin": ["R404", "1"]}),
                            ("unknown anchor", {"anchor": "missing"})):
            recipe = self.series()
            recipe["labels"][0]["at"] = point
            cases.append((name, recipe))
        for name, recipe in cases:
            with self.subTest(case=name), self.assertRaises(ValueError):
                self.compile(recipe)

    def test_malformed_schema_and_unknown_keys_fail(self):
        cases = []
        for key, value in (("schema_version", 99), ("schema_version", True), ("project_id", "not-a-uuid"),
                           ("symblos", [])):
            recipe = self.series()
            recipe[key] = value
            cases.append((f"{key}={value!r}", recipe))
        for key, value in (("rotation", 45), ("mirror", "xy"), ("at", ["NaN", 40]),
                           ("at", [True, 40]), ("group", "absent"), ("roation", 90),
                           ("exclude_from_sim", "no"), ("exclude_from_sim", 1)):
            recipe = self.series()
            recipe["symbols"][0][key] = value
            cases.append((f"symbol {key}={value}", recipe))
        for section, key, value in (("wires", "route", "diagonal"),
                                    ("wires", "rout", "hv"),
                                    ("groups", "originn", [0, 0]),
                                    ("labels", "side", "up"),
                                    ("labels", "rol", "input")):
            recipe = self.series()
            recipe[section][0][key] = value
            cases.append((f"{section} {key}", recipe))
        recipe = self.series()
        recipe["symbols"][0]["fields"]["Value"]["algin"] = "left"
        cases.append(("unknown field option", recipe))
        recipe = self.series()
        recipe["labels"][0]["at"]["offest"] = [1, 2]
        cases.append(("unknown anchor option", recipe))
        recipe = self.series()
        recipe["labels"][0].pop("role")
        cases.append(("global label missing role", recipe))
        recipe = self.series()
        recipe["symbols"][1]["at"] = [0, 0]
        cases.append(("both at and attach", recipe))
        recipe = self.series()
        recipe["symbols"][1]["attach"]["to"] = {"pin": []}
        cases.append(("empty attachment pin reference", recipe))
        recipe = self.series()
        recipe["symbols"].append(deepcopy(recipe["symbols"][0]))
        cases.append(("duplicate symbol identity", recipe))
        for name, recipe in cases:
            with self.subTest(case=name), self.assertRaises(ValueError):
                self.compile(recipe)

    def test_absent_unit_or_body_style_is_not_satisfied_by_common_graphics(self):
        for option in ("unit", "body_style"):
            for value in (2, 99):
                with self.subTest(option=option, value=value):
                    recipe = self.recipe()
                    recipe["symbols"] = [self.resistor(**{option: value})]
                    with self.assertRaises(ValueError):
                        self.compile(recipe)

    def test_multiunit_package_allows_independent_field_placement(self):
        recipe = self.multiunit()
        recipe["symbols"][1]["fields"] = {"Value": {"offset": [0, "2.54"]},
                                            "Reference": {"visible": False}}
        root = self.compile(recipe)
        units = children(root, "symbol")
        self.assertEqual(len(units), 2)
        self.assertEqual({child(symbol, "unit")[1] for symbol in units}, {"1", "2"})
        self.assertEqual({properties(symbol)["Reference"][2] for symbol in units}, {"U1"})

    def test_multiunit_package_rejects_inconsistent_identity_properties_and_flags(self):
        changes = [("lib_id", {"lib_id": "Test:Asym"}),
                   ("value", {"value": "Other"}),
                   ("Value field", {"fields": {"Value": {"value": "Other"}}}),
                   ("Footprint property", {"properties": {"Footprint": "Package_Test:Other"}}),
                   ("Footprint field", {"fields": {"Footprint": {"value": "Package_Test:Other"}}}),
                   ("BOM", {"in_bom": False}), ("board", {"on_board": False}), ("DNP", {"dnp": True}),
                   ("simulation", {"exclude_from_sim": True})]
        for name, change in changes:
            with self.subTest(property=name):
                recipe = self.multiunit()
                recipe["symbols"][0].update(change)
                with self.assertRaises(ValueError):
                    self.compile(recipe)

    def native_netlist(self, recipe, name):
        source = self.directory / f"{name}.kicad_sch"
        target = self.directory / f"{name}.xml"
        source.write_text(Drawing(recipe).render(), encoding="utf-8")
        run = subprocess.run([str(CLI), "sch", "export", "netlist", "--format", "kicadxml",
                              "-o", str(target), str(source)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
        root = ET.parse(target).getroot()
        components = {node.get("ref"): node.findtext("value") for node in root.findall("./components/comp")}
        nets = {node.get("name"): {(pin.get("ref"), pin.get("pin")) for pin in node.findall("node")}
                for node in root.findall("./nets/net")}
        return components, nets

    def run_author(self, *arguments):
        script = Path(__file__).resolve().parents[1] / "scripts" / "sch_author.py"
        return subprocess.run([sys.executable, "-B", str(script), *map(str, arguments)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)

    def test_cli_build_preserves_existing_output_and_freezes_library_paths(self):
        with tempfile.TemporaryDirectory(prefix="sch-author-build-") as temporary:
            directory = Path(temporary)
            recipe = self.series()
            source = directory / "input.json"
            source.write_text(json.dumps(recipe), encoding="utf-8")
            output = directory / "bundle"
            first = self.run_author("build", source, "--output", output)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["status"], "BUILT")
            frozen = json.loads((output / "recipe.json").read_text(encoding="utf-8"))
            self.assertTrue(all(Path(path).is_absolute() for path in frozen["libraries"].values()))
            before = {path.name: path.read_bytes() for path in output.iterdir()}
            second = self.run_author("build", source, "--output", output)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(json.loads(second.stderr)["status"], "ERROR")
            self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, before)

    def test_cli_move_supports_negative_decimal_delta_and_library_override(self):
        with tempfile.TemporaryDirectory(prefix="sch-author-move-") as temporary:
            directory = Path(temporary)
            recipe = self.series()
            recipe["libraries"] = {"Device": "missing.kicad_sym"}
            source = directory / "input.json"
            source.write_text(json.dumps(recipe), encoding="utf-8")
            before = source.read_bytes()
            output = directory / "nested" / "moved.json"
            run = self.run_author("move-group", source, "--group", "stage", "--delta", "-2.54", "5.08",
                                  "--output", output, "--library", f"Device={self.library}")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(source.read_bytes(), before)
            moved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(Drawing(moved).group_origin("stage"), (D("37.46"), D("45.08")))
            self.assertEqual(Path(moved["libraries"]["Device"]), self.library)
            refused = self.run_author("move-group", source, "--group", "stage", "--delta", "1", "1",
                                      "--output", source, "--library", f"Device={self.library}")
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(source.read_bytes(), before)

    def test_cli_bad_input_reports_error_without_partial_output(self):
        with tempfile.TemporaryDirectory(prefix="sch-author-invalid-") as temporary:
            directory = Path(temporary)
            source, output = directory / "bad.json", directory / "bundle"
            for text in ("{broken-json", json.dumps({"schema_version": 1, "symbols": []})):
                with self.subTest(source=text):
                    source.write_text(text, encoding="utf-8")
                    run = self.run_author("build", source, "--output", output)
                    self.assertEqual(run.returncode, 2)
                    self.assertEqual(json.loads(run.stderr)["status"], "ERROR")
                    self.assertNotIn("Traceback", run.stderr)
                    self.assertFalse(output.exists())

    def test_cli_native_export_failure_preserves_error_manifest_and_source(self):
        with tempfile.TemporaryDirectory(prefix="sch-author-export-error-") as temporary:
            directory = Path(temporary)
            source, output = directory / "input.json", directory / "bundle"
            source.write_text(json.dumps(self.series()), encoding="utf-8")
            missing_cli = directory / "missing-kicad-cli.exe"
            run = self.run_author("build", source, "--output", output, "--kicad-cli", missing_cli)
            self.assertEqual(run.returncode, 2, run.stderr or run.stdout)
            self.assertNotIn("Traceback", run.stderr)
            failure = json.loads(run.stderr)
            self.assertEqual(failure["status"], "ERROR")
            self.assertTrue(failure["error"])
            saved = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, failure)
            self.assertNotEqual(saved["native"], "EXPORTED")
            self.assertTrue((output / "sample.kicad_sch").is_file())
            self.assertTrue((output / "recipe.json").is_file())

    def test_cli_invalid_project_id_and_expected_pin_pair_fail_before_output_creation(self):
        for key, value in (("project_id", 123), ("expected_nets", [{"pins": [[]]}])):
            with self.subTest(key=key), tempfile.TemporaryDirectory(prefix="sch-author-schema-") as temporary:
                directory = Path(temporary)
                source, output = directory / "invalid.json", directory / "bundle"
                recipe = self.series()
                recipe[key] = value
                source.write_text(json.dumps(recipe), encoding="utf-8")
                run = self.run_author("build", source, "--output", output)
                self.assertEqual(run.returncode, 2, run.stderr or run.stdout)
                self.assertNotIn("Traceback", run.stderr)
                error = json.loads(run.stderr)
                self.assertEqual(error["status"], "ERROR")
                self.assertTrue(error["error"])
                self.assertFalse(output.exists())

    def test_missing_expected_nets_is_unverified(self):
        self.assertEqual(verify_expected(self.series(), "unused.xml")["status"], "UNVERIFIED")

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_series_values_and_endpoint_sets_survive_group_move(self):
        recipe = self.series()
        original = self.native_netlist(recipe, "series-original")
        shifted = self.native_netlist(move_group(recipe, "stage", ["12.7", "-5.08"]), "series-shifted")
        self.assertEqual(original, shifted)
        components, nets = original
        self.assertEqual(components, {"R1": "10k", "R2": "22k"})
        self.assertEqual(nets["IN"], {("R1", "1")})
        self.assertEqual(nets["OUT"], {("R2", "2")})
        self.assertIn({("R1", "2"), ("R2", "1")}, nets.values())
        self.assertEqual(len(nets), 3)

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_intent_verification_rejects_both_open_and_short(self):
        recipe = self.series()
        recipe["expected_nets"] = [{"name": "IN", "pins": [["R1", "1"]]},
                                   {"name": "OUT", "pins": [["R2", "2"]]},
                                   {"pins": [["R1", "2"], ["R2", "1"]]}]
        self.native_netlist(recipe, "intent-correct")
        report = verify_expected(recipe, self.directory / "intent-correct.xml")
        self.assertEqual(report["status"], "PASS")
        for fault in ("open", "short"):
            with self.subTest(fault=fault):
                broken = deepcopy(recipe)
                if fault == "open":
                    broken["wires"] = []
                else:
                    broken["wires"].append({"id": "short", "points": [
                        {"pin": ["R1", "1"]}, {"pin": ["R1", "2"]}]})
                self.native_netlist(broken, f"intent-{fault}")
                report = verify_expected(recipe, self.directory / f"intent-{fault}.xml")
                self.assertEqual(report["status"], "DIFFERENT")
                self.assertTrue(report["missing"])
                self.assertTrue(report["extra"])

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_intent_verification_includes_isolated_no_connect_pin(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor()]
        recipe["no_connects"] = [{"pin": ["R1", "2"]}]
        recipe["labels"] = [{"id": "input", "name": "IN", "at": {"pin": ["R1", "1"]},
                             "kind": "global", "role": "input", "side": "left"}]
        recipe["expected_nets"] = [{"name": "IN", "pins": [["R1", "1"]]},
                                   {"pins": [["R1", "2"]]}]
        _, nets = self.native_netlist(recipe, "intent-no-connect")
        self.assertIn({("R1", "2")}, nets.values())
        self.assertEqual(verify_expected(recipe, self.directory / "intent-no-connect.xml")["status"], "PASS")
        recipe["expected_nets"].pop()
        self.assertEqual(verify_expected(recipe, self.directory / "intent-no-connect.xml")["status"], "DIFFERENT")

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_all_twelve_transforms_use_true_connection_anchors(self):
        recipe = self.recipe()
        recipe["labels"] = []
        expected = {}
        for row, mirror in enumerate((None, "x", "y")):
            for column, rotation in enumerate((0, 90, 180, 270)):
                ref = f"U{row * 4 + column + 1}"
                origin = (D(30 + column * 35), D(30 + row * 35))
                symbol = self.resistor(ref, lib_id="Test:Asym", rotation=rotation,
                                        at=[str(value) for value in origin])
                if mirror is not None:
                    symbol["mirror"] = mirror
                recipe["symbols"].append(symbol)
                for number, local in (("1", ("-2.54", "5.08")), ("2", ("3.81", "-1.27"))):
                    offset = transformed_pin(local, rotation, mirror)
                    endpoint = [str(value + delta) for value, delta in zip(origin, offset)]
                    name = f"{ref}_P{number}"
                    recipe["labels"].append({"id": name, "name": name, "at": endpoint,
                                              "kind": "global", "role": "passive", "side": "left"})
                    expected[name] = {(ref, number)}
        _, nets = self.native_netlist(recipe, "all-transforms")
        self.assertEqual(nets, expected)

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_crossing_remains_separate_unless_explicitly_joined(self):
        recipe = self.recipe()
        recipe["symbols"] = [self.resistor("R1", at=["20.32", "40.64"], rotation=90),
                             self.resistor("R2", at=["60.96", "40.64"], rotation=90),
                             self.resistor("R3", at=["40.64", "20.32"]),
                             self.resistor("R4", at=["40.64", "60.96"])]
        recipe["wires"] = [
            {"id": "horizontal", "points": [{"pin": ["R1", "2"]}, {"pin": ["R2", "1"]}]},
            {"id": "vertical", "points": [{"pin": ["R3", "2"]}, {"pin": ["R4", "1"]}]},
        ]
        _, separate = self.native_netlist(recipe, "crossing-separate")
        self.assertEqual({frozenset(pins) for pins in separate.values() if len(pins) > 1}, {
            frozenset((("R1", "2"), ("R2", "1"))),
            frozenset((("R3", "2"), ("R4", "1"))),
        })
        recipe["junctions"] = [["40.64", "40.64"]]
        _, joined = self.native_netlist(recipe, "crossing-joined")
        self.assertEqual([pins for pins in joined.values() if len(pins) > 1], [
            {("R1", "2"), ("R2", "1"), ("R3", "2"), ("R4", "1")},
        ])


if __name__ == "__main__":
    unittest.main()
