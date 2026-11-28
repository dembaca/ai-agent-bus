"""Session file: the bot a session registered itself, for `claude --continue`.

session_start writes it (0600), session_end removes it; the server's exit
touches it, and the next server of the same Claude Code session takes the bot
over from it (RT-6.10). It lives next to the corral session files, so
``mm-agent-sweep.sh`` sees it too: it records CB_SESSION_PID, which the sweep
uses to tell a live session from an abandoned one. No shell reads its token:
hooks and scripts ask the server's hook API (RT-6.17), whose endpoint file is
keyed the same way (keyed_path).

Two sessions must never share a key. Under Claude Code it is the session
(CLAUDE_CODE_SESSION_ID), which Claude Code puts into the environment of the
MCP server, the hooks and every command the agent starts. Elsewhere (Cursor,
OpenCode, Codex) nothing names a session, so it is the workspace (workspace()):
one MCP server per workspace, one bot per workspace. Keyed on the project
alone, a second Claude Code session in the same checkout overwrote the first
one's file: the first agent's hooks and listener then ran as the second bot
and never saw the messages for its own (RT-6.10).
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import logging
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import config

DEFAULT_AGENT_PREFIX = "agent-"


def agent_prefix(values: Mapping[str, str] | None = None) -> str:
    """The registrar's bot username prefix (its BOT_USERNAME_PREFIX, arc42 §7.1).

    MM_AGENT_PREFIX when set; else CB_MM_BOT_USERNAME minus the name it was
    registered with (MM_REG_BOT_NAME); else "agent-". ``values`` is the
    environment by default, or a session file's keys. Must match
    mm_agent_prefix in bin/mm-agent-lib.sh.
    """
    v = os.environ if values is None else values
    if v.get("MM_AGENT_PREFIX"):
        return v["MM_AGENT_PREFIX"]
    user = config.lookup("CB_MM_BOT_USERNAME", v, quiet=values is not None) or ""
    name = v.get("MM_REG_BOT_NAME", "")
    if name and len(user) > len(name) and user.endswith(name):
        return user[: -len(name)]
    return DEFAULT_AGENT_PREFIX


@dataclass
class SessionInfo:
    url: str
    name: str
    username: str
    user_id: str
    bot_token: str
    # The registrar's signed owner claim (RT-6.24); '' when it sent none.
    attestation: str = ""

    @property
    def prefix(self) -> str:
        """The registrar's username prefix, learned from this registration."""
        return agent_prefix(
            {"CB_MM_BOT_USERNAME": self.username, "MM_REG_BOT_NAME": self.name})


# The state directory the host process chose for its session (RT-6.18): set,
# this process is the host side of a keyless corral session.
_hosted_dir: Path | None = None


def use_hosted_dir(path: Path) -> Path:
    """Make ``path`` this process's state directory for good (RT-6.18).

    The host process of a keyless session keeps its per-bot files there, never
    in a directory the sandbox names: HOME and XDG_STATE_HOME of the session
    are the agent's. Created 0700; refused unless it is a real directory of
    this user that nobody else may write.
    """
    global _hosted_dir
    if not path.is_absolute():
        raise ValueError(f"not an absolute path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_private_dir(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    ensure_private_dir(path)
    _hosted_dir = path
    return path


def hosted() -> bool:
    """This process is the host side of a keyless session (RT-6.18)."""
    return _hosted_dir is not None


# A transcript larger than this is not read at all (RT-6.18, RT-6.9).
TRANSCRIPT_MAX = 64 << 20


def claude_projects() -> str:
    """The real path of ~/.claude/projects, where Claude Code keeps transcripts."""
    return os.path.realpath(Path.home() / ".claude" / "projects")


def _fd_path(fd: int) -> str | None:
    """The path an open fd really names, or None where it cannot be told."""
    try:
        if sys.platform == "darwin":
            buf = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
            return os.fsdecode(buf.split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return None


def read_transcript(path: str | os.PathLike[str]) -> str | None:
    """A transcript's text, or None when it may not or cannot be read (RT-6.18).

    The path came from the agent's side. Never through a symlink in its last
    part, never a FIFO or device (opened non-blocking, a regular file only),
    nothing over TRANSCRIPT_MAX. In the host process of a keyless session the
    opened file must still lie under the host's ~/.claude/projects/: a
    directory swapped for a symlink after a path check is caught here.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, ValueError):
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > TRANSCRIPT_MAX:
            return None
        if hosted():
            real = _fd_path(fd)
            if not real or not real.startswith(claude_projects() + os.sep):
                return None
        chunks: list[bytes] = []
        size = 0
        while size <= TRANSCRIPT_MAX:
            chunk = os.read(fd, min(1 << 20, TRANSCRIPT_MAX + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > TRANSCRIPT_MAX:
            return None
        return b"".join(chunks).decode("utf-8")
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def state_dir() -> Path:
    if _hosted_dir is not None:
        return _hosted_dir
    return host_state_dir()


def host_state_dir() -> Path:
    """The host's state directory, shared by every server of this user: the
    session files, and the release check's release.env (RT-6.30). For the host
    process of a keyless session the one preStart chose, its own state
    directory being `<it>/hosted/<key>` (RT-6.18)."""
    if _hosted_dir is not None:
        return _hosted_dir.parent.parent
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mm-agent-bus"


# The workspace the client named in roots/list, when the environment names none.
_roots_dir: str | None = None
# Where the workspace may come from, before roots and cwd (RT-6.17 step 2).
# Read through config.env, which takes MM_WORKSPACE for CB_WORKSPACE (docs/env-names.md).
WORKSPACE_VARS = ("CLAUDE_PROJECT_DIR", "CB_WORKSPACE")


def workspace() -> tuple[str, str]:
    """The directory a session without a session id is keyed on, and its source:
    CLAUDE_PROJECT_DIR, CB_WORKSPACE (Cursor: ``${workspaceFolder}``), the
    first file:// root the client named (roots), else the server's working
    directory (cwd; OpenCode starts its servers there). Resolved, so a shell
    that walks up from its real directory meets the same name. Must match
    mm_bus_file in bin/mm-agent-lib.sh (RT-6.17 step 2)."""
    if _hosted_dir is not None:
        # The sandbox's directory, from the relay's handshake: a name only,
        # never resolved, opened or entered on the host (RT-6.18).
        return os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or "/"), "hosted"
    for var in WORKSPACE_VARS:
        value = config.env(var).strip()
        if value:
            return os.path.realpath(value), var
    if _roots_dir:
        return _roots_dir, "roots"
    return os.path.realpath(os.getcwd()), "cwd"


def too_broad(path: str) -> bool:
    """/, the home directory or a directory above it: no workspace to key an
    endpoint file on (RT-6.17 step 2), a shell anywhere below would find it."""
    home = os.path.realpath(str(Path.home()))
    path = os.path.realpath(path)
    return path == home or home.startswith(path.rstrip("/") + "/")


def unkeyed() -> str:
    """The directory the endpoint file is not keyed on, or '': without a
    session id the working directory or the client's root, when it is / or
    the home directory (or above it). A variable that names it is taken."""
    if session_id() or _hosted_dir is not None:
        return ""
    path, source = workspace()
    return path if source in ("cwd", "roots") and too_broad(path) else ""


def project_dir() -> str:
    return workspace()[0]


def root_path(uri: str) -> str | None:
    """A client's file:// root as the real path of a directory, else None."""
    parts = urlsplit(uri)
    if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
        return None
    path = unquote(parts.path)
    if not os.path.isabs(path) or not os.path.isdir(path):
        return None
    return os.path.realpath(path)


def set_roots_dir(path: str) -> bool:
    """Key on the client's first root from now on, where neither a session id
    nor the environment names the workspace. True when the key changed."""
    global _roots_dir
    if session_id() or workspace()[1] not in ("cwd", "roots"):
        return False
    before = keyed_path("mcp")
    _roots_dir = path
    return keyed_path("mcp") != before


# --- writing state files --------------------------------------------------------

def ensure_private_dir(path: Path) -> None:
    """A directory of this user, not a symlink, writable by nobody else."""
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "not a real directory", str(path))
    if st.st_uid != os.geteuid():
        raise OSError(errno.EPERM, "not this user's directory", str(path))
    if st.st_mode & 0o022:
        raise OSError(errno.EPERM, "writable by others", str(path))


def _state_parent(path: Path) -> Path:
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The host process keeps its files where the host hook chose: nothing from
    # a sandbox may redirect them (RT-6.18). Elsewhere the state dir is the
    # user's own and may be a symlink (a dotfiles setup).
    if hosted() and os.path.islink(d):
        raise OSError(errno.ELOOP, "state directory is a symlink", str(d))
    if os.path.islink(path):
        raise OSError(errno.ELOOP, "state file is a symlink", str(path))
    return d


def write_private(path: Path, text: str) -> None:
    """Replace a state file, 0600: a new file in its directory (mkstemp,
    exclusive), then renamed over it. Refuses a symlink in its place: nothing
    written here may land in the file it points to (RT-6.18)."""
    d = _state_parent(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_private(path: Path, text: str) -> None:
    """Append to a state file, 0600, never through a symlink (O_NOFOLLOW)."""
    _state_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(text)


def session_id() -> str:
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()


def keyed_path(prefix: str) -> Path:
    """A file of this session in the state dir: <prefix>-sid-<key>.env keyed on
    the session id, else <prefix>-<key>.env keyed on the workspace.
    Must match mm_bus_file in bin/mm-agent-lib.sh."""
    sid = session_id()
    if sid:
        return state_dir() / f"{prefix}-sid-{hashlib.sha1(sid.encode()).hexdigest()[:16]}.env"
    key = hashlib.sha1(project_dir().encode()).hexdigest()[:16]
    return state_dir() / f"{prefix}-{key}.env"


# --- the kept state's key (RT-6.2, ADR-014 phase 5 piece 2b) ---------------------

_BOT_ID_RE = re.compile(r"[a-z0-9]{26}")
# The session's NATS name and the home it is the name in, once the bus has it (use_nats_name).
_nats_name: tuple[str, str] | None = None
# The kinds of kept state the MCP server reads, copied to the new key once (piece 2b: last,
# threads, unanswered; piece 14c-1: the rest). A bare kind is <kind>-<key>.env, one with a dot
# names its extension. Not copied: the locks (a lock belongs to its process), channel- (this
# process's channel confirmation, moved by latch), nonce- (an input of the name itself, so it
# stays keyed by the bot id) and the files of one turn the hooks keep (listen-, update-, turn-).
STATE_KINDS = ("last", "threads", "unanswered", "asks", "read", "remind", "gate", "stall", "notice",
               "askmode", "presence", "coord", "stats.jsonl", "crown.log", "permission.log")
# Moved, not copied: the counters, so no part publishes them twice (RT-6.9 step 2).
MOVED_KINDS = frozenset({"stats.jsonl"})


def _kind_file(kind: str, key: str) -> str:
    """The file name of a STATE_KINDS entry under `key`."""
    base, dot, ext = kind.partition(".")
    return f"{base}-{key}.{ext}" if dot else f"{kind}-{key}.env"


def use_nats_name(name: str, home: str) -> None:
    """The bus knows the session's NATS name `name` (as the agent of `home`). With plan B
    piece 2 on for `home` right now, the kept state is keyed by it from now on, for the
    rest of the process (RT-6.2 step 9): the switch is read here once and latched, so a
    later flip or the bus detaching its switch source at stop() (before the exit handler's
    BYE) moves no file. Else the key stays the bot id."""
    from . import planb

    global _nats_name
    _nats_name = (name, home) if name and key_ok(name) and planb.on(2, home) else None
    if _nats_name is not None and _key_listener is not None:
        try:
            _key_listener()
        except Exception as exc:  # noqa: BLE001 — bookkeeping (the endpoint file)
            logging.getLogger(__name__).debug("state key not announced: %s", type(exc).__name__)


# Told once the key is latched: the hook API writes it into its endpoint file (CB_STATE_KEY),
# so mm-agent-sweep.sh keeps the name's files while this server runs (RT-6.1 step 7).
_key_listener: Callable[[], None] | None = None


def on_state_key(listener: Callable[[], None] | None) -> None:
    """Call `listener` when use_nats_name latches a NATS name (one per process: the last set)."""
    global _key_listener
    _key_listener = listener


def nats_state_key() -> str:
    """The NATS name the kept state is keyed by (RT-6.2 step 9), "" while that is the bot id."""
    return _nats_name[0] if _nats_name else ""


def key_ok(key: str) -> bool:
    """Whether `key` may name a state file: a Mattermost bot id (26 characters) or a NATS
    name (natsbus.AGENT_RE) without capitals (on a case-insensitive file system two names
    differing in case would share one file; NATS names from the hash are lower case),
    nothing else (no path part, no dot)."""
    from .natsbus import AGENT_RE

    return bool(_BOT_ID_RE.fullmatch(key) or (AGENT_RE.fullmatch(key) and key == key.lower()))


def state_key(uid: str, nats_name: str | None = None, home: str | None = None) -> str:
    """The key of the session's kept state (RT-6.2 step 9): the NATS name when use_nats_name
    latched it (plan B piece 2 was on at the start), else `uid`, the bot's Mattermost id.
    An empty `uid` (no bot) is the latched name too, else empty: no key, no state (piece 14c-1).
    `nats_name` is the session's own, unless named (tests); `home` is unused then."""
    name = nats_name if nats_name is not None else (_nats_name[0] if _nats_name else None)
    if name is not None and key_ok(name):
        return name
    return uid


def copy_state_once(uid: str, name: str, kinds: tuple[str, ...] = STATE_KINDS) -> list[str]:
    """Copy the kept state keyed by the bot id `uid` to the files keyed by the NATS name
    `name` (RT-6.2 step 9), per kind: a missing `<kind>-<name>.env` is made from an existing
    `<kind>-<uid>.env`, unless the marker says it was copied and the session deleted it
    since (the source is then not newer than the marker); an existing one is replaced only
    by a source with a newer mtime (a switch that was off in between wrote the bot id's
    file), never by an older source. A symlink as source or target is refused. The source
    stays where it is, so the old plugin still finds its copy. The marker
    `copied-<name>.env` holds the source ids. The source is a bot id or, at a takeover
    (_copy_source), the name of a predecessor. The counters (`stats.jsonl`) are moved, not
    copied: appended to the name's file and removed at the source, so no part publishes them
    twice. Returns the kinds copied."""
    if not (key_ok(uid) and key_ok(name)) or uid == name:
        return []
    d = state_dir()
    marker = d / f"copied-{name}.env"
    ids = _marker_ids(marker) if marker.exists() else []
    try:
        marked_at = marker.stat().st_mtime_ns if uid in ids else None
    except OSError:
        marked_at = None
    done: list[str] = []
    for kind in kinds:
        src, dst = d / _kind_file(kind, uid), d / _kind_file(kind, name)
        try:
            if dst.is_symlink() or src.is_symlink() or not src.is_file():
                continue
            if kind in MOVED_KINDS:
                if text := src.read_text():
                    append_private(dst, text)
                    done.append(kind)
                src.unlink()
                continue
            src_at = src.stat().st_mtime_ns
            if dst.exists():
                if src_at <= dst.stat().st_mtime_ns:
                    continue
            elif marked_at is not None and src_at <= marked_at:
                continue
            write_private(dst, src.read_text())
            done.append(kind)
        except OSError:
            continue
    if done or uid not in ids:
        _mark(marker, add_ids=[uid])
    return done


def _marker_lines(marker: Path) -> list[str]:
    try:
        return marker.read_text().splitlines() if not marker.is_symlink() else []
    except OSError:
        return []


def _mark(marker: Path, add_ids: tuple[str, ...] | list[str] = (), add: tuple[str, ...] | list[str] = ()) -> None:
    """Rewrite `marker` (copied-<name>.env) with the ids `add_ids` among its CB_COPIED_FROM
    lines and the lines `add`, keeping every line it has (CB_TIE, CB_TAKEN_BY). Never raises."""
    lines = _marker_lines(marker)
    ids = [ln for ln in lines if ln.startswith("CB_COPIED_FROM=")]
    rest = [ln for ln in lines if ln and not ln.startswith("CB_COPIED_FROM=")]
    ids += [f"CB_COPIED_FROM={i}" for i in add_ids if f"CB_COPIED_FROM={i}" not in ids]
    rest += [ln for ln in add if ln not in rest]
    try:
        write_private(marker, "".join(f"{ln}\n" for ln in ids + rest))
    except OSError:
        pass


# --- the latch: one holder per name (plan B piece 14c-1, RT-6.2 step 9) ---------------

# The open lock file key-<name>.lock this process holds for its whole run, so no second
# process keys its state by the same name; and why the latch was refused, for bus_info.
_key_fds: dict[str, int] = {}
_refused = ""


def _hold_key(name: str) -> str:
    """Take key-<name>.lock (flock, never waits) for the rest of the process. "" when held,
    else why not: another live process holds it, or the lock cannot be made (fail-safe:
    a key no process can lock is no key)."""
    if name in _key_fds:
        return ""
    path = state_dir() / f"key-{name}.lock"
    try:
        _state_parent(path)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        return f"the lock key-{name}.lock cannot be made ({type(exc).__name__})"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return "another running MCP server keys its state by the same NATS name"
        return f"the lock key-{name}.lock cannot be taken ({type(exc).__name__})"
    _key_fds[name] = fd
    return ""


def lock_key(uid: str) -> str:
    """The key of a lock meant for one sender per bot (the reminders, the ask queue, the stats
    part): the bot id whenever there is one, so two servers of one bot (one latched on the
    name, one refused) still take turns; else the state key."""
    return uid if _BOT_ID_RE.fullmatch(uid) else state_key(uid)


# The bot-id files this process wrote as mirrors of its name-keyed ones (mixed versions,
# RT-6.2 step 9): path -> (text, mtime_ns). A mirror is rewritten or removed only while it is
# still what this process wrote: a second server of the bot, refused the name, keys its own
# state by the bot id and owns the file then.
_mirrors: dict[str, tuple[str, int]] = {}


def _mine(path: Path) -> bool:
    kept = _mirrors.get(str(path))
    try:
        return kept is not None and path.stat().st_mtime_ns == kept[1] and path.read_text() == kept[0]
    except OSError:
        return False


def write_mirror(path: Path, text: str) -> None:
    """Write the bot-id mirror `path`, unless another process's file is there."""
    if path.is_symlink() or (path.exists() and not _mine(path)):
        return
    try:
        write_private(path, text)
        _mirrors[str(path)] = (text, path.stat().st_mtime_ns)
    except OSError:
        pass


def drop_mirror(path: Path) -> None:
    """Remove the bot-id mirror `path`, only if this process wrote it."""
    if _mine(path):
        path.unlink(missing_ok=True)
    _mirrors.pop(str(path), None)


def session_tie() -> tuple[str, str]:
    """Which session this process serves, across its restarts (RT-6.2 step 9, the takeover):
    ("claude", CLAUDE_CODE_SESSION_ID), ("opencode", OPENCODE_SESSION_ID, the OpenCode
    session the plugin bound, RT-6.20), ("pi", PI_SESSION_ID, the pi session's id the
    extension passes, RT-6.27), else ("workspace", the workspace keyed_path keys on). Only
    read: keyed_path and session_id are unchanged, so no endpoint, session or nonce file
    moves."""
    for harness, var in (("claude", "CLAUDE_CODE_SESSION_ID"), ("opencode", "OPENCODE_SESSION_ID"),
                         ("pi", "PI_SESSION_ID")):
        if value := os.environ.get(var, "").strip():
            return harness, value
    return "workspace", project_dir()


def tie() -> str:
    """CB_TIE as the marker holds it: `<harness>:<first 16 hex of sha1(id)>`, the hash
    keyed_path uses, so no raw session id or directory goes into a file."""
    harness, value = session_tie()
    return f"{harness}:{hashlib.sha1(value.encode()).hexdigest()[:16]}"


def _gone(name: str) -> bool:
    """Whether no process holds key-<name>.lock: a non-blocking flock on it succeeds (it is
    released at once), or there is no lock file. A symlink or an unreadable lock is not gone."""
    path = state_dir() / f"key-{name}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _predecessor(name: str) -> str | None:
    """The newest other name (by its marker's mtime) whose marker carries this process's
    CB_TIE and no CB_TAKEN_BY: the name this session ran under before a restart."""
    me, best = f"CB_TIE={tie()}", None
    for marker in state_dir().glob("copied-*.env"):
        pred = marker.name.removeprefix("copied-").removesuffix(".env")
        if pred == name or not key_ok(pred) or _BOT_ID_RE.fullmatch(pred) or marker.is_symlink():
            continue
        lines = _marker_lines(marker)
        if me not in lines or any(ln.startswith("CB_TAKEN_BY=") for ln in lines):
            continue
        try:
            at = marker.stat().st_mtime_ns
        except OSError:
            continue
        if best is None or at > best[0]:
            best = (at, pred)
    return best[1] if best else None


def _copy_source(bot_id: str, name: str) -> str | None:
    """The key whose kept state the latch copies to `name` (RT-6.2 step 9). Decided by the
    owner (#328): the state carries over across a restart for every harness, CB_LOGIN_NKEY
    stays ephemeral (every process a new name). So: the newest predecessor name of the same
    session (_predecessor, by CB_TIE), when its lock proves it gone (_gone); else, with no
    such name or the predecessor still running, the bot id; None without either."""
    pred = _predecessor(name)
    if pred is not None and _gone(pred):
        return pred
    return bot_id if _BOT_ID_RE.fullmatch(bot_id) and bot_id != name else None


def latch(name: str, home: str, uid: str) -> list[str]:
    """The bus has the session's NATS name `name` in `home` (RT-6.2 step 9): with plan B piece 2
    on there, take the name's lock, copy the state the bot `uid` kept (copy_state_once), write
    the marker even without a bot (so mm-agent-sweep.sh's day rule applies to the name's files),
    move this process's channel confirmation, and only then latch the key (use_nats_name): a
    hook that learns the key appends to a file that already holds the copy. A second process
    with the same name refuses: its key stays the bot id (or none) and latch_refused() says
    why. Returns the kinds copied."""
    from . import planb

    global _refused
    if not (name and key_ok(name) and planb.on(2, home)):
        return []
    first = _nats_name is None  # the latch is once per process; a repeat moves nothing
    if why := _hold_key(name):
        _refused = why
        logging.getLogger(__name__).warning("session: the kept state stays keyed by the bot id: %s", why)
        return []
    src_key = _copy_source(uid, name) if first else None
    copied = copy_state_once(src_key, name) if src_key else []
    d = state_dir()
    marker = d / f"copied-{name}.env"
    if first and not marker.is_symlink():
        # The tie names the session for its next restart; the bot id ties the name to the
        # bot for the sweep (RT-6.1 step 7), also after a takeover from a predecessor name.
        _mark(marker, add_ids=[uid] if _BOT_ID_RE.fullmatch(uid) else [], add=[f"CB_TIE={tie()}"])
        if src_key and src_key != uid:
            # The predecessor's files are taken over: its marker says by whom, so no later
            # start takes them again and the sweep no longer ties them to the bot.
            _mark(d / f"copied-{src_key}.env", add=[f"CB_TAKEN_BY={name}"])
    # channel-<key>.env says this process confirmed the channel (RT-6.12): an earlier process
    # of the name left nothing that counts, and a confirmation made under the bot id moves.
    src, dst = d / f"channel-{uid}.env", d / f"channel-{name}.env"
    try:
        if first and not dst.is_symlink():
            dst.unlink(missing_ok=True)
            if _BOT_ID_RE.fullmatch(uid) and src.is_file() and not src.is_symlink():
                text = src.read_text()
                write_private(dst, text)
                # Made before the latch, by this process (the start removed any older one):
                # from now on its mirror.
                _mirrors[str(src)] = (text, src.stat().st_mtime_ns)
    except OSError:
        pass
    use_nats_name(name, home)
    return copied


def latch_refused() -> str:
    """Why this process keys its state by the bot id although piece 2 is on ("" when not)."""
    return _refused


def _marker_ids(marker: Path) -> list[str]:
    try:
        lines = marker.read_text().splitlines()
    except OSError:
        return []
    return [line.partition("=")[2] for line in lines if line.startswith("CB_COPIED_FROM=")]


NONCE_RE = re.compile(r"^[a-z2-7]{26}$")


def _nonce_in(path: Path) -> str:
    """The kept part in a state file, "" for none (a damaged file warns)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        k, _, v = line.partition("=")
        if k == "CB_SESSION_NONCE" and NONCE_RE.fullmatch(v):
            return v
    if text.strip():
        logging.getLogger(__name__).warning("session: the nonce file is damaged: a new part is "
                                            "made, so this session gets a new name")
    return ""


def _keep_nonce(path: Path, value: str) -> None:
    try:
        write_private(path, f"CB_SESSION_NONCE={value}\n")
    except OSError as exc:  # not kept: a restart gets a new name, the log says so
        logging.getLogger(__name__).warning("session: the nonce was not kept (%s): a restart "
                                            "gets a new name", type(exc).__name__)


def nonce() -> str:
    """The session's own part of the agent name (#163, RT-6.36): 16 random bytes as
    26 lower-case base32 characters, made once and kept per bot
    (`nonce-<uid>.env`), so a resumed session (`--resume`, `--continue`) and a
    restarted server keep their name while two live sessions — which have two
    bots — never share one. A server that registered late takes the part its
    earlier start kept under the session's key, so its name does not change once
    more."""
    uid = config.value(read(), "CB_MM_BOT_USER_ID")
    if uid:
        path = state_dir() / f"nonce-{uid}.env"
        if value := _nonce_in(path):
            return value
        # The first start of a server that registered later: claim the part its
        # earlier start kept. The rename takes the keyed file away, so only one
        # bot can adopt it; a second one makes its own (two live sessions never
        # share a name).
        keyed = keyed_path("nonce")
        try:
            # Claim it by the rename first, then read: two bots racing get one
            # winner, and the loser's rename finds nothing and makes its own part.
            os.replace(keyed, path)
        except FileNotFoundError:
            pass
        except OSError:
            pass  # an unusable keyed file: a new part is made below
        if value := _nonce_in(path):
            return value
    else:
        path = keyed_path("nonce")
        if value := _nonce_in(path):
            return value
    value = base64.b32encode(os.urandom(16)).decode().lower().rstrip("=")
    _keep_nonce(path, value)
    return value


def session_file_path() -> Path:
    return keyed_path("mcp")


def write(info: SessionInfo, team: str, channel: str, project: str = "") -> Path:
    """Write the session file, 0600 in a 0700 directory. Returns its path.
    project: CB_MM_PROJECT_CHANNEL, the home first, then the guests (RT-6.22)."""
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Each renamed key under all its names, the alias keys MATTERMOST_URL and
    # MATTERMOST_TOKEN included: a hook or server of the version before reads
    # the old ones (docs/env-names.md). The registrar's keys keep theirs.
    def both(name: str, value: str) -> list[str]:
        return [f"{k}={v}" for k, v in config.both(name, value).items()]

    lines = [
        "# ephemeral session — do not commit",
        *both("CB_MM_CHAT_URL", info.url),
        f"MM_REG_BOT_NAME={info.name}",
        *both("CB_MM_BOT_USERNAME", info.username),
        f"MM_AGENT_PREFIX={info.prefix}",
        *both("CB_MM_BOT_USER_ID", info.user_id),
        *both("CB_MM_BOT_TOKEN", info.bot_token),
        *both("CB_MM_TEAM", team),
        *both("CB_MM_CHANNEL", channel),
        *(both("CB_MM_PROJECT_CHANNEL", project) if project else []),
        # The registrar's signed owner claim (RT-6.24): not secret, kept for adopt.
        *([f"MM_BOT_ATTESTATION={info.attestation}"] if info.attestation else []),
        *both("CB_SESSION_PID", str(os.getppid())),
    ]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def parse(path: Path) -> dict[str, str]:
    """KEY=value lines of a session file, in the order of their last lines; {} when unreadable."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out.pop(k, None)  # a key sits at its last line: config.value takes the last spelling
            out[k] = v
    return out


def _pid_alive(pid: str) -> bool:
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# A server without a session id that ends without session_end appends this
# mark (epoch s) to its workspace's file and keeps the bot: the next server of
# that workspace takes it over within the grace period (RT-6.10 step 7). Only
# an ended server writes it, so a server still running there keeps its bot.
ENDED = "CB_SERVER_ENDED"


def keep_sec() -> float:
    """The grace period of a kept bot: CB_SESSION_KEEP_SEC, default 24 h."""
    try:
        return float(config.env("CB_SESSION_KEEP_SEC") or 86400)
    except ValueError:
        return 86400.0


def keyed_on_workspace() -> bool:
    """No session id, no keyless session, and a workspace below the home
    directory: the file is this workspace's (RT-6.17 step 2)."""
    return not session_id() and not hosted() and not unkeyed()


def ended_at(data: Mapping[str, str]) -> float | None:
    try:
        return float(config.value(data, ENDED) or "")
    except ValueError:
        return None


def mark_ended(token: str) -> bool:
    """Keep the bot for the workspace's next server (RT-6.10 step 7): only
    when the file still holds this server's bot."""
    path = session_file_path()
    if not token or config.value(parse(path), "CB_MM_BOT_TOKEN") != token:
        return False
    try:
        append_private(path, "".join(f"{k}={v}\n" for k, v in config.both(ENDED, str(int(time.time()))).items()))
    except OSError:
        return False
    return True


def _claim_ended(path: Path) -> dict[str, str]:
    """The workspace file a server ended with, within the grace period, moved
    out of the way first: of two servers starting at once only one gets it."""
    ended = ended_at(parse(path))
    if ended is None or time.time() - ended > keep_sec():
        return {}
    claimed = path.with_name(f".{path.name}.taken-{os.getpid()}")
    try:
        os.rename(path, claimed)
    except OSError:
        return {}
    try:
        return parse(claimed)
    finally:
        claimed.unlink(missing_ok=True)


def read() -> dict[str, str]:
    """The bot to take over after a restart (RT-6.10): this Claude Code
    session's file, or without a session id the workspace's file once its
    server ended without session_end, within the grace period. A workspace
    file without that mark belongs to a server that still runs."""
    if session_id():
        data = parse(session_file_path())
    elif keyed_on_workspace():
        data = _claim_ended(session_file_path())
    else:
        return {}
    return data if config.value(data, "CB_MM_BOT_TOKEN") and data.get("MM_REG_BOT_NAME") else {}


def touch() -> None:
    """Mark the moment the session's MCP server ended: the start of the grace
    period after which another session's start removes the bot."""
    try:
        os.utime(session_file_path())
    except OSError:
        pass


def others() -> list[Path]:
    """Session files of other MCP-registered sessions on this host."""
    own = session_file_path()
    return [p for p in sorted(state_dir().glob("mcp-*.env")) if p != own]


def remove() -> None:
    try:
        session_file_path().unlink()
    except FileNotFoundError:
        pass
