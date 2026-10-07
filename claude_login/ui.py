"""Terminal output helpers: colours, tables, prompts, relative timestamps."""

from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timezone
from typing import Iterable, Sequence

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "grey": "90",
}


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def paint(text: str, *styles: str, stream=None) -> str:
    if not styles or not color_enabled(stream):
        return text
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    if not codes:
        return text
    return f"\x1b[{codes}m{text}\x1b[0m"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def width(text: str) -> int:
    return len(strip_ansi(text))


def pad(text: str, target: int, align: str = "<") -> str:
    """Pad to `target` columns, ignoring ANSI codes when measuring."""
    missing = max(0, target - width(text))
    if align == ">":
        return " " * missing + text
    if align == "^":
        left = missing // 2
        return " " * left + text + " " * (missing - left)
    return text + " " * missing


def truncate(text: str, limit: int) -> str:
    """Cut to `limit` columns, marking the loss with `…`.

    Colour codes are carried over rather than counted, so a cut never lands in
    the middle of an escape sequence and leaves the terminal painted.
    """
    if limit <= 0:
        return ""
    if width(text) <= limit:
        return text
    kept: list[str] = []
    seen, index = 0, 0
    while index < len(text) and seen < limit - 1:
        match = _ANSI_RE.match(text, index)
        if match:
            kept.append(match.group())
            index = match.end()
            continue
        kept.append(text[index])
        seen += 1
        index += 1
    # The cut may have dropped the reset that closed a coloured run.
    return "".join(kept) + "…" + ("\x1b[0m" if "\x1b[" in text else "")


def terminal_width(default: int = 80) -> int:
    """Columns available for output, falling back to 80 for pipes and CI."""
    try:
        return max(20, shutil.get_terminal_size((default, 24)).columns)
    except Exception:
        return default


# --- message helpers -------------------------------------------------------


def info(msg: str) -> None:
    print(msg)


def note(msg: str) -> None:
    print(paint(msg, "grey"))


def success(msg: str) -> None:
    print(f"{paint('✓', 'green')} {msg}")


def warn(msg: str) -> None:
    print(f"{paint('!', 'yellow')} {msg}", file=sys.stderr)


def error(msg: str) -> None:
    print(f"{paint('error', 'red', 'bold')}: {msg}", file=sys.stderr)


def step(msg: str) -> None:
    print(f"{paint('›', 'cyan')} {msg}")


# --- tables ----------------------------------------------------------------


def render_table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    aligns: Sequence[str] | None = None,
) -> str:
    rows = [list(r) for r in rows]
    ncols = max([len(headers)] + [len(r) for r in rows] or [0])
    aligns = list(aligns or []) + ["<"] * ncols
    widths = [width(h) for h in headers] + [0] * (ncols - len(headers))
    for row in rows:
        for i, cell in enumerate(row[:ncols]):
            widths[i] = max(widths[i], width(cell))

    def fmt(cells: Sequence[str], styles: Sequence[str] = (), align: str = "") -> str:
        out = [
            pad(cell, widths[i], align or aligns[i]) for i, cell in enumerate(cells[:ncols])
        ]
        line = "  ".join(out).rstrip()
        return paint(line, *styles) if styles else line

    # Headers are centred over their column; the data below stays left-aligned.
    lines = [fmt(headers, ("bold",), "^")] if headers else []
    lines.extend(fmt(r) for r in rows)
    return "\n".join(lines)


# --- prompts ---------------------------------------------------------------


def is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def confirm(question: str, default: bool = False, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not is_interactive():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {paint(suffix, 'grey')} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask(question: str, default: str | None = None) -> str | None:
    hint = f" {paint('[' + default + ']', 'grey')}" if default else ""
    try:
        answer = input(f"{question}{hint} ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return answer or default


# --- time formatting -------------------------------------------------------


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _humanize(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def relative_ms(epoch_ms: int | None, *, placeholder: str = "—") -> str:
    """Render an epoch-millisecond timestamp as `in 4h` / `3d ago`."""
    if not epoch_ms:
        return placeholder
    delta = (epoch_ms - now_ms()) / 1000.0
    return f"in {_humanize(delta)}" if delta >= 0 else f"{_humanize(delta)} ago"


def relative_iso(value: str | None, *, placeholder: str = "never") -> str:
    if not value:
        return placeholder
    parsed = _parse_iso(value)
    if parsed is None:
        return placeholder
    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return f"in {_humanize(delta)}" if delta >= 0 else f"{_humanize(delta)} ago"
