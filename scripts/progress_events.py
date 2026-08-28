"""Small, deterministic progress events shared by the CLI and desktop app."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping


PROGRESS_ENVIRONMENT_VARIABLE = "MAKE_INTERACTIVE_PDFS_PROGRESS"
PROGRESS_PREFIX = "MIPDF_PROGRESS "
_sequence = 0


def emit_progress(
    phase: str,
    *,
    completed: int = 0,
    total: int = 0,
    unit: str = "",
    page: int | None = None,
    total_pages: int | None = None,
    message: str = "",
    counts: Mapping[str, int] | None = None,
) -> None:
    """Emit one truthful JSON event without affecting engine result output."""

    global _sequence
    _sequence += 1
    event: dict[str, object] = {
        "schema_version": 1,
        "sequence": _sequence,
        "phase": str(phase).strip().upper(),
        "completed": max(0, int(completed)),
        "total": max(0, int(total)),
        "unit": str(unit).strip().lower(),
        "message": str(message).strip(),
    }
    if page is not None:
        event["page"] = max(1, int(page))
    if total_pages is not None:
        event["total_pages"] = max(1, int(total_pages))
    if counts:
        event["counts"] = {
            str(key): max(0, int(value)) for key, value in counts.items()
        }

    encoded = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    progress_path = os.environ.get(PROGRESS_ENVIRONMENT_VARIABLE)
    if progress_path:
        # Progress is observability, never part of the PDF result contract.  A
        # deleted/locked telemetry file must not turn a successful conversion
        # into a failed one.
        try:
            path = Path(progress_path)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.write("\n")
        except (OSError, ValueError):
            return
    elif completed == 0 or (total > 0 and completed == total):
        try:
            print(f"{PROGRESS_PREFIX}{encoded}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            # A detached/broken stderr stream is telemetry failure too.
            pass
