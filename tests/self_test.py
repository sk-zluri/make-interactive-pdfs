#!/usr/bin/env python3
"""Create a synthetic PDF and exercise both bundled CLIs end to end."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


REPO = Path(__file__).resolve().parents[1]


def isolated_command(command: str) -> list[str]:
    return [sys.executable, str(REPO / "scripts" / "run_isolated.py"), command]


def create_fixture(path: Path) -> None:
    raw_path = path.with_name(f"{path.stem}-raw.pdf")
    pdf = canvas.Canvas(str(raw_path), pagesize=A4, pdfVersion=(1, 4))
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
    reader = PdfReader(raw_path)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = "%PDF-1.4"
    writer.root_object[NameObject("/Version")] = NameObject("/1.7")
    with path.open("wb") as handle:
        writer.write(handle)
    raw_path.unlink()


def version_signature(path: Path) -> tuple[str, str | None]:
    reader = PdfReader(path)
    root = reader.trailer["/Root"]
    catalog_version = root.get("/Version")
    return reader.pdf_header, str(catalog_version) if catalog_version is not None else None


def run(command: list[str]) -> None:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    print(completed.stdout)


def run_expect_failure(
    command: list[str], expected_text: str, *, environment: dict[str, str] | None = None
) -> None:
    completed = subprocess.run(command, text=True, capture_output=True, env=environment)
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or expected_text not in combined:
        raise RuntimeError(f"Command did not fail as expected: {' '.join(command)}\n{combined}")


def run_json(command: list[str]) -> dict:
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(f"Command failed: {' '.join(command)}\n{completed.stdout}\n{completed.stderr}")
    print(completed.stdout)
    return json.loads(completed.stdout)


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
        provenance = run_json(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--expect-repository",
                "https://github.com/sk-zluri/make-interactive-pdfs",
                "--require-git",
            ]
        )
        if provenance["status"] != "PASS" or not provenance["repository_verified"]:
            raise RuntimeError(provenance)
        if provenance["version"] != "1.1.0" or len(provenance["git_commit"] or "") != 40:
            raise RuntimeError(provenance)
        pinned_provenance = run_json(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--expect-repository",
                "https://github.com/sk-zluri/make-interactive-pdfs.git",
                "--expect-commit",
                provenance["git_commit"],
                "--require-git",
            ]
        )
        if not pinned_provenance["repository_verified"] or not pinned_provenance["commit_verified"]:
            raise RuntimeError(pinned_provenance)
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--expect-repository",
                "https://github.com/example/not-this-skill",
            ],
            "Repository mismatch",
        )
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--expect-commit",
                "0" * 40,
            ],
            "Commit mismatch",
        )
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--expect-repository",
                "file://github.com/sk-zluri/make-interactive-pdfs",
            ],
            "supported GitHub HTTPS or SSH URL",
        )
        invalid_index = temp_dir / "invalid-git-index"
        invalid_index.write_bytes(b"not a Git index")
        broken_git_environment = os.environ.copy()
        broken_git_environment["GIT_INDEX_FILE"] = str(invalid_index)
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "skill_provenance.py"),
                "--require-clean",
            ],
            "clean Git checkout",
            environment=broken_git_environment,
        )
        rejected_environment = temp_dir / "must-not-be-created"
        rejected_output = temp_dir / "must-not-be-created.pdf"
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "run_isolated.py"),
                "--venv-dir",
                str(rejected_environment),
                "--expect-repository",
                "https://github.com/example/not-this-skill",
                "make",
                str(source),
                "--output",
                str(rejected_output),
            ],
            "Repository mismatch",
        )
        if rejected_environment.exists() or rejected_output.exists():
            raise RuntimeError("Provenance failure did not occur before environment/PDF creation")
        unowned_environment = temp_dir / "unowned-environment"
        venv.EnvBuilder(with_pip=False).create(unowned_environment)
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "run_isolated.py"),
                "--venv-dir",
                str(unowned_environment),
                "make",
                str(source),
                "--output",
                str(rejected_output),
            ],
            "not owned by this skill",
        )
        shared_environment = temp_dir / "shared-site-environment"
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(shared_environment)
        (shared_environment / ".make-interactive-pdfs-environment.json").write_text(
            json.dumps({"managed_by": "make-interactive-pdfs"}), encoding="utf-8"
        )
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "run_isolated.py"),
                "--venv-dir",
                str(shared_environment),
                "make",
                str(source),
                "--output",
                str(rejected_output),
            ],
            "disable system site packages",
        )
        direct_output = temp_dir / "direct-execution-must-fail.pdf"
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "make_interactive_pdf.py"),
                str(source),
                "--output",
                str(direct_output),
            ],
            "must run through scripts/run_isolated.py",
        )
        if direct_output.exists():
            raise RuntimeError("Direct execution created an output outside the isolated runner")
        run_expect_failure(
            [
                sys.executable,
                str(REPO / "scripts" / "verify_interactive_pdf.py"),
                str(source),
                str(source),
            ],
            "must run through scripts/run_isolated.py",
        )
        run(
            [
                *isolated_command("make"),
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
                *isolated_command("verify"),
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
        if structural_data["pdf_version"] != {"header": "%PDF-1.4", "catalog": "/1.7"}:
            raise RuntimeError(structural_data)
        run_expect_failure(
            [
                *isolated_command("verify"),
                str(source),
                str(output),
                "--pixel-c",
                "auto",
            ],
            "unrecognized arguments",
        )
        run(
            [
                *isolated_command("verify"),
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
                *isolated_command("verify"),
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
                *isolated_command("verify"),
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
        if data["pdf_version"] != {"header": "%PDF-1.4", "catalog": "/1.7"}:
            raise RuntimeError(data)
        if data["skill_provenance"]["version"] != "1.1.0":
            raise RuntimeError(data["skill_provenance"])
        if not data["skill_provenance"]["bundle_sha256"]:
            raise RuntimeError(data["skill_provenance"])
        if not data["skill_provenance"]["runtime"]["isolated"]:
            raise RuntimeError(data["skill_provenance"])
        if not data["skill_provenance"]["runtime"]["environment"]:
            raise RuntimeError(data["skill_provenance"])
        if len(PdfReader(output).pages) != 4:
            raise RuntimeError("Output page count mismatch")
        if version_signature(output) != version_signature(source):
            raise RuntimeError("Output PDF version signature was not preserved")
        version_regressed_output = temp_dir / "fixture-version-regressed.pdf"
        version_reader = PdfReader(output)
        version_writer = PdfWriter(clone_from=version_reader)
        version_writer.pdf_header = "%PDF-1.3"
        with version_regressed_output.open("wb") as handle:
            version_writer.write(handle)
        run_expect_failure(
            [
                *isolated_command("verify"),
                str(source),
                str(version_regressed_output),
            ],
            "PDF version changed",
        )
        catalog_regressed_output = temp_dir / "fixture-catalog-version-regressed.pdf"
        catalog_reader = PdfReader(output)
        catalog_writer = PdfWriter(clone_from=catalog_reader)
        catalog_writer.pdf_header = catalog_reader.pdf_header
        catalog_writer.root_object[NameObject("/Version")] = NameObject("/1.6")
        with catalog_regressed_output.open("wb") as handle:
            catalog_writer.write(handle)
        run_expect_failure(
            [
                *isolated_command("verify"),
                str(source),
                str(catalog_regressed_output),
            ],
            "PDF version changed",
        )
        broken_output = temp_dir / "fixture-broken-destination.pdf"
        broken_reader = PdfReader(output)
        broken_writer = PdfWriter(clone_from=broken_reader)
        broken_writer.pdf_header = broken_reader.pdf_header
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
                *isolated_command("verify"),
                str(source),
                str(broken_output),
            ],
            "invalid internal destination",
        )
        run(
            [
                *isolated_command("make"),
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
                *isolated_command("verify"),
                str(source),
                str(restored),
                "--require-internal",
                "--require-external",
            ]
        )
        if version_signature(restored) != version_signature(source):
            raise RuntimeError("Reference-copy mode changed the PDF version signature")

        root_source = temp_dir / "root-choice.pdf"
        shutil.copy2(source, root_source)
        run(
            [
                *isolated_command("make"),
                str(root_source),
                "--output-mode",
                "root",
                "--strict",
            ]
        )
        root_output = temp_dir / "root-choice - Interactive.pdf"
        if not root_output.is_file():
            raise RuntimeError("Root output mode did not create the expected PDF")
        if version_signature(root_output) != version_signature(root_source):
            raise RuntimeError("Root output mode changed the PDF version signature")
        if list(temp_dir.glob("root-choice*Link Report.json")):
            raise RuntimeError("Root output mode unexpectedly created a link report")

        folder_source = temp_dir / "folder-choice.pdf"
        shutil.copy2(source, folder_source)
        run(
            [
                *isolated_command("make"),
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
        if version_signature(folder_output) != version_signature(folder_source):
            raise RuntimeError("Folder output mode changed the PDF version signature")
        folder_files = sorted(path.name for path in (temp_dir / "output").iterdir())
        if folder_files != [folder_output.name, folder_report.name]:
            raise RuntimeError(f"Folder output mode created unexpected artifacts: {folder_files}")
    print("SELF TEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
