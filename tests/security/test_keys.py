"""V-48 alpha: device key file 0600 in a 0700 dir; unsafe keys refused."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from flexengine2.identity import crypto, keys
from flexengine2.identity.keys import KeyPermissionError, load_or_create_device_key


def mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_created_0600_in_0700_dir(tmp_path):
    p = tmp_path / "new" / "sub" / "device.key"
    k = load_or_create_device_key(p)
    assert mode(p) == 0o600 and mode(p.parent) == 0o700
    again = load_or_create_device_key(p)
    assert crypto.public_b64(again) == crypto.public_b64(k)
    assert crypto.public_b64(crypto.load_or_create(p)) == crypto.public_b64(k)  # format unchanged


@pytest.mark.parametrize("m", [0o640, 0o604, 0o644, 0o660])
def test_group_other_readable_refused(tmp_path, m):
    p = tmp_path / "device.key"
    load_or_create_device_key(p)
    os.chmod(p, m)
    with pytest.raises(KeyPermissionError, match="group/other"):
        load_or_create_device_key(p)
    assert mode(p) == m  # not silently changed


def test_fix_permissions(tmp_path):
    p = tmp_path / "device.key"
    k = load_or_create_device_key(p)
    os.chmod(p, 0o644)
    assert crypto.public_b64(load_or_create_device_key(p, fix_permissions=True)) == crypto.public_b64(k)
    assert mode(p) == 0o600


def test_group_writable_dir_refused(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    os.chmod(d, 0o777)
    with pytest.raises(KeyPermissionError, match="directory"):
        load_or_create_device_key(d / "device.key")
    load_or_create_device_key(d / "device.key", fix_permissions=True)
    assert mode(d) & 0o077 == 0


def test_symlink_refused(tmp_path):
    real = tmp_path / "real.key"
    load_or_create_device_key(real)
    (tmp_path / "device.key").symlink_to(real)
    with pytest.raises(KeyPermissionError, match="regular"):
        load_or_create_device_key(tmp_path / "device.key")


def test_other_uid_refused_even_with_fix(tmp_path, monkeypatch):
    p = tmp_path / "device.key"
    load_or_create_device_key(p)
    real_stat = os.stat

    def fake_stat(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        if str(path) == str(p):
            return SimpleNamespace(st_mode=st.st_mode, st_uid=os.geteuid() + 1)
        return st

    monkeypatch.setattr(keys.os, "stat", fake_stat)
    for fix in (False, True):
        with pytest.raises(KeyPermissionError, match="owned by uid"):
            load_or_create_device_key(p, fix_permissions=fix)
