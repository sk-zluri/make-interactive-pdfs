from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import local_ocr


def _box(x0: float, top: float, x1: float, bottom: float) -> list[list[float]]:
    return [[x0, top], [x1, top], [x1, bottom], [x0, bottom]]


class _FakePILImage:
    def __init__(self, pixels: np.ndarray):
        self.pixels = pixels

    def convert(self, mode: str) -> "_FakePILImage":
        if mode != "RGB":
            raise AssertionError(mode)
        return self

    def __array__(self, dtype: object = None, copy: object = None) -> np.ndarray:
        del copy
        return np.asarray(self.pixels, dtype=dtype)


class _FakeBitmap:
    def __init__(self, pixels: np.ndarray):
        self.pixels = pixels

    def to_pil(self) -> _FakePILImage:
        return _FakePILImage(self.pixels)


class _FakePage:
    def __init__(self, width: float, height: float, rotation: int, fill: int):
        self.width = width
        self.height = height
        self.rotation = rotation
        self.fill = fill
        self.render_calls: list[tuple[float, tuple[float, float, float, float]]] = []

    def get_rotation(self) -> int:
        return self.rotation

    def get_size(self) -> tuple[float, float]:
        return self.width, self.height

    def render(
        self,
        *,
        scale: float,
        crop: tuple[float, float, float, float],
    ) -> _FakeBitmap:
        self.render_calls.append((scale, crop))
        pixel_width = round((self.width - crop[0] - crop[2]) * scale)
        pixel_height = round((self.height - crop[1] - crop[3]) * scale)
        pixels = np.full((pixel_height, pixel_width, 3), self.fill, dtype=np.uint8)
        return _FakeBitmap(pixels)


class _FakeDocument:
    def __init__(self, pages: list[_FakePage]):
        self.pages = pages
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, page_index: int) -> _FakePage:
        return self.pages[page_index]

    def close(self) -> None:
        self.closed = True


class _FakeRapidOCR:
    results: list[object] = []
    instances: list["_FakeRapidOCR"] = []

    def __init__(self, *, params: dict[str, object]):
        self.params = params
        self.calls: list[np.ndarray] = []
        type(self).instances.append(self)

    def __call__(self, image: np.ndarray, *, return_word_box: bool) -> object:
        if not return_word_box:
            raise AssertionError("Word boxes must be requested")
        self.calls.append(image.copy())
        return type(self).results[len(self.calls) - 1]


class NavigationOCRTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeRapidOCR.instances.clear()
        _FakeRapidOCR.results = []

    def _module_context(
        self,
        pages: list[_FakePage],
        results: list[object],
    ) -> tuple[object, _FakeDocument, dict[str, object]]:
        document = _FakeDocument(pages)
        opened: dict[str, object] = {}

        def open_document(path: str, *, password: str | None) -> _FakeDocument:
            opened.update(path=path, password=password)
            return document

        pdfium = types.ModuleType("pypdfium2")
        pdfium.PdfDocument = open_document
        rapidocr = types.ModuleType("rapidocr")
        rapidocr.__file__ = str(REPO / "fake-rapidocr" / "__init__.py")
        rapidocr.RapidOCR = _FakeRapidOCR
        _FakeRapidOCR.results = results
        context = patch.dict(
            sys.modules,
            {"pypdfium2": pdfium, "rapidocr": rapidocr},
        )
        return context, document, opened

    def test_batches_four_pages_and_maps_boxes_to_full_page(self) -> None:
        pages = [_FakePage(200.0, 100.0, 0, 20 + index) for index in range(5)]
        analyses = [
            SimpleNamespace(page_index=index, width=200.0, height=100.0, rotation=0)
            for index in range(5)
        ]
        first_batch_words = [
            ("10", 0.99, _box(9, 30, 90, 60)),
            ("20", 0.98, _box(147, 30, 228, 60)),
            ("30", 0.97, _box(285, 30, 366, 60)),
            ("40", 0.96, _box(423, 30, 504, 60)),
            ("separator", 0.99, _box(116, 30, 126, 60)),
            ("low-confidence", 0.20, _box(9, 90, 90, 120)),
        ]
        results = [
            SimpleNamespace(word_results=(tuple(first_batch_words),)),
            SimpleNamespace(
                word_results=((("50", 0.95, _box(9, 30, 90, 60)),),),
            ),
        ]
        context, document, opened = self._module_context(pages, results)
        ticks = iter(index / 10 for index in range(16))
        with (
            context,
            patch.object(local_ocr.metadata, "version", return_value="test-version"),
            patch.object(local_ocr, "_model_fingerprint", return_value="model-hash"),
            patch.object(local_ocr, "perf_counter", side_effect=lambda: next(ticks)),
        ):
            detected, summary = local_ocr.ocr_navigation_columns(
                Path("fixture.pdf"),
                analyses,
                [4, 2, 0, 3, 1, 2],
                password="secret",
                rotations=[0, 0, 0, 0, 0],
            )

        self.assertEqual(opened, {"path": "fixture.pdf", "password": "secret"})
        self.assertTrue(document.closed)
        self.assertEqual(set(detected), {0, 1, 2, 3, 4})
        self.assertEqual([word["text"] for word in detected[0]], ["10"])
        self.assertEqual([word["text"] for word in detected[4]], ["50"])
        self.assertTrue(
            all(
                word["ocr_navigation"]
                for words in detected.values()
                for word in words
            )
        )
        self.assertAlmostEqual(detected[0][0]["x0"], 165.0)
        self.assertAlmostEqual(detected[0][0]["x1"], 192.0)
        self.assertAlmostEqual(detected[0][0]["top"], 10.0)
        self.assertAlmostEqual(detected[0][0]["bottom"], 20.0)

        engine = _FakeRapidOCR.instances[0]
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[0].shape, (300, 528, 3))
        self.assertEqual(engine.calls[1].shape, (300, 114, 3))
        self.assertTrue(np.all(engine.calls[0][:, 114:138] == 255))
        self.assertEqual(
            pages[0].render_calls,
            [(3.0, (162.0, 0.0, 0.0, 0.0))],
        )

        self.assertEqual(summary.attempted_pages, (1, 2, 3, 4, 5))
        self.assertEqual(summary.rendered_pages, (1, 2, 3, 4, 5))
        self.assertEqual(summary.pages_with_words, (1, 2, 3, 4, 5))
        self.assertEqual(summary.failed_pages, ())
        self.assertEqual(summary.batch_attempts, ((1, 2, 3, 4), (5,)))
        self.assertEqual(summary.page_word_counts, ((1, 1), (2, 1), (3, 1), (4, 1), (5, 1)))
        self.assertEqual(summary.render_seconds, 0.5)
        self.assertEqual(summary.ocr_seconds, 0.2)
        self.assertEqual(summary.total_seconds, 1.5)
        report = summary.as_report()
        self.assertEqual(report["page_word_counts"], {"1": 1, "2": 1, "3": 1, "4": 1, "5": 1})
        self.assertEqual(report["model_sha256"], "model-hash")

    def test_intrinsically_rotated_page_is_cropped_in_visual_coordinates(self) -> None:
        page = _FakePage(100.0, 200.0, 90, 40)
        analysis = SimpleNamespace(
            page_index=0,
            width=100.0,
            height=200.0,
            rotation=90,
        )
        results = [
            SimpleNamespace(
                word_results=((("ix", 0.99, _box(6, 60, 30, 90)),),),
            )
        ]
        context, document, _ = self._module_context([page], results)
        ticks = iter(index / 10 for index in range(6))
        with (
            context,
            patch.object(local_ocr.metadata, "version", return_value="test-version"),
            patch.object(local_ocr, "_model_fingerprint", return_value="model-hash"),
            patch.object(local_ocr, "perf_counter", side_effect=lambda: next(ticks)),
        ):
            detected, summary = local_ocr.ocr_navigation_columns(
                Path("rotated.pdf"),
                [analysis],
                [0],
                rotations=[90],
            )

        self.assertTrue(document.closed)
        self.assertEqual(page.render_calls, [(3.0, (81.0, 0.0, 0.0, 0.0))])
        self.assertAlmostEqual(detected[0][0]["x0"], 83.0)
        self.assertAlmostEqual(detected[0][0]["top"], 20.0)
        self.assertEqual(summary.failed_pages, ())

    def test_rotation_mismatch_fails_closed_without_ocr(self) -> None:
        page = _FakePage(100.0, 200.0, 90, 40)
        analysis = SimpleNamespace(
            page_index=0,
            width=100.0,
            height=200.0,
            rotation=0,
        )
        context, document, _ = self._module_context([page], [])
        ticks = iter(index / 10 for index in range(4))
        with (
            context,
            patch.object(local_ocr.metadata, "version", return_value="test-version"),
            patch.object(local_ocr, "_model_fingerprint", return_value="model-hash"),
            patch.object(local_ocr, "perf_counter", side_effect=lambda: next(ticks)),
        ):
            detected, summary = local_ocr.ocr_navigation_columns(
                Path("mismatch.pdf"),
                [analysis],
                [0],
                rotations=[0],
            )

        self.assertTrue(document.closed)
        self.assertEqual(detected, {})
        self.assertEqual(summary.rendered_pages, ())
        self.assertEqual(summary.failed_pages, (1,))
        self.assertEqual(summary.batch_attempts, ())
        self.assertEqual(_FakeRapidOCR.instances[0].calls, [])

    def test_batch_size_cannot_exceed_four(self) -> None:
        analysis = SimpleNamespace(page_index=0, width=100.0, height=100.0)
        with self.assertRaisesRegex(ValueError, "between 1 and 4"):
            local_ocr.ocr_navigation_columns(
                Path("fixture.pdf"),
                [analysis],
                [0],
                batch_size=5,
            )


    def test_margin_regions_batch_and_keep_results_isolated(self) -> None:
        pages = [_FakePage(200.0, 100.0, 0, 20 + index) for index in range(5)]
        analyses = [
            SimpleNamespace(page_index=index, width=200.0, height=100.0, rotation=0)
            for index in range(5)
        ]
        regions = [
            local_ocr.MarginOCRRegion(
                f"footer-{index}",
                index,
                0.25,
                0.84,
                0.75,
                1.0,
            )
            for index in range(5)
        ]
        first_batch = [
            ("101", 0.99, _box(5, 10, 50, 35)),
            ("102", 0.98, _box(429, 10, 474, 35)),
            ("103", 0.97, _box(853, 10, 898, 35)),
            ("104", 0.96, _box(1277, 10, 1322, 35)),
            ("separator", 0.99, _box(405, 10, 419, 35)),
        ]
        results = [
            SimpleNamespace(word_results=(tuple(first_batch),)),
            SimpleNamespace(word_results=((("105", 0.95, _box(5, 10, 50, 35)),),)),
        ]

        context, document, _ = self._module_context(pages, results)
        with context:
            ticks = iter(index / 10 for index in range(100))
            with (
                patch.object(
                    local_ocr.metadata,
                    "version",
                    return_value="test-version",
                ),
                patch.object(
                    local_ocr,
                    "_model_fingerprint",
                    return_value="model-hash",
                ),
                patch.object(
                    local_ocr,
                    "perf_counter",
                    side_effect=lambda: next(ticks),
                ),
            ):
                detected, summary = local_ocr.ocr_margin_regions(
                    Path("fixture.pdf"),
                    analyses,
                    regions,
                    password="secret",
                    rotations=[0, 0, 0, 0, 0],
                )

        self.assertTrue(document.closed)
        self.assertIn("footer-0", detected, summary.as_report())
        self.assertEqual(detected["footer-0"], [("101", 0.99)])
        self.assertEqual(detected["footer-4"], [("105", 0.95)])
        self.assertNotIn("separator", [text for values in detected.values() for text, _ in values])
        engine = _FakeRapidOCR.instances[0]
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[0].shape, (64, 1672, 3))
        self.assertEqual(engine.calls[1].shape, (64, 400, 3))
        self.assertEqual(summary.attempted_pages, (1, 2, 3, 4, 5))
        self.assertEqual(summary.batch_count, 2)
        self.assertEqual(summary.failed_regions, ())
        self.assertEqual(summary.regions_with_words, tuple(f"footer-{index}" for index in range(5)))


if __name__ == "__main__":
    unittest.main()
