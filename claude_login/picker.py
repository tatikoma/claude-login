"""An arrow-key list picker built on termios — no curses, no dependencies."""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import ui

ARROW_UP = "up"
ARROW_DOWN = "down"
ENTER = "enter"
CANCEL = "cancel"
#: Returned by read_key when nothing arrived within the timeout. Not a key any
#: terminal can produce, so it can never collide with real input.
TIMEOUT = "\0timeout"

#: How often to check `poll` while waiting for a keystroke.
POLL_SECONDS = 0.12


@dataclass
class Item:
    """One row. ``cells`` are aligned into columns; the first is the row's name."""

    cells: Sequence[str]
    value: Any = None

    @property
    def label(self) -> str:
        return self.cells[0] if self.cells else ""


@dataclass
class Result:
    action: str  # "select", "cancel", or one of the registered action keys
    index: Optional[int] = None
    item: Optional[Item] = None


@dataclass
class Action:
    key: str
    description: str
    needs_item: bool = False


@dataclass
class _Terminal:
    """Raw-mode terminal bound to /dev/tty (so pipes do not break the picker)."""

    read_fd: int
    write: Any
    _saved: Any = None
    _own_read: bool = False
    _own_write: bool = False
    _lines: int = field(default=0, init=False)
    _pending: bytes = field(default=b"", init=False)

    def __enter__(self) -> "_Terminal":
        self._saved = termios.tcgetattr(self.read_fd)
        tty.setraw(self.read_fd)
        self.write.write("\x1b[?25l")  # hide cursor
        self.write.flush()
        return self

    def __exit__(self, *exc) -> None:
        self.write.write("\x1b[?25h")
        self.write.flush()
        if self._saved is not None:
            termios.tcsetattr(self.read_fd, termios.TCSADRAIN, self._saved)
        if self._own_read:
            os.close(self.read_fd)
        if self._own_write:
            self.write.close()

    def render(self, lines: Sequence[str]) -> None:
        out = []
        if self._lines:
            out.append(f"\x1b[{self._lines}A")
        for line in lines:
            out.append("\x1b[2K" + line + "\r\n")
        # Clear any rows left over from a previously taller frame.
        for _ in range(max(0, self._lines - len(lines))):
            out.append("\x1b[2K\r\n")
        self._lines = max(len(lines), self._lines)
        self.write.write("".join(out))
        self.write.flush()

    def restart(self) -> None:
        """Drop the frame on screen and repaint from the top-left instead.

        A resize is the one event the cursor arithmetic cannot survive: the
        emulator re-wraps what is already on screen, by rules that differ
        between terminals, so nothing tells us where the old frame now begins.
        Counting more cleverly cannot fix that — only starting from a position
        we know can, at the cost of the screen the list was drawn over.
        """
        self.write.write("\x1b[H\x1b[2J")
        self.write.flush()
        self._lines = 0

    def clear(self) -> None:
        if not self._lines:
            return
        self.write.write(f"\x1b[{self._lines}A" + "\x1b[0J")
        self.write.flush()
        self._lines = 0

    def _next_byte(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """One byte, from the pushback buffer or the tty.

        Returns ``None`` when the timeout expired and ``b""`` on end of input —
        the caller has to tell those apart, or a closed tty turns into a spin.

        Escape sequences must be consumed a byte at a time: reading a fixed
        chunk would swallow — and drop — whatever the user typed next, which
        loses keystrokes whenever input arrives faster than the redraw.
        """
        if self._pending:
            byte, self._pending = self._pending[:1], self._pending[1:]
            return byte
        if timeout is not None and not select.select([self.read_fd], [], [], timeout)[0]:
            return None
        try:
            return os.read(self.read_fd, 1)
        except OSError:
            return b""

    def read_key(self, timeout: Optional[float] = None) -> str:
        first = self._next_byte(timeout)
        if first is None:
            return TIMEOUT
        if not first:
            return CANCEL
        if first in (b"\r", b"\n"):
            return ENTER
        if first == b"\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if first == b"\x04":  # Ctrl-D
            return CANCEL
        if first != b"\x1b":
            return first.decode("utf-8", "replace")

        # Escape: a bare Esc, or the introducer of a CSI / SS3 sequence.
        second = self._next_byte(0.05)
        if not second:
            return CANCEL
        if second not in (b"[", b"O"):
            self._pending = second + self._pending
            return CANCEL
        sequence = b""
        while True:
            byte = self._next_byte(0.05)
            if not byte:
                break
            sequence += byte
            if 0x40 <= byte[0] <= 0x7E:  # final byte of the sequence
                break
        if sequence == b"A":
            return ARROW_UP
        if sequence == b"B":
            return ARROW_DOWN
        return ""


def _open_terminal() -> Optional[_Terminal]:
    read_fd, own_read = None, False
    try:
        if sys.stdin.isatty():
            read_fd = sys.stdin.fileno()
        else:
            read_fd, own_read = os.open("/dev/tty", os.O_RDONLY), True
    except (OSError, ValueError):
        return None

    write, own_write = sys.stdout, False
    try:
        if not sys.stdout.isatty():
            write, own_write = open("/dev/tty", "w"), True
    except (OSError, ValueError):
        if own_read and read_fd is not None:
            os.close(read_fd)
        return None

    try:
        termios.tcgetattr(read_fd)
    except termios.error:
        if own_read and read_fd is not None:
            os.close(read_fd)
        if own_write:
            write.close()
        return None

    return _Terminal(read_fd=read_fd, write=write, _own_read=own_read, _own_write=own_write)


def supported() -> bool:
    terminal = _open_terminal()
    if terminal is None:
        return False
    if terminal._own_read:
        os.close(terminal.read_fd)
    if terminal._own_write:
        terminal.write.close()
    return True


#: Two columns for the selection marker.
MARKER_WIDTH = 2
#: Gaps between columns, widest first. The narrow one is tried before any text
#: is cut: tight spacing still reads, a trimmed account name does not.
GAPS = ("  ", " ")
#: Never squeeze a column past this: a two-letter stub identifies nothing.
MIN_COLUMN = 8


def _column_widths(rows: Sequence[Sequence[str]]) -> list[int]:
    count = max((len(r) for r in rows), default=0)
    widths = [0] * count
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], ui.width(cell))
    return widths


def _fit(widths: Sequence[int], budget: int) -> list[int]:
    """Squeeze columns into `budget`, taking from the first column first.

    Column zero holds the row's name, which stays recognisable from its start —
    a trimmed `someone@examp…` still picks out the account.  The value
    columns lose their meaning outright once cut (`98% ⟳ 05.08 01:00 · Fa…`),
    so they are only touched when trimming the name is not enough, and then
    always the widest of them, so the loss spreads instead of gutting one.
    """
    result = list(widths)
    excess = sum(result) - budget
    if excess <= 0 or not result:
        return result
    give = min(excess, max(0, result[0] - MIN_COLUMN))
    result[0] -= give
    excess -= give
    while excess > 0:
        index = max(range(len(result)), key=lambda i: result[i])
        if result[index] <= MIN_COLUMN:
            break  # everything is at the floor; the caller cuts the line instead
        result[index] -= 1
        excess -= 1
    return result


def _wrap_hints(parts: Sequence[str], limit: int) -> list[str]:
    """Fold the key hints onto as many lines as they need.

    The picker redraws by moving the cursor up over a known number of lines, so
    a line the terminal wraps on its own puts every following frame out of step
    and the list starts overwriting itself.  Wrapping here keeps the count ours.
    """
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current} · {part}" if current else part
        if current and ui.width(candidate) > limit:
            lines.append(current)
            current = part
        else:
            current = candidate
    return lines + [current] if current else lines


def _layout(
    items: Sequence[Item],
    selected: int,
    actions: Sequence[Action],
    title: str,
    headers: Sequence[str] = (),
    enter_label: str = "launch",
    quit_label: str = "quit",
    *,
    limit: Optional[int] = None,
) -> list[str]:
    limit = ui.terminal_width() if limit is None else limit
    grid = [list(item.cells) for item in items]
    natural = _column_widths(grid + ([list(headers)] if headers else []))
    for gap in GAPS:
        budget = limit - MARKER_WIDTH - len(gap) * max(0, len(natural) - 1)
        if sum(natural) <= budget:
            break
    widths = _fit(natural, max(MIN_COLUMN, budget))

    def compose(cells: Sequence[str], marker: str, *, bold_first=False, align="<") -> str:
        parts = [
            ui.pad(
                ui.truncate(
                    ui.paint(cell, "bold") if index == 0 and bold_first else cell,
                    widths[index],
                ),
                widths[index],
                align,
            )
            for index, cell in enumerate(cells)
        ]
        return (f"{marker} " + gap.join(parts)).rstrip()

    lines = []
    if headers:
        # Centred over the column, like the table `list` prints.
        lines.append(ui.paint(compose(headers, " ", align="^"), "grey"))
    for index, item in enumerate(items):
        active = index == selected
        marker = ui.paint("❯", "cyan", "bold") if active else " "
        lines.append(compose(item.cells, marker, bold_first=active))

    keys = _wrap_hints(
        [f"{ui.paint('↑↓', 'bold')} move", f"{ui.paint('⏎', 'bold')} {enter_label}"]
        + [f"{ui.paint(a.key, 'bold')} {a.description}" for a in actions]
        + [f"{ui.paint('q', 'bold')} {quit_label}"],
        limit,
    )
    header = (title.split("\n") + [""]) if title else []
    frame = header + lines + [""] + [ui.paint(line, "grey") for line in keys]
    # Last resort: a title or a floored-out row must still not wrap.
    return [ui.truncate(line, limit) for line in frame]


def _resolve(source):
    return list(source()) if callable(source) else list(source)


def _text(source) -> str:
    return source() if callable(source) else source


def pick(
    items,
    *,
    title="",
    actions: Sequence[Action] = (),
    initial: int = 0,
    headers: Sequence[str] = (),
    enter_label: str = "launch",
    quit_label: str = "quit",
    on_select=None,
    poll=None,
) -> Result:
    """Show an interactive list. Falls back to a numbered prompt without a TTY.

    ``items`` and ``title`` may be callables, in which case they are re-invoked
    after every handled selection.  Together with ``on_select`` — which returns
    True when it handled the row in place — that keeps a self-refreshing screen
    inside a single call, so the terminal is never taken out of raw mode
    mid-session and no keystroke is lost between redraws.

    ``poll`` is checked while idle and, when it returns True, the rows are
    rebuilt.  That is how the list paints immediately and fills in slow data
    (rate-limit usage) as it lands, instead of waiting for it up front.

    The idle wait is always bounded, even without ``poll``, so a resized window
    is noticed within one tick: the columns are laid out afresh every frame, so
    room won back by a wider window undoes the trimming of its own accord.
    """
    resolved = _resolve(items)
    if not resolved and not actions:
        return Result(CANCEL)

    terminal = _open_terminal()
    if terminal is None:
        return _pick_fallback(
            resolved, title=_text(title), actions=actions, headers=headers
        )

    action_keys = {a.key: a for a in actions}
    selected = max(0, min(initial, len(resolved) - 1)) if resolved else 0
    dirty = True
    drawn_at = 0

    with terminal:
        while True:
            if dirty:
                drawn_at = ui.terminal_width()
                terminal.render(
                    _layout(
                        resolved, selected, actions, _text(title), headers, enter_label, quit_label
                    )
                )
                dirty = False
            try:
                key = terminal.read_key(POLL_SECONDS)
            except KeyboardInterrupt:
                terminal.clear()
                return Result(CANCEL)

            if key == TIMEOUT:
                # Nothing typed; catch up on a resized window and on data that
                # the caller has meanwhile finished fetching.
                if ui.terminal_width() != drawn_at:
                    terminal.restart()
                    dirty = True
                if poll and poll():
                    resolved = _resolve(items)
                    selected = min(selected, len(resolved) - 1) if resolved else 0
                    dirty = True
                continue
            dirty = True

            if key in (CANCEL, "q", "Q"):
                terminal.clear()
                return Result(CANCEL)
            if key == ARROW_UP or key == "k":
                if resolved:
                    selected = (selected - 1) % len(resolved)
            elif key == ARROW_DOWN or key == "j":
                if resolved:
                    selected = (selected + 1) % len(resolved)
            elif key == ENTER:
                if not resolved:
                    continue
                if on_select is not None and on_select(selected):
                    resolved = _resolve(items)
                    selected = min(selected, len(resolved) - 1) if resolved else 0
                    continue
                terminal.clear()
                return Result("select", selected, resolved[selected])
            elif key in action_keys:
                action = action_keys[key]
                if action.needs_item and not resolved:
                    continue
                terminal.clear()
                item = resolved[selected] if resolved else None
                return Result(action.key, selected if resolved else None, item)


def _pick_fallback(
    items: Sequence[Item], *, title: str, actions: Sequence[Action], headers: Sequence[str] = ()
) -> Result:
    """Numbered prompt for dumb terminals, CI and piped input."""
    if title:
        print(title)
        print()
    numbered = [
        [f"{index:>2}.", *item.cells] for index, item in enumerate(items, start=1)
    ]
    print(ui.render_table(["", *headers] if headers else [], numbered))
    print()
    hints = ", ".join([f"{a.key} = {a.description}" for a in actions])
    prompt = f"Select 1-{len(items)}" + (f" ({hints}, q = quit)" if hints else " (q = quit)")
    answer = ui.ask(f"{prompt}:")
    if not answer or answer.lower() in ("q", "quit"):
        return Result(CANCEL)
    action_keys = {a.key: a for a in actions}
    if answer in action_keys:
        return Result(answer, None, None)
    if answer.isdigit() and 1 <= int(answer) <= len(items):
        index = int(answer) - 1
        return Result("select", index, items[index])
    ui.warn(f"unrecognised choice: {answer!r}")
    return Result(CANCEL)
