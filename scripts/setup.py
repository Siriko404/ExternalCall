"""Install ExternalCall end to end. One command, run once, from the checkout.

    python scripts/setup.py
    python scripts/setup.py --dry-run     # report the plan, change nothing
    python scripts/setup.py --no-browser  # do not open Chrome or wait for it

There are two halves and they used to be separate errands: the native-messaging
host that lets Chrome talk to the companion, and the `externalcall` skills that give
an agent session a way to drive it. Neither is useful without the other, and
doing one and forgetting the other produces a system that looks installed and
does nothing.

Everything a machine can do is done here, including the parts around the one
step it cannot. Loading an unpacked extension is that step: Chrome removed
`--load-extension` from stable, and a profile's preferences are HMAC-protected
against being written by hand. Verified against Chrome 151 — launching with
`--load-extension` on a fresh profile leaves the extension absent.

So the click stays with a person, and everything else does not. This opens
chrome://extensions, puts the folder path on the clipboard so it can be pasted
into the picker rather than navigated to, waits for Chrome to record the
extension, and then checks that what Chrome loaded is what the host was pinned
to — repinning by itself if they disagree. The only other human step is
restarting Claude Code, because slash commands register at startup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from extension_id import (  # noqa: E402
    loaded_extension_id,
    resolve,
    wait_for_extension,
)

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

GUIDE = """\
How to use it, in full:

  /xcall:prep   describe the task; it writes the request, freezes the files
                  and hands you an exchange to launch
  /xcall:manage status, results, recovery and correction

  In the side panel: Go -> click ChatGPT's own Attach files -> check what
  attached -> Send -> download the one archive -> Done and validate.

  The extension never sends and never reads the reply page. Those clicks are
  yours by design, not by omission.
"""


def run(command: list[str], what: str) -> None:
    print(f"\n--- {what} ---", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(
            f"\n{what} failed with exit code {result.returncode}. "
            "Nothing further was attempted; read the output above."
        )


def chrome_executable() -> str | None:
    if IS_WINDOWS:
        try:
            import winreg
        except ImportError:
            return None
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if value and Path(value).is_file():
                return str(value)
        return None
    for name in (
        "google-chrome-stable",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "brave-browser",
    ):
        binary = shutil.which(name)
        if binary:
            return binary
    return None


def stage_on_clipboard(text: str) -> bool:
    """Put the folder on the clipboard, and only say so if it round-trips.

    `clip.exe` takes UTF-16LE on stdin and this checkout's path survives it
    intact. It is still read back before the operator is told to paste, because
    the failure it would otherwise produce is the worst kind: a mangled path
    pasted into Chrome's folder picker, in the one step the installer exists to
    make foolproof, with nothing having reported a problem.
    """
    if IS_WINDOWS:
        try:
            subprocess.run("clip", input=text.encode("utf-16-le"), check=True, shell=True)
            readback = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            return False
        return readback.strip() == text

    # Wayland first (Hyprland, GNOME on Wayland), then X11. wl-paste appends a
    # newline to its output, hence --no-newline before the comparison.
    if shutil.which("wl-copy") and shutil.which("wl-paste"):
        put, get = ["wl-copy"], ["wl-paste", "--no-newline"]
    elif shutil.which("xclip"):
        put, get = ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]
    elif shutil.which("xsel"):
        put, get = ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]
    else:
        return False
    try:
        subprocess.run(put, input=text.encode(), check=True)
        readback = subprocess.run(
            get, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return False
    return readback.strip() == text


def pinned_origin_id() -> str | None:
    """The extension id the installed host manifest actually pins.

    Read as utf-8-sig, not utf-8. PowerShell's `Set-Content -Encoding UTF8`
    writes a BOM, so the manifest this very repository generates starts with
    one; reading it strictly made this function return None and the caller
    concluded the pin disagreed with Chrome and repinned an already correct
    install. Chrome itself takes the BOM without complaint, which is why the
    file has carried one all along without anyone noticing.
    """
    manifest = ROOT / "native-host" / "com.sina.externalcall.json"
    if not manifest.is_file():
        return None
    import json

    try:
        origins = json.loads(manifest.read_text(encoding="utf-8-sig"))["allowed_origins"]
    except (OSError, ValueError, KeyError):
        return None
    if not origins:
        return None
    return str(origins[0]).removeprefix("chrome-extension://").rstrip("/")


def install_extension(extension: Path, no_browser: bool) -> None:
    """Walk the operator through the one step, then verify it happened."""
    print("\n--- Chrome extension ---")
    already = loaded_extension_id(extension)
    if already:
        print(f"Chrome already has this extension loaded as {already}.")
        print("Reload it in chrome://extensions so it picks up this checkout.")
        confirm_pinned_id(already)
        return

    print("This is the one step a script cannot do: Chrome removed")
    print("--load-extension from stable, so the folder has to be chosen by hand.")
    print()
    print(f"    {extension}")
    print()
    if stage_on_clipboard(str(extension)):
        print("That path is on your clipboard.")
    print("In chrome://extensions: turn on Developer mode, click Load unpacked,")
    print("paste the path into the folder picker, and choose it.")

    if no_browser:
        print("\n(--no-browser: not opening Chrome, not waiting.)")
        return

    chrome = chrome_executable()
    if chrome:
        try:
            subprocess.Popen([chrome, "chrome://extensions"])
            print("\nOpened chrome://extensions.")
        except OSError:
            print("\nCould not launch Chrome; open chrome://extensions yourself.")
    else:
        print("\nChrome was not found; open chrome://extensions yourself.")

    print("Waiting up to five minutes for Chrome to report it. Ctrl+C to skip.")
    try:
        found = wait_for_extension(extension, timeout=300.0)
    except KeyboardInterrupt:
        print("\nSkipped. Rerun this script once the extension is loaded.")
        return
    if not found:
        print("\nChrome has not recorded it yet. Rerun this script once it is loaded;")
        print("nothing already installed is undone by running it again.")
        return
    print(f"Chrome loaded it as {found}.")
    confirm_pinned_id(found)


def confirm_pinned_id(actual: str) -> None:
    """The host pins one origin. If Chrome disagrees, repin rather than report."""
    pinned = pinned_origin_id()
    if pinned == actual:
        print(f"The native host is pinned to the same id. ({actual})")
        return
    print(f"The native host is pinned to {pinned}, but Chrome loaded {actual}.")
    print("Repinning.")
    if IS_WINDOWS:
        run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "install.ps1"),
                "-ExtensionId",
                actual,
            ],
            "Native messaging host (repin)",
        )
    else:
        run(
            [sys.executable, str(ROOT / "scripts" / "install.py"), "--extension-id", actual],
            "Native messaging host (repin)",
        )
    print("Reload the extension in chrome://extensions so it reconnects.")


def native_host_installer(dry_run: bool) -> list[str]:
    """The per-platform command that registers the native-messaging host."""
    if IS_WINDOWS:
        installer = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "install.ps1"),
        ]
        if dry_run:
            installer.append("-WhatIf")
        return installer
    installer = [sys.executable, str(ROOT / "scripts" / "install.py")]
    if dry_run:
        installer.append("--dry-run")
    return installer


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    no_browser = "--no-browser" in argv or dry_run

    if not IS_WINDOWS and not IS_LINUX:
        print(
            "ExternalCall installs on Windows and Linux: it registers a Chrome "
            "native-messaging host (under HKCU on Windows, in the browser's "
            "NativeMessagingHosts directory on Linux) and the extension asserts "
            "absolute attachment paths.",
            file=sys.stderr,
        )
        return 1
    if sys.version_info < (3, 10):
        print(
            f"Python 3.10 or newer is required; this is {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        return 1
    if not (ROOT / "WEB_CALL_PROTOCOL.md").is_file():
        print(f"not a complete checkout: {ROOT}", file=sys.stderr)
        return 1

    active_files = list((ROOT / "state" / "active").glob("*.json"))
    if (ROOT / "state" / "ACTIVE_CALL.json").is_file():
        active_files.append(ROOT / "state" / "ACTIVE_CALL.json")
    if active_files and not dry_run:
        print("Finish or stop active calls before changing this installation.", file=sys.stderr)
        return 1
    if not dry_run:
        environment = ROOT / ".venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        runtime_python = environment / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
        run([str(runtime_python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
            "Python validator dependency")
    else:
        print("Would install the pinned Python validator from requirements.txt.")
    run(native_host_installer(dry_run), "Native messaging host")

    skill = [sys.executable, str(ROOT / "scripts" / "install_skill.py")]
    if dry_run:
        skill.append("--dry-run")
    run(skill, "Claude Code skills")

    extension = ROOT / "extension"
    if dry_run:
        answer = resolve(extension)
        print(f"\nWould use extension id {answer['id']} (source: {answer['source']}).")
        print("Would open chrome://extensions and wait for the extension to load.")
        print("\nDry run. Nothing was changed.")
        return 0

    install_extension(extension, no_browser)

    print(
        f"""
=== One step left ===

Restart Claude Code. Slash commands register at startup, so /xcall:* appears
in the next session, not this one.

Use /xcall:prep for a small smoke-test job and record the actual browser
observations from skill/xcall/references/SMOKE_TEST.md.

{GUIDE}"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
