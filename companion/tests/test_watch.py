"""The waiter exists so a coding-agent session can hear a call end.

Nothing can push into a running session, but a session can start a process and
be re-entered when it exits — so the process exit is the event, and everything
here pins what that exit means. The failure record exists for the one ending
that used to leave no durable trace: a completed download the extension could
not file.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from companion.cli import run
from companion.core import (
    delete_call,
    inspect_call,
    load_active_call,
    prepare_call,
    start_call,
    stop_call,
)
from companion.downloads import finish_call
from companion.lock import state_lock
from companion.repair import open_repair_round
from companion.watch import (
    WaitInconsistency,
    _snapshot,
    record_download_failure,
    wait_for_event,
)


class WatchFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        sources = base / "sources"
        sources.mkdir()
        request = sources / "WEB_REVIEW_REQUEST.json"
        request.write_text('{"request_id":"request_watch"}\n', encoding="utf-8")
        schema = sources / "WEB_RESPONSE_SCHEMA.json"
        schema.write_text('{"type":"object"}\n', encoding="utf-8")
        self.manifest = prepare_call(
            self.root,
            {
                "subject": "Watch fixture",
                "request_id": "request_watch",
                "expected_main_json": "watch_result.json",
                "expected_artifacts": ["watch_outputs.zip"],
                "prompt_text": "Return files only.\n",
                "input_files": [
                    {"path": str(request), "filename": request.name},
                    {"path": str(schema), "filename": schema.name},
                ],
            },
            datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc),
        )
        self.exchange_id = self.manifest["exchange_id"]

    def tearDown(self):
        self.temp.cleanup()

    def scripted_sleep(self, actions):
        """A fake sleep that mutates the filesystem at chosen poll numbers."""
        state = {"n": 0}

        def sleep(_seconds):
            state["n"] += 1
            action = actions.get(state["n"])
            if action:
                action()

        return sleep


class WaitEventTests(WatchFixture):
    def test_a_finished_call_is_reported_immediately_without_polling(self):
        start_call(self.root, self.exchange_id, 5, [])
        finish_call(self.root, self.exchange_id)

        def never_sleep(_seconds):
            raise AssertionError("an already-terminal call must not poll")

        result = wait_for_event(
            self.root, self.exchange_id, sleep=never_sleep
        )

        self.assertEqual(result["event"], "INCOMPLETE")
        self.assertEqual(result["state"], "INCOMPLETE")
        self.assertIsNotNone(result["validation_report"])
        self.assertIsNone(result["attention_record"])

    def test_a_stopped_call_reports_stopped_not_success(self):
        start_call(self.root, self.exchange_id, 5, [])
        stop_call(self.root, self.exchange_id)

        result = wait_for_event(self.root, self.exchange_id)

        self.assertEqual(result["event"], "STOPPED")

    def test_the_wait_ends_when_the_call_does(self):
        start_call(self.root, self.exchange_id, 5, [])
        sleep = self.scripted_sleep(
            {2: lambda: finish_call(self.root, self.exchange_id)}
        )

        result = wait_for_event(self.root, self.exchange_id, sleep=sleep)

        self.assertEqual(result["event"], "INCOMPLETE")

    def test_deletion_after_the_wait_began_is_an_event(self):
        sleep = self.scripted_sleep(
            {1: lambda: delete_call(self.root, self.exchange_id)}
        )

        result = wait_for_event(self.root, self.exchange_id, sleep=sleep)

        self.assertEqual(result["event"], "DELETED")
        self.assertIsNone(result["state"])

    def test_an_absent_exchange_at_first_sight_is_an_error_not_an_event(self):
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            wait_for_event(self.root, "2026-01-01_000000_never_was")

    def test_a_correction_round_wakes_the_waiter_as_repair_not_as_an_ending(self):
        start_call(self.root, self.exchange_id, 5, [])
        finish_call(self.root, self.exchange_id)
        sleep = self.scripted_sleep(
            {1: lambda: open_repair_round(self.root, self.exchange_id, 5, [])}
        )

        result = wait_for_event(
            self.root, self.exchange_id, after_current=True, sleep=sleep
        )

        self.assertEqual(result["event"], "REPAIR_OPENED")
        self.assertEqual(result["state"], "ACTIVE")
        self.assertEqual(result["repair_round"], 1)

    def test_a_filing_failure_wakes_the_waiter_while_the_call_still_runs(self):
        start_call(self.root, self.exchange_id, 5, [])
        sleep = self.scripted_sleep(
            {
                1: lambda: record_download_failure(
                    self.root,
                    {
                        "download_id": 9,
                        "filename": "watch_outputs.zip",
                        "message": "Native companion failed",
                    },
                )
            }
        )

        result = wait_for_event(self.root, self.exchange_id, sleep=sleep)

        self.assertEqual(result["event"], "DOWNLOAD_FILING_FAILED")
        self.assertEqual(result["state"], "ACTIVE")
        # The record it should go read, not the response it must not trust.
        self.assertIn(self.exchange_id, result["attention_record"])

    def test_timeout_reports_still_waiting_rather_than_inventing_an_ending(self):
        start_call(self.root, self.exchange_id, 5, [])

        result = wait_for_event(
            self.root, self.exchange_id, timeout_seconds=0
        )

        self.assertEqual(result["event"], "STILL_WAITING")
        self.assertEqual(result["state"], "ACTIVE")

    def test_persistently_unreadable_state_is_inconsistency_not_an_ending(self):
        manifest_path = (
            self.root / "calls" / self.exchange_id / "EXCHANGE_MANIFEST.json"
        )
        sleep = self.scripted_sleep(
            {1: lambda: manifest_path.write_text("{broken", encoding="utf-8")}
        )

        with self.assertRaises(WaitInconsistency):
            wait_for_event(self.root, self.exchange_id, sleep=sleep)

    def test_snapshots_are_lockless_so_the_waiter_cannot_starve_the_browser(self):
        start_call(self.root, self.exchange_id, 5, [])
        with state_lock(self.root):
            snap = _snapshot(self.root, self.exchange_id)

        self.assertEqual(snap["state"], "ACTIVE")
        self.assertTrue(snap["active"])

    def test_poll_interval_has_a_floor(self):
        with self.assertRaisesRegex(ValueError, "poll_ms"):
            wait_for_event(self.root, self.exchange_id, poll_ms=0)


class WaitCliTests(WatchFixture):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = run(
            ["--root", str(self.root), *args], stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def test_exit_zero_carries_the_event(self):
        start_call(self.root, self.exchange_id, 5, [])
        finish_call(self.root, self.exchange_id)

        code, out, _err = self.cli("wait", "--exchange", self.exchange_id)

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["event"], "INCOMPLETE")

    def test_exit_one_is_a_timeout_with_the_truth_on_stdout(self):
        start_call(self.root, self.exchange_id, 5, [])

        code, out, _err = self.cli(
            "wait", "--exchange", self.exchange_id, "--timeout-seconds", "0"
        )

        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["event"], "STILL_WAITING")

    def test_exit_two_for_an_exchange_that_never_existed(self):
        code, out, err = self.cli("wait", "--exchange", "no_such_exchange")

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(json.loads(err)["ok"])

    def test_exit_three_for_inconsistency_is_distinct_from_both(self):
        with mock.patch(
            "companion.cli.wait_for_event",
            side_effect=WaitInconsistency("unreadable"),
        ):
            code, out, err = self.cli("wait", "--exchange", self.exchange_id)

        self.assertEqual(code, 3)
        self.assertEqual(out, "")
        self.assertIn("unreadable", json.loads(err)["error"])


class FailureRecordTests(WatchFixture):
    def test_a_failure_matching_an_active_call_lands_on_that_call(self):
        start_call(self.root, self.exchange_id, 5, [])

        result = record_download_failure(
            self.root,
            {
                "download_id": 12,
                "filename": r"C:\Users\someone\Downloads\watch_outputs (1).zip",
                "message": "  Native companion failed  ",
            },
        )

        self.assertEqual(result["attributed_to"], self.exchange_id)
        active = load_active_call(self.root, self.exchange_id)
        failures = active["download_failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["download_id"], 12)
        # Path stripped to a basename; message trimmed; time stamped here.
        self.assertEqual(failures[0]["filename"], "watch_outputs (1).zip")
        self.assertEqual(failures[0]["message"], "Native companion failed")
        self.assertIn("T", failures[0]["at"])

    def test_an_unattributable_failure_goes_to_the_global_record(self):
        start_call(self.root, self.exchange_id, 5, [])

        result = record_download_failure(
            self.root,
            {"download_id": 3, "filename": "unrelated.pdf", "message": "boom"},
        )

        self.assertIsNone(result["attributed_to"])
        stored = json.loads(
            (self.root / "state" / "DOWNLOAD_FAILURES.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(len(stored["failures"]), 1)
        active = load_active_call(self.root, self.exchange_id)
        self.assertNotIn("download_failures", active)

    def test_records_are_capped_newest_kept(self):
        start_call(self.root, self.exchange_id, 5, [])
        for index in range(12):
            record_download_failure(
                self.root,
                {
                    "download_id": index,
                    "filename": "watch_outputs.zip",
                    "message": f"failure {index}",
                },
            )

        active = load_active_call(self.root, self.exchange_id)
        failures = active["download_failures"]
        self.assertEqual(len(failures), 10)
        self.assertEqual(failures[-1]["message"], "failure 11")
        self.assertEqual(failures[0]["message"], "failure 2")

    def test_the_message_is_required_and_ids_are_validated(self):
        with self.assertRaisesRegex(ValueError, "message"):
            record_download_failure(self.root, {"filename": "x.zip"})
        with self.assertRaisesRegex(ValueError, "download_id"):
            record_download_failure(
                self.root, {"message": "boom", "download_id": True}
            )


class InspectTests(WatchFixture):
    def test_an_incomplete_exchange_reports_its_defects_and_report(self):
        start_call(self.root, self.exchange_id, 5, [])
        finish_call(self.root, self.exchange_id)

        result = inspect_call(self.root, self.exchange_id)

        self.assertEqual(result["state"], "INCOMPLETE")
        self.assertEqual(result["validation"]["status"], "INCOMPLETE")
        kinds = {defect["kind"] for defect in result["defects"]}
        self.assertIn("MAIN_MISSING", kinds)
        self.assertIsNone(result["paths"]["main_response"])
        self.assertIsNotNone(result["paths"]["validation_report"])

    def test_defects_are_capped_so_the_native_frame_cannot_overflow(self):
        """One megabyte is a hard ceiling, and overflowing it fails the whole
        panel command rather than truncating. `defects` still reports every one.
        """
        start_call(self.root, self.exchange_id, 5, [])
        finish_call(self.root, self.exchange_id)
        many = [
            {"kind": "X", "target": f"f{index}", "expected": "a", "observed": "b"}
            for index in range(90)
        ]

        with mock.patch("companion.repair.collect_defects", return_value=many):
            result = inspect_call(self.root, self.exchange_id)

        self.assertEqual(len(result["defects"]), 25)
        self.assertEqual(result["defects_omitted"], 65)

    def test_a_prepared_exchange_is_not_diagnosed(self):
        result = inspect_call(self.root, self.exchange_id)

        self.assertEqual(result["state"], "PREPARED")
        self.assertEqual(result["defects"], [])
        self.assertIsNone(result["validation"])
        self.assertEqual(result["response_files"], [])

    def test_inspect_refuses_unsafe_and_unknown_ids(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            inspect_call(self.root, "../escape")
        with self.assertRaises(FileNotFoundError):
            inspect_call(self.root, "2026-01-01_000000_gone")


if __name__ == "__main__":
    unittest.main()
