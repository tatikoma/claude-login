"""Live rate-limit usage per account.

Talks to the same endpoint Claude Code uses for ``/usage``:

    GET  https://api.anthropic.com/api/oauth/usage      -> utilisation windows
    POST https://api.anthropic.com/v1/oauth/token       -> refresh an expired token

Only the profile's own credentials are ever sent, and a refresh is attempted
only when the access token has actually expired — a valid token is used as-is,
which keeps us out of any refresh race with a running session.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import claude_cli, ui

BASE_API_URL = os.environ.get("CLAUDE_LOGIN_API_BASE", "https://api.anthropic.com")
USAGE_PATH = "/api/oauth/usage"
PROFILE_PATH = "/api/oauth/profile"
TOKEN_PATH = "/v1/oauth/token"
CLIENT_ID = os.environ.get(
    "CLAUDE_LOGIN_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
)
BETA_HEADER = "oauth-2025-04-20"

TIMEOUT = 6
#: Refresh only once the token is this close to (or past) expiry.
REFRESH_MARGIN_MS = 60_000

_ssl_ctx: Optional[ssl.SSLContext] = None


# --- model -----------------------------------------------------------------


@dataclass
class Window:
    """One rate-limit window: how full it is and when it resets."""

    percent: Optional[float] = None
    resets_at: Optional[datetime] = None
    scope: str = ""

    @property
    def known(self) -> bool:
        return self.percent is not None

    @property
    def maxed(self) -> bool:
        return self.percent is not None and self.percent >= 100


@dataclass
class Usage:
    session: Window
    weekly: Window
    #: Worst per-model weekly cap (e.g. Opus), when one is stricter than the overall.
    weekly_scoped: Optional[Window] = None
    fetched_at: int = 0


# --- transport -------------------------------------------------------------


def _context() -> ssl.SSLContext:
    """A verifying SSL context, even on Pythons shipped without a CA bundle."""
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for loader in (_certifi_bundle, lambda: "/etc/ssl/cert.pem"):
            path = loader()
            if path and Path(path).exists():
                try:
                    ctx = ssl.create_default_context(cafile=path)
                    break
                except (ssl.SSLError, OSError):
                    continue
    _ssl_ctx = ctx
    return ctx


def _certifi_bundle() -> Optional[str]:
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def _connect() -> http.client.HTTPSConnection:
    """A connection to the API, tunnelled through HTTPS_PROXY when set.

    ``http.client`` directly rather than ``urllib.request``: building an opener
    chain per call cost more than the request itself (410ms → 275ms for three
    parallel lookups).
    """
    target = urllib.parse.urlsplit(BASE_API_URL)
    host, port = target.hostname or "api.anthropic.com", target.port or 443
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        parsed = urllib.parse.urlsplit(proxy if "//" in proxy else f"//{proxy}")
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port or 8080, timeout=TIMEOUT, context=_context()
        )
        connection.set_tunnel(host, port)
        return connection
    return http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=_context())


def _request(
    path: str, *, token: Optional[str] = None, payload: Optional[dict] = None
) -> tuple[int, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"anthropic-beta": BETA_HEADER, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection = None
    try:
        connection = _connect()
        connection.request("POST" if body is not None else "GET", path, body, headers)
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            return response.status, None
        return response.status, json.loads(raw or b"{}")
    except Exception:
        return 0, None
    finally:
        if connection is not None:
            connection.close()


# --- credentials -----------------------------------------------------------


def _refresh(profile, blob: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Exchange the refresh token for a new access token and persist the result."""
    tokens = claude_cli.oauth_tokens(blob)
    refresh_token = tokens.get("refreshToken")
    if not refresh_token:
        return None
    scopes = tokens.get("scopes") or []
    status, data = _request(
        TOKEN_PATH,
        payload={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": " ".join(scopes) if scopes else "",
        },
    )
    if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
        return None

    now = int(time.time() * 1000)
    updated = dict(tokens)
    updated["accessToken"] = data["access_token"]
    updated["refreshToken"] = data.get("refresh_token") or refresh_token
    if isinstance(data.get("expires_in"), (int, float)):
        updated["expiresAt"] = now + int(data["expires_in"] * 1000)
    if isinstance(data.get("refresh_token_expires_in"), (int, float)):
        updated["refreshTokenExpiresAt"] = now + int(data["refresh_token_expires_in"] * 1000)
    if isinstance(data.get("scope"), str) and data["scope"]:
        updated["scopes"] = data["scope"].split()

    merged = {**blob, "claudeAiOauth": updated}
    claude_cli.write_credentials(profile.config_dir, merged)
    return merged


def _token_for(profile) -> Optional[str]:
    blob = claude_cli.read_credentials(profile.config_dir)
    tokens = claude_cli.oauth_tokens(blob)
    access = tokens.get("accessToken")
    if not access:
        return None
    expires_at = tokens.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at - REFRESH_MARGIN_MS > ui.now_ms():
        return access
    refreshed = _refresh(profile, blob or {})
    return claude_cli.oauth_tokens(refreshed).get("accessToken") if refreshed else access


# --- parsing ---------------------------------------------------------------


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def _window(node: Any) -> Window:
    if not isinstance(node, dict):
        return Window()
    percent = node.get("utilization")
    if percent is None:
        percent = node.get("percent")
    return Window(
        percent=float(percent) if isinstance(percent, (int, float)) else None,
        resets_at=_parse_time(node.get("resets_at")),
    )


def parse(payload: dict[str, Any], *, fetched_at: int = 0) -> Usage:
    session = _window(payload.get("five_hour"))
    weekly = _window(payload.get("seven_day"))

    worst: Optional[Window] = None
    for entry in payload.get("limits") or []:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        candidate = _window(entry)
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name") or ""
        candidate.scope = model
        if candidate.percent is None:
            continue
        if worst is None or candidate.percent > (worst.percent or 0):
            worst = candidate
    # Only interesting when it binds harder than the overall weekly cap.
    if worst and weekly.percent is not None and worst.percent <= weekly.percent:
        worst = None

    return Usage(session=session, weekly=weekly, weekly_scoped=worst, fetched_at=fetched_at)


# --- public API ------------------------------------------------------------


def fetch_one(profile) -> Optional[Usage]:
    """Live usage for one profile, or None when it cannot be read right now."""
    token = _token_for(profile)
    if not token:
        return None
    status, payload = _request(USAGE_PATH, token=token)
    if status != 200 or not isinstance(payload, dict):
        return None
    return parse(payload, fetched_at=ui.now_ms())


def fetch_org_uuid(profile) -> Optional[str]:
    """The account's organisation uuid, which names the app's chat directory.

    Lives here rather than in ``claude_app`` because it needs this module's
    transport and its refresh-on-expiry handling; the app itself reads the very
    same endpoint to decide where to keep its sessions.
    """
    token = _token_for(profile)
    if not token:
        return None
    status, payload = _request(PROFILE_PATH, token=token)
    if status != 200 or not isinstance(payload, dict):
        return None
    organization = payload.get("organization")
    uuid = organization.get("uuid") if isinstance(organization, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def fetch_all(profiles: Iterable[Any]) -> dict[str, Optional[Usage]]:
    """Fetch usage for every profile concurrently; never raises.

    Nothing is cached between runs on purpose: a limit that reads 4% when it is
    really spent is worse than no number at all, so a failed lookup shows "—".
    """
    targets = list(profiles)
    if not targets:
        return {}
    _context()  # build the shared TLS context once, not inside every worker
    results: dict[str, Optional[Usage]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        futures = {pool.submit(fetch_one, p): p.name for p in targets}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception:
                results[name] = None
    return results


class BackgroundFetch:
    """Fetches usage off the main thread so the list can paint straight away.

    ``values`` is empty until the lookup lands; ``settled()`` reports completion
    exactly once, which is the picker's cue to redraw.
    """

    def __init__(self, profiles: Iterable[Any]):
        self._profiles = list(profiles)
        self.values: dict[str, Optional[Usage]] = {}
        self._done = threading.Event()
        self._reported = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            self.values = fetch_all(self._profiles)
        except Exception:
            pass
        finally:
            self._done.set()

    @property
    def pending(self) -> bool:
        return not self._done.is_set()

    def settled(self) -> bool:
        """True once, the first time the fetch has finished."""
        if self._reported or self.pending:
            return False
        self._reported = True
        return True

    def wait(self, timeout: Optional[float] = None) -> dict[str, Optional[Usage]]:
        self._done.wait(timeout)
        return self.values


# --- rendering -------------------------------------------------------------


def _style(percent: Optional[float]) -> tuple[str, ...]:
    if percent is None:
        return ("grey",)
    if percent >= 100:
        return ("red", "bold")
    if percent >= 85:
        return ("red",)
    if percent >= 60:
        return ("yellow",)
    return ("green",)


def reset_stamp(moment: Optional[datetime], *, weekly: bool) -> str:
    """`14:20` for a window closing today, `28.07 22:59` otherwise."""
    if moment is None:
        return ""
    if weekly or moment.date() != datetime.now().astimezone().date():
        return moment.strftime("%d.%m %H:%M")
    return moment.strftime("%H:%M")


#: Widest percentage is "100%", so right-align every value to that. In a
#: monospace list this keeps the ⟳ clocks in one straight column.
PERCENT_WIDTH = 4


def _percent(text: str, styles: tuple[str, ...]) -> str:
    """Right-align inside a fixed field; padding goes outside the colour codes."""
    return " " * max(0, PERCENT_WIDTH - ui.width(text)) + ui.paint(text, *styles)


def render(window: Optional[Window], *, weekly: bool, blocked: bool = False) -> str:
    """` 4% ⟳ 14:20` — the percentage plus when the window closes.

    An untouched 5-hour window has no end yet: it only starts on your first
    request, and the API reports ``resets_at: null`` until then.  That shows as
    ``0% ⟳ —`` rather than a blank, so the column always reads the same way.

    ``blocked`` paints the value red regardless of its own level: an empty
    5-hour window is no use while the weekly cap is spent.
    """
    if window is None or not window.known:
        return _percent("—", ("grey",))
    style = ("red", "bold") if blocked else _style(window.percent)
    stamp = reset_stamp(window.resets_at, weekly=weekly)
    # Dim the clock so the percentage stays the thing you read first — unless
    # the window is full, where the reset time is the useful bit.
    clock_style = style if window.maxed and stamp else ("grey",)
    return _percent(f"{round(window.percent):g}%", style) + " " + ui.paint(
        f"⟳ {stamp or '—'}", *clock_style
    )


def cells(usage: Optional[Usage], *, pending: bool = False) -> tuple[str, str]:
    """The (5h, week) columns for one account.

    The weekly column is the overall weekly cap.  A per-model cap (Opus, Fable…)
    only gets appended when it is already exhausted while the overall one is
    not — otherwise the account would look usable when it is not.
    """
    if usage is None:
        placeholder = _percent("…" if pending else "—", ("grey",))
        return placeholder, placeholder
    # A spent weekly cap blocks the account outright, so do not let a fresh
    # 5-hour window sit there in green as if it were usable.
    session = render(usage.session, weekly=False, blocked=usage.weekly.maxed)
    week = render(usage.weekly, weekly=True)
    scoped = usage.weekly_scoped
    if scoped and scoped.maxed and not usage.weekly.maxed:
        suffix = f"· {scoped.scope or 'model'} 100%"
        if not _same_moment(scoped.resets_at, usage.weekly.resets_at):
            suffix += f" ⟳ {reset_stamp(scoped.resets_at, weekly=True)}"
        week += " " + ui.paint(suffix, "red")
    return session, week


def _same_moment(left: Optional[datetime], right: Optional[datetime]) -> bool:
    if left is None or right is None:
        return False
    return abs((left - right).total_seconds()) < 60
