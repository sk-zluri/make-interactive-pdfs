#!/usr/bin/env python3
"""Focused parser regressions distilled from the five-PDF compatibility corpus."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import make_interactive_pdf as linker  # noqa: E402


PAGE_WIDTH = 600.0
PAGE_HEIGHT = 800.0


def word(text: str, x0: float, top: float, *, width: float | None = None) -> dict:
    """Return the minimal pdfplumber-like word used by parser unit tests."""

    measured_width = width if width is not None else max(7.0, len(text) * 6.0)
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + measured_width,
        "top": top,
        "bottom": top + 12.0,
    }


def line(top: float, left_text: str, right_text: str) -> list[dict]:
    return [word(left_text, 50.0, top), word(right_text, 510.0, top)]


def analysis(
    page_index: int,
    words: list[dict] | None = None,
    *,
    normalized_text: str | None = None,
    margin_labels: list[linker.PageLabelObservation] | None = None,
    width: float = PAGE_WIDTH,
    height: float = PAGE_HEIGHT,
) -> linker.PageAnalysis:
    page_words = list(words or [])
    lines = linker.group_words_into_lines(page_words)
    raw_text = " ".join(str(item["text"]) for row in lines for item in row)
    return linker.PageAnalysis(
        page_index=page_index,
        width=width,
        height=height,
        rotation=0,
        image_area_ratio=0.0,
        words=page_words,
        lines=lines,
        raw_text=raw_text,
        normalized_text=(
            linker.normalize_text(raw_text)
            if normalized_text is None
            else normalized_text
        ),
        nontext_object_count=0,
        content_stream_bytes=0,
        margin_labels=list(margin_labels or []),
        ordinal_anchor_count=0,
    )


class TocDetectionRegressions(unittest.TestCase):
    def test_named_volume_contents_heading_is_supported(self) -> None:
        toc_words = [
            word("Contents", 50.0, 30.0),
            word("Volume", 120.0, 30.0),
            word("1", 180.0, 30.0),
            *line(90.0, "First", "1"),
            *line(115.0, "Second", "2"),
        ]

        toc_pages, _ = linker.detect_toc_pages([analysis(0, toc_words)], None)

        self.assertEqual(toc_pages, {0})

    def test_contents_of_volume_heading_is_supported_across_two_lines(self) -> None:
        toc_words = [
            word("CONTENTS", 50.0, 30.0),
            word("OF", 118.0, 30.0),
            word("VOLUME", 50.0, 48.0),
            word("II.", 120.0, 48.0),
            *line(105.0, "Nine Days", "7"),
            *line(130.0, "Chapter Nineteen", "16"),
        ]

        toc_pages, _ = linker.detect_toc_pages([analysis(0, toc_words)], None)

        self.assertEqual(toc_pages, {0})

    def test_scanned_contents_with_malformed_labels_requests_narrow_ocr(self) -> None:
        scanned_page = analysis(0)
        scanned_page.image_area_ratio = 0.75
        corrupt_rows = [
            linker.TocRow(0, "First", "7", 7, "arabic", (10, 10, 100, 30)),
            linker.TocRow(0, "Second", "9'", 9, "arabic", (10, 40, 100, 60)),
        ]
        clean_rows = [
            linker.TocRow(0, "First", "7", 7, "arabic", (10, 10, 100, 30)),
            linker.TocRow(0, "Second", "9", 9, "arabic", (10, 40, 100, 60)),
        ]

        self.assertTrue(
            linker.toc_page_needs_label_repair_ocr(scanned_page, corrupt_rows)
        )
        self.assertFalse(
            linker.toc_page_needs_label_repair_ocr(scanned_page, clean_rows)
        )

    def test_contents_inside_prose_is_not_a_toc_heading(self) -> None:
        page_words = [
            word("This", 50.0, 35.0),
            word("chapter", 85.0, 35.0),
            word("discusses", 140.0, 35.0),
            word("contents", 205.0, 35.0),
            *line(100.0, "First incidental reference", "1"),
            *line(125.0, "Second incidental reference", "2"),
        ]

        toc_pages, _ = linker.detect_toc_pages([analysis(0, page_words)], None)

        self.assertEqual(toc_pages, set())

    def test_adjacent_prose_with_near_roman_words_is_not_a_continuation(self) -> None:
        toc_words = [
            word("CONTENTS", 50.0, 30.0),
            *line(90.0, "Chapter One", "1"),
            *line(115.0, "Chapter Two", "2"),
        ]
        prose_words: list[dict] = []
        for top, ending in zip(
            (80.0, 105.0, 130.0, 155.0, 180.0),
            ("him", "mind", "claim", "civil", "mild"),
            strict=True,
        ):
            prose_words.extend(line(top, "Ordinary sentence ending with", ending))

        toc_pages, _ = linker.detect_toc_pages(
            [analysis(0, toc_words), analysis(1, prose_words)],
            None,
        )

        self.assertEqual(toc_pages, {0})


class TocSafetyRegressions(unittest.TestCase):
    def test_split_heading_seeds_only_parsed_continuation_pages(self) -> None:
        first_page_words = [
            word("TABLE", 230.0, 25.0),
            word("OF CONTENTS", 205.0, 42.0),
            *line(90.0, "Opening chapter", "1"),
            *line(115.0, "Second chapter", "2"),
        ]
        continuation_words: list[dict] = []
        for index, top in enumerate((70.0, 95.0, 120.0, 145.0, 170.0), start=3):
            continuation_words.extend(line(top, f"Chapter {index}", str(index)))
        numbered_body_words: list[dict] = []
        for index, top in enumerate((80.0, 110.0, 140.0, 170.0), start=1):
            numbered_body_words.extend(line(top, f"Body item {index}", str(index)))

        first = analysis(0, first_page_words)
        continuation = analysis(1, continuation_words)
        numbered_body = analysis(2, numbered_body_words)
        numbered_body.ordinal_anchor_count = 8

        toc_pages, rows_by_page = linker.detect_toc_pages(
            [first, continuation, numbered_body],
            None,
        )

        self.assertEqual(toc_pages, {0, 1})
        self.assertEqual(len(rows_by_page[1]), 5)
        heading = linker.toc_heading_line(first.words, first.height)
        self.assertIsNotNone(heading)
        self.assertEqual(
            [item["text"] for item in heading or []],
            ["TABLE", "OF CONTENTS"],
        )

    def test_isolated_headingless_numeric_page_fails_closed(self) -> None:
        page_words: list[dict] = []
        for index, top in enumerate(range(70, 270, 25), start=1):
            page_words.extend(line(float(top), f"Numbered body item {index}", str(index)))

        toc_pages, rows_by_page = linker.detect_toc_pages(
            [analysis(0, page_words)],
            None,
        )

        self.assertEqual(toc_pages, set())
        self.assertEqual(
            linker.headingless_navigation_candidates(
                [analysis(0, page_words)],
                toc_pages,
                rows_by_page,
            ),
            [0],
        )

    def test_external_annotation_cannot_mask_missing_page_jumps(self) -> None:
        row = linker.TocRow(
            0,
            "Opening chapter",
            "1",
            1,
            "arabic",
            (10, 10, 100, 30),
        )

        self.assertTrue(
            linker.navigation_rows_without_internal_links(
                {0},
                {0: [row]},
                {"internal": 0, "external": 1},
                [],
            )
        )
        self.assertFalse(
            linker.navigation_rows_without_internal_links(
                {0},
                {0: [row]},
                {"internal": 1, "external": 1},
                [],
            )
        )


class SemanticRowRegressions(unittest.TestCase):
    def test_section_range_preserves_starting_ordinal(self) -> None:
        page_words = [
            word("SECTION", 180.0, 40.0),
            word("CLIX-CLXV", 260.0, 40.0),
            word("Parva Vaka-Badha", 50.0, 70.0),
            word("367-380", 510.0, 70.0),
        ]
        page = SimpleNamespace(width=PAGE_WIDTH, height=PAGE_HEIGHT, rotation=0)

        rows = linker.toc_rows_for_page(2, page, page_words)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].printed_number, 367)
        self.assertEqual(rows[0].ordinal, 159)

    def test_compact_ranges_require_a_contiguous_section_range_layout(self) -> None:
        page_words = [word("CONTENTS", 50.0, 25.0), word("Page", 500.0, 45.0)]
        for top, title, label in (
            (80.0, "First", "117"),
            (120.0, "Second", "17-35"),
            (160.0, "Third", "3648"),
            (200.0, "Fourth", "49-60"),
        ):
            page_words.extend(
                [
                    word("SECTION", 180.0, top - 12.0),
                    word(title, 50.0, top),
                    word(label, 510.0, top),
                ]
            )
        page = SimpleNamespace(width=PAGE_WIDTH, height=PAGE_HEIGHT, rotation=0)

        rows = linker.toc_rows_for_page(2, page, page_words)

        self.assertEqual([row.printed_number for row in rows], [1, 17, 36, 49])

    def test_section_metadata_and_arabic_range_form_one_semantic_row(self) -> None:
        page_words = [
            word("SECTION", 220.0, 40.0),
            word("I-II", 300.0, 40.0),
            word("Introductory", 50.0, 70.0),
            word("1-17", 510.0, 70.0),
        ]
        page = SimpleNamespace(width=PAGE_WIDTH, height=PAGE_HEIGHT, rotation=0)

        rows = linker.toc_rows_for_page(2, page, page_words)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.source_page, 2)
        self.assertEqual(row.title, "Introductory")
        self.assertEqual(row.printed_label, "1-17")
        self.assertEqual(row.printed_number, 1)
        self.assertEqual(row.label_kind, "arabic")
        self.assertLessEqual(row.rect[1], PAGE_HEIGHT - 82.0)
        self.assertGreaterEqual(row.rect[3], PAGE_HEIGHT - 40.0)

    def test_plain_native_numbers_are_not_guessed_as_compact_ranges(self) -> None:
        page_words = []
        for top, label in ((70.0, "12"), (110.0, "30"), (150.0, "45")):
            page_words.extend(
                [
                    word("SECTION", 50.0, top),
                    word(f"Title {label}", 180.0, top),
                    word(label, 510.0, top),
                ]
            )
        page = SimpleNamespace(width=PAGE_WIDTH, height=PAGE_HEIGHT, rotation=0)

        rows = linker.toc_rows_for_page(2, page, page_words)

        self.assertEqual([row.printed_number for row in rows], [12, 30, 45])


class NavigationRowRegressions(unittest.TestCase):
    def test_rotated_alternatives_do_not_split_rows_or_capture_heading(self) -> None:
        page_words = [word("CONTENTS", 50.0, 30.0)]
        for top, title, label in (
            (100.0, "First chapter", "12"),
            (180.0, "Second chapter", "30"),
        ):
            page_words.append(word(title, 50.0, top, width=180.0))
            label_word = word(label, 520.0, top)
            label_word["ocr_navigation"] = True
            page_words.append(label_word)
        page = SimpleNamespace(width=PAGE_WIDTH, height=PAGE_HEIGHT, rotation=0)

        rows = linker.toc_rows_for_page(2, page, page_words)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row.printed_number for row in rows], [12, 30])
        self.assertNotIn("contents", rows[0].title.lower())
        self.assertEqual(linker.rect_overlap_ratio(rows[0].rect, rows[1].rect), 0.0)

    def test_navigation_ocr_cannot_erase_prior_missing_row_evidence(self) -> None:
        label_word = word("12", 520.0, 100.0)
        label_word["ocr_navigation"] = True
        page_analysis = analysis(
            0,
            [word("CONTENTS", 50.0, 30.0), word("First", 50.0, 100.0), label_word],
        )
        row = linker.TocRow(
            0,
            "First",
            "12",
            12,
            "arabic",
            (10, 10, 100, 30),
            navigation_ocr=True,
        )

        diagnostics, suspected = linker.toc_completeness_diagnostics(
            [page_analysis],
            {0},
            {0: [row]},
            {0: 3},
        )

        self.assertEqual(suspected, 2)
        self.assertEqual(diagnostics[0]["expected_rows"], 3)


class LabelCandidateRegressions(unittest.TestCase):
    def test_ocr_confusions_are_candidates_but_not_strict_labels(self) -> None:
        self.assertEqual(linker.parse_page_label_details("I"), (1, "roman"))
        self.assertIsNone(linker.parse_page_label_details("Ill"))
        self.assertIsNone(linker.parse_page_label_details("XVU"))

        self.assertEqual(
            linker.page_label_candidates("I"),
            [(1, "roman"), (1, "arabic")],
        )
        self.assertEqual(
            linker.page_label_candidates("Ill"),
            [(111, "arabic"), (3, "roman")],
        )
        self.assertEqual(
            linker.page_label_candidates("XVU"),
            [(17, "roman")],
        )
        self.assertEqual(linker.page_label_candidates("601"), [(601, "arabic")])
        self.assertEqual(linker.rotated_page_label_candidate("601"), 109)


class DestinationResolutionRegressions(unittest.TestCase):
    def test_sparse_title_only_opener_can_resolve_a_damaged_contents_title(self) -> None:
        analyses = [analysis(index) for index in range(5)]
        analyses[1] = analysis(
            1,
            [
                word("Stave", 250.0, 305.0),
                word("One", 300.0, 305.0),
                word("MARLEY'S", 220.0, 330.0),
                word("GHOST", 300.0, 330.0),
            ],
        )
        for page_index in (2, 3):
            analyses[page_index] = analysis(
                page_index,
                [
                    word("MARLEY'S", 50.0, 35.0),
                    word("GHOST", 130.0, 35.0),
                    *[
                        word(f"body{ordinal}", 50.0, 80.0 + ordinal * 14.0)
                        for ordinal in range(24)
                    ],
                ],
            )
        row = linker.TocRow(
            0,
            "Marlev's Ghost",
            "7",
            7,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertEqual(destination, 1)
        self.assertEqual(matched_by, "title-only-opener")
        self.assertEqual(confidence, "high")
        self.assertTrue(evidence["title_only_opener"])

    def test_title_only_opener_requires_one_unambiguous_candidate(self) -> None:
        analyses = [analysis(index) for index in range(4)]
        for page_index in (1, 2):
            analyses[page_index] = analysis(
                page_index,
                [word("UNIQUE", 220.0, 330.0), word("CHAPTER", 285.0, 330.0)],
            )
        row = linker.TocRow(
            0,
            "Unique Chapter",
            "7",
            7,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, matched_by, confidence, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)
        self.assertIsNone(matched_by)
        self.assertEqual(confidence, "low")

    def test_section_page_label_resolves_restarted_technical_pagination(self) -> None:
        analyses = [analysis(index) for index in range(4)]
        analyses[1] = analysis(
            1,
            [
                word("MISSION", 50.0, 40.0),
                word("SUMMARY", 110.0, 40.0),
                word("FLIGHT", 180.0, 40.0),
                word("PLAN", 230.0, 40.0),
                word("1", 285.0, 760.0),
                word("-", 301.0, 760.0),
                word("5", 315.0, 760.0),
            ],
        )
        analyses[2] = analysis(
            2,
            [
                word("MISSION", 50.0, 40.0),
                word("SUMMARY", 110.0, 40.0),
                word("FLIGHT", 180.0, 40.0),
                word("PLAN", 230.0, 40.0),
                word("1", 285.0, 760.0),
                word("-", 301.0, 760.0),
                word("6", 315.0, 760.0),
            ],
        )
        row = linker.TocRow(
            0,
            "Summary Flight Plan",
            "1-5",
            1,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            section_page_label_mode=True,
        )

        self.assertEqual(destination, 1)
        self.assertEqual(matched_by, "section-page-label")
        self.assertEqual(confidence, "high")
        self.assertEqual(evidence["section_page_label"], "1-5")

    def test_unmatched_section_page_label_cannot_fall_back_to_plain_page_number(self) -> None:
        analyses = [analysis(index) for index in range(4)]
        row = linker.TocRow(
            0,
            "Summary Flight Plan",
            "1-5",
            1,
            "arabic",
            (10, 10, 100, 30),
        )
        segment = linker.PaginationSegment(
            block_index=0,
            label_kind="arabic",
            printed_start=1,
            printed_end=4,
            offset=1,
            evidence_pages=3,
            density=1.0,
        )

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [segment],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            section_page_label_mode=True,
        )

        self.assertIsNone(destination)
        self.assertIsNone(matched_by)
        self.assertEqual(confidence, "low")
        self.assertIn("technical section-page label", evidence["reason"])

    def test_duplicate_section_page_label_stays_unresolved(self) -> None:
        analyses = [analysis(index) for index in range(4)]
        for page_index in (1, 2):
            analyses[page_index] = analysis(
                page_index,
                [
                    word("SUMMARY", 50.0, 40.0),
                    word("FLIGHT", 120.0, 40.0),
                    word("PLAN", 190.0, 40.0),
                    word("1", 285.0, 760.0),
                    word("-", 301.0, 760.0),
                    word("5", 315.0, 760.0),
                ],
            )
        row = linker.TocRow(
            0,
            "Summary Flight Plan",
            "1-5",
            1,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, matched_by, confidence, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            section_page_label_mode=True,
        )

        self.assertIsNone(destination)
        self.assertIsNone(matched_by)
        self.assertEqual(confidence, "low")

    def test_unique_section_page_label_allows_imperfect_technical_title(self) -> None:
        analyses = [analysis(index) for index in range(3)]
        analyses[1] = analysis(
            1,
            [
                word("SUMMARY", 50.0, 40.0),
                word("1", 285.0, 760.0),
                word("-", 301.0, 760.0),
                word("5", 315.0, 760.0),
            ],
        )
        row = linker.TocRow(
            0,
            "Summary Flight Plan",
            "1-5",
            1,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            section_page_label_mode=True,
        )

        self.assertEqual(destination, 1)
        self.assertEqual(matched_by, "section-page-label")
        self.assertEqual(confidence, "high")
        self.assertGreaterEqual(evidence["target_title_score"], 0.30)

    def test_landscape_outer_edge_section_label_is_detected(self) -> None:
        landscape = analysis(
            1,
            [
                word("1", 36.0, 288.0),
                word("-", 36.0, 302.0),
                word("5", 36.0, 316.0),
                word("MCC4", 790.0, 288.0),
                word("-22", 790.0, 302.0),
            ],
            width=842.0,
            height=595.0,
        )

        self.assertEqual(linker.visible_section_page_labels(landscape), {"1-5"})

    def test_margin_ocr_numbers_require_high_confidence_standalone_labels(
        self,
    ) -> None:
        self.assertEqual(linker.ocr_margin_numbers([("120", 0.95)], "arabic"), {120})
        self.assertEqual(linker.ocr_margin_numbers([("120", 0.89)], "arabic"), set())
        self.assertEqual(linker.ocr_margin_numbers([("is", 1.0)], "arabic"), set())
        self.assertEqual(
            linker.ocr_margin_numbers([("120", 0.99), ("121", 0.99)], "arabic"),
            {120, 121},
        )

    def test_segment_prediction_cannot_ignore_a_visible_footer_at_92_percent(self) -> None:
        analyses = [analysis(index) for index in range(5)]
        analyses[2] = analysis(
            2,
            margin_labels=[
                linker.PageLabelObservation(
                    2,
                    6,
                    "arabic",
                    "6",
                    top=PAGE_HEIGHT * 0.92,
                    bottom=PAGE_HEIGHT * 0.92 + 12.0,
                    x0=292.0,
                    x1=308.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        row = linker.TocRow(
            0,
            "Unmatched chapter title",
            "5",
            5,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [linker.PaginationSegment(0, "arabic", 1, 10, -2, 8, 1.0)]

        destination, _, _, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)
        self.assertEqual(
            evidence.get("reason"),
            "candidate destination has a conflicting printed page label",
        )

    def test_outer_corner_page_number_is_visible_conflict_evidence(self) -> None:
        page_analysis = analysis(
            2,
            margin_labels=[
                linker.PageLabelObservation(
                    2,
                    6,
                    "arabic",
                    "6",
                    top=PAGE_HEIGHT * 0.05,
                    bottom=PAGE_HEIGHT * 0.05 + 12.0,
                    x0=560.0,
                    x1=572.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )

        self.assertEqual(linker.visible_margin_numbers(page_analysis, "arabic"), [6])

    def test_inline_corner_footnote_digits_are_not_page_label_conflicts(self) -> None:
        left_words = [
            word("1", 20.0, 720.0, width=5.0),
            word("Footnote", 28.0, 720.0),
        ]
        right_words = [
            word("Footnote", 530.0, 720.0, width=25.0),
            word("2", 560.0, 720.0, width=5.0),
        ]
        left_words[0]["bottom"] = 724.0
        right_words[1]["bottom"] = 724.0
        left = analysis(
            0,
            left_words,
            margin_labels=linker.margin_label_observations(
                0, PAGE_WIDTH, PAGE_HEIGHT, left_words, 10
            ),
        )
        right = analysis(
            1,
            right_words,
            margin_labels=linker.margin_label_observations(
                1, PAGE_WIDTH, PAGE_HEIGHT, right_words, 10
            ),
        )

        self.assertEqual(linker.visible_margin_numbers(left, "arabic"), [])
        self.assertEqual(linker.visible_margin_numbers(right, "arabic"), [])

    def test_isolated_corner_and_centered_footer_numbers_remain_visible(self) -> None:
        corner_words = [
            word("6", 560.0, 40.0, width=8.0),
            word("Running header", 400.0, 40.0, width=80.0),
        ]
        center_words = [
            word("7", 294.0, 720.0, width=12.0),
            word("Nearby text", 310.0, 720.0, width=60.0),
        ]
        corner = analysis(
            0,
            corner_words,
            margin_labels=linker.margin_label_observations(
                0, PAGE_WIDTH, PAGE_HEIGHT, corner_words, 10
            ),
        )
        centered = analysis(
            1,
            center_words,
            margin_labels=linker.margin_label_observations(
                1, PAGE_WIDTH, PAGE_HEIGHT, center_words, 10
            ),
        )

        self.assertEqual(linker.visible_margin_numbers(corner, "arabic"), [6])
        self.assertEqual(linker.visible_margin_numbers(centered, "arabic"), [7])

        close_header_words = [
            word("8", 20.0, 40.0, width=8.0),
            word("CHAPTER", 30.0, 40.0, width=50.0),
        ]
        close_header = analysis(
            2,
            close_header_words,
            margin_labels=linker.margin_label_observations(
                2, PAGE_WIDTH, PAGE_HEIGHT, close_header_words, 10
            ),
        )
        self.assertEqual(linker.visible_margin_numbers(close_header, "arabic"), [8])

    def test_inline_footnotes_cannot_seed_or_trust_pagination(self) -> None:
        analyses = [analysis(index) for index in range(16)]
        for page_index in range(1, 15):
            analyses[page_index] = analysis(
                page_index,
                margin_labels=[
                    linker.PageLabelObservation(
                        page_index,
                        page_index,
                        "arabic",
                        str(page_index),
                        has_inline_text_neighbor=True,
                    )
                ],
            )
        segments, _ = linker.infer_pagination_segments(analyses, {0})
        self.assertEqual(segments, [])

        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            margin_labels=[
                linker.PageLabelObservation(
                    5,
                    4,
                    "arabic",
                    "4",
                    has_inline_text_neighbor=True,
                ),
                linker.PageLabelObservation(5, 200, "arabic", "200"),
            ],
        )
        analyses[6] = analysis(
            6,
            margin_labels=[
                linker.PageLabelObservation(6, 201, "arabic", "201")
            ],
        )
        row = linker.TocRow(
            0,
            "Unmatched chapter title",
            "4",
            4,
            "arabic",
            (10, 10, 100, 30),
        )
        trusted_segment = [
            linker.PaginationSegment(0, "arabic", 1, 10, 2, 8, 1.0)
        ]

        destination, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            trusted_segment,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)

    def test_unrelated_neighbor_cannot_erase_a_visible_conflict(self) -> None:
        row = linker.TocRow(
            0,
            "Unmatched chapter title",
            "104",
            104,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 100, 108, -99, 8, 1.0)
        ]
        for neighbour_number in (999, 105):
            with self.subTest(neighbour_number=neighbour_number):
                analyses = [analysis(index) for index in range(8)]
                analyses[4] = analysis(
                    4,
                    margin_labels=[
                        linker.PageLabelObservation(4, 200, "arabic", "200")
                    ],
                )
                analyses[5] = analysis(
                    5,
                    margin_labels=[
                        linker.PageLabelObservation(
                            5,
                            neighbour_number,
                            "arabic",
                            str(neighbour_number),
                        )
                    ],
                )

                destination, _, _, _ = linker.resolve_toc_row(
                    row,
                    0,
                    [(0, 0)],
                    segments,
                    analyses,
                    [item.normalized_text for item in analyses],
                    {0},
                )

                self.assertIsNone(destination)

    def test_visual_margin_ocr_confirmation_can_clear_a_footer_conflict(
        self,
    ) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            margin_labels=[
                linker.PageLabelObservation(
                    5,
                    512,
                    "arabic",
                    "512",
                    top=PAGE_HEIGHT * 0.92,
                    bottom=PAGE_HEIGHT * 0.94,
                    x0=292.0,
                    x1=308.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        row = linker.TocRow(
            0,
            "The Death of Lavana",
            "572",
            572,
            "arabic",
            (10, 10, 100, 30),
            ordinal=69,
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 560, 580, -566, 10, 1.0)
        ]
        confirmation = {
            (5, 572, "arabic"): {
                "expected": 572,
                "native_text_label": 512,
                "ocr_tokens": [{"text": "572", "confidence": 0.99}],
            }
        }

        destination, _, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            visual_margin_confirmations=confirmation,
        )

        self.assertEqual(destination, 5)
        self.assertEqual(confidence, "high")
        self.assertEqual(
            evidence["visual_margin_confirmation"]["native_text_label"], 512
        )

    def test_footer_glyph_error_without_matching_neighbor_stays_blocked(self) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            normalized_text="chapter 69 the death of lavana",
            margin_labels=[
                linker.PageLabelObservation(
                    5,
                    512,
                    "arabic",
                    "512",
                    top=PAGE_HEIGHT * 0.92,
                    bottom=PAGE_HEIGHT * 0.92 + 12.0,
                    x0=292.0,
                    x1=308.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        row = linker.TocRow(
            0,
            "The Death of Lavana",
            "572",
            572,
            "arabic",
            (10, 10, 100, 30),
            ordinal=69,
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 560, 580, -566, 10, 1.0)
        ]

        destination, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)

    def test_isolated_text_layer_digit_error_requires_visual_confirmation(
        self,
    ) -> None:
        analyses = [analysis(index) for index in range(8)]
        analyses[4] = analysis(
            4,
            margin_labels=[
                linker.PageLabelObservation(
                    4,
                    114,
                    "arabic",
                    "114",
                    top=PAGE_HEIGHT * 0.92,
                    bottom=PAGE_HEIGHT * 0.94,
                    x0=292.0,
                    x1=308.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        row = linker.TocRow(
            0,
            "A chapter title",
            "104",
            104,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 100, 108, -99, 8, 1.0)
        ]

        blocked, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )
        confirmed, _, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            visual_margin_confirmations={(4, 104, "arabic"): {"expected": 104}},
        )

        self.assertIsNone(blocked)
        self.assertEqual(confirmed, 4)
        self.assertEqual(confidence, "high")
        self.assertEqual(evidence["visual_margin_confirmation"]["expected"], 104)

    def test_locally_sequenced_conflicting_footer_still_blocks_a_segment(self) -> None:
        analyses = [analysis(index) for index in range(8)]
        for page_index, printed_number in ((3, 203), (4, 204), (5, 205)):
            analyses[page_index] = analysis(
                page_index,
                margin_labels=[
                    linker.PageLabelObservation(
                        page_index,
                        printed_number,
                        "arabic",
                        str(printed_number),
                    )
                ],
            )
        row = linker.TocRow(
            0,
            "Unmatched chapter title",
            "104",
            104,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 100, 108, -99, 8, 1.0)
        ]

        destination, _, _, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)
        self.assertEqual(
            evidence.get("reason"),
            "candidate destination has a conflicting printed page label",
        )

    def test_one_page_footer_conflict_requires_visual_confirmation(
        self,
    ) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            normalized_text="chapter 26 unreadable scanned heading",
            margin_labels=[
                linker.PageLabelObservation(5, 6, "arabic", "6")
            ],
        )
        row = linker.TocRow(
            0,
            "Garbled title words",
            "5",
            5,
            "arabic",
            (10, 10, 100, 30),
            ordinal=26,
        )
        segments = [linker.PaginationSegment(0, "arabic", 1, 10, 1, 8, 1.0)]

        destination, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )
        confirmed, _, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
            visual_margin_confirmations={(5, 5, "arabic"): {"expected": 5}},
        )

        self.assertIsNone(destination)
        self.assertEqual(confirmed, 5)
        self.assertEqual(confidence, "high")
        self.assertEqual(evidence["visual_margin_confirmation"]["expected"], 5)

    def test_unique_title_cannot_bypass_a_visible_conflicting_footer(self) -> None:
        analyses = [analysis(index) for index in range(4)]
        analyses[2] = analysis(
            2,
            normalized_text="unique chapter title",
            margin_labels=[linker.PageLabelObservation(2, 6, "arabic", "6")],
        )
        row = linker.TocRow(
            0,
            "Unique chapter title",
            "5",
            5,
            "arabic",
            (10, 10, 100, 30),
        )

        destination, _, _, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)
        self.assertEqual(
            evidence.get("reason"),
            "title match conflicts with destination page label",
        )

    def test_segment_extrapolation_cannot_cross_into_next_toc_block(self) -> None:
        analyses = [analysis(index) for index in range(25)]
        analyses[20] = analysis(
            20,
            normalized_text="outside chapter",
            margin_labels=[linker.PageLabelObservation(20, 20, "arabic", "20")],
        )
        row = linker.TocRow(
            0,
            "Outside chapter",
            "20",
            20,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [linker.PaginationSegment(0, "arabic", 1, 5, 1, 5, 1.0)]

        destination, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0), (10, 10)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0, 10},
        )

        self.assertIsNone(destination)

    def test_far_segment_extrapolation_requires_exact_title_and_margin_label(self) -> None:
        analyses = [analysis(index) for index in range(40)]
        analyses[9] = analysis(
            9,
            normalized_text="foreword",
            margin_labels=[linker.PageLabelObservation(9, 9, "roman", "IX")],
        )
        # A duplicate title defeats the generic unique-title fallback. The safe
        # result must come from the segment-predicted page's exact IX + title.
        analyses[30] = analysis(30, normalized_text="foreword")
        normalized_pages = [item.normalized_text for item in analyses]
        row = linker.TocRow(0, "Foreword", "IX", 9, "roman", (10, 10, 100, 30))
        segments = [linker.PaginationSegment(0, "roman", 15, 28, 1, 14, 1.0)]

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            normalized_pages,
            {0},
        )

        self.assertEqual(destination, 9)
        self.assertIsNotNone(matched_by)
        self.assertEqual(confidence, "high")
        self.assertTrue(evidence.get("segment_extrapolated"))
        self.assertEqual(evidence.get("target_margin_labels"), [9])
        self.assertEqual(evidence.get("target_title_score"), 1.0)

    def test_adjacent_titles_cannot_bypass_a_visible_conflicting_footer(
        self,
    ) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            normalized_text="sarana tells ravana the principal leaders of the monkeys",
            margin_labels=[
                linker.PageLabelObservation(5, 6, "arabic", "6")
            ],
        )
        row = linker.TocRow(
            0,
            "25. Other chapter 26. Sarana tells story 27. Next chapter",
            "5",
            5,
            "arabic",
            (10, 10, 100, 30),
            ordinal=26,
        )
        segments = [linker.PaginationSegment(0, "arabic", 1, 10, 1, 8, 1.0)]

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertIsNone(destination)
        self.assertIsNone(matched_by)
        self.assertEqual(confidence, "low")
        self.assertIn("conflicting printed page label", evidence["reason"])


class ExplicitHeadingAnchorRegressionTests(unittest.TestCase):
    def test_explicit_top_heading_confirms_a_restarted_pagination_section(self) -> None:
        analyses = [analysis(index) for index in range(8)]
        analyses[5] = analysis(
            5,
            [
                word("SECTION", 190.0, 60.0),
                word("CCX", 285.0, 60.0),
                word("Rajya-labha", 175.0, 82.0),
                word("Parva", 290.0, 82.0),
            ],
            margin_labels=[
                linker.PageLabelObservation(
                    5,
                    59,
                    "arabic",
                    "59",
                    top=PAGE_HEIGHT * 0.95,
                    bottom=PAGE_HEIGHT * 0.97,
                    x0=40.0,
                    x1=55.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        row = linker.TocRow(
            0,
            "CCX-CCXIV Parva Rajya-labha",
            "3-12",
            3,
            "arabic",
            (10.0, 10.0, 100.0, 30.0),
            ordinal=210,
            structural_heading_kind="section",
        )
        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            [linker.PaginationSegment(0, "arabic", 2, 519, 3, 332, 0.64)],
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertEqual(destination, 5)
        self.assertEqual(matched_by, "pagination-segment")
        self.assertEqual(confidence, "high")
        self.assertEqual(evidence["explicit_heading_anchor"]["heading_type"], "section")

    def test_identical_heading_without_structural_row_provenance_stays_blocked(self) -> None:
        row = linker.TocRow(
            0,
            "CCX-CCXIV Parva Rajya-labha",
            "3-12",
            3,
            "arabic",
            (10.0, 10.0, 100.0, 30.0),
            ordinal=210,
        )
        candidate = analysis(
            5,
            [
                word("SECTION", 190.0, 60.0),
                word("CCX", 285.0, 60.0),
                word("Rajya-labha", 175.0, 82.0),
                word("Parva", 290.0, 82.0),
            ],
        )

        self.assertIsNone(linker.explicit_heading_anchor(candidate, row))

    def test_wrong_section_ordinal_cannot_count_as_an_explicit_heading(self) -> None:
        row = linker.TocRow(
            0,
            "CCX-CCXIV Parva Rajya-labha",
            "3-12",
            3,
            "arabic",
            (10.0, 10.0, 100.0, 30.0),
            ordinal=210,
            structural_heading_kind="section",
        )
        candidate = analysis(
            5,
            [
                word("SECTION", 190.0, 60.0),
                word("CCXI", 285.0, 60.0),
            ],
        )

        self.assertIsNone(linker.explicit_heading_anchor(candidate, row))


class MarginOCRRegressionTests(unittest.TestCase):
    def test_confirmation_requires_one_exact_high_confidence_label(self) -> None:
        observation = linker.PageLabelObservation(
            4,
            114,
            "arabic",
            "114",
            top=PAGE_HEIGHT * 0.92,
            bottom=PAGE_HEIGHT * 0.94,
            x0=292.0,
            x1=308.0,
            page_width=PAGE_WIDTH,
            page_height=PAGE_HEIGHT,
        )
        region = linker.margin_ocr_region_for_observation("footer", observation)
        self.assertIsNotNone(region)
        assert region is not None
        check = linker.MarginOCRCheck(
            region=region,
            expected_number=104,
            label_kind="arabic",
            observation=observation,
        )

        confirmed = linker.confirmed_margin_ocr_labels(
            [check],
            {"footer": [("104", 0.99)]},
        )
        ambiguous = linker.confirmed_margin_ocr_labels(
            [check],
            {"footer": [("104", 0.99), ("114", 0.99)]},
        )
        low_confidence = linker.confirmed_margin_ocr_labels(
            [check],
            {"footer": [("104", 0.89)]},
        )

        self.assertIn((4, 104, "arabic"), confirmed)
        self.assertEqual(ambiguous, {})
        self.assertEqual(low_confidence, {})
        self.assertLess(region.bottom_ratio - region.top_ratio, 0.20)
        self.assertLess(region.x1_ratio - region.x0_ratio, 0.45)

    def test_retry_confirmation_cannot_erase_first_pass_confirmation(self) -> None:
        key = (4, 104, "arabic")
        first_pass = {key: {"region": "footer", "expected": 104}}
        retry = {key: {"region": "footer:retry", "expected": 104}}

        merged = linker.merge_margin_ocr_confirmations(first_pass, retry)

        self.assertEqual(merged[key]["region"], "footer")

    def test_requests_are_limited_to_strong_conflicting_destinations(self) -> None:
        analyses = [analysis(index) for index in range(8)]
        conflict = linker.PageLabelObservation(
            4,
            114,
            "arabic",
            "114",
            top=PAGE_HEIGHT * 0.92,
            bottom=PAGE_HEIGHT * 0.94,
            x0=292.0,
            x1=308.0,
            page_width=PAGE_WIDTH,
            page_height=PAGE_HEIGHT,
        )
        analyses[4] = analysis(4, margin_labels=[conflict])
        row = linker.TocRow(
            0,
            "A chapter title",
            "104",
            104,
            "arabic",
            (10, 10, 100, 30),
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 100, 108, -99, 8, 1.0)
        ]

        checks, regions = linker.conflicting_margin_ocr_checks(
            {0: [row]},
            {0},
            [(0, 0)],
            segments,
            analyses,
        )
        analyses[4] = analysis(
            4,
            margin_labels=[
                linker.PageLabelObservation(
                    4,
                    104,
                    "arabic",
                    "104",
                    top=PAGE_HEIGHT * 0.92,
                    bottom=PAGE_HEIGHT * 0.94,
                    x0=292.0,
                    x1=308.0,
                    page_width=PAGE_WIDTH,
                    page_height=PAGE_HEIGHT,
                )
            ],
        )
        no_checks, no_regions = linker.conflicting_margin_ocr_checks(
            {0: [row]},
            {0},
            [(0, 0)],
            segments,
            analyses,
        )

        self.assertEqual(len(checks), 1)
        self.assertEqual(len(regions), 1)
        self.assertEqual(checks[0].expected_number, 104)
        self.assertEqual(no_checks, [])
        self.assertEqual(no_regions, [])

    def test_outer_margin_conflict_also_checks_the_center_footer(self) -> None:
        analyses = [analysis(index) for index in range(8)]
        outer = linker.PageLabelObservation(
            4,
            1,
            "arabic",
            "1",
            top=PAGE_HEIGHT * 0.90,
            bottom=PAGE_HEIGHT * 0.92,
            x0=18.0,
            x1=22.0,
            page_width=PAGE_WIDTH,
            page_height=PAGE_HEIGHT,
        )
        analyses[4] = analysis(4, margin_labels=[outer])
        row = linker.TocRow(
            0,
            "A chapter title",
            "104",
            104,
            "arabic",
            (10, 10, 100, 30),
        )
        checks, regions = linker.conflicting_margin_ocr_checks(
            {0: [row]},
            {0},
            [(0, 0)],
            [linker.PaginationSegment(0, "arabic", 100, 108, -99, 8, 1.0)],
            analyses,
        )
        center = next(region for region in regions if region.key.endswith(":center"))
        confirmations = linker.confirmed_margin_ocr_labels(
            checks,
            {
                checks[0].region.key: [("1", 0.99)],
                center.key: [("104", 0.99)],
            },
        )

        self.assertEqual(len(regions), 2)
        self.assertEqual(center.x0_ratio, 0.30)
        self.assertIn((4, 104, "arabic"), confirmations)


if __name__ == "__main__":
    unittest.main(verbosity=2)
