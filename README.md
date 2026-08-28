<p align="center">
  <img src="interactive_pdf_app/static/app-icon.png" alt="Make Interactive PDFs" width="88">
</p>

# Make Interactive PDFs

Turn static and scanned PDFs into clickable PDFs—privately, on your own computer.

Make Interactive PDFs can add:

- Page jumps from tables of contents, agendas, indexes, and similar page-reference lists
- Clickable web addresses and email addresses
- Local OCR for scanned pages that genuinely need it
- A verified, separate output PDF without altering the original

## Download for Windows

[Download the latest release](https://github.com/sk-zluri/make-interactive-pdfs/releases/latest)

1. Download `Make.Interactive.PDFs.v1.3.4.zip`.
2. Extract the entire ZIP to a folder.
3. Double-click `Make Interactive PDFs.exe`.
4. Keep the small desktop window open while using the workspace in Chrome.
5. Choose a PDF and select **Make interactive**.

The app is not code-signed yet, so Windows may show a SmartScreen warning. Only continue if you downloaded it from this repository's official Releases page.

## Private by design

Your PDF is processed locally. It is not uploaded to a cloud service.

The original file stays untouched. The app creates a separate copy, verifies its links, and only then makes it available for download. Temporary files are removed when you start another PDF or close the app.

## Smart, selective OCR

The app uses a PDF's existing selectable text whenever that text is healthy—even when a scanned image sits behind it.

Local OCR runs only on pages where text is missing or unusable. Blank pages are skipped. OCR is used to understand the page and place links; it does not redraw the page or replace its visual design.

## When the app is uncertain

Some PDFs have ambiguous page numbering, damaged text, unusual layouts, or scans that are difficult to read. In these cases, the app returns a review report instead of publishing a PDF that may contain incorrect links.

## Current scope and limitations

In this project, “interactive” currently means internal page-jump links plus clickable web and email addresses. It does not add form fields, audio, video, or other rich-media features.

The Windows app currently:

- Processes one PDF at a time, up to 512 MB
- Does not modify password-protected PDFs
- Does not modify digitally signed PDFs, because doing so would invalidate the signature
- May request review for complex contents pages, poor scans, or unclear page references
- Is distributed as an unsigned folder-based app rather than an installer

## Run from source

Python 3.11 is recommended.

```powershell
git clone https://github.com/sk-zluri/make-interactive-pdfs.git
cd make-interactive-pdfs

py -3.11 scripts\run_isolated.py self-test
.\.venv\Scripts\python.exe -m pip --isolated --disable-pip-version-check install -r requirements-app.txt
.\.venv\Scripts\python.exe -m interactive_pdf_app
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest tests.app_test -v
.\.venv\Scripts\python.exe scripts\run_isolated.py --venv-dir .venv self-test
.\.venv\Scripts\python.exe scripts\run_isolated.py --venv-dir .venv regression-test
```

## License

This project is licensed under the [MIT License](LICENSE). Bundled third-party components remain under their respective licenses.

## Creator

Made with ❤️ by [Sashank Kondepudi](https://www.linkedin.com/in/techhfreakk).
