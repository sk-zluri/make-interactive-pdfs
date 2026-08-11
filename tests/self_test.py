#!/usr/bin/env python3
"""Create a synthetic PDF and exercise both bundled CLIs end to end."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


REPO = Path(__file__).resolve().parents[1]


def create_fixture(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(72, height - 90, "Table of Contents")
    rows = [("Introduction", 1), ("Security Controls", 2), ("Conclusion", 3)]
    y = height - 150
    for title, number in rows:
        pdf.setFont("Helvetica", 14)
        pdf.drawString(72, y, title)
        pdf.drawRightString(width - 72, y, str(number))
        y -= 28
    pdf.drawString(72, y - 20, "https://example.com/security")
    pdf.showPage()
    for number, title in enumerate(("Introduction", "Security Controls", "Conclusion"), start=1):
        pdf.setFont("Helvetica-Bold", 24)
        pdf.drawString(72, height - 90, title)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(72, height - 130, f"Content for {title}.")
        pdf.drawString(72, 45, str(number))
        pdf.showPage()
    pdf.save()


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    print(completed.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="interactive-pdf-self-test-") as temp:
        temp_dir = Path(temp)
        source = temp_dir / "fixture.pdf"
        output = temp_dir / "fixture-interactive.pdf"
        restored = temp_dir / "fixture-restored-from-reference.pdf"
        report = temp_dir / "report.json"
        create_fixture(source)
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "make_interactive_pdf.py"),
                str(source),
                "--output",
                str(output),
                "--report-json",
                str(report),
                "--strict",
            ]
        )
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(output),
                "--require-internal",
                "--require-external",
                "--render-pages",
                "auto",
                "--render-dir",
                str(temp_dir / "verification"),
            ]
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        if data["added_links"].get("internal") != 3:
            raise RuntimeError(data)
        if data["added_links"].get("external") != 1:
            raise RuntimeError(data)
        if len(PdfReader(output).pages) != 4:
            raise RuntimeError("Output page count mismatch")
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "make_interactive_pdf.py"),
                str(source),
                "--reference-pdf",
                str(output),
                "--output",
                str(restored),
                "--strict",
            ]
        )
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(restored),
                "--require-internal",
                "--require-external",
            ]
        )

        root_source = temp_dir / "root-choice.pdf"
        shutil.copy2(source, root_source)
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "make_interactive_pdf.py"),
                str(root_source),
                "--output-mode",
                "root",
                "--strict",
            ]
        )
        root_output = temp_dir / "root-choice - Interactive.pdf"
        if not root_output.is_file():
            raise RuntimeError("Root output mode did not create the expected PDF")
        if list(temp_dir.glob("root-choice*Link Report.json")):
            raise RuntimeError("Root output mode unexpectedly created a link report")

        folder_source = temp_dir / "folder-choice.pdf"
        shutil.copy2(source, folder_source)
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "make_interactive_pdf.py"),
                str(folder_source),
                "--output-mode",
                "folder",
                "--strict",
            ]
        )
        folder_output = temp_dir / "output" / "folder-choice - Interactive.pdf"
        folder_report = temp_dir / "output" / "folder-choice - Link Report.json"
        if not folder_output.is_file() or not folder_report.is_file():
            raise RuntimeError("Folder output mode did not create the expected PDF and report")
        folder_files = sorted(path.name for path in (temp_dir / "output").iterdir())
        if folder_files != [folder_output.name, folder_report.name]:
            raise RuntimeError(f"Folder output mode created unexpected artifacts: {folder_files}")
    print("SELF TEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
