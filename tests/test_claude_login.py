"""Tests for claude-login.

The suite runs against a stub ``claude`` executable so nothing touches your real
accounts.  One extra test exercises the genuine Keychain/`claude` interaction and
only runs when ``CLAUDE_LOGIN_INTEGRATION=1`` is set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_login import claude_cli, cli, commands, keychain, store, ui, usage  # noqa: E402
from claude_login.cli import split_args  # noqa: E402
from claude_login.errors import UsageError  # noqa: E402
from claude_login.store import Profile, Vault  # noqa: E402

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
        self.assertEqual(len(commands._SETTINGS_ROWS), len(commands._settings_items(self.vault)))

    def test_rendered_rows_reflect_the_stored_flags(self):
        commands._save_launch_args(self.vault, ["--continue", "--effort", "high"])
        cells = [ui.strip_ansi(item.cells[2]) for item in commands._settings_items(self.vault)]
        self.assertEqual(cells[:3], ["off", "on", "high"])

    def test_title_previews_the_command_line(self):
        commands._save_launch_args(self.vault, ["--continue"])
        self.assertIn("claude --continue", ui.strip_ansi(commands._settings_title(self.vault)))
        commands._save_launch_args(self.vault, [])
        self.assertIn("no flags", ui.strip_ansi(commands._settings_title(self.vault)))


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
        soon = datetime.now().astimezone() + timedelta(hours=2)
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
