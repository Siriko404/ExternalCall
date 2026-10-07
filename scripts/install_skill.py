"""Register the externalcall skill with Claude Code, without typing /plugin.

`/plugin marketplace add` followed by `/plugin install` writes exactly two keys
into ~/.claude/settings.json: the marketplace under `extraKnownMarketplaces`,
and `excall@excall-local` under `enabledPlugins`. This writes the same two, so
an agent can install the skills itself instead of handing the operator commands
to paste.

The one skill is invoked as /excall:prep. It was /xcall:* and, before that,
/webcall:*; an install retires both older registrations, so the same skill is
never offered under two prefixes at once.

Everything else in settings.json is preserved. The previous file is copied to
settings.json.bak-<timestamp> first, and nothing is written when the file
already says all of this.

    python scripts/install_skill.py            # install
    python scripts/install_skill.py --dry-run  # show the change, write nothing
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

MARKETPLACE = "excall-local"
PLUGIN = "excall@excall-local"
RETIRED_PLUGINS = ("webcall@webcall-local", "xcall@xcall-local")
RETIRED_MARKETPLACE = "xcall-local"
RETIRED_DIRECTORY = "xcall"


def settings_path() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "settings.json"


def plugin_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "skill" / "excall"


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    source = plugin_dir()
    for required in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        if not (source / required).is_file():
            print(f"not a plugin directory: {source} is missing {required}")
            return 1

    target = settings_path()
    if target.is_file():
        settings = json.loads(target.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            print(f"{target} is not a JSON object; refusing to touch it")
            return 1
    else:
        settings = {}

    before = json.dumps(settings, sort_keys=True)
    entry = {"source": {"source": "directory", "path": str(source)}}
    marketplaces = settings.setdefault("extraKnownMarketplaces", {})
    enabled = settings.setdefault("enabledPlugins", {})
    marketplaces[MARKETPLACE] = entry
    enabled[PLUGIN] = True
    # Retire the old namespaces during an explicit installation, preserving backup.
    for retired in RETIRED_PLUGINS:
        if retired in enabled:
            enabled[retired] = False
    # The previous marketplace pointed at this checkout's skill folder under its
    # old name. That folder was renamed, so the entry now names a directory
    # that does not exist; it goes, and its plugin entry goes with it.
    previous = marketplaces.get(RETIRED_MARKETPLACE)
    if (isinstance(previous, dict) and isinstance(previous.get("source"), dict)
            and previous["source"].get("path") == str(source.parent / RETIRED_DIRECTORY)):
        del marketplaces[RETIRED_MARKETPLACE]
        enabled.pop(f"{RETIRED_DIRECTORY}@{RETIRED_MARKETPLACE}", None)
    if json.dumps(settings, sort_keys=True) == before:
        print(f"already registered: {PLUGIN} -> {source}")
        return 0

    if dry_run:
        print(f"would write {target}:")
        print(f"  extraKnownMarketplaces[{MARKETPLACE!r}] = {json.dumps(entry)}")
        print(f"  enabledPlugins[{PLUGIN!r}] = true")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, backup)
        print(f"backed up {target} to {backup.name}")
    # Write beside the target and replace, so an interrupted run cannot leave
    # the operator with a truncated settings file.
    staging = target.with_name(f".{target.name}.new")
    staging.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, target)

    print(f"registered {PLUGIN} -> {source}")
    print("Restart Claude Code for /excall:prep.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
