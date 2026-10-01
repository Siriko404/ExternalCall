from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from companion.core import (
    _active_path_for,
    _copy_stream,
    _exchange_dir,
    _read_json_object,
    _safe_name,
    _sha256,
    _write_json_atomic,
    load_active_call,
    load_active_calls,
)
from companion.lock import exchange_lock, state_lock


def handle_completed_download(root: Path, download: dict[str, Any]) -> dict[str, Any]:
    """Route one completed download to whichever active call named it.

    Attribution is by filename, not by tab, because Chrome does not tell an
    extension which tab produced a download. `start_call` therefore refuses to
    run two calls that expect the same main JSON name.
    """
    root = Path(root).resolve()
    download_id = download.get("id")
    if not isinstance(download_id, int) or download_id < 0:
        raise ValueError("download id must be a non-negative integer")
    if download.get("state", "complete") != "complete":
        raise ValueError("download is not complete")
    filename = download.get("filename")
    if not isinstance(filename, str) or not filename:
        raise ValueError("completed download filename is required")
    source = Path(filename)

    with state_lock(root):
        every = load_active_calls(root)
        if not every:
            raise RuntimeError("no call is active")
        watching = [record for record in every if record.get("monitoring")]
        if not watching:
            return {"status": "IGNORED", "reason": "monitoring is stopped"}
        if any(
            download_id in record.get("observed_download_ids", [])
            for record in watching
        ):
            return {"status": "DUPLICATE", "download_id": download_id}
        source = source.resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"completed download is not a regular file: {source.name}")
        candidates = [
            record
            for record in watching
            if download_id not in record.get("download_baseline", [])
        ]
        if not candidates:
            return {"status": "IGNORED", "reason": "download predates Go"}

        # A download under the main JSON's own name used to be accepted here.
        # It cannot be right any more: the main response is a member of the one
        # archive, so a loose copy beside it means the responder ignored the
        # rule, and filing it quietly would let a two-file delivery validate as
        # though it had obeyed. It falls through to IGNORED and is named there,
        # which is what tells the operator the rule was broken. The archive is
        # still the delivery, and if the response is not inside it the archive
        # is refused with that filename in the error.
        named_archive = [
            record
            for record in candidates
            if _expected_archive(record)
            and _matches_expected_name(source.name, _expected_archive(record))
        ]
        if len(named_archive) > 1:
            return {
                "status": "AMBIGUOUS",
                "download_id": download_id,
                "error": "several active calls expect "
                f"{source.name}: "
                + ", ".join(str(record["exchange_id"]) for record in named_archive),
            }
        if named_archive:
            return _accept_outputs_archive(
                root, named_archive[0], source, download_id
            )

        # There used to be more here: a download could be an artifact named by
        # a main JSON that had already landed, and one that arrived before any
        # main JSON went into a shared pending pool to be attributed later. Both
        # existed because a delivery came down as several files in an order
        # nobody controlled.
        #
        # A delivery is one archive now, so neither case can occur, and the pool
        # is gone with them. It is worth saying why rather than only that it is
        # simpler: a download parked in that pool was invisible. Three separate
        # calls ended with files sitting in the Downloads folder, the panel
        # showing nothing wrong, and validation later reporting them missing.
        # An unrecognised filename is now reported as ignored, immediately.
        return {
            "status": "IGNORED",
            "reason": f"no active call expects {source.name}",
        }


def default_downloads_dir() -> Path:
    """Where Chrome writes downloads before they are ingested.

    Shared by every entry point that finishes a call, so the side panel and the
    CLI agree. EXTERNALCALL_DOWNLOADS_DIR overrides it, which is also what lets a
    test point the ingest at a temporary folder instead of the real one.
    """
    chosen = os.environ.get("EXTERNALCALL_DOWNLOADS_DIR") or os.environ.get("GPTWEBCALL_DOWNLOADS_DIR")
    return Path(chosen) if chosen else Path.home() / "Downloads"


def ingest_from_downloads(
    root: Path, exchange_id: str, downloads_dir: Path
) -> dict[str, Any]:
    """Pull an exchange's expected files straight out of the Downloads folder.

    Chrome always writes a download to the Downloads folder first; the extension
    was then relied on to move it into the exchange, and when that step failed
    the files sat in Downloads and validation found nothing. This function makes
    the companion do the move itself, keyed off the prepare-time contract
    (expected_main_json and expected_artifacts) rather than anything the model
    self-reports. It is deliberately conservative: it copies by exact expected
    name (Chrome "(1)" suffixes included), takes the newest match no older than
    the call, never overwrites different bytes, and leaves the Downloads copy in
    place. It is a no-op when the files are already collected.
    """
    root = Path(root).resolve()
    downloads_dir = Path(downloads_dir)
    exchange = _exchange_dir(root, exchange_id)
    manifest = _read_json_object(
        exchange / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    expected = [
        _safe_name(str(manifest["expected_main_json"]), "expected main JSON filename")
    ]
    for artifact in manifest.get("expected_artifacts", []):
        expected.append(_safe_name(str(artifact), "expected artifact filename"))

    ingested: list[str] = []
    already: list[str] = []
    conflicts: list[str] = []
    not_found: list[str] = []
    if not downloads_dir.is_dir():
        return {
            "status": "NO_DOWNLOADS_DIR",
            "downloads_dir": str(downloads_dir),
            "not_found": expected,
        }

    repairs = manifest.get("repairs", [])
    repaired_at = repairs[-1].get("opened_at") if repairs else None
    floor = _download_floor(repaired_at or manifest.get("created_at"))
    supersede_round = _supersede_round(manifest) if manifest.get("contract_version", 1) == 1 else None
    for name in expected:
        destination = exchange / "response" / name
        candidate = _newest_download_match(downloads_dir, name, floor)
        if candidate is None:
            (already if destination.is_file() else not_found).append(name)
            continue
        outcome = _safe_copy(candidate, destination, supersede_round)
        if outcome == "COPIED":
            ingested.append(name)
        elif outcome == "IDENTICAL":
            already.append(name)
        else:
            conflicts.append(name)

    # The main JSON is not a download any more: it comes back inside the one
    # archive. Nothing will ever be found for its name in the Downloads folder,
    # so lift it out of the archive that was.
    expected_main = expected[0]
    if expected_main in not_found or (supersede_round is not None and ingested):
        archive = _delivered_archive(exchange, manifest)
        if archive is not None:
            try:
                _extract_main_from_archive(archive, expected_main, exchange, supersede_round)
            except FileExistsError:
                if expected_main in not_found:
                    not_found.remove(expected_main)
                conflicts.append(expected_main)
            except (OSError, ValueError, zipfile.BadZipFile):
                pass
            else:
                if expected_main in not_found:
                    not_found.remove(expected_main)
                ingested.append(expected_main)
    return {
        "status": "OK",
        "downloads_dir": str(downloads_dir),
        "ingested": ingested,
        "already_present": already,
        "conflicts": conflicts,
        "not_found": not_found,
    }


def _delivered_archive(exchange: Path, manifest: dict[str, Any]) -> Path | None:
    """The call's own outputs archive, once it is sitting in `response\\`."""
    for declared in manifest.get("expected_artifacts", []):
        name = _safe_name(str(declared), "expected artifact filename")
        path = exchange / "response" / name
        if path.is_file():
            return path
    return None


def _download_floor(created_at: Any) -> float | None:
    """The earliest mtime an ingested download may have, from the call's own time.

    Expected filenames are released for reuse once a call finishes, so a stale
    file left in Downloads by an earlier call that reused the name must not be
    picked up. The download finished after the call was prepared, so its mtime is
    at or after created_at.
    """
    if not isinstance(created_at, str):
        return None
    try:
        return datetime.fromisoformat(created_at).timestamp()
    except ValueError:
        return None


def _newest_download_match(
    downloads_dir: Path, expected: str, floor: float | None
) -> Path | None:
    best: Path | None = None
    best_mtime = -1.0
    for entry in downloads_dir.iterdir():
        if not entry.is_file() or entry.name.endswith(".crdownload"):
            continue
        if not _matches_expected_name(entry.name, expected):
            continue
        mtime = entry.stat().st_mtime
        if floor is not None and mtime < floor:
            continue
        if mtime > best_mtime:
            best, best_mtime = entry, mtime
    return best


def _safe_copy(source: Path, destination: Path, supersede_round: int | None = None) -> str:
    """Copy a download into the response dir, leaving the original in Downloads.

    Mirrors _safe_move's guard: an identical file is a no-op, and different bytes
    already in place are never overwritten. Unlike _safe_move it keeps the source
    so a re-validate remains possible and Downloads stays the record of truth.
    """
    source = source.resolve(strict=True)
    if source == destination.resolve():
        return "IDENTICAL"
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_digest, source_size = _sha256(source)
    if destination.exists():
        destination_digest, destination_size = _sha256(destination)
        if source_digest == destination_digest and source_size == destination_size:
            return "IDENTICAL"
        if supersede_round is None:
            return "CONFLICT"
        _archive_superseded(destination, supersede_round)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".incoming-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            _copy_stream(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        copied_digest, copied_size = _sha256(temporary)
        if copied_digest != source_digest or copied_size != source_size:
            raise OSError(f"copied download changed: {source.name}")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return "COPIED"


# A nested packaging archive is still "inside the outputs archive": responders
# group replacement files in an inner zip (e.g. wc15_visual_repo.zip inside
# wc15_outputs.zip). Depth and byte caps keep a hostile archive from turning
# the validator into a decompression bomb.
def archive_member_index(response_dir: Path) -> dict[str, tuple[str, int]]:
    from companion.archives import member_index
    return member_index(response_dir)


def finish_call(root: Path, exchange_id: str | None = None, downloads_dir: Path | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    initial = load_active_call(root, exchange_id)
    if initial is None:
        if exchange_id is not None:
            exchange = _exchange_dir(root, exchange_id)
            report_path = exchange / "validation" / "VALIDATION_REPORT.json"
            if report_path.is_file():
                report = _read_json_object(report_path, "validation report")
                if report.get("exchange_id") == exchange_id:
                    return report
        raise RuntimeError("no call is active")
    exchange_id = initial["exchange_id"]
    exchange = _exchange_dir(root, exchange_id)
    with exchange_lock(exchange):
        with state_lock(root):
            active = load_active_call(root, exchange_id)
            manifest_path = exchange / "EXCHANGE_MANIFEST.json"
            manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
            report_path = exchange / "validation" / "VALIDATION_REPORT.json"
            # Recover a crash after committing completion but before clearing active.
            if manifest.get("state") in {"COMPLETE", "INCOMPLETE"} and report_path.is_file():
                report = _read_json_object(report_path, "validation report")
                if report.get("exchange_id") != exchange_id:
                    raise ValueError("validation report belongs to another exchange")
                _active_path_for(root, exchange_id).unlink(missing_ok=True)
                return report
            if active is None or manifest.get("state") != "ACTIVE":
                raise RuntimeError("call is no longer active")
            operation = uuid.uuid4().hex
            active["monitoring"] = False
            active["operation_id"] = operation
            _save_active(root, active)
            binding = (active.get("started_at"), active.get("tab_id"), active.get("repair_round"))
        if downloads_dir is not None:
            ingest_from_downloads(root, exchange_id, downloads_dir)
        report = validate_response(exchange)
        with state_lock(root):
            current = load_active_call(root, exchange_id)
            manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
            if (current is None or current.get("operation_id") != operation or current.get("monitoring") or
                    (current.get("started_at"), current.get("tab_id"), current.get("repair_round")) != binding or
                    manifest.get("state") != "ACTIVE"):
                raise RuntimeError("call changed while validating; response evidence retained")
            _write_json_atomic(report_path, report)
            manifest["state"] = report["status"]
            _write_json_atomic(manifest_path, manifest)
            _write_json_atomic(root / "state" / "LAST_RESULT.json", {"exchange_id": exchange_id, "report": report})
            _active_path_for(root, exchange_id).unlink(missing_ok=True)
    return report


def _accept_outputs_archive(
    root: Path, record: dict[str, Any], source: Path, download_id: int
) -> dict[str, Any]:
    """File the one archive a call comes back as, and lift the main JSON out.

    One zip comes down carrying everything, so this is the whole delivery
    arriving in a single event. Nothing has to be held pending waiting for
    another file to explain it, and a call can no longer half-arrive: either the
    archive is here or nothing is.

    The archive is filed first and kept even when the main JSON inside it is
    unusable. It is the evidence, and discarding it would leave an operator with
    an error message and nothing to inspect.
    """
    exchange = Path(record["exchange_path"]).resolve()
    archive_name = _expected_archive(record)
    expected_main = _expected_main(record)
    supersede_round = _supersede_round(record)
    destination = exchange / "response" / archive_name
    try:
        _safe_move(source, destination, supersede_round)
    except FileExistsError as error:
        _save_active(root, record)
        return {"status": "CONFLICT", "error": str(error)}
    except OSError as error:
        _save_active(root, record)
        return {"status": "INVALID", "error": str(error)}
    _record_collected(record, archive_name)
    record.setdefault("observed_download_ids", []).append(download_id)

    try:
        _extract_main_from_archive(destination, expected_main, exchange, supersede_round)
        main = _parse_main_response(
            exchange / "response" / expected_main,
            record["request_id"],
            expected_main,
            (archive_name,),
        )
    except FileExistsError as error:
        _save_active(root, record)
        return {"status": "CONFLICT", "error": str(error)}
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        _save_active(root, record)
        return {
            "status": "INVALID",
            "download_id": download_id,
            "exchange_id": record["exchange_id"],
            "stored_name": archive_name,
            "error": str(error),
        }
    _record_collected(record, expected_main)
    _save_active(root, record)
    return {
        "status": "MOVED",
        "download_id": download_id,
        "exchange_id": record["exchange_id"],
        "stored_name": archive_name,
        "extracted_main": expected_main,
    }


def _extract_main_from_archive(
    archive: Path, expected_main: str, exchange: Path, supersede_round: int | None
) -> None:
    """Write the main JSON out of the delivered archive, beside it.

    Everything downstream reads the main response from `response\\` as a file,
    so lifting it out here means validation, defects and repair need to know
    nothing about where it travelled.

    The member is read by name rather than extracted, so no member path can
    decide where anything lands. A responder that casefolds the name differently
    is still accepted; one that omits it has not delivered the response.
    """
    with zipfile.ZipFile(archive) as bundle:
        from companion.archives import MAX_MAIN_BYTES, safe_path
        wanted = expected_main.casefold()
        candidates = [entry for entry in bundle.infolist()
                      if safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename).casefold() == wanted]
        if len(candidates) != 1:
            raise ValueError(
                f"{archive.name} must contain exactly one main response {expected_main}"
            )
        member = candidates[0]
        if member.file_size > MAX_MAIN_BYTES:
            raise ValueError("main response exceeds byte budget")
        with bundle.open(member) as stream:
            payload = stream.read(MAX_MAIN_BYTES + 1)
        if len(payload) > MAX_MAIN_BYTES:
            raise ValueError("main response exceeds byte budget")

    destination = exchange / "response" / expected_main
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = destination.read_bytes()
        if existing == payload:
            return
        if supersede_round is None:
            raise FileExistsError(
                f"response already contains different bytes: {expected_main}"
            )
        _archive_superseded(destination, supersede_round)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".incoming-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as outgoing:
            outgoing.write(payload)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _expected_archive(record: dict[str, Any]) -> str | None:
    """The one outputs archive an active call expects, read from its manifest.

    Calls prepared before one-zip-out expected the main JSON as its own
    download; those have no archive to match and return None so the older
    routing still applies to them.
    """
    try:
        manifest = _read_json_object(
            Path(record["exchange_path"]) / "EXCHANGE_MANIFEST.json",
            "EXCHANGE_MANIFEST",
        )
    except (FileNotFoundError, ValueError, KeyError):
        return None
    declared = manifest.get("expected_artifacts")
    if not isinstance(declared, list) or len(declared) != 1:
        return None
    return _safe_name(str(declared[0]), "expected artifact filename")


def _expected_main(record: dict[str, Any]) -> str:
    """The main JSON filename for an active call.

    Records written before parallel calls existed do not carry the name, so it
    is read back from the exchange manifest.
    """
    name = record.get("expected_main_json")
    if not name:
        manifest = _read_json_object(
            Path(record["exchange_path"]) / "EXCHANGE_MANIFEST.json",
            "EXCHANGE_MANIFEST",
        )
        name = manifest["expected_main_json"]
    return _safe_name(str(name), "expected main JSON filename")


def validate_response(exchange_dir: Path) -> dict[str, Any]:
    from companion.validation import validate_response as validate
    return validate(exchange_dir)


def finalize_exchange(root: Path, exchange_id: str, downloads_dir: Path | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    exchange = _exchange_dir(root, exchange_id)
    with exchange_lock(exchange):
        with state_lock(root):
            if load_active_call(root, exchange_id) is not None:
                raise RuntimeError("an active call must be finished with done or stop")
            manifest_path = exchange / "EXCHANGE_MANIFEST.json"
            manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
            before = manifest.get("state")
            if before not in {"PREPARED", "INCOMPLETE", "COMPLETE"}:
                raise RuntimeError(f"exchange cannot be validated from state {before}")
        if downloads_dir is not None:
            ingest_from_downloads(root, exchange_id, downloads_dir)
        if before == "PREPARED" and not any((exchange / "response").iterdir()):
            raise RuntimeError("nothing to validate; call has not been answered; use show")
        report = validate_response(exchange)
        with state_lock(root):
            current = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
            if load_active_call(root, exchange_id) is not None or current.get("state") != before:
                raise RuntimeError("call changed while validating; response evidence retained")
            _write_json_atomic(exchange / "validation" / "VALIDATION_REPORT.json", report)
            current["state"] = report["status"]
            _write_json_atomic(manifest_path, current)
            _write_json_atomic(root / "state" / "LAST_RESULT.json", {"exchange_id": exchange_id, "report": report})
        return report


def _parse_main_response(
    path: Path,
    request_id: str,
    expected_main: str,
    delivered_as: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    main = _read_json_object(path, "main response")
    if main.get("request_id") != request_id:
        raise ValueError("main response request_id does not match the active call")
    if not isinstance(main.get("status"), str) or main.get("status") not in {"COMPLETE", "PARTIAL", "BLOCKED"}:
        raise ValueError("main response status is invalid")
    artifacts = main.get("artifacts_manifest")
    if not isinstance(artifacts, list):
        raise ValueError("main response artifacts_manifest must be a list")
    delivery = main.get("delivery")
    if not isinstance(delivery, list) or any(
        not isinstance(item, str) for item in delivery
    ):
        raise ValueError("main response delivery must be a filename list")
    if len({item.casefold() for item in delivery}) != len(delivery):
        raise ValueError("main response delivery contains duplicate filenames")

    seen: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ValueError("artifact manifest entries must be objects")
        from companion.archives import safe_path
        name = safe_path(artifact.get("filename", ""))
        folded = name.casefold()
        if folded in seen or folded == expected_main.casefold():
            raise ValueError(f"artifact filename is duplicate or reserved: {name}")
        seen.add(folded)
        status = artifact.get("status")
        if not isinstance(status, str) or status not in {"CREATED", "MISSING", "NOT_CREATED"}:
            raise ValueError(f"artifact status is invalid: {name}")
        artifact["filename"] = name
        if status == "CREATED":
            size = artifact.get("size")
            digest = artifact.get("sha256")
            media_type = artifact.get("media_type")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError(f"artifact size is invalid: {name}")
            if not isinstance(digest, str) or not re.fullmatch(
                r"[0-9a-fA-F]{64}", digest
            ):
                raise ValueError(f"artifact hash is invalid: {name}")
            if not isinstance(media_type, str) or not media_type:
                raise ValueError(f"artifact media_type is invalid: {name}")
            artifact["sha256"] = digest.casefold()

    # `delivery` names what came down as downloads, which is now one archive
    # with this file inside it. Naming the main JSON is still accepted, because
    # it is the response and a responder listing it is not wrong, and because
    # exchanges prepared before one-zip-out did download it under that name.
    # Naming neither means the response is describing some other delivery.
    #
    # It cannot be required to name every created artifact: a created artifact
    # is usually a member of the archive and was never separately downloadable.
    # This function gates whether the file may be filed at all, so it checks
    # identity only. Whether every created file was accounted for is decided by
    # `collect_defects`, once everything has landed and members can be resolved.
    named = {item.casefold() for item in delivery}
    acceptable = {expected_main.casefold()} | {
        item.casefold() for item in delivered_as or ()
    }
    if not (named & acceptable):
        raise ValueError("main response delivery does not name what came down")
    return main


def _supersede_round(active: dict[str, Any]) -> int | None:
    value = active.get("repair_round")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _safe_move(
    source: Path, destination: Path, supersede_round: int | None = None
) -> None:
    source = source.resolve(strict=True)
    if source == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_digest, source_size = _sha256(source)
    if destination.exists():
        destination_digest, destination_size = _sha256(destination)
        if source_digest == destination_digest and source_size == destination_size:
            source.unlink()
            return
        if supersede_round is None:
            raise FileExistsError(
                f"response already contains different bytes: {destination.name}"
            )
        _archive_superseded(destination, supersede_round)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".incoming-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            _copy_stream(incoming, outgoing)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        copied_digest, copied_size = _sha256(temporary)
        if copied_digest != source_digest or copied_size != source_size:
            raise OSError(f"copied download changed: {source.name}")
        os.replace(temporary, destination)
        source.unlink()
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _archive_superseded(destination: Path, supersede_round: int) -> None:
    """Move a rejected earlier delivery aside so a correction round can replace it."""
    archive = destination.parent / "superseded" / f"round{supersede_round}"
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / destination.name
    attempt = 1
    while target.exists():
        attempt += 1
        stem = Path(destination.name).stem
        suffix = Path(destination.name).suffix
        target = archive / f"{stem} ({attempt}){suffix}"
    os.replace(destination, target)


def _matches_expected_name(actual: str, expected: str) -> bool:
    if actual.casefold() == expected.casefold():
        return True
    actual_path = Path(actual)
    expected_path = Path(expected)
    if actual_path.suffix.casefold() != expected_path.suffix.casefold():
        return False
    pattern = re.compile(
        rf"^{re.escape(expected_path.stem)} \([0-9]+\)$", re.IGNORECASE
    )
    return pattern.fullmatch(actual_path.stem) is not None


def _record_collected(active: dict[str, Any], name: str) -> None:
    collected = active.setdefault("collected_files", [])
    if name not in collected:
        collected.append(name)


def _save_active(root: Path, active: dict[str, Any]) -> None:
    _write_json_atomic(_active_path_for(root, active["exchange_id"]), active)
