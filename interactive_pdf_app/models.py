"""Stable public API types for the local application."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    """Product and transitional job states exposed to the browser."""

    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    VERIFYING = "VERIFYING"
    CANCELLING = "CANCELLING"
    PASS = "PASS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAIL = "FAIL"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.PASS,
        JobStatus.NEEDS_REVIEW,
        JobStatus.FAIL,
        JobStatus.CANCELLED,
    }
)


class ErrorCode(str, Enum):
    """Stable codes; UI copy can change without changing API behavior."""

    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_ORIGIN = "INVALID_ORIGIN"
    INVALID_REQUEST = "INVALID_REQUEST"
    APP_BUSY = "APP_BUSY"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_READY = "JOB_NOT_READY"
    INVALID_FILENAME = "INVALID_FILENAME"
    INVALID_FILE_TYPE = "INVALID_FILE_TYPE"
    INVALID_PDF_HEADER = "INVALID_PDF_HEADER"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    DOWNLOAD_NOT_AVAILABLE = "DOWNLOAD_NOT_AVAILABLE"
    PASSWORD_REQUIRED = "PASSWORD_REQUIRED"
    SIGNED_PDF = "SIGNED_PDF"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANCELLED = "CANCELLED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorPayload(BaseModel):
    code: ErrorCode
    message: str


class ErrorEnvelope(BaseModel):
    error: ErrorPayload


class ProgressActivity(BaseModel):
    """Latest real engine activity shown while a job is running."""

    phase: str = "PREFLIGHT"
    label: str = "Starting local processing"
    completed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    unit: str = ""
    page: int | None = Field(default=None, ge=1)
    total_pages: int | None = Field(default=None, ge=1)
    history: list[str] = Field(default_factory=list)


class JobView(BaseModel):
    """A deliberately small polling contract for the browser UI."""

    id: str
    job_id: str
    status: JobStatus
    state: JobStatus
    stage: str
    progress: int = Field(ge=0, le=100)
    message: str
    filename: str
    size_bytes: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)
    links: int = Field(default=0, ge=0)
    link_counts: dict[str, int] = Field(default_factory=dict)
    progress_sequence: int = Field(default=0, ge=0)
    activity: ProgressActivity = Field(default_factory=ProgressActivity)
    pdf_ready: bool = False
    report_ready: bool = False
    # Compatibility aliases used by the first browser UI.
    pdf_available: bool = False
    report_available: bool = False
    error: ErrorPayload | None = None
    created_at: str
    updated_at: str


class HealthView(BaseModel):
    status: str = "ok"
    active_job: bool
    version: str


class AppError(Exception):
    """Expected API failure with a stable, user-safe payload."""

    def __init__(self, http_status: int, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.payload = ErrorPayload(code=code, message=message)
