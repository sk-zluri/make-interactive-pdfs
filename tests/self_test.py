#!/usr/bin/env python3
"""Create a synthetic PDF and exercise both bundled CLIs end to end."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NumberObject
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


def run_expect_failure(command: list[str], expected_text: str) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or expected_text not in combined:
        raise RuntimeError(f"Command did not fail as expected: {' '.join(command)}\n{combined}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="interactive-pdf-self-test-") as temp:
        temp_dir = Path(temp)
        source = temp_dir / "fixture.pdf"
        output = temp_dir / "fixture-interactive.pdf"
        restored = temp_dir / "fixture-restored-from-reference.pdf"
        report = temp_dir / "report.json"
        structural_report = temp_dir / "structural-verification.json"
        pixel_report = temp_dir / "pixel-verification.json"
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
                "--json",
                str(structural_report),
            ]
        )
        structural_data = json.loads(structural_report.read_text(encoding="utf-8"))
        if structural_data["verification_mode"] != "structural":
            raise RuntimeError(structural_data)
        if structural_data["deep_content_check"]:
            raise RuntimeError("Default structural verification repeated the deep content check")
        if structural_data["pixel_compared_pages"] or structural_data["saved_render_dir"]:
            raise RuntimeError("Default structural verification created pixel artifacts")
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(output),
                "--require-internal",
                "--require-external",
                "--pixel-compare",
                "1,4",
                "--json",
                str(pixel_report),
            ]
        )
        pixel_data = json.loads(pixel_report.read_text(encoding="utf-8"))
        if pixel_data["verification_mode"] != "structural+pixel":
            raise RuntimeError(pixel_data)
        if pixel_data["pixel_compared_pages"] != [1, 4] or pixel_data["saved_render_dir"]:
            raise RuntimeError("In-memory pixel verification produced unexpected artifacts")
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(output),
                "--render-pages",
                "1",
                "--render-dir",
                str(temp_dir / "legacy-verification"),
            ]
        )
        if not (temp_dir / "legacy-verification" / "page-0001.png").is_file():
            raise RuntimeError("Legacy render arguments no longer save requested PNGs")
        run(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(output),
                "--deep-content-check",
            ]
        )
        data = json.loads(report.read_text(encoding="utf-8"))
        if data["added_links"].get("internal") != 3:
            raise RuntimeError(data)
        if data["added_links"].get("external") != 1:
            raise RuntimeError(data)
        if len(PdfReader(output).pages) != 4:
            raise RuntimeError("Output page count mismatch")
        broken_output = temp_dir / "fixture-broken-destination.pdf"
        broken_reader = PdfReader(output)
        broken_writer = PdfWriter(clone_from=broken_reader)
        changed = False
        for annotation_ref in broken_writer.pages[0].get("/Annots", []):
            annotation = annotation_ref.get_object()
            if "/Dest" in annotation:
                annotation["/Dest"][0] = NumberObject(999)
                changed = True
                break
        if not changed:
            raise RuntimeError("Could not create the invalid-destination fixture")
        with broken_output.open("wb") as handle:
            broken_writer.write(handle)
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(broken_output),
            ],
            "invalid internal destination",
        )
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
