"""Small formatting helpers shared by templates and the JSON layer."""

from __future__ import annotations

from datetime import datetime, timezone

from .db import from_iso


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while n >= 1000 and i < len(units) - 1:
        n /= 1000.0
        i += 1
    if i == 0:
        return f"{int(n)} B"
    return f"{n:.1f} {units[i]}"


GIB = 1024 ** 3


def human_gib(n: int | float | None) -> str:
    """Binary gibibytes — the unit disk capacity is actually discussed in
    ("105 GiB free"), as opposed to :func:`human_bytes`' decimal GB."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    if n < 0:
        return "—"
    return f"{n / GIB:.1f} GiB"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = abs(int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s, 3600)
        return f"{h}h" if m < 60 else f"{h}h {m // 60}m"
    d, r = divmod(s, 86400)
    h = r // 3600
    return f"{d}d" if h == 0 else f"{d}d {h}h"


def relative(iso: str | None, now: float) -> str:
    """'3m ago' / 'in 2h' / 'never'."""
    epoch = from_iso(iso)
    if epoch is None:
        return "never"
    delta = now - epoch
    if abs(delta) < 5:
        return "just now"
    if delta >= 0:
        return f"{human_duration(delta)} ago"
    return f"in {human_duration(-delta)}"


def absolute(iso: str | None) -> str:
    """UTC wall-clock text, or "" for anything unparseable/out of range.
    Never raises: this runs inside templates over untrusted metric values."""
    epoch = from_iso(iso)
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        return ""
