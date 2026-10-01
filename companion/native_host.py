from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from companion.core import (
    _exchange_dir,
    _read_json_object,
    clone_call,
    inspect_call,
    list_ready_calls,
    load_active_calls,
    call_progress,
    launch_prompt,
    list_recent_calls,
    request_paths,
    resume_call,
    start_call,
    stop_call,
)
from companion.downloads import (
    default_downloads_dir,
    finish_call,
    handle_completed_download,
)
from companion.repair import open_repair_round
from companion.watch import record_download_failure
from companion.lock import state_lock


MAX_MESSAGE_SIZE = 1024 * 1024
ALLOWED_COMMANDS = {
    "status.snapshot",
    "health",
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
    "call.repair",
    "call.stop",
}


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = stream.read(4)
    if header == b"":
        return None
    if len(header) != 4:
        raise ValueError("native message header is truncated")
    length = struct.unpack("<I", header)[0]
    if length == 0:
        raise ValueError("native message body is empty")
    if length > MAX_MESSAGE_SIZE:
        raise ValueError("native message is too large")
    body = stream.read(length)
    if len(body) != length:
        raise ValueError("native message body is truncated")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("native message body is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("native message body must be a JSON object")
    return value


def write_message(stream: BinaryIO, value: dict[str, Any]) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if not body or len(body) > MAX_MESSAGE_SIZE:
        raise ValueError("native response is empty or too large")
    stream.write(struct.pack("<I", len(body)))
    stream.write(body)
    stream.flush()


def dispatch(root: Path, message: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
    if message.get("protocol_version") != 1:
        raise ValueError("protocol_version must be 1")
    command = message.get("command")
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"unknown command: {command}")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    root = Path(root).resolve()

    if command == "health":
        _require_keys(payload, set())
        return {"status": "OK", "protocol_version": 1, "root": str(root)}
    if command == "calls.list_ready":
        _require_keys(payload, set())
        return list_ready_calls(root)
    if command == "call.active":
        _require_keys(payload, set())
        records = load_active_calls(root)
        return records[0] if len(records) == 1 else None
    if command == "calls.active":
        _require_keys(payload, set())
        return load_active_calls(root)
    if command == "status.snapshot":
        _require_keys(payload, set())
        with state_lock(root):
            return {"root": str(root), "ready": list_ready_calls(root),
                    "active": load_active_calls(root), "progress": call_progress(root),
                    "recent": list_recent_calls(root)}
    if command == "calls.progress":
        _require_keys(payload, set())
        return call_progress(root)
    if command == "calls.recent":
        _require_keys(payload, set())
        return list_recent_calls(root)
    if command == "call.inspect":
        # Read-only recall for one exchange: metadata, report, defects, paths.
        # Never file contents — the panel is not a viewer.
        _require_keys(payload, {"exchange_id"})
        return inspect_call(root, _required_string(payload, "exchange_id"))
    if command == "call.clone":
        # Send the same request again as a new exchange. The finished one is
        # left exactly as it is: its response is the only copy of that work.
        _require_keys(payload, {"exchange_id"})
        return clone_call(
            root,
            _required_string(payload, "exchange_id"),
            datetime.now(timezone.utc),
        )
    if command == "download.failure.record":
        # The one write the panel side may ask for beyond the call lifecycle:
        # a bounded scalar event saying a completed download could not be
        # filed. The companion stamps the time and does the attribution; the
        # extension supplies only what Chrome told it.
        _allow_keys(payload, {"message"}, {"download_id", "filename"})
        return record_download_failure(root, payload)
    if command == "call.go":
        _require_keys(payload, {"exchange_id", "tab_id", "download_baseline"})
        exchange_id = _required_string(payload, "exchange_id")
        paths = request_paths(root, exchange_id)
        launch = launch_prompt(root, exchange_id)
        active = start_call(
            root,
            exchange_id,
            _required_integer(payload, "tab_id"),
            _integer_list(payload, "download_baseline"),
        )
        return {
            "active": active,
            "request_paths": paths,
            # An archive sent with no message gets a model asking what to do
            # with it. The panel types this line into the composer; the operator
            # still reviews it and clicks Send.
            "launch_prompt": launch,
        }
    if command == "call.resume":
        _allow_keys(payload, {"tab_id", "download_baseline"}, {"exchange_id"})
        active = resume_call(
            root,
            _required_integer(payload, "tab_id"),
            _integer_list(payload, "download_baseline"),
            _optional_string(payload, "exchange_id"),
        )
        return {
            "active": active,
            "request_paths": request_paths(root, active["exchange_id"]),
            "launch_prompt": launch_prompt(root, active["exchange_id"]),
        }
    if command == "download.completed":
        allowed = {
            "id",
            "filename",
            "state",
            "url",
            "finalUrl",
            "mime",
            "startTime",
            "endTime",
        }
        if not set(payload).issubset(allowed):
            raise ValueError("download payload contains unknown fields")
        _required_integer(payload, "id")
        _required_string(payload, "filename")
        return handle_completed_download(root, payload)
    if command == "call.done":
        _allow_keys(payload, set(), {"exchange_id"})
        exchange_id = _optional_string(payload, "exchange_id")
        if any(
            record.get("exchange_id") == exchange_id or exchange_id is None
            for record in load_active_calls(root)
        ):
            # The side panel's Done arrives here, not through the CLI, so it has
            # to ingest from the downloads folder too. Without this the capture
            # fix would only ever run for someone typing the command by hand.
            return finish_call(root, exchange_id, default_downloads_dir())
        if exchange_id is None:
            raise RuntimeError("name the finished exchange to retrieve its result")
        report_path = _exchange_dir(root, exchange_id) / "validation" / "VALIDATION_REPORT.json"
        report = _read_json_object(report_path, "validation report")
        if report.get("exchange_id") != exchange_id:
            raise ValueError("validation report belongs to another exchange")
        return report
    if command == "call.repair":
        _require_keys(payload, {"exchange_id", "tab_id", "download_baseline"})
        return open_repair_round(
            root,
            _required_string(payload, "exchange_id"),
            _required_integer(payload, "tab_id"),
            _integer_list(payload, "download_baseline"),
        )
    if command == "call.stop":
        _allow_keys(payload, set(), {"exchange_id"})
        return stop_call(root, _optional_string(payload, "exchange_id"))
    raise AssertionError(f"unhandled command: {command}")


def serve(root: Path, incoming: BinaryIO, outgoing: BinaryIO) -> None:
    while True:
        try:
            message = read_message(incoming)
            if message is None:
                return
            command = str(message.get("command", "unknown"))
            result = dispatch(root, message)
            response = {"ok": True, "command": command, "result": result}
        except Exception as error:
            response = {
                "ok": False,
                "command": locals().get("command", "unknown"),
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        write_message(outgoing, response)


def _require_keys(payload: dict[str, Any], required: set[str]) -> None:
    keys = set(payload)
    if keys != required:
        missing = sorted(required - keys)
        extra = sorted(keys - required)
        raise ValueError(f"payload fields do not match; missing={missing} extra={extra}")


def _allow_keys(
    payload: dict[str, Any], required: set[str], optional: set[str]
) -> None:
    keys = set(payload)
    missing = sorted(required - keys)
    extra = sorted(keys - required - optional)
    if missing or extra:
        raise ValueError(f"payload fields do not match; missing={missing} extra={extra}")


def _optional_string(payload: dict[str, Any], name: str) -> str | None:
    if name not in payload:
        return None
    return _required_string(payload, name)


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _required_integer(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _integer_list(payload: dict[str, Any], name: str) -> list[int]:
    value = payload.get(name)
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0
        for item in value
    ):
        raise ValueError(f"{name} must contain non-negative integers")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    options, chrome_arguments = parser.parse_known_args(argv)
    if chrome_arguments:
        origin = chrome_arguments[0]
        if not origin.startswith("chrome-extension://"):
            raise ValueError("native host caller origin is invalid")
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    serve(Path(options.root), sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
