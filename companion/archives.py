"""Bounded archive inspection. Returned files are never executed or extracted."""
from __future__ import annotations

import hashlib
import io
import stat
import time
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

from companion.core import _safe_name

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_NESTED_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_SECONDS = 30
MAX_MAIN_BYTES = 2 * 1024 * 1024


def safe_path(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise ValueError(f"unsafe artifact path: {name!r}")
    normalized = unicodedata.normalize("NFC", name)
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"unsafe artifact path: {name!r}")
    for part in parts:
        _safe_name(part, "artifact path component")
    return str(PurePosixPath(*parts))


def inspect_zip(path: Path, *, nested: bool = False) -> dict[str, tuple[str, int]]:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds compressed-byte budget")
    index: dict[str, tuple[str, int]] = {}
    budget = {"bytes": 0, "members": 0, "deadline": time.monotonic() + MAX_SECONDS}

    def inspect(archive, prefix="", depth=0):
        local_seen = set()
        for entry in archive.infolist():
            budget["members"] += 1
            if budget["members"] > MAX_MEMBERS:
                raise ValueError("archive exceeds member-count budget")
            name = safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
            folded = name.casefold()
            if folded in local_seen:
                raise ValueError(f"duplicate normalized archive path: {name}")
            local_seen.add(folded)
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(f"unsupported archive member type: {name}")
            if entry.flag_bits & 1:
                raise ValueError(f"encrypted archive member: {name}")
            if entry.is_dir():
                continue
            if entry.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"archive member exceeds size budget: {name}")
            digest, size = hashlib.sha256(), 0
            child = io.BytesIO() if nested and name.casefold().endswith(".zip") and depth < 3 else None
            with archive.open(entry) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    budget["bytes"] += len(chunk)
                    if size > MAX_MEMBER_BYTES or budget["bytes"] > MAX_EXPANDED_BYTES:
                        raise ValueError("archive exceeds expanded-byte budget")
                    if time.monotonic() > budget["deadline"]:
                        raise ValueError("archive exceeds processing-time budget")
                    digest.update(chunk)
                    if child is not None:
                        if size > MAX_NESTED_BYTES:
                            raise ValueError("nested archive exceeds inspection budget")
                        child.write(chunk)
            if size != entry.file_size:
                raise ValueError(f"archive member size mismatch: {name}")
            key = (prefix + name).casefold()
            if key in index:
                raise ValueError(f"duplicate archive identity: {name}")
            index[key] = (digest.hexdigest(), size)
            if child is not None:
                child.seek(0)
                with zipfile.ZipFile(child) as inner:
                    inspect(inner, prefix + name + "!/", depth + 1)

    with zipfile.ZipFile(path) as archive:
        inspect(archive)
    return index


def member_index(response_dir: Path, *, nested: bool = True, aliases: bool = True):
    index = {}
    basename_keys = {}
    for path in sorted(response_dir.glob("*.zip")):
        for key, value in inspect_zip(path, nested=nested).items():
            if key in index:
                raise ValueError(f"duplicate artifact identity across archives: {key}")
            index[key] = value
            basename = key.rsplit("/", 1)[-1]
            basename_keys.setdefault(basename, []).append(key)
    if aliases:
        for name, keys in basename_keys.items():
            if len(keys) != 1:
                raise ValueError(f"ambiguous legacy artifact basename: {name}")
            index.setdefault(name, index[keys[0]])
    return index
