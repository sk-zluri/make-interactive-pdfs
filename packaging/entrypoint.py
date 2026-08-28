"""PyInstaller entry point for the local browser application."""

import os
import sys
import traceback
from pathlib import Path


# Windowed PyInstaller applications start without console streams. Uvicorn and
# standard logging expect writable streams even when their output is hidden.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-worker":
        from interactive_pdf_app.worker import run_internal_worker

        raise SystemExit(run_internal_worker(sys.argv[2:]))

    try:
        from interactive_pdf_app.__main__ import main

        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        error_log = None
        try:
            app_data = Path(
                os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
            ) / "Make Interactive PDFs"
            app_data.mkdir(parents=True, exist_ok=True)
            error_log = app_data / "startup-error.log"
            error_log.write_text(traceback.format_exc(), encoding="utf-8")
        except (OSError, RuntimeError):
            error_log = None
        if os.name == "nt":
            try:
                import ctypes

                detail = (
                    f"\n\nDiagnostic details were saved to:\n{error_log}"
                    if error_log is not None
                    else ""
                )
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Make Interactive PDFs could not start. Close this message and "
                    f"open the app again.{detail}",
                    "Make Interactive PDFs",
                    0x10,
                )
            except (AttributeError, OSError):
                pass
        raise SystemExit(1)
