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

import fcntl
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from . import session_file

NKEY_SEED = "nkey_seed"
_NAME_RE = re.compile(r"^[a-z_]{1,40}$")


def refresh_entry(issuer: str, client_id: str, human: str) -> str:
    """The entry of one refresh token: its name carries a hash of the issuer,
    the client and the human (letters only, as entry names are), so a token of
    another realm, client or person is never the one found."""
    raw = "\0".join((issuer.rstrip("/"), client_id, human.strip().casefold()))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return "refresh_" + "".join(chr(ord("a") + int(c, 16)) for c in digest)


class KeyStoreError(Exception):
    """A stored value that must not be read or written; the message names the
    entry and the reason, never a value."""


class KeyStore(Protocol):
    def load(self, name: str) -> str | None:
        """The value stored under `name`; None when there is none."""
        ...

    def save(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...

    def try_lock(self, name: str) -> object | None:
        """Take the lock of entry `name` without blocking: a handle, or None
        when another holder (process or object) has it."""
        ...

    def unlock(self, handle: object) -> None: ...


class Ephemeral:
    """In memory only: gone with the process (the nkey is new per process)."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._locked: set[str] = set()

    def try_lock(self, name: str) -> object | None:
        if name in self._locked:
            return None
        self._locked.add(name)
        return name

    def unlock(self, handle: object) -> None:
        self._locked.discard(str(handle))

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
            self._fsync_dir()
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _fsync_dir(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def delete(self, name: str) -> None:
        path = self._path(name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        self._fsync_dir()

    def try_lock(self, name: str) -> object | None:
        """flock on a lock file of its own per entry, so that processes of one
        OS user take turns at load, refresh and store. Never blocks."""
        path = self._path(name).with_name(f".{name}.lock")
        self._check_dir(create=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None  # another holder: the caller tries again
        except OSError:
            os.close(fd)
            raise  # no locking here at all (ENOLCK on NFS or SMB): the caller goes on without
        return fd

    def unlock(self, handle: object) -> None:
        if isinstance(handle, int):
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)
