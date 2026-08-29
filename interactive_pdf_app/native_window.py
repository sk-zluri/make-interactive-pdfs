"""Visible Windows controller that owns the local browser app lifetime."""

from __future__ import annotations

import os
import threading
import time
import tkinter as tk
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import uvicorn

from . import __version__
from .browser_launcher import BrowserLaunch


WINDOW_TITLE = "Make Interactive PDFs"
LINKEDIN_URL = "https://www.linkedin.com/in/techhfreakk"
SHUTDOWN_WINDOW_SECONDS = 20.0
READY_MESSAGE = (
    "Your PDF workspace opens in your default browser. Keep this window open "
    "while you work—your files are processed privately on this computer."
)
LAUNCH_FAILED_STATUS = "Open your browser manually to continue"

ThemeName = Literal["light", "dark"]
StatusKind = Literal["good", "warning", "error"]

THEMES: dict[ThemeName, dict[str, str]] = {
    "light": {
        "background": "#f7faf9",
        "text": "#12201b",
        "muted": "#58645f",
        "border": "#d5ddda",
        "accent": "#176b52",
        "accent_hover": "#105640",
        "accent_text": "#f5fbf8",
        "secondary": "#e8eeec",
        "secondary_hover": "#dce5e2",
        "secondary_text": "#34423d",
        "disabled_text": "#8f9b96",
        "link": "#176b52",
        "focus": "#2b8067",
        "good": "#176b52",
        "warning": "#8b6b18",
        "error": "#b42318",
    },
    "dark": {
        "background": "#111714",
        "text": "#f1f7f4",
        "muted": "#aebbb5",
        "border": "#35413c",
        "accent": "#2e8e6d",
        "accent_hover": "#3ba17d",
        "accent_text": "#f5fbf8",
        "secondary": "#27312d",
        "secondary_hover": "#35423d",
        "secondary_text": "#e3ece8",
        "disabled_text": "#77827d",
        "link": "#85e0c0",
        "focus": "#85e0c0",
        "good": "#63d0aa",
        "warning": "#e4bb65",
        "error": "#ff8b7e",
    },
}


def _configure_windows_identity() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "SashankKondepudi.MakeInteractivePDFs"
        )
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _system_theme() -> ThemeName:
    """Return the Windows app theme, with a predictable cross-platform fallback."""

    if os.name != "nt":
        return "light"
    try:
        import winreg

        registry_path = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(value) else "dark"
    except (ImportError, OSError, TypeError, ValueError):
        return "light"


class _Tooltip:
    """Small keyboard- and pointer-triggered hint for an icon-only control."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def set_text(self, text: str) -> None:
        self.text = text
        self._hide()

    def _schedule(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._cancel_schedule()
        self.after_id = self.widget.after(450, self._show)

    def _cancel_schedule(self) -> None:
        if self.after_id is None:
            return
        try:
            self.widget.after_cancel(self.after_id)
        except tk.TclError:
            pass
        self.after_id = None

    def _show(self) -> None:
        self.after_id = None
        if self.window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + self.widget.winfo_width() - 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
            window = tk.Toplevel(self.widget)
            window.wm_overrideredirect(True)
            window.wm_geometry(f"+{x}+{y}")
            colors = THEMES["dark"]
            tk.Label(
                window,
                text=self.text,
                background=colors["secondary"],
                foreground=colors["text"],
                font=("Segoe UI", 9),
                borderwidth=1,
                relief="solid",
                padx=8,
                pady=5,
            ).pack()
            self.window = window
        except tk.TclError:
            self.window = None

    def _hide(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self._cancel_schedule()
        if self.window is None:
            return
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None


class NativeController:
    """Small, honest controller window for the local-only processor."""

    def __init__(
        self,
        *,
        server: uvicorn.Server,
        server_thread: threading.Thread,
        open_workspace: Callable[[], BrowserLaunch],
        has_active_job: Callable[[], bool],
        icon_png: Path | None = None,
        icon_ico: Path | None = None,
    ) -> None:
        _configure_windows_identity()
        self.server = server
        self.server_thread = server_thread
        self.open_workspace = open_workspace
        self.has_active_job = has_active_job
        self.closing = False
        self.shutdown_deadline: float | None = None
        self.opened_automatically = False
        self.theme: ThemeName = _system_theme()
        self.status_kind: StatusKind = "good"
        self.launch_failed = False
        # Launches run off the UI thread; the existing poll loop applies the
        # result so no Tk widget is ever touched from a worker thread.
        self._launch_guard = threading.Lock()
        self._launch_result: BrowserLaunch | None = None

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.geometry("520x400")
        self.root.minsize(480, 380)
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)

        self._icon_image: tk.PhotoImage | None = None
        self._header_icon: tk.PhotoImage | None = None
        if icon_ico is not None and icon_ico.is_file() and os.name == "nt":
            try:
                self.root.iconbitmap(default=str(icon_ico))
            except tk.TclError:
                pass
        if icon_png is not None and icon_png.is_file():
            try:
                self._icon_image = tk.PhotoImage(file=str(icon_png))
                self.root.iconphoto(True, self._icon_image)
                largest_dimension = max(
                    self._icon_image.width(), self._icon_image.height()
                )
                scale = max(1, (largest_dimension + 55) // 56)
                self._header_icon = self._icon_image.subsample(scale, scale)
            except tk.TclError:
                self._icon_image = None
                self._header_icon = None

        self.status_text = tk.StringVar(value="Starting local processor…")
        self.message_text = tk.StringVar(value=READY_MESSAGE)

        self.body = tk.Frame(self.root)
        self.body.pack(fill="both", expand=True, padx=36, pady=(28, 24))

        self.title_row = tk.Frame(self.body)
        self.title_row.pack(fill="x")
        if self._header_icon is not None:
            self.header_icon_label = tk.Label(
                self.title_row,
                image=self._header_icon,
                borderwidth=0,
            )
            self.header_icon_label.pack(side="left", padx=(0, 14))
        else:
            self.header_icon_label = None

        self.title_label = tk.Label(
            self.title_row,
            text=WINDOW_TITLE,
            font=("Segoe UI Semibold", 19),
            anchor="w",
        )
        self.title_label.pack(side="left", fill="x", expand=True)

        self.theme_button = tk.Button(
            self.title_row,
            command=self._toggle_theme,
            font=("Segoe UI Symbol", 15),
            relief="flat",
            borderwidth=0,
            width=3,
            padx=2,
            pady=1,
            cursor="hand2",
            takefocus=True,
        )
        self.theme_button.pack(side="right", padx=(12, 0))
        self.theme_button.bind(
            "<Return>", lambda _event: self._toggle_theme(), add="+"
        )
        self.theme_tooltip = _Tooltip(self.theme_button, "")

        self.status_row = tk.Frame(self.body)
        self.status_row.pack(fill="x", pady=(24, 0))
        self.status_dot = tk.Label(
            self.status_row,
            text="•",
            font=("Segoe UI", 13),
        )
        self.status_dot.pack(side="left", padx=(0, 9))
        self.status_label = tk.Label(
            self.status_row,
            textvariable=self.status_text,
            font=("Segoe UI Semibold", 11),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.message_label = tk.Label(
            self.body,
            textvariable=self.message_text,
            font=("Segoe UI", 10),
            justify="left",
            anchor="nw",
            wraplength=440,
        )
        self.message_label.pack(fill="x", pady=(14, 0))

        self.separator = tk.Frame(self.body, height=1)
        self.separator.pack(fill="x", pady=(28, 20))

        self.buttons = tk.Frame(self.body)
        self.buttons.pack(fill="x")
        self.open_button = tk.Button(
            self.buttons,
            text="Open workspace",
            command=self._open_in_background,
            state="disabled",
            font=("Segoe UI Semibold", 10),
            relief="flat",
            borderwidth=0,
            padx=20,
            pady=10,
            cursor="hand2",
            takefocus=True,
        )
        self.open_button.pack(side="left")
        self.stop_button = tk.Button(
            self.buttons,
            text="Stop local app",
            command=self.request_close,
            font=("Segoe UI Semibold", 10),
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=10,
            cursor="hand2",
            takefocus=True,
        )
        self.stop_button.pack(side="right")

        self.footer = tk.Frame(self.body)
        self.footer.pack(side="bottom", fill="x", pady=(24, 0))
        self.linkedin_button = tk.Button(
            self.footer,
            text="Made with ❤️ by Sashank Kondepudi",
            command=self._open_linkedin,
            # The emoji-aware font treats U+2764 + variation selector as one
            # glyph, avoiding the large phantom gap produced by Segoe UI/Tk.
            font=("Segoe UI Emoji", 8, "underline"),
            relief="flat",
            borderwidth=0,
            padx=0,
            pady=0,
            cursor="hand2",
            takefocus=True,
        )
        self.linkedin_button.pack(side="left")
        self.version_label = tk.Label(
            self.footer,
            text=f"v{__version__}",
            font=("Segoe UI", 8),
            anchor="e",
        )
        self.version_label.pack(side="right")

        self._apply_theme()
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        # Some stripped-down Windows/Tk installations do not expose the
        # topmost window attribute. The controller should still open normally.
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(900, self._release_topmost)
        except tk.TclError:
            pass
        self.root.after(100, self._poll_server)

    def _apply_theme(self) -> None:
        colors = THEMES[self.theme]
        self.root.configure(background=colors["background"])
        for frame in (
            self.body,
            self.title_row,
            self.status_row,
            self.buttons,
            self.footer,
        ):
            frame.configure(background=colors["background"])

        if self.header_icon_label is not None:
            self.header_icon_label.configure(background=colors["background"])
        self.title_label.configure(
            background=colors["background"], foreground=colors["text"]
        )
        self.status_dot.configure(
            background=colors["background"],
            foreground=colors[self.status_kind],
        )
        self.status_label.configure(
            background=colors["background"], foreground=colors["text"]
        )
        self.message_label.configure(
            background=colors["background"], foreground=colors["muted"]
        )
        self.separator.configure(background=colors["border"])

        self.open_button.configure(
            foreground=colors["accent_text"],
            background=colors["accent"],
            activeforeground=colors["accent_text"],
            activebackground=colors["accent_hover"],
            disabledforeground=colors["disabled_text"],
            highlightbackground=colors["background"],
            highlightcolor=colors["focus"],
            highlightthickness=2,
        )
        self.stop_button.configure(
            foreground=colors["secondary_text"],
            background=colors["secondary"],
            activeforeground=colors["secondary_text"],
            activebackground=colors["secondary_hover"],
            disabledforeground=colors["disabled_text"],
            highlightbackground=colors["background"],
            highlightcolor=colors["focus"],
            highlightthickness=2,
        )
        self.linkedin_button.configure(
            foreground=colors["link"],
            background=colors["background"],
            activeforeground=colors["link"],
            activebackground=colors["background"],
            highlightbackground=colors["background"],
            highlightcolor=colors["focus"],
            highlightthickness=1,
        )
        self.version_label.configure(
            foreground=colors["muted"], background=colors["background"]
        )

        if self.theme == "light":
            self.theme_button.configure(
                text="☾",
                foreground=colors["text"],
                background=colors["secondary"],
                activeforeground=colors["text"],
                activebackground=colors["secondary_hover"],
                highlightbackground=colors["background"],
                highlightcolor=colors["focus"],
                highlightthickness=2,
            )
            self.theme_tooltip.set_text("Switch to dark mode")
        else:
            self.theme_button.configure(
                text="☀",
                foreground=colors["text"],
                background=colors["secondary"],
                activeforeground=colors["text"],
                activebackground=colors["secondary_hover"],
                highlightbackground=colors["background"],
                highlightcolor=colors["focus"],
                highlightthickness=2,
            )
            self.theme_tooltip.set_text("Switch to light mode")

    def _toggle_theme(self) -> None:
        self.theme = "dark" if self.theme == "light" else "light"
        self._apply_theme()

    def _set_status(self, kind: StatusKind, text: str) -> None:
        self.status_kind = kind
        self.status_text.set(text)
        try:
            self.status_dot.configure(foreground=THEMES[self.theme][kind])
        except tk.TclError:
            pass

    def _open_linkedin(self) -> None:
        threading.Thread(
            target=webbrowser.open_new_tab,
            args=(LINKEDIN_URL,),
            name="open-linkedin-profile",
            daemon=True,
        ).start()

    def _release_topmost(self) -> None:
        try:
            if self.root.winfo_exists():
                self.root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _open_in_background(self) -> None:
        if self.closing or not self.server_thread.is_alive():
            return
        threading.Thread(
            target=self._open_and_record,
            name="open-pdf-workspace",
            daemon=True,
        ).start()

    def _open_and_record(self) -> None:
        try:
            result = self.open_workspace()
        except Exception as error:  # noqa: BLE001 - a click must never be lost
            result = BrowserLaunch(opened=False, url="", detail=str(error))
        with self._launch_guard:
            self._launch_result = result

    def _consume_launch_result(self) -> None:
        with self._launch_guard:
            result = self._launch_result
            self._launch_result = None
        if result is None:
            return
        self.launch_failed = not result.opened
        self.message_text.set(result.help_text() if self.launch_failed else READY_MESSAGE)

    def _destroy_root(self) -> None:
        try:
            if self.root.winfo_exists():
                self.root.destroy()
        except tk.TclError:
            pass

    def _poll_server(self) -> None:
        if self.closing:
            timed_out = (
                self.shutdown_deadline is not None
                and time.monotonic() >= self.shutdown_deadline
            )
            if self.server_thread.is_alive() and not timed_out:
                self.root.after(100, self._poll_server)
            else:
                # Let __main__ own the remaining bounded server join and
                # listener cleanup; never leave an uncloseable Tk window.
                self._destroy_root()
            return

        if not self.server_thread.is_alive():
            self._set_status("error", "Local processor stopped unexpectedly")
            self.message_text.set(
                "The browser workspace is no longer connected. Close this window "
                "and open the app again."
            )
            self.open_button.configure(state="disabled")
            self.stop_button.configure(text="Close")
            return

        if self.server.started:
            self.open_button.configure(state="normal")
            if not self.opened_automatically:
                self.opened_automatically = True
                self._open_in_background()
            self._consume_launch_result()
            if self.launch_failed:
                self._set_status("warning", LAUNCH_FAILED_STATUS)
            elif self.has_active_job():
                self._set_status("good", "Processing PDF locally…")
            else:
                self._set_status("good", "Local processor is running")
        self.root.after(350, self._poll_server)

    def request_close(self) -> None:
        if self.closing:
            return
        if not self.server_thread.is_alive():
            self._destroy_root()
            return
        self.closing = True
        self.shutdown_deadline = time.monotonic() + SHUTDOWN_WINDOW_SECONDS
        self._set_status("warning", "Stopping local processor…")
        self.message_text.set(
            "Any active processing will stop safely. The browser tab will show "
            "that the local app is closed."
        )
        self.open_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.server.should_exit = True
        self.root.after(100, self._poll_server)

    def run(self) -> None:
        self.root.mainloop()


def run_native_controller(
    *,
    server: uvicorn.Server,
    server_thread: threading.Thread,
    open_workspace: Callable[[], BrowserLaunch],
    has_active_job: Callable[[], bool],
    icon_png: Path | None,
    icon_ico: Path | None,
) -> None:
    controller = NativeController(
        server=server,
        server_thread=server_thread,
        open_workspace=open_workspace,
        has_active_job=has_active_job,
        icon_png=icon_png,
        icon_ico=icon_ico,
    )
    controller.run()
