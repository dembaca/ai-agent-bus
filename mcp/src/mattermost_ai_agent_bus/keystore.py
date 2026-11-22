"""Where the host's login keeps its secrets (RT-6.34, ADR-014 points 7 and 9).

A store holds two named values: the Keycloak refresh token and the nkey seed.
`Ephemeral` keeps them in memory only; `HostFile` keeps each in a file of its
own, mode 0600, in `<host state dir>/login/` (mode 0700): on the host, outside
every sandbox. A file that is a symlink, not the user's own, or readable by
group or world is refused, never read.

Nothing here logs or raises a value: errors name the store entry, never its
content.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from . import session_file

REFRESH_TOKEN = "refresh_token"
NKEY_SEED = "nkey_seed"
_NAME_RE = re.compile(r"^[a-z_]{1,32}$")


class KeyStoreError(Exception):
    """A stored value that must not be read or written; the message names the
    entry and the reason, never a value."""


class KeyStore(Protocol):
    def load(self, name: str) -> str | None:
        """The value stored under `name`; None when there is none."""
        ...

    def save(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


class Ephemeral:
    """In memory only: gone with the process (the nkey is new per process)."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def load(self, name: str) -> str | None:
        return self._values.get(name)

    def save(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)

    def __repr__(self) -> str:
        return f"Ephemeral(entries={sorted(self._values)})"


class HostFile:
    """One 0600 file per entry in a 0700 directory of the host's state."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory if directory is not None else session_file.host_state_dir() / "login"

    def __repr__(self) -> str:
        return f"HostFile({str(self.directory)!r})"

    def _path(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(name):
            raise KeyStoreError("invalid store entry name")
        return self.directory / name

    def _check_dir(self, *, create: bool) -> bool:
        """The directory is ours, not a link, 0700. False when absent (and not created)."""
        d = self.directory
        if create:
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.mkdir(d, 0o700)
            except FileExistsError:
                pass
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise KeyStoreError("the login directory is a link or not a directory")
        if st.st_uid != os.geteuid():
            raise KeyStoreError("the login directory is not owned by this user")
        if st.st_mode & 0o077:
            if not create:
                raise KeyStoreError("the login directory is readable by group or world")
            os.chmod(d, 0o700)  # ours and created by an earlier version or umask: tighten it
        return True

    def load(self, name: str) -> str | None:
        path = self._path(name)
        if not self._check_dir(create=False):
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        except OSError:  # ELOOP for a symlink, among others
            raise KeyStoreError(f"{name}: not readable (a symlink?)") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise KeyStoreError(f"{name}: not a regular file")
            if st.st_uid != os.geteuid():
                raise KeyStoreError(f"{name}: not owned by this user")
            if st.st_mode & 0o077:
                raise KeyStoreError(f"{name}: readable by group or world")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
                return fh.read(65536).strip() or None
        finally:
            os.close(fd)

    def save(self, name: str, value: str) -> None:
        path = self._path(name)
        self._check_dir(create=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=f".{name}.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)  # a symlink in its place is replaced, never followed
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self, name: str) -> None:
        path = self._path(name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
