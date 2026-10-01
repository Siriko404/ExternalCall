import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import load_active_call, prepare_call, start_call
from companion.downloads import (
    finish_call,
    handle_completed_download,
    ingest_from_downloads,
    validate_response,
)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        self.sources = base / "sources"
        self.downloads = base / "Downloads"
        self.sources.mkdir()
        self.downloads.mkdir()
        request = self.write_source(
            "WEB_REVIEW_REQUEST.json", {"request_id": "request_fixture"}
        )
        schema = self.write_source(
            "WEB_RESPONSE_SCHEMA.json", {"type": "object"}
        )
        context = self.write_source("context.txt", "context\n")
        spec = {
            "subject": "Fixture call",
            "request_id": "request_fixture",
            "expected_main_json": "result.json",
            "expected_artifacts": ["fixture_outputs.zip"],
            "prompt_text": "Return files only.\n",
            "input_files": [
                {"path": str(request), "filename": request.name},
                {"path": str(schema), "filename": schema.name},
                {"path": str(context), "filename": context.name},
            ],
        }
        now = datetime(
            2026, 7, 14, 15, 15, 0, tzinfo=timezone(timedelta(hours=-4))
        )
        self.manifest = prepare_call(self.root, spec, now)
        start_call(self.root, self.manifest["exchange_id"], 11, [99])
        self.exchange = self.root / "calls" / self.manifest["exchange_id"]
        self.report_bytes = b"report"
        self.report_digest = hashlib.sha256(self.report_bytes).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, name, value):
        path = self.sources / name
        if isinstance(value, dict):
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        else:
            path.write_text(value, encoding="utf-8")
        return path

    def download(self, name, contents):
        path = self.downloads / name
        path.write_bytes(contents)
        return path

    def main_json_bytes(
        self,
        *,
        request_id="request_fixture",
        status="COMPLETE",
        artifacts=True,
        artifact_digest=None,
    ):
        manifest = []
        # One archive is what comes down, so that is what `delivery` names.
        delivery = ["fixture_outputs.zip"]
        if artifacts:
            manifest.append(
                {
                    "filename": "report.md",
                    "status": "CREATED",
                    "media_type": "text/markdown",
                    "size": len(self.report_bytes),
                    "sha256": artifact_digest or self.report_digest,
                }
            )
            delivery.append("report.md")
        return (
            json.dumps(
                {
                    "request_id": request_id,
                    "status": status,
                    "artifacts_manifest": manifest,
                    "delivery": delivery,
                }
            )
            + "\n"
        ).encode("utf-8")

    def completed(self, download_id, path):
        return {"id": download_id, "filename": str(path), "state": "complete"}

    def delivery_archive(self, *, main=None, report=..., name="fixture_outputs.zip"):
        """The one archive a call comes back as: the main JSON, plus members.

        A delivery is a single download now, so a test that used to write two
        files into the Downloads folder writes one archive holding both.
        """
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(
                "result.json", main if main is not None else self.main_json_bytes()
            )
            if report is not ...:
                if report is not None:
                    bundle.writestr("report.md", report)
            else:
                bundle.writestr("report.md", self.report_bytes)
        return self.download(name, buffer.getvalue())

    def test_baseline_download_is_ignored(self):
        source = self.download("old.json", b"old")

        result = handle_completed_download(self.root, self.completed(99, source))

        self.assertEqual(result["status"], "IGNORED")
        self.assertTrue(source.exists())

    def test_one_archive_carries_the_whole_delivery(self):
        """The main JSON is not a download; it is lifted out of the archive.

        Nothing waits for anything else to explain it, which is what the shared
        pending pool used to be for. A call either has its archive or it has
        nothing.
        """
        archive = self.delivery_archive()

        moved = handle_completed_download(self.root, self.completed(2, archive))

        self.assertEqual(moved["status"], "MOVED")
        self.assertEqual(moved["stored_name"], "fixture_outputs.zip")
        self.assertEqual(moved["extracted_main"], "result.json")
        self.assertFalse(archive.exists())
        self.assertTrue((self.exchange / "response" / "fixture_outputs.zip").is_file())
        self.assertEqual(
            json.loads(
                (self.exchange / "response" / "result.json").read_text(encoding="utf-8")
            )["request_id"],
            "request_fixture",
        )

    def test_an_archive_without_the_main_response_is_invalid(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("report.md", self.report_bytes)
        archive = self.download("fixture_outputs.zip", buffer.getvalue())

        result = handle_completed_download(self.root, self.completed(2, archive))

        self.assertEqual(result["status"], "INVALID")
        self.assertIn("result.json", result["error"])
        # The archive is kept: it is the evidence, and an operator with an error
        # message and nothing to open cannot tell what went wrong.
        self.assertTrue((self.exchange / "response" / "fixture_outputs.zip").is_file())

    def test_browser_suffix_binds_to_the_expected_archive(self):
        archive = self.delivery_archive(
            main=self.main_json_bytes(artifacts=False),
            report=None,
            name="fixture_outputs (1).zip",
        )

        moved = handle_completed_download(self.root, self.completed(3, archive))

        self.assertEqual(moved["stored_name"], "fixture_outputs.zip")
        self.assertFalse(archive.exists())
        self.assertTrue((self.exchange / "response" / "result.json").is_file())

    def test_unmatched_download_after_main_stays_in_downloads(self):
        archive = self.delivery_archive(main=self.main_json_bytes(artifacts=False), report=None)
        handle_completed_download(self.root, self.completed(3, archive))
        unrelated = self.download("unrelated.pdf", b"unrelated")

        result = handle_completed_download(
            self.root, self.completed(4, unrelated)
        )

        self.assertEqual(result["status"], "IGNORED")
        self.assertTrue(unrelated.exists())

    def test_wrong_request_id_is_rejected_without_moving(self):
        archive = self.delivery_archive(
            main=self.main_json_bytes(request_id="another_request")
        )

        result = handle_completed_download(self.root, self.completed(3, archive))

        self.assertEqual(result["status"], "INVALID")
        self.assertIn("request_id", result["error"])
        # Both files are kept. They are what came back, and an operator told
        # only "invalid" with nothing to open cannot tell what happened.
        # Validation reads the same file and reports it invalid by name.
        self.assertTrue((self.exchange / "response" / "fixture_outputs.zip").is_file())
        self.assertTrue((self.exchange / "response" / "result.json").is_file())
        report = validate_response(self.exchange)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("result.json", report["invalid_files"])

    def test_a_loose_main_json_beside_the_archive_is_ignored(self):
        """One zip out means exactly that.

        A responder that also offers the main JSON as its own download has
        broken the rule, and filing it quietly would let that delivery validate
        as though it had obeyed. It is named in the reason instead, which is how
        the operator finds out.
        """
        loose = self.download("result.json", self.main_json_bytes())

        result = handle_completed_download(self.root, self.completed(3, loose))

        self.assertEqual(result["status"], "IGNORED")
        self.assertIn("result.json", result["reason"])
        self.assertTrue(loose.exists())
        self.assertFalse((self.exchange / "response" / "result.json").is_file())

    def test_a_loose_file_beside_the_archive_is_ignored_and_left_alone(self):
        """Only the archive is expected, so anything else is reported as
        ignored rather than parked somewhere the operator cannot see."""
        archive = self.delivery_archive()
        handle_completed_download(self.root, self.completed(3, archive))
        loose = self.download("report.md", self.report_bytes)

        result = handle_completed_download(self.root, self.completed(4, loose))

        self.assertEqual(result["status"], "IGNORED")
        self.assertIn("report.md", result["reason"])
        self.assertTrue(loose.exists())

    def test_existing_different_response_is_never_overwritten(self):
        destination = self.exchange / "response" / "fixture_outputs.zip"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"different")
        archive = self.delivery_archive()

        result = handle_completed_download(self.root, self.completed(4, archive))

        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(destination.read_bytes(), b"different")
        self.assertTrue(archive.exists())

    def test_nonexistent_completed_path_is_rejected(self):
        missing = self.downloads / "missing.json"

        with self.assertRaisesRegex(FileNotFoundError, "missing.json"):
            handle_completed_download(self.root, self.completed(3, missing))

    def test_finish_call_reports_missing_artifact_and_stops_monitoring(self):
        archive = self.delivery_archive(report=None)
        handle_completed_download(self.root, self.completed(3, archive))

        report = finish_call(self.root)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["missing_files"], ["report.md"])
        self.assertIsNone(load_active_call(self.root))
        stored = json.loads(
            (self.exchange / "validation" / "VALIDATION_REPORT.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stored, report)

    def test_finish_call_validates_complete_delivery(self):
        archive = self.delivery_archive()
        handle_completed_download(self.root, self.completed(3, archive))

        report = finish_call(self.root)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["missing_files"], [])
        self.assertEqual(report["invalid_files"], [])
        self.assertEqual(report["response_status"], "COMPLETE")
        self.assertTrue(report["work_complete"])
        self.assertEqual(validate_response(self.exchange), report)

    def test_partial_response_delivered_intact_is_not_invalid(self):
        """A responder that declares its own gaps must not be told it is corrupt.

        This is the regression. Any status other than COMPLETE used to put the
        main JSON into invalid_files, the same bucket as a failed hash, so the
        exchange went INCOMPLETE and every reader was told to repair a response
        whose files were flawless. Prompts in this project explicitly ask for
        PARTIAL when something cannot be settled, so the system punished the one
        behaviour it had asked for, and it did so on every such call.

        Nothing covered this case, which is why it shipped. The delivery facts
        and the work facts are asserted separately here so they cannot be
        conflated again without a test going red.
        """
        archive = self.delivery_archive(
            main=self.main_json_bytes(status="PARTIAL")
        )
        handle_completed_download(self.root, self.completed(3, archive))

        report = finish_call(self.root)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["invalid_files"], [])
        self.assertEqual(report["missing_files"], [])
        self.assertEqual(report["response_status"], "PARTIAL")
        self.assertFalse(report["work_complete"])
        manifest = json.loads(
            (self.exchange / "EXCHANGE_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["state"], "COMPLETE")

    def test_blocked_response_delivered_intact_is_not_invalid(self):
        archive = self.delivery_archive(
            main=self.main_json_bytes(status="BLOCKED")
        )
        handle_completed_download(self.root, self.completed(3, archive))

        report = finish_call(self.root)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["invalid_files"], [])
        self.assertEqual(report["response_status"], "BLOCKED")
        self.assertFalse(report["work_complete"])

    def test_a_corrupt_artifact_is_still_invalid_whatever_the_status_says(self):
        """The fix must not soften the check it was carved out of.

        A hash mismatch is a real defect and has to keep reporting as one no
        matter how the responder describes its own work. Written against
        validate_response directly, because the download handler refuses a
        mismatching file on arrival and it would never reach the validator by
        the normal route.
        """
        response = self.exchange / "response"
        response.mkdir(parents=True, exist_ok=True)
        (response / "result.json").write_bytes(
            self.main_json_bytes(status="PARTIAL", artifact_digest="0" * 64)
        )
        (response / "report.md").write_bytes(self.report_bytes)

        report = validate_response(self.exchange)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["invalid_files"], ["report.md"])
        self.assertEqual(report["response_status"], "PARTIAL")

    def test_an_expected_artifact_declared_absent_is_not_reported_missing(self):
        """Declaring an artifact was not created is a report, not a silent drop.

        The expected-artifact backstop exists so a deliverable the main JSON
        forgot cannot vanish quietly. A response that names the file and marks
        it NOT_CREATED has not forgotten it, so calling it missing would punish
        the same honesty the status fix stopped punishing.
        """
        exchange, main_path = self._exchange_expecting_an_artifact()
        payload = {
            "request_id": "request_declared",
            "status": "BLOCKED",
            "artifacts_manifest": [
                {"filename": "declared_outputs.zip", "status": "NOT_CREATED"}
            ],
            "delivery": ["declared_result.json"],
        }
        main_path.write_text(json.dumps(payload), encoding="utf-8")

        report = validate_response(exchange)

        self.assertEqual(report["missing_files"], [])
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["response_status"], "BLOCKED")

    def test_an_expected_artifact_silently_dropped_is_still_missing(self):
        """The other half of that carve-out, so it cannot be widened by accident."""
        exchange, main_path = self._exchange_expecting_an_artifact()
        payload = {
            "request_id": "request_declared",
            "status": "COMPLETE",
            "artifacts_manifest": [],
            "delivery": ["declared_result.json"],
        }
        main_path.write_text(json.dumps(payload), encoding="utf-8")

        report = validate_response(exchange)

        self.assertEqual(report["missing_files"], ["declared_outputs.zip"])
        self.assertEqual(report["status"], "INCOMPLETE")

    def valid_zip_bytes(self):
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("inner.md", "content\n")
        return buffer.getvalue()

    # ---- ingest from Downloads (the capture fix) ----

    def test_ingest_pulls_expected_main_from_downloads(self):
        self.download("result.json", self.main_json_bytes(artifacts=False))

        result = ingest_from_downloads(
            self.root, self.manifest["exchange_id"], self.downloads
        )

        self.assertIn("result.json", result["ingested"])
        self.assertTrue((self.exchange / "response" / "result.json").is_file())
        # the Downloads copy is left in place as the record of truth
        self.assertTrue((self.downloads / "result.json").exists())

    def test_ingest_matches_browser_suffix(self):
        self.download("result (1).json", self.main_json_bytes(artifacts=False))

        result = ingest_from_downloads(
            self.root, self.manifest["exchange_id"], self.downloads
        )

        self.assertIn("result.json", result["ingested"])
        self.assertTrue((self.exchange / "response" / "result.json").is_file())

    def test_ingest_never_overwrites_different_bytes(self):
        (self.exchange / "response").mkdir(parents=True, exist_ok=True)
        (self.exchange / "response" / "result.json").write_bytes(b"already here")
        self.download("result.json", self.main_json_bytes(artifacts=False))

        result = ingest_from_downloads(
            self.root, self.manifest["exchange_id"], self.downloads
        )

        self.assertIn("result.json", result["conflicts"])
        self.assertEqual(
            (self.exchange / "response" / "result.json").read_bytes(), b"already here"
        )

    def test_ingest_missing_file_is_reported_not_found(self):
        result = ingest_from_downloads(
            self.root, self.manifest["exchange_id"], self.downloads
        )

        self.assertEqual(result["not_found"], ["result.json", "fixture_outputs.zip"])

    def test_ingest_lifts_the_main_response_out_of_the_archive(self):
        """The main JSON is never in the Downloads folder under its own name.

        This is the path that rescued a call whose download events the extension
        lost, so it has to find the main response the same way the live route
        does: inside the one archive that did come down.
        """
        self.delivery_archive(main=self.main_json_bytes(artifacts=False), report=None)

        result = ingest_from_downloads(
            self.root, self.manifest["exchange_id"], self.downloads
        )

        self.assertEqual(result["not_found"], [])
        self.assertIn("fixture_outputs.zip", result["ingested"])
        self.assertIn("result.json", result["ingested"])
        self.assertTrue((self.exchange / "response" / "result.json").is_file())

    def test_finish_call_ingests_from_downloads_then_validates(self):
        self.delivery_archive(main=self.main_json_bytes(artifacts=False), report=None)

        report = finish_call(self.root, downloads_dir=self.downloads)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertTrue((self.exchange / "response" / "result.json").is_file())

    def test_side_panel_done_ingests_from_downloads(self):
        """The side panel's Done must ingest too, not just the CLI.

        This is the regression. The capture fix was wired into cli.py, but the
        Done button goes extension -> service worker -> native_host, which called
        finish_call without a downloads dir. The ingest is guarded on that
        argument, so it silently did nothing on the one path the operator
        actually uses, while the CLI path looked fixed.
        """
        import os
        from companion.native_host import dispatch

        self.delivery_archive(main=self.main_json_bytes(artifacts=False), report=None)
        previous = os.environ.get("EXTERNALCALL_DOWNLOADS_DIR")
        os.environ["EXTERNALCALL_DOWNLOADS_DIR"] = str(self.downloads)
        try:
            report = dispatch(
                self.root,
                {"protocol_version": 1, "command": "call.done", "payload": {}},
            )
        finally:
            if previous is None:
                os.environ.pop("EXTERNALCALL_DOWNLOADS_DIR", None)
            else:
                os.environ["EXTERNALCALL_DOWNLOADS_DIR"] = previous

        self.assertEqual(report["status"], "COMPLETE")
        self.assertTrue((self.exchange / "response" / "result.json").is_file())

    # ---- tolerate a junk model manifest on byte-perfect files ----

    def test_junk_manifest_retains_sound_files_but_reports_defects(self):
        exchange, main_path = self._exchange_expecting_an_artifact()
        # A manifest shaped the way ChatGPT actually returned it: main JSON listed
        # as its own artifact, no per-file status, prose in delivery.
        payload = {
            "request_id": "request_declared",
            "status": "COMPLETE",
            "artifacts_manifest": [
                {"filename": "declared_result.json", "role": "main"},
                {"filename": "declared_outputs.zip", "role": "archive"},
            ],
            "delivery": ["declared_result.json", "declared_outputs.zip containing x"],
        }
        main_path.write_text(json.dumps(payload), encoding="utf-8")
        (exchange / "response" / "declared_outputs.zip").write_bytes(
            self.valid_zip_bytes()
        )

        report = validate_response(exchange)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["manifest_verified"])
        self.assertEqual(report["response_status"], "COMPLETE")
        self.assertTrue(report["invalid_files"])
        self.assertTrue(main_path.is_file())
        self.assertTrue((exchange / "response" / "declared_outputs.zip").is_file())

    def test_junk_manifest_with_missing_artifact_is_still_incomplete(self):
        exchange, main_path = self._exchange_expecting_an_artifact()
        payload = {
            "request_id": "request_declared",
            "status": "COMPLETE",
            "artifacts_manifest": [{"filename": "declared_result.json", "role": "main"}],
            "delivery": ["declared_result.json"],
        }
        main_path.write_text(json.dumps(payload), encoding="utf-8")

        report = validate_response(exchange)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("declared_outputs.zip", report["missing_files"])
        self.assertFalse(report["manifest_verified"])

    def test_junk_manifest_with_corrupt_archive_is_invalid(self):
        exchange, main_path = self._exchange_expecting_an_artifact()
        payload = {
            "request_id": "request_declared",
            "status": "COMPLETE",
            "artifacts_manifest": [{"filename": "x", "role": "archive"}],
            "delivery": ["declared_result.json"],
        }
        main_path.write_text(json.dumps(payload), encoding="utf-8")
        (exchange / "response" / "declared_outputs.zip").write_bytes(b"not a real zip")

        report = validate_response(exchange)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("declared_outputs.zip", report["invalid_files"])

    def test_broken_main_json_still_invalid_under_fallback(self):
        exchange, main_path = self._exchange_expecting_an_artifact()
        main_path.write_text(
            json.dumps({"request_id": "wrong", "status": "COMPLETE"}), encoding="utf-8"
        )
        (exchange / "response" / "declared_outputs.zip").write_bytes(
            self.valid_zip_bytes()
        )

        report = validate_response(exchange)

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("declared_result.json", report["invalid_files"])

    def test_strict_manifest_stays_verified(self):
        archive = self.delivery_archive()
        handle_completed_download(self.root, self.completed(3, archive))

        report = validate_response(self.exchange)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertTrue(report["manifest_verified"])

    def _exchange_expecting_an_artifact(self):
        """A prepared exchange that declares expected_artifacts.

        The shared fixture declares none, so the backstop it guards never runs
        there. A test of that backstop needs an exchange that actually expects
        something.
        """
        request = self.write_source(
            "declared_request.json", {"request_id": "request_declared"}
        )
        schema = self.write_source("declared_schema.json", {"type": "object"})
        spec = {
            "subject": "Declared artifact call",
            "request_id": "request_declared",
            "expected_main_json": "declared_result.json",
            "expected_artifacts": ["declared_outputs.zip"],
            "prompt_text": "Return files only.\n",
            "input_files": [
                {"path": str(request), "filename": "WEB_REVIEW_REQUEST.json"},
                {"path": str(schema), "filename": "WEB_RESPONSE_SCHEMA.json"},
            ],
        }
        manifest = prepare_call(
            self.root,
            spec,
            datetime(2026, 7, 14, 16, 0, 0, tzinfo=timezone(timedelta(hours=-4))),
        )
        exchange = self.root / "calls" / manifest["exchange_id"]
        (exchange / "response").mkdir(parents=True, exist_ok=True)
        # validate_response reads the expected main name from the manifest, so
        # the caller writes under whatever name was declared rather than
        # guessing one.
        return exchange, exchange / "response" / manifest["expected_main_json"]


if __name__ == "__main__":
    unittest.main()
