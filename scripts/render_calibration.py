#!/usr/bin/env python3
"""Build and native-render small calibration candidates, never user schematics.

Source generation and native net checks use only the Python standard library.
Optional PNG contact sheets require PyMuPDF. No candidate is user-approved.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET


ASSETS = Path(__file__).resolve().parents[1] / "references" / "calibration-candidates"
NAMESPACE = uuid.UUID("e5370fa1-f609-4cec-b7c7-7b50f260db30")
FONT = 1.27
CASES = (
    "caps_loose", "caps_compact", "caps_crowded",
    "resistor_short_inside", "resistor_long_inside", "resistor_long_outside",
)


def ident(name):
    return str(uuid.uuid5(NAMESPACE, name))


def quote(value):
    return json.dumps(str(value), ensure_ascii=True)


def extract_symbol(text, name):
    """Extract a balanced named symbol, respecting quoted parentheses."""
    match = re.search(r'\(symbol\s+' + re.escape(quote(name)) + r'(?=\s)', text)
    if match is None:
        raise ValueError(f"Missing embedded/library symbol {name}")
    depth, quoted, escaped = 0, False, False
    for offset in range(match.start(), len(text)):
        char = text[offset]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[match.start():offset + 1]
    raise ValueError(f"Unbalanced symbol {name}")


def definition(kind, library=None):
    if library:
        result = extract_symbol(Path(library).read_text(encoding="utf-8"), kind)
        return result.replace(f'(symbol "{kind}"', f'(symbol "Device:{kind}"', 1)
    case = "caps_compact" if kind == "C" else "resistor_short_inside"
    source = ASSETS / f"{case}.kicad_sch"
    return extract_symbol(source.read_text(encoding="utf-8"), f"Device:{kind}")


def field(name, text, x, y, angle=0, hidden=False, justify=None):
    hide = " (hide yes)" if hidden else ""
    align = f" (justify {justify})" if justify else ""
    return (f'(property {quote(name)} {quote(text)} (at {x} {y} {angle}){hide}'
            f' (effects (font (size {FONT} {FONT})){align}))')


def symbol(kind, ref, value, x, y, case, value_at=None, value_angle=0):
    if kind == "C":
        ref_at, default_value_at = (x + 0.7, y - 1.9), (x + 0.7, y + 2.0)
        align = "left"
    else:
        ref_at, default_value_at = (x + 2.032, y), (x, y)
        align = "left" if value_at else None
    value_at = value_at or default_value_at
    properties = [
        field("Reference", ref, *ref_at, hidden=kind == "R", justify=align),
        field("Value", value, *value_at, angle=value_angle, justify=align),
        field("Footprint", "", x, y, hidden=True),
        field("Datasheet", "", x, y, hidden=True),
    ]
    root_uuid = ident("root:" + case)
    return (f'(symbol (lib_id "Device:{kind}") (at {x} {y} 0) (unit 1)'
            f' (in_bom yes) (on_board yes) (dnp no) (uuid {quote(ident(ref))})\n'
            + "\n".join(properties)
            + f'\n(pin "1" (uuid {quote(ident(ref + ":1"))}))'
            + f' (pin "2" (uuid {quote(ident(ref + ":2"))}))'
            + f' (instances (project {quote(case)} (path {quote("/" + root_uuid)}'
            + f' (reference {quote(ref)}) (unit 1)))))')


def wire(a, b, key):
    if a == b:
        return ""
    return (f'(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]}))'
            f' (stroke (width 0) (type default)) (uuid {quote(ident(key))}))')


def label(name, x, y, justify="left bottom"):
    return (f'(label {quote(name)} (at {x} {y} 0)'
            f' (effects (font (size {FONT} {FONT})) (justify {justify}))'
            f' (uuid {quote(ident("label:" + name))}))')


def build(case, definitions):
    if case not in CASES:
        raise ValueError(f"Unknown case {case}")
    x, y = 30.48, 30.48
    elements = []
    if case.startswith("caps_"):
        pitch, half_bus = {
            "caps_loose": (17.78, 7.62),
            "caps_compact": (7.62, 3.81),
            "caps_crowded": (3.81, 3.81),
        }[case]
        xs = [x, round(x + pitch, 6)]
        top, bottom = round(y - half_bus, 6), round(y + half_bus, 6)
        for index, cx in enumerate(xs, 1):
            elements.append(symbol("C", f"C{index}", "100nF" if index == 1 else "1uF", cx, y, case))
            elements.append(wire((cx, top), (cx, round(y - 3.81, 6)), f"lead:{index}:top"))
            elements.append(wire((cx, round(y + 3.81, 6)), (cx, bottom), f"lead:{index}:bottom"))
        elements.extend([wire((xs[0], top), (xs[1], top), "top"),
                         wire((xs[0], bottom), (xs[1], bottom), "bottom"),
                         label("SUPPLY", x, top), label("RETURN", x, bottom, "right bottom")])
        embedded = definitions["C"]
    else:
        value = "10k" if case == "resistor_short_inside" else "17.4k"
        outside = case == "resistor_long_outside"
        elements.extend([
            symbol("R", "R1", value, x, y, case,
                   value_at=(x + 1.9, y) if outside else None,
                   value_angle=0 if outside else 90),
            label("TOP", x, round(y - 3.81, 6)),
            label("BOTTOM", x, round(y + 3.81, 6), "left top"),
        ])
        embedded = definitions["R"]
    return (f'(kicad_sch (version 20250114) (generator "eeschema")'
            f' (uuid {quote(ident("root:" + case))}) (paper "A4")\n'
            f'(lib_symbols {embedded})\n' + "\n".join(filter(None, elements))
            + '\n(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n')


def generate_sources(output, library=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    definitions = {kind: definition(kind, library) for kind in ("C", "R")}
    for case in CASES:
        (output / f"{case}.kicad_sch").write_text(build(case, definitions), encoding="utf-8")


def native_signature(path):
    root = ET.parse(path).getroot()
    components = {
        comp.get("ref"): (comp.findtext("value"), comp.findtext("footprint") or "",
                          comp.findtext("datasheet") or "",
                          comp.find("libsource").get("lib"), comp.find("libsource").get("part"))
        for comp in root.findall("./components/comp")
    }
    nets = {net.get("name"): sorted((pin.get("ref"), pin.get("pin")) for pin in net.findall("node"))
            for net in root.findall("./nets/net")}
    return {"components": components, "nets": nets}


def expected_signature(case):
    if case.startswith("caps_"):
        return {
            "components": {"C1": ("100nF", "", "", "Device", "C"),
                           "C2": ("1uF", "", "", "Device", "C")},
            "nets": {"/SUPPLY": [("C1", "1"), ("C2", "1")],
                     "/RETURN": [("C1", "2"), ("C2", "2")]},
        }
    return {
        "components": {"R1": ("10k" if case == "resistor_short_inside" else "17.4k",
                              "", "", "Device", "R")},
        "nets": {"/TOP": [("R1", "1")], "/BOTTOM": [("R1", "2")]},
    }


def run_cli(cli, args):
    result = subprocess.run([str(cli), *map(str, args)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise RuntimeError(f"KiCad failed ({result.returncode}): {result.stderr or result.stdout}")
    return result.stdout.strip()


def render_panels(output, native_dir):
    import pymupdf
    panels = [
        ("capacitor-spacing.png", CASES[:3],
         ["Loose candidate", "Compact candidate", "Intentional collision"],
         (20, 18, 61, 44), "Same parts, values and nets. Aesthetic approval pending."),
        ("resistor-fields.png", CASES[3:],
         ["Short value inside", "Long value hits border", "Same long value outside"],
         (24, 23, 43, 39),
         "Normal 1.27 mm fields; long-value pair electrically equal. Aesthetic approval pending."),
    ]
    for filename, cases, titles, crop, footer in panels:
        doc = pymupdf.open()
        page = doc.new_page(width=870, height=270)
        clip = pymupdf.Rect(*(value * 72 / 25.4 for value in crop))
        for index, (case, title) in enumerate(zip(cases, titles)):
            left = index * 290
            page.insert_text((left + 12, 22), title, fontsize=12)
            with pymupdf.open(native_dir / f"{case}.pdf") as native:
                page.show_pdf_page(pymupdf.Rect(left + 8, 32, left + 282, 230), native, 0, clip=clip)
        page.insert_text((12, 253), footer, fontsize=10)
        page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(output / filename)
        doc.close()


def evaluate(output, cli, png=False):
    output = Path(output)
    with tempfile.TemporaryDirectory(prefix="kicad-calibration-") as temp:
        native_dir = Path(temp)
        signatures = {}
        for case in CASES:
            source = output / f"{case}.kicad_sch"
            netlist = native_dir / f"{case}.xml"
            run_cli(cli, ["sch", "export", "netlist", "--format", "kicadxml", "-o", netlist, source])
            signatures[case] = native_signature(netlist)
            if signatures[case] != expected_signature(case):
                raise ValueError(f"{case}: native parts or endpoints differ from the fixture specification")
            if png:
                run_cli(cli, ["sch", "export", "pdf", "--exclude-drawing-sheet", "-o",
                              native_dir / f"{case}.pdf", source])
        cap_equal = all(signatures[case] == signatures[CASES[0]] for case in CASES[:3])
        long_equal = signatures[CASES[4]] == signatures[CASES[5]]
        if not cap_equal or not long_equal:
            raise ValueError("Calibration layout variants changed native components or net endpoints")
        if png:
            render_panels(output, native_dir)
    result = {
        "schema": 1,
        "personal_style_approval": "PENDING_USER_APPROVAL",
        "kicad_version": run_cli(cli, ["version"]),
        "font_mm": FONT,
        "all_native_signatures_match_expected": True,
        "caps_native_parts_and_nets_equal": cap_equal,
        "caps_expected_nets": signatures[CASES[0]]["nets"],
        "long_resistor_native_parts_and_nets_equal": long_equal,
        "resistor_expected_nets": signatures[CASES[4]]["nets"],
        "visual_checks": "NOT_AUTOMATED: view the current native renders; no glyph-fit or compactness score",
        "png_panels_generated": png,
        "panel_sha256": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                         for name in ("capacitor-spacing.png", "resistor-fields.png")} if png else {},
        "source_sha256": {case: hashlib.sha256((output / f"{case}.kicad_sch").read_bytes()).hexdigest()
                          for case in CASES},
    }
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Dedicated generated-candidate directory")
    parser.add_argument("--kicad-cli", default=shutil.which("kicad-cli"), help="Native KiCad CLI executable")
    parser.add_argument("--library", type=Path, help="Optional Device.kicad_sym; default reuses bundled definitions")
    parser.add_argument("--png", action="store_true", help="Render native contact sheets; requires PyMuPDF")
    args = parser.parse_args()
    if not args.kicad_cli:
        parser.error("Provide --kicad-cli or add kicad-cli to PATH")
    generate_sources(args.output, args.library)
    print(json.dumps(evaluate(args.output, args.kicad_cli, args.png), indent=2))


if __name__ == "__main__":
    main()
