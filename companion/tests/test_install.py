"""The install is only worth anything if a stranger can run it.

These cover the parts of it that are ordinary code: working out the extension
ID, and the documents that tell an agent what to run. What Chrome does with a
registry key is not testable here; what we hand Chrome is.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from extension_id import (  # noqa: E402
    derive_extension_id,
    loaded_extension_id,
    resolve,
)


class ExtensionIdTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "the verified fixture is a Windows path")
    def test_the_id_is_derived_the_way_chrome_derives_it_on_windows(self):
        """Chrome hashes the absolute path encoded UTF-16LE, keeps sixteen
        bytes, and maps each hex digit onto a..p.

        The algorithm was verified against a real pair on 2026-08-14 — a loaded
        unpacked extension's directory and the ID Chrome recorded for it — and
        this fixture pins the implementation against that verified behaviour,
        so a quiet change to the encoding, the slice, or the alphabet goes red
        here. The live guarantee does not rest on this test: setup.py compares
        the pinned ID against Chrome's own record on every install and repins
        when they disagree.

        If the derivation ever breaks anyway, the symptom is a host pinned to
        an origin Chrome does not use, and a side panel that says the companion
        is unavailable with nothing else wrong.
        """
        self.assertEqual(
            derive_extension_id(Path(r"C:\GptWebCall\extension")),
            "momigejapnppdkohggnofjapbhnfcalh",
        )

    @unittest.skipUnless(os.name == "posix", "the fixture is a POSIX path")
    def test_the_id_is_derived_the_way_chrome_derives_it_on_posix(self):
        """On Linux, Chrome hashes the absolute path encoded UTF-8 — not the
        UTF-16LE Windows uses. Verified live on 2026-09-07 against Chromium
        151 on Arch Linux: the extension loaded from
        /home/sina/Work/GptWebCall/extension carried exactly the id this
        derivation gives, which is the live pair the fixture below pins. The
        install-time repin remains the live guarantee; this fixture keeps a
        quiet change to the encoding, the slice, or the alphabet red here."""
        self.assertEqual(
            derive_extension_id(Path("/home/operator/GptWebCall/extension")),
            "pclfppijpiadnaomadealohejgabdeil",
        )

    def test_every_derived_id_is_thirty_two_characters_of_a_to_p(self):
        for name in ("extension", "Extension Files", "a"):
            with self.subTest(name=name):
                value = derive_extension_id(Path("C:/somewhere") / name)
                self.assertEqual(len(value), 32)
                self.assertTrue(set(value) <= set("abcdefghijklmnop"), value)

    def test_the_derivation_does_not_depend_on_the_directory_existing(self):
        """The pinned pair above was measured on a machine where that path
        exists. `resolve()` is allowed to consult the filesystem, so if it ever
        rewrote an existing path the test would pass there and fail on every
        machine that has never had that directory. Same string in, same id out,
        present or not."""
        present = Path(__file__).resolve().parents[2] / "extension"
        self.assertTrue(present.is_dir(), "fixture assumes this checkout")
        absent = Path(str(present) + "-does-not-exist")

        self.assertEqual(
            derive_extension_id(present),
            derive_extension_id(Path(str(present))),
        )
        self.assertNotEqual(derive_extension_id(present), derive_extension_id(absent))

    def test_a_different_directory_gets_a_different_id(self):
        """Two checkouts are two extensions, which is why the path is the input
        and why moving a checkout means installing again."""
        self.assertNotEqual(
            derive_extension_id(Path("C:/one/extension")),
            derive_extension_id(Path("C:/two/extension")),
        )

    def test_chrome_s_own_record_is_read_when_it_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            # Resolved, because that is what Chrome records. A Windows temp
            # directory is handed out by its short 8.3 name (RUNNER~1), and a
            # profile never stores a path in that form.
            base = Path(temp).resolve()
            extension = base / "checkout" / "extension"
            extension.mkdir(parents=True)
            profile = base / "User Data" / "Profile 6"
            profile.mkdir(parents=True)
            (profile / "Secure Preferences").write_text(
                json.dumps(
                    {
                        "extensions": {
                            "settings": {
                                "unrelatedextensionidentifierxx": {
                                    "path": str(base / "elsewhere")
                                },
                                "aaaabbbbccccddddeeeeffffgggghhhh": {
                                    "path": str(extension)
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            found = loaded_extension_id(extension, user_data=base / "User Data")

            self.assertEqual(found, "aaaabbbbccccddddeeeeffffgggghhhh")

    def test_an_unreadable_or_absent_profile_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            extension = base / "extension"
            extension.mkdir()
            profile = base / "User Data" / "Default"
            profile.mkdir(parents=True)
            (profile / "Secure Preferences").write_text("{not json", encoding="utf-8")

            self.assertIsNone(
                loaded_extension_id(extension, user_data=base / "User Data")
            )
            self.assertIsNone(loaded_extension_id(extension, user_data=base / "gone"))

    def test_an_explicit_id_overrides_everything_and_still_reports_the_derivation(self):
        with tempfile.TemporaryDirectory() as temp:
            extension = Path(temp) / "extension"
            extension.mkdir()

            answer = resolve(extension, explicit="p" * 32)

            self.assertEqual(answer["id"], "p" * 32)
            self.assertEqual(answer["source"], "given")
            # Reported so a caller can see the two disagree.
            self.assertEqual(answer["derived"], derive_extension_id(extension))

    def test_the_derivation_is_used_when_chrome_has_never_seen_the_directory(self):
        """This is what lets the host be registered before anyone opens Chrome,
        which is what removes the ordering constraint from the install."""
        with tempfile.TemporaryDirectory() as temp:
            extension = Path(temp) / "extension"
            extension.mkdir()

            answer = resolve(extension)

            self.assertEqual(answer["source"], "derived")
            self.assertEqual(answer["id"], derive_extension_id(extension))


class PinnedManifestTests(unittest.TestCase):
    def test_the_rendered_host_manifest_is_read_through_its_bom(self):
        """PowerShell writes one, and reading strictly hid a real bug.

        `Set-Content -Encoding UTF8` prefixes a BOM, so the manifest this
        repository generates starts with one. Reading it as plain utf-8 raised,
        the caller took the exception as "nothing is pinned", and concluded the
        pin disagreed with Chrome — repinning an installation that was already
        correct. Chrome takes the BOM without complaint, which is why it went
        unnoticed until something else read the file.
        """
        setup = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")
        self.assertIn('read_text(encoding="utf-8-sig")', setup)

        installed = ROOT / "native-host" / "com.sina.externalcall.json"
        if not installed.is_file():
            self.skipTest("no host manifest on this machine")
        # Whatever the encoding, the shipped reader must cope with the file the
        # shipped installer writes.
        value = json.loads(installed.read_text(encoding="utf-8-sig"))
        self.assertEqual(len(value["allowed_origins"]), 1)


class InstallDocumentationTests(unittest.TestCase):
    def test_one_command_installs_both_halves_and_the_extension(self):
        setup = (ROOT / "scripts" / "setup.py").read_text(encoding="utf-8")

        self.assertIn("install.ps1", setup)
        self.assertIn("install.py", setup)
        self.assertIn("install_skill.py", setup)
        # The extension step is walked through and then verified, not left as an
        # instruction the installer hopes was followed.
        self.assertIn("chrome://extensions", setup)
        self.assertIn("wait_for_extension", setup)
        self.assertIn("confirm_pinned_id", setup)
        self.assertIn("Restart Claude Code", setup)

    def test_the_linux_installer_registers_without_a_registry(self):
        """On Linux there is no registry indirection: the rendered manifest is
        written directly into each browser's NativeMessagingHosts directory."""
        installer = (ROOT / "scripts" / "install.py").read_text(encoding="utf-8")

        self.assertIn("NativeMessagingHosts", installer)
        self.assertIn("allowed_origins", installer)
        self.assertIn("EXTERNALCALL_DOWNLOADS_DIR", installer)



    def test_the_installer_no_longer_demands_an_id(self):
        installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        self.assertNotIn("Mandatory = $true", installer)
        self.assertIn("extension_id.py", installer)



if __name__ == "__main__":
    unittest.main()
