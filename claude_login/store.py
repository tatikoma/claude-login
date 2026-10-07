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

from . import claude_app, claude_cli, ui
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

#: Launch targets `claude-login` knows how to hand a session over to.
LAUNCH_TARGETS = ("cli", "app", "ask")

#: Where the shared pool of app chats lives, relative to the vault root.  The
#: pool keeps one directory per organisation uuid, because that is the last
#: component of the path the app writes to and the app insists on creating that
#: one itself (``claude_app.rejects_leaf``).
APP_SHARED_DIRNAME = "app-shared"
#: Only the Code tab's index is pooled.  ``local-agent-mode-sessions`` looks
#: similar and is deliberately left alone: its leaf is not an index but a whole
#: workspace (``cowork_plugins``, ``backups``, ``rpm``, its own ``.claude.json``,
#: nested per-account directories), and its first level is not always an account
#: uuid — ``skills-plugin`` lives there too.  Merging that across accounts mixes
#: state that has nothing to do with chat history.
POOL_DIRNAMES = {claude_app.SESSIONS_DIRNAME: "ccd-sessions"}


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
    #: ``CLAUDE_USER_DATA_DIR`` for the desktop app; filled in on first app launch.
    app_data_dir: Optional[str] = None
    #: Cached organisation uuid — half of the key the app names its chat
    #: directory with. Only the API knows it, so it is worth remembering.
    org_uuid: Optional[str] = None

    @property
    def is_default(self) -> bool:
        """True for the machine-wide ``~/.claude`` login (no env override)."""
        return self.config_dir is None

    @property
    def display(self) -> str:
        """Accounts are identified by their email; the name is just the directory.

        The machine-wide login is marked: a dedicated profile can hold the very
        same account, and two rows that read identically get the wrong one
        picked — with the wrong app window behind it.
        """
        if self.email and self.is_default:
            return f"{self.email} (default)"
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
            "appDataDir": self.app_data_dir,
            "orgUuid": self.org_uuid,
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
            app_data_dir=data.get("appDataDir"),
            org_uuid=data.get("orgUuid"),
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


@dataclass
class PoolPlan:
    """What wiring the app's chat pool did — or would do, for a dry run."""

    moved: int = 0
    linked: int = 0
    collisions: int = 0
    backup: Optional[str] = None
    #: Links created ahead of the app, before it had a directory of its own.
    prelinked: int = 0
    #: Leaves folded in while the app had them open, copy-then-swap.
    live: int = 0
    #: Leaves left alone because we could not tell what a live window was using.
    skipped: int = 0
    #: Chats parked because their transcript is no longer on disk.
    orphans: int = 0
    #: Entries left in place because they are not chat files, so not ours to move.
    unmoved: list[str] = field(default_factory=list)


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
        # App support stays off the beaten path until the user picks it: a fresh
        # install keeps launching the CLI, exactly as launchArgs stays empty.
        data.setdefault("launchTarget", "cli")
        data.setdefault("appShared", list(claude_app.DEFAULT_APP_SHARED))
        data.setdefault("appEnv", {})
        data.setdefault("appPerAccount", True)
        # An absent account list means "every account"; an empty one means none.
        # The old boolean only had the second meaning worth keeping.
        legacy = data.pop("appSessionsShared", None)
        if legacy is False:
            data.setdefault("appSharedAccounts", [])
        self._data = data
        self._loaded = True
        return self

    @property
    def launch_args(self) -> list[str]:
        value = self.load()._data.get("launchArgs")
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_launch_args(self, args: list[str]) -> None:
        self._data["launchArgs"] = list(args)

    # -- app settings -------------------------------------------------------

    @property
    def launch_target(self) -> str:
        value = self.load()._data.get("launchTarget")
        return value if value in LAUNCH_TARGETS else "cli"

    def set_launch_target(self, target: str) -> None:
        if target not in LAUNCH_TARGETS:
            raise UsageError(
                f"unknown launch target {target!r} — pick one of {', '.join(LAUNCH_TARGETS)}"
            )
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
        return {str(key): str(item) for key, item in value.items()}

    def set_app_env(self, env: dict[str, str]) -> None:
        self._data["appEnv"] = dict(env)

    def _account_list(self, key: str) -> Optional[list[str]]:
        """``None`` when the key is absent, which everywhere means "all of them".

        Keeping absence distinct from an empty list is what lets a fresh install
        do the obvious thing while still allowing "nobody" to be chosen.
        """
        value = self.load()._data.get(key)
        return [str(item) for item in value] if isinstance(value, list) else None

    @property
    def app_open_accounts(self) -> Optional[list[str]]:
        return self._account_list("appOpenAccounts")

    def set_app_open_accounts(self, names: Optional[list[str]]) -> None:
        if names is None:
            self._data.pop("appOpenAccounts", None)
        else:
            self._data["appOpenAccounts"] = list(names)

    @property
    def app_shared_accounts(self) -> Optional[list[str]]:
        return self._account_list("appSharedAccounts")

    def set_app_shared_accounts(self, names: Optional[list[str]]) -> None:
        if names is None:
            self._data.pop("appSharedAccounts", None)
        else:
            self._data["appSharedAccounts"] = list(names)

    def shares_chats(self, profile: Profile) -> bool:
        selected = self.app_shared_accounts
        return True if selected is None else profile.name in selected

    @property
    def sharing_enabled(self) -> bool:
        """False only when the user has unticked every account."""
        selected = self.app_shared_accounts
        return selected is None or bool(selected)

    def sharing_account_uuids(self) -> Optional[set[str]]:
        """Account uuids allowed into the pool; ``None`` means no restriction.

        Chat directories are named by account uuid, so membership has to be
        answered in those terms — a data directory can hold leaves belonging to
        several accounts, and an unticked one stays private even there.
        """
        selected = self.app_shared_accounts
        if selected is None:
            return None
        chosen = set(selected)
        return {p.account_uuid for p in self.profiles if p.name in chosen and p.account_uuid}

    @property
    def app_per_account(self) -> bool:
        return bool(self.load()._data.get("appPerAccount", True))

    def set_app_per_account(self, value: bool) -> None:
        self._data["appPerAccount"] = bool(value)

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

    def app_dir_for(self, name: str) -> Path:
        return self.profiles_dir / name / "app-data"

    def app_data_dir_for(self, profile: Profile) -> str:
        """Which user-data directory the app should run against for this profile.

        The machine-wide directory plays the part ``~/.claude`` plays for the
        CLI: it belongs to the ``default`` profile and is never moved.
        """
        if profile.is_default or not self.app_per_account:
            return claude_app.default_app_support_dir()
        return profile.app_data_dir or str(self.app_dir_for(profile.name))

    def pool_dir(self, kind: str) -> Path:
        """The pool's root; the chats themselves sit one level down, per organisation."""
        return self.root / APP_SHARED_DIRNAME / POOL_DIRNAMES[kind]

    def app_data_paths(self) -> list[str]:
        """Every app user-data directory in play, machine-wide one first."""
        paths = [claude_app.default_app_support_dir()]
        paths += [self.app_data_dir_for(profile) for profile in self.profiles]
        seen: set[str] = set()
        unique: list[str] = []
        for path in paths:
            key = os.path.normpath(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

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

    def link_app_shared(
        self, profile: Profile, *, repair: bool = False
    ) -> tuple[list[str], list[str]]:
        """The same idea for the app: link its heavy, account-neutral entries.

        ``config.json`` is never in the list — it carries the account's token,
        which is exactly what has to stay private to each profile.
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

    def shared_conflicts(self, profile: Profile) -> tuple[list[str], list[str]]:
        """Inspect shared entries without touching anything: (missing, diverged)."""
        if profile.is_default or not profile.config_dir:
            return [], []
        return self._entry_conflicts(
            Path(claude_cli.default_config_dir()), Path(profile.config_dir), self.shared
        )

    def app_shared_conflicts(self, profile: Profile) -> tuple[list[str], list[str]]:
        """The same inspection for the app's shared entries."""
        if profile.is_default or not self.app_per_account:
            return [], []
        return self._entry_conflicts(
            Path(claude_app.default_app_support_dir()),
            Path(self.app_data_dir_for(profile)),
            self.app_shared,
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

    # -- the app's shared chat pool -----------------------------------------

    def wire_session_pool(
        self,
        data_dir: str,
        *,
        dry_run: bool = False,
        backup: bool = True,
        live_account: Optional[str] = None,
        sharing: Optional[set[str]] = None,
    ) -> PoolPlan:
        """Point every ``<accountUuid>`` chat directory at one shared pool.

        The app looks its chats up under the *current* account's uuid, which is
        why they seem to vanish after signing in as somebody else.  Linking the
        account directory onto a pool that holds one directory per organisation
        makes the same Recents list show up for every account of that
        organisation, whichever one is signed in.

        The link sits on the account and not on the ``<orgUuid>`` leaf below it
        because the app refuses to write through a symlinked leaf — see
        ``claude_app.rejects_leaf``.  Which also means accounts of *different*
        organisations cannot be merged at all: their last path component differs,
        and only the app may create it.

        A directory only exists once the app has resolved an account, so this is
        the lazy path too: call it again later and whatever the app created in
        the meantime is folded in.

        ``live_account`` names the account a window is signed in as right now.
        Its directory is the only one the app writes to, so that one is folded in
        with the copy-then-swap dance rather than moved; everything else in the
        same data directory is dormant and moves outright.

        ``sharing`` limits the pool to a set of account uuids; ``None`` lets all
        of them in.  The filter is per account directory because one data
        directory can hold the directories of several accounts.
        """
        plan = PoolPlan()
        if not dry_run:
            self.migrate_flat_pool()
        for kind in POOL_DIRNAMES:
            agent = kind == claude_app.AGENT_SESSIONS_DIRNAME
            pending = [
                account
                for account in claude_app.session_account_dirs(data_dir, agent=agent)
                if sharing is None or account.name in sharing
            ]
            if not pending:
                continue
            if backup and not dry_run and plan.backup is None:
                plan.backup = self._backup_sessions(data_dir)
            pool = self.pool_dir(kind)
            if not dry_run:
                pool.mkdir(mode=0o700, parents=True, exist_ok=True)
            for account in pending:
                live = bool(live_account) and account.name == live_account
                if dry_run:
                    moved, collisions, _ = self._drain_account(
                        account, pool, dry_run=True
                    )
                elif live:
                    moved, collisions = self._drain_live_into_pool(account, pool)
                    plan.live += 1
                else:
                    moved, collisions, leftover = self._drain_account(
                        account, pool, dry_run=False
                    )
                    if leftover:
                        # Never delete what we did not move. A directory holding
                        # anything but chat files is not the index we think it
                        # is, and guessing there once cost real data.
                        plan.unmoved.extend(
                            f"{account.name}/{name}" for name in leftover
                        )
                        plan.moved += moved
                        plan.collisions += collisions
                        continue
                    if not self._retire_account(account):
                        continue
                    account.symlink_to(pool)
                plan.moved += moved
                plan.collisions += collisions
                plan.linked += 1
        return plan

    def migrate_flat_pool(self) -> int:
        """Lift a flat pool into the per-organisation layout the app now forces.

        Pools written before the app started refusing symlinked leaves are one
        directory of chat files that every account shared.  The leaf has to be a
        real directory now, so the pool keeps one per organisation — and since a
        chat file records nothing about which organisation it belongs to, while
        every account has in fact been looking at the same list until now, each
        organisation gets its own copy.  Copy first, move last: no chat is
        removed from the old place before it exists in the new one.
        """
        moved = 0
        for kind in POOL_DIRNAMES:
            pool = self.pool_dir(kind)
            if not pool.is_dir():
                continue
            stray = [entry for entry in sorted(pool.iterdir()) if entry.is_file()]
            if not stray:
                continue
            targets = [pool / org for org in self._known_orgs(kind)]
            if not targets:
                continue
            for target in targets:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            for entry in stray:
                for target in targets[1:]:
                    shutil.copy2(entry, target / entry.name)
                shutil.move(str(entry), str(targets[0] / entry.name))
                moved += 1
        return moved

    def _known_orgs(self, kind: str) -> list[str]:
        """Every organisation uuid we can name: from disk first, registry second."""
        agent = kind == claude_app.AGENT_SESSIONS_DIRNAME
        found: list[str] = []
        for data_dir in self.app_data_paths():
            for leaf in claude_app.session_leaf_dirs(data_dir, agent=agent):
                if leaf.name not in found:
                    found.append(leaf.name)
        for profile in self.profiles:
            if profile.org_uuid and profile.org_uuid not in found:
                found.append(profile.org_uuid)
        return found

    def sweep_pool(self, *, dry_run: bool = False) -> int:
        """Move chats whose transcript is gone out of the shared pool.

        The app's chat index and the transcripts it points at have different
        lifetimes: an index entry can outlive its ``~/.claude/projects`` file by
        machines and months.  Pooling such an entry puts a row in everybody's
        Recents that can only answer "session not found on disk", so it is parked
        in ``app-shared/orphans`` instead — kept, because it is still the user's
        data, just not shown.
        """
        alive = claude_cli.transcript_ids(None)
        attic = self.root / APP_SHARED_DIRNAME / "orphans"
        moved = 0
        for kind in POOL_DIRNAMES:
            for directory in claude_app.chat_dirs(self.pool_dir(kind)):
                try:
                    entries = sorted(directory.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.suffix != ".json" or not entry.name.startswith(
                        claude_app.SESSION_PREFIX
                    ):
                        continue
                    session = claude_app.read_session(entry)
                    cli_id = session.get("cliSessionId")
                    if not isinstance(cli_id, str) or cli_id in alive:
                        continue
                    moved += 1
                    if dry_run:
                        continue
                    attic.mkdir(mode=0o700, parents=True, exist_ok=True)
                    shutil.move(str(entry), str(attic / entry.name))
        return moved

    def link_session_pool(self, data_dir: str, account_uuid: str) -> int:
        """Link an account into the pool before the app has looked.

        The app makes its chat directory only once it has resolved an account —
        which happens after a login, long after we have exec'd away — so waiting
        for it to appear means a fresh profile comes up with an empty Recents
        list and no obvious moment when it starts sharing.  The account uuid is
        the whole key: the organisation directory below it is the app's to
        create, and it creates it inside the pool.

        A directory that is already real is left alone: folding its contents in
        is ``wire_session_pool``'s job, and it knows how to back them up.
        """
        if not account_uuid:
            return 0
        self.migrate_flat_pool()
        linked = 0
        for kind in POOL_DIRNAMES:
            agent = kind == claude_app.AGENT_SESSIONS_DIRNAME
            root = claude_app.sessions_root(data_dir, agent=agent)
            link = root / account_uuid
            if link.is_symlink() or link.exists():
                continue
            pool = self.pool_dir(kind)
            pool.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                link.symlink_to(pool)
            except OSError:
                continue
            linked += 1
        return linked

    def clear_agent_pool_links(self, data_dir: str) -> tuple[int, int]:
        """Drop pool links an earlier version left in the agent-mode directories.

        Agent-mode is not pooled any more — its leaf is a whole workspace, not an
        index — but the links that experiment created are still on disk, and now
        they do more than nothing: the app refuses a symlinked chat directory
        outright, so agent-mode saves nothing at all while one is in the way.

        Only links that point nowhere are removed; those still hold no data of
        their own.  One that resolves is counted and left for a human.
        """
        removed = kept = 0
        candidates = list(claude_app.session_account_links(data_dir, agent=True))
        for account in claude_app.session_account_dirs(data_dir, agent=True):
            try:
                candidates += [org for org in sorted(account.iterdir()) if org.is_symlink()]
            except OSError:
                continue
        for link in candidates:
            if link.exists():
                kept += 1
                continue
            try:
                link.unlink()
            except OSError:
                kept += 1
                continue
            removed += 1
        return removed, kept

    def known_org_uuids(self, data_dir: str, account_uuid: str) -> list[str]:
        """Organisation uuids this account already has a chat directory under.

        Cheaper and more reliable than asking the API, and it works offline: the
        name is right there on disk, whether the entry is a real directory or a
        link we made earlier.  Agent-mode counts — the two live under the same
        organisation.
        """
        found: list[str] = []
        for agent in (False, True):
            for leaf in claude_app.session_leaf_dirs(data_dir, agent=agent):
                if leaf.parent.name == account_uuid and leaf.name not in found:
                    found.append(leaf.name)
        return found

    def unwire_session_pool(self, data_dir: str) -> int:
        """Undo the wiring: hand the profile back its own copy of the chats."""
        restored = 0
        for kind in POOL_DIRNAMES:
            agent = kind == claude_app.AGENT_SESSIONS_DIRNAME
            pool = self.pool_dir(kind)
            for link in claude_app.session_account_links(data_dir, agent=agent):
                link.unlink()
                link.mkdir(mode=0o700, parents=True, exist_ok=True)
                for org in claude_app.chat_dirs(pool):
                    if org == pool:
                        continue
                    destination = link / org.name
                    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                    for entry in org.iterdir():
                        if entry.is_file():
                            shutil.copy2(entry, destination / entry.name)
                restored += 1
        return restored

    def _backup_sessions(self, data_dir: str) -> str:
        """Copy the chat directories aside before the very first move."""
        destination = self.root / APP_SHARED_DIRNAME / f".backup-{ui.now_ms()}"
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        for kind in POOL_DIRNAMES:
            source = Path(data_dir) / kind
            if source.is_dir():
                shutil.copytree(source, destination / kind, symlinks=True)
        return str(destination)

    def _drain_into_pool(
        self, leaf: Path, pool: Path, *, dry_run: bool
    ) -> tuple[int, int]:
        """Move one leaf directory's files into the pool; newer wins a clash."""
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
                if dry_run:
                    continue
                if self._is_newer(entry, target):
                    self._park_loser(target)
                    shutil.move(str(entry), str(target))
                else:
                    self._park_loser(entry)
                continue
            moved += 1
            if not dry_run:
                shutil.move(str(entry), str(target))
        return moved, collisions

    def _copy_into_pool(self, source: Path, pool: Path) -> tuple[int, int]:
        """Copy one directory's chat files into the pool; newer wins a clash."""
        copied = collisions = 0
        try:
            entries = sorted(source.iterdir())
        except OSError:
            return 0, 0
        for entry in entries:
            if not entry.is_file():
                continue
            target = pool / entry.name
            if target.exists():
                if not self._is_newer(entry, target):
                    continue
                collisions += 1
                self._park_loser(target)
            else:
                copied += 1
            try:
                shutil.copy2(entry, target)
            except OSError:
                continue
        return copied, collisions

    def _drain_account(
        self, account: Path, pool: Path, *, dry_run: bool
    ) -> tuple[int, int, list[str]]:
        """Move one account's chats into the pool, one organisation at a time.

        A stale ``<orgUuid>`` link from the layout we used before the app began
        refusing them holds nothing of its own — whatever it pointed at is in the
        pool already — so it just goes.  Anything that is not a chat file stays
        exactly where it is and comes back in the leftover list.
        """
        moved = collisions = 0
        leftover: list[str] = []
        try:
            entries = sorted(account.iterdir())
        except OSError:
            return 0, 0, []
        for org in entries:
            if org.is_symlink():
                if not dry_run:
                    org.unlink()
                continue
            if org.name.startswith(".") or claude_app.REPLACED_MARKER in org.name:
                # Our own backup from an earlier fold-in, or a dotfile. It is not
                # data to move and not a reason to refuse either: ``_retire_account``
                # carries it along instead of deleting it.
                continue
            if not org.is_dir():
                leftover.append(org.name)
                continue
            target = pool / org.name
            if not dry_run:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            grabbed, clashed = self._drain_into_pool(org, target, dry_run=dry_run)
            moved += grabbed
            collisions += clashed
            if dry_run:
                continue
            rest = sorted(p.name for p in org.iterdir())
            if rest:
                leftover.extend(f"{org.name}/{name}" for name in rest)
            else:
                org.rmdir()
        return moved, collisions, leftover

    @staticmethod
    def _retire_account(account: Path) -> bool:
        """Clear the account directory's name for the link, deleting nothing.

        Empty means the drain took everything, so the directory itself goes.
        Otherwise what is left is ours — an older ``.replaced-`` backup from a
        previous fold-in, a dotfile — and the whole directory is renamed aside
        rather than emptied: this must never remove something it did not move.
        """
        try:
            account.rmdir()
            return True
        except OSError:
            pass
        aside = account.with_name(
            f"{account.name}{claude_app.REPLACED_MARKER}{ui.now_ms()}"
        )
        try:
            account.rename(aside)
        except OSError:
            return False
        return True

    def _copy_account_into_pool(self, account: Path, pool: Path) -> tuple[int, int]:
        """``_drain_account`` without the removals, for a directory in use."""
        copied = collisions = 0
        try:
            entries = sorted(account.iterdir())
        except OSError:
            return 0, 0
        for org in entries:
            if (
                org.is_symlink()
                or not org.is_dir()
                or org.name.startswith(".")
                or claude_app.REPLACED_MARKER in org.name
            ):
                continue
            target = pool / org.name
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
            grabbed, clashed = self._copy_into_pool(org, target)
            copied += grabbed
            collisions += clashed
        return copied, collisions

    def _drain_live_into_pool(self, account: Path, pool: Path) -> tuple[int, int]:
        """Fold a directory the app is writing to *right now* into the pool.

        Copy first, then swap the directory for the symlink in two syscalls, then
        copy again to catch whatever was written in between.  Nothing is deleted:
        the original directory stays on disk next to its old place, so even a
        write that lost the race is still there to be found.

        Moving the files outright would be simpler, but it would mean telling
        somebody to quit the window they are working in — and that is a worse
        trade than a sub-millisecond race that cannot lose data.
        """
        copied, collisions = self._copy_account_into_pool(account, pool)
        aside = account.with_name(
            f"{account.name}{claude_app.REPLACED_MARKER}{ui.now_ms()}"
        )
        account.rename(aside)
        account.symlink_to(pool)
        late, more = self._copy_account_into_pool(aside, pool)
        return copied + late, collisions + more

    @staticmethod
    def _is_newer(candidate: Path, incumbent: Path) -> bool:
        def stamp(path: Path) -> int:
            recorded = claude_app.activity(claude_app.read_session(path))
            if recorded:
                return recorded
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

    def gather_config(self) -> list[str]:
        """Pull MCP servers the profiles have but the machine-wide config lacks.

        ``sync_config`` only ever flows machine-wide → profile, so a server
        installed while working inside a profile never reached the others.  This
        is the missing direction, and it only ever *adds*: a name already in the
        machine-wide config wins, and a server deleted there does not come back
        from a profile that still has it.

        Returns the names that were added.
        """
        machine = claude_cli.read_global_config(None)
        servers = dict(machine.get("mcpServers") or {})
        added: list[str] = []
        for profile in self.profiles:
            if profile.is_default or not profile.config_dir:
                continue
            found = claude_cli.read_global_config(profile.config_dir).get("mcpServers")
            if not isinstance(found, dict):
                continue
            for name, definition in found.items():
                if name not in servers:
                    servers[name] = definition
                    added.append(name)
        if added:
            self._write_machine_config({**machine, "mcpServers": servers})
        return added

    @staticmethod
    def _write_machine_config(data: dict[str, Any]) -> None:
        """Write ``~/.claude.json`` with the same atomic dance as a profile's."""
        target = claude_cli.global_config_path(None)
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
