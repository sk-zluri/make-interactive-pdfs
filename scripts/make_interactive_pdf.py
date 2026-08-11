#!/usr/bin/env python3
"""Add internal TOC links and visible URL links to an existing PDF."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, Fit, NumberObject


TOC_HEADINGS = ("table of contents", "contents", "agenda", "index")
PAGE_TOKEN_RE = re.compile(r"^[\s.·•()\[\]-]*([0-9]{1,4}|[ivxlcdm]{1,10})[\s.·•()\[\]-]*$", re.I)
EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,24}$", re.I)
URL_RE = re.compile(r"^(?:https?://|www\.)[^\s<>]+$", re.I)
DOMAIN_RE = re.compile(
    r"^(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,24}(?::\d{1,5})?(?:/[^\s<>]*)?$",
    re.I,
)
BLOCKED_FILE_TLDS = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg", "svg"}
TRAILING_URL_PUNCTUATION = ".,;:!?)]}>'\""
LEADING_URL_PUNCTUATION = "([{<'\""
STOPWORDS = {"a", "an", "and", "for", "in", "of", "on", "or", "the", "to", "with"}


@dataclass(frozen=True)
class TocRow:
    source_page: int
    title: str
    printed_label: str
    printed_number: int
    rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class AddedLink:
    kind: str
    source_page: int
    rect: tuple[float, float, float, float]
    target_page: int | None = None
    uri: str | None = None
    label: str | None = None


def normalize_text(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def roman_to_int(value: str) -> int | None:
    value = value.upper()
    if not value or not re.fullmatch(r"[IVXLCDM]+", value):
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = 0
    previous = 0
    for char in reversed(value):
        current = values[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total if total > 0 else None


def parse_page_label(value: str) -> int | None:
    match = PAGE_TOKEN_RE.fullmatch(value)
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else roman_to_int(token)


def group_words_into_lines(words: Sequence[dict], tolerance: float = 3.0) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not lines:
            lines.append([word])
            continue
        current_top = sum(float(item["top"]) for item in lines[-1]) / len(lines[-1])
        if abs(float(word["top"]) - current_top) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda item: float(item["x0"]))
    return lines


def toc_rows_for_page(page_index: int, page) -> list[TocRow]:
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    rows: list[TocRow] = []
    for line in group_words_into_lines(words):
        if len(line) < 2:
            continue
        label = parse_page_label(str(line[-1]["text"]))
        if label is None or label <= 0:
            continue
        if float(line[-1]["x0"]) < float(page.width) * 0.55:
            continue
        title = " ".join(str(word["text"]) for word in line[:-1])
        title = re.sub(r"(?:\s*[.·•]{2,}\s*)+$", "", title).strip(" .·•-–—\t")
        normalized = normalize_text(title)
        if len(normalized) < 3 or normalized in TOC_HEADINGS:
            continue
        x0 = max(0.0, min(float(word["x0"]) for word in line) - 1.5)
        x1 = min(float(page.width), max(float(word["x1"]) for word in line) + 1.5)
        top = max(0.0, min(float(word["top"]) for word in line) - 1.5)
        bottom = min(float(page.height), max(float(word["bottom"]) for word in line) + 1.5)
        rect = (x0, float(page.height) - bottom, x1, float(page.height) - top)
        rows.append(TocRow(page_index, title, str(line[-1]["text"]), label, rect))
    return rows


def detect_toc_pages(plumber_pdf, explicit_pages: set[int] | None) -> tuple[set[int], dict[int, list[TocRow]]]:
    rows_by_page: dict[int, list[TocRow]] = {}
    toc_pages: set[int] = set()
    for page_index, page in enumerate(plumber_pdf.pages):
        rows = toc_rows_for_page(page_index, page)
        rows_by_page[page_index] = rows
        if explicit_pages is not None:
            if page_index in explicit_pages:
                toc_pages.add(page_index)
            continue
        normalized = normalize_text(page.extract_text() or "")
        has_heading = any(heading in normalized for heading in TOC_HEADINGS)
        if (has_heading and len(rows) >= 2) or len(rows) >= 6:
            toc_pages.add(page_index)
    return toc_pages, rows_by_page


def detect_page_offset(plumber_pdf, toc_pages: set[int]) -> tuple[int | None, dict[int, int], int]:
    page_count = len(plumber_pdf.pages)
    observations: list[tuple[int, int, int]] = []
    votes: Counter[int] = Counter()
    for page_index, page in enumerate(plumber_pdf.pages):
        if page_index in toc_pages:
            continue
        per_page_offsets: set[int] = set()
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        for word in words:
            top = float(word["top"])
            is_margin = top <= float(page.height) * 0.08 or top >= float(page.height) * 0.88
            if not is_margin:
                continue
            x0, x1 = float(word["x0"]), float(word["x1"])
            center = (x0 + x1) / 2
            is_page_number_position = (
                x0 <= float(page.width) * 0.22
                or x1 >= float(page.width) * 0.78
                or abs(center - float(page.width) / 2) <= float(page.width) * 0.12
            )
            if not is_page_number_position:
                continue
            label = parse_page_label(str(word["text"]))
            if label is None or label <= 0 or label > max(100, page_count * 3):
                continue
            offset = (page_index + 1) - label
            observations.append((page_index, label, offset))
            per_page_offsets.add(offset)
        votes.update(per_page_offsets)
    if not votes:
        return None, {}, 0
    best_offset, score = sorted(votes.items(), key=lambda pair: (-pair[1], abs(pair[0]), pair[0]))[0]
    if score < min(3, max(1, page_count // 2)):
        return None, {}, score
    labels = [label for _, label, offset in observations if offset == best_offset]
    if not labels:
        return None, {}, score
    minimum, maximum = min(labels), max(labels)
    mapping = {
        label: label + best_offset - 1
        for label in range(minimum, maximum + 1)
        if 0 <= label + best_offset - 1 < page_count
    }
    return best_offset, mapping, score


def unique_title_destination(title: str, normalized_pages: Sequence[str], excluded_pages: set[int]) -> int | None:
    normalized_title = normalize_text(title)
    if len(normalized_title) < 5:
        return None
    exact = [
        index
        for index, text in enumerate(normalized_pages)
        if index not in excluded_pages and normalized_title in text
    ]
    if len(exact) == 1:
        return exact[0]
    tokens = [token for token in normalized_title.split() if token not in STOPWORDS and len(token) > 2]
    if len(tokens) < 2:
        return None
    scores: list[tuple[float, int]] = []
    token_set = set(tokens)
    for index, text in enumerate(normalized_pages):
        if index in excluded_pages:
            continue
        page_tokens = set(text.split())
        score = len(token_set & page_tokens) / len(token_set)
        scores.append((score, index))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.85:
        return None
    if len(scores) > 1 and abs(scores[0][0] - scores[1][0]) < 0.05:
        return None
    return scores[0][1]


def rect_overlap_ratio(a: Sequence[float], b: Sequence[float]) -> float:
    ix0, iy0 = max(float(a[0]), float(b[0])), max(float(a[1]), float(b[1]))
    ix1, iy1 = min(float(a[2]), float(b[2])), min(float(a[3]), float(b[3]))
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, (float(a[2]) - float(a[0])) * (float(a[3]) - float(a[1])))
    area_b = max(0.0, (float(b[2]) - float(b[0])) * (float(b[3]) - float(b[1])))
    denominator = min(area_a, area_b)
    return intersection / denominator if denominator else 0.0


def annotation_kind(annotation) -> str:
    action = annotation.get("/A")
    if "/Dest" in annotation or (action and action.get("/S") == "/GoTo"):
        return "internal"
    if action and action.get("/S") == "/URI":
        return "external"
    return "other"


def existing_link_rects(reader: PdfReader) -> dict[int, list[tuple[float, float, float, float]]]:
    result: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for page_index, page in enumerate(reader.pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if (
                annotation.get("/Subtype") == "/Link"
                and annotation_kind(annotation) != "other"
                and "/Rect" in annotation
            ):
                result[page_index].append(tuple(float(value) for value in annotation["/Rect"]))
    return result


def classify_annotations(reader: PdfReader) -> Counter[str]:
    counts: Counter[str] = Counter()
    for page in reader.pages:
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            counts[annotation_kind(annotation)] += 1
    return counts


def normalized_uri(raw: str) -> str | None:
    value = raw.strip().lstrip(LEADING_URL_PUNCTUATION).rstrip(TRAILING_URL_PUNCTUATION)
    if not value:
        return None
    if EMAIL_RE.fullmatch(value):
        return f"mailto:{value}"
    if URL_RE.fullmatch(value):
        return value if value.lower().startswith(("http://", "https://")) else f"https://{value}"
    if DOMAIN_RE.fullmatch(value):
        tld = value.split("/", 1)[0].split(":", 1)[0].rsplit(".", 1)[-1].lower()
        if tld in BLOCKED_FILE_TLDS:
            return None
        return f"https://{value}"
    return None


def visible_url_candidates(plumber_pdf) -> Iterable[AddedLink]:
    for page_index, page in enumerate(plumber_pdf.pages):
        for word in page.extract_words(use_text_flow=False, keep_blank_chars=False):
            raw = str(word["text"])
            uri = normalized_uri(raw)
            if uri is None:
                continue
            rect = (
                float(word["x0"]),
                float(page.height) - float(word["bottom"]),
                float(word["x1"]),
                float(page.height) - float(word["top"]),
            )
            yield AddedLink("external", page_index, rect, uri=uri, label=raw)


def recoverable_broken_uri_candidates(reader: PdfReader) -> Iterable[AddedLink]:
    for page_index, page in enumerate(reader.pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link" or annotation_kind(annotation) != "other":
                continue
            uri = normalized_uri(str(annotation.get("/Contents", "")))
            if uri is None or "/Rect" not in annotation:
                continue
            rect = tuple(float(value) for value in annotation["/Rect"])
            yield AddedLink("external", page_index, rect, uri=uri, label="recovered annotation metadata")


def destination_page_index(reader: PdfReader, destination) -> int | None:
    if destination is None:
        return None
    target = destination[0] if isinstance(destination, (list, tuple, ArrayObject)) else destination
    if isinstance(target, int):
        return int(target)
    if hasattr(target, "idnum"):
        for index, page in enumerate(reader.pages):
            reference = page.indirect_reference
            if reference is not None and reference.idnum == target.idnum:
                return index
    return None


def reference_link_candidates(reference: PdfReader) -> tuple[list[AddedLink], list[str]]:
    links: list[AddedLink] = []
    warnings: list[str] = []
    for page_index, page in enumerate(reference.pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link" or "/Rect" not in annotation:
                continue
            rect = tuple(float(value) for value in annotation["/Rect"])
            kind = annotation_kind(annotation)
            action = annotation.get("/A")
            if kind == "internal":
                destination = annotation.get("/Dest")
                if destination is None and action:
                    destination = action.get("/D")
                target = destination_page_index(reference, destination)
                if target is None:
                    warnings.append(f"Could not resolve reference link on page {page_index + 1}")
                    continue
                links.append(
                    AddedLink(
                        "internal",
                        page_index,
                        rect,
                        target_page=target,
                        label="copied from reference PDF",
                    )
                )
            elif kind == "external":
                links.append(
                    AddedLink(
                        "external",
                        page_index,
                        rect,
                        uri=str(action.get("/URI")),
                        label="copied from reference PDF",
                    )
                )
    return links, warnings


def parse_page_list(value: str | None, page_count: int) -> set[int] | None:
    if value is None:
        return None
    pages: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part)
        if not 1 <= number <= page_count:
            raise ValueError(f"TOC page {number} is outside 1..{page_count}")
        pages.add(number - 1)
    if not pages:
        raise ValueError("--toc-pages did not contain any page numbers")
    return pages


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem} - Interactive.pdf")


def make_interactive(args: argparse.Namespace) -> dict:
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if args.output and args.output_mode:
        raise ValueError("Use either --output or --output-mode, not both")
    if args.output:
        output_mode = "custom"
        output_path = Path(args.output).expanduser().resolve()
    elif args.output_mode == "folder":
        output_mode = "folder"
        output_path = input_path.parent / "output" / f"{input_path.stem} - Interactive.pdf"
    else:
        output_mode = "root"
        output_path = default_output_path(input_path)
    if input_path == output_path:
        raise ValueError("Output must differ from the source PDF")
    if output_path.exists() and not args.force:
        raise FileExistsError(f"Output exists; pass --force to replace it: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(input_path, strict=False)
    if reader.is_encrypted:
        if not args.password or not reader.decrypt(args.password):
            raise ValueError("Encrypted PDF requires a valid --password")
    page_count = len(reader.pages)
    existing_counts = classify_annotations(reader)
    occupied = existing_link_rects(reader)
    added: list[AddedLink] = []
    unresolved: list[dict] = []
    warnings: list[str] = []
    toc_pages: set[int] = set()
    detected_offset: int | None = None
    offset_score = 0
    mode = "automatic-analysis"

    if args.reference_pdf:
        mode = "reference-copy"
        reference_path = Path(args.reference_pdf).expanduser().resolve()
        if not reference_path.is_file():
            raise FileNotFoundError(reference_path)
        reference = PdfReader(reference_path, strict=False)
        if len(reference.pages) != page_count:
            raise ValueError("Reference PDF page count differs from the source")
        for index, (source_page, reference_page) in enumerate(
            zip(reader.pages, reference.pages, strict=True), start=1
        ):
            source_size = (float(source_page.mediabox.width), float(source_page.mediabox.height))
            reference_size = (float(reference_page.mediabox.width), float(reference_page.mediabox.height))
            if any(abs(a - b) > 0.05 for a, b in zip(source_size, reference_size, strict=True)):
                raise ValueError(f"Reference PDF page {index} dimensions differ")
        candidates, reference_warnings = reference_link_candidates(reference)
        warnings.extend(reference_warnings)
        toc_pages = {link.source_page for link in candidates if link.kind == "internal"}
        for link in candidates:
            if link.kind == "internal" and args.no_toc_links:
                continue
            if link.kind == "external" and args.no_url_links:
                continue
            if any(rect_overlap_ratio(link.rect, rect) >= 0.8 for rect in occupied[link.source_page]):
                continue
            added.append(link)
            occupied[link.source_page].append(link.rect)
    else:
        with pdfplumber.open(input_path, password=args.password) as plumber_pdf:
            explicit_toc_pages = parse_page_list(args.toc_pages, page_count)
            toc_pages, rows_by_page = detect_toc_pages(plumber_pdf, explicit_toc_pages)
            detected_offset, page_mapping, offset_score = detect_page_offset(plumber_pdf, toc_pages)
            if args.page_offset is not None:
                detected_offset = args.page_offset
                page_mapping = {
                    printed: printed + detected_offset - 1
                    for printed in range(1, page_count + 1)
                    if 0 <= printed + detected_offset - 1 < page_count
                }
                offset_score = -1
            extracted_text = [page.extract_text() or "" for page in plumber_pdf.pages]
            normalized_pages = [normalize_text(text) for text in extracted_text]

            if not any(text.strip() for text in extracted_text):
                warnings.append(
                    "The PDF has no extractable text. Use OCR, --reference-pdf, or a manual link manifest."
                )

            if not args.no_toc_links:
                for source_page in sorted(toc_pages):
                    for row in rows_by_page[source_page]:
                        destination = page_mapping.get(row.printed_number)
                        matched_by = "page-offset" if destination is not None else None
                        if destination is None:
                            destination = unique_title_destination(row.title, normalized_pages, toc_pages)
                            matched_by = "title" if destination is not None else None
                        if destination is None and 1 <= row.printed_number <= page_count:
                            destination = row.printed_number - 1
                            matched_by = "physical-page-fallback"
                        if destination is None or destination == source_page:
                            unresolved.append(
                                {
                                    "source_page": source_page + 1,
                                    "title": row.title,
                                    "printed_label": row.printed_label,
                                    "reason": "no safe destination",
                                }
                            )
                            continue
                        if any(rect_overlap_ratio(row.rect, rect) >= 0.8 for rect in occupied[source_page]):
                            continue
                        link = AddedLink(
                            "internal",
                            source_page,
                            row.rect,
                            target_page=destination,
                            label=f"{row.title} -> {row.printed_label} ({matched_by})",
                        )
                        added.append(link)
                        occupied[source_page].append(row.rect)

            if not args.no_url_links:
                for link in visible_url_candidates(plumber_pdf):
                    if any(rect_overlap_ratio(link.rect, rect) >= 0.8 for rect in occupied[link.source_page]):
                        continue
                    added.append(link)
                    occupied[link.source_page].append(link.rect)

    if not args.no_url_links:
        for link in recoverable_broken_uri_candidates(reader):
            if any(rect_overlap_ratio(link.rect, rect) >= 0.8 for rect in occupied[link.source_page]):
                continue
            added.append(link)
            occupied[link.source_page].append(link.rect)

    writer = PdfWriter(clone_from=reader)
    borderless = ArrayObject([NumberObject(0), NumberObject(0), NumberObject(0)])
    for link in added:
        if link.kind == "internal":
            annotation = Link(
                rect=link.rect,
                border=borderless,
                target_page_index=int(link.target_page),
                fit=Fit.fit_horizontally(top=float(reader.pages[int(link.target_page)].mediabox.top)),
            )
        else:
            annotation = Link(rect=link.rect, border=borderless, url=str(link.uri))
        writer.add_annotation(link.source_page, annotation)

    with output_path.open("wb") as handle:
        writer.write(handle)

    result = PdfReader(output_path, strict=False)
    if len(result.pages) != page_count:
        raise RuntimeError("Output page count changed")
    for index, (source_page, result_page) in enumerate(zip(reader.pages, result.pages, strict=True), start=1):
        source_size = (float(source_page.mediabox.width), float(source_page.mediabox.height))
        result_size = (float(result_page.mediabox.width), float(result_page.mediabox.height))
        if source_size != result_size:
            raise RuntimeError(f"Page {index} dimensions changed: {source_size} -> {result_size}")
        if (source_page.extract_text() or "") != (result_page.extract_text() or ""):
            raise RuntimeError(f"Page {index} extracted text changed")

    result_counts = classify_annotations(result)
    added_counts = Counter(link.kind for link in added)
    for kind in ("internal", "external"):
        expected = existing_counts[kind] + added_counts[kind]
        if result_counts[kind] != expected:
            raise RuntimeError(f"Expected {expected} {kind} links, found {result_counts[kind]}")

    if not toc_pages and not args.no_toc_links:
        warnings.append("No TOC-like pages were detected. Use --toc-pages for unusual layouts.")
    if unresolved:
        warnings.append(f"{len(unresolved)} TOC rows could not be safely mapped.")
    if not added and not existing_counts["internal"] and not existing_counts["external"]:
        warnings.append("No link annotations were found or added.")
    if args.strict and warnings:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Strict mode failed: " + " ".join(warnings))

    report = {
        "input": str(input_path),
        "output": str(output_path),
        "output_mode": output_mode,
        "mode": mode,
        "pages": page_count,
        "toc_pages": [page + 1 for page in sorted(toc_pages)],
        "detected_page_offset": detected_offset,
        "offset_evidence_pages": offset_score,
        "existing_links": dict(existing_counts),
        "added_links": dict(added_counts),
        "final_links": dict(result_counts),
        "unresolved_toc_rows": unresolved,
        "warnings": warnings,
        "links": [asdict(link) for link in added],
    }
    report_path = None
    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
    elif output_mode == "folder":
        report_path = output_path.parent / f"{input_path.stem} - Link Report.json"
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_json"] = str(report_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Source PDF; never overwritten")
    parser.add_argument("--output", help="Output PDF (default: '<stem> - Interactive.pdf')")
    parser.add_argument(
        "--output-mode",
        choices=("root", "folder"),
        help="root: PDF beside source; folder: output directory with PDF and link report",
    )
    parser.add_argument("--toc-pages", help="Comma-separated 1-based TOC/agenda/index pages")
    parser.add_argument("--page-offset", type=int, help="Physical PDF page minus printed page label")
    parser.add_argument(
        "--reference-pdf",
        help="Same-layout interactive PDF whose working link annotations should be copied",
    )
    parser.add_argument("--password", help="Password for an encrypted source; output is not encrypted")
    parser.add_argument("--no-toc-links", action="store_true", help="Do not add internal navigation links")
    parser.add_argument("--no-url-links", action="store_true", help="Do not add visible URL/email links")
    parser.add_argument("--report-json", help="Write a detailed JSON detection/link report")
    parser.add_argument("--strict", action="store_true", help="Fail and remove output when warnings remain")
    parser.add_argument("--force", action="store_true", help="Replace an existing output file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = make_interactive(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in report.items() if key != "links"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
