"""FastAPI server for the loopback-only interactive-PDF application.

The launcher places the per-launch secret in ``#token=<secret>``. URL fragments
are never sent in HTTP requests; the browser UI reads it once and sends it as
``X-App-Token`` on every ``/api`` request.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .jobs import JobManager
from .models import (
    AppError,
    ErrorCode,
    ErrorEnvelope,
    ErrorPayload,
    HealthView,
    JobView,
    TERMINAL_STATUSES,
)


LOGGER = logging.getLogger(__name__)
API_TOKEN_HEADER = "X-App-Token"
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_MULTIPART_OVERHEAD_BYTES = 8 * 1024 * 1024
PDF_HEADER_BYTES = 1024
PDF_HEADER_PATTERN = re.compile(rb"%PDF-[0-9]\.[0-9]")
WINDOWS_INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
ALLOWED_UPLOAD_MEDIA_TYPES = {
    "",
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
}
SAFE_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _model_json(model: ErrorEnvelope) -> dict[str, object]:
    # The fallback keeps the tiny wrapper usable across Pydantic 1/2 transitions.
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model.dict()  # type: ignore[no-any-return,attr-defined]


def _error_response(status_code: int, code: ErrorCode, message: str) -> JSONResponse:
    envelope = ErrorEnvelope(error=ErrorPayload(code=code, message=message))
    return JSONResponse(status_code=status_code, content=_model_json(envelope))


def _validated_filename(filename: str | None) -> str:
    if not filename or not isinstance(filename, str):
        raise AppError(400, ErrorCode.INVALID_FILENAME, "Choose a PDF with a valid filename.")
    if filename != filename.strip() or filename != filename.rstrip(". "):
        raise AppError(400, ErrorCode.INVALID_FILENAME, "The PDF filename is not supported.")
    if WINDOWS_INVALID_FILENAME.search(filename):
        raise AppError(400, ErrorCode.INVALID_FILENAME, "The PDF filename is not supported.")
    if len(filename.encode("utf-8")) > 220:
        raise AppError(400, ErrorCode.INVALID_FILENAME, "The PDF filename is too long.")
    if not filename.casefold().endswith(".pdf") or len(filename) <= 4:
        raise AppError(400, ErrorCode.INVALID_FILE_TYPE, "Choose a file ending in .pdf.")
    stem = filename[:-4].rstrip(". ")
    if not stem or stem.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise AppError(400, ErrorCode.INVALID_FILENAME, "The PDF filename is not supported.")
    return filename


def _validate_media_type(upload: UploadFile) -> None:
    media_type = (upload.content_type or "").split(";", 1)[0].strip().casefold()
    if media_type not in ALLOWED_UPLOAD_MEDIA_TYPES:
        raise AppError(415, ErrorCode.INVALID_FILE_TYPE, "Choose a PDF file.")


def _origin_is_local_and_same_port(request: Request, origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
        request_port = request.url.port or 80
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in SAFE_HOSTS
        and request.url.hostname in SAFE_HOSTS
        and origin_port == request_port
    )


async def _stream_upload(upload: UploadFile, destination: Path) -> int:
    total_bytes = 0
    header_probe = bytearray()
    header_validated = False
    try:
        with destination.open("xb") as output:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise AppError(
                        413,
                        ErrorCode.FILE_TOO_LARGE,
                        "This PDF is larger than the 512 MB limit.",
                    )
                if len(header_probe) < PDF_HEADER_BYTES:
                    remaining = PDF_HEADER_BYTES - len(header_probe)
                    header_probe.extend(chunk[:remaining])
                    if PDF_HEADER_PATTERN.search(header_probe):
                        header_validated = True
                    elif len(header_probe) == PDF_HEADER_BYTES:
                        raise AppError(
                            415,
                            ErrorCode.INVALID_PDF_HEADER,
                            "This file does not appear to be a valid PDF.",
                        )
                output.write(chunk)
        if total_bytes == 0 or not header_validated:
            raise AppError(
                415,
                ErrorCode.INVALID_PDF_HEADER,
                "This file does not appear to be a valid PDF.",
            )
        return total_bytes
    finally:
        await upload.close()


def create_app(
    *,
    api_token: str,
    on_browser_activity: Callable[[], None] | None = None,
) -> FastAPI:
    """Create one app/server session with an unguessable API token."""

    if not api_token or len(api_token) < 32:
        raise ValueError("A strong per-launch API token is required")
    manager = JobManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(
        title="Make Interactive PDFs",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.api_token = api_token
    app.state.job_manager = manager

    @app.middleware("http")
    async def local_session_security(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.hostname not in SAFE_HOSTS:
            return _error_response(400, ErrorCode.INVALID_REQUEST, "Invalid local app address.")

        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            supplied = request.headers.get(API_TOKEN_HEADER, "")
            if len(supplied) > 256 or not secrets.compare_digest(supplied, api_token):
                return _error_response(
                    401,
                    ErrorCode.UNAUTHORIZED,
                    "This app session is no longer valid. Reopen the app and try again.",
                )
            origin = request.headers.get("origin")
            if origin and not _origin_is_local_and_same_port(request, origin):
                return _error_response(
                    403,
                    ErrorCode.INVALID_ORIGIN,
                    "This request did not come from the local app.",
                )

            content_length = request.headers.get("content-length")
            if request.method == "POST" and request.url.path == "/api/jobs" and content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = -1
                if declared_length < 0:
                    return _error_response(
                        400, ErrorCode.INVALID_REQUEST, "The upload size is invalid."
                    )
                if declared_length > MAX_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES:
                    return _error_response(
                        413,
                        ErrorCode.FILE_TOO_LARGE,
                        "This PDF is larger than the 512 MB limit.",
                    )

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, error: AppError) -> JSONResponse:
        return _error_response(error.http_status, error.payload.code, error.payload.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error_response(
            400,
            ErrorCode.INVALID_REQUEST,
            "Choose one PDF file and try again.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> Response:
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            code = ErrorCode.JOB_NOT_FOUND if error.status_code == 404 else ErrorCode.INVALID_REQUEST
            message = "This job no longer exists." if error.status_code == 404 else "Invalid request."
            return _error_response(error.status_code, code, message)
        return PlainTextResponse(str(error.detail), status_code=error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> Response:
        LOGGER.exception("Unexpected local-app error", exc_info=error)
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            return _error_response(
                500,
                ErrorCode.INTERNAL_ERROR,
                "Something unexpected happened. Please try again.",
            )
        return PlainTextResponse("Local app error", status_code=500)

    @app.get("/healthz", response_model=HealthView, include_in_schema=False)
    async def health() -> HealthView:
        return HealthView(active_job=manager.has_active_job, version=__version__)

    @app.post("/api/session/heartbeat", status_code=204, include_in_schema=False)
    async def browser_heartbeat() -> Response:
        if on_browser_activity is not None:
            on_browser_activity()
        return Response(status_code=204)

    @app.post("/api/jobs", response_model=JobView, status_code=202)
    async def create_job(file: UploadFile = File(...)) -> JobView:
        filename = _validated_filename(file.filename)
        _validate_media_type(file)
        job = await manager.reserve_upload(filename)
        try:
            size_bytes = await _stream_upload(file, job.source_path)
            return await manager.start_processing(job, size_bytes)
        except AppError:
            await manager.abort_upload(job)
            raise
        except OSError as exc:
            await manager.abort_upload(job)
            raise AppError(
                500,
                ErrorCode.UPLOAD_FAILED,
                "The PDF could not be saved for processing.",
            ) from exc
        except Exception:
            await manager.abort_upload(job)
            raise

    @app.get("/api/jobs/{job_id}", response_model=JobView)
    async def get_job(job_id: str) -> JobView:
        return await manager.get(job_id)

    @app.post("/api/jobs/{job_id}/cancel", response_model=JobView, status_code=202)
    async def cancel_job(job_id: str) -> JobView:
        return await manager.request_cancel(job_id)

    @app.post(
        "/api/jobs/{job_id}/publish-verified-links",
        response_model=JobView,
        status_code=202,
    )
    async def publish_verified_links(job_id: str) -> JobView:
        return await manager.publish_verified_links(job_id)

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> Response:
        # DELETE doubles as the simple UI's cancellation gesture while active;
        # terminal jobs use the same route for permanent temp-file cleanup.
        current = await manager.get(job_id)
        if current.status not in TERMINAL_STATUSES:
            await manager.request_cancel(job_id)
            return Response(status_code=202)
        await manager.cleanup(job_id)
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}/pdf")
    async def download_pdf(job_id: str) -> FileResponse:
        path, filename = await manager.output_download(job_id)
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=filename,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/jobs/{job_id}/report")
    async def download_report(job_id: str) -> FileResponse:
        path, filename = await manager.report_download(job_id)
        return FileResponse(
            path,
            media_type="application/json",
            filename=filename,
            headers={"Cache-Control": "no-store"},
        )

    static_directory = Path(__file__).resolve().parent / "static"
    if static_directory.is_dir():
        app.mount("/static", StaticFiles(directory=static_directory), name="static")

        @app.get("/", include_in_schema=False)
        async def frontend() -> FileResponse:
            return FileResponse(
                static_directory / "index.html",
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
    else:

        @app.get("/", include_in_schema=False)
        async def frontend_not_installed() -> PlainTextResponse:
            return PlainTextResponse("Make Interactive PDFs is starting…")

    return app


def new_api_token() -> str:
    """Return a per-launch bearer secret suitable for a URL fragment."""

    return secrets.token_urlsafe(32)
