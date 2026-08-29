"""Launch the private local processor and its browser workspace."""

from __future__ import annotations

import argparse
import http.client
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .browser_launcher import open_workspace_url
from .server import create_app, new_api_token


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_BROWSER_IDLE_SECONDS = 90.0
STARTUP_GRACE_SECONDS = 120.0


class BrowserSessionMonitor:
    """Track authenticated browser heartbeats for windowless app shutdown."""

    def __init__(
        self,
        *,
        idle_seconds: float = DEFAULT_BROWSER_IDLE_SECONDS,
        startup_grace_seconds: float = STARTUP_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_seconds = idle_seconds
        self.startup_grace_seconds = startup_grace_seconds
        self._clock = clock
        self._started_at = clock()
        self._last_activity: float | None = None
        self._guard = threading.Lock()

    def mark_activity(self) -> None:
        with self._guard:
            self._last_activity = self._clock()

    def expired(self) -> bool:
        now = self._clock()
        with self._guard:
            last_activity = self._last_activity
        if last_activity is None:
            return now - self._started_at >= self.startup_grace_seconds
        return now - last_activity >= self.idle_seconds


def _stop_when_browser_inactive(
    server: uvicorn.Server,
    monitor: BrowserSessionMonitor,
    manager,
) -> None:  # type: ignore[no-untyped-def]
    while not server.should_exit:
        if monitor.expired() and not manager.has_active_job:
            server.should_exit = True
            return
        time.sleep(1.0)


def _open_when_ready(port: int, url: str) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=0.5)
        try:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                open_workspace_url(url)
                return
        except OSError:
            time.sleep(0.1)
        finally:
            connection.close()
    open_workspace_url(url)


def _bound_listener(port: int) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        listener.bind((LOOPBACK_HOST, port))
        listener.listen(socket.SOMAXCONN)
        listener.set_inheritable(True)
        return listener, int(listener.getsockname()[1])
    except Exception:
        listener.close()
        raise


def _application_root() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).resolve()
    return Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Local port for development; 0 chooses an available port (default)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Run without the browser or Windows controller (development only)",
    )
    parser.add_argument(
        "--browser-idle-seconds",
        type=float,
        default=DEFAULT_BROWSER_IDLE_SECONDS,
        help=argparse.SUPPRESS,
    )
    return parser


def _create_server(app, port: int) -> uvicorn.Server:  # type: ignore[no-untyped-def]
    config = uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=port,
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
        log_level="info",
        access_log=False,
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=5.0,
    )
    return uvicorn.Server(config)


def _run_with_native_controller(
    server: uvicorn.Server,
    listener: socket.socket,
    url: str,
    manager,
) -> int:  # type: ignore[no-untyped-def]
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run(sockets=[listener])
        except BaseException as exc:
            errors.append(exc)

    # The normal path always joins this thread. Daemon mode is the final
    # safety net if a third-party server dependency ignores both shutdown
    # signals, so closing the visible controller cannot strand a background
    # process indefinitely.
    server_thread = threading.Thread(target=serve, name="local-pdf-server", daemon=True)
    server_thread.start()

    root = _application_root()
    from .native_window import run_native_controller

    try:
        run_native_controller(
            server=server,
            server_thread=server_thread,
            open_workspace=lambda: open_workspace_url(url),
            has_active_job=lambda: bool(manager.has_active_job),
            icon_png=root / "Icon" / "Make Interactive PDFs.png",
            icon_ico=root / "Icon" / "Make Interactive PDFs.ico",
        )
    finally:
        server.should_exit = True
        server_thread.join(timeout=30)
        if server_thread.is_alive():
            server.force_exit = True
        listener.close()
        if server_thread.is_alive():
            server_thread.join(timeout=5)
    if server_thread.is_alive():
        raise RuntimeError("The local processor did not stop cleanly")
    if errors:
        raise RuntimeError("The local processor stopped unexpectedly") from errors[0]
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-worker":
        from .worker import run_internal_worker

        return run_internal_worker(sys.argv[2:])

    arguments = build_parser().parse_args()
    if not 0 <= arguments.port <= 65535:
        print("ERROR: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    if arguments.browser_idle_seconds < 1:
        print("ERROR: --browser-idle-seconds must be at least 1", file=sys.stderr)
        return 2

    listener, port = _bound_listener(arguments.port)
    token = new_api_token()
    browser_monitor = BrowserSessionMonitor(idle_seconds=arguments.browser_idle_seconds)
    app = create_app(api_token=token, on_browser_activity=browser_monitor.mark_activity)
    # The secret stays after '#': browsers never include URL fragments in HTTP requests.
    url = f"http://{LOOPBACK_HOST}:{port}/#token={token}"
    server = _create_server(app, port)

    if arguments.no_browser:
        print(url)
        try:
            server.run(sockets=[listener])
        finally:
            listener.close()
        return 0

    if os.name == "nt":
        return _run_with_native_controller(server, listener, url, app.state.job_manager)

    browser_thread = threading.Thread(
        target=_open_when_ready,
        args=(port, url),
        name="open-local-app",
        daemon=True,
    )
    browser_thread.start()
    idle_thread = threading.Thread(
        target=_stop_when_browser_inactive,
        args=(server, browser_monitor, app.state.job_manager),
        name="stop-inactive-local-app",
        daemon=True,
    )
    idle_thread.start()
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
