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

from interactive_pdf_app import browser_launcher, jobs, worker
from interactive_pdf_app.__main__ import BrowserSessionMonitor
from interactive_pdf_app.browser_launcher import BrowserLaunch
from interactive_pdf_app.jobs import EngineTimeoutError, JobManager, _safe_rmtree
from interactive_pdf_app.models import AppError, ErrorCode, ErrorPayload, JobStatus
from interactive_pdf_app.native_window import (
    LAUNCH_FAILED_STATUS,
    READY_MESSAGE,
    NativeController,
)
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


class BrowserLaunchTests(unittest.TestCase):
    """The launcher must prefer the user's default browser and stay honest."""

    URL = "http://127.0.0.1:8765/#token=abc"

    @staticmethod
    def _opens(_url: str) -> bool:
        return True

    @staticmethod
    def _refuses(_url: str) -> bool:
        return False

    @staticmethod
    def _raises(_url: str) -> bool:
        raise OSError("no application is associated with http")

    def test_windows_order_asks_the_default_browser_first(self) -> None:
        names = [name for name, _ in browser_launcher.default_strategies(windows=True)]
        self.assertEqual(names[0], "windows-default-browser")
        self.assertGreater(len(names), 1)
        self.assertEqual(len(set(names)), len(names))

    def test_non_windows_keeps_the_standard_library_fallback(self) -> None:
        self.assertEqual(
            [name for name, _ in browser_launcher.default_strategies(windows=False)],
            ["python-webbrowser"],
        )

    def test_first_working_strategy_wins_and_later_ones_never_run(self) -> None:
        calls: list[str] = []

        def first(url: str) -> bool:
            calls.append("first")
            return True

        def second(url: str) -> bool:
            calls.append("second")
            return True

        result = browser_launcher.open_workspace_url(
            self.URL,
            strategies=(("first", first), ("second", second)),
        )

        self.assertTrue(result.opened)
        self.assertEqual(result.method, "first")
        self.assertEqual(calls, ["first"])
        self.assertEqual(result.help_text(), "")

    def test_silent_refusal_and_os_error_fall_through_to_the_next_strategy(self) -> None:
        result = browser_launcher.open_workspace_url(
            self.URL,
            strategies=(
                ("silent", self._refuses),
                ("raising", self._raises),
                ("works", self._opens),
            ),
        )

        self.assertTrue(result.opened)
        self.assertEqual(result.method, "works")

    def test_total_failure_reports_the_cause_and_actionable_help(self) -> None:
        result = browser_launcher.open_workspace_url(
            self.URL,
            strategies=(("silent", self._refuses), ("raising", self._raises)),
        )

        self.assertFalse(result.opened)
        self.assertEqual(result.method, "")
        self.assertIn("silent", result.detail)
        self.assertIn("no application is associated with http", result.detail)
        self.assertIn(self.URL, result.help_text())
        self.assertIn("Default apps", result.help_text())

    def test_missing_url_still_produces_actionable_help(self) -> None:
        help_text = BrowserLaunch(opened=False, url="").help_text()
        self.assertIn("Default apps", help_text)

    def test_windows_strategy_defers_to_the_shell_association(self) -> None:
        opened: list[str] = []
        with patch.object(
            browser_launcher.os,
            "startfile",
            opened.append,
            create=True,
        ):
            self.assertTrue(browser_launcher._windows_shell_default(self.URL))  # noqa: SLF001
        self.assertEqual(opened, [self.URL])

    def test_standard_library_strategy_opens_a_new_tab(self) -> None:
        with patch.object(
            browser_launcher.webbrowser, "open", return_value=True
        ) as opener:
            self.assertTrue(browser_launcher._python_webbrowser(self.URL))  # noqa: SLF001
        opener.assert_called_once_with(self.URL, new=2, autoraise=True)

    def test_no_browser_is_hard_coded(self) -> None:
        source = Path(browser_launcher.__file__).read_text(encoding="utf-8").casefold()
        for browser_name in ("chrome", "msedge", "firefox", "iexplore", "safari"):
            self.assertNotIn(browser_name, source)


class _FakeVariable:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class _FakeWidget:
    def __init__(self) -> None:
        self.options: dict[str, object] = {}

    def configure(self, **options: object) -> None:
        self.options.update(options)


class _AliveThread:
    @staticmethod
    def is_alive() -> bool:
        return True


class _StartedServer:
    started = True


class ControllerLaunchFeedbackTests(unittest.TestCase):
    """A failed browser launch must surface in the controller, not vanish."""

    def _controller(self, open_workspace) -> NativeController:  # type: ignore[no-untyped-def]
        controller = NativeController.__new__(NativeController)
        controller.closing = False
        controller.launch_failed = False
        controller._launch_guard = threading.Lock()  # noqa: SLF001
        controller._launch_result = None  # noqa: SLF001
        controller.open_workspace = open_workspace
        controller.has_active_job = lambda: False
        controller.opened_automatically = True
        controller.server = _StartedServer()
        controller.server_thread = _AliveThread()
        controller.theme = "light"
        controller.status_kind = "good"
        controller.status_text = _FakeVariable()
        controller.message_text = _FakeVariable(READY_MESSAGE)
        controller.status_dot = _FakeWidget()
        controller.open_button = _FakeWidget()
        controller.root = _FakeWidget()
        controller.root.after = lambda *_args: None  # type: ignore[attr-defined]
        return controller

    def test_successful_launch_keeps_the_normal_message(self) -> None:
        launch = BrowserLaunch(opened=True, url="http://127.0.0.1:1/", method="test")
        controller = self._controller(lambda: launch)

        controller._open_and_record()  # noqa: SLF001 - thread body run inline
        controller._poll_server()  # noqa: SLF001

        self.assertFalse(controller.launch_failed)
        self.assertEqual(controller.message_text.get(), READY_MESSAGE)
        self.assertEqual(controller.status_text.get(), "Local processor is running")

    def test_failed_launch_shows_the_address_and_a_warning_status(self) -> None:
        url = "http://127.0.0.1:8765/#token=abc"
        launch = BrowserLaunch(opened=False, url=url, detail="nothing responded")
        controller = self._controller(lambda: launch)

        controller._open_and_record()  # noqa: SLF001 - thread body run inline
        controller._poll_server()  # noqa: SLF001

        self.assertTrue(controller.launch_failed)
        self.assertIn(url, controller.message_text.get())
        self.assertEqual(controller.status_text.get(), LAUNCH_FAILED_STATUS)
        self.assertEqual(controller.open_button.options.get("state"), "normal")

    def test_unexpected_launcher_error_is_reported_instead_of_lost(self) -> None:
        def explode() -> BrowserLaunch:
            raise RuntimeError("shell unavailable")

        controller = self._controller(explode)

        controller._open_and_record()  # noqa: SLF001 - thread body run inline
        controller._poll_server()  # noqa: SLF001

        self.assertTrue(controller.launch_failed)
        self.assertIn("Default apps", controller.message_text.get())

    def test_a_later_successful_launch_clears_the_failure_message(self) -> None:
        url = "http://127.0.0.1:8765/#token=abc"
        results = [
            BrowserLaunch(opened=False, url=url, detail="nothing responded"),
            BrowserLaunch(opened=True, url=url, method="test"),
        ]
        controller = self._controller(lambda: results.pop(0))

        controller._open_and_record()  # noqa: SLF001
        controller._poll_server()  # noqa: SLF001
        self.assertTrue(controller.launch_failed)

        controller._open_and_record()  # noqa: SLF001
        controller._poll_server()  # noqa: SLF001

        self.assertFalse(controller.launch_failed)
        self.assertEqual(controller.message_text.get(), READY_MESSAGE)


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

    async def test_verified_links_replay_is_rejected_while_shutting_down(self) -> None:
        self.manager._closing = True  # noqa: SLF001 - lifecycle boundary test

        with self.assertRaises(AppError) as raised:
            await self.manager.publish_verified_links("missing-job")

        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(raised.exception.payload.code, ErrorCode.ENGINE_UNAVAILABLE)

    async def test_verified_links_replay_keeps_source_only_for_eligible_review(self) -> None:
        job = await self.manager.reserve_upload("review.pdf")
        job.source_path.write_bytes(b"%PDF-1.4\nprivate test data")
        job.report_path.write_text('{"status":"NEEDS_REVIEW"}', encoding="utf-8")
        job.review_source_path.write_text('{"verified_links_offer":{"eligible":true}}', encoding="utf-8")
        job.status = JobStatus.NEEDS_REVIEW
        job.verified_links_available = True
        job.verified_links_safe_links = 3
        job.review_source_sha256 = jobs._sha256_file(job.review_source_path)  # noqa: SLF001
        self.manager._active_job_id = None  # noqa: SLF001 - completed-review state fixture

        async def no_processing(_: object) -> None:
            return None

        self.manager._process_job = no_processing  # type: ignore[method-assign]  # noqa: SLF001
        view = await self.manager.publish_verified_links(job.id)
        self.assertEqual(view.status, JobStatus.QUEUED)
        self.assertTrue(job.replaying_verified_links)
        self.assertIsNotNone(job.task)
        await job.task

        await self.manager._remove_intermediates(  # noqa: SLF001 - lifecycle boundary test
            job,
            keep_source=True,
            keep_output=False,
            keep_report=True,
            keep_review_source=True,
        )
        self.assertTrue(job.source_path.is_file())
        self.assertTrue(job.report_path.is_file())
        self.assertTrue(job.review_source_path.is_file())
        self.assertFalse(job.output_path.exists())

        job.status = JobStatus.NEEDS_REVIEW
        self.manager._active_job_id = None  # noqa: SLF001 - completed-review state fixture
        await self.manager.cleanup(job.id)
        self.assertFalse(job.directory.exists())

    async def test_verified_links_replay_rejects_tampered_review_source(self) -> None:
        job = await self.manager.reserve_upload("review-tampered.pdf")
        job.source_path.write_bytes(b"%PDF-1.4\nprivate test data")
        job.review_source_path.write_text(
            '{"verified_links_offer":{"eligible":true}}', encoding="utf-8"
        )
        job.status = JobStatus.NEEDS_REVIEW
        job.verified_links_available = True
        job.verified_links_safe_links = 1
        job.review_source_sha256 = jobs._sha256_file(job.review_source_path)  # noqa: SLF001
        self.manager._active_job_id = None  # noqa: SLF001 - completed-review state fixture

        job.review_source_path.write_text(
            '{"verified_links_offer":{"eligible":false}}', encoding="utf-8"
        )

        with self.assertRaises(AppError) as raised:
            await self.manager.publish_verified_links(job.id)

        self.assertEqual(
            raised.exception.payload.code,
            ErrorCode.VERIFIED_LINKS_NOT_AVAILABLE,
        )
        self.assertFalse(job.verified_links_available)
        self.assertIsNone(job.review_source_sha256)
        self.assertFalse(job.replaying_verified_links)

    async def test_verified_links_replay_rechecks_manifest_before_running(self) -> None:
        job = await self.manager.reserve_upload("review-worker-tampered.pdf")
        job.review_source_path.write_text(
            '{"verified_links_offer":{"eligible":true}}',
            encoding="utf-8",
        )
        job.review_source_sha256 = jobs._sha256_file(job.review_source_path)  # noqa: SLF001
        job.replaying_verified_links = True
        job.review_source_path.write_text(
            '{"verified_links_offer":{"eligible":false}}',
            encoding="utf-8",
        )

        engine_called = False

        async def engine_must_not_run(*_: object) -> tuple[int, str]:
            nonlocal engine_called
            engine_called = True
            return 1, "unexpected"

        self.manager._run_engine = engine_must_not_run  # type: ignore[method-assign]  # noqa: SLF001
        await self.manager._process_job(job)  # noqa: SLF001 - worker boundary test

        view = await self.manager.get(job.id)
        self.assertFalse(engine_called)
        self.assertEqual(view.status, JobStatus.FAIL)
        self.assertEqual(view.error.code, ErrorCode.VERIFIED_LINKS_NOT_AVAILABLE)
        self.assertFalse(job.directory.exists())


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

    def test_packaged_worker_uses_internal_review_source_for_verified_replay(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp) / "owned"
            job = root / "job"
            job.mkdir(parents=True)
            observed: dict[str, list[str]] = {}

            class FakeEngine:
                @staticmethod
                def main() -> int:
                    observed["argv"] = list(sys.argv)
                    return 0

            with (
                patch.dict(
                    os.environ,
                    {"MAKE_INTERACTIVE_PDFS_JOB_ROOT": str(root)},
                    clear=False,
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(worker.importlib, "import_module", return_value=FakeEngine()),
            ):
                self.assertEqual(run_internal_worker(("make-verified", str(job))), 0)

            arguments = observed["argv"]
            self.assertIn("--publish-confirmed-links", arguments)
            self.assertIn(str(job / "review-source.json"), arguments)
            self.assertIn("--force", arguments)


if __name__ == "__main__":
    unittest.main(verbosity=2)
