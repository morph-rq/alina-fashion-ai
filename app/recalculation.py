"""Optional controlled recalculation of exported workbooks with LibreOffice.

openpyxl writes formulas without cached results. When reliable cached values
are needed (previews, automated reconciliation), this converts the workbook
with a headless LibreOffice using a private, temporary profile and returns the
recalculated file. If LibreOffice is not installed the step is unavailable and
callers must not claim recalculated values.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

CANDIDATES = [os.environ.get("FASHION_SOFFICE", ""), r"C:\Program Files\LibreOffice\program\soffice.exe",
              r"C:\Program Files (x86)\LibreOffice\program\soffice.exe", shutil.which("soffice") or "", shutil.which("libreoffice") or ""]


def soffice_path() -> str | None:
    return next((candidate for candidate in CANDIDATES if candidate and Path(candidate).is_file()), None)


def recalculate(content: bytes, timeout: int = 120) -> bytes:
    executable = soffice_path()
    if executable is None:
        raise RuntimeError("LibreOffice is not installed; formulas cannot be recalculated here.")
    with tempfile.TemporaryDirectory(prefix="recalc-") as work:
        work_path = Path(work)
        source = work_path / "in" / "workbook.xlsx"
        source.parent.mkdir()
        source.write_bytes(content)
        output = work_path / "out"
        profile = (work_path / "profile").as_uri()
        subprocess.run([executable, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--calc",
                        "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(output), str(source)],
                       check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        result = output / "workbook.xlsx"
        if not result.is_file():
            raise RuntimeError("LibreOffice did not produce a recalculated workbook.")
        return result.read_bytes()
