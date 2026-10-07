"""Updating the checkout from what has been published.

The refusals are the whole point. An update replaces the code that is watching a
running call's downloads, and it moves files the operator has open; getting it
wrong costs work that cannot be recovered from the panel. So this drives real
git repositories rather than mocking git, because every refusal here is a
question about the actual state of a repository.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.update import UpdateRefused, latest_published, update


def git(cwd, *arguments):
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)}: {result.stderr}")
    return result.stdout.strip()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.upstream = base / "upstream"
        self.upstream.mkdir()
        git(self.upstream, "init", "-q", "-b", "main")
        git(self.upstream, "config", "user.email", "t@example.test")
        git(self.upstream, "config", "user.name", "t")
        (self.upstream / "WEB_CALL_PROTOCOL.md").write_text("v1\n", encoding="utf-8")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", "first")

        self.root = base / "work"
        git(base, "clone", "-q", str(self.upstream), str(self.root))
        git(self.root, "config", "user.email", "t@example.test")
        git(self.root, "config", "user.name", "t")

    def tearDown(self):
        self.temp.cleanup()

    def publish(self, path, text, message="upstream work"):
        target = self.upstream / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-qm", message)

    def test_a_running_call_stops_the_update_before_anything_moves(self):
        """The one refusal that protects work rather than tidiness.

        The download monitor lives in the code an update replaces, so a call
        updated out from under loses the files it is waiting for - while the
        panel goes on saying it is running.
        """
        self.publish("companion/core.py", "new\n")
        head_before = git(self.root, "rev-parse", "HEAD")
        active = self.root / "state" / "active"
        active.mkdir(parents=True)
        (active / "call-1.json").write_text(
            json.dumps({"exchange_id": "call-1", "monitoring": True}) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(UpdateRefused) as refusal:
            update(self.root)

        self.assertIn("call-1", str(refusal.exception))
        self.assertIn("still running", str(refusal.exception))
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head_before)

    def test_uncommitted_work_is_never_discarded(self):
        self.publish("companion/core.py", "new\n")
        (self.root / "WEB_CALL_PROTOCOL.md").write_text("mine\n", encoding="utf-8")

        with self.assertRaises(UpdateRefused) as refusal:
            update(self.root)

        self.assertIn("uncommitted", str(refusal.exception))
        self.assertEqual(
            (self.root / "WEB_CALL_PROTOCOL.md").read_text(encoding="utf-8"), "mine\n"
        )

    def test_a_diverged_checkout_is_reconciled_by_a_person_not_by_this(self):
        self.publish("companion/core.py", "theirs\n")
        (self.root / "LOCAL.md").write_text("mine\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "local only")

        with self.assertRaises(UpdateRefused) as refusal:
            update(self.root)

        self.assertIn("only ever fast-forwards", str(refusal.exception))

    def test_being_ahead_of_what_is_published_is_success_not_a_refusal(self):
        """The author's own checkout is routinely ahead: work is committed here
        and pushed later. Reporting that as a failure would cry wolf on the
        normal state of the machine the system is developed on."""
        (self.root / "LOCAL.md").write_text("mine\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "local only")

        result = update(self.root)

        self.assertEqual(result["status"], "ALREADY_CURRENT")
        self.assertEqual(result["commits_ahead"], 1)
        self.assertEqual(result["next_steps"], [])

    def test_an_update_fast_forwards_and_names_what_the_operator_must_do(self):
        self.publish("extension/sidepanel.js", "new panel\n")
        self.publish("skill/excall/skills/prep/SKILL.md", "new menu\n")

        result = update(self.root)

        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["commits"], 2)
        self.assertEqual(
            (self.root / "extension" / "sidepanel.js").read_text(encoding="utf-8"),
            "new panel\n",
        )
        self.assertIn("the Chrome extension", result["changed"])
        self.assertIn("the Claude Code skill", result["changed"])
        # Chrome cannot be told to reload an unpacked extension and Claude Code
        # registers skills at startup. Both are human steps, and an update that
        # does not say so leaves the operator running the old code.
        steps = " ".join(result["next_steps"])
        self.assertIn("chrome://extensions", steps)
        self.assertIn("Restart Claude Code", steps)
        self.assertIn("setup.py", steps)

    def test_an_update_that_touches_nothing_visible_still_says_to_reinstall(self):
        self.publish("companion/core.py", "new\n")

        result = update(self.root)

        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["changed"], ["the companion and native host"])
        self.assertEqual(len(result["next_steps"]), 1)
        self.assertIn("setup.py", result["next_steps"][0])

    def test_with_no_release_published_the_branch_is_the_target(self):
        """Where every repository starts. Refusing to update until someone cuts
        a tag would make the command useless on the day it shipped."""
        self.assertEqual(latest_published(self.root), ("origin/main", "main"))

    def test_the_branch_followed_is_the_one_the_remote_calls_its_default(self):
        """This repository's default branch is not `main`. A checkout that also
        carries a stale `origin/main` followed that instead, and reported it
        was current while the published branch moved on without it."""
        git(self.upstream, "branch", "-m", "main", "rewrite")
        self.publish("companion/core.py", "published on the default branch\n")
        # The local origin/main still exists and still points at the old commit.
        self.assertTrue(git(self.root, "rev-parse", "origin/main"))

        self.assertEqual(latest_published(self.root), ("origin/rewrite", "rewrite"))
        result = update(self.root)

        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["tracking"], "rewrite")
        self.assertEqual(
            (self.root / "companion" / "core.py").read_text(encoding="utf-8"),
            "published on the default branch\n",
        )

    def test_a_release_is_preferred_over_the_branch_once_one_exists(self):
        """And unreleased work on the branch is deliberately left behind."""
        self.publish("companion/core.py", "released\n")
        git(self.upstream, "tag", "v0.2.0")
        self.publish("companion/core.py", "unreleased\n")
        git(self.root, "fetch", "-q", "--tags", "origin")

        self.assertEqual(latest_published(self.root), ("refs/tags/v0.2.0", "v0.2.0"))

        result = update(self.root)

        self.assertEqual(result["tracking"], "v0.2.0")
        self.assertEqual(
            (self.root / "companion" / "core.py").read_text(encoding="utf-8"),
            "released\n",
        )

    def test_releases_are_ordered_by_number_and_not_by_text(self):
        """v0.10.0 is newer than v0.9.0, and sorts before it as a string."""
        for tag in ("v0.9.0", "v0.10.0", "v0.2.0"):
            self.publish("companion/core.py", f"{tag}\n")
            git(self.upstream, "tag", tag)
        git(self.root, "fetch", "-q", "--tags", "origin")

        self.assertEqual(latest_published(self.root)[1], "v0.10.0")

    def test_a_tag_that_is_not_a_version_is_not_mistaken_for_a_release(self):
        self.publish("companion/core.py", "new\n")
        git(self.upstream, "tag", "backup-pre-scrub")
        git(self.root, "fetch", "-q", "--tags", "origin")

        self.assertEqual(latest_published(self.root), ("origin/main", "main"))

    def test_a_missing_remote_is_named_rather_than_crashed_on(self):
        with self.assertRaises(UpdateRefused) as refusal:
            update(self.root, remote="upstream")

        self.assertIn("no remote named upstream", str(refusal.exception))


if __name__ == "__main__":
    unittest.main()
