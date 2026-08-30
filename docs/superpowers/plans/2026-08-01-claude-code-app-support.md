# Claude Code App support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `claude-login` запускает настольное приложение Claude под выбранным аккаунтом, а чаты и проекты вкладки Code остаются общими между аккаунтами.

**Architecture:** Каждому аккаунту — свой каталог данных приложения через `CLAUDE_USER_DATA_DIR` (проверено: экземпляры работают одновременно). Листовой каталог `claude-code-sessions/<accountUuid>/<orgUuid>` в каждом профиле — симлинк на один плоский пул в `~/.claude-accounts/app-shared/`. Тяжёлые бинари и расширения симлинкуются из системного каталога приложения. Всё знание о приложении — в новом модуле `claude_app.py`, по образцу `claude_cli.py`.

**Tech Stack:** Python 3.9+, только стандартная библиотека. Тесты — `unittest` против заглушек `claude` и приложения. Спека: `docs/superpowers/specs/2026-08-01-claude-code-app-support-design.md`.

**Замечание про коммиты:** каталог не является git-репозиторием, поэтому вместо шага «commit» в каждой задаче — контрольный прогон `python3 -m unittest discover -s tests` (должно быть `OK`, существующие 100 тестов зелёные).

---

## Файловая структура

| файл | ответственность |
| --- | --- |
| `claude_login/claude_app.py` | **создать.** Всё про приложение: бандл, раскладка каталога данных, статус логина, чтение файлов сессий, окружение, запуск процесса |
| `claude_login/errors.py` | добавить `ClaudeAppError` |
| `claude_login/store.py` | поля профиля `appDataDir`/`orgUuid`; ключи реестра; пул сессий и симлинки общего для App |
| `claude_login/usage.py` | `fetch_org_uuid` — нужен транспорт и обновление токена, которые уже здесь |
| `claude_login/commands.py` | выбор цели, `launch_app`, подкоманды `app*`, секции настроек, App в `doctor`, клавиша `o` |
| `claude_login/cli.py` | флаги `--app`/`--cli`, подкоманда `app` с `status`/`adopt`/`relink` |
| `tests/test_claude_login.py` | заглушка `FAKE_APP` и шесть новых классов тестов |
| `README.md` | раздел про App |

---

### Task 1: Модуль `claude_app.py` — раскладка, статус, окружение

**Files:**
- Create: `claude_login/claude_app.py`
- Modify: `claude_login/errors.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Добавить тип ошибки**

В `claude_login/errors.py`, после `ClaudeCliError`:

```python
class ClaudeAppError(ClaudeLoginError):
    """The Claude desktop app is missing or cannot be launched."""
```

- [ ] **Step 2: Написать падающие тесты**

В `tests/test_claude_login.py` добавить импорт `claude_app` в общий импорт из `claude_login`, затем класс:

```python
class TestAppLayout(unittest.TestCase):
    def test_bundle_override_is_honoured(self):
        with _env_var("CLAUDE_LOGIN_APP_PATH", "/tmp/Other.app"):
            self.assertEqual(claude_app.app_bundle(), "/tmp/Other.app")

    def test_support_dir_override_is_honoured(self):
        with _env_var("CLAUDE_LOGIN_APP_SUPPORT", "/tmp/support"):
            self.assertEqual(claude_app.default_app_support_dir(), "/tmp/support")

    def test_missing_bundle_is_an_error(self):
        with _env_var("CLAUDE_LOGIN_APP_PATH", "/tmp/nope-does-not-exist.app"):
            with self.assertRaises(ClaudeAppError):
                claude_app.find_app()

    def test_sessions_root_is_keyed_off_the_data_dir(self):
        self.assertEqual(
            claude_app.sessions_root("/d"), Path("/d/claude-code-sessions")
        )
        self.assertEqual(
            claude_app.sessions_root("/d", agent=True),
            Path("/d/local-agent-mode-sessions"),
        )


class TestAppEnv(unittest.TestCase):
    def test_data_dir_is_exported(self):
        env = claude_app.child_env("/data")
        self.assertEqual(env["CLAUDE_USER_DATA_DIR"], "/data")

    def test_none_clears_the_variable(self):
        with _env_var("CLAUDE_USER_DATA_DIR", "/stale"):
            self.assertNotIn("CLAUDE_USER_DATA_DIR", claude_app.child_env(None))

    def test_config_dir_is_dropped_so_transcripts_stay_shared(self):
        with _env_var("CLAUDE_CONFIG_DIR", "/some/profile"):
            self.assertNotIn("CLAUDE_CONFIG_DIR", claude_app.child_env("/data"))

    def test_ambient_credentials_are_dropped(self):
        with _env_var("ANTHROPIC_API_KEY", "sk-ant-nope"):
            self.assertNotIn("ANTHROPIC_API_KEY", claude_app.child_env("/data"))

    def test_extra_env_is_applied_last(self):
        env = claude_app.child_env("/data", {"FOO": "bar"})
        self.assertEqual(env["FOO"], "bar")
```

Вспомогательный контекст-менеджер — рядом с `Base`, до классов тестов:

```python
@contextlib.contextmanager
def _env_var(key: str, value: Optional[str]):
    """Set (or clear with None) an environment variable for the duration."""
    original = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
```

Дописать в шапку тестов `import contextlib` и `from typing import Optional`, а также `from claude_login.errors import ClaudeAppError, UsageError`.

- [ ] **Step 3: Прогнать и убедиться, что падает**

Run: `python3 -m unittest tests.test_claude_login.TestAppLayout tests.test_claude_login.TestAppEnv -v`
Expected: FAIL — `ImportError` / `AttributeError: module 'claude_login.claude_app' has no attribute ...`

- [ ] **Step 4: Написать модуль**

Создать `claude_login/claude_app.py`:

```python
"""Everything we know about the Claude desktop app and its on-disk layout.

The app keys its whole user-data directory off ``CLAUDE_USER_DATA_DIR`` — the
same trick ``CLAUDE_CONFIG_DIR`` plays for the CLI:

* user data   -> ``$CLAUDE_USER_DATA_DIR`` (``~/Library/Application Support/Claude``
                 when unset); the app also moves its logs into ``<dir>/Logs``
* credentials -> ``<dir>/config.json``, under ``oauth:tokenCacheV2``, encrypted
                 with Electron safeStorage.  We never read the value, only note
                 whether it is there: the app is its own credential provider and
                 hands the sidecar CLI ``CLAUDE_CODE_HOST_CREDS_FILE``, so
                 ``claude auth login`` does not sign the app in.
* Code chats  -> ``<dir>/claude-code-sessions/<accountUuid>/<orgUuid>/<id>.json``,
                 which is why they vanish when you sign in as someone else.

Two instances with different data directories run side by side — verified, no
single-instance lock — so accounts do not have to take turns.
"""

from __future__ import annotations

import json
import os
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

#: Presence of any of these in config.json means the profile is signed in.
TOKEN_KEYS = ("oauth:tokenCacheV2", "oauth:tokenCache")
ACCOUNT_KEY = "lastKnownAccountUuid"

#: Entries symlinked from the machine-wide app support directory into every app
#: profile: the downloaded sidecar CLI and VM bundles (hundreds of megabytes we
#: do not want per account) plus extensions and their MCP configuration.
#: ``config.json`` is deliberately absent — it holds the account's token.
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
    """The machine-wide user-data directory, i.e. the app's own ``~/.claude``."""
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
    """The ``<accountUuid>/<orgUuid>`` directories the app has created so far.

    The app only makes one once it has resolved an account and an organisation,
    so a freshly created profile has none — that is why wiring the pool has to
    work lazily as well as up front.
    """
    root = sessions_root(data_dir, agent=agent)
    leaves: list[Path] = []
    try:
        accounts = sorted(root.iterdir())
    except OSError:
        return leaves
    for account in accounts:
        if not account.is_dir():
            continue
        try:
            orgs = sorted(account.iterdir())
        except OSError:
            continue
        leaves.extend(org for org in orgs if org.is_dir() or org.is_symlink())
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


# --- sessions --------------------------------------------------------------


def read_session(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _activity(session: dict[str, Any]) -> int:
    for key in ("lastFocusedAt", "lastActivityAt", "createdAt"):
        value = session.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def sessions_in(pool: Path) -> Iterator[dict[str, Any]]:
    try:
        entries = sorted(pool.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.suffix == ".json" and entry.name.startswith("local_"):
            session = read_session(entry)
            if session:
                yield session


def last_session(pool: Path, cwd: Optional[str] = None) -> Optional[dict[str, Any]]:
    """The chat you would want to carry on with — newest first, archived skipped.

    With ``cwd`` set, only chats opened in that directory count, which is what
    makes this the app's answer to ``claude --continue``.
    """
    best: Optional[dict[str, Any]] = None
    for session in sessions_in(pool):
        if session.get("isArchived"):
            continue
        if cwd is not None and session.get("cwd") != cwd:
            continue
        if best is None or _activity(session) > _activity(best):
            best = session
    return best


# --- running the app -------------------------------------------------------


def child_env(data_dir: Optional[str], extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    if data_dir:
        env["CLAUDE_USER_DATA_DIR"] = data_dir
    else:
        env.pop("CLAUDE_USER_DATA_DIR", None)
    # The app spawns the real CLI as a sidecar. It must keep using the shared
    # ~/.claude, or transcripts would land outside the directory every profile
    # shares — and continuing a chat across accounts is the whole point.
    env.pop("CLAUDE_CONFIG_DIR", None)
    for leaked in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(leaked, None)
    env.update(extra or {})
    return env


def launch(data_dir: Optional[str], extra_env: Optional[dict[str, str]] = None) -> int:
    """Start the app detached and return its pid.

    Not ``execve``: a GUI must outlive the terminal it was started from. Not
    ``open -a`` either — that one drops the environment we just built.
    """
    binary = find_app()
    proc = subprocess.Popen(  # noqa: S603 - fixed path from find_app()
        [binary],
        env=child_env(data_dir, extra_env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def running_pids() -> list[int]:
    """PIDs of live app processes (best effort).

    Matches the bundle's own executable so the renderer and utility helpers,
    which live under ``Contents/Frameworks/Claude Helper.app``, do not count.
    """
    if sys.platform != "darwin":
        return []
    pattern = f"{app_bundle()}/{BINARY_SUBPATH}"
    try:
        proc = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return [int(line) for line in proc.stdout.split() if line.isdigit()]
```

- [ ] **Step 5: Прогнать тесты — должны пройти**

Run: `python3 -m unittest tests.test_claude_login.TestAppLayout tests.test_claude_login.TestAppEnv -v`
Expected: PASS (9 тестов)

- [ ] **Step 6: Контрольная точка**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, существующие 100 тестов не тронуты.

---

### Task 2: Статус логина и «continue» — тесты на данные

**Files:**
- Modify: `claude_login/claude_app.py` (только если тесты найдут расхождение)
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestAppStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="app-status-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _config(self, payload: dict) -> str:
        (self.tmp / "config.json").write_text(json.dumps(payload))
        return str(self.tmp)

    def test_missing_directory(self):
        self.assertEqual(claude_app.app_status(str(self.tmp / "nope")).state, "missing")

    def test_directory_without_a_token(self):
        status = claude_app.app_status(self._config({"locale": "en-US"}))
        self.assertEqual(status.state, "logged-out")
        self.assertFalse(status.signed_in)

    def test_token_cache_means_signed_in(self):
        status = claude_app.app_status(
            self._config({"oauth:tokenCacheV2": "djEw...", "lastKnownAccountUuid": "uuid-a"})
        )
        self.assertEqual(status.state, "signed-in")
        self.assertEqual(status.account_uuid, "uuid-a")

    def test_legacy_token_key_is_accepted(self):
        self.assertTrue(claude_app.app_status(self._config({"oauth:tokenCache": "djEw"})).signed_in)

    def test_empty_token_does_not_count(self):
        self.assertFalse(claude_app.app_status(self._config({"oauth:tokenCacheV2": ""})).signed_in)


class TestAppContinue(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = Path(tempfile.mkdtemp(prefix="app-pool-"))
        self.addCleanup(shutil.rmtree, self.pool, ignore_errors=True)

    def _session(self, name: str, **fields) -> None:
        payload = {"sessionId": name, "cwd": "/work", **fields}
        (self.pool / f"{name}.json").write_text(json.dumps(payload))

    def test_newest_by_last_focused_wins(self):
        self._session("local_a", lastFocusedAt=100)
        self._session("local_b", lastFocusedAt=300)
        self._session("local_c", lastFocusedAt=200)
        self.assertEqual(claude_app.last_session(self.pool)["sessionId"], "local_b")

    def test_falls_back_to_last_activity(self):
        self._session("local_a", lastActivityAt=500)
        self.assertEqual(claude_app.last_session(self.pool)["sessionId"], "local_a")

    def test_archived_sessions_are_skipped(self):
        self._session("local_a", lastFocusedAt=100)
        self._session("local_b", lastFocusedAt=900, isArchived=True)
        self.assertEqual(claude_app.last_session(self.pool)["sessionId"], "local_a")

    def test_cwd_filter(self):
        self._session("local_a", lastFocusedAt=100, cwd="/work")
        self._session("local_b", lastFocusedAt=900, cwd="/elsewhere")
        self.assertEqual(
            claude_app.last_session(self.pool, cwd="/work")["sessionId"], "local_a"
        )

    def test_no_sessions_at_all(self):
        self.assertIsNone(claude_app.last_session(self.pool))

    def test_non_session_files_are_ignored(self):
        (self.pool / "scheduled-tasks.json").write_text("{}")
        self.assertIsNone(claude_app.last_session(self.pool))
```

- [ ] **Step 2: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestAppStatus tests.test_claude_login.TestAppContinue -v`
Expected: PASS (11 тестов). Код уже написан в Task 1 — если что-то падает, править `claude_app.py`, а не тест.

- [ ] **Step 3: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 3: Реестр — поля профиля и ключи настроек

**Files:**
- Modify: `claude_login/store.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestAppRegistry(Base):
    def test_fresh_vault_defaults_to_the_cli(self):
        self.assertEqual(self.vault.launch_target, "cli")

    def test_launch_target_round_trip(self):
        with self.vault.locked():
            self.vault.set_launch_target("app")
            self.vault.save()
        self.assertEqual(self.vault.reload().launch_target, "app")

    def test_invalid_launch_target_falls_back_to_cli(self):
        with self.vault.locked():
            self.vault._data["launchTarget"] = "nonsense"
            self.vault.save()
        self.assertEqual(self.vault.reload().launch_target, "cli")

    def test_app_shared_defaults_to_the_built_in_list(self):
        self.assertEqual(self.vault.app_shared, claude_app.DEFAULT_APP_SHARED)

    def test_app_env_defaults_to_empty_and_round_trips(self):
        self.assertEqual(self.vault.app_env, {})
        with self.vault.locked():
            self.vault.set_app_env({"FOO": "bar"})
            self.vault.save()
        self.assertEqual(self.vault.reload().app_env, {"FOO": "bar"})

    def test_sessions_are_shared_and_per_account_by_default(self):
        self.assertTrue(self.vault.app_sessions_shared)
        self.assertTrue(self.vault.app_per_account)

    def test_app_data_dir_for_a_profile(self):
        profile = self.add(email="one@example.com")
        self.assertEqual(
            self.vault.app_data_dir_for(profile),
            str(self.vault.root / "profiles" / profile.name / "app-data"),
        )

    def test_default_profile_uses_the_machine_wide_app_dir(self):
        profile = Profile(name="default", config_dir=None)
        self.assertEqual(
            self.vault.app_data_dir_for(profile), claude_app.default_app_support_dir()
        )

    def test_per_account_off_sends_everyone_to_the_machine_wide_dir(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault._data["appPerAccount"] = False
            self.vault.save()
        self.assertEqual(
            self.vault.reload().app_data_dir_for(profile),
            claude_app.default_app_support_dir(),
        )

    def test_org_uuid_round_trips_through_the_registry(self):
        profile = self.add(email="one@example.com")
        profile.org_uuid = "org-1"
        with self.vault.locked():
            self.vault.upsert(profile)
            self.vault.save()
        self.assertEqual(self.vault.reload().get(profile.name).org_uuid, "org-1")
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestAppRegistry -v`
Expected: FAIL — `AttributeError: 'Vault' object has no attribute 'launch_target'`

- [ ] **Step 3: Реализовать**

В `claude_login/store.py`: добавить `from . import claude_app` к существующему `from . import claude_cli, ui`, и константу под `DEFAULT_SHARED`:

```python
#: Launch targets `claude-login` knows how to hand a session over to.
LAUNCH_TARGETS = ("cli", "app", "ask")

#: Where the shared pools of app chats live, relative to the vault root.
APP_SHARED_DIRNAME = "app-shared"
POOL_DIRNAMES = {
    claude_app.SESSIONS_DIRNAME: "ccd-sessions",
    claude_app.AGENT_SESSIONS_DIRNAME: "agent-sessions",
}
```

В `Profile` — два новых поля и их сериализация:

```python
    extra_args: list[str] = field(default_factory=list)
    app_data_dir: Optional[str] = None
    #: Cached organisation uuid; needed to name the app's session directory.
    org_uuid: Optional[str] = None
```

в `to_json` добавить `"appDataDir": self.app_data_dir, "orgUuid": self.org_uuid`, в `from_json` — `app_data_dir=data.get("appDataDir"), org_uuid=data.get("orgUuid")`.

В `Vault.load` — новые дефолты рядом с `data.setdefault("launchArgs", [])`:

```python
        # App support is off the beaten path until the user picks it: a fresh
        # install keeps launching the CLI, exactly as launchArgs stays empty.
        data.setdefault("launchTarget", "cli")
        data.setdefault("appShared", list(claude_app.DEFAULT_APP_SHARED))
        data.setdefault("appEnv", {})
        data.setdefault("appSessionsShared", True)
        data.setdefault("appPerAccount", True)
```

Аксессоры — после `set_launch_args`:

```python
    @property
    def launch_target(self) -> str:
        value = self.load()._data.get("launchTarget")
        return value if value in LAUNCH_TARGETS else "cli"

    def set_launch_target(self, target: str) -> None:
        if target not in LAUNCH_TARGETS:
            raise UsageError(f"unknown launch target {target!r} — pick one of {', '.join(LAUNCH_TARGETS)}")
        self._data["launchTarget"] = target

    @property
    def app_shared(self) -> list[str]:
        value = self.load()._data.get("appShared")
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_app_shared(self, entries: list[str]) -> None:
        self._data["appShared"] = list(entries)

    @property
    def app_env(self) -> dict[str, str]:
        value = self.load()._data.get("appEnv")
        if not isinstance(value, dict):
            return {}
        return {str(k): str(v) for k, v in value.items()}

    def set_app_env(self, env: dict[str, str]) -> None:
        self._data["appEnv"] = dict(env)

    @property
    def app_sessions_shared(self) -> bool:
        return bool(self.load()._data.get("appSessionsShared", True))

    def set_app_sessions_shared(self, value: bool) -> None:
        self._data["appSessionsShared"] = bool(value)

    @property
    def app_per_account(self) -> bool:
        return bool(self.load()._data.get("appPerAccount", True))

    def set_app_per_account(self, value: bool) -> None:
        self._data["appPerAccount"] = bool(value)
```

И раскладка каталогов — рядом с `dir_for`:

```python
    def app_dir_for(self, name: str) -> Path:
        return self.profiles_dir / name / "app-data"

    def app_data_dir_for(self, profile: Profile) -> str:
        """Which user-data directory the app should run against for this profile.

        The machine-wide directory plays the same role ``~/.claude`` plays for
        the CLI: it belongs to the ``default`` profile and is never moved.
        """
        if profile.is_default or not self.app_per_account:
            return claude_app.default_app_support_dir()
        return profile.app_data_dir or str(self.app_dir_for(profile.name))

    def pool_dir(self, kind: str) -> Path:
        return self.root / APP_SHARED_DIRNAME / POOL_DIRNAMES[kind]
```

- [ ] **Step 4: Прогнать — должно пройти**

Run: `python3 -m unittest tests.test_claude_login.TestAppRegistry -v`
Expected: PASS (10 тестов)

- [ ] **Step 5: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 4: Общие симлинки для профиля приложения

**Files:**
- Modify: `claude_login/store.py`
- Test: `tests/test_claude_login.py`

Существующий `link_shared` и новый `link_app_shared` отличаются только корнями и списком — общая часть выносится в `_link_entries`, иначе логика с `.shadowed` и побайтовым сравнением была бы скопирована дважды.

- [ ] **Step 1: Написать тесты**

```python
class TestAppSharedLinks(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app_support = self.tmp / "app-support"
        (self.app_support / "claude-code" / "2.1.0").mkdir(parents=True)
        (self.app_support / "Claude Extensions").mkdir()
        (self.app_support / "claude_desktop_config.json").write_text('{"mcpServers":{}}')
        (self.app_support / "config.json").write_text('{"oauth:tokenCacheV2":"djEw"}')
        self._env("CLAUDE_LOGIN_APP_SUPPORT", str(self.app_support))

    def test_heavy_entries_are_symlinked(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            linked, conflicts = self.vault.link_app_shared(profile)
        target = Path(self.vault.app_data_dir_for(profile))
        self.assertEqual(conflicts, [])
        self.assertIn("claude-code", linked)
        self.assertTrue((target / "claude-code").is_symlink())
        self.assertTrue((target / "Claude Extensions").is_symlink())

    def test_the_token_bearing_config_is_never_shared(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.link_app_shared(profile)
        target = Path(self.vault.app_data_dir_for(profile))
        self.assertFalse((target / "config.json").exists())

    def test_missing_sources_are_skipped_quietly(self):
        shutil.rmtree(self.app_support / "claude-code")
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            linked, conflicts = self.vault.link_app_shared(profile)
        self.assertNotIn("claude-code", linked)
        self.assertEqual(conflicts, [])

    def test_identical_private_copy_is_relinked_silently(self):
        profile = self.add(email="one@example.com")
        target = Path(self.vault.app_data_dir_for(profile))
        target.mkdir(parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mcpServers":{}}')
        with self.vault.locked():
            linked, conflicts = self.vault.link_app_shared(profile)
        self.assertEqual(conflicts, [])
        self.assertTrue((target / "claude_desktop_config.json").is_symlink())

    def test_diverged_copy_is_reported_not_clobbered(self):
        profile = self.add(email="one@example.com")
        target = Path(self.vault.app_data_dir_for(profile))
        target.mkdir(parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mine":true}')
        with self.vault.locked():
            _, conflicts = self.vault.link_app_shared(profile)
        self.assertIn("claude_desktop_config.json", conflicts)
        self.assertEqual(
            (target / "claude_desktop_config.json").read_text(), '{"mine":true}'
        )

    def test_repair_shadows_the_diverged_copy(self):
        profile = self.add(email="one@example.com")
        target = Path(self.vault.app_data_dir_for(profile))
        target.mkdir(parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mine":true}')
        with self.vault.locked():
            _, conflicts = self.vault.link_app_shared(profile, repair=True)
        self.assertEqual(conflicts, [])
        self.assertTrue((target / "claude_desktop_config.json").is_symlink())
        self.assertTrue(list((target / ".shadowed").iterdir()))

    def test_default_profile_is_the_source_not_a_target(self):
        profile = Profile(name="default", config_dir=None)
        with self.vault.locked():
            linked, conflicts = self.vault.link_app_shared(profile)
        self.assertEqual((linked, conflicts), ([], []))

    def test_profile_directory_is_private(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.link_app_shared(profile)
        mode = Path(self.vault.app_data_dir_for(profile)).stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestAppSharedLinks -v`
Expected: FAIL — `AttributeError: 'Vault' object has no attribute 'link_app_shared'`

- [ ] **Step 3: Вынести общую часть и добавить метод**

В `store.py` заменить тело `link_shared` на вызов общего помощника и добавить рядом `_link_entries` и `link_app_shared`:

```python
    def link_shared(self, profile: Profile, *, repair: bool = False) -> tuple[list[str], list[str]]:
        """Symlink shared entries from ``~/.claude`` into the profile directory.

        Claude Code rewrites files like ``settings.json`` atomically, and
        ``rename()`` replaces the symlink itself rather than following it — so a
        profile can quietly end up with a private copy.  Identical copies are
        re-linked silently; genuinely diverged ones are reported as conflicts and
        only moved aside when ``repair`` is set.

        Returns ``(linked, conflicts)``.
        """
        if profile.is_default or not profile.config_dir:
            return [], []
        return self._link_entries(
            Path(claude_cli.default_config_dir()),
            Path(profile.config_dir),
            self.shared,
            repair=repair,
        )

    def link_app_shared(self, profile: Profile, *, repair: bool = False) -> tuple[list[str], list[str]]:
        """Same idea for the app: link the heavy, account-neutral entries.

        ``config.json`` is never in the list — it carries the account's token,
        which is exactly what has to stay private per profile.
        """
        if profile.is_default or not self.app_per_account:
            return [], []
        return self._link_entries(
            Path(claude_app.default_app_support_dir()),
            Path(self.app_data_dir_for(profile)),
            self.app_shared,
            repair=repair,
        )

    def _link_entries(
        self,
        source_root: Path,
        target_root: Path,
        entries: list[str],
        *,
        repair: bool = False,
    ) -> tuple[list[str], list[str]]:
        target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        linked: list[str] = []
        conflicts: list[str] = []
        for entry in entries:
            source = source_root / entry
            target = target_root / entry
            if not source.exists():
                continue
            if target.is_symlink():
                if os.path.realpath(target) == os.path.realpath(source):
                    continue
                target.unlink()
            elif target.exists():
                if _same_content(target, source):
                    target.unlink()
                elif repair:
                    self._shadow(target_root, entry, target)
                else:
                    conflicts.append(entry)
                    continue
            try:
                target.symlink_to(source)
                linked.append(entry)
            except OSError:
                conflicts.append(entry)
        return linked, conflicts
```

Также обобщить инспекцию — `shared_conflicts` получает близнеца:

```python
    def app_shared_conflicts(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Inspect the app's shared entries without touching anything."""
        if profile.is_default or not self.app_per_account:
            return [], []
        return self._entry_conflicts(
            Path(claude_app.default_app_support_dir()),
            Path(self.app_data_dir_for(profile)),
            self.app_shared,
        )
```

и `shared_conflicts` переписать через тот же `_entry_conflicts`:

```python
    def shared_conflicts(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Inspect shared entries without touching anything: (missing, diverged)."""
        if profile.is_default or not profile.config_dir:
            return [], []
        return self._entry_conflicts(
            Path(claude_cli.default_config_dir()), Path(profile.config_dir), self.shared
        )

    @staticmethod
    def _entry_conflicts(
        source_root: Path, target_root: Path, entries: list[str]
    ) -> tuple[list[str], list[str]]:
        missing, diverged = [], []
        for entry in entries:
            source = source_root / entry
            target = target_root / entry
            if not source.exists():
                continue
            if not target.exists() and not target.is_symlink():
                missing.append(entry)
            elif not target.is_symlink() and not _same_content(target, source):
                diverged.append(entry)
        return missing, diverged
```

- [ ] **Step 4: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestAppSharedLinks tests.test_claude_login.TestSharedLinks -v`
Expected: PASS — 8 новых и 5 существующих (рефакторинг не изменил поведение).

- [ ] **Step 5: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 5: Пул сессий — адопция и симлинки

**Files:**
- Modify: `claude_login/store.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestSessionPool(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app_support = self.tmp / "app-support"
        self.app_support.mkdir()
        self._env("CLAUDE_LOGIN_APP_SUPPORT", str(self.app_support))

    def _leaf(self, data_dir: Path, account: str, org: str) -> Path:
        leaf = data_dir / claude_app.SESSIONS_DIRNAME / account / org
        leaf.mkdir(parents=True)
        return leaf

    def _chat(self, leaf: Path, name: str, activity: int) -> Path:
        path = leaf / f"{name}.json"
        path.write_text(json.dumps({"sessionId": name, "lastActivityAt": activity}))
        return path

    def test_existing_chats_move_into_the_pool(self):
        leaf = self._leaf(self.app_support, "acct-a", "org-1")
        self._chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertTrue((pool / "local_one.json").is_file())
        self.assertTrue(leaf.is_symlink())
        self.assertEqual(os.path.realpath(leaf), os.path.realpath(pool))
        self.assertEqual(plan.moved, 1)

    def test_two_accounts_end_up_in_one_pool(self):
        first = self._leaf(self.app_support, "acct-a", "org-1")
        second = self._leaf(self.app_support, "acct-b", "org-2")
        self._chat(first, "local_one", 10)
        self._chat(second, "local_two", 20)
        self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertEqual(
            sorted(p.name for p in pool.iterdir()), ["local_one.json", "local_two.json"]
        )

    def test_backup_is_taken_before_the_first_move(self):
        leaf = self._leaf(self.app_support, "acct-a", "org-1")
        self._chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertIsNotNone(plan.backup)
        self.assertTrue(list(Path(plan.backup).rglob("local_one.json")))

    def test_dry_run_changes_nothing(self):
        leaf = self._leaf(self.app_support, "acct-a", "org-1")
        self._chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support), dry_run=True)
        self.assertEqual(plan.moved, 1)
        self.assertFalse(leaf.is_symlink())
        self.assertTrue((leaf / "local_one.json").is_file())
        self.assertFalse(self.vault.pool_dir(claude_app.SESSIONS_DIRNAME).exists())

    def test_running_twice_is_idempotent(self):
        leaf = self._leaf(self.app_support, "acct-a", "org-1")
        self._chat(leaf, "local_one", 10)
        self.vault.wire_session_pool(str(self.app_support))
        again = self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual(again.moved, 0)
        self.assertTrue(leaf.is_symlink())

    def test_collision_keeps_the_newer_chat(self):
        first = self._leaf(self.app_support, "acct-a", "org-1")
        second = self._leaf(self.app_support, "acct-b", "org-2")
        self._chat(first, "local_same", 10)
        self._chat(second, "local_same", 99)
        plan = self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        kept = json.loads((pool / "local_same.json").read_text())
        self.assertEqual(kept["lastActivityAt"], 99)
        self.assertEqual(plan.collisions, 1)

    def test_lazily_adopts_a_directory_the_app_created_later(self):
        self.vault.wire_session_pool(str(self.app_support))
        leaf = self._leaf(self.app_support, "acct-new", "org-9")
        self._chat(leaf, "local_late", 5)
        self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertTrue((pool / "local_late.json").is_file())
        self.assertTrue(leaf.is_symlink())

    def test_agent_sessions_get_their_own_pool(self):
        leaf = self.app_support / claude_app.AGENT_SESSIONS_DIRNAME / "acct-a" / "org-1"
        leaf.mkdir(parents=True)
        (leaf / "local_agent.json").write_text(json.dumps({"sessionId": "local_agent"}))
        self.vault.wire_session_pool(str(self.app_support))
        self.assertTrue(
            (self.vault.pool_dir(claude_app.AGENT_SESSIONS_DIRNAME) / "local_agent.json").is_file()
        )

    def test_nothing_to_do_when_the_app_has_never_run(self):
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual((plan.moved, plan.linked, plan.collisions), (0, 0, 0))
        self.assertIsNone(plan.backup)

    def test_unwiring_returns_the_chats_to_the_profile(self):
        leaf = self._leaf(self.app_support, "acct-a", "org-1")
        self._chat(leaf, "local_one", 10)
        self.vault.wire_session_pool(str(self.app_support))
        self.vault.unwire_session_pool(str(self.app_support))
        self.assertFalse(leaf.is_symlink())
        self.assertTrue((leaf / "local_one.json").is_file())
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestSessionPool -v`
Expected: FAIL — `AttributeError: 'Vault' object has no attribute 'wire_session_pool'`

- [ ] **Step 3: Реализовать**

В `store.py` — датакласс отчёта рядом со `Status`:

```python
@dataclass
class PoolPlan:
    """What wiring the app's chat pool did (or would do, for a dry run)."""

    moved: int = 0
    linked: int = 0
    collisions: int = 0
    backup: Optional[str] = None
```

и методы в `Vault`:

```python
    def wire_session_pool(
        self, data_dir: str, *, dry_run: bool = False, backup: bool = True
    ) -> PoolPlan:
        """Point every ``<accountUuid>/<orgUuid>`` chat directory at one pool.

        The app looks its chats up under the *current* account's uuid, which is
        why they seem to vanish after signing in as someone else.  Collapsing
        every leaf directory onto a single pool makes the same Recents list show
        up whichever account is signed in.

        A directory only exists once the app has resolved an account, so this is
        also the lazy path: call it again later and whatever the app created in
        the meantime is folded in.
        """
        plan = PoolPlan()
        for kind in POOL_DIRNAMES:
            leaves = claude_app.session_leaf_dirs(
                data_dir, agent=kind == claude_app.AGENT_SESSIONS_DIRNAME
            )
            pending = [leaf for leaf in leaves if not leaf.is_symlink()]
            if not pending:
                continue
            if backup and not dry_run and plan.backup is None:
                plan.backup = self._backup_sessions(data_dir)
            pool = self.pool_dir(kind)
            if not dry_run:
                pool.mkdir(mode=0o700, parents=True, exist_ok=True)
            for leaf in pending:
                moved, collisions = self._drain_into_pool(leaf, pool, dry_run=dry_run)
                plan.moved += moved
                plan.collisions += collisions
                if not dry_run:
                    shutil.rmtree(leaf, ignore_errors=True)
                    leaf.symlink_to(pool)
                plan.linked += 1
        return plan

    def unwire_session_pool(self, data_dir: str) -> int:
        """Undo the wiring: give the profile its own copy of the chats back."""
        restored = 0
        for kind in POOL_DIRNAMES:
            agent = kind == claude_app.AGENT_SESSIONS_DIRNAME
            pool = self.pool_dir(kind)
            for leaf in claude_app.session_leaf_dirs(data_dir, agent=agent):
                if not leaf.is_symlink():
                    continue
                leaf.unlink()
                leaf.mkdir(mode=0o700, parents=True, exist_ok=True)
                if pool.is_dir():
                    for entry in pool.iterdir():
                        if entry.is_file():
                            shutil.copy2(entry, leaf / entry.name)
                restored += 1
        return restored

    def _backup_sessions(self, data_dir: str) -> str:
        """Copy the chat directories aside before the first move."""
        destination = self.root / APP_SHARED_DIRNAME / f".backup-{ui.now_ms()}"
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        for kind in POOL_DIRNAMES:
            source = Path(data_dir) / kind
            if source.is_dir():
                shutil.copytree(source, destination / kind, symlinks=True)
        return str(destination)

    def _drain_into_pool(self, leaf: Path, pool: Path, *, dry_run: bool) -> tuple[int, int]:
        """Move one leaf directory's files into the pool. Newer wins a clash."""
        moved = collisions = 0
        try:
            entries = sorted(leaf.iterdir())
        except OSError:
            return 0, 0
        for entry in entries:
            if not entry.is_file():
                continue
            target = pool / entry.name
            if target.exists():
                collisions += 1
                if not dry_run and self._is_newer(entry, target):
                    self._park_loser(target)
                    shutil.move(str(entry), str(target))
                elif not dry_run:
                    self._park_loser(entry)
                continue
            moved += 1
            if not dry_run:
                shutil.move(str(entry), str(target))
        return moved, collisions

    @staticmethod
    def _is_newer(candidate: Path, incumbent: Path) -> bool:
        def stamp(path: Path) -> int:
            session = claude_app.read_session(path)
            for key in ("lastActivityAt", "lastFocusedAt", "createdAt"):
                value = session.get(key)
                if isinstance(value, (int, float)):
                    return int(value)
            try:
                return int(path.stat().st_mtime * 1000)
            except OSError:
                return 0

        return stamp(candidate) > stamp(incumbent)

    def _park_loser(self, path: Path) -> None:
        """Keep the chat that lost a name clash instead of deleting it."""
        attic = self.root / APP_SHARED_DIRNAME / "collisions"
        attic.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(str(path), str(attic / f"{path.stem}.{ui.now_ms()}{path.suffix}"))
```

- [ ] **Step 4: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestSessionPool -v`
Expected: PASS (10 тестов)

- [ ] **Step 5: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 6: `orgUuid` из API

**Files:**
- Modify: `claude_login/usage.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestOrgUuidLookup(Base):
    def test_org_uuid_is_read_from_the_profile_endpoint(self):
        profile = self.add(email="one@example.com")
        captured = {}

        def fake_request(path, *, token=None, payload=None):
            captured["path"] = path
            captured["token"] = token
            return 200, {"organization": {"uuid": "org-42"}}

        self._patch(usage, "_request", fake_request)
        self.assertEqual(usage.fetch_org_uuid(profile), "org-42")
        self.assertEqual(captured["path"], usage.PROFILE_PATH)
        self.assertTrue(captured["token"])

    def test_failure_returns_none_rather_than_raising(self):
        profile = self.add(email="one@example.com")
        self._patch(usage, "_request", lambda *a, **k: (0, None))
        self.assertIsNone(usage.fetch_org_uuid(profile))

    def test_a_malformed_uuid_is_rejected(self):
        profile = self.add(email="one@example.com")
        self._patch(usage, "_request", lambda *a, **k: (200, {"organization": {"uuid": 7}}))
        self.assertIsNone(usage.fetch_org_uuid(profile))
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestOrgUuidLookup -v`
Expected: FAIL — `AttributeError: module 'claude_login.usage' has no attribute 'PROFILE_PATH'`

- [ ] **Step 3: Реализовать**

В `usage.py` рядом с `USAGE_PATH`:

```python
PROFILE_PATH = "/api/oauth/profile"
```

и функцию после `fetch_one`:

```python
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
```

- [ ] **Step 4: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestOrgUuidLookup -v`
Expected: PASS (3 теста)

- [ ] **Step 5: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 7: Цель запуска — argv, реестр, запуск приложения

**Files:**
- Modify: `claude_login/cli.py`, `claude_login/commands.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestLaunchTarget(Base):
    def _args(self, **kwargs) -> argparse.Namespace:
        return argparse.Namespace(**kwargs)

    def test_registry_default_is_the_cli(self):
        self.assertEqual(commands.resolve_target(self.vault, self._args()), "cli")

    def test_registry_can_prefer_the_app(self):
        with self.vault.locked():
            self.vault.set_launch_target("app")
            self.vault.save()
        self.assertEqual(commands.resolve_target(self.vault.reload(), self._args()), "app")

    def test_app_flag_wins_over_the_registry(self):
        self.assertEqual(commands.resolve_target(self.vault, self._args(app=True)), "app")

    def test_cli_flag_wins_over_the_registry(self):
        with self.vault.locked():
            self.vault.set_launch_target("app")
            self.vault.save()
        self.assertEqual(
            commands.resolve_target(self.vault.reload(), self._args(cli=True)), "cli"
        )

    def test_ask_falls_back_to_the_cli_without_a_terminal(self):
        with self.vault.locked():
            self.vault.set_launch_target("ask")
            self.vault.save()
        self.assertEqual(commands.resolve_target(self.vault.reload(), self._args()), "cli")

    def test_app_flag_survives_the_subcommand_split(self):
        argv, forwarded = split_args(["--app", "work"])
        self.assertEqual(argv, ["--app", "use", "work"])
        self.assertEqual(forwarded, [])

    def test_target_flags_are_not_forwarded_to_claude(self):
        args = cli.build_parser().parse_args(["use", "work", "--app"])
        self.assertTrue(getattr(args, "app", False))
        self.assertEqual(list(args.claude_args or []), [])


class TestAppLaunch(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app_support = self.tmp / "app-support"
        self.app_support.mkdir()
        self._env("CLAUDE_LOGIN_APP_SUPPORT", str(self.app_support))
        bundle = self.tmp / "Fake.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        binary = bundle / claude_app.BINARY_SUBPATH
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self._env("CLAUDE_LOGIN_APP_PATH", str(bundle))
        self.spawned = []
        self._patch(claude_app, "launch", self._record_launch)

    def _record_launch(self, data_dir, extra_env=None):
        self.spawned.append((data_dir, dict(extra_env or {})))
        return 4242

    def test_launch_uses_the_profiles_own_data_dir(self):
        profile = self.add(email="one@example.com")
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(self.spawned[0][0], self.vault.app_data_dir_for(profile))

    def test_configured_env_reaches_the_app(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.set_app_env({"FOO": "bar"})
            self.vault.save()
        commands.launch_app(self.vault.reload(), profile, argparse.Namespace())
        self.assertEqual(self.spawned[0][1], {"FOO": "bar"})

    def test_the_data_directory_and_shared_links_are_prepared(self):
        (self.app_support / "claude-code").mkdir()
        profile = self.add(email="one@example.com")
        commands.launch_app(self.vault, profile, argparse.Namespace())
        data_dir = Path(self.vault.app_data_dir_for(profile))
        self.assertTrue(data_dir.is_dir())
        self.assertTrue((data_dir / "claude-code").is_symlink())

    def test_launching_records_the_profile_as_last_used(self):
        profile = self.add(email="one@example.com")
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(self.vault.reload().last_used, profile.name)

    def test_a_missing_bundle_is_reported(self):
        self._env("CLAUDE_LOGIN_APP_PATH", str(self.tmp / "Absent.app"))
        profile = self.add(email="one@example.com")
        with self.assertRaises(ClaudeAppError):
            commands.launch_app(self.vault, profile, argparse.Namespace())
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestLaunchTarget tests.test_claude_login.TestAppLaunch -v`
Expected: FAIL — `AttributeError: module 'claude_login.commands' has no attribute 'resolve_target'`

- [ ] **Step 3: Флаги в `cli.py`**

Добавить в `_add_launch_flags` (он уже вызывается и в корневом парсере, и в `use`):

```python
    parser.add_argument(
        "--app",
        dest="app",
        action="store_true",
        default=argparse.SUPPRESS,
        help="launch the Claude desktop app instead of the CLI",
    )
    parser.add_argument(
        "--cli",
        dest="cli",
        action="store_true",
        default=argparse.SUPPRESS,
        help="launch the CLI even when the configured target is the app",
    )
```

`_VALUE_FLAGS` не меняется: оба флага булевы.

- [ ] **Step 4: Выбор цели и запуск в `commands.py`**

Добавить `claude_app` в импорт `from . import ...`, `ClaudeAppError` — в импорт из `.errors`, и функции после `launch_args_for`:

```python
def resolve_target(vault: Vault, args) -> str:
    """Which target to hand this launch to: the CLI or the desktop app.

    An explicit flag wins over the configured default; ``ask`` only asks when
    there is a terminal to ask in, and otherwise behaves like ``cli`` so that
    scripts keep working.
    """
    if getattr(args, "app", False):
        return "app"
    if getattr(args, "cli", False):
        return "cli"
    target = vault.launch_target
    if target != "ask":
        return target
    if not ui.is_interactive():
        return "cli"
    return "app" if ui.confirm("Launch the Claude app instead of the CLI?", default=False) else "cli"


def prepare_app_profile(vault: Vault, profile: Profile) -> tuple[str, store.PoolPlan]:
    """Make sure the profile has a data directory, its links and its pool.

    Wiring the pool moves files around, so it is skipped while the app is up:
    losing a launch to a migration would be the wrong trade for something you
    do every day.
    """
    data_dir = vault.app_data_dir_for(profile)
    Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = store.PoolPlan()
    with vault.locked():
        linked, conflicts = vault.link_app_shared(profile)
        if not profile.app_data_dir and not profile.is_default and vault.app_per_account:
            profile.app_data_dir = data_dir
            vault.upsert(profile)
        if vault.app_sessions_shared and not claude_app.running_pids():
            plan = vault.wire_session_pool(data_dir)
        vault.touch(profile.name)
        vault.save()
    if linked:
        ui.note(f"  shared with the app: {', '.join(linked)}")
    if conflicts:
        ui.warn(f"not shared (a local copy is in the way): {', '.join(conflicts)}")
    return data_dir, plan


def launch_app(vault: Vault, profile: Profile, args) -> int:
    """Start the desktop app under this account and return its pid."""
    claude_app.find_app()
    data_dir, plan = prepare_app_profile(vault, profile)
    status = claude_app.app_status(data_dir)

    label = ui.paint(profile.display, "bold")
    ui.step(f"{label}  {ui.paint(describe(profile), 'grey')}")
    ui.note(f"  CLAUDE_USER_DATA_DIR={data_dir}")
    if plan.linked:
        ui.note(f"  chats pooled: {plan.moved} moved from {plan.linked} directory(ies)")
    if plan.collisions:
        ui.note(f"  {plan.collisions} name clash(es) resolved by keeping the newer chat")
    if vault.app_sessions_shared and claude_app.running_pids():
        ui.note("  the app is already running — chats will be pooled by `claude-login app adopt`")
    if not status.signed_in:
        ui.note("  the app will ask you to sign in once — that login is separate from the CLI's")
    elif status.account_uuid and profile.account_uuid and status.account_uuid != profile.account_uuid:
        ui.warn(f"this app profile is signed in as {status.account_uuid}, not {profile.account_uuid}")
    else:
        last = claude_app.last_session(
            vault.pool_dir(claude_app.SESSIONS_DIRNAME), cwd=os.getcwd()
        )
        if last and last.get("title"):
            ui.note(f"  last chat here: {last['title']}")

    pid = claude_app.launch(data_dir, vault.app_env)
    ui.success(f"launched the Claude app (pid {pid})")
    return pid
```

- [ ] **Step 5: Развести цели в `cmd_use` и `interactive`**

В `cmd_use` заменить последние две строки на:

```python
    profile = vault.reload().get(profile.name)
    if resolve_target(vault, args) == "app":
        launch_app(vault, profile, args)
        return 0
    launch(vault, profile, list(args.claude_args or []), launch_args_for(vault, args))
    return 0
```

В `interactive` — то же в ветке `result.action == "select"`, плюс новое действие в списке `actions`:

```python
        picker.Action("o", "open in the other target", needs_item=True),
```

и его обработка рядом с остальными:

```python
        elif result.action == "o" and result.item:
            profile = vault.reload().get(result.item.value.name)
            if resolve_target(vault, args) == "app":
                launch(vault, profile, list(args.claude_args or []), launch_args_for(vault, args))
            else:
                launch_app(vault, profile, args)
            return 0
```

- [ ] **Step 6: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestLaunchTarget tests.test_claude_login.TestAppLaunch -v`
Expected: PASS (12 тестов)

- [ ] **Step 7: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 8: Подкоманда `app`, секции настроек, `doctor`

**Files:**
- Modify: `claude_login/cli.py`, `claude_login/commands.py`
- Test: `tests/test_claude_login.py`

- [ ] **Step 1: Написать тесты**

```python
class TestAppCommands(Base):
    def setUp(self) -> None:
        super().setUp()
        self.app_support = self.tmp / "app-support"
        self.app_support.mkdir()
        self._env("CLAUDE_LOGIN_APP_SUPPORT", str(self.app_support))

    def test_adopt_dry_run_touches_nothing(self):
        leaf = self.app_support / claude_app.SESSIONS_DIRNAME / "acct" / "org"
        leaf.mkdir(parents=True)
        (leaf / "local_one.json").write_text("{}")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=True, yes=True))
        self.assertFalse(leaf.is_symlink())

    def test_adopt_moves_chats_into_the_pool(self):
        leaf = self.app_support / claude_app.SESSIONS_DIRNAME / "acct" / "org"
        leaf.mkdir(parents=True)
        (leaf / "local_one.json").write_text("{}")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertTrue(leaf.is_symlink())

    def test_adopt_refuses_while_the_app_is_running(self):
        self._patch(claude_app, "running_pids", lambda: [123])
        with self.assertRaises(ClaudeAppError):
            commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))

    def test_status_runs_without_profiles(self):
        self.assertEqual(commands.cmd_app_status(self.vault, argparse.Namespace()), 0)

    def test_relink_repairs_a_missing_link(self):
        (self.app_support / "claude-code").mkdir()
        profile = self.add(email="one@example.com")
        commands.cmd_app_relink(self.vault, argparse.Namespace(name=None, force=False))
        data_dir = Path(self.vault.app_data_dir_for(profile))
        self.assertTrue((data_dir / "claude-code").is_symlink())

    def test_app_subcommand_dispatches(self):
        args = cli.build_parser().parse_args(["app", "adopt", "--dry-run"])
        self.assertIs(args.func, commands.cmd_app_adopt)
        self.assertTrue(args.dry_run)


class TestSettingsSections(Base):
    def test_target_row_cycles_through_the_targets(self):
        self.assertEqual(commands.cycle_target("cli"), "app")
        self.assertEqual(commands.cycle_target("app"), "ask")
        self.assertEqual(commands.cycle_target("ask"), "cli")

    def test_section_rows_describe_the_current_state(self):
        rows = commands._settings_sections(self.vault)
        self.assertEqual(len(rows), 3)
        self.assertIn("CLI", rows[0].cells[1])

    def test_app_rows_reflect_the_toggles(self):
        with self.vault.locked():
            self.vault.set_app_sessions_shared(False)
            self.vault.save()
        rows = commands._app_settings_items(self.vault.reload())
        self.assertIn("off", rows[1].cells[2])
```

- [ ] **Step 2: Прогнать, убедиться что падает**

Run: `python3 -m unittest tests.test_claude_login.TestAppCommands tests.test_claude_login.TestSettingsSections -v`
Expected: FAIL — `AttributeError: module 'claude_login.commands' has no attribute 'cmd_app_adopt'`

- [ ] **Step 3: Реализовать подкоманды в `commands.py`**

```python
APP_STATUS_HEADERS = ("ACCOUNT", "APP LOGIN", "CHATS", "DATA DIR")


def _app_profiles(vault: Vault) -> list[Profile]:
    return vault.profiles


def cmd_app_status(vault: Vault, args) -> int:
    """Show, per account, whether the app is signed in and where its data lives."""
    bootstrap(vault)
    if not claude_app.available():
        ui.warn(f"the Claude app was not found at {claude_app.app_bundle()}")
    rows = []
    for profile in _app_profiles(vault):
        data_dir = vault.app_data_dir_for(profile)
        status = claude_app.app_status(data_dir)
        pooled = all(
            leaf.is_symlink() for leaf in claude_app.session_leaf_dirs(data_dir)
        ) and bool(claude_app.session_leaf_dirs(data_dir))
        badge = {
            "signed-in": ui.paint("signed in", "green"),
            "logged-out": ui.paint("not signed in", "yellow"),
            "missing": ui.paint("no profile yet", "grey"),
        }[status.state]
        rows.append(
            [
                profile.display,
                badge,
                ui.paint("shared", "green") if pooled else ui.paint("private", "grey"),
                ui.paint(data_dir, "grey"),
            ]
        )
    if not rows:
        ui.info("No accounts yet. Add one with `claude-login add`.")
        return 0
    print(ui.render_table(list(APP_STATUS_HEADERS), rows))
    return 0


def cmd_app_adopt(vault: Vault, args) -> int:
    """Fold every account's existing app chats into the shared pool."""
    bootstrap(vault)
    if claude_app.running_pids():
        raise ClaudeAppError(
            "the Claude app is running — quit it first so its chats can be moved safely"
        )
    dry_run = getattr(args, "dry_run", False)
    total = store.PoolPlan()
    for profile in _app_profiles(vault):
        data_dir = vault.app_data_dir_for(profile)
        if not Path(data_dir).is_dir():
            continue
        with vault.locked():
            plan = vault.wire_session_pool(data_dir, dry_run=dry_run)
            vault.save()
        total.moved += plan.moved
        total.linked += plan.linked
        total.collisions += plan.collisions
        total.backup = total.backup or plan.backup
        if plan.linked:
            ui.info(f"{profile.display}: {plan.moved} chat(s) from {plan.linked} directory(ies)")
    if not total.linked:
        ui.success("nothing to adopt — chats are already shared")
        return 0
    if total.backup:
        ui.note(f"  backup: {total.backup}")
    if total.collisions:
        ui.note(f"  {total.collisions} name clash(es); the newer chat was kept")
    if dry_run:
        ui.note("  (dry run — nothing was touched)")
        return 0
    ui.success(f"pooled {total.moved} chat(s) — every account now shows the same Recents")
    return 0


def cmd_app_relink(vault: Vault, args) -> int:
    """Recreate the app's shared symlinks and pool links."""
    bootstrap(vault)
    targets = [vault.resolve(args.name)] if args.name else _app_profiles(vault)
    stuck = False
    for profile in targets:
        data_dir = vault.app_data_dir_for(profile)
        if profile.is_default and not Path(data_dir).is_dir():
            continue
        with vault.locked():
            linked, conflicts = vault.link_app_shared(profile, repair=args.force)
            if vault.app_sessions_shared and not claude_app.running_pids():
                vault.wire_session_pool(data_dir)
            vault.save()
        ui.info(f"{profile.display}: {', '.join(linked) if linked else 'already up to date'}")
        if conflicts:
            stuck = True
            ui.warn(f"  {profile.display} has its own copy of: {', '.join(conflicts)}")
    if stuck:
        ui.note("  `claude-login app relink --force` moves those copies into <profile>/.shadowed/")
    return 0
```

- [ ] **Step 4: Секции настроек в `commands.py`**

Заменить `cmd_settings` на верхний экран и вынести старый в `cmd_settings_flags`:

```python
SETTINGS_SECTIONS = ("target", "flags", "app")


def cycle_target(current: str) -> str:
    order = store.LAUNCH_TARGETS
    return order[(order.index(current) + 1) % len(order)] if current in order else order[0]


def _target_label(target: str) -> str:
    return {"cli": "CLI", "app": "Claude Code App", "ask": "ask every time"}[target]


def _settings_sections(vault: Vault) -> list[picker.Item]:
    vault.reload()
    flags = vault.launch_args
    app_bits = [
        "per-account" if vault.app_per_account else "one shared profile",
        "chats shared" if vault.app_sessions_shared else "chats private",
        f"{len(vault.app_shared)} shared entries",
    ]
    return [
        picker.Item(cells=["Launch target", ui.paint(_target_label(vault.launch_target), "green")]),
        picker.Item(
            cells=[
                "Launch flags (CLI)",
                ui.paint(" ".join(flags) or "none", "grey" if not flags else "cyan"),
            ]
        ),
        picker.Item(cells=["Claude Code App", ui.paint(" · ".join(app_bits), "grey")]),
    ]


def cmd_settings(vault: Vault, args=None) -> int:
    """Top-level settings: launch target, CLI flags, app profile."""
    bootstrap(vault)
    selected = 0

    def on_select(index: int) -> bool:
        if SETTINGS_SECTIONS[index] != "target":
            return False
        with vault.locked():
            vault.set_launch_target(cycle_target(vault.launch_target))
            vault.save()
        return True

    while True:
        result = picker.pick(
            lambda: _settings_sections(vault),
            title=lambda: ui.paint("Settings", "bold"),
            headers=("SETTING", "VALUE"),
            initial=selected,
            enter_label="open",
            quit_label="back",
            on_select=on_select,
        )
        if result.action == picker.CANCEL:
            return 0
        if result.index is not None:
            selected = result.index
        if result.action == "select":
            if SETTINGS_SECTIONS[selected] == "flags":
                cmd_settings_flags(vault)
            elif SETTINGS_SECTIONS[selected] == "app":
                cmd_settings_app(vault)
```

Существующее тело `cmd_settings` переименовать в `cmd_settings_flags` без изменений. Новый экран приложения:

```python
#: (kind, label) per app-settings row, in display order.
_APP_SETTINGS_ROWS = (
    ("per-account", "Per-account app profile"),
    ("sessions", "Share chats & projects"),
    ("shared", "Shared entries"),
    ("env", "Launch env"),
    ("path", "App path"),
)


def _app_settings_items(vault: Vault) -> list[picker.Item]:
    vault.reload()
    on, off = ui.paint("on", "green"), ui.paint("off", "grey")
    values = {
        "per-account": on if vault.app_per_account else off,
        "sessions": on if vault.app_sessions_shared else off,
        "shared": ui.paint(f"{len(vault.app_shared)} entries", "grey"),
        "env": ui.paint(f"{len(vault.app_env)} variable(s)", "grey"),
        "path": ui.paint(claude_app.app_bundle(), "grey"),
    }
    hints = {"shared": ui.paint("edit", "grey"), "env": ui.paint("edit", "grey")}
    return [
        picker.Item(cells=[label, values[kind], hints.get(kind, "")])
        for kind, label in _APP_SETTINGS_ROWS
    ]


def cmd_settings_app(vault: Vault, args=None) -> int:
    """Toggles for how the desktop app is launched and what it shares."""
    bootstrap(vault)
    selected = 0

    def on_select(index: int) -> bool:
        kind = _APP_SETTINGS_ROWS[index][0]
        if kind in ("shared", "env", "path"):
            return False
        with vault.locked():
            if kind == "per-account":
                vault.set_app_per_account(not vault.app_per_account)
            else:
                sharing = not vault.app_sessions_shared
                vault.set_app_sessions_shared(sharing)
                if not sharing:
                    for profile in vault.profiles:
                        vault.unwire_session_pool(vault.app_data_dir_for(profile))
            vault.save()
        return True

    while True:
        result = picker.pick(
            lambda: _app_settings_items(vault),
            title=lambda: "\n".join(
                [
                    ui.paint("Settings — Claude Code App", "bold"),
                    "",
                    ui.paint(
                        "claude-login launches the app under the selected account; "
                        "chats stay shared between accounts.",
                        "grey",
                    ),
                ]
            ),
            headers=("SETTING", "VALUE", ""),
            initial=selected,
            enter_label="change",
            quit_label="back",
            on_select=on_select,
        )
        if result.action == picker.CANCEL:
            return 0
        if result.index is not None:
            selected = result.index
        if result.action != "select":
            continue
        kind = _APP_SETTINGS_ROWS[selected][0]
        if kind == "shared":
            answer = ui.ask("Shared entries:", default=" ".join(vault.app_shared))
            if answer is not None:
                with vault.locked():
                    vault.set_app_shared(answer.split())
                    vault.save()
        elif kind == "env":
            current = " ".join(f"{k}={v}" for k, v in vault.app_env.items())
            answer = ui.ask("Launch env (KEY=VALUE ...):", default=current)
            if answer is not None:
                with vault.locked():
                    vault.set_app_env(_parse_env(answer))
                    vault.save()
        elif kind == "path":
            ui.note(f"  set CLAUDE_LOGIN_APP_PATH to point at another bundle")


def _parse_env(text: str) -> dict[str, str]:
    """``FOO=bar BAZ=qux`` → dict, ignoring anything without an ``=``."""
    env = {}
    for token in text.split():
        key, sep, value = token.partition("=")
        if sep and key:
            env[key] = value
    return env
```

- [ ] **Step 5: Подкоманда в `cli.py`**

Добавить `"app"` в `_COMMANDS`, и после `p_settings`:

```python
    p_app = sub.add_parser("app", help="manage the Claude desktop app profiles")
    app_sub = p_app.add_subparsers(dest="app_command", metavar="<action>")
    p_app.set_defaults(func=commands.cmd_app_status)

    p_app_status = app_sub.add_parser("status", help="show app login state per account")
    p_app_status.set_defaults(func=commands.cmd_app_status)

    p_app_adopt = app_sub.add_parser(
        "adopt", help="fold existing app chats into the shared pool"
    )
    p_app_adopt.add_argument("--dry-run", "-n", action="store_true", help="only show what would move")
    p_app_adopt.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p_app_adopt.set_defaults(func=commands.cmd_app_adopt)

    p_app_relink = app_sub.add_parser("relink", help="recreate the app's shared links")
    p_app_relink.add_argument("name", nargs="?", help="default: every account")
    p_app_relink.add_argument(
        "--force", "-f", action="store_true", help="move diverged copies into .shadowed/"
    )
    p_app_relink.set_defaults(func=commands.cmd_app_relink)
```

- [ ] **Step 6: App-блок в `cmd_doctor`**

Перед итоговым `if problems:` добавить:

```python
    ui.info("")
    ui.info(ui.paint("Claude desktop app", "bold"))
    if not claude_app.available():
        ui.info(f"  bundle          {ui.paint('not found', 'yellow')} at {claude_app.app_bundle()}")
    else:
        ui.info(f"  bundle          {claude_app.app_bundle()}")
    ui.info(f"  support dir     {claude_app.default_app_support_dir()}")
    running = claude_app.running_pids()
    if running:
        ui.info(f"  running         {len(running)} instance(s): {', '.join(map(str, running))}")
    for profile in vault.profiles:
        data_dir = vault.app_data_dir_for(profile)
        status = claude_app.app_status(data_dir)
        ui.info(f"  {ui.paint(profile.display, 'bold')}  {status.state}")
        ui.info(f"    data dir      {data_dir}")
        leaves = claude_app.session_leaf_dirs(data_dir)
        unpooled = [str(leaf) for leaf in leaves if not leaf.is_symlink()]
        if unpooled:
            problems += 1
            ui.info(
                f"    {ui.paint('not pooled', 'yellow')}    {len(unpooled)} chat directory(ies)"
                "  (fix with `claude-login app adopt`)"
            )
        if status.account_uuid and profile.account_uuid and status.account_uuid != profile.account_uuid:
            problems += 1
            ui.info(
                f"    {ui.paint('wrong account', 'yellow')} app says {status.account_uuid}"
            )
        missing, diverged = vault.app_shared_conflicts(profile)
        if missing:
            ui.info(f"    unlinked      {', '.join(missing)}  (`claude-login app relink`)")
        if diverged:
            problems += 1
            ui.info(f"    diverged      {', '.join(diverged)}  (`claude-login app relink --force`)")
```

- [ ] **Step 7: Прогнать**

Run: `python3 -m unittest tests.test_claude_login.TestAppCommands tests.test_claude_login.TestSettingsSections -v`
Expected: PASS (9 тестов)

- [ ] **Step 8: Контрольная точка**

Run: `python3 -m unittest discover -s tests` → `OK`

---

### Task 9: Документация

**Files:**
- Modify: `README.md`, `AGENTS.md`

- [ ] **Step 1: README — раздел про приложение**

После раздела «Аргументы запуска» добавить раздел «Claude Code App»: зачем (чаты не пропадают при смене аккаунта), как (`CLAUDE_USER_DATA_DIR` на аккаунт, общий пул), команды (`--app`, `app status`, `app adopt`, `app relink`), что общее и что нет, и что логин в приложении отдельный от CLI. Обновить блок команд и таблицу переменных окружения (`CLAUDE_LOGIN_APP_PATH`, `CLAUDE_LOGIN_APP_SUPPORT`).

- [ ] **Step 2: AGENTS.md — перевести разведку в описание реализованного**

Раздел «Claude Desktop App — разведка под будущую работу» переписать как описание работающего механизма: убрать «это разведка, а не реализация», добавить `claude_app.py` в таблицу модулей, дописать инварианты (`config.json` никогда не общий; пул трогать только при закрытом приложении; `CLAUDE_CONFIG_DIR` из окружения приложения убирается) и ловушки (ленивая адопция; каталог сессий появляется только после логина).

- [ ] **Step 3: Финальный прогон**

Run: `python3 -m unittest discover -s tests`
Expected: `OK`, ~155 тестов.

---

## Self-review

**Покрытие спеки.** Профиль данных на аккаунт — Task 3, 4, 7. Пул и миграция с бэкапом — Task 5, 8. `orgUuid` — Task 6 (используется в ленивой адопции: каталог создаёт приложение, поэтому явное создание листа по uuid не требуется — уточнение против спеки). Цель запуска и клавиша `o` — Task 7. Настройки — Task 8. Статус логина и предупреждение о чужом аккаунте — Task 2, 7, 8. Continue — Task 2 (`last_session`) и печать в Task 7. `doctor` — Task 8. Документация — Task 9. Не покрыто сознательно: автооткрытие последнего чата через deep link — надстройка, отложена до выяснения формы ссылки; в плане её нет.

**Типы и имена.** `PoolPlan` (`moved`, `linked`, `collisions`, `backup`) объявлен в Task 5 и используется в Task 7 и 8. `AppStatus` (`state`, `account_uuid`, `signed_in`) — Task 1, используется в 2, 7, 8. `claude_app.SESSIONS_DIRNAME`/`AGENT_SESSIONS_DIRNAME` — Task 1, используются в 3, 5, 8. `vault.app_data_dir_for` — Task 3, используется в 4, 5, 7, 8. `link_app_shared`/`_link_entries`/`_entry_conflicts` — Task 4. `wire_session_pool`/`unwire_session_pool` — Task 5, второй используется в Task 8. `resolve_target`/`launch_app`/`prepare_app_profile` — Task 7, используются в `cmd_use` и `interactive`.

**Placeholder-скан.** Код есть в каждом шаге, который меняет код; Task 9 — единственный текстовый, и там перечислено конкретно, что писать.
