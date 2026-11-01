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
        for page in (1, 2):
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
        return {"schema_version": 1, "packet_id": state["packet_id"], "stage": state["stage"],
                "source_sha256": state["source_sha256"], "pdf_sha256": state["pdf_sha256"],
                "reviewed_pages": [p["page"] for p in state["pages"]],
                "viewed_images": [name for p in state["pages"] for name in
                                  [p["full"], *[d["path"] for d in p["details"]]]],
                "isolation": {"author_conclusions_seen": False, "outside_packet_files_seen": False,
                              "notes": "Only packet input viewed."},
                "assessments": {name: {"status": "pass", "evidence": "Observed original source and native image."}
                                for name in gate.DIMENSIONS},
                "findings": [], "unverified": ["Electrical correctness is outside this visual review."],
                "probe_considered": True}

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
        self.assertIsNone(result["electrical_comparison"])
        self.assertTrue(any("equivalence" in item for item in result["limitations"]))

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

    def candidate_snapshot(self):
        candidate = self.base / "paper-candidate-snapshot"
        original = self.source.read_bytes()
        self.source.write_text(fixtures.schematic().replace('(paper "A4")', '(paper "A5")'), encoding="utf-8")
        self.capture(self.source, candidate, self.cli, pdf=True)
        self.source.write_bytes(original)
        return candidate

    def test_candidate_is_frozen_and_its_image_coverage_is_required(self):
        candidate = self.candidate_snapshot()
        self.prepare(fixed_paper=False, paper_candidate=candidate)
        report = self.response()
        result, _ = self.runner(response=report)
        self.assertEqual("UNVERIFIED", result["status"])
        self.assertTrue(any("candidate visual coverage" in item for item in result["review_issues"]))
        for name in ("review.json", "invocation.json"):
            (self.output / name).unlink()
        state = gate.read_json(self.output / "gate.json")
        for page in state["candidate_images"]:
            report["viewed_images"].append(page["full"])
            report["viewed_images"].extend(d["path"] for d in page["details"])
        result, _ = self.runner(response=report)
        self.assertEqual("REVIEW_ACCEPTED", result["status"])
        self.assertEqual("PASS", state["paper_comparison"]["status"])
        (self.output / "packet" / "paper-candidate" / "preview.pdf").write_bytes(b"replaced candidate")
        self.assertEqual("UNVERIFIED", gate.finalize(self.output)["status"])

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
        report = self.response()
        state = gate.read_json(self.output / "gate.json")
        for page in state["candidate_images"]:
            report["viewed_images"].append(page["full"])
            report["viewed_images"].extend(d["path"] for d in page["details"])
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

    def test_packet_path_cannot_escape_root(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            gate.within(self.base, "../outside.json")


if __name__ == "__main__":
    unittest.main()
