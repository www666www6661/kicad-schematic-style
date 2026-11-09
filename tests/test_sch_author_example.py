"""Portable MOS recipe and native group-movement regression checks."""

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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from sch_author import Drawing, read_recipe
from sch_author_io import child, children, loads


RECIPE = ROOT / "references" / "authoring-examples" / "mos-driver.json"
CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
LIBRARIES = ("Device", "Transistor_FET", "power")


def library_paths():
    candidates = [Path(value) for key, value in os.environ.items()
                  if key.startswith("KICAD") and key.endswith("SYMBOL_DIR")]
    if CLI:
        candidates.append(Path(CLI).resolve().parent.parent / "share" / "kicad" / "symbols")
    candidates.append(Path("/usr/share/kicad/symbols"))
    for directory in candidates:
        paths = {name: directory / (name + ".kicad_sym") for name in LIBRARIES}
        if all(path.is_file() for path in paths.values()):
            return paths
    return None


def native_signature(path):
    root = ET.parse(path).getroot()
    return frozenset(
        (net.get("name"), frozenset((node.get("ref"), node.get("pin"))
                                    for node in net.findall("node")))
        for net in root.findall("./nets/net")
    )


class RecipeTests(unittest.TestCase):
    def test_portable_complete_endpoint_contract(self):
        recipe = read_recipe(RECIPE)
        self.assertEqual(recipe["libraries"], {})
        self.assertEqual({group["id"] for group in recipe["groups"]},
                         {"entry", "channel1", "channel2"})
        wanted = [frozenset((sid, str(pin)) for sid, pin in net["pins"])
                  for net in recipe["expected_nets"]]
        self.assertEqual(len(wanted), 8)
        self.assertEqual(sum(map(len, wanted)), 26)
        expected = {(f"q{channel}", pin) for channel in (1, 2) for pin in ("1", "2", "3")}
        expected |= {(f"{part}{channel}", pin) for channel in (1, 2)
                     for part in ("load", "gate", "bias", "clamp") for pin in ("1", "2")}
        expected |= {(f"c{channel}", pin) for channel in (1, 2) for pin in ("1", "2")}
        self.assertEqual(set().union(*wanted), expected)
        for channel in (1, 2):
            self.assertIn(frozenset({(f"q{channel}", "1"), (f"gate{channel}", "2"),
                                    (f"bias{channel}", "1"), (f"clamp{channel}", "1")}), wanted)
            self.assertIn(frozenset({(f"q{channel}", "3"), (f"load{channel}", "2")}), wanted)


class StandardLibraryExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = library_paths()
        if cls.paths is None:
            raise unittest.SkipTest("Set KICAD_SYMBOL_DIR or KICAD_CLI for standard symbol libraries")

    def test_field_sides_and_direct_endpoint_attachments(self):
        drawing = Drawing(read_recipe(RECIPE), RECIPE.parent, self.paths)
        source = loads(drawing.render())
        symbols = {child(item, "uuid")[1]: item for item in children(source, "symbol")}
        for sid, side in (("c1", "left"), ("c2", "right"), ("clamp1", "left"), ("q1", "right")):
            props = {prop[1]: prop for prop in children(symbols[drawing.ident("symbol", sid)], "property")}
            for name in ("Reference", "Value"):
                at = child(props[name], "at")
                x = Decimal(at[1])
                origin_x = drawing.origin(sid)[0]
                self.assertTrue(x < origin_x if side == "left" else x > origin_x)
                self.assertEqual(child(child(props[name], "effects"), "font")[1][1:], ["1.27", "1.27"])
        for channel in (1, 2):
            self.assertEqual(drawing.pin(f"load{channel}", "2"), drawing.pin(f"q{channel}", "3"))
            self.assertEqual(drawing.pin(f"vcc{channel}", "1"), drawing.pin(f"load{channel}", "1"))
        self.assertEqual(drawing.pin("entry_vcc", "1"), drawing.pin("c1", "1"))
        self.assertEqual(drawing.pin("entry_gnd", "1"), drawing.pin("c1", "2"))

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_cli_build_and_each_group_move_keep_native_connectivity(self):
        arguments = [arg for name, path in self.paths.items()
                     for arg in ("--library", f"{name}={path}")]

        def run(*args):
            result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(ROOT / "scripts" / "sch_author.py"),
                                     *map(str, args)], capture_output=True, text=True, encoding="utf-8", timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            baseline = output / "baseline"
            run("build", RECIPE, "--output", baseline, "--kicad-cli", CLI, *arguments)
            baseline_result = json.loads((baseline / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(baseline_result["electrical_intent"]["status"], "PASS")
            signature = native_signature(baseline / "netlist.xml")
            self.assertEqual(len(signature), 8)
            baseline_drawing = json.loads((baseline / "drawing.json").read_text(encoding="utf-8"))
            for group in ("entry", "channel1", "channel2"):
                moved_recipe = output / f"{group}.json"
                moved = output / group
                run("move-group", RECIPE, "--group", group, "--delta", "1.27", "-2.54",
                    "--output", moved_recipe, *arguments)
                run("build", moved_recipe, "--output", moved, "--kicad-cli", CLI)
                result = json.loads((moved / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["electrical_intent"]["status"], "PASS", group)
                self.assertEqual(native_signature(moved / "netlist.xml"), signature, group)
                moved_drawing = json.loads((moved / "drawing.json").read_text(encoding="utf-8"))
                members = set(baseline_drawing["groups"][group]["members"])
                for sid, before in baseline_drawing["symbols"].items():
                    after = moved_drawing["symbols"][sid]
                    self.assertEqual(before["uuid"], after["uuid"])
                    delta = [Decimal(a) - Decimal(b) for a, b in zip(after["origin_mm"], before["origin_mm"])]
                    self.assertEqual(delta, [Decimal("1.27"), Decimal("-2.54")] if sid in members else [0, 0])


if __name__ == "__main__":
    unittest.main()
