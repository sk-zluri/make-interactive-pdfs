# Dependencies

The default linker and structural verifier require Python 3.10 or newer. Always use `scripts/run_isolated.py`; it creates `.venv` inside the skill checkout, installs only the required profile there, and reuses it while the requirements hash is unchanged.

- Python: https://www.python.org/downloads/
- pypdf: https://pypdf.readthedocs.io/
- pdfplumber: https://github.com/jsvine/pdfplumber

Run the default workflow with:

```powershell
python scripts/run_isolated.py make "D:\PDF\PATH\HERE\test.pdf" --output-mode root
python scripts/run_isolated.py verify "D:\PDF\PATH\HERE\test.pdf" "D:\PDF\PATH\HERE\test - Interactive.pdf" --require-internal
```

Never run `pip install` against the system interpreter, a shared tool environment, or the agent's own environment. The low-level linker and verifier reject direct execution. If the skill checkout is read-only, place the dedicated environment elsewhere by putting `--venv-dir PATH` before `make` or `verify`; an existing environment is accepted only when it carries this skill's matching ownership marker and disables system site packages.

## Repository provenance

When the user supplies a GitHub URL, use a clean checkout from that URL and verify it before processing:

```powershell
python scripts/skill_provenance.py --expect-repository "https://github.com/sk-zluri/make-interactive-pdfs" --require-git --require-clean
```

The provenance check also requires local `HEAD` to match the commit advertised by the repository's remote `HEAD`. Its report includes the isolated-runtime profile, skill version, canonical and actual repository URLs, full Git commit, dirty state, per-file hashes, and one combined bundle hash. Generation and verification reports embed the same provenance data.

Also pass `--expect-repository URL` and the captured `--expect-commit FULL_SHA` before every `make` and `verify`. The isolated runner checks before environment setup, immediately before launch, and after processing; it fails on a missing/mismatched Git checkout, unpublished/non-current commit, or dirty worktree.

## Optional pixel comparison

Pixel comparison uses PyMuPDF and runs entirely in memory unless `--save-renders` is supplied. The isolated runner selects `requirements-pixel.txt` automatically.

- PyMuPDF: https://pymupdf.readthedocs.io/

```powershell
python scripts/run_isolated.py verify source.pdf output.pdf --pixel-compare auto
```

## Optional live-viewer verification

Browser automation is not required for normal generation or verification. Use it only when the user explicitly requests proof that a particular PDF viewer responds to real clicks.

- browser-harness: https://github.com/browser-use/browser-harness

Alternatively, open the PDF manually in Chrome or Adobe Acrobat and click representative links. Structural verification through `scripts/run_isolated.py verify` remains the default acceptance check.
