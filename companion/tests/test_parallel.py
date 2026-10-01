import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from companion.core import load_active_calls, prepare_call, start_call
from companion.downloads import finish_call, handle_completed_download


class ParallelDownloadRoutingTests(unittest.TestCase):
    """Two calls run at once and their downloads arrive interleaved."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / "ExternalCall"
        self.sources = base / "sources"
        self.downloads = base / "Downloads"
        self.sources.mkdir()
        self.downloads.mkdir()
        self.now = datetime(2026, 7, 21, 9, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
        self.calls = {}
        for index, name in enumerate(("numbers", "claims")):
            request = self.sources / f"WEB_REVIEW_REQUEST_{name}.json"
            request.write_text(json.dumps({"request_id": f"pass_{name}"}), encoding="utf-8")
            schema = self.sources / f"WEB_RESPONSE_SCHEMA_{name}.json"
            schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
            manifest = prepare_call(
                self.root,
                {
                    "subject": f"{name} pass",
                    "request_id": f"pass_{name}",
                    "expected_main_json": f"{name}_response.json",
                    "expected_artifacts": [f"{name}_outputs.zip"],
                    "prompt_text": "Return files only.\n",
                    "input_files": [
                        {"path": str(request), "filename": "WEB_REVIEW_REQUEST.json"},
                        {"path": str(schema), "filename": "WEB_RESPONSE_SCHEMA.json"},
                    ],
                },
                self.now + timedelta(minutes=index),
            )
            start_call(self.root, manifest["exchange_id"], 100 + index, [])
            self.calls[name] = manifest

    def tearDown(self):
        self.temp.cleanup()

    def exchange(self, name):
        return self.root / "calls" / self.calls[name]["exchange_id"] / "response"

    def artifact_bytes(self, name):
        return f"# {name} findings\n".encode("utf-8")

    def archive(self, name, download_id, *, with_artifact=True):
        """Deliver one call's whole response as the single archive it returns."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(
                f"{name}_response.json",
                self.main_bytes(name, with_artifact=with_artifact),
            )
            if with_artifact:
                bundle.writestr(f"{name}_findings.md", self.artifact_bytes(name))
        path = self.download(f"{name}_outputs.zip", buffer.getvalue())
        return handle_completed_download(self.root, self.completed(download_id, path))

    def main_bytes(self, name, *, with_artifact=True):
        artifacts = []
        delivery = [f"{name}_outputs.zip"]
        if with_artifact:
            payload = self.artifact_bytes(name)
            artifacts.append(
                {
                    "filename": f"{name}_findings.md",
                    "status": "CREATED",
                    "media_type": "text/markdown",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
            delivery.append(f"{name}_findings.md")
        return (
            json.dumps(
                {
                    "request_id": f"pass_{name}",
                    "status": "COMPLETE",
                    "artifacts_manifest": artifacts,
                    "delivery": delivery,
                }
            )
            + "\n"
        ).encode("utf-8")

    def download(self, filename, contents):
        path = self.downloads / filename
        path.write_bytes(contents)
        return path

    def completed(self, download_id, path):
        return {"id": download_id, "filename": str(path), "state": "complete"}

    def test_two_calls_are_active_at_once(self):
        self.assertEqual(len(load_active_calls(self.root)), 2)

    def test_each_archive_is_routed_to_its_own_exchange(self):
        claims = self.archive("claims", 1, with_artifact=False)
        numbers = self.archive("numbers", 2, with_artifact=False)

        self.assertEqual(claims["exchange_id"], self.calls["claims"]["exchange_id"])
        self.assertEqual(numbers["exchange_id"], self.calls["numbers"]["exchange_id"])
        self.assertTrue((self.exchange("claims") / "claims_response.json").is_file())
        self.assertTrue((self.exchange("numbers") / "numbers_response.json").is_file())

    def test_interleaved_archives_reach_the_call_that_named_them(self):
        numbers = self.archive("numbers", 1)
        claims = self.archive("claims", 2)

        self.assertEqual(claims["status"], "MOVED")
        self.assertEqual(numbers["status"], "MOVED")
        self.assertEqual(claims["exchange_id"], self.calls["claims"]["exchange_id"])
        self.assertEqual(numbers["exchange_id"], self.calls["numbers"]["exchange_id"])
        self.assertTrue((self.exchange("claims") / "claims_response.json").is_file())
        self.assertTrue((self.exchange("numbers") / "numbers_response.json").is_file())

    def test_an_unexpected_download_is_ignored_rather_than_pooled(self):
        """A shared pending pool used to hold anything it could not attribute
        yet, and an entry parked there was invisible until validation later
        reported the file missing. One archive per call means there is nothing
        left to wait for, so an unattributable download is said so at once."""
        early = self.download("claims_findings.md", self.artifact_bytes("claims"))

        result = handle_completed_download(self.root, self.completed(1, early))

        self.assertEqual(result["status"], "IGNORED")
        self.assertIn("claims_findings.md", result["reason"])
        self.assertTrue(early.exists())

    def test_finishing_one_call_leaves_the_other_collecting(self):
        self.archive("numbers", 1, with_artifact=False)

        report = finish_call(self.root, self.calls["numbers"]["exchange_id"])

        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(
            [item["exchange_id"] for item in load_active_calls(self.root)],
            [self.calls["claims"]["exchange_id"]],
        )
        still_working = self.archive("claims", 2, with_artifact=False)
        self.assertEqual(still_working["status"], "MOVED")

    def test_a_download_named_by_neither_call_is_left_alone(self):
        for name in ("numbers", "claims"):
            main = self.download(f"{name}_response.json", self.main_bytes(name, with_artifact=False))
            handle_completed_download(self.root, self.completed(hash(name) % 50, main))
        unrelated = self.download("holiday_photo.jpg", b"jpeg")

        result = handle_completed_download(self.root, self.completed(90, unrelated))

        self.assertEqual(result["status"], "IGNORED")
        self.assertTrue(unrelated.exists())

    def test_a_baseline_download_for_one_call_can_still_reach_the_other(self):
        # The claims call started later, so an id in its baseline may still be a
        # legitimate download for the numbers call.
        claims_id = self.calls["claims"]["exchange_id"]
        record = next(
            item for item in load_active_calls(self.root) if item["exchange_id"] == claims_id
        )
        record["download_baseline"] = [7]
        (self.root / "state" / "active" / f"{claims_id}.json").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        result = self.archive("numbers", 7, with_artifact=False)

        self.assertEqual(result["status"], "MOVED")
        self.assertEqual(result["exchange_id"], self.calls["numbers"]["exchange_id"])


if __name__ == "__main__":
    unittest.main()
