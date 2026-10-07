"""Registering the skills in a settings file that belongs to the operator.

The installer writes into ~/.claude/settings.json, which holds everything else
the operator has configured. These run it against a stand-in file: what it adds,
what it retires, and that it leaves the rest alone.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import install_skill


class InstallSkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Path(self.temp.name) / ".claude" / "settings.json"
        self.source = install_skill.plugin_dir()

    def tearDown(self):
        self.temp.cleanup()

    def run_installer(self, *arguments):
        with patch.object(install_skill, "settings_path", return_value=self.settings):
            with redirect_stdout(io.StringIO()) as output:
                code = install_skill.main(list(arguments))
        return code, output.getvalue()

    def write(self, value):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_the_one_skill_is_prep_under_the_excall_prefix(self):
        """The slash prefix is the plugin's name, so the name is the contract."""
        plugin = json.loads((self.source / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((self.source / ".claude-plugin" / "marketplace.json").read_text())

        self.assertEqual(self.source.name, "excall")
        self.assertEqual(plugin["name"], "excall")
        self.assertEqual(market["plugins"][0]["name"], "excall")
        self.assertEqual(install_skill.PLUGIN, f'{plugin["name"]}@{market["name"]}')
        self.assertEqual(sorted(path.parent.name for path in self.source.glob("skills/*/SKILL.md")),
                         ["prep"])

    def test_a_first_install_registers_the_plugin_from_this_checkout(self):
        code, _ = self.run_installer()

        self.assertEqual(code, 0)
        settings = self.read()
        self.assertEqual(
            settings["extraKnownMarketplaces"]["excall-local"],
            {"source": {"source": "directory", "path": str(self.source)}},
        )
        self.assertIs(settings["enabledPlugins"]["excall@excall-local"], True)

    def test_the_older_prefixes_are_retired_and_everything_else_is_kept(self):
        old_directory = str(self.source.parent / "xcall")
        self.write({
            "model": "kept",
            "extraKnownMarketplaces": {
                "webcall-local": {"source": {"source": "directory", "path": "/elsewhere/webcall"}},
                "xcall-local": {"source": {"source": "directory", "path": old_directory}},
                "someone-else": {"source": {"source": "github", "repo": "a/b"}},
            },
            "enabledPlugins": {
                "webcall@webcall-local": True,
                "xcall@xcall-local": True,
                "other@someone-else": True,
            },
        })

        code, _ = self.run_installer()

        self.assertEqual(code, 0)
        settings = self.read()
        self.assertEqual(settings["model"], "kept")
        self.assertIs(settings["enabledPlugins"]["excall@excall-local"], True)
        self.assertIs(settings["enabledPlugins"]["other@someone-else"], True)
        self.assertIs(settings["enabledPlugins"]["webcall@webcall-local"], False)
        # Its folder was renamed, so the entry would name a directory that is gone.
        self.assertNotIn("xcall-local", settings["extraKnownMarketplaces"])
        self.assertNotIn("xcall@xcall-local", settings["enabledPlugins"])
        self.assertIn("webcall-local", settings["extraKnownMarketplaces"])
        self.assertIn("someone-else", settings["extraKnownMarketplaces"])
        backups = list(self.settings.parent.glob("settings.json.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertIs(json.loads(backups[0].read_text())["enabledPlugins"]["xcall@xcall-local"], True)

    def test_an_xcall_registration_from_another_checkout_is_only_disabled(self):
        self.write({
            "extraKnownMarketplaces": {
                "xcall-local": {"source": {"source": "directory", "path": "/another/checkout/skill/xcall"}},
            },
            "enabledPlugins": {"xcall@xcall-local": True},
        })

        self.run_installer()

        settings = self.read()
        self.assertIn("xcall-local", settings["extraKnownMarketplaces"])
        self.assertIs(settings["enabledPlugins"]["xcall@xcall-local"], False)

    def test_a_second_install_changes_nothing(self):
        self.run_installer()
        first = self.settings.read_bytes()

        code, output = self.run_installer()

        self.assertEqual(code, 0)
        self.assertIn("already registered", output)
        self.assertEqual(self.settings.read_bytes(), first)
        self.assertEqual(list(self.settings.parent.glob("settings.json.bak-*")), [])

    def test_a_dry_run_writes_nothing(self):
        self.write({"enabledPlugins": {"xcall@xcall-local": True}})
        before = self.settings.read_bytes()

        code, output = self.run_installer("--dry-run")

        self.assertEqual(code, 0)
        self.assertIn("would write", output)
        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
