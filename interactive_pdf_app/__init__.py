"""Local-browser application for the deterministic interactive-PDF engine."""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        # Frozen builds may inject their own package version without shipping VERSION.
        return "0.0.0"


__version__ = _read_version()

__all__ = ["__version__"]
