"""Where the host's login keeps its secrets (RT-6.34, ADR-014 points 7 and 9).

A store holds three named values: the Keycloak refresh token, the login cooldown
beside it (`cooldown_entry`), and the nkey seed. `Ephemeral` keeps them in
memory only; `HostFile` keeps each in a file of its own, mode 0600, in a `login/`
directory (mode 0700). A file that is a symlink, not the user's own, or readable
by group or world is refused, never read. `MacKeychain` and `SecretTool` keep
them in the system keychain (macOS Keychain, libsecret), one generic password per
entry under the service `SERVICE`, account = the entry name.

`default()` picks the store the login uses (#154, decision 10): the system
keychain where one answers a harmless probe within `PROBE_TIMEOUT`, else the
0600 file; under corral that file lies in the sandbox's own home, else in the
host's state directory. Every store locks with flock on lock files in its 0700
directory, a keychain having no locks, so sessions on one host renew one at a
time.

Nothing here logs or raises a value: errors name the store entry, never its
content, and a value never travels in an argument vector, the environment or a
temporary file outside the store (a keychain gets it on the tool's stdin).
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import session_file

log = logging.getLogger(__name__)

NKEY_SEED = "nkey_seed"
# The keychain's service of every entry; the account is the entry name.
SERVICE = "mm-agent-bus-login"
# How long a keychain call may take: one that hangs (a locked keychain's dialog,
# a sandbox that denies the Mach lookup) is killed and counts as an error.
OP_TIMEOUT = 10.0
PROBE_TIMEOUT = 3.0
PROBE_ENTRY = "probe"
_NAME_RE = re.compile(r"^[a-z_]{1,40}$")


def refresh_entry(issuer: str, client_id: str, human: str) -> str:
    """The entry of one refresh token: its name carries a hash of the issuer,
    the client and the human (letters only, as entry names are), so a token of
    another realm, client or person is never the one found."""
    raw = "\0".join((issuer.rstrip("/"), client_id, human.strip().casefold()))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return "refresh_" + "".join(chr(ord("a") + int(c, 16)) for c in digest)


def cooldown_entry(issuer: str, client_id: str, human: str) -> str:
    """The entry of one person's login cooldown: after a failed approval no new
    device approval may start for a while, and a restart is a new process.

    It sits beside the refresh token rather than inside it, because the token
    entry is rewritten on every refresh and would lose the mark (login.py).
    """
    return "cool_" + refresh_entry(issuer, client_id, human)[len("refresh_"):]


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


# --- the system keychain ----------------------------------------------------


@dataclass(frozen=True)
class Ran:
    """What a keychain tool returned; its output may hold a value, so no repr."""
    code: int
    out: bytes = field(repr=False, default=b"")
    err: bytes = field(repr=False, default=b"")


Runner = Callable[[Sequence[str], "bytes | None", float], Ran]


def run(argv: Sequence[str], stdin: bytes | None, timeout: float) -> Ran:
    """Run a keychain tool: `stdin` is its whole input (never the MCP server's
    own stdin), killed after `timeout`. The error names the tool only."""
    tool = os.path.basename(argv[0])
    try:
        p = subprocess.run(list(argv), input=stdin,
                           stdin=subprocess.DEVNULL if stdin is None else None,
                           capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise KeyStoreError(f"{tool}: timed out") from None
    except OSError:
        raise KeyStoreError(f"{tool}: cannot run") from None
    return Ran(p.returncode, p.stdout, p.stderr)


class _Keychain:
    """The shared part: entry names, locks in a 0700 directory, the timeout."""

    DEFAULT_TOOL = ""

    def __init__(self, lock_dir: Path, tool: str | None = None, *, runner: Runner = run,
                 service: str = SERVICE, timeout: float = OP_TIMEOUT) -> None:
        self._locks = HostFile(lock_dir)
        self.tool = tool or self.DEFAULT_TOOL
        self.service = service
        self.timeout = timeout
        self._run = runner

    def __repr__(self) -> str:
        return f"{type(self).__name__}(service={self.service!r}, locks={str(self._locks.directory)!r})"

    @staticmethod
    def _check(name: str) -> str:
        if not _NAME_RE.fullmatch(name):
            raise KeyStoreError("invalid store entry name")
        return name

    def load(self, name: str) -> str | None:
        raise NotImplementedError

    def save(self, name: str, value: str) -> None:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def try_lock(self, name: str) -> object | None:
        """flock on `<lock dir>/.<name>.lock`, the same file a HostFile of
        that directory takes, so every store of one host takes turns."""
        return self._locks.try_lock(name)

    def unlock(self, handle: object) -> None:
        self._locks.unlock(handle)


# errSecItemNotFound, as `security` exits with it
_MAC_NOT_FOUND = 44
# `security -i` reads lines into a buffer of its own; stay well below it.
_MAC_MAX_LINE = 4000


class MacKeychain(_Keychain):
    """The macOS Keychain through `/usr/bin/security`: generic passwords.

    A write goes as one command line on the stdin of `security -i`, never on
    its argv, and is read back: `security -i` does not report a failed
    command in its exit status. The value is stored base64-encoded, so any
    text survives the command line and the tool's printing of it.
    """

    DEFAULT_TOOL = "/usr/bin/security"

    def load(self, name: str) -> str | None:
        self._check(name)
        r = self._run([self.tool, "find-generic-password", "-s", self.service, "-a", name, "-w"],
                      None, self.timeout)
        if r.code == _MAC_NOT_FOUND:
            return None
        if r.code != 0:
            raise KeyStoreError(f"{name}: keychain read failed (exit {r.code})")
        try:
            return base64.b64decode(r.out.strip(), validate=True).decode("utf-8") or None
        except (binascii.Error, UnicodeDecodeError):
            raise KeyStoreError(f"{name}: not a value of this store") from None

    def save(self, name: str, value: str) -> None:
        self._check(name)
        data = base64.b64encode(value.encode("utf-8")).decode("ascii")
        line = (f"add-generic-password -U -s {self.service} -a {name} -l {self.service} "
                f"-w {data}\n")
        if len(line) > _MAC_MAX_LINE:
            raise KeyStoreError(f"{name}: too long for the keychain")
        r = self._run([self.tool, "-i"], line.encode("ascii"), self.timeout)
        if r.code != 0 or self.load(name) != value:
            raise KeyStoreError(f"{name}: keychain write failed")

    def delete(self, name: str) -> None:
        self._check(name)
        r = self._run([self.tool, "delete-generic-password", "-s", self.service, "-a", name],
                      None, self.timeout)
        if r.code not in (0, _MAC_NOT_FOUND):
            raise KeyStoreError(f"{name}: keychain delete failed (exit {r.code})")


class SecretTool(_Keychain):
    """libsecret (GNOME Keyring, KWallet's Secret Service) through `secret-tool`,
    the value on its stdin. A missing item is exit 1 with nothing on stderr."""

    DEFAULT_TOOL = "secret-tool"

    def _attrs(self, name: str) -> list[str]:
        return ["service", self.service, "account", self._check(name)]

    def load(self, name: str) -> str | None:
        r = self._run([self.tool, "lookup", *self._attrs(name)], None, self.timeout)
        if r.code == 1 and not r.err.strip():
            return None
        if r.code != 0:
            raise KeyStoreError(f"{name}: secret service read failed (exit {r.code})")
        try:
            return r.out.decode("utf-8").strip() or None
        except UnicodeDecodeError:
            raise KeyStoreError(f"{name}: not a value of this store") from None

    def save(self, name: str, value: str) -> None:
        attrs = self._attrs(name)
        r = self._run([self.tool, "store", "--label", f"{self.service} {name}", *attrs],
                      value.encode("utf-8"), self.timeout)
        if r.code != 0:
            raise KeyStoreError(f"{name}: secret service write failed (exit {r.code})")

    def delete(self, name: str) -> None:
        r = self._run([self.tool, "clear", *self._attrs(name)], None, self.timeout)
        if r.code != 0 and not (r.code == 1 and not r.err.strip()):
            raise KeyStoreError(f"{name}: secret service delete failed (exit {r.code})")


# --- which store ------------------------------------------------------------


def store_directory(environ: Mapping[str, str] | None = None) -> Path:
    """The 0700 directory of the file store and of every store's locks: under
    corral (`CORRAL_SANDBOX`) in the sandbox's own home, whose $HOME is the
    sandbox's private one; else `login/` in the host's state directory
    (`session_file.host_state_dir`, from the process environment)."""
    env = os.environ if environ is None else environ
    if env.get("CORRAL_SANDBOX", "").strip():
        home = env.get("HOME") or str(Path.home())
        return Path(home) / ".local" / "state" / "mm-agent-bus" / "login"
    return session_file.host_state_dir() / "login"


def _find_tool(name: str) -> str | None:
    if name == "security":
        path = "/usr/bin/security"  # the system's own, never one from PATH
        return path if os.access(path, os.X_OK) else None
    return shutil.which(name)


def _probe(store: _Keychain) -> bool:
    """Write, read and remove a throwaway entry with a random, worthless value."""
    marker = secrets.token_hex(8)
    try:
        store.save(PROBE_ENTRY, marker)
        ok = store.load(PROBE_ENTRY) == marker
        store.delete(PROBE_ENTRY)
    except KeyStoreError as exc:
        log.info("key store: %s not usable (%s), using the 0600 file", store.tool, exc)
        return False
    return ok


def choose(environ: Mapping[str, str] | None = None, *, platform: str = sys.platform,
           runner: Runner = run, find_tool: Callable[[str], str | None] = _find_tool,
           probe_timeout: float = PROBE_TIMEOUT) -> KeyStore:
    """The system keychain if it answers the probe within `probe_timeout`,
    else the 0600 file in `store_directory()`."""
    directory = store_directory(environ)
    candidate: _Keychain | None = None
    if platform == "darwin" and (tool := find_tool("security")):
        candidate = MacKeychain(directory, tool, runner=runner, timeout=probe_timeout)
    elif platform.startswith("linux") and (tool := find_tool("secret-tool")):
        candidate = SecretTool(directory, tool, runner=runner, timeout=probe_timeout)
    if candidate is not None and _probe(candidate):
        candidate.timeout = OP_TIMEOUT
        return candidate
    return HostFile(directory)


_chosen: KeyStore | None = None
_chosen_lock = threading.Lock()


def default() -> KeyStore:
    """The store the login keeps its refresh token, cooldown and nkey seed in:
    chosen on the first call (one probe per process), the same store after."""
    global _chosen
    with _chosen_lock:
        if _chosen is None:
            _chosen = choose()
        return _chosen
