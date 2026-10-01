"""Prepare one fixture exchange in a temporary root and print it as JSON.

The Python counterpart of fixture_call.ps1: same spec, same expected packaged
names, same single JSON object on stdout. Used by run_all.py and by anything
that wants a real prepared exchange to operate on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_FILES = (
    "000_READ_ME_FIRST.md",
    "WEB_REVIEW_REQUEST.json",
    "WEB_RESPONSE_SCHEMA.json",
    "context.txt",
    "fixture_call_inputs.zip",
)


def prepare(root: Path) -> dict:
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    request_path = sources / "WEB_REVIEW_REQUEST.json"
    schema_path = sources / "WEB_RESPONSE_SCHEMA.json"
    context_path = sources / "context.txt"
    spec_path = root / "spec.json"

    request_path.write_text(json.dumps({"request_id": "fixture_request"}) + "\n", encoding="utf-8")
    schema_path.write_text(json.dumps({"type": "object"}) + "\n", encoding="utf-8")
    context_path.write_text("fixture context\n", encoding="utf-8")

    spec = {
        "subject": "Fixture call",
        "request_id": "fixture_request",
        "expected_main_json": "result.json",
        "expected_artifacts": ["fixture_outputs.zip"],
        "prompt_text": "Return the required files only.",
        "created_at": "2026-07-14T20:15:00-04:00",
        "input_files": [
            {"path": str(request_path), "filename": "WEB_REVIEW_REQUEST.json"},
            {"path": str(schema_path), "filename": "WEB_RESPONSE_SCHEMA.json"},
            {"path": str(context_path), "filename": "context.txt"},
        ],
    }
    spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

    prepared = json.loads(
        subprocess.run(
            [
                sys.executable, "-m", "companion.cli",
                "--root", str(root), "prepare", "--spec", str(spec_path),
            ],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if not prepared.get("ok"):
        raise SystemExit("Fixture call preparation failed.")

    exchange = root / "calls" / prepared["result"]["exchange_id"]
    manifest = json.loads(
        (exchange / "EXCHANGE_MANIFEST.json").read_text(encoding="utf-8-sig")
    )
    names = [entry["filename"] for entry in manifest["request_files"]]
    if sorted(names) != sorted(EXPECTED_FILES):
        raise SystemExit(f"Unexpected fixture files: {', '.join(names)}")
    if not (exchange / "response").is_dir():
        raise SystemExit("Response directory is missing.")

    return {
        "root": str(root),
        "exchange_id": manifest["exchange_id"],
        "request_files": names,
    }


def main(argv: list[str]) -> int:
    root = Path(argv[0]) if argv else Path(
        tempfile.mkdtemp(prefix="externalcall-fixture-")
    )
    print(json.dumps(prepare(root), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
