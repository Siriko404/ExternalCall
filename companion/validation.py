"""Single response validator. Repair and every completion path consume this report."""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

from companion.archives import MAX_MAIN_BYTES, member_index, safe_path
from companion.core import _read_json_object, _safe_name, _sha256
from companion.schema import errors


def archive_member_index(response_dir):
    return member_index(response_dir)


def validate_response(exchange_dir: Path) -> dict[str, Any]:
    exchange = Path(exchange_dir).resolve()
    manifest = _read_json_object(exchange / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST")
    expected = _safe_name(manifest["expected_main_json"], "main response")
    response = exchange / "response"
    main = response / expected
    defects, checked = [], []
    raw = {}
    schema_valid = False
    version = manifest.get("contract_version", 1)
    members = {}
    archive_ok = True
    try:
        members = member_index(response, nested=version < 2, aliases=version < 2)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        archive_ok = False
        target = manifest.get("expected_artifacts", ["outputs archive"])[0]
        defects.append(_defect("ARCHIVE_INVALID", target, "bounded, unambiguous CRC-valid archive", str(error)))
    for name in manifest.get("expected_artifacts", []):
        if not (response / name).is_file():
            defects.append(_defect("EXPECTED_ARTIFACT_ABSENT", name, "expected downloadable archive", "not delivered"))
    if not main.is_file():
        defects.append(_defect("MAIN_MISSING", expected, "main JSON", "not delivered"))
    else:
        try:
            if main.stat().st_size > MAX_MAIN_BYTES:
                raise ValueError("main JSON exceeds byte budget")
            raw = _read_json_object(main, "main response")
            checked.append(expected)
            if raw.get("request_id") != manifest["request_id"]:
                defects.append(_defect("REQUEST_ID_MISMATCH", expected, manifest["request_id"], str(raw.get("request_id"))))
            if raw.get("status") not in {"COMPLETE", "PARTIAL", "BLOCKED"}:
                defects.append(_defect("STATUS_INVALID", expected, "COMPLETE/PARTIAL/BLOCKED", str(raw.get("status"))))
            # Schemas are frozen inputs. Their hashes are verified before use.
            records = {x["filename"]: x for x in manifest.get("request_files", [])}
            schema_path = exchange / "request" / "WEB_RESPONSE_SCHEMA.json"
            schema_record = records.get("WEB_RESPONSE_SCHEMA.json")
            if schema_record and _sha256(schema_path) != (schema_record["sha256"], schema_record["size"]):
                raise ValueError("frozen response schema changed")
            schema = _read_json_object(schema_path, "response schema")
            problems = errors(schema, raw)
            schema_valid = not problems
            for problem in problems:
                defects.append(_defect("SCHEMA_INVALID", expected, "frozen response schema", problem))
            if version >= 2:
                actual = members.get(expected.casefold())
                if actual != _sha256(main):
                    defects.append(_defect("MAIN_ARCHIVE_MISMATCH", expected, "exact envelope bytes inside ZIP", "absent or different envelope"))
                archive = manifest["expected_artifacts"][0]
                if raw.get("delivery") != [archive]:
                    defects.append(_defect("DELIVERY_INVALID", expected, f"only {archive}", "delivery differs from generated contract"))
                allowed = {expected.casefold()} | {safe_path(x).casefold() for x in manifest["expected_deliverables"]}
                for extra in sorted(set(members) - allowed):
                    defects.append(_defect("UNEXPECTED_ARTIFACT", extra, "approved artifact inventory", "undeclared archive member"))
            defects.extend(_artifact_defects(raw, response, expected, members, allow_loose=version < 2))
            defects.extend(_delivery_defects(raw, expected, response, tuple(manifest.get("expected_artifacts", []))))
            for entry in raw.get("artifacts_manifest", []) if isinstance(raw.get("artifacts_manifest"), list) else []:
                if isinstance(entry, dict) and entry.get("status") == "CREATED":
                    name = entry.get("filename")
                    if isinstance(name, str) and not any(x["target"] == name for x in defects):
                        checked.append(name)
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            defects.append(_defect("MAIN_NOT_JSON", expected, "bounded valid JSON and frozen schema", str(error)))
        except Exception as error:
            # A broken reference/validator is an explicit failure, never a pass.
            defects.append(_defect("SCHEMA_INVALID", expected, "supported frozen schema", str(error)))
    missing_kinds = {"MAIN_MISSING", "ARTIFACT_MISSING", "EXPECTED_ARTIFACT_ABSENT"}
    if version < 2:
        absent = {x.get("filename") for x in raw.get("artifacts_manifest", [])
                  if isinstance(x, dict) and isinstance(x.get("filename"), str)
                  and x.get("status") in ("MISSING", "NOT_CREATED")} if isinstance(raw.get("artifacts_manifest"), list) else set()
        defects = [x for x in defects if not (x["kind"] == "EXPECTED_ARTIFACT_ABSENT" and x["target"] in absent)]
    missing = sorted({x["target"] for x in defects if x["kind"] in missing_kinds})
    invalid = sorted({x["target"] for x in defects if x["kind"] not in missing_kinds})
    value = raw.get("status")
    status = value if isinstance(value, str) and value in {"COMPLETE", "PARTIAL", "BLOCKED"} else None
    artifact_kinds = {"ARTIFACT_ENTRY_INVALID", "ARTIFACTS_MANIFEST_INVALID", "ARTIFACT_HASH_MISMATCH", "ARTIFACT_SIZE_MISMATCH", "ARTIFACT_MISSING", "MAIN_JSON_LISTED_AS_ARTIFACT"}
    metadata_ok = not any(x["kind"] in artifact_kinds for x in defects)
    criteria = raw.get("criteria_report")
    criteria = criteria if isinstance(criteria, list) else []
    artifacts = raw.get("artifacts_manifest")
    artifacts = artifacts if isinstance(artifacts, list) else []
    claimed_complete = status == "COMPLETE"
    if version >= 2:
        claimed_complete = claimed_complete and schema_valid and all(x.get("status") == "MET" for x in criteria if isinstance(x, dict))
        claimed_complete = claimed_complete and all(x.get("status") == "CREATED" for x in artifacts if isinstance(x, dict))
    return {
        "schema_version": 2, "exchange_id": manifest["exchange_id"], "request_id": manifest["request_id"],
        "status": "INCOMPLETE" if defects else "COMPLETE", "response_status": status,
        "work_complete": claimed_complete, "manifest_verified": metadata_ok and archive_ok and bool(raw),
        "schema_valid": schema_valid, "archive_valid": archive_ok,
        "semantic_acceptance": "NOT_REVIEWED", "main_json": expected,
        "missing_files": missing, "invalid_files": invalid, "checked_files": sorted(set(checked)),
        "defects": defects,
    }


def _artifact_defects(
    raw: dict[str, Any], response_dir: Path, expected_main: str, member_index_override=None, *, allow_loose=True
) -> list[dict[str, Any]]:
    artifacts = raw.get("artifacts_manifest")
    if not isinstance(artifacts, list):
        return [
            _defect(
                "ARTIFACTS_MANIFEST_INVALID",
                expected_main,
                "artifacts_manifest as a JSON array",
                f"artifacts_manifest is {type(artifacts).__name__}",
            )
        ]

    defects: list[dict[str, Any]] = []
    seen = set()
    members = member_index_override
    for position, artifact in enumerate(artifacts, 1):
        label = f"artifacts_manifest[{position}]"
        if not isinstance(artifact, dict):
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    label,
                    "a JSON object per artifact",
                    f"entry is {type(artifact).__name__}",
                )
            )
            continue
        name = artifact.get("filename")
        try:
            safe_path(name)
            valid_name = True
        except (ValueError, TypeError):
            valid_name = False
        if not valid_name:
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    label,
                    "filename as a plain file name with no path separators",
                    f"filename has invalid type/value ({type(name).__name__})",
                )
            )
            continue
        folded = safe_path(name).casefold()
        if folded in seen:
            defects.append(_defect("ARTIFACT_ENTRY_INVALID", name, "unique artifact paths", "duplicate path"))
            continue
        seen.add(folded)
        if name.casefold() == expected_main.casefold():
            # Self-reference. The declared digest can never be right, because the
            # file would have to contain the hash of itself. Saying "recompute
            # the hash" here would ask for something impossible and the
            # correction round would never converge.
            defects.append(
                _defect(
                    "MAIN_JSON_LISTED_AS_ARTIFACT",
                    name,
                    "artifacts_manifest to list only the additional files, never "
                    "the main JSON itself",
                    f"artifacts_manifest contains an entry for {name}, which is "
                    "the main JSON. Remove that entry entirely. Keep the file "
                    "listed in delivery, which is correct.",
                )
            )
            continue
        status = artifact.get("status")
        if status not in {"CREATED", "MISSING", "NOT_CREATED"}:
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    name,
                    "status CREATED, MISSING, or NOT_CREATED",
                    f"status has invalid type/value ({type(status).__name__})",
                )
            )
            continue
        if status != "CREATED":
            continue

        declared_size = artifact.get("size")
        declared_digest = artifact.get("sha256")
        media_type = artifact.get("media_type")
        if not isinstance(media_type, str) or not media_type:
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    name,
                    "media_type as a non-empty string",
                    f"media_type has invalid type/value ({type(media_type).__name__})",
                )
            )
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    name,
                    "size as a non-negative integer byte count",
                    f"size has invalid type/value ({type(declared_size).__name__})",
                )
            )
            declared_size = None
        if not isinstance(declared_digest, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", declared_digest
        ):
            defects.append(
                _defect(
                    "ARTIFACT_ENTRY_INVALID",
                    name,
                    "sha256 as 64 hexadecimal characters",
                    f"sha256 has invalid type/value ({type(declared_digest).__name__})",
                )
            )
            declared_digest = None

        # A declared artifact is either a file beside the main JSON or a member of
        # the archive that came down with it. Looking only on disk made every
        # declared member of a byte-exact archive an ARTIFACT_MISSING defect, and
        # told the operator to spend a correction round repairing nothing.
        path = response_dir / name
        if allow_loose and path.is_file() and not path.is_symlink():
            resolved = _sha256(path)
        else:
            if members is None:
                members = archive_member_index(response_dir)
            resolved = members.get(safe_path(name).casefold())
        if resolved is None:
            defects.append(
                _defect(
                    "ARTIFACT_MISSING",
                    name,
                    "the file to be delivered, beside the main JSON or inside the "
                    "outputs archive",
                    "it was declared CREATED but is neither a delivered file nor a "
                    "member of a delivered archive",
                )
            )
            continue
        actual_digest, actual_size = resolved
        if declared_size is not None and declared_size != actual_size:
            defects.append(
                _defect(
                    "ARTIFACT_SIZE_MISMATCH",
                    name,
                    f"size {declared_size} bytes, as your manifest declared",
                    f"the delivered file is {actual_size} bytes",
                )
            )
        if declared_digest is not None and declared_digest.casefold() != actual_digest:
            defects.append(
                _defect(
                    "ARTIFACT_HASH_MISMATCH",
                    name,
                    f"sha256 {declared_digest.casefold()}, as your manifest declared",
                    f"the delivered file hashes to {actual_digest}",
                )
            )
    return defects


def _delivery_defects(
    raw: dict[str, Any],
    expected_main: str,
    response_dir: Path,
    expected_downloads: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    delivery = raw.get("delivery")
    if not isinstance(delivery, list) or any(
        not isinstance(item, str) for item in delivery
    ):
        return [
            _defect(
                "DELIVERY_INVALID",
                expected_main,
                "delivery as an array of file names",
                f"delivery has invalid type/entries ({type(delivery).__name__})",
            )
        ]

    # `delivery` names what came down as downloads, which is one archive with
    # the main JSON inside it. A created artifact that lives inside that archive
    # was never separately downloadable, so requiring it here reported a correct
    # delivery as incomplete. Only files that arrived on their own belong in the
    # requirement, and the main JSON no longer does — naming it stays acceptable
    # because it is the response and older exchanges did download it.
    delivered = {item.casefold() for item in delivery}
    required: set[str] = set()
    if not delivered & ({expected_main.casefold()} | {name.casefold() for name in expected_downloads}):
        required.add(expected_main.casefold())
    artifacts = raw.get("artifacts_manifest")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if (
                isinstance(artifact, dict)
                and artifact.get("status") == "CREATED"
                and isinstance(artifact.get("filename"), str)
                and (response_dir / artifact["filename"]).is_file()
            ):
                required.add(artifact["filename"].casefold())
    absent = sorted(required - delivered)
    if not absent:
        return []
    return [
        _defect(
            "DELIVERY_INCOMPLETE",
            expected_main,
            "delivery to name the outputs archive and every separately "
            "downloaded file",
            "delivery omits " + ", ".join(absent),
        )
    ]


def _defect(kind: str, target: str, expected: str, observed: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "target": target,
        "expected": expected,
        "observed": observed,
    }
