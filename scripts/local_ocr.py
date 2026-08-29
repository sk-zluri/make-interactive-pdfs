"""Fully local OCR for PDF pages whose text layer is missing or unusable."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from time import perf_counter
from typing import Callable, Sequence


OCR_RENDER_SCALE = 2.0
MIN_WORD_CONFIDENCE = 0.55
LOW_TEXT_WORD_LIMIT = 8
LOW_TEXT_CHARACTER_LIMIT = 40
DOMINANT_RASTER_AREA_RATIO = 0.55
MIN_GARBLED_SAMPLE_CHARACTERS = 20
MIN_NATIVE_ALNUM_RATIO = 0.35
MAX_INVALID_CHARACTER_RATIO = 0.02
MAX_SINGLE_CHARACTER_TOKEN_RATIO = 0.8
SIGNIFICANT_NON_TEXT_OBJECT_LIMIT = 12
NAVIGATION_OCR_RENDER_SCALE = 3.0
NAVIGATION_OCR_RIGHT_COLUMN_RATIO = 0.19
NAVIGATION_OCR_MAX_BATCH_SIZE = 4
NAVIGATION_OCR_SEPARATOR_PIXELS = 24
MARGIN_OCR_RENDER_SCALE = 4.0
MARGIN_OCR_MAX_BATCH_SIZE = 4
MARGIN_OCR_SEPARATOR_PIXELS = 24


@dataclass(frozen=True)
class OCRSummary:
    engine: str
    version: str
    model_sha256: str
    attempted_pages: tuple[int, ...]
    enriched_pages: tuple[int, ...]
    failed_pages: tuple[int, ...]
    attempt_reasons: tuple[tuple[str, tuple[int, ...]], ...] = ()
    skipped_blank_pages: tuple[int, ...] = ()
    native_text_pages: int = 0
    replaced_pages: tuple[int, ...] = ()
    merged_pages: tuple[int, ...] = ()
    retained_native_pages: tuple[int, ...] = ()
    no_text_found_pages: tuple[int, ...] = ()
    quality_unresolved_pages: tuple[int, ...] = ()

    def as_report(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "version": self.version,
            "model_sha256": self.model_sha256,
            "attempted_pages": list(self.attempted_pages),
            "enriched_pages": list(self.enriched_pages),
            "failed_pages": list(self.failed_pages),
            "attempt_reasons": {
                reason: list(pages) for reason, pages in self.attempt_reasons
            },
            "skipped_blank_pages": list(self.skipped_blank_pages),
            "native_text_pages": self.native_text_pages,
            "replaced_pages": list(self.replaced_pages),
            "merged_pages": list(self.merged_pages),
            "retained_native_pages": list(self.retained_native_pages),
            "no_text_found_pages": list(self.no_text_found_pages),
            "quality_unresolved_pages": list(self.quality_unresolved_pages),
        }


@dataclass(frozen=True)
class NavigationOCRSummary:
    """Deterministic audit metadata for targeted page-number-column OCR."""

    engine: str
    version: str
    model_sha256: str
    attempted_pages: tuple[int, ...]
    rendered_pages: tuple[int, ...]
    pages_with_words: tuple[int, ...]
    failed_pages: tuple[int, ...]
    batch_attempts: tuple[tuple[int, ...], ...]
    page_word_counts: tuple[tuple[int, int], ...]
    render_scale: float = NAVIGATION_OCR_RENDER_SCALE
    right_column_ratio: float = NAVIGATION_OCR_RIGHT_COLUMN_RATIO
    max_batch_size: int = NAVIGATION_OCR_MAX_BATCH_SIZE
    render_seconds: float = 0.0
    ocr_seconds: float = 0.0
    total_seconds: float = 0.0

    def as_report(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "version": self.version,
            "model_sha256": self.model_sha256,
            "attempted_pages": list(self.attempted_pages),
            "rendered_pages": list(self.rendered_pages),
            "pages_with_words": list(self.pages_with_words),
            "failed_pages": list(self.failed_pages),
            "batch_attempts": [list(pages) for pages in self.batch_attempts],
            "page_word_counts": {
                str(page): count for page, count in self.page_word_counts
            },
            "render_scale": self.render_scale,
            "right_column_ratio": self.right_column_ratio,
            "max_batch_size": self.max_batch_size,
            "render_seconds": self.render_seconds,
            "ocr_seconds": self.ocr_seconds,
            "total_seconds": self.total_seconds,
        }


@dataclass(frozen=True)
class MarginOCRRegion:
    """One normalized edge region to use as a visual page-label referee."""

    key: str
    page_index: int
    x0_ratio: float
    top_ratio: float
    x1_ratio: float
    bottom_ratio: float


@dataclass(frozen=True)
class MarginOCRSummary:
    """Audit metadata for small, conflict-only page-margin OCR batches."""

    engine: str
    version: str
    model_sha256: str
    attempted_pages: tuple[int, ...]
    attempted_regions: tuple[str, ...]
    regions_with_words: tuple[str, ...]
    failed_regions: tuple[str, ...]
    batch_count: int
    render_scale: float = MARGIN_OCR_RENDER_SCALE
    max_batch_size: int = MARGIN_OCR_MAX_BATCH_SIZE
    render_seconds: float = 0.0
    ocr_seconds: float = 0.0
    total_seconds: float = 0.0

    def as_report(self) -> dict[str, object]:
        return {
            "engine": self.engine,
            "version": self.version,
            "model_sha256": self.model_sha256,
            "attempted_pages": list(self.attempted_pages),
            "attempted_regions": list(self.attempted_regions),
            "regions_with_words": list(self.regions_with_words),
            "failed_regions": list(self.failed_regions),
            "batch_count": self.batch_count,
            "render_scale": self.render_scale,
            "max_batch_size": self.max_batch_size,
            "render_seconds": self.render_seconds,
            "ocr_seconds": self.ocr_seconds,
            "total_seconds": self.total_seconds,
        }


@dataclass(frozen=True)
class OCRDecision:
    should_ocr: bool
    reason: str
    replace_native: bool = False


def _extracted_word_text(words: Sequence[dict], fallback_text: str) -> str:
    text = " ".join(
        str(word.get("text", "")).strip()
        for word in words
        if str(word.get("text", "")).strip()
    )
    return text or str(fallback_text or "")


def _looks_garbled(text: str, words: Sequence[dict]) -> bool:
    """Reject clearly corrupt text without assuming a language or dictionary."""

    visible = [character for character in text if not character.isspace()]
    if not visible:
        return False

    invalid = sum(
        character == "\ufffd"
        or unicodedata.category(character) in {"Cc", "Cs", "Co", "Cn"}
        for character in visible
    )
    if invalid / len(visible) > MAX_INVALID_CHARACTER_RATIO:
        return True

    if len(visible) >= MIN_GARBLED_SAMPLE_CHARACTERS:
        alphanumeric = sum(character.isalnum() for character in visible)
        if alphanumeric / len(visible) < MIN_NATIVE_ALNUM_RATIO:
            return True

    tokens = [
        str(word.get("text", "")).strip()
        for word in words
        if str(word.get("text", "")).strip()
    ]
    if len(tokens) >= 12:
        single_character_tokens = sum(
            len("".join(character for character in token if character.isalnum())) <= 1
            for token in tokens
        )
        if single_character_tokens / len(tokens) >= MAX_SINGLE_CHARACTER_TOKEN_RATIO:
            return True

    if re.search(r"\(cid:\d+\)", text, flags=re.IGNORECASE):
        return True

    if 4 <= len(tokens) <= 30:
        suspicious_tokens = sum(
            bool(re.search(r"[<>{}~|\\]", token))
            or bool(re.search(r"[^\w\s]{2,}", token, flags=re.UNICODE))
            for token in tokens
        )
        if suspicious_tokens >= max(2, round(len(tokens) * 0.2)):
            return True

    return False


def ocr_decision(
    words: Sequence[dict],
    extracted_text: str,
    image_area_ratio: float = 0.0,
    *,
    has_nontext_content: bool | None = None,
) -> OCRDecision:
    """Choose OCR page-by-page, trusting a healthy selectable text layer first."""

    text = _extracted_word_text(words, extracted_text)
    tokens = [
        str(word.get("text", "")).strip()
        for word in words
        if str(word.get("text", "")).strip()
    ]
    readable_characters = sum(character.isalnum() for character in text)
    has_visual_content = (
        image_area_ratio > 0.0
        if has_nontext_content is None
        else bool(has_nontext_content)
    )

    if not tokens and readable_characters == 0:
        if not has_visual_content:
            return OCRDecision(False, "blank_page")
        return OCRDecision(True, "no_text", replace_native=True)

    if _looks_garbled(text, words):
        return OCRDecision(True, "garbled_text", replace_native=True)

    if (
        len(tokens) >= LOW_TEXT_WORD_LIMIT
        and readable_characters >= LOW_TEXT_CHARACTER_LIMIT
    ):
        return OCRDecision(False, "native_text_usable")

    if image_area_ratio >= DOMINANT_RASTER_AREA_RATIO:
        return OCRDecision(True, "sparse_scan")

    if has_visual_content:
        return OCRDecision(True, "sparse_visual_page")

    return OCRDecision(False, "sparse_native_text")


def needs_ocr(
    words: Sequence[dict],
    normalized_text: str,
    image_area_ratio: float = 0.0,
    *,
    has_nontext_content: bool | None = None,
) -> bool:
    """Compatibility wrapper around the deterministic page decision."""

    return ocr_decision(
        words,
        normalized_text,
        image_area_ratio,
        has_nontext_content=has_nontext_content,
    ).should_ocr


def _model_fingerprint(package_root: Path) -> str:
    digest = hashlib.sha256()
    model_paths = sorted((package_root / "models").glob("*.onnx"))
    for path in model_paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _box_to_word(
    text: str,
    confidence: float,
    box: Sequence[Sequence[float]],
    *,
    pdf_width: float,
    pdf_height: float,
    image_width: int,
    image_height: int,
) -> dict | None:
    clean_text = str(text).strip()
    if not clean_text or float(confidence) < MIN_WORD_CONFIDENCE or len(box) < 4:
        return None

    x_values = [float(point[0]) * pdf_width / image_width for point in box]
    y_values = [float(point[1]) * pdf_height / image_height for point in box]
    x0 = max(0.0, min(pdf_width, min(x_values)))
    x1 = max(0.0, min(pdf_width, max(x_values)))
    top = max(0.0, min(pdf_height, min(y_values)))
    bottom = max(0.0, min(pdf_height, max(y_values)))
    if x1 <= x0 or bottom <= top:
        return None
    return {
        "text": clean_text,
        "x0": x0,
        "x1": x1,
        "top": top,
        "bottom": bottom,
        "doctop": top,
        "upright": True,
        "direction": "ltr",
        "ocr_confidence": round(float(confidence), 5),
        "ocr_text": True,
    }


@dataclass
class _NavigationOCRStrip:
    page_index: int
    image: object
    pixel_width: int
    pixel_height: int
    pdf_width: float
    pdf_height: float
    crop_x0: float
    batch_left: int = 0


def _navigation_word_for_strip(
    text: str,
    confidence: float,
    box: Sequence[Sequence[float]],
    strip: _NavigationOCRStrip,
) -> dict | None:
    """Map one batch-image OCR box back into full-page visual coordinates."""

    if len(box) < 4:
        return None
    try:
        x_values = [float(point[0]) for point in box]
        y_values = [float(point[1]) for point in box]
    except (IndexError, TypeError, ValueError):
        return None

    strip_left = float(strip.batch_left)
    strip_right = strip_left + float(strip.pixel_width)
    tolerance = 1.0
    if (
        min(x_values) < strip_left - tolerance
        or max(x_values) > strip_right + tolerance
        or max(y_values) < -tolerance
        or min(y_values) > float(strip.pixel_height) + tolerance
    ):
        return None

    local_box = [
        [float(point[0]) - strip_left, float(point[1])]
        for point in box
    ]
    crop_width = strip.pdf_width - strip.crop_x0
    word = _box_to_word(
        text,
        confidence,
        local_box,
        pdf_width=crop_width,
        pdf_height=strip.pdf_height,
        image_width=strip.pixel_width,
        image_height=strip.pixel_height,
    )
    if word is None:
        return None
    word["x0"] = max(0.0, min(strip.pdf_width, word["x0"] + strip.crop_x0))
    word["x1"] = max(0.0, min(strip.pdf_width, word["x1"] + strip.crop_x0))
    if word["x1"] <= word["x0"]:
        return None
    word["ocr_navigation"] = True
    return word


def ocr_navigation_columns(
    input_path: Path,
    analyses: Sequence[object],
    page_indices: Sequence[int],
    *,
    password: str | None = None,
    rotations: Sequence[int] | None = None,
    right_column_ratio: float = NAVIGATION_OCR_RIGHT_COLUMN_RATIO,
    render_scale: float = NAVIGATION_OCR_RENDER_SCALE,
    batch_size: int = NAVIGATION_OCR_MAX_BATCH_SIZE,
) -> tuple[dict[int, list[dict]], NavigationOCRSummary]:
    """OCR selected right-hand page-number columns in small horizontal batches.

    Page indices and dictionary keys are zero based, matching ``analyses``.
    Summary page numbers are one based for user-facing reports. PDFium applies a
    page's intrinsic rotation before cropping; dimensions and any supplied
    rotation are checked so uncertain coordinate transforms fail closed.
    """

    selected = tuple(sorted({int(page_index) for page_index in page_indices}))
    if not 0.0 < float(right_column_ratio) < 1.0:
        raise ValueError("right_column_ratio must be between 0 and 1")
    if float(render_scale) <= 0.0:
        raise ValueError("render_scale must be positive")
    if not 1 <= int(batch_size) <= NAVIGATION_OCR_MAX_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {NAVIGATION_OCR_MAX_BATCH_SIZE}"
        )

    analysis_by_page = {
        int(getattr(analysis, "page_index")): analysis for analysis in analyses
    }
    missing = [page for page in selected if page < 0 or page not in analysis_by_page]
    if missing:
        raise ValueError(f"Selected page indices are unavailable: {missing}")
    if rotations is not None and selected and max(selected) >= len(rotations):
        raise ValueError("rotations does not cover every selected page")

    try:
        version = metadata.version("rapidocr")
    except metadata.PackageNotFoundError:
        version = "unavailable"

    engine_name = "RapidOCR (ONNX Runtime)"
    if not selected:
        return {}, NavigationOCRSummary(
            engine=engine_name,
            version=version,
            model_sha256="",
            attempted_pages=(),
            rendered_pages=(),
            pages_with_words=(),
            failed_pages=(),
            batch_attempts=(),
            page_word_counts=(),
            render_scale=float(render_scale),
            right_column_ratio=float(right_column_ratio),
            max_batch_size=int(batch_size),
        )

    started_at = perf_counter()
    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("The bundled local PDF renderer is unavailable") from exc
    try:
        import rapidocr
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError("The bundled local OCR engine is unavailable") from exc

    package_root = Path(rapidocr.__file__).resolve().parent
    engine = RapidOCR(
        params={
            "Global.log_level": "error",
            "Global.return_word_box": True,
        }
    )
    document = pdfium.PdfDocument(str(input_path), password=password)
    if selected and max(selected) >= len(document):
        document.close()
        raise ValueError("Selected page index exceeds the PDF page count")

    render_seconds = 0.0
    ocr_seconds = 0.0
    rendered_pages: list[int] = []
    failed_pages: set[int] = set()
    batch_attempts: list[tuple[int, ...]] = []
    detected_by_page: dict[int, list[dict]] = {}

    try:
        for chunk_start in range(0, len(selected), int(batch_size)):
            chunk = selected[chunk_start : chunk_start + int(batch_size)]
            strips: list[_NavigationOCRStrip] = []
            for page_index in chunk:
                render_started_at = perf_counter()
                try:
                    analysis = analysis_by_page[page_index]
                    page = document[page_index]
                    actual_rotation = int(page.get_rotation() or 0) % 360
                    expected_rotation = (
                        int(rotations[page_index] or 0) % 360
                        if rotations is not None
                        else int(getattr(analysis, "rotation", actual_rotation) or 0) % 360
                    )
                    if actual_rotation not in {0, 90, 180, 270}:
                        raise ValueError("Unsupported PDF page rotation")
                    if expected_rotation != actual_rotation:
                        raise ValueError("Page rotation metadata does not match the renderer")

                    rendered_width, rendered_height = (
                        float(value) for value in page.get_size()
                    )
                    pdf_width = float(getattr(analysis, "width"))
                    pdf_height = float(getattr(analysis, "height"))
                    if min(rendered_width, rendered_height, pdf_width, pdf_height) <= 0.0:
                        raise ValueError("Page dimensions must be positive")
                    width_delta = abs(rendered_width - pdf_width) / max(
                        rendered_width, pdf_width
                    )
                    height_delta = abs(rendered_height - pdf_height) / max(
                        rendered_height, pdf_height
                    )
                    if width_delta > 0.01 or height_delta > 0.01:
                        raise ValueError("Page geometry does not match the extracted analysis")

                    rendered_crop_x0 = rendered_width * (1.0 - right_column_ratio)
                    bitmap = page.render(
                        scale=float(render_scale),
                        crop=(rendered_crop_x0, 0.0, 0.0, 0.0),
                    )
                    image = np.asarray(bitmap.to_pil().convert("RGB"))
                    if image.ndim != 3 or image.shape[0] <= 0 or image.shape[1] <= 0:
                        raise ValueError("Navigation crop rendered an empty image")
                    pixel_height, pixel_width = (int(value) for value in image.shape[:2])
                    strips.append(
                        _NavigationOCRStrip(
                            page_index=page_index,
                            image=image,
                            pixel_width=pixel_width,
                            pixel_height=pixel_height,
                            pdf_width=pdf_width,
                            pdf_height=pdf_height,
                            crop_x0=pdf_width * (1.0 - right_column_ratio),
                        )
                    )
                    rendered_pages.append(page_index + 1)
                except Exception:
                    failed_pages.add(page_index + 1)
                finally:
                    render_seconds += perf_counter() - render_started_at

            if not strips:
                continue

            max_height = max(strip.pixel_height for strip in strips)
            total_width = sum(strip.pixel_width for strip in strips)
            total_width += NAVIGATION_OCR_SEPARATOR_PIXELS * (len(strips) - 1)
            batch_image = np.full((max_height, total_width, 3), 255, dtype=np.uint8)
            next_left = 0
            for strip in strips:
                strip.batch_left = next_left
                batch_image[
                    : strip.pixel_height,
                    next_left : next_left + strip.pixel_width,
                ] = strip.image
                next_left += strip.pixel_width + NAVIGATION_OCR_SEPARATOR_PIXELS

            batch_pages = tuple(strip.page_index + 1 for strip in strips)
            batch_attempts.append(batch_pages)
            ocr_started_at = perf_counter()
            try:
                result = engine(batch_image, return_word_box=True)
                for strip in strips:
                    detected_by_page[strip.page_index] = []
                for line in (getattr(result, "word_results", None) or ()):
                    for text, confidence, box in line:
                        if len(box) < 4:
                            continue
                        try:
                            center_x = sum(float(point[0]) for point in box) / len(box)
                        except (IndexError, TypeError, ValueError, ZeroDivisionError):
                            continue
                        strip = next(
                            (
                                candidate
                                for candidate in strips
                                if candidate.batch_left
                                <= center_x
                                < candidate.batch_left + candidate.pixel_width
                            ),
                            None,
                        )
                        if strip is None:
                            continue
                        word = _navigation_word_for_strip(
                            text, confidence, box, strip
                        )
                        if word is not None:
                            detected_by_page[strip.page_index].append(word)
                for strip in strips:
                    detected_by_page[strip.page_index].sort(
                        key=lambda word: (
                            round(float(word["top"]), 5),
                            round(float(word["x0"]), 5),
                            str(word["text"]),
                        )
                    )
            except Exception:
                for strip in strips:
                    failed_pages.add(strip.page_index + 1)
                    detected_by_page.pop(strip.page_index, None)
            finally:
                ocr_seconds += perf_counter() - ocr_started_at
    finally:
        document.close()

    page_word_counts = tuple(
        (page_index + 1, len(detected_by_page[page_index]))
        for page_index in sorted(detected_by_page)
    )
    model_sha256 = _model_fingerprint(package_root)
    total_seconds = perf_counter() - started_at
    summary = NavigationOCRSummary(
        engine=engine_name,
        version=version,
        model_sha256=model_sha256,
        attempted_pages=tuple(page + 1 for page in selected),
        rendered_pages=tuple(rendered_pages),
        pages_with_words=tuple(
            page for page, count in page_word_counts if count > 0
        ),
        failed_pages=tuple(sorted(failed_pages)),
        batch_attempts=tuple(batch_attempts),
        page_word_counts=page_word_counts,
        render_scale=float(render_scale),
        right_column_ratio=float(right_column_ratio),
        max_batch_size=int(batch_size),
        render_seconds=round(render_seconds, 4),
        ocr_seconds=round(ocr_seconds, 4),
        total_seconds=round(total_seconds, 4),
    )
    return detected_by_page, summary


@dataclass
class _MarginOCRStrip:
    region: MarginOCRRegion
    image: object
    batch_left: int = 0


def _iter_word_results(result: object):
    """Accept RapidOCR's single-image and batched result shapes."""

    for item in (getattr(result, "word_results", None) or ()):
        if (
            isinstance(item, (tuple, list))
            and len(item) == 3
            and isinstance(item[0], str)
        ):
            yield item
            continue
        for candidate in item or ():
            if (
                isinstance(candidate, (tuple, list))
                and len(candidate) == 3
                and isinstance(candidate[0], str)
            ):
                yield candidate


def ocr_margin_regions(
    input_path: Path,
    analyses: Sequence[object],
    regions: Sequence[MarginOCRRegion],
    *,
    password: str | None = None,
    rotations: Sequence[int] | None = None,
    render_scale: float = MARGIN_OCR_RENDER_SCALE,
    batch_size: int = MARGIN_OCR_MAX_BATCH_SIZE,
) -> tuple[dict[str, list[tuple[str, float]]], MarginOCRSummary]:
    """Read only explicitly requested page-margin crops.

    This is intentionally a referee, not a second document parser.  Callers
    supply small edge regions only after a reliable native pagination mapping
    conflicts with the PDF's text layer.  Results remain raw OCR tokens so the
    caller can reject anything ambiguous.
    """

    selected = tuple(regions)
    if not 1 <= int(batch_size) <= MARGIN_OCR_MAX_BATCH_SIZE:
        raise ValueError(
            f"batch_size must be between 1 and {MARGIN_OCR_MAX_BATCH_SIZE}"
        )
    if float(render_scale) <= 0.0:
        raise ValueError("render_scale must be positive")
    keys = [region.key for region in selected]
    if len(keys) != len(set(keys)):
        raise ValueError("margin OCR region keys must be unique")
    for region in selected:
        if (
            region.page_index < 0
            or not 0.0 <= region.x0_ratio < region.x1_ratio <= 1.0
            or not 0.0 <= region.top_ratio < region.bottom_ratio <= 1.0
        ):
            raise ValueError(f"invalid margin OCR region: {region.key}")

    try:
        version = metadata.version("rapidocr")
    except metadata.PackageNotFoundError:
        version = "unavailable"
    engine_name = "RapidOCR (ONNX Runtime)"
    if not selected:
        return {}, MarginOCRSummary(
            engine=engine_name,
            version=version,
            model_sha256="",
            attempted_pages=(),
            attempted_regions=(),
            regions_with_words=(),
            failed_regions=(),
            batch_count=0,
            render_scale=float(render_scale),
            max_batch_size=int(batch_size),
        )

    analysis_by_page = {
        int(getattr(analysis, "page_index")): analysis for analysis in analyses
    }
    missing = sorted(
        {
            region.page_index
            for region in selected
            if region.page_index not in analysis_by_page
        }
    )
    if missing:
        raise ValueError(f"Selected page indices are unavailable: {missing}")
    if rotations is not None and max(region.page_index for region in selected) >= len(
        rotations
    ):
        raise ValueError("rotations does not cover every selected page")

    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("The bundled local PDF renderer is unavailable") from exc
    try:
        import rapidocr
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError("The bundled local OCR engine is unavailable") from exc

    started_at = perf_counter()
    package_root = Path(rapidocr.__file__).resolve().parent
    engine = RapidOCR(
        params={
            "Global.log_level": "error",
            "Global.return_word_box": True,
        }
    )
    regions_by_page: dict[int, list[MarginOCRRegion]] = {}
    for region in selected:
        regions_by_page.setdefault(region.page_index, []).append(region)
    strips: list[_MarginOCRStrip] = []
    failed_regions: set[str] = set()
    render_seconds = 0.0
    ocr_seconds = 0.0
    batch_count = 0
    document = pdfium.PdfDocument(str(input_path), password=password)
    try:
        if max(regions_by_page) >= len(document):
            raise ValueError("Selected page index exceeds PDF page count")
        for page_index in sorted(regions_by_page):
            page_regions = regions_by_page[page_index]
            render_started_at = perf_counter()
            try:
                page = document[page_index]
                actual_rotation = int(page.get_rotation() or 0) % 360
                expected_rotation = (
                    int(rotations[page_index] or 0) % 360
                    if rotations is not None
                    else int(
                        getattr(
                            analysis_by_page[page_index],
                            "rotation",
                            actual_rotation,
                        )
                        or 0
                    )
                    % 360
                )
                if actual_rotation not in {0, 90, 180, 270}:
                    raise ValueError("Unsupported PDF page rotation")
                if expected_rotation != actual_rotation:
                    raise ValueError(
                        "Page rotation metadata does not match the renderer"
                    )
                bitmap = page.render(
                    scale=float(render_scale),
                    crop=(0.0, 0.0, 0.0, 0.0),
                )
                image = np.asarray(bitmap.to_pil().convert("RGB"))
                if image.ndim != 3 or image.shape[0] <= 0 or image.shape[1] <= 0:
                    raise ValueError("Margin crop rendered an empty image")
                pixel_height, pixel_width = (
                    int(image.shape[0]),
                    int(image.shape[1]),
                )
                for region in page_regions:
                    x0 = int(round(region.x0_ratio * pixel_width))
                    x1 = int(round(region.x1_ratio * pixel_width))
                    top = int(round(region.top_ratio * pixel_height))
                    bottom = int(round(region.bottom_ratio * pixel_height))
                    x0 = max(0, min(pixel_width - 1, x0))
                    x1 = max(1, min(pixel_width, x1))
                    top = max(0, min(pixel_height - 1, top))
                    bottom = max(1, min(pixel_height, bottom))
                    if x1 <= x0 or bottom <= top:
                        failed_regions.add(region.key)
                        continue
                strips.append(
                    _MarginOCRStrip(
                        region=region,
                        # Keep only the small crop. A NumPy view would retain
                        # the full rendered page for the rest of OCR batching.
                        image=image[top:bottom, x0:x1].copy(),
                    )
                )
            except Exception:
                failed_regions.update(region.key for region in page_regions)
            finally:
                render_seconds += perf_counter() - render_started_at

        detected: dict[str, list[tuple[str, float]]] = {}
        for chunk_start in range(0, len(strips), int(batch_size)):
            chunk = strips[chunk_start : chunk_start + int(batch_size)]
            if not chunk:
                continue
            max_height = max(int(strip.image.shape[0]) for strip in chunk)
            total_width = sum(int(strip.image.shape[1]) for strip in chunk) + (
                MARGIN_OCR_SEPARATOR_PIXELS * (len(chunk) - 1)
            )
            canvas = np.full((max_height, total_width, 3), 255, dtype=np.uint8)
            cursor = 0
            for strip in chunk:
                strip.batch_left = cursor
                strip_height, strip_width = strip.image.shape[:2]
                canvas[:strip_height, cursor : cursor + strip_width] = strip.image
                cursor += strip_width + MARGIN_OCR_SEPARATOR_PIXELS
            batch_count += 1
            ocr_started_at = perf_counter()
            try:
                result = engine(canvas, return_word_box=True)
                for text, confidence, box in _iter_word_results(result):
                    if len(box) < 4:
                        continue
                    try:
                        center_x = sum(float(point[0]) for point in box) / len(box)
                        score = float(confidence)
                    except (
                        IndexError,
                        TypeError,
                        ValueError,
                        ZeroDivisionError,
                    ):
                        continue
                    strip = next(
                        (
                            candidate
                            for candidate in chunk
                            if candidate.batch_left
                            <= center_x
                            < candidate.batch_left
                            + int(candidate.image.shape[1])
                        ),
                        None,
                    )
                    if strip is None or score < MIN_WORD_CONFIDENCE:
                        continue
                    raw = str(text).strip()
                    if raw:
                        detected.setdefault(strip.region.key, []).append(
                            (raw, round(score, 5))
                        )
            except Exception:
                failed_regions.update(strip.region.key for strip in chunk)
                for strip in chunk:
                    detected.pop(strip.region.key, None)
            finally:
                ocr_seconds += perf_counter() - ocr_started_at
    finally:
        document.close()

    model_sha256 = _model_fingerprint(package_root)
    summary = MarginOCRSummary(
        engine=engine_name,
        version=version,
        model_sha256=model_sha256,
        attempted_pages=tuple(page + 1 for page in sorted(regions_by_page)),
        attempted_regions=tuple(region.key for region in selected),
        regions_with_words=tuple(sorted(detected)),
        failed_regions=tuple(sorted(failed_regions)),
        batch_count=batch_count,
        render_scale=float(render_scale),
        max_batch_size=int(batch_size),
        render_seconds=round(render_seconds, 4),
        ocr_seconds=round(ocr_seconds, 4),
        total_seconds=round(perf_counter() - started_at, 4),
    )
    return detected, summary


def _word_result_score(result: object) -> tuple[int, float]:
    words = [
        word
        for line in (getattr(result, "word_results", None) or ())
        for word in line
    ]
    return len(words), sum(float(word[1]) for word in words)


def _overlap_ratio(first: dict, second: dict) -> float:
    left = max(float(first["x0"]), float(second["x0"]))
    right = min(float(first["x1"]), float(second["x1"]))
    top = max(float(first["top"]), float(second["top"]))
    bottom = min(float(first["bottom"]), float(second["bottom"]))
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, float(first["x1"]) - float(first["x0"])) * max(
        0.0, float(first["bottom"]) - float(first["top"])
    )
    second_area = max(0.0, float(second["x1"]) - float(second["x0"])) * max(
        0.0, float(second["bottom"]) - float(second["top"])
    )
    smaller = min(first_area, second_area)
    return intersection / smaller if smaller > 0 else 0.0


def _merge_native_words(native_words: Sequence[dict], ocr_words: Sequence[dict]) -> list[dict]:
    merged = [dict(word) for word in native_words]
    for candidate in ocr_words:
        if any(_overlap_ratio(candidate, existing) >= 0.65 for existing in merged):
            continue
        merged.append(candidate)
    return merged


def _ocr_is_safe_replacement(
    native_words: Sequence[dict],
    ocr_words: Sequence[dict],
    reason: str,
) -> bool:
    if not ocr_words:
        return False
    if reason == "no_text":
        return True

    native_text = _extracted_word_text(native_words, "")
    ocr_text = _extracted_word_text(ocr_words, "")
    if _looks_garbled(ocr_text, ocr_words):
        return False
    native_characters = sum(character.isalnum() for character in native_text)
    ocr_characters = sum(character.isalnum() for character in ocr_text)
    return ocr_characters >= max(12, round(native_characters * 0.35))


def _looks_visually_blank(grayscale_image: object) -> bool:
    """Conservatively recognize an empty white scan before invoking OCR."""

    import numpy as np

    pixels = np.asarray(grayscale_image, dtype=np.float32)
    if pixels.ndim == 3:
        pixels = pixels[..., :3].mean(axis=2)
    if pixels.size == 0:
        return True
    low, high = np.percentile(pixels, (1, 99))
    dark_fraction = float(np.mean(pixels < 235.0))
    vertical_edges = (
        float(np.mean(np.abs(np.diff(pixels, axis=0)) > 12.0))
        if pixels.shape[0] > 1
        else 0.0
    )
    horizontal_edges = (
        float(np.mean(np.abs(np.diff(pixels, axis=1)) > 12.0))
        if pixels.shape[1] > 1
        else 0.0
    )
    return (
        float(pixels.mean()) >= 245.0
        and float(pixels.std()) <= 5.0
        and float(high - low) <= 10.0
        and dark_fraction <= 0.0005
        and vertical_edges + horizontal_edges <= 0.001
    )


def choose_page_words(
    native_words: Sequence[dict],
    ocr_words: Sequence[dict],
    decision: OCRDecision,
    *,
    rotation_normalized: bool = False,
) -> tuple[list[dict], str]:
    """Choose a deterministic page-text outcome after OCR."""

    native = [dict(word) for word in native_words]
    detected = [dict(word) for word in ocr_words]
    if decision.replace_native:
        if _ocr_is_safe_replacement(native, detected, decision.reason):
            return detected, "replaced"
        if decision.reason == "garbled_text":
            return native, "quality_unresolved"
        return native, "no_text_found"

    if rotation_normalized:
        if detected:
            return detected, "replaced"
        return native, "retained_native"

    merged = _merge_native_words(native, detected)
    if len(merged) > len(native):
        return merged, "merged"
    return native, "retained_native"


def ocr_low_text_pages(
    input_path: Path,
    analyses: Sequence[object],
    *,
    password: str | None = None,
    rotations: Sequence[int] | None = None,
    on_selection: Callable[[int, int], None] | None = None,
    on_page_started: Callable[[int, int, int], None] | None = None,
    on_page_finished: Callable[[int, int, int, int], None] | None = None,
) -> tuple[dict[int, list[dict]], OCRSummary]:
    """OCR likely scanned pages and return pdfplumber-compatible visual words."""

    candidates: list[int] = []
    decisions: dict[int, OCRDecision] = {}
    reason_pages: dict[str, list[int]] = {}
    skipped_blank_pages: list[int] = []
    native_text_pages = 0
    for analysis in analyses:
        page_index = int(getattr(analysis, "page_index"))
        native_words = list(getattr(analysis, "words"))
        nontext_object_count = int(getattr(analysis, "nontext_object_count", 0))
        content_stream_bytes = int(getattr(analysis, "content_stream_bytes", 0))
        image_area_ratio = float(getattr(analysis, "image_area_ratio", 0.0))
        decision = ocr_decision(
            native_words,
            str(
                getattr(
                    analysis,
                    "raw_text",
                    getattr(analysis, "normalized_text", ""),
                )
            ),
            image_area_ratio,
            has_nontext_content=bool(
                image_area_ratio >= DOMINANT_RASTER_AREA_RATIO
                or nontext_object_count >= SIGNIFICANT_NON_TEXT_OBJECT_LIMIT
                or (not native_words and content_stream_bytes)
            ),
        )
        decisions[page_index] = decision
        if decision.should_ocr:
            candidates.append(page_index)
            reason_pages.setdefault(decision.reason, []).append(page_index + 1)
        elif decision.reason == "blank_page":
            skipped_blank_pages.append(page_index + 1)
        else:
            native_text_pages += 1

    try:
        version = metadata.version("rapidocr")
    except metadata.PackageNotFoundError:
        version = "unavailable"

    if not candidates:
        return {}, OCRSummary(
            engine="RapidOCR (ONNX Runtime)",
            version=version,
            model_sha256="",
            attempted_pages=(),
            enriched_pages=(),
            failed_pages=(),
            attempt_reasons=(),
            skipped_blank_pages=tuple(skipped_blank_pages),
            native_text_pages=native_text_pages,
        )

    try:
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise RuntimeError("The bundled local PDF renderer is unavailable") from exc

    document = pdfium.PdfDocument(str(input_path), password=password)
    visually_blank: set[int] = set()
    try:
        for page_index in candidates:
            decision = decisions[page_index]
            if decision.reason == "garbled_text":
                continue
            preview = document[page_index].render(scale=0.35, grayscale=True)
            preview_image = np.asarray(preview.to_pil())
            if _looks_visually_blank(preview_image):
                visually_blank.add(page_index)
    except Exception:
        # This is an optimization only. Uncertain pages still go to OCR.
        visually_blank.clear()

    if visually_blank:
        candidates = [page for page in candidates if page not in visually_blank]
        skipped_blank_pages.extend(page + 1 for page in sorted(visually_blank))
        for reason, pages in list(reason_pages.items()):
            remaining = [page for page in pages if page - 1 not in visually_blank]
            if remaining:
                reason_pages[reason] = remaining
            else:
                reason_pages.pop(reason)

    if candidates and on_selection is not None:
        on_selection(len(candidates), len(analyses))

    if not candidates:
        document.close()
        return {}, OCRSummary(
            engine="RapidOCR (ONNX Runtime)",
            version=version,
            model_sha256="",
            attempted_pages=(),
            enriched_pages=(),
            failed_pages=(),
            attempt_reasons=(),
            skipped_blank_pages=tuple(sorted(set(skipped_blank_pages))),
            native_text_pages=native_text_pages,
        )

    try:
        import rapidocr
        from rapidocr import RapidOCR
    except ImportError as exc:
        document.close()
        raise RuntimeError("The bundled local OCR engine is unavailable") from exc

    package_root = Path(rapidocr.__file__).resolve().parent
    engine = RapidOCR(
        params={
            "Global.log_level": "error",
            "Global.return_word_box": True,
        }
    )
    replacements: dict[int, list[dict]] = {}
    enriched: list[int] = []
    failed: list[int] = []
    replaced: list[int] = []
    merged_pages: list[int] = []
    retained_native: list[int] = []
    no_text_found: list[int] = []
    quality_unresolved: list[int] = []
    total = len(candidates)

    try:
        for position, page_index in enumerate(candidates, start=1):
            if on_page_started is not None:
                on_page_started(position - 1, total, page_index)
            analysis = analyses[page_index]
            try:
                page = document[page_index]
                bitmap = page.render(scale=OCR_RENDER_SCALE)
                image = np.asarray(bitmap.to_pil())
                image_height, image_width = image.shape[:2]
                result = engine(image, return_word_box=True)
                selected_result = result
                selected_turns = 0
                selected_image_height, selected_image_width = image_height, image_width
                page_rotation = int(getattr(analysis, "rotation", 0) or 0) % 360
                retry_turns = {90: 1, 180: 2, 270: 3}.get(page_rotation, 0)
                if retry_turns:
                    rotated_image = np.rot90(image, k=retry_turns)
                    rotated_result = engine(rotated_image, return_word_box=True)
                    if _word_result_score(rotated_result) > _word_result_score(result):
                        selected_result = rotated_result
                        selected_turns = retry_turns
                        selected_image_height, selected_image_width = rotated_image.shape[:2]
                pdf_width = float(getattr(analysis, "width"))
                pdf_height = float(getattr(analysis, "height"))
                if selected_turns and page_rotation in (90, 270):
                    pdf_width, pdf_height = pdf_height, pdf_width
                detected: list[dict] = []
                for line in selected_result.word_results or ():
                    for text, confidence, box in line:
                        word = _box_to_word(
                            text,
                            confidence,
                            box,
                            pdf_width=pdf_width,
                            pdf_height=pdf_height,
                            image_width=int(selected_image_width),
                            image_height=int(selected_image_height),
                        )
                        if word is not None:
                            if selected_turns:
                                word["_ocr_rotation_normalized"] = True
                            detected.append(word)
                native_words = list(getattr(analysis, "words"))
                decision = decisions[page_index]
                selected_words, outcome = choose_page_words(
                    native_words,
                    detected,
                    decision,
                    rotation_normalized=bool(selected_turns),
                )
                if outcome in {"replaced", "merged"}:
                    replacements[page_index] = selected_words
                    enriched.append(page_index + 1)
                if outcome == "replaced":
                    replaced.append(page_index + 1)
                elif outcome == "merged":
                    merged_pages.append(page_index + 1)
                elif outcome == "retained_native":
                    retained_native.append(page_index + 1)
                elif outcome == "no_text_found":
                    no_text_found.append(page_index + 1)
                elif outcome == "quality_unresolved":
                    quality_unresolved.append(page_index + 1)
                    failed.append(page_index + 1)
                if on_page_finished is not None:
                    on_page_finished(position, total, page_index, len(detected))
            except Exception:
                failed.append(page_index + 1)
                if on_page_finished is not None:
                    on_page_finished(position, total, page_index, 0)
    finally:
        document.close()

    summary = OCRSummary(
        engine="RapidOCR (ONNX Runtime)",
        version=version,
        model_sha256=_model_fingerprint(package_root),
        attempted_pages=tuple(page + 1 for page in candidates),
        enriched_pages=tuple(enriched),
        failed_pages=tuple(sorted(set(failed))),
        attempt_reasons=tuple(
            (reason, tuple(pages)) for reason, pages in sorted(reason_pages.items())
        ),
        skipped_blank_pages=tuple(sorted(set(skipped_blank_pages))),
        native_text_pages=native_text_pages,
        replaced_pages=tuple(replaced),
        merged_pages=tuple(merged_pages),
        retained_native_pages=tuple(retained_native),
        no_text_found_pages=tuple(no_text_found),
        quality_unresolved_pages=tuple(quality_unresolved),
    )
    return replacements, summary
