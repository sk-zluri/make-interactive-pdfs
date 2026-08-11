# Dependencies

The default linker and structural verifier require Python 3.10 or newer and only the packages in `requirements.txt`.

- Python: https://www.python.org/downloads/
- pypdf: https://pypdf.readthedocs.io/
- pdfplumber: https://github.com/jsvine/pdfplumber

Install the default dependencies with:

```powershell
python -m pip install -r requirements.txt
```

## Optional pixel comparison

Pixel comparison uses PyMuPDF and runs entirely in memory unless `--save-renders` is supplied.

- PyMuPDF: https://pymupdf.readthedocs.io/

```powershell
python -m pip install -r requirements-pixel.txt
```

## Optional live-viewer verification

Browser automation is not required for normal generation or verification. Use it only when the user explicitly requests proof that a particular PDF viewer responds to real clicks.

- browser-harness: https://github.com/browser-use/browser-harness

Alternatively, open the PDF manually in Chrome or Adobe Acrobat and click representative links. Structural verification with `scripts/verify_interactive_pdf.py` remains the default acceptance check.
