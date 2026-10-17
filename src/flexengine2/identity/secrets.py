"""Secrets from files only (EV-06, WS8 E11): never from the environment, never inherited by child processes.

  read_secret_file(path)            stripped contents; refuses group/other-readable or foreign-owned files
  consume_onboarding_token(path)    two-step: read -> caller onboards -> mark_consumed() destroys the file
  scrub_env(env)                    child-process environment without FLEX_* and secret-looking variables
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from flexengine2.identity.keys import check_private_file


class SecretPermissionError(PermissionError):
    """Secret file has unsafe ownership or permissions."""


class TokenConsumedError(RuntimeError):
    """The onboarding token was already used (file gone or marked consumed)."""


def read_secret_file(path: str | Path, *, allow_insecure: bool = False) -> str:
    """Contents of a secret file, stripped. Never consults os.environ."""
    path = Path(path)
    if not allow_insecure:
        check_private_file(path, what="secret", error=SecretPermissionError)
    value = path.read_text().strip()
    if not value:
        raise ValueError(f"secret {path}: empty")
    return value


class OnboardingToken:
    """A one-time token read from a file. `value` is usable until mark_consumed(); then the file is overwritten,
    deleted, and the object refuses further use."""

    def __init__(self, path: Path, value: str):
        self.path, self._value, self.consumed = path, value, False

    @property
    def value(self) -> str:
        if self.consumed:
            raise TokenConsumedError(f"onboarding token {self.path} already consumed")
        return self._value

    def mark_consumed(self) -> None:
        """Call only after the cloud confirmed onboarding. Overwrites then deletes the token file."""
        if self.consumed:
            return
        try:
            size = self.path.stat().st_size
            with open(self.path, "r+b") as f:
                f.write(b"\0" * max(size, 1))
                f.flush()
                os.fsync(f.fileno())
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._value, self.consumed = "", True

    def __repr__(self) -> str:  # never print the token
        return f"OnboardingToken({str(self.path)!r}, consumed={self.consumed})"


def consume_onboarding_token(path: str | Path, *, allow_insecure: bool = False) -> OnboardingToken:
    path = Path(path)
    if not path.exists():
        raise TokenConsumedError(f"onboarding token {path} missing (already used?)")
    return OnboardingToken(path, read_secret_file(path, allow_insecure=allow_insecure))


_SECRETISH = re.compile(r"TOKEN|SECRET|KEY|PASSWORD|PASSWD|CREDENTIAL|PRIVATE", re.I)


def scrub_env(env: dict[str, str] | None = None, *, keep: tuple[str, ...] = ()) -> dict[str, str]:
    """Copy of `env` (default os.environ) without FLEX_* and secret-looking names. `keep` names survive."""
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items()
            if k in keep or not (k.upper().startswith("FLEX_") or _SECRETISH.search(k))}
