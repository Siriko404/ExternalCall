import json
import re
import unittest
from pathlib import Path
from companion.jobs import check_job


class ProtocolTests(unittest.TestCase):
    def test_documented_job_example_is_accepted(self):
        root = Path(__file__).resolve().parents[2]
        job = json.loads((root / "examples/repository_audit.job.json").read_text())
        check_job(job)
        for item in job["sources"]:
            self.assertTrue((root / item["path"]).is_file())

    def test_skill_references_are_resolvable(self):
        root = Path(__file__).resolve().parents[2] / "skill/xcall"
        for skill in (root / "skills").glob("*/SKILL.md"):
            for relative in re.findall(r"\]\(([^)]+)\)", skill.read_text()):
                self.assertTrue((skill.parent / relative).is_file(), relative)
