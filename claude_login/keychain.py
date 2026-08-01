"""Thin wrapper around macOS ``security(1)`` generic-password items.

Claude Code stores its OAuth credentials as a generic password whose *service*
name encodes the config directory and whose *account* is ``$USER``.  We only
ever read and delete those items — the writing is left to Claude Code itself.
"""

from __future__ import annotations

import binascii
import json
import os
import re
import subprocess
import sys
from typing import Any, Optional

from .errors import SecretStoreError

SECURITY_BIN = "/usr/bin/security"

# `security -i` reads one command per line; Claude Code caps the line at this
# many bytes before falling back to argv, and we mirror that.
_STDIN_LIMIT = 4032
_TIMEOUT = 10
_HEX_RE = re.compile(r"\A(?:[0-9A-Fa-f]{2})+\Z")
_SAFE_ARG_RE = re.compile(r"\A[A-Za-z0-9 ._-]+\Z")


def available() -> bool:
    """True when the macOS Keychain backend can be used."""
    return sys.platform == "darwin" and os.path.exists(SECURITY_BIN)


def account_name() -> str:
    """Reproduce Claude Code's keychain account selection."""
    name = os.environ.get("USER") or ""
    if not name:
        try:
            import pwd

            name = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            name = ""
    return name if re.fullmatch(r"[a-zA-Z0-9._-]+", name or "") else "claude-code-user"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [SECURITY_BIN, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            **kwargs,
        )
    except FileNotFoundError as exc:  # pragma: no cover - macOS always has it
        raise SecretStoreError(f"{SECURITY_BIN} not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise SecretStoreError("the macOS Keychain did not respond in time") from exc


def read(service: str, account: Optional[str] = None) -> Optional[str]:
    """Return the stored password, or None when the item does not exist."""
    if not available():
        return None
    proc = _run(["find-generic-password", "-a", account or account_name(), "-w", "-s", service])
    if proc.returncode != 0:
        return None
    return _decode(proc.stdout.rstrip("\n"))


def read_json(service: str, account: Optional[str] = None) -> Optional[dict[str, Any]]:
    raw = read(service, account)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def exists(service: str, account: Optional[str] = None) -> bool:
    if not available():
        return False
    proc = _run(["find-generic-password", "-a", account or account_name(), "-s", service])
    return proc.returncode == 0


def write(service: str, value: str, account: Optional[str] = None) -> None:
    """Create or update an item, keeping the secret out of the process table."""
    if not available():
        raise SecretStoreError("the macOS Keychain is not available on this platform")
    acct = account or account_name()
    if not _SAFE_ARG_RE.fullmatch(service) or not _SAFE_ARG_RE.fullmatch(acct):
        raise SecretStoreError(f"refusing to write keychain item with unsafe name: {service!r}")
    payload = binascii.hexlify(value.encode("utf-8")).decode("ascii")
    command = f'add-generic-password -U -a "{acct}" -s "{service}" -X "{payload}"\n'
    if len(command) <= _STDIN_LIMIT:
        # Preferred: the secret travels over stdin, never through argv.
        proc = _run(["-i"], input=command)
    else:
        proc = _run(["add-generic-password", "-U", "-a", acct, "-s", service, "-X", payload])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", "; ")
        raise SecretStoreError(f"keychain write failed{f' ({detail})' if detail else ''}")


def delete(service: str, account: Optional[str] = None) -> bool:
    """Delete an item. Returns True when something was actually removed."""
    if not available():
        return False
    proc = _run(["delete-generic-password", "-a", account or account_name(), "-s", service])
    return proc.returncode == 0


def _decode(raw: str) -> str:
    """`security -w` prints hex when the payload is not printable text."""
    candidate = raw.strip()
    if candidate and _HEX_RE.fullmatch(candidate):
        try:
            decoded = binascii.unhexlify(candidate).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return raw
        # Only prefer the decoded form when it actually looks like the payload.
        if decoded.lstrip()[:1] in ("{", "["):
            return decoded
    return raw
