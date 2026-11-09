"""TEMP-only review-gate regressions; no CLI, network, or live project calls."""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_gate as gate
import test_schematic_check as fixtures


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kicad-review-gate-tests-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "main.kicad_sch"
        self.source.write_text(fixtures.schematic(), encoding="utf-8")
        self.requirements = self.base / "requirements.md"
        self.requirements.write_text("Inspect the resistor connection and original fields. Keep the user-specified A4 sheet.\n", encoding="utf-8")
        self.cli = self.base / "kicad-cli.exe"
        self.cli.write_bytes(b"not executable: test double")
        self.skill = self.base / "skill"
        refs = self.skill / "references"
        refs.mkdir(parents=True)
        for name in (*gate.RULES, "reviewer-prompt.md", "review-report.schema.json"):
            shutil.copyfile(gate.ROOT / "references" / name, refs / name)
        (refs / "calibration.md").write_text("The author already approved this project.", encoding="utf-8")
        self.schema = gate.read_json(refs / "review-report.schema.json")
        self.output = self.base / "review"
        self.render_pages = (1, 2)
        self.capture_mock = patch.object(gate.electrical, "capture", side_effect=self.capture).start()
        self.addCleanup(patch.stopall)
        patch.object(gate.electrical, "collect_dependencies", side_effect=self.dependencies).start()
        patch.object(gate, "render", side_effect=self.render).start()
        patch.dict(sys.modules, {"pymupdf": Mock()}).start()

    def dependencies(self, source, cli, version, model, files, extra=()):
        roles = {name: "schematic" for name in files}
        project = source.with_suffix(".kicad_pro")
        paths = ([project] if project.exists() else []) + list(extra)
        for path in paths:
            path = Path(path).resolve()
            files[str(path)] = path.read_bytes()
            roles[str(path)] = "project_dependency"
        return roles, {}, []

    def capture(self, source, folder, cli, dependencies=(), pdf=False):
        folder.mkdir()
        (folder / "inputs").mkdir()
        model, files = gate.electrical.inspect_sources(source)
        roles, env, gaps = self.dependencies(source, cli, "10.0.5", model, files, dependencies)
        inputs = []
        for index, (name, data) in enumerate(sorted(files.items())):
            relative = f"inputs/{index:04d}{Path(name).suffix}"
            (folder / relative).write_bytes(data)
            inputs.append({"original": name, "stored": relative,
                           "sha256": gate.electrical.digest(data), "role": roles[name]})
        (folder / "netlist.xml").write_bytes(fixtures.fixture_netlist(model))
        gate.write_json(folder / "erc.json", fixtures.fixture_erc(model))
        gate.write_json(folder / "source-model.json", model)
        artifacts = ["netlist.xml", "erc.json", "source-model.json"]
        if pdf:
            (folder / "preview.pdf").write_bytes(b"%PDF-1.7\nfixture-native-output\n")
            artifacts.append("preview.pdf")
        manifest = {"schema": 1, "source": str(source), "capture_status": "CAPTURED",
                    "kicad_version": "10.0.5", "kicad_cli": str(cli),
                    "kicad_cli_sha256": gate.sha(cli), "inputs": inputs,
                    "environment": env, "coverage_gaps": gaps,
                    "artifacts": {name: gate.sha(folder / name) for name in artifacts}}
        gate.write_json(folder / "manifest.json", manifest)
        return manifest

    def render(self, pdf, directory):
        directory.mkdir()
        pages = []
        for page in self.render_pages:
            whole = f"images/page-{page:03d}.png"
            (directory / Path(whole).name).write_bytes(b"full-image-fixture" + bytes([page]))
            details = []
            for index in range(1, 5):
                detail = f"images/page-{page:03d}-detail-{index}.png"
                (directory / Path(detail).name).write_bytes(b"detail-image-fixture" + bytes([page, index]))
                details.append({"path": detail, "clip_points": [0, 0, 100, 100]})
            pages.append({"page": page, "full": whole, "details": details})
        return pages

    def prepare(self, stage="final", **kwargs):
        kwargs.setdefault("fixed_paper", True)
        return gate.prepare(self.source, self.requirements, self.output, self.cli,
                            stage=stage, skill_root=self.skill, **kwargs)

    def response(self):
        state = gate.read_json(self.output / "gate.json")
        report = {"schema_version": 2, "packet_id": state["packet_id"], "stage": state["stage"],
                "source_sha256": state["source_sha256"], "pdf_sha256": state["pdf_sha256"],
                "reviewed_pages": [p["page"] for p in state["pages"]],
                "viewed_images": [name for p in state["pages"] for name in
                                  [p["full"], *[d["path"] for d in p["details"]]]],
                "isolation": {"author_conclusions_seen": False, "outside_packet_files_seen": False,
                              "notes": "Only packet input viewed."},
                "assessments": {name: {"status": "pass", "evidence": "Observed original source and native image."}
                                for name in gate.DIMENSIONS},
                "space_checks": [{"page": p["page"], "level": level,
                                  "anchors": ["R1 support branch", "output connector"],
                                  "observation": "The largest gap keeps the output label clear of the support branch.",
                                  "disposition": "retained",
                                  "action_or_constraint": "Retain the gap to preserve label and wire clearance."}
                                 for p in state["pages"] for level in ("within_core", "between_cores")],
                "findings": [], "rechecks": [],
                "unverified": ["Electrical correctness is outside this visual review."],
                "probe_considered": True}
        if len(state["pages"]) > 1:
            report["space_checks"].append({
                "page": state["pages"][0]["page"], "level": "across_sheets",
                "anchors": ["page 1 input conditioning", "page 2 isolated output stage"],
                "observation": "The isolated output stage is on page 2 and crossing signals meet at labeled interfaces.",
                "disposition": "retained",
                "action_or_constraint": "Keep the isolation boundary readable and the crossing interface explicit."})
        return report

    def finding(self, finding_id="C1", category="compactness", page=1):
        return {"id": finding_id, "category": category, "page": page,
                "references": ["R1", "output connector"],
                "observation": "The output core leaves an unnecessary gap beside its source.",
                "action": "Move the output core toward the source while keeping labels legible."}

    def prepare_followup(self, **kwargs):
        self.prepare(**kwargs)
        report = self.response()
        report["findings"] = [self.finding()]
        report["assessments"]["compactness"]["status"] = "fail"
        self.assertEqual("CHANGES_REQUESTED", self.submit(report)["status"])
        previous = self.output
        self.output = self.base / "followup"
        self.prepare(previous_review=previous, **kwargs)
        return previous

    def submit(self, response=None):
        candidate = self.base / "subagent-response.json"
        gate.write_json(candidate, response or self.response())
        return gate.finalize(self.output, candidate, reviewer_id="fresh-reviewer-test-id")

    def runner(self, returncode=0, timeout=False, response=None):
        process = Mock(pid=7654321, returncode=returncode)
        def communicate(prompt, **kwargs):
            if timeout:
                raise subprocess.TimeoutExpired("fixture-codex.exe", kwargs["timeout"])
            if response is not False:
                gate.write_json(self.output / "review.json", response or self.response())
            return None, None
        process.communicate.side_effect = communicate
        with patch.object(gate, "codex_command", return_value=["fixture-codex.exe"]), \
                patch.object(gate, "reviewer_overrides", return_value=["-c", "project_doc_max_bytes=0"]), \
                patch.object(gate.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, stdout="--ephemeral --output-schema --sandbox --output-last-message")), \
                patch.object(gate.subprocess, "Popen", return_value=process) as run, \
                patch.object(gate, "stop_process_tree") as stop:
            result = gate.run_reviewer(self.output, timeout=5)
        self.last_process = process
        self.last_stop = stop
        return result, run.call_args_list

    def test_prepare_is_pending_not_acceptance_and_excludes_history(self):
        result = self.prepare()
        self.assertEqual("AWAITING_REVIEW", result["status"])
        self.assertEqual("UNVERIFIED", gate.finalize(self.output)["status"])
        copied = {p.name for p in (self.output / "packet" / "rules").iterdir()}
        self.assertEqual(set(gate.RULES), copied)
        dispatch = gate.read_json(self.output / "dispatch.json")
        self.assertEqual("none", dispatch["fork_turns"])
        self.assertNotIn("{{PACKET_ID}}", dispatch["message"])
        self.assertNotIn("The author already approved", dispatch["message"])

    def test_prompt_embeds_exact_utf8_task_inputs_without_author_history(self):
        original_requirements = "\u68c0\u67e5\u6811\u6781\u4fdd\u62a4\u548c\u53bb\u8026\u5f52\u5c5e\uff1b\u4fdd\u7559\u539f\u59cb\u4f4d\u53f7\u3002\n"
        original_rule = "\u7535\u6e90\u671d\u4e0a\uff0c\u5730\u671d\u4e0b\u3002\n"
        self.requirements.write_text(original_requirements, encoding="utf-8-sig")
        (self.skill / "references" / gate.RULES[0]).write_text(original_rule, encoding="utf-8-sig")
        self.prepare()
        prompt = (self.output / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn('<packet_file path="requirements.md">\n' + original_requirements + '\n</packet_file>', prompt)
        self.assertIn('<packet_file path="rules/' + gate.RULES[0] + '">\n' + original_rule + '\n</packet_file>', prompt)
        self.assertNotIn("\ufffd", prompt)
        self.assertNotIn("\ufeff", prompt)
        self.assertNotIn("The author already approved this project.", prompt)
        self.assertEqual(prompt, gate.read_json(self.output / "dispatch.json")["message"])

    def test_valid_review_is_scoped_acceptance_without_electrical_claim(self):
        self.prepare()
        result = self.submit()
        self.assertEqual("REVIEW_ACCEPTED", result["status"])
        self.assertTrue(all(item["disposition"] == "retained" for item in result["review"]["space_checks"]))
        self.assertEqual([], result["review"]["findings"])
        self.assertEqual([], result["review"]["rechecks"])
        self.assertIsNone(result["electrical_comparison"])
        self.assertTrue(any("equivalence" in item for item in result["limitations"]))

    def test_followup_freezes_only_original_report_after_live_source_and_rule_changes(self):
        self.prepare(stage="layout")
        report = self.response()
        report["findings"] = [self.finding()]
        report["assessments"]["compactness"]["status"] = "fail"
        self.submit(report)
        previous = self.output
        previous_state = gate.read_json(previous / "gate.json")
        previous_raw = (previous / "review.json").read_bytes()
        (previous / "author-repair-notes.md").write_text("The author says everything is fixed.", encoding="utf-8")
        history = {p: p.read_bytes() for p in previous.rglob("*") if p.is_file()}
        self.source.write_text(fixtures.schematic(value="2k"), encoding="utf-8")
        rule = self.skill / "references" / gate.RULES[0]
        rule.write_text(rule.read_text(encoding="utf-8") + "\nUpdated rule.\n", encoding="utf-8")
        self.output = self.base / "followup"
        self.prepare(previous_review=previous)
        state = gate.read_json(self.output / "gate.json")
        self.assertEqual(previous_state["packet_id"], state["previous_review"]["packet_id"])
        self.assertEqual(previous_state["source_sha256"], state["previous_review"]["source_sha256"])
        self.assertNotEqual(state["source_sha256"], state["previous_review"]["source_sha256"])
        self.assertEqual(report["findings"], state["previous_findings"])
        self.assertEqual(previous_raw, (self.output / "packet" / "previous-review.json").read_bytes())
        old_inputs = {p.relative_to(previous / "packet") for p in (previous / "packet").rglob("*") if p.is_file()}
        new_inputs = {p.relative_to(self.output / "packet") for p in (self.output / "packet").rglob("*") if p.is_file()}
        self.assertEqual(old_inputs | {Path("previous-review.json")}, new_inputs)
        self.assertTrue(all(previous not in Path(item["path"]).parents for item in state["originals"]))
        self.assertEqual(history, {p: p.read_bytes() for p in previous.rglob("*") if p.is_file()})
        (previous / "review.json").write_text("Old history changed after the new packet froze.", encoding="utf-8")
        report = self.response()
        report["rechecks"] = [{"finding_id": "C1", "status": "resolved",
                               "evidence": "The output core now shares a compact boundary with the source."}]
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])
        self.assertEqual(previous_raw, (self.output / "packet" / "previous-review.json").read_bytes())

    def test_previous_review_requires_completed_report_before_capture(self):
        self.prepare()
        previous = self.output
        self.output = self.base / "followup"
        calls = self.capture_mock.call_count
        with self.assertRaisesRegex(ValueError, "completed report"):
            self.prepare(previous_review=previous)
        self.assertFalse(self.output.exists())
        self.assertEqual(calls, self.capture_mock.call_count)

    def test_failed_first_review_without_findings_cannot_enter_second_round(self):
        self.prepare()
        report = self.response()
        report["assessments"]["compactness"]["status"] = "fail"
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])
        previous = self.output
        self.source.write_text(fixtures.schematic(value="2k"), encoding="utf-8")
        self.output = self.base / "followup"
        calls = self.capture_mock.call_count
        with self.assertRaisesRegex(ValueError, "Failed assessment needs a same-category finding: compactness"):
            self.prepare(previous_review=previous)
        self.assertFalse(self.output.exists())
        self.assertEqual(calls, self.capture_mock.call_count)

    def test_previous_frozen_evidence_and_manifest_tampering_are_rejected(self):
        previous = self.prepare_followup()
        for name in ("packet/preview.pdf", "packet/source-index.json", "review-report.schema.json",
                     "prompt.txt", "dispatch.json"):
            with self.subTest(name=name):
                path = previous / name
                original = path.read_bytes()
                path.write_bytes(original + b"\nchanged")
                with self.assertRaisesRegex(ValueError, "frozen evidence changed"):
                    gate.load_previous_review(previous, self.source, "whole", [])
                path.write_bytes(original)
        manifest_path = previous / "gate.json"
        original = manifest_path.read_bytes()
        for field in ("source", "scope", "affected_refs", "files"):
            with self.subTest(field=field):
                manifest = json.loads(original)
                if field == "files":
                    del manifest["files"]["packet/preview.pdf"]
                else:
                    manifest[field] = ["R2"] if field == "affected_refs" else "changed"
                gate.write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ValueError, "manifest changed"):
                    gate.load_previous_review(previous, self.source, "whole", [])
                manifest_path.write_bytes(original)

    def test_previous_report_and_invocation_must_match_each_other_and_packet(self):
        previous = self.prepare_followup()
        report_path, receipt_path = previous / "review.json", previous / "invocation.json"
        original_report, original_receipt = report_path.read_bytes(), receipt_path.read_bytes()
        report_path.write_bytes(original_report + b"\n")
        with self.assertRaisesRegex(ValueError, "report changed"):
            gate.load_previous_review(previous, self.source, "whole", [])
        report_path.write_bytes(original_report)
        for field in ("packet_id", "stage", "source_sha256", "pdf_sha256", "schema_version"):
            with self.subTest(report_field=field):
                report = json.loads(original_report)
                report[field] = "layout" if field == "stage" else 99 if field == "schema_version" else "f" * 64
                gate.write_json(report_path, report)
                receipt = json.loads(original_receipt)
                receipt["report_sha256"] = gate.sha(report_path)
                gate.write_json(receipt_path, receipt)
                with self.assertRaises(ValueError):
                    gate.load_previous_review(previous, self.source, "whole", [])
                report_path.write_bytes(original_report)
                receipt_path.write_bytes(original_receipt)
        for field, value in (("packet_id", "wrong"), ("report_sha256", "wrong"),
                             ("returncode", 1), ("returncode", None), ("returncode", False)):
            with self.subTest(receipt_field=field, value=value):
                receipt = json.loads(original_receipt)
                receipt[field] = value
                gate.write_json(receipt_path, receipt)
                with self.assertRaisesRegex(ValueError, "invocation failed or report changed"):
                    gate.load_previous_review(previous, self.source, "whole", [])
                receipt_path.write_bytes(original_receipt)

    def test_previous_review_rejects_other_source_scope_or_affected_refs(self):
        previous = self.prepare_followup(scope="local", affected_refs=["R1", "R2"])
        for source, scope, refs in ((self.base / "other.kicad_sch", "local", ["R1", "R2"]),
                                    (self.source, "whole", ["R1", "R2"]),
                                    (self.source, "local", ["R1"])):
            with self.subTest(source=source, scope=scope, refs=refs), self.assertRaisesRegex(ValueError, "another source or scope"):
                gate.load_previous_review(previous, source, scope, refs)
        raw, _, _ = gate.load_previous_review(previous, self.source, "local", ["R2", "R1", "R1"])
        self.assertEqual((previous / "review.json").read_bytes(), raw)

    def test_legacy_reports_without_pagination_can_carry_existing_findings_forward(self):
        schema_path = self.skill / "references" / "review-report.schema.json"
        current_schema = schema_path.read_bytes()
        for version in (1, 2):
            with self.subTest(version=version):
                legacy_schema = json.loads(current_schema)
                legacy_schema["properties"]["schema_version"]["const"] = version
                assessments = legacy_schema["properties"]["assessments"]
                del assessments["properties"]["pagination"]
                assessments["required"].remove("pagination")
                legacy_schema["properties"]["findings"]["items"]["properties"]["category"]["enum"].remove("pagination")
                if version == 1:
                    for field in ("space_checks", "rechecks"):
                        del legacy_schema["properties"][field]
                        legacy_schema["required"].remove(field)
                else:
                    legacy_schema["properties"]["space_checks"]["items"]["properties"]["level"]["enum"].remove("across_sheets")
                gate.write_json(schema_path, legacy_schema)
                self.output = self.base / f"legacy-{version}"
                self.prepare()
                report = self.response()
                report["schema_version"] = version
                del report["assessments"]["pagination"]
                report["assessments"]["compactness"]["status"] = "fail"
                report["findings"] = [self.finding()]
                if version == 1:
                    del report["space_checks"], report["rechecks"]
                else:
                    report["space_checks"] = [item for item in report["space_checks"] if item["level"] != "across_sheets"]
                self.runner(response=report)
                previous = self.output
                schema_path.write_bytes(current_schema)
                self.output = self.base / f"followup-{version}"
                self.prepare(previous_review=previous)
                self.assertEqual([self.finding()], gate.read_json(self.output / "gate.json")["previous_findings"])
                report = self.response()
                report["rechecks"] = [{"finding_id": "C1", "status": "resolved",
                                       "evidence": "The new native rendering shows the output core beside its source."}]
                self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_current_manifest_cannot_drop_previous_findings(self):
        self.prepare_followup()
        state = gate.read_json(self.output / "gate.json")
        state["previous_findings"] = []
        gate.write_json(self.output / "gate.json", state)
        result = self.submit()
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("gate.json", result["damaged_inputs"])

    def test_incomplete_previous_recheck_tracking_cannot_drop_older_findings(self):
        self.prepare_followup()
        for status in (None, "open", "unverified"):
            with self.subTest(status=status):
                report = self.response()
                if status:
                    report["rechecks"] = [{"finding_id": "C1", "status": status,
                                           "evidence": "The prior issue has not been closed."}]
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                with self.assertRaisesRegex(ValueError, "Previous review tracking is incomplete"):
                    gate.load_previous_review(self.output, self.source, "whole", [])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_unverified_previous_recheck_can_continue_when_finding_is_retained(self):
        self.prepare_followup()
        report = self.response()
        report["rechecks"] = [{"finding_id": "C1", "status": "unverified",
                               "evidence": "Clearance at the earlier gap is still uncertain."}]
        report["findings"] = [self.finding()]
        report["assessments"]["compactness"]["status"] = "unverified"
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])
        previous = self.output
        self.output = self.base / "third-review"
        self.prepare(previous_review=previous)
        report = self.response()
        report["rechecks"] = [{"finding_id": "C1", "status": "resolved",
                               "evidence": "The new detail confirms the corrected clearance."}]
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_changed_live_source_is_stale(self):
        self.prepare()
        self.source.write_text(fixtures.schematic(value="2k"), encoding="utf-8")
        result = self.submit()
        self.assertEqual("STALE", result["status"])
        self.assertIn(str(self.source), result["stale_inputs"])

    def test_new_project_dependency_invalidates_prepared_version(self):
        self.prepare()
        self.source.with_suffix(".kicad_pro").write_text("{}", encoding="utf-8")
        result = self.submit()
        self.assertEqual("STALE", result["status"])

    def test_changed_requirements_and_rules_are_stale(self):
        self.prepare()
        self.requirements.write_text("Changed original requirements", encoding="utf-8")
        (self.skill / "references" / gate.RULES[0]).write_text("New rule", encoding="utf-8")
        result = self.submit()
        self.assertEqual("STALE", result["status"])
        self.assertIn(str(self.requirements), result["stale_inputs"])

    def test_replaced_native_pdf_or_raster_never_passes(self):
        self.prepare()
        for relative in ("packet/preview.pdf", "packet/images/page-001.png", "snapshot/preview.pdf"):
            with self.subTest(relative=relative):
                path = self.output / relative
                before = path.read_bytes()
                path.write_bytes(b"old-or-replaced-artifact")
                result = gate.finalize(self.output)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertIn(relative, result["damaged_inputs"])
                path.write_bytes(before)

    def test_report_for_another_packet_source_or_pdf_is_unverified(self):
        self.prepare()
        for field in ("packet_id", "source_sha256", "pdf_sha256", "stage"):
            with self.subTest(field=field):
                report = self.response()
                report[field] = "layout" if field == "stage" else "f" * 64
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_missing_or_failed_reviewer_never_passes(self):
        self.prepare()
        for returncode, response in ((7, None), (0, False)):
            with self.subTest(returncode=returncode, response=response):
                result, _ = self.runner(returncode=returncode, response=response)
                self.assertEqual("UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink(missing_ok=True)

    def test_timeout_retains_unverified_receipt(self):
        self.prepare()
        result, _ = self.runner(timeout=True)
        self.assertEqual("UNVERIFIED", result["status"])
        receipt = gate.read_json(self.output / "invocation.json")
        self.assertIsNone(receipt["returncode"])
        self.assertIn("error", receipt)
        self.last_stop.assert_called_once_with(self.last_process)

    def test_full_page_and_detail_coverage_are_both_required(self):
        self.prepare()
        variants = []
        report = self.response()
        report["reviewed_pages"] = [1]
        variants.append(report)
        report = self.response()
        report["viewed_images"] = [p for p in report["viewed_images"] if "detail" not in p]
        variants.append(report)
        report = self.response()
        report["viewed_images"].append("images/invented.png")
        variants.append(report)
        report = self.response()
        report["viewed_images"].remove("images/page-001-detail-4.png")
        variants.append(report)
        for report in variants:
            with self.subTest(report=report):
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_whole_scope_needs_both_space_levels_on_every_page(self):
        self.prepare()
        for page, level, compactness in ((2, None, "pass"), (1, "between_cores", "pass"),
                                          (2, "within_core", "pass"), (2, None, "fail")):
            with self.subTest(page=page, level=level, compactness=compactness):
                report = self.response()
                report["space_checks"] = [item for item in report["space_checks"]
                                          if item["page"] != page or (level and item["level"] != level)]
                report["assessments"]["compactness"]["status"] = compactness
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertTrue(any("Missing space observation" in item for item in result["review_issues"]))
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_whole_multi_page_review_needs_an_across_sheets_observation(self):
        self.prepare()
        report = self.response()
        report["space_checks"] = [item for item in report["space_checks"] if item["level"] != "across_sheets"]
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Missing space observation: across_sheets", result["review_issues"])

    def test_multi_page_a5_paper_exemption_does_not_skip_pagination(self):
        self.source.write_text(fixtures.schematic().replace('(paper "A4")', '(paper "A5")'), encoding="utf-8")
        self.prepare(fixed_paper=False)
        self.assertEqual("A5", gate.read_json(self.output / "gate.json")["paper_exemption"])
        for status in ("not_applicable", "unverified", "pass"):
            with self.subTest(status=status):
                report = self.response()
                report["assessments"]["paper"]["status"] = "not_applicable"
                report["assessments"]["pagination"]["status"] = status
                result, _ = self.runner(response=report)
                self.assertEqual("REVIEW_ACCEPTED" if status == "pass" else "UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_fixed_paper_does_not_exempt_whole_multi_page_pagination(self):
        self.prepare()
        report = self.response()
        report["assessments"]["pagination"]["status"] = "not_applicable"
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Pagination cannot be skipped for a whole multi-page review", result["review_issues"])

    def test_single_page_can_skip_pagination_without_across_sheets_observation(self):
        self.render_pages = (1,)
        self.prepare()
        report = self.response()
        report["assessments"]["pagination"]["status"] = "not_applicable"
        self.assertFalse(any(item["level"] == "across_sheets" for item in report["space_checks"]))
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_local_multi_page_can_skip_pagination_without_across_sheets_observation(self):
        self.prepare(scope="local", affected_refs=["R1"])
        report = self.response()
        report["assessments"]["pagination"]["status"] = "not_applicable"
        report["space_checks"] = [item for item in report["space_checks"] if item["level"] == "within_core"]
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_layout_stage_requires_pagination_even_while_paper_is_unverified(self):
        self.prepare(stage="layout")
        for status in ("unverified", "fail", "pass"):
            with self.subTest(status=status):
                report = self.response()
                report["assessments"]["paper"]["status"] = "unverified"
                report["assessments"]["pagination"]["status"] = status
                if status == "fail":
                    report["findings"] = [self.finding(finding_id="P1", category="pagination")]
                result, _ = self.runner(response=report)
                expected = {"unverified": "UNVERIFIED", "fail": "CHANGES_REQUESTED", "pass": "REVIEW_ACCEPTED"}
                self.assertEqual(expected[status], result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_across_sheets_change_requests_pagination_fix_without_failing_compactness(self):
        self.prepare()
        report = self.response()
        report["space_checks"][-1].update({
            "disposition": "change_needed", "observation": "The output support circuit is split onto a mostly empty second sheet.",
            "action_or_constraint": "Combine the support circuit with its served stage and recheck the combined layout."})
        report["assessments"]["pagination"]["status"] = "fail"
        report["findings"] = [self.finding(finding_id="P1", category="pagination")]
        result = self.submit(report)
        self.assertEqual("CHANGES_REQUESTED", result["status"])
        self.assertEqual("pass", result["review"]["assessments"]["compactness"]["status"])
        previous = self.output
        self.output = self.base / "followup"
        self.prepare(previous_review=previous)
        report = self.response()
        report["rechecks"] = [{"finding_id": "P1", "status": "resolved",
                               "evidence": "The partition now follows the isolation boundary with no detached support circuit."}]
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_across_sheets_change_requires_pagination_fail_and_same_page_finding(self):
        self.prepare()
        variants = (("pass", [self.finding(finding_id="P1", category="pagination")]),
                    ("fail", []),
                    ("fail", [self.finding()]),
                    ("fail", [self.finding(finding_id="P1", category="pagination", page=2)]))
        for status, findings in variants:
            with self.subTest(status=status, findings=findings):
                report = self.response()
                report["space_checks"][-1]["disposition"] = "change_needed"
                report["assessments"]["pagination"]["status"] = status
                report["findings"] = findings
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertTrue(any("pagination" in item for item in result["review_issues"]))
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_pagination_finding_does_not_cover_a_within_core_space_change(self):
        self.prepare()
        report = self.response()
        report["space_checks"][0]["disposition"] = "change_needed"
        report["assessments"]["pagination"]["status"] = "fail"
        report["findings"] = [self.finding(finding_id="P1", category="pagination")]
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Space changes require a failed compactness assessment", result["review_issues"])
        self.assertIn("Space change needs a compactness finding: page 1", result["review_issues"])

    def test_unverified_across_sheets_observation_prevents_acceptance(self):
        self.prepare()
        report = self.response()
        report["space_checks"][-1]["disposition"] = "unverified"
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Space observation is unverified", result["review_issues"])

    def test_local_scope_still_needs_space_observation_on_every_page(self):
        self.prepare(scope="local", affected_refs=["R1"])
        report = self.response()
        report["space_checks"] = [item for item in report["space_checks"] if item["page"] == 1]
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Missing space observation: page 2, within_core", result["review_issues"])

    def test_space_checks_cannot_reference_an_unknown_page(self):
        self.prepare()
        report = self.response()
        report["space_checks"].append(dict(report["space_checks"][0], page=3))
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Space check references an unknown page", result["review_issues"])

    def test_empty_space_anchors_cannot_pass_finalization(self):
        self.prepare()
        for anchors in ([], [""], ["   "]):
            with self.subTest(anchors=anchors):
                report = self.response()
                report["space_checks"][0]["anchors"] = anchors
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertTrue(any("space_checks[0].anchors" in item for item in result["review_issues"]))
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_unresolved_space_checks_cannot_accompany_compactness_pass(self):
        self.prepare()
        for disposition in ("change_needed", "unverified"):
            with self.subTest(disposition=disposition):
                report = self.response()
                report["space_checks"][0]["disposition"] = disposition
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertTrue(any("Space" in item for item in result["review_issues"]))
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_actionable_space_check_with_failed_compactness_requests_changes(self):
        self.prepare()
        report = self.response()
        report["space_checks"][1].update({
            "disposition": "change_needed",
            "observation": "The gap between the regulator and output core has no crossing or labels.",
            "action_or_constraint": "Move the output core toward the regulator while retaining connector clearance."})
        report["assessments"]["compactness"]["status"] = "fail"
        report["findings"] = [self.finding()]
        result = self.submit(report)
        self.assertEqual("CHANGES_REQUESTED", result["status"])
        self.assertEqual([], result["review_issues"])

    def test_failed_compactness_can_coexist_with_unverified_space_observations(self):
        self.prepare()
        report = self.response()
        report["space_checks"][0]["disposition"] = "change_needed"
        report["space_checks"][1]["disposition"] = "unverified"
        report["assessments"]["compactness"]["status"] = "fail"
        report["findings"] = [self.finding()]
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertEqual("fail", result["review"]["assessments"]["compactness"]["status"])
        self.assertEqual(["Space observation is unverified"], result["review_issues"])

    def test_failed_compactness_can_report_other_issues_with_retained_spaces(self):
        self.prepare()
        report = self.response()
        report["assessments"]["compactness"].update({
            "status": "fail", "evidence": "A feedback branch is detached from its served pin pair."})
        report["findings"] = [dict(self.finding(),
                                   observation="The feedback branch is grouped with the output connector instead of its served pin pair.",
                                   action="Regroup the feedback branch beside its served pin pair, retaining the justified clearances.")]
        self.assertEqual("CHANGES_REQUESTED", self.submit(report)["status"])

    def test_each_failed_assessment_needs_a_same_category_finding(self):
        self.prepare(stage="layout")
        for category in gate.DIMENSIONS:
            for finding_category in (None, next(item for item in gate.DIMENSIONS if item != category), category):
                with self.subTest(category=category, finding_category=finding_category):
                    report = self.response()
                    report["assessments"][category]["status"] = "fail"
                    if finding_category:
                        report["findings"] = [self.finding(category=finding_category)]
                    result, _ = self.runner(response=report)
                    expected = "CHANGES_REQUESTED" if finding_category == category else "UNVERIFIED"
                    self.assertEqual(expected, result["status"])
                    if expected == "UNVERIFIED":
                        self.assertIn("Failed assessment needs a same-category finding: " + category,
                                      result["review_issues"])
                    for name in ("review.json", "invocation.json"):
                        (self.output / name).unlink()

    def test_space_changes_require_fail_even_when_compactness_is_not_pass(self):
        self.prepare()
        for status in ("unverified", "not_applicable"):
            with self.subTest(status=status):
                report = self.response()
                report["space_checks"][0]["disposition"] = "change_needed"
                report["assessments"]["compactness"]["status"] = status
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertIn("Space changes require a failed compactness assessment", result["review_issues"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_initial_review_cannot_claim_rechecks(self):
        self.prepare()
        report = self.response()
        report["rechecks"] = [{"finding_id": "C1", "status": "resolved", "evidence": "No previous report exists."}]
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Recheck references an unknown previous finding", result["review_issues"])

    def test_followup_requires_each_previous_finding_exactly_once(self):
        self.prepare_followup()
        recheck = {"finding_id": "C1", "status": "resolved", "evidence": "The earlier gap has been removed."}
        for rechecks in ([], [recheck, recheck], [dict(recheck, finding_id="unknown")]):
            with self.subTest(rechecks=rechecks):
                report = self.response()
                report["rechecks"] = rechecks
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_closed_rechecks_accept_only_without_same_current_finding(self):
        self.prepare_followup()
        for status in ("resolved", "not_applicable"):
            for retained in (False, True):
                with self.subTest(status=status, retained=retained):
                    report = self.response()
                    report["rechecks"] = [{"finding_id": "C1", "status": status,
                                           "evidence": "The current core position no longer has the earlier gap."}]
                    if retained:
                        report["findings"] = [self.finding()]
                        report["assessments"]["compactness"]["status"] = "fail"
                    result, _ = self.runner(response=report)
                    self.assertEqual("UNVERIFIED" if retained else "REVIEW_ACCEPTED", result["status"])
                    for name in ("review.json", "invocation.json"):
                        (self.output / name).unlink()

    def test_open_rechecks_require_same_id_category_and_failed_assessment(self):
        self.prepare_followup()
        for mode in ("missing", "other_id", "other_category", "passing", "valid"):
            with self.subTest(mode=mode):
                report = self.response()
                report["rechecks"] = [{"finding_id": "C1", "status": "open", "evidence": "The earlier gap remains."}]
                if mode != "missing":
                    report["findings"] = [self.finding(finding_id="C2" if mode == "other_id" else "C1",
                                                        category="geometry" if mode == "other_category" else "compactness")]
                if mode != "passing":
                    report["assessments"]["compactness"]["status"] = "fail"
                if mode == "other_category":
                    report["assessments"]["geometry"]["status"] = "fail"
                result, _ = self.runner(response=report)
                self.assertEqual("CHANGES_REQUESTED" if mode == "valid" else "UNVERIFIED", result["status"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_unverified_recheck_has_priority_over_current_failures(self):
        self.prepare_followup()
        report = self.response()
        report["rechecks"] = [{"finding_id": "C1", "status": "unverified", "evidence": "The relevant detail is obscured."}]
        report["findings"] = [self.finding(finding_id="C2")]
        report["assessments"]["compactness"]["status"] = "fail"
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Previous finding recheck is unverified: C1", result["review_issues"])

    def test_current_and_previous_finding_ids_must_be_unique(self):
        self.prepare()
        report = self.response()
        report["findings"] = [self.finding(), self.finding()]
        report["assessments"]["compactness"]["status"] = "fail"
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("Current finding IDs are not unique", result["review_issues"])
        with self.assertRaisesRegex(ValueError, "Previous finding IDs are not unique"):
            gate.load_previous_review(self.output, self.source, "whole", [])

    def test_each_space_change_needs_a_same_page_compactness_finding(self):
        self.prepare()
        for findings in ([], [self.finding(page=2)], [self.finding(category="geometry")]):
            with self.subTest(findings=findings):
                report = self.response()
                report["space_checks"][0]["disposition"] = "change_needed"
                report["assessments"]["compactness"]["status"] = "fail"
                report["findings"] = findings
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertIn("Space change needs a compactness finding: page 1", result["review_issues"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_author_contamination_or_unread_probe_is_unverified(self):
        self.prepare()
        for mode in ("author", "outside_packet", "probe"):
            report = self.response()
            if mode == "author":
                report["isolation"]["author_conclusions_seen"] = True
            elif mode == "outside_packet":
                report["isolation"]["outside_packet_files_seen"] = True
            else:
                report["probe_considered"] = False
            result, _ = self.runner(response=report)
            self.assertEqual("UNVERIFIED", result["status"])
            for name in ("review.json", "invocation.json"):
                (self.output / name).unlink()

    def test_final_unverified_paper_is_not_accepted(self):
        self.prepare()
        report = self.response()
        report["assessments"]["paper"]["status"] = "unverified"
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])

    def test_layout_stage_can_leave_paper_unverified(self):
        self.prepare(stage="layout")
        report = self.response()
        report["assessments"]["paper"]["status"] = "unverified"
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_specific_finding_requests_changes(self):
        self.prepare()
        report = self.response()
        report["assessments"]["geometry"]["status"] = "fail"
        report["findings"].append({"id": "G1", "category": "geometry", "page": 1,
                                   "references": ["R1"], "observation": "Wire crosses value glyph.",
                                   "action": "Move the value into free space."})
        self.assertEqual("CHANGES_REQUESTED", self.submit(report)["status"])

    def test_required_visual_dimensions_cannot_be_not_applicable(self):
        self.prepare()
        report = self.response()
        report["assessments"]["geometry"]["status"] = "not_applicable"
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])

    def test_review_tampering_after_submission_is_detected(self):
        self.prepare()
        self.submit()
        report = gate.read_json(self.output / "review.json")
        report["isolation"]["notes"] = "Changed after submission"
        gate.write_json(self.output / "review.json", report)
        self.assertEqual("UNVERIFIED", gate.finalize(self.output)["status"])

    def test_schema_rejects_missing_extra_wrong_type_and_invalid_enum(self):
        self.prepare()
        mutations = []
        report = self.response()
        del report["assessments"]["geometry"]["evidence"]
        mutations.append(report)
        report = self.response()
        report["override_pass"] = True
        mutations.append(report)
        report = self.response()
        report["reviewed_pages"] = [True]
        mutations.append(report)
        report = self.response()
        report["assessments"]["geometry"]["status"] = "looks-fine"
        mutations.append(report)
        report = self.response()
        report["assessments"]["geometry"]["evidence"] = "   "
        mutations.append(report)
        for report in mutations:
            with self.subTest(report=report), self.assertRaises(ValueError):
                gate.validate(report, self.schema)

    def test_schema_requires_v2_and_space_observations(self):
        self.prepare()
        reports = []
        report = self.response()
        report["schema_version"] = 1
        reports.append(report)
        report = self.response()
        del report["space_checks"]
        reports.append(report)
        report = self.response()
        report["space_checks"] = []
        reports.append(report)
        report = self.response()
        del report["assessments"]["pagination"]
        reports.append(report)
        for report in reports:
            with self.subTest(report=report), self.assertRaises(ValueError):
                gate.validate(report, self.schema)

    def test_schema_requires_concrete_space_observation_fields(self):
        self.prepare()
        for field, value in (("page", 0), ("level", "whole_page"), ("anchors", []),
                             ("anchors", ["   "]), ("observation", "   "),
                             ("disposition", "looks-fine"), ("action_or_constraint", "")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                report = self.response()
                report["space_checks"][0][field] = value
                gate.validate(report, self.schema)
        for field in self.response()["space_checks"][0]:
            with self.subTest(missing=field), self.assertRaises(ValueError):
                report = self.response()
                del report["space_checks"][0][field]
                gate.validate(report, self.schema)

    def test_schema_requires_rechecks_and_concrete_recheck_fields(self):
        self.prepare()
        report = self.response()
        del report["rechecks"]
        with self.assertRaises(ValueError):
            gate.validate(report, self.schema)
        for field, value in (("finding_id", "  "), ("status", "fixed"), ("evidence", "")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                report = self.response()
                report["rechecks"] = [{"finding_id": "C1", "status": "resolved", "evidence": "Gap removed."}]
                report["rechecks"][0][field] = value
                gate.validate(report, self.schema)

    def test_local_review_needs_concrete_affected_reference(self):
        with self.assertRaisesRegex(ValueError, "affected-ref"):
            self.prepare(scope="local")
        self.assertFalse(self.output.exists())
        self.capture_mock.assert_not_called()

    def test_local_scope_accepts_relevant_tiles_and_retains_scope(self):
        self.prepare(scope="local", affected_refs=["R1"], fixed_paper=False)
        report = self.response()
        report["viewed_images"] = [name for name in report["viewed_images"]
                                  if "detail" not in name or name.endswith("detail-1.png")]
        report["space_checks"] = [item for item in report["space_checks"] if item["level"] == "within_core"]
        report["assessments"]["paper"]["status"] = "not_applicable"
        result = self.submit(report)
        self.assertEqual("REVIEW_ACCEPTED", result["status"])
        self.assertEqual("local", result["scope"])
        self.assertEqual(["R1"], result["affected_refs"])

    def test_wrong_packet_with_finding_is_unverified_not_changes_requested(self):
        self.prepare()
        report = self.response()
        report["packet_id"] = "f" * 64
        report["findings"] = [{"id": "OLD-G1", "category": "geometry", "page": 1,
                               "references": ["R1"], "observation": "A finding from another version.",
                               "action": "Do not apply this finding to the current source."}]
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])

    def test_final_cannot_claim_paper_pass_without_candidate_or_exception(self):
        self.prepare(fixed_paper=False)
        result = self.submit()
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertTrue(any("candidate evidence" in issue for issue in result["review_issues"]))

    def test_paper_not_applicable_requires_recorded_exception(self):
        self.prepare(fixed_paper=False)
        report = self.response()
        report["assessments"]["paper"]["status"] = "not_applicable"
        self.assertEqual("UNVERIFIED", self.submit(report)["status"])

    def test_a5_is_detected_without_a_fixed_paper_claim(self):
        self.source.write_text(fixtures.schematic().replace('(paper "A4")', '(paper "A5")'), encoding="utf-8")
        self.prepare(fixed_paper=False)
        report = self.response()
        report["assessments"]["paper"]["status"] = "not_applicable"
        self.assertEqual("A5", gate.read_json(self.output / "gate.json")["paper_exemption"])
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def candidate_snapshot(self, source_text=None):
        candidate = self.base / "paper-candidate-snapshot"
        original = self.source.read_bytes()
        self.source.write_text((source_text or original.decode("utf-8")).replace('(paper "A4")', '(paper "A5")'), encoding="utf-8")
        self.capture(self.source, candidate, self.cli, pdf=True)
        self.source.write_bytes(original)
        return candidate

    def candidate_response(self):
        report = self.response()
        state = gate.read_json(self.output / "gate.json")
        for page in state["candidate_images"]:
            report["viewed_images"].append(page["full"])
            report["viewed_images"].extend(d["path"] for d in page["details"])
        for page in state["pages"]:
            report["space_checks"].append({
                "page": page["page"], "level": "paper_trial",
                "anchors": [f"candidate page {page['page']}: R1 support branch and output connector"],
                "observation": "The complete support branch and output connector remain legible, but their required separation reaches the title-block boundary.",
                "disposition": "retained",
                "action_or_constraint": "Retain the current sheet to preserve the required separation from the title block."})
        return report

    def test_candidate_is_frozen_and_its_image_coverage_is_required(self):
        candidate = self.candidate_snapshot()
        self.prepare(fixed_paper=False, paper_candidate=candidate)
        report = self.candidate_response()
        report["viewed_images"] = self.response()["viewed_images"]
        result, _ = self.runner(response=report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertTrue(any("candidate visual coverage" in item for item in result["review_issues"]))
        for name in ("review.json", "invocation.json"):
            (self.output / name).unlink()
        state = gate.read_json(self.output / "gate.json")
        report = self.candidate_response()
        result, _ = self.runner(response=report)
        self.assertEqual("REVIEW_ACCEPTED", result["status"])
        self.assertEqual([], result["review_issues"])
        self.assertEqual("PASS", state["paper_comparison"]["status"])
        (self.output / "packet" / "paper-candidate" / "preview.pdf").write_bytes(b"replaced candidate")
        self.assertEqual("UNVERIFIED", gate.finalize(self.output)["status"])

    def test_final_paper_pass_needs_trial_observations_on_every_current_page(self):
        self.prepare(fixed_paper=False, paper_candidate=self.candidate_snapshot())
        for omitted in ((1, 2), (2,)):
            with self.subTest(omitted=omitted):
                report = self.candidate_response()
                report["space_checks"] = [item for item in report["space_checks"]
                                          if item["level"] != "paper_trial" or item["page"] not in omitted]
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertEqual([f"Missing paper trial observation: page {page}" for page in omitted],
                                 result["review_issues"])
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_unverified_paper_trial_cannot_support_paper_pass(self):
        self.prepare(fixed_paper=False, paper_candidate=self.candidate_snapshot())
        report = self.candidate_response()
        report["space_checks"][-1]["disposition"] = "unverified"
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertEqual(["Space observation is unverified"], result["review_issues"])

    def test_invalid_paper_trial_requests_paper_fix_without_failing_compactness(self):
        self.prepare(fixed_paper=False, paper_candidate=self.candidate_snapshot())
        report = self.candidate_response()
        report["space_checks"][-1].update({
            "disposition": "change_needed",
            "observation": "The candidate moved the output connector over the support branch and beyond the page edge.",
            "action_or_constraint": "Rearrange the complete groups and review another candidate before choosing the sheet size."})
        report["assessments"]["paper"]["status"] = "fail"
        report["findings"] = [self.finding(finding_id="P1", category="paper", page=2)]
        result = self.submit(report)
        self.assertEqual("CHANGES_REQUESTED", result["status"])
        self.assertEqual("pass", result["review"]["assessments"]["compactness"]["status"])
        self.assertEqual([], result["review_issues"])

    def test_paper_trial_change_requires_paper_fail_and_same_page_finding(self):
        self.prepare(fixed_paper=False, paper_candidate=self.candidate_snapshot())
        variants = (("pass", [self.finding(finding_id="P1", category="paper", page=2)]),
                    ("fail", []), ("pass", [self.finding(page=2)]), ("fail", [self.finding(page=2)]),
                    ("fail", [self.finding(finding_id="P1", category="paper", page=1)]))
        for status, findings in variants:
            with self.subTest(status=status, findings=findings):
                report = self.candidate_response()
                report["space_checks"][-1]["disposition"] = "change_needed"
                report["assessments"]["paper"]["status"] = status
                report["findings"] = findings
                if any(item["category"] == "compactness" for item in findings):
                    report["assessments"]["compactness"]["status"] = "fail"
                result, _ = self.runner(response=report)
                self.assertEqual("UNVERIFIED", result["status"])
                self.assertTrue(any("paper" in item for item in result["review_issues"]))
                self.assertFalse(any("compactness" in item for item in result["review_issues"]))
                for name in ("review.json", "invocation.json"):
                    (self.output / name).unlink()

    def test_legacy_final_paper_pass_can_carry_findings_without_trial_observations(self):
        schema_path = self.skill / "references" / "review-report.schema.json"
        current_schema = schema_path.read_bytes()
        legacy_schema = json.loads(current_schema)
        legacy_schema["properties"]["space_checks"]["items"]["properties"]["level"]["enum"].remove("paper_trial")
        gate.write_json(schema_path, legacy_schema)
        candidate = self.candidate_snapshot()
        self.prepare(fixed_paper=False, paper_candidate=candidate)
        report = self.candidate_response()
        report["space_checks"] = [item for item in report["space_checks"] if item["level"] != "paper_trial"]
        report["findings"] = [self.finding()]
        report["assessments"]["compactness"]["status"] = "fail"
        result, _ = self.runner(response=report)
        self.assertEqual("UNVERIFIED", result["status"])
        previous = self.output
        schema_path.write_bytes(current_schema)
        self.output = self.base / "followup"
        self.prepare(previous_review=previous, fixed_paper=False, paper_candidate=candidate)
        self.assertEqual(report["findings"], gate.read_json(self.output / "gate.json")["previous_findings"])
        report = self.candidate_response()
        report["rechecks"] = [{"finding_id": "C1", "status": "resolved",
                               "evidence": "The output core now shares a compact boundary with the source."}]
        self.assertEqual("REVIEW_ACCEPTED", self.submit(report)["status"])

    def test_candidate_sources_each_have_frozen_probes_including_off_page_anchors(self):
        self.source.write_text(fixtures.schematic(child=True), encoding="utf-8")
        (self.base / "child.kicad_sch").write_text(fixtures.schematic(
            root=fixtures.CHILD, reference="R2", symbol=fixtures.uid("resistor2"),
            instance_path="/" + fixtures.ROOT + "/" + fixtures.SHEET), encoding="utf-8")
        candidate_source = self.source.read_text(encoding="utf-8").replace(
            '(at 50 50 0) (unit 1)', '(at 240 50 0) (unit 1)')
        candidate = self.candidate_snapshot(source_text=candidate_source)
        self.prepare(fixed_paper=False, paper_candidate=candidate)
        packet = self.output / "packet"
        evidence = gate.read_json(packet / "page_evidence.json")
        state = gate.read_json(self.output / "gate.json")
        self.assertEqual(2, len(evidence["sources"]))
        reports = {}
        for item in evidence["sources"]:
            path = packet / item["probe"]
            probe = gate.read_json(path)
            self.assertEqual(item["sha256"], probe["source_sha256"])
            self.assertEqual(str((packet / item["path"]).resolve()), probe["source"])
            self.assertEqual(item["probe_sha256"], gate.sha(path))
            self.assertEqual(item["probe_sha256"], state["files"]["packet/" + item["probe"]])
            reports[item["original_name"]] = probe
        self.assertEqual([{"reference": "R1", "at_raw": ["240", "50", "0"]}],
                         reports["main.kicad_sch"]["off_page_symbol_anchors"])
        self.assertEqual([], reports["child.kicad_sch"]["off_page_symbol_anchors"])
        damaged = evidence["sources"][0]["probe"]
        (packet / damaged).write_text("{}", encoding="utf-8")
        result = gate.finalize(self.output)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertIn("packet/" + damaged, result["damaged_inputs"])

    def test_candidate_with_replaced_pdf_is_rejected_before_dispatch(self):
        candidate = self.candidate_snapshot()
        (candidate / "preview.pdf").write_bytes(b"old PDF without matching snapshot hash")
        with self.assertRaisesRegex(ValueError, "intact snapshot"):
            self.prepare(fixed_paper=False, paper_candidate=candidate)
        self.assertFalse((self.output / "dispatch.json").exists())

    def test_paper_candidate_with_changed_component_content_cannot_pass(self):
        candidate = self.base / "paper-candidate-snapshot"
        original = self.source.read_bytes()
        self.source.write_text(fixtures.schematic(value="2k").replace('(paper "A4")', '(paper "A5")'), encoding="utf-8")
        self.capture(self.source, candidate, self.cli, pdf=True)
        self.source.write_bytes(original)
        self.prepare(fixed_paper=False, paper_candidate=candidate)
        report = self.candidate_response()
        result = self.submit(report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertTrue(any("content is not verified equivalent" in issue for issue in result["review_issues"]))

    def test_missing_fresh_pdf_cannot_reuse_old_project_pdf(self):
        self.source.with_suffix(".pdf").write_bytes(b"%PDF-1.7 old project PDF")
        def without_pdf(source, folder, cli, dependencies=(), pdf=False):
            return self.capture(source, folder, cli, dependencies, pdf=False)
        self.capture_mock.side_effect = without_pdf
        with self.assertRaisesRegex(ValueError, "Fresh native PDF unavailable"):
            self.prepare()

    def test_windows_npm_shim_resolves_installed_native_without_command_shell(self):
        shim = self.base / "npm with spaces" / "codex.cmd"
        shim.parent.mkdir()
        shim.write_text("@echo off", encoding="utf-8")
        entry = shim.parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
        entry.parent.mkdir(parents=True)
        entry.write_text("// fixture", encoding="utf-8")
        self.assertEqual([str(entry)], gate.codex_command(str(shim)))

    def test_windows_shim_without_unique_native_binary_is_rejected(self):
        shim = self.base / "codex.cmd"
        shim.write_text("@echo off", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unique native"):
            gate.codex_command(str(shim))
        for arch in ("x64", "arm64"):
            binary = self.base / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / ("codex-win32-" + arch) / "vendor" / arch / "bin" / "codex.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
        with self.assertRaisesRegex(ValueError, "unique native"):
            gate.codex_command(str(shim))

    def test_windows_timeout_cleanup_targets_only_started_pid_and_children(self):
        process = Mock(pid=7654321)
        with patch.object(gate.os, "name", "nt"), patch.object(gate.subprocess, "run") as run:
            gate.stop_process_tree(process)
        self.assertEqual(["taskkill", "/PID", "7654321", "/T", "/F"], run.call_args.args[0])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        process.wait.assert_called_once_with(timeout=30)

    def test_runner_is_fresh_read_only_and_uses_stdin(self):
        self.prepare()
        inherited_ids = {"CODEX_THREAD_ID": "parent-thread", "CODEX_SESSION_ID": "parent-session",
                         "CODEX_TOOL_CALL_ID": "parent-tool", "OPENAI_API_KEY": "fixture-auth-secret"}
        with patch.dict(gate.os.environ, inherited_ids):
            result, calls = self.runner()
        self.assertEqual("REVIEW_ACCEPTED", result["status"])
        call = calls[-1]
        command = call.args[0]
        self.assertIn("exec", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual("read-only", command[command.index("--sandbox") + 1])
        self.assertIn('approval_policy="never"', command)
        self.assertEqual("-", command[-1])
        self.assertNotIn("resume", command)
        self.assertNotIn("fork", command)
        self.assertFalse(any("bypass" in value for value in command))
        self.assertFalse(call.kwargs.get("shell", False))
        self.assertEqual(subprocess.PIPE, call.kwargs["stdin"])
        self.assertEqual(self.output / "packet", call.kwargs["cwd"])
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_TOOL_CALL_ID"):
            self.assertNotIn(name, call.kwargs["env"])
        self.assertEqual("fixture-auth-secret", call.kwargs["env"]["OPENAI_API_KEY"])
        self.assertNotIn("fixture-auth-secret", json.dumps(command))
        self.assertNotIn("fixture-auth-secret", (self.output / "invocation.json").read_text(encoding="utf-8"))
        self.assertEqual((self.output / "prompt.txt").read_text(encoding="utf-8"),
                         self.last_process.communicate.call_args.args[0])

    def test_context_overrides_disable_integrations_without_leaking_configuration(self):
        temporary_home = self.base / "home"
        codex_home = temporary_home / "codex-config"
        codex_home.mkdir(parents=True)
        config = codex_home / "config.toml"
        config_text = '''model = "fixture-user-model"
model_provider = "fixture-provider"
[model_providers.fixture-provider]
base_url = "https://fixture-provider.invalid/api"
experimental_bearer_token = "fixture-provider-secret"
[mcp_servers."server with spaces"]
command = "fixture-command"
env = { API_TOKEN = "fixture-mcp-secret" }
[plugins."example@registry"]
enabled = true
[[skills.config]]
path = "explicit/special/SKILL.md"
enabled = true
'''
        config.write_text(config_text, encoding="utf-8")
        paths = [temporary_home / ".agents" / "skills" / "kicad" / "SKILL.md",
                 codex_home / "skills" / "generic" / "SKILL.md"]
        for path in paths:
            path.parent.mkdir(parents=True)
            path.write_text("Fixture skill; not a review input.", encoding="utf-8")
        with patch.dict(gate.os.environ, {"CODEX_HOME": str(codex_home)}), \
                patch.object(gate.Path, "home", return_value=temporary_home):
            overrides = gate.reviewer_overrides()
        self.assertEqual(overrides[::2], ["-c"] * (len(overrides) // 2))
        settings = overrides[1::2]
        self.assertIn("project_doc_max_bytes=0", settings)
        self.assertIn("mcp_servers={}", settings)
        decoded = tomllib.loads("\n".join(settings))
        self.assertEqual({}, decoded["mcp_servers"])
        self.assertEqual({"example@registry": {"enabled": False}}, decoded["plugins"])
        self.assertEqual(0, decoded["project_doc_max_bytes"])
        text = "\n".join(settings)
        for secret in ("fixture-provider-secret", "fixture-mcp-secret", "fixture-user-model", "fixture-provider.invalid"):
            self.assertNotIn(secret, text)
        self.assertTrue(any(value.startswith("skills.config=") for value in settings))
        self.assertTrue(all(item["enabled"] is False for item in decoded["skills"]["config"]))
        self.assertIn("explicit/special/SKILL.md", text)
        for path in paths:
            self.assertIn(json.dumps(str(path.resolve())), text)
        self.assertEqual(config_text, config.read_text(encoding="utf-8"))

    def test_second_invocation_does_not_resume_or_overwrite(self):
        self.prepare()
        self.runner()
        with patch.object(gate.subprocess, "run") as run, self.assertRaisesRegex(ValueError, "already invoked"):
            gate.run_reviewer(self.output)
        run.assert_not_called()

    def test_prepare_and_run_cli_forward_previous_review(self):
        previous = self.base / "previous"
        for command in ("prepare", "run"):
            with self.subTest(command=command), \
                    patch.object(gate, "prepare", return_value={"status": "AWAITING_REVIEW", "output": str(self.output)}) as prepare, \
                    patch.object(gate, "run_reviewer", return_value={"status": "REVIEW_ACCEPTED"}) as run, \
                    patch("builtins.print"):
                result = gate.main([command, str(self.source), "--requirements", str(self.requirements),
                                    "--kicad-cli", str(self.cli), "--previous-review", str(previous)])
                self.assertEqual(0, result)
                self.assertEqual(previous, prepare.call_args.kwargs["previous_review"])
                self.assertEqual(command == "run", run.called)

    def test_packet_path_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            gate.within(self.base, "../outside.json")


if __name__ == "__main__":
    unittest.main()
