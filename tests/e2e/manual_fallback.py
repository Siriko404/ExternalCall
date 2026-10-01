"""Validate one fixture exchange by hand, without the extension.

The Python counterpart of manual_fallback.ps1, updated to the one-zip-in
contract: the mock responder's delivery arrives as the expected outputs
archive, the main JSON is lifted out of it under its exact expected name, and
`validate` must report COMPLETE with the archive-member hash of report.md
verified against the manifest the response declared.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixture_call import prepare  # noqa: E402

ARCHIVE_NAME = "fixture_outputs.zip"


def main(argv: list[str]) -> int:
    if argv:
        root = Path(argv[0])
        with_temp_root = False
    else:
        root = Path(tempfile.mkdtemp(prefix="externalcall-fixture-"))
        with_temp_root = True
    fixture = prepare(root)

    fixtures = REPOSITORY_ROOT / "tests" / "mock_chatgpt" / "fixtures"
    exchange = Path(fixture["root"]) / "calls" / fixture["exchange_id"]
    response = exchange / "response"

    # The one file that comes back, and the main JSON lifted out of it under
    # its exact expected name — the manual equivalent of what the companion
    # does when the download arrives.
    with zipfile.ZipFile(response / ARCHIVE_NAME, "w") as bundle:
        for name in ("result.json", "report.md"):
            bundle.write(fixtures / name, arcname=name)
    shutil.copy(fixtures / "result.json", response / "result.json")

    validated = json.loads(
        subprocess.run(
            [
                sys.executable, "-m", "companion.cli",
                "--root", fixture["root"],
                "validate", "--exchange", fixture["exchange_id"],
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if not validated.get("ok"):
        raise SystemExit("Manual validation command failed.")
    status = validated["result"]["status"]
    if status != "COMPLETE":
        raise SystemExit(f"Manual fallback status was {status}.")

    report = json.loads(
        (exchange / "validation" / "VALIDATION_REPORT.json").read_text(encoding="utf-8-sig")
    )
    if (
        report["status"] != "COMPLETE"
        or report["missing_files"]
        or report["invalid_files"]
    ):
        raise SystemExit("Manual fallback validation report is not complete.")
    if report.get("manifest_verified") is not True:
        raise SystemExit("Manual fallback did not verify the declared manifest.")

    print(
        json.dumps(
            {
                "exchange_id": fixture["exchange_id"],
                "status": report["status"],
                "mode": "manual-fallback",
            },
            separators=(",", ":"),
        )
    )
    if with_temp_root:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
