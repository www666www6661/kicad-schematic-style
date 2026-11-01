"""Reproducibility and native endpoint checks for synthetic style candidates."""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import render_calibration as calibration


CLI = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")


class CalibrationTests(unittest.TestCase):
    def test_bundled_sources_reproduce(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            calibration.generate_sources(output)
            for case in calibration.CASES:
                name = f"{case}.kicad_sch"
                self.assertEqual((output / name).read_text(encoding="utf-8"),
                                 (calibration.ASSETS / name).read_text(encoding="utf-8"), case)

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_native_components_and_endpoint_sets(self):
        with tempfile.TemporaryDirectory() as temporary:
            calibration.generate_sources(temporary)
            result = calibration.evaluate(temporary, CLI)
            self.assertTrue(result["all_native_signatures_match_expected"])
            self.assertTrue(result["caps_native_parts_and_nets_equal"])
            self.assertTrue(result["long_resistor_native_parts_and_nets_equal"])
            self.assertEqual(result["personal_style_approval"], "PENDING_USER_APPROVAL")

    @unittest.skipUnless(CLI, "Set KICAD_CLI or put kicad-cli on PATH for native checks")
    def test_common_wiring_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            calibration.generate_sources(output)
            # All three layouts still match each other after this shared short.
            for case in calibration.CASES[:3]:
                path = output / f"{case}.kicad_sch"
                source = path.read_text(encoding="utf-8")
                path.write_text(source.replace('(label "SUPPLY"', '(label "RETURN"'),
                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fixture specification"):
                calibration.evaluate(output, CLI)


if __name__ == "__main__":
    unittest.main()
