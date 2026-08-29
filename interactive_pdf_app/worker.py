"""Run the deterministic PDF engine inside a packaged application process."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Sequence


_COMMAND_MODULES = {
    "make": "make_interactive_pdf",
    "make-verified": "make_interactive_pdf",
    "verify": "verify_interactive_pdf",
}
_INHERITED_DEVELOPMENT_VARIABLES = (
    "MAKE_INTERACTIVE_PDFS_ADVERTISED_HEAD",
    "MAKE_INTERACTIVE_PDFS_EXPECTED_COMMIT",
    "MAKE_INTERACTIVE_PDFS_EXPECTED_REPOSITORY",
    "MAKE_INTERACTIVE_PDFS_PYTHON",
    "MAKE_INTERACTIVE_PDFS_ROOT",
    "MAKE_INTERACTIVE_PDFS_RUNNER",
)


def _bundle_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[1]


def _configure_worker_streams() -> None:
    """Give a windowed PyInstaller child a useful private log stream."""

    log_path = os.environ.get("MAKE_INTERACTIVE_PDFS_WORKER_LOG")
    if not log_path:
        return
    stream = Path(log_path).open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream


def _validated_job_directory(raw_path: str) -> Path:
    root_value = os.environ.get("MAKE_INTERACTIVE_PDFS_JOB_ROOT")
    if not root_value:
        raise RuntimeError("The internal worker has no app-owned job root")
    job_root = Path(root_value).resolve(strict=True)
    job_directory = Path(raw_path).resolve(strict=True)
    if job_root not in job_directory.parents:
        raise RuntimeError("The internal worker job path is outside the app-owned root")
    return job_directory


def run_internal_worker(arguments: Sequence[str]) -> int:
    """Dispatch one fixed-shape engine command without launching a web server."""

    if not getattr(sys, "frozen", False):
        raise RuntimeError("The internal worker is available only in the packaged app")
    if len(arguments) != 2 or arguments[0] not in _COMMAND_MODULES:
        raise RuntimeError("Unknown internal PDF worker command")

    _configure_worker_streams()
    command = arguments[0]
    job_directory = _validated_job_directory(arguments[1])
    bundle_root = _bundle_root()
    scripts_root = bundle_root / "scripts"
    if str(scripts_root) not in sys.path:
        sys.path.insert(0, str(scripts_root))

    for variable in _INHERITED_DEVELOPMENT_VARIABLES:
        os.environ.pop(variable, None)
    os.environ["MAKE_INTERACTIVE_PDFS_ISOLATED"] = "1"
    os.environ["MAKE_INTERACTIVE_PDFS_PACKAGED"] = "1"
    os.environ["MAKE_INTERACTIVE_PDFS_PROFILE"] = "core"
    os.environ["MAKE_INTERACTIVE_PDFS_ENVIRONMENT"] = str(bundle_root)

    module = importlib.import_module(_COMMAND_MODULES[command])
    source_path = job_directory / "source.pdf"
    output_path = job_directory / "interactive.pdf"
    report_path = job_directory / "link-report.json"
    verification_path = job_directory / "verification.json"
    if command in {"make", "make-verified"}:
        engine_arguments = (
            str(source_path),
            "--output",
            str(output_path),
            "--report-json",
            str(report_path),
        )
        if command == "make-verified":
            engine_arguments = (
                str(source_path),
                "--link-manifest",
                str(job_directory / "review-source.json"),
                "--publish-confirmed-links",
                "--output",
                str(output_path),
                "--report-json",
                str(report_path),
                "--force",
            )
    else:
        engine_arguments = (
            str(source_path),
            str(output_path),
            "--link-report",
            str(report_path),
            "--json",
            str(verification_path),
        )
    previous_argv = sys.argv
    try:
        sys.argv = [f"{command}.py", *engine_arguments]
        return int(module.main())
    finally:
        sys.argv = previous_argv
