import copy
import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from companion.core import clone_call, load_active_call, start_call, stop_call
import companion.downloads as downloads_module
from companion.downloads import finish_call, handle_completed_download, validate_response
from companion.jobs import check_job, job_brief, prepare_job
from companion.native_host import dispatch
from companion.repair import collect_defects, open_repair_round
from companion.schema import errors
from scripts.package_response import package


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "ExternalCall"
        self.downloads = self.base / "Downloads"
        self.downloads.mkdir()
        self.job = {
            "title": "Review fixture", "purpose": "Choose the next change.",
            "task": "Evaluate the supplied evidence.", "scope": {"in": ["Review"], "out": ["Edit"]},
            "constraints": ["No product edits."], "sources": [], "stop_condition": "Report limitations.",
            "requirements": [{"id": "R1", "requirement": "Give evidence for the recommendation.",
                              "acceptance": {"condition": "Report supports the recommendation.",
                                             "method": "review", "evidence": ["report.md"], "reviewer": "terminal"}}],
            "deliverables": [{"filename": "report.md", "description": "Recommendation with evidence."}],
            "result_schema": {"type": "object", "required": ["summary"],
                              "properties": {"summary": {"type": "string"}}},
        }

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, job=None):
        manifest = prepare_job(self.root, job or self.job, datetime.now(timezone.utc))
        return manifest, self.root / "calls" / manifest["exchange_id"]

    def message(self, command, exchange):
        return {"protocol_version": 1, "command": command, "payload": {"exchange_id": exchange}}

    def deliver(self, manifest, exchange, *, status="COMPLETE", results=None, missing=False, finish=True):
        start_call(self.root, manifest["exchange_id"], 42, [])
        outputs = self.base / "outputs"
        outputs.mkdir(exist_ok=True)
        artifact = outputs / "report.md"
        if missing:
            artifact.unlink(missing_ok=True)
        else:
            artifact.write_bytes(b"supported finding\n")
        payload = {"results": results if results is not None else {"summary": "Supported finding."},
                   "criteria_report": [{"id": "R1", "status": "UNMET" if missing else "MET",
                                        "evidence": "report.md" if not missing else "Unavailable."}],
                   "limitations": ["Artifact unavailable."] if missing else []}
        output = package(exchange / "request" / "WEB_REVIEW_REQUEST.json", payload,
                         outputs, self.downloads, status)
        self.assertEqual(handle_completed_download(self.root, {
            "id": 100, "filename": str(output), "state": "complete"
        })["status"], "MOVED")
        return finish_call(self.root, manifest["exchange_id"], self.downloads) if finish else None

    def test_compiler_packager_ingestion_and_acceptance_are_distinct(self):
        manifest, exchange = self.prepare()
        report = self.deliver(manifest, exchange)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertTrue(report["schema_valid"])
        self.assertTrue(report["manifest_verified"])
        self.assertTrue(report["work_complete"])
        self.assertEqual(report["semantic_acceptance"], "NOT_REVIEWED")
        self.assertEqual(collect_defects(exchange), [])
        self.assertIn("Done:", manifest["job_brief"])
        with zipfile.ZipFile(exchange / "request" / manifest["attach_files"][0]) as archive:
            self.assertIn("000_READ_ME_FIRST.md", archive.namelist())
            self.assertIn("package_response.py", archive.namelist())

    def test_actual_task_schema_is_enforced(self):
        manifest, exchange = self.prepare()
        report = self.deliver(manifest, exchange, results={"wrong": True})
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["schema_valid"])
        self.assertIn("SCHEMA_INVALID", {d["kind"] for d in report["defects"]})
        self.assertEqual(report["defects"], collect_defects(exchange))

    def test_honest_partial_with_absent_artifact_is_delivered_intact(self):
        manifest, exchange = self.prepare()
        report = self.deliver(manifest, exchange, status="PARTIAL", missing=True)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["response_status"], "PARTIAL")
        self.assertFalse(report["work_complete"])
        self.assertTrue(report["schema_valid"])

    def test_scope_blocker_prevents_any_package(self):
        job = copy.deepcopy(self.job)
        job["unresolved_decisions"] = ["Scope is unsettled."]
        with self.assertRaisesRegex(ValueError, "not settled"):
            self.prepare(job)
        self.assertFalse((self.root / "calls").exists())

    def test_orphan_evidence_and_fake_mechanical_review_are_refused(self):
        job = copy.deepcopy(self.job)
        job["requirements"][0]["acceptance"]["evidence"] = ["unknown.md"]
        with self.assertRaisesRegex(ValueError, "unknown acceptance evidence"):
            check_job(job)
        job = copy.deepcopy(self.job)
        job["requirements"][0]["acceptance"]["reviewer"] = "mechanical"
        with self.assertRaisesRegex(ValueError, "semantic review"):
            check_job(job)

    def test_internal_and_credential_sources_are_refused(self):
        for relative in ("state/private.json", "calls/old/private.json", ".env"):
            source = self.root / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("fixture")
            job = copy.deepcopy(self.job)
            job["sources"] = [{"path": str(source), "role": "Fixture"}]
            with self.assertRaises(ValueError):
                self.prepare(job)

    def test_nonportable_and_colliding_deliverables_fail(self):
        for name in ("report?.md", "../report.md", "folder/CON.txt"):
            job = copy.deepcopy(self.job)
            job["deliverables"][0]["filename"] = name
            with self.assertRaises(ValueError):
                check_job(job)
        job = copy.deepcopy(self.job)
        job["deliverables"].append({"filename": "REPORT.MD", "description": "Collision."})
        with self.assertRaisesRegex(ValueError, "unique"):
            check_job(job)

    def test_failed_go_does_not_activate(self):
        manifest, exchange = self.prepare()
        (exchange / "request" / manifest["attach_files"][0]).write_bytes(b"tampered")
        message = self.message("call.go", manifest["exchange_id"])
        message["payload"].update(tab_id=42, download_baseline=[])
        with self.assertRaises(ValueError):
            dispatch(self.root, message)
        self.assertIsNone(load_active_call(self.root, manifest["exchange_id"]))
        stored = json.loads((exchange / "EXCHANGE_MANIFEST.json").read_text())
        self.assertEqual(stored["state"], "PREPARED")

    def test_done_retry_is_bound_to_original_exchange(self):
        first, exchange = self.prepare()
        original = self.deliver(first, exchange)
        second, exchange2 = self.prepare()
        self.deliver(second, exchange2)
        self.assertEqual(dispatch(self.root, self.message("call.done", first["exchange_id"])), original)

    def test_clone_preserves_job_and_evidence_but_has_fresh_routing(self):
        first, exchange = self.prepare()
        self.deliver(first, exchange)
        clone = clone_call(self.root, first["exchange_id"], datetime.now(timezone.utc))
        self.assertNotEqual(first["request_id"], clone["request_id"])
        self.assertEqual(first["job_revision"], clone["job_revision"])
        self.assertNotEqual(first["expected_artifacts"], clone["expected_artifacts"])
        self.assertEqual(clone["contract_version"], 2)

    def test_repair_uses_fresh_attempt_and_old_download_cannot_satisfy_it(self):
        first, exchange = self.prepare()
        self.deliver(first, exchange, results={})
        correction = open_repair_round(self.root, first["exchange_id"], 42, [])
        self.assertTrue(correction["new_attempt"])
        self.assertNotEqual(correction["exchange_id"], first["exchange_id"])
        old_name = first["expected_artifacts"][0]
        old = self.downloads / old_name
        shutil.copyfile(exchange / "response" / old_name, old)
        self.assertEqual(handle_completed_download(self.root, {
            "id": 101, "filename": str(old), "state": "complete"
        })["status"], "IGNORED")
        self.assertTrue(old.exists())

    def test_local_task_schema_references_work_and_network_refs_fail(self):
        job = copy.deepcopy(self.job)
        job["result_schema"] = {"type": "object", "$defs": {"summary": {"type": "string"}},
                                "properties": {"summary": {"$ref": "#/$defs/summary"}},
                                "required": ["summary"]}
        manifest, exchange = self.prepare(job)
        self.assertEqual(self.deliver(manifest, exchange)["status"], "COMPLETE")
        with self.assertRaisesRegex(ValueError, "local fragments"):
            errors({"$ref": "https://example.invalid/schema"}, {})

    def test_brief_is_bounded_and_contains_completion_rule(self):
        job = copy.deepcopy(self.job)
        job["task"] = "x" * 8000
        self.assertLess(len(job_brief(job)), 3000)
        self.assertIn("Stop:", job_brief(job))

    def test_stop_during_validation_wins_without_losing_evidence(self):
        manifest, exchange = self.prepare()
        self.deliver(manifest, exchange, finish=False)
        original = downloads_module.validate_response
        def interrupted(path):
            stop_call(self.root, manifest["exchange_id"])
            return original(path)
        with patch("companion.downloads.validate_response", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "changed while validating"):
                finish_call(self.root, manifest["exchange_id"])
        self.assertEqual(json.loads((exchange / "EXCHANGE_MANIFEST.json").read_text())["state"], "STOPPED")
        self.assertTrue((exchange / "response" / manifest["expected_artifacts"][0]).is_file())

    def test_unrelated_preparation_does_not_wait_for_global_validation_lock(self):
        manifest, exchange = self.prepare()
        self.deliver(manifest, exchange, finish=False)
        original = downloads_module.validate_response
        prepared = []
        def concurrent(path):
            prepared.append(self.prepare()[0])
            return original(path)
        with patch("companion.downloads.validate_response", side_effect=concurrent):
            self.assertEqual(finish_call(self.root, manifest["exchange_id"])["status"], "COMPLETE")
        self.assertNotEqual(prepared[0]["exchange_id"], manifest["exchange_id"])

    def test_done_recovers_commit_before_active_cleanup(self):
        manifest, exchange = self.prepare()
        self.deliver(manifest, exchange, finish=False)
        original = downloads_module._write_json_atomic
        def interrupted(path, value):
            if path.name == "LAST_RESULT.json":
                raise OSError("simulated process interruption")
            original(path, value)
        with patch("companion.downloads._write_json_atomic", side_effect=interrupted):
            with self.assertRaises(OSError):
                finish_call(self.root, manifest["exchange_id"])
        report = finish_call(self.root, manifest["exchange_id"])
        self.assertEqual(report["status"], "COMPLETE")
        self.assertIsNone(load_active_call(self.root, manifest["exchange_id"]))
