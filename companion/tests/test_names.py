import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import (
    claimed_deliverable_names,
    prepare_call,
    start_call,
    stop_call,
)
from companion.downloads import finish_call, validate_response
from companion.repair import collect_defects


class DeliverableNameTests(unittest.TestCase):
    """Filenames are how downloads are routed, so they are allocated, not chosen."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        self.sources = base / "sources"
        self.sources.mkdir()
        self.now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        self.request = self.sources / "WEB_REVIEW_REQUEST.json"
        self.request.write_text('{"request_id":"fixture"}\n', encoding="utf-8")
        self.schema = self.sources / "WEB_RESPONSE_SCHEMA.json"
        self.schema.write_text('{"type":"object"}\n', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def spec(self, subject, main, artifacts=None):
        value = {
            "subject": subject,
            "request_id": "fixture",
            "expected_main_json": main,
            "expected_artifacts": [f"{Path(main).stem}_outputs.zip"],
            "prompt_text": "Return files only.\n",
            "input_files": [
                {"path": str(self.request), "filename": "WEB_REVIEW_REQUEST.json"},
                {"path": str(self.schema), "filename": "WEB_RESPONSE_SCHEMA.json"},
            ],
        }
        if artifacts is not None:
            value["expected_artifacts"] = artifacts
        return value

    def test_preparing_a_colliding_main_json_name_is_refused(self):
        prepare_call(self.root, self.spec("First", "result.json"), self.now)

        with self.assertRaisesRegex(ValueError, "already claimed"):
            prepare_call(
                self.root,
                self.spec("Second", "result.json"),
                self.now + timedelta(minutes=1),
            )

    def test_preparing_a_colliding_artifact_name_is_refused(self):
        prepare_call(
            self.root,
            self.spec("First", "first.json", ["findings_bundle.zip"]),
            self.now,
        )

        with self.assertRaisesRegex(ValueError, "findings_bundle.zip is already claimed"):
            prepare_call(
                self.root,
                self.spec("Second", "second.json", ["findings_bundle.zip"]),
                self.now + timedelta(minutes=1),
            )

    def test_an_artifact_can_no_longer_collide_with_a_main_json(self):
        """The one-archive rule makes this whole class of collision unreachable.

        A main response must end in .json and an artifact must be the single
        .zip, so the two namespaces cannot meet. The rule is refused before the
        claim check ever runs, which is why the message is about the archive and
        not about the name.
        """
        prepare_call(self.root, self.spec("First", "shared.json"), self.now)

        with self.assertRaisesRegex(ValueError, "exactly one .zip"):
            prepare_call(
                self.root,
                self.spec("Second", "second.json", ["shared.json"]),
                self.now + timedelta(minutes=1),
            )

    def test_collision_is_case_insensitive(self):
        prepare_call(self.root, self.spec("First", "Result.json"), self.now)

        with self.assertRaisesRegex(ValueError, "already claimed"):
            prepare_call(
                self.root,
                self.spec("Second", "result.JSON"),
                self.now + timedelta(minutes=1),
            )

    def test_distinct_names_prepare_and_run_together(self):
        first = prepare_call(
            self.root,
            self.spec("Numbers", "numbers_response.json", ["numbers_outputs.zip"]),
            self.now,
        )
        second = prepare_call(
            self.root,
            self.spec("Claims", "claims_response.json", ["claims_outputs.zip"]),
            self.now + timedelta(minutes=1),
        )

        start_call(self.root, first["exchange_id"], 11, [])
        start_call(self.root, second["exchange_id"], 12, [])

        claimed = claimed_deliverable_names(self.root)
        self.assertEqual(
            sorted(claimed),
            [
                "claims_outputs.zip",
                "claims_response.json",
                "numbers_outputs.zip",
                "numbers_response.json",
            ],
        )

    def test_a_finished_call_releases_its_names(self):
        first = prepare_call(self.root, self.spec("First", "result.json"), self.now)
        start_call(self.root, first["exchange_id"], 11, [])
        stop_call(self.root, first["exchange_id"])

        reused = prepare_call(
            self.root, self.spec("Second", "result.json"), self.now + timedelta(minutes=1)
        )

        self.assertEqual(reused["expected_main_json"], "result.json")

    def test_a_promised_artifact_that_is_never_mentioned_fails_validation(self):
        call = prepare_call(
            self.root,
            self.spec("Numbers", "numbers_response.json", ["numbers_outputs.zip"]),
            self.now,
        )
        start_call(self.root, call["exchange_id"], 11, [])
        exchange = self.root / "calls" / call["exchange_id"]
        (exchange / "response" / "numbers_response.json").write_text(
            json.dumps(
                {
                    "request_id": "fixture",
                    "status": "COMPLETE",
                    "artifacts_manifest": [],
                    "delivery": ["numbers_response.json"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        report = finish_call(self.root, call["exchange_id"])

        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertEqual(report["missing_files"], ["numbers_outputs.zip"])
        self.assertEqual(validate_response(exchange)["status"], "INCOMPLETE")
        self.assertEqual(
            [defect["kind"] for defect in collect_defects(exchange)],
            ["EXPECTED_ARTIFACT_ABSENT"],
        )

    def test_a_spec_written_with_a_byte_order_mark_is_accepted(self):
        spec_path = self.sources / "spec.json"
        spec_path.write_text(
            json.dumps(self.spec("Bom", "bom_response.json")),
            encoding="utf-8-sig",
        )
        from companion.core import _read_json_object

        manifest = prepare_call(
            self.root, _read_json_object(spec_path, "spec"), self.now
        )

        self.assertEqual(manifest["expected_main_json"], "bom_response.json")

    def test_expected_artifacts_are_validated_as_file_names(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            prepare_call(
                self.root,
                self.spec("Bad", "bad.json", ["../escape.zip"]),
                self.now,
            )


if __name__ == "__main__":
    unittest.main()
