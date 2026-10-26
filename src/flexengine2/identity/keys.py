"""Device key storage with enforced file permissions (EV-06, V-48 alpha; TPM-backed keys are beta, EV-21).

The key file must be a regular file owned by the engine's uid with no group/other bits (0600) inside a directory
that is not group/other writable (created 0700). A key that fails these checks is refused (KeyPermissionError)
unless ``fix_permissions=True`` (cfg ``fixPermissions``) is given, which chmods it to 0600 — it never adopts a key
owned by another uid. Wraps ``crypto.load_or_create`` so the on-disk format is unchanged.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from flexengine2.identity import crypto


class KeyPermissionError(PermissionError):
    """Key file (or its directory) has unsafe ownership or permissions."""


def check_private_file(path: str | Path, *, fix_permissions: bool = False, what: str = "key",
                       error=KeyPermissionError) -> None:
    """Raise `error` unless `path` is a regular file owned by us with no group/other permission bits."""
    path = Path(path)
    st = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise error(f"{what} {path}: not a regular file")
    if st.st_uid != os.geteuid():
        raise error(f"{what} {path}: owned by uid {st.st_uid}, not {os.geteuid()}")
    if st.st_mode & 0o077:
        if not fix_permissions:
            raise error(f"{what} {path}: mode {stat.S_IMODE(st.st_mode):04o} is group/other accessible "
                        f"(expected 0600; set fixPermissions to repair)")
        os.chmod(path, 0o600)


def _check_dir(d: Path, fix_permissions: bool) -> None:
    st = os.stat(d)
    if st.st_uid != os.geteuid():
        raise KeyPermissionError(f"key directory {d}: owned by uid {st.st_uid}, not {os.geteuid()}")
    if st.st_mode & 0o022:
        if not fix_permissions:
            raise KeyPermissionError(f"key directory {d}: mode {stat.S_IMODE(st.st_mode):04o} is group/other "
                                     f"writable (set fixPermissions to repair)")
        os.chmod(d, stat.S_IMODE(st.st_mode) & ~0o077)


def load_or_create_device_key(path: str | Path, *, fix_permissions: bool = False) -> Ed25519PrivateKey:
    path = Path(path)
    d = path.parent
    if not d.exists():
        d.mkdir(mode=0o700, parents=True)
        os.chmod(d, 0o700)  # mkdir honours umask
    _check_dir(d, fix_permissions)
    if path.exists() or path.is_symlink():
        check_private_file(path, fix_permissions=fix_permissions)
        return crypto.load_or_create(path)
    key = crypto.load_or_create(path)  # atomic write, mode 0600
    check_private_file(path)
    return key
