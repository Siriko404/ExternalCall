from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from companion.lock import state_lock


_TIMESTAMP = "%Y-%m-%d_%H%M%S"
MAX_FILE_BYTES = 512 * 1024 * 1024
# The prompt's name inside the archive. The bundle is written in casefolded name
# order, so the leading 000 puts it first in the archive and first in any listing
# a model prints - inside an archive the name is the only thing telling a model
# which file governs.
PROMPT_IN_BUNDLE_NAME = "000_READ_ME_FIRST.md"
# How many defects `inspect_call` carries back to the panel. The native
# messaging frame has a hard one-megabyte ceiling and overflowing it fails the
# whole command; `defects --exchange <id>` still reports every one.
_DEFECTS_IN_INSPECT = 25
# Which states can be sent again. A call that can still receive files is not
# finished, and cloning one would duplicate a delivery in flight.
CLONEABLE_STATES = {"COMPLETE", "INCOMPLETE", "STOPPED"}
# How much of the archive the panel gets. It was twelve, which is a window on
# the archive rather than the archive, and the panel now presents this list as
# every past call. The number is still finite because the native-messaging frame
# has a hard one-megabyte ceiling and overflowing it fails the whole command:
# these rows are a few hundred bytes each, so this is roughly a twentieth of it.
_RECENT_LIMIT = 200
# A project name leads every name an operator handles: the exchange folder, the
# archive that goes up and the archive that comes back. Several projects send
# calls through one panel and one Downloads folder, and without it the names
# say which attempt a file is and nothing about whose.
PROJECT_NAME = r"[a-z0-9][a-z0-9_-]{0,31}"
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def prepare_call(root: Path, spec: dict[str, Any], now: datetime, *, _reexport=False) -> dict[str, Any]:
    root = Path(root).resolve()
    if now.tzinfo is None:
        raise ValueError("creation time must include a timezone")

    subject = str(spec.get("subject", "")).strip()
    slug = _subject_slug(subject)
    request_id = str(spec.get("request_id", "")).strip()
    if not request_id:
        raise ValueError("request_id is required")
    expected_main = _safe_name(
        str(spec.get("expected_main_json", "")), "expected main JSON filename"
    )
    if Path(expected_main).suffix.casefold() != ".json":
        raise ValueError("expected main JSON filename must end in .json")
    prompt_text = spec.get("prompt_text")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        raise ValueError("prompt_text is required")
    input_files = spec.get("input_files")
    if not isinstance(input_files, list) or not input_files:
        raise ValueError("input_files must be a non-empty list")
    expected_artifacts = _expected_artifact_names(spec)
    if len(input_files) > 1005:
        raise ValueError("input file-count budget exceeded")

    # One zip goes up, and nothing else. ChatGPT now refuses loose .md
    # attachments outright, so a prompt uploaded beside the archive strands the
    # call it was meant to govern. The prompt therefore lives inside the archive
    # under PROMPT_IN_BUNDLE_NAME.
    if spec.get("prompt_in_bundle") is False:
        raise ValueError(
            "prompt_in_bundle cannot be turned off: exactly one zip goes up, "
            "because ChatGPT refuses loose .md attachments"
        )

    timestamp = now.strftime(_TIMESTAMP)
    token = spec.get("exchange_token")
    if token is not None and not re.fullmatch(r"[0-9a-f]{12}", token):
        raise ValueError("exchange_token must be 12 hexadecimal characters")
    project = spec.get("project")
    if project is not None and (
        not isinstance(project, str) or not re.fullmatch(PROJECT_NAME, project)
    ):
        raise ValueError(
            "project must be a short lowercase name: letters, digits, - or _"
        )
    lead = f"{project}_" if project else ""
    exchange_id = f"{timestamp}_{lead}{slug}" + (f"_{token}" if token else "")
    prompt_name = PROMPT_IN_BUNDLE_NAME
    bundle_name = f"{lead}{slug}_inputs.zip"
    calls_dir = root / "calls"
    state_dir = root / "state"
    destination = calls_dir / exchange_id
    if destination.exists():
        raise FileExistsError(f"exchange already exists: {exchange_id}")

    prepared_inputs: list[tuple[Path, str]] = []
    seen = {prompt_name.casefold(), bundle_name.casefold()}
    for item in input_files:
        if not isinstance(item, dict):
            raise ValueError("each input file must be an object")
        source_text = item.get("path")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("input file path is required")
        source = Path(source_text).expanduser()
        try:
            source = source.resolve(strict=True)
        except FileNotFoundError as error:
            raise FileNotFoundError(f"input file does not exist: {source.name}") from error
        if not source.is_file():
            raise ValueError(f"input path is not a regular file: {source.name}")
        _check_export_source(root, source, reexport=_reexport)
        packaged_name = _safe_name(
            str(item.get("filename", "")), "input filename"
        )
        if packaged_name.upper().startswith("PROMPT_"):
            raise ValueError("PROMPT files are generated by the companion")
        folded = packaged_name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate input filename: {packaged_name}")
        seen.add(folded)
        prepared_inputs.append((source, packaged_name))
    if sum(path.stat().st_size for path, _ in prepared_inputs) + len(prompt_text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError("input package exceeds expanded-byte budget")

    required_names = {name.casefold() for _, name in prepared_inputs}
    for required in ("WEB_REVIEW_REQUEST.json", "WEB_RESPONSE_SCHEMA.json"):
        if required.casefold() not in required_names:
            raise ValueError(f"required input is missing: {required}")
    # Canonical governing names are needed on case-sensitive filesystems.
    prepared_inputs = [
        (source, next((required for required in ("WEB_REVIEW_REQUEST.json", "WEB_RESPONSE_SCHEMA.json")
                       if name.casefold() == required.casefold()), name))
        for source, name in prepared_inputs
    ]

    state_dir.mkdir(parents=True, exist_ok=True)
    calls_dir.mkdir(parents=True, exist_ok=True)
    _assert_deliverable_names_are_free(
        root, exchange_id, [expected_main, *expected_artifacts]
    )
    staging = Path(tempfile.mkdtemp(prefix=f".{exchange_id}-", dir=state_dir))
    try:
        request_dir = staging / "request"
        (staging / "response").mkdir(parents=True)
        (staging / "validation").mkdir()
        request_dir.mkdir()

        prompt_path = request_dir / prompt_name
        _write_bytes_durable(prompt_path, prompt_text.encode("utf-8"))
        request_files = [_file_record(prompt_path)]

        for source, packaged_name in prepared_inputs:
            target = request_dir / packaged_name
            _copy_verified(source, target)
            request_files.append(_file_record(target))

        # Everything travels as one archive, prompt included. The individual
        # files stay on disk beside it: they are the provenance record and the
        # thing whose hashes are checked, while the archive is what is uploaded.
        bundle_path = request_dir / bundle_name
        bundled = [name for _, name in prepared_inputs] + [prompt_name]
        _write_bundle(bundle_path, bundled, request_dir)
        request_files.append(_file_record(bundle_path))

        governing = _read_json_object(
            request_dir / "WEB_REVIEW_REQUEST.json", "WEB_REVIEW_REQUEST"
        )
        if governing.get("request_id") != request_id:
            raise ValueError("WEB_REVIEW_REQUEST request_id does not match the call")
        response_schema = _read_json_object(
            request_dir / "WEB_RESPONSE_SCHEMA.json", "WEB_RESPONSE_SCHEMA"
        )
        from companion.schema import validator
        validator(response_schema)

        manifest = {
            "schema_version": 1,
            "exchange_id": exchange_id,
            "request_id": request_id,
            "subject": subject,
            "project": project,
            "created_at": now.astimezone(timezone.utc).isoformat(),
            "state": "PREPARED",
            # Set only by clone_call. Two exchanges with the same subject differ
            # by timestamp alone, which is not enough to tell a resend from a
            # coincidence when reading the archive later.
            "cloned_from": spec.get("cloned_from") or None,
            "expected_main_json": expected_main,
            "expected_artifacts": expected_artifacts,
            "request_files": request_files,
            "attach_files": [bundle_name],
            "response_dir": "response",
            "contract_version": spec.get("contract_version", 1),
            "job_revision": spec.get("job_revision"),
            "expected_deliverables": spec.get("expected_deliverables", []),
            "job_brief": spec.get("job_brief"),
            "repair_round": spec.get("repair_round", 0),
        }
        _write_json_atomic(staging / "EXCHANGE_MANIFEST.json", manifest)
        # The check above runs before staging so an author learns about a taken
        # name immediately. This one is the guarantee. Copying and hashing every
        # input sits between them and is unbounded, so a second preparation can
        # start in that window, see the same names free, and publish first. The
        # re-check and the publication happen together under the lock, which is
        # what makes claiming a deliverable name a single step.
        with state_lock(root):
            _assert_deliverable_names_are_free(
                root, exchange_id, [expected_main, *expected_artifacts]
            )
            if destination.exists():
                raise FileExistsError(f"exchange already exists: {exchange_id}")
            os.replace(staging, destination)
            _sync_directory(calls_dir)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def clone_call(root: Path, exchange_id: str, now: datetime) -> dict[str, Any]:
    """Prepare the same request again, as a new exchange.

    A finished call is a dead end otherwise: start_call takes PREPARED alone, so
    asking the same question twice means asking it as a new call. The inputs are
    still on disk beside the response, so the preparation is rebuilt here rather
    than re-authored.

    It is rebuilt from the manifest's own record of the request files, not from
    a listing of the directory. A correction round writes into `repair/` today
    and nothing writes into `request/` after preparation, but a clone driven by
    whatever happens to be lying in that directory would quietly become a
    different call the day something does. The manifest is the record of what
    was sent; that is what gets sent again.

    Refused unless the source has finished. A PREPARED or ACTIVE exchange still
    holds its deliverable names, so the clone would be refused by the name check
    anyway - with a message about filenames rather than about the thing actually
    being asked for.

    The resend expects the same filename as the original, whose download is
    normally still sitting in the Downloads folder, so Chrome saves the second
    one as `name (1).zip`. That is already handled: attribution matches the
    dedupe suffix (see _matches_expected_name in downloads.py).
    """
    root = Path(root).resolve()
    source_dir = _exchange_dir(root, exchange_id)
    manifest = _read_json_object(
        source_dir / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    state = str(manifest.get("state", ""))
    if state not in CLONEABLE_STATES:
        raise RuntimeError(
            f"call {exchange_id} is {state or 'in an unknown state'} and has not "
            "finished. Only a finished call can be sent again: stop it first, or "
            "wait for it."
        )

    request_dir = source_dir / "request"
    if manifest.get("contract_version", 1) >= 2:
        from companion.jobs import reprepare_job
        return reprepare_job(root, source_dir, now)
    request_paths(root, exchange_id)
    # The prompt travels inside the archive and the archive is rebuilt from the
    # inputs, so neither is an input to the new preparation.
    packed = {str(name).casefold() for name in manifest.get("attach_files", [])}
    packed.add(PROMPT_IN_BUNDLE_NAME.casefold())
    input_files: list[dict[str, str]] = []
    for record in manifest.get("request_files", []):
        if not isinstance(record, dict):
            continue
        name = str(record.get("filename", ""))
        if not name or name.casefold() in packed:
            continue
        input_files.append({"path": str(request_dir / name), "filename": name})
    if not input_files:
        raise RuntimeError(
            f"call {exchange_id} records no input files to send again"
        )

    prompt_path = request_dir / PROMPT_IN_BUNDLE_NAME
    if not prompt_path.is_file():
        raise FileNotFoundError(
            f"call {exchange_id} no longer has its prompt "
            f"({PROMPT_IN_BUNDLE_NAME}); there is nothing to send again"
        )

    return prepare_call(
        root,
        {
            "subject": manifest.get("subject", ""),
            "project": manifest.get("project"),
            "request_id": manifest.get("request_id", ""),
            "expected_main_json": manifest.get("expected_main_json", ""),
            "expected_artifacts": manifest.get("expected_artifacts", []),
            "prompt_text": prompt_path.read_text(encoding="utf-8"),
            "input_files": input_files,
            "cloned_from": exchange_id,
        },
        now,
        _reexport=True,
    )


def _expected_artifact_names(spec: dict[str, Any]) -> list[str]:
    declared = spec.get("expected_artifacts", [])
    if not isinstance(declared, list):
        raise ValueError("expected_artifacts must be a list of file names")
    names: list[str] = []
    seen: set[str] = set()
    for item in declared:
        if not isinstance(item, str):
            raise ValueError("expected_artifacts must contain file names")
        name = _safe_name(item, "expected artifact filename")
        if name.casefold() in seen:
            raise ValueError(f"duplicate expected artifact: {name}")
        seen.add(name.casefold())
        names.append(name)
    # One archive comes back and everything is inside it, the main JSON
    # included. One name is therefore the only thing download routing ever has
    # to attribute, and a call cannot half-arrive: either the archive is there
    # or nothing is.
    if len(names) != 1 or Path(names[0]).suffix.casefold() != ".zip":
        raise ValueError(
            "expected_artifacts must be exactly one .zip; the main JSON and "
            "every returned file come back inside that one archive"
        )
    return names


def _write_bundle(target: Path, names: list[str], source_dir: Path) -> None:
    """Pack the request files into one archive, byte-identical run to run.

    Ordinary zipfile writes embed each file's modification time, so the same
    inputs would produce a different digest on every preparation and the archive
    could not be verified against its own manifest record. Fixing the timestamp
    and the order removes both sources of variation.
    """
    with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as bundle:
        for name in sorted(names, key=str.casefold):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.file_size = (source_dir / name).stat().st_size
            with (source_dir / name).open("rb") as source, bundle.open(info, "w") as outgoing:
                _copy_stream(source, outgoing)
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("input ZIP exceeds compressed-byte budget")
    with target.open("r+b") as stream:
        os.fsync(stream.fileno())


def claimed_deliverable_names(
    root: Path, exclude_exchange_id: str | None = None
) -> dict[str, str]:
    """Map every deliverable filename already spoken for to its exchange.

    Downloads are attributed by filename, so a name may be claimed by only one
    exchange that is still able to receive files. Exchanges that are finished,
    stopped, or incomplete no longer hold their names.
    """
    calls_dir = Path(root).resolve() / "calls"
    if not calls_dir.is_dir():
        return {}
    claimed: dict[str, str] = {}
    for manifest_path in calls_dir.glob("*/EXCHANGE_MANIFEST.json"):
        manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
        if manifest.get("state") not in {"PREPARED", "ACTIVE"}:
            continue
        exchange_id = str(manifest.get("exchange_id", ""))
        if exchange_id == exclude_exchange_id:
            continue
        names = [
            str(manifest.get("expected_main_json", "")),
            *[str(item) for item in manifest.get("expected_artifacts", [])],
        ]
        for name in names:
            if name:
                claimed.setdefault(name.casefold(), exchange_id)
    return claimed


def _assert_deliverable_names_are_free(
    root: Path, exchange_id: str, names: list[str]
) -> None:
    claimed = claimed_deliverable_names(root, exclude_exchange_id=exchange_id)
    for name in names:
        owner = claimed.get(name.casefold())
        if owner:
            raise ValueError(
                f"the filename {name} is already claimed by call {owner}. "
                "Downloads are routed by filename, so every call that can still "
                "receive files needs its own deliverable names. Prefix this "
                "call's filenames with its pass name and prepare it again."
            )


def list_ready_calls(root: Path) -> list[dict[str, Any]]:
    calls_dir = Path(root).resolve() / "calls"
    if not calls_dir.exists():
        return []
    ready: list[dict[str, Any]] = []
    for manifest_path in calls_dir.glob("*/EXCHANGE_MANIFEST.json"):
        manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
        if manifest.get("state") == "PREPARED":
            ready.append(manifest)
    return sorted(ready, key=lambda item: item["exchange_id"])


def list_recent_calls(root: Path, limit: int = _RECENT_LIMIT) -> list[dict[str, Any]]:
    """Every exchange, newest first, for the panel's archive.

    Exchange ids begin with their timestamp, so sorting the name sorts the time.

    A row carries the delivery verdict and nothing about the work. `state` is
    this end of the exchange: whether every promised file arrived and matched
    the hash declared for it. What the responder said about its own work is in
    the validation report, which the operator reads; the panel reports on the
    pipe. A row briefly carried `response_status` too, so a COMPLETE delivery of
    self-declared PARTIAL work showed as PARTIAL - which makes an intact
    delivery worth reading look like a failure, the exact misreading the three
    separate facts existed to prevent.
    """
    calls_dir = Path(root).resolve() / "calls"
    if not calls_dir.exists():
        return []
    recent: list[dict[str, Any]] = []
    for manifest_path in calls_dir.glob("*/EXCHANGE_MANIFEST.json"):
        manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
        recent.append(
            {
                "exchange_id": manifest.get("exchange_id"),
                "subject": manifest.get("subject"),
                "project": manifest.get("project"),
                "request_id": manifest.get("request_id"),
                "state": manifest.get("state"),
                "created_at": manifest.get("created_at"),
                "cloned_from": manifest.get("cloned_from") or None,
                "repair_round": int(manifest.get("repair_round", 0) or 0),
            }
        )
    recent.sort(key=lambda item: str(item["exchange_id"]), reverse=True)
    return recent[: max(0, int(limit))]


def call_progress(root: Path) -> list[dict[str, Any]]:
    """Which expected downloads have actually landed, for every active call.

    The panel needs this to guard Done. Monitoring stops the moment Done is
    clicked, so anything still in flight at that point is never collected, and
    recovering it means copying files in by hand. Showing the checklist turns
    that trap into something the operator can simply see.

    Downloads, and only downloads. This used to list the main JSON as well, and
    under one-zip-out that is a file nobody will ever download: it comes out of
    the archive when the archive lands. Listing it told the operator to wait for
    a second file that was never coming, next to a checklist that could not
    complete until it silently appeared. Whether the archive actually contained
    it is a question for validation, which reports it as a missing file with its
    own name.
    """
    root = Path(root).resolve()
    progress: list[dict[str, Any]] = []
    for active in load_active_calls(root):
        exchange_id = str(active.get("exchange_id", ""))
        try:
            exchange_dir = _exchange_dir(root, exchange_id)
            manifest = _read_json_object(
                exchange_dir / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
            )
        except (FileNotFoundError, ValueError):
            continue
        response_dir = exchange_dir / "response"
        expected = [str(name) for name in manifest.get("expected_artifacts", [])]
        files = []
        for name in expected:
            if not name:
                continue
            path = response_dir / name
            arrived = path.is_file()
            files.append(
                {
                    "filename": name,
                    "arrived": arrived,
                    "size": path.stat().st_size if arrived else 0,
                    "is_main": False,
                }
            )
        progress.append(
            {
                "exchange_id": exchange_id,
                "subject": manifest.get("subject"),
                "project": manifest.get("project"),
                "request_id": manifest.get("request_id"),
                "started_at": active.get("started_at"),
                "tab_id": active.get("tab_id"),
                "monitoring": bool(active.get("monitoring")),
                "files": files,
                "received": sum(1 for item in files if item["arrived"]),
                "expected": len(files),
                "complete": bool(files) and all(item["arrived"] for item in files),
            }
        )
    progress.sort(key=lambda item: str(item["exchange_id"]))
    return progress


def inspect_call(root: Path, exchange_id: str) -> dict[str, Any]:
    """Everything the panel may know about one exchange, without its content.

    Recall is metadata: what the call was, what state it reached, what came
    back, where the files live. Deliberately no file bodies — the panel is not
    a viewer, and returned content stays inert on disk where "never execute
    what a call returns" can hold.

    Defects are computed only for an INCOMPLETE exchange, where they are the
    next action. A PREPARED call has no response to diagnose, and diagnosing
    one would report its absence as a defect; a COMPLETE one needs no repair.
    """
    root = Path(root).resolve()
    exchange = _exchange_dir(root, exchange_id)
    manifest = _read_json_object(
        exchange / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    state = str(manifest.get("state", ""))

    response_dir = exchange / "response"
    response_files = []
    if response_dir.is_dir():
        for path in sorted(response_dir.iterdir()):
            if path.is_file():
                response_files.append(
                    {"filename": path.name, "size": path.stat().st_size}
                )

    validation = None
    report_path = exchange / "validation" / "VALIDATION_REPORT.json"
    if report_path.is_file():
        try:
            validation = _read_json_object(report_path, "VALIDATION_REPORT")
        except ValueError:
            validation = None

    defects: list[dict[str, Any]] = []
    defects_omitted = 0
    if state == "INCOMPLETE":
        # Imported here because repair imports this module at load time.
        from companion.repair import collect_defects

        found = collect_defects(exchange)
        # A response declaring hundreds of artifacts can produce a defect for
        # each one, and the native-messaging frame has a hard one-megabyte
        # ceiling: overflowing it fails the whole panel command rather than
        # truncating. The first few are what anyone acts on, and `defects`
        # remains the complete account.
        defects = found[:_DEFECTS_IN_INSPECT]
        defects_omitted = max(0, len(found) - len(defects))

    active = load_active_call(root, exchange_id)
    main_path = response_dir / str(manifest.get("expected_main_json", ""))
    return {
        "exchange_id": manifest.get("exchange_id"),
        "subject": manifest.get("subject"),
        "project": manifest.get("project"),
        "request_id": manifest.get("request_id"),
        "state": state,
        "created_at": manifest.get("created_at"),
        "expected_main_json": manifest.get("expected_main_json"),
        "expected_artifacts": manifest.get("expected_artifacts", []),
        "attach_files": manifest.get("attach_files", []),
        "repair_round": int(manifest.get("repair_round", 0) or 0),
        "repairs": manifest.get("repairs", []),
        "response_files": response_files,
        "validation": validation,
        "defects": defects,
        "defects_omitted": defects_omitted,
        "download_failures": (
            list(active.get("download_failures", [])) if active else []
        ),
        "paths": {
            "exchange": str(exchange),
            "main_response": str(main_path) if main_path.is_file() else None,
            "validation_report": str(report_path) if report_path.is_file() else None,
        },
    }


def start_call(
    root: Path,
    exchange_id: str,
    tab_id: int,
    download_baseline: list[int],
) -> dict[str, Any]:
    root = Path(root).resolve()
    if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
        raise ValueError("tab_id must be a non-negative integer")
    if not isinstance(download_baseline, list) or any(
        not isinstance(item, int) or item < 0 for item in download_baseline
    ):
        raise ValueError("download_baseline must contain non-negative integers")

    exchange_dir = _exchange_dir(root, exchange_id)
    manifest_path = exchange_dir / "EXCHANGE_MANIFEST.json"
    manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
    if manifest.get("exchange_id") != exchange_id:
        raise ValueError("exchange manifest identity does not match its directory")
    if manifest.get("state") != "PREPARED":
        raise RuntimeError("call is not prepared")

    with state_lock(root):
        manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
        if manifest.get("state") != "PREPARED":
            raise RuntimeError("call is not prepared")
        request_paths(root, exchange_id)
        _assert_can_run_alongside(root, exchange_id, tab_id, manifest)
        active = {
            "exchange_id": exchange_id,
            "exchange_path": str(exchange_dir),
            "request_id": manifest["request_id"],
            "expected_main_json": manifest["expected_main_json"],
            "tab_id": tab_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "monitoring": True,
            "download_baseline": sorted(set(download_baseline)),
            "observed_download_ids": [],
            "collected_files": [],
        }
        active_path = _active_path_for(root, exchange_id)
        manifest["state"] = "ACTIVE"
        _write_json_atomic(active_path, active)
        try:
            _write_json_atomic(manifest_path, manifest)
        except BaseException:
            active_path.unlink(missing_ok=True)
            raise
    return active


def load_active_calls(root: Path) -> list[dict[str, Any]]:
    """Return every active call, oldest exchange first."""
    root = Path(root).resolve()
    _migrate_legacy_active(root)
    active_dir = _active_dir(root)
    if not active_dir.is_dir():
        return []
    records = [
        _read_json_object(path, "ACTIVE_CALL")
        for path in sorted(active_dir.glob("*.json"))
    ]
    return sorted(records, key=lambda item: str(item.get("exchange_id", "")))


def load_active_call(
    root: Path, exchange_id: str | None = None
) -> dict[str, Any] | None:
    """Return one active call.

    With no exchange_id this returns the single active call when exactly one is
    running, which keeps single-call callers and the manual fallback simple. It
    refuses to guess when several calls are running.
    """
    records = load_active_calls(root)
    if exchange_id is not None:
        for record in records:
            if record.get("exchange_id") == exchange_id:
                return record
        return None
    if not records:
        return None
    if len(records) > 1:
        raise RuntimeError(
            "several calls are active; name one with --exchange: "
            + ", ".join(str(record.get("exchange_id")) for record in records)
        )
    return records[0]


def resume_call(
    root: Path,
    tab_id: int,
    download_baseline: list[int],
    exchange_id: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
        raise ValueError("tab_id must be a non-negative integer")
    if not isinstance(download_baseline, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in download_baseline
    ):
        raise ValueError("download_baseline must contain non-negative integers")
    with state_lock(root):
        active = load_active_call(root, exchange_id)
        if active is None:
            raise RuntimeError("no call is active")
        request_paths(root, active["exchange_id"])
        for other in load_active_calls(root):
            if (
                other.get("exchange_id") != active["exchange_id"]
                and other.get("tab_id") == tab_id
            ):
                raise RuntimeError(
                    f"tab {tab_id} is already bound to call {other['exchange_id']}"
                )
        active["tab_id"] = tab_id
        active["started_at"] = datetime.now(timezone.utc).isoformat()
        active["monitoring"] = True
        active["download_baseline"] = sorted(set(download_baseline))
        _write_json_atomic(_active_path_for(root, active["exchange_id"]), active)
    return active


def stop_call(root: Path, exchange_id: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    with state_lock(root):
        active = load_active_call(root, exchange_id)
        if active is None:
            raise RuntimeError("no call is active")
        manifest_path = (
            _exchange_dir(root, active["exchange_id"]) / "EXCHANGE_MANIFEST.json"
        )
        manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
        manifest["state"] = "STOPPED"
        _write_json_atomic(manifest_path, manifest)
        _active_path_for(root, active["exchange_id"]).unlink(missing_ok=True)
    return manifest


def delete_call(root: Path, exchange_id: str, force: bool = False) -> dict[str, Any]:
    """Remove an exchange from disk, freeing the deliverable names it claimed.

    A superseded call is not harmless. It stays PREPARED forever, so it keeps
    claiming its expected filenames and the next call that wants them is refused,
    and it sits in the panel inviting someone to send the wrong payload. Stopping
    is the wrong instrument: STOPPED still leaves the directory and its request
    bundle behind.

    Deleting is refused while the call is running, because the download monitor
    would keep writing into a directory that no longer exists. It is also refused
    once a response has landed, since that file is the only copy of work the model
    already did; force overrides that and only that.
    """
    root = Path(root).resolve()
    with state_lock(root):
        exchange_dir = _exchange_dir(root, exchange_id)
        manifest = _read_json_object(
            exchange_dir / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
        )
        if any(record.get("exchange_id") == exchange_id for record in load_active_calls(root)):
            raise RuntimeError(
                f"call {exchange_id} is running. Stop it first, then delete it."
            )
        received = sorted(
            path.name
            for path in (exchange_dir / "response").glob("*")
            if path.is_file()
        )
        if received and not force:
            raise RuntimeError(
                f"call {exchange_id} has already received {', '.join(received)}. "
                "Deleting discards the only copy of that response. Move the files "
                "elsewhere first, or pass --force to discard them."
            )
        freed = [
            name
            for name in [
                str(manifest.get("expected_main_json", "")),
                *[str(item) for item in manifest.get("expected_artifacts", [])],
            ]
            if name
        ]
        shutil.rmtree(exchange_dir)
        _active_path_for(root, exchange_id).unlink(missing_ok=True)
    return {
        "exchange_id": exchange_id,
        "subject": manifest.get("subject"),
        "state_before_delete": manifest.get("state"),
        "deleted": True,
        "discarded_responses": received,
        "freed_deliverable_names": freed,
    }


def _assert_can_run_alongside(
    root: Path, exchange_id: str, tab_id: int, manifest: dict[str, Any]
) -> None:
    """Refuse a new call that another active call could be confused with.

    Downloads are routed by filename, so two concurrent calls must never expect
    the same main JSON name. Tabs are the attachment binding, so one tab may
    drive only one call.
    """
    for other in load_active_calls(root):
        if other.get("exchange_id") == exchange_id:
            raise RuntimeError("this call is already active")
        if other.get("tab_id") == tab_id:
            raise RuntimeError(
                f"tab {tab_id} is already bound to call {other['exchange_id']}"
            )

    mine = [
        str(manifest["expected_main_json"]),
        *[str(item) for item in manifest.get("expected_artifacts", [])],
    ]
    claimed = claimed_deliverable_names(root, exclude_exchange_id=exchange_id)
    running = {record["exchange_id"] for record in load_active_calls(root)}
    for name in mine:
        owner = claimed.get(name.casefold())
        if owner in running:
            raise RuntimeError(
                f"call {owner} is already running and expects the filename {name}. "
                "Downloads are routed by filename, so concurrent calls need "
                "distinct expected_main_json and artifact names."
            )


def _active_dir(root: Path) -> Path:
    return root / "state" / "active"


def _active_path_for(root: Path, exchange_id: str) -> Path:
    if not re.fullmatch(r"[0-9A-Za-z_-]+", exchange_id):
        raise ValueError("exchange_id is unsafe")
    return _active_dir(root) / f"{exchange_id}.json"


def _migrate_legacy_active(root: Path) -> None:
    """Move a single-call ACTIVE_CALL.json into the multi-call directory."""
    legacy = _active_path(root)
    if not legacy.is_file():
        return
    record = _read_json_object(legacy, "ACTIVE_CALL")
    exchange_id = str(record.get("exchange_id", ""))
    if not exchange_id:
        legacy.unlink(missing_ok=True)
        return
    target = _active_path_for(root, exchange_id)
    if not target.exists():
        _write_json_atomic(target, record)
    legacy.unlink(missing_ok=True)


def request_paths(root: Path, exchange_id: str) -> list[str]:
    exchange_dir = _exchange_dir(Path(root).resolve(), exchange_id)
    manifest = _read_json_object(
        exchange_dir / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    records = {
        _safe_name(str(item.get("filename", "")), "request filename"): item
        for item in manifest.get("request_files", [])
    }
    # Only the prompt and the archive are uploaded. Exchanges prepared before
    # bundling existed have no attach list and upload every request file.
    attach = manifest.get("attach_files") or list(records)
    paths: list[str] = []
    for name in attach:
        name = _safe_name(str(name), "request filename")
        item = records.get(name)
        if item is None:
            raise ValueError(f"attach file is not a request file: {name}")
        path = (exchange_dir / "request" / name).resolve(strict=True)
        if path.parent != (exchange_dir / "request").resolve():
            raise ValueError("request file escapes the exchange")
        digest, size = _sha256(path)
        if digest != item.get("sha256") or size != item.get("size"):
            raise ValueError(f"request file changed after preparation: {name}")
        paths.append(str(path))
    return paths


def launch_prompt(root: Path, exchange_id: str) -> str:
    """The line the operator sends with the archive.

    One zip goes up and the prompt is inside it, so the message ChatGPT receives
    carries no instructions of its own. Sent bare, an archive gets a model
    asking what to do with it, and the call stalls before it has begun. This is
    the smallest text that turns the attachment into an instruction: open it,
    read the governing file first, follow it.

    It is deliberately about routing and nothing else. Anything describing the
    work belongs in the prompt inside the archive, where it is hashed with the
    rest of the request instead of typed into a composer.

    That is why this line does not dictate the shape of the reply. It used to
    end "do not reply with anything except the downloads it asks for", which is
    not routing but a rule about the work, and it silently broke any call whose
    prompt wanted the model to say something as well - a call standing up a
    conversational role, for one. The archive decides what comes back.
    """
    exchange_dir = _exchange_dir(Path(root).resolve(), exchange_id)
    manifest = _read_json_object(
        exchange_dir / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    attach = manifest.get("attach_files") or []
    if len(attach) != 1:
        raise ValueError("exactly one file is uploaded, so exactly one is named")
    archive = _safe_name(str(attach[0]), "attached archive name")
    return (
        f"Open the attached archive {archive} now. Read {PROMPT_IN_BUNDLE_NAME} "
        "inside it first, then follow it exactly. Every file you need is in the "
        "archive; do not ask what to do with it, and do not stop to summarise it "
        "back. That file says what to do and what to reply with."
    )


def _subject_slug(subject: str) -> str:
    if not subject or "/" in subject or "\\" in subject or subject in {".", ".."}:
        raise ValueError("subject is empty or unsafe")
    slug = re.sub(r"[^a-z0-9]+", "_", subject.casefold()).strip("_")
    if not slug or len(slug) > 80:
        raise ValueError("subject cannot form a safe exchange name")
    return slug


def _safe_name(name: str, label: str) -> str:
    name = unicodedata.normalize("NFC", name)
    if (
        not name
        or name in {".", ".."}
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or name.endswith((".", " "))
        or any(ord(character) < 32 for character in name)
        or any(character in '<>:"|?*' for character in name)
        or len(name.encode("utf-8")) > 255
    ):
        raise ValueError(f"{label} is unsafe: {name!r}")
    device = name.rstrip(". ").split(".", 1)[0].upper()
    if device in _RESERVED_NAMES:
        raise ValueError(f"{label} uses a reserved Windows name: {name}")
    return name


def _check_export_source(root: Path, source: Path, *, reexport=False) -> None:
    forbidden = [root / "state", root / ".git"]
    if not reexport:
        forbidden.append(root / "calls")
    if any(source.is_relative_to(path.resolve()) for path in forbidden):
        raise ValueError("export a sanitized approved source outside internal call/state/Git storage")
    if any(part in {".ssh", ".gnupg", ".git", ".aws", "gcloud"} for part in source.parts):
        raise ValueError("credential or Git storage cannot be packaged")
    if (source.name == ".env" or source.name.startswith(".env.") or
            source.name in {"id_rsa", "id_ed25519", "credentials", "application_default_credentials.json"} or
            source.suffix.casefold() in {".pem", ".key"}):
        raise ValueError("credential files cannot be packaged")


def _exchange_dir(root: Path, exchange_id: str) -> Path:
    if not re.fullmatch(r"[0-9A-Za-z_-]+", exchange_id):
        raise ValueError("exchange_id is unsafe")
    calls_dir = (root / "calls").resolve()
    exchange = (calls_dir / exchange_id).resolve()
    if exchange.parent != calls_dir or not exchange.is_dir():
        raise FileNotFoundError(f"exchange does not exist: {exchange_id}")
    return exchange


def _active_path(root: Path) -> Path:
    return root / "state" / "ACTIVE_CALL.json"


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    # utf-8-sig tolerates a byte-order mark. PowerShell writes one by default and
    # ChatGPT sometimes emits one, and rejecting those files taught nobody
    # anything. Hashes are still computed over the raw bytes on disk.
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_bytes_durable(path: Path, contents: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _copy_verified(source: Path, destination: Path) -> None:
    before_digest, before_size = _sha256(source)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        _copy_stream(incoming, outgoing)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    after_digest, after_size = _sha256(source)
    copied_digest, copied_size = _sha256(destination)
    if (
        before_digest != after_digest
        or before_size != after_size
        or before_digest != copied_digest
        or before_size != copied_size
    ):
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"input changed while it was copied: {source.name}")


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ValueError(f"file exceeds byte budget: {path.name}")
    return digest.hexdigest(), size


def _copy_stream(incoming, outgoing) -> None:
    total = 0
    while chunk := incoming.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise ValueError("file exceeds copy byte budget")
        outgoing.write(chunk)


def _file_record(path: Path) -> dict[str, Any]:
    digest, size = _sha256(path)
    return {"filename": path.name, "size": size, "sha256": digest}


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
