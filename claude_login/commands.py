"""Implementation of every claude-login subcommand."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import claude_cli, keychain, picker, store, ui, usage
from .errors import ClaudeLoginError, UsageError
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
        launch(vault, profile, [], launch_args_for(vault, args))
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
    status = vault.status(profile)
    if not status.usable and not _offer_login(vault, profile, status, assume_yes=args.yes):
        return 1
    profile = vault.reload().get(profile.name)
    launch(vault, profile, list(args.claude_args or []), launch_args_for(vault, args))
    return 0


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
_SETTINGS_ROWS = (
    *(("toggle", name) for name, _ in TOGGLE_SETTINGS),
    ("effort", "--effort"),
    ("other", None),
)


def _settings_title(vault: Vault) -> str:
    flags = vault.reload().launch_args
    preview = f"claude {' '.join(flags)}".rstrip()
    return "\n".join(
        [
            ui.paint("Settings — launch flags", "bold"),
            "",
            ui.paint(preview, "cyan") if flags else ui.paint(preview + "  (no flags)", "grey"),
        ]
    )


def _settings_items(vault: Vault) -> list[picker.Item]:
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


def cmd_settings(vault: Vault, args=None) -> int:
    """Interactive editor for the flags every launch passes to ``claude``."""
    bootstrap(vault)
    actions = [picker.Action("e", "edit as text"), picker.Action("c", "clear all")]
    selected = 0

    def on_select(index: int) -> bool:
        """Toggle in place; the text row defers so the caller can prompt."""
        kind, name = _SETTINGS_ROWS[index]
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
            lambda: _settings_items(vault),
            title=lambda: _settings_title(vault),
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

    ui.info("")
    if problems:
        ui.warn(f"{problems} issue(s) found — `claude-login prune` cleans up dead profiles")
    else:
        ui.success("everything looks healthy")
    return 0


# --- interactive entry point ----------------------------------------------


def interactive(vault: Vault, args) -> int:
    bootstrap(vault)
    actions = [
        picker.Action("a", "add"),
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
        if result.action == "select" and result.item:
            profile: Profile = result.item.value
            status = vault.status(profile)
            if not status.usable and not _offer_login(vault, profile, status, assume_yes=False):
                continue
            profile = vault.reload().get(profile.name)
            launch(vault, profile, list(args.claude_args or []), launch_args_for(vault, args))
            return 0  # unreachable: exec replaced us
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
