"""Everything we know about the ``claude`` executable and its on-disk layout.

Claude Code keys both its config directory *and* its Keychain item off the
``CLAUDE_CONFIG_DIR`` environment variable:

* config file  -> ``$CLAUDE_CONFIG_DIR/.config.json`` if present, else
                  ``$CLAUDE_CONFIG_DIR/.claude.json`` (``~/.claude.json`` when unset)
* credentials  -> Keychain service ``Claude Code-credentials`` when the variable
                  is unset, otherwise ``Claude Code-credentials-<sha256(dir)[:8]>``,
                  with ``$CLAUDE_CONFIG_DIR/.credentials.json`` as a fallback.

That is the whole trick behind this tool: point ``CLAUDE_CONFIG_DIR`` at a
per-account directory and Claude Code isolates the login for us.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Optional

from . import keychain
from .errors import ClaudeCliError

DEFAULT_CONFIG_DIRNAME = ".claude"
CREDENTIALS_FILENAME = ".credentials.json"


# --- locating the binary ---------------------------------------------------


def find_claude() -> str:
    """Absolute path to the ``claude`` executable."""
    override = os.environ.get("CLAUDE_LOGIN_CLAUDE_BIN")
    if override:
        if not os.access(override, os.X_OK):
            raise ClaudeCliError(f"CLAUDE_LOGIN_CLAUDE_BIN={override!r} is not executable")
        return override
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (Path.home() / ".local/bin/claude", Path("/usr/local/bin/claude")):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    raise ClaudeCliError(
        "could not find the `claude` executable on PATH — install Claude Code first, "
        "or point CLAUDE_LOGIN_CLAUDE_BIN at it"
    )


def version() -> Optional[str]:
    try:
        proc = subprocess.run(
            [find_claude(), "--version"], capture_output=True, text=True, timeout=20
        )
    except (ClaudeCliError, subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


# --- path / keychain-name derivation ---------------------------------------


def default_config_dir() -> str:
    return str(Path.home() / DEFAULT_CONFIG_DIRNAME)


def credentials_service(config_dir: Optional[str]) -> str:
    """Keychain service name Claude Code uses for ``CLAUDE_CONFIG_DIR=config_dir``.

    ``None`` means "the variable is unset", i.e. the machine-wide default login.
    """
    prefix = os.environ.get("CLAUDE_LOGIN_KEYCHAIN_PREFIX", "Claude Code")
    oauth_suffix = os.environ.get("CLAUDE_LOGIN_OAUTH_SUFFIX", "")
    if config_dir is None:
        scope = ""
    else:
        normalized = unicodedata.normalize("NFC", config_dir)
        scope = "-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{oauth_suffix}-credentials{scope}"


def storage_dir(config_dir: Optional[str]) -> Path:
    return Path(config_dir) if config_dir else Path(default_config_dir())


def global_config_path(config_dir: Optional[str]) -> Path:
    """Location of the big ``.claude.json`` blob for a given config dir."""
    base = storage_dir(config_dir)
    override = base / ".config.json"
    if override.exists():
        return override
    return (Path(config_dir) if config_dir else Path.home()) / ".claude.json"


def read_global_config(config_dir: Optional[str]) -> dict[str, Any]:
    path = global_config_path(config_dir)
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# --- credentials -----------------------------------------------------------


#: Each Keychain read is a ~25ms `security` subprocess and a single run asks for
#: the same profile several times, so remember them for the life of the process.
#: Anything that changes credentials invalidates its entry.
_credentials_cache: dict[Optional[str], Optional[dict[str, Any]]] = {}


def _load_credentials(config_dir: Optional[str]) -> Optional[dict[str, Any]]:
    from_keychain = keychain.read_json(credentials_service(config_dir))
    if from_keychain:
        return from_keychain
    fallback = storage_dir(config_dir) / CREDENTIALS_FILENAME
    try:
        with fallback.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_credentials(config_dir: Optional[str]) -> Optional[dict[str, Any]]:
    """Read the credential blob, preferring the Keychain like Claude Code does."""
    if config_dir not in _credentials_cache:
        _credentials_cache[config_dir] = _load_credentials(config_dir)
    return _credentials_cache[config_dir]


def forget_credentials(config_dir: Optional[str] = None, *, everything: bool = False) -> None:
    if everything:
        _credentials_cache.clear()
    else:
        _credentials_cache.pop(config_dir, None)


def prefetch_credentials(config_dirs: Iterable[Optional[str]]) -> None:
    """Warm the cache for several profiles at once.

    The reads are independent subprocesses, so doing them concurrently turns
    N × 25ms into roughly one 25ms wait.
    """
    pending = [d for d in dict.fromkeys(config_dirs) if d not in _credentials_cache]
    if len(pending) < 2:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        loaded = list(pool.map(_load_credentials, pending))
    _credentials_cache.update(zip(pending, loaded))


def write_credentials(config_dir: Optional[str], blob: dict[str, Any]) -> None:
    """Store a credential blob, keeping whichever backend already holds it.

    Claude Code prefers the Keychain but falls back to a plaintext file; writing
    to the other one would leave two copies and let a stale one win on read.
    """
    payload = json.dumps(blob, ensure_ascii=True, separators=(",", ":"))
    target = storage_dir(config_dir) / CREDENTIALS_FILENAME
    _credentials_cache[config_dir] = blob
    if keychain.available() and (
        keychain.exists(credentials_service(config_dir)) or not target.exists()
    ):
        keychain.write(credentials_service(config_dir), payload)
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".credentials-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def delete_credentials(config_dir: Optional[str]) -> bool:
    """Remove both the Keychain item and the plaintext fallback."""
    forget_credentials(config_dir)
    removed = keychain.delete(credentials_service(config_dir))
    fallback = storage_dir(config_dir) / CREDENTIALS_FILENAME
    try:
        fallback.unlink()
        removed = True
    except OSError:
        pass
    return removed


# --- transcripts -----------------------------------------------------------

#: Claude Code truncates a project directory name here and appends a hash.
PROJECT_NAME_LIMIT = 200
_NON_ALNUM = re.compile(r"[^a-zA-Z0-9]")
_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _legacy_hash36(text: str) -> str:
    """Reproduce Claude Code's ``(h << 5) - h + c | 0`` string hash, base 36."""
    value = 0
    for char in text:
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
        if value >= 0x80000000:
            value -= 0x100000000
    value = abs(value)
    if value == 0:
        return "0"
    digits = ""
    while value:
        value, remainder = divmod(value, 36)
        digits = _BASE36[remainder] + digits
    return digits


def project_dir_name(path: str) -> str:
    """Directory Claude Code keeps a working directory's transcripts under."""
    slug = _NON_ALNUM.sub("-", path)
    if len(slug) <= PROJECT_NAME_LIMIT:
        return slug
    return f"{slug[:PROJECT_NAME_LIMIT]}-{_legacy_hash36(path)}"


def transcript_dirs(config_dir: Optional[str], cwd: str) -> list[Path]:
    base = storage_dir(config_dir) / "projects"
    candidates = {cwd}
    try:
        candidates.add(os.path.realpath(cwd))
    except OSError:
        pass
    return [base / project_dir_name(candidate) for candidate in candidates]


def has_transcript(config_dir: Optional[str], cwd: str) -> bool:
    """True when ``claude --continue`` would find something in this directory.

    Without a transcript that flag aborts the launch outright, so callers use
    this to decide whether passing it is safe.
    """
    for directory in transcript_dirs(config_dir, cwd):
        try:
            for entry in directory.iterdir():
                if entry.name.endswith(".jsonl") and entry.stat().st_size > 0:
                    return True
        except OSError:
            continue
    return False


def oauth_tokens(credentials: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Pull the ``claudeAiOauth`` sub-object out of a credential blob."""
    if not credentials:
        return {}
    tokens = credentials.get("claudeAiOauth")
    return tokens if isinstance(tokens, dict) else {}


# --- running the CLI -------------------------------------------------------


def child_env(config_dir: Optional[str], extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    # Never let an ambient token or key shadow the profile we just selected.
    for leaked in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(leaked, None)
    env.update(extra or {})
    return env


def run(config_dir: Optional[str], args: list[str], *, capture: bool = False, timeout: int = 0):
    """Run ``claude <args>`` against a specific config dir."""
    cmd = [find_claude(), *args]
    kwargs: dict[str, Any] = {"env": child_env(config_dir)}
    if capture:
        kwargs.update(capture_output=True, text=True)
    if timeout:
        kwargs["timeout"] = timeout
    return subprocess.run(cmd, **kwargs)


def auth_status(config_dir: Optional[str], *, timeout: int = 30) -> dict[str, Any]:
    """``claude auth status --json`` for a config dir; ``{}`` when unavailable."""
    try:
        proc = run(config_dir, ["auth", "status", "--json"], capture=True, timeout=timeout)
    except (ClaudeCliError, subprocess.TimeoutExpired, OSError):
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def login(config_dir: Optional[str], extra_args: Optional[list[str]] = None) -> int:
    """Run the interactive browser login against a config dir."""
    proc = run(config_dir, ["auth", "login", *(extra_args or [])])
    forget_credentials(config_dir)
    return proc.returncode


def logout(config_dir: Optional[str]) -> int:
    proc = run(config_dir, ["auth", "logout"])
    forget_credentials(config_dir)
    return proc.returncode


def exec_claude(config_dir: Optional[str], args: list[str]) -> "int":
    """Replace this process with ``claude`` so signals and exit codes pass through."""
    binary = find_claude()
    os.execve(binary, [binary, *args], child_env(config_dir))
    raise ClaudeCliError("exec failed")  # pragma: no cover - execve does not return


def running_sessions() -> list[int]:
    """PIDs of live ``claude`` processes (best effort)."""
    try:
        proc = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [int(line) for line in proc.stdout.split() if line.isdigit()]
