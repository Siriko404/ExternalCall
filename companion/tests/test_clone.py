"""Sending the same request again.

A finished call is a dead end: start_call takes PREPARED alone, stop_call and
finish_call refuse a STOPPED exchange, and there is no delete command in the
panel at all. A stopped call - the panel's Done clicked on the wrong row, a
browser closed mid-flight - therefore had nowhere to go but the CLI. Cloning is
the way back, and it must be a faithful copy or it is a different call wearing
the same subject.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import (
    clone_call,
    list_recent_calls,
    prepare_call,
    start_call,
    stop_call,
)


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        self.sources = base / "sources"
        self.sources.mkdir()
        request = self.write_source(
            "WEB_REVIEW_REQUEST.json", {"request_id": "request_fixture"}
        )
        schema = self.write_source("WEB_RESPONSE_SCHEMA.json", {"type": "object"})
        notes = self.sources / "NOTES.md"
        notes.write_text("context the call needs\n", encoding="utf-8")
        self.spec = {
            "subject": "Fixture call",
            "request_id": "request_fixture",
            "expected_main_json": "result.json",
            "expected_artifacts": ["fixture_outputs.zip"],
            "prompt_text": "Return files only.\n",
            "input_files": [
                {"path": str(request), "filename": request.name},
                {"path": str(schema), "filename": schema.name},
                {"path": str(notes), "filename": notes.name},
            ],
        }
        self.first_time = datetime(
            2026, 7, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4))
        )
        self.later = self.first_time + timedelta(hours=3)
        self.manifest = prepare_call(self.root, self.spec, self.first_time)
        self.exchange_id = self.manifest["exchange_id"]

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, name, value):
        path = self.sources / name
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def stop(self):
        """The Matt case: a call clicked Done on the wrong row and stopped."""
        start_call(self.root, self.exchange_id, 11, [])
        stop_call(self.root, self.exchange_id)

    def digests(self, manifest):
        return {
            record["filename"]: record["sha256"]
            for record in manifest["request_files"]
        }

    def test_the_clone_sends_byte_identical_files(self):
        """The archive is what goes up, so the archive is what must match.

        _write_bundle pins the member timestamps and the member order, which is
        what makes this comparable at all: the same inputs packed twice produce
        the same bytes. A digest that differs means the clone packed something
        the original did not.
        """
        self.stop()

        clone = clone_call(self.root, self.exchange_id, self.later)

        self.assertEqual(self.digests(clone), self.digests(self.manifest))
        self.assertEqual(clone["attach_files"], ["fixture_call_inputs.zip"])

    def test_the_clone_carries_the_whole_request(self):
        self.stop()

        clone = clone_call(self.root, self.exchange_id, self.later)

        self.assertEqual(clone["state"], "PREPARED")
        self.assertEqual(clone["request_id"], self.manifest["request_id"])
        self.assertEqual(clone["subject"], self.manifest["subject"])
        self.assertEqual(
            clone["expected_main_json"], self.manifest["expected_main_json"]
        )
        self.assertEqual(
            clone["expected_artifacts"], self.manifest["expected_artifacts"]
        )
        # The prompt travels inside the archive, so it is not an input to the
        # new preparation - but it has to survive into the new bundle.
        prompt = (
            self.root / "calls" / clone["exchange_id"] / "request"
            / "000_READ_ME_FIRST.md"
        )
        self.assertEqual(prompt.read_text(encoding="utf-8"), "Return files only.\n")

    def test_the_original_is_left_alone(self):
        """Its response is the only copy of work the model already did."""
        self.stop()

        clone = clone_call(self.root, self.exchange_id, self.later)

        self.assertNotEqual(clone["exchange_id"], self.exchange_id)
        source = json.loads(
            (self.root / "calls" / self.exchange_id / "EXCHANGE_MANIFEST.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(source["state"], "STOPPED")

    def test_the_clone_says_what_it_came_from(self):
        """Two exchanges with one subject differ by timestamp alone otherwise,
        which cannot tell a resend from a coincidence."""
        self.stop()

        clone = clone_call(self.root, self.exchange_id, self.later)

        self.assertEqual(clone["cloned_from"], self.exchange_id)
        self.assertIsNone(self.manifest["cloned_from"])
        rows = {row["exchange_id"]: row for row in list_recent_calls(self.root)}
        self.assertEqual(rows[clone["exchange_id"]]["cloned_from"], self.exchange_id)

    def test_a_call_that_has_not_finished_is_refused(self):
        """PREPARED and ACTIVE still hold their deliverable names, so the clone
        would fail the name check anyway - with a message about filenames rather
        than about the thing being asked for."""
        with self.assertRaises(RuntimeError) as prepared:
            clone_call(self.root, self.exchange_id, self.later)
        self.assertIn("has not finished", str(prepared.exception))

        start_call(self.root, self.exchange_id, 11, [])
        with self.assertRaises(RuntimeError) as active:
            clone_call(self.root, self.exchange_id, self.later)
        self.assertIn("ACTIVE", str(active.exception))

    def test_a_repair_round_does_not_travel_into_the_clone(self):
        """The clone is built from the manifest's record of the request, not
        from a listing of the directory, so anything written into the exchange
        after preparation stays out of the resend."""
        self.stop()
        exchange = self.root / "calls" / self.exchange_id
        (exchange / "repair").mkdir()
        (exchange / "repair" / "ROUND_1_PROMPT.txt").write_text("fix it", "utf-8")
        (exchange / "request" / "STRAY.md").write_text("not sent", "utf-8")

        clone = clone_call(self.root, self.exchange_id, self.later)

        names = {record["filename"] for record in clone["request_files"]}
        self.assertNotIn("STRAY.md", names)
        self.assertNotIn("ROUND_1_PROMPT.txt", names)
        self.assertEqual(self.digests(clone), self.digests(self.manifest))

    def test_the_clone_can_be_sent(self):
        """The point of the whole thing: a finished call becomes sendable again.

        The deliverable names are free because a finished exchange releases
        them (claimed_deliverable_names counts PREPARED and ACTIVE alone).
        """
        self.stop()

        clone = clone_call(self.root, self.exchange_id, self.later)
        active = start_call(self.root, clone["exchange_id"], 12, [])

        self.assertEqual(active["exchange_id"], clone["exchange_id"])
        self.assertEqual(active["expected_main_json"], "result.json")


if __name__ == "__main__":
    unittest.main()
