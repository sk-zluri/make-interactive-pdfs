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
from dataclasses import asdict, dataclass, field, replace
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

from local_ocr import (
    MarginOCRRegion,
    MarginOCRSummary,
    NavigationOCRSummary,
    OCRSummary,
    ocr_low_text_pages,
    ocr_margin_regions,
    ocr_navigation_columns,
)
from progress_events import emit_progress
from skill_provenance import require_isolated_runtime


TOC_HEADINGS = ("table of contents", "contents", "agenda", "index")
MARGIN_OCR_ACCEPTANCE_CONFIDENCE = 0.90
PAGE_TOKEN_RE = re.compile(r"^[\s.·•()\[\]-]*([0-9]{1,4}|[ivxlcdm]{1,10})[\s.·•()\[\]-]*$", re.I)
PAGE_RANGE_RE = re.compile(
    r"^[\s.·•()\[\]]*([0-9]{1,4})\s*[-–—―�]+\s*([0-9]{1,4})[\s.·•()\[\]]*$"
)
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
    label_candidates: tuple[tuple[int, str], ...] = ()
    ordinal: int | None = None
    navigation_ocr: bool = False
    structural_heading_kind: str | None = None


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
    top: float | None = None
    bottom: float | None = None
    x0: float | None = None
    x1: float | None = None
    page_width: float | None = None
    page_height: float | None = None
    has_inline_text_neighbor: bool = False


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
    rotation: int
    image_area_ratio: float
    words: list[dict]
    lines: list[list[dict]]
    raw_text: str
    normalized_text: str
    nontext_object_count: int
    content_stream_bytes: int
    margin_labels: list[PageLabelObservation]
    ordinal_anchor_count: int


@dataclass(frozen=True)
class MarginOCRCheck:
    """One visual label check prompted by a native-text margin conflict."""

    region: MarginOCRRegion
    expected_number: int
    label_kind: str
    observation: PageLabelObservation


def normalize_text(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def visual_rect_to_pdf(
    x0: float,
    top: float,
    x1: float,
    bottom: float,
    *,
    width: float,
    height: float,
    rotation: int = 0,
) -> tuple[float, float, float, float]:
    """Convert a displayed top-origin rectangle to the page's PDF user space."""

    rotation = int(rotation) % 360
    if rotation == 90:
        return (top, x0, bottom, x1)
    if rotation == 180:
        return (width - x1, top, width - x0, bottom)
    if rotation == 270:
        return (height - bottom, width - x1, height - top, width - x0)
    return (x0, height - bottom, x1, height - top)


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


def page_label_candidates(value: str) -> list[tuple[int, str]]:
    """Return strict and OCR-confusable page-label interpretations.

    These are candidates only. Callers must use pagination and title evidence
    before accepting a non-strict interpretation.
    """

    raw = str(value).strip()
    results: list[tuple[int, str]] = []

    def add(number: int | None, kind: str) -> None:
        if number is not None and 0 < number <= 3999 and (number, kind) not in results:
            results.append((number, kind))

    strict = parse_page_label_details(raw)
    if strict is not None:
        add(*strict)

    compact = re.sub(r"[^A-Za-z0-9]", "", raw)
    if not compact:
        return results

    digit_translation = str.maketrans(
        {
            "I": "1",
            "i": "1",
            "L": "1",
            "l": "1",
            "O": "0",
            "o": "0",
            "Z": "2",
            "z": "2",
            "S": "5",
            "s": "5",
        }
    )
    digit_candidate = compact.translate(digit_translation)
    if digit_candidate.isdigit():
        add(int(digit_candidate), "arabic")

    roman_candidate = compact.upper()
    if roman_candidate.endswith("U"):
        roman_candidate = roman_candidate[:-1] + "II"
    roman_candidate = roman_candidate.replace("L", "I")
    if roman_candidate and re.fullmatch(r"[IVXLCDM]+", roman_candidate):
        add(roman_to_int(roman_candidate), "roman")

    return results


def rotated_page_label_candidate(value: str) -> int | None:
    """Return a possible 180-degree numeric reading for targeted OCR only."""

    compact = re.sub(r"\D", "", str(value))
    if not compact:
        return None
    rotated = compact[::-1].translate(str.maketrans({"6": "9", "9": "6"}))
    if rotated == compact or not rotated.isdigit():
        return None
    number = int(rotated)
    return number if 0 < number <= 3999 else None


def page_range_candidates(
    value: str,
    *,
    maximum: int = 9999,
    allow_compact: bool = False,
) -> list[tuple[int, int]]:
    """Parse an Arabic range; compact guesses require page-layout evidence."""

    raw = str(value).strip()
    explicit = PAGE_RANGE_RE.fullmatch(raw)
    if explicit:
        start, end = int(explicit.group(1)), int(explicit.group(2))
        return [(start, end)] if 0 < start <= end <= maximum else []
    if not allow_compact:
        return []
    compact = re.sub(r"\D", "", raw)
    if len(compact) < 2 or len(compact) > 8:
        return []
    candidates: list[tuple[int, int]] = []
    for split in range(1, len(compact)):
        start, end = int(compact[:split]), int(compact[split:])
        if 0 < start <= end <= maximum and (start, end) not in candidates:
            candidates.append((start, end))
    candidates.sort(key=lambda item: (item[1] - item[0], abs(len(str(item[0])) - len(str(item[1])))))
    return candidates


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


def _is_toc_heading_text(normalized: str) -> bool:
    is_named_volume = bool(
        re.fullmatch(
            r"(?:table of contents|contents) (?:volume|part|book) "
            r"(?:[0-9]{1,3}|[ivxlcdm]{1,10})",
            normalized,
            flags=re.I,
        )
    )
    return (
        normalized in TOC_HEADINGS
        or normalized in {f"{heading} continued" for heading in TOC_HEADINGS}
        or is_named_volume
    )


def toc_heading_line(words: Sequence[dict], page_height: float) -> list[dict] | None:
    """Return an exact, top-of-page TOC heading line.

    A prose sentence merely containing the word ``contents`` is deliberately
    not a heading.
    """

    lines = group_words_into_lines(words, tolerance=4.0)
    top_lines = [
        line
        for line in lines
        if line and min(float(word["top"]) for word in line) <= page_height * 0.34
    ]
    for first, second in zip(top_lines, top_lines[1:]):
        first_bottom = max(float(word["bottom"]) for word in first)
        second_top = min(float(word["top"]) for word in second)
        first_height = first_bottom - min(float(word["top"]) for word in first)
        second_height = max(float(word["bottom"]) for word in second) - second_top
        if second_top - first_bottom > max(12.0, 2.5 * max(first_height, second_height)):
            continue
        combined = normalize_text(
            " ".join(str(word["text"]) for word in (*first, *second))
        )
        if _is_toc_heading_text(combined):
            return [*first, *second]

    for line in lines:
        if not line:
            continue
        top = min(float(word["top"]) for word in line)
        if top > page_height * 0.34:
            continue
        normalized = normalize_text(" ".join(str(word["text"]) for word in line))
        is_named_volume = bool(
            re.fullmatch(
                r"(?:table of contents|contents) (?:volume|part|book) "
                r"(?:[0-9]{1,3}|[ivxlcdm]{1,10})",
                normalized,
                flags=re.I,
            )
        )
        if (
            normalized in TOC_HEADINGS
            or normalized in {f"{heading} continued" for heading in TOC_HEADINGS}
            or is_named_volume
        ):
            return line
    return None


def has_toc_heading_line(analysis: PageAnalysis) -> bool:
    return toc_heading_line(analysis.words, analysis.height) is not None


def _row_rect(page, row_words: Sequence[dict]) -> tuple[float, float, float, float]:
    x0 = max(0.0, min(float(word["x0"]) for word in row_words) - 1.5)
    x1 = min(float(page.width), max(float(word["x1"]) for word in row_words) + 1.5)
    top = max(0.0, min(float(word["top"]) for word in row_words) - 1.5)
    bottom = min(float(page.height), max(float(word["bottom"]) for word in row_words) + 1.5)
    return visual_rect_to_pdf(
        x0,
        top,
        x1,
        bottom,
        width=float(page.width),
        height=float(page.height),
        rotation=int(getattr(page, "rotation", 0)),
    )


def _semantic_range_rows(page_index: int, page, words: Sequence[dict]) -> list[TocRow]:
    """Parse SECTION metadata followed by title + Arabic page-range rows."""

    section_words = [
        word for word in words if normalize_text(str(word.get("text", ""))) == "section"
    ]
    explicit_range_present = any(
        float(word.get("x0", 0.0)) >= float(page.width) * 0.72
        and PAGE_RANGE_RE.fullmatch(str(word.get("text", "")).strip())
        for word in words
    )
    if len(section_words) < 3 and not (section_words and explicit_range_present):
        return []

    explicit_range_count = sum(
        bool(PAGE_RANGE_RE.fullmatch(str(word.get("text", "")).strip()))
        for word in words
        if float(word.get("x0", 0.0)) >= float(page.width) * 0.72
    )
    has_page_column_heading = any(
        normalize_text(str(word.get("text", ""))) == "page"
        and float(word.get("x0", 0.0)) >= float(page.width) * 0.65
        for word in words
    )
    allow_compact_ranges = (
        len(section_words) >= 3
        and explicit_range_count >= 2
        and has_page_column_heading
    )

    anchors: list[tuple[dict, list[tuple[int, int]]]] = []
    for word in words:
        if bool(word.get("ocr_navigation")):
            continue
        if float(word["x0"]) < float(page.width) * 0.72:
            continue
        candidates = page_range_candidates(
            str(word.get("text", "")),
            maximum=9999,
            allow_compact=allow_compact_ranges,
        )
        if candidates:
            anchors.append((word, [candidates[0]]))
    anchors.sort(key=lambda item: (float(item[0]["top"]), float(item[0]["x0"])))
    if len(anchors) < 1:
        return []
    if allow_compact_ranges and len(anchors) >= 3:
        transitions = [
            next_ranges[0][0] - previous_ranges[0][1]
            for (_, previous_ranges), (_, next_ranges) in zip(anchors, anchors[1:])
        ]
        contiguous = sum(delta in {0, 1} for delta in transitions)
        if contiguous / len(transitions) < 0.8:
            return []

    rows: list[TocRow] = []
    previous_bottom = 0.0
    for index, (anchor, ranges) in enumerate(anchors):
        anchor_top = float(anchor["top"])
        following_top = (
            float(anchors[index + 1][0]["top"]) if index + 1 < len(anchors) else float(page.height)
        )
        nearby_titles = [
            word
            for word in words
            if float(word["x0"]) < float(page.width) * 0.55
            and previous_bottom - 2.0 <= float(word["top"]) <= min(following_top, anchor_top + 13.0)
            and abs(float(word["top"]) - anchor_top) <= 13.0
            and normalize_text(str(word.get("text", ""))) != "section"
            and parse_page_label_details(str(word.get("text", ""))) is None
        ]
        nearby_titles.sort(key=lambda word: (float(word["top"]), float(word["x0"])))
        title = " ".join(str(word["text"]) for word in nearby_titles)
        title = re.sub(r"\s+", " ", title).strip(" .·•-–—\t")
        if len(normalize_text(title)) < 3:
            previous_bottom = float(anchor["bottom"])
            continue

        metadata = [
            word
            for word in words
            if max(anchor_top - 40.0, previous_bottom + 0.1)
            <= float(word["top"])
            <= anchor_top + 13.0
            and (
                normalize_text(str(word.get("text", ""))) == "section"
                or re.fullmatch(r"[IVXLCDM^\-]+", str(word.get("text", "")).upper())
            )
        ]
        section_ordinal = None
        for word in metadata:
            raw_metadata = str(word.get("text", "")).upper().replace("^", "")
            match = re.fullmatch(
                r"([IVXLCDM]+)(?:[-–—]([IVXLCDM]+))?",
                raw_metadata,
            )
            if match:
                section_ordinal = roman_to_int(match.group(1))
                if section_ordinal is not None:
                    break
        row_words = [*metadata, *nearby_titles, anchor]
        start, _ = ranges[0]
        rows.append(
            TocRow(
                page_index,
                title,
                str(anchor["text"]),
                start,
                "arabic",
                _row_rect(page, row_words),
                tuple((candidate_start, "arabic") for candidate_start, _ in ranges),
                ordinal=section_ordinal,
                structural_heading_kind="section",
            )
        )
        previous_bottom = float(anchor["bottom"])
    return rows


def has_section_range_layout(words: Sequence[dict]) -> bool:
    section_count = sum(
        normalize_text(str(word.get("text", ""))) == "section" for word in words
    )
    return section_count >= 3 or (
        section_count >= 1
        and any(
            PAGE_RANGE_RE.fullmatch(str(word.get("text", "")).strip())
            for word in words
        )
    )


def _toc_ordinal(value: str) -> int | None:
    compact = re.sub(r"[^A-Za-z0-9]", "", str(value))
    if not compact:
        return None
    translated = compact.translate(
        str.maketrans(
            {
                "I": "1",
                "i": "1",
                "L": "1",
                "l": "1",
                "O": "0",
                "o": "0",
                "Z": "2",
                "z": "2",
                "S": "5",
                "s": "5",
            }
        )
    )
    if translated.isdigit():
        number = int(translated)
        return number if 0 < number <= 999 else None
    return None


def _navigation_anchor_rows(page_index: int, page, words: Sequence[dict]) -> list[TocRow]:
    """Build one semantic row per OCR-confirmed right-column page label."""

    if not any(bool(word.get("ocr_navigation")) for word in words):
        return []

    width, height = float(page.width), float(page.height)
    source_groups: list[tuple[float, float, list[dict], list[tuple[int, str]], str, bool]] = []
    for navigation_only in (True, False):
        source_words = [
            word
            for word in words
            if bool(word.get("ocr_navigation")) is navigation_only
        ]
        for line in group_words_into_lines(source_words, tolerance=2.0):
            minimum_x0 = width * (0.84 if navigation_only else 0.83)
            right_words = [
                word
                for word in line
                if (
                    float(word["x0"]) >= minimum_x0
                    or (
                        not navigation_only
                        and float(word["x0"]) >= width * 0.78
                        and sum(
                            character.isdigit()
                            for character in str(word.get("text", ""))
                        )
                        >= 2
                    )
                )
                and float(word["bottom"]) - float(word["top"]) <= height * 0.03
            ]
            if not right_words or max(float(word["x1"]) for word in right_words) < width * 0.87:
                continue
            right_words.sort(key=lambda word: float(word["x0"]))
            raw = "".join(str(word.get("text", "")) for word in right_words)
            candidates = page_label_candidates(raw)
            if not candidates:
                for word in right_words:
                    for candidate in page_label_candidates(str(word.get("text", ""))):
                        if candidate not in candidates:
                            candidates.append(candidate)
            if not candidates:
                continue
            top = min(float(word["top"]) for word in right_words)
            bottom = max(float(word["bottom"]) for word in right_words)
            if top <= height * 0.08 or top >= height * 0.94:
                continue
            source_groups.append(
                (top, bottom, right_words, candidates, raw, navigation_only)
            )

    source_groups.sort(key=lambda item: (item[0], not item[5]))
    clusters: list[dict] = []
    for top, bottom, label_words, candidates, raw, navigation_only in source_groups:
        center = (top + bottom) / 2
        shared_candidate = bool(
            clusters
            and set(candidates).intersection(clusters[-1]["candidates"])
        )
        vertical_overlap = (
            max(
                0.0,
                min(bottom, float(clusters[-1]["bottom"]))
                - max(top, float(clusters[-1]["top"])),
            )
            if clusters
            else 0.0
        )
        different_sources = bool(
            clusters
            and any(
                bool(existing_navigation) is not navigation_only
                for _, existing_navigation in clusters[-1]["raw"]
            )
        )
        overlap_ratio = (
            vertical_overlap
            / max(
                0.001,
                min(bottom - top, float(clusters[-1]["bottom"]) - float(clusters[-1]["top"])),
            )
            if clusters
            else 0.0
        )
        if clusters and (
            abs(center - float(clusters[-1]["center"])) <= 2.0
            or (
                shared_candidate
                and abs(center - float(clusters[-1]["center"])) <= 5.5
            )
            or (different_sources and overlap_ratio >= 0.45)
        ):
            cluster = clusters[-1]
            cluster["top"] = min(float(cluster["top"]), top)
            cluster["bottom"] = max(float(cluster["bottom"]), bottom)
            cluster["words"].extend(label_words)
            cluster["raw"].append((raw, navigation_only))
            for candidate in candidates:
                if candidate not in cluster["candidates"]:
                    cluster["candidates"].append(candidate)
            cluster["center"] = (float(cluster["top"]) + float(cluster["bottom"])) / 2
        else:
            clusters.append(
                {
                    "top": top,
                    "bottom": bottom,
                    "center": center,
                    "words": list(label_words),
                    "candidates": list(candidates),
                    "raw": [(raw, navigation_only)],
                }
            )

    heading = toc_heading_line(words, height)
    heading_bottom = max((float(word["bottom"]) for word in heading or ()), default=height * 0.08)
    clusters = [cluster for cluster in clusters if float(cluster["top"]) > heading_bottom + 4.0]

    expanded_clusters: list[dict] = []
    for index, cluster in enumerate(clusters):
        previous_number = (
            int(expanded_clusters[-1]["candidates"][0][0])
            if expanded_clusters
            else 0
        )
        next_number = (
            int(clusters[index + 1]["candidates"][0][0])
            if index + 1 < len(clusters)
            else 4000
        )
        plausible = sorted(
            {
                int(number)
                for number, kind in cluster["candidates"]
                if kind == "arabic" and previous_number < int(number) < next_number
            }
        )
        raw_digit_options = [
            re.sub(r"\D", "", raw)
            for raw, _ in cluster["raw"]
            if re.sub(r"\D", "", raw)
        ]
        primary_number = int(cluster["candidates"][0][0])
        if (
            len(plausible) < 2
            and not (previous_number < primary_number < next_number)
        ):
            for compact in raw_digit_options:
                for split in range(1, len(compact)):
                    first, second = int(compact[:split]), int(compact[split:])
                    if previous_number < first < second < next_number:
                        plausible = [first, second]
                        break
                if len(plausible) >= 2:
                    break

        if len(plausible) < 2:
            expanded_clusters.append(cluster)
            continue

        slice_height = max(
            1.0,
            (float(cluster["bottom"]) - float(cluster["top"])) / len(plausible),
        )
        for part, number in enumerate(plausible):
            child = dict(cluster)
            child_top = float(cluster["top"]) + part * slice_height
            child_bottom = (
                float(cluster["bottom"])
                if part + 1 == len(plausible)
                else child_top + slice_height
            )
            template = dict(cluster["words"][0])
            template.update(
                {
                    "text": str(number),
                    "top": child_top,
                    "bottom": child_bottom,
                }
            )
            child.update(
                {
                    "top": child_top,
                    "bottom": child_bottom,
                    "center": (child_top + child_bottom) / 2,
                    "words": [template],
                    "candidates": [(number, "arabic")],
                    "raw": [(str(number), True)],
                }
            )
            expanded_clusters.append(child)
    clusters = expanded_clusters

    rows: list[TocRow] = []
    heading_word_ids = {id(word) for word in heading or ()}
    for cluster_index, cluster in enumerate(clusters):
        top = max(
            heading_bottom + 0.25,
            (
                (
                    float(clusters[cluster_index - 1]["center"])
                    + float(cluster["center"])
                )
                / 2
                if cluster_index > 0
                else heading_bottom + 0.25
            ),
        )
        bottom = min(
            height,
            (
                (
                    float(cluster["center"])
                    + float(clusters[cluster_index + 1]["center"])
                )
                / 2
                if cluster_index + 1 < len(clusters)
                else float(cluster["bottom"]) + 2.5
            ),
        )
        title_words = [
            word
            for word in words
            if not bool(word.get("ocr_navigation"))
            and id(word) not in heading_word_ids
            and float(word["x0"]) < width * 0.80
            and float(word["bottom"]) >= top - 1.0
            and float(word["top"]) <= bottom
        ]
        title_words.sort(key=lambda word: (float(word["top"]), float(word["x0"])))
        title = " ".join(str(word.get("text", "")) for word in title_words)
        title = re.sub(r"^\s*[A-Za-z0-9]{1,4}[.·):\-]+\s*", "", title)
        title = re.sub(r"(?:\s*[-–—·•�]\s*){2,}", " ", title)
        title = re.sub(r"\s+", " ", title).strip(" .·•-–—\t")
        normalized = normalize_text(title)
        if normalized in TOC_HEADINGS or normalized in {"chapter page", "page"}:
            title = ""
        if len(normalize_text(title)) < 3:
            title = f"Contents entry {len(rows) + 1}"

        candidates = tuple(cluster["candidates"])
        label, label_kind = candidates[0]
        raw_options = sorted(cluster["raw"], key=lambda item: not item[1])
        raw_label = raw_options[0][0]
        ordinal = None
        left_edge_words = sorted(
            (word for word in title_words if float(word["x0"]) <= width * 0.30),
            key=lambda word: (
                abs(
                    (float(word["top"]) + float(word["bottom"])) / 2
                    - float(cluster["center"])
                ),
                float(word["x0"]),
            ),
        )
        for word in left_edge_words:
            ordinal = _toc_ordinal(str(word.get("text", "")))
            if ordinal is not None:
                break

        row_words = [*title_words, *cluster["words"]]
        row_x0 = max(0.0, min(float(word["x0"]) for word in row_words) - 1.0)
        row_x1 = min(width, max(float(word["x1"]) for word in row_words) + 1.0)
        row_rect = visual_rect_to_pdf(
            row_x0,
            top,
            row_x1,
            bottom,
            width=width,
            height=height,
            rotation=int(getattr(page, "rotation", 0)),
        )

        rows.append(
            TocRow(
                page_index,
                title,
                raw_label,
                label,
                label_kind,
                row_rect,
                candidates,
                ordinal,
                any(is_navigation for _, is_navigation in cluster["raw"]),
            )
        )
    return rows


def toc_rows_for_page(page_index: int, page, words: Sequence[dict] | None = None) -> list[TocRow]:
    words = list(words) if words is not None else page.extract_words(
        use_text_flow=False, keep_blank_chars=False
    )
    range_rows = _semantic_range_rows(page_index, page, words)
    if range_rows:
        return range_rows
    navigation_rows = _navigation_anchor_rows(page_index, page, words)
    if navigation_rows:
        return navigation_rows

    rows: list[TocRow] = []
    for line in group_words_into_lines(words):
        if len(line) < 2:
            continue
        raw_label = str(line[-1]["text"])
        candidates = page_label_candidates(raw_label)
        if not candidates or candidates[0][0] <= 0:
            continue
        label, label_kind = candidates[0]
        if float(line[-1]["x0"]) < float(page.width) * 0.55:
            continue
        title = " ".join(str(word["text"]) for word in line[:-1])
        title = re.sub(r"(?:\s*[.·•]{2,}\s*)+$", "", title).strip(" .·•-–—\t")
        normalized = normalize_text(title)
        if len(normalized) < 3 or normalized in TOC_HEADINGS:
            continue
        rows.append(
            TocRow(
                page_index,
                title,
                raw_label,
                label,
                label_kind,
                _row_rect(page, line),
                tuple(candidates),
            )
        )
    return rows


def margin_label_observations(
    page_index: int, width: float, height: float, words: Sequence[dict], page_count: int
) -> list[PageLabelObservation]:
    observations: list[PageLabelObservation] = []
    seen: set[tuple[int, str, bool]] = set()
    for word in words:
        top = float(word["top"])
        if not (top <= height * 0.08 or top >= height * 0.88):
            continue
        x0, x1 = float(word["x0"]), float(word["x1"])
        center = (x0 + x1) / 2
        centered = abs(center - width / 2) <= width * 0.12
        in_outer_corner = x0 <= width * 0.22 or x1 >= width * 0.78
        if not (in_outer_corner or centered):
            continue
        parsed = parse_page_label_details(str(word["text"]))
        if parsed is None:
            continue
        number, label_kind = parsed
        if number <= 0 or number > max(100, page_count * 3):
            continue

        has_inline_text_neighbor = False
        if in_outer_corner and not centered and top >= height * 0.88:
            word_top = float(word["top"])
            word_bottom = float(word["bottom"])
            word_height = max(0.1, word_bottom - word_top)
            gap_limit = max(4.0, min(10.0, width * 0.02))
            for other in words:
                if other is word or not re.search(r"[A-Za-z]", str(other["text"])):
                    continue
                other_top = float(other["top"])
                other_bottom = float(other["bottom"])
                if min(word_bottom, other_bottom) <= max(word_top, other_top):
                    continue
                other_height = max(0.1, other_bottom - other_top)
                if word_height > other_height * 0.8:
                    continue
                other_x0 = float(other["x0"])
                other_x1 = float(other["x1"])
                if other_x0 >= x1:
                    horizontal_gap = other_x0 - x1
                elif x0 >= other_x1:
                    horizontal_gap = x0 - other_x1
                else:
                    horizontal_gap = 0.0
                if horizontal_gap <= gap_limit:
                    has_inline_text_neighbor = True
                    break
        key = (number, label_kind, has_inline_text_neighbor)
        if key in seen:
            continue
        seen.add(key)
        observations.append(
            PageLabelObservation(
                page_index,
                number,
                label_kind,
                str(word["text"]),
                float(word["top"]),
                float(word["bottom"]),
                x0,
                x1,
                width,
                height,
                has_inline_text_neighbor,
            )
        )
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
    emit_progress(
        "READING_PAGES",
        completed=0,
        total=page_count,
        unit="page",
        page=1,
        total_pages=page_count,
        message="Reading the PDF text layer",
    )
    for page_index, page in enumerate(plumber_pdf.pages):
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        lines = group_words_into_lines(words)
        ordered_text = " ".join(str(word["text"]) for line in lines for word in line)
        width, height = float(page.width), float(page.height)
        image_area = 0.0
        for image in page.images:
            image_width = max(0.0, float(image.get("x1", 0.0)) - float(image.get("x0", 0.0)))
            image_height = max(
                0.0,
                float(image.get("bottom", 0.0)) - float(image.get("top", 0.0)),
            )
            image_area += image_width * image_height
        page_area = width * height
        image_area_ratio = min(1.0, image_area / page_area) if page_area > 0 else 0.0
        nontext_object_count = sum(
            len(page.objects.get(kind, ()))
            for kind in ("image", "rect", "curve", "line")
        )
        content_stream_bytes = 0
        for stream in getattr(page.page_obj, "contents", ()) or ():
            try:
                content_stream_bytes += len(stream.get_data())
            except (AttributeError, OSError, TypeError, ValueError):
                continue
        analyses.append(
            PageAnalysis(
                page_index=page_index,
                width=width,
                height=height,
                rotation=int(getattr(page, "rotation", 0) or 0) % 360,
                image_area_ratio=image_area_ratio,
                words=words,
                lines=lines,
                raw_text=ordered_text,
                normalized_text=normalize_text(ordered_text),
                nontext_object_count=nontext_object_count,
                content_stream_bytes=content_stream_bytes,
                margin_labels=margin_label_observations(
                    page_index, width, height, words, page_count
                ),
                ordinal_anchor_count=ordinal_anchor_count(width, height, words),
            )
        )
        emit_progress(
            "READING_PAGES",
            completed=page_index + 1,
            total=page_count,
            unit="page",
            page=page_index + 1,
            total_pages=page_count,
            message=f"Read page {page_index + 1} of {page_count}",
        )
        # pdfplumber otherwise retains each page's full parsed layout until the
        # document closes. The compact PageAnalysis above is all later phases need.
        close_page = getattr(page, "close", None)
        if callable(close_page):
            close_page()
    return analyses


def replace_analysis_words(
    analysis: PageAnalysis, words: Sequence[dict], page_count: int
) -> None:
    """Rebuild one analysis with OCR words while keeping the normal heuristics."""

    rotation_normalized = any(
        bool(word.get("_ocr_rotation_normalized")) for word in words
    )
    rebuilt_words = [
        {key: value for key, value in word.items() if key != "_ocr_rotation_normalized"}
        for word in words
    ]
    if rotation_normalized:
        if analysis.rotation in (90, 270):
            analysis.width, analysis.height = analysis.height, analysis.width
        analysis.rotation = 0
    lines = group_words_into_lines(rebuilt_words)
    ordered_text = " ".join(str(word["text"]) for line in lines for word in line)
    analysis.words = rebuilt_words
    analysis.lines = lines
    analysis.raw_text = ordered_text
    analysis.normalized_text = normalize_text(ordered_text)
    analysis.margin_labels = margin_label_observations(
        analysis.page_index,
        analysis.width,
        analysis.height,
        rebuilt_words,
        page_count,
    )
    analysis.ordinal_anchor_count = ordinal_anchor_count(
        analysis.width, analysis.height, rebuilt_words
    )


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
            rotation = analysis.rotation

        rows = toc_rows_for_page(page_index, PageShape(), analysis.words)
        rows_by_page[page_index] = rows
        if explicit_pages is not None:
            if page_index in explicit_pages:
                toc_pages.add(page_index)
            continue
        has_heading = has_toc_heading_line(analysis)
        if has_heading and len(rows) >= 2:
            toc_pages.add(page_index)
    if explicit_pages is None:
        toc_pages = expand_continuation_toc_pages(
            analyses,
            toc_pages,
            rows_by_page,
        )
    return toc_pages, rows_by_page


def headingless_navigation_candidates(
    analyses: Sequence[PageAnalysis],
    toc_pages: set[int],
    rows_by_page: Mapping[int, Sequence[TocRow]],
) -> list[int]:
    """Return strong isolated navigation layouts that must not pass silently."""

    candidates: list[int] = []
    for analysis in analyses:
        rows = rows_by_page.get(analysis.page_index, ())
        readable_lines = sum(
            bool(normalize_text(" ".join(str(word["text"]) for word in line)))
            for line in analysis.lines
        )
        row_density = len(rows) / max(1, readable_lines)
        if (
            analysis.page_index not in toc_pages
            and len(rows) >= 6
            and row_density >= 0.35
        ):
            candidates.append(analysis.page_index)
    return sorted(candidates)


def navigation_rows_without_internal_links(
    toc_pages: set[int],
    rows_by_page: Mapping[int, Sequence[TocRow]],
    existing_counts: Mapping[str, int],
    added: Sequence[AddedLink],
) -> bool:
    """Return whether detected navigation produced no page-jump annotations."""

    navigation_rows_expected = any(rows_by_page.get(page) for page in toc_pages)
    final_internal_links = int(existing_counts.get("internal", 0)) + sum(
        link.kind == "internal" for link in added
    )
    return navigation_rows_expected and final_internal_links == 0


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


def eligible_margin_observation(observation: PageLabelObservation) -> bool:
    """Return whether an edge number may support or contradict pagination."""

    if observation.has_inline_text_neighbor:
        return False
    if (
        observation.top is None
        or observation.x0 is None
        or observation.x1 is None
        or observation.page_width is None
        or observation.page_height is None
    ):
        return True
    center = (observation.x0 + observation.x1) / 2
    centered = (
        abs(center - observation.page_width / 2)
        <= observation.page_width * 0.14
    )
    in_outer_corner = (
        observation.x0 <= observation.page_width * 0.22
        or observation.x1 >= observation.page_width * 0.78
    )
    at_page_edge = (
        observation.top <= observation.page_height * 0.08
        or observation.top >= observation.page_height * 0.88
    )
    return (centered or in_outer_corner) and at_page_edge


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
            if eligible_margin_observation(observation)
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
    support_by_group: dict[tuple[int, str], int] = defaultdict(int)
    for segment in segments:
        key = (segment.block_index, segment.label_kind)
        support_by_group[key] = max(support_by_group[key], segment.evidence_pages)
    segments = [
        segment
        for segment in segments
        if segment.evidence_pages >= 8
        or segment.evidence_pages
        >= max(
            2,
            math.ceil(
                support_by_group[(segment.block_index, segment.label_kind)] * 0.04
            ),
        )
    ]
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


def toc_title_variants(title: str, ordinal: int | None = None) -> list[str]:
    """Return the full title plus only the fragment owned by this row ordinal."""

    variants = [str(title).strip()]
    if ordinal is None:
        return variants
    matches = list(re.finditer(r"(?<!\w)([0-9]{1,3})[.·):]+\s*", str(title)))
    for index, match in enumerate(matches):
        if int(match.group(1)) != ordinal:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(title)
        fragment = str(title)[match.start() : end].strip(" .·•-–—\t")
        without_ordinal = str(title)[match.end() : end].strip(" .·•-–—\t")
        for candidate in (fragment, without_ordinal):
            if len(normalize_text(candidate)) >= 5 and candidate not in variants:
                variants.append(candidate)
    return variants




def explicit_heading_anchor(
    analysis: PageAnalysis,
    row: TocRow,
) -> dict | None:
    """Confirm a parsed SECTION-range row from an exact section heading.

    The parser adds this row provenance only for a structured SECTION / Roman
    range / page-range layout. This deliberately does not turn a loose title
    match, a body-text mention, or another heading kind into a fallback.
    """

    if (
        row.structural_heading_kind != "section"
        or row.ordinal is None
        or row.ordinal <= 0
        or not analysis.lines
    ):
        return None
    expected_ordinal = int_to_roman(row.ordinal).casefold()
    heading_pattern = re.compile(
        rf"\bsection\s+{re.escape(expected_ordinal)}\b",
        re.I,
    )
    for line in analysis.lines:
        if not line:
            continue
        if min(float(word["top"]) for word in line) > analysis.height * 0.20:
            continue
        line_text = normalize_text(" ".join(str(word["text"]) for word in line))
        if heading_pattern.search(line_text) is not None:
            return {
                "heading_type": "section",
                "ordinal": row.ordinal,
            }
    return None


def likely_toc_line_count(analysis: PageAnalysis) -> int:
    count = 0
    for line in analysis.lines:
        if len(line) < 2 or float(line[-1]["x0"]) < analysis.width * 0.55:
            continue
        title = normalize_text(" ".join(str(word["text"]) for word in line[:-1]))
        if len(title) < 3 or title in TOC_HEADINGS:
            continue
        raw_label = str(line[-1]["text"])
        if page_label_candidates(raw_label) or page_range_candidates(raw_label):
            count += 1
    return count


def expand_continuation_toc_pages(
    analyses: Sequence[PageAnalysis],
    toc_pages: set[int],
    rows_by_page: Mapping[int, Sequence[TocRow]],
) -> set[int]:
    expanded = set(toc_pages)
    changed = True
    while changed:
        changed = False
        for page_index, analysis in enumerate(analyses):
            if page_index in expanded:
                continue
            adjacent = page_index - 1 in expanded or page_index + 1 in expanded
            if adjacent and len(rows_by_page.get(page_index, ())) >= 5:
                expanded.add(page_index)
                changed = True
    return expanded


def toc_completeness_diagnostics(
    analyses: Sequence[PageAnalysis],
    toc_pages: set[int],
    rows_by_page: dict[int, list[TocRow]],
    minimum_expected_by_page: Mapping[int, int] | None = None,
) -> tuple[list[dict], int]:
    diagnostics: list[dict] = []
    suspected_total = 0
    diagnostic_pages = set(toc_pages) | set((minimum_expected_by_page or {}).keys())
    for page_index in sorted(diagnostic_pages):
        analysis = analyses[page_index]
        parsed = len(rows_by_page.get(page_index, []))
        candidate_lines = likely_toc_line_count(analysis)
        navigation_layout = any(
            bool(word.get("ocr_navigation")) for word in analysis.words
        )
        prior_expected = int((minimum_expected_by_page or {}).get(page_index, 0))
        expected = max(parsed, prior_expected)
        if not navigation_layout:
            expected = max(
                expected,
                candidate_lines,
                analysis.ordinal_anchor_count,
            )
        suspected = max(0, expected - parsed)
        suspected_total += suspected
        diagnostics.append(
            {
                "page": page_index + 1,
                "parsed_rows": parsed,
                "candidate_lines": candidate_lines,
                "ordinal_anchors": analysis.ordinal_anchor_count,
                "minimum_expected_rows": prior_expected,
                "expected_rows": expected,
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
    trusted: set[int] = set()
    for observation in analysis.margin_labels:
        if not eligible_margin_observation(observation):
            continue
        candidates = page_label_candidates(observation.raw)
        if not candidates:
            candidates = [(observation.printed_number, observation.label_kind)]
        for printed_number, candidate_kind in candidates:
            if (
                candidate_kind == label_kind
                and (analysis.page_index + 1) - printed_number in offsets
            ):
                trusted.add(printed_number)
    return sorted(trusted)


def visible_margin_numbers(analysis: PageAnalysis, label_kind: str) -> list[int]:
    """Return visible edge labels credible enough to prove a conflict.

    ``margin_label_observations`` already limits candidates to the outer page
    bands and to conventional centred or outside-corner positions.  Keep the
    same bounds here so a real label cannot disappear in the narrow gap
    between pagination inference and destination validation.
    """

    return sorted(
        {
            observation.printed_number
            for observation in analysis.margin_labels
            if observation.label_kind == label_kind
            and eligible_margin_observation(observation)
        }
    )


def margin_ocr_region_for_observation(
    key: str,
    observation: PageLabelObservation,
) -> MarginOCRRegion | None:
    """Return a generous, isolated edge band around one visible label."""

    if (
        observation.x0 is None
        or observation.x1 is None
        or observation.top is None
        or observation.page_width is None
        or observation.page_height is None
        or observation.page_width <= 0
        or observation.page_height <= 0
    ):
        return None
    center_ratio = (
        (observation.x0 + observation.x1) / 2 / observation.page_width
    )
    label_width_ratio = (
        max(0.0, observation.x1 - observation.x0) / observation.page_width
    )
    label_top_ratio = observation.top / observation.page_height
    label_bottom_ratio = (
        (observation.bottom or observation.top) / observation.page_height
    )
    half_width = max(0.20, label_width_ratio * 3)
    x0_ratio = max(0.0, center_ratio - half_width)
    x1_ratio = min(1.0, center_ratio + half_width)
    vertical_padding = max(0.04, (label_bottom_ratio - label_top_ratio) * 3)
    top_ratio = max(0.0, label_top_ratio - vertical_padding)
    bottom_ratio = min(1.0, label_bottom_ratio + vertical_padding)
    if bottom_ratio - top_ratio < 0.09:
        midpoint = (top_ratio + bottom_ratio) / 2
        top_ratio = max(0.0, midpoint - 0.045)
        bottom_ratio = min(1.0, midpoint + 0.045)
    return MarginOCRRegion(
        key=key,
        page_index=observation.page_index,
        x0_ratio=x0_ratio,
        top_ratio=top_ratio,
        x1_ratio=x1_ratio,
        bottom_ratio=bottom_ratio,
    )


def conflicting_margin_ocr_checks(
    rows_by_page: Mapping[int, Sequence[TocRow]],
    toc_pages: set[int],
    blocks: Sequence[tuple[int, int]],
    segments: Sequence[PaginationSegment],
    analyses: Sequence[PageAnalysis],
) -> tuple[list[MarginOCRCheck], list[MarginOCRRegion]]:
    """Prepare OCR only for strong pagination candidates with edge conflicts."""

    checks: list[MarginOCRCheck] = []
    regions: dict[str, MarginOCRRegion] = {}
    seen_checks: set[tuple[int, int, str, str]] = set()
    for source_page in sorted(toc_pages):
        block_index = block_index_for_page(source_page, blocks)
        if block_index is None:
            continue
        allowed_pages = block_content_pages(block_index, blocks, len(analyses))
        for row in rows_by_page.get(source_page, ()):
            for segment in segments:
                if (
                    segment.block_index != block_index
                    or segment.label_kind != row.label_kind
                    or segment.evidence_pages < 3
                ):
                    continue
                destination = row.printed_number + segment.offset - 1
                near_segment = (
                    segment.printed_start - 4
                    <= row.printed_number
                    <= segment.printed_end + 4
                )
                if (
                    not near_segment
                    or destination not in allowed_pages
                    or not 0 <= destination < len(analyses)
                    or row.printed_number
                    in visible_margin_numbers(analyses[destination], row.label_kind)
                ):
                    continue
                for observation in analyses[destination].margin_labels:
                    if (
                        observation.label_kind != row.label_kind
                        or observation.printed_number == row.printed_number
                        or not eligible_margin_observation(observation)
                    ):
                        continue
                    key = (
                        f"{destination}:{row.label_kind}:"
                        f"{round(float(observation.x0 or 0.0), 1)}:"
                        f"{round(float(observation.top or 0.0), 1)}"
                    )
                    region = regions.get(key)
                    if region is None:
                        region = margin_ocr_region_for_observation(key, observation)
                        if region is None:
                            continue
                        regions[key] = region
                    signature = (
                        destination,
                        row.printed_number,
                        row.label_kind,
                        key,
                    )
                    if signature in seen_checks:
                        continue
                    seen_checks.add(signature)
                    checks.append(
                        MarginOCRCheck(
                            region=region,
                            expected_number=row.printed_number,
                            label_kind=row.label_kind,
                            observation=observation,
                        )
                    )
                    if region.x0_ratio <= 0.01 or region.x1_ratio >= 0.99:
                        center_key = f"{key}:center"
                        center_region = regions.get(center_key)
                        if center_region is None:
                            center_region = MarginOCRRegion(
                                key=center_key,
                                page_index=region.page_index,
                                x0_ratio=0.30,
                                top_ratio=0.90,
                                x1_ratio=0.70,
                                bottom_ratio=1.0,
                            )
                            regions[center_key] = center_region
                        center_signature = (
                            destination,
                            row.printed_number,
                            row.label_kind,
                            center_key,
                        )
                        if center_signature not in seen_checks:
                            seen_checks.add(center_signature)
                            checks.append(
                                MarginOCRCheck(
                                    region=center_region,
                                    expected_number=row.printed_number,
                                    label_kind=row.label_kind,
                                    observation=observation,
                                )
                            )
    return checks, list(regions.values())


def ocr_margin_numbers(
    tokens: Sequence[tuple[str, float]],
    label_kind: str,
) -> set[int]:
    """Return only high-confidence, standalone OCR page labels.

    OCR substitutions can turn normal words such as "is" into a plausible
    number. Margin confirmation deliberately accepts digit-only Arabic text;
    Roman labels keep their strict native parser.
    """

    numbers: set[int] = set()
    for raw, confidence in tokens:
        if float(confidence) < MARGIN_OCR_ACCEPTANCE_CONFIDENCE:
            continue
        token = str(raw).strip()
        if label_kind == "arabic":
            if re.fullmatch(r"[0-9]{1,4}", token):
                numbers.add(int(token))
            continue
        parsed = parse_page_label_details(token)
        if parsed is not None and parsed[1] == label_kind:
            numbers.add(parsed[0])
    return numbers


def confirmed_margin_ocr_labels(
    checks: Sequence[MarginOCRCheck],
    ocr_tokens: Mapping[str, Sequence[tuple[str, float]]],
) -> dict[tuple[int, int, str], dict]:
    """Keep exact visual confirmations separate from native page analysis."""

    confirmations: dict[tuple[int, int, str], dict] = {}
    for check in checks:
        tokens = list(ocr_tokens.get(check.region.key, ()))
        numbers = ocr_margin_numbers(tokens, check.label_kind)
        if numbers != {check.expected_number}:
            continue
        confirmation_key = (
            check.observation.page_index,
            check.expected_number,
            check.label_kind,
        )
        if confirmation_key in confirmations:
            continue
        confirmations[confirmation_key] = {
            "page": check.observation.page_index + 1,
            "expected": check.expected_number,
            "native_text_label": check.observation.printed_number,
            "label_kind": check.label_kind,
            "region": check.region.key,
            "ocr_tokens": [
                {"text": raw, "confidence": confidence}
                for raw, confidence in tokens
            ],
        }
    return confirmations






def merge_margin_ocr_confirmations(
    initial: Mapping[tuple[int, int, str], dict],
    retry: Mapping[tuple[int, int, str], dict],
) -> dict[tuple[int, int, str], dict]:
    """Keep an exact first-pass visual confirmation if a retry is weaker."""

    merged = dict(initial)
    for confirmation_key, confirmation in retry.items():
        merged.setdefault(confirmation_key, confirmation)
    return merged


def resolve_toc_row(
    row: TocRow,
    block_index: int,
    blocks: Sequence[tuple[int, int]],
    segments: Sequence[PaginationSegment],
    analyses: Sequence[PageAnalysis],
    normalized_pages: Sequence[str],
    toc_pages: set[int],
    visual_margin_confirmations: Mapping[tuple[int, int, str], dict] | None = None,
) -> tuple[int | None, str | None, str, dict]:
    page_count = len(analyses)
    allowed_content_pages = block_content_pages(block_index, blocks, page_count)
    candidates: list[tuple[PaginationSegment, int, bool]] = []
    for segment in segments:
        if (
            segment.block_index == block_index
            and segment.label_kind == row.label_kind
        ):
            destination = row.printed_number + segment.offset - 1
            if 0 <= destination < page_count and destination in allowed_content_pages:
                near_segment = (
                    segment.printed_start - 4
                    <= row.printed_number
                    <= segment.printed_end + 4
                )
                candidates.append((segment, destination, near_segment))

    scored: list[
        tuple[
            float,
            bool,
            bool,
            PaginationSegment,
            int,
            list[int],
            bool,
            float,
            bool,
            dict | None,
        ]
    ] = []
    for segment, destination, near_segment in candidates:
        trusted_observed = trusted_margin_numbers(
            analyses[destination], block_index, row.label_kind, segments
        )
        observed = visible_margin_numbers(analyses[destination], row.label_kind)
        visual_margin_confirmation = (visual_margin_confirmations or {}).get(
            (destination, row.printed_number, row.label_kind)
        )
        heading_anchor = explicit_heading_anchor(analyses[destination], row)
        structural_heading_confirmation = (
            heading_anchor
            if (
                heading_anchor is not None
                and near_segment
                and segment.evidence_pages >= 8
            )
            else None
        )
        exact_label = (
            row.printed_number in trusted_observed
            or visual_margin_confirmation is not None
            or structural_heading_confirmation is not None
        )
        conflicting_numbers = [
            number for number in observed if number != row.printed_number
        ]
        conflicting_label = bool(conflicting_numbers) and not exact_label
        title_variants = toc_title_variants(row.title, row.ordinal)
        variant_scores = [
            (title_match_score(variant, normalized_pages[destination]), variant)
            for variant in title_variants
        ]
        title_score, _ = max(variant_scores, default=(0.0, row.title))
        score = (2.0 if exact_label else 0.0) + title_score + min(0.5, segment.evidence_pages / 20)
        scored.append(
            (
                score,
                exact_label,
                conflicting_label,
                segment,
                destination,
                observed,
                near_segment,
                title_score,
            )
        )
    scored.sort(key=lambda item: (-item[0], -item[3].evidence_pages, item[4]))
    viable = [
        item
        for item in scored
        if not item[2]
        and (item[6] or (item[1] and item[7] == 1.0))
    ]
    if viable:
        chosen = viable[0]
        if len(viable) > 1 and abs(viable[0][0] - viable[1][0]) < 0.25:
            return None, None, "low", {
                "reason": "multiple pagination segments map this label",
                "candidate_target_pages": [item[4] + 1 for item in viable],
            }
        (
            _,
            exact_label,
            _,
            segment,
            destination,
            observed,
            _,
            title_score,
        ) = chosen
        confidence = "high" if exact_label or title_score >= 0.9 else "medium"
        return destination, "pagination-segment", confidence, {
            "block_index": block_index,
            "offset": segment.offset,
            "segment_printed_range": [segment.printed_start, segment.printed_end],
            "segment_evidence_pages": segment.evidence_pages,
            "segment_extrapolated": not (
                segment.printed_start <= row.printed_number <= segment.printed_end
            ),
            "target_margin_labels": observed,
            "target_title_score": round(title_score, 4),
            "row_ordinal": row.ordinal,
            "visual_margin_confirmation": visual_margin_confirmation,
            "explicit_heading_anchor": structural_heading_confirmation,
        }
    if scored:
        return None, None, "low", {
            "reason": "candidate destination has a conflicting printed page label",
            "candidate_target_pages": [item[4] + 1 for item in scored],
            "observed_labels": [item[5] for item in scored],
        }

    destination, score = unique_title_destination(
        row.title,
        normalized_pages,
        toc_pages,
        allowed_pages=allowed_content_pages,
    )
    if destination is not None:
        observed = visible_margin_numbers(analyses[destination], row.label_kind)
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


def canonicalize_toc_rows(
    rows_by_page: dict[int, list[TocRow]],
    toc_pages: set[int],
    blocks: Sequence[tuple[int, int]],
    segments: Sequence[PaginationSegment],
    analyses: Sequence[PageAnalysis],
    normalized_pages: Sequence[str],
) -> dict[int, list[TocRow]]:
    """Choose OCR label candidates using destination and neighbour evidence."""

    canonical = {page: list(rows) for page, rows in rows_by_page.items()}
    for block_index, (block_start, block_end) in enumerate(blocks):
        ordered: list[tuple[int, int, TocRow]] = []
        for page_index in range(block_start, block_end + 1):
            ordered.extend(
                (page_index, row_index, row)
                for row_index, row in enumerate(canonical.get(page_index, ()))
            )

        previous_by_kind: dict[str, int] = {}
        for position, (page_index, row_index, row) in enumerate(ordered):
            options = list(row.label_candidates) or [(row.printed_number, row.label_kind)]
            rotated_candidate = (
                rotated_page_label_candidate(row.printed_label)
                if row.navigation_ocr
                else None
            )
            if (
                rotated_candidate is not None
                and (rotated_candidate, "arabic") not in options
            ):
                options.append((rotated_candidate, "arabic"))
            options = list(dict.fromkeys(options))
            evaluated: list[tuple[float, int, str, int | None, dict]] = []
            for number, kind in options:
                if number <= 0:
                    continue
                previous = previous_by_kind.get(kind)
                if previous is not None and number < previous:
                    continue

                following_numbers = [
                    candidate_number
                    for _, _, following_row in ordered[position + 1 : position + 4]
                    for candidate_number, candidate_kind in (
                        list(following_row.label_candidates)
                        or [(following_row.printed_number, following_row.label_kind)]
                    )
                    if candidate_kind == kind
                ]
                if following_numbers and max(following_numbers) < number:
                    continue

                candidate_row = replace(
                    row,
                    printed_number=number,
                    label_kind=kind,
                    label_candidates=(),
                )
                destination, _, confidence, evidence = resolve_toc_row(
                    candidate_row,
                    block_index,
                    blocks,
                    segments,
                    analyses,
                    normalized_pages,
                    toc_pages,
                )
                is_primary_candidate = (number, kind) == (
                    row.printed_number,
                    row.label_kind,
                )
                if (
                    not is_primary_candidate
                    and destination is not None
                    and number not in evidence.get("target_margin_labels", [])
                ):
                    destination = None
                score = 0.0
                if destination is not None:
                    score += 100.0
                    score += 20.0 if confidence == "high" else 8.0
                    if number in evidence.get("target_margin_labels", []):
                        score += 20.0
                    score += float(evidence.get("target_title_score", 0.0)) * 12.0
                else:
                    if any(
                        segment.block_index == block_index
                        and segment.label_kind == kind
                        and 0 <= number + segment.offset - 1 < len(analyses)
                        for segment in segments
                    ):
                        score += 2.0
                if (number, kind) == (row.printed_number, row.label_kind):
                    score += 0.25
                evaluated.append((score, number, kind, destination, evidence))

            if not evaluated:
                continue
            primary_resolved = any(
                number == row.printed_number
                and kind == row.label_kind
                and destination is not None
                for _, number, kind, destination, _ in evaluated
            )
            if primary_resolved and rotated_candidate is not None:
                evaluated = [
                    item
                    for item in evaluated
                    if not (
                        item[1] == rotated_candidate
                        and item[2] == "arabic"
                        and item[1] != row.printed_number
                    )
                ]
            evaluated.sort(key=lambda item: (-item[0], item[1], item[2]))
            _, number, kind, _, _ = evaluated[0]
            canonical_row = replace(
                row,
                printed_number=number,
                label_kind=kind,
            )
            canonical[page_index][row_index] = canonical_row
            previous_by_kind[kind] = number
    return canonical


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
            rect = visual_rect_to_pdf(
                float(word["x0"]),
                float(word["top"]),
                float(word["x1"]),
                float(word["bottom"]),
                width=analysis.width,
                height=analysis.height,
                rotation=analysis.rotation,
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
    emit_progress(
        "PREFLIGHT",
        completed=1,
        total=1,
        unit="check",
        total_pages=page_count,
        message=f"Opened {page_count}-page PDF safely",
    )
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
    rows_by_page: dict[int, list[TocRow]] = {}
    headingless_navigation_pages: list[int] = []
    detected_offset: int | None = None
    offset_score = 0
    mode = "automatic-analysis"
    pagination_segments: list[PaginationSegment] = []
    pagination_diagnostics: list[dict] = []
    completeness_diagnostics: list[dict] = []
    suspected_unparsed_rows = 0
    nontext_visual_pages: list[int] = []
    parsed_toc_rows = 0
    covered_existing_rows = 0
    ocr_summary: OCRSummary | None = None
    navigation_ocr_summary: NavigationOCRSummary | None = None
    margin_ocr_summary: MarginOCRSummary | None = None
    margin_ocr_retry_summary: MarginOCRSummary | None = None
    margin_ocr_confirmations: dict[tuple[int, int, str], dict] = {}
    preliminary_expected_by_page: dict[int, int] = {}
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
            rotations = [
                int(page.get("/Rotate", 0) or 0) % 360 for page in reader.pages
            ]
            ocr_replacements, ocr_summary = ocr_low_text_pages(
                input_path,
                analyses,
                password=args.password,
                rotations=rotations,
                on_selection=lambda selected, total: emit_progress(
                    "OCR_PAGES",
                    completed=0,
                    total=selected,
                    unit="page",
                    total_pages=page_count,
                    message=(
                        f"Existing text is ready on {total - selected} of {total} pages; "
                        f"local OCR is needed on {selected} "
                        f"page{'s' if selected != 1 else ''}"
                    ),
                    counts={
                        "native_or_blank_pages": total - selected,
                        "ocr_pages": selected,
                    },
                ),
                on_page_started=lambda completed, total, page_index: emit_progress(
                    "OCR_PAGES",
                    completed=min(total, completed + 1),
                    total=total,
                    unit="page",
                    page=page_index + 1,
                    total_pages=page_count,
                    message=f"Scanning page {page_index + 1} with local OCR",
                ),
                on_page_finished=lambda completed, total, page_index, words: emit_progress(
                    "OCR_PAGES",
                    completed=completed,
                    total=total,
                    unit="page",
                    page=page_index + 1,
                    total_pages=page_count,
                    message=(
                        f"Finished OCR on page {page_index + 1}"
                        if words
                        else f"No readable text found on page {page_index + 1}"
                    ),
                    counts={"words_found": words},
                ),
            )
            for page_index, ocr_words in ocr_replacements.items():
                replace_analysis_words(analyses[page_index], ocr_words, page_count)
            explicit_toc_pages = parse_page_list(args.toc_pages, page_count)
            toc_pages, rows_by_page = detect_toc_pages(analyses, explicit_toc_pages)
            preliminary_completeness, _ = toc_completeness_diagnostics(
                analyses, toc_pages, rows_by_page
            )
            preliminary_expected_by_page = {
                int(item["page"]) - 1: int(item["expected_rows"])
                for item in preliminary_completeness
            }
            full_page_ocr_enriched = set(ocr_summary.enriched_pages)
            navigation_ocr_pages = [
                int(item["page"]) - 1
                for item in preliminary_completeness
                if analyses[int(item["page"]) - 1].image_area_ratio >= 0.55
                and (
                    int(item["suspected_unparsed_rows"]) > 0
                    or (
                        int(item["parsed_rows"]) >= 8
                        and int(item["page"]) not in full_page_ocr_enriched
                        and not has_section_range_layout(
                            analyses[int(item["page"]) - 1].words
                        )
                    )
                )
            ]
            if navigation_ocr_pages and not args.no_toc_links:
                emit_progress(
                    "OCR_PAGES",
                    completed=0,
                    total=len(navigation_ocr_pages),
                    unit="page",
                    message=(
                        f"Rechecking the page-number column on "
                        f"{len(navigation_ocr_pages)} contents pages"
                    ),
                    counts={"navigation_pages": len(navigation_ocr_pages)},
                )
                navigation_words, navigation_ocr_summary = ocr_navigation_columns(
                    input_path,
                    analyses,
                    navigation_ocr_pages,
                    password=args.password,
                    rotations=rotations,
                )
                for page_index, label_words in navigation_words.items():
                    replace_analysis_words(
                        analyses[page_index],
                        [*analyses[page_index].words, *label_words],
                        page_count,
                    )
                emit_progress(
                    "OCR_PAGES",
                    completed=len(navigation_ocr_pages),
                    total=len(navigation_ocr_pages),
                    unit="page",
                    message="Finished rechecking contents page numbers",
                    counts={
                        "navigation_pages": len(navigation_ocr_pages),
                        "navigation_pages_with_words": len(
                            navigation_ocr_summary.pages_with_words
                        ),
                    },
                )
            toc_pages, rows_by_page = detect_toc_pages(
                analyses, explicit_toc_pages
            )
            headingless_navigation_pages = headingless_navigation_candidates(
                analyses,
                toc_pages,
                rows_by_page,
            )
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
            rows_by_page = canonicalize_toc_rows(
                rows_by_page,
                toc_pages,
                blocks,
                pagination_segments,
                analyses,
                normalized_pages,
            )
            if not args.no_toc_links:
                margin_checks, margin_regions = conflicting_margin_ocr_checks(
                    rows_by_page,
                    toc_pages,
                    blocks,
                    pagination_segments,
                    analyses,
                )
                if margin_regions:
                    emit_progress(
                        "OCR_PAGES",
                        completed=0,
                        total=len(margin_regions),
                        unit="check",
                        total_pages=page_count,
                        message=(
                            "Visually checking "
                            f"{len(margin_regions)} uncertain page-number check"
                            f"{'s' if len(margin_regions) != 1 else ''}"
                        ),
                        counts={
                            "margin_checks": len(margin_checks),
                            "margin_regions": len(margin_regions),
                        },
                    )
                    try:
                        margin_ocr_tokens, margin_ocr_summary = ocr_margin_regions(
                            input_path,
                            analyses,
                            margin_regions,
                            password=args.password,
                            rotations=rotations,
                        )
                        margin_ocr_confirmations = confirmed_margin_ocr_labels(
                            margin_checks,
                            margin_ocr_tokens,
                        )
                        retry_keys = {
                            check.region.key
                            for check in margin_checks
                            if (
                                check.observation.page_index,
                                check.expected_number,
                                check.label_kind,
                            )
                            not in margin_ocr_confirmations
                        }
                        # Retry only a tighter footer crop. This is visual
                        # confirmation, not another way to infer a destination.
                        retry_regions = [
                            MarginOCRRegion(
                                key=region.key,
                                page_index=region.page_index,
                                x0_ratio=0.30,
                                top_ratio=0.90,
                                x1_ratio=0.70,
                                bottom_ratio=1.0,
                            )
                            for region in margin_regions
                            if region.key in retry_keys
                        ]
                        if retry_regions:
                            retry_tokens, margin_ocr_retry_summary = (
                                ocr_margin_regions(
                                    input_path,
                                    analyses,
                                    retry_regions,
                                    password=args.password,
                                    rotations=rotations,
                                    render_scale=3.0,
                                    batch_size=1,
                                )
                            )
                            retry_confirmations = confirmed_margin_ocr_labels(
                                margin_checks,
                                retry_tokens,
                            )
                            margin_ocr_confirmations = (
                                merge_margin_ocr_confirmations(
                                    margin_ocr_confirmations,
                                    retry_confirmations,
                                )
                            )
                        emit_progress(
                            "OCR_PAGES",
                            completed=len(margin_regions),
                            total=len(margin_regions),
                            unit="check",
                            total_pages=page_count,
                            message=(
                                "Finished visual page-number checks "
                                f"({len(margin_ocr_confirmations)} confirmed)"
                            ),
                            counts={
                                "margin_checks": len(margin_checks),
                                "margin_regions": len(margin_regions),
                                "margin_confirmed": len(
                                    margin_ocr_confirmations
                                ),
                            },
                        )
                    except (RuntimeError, ValueError) as exc:
                        message = (
                            "Visual page-number verification could not complete: "
                            f"{exc}"
                        )
                        warnings.append(message)
                        review_reasons.append(message)
            headingless_navigation_pages = headingless_navigation_candidates(
                analyses,
                toc_pages,
                rows_by_page,
            )
            if not toc_pages and not args.no_toc_links:
                # A contents page converted to outlines or a page-sized image has no
                # readable words. Inspect only likely front matter so ordinary image
                # pages later in a document do not make the whole job ambiguous.
                for page_index in range(min(page_count, 12)):
                    if normalized_pages[page_index].strip():
                        continue
                    page = reader.pages[page_index]
                    contents = page.get_contents()
                    try:
                        content_bytes = len(contents.get_data()) if contents is not None else 0
                    except (AttributeError, OSError, TypeError, ValueError):
                        content_bytes = 0
                    resources = page.get("/Resources") or {}
                    try:
                        xobjects = resources.get("/XObject")
                        if hasattr(xobjects, "get_object"):
                            xobjects = xobjects.get_object()
                        has_xobjects = bool(xobjects)
                    except (AttributeError, KeyError, TypeError, ValueError):
                        has_xobjects = False
                    if content_bytes >= 4096 or has_xobjects:
                        nontext_visual_pages.append(page_index)

            parsed_toc_rows = sum(len(rows_by_page[page]) for page in toc_pages)
            completeness_diagnostics, suspected_unparsed_rows = toc_completeness_diagnostics(
                analyses,
                toc_pages,
                rows_by_page,
                preliminary_expected_by_page,
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
            if (
                headingless_navigation_pages
                and explicit_toc_pages is None
                and not args.no_toc_links
            ):
                displayed_pages = ", ".join(
                    str(page_index + 1) for page_index in headingless_navigation_pages
                )
                message = (
                    f"Pages {displayed_pages} look like navigation lists but have no clear "
                    "heading. Select those pages explicitly before adding page jumps."
                )
                warnings.append(message)
                review_reasons.append(message)

            if not args.no_toc_links:
                planned_row_number = 0
                for source_page in sorted(toc_pages):
                    for row in rows_by_page[source_page]:
                        planned_row_number += 1
                        emit_progress(
                            "PLANNING_LINKS",
                            completed=planned_row_number - 1,
                            total=parsed_toc_rows,
                            unit="link",
                            page=source_page + 1,
                            total_pages=page_count,
                            message=(
                                f"Matching contents entry {planned_row_number} "
                                f"of {parsed_toc_rows}"
                            ),
                        )
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
                            visual_margin_confirmations=margin_ocr_confirmations,
                        )
                        if destination is None or destination == source_page:
                            unresolved.append(
                                {
                                    "source_page": source_page + 1,
                            "title": row.title,
                            "printed_label": row.printed_label,
                            "canonical_printed_number": row.printed_number,
                            "row_ordinal": row.ordinal,
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
                                "canonical_printed_number": row.printed_number,
                                "row_ordinal": row.ordinal,
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

                emit_progress(
                    "PLANNING_LINKS",
                    completed=parsed_toc_rows,
                    total=parsed_toc_rows,
                    unit="link",
                    total_pages=page_count,
                    message=f"Matched {len(added)} safe links",
                    counts={"links_planned": len(added)},
                )

            if not args.no_url_links:
                for analysis in analyses:
                    for word in analysis.words:
                        raw = str(word["text"])
                        if normalized_uri(raw, allow_bare_domains=True) and not normalized_uri(raw):
                            skipped_bare_domain_candidates += 1
                for link in visible_url_candidates(
                    analyses, allow_bare_domains=args.allow_bare_domains
                ):
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

        if ocr_summary is not None and ocr_summary.failed_pages:
            pages = ", ".join(str(page) for page in ocr_summary.failed_pages)
            message = f"Local OCR could not complete on page(s): {pages}."
            warnings.append(message)
            review_reasons.append(message)
        if navigation_ocr_summary is not None and navigation_ocr_summary.failed_pages:
            pages = ", ".join(
                str(page) for page in navigation_ocr_summary.failed_pages
            )
            message = (
                "Page-number column OCR could not complete on page(s): "
                f"{pages}."
            )
            warnings.append(message)
            review_reasons.append(message)

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
        if len(nontext_visual_pages) >= 2 and not existing_counts["internal"]:
            pages = ", ".join(str(page + 1) for page in nontext_visual_pages)
            message = (
                "Visually populated front-matter pages have no extractable text "
                f"({pages}). Their navigation cannot be checked safely; use OCR or a "
                "reviewed link manifest."
            )
            warnings.append(message)
            review_reasons.append(message)
    if (
        toc_pages
        and not parsed_toc_rows
        and not args.no_toc_links
        and not reference_path
        and not manifest_path
    ):
        message = (
            "The selected navigation pages contain no readable TOC rows. Use OCR or a "
            "reviewed link manifest."
        )
        warnings.append(message)
        review_reasons.append(message)
    ambiguous_unresolved_labels = [
        item
        for item in unresolved
        if parse_page_label_details(str(item.get("printed_label", ""))) is None
        and page_label_candidates(str(item.get("printed_label", "")))
    ]
    if ambiguous_unresolved_labels:
        suspected_unparsed_rows = max(
            suspected_unparsed_rows,
            len(ambiguous_unresolved_labels),
        )
    if unresolved:
        message = f"{len(unresolved)} detected TOC rows could not be safely mapped."
        warnings.append(message)
        review_reasons.append(message)
    if (
        not args.no_toc_links
        and navigation_rows_without_internal_links(
            toc_pages,
            rows_by_page,
            existing_counts,
            added,
        )
    ):
        message = (
            "Navigation rows were detected, but no internal page links were retained."
        )
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
        "ocr": (
            {
                "used": bool(ocr_summary.attempted_pages)
                or bool(
                    navigation_ocr_summary
                    and navigation_ocr_summary.attempted_pages
                )
                or bool(
                    margin_ocr_summary
                    and margin_ocr_summary.attempted_pages
                ),
                **ocr_summary.as_report(),
                "navigation_recheck": (
                    navigation_ocr_summary.as_report()
                    if navigation_ocr_summary is not None
                    else {"attempted_pages": []}
                ),
                "margin_recheck": {
                    "initial": (
                        margin_ocr_summary.as_report()
                        if margin_ocr_summary is not None
                        else {"attempted_pages": []}
                    ),
                    "retry": (
                        margin_ocr_retry_summary.as_report()
                        if margin_ocr_retry_summary is not None
                        else {"attempted_pages": []}
                    ),
                    "confirmations": [
                        confirmation
                        for _, confirmation in sorted(
                            margin_ocr_confirmations.items()
                        )
                    ],
                },
            }
            if ocr_summary is not None
            else {"used": False}
        ),
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
        "nontext_visual_pages": [page + 1 for page in nontext_visual_pages],
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
    emit_progress(
        "WRITING_LINKS",
        completed=0,
        total=len(added),
        unit="link",
        total_pages=page_count,
        message=f"Adding {len(added)} verified link areas",
    )
    for link_number, link in enumerate(added, start=1):
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
        emit_progress(
            "WRITING_LINKS",
            completed=link_number,
            total=len(added),
            unit="link",
            page=link.source_page + 1,
            total_pages=page_count,
            message=f"Added link {link_number} of {len(added)}",
            counts={"links_added": link_number},
        )

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
            emit_progress(
                "VALIDATING_PAGES",
                completed=index,
                total=page_count,
                unit="page",
                page=index,
                total_pages=page_count,
                message=f"Validated page {index} of {page_count}",
            )

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
