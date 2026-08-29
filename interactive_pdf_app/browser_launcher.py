"""Open the local workspace URL in whichever browser the user has made default.

No browser is named or searched for here. Windows is asked to resolve the
``http`` association itself, so whichever browser the user has chosen is the
one that opens. Every attempt reports success or failure so the controller can
tell the user what to do instead of failing silently.
"""

from __future__ import annotations

import os
import subprocess
import webbrowser
from collections.abc import Callable, Iterable
from typing import NamedTuple


LaunchStep = tuple[str, Callable[[str], bool]]

MANUAL_HELP = (
    "Your default browser did not open. Copy this address into any browser: "
    "{url} — or pick a default browser in Windows Settings › Apps › Default "
    "apps, then click Open workspace again."
)
NO_URL_HELP = (
    "Your default browser did not open. Pick a default browser in Windows "
    "Settings › Apps › Default apps, then click Open workspace again."
)


class BrowserLaunch(NamedTuple):
    """Outcome of handing the workspace URL to the user's browser."""

    opened: bool
    url: str
    method: str = ""
    detail: str = ""

    def help_text(self) -> str:
        """Actionable guidance to show when the browser could not be opened."""

        if self.opened:
            return ""
        if not self.url:
            return NO_URL_HELP
        return MANUAL_HELP.format(url=self.url)


def _windows_shell_default(url: str) -> bool:
    """Let the Windows shell open the URL with the registered default handler."""

    start_file = getattr(os, "startfile", None)
    if start_file is None:
        return False
    start_file(url)
    return True


def _python_webbrowser(url: str) -> bool:
    """Use the standard library resolution, which is also the POSIX fallback."""

    return bool(webbrowser.open(url, new=2, autoraise=True))


def _windows_url_protocol_handler(url: str) -> bool:
    """Last Windows resort: the shell's own URL protocol handler."""

    subprocess.Popen(
        ["rundll32.exe", "url.dll,FileProtocolHandler", url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def default_strategies(*, windows: bool | None = None) -> tuple[LaunchStep, ...]:
    """Ordered launch attempts for this platform, default browser first."""

    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return (("python-webbrowser", _python_webbrowser),)
    return (
        ("windows-default-browser", _windows_shell_default),
        ("python-webbrowser", _python_webbrowser),
        ("windows-url-handler", _windows_url_protocol_handler),
    )


def open_workspace_url(
    url: str,
    *,
    strategies: Iterable[LaunchStep] | None = None,
) -> BrowserLaunch:
    """Try each launch strategy in order and report what happened."""

    steps = tuple(default_strategies() if strategies is None else strategies)
    failures: list[str] = []
    for name, attempt in steps:
        try:
            if attempt(url):
                return BrowserLaunch(opened=True, url=url, method=name)
            failures.append(f"{name}: no browser responded")
        except (OSError, ValueError, webbrowser.Error) as error:
            failures.append(f"{name}: {error}")
    return BrowserLaunch(
        opened=False,
        url=url,
        detail="; ".join(failures) or "no launch method available",
    )
