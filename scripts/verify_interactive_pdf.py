#!/usr/bin/env python3
"""Verify that an interactive PDF preserves its source and contains working annotations."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import pymupdf
from pypdf import PdfReader


def annotation_manifest(reader: PdfReader) -> list[dict]:
    links: list[dict] = []
    page_reference_to_index = {
        page.indirect_reference.idnum: index
        for index, page in enumerate(reader.pages)
        if page.indirect_reference is not None
    }
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
                target = destination[0] if isinstance(destination, (list, tuple)) else destination
                if isinstance(target, int):
                    item["target_page"] = int(target) + 1
                elif hasattr(target, "idnum"):
                    index = page_reference_to_index.get(target.idnum)
                    item["target_page"] = index + 1 if index is not None else None
            elif action and action.get("/S") == "/URI":
                item["kind"] = "external"
                item["uri"] = str(action.get("/URI"))
            links.append(item)
    return links


def parse_render_pages(value: str, page_count: int, manifest: list[dict]) -> list[int]:
    if value == "auto":
        pages = {1, page_count}
        pages.update(item["source_page"] for item in manifest if item["kind"] == "internal")
        pages.update(item["target_page"] for item in manifest if item.get("target_page") is not None)
        return sorted(page for page in pages if 1 <= page <= page_count)
    result = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    invalid = [page for page in result if not 1 <= page <= page_count]
    if invalid:
        raise ValueError(f"Render pages outside 1..{page_count}: {invalid}")
    return result


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
        if (source_page.extract_text() or "") != (output_page.extract_text() or ""):
            raise RuntimeError(f"Page {index} extracted text changed")

    manifest = annotation_manifest(output)
    counts = Counter(item["kind"] for item in manifest)
    if args.require_internal and counts["internal"] == 0:
        raise RuntimeError("No internal links found")
    if args.require_external and counts["external"] == 0:
        raise RuntimeError("No external URI links found")

    render_dir = None
    rendered_pages: list[int] = []
    if args.render_pages:
        rendered_pages = parse_render_pages(args.render_pages, page_count, manifest)
        render_dir = (
            Path(args.render_dir).expanduser().resolve()
            if args.render_dir
            else output_path.with_name(f"{output_path.stem}-verification")
        )
        render_dir.mkdir(parents=True, exist_ok=True)
        source_doc = pymupdf.open(source_path)
        output_doc = pymupdf.open(output_path)
        for page_number in rendered_pages:
            matrix = pymupdf.Matrix(args.render_scale, args.render_scale)
            source_pix = source_doc[page_number - 1].get_pixmap(matrix=matrix, alpha=False, annots=False)
            output_pix = output_doc[page_number - 1].get_pixmap(matrix=matrix, alpha=False, annots=False)
            if source_pix.width != output_pix.width or source_pix.height != output_pix.height:
                raise RuntimeError(f"Rendered page {page_number} dimensions changed")
            if source_pix.samples != output_pix.samples:
                raise RuntimeError(f"Rendered artwork differs on page {page_number}")
            output_pix.save(render_dir / f"page-{page_number:04d}.png")

    report = {
        "source": str(source_path),
        "output": str(output_path),
        "pages": page_count,
        "links": dict(counts),
        "rendered_pages": rendered_pages,
        "render_dir": str(render_dir) if render_dir else None,
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
    parser.add_argument("--render-pages", help="Comma-separated pages or 'auto' for link-related pages")
    parser.add_argument("--render-dir", help="Directory for verification PNGs")
    parser.add_argument("--render-scale", type=float, default=1.5, help="Raster comparison scale")
    parser.add_argument("--json", help="Write complete verification JSON")
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
