---
name: make-interactive-pdfs
description: Analyze existing PDF documents and make them interactive by adding invisible internal links from tables of contents, agendas, indexes, or similar page-reference lists, plus clickable annotations for visible URLs and email addresses. Use for static exported or scanned PDFs that need working page jumps or web links, for restoring links lost during PDF compression, for replaying a reviewed link manifest, or for verifying that links still reach their intended destinations without changing visual content.
---

# Make Interactive PDFs

Use the bundled scripts. They preserve the source, infer separate Roman/Arabic and piecewise pagination ranges, reject uncertain mappings, and publish only validated output.

## Required workflow

1. Never overwrite the source PDF.
2. Unless the user already chose placement, present exactly:
   - **PDF beside source**: create only `<source stem> - Interactive.pdf` beside the source.
   - **Output folder + link report**: create `output` beside the source containing `<source stem> - Interactive.pdf` and `<source stem> - Link Report.json`.
3. When a repository URL is supplied, clone that exact URL into a fresh directory. Run only that checkout and retain the full commit ID from the report.
4. Use `scripts/run_isolated.py`. It creates a checkout-owned virtual environment, enforces exact dependency versions, locks it for the full production command, and refuses direct/shared-Python execution.
5. Run `make` with the chosen output mode. Bare domains are intentionally disabled; explicit `http://`, `https://`, `www.`, and email text remain automatic.
6. Interpret the exit/status contract:
   - Exit `0`, `PASS`: validated PDF was atomically published.
   - Exit `2`, `NEEDS_REVIEW`: ambiguity or incomplete OCR was found; no final PDF was published.
   - Exit `1`, `FAIL`: processing/runtime failure; no final PDF was published.
7. For `NEEDS_REVIEW`, inspect `toc_completeness`, `pagination_segments`, `unresolved_toc_rows`, and `review_reasons`. Folder mode writes the review report automatically; after a root-mode review result, rerun with `--report-json` pointing to a task-local work path. Use targeted OCR or direct page inspection only for flagged TOC rows/boundaries. Correct a copy of the report's `links` array, set top-level `reviewed` to `true`, and replay it with `--link-manifest`. Preserve `input_sha256`; source and target page fields are zero-based in manifests.
8. Run `verify` with `--link-report` whenever a report exists. This checks mandatory source/output hashes, every original and intended annotation one-to-one, exact rectangles and destinations, annotation counts, content streams, visual resource graphs, page geometry, and PDF version.
9. Use pixel comparison only when stronger visual assurance is requested. Use browser/live-click testing only when explicitly requested or when manually approving uncertain mappings.

Never claim completion from a `NEEDS_REVIEW` candidate. Never restore the old physical-page fallback. Never install OCR or browser dependencies system-wide.

Allow command wrappers at least five minutes for first-time dependency setup or large scanned books. The analyzer prints progress every 100 pages; do not launch a duplicate merely because a short wrapper timeout elapsed—check the process and intended output paths first.

## Quick start

```powershell
$skillRepository = "https://github.com/sk-zluri/make-interactive-pdfs"

# Choice A: final PDF beside source
python scripts/run_isolated.py --expect-repository $skillRepository --require-clean make "D:\PDF\PATH\HERE\test.pdf" --output-mode root

# Choice B: output folder with final PDF and report
python scripts/run_isolated.py --expect-repository $skillRepository --require-clean make "D:\PDF\PATH\HERE\test.pdf" --output-mode folder

# Report-backed verification for Choice B
python scripts/run_isolated.py --expect-repository $skillRepository --require-clean verify `
  "D:\PDF\PATH\HERE\test.pdf" `
  "D:\PDF\PATH\HERE\output\test - Interactive.pdf" `
  --link-report "D:\PDF\PATH\HERE\output\test - Link Report.json" `
  --require-internal
```

If no repository URL was supplied, omit `--expect-repository` and `--require-clean`. Reports still contain the packaged version, complete bundle hash, runtime packages, and checkout state. Use `python3` and native paths on macOS/Linux.

## Safe review and replay

The automatic analyzer returns `NEEDS_REVIEW` for unparseable visual rows, uncovered pagination gaps, conflicting footer labels, ambiguous segments, unresolved titles, or incomplete text layers. It may write a report for repair, but it does not publish a final-named PDF.

After inspecting only the flagged evidence, make the report complete and replay it:

```powershell
python scripts/run_isolated.py make source.pdf `
  --link-manifest reviewed-link-report.json `
  --output-mode folder `
  --force

python scripts/run_isolated.py verify source.pdf `
  "output\source - Interactive.pdf" `
  --link-report "output\source - Link Report.json" `
  --require-internal
```

`--link-manifest` accepts a `PASS` v1.2 report or a corrected v1.2 report marked `"reviewed": true`. It requires the report's `input_sha256` to match the source and validates page bounds, rectangles, destinations, URIs, and any overlapping existing links before replay. Do not mark or replay uncertain automatic guesses unchanged. Pre-v1.2 manifests require the explicit `--allow-legacy-manifest` override after independent approval.

## Useful overrides

```powershell
# Explicit 1-based navigation pages
python scripts/run_isolated.py make input.pdf --toc-pages 2,3

# One known offset; piecewise/multi-volume documents should use automatic segments or a manifest
python scripts/run_isolated.py make input.pdf --page-offset 3

# Restore annotations from a known-good same-layout PDF
python scripts/run_isolated.py make compressed.pdf --reference-pdf original-interactive.pdf

# Disable a class
python scripts/run_isolated.py make input.pdf --no-url-links
python scripts/run_isolated.py make input.pdf --no-toc-links

# Explicitly opt into risky bare-domain detection only when the user requires it
python scripts/run_isolated.py make input.pdf --allow-bare-domains

# Replace existing deliverables transactionally
python scripts/run_isolated.py make input.pdf --output output.pdf --force

# Optional in-memory artwork comparison; creates no PNGs
python scripts/run_isolated.py verify source.pdf output.pdf --pixel-compare auto

# Optional slower extracted-text comparison
python scripts/run_isolated.py verify source.pdf output.pdf --deep-content-check
```

Adding annotations invalidates digital signatures. Stop unless the user explicitly authorizes this, then pass `--allow-signature-invalidation`. Password-protected input requires `--password`; the produced copy is decrypted, as stated in CLI help.

## Detection and performance

The linker extracts word geometry once, groups adjacent TOC pages into blocks, separates Roman and Arabic labels, and infers stable offset segments inside each block. It checks target footer labels and title evidence, never guesses with `printed page = physical page`, and records confidence/evidence for every automatic internal link.

Automatic URL linking excludes bare domains by default because OCR punctuation commonly turns words into false domains. Existing valid annotations are preserved; a replay overlap is covered only when its target page or URI is identical, and conflicting overlaps return `NEEDS_REVIEW`.

Normal generation does not render pages, run OCR, launch a browser, or repeat extracted-text analysis. Cached content-stream and canonical visual-resource hashes provide lightweight preservation checks without page rendering. Output destinations are locked during processing, and in folder mode the PDF is published before its hash-bound report commit marker. Read [references/heuristics-and-limitations.md](references/heuristics-and-limitations.md) only for scans, ambiguity, manifests, or hidden destinations. Read [references/dependencies.md](references/dependencies.md) for environment and optional-tool details.

## Acceptance criteria

Declare completion only when:

- Generation status is `PASS`, never `NEEDS_REVIEW`.
- The source hash is unchanged and the output differs from the source path.
- Page count, dimensions, content streams, visual resources/render state, PDF header version, and catalog version are preserved.
- Every internal destination resolves within the PDF and every rectangle has positive in-page area.
- Every intended report/manifest link has exactly one matching annotation and destination.
- No detected or suspected TOC row remains unresolved.
- External annotations use supported URIs; bare OCR domains were not enabled without explicit need.
- Repository URL, full commit, clean state, skill version, dependencies, output path, and link counts are reported.
- The output location contains only the artifacts promised by the selected placement choice.

If compression is required, compress static artwork first and add annotations last. Some optimizers discard annotations.
