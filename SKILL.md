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
3. When the user supplies a skill repository URL, make a fresh Git clone from that exact URL rather than substituting an installed copy. Run `scripts/skill_provenance.py` with `--expect-repository`, `--require-git`, and `--require-clean`; retain its full commit ID in the result.
4. Use `scripts/run_isolated.py` for generation and verification. It creates and reuses a dedicated `.venv` inside the skill checkout. Never install packages into a system, shared, or agent Python environment. The low-level linker and verifier intentionally refuse direct execution.
5. Run the `make` command using the selected output mode.
6. Review the printed detection report. Resolve any unmapped or ambiguous TOC rows with the CLI overrides.
7. Run the `verify` command against the source and result. This default structural check requires no browser, image rendering, or repeated text extraction; the linker already checks text parity while creating the PDF.
8. Only when the user requests stronger visual assurance, add `--pixel-compare`; the isolated runner installs the optional dependency, comparison stays in memory, and no PNGs are created.
9. Only when the user explicitly requests live-viewer proof, use browser-harness or manually click representative links in Chrome or Acrobat.

## Quick Start

```powershell
# When a GitHub URL was supplied, verify the exact clean checkout first
$skillRepository = "https://github.com/sk-zluri/make-interactive-pdfs"
$skillProvenance = python scripts/skill_provenance.py --expect-repository $skillRepository --require-git --require-clean | ConvertFrom-Json
$skillCommit = $skillProvenance.git_commit

# Choice A: PDF beside source; no JSON report
python scripts/run_isolated.py --expect-repository $skillRepository --expect-commit $skillCommit make "D:\PDF\PATH\HERE\test.pdf" --output-mode root

# Choice B: output folder containing the PDF and JSON report
python scripts/run_isolated.py --expect-repository $skillRepository --expect-commit $skillCommit make "D:\PDF\PATH\HERE\test.pdf" --output-mode folder

# Default lightweight structural verification
python scripts/run_isolated.py --expect-repository $skillRepository --expect-commit $skillCommit verify "D:\PDF\PATH\HERE\test.pdf" "D:\PDF\PATH\HERE\test - Interactive.pdf" --require-internal

# Optional in-memory pixel comparison
python scripts/run_isolated.py --expect-repository $skillRepository --expect-commit $skillCommit verify "D:\PDF\PATH\HERE\test.pdf" "D:\PDF\PATH\HERE\test - Interactive.pdf" --require-internal --pixel-compare auto

# Optional slower repeat of page-by-page extracted-text parity
python scripts/run_isolated.py --expect-repository $skillRepository --expect-commit $skillCommit verify "D:\PDF\PATH\HERE\test.pdf" "D:\PDF\PATH\HERE\test - Interactive.pdf" --require-internal --deep-content-check
```

If the user did not supply a repository URL, omit both repository/commit variables and both `--expect-*` options; reports still include the packaged skill version and bundle hashes.

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
- Preserves both the PDF header version and any catalog version override.
- Verifies PDF version, page count, dimensions, text extraction, and final annotation counts before success.
- Prints the isolated-runtime profile, skill version, bundle hash, repository URL, full Git commit, and checkout state in every report.

Read [references/heuristics-and-limitations.md](references/heuristics-and-limitations.md) when auto-detection reports ambiguity, scans contain no selectable text, or link labels hide their actual URLs. Read [references/dependencies.md](references/dependencies.md) for core and optional verification dependencies.

## Useful Overrides

```powershell
# Explicit 1-based TOC pages
python scripts/run_isolated.py make input.pdf --toc-pages 2,3

# Explicit physical-page minus printed-page offset
python scripts/run_isolated.py make input.pdf --page-offset 3

# Restore links after compression from a known-good same-layout PDF
python scripts/run_isolated.py make compressed.pdf --reference-pdf original-interactive.pdf

# Disable one link class
python scripts/run_isolated.py make input.pdf --no-url-links
python scripts/run_isolated.py make input.pdf --no-toc-links

# Replace an existing output deliberately
python scripts/run_isolated.py make input.pdf --output output.pdf --force
```

Use `--toc-pages` and `--page-offset` together when the document has unusual numbering. Physical PDF pages and CLI page numbers are 1-based; internal script indices are 0-based.

Treat `--output` and `--report-json` as advanced overrides. Do not create `output/pdf`, a verification directory beside the final PDF, or a link-report JSON for the root output choice.

## Acceptance Criteria

Do not declare completion unless:

- The output has the same page count and page dimensions as the source.
- The output preserves the source PDF header version and catalog version override.
- Extracted text matches page by page.
- Every internal destination resolves to a valid physical PDF page.
- Every link rectangle has positive area and remains within its source page.
- Every external link has a valid supported URI.
- All intended TOC rows have destinations or are explicitly reported as unresolved.
- Visible URLs are linked or explicitly reported as unsupported.
- The final output path, link counts, unresolved rows, and verification results are reported to the user.
- When a repository URL was supplied, provenance reports that exact origin, a full commit ID, and a clean checkout.
- The user-facing deliverable contains only the artifacts promised by the selected output choice.

If optional pixel comparison is requested, require an exact in-memory artwork match. If optional live-viewer verification is requested, require the tested clicks to reach their expected pages or URLs. Do not install browser-harness or create verification PNGs during the default workflow.

If compression is also required, compress the static artwork first and add link annotations last. Some PDF optimizers discard annotations.

For an image-only PDF, first use OCR as described in [references/heuristics-and-limitations.md](references/heuristics-and-limitations.md), or use `--reference-pdf` when a same-layout interactive version exists.
