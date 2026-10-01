import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import (
    call_progress,
    list_ready_calls,
    list_recent_calls,
    load_active_call,
    load_active_calls,
    prepare_call,
    resume_call,
    start_call,
    stop_call,
)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ExternalCall"
        self.sources = Path(self.temp.name) / "sources"
        self.sources.mkdir()
        self.now = datetime(
            2026, 7, 14, 15, 15, 0, tzinfo=timezone(timedelta(hours=-4))
        )
        self.request = self.write(
            "WEB_REVIEW_REQUEST.json", {"request_id": "request_fixture"}
        )
        self.schema = self.write(
            "WEB_RESPONSE_SCHEMA.json", {"type": "object"}
        )
        self.context = self.write("context.txt", "context\n")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        path = self.sources / name
        if isinstance(value, dict):
            path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        else:
            path.write_text(value, encoding="utf-8")
        return path

    def spec(self, subject="Fixture call", expected_main_json="result.json"):
        # One archive comes back and the main JSON is inside it, so every call
        # names exactly one .zip. Derived from the main name here so two
        # fixtures never collide on the reserved deliverable name.
        return {
            "subject": subject,
            "request_id": "request_fixture",
            "expected_main_json": expected_main_json,
            "expected_artifacts": [f"{Path(expected_main_json).stem}_outputs.zip"],
            "prompt_text": "Follow this request and return files only.\n",
            "input_files": [
                {"path": str(self.request), "filename": self.request.name},
                {"path": str(self.schema), "filename": self.schema.name},
                {"path": str(self.context), "filename": self.context.name},
            ],
        }

    def test_progress_counts_downloads_and_not_the_main_json(self):
        """The panel's checklist is what the operator waits on.

        It listed the main JSON too, which under one-zip-out is a file nobody
        downloads: it is written out of the archive when the archive lands. The
        operator was shown two files to wait for, one of which would never
        arrive on its own, beside a checklist that could not complete until it
        silently appeared.
        """
        manifest = prepare_call(self.root, self.spec(), self.now)
        start_call(self.root, manifest["exchange_id"], 11, [])

        [call] = call_progress(self.root)

        self.assertEqual(
            [item["filename"] for item in call["files"]], ["result_outputs.zip"]
        )
        self.assertEqual(call["expected"], 1)
        self.assertEqual(call["received"], 0)
        self.assertFalse(call["complete"])

        response = self.root / "calls" / manifest["exchange_id"] / "response"
        response.mkdir(parents=True, exist_ok=True)
        (response / "result_outputs.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

        [landed] = call_progress(self.root)
        self.assertTrue(landed["complete"])
        self.assertEqual(landed["received"], 1)

    def test_prepare_call_uses_one_timestamp_for_the_folder(self):
        manifest = prepare_call(self.root, self.spec(), self.now)

        self.assertEqual(
            manifest["exchange_id"], "2026-07-14_151500_fixture_call"
        )
        self.assertEqual(manifest["state"], "PREPARED")
        self.assertEqual(manifest["request_id"], "request_fixture")
        names = [item["filename"] for item in manifest["request_files"]]
        self.assertEqual(
            names,
            [
                "000_READ_ME_FIRST.md",
                "WEB_REVIEW_REQUEST.json",
                "WEB_RESPONSE_SCHEMA.json",
                "context.txt",
                "fixture_call_inputs.zip",
            ],
        )
        exchange = self.root / "calls" / manifest["exchange_id"]
        for item in manifest["request_files"]:
            stored = exchange / "request" / item["filename"]
            self.assertTrue(stored.is_file())
            self.assertEqual(item["size"], stored.stat().st_size)
            self.assertEqual(
                item["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest()
            )
        self.assertTrue((exchange / "response").is_dir())
        self.assertTrue((exchange / "validation").is_dir())

    def test_prepare_validates_request_identity(self):
        self.request.write_text('{"request_id":"wrong"}\n', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "request_id"):
            prepare_call(self.root, self.spec(), self.now)

    def test_prepare_rejects_invalid_governing_json(self):
        self.request.write_text("{not-json}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "WEB_REVIEW_REQUEST"):
            prepare_call(self.root, self.spec(), self.now)

    def test_prepare_rejects_invalid_response_schema_json(self):
        self.schema.write_text("{not-json}\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "WEB_RESPONSE_SCHEMA"):
            prepare_call(self.root, self.spec(), self.now)

    def test_prepare_rejects_missing_input(self):
        self.context.unlink()

        with self.assertRaisesRegex(FileNotFoundError, "context.txt"):
            prepare_call(self.root, self.spec(), self.now)

    def test_prepare_rejects_duplicate_packaged_names(self):
        spec = self.spec()
        spec["input_files"].append(
            {"path": str(self.context), "filename": "CONTEXT.txt"}
        )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            prepare_call(self.root, spec, self.now)

    def test_prepare_rejects_caller_supplied_prompt(self):
        spec = self.spec()
        spec["input_files"].append(
            {
                "path": str(self.context),
                "filename": "PROMPT_2026-07-14_151500.md",
            }
        )

        with self.assertRaisesRegex(ValueError, "PROMPT"):
            prepare_call(self.root, spec, self.now)

    def test_prepare_rejects_unsafe_subject_and_filename(self):
        with self.assertRaisesRegex(ValueError, "subject"):
            prepare_call(self.root, self.spec("../escape"), self.now)

        spec = self.spec()
        spec["input_files"][0]["filename"] = "../request.json"
        with self.assertRaisesRegex(ValueError, "filename"):
            prepare_call(self.root, spec, self.now)

    def test_list_ready_calls_and_single_active_call(self):
        first = prepare_call(self.root, self.spec("First"), self.now)
        second = prepare_call(
            self.root,
            self.spec("Second", expected_main_json="second_result.json"),
            self.now + timedelta(seconds=1),
        )

        ready = list_ready_calls(self.root)
        self.assertEqual(
            [item["exchange_id"] for item in ready],
            [first["exchange_id"], second["exchange_id"]],
        )


        active = start_call(self.root, first["exchange_id"], 11, [2, 4])
        self.assertTrue(active["monitoring"])
        self.assertEqual(active["tab_id"], 11)
        self.assertEqual(active["download_baseline"], [2, 4])
        self.assertEqual(load_active_call(self.root), active)
        with self.assertRaisesRegex(RuntimeError, "not prepared"):
            start_call(self.root, first["exchange_id"], 12, [])

        remaining = list_ready_calls(self.root)
        self.assertEqual(
            [item["exchange_id"] for item in remaining], [second["exchange_id"]]
        )

    def test_two_calls_run_at_once_when_their_main_json_names_differ(self):
        first = prepare_call(self.root, self.spec(subject="First"), self.now)
        second = prepare_call(
            self.root,
            self.spec(subject="Second", expected_main_json="second_result.json"),
            self.now + timedelta(minutes=1),
        )

        start_call(self.root, first["exchange_id"], 11, [])
        start_call(self.root, second["exchange_id"], 12, [])

        running = load_active_calls(self.root)
        self.assertEqual(
            sorted(item["exchange_id"] for item in running),
            sorted([first["exchange_id"], second["exchange_id"]]),
        )
        self.assertEqual(
            load_active_call(self.root, second["exchange_id"])["tab_id"], 12
        )

    def test_start_refuses_a_name_collision_introduced_after_preparation(self):
        # prepare_call already refuses colliding names, so this backstop only
        # fires when a manifest is edited on disk after it was prepared.
        first = prepare_call(self.root, self.spec(subject="First"), self.now)
        second = prepare_call(
            self.root,
            self.spec(subject="Second", expected_main_json="second_result.json"),
            self.now + timedelta(minutes=1),
        )
        start_call(self.root, first["exchange_id"], 11, [])
        manifest_path = (
            self.root / "calls" / second["exchange_id"] / "EXCHANGE_MANIFEST.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["expected_main_json"] = "result.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "already running and expects"):
            start_call(self.root, second["exchange_id"], 12, [])

    def test_a_second_call_cannot_reuse_a_bound_tab(self):
        first = prepare_call(self.root, self.spec(subject="First"), self.now)
        second = prepare_call(
            self.root,
            self.spec(subject="Second", expected_main_json="second_result.json"),
            self.now + timedelta(minutes=1),
        )
        start_call(self.root, first["exchange_id"], 11, [])

        with self.assertRaisesRegex(RuntimeError, "tab 11 is already bound"):
            start_call(self.root, second["exchange_id"], 11, [])

    def test_load_active_call_refuses_to_guess_between_running_calls(self):
        first = prepare_call(self.root, self.spec(subject="First"), self.now)
        second = prepare_call(
            self.root,
            self.spec(subject="Second", expected_main_json="second_result.json"),
            self.now + timedelta(minutes=1),
        )
        start_call(self.root, first["exchange_id"], 11, [])
        start_call(self.root, second["exchange_id"], 12, [])

        with self.assertRaisesRegex(RuntimeError, "several calls are active"):
            load_active_call(self.root)

    def test_stopping_one_call_leaves_the_other_running(self):
        first = prepare_call(self.root, self.spec(subject="First"), self.now)
        second = prepare_call(
            self.root,
            self.spec(subject="Second", expected_main_json="second_result.json"),
            self.now + timedelta(minutes=1),
        )
        start_call(self.root, first["exchange_id"], 11, [])
        start_call(self.root, second["exchange_id"], 12, [])

        stop_call(self.root, first["exchange_id"])

        running = load_active_calls(self.root)
        self.assertEqual([item["exchange_id"] for item in running], [second["exchange_id"]])

    def test_a_legacy_single_call_record_is_migrated(self):
        call = prepare_call(self.root, self.spec(), self.now)
        active = start_call(self.root, call["exchange_id"], 11, [1])
        (self.root / "state" / "active" / f"{call['exchange_id']}.json").unlink()
        legacy = self.root / "state" / "ACTIVE_CALL.json"
        legacy.write_text(json.dumps(active) + "\n", encoding="utf-8")

        running = load_active_calls(self.root)

        self.assertEqual([item["exchange_id"] for item in running], [call["exchange_id"]])
        self.assertFalse(legacy.exists())

    def test_stop_call_records_stop_before_clearing_active_state(self):
        call = prepare_call(self.root, self.spec(), self.now)
        start_call(self.root, call["exchange_id"], 11, [])

        stopped = stop_call(self.root)

        self.assertEqual(stopped["state"], "STOPPED")
        self.assertIsNone(load_active_call(self.root))
        manifest = json.loads(
            (self.root / "calls" / call["exchange_id"] / "EXCHANGE_MANIFEST.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["state"], "STOPPED")

    def test_resume_call_rebinds_the_active_exchange_without_resending(self):
        call = prepare_call(self.root, self.spec(), self.now)
        active = start_call(self.root, call["exchange_id"], 11, [1, 2])
        active["observed_download_ids"] = [3]
        (self.root / "state" / "active" / f"{call['exchange_id']}.json").write_text(
            json.dumps(active) + "\n", encoding="utf-8"
        )

        resumed = resume_call(self.root, 22, [1, 2, 3, 4])

        self.assertEqual(resumed["tab_id"], 22)
        self.assertEqual(resumed["download_baseline"], [1, 2, 3, 4])
        self.assertEqual(resumed["observed_download_ids"], [3])
        self.assertTrue(resumed["monitoring"])
        self.assertEqual(resumed["exchange_id"], call["exchange_id"])

    def test_the_archive_row_reports_the_delivery_and_not_the_work(self):
        """The panel reports on the pipe. What the responder said about its own
        work is in the report on disk, which the operator reads; a row that
        relayed it turned an intact delivery into one that looks failed."""
        call = prepare_call(self.root, self.spec(), self.now)
        exchange = self.root / "calls" / call["exchange_id"]
        manifest_path = exchange / "EXCHANGE_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state"] = "COMPLETE"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (exchange / "validation").mkdir(exist_ok=True)
        (exchange / "validation" / "VALIDATION_REPORT.json").write_text(
            json.dumps({"status": "COMPLETE", "response_status": "BLOCKED"}) + "\n",
            encoding="utf-8",
        )

        [row] = list_recent_calls(self.root)

        self.assertEqual(row["state"], "COMPLETE")
        self.assertNotIn("response_status", row)
        self.assertNotIn("manifest_verified", row)

    def test_the_archive_is_not_a_window_twelve_calls_wide(self):
        """The panel presents this list as every past call, so twelve was a
        quiet truncation. It is still bounded: the native-messaging frame fails
        the whole command rather than truncating when it overflows."""
        for index in range(14):
            spec = self.spec()
            spec["subject"] = f"Call number {index}"
            spec["expected_artifacts"] = [f"call_{index}_outputs.zip"]
            spec["expected_main_json"] = f"call_{index}.json"
            prepare_call(
                self.root, spec, self.now + timedelta(minutes=index)
            )

        rows = list_recent_calls(self.root)

        self.assertEqual(len(rows), 14)
        self.assertEqual(len(list_recent_calls(self.root, limit=5)), 5)


if __name__ == "__main__":
    unittest.main()
