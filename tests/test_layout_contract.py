"""Behavioral tests for the optional ownership and port contract."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from layout_contract import validate
from style_probe import parse_sexpr


SOURCE = '''(kicad_sch (uuid "root")
 (lib_symbols (symbol "Device:Fixture"
   (symbol "Fixture_1_1" (pin passive line (at -2.54 0 0) (length 2.54) (number "1") (name "A")))
   (symbol "Fixture_2_1" (pin passive line (at 0 2.54 270) (length 2.54) (number "2") (name "B")))))
 (symbol (lib_id "Device:Fixture") (at 10.16 10.16 0) (unit 1) (uuid "u1a")
   (property "Reference" "U1") (property "Value" "Fixture"))
 (symbol (lib_id "Device:Fixture") (at 20.32 20.32 0) (unit 2) (uuid "u1b")
   (property "Reference" "U1") (property "Value" "Fixture"))
 (symbol (lib_id "Device:Fixture") (at 30.48 10.16 0) (unit 1) (uuid "r1")
   (property "Reference" "R1") (property "Value" "10k"))
 (global_label "ENABLE" (at 35.56 10.16 0) (shape output) (uuid "enable")))'''


def contract():
    return {"version": 1, "scope": "sheet",
            "cores": [{"id": "control", "members": [{"ref": "U1", "unit": 1}, {"ref": "U1", "unit": 2}]}],
            "support_groups": [{"id": "pullup", "members": [{"ref": "R1"}], "serves": "control",
                                "serves_pins": [{"ref": "U1", "unit": 1, "pin": "1"}],
                                "physical_constraints": ["Preserve the existing local support assignment"]}],
            "read_chains": [["pullup", "control"]],
            "keep_together": [{"id": "input", "members": [{"ref": "U1", "unit": 1}, {"ref": "R1"}]}],
            "ports": [{"id": "enable", "owner": "control", "anchor": {"uuid": "enable"},
                       "net": "ENABLE", "role": "output", "connection": "label", "direction": "right",
                       "origin_mm": ["0", "0"], "local_mm": ["35.56", "10.16"], "grid_mm": "1.27"}]}


class ContractTests(unittest.TestCase):
    def check(self, doc=None, source=SOURCE):
        return validate(doc if doc is not None else contract(), parse_sexpr(source))

    def codes(self, doc, source=SOURCE):
        return {f["code"] for f in self.check(doc, source)["findings"]}

    def test_consistent_multiu_unit_support_assignment(self):
        report = self.check()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["assigned_units"], 3)
        self.assertEqual(report["ports"][0]["actual_mm"], ["35.56", "10.16"])

    def test_omitted_unit_is_ambiguous_for_multiu_unit(self):
        doc = contract()
        doc["cores"][0]["members"][0].pop("unit")
        self.assertIn("ambiguous_member", self.codes(doc))

    def test_missing_member_and_duplicate_owner(self):
        doc = contract()
        doc["cores"][0]["members"] += [{"ref": "R404"}, {"ref": "R1"}]
        codes = self.codes(doc)
        self.assertTrue({"missing_member", "duplicate_owner"}.issubset(codes))

    def test_sheet_scope_requires_all_units_listed_scope_does_not(self):
        doc = contract()
        doc["cores"][0]["members"].pop()
        self.assertIn("unassigned_unit", self.codes(doc))
        doc["scope"] = "listed"
        self.assertEqual(self.check(doc)["status"], "PASS")

    def test_dangling_support_chain_and_unknown_pin(self):
        doc = contract()
        doc["support_groups"][0]["serves"] = "missing"
        doc["support_groups"][0]["serves_pins"][0]["pin"] = "404"
        doc["read_chains"] = [["pullup", "missing"]]
        codes = self.codes(doc)
        self.assertTrue({"missing_service_core", "service_pin_wrong_owner",
                         "service_pin_not_resolved", "unknown_chain_member"}.issubset(codes))

    def test_cross_owner_group_requires_declared_reason(self):
        doc = contract()
        doc["cores"][0]["members"].pop()
        doc["cores"].append({"id": "other", "members": [{"ref": "U1", "unit": 2}]})
        doc["keep_together"][0]["members"].append({"ref": "U1", "unit": 2})
        self.assertIn("mixed_keep_ownership", self.codes(doc))
        doc["keep_together"][0]["cross_owner_reason"] = "Shared test interface intentionally crosses cores"
        self.assertEqual(self.check(doc)["status"], "PASS")

    def test_explicit_grid_does_not_reject_nonstandard_grid(self):
        doc = contract()
        doc["ports"][0]["origin_mm"] = ["35.06", "9.66"]
        doc["ports"][0]["local_mm"] = ["0.5", "0.5"]
        doc["ports"][0]["grid_mm"] = "0.5"
        self.assertEqual(self.check(doc)["status"], "PASS")
        doc["ports"][0]["grid_mm"] = "1.27"
        self.assertIn("declared_grid_mismatch", self.codes(doc))

    def test_label_position_text_role_diffs(self):
        doc = contract()
        port = doc["ports"][0]
        port.update({"net": "OTHER", "role": "input", "local_mm": ["34.29", "10.16"]})
        self.assertTrue({"label_net_mismatch", "label_role_mismatch", "port_position_mismatch"}.issubset(self.codes(doc)))

    def test_invalid_enum_and_coordinates(self):
        doc = contract()
        doc["ports"][0].update({"role": "source", "local_mm": ["NaN", "0"]})
        self.assertTrue({"invalid_port_role", "invalid_port_coordinates"}.issubset(self.codes(doc)))

    def test_hierarchy_is_unverified_not_invented(self):
        doc = contract()
        doc["scope"] = "listed"
        doc["cores"][0]["members"][1]["sheet_path"] = "/child"
        self.assertEqual(self.check(doc)["status"], "UNVERIFIED")

    def test_unknown_key_is_unverified(self):
        doc = contract()
        doc["portz"] = []
        self.assertEqual(self.check(doc)["status"], "UNVERIFIED")

    def test_missing_core_and_duplicate_keep_id(self):
        self.assertIn("missing_cores", self.codes({"version": 1}))
        doc = contract()
        doc["keep_together"] += [doc["keep_together"][0]]
        self.assertIn("invalid_or_duplicate_keep_id", self.codes(doc))

    def test_label_anchor_cannot_claim_wire_interface(self):
        doc = contract()
        doc["ports"][0]["connection"] = "wire"
        self.assertIn("label_anchor_connection_mismatch", self.codes(doc))

    def test_pin_endpoint_and_owner(self):
        doc = contract()
        doc["ports"][0].update({"anchor": {"ref": "U1", "unit": 1, "pin": "1"},
                                "connection": "wire", "local_mm": ["7.62", "10.16"]})
        self.assertEqual(self.check(doc)["status"], "PASS")
        doc["ports"][0]["local_mm"] = ["10.16", "10.16"]
        self.assertIn("port_position_mismatch", self.codes(doc))
        doc["ports"][0]["owner"] = "pullup"
        self.assertIn("port_anchor_wrong_owner", self.codes(doc))

    def test_output_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text("{}", encoding="utf-8")
            run = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "scripts/layout_contract.py"),
                                  str(path), "missing.kicad_sch", "--output", str(path)], capture_output=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(path.read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
