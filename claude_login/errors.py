"""Exception types used across claude-login."""

from __future__ import annotations


class ClaudeLoginError(Exception):
    """Base class for every error we raise deliberately.

    The CLI catches these and prints ``error: <message>`` instead of a traceback.
    """

    exit_code = 1


class UsageError(ClaudeLoginError):
    """The user asked for something that does not make sense."""

    exit_code = 2


class SecretStoreError(ClaudeLoginError):
    """Reading from or writing to the credential store failed."""


class ClaudeCliError(ClaudeLoginError):
    """The `claude` executable is missing or misbehaved."""


class ProfileNotFound(UsageError):
    def __init__(self, name: str):
        super().__init__(f"no profile named {name!r} (run `claude-login list`)")
        self.name = name
