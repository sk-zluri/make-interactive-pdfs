"""Fully local OCR for PDF pages whose text layer is missing or unusable."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
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
    }


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
