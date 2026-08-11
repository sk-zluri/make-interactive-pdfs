# Dependencies

The core linker requires Python 3.10 or newer and the packages in `requirements.txt`.

- Python: https://www.python.org/downloads/
- pypdf: https://pypdf.readthedocs.io/
- pdfplumber: https://github.com/jsvine/pdfplumber
- PyMuPDF: https://pymupdf.readthedocs.io/

Install them with:

```powershell
python -m pip install -r requirements.txt
```

## Browser click verification

Browser automation is optional for generating annotations but recommended for final click testing.

- browser-harness: https://github.com/browser-use/browser-harness

After installing browser-harness and enabling Chrome remote debugging, open the local `file:///...pdf` URL, click representative TOC rows, and confirm the PDF viewer's physical page indicator.

If browser-harness is unavailable, verify with Chrome or Adobe Acrobat manually. Do not skip structural verification with `scripts/verify_interactive_pdf.py`.
