#!/usr/bin/env python3
"""Add internal TOC links and visible URL links to an existing PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    Fit,
    IndirectObject,
    NumberObject,
    StreamObject,
)

from skill_provenance import require_isolated_runtime


TOC_HEADINGS = ("table of contents", "contents", "agenda", "index")
PAGE_TOKEN_RE = re.compile(r"^[\s.·•()\[\]-]*([0-9]{1,4}|[ivxlcdm]{1,10})[\s.·•()\[\]-]*$", re.I)
ORDINAL_TOKEN_RE = re.compile(r"^\s*([0-9]{1,4}|[ivxlcdm]{1,10})[.·):\-]+\s*$", re.I)
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
    label_kind: str
    rect: tuple[float, float, float, float]


@dataclass(frozen=True)
class AddedLink:
    kind: str
    source_page: int
    rect: tuple[float, float, float, float]
    target_page: int | None = None
    uri: str | None = None
    label: str | None = None
    title: str | None = None
    printed_label: str | None = None
    printed_number: int | None = None
    label_kind: str | None = None
    matched_by: str | None = None
    confidence: str | None = None
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PageLabelObservation:
    page_index: int
    printed_number: int
    label_kind: str
    raw: str


@dataclass(frozen=True)
class PaginationSegment:
    block_index: int
    label_kind: str
    printed_start: int
    printed_end: int
    offset: int
    evidence_pages: int
    density: float

    @property
    def physical_start(self) -> int:
        return self.printed_start + self.offset

    @property
    def physical_end(self) -> int:
        return self.printed_end + self.offset


@dataclass
class PageAnalysis:
    page_index: int
    width: float
    height: float
    words: list[dict]
    lines: list[list[dict]]
    normalized_text: str
    margin_labels: list[PageLabelObservation]
    ordinal_anchor_count: int


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
    if total <= 0 or int_to_roman(total) != value:
        return None
    return total


def int_to_roman(value: int) -> str:
    if value <= 0 or value > 3999:
        return ""
    numerals = (
        (1000, "M"),
        (900, "CM"),
        (500, "D"),
        (400, "CD"),
        (100, "C"),
        (90, "XC"),
        (50, "L"),
        (40, "XL"),
        (10, "X"),
        (9, "IX"),
        (5, "V"),
        (4, "IV"),
        (1, "I"),
    )
    result: list[str] = []
    remainder = value
    for number, token in numerals:
        count, remainder = divmod(remainder, number)
        result.extend([token] * count)
    return "".join(result)


def parse_page_label_details(value: str) -> tuple[int, str] | None:
    match = PAGE_TOKEN_RE.fullmatch(value)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token), "arabic"
    number = roman_to_int(token)
    return (number, "roman") if number is not None else None


def parse_page_label(value: str) -> int | None:
    parsed = parse_page_label_details(value)
    return parsed[0] if parsed else None


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


def toc_rows_for_page(page_index: int, page, words: Sequence[dict] | None = None) -> list[TocRow]:
    words = list(words) if words is not None else page.extract_words(
        use_text_flow=False, keep_blank_chars=False
    )
    rows: list[TocRow] = []
    for line in group_words_into_lines(words):
        if len(line) < 2:
            continue
        parsed = parse_page_label_details(str(line[-1]["text"]))
        if parsed is None or parsed[0] <= 0:
            continue
        label, label_kind = parsed
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
        rows.append(TocRow(page_index, title, str(line[-1]["text"]), label, label_kind, rect))
    return rows


def margin_label_observations(
    page_index: int, width: float, height: float, words: Sequence[dict], page_count: int
) -> list[PageLabelObservation]:
    observations: list[PageLabelObservation] = []
    seen: set[tuple[int, str]] = set()
    for word in words:
        top = float(word["top"])
        if not (top <= height * 0.08 or top >= height * 0.88):
            continue
        x0, x1 = float(word["x0"]), float(word["x1"])
        center = (x0 + x1) / 2
        if not (x0 <= width * 0.22 or x1 >= width * 0.78 or abs(center - width / 2) <= width * 0.12):
            continue
        parsed = parse_page_label_details(str(word["text"]))
        if parsed is None:
            continue
        number, label_kind = parsed
        if number <= 0 or number > max(100, page_count * 3):
            continue
        key = (number, label_kind)
        if key in seen:
            continue
        seen.add(key)
        observations.append(PageLabelObservation(page_index, number, label_kind, str(word["text"])))
    return observations


def ordinal_anchor_count(width: float, height: float, words: Sequence[dict]) -> int:
    anchors: list[float] = []
    for word in words:
        if float(word["x0"]) > width * 0.28:
            continue
        top = float(word["top"])
        if top <= height * 0.08 or top >= height * 0.9:
            continue
        if ORDINAL_TOKEN_RE.fullmatch(str(word["text"])):
            if not any(abs(top - existing) <= 3.0 for existing in anchors):
                anchors.append(top)
    return len(anchors)


def analyze_document(plumber_pdf) -> list[PageAnalysis]:
    page_count = len(plumber_pdf.pages)
    analyses: list[PageAnalysis] = []
    for page_index, page in enumerate(plumber_pdf.pages):
        if page_index and page_index % 100 == 0:
            print(f"Analyzed {page_index}/{page_count} pages...", file=sys.stderr)
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_words_into_lines(words)
        ordered_text = " ".join(str(word["text"]) for line in lines for word in line)
        width, height = float(page.width), float(page.height)
        analyses.append(
            PageAnalysis(
                page_index=page_index,
                width=width,
                height=height,
                words=words,
                lines=lines,
                normalized_text=normalize_text(ordered_text),
                margin_labels=margin_label_observations(
                    page_index, width, height, words, page_count
                ),
                ordinal_anchor_count=ordinal_anchor_count(width, height, words),
            )
        )
    return analyses


def detect_toc_pages(
    analyses: Sequence[PageAnalysis], explicit_pages: set[int] | None
) -> tuple[set[int], dict[int, list[TocRow]]]:
    rows_by_page: dict[int, list[TocRow]] = {}
    toc_pages: set[int] = set()
    for analysis in analyses:
        page_index = analysis.page_index
        class PageShape:
            width = analysis.width
            height = analysis.height

        rows = toc_rows_for_page(page_index, PageShape(), analysis.words)
        rows_by_page[page_index] = rows
        if explicit_pages is not None:
            if page_index in explicit_pages:
                toc_pages.add(page_index)
            continue
        has_heading = any(heading in analysis.normalized_text for heading in TOC_HEADINGS)
        if (has_heading and len(rows) >= 2) or len(rows) >= 6:
            toc_pages.add(page_index)
    if explicit_pages is None:
        toc_pages = expand_continuation_toc_pages(analyses, toc_pages)
    return toc_pages, rows_by_page


def contiguous_groups(pages: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(pages))
    if not ordered:
        return []
    groups: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for page in ordered[1:]:
        if page != previous + 1:
            groups.append((start, previous))
            start = page
        previous = page
    groups.append((start, previous))
    return groups


def infer_pagination_segments(
    analyses: Sequence[PageAnalysis], toc_pages: set[int]
) -> tuple[list[PaginationSegment], list[dict]]:
    blocks = contiguous_groups(toc_pages)
    segments: list[PaginationSegment] = []
    diagnostics: list[dict] = []
    page_count = len(analyses)
    for block_index, (block_start, block_end) in enumerate(blocks):
        region_start = block_end + 1
        region_end = blocks[block_index + 1][0] - 1 if block_index + 1 < len(blocks) else page_count - 1
        observations = [
            observation
            for analysis in analyses[region_start : region_end + 1]
            for observation in analysis.margin_labels
        ]
        for label_kind in ("roman", "arabic"):
            by_offset: dict[int, dict[int, PageLabelObservation]] = defaultdict(dict)
            for observation in observations:
                if observation.label_kind != label_kind:
                    continue
                offset = (observation.page_index + 1) - observation.printed_number
                by_offset[offset][observation.page_index] = observation
            if not by_offset:
                continue
            dominant = max(len(items) for items in by_offset.values())
            minimum_support = 2 if region_end - region_start < 8 else 3
            retained = [
                (offset, list(items.values()))
                for offset, items in by_offset.items()
                if len(items) >= minimum_support
                and (len(items) >= max(minimum_support, int(dominant * 0.04)) or len(items) >= 8)
            ]
            for offset, items in retained:
                ordered = sorted(items, key=lambda item: item.page_index)
                clusters: list[list[PageLabelObservation]] = [[ordered[0]]]
                for observation in ordered[1:]:
                    if observation.page_index - clusters[-1][-1].page_index > 24:
                        clusters.append([observation])
                    else:
                        clusters[-1].append(observation)
                for cluster in clusters:
                    if len(cluster) < minimum_support:
                        continue
                    labels = sorted({item.printed_number for item in cluster})
                    span = labels[-1] - labels[0] + 1
                    density = len(labels) / span if span else 1.0
                    if len(cluster) < 8 and density < 0.2:
                        continue
                    segments.append(
                        PaginationSegment(
                            block_index=block_index,
                            label_kind=label_kind,
                            printed_start=labels[0],
                            printed_end=labels[-1],
                            offset=offset,
                            evidence_pages=len(cluster),
                            density=round(density, 4),
                        )
                    )
        diagnostics.append(
            {
                "block_index": block_index,
                "toc_pages": [page + 1 for page in range(block_start, block_end + 1)],
                "content_region": [region_start + 1, region_end + 1],
                "margin_observations": len(observations),
            }
        )
    segments.sort(
        key=lambda item: (item.block_index, item.label_kind, item.printed_start, item.offset)
    )
    return segments, diagnostics


def title_match_score(title: str, normalized_page: str) -> float:
    normalized_title = normalize_text(title)
    if len(normalized_title) < 5:
        return 0.0
    if normalized_title in normalized_page:
        return 1.0
    tokens = [token for token in normalized_title.split() if token not in STOPWORDS and len(token) > 2]
    if len(tokens) < 2:
        return 0.0
    page_tokens = set(normalized_page.split())
    return len(set(tokens) & page_tokens) / len(set(tokens))


def unique_title_destination(
    title: str,
    normalized_pages: Sequence[str],
    excluded_pages: set[int],
    allowed_pages: set[int] | None = None,
) -> tuple[int | None, float]:
    normalized_title = normalize_text(title)
    if len(normalized_title) < 5:
        return None, 0.0
    exact = [
        index
        for index, text in enumerate(normalized_pages)
        if index not in excluded_pages
        and (allowed_pages is None or index in allowed_pages)
        and normalized_title in text
    ]
    if len(exact) == 1:
        return exact[0], 1.0
    scores: list[tuple[float, int]] = []
    for index, text in enumerate(normalized_pages):
        if index in excluded_pages or (allowed_pages is not None and index not in allowed_pages):
            continue
        score = title_match_score(title, text)
        scores.append((score, index))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.85:
        return None, scores[0][0] if scores else 0.0
    if len(scores) > 1 and abs(scores[0][0] - scores[1][0]) < 0.05:
        return None, scores[0][0]
    return scores[0][1], scores[0][0]


def likely_toc_line_count(analysis: PageAnalysis) -> int:
    count = 0
    for line in analysis.lines:
        if len(line) < 2 or float(line[-1]["x0"]) < analysis.width * 0.55:
            continue
        title = normalize_text(" ".join(str(word["text"]) for word in line[:-1]))
        if len(title) < 3 or title in TOC_HEADINGS:
            continue
        raw_label = str(line[-1]["text"])
        compact = re.sub(r"[^A-Za-z]", "", raw_label)
        roman_like = (
            0 < len(compact) <= 6
            and sum(char.upper() in "IVXLCDM" for char in compact) >= len(compact) - 1
        )
        if (
            parse_page_label_details(raw_label) is not None
            or any(char.isdigit() for char in raw_label)
            or roman_like
        ):
            count += 1
    return count


def expand_continuation_toc_pages(
    analyses: Sequence[PageAnalysis], toc_pages: set[int]
) -> set[int]:
    expanded = set(toc_pages)
    changed = True
    while changed:
        changed = False
        for page_index, analysis in enumerate(analyses):
            if page_index in expanded:
                continue
            adjacent = page_index - 1 in expanded or page_index + 1 in expanded
            if adjacent and max(likely_toc_line_count(analysis), analysis.ordinal_anchor_count) >= 5:
                expanded.add(page_index)
                changed = True
    return expanded


def toc_completeness_diagnostics(
    analyses: Sequence[PageAnalysis],
    toc_pages: set[int],
    rows_by_page: dict[int, list[TocRow]],
) -> tuple[list[dict], int]:
    diagnostics: list[dict] = []
    suspected_total = 0
    for page_index in sorted(toc_pages):
        analysis = analyses[page_index]
        parsed = len(rows_by_page.get(page_index, []))
        candidate_lines = likely_toc_line_count(analysis)
        expected = max(parsed, candidate_lines, analysis.ordinal_anchor_count)
        suspected = max(0, expected - parsed)
        suspected_total += suspected
        diagnostics.append(
            {
                "page": page_index + 1,
                "parsed_rows": parsed,
                "candidate_lines": candidate_lines,
                "ordinal_anchors": analysis.ordinal_anchor_count,
                "suspected_unparsed_rows": suspected,
            }
        )
    return diagnostics, suspected_total


def block_index_for_page(source_page: int, blocks: Sequence[tuple[int, int]]) -> int | None:
    for index, (start, end) in enumerate(blocks):
        if start <= source_page <= end:
            return index
    return None


def block_content_pages(
    block_index: int, blocks: Sequence[tuple[int, int]], page_count: int
) -> set[int]:
    start = blocks[block_index][1] + 1
    end = blocks[block_index + 1][0] - 1 if block_index + 1 < len(blocks) else page_count - 1
    return set(range(start, end + 1))


def trusted_margin_numbers(
    analysis: PageAnalysis,
    block_index: int,
    label_kind: str,
    segments: Sequence[PaginationSegment],
) -> list[int]:
    offsets = {
        segment.offset
        for segment in segments
        if segment.block_index == block_index and segment.label_kind == label_kind
    }
    return sorted(
        {
            observation.printed_number
            for observation in analysis.margin_labels
            if observation.label_kind == label_kind
            and (analysis.page_index + 1) - observation.printed_number in offsets
        }
    )


def resolve_toc_row(
    row: TocRow,
    block_index: int,
    blocks: Sequence[tuple[int, int]],
    segments: Sequence[PaginationSegment],
    analyses: Sequence[PageAnalysis],
    normalized_pages: Sequence[str],
    toc_pages: set[int],
) -> tuple[int | None, str | None, str, dict]:
    page_count = len(analyses)
    candidates: list[tuple[PaginationSegment, int]] = []
    for segment in segments:
        if (
            segment.block_index == block_index
            and segment.label_kind == row.label_kind
            and segment.printed_start - 4 <= row.printed_number <= segment.printed_end + 4
        ):
            destination = row.printed_number + segment.offset - 1
            if 0 <= destination < page_count:
                candidates.append((segment, destination))

    scored: list[tuple[float, bool, bool, PaginationSegment, int, list[int]]] = []
    for segment, destination in candidates:
        observed = trusted_margin_numbers(
            analyses[destination], block_index, row.label_kind, segments
        )
        exact_label = row.printed_number in observed
        conflicting_label = bool(observed) and not exact_label
        title_score = title_match_score(row.title, normalized_pages[destination])
        score = (2.0 if exact_label else 0.0) + title_score + min(0.5, segment.evidence_pages / 20)
        scored.append((score, exact_label, conflicting_label, segment, destination, observed))
    scored.sort(key=lambda item: (-item[0], -item[3].evidence_pages, item[4]))
    viable = [item for item in scored if not item[2]]
    if viable:
        chosen = viable[0]
        if len(viable) > 1 and abs(viable[0][0] - viable[1][0]) < 0.25:
            return None, None, "low", {
                "reason": "multiple pagination segments map this label",
                "candidate_target_pages": [item[4] + 1 for item in viable],
            }
        _, exact_label, _, segment, destination, observed = chosen
        confidence = "high" if exact_label or title_match_score(row.title, normalized_pages[destination]) >= 0.9 else "medium"
        return destination, "pagination-segment", confidence, {
            "block_index": block_index,
            "offset": segment.offset,
            "segment_printed_range": [segment.printed_start, segment.printed_end],
            "segment_evidence_pages": segment.evidence_pages,
            "segment_extrapolated": not (
                segment.printed_start <= row.printed_number <= segment.printed_end
            ),
            "target_margin_labels": observed,
            "target_title_score": round(title_match_score(row.title, normalized_pages[destination]), 4),
        }
    if scored:
        return None, None, "low", {
            "reason": "candidate destination has a conflicting printed page label",
            "candidate_target_pages": [item[4] + 1 for item in scored],
            "observed_labels": [item[5] for item in scored],
        }

    allowed_pages = block_content_pages(block_index, blocks, page_count)
    destination, score = unique_title_destination(
        row.title, normalized_pages, toc_pages, allowed_pages=allowed_pages
    )
    if destination is not None:
        observed = trusted_margin_numbers(
            analyses[destination], block_index, row.label_kind, segments
        )
        if observed and row.printed_number not in observed:
            return None, None, "low", {
                "reason": "title match conflicts with destination page label",
                "candidate_target_page": destination + 1,
                "target_margin_labels": observed,
                "target_title_score": round(score, 4),
            }
        return destination, "unique-title", "high" if score >= 0.95 else "medium", {
            "block_index": block_index,
            "target_margin_labels": observed,
            "target_title_score": round(score, 4),
        }
    return None, None, "low", {"reason": "no safe pagination or unique title match"}


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


def resolved_pdf_object(value):
    return value.get_object() if hasattr(value, "get_object") else value


def mapping_value(value, key: str, default=None):
    resolved = resolved_pdf_object(value)
    return resolved.get(key, default) if isinstance(resolved, Mapping) else default


def annotation_kind(annotation) -> str:
    action = resolved_pdf_object(annotation.get("/A"))
    if "/Dest" in annotation or (
        isinstance(action, Mapping) and action.get("/S") == "/GoTo"
    ):
        return "internal"
    if isinstance(action, Mapping) and action.get("/S") == "/URI":
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


def normalized_uri(raw: str, *, allow_bare_domains: bool = False) -> str | None:
    value = raw.strip().lstrip(LEADING_URL_PUNCTUATION).rstrip(TRAILING_URL_PUNCTUATION)
    if not value:
        return None
    if EMAIL_RE.fullmatch(value):
        return f"mailto:{value}"
    if URL_RE.fullmatch(value):
        return value if value.lower().startswith(("http://", "https://")) else f"https://{value}"
    if allow_bare_domains and DOMAIN_RE.fullmatch(value):
        tld = value.split("/", 1)[0].split(":", 1)[0].rsplit(".", 1)[-1].lower()
        if tld in BLOCKED_FILE_TLDS:
            return None
        return f"https://{value}"
    return None


def supported_uri(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "ftp"}:
        return bool(parsed.netloc)
    return parsed.scheme == "mailto" and "@" in parsed.path


def visible_url_candidates(
    analyses: Sequence[PageAnalysis], *, allow_bare_domains: bool
) -> Iterable[AddedLink]:
    for analysis in analyses:
        for word in analysis.words:
            raw = str(word["text"])
            uri = normalized_uri(raw, allow_bare_domains=allow_bare_domains)
            if uri is None:
                continue
            rect = (
                float(word["x0"]),
                analysis.height - float(word["bottom"]),
                float(word["x1"]),
                analysis.height - float(word["top"]),
            )
            yield AddedLink("external", analysis.page_index, rect, uri=uri, label=raw)


def recoverable_broken_uri_candidates(
    reader: PdfReader, *, allow_bare_domains: bool
) -> Iterable[AddedLink]:
    for page_index, page in enumerate(reader.pages):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            if annotation.get("/Subtype") != "/Link" or annotation_kind(annotation) != "other":
                continue
            uri = normalized_uri(
                str(annotation.get("/Contents", "")),
                allow_bare_domains=allow_bare_domains,
            )
            if uri is None or "/Rect" not in annotation:
                continue
            rect = tuple(float(value) for value in annotation["/Rect"])
            yield AddedLink("external", page_index, rect, uri=uri, label="recovered annotation metadata")


def destination_page_index(reader: PdfReader, destination) -> int | None:
    if destination is None:
        return None
    if isinstance(destination, str):
        named = reader.named_destinations.get(destination)
        if named is None:
            return None
        try:
            return reader.get_destination_page_number(named)
        except Exception:
            return None
    target = destination[0] if isinstance(destination, (list, tuple, ArrayObject)) else destination
    if isinstance(target, int):
        value = int(target)
        return value if 0 <= value < len(reader.pages) else None
    if hasattr(target, "idnum"):
        for index, page in enumerate(reader.pages):
            reference = page.indirect_reference
            if (
                reference is not None
                and reference.idnum == target.idnum
                and reference.generation == getattr(target, "generation", reference.generation)
            ):
                return index
    indirect = getattr(target, "indirect_reference", None)
    if indirect is not None:
        return destination_page_index(reader, indirect)
    return None


def annotation_rect_error(page, raw_rect) -> str | None:
    if not isinstance(raw_rect, (list, tuple, ArrayObject)) or len(raw_rect) != 4:
        return "missing or malformed /Rect"
    try:
        x0, y0, x1, y1 = (float(value) for value in raw_rect)
    except (TypeError, ValueError):
        return "non-numeric /Rect"
    if not all(math.isfinite(value) for value in (x0, y0, x1, y1)):
        return "non-finite /Rect"
    if x1 <= x0 or y1 <= y0:
        return "empty or reversed /Rect"
    left = float(page.mediabox.left)
    bottom = float(page.mediabox.bottom)
    right = float(page.mediabox.right)
    top = float(page.mediabox.top)
    tolerance = 0.01
    if x0 < left - tolerance or y0 < bottom - tolerance or x1 > right + tolerance or y1 > top + tolerance:
        return "out-of-page /Rect"
    return None


def link_annotation_errors(reader: PdfReader) -> list[str]:
    errors: list[str] = []
    for page_index, page in enumerate(reader.pages):
        for annotation_index, ref in enumerate(page.get("/Annots", []), start=1):
            annotation = resolved_pdf_object(ref)
            if not isinstance(annotation, Mapping) or annotation.get("/Subtype") != "/Link":
                continue
            kind = annotation_kind(annotation)
            if kind == "other":
                continue
            prefix = f"page {page_index + 1} link {annotation_index}"
            rect_error = annotation_rect_error(page, annotation.get("/Rect"))
            if rect_error:
                errors.append(f"{prefix}: {rect_error}")
                continue
            action = resolved_pdf_object(annotation.get("/A"))
            if kind == "internal":
                destination = annotation.get("/Dest")
                if destination is None:
                    destination = mapping_value(action, "/D")
                if destination_page_index(reader, destination) is None:
                    errors.append(f"{prefix}: unresolved internal destination")
            else:
                uri = mapping_value(action, "/URI")
                if uri is None or not supported_uri(str(uri)):
                    errors.append(f"{prefix}: invalid external URI")
    return errors


def overlapping_existing_link_semantics(
    reader: PdfReader, page_index: int, rect: Sequence[float]
) -> list[dict]:
    overlaps: list[dict] = []
    for ref in reader.pages[page_index].get("/Annots", []):
        annotation = resolved_pdf_object(ref)
        if not isinstance(annotation, Mapping) or annotation.get("/Subtype") != "/Link":
            continue
        annotation_rect = annotation.get("/Rect")
        if not isinstance(annotation_rect, (list, tuple, ArrayObject)) or len(annotation_rect) != 4:
            continue
        try:
            normalized_rect = tuple(float(value) for value in annotation_rect)
        except (TypeError, ValueError):
            continue
        if rect_overlap_ratio(rect, normalized_rect) < 0.8:
            continue
        kind = annotation_kind(annotation)
        semantics: dict = {"kind": kind, "rect": list(normalized_rect)}
        action = resolved_pdf_object(annotation.get("/A"))
        if kind == "internal":
            destination = annotation.get("/Dest")
            if destination is None:
                destination = mapping_value(action, "/D")
            semantics["target_page"] = destination_page_index(reader, destination)
        elif kind == "external":
            uri = mapping_value(action, "/URI")
            semantics["uri"] = str(uri) if uri is not None else None
        overlaps.append(semantics)
    return overlaps


def replay_link_matches_existing(link: AddedLink, existing: dict) -> bool:
    if existing.get("kind") != link.kind:
        return False
    if link.kind == "internal":
        return existing.get("target_page") == link.target_page
    return existing.get("uri") == link.uri


def replay_overlap_status(
    reader: PdfReader,
    link: AddedLink,
    occupied: dict[int, list[tuple[float, float, float, float]]],
) -> tuple[str, list[dict]]:
    existing = overlapping_existing_link_semantics(
        reader, link.source_page, link.rect
    )
    if existing:
        if len(existing) == 1 and replay_link_matches_existing(link, existing[0]):
            return "covered", existing
        return "conflict", existing
    if any(
        rect_overlap_ratio(link.rect, rect) >= 0.8
        for rect in occupied[link.source_page]
    ):
        return "conflict", [{"kind": "planned-or-unknown-overlap"}]
    return "add", []


def replay_conflict_record(link: AddedLink, existing: list[dict]) -> dict:
    expected = {"kind": link.kind}
    if link.kind == "internal":
        expected["target_page"] = link.target_page
    else:
        expected["uri"] = link.uri
    return {
        "source_page": link.source_page + 1,
        "rect": list(link.rect),
        "label": link.label,
        "reason": "overlapping existing or planned link is not semantically identical",
        "expected": expected,
        "overlapping_links": existing,
    }


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
            action = resolved_pdf_object(annotation.get("/A"))
            if kind == "internal":
                destination = annotation.get("/Dest")
                if destination is None:
                    destination = mapping_value(action, "/D")
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
                uri = mapping_value(action, "/URI")
                if uri is None:
                    warnings.append(f"Could not resolve reference URI on page {page_index + 1}")
                    continue
                links.append(
                    AddedLink(
                        "external",
                        page_index,
                        rect,
                        uri=str(uri),
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


def planned_output_paths(
    args: argparse.Namespace, input_path: Path
) -> tuple[str, Path, Path | None]:
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
    if args.report_json:
        report_path = Path(args.report_json).expanduser().resolve()
    elif output_mode == "folder":
        report_path = output_path.parent / f"{input_path.stem} - Link Report.json"
    else:
        report_path = None
    return output_mode, output_path, report_path


@contextmanager
def destination_locks(paths: Iterable[Path | None]):
    canonical_paths = sorted(
        {
            os.path.normcase(os.path.realpath(path))
            for path in paths
            if path is not None
        }
    )
    lock_root = Path(tempfile.gettempdir()) / "make-interactive-pdfs-locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    handles = []
    try:
        for canonical_path in canonical_paths:
            lock_name = (
                hashlib.sha256(
                    b"destination\0" + canonical_path.encode("utf-8")
                ).hexdigest()
                + ".lock"
            )
            handle = (lock_root / lock_name).open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 300.0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as exc:
                        if time.monotonic() >= deadline:
                            handle.close()
                            raise TimeoutError(
                                f"Timed out waiting for destination lock: {canonical_path}"
                            ) from exc
                        time.sleep(0.1)
            else:
                import fcntl

                deadline = time.monotonic() + 300.0
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as exc:
                        if time.monotonic() >= deadline:
                            handle.close()
                            raise TimeoutError(
                                f"Timed out waiting for destination lock: {canonical_path}"
                            ) from exc
                        time.sleep(0.1)
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


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


def canonical_pdf_object_digest(
    value,
    cache: dict[tuple[int, int, int], bytes],
    active: set[tuple[int, int, int]] | None = None,
) -> bytes:
    active = active if active is not None else set()
    if isinstance(value, IndirectObject):
        key = (id(value.pdf), int(value.idnum), int(value.generation))
        if key in cache:
            return b"R" + cache[key]
        if key in active:
            return b"CYCLE"
        active.add(key)
        digest = hashlib.sha256(
            canonical_pdf_object_digest(value.get_object(), cache, active)
        ).digest()
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
            digest.update(canonical_pdf_object_digest(value[key], cache, active))
        digest.update(hashlib.sha256(stream_data).digest())
        return digest.digest()
    if isinstance(value, DictionaryObject):
        digest = hashlib.sha256(b"DICT")
        for key in sorted(value, key=str):
            digest.update(str(key).encode("utf-8", errors="backslashreplace"))
            digest.update(canonical_pdf_object_digest(value[key], cache, active))
        return digest.digest()
    if isinstance(value, (ArrayObject, list, tuple)):
        digest = hashlib.sha256(b"ARRAY")
        for item in value:
            digest.update(canonical_pdf_object_digest(item, cache, active))
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


def visual_resource_state(reader: PdfReader) -> dict:
    cache: dict[tuple[int, int, int], bytes] = {}
    page_digests: list[str] = []
    for page in reader.pages:
        digest = hashlib.sha256()
        for key, value in (
            ("/Resources", page.get_inherited("/Resources")),
            ("/Group", page.get("/Group")),
            ("/UserUnit", page.get("/UserUnit")),
        ):
            digest.update(key.encode("ascii"))
            digest.update(canonical_pdf_object_digest(value, cache))
        page_digests.append(digest.hexdigest())
    root = resolved_pdf_object(reader.trailer.get("/Root"))
    catalog_digest = hashlib.sha256()
    for key in ("/OCProperties", "/OutputIntents"):
        catalog_digest.update(key.encode("ascii"))
        catalog_digest.update(
            canonical_pdf_object_digest(mapping_value(root, key), cache)
        )
    return {
        "pages": page_digests,
        "catalog": catalog_digest.hexdigest(),
    }


def paths_collide(first: Path, second: Path) -> bool:
    if first == second:
        return True
    if first.exists() and second.exists():
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False
    return False


def reject_path_collisions(named_paths: dict[str, Path | None]) -> None:
    present = [(name, path) for name, path in named_paths.items() if path is not None]
    for index, (first_name, first_path) in enumerate(present):
        for second_name, second_path in present[index + 1 :]:
            if paths_collide(first_path, second_path):
                raise ValueError(
                    f"Path collision: {first_name} and {second_name} refer to {first_path}"
                )


def reject_non_file_destinations(named_paths: dict[str, Path | None]) -> None:
    for name, path in named_paths.items():
        if path is None or not os.path.lexists(path):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{name} must be a regular file path, not: {path}")


def sibling_temporary_path(final_path: Path, suffix: str) -> Path:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{final_path.name}.", suffix=suffix, dir=final_path.parent
    )
    os.close(descriptor)
    return Path(raw_path)


def atomic_publish(staged: Sequence[tuple[Path, Path]], *, force: bool) -> None:
    backups: list[tuple[Path, Path]] = []
    published: list[Path] = []
    try:
        for temporary, final in staged:
            if not temporary.is_file():
                raise RuntimeError(f"Staged artifact is missing: {temporary}")
            reject_non_file_destinations({"Output destination": final})
            if final.exists():
                if not force:
                    raise FileExistsError(f"Output exists; pass --force to replace it: {final}")
                backup = sibling_temporary_path(final, ".backup")
                backup.unlink()
                os.replace(final, backup)
                backups.append((final, backup))
            os.replace(temporary, final)
            published.append(final)
    except BaseException:
        for final in reversed(published):
            final.unlink(missing_ok=True)
        for final, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, final)
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)
        raise
    else:
        for _, backup in backups:
            backup.unlink(missing_ok=True)


def load_link_manifest(
    manifest_path: Path,
    input_path: Path,
    page_count: int,
    *,
    allow_legacy: bool,
) -> tuple[list[AddedLink], dict]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON link manifest: {manifest_path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        raise ValueError("Link manifest must be an object containing a links array")
    schema_version = data.get("schema_version")
    explicitly_reviewed = data.get("reviewed") is True
    if schema_version == 2:
        if data.get("status") != "PASS" and not explicitly_reviewed:
            raise ValueError(
                "A NEEDS_REVIEW manifest must be corrected and marked reviewed=true before replay"
            )
    elif not allow_legacy:
        raise ValueError(
            "Legacy manifests have no enforceable review status; inspect the file and pass "
            "--allow-legacy-manifest only when its mappings are already approved"
        )
    declared_input_hash = data.get("input_sha256")
    declared_legacy_hash = data.get("source_sha256")
    if declared_input_hash is not None and declared_legacy_hash is not None:
        if str(declared_input_hash).casefold() != str(declared_legacy_hash).casefold():
            raise ValueError("Link manifest input_sha256 and source_sha256 disagree")
    if schema_version == 2:
        if declared_input_hash is None:
            raise ValueError("Schema-v2 link manifest is missing required input_sha256")
        expected_hash = declared_input_hash
        hash_field = "input_sha256"
    elif declared_input_hash is not None:
        expected_hash = declared_input_hash
        hash_field = "input_sha256"
    elif declared_legacy_hash is not None:
        expected_hash = declared_legacy_hash
        hash_field = "source_sha256"
    else:
        expected_hash = None
        hash_field = None
    if expected_hash is not None and (
        not isinstance(expected_hash, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash) is None
    ):
        raise ValueError(f"Link manifest {hash_field} is not a valid SHA-256 digest")
    actual_hash = sha256_file(input_path)
    if expected_hash is not None and expected_hash.casefold() != actual_hash:
        raise ValueError(f"Link manifest {hash_field} does not match the input PDF")
    links: list[AddedLink] = []
    for position, raw in enumerate(data["links"], start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Manifest link {position} is not an object")
        kind = str(raw.get("kind", ""))
        if kind not in {"internal", "external"}:
            raise ValueError(f"Manifest link {position} has unsupported kind {kind!r}")
        source_page = raw.get("source_page_index", raw.get("source_page"))
        if not isinstance(source_page, int) or not 0 <= source_page < page_count:
            raise ValueError(f"Manifest link {position} has an invalid zero-based source page")
        rect = raw.get("rect")
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError(f"Manifest link {position} has an invalid rectangle")
        rectangle = tuple(float(value) for value in rect)
        if rectangle[2] <= rectangle[0] or rectangle[3] <= rectangle[1]:
            raise ValueError(f"Manifest link {position} has an empty rectangle")
        target_page = raw.get("target_page_index", raw.get("target_page"))
        uri = raw.get("uri")
        if kind == "internal":
            if not isinstance(target_page, int) or not 0 <= target_page < page_count:
                raise ValueError(f"Manifest link {position} has an invalid zero-based target page")
            uri = None
        else:
            if not isinstance(uri, str) or not uri:
                raise ValueError(f"Manifest link {position} has no URI")
            if not supported_uri(uri):
                raise ValueError(f"Manifest link {position} has an invalid URI")
        links.append(
            AddedLink(
                kind=kind,
                source_page=source_page,
                rect=rectangle,
                target_page=target_page if kind == "internal" else None,
                uri=uri,
                label=raw.get("label"),
                title=raw.get("title"),
                printed_label=raw.get("printed_label"),
                printed_number=raw.get("printed_number"),
                label_kind=raw.get("label_kind"),
                matched_by="reviewed-manifest",
                confidence="reviewed",
                evidence={"manifest": str(manifest_path)},
            )
        )
    return links, {
        "path": str(manifest_path),
        "input_sha256": actual_hash,
        "source_sha256": actual_hash,
        "hash_field": hash_field,
        "legacy_hash_field": hash_field == "source_sha256",
        "declared_schema_version": data.get("schema_version"),
        "explicitly_reviewed": explicitly_reviewed,
        "legacy_override": schema_version != 2,
        "reviewed_links": len(links),
    }


def pdf_version_manifest(reader: PdfReader) -> dict[str, str | None]:
    root = reader.trailer.get("/Root")
    if hasattr(root, "get_object"):
        root = root.get_object()
    catalog_version = root.get("/Version") if root else None
    return {
        "header": reader.pdf_header,
        "catalog": str(catalog_version) if catalog_version is not None else None,
    }


def pdf_has_signatures(reader: PdfReader) -> bool:
    root = reader.trailer.get("/Root")
    if hasattr(root, "get_object"):
        root = root.get_object()
    if not root:
        return False
    if root.get("/Perms") is not None:
        return True
    form = resolved_pdf_object(root.get("/AcroForm"))
    if not isinstance(form, Mapping):
        return False
    fields = resolved_pdf_object(form.get("/Fields", []))
    pending = list(fields) if isinstance(fields, (list, tuple, ArrayObject)) else []
    while pending:
        field = resolved_pdf_object(pending.pop())
        if not isinstance(field, Mapping):
            continue
        value = resolved_pdf_object(field.get("/V"))
        if field.get("/FT") == "/Sig" or (
            isinstance(value, Mapping) and value.get("/Type") == "/Sig"
        ):
            return True
        kids = resolved_pdf_object(field.get("/Kids", []))
        if isinstance(kids, (list, tuple, ArrayObject)):
            pending.extend(kids)
    return False


def unsupported_pdf_features(reader: PdfReader) -> list[str]:
    root = reader.trailer.get("/Root")
    if hasattr(root, "get_object"):
        root = root.get_object()
    if not root:
        return []
    features: list[str] = []
    if root.get("/Collection") is not None:
        features.append("PDF portfolio/collection")
    form = resolved_pdf_object(root.get("/AcroForm"))
    if isinstance(form, Mapping) and form.get("/XFA") is not None:
        features.append("XFA form")
    return features


def existing_internal_targets(
    reader: PdfReader, page_index: int, rect: Sequence[float]
) -> list[int]:
    targets: list[int] = []
    for ref in reader.pages[page_index].get("/Annots", []):
        annotation = ref.get_object()
        if annotation.get("/Subtype") != "/Link" or annotation_kind(annotation) != "internal":
            continue
        annotation_rect = annotation.get("/Rect")
        if not annotation_rect or rect_overlap_ratio(rect, annotation_rect) < 0.8:
            continue
        action = resolved_pdf_object(annotation.get("/A"))
        destination = annotation.get("/Dest")
        if destination is None:
            destination = mapping_value(action, "/D")
        target = destination_page_index(reader, destination)
        if target is not None:
            targets.append(target)
    return targets


def _make_interactive(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    timings: dict[str, float] = {}
    runtime_provenance = require_isolated_runtime()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    output_mode, output_path, report_path = planned_output_paths(args, input_path)
    reference_path = (
        Path(args.reference_pdf).expanduser().resolve() if args.reference_pdf else None
    )
    manifest_path = (
        Path(args.link_manifest).expanduser().resolve() if args.link_manifest else None
    )
    if reference_path and manifest_path:
        raise ValueError("Use either --reference-pdf or --link-manifest, not both")
    reject_path_collisions(
        {
            "source": input_path,
            "output": output_path,
            "report": report_path,
            "reference": reference_path,
            "link manifest": manifest_path,
        }
    )
    reject_non_file_destinations({"Output": output_path, "Report": report_path})
    if output_path.exists() and not args.force:
        raise FileExistsError(f"Output exists; pass --force to replace it: {output_path}")
    if report_path and report_path.exists() and not args.force:
        raise FileExistsError(f"Report exists; pass --force to replace it: {report_path}")
    if reference_path and not reference_path.is_file():
        raise FileNotFoundError(reference_path)
    if manifest_path and not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)

    read_started = time.perf_counter()
    reader = PdfReader(input_path, strict=False)
    if reader.is_encrypted:
        if not args.password or not reader.decrypt(args.password):
            raise ValueError("Encrypted PDF requires a valid --password")
    if pdf_has_signatures(reader) and not args.allow_signature_invalidation:
        raise ValueError(
            "The source contains a digital signature. Adding links invalidates signatures; "
            "pass --allow-signature-invalidation only with explicit user approval."
        )
    unsupported_features = unsupported_pdf_features(reader)
    if unsupported_features:
        raise ValueError(
            "Unsupported PDF feature requires a specialized workflow: "
            + ", ".join(unsupported_features)
        )
    page_count = len(reader.pages)
    source_link_errors = link_annotation_errors(reader)
    if source_link_errors:
        preview = "; ".join(source_link_errors[:10])
        remainder = len(source_link_errors) - 10
        suffix = f"; plus {remainder} more" if remainder > 0 else ""
        raise ValueError(f"Source contains invalid link annotations: {preview}{suffix}")
    input_sha256 = sha256_file(input_path)
    timings["read_source_seconds"] = round(time.perf_counter() - read_started, 4)
    existing_counts = classify_annotations(reader)
    occupied = existing_link_rects(reader)
    added: list[AddedLink] = []
    unresolved: list[dict] = []
    warnings: list[str] = []
    review_reasons: list[str] = []
    toc_pages: set[int] = set()
    detected_offset: int | None = None
    offset_score = 0
    mode = "automatic-analysis"
    pagination_segments: list[PaginationSegment] = []
    pagination_diagnostics: list[dict] = []
    completeness_diagnostics: list[dict] = []
    suspected_unparsed_rows = 0
    parsed_toc_rows = 0
    covered_existing_rows = 0
    covered_replay_links: list[AddedLink] = []
    replay_conflicts: list[dict] = []
    rotated_automatic_url_pages: set[int] = set()
    skipped_bare_domain_candidates = 0
    manifest_input: dict | None = None
    reference_sha256: str | None = None

    analysis_started = time.perf_counter()
    if reference_path:
        mode = "reference-copy"
        reference_sha256 = sha256_file(reference_path)
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
            source_crop = tuple(float(value) for value in source_page.cropbox)
            reference_crop = tuple(float(value) for value in reference_page.cropbox)
            if any(abs(a - b) > 0.05 for a, b in zip(source_crop, reference_crop, strict=True)):
                raise ValueError(f"Reference PDF page {index} crop box differs")
            if int(source_page.get("/Rotate", 0) or 0) != int(reference_page.get("/Rotate", 0) or 0):
                raise ValueError(f"Reference PDF page {index} rotation differs")
        candidates, reference_warnings = reference_link_candidates(reference)
        warnings.extend(reference_warnings)
        review_reasons.extend(reference_warnings)
        toc_pages = {link.source_page for link in candidates if link.kind == "internal"}
        for link in candidates:
            if link.kind == "internal" and args.no_toc_links:
                continue
            if link.kind == "external" and args.no_url_links:
                continue
            overlap_status, overlap_evidence = replay_overlap_status(reader, link, occupied)
            if overlap_status == "covered":
                covered_existing_rows += 1
                covered_replay_links.append(link)
                continue
            if overlap_status == "conflict":
                replay_conflicts.append(replay_conflict_record(link, overlap_evidence))
                continue
            added.append(link)
            occupied[link.source_page].append(link.rect)
    elif manifest_path:
        mode = "reviewed-manifest"
        candidates, manifest_input = load_link_manifest(
            manifest_path,
            input_path,
            page_count,
            allow_legacy=args.allow_legacy_manifest,
        )
        for link in candidates:
            if link.kind == "internal" and args.no_toc_links:
                continue
            if link.kind == "external" and args.no_url_links:
                continue
            overlap_status, overlap_evidence = replay_overlap_status(reader, link, occupied)
            if overlap_status == "covered":
                covered_existing_rows += 1
                covered_replay_links.append(link)
                continue
            if overlap_status == "conflict":
                replay_conflicts.append(replay_conflict_record(link, overlap_evidence))
                continue
            added.append(link)
            occupied[link.source_page].append(link.rect)
    else:
        with pdfplumber.open(input_path, password=args.password) as plumber_pdf:
            analyses = analyze_document(plumber_pdf)
            explicit_toc_pages = parse_page_list(args.toc_pages, page_count)
            toc_pages, rows_by_page = detect_toc_pages(analyses, explicit_toc_pages)
            geometry_review_pages = [
                page_index + 1
                for page_index in sorted(toc_pages)
                if int(reader.pages[page_index].get("/Rotate", 0) or 0) % 360 != 0
            ]
            if geometry_review_pages:
                message = (
                    "Rotated navigation pages require reviewed annotation coordinates: "
                    + ", ".join(str(page) for page in geometry_review_pages)
                )
                warnings.append(message)
                review_reasons.append(message)
            pagination_segments, pagination_diagnostics = infer_pagination_segments(
                analyses, toc_pages
            )
            blocks = contiguous_groups(toc_pages)
            if args.page_offset is not None:
                detected_offset = args.page_offset
                pagination_segments = []
                for block_index, (block_start, block_end) in enumerate(blocks):
                    block_rows = [
                        row
                        for source_page in range(block_start, block_end + 1)
                        for row in rows_by_page[source_page]
                    ]
                    for label_kind in {row.label_kind for row in block_rows}:
                        numbers = [
                            row.printed_number for row in block_rows if row.label_kind == label_kind
                        ]
                        if numbers:
                            pagination_segments.append(
                                PaginationSegment(
                                    block_index,
                                    label_kind,
                                    min(numbers),
                                    max(numbers),
                                    args.page_offset,
                                    0,
                                    1.0,
                                )
                            )
            unique_offsets = {segment.offset for segment in pagination_segments}
            detected_offset = next(iter(unique_offsets)) if len(unique_offsets) == 1 else None
            offset_score = max(
                (segment.evidence_pages for segment in pagination_segments), default=0
            )
            normalized_pages = [analysis.normalized_text for analysis in analyses]
            parsed_toc_rows = sum(len(rows_by_page[page]) for page in toc_pages)
            completeness_diagnostics, suspected_unparsed_rows = toc_completeness_diagnostics(
                analyses, toc_pages, rows_by_page
            )

            if not any(text.strip() for text in normalized_pages):
                message = (
                    "The PDF has no extractable text. Use OCR, --reference-pdf, or a reviewed link manifest."
                )
                warnings.append(message)
                review_reasons.append(message)
            if suspected_unparsed_rows:
                message = (
                    f"The text layer suggests at least {suspected_unparsed_rows} TOC rows whose "
                    "page labels were not parsed. Use targeted OCR or a reviewed link manifest."
                )
                warnings.append(message)
                review_reasons.append(message)

            if not args.no_toc_links:
                for source_page in sorted(toc_pages):
                    for row in rows_by_page[source_page]:
                        block_index = block_index_for_page(source_page, blocks)
                        if block_index is None:
                            destination, matched_by, confidence, evidence = (
                                None,
                                None,
                                "low",
                                {"reason": "TOC row is outside a detected TOC block"},
                            )
                        else:
                            destination, matched_by, confidence, evidence = resolve_toc_row(
                                row,
                                block_index,
                                blocks,
                                pagination_segments,
                                analyses,
                                normalized_pages,
                                toc_pages,
                            )
                        if destination is None or destination == source_page:
                            unresolved.append(
                                {
                                    "source_page": source_page + 1,
                                    "title": row.title,
                                    "printed_label": row.printed_label,
                                    "label_kind": row.label_kind,
                                    "reason": evidence.get("reason", "no safe destination"),
                                    "evidence": evidence,
                                }
                            )
                            continue
                        if any(rect_overlap_ratio(row.rect, rect) >= 0.8 for rect in occupied[source_page]):
                            targets = existing_internal_targets(reader, source_page, row.rect)
                            if destination in targets:
                                covered_existing_rows += 1
                            else:
                                unresolved.append(
                                    {
                                        "source_page": source_page + 1,
                                        "title": row.title,
                                        "printed_label": row.printed_label,
                                        "label_kind": row.label_kind,
                                        "reason": "an overlapping existing link has a different or unknown destination",
                                        "evidence": {"expected_target_page": destination + 1, "existing_targets": [value + 1 for value in targets]},
                                    }
                                )
                            continue
                        link = AddedLink(
                            "internal",
                            source_page,
                            row.rect,
                            target_page=destination,
                            label=f"{row.title} -> {row.printed_label} ({matched_by})",
                            title=row.title,
                            printed_label=row.printed_label,
                            printed_number=row.printed_number,
                            label_kind=row.label_kind,
                            matched_by=matched_by,
                            confidence=confidence,
                            evidence=evidence,
                        )
                        added.append(link)
                        occupied[source_page].append(row.rect)

            if not args.no_url_links:
                for analysis in analyses:
                    for word in analysis.words:
                        raw = str(word["text"])
                        if normalized_uri(raw, allow_bare_domains=True) and not normalized_uri(raw):
                            skipped_bare_domain_candidates += 1
                for link in visible_url_candidates(
                    analyses, allow_bare_domains=args.allow_bare_domains
                ):
                    if int(reader.pages[link.source_page].get("/Rotate", 0) or 0) % 360 != 0:
                        rotated_automatic_url_pages.add(link.source_page)
                        continue
                    if any(rect_overlap_ratio(link.rect, rect) >= 0.8 for rect in occupied[link.source_page]):
                        continue
                    added.append(link)
                    occupied[link.source_page].append(link.rect)
    timings["analysis_seconds"] = round(time.perf_counter() - analysis_started, 4)

    if not args.no_url_links:
        for link in recoverable_broken_uri_candidates(
            reader, allow_bare_domains=args.allow_bare_domains
        ):
            if any(rect_overlap_ratio(link.rect, rect) >= 0.8 for rect in occupied[link.source_page]):
                continue
            added.append(link)
            occupied[link.source_page].append(link.rect)

    invalid_external_links = [
        link for link in added if link.kind == "external" and not supported_uri(str(link.uri))
    ]
    if invalid_external_links:
        message = f"{len(invalid_external_links)} external links have unsupported URIs."
        warnings.append(message)
        review_reasons.append(message)
    if rotated_automatic_url_pages:
        pages = ", ".join(str(page + 1) for page in sorted(rotated_automatic_url_pages))
        message = (
            "Rotated pages require reviewed PDF-coordinate rectangles before adding "
            f"automatically detected URL links: {pages}."
        )
        warnings.append(message)
        review_reasons.append(message)
    if replay_conflicts:
        message = (
            f"{len(replay_conflicts)} replay candidates overlap links with different or "
            "unknown destinations/URIs."
        )
        warnings.append(message)
        review_reasons.append(message)
    if not toc_pages and not args.no_toc_links and not reference_path and not manifest_path:
        warnings.append("No TOC-like pages were detected. Use --toc-pages for unusual layouts.")
    if unresolved:
        message = f"{len(unresolved)} detected TOC rows could not be safely mapped."
        warnings.append(message)
        review_reasons.append(message)
    if not added and not existing_counts["internal"] and not existing_counts["external"]:
        message = "No link annotations were found or safely planned."
        warnings.append(message)
        review_reasons.append(message)

    source_pdf_version = pdf_version_manifest(reader)
    added_counts = Counter(link.kind for link in added)
    predicted_counts = Counter(existing_counts)
    predicted_counts.update(added_counts)
    segments_report = [
        {
            **asdict(segment),
            "physical_start": segment.physical_start,
            "physical_end": segment.physical_end,
        }
        for segment in pagination_segments
    ]
    if manifest_input is not None:
        manifest_input["covered_existing_links"] = len(covered_replay_links)
        manifest_input["conflicting_links"] = len(replay_conflicts)
    status = "NEEDS_REVIEW" if review_reasons else "PASS"
    report = {
        "schema_version": 2,
        "status": status,
        "input": str(input_path),
        "input_sha256": input_sha256,
        "output": str(output_path),
        "output_sha256": None,
        "output_mode": output_mode,
        "mode": mode,
        "skill_provenance": runtime_provenance,
        "pdf_version": source_pdf_version,
        "pages": page_count,
        "toc_pages": [page + 1 for page in sorted(toc_pages)],
        "toc_blocks": [
            {"block_index": index, "pages": [start + 1, end + 1]}
            for index, (start, end) in enumerate(contiguous_groups(toc_pages))
        ],
        "parsed_toc_rows": parsed_toc_rows,
        "covered_by_existing_links": covered_existing_rows,
        "replay_conflicts": replay_conflicts,
        "rotated_automatic_url_pages": [
            page + 1 for page in sorted(rotated_automatic_url_pages)
        ],
        "suspected_unparsed_toc_rows": suspected_unparsed_rows,
        "toc_completeness": completeness_diagnostics,
        "detected_page_offset": detected_offset,
        "offset_evidence_pages": offset_score,
        "pagination_segments": segments_report,
        "pagination_diagnostics": pagination_diagnostics,
        "existing_links": dict(existing_counts),
        "added_links": dict(added_counts),
        "final_links": dict(predicted_counts),
        "unresolved_toc_rows": unresolved,
        "skipped_bare_domain_candidates": skipped_bare_domain_candidates,
        "bare_domain_links_enabled": bool(args.allow_bare_domains),
        "review_reasons": list(dict.fromkeys(review_reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "reference_pdf_sha256": reference_sha256,
        "manifest_input": manifest_input,
        "timings": timings,
        "links": [asdict(link) for link in added],
    }
    if report_path is not None:
        report["report_json"] = str(report_path)

    if status != "PASS":
        report["timings"]["total_seconds"] = round(time.perf_counter() - started, 4)
        # A review report is useful for deterministic repair, but never replace a
        # previously published deliverable/report after an unsuccessful rerun.
        if report_path is not None and not output_path.exists():
            staged_report = sibling_temporary_path(report_path, ".json")
            try:
                staged_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
                atomic_publish([(staged_report, report_path)], force=args.force)
            finally:
                staged_report.unlink(missing_ok=True)
        return report

    write_started = time.perf_counter()
    source_content_hashes = [page_content_sha256(page) for page in reader.pages]
    source_visual_resources = visual_resource_state(reader)
    writer = PdfWriter(clone_from=reader)
    # PdfWriter otherwise defaults cloned documents to %PDF-1.3, which can
    # under-declare artwork features such as transparency and soft masks.
    writer.pdf_header = reader.pdf_header
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

    staged_pdf = sibling_temporary_path(output_path, ".pdf")
    staged_report: Path | None = None
    try:
        with staged_pdf.open("wb") as handle:
            writer.write(handle)
        timings["write_seconds"] = round(time.perf_counter() - write_started, 4)

        validation_started = time.perf_counter()
        result = PdfReader(staged_pdf, strict=False)
        output_pdf_version = pdf_version_manifest(result)
        if output_pdf_version != source_pdf_version:
            raise RuntimeError(f"PDF version changed: {source_pdf_version} -> {output_pdf_version}")
        if len(result.pages) != page_count:
            raise RuntimeError("Output page count changed")
        for index, (source_page, result_page, source_content_hash) in enumerate(
            zip(reader.pages, result.pages, source_content_hashes, strict=True), start=1
        ):
            source_size = (float(source_page.mediabox.width), float(source_page.mediabox.height))
            result_size = (float(result_page.mediabox.width), float(result_page.mediabox.height))
            if source_size != result_size:
                raise RuntimeError(f"Page {index} dimensions changed: {source_size} -> {result_size}")
            source_crop = tuple(float(value) for value in source_page.cropbox)
            result_crop = tuple(float(value) for value in result_page.cropbox)
            if source_crop != result_crop:
                raise RuntimeError(f"Page {index} crop box changed")
            if int(source_page.get("/Rotate", 0) or 0) != int(result_page.get("/Rotate", 0) or 0):
                raise RuntimeError(f"Page {index} rotation changed")
            if page_content_sha256(result_page) != source_content_hash:
                raise RuntimeError(f"Page {index} content stream changed")

        result_visual_resources = visual_resource_state(result)
        for index, (source_digest, result_digest) in enumerate(
            zip(
                source_visual_resources["pages"],
                result_visual_resources["pages"],
                strict=True,
            ),
            start=1,
        ):
            if source_digest != result_digest:
                raise RuntimeError(f"Page {index} visual resources changed")
        if source_visual_resources["catalog"] != result_visual_resources["catalog"]:
            raise RuntimeError("Catalog visual state changed")

        result_link_errors = link_annotation_errors(result)
        if result_link_errors:
            preview = "; ".join(result_link_errors[:10])
            remainder = len(result_link_errors) - 10
            suffix = f"; plus {remainder} more" if remainder > 0 else ""
            raise RuntimeError(f"Output contains invalid link annotations: {preview}{suffix}")

        result_counts = classify_annotations(result)
        for kind in ("internal", "external"):
            expected = existing_counts[kind] + added_counts[kind]
            if result_counts[kind] != expected:
                raise RuntimeError(f"Expected {expected} {kind} links, found {result_counts[kind]}")
        timings["validation_seconds"] = round(time.perf_counter() - validation_started, 4)
        report["pdf_version"] = output_pdf_version
        report["visual_resource_check"] = True
        report["final_links"] = dict(result_counts)
        report["output_sha256"] = sha256_file(staged_pdf)
        report["timings"] = timings
        report["timings"]["total_seconds"] = round(time.perf_counter() - started, 4)

        # Publish the PDF first and the report last so the report is the
        # transaction's commit marker for consumers.
        staged: list[tuple[Path, Path]] = [(staged_pdf, output_path)]
        if report_path is not None:
            staged_report = sibling_temporary_path(report_path, ".json")
            staged_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
            staged.append((staged_report, report_path))
        atomic_publish(staged, force=args.force)
    finally:
        staged_pdf.unlink(missing_ok=True)
        if staged_report is not None:
            staged_report.unlink(missing_ok=True)
    return report


def make_interactive(args: argparse.Namespace) -> dict:
    input_path = Path(args.input).expanduser().resolve()
    _, output_path, report_path = planned_output_paths(args, input_path)
    reject_non_file_destinations({"Output": output_path, "Report": report_path})
    with destination_locks((output_path, report_path)):
        return _make_interactive(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
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
    parser.add_argument(
        "--link-manifest",
        help="Reviewed JSON report/manifest whose exact zero-based link mappings should be applied",
    )
    parser.add_argument(
        "--allow-legacy-manifest",
        action="store_true",
        help="Accept a pre-v1.2 manifest only after its mappings have been independently approved",
    )
    parser.add_argument("--password", help="Password for an encrypted source; output is not encrypted")
    parser.add_argument("--no-toc-links", action="store_true", help="Do not add internal navigation links")
    parser.add_argument("--no-url-links", action="store_true", help="Do not add visible URL/email links")
    parser.add_argument(
        "--allow-bare-domains",
        action="store_true",
        help="Also link bare domains; disabled by default because OCR punctuation causes false URLs",
    )
    parser.add_argument(
        "--allow-signature-invalidation",
        action="store_true",
        help="Allow adding annotations to a digitally signed PDF, which invalidates its signatures",
    )
    parser.add_argument("--report-json", help="Write a detailed JSON detection/link report")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Backward-compatible alias for the default fail-closed policy",
    )
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
    return 2 if report.get("status") == "NEEDS_REVIEW" else 0


if __name__ == "__main__":
    raise SystemExit(main())
