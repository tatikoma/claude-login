"""Argument parsing and dispatch for the ``claude-login`` executable."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from . import __version__, commands, ui
from .errors import ClaudeLoginError
from .store import Vault

PROG = "claude-login"

_ALIASES = {
    "ls": "list",
    "run": "use",
    "switch": "use",
    "rm": "remove",
    "delete": "remove",
    "clean": "prune",
    "cleanup": "prune",
    "new": "add",
    "login": "add",
}

#: claude-login's own flags that take a separate value token, so that value is
#: not mistaken for the account name. All are boolean today.
_VALUE_FLAGS: set[str] = set()

_COMMANDS = {
    "add",
    "list",
    "use",
    "rename",
    "remove",
    "prune",
    "env",
    "flags",
    "settings",
    "relink",
    "sync",
    "doctor",
}

_EPILOG = f"""\
examples:
  {PROG}                     pick an account with the arrow keys and launch claude
  {PROG} work                launch claude as the "work" account
  {PROG} work --resume       extra arguments are passed through to claude
  {PROG} add                 sign a new account in (named after its email)
  {PROG} list                show every account, its limits and token health
  {PROG} flags               show the flags claude is launched with
  {PROG} prune               drop accounts whose login has expired

Accounts are named after their email automatically; the list shows how full the
5-hour and weekly rate-limit windows are, with the reset time once one is full.

The flags claude is launched with live in ~/.claude-accounts/accounts.json under
"launchArgs" and are edited in Settings (`s` in the list, or `claude-login
settings`); a fresh install passes none. Flags for claude-login itself go before
the account name; everything after it is passed straight through to claude and
overrides a configured flag of the same name.

Each account gets its own CLAUDE_CONFIG_DIR under ~/.claude-accounts/profiles,
so logins never overwrite each other and two accounts can run side by side in
different terminals. Settings, memory, commands and transcripts stay shared via
symlinks into ~/.claude.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Switch between multiple Claude Code accounts.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    _add_launch_flags(parser)
    _add_usage_flags(parser)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_use = sub.add_parser("use", help="launch claude as a given account")
    p_use.add_argument("name", help="profile name, prefix or email")
    _add_launch_flags(p_use)
    p_use.add_argument("--yes", "-y", action="store_true", help="assume yes for prompts")
    p_use.add_argument("claude_args", nargs=argparse.REMAINDER, help="passed to claude")
    p_use.set_defaults(func=commands.cmd_use)

    p_add = sub.add_parser("add", help="sign in to a new account and register it")
    p_add.add_argument(
        "name", nargs="?", help="override the profile name (default: the account's email)"
    )
    p_add.add_argument("--console", action="store_true", help="use an Anthropic Console account")
    p_add.add_argument("--sso", action="store_true", help="force the SSO login flow")
    p_add.add_argument("--email", help="pre-fill the email on the login page")
    p_add.add_argument(
        "--no-seed",
        action="store_true",
        help="do not copy onboarding/trust settings from ~/.claude.json",
    )
    p_add.add_argument("--use", action="store_true", help="launch claude right after signing in")
    p_add.add_argument("--no-use", action="store_true", help="never offer to launch afterwards")
    p_add.add_argument("--yes", "-y", action="store_true", help="assume yes for prompts")
    p_add.set_defaults(func=commands.cmd_add)

    p_list = sub.add_parser("list", help="show every registered account and its limits")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    _add_usage_flags(p_list)
    p_list.set_defaults(func=commands.cmd_list)

    p_rename = sub.add_parser("rename", help="rename an account")
    p_rename.add_argument("old")
    p_rename.add_argument("new")
    p_rename.set_defaults(func=commands.cmd_rename)

    p_remove = sub.add_parser("remove", help="delete an account and revoke its session")
    p_remove.add_argument("name")
    p_remove.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p_remove.add_argument(
        "--keep-session",
        action="store_true",
        help="delete locally without revoking the token server-side",
    )
    p_remove.set_defaults(func=commands.cmd_remove)

    p_prune = sub.add_parser("prune", help="clean up expired and broken accounts")
    p_prune.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p_prune.add_argument("--dry-run", "-n", action="store_true", help="only show what would go")
    p_prune.add_argument(
        "--stale-days",
        type=int,
        default=0,
        metavar="N",
        help="also drop accounts unused for N days",
    )
    p_prune.set_defaults(func=commands.cmd_prune)

    p_flags = sub.add_parser(
        "flags", help="show or replace the flags every launch passes to claude"
    )
    p_flags.add_argument(
        "flags",
        nargs=argparse.REMAINDER,
        help="new flag list, e.g. `flags -- --effort max --continue` (omit to show)",
    )
    p_flags.add_argument("--clear", action="store_true", help="launch claude with no flags")
    p_flags.set_defaults(func=commands.cmd_flags)

    p_settings = sub.add_parser(
        "settings", help="interactive editor for the launch flags (also `s` in the list)"
    )
    p_settings.set_defaults(func=commands.cmd_settings)

    p_env = sub.add_parser("env", help="print the shell export for an account")
    p_env.add_argument("name")
    p_env.set_defaults(func=commands.cmd_env)

    p_relink = sub.add_parser("relink", help="recreate the shared symlinks into ~/.claude")
    p_relink.add_argument("name", nargs="?", help="default: every account")
    p_relink.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="move diverged private copies into <profile>/.shadowed/ and relink",
    )
    p_relink.set_defaults(func=commands.cmd_relink)

    p_sync = sub.add_parser(
        "sync", help="re-copy trusted folders / MCP servers from ~/.claude.json"
    )
    p_sync.add_argument("name", nargs="?", help="default: every account")
    p_sync.set_defaults(func=commands.cmd_sync)

    p_doctor = sub.add_parser("doctor", help="diagnose the setup")
    p_doctor.set_defaults(func=commands.cmd_doctor)

    return parser


def _add_usage_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-usage",
        dest="no_usage",
        action="store_true",
        default=argparse.SUPPRESS,
        help="skip the rate-limit lookup (offline / faster)",
    )


def _add_launch_flags(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS keeps a subparser from clobbering a value the root parser already
    # read, so `claude-login --no-flags work` behaves as written.
    parser.add_argument(
        "--no-flags",
        dest="no_flags",
        action="store_true",
        default=argparse.SUPPRESS,
        help="ignore the configured launch flags and start claude bare",
    )


def split_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Normalise argv and peel off arguments meant for ``claude`` itself.

    ``claude-login work`` is shorthand for ``claude-login use work``, and a bare
    ``claude-login -- --resume`` forwards ``--resume`` to whichever account the
    picker ends up selecting.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[:index], argv[index + 1 :]
        if token.startswith("-"):
            # Step over a flag's value so it is not mistaken for the command.
            index += 2 if token in _VALUE_FLAGS else 1
            continue
        if token in _COMMANDS or token in _ALIASES:
            return argv[:index] + [_ALIASES.get(token, token)] + argv[index + 1 :], []
        return argv[:index] + ["use"] + argv[index:], []
    return argv, []


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parsed_argv, forwarded = split_args(raw)
    parser = build_parser()
    args = parser.parse_args(parsed_argv)

    # REMAINDER keeps a leading "--" separator; claude does not want it.
    passthrough = list(getattr(args, "claude_args", None) or forwarded)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    args.claude_args = passthrough

    vault = Vault()
    handler = getattr(args, "func", None) or commands.interactive
    try:
        return handler(vault, args) or 0
    except ClaudeLoginError as exc:
        ui.error(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:  # pragma: no cover - `| head` and friends
        return 0
