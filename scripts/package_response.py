"""Trusted, dependency-free responder helper. Run from the extracted request."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import tempfile
import zipfile
from pathlib import Path


def package(request_path: Path, payload: dict, artifacts_dir: Path, output_dir: Path, status: str) -> Path:
    if status not in {"COMPLETE", "PARTIAL", "BLOCKED"}:
        raise ValueError("invalid completion status")
    request = json.loads(request_path.read_text(encoding="utf-8-sig"))
    limits = request["limits"]
    base = artifacts_dir.resolve()
    manifest, total = [], 0
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / request["expected_archive"]
    if target.exists():
        raise FileExistsError(f"output already exists: {target.name}")
    fd, temporary = tempfile.mkstemp(prefix=".response-", dir=output_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for artifact in request["job"]["deliverables"]:
                name = artifact["filename"]
                path = base / name
                if not path.resolve().is_relative_to(base) or path.is_symlink():
                    raise ValueError("artifact escapes its directory")
                media = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if not path.is_file():
                    if status == "COMPLETE":
                        raise ValueError(f"missing artifact {name}; use PARTIAL or BLOCKED honestly")
                    manifest.append({"filename": name, "status": "NOT_CREATED", "media_type": media,
                                     "size": 0, "sha256": None})
                    continue
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                size, digest = 0, hashlib.sha256()
                with path.open("rb") as incoming, archive.open(info, "w") as outgoing:
                    while chunk := incoming.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > limits["member_bytes"] or total > limits["expanded_bytes"]:
                            raise ValueError("artifact bytes exceed the job's archive budget")
                        digest.update(chunk)
                        outgoing.write(chunk)
                manifest.append({"filename": name, "status": "CREATED", "media_type": media,
                                 "size": size, "sha256": digest.hexdigest()})
            envelope = {
                "request_id": request["request_id"], "job_revision": request["job_revision"],
                "status": status, "results": payload["results"],
                "criteria_report": payload["criteria_report"], "artifacts_manifest": manifest,
                "limitations": payload.get("limitations", []), "delivery": [request["expected_archive"]],
            }
            data = (json.dumps(envelope, indent=2) + "\n").encode()
            if len(data) > limits["main_bytes"] or total + len(data) > limits["expanded_bytes"]:
                raise ValueError("envelope exceeds the job's byte budget")
            info = zipfile.ZipInfo(request["expected_main_json"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        if Path(temporary).stat().st_size > limits["archive_bytes"]:
            raise ValueError("ZIP exceeds the compressed-byte budget")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, default=Path("WEB_REVIEW_REQUEST.json"))
    parser.add_argument("--payload", type=Path, required=True,
                        help="JSON with results, criteria_report, and optional limitations")
    parser.add_argument("--artifacts", type=Path, default=Path("outputs"))
    parser.add_argument("--output", type=Path, default=Path("."))
    parser.add_argument("--status", choices=["COMPLETE", "PARTIAL", "BLOCKED"], required=True)
    args = parser.parse_args()
    print(package(args.request, json.loads(args.payload.read_text(encoding="utf-8-sig")),
                  args.artifacts, args.output, args.status))


if __name__ == "__main__":
    main()
