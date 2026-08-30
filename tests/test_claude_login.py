"""Tests for claude-login.

The suite runs against a stub ``claude`` executable so nothing touches your real
accounts.  One extra test exercises the genuine Keychain/`claude` interaction and
only runs when ``CLAUDE_LOGIN_INTEGRATION=1`` is set.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_login import (  # noqa: E402
    claude_app,
    claude_cli,
    cli,
    commands,
    keychain,
    store,
    ui,
    usage,
)
from claude_login.cli import split_args  # noqa: E402
from claude_login.errors import ClaudeAppError, UsageError  # noqa: E402
from claude_login.store import Profile, Vault  # noqa: E402


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

FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Stand-in for the real `claude` CLI: just enough auth surface to test against."""
import json, os, sys, time

config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
creds = os.path.join(config_dir, ".credentials.json") if config_dir else None
argv = sys.argv[1:]

def tokens():
    try:
        with open(creds) as fh:
            return json.load(fh).get("claudeAiOauth", {})
    except (OSError, ValueError):
        return {}

if argv[:2] == ["auth", "login"]:
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        sys.exit(1)
    email = os.environ.get("FAKE_CLAUDE_EMAIL", "someone@example.com")
    now = int(time.time() * 1000)
    with open(creds, "w") as fh:
        json.dump({"claudeAiOauth": {
            "accessToken": "sk-ant-oat01-fake",
            "refreshToken": "sk-ant-ort01-fake",
            "expiresAt": now + 8 * 3600 * 1000,
            "refreshTokenExpiresAt": now + 11 * 86400 * 1000,
            "scopes": ["user:inference"],
            "subscriptionType": os.environ.get("FAKE_CLAUDE_PLAN", "max"),
        }}, fh)
    # The real CLI read-modify-writes this file; mirror that so the test can
    # tell the difference between merging and clobbering.
    path = os.path.join(config_dir, ".claude.json")
    try:
        with open(path) as fh:
            config = json.load(fh)
    except (OSError, ValueError):
        config = {}
    config["oauthAccount"] = {
        "accountUuid": os.environ.get("FAKE_CLAUDE_UUID", "uuid-" + email),
        "emailAddress": email,
        "organizationName": email + "'s Organization",
    }
    with open(path, "w") as fh:
        json.dump(config, fh)
elif argv[:2] == ["auth", "status"]:
    live = tokens()
    print(json.dumps({"loggedIn": bool(live), "email": None,
                      "subscriptionType": live.get("subscriptionType")}))
elif argv[:2] == ["auth", "logout"]:
    try:
        os.unlink(creds)
    except OSError:
        pass
elif argv[:1] == ["--version"]:
    print("0.0.0-fake (Claude Code)")
else:
    sys.exit(0)
'''


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-login-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # A stand-in for ~/.claude that shared links and seeding point at.
        self.home_claude = self.tmp / "home-claude"
        self.home_claude.mkdir()
        (self.home_claude / "settings.json").write_text('{"theme":"dark"}')
        (self.home_claude / "commands").mkdir()
        (self.home_claude / ".config.json").write_text(
            json.dumps({"hasCompletedOnboarding": True, "projects": {"/x": {"trusted": True}}})
        )

        binary = self.tmp / "claude"
        binary.write_text(FAKE_CLAUDE)
        binary.chmod(0o755)

        self._patch(claude_cli, "default_config_dir", lambda: str(self.home_claude))
        self._patch(ui, "is_interactive", lambda: False)
        self._env("CLAUDE_LOGIN_CLAUDE_BIN", str(binary))
        self._env("FAKE_CLAUDE_EMAIL", "one@example.com")
        # The unscoped item name is the developer's own: with ~/.claude signed in
        # on this machine, `adopt_default` would read a real login and every test
        # that counts profiles would shift by one.  A name nothing can match
        # keeps the suite off real accounts — reads miss, and no unit test writes.
        self._env("CLAUDE_LOGIN_KEYCHAIN_PREFIX", f"claude-login-test-{os.getpid()}")

        self.vault = Vault(self.tmp / "vault")
        # Credentials are memoised per process; tests poke the files directly.
        claude_cli.forget_credentials(everything=True)
        self.addCleanup(claude_cli.forget_credentials, everything=True)

    def invalidate(self) -> None:
        """Call after changing credentials behind the library's back."""
        claude_cli.forget_credentials(everything=True)

    def _patch(self, module, name, value) -> None:
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def _env(self, key: str, value: str) -> None:
        original = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(key, original)
            if original is not None
            else os.environ.pop(key, None)
        )

    def add(self, name=None, email: str = "one@example.com", **overrides) -> Profile:
        os.environ["FAKE_CLAUDE_EMAIL"] = email
        args = argparse.Namespace(
            name=name, console=False, sso=False, email=None,
            no_seed=False, use=False, no_use=True, yes=True,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        commands.cmd_add(self.vault, args)
        return self.vault.reload().get(name or email)


class TestServiceNaming(unittest.TestCase):
    def test_default_login_has_no_scope_suffix(self):
        self.assertEqual(claude_cli.credentials_service(None), "Claude Code-credentials")

    def test_scoped_dir_matches_claude_codes_hash(self):
        path = "/Users/someone/.claude-accounts/profiles/work"
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(
            claude_cli.credentials_service(path), f"Claude Code-credentials-{digest}"
        )

    def test_distinct_dirs_never_share_an_item(self):
        self.assertNotEqual(
            claude_cli.credentials_service("/a"), claude_cli.credentials_service("/b")
        )


class TestArgSplitting(unittest.TestCase):
    def test_bare_name_becomes_use(self):
        self.assertEqual(split_args(["work"]), (["use", "work"], []))

    def test_passthrough_after_name(self):
        self.assertEqual(
            split_args(["work", "--resume"]), (["use", "work", "--resume"], [])
        )

    def test_known_command_is_left_alone(self):
        self.assertEqual(split_args(["add", "x"]), (["add", "x"], []))

    def test_alias_is_expanded(self):
        self.assertEqual(split_args(["ls"]), (["list"], []))

    def test_double_dash_forwards_to_the_picker(self):
        self.assertEqual(split_args(["--", "--resume"]), ([], ["--resume"]))

    def test_global_flag_before_name(self):
        self.assertEqual(
            split_args(["--no-skip-permissions", "work"]),
            (["--no-skip-permissions", "use", "work"], []),
        )

    def test_claude_flags_after_the_name_stay_passthrough(self):
        self.assertEqual(
            split_args(["work", "--effort", "high"]),
            (["use", "work", "--effort", "high"], []),
        )

    def test_a_value_taking_own_flag_does_not_swallow_the_command(self):
        # No claude-login flag takes a value today; guard the mechanism anyway.
        original = set(cli._VALUE_FLAGS)
        cli._VALUE_FLAGS.add("--thing")
        self.addCleanup(lambda: (cli._VALUE_FLAGS.clear(), cli._VALUE_FLAGS.update(original)))
        self.assertEqual(
            split_args(["--thing", "value", "work"]),
            (["--thing", "value", "use", "work"], []),
        )
        self.assertEqual(
            split_args(["--thing", "value", "list"]), (["--thing", "value", "list"], [])
        )


class TestAddAndList(Base):
    def test_add_creates_isolated_profile(self):
        profile = self.add("work", "work@example.com")
        self.assertEqual(profile.email, "work@example.com")
        self.assertEqual(profile.subscription_type, "max")
        self.assertEqual(Path(profile.config_dir), self.vault.dir_for("work"))
        self.assertTrue(Path(profile.config_dir, ".credentials.json").exists())
        self.assertEqual(self.vault.status(profile).state, "ok")

    def test_shared_entries_are_symlinked(self):
        profile = self.add("work")
        settings = Path(profile.config_dir) / "settings.json"
        self.assertTrue(settings.is_symlink())
        self.assertEqual(settings.resolve(), (self.home_claude / "settings.json").resolve())
        self.assertTrue((Path(profile.config_dir) / "commands").is_symlink())

    def test_seeding_copies_onboarding_but_not_the_account(self):
        profile = self.add("work")
        config = claude_cli.read_global_config(profile.config_dir)
        self.assertTrue(config["hasCompletedOnboarding"])
        self.assertIn("/x", config["projects"])
        # The account block must come from the login, not from the seed.
        self.assertEqual(config["oauthAccount"]["emailAddress"], "one@example.com")

    def test_no_seed_flag(self):
        profile = self.add("work", no_seed=True)
        config = claude_cli.read_global_config(profile.config_dir)
        self.assertNotIn("hasCompletedOnboarding", config)

    def test_two_accounts_do_not_share_credentials(self):
        first = self.add("work", "work@example.com")
        second = self.add("personal", "personal@example.com")
        self.assertNotEqual(first.config_dir, second.config_dir)
        self.assertNotEqual(
            claude_cli.credentials_service(first.config_dir),
            claude_cli.credentials_service(second.config_dir),
        )
        self.assertEqual(self.vault.reload().get("work").email, "work@example.com")
        self.assertEqual(self.vault.get("personal").email, "personal@example.com")

    def test_duplicate_name_is_rejected(self):
        self.add("work")
        with self.assertRaises(UsageError):
            self.add("work")

    def test_invalid_name_is_rejected(self):
        with self.assertRaises(UsageError):
            self.add("has spaces")

    def test_failed_login_cleans_up(self):
        self._env("FAKE_CLAUDE_FAIL", "1")
        args = argparse.Namespace(
            name="broken", console=False, sso=False, email=None,
            no_seed=False, use=False, no_use=True, yes=True,
        )
        self.assertEqual(commands.cmd_add(self.vault, args), 1)
        self.assertIsNone(self.vault.reload().find("broken"))
        self.assertFalse(self.vault.dir_for("broken").exists())
        self.assertEqual(list(self.vault.staging_dir.glob("*")), [])

    def test_profile_is_named_after_the_email(self):
        profile = self.add(email="work@example.com")
        self.assertEqual(profile.name, "work@example.com")
        self.assertEqual(profile.email, "work@example.com")
        self.assertEqual(Path(profile.config_dir).name, "work@example.com")

    def test_same_account_cannot_be_added_twice(self):
        self.add(email="dup@example.com")
        with self.assertRaises(UsageError):
            self.add(email="dup@example.com")

    def test_second_add_leaves_no_staging_leftovers(self):
        self.add(email="dup@example.com")
        with self.assertRaises(UsageError):
            self.add(email="dup@example.com")
        self.assertEqual(list(self.vault.staging_dir.glob("*")), [])


class TestSharedLinks(Base):
    """Claude Code rewrites files atomically, which silently unlinks them."""

    def _simulate_atomic_rewrite(self, profile: Profile, entry: str, body: str) -> Path:
        target = Path(profile.config_dir) / entry
        scratch = Path(profile.config_dir) / (entry + ".tmp")
        scratch.write_text(body)
        os.replace(scratch, target)  # exactly what Claude Code does
        return target

    def test_atomic_rewrite_breaks_the_link(self):
        profile = self.add("work")
        target = self._simulate_atomic_rewrite(profile, "settings.json", '{"theme":"light"}')
        self.assertFalse(target.is_symlink())
        self.assertEqual((self.home_claude / "settings.json").read_text(), '{"theme":"dark"}')
        _, diverged = self.vault.shared_conflicts(profile)
        self.assertIn("settings.json", diverged)

    def test_identical_copy_is_relinked_silently(self):
        profile = self.add("work")
        self._simulate_atomic_rewrite(profile, "settings.json", '{"theme":"dark"}')
        linked, conflicts = self.vault.link_shared(profile)
        self.assertIn("settings.json", linked)
        self.assertEqual(conflicts, [])
        self.assertTrue((Path(profile.config_dir) / "settings.json").is_symlink())

    def test_diverged_copy_is_reported_not_clobbered(self):
        profile = self.add("work")
        self._simulate_atomic_rewrite(profile, "settings.json", '{"theme":"light"}')
        linked, conflicts = self.vault.link_shared(profile)
        self.assertEqual(conflicts, ["settings.json"])
        self.assertNotIn("settings.json", linked)
        self.assertEqual(
            (Path(profile.config_dir) / "settings.json").read_text(), '{"theme":"light"}'
        )

    def test_relink_force_shadows_the_copy(self):
        profile = self.add("work")
        self._simulate_atomic_rewrite(profile, "settings.json", '{"theme":"light"}')
        commands.cmd_relink(self.vault, argparse.Namespace(name="work", force=True))
        settings = Path(profile.config_dir) / "settings.json"
        self.assertTrue(settings.is_symlink())
        self.assertEqual(settings.read_text(), '{"theme":"dark"}')
        shadowed = list((Path(profile.config_dir) / ".shadowed").glob("settings.json.*"))
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0].read_text(), '{"theme":"light"}')

    def test_relink_restores_a_deleted_link(self):
        profile = self.add("work")
        (Path(profile.config_dir) / "settings.json").unlink()
        commands.cmd_relink(self.vault, argparse.Namespace(name="work", force=False))
        self.assertTrue((Path(profile.config_dir) / "settings.json").is_symlink())


class TestConfigSync(Base):
    """.claude.json cannot be a symlink, so shared keys are pushed on demand."""

    def _main_config(self, **changes) -> None:
        path = self.home_claude / ".config.json"
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))

    def test_sync_brings_over_a_later_mcp_server(self):
        profile = self.add("work")
        self.assertNotIn("mcpServers", claude_cli.read_global_config(profile.config_dir))
        self._main_config(mcpServers={"linear": {"command": "npx"}})
        changed = self.vault.sync_config(profile)
        self.assertIn("mcpServers", changed)
        config = claude_cli.read_global_config(profile.config_dir)
        self.assertEqual(config["mcpServers"], {"linear": {"command": "npx"}})

    def test_sync_adds_new_trusted_folders_without_losing_local_ones(self):
        profile = self.add("work")
        config = claude_cli.read_global_config(profile.config_dir)
        config["projects"]["/x"] = {"trusted": True, "history": ["local prompt"]}
        config["projects"]["/only-here"] = {"trusted": True}
        self.vault._write_global_config(profile, config)

        self._main_config(projects={"/x": {"trusted": True}, "/new": {"trusted": True}})
        self.vault.sync_config(profile)

        projects = claude_cli.read_global_config(profile.config_dir)["projects"]
        self.assertIn("/new", projects)                        # gained from ~/.claude
        self.assertIn("/only-here", projects)                  # profile's own kept
        self.assertEqual(projects["/x"]["history"], ["local prompt"])  # not clobbered

    def test_sync_never_touches_the_account(self):
        profile = self.add("work", "work@example.com")
        self._main_config(mcpServers={"a": {}}, oauthAccount={"emailAddress": "other@x.com"})
        self.vault.sync_config(profile)
        config = claude_cli.read_global_config(profile.config_dir)
        self.assertEqual(config["oauthAccount"]["emailAddress"], "work@example.com")

    def test_sync_is_idempotent(self):
        profile = self.add("work")
        self._main_config(mcpServers={"a": {}})
        self.assertTrue(self.vault.sync_config(profile))
        self.assertEqual(self.vault.sync_config(profile), [])

    def test_sync_skips_the_default_profile(self):
        self.assertEqual(self.vault.sync_config(Profile(name="default")), [])


class TestLaunchArgs(Base):
    """Launch flags come from the registry, never from hardcoded constants."""

    def build(self, profile, passthrough=(), launch=None, cwd=None):
        return commands.build_claude_args(
            profile,
            list(passthrough),
            self.vault.launch_args if launch is None else list(launch),
            cwd=cwd or str(self.tmp),
        )

    def test_a_fresh_vault_launches_claude_bare(self):
        fresh = Vault(self.tmp / "fresh-vault")
        self.assertEqual(fresh.launch_args, [])
        fresh.save()
        self.assertEqual(json.loads(fresh.registry_path.read_text())["launchArgs"], [])

    def test_missing_key_means_no_flags(self):
        profile = self.add("work")
        with self.vault.locked():
            self.vault._data.pop("launchArgs", None)
            self.vault.save()
        self.assertEqual(self.vault.reload().launch_args, [])
        self.assertEqual(self.build(profile), [])

    def test_configured_flags_are_passed_through_verbatim(self):
        profile = self.add("work")
        args = self.build(profile, launch=["--dangerously-skip-permissions", "--effort", "max"])
        self.assertEqual(args, ["--dangerously-skip-permissions", "--effort", "max"])

    def test_passthrough_overrides_a_configured_flag(self):
        profile = self.add("work")
        args = self.build(profile, ["--effort", "low"], launch=["--effort", "max"])
        self.assertEqual(args, ["--effort", "low"])

    def test_profile_extra_args_override_a_configured_flag(self):
        profile = self.add("work")
        profile.extra_args = ["--effort", "high"]
        args = self.build(profile, launch=["--effort", "max"])
        self.assertEqual(args, ["--effort", "high"])

    def test_boolean_flag_is_not_duplicated(self):
        profile = self.add("work")
        args = self.build(
            profile,
            ["--dangerously-skip-permissions"],
            launch=["--dangerously-skip-permissions"],
        )
        self.assertEqual(args.count("--dangerously-skip-permissions"), 1)

    def test_inline_value_form_also_deduplicates(self):
        profile = self.add("work")
        args = self.build(profile, ["--effort=low"], launch=["--effort", "max"])
        self.assertEqual(args, ["--effort=low"])

    def test_passthrough_order(self):
        profile = self.add("work")
        profile.extra_args = ["--model", "opus"]
        args = self.build(profile, ["--resume"], launch=[])
        self.assertEqual(args, ["--model", "opus", "--resume"])

    def test_child_env_points_at_the_profile(self):
        profile = self.add("work")
        env = claude_cli.child_env(profile.config_dir)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], profile.config_dir)

    def test_child_env_drops_ambient_credentials(self):
        self._env("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
        env = claude_cli.child_env(str(self.tmp))
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class TestSettingsEditing(Base):
    """The settings screen edits the launchArgs list; these are its primitives."""

    def test_toggle_on_and_off(self):
        flags = commands.with_flag([], "--continue", "")
        self.assertEqual(flags, ["--continue"])
        self.assertEqual(commands.flag_state(flags, "--continue"), "")
        self.assertEqual(commands.with_flag(flags, "--continue", None), [])

    def test_absent_flag_reads_as_none(self):
        self.assertIsNone(commands.flag_state(["--continue"], "--effort"))

    def test_value_flag_round_trip(self):
        flags = commands.with_flag([], "--effort", "max")
        self.assertEqual(flags, ["--effort", "max"])
        self.assertEqual(commands.flag_state(flags, "--effort"), "max")

    def test_inline_value_is_read(self):
        self.assertEqual(commands.flag_state(["--effort=high"], "--effort"), "high")

    def test_replacing_a_flag_keeps_its_position(self):
        flags = ["--effort", "low", "--continue"]
        self.assertEqual(
            commands.with_flag(flags, "--effort", "max"), ["--effort", "max", "--continue"]
        )

    def test_effort_cycles_through_off(self):
        seen = []
        current = None
        for _ in range(len(commands.EFFORT_LEVELS) + 1):
            current = commands._cycle_effort(current)
            seen.append(current)
        self.assertEqual(seen, [*commands.EFFORT_LEVELS, None])

    def test_other_flags_are_isolated_from_the_known_ones(self):
        flags = ["--dangerously-skip-permissions", "--model", "opus", "--effort", "max"]
        self.assertEqual(commands.other_flags(flags), ["--model", "opus"])
        replaced = commands.with_other_flags(flags, ["--verbose"])
        self.assertEqual(
            replaced, ["--dangerously-skip-permissions", "--effort", "max", "--verbose"]
        )

    def test_settings_rows_line_up_with_the_rendered_items(self):
        self.assertEqual(len(commands._FLAGS_ROWS), len(commands._flags_items(self.vault)))

    def test_rendered_rows_reflect_the_stored_flags(self):
        commands._save_launch_args(self.vault, ["--continue", "--effort", "high"])
        cells = [ui.strip_ansi(item.cells[2]) for item in commands._flags_items(self.vault)]
        self.assertEqual(cells[:3], ["off", "on", "high"])

    def test_title_previews_the_command_line(self):
        commands._save_launch_args(self.vault, ["--continue"])
        self.assertIn("claude --continue", ui.strip_ansi(commands._flags_title(self.vault)))
        commands._save_launch_args(self.vault, [])
        self.assertIn("no flags", ui.strip_ansi(commands._flags_title(self.vault)))


class TestContinueFlag(Base):
    """`--continue` aborts the launch without a transcript, so it is conditional."""

    def _transcript(self, profile: Profile, cwd: Path, *, body: str = '{"x":1}\n') -> Path:
        directory = (
            Path(profile.config_dir) / "projects" / claude_cli.project_dir_name(str(cwd))
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "11111111-2222-3333-4444-555555555555.jsonl"
        path.write_text(body)
        return path

    def build(self, profile, cwd, passthrough=()):
        return commands.build_claude_args(
            profile, list(passthrough), ["--continue"], cwd=str(cwd)
        )

    def test_dropped_without_a_transcript(self):
        profile = self.add("work")
        self.assertEqual(self.build(profile, self.tmp), [])

    def test_kept_once_a_transcript_exists(self):
        profile = self.add("work")
        workdir = self.tmp / "repo"
        workdir.mkdir()
        self._transcript(profile, workdir)
        self.assertEqual(self.build(profile, workdir), ["--continue"])

    def test_empty_transcript_does_not_count(self):
        profile = self.add("work")
        workdir = self.tmp / "repo2"
        workdir.mkdir()
        self._transcript(profile, workdir, body="")
        self.assertEqual(self.build(profile, workdir), [])

    def test_resume_supersedes_continue(self):
        profile = self.add("work")
        workdir = self.tmp / "repo3"
        workdir.mkdir()
        self._transcript(profile, workdir)
        self.assertEqual(self.build(profile, workdir, ["--resume"]), ["--resume"])

    def test_short_form_in_passthrough_is_not_duplicated(self):
        profile = self.add("work")
        workdir = self.tmp / "repo4"
        workdir.mkdir()
        self._transcript(profile, workdir)
        self.assertEqual(self.build(profile, workdir, ["-c"]), ["-c"])

    def test_directory_name_matches_claude_codes_scheme(self):
        self.assertEqual(
            claude_cli.project_dir_name("/Users/someone/projects/claude-login"),
            "-Users-someone-projects-claude-login",
        )

    def test_long_paths_get_a_hash_suffix(self):
        long_path = "/" + "/".join(f"segment{index}" for index in range(40))
        name = claude_cli.project_dir_name(long_path)
        self.assertEqual(len(name.split("-")[0]), 0)  # leading separator became "-"
        self.assertTrue(len(name) > claude_cli.PROJECT_NAME_LIMIT)
        self.assertEqual(name[: claude_cli.PROJECT_NAME_LIMIT], "-".join([""] + [
            f"segment{index}" for index in range(40)
        ])[: claude_cli.PROJECT_NAME_LIMIT])
        # Deterministic and path-specific.
        self.assertEqual(name, claude_cli.project_dir_name(long_path))
        self.assertNotEqual(name, claude_cli.project_dir_name(long_path + "x"))


class TestStatusAndPrune(Base):
    def _expire(self, profile: Profile, *, refresh_ms: int) -> None:
        path = Path(profile.config_dir) / ".credentials.json"
        blob = json.loads(path.read_text())
        blob["claudeAiOauth"]["refreshTokenExpiresAt"] = ui.now_ms() + refresh_ms
        path.write_text(json.dumps(blob))
        self.invalidate()

    def test_expired_refresh_token(self):
        profile = self.add("old")
        self._expire(profile, refresh_ms=-1000)
        self.assertEqual(self.vault.status(profile).state, "expired")

    def test_expiring_soon(self):
        profile = self.add("soon")
        self._expire(profile, refresh_ms=2 * 86_400_000)
        self.assertEqual(self.vault.status(profile).state, "expiring")

    def test_logged_out(self):
        profile = self.add("gone")
        (Path(profile.config_dir) / ".credentials.json").unlink()
        self.invalidate()
        self.assertEqual(self.vault.status(profile).state, "logged-out")

    def test_missing_directory(self):
        profile = self.add("vanished")
        shutil.rmtree(profile.config_dir)
        self.invalidate()
        self.assertEqual(self.vault.status(profile).state, "missing")

    def test_prune_removes_expired_and_keeps_healthy(self):
        healthy = self.add("keep", "keep@example.com")
        doomed = self.add("drop", "drop@example.com")
        self._expire(doomed, refresh_ms=-1)
        args = argparse.Namespace(yes=True, dry_run=False, stale_days=0)
        commands.cmd_prune(self.vault, args)
        names = {p.name for p in self.vault.reload().profiles}
        self.assertIn("keep", names)
        self.assertNotIn("drop", names)
        self.assertFalse(Path(doomed.config_dir).exists())
        self.assertTrue(Path(healthy.config_dir).exists())

    def test_prune_dry_run_changes_nothing(self):
        doomed = self.add("drop")
        self._expire(doomed, refresh_ms=-1)
        args = argparse.Namespace(yes=True, dry_run=True, stale_days=0)
        commands.cmd_prune(self.vault, args)
        self.assertIsNotNone(self.vault.reload().find("drop"))

    def test_prune_collects_orphan_directories(self):
        self.add("real")
        orphan = self.vault.profiles_dir / "leftover"
        orphan.mkdir()
        args = argparse.Namespace(yes=True, dry_run=False, stale_days=0)
        commands.cmd_prune(self.vault, args)
        self.assertFalse(orphan.exists())
        self.assertIsNotNone(self.vault.reload().find("real"))

    def test_stale_days_is_opt_in(self):
        profile = self.add("dusty")
        with self.vault.locked():
            fresh = self.vault.get(profile.name)
            fresh.last_used_at = "2020-01-01T00:00:00+00:00"
            self.vault.upsert(fresh)
            self.vault.save()
        commands.cmd_prune(
            self.vault, argparse.Namespace(yes=True, dry_run=False, stale_days=0)
        )
        self.assertIsNotNone(self.vault.reload().find("dusty"))
        commands.cmd_prune(
            self.vault, argparse.Namespace(yes=True, dry_run=False, stale_days=30)
        )
        self.assertIsNone(self.vault.reload().find("dusty"))


class TestRenameAndRemove(Base):
    def test_rename_moves_the_directory(self):
        self.add("before")
        commands.cmd_rename(self.vault, argparse.Namespace(old="before", new="after"))
        profile = self.vault.reload().get("after")
        self.assertEqual(Path(profile.config_dir), self.vault.dir_for("after"))
        self.assertTrue(Path(profile.config_dir, ".credentials.json").exists())
        self.assertFalse(self.vault.dir_for("before").exists())

    def test_remove_deletes_everything(self):
        profile = self.add("temporary")
        commands.cmd_remove(
            self.vault, argparse.Namespace(name="temporary", yes=True, keep_session=False)
        )
        self.assertIsNone(self.vault.reload().find("temporary"))
        self.assertFalse(Path(profile.config_dir).exists())

    def test_remove_does_not_follow_shared_symlinks(self):
        self.add("temporary")
        commands.cmd_remove(
            self.vault, argparse.Namespace(name="temporary", yes=True, keep_session=False)
        )
        self.assertTrue((self.home_claude / "settings.json").exists())
        self.assertTrue((self.home_claude / "commands").is_dir())

    def test_resolve_by_prefix_and_email(self):
        self.add("production", "ops@example.com")
        self.assertEqual(self.vault.reload().resolve("prod").name, "production")
        self.assertEqual(self.vault.resolve("ops@").name, "production")


class TestRegistry(Base):
    def test_registry_is_private(self):
        self.add("work")
        mode = (self.vault.registry_path.stat().st_mode & 0o777)
        self.assertEqual(mode, 0o600)

    def test_registry_survives_corruption(self):
        self.add("work")
        self.vault.registry_path.write_text("{ this is not json")
        self.assertEqual(self.vault.reload().profiles, [])

    def test_name_validation(self):
        for good in ("work", "work-2", "a.b_c", "A1", "work@example.com", "me+dev@x.io"):
            self.assertEqual(store.validate_name(good), good)
        for bad in ("", "-lead", ".hidden", "has space", "sla/sh", "a\\b", "x" * 65):
            with self.assertRaises(UsageError):
                store.validate_name(bad)

    def test_email_named_profile_resolves_by_prefix(self):
        profile = self.add(email="work@example.com")
        self.assertEqual(self.vault.status(profile).state, "ok")
        # The short prefix still resolves it, so you never type the whole thing.
        self.assertEqual(self.vault.reload().resolve("wo").name, "work@example.com")


class TestTableLayout(unittest.TestCase):
    """Headers are centred over their column; data stays left-aligned."""

    ROWS = [["me@home.com", "max"], ["work@example.com", "team"]]

    def test_pad_alignments(self):
        self.assertEqual(ui.pad("ab", 6), "ab    ")
        self.assertEqual(ui.pad("ab", 6, ">"), "    ab")
        self.assertEqual(ui.pad("ab", 6, "^"), "  ab  ")

    def test_pad_ignores_colour_codes(self):
        painted = "\x1b[32mab\x1b[0m"
        self.assertEqual(ui.width(ui.pad(painted, 6, "^")), 6)

    def test_odd_padding_favours_the_left(self):
        self.assertEqual(ui.pad("ab", 5, "^"), " ab  ")

    def test_header_is_centred_over_the_column(self):
        table = ui.render_table(["ACCOUNT", "PLAN"], self.ROWS).splitlines()
        header, first = table[0], table[1]
        self.assertEqual(header.index("ACCOUNT"), 4)  # (16 - 7) // 2
        self.assertEqual(first.index("me@home.com"), 0)

    def test_header_line_has_no_trailing_padding(self):
        header = ui.render_table(["ACCOUNT", "PLAN"], self.ROWS).splitlines()[0]
        self.assertEqual(header, header.rstrip())

    def test_table_without_headers_still_renders(self):
        self.assertEqual(ui.render_table([], [["a", "b"]]), "a  b")


class TestUsageRendering(unittest.TestCase):
    """Parsing and formatting of the /api/oauth/usage payload."""

    SAMPLE = {
        "five_hour": {"utilization": 4.0, "resets_at": "2026-07-26T10:10:00+00:00"},
        "seven_day": {"utilization": 96.0, "resets_at": "2026-07-28T20:00:00+00:00"},
        "limits": [
            {"kind": "session", "percent": 4, "resets_at": "2026-07-26T10:10:00+00:00"},
            {"kind": "weekly_all", "percent": 96, "resets_at": "2026-07-28T20:00:00+00:00"},
            {
                "kind": "weekly_scoped",
                "percent": 100,
                "resets_at": "2026-07-28T20:00:00+00:00",
                "scope": {"model": {"display_name": "Fable"}},
            },
        ],
    }

    def parse(self, payload=None):
        return usage.parse(payload or self.SAMPLE, fetched_at=ui.now_ms())

    def test_windows_are_extracted(self):
        parsed = self.parse()
        self.assertEqual(parsed.session.percent, 4.0)
        self.assertEqual(parsed.weekly.percent, 96.0)
        self.assertIsNotNone(parsed.session.resets_at)

    def test_stricter_per_model_cap_is_kept(self):
        parsed = self.parse()
        self.assertIsNotNone(parsed.weekly_scoped)
        self.assertEqual(parsed.weekly_scoped.scope, "Fable")
        self.assertEqual(parsed.weekly_scoped.percent, 100.0)

    def test_looser_per_model_cap_is_dropped(self):
        payload = json.loads(json.dumps(self.SAMPLE))
        payload["limits"][2]["percent"] = 10
        self.assertIsNone(self.parse(payload).weekly_scoped)

    def test_reset_time_is_always_shown(self):
        # Pinned to today's calendar day: `now + 2h` crossed midnight when the
        # suite ran late in the evening, and the renderer rightly added the date.
        soon = datetime.now().astimezone().replace(hour=23, minute=59, second=0, microsecond=0)
        rendered = ui.strip_ansi(usage.render(usage.Window(42.0, soon), weekly=False))
        self.assertEqual(rendered.strip(), f"42% ⟳ {soon.strftime('%H:%M')}")

    def test_weekly_reset_carries_the_date(self):
        moment = datetime(2026, 7, 26, 14, 20, tzinfo=timezone.utc).astimezone()
        rendered = ui.strip_ansi(usage.render(usage.Window(100.0, moment), weekly=True))
        self.assertEqual(rendered.strip(), f"100% ⟳ {moment.strftime('%d.%m %H:%M')}")

    def test_five_hour_reset_gains_a_date_when_not_today(self):
        tomorrow = datetime.now().astimezone() + timedelta(days=1)
        rendered = ui.strip_ansi(usage.render(usage.Window(10.0, tomorrow), weekly=False))
        self.assertEqual(rendered.strip(), f"10% ⟳ {tomorrow.strftime('%d.%m %H:%M')}")

    def test_untouched_window_shows_a_placeholder(self):
        """An unused 5-hour window has not started, so the API sends no reset."""
        rendered = ui.strip_ansi(usage.render(usage.Window(0.0, None), weekly=False))
        self.assertEqual(rendered.strip(), "0% ⟳ —")

    def test_percentages_are_right_aligned_to_a_fixed_field(self):
        """Monospace lists only line up if 0%, 92% and 100% take the same room."""
        moment = datetime.now().astimezone() + timedelta(hours=1)
        rendered = [
            ui.strip_ansi(usage.render(usage.Window(value, moment), weekly=False))
            for value in (0.0, 4.0, 40.0, 100.0)
        ]
        self.assertEqual([line.index("⟳") for line in rendered], [5] * 4)
        self.assertEqual(rendered[0][:4], "  0%")
        self.assertEqual(rendered[3][:4], "100%")

    def test_unknown_value_keeps_the_same_field_width(self):
        dash = ui.strip_ansi(usage.render(None, weekly=False))
        percent = ui.strip_ansi(usage.render(usage.Window(7.0, None), weekly=False))
        self.assertEqual(len(dash), usage.PERCENT_WIDTH)
        self.assertEqual(dash.rstrip(), dash)  # right-aligned like a number
        self.assertTrue(percent.startswith(" " * (usage.PERCENT_WIDTH - 2)))

    def test_weekly_column_leads_with_the_overall_cap(self):
        session, week = usage.cells(self.parse())
        self.assertTrue(ui.strip_ansi(session).strip().startswith("4% ⟳ "))
        plain = ui.strip_ansi(week).strip()
        self.assertTrue(plain.startswith("96% ⟳ "), plain)
        self.assertIn("Fable 100%", plain)

    def test_shared_reset_is_not_printed_twice(self):
        plain = ui.strip_ansi(usage.cells(self.parse())[1])
        self.assertEqual(plain.count("⟳"), 1, plain)

    def test_differing_scoped_reset_is_printed(self):
        payload = json.loads(json.dumps(self.SAMPLE))
        payload["limits"][2]["resets_at"] = "2026-07-30T20:00:00+00:00"
        plain = ui.strip_ansi(usage.cells(self.parse(payload))[1])
        self.assertEqual(plain.count("⟳"), 2, plain)

    def test_maxed_overall_weekly_does_not_repeat_the_model(self):
        payload = json.loads(json.dumps(self.SAMPLE))
        payload["seven_day"]["utilization"] = 100
        _, week = usage.cells(self.parse(payload))
        plain = ui.strip_ansi(week)
        self.assertTrue(plain.startswith("100% ⟳"), plain)
        self.assertNotIn("Fable", plain)

    def test_unknown_usage_renders_a_dash(self):
        session, week = usage.cells(None)
        self.assertEqual(ui.strip_ansi(session).strip(), "—")
        self.assertEqual(ui.strip_ansi(week).strip(), "—")

    def test_pending_lookup_shows_an_ellipsis(self):
        session, week = usage.cells(None, pending=True)
        self.assertEqual(ui.strip_ansi(session).strip(), "…")
        self.assertEqual(ui.strip_ansi(week).strip(), "…")

    def test_failed_lookup_shows_a_dash_not_a_stale_number(self):
        session, week = usage.cells(None, pending=False)
        self.assertEqual(ui.strip_ansi(session).strip(), "—")
        self.assertEqual(ui.strip_ansi(week).strip(), "—")

    def test_percent_colour_escalates(self):
        self.assertEqual(usage._style(10), ("green",))
        self.assertEqual(usage._style(70), ("yellow",))
        self.assertEqual(usage._style(90), ("red",))
        self.assertEqual(usage._style(100), ("red", "bold"))


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

    def test_unknown_target_cannot_be_set(self):
        with self.assertRaises(UsageError):
            self.vault.load().set_launch_target("browser")

    def test_app_shared_defaults_to_the_built_in_list(self):
        self.assertEqual(self.vault.app_shared, claude_app.DEFAULT_APP_SHARED)

    def test_app_env_defaults_to_empty_and_round_trips(self):
        self.assertEqual(self.vault.app_env, {})
        with self.vault.locked():
            self.vault.set_app_env({"FOO": "bar"})
            self.vault.save()
        self.assertEqual(self.vault.reload().app_env, {"FOO": "bar"})

    def test_sessions_are_shared_and_per_account_by_default(self):
        self.assertIsNone(self.vault.app_shared_accounts)
        self.assertIsNone(self.vault.app_open_accounts)
        self.assertTrue(self.vault.sharing_enabled)
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
            self.vault.set_app_per_account(False)
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


class _AppBase(Base):
    """Base with a stand-in for the machine-wide app support directory."""

    def setUp(self) -> None:
        super().setUp()
        self.app_support = self.tmp / "app-support"
        self.app_support.mkdir()
        self._env("CLAUDE_LOGIN_APP_SUPPORT", str(self.app_support))

    def fake_bundle(self) -> Path:
        bundle = self.tmp / "Fake.app"
        binary = bundle / claude_app.BINARY_SUBPATH
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self._env("CLAUDE_LOGIN_APP_PATH", str(bundle))
        return bundle

    def leaf(self, data_dir: Path, account: str, org: str, *, agent=False) -> Path:
        kind = (
            claude_app.AGENT_SESSIONS_DIRNAME if agent else claude_app.SESSIONS_DIRNAME
        )
        leaf = data_dir / kind / account / org
        leaf.mkdir(parents=True)
        return leaf

    @staticmethod
    def chat(leaf: Path, name: str, activity: int = 0) -> Path:
        path = leaf / f"{name}.json"
        path.write_text(json.dumps({"sessionId": name, "lastActivityAt": activity}))
        return path

    def pooled(self, org: str, name: str) -> Path:
        """Where a chat ends up: the pool holds one directory per organisation."""
        return self.vault.pool_dir(claude_app.SESSIONS_DIRNAME) / org / f"{name}.json"

    @staticmethod
    def is_pooled(leaf: Path) -> bool:
        """True when a chat directory reaches the pool through its account link.

        The link is on the account, never on the leaf: a symlinked leaf is the
        one thing the app refuses to write to.
        """
        return leaf.parent.is_symlink() and not leaf.is_symlink()


class TestAppSharedLinks(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        (self.app_support / "claude-code" / "2.1.0").mkdir(parents=True)
        (self.app_support / "Claude Extensions").mkdir()
        (self.app_support / "claude_desktop_config.json").write_text('{"mcpServers":{}}')
        (self.app_support / "config.json").write_text('{"oauth:tokenCacheV2":"djEw"}')

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
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mcpServers":{}}')
        with self.vault.locked():
            _, conflicts = self.vault.link_app_shared(profile)
        self.assertEqual(conflicts, [])
        self.assertTrue((target / "claude_desktop_config.json").is_symlink())

    def test_diverged_copy_is_reported_not_clobbered(self):
        profile = self.add(email="one@example.com")
        target = Path(self.vault.app_data_dir_for(profile))
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mine":true}')
        with self.vault.locked():
            _, conflicts = self.vault.link_app_shared(profile, repair=True)
        self.assertEqual(conflicts, [])
        self.assertTrue((target / "claude_desktop_config.json").is_symlink())
        self.assertTrue(list((target / ".shadowed").iterdir()))

    def test_default_profile_is_the_source_not_a_target(self):
        profile = Profile(name="default", config_dir=None)
        with self.vault.locked():
            self.assertEqual(self.vault.link_app_shared(profile), ([], []))

    def test_profile_directory_is_private(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.link_app_shared(profile)
        mode = Path(self.vault.app_data_dir_for(profile)).stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_conflicts_are_reported_without_touching_anything(self):
        profile = self.add(email="one@example.com")
        target = Path(self.vault.app_data_dir_for(profile))
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        (target / "claude_desktop_config.json").write_text('{"mine":true}')
        missing, diverged = self.vault.app_shared_conflicts(profile)
        self.assertIn("claude-code", missing)
        self.assertIn("claude_desktop_config.json", diverged)


class TestSessionPool(_AppBase):
    def test_existing_chats_move_into_the_pool(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertTrue(self.pooled("org-1", "local_one").is_file())
        self.assertTrue(self.is_pooled(leaf))
        self.assertEqual(os.path.realpath(leaf.parent), os.path.realpath(pool))
        self.assertEqual(plan.moved, 1)

    def test_the_leaf_the_app_writes_to_stays_a_real_directory(self):
        """The app opens it with O_DIRECTORY|O_NOFOLLOW, so a link there is ENOTDIR
        and every single chat save fails.  The link belongs one level up."""
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        self.vault.wire_session_pool(str(self.app_support))
        self.assertFalse(claude_app.rejects_leaf(leaf))
        self.assertTrue(leaf.is_dir())
        self.assertTrue(leaf.parent.is_symlink())

    def test_two_accounts_of_one_organisation_end_up_in_one_directory(self):
        first = self.leaf(self.app_support, "acct-a", "org-1")
        second = self.leaf(self.app_support, "acct-b", "org-1")
        self.chat(first, "local_one", 10)
        self.chat(second, "local_two", 20)
        self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertEqual(
            sorted(p.name for p in (pool / "org-1").iterdir()),
            ["local_one.json", "local_two.json"],
        )
        self.assertEqual(os.path.realpath(first), os.path.realpath(second))

    def test_different_organisations_keep_their_own_lists(self):
        """Not a shortcoming we can design away: the organisation uuid is the last
        component of the path, and only the app is allowed to create it."""
        first = self.leaf(self.app_support, "acct-a", "org-1")
        second = self.leaf(self.app_support, "acct-b", "org-2")
        self.chat(first, "local_one", 10)
        self.chat(second, "local_two", 20)
        self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertEqual(sorted(p.name for p in pool.iterdir()), ["org-1", "org-2"])
        self.assertNotEqual(os.path.realpath(first), os.path.realpath(second))

    def test_backup_is_taken_before_the_first_move(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertIsNotNone(plan.backup)
        self.assertTrue(list(Path(plan.backup).rglob("local_one.json")))

    def test_dry_run_changes_nothing(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        plan = self.vault.wire_session_pool(str(self.app_support), dry_run=True)
        self.assertEqual(plan.moved, 1)
        self.assertFalse(leaf.is_symlink())
        self.assertTrue((leaf / "local_one.json").is_file())
        self.assertFalse(self.vault.pool_dir(claude_app.SESSIONS_DIRNAME).exists())

    def test_running_twice_is_idempotent(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        self.vault.wire_session_pool(str(self.app_support))
        again = self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual(again.moved, 0)
        self.assertTrue(self.is_pooled(leaf))

    def test_collision_keeps_the_newer_chat(self):
        first = self.leaf(self.app_support, "acct-a", "org-1")
        second = self.leaf(self.app_support, "acct-b", "org-1")
        self.chat(first, "local_same", 10)
        self.chat(second, "local_same", 99)
        plan = self.vault.wire_session_pool(str(self.app_support))
        kept = json.loads(self.pooled("org-1", "local_same").read_text())
        self.assertEqual(kept["lastActivityAt"], 99)
        self.assertEqual(plan.collisions, 1)

    def test_the_loser_of_a_clash_is_kept_aside(self):
        first = self.leaf(self.app_support, "acct-a", "org-1")
        second = self.leaf(self.app_support, "acct-b", "org-1")
        self.chat(first, "local_same", 10)
        self.chat(second, "local_same", 99)
        self.vault.wire_session_pool(str(self.app_support))
        attic = self.vault.root / store.APP_SHARED_DIRNAME / "collisions"
        self.assertTrue(list(attic.iterdir()))

    def test_lazily_adopts_a_directory_the_app_created_later(self):
        self.vault.wire_session_pool(str(self.app_support))
        leaf = self.leaf(self.app_support, "acct-new", "org-9")
        self.chat(leaf, "local_late", 5)
        self.vault.wire_session_pool(str(self.app_support))
        self.assertTrue(self.pooled("org-9", "local_late").is_file())
        self.assertTrue(self.is_pooled(leaf))

    def test_only_the_code_tab_index_is_pooled(self):
        self.assertEqual(list(store.POOL_DIRNAMES), [claude_app.SESSIONS_DIRNAME])

    def test_nothing_to_do_when_the_app_has_never_run(self):
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual((plan.moved, plan.linked, plan.collisions), (0, 0, 0))
        self.assertIsNone(plan.backup)

    def test_unwiring_returns_the_chats_to_the_profile(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual(self.vault.unwire_session_pool(str(self.app_support)), 1)
        self.assertFalse(leaf.parent.is_symlink())
        self.assertTrue((leaf / "local_one.json").is_file())

    def test_a_directory_inside_a_leaf_is_never_deleted(self):
        """The bug this guards cost real data: a leaf holding a whole workspace
        had its subdirectories removed because only files were moved."""
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        workspace = leaf / "cowork_plugins" / "marketplaces"
        workspace.mkdir(parents=True)
        (workspace / "keep-me.txt").write_text("precious")
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertTrue((workspace / "keep-me.txt").is_file())
        self.assertFalse(leaf.parent.is_symlink())
        self.assertTrue(any("cowork_plugins" in entry for entry in plan.unmoved))

    def test_agent_mode_sessions_are_left_alone_entirely(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1", agent=True)
        self.chat(leaf, "local_agent", 1)
        (leaf / "rpm").mkdir()
        self.vault.wire_session_pool(str(self.app_support))
        self.assertFalse(leaf.is_symlink())
        self.assertFalse(leaf.parent.is_symlink())
        self.assertTrue((leaf / "local_agent.json").is_file())
        self.assertTrue((leaf / "rpm").is_dir())

    def test_scheduled_tasks_travel_with_the_chats(self):
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        (leaf / "scheduled-tasks.json").write_text("{}")
        self.vault.wire_session_pool(str(self.app_support))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertTrue((pool / "org-1" / "scheduled-tasks.json").is_file())

    def test_a_flat_pool_is_split_per_organisation(self):
        """Pools written before the app refused symlinked leaves are one pile of
        files.  Nothing in a chat says which organisation it belongs to and every
        account has been reading the same list, so each organisation gets it."""
        self.leaf(self.app_support, "acct-a", "org-1")
        self.leaf(self.app_support, "acct-b", "org-2")
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        pool.mkdir(parents=True)
        self.chat(pool, "local_old", 7)
        self.assertEqual(self.vault.migrate_flat_pool(), 1)
        self.assertTrue(self.pooled("org-1", "local_old").is_file())
        self.assertTrue(self.pooled("org-2", "local_old").is_file())
        self.assertFalse((pool / "local_old.json").exists())

    def test_splitting_a_flat_pool_twice_changes_nothing(self):
        self.leaf(self.app_support, "acct-a", "org-1")
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        pool.mkdir(parents=True)
        self.chat(pool, "local_old", 7)
        self.vault.migrate_flat_pool()
        self.assertEqual(self.vault.migrate_flat_pool(), 0)

    def test_an_older_backup_does_not_block_the_link(self):
        """A `.replaced-` directory from an earlier fold-in is ours, not unknown
        data: the account directory is renamed aside so nothing is deleted, and
        the link still gets made."""
        leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        backup = leaf.parent / f"org-1{claude_app.REPLACED_MARKER}1"
        backup.mkdir()
        (backup / "keep-me.json").write_text("{}")
        plan = self.vault.wire_session_pool(str(self.app_support))
        self.assertEqual(plan.unmoved, [])
        self.assertTrue(self.is_pooled(leaf))
        root = self.app_support / claude_app.SESSIONS_DIRNAME
        aside = list(root.glob(f"acct-a{claude_app.REPLACED_MARKER}*"))
        self.assertEqual(len(aside), 1)
        carried = aside[0] / f"org-1{claude_app.REPLACED_MARKER}1" / "keep-me.json"
        self.assertTrue(carried.is_file())

    def test_a_dead_agent_mode_link_is_removed(self):
        """Left over from when agent-mode was pooled too.  It points nowhere, so
        it holds nothing — but while it is there the app saves no agent-mode
        session at all, because it refuses a symlinked directory outright."""
        account = self.app_support / claude_app.AGENT_SESSIONS_DIRNAME / "acct-a"
        account.mkdir(parents=True)
        (account / "org-1").symlink_to(self.tmp / "gone")
        self.assertEqual(
            self.vault.clear_agent_pool_links(str(self.app_support)), (1, 0)
        )
        self.assertFalse((account / "org-1").is_symlink())

    def test_an_agent_mode_link_that_still_resolves_is_left_for_a_human(self):
        target = self.tmp / "agent-pool"
        target.mkdir()
        account = self.app_support / claude_app.AGENT_SESSIONS_DIRNAME / "acct-a"
        account.mkdir(parents=True)
        (account / "org-1").symlink_to(target)
        self.assertEqual(
            self.vault.clear_agent_pool_links(str(self.app_support)), (0, 1)
        )
        self.assertTrue((account / "org-1").is_symlink())

    def test_a_leaf_linked_the_old_way_is_rewired(self):
        """The layout that broke on 1.25927: the link sat on the leaf itself."""
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        pool.mkdir(parents=True)
        self.chat(pool, "local_old", 7)
        account = self.app_support / claude_app.SESSIONS_DIRNAME / "acct-a"
        account.mkdir(parents=True)
        leaf = account / "org-1"
        leaf.symlink_to(pool)
        self.vault.wire_session_pool(str(self.app_support))
        self.assertTrue(self.is_pooled(leaf))
        self.assertTrue(self.pooled("org-1", "local_old").is_file())


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


class TestAppCommands(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        self._patch(claude_app, "running_pids", lambda: [])
        self._patch(claude_app, "running_data_dirs", set)
        self._patch(claude_app, "is_in_use", lambda _dir: False)

    def test_adopt_dry_run_touches_nothing(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_one")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=True, yes=True))
        self.assertFalse(leaf.is_symlink())

    def test_adopt_moves_chats_into_the_pool(self):
        # No profiles at all: the chats that matter most predate the tool, and
        # they live in the machine-wide directory.
        self.assertEqual(self.vault.profiles, [])
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_one")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertTrue(self.is_pooled(leaf))
        self.assertTrue(self.pooled("org", "local_one").is_file())

    def test_pool_link_is_created_before_the_app_exists(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            stored = self.vault.get(profile.name)
            stored.account_uuid, stored.org_uuid = "acct-x", "org-y"
            self.vault.upsert(stored)
            self.vault.save()
        commands.cmd_app_link(
            self.vault.reload(), argparse.Namespace(name=profile.name)
        )
        link = (
            Path(self.vault.app_data_dir_for(profile))
            / claude_app.SESSIONS_DIRNAME
            / "acct-x"
        )
        self.assertTrue(link.is_symlink())
        self.assertEqual(
            os.path.realpath(link),
            os.path.realpath(self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)),
        )

    def test_org_uuid_is_learnt_from_a_directory_on_disk(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            stored = self.vault.get(profile.name)
            stored.account_uuid = "acct-x"
            self.vault.upsert(stored)
            self.vault.save()
        self.leaf(self.app_support, "acct-x", "org-from-disk")
        resolved = commands.resolve_org_uuid(
            self.vault.reload(), self.vault.get(profile.name)
        )
        self.assertEqual(resolved, "org-from-disk")
        self.assertEqual(self.vault.reload().get(profile.name).org_uuid, "org-from-disk")

    def test_link_reports_when_the_org_is_unknown(self):
        profile = self.add(email="one@example.com")
        self._patch(usage, "fetch_org_uuid", lambda _p: None)
        commands.cmd_app_link(self.vault, argparse.Namespace(name=profile.name))
        data_dir = Path(self.vault.app_data_dir_for(profile))
        self.assertEqual(claude_app.session_leaf_dirs(str(data_dir)), [])

    def _transcript(self, session_id: str) -> None:
        project = self.home_claude / "projects" / "-w"
        project.mkdir(parents=True, exist_ok=True)
        (project / f"{session_id}.jsonl").write_text('{"type":"user"}\n')

    def test_a_chat_without_a_transcript_is_kept_out_of_the_pool(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        alive = leaf / "local_alive.json"
        alive.write_text(json.dumps({"sessionId": "local_alive", "cliSessionId": "live-1"}))
        dead = leaf / "local_dead.json"
        dead.write_text(json.dumps({"sessionId": "local_dead", "cliSessionId": "gone-1"}))
        self._transcript("live-1")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertTrue(self.pooled("org", "local_alive").is_file())
        self.assertFalse(self.pooled("org", "local_dead").exists())
        attic = self.vault.root / store.APP_SHARED_DIRNAME / "orphans"
        self.assertTrue((attic / "local_dead.json").is_file())

    def test_an_empty_transcript_does_not_count_as_alive(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        (leaf / "local_dead.json").write_text(
            json.dumps({"sessionId": "local_dead", "cliSessionId": "empty-1"})
        )
        project = self.home_claude / "projects" / "-w"
        project.mkdir(parents=True, exist_ok=True)
        (project / "empty-1.jsonl").write_text("")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertFalse((pool / "local_dead.json").exists())

    def test_a_chat_without_a_cli_session_id_is_left_alone(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_plain")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertTrue(self.pooled("org", "local_plain").is_file())

    def test_sweep_dry_run_moves_nothing(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        (leaf / "local_dead.json").write_text(
            json.dumps({"sessionId": "local_dead", "cliSessionId": "gone-1"})
        )
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=True, yes=True))
        self.assertFalse(
            (self.vault.root / store.APP_SHARED_DIRNAME / "orphans").exists()
        )

    def test_pending_labels_flag_unpooled_chats(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_one")
        self.assertEqual(commands.pending_pool_labels(self.vault), ["machine-wide"])
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertEqual(commands.pending_pool_labels(self.vault), [])

    def test_data_dirs_always_start_with_the_machine_wide_one(self):
        self.add(email="one@example.com")
        labels = [label for label, _ in commands.app_data_dirs(self.vault)]
        self.assertEqual(labels[0], "machine-wide")
        self.assertEqual(len(labels), 2)

    def test_data_dirs_collapse_duplicates(self):
        self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.set_app_per_account(False)
            self.vault.save()
        self.assertEqual(len(commands.app_data_dirs(self.vault.reload())), 1)

    def _signed_in_as(self, account: str) -> None:
        (self.app_support / "config.json").write_text(
            json.dumps({"oauth:tokenCacheV2": "djEw", "lastKnownAccountUuid": account})
        )
        self._patch(claude_app, "is_in_use", lambda _dir: True)

    def test_the_signed_in_accounts_chats_are_folded_in_by_copy(self):
        leaf = self.leaf(self.app_support, "acct-live", "org")
        self.chat(leaf, "local_one")
        self._signed_in_as("acct-live")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertTrue(self.is_pooled(leaf))
        self.assertTrue(self.pooled("org", "local_one").is_file())

    def test_the_original_of_a_live_directory_is_kept(self):
        leaf = self.leaf(self.app_support, "acct-live", "org")
        self.chat(leaf, "local_one")
        self._signed_in_as("acct-live")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        root = self.app_support / claude_app.SESSIONS_DIRNAME
        aside = [p for p in root.iterdir() if p.name.startswith("acct-live.replaced-")]
        self.assertEqual(len(aside), 1)
        self.assertTrue((aside[0] / "org" / "local_one.json").is_file())

    def test_a_dormant_account_moves_while_another_is_signed_in(self):
        live = self.leaf(self.app_support, "acct-live", "org")
        dormant = self.leaf(self.app_support, "acct-dormant", "org")
        self.chat(live, "local_live")
        self.chat(dormant, "local_dormant")
        self._signed_in_as("acct-live")
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        pool = self.vault.pool_dir(claude_app.SESSIONS_DIRNAME)
        self.assertTrue(self.is_pooled(dormant))
        self.assertTrue(self.is_pooled(live))
        self.assertEqual(
            sorted(p.name for p in (pool / "org").iterdir()),
            ["local_dormant.json", "local_live.json"],
        )
        # The dormant one was moved, the live one only copied.
        root = self.app_support / claude_app.SESSIONS_DIRNAME
        self.assertFalse(list(root.glob("acct-dormant.replaced-*")))
        self.assertTrue(list(root.glob("acct-live.replaced-*")))

    def test_an_unreadable_signed_in_account_stops_everything(self):
        leaf = self.leaf(self.app_support, "acct-a", "org")
        self.chat(leaf, "local_one")
        # In use, but config.json says nothing about who is signed in.
        self._patch(claude_app, "is_in_use", lambda _dir: True)
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertFalse(leaf.is_symlink())

    def test_a_free_profile_is_pooled_while_another_stays_open(self):
        machine_leaf = self.leaf(self.app_support, "acct-a", "org-1")
        self.chat(machine_leaf, "local_machine")
        profile = self.add(email="one@example.com")
        profile_dir = Path(self.vault.app_data_dir_for(profile))
        profile_leaf = self.leaf(profile_dir, "acct-b", "org-2")
        self.chat(profile_leaf, "local_profile")
        busy = os.path.normpath(str(self.app_support))
        self._patch(
            claude_app, "is_in_use", lambda path: os.path.normpath(path) == busy
        )
        commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True))
        self.assertFalse(machine_leaf.parent.is_symlink())
        self.assertTrue(self.is_pooled(profile_leaf))

    def test_adopt_is_a_no_op_when_there_is_nothing_to_pool(self):
        self.assertEqual(
            commands.cmd_app_adopt(self.vault, argparse.Namespace(dry_run=False, yes=True)),
            0,
        )

    def test_status_runs_without_profiles(self):
        self.assertEqual(commands.cmd_app_status(self.vault, argparse.Namespace()), 0)

    def test_status_lists_a_profile(self):
        self.add(email="one@example.com")
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

    def test_bare_app_shows_status(self):
        argv, _ = split_args(["app"])
        args = cli.build_parser().parse_args(argv)
        self.assertIs(args.func, commands.cmd_app_status)

    def test_chats_state_reports_private_before_pooling(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_one")
        state = commands._chats_state(self.vault, str(self.app_support))
        self.assertEqual(ui.strip_ansi(state), "private")

    def test_chats_state_reports_shared_once_the_account_is_linked(self):
        leaf = self.leaf(self.app_support, "acct", "org")
        self.chat(leaf, "local_one")
        self.vault.wire_session_pool(str(self.app_support))
        state = commands._chats_state(self.vault, str(self.app_support))
        self.assertEqual(ui.strip_ansi(state), "shared")

    def test_doctor_mentions_the_app(self):
        self.add(email="one@example.com")
        self.assertEqual(commands.cmd_doctor(self.vault, argparse.Namespace()), 0)

    def test_doctor_flags_a_chat_directory_the_app_refuses(self):
        """It used to call this healthy while every chat save failed with ENOTDIR
        — the only place that said so was the app's own log."""
        leaf = self.leaf(self.app_support, "acct", "org")
        healthy = commands._doctor_app(self.vault)
        leaf.rmdir()
        leaf.symlink_to(self.vault.pool_dir(claude_app.SESSIONS_DIRNAME))
        self.assertEqual(commands._doctor_app(self.vault), healthy + 1)


class TestSettingsSections(Base):
    def test_target_row_cycles_through_the_targets(self):
        self.assertEqual(commands.cycle_target("cli"), "app")
        self.assertEqual(commands.cycle_target("app"), "ask")
        self.assertEqual(commands.cycle_target("ask"), "cli")

    def test_section_rows_describe_the_current_state(self):
        rows = commands._settings_sections(self.vault)
        self.assertEqual(len(rows), len(commands.SETTINGS_SECTIONS))
        self.assertEqual(ui.strip_ansi(rows[0].cells[1]), "CLI")
        self.assertEqual(ui.strip_ansi(rows[1].cells[1]), "none")
        self.assertIn("sharing chats", ui.strip_ansi(rows[2].cells[1]))

    def test_app_rows_count_the_chosen_accounts(self):
        self.add(email="one@example.com")
        rows = commands._app_settings_items(self.vault.reload())
        self.assertEqual(len(rows), len(commands._APP_SETTINGS_ROWS))
        self.assertEqual(ui.strip_ansi(rows[1].cells[1]), "1 of 1")
        with self.vault.locked():
            self.vault.set_app_shared_accounts([])
            self.vault.save()
        rows = commands._app_settings_items(self.vault.reload())
        self.assertEqual(ui.strip_ansi(rows[2].cells[1]), "0 of 1")

    def test_env_parsing_ignores_junk(self):
        self.assertEqual(
            commands.parse_env("FOO=bar nonsense BAZ=qux ="), {"FOO": "bar", "BAZ": "qux"}
        )

    def test_env_parsing_keeps_an_empty_value(self):
        self.assertEqual(commands.parse_env("FOO="), {"FOO": ""})


class TestLaunchTarget(Base):
    @staticmethod
    def _args(**kwargs) -> argparse.Namespace:
        return argparse.Namespace(**kwargs)

    def _target(self, value: str) -> Vault:
        with self.vault.locked():
            self.vault.set_launch_target(value)
            self.vault.save()
        return self.vault.reload()

    def test_registry_default_is_the_cli(self):
        self.assertEqual(commands.resolve_target(self.vault, self._args()), "cli")

    def test_registry_can_prefer_the_app(self):
        self.assertEqual(commands.resolve_target(self._target("app"), self._args()), "app")

    def test_app_flag_wins_over_the_registry(self):
        self.assertEqual(commands.resolve_target(self.vault, self._args(app=True)), "app")

    def test_cli_flag_wins_over_the_registry(self):
        self.assertEqual(
            commands.resolve_target(self._target("app"), self._args(cli=True)), "cli"
        )

    def test_ask_falls_back_to_the_cli_without_a_terminal(self):
        self.assertEqual(commands.resolve_target(self._target("ask"), self._args()), "cli")

    def test_base_target_never_asks(self):
        self.assertEqual(commands.base_target(self._target("ask"), self._args()), "cli")

    def test_other_target_flips(self):
        self.assertEqual(commands.other_target("cli"), "app")
        self.assertEqual(commands.other_target("app"), "cli")

    def test_app_flag_survives_the_subcommand_split(self):
        self.assertEqual(split_args(["--app", "work"]), (["--app", "use", "work"], []))

    def test_target_flag_after_the_name_is_still_ours(self):
        argv, _ = split_args(["work", "--app"])
        args = cli.build_parser().parse_args(argv)
        self.assertTrue(getattr(args, "app", False))
        self.assertEqual(list(args.claude_args or []), [])

    def test_root_level_flag_is_not_clobbered_by_the_subparser(self):
        argv, _ = split_args(["--app", "work"])
        args = cli.build_parser().parse_args(argv)
        self.assertTrue(getattr(args, "app", False))

    def test_an_explicit_separator_still_forwards_the_flag(self):
        self.assertEqual(
            cli.hoist_target_flags(["work", "--", "--app"]), ["work", "--", "--app"]
        )

    def test_other_trailing_flags_stay_passthrough(self):
        argv, _ = split_args(["work", "--app", "--resume"])
        args = cli.build_parser().parse_args(argv)
        self.assertTrue(getattr(args, "app", False))
        self.assertEqual(list(args.claude_args or []), ["--resume"])


class TestAppLaunch(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bundle()
        self.spawned = []
        self._patch(claude_app, "launch", self._record_launch)
        self._patch(claude_app, "running_pids", lambda: [])
        self._patch(claude_app, "running_data_dirs", set)
        self._patch(claude_app, "is_in_use", lambda _dir: False)

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

    def test_launch_prelinks_the_pool_for_a_brand_new_profile(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            stored = self.vault.get(profile.name)
            stored.account_uuid, stored.org_uuid = "acct-x", "org-y"
            self.vault.upsert(stored)
            self.vault.save()
        profile = self.vault.reload().get(profile.name)
        commands.launch_app(self.vault, profile, argparse.Namespace())
        link = (
            Path(self.vault.app_data_dir_for(profile))
            / claude_app.SESSIONS_DIRNAME
            / "acct-x"
        )
        self.assertTrue(link.is_symlink())

    def test_the_data_dir_is_remembered_in_the_registry(self):
        profile = self.add(email="one@example.com")
        commands.launch_app(self.vault, profile, argparse.Namespace())
        stored = self.vault.reload().get(profile.name)
        self.assertEqual(stored.app_data_dir, self.vault.app_data_dir_for(profile))

    def test_launching_records_the_profile_as_last_used(self):
        profile = self.add(email="one@example.com")
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(self.vault.reload().last_used, profile.name)

    def test_chats_left_in_the_profile_are_pooled_on_launch(self):
        profile = self.add(email="one@example.com")
        data_dir = Path(self.vault.app_data_dir_for(profile))
        leaf = self.leaf(data_dir, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertTrue(self.pooled("org-1", "local_one").is_file())
        self.assertTrue(self.is_pooled(leaf))

    def test_pooling_is_skipped_while_this_profile_is_open(self):
        self._patch(claude_app, "is_in_use", lambda _dir: True)
        profile = self.add(email="one@example.com")
        data_dir = Path(self.vault.app_data_dir_for(profile))
        leaf = self.leaf(data_dir, "acct-a", "org-1")
        self.chat(leaf, "local_one", 10)
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertFalse(leaf.is_symlink())
        self.assertEqual(len(self.spawned), 1)

    def test_a_missing_bundle_is_reported(self):
        self._env("CLAUDE_LOGIN_APP_PATH", str(self.tmp / "Absent.app"))
        profile = self.add(email="one@example.com")
        with self.assertRaises(ClaudeAppError):
            commands.launch_app(self.vault, profile, argparse.Namespace())

    def test_dispatch_sends_the_app_target_to_the_app(self):
        profile = self.add(email="one@example.com")
        commands.dispatch(self.vault, profile, argparse.Namespace(), "app")
        self.assertEqual(len(self.spawned), 1)

    def _window_signed_in_as(self, data_dir: Path, account: str) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.json").write_text(
            json.dumps({"oauth:tokenCacheV2": "djEw", "lastKnownAccountUuid": account})
        )

    def _accounted(self, profile: Profile, account: str) -> Profile:
        with self.vault.locked():
            stored = self.vault.get(profile.name)
            stored.account_uuid = account
            self.vault.upsert(stored)
            self.vault.save()
        return self.vault.reload().get(profile.name)

    def test_a_window_signed_in_as_another_account_is_refused(self):
        profile = self._accounted(self.add(email="one@example.com"), "acct-one")
        self._window_signed_in_as(
            Path(self.vault.app_data_dir_for(profile)), "acct-two"
        )
        before = self.vault.reload().last_used
        with self.assertRaises(ClaudeAppError):
            commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.vault.reload().last_used, before)

    def test_a_mismatched_window_still_opens_with_yes(self):
        profile = self._accounted(self.add(email="one@example.com"), "acct-one")
        self._window_signed_in_as(
            Path(self.vault.app_data_dir_for(profile)), "acct-two"
        )
        commands.launch_app(self.vault, profile, argparse.Namespace(yes=True))
        self.assertEqual(len(self.spawned), 1)

    def test_the_refusal_points_at_the_profile_that_owns_the_account(self):
        owner = self._accounted(self.add(email="one@example.com"), "acct-one")
        with self.vault.locked():
            self.vault.upsert(
                Profile(
                    name="default",
                    config_dir=None,
                    email="one@example.com",
                    account_uuid="acct-one",
                )
            )
            self.vault.save()
        self._window_signed_in_as(self.app_support, "acct-two")
        default = self.vault.reload().get("default")
        with self.assertRaises(ClaudeAppError) as caught:
            commands.launch_app(self.vault, default, argparse.Namespace())
        self.assertIn(owner.name, str(caught.exception))
        self.assertEqual(self.spawned, [])

    def _bundle_asar(self, payload: bytes) -> None:
        asar = Path(os.environ["CLAUDE_LOGIN_APP_PATH"]) / "Contents/Resources/app.asar"
        asar.parent.mkdir(parents=True, exist_ok=True)
        asar.write_bytes(payload)

    def _scrubbing_bundle(self) -> None:
        """Deletes the variable, but still takes --user-data-dir on argv."""
        self._bundle_asar(
            b"var MB=[`remote-debugging-port`,`browser-subprocess-path`];"
            b"(delete process.env.CLAUDE_USER_DATA_DIR,delete process.env.SSLKEYLOGFILE)"
        )

    def _denylisting_bundle(self) -> None:
        """Would exit rather than start with --user-data-dir on argv."""
        self._bundle_asar(
            b"var MB=[`remote-debugging-port`,`user-data-dir`,`host-rules`];"
            b"(delete process.env.CLAUDE_USER_DATA_DIR)"
        )

    def test_a_build_that_only_scrubs_the_variable_still_launches_per_account(self):
        profile = self.add(email="one@example.com")
        self._scrubbing_bundle()
        commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(len(self.spawned), 1)

    def test_an_app_build_that_refuses_the_switch_refuses_a_per_account_launch(self):
        profile = self.add(email="one@example.com")
        self._denylisting_bundle()
        with self.assertRaises(ClaudeAppError):
            commands.launch_app(self.vault, profile, argparse.Namespace())
        self.assertEqual(self.spawned, [])

    def test_the_machine_wide_default_still_launches_on_such_a_build(self):
        self._denylisting_bundle()
        with self.vault.locked():
            self.vault.upsert(Profile(name="default", config_dir=None, email="one@example.com"))
            self.vault.save()
        default = self.vault.reload().get("default")
        commands.launch_app(self.vault, default, argparse.Namespace())
        self.assertEqual(len(self.spawned), 1)

    def test_the_foreign_window_hint_never_points_at_a_refused_launch(self):
        owner = self._accounted(self.add(email="one@example.com"), "acct-one")
        with self.vault.locked():
            self.vault.upsert(
                Profile(
                    name="default",
                    config_dir=None,
                    email="one@example.com",
                    account_uuid="acct-one",
                )
            )
            self.vault.save()
        self._window_signed_in_as(self.app_support, "acct-two")
        self._denylisting_bundle()
        default = self.vault.reload().get("default")
        with self.assertRaises(ClaudeAppError) as caught:
            commands.launch_app(self.vault, default, argparse.Namespace())
        self.assertNotIn(owner.name, str(caught.exception))
        self.assertEqual(self.spawned, [])

    def test_app_open_skips_a_mismatched_window_and_opens_the_rest(self):
        bad = self._accounted(self.add(email="one@example.com"), "acct-one")
        self._window_signed_in_as(Path(self.vault.app_data_dir_for(bad)), "acct-two")
        good = self.add(name="two", email="two@example.com")
        args = argparse.Namespace(names=[bad.name, good.name])
        self.assertEqual(commands.cmd_app_open(self.vault, args), 0)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.spawned[0][0], self.vault.app_data_dir_for(good))

    def test_app_target_does_not_require_a_usable_cli_login(self):
        profile = self.add(email="one@example.com")
        claude_cli.delete_credentials(profile.config_dir)
        self.invalidate()
        self.assertEqual(self.vault.status(profile).state, "logged-out")
        args = argparse.Namespace(name=profile.name, app=True, yes=True, claude_args=[])
        self.assertEqual(commands.cmd_use(self.vault, args), 0)
        self.assertEqual(len(self.spawned), 1)


class TestAppScrubDetection(_AppBase):
    """The packaged app deleting CLAUDE_USER_DATA_DIR at startup (since ~1.34)."""

    def _asar(self, payload: bytes) -> None:
        asar = Path(os.environ["CLAUDE_LOGIN_APP_PATH"]) / "Contents/Resources/app.asar"
        asar.parent.mkdir(parents=True, exist_ok=True)
        asar.write_bytes(payload)

    def test_a_bundle_that_deletes_the_variable_is_detected(self):
        self.fake_bundle()
        self._asar(
            b"E.app.isPackaged&&!qU&&(delete process.env.CLAUDE_USER_DATA_DIR,"
            b"delete process.env.SSLKEYLOGFILE)"
        )
        self.assertTrue(claude_app.scrubs_user_data_dir())

    def test_a_bundle_that_honours_the_variable_is_not(self):
        self.fake_bundle()
        self._asar(b"if(process.env.CLAUDE_USER_DATA_DIR){E.app.setPath(`userData`,e)}")
        self.assertFalse(claude_app.scrubs_user_data_dir())

    def test_a_missing_archive_reads_as_not_scrubbing(self):
        self.fake_bundle()
        self.assertFalse(claude_app.scrubs_user_data_dir())

    def test_a_build_that_scrubs_the_variable_still_takes_the_switch(self):
        self.fake_bundle()
        self._asar(
            b"var MB=[`remote-debugging-port`,`host-rules`,`browser-subprocess-path`];"
            b"E.app.isPackaged&&(delete process.env.CLAUDE_USER_DATA_DIR)"
        )
        self.assertTrue(claude_app.scrubs_user_data_dir())
        self.assertFalse(claude_app.rejects_user_data_switch())
        self.assertTrue(claude_app.can_relocate_user_data())

    def test_a_denylisted_switch_is_detected(self):
        self.fake_bundle()
        self._asar(b"var MB=[`remote-debugging-port`,`user-data-dir`,`host-rules`];")
        self.assertTrue(claude_app.rejects_user_data_switch())
        self.assertFalse(claude_app.can_relocate_user_data())

    def test_the_switch_named_outside_the_denylist_does_not_count(self):
        self.fake_bundle()
        self._asar(
            b"var MB=[`remote-debugging-port`,`host-rules`];"
            b"P.warn(`relocated, ignoring user-data-dir`)"
        )
        self.assertFalse(claude_app.rejects_user_data_switch())

    def test_a_bundle_that_never_names_the_switch_cannot_screen_it(self):
        self.fake_bundle()
        self._asar(b"delete process.env.CLAUDE_USER_DATA_DIR")
        self.assertFalse(claude_app.rejects_user_data_switch())


class TestAppLaunchArgv(_AppBase):
    """Chromium reads --user-data-dir before the app can delete the variable."""

    class _Proc:
        pid = 4242

    def setUp(self) -> None:
        super().setUp()
        self.fake_bundle()
        self.calls: list[list[str]] = []
        self._patch(claude_app.subprocess, "Popen", self._record)

    def _record(self, argv, **kwargs):
        self.calls.append(list(argv))
        return self._Proc()

    def test_a_per_account_directory_is_named_on_the_command_line(self):
        target = str(self.tmp / "profile-data")
        self.assertEqual(claude_app.launch(target), 4242)
        self.assertIn(f"--user-data-dir={target}", self.calls[0])

    def test_the_machine_wide_directory_is_launched_bare(self):
        claude_app.launch(str(self.app_support))
        self.assertEqual(len(self.calls[0]), 1)

    def test_the_variable_travels_alongside_the_switch(self):
        target = str(self.tmp / "profile-data")
        env = claude_app.child_env(target)
        self.assertEqual(env["CLAUDE_USER_DATA_DIR"], target)


class TestProfileDisplay(unittest.TestCase):
    def test_the_default_profile_is_marked(self):
        profile = Profile(name="default", config_dir=None, email="a@b.com")
        self.assertEqual(profile.display, "a@b.com (default)")

    def test_a_named_profile_shows_the_bare_email(self):
        profile = Profile(name="a@b.com", config_dir="/x", email="a@b.com")
        self.assertEqual(profile.display, "a@b.com")

    def test_a_default_with_no_login_stays_plain(self):
        profile = Profile(name="default", config_dir=None)
        self.assertEqual(profile.display, "default")


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
            self.assertFalse(claude_app.available())

    def test_our_own_replaced_backup_is_not_a_leaf(self):
        tmp = Path(tempfile.mkdtemp(prefix="app-leaves-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = tmp / claude_app.SESSIONS_DIRNAME / "acct"
        (root / "org").mkdir(parents=True)
        (root / f"org{claude_app.REPLACED_MARKER}123").mkdir()
        (root / ".hidden").mkdir()
        leaves = claude_app.session_leaf_dirs(str(tmp))
        self.assertEqual([leaf.name for leaf in leaves], ["org"])

    def test_sessions_root_is_keyed_off_the_data_dir(self):
        self.assertEqual(claude_app.sessions_root("/d"), Path("/d/claude-code-sessions"))
        self.assertEqual(
            claude_app.sessions_root("/d", agent=True),
            Path("/d/local-agent-mode-sessions"),
        )


class TestAppEnv(unittest.TestCase):
    def test_data_dir_is_exported(self):
        self.assertEqual(claude_app.child_env("/data")["CLAUDE_USER_DATA_DIR"], "/data")

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
        self.assertEqual(claude_app.child_env("/data", {"FOO": "bar"})["FOO"], "bar")


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
            self._config(
                {"oauth:tokenCacheV2": "djEw...", "lastKnownAccountUuid": "uuid-a"}
            )
        )
        self.assertEqual(status.state, "signed-in")
        self.assertEqual(status.account_uuid, "uuid-a")

    def test_legacy_token_key_is_accepted(self):
        status = claude_app.app_status(self._config({"oauth:tokenCache": "djEw"}))
        self.assertTrue(status.signed_in)

    def test_empty_token_does_not_count(self):
        status = claude_app.app_status(self._config({"oauth:tokenCacheV2": ""}))
        self.assertFalse(status.signed_in)


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

    def test_chats_are_found_in_the_per_organisation_directories(self):
        org = self.pool / "org-1"
        org.mkdir()
        (org / "local_a.json").write_text(
            json.dumps({"sessionId": "local_a", "cwd": "/work", "lastFocusedAt": 400})
        )
        self.assertEqual(claude_app.last_session(self.pool)["sessionId"], "local_a")


class TestAccountSelection(_AppBase):
    def test_absent_key_means_every_account(self):
        profile = self.add(email="one@example.com")
        self.assertIsNone(self.vault.app_shared_accounts)
        self.assertTrue(self.vault.shares_chats(profile))
        self.assertIsNone(self.vault.sharing_account_uuids())

    def test_empty_list_means_nobody(self):
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.set_app_shared_accounts([])
            self.vault.save()
        vault = self.vault.reload()
        self.assertFalse(vault.shares_chats(profile))
        self.assertFalse(vault.sharing_enabled)
        self.assertEqual(vault.sharing_account_uuids(), set())

    def test_the_old_boolean_migrates_to_an_empty_list(self):
        with self.vault.locked():
            self.vault._data["appSessionsShared"] = False
            self.vault.save()
        vault = self.vault.reload()
        self.assertEqual(vault.app_shared_accounts, [])
        self.assertNotIn("appSessionsShared", vault._data)

    def test_the_old_boolean_true_leaves_the_default(self):
        with self.vault.locked():
            self.vault._data["appSessionsShared"] = True
            self.vault.save()
        self.assertIsNone(self.vault.reload().app_shared_accounts)

    def test_a_stale_name_does_not_break_the_count(self):
        self.add(email="one@example.com")
        with self.vault.locked():
            self.vault.set_app_open_accounts(["gone@example.com"])
            self.vault.save()
        self.assertEqual(commands._chosen_count(self.vault.reload(), ["gone@example.com"]), "0 of 1")

    def test_sharing_uuids_only_cover_ticked_accounts(self):
        first = self.add(email="one@example.com")
        second = self.add(email="two@example.com")
        with self.vault.locked():
            self.vault.set_app_shared_accounts([first.name])
            self.vault.save()
        uuids = self.vault.reload().sharing_account_uuids()
        self.assertIn(first.account_uuid, uuids)
        self.assertNotIn(second.account_uuid, uuids)

    def test_toggling_a_name_adds_and_removes(self):
        self.assertEqual(commands.toggled(["a"], "b"), ["a", "b"])
        self.assertEqual(commands.toggled(["a", "b"], "a"), ["b"])


class TestPerAccountSharing(_AppBase):
    def test_an_unticked_account_stays_private_in_the_machine_dir(self):
        shared = self.leaf(self.app_support, "acct-shared", "org")
        private = self.leaf(self.app_support, "acct-private", "org")
        self.chat(shared, "local_shared")
        self.chat(private, "local_private")
        plan = self.vault.wire_session_pool(
            str(self.app_support), sharing={"acct-shared"}
        )
        self.assertTrue(self.pooled("org", "local_shared").is_file())
        self.assertFalse(self.pooled("org", "local_private").exists())
        self.assertTrue(self.is_pooled(shared))
        self.assertFalse(private.parent.is_symlink())
        self.assertEqual(plan.linked, 1)

    def test_none_lets_everybody_in(self):
        first = self.leaf(self.app_support, "acct-a", "org")
        second = self.leaf(self.app_support, "acct-b", "org")
        self.chat(first, "local_a")
        self.chat(second, "local_b")
        self.vault.wire_session_pool(str(self.app_support), sharing=None)
        self.assertTrue(self.is_pooled(first))
        self.assertTrue(self.is_pooled(second))


class TestAppOpen(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bundle()
        self.opened = []
        self._patch(claude_app, "launch", lambda d, e=None: self.opened.append(d) or 1)
        self._patch(claude_app, "running_pids", lambda: [])
        self._patch(claude_app, "running_data_dirs", set)
        self._patch(claude_app, "is_in_use", lambda _dir: False)
        self._patch(commands, "LAUNCH_STAGGER_SECONDS", 0)

    def test_opens_every_account_by_default(self):
        self.add(email="one@example.com")
        self.add(email="two@example.com")
        commands.cmd_app_open(self.vault.reload(), argparse.Namespace(names=[]))
        self.assertEqual(len(self.opened), 2)

    def test_respects_the_configured_list(self):
        first = self.add(email="one@example.com")
        self.add(email="two@example.com")
        with self.vault.locked():
            self.vault.set_app_open_accounts([first.name])
            self.vault.save()
        commands.cmd_app_open(self.vault.reload(), argparse.Namespace(names=[]))
        self.assertEqual(self.opened, [self.vault.app_data_dir_for(first)])

    def test_arguments_beat_the_configured_list(self):
        first = self.add(email="one@example.com")
        second = self.add(email="two@example.com")
        with self.vault.locked():
            self.vault.set_app_open_accounts([first.name])
            self.vault.save()
        commands.cmd_app_open(
            self.vault.reload(), argparse.Namespace(names=[second.name])
        )
        self.assertEqual(self.opened, [self.vault.app_data_dir_for(second)])

    def test_an_already_open_account_is_skipped(self):
        self.add(email="one@example.com")
        self._patch(claude_app, "is_in_use", lambda _dir: True)
        commands.cmd_app_open(self.vault.reload(), argparse.Namespace(names=[]))
        self.assertEqual(self.opened, [])

    def test_a_stale_name_is_skipped_not_fatal(self):
        self.add(email="one@example.com")
        commands.cmd_app_open(
            self.vault.reload(), argparse.Namespace(names=["gone@example.com"])
        )
        self.assertEqual(self.opened, [])


class TestGatherMcp(Base):
    def _servers(self, config_dir, servers: dict) -> None:
        current = claude_cli.read_global_config(config_dir)
        path = claude_cli.global_config_path(config_dir)
        path.write_text(json.dumps({**current, "mcpServers": servers}))

    def test_a_server_added_in_a_profile_reaches_the_machine_config(self):
        profile = self.add(email="one@example.com")
        self._servers(profile.config_dir, {"local-tool": {"command": "run-me"}})
        with self.vault.locked():
            added = self.vault.gather_config()
            self.vault.save()
        self.assertEqual(added, ["local-tool"])
        machine = claude_cli.read_global_config(None)
        self.assertIn("local-tool", machine["mcpServers"])

    def test_the_machine_config_wins_on_a_name_clash(self):
        profile = self.add(email="one@example.com")
        self._servers(None, {"tool": {"command": "machine"}})
        self._servers(profile.config_dir, {"tool": {"command": "profile"}})
        with self.vault.locked():
            self.vault.gather_config()
            self.vault.save()
        machine = claude_cli.read_global_config(None)
        self.assertEqual(machine["mcpServers"]["tool"]["command"], "machine")

    def test_gathering_then_syncing_spreads_it_to_the_others(self):
        first = self.add(email="one@example.com")
        second = self.add(email="two@example.com")
        self._servers(first.config_dir, {"local-tool": {"command": "run-me"}})
        commands.cmd_sync(self.vault, argparse.Namespace(name=None, gather=True))
        landed = claude_cli.read_global_config(second.config_dir)
        self.assertIn("local-tool", landed.get("mcpServers", {}))

    def test_plain_sync_does_not_gather(self):
        profile = self.add(email="one@example.com")
        self._servers(profile.config_dir, {"local-tool": {"command": "run-me"}})
        commands.cmd_sync(self.vault, argparse.Namespace(name=None, gather=False))
        self.assertNotIn(
            "local-tool", claude_cli.read_global_config(None).get("mcpServers", {})
        )


class TestSyncAllAndSetup(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bundle()
        self._patch(claude_app, "running_pids", lambda: [])
        self._patch(claude_app, "running_data_dirs", set)
        self._patch(claude_app, "is_in_use", lambda _dir: False)

    def test_sync_all_runs_every_step(self):
        self.add(email="one@example.com")
        code = commands.cmd_sync_all(self.vault.reload(), argparse.Namespace(dry_run=False))
        self.assertEqual(code, 0)

    def test_a_failing_step_does_not_stop_the_others(self):
        self.add(email="one@example.com")
        calls = []

        def boom(vault, args):
            calls.append("relink")
            raise ClaudeAppError("nope")

        self._patch(commands, "cmd_relink", boom)
        code = commands.cmd_sync_all(self.vault.reload(), argparse.Namespace(dry_run=False))
        self.assertEqual(code, 1)
        self.assertEqual(calls, ["relink"])
        # The pool step still ran despite the earlier failure.
        self.assertTrue(self.vault.reload().pool_dir(claude_app.SESSIONS_DIRNAME) is not None)

    def test_setup_says_its_piece_without_a_terminal(self):
        self.assertEqual(commands.cmd_setup(self.vault, argparse.Namespace()), 0)

    def test_setup_is_safe_to_repeat(self):
        self.add(email="one@example.com")
        self.assertEqual(commands.cmd_setup(self.vault.reload(), argparse.Namespace()), 0)
        self.assertEqual(commands.cmd_setup(self.vault.reload(), argparse.Namespace()), 0)


class TestSharedEntryEditing(_AppBase):
    """The entries with spaces in their names are the whole point here.

    A space-separated round trip once shredded ``Claude Extensions`` into
    ``Claude`` and ``Extensions``, which matched nothing on disk, so the app's
    extensions were silently never shared while every command reported success.
    """

    def test_defaults_contain_names_with_spaces(self):
        with_spaces = [e for e in claude_app.DEFAULT_APP_SHARED if " " in e]
        self.assertTrue(with_spaces, "the regression this guards needs such a name")

    def test_toggling_keeps_names_with_spaces_intact(self):
        entry = next(e for e in claude_app.DEFAULT_APP_SHARED if " " in e)
        with self.vault.locked():
            self.vault.set_app_shared(commands.toggled(self.vault.app_shared, entry))
            self.vault.save()
        after = self.vault.reload().app_shared
        self.assertNotIn(entry, after)
        self.assertNotIn("Claude", after)
        with self.vault.locked():
            self.vault.set_app_shared(commands.toggled(after, entry))
            self.vault.save()
        self.assertIn(entry, self.vault.reload().app_shared)

    def test_choices_keep_configured_extras(self):
        with self.vault.locked():
            self.vault.set_app_shared([*claude_app.DEFAULT_APP_SHARED, "My Own Thing"])
            self.vault.save()
        choices = commands.shared_entry_choices(self.vault.reload())
        self.assertIn("My Own Thing", choices)
        self.assertEqual(len(choices), len(claude_app.DEFAULT_APP_SHARED) + 1)

    def test_rows_mark_what_is_absent_on_this_machine(self):
        (self.app_support / "claude-code").mkdir()
        rows = commands._shared_items(self.vault)
        labels = {
            ui.strip_ansi(row.cells[1]): ui.strip_ansi(row.cells[2]) for row in rows
        }
        self.assertEqual(labels["claude-code"], "")
        self.assertEqual(labels["Claude Extensions"], "not on this machine")

    def test_an_entry_with_spaces_actually_gets_linked(self):
        entry = "Claude Extensions"
        (self.app_support / entry).mkdir(parents=True)
        profile = self.add(email="one@example.com")
        with self.vault.locked():
            linked, _ = self.vault.link_app_shared(profile)
        self.assertIn(entry, linked)
        self.assertTrue((Path(self.vault.app_data_dir_for(profile)) / entry).is_symlink())


class TestSyncAllRepairs(_AppBase):
    def setUp(self) -> None:
        super().setUp()
        self.fake_bundle()
        self._patch(claude_app, "running_pids", lambda: [])
        self._patch(claude_app, "running_data_dirs", set)
        self._patch(claude_app, "is_in_use", lambda _dir: False)

    def test_sync_all_resolves_a_private_copy(self):
        (self.app_support / "claude_desktop_config.json").write_text('{"shared":true}')
        profile = self.add(email="one@example.com")
        data_dir = Path(self.vault.app_data_dir_for(profile))
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (data_dir / "claude_desktop_config.json").write_text('{"mine":true}')
        code = commands.cmd_sync_all(
            self.vault.reload(), argparse.Namespace(dry_run=False)
        )
        self.assertEqual(code, 0)
        self.assertTrue((data_dir / "claude_desktop_config.json").is_symlink())
        self.assertTrue(list((data_dir / ".shadowed").iterdir()))

    def test_relink_reports_a_problem_when_a_copy_survives(self):
        (self.app_support / "claude_desktop_config.json").write_text('{"shared":true}')
        profile = self.add(email="one@example.com")
        data_dir = Path(self.vault.app_data_dir_for(profile))
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (data_dir / "claude_desktop_config.json").write_text('{"mine":true}')
        code = commands.cmd_app_relink(
            self.vault.reload(), argparse.Namespace(name=None, force=False)
        )
        self.assertEqual(code, 1)


class TestScripts(unittest.TestCase):
    """The clickable wrappers must stay runnable and stay thin."""

    ROOT = Path(__file__).resolve().parent.parent

    def scripts(self) -> list[Path]:
        return sorted((self.ROOT / "scripts").glob("*.command"))

    def test_every_script_is_present_and_executable(self):
        names = {path.name for path in self.scripts()}
        self.assertEqual(names, {name for name, _ in commands.SCRIPT_SUMMARY})
        for path in self.scripts():
            self.assertTrue(os.access(path, os.X_OK), f"{path.name} is not executable")

    def test_every_script_only_calls_the_cli(self):
        for path in self.scripts():
            body = path.read_text()
            self.assertTrue(body.startswith("#!/usr/bin/env bash"), path.name)
            self.assertIn("./bin/claude-login", body)
            self.assertIn('cd "$(dirname "${BASH_SOURCE[0]}")/.."', body)


class TestNoPrivateData(unittest.TestCase):
    """Guards the repository against personal data before it goes public.

    Written once so that a pasted `list --json` or a debugging log cannot quietly
    ship somebody's email or account uuid.
    """

    ROOT = Path(__file__).resolve().parent.parent
    SUFFIXES = {".py", ".md", ".sh", ".toml", ".command", ".json", ".txt", ".cfg"}
    SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "build", "dist"}
    #: Domains only ever used as examples in docs and tests.
    PLACEHOLDER_DOMAINS = ("example.com", "x.io", "x.com", "corp.com", "home.com", "b.com")
    #: The public OAuth client id of Claude Code, plus test fixtures.
    ALLOWED_UUIDS = {
        "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "11111111-2222-3333-4444-555555555555",
    }
    EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    HOME_RE = re.compile(r"/Users/(?!someone\b)[A-Za-z0-9._-]+")

    def files(self):
        for path in self.ROOT.rglob("*"):
            if not path.is_file() or path.suffix not in self.SUFFIXES:
                continue
            if self.SKIP_DIRS & set(path.relative_to(self.ROOT).parts):
                continue
            yield path

    def test_no_real_email_addresses(self):
        found = {
            f"{path.relative_to(self.ROOT)}: {hit}"
            for path in self.files()
            for hit in self.EMAIL_RE.findall(path.read_text(errors="replace"))
            if not hit.endswith(self.PLACEHOLDER_DOMAINS)
        }
        self.assertEqual(found, set(), "real-looking email in the repository")

    def test_no_unexpected_uuids(self):
        found = {
            f"{path.relative_to(self.ROOT)}: {hit}"
            for path in self.files()
            for hit in self.UUID_RE.findall(path.read_text(errors="replace"))
            if hit not in self.ALLOWED_UUIDS
        }
        self.assertEqual(found, set(), "account uuid in the repository")

    def test_no_absolute_home_paths(self):
        found = {
            f"{path.relative_to(self.ROOT)}: {hit}"
            for path in self.files()
            for hit in self.HOME_RE.findall(path.read_text(errors="replace"))
        }
        self.assertEqual(found, set(), "somebody's home directory in the repository")


@unittest.skipUnless(
    os.environ.get("CLAUDE_LOGIN_INTEGRATION") == "1" and sys.platform == "darwin",
    "set CLAUDE_LOGIN_INTEGRATION=1 to exercise the real Keychain and claude binary",
)
class TestKeychainIntegration(unittest.TestCase):
    """Proves our service-name formula is the one the real `claude` reads."""

    def test_claude_resolves_symlinks_before_writing_settings(self):
        """Canary: sharing via symlinks only works because claude realpaths first.

        Its config writes are atomic (temp file + rename), which would normally
        replace the symlink with a private copy.  It resolves the path first, so
        the rename lands on the shared file instead.  If a future version stops
        doing that, every profile silently forks its settings — catch it here.
        """
        os.environ.pop("CLAUDE_LOGIN_CLAUDE_BIN", None)
        root = Path(tempfile.mkdtemp(prefix="cl-symlink-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        shared, profile = root / "shared", root / "profile"
        shared.mkdir()
        profile.mkdir()
        settings = shared / "settings.json"
        settings.write_text('{"autoMode":{"allow":["Bash(ls:*)"]},"MARKER":"shared"}')
        (profile / ".claude.json").write_text('{"hasCompletedOnboarding":true}')
        (profile / "settings.json").symlink_to(settings)

        claude_cli.run(
            str(profile), ["auto-mode", "reset", "--yes"], capture=True, timeout=60
        )

        self.assertTrue(
            (profile / "settings.json").is_symlink(),
            "claude replaced the symlink with a private copy — sharing is broken",
        )
        written = json.loads(settings.read_text())
        self.assertNotIn("autoMode", written, "the write did not reach the shared file")
        self.assertEqual(written.get("MARKER"), "shared")

    def test_planted_credentials_are_visible_to_claude(self):
        os.environ.pop("CLAUDE_LOGIN_CLAUDE_BIN", None)
        work = tempfile.mkdtemp(prefix="cl-probe-", dir=str(Path.home()))
        service = claude_cli.credentials_service(work)
        blob = {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-probe",
                "refreshToken": "sk-ant-ort01-probe",
                "expiresAt": 4102444800000,
                "refreshTokenExpiresAt": 4102444800000,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        }
        try:
            keychain.write(service, json.dumps(blob))
            self.assertEqual(claude_cli.read_credentials(work), blob)
            status = claude_cli.auth_status(work, timeout=60)
            self.assertTrue(status.get("loggedIn"))
            self.assertEqual(status.get("subscriptionType"), "max")
        finally:
            keychain.delete(service)
            shutil.rmtree(work, ignore_errors=True)
        self.assertFalse(keychain.exists(service))


if __name__ == "__main__":
    unittest.main(verbosity=2)
