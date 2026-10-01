import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import load_active_call, prepare_call, start_call
from companion.downloads import finish_call, handle_completed_download, ingest_from_downloads
from companion.repair import build_repair_prompt, collect_defects, open_repair_round


class RepairTests(unittest.TestCase):
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
        schema = self.write_source("WEB_RESPONSE_SCHEMA.json", {"type": "object"})
        spec = {
            "subject": "Fixture call",
            "request_id": "request_fixture",
            "expected_main_json": "result.json",
            "expected_artifacts": ["fixture_outputs.zip"],
            "prompt_text": "Return files only.\n",
            "input_files": [
                {"path": str(request), "filename": request.name},
                {"path": str(schema), "filename": schema.name},
            ],
        }
        now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        self.manifest = prepare_call(self.root, spec, now)
        self.exchange_id = self.manifest["exchange_id"]
        start_call(self.root, self.exchange_id, 11, [])
        self.exchange = self.root / "calls" / self.exchange_id
        self.report_bytes = b"report"
        self.report_digest = hashlib.sha256(self.report_bytes).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, name, value):
        path = self.sources / name
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
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
        artifact_size=None,
        delivery=None,
        limitations=None,
    ):
        manifest = []
        names = ["fixture_outputs.zip"]
        if artifacts:
            manifest.append(
                {
                    "filename": "report.md",
                    "status": "CREATED",
                    "media_type": "text/markdown",
                    "size": len(self.report_bytes) if artifact_size is None else artifact_size,
                    "sha256": artifact_digest or self.report_digest,
                }
            )
            names.append("report.md")
        body = {
            "request_id": request_id,
            "status": status,
            "artifacts_manifest": manifest,
            "delivery": names if delivery is None else delivery,
        }
        if limitations is not None:
            body["limitations"] = limitations
        return (json.dumps(body) + "\n").encode("utf-8")

    def completed(self, download_id, path):
        return {"id": download_id, "filename": str(path), "state": "complete"}

    def deliver(self, download_id=3, *, main=None, report=None, name="fixture_outputs.zip"):
        """Deliver the one archive a call comes back as."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(
                "result.json", main if main is not None else self.main_json_bytes()
            )
            if report is not None:
                bundle.writestr("report.md", report)
        archive = self.download(name, buffer.getvalue())
        return handle_completed_download(self.root, self.completed(download_id, archive))

    def deliver_main_only(self):
        """The archive arrives, but the member it promised is not inside it."""
        self.deliver()

    def test_missing_artifact_is_reported_with_its_name(self):
        self.deliver_main_only()
        finish_call(self.root)

        defects = collect_defects(self.exchange)

        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["kind"], "ARTIFACT_MISSING")
        self.assertEqual(defects[0]["target"], "report.md")

    def test_missing_main_json_is_the_only_defect_reported(self):
        finish_call(self.root)

        defects = collect_defects(self.exchange)

        self.assertEqual({defect["kind"] for defect in defects}, {"MAIN_MISSING", "EXPECTED_ARTIFACT_ABSENT"})
        self.assertIn("result.json", [d["target"] for d in defects])

    def test_hash_mismatch_reports_declared_and_actual_digests(self):
        wrong_digest = hashlib.sha256(b"different").hexdigest()
        self.deliver(
            main=self.main_json_bytes(artifact_digest=wrong_digest),
            report=self.report_bytes,
        )
        finish_call(self.root)

        defects = collect_defects(self.exchange)

        kinds = [defect["kind"] for defect in defects]
        self.assertIn("ARTIFACT_HASH_MISMATCH", kinds)
        mismatch = defects[kinds.index("ARTIFACT_HASH_MISMATCH")]
        self.assertIn(wrong_digest, mismatch["expected"])
        self.assertIn(self.report_digest, mismatch["observed"])

    def test_an_intact_partial_delivery_has_no_defects_to_repair(self):
        """A repair round fixes a broken delivery, not an honest answer.

        This test previously asserted the opposite, and that assertion was the
        bug: PARTIAL is the responder's account of its own work, which these
        prompts explicitly ask for, and validate_response already stopped
        treating it as a delivery fault. collect_defects still did, so the two
        commands gave opposite verdicts on the same bytes and the operator was
        told to spend a correction round on a flawless delivery. Where the work
        itself is inadequate the remedy is a new correction call with a new
        request ID, not a repair round.
        """
        self.deliver(
            main=self.main_json_bytes(
                status="PARTIAL", artifacts=False, limitations=["ran out of context"]
            )
        )
        report = finish_call(self.root)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["response_status"], "PARTIAL")
        self.assertEqual(collect_defects(self.exchange), [])

    def test_delivery_omission_is_reported(self):
        """A file that came down on its own must be named in delivery.

        The archive's members must not be: they were never downloadable. This
        fixture places report.md in the response directory as its own file, so
        it is one of the few things delivery is still required to name.
        """
        # The strict move gate refuses this main JSON, so it only reaches the
        # response directory through the manual fallback path.
        (self.exchange / "response" / "result.json").write_bytes(
            self.main_json_bytes(delivery=["fixture_outputs.zip"])
        )
        (self.exchange / "response" / "report.md").write_bytes(self.report_bytes)
        (self.exchange / "response" / "fixture_outputs.zip").write_bytes(
            self._empty_zip()
        )
        finish_call(self.root)

        defects = collect_defects(self.exchange)

        self.assertEqual([defect["kind"] for defect in defects], ["DELIVERY_INCOMPLETE"])
        self.assertIn("report.md", defects[0]["observed"])

    def _empty_zip(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("placeholder.txt", "x")
        return buffer.getvalue()

    def test_complete_delivery_has_no_defects(self):
        self.deliver(report=self.report_bytes)
        report = finish_call(self.root)

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(collect_defects(self.exchange), [])

    def test_a_main_json_that_lists_itself_is_named_as_that_error(self):
        # A model that lists the main JSON among its own artifacts declares a
        # digest it cannot possibly compute. Reporting that as a hash mismatch
        # would ask for an impossible correction and never converge.
        body = {
            "request_id": "request_fixture",
            "status": "COMPLETE",
            "artifacts_manifest": [
                {
                    "filename": "result.json",
                    "status": "CREATED",
                    "media_type": "application/json",
                    "size": 10,
                    "sha256": "0" * 64,
                }
            ],
            "delivery": ["result.json"],
        }
        (self.exchange / "response" / "result.json").write_bytes(
            (json.dumps(body) + "\n").encode("utf-8")
        )
        finish_call(self.root)

        defects = collect_defects(self.exchange)

        kinds = [defect["kind"] for defect in defects]
        self.assertIn("MAIN_JSON_LISTED_AS_ARTIFACT", kinds)
        self.assertNotIn("ARTIFACT_HASH_MISMATCH", kinds)
        entry = defects[kinds.index("MAIN_JSON_LISTED_AS_ARTIFACT")]
        self.assertIn("Remove that entry", entry["observed"])

    def test_prompt_names_every_defect_and_keeps_the_request_id(self):
        self.deliver_main_only()
        finish_call(self.root)
        defects = collect_defects(self.exchange)

        prompt = build_repair_prompt(self.exchange, defects, 1)

        self.assertIn("CORRECTION ROUND 1", prompt)
        self.assertIn("request_fixture", prompt)
        self.assertIn("ARTIFACT_MISSING", prompt)
        self.assertIn("report.md", prompt)
        self.assertNotIn("—", prompt)

    def test_open_repair_round_rearms_monitoring_and_writes_round_files(self):
        self.deliver_main_only()
        finish_call(self.root)
        self.assertIsNone(load_active_call(self.root))

        result = open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[7])

        self.assertEqual(result["round"], 1)
        self.assertTrue(Path(result["prompt_path"]).is_file())
        self.assertTrue(Path(result["defects_path"]).is_file())
        active = load_active_call(self.root)
        self.assertEqual(active["exchange_id"], self.exchange_id)
        self.assertEqual(active["tab_id"], 42)
        self.assertEqual(active["repair_round"], 1)
        self.assertTrue(active["monitoring"])
        manifest = json.loads(
            (self.exchange / "EXCHANGE_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["state"], "ACTIVE")
        self.assertEqual(manifest["repair_round"], 1)
        self.assertEqual(manifest["repairs"][0]["defect_kinds"], ["ARTIFACT_MISSING"])

    def test_repair_round_supersedes_the_earlier_main_json(self):
        wrong_digest = hashlib.sha256(b"different").hexdigest()
        self.deliver(
            main=self.main_json_bytes(artifact_digest=wrong_digest),
            report=self.report_bytes,
        )
        finish_call(self.root)
        original = (self.exchange / "response" / "result.json").read_bytes()

        open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[])
        result = self.deliver(9, report=self.report_bytes)

        self.assertEqual(result["status"], "MOVED")
        self.assertEqual(
            (self.exchange / "response" / "result.json").read_bytes(),
            self.main_json_bytes(),
        )
        archived = self.exchange / "response" / "superseded" / "round1" / "result.json"
        self.assertEqual(archived.read_bytes(), original)
        self.assertEqual(finish_call(self.root)["status"], "COMPLETE")

    def test_manual_repair_replaces_archive_and_envelope_preserving_original(self):
        self.deliver(main=self.main_json_bytes(artifact_digest="0" * 64), report=self.report_bytes)
        finish_call(self.root)
        original = (self.exchange / "response" / "result.json").read_bytes()
        open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[])
        with zipfile.ZipFile(self.downloads / "fixture_outputs.zip", "w") as archive:
            archive.writestr("result.json", self.main_json_bytes())
            archive.writestr("report.md", self.report_bytes)
        result = ingest_from_downloads(self.root, self.exchange_id, self.downloads)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(finish_call(self.root)["status"], "COMPLETE")
        saved = self.exchange / "response" / "superseded" / "round1" / "result.json"
        self.assertEqual(saved.read_bytes(), original)

    def test_repair_is_refused_when_nothing_is_wrong(self):
        self.deliver(report=self.report_bytes)
        finish_call(self.root)

        with self.assertRaisesRegex(RuntimeError, "no validation defects"):
            open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[])

    def test_second_round_increments_and_keeps_the_first_round_files(self):
        self.deliver_main_only()
        finish_call(self.root)
        open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[])
        finish_call(self.root)

        second = open_repair_round(self.root, self.exchange_id, tab_id=42, download_baseline=[])

        self.assertEqual(second["round"], 2)
        self.assertTrue((self.exchange / "repair" / "ROUND_1_PROMPT.txt").is_file())
        self.assertTrue((self.exchange / "repair" / "ROUND_2_PROMPT.txt").is_file())
        manifest = json.loads(
            (self.exchange / "EXCHANGE_MANIFEST.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["repairs"]), 2)


if __name__ == "__main__":
    unittest.main()
