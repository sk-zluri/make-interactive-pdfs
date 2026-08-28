"""Single-worker job orchestration for the local browser application."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .models import (
    AppError,
    ErrorCode,
    ErrorPayload,
    JobStatus,
    JobView,
    ProgressActivity,
    TERMINAL_STATUSES,
)


APP_DATA_DIRECTORY = "MakeInteractivePDFs"
MAX_REPORT_BYTES = 64 * 1024 * 1024
ENGINE_TIMEOUT_SECONDS = 30 * 60
MAX_PROGRESS_LINE_BYTES = 8 * 1024
PROGRESS_PHASE_RANGES: dict[str, tuple[int, int]] = {
    "PREFLIGHT": (8, 10),
    "READING_PAGES": (10, 30),
    "OCR_PAGES": (30, 65),
    "PLANNING_LINKS": (65, 73),
    "WRITING_LINKS": (73, 80),
    "VALIDATING_PAGES": (80, 84),
    "VERIFYING_PAGES": (84, 97),
    "VERIFYING_LINKS": (97, 99),
}
PROGRESS_PHASE_LABELS = {
    "PREFLIGHT": "Checking the PDF",
    "READING_PAGES": "Reading pages",
    "OCR_PAGES": "Reading scanned pages with OCR",
    "PLANNING_LINKS": "Matching link targets",
    "WRITING_LINKS": "Adding clickable areas",
    "VALIDATING_PAGES": "Checking the new PDF",
    "VERIFYING_PAGES": "Verifying every page",
    "VERIFYING_LINKS": "Verifying every link",
}


class EngineTimeoutError(RuntimeError):
    """Raised when one deterministic engine phase exceeds its safety limit."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _workflow_progress(phase: str, completed: int, total: int) -> int:
    start, end = PROGRESS_PHASE_RANGES.get(phase, (8, 99))
    if total <= 0:
        return start
    ratio = min(1.0, max(0.0, completed / total))
    return int(round(start + ((end - start) * ratio)))


def _local_temp_root() -> Path:
    """Keep every uploaded document under LOCALAPPDATA on Windows."""

    configured = os.environ.get("MAKE_INTERACTIVE_PDFS_APP_DATA")
    if configured:
        local_root = Path(configured).expanduser()
    else:
        local_root_text = os.environ.get("LOCALAPPDATA")
        local_root = Path(local_root_text) if local_root_text else Path(tempfile.gettempdir())
    return (local_root / APP_DATA_DIRECTORY / "Temp").resolve()


def _safe_rmtree(path: Path, *, parent: Path) -> None:
    """Remove one app-owned tree, never the parent or an unrelated directory."""

    resolved_path = path.resolve(strict=False)
    resolved_parent = parent.resolve(strict=False)
    if resolved_path == resolved_parent or resolved_parent not in resolved_path.parents:
        raise RuntimeError(f"Refusing to remove path outside app temp root: {resolved_path}")
    if resolved_path.is_symlink():
        resolved_path.unlink(missing_ok=True)
    elif resolved_path.exists():
        shutil.rmtree(resolved_path)


def _application_root() -> Path:
    configured = os.environ.get("MAKE_INTERACTIVE_PDFS_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _runner_path() -> Path:
    configured = os.environ.get("MAKE_INTERACTIVE_PDFS_RUNNER")
    if configured:
        return Path(configured).expanduser().resolve()
    return _application_root() / "scripts" / "run_isolated.py"


def _runtime_python() -> Path:
    """Find an interpreter now and leave a clear seam for a frozen runtime."""

    configured = os.environ.get("MAKE_INTERACTIVE_PDFS_PYTHON")
    if configured:
        return Path(configured).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()

    executable_root = Path(sys.executable).resolve().parent
    bundle_root = Path(getattr(sys, "_MEIPASS", executable_root)).resolve()
    candidates = (
        executable_root / "runtime" / "python.exe",
        executable_root / "python" / "python.exe",
        bundle_root / "runtime" / "python.exe",
        bundle_root / "python" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("The packaged PDF runtime is unavailable")


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size <= 0 or size > MAX_REPORT_BYTES:
        raise ValueError("Engine report has an invalid size")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Engine report must be a JSON object")
    return value


def _read_log_tail(path: Path, limit: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _engine_error(log_text: str, *, verification: bool = False) -> ErrorPayload:
    lowered = log_text.casefold()
    if "digital signature" in lowered or "digitally signed" in lowered:
        return ErrorPayload(
            code=ErrorCode.SIGNED_PDF,
            message="This PDF is digitally signed, so changing it would invalidate the signature.",
        )
    if "encrypted pdf requires" in lowered or "requires a valid --password" in lowered:
        return ErrorPayload(
            code=ErrorCode.PASSWORD_REQUIRED,
            message="This PDF is password-protected. Password-protected PDFs are not supported yet.",
        )
    if (
        "environment is unavailable" in lowered
        or "installed dependency" in lowered
        or "no module named" in lowered
        or "python runtime" in lowered
    ):
        return ErrorPayload(
            code=ErrorCode.ENGINE_UNAVAILABLE,
            message="The PDF processor is unavailable. Please restart the app and try again.",
        )
    if verification:
        return ErrorPayload(
            code=ErrorCode.VERIFICATION_FAILED,
            message="The result could not be verified, so no PDF was published.",
        )
    return ErrorPayload(
        code=ErrorCode.PROCESSING_FAILED,
        message="This PDF could not be processed safely.",
    )


@dataclass(slots=True)
class Job:
    id: str
    directory: Path
    filename: str
    source_path: Path
    output_path: Path
    report_path: Path
    verification_path: Path
    output_filename: str
    report_filename: str
    status: JobStatus = JobStatus.UPLOADING
    stage: str = "UPLOADING"
    progress: int = 2
    message: str = "Uploading PDF…"
    size_bytes: int = 0
    reasons: list[str] = field(default_factory=list)
    link_counts: dict[str, int] = field(default_factory=dict)
    progress_sequence: int = 0
    activity: ProgressActivity = field(default_factory=ProgressActivity)
    error: ErrorPayload | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    cancel_requested: bool = False
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class JobManager:
    """Own app artifacts and allow at most one upload/worker at a time."""

    def __init__(self) -> None:
        self.base_root = _local_temp_root()
        self.base_root.mkdir(parents=True, exist_ok=True)
        self.launch_root = (self.base_root / f"launch-{uuid.uuid4().hex}").resolve()
        self.launch_root.mkdir(parents=False, exist_ok=False)
        self._jobs: dict[str, Job] = {}
        self._active_job_id: str | None = None
        self._guard = asyncio.Lock()
        self._closing = False

    @property
    def has_active_job(self) -> bool:
        return self._active_job_id is not None

    async def reserve_upload(self, filename: str) -> Job:
        async with self._guard:
            if self._closing:
                raise AppError(
                    503,
                    ErrorCode.ENGINE_UNAVAILABLE,
                    "The app is shutting down. Please reopen it and try again.",
                )
            if self._active_job_id is not None:
                raise AppError(
                    409,
                    ErrorCode.APP_BUSY,
                    "Another PDF is being processed. Please wait for it to finish.",
                )

            job_id = uuid.uuid4().hex
            job_directory = (self.launch_root / job_id).resolve()
            if self.launch_root not in job_directory.parents:
                raise RuntimeError("Invalid generated job path")
            job_directory.mkdir(parents=False, exist_ok=False)

            stem = filename[:-4].rstrip() or "Document"
            output_filename = f"{stem} - Interactive.pdf"
            report_filename = f"{stem} - Report.json"
            job = Job(
                id=job_id,
                directory=job_directory,
                filename=filename,
                source_path=job_directory / "source.pdf",
                output_path=job_directory / "interactive.pdf",
                report_path=job_directory / "link-report.json",
                verification_path=job_directory / "verification.json",
                output_filename=output_filename,
                report_filename=report_filename,
            )
            self._jobs[job_id] = job
            self._active_job_id = job_id
            return job

    async def abort_upload(self, job: Job) -> None:
        async with self._guard:
            self._jobs.pop(job.id, None)
            if self._active_job_id == job.id:
                self._active_job_id = None
        try:
            await asyncio.to_thread(_safe_rmtree, job.directory, parent=self.launch_root)
        except OSError:
            # The launch-root cleanup is a second, guaranteed attempt on shutdown.
            pass

    async def start_processing(self, job: Job, size_bytes: int) -> JobView:
        async with self._guard:
            current = self._jobs.get(job.id)
            if current is not job or self._active_job_id != job.id:
                raise AppError(404, ErrorCode.JOB_NOT_FOUND, "This job no longer exists.")
            job.size_bytes = size_bytes
            job.status = JobStatus.QUEUED
            job.stage = "PREFLIGHT"
            job.progress = 8
            job.message = "PDF received. Starting…"
            job.updated_at = _utc_now()
            job.task = asyncio.create_task(self._process_job(job), name=f"pdf-job-{job.id}")
            return self._view(job)

    async def get(self, job_id: str) -> JobView:
        async with self._guard:
            return self._view(self._require_job(job_id))

    async def request_cancel(self, job_id: str) -> JobView:
        process: asyncio.subprocess.Process | None = None
        async with self._guard:
            job = self._require_job(job_id)
            if job.status in TERMINAL_STATUSES:
                return self._view(job)
            first_request = not job.cancel_requested
            job.cancel_requested = True
            job.status = JobStatus.CANCELLING
            job.stage = "CANCELLING"
            job.message = "Stopping safely…"
            job.updated_at = _utc_now()
            if first_request:
                process = job.process
            view = self._view(job)
        if process is not None:
            await self._terminate_process_tree(process)
        return view

    async def cleanup(self, job_id: str) -> None:
        async with self._guard:
            job = self._require_job(job_id)
            if job.status not in TERMINAL_STATUSES:
                raise AppError(
                    409,
                    ErrorCode.JOB_NOT_READY,
                    "Stop this job or wait for it to finish before removing it.",
                )
            job_directory = job.directory
        try:
            await asyncio.to_thread(_safe_rmtree, job_directory, parent=self.launch_root)
        except OSError as exc:
            raise AppError(
                500,
                ErrorCode.CLEANUP_FAILED,
                "The job files are still in use. Close any open download and try again.",
            ) from exc
        async with self._guard:
            self._jobs.pop(job_id, None)

    async def output_download(self, job_id: str) -> tuple[Path, str]:
        async with self._guard:
            job = self._require_job(job_id)
            if job.status != JobStatus.PASS or not job.output_path.is_file():
                raise AppError(
                    409,
                    ErrorCode.DOWNLOAD_NOT_AVAILABLE,
                    "The interactive PDF is available only after verification passes.",
                )
            return job.output_path, job.output_filename

    async def report_download(self, job_id: str) -> tuple[Path, str]:
        async with self._guard:
            job = self._require_job(job_id)
            if (
                job.status not in {JobStatus.PASS, JobStatus.NEEDS_REVIEW}
                or not job.report_path.is_file()
            ):
                raise AppError(
                    409,
                    ErrorCode.DOWNLOAD_NOT_AVAILABLE,
                    "A report is available only for completed or review-needed jobs.",
                )
            return job.report_path, job.report_filename

    async def shutdown(self) -> None:
        async with self._guard:
            self._closing = True
            live_jobs = [job for job in self._jobs.values() if job.status not in TERMINAL_STATUSES]
            processes = []
            tasks = []
            for job in live_jobs:
                job.cancel_requested = True
                if job.process is not None:
                    processes.append(job.process)
                if job.task is not None:
                    tasks.append(job.task)
        await asyncio.gather(
            *(self._terminate_process_tree(process) for process in processes),
            return_exceptions=True,
        )
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=15)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except (Exception, asyncio.CancelledError):
                    pass
        try:
            await asyncio.to_thread(_safe_rmtree, self.launch_root, parent=self.base_root)
        except OSError:
            pass

    def _require_job(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise AppError(404, ErrorCode.JOB_NOT_FOUND, "This job no longer exists.")
        return job

    def _view(self, job: Job) -> JobView:
        link_counts = {key: max(0, int(value)) for key, value in job.link_counts.items()}
        pdf_ready = job.status == JobStatus.PASS and job.output_path.is_file()
        report_ready = (
            job.status in {JobStatus.PASS, JobStatus.NEEDS_REVIEW} and job.report_path.is_file()
        )
        return JobView(
            id=job.id,
            job_id=job.id,
            status=job.status,
            state=job.status,
            stage=job.stage,
            progress=job.progress,
            message=job.message,
            filename=job.filename,
            size_bytes=job.size_bytes,
            reasons=list(job.reasons),
            links=sum(link_counts.values()),
            link_counts=link_counts,
            progress_sequence=job.progress_sequence,
            activity=job.activity.model_copy(deep=True),
            pdf_ready=pdf_ready,
            report_ready=report_ready,
            pdf_available=pdf_ready,
            report_available=report_ready,
            error=job.error,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    async def _update(
        self,
        job: Job,
        *,
        status: JobStatus,
        stage: str,
        progress: int,
        message: str,
        error: ErrorPayload | None = None,
    ) -> None:
        async with self._guard:
            job.status = status
            job.stage = stage
            job.progress = max(job.progress, progress)
            job.message = message
            job.error = error
            job.updated_at = _utc_now()

    async def _process_job(self, job: Job) -> None:
        try:
            await self._update(
                job,
                status=JobStatus.PROCESSING,
                stage="ANALYZING",
                progress=18,
                message="Finding links and page destinations…",
            )
            make_return_code, make_log = await self._run_engine(
                job,
                "make",
                (
                    "make",
                    str(job.source_path),
                    "--output",
                    str(job.output_path),
                    "--report-json",
                    str(job.report_path),
                ),
            )
            if job.cancel_requested:
                await self._finish_cancelled(job)
                return

            try:
                report = _read_json_object(job.report_path)
            except (OSError, ValueError, json.JSONDecodeError):
                if make_return_code == 1:
                    await self._finish_failed(job, _engine_error(make_log))
                else:
                    await self._finish_failed(
                        job,
                        ErrorPayload(
                            code=ErrorCode.PROCESSING_FAILED,
                            message="The PDF processor returned an invalid report.",
                        ),
                    )
                return

            self._apply_report(job, report)
            report_status = report.get("status")
            if report_status == JobStatus.NEEDS_REVIEW.value and make_return_code == 2:
                await self._update(
                    job,
                    status=JobStatus.NEEDS_REVIEW,
                    stage="COMPLETE",
                    progress=100,
                    message="This PDF needs review before safe links can be published.",
                )
                await self._remove_intermediates(job, keep_output=False, keep_report=True)
                return

            if (
                make_return_code != 0
                or report_status != JobStatus.PASS.value
                or not job.output_path.is_file()
            ):
                await self._finish_failed(job, _engine_error(make_log))
                return

            await self._update(
                job,
                status=JobStatus.VERIFYING,
                stage="VERIFYING",
                progress=82,
                message="Checking every link and confirming the design is unchanged…",
            )
            verify_return_code, verify_log = await self._run_engine(
                job,
                "verify",
                (
                    "verify",
                    str(job.source_path),
                    str(job.output_path),
                    "--link-report",
                    str(job.report_path),
                    "--json",
                    str(job.verification_path),
                ),
            )
            if job.cancel_requested:
                await self._finish_cancelled(job)
                return

            try:
                verification = _read_json_object(job.verification_path)
            except (OSError, ValueError, json.JSONDecodeError):
                verification = {}
            if verify_return_code != 0 or verification.get("status") != JobStatus.PASS.value:
                await self._finish_failed(job, _engine_error(verify_log, verification=True))
                return

            await self._update(
                job,
                status=JobStatus.PASS,
                stage="COMPLETE",
                progress=100,
                message="Your verified interactive PDF is ready.",
            )
            await self._remove_intermediates(job, keep_output=True, keep_report=True)
        except EngineTimeoutError:
            await self._finish_failed(
                job,
                ErrorPayload(
                    code=ErrorCode.PROCESSING_FAILED,
                    message="Processing took too long and was stopped safely. No PDF was published.",
                ),
            )
        except FileNotFoundError:
            await self._finish_failed(
                job,
                ErrorPayload(
                    code=ErrorCode.ENGINE_UNAVAILABLE,
                    message="The PDF processor is unavailable. Please restart the app and try again.",
                ),
            )
        except asyncio.CancelledError:
            if job.process is not None:
                await self._terminate_process_tree(job.process)
            await self._finish_cancelled(job)
            raise
        except Exception:
            await self._finish_failed(
                job,
                ErrorPayload(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="Something unexpected happened. No PDF was published.",
                ),
            )
        finally:
            async with self._guard:
                job.process = None
                if self._active_job_id == job.id:
                    self._active_job_id = None

    def _apply_report(self, job: Job, report: dict[str, Any]) -> None:
        reasons = report.get("review_reasons")
        job.reasons = [str(item) for item in reasons if isinstance(item, str)] if isinstance(
            reasons, list
        ) else []

        raw_counts = report.get("final_links")
        counts: dict[str, int] = {}
        if isinstance(raw_counts, dict):
            for key in ("internal", "external"):
                value = raw_counts.get(key, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    counts[key] = value
        job.link_counts = counts

    async def _apply_progress_event(self, job: Job, event: dict[str, Any]) -> None:
        if event.get("schema_version") != 1:
            return
        phase = str(event.get("phase", "")).upper()
        if phase not in PROGRESS_PHASE_RANGES:
            return
        completed = event.get("completed")
        total = event.get("total")
        if (
            not isinstance(completed, int)
            or isinstance(completed, bool)
            or completed < 0
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or (total > 0 and completed > total)
        ):
            return

        page = event.get("page")
        total_pages = event.get("total_pages")
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            page = None
        if (
            not isinstance(total_pages, int)
            or isinstance(total_pages, bool)
            or total_pages < 1
        ):
            total_pages = None
        if page is not None and total_pages is not None and page > total_pages:
            page = None

        unit = str(event.get("unit", ""))[:24].casefold()
        raw_message = str(event.get("message", "")).strip()
        message = " ".join(raw_message.split())[:180]
        label = PROGRESS_PHASE_LABELS[phase]

        async with self._guard:
            if job.status in TERMINAL_STATUSES or job.cancel_requested:
                return
            history = list(job.activity.history)
            history_message = message or label
            if not history or history[-1] != history_message:
                history.append(history_message)
            job.progress_sequence += 1
            job.stage = phase
            job.progress = max(job.progress, _workflow_progress(phase, completed, total))
            job.message = message or label
            job.activity = ProgressActivity(
                phase=phase,
                label=label,
                completed=completed,
                total=total,
                unit=unit,
                page=page,
                total_pages=total_pages,
                history=history[-5:],
            )
            job.updated_at = _utc_now()

    async def _consume_progress_file(
        self, job: Job, progress_path: Path, offset: int
    ) -> int:
        def read_complete_lines() -> tuple[int, list[bytes]]:
            if not progress_path.is_file():
                return offset, []
            with progress_path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(1024 * 1024)
            final_newline = payload.rfind(b"\n")
            if final_newline < 0:
                return offset, []
            complete = payload[: final_newline + 1]
            return offset + final_newline + 1, complete.splitlines()

        try:
            new_offset, lines = await asyncio.to_thread(read_complete_lines)
        except OSError:
            # The engine result is authoritative. Progress is best-effort and
            # may briefly be unavailable because of antivirus/file locking or
            # teardown races on Windows.
            return offset
        for line in lines:
            if not line or len(line) > MAX_PROGRESS_LINE_BYTES:
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                await self._apply_progress_event(job, event)
        return new_offset

    async def _follow_progress(
        self, job: Job, progress_path: Path, process: asyncio.subprocess.Process
    ) -> None:
        offset = 0
        while process.returncode is None:
            offset = await self._consume_progress_file(job, progress_path, offset)
            await asyncio.sleep(0.15)
        await self._consume_progress_file(job, progress_path, offset)

    async def _run_engine(
        self,
        job: Job,
        phase: str,
        arguments: Sequence[str],
    ) -> tuple[int, str]:
        packaged = bool(getattr(sys, "frozen", False))
        if packaged:
            command = (
                str(Path(sys.executable).resolve()),
                "--internal-worker",
                phase,
                str(job.directory),
            )
            working_directory = Path(sys.executable).resolve().parent
        else:
            runner = _runner_path()
            runtime = _runtime_python()
            if not runner.is_file() or not runtime.is_file():
                raise FileNotFoundError("PDF engine runtime or runner is unavailable")
            command = (str(runtime), str(runner), *arguments)
            working_directory = _application_root()

        log_path = job.directory / f"{phase}.log"
        progress_path = job.directory / f"{phase}-progress.jsonl"
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["MAKE_INTERACTIVE_PDFS_PROGRESS"] = str(progress_path)
        if packaged:
            environment["MAKE_INTERACTIVE_PDFS_WORKER_LOG"] = str(log_path)
            environment["MAKE_INTERACTIVE_PDFS_JOB_ROOT"] = str(self.launch_root)
        with log_path.open("xb") as log_handle:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(working_directory),
                env=environment,
                stdout=log_handle,
                stderr=log_handle,
                creationflags=creation_flags,
            )
            async with self._guard:
                job.process = process
                cancel_now = job.cancel_requested
            progress_task = asyncio.create_task(
                self._follow_progress(job, progress_path, process),
                name=f"pdf-progress-{job.id}-{phase}",
            )
            try:
                if cancel_now:
                    await self._terminate_process_tree(process)
                return_code = await asyncio.wait_for(
                    process.wait(),
                    timeout=ENGINE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as error:
                await self._terminate_process_tree(process)
                raise EngineTimeoutError(f"{phase} exceeded the processing time limit") from error
            finally:
                try:
                    await asyncio.wait_for(progress_task, timeout=2)
                except asyncio.TimeoutError:
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                except Exception:
                    # Never let a telemetry reader failure mask the worker's
                    # real exit code or otherwise fail a completed PDF job.
                    pass
                async with self._guard:
                    if job.process is process:
                        job.process = None
        return return_code, _read_log_tail(log_path)

    async def _finish_failed(self, job: Job, error: ErrorPayload) -> None:
        await self._update(
            job,
            status=JobStatus.FAIL,
            stage="FAILED",
            progress=100,
            message=error.message,
            error=error,
        )
        try:
            await asyncio.to_thread(_safe_rmtree, job.directory, parent=self.launch_root)
        except OSError:
            pass

    async def _finish_cancelled(self, job: Job) -> None:
        error = ErrorPayload(code=ErrorCode.CANCELLED, message="Processing was cancelled.")
        await self._update(
            job,
            status=JobStatus.CANCELLED,
            stage="CANCELLED",
            progress=100,
            message=error.message,
            error=error,
        )
        try:
            await asyncio.to_thread(_safe_rmtree, job.directory, parent=self.launch_root)
        except OSError:
            pass

    async def _remove_intermediates(
        self,
        job: Job,
        *,
        keep_output: bool,
        keep_report: bool,
    ) -> None:
        retained = {job.output_path if keep_output else None, job.report_path if keep_report else None}
        candidates = (
            job.source_path,
            job.output_path,
            job.report_path,
            job.verification_path,
            job.directory / "make.log",
            job.directory / "verify.log",
        )
        for path in candidates:
            if path in retained:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(killer.wait(), timeout=8)
            except (FileNotFoundError, OSError, asyncio.TimeoutError):
                process.terminate()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=8)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
