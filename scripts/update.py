"""Move this checkout to the latest published version.

Only the git half lives here. Re-registering afterwards is `setup.py`, which is
idempotent and already knows how; running it is a visible step the operator
sees, because `install_skill.py` writes into `~/.claude` and that is outside
this project.

The skills themselves need no reinstalling: `install_skill.py` registers this
checkout as a plugin source in place rather than copying it, so a fast-forward
updates the installed skills the moment it lands. What it does not do is make
Claude Code notice - plugins register at startup - which is why a restart is
reported as a step rather than assumed.

The refusals are the reason this is a script rather than a paragraph in a skill.
A session can skip an instruction; it cannot skip a non-zero exit.

Updating while a call is running is the one that would actually cost something.
The extension's download monitor lives in the code being replaced, so an update
mid-flight loses the files a call is waiting for — and the operator would be
looking at a panel that says everything is fine.

A script replacing itself mid-run is fine here, and was measured rather than
assumed: on Windows, `git merge --ff-only` over a running Python script returns
0, the new bytes land, and the already-compiled process runs to completion.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from companion.core import load_active_calls  # noqa: E402

# Which part of the system each top-level path belongs to, and what an operator
# has to do about it once it moves. Chrome cannot be told to reload an unpacked
# extension, and Claude Code registers skills at startup, so both are human
# steps and saying so is the difference between an update and a puzzle.
SURFACES = {
    "companion": ("the companion and native host", None),
    "cmd": ("the native-host launcher", None),
    "extension": ("the Chrome extension", "Reload it in chrome://extensions."),
    "skill": ("the Claude Code skills", "Restart Claude Code: it reads them from this checkout, but only at startup."),
    "scripts": ("the installer", "Re-run `python scripts/setup.py`."),
}


class UpdateRefused(RuntimeError):
    """A precondition failed. Nothing was changed."""


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        raise UpdateRefused(
            f"git {' '.join(arguments)} failed: {(result.stderr or '').strip()}"
        )
    return (result.stdout or "").strip()


def _is_ancestor(root: Path, earlier: str, later: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", earlier, later],
        cwd=root,
        capture_output=True,
    ).returncode == 0


def default_branch(root: Path, remote: str = "origin") -> str:
    """The branch the remote itself calls its default.

    Asked of the remote, not assumed and not read from a local ref. This
    repository's default is not `main`, and a checkout cloned from somewhere
    else keeps a stale `origin/main` that an update would then follow forever
    while reporting that it was current.
    """
    for line in _git(root, "ls-remote", "--symref", remote, "HEAD").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "ref:" and parts[2] == "HEAD":
            return parts[1].removeprefix("refs/heads/")
    return "main"


def latest_published(root: Path, remote: str = "origin") -> tuple[str, str]:
    """The newest release tag, or the remote's default branch when there are none.

    Tags are read with git rather than the GitHub API so this works from a
    checkout with no `gh` and no credentials. When the repository has never
    published a release - which is where it starts - the branch is the honest
    answer, and the moment a release is tagged this follows it with no change
    here.
    """
    lines = _git(root, "ls-remote", "--tags", "--refs", remote).splitlines()
    tags = [
        line.split("refs/tags/", 1)[1]
        for line in lines
        if "refs/tags/" in line
    ]
    versioned = [tag for tag in tags if re.fullmatch(r"v?\d+(\.\d+)*", tag)]
    if versioned:
        newest = max(
            versioned,
            key=lambda tag: [int(part) for part in re.findall(r"\d+", tag)],
        )
        return f"refs/tags/{newest}", newest
    branch = default_branch(root, remote)
    return f"{remote}/{branch}", branch


def update(root: Path = ROOT, *, remote: str = "origin") -> dict[str, object]:
    running = load_active_calls(root)
    if running:
        names = ", ".join(str(call.get("exchange_id")) for call in running)
        raise UpdateRefused(
            f"{len(running)} call(s) still running: {names}. Updating replaces the "
            "code that is watching their downloads, so finish or stop them first."
        )

    dirty = _git(root, "status", "--porcelain")
    if dirty:
        raise UpdateRefused(
            "the working tree has uncommitted changes. Commit or stash them "
            f"first; an update never discards local work.\n{dirty}"
        )

    remotes = _git(root, "remote").splitlines()
    if remote not in remotes:
        raise UpdateRefused(
            f"no remote named {remote}. This checkout has: "
            f"{', '.join(remotes) or 'none'}."
        )

    _git(root, "fetch", "--tags", "--prune", remote)
    target_ref, target_name = latest_published(root, remote)
    target = _git(root, "rev-parse", target_ref)
    before = _git(root, "rev-parse", "HEAD")

    if _is_ancestor(root, target, before):
        return {
            "status": "ALREADY_CURRENT",
            "tracking": target_name,
            "head": before,
            "commits_ahead": int(_git(root, "rev-list", "--count", f"{target}..HEAD") or 0),
            "changed": [],
            "next_steps": [],
        }
    if not _is_ancestor(root, before, target):
        raise UpdateRefused(
            f"this checkout has commits that {target_name} does not, and "
            f"{target_name} has commits this checkout does not. An update only "
            "ever fast-forwards; reconcile them yourself."
        )

    changed_paths = _git(root, "diff", "--name-only", f"{before}..{target}").splitlines()
    _git(root, "merge", "--ff-only", target)
    after = _git(root, "rev-parse", "HEAD")

    touched = {path.split("/", 1)[0] for path in changed_paths if path}
    changed = [name for name in SURFACES if name in touched]
    next_steps = ["Re-run `python scripts/setup.py`; it is safe to run again."]
    for name in changed:
        step = SURFACES[name][1]
        if step and step not in next_steps:
            next_steps.append(step)

    return {
        "status": "UPDATED",
        "tracking": target_name,
        "from": before,
        "head": after,
        "commits": int(_git(root, "rev-list", "--count", f"{before}..{after}") or 0),
        "changed": [SURFACES[name][0] for name in changed],
        "files_changed": len(changed_paths),
        "next_steps": next_steps,
    }


def main(argv: list[str]) -> int:
    import json

    remote = "origin"
    if "--remote" in argv:
        remote = argv[argv.index("--remote") + 1]
    try:
        result = update(remote=remote)
    except UpdateRefused as refusal:
        print(json.dumps({"ok": False, "error": str(refusal)}, indent=2))
        return 2
    print(json.dumps({"ok": True, "result": result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
