"""Implementation of every claude-login subcommand."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import claude_app, claude_cli, keychain, picker, store, ui, usage
from .errors import ClaudeAppError, ClaudeLoginError, UsageError
from .store import Profile, Status, Vault

#: Short forms normalised before de-duplicating flags across layers.
FLAG_ALIASES = {"-c": "--continue", "-r": "--resume"}
#: Needs a transcript in the current directory or claude aborts the launch.
CONTINUE_FLAGS = {"--continue"}
#: --resume supersedes --continue; passing both makes no sense.
CONTINUE_CONFLICTS = {"--resume"}

_BADGES = {
    "ok": ("ok", ("green",)),
    "expiring": ("expiring", ("yellow",)),
    "expired": ("expired", ("red",)),
    "logged-out": ("not signed in", ("red",)),
    "missing": ("directory missing", ("red",)),
}


# --- shared helpers --------------------------------------------------------


def bootstrap(vault: Vault) -> Vault:
    """Register the machine-wide ``~/.claude`` login the first time we run."""
    vault.load()
    if vault.find("default") or vault.default_hidden:
        return vault
    with vault.locked():
        if vault.adopt_default():
            vault.save()
    return vault


def badge_for(status: Status) -> tuple[str, tuple[str, ...]]:
    text, styles = _BADGES.get(status.state, (status.state, ()))
    if status.state == "expiring" and status.refresh_expires_at:
        text = f"expires {ui.relative_ms(status.refresh_expires_at)}"
    return text, styles


def describe(profile: Profile) -> str:
    bits = [profile.email or "not signed in"]
    if profile.subscription_type:
        bits.append(profile.subscription_type)
    return " · ".join(bits)


LIST_HEADERS = (" ", "ACCOUNT", "PLAN", "5H", "WEEK", "STATUS")


def warm_credentials(profiles) -> None:
    """Read every profile's credentials up front, concurrently."""
    claude_cli.prefetch_credentials(p.config_dir for p in profiles)


def _usage_targets(vault: Vault, profiles, args) -> Optional[list]:
    """Profiles worth querying, or None when the lookup is switched off."""
    if getattr(args, "no_usage", False):
        return None
    return [p for p in profiles if vault.status(p).state != "logged-out"]


def usage_for(vault: Vault, profiles, args) -> dict:
    """Live rate-limit usage per profile; empty when disabled or offline."""
    targets = _usage_targets(vault, profiles, args)
    return usage.fetch_all(targets) if targets else {}


class _NoUsage:
    """Stand-in used when the rate-limit lookup is switched off."""

    values: dict = {}
    pending = False

    @staticmethod
    def settled() -> bool:
        return False


def background_usage(vault: Vault, profiles, args):
    targets = _usage_targets(vault, profiles, args)
    return usage.BackgroundFetch(targets) if targets else _NoUsage()


def row_for(
    vault: Vault,
    profile: Profile,
    usages: dict,
    *,
    marker: bool = True,
    pending: bool = False,
) -> list[str]:
    status = vault.status(profile)
    text, styles = badge_for(status)
    session_cell, week_cell = usage.cells(usages.get(profile.name), pending=pending)
    active = ui.paint("●", "cyan") if profile.name == vault.last_used else " "
    return [
        *([active] if marker else []),
        profile.display,
        profile.subscription_type or ui.paint("—", "grey"),
        session_cell,
        week_cell,
        ui.paint(text, *styles),
    ]


def _flag_name(token: str) -> Optional[str]:
    if not token.startswith("-"):
        return None
    name = token.split("=", 1)[0]
    return FLAG_ALIASES.get(name, name)


def _flag_names(tokens: list[str]) -> set[str]:
    return {name for name in map(_flag_name, tokens) if name}


def _flag_groups(tokens: list[str]) -> list[tuple[Optional[str], list[str]]]:
    """Split a flag list into groups so a flag and its value travel together."""
    groups: list[tuple[Optional[str], list[str]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = _flag_name(token)
        group = [token]
        if (
            name
            and "=" not in token
            and index + 1 < len(tokens)
            and not tokens[index + 1].startswith("-")
        ):
            index += 1
            group.append(tokens[index])
        groups.append((name, group))
        index += 1
    return groups


def build_claude_args(
    profile: Profile,
    passthrough: list[str],
    launch_args: list[str],
    *,
    cwd: Optional[str] = None,
) -> list[str]:
    """Configured flags, then per-profile extras, then whatever the user typed.

    A configured flag is dropped when the same flag appears further right, so
    `claude-login cto --effort low` beats an `--effort max` in the config.
    ``--continue`` is also dropped when the current directory has no transcript,
    because claude refuses to start at all in that case.
    """
    later = _flag_names(profile.extra_args) | _flag_names(passthrough)
    resume_requested = bool(later & CONTINUE_CONFLICTS)
    args: list[str] = []
    for name, group in _flag_groups(launch_args):
        if name and name in later:
            continue
        if name in CONTINUE_FLAGS:
            if resume_requested:
                continue
            if not claude_cli.has_transcript(profile.config_dir, cwd or os.getcwd()):
                continue
        args.extend(group)
    args.extend(profile.extra_args)
    args.extend(passthrough)
    return args


def launch_args_for(vault: Vault, args) -> list[str]:
    return [] if getattr(args, "no_flags", False) else vault.launch_args


# --- launch targets --------------------------------------------------------


def resolve_target(vault: Vault, args) -> str:
    """Whether this launch goes to the CLI or to the desktop app.

    An explicit flag beats the configured default; ``ask`` only asks when there
    is a terminal to ask in, and otherwise behaves like ``cli`` so scripts and
    pipes keep working.
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
    return "app" if ui.confirm("Launch the Claude app instead of the CLI?") else "cli"


def base_target(vault: Vault, args) -> str:
    """The target Enter would pick, without stopping to ask.

    ``o`` in the list flips this rather than the answer to a prompt: flipping a
    question you were just asked would be a confusing thing to offer.
    """
    if getattr(args, "app", False):
        return "app"
    if getattr(args, "cli", False):
        return "cli"
    target = vault.launch_target
    return target if target != "ask" else "cli"


def other_target(target: str) -> str:
    return "cli" if target == "app" else "app"


def dispatch(vault: Vault, profile: Profile, args, target: str) -> int:
    """Hand the profile over to whichever target was chosen."""
    if target == "app":
        launch_app(vault, profile, args)
        return 0
    passthrough = list(getattr(args, "claude_args", None) or [])
    launch(vault, profile, passthrough, launch_args_for(vault, args))
    return 0  # unreachable for the CLI: exec replaced us


def pool_guard(data_dir: str) -> tuple[bool, Optional[str]]:
    """``(safe_to_wire, account_being_written_to)`` for one data directory.

    A live window only ever writes to the chat directory of the account it is
    signed in as, so that one is folded in carefully and the rest move outright.
    When the directory is open but the signed-in account cannot be read, nothing
    is touched: guessing is the one mistake here with real data behind it.
    """
    if not claude_app.is_in_use(data_dir):
        return True, None
    account = claude_app.app_status(data_dir).account_uuid
    return (True, account) if account else (False, None)


def pending_pool_labels(vault: Vault) -> list[str]:
    """Data directories whose chats are still private, worth nagging about.

    This is the case that reads as "sharing does not work": the pool is wired but
    the chats from before it existed never moved, so the shared list is empty.
    """
    if not vault.sharing_enabled:
        return []
    pending = []
    for label, data_dir in app_data_dirs(vault):
        if claude_app.session_account_dirs(data_dir):
            pending.append(label)
    return pending


def resolve_org_uuid(vault: Vault, profile: Profile) -> Optional[str]:
    """The organisation uuid that, with the account uuid, names the chat dir.

    Cached in the registry once found.  Disk first — every app data directory is
    scanned for a directory this account already owns, which costs nothing and
    works offline — and only then the API, the same endpoint the app itself uses.
    """
    if profile.org_uuid:
        return profile.org_uuid
    if not profile.account_uuid:
        return None
    for _, data_dir in app_data_dirs(vault):
        known = vault.known_org_uuids(data_dir, profile.account_uuid)
        if known:
            return _remember_org_uuid(vault, profile, known[0])
    fetched = usage.fetch_org_uuid(profile)
    return _remember_org_uuid(vault, profile, fetched) if fetched else None


def _remember_org_uuid(vault: Vault, profile: Profile, org_uuid: str) -> str:
    profile.org_uuid = org_uuid
    with vault.locked():
        stored = vault.find(profile.name)
        if stored:
            stored.org_uuid = org_uuid
            vault.upsert(stored)
            vault.save()
    return org_uuid


def prepare_app_profile(vault: Vault, profile: Profile) -> tuple[str, store.PoolPlan]:
    """Give the profile its data directory, its shared links and its chat pool.

    Wiring the pool moves files, so it is skipped while the app is up: losing a
    launch to a migration would be the wrong trade for something done daily.
    Creating the link ahead of the app is safe either way, and it is what makes a
    brand-new profile show the shared chats the moment it signs in.
    """
    data_dir = vault.app_data_dir_for(profile)
    Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = store.PoolPlan()
    with vault.locked():
        linked, conflicts = vault.link_app_shared(profile)
        if vault.app_per_account and not profile.is_default and not profile.app_data_dir:
            fresh = vault.get(profile.name)
            fresh.app_data_dir = data_dir
            vault.upsert(fresh)
        if vault.shares_chats(profile):
            safe, live = pool_guard(data_dir)
            if safe:
                plan = vault.wire_session_pool(
                    data_dir, live_account=live, sharing=vault.sharing_account_uuids()
                )
            vault.clear_agent_pool_links(data_dir)
            if profile.account_uuid:
                plan.prelinked = vault.link_session_pool(data_dir, profile.account_uuid)
        vault.touch(profile.name)
        vault.save()
    if linked:
        ui.note(f"  shared with the app: {', '.join(linked)}")
    if conflicts:
        ui.warn(f"not shared (a local copy is in the way): {', '.join(conflicts)}")
    return data_dir, plan


def _profile_owning(
    vault: Vault, account_uuid: Optional[str], *, exclude: str = ""
) -> Optional[Profile]:
    """The registered profile an account uuid belongs to, if any."""
    if not account_uuid:
        return None
    for profile in vault.profiles:
        if profile.name != exclude and profile.account_uuid == account_uuid:
            return profile
    return None


def _window_account_mismatch(profile: Profile, status) -> Optional[str]:
    """The account this window is signed in as, when it is knowably not ours.

    The app opens as whoever its data directory is signed in as, never as the
    picked profile — the machine-wide directory under ``default`` being the
    usual offender. Unknown uuids stay quiet: a fresh directory just asks for
    a sign-in.
    """
    if not (status.signed_in and status.account_uuid and profile.account_uuid):
        return None
    return status.account_uuid if status.account_uuid != profile.account_uuid else None


def _allow_foreign_window(
    vault: Vault, profile: Profile, foreign: str, *, assume_yes: bool
) -> None:
    """Fronting somebody else's chats takes an explicit yes, not a warning."""
    owner = _profile_owning(vault, foreign)
    ui.warn(
        f"this app profile is signed in as {owner.display if owner else foreign}, "
        f"not {profile.email or profile.name}"
    )
    if ui.confirm("open that window anyway?", assume_yes=assume_yes):
        return
    wanted = _profile_owning(vault, profile.account_uuid, exclude=profile.name)
    # A build that strips CLAUDE_USER_DATA_DIR would refuse the suggested launch
    # too, so pointing at it would send the user in a circle.
    if wanted and not claude_app.scrubs_user_data_dir():
        hint = f"run `claude-login {wanted.name} --app` to open {wanted.email or wanted.name}"
    else:
        hint = f"run `claude-login use --yes {profile.name} --app` to open it anyway"
    raise ClaudeAppError(f"not opening a window signed in as another account — {hint}")


def launch_app(vault: Vault, profile: Profile, args) -> int:
    """Start the desktop app under this account and return its pid."""
    claude_app.find_app()
    ui.step(f"{ui.paint(profile.display, 'bold')}  {ui.paint(describe(profile), 'grey')}")
    data_dir = vault.app_data_dir_for(profile)
    machine = claude_app.default_app_support_dir()
    if os.path.normpath(data_dir) != os.path.normpath(
        machine
    ) and not claude_app.can_relocate_user_data():
        # Nothing would relocate the profile, so Chromium's singleton would front
        # whatever machine-wide window is already open — the opposite of picking
        # an account. Refusing is the only honest move.
        raise ClaudeAppError(
            "this Claude app build refuses to start with --user-data-dir on the "
            "command line, so a per-account window cannot open — the machine-wide "
            "profile would appear instead; sign the app in as the account you "
            "need in that window"
        )
    status = claude_app.app_status(data_dir)
    foreign = _window_account_mismatch(profile, status)
    if foreign:
        _allow_foreign_window(
            vault, profile, foreign, assume_yes=getattr(args, "yes", False)
        )
    data_dir, plan = prepare_app_profile(vault, profile)
    ui.note(f"  CLAUDE_USER_DATA_DIR={data_dir}")
    if plan.linked:
        ui.note(f"  chats pooled: {plan.moved} moved from {plan.linked} directory(ies)")
    if plan.collisions:
        ui.note(f"  {plan.collisions} name clash(es) resolved by keeping the newer chat")
    if plan.prelinked:
        ui.note("  chat pool linked ahead of the app — shared chats show up on sign-in")
    if vault.shares_chats(profile) and claude_app.is_in_use(data_dir):
        ui.note(
            "  this profile is already open — quit that window and run"
            " `claude-login app adopt` to pool the rest of its chats"
        )
    pending = pending_pool_labels(vault)
    if pending:
        ui.warn(
            f"chats still outside the pool: {', '.join(pending)} — quit every Claude "
            "window and run `claude-login app adopt` once to share them"
        )
    if not status.signed_in:
        ui.note("  the app asks for a sign-in once; that login is separate from the CLI's")
    elif not foreign:
        last = claude_app.last_session(
            vault.pool_dir(claude_app.SESSIONS_DIRNAME), cwd=os.getcwd()
        )
        if last and last.get("title"):
            ui.note(f"  last chat here: {last['title']}")

    pid = claude_app.launch(data_dir, vault.app_env)
    ui.success(f"launched the Claude app (pid {pid})")
    return pid


# --- launch-flag editing ---------------------------------------------------

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

#: Flags the settings screen exposes as plain on/off switches.
TOGGLE_SETTINGS = (
    ("--dangerously-skip-permissions", "Skip permission prompts"),
    ("--continue", "Continue last conversation"),
)
KNOWN_SETTINGS = {name for name, _ in TOGGLE_SETTINGS} | {"--effort"}


def flag_state(flags: list[str], name: str) -> Optional[str]:
    """``None`` when absent, ``""`` for a bare flag, otherwise its value."""
    for group_name, group in _flag_groups(flags):
        if group_name != name:
            continue
        if "=" in group[0]:
            return group[0].split("=", 1)[1]
        return group[1] if len(group) > 1 else ""
    return None


def with_flag(flags: list[str], name: str, value: Optional[str]) -> list[str]:
    """Add, replace or (``value=None``) remove a flag, keeping its position."""
    replacement = None if value is None else ([name] if value == "" else [name, value])
    out: list[list[str]] = []
    placed = False
    for group_name, group in _flag_groups(flags):
        if group_name == name:
            if replacement is not None and not placed:
                out.append(replacement)
                placed = True
            continue
        out.append(group)
    if replacement is not None and not placed:
        out.append(replacement)
    return [token for group in out for token in group]


def other_flags(flags: list[str]) -> list[str]:
    """Everything the settings screen does not model as a named switch."""
    return [
        token
        for name, group in _flag_groups(flags)
        if name not in KNOWN_SETTINGS
        for token in group
    ]


def with_other_flags(flags: list[str], replacement: list[str]) -> list[str]:
    kept = [
        token
        for name, group in _flag_groups(flags)
        if name in KNOWN_SETTINGS
        for token in group
    ]
    return kept + replacement


def _cycle_effort(current: Optional[str]) -> Optional[str]:
    order: list[Optional[str]] = [None, *EFFORT_LEVELS]
    try:
        index = order.index(current)
    except ValueError:
        index = 0
    return order[(index + 1) % len(order)]


def launch(vault: Vault, profile: Profile, passthrough: list[str], launch_args: list[str]):
    """Record the choice and hand the terminal over to ``claude``."""
    args = build_claude_args(profile, passthrough, launch_args)
    with vault.locked():
        vault.touch(profile.name)
        vault.save()
    label = ui.paint(profile.display, "bold")
    where = profile.config_dir or claude_cli.default_config_dir()
    ui.step(f"{label}  {ui.paint(describe(profile), 'grey')}")
    ui.note(f"  CLAUDE_CONFIG_DIR={where}")
    ui.note(f"  claude {' '.join(args)}".rstrip())
    claude_cli.exec_claude(profile.config_dir, args)


def _offer_login(vault: Vault, profile: Profile, status: Status, *, assume_yes: bool) -> bool:
    """When a profile is not usable, offer to (re-)authenticate it in place."""
    if status.usable:
        return True
    if status.state == "missing":
        ui.warn(f"config directory for {profile.name!r} is gone; recreating it")
        vault.create_profile_dir(profile.name)
        with vault.locked():
            vault.link_shared(profile)
            vault.seed_config(profile)
    reason = {
        "expired": "its refresh token has expired",
        "logged-out": "it has no stored credentials",
        "missing": "its directory was missing",
    }.get(status.state, status.state)
    ui.warn(f"{profile.name!r} needs a new login — {reason}")
    if not ui.confirm(f"Sign in to {profile.name!r} now?", default=True, assume_yes=assume_yes):
        return False
    code = claude_cli.login(profile.config_dir)
    if code != 0:
        ui.error("login did not complete")
        return False
    with vault.locked():
        fresh = vault.get(profile.name)
        vault.refresh_identity(fresh)
        vault.upsert(fresh)
        vault.save()
    return True


# --- commands --------------------------------------------------------------


def cmd_list(vault: Vault, args) -> int:
    bootstrap(vault)
    profiles = vault.profiles
    if not profiles:
        ui.info("No accounts yet. Add one with `claude-login add`.")
        return 0

    warm_credentials(profiles)
    usages = usage_for(vault, profiles, args)

    if args.json:
        payload = []
        for profile in profiles:
            status = vault.status(profile)
            entry = {
                **profile.to_json(),
                "status": status.state,
                "expiresAt": status.expires_at,
                "refreshTokenExpiresAt": status.refresh_expires_at,
                "active": profile.name == vault.last_used,
            }
            live = usages.get(profile.name)
            if live:
                entry["usage"] = {
                    "fiveHourPercent": live.session.percent,
                    "fiveHourResetsAt": _iso(live.session.resets_at),
                    "weeklyPercent": live.weekly.percent,
                    "weeklyResetsAt": _iso(live.weekly.resets_at),
                    "weeklyScoped": (
                        {
                            "model": live.weekly_scoped.scope,
                            "percent": live.weekly_scoped.percent,
                            "resetsAt": _iso(live.weekly_scoped.resets_at),
                        }
                        if live.weekly_scoped
                        else None
                    ),
                    "stale": live.stale,
                }
            payload.append(entry)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    rows = [row_for(vault, profile, usages) for profile in profiles]
    print(ui.render_table(list(LIST_HEADERS), rows))
    return 0


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def cmd_add(vault: Vault, args) -> int:
    """Sign a new account in, then name the profile after its email address."""
    bootstrap(vault)
    if args.name:
        store.validate_name(args.name)
        if vault.find(args.name):
            raise UsageError(f"a profile named {args.name!r} already exists")

    login_args: list[str] = []
    if args.console:
        login_args.append("--console")
    if args.sso:
        login_args.append("--sso")
    if args.email:
        login_args += ["--email", args.email]

    # Log in inside a staging directory: the final one is named after the email,
    # which we only learn once the browser flow has finished.
    staging = _staging_dir(vault)
    ui.step("Signing in (a browser window will open)…")
    code = claude_cli.login(str(staging), login_args)

    account = claude_cli.read_global_config(str(staging)).get("oauthAccount") or {}
    signed_in = claude_cli.oauth_tokens(claude_cli.read_credentials(str(staging)))
    if code != 0 or not signed_in.get("accessToken"):
        _discard_staging(staging)
        ui.error("login did not complete — nothing was added")
        return 1

    email = account.get("emailAddress") or ""
    name = args.name or _name_from(email, vault)
    existing = vault.find(name)
    if existing:
        _discard_staging(staging)
        raise UsageError(f"{email or name} is already registered as {name!r}")

    duplicate = next(
        (p for p in vault.profiles if p.account_uuid and p.account_uuid == account.get("accountUuid")),
        None,
    )
    if duplicate:
        _discard_staging(staging)
        raise UsageError(f"that account is already registered as {duplicate.name!r}")

    target = vault.dir_for(name)
    vault.move_config_dir(str(staging), target)

    profile = Profile(name=name, config_dir=str(target), created_at=ui.iso_now())
    with vault.locked():
        linked, conflicts = vault.link_shared(profile)
        if not args.no_seed:
            vault.seed_config(profile)
        vault.refresh_identity(profile)
        vault.upsert(profile)
        vault.save()
    if linked:
        ui.note(f"  shared with ~/.claude: {', '.join(linked)}")
    if conflicts:
        ui.warn(f"not shared (a local copy is in the way): {', '.join(conflicts)}")

    ui.success(f"added {ui.paint(profile.display, 'bold')} — {describe(profile)}")
    if args.use or (
        ui.is_interactive()
        and not args.no_use
        and ui.confirm("Launch claude as this account now?", default=False)
    ):
        dispatch(vault, profile, args, base_target(vault, args))
    return 0


def _name_from(email: str, vault: Vault) -> str:
    """Profile directory name: the email itself, de-duplicated if need be."""
    base = email if email and store.NAME_RE.fullmatch(email) else "account"
    if not vault.find(base):
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if not vault.find(candidate):
            return candidate
    raise UsageError(f"could not derive a free profile name from {email!r}")


def _staging_dir(vault: Vault) -> Path:
    vault.staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="login-", dir=str(vault.staging_dir)))
    path.chmod(0o700)
    return path


def _discard_staging(staging: Path) -> None:
    claude_cli.delete_credentials(str(staging))
    shutil.rmtree(staging, ignore_errors=True)


def cmd_use(vault: Vault, args) -> int:
    bootstrap(vault)
    profile = vault.resolve(args.name)
    target = resolve_target(vault, args)
    # The app signs in on its own, so the CLI's credential state is not a
    # precondition for handing an account over to it.
    if target == "cli":
        status = vault.status(profile)
        if not status.usable and not _offer_login(
            vault, profile, status, assume_yes=args.yes
        ):
            return 1
    profile = vault.reload().get(profile.name)
    return dispatch(vault, profile, args, target)


def cmd_rename(vault: Vault, args) -> int:
    bootstrap(vault)
    profile = vault.resolve(args.old)
    if profile.is_default:
        raise UsageError("the default profile cannot be renamed (it is just ~/.claude)")
    store.validate_name(args.new)
    if vault.find(args.new):
        raise UsageError(f"a profile named {args.new!r} already exists")
    new_dir = vault.dir_for(args.new)
    if new_dir.exists():
        raise UsageError(f"{new_dir} already exists")

    with vault.locked():
        vault.move_config_dir(profile.config_dir or "", new_dir)
        renamed = vault.rename(profile.name, args.new)
        renamed.config_dir = str(new_dir)
        vault.upsert(renamed)
        vault.save()
    ui.success(f"renamed {profile.name} → {args.new}")
    return 0


def cmd_remove(vault: Vault, args) -> int:
    bootstrap(vault)
    profile = vault.resolve(args.name)
    what = "hide the default login from the list" if profile.is_default else f"delete profile {profile.name!r}"
    if not ui.confirm(f"Really {what}?", default=False, assume_yes=args.yes):
        ui.note("  cancelled")
        return 1

    if profile.is_default:
        with vault.locked():
            vault.hide_default()
            vault.delete(profile.name)
            vault.save()
        ui.success("default login hidden (its credentials were left untouched)")
        return 0

    if not args.keep_session:
        ui.step("Revoking the session…")
        claude_cli.logout(profile.config_dir)
    with vault.locked():
        claude_cli.delete_credentials(profile.config_dir)
        vault.remove_profile_dir(profile)
        vault.delete(profile.name)
        vault.save()
    ui.success(f"removed {profile.name!r}")
    return 0


def cmd_prune(vault: Vault, args) -> int:
    bootstrap(vault)
    plan: list[tuple[str, str, Any]] = []

    for profile in vault.profiles:
        status = vault.status(profile)
        if profile.is_default:
            continue
        if status.state == "expired":
            plan.append(("profile", f"{profile.name} — refresh token expired", profile))
        elif status.state == "logged-out":
            plan.append(("profile", f"{profile.name} — never signed in", profile))
        elif status.state == "missing":
            plan.append(("registry", f"{profile.name} — config directory is gone", profile))
        elif args.stale_days and profile.last_used_at:
            age_days = _age_days(profile.last_used_at)
            if age_days is not None and age_days >= args.stale_days:
                plan.append(
                    ("profile", f"{profile.name} — unused for {int(age_days)} days", profile)
                )

    known_dirs = {Path(p.config_dir).resolve() for p in vault.profiles if p.config_dir}
    if vault.profiles_dir.is_dir():
        for entry in sorted(vault.profiles_dir.iterdir()):
            if entry.is_dir() and entry.resolve() not in known_dirs:
                plan.append(("orphan", f"{entry.name} — directory with no registry entry", entry))
    if vault.staging_dir.is_dir():
        for entry in sorted(vault.staging_dir.iterdir()):
            if entry.is_dir():
                plan.append(("orphan", f"{entry.name} — interrupted sign-in", entry))

    if not plan:
        ui.success("nothing to clean up")
        return 0

    ui.info(ui.paint("The following will be removed:", "bold"))
    for kind, description, _ in plan:
        ui.info(f"  {ui.paint(kind, 'grey')}  {description}")
    print()
    if args.dry_run:
        ui.note("  (dry run — nothing was touched)")
        return 0
    if not ui.confirm(f"Remove {len(plan)} item(s)?", default=False, assume_yes=args.yes):
        ui.note("  cancelled")
        return 1

    removed = 0
    with vault.locked():
        for kind, _, target in plan:
            if kind in ("profile", "registry"):
                profile: Profile = target
                if kind == "profile":
                    claude_cli.delete_credentials(profile.config_dir)
                    vault.remove_profile_dir(profile)
                vault.delete(profile.name)
                removed += 1
            elif kind == "orphan":
                path: Path = target
                keychain.delete(claude_cli.credentials_service(str(path)))
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        vault.save()
    ui.success(f"cleaned up {removed} item(s)")
    return 0


def cmd_flags(vault: Vault, args) -> int:
    """Show or replace the flags every launch passes to ``claude``."""
    bootstrap(vault)
    new = list(args.flags or [])
    if new and new[0] == "--":
        new = new[1:]
    if args.clear:
        new = []
    if new or args.clear:
        with vault.locked():
            vault.set_launch_args(new)
            vault.save()

    current = vault.reload().launch_args
    if current:
        ui.info(f"claude {' '.join(current)}")
    else:
        ui.note("  no launch flags — claude starts bare")
    if "--continue" in current or "-c" in current:
        ui.note("  --continue is dropped automatically where there is no transcript yet")
    return 0


#: (kind, flag) per settings row, in display order.
_FLAGS_ROWS = (
    *(("toggle", name) for name, _ in TOGGLE_SETTINGS),
    ("effort", "--effort"),
    ("other", None),
)


def _flags_title(vault: Vault) -> str:
    flags = vault.reload().launch_args
    preview = f"claude {' '.join(flags)}".rstrip()
    return "\n".join(
        [
            ui.paint("Settings — launch flags", "bold"),
            "",
            ui.paint(preview, "cyan") if flags else ui.paint(preview + "  (no flags)", "grey"),
        ]
    )


def _flags_items(vault: Vault) -> list[picker.Item]:
    flags = vault.reload().launch_args
    items = []
    for name, label in TOGGLE_SETTINGS:
        enabled = flag_state(flags, name) is not None
        items.append(
            picker.Item(
                cells=[
                    label,
                    ui.paint(name, "grey"),
                    ui.paint("on", "green") if enabled else ui.paint("off", "grey"),
                ]
            )
        )
    effort = flag_state(flags, "--effort")
    items.append(
        picker.Item(
            cells=[
                "Reasoning effort",
                ui.paint("--effort", "grey"),
                ui.paint(effort, "green") if effort else ui.paint("off", "grey"),
            ]
        )
    )
    extra = other_flags(flags)
    items.append(
        picker.Item(
            cells=[
                "Other flags",
                ui.paint(" ".join(extra) or "—", "grey"),
                ui.paint("edit", "grey"),
            ]
        )
    )
    return items


def _save_launch_args(vault: Vault, flags: list[str]) -> None:
    with vault.locked():
        vault.set_launch_args(flags)
        vault.save()


def cmd_settings_flags(vault: Vault, args=None) -> int:
    """Interactive editor for the flags every launch passes to ``claude``."""
    bootstrap(vault)
    actions = [picker.Action("e", "edit as text"), picker.Action("c", "clear all")]
    selected = 0

    def on_select(index: int) -> bool:
        """Toggle in place; the text row defers so the caller can prompt."""
        kind, name = _FLAGS_ROWS[index]
        if kind == "other":
            return False
        flags = vault.reload().launch_args
        if kind == "toggle":
            value = None if flag_state(flags, name) is not None else ""
            _save_launch_args(vault, with_flag(flags, name, value))
        else:
            _save_launch_args(
                vault, with_flag(flags, "--effort", _cycle_effort(flag_state(flags, "--effort")))
            )
        return True

    while True:
        result = picker.pick(
            lambda: _flags_items(vault),
            title=lambda: _flags_title(vault),
            actions=actions,
            headers=("SETTING", "FLAG", "VALUE"),
            initial=selected,
            enter_label="change",
            quit_label="back",
            on_select=on_select,
        )
        if result.action == picker.CANCEL:
            return 0
        if result.index is not None:
            selected = result.index

        flags = vault.reload().launch_args
        if result.action == "select":
            answer = ui.ask("Other flags:", default=" ".join(other_flags(flags)))
            if answer is not None:
                _save_launch_args(vault, with_other_flags(flags, answer.split()))
        elif result.action == "e":
            answer = ui.ask("Launch flags:", default=" ".join(flags))
            if answer is not None:
                _save_launch_args(vault, answer.split())
        elif result.action == "c":
            _save_launch_args(vault, [])


# --- settings: sections ----------------------------------------------------

#: The sections of the top-level settings screen, in display order.
SETTINGS_SECTIONS = ("target", "flags", "app")


def cycle_target(current: str) -> str:
    order = store.LAUNCH_TARGETS
    index = order.index(current) if current in order else -1
    return order[(index + 1) % len(order)]


def target_label(target: str) -> str:
    return {"cli": "CLI", "app": "Claude Code App", "ask": "ask every time"}.get(
        target, target
    )


def _settings_sections(vault: Vault) -> list[picker.Item]:
    vault.reload()
    flags = vault.launch_args
    app_bits = [
        "per-account" if vault.app_per_account else "one shared profile",
        f"{_chosen_count(vault, vault.app_shared_accounts)} sharing chats",
        f"{len(vault.app_shared)} shared entries",
    ]
    return [
        picker.Item(
            cells=["Launch target", ui.paint(target_label(vault.launch_target), "green")]
        ),
        picker.Item(
            cells=[
                "Launch flags (CLI)",
                ui.paint(" ".join(flags), "cyan") if flags else ui.paint("none", "grey"),
            ]
        ),
        picker.Item(cells=["Claude Code App", ui.paint(" · ".join(app_bits), "grey")]),
    ]


def cmd_settings(vault: Vault, args=None) -> int:
    """Top-level settings: launch target, CLI flags, the app profile."""
    bootstrap(vault)
    selected = 0

    def on_select(index: int) -> bool:
        """The target row cycles in place; the other two open a screen."""
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
        if result.action != "select":
            continue
        if SETTINGS_SECTIONS[selected] == "flags":
            cmd_settings_flags(vault)
        elif SETTINGS_SECTIONS[selected] == "app":
            cmd_settings_app(vault)


#: (kind, label) per app-settings row, in display order.
_APP_SETTINGS_ROWS = (
    ("per-account", "Per-account app profile"),
    ("open", "Accounts to open"),
    ("sharing", "Accounts sharing chats"),
    ("shared", "Shared entries"),
    ("env", "Launch env"),
    ("path", "App path"),
)


def _chosen_count(vault: Vault, selected: Optional[list[str]]) -> str:
    """``2 of 3`` — how many accounts a list covers. Absent means all of them."""
    total = len(vault.profiles)
    chosen = total if selected is None else len([n for n in selected if vault.find(n)])
    return f"{chosen} of {total}"


def _app_settings_items(vault: Vault) -> list[picker.Item]:
    vault.reload()
    on, off = ui.paint("on", "green"), ui.paint("off", "grey")
    values = {
        "per-account": on if vault.app_per_account else off,
        "open": ui.paint(_chosen_count(vault, vault.app_open_accounts), "grey"),
        "sharing": ui.paint(_chosen_count(vault, vault.app_shared_accounts), "grey"),
        "shared": ui.paint(f"{len(vault.app_shared)} entries", "grey"),
        "env": ui.paint(f"{len(vault.app_env)} variable(s)", "grey"),
        "path": ui.paint(claude_app.app_bundle(), "grey"),
    }
    hints = {
        kind: ui.paint("edit", "grey") for kind in ("open", "sharing", "shared", "env")
    }
    return [
        picker.Item(cells=[label, values[kind], hints.get(kind, "")])
        for kind, label in _APP_SETTINGS_ROWS
    ]


def _app_settings_title() -> str:
    return "\n".join(
        [
            ui.paint("Settings — Claude Code App", "bold"),
            "",
            ui.paint(
                "Each account gets its own app profile, so both stay signed in and "
                "can be open at once; chats stay shared between them.",
                "grey",
            ),
        ]
    )


def cmd_settings_app(vault: Vault, args=None) -> int:
    """How the desktop app is launched, and what its profiles share."""
    bootstrap(vault)
    selected = 0

    def on_select(index: int) -> bool:
        """Only the boolean row toggles here; the lists open their own screen."""
        if _APP_SETTINGS_ROWS[index][0] != "per-account":
            return False
        with vault.locked():
            vault.set_app_per_account(not vault.app_per_account)
            vault.save()
        return True

    while True:
        result = picker.pick(
            lambda: _app_settings_items(vault),
            title=_app_settings_title,
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
        if kind == "open":
            cmd_settings_accounts(vault, "open")
        elif kind == "sharing":
            cmd_settings_accounts(vault, "sharing")
        elif kind == "shared":
            cmd_settings_shared(vault)
        elif kind == "env":
            current = " ".join(f"{k}={v}" for k, v in vault.app_env.items())
            answer = ui.ask("Launch env (KEY=VALUE ...):", default=current)
            if answer is not None:
                with vault.locked():
                    vault.set_app_env(parse_env(answer))
                    vault.save()
        elif kind == "path":
            ui.note("  point CLAUDE_LOGIN_APP_PATH at another bundle to change this")


def shared_entry_choices(vault: Vault) -> list[str]:
    """Everything worth offering: the built-in list plus whatever is configured.

    Order is stable so the rows do not jump around between redraws.
    """
    choices = list(claude_app.DEFAULT_APP_SHARED)
    for entry in vault.app_shared:
        if entry not in choices:
            choices.append(entry)
    return choices


def _shared_items(vault: Vault) -> list[picker.Item]:
    vault.reload()
    chosen = set(vault.app_shared)
    machine = Path(claude_app.default_app_support_dir())
    items = []
    for entry in shared_entry_choices(vault):
        ticked = entry in chosen
        present = (machine / entry).exists()
        items.append(
            picker.Item(
                cells=[
                    ui.paint("[x]", "green") if ticked else ui.paint("[ ]", "grey"),
                    entry,
                    ui.paint("" if present else "not on this machine", "grey"),
                ],
                value=entry,
            )
        )
    return items


def cmd_settings_shared(vault: Vault, args=None) -> int:
    """Tick which app entries are shared between profiles.

    A checkbox list rather than an editable line: two of the names contain
    spaces (``Claude Extensions``), and a space-separated round trip silently
    shredded them into fragments that match nothing — which looked exactly like
    "sharing does not work" while everything reported success.
    """
    bootstrap(vault)
    choices = shared_entry_choices(vault)

    def on_select(index: int) -> bool:
        if index >= len(choices):
            return True
        with vault.locked():
            vault.set_app_shared(toggled(vault.app_shared, choices[index]))
            vault.save()
        return True

    picker.pick(
        lambda: _shared_items(vault),
        title="\n".join(
            [
                ui.paint("Settings — shared with the app", "bold"),
                "",
                ui.paint(
                    "Ticked entries are symlinked from the machine-wide app "
                    "directory into every profile. config.json is never here — "
                    "it holds the account's token.",
                    "grey",
                ),
            ]
        ),
        headers=("", "ENTRY", ""),
        enter_label="toggle",
        quit_label="back",
        on_select=on_select,
    )
    return 0


#: (title, hint) per account-list screen.
_ACCOUNT_SCREENS = {
    "open": (
        "Settings — accounts to open",
        "`claude-login app open` and the open-accounts script start these.",
    ),
    "sharing": (
        "Settings — accounts sharing chats",
        "Ticked accounts see one shared list of Code chats. Unticking gives an "
        "account its own copy back — nothing is lost either way.",
    ),
}


def selected_accounts(vault: Vault, kind: str) -> Optional[list[str]]:
    return vault.app_open_accounts if kind == "open" else vault.app_shared_accounts


def _account_items(vault: Vault, kind: str) -> list[picker.Item]:
    vault.reload()
    selected = selected_accounts(vault, kind)
    items = []
    for profile in vault.profiles:
        ticked = selected is None or profile.name in selected
        items.append(
            picker.Item(
                cells=[
                    ui.paint("[x]", "green") if ticked else ui.paint("[ ]", "grey"),
                    profile.display,
                    ui.paint(describe(profile), "grey"),
                ],
                value=profile,
            )
        )
    return items


def toggled(names: list[str], name: str) -> list[str]:
    return [n for n in names if n != name] if name in names else [*names, name]


def cmd_settings_accounts(vault: Vault, kind: str, args=None) -> int:
    """Tick which accounts a list covers, one screen for both lists.

    An absent list means "all accounts"; the first toggle turns it into an
    explicit one, which is why the current names are materialised on entry.
    """
    bootstrap(vault)
    title, hint = _ACCOUNT_SCREENS[kind]

    def on_select(index: int) -> bool:
        vault.reload()
        profiles = vault.profiles
        if index >= len(profiles):
            return True
        selected = selected_accounts(vault, kind)
        names = [p.name for p in profiles] if selected is None else list(selected)
        target = profiles[index].name
        names = toggled(names, target)
        with vault.locked():
            if kind == "open":
                vault.set_app_open_accounts(names)
            else:
                vault.set_app_shared_accounts(names)
                if target not in names:
                    # Unticking has to be reversible, so hand the account back
                    # its own copy rather than leave a link into a pool it is no
                    # longer part of.
                    profile = vault.find(target)
                    if profile:
                        data_dir = vault.app_data_dir_for(profile)
                        if not claude_app.is_in_use(data_dir):
                            vault.unwire_session_pool(data_dir)
            vault.save()
        return True

    picker.pick(
        lambda: _account_items(vault, kind),
        title="\n".join([ui.paint(title, "bold"), "", ui.paint(hint, "grey")]),
        headers=("", "ACCOUNT", ""),
        enter_label="toggle",
        quit_label="back",
        on_select=on_select,
    )
    return 0


def parse_env(text: str) -> dict[str, str]:
    """``FOO=bar BAZ=qux`` → dict. Tokens without an ``=`` are ignored."""
    env: dict[str, str] = {}
    for token in text.split():
        key, separator, value = token.partition("=")
        if separator and key:
            env[key] = value
    return env


# --- the app's own subcommands ---------------------------------------------

APP_STATUS_HEADERS = ("ACCOUNT", "APP LOGIN", "CHATS", "DATA DIR")


def app_data_dirs(vault: Vault) -> list[tuple[str, str]]:
    """Every app data directory worth touching, as ``(label, path)``.

    The machine-wide one is always first and never conditional on ``~/.claude``
    having a login of its own: it holds the chats from before any of this
    existed, which are exactly the ones worth pooling.  Duplicates collapse, so
    this stays right when several profiles map to the same directory.
    """
    entries = [("machine-wide", claude_app.default_app_support_dir())]
    entries += [(p.display, vault.app_data_dir_for(p)) for p in vault.profiles]
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, path in entries:
        key = os.path.normpath(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append((label, path))
    return unique


def _chats_state(vault: Vault, data_dir: str) -> str:
    """How this profile's chat directories relate to the shared pool."""
    private = claude_app.session_account_dirs(data_dir)
    shared = claude_app.session_account_links(data_dir)
    if not private and not shared:
        return ui.paint("none yet", "grey")
    if not private:
        return ui.paint("shared", "green")
    if shared:
        return ui.paint("partly shared", "yellow")
    return ui.paint("private", "yellow")


def cmd_app_status(vault: Vault, args=None) -> int:
    """Per account: whether the app is signed in, and where its data lives."""
    bootstrap(vault)
    if not claude_app.available():
        ui.warn(f"the Claude app was not found at {claude_app.app_bundle()}")
    badges = {
        "signed-in": ui.paint("signed in", "green"),
        "logged-out": ui.paint("not signed in", "yellow"),
        "missing": ui.paint("no profile yet", "grey"),
    }
    rows = []
    for profile in vault.profiles:
        data_dir = vault.app_data_dir_for(profile)
        status = claude_app.app_status(data_dir)
        rows.append(
            [
                profile.display,
                badges.get(status.state, status.state),
                _chats_state(vault, data_dir),
                ui.paint(data_dir, "grey"),
            ]
        )
    if not rows:
        ui.info("No accounts yet. Add one with `claude-login add`.")
        return 0
    print(ui.render_table(list(APP_STATUS_HEADERS), rows))
    return 0


def cmd_app_adopt(vault: Vault, args) -> int:
    """Fold every account's existing app chats into the one shared pool.

    A directory that a live window has open is left alone rather than blocking
    the whole run: pooling one account's chats while another account's window
    stays open is both safe and useful.
    """
    bootstrap(vault)
    dry_run = getattr(args, "dry_run", False)
    total = store.PoolPlan()
    busy: list[str] = []
    for label, data_dir in app_data_dirs(vault):
        if not Path(data_dir).is_dir():
            continue
        safe, live_account = pool_guard(data_dir)
        if not safe:
            busy.append(label)
            continue
        with vault.locked():
            plan = vault.wire_session_pool(
                data_dir, dry_run=dry_run, live_account=live_account
            )
            vault.save()
        total.moved += plan.moved
        total.linked += plan.linked
        total.collisions += plan.collisions
        total.live += plan.live
        total.unmoved.extend(plan.unmoved)
        total.backup = total.backup or plan.backup
        if plan.linked:
            ui.info(f"{label}: {plan.moved} chat(s) from {plan.linked} directory(ies)")

    with vault.locked():
        total.orphans = vault.sweep_pool(dry_run=dry_run)
        vault.save()

    if busy:
        ui.warn(
            f"left untouched, cannot tell who is signed in: {', '.join(sorted(set(busy)))}"
            " — quit that window and run this again"
        )
    if total.live:
        ui.note(
            f"  {total.live} directory(ies) were open in the app and folded in by copy;"
            " the originals are kept next to them as .replaced-<ms>"
        )
    if total.backup:
        ui.note(f"  backup: {total.backup}")
    if total.collisions:
        ui.note(f"  {total.collisions} name clash(es); the newer chat was kept")
    if total.unmoved:
        ui.warn(
            "left alone because it is not a chat file: "
            f"{', '.join(total.unmoved[:6])} — that directory keeps its own chats"
        )
    if total.orphans:
        ui.note(
            f"  {total.orphans} chat(s) had no transcript left on disk and were set "
            "aside in app-shared/orphans (the app could only say 'session not found')"
        )
    if dry_run:
        ui.note("  (dry run — nothing was touched)")
        return 0
    if not total.linked and not total.orphans:
        if busy:
            ui.note("  nothing else to move")
            return 1
        ui.success("nothing to adopt — chats are already shared")
        return 0
    ui.success(
        f"pooled {total.moved} chat(s) — every account now shows the same Recents"
    )
    return 0


#: Electron instances started in the same instant fight over CPU and over first
#: access to the shared sidecar binaries, so they are staggered.
LAUNCH_STAGGER_SECONDS = 1.5


def cmd_app_open(vault: Vault, args) -> int:
    """Open the app under several accounts at once."""
    bootstrap(vault)
    claude_app.find_app()
    names = list(getattr(args, "names", None) or []) or vault.app_open_accounts
    if names is None:
        targets = vault.profiles
    else:
        targets = []
        for name in names:
            try:
                targets.append(vault.resolve(name))
            except ClaudeLoginError:
                # A stale name in the configured list must not stop the rest.
                ui.warn(f"no account named {name!r} — skipping")

    opened = 0
    for profile in targets:
        data_dir = vault.app_data_dir_for(profile)
        if claude_app.is_in_use(data_dir):
            ui.note(f"  {profile.display}: already open")
            continue
        if opened:
            time.sleep(LAUNCH_STAGGER_SECONDS)
        try:
            launch_app(vault, vault.reload().get(profile.name), args)
        except ClaudeLoginError as exc:
            # One window signed in as somebody else must not block the rest.
            ui.error(str(exc))
            continue
        opened += 1
    if not opened:
        ui.success("nothing to open — every account already has a window")
    return 0


def cmd_app_link(vault: Vault, args) -> int:
    """Link accounts into the chat pool without waiting for the app to run.

    Useful right after adding an account: the pool link normally appears on the
    first launch, and this makes it appear now.
    """
    bootstrap(vault)
    if not vault.sharing_enabled:
        ui.note("  chat sharing is off — turn it on in `claude-login settings`")
        return 0
    named = getattr(args, "name", None)
    targets = [vault.resolve(named)] if named else vault.profiles
    linked = 0
    for profile in targets:
        data_dir = vault.app_data_dir_for(profile)
        if not profile.account_uuid:
            ui.warn(
                f"{profile.display}: no account uuid yet — it is learnt on the first "
                "sign-in, so run `claude-login use` once"
            )
            continue
        # Not needed to link any more, but a cached organisation uuid is what
        # lets an older flat pool be split correctly, so warm it while we are
        # here.  Outside the lock: flock is not reentrant and this may write.
        resolve_org_uuid(vault, profile)
        Path(data_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
        with vault.locked():
            count = vault.link_session_pool(data_dir, profile.account_uuid)
            vault.save()
        linked += count
        ui.info(
            f"{profile.display}: {'linked' if count else 'already linked'}"
            f"  ({profile.account_uuid})"
        )
    if linked:
        ui.success(f"created {linked} pool link(s)")
    return 0


def cmd_app_relink(vault: Vault, args) -> int:
    """Recreate the app's shared symlinks and its links into the pool."""
    bootstrap(vault)
    named = getattr(args, "name", None)
    targets = [vault.resolve(named)] if named else vault.profiles
    pooling = vault.sharing_enabled
    stuck = False
    stale_links = 0
    for profile in targets:
        data_dir = vault.app_data_dir_for(profile)
        with vault.locked():
            linked, conflicts = vault.link_app_shared(
                profile, repair=getattr(args, "force", False)
            )
            if pooling and Path(data_dir).is_dir():
                safe, live = pool_guard(data_dir)
                if safe:
                    vault.wire_session_pool(data_dir, live_account=live)
                dropped, _ = vault.clear_agent_pool_links(data_dir)
                stale_links += dropped
                if profile.account_uuid:
                    vault.link_session_pool(data_dir, profile.account_uuid)
            vault.save()
        ui.info(
            f"{profile.display}: {', '.join(linked) if linked else 'already up to date'}"
        )
        if linked and getattr(args, "force", False):
            ui.note(f"  any private copy of those went to {data_dir}/.shadowed/")
        if conflicts:
            stuck = True
            ui.warn(f"  {profile.display} has its own copy of: {', '.join(conflicts)}")
    if pooling and not named:
        # The machine-wide directory has no shared links of its own — it is the
        # source — but its chats still belong in the pool.
        machine = claude_app.default_app_support_dir()
        safe, machine_live = pool_guard(machine)
        if Path(machine).is_dir() and safe:
            with vault.locked():
                vault.wire_session_pool(machine, live_account=machine_live)
                dropped, _ = vault.clear_agent_pool_links(machine)
                stale_links += dropped
                vault.save()
    if stale_links:
        ui.note(
            f"  removed {stale_links} dead pool link(s) left in agent-mode directories"
            " — the app refuses to save anything while one is in the way"
        )
    if stuck:
        ui.note(
            "  `claude-login app relink --force` moves those copies into"
            " <profile>/app-data/.shadowed/"
        )
    # A left-over private copy means the entry is not actually shared, so this
    # cannot report success — `sync-all` counts on the return value.
    return 1 if stuck else 0


def cmd_env(vault: Vault, args) -> int:
    bootstrap(vault)
    profile = vault.resolve(args.name)
    if profile.is_default:
        print("unset CLAUDE_CONFIG_DIR")
    else:
        print(f'export CLAUDE_CONFIG_DIR="{profile.config_dir}"')
    return 0


def cmd_relink(vault: Vault, args) -> int:
    bootstrap(vault)
    targets = [vault.resolve(args.name)] if args.name else vault.profiles
    stuck = False
    with vault.locked():
        for profile in targets:
            if profile.is_default:
                continue
            linked, conflicts = vault.link_shared(profile, repair=args.force)
            ui.info(f"{profile.name}: {', '.join(linked) if linked else 'already up to date'}")
            if conflicts:
                stuck = True
                ui.warn(f"  {profile.name} has its own copy of: {', '.join(conflicts)}")
    if stuck:
        ui.note(
            "  Claude Code rewrites some files in place, which replaces the link.\n"
            "  `claude-login relink --force` moves those copies into <profile>/.shadowed/\n"
            "  and restores the link to ~/.claude."
        )
    return 0


def cmd_sync(vault: Vault, args) -> int:
    """Re-copy the shared half of ~/.claude.json into the profiles."""
    bootstrap(vault)
    if getattr(args, "gather", False):
        with vault.locked():
            added = vault.gather_config()
            vault.save()
        if added:
            ui.info(f"gathered from profiles: {', '.join(added)}")
    targets = [vault.resolve(args.name)] if args.name else vault.profiles
    if not [p for p in targets if not p.is_default]:
        ui.note("  nothing to sync — ~/.claude.json is the source, not a target")
        return 0
    touched = 0
    with vault.locked():
        for profile in targets:
            if profile.is_default:
                continue
            changed = vault.sync_config(profile)
            if changed:
                touched += 1
                ui.info(f"{profile.name}: updated {', '.join(changed)}")
            else:
                ui.info(f"{profile.name}: {ui.paint('already in sync', 'grey')}")
    if touched:
        ui.success(f"synced {touched} profile(s) from ~/.claude.json")
    return 0


#: (label, function, extra kwargs) for every step `sync-all` runs, in order.
def _sync_all_steps(dry_run: bool) -> list[tuple[str, Any, dict]]:
    return [
        ("CLI links", cmd_relink, {"name": None, "force": False}),
        ("MCP servers and trusted folders", cmd_sync, {"name": None, "gather": True}),
        # Repairing is the whole point of this button: a profile that kept its
        # own copy of a shared entry is exactly the state the user is trying to
        # get out of, and the copy is preserved under .shadowed either way.
        ("app links", cmd_app_relink, {"name": None, "force": True}),
        ("chat pool links", cmd_app_link, {"name": None}),
        ("existing chats", cmd_app_adopt, {"dry_run": dry_run, "yes": True}),
    ]


def cmd_sync_all(vault: Vault, args) -> int:
    """Make everything shareable shared, in one idempotent pass.

    The steps already exist as separate commands; the value here is not having
    to remember which of them applies to what.  A failing step does not cancel
    the rest — half the work done and named beats nothing done.
    """
    bootstrap(vault)
    dry_run = getattr(args, "dry_run", False)
    failed: list[str] = []
    for label, handler, kwargs in _sync_all_steps(dry_run):
        ui.info("")
        ui.info(ui.paint(label, "bold"))
        try:
            if handler(vault.reload(), argparse.Namespace(**kwargs)):
                failed.append(label)
        except ClaudeLoginError as exc:
            failed.append(label)
            ui.error(str(exc))
    ui.info("")
    if failed:
        ui.warn(f"finished with problems in: {', '.join(failed)}")
        return 1
    ui.success("everything that can be shared is shared")
    return 0


def cmd_setup(vault: Vault, args) -> int:
    """Walk a new machine from nothing to working. Safe to run again."""
    bootstrap(vault)
    ui.info(ui.paint("claude-login setup", "bold"))
    ui.info("")

    ui.info(ui.paint("what is installed", "bold"))
    try:
        ui.info(f"  claude          {claude_cli.find_claude()}")
    except ClaudeLoginError as exc:
        ui.info(f"  claude          {ui.paint('missing', 'red')} — {exc}")
    if claude_app.available():
        ui.info(f"  Claude app      {claude_app.app_bundle()}")
    else:
        ui.info(
            f"  Claude app      {ui.paint('not found', 'yellow')} at "
            f"{claude_app.app_bundle()} — the CLI half works without it"
        )

    ui.info("")
    ui.info(ui.paint("accounts", "bold"))
    for profile in vault.profiles:
        ui.info(f"  {profile.display}  {ui.paint(describe(profile), 'grey')}")
    if not vault.profiles:
        ui.info("  (none yet)")

    if not ui.is_interactive():
        ui.info("")
        ui.note("  not a terminal — run `claude-login setup` from one to continue")
        return 0

    while ui.confirm("Add an account now?", default=not vault.profiles):
        _run_add(vault, args)
        vault.reload()

    if len(vault.profiles) < 2:
        ui.note("  one account is enough to start; add the second whenever you like")

    if claude_app.available() and ui.confirm(
        "Launch the Claude app by default instead of the CLI?", default=False
    ):
        with vault.locked():
            vault.set_launch_target("app")
            vault.save()

    cmd_sync_all(vault.reload(), argparse.Namespace(dry_run=False))

    ui.info("")
    ui.info(ui.paint("what to click from now on", "bold"))
    for name, what in SCRIPT_SUMMARY:
        ui.info(f"  scripts/{name:<24} {ui.paint(what, 'grey')}")
    return 0


#: Kept next to the scripts themselves so `setup` and the README agree.
SCRIPT_SUMMARY = (
    ("setup.command", "this walkthrough"),
    ("add-account.command", "sign a new account in and open a clean window for it"),
    ("open-accounts.command", "open the app under every chosen account"),
    ("sync-all.command", "share skills, MCP servers and chats across accounts"),
    ("doctor.command", "diagnose what is wired and what is not"),
)


def cmd_doctor(vault: Vault, args) -> int:
    bootstrap(vault)
    problems = 0

    ui.info(ui.paint("environment", "bold"))
    try:
        binary = claude_cli.find_claude()
        ui.info(f"  claude          {binary} ({claude_cli.version() or 'unknown version'})")
    except ClaudeLoginError as exc:
        ui.info(f"  claude          {ui.paint('missing', 'red')} — {exc}")
        problems += 1
    ui.info(f"  vault           {vault.root}")
    ui.info(
        f"  keychain        {'available' if keychain.available() else ui.paint('unavailable', 'yellow')}"
        f"  (account: {keychain.account_name()})"
    )
    sessions = claude_cli.running_sessions()
    if sessions:
        ui.info(f"  running claude  {len(sessions)} process(es): {', '.join(map(str, sessions))}")

    ui.info("")
    ui.info(ui.paint("machine-wide login (~/.claude)", "bold"))
    default_service = claude_cli.credentials_service(None)
    default_present = keychain.exists(default_service)
    ui.info(f"  keychain item   {default_service} — {'found' if default_present else 'absent'}")
    if not default_present and keychain.available():
        ui.info(
            f"  {ui.paint('note', 'yellow')}            no default login; that is fine if you only use profiles"
        )

    ui.info("")
    ui.info(ui.paint("profiles", "bold"))
    for profile in vault.profiles:
        status = vault.status(profile)
        text, styles = badge_for(status)
        ui.info(f"  {ui.paint(profile.name, 'bold')}  {ui.paint(text, *styles)}")
        ui.info(f"    dir           {profile.config_dir or claude_cli.default_config_dir()}")
        ui.info(f"    keychain      {claude_cli.credentials_service(profile.config_dir)}")
        if status.expires_at:
            ui.info(f"    access token  expires {ui.relative_ms(status.expires_at)}")
        if status.refresh_expires_at:
            ui.info(f"    refresh token expires {ui.relative_ms(status.refresh_expires_at)}")
        if status.prunable:
            problems += 1
        missing, diverged = vault.shared_conflicts(profile)
        if missing:
            ui.info(
                f"    {ui.paint('unlinked', 'yellow')}      {', '.join(missing)}"
                "  (fix with `claude-login relink`)"
            )
        if diverged:
            problems += 1
            ui.info(
                f"    {ui.paint('diverged', 'yellow')}      {', '.join(diverged)}"
                "  (this profile keeps a private copy; `claude-login relink --force`)"
            )
    if not vault.profiles:
        ui.info("  (none yet)")

    problems += _doctor_app(vault)

    ui.info("")
    if problems:
        ui.warn(f"{problems} issue(s) found — `claude-login prune` cleans up dead profiles")
    else:
        ui.success("everything looks healthy")
    return 0


def _doctor_app(vault: Vault) -> int:
    """The Claude desktop app half of `doctor`. Returns the problem count."""
    problems = 0
    ui.info("")
    ui.info(ui.paint("Claude desktop app", "bold"))
    if claude_app.available():
        ui.info(f"  bundle          {claude_app.app_bundle()}")
        if not claude_app.can_relocate_user_data():
            problems += 1
            ui.info(
                f"  {ui.paint('app build', 'yellow')}       refuses --user-data-dir on"
                " the command line — per-account app windows cannot open"
            )
        elif claude_app.scrubs_user_data_dir():
            ui.info(
                "  app build       ignores CLAUDE_USER_DATA_DIR; per-account windows"
                " use --user-data-dir (Chrome extension pairing is off in them)"
            )
    else:
        ui.info(
            f"  bundle          {ui.paint('not found', 'yellow')}"
            f" at {claude_app.app_bundle()}"
        )
    ui.info(f"  support dir     {claude_app.default_app_support_dir()}")
    ui.info(f"  launch target   {target_label(vault.launch_target)}")
    running = claude_app.running_pids()
    if running:
        ui.info(f"  running         {len(running)}: {', '.join(map(str, running))}")
    for open_dir in sorted(claude_app.running_data_dirs()):
        ui.info(f"  open profile    {open_dir}")

    for label, data_dir in app_data_dirs(vault):
        unpooled = claude_app.session_account_dirs(data_dir)
        if unpooled and vault.sharing_enabled:
            problems += 1
            ui.info(
                f"  {ui.paint('not pooled', 'yellow')}      {label}: {len(unpooled)}"
                " chat directory(ies)  (fix with `claude-login app adopt`)"
            )
        # A symlink where the app wants to create its own directory is not a
        # cosmetic problem: it makes every save fail with ENOTDIR, and the only
        # place that says so is the app's own log.
        blocked = [
            leaf
            for agent in (False, True)
            for leaf in claude_app.session_leaf_dirs(data_dir, agent=agent)
            if claude_app.rejects_leaf(leaf)
        ]
        blocked += claude_app.session_account_links(data_dir, agent=True)
        if blocked:
            problems += 1
            ui.info(
                f"  {ui.paint('unwritable', 'yellow')}      {label}: {len(blocked)}"
                " chat directory(ies) the app refuses to write to"
                "  (fix with `claude-login app relink`)"
            )

    for profile in vault.profiles:
        data_dir = vault.app_data_dir_for(profile)
        status = claude_app.app_status(data_dir)
        ui.info(f"  {ui.paint(profile.display, 'bold')}  {status.state}")
        ui.info(f"    data dir      {data_dir}")
        if (
            status.account_uuid
            and profile.account_uuid
            and status.account_uuid != profile.account_uuid
        ):
            problems += 1
            ui.info(
                f"    {ui.paint('wrong account', 'yellow')} the app profile is signed"
                f" in as {status.account_uuid}"
            )
        missing, diverged = vault.app_shared_conflicts(profile)
        if missing:
            ui.info(
                f"    {ui.paint('unlinked', 'yellow')}      {', '.join(missing)}"
                "  (fix with `claude-login app relink`)"
            )
        if diverged:
            problems += 1
            ui.info(
                f"    {ui.paint('diverged', 'yellow')}      {', '.join(diverged)}"
                "  (`claude-login app relink --force`)"
            )
    return problems


# --- interactive entry point ----------------------------------------------


def interactive(vault: Vault, args) -> int:
    bootstrap(vault)
    actions = [
        picker.Action("a", "add"),
        picker.Action("o", "other target", needs_item=True),
        picker.Action("s", "settings"),
        picker.Action("r", "reload"),
        picker.Action("d", "delete", needs_item=True),
        picker.Action("p", "prune"),
    ]
    force_refresh = getattr(args, "refresh", False)
    while True:
        profiles = vault.reload().profiles
        if not profiles:
            ui.info("No accounts yet.")
            if not ui.confirm("Add one now?", default=True):
                return 0
            _run_add(vault, args)
            continue

        if force_refresh:
            claude_cli.forget_credentials(everything=True)
            force_refresh = False
        warm_credentials(profiles)

        # Paint straight away and let the rate-limit lookup land a moment
        # later, instead of making the user wait on the network.
        pending = background_usage(vault, profiles, args)

        initial = next(
            (i for i, p in enumerate(profiles) if p.name == vault.last_used), 0
        )
        result = picker.pick(
            lambda: [
                picker.Item(
                    cells=row_for(
                        vault, p, pending.values, marker=False, pending=pending.pending
                    ),
                    value=p,
                )
                for p in profiles
            ],
            title=ui.paint("Select a Claude Code account", "bold"),
            actions=actions,
            initial=initial,
            headers=LIST_HEADERS[1:],
            poll=pending.settled,
        )

        if result.action == picker.CANCEL:
            return 0
        if result.action in ("select", "o") and result.item:
            profile: Profile = result.item.value
            target = (
                resolve_target(vault, args)
                if result.action == "select"
                else other_target(base_target(vault, args))
            )
            if target == "cli":
                status = vault.status(profile)
                if not status.usable and not _offer_login(
                    vault, profile, status, assume_yes=False
                ):
                    continue
            profile = vault.reload().get(profile.name)
            return dispatch(vault, profile, args, target)
        if result.action == "a":
            _run_add(vault, args)
        elif result.action == "s":
            cmd_settings(vault)
        elif result.action == "r":
            force_refresh = True
        elif result.action == "d" and result.item:
            _run(cmd_remove, vault, name=result.item.value.name, yes=False, keep_session=False)
        elif result.action == "p":
            _run(cmd_prune, vault, yes=False, dry_run=False, stale_days=0)


def _run_add(vault: Vault, args) -> None:
    _run(
        cmd_add,
        vault,
        name=None,
        console=False,
        sso=False,
        email=None,
        no_seed=False,
        use=False,
        no_use=True,
        yes=False,
    )


def _run(func, vault: Vault, **kwargs) -> None:
    """Call a command with an ad-hoc argument object, reporting errors inline."""
    try:
        func(vault, argparse.Namespace(**kwargs))
    except ClaudeLoginError as exc:
        ui.error(str(exc))
    print()




def _age_days(iso_value: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0
