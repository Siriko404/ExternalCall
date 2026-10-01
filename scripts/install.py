"""Register the ExternalCall native-messaging host on Linux.

The counterpart of install.ps1, which does the same job through the registry.
On Linux there is no registry indirection: Chrome reads the host manifest
directly out of a per-browser `NativeMessagingHosts/` directory, so rendering
`native-host/com.sina.externalcall.template.json` and putting it there *is* the
registration.

The manifest must name one exact extension ID in allowed_origins, with no
wildcard. Chrome derives an unpacked extension's ID from where it sits on
disk, so scripts/extension_id.py works it out — from the browser's own profile
data when the extension is already loaded, and from the path itself when it is
not. Pass --extension-id to override both.

Everything is checked before anything is written. A missing prerequisite fails
here, with the reason, rather than surfacing later as a side panel that says
the companion is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extension_id import resolve  # noqa: E402

HOST_NAME = "com.sina.externalcall"
TEMPLATE_PATH = ROOT / "native-host" / f"{HOST_NAME}.template.json"
MANIFEST_PATH = ROOT / "native-host" / f"{HOST_NAME}.json"
HOST_PATH = ROOT / "bin" / "externalcall-host"
EXTENSION_PATH = ROOT / "extension"
MINIMUM_CHROME_MAJOR = 125

# Browser config directories in the order they are preferred. A manifest is
# written into each one that exists, because an operator may run more than one
# Chromium-family browser and the extension works in any of them.
BROWSER_CONFIG_DIRS = (
    ("chromium", Path.home() / ".config" / "chromium"),
    ("google-chrome", Path.home() / ".config" / "google-chrome"),
    ("brave", Path.home() / ".config" / "BraveSoftware" / "Brave-Browser"),
)

BROWSER_BINARIES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
)


class InstallError(Exception):
    """A prerequisite failed or the postflight disagreed. Named with its fix."""


def fail(problem: str, remedy: str) -> None:
    raise InstallError(f"{problem}\n  Fix: {remedy}")


def target_dirs() -> list[Path]:
    """The browser config directories a manifest will be written into."""
    found = [config for _, config in BROWSER_CONFIG_DIRS if config.is_dir()]
    if not found:
        fail(
            "No Chromium-family browser profile directory was found under ~/.config.",
            "Install Chromium or Google Chrome, launch it once, then rerun this script.",
        )
    return found


def check_python() -> None:
    """The registered host launches the Python companion through PATH, so the
    interpreter the launcher will find is the one that has to be checked."""
    python = shutil.which("python3") or shutil.which("python")
    if not python:
        fail(
            "Python was not found on PATH.",
            "Install Python 3.10 or newer and ensure `python3` runs in a new terminal.",
        )
    version = subprocess.run(
        [python, "--version"], capture_output=True, text=True, check=True
    ).stdout
    match = re.search(r"Python (\d+)\.(\d+)", version)
    if not match:
        fail(
            f"Could not read a version from `{python} --version`: {version.strip()}",
            "Ensure `python3` on PATH is a real Python interpreter.",
        )
    if (int(match[1]), int(match[2])) < (3, 10):
        fail(
            f"Python {match[1]}.{match[2]} is too old.",
            "Install Python 3.10 or newer.",
        )
    print(f"  Python {match[1]}.{match[2]} at {python}")


def resolve_extension_id(explicit: str | None) -> tuple[str, str]:
    if explicit:
        if not re.fullmatch(r"[a-p]{32}", explicit):
            fail(
                f"The supplied extension ID is not 32 characters of a-p: {explicit}",
                "Copy it from chrome://extensions, or omit --extension-id and let it be resolved.",
            )
        return explicit, "given on the command line"
    answer = resolve(EXTENSION_PATH)
    if not answer["id"]:
        fail(
            "Could not work out the extension ID.",
            "Load the extension in chrome://extensions and pass its ID with --extension-id.",
        )
    source = {
        "chrome": "read from the browser, which has this directory loaded",
        "derived": "derived from the extension path; the browser has not loaded it yet",
    }.get(answer["source"], answer["source"])
    return answer["id"], source


def check_go() -> None:
    if not shutil.which("go"):
        fail(
            "The Go toolchain was not found on PATH.",
            "Install Go 1.24 or newer (on Arch: `sudo pacman -S go`). It builds the small"
            " launcher that starts the Python companion; it is not a runtime dependency.",
        )
    version = subprocess.run(
        ["go", "version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    print(f"  {version}")


def chrome_binary() -> str | None:
    for name in BROWSER_BINARIES:
        binary = shutil.which(name)
        if binary:
            return binary
    return None


def check_chrome(binary: str | None) -> None:
    if not binary:
        print(
            "warning: no Chromium-family browser was found on PATH. Confirm one"
            " at version 125 or newer is installed before using the extension."
        )
        return
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=True, timeout=30
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        print(f"warning: could not read a version from `{binary} --version`.")
        return
    match = re.search(r"(\d+)\.", version)
    if match and int(match[1]) < MINIMUM_CHROME_MAJOR:
        fail(
            f"{Path(binary).name} {version.split()[-1]} is older than the required"
            f" {MINIMUM_CHROME_MAJOR}.",
            "Update the browser. The extension manifest sets"
            f" minimum_chrome_version {MINIMUM_CHROME_MAJOR}.",
        )
    print(f"  {version}")


def render_manifest(extension_id: str) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8-sig")
    return template.replace("__HOST_PATH__", str(HOST_PATH)).replace(
        "__EXTENSION_ID__", extension_id
    )


def write_manifests(extension_id: str) -> list[Path]:
    """Write the rendered manifest into every browser that could host it."""
    HOST_PATH.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/bin/sh", str(ROOT / "scripts" / "build.sh")], cwd=ROOT, check=True)
    if not HOST_PATH.is_file():
        fail(
            f"The native host was not built: {HOST_PATH}",
            "Read the Go build output above.",
        )
    HOST_PATH.chmod(HOST_PATH.stat().st_mode | 0o111)

    written = []
    rendered = render_manifest(extension_id)
    # The checkout keeps its own copy of the rendered manifest, exactly as
    # install.ps1 writes one: setup.py reads it back with pinned_origin_id()
    # to decide whether a repin is needed.
    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    for config in target_dirs():
        host_dir = config / "NativeMessagingHosts"
        host_dir.mkdir(parents=True, exist_ok=True)
        manifest = host_dir / f"{HOST_NAME}.json"
        manifest.write_text(rendered, encoding="utf-8")
        written.append(manifest)
    return written


def verify(written: list[Path], extension_id: str) -> None:
    """Re-read what was written and check it against what was meant."""
    expected_origin = f"chrome-extension://{extension_id}/"
    for manifest in written:
        try:
            value = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as problem:
            fail(
                f"The manifest at {manifest} does not read back as JSON: {problem}",
                "Rerun this script.",
            )
        if value.get("name") != HOST_NAME:
            fail(
                f"The rendered manifest names the host '{value.get('name')}'.",
                "The template was modified; restore it.",
            )
        path = value.get("path")
        if not path or not Path(path).is_file():
            fail(
                f"The rendered manifest points at a host binary that does not exist: {path}",
                "Rerun this script.",
            )
        origins = value.get("allowed_origins") or []
        if len(origins) != 1 or origins[0] != expected_origin:
            fail(
                f"The rendered manifest pins '{', '.join(origins)}' rather than"
                f" {expected_origin}.",
                "Rerun this script, passing --extension-id with the ID shown in"
                " chrome://extensions.",
            )

    for manifest in written:
        print(f"  Host manifest: {manifest}")
    print(f"  Pinned origin: {expected_origin}")
    print(f"  Registered in: {', '.join(str(m.parent.parent) for m in written)}")


def check_downloads(written: list[Path]) -> None:
    """The companion collects finished downloads from one directory. It
    defaults to ~/Downloads, but the browser lets the user choose another, and
    when the two disagree a call validates as if nothing was ever returned.
    The browser records the choice in its own preferences, so the mismatch can
    be reported here instead of being discovered at the end of a real call."""
    expected = Path.home() / "Downloads"
    seen = set()
    for manifest in written:
        preferences = manifest.parent.parent / "Default" / "Preferences"
        if not preferences.is_file() or preferences in seen:
            continue
        seen.add(preferences)
        try:
            chosen = json.loads(preferences.read_text(encoding="utf-8")).get(
                "download", {}
            ).get("default_directory")
        except (OSError, ValueError):
            chosen = None
        if chosen and Path(chosen) != expected:
            print(
                f"warning: {manifest.parent.parent.name} saves downloads to {chosen},"
                f" but the companion reads {expected}."
            )
            print("  Point the companion at the browser's directory:")
            print(f"    export EXTERNALCALL_DOWNLOADS_DIR='{chosen}'")
            print("  Then restart the browser so the companion inherits it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--extension-id",
        default=None,
        help="Override the resolved ID; use it only when chrome://extensions"
        " disagrees with what this script reports.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be done and change nothing.",
    )
    args = parser.parse_args(argv)

    print("Checking prerequisites.")
    if not TEMPLATE_PATH.is_file():
        fail(
            f"The host manifest template is missing: {TEMPLATE_PATH}",
            "Run this from a complete checkout of the repository.",
        )
    if not (EXTENSION_PATH / "manifest.json").is_file():
        fail(
            f"The extension directory is missing: {EXTENSION_PATH}",
            "Run this from a complete checkout of the repository.",
        )
    check_python()
    extension_id, id_source = resolve_extension_id(args.extension_id)
    print(f"  Extension ID {extension_id} ({id_source})")
    check_go()
    binary = chrome_binary()
    check_chrome(binary)
    targets = target_dirs()

    if args.dry_run:
        print()
        print(f"Would build:    {HOST_PATH}")
        print(f"Would write:    {MANIFEST_PATH}")
        for config in targets:
            print(f"Would register: {config / 'NativeMessagingHosts' / (HOST_NAME + '.json')}")
        print(f"Would pin origin: chrome-extension://{extension_id}/")
        return 0

    written = write_manifests(extension_id)

    print("Verifying the installation.")
    verify(written, extension_id)
    check_downloads(written)

    print()
    print("Installed. In chrome://extensions:")
    print("  - if the extension is not loaded yet: enable Developer mode, Load unpacked,")
    print(f"    and pick {EXTENSION_PATH}")
    print("  - if it is already loaded: reload it")
    print(
        "Then open its side panel: a green dot and the repository name mean the"
        " companion answered."
    )
    print()
    print(
        f"The browser should show the ID {extension_id}. If it shows a different"
        " one, rerun"
    )
    print("this script with --extension-id and that value.")
    print("If the panel says the companion is unavailable, reload the extension first.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as problem:
        print(problem, file=sys.stderr)
        raise SystemExit(1) from None
