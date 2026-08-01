"""The account vault: a registry file plus one config directory per account."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from . import claude_cli, ui
from .errors import ProfileNotFound, UsageError

REGISTRY_VERSION = 1
#: A profile name becomes a directory name, so it must not contain a separator
#: and must not start with a dash (which argparse would read as a flag). Email
#: addresses are deliberately allowed — they make perfectly good profile names.
NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}\Z")

#: Entries symlinked from ``~/.claude`` into every profile so that settings,
#: memory, commands and transcripts stay shared across accounts.
DEFAULT_SHARED = [
    "settings.json",
    "CLAUDE.md",
    "commands",
    "agents",
    "skills",
    "workflows",
    "output-styles",
    "rules",
    "hooks",
    "plugins",
    "projects",
    # Checkpoints for /rewind are keyed by session uuid and pair 1:1 with the
    # transcripts in projects/, so the two have to travel together.
    "file-history",
    "history.jsonl",
]

#: Keys copied from the machine-wide ``~/.claude.json`` into a fresh profile so
#: you do not have to redo onboarding or re-trust every folder. Deliberately
#: excludes ``oauthAccount`` and every account-scoped cache.
SEEDED_CONFIG_KEYS = [
    "hasCompletedOnboarding",
    "lastOnboardingVersion",
    "installMethod",
    "autoUpdates",
    "autoUpdatesProtectedForNative",
    "theme",
    "projects",
    "tipsHistory",
    "hasSeenTasksHint",
    "optionAsMetaKeyInstalled",
    "appleTerminalSetupInProgress",
    "appleTerminalBackupPath",
    "hasIdeOnboardingBeenShown",
    "shiftEnterKeyBindingInstalled",
    "githubRepoPaths",
    "mcpServers",
    "migrationVersion",
]

#: A refresh token closer than this to expiry gets flagged in `list`.
EXPIRY_WARNING_DAYS = 3


def vault_root() -> Path:
    override = os.environ.get("CLAUDE_ACCOUNTS_HOME")
    return Path(override).expanduser() if override else Path.home() / ".claude-accounts"


def _same_content(target: Path, source: Path) -> bool:
    """True when a private copy is byte-identical to the shared original."""
    if target.is_dir() or source.is_dir():
        return False
    try:
        return target.read_bytes() == source.read_bytes()
    except OSError:
        return False


def validate_name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise UsageError(
            f"invalid profile name {name!r} — start with a letter or digit, then "
            "letters, digits and . _ - @ + (max 64 characters)"
        )
    return name


# --- model -----------------------------------------------------------------


@dataclass
class Profile:
    name: str
    config_dir: Optional[str] = None
    email: Optional[str] = None
    account_uuid: Optional[str] = None
    organization_name: Optional[str] = None
    subscription_type: Optional[str] = None
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    extra_args: list[str] = field(default_factory=list)

    @property
    def is_default(self) -> bool:
        """True for the machine-wide ``~/.claude`` login (no env override)."""
        return self.config_dir is None

    @property
    def display(self) -> str:
        """Accounts are identified by their email; the name is just the directory."""
        return self.email or self.name

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configDir": self.config_dir,
            "email": self.email,
            "accountUuid": self.account_uuid,
            "organizationName": self.organization_name,
            "subscriptionType": self.subscription_type,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "extraArgs": self.extra_args,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            name=data["name"],
            config_dir=data.get("configDir"),
            email=data.get("email"),
            account_uuid=data.get("accountUuid"),
            organization_name=data.get("organizationName"),
            subscription_type=data.get("subscriptionType"),
            created_at=data.get("createdAt"),
            last_used_at=data.get("lastUsedAt"),
            extra_args=list(data.get("extraArgs") or []),
        )


@dataclass
class Status:
    """Live health of a profile, derived from its credentials on demand."""

    state: str  # ok | expiring | expired | logged-out | missing
    expires_at: Optional[int] = None
    refresh_expires_at: Optional[int] = None
    subscription_type: Optional[str] = None

    @property
    def usable(self) -> bool:
        return self.state in ("ok", "expiring")

    @property
    def prunable(self) -> bool:
        return self.state in ("expired", "logged-out", "missing")


# --- vault -----------------------------------------------------------------


class Vault:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or vault_root()
        self.registry_path = self.root / "accounts.json"
        self.profiles_dir = self.root / "profiles"
        self.staging_dir = self.root / ".login"
        self._data: dict[str, Any] = {}
        self._loaded = False

    # -- persistence --------------------------------------------------------

    def ensure_dirs(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.profiles_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def reload(self) -> "Vault":
        self._loaded = False
        return self.load()

    def setting(self, key: str, default: Any = None) -> Any:
        return self.load()._data.get(key, default)

    def load(self) -> "Vault":
        if self._loaded:
            return self
        try:
            with self.registry_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", REGISTRY_VERSION)
        data.setdefault("profiles", [])
        data.setdefault("shared", list(DEFAULT_SHARED))
        data.setdefault("lastUsed", None)
        data.setdefault("defaultHidden", False)
        # No launch flags unless the user configures some (Settings, or `flags`).
        data.setdefault("launchArgs", [])
        self._data = data
        self._loaded = True
        return self

    @property
    def launch_args(self) -> list[str]:
        value = self.load()._data.get("launchArgs")
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_launch_args(self, args: list[str]) -> None:
        self._data["launchArgs"] = list(args)

    def save(self) -> None:
        self.ensure_dirs()
        payload = json.dumps(self._data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".accounts-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.registry_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextmanager
    def locked(self) -> Iterator["Vault"]:
        """Serialise read-modify-write cycles against concurrent invocations."""
        self.ensure_dirs()
        lock_path = self.root / ".registry.lock"
        with open(lock_path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                self._loaded = False
                self.load()
                yield self
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # -- accessors ----------------------------------------------------------

    @property
    def shared(self) -> list[str]:
        return list(self.load()._data.get("shared") or [])

    @property
    def last_used(self) -> Optional[str]:
        return self.load()._data.get("lastUsed")

    @property
    def profiles(self) -> list[Profile]:
        return [Profile.from_json(p) for p in self.load()._data.get("profiles", [])]

    def get(self, name: str) -> Profile:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        raise ProfileNotFound(name)

    def find(self, name: str) -> Optional[Profile]:
        try:
            return self.get(name)
        except ProfileNotFound:
            return None

    def resolve(self, name: str) -> Profile:
        """Look a profile up by exact name, then by unique prefix or label."""
        exact = self.find(name)
        if exact:
            return exact
        lowered = name.lower()
        matches = [
            p
            for p in self.profiles
            if p.name.lower().startswith(lowered)
            or (p.email or "").lower().startswith(lowered)
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(p.name for p in matches)
            raise UsageError(f"{name!r} is ambiguous — matches {names}")
        raise ProfileNotFound(name)

    # -- mutation -----------------------------------------------------------

    def _replace(self, profiles: list[Profile]) -> None:
        self._data["profiles"] = [p.to_json() for p in profiles]

    def upsert(self, profile: Profile) -> None:
        profiles = self.profiles
        for index, existing in enumerate(profiles):
            if existing.name == profile.name:
                profiles[index] = profile
                break
        else:
            profiles.append(profile)
        self._replace(profiles)

    def delete(self, name: str) -> None:
        profiles = [p for p in self.profiles if p.name != name]
        self._replace(profiles)
        if self._data.get("lastUsed") == name:
            self._data["lastUsed"] = None

    def rename(self, old: str, new: str) -> Profile:
        validate_name(new)
        if self.find(new):
            raise UsageError(f"a profile named {new!r} already exists")
        profiles = self.profiles
        for index, profile in enumerate(profiles):
            if profile.name == old:
                profile.name = new
                profiles[index] = profile
                break
        else:
            raise ProfileNotFound(old)
        self._replace(profiles)
        if self._data.get("lastUsed") == old:
            self._data["lastUsed"] = new
        return profiles[index]

    def touch(self, name: str) -> None:
        profiles = self.profiles
        for profile in profiles:
            if profile.name == name:
                profile.last_used_at = ui.iso_now()
        self._replace(profiles)
        self._data["lastUsed"] = name

    def hide_default(self) -> None:
        self._data["defaultHidden"] = True

    @property
    def default_hidden(self) -> bool:
        return bool(self.load()._data.get("defaultHidden"))

    # -- profile directories -------------------------------------------------

    def dir_for(self, name: str) -> Path:
        return self.profiles_dir / name

    def create_profile_dir(self, name: str) -> str:
        path = self.dir_for(name)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(path)

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
        source_root = Path(claude_cli.default_config_dir())
        target_root = Path(profile.config_dir)
        target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        linked: list[str] = []
        conflicts: list[str] = []
        for entry in self.shared:
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

    def shared_conflicts(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Inspect shared entries without touching anything: (missing, diverged)."""
        if profile.is_default or not profile.config_dir:
            return [], []
        source_root = Path(claude_cli.default_config_dir())
        target_root = Path(profile.config_dir)
        missing, diverged = [], []
        for entry in self.shared:
            source = source_root / entry
            target = target_root / entry
            if not source.exists():
                continue
            if not target.exists() and not target.is_symlink():
                missing.append(entry)
            elif not target.is_symlink() and not _same_content(target, source):
                diverged.append(entry)
        return missing, diverged

    @staticmethod
    def _shadow(target_root: Path, entry: str, target: Path) -> Path:
        """Move a diverged copy aside instead of deleting the user's data."""
        attic = target_root / ".shadowed"
        attic.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = attic / f"{entry}.{ui.now_ms()}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
        return destination

    def seed_config(self, profile: Profile) -> bool:
        """Copy onboarding/trust settings from the machine-wide config.

        Only fills in what the profile does not have yet — never overwrites the
        account block or anything the login just wrote.
        """
        if profile.is_default or not profile.config_dir:
            return False
        source = claude_cli.read_global_config(None)
        if not source:
            return False
        current = claude_cli.read_global_config(profile.config_dir)
        seeded = {key: source[key] for key in SEEDED_CONFIG_KEYS if key in source}
        if not seeded:
            return False
        self._write_global_config(profile, {**seeded, **current})
        return True

    def sync_config(self, profile: Profile) -> list[str]:
        """Push later changes from ``~/.claude.json`` into an existing profile.

        ``.claude.json`` holds both shared things (trusted folders, MCP servers,
        onboarding state) and the account itself, so it cannot be a symlink.
        This re-copies the shared keys on demand.  ``projects`` is merged rather
        than replaced, so a profile keeps its own per-project prompt history and
        only gains folders it had not seen.

        Returns the list of keys that changed.
        """
        if profile.is_default or not profile.config_dir:
            return []
        source = claude_cli.read_global_config(None)
        if not source:
            return []
        current = claude_cli.read_global_config(profile.config_dir)
        updated = dict(current)
        changed: list[str] = []
        for key in SEEDED_CONFIG_KEYS:
            if key not in source:
                continue
            if key == "projects":
                merged = {**(source[key] or {}), **(current.get(key) or {})}
                if merged != current.get(key):
                    updated[key] = merged
                    changed.append(key)
            elif current.get(key) != source[key]:
                updated[key] = source[key]
                changed.append(key)
        if changed:
            self._write_global_config(profile, updated)
        return changed

    @staticmethod
    def _write_global_config(profile: Profile, data: dict[str, Any]) -> None:
        target = claude_cli.global_config_path(profile.config_dir)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".claude-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def move_config_dir(old_dir: str, new_dir: Path) -> None:
        """Move a config directory, carrying its Keychain item with it.

        The Keychain service name is derived from the directory path, so a plain
        rename would strand the credentials under the old name.
        """
        from . import keychain

        old_service = claude_cli.credentials_service(old_dir)
        # Only Keychain-held credentials need re-filing; a plaintext fallback
        # simply travels with the directory.
        stored = keychain.read_json(old_service)
        new_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        Path(old_dir).rename(new_dir)
        claude_cli.forget_credentials(old_dir)
        if stored is not None:
            claude_cli.write_credentials(str(new_dir), stored)
            keychain.delete(old_service)

    def remove_profile_dir(self, profile: Profile) -> bool:
        if profile.is_default or not profile.config_dir:
            return False
        path = Path(profile.config_dir)
        if not path.exists():
            return False
        # Shared entries are symlinks; rmtree does not follow them.
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()

    # -- status -------------------------------------------------------------

    def status(self, profile: Profile) -> Status:
        if not profile.is_default and profile.config_dir and not Path(profile.config_dir).is_dir():
            return Status("missing")
        tokens = claude_cli.oauth_tokens(claude_cli.read_credentials(profile.config_dir))
        if not tokens.get("accessToken"):
            return Status("logged-out")
        now = ui.now_ms()
        refresh_expiry = tokens.get("refreshTokenExpiresAt")
        state = "ok"
        if isinstance(refresh_expiry, (int, float)):
            if refresh_expiry <= now:
                state = "expired"
            elif refresh_expiry - now <= EXPIRY_WARNING_DAYS * 86_400_000:
                state = "expiring"
        return Status(
            state=state,
            expires_at=tokens.get("expiresAt"),
            refresh_expires_at=refresh_expiry,
            subscription_type=tokens.get("subscriptionType"),
        )

    def identity(self, profile: Profile) -> dict[str, Any]:
        """Read account identity out of the profile's own ``.claude.json``."""
        config = claude_cli.read_global_config(profile.config_dir)
        account = config.get("oauthAccount")
        return account if isinstance(account, dict) else {}

    def refresh_identity(self, profile: Profile) -> Profile:
        """Sync cached email/org/plan from the profile's config and credentials."""
        account = self.identity(profile)
        if account.get("emailAddress"):
            profile.email = account["emailAddress"]
        if account.get("accountUuid"):
            profile.account_uuid = account["accountUuid"]
        if account.get("organizationName"):
            profile.organization_name = account["organizationName"]
        status = self.status(profile)
        if status.subscription_type:
            profile.subscription_type = status.subscription_type
        return profile

    # -- bootstrap ----------------------------------------------------------

    def adopt_default(self) -> Optional[Profile]:
        """Register the machine-wide ``~/.claude`` login as a profile."""
        if self.default_hidden or self.find("default"):
            return None
        if not claude_cli.oauth_tokens(claude_cli.read_credentials(None)).get("accessToken"):
            return None
        profile = Profile(name="default", config_dir=None, created_at=ui.iso_now())
        self.refresh_identity(profile)
        self.upsert(profile)
        return profile
