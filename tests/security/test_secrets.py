"""E11: secrets from files only, never env; onboarding token is one-time; child env scrubbed."""

from __future__ import annotations

import os

import pytest

from flexengine2.identity.secrets import (SecretPermissionError, TokenConsumedError, consume_onboarding_token,
                                          read_secret_file, scrub_env)


def secret(tmp_path, text="s3cr3t\n", m=0o600, name="tok"):
    p = tmp_path / name
    p.write_text(text)
    os.chmod(p, m)
    return p


def test_read_strips(tmp_path):
    assert read_secret_file(secret(tmp_path, "  abc \n")) == "abc"


def test_env_never_read(tmp_path, monkeypatch):
    p = secret(tmp_path, "from-file")
    monkeypatch.setenv("FLEX_ONBOARDING_TOKEN", "from-env")

    class Trap(dict):
        def __getitem__(self, k):
            raise AssertionError(f"env read: {k}")
        get = __contains__ = __iter__ = __getitem__

    saved, saved_getenv = os.environ, os.getenv
    os.environ, os.getenv = Trap(), lambda *a, **k: (_ for _ in ()).throw(AssertionError("getenv"))
    try:  # any environment access inside the secrets API raises
        got = read_secret_file(p)
        t = consume_onboarding_token(p)
        val = t.value
        t.mark_consumed()
    finally:
        os.environ, os.getenv = saved, saved_getenv
    assert got == val == "from-file" and not p.exists()


def test_missing_file_no_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEX_ONBOARDING_TOKEN", "from-env")
    with pytest.raises(FileNotFoundError):
        read_secret_file(tmp_path / "nope")


@pytest.mark.parametrize("m", [0o640, 0o604, 0o644])
def test_perms_refused(tmp_path, m):
    p = secret(tmp_path, m=m)
    with pytest.raises(SecretPermissionError, match="group/other"):
        read_secret_file(p)
    with pytest.raises(SecretPermissionError):
        consume_onboarding_token(p)
    assert read_secret_file(p, allow_insecure=True) == "s3cr3t"


def test_empty_refused(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        read_secret_file(secret(tmp_path, " \n"))


def test_token_consumed_once(tmp_path):
    p = secret(tmp_path, "one-time")
    t = consume_onboarding_token(p)
    assert p.exists() and t.value == "one-time" and "one-time" not in repr(t)
    t.mark_consumed()
    assert not p.exists() and t.consumed
    with pytest.raises(TokenConsumedError):
        _ = t.value
    with pytest.raises(TokenConsumedError):
        consume_onboarding_token(p)
    t.mark_consumed()  # idempotent


def test_token_kept_when_onboarding_fails(tmp_path):
    p = secret(tmp_path, "one-time")
    t = consume_onboarding_token(p)
    # caller's onboarding raised -> mark_consumed() not called -> token still there for the retry
    assert p.read_text() == "one-time" and t.value == "one-time"
    assert consume_onboarding_token(p).value == "one-time"


def test_scrub_env():
    env = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C", "FLEX_ONBOARDING_TOKEN": "t", "FLEX_UNIT": "u",
           "flex_debug": "1", "AWS_SECRET_ACCESS_KEY": "s", "GITHUB_TOKEN": "g", "DB_PASSWORD": "p",
           "SSH_PRIVATE_KEY": "k", "api_key": "a", "MY_CREDENTIALS": "c"}
    assert scrub_env(env) == {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C"}
    assert scrub_env(env, keep=("FLEX_UNIT",))["FLEX_UNIT"] == "u"
    assert "FLEX_UNIT" in env  # input not modified


def test_scrub_env_default_os_environ(monkeypatch):
    monkeypatch.setenv("FLEX_X_TOKEN", "t")
    monkeypatch.setenv("HARMLESS_VAR", "1")
    out = scrub_env()
    assert "FLEX_X_TOKEN" not in out and out["HARMLESS_VAR"] == "1"
