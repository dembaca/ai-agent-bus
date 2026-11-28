"""The release notice (ADR-011 phase 2, RT-6.30 in docs/arc42/06-runtime-view.md).

Two sources, one switch: with `CB_RECORDS_URL` unset, the host's release check
bin/mm-agent-release-check.sh lists the newest release tags, checks them as the
installer does (RT-6.29: with the pinned keys when the host sets
CB_RELEASE_SIGNERS) and writes what the CHANGELOG of the newest one that passed
says into release.env in the host state directory; this module reads that file
and checks every field again. With `CB_RECORDS_URL` set (plan B piece 14b,
ADR-014), the bridge is the only source: `GET <address>/register/v1/release`
answers the bridge's compact JWS, verified fail-closed against the pinned keys
(`attest.verify_release`), and the host's check is not read or started. Any
doubt there is no notice, never the file's. What reaches the agent is a version,
the step (major, minor, patch), `Breaking`, the `Needs:` lines in their grammar
and whether the record's signature was checked; never free text.

It also starts that check, at most once a day, as a child with a clean
environment and a timeout that writes only its state (check_loop); with
`CB_RECORDS_URL` set it reads the record instead and starts no check. Nothing
here installs anything: a human installs a release on the host
(`mm-agent-host-install.sh --release v<x.y.z>`) or, with the record, through the
harness's marketplace, never the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import re
import signal
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import attest, config, selfupdate, session_file

log = logging.getLogger(__name__)

# The tree this server runs from: a checkout, the plugin, or releases/<name>/
# of the host copy (RT-6.28).
ROOT = Path(__file__).resolve().parents[3]
# The bus skill beside the server (optional in a host copy): the handoff after
# a compaction (RT-6.7) and the overhead counts (RT-6.9) read it.
SKILL_FILE = ROOT / "skills" / "ai-agent-bus" / "SKILL.md"
STATE_FILE = "release.env"
# Plan B piece 14b (ADR-014): the bridge's address for the signed records. Unset
# is off, and today's host check path stays byte for byte. Later the setting
# serves the other signed records too; only the release record is read here yet.
RECORDS_ENV = "CB_RECORDS_URL"
# TODO(ADR-014 answer 4): MM_REGISTRAR_KEYS keeps its name until the decision
# release names its CB_ spelling (docs/env-names.md, "Goes with the registrar").
RECORD_PATH = "/register/v1/release"
RECORD_EVERY = 600.0      # the record is signed fresh per read; look again every ten minutes
RECORD_HOUR = 3600        # a record's whole validity (services/bridge/internal/release)
# What a human does with the record's version: the harness's marketplace, never
# the agent, and no self-update (RT-6.32 goes in a later piece).
RECORD_NOTE = ("a human installs a release through the harness's marketplace; you never run "
               "the installer")
STAMP_FILE = "release-check.at"          # the last check that succeeded (the script writes it)
FAILED_FILE = "release-check.failed"     # the failed tries, one epoch a line (this module)
SCRIPT = Path("bin") / "mm-agent-release-check.sh"
HOST_DIR = Path.home() / ".local" / "share" / "mm-agent-bus" / "current"
CHECK_EVERY = 86400       # at most once a day (ADR-011 point 3)
TRIES = 3                 # failed tries a day, one per POLL
CHECK_TIMEOUT = 120.0     # the child's bound; then TERM, and KILL after KILL_GRACE
KILL_GRACE = 2.0
FIRST_DELAY = 60.0        # not while the session starts
POLL = 3600.0
STATE_MAX = 65536
# What the child gets of this server's environment besides PATH and HOME:
# where the releases come from, the pinned keys, where src.git lies, the
# session's workdir (which the script's own sandbox check counts), and what
# reading the registrar's release record takes (RT-6.32): its pinned keys, its
# URL, TLS and proxy settings. Never a token, the registration secret or
# anything else of the session.
# Both spellings of each renamed name (docs/env-names.md, "Lists of names"):
# without the new ones a host that set only CB_RELEASE_SIGNERS would check
# without its signers.
CHILD_ENV = ("CB_RELEASE_URL", "MM_RELEASE_URL", "CB_RELEASE_SIGNERS", "MM_RELEASE_SIGNERS",
             "CB_BUS_HOST_DIR", "MM_BUS_HOST_DIR", "CORRAL_WORKDIR",
             "MM_REGISTRAR_KEYS", "MM_REGISTER_URL", "CB_MM_CHAT_URL", "MM_CHAT_URL", "SSL_CERT_FILE", "SSL_CERT_DIR",
             "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")

_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# A TARGET when the registrar names `latest` (RT-6.32 step 4): the newest tag
# the host's release check knows that is not withdrawn, once it knows one.
# CHECKED_AT: epoch seconds, as the release check writes them; read as
# mm_release_notice reads it (no leading zero, at most ten digits).
_EPOCH = re.compile(r"^[1-9][0-9]{0,9}$")
_LATEST = re.compile(r"^latest(?: \(v(\d+\.\d+\.\d+)\))?$")
# The bridge's address: scheme, host (IPv6 in brackets) and a plain path, no
# credentials, query or fragment (selfupdate.release_url reads alike).
_HTTP_URL = re.compile(r"^(https?)://([A-Za-z0-9.:\[\]-]+)(/[A-Za-z0-9._~%+-]*)*$")

# The Needs: grammar (ADR-011 point 3, CLAUDE.md "Releasing"): `CB_*` and
# `MM_*` names (the old ones while their fallbacks and the registrar's names
# last, docs/env-names.md "The Needs: rule"), versions x.y.z and lowercase words of at most 15 letters, each with an
# optional `,` or `.`, one space between, at most NEEDS_MAX characters. The
# same text as CB_NEEDS_RE in bin/mm-agent-lib.sh; scripts/doc-check.py reads
# both from here.
NEEDS_PATTERN = r"^((CB|MM)_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?( ((CB|MM)_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?)*$"
NEEDS_MAX = 120
_NEEDS = re.compile(NEEDS_PATTERN)
NOTE = ("a human installs a release on the host (mm-agent-host-install.sh --release), or the "
        "host copy updates itself between sessions to the version the registrar names (RT-6.32); "
        "you never run the installer")
# Self-update (ADR-011 phase 3, RT-6.32): what the installer's --auto and the
# release check last saw of the registrar's release record, in the host state
# directory; the hold beside releases/.
AUTO_FILE = "release-auto.env"
HOLD_FILE = "release-hold"
_AUTO_WORDS = {"REGISTRAR": ("ok", "none", "failed", "nokeys"),
               "KEYS": ("none", "match", "mismatch", "bad"),
               "RESULT": ("updated", "failed", "waiting", "held", "nothing", "refused", "current")}


def needs_ok(text: str) -> bool:
    """A `Needs:` line's text in its grammar (RT-6.30)."""
    return 0 < len(text) <= NEEDS_MAX and _NEEDS.fullmatch(text) is not None


def _key(v: str) -> tuple[int, int, int]:
    m = _VER.match(v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (0, 0, 0)


def _plugin_version() -> str:
    try:
        v = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    return v if isinstance(v, str) and _VER.match(v) else ""


VERSION = _plugin_version()


def _read(path: Path, limit: int) -> str | None:
    """A regular file's text, never through a symlink, at most `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def running(root: Path | None = None) -> tuple[str, str]:
    """What kind of tree this server runs (RT-6.29 step 7, RT-6.30): ("signed",
    tag) for a release whose signature the installer verified, ("release", tag)
    for one installed without the keys, ("plugin", "") for a tree without a
    SOURCE file that is no host copy's release (the plugin, a checkout), else
    ("unsigned", ""): `--from`, a clone, a host copy from before 0.81.0. Like
    mm_release_kind, which tells host copies only."""
    root = root or ROOT
    text = _read(root / "SOURCE", 4096)
    if text is None:
        return ("unsigned", "") if root.parent.name == "releases" else ("plugin", "")
    lines = text.splitlines()
    tag = next((ln[4:] for ln in lines if ln.startswith("TAG=")), "")
    if _TAG.match(tag):
        if "SOURCE=signed" in lines:
            return "signed", tag
        if "SOURCE=release" in lines:
            return "release", tag
    return "unsigned", ""


def source_label(root: Path | None = None) -> str:
    kind, tag = running(root)
    return {"signed": f"signed release {tag}", "release": f"release {tag} (signature not checked)",
            "plugin": "plugin"}.get(kind, "unsigned")


# The roster's version (ADR-011 point 6): one segment of the status text,
# `v0.83.0`, `v0.83.0 unchecked`, `v0.83.0 unsigned` or `v0.83.0 plugin`; older
# readers skip a segment they do not know (usage.from_status, home_of, the
# status word). lease.status_text cuts it before the label.
_SOURCE_WORD = {"signed": "", "release": "unchecked", "unsigned": "unsigned", "plugin": "plugin"}
_SEGMENT = re.compile(r"^v(\d+\.\d+\.\d+)(?: (unchecked|unsigned|plugin))?$")


def segment() -> str:
    """The status segment of this server's version; '' when it has none."""
    if not VERSION:
        return ""
    word = _SOURCE_WORD[running()[0]]
    return f"v{VERSION} {word}" if word else f"v{VERSION}"


def from_status(text: str) -> dict[str, str]:
    """The version an agent's status text names: {version[, source]}, source
    "unchecked", "unsigned" or "plugin" (none: a signed release); {} when it
    names none."""
    for piece in str(text).split(" · "):
        m = _SEGMENT.match(piece.strip())
        if m:
            return {"version": m[1], **({"source": m[2]} if m[2] else {})}
    return {}


@dataclass
class State:
    """release.env, every field checked again; or the bridge's last verified
    record (`origin="record"`, `expires_at` its exp)."""
    newest: str
    verified: bool
    checked_at: int = 0
    breaking: set[str] = field(default_factory=set)
    needs: list[tuple[str, str]] = field(default_factory=list)
    withdrawn: set[str] = field(default_factory=set)
    origin: str = "check"  # "check": release.env; "record": the bridge's signed record
    expires_at: int = 0    # a record's exp (unix seconds); 0 for release.env


def state_dir() -> Path:
    return session_file.host_state_dir()


def read_state(directory: Path | None = None) -> State | None:
    """What the host's last check that passed found; None for no check yet or
    a file without its fields. A line outside its form is skipped."""
    text = _read((directory or state_dir()) / STATE_FILE, STATE_MAX)
    if text is None:
        return None
    fields: dict[str, str] = {}
    breaking: set[str] = set()
    needs: list[tuple[str, str]] = []
    withdrawn: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        ver, _, rest = value.partition(" ")
        if key in ("NEWEST", "VERIFIED", "CHECKED_AT"):
            fields[key] = value
        elif key == "ENTRY" and _VER.match(ver) and rest == "breaking":
            breaking.add(ver)
        elif key == "NEEDS" and _VER.match(ver) and needs_ok(rest) and (ver, rest) not in needs:
            needs.append((ver, rest))
        elif key == "WITHDRAWN" and _VER.match(value):
            withdrawn.add(value)
    newest, verified = fields.get("NEWEST", ""), fields.get("VERIFIED", "")
    if not _VER.match(newest) or verified not in ("yes", "no"):
        return None
    # A release withdraws only older ones.
    withdrawn = {v for v in withdrawn if _key(v) < _key(newest)}
    at = fields.get("CHECKED_AT", "")
    return State(newest=newest, verified=verified == "yes",
                 checked_at=int(at) if _EPOCH.match(at) else 0,
                 breaking=breaking, needs=needs, withdrawn=withdrawn)


def _between(st: State, have: str) -> tuple[bool, list[str]]:
    """Breaking and the Needs: lines of the entries after `have` up to the
    newest release: an update from `have` needs what each of them needs."""
    lo, hi = _key(have), _key(st.newest)
    breaking = any(lo < _key(v) <= hi for v in st.breaking)
    needs = list(dict.fromkeys(t for v, t in st.needs if lo < _key(v) <= hi))
    return breaking, needs


def _step(have: str, newest: str) -> str:
    a, b = _key(have), _key(newest)
    return "major" if a[0] != b[0] else "minor" if a[1] != b[1] else "patch"


def notice(have: str | None = None, state: State | None = None,
           directory: Path | None = None) -> dict[str, Any]:
    """bus_info's `release` (RT-6.30): this server's version and source, and
    what the host's check found: the newest release that passed, the step to
    it, Breaking and the Needs: lines of the entries in between, whether its
    signature was checked, this version withdrawn, the newest older than this."""
    have = VERSION if have is None else have
    if not _VER.match(have):
        return {}  # this server's own version unknown: nothing to compare
    out: dict[str, Any] = {"this": {"version": have, "source": source_label()},
                           "auto": auto_info(have, directory)}
    st = state if state is not None else read_state(directory)
    if st is None:
        out["check"] = "none yet: the host's release check has not found a release that passes"
        auto = auto_line(have, directory)
        if auto:
            out["notice"] = auto
            out["note"] = NOTE
        return out
    out["newest"] = st.newest
    out["signature"] = "checked" if st.verified else "not checked"
    if st.checked_at:
        out["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.checked_at))
    if _key(st.newest) > _key(have):
        breaking, needs = _between(st, have)
        out["step"] = _step(have, st.newest)
        out["breaking"] = breaking
        if needs:
            out["needs"] = needs
    if have in st.withdrawn:
        out["withdrawn"] = True
    if older(st, have):
        out["older"] = True
    text = line(have, st)
    if text:
        out["notice"] = text
        out["note"] = NOTE
    return out


def older(st: State, have: str | None = None) -> bool:
    """The newest release the check found is older than this server's version
    (RT-6.30 step 6): never one to install."""
    have = VERSION if have is None else have
    return bool(_VER.match(have)) and _key(st.newest) < _key(have)


def line(have: str | None = None, state: State | None = None,
         directory: Path | None = None) -> str:
    """One line for the human (the turn-end banner, RT-6.2 step 8); '' when
    there is nothing to say. The same text as mm_release_notice in
    bin/mm-agent-lib.sh when the host's check is the source."""
    have = VERSION if have is None else have
    st = state if state is not None else read_state(directory)
    if not _VER.match(have):
        return ""
    parts: list[str] = []
    if st is not None:
        parts += _release_parts(st, have)
    auto = auto_line(have, directory)  # RT-6.32, also before the first release check
    if auto:
        parts.append(auto)
    return "; ".join(parts)


def _release_parts(st: State, have: str) -> list[str]:
    parts: list[str] = []
    if have in st.withdrawn:
        if st.origin == "record":
            parts.append(f"this host's {have} is withdrawn (the release record of v{st.newest})")
        else:
            parts.append(f"this host's {have} is withdrawn (CHANGELOG of v{st.newest})")
    if _key(st.newest) > _key(have):
        breaking, needs = _between(st, have)
        what = _step(have, st.newest)
        if breaking:
            what += ", Breaking"
        if needs:
            what += "; needs: " + "; ".join(needs)
        if st.origin == "record":
            what += "; record signed"
        else:
            what += "; signature checked" if st.verified else "; signature not checked"
        parts.append(f"release v{st.newest} is out ({what})")
    elif _key(st.newest) < _key(have):
        # Never a step back: the check may just not have run since this version.
        when = f" of {time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(st.checked_at))}" if st.checked_at else ""
        if st.origin == "record":
            parts.append(f"the release record{when} names v{st.newest} as newest, older than "
                         f"{have}: the record is stale, or this host runs a build it does not know")
        else:
            parts.append(f"the release check{when} found v{st.newest} as newest, older than {have}: "
                         "not run since, or the repository is frozen or rewound")
    return parts


# --- self-update between sessions (RT-6.32) ------------------------------------------

def layout_dir(root: Path | None = None) -> Path | None:
    """The host copy's layout directory (releases/' parent) when this server
    runs from one of its releases; None for the plugin or a checkout, which
    never update themselves."""
    root = root or ROOT
    return root.parent.parent if root.parent.name == "releases" else None


def held(layout: Path | None) -> bool:
    """release-hold beside releases/: anything of that name holds."""
    if layout is None:
        return False
    try:
        os.lstat(layout / HOLD_FILE)
    except OSError:
        return False
    return True


def read_auto(directory: Path | None = None) -> dict[str, str]:
    """release-auto.env, every field checked again: REGISTRAR, KEYS and RESULT
    as their words, TARGET a version, `latest` or `latest (v<x.y.z>)`, AT
    and RESULT_AT epochs."""
    text = _read((directory or state_dir()) / AUTO_FILE, 4096) or ""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        key, sep, value = ln.partition("=")
        if not sep:
            continue
        if key in _AUTO_WORDS and value in _AUTO_WORDS[key]:
            out[key] = value
        elif key == "TARGET" and (_VER.match(value) or _LATEST.match(value)):
            out[key] = value
        elif key in ("AT", "RESULT_AT") and value.isdigit():
            out[key] = value
    return out


def auto_line(have: str | None = None, directory: Path | None = None,
              layout: Path | None = None, root: Path | None = None) -> str:
    """What self-update will do, in plain words, for a host copy session of
    `have`; '' for the plugin or a checkout, or when there is nothing to say.
    The same text as mm_release_auto_notice in bin/mm-agent-lib.sh."""
    have = VERSION if have is None else have
    layout = layout if layout is not None else layout_dir(root)
    if layout is None or not _VER.match(have):
        return ""
    auto = read_auto(directory)
    keys, target = auto.get("KEYS", ""), auto.get("TARGET", "")
    if held(layout):
        return "self-update held on this host (mm-agent-host-install.sh --unhold lifts it)"
    if keys == "mismatch":
        return "the registrar names a release key this host doesn't trust: no self-update"
    if keys == "bad":
        return "the release keys on this host can't be used (see release-auto.log): no self-update"
    want = target_version(target)
    if want and _key(want) > _key(have):
        return f"updates itself to {target if _LATEST.match(target) else 'v' + want} when this session ends"
    return ""


def target_version(target: str) -> str:
    """The x.y.z a TARGET of release-auto.env stands for: itself, the one in
    `latest (v<x.y.z>)`, '' for `latest` while the newest tag is not known."""
    if _VER.match(target):
        return target
    m = _LATEST.match(target)
    return m[1] if m and m[1] else ""


def auto_info(have: str | None = None, directory: Path | None = None,
              layout: Path | None = None, root: Path | None = None) -> dict[str, Any]:
    """bus_info's `release.auto`: whether self-update applies here, what the
    registrar named when last read, the keys, held, the last decision."""
    have = VERSION if have is None else have
    layout = layout if layout is not None else layout_dir(root)
    if layout is None:
        return {"applies": False, "why": "only corral's host copy updates itself, between sessions"}
    auto = read_auto(directory)
    out: dict[str, Any] = {"applies": True, "held": held(layout)}
    for key, name in (("REGISTRAR", "registrar"), ("TARGET", "target"), ("KEYS", "keys"),
                      ("RESULT", "last")):
        if auto.get(key):
            out[name] = auto[key]
    if auto.get("AT"):
        out["read_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(auto["AT"])))
    text = auto_line(have, directory, layout)
    if text:
        out["will"] = text
    return out


# --- the check, as a bounded child --------------------------------------------------

# corral's temporary directories (mm_sandbox_can_write in bin/mm-agent-lib.sh).
SANDBOX_TMP = ("/tmp/corral-", "/private/tmp/corral-")
_noted: set[str] = set()


def sandbox_can_write(path: str) -> bool:
    """A path a corral sandbox can write: every corral home, ~/.claude (the
    plugin cache among it), the session's workdir, corral's temporary
    directories; one that is not absolute or has a . or .. component counts too.
    Without regard to case. The twin of mm_sandbox_can_write."""
    p = path + "/"
    if not path.startswith("/") or "/../" in p or "/./" in p:
        return True
    low = p.lower()
    if low.startswith(SANDBOX_TMP):
        return True
    home = Path.home()
    roots = [str(home / ".cache" / "corral"), str(home / ".claude"), os.environ.get("CORRAL_WORKDIR", "")]
    tmp = os.environ.get("TMPDIR", "")
    if "corral" in tmp.lower():
        roots.append(tmp)
    for r in filter(None, roots):
        for c in {r, os.path.realpath(r)}:
            if low.startswith(c.rstrip("/").lower() + "/"):
                return True
    return False


def _note_once(msg: str) -> None:
    if msg not in _noted:
        _noted.add(msg)
        log.info("release check: %s (RT-6.30)", msg)


def _note_record_once(msg: str) -> None:
    if msg not in _noted:
        _noted.add(msg)
        log.info("release record: %s (RT-6.30)", msg)


def check_script() -> tuple[Path, str] | None:
    """Which check script to run, and the CB_BUS_HOST_DIR to give it ('' keeps
    the environment's). This tree's own when no sandbox can write it (a host
    copy's release, a reviewed checkout); for a tree a sandbox can write (the
    plugin cache under ~/.claude) never that one, but the host copy's
    (CB_BUS_HOST_DIR, default ~/.local/share/mm-agent-bus/current) when it is
    there and out of every sandbox's reach; else none, said once in the log."""
    if not sandbox_can_write(str(ROOT)):
        return ROOT / SCRIPT, ""
    host = config.env("CB_BUS_HOST_DIR") or str(HOST_DIR)
    real = os.path.realpath(host)
    if os.path.isabs(host) and not sandbox_can_write(real) and (Path(real) / SCRIPT).is_file():
        return Path(real) / SCRIPT, host
    _note_once(f"not run: this server's tree {ROOT} can be changed from inside a corral sandbox, "
               f"and the host copy {host} has no check script out of every sandbox's reach")
    return None


def child_env(host_dir: str = "") -> dict[str, str]:
    """The child's whole environment: PATH (the script keeps only entries no
    sandbox writes), HOME, LC_ALL=C and CHILD_ENV; CB_BUS_HOST_DIR the host
    copy whose script runs, or for a host copy's release without it the layout
    it lies in (RT-6.28)."""
    env = {"PATH": os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
           "HOME": str(Path.home()), "LC_ALL": "C"}
    env.update({k: os.environ[k] for k in CHILD_ENV if k in os.environ})
    if host_dir:
        config.put("CB_BUS_HOST_DIR", host_dir, env)
    elif config.lookup("CB_BUS_HOST_DIR", env, quiet=True) is None and ROOT.parent.name == "releases":
        config.put("CB_BUS_HOST_DIR", str(ROOT.parent.parent / "current"), env)
    return env


# --- the release record the bridge serves (RT-6.30, plan B piece 14b) -----------------


class RecordFailed(Exception):
    """The bridge's release record could not be read or verified: why."""


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def records_configured(environ: Mapping[str, str] | None = None) -> bool:
    """CB_RECORDS_URL is set: the bridge's record is the notice's only source."""
    return bool(config.env(RECORDS_ENV, environ=environ).strip())


def records_url(environ: Mapping[str, str] | None = None) -> str | None:
    """Where the bridge serves the release record; None when the setting is
    unset, is not https (http only to a loopback host: the record carries no
    token, but a forged one must not come over a network someone can rewrite),
    or carries credentials, a query or a fragment. The setting may be the
    bridge's base address, `…/register/v1` or the registrar's `…/agents`; a
    full `…/release` is taken as it is."""
    value = config.env(RECORDS_ENV, environ=environ).strip()
    if not value or _HTTP_URL.fullmatch(value) is None:
        return None
    parts = urlsplit(value)
    if parts.scheme == "http" and not _loopback(parts.hostname or ""):
        return None
    root, path = value.rstrip("/"), parts.path.rstrip("/")
    if path.endswith("/release"):
        return root
    if path.endswith("/agents"):
        root, path = root[: -len("/agents")], path[: -len("/agents")]
    if not path.endswith("/register/v1"):
        root += "/register/v1"
    return root + "/release"


def record_state(url: str | None = None, *, environ: Mapping[str, str] | None = None,
                 now: float | None = None, transport: Any = None) -> State:
    """The release record read and verified, or RecordFailed: a 200 with a
    record, the pinned keys (`MM_REGISTRAR_KEYS`; TODO: its new `CB_` name is
    open, ADR-014 answer 4), the bridge's kid and Ed25519 signature through
    attest.verify_release, an hour's validity, and a version `x.y.z` (not
    `latest`, not keys only). The `Needs:` lines are checked again in their
    grammar. Any doubt is no state, never a partly trusted one."""
    env = os.environ if environ is None else environ
    url = records_url(env) if url is None else url
    if url is None:
        raise RecordFailed(f"{RECORDS_ENV} is not an https address (http only on loopback), "
                           "or carries credentials, a query or a fragment")
    keys = attest.pinned(env.get(attest.KEYS_ENV, ""))
    if not keys:
        raise RecordFailed(f"no pinned key ({attest.KEYS_ENV})")
    try:
        token = selfupdate.fetch(url, transport=transport, environ=env)
    except selfupdate.Failed as exc:
        raise RecordFailed(str(exc)) from None
    if token is None:
        raise RecordFailed("the bridge names no release (no release set)")
    record, why = attest.verify_release(token, keys, now=time.time() if now is None else now)
    if record is None:
        raise RecordFailed(f"refused: {why}")
    if not 0 < record.exp - record.iat <= RECORD_HOUR:
        raise RecordFailed(f"validity {record.exp - record.iat} s, not within the hour")
    if record.version == attest.RELEASE_LATEST:
        raise RecordFailed("names latest, not a version")
    if not _VER.match(record.version):
        raise RecordFailed("names no version")
    for text in record.needs:
        if not needs_ok(text):
            raise RecordFailed("a Needs: line outside its grammar")
    newest = record.version
    return State(newest=newest, verified=True, checked_at=record.iat,
                 breaking=set(record.breaking),
                 needs=[(newest, text) for text in record.needs],
                 withdrawn={v for v in record.withdrawn if _key(v) < _key(newest)},
                 origin="record", expires_at=record.exp)


# The last read: (monotonic time, state or None, why). The server's loop fills it
# (check_loop); `bus_info` and the turn-end banner only read it, so neither
# waits on the network.
_record_cache: tuple[float, State | None, str] | None = None


def clear_record_cache() -> None:
    """Forget the last record read (tests, and a setting that changed)."""
    global _record_cache
    _record_cache = None


def cached_record(now: float | None = None) -> tuple[State | None, str]:
    """The last verified record and why there is none; a record past its exp
    counts as none (its hour bounds a copy, not only the read)."""
    if _record_cache is None:
        return None, "not read yet"
    _, state, why = _record_cache
    if state is not None and state.expires_at and (time.time() if now is None else now) >= state.expires_at:
        return None, "the record's hour passed"
    return state, why


async def refresh_record(*, environ: Mapping[str, str] | None = None, now: float | None = None,
                         transport: Any = None) -> tuple[State | None, str]:
    """Read the record once and cache it; a failure is cached as none, said once
    in the log, and never falls back to release.env (the switch's promise). The
    whole read is bounded by `selfupdate.TIMEOUT`, not only each phase of it: a
    dripping answer holds no notice for longer. A record older than the one
    cached (a stale copy behind a load balancer) does not replace it: the
    newest `iat` read wins."""
    global _record_cache
    env = os.environ if environ is None else environ
    try:
        state = await asyncio.wait_for(
            asyncio.to_thread(record_state, records_url(env), environ=env,
                              now=now, transport=transport),
            selfupdate.TIMEOUT)
    except TimeoutError:
        state, why = None, f"not read within {selfupdate.TIMEOUT:g} s"
    except RecordFailed as exc:
        state, why = None, str(exc)
    except Exception as exc:  # noqa: BLE001 — an unreadable record is no notice
        state, why = None, f"not read ({type(exc).__name__})"
    else:
        why = ""
        kept, _ = cached_record()
        if (kept is not None and kept.checked_at and state is not None
                and state.checked_at < kept.checked_at):
            state = kept
    if state is None:
        _note_record_once(f"not read: {why}; no release notice")
    _record_cache = (time.monotonic(), state, why)
    return state, why


def record_notice(have: str | None = None, state: State | None = None, why: str = "") -> dict[str, Any]:
    """bus_info's `release` when the bridge's record is the source (RT-6.30,
    plan B piece 14b): this version, the record's newest and whether it is
    read, the step, `Breaking`, the `Needs:` lines, `withdrawn` and `older` as
    the host's check shows them, the line, the note that a human installs it,
    and `record` ("read", else why not). Never `check` and never a fallback."""
    have = VERSION if have is None else have
    if not _VER.match(have):
        return {}
    out: dict[str, Any] = {"this": {"version": have, "source": source_label()}}
    if state is None:
        out["record"] = f"not read: {why}" if why else "not read"
        return out
    out["record"] = "read"
    out["newest"] = state.newest
    out["signature"] = "checked"
    if state.checked_at:
        out["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.checked_at))
    if _key(state.newest) > _key(have):
        breaking, needs = _between(state, have)
        out["step"] = _step(have, state.newest)
        out["breaking"] = breaking
        if needs:
            out["needs"] = needs
    if have in state.withdrawn:
        out["withdrawn"] = True
    if older(state, have):
        out["older"] = True
    text = record_line(have, state)
    if text:
        out["notice"] = text
        out["note"] = RECORD_NOTE
    return out


def record_line(have: str | None = None, state: State | None = None) -> str:
    """The turn-end banner's line from the bridge's record (RT-6.2 step 8,
    RT-6.30); '' when there is nothing to say. No self-update line: RT-6.32
    goes in a later piece, and the plugin takes the marketplace's version."""
    have = VERSION if have is None else have
    if state is None or state.origin != "record" or not _VER.match(have):
        return ""
    return "; ".join(_release_parts(state, have))


def info(have: str | None = None) -> dict[str, Any]:
    """bus_info's `release`: the bridge's record when CB_RECORDS_URL is set,
    else the host's check exactly as before (RT-6.30)."""
    if not records_configured():
        return notice(have)
    state, why = cached_record()
    return record_notice(have, state, why)


def _failures(directory: Path, now: float) -> list[int]:
    text = _read(directory / FAILED_FILE, 4096) or ""
    return [int(x) for x in text.split() if x.isdigit() and now - int(x) < CHECK_EVERY]


def _record_failure(directory: Path, now: float) -> None:
    """One failed try more in release-check.failed (the last TRIES kept)."""
    kept = [*_failures(directory, now), int(now)][-TRIES:]
    try:
        session_file.write_private(directory / FAILED_FILE, "".join(f"{x}\n" for x in kept))
    except OSError as exc:
        log.debug("release check: %s", exc)


def _period(directory: Path) -> int:
    """How old the last success may be (RT-6.30 step 5): a day; an hour when
    this is a host copy and the newest found is older than its version (just
    installed since the check: catch up). A plugin tree or checkout ahead of
    the tags is normal and keeps the day."""
    st = read_state(directory)
    if st is None or running()[0] == "plugin" or not older(st):
        return CHECK_EVERY
    return int(POLL)


def due(directory: Path | None = None, now: float | None = None) -> bool:
    """The last check that succeeded (release-check.at) is a day old (an hour
    when the newest found is older than this version, see _period), or there
    was none, and fewer than TRIES tries failed in the last day: a failure is
    tried again at the next hourly look, at most TRIES times a day."""
    d = directory or state_dir()
    now = time.time() if now is None else now
    text = (_read(d / STAMP_FILE, 64) or "").strip()
    if now - (int(text) if text.isdigit() else 0) < _period(d):
        return False
    return len(_failures(d, now)) < TRIES


async def run_check(script: Path | None = None, *, directory: Path | None = None,
                    timeout: float = CHECK_TIMEOUT) -> int | None:
    """Run the check once as a child (ADR-011 point 3): the script of
    check_script() unless one is given, its own process group, stdin closed,
    the environment of child_env() only, from /, `--if-due` _period() (a day); after
    `timeout` seconds its group gets TERM (git removes its lock files on it),
    KILL_GRACE later KILL. A failure or a stop counts in release-check.failed;
    the script stamps a success. Its exit code; None when it did not run or
    was stopped. Never inside a corral sandbox."""
    if os.environ.get("CORRAL_SANDBOX"):
        return None
    host_dir = ""
    if script is None:
        found = check_script()
        if found is None:
            return None
        script, host_dir = found
    if not script.is_file():
        return None
    d = directory or state_dir()
    bash = "/bin/bash" if Path("/bin/bash").exists() else "bash"
    proc = await asyncio.create_subprocess_exec(
        bash, str(script), "--state-dir", str(d),
        "--if-due", str(_period(d)), env=child_env(host_dir), cwd="/",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _stop(proc)
        _record_failure(d, time.time())
        log.info("release check: no result within %.0f s, stopped (RT-6.30)", timeout)
        return None
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    text = out.decode("utf-8", "replace").strip()
    if text:
        log.info("release check: %s", text[-500:].replace("\n", " | "))
    if proc.returncode != 0:
        _record_failure(d, time.time())
    return proc.returncode


async def _stop(proc: asyncio.subprocess.Process) -> None:
    for sig, wait in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, KILL_GRACE)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


async def check_loop(first_delay: float = FIRST_DELAY) -> None:
    """Started with the server: the host's check when it is due, looked at
    hourly — or, with CB_RECORDS_URL set, the bridge's record read at once and
    every RECORD_EVERY, and the host's check never started (RT-6.30, plan B
    piece 14b: one source, the record)."""
    record = records_configured()
    await asyncio.sleep(0 if record else first_delay)
    while True:
        try:
            if record:
                await refresh_record()
            elif due():
                await run_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a notice, never in the way
            log.debug("release check: %s", exc)
        await asyncio.sleep(RECORD_EVERY if record else POLL)
