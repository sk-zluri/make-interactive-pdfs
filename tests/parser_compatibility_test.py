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
) -> linker.PageAnalysis:
    page_words = list(words or [])
    lines = linker.group_words_into_lines(page_words)
    raw_text = " ".join(str(item["text"]) for row in lines for item in row)
    return linker.PageAnalysis(
        page_index=page_index,
        width=PAGE_WIDTH,
        height=PAGE_HEIGHT,
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
    def test_ocr_like_chapter_and_section_ordinals_are_confirmed(self) -> None:
        self.assertTrue(
            linker.page_has_chapter_heading(
                "the ramayana chapter i3 kaikeyi disregards the king",
                13,
            )
        )
        self.assertTrue(
            linker.page_has_chapter_heading(
                "section ccx rajya labha parva",
                210,
            )
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

    def test_repeated_centered_footer_glyph_error_can_be_safely_overridden(self) -> None:
        analyses = [analysis(index) for index in range(10)]

        def footer(page_index: int, raw: str, text: str = "") -> linker.PageAnalysis:
            return analysis(
                page_index,
                normalized_text=text,
                margin_labels=[
                    linker.PageLabelObservation(
                        page_index,
                        int(raw),
                        "arabic",
                        raw,
                        top=PAGE_HEIGHT * 0.92,
                        bottom=PAGE_HEIGHT * 0.92 + 12.0,
                        x0=292.0,
                        x1=308.0,
                        page_width=PAGE_WIDTH,
                        page_height=PAGE_HEIGHT,
                    )
                ],
            )

        analyses[5] = footer(5, "512", "chapter 69 the death of lavana")
        analyses[6] = footer(6, "513")
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

        destination, _, _, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertEqual(destination, 5)
        self.assertEqual(
            evidence["footer_ocr_substitution_override"]["substitution"], "7->1"
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

    def test_isolated_text_layer_digit_error_does_not_block_a_stable_segment(self) -> None:
        analyses = [analysis(index) for index in range(8)]
        for page_index, printed_number in ((3, 103), (5, 105)):
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
        analyses[4] = analysis(
            4,
            margin_labels=[
                linker.PageLabelObservation(4, 114, "arabic", "114")
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

        destination, _, _, _ = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertEqual(destination, 4)

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

    def test_chapter_heading_confirms_a_one_page_footer_mismatch(self) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            normalized_text="chapter 26 unreadable scanned heading",
            margin_labels=[linker.PageLabelObservation(5, 6, "arabic", "6")],
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

        destination, _, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            [item.normalized_text for item in analyses],
            {0},
        )

        self.assertEqual(destination, 5)
        self.assertEqual(confidence, "high")
        self.assertTrue(evidence.get("ordinal_heading_match"))
        self.assertTrue(evidence.get("one_page_label_override"))

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

    def test_adjacent_titles_can_confirm_a_one_page_label_override(self) -> None:
        analyses = [analysis(index) for index in range(10)]
        analyses[5] = analysis(
            5,
            normalized_text="sarana tells his story",
            margin_labels=[linker.PageLabelObservation(5, 6, "arabic", "6")],
        )
        normalized_pages = [item.normalized_text for item in analyses]
        row = linker.TocRow(
            0,
            "25. Other chapter 26. Sarana tells his story 27. Next chapter",
            "5",
            5,
            "arabic",
            (10, 10, 100, 30),
            ordinal=26,
        )
        segments = [
            linker.PaginationSegment(0, "arabic", 1, 10, 1, 8, 1.0),
        ]

        destination, matched_by, confidence, evidence = linker.resolve_toc_row(
            row,
            0,
            [(0, 0)],
            segments,
            analyses,
            normalized_pages,
            {0},
        )

        self.assertEqual(destination, 5)
        self.assertEqual(matched_by, "pagination-segment")
        self.assertEqual(confidence, "high")
        self.assertTrue(evidence.get("one_page_label_override"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
