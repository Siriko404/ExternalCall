"""Work out the Chrome extension ID for this checkout's `extension\\` directory.

The host manifest must pin one exact `chrome-extension://<id>/` origin, and that
id used to be copied by hand off `chrome://extensions`. Thirty-two characters
transcribed by a person is the step an install fails on, and it forced an order
on the install too: the extension had to be loaded before the host could be
registered, because there was no id until then.

Neither is necessary. Chrome derives an unpacked extension's id from where it
sits on disk, so the id exists before Chrome has ever seen the directory, and
once Chrome has seen it the id is recorded in that profile's own preferences.

Three sources, most authoritative first:

1. An id the caller supplies. Nothing overrides an explicit instruction.
2. Chrome's own state: the profile that has this exact directory loaded knows
   its id as a fact rather than a derivation.
3. The derivation: SHA-256 of the absolute path encoded UTF-16LE, first sixteen
   bytes, each hex digit mapped onto a..p. Verified against a real installed
   extension, and it is what lets the host be registered first.

Windows and Linux, like the rest of the installer. Chrome encodes the path
UTF-16LE on Windows and UTF-8 on Linux.

On Linux the profile data lives under ~/.config/<browser>/ instead of
%LOCALAPPDATA%\\Google\\Chrome\\User Data. Every browser directory that
exists is searched, overridable with EXTERNALCALL_BROWSER_DATA.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

ID_PATTERN = "abcdefghijklmnop"

LINUX_USER_DATA_DIRS = (
    Path.home() / ".config" / "google-chrome",
    Path.home() / ".config" / "chromium",
    Path.home() / ".config" / "BraveSoftware" / "Brave-Browser",
)


def derive_extension_id(directory: Path) -> str:
    """The id Chrome will give an unpacked extension loaded from `directory`."""
    path = Path(directory)
    # A path that is already absolute on the running platform is used verbatim.
    # Resolving a foreign platform's absolute form — a Windows path read on
    # Linux, or the reverse — would rewrite it into a local path and hash
    # something Chrome would never see.
    absolute = str(path if path.is_absolute() else path.resolve())
    # Chrome hashes the path as UTF-16LE on Windows and UTF-8 on Linux.
    # Verified live on 2026-09-07: Chromium 151 on Arch Linux loaded
    # /home/sina/Work/ExternalCall/extension with the id the UTF-8 hash gives,
    # and only the install-time repin (setup.py comparing against Chrome's own
    # record) is what kept the earlier UTF-16LE assumption from pinning a dead
    # origin.
    encoding = "utf-16-le" if os.name == "nt" else "utf-8"
    digest = hashlib.sha256(absolute.encode(encoding)).hexdigest()[:32]
    return "".join(ID_PATTERN[int(char, 16)] for char in digest)


def chrome_user_data() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    override = os.environ.get("EXTERNALCALL_BROWSER_DATA")
    if override:
        return Path(override)
    for directory in LINUX_USER_DATA_DIRS:
        if directory.is_dir():
            return directory
    # Nothing is installed yet. chrome_user_data() is also the answer to "where
    # will the profile appear", so return the preferred location rather than a
    # missing path that would read as "not installed" forever.
    return LINUX_USER_DATA_DIRS[0]


def user_data_dirs(explicit: Path | None = None) -> list[Path]:
    """Every browser profile root that could hold the extension.

    An explicit root wins, and on Windows there is only ever one. On Linux an
    operator may have more than one Chromium-family browser, and a leftover
    directory from a browser that is barely used must not hide the profile the
    extension was actually loaded into — so every root that exists is searched.
    """
    if explicit is not None:
        return [explicit]
    if os.name == "nt" or os.environ.get("EXTERNALCALL_BROWSER_DATA"):
        return [chrome_user_data()]
    return [directory for directory in LINUX_USER_DATA_DIRS if directory.is_dir()]


def loaded_extension_id(directory: Path, user_data: Path | None = None) -> str | None:
    """The id Chrome has actually recorded for `directory`, or None.

    Every profile is searched, because the operator may not be using Default and
    an extension loaded in any of them still gets messages from this host. Both
    preference files are read: an unpacked extension normally lands in `Secure
    Preferences`, but older profiles carry it in `Preferences`.
    """
    target = str(Path(directory).resolve()).casefold()
    for base in user_data_dirs(user_data):
        if not base.is_dir():
            continue
        for profile in sorted(base.iterdir()):
            if not profile.is_dir():
                continue
            for name in ("Secure Preferences", "Preferences"):
                path = profile / name
                if not path.is_file():
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                settings = data.get("extensions", {})
                settings = settings.get("settings", {}) if isinstance(settings, dict) else {}
                if not isinstance(settings, dict):
                    continue
                for extension_id, value in settings.items():
                    if not isinstance(value, dict):
                        continue
                    where = value.get("path")
                    if isinstance(where, str) and where.casefold() == target:
                        return str(extension_id)
    return None


def wait_for_extension(
    directory: Path, timeout: float = 300.0, poll: float = 2.0
) -> str | None:
    """Block until Chrome reports this directory as a loaded extension.

    Loading an unpacked extension is the one step of the install a machine
    cannot perform. Chrome removed `--load-extension` from stable, and writing
    into a profile's preferences by hand defeats the HMAC that protects them, so
    a person clicks Load unpacked. Verified against Chrome 151: launching with
    `--load-extension` on a fresh profile leaves the extension absent.

    Waiting is not the same as assuming. When this returns an id, Chrome has
    recorded the directory itself, which is the difference between an installer
    that says "now go and click" and one that knows whether the click happened.
    """
    deadline = time.monotonic() + timeout
    while True:
        found = loaded_extension_id(directory)
        if found:
            return found
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def resolve(directory: Path, explicit: str | None = None) -> dict[str, str]:
    """The id to pin, and where it came from."""
    directory = Path(directory)
    derived = derive_extension_id(directory)
    if explicit:
        return {"id": explicit, "source": "given", "derived": derived}
    loaded = loaded_extension_id(directory)
    if loaded:
        return {"id": loaded, "source": "chrome", "derived": derived}
    return {"id": derived, "source": "derived", "derived": derived}


def main(argv: list[str]) -> int:
    directory = Path(argv[0]) if argv else Path(__file__).resolve().parents[1] / "extension"
    if not (directory / "manifest.json").is_file():
        print(f"not an extension directory: {directory}", file=sys.stderr)
        return 1
    print(json.dumps(resolve(directory), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
