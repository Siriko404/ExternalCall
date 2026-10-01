import io
import json
import unittest
import zipfile
from unittest.mock import patch

from companion.downloads import validate_response, handle_completed_download
from companion.tests import test_downloads as fixtures
from companion.archives import inspect_zip


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DownloadTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_corrupt_unlisted_member_is_rejected_after_real_ingestion(self):
        f = self.fixture
        output = f.delivery_archive(main=f.main_json_bytes(artifacts=False),
                                    report=b"unlisted-payload-marker")
        data = output.read_bytes().replace(b"unlisted-payload-marker", b"unlisted-payload-markeR", 1)
        self.assertNotEqual(data, output.read_bytes())
        output.write_bytes(data)
        handle_completed_download(f.root, f.completed(2, output))
        report = validate_response(f.exchange)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["archive_valid"])
        self.assertIn("ARCHIVE_INVALID", {x["kind"] for x in report["defects"]})

    def test_repeated_download_already_at_destination_preserves_bytes(self):
        from companion.downloads import _safe_move, _safe_copy
        path = self.fixture.exchange / "response" / "already.txt"
        path.write_bytes(b"evidence")
        _safe_move(path, path)
        self.assertEqual(_safe_copy(path, path), "IDENTICAL")
        self.assertEqual(path.read_bytes(), b"evidence")

    def test_ambiguous_legacy_basename_is_rejected(self):
        f = self.fixture
        (f.exchange / "response" / "result.json").write_bytes(f.main_json_bytes())
        with zipfile.ZipFile(f.exchange / "response" / "fixture_outputs.zip", "w") as archive:
            archive.writestr("a/report.md", f.report_bytes)
            archive.writestr("b/report.md", b"different bytes")
        report = validate_response(f.exchange)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertIn("ARCHIVE_INVALID", {x["kind"] for x in report["defects"]})

    def test_traversal_duplicate_and_symlink_archive_members_fail(self):
        path = self.fixture.downloads / "unsafe.zip"
        for members in [("../escape.txt",), ("a.txt", "A.txt")]:
            with zipfile.ZipFile(path, "w") as archive:
                for name in members:
                    archive.writestr(name, b"x")
            with self.assertRaises(ValueError):
                inspect_zip(path)
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"outside")
        with self.assertRaisesRegex(ValueError, "member type"):
            inspect_zip(path)

    def test_expansion_and_member_count_budgets_are_enforced(self):
        path = self.fixture.delivery_archive()
        with patch("companion.archives.MAX_EXPANDED_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "expanded-byte"):
                inspect_zip(path)
        with patch("companion.archives.MAX_MEMBERS", 1):
            with self.assertRaisesRegex(ValueError, "member-count"):
                inspect_zip(path)

    def test_malformed_response_types_are_reports_not_uncaught_exceptions(self):
        f = self.fixture
        archive = f.delivery_archive(main=f.main_json_bytes(artifacts=False))
        handle_completed_download(f.root, f.completed(2, archive))
        for field, value in [("status", {}), ("artifacts_manifest", None), ("delivery", [None])]:
            body = json.loads(f.main_json_bytes(artifacts=False))
            body[field] = value
            (f.exchange / "response" / "result.json").write_text(json.dumps(body))
            self.assertEqual(validate_response(f.exchange)["status"], "INCOMPLETE")
