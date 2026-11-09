"""S-expression value preservation and bounded symbol inheritance regressions."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sch_author_io import Atom, SymbolLibrary, child, children, dumps, loads


class SexprTests(unittest.TestCase):
    def test_quoted_and_numeric_tokens_stay_distinct(self):
        tree = loads('(root -0.00 1e-04 yes "yes" "123" "")')
        self.assertEqual(tree, ["root", "-0.00", "1e-04", "yes", "yes", "123", ""])
        self.assertTrue(all(isinstance(item, Atom) for item in tree[:4]))
        self.assertTrue(all(type(item) is str for item in tree[4:]))
        self.assertEqual(dumps(tree), '(root -0.00 1e-04 yes "yes" "123" "")\n')

    def test_string_escaping(self):
        tree = loads(r'(root "a\"b" "C:\\library\\part" "line\nnext\tcell\rend")')
        self.assertEqual(tree[1:], ['a"b', "C:\\library\\part", "line\nnext\tcell\rend"])
        self.assertEqual(loads(dumps(tree)), tree)

    def test_unknown_escape_keeps_data(self):
        tree = loads(r'(root "unknown\q")')
        self.assertEqual(tree[1], "unknown\\q")
        self.assertEqual(loads(dumps(tree)), tree)

    def test_native_control_and_byte_escapes(self):
        tree = loads(r'(root "\a\b\f\n\r\t\v" "\x41\101" "\xc3\xa9" "\303\251" "\xz")')
        self.assertEqual(tree[1:], ["\a\b\f\n\r\t\v", "AA", "\u00e9", "\u00e9", "xz"])
        self.assertEqual(loads(dumps(tree)), tree)
        self.assertEqual(loads(dumps(["\x00\x01f"])), ["\x00\x01f"])
        with self.assertRaisesRegex(ValueError, "UTF-8 byte escape"):
            loads(r'(root "\xff")')

    def test_nested_unknown_nodes_round_trip(self):
        tree = loads('(future (extension "0001" (opaque -0.25 (new_flag yes))) (empty) ())')
        recovered = loads(dumps(tree))
        self.assertEqual(recovered, tree)
        self.assertIsInstance(recovered[1][2][1], Atom)
        self.assertIs(type(recovered[1][1]), str)

    def test_unicode_value_is_preserved(self):
        tree = [Atom("root"), "\u7535\u963b", [Atom("value"), "\u03a9"]]
        self.assertEqual(loads(dumps(tree)), tree)

    def test_bom_and_empty_list(self):
        self.assertEqual(loads("\ufeff ( ) \n"), [])
        self.assertEqual(loads(dumps([])), [])

    def test_native_whole_line_comments(self):
        tree = loads('\ufeff # header\n(root #inline ;ordinary\n  # ignored ( " )\n (node 1))\n# final')
        self.assertEqual(tree, ["root", "#inline", ";ordinary", ["node", "1"]])
        self.assertEqual(loads(dumps(tree)), tree)
        with self.assertRaises(ValueError):
            loads("(root) # inline comment is not supported by native KiCad")

    def test_invalid_input_is_rejected(self):
        for source in ("", "root", "(root", "(root))", "(root) (other)", '(root "open)',
                       '(root "trailing\\', '(root bare"quoted")', '(root "quoted"bare)'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                loads(source)

    def test_dump_rejects_ambiguous_scalars(self):
        for token in (Atom(""), Atom("has space"), Atom("a(b"), Atom('a"b')):
            with self.subTest(token=token), self.assertRaises(ValueError):
                dumps([token])
        with self.assertRaises(TypeError):
            dumps([Atom("at"), 1.0])
        with self.assertRaises(TypeError):
            dumps("root")

    def test_child_helpers_are_immediate_and_default_aware(self):
        tree = loads('(root (node 1) (wrapper (node 2)) (node 3))')
        self.assertEqual(children(tree, "node"), [["node", "1"], ["node", "3"]])
        self.assertIs(child(tree, "node"), tree[1])
        self.assertIsNone(child(tree, "missing"))
        marker = []
        self.assertIs(child(tree, "missing", marker), marker)


BASE = '''
(symbol "Base"
  (pin_names (offset 0) hide)
  (pin_numbers hide)
  (in_bom yes)
  (on_board yes)
  (property "Reference" "Q" (at 0 5 0))
  (property "Value" "Base" (at 0 2 0))
  (property "Footprint" "Package:Base" (at 0 0 0))
  (property "Datasheet" "base.pdf" (at 0 0 0))
  (property "Custom" "inherited" (at 0 0 0))
  (vendor_extension (opaque "untouched"))
  (symbol "Base_0_1"
    (rectangle (start -1 1) (end 1 -1) (stroke (width 0.254) (type default))))
  (symbol "Base_1_1"
    (pin input line (at -2 0 0) (length 1) (name "G") (number "1"))
    (pin passive line (at 0 2 270) (length 1) (name "D") (number "3")))
  (symbol "Base_2_1"
    (pin passive line (at 2 0 180) (length 1) (name "S") (number "2"))))
'''


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "Test.kicad_sym"

    def library(self, content, nickname="Test"):
        self.path.write_text('(kicad_symbol_lib (version 20251024) ' + content + ')', encoding="utf-8")
        return SymbolLibrary({nickname: self.path})

    def test_base_keeps_properties_unknowns_graphics_and_pins(self):
        source = loads(BASE)
        result = self.library(BASE).load("Test:Base")
        self.assertEqual(result[1], "Test:Base")
        self.assertEqual(result[2:], source[2:])
        self.assertEqual(loads(dumps(result)), result)

    def test_derived_fields_override_but_root_metadata_is_kept(self):
        library = self.library(BASE + '''
          (symbol "Actual" (extends "Base")
            (pin_names (offset 0.5))
            (in_bom no)
            (property "Reference" "T" (at 5 0 0))
            (property "Value" "Actual" (at 5 2 0))
            (property "Footprint" "Package:Actual" (at 5 4 0))
            (property "Datasheet" "actual.pdf" (at 5 6 0))
            (property "New" "added" (at 0 0 0)))
        ''')
        result = library.load("Test:Actual")
        props = {item[1]: item for item in children(result, "property")}
        self.assertEqual(props["Reference"][2], "T")
        self.assertEqual(props["Value"][2], "Actual")
        self.assertEqual(child(props["Value"], "at")[1:], ["5", "2", "0"])
        self.assertEqual(props["Footprint"][2], "Package:Actual")
        self.assertEqual(props["Datasheet"][2], "actual.pdf")
        self.assertEqual(props["Custom"][2], "inherited")
        self.assertEqual(props["New"][2], "added")
        self.assertEqual(child(result, "in_bom"), ["in_bom", "yes"])
        self.assertEqual(child(result, "pin_names"), ["pin_names", ["offset", "0"], "hide"])
        self.assertEqual(child(result, "pin_numbers"), ["pin_numbers", "hide"])
        self.assertEqual(child(result, "vendor_extension"), ["vendor_extension", ["opaque", "untouched"]])
        self.assertIsNone(child(result, "extends"))
        self.assertEqual([group[1] for group in children(result, "symbol")],
                         ["Actual_0_1", "Actual_1_1", "Actual_2_1"])
        self.assertEqual(sum(len(children(group, "pin")) for group in children(result, "symbol")), 3)

    def test_multilevel_inheritance_renames_all_groups(self):
        library = self.library(BASE + '''
          (symbol "Middle" (extends "Base") (property "Value" "Middle"))
          (symbol "Leaf_With_Underscores" (extends "Middle")
            (property "Value" "Leaf_With_Underscores") (on_board no))
        ''')
        result = library.load("Test:Leaf_With_Underscores")
        self.assertEqual([item[1] for item in children(result, "symbol")],
                         ["Leaf_With_Underscores_0_1", "Leaf_With_Underscores_1_1", "Leaf_With_Underscores_2_1"])
        self.assertEqual(child(result, "on_board"), ["on_board", "yes"])
        self.assertEqual(child(library.load("Test:Base"), "on_board"), ["on_board", "yes"])

    def test_pin_visibility_and_other_metadata_come_from_root(self):
        library = self.library('''
          (symbol "Base"
            (pin_names (offset 0.5) (hide yes)) (pin_numbers (hide yes))
            (power global) (body_styles "Root")
            (duplicate_pin_numbers_are_jumpers yes) (jumper_pin_groups (group "1" "2"))
            (embedded_fonts yes) (property "ki_locked" ""))
          (symbol "Middle" (extends "Base")
            (pin_names (offset 15) (hide no)) (pin_numbers (hide no))
            (power local) (body_styles "Middle")
            (duplicate_pin_numbers_are_jumpers no) (jumper_pin_groups (group "3" "4"))
            (embedded_fonts no))
          (symbol "Leaf" (extends "Middle")
            (pin_names (offset 25) (hide no)) (pin_numbers (hide no)))
        ''')
        base = library.load("Test:Base")
        for name in ("Middle", "Leaf"):
            result = library.load("Test:" + name)
            for key in ("pin_names", "pin_numbers", "power", "body_styles",
                        "duplicate_pin_numbers_are_jumpers", "jumper_pin_groups", "embedded_fonts"):
                with self.subTest(name=name, key=key):
                    self.assertEqual(child(result, key), child(base, key))

    def test_child_metadata_does_not_create_absent_root_settings(self):
        library = self.library('''
          (symbol "Base")
          (symbol "Derived" (extends "Base")
            (pin_numbers (hide yes)) (power global) (embedded_fonts yes)
            (property "ki_locked" ""))
        ''')
        result = library.load("Test:Derived")
        for key in ("pin_numbers", "power", "embedded_fonts", "property"):
            self.assertIsNone(child(result, key))

    def test_exclusion_flags_use_raw_immediate_parent_values_and_defaults(self):
        defaults = {"exclude_from_sim": "no", "in_bom": "yes", "on_board": "yes", "in_pos_files": "yes"}
        for key, default in defaults.items():
            opposite = "no" if default == "yes" else "yes"
            library = self.library(f'''
              (symbol "Base" ({key} {opposite}))
              (symbol "Middle" (extends "Base"))
              (symbol "Leaf" (extends "Middle") ({key} {opposite}))
              (symbol "Explicit" (extends "Base") ({key} {default}))
              (symbol "Tip" (extends "Explicit") ({key} {opposite}))
            ''')
            with self.subTest(key=key):
                self.assertEqual(child(library.load("Test:Middle"), key), [key, opposite])
                self.assertEqual(child(library.load("Test:Leaf"), key), [key, default])
                self.assertEqual(child(library.load("Test:Explicit"), key), [key, opposite])
                self.assertEqual(child(library.load("Test:Tip"), key), [key, default])

    def test_unknown_derived_metadata_is_explicitly_rejected(self):
        for parent_metadata in ("", '(vendor_extension (opaque "parent"))'):
            library = self.library(f'''
              (symbol "Base" {parent_metadata})
              (symbol "Derived" (extends "Base") (vendor_extension (opaque "child")))
            ''')
            with self.subTest(parent_metadata=parent_metadata), self.assertRaisesRegex(ValueError, "Unsupported inherited metadata"):
                library.load("Test:Derived")

    def test_empty_mandatory_fields_keep_entire_parent_field(self):
        library = self.library(BASE + '''
          (symbol "Empty" (extends "Base")
            (property "Value" "" (at 99 99 90))
            (property "Footprint" "" (at 99 99 90))
            (property "Custom" "" (at 99 99 90)))
        ''')
        source = {item[1]: item for item in children(library.load("Test:Base"), "property")}
        result = {item[1]: item for item in children(library.load("Test:Empty"), "property")}
        self.assertEqual(result["Value"], source["Value"])
        self.assertEqual(result["Footprint"], source["Footprint"])
        self.assertEqual(result["Custom"][2:], ["", ["at", "99", "99", "90"]])

    def test_omitted_mandatory_fields_keep_entire_parent_field(self):
        parent = loads(BASE)
        parent.append(loads('''(property "Description" "Parent description"
          (at 7 8 90) (hide yes) (effects (font (size 1.1 1.1))))'''))
        fields = {item[1]: item for item in children(parent, "property")}
        mandatory = {"Reference", "Value", "Footprint", "Datasheet", "Description"}
        # Native LIB_SYMBOL constructs missing fields with empty text; Flatten
        # consequently retains the full parent field instead of default styling.
        for missing in [*({name} for name in sorted(mandatory)), mandatory]:
            derived = [Atom("symbol"), "Omitted", [Atom("extends"), "Base"]]
            derived.extend(field for name, field in fields.items() if name not in missing)
            library = self.library(dumps(parent) + dumps(derived))
            result = {item[1]: item for item in children(library.load("Test:Omitted"), "property")}
            with self.subTest(missing=sorted(missing)):
                for name in missing:
                    self.assertEqual(result[name], fields[name])

    def test_private_properties_keep_their_flag_and_merge_by_name(self):
        library = self.library('''
          (symbol "Base"
            (property private "First" "A") (property private "Second" "B")
            (property "private" "Ordinary field name"))
          (symbol "Derived" (extends "Base") (property private "Second" "Override"))
        ''')
        result = children(library.load("Test:Derived"), "property")
        self.assertEqual(result, [["property", "private", "First", "A"],
                                  ["property", "private", "Second", "Override"],
                                  ["property", "private", "Ordinary field name"]])
        self.assertIsInstance(result[0][1], Atom)
        self.assertIs(type(result[2][1]), str)

    def test_mandatory_field_names_match_case_insensitively(self):
        library = self.library(BASE + '(symbol "Derived" (extends "Base") (property "vAlUe" "Derived"))')
        fields = children(library.load("Test:Derived"), "property")
        self.assertEqual([item for item in fields if item[1].casefold() == "value"],
                         [["property", "Value", "Derived"]])

    def test_empty_keywords_and_filters_inherit(self):
        library = self.library('''
          (symbol "Base" (property "ki_keywords" "keyword") (property "ki_fp_filters" "SOT*"))
          (symbol "Derived" (extends "Base") (property "ki_keywords" "") (property "ki_fp_filters" ""))
        ''')
        self.assertEqual(children(library.load("Test:Derived"), "property"),
                         [["property", "ki_keywords", "keyword"], ["property", "ki_fp_filters", "SOT*"]])

    def test_returned_trees_are_independent_and_source_file_unchanged(self):
        library = self.library(BASE + '(symbol "Alias" (extends "Base") (property "Value" "Alias"))')
        before = self.path.read_bytes()
        result = library.load("Test:Alias")
        child(result, "property")[2] = "MUTATED"
        children(result, "symbol")[0][1] = "MUTATED_0_1"
        fresh = library.load("Test:Alias")
        self.assertEqual(child(fresh, "property")[2], "Q")
        self.assertEqual(children(fresh, "symbol")[0][1], "Alias_0_1")
        self.assertEqual(children(library.load("Test:Base"), "symbol")[0][1], "Base_0_1")
        self.assertEqual(self.path.read_bytes(), before)

    def test_same_symbol_names_in_different_libraries_stay_separate(self):
        self.library(BASE)
        second = Path(self.directory.name) / "Second.kicad_sym"
        second.write_text('(kicad_symbol_lib (symbol "Base" (property "Value" "Other")))', encoding="utf-8")
        library = SymbolLibrary({"Test": str(self.path), "Second": second})
        self.assertEqual(child(library.load("Second:Base"), "property")[2], "Other")
        self.assertEqual(child(library.load("Test:Base"), "property")[2], "Q")

    def test_unknown_library_and_symbol_have_clear_errors(self):
        library = self.library(BASE)
        with self.assertRaisesRegex(ValueError, "Unknown library"):
            library.load("Device:R")
        with self.assertRaisesRegex(ValueError, "Missing symbol Test:Absent"):
            library.load("Test:Absent")
        for identity in ("Base", ":Base", "Test:", "Test:Other:Base"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                library.load(identity)

    def test_missing_parent_reports_chain(self):
        library = self.library('(symbol "Leaf" (extends "Middle")) (symbol "Middle" (extends "Missing"))')
        with self.assertRaisesRegex(ValueError, "Leaf -> Middle -> Missing"):
            library.load("Test:Leaf")

    def test_cycles_report_chain(self):
        for content, chain in ((('(symbol "A" (extends "B")) (symbol "B" (extends "A"))'), "A -> B -> A"),
                               ('(symbol "A" (extends "A"))', "A -> A")):
            with self.subTest(chain=chain):
                library = self.library(content)
                with self.assertRaisesRegex(ValueError, chain):
                    library.load("Test:A")

    def test_duplicate_symbols_and_properties_are_rejected(self):
        cases = [('(symbol "X") (symbol "X")', "Duplicate symbol"),
                 ('(symbol "X" (property "Value" "One") (property "Value" "Two"))', "Duplicate property")]
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, message):
                self.library(content).load("Test:X")

    def test_unsupported_inheritance_has_explicit_errors(self):
        cases = [
            ('(symbol "X" (extends "Other:Base"))', "local symbol name"),
            ('(symbol "X" (extends "Base") (extends "Base"))', "Multiple extends"),
            ('(symbol "X" (extends "Base") (symbol "X_1_1"))', "graphic/unit overrides"),
            ('(symbol "X" (extends "Base") (pin passive line))', "graphic/unit overrides"),
            ('(symbol "X" (extends "Base") (custom one) (custom two))', "repeated inherited metadata"),
        ]
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, message):
                self.library(BASE + content).load("Test:X")

    def test_embedded_resource_merge_is_explicitly_unsupported(self):
        library = self.library('''
          (symbol "Base" (embedded_files (file "parent.bin")))
          (symbol "Derived" (extends "Base") (embedded_files (file "child.bin")))
        ''')
        with self.assertRaisesRegex(ValueError, "embedded_files merge"):
            library.load("Test:Derived")
        library = self.library('''
          (symbol "Base")
          (symbol "Middle" (extends "Base") (embedded_files (file "middle.bin")))
          (symbol "Leaf" (extends "Middle"))
        ''')
        with self.assertRaisesRegex(ValueError, "embedded_files merge"):
            library.load("Test:Leaf")

    def test_malformed_groups_are_rejected(self):
        cases = [('(symbol "X" (symbol "Wrong_0_1"))', "nested symbol name"),
                 ('(symbol "X" (symbol "X_1_1" (symbol "X_1_1")))', "inside unit/body"),
                 ('(symbol "X" (symbol "X_1_1") (symbol "X_1_1"))', "Duplicate unit/body")]
        for content, message in cases:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, message):
                self.library(content).load("Test:X")

    def test_non_library_file_is_rejected(self):
        self.path.write_text("(kicad_sch)", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "kicad_symbol_lib root"):
            SymbolLibrary({"Test": self.path}).load("Test:Base")


def standard_library_directory():
    candidates = [Path(value) for key, value in os.environ.items()
                  if key.startswith("KICAD") and key.endswith("SYMBOL_DIR")]
    candidates.extend([Path("D:/app/KiCad/10.0/share/kicad/symbols"), Path("/usr/share/kicad/symbols")])
    program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "KiCad"
    if program_files.is_dir():
        candidates.extend(program_files.glob("*/share/kicad/symbols"))
    return next((path for path in candidates if all((path / (name + ".kicad_sym")).is_file()
                                                   for name in ("Device", "power", "Transistor_FET"))), None)


class StandardLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = standard_library_directory()
        if directory is None:
            raise unittest.SkipTest("KiCad standard symbol libraries are unavailable")
        cls.paths = {name: directory / (name + ".kicad_sym") for name in ("Device", "power", "Transistor_FET")}
        cls.library = SymbolLibrary(cls.paths)

    def test_standard_resistors_capacitors_and_power_symbols(self):
        for identity in ("Device:R", "Device:C", "power:+5V", "power:GND", "power:VDD"):
            with self.subTest(identity=identity):
                symbol = self.library.load(identity)
                self.assertEqual(symbol[1], identity)
                self.assertIsNone(child(symbol, "extends"))
                self.assertTrue(children(symbol, "symbol"))
                props = {item[1]: item[2] for item in children(symbol, "property")}
                self.assertEqual(props["Value"], identity.split(":")[1])
                self.assertEqual(loads(dumps(symbol)), symbol)

    def test_actual_2n7002_inherits_graphics_and_its_own_identity(self):
        path = self.paths["Transistor_FET"]
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        symbol = self.library.load("Transistor_FET:2N7002")
        parent = self.library.load("Transistor_FET:Q_NMOS_GSD")
        props = {item[1]: item[2] for item in children(symbol, "property")}
        self.assertEqual(props["Reference"], "Q")
        self.assertEqual(props["Value"], "2N7002")
        self.assertEqual(props["Footprint"], "Package_TO_SOT_SMD:SOT-23")
        self.assertIsNone(child(symbol, "extends"))
        self.assertEqual([group[2:] for group in children(symbol, "symbol")],
                         [group[2:] for group in children(parent, "symbol")])
        groups = children(symbol, "symbol")
        self.assertTrue(all(group[1].startswith("2N7002_") for group in groups))
        pins = [pin for group in groups for pin in children(group, "pin")]
        self.assertEqual({child(pin, "number")[1] for pin in pins}, {"1", "2", "3"})
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


@unittest.skipUnless(os.environ.get("KICAD_CLI"), "KICAD_CLI is required for native SVG comparison")
class NativeInheritanceTests(unittest.TestCase):
    def test_flattened_resistor_matches_native_pin_display_inheritance(self):
        directory = standard_library_directory()
        if directory is None:
            self.skipTest("KiCad standard symbol libraries are unavailable")
        base = SymbolLibrary({"Device": directory / "Device.kicad_sym"}).load("Device:R")
        base[1] = "R"
        derived = loads('''(symbol "Child" (extends "R")
          (pin_numbers (hide no)) (pin_names (offset 20) (hide no)))''')
        # sym export svg draws original child fields, not Flatten's inherited
        # fields. Explicit fields isolate this comparison to graphics/pin metadata.
        derived.extend(loads(dumps(field)) for field in children(base, "property"))
        header = [Atom("kicad_symbol_lib"), [Atom("version"), Atom("20251024")],
                  [Atom("generator"), "test"]]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.kicad_sym"
            original.write_text(dumps(header + [base, derived]), encoding="utf-8")
            flattened = SymbolLibrary({"Test": original}).load("Test:Child")
            flattened[1] = "Child"
            flat = root / "flattened.kicad_sym"
            flat.write_text(dumps(header + [flattened]), encoding="utf-8")
            svgs = []
            for name, source in (("original", original), ("flattened", flat)):
                output = root / name
                process = subprocess.run([os.environ["KICAD_CLI"], "sym", "export", "svg", "--symbol", "Child",
                                          "--output", str(output), str(source)],
                                         capture_output=True, text=True, timeout=30, encoding="utf-8")
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                svg = (output / "Child_unit1.svg").read_text(encoding="utf-8")
                svgs.append(re.sub(r"<title>.*?</title>", "", svg))
            self.assertNotRegex(svgs[0], r">[12]</text>")
            self.assertEqual(svgs[1], svgs[0])


if __name__ == "__main__":
    unittest.main()
