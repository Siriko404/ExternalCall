"""The watch on the Downloads folder: a call finishes itself once its archive lands.

Done was a button held back until the archive was in. The archive being in was
the whole decision, so nothing is left for a person to press. What that removes
is the judgement a person brought to it, so these pin the judgement instead: a
call is closed only when its own archive is really, completely there.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from companion.core import load_active_call, prepare_call, start_call
from companion.downloads import finish_landed_calls, handle_completed_download
from companion.jobs import prepare_job
from companion.native_host import dispatch
from scripts.package_response import package


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "ExternalCall"
        self.downloads = self.base / "Downloads"
        self.downloads.mkdir()
        self.tab = 40

    def tearDown(self):
        self.temp.cleanup()

    def running(self, title="Review fixture"):
        """One prepared and started call, as the panel leaves it after Go."""
        job = {
            "project": "fixture",
            "title": title, "purpose": "Choose the next change.",
            "task": "Evaluate the supplied evidence.", "scope": {"in": ["Review"], "out": ["Edit"]},
            "constraints": ["No product edits."], "sources": [], "stop_condition": "Report limitations.",
            "requirements": [{"id": "R1", "requirement": "Give evidence for the recommendation.",
                              "acceptance": {"condition": "Report supports the recommendation.",
                                             "method": "review", "evidence": ["report.md"], "reviewer": "terminal"}}],
            "deliverables": [{"filename": "report.md", "description": "Recommendation with evidence."}],
            "result_schema": {"type": "object", "required": ["summary"],
                              "properties": {"summary": {"type": "string"}}},
        }
        manifest = prepare_job(self.root, job, datetime.now(timezone.utc))
        self.tab += 1
        start_call(self.root, manifest["exchange_id"], self.tab, [])
        return manifest, self.root / "calls" / manifest["exchange_id"]

    def answer(self, exchange, into):
        """The archive a responder returns, written where a browser would save it."""
        outputs = self.base / f"outputs-{exchange.name}"
        outputs.mkdir()
        (outputs / "report.md").write_bytes(b"supported finding\n")
        payload = {"results": {"summary": "Supported finding."},
                   "criteria_report": [{"id": "R1", "status": "MET", "evidence": "report.md"}],
                   "limitations": []}
        return package(exchange / "request" / "WEB_REVIEW_REQUEST.json", payload, outputs, into, "COMPLETE")

    def state(self, exchange):
        return json.loads((exchange / "EXCHANGE_MANIFEST.json").read_text())["state"]

    def collect(self, **options):
        options.setdefault("settle_seconds", 0)
        return finish_landed_calls(self.root, self.downloads, **options)

    def test_a_landed_archive_finishes_its_call_with_no_one_pressing_done(self):
        manifest, exchange = self.running()
        landed = self.answer(exchange, self.downloads)

        result = self.collect()

        self.assertEqual(result["finished"], [{"exchange_id": manifest["exchange_id"], "status": "COMPLETE"}])
        self.assertEqual(result["waiting"], [])
        self.assertEqual(self.state(exchange), "COMPLETE")
        self.assertIsNone(load_active_call(self.root, manifest["exchange_id"]))
        self.assertTrue((exchange / "validation" / "VALIDATION_REPORT.json").is_file())
        # Collected by copy: the Downloads folder keeps what the browser wrote.
        self.assertTrue(landed.is_file())

    def test_a_call_whose_archive_has_not_landed_is_left_running(self):
        """Finishing it would validate nothing and close a running call as a
        failed delivery, which is the one thing an automatic Done must not do."""
        manifest, exchange = self.running()
        (self.downloads / "someone_elses_outputs.zip").write_bytes(b"unrelated")

        result = self.collect()

        self.assertEqual(result, {"finished": [], "waiting": [manifest["exchange_id"]], "failed": []})
        self.assertEqual(self.state(exchange), "ACTIVE")
        self.assertTrue(load_active_call(self.root, manifest["exchange_id"])["monitoring"])
        self.assertFalse((exchange / "validation" / "VALIDATION_REPORT.json").exists())

    def test_a_download_still_in_flight_is_not_taken_for_the_archive(self):
        manifest, exchange = self.running()
        archive = manifest["expected_artifacts"][0]
        (self.downloads / f"{archive}.crdownload").write_bytes(b"half of it")

        self.assertEqual(self.collect()["waiting"], [manifest["exchange_id"]])
        self.assertEqual(self.state(exchange), "ACTIVE")

    def test_a_file_still_being_written_is_given_time_to_settle(self):
        """A file copied in by hand grows where it stands. Read too early it is
        a truncated archive, and the call would be closed as a faulty delivery."""
        manifest, exchange = self.running()
        landed = self.answer(exchange, self.downloads)
        written = landed.stat().st_mtime

        early = self.collect(settle_seconds=2.0, clock=lambda: written + 0.5)
        self.assertEqual(early["waiting"], [manifest["exchange_id"]])
        self.assertEqual(self.state(exchange), "ACTIVE")

        later = self.collect(settle_seconds=2.0, clock=lambda: written + 2.5)
        self.assertEqual([item["status"] for item in later["finished"]], ["COMPLETE"])

    def test_a_faulty_archive_finishes_the_call_as_incomplete(self):
        manifest, exchange = self.running()
        (self.downloads / manifest["expected_artifacts"][0]).write_bytes(b"not an archive at all")

        result = self.collect()

        self.assertEqual(result["finished"], [{"exchange_id": manifest["exchange_id"], "status": "INCOMPLETE"}])
        self.assertEqual(self.state(exchange), "INCOMPLETE")
        # The bytes are kept as evidence for the correction round.
        self.assertTrue((exchange / "response" / manifest["expected_artifacts"][0]).is_file())

    def test_only_the_call_whose_archive_landed_is_finished(self):
        first, first_dir = self.running("First call")
        second, second_dir = self.running("Second call")
        self.answer(second_dir, self.downloads)

        result = self.collect()

        self.assertEqual([item["exchange_id"] for item in result["finished"]], [second["exchange_id"]])
        self.assertEqual(result["waiting"], [first["exchange_id"]])
        self.assertEqual(self.state(first_dir), "ACTIVE")
        self.assertEqual(self.state(second_dir), "COMPLETE")

    def test_an_archive_already_filed_but_never_finished_is_finished(self):
        """The browser's own event files the archive and then asks for Done. If
        the extension dies between the two, the call must not hang forever."""
        manifest, exchange = self.running()
        staged = self.base / "staged"
        staged.mkdir()
        landed = self.answer(exchange, staged)
        filed = handle_completed_download(self.root, {"id": 7, "filename": str(landed), "state": "complete"})
        self.assertEqual(filed["status"], "MOVED")
        self.assertEqual(self.state(exchange), "ACTIVE")

        result = self.collect()

        self.assertEqual([item["status"] for item in result["finished"]], ["COMPLETE"])
        self.assertEqual(self.state(exchange), "COMPLETE")

    def test_collecting_twice_finishes_a_call_once(self):
        manifest, exchange = self.running()
        self.answer(exchange, self.downloads)

        self.assertEqual(len(self.collect()["finished"]), 1)
        self.assertEqual(self.collect(), {"finished": [], "waiting": [], "failed": []})

    def test_a_call_with_a_hand_chosen_name_is_not_collected_from_the_folder(self):
        """Its filename is not unique to the attempt, so a file under that name
        may be a leftover from an earlier call that used it."""
        sources = self.base / "sources"
        sources.mkdir()
        (sources / "request.json").write_text(json.dumps({"request_id": "old_request"}))
        (sources / "schema.json").write_text(json.dumps({"type": "object"}))
        manifest = prepare_call(self.root, {
            "subject": "Old style", "request_id": "old_request",
            "expected_main_json": "old_response.json", "expected_artifacts": ["old_outputs.zip"],
            "prompt_text": "Return the files.",
            "input_files": [
                {"path": str(sources / "request.json"), "filename": "WEB_REVIEW_REQUEST.json"},
                {"path": str(sources / "schema.json"), "filename": "WEB_RESPONSE_SCHEMA.json"},
            ],
        }, datetime.now(timezone.utc))
        start_call(self.root, manifest["exchange_id"], 77, [])
        (self.downloads / "old_outputs.zip").write_bytes(b"left over")

        result = self.collect()

        self.assertEqual(result["finished"], [])
        self.assertEqual(result["waiting"], [manifest["exchange_id"]])

    def test_the_panel_reaches_it_through_the_native_host(self):
        manifest, exchange = self.running()
        landed = self.answer(exchange, self.downloads)
        written = landed.stat().st_mtime
        message = {"protocol_version": 1, "command": "calls.collect", "payload": {}}

        with patch.dict(os.environ, {"EXTERNALCALL_DOWNLOADS_DIR": str(self.downloads)}):
            with self.assertRaisesRegex(ValueError, "payload fields do not match"):
                dispatch(self.root, {**message, "payload": {"exchange_id": manifest["exchange_id"]}})
            # The host keeps the settling time: a file written this instant waits.
            with patch("companion.downloads.time.time", return_value=written + 0.5):
                self.assertEqual(dispatch(self.root, message)["waiting"], [manifest["exchange_id"]])
            with patch("companion.downloads.time.time", return_value=written + 5):
                result = dispatch(self.root, message)

        self.assertEqual(result["finished"], [{"exchange_id": manifest["exchange_id"], "status": "COMPLETE"}])


if __name__ == "__main__":
    unittest.main()
