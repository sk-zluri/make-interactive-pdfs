# Dependencies and runtime

Python 3.10 or newer is required. Always call `scripts/run_isolated.py`; the low-level linker and verifier reject direct execution.

The runner creates `.venv` inside the checkout by default, disables system site packages and user installs, validates checkout ownership, verifies installed versions against every exact release pin, and atomically records requirement stamps. Production `make`/`verify` commands retain the environment lock for their full run so another profile cannot mutate dependencies mid-process. It never installs into system, shared, or agent Python environments.

Core libraries:

- pypdf: https://pypdf.readthedocs.io/
- pdfplumber: https://github.com/jsvine/pdfplumber
- pdfminer.six: https://pdfminersix.readthedocs.io/
- pypdfium2: https://pypdfium2.readthedocs.io/

If the checkout is read-only, put `--venv-dir PATH` before `make` or `verify`. A reusable environment must carry the matching skill ownership marker, checkout path, and disabled-system-site configuration.

## Repository provenance

When the user supplies a GitHub URL, use a fresh clone of that exact URL and pass it to the runner:

```powershell
python scripts/run_isolated.py `
  --expect-repository "https://github.com/sk-zluri/make-interactive-pdfs" `
  --require-clean `
  make "D:\PDF\PATH\HERE\test.pdf" --output-mode root
```

The runner performs one online remote-HEAD acquisition check, pins the full local commit for that process, then uses local repository/commit/clean/bundle checks during environment setup and PDF processing. A remote update during a long run does not invalidate the already acquired commit.

Reports include the canonical/actual repository, full Git commit, clean state, complete release bundle hash, per-file hashes, Python version, and resolved package versions.

## Optional verification

Pixel comparison adds the pinned PyMuPDF profile and runs in memory unless `--save-renders` is explicitly requested:

```powershell
python scripts/run_isolated.py verify source.pdf output.pdf --pixel-compare auto
```

Browser automation is unnecessary for generation and normal verification. Use a live viewer only for explicit user-requested proof or manual approval of ambiguous mappings.

- Browser Harness: https://github.com/browser-use/browser-harness

OCR is not a normal dependency and must never be installed system-wide by the skill. See [heuristics-and-limitations.md](heuristics-and-limitations.md) for the targeted review workflow.
