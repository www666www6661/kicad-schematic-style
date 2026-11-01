#!/usr/bin/env python3
"""Freeze native evidence, dispatch a fresh reviewer, and validate its result.

The gate never edits a schematic. REVIEW_ACCEPTED is a scoped visual opinion,
not electrical approval. The packet is an input boundary, not an OS read jail.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import tomllib

import schematic_check as electrical
import style_probe


ROOT = Path(__file__).resolve().parents[1]
RULES = ("drawing-conventions.md", "functional-layout.md")
DIMENSIONS = ("functional_reading", "geometry", "compactness", "paper")
EXIT_CODES = {"AWAITING_REVIEW": 0, "REVIEW_ACCEPTED": 0,
              "CHANGES_REQUESTED": 1, "UNVERIFIED": 2, "STALE": 3}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def within(root, relative):
    path = (root / relative).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("Packet path escapes its root: " + str(relative))
    return path


def render(pdf, directory):
    import pymupdf
    directory.mkdir()
    pages = []
    with pymupdf.open(pdf) as document:
        for number, page in enumerate(document, 1):
            whole = directory / f"page-{number:03d}.png"
            zoom = min(1.5, 3200 / max(page.rect.width, page.rect.height))
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(whole)
            details = []
            mid_x, mid_y = page.rect.width / 2, page.rect.height / 2
            for index, rect in enumerate(((0, 0, mid_x + 24, mid_y + 24),
                                          (mid_x - 24, 0, page.rect.width, mid_y + 24),
                                          (0, mid_y - 24, mid_x + 24, page.rect.height),
                                          (mid_x - 24, mid_y - 24, page.rect.width, page.rect.height)), 1):
                clip = pymupdf.Rect(rect) & page.rect
                path = directory / f"page-{number:03d}-detail-{index}.png"
                scale = min(3, 4000 / max(clip.width, clip.height))
                page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).save(path)
                details.append({"path": "images/" + path.name, "clip_points": list(clip)})
            pages.append({"page": number, "full": "images/" + whole.name, "details": details,
                          "size_points": [page.rect.width, page.rect.height]})
    if not pages:
        raise ValueError("Native PDF has no pages")
    return pages


def prepare(source, requirements, output, cli, stage="final", scope="whole",
            affected_refs=(), before=None, dependencies=(), skill_root=ROOT,
            paper_candidate=None, fixed_paper=False):
    # Fail before creating evidence if a required local runtime is absent.
    import pymupdf  # noqa: F401
    source, requirements, cli = (Path(p).resolve() for p in (source, requirements, cli))
    if not source.is_file() or not requirements.is_file() or not cli.is_file():
        raise ValueError("Source, original requirements, and kicad-cli must be existing files")
    if not requirements.read_text(encoding="utf-8-sig").strip():
        raise ValueError("Original requirements must not be empty")
    if scope == "local" and not affected_refs:
        raise ValueError("Local review requires --affected-ref")
    if output:
        output = Path(output).resolve()
        output.mkdir(parents=True, exist_ok=False)
    else:
        output = Path(tempfile.mkdtemp(prefix="kicad-review-gate-"))
    snapshot = output / "snapshot"
    captured = electrical.capture(source, snapshot, cli, dependencies, pdf=True)
    manifest, _, gaps = electrical.load_snapshot(snapshot)
    if not captured["artifacts"].get("preview.pdf") or not (snapshot / "preview.pdf").is_file():
        raise ValueError("Fresh native PDF unavailable; evidence retained at " + str(output))

    packet = output / "packet"
    packet.mkdir()
    (packet / "source").mkdir()
    (packet / "rules").mkdir()
    originals = []
    def copy_input(original, target):
        shutil.copyfile(original, target)
        originals.append({"path": str(Path(original).resolve()), "sha256": sha(target)})
    copy_input(requirements, packet / "requirements.md")
    for rule in RULES:
        copy_input(skill_root / "references" / rule, packet / "rules" / rule)
    for resource in ("reviewer-prompt.md", "review-report.schema.json"):
        copy_input(skill_root / "references" / resource, output / resource)
    shutil.copyfile(snapshot / "preview.pdf", packet / "preview.pdf")
    sources, probes = [], []
    for item in manifest["inputs"]:
        if item["role"] != "schematic":
            continue
        name = "source/" + Path(item["stored"]).name
        shutil.copyfile(within(snapshot, item["stored"]), packet / name)
        sources.append({"path": name, "original_name": Path(item["original"]).name,
                        "sha256": item["sha256"], "root": item["original"] == str(source)})
        probes.append({"source": name, "report": style_probe.inspect(packet / name, details=True)})
    write_json(packet / "source-index.json", sources)
    write_json(packet / "probe.json", probes)
    write_json(packet / "scope.json", {"scope": scope, "affected_refs": list(affected_refs)})
    images = render(packet / "preview.pdf", packet / "images")
    write_json(packet / "images-index.json", images)

    candidate_images = []
    paper_check = None
    if paper_candidate:
        candidate = Path(paper_candidate).resolve()
        cm, _, candidate_gaps = electrical.load_snapshot(candidate)
        if candidate_gaps or not cm["artifacts"].get("preview.pdf"):
            raise ValueError("Paper candidate needs an intact snapshot with a fresh PDF")
        candidate_dir = packet / "paper-candidate"
        candidate_dir.mkdir()
        shutil.copyfile(candidate / "preview.pdf", candidate_dir / "preview.pdf")
        candidate_sources = []
        for item in cm["inputs"]:
            if item["role"] == "schematic":
                target = candidate_dir / Path(item["stored"]).name
                shutil.copyfile(within(candidate, item["stored"]), target)
                candidate_sources.append({"path": target.relative_to(packet).as_posix(),
                                          "sha256": item["sha256"]})
        candidate_images = render(candidate_dir / "preview.pdf", candidate_dir / "images")
        for page in candidate_images:
            page["full"] = "paper-candidate/" + page["full"]
            for detail in page["details"]:
                detail["path"] = "paper-candidate/" + detail["path"]
        paper_check = electrical.compare(snapshot, candidate)
        write_json(packet / "page_evidence.json", {
            "kind": "frozen_candidate_snapshot", "pdf": "paper-candidate/preview.pdf",
            "pdf_sha256": cm["artifacts"]["preview.pdf"], "sources": candidate_sources,
            "images": candidate_images, "comparison": paper_check,
            "note": "Inspect the candidate after initial independent observations. A different path may leave equivalence unverified; no author verdict is supplied."})

    small_paper = all(p["report"].get("paper") == ["A5"] for p in probes)
    exemption = "local_scope" if scope == "local" else "user_fixed_paper" if fixed_paper else "A5" if small_paper else None
    write_json(packet / "scope.json", {"scope": scope, "affected_refs": list(affected_refs),
                                       "paper_exemption": exemption})

    comparison = electrical.compare(Path(before), snapshot) if before else None
    write_json(output / "electrical-comparison.json", comparison)
    frozen = {}
    for directory in (snapshot, packet):
        for path in directory.rglob("*"):
            if path.is_file():
                frozen[path.relative_to(output).as_posix()] = sha(path)
    for name in ("reviewer-prompt.md", "review-report.schema.json", "electrical-comparison.json"):
        frozen[name] = sha(output / name)
    root_source = next(item for item in sources if item["root"])
    gate = {"schema": 1, "stage": stage, "scope": scope, "source": str(source),
            "affected_refs": list(affected_refs), "paper_exemption": exemption,
            "candidate_images": candidate_images, "paper_comparison": paper_check,
            "source_sha256": root_source["sha256"], "pdf_sha256": sha(packet / "preview.pdf"),
            "files": frozen, "originals": originals, "snapshot_gaps": gaps,
            "pages": images, "dependencies": [str(Path(p).resolve()) for p in dependencies]}
    gate["packet_id"] = hashlib.sha256(electrical.canonical(gate).encode()).hexdigest()
    template = (output / "reviewer-prompt.md").read_text(encoding="utf-8")
    replacements = {"PACKET_ROOT": str(packet), "PACKET_ID": gate["packet_id"],
                    "STAGE": stage, "SCOPE": scope, "SOURCE_SHA256": gate["source_sha256"],
                    "PDF_SHA256": gate["pdf_sha256"]}
    for key, value in replacements.items():
        template = template.replace("{{" + key + "}}", value)
    schema_text = (output / "review-report.schema.json").read_text(encoding="utf-8")
    inline = "\n\nFrozen UTF-8 task materials (exact copies of the named packet files):\n"
    for name in ("requirements.md", "scope.json", *("rules/" + rule for rule in RULES)):
        inline += "\n<packet_file path=" + json.dumps(name) + ">\n" + (packet / name).read_text(encoding="utf-8-sig") + "\n</packet_file>\n"
    prompt = template + inline + "\n\nReturn only a JSON object matching this schema:\n" + schema_text
    (output / "prompt.txt").write_text(prompt, encoding="utf-8")
    write_json(output / "dispatch.json", {"task_name": "schematic_review_" + gate["packet_id"][:10],
                                          "fork_turns": "none", "message": prompt})
    gate["files"]["prompt.txt"] = sha(output / "prompt.txt")
    gate["files"]["dispatch.json"] = sha(output / "dispatch.json")
    write_json(output / "gate.json", gate)
    result = {"status": "AWAITING_REVIEW", "output": str(output),
              "packet_id": gate["packet_id"], "dispatch": str(output / "dispatch.json"),
              "snapshot_gaps": gaps, "electrical_comparison": comparison,
              "note": "Preparation is not visual acceptance. Run a fresh reviewer, then finalize."}
    write_json(output / "gate-result.json", result)
    return result


def integrity(output):
    gate = read_json(output / "gate.json")
    if gate.get("schema") != 1:
        raise ValueError("Unsupported gate manifest")
    damaged, stale = [], []
    for name, expected in gate["files"].items():
        path = within(output, name)
        if not path.is_file() or sha(path) != expected:
            damaged.append(name)
    manifest, _, gaps = electrical.load_snapshot(output / "snapshot")
    for item in manifest["inputs"]:
        path = Path(item["original"])
        if not path.is_file() or sha(path) != item["sha256"]:
            stale.append(str(path))
    for item in gate["originals"]:
        path = Path(item["path"])
        if not path.is_file() or sha(path) != item["sha256"]:
            stale.append(str(path))
    cli = Path(manifest["kicad_cli"])
    if not cli.is_file() or sha(cli) != manifest["kicad_cli_sha256"]:
        stale.append(str(cli))
    # Also detect newly introduced project files, dependencies and environment.
    model, current_files = electrical.inspect_sources(gate["source"])
    roles, environment, current_gaps = electrical.collect_dependencies(
        Path(gate["source"]), cli, manifest["kicad_version"], model,
        current_files, gate["dependencies"])
    expected_inputs = {i["original"]: (i["sha256"], i["role"]) for i in manifest["inputs"]}
    actual_inputs = {name: (electrical.digest(data), roles[name]) for name, data in current_files.items()}
    if actual_inputs != expected_inputs or environment != manifest["environment"]:
        stale.append("project_dependency_set_or_environment")
    return gate, damaged, sorted(set(stale)), gaps + model["coverage_gaps"] + current_gaps


def validate(value, schema, location="report"):
    """Validate the bounded schema used by the bundled report, without a dependency."""
    kind = schema.get("type")
    valid = {"object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str, "integer": type(value) is int,
             "boolean": type(value) is bool, "number": type(value) in (int, float)}
    if kind not in valid or not valid[kind]:
        raise ValueError(location + ": invalid type")
    if "const" in schema and value != schema["const"]:
        raise ValueError(location + ": unexpected constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(location + ": invalid enum")
    if kind == "object":
        props = schema.get("properties", {})
        if set(schema.get("required", ())) - value.keys():
            raise ValueError(location + ": missing fields")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise ValueError(location + ": unexpected fields")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], location + "." + key)
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(location + ": incomplete array")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{location}[{index}]")
    if kind == "string" and len(value.strip()) < schema.get("minLength", 0):
        raise ValueError(location + ": empty text")
    if kind in ("integer", "number") and value < schema.get("minimum", value):
        raise ValueError(location + ": below minimum")


def finalize(output, report=None, reviewer_id=None):
    output = Path(output).resolve()
    gate, damaged, stale, gaps = integrity(output)
    result = {"status": "UNVERIFIED", "output": str(output), "packet_id": gate["packet_id"],
              "stage": gate["stage"], "scope": gate["scope"], "affected_refs": gate["affected_refs"],
              "damaged_inputs": damaged, "stale_inputs": stale,
              "snapshot_gaps": gaps, "review_issues": [],
              "electrical_comparison": read_json(output / "electrical-comparison.json"),
              "limitations": ["Visual verdict is model judgment, not proof of design correctness.",
                              "Image-view claims are reviewer attestations, not tool-use proof.",
                              "Fresh context and a packet are not filesystem read isolation.",
                              "No baseline means electrical equivalence remains unverified."]}
    if damaged or stale:
        result["status"] = "STALE" if stale else "UNVERIFIED"
        write_json(output / "gate-result.json", result)
        return result
    target = output / "review.json"
    receipt_path = output / "invocation.json"
    if report:
        if target.exists() or receipt_path.exists():
            raise ValueError("Review already submitted; use a fresh gate for a new review")
        if not reviewer_id:
            raise ValueError("Native subagent submission needs its actual --reviewer-id")
        data = read_json(report)
        validate(data, read_json(output / "review-report.schema.json"))
        write_json(target, data)
        write_json(receipt_path, {"backend": "native-subagent", "reviewer_id": reviewer_id,
                                 "packet_id": gate["packet_id"], "report_sha256": sha(target),
                                 "returncode": 0, "isolation": "Caller declares fork_turns=none; not enforced by this script."})
    if not target.is_file() or not receipt_path.is_file():
        result["review_issues"].append("No completed independent reviewer response")
    else:
        try:
            review, receipt = read_json(target), read_json(receipt_path)
            validate(review, read_json(output / "review-report.schema.json"))
            for key in ("packet_id", "stage", "source_sha256", "pdf_sha256"):
                if review[key] != gate[key]:
                    result["review_issues"].append("Review belongs to another " + key)
            if receipt.get("packet_id") != gate["packet_id"] or receipt.get("report_sha256") != sha(target) or receipt.get("returncode") != 0:
                result["review_issues"].append("Reviewer invocation failed or response changed")
            page_numbers = {p["page"] for p in gate["pages"]}
            if set(review["reviewed_pages"]) != page_numbers:
                result["review_issues"].append("Full-page review coverage is incomplete")
            images = set(review["viewed_images"])
            all_pages = gate["pages"] + gate["candidate_images"]
            allowed_images = {p["full"] for p in all_pages} | {d["path"] for p in all_pages for d in p["details"]}
            if images - allowed_images:
                result["review_issues"].append("Unknown image evidence")
            for page in gate["pages"]:
                details = {d["path"] for d in page["details"]}
                covered = details <= images if gate["scope"] == "whole" else bool(images & details)
                if page["full"] not in images or not covered:
                    result["review_issues"].append(f"Missing whole-page or detail observation: page {page['page']}")
            if review["isolation"]["author_conclusions_seen"]:
                result["review_issues"].append("Author conclusions contaminated the first review")
            if review["isolation"]["outside_packet_files_seen"]:
                result["review_issues"].append("Reviewer read task material outside the frozen packet")
            if not review["probe_considered"]:
                result["review_issues"].append("Geometric candidates not reconciled after independent observation")
            for finding in review["findings"]:
                if finding["page"] not in page_numbers:
                    result["review_issues"].append("Finding references an unknown page")
            required = DIMENSIONS if gate["stage"] == "final" else DIMENSIONS[:3]
            assessments = review["assessments"]
            if any(assessments[k]["status"] == "unverified" for k in required):
                result["review_issues"].append("Required visual assessment is unverified")
            if any(assessments[k]["status"] == "not_applicable" for k in DIMENSIONS[:3]):
                result["review_issues"].append("Functional, geometry and compactness checks cannot be skipped")
            if assessments["paper"]["status"] == "not_applicable" and not gate["paper_exemption"]:
                result["review_issues"].append("Paper exception is not applicable to this task")
            if gate["stage"] == "final" and not gate["paper_exemption"] and assessments["paper"]["status"] == "pass":
                if not gate["candidate_images"]:
                    result["review_issues"].append("Adjacent paper candidate evidence is missing")
                elif any(p["full"] not in images or not {d["path"] for d in p["details"]} <= images for p in gate["candidate_images"]):
                    result["review_issues"].append("Paper candidate visual coverage is incomplete")
                else:
                    comparable = ("identity", "properties_and_pin_definitions", "exported_component_properties",
                                  "network_endpoint_sets", "pin_semantics_and_nc", "labels_and_scopes")
                    dimensions = (gate["paper_comparison"] or {}).get("dimensions", {})
                    if any(dimensions.get(k, {}).get("status") != "PASS" for k in comparable):
                        result["review_issues"].append("Paper candidate component and network content is not verified equivalent")
            result["review"] = review
            result["invocation"] = receipt
            if not result["review_issues"] and (review["findings"] or any(assessments[k]["status"] == "fail" for k in required)):
                result["status"] = "CHANGES_REQUESTED"
            elif not result["review_issues"] and not gaps:
                result["status"] = "REVIEW_ACCEPTED"
        except (ValueError, KeyError, TypeError) as error:
            result["review_issues"].append(str(error))
    write_json(output / "gate-result.json", result)
    return result


def codex_command(explicit=None):
    candidate = explicit or shutil.which("codex.exe") or shutil.which("codex")
    if not candidate:
        raise ValueError("Codex CLI unavailable; use prepare + dispatch.json with a fresh native subagent")
    path = Path(candidate).resolve()
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        # Resolve the installed native binary; never execute a batch shell.
        package = path.parent / "node_modules" / "@openai" / "codex"
        binaries = list(package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
        if len(binaries) != 1:
            raise ValueError("Cannot resolve a unique native Codex executable; pass --codex")
        return [str(binaries[0])]
    return [str(path)]


def stop_process_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=30)


def reviewer_overrides():
    """Disable automatic context integrations for this process, retaining auth/model."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.is_file() else {}
    settings = ["project_doc_max_bytes=0", "mcp_servers={}"]
    plugins = config.get("plugins", {})
    if plugins:
        settings.append("plugins={" + ",".join(
            json.dumps(name) + "={enabled=false}" for name in plugins) + "}")
    skill_paths = set()
    for root in (Path.home() / ".agents" / "skills", codex_home / "skills"):
        if root.is_dir():
            skill_paths.update(str(p.resolve()) for p in root.rglob("SKILL.md"))
    for item in config.get("skills", {}).get("config", []):
        if item.get("path"):
            skill_paths.add(str(item["path"]))
    if skill_paths:
        settings.append("skills.config=[" + ",".join(
            "{path=" + json.dumps(path) + ",enabled=false}" for path in sorted(skill_paths)) + "]")
    return [argument for setting in settings for argument in ("-c", setting)]


def run_reviewer(output, codex=None, model=None, timeout=900):
    output = Path(output).resolve()
    gate, damaged, stale, _ = integrity(output)
    if damaged or stale:
        return finalize(output)
    if (output / "invocation.json").exists() or (output / "review.json").exists():
        raise ValueError("A reviewer was already invoked; use a fresh gate rather than resume")
    executable = codex_command(codex)
    help_run = subprocess.run(executable + ["exec", "--help"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
    for flag in ("--ephemeral", "--output-schema", "--sandbox", "--output-last-message"):
        if flag not in help_run.stdout:
            raise ValueError("Installed Codex CLI lacks " + flag + "; use the native subagent dispatch")
    command = executable + ["exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                            "-c", 'approval_policy="never"', "--cd", str(output / "packet"),
                            "--output-schema", str(output / "review-report.schema.json"),
                            "--output-last-message", str(output / "review.json"), "--json"]
    command.extend(reviewer_overrides())
    if model:
        command.extend(["--model", model])
    command.append("-")
    receipt = {"backend": "codex-exec", "packet_id": gate["packet_id"], "command": command,
               "returncode": None, "report_sha256": None,
               "isolation": "Fresh ephemeral exec, read-only command sandbox; user configuration and global instructions may still apply."}
    write_json(output / "invocation.json", receipt)
    try:
        with (output / "reviewer-events.jsonl").open("w", encoding="utf-8") as events, (output / "reviewer-stderr.txt").open("w", encoding="utf-8") as errors:
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            environment = dict(os.environ)
            for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID"):
                environment.pop(name, None)
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                       text=True, encoding="utf-8", errors="replace", cwd=output / "packet",
                                       env=environment, **options)
            try:
                process.communicate((output / "prompt.txt").read_text(encoding="utf-8"), timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                stop_process_tree(process)
                raise
            receipt["returncode"] = process.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        receipt["error"] = str(error)
    if (output / "review.json").is_file():
        receipt["report_sha256"] = sha(output / "review.json")
    write_json(output / "invocation.json", receipt)
    return finalize(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("source", type=Path)
        sub.add_argument("--requirements", type=Path, required=True)
        sub.add_argument("--kicad-cli", type=Path, required=True)
        sub.add_argument("--output", type=Path)
        sub.add_argument("--stage", choices=("layout", "final"), default="final")
        sub.add_argument("--scope", choices=("whole", "local"), default="whole")
        sub.add_argument("--affected-ref", action="append", default=[])
        sub.add_argument("--before", type=Path)
        sub.add_argument("--dependency", type=Path, action="append", default=[])
        sub.add_argument("--paper-candidate", type=Path, help="Existing frozen snapshot --pdf of the adjacent paper trial")
        sub.add_argument("--fixed-paper", action="store_true", help="Only when the user explicitly fixes the paper format")
    existing = commands.add_parser("review", help="Run a fresh CLI reviewer on an already prepared gate")
    existing.add_argument("output", type=Path)
    for sub in (commands.choices["run"], existing):
        sub.add_argument("--codex", help="Codex executable or recognized npm shim")
        sub.add_argument("--model", help="Omit to use the user's configured model")
        sub.add_argument("--timeout", type=int, default=900)
    finish = commands.add_parser("finalize")
    finish.add_argument("output", type=Path)
    finish.add_argument("--report", type=Path, help="JSON response from the fresh native subagent")
    finish.add_argument("--reviewer-id", help="Actual native subagent ID; not an author-supplied pass label")
    args = parser.parse_args(argv)
    try:
        if args.command in ("prepare", "run"):
            result = prepare(args.source, args.requirements, args.output, args.kicad_cli,
                             args.stage, args.scope, args.affected_ref, args.before, args.dependency,
                             paper_candidate=args.paper_candidate, fixed_paper=args.fixed_paper)
            if args.command == "run":
                print(json.dumps({"status": "AWAITING_REVIEW", "output": result["output"]}), flush=True)
                result = run_reviewer(result["output"], args.codex, args.model, args.timeout)
        elif args.command == "review":
            result = run_reviewer(args.output, args.codex, args.model, args.timeout)
        else:
            result = finalize(args.output, args.report, args.reviewer_id)
        summary = {k: result[k] for k in ("status", "output", "stage", "scope", "affected_refs", "review_issues", "stale_inputs", "damaged_inputs") if k in result}
        summary["findings"] = len(result.get("review", {}).get("findings", []))
        print(json.dumps(summary, ensure_ascii=False))
        return EXIT_CODES[result["status"]]
    except (OSError, ValueError, KeyError, TypeError, ImportError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"status": "UNVERIFIED", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
