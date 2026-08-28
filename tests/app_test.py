#!/usr/bin/env python3
"""Focused tests for the local browser application boundary."""

from __future__ import annotations

import asyncio
import http.client
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import uvicorn

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from interactive_pdf_app import jobs
from interactive_pdf_app.__main__ import BrowserSessionMonitor
from interactive_pdf_app.jobs import EngineTimeoutError, JobManager, _safe_rmtree
from interactive_pdf_app.models import AppError, ErrorCode, ErrorPayload, JobStatus
from interactive_pdf_app.native_window import NativeController
from interactive_pdf_app.server import _validated_filename, create_app
from interactive_pdf_app.worker import _validated_job_directory, run_internal_worker
from scripts import progress_events


class LiveServer:
    """Run the ASGI app on a pre-bound loopback socket for HTTP boundary tests."""

    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(socket.SOMAXCONN)
        self.port = int(self.socket.getsockname()[1])
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
            server_header=False,
            date_header=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run,
            kwargs={"sockets": [self.socket]},
            daemon=True,
        )

    def __enter__(self) -> "LiveServer":
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                status, _, _ = self.request("GET", "/healthz")
                if status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.02)
        raise RuntimeError("Local test server did not start")

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Local test server did not stop")

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            response_headers = {key.casefold(): value for key, value in response.getheaders()}
            return response.status, response_headers, response.read()
        finally:
            connection.close()


class ServerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="interactive-pdf-app-test-")
        self.previous_app_data = os.environ.get("MAKE_INTERACTIVE_PDFS_APP_DATA")
        os.environ["MAKE_INTERACTIVE_PDFS_APP_DATA"] = self.temp.name

    def tearDown(self) -> None:
        if self.previous_app_data is None:
            os.environ.pop("MAKE_INTERACTIVE_PDFS_APP_DATA", None)
        else:
            os.environ["MAKE_INTERACTIVE_PDFS_APP_DATA"] = self.previous_app_data
        self.temp.cleanup()

    def test_loopback_auth_origin_and_security_headers(self) -> None:
        token = "t" * 40
        app = create_app(api_token=token)
        with LiveServer(app) as server:
            status, headers, body = server.request("GET", "/")
            self.assertEqual(status, 200)
            self.assertIn(b"Make Interactive PDFs", body)
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("x-frame-options"), "DENY")
            self.assertIn("default-src 'self'", headers.get("content-security-policy", ""))

            status, _, _ = server.request("GET", "/api/jobs/missing")
            self.assertEqual(status, 401)

            status, _, _ = server.request(
                "GET",
                "/api/jobs/missing",
                headers={"X-App-Token": token, "Origin": "http://evil.test"},
            )
            self.assertEqual(status, 403)

            status, _, _ = server.request(
                "GET",
                "/api/jobs/missing",
                headers={
                    "X-App-Token": token,
                    "Origin": f"http://127.0.0.1:{server.port}",
                },
            )
            self.assertEqual(status, 404)

            status, _, _ = server.request(
                "GET",
                "/healthz",
                headers={"Host": "evil.test"},
            )
            self.assertEqual(status, 400)

    def test_filename_validation_blocks_unsafe_names(self) -> None:
        self.assertEqual(_validated_filename("proposal.pdf"), "proposal.pdf")
        for value in (None, "", "proposal.txt", "../proposal.pdf", "CON.pdf", "bad?.pdf"):
            with self.subTest(value=value), self.assertRaises(AppError):
                _validated_filename(value)


    def test_authenticated_heartbeat_marks_browser_activity(self) -> None:
        token = "h" * 40
        heartbeats: list[str] = []
        app = create_app(
            api_token=token,
            on_browser_activity=lambda: heartbeats.append("seen"),
        )
        with LiveServer(app) as server:
            status, _, body = server.request(
                "POST",
                "/api/session/heartbeat",
                headers={
                    "X-App-Token": token,
                    "Origin": f"http://127.0.0.1:{server.port}",
                },
            )
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        self.assertEqual(heartbeats, ["seen"])


class BrowserSessionMonitorTests(unittest.TestCase):
    def test_startup_grace_and_idle_expiry(self) -> None:
        now = [0.0]
        monitor = BrowserSessionMonitor(
            idle_seconds=10,
            startup_grace_seconds=20,
            clock=lambda: now[0],
        )
        now[0] = 19.9
        self.assertFalse(monitor.expired())
        now[0] = 20
        self.assertTrue(monitor.expired())
        monitor.mark_activity()
        now[0] = 29.9
        self.assertFalse(monitor.expired())
        now[0] = 30
        self.assertTrue(monitor.expired())


class NativeControllerTests(unittest.TestCase):
    def test_shutdown_deadline_forces_window_exit_if_server_thread_hangs(self) -> None:
        class HungThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        class FakeRoot:
            def __init__(self) -> None:
                self.destroyed = False
                self.after_calls = 0

            def winfo_exists(self) -> bool:
                return True

            def destroy(self) -> None:
                self.destroyed = True

            def after(self, *_args: object) -> None:
                self.after_calls += 1

        controller = NativeController.__new__(NativeController)
        controller.closing = True
        controller.shutdown_deadline = time.monotonic() - 1
        controller.server_thread = HungThread()
        controller.root = FakeRoot()

        controller._poll_server()  # noqa: SLF001 - intentional lifecycle boundary test

        self.assertTrue(controller.root.destroyed)
        self.assertEqual(controller.root.after_calls, 0)


class JobManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="interactive-pdf-job-test-")
        self.previous_app_data = os.environ.get("MAKE_INTERACTIVE_PDFS_APP_DATA")
        os.environ["MAKE_INTERACTIVE_PDFS_APP_DATA"] = self.temp.name
        self.manager = JobManager()

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()
        if self.previous_app_data is None:
            os.environ.pop("MAKE_INTERACTIVE_PDFS_APP_DATA", None)
        else:
            os.environ["MAKE_INTERACTIVE_PDFS_APP_DATA"] = self.previous_app_data
        self.temp.cleanup()

    async def test_failed_job_immediately_removes_sensitive_files(self) -> None:
        job = await self.manager.reserve_upload("private.pdf")
        job.source_path.write_bytes(b"%PDF-1.4\nprivate test data")
        job.output_path.write_bytes(b"invalid output")
        job.report_path.write_text("{}", encoding="utf-8")

        await self.manager._finish_failed(  # noqa: SLF001 - intentional boundary test
            job,
            ErrorPayload(code=ErrorCode.PROCESSING_FAILED, message="Test failure"),
        )

        view = await self.manager.get(job.id)
        self.assertEqual(view.status, JobStatus.FAIL)
        self.assertFalse(job.directory.exists())
        self.assertFalse(view.pdf_available)
        self.assertFalse(view.report_available)

    async def test_unreadable_progress_file_does_not_fail_job_monitoring(self) -> None:
        job = await self.manager.reserve_upload("progress.pdf")
        progress_path = job.directory / "make-progress.jsonl"
        progress_path.write_text('{"phase":"READING_PAGES"}\n', encoding="utf-8")

        with patch.object(Path, "open", side_effect=OSError("temporarily locked")):
            offset = await self.manager._consume_progress_file(  # noqa: SLF001
                job,
                progress_path,
                41,
            )

        self.assertEqual(offset, 41)
    async def test_engine_timeout_kills_phase_and_clears_process_reference(self) -> None:
        job = await self.manager.reserve_upload("slow.pdf")

        class SlowProcess:
            pid = 987654
            returncode: int | None = None

            async def wait(self) -> int:
                await asyncio.sleep(60)
                return 0

        process = SlowProcess()
        terminated = False

        async def fake_create_subprocess_exec(*_: object, **__: object) -> SlowProcess:
            return process

        async def fake_terminate(_: object) -> None:
            nonlocal terminated
            terminated = True
            process.returncode = -1

        self.manager._terminate_process_tree = fake_terminate  # type: ignore[method-assign]  # noqa: SLF001
        with (
            patch.object(jobs, "ENGINE_TIMEOUT_SECONDS", 0.01),
            patch.object(jobs.asyncio, "create_subprocess_exec", fake_create_subprocess_exec),
        ):
            with self.assertRaises(EngineTimeoutError):
                await self.manager._run_engine(job, "make", ("make", "input.pdf"))  # noqa: SLF001

        self.assertTrue(terminated)
        self.assertIsNone(job.process)

    async def test_downloads_are_gated_by_terminal_status(self) -> None:
        job = await self.manager.reserve_upload("document.pdf")
        job.output_path.write_bytes(b"%PDF-1.4\n")
        job.report_path.write_text('{"status":"PASS"}', encoding="utf-8")

        with self.assertRaises(AppError):
            await self.manager.output_download(job.id)
        with self.assertRaises(AppError):
            await self.manager.report_download(job.id)

        await self.manager._update(  # noqa: SLF001 - intentional state-machine test
            job,
            status=JobStatus.PASS,
            stage="COMPLETE",
            progress=100,
            message="Ready",
        )
        output, _ = await self.manager.output_download(job.id)
        report, _ = await self.manager.report_download(job.id)
        self.assertEqual(output, job.output_path)
        self.assertEqual(report, job.report_path)


class ProgressTelemetryTests(unittest.TestCase):
    def test_emit_progress_ignores_unwritable_telemetry_file(self) -> None:
        with (
            patch.dict(
                os.environ,
                {progress_events.PROGRESS_ENVIRONMENT_VARIABLE: "unwritable-progress.jsonl"},
            ),
            patch.object(Path, "open", side_effect=OSError("access denied")),
        ):
            progress_events.emit_progress("READING_PAGES", completed=1, total=2)

    def test_emit_progress_ignores_broken_stderr(self) -> None:
        with (
            patch.dict(
                os.environ,
                {progress_events.PROGRESS_ENVIRONMENT_VARIABLE: ""},
            ),
            patch("builtins.print", side_effect=BrokenPipeError("detached console")),
        ):
            progress_events.emit_progress("PREFLIGHT", completed=0, total=2)


class FilesystemSafetyTests(unittest.TestCase):
    def test_safe_rmtree_rejects_paths_outside_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="interactive-pdf-safe-root-") as root:
            parent = Path(root) / "parent"
            outside = Path(root) / "outside"
            parent.mkdir()
            outside.mkdir()
            with self.assertRaises(RuntimeError):
                _safe_rmtree(outside, parent=parent)
            self.assertTrue(outside.is_dir())

    def test_packaged_worker_rejects_extra_arguments_and_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp) / "owned"
            job = root / "job"
            outside = Path(raw_temp) / "outside"
            job.mkdir(parents=True)
            outside.mkdir()
            with patch.dict(
                os.environ,
                {"MAKE_INTERACTIVE_PDFS_JOB_ROOT": str(root)},
                clear=False,
            ):
                self.assertEqual(_validated_job_directory(str(job)), job.resolve())
                with self.assertRaises(RuntimeError):
                    _validated_job_directory(str(outside))
                with patch.object(sys, "frozen", True, create=True):
                    with self.assertRaises(RuntimeError):
                        run_internal_worker(("make", str(job), "--force"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
