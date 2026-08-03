"""Everything we know about the Claude desktop app and its on-disk layout.

The app keys its whole user-data directory off ``CLAUDE_USER_DATA_DIR`` — the
same trick ``CLAUDE_CONFIG_DIR`` plays for the CLI:

* user data   -> ``$CLAUDE_USER_DATA_DIR`` (``~/Library/Application Support/Claude``
                 when unset); the app moves its logs into ``<dir>/Logs`` too
* credentials -> ``<dir>/config.json``, under ``oauth:tokenCacheV2``, encrypted
                 with Electron safeStorage.  We never read the value, only note
                 whether it is there: the app is its own credential provider and
                 hands the sidecar CLI ``CLAUDE_CODE_HOST_CREDS_FILE``, so
                 ``claude auth login`` does not sign the app in.
* Code chats  -> ``<dir>/claude-code-sessions/<accountUuid>/<orgUuid>/<id>.json``,
                 which is exactly why they seem to vanish when you sign in as
                 somebody else — the app looks under the *current* account.

Two instances with different data directories run side by side (verified: there
is no single-instance lock), so accounts do not have to take turns.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .errors import ClaudeAppError

DEFAULT_BUNDLE = "/Applications/Claude.app"
BINARY_SUBPATH = "Contents/MacOS/Claude"
DEFAULT_SUPPORT_SUBPATH = "Library/Application Support/Claude"

SESSIONS_DIRNAME = "claude-code-sessions"
AGENT_SESSIONS_DIRNAME = "local-agent-mode-sessions"
CONFIG_FILENAME = "config.json"
#: The app names every chat file it owns with this prefix.
SESSION_PREFIX = "local_"
#: Suffix we give a chat directory we replaced with a link while the app had it
#: open. It sits next to the link, so it has to be excluded from every scan —
#: otherwise our own backup reads as a directory that still needs pooling.
REPLACED_MARKER = ".replaced-"

#: Presence of any of these keys in config.json means the profile is signed in.
TOKEN_KEYS = ("oauth:tokenCacheV2", "oauth:tokenCache")
ACCOUNT_KEY = "lastKnownAccountUuid"

#: Entries symlinked from the machine-wide app support directory into every app
#: profile: the downloaded sidecar CLI and VM bundles (hundreds of megabytes we
#: do not want a copy of per account) plus extensions and their MCP config.
#: ``config.json`` is deliberately absent — it holds the account's own token.
DEFAULT_APP_SHARED = [
    "claude-code",
    "claude-code-vm",
    "vm_bundles",
    "Claude Extensions",
    "Claude Extensions Settings",
    "extensions-installations.json",
    "claude_desktop_config.json",
    "git-worktrees.json",
]


# --- locating the app ------------------------------------------------------


def app_bundle() -> str:
    return os.environ.get("CLAUDE_LOGIN_APP_PATH") or DEFAULT_BUNDLE


def find_app() -> str:
    """Absolute path to the app's executable."""
    binary = Path(app_bundle()) / BINARY_SUBPATH
    if not os.access(binary, os.X_OK):
        raise ClaudeAppError(
            f"could not find the Claude app at {app_bundle()} — install it, "
            "or point CLAUDE_LOGIN_APP_PATH at the bundle"
        )
    return str(binary)


def available() -> bool:
    try:
        find_app()
    except ClaudeAppError:
        return False
    return True


def default_app_support_dir() -> str:
    """The machine-wide user-data directory: the app's own ``~/.claude``."""
    override = os.environ.get("CLAUDE_LOGIN_APP_SUPPORT")
    if override:
        return str(Path(override).expanduser())
    return str(Path.home() / DEFAULT_SUPPORT_SUBPATH)


# --- layout ----------------------------------------------------------------


def config_path(data_dir: str) -> Path:
    return Path(data_dir) / CONFIG_FILENAME


def read_config(data_dir: str) -> dict[str, Any]:
    try:
        with config_path(data_dir).open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def sessions_root(data_dir: str, *, agent: bool = False) -> Path:
    return Path(data_dir) / (AGENT_SESSIONS_DIRNAME if agent else SESSIONS_DIRNAME)


def session_leaf_dirs(data_dir: str, *, agent: bool = False) -> list[Path]:
    """The ``<accountUuid>/<orgUuid>`` chat directories the app has created.

    It only makes one once it has resolved both an account and an organisation,
    so a freshly created profile has none.  That is why wiring the pool has to
    work lazily as well as up front — the directory shows up after the login.
    """
    root = sessions_root(data_dir, agent=agent)
    leaves: list[Path] = []
    try:
        accounts = sorted(root.iterdir())
    except OSError:
        return leaves
    for account in accounts:
        if account.is_symlink() or not account.is_dir():
            continue
        try:
            orgs = sorted(account.iterdir())
        except OSError:
            continue
        leaves.extend(
            org
            for org in orgs
            if (org.is_symlink() or org.is_dir())
            and REPLACED_MARKER not in org.name
            and not org.name.startswith(".")
        )
    return leaves


# --- login state -----------------------------------------------------------


@dataclass
class AppStatus:
    """Whether an app profile is signed in, and as whom."""

    state: str  # signed-in | logged-out | missing
    account_uuid: Optional[str] = None

    @property
    def signed_in(self) -> bool:
        return self.state == "signed-in"


def app_status(data_dir: str) -> AppStatus:
    """Read the login state without ever decrypting the token."""
    if not Path(data_dir).is_dir():
        return AppStatus("missing")
    config = read_config(data_dir)
    account = config.get(ACCOUNT_KEY)
    signed_in = any(config.get(key) for key in TOKEN_KEYS)
    return AppStatus(
        "signed-in" if signed_in else "logged-out",
        account if isinstance(account, str) else None,
    )


# --- chats -----------------------------------------------------------------


def read_session(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def activity(session: dict[str, Any]) -> int:
    """When this chat was last touched, in epoch milliseconds."""
    for key in ("lastFocusedAt", "lastActivityAt", "createdAt"):
        value = session.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def sessions_in(pool: Path) -> Iterator[dict[str, Any]]:
    """Every chat in a pool directory. Non-chat files (tasks) are skipped."""
    try:
        entries = sorted(pool.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix != ".json" or not entry.name.startswith(SESSION_PREFIX):
            continue
        session = read_session(entry)
        if session:
            yield session


def last_session(pool: Path, cwd: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The chat you would want to carry on with: newest first, archived skipped.

    With ``cwd`` set only chats opened in that directory count, which is what
    makes this the app's answer to ``claude --continue``.
    """
    best: Optional[dict[str, Any]] = None
    for session in sessions_in(pool):
        if session.get("isArchived"):
            continue
        if cwd is not None and session.get("cwd") != cwd:
            continue
        if best is None or activity(session) > activity(best):
            best = session
    return best


# --- running the app -------------------------------------------------------


def child_env(
    data_dir: Optional[str], extra: Optional[dict[str, str]] = None
) -> dict[str, str]:
    env = dict(os.environ)
    if data_dir:
        env["CLAUDE_USER_DATA_DIR"] = data_dir
    else:
        env.pop("CLAUDE_USER_DATA_DIR", None)
    # The app spawns the real CLI as a sidecar.  It has to keep using the shared
    # ~/.claude, or transcripts would land outside the directory every profile
    # shares — and carrying a chat across accounts is the whole point.
    env.pop("CLAUDE_CONFIG_DIR", None)
    for leaked in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(leaked, None)
    env.update(extra or {})
    return env


def launch(data_dir: Optional[str], extra_env: Optional[dict[str, str]] = None) -> int:
    """Start the app detached and return its pid.

    Not ``execve``: a window has to outlive the terminal it was started from.
    Not ``open -a`` either — that one drops the environment we just built, and
    the environment is how the account gets selected.
    """
    binary = find_app()
    proc = subprocess.Popen(
        [binary],
        env=child_env(data_dir, extra_env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


#: Every Electron helper is started with ``--user-data-dir=<path>``. The value
#: can contain spaces, so it runs to the next ``--flag`` or the end of the line.
_USER_DATA_RE = re.compile(r"--user-data-dir=(.+?)(?=\s--|\s*$)")


def _process_lines() -> list[str]:
    """``ps`` output, one command line per process.

    Deliberately not ``pgrep -f``: it cannot see the app's main process at all
    (verified — it lists the two dozen helpers and misses the one that matters),
    so anything built on it would happily move files under a running app.
    """
    if sys.platform != "darwin":
        return []
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.splitlines()


def running_pids() -> list[int]:
    """PIDs of live app processes (best effort).

    Matches the bundle's own executable, so the renderer and utility helpers
    under ``Contents/Frameworks/Claude Helper.app`` do not count.
    """
    if sys.platform != "darwin":
        return []
    binary = f"{app_bundle()}/{BINARY_SUBPATH}"
    try:
        proc = subprocess.run(
            ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    pids = []
    for line in proc.stdout.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and command.strip().startswith(binary):
            pids.append(int(pid))
    return pids


def running_data_dirs() -> set[str]:
    """The user-data directories the app currently has open.

    The main process's argv says nothing about which directory it picked, but
    every helper it spawns carries ``--user-data-dir=<path>`` — which is a far
    more useful answer than "is the app running": one account's chats can be
    pooled while another account's window stays open.
    """
    bundle = app_bundle()
    found: set[str] = set()
    for line in _process_lines():
        if bundle not in line:
            continue
        match = _USER_DATA_RE.search(line)
        if match:
            found.add(os.path.normpath(match.group(1)))
    return found


def is_in_use(data_dir: str) -> bool:
    """True when a live app instance has this exact data directory open."""
    wanted = {os.path.normpath(data_dir)}
    try:
        wanted.add(os.path.normpath(os.path.realpath(data_dir)))
    except OSError:
        pass
    open_dirs = running_data_dirs()
    for candidate in list(open_dirs):
        try:
            open_dirs.add(os.path.normpath(os.path.realpath(candidate)))
        except OSError:
            continue
    return bool(wanted & open_dirs)
