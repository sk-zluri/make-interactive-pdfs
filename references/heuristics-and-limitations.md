# Heuristics and limitations

## Contents and pagination

The analyzer caches word geometry once, detects TOC-like pages, groups adjacent pages into blocks, and infers pagination only from repeated header/footer evidence inside the content region following each block. Roman and Arabic labels are separate. Stable offset changes become separate segments, which supports volumes, numbering restarts, and scan-page omissions.

Rows are accepted only when a stable segment, matching target footer, or strong unique title supports the destination. Uncovered gaps, conflicting footers, multiple plausible targets, and fuzzy-only title matches produce `NEEDS_REVIEW`. There is no physical-page fallback.

`toc_completeness` compares parsed rows with visual right-column candidates and chapter anchors. A nonzero `suspected_unparsed_toc_rows` means the OCR/text layer likely omitted or corrupted rows; zero is evidence, not a guarantee for image-only pages.

Use `--toc-pages` for unusual navigation-page headings. Use `--page-offset` only for a single known mapping. Prefer automatically inferred piecewise segments or a reviewed manifest for multiple sequences.

## Reviewed manifests

A report's `links` array is replayable with `--link-manifest`. Internal `source_page` and `target_page` values are zero-based. Rectangles use PDF coordinates. Preserve the schema-v2 `input_sha256`; replay refuses a different source. The legacy `source_sha256` field is accepted only for explicitly approved pre-v1.2 manifests used with `--allow-legacy-manifest`.

For a review repair:

1. Inspect only flagged TOC pages and pagination boundaries.
2. Add/correct links and remove false candidates in a copy of the report.
3. Keep one link per intended visual row.
4. Set top-level `reviewed` to `true`; unchanged `NEEDS_REVIEW` reports are rejected.
5. Replay the reviewed file with `--link-manifest`.
6. Verify the new output using its newly generated report and `verify --link-report`.

The reviewed manifest is the reproducible record of manual/OCR decisions. Do not rely on one-off scripts.

## URLs

Default URL detection links explicit `http://`, `https://`, `www.`, and email text. Bare domains are disabled because OCR frequently produces false strings such as `Brah.ma`. Enable `--allow-bare-domains` only after inspecting candidates.

Hidden destinations cannot be inferred from labels such as `Privacy Policy` after an optimizer removes the annotation. Use a known-good same-layout PDF or a reviewed manifest. Split visual URLs can require review.

## Scanned PDFs and OCR

The analyzer judges each page independently. Healthy selectable text is used directly even when a full-page scan sits behind it. Local OCR runs only when text is missing, too sparse for the visible page, or clearly corrupted; structural blanks and conservatively detected blank scans are skipped. Rotated scans are normalized for OCR and mapped back to the source page's PDF coordinate system. OCR words pass through the same TOC, pagination, URL, and fail-closed verification logic as native PDF words.

The tool uses exact-pinned RapidOCR and ONNX Runtime packages inside its checkout-owned environment; it never installs OCR system-wide:

- RapidOCR: https://github.com/RapidAI/RapidOCR
- ONNX Runtime: https://onnxruntime.ai/

OCR never rewrites page artwork or creates a replacement text layer. Reports record why pages were selected, which pages were skipped as blank, and whether OCR replaced, merged with, or could not safely improve existing text. A page-level OCR engine failure or unresolved corrupted text forces `NEEDS_REVIEW`; an honestly blank page does not. If ambiguity remains, inspect the flagged pages and create a reviewed manifest. Never publish a partial automatic candidate.

## Unsupported or review-required cases

- Damaged or malformed PDFs that pypdf/pdfplumber cannot parse.
- Image-only navigation pages whose local OCR does not produce safe word coordinates.
- Digital signatures unless invalidation is explicitly authorized.
- Encrypted input without a password; output is a decrypted copy.
- XFA, portfolios, embedded-file workflows, or forms whose behavior depends on incremental updates.
- Reference PDFs with different page count or dimensions.

For same-layout compressed PDFs, `--reference-pdf` remains the safest restoration route. It copies link rectangles and destinations without OCR while preserving the compressed source artwork.
