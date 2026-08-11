# Heuristics and Limitations

## Automatic TOC detection

The linker groups words into visual lines and looks for pages containing a TOC-like heading or a dense set of rows whose final token is a page label aligned near the right edge. It supports Arabic and basic Roman page labels.

It maps printed labels to physical pages by finding a repeated offset in page numbers located near page headers or footers. For example, if printed page 1 is physical PDF page 4, the offset is `3`.

When no reliable offset exists, it tries a unique normalized title match against document page text, then falls back to treating the printed label as a physical page number.

## Cases requiring overrides

Use `--toc-pages` when navigation pages do not say Table of Contents, Contents, Agenda, or Index and do not contain enough consistently aligned rows.

Use `--page-offset` when page numbers are absent, stylized as graphics, restart in multiple sections, or use a numbering scheme the detector cannot infer.

For documents with multiple independent numbering sequences, process with custom code or split the document into sections. A single global offset cannot represent them reliably.

## URL limits

The script links visible URL, domain, and email text. It preserves existing URI annotations.

It cannot infer a hidden destination from a label such as `Privacy Policy` after a compressor has deleted the original annotation. Supply or restore those destinations manually from a known-good PDF.

URLs split across multiple visual lines may require manual repair. Review the generated JSON report and final annotations.

## Scanned PDFs

Scanned/image-only PDFs have no usable word coordinates. Run OCR first, then use this skill. OCR can change text geometry, so render and click verification are mandatory.

Recommended OCR tools:

- OCRmyPDF: https://ocrmypdf.readthedocs.io/
- Tesseract OCR: https://github.com/tesseract-ocr/tesseract

Typical OCR-first flow:

```powershell
ocrmypdf --skip-text input.pdf searchable.pdf
python scripts/run_isolated.py make searchable.pdf
```

If a known-good interactive PDF has the same page count, dimensions, and layout as the compressed or flattened source, prefer `--reference-pdf`. This copies the verified link rectangles and destinations without OCR and is the safest way to restore interactivity lost during optimization.

## Safety

Never overwrite the source. Do not remove existing annotations unless the user explicitly requests replacement. Treat unresolved rows as a verification failure when the user expects every entry to work.
