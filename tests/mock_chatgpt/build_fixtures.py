"""Build deterministic one-ZIP download fixtures for the local UAT page."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent / "fixtures"
for output, artifact in (("fixture_outputs.zip", "report.md"), ("fixture_mismatch.zip", "report-mismatch.md")):
    with zipfile.ZipFile(ROOT / output, "w") as archive:
        for name, source in (("result.json", "result.json"), ("report.md", artifact)):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, (ROOT / source).read_bytes())
