#!/usr/bin/env python3
"""Verify PDF links structurally, with optional in-memory pixel comparison."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.generic import ArrayObject


def page_reference_map(reader: PdfReader) -> dict[tuple[int, int], int]:
    result: dict[tuple[int, int], int] = {}
    for index, page in enumerate(reader.pages):
        reference = page.indirect_reference
        if reference is not None:
            result[(reference.idnum, reference.generation)] = index + 1
    return result


def resolve_destination_page(reader: PdfReader, destination, references: dict[tuple[int, int], int]) -> int | None:
    if destination is None:
        return None
    if isinstance(destination, str):
        named = reader.named_destinations.get(destination)
        if named is not None:
            try:
                return reader.get_destination_page_number(named) + 1
            except Exception:
                return None
        return None
    target = destination[0] if isinstance(destination, (list, tuple, ArrayObject)) else destination
    if isinstance(target, int):
        return int(target) + 1
    if hasattr(target, "idnum"):
        return references.get((target.idnum, target.generation))
    indirect = getattr(target, "indirect_reference", None)
    if indirect is not None:
        return references.get((indirect.idnum, indirect.generation))
    return None


def annotation_manifest(reader: PdfReader) -> list[dict]:
    links: list[dict] = []
    references = page_reference_map(reader)
    for source_index, page in enumerate(reader.pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            item = {
                "source_page": source_index + 1,
                "rect": [float(value) for value in annotation.get("/Rect", [])],
                "kind": "other",
            }
            action = annotation.get("/A")
            destination = annotation.get("/Dest")
            if destination is None and action and action.get("/S") == "/GoTo":
                destination = action.get("/D")
            if destination is not None:
                item["kind"] = "internal"
                item["target_page"] = resolve_destination_page(reader, destination, references)
            elif action and action.get("/S") == "/URI":
                item["kind"] = "external"
                item["uri"] = str(action.get("/URI", ""))
            links.append(item)
    return links


def valid_uri(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "ftp"}:
        return bool(parsed.netloc)
    if parsed.scheme == "mailto":
        return "@" in parsed.path
    return False


def validate_links(reader: PdfReader, manifest: list[dict]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    page_count = len(reader.pages)
    for index, item in enumerate(manifest, start=1):
        source_page = int(item["source_page"])
        rect = item.get("rect", [])
        prefix = f"Link {index} on page {source_page}"
        if len(rect) != 4:
            errors.append(f"{prefix} has no valid rectangle")
            continue
        x0, y0, x1, y1 = (float(value) for value in rect)
        if x1 <= x0 or y1 <= y0:
            errors.append(f"{prefix} has an empty or inverted rectangle")
        page_box = reader.pages[source_page - 1].mediabox
        tolerance = 2.0
        if (
            x0 < float(page_box.left) - tolerance
            or y0 < float(page_box.bottom) - tolerance
            or x1 > float(page_box.right) + tolerance
            or y1 > float(page_box.top) + tolerance
        ):
            errors.append(f"{prefix} rectangle lies outside the page bounds")
        if item["kind"] == "internal":
            target = item.get("target_page")
            if not isinstance(target, int) or not 1 <= target <= page_count:
                errors.append(f"{prefix} has an unresolved or invalid internal destination")
        elif item["kind"] == "external":
            uri = str(item.get("uri", ""))
            if not valid_uri(uri):
                errors.append(f"{prefix} has an invalid external URI: {uri!r}")
        else:
            warnings.append(f"{prefix} uses an unsupported or incomplete link action")
    return errors, warnings


def parse_page_selection(value: str, page_count: int, manifest: list[dict]) -> list[int]:
    if value == "auto":
        pages = {1, page_count}
        pages.update(item["source_page"] for item in manifest if item["kind"] == "internal")
        pages.update(item["target_page"] for item in manifest if item.get("target_page") is not None)
        return sorted(page for page in pages if 1 <= page <= page_count)
    result = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    invalid = [page for page in result if not 1 <= page <= page_count]
    if invalid:
        raise ValueError(f"Pixel-comparison pages outside 1..{page_count}: {invalid}")
    if not result:
        raise ValueError("Pixel comparison requires at least one page or 'auto'")
    return result


def pixel_compare(
    source_path: Path,
    output_path: Path,
    page_numbers: list[int],
    scale: float,
    save_renders: Path | None,
) -> None:
    try:
        import pymupdf
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Pixel comparison requires PyMuPDF. Install requirements-pixel.txt."
        ) from exc
    if save_renders is not None:
        save_renders.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(source_path) as source_doc, pymupdf.open(output_path) as output_doc:
        matrix = pymupdf.Matrix(scale, scale)
        for page_number in page_numbers:
            source_pix = source_doc[page_number - 1].get_pixmap(matrix=matrix, alpha=False, annots=False)
            output_pix = output_doc[page_number - 1].get_pixmap(matrix=matrix, alpha=False, annots=False)
            if source_pix.width != output_pix.width or source_pix.height != output_pix.height:
                raise RuntimeError(f"Rendered page {page_number} dimensions changed")
            if source_pix.samples != output_pix.samples:
                raise RuntimeError(f"Rendered artwork differs on page {page_number}")
            if save_renders is not None:
                output_pix.save(save_renders / f"page-{page_number:04d}.png")


def verify(args: argparse.Namespace) -> dict:
    source_path = Path(args.source).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not source_path.is_file() or not output_path.is_file():
        raise FileNotFoundError("Source and output PDFs must exist")
    source = PdfReader(source_path, strict=False)
    output = PdfReader(output_path, strict=False)
    if len(source.pages) != len(output.pages):
        raise RuntimeError(f"Page count changed: {len(source.pages)} -> {len(output.pages)}")
    page_count = len(source.pages)
    for index, (source_page, output_page) in enumerate(zip(source.pages, output.pages, strict=True), start=1):
        source_size = (float(source_page.mediabox.width), float(source_page.mediabox.height))
        output_size = (float(output_page.mediabox.width), float(output_page.mediabox.height))
        if source_size != output_size:
            raise RuntimeError(f"Page {index} size changed: {source_size} -> {output_size}")
        if args.deep_content_check:
            if (source_page.extract_text() or "") != (output_page.extract_text() or ""):
                raise RuntimeError(f"Page {index} extracted text changed")

    manifest = annotation_manifest(output)
    counts = Counter(item["kind"] for item in manifest)
    if counts["internal"] + counts["external"] == 0:
        raise RuntimeError("No working internal or external links found")
    if args.require_internal and counts["internal"] == 0:
        raise RuntimeError("No internal links found")
    if args.require_external and counts["external"] == 0:
        raise RuntimeError("No external URI links found")
    link_errors, link_warnings = validate_links(output, manifest)
    if link_errors:
        preview = "; ".join(link_errors[:10])
        remainder = len(link_errors) - 10
        suffix = f"; plus {remainder} more" if remainder > 0 else ""
        raise RuntimeError(f"Link validation failed: {preview}{suffix}")

    if args.pixel_compare and args.render_pages:
        raise ValueError("Use --pixel-compare or legacy --render-pages, not both")
    selection = args.pixel_compare or args.render_pages
    save_value = args.save_renders or args.render_dir
    if save_value and not selection:
        selection = "auto"
    compared_pages: list[int] = []
    render_dir = Path(save_value).expanduser().resolve() if save_value else None
    if selection:
        compared_pages = parse_page_selection(selection, page_count, manifest)
        pixel_compare(source_path, output_path, compared_pages, args.render_scale, render_dir)

    report = {
        "source": str(source_path),
        "output": str(output_path),
        "verification_mode": "structural+pixel" if compared_pages else "structural",
        "deep_content_check": bool(args.deep_content_check),
        "pages": page_count,
        "links": dict(counts),
        "link_warnings": link_warnings,
        "pixel_compared_pages": compared_pages,
        "saved_render_dir": str(render_dir) if render_dir else None,
        "manifest": manifest,
        "status": "PASS",
    }
    if args.json:
        json_path = Path(args.json).expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Original static PDF")
    parser.add_argument("output", help="Interactive PDF to verify")
    parser.add_argument("--require-internal", action="store_true", help="Fail if no internal links exist")
    parser.add_argument("--require-external", action="store_true", help="Fail if no external links exist")
    parser.add_argument(
        "--deep-content-check",
        action="store_true",
        help="Repeat the linker's slower page-by-page extracted-text parity check",
    )
    parser.add_argument(
        "--pixel-compare",
        nargs="?",
        const="auto",
        help="Optionally compare artwork in memory; use 'auto' or comma-separated pages",
    )
    parser.add_argument("--save-renders", help="Optionally save compared output pages as PNGs")
    parser.add_argument("--render-scale", type=float, default=1.5, help="Optional pixel-comparison scale")
    parser.add_argument("--json", help="Write complete verification JSON")
    parser.add_argument("--render-pages", help=argparse.SUPPRESS)
    parser.add_argument("--render-dir", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = verify(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in report.items() if key != "manifest"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
