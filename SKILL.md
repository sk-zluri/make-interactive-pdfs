---
name: make-interactive-pdfs
description: Analyze existing PDF documents and make them interactive by adding invisible internal links from tables of contents, agendas, indexes, or similar page-reference lists, plus clickable annotations for visible URLs and email addresses. Use for static exported PDFs that need working page jumps or web links, for restoring links lost during PDF compression, or for verifying that an interactive PDF still has correct destinations without changing its visual content.
---

# Make Interactive PDFs

Turn a static PDF into a linked copy while preserving its pages, text, dimensions, and visual appearance. Use the bundled scripts instead of rewriting PDF-annotation logic.

## Workflow

1. Preserve the source PDF. Never overwrite it.
2. Unless the user already specified placement, present exactly these two output choices before processing:
   - **PDF beside source**: create only `<source stem> - Interactive.pdf` in the source directory.
   - **Output folder + link report**: create an `output` folder beside the source containing `<source stem> - Interactive.pdf` and `<source stem> - Link Report.json`.
3. Install Python dependencies from `requirements.txt`.
4. Run `scripts/make_interactive_pdf.py` using the selected output mode.
5. Review the printed detection report. Resolve any unmapped or ambiguous TOC rows with the CLI overrides.
6. Run `scripts/verify_interactive_pdf.py` against the source and result.
7. Render and visually inspect the cover, every detected navigation page, and representative destinations. Keep verification PNGs in the verifier's temporary directory, never in either user deliverable location, and remove them after inspection.
8. Open the exact output PDF in Chrome or Acrobat and click at least two internal links and one external link when present.

## Quick Start

```powershell
python -m pip install -r requirements.txt

# Choice A: PDF beside source; no JSON report
python scripts/make_interactive_pdf.py "D:\PDF\PATH\HERE\test.pdf" --output-mode root

# Choice B: output folder containing the PDF and JSON report
python scripts/make_interactive_pdf.py "D:\PDF\PATH\HERE\test.pdf" --output-mode folder

# Verification renders go to a system temporary directory by default
python scripts/verify_interactive_pdf.py "D:\PDF\PATH\HERE\test.pdf" "D:\PDF\PATH\HERE\test - Interactive.pdf" --render-pages auto
```

On macOS or Linux, use the same commands with native paths and `python3` if required.

## Detection Behavior

The linker automatically:

- Detects TOC-like pages from headings and rows ending in page labels.
- Infers the offset between printed page numbers and physical PDF pages from header/footer numbering.
- Falls back to matching TOC titles against page text.
- Adds invisible full-row internal link annotations.
- Detects visible `http://`, `https://`, `www.`, bare-domain, and email text and adds URI annotations.
- Recovers URI annotations whose action was removed but whose destination remains in annotation metadata.
- Copies all links from a known-good same-layout PDF when `--reference-pdf` is supplied.
- Preserves existing annotations and skips overlapping links to avoid duplicates.
- Verifies page count, dimensions, text extraction, and final annotation counts before success.

Read [references/heuristics-and-limitations.md](references/heuristics-and-limitations.md) when auto-detection reports ambiguity, scans contain no selectable text, or link labels hide their actual URLs. Read [references/dependencies.md](references/dependencies.md) for dependency links and browser verification setup.

## Useful Overrides

```powershell
# Explicit 1-based TOC pages
python scripts/make_interactive_pdf.py input.pdf --toc-pages 2,3

# Explicit physical-page minus printed-page offset
python scripts/make_interactive_pdf.py input.pdf --page-offset 3

# Restore links after compression from a known-good same-layout PDF
python scripts/make_interactive_pdf.py compressed.pdf --reference-pdf original-interactive.pdf

# Disable one link class
python scripts/make_interactive_pdf.py input.pdf --no-url-links
python scripts/make_interactive_pdf.py input.pdf --no-toc-links

# Replace an existing output deliberately
python scripts/make_interactive_pdf.py input.pdf --output output.pdf --force
```

Use `--toc-pages` and `--page-offset` together when the document has unusual numbering. Physical PDF pages and CLI page numbers are 1-based; internal script indices are 0-based.

Treat `--output` and `--report-json` as advanced overrides. Do not create `output/pdf`, a verification directory beside the final PDF, or a link-report JSON for the root output choice.

## Acceptance Criteria

Do not declare completion unless:

- The output has the same page count and page dimensions as the source.
- Extracted text matches page by page.
- All intended TOC rows have destinations or are explicitly reported as unresolved.
- Visible URLs are linked or explicitly reported as unsupported.
- Render comparison shows no artwork changes.
- Click testing reaches the expected physical PDF pages.
- The final output path, link counts, unresolved rows, and verification results are reported to the user.
- The user-facing deliverable contains only the artifacts promised by the selected output choice.

If compression is also required, compress the static artwork first and add link annotations last. Some PDF optimizers discard annotations.

For an image-only PDF, first use OCR as described in [references/heuristics-and-limitations.md](references/heuristics-and-limitations.md), or use `--reference-pdf` when a same-layout interactive version exists.
