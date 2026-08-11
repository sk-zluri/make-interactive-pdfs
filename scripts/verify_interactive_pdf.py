#!/usr/bin/env python3
"""Verify PDF links structurally, with optional in-memory pixel comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

from skill_provenance import require_isolated_runtime


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_content_sha256(page) -> str:
    contents = page.get_contents()
    data = contents.get_data() if contents is not None else b""
    return hashlib.sha256(data).hexdigest()


def pdf_object_digest(
    value,
    cache: dict[tuple[int, int, int], bytes],
    active: set[tuple[int, int, int]] | None = None,
) -> bytes:
    """Canonicalize a resource graph while tolerating equivalent stream recompression."""
    active = active if active is not None else set()
    if isinstance(value, IndirectObject):
        key = (id(value.pdf), int(value.idnum), int(value.generation))
        if key in cache:
            return b"R" + cache[key]
        if key in active:
            return b"CYCLE"
        active.add(key)
        digest = hashlib.sha256(pdf_object_digest(value.get_object(), cache, active)).digest()
        active.remove(key)
        cache[key] = digest
        return b"R" + digest
    if isinstance(value, StreamObject):
        try:
            stream_data = value.get_data()
            decoded = True
        except Exception:
            stream_data = getattr(value, "_data", b"")
            if isinstance(stream_data, memoryview):
                stream_data = stream_data.tobytes()
            decoded = False
        digest = hashlib.sha256(b"STREAM-DECODED" if decoded else b"STREAM-ENCODED")
        for key in sorted(value, key=str):
            if str(key) == "/Length":
                continue
            digest.update(str(key).encode("utf-8", errors="backslashreplace"))
            digest.update(pdf_object_digest(value[key], cache, active))
        digest.update(hashlib.sha256(stream_data).digest())
        return digest.digest()
    if isinstance(value, DictionaryObject):
        digest = hashlib.sha256(b"DICT")
        for key in sorted(value, key=str):
            digest.update(str(key).encode("utf-8", errors="backslashreplace"))
            digest.update(pdf_object_digest(value[key], cache, active))
        return digest.digest()
    if isinstance(value, (ArrayObject, list, tuple)):
        digest = hashlib.sha256(b"ARRAY")
        for item in value:
            digest.update(pdf_object_digest(item, cache, active))
        return digest.digest()
    if isinstance(value, bytes):
        return b"BYTES" + hashlib.sha256(value).digest()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = Decimal(str(value)).normalize()
        if number == 0:
            number = Decimal(0)
        return b"NUMBER:" + format(number, "f").encode("ascii")
    return (
        type(value).__name__.encode("ascii", errors="backslashreplace")
        + b":"
        + repr(value).encode("utf-8", errors="backslashreplace")
    )


def page_visual_resources_sha256(page, cache: dict[tuple[int, int, int], bytes]) -> str:
    digest = hashlib.sha256()
    for key in ("/Resources", "/Group", "/UserUnit"):
        digest.update(key.encode("ascii"))
        value = page.get_inherited(key) if key == "/Resources" else page.get(key)
        digest.update(pdf_object_digest(value, cache))
    return digest.hexdigest()


def catalog_visual_state_sha256(reader, cache: dict[tuple[int, int, int], bytes]) -> str:
    root = reader.trailer.get("/Root")
    if isinstance(root, IndirectObject):
        root = root.get_object()
    digest = hashlib.sha256()
    for key in ("/OCProperties", "/OutputIntents"):
        digest.update(key.encode("ascii"))
        digest.update(pdf_object_digest(root.get(key) if root else None, cache))
    return digest.hexdigest()


def rectangles_equivalent(first: list[float], second: list[float], tolerance: float = 0.5) -> bool:
    return len(first) == 4 and len(second) == 4 and all(
        abs(float(a) - float(b)) <= tolerance for a, b in zip(first, second, strict=True)
    )


def paths_collide(first: Path, second: Path) -> bool:
    if first == second:
        return True
    if first.exists() and second.exists():
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False
    return False


def atomic_write_json(path: Path, report: dict, *, force: bool) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"JSON output must be a regular file path, not: {path}")
    if path.exists() and not force:
        raise FileExistsError(f"JSON output exists; pass --force to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".json", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    backup: Path | None = None
    try:
        temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if path.exists():
            backup = Path(temporary_name + ".backup")
            os.replace(path, backup)
        try:
            os.replace(temporary, path)
        except BaseException:
            if backup and backup.exists():
                os.replace(backup, path)
            raise
        if backup:
            backup.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


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


def pdf_version_manifest(reader: PdfReader) -> dict[str, str | None]:
    root = reader.trailer.get("/Root")
    if hasattr(root, "get_object"):
        root = root.get_object()
    catalog_version = root.get("/Version") if root else None
    return {
        "header": reader.pdf_header,
        "catalog": str(catalog_version) if catalog_version is not None else None,
    }


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


def validate_link_report(
    source_manifest: list[dict],
    output_manifest: list[dict],
    report_path: Path,
    page_count: int,
    source_sha256: str,
    output_sha256: str,
) -> dict:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON link report: {report_path}") from exc
    if not isinstance(report, dict) or not isinstance(report.get("links"), list):
        raise RuntimeError("Link report must be an object containing a links array")
    errors: list[str] = []
    if report.get("schema_version") != 2:
        errors.append("link report schema_version must be 2")
    if report.get("status") != "PASS":
        errors.append(f"link report status is {report.get('status')!r}, not 'PASS'")
    for field, actual_hash in (
        ("input_sha256", source_sha256),
        ("output_sha256", output_sha256),
    ):
        declared_hash = report.get(field)
        if not isinstance(declared_hash, str) or len(declared_hash) != 64:
            errors.append(f"link report {field} is required and must be a SHA-256 digest")
        elif declared_hash.casefold() != actual_hash:
            errors.append(f"link report {field} does not match its PDF")
    if report.get("pages") != page_count:
        errors.append("link report page count does not match the source PDF")
    if report.get("unresolved_toc_rows"):
        errors.append("link report contains unresolved TOC rows")
    if int(report.get("suspected_unparsed_toc_rows", 0) or 0) > 0:
        errors.append("link report contains suspected unparsed TOC rows")

    for field in ("existing_links", "added_links", "final_links"):
        if not isinstance(report.get(field), dict):
            errors.append(f"link report {field} is required and must be an object")

    source_counts = Counter(item["kind"] for item in source_manifest)
    output_counts = Counter(item["kind"] for item in output_manifest)
    report_link_counts = Counter(
        item.get("kind") for item in report["links"] if isinstance(item, dict)
    )
    existing_counts = report.get("existing_links")
    added_counts = report.get("added_links")
    final_counts = report.get("final_links")
    if isinstance(existing_counts, dict):
        for kind in ("internal", "external", "other"):
            if int(existing_counts.get(kind, 0) or 0) != source_counts[kind]:
                errors.append(
                    f"link report existing {kind} count does not match the source PDF"
                )
    if isinstance(added_counts, dict):
        for kind in ("internal", "external"):
            if int(added_counts.get(kind, 0) or 0) != report_link_counts[kind]:
                errors.append(
                    f"link report added {kind} count does not match its links array"
                )
    if isinstance(existing_counts, dict) and isinstance(added_counts, dict) and isinstance(final_counts, dict):
        for kind in ("internal", "external", "other"):
            planned = int(existing_counts.get(kind, 0) or 0) + int(added_counts.get(kind, 0) or 0)
            if int(final_counts.get(kind, 0) or 0) != planned:
                errors.append(f"link report final {kind} count is internally inconsistent")
            if int(final_counts.get(kind, 0) or 0) != output_counts[kind]:
                errors.append(f"link report final {kind} count does not match the output PDF")

    used_output_indexes: set[int] = set()

    def matching_output_indexes(expected: dict) -> list[int]:
        candidates: list[int] = []
        for index, actual in enumerate(output_manifest):
            if index in used_output_indexes:
                continue
            if (
                actual.get("source_page") != expected.get("source_page")
                or actual.get("kind") != expected.get("kind")
                or not rectangles_equivalent(expected.get("rect", []), actual.get("rect", []))
            ):
                continue
            if expected.get("kind") == "internal" and actual.get("target_page") != expected.get("target_page"):
                continue
            if expected.get("kind") == "external" and actual.get("uri") != expected.get("uri"):
                continue
            candidates.append(index)
        return candidates

    for position, expected in enumerate(source_manifest, start=1):
        candidates = matching_output_indexes(expected)
        if not candidates:
            errors.append(
                f"source link {position} has no matching output annotation"
            )
        else:
            used_output_indexes.add(candidates[0])

    matched = 0
    for position, expected in enumerate(report["links"], start=1):
        if not isinstance(expected, dict):
            errors.append(f"reported link {position} is not an object")
            continue
        source_index = expected.get("source_page_index", expected.get("source_page"))
        rect = expected.get("rect")
        kind = expected.get("kind")
        if not isinstance(source_index, int) or not isinstance(rect, list) or len(rect) != 4:
            errors.append(f"reported link {position} has invalid source-page/rectangle metadata")
            continue
        normalized_expected = {
            "source_page": source_index + 1,
            "kind": kind,
            "rect": rect,
        }
        if kind == "internal":
            target_index = expected.get("target_page_index", expected.get("target_page"))
            if isinstance(target_index, int):
                normalized_expected["target_page"] = target_index + 1
            confidence = expected.get("confidence")
            matched_by = expected.get("matched_by")
            if report.get("mode") == "automatic-analysis" and confidence not in {"high", "medium"}:
                errors.append(f"reported internal link {position} lacks accepted semantic confidence")
            if matched_by == "physical-page-fallback":
                errors.append(f"reported internal link {position} uses an unsafe physical-page fallback")
        elif kind == "external":
            normalized_expected["uri"] = expected.get("uri")
        else:
            errors.append(f"reported link {position} has unsupported kind {kind!r}")
            continue
        candidates = matching_output_indexes(normalized_expected)
        if not candidates:
            errors.append(
                f"reported link {position} has no matching output annotation"
            )
        else:
            used_output_indexes.add(candidates[0])
            matched += 1

    if len(used_output_indexes) != len(output_manifest):
        errors.append(
            f"{len(output_manifest) - len(used_output_indexes)} output link annotations are not accounted for"
        )
    if errors:
        preview = "; ".join(errors[:10])
        remainder = len(errors) - 10
        suffix = f"; plus {remainder} more" if remainder > 0 else ""
        raise RuntimeError(f"Link-report verification failed: {preview}{suffix}")
    return {
        "path": str(report_path),
        "schema_version": report.get("schema_version"),
        "matched_links": matched,
        "status": "PASS",
    }


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
    runtime_provenance = require_isolated_runtime()
    source_path = Path(args.source).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    if not source_path.is_file() or not output_path.is_file():
        raise FileNotFoundError("Source and output PDFs must exist")
    if paths_collide(source_path, output_path):
        raise ValueError("Source and output PDFs must be different files")
    link_report_path = Path(args.link_report).expanduser().resolve() if args.link_report else None
    json_path = Path(args.json).expanduser().resolve() if args.json else None
    if link_report_path and not link_report_path.is_file():
        raise FileNotFoundError(link_report_path)
    for name, path in (("JSON output", json_path), ("link report", link_report_path)):
        if path and (paths_collide(path, source_path) or paths_collide(path, output_path)):
            raise ValueError(f"{name} must not refer to the source or output PDF")
    if json_path and link_report_path and paths_collide(json_path, link_report_path):
        raise ValueError("JSON output must differ from the input link report")
    source_sha256 = sha256_file(source_path)
    output_sha256 = sha256_file(output_path)
    source = PdfReader(source_path, strict=False)
    output = PdfReader(output_path, strict=False)
    source_pdf_version = pdf_version_manifest(source)
    output_pdf_version = pdf_version_manifest(output)
    if source_pdf_version != output_pdf_version:
        raise RuntimeError(f"PDF version changed: {source_pdf_version} -> {output_pdf_version}")
    if len(source.pages) != len(output.pages):
        raise RuntimeError(f"Page count changed: {len(source.pages)} -> {len(output.pages)}")
    page_count = len(source.pages)
    source_resource_cache: dict[tuple[int, int, int], bytes] = {}
    output_resource_cache: dict[tuple[int, int, int], bytes] = {}
    if catalog_visual_state_sha256(
        source, source_resource_cache
    ) != catalog_visual_state_sha256(output, output_resource_cache):
        raise RuntimeError("Catalog visual state changed")
    for index, (source_page, output_page) in enumerate(zip(source.pages, output.pages, strict=True), start=1):
        source_size = (float(source_page.mediabox.width), float(source_page.mediabox.height))
        output_size = (float(output_page.mediabox.width), float(output_page.mediabox.height))
        if source_size != output_size:
            raise RuntimeError(f"Page {index} size changed: {source_size} -> {output_size}")
        source_crop = tuple(float(value) for value in source_page.cropbox)
        output_crop = tuple(float(value) for value in output_page.cropbox)
        if source_crop != output_crop:
            raise RuntimeError(f"Page {index} crop box changed")
        if int(source_page.get("/Rotate", 0) or 0) != int(output_page.get("/Rotate", 0) or 0):
            raise RuntimeError(f"Page {index} rotation changed")
        if page_content_sha256(source_page) != page_content_sha256(output_page):
            raise RuntimeError(f"Page {index} content stream changed")
        if page_visual_resources_sha256(
            source_page, source_resource_cache
        ) != page_visual_resources_sha256(output_page, output_resource_cache):
            raise RuntimeError(f"Page {index} visual resources changed")
        if args.deep_content_check:
            if (source_page.extract_text() or "") != (output_page.extract_text() or ""):
                raise RuntimeError(f"Page {index} extracted text changed")

    source_manifest = annotation_manifest(source)
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

    link_report_verification = (
        validate_link_report(
            source_manifest,
            manifest,
            link_report_path,
            page_count,
            source_sha256,
            output_sha256,
        )
        if link_report_path
        else None
    )

    if args.pixel_compare and args.render_pages:
        raise ValueError("Use --pixel-compare or legacy --render-pages, not both")
    selection = args.pixel_compare or args.render_pages
    save_value = args.save_renders or args.render_dir
    if save_value and not selection:
        selection = "auto"
    compared_pages: list[int] = []
    render_dir = Path(save_value).expanduser().resolve() if save_value else None
    if selection:
        if not math.isfinite(args.render_scale) or not 0.1 <= args.render_scale <= 10.0:
            raise ValueError("--render-scale must be a finite number between 0.1 and 10.0")
        compared_pages = parse_page_selection(selection, page_count, manifest)
        pixel_compare(source_path, output_path, compared_pages, args.render_scale, render_dir)

    report = {
        "schema_version": 2,
        "source": str(source_path),
        "source_sha256": source_sha256,
        "output": str(output_path),
        "output_sha256": output_sha256,
        "verification_mode": "structural+pixel" if compared_pages else "structural",
        "content_stream_check": True,
        "visual_resource_check": True,
        "deep_content_check": bool(args.deep_content_check),
        "skill_provenance": runtime_provenance,
        "pdf_version": output_pdf_version,
        "pages": page_count,
        "links": dict(counts),
        "link_warnings": link_warnings,
        "pixel_compared_pages": compared_pages,
        "saved_render_dir": str(render_dir) if render_dir else None,
        "link_report_verification": link_report_verification,
        "manifest": manifest,
        "status": "PASS",
    }
    if json_path:
        atomic_write_json(json_path, report, force=args.force)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("source", help="Original static PDF")
    parser.add_argument("output", help="Interactive PDF to verify")
    parser.add_argument("--require-internal", action="store_true", help="Fail if no internal links exist")
    parser.add_argument("--require-external", action="store_true", help="Fail if no external links exist")
    parser.add_argument(
        "--link-report",
        help="Generation report/manifest whose intended rectangles and destinations must match exactly",
    )
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
    parser.add_argument("--force", action="store_true", help="Replace an existing verification JSON")
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
