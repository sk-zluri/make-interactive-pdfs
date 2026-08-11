#!/usr/bin/env python3
"""Fast generated-fixture regressions for safe PDF link analysis."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    Fit,
    NameObject,
    NumberObject,
    TextStringObject,
)


REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "scripts" / "run_isolated.py"
LINKER = REPO / "scripts" / "make_interactive_pdf.py"
WIDTH, HEIGHT = A4


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(name: str, *arguments: object) -> list[str]:
    return [sys.executable, str(RUNNER), name, *(str(value) for value in arguments)]


def run_json(arguments: list[str], expected_code: int = 0) -> dict:
    completed = subprocess.run(arguments, text=True, capture_output=True)
    if completed.returncode != expected_code:
        raise RuntimeError(
            f"Expected exit {expected_code}, got {completed.returncode}: {' '.join(arguments)}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Command did not emit JSON:\n{completed.stdout}\n{completed.stderr}") from exc


def draw_toc(pdf: canvas.Canvas, rows: list[tuple[str, str]], heading: str = "Table of Contents") -> None:
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(54, HEIGHT - 70, heading)
    y = HEIGHT - 115
    for title, label in rows:
        pdf.setFont("Helvetica", 12)
        pdf.drawString(54, y, title)
        pdf.drawRightString(WIDTH - 54, y, label)
        y -= 25
    pdf.showPage()


def draw_body(pdf: canvas.Canvas, title: str, label: str | None) -> None:
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(54, HEIGHT - 80, title)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(54, HEIGHT - 112, f"Body text for {title}.")
    if label is not None:
        pdf.drawCentredString(WIDTH / 2, 34, label)
    pdf.showPage()


def make_mixed_numbering(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    draw_toc(
        pdf,
        [
            ("Foreword", "ii"),
            ("Preface", "iii"),
            ("Chapter One", "1"),
            ("Chapter Two", "2"),
            ("Conclusion", "3"),
        ],
    )
    draw_body(pdf, "Divider", None)
    draw_body(pdf, "Roman One", "i")
    draw_body(pdf, "Foreword", "ii")
    draw_body(pdf, "Preface", "iii")
    draw_body(pdf, "Chapter One", "1")
    draw_body(pdf, "Chapter Two", "2")
    draw_body(pdf, "Conclusion", "3")
    pdf.save()


def make_three_volumes(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    for volume in range(1, 4):
        rows = [(f"Volume {volume} Chapter {number}", str(number)) for number in range(1, 4)]
        draw_toc(pdf, rows, heading=f"Contents — Volume {volume}")
        for number in range(1, 4):
            draw_body(pdf, f"Volume {volume} Chapter {number}", str(number))
    pdf.save()


def make_piecewise(path: Path, include_missing: bool) -> None:
    rows = [("Chapter One", "1"), ("Chapter Four", "4")]
    if include_missing:
        rows.append(("Missing Chapter Five", "5"))
    rows.extend((("Chapter Six", "6"), ("Chapter Eight", "8")))
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    draw_toc(pdf, rows)
    draw_body(pdf, "Divider", None)
    for number in range(1, 5):
        draw_body(pdf, f"Chapter {('One', 'Two', 'Three', 'Four')[number - 1]}", str(number))
    for number, word in ((6, "Six"), (7, "Seven"), (8, "Eight")):
        draw_body(pdf, f"Chapter {word}", str(number))
    pdf.save()


def make_url_fixture(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    tokens = [
        "RIKSHARA.JA",
        "Brah.ma",
        "Kaik.eyi",
        "Hanu.man",
        "l.oMiu",
        "B.sma",
        "RA.MAYANA",
        "example.com",
        "https://example.com/security",
        "www.example.org",
        "security@example.com",
    ]
    y = HEIGHT - 70
    pdf.setFont("Helvetica", 12)
    for token in tokens:
        pdf.drawString(54, y, token)
        y -= 24
    pdf.showPage()
    pdf.save()


def make_jpeg_link_fixture(path: Path, jpeg_path: Path) -> None:
    Image.new("RGB", (24, 24), (220, 24, 32)).save(jpeg_path, format="JPEG")
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    pdf.drawImage(str(jpeg_path), 54, HEIGHT - 190, width=96, height=96)
    pdf.setFont("Helvetica", 12)
    pdf.drawString(54, HEIGHT - 225, "https://example.com/security")
    pdf.linkURL(
        "https://example.com/security",
        (54, HEIGHT - 230, 240, HEIGHT - 210),
        relative=0,
    )
    pdf.showPage()
    pdf.save()


def make_corrupt_toc(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=A4, pdfVersion=(1, 6))
    draw_toc(
        pdf,
        [
            ("Chapter One", "1"),
            ("Chapter Two", "2"),
            ("Corrupted Introduction", "XVU"),
            ("Chapter Three", "3"),
        ],
    )
    for number in range(1, 4):
        draw_body(pdf, f"Chapter {('One', 'Two', 'Three')[number - 1]}", str(number))
    pdf.save()


def add_signature_marker(source: Path, output: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    writer.root_object[NameObject("/Perms")] = DictionaryObject()
    with output.open("wb") as handle:
        writer.write(handle)


def add_acroform_field(source: Path, output: Path, *, signature: bool) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Sig" if signature else "/Tx"),
            NameObject("/T"): TextStringObject("Approval" if signature else "Notes"),
            NameObject("/V"): (
                DictionaryObject({NameObject("/Type"): NameObject("/Sig")})
                if signature
                else TextStringObject("ordinary scalar value")
            ),
        }
    )
    field_ref = writer._add_object(field)
    form = DictionaryObject({NameObject("/Fields"): ArrayObject([field_ref])})
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(form)
    with output.open("wb") as handle:
        writer.write(handle)


def add_existing_links(
    source: Path,
    output: Path,
    internal_rect: tuple[float, float, float, float],
    external_rect: tuple[float, float, float, float],
) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    borderless = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)])
    writer.add_annotation(
        0,
        Link(
            rect=internal_rect,
            border=borderless,
            target_page_index=5,
            fit=Fit.fit_horizontally(top=float(reader.pages[5].mediabox.top)),
        ),
    )
    writer.add_annotation(
        0,
        Link(
            rect=external_rect,
            border=borderless,
            url="https://example.com/security",
        ),
    )
    with output.open("wb") as handle:
        writer.write(handle)


def rotate_first_page(source: Path, output: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    writer.pages[0].rotate(90)
    with output.open("wb") as handle:
        writer.write(handle)


def add_malformed_links(source: Path, output: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    annotations = ArrayObject()
    for rect, action in (
        (
            [50, 650, 220, 670],
            DictionaryObject(
                {
                    NameObject("/S"): NameObject("/URI"),
                    NameObject("/URI"): TextStringObject(""),
                }
            ),
        ),
        (
            [50, 610, 220, 630],
            DictionaryObject(
                {
                    NameObject("/S"): NameObject("/GoTo"),
                    NameObject("/D"): ArrayObject([NumberObject(999), NameObject("/Fit")]),
                }
            ),
        ),
        (
            [50, 800, 900, 900],
            DictionaryObject(
                {
                    NameObject("/S"): NameObject("/URI"),
                    NameObject("/URI"): TextStringObject("https://example.com"),
                }
            ),
        ),
    ):
        annotation = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([NumberObject(value) for value in rect]),
                NameObject("/A"): action,
            }
        )
        annotations.append(writer._add_object(annotation))
    writer.pages[0][NameObject("/Annots")] = annotations
    with output.open("wb") as handle:
        writer.write(handle)


def mutate_first_font_resource(source: Path, output: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    resources = writer.pages[0]["/Resources"].get_object()
    fonts = resources["/Font"].get_object()
    first_font = next(iter(fonts.values())).get_object()
    first_font[NameObject("/BaseFont")] = NameObject("/Courier")
    with output.open("wb") as handle:
        writer.write(handle)


def mutate_first_image_filter(source: Path, output: Path) -> None:
    reader = PdfReader(source)
    writer = PdfWriter(clone_from=reader)
    writer.pdf_header = reader.pdf_header
    resources = writer.pages[0]["/Resources"].get_object()
    xobjects = resources["/XObject"].get_object()
    changed = False
    for reference in xobjects.values():
        image = reference.get_object()
        if image.get("/Subtype") != "/Image":
            continue
        filters = image.get("/Filter")
        if isinstance(filters, ArrayObject):
            for index, value in enumerate(filters):
                if str(value) == "/DCTDecode":
                    filters[index] = NameObject("/JPXDecode")
                    changed = True
                    break
        elif str(filters) == "/DCTDecode":
            image[NameObject("/Filter")] = NameObject("/JPXDecode")
            changed = True
        if changed:
            break
    if not changed:
        raise RuntimeError("JPEG fixture had no DCT image filter to mutate")
    with output.open("wb") as handle:
        writer.write(handle)


def internal_targets(report: dict) -> dict[tuple[str, str], int]:
    return {
        (str(link.get("title")), str(link.get("printed_label"))): int(link["target_page"]) + 1
        for link in report["links"]
        if link["kind"] == "internal"
    }


def test_mixed_and_manifest(temp: Path) -> None:
    source = temp / "mixed.pdf"
    output = temp / "mixed-interactive.pdf"
    report_path = temp / "mixed-report.json"
    make_mixed_numbering(source)
    report = run_json(
        command("make", source, "--output", output, "--report-json", report_path, "--strict")
    )
    expected = {
        ("Foreword", "ii"): 4,
        ("Preface", "iii"): 5,
        ("Chapter One", "1"): 6,
        ("Chapter Two", "2"): 7,
        ("Conclusion", "3"): 8,
    }
    if (
        report["status"] != "PASS"
        or report.get("visual_resource_check") is not True
        or internal_targets(json.loads(report_path.read_text())) != expected
    ):
        raise RuntimeError(report)
    replay = temp / "mixed-replayed.pdf"
    replay_report = temp / "mixed-replayed-report.json"
    replay_data = run_json(
        command(
            "make",
            source,
            "--link-manifest",
            report_path,
            "--output",
            replay,
            "--report-json",
            replay_report,
        )
    )
    if replay_data["mode"] != "reviewed-manifest" or replay_data["status"] != "PASS":
        raise RuntimeError(replay_data)
    verified = run_json(command("verify", source, replay, "--link-report", replay_report))
    if verified["link_report_verification"]["status"] != "PASS":
        raise RuntimeError(verified)


def test_multiple_and_piecewise(temp: Path) -> None:
    multi = temp / "multi.pdf"
    multi_output = temp / "multi-interactive.pdf"
    multi_report = temp / "multi-report.json"
    make_three_volumes(multi)
    data = run_json(command("make", multi, "--output", multi_output, "--report-json", multi_report))
    if data["status"] != "PASS" or len(data["pagination_segments"]) != 3:
        raise RuntimeError(data)
    targets = internal_targets(json.loads(multi_report.read_text()))
    expected_pages = {
        (f"Volume {volume} Chapter {number}", str(number)): (volume - 1) * 4 + number + 1
        for volume in range(1, 4)
        for number in range(1, 4)
    }
    if targets != expected_pages:
        raise RuntimeError({"expected": expected_pages, "actual": targets})

    piecewise = temp / "piecewise.pdf"
    piecewise_output = temp / "piecewise-interactive.pdf"
    piecewise_report = temp / "piecewise-report.json"
    make_piecewise(piecewise, include_missing=False)
    piecewise_data = run_json(
        command("make", piecewise, "--output", piecewise_output, "--report-json", piecewise_report)
    )
    if piecewise_data["status"] != "PASS" or len(piecewise_data["pagination_segments"]) != 2:
        raise RuntimeError(piecewise_data)
    expected_piecewise = {
        ("Chapter One", "1"): 3,
        ("Chapter Four", "4"): 6,
        ("Chapter Six", "6"): 7,
        ("Chapter Eight", "8"): 9,
    }
    if internal_targets(json.loads(piecewise_report.read_text())) != expected_piecewise:
        raise RuntimeError(piecewise_data)


def test_fail_closed_and_urls(temp: Path) -> None:
    corrupt = temp / "corrupt.pdf"
    corrupt_output = temp / "corrupt-interactive.pdf"
    corrupt_report = temp / "corrupt-report.json"
    make_corrupt_toc(corrupt)
    data = run_json(
        command("make", corrupt, "--output", corrupt_output, "--report-json", corrupt_report),
        expected_code=2,
    )
    if data["status"] != "NEEDS_REVIEW" or data["suspected_unparsed_toc_rows"] < 1:
        raise RuntimeError(data)
    if corrupt_output.exists() or not corrupt_report.is_file():
        raise RuntimeError("NEEDS_REVIEW did not preserve the no-final-PDF contract")
    unchanged_replay = subprocess.run(
        command("make", corrupt, "--link-manifest", corrupt_report, "--output", corrupt_output),
        text=True,
        capture_output=True,
    )
    if unchanged_replay.returncode != 1 or corrupt_output.exists():
        raise RuntimeError("An unchanged NEEDS_REVIEW report was accepted as a reviewed manifest")

    prior_output = temp / "prior-good.pdf"
    prior_report = temp / "prior-good.json"
    shutil.copy2(corrupt, prior_output)
    prior_report.write_text('{"prior": true}\n', encoding="utf-8")
    before = (file_hash(prior_output), file_hash(prior_report))
    run_json(
        command(
            "make",
            corrupt,
            "--output",
            prior_output,
            "--report-json",
            prior_report,
            "--force",
        ),
        expected_code=2,
    )
    if before != (file_hash(prior_output), file_hash(prior_report)):
        raise RuntimeError("A failed forced run replaced a previously good deliverable")

    source_hash = file_hash(corrupt)
    failed = subprocess.run(
        command("make", corrupt, "--output", temp / "collision.pdf", "--report-json", corrupt),
        text=True,
        capture_output=True,
    )
    if failed.returncode != 1 or file_hash(corrupt) != source_hash:
        raise RuntimeError("Path-collision protection failed")

    unsigned = temp / "unsigned.pdf"
    signed = temp / "signed.pdf"
    make_mixed_numbering(unsigned)
    add_signature_marker(unsigned, signed)
    signed_output = temp / "signed-interactive.pdf"
    signed_attempt = subprocess.run(
        command("make", signed, "--output", signed_output), text=True, capture_output=True
    )
    if signed_attempt.returncode != 1 or signed_output.exists():
        raise RuntimeError("Signed input did not fail closed")
    authorized = run_json(
        command(
            "make",
            signed,
            "--output",
            signed_output,
            "--allow-signature-invalidation",
        )
    )
    if authorized["status"] != "PASS":
        raise RuntimeError(authorized)

    urls = temp / "urls.pdf"
    urls_output = temp / "urls-interactive.pdf"
    urls_report = temp / "urls-report.json"
    make_url_fixture(urls)
    url_data = run_json(
        command(
            "make",
            urls,
            "--output",
            urls_output,
            "--report-json",
            urls_report,
            "--no-toc-links",
        )
    )
    if url_data["added_links"].get("external") != 3:
        raise RuntimeError(url_data)
    if url_data["skipped_bare_domain_candidates"] != 8:
        raise RuntimeError(url_data)


def test_manifest_binding_and_overlap_semantics(temp: Path) -> None:
    source = temp / "manifest-source.pdf"
    generated = temp / "manifest-generated.pdf"
    generated_report = temp / "manifest-generated.json"
    make_mixed_numbering(source)
    run_json(
        command(
            "make",
            source,
            "--output",
            generated,
            "--report-json",
            generated_report,
        )
    )

    different_source = temp / "manifest-different-source.pdf"
    add_acroform_field(source, different_source, signature=False)
    mismatched_output = temp / "manifest-mismatched-output.pdf"
    mismatched = subprocess.run(
        command(
            "make",
            different_source,
            "--link-manifest",
            generated_report,
            "--output",
            mismatched_output,
        ),
        text=True,
        capture_output=True,
    )
    if (
        mismatched.returncode != 1
        or mismatched_output.exists()
        or "input_sha256" not in mismatched.stderr
    ):
        raise RuntimeError("A generated report was not bound to its input_sha256")

    report_data = json.loads(generated_report.read_text(encoding="utf-8"))
    schema_v2_source_only = temp / "manifest-v2-source-hash-only.json"
    schema_v2_source_data = dict(report_data)
    schema_v2_source_data["source_sha256"] = schema_v2_source_data.pop("input_sha256")
    schema_v2_source_only.write_text(
        json.dumps(schema_v2_source_data, indent=2), encoding="utf-8"
    )
    schema_v2_attempt = subprocess.run(
        command(
            "make",
            source,
            "--link-manifest",
            schema_v2_source_only,
            "--output",
            temp / "manifest-v2-source-hash-output.pdf",
        ),
        text=True,
        capture_output=True,
    )
    if schema_v2_attempt.returncode != 1 or "input_sha256" not in schema_v2_attempt.stderr:
        raise RuntimeError("Schema-v2 manifest accepted legacy-only source_sha256")

    legacy_manifest = temp / "manifest-legacy-source-hash.json"
    legacy_data = dict(schema_v2_source_data)
    legacy_data["schema_version"] = 1
    legacy_manifest.write_text(json.dumps(legacy_data, indent=2), encoding="utf-8")
    legacy_output = temp / "manifest-legacy-output.pdf"
    legacy_result = run_json(
        command(
            "make",
            source,
            "--link-manifest",
            legacy_manifest,
            "--allow-legacy-manifest",
            "--output",
            legacy_output,
        )
    )
    if (
        legacy_result["status"] != "PASS"
        or legacy_result["manifest_input"]["hash_field"] != "source_sha256"
        or legacy_result["manifest_input"]["legacy_hash_field"] is not True
    ):
        raise RuntimeError("Legacy source_sha256 compatibility failed")

    disagreeing_manifest = temp / "manifest-disagreeing-hashes.json"
    disagreeing_data = dict(report_data)
    disagreeing_data["source_sha256"] = "0" * 64
    disagreeing_manifest.write_text(
        json.dumps(disagreeing_data, indent=2), encoding="utf-8"
    )
    disagreeing = subprocess.run(
        command(
            "make",
            source,
            "--link-manifest",
            disagreeing_manifest,
            "--output",
            temp / "manifest-disagreeing-output.pdf",
        ),
        text=True,
        capture_output=True,
    )
    if disagreeing.returncode != 1 or "disagree" not in disagreeing.stderr:
        raise RuntimeError("Conflicting manifest hash fields were not rejected")

    internal_rect = (45.0, 700.0, 260.0, 720.0)
    external_rect = (45.0, 660.0, 260.0, 680.0)
    prelinked = temp / "manifest-prelinked.pdf"
    add_existing_links(source, prelinked, internal_rect, external_rect)
    replay_manifest = {
        "schema_version": 2,
        "status": "PASS",
        "input_sha256": file_hash(prelinked),
        "links": [
            {
                "kind": "internal",
                "source_page": 0,
                "rect": list(internal_rect),
                "target_page": 5,
                "label": "existing internal",
            },
            {
                "kind": "external",
                "source_page": 0,
                "rect": list(external_rect),
                "uri": "https://example.com/security",
                "label": "existing external",
            },
        ],
    }
    replay_manifest_path = temp / "manifest-overlap.json"
    replay_manifest_path.write_text(
        json.dumps(replay_manifest, indent=2), encoding="utf-8"
    )
    matching_output = temp / "manifest-overlap-matching.pdf"
    matching_report = temp / "manifest-overlap-matching.json"
    matching = run_json(
        command(
            "make",
            prelinked,
            "--link-manifest",
            replay_manifest_path,
            "--output",
            matching_output,
            "--report-json",
            matching_report,
        )
    )
    matching_report_data = json.loads(matching_report.read_text(encoding="utf-8"))
    if (
        matching["status"] != "PASS"
        or matching["covered_by_existing_links"] != 2
        or matching["manifest_input"]["covered_existing_links"] != 2
        or matching_report_data["links"]
    ):
        raise RuntimeError("Semantically identical existing links were not counted as covered")
    verified = run_json(
        command("verify", prelinked, matching_output, "--link-report", matching_report)
    )
    if verified["link_report_verification"]["matched_links"] != 0:
        raise RuntimeError(verified)

    for suffix, position, replacement in (
        ("target", 0, ("target_page", 6)),
        ("uri", 1, ("uri", "https://example.com/privacy")),
    ):
        conflict_data = json.loads(json.dumps(replay_manifest))
        conflict_data["links"][position][replacement[0]] = replacement[1]
        conflict_manifest = temp / f"manifest-overlap-conflict-{suffix}.json"
        conflict_manifest.write_text(
            json.dumps(conflict_data, indent=2), encoding="utf-8"
        )
        conflict_output = temp / f"manifest-overlap-conflict-{suffix}.pdf"
        conflict_report = temp / f"manifest-overlap-conflict-{suffix}-report.json"
        conflict = run_json(
            command(
                "make",
                prelinked,
                "--link-manifest",
                conflict_manifest,
                "--output",
                conflict_output,
                "--report-json",
                conflict_report,
            ),
            expected_code=2,
        )
        if (
            conflict["status"] != "NEEDS_REVIEW"
            or len(conflict["replay_conflicts"]) != 1
            or conflict_output.exists()
        ):
            raise RuntimeError(f"Conflicting replay {suffix} was silently accepted")


def test_form_rotation_and_destination_safety(temp: Path) -> None:
    plain = temp / "form-plain.pdf"
    make_mixed_numbering(plain)
    ordinary_form = temp / "form-ordinary.pdf"
    add_acroform_field(plain, ordinary_form, signature=False)
    ordinary_output = temp / "form-ordinary-interactive.pdf"
    ordinary = run_json(command("make", ordinary_form, "--output", ordinary_output))
    if ordinary["status"] != "PASS":
        raise RuntimeError("An ordinary scalar AcroForm value was treated as a signature")

    signature_form = temp / "form-signature.pdf"
    add_acroform_field(plain, signature_form, signature=True)
    signature_output = temp / "form-signature-interactive.pdf"
    signature_attempt = subprocess.run(
        command("make", signature_form, "--output", signature_output),
        text=True,
        capture_output=True,
    )
    if signature_attempt.returncode != 1 or signature_output.exists():
        raise RuntimeError("An AcroForm signature field without /Perms was not detected")

    url_source = temp / "rotated-url-plain.pdf"
    rotated_url = temp / "rotated-url.pdf"
    rotated_output = temp / "rotated-url-interactive.pdf"
    rotated_report = temp / "rotated-url-report.json"
    make_url_fixture(url_source)
    rotate_first_page(url_source, rotated_url)
    rotated = run_json(
        command(
            "make",
            rotated_url,
            "--output",
            rotated_output,
            "--report-json",
            rotated_report,
            "--no-toc-links",
        ),
        expected_code=2,
    )
    if (
        rotated["status"] != "NEEDS_REVIEW"
        or rotated["rotated_automatic_url_pages"] != [1]
        or rotated_output.exists()
    ):
        raise RuntimeError("Rotated automatic URL geometry did not fail closed")

    output_directory = temp / "forced-output-directory"
    output_directory.mkdir()
    output_directory_attempt = subprocess.run(
        command("make", plain, "--output", output_directory, "--force"),
        text=True,
        capture_output=True,
    )
    if output_directory_attempt.returncode != 1 or not output_directory.is_dir():
        raise RuntimeError("A forced output directory was moved or replaced")

    report_directory = temp / "forced-report-directory"
    report_directory.mkdir()
    report_output = temp / "forced-report-output.pdf"
    report_directory_attempt = subprocess.run(
        command(
            "make",
            plain,
            "--output",
            report_output,
            "--report-json",
            report_directory,
            "--force",
        ),
        text=True,
        capture_output=True,
    )
    if (
        report_directory_attempt.returncode != 1
        or not report_directory.is_dir()
        or report_output.exists()
    ):
        raise RuntimeError("A forced report directory was moved or partially published")


def test_link_validity_resources_and_concurrency(temp: Path) -> None:
    plain = temp / "validity-plain.pdf"
    malformed = temp / "validity-malformed.pdf"
    malformed_output = temp / "validity-malformed-interactive.pdf"
    make_url_fixture(plain)
    add_malformed_links(plain, malformed)
    malformed_attempt = subprocess.run(
        command("make", malformed, "--output", malformed_output, "--no-toc-links"),
        text=True,
        capture_output=True,
    )
    for expected in (
        "invalid external URI",
        "unresolved internal destination",
        "out-of-page /Rect",
    ):
        if expected not in malformed_attempt.stderr:
            raise RuntimeError(f"Malformed retained link did not report {expected!r}")
    if malformed_attempt.returncode != 1 or malformed_output.exists():
        raise RuntimeError("Malformed retained links were allowed to publish")

    resource_source = temp / "resources-source.pdf"
    resource_mutated = temp / "resources-mutated.pdf"
    make_url_fixture(resource_source)
    mutate_first_font_resource(resource_source, resource_mutated)
    sys.path.insert(0, str(REPO / "scripts"))
    from make_interactive_pdf import page_content_sha256, visual_resource_state

    source_reader = PdfReader(resource_source)
    mutated_reader = PdfReader(resource_mutated)
    if page_content_sha256(source_reader.pages[0]) != page_content_sha256(
        mutated_reader.pages[0]
    ):
        raise RuntimeError("Resource-only mutation unexpectedly changed page contents")
    if visual_resource_state(source_reader) == visual_resource_state(mutated_reader):
        raise RuntimeError("Visual-resource digest missed a BaseFont mutation")

    jpeg_source = temp / "resources-jpeg-source.pdf"
    jpeg_mutated = temp / "resources-jpeg-filter-mutated.pdf"
    make_jpeg_link_fixture(jpeg_source, temp / "resource-fixture.jpg")
    mutate_first_image_filter(jpeg_source, jpeg_mutated)
    jpeg_source_reader = PdfReader(jpeg_source)
    jpeg_mutated_reader = PdfReader(jpeg_mutated)
    if page_content_sha256(jpeg_source_reader.pages[0]) != page_content_sha256(
        jpeg_mutated_reader.pages[0]
    ):
        raise RuntimeError("Filter-only mutation unexpectedly changed page contents")
    if visual_resource_state(jpeg_source_reader) == visual_resource_state(jpeg_mutated_reader):
        raise RuntimeError("Visual-resource digest ignored an image filter mutation")
    filter_attempt = subprocess.run(
        command("verify", jpeg_source, jpeg_mutated),
        text=True,
        capture_output=True,
    )
    if filter_attempt.returncode != 1 or "visual resources changed" not in filter_attempt.stderr:
        raise RuntimeError("Structural verifier accepted a visual image-filter mutation")

    concurrent_source = temp / "concurrent-source.pdf"
    concurrent_output = temp / "concurrent-output.pdf"
    concurrent_report = temp / "concurrent-report.json"
    make_mixed_numbering(concurrent_source)
    arguments = [
        sys.executable,
        str(LINKER),
        str(concurrent_source),
        "--output",
        str(concurrent_output),
        "--report-json",
        str(concurrent_report),
        "--force",
    ]
    processes = [
        subprocess.Popen(arguments, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    results = [process.communicate() for process in processes]
    if any(process.returncode != 0 for process in processes):
        raise RuntimeError(f"Concurrent publication failed: {results}")
    final_report = json.loads(concurrent_report.read_text(encoding="utf-8"))
    if final_report["output_sha256"] != file_hash(concurrent_output):
        raise RuntimeError("Concurrent publication left a mismatched PDF/report pair")
    leftovers = [
        path.name
        for path in temp.iterdir()
        if path.name.startswith(f".{concurrent_output.name}.")
        or path.name.startswith(f".{concurrent_report.name}.")
    ]
    if leftovers:
        raise RuntimeError(f"Concurrent publication left transaction artifacts: {leftovers}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="interactive-pdf-regression-") as raw_temp:
        temp = Path(raw_temp)
        test_mixed_and_manifest(temp)
        test_multiple_and_piecewise(temp)
        test_fail_closed_and_urls(temp)
        test_manifest_binding_and_overlap_semantics(temp)
        test_form_rotation_and_destination_safety(temp)
        test_link_validity_resources_and_concurrency(temp)
    print("REGRESSION TEST PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
