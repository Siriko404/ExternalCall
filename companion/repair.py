"""Corrections use the shared validator. Version 2 creates a fresh attempt with
fresh routing names and frozen job evidence; legacy calls retain correction
rounds in the same exchange, preserving superseded downloads.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from companion.core import (
    _active_path_for,
    claimed_deliverable_names,
    _exchange_dir,
    _read_json_object,
    _safe_name,
    _sha256,
    _write_json_atomic,
    load_active_call,
    load_active_calls,
)
from companion.downloads import archive_member_index
from companion.lock import state_lock

REPAIRABLE_STATES = {"ACTIVE", "INCOMPLETE", "COMPLETE"}


def collect_defects(exchange_dir: Path) -> list[dict[str, Any]]:
    from companion.validation import validate_response
    return validate_response(exchange_dir)["defects"]


def build_repair_prompt(
    exchange_dir: Path,
    defects: list[dict[str, Any]],
    round_number: int,
) -> str:
    """Render the correction instruction for the current round."""
    exchange = Path(exchange_dir).resolve()
    manifest = _read_json_object(
        exchange / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST"
    )
    expected_main = str(manifest["expected_main_json"])
    request_id = str(manifest["request_id"])

    lines = [
        f"CORRECTION ROUND {round_number} for request_id {request_id}.",
        "",
        "Your previous delivery did not pass deterministic local validation.",
        "Do not apologize, summarize, or restate the assignment. Fix exactly the",
        "problems listed below and deliver corrected downloadable files.",
        "",
        "PROBLEMS FOUND",
    ]
    for index, defect in enumerate(defects, 1):
        lines.append(f"{index}. [{defect['kind']}] {defect['target']}")
        lines.append(f"   expected: {defect['expected']}")
        lines.append(f"   observed: {defect['observed']}")
    lines.extend(
        [
            "",
            "HOW TO FIX",
            "- Regenerate only the files named above. Every other delivered file must",
            "  stay byte-identical; do not rewrite work that already validated.",
            "- Write each file first, then compute its SHA-256 and byte size from the",
            "  bytes you actually wrote, then emit the main JSON last using those",
            "  measured values. Declaring a digest computed from an earlier draft is",
            "  the most common cause of this failure.",
            f'- Keep request_id exactly "{request_id}".',
            "- artifacts_manifest must list every created file once, with filename,",
            "  status CREATED, media_type, exact size in bytes, and exact sha256.",
            "  A file inside the outputs archive belongs in this list too, with its",
            "  own hash.",
            f"- Put {expected_main} inside the expected outputs ZIP.",
            "- delivery must list only the outputs ZIP. Do not list its members.",
            "- If a file genuinely cannot be produced, set its status to NOT_CREATED,",
            "  leave it out of delivery, set the top-level status to PARTIAL, and give",
            "  the reason in limitations.",
            "",
            "DELIVERY",
            "- Preserve valid artifact bytes; regenerate the envelope and ZIP as needed.",
            "- Never hash the main JSON inside itself or the outer ZIP inside its own main JSON.",
            "- Return no conversational text. Deliver exactly one corrected outputs ZIP.",
        ]
    )
    return "\n".join(lines) + "\n"


def open_repair_round(root, exchange_id, tab_id=None, download_baseline=None):
    exchange = _exchange_dir(Path(root).resolve(), exchange_id)
    manifest = _read_json_object(exchange / "EXCHANGE_MANIFEST.json", "EXCHANGE_MANIFEST")
    if manifest.get("contract_version", 1) >= 2:
        if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
            raise ValueError("tab_id must be a non-negative integer")
        baseline = download_baseline or []
        if not isinstance(baseline, list) or any(not isinstance(x, int) or isinstance(x, bool) or x < 0 for x in baseline):
            raise ValueError("download_baseline must contain non-negative integers")
        if load_active_call(root, exchange_id) is not None:
            raise RuntimeError("finish the current attempt before preparing its correction")
        defects = collect_defects(exchange)
        if not defects:
            raise RuntimeError("the delivered response has no validation defects")
        from companion.jobs import reprepare_job
        from companion.core import launch_prompt, request_paths, start_call
        round_number = int(manifest.get("repair_round", 0)) + 1
        prepared = reprepare_job(Path(root).resolve(), exchange, datetime.now(timezone.utc),
                                 repair_context={"previous_exchange": exchange_id, "round": round_number, "defects": defects})
        new_id = prepared["exchange_id"]
        paths = request_paths(root, new_id)
        active = start_call(root, new_id, tab_id, baseline)
        return {"exchange_id": new_id, "round": round_number,
                "new_attempt": True, "defects": defects, "active": active,
                "request_paths": paths,
                "prompt": launch_prompt(root, new_id), "prompt_path": None, "defects_path": None}
    # All legacy ownership checks and writes share the same critical section.
    with state_lock(root):
        return _open_repair_round(root, exchange_id, tab_id, download_baseline)


def _open_repair_round(
    root: Path,
    exchange_id: str,
    tab_id: int | None = None,
    download_baseline: list[int] | None = None,
) -> dict[str, Any]:
    """Record a correction round and re-arm monitoring for the same exchange."""
    root = Path(root).resolve()
    exchange = _exchange_dir(root, exchange_id)
    manifest_path = exchange / "EXCHANGE_MANIFEST.json"
    manifest = _read_json_object(manifest_path, "EXCHANGE_MANIFEST")
    state = manifest.get("state")
    if state not in REPAIRABLE_STATES:
        raise RuntimeError(f"exchange cannot be repaired from state {state}")

    active = load_active_call(root, exchange_id)
    if tab_id is not None:
        for other in load_active_calls(root):
            if other.get("exchange_id") != exchange_id and other.get("tab_id") == tab_id:
                raise RuntimeError(
                    f"tab {tab_id} is already bound to call {other['exchange_id']}"
                )

    # Reopening a finished exchange re-claims its deliverable names, and a
    # finished exchange has already released them. If another call took them in
    # the meantime, two calls able to receive files would expect the same
    # filename, which is the one thing download routing cannot survive. Checked
    # before anything is written, so a refusal leaves no orphan round behind.
    claimed = claimed_deliverable_names(root, exclude_exchange_id=exchange_id)
    for name in [
        str(manifest["expected_main_json"]),
        *[str(item) for item in manifest.get("expected_artifacts", [])],
    ]:
        owner = claimed.get(name.casefold())
        if owner:
            raise RuntimeError(
                f"call {owner} now expects the filename {name}, which this "
                "exchange released when it finished. Reopening it would leave two "
                "calls expecting the same download. Finish or delete that call "
                "first, or raise a new correction call with its own filenames."
            )

    defects = collect_defects(exchange)
    if not defects:
        raise RuntimeError("the delivered response has no validation defects")

    resolved_tab = tab_id if tab_id is not None else active.get("tab_id") if active else None
    if resolved_tab is None:
        raise ValueError("tab_id is required when no call is active")
    if not isinstance(resolved_tab, int) or isinstance(resolved_tab, bool) or resolved_tab < 0:
        raise ValueError("tab_id must be a non-negative integer")
    baseline = sorted(set(download_baseline or []))
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in baseline):
        raise ValueError("download_baseline must contain non-negative integers")

    round_number = int(manifest.get("repair_round", 0)) + 1
    prompt = build_repair_prompt(exchange, defects, round_number)
    repair_dir = exchange / "repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = repair_dir / f"ROUND_{round_number}_PROMPT.txt"
    defects_path = repair_dir / f"ROUND_{round_number}_DEFECTS.json"
    opened_at = datetime.now(timezone.utc).isoformat()
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
    _write_json_atomic(
        defects_path,
        {
            "schema_version": 1,
            "exchange_id": exchange_id,
            "request_id": manifest["request_id"],
            "round": round_number,
            "opened_at": opened_at,
            "defects": defects,
        },
    )

    reopened = {
        "exchange_id": exchange_id,
        "exchange_path": str(exchange),
        "request_id": manifest["request_id"],
        "expected_main_json": manifest["expected_main_json"],
        "tab_id": resolved_tab,
        "started_at": opened_at,
        "monitoring": True,
        "repair_round": round_number,
        "download_baseline": baseline,
        "observed_download_ids": [],
        "collected_files": list(active.get("collected_files", [])) if active else [],
    }
    manifest["state"] = "ACTIVE"
    manifest["repair_round"] = round_number
    manifest.setdefault("repairs", []).append(
        {
            "round": round_number,
            "opened_at": opened_at,
            "state_before": state,
            "defect_kinds": sorted({defect["kind"] for defect in defects}),
            "prompt_file": prompt_path.name,
            "defects_file": defects_path.name,
        }
    )
    active_path = _active_path_for(root, exchange_id)
    _write_json_atomic(active_path, reopened)
    try:
        _write_json_atomic(manifest_path, manifest)
    except BaseException:
        active_path.unlink(missing_ok=True)
        raise
    return {
        "exchange_id": exchange_id,
        "round": round_number,
        "defects": defects,
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "defects_path": str(defects_path),
        "active": reopened,
    }
