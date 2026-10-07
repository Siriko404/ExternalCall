import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from companion.core import prepare_call
from companion.native_host import (
    ALLOWED_COMMANDS,
    dispatch,
    read_message,
    write_message,
)


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        sources = base / "sources"
        sources.mkdir()
        request = sources / "WEB_REVIEW_REQUEST.json"
        request.write_text('{"request_id":"request_native"}\n', encoding="utf-8")
        schema = sources / "WEB_RESPONSE_SCHEMA.json"
        schema.write_text('{"type":"object"}\n', encoding="utf-8")
        self.manifest = prepare_call(
            self.root,
            {
                "subject": "Native fixture",
                "request_id": "request_native",
                "expected_main_json": "native_result.json",
                "expected_artifacts": ["native_outputs.zip"],
                "prompt_text": "Return files only.\n",
                "input_files": [
                    {"path": str(request), "filename": request.name},
                    {"path": str(schema), "filename": schema.name},
                ],
            },
            datetime(2026, 7, 14, 19, 30, tzinfo=timezone.utc),
        )

    def tearDown(self):
        self.temp.cleanup()

    def message(self, command, payload=None):
        return {
            "protocol_version": 1,
            "command": command,
            "payload": payload or {},
        }

    def test_message_framing_round_trip(self):
        stream = io.BytesIO()
        value = {"protocol_version": 1, "command": "health", "payload": {}}

        write_message(stream, value)
        stream.seek(0)

        self.assertEqual(read_message(stream), value)

    def test_read_message_returns_none_at_clean_eof(self):
        self.assertIsNone(read_message(io.BytesIO()))

    def test_read_message_rejects_truncated_header_and_body(self):
        with self.assertRaisesRegex(ValueError, "header"):
            read_message(io.BytesIO(b"\x05\x00"))
        with self.assertRaisesRegex(ValueError, "body"):
            read_message(io.BytesIO((5).to_bytes(4, "little") + b"{}"))

    def test_read_message_rejects_oversized_frame(self):
        oversized = (1024 * 1024 + 1).to_bytes(4, "little")
        with self.assertRaisesRegex(ValueError, "too large"):
            read_message(io.BytesIO(oversized))

    def test_command_allowlist_is_exact(self):
        self.assertEqual(
            ALLOWED_COMMANDS,
            {
                "health",
                "status.snapshot",
                "calls.list_ready",
                "call.active",
                "calls.active",
                "calls.progress",
                "calls.recent",
                "call.inspect",
                "call.clone",
                "call.go",
                "call.resume",
                "download.completed",
                "download.failure.record",
                "call.done",
                "calls.collect",
                "call.repair",
                "call.stop",
            },
        )

    def test_inspect_returns_metadata_and_never_content(self):
        result = dispatch(
            self.root,
            self.message(
                "call.inspect", {"exchange_id": self.manifest["exchange_id"]}
            ),
        )

        self.assertEqual(result["state"], "PREPARED")
        self.assertEqual(result["request_id"], "request_native")
        self.assertEqual(result["expected_artifacts"], ["native_outputs.zip"])
        self.assertEqual(result["repair_round"], 0)
        self.assertIsNone(result["validation"])
        self.assertEqual(result["defects"], [])
        # The prompt travels in the request; recall must not carry it back.
        self.assertNotIn("Return files only", json.dumps(result))

    def test_failure_record_rejects_unknown_fields_and_missing_message(self):
        with self.assertRaisesRegex(ValueError, "payload fields"):
            dispatch(
                self.root,
                self.message("download.failure.record", {"path": "C:/evil"}),
            )
        with self.assertRaisesRegex(ValueError, "payload fields"):
            dispatch(self.root, self.message("download.failure.record", {}))

    def test_dispatch_rejects_unknown_version_command_and_payload(self):
        with self.assertRaisesRegex(ValueError, "protocol_version"):
            dispatch(
                self.root,
                {"protocol_version": 2, "command": "health", "payload": {}},
            )
        with self.assertRaisesRegex(ValueError, "unknown command"):
            dispatch(self.root, self.message("shell.execute"))
        with self.assertRaisesRegex(ValueError, "payload"):
            dispatch(
                self.root,
                {"protocol_version": 1, "command": "health", "payload": []},
            )

    def test_call_go_returns_only_manifest_approved_request_paths(self):
        result = dispatch(
            self.root,
            self.message(
                "call.go",
                {
                    "exchange_id": self.manifest["exchange_id"],
                    "tab_id": 42,
                    "download_baseline": [1, 3],
                },
            ),
        )

        self.assertEqual(result["active"]["tab_id"], 42)
        # One archive is uploaded, however many files went into it.
        self.assertEqual(
            [Path(path).name for path in result["request_paths"]],
            ["native_fixture_inputs.zip"],
        )
        self.assertTrue(
            all(Path(path).parent.name == "request" for path in result["request_paths"])
        )
        # Sent bare, an archive gets a model asking what to do with it. Go
        # carries the line the panel types so that cannot happen.
        self.assertIn("native_fixture_inputs.zip", result["launch_prompt"])
        self.assertIn("000_READ_ME_FIRST.md", result["launch_prompt"])

    def test_done_is_idempotent_after_active_state_is_cleared(self):
        dispatch(
            self.root,
            self.message(
                "call.go",
                {
                    "exchange_id": self.manifest["exchange_id"],
                    "tab_id": 42,
                    "download_baseline": [],
                },
            ),
        )

        first = dispatch(self.root, self.message("call.done", {"exchange_id": self.manifest["exchange_id"]}))
        second = dispatch(self.root, self.message("call.done", {"exchange_id": self.manifest["exchange_id"]}))

        self.assertEqual(first, second)
        self.assertEqual(first["status"], "INCOMPLETE")

    def test_call_resume_returns_the_same_approved_request_paths(self):
        original = dispatch(
            self.root,
            self.message(
                "call.go",
                {
                    "exchange_id": self.manifest["exchange_id"],
                    "tab_id": 42,
                    "download_baseline": [1],
                },
            ),
        )

        resumed = dispatch(
            self.root,
            self.message(
                "call.resume",
                {"tab_id": 77, "download_baseline": [1, 2]},
            ),
        )

        self.assertEqual(resumed["active"]["tab_id"], 77)
        self.assertEqual(resumed["active"]["exchange_id"], self.manifest["exchange_id"])
        self.assertEqual(resumed["request_paths"], original["request_paths"])


if __name__ == "__main__":
    unittest.main()
