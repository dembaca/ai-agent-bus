"""NATS transport of the agent bus (ADR-014, point 16). Not wired into the server yet.

The permission model, from the host's side:

* The sender of a message is the last token of its subject
  (`<bus>.ch.<channel>.<sender>`, `<bus>.dm.<recipient>.<sender>`,
  `<presence>.<owner>.<sender>`, `<presence>.crown.<channel>.<sender>`), never a header or
  the body. No `Nats-Msg-Id` header is ever set (the stream's message-id window is
  the side channel of condition 1).
* The host never talks to JetStream. It reads only through the push consumers the
  login service makes for its agent (ack none), subscribed by their literal deliver
  subject `deliver.<name>.ch.<channel>`, `.dm` and `.presence`.
* The login service gets everything in the CONNECT `auth_token` (Keycloak access
  token, channels, `since`) and the host's nkey signature over the server's nonce.
  The credential lives 15 minutes; the server closes the connection at expiry. The
  bus reconnects itself with a fresh access token each time and sends `since`, the
  last stream sequence seen per channel.

Nothing here logs, stores or puts into an exception the access token or the seed:
the seed never leaves `login.Login.nkey()`, which only gives a `sign` function.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import random
import re
import ssl
import time
from urllib.parse import urlsplit
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from nats.aio.client import Client as NatsClient

from . import a2a
from .login import NKey

log = logging.getLogger(__name__)

# How long closing a superseded connection may take: it runs under the send
# lock, and a close that never answers must not block the next post.
CLOSE_SEC = 5.0

MAX_BODY = 65536  # the BUS stream's max_msg_size
HEARTBEAT_SEC = 30.0
PRESENCE_FRESH_SEC = 90.0  # a holder's last presence must be younger than this
OWN_ECHO_SEC = 75.0  # the echo of this agent's own presence must be younger than this (monotonic), else the view is unknown
GAP_RECONNECT_SEC = 30.0  # at most one reconnect for a gap within this time
MAX_AGE_SEC = 86400.0  # the PRESENCE stream's max_age: older claims and presence are gone
MEMBER_RENEW_SEC = 6 * 3600.0  # a membership is announced again this often (the stream keeps 24 h)
MEMBER_SYNC_WAIT_SEC = 10.0  # how long an announcement waits for the bus to be synced
PRESENCE_FRESH_KEY = "@presence"  # key of the hello's `since`: this session has not read the presence stream
RENEW_SEC = 14 * 60.0  # the login service's credential lives 15 minutes: renew a minute before
SEND_WAIT_SEC = 10.0  # a send waits this long for a planned reconnect
BUS_STREAM = "BUS"
PRESENCE_STREAM = "PRESENCE"
DELIVER = "deliver"  # consumers.DeliverPrefix / callout.DeliverPrefix

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # Go checkToken
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # Go consumers.agentRE
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# An owner counts only in this form (RT-6.37): a Mattermost username's characters in lower
# case, the same string the bus compares with MM_TRUSTED_HUMANS and the owners of Mattermost
# bots. Nothing is normalised: an owner in any other form is no owner.
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
NO_OWNER = "-"  # the owner token of an owner not in OWNER_RE's form
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")  # a human's name as the login service reads it
MAX_TRUSTED = 32  # the hello's `trusted` and an attestation's trusted list: at most this many


def owner_token(owner: str) -> str:
    """The owner as the token of a presence subject (`<presence>.<owner>.<name>`, RT-6.37),
    as Go's ownerToken (services/auth/internal/callout/perms.go) makes it: "." as "~" (a
    subject token holds no "."; no username holds "~"), NO_OWNER for any other form."""
    return owner.replace(".", "~") if OWNER_RE.fullmatch(owner) else NO_OWNER


def owner_of_token(token: str) -> str:
    """The owner a presence subject's owner token names, '' for none."""
    owner = token.replace("~", ".")
    return owner if token != NO_OWNER and "." not in token and OWNER_RE.fullmatch(owner) else ""

_PUBLISH_DENIED_RE = re.compile(r'permissions violation for publish to "([^"]*)"', re.IGNORECASE)
_KINDS = ("post", "reaction", "unreaction")
_STATUSES = ("online", "away", "bye")


class NatsBusError(Exception):
    """A bus operation failed. The text never holds a token or a seed."""


class NameMismatch(NatsBusError):
    """The server refused the host's subscription to its own deliver subjects:
    the name the login service wrote into the credential is not the one the
    host computed. The host sends nothing under the computed name."""


class MessageError(ValueError):
    """A message body is not format v1. The text never holds content."""


# --- the agent's name --------------------------------------------------------

def agent_name(owner: str, host_nkey_public: str, prefix: str = "agent-") -> str:
    """Same as Go `name()` (services/auth/internal/callout/service.go) plus the
    prefix: SHA-256 over owner, a NUL byte and the host nkey, the first 16 bytes
    as lower-case base32 without padding (26 characters). The owner is used as
    the login service gets it (the token's `preferred_username`), not changed."""
    digest = hashlib.sha256(owner.encode() + b"\x00" + host_nkey_public.encode()).digest()[:16]
    return prefix + base64.b32encode(digest).decode().lower().rstrip("=")


# --- message format v1 -----------------------------------------------------------

Prop = str | int | float | bool


@dataclass(frozen=True)
class Message:
    """One bus message. The sender is not part of it: it is the subject's last token."""

    kind: str  # "post" | "reaction" | "unreaction"
    text: str = ""
    root: int | None = None  # post: stream sequence of the thread's root post
    ref: int | None = None  # reaction: stream sequence of the post
    emoji: str | None = None
    props: dict[str, Prop] = field(default_factory=dict)


def _seq(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _wellformed(s: str) -> bool:
    """False for a string with a lone surrogate: it has no UTF-8 form."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check(m: Message, props_prefix: str = a2a.DEFAULT_PREFIX) -> None:
    if m.kind not in _KINDS:
        raise MessageError("unknown kind")
    if not isinstance(m.text, str):
        raise MessageError("text is not a string")
    if m.root is not None and not _seq(m.root):
        raise MessageError("root is not a sequence")
    if not _wellformed(m.text):
        raise MessageError("text is not valid Unicode")
    for k, v in m.props.items():
        if not isinstance(k, str) or not isinstance(v, str | int | float | bool):
            raise MessageError("props hold only strings and simple values")
        if not k.startswith(props_prefix):
            raise MessageError("props keys start with the bus's props prefix (MM_PROPS_PREFIX)")
        if not _wellformed(k) or (isinstance(v, str) and not _wellformed(v)):
            raise MessageError("props are not valid Unicode")
    if m.kind == "post":
        if m.ref is not None or m.emoji is not None:
            raise MessageError("a post has no ref or emoji")
    else:
        if not _seq(m.ref):
            raise MessageError("a reaction needs ref")
        if not isinstance(m.emoji, str) or not m.emoji:
            raise MessageError("a reaction needs an emoji")
        if len(m.emoji) > 64 or m.emoji.splitlines() != [m.emoji]:
            raise MessageError("an emoji is one line of at most 64 characters")
        if not _wellformed(m.emoji):
            raise MessageError("emoji is not valid Unicode")
        if m.text != "" or m.root is not None:
            raise MessageError("a reaction has no text or root")


def _dumps(obj: object, ensure_ascii: bool = True) -> str:
    """JSON for the wire: never NaN or Infinity (not JSON, other readers refuse them)."""
    try:
        return json.dumps(obj, ensure_ascii=ensure_ascii, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise MessageError("a number is not representable in JSON") from None


def encode(m: Message, props_prefix: str = a2a.DEFAULT_PREFIX) -> bytes:
    _check(m, props_prefix)
    body = _dumps({"v": 1, "kind": m.kind, "text": m.text, "root": m.root, "ref": m.ref,
                   "emoji": m.emoji, "props": m.props}, ensure_ascii=False).encode()
    if len(body) > MAX_BODY:
        raise MessageError(f"message is {len(body)} bytes, more than {MAX_BODY}")
    return body


def _constant(name: str) -> Any:
    raise MessageError("NaN and Infinity are not JSON")


def _float(text: str) -> float:
    f = float(text)
    if not math.isfinite(f):
        raise MessageError("a number is out of range")
    return f


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise MessageError("duplicate key")
        out[k] = v
    return out


def _obj(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BODY:
        raise MessageError("body too big")
    try:
        text = data.decode("utf-8")  # strict: no replacement characters, no other encoding
    except UnicodeDecodeError:
        raise MessageError("body is not UTF-8") from None
    try:
        obj = json.loads(text, parse_constant=_constant, parse_float=_float, object_pairs_hook=_pairs)
    except MessageError:
        raise
    except (ValueError, RecursionError):
        raise MessageError("body is not JSON") from None
    if not isinstance(obj, dict):
        raise MessageError("body is not an object")
    v = obj.get("v")
    if isinstance(v, bool) or v != 1 or not isinstance(v, int):
        raise MessageError("unknown version")
    return obj


def decode(data: bytes, props_prefix: str = a2a.DEFAULT_PREFIX) -> Message:
    """Strict: anything that is not a well-formed v1 message raises MessageError.
    Unknown keys (a forged `sender`, say) are ignored, never read."""
    obj = _obj(data)
    props = obj.get("props", {})
    if not isinstance(props, dict):
        raise MessageError("props is not an object")
    m = Message(kind=obj.get("kind"), text=obj.get("text", ""), root=obj.get("root"),  # type: ignore[arg-type]
                ref=obj.get("ref"), emoji=obj.get("emoji"), props=props)
    _check(m, props_prefix)
    return m


BOT_RE = re.compile(r"[a-z0-9]{26}")  # a Mattermost user id


def encode_presence(status: str, bot: str = "") -> bytes:
    """The presence body; `bot` is the agent's Mattermost bot id (RT-6.37), left out when ''."""
    if status not in _STATUSES:
        raise MessageError("unknown status")
    body: dict[str, Any] = {"v": 1, "status": status}
    if bot:
        body["bot"] = bot
    return _dumps(body).encode()


def decode_presence(data: bytes) -> str:
    status = _obj(data).get("status")
    if status not in _STATUSES:
        raise MessageError("unknown status")
    return str(status)


def decode_presence_bot(data: bytes) -> str:
    """The bot id of a presence body when it is a Mattermost user id, else ''. A bad
    bot field never drops the message."""
    try:
        bot = _obj(data).get("bot")
    except MessageError:
        return ""
    return bot if isinstance(bot, str) and BOT_RE.fullmatch(bot) else ""


def link_tag(name: str) -> str:
    """The 16 characters of the hash part of an agent name that the roster status shows
    (RT-6.37); '' for a name shorter than the hash part (26 characters)."""
    return name[-26:][:16] if len(name) >= 26 else ""


@dataclass(frozen=True)
class Attestation:
    """The login service's record of an agent's circle, as it wrote it at the agent's
    credential (`<presence>.attest.<agent>.v1`): anchors are what the service pinned
    (stable), names what they were called at that time. `exp` is server time."""

    owner_anchor: str
    owner_name: str
    trusted: tuple[tuple[str, str], ...]  # (anchor, name)
    cut: bool
    exp: float


def _person(obj: Any, what: str) -> tuple[str, str]:
    if not isinstance(obj, dict):
        raise MessageError(f"{what} is not an object")
    anchor, name = obj.get("anchor"), obj.get("name")
    if not isinstance(anchor, str) or not OWNER_RE.fullmatch(anchor):
        raise MessageError(f"{what} has a bad anchor")
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise MessageError(f"{what} has a bad name")
    return anchor, name


def _int(obj: dict[str, Any], key: str) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise MessageError(f"{key} is not an integer")
    return v


def decode_attestation(data: bytes, agent: str) -> Attestation:
    """Strict: anything that is not a well-formed v1 attestation of `agent` raises MessageError."""
    obj = _obj(data)
    if obj.get("agent") != agent:
        raise MessageError("attestation of another agent")
    owner = _person(obj.get("owner"), "owner")
    trusted = obj.get("trusted")
    if not isinstance(trusted, list) or len(trusted) > MAX_TRUSTED:
        raise MessageError("trusted is not a list of at most 32")
    cut = obj.get("cut")
    if not isinstance(cut, bool):
        raise MessageError("cut is not a boolean")
    _int(obj, "iat")
    return Attestation(owner_anchor=owner[0], owner_name=owner[1],
                       trusted=tuple(_person(t, "a trusted entry") for t in trusted),
                       cut=cut, exp=float(_int(obj, "exp")))


def normalise_trusted(names: Iterable[str]) -> tuple[tuple[str, ...], bool]:
    """The hello's trusted names and whether the list was cut: a leading "@" dropped, lower
    case, only valid usernames, each once, at most MAX_TRUSTED. cut: an invalid name was
    dropped or the list truncated (a repeat or a blank is no loss)."""
    out: dict[str, None] = {}
    cut = False
    for n in names:
        n = n.strip().lstrip("@").lower()
        if USERNAME_RE.fullmatch(n):
            out.setdefault(n)
        elif n:
            cut = True
    return tuple(out)[:MAX_TRUSTED], cut or len(out) > MAX_TRUSTED


def encode_claim() -> bytes:
    return _dumps({"v": 1}).encode()


def decode_claim(data: bytes) -> None:
    _obj(data)


# --- JetStream metadata and gaps -----------------------------------------------------

@dataclass(frozen=True)
class Ack:
    stream_seq: int
    consumer_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    pending: int = -1  # messages the consumer still has to deliver; -1: not told


def parse_ack(reply: str | None, stream: str | None = None, consumer: str | None = None) -> Ack | None:
    """`$JS.ACK.<stream>.<consumer>.<delivered>.<sseq>.<cseq>.<ts ns>.<pending>`, or with
    domain and account hash in front (and a token behind). None: not JetStream's, or,
    when `stream` and `consumer` are given, not the reply of that consumer."""
    if not reply:
        return None
    t = reply.split(".")
    if t[:2] != ["$JS", "ACK"] or len(t) not in (9, 11, 12):
        return None
    i = 4 if len(t) == 9 else 6
    if (stream is not None and t[i - 2] != stream) or (consumer is not None and t[i - 1] != consumer):
        return None
    try:
        sseq, cseq, ns, pending = int(t[i + 1]), int(t[i + 2]), int(t[i + 3]), int(t[i + 4])
    except (ValueError, IndexError):
        return None
    if sseq < 1 or cseq < 1 or pending < 0:
        return None
    return Ack(sseq, cseq, ns / 1e9, pending)


class GapTracker:
    """The last consumer sequence per deliver subject. A push consumer counts every
    delivery, so a jump of more than one is a message this connection lost."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def observe(self, key: str, consumer_seq: int) -> bool:
        """Record consumer_seq; True when it is more than one past the last."""
        last = self._last.get(key)
        self._last[key] = consumer_seq
        return last is not None and consumer_seq > last + 1

    def reset(self) -> None:
        self._last.clear()


# --- the crown ---------------------------------------------------------------------------

def crown_candidates(claims: Mapping[str, int], presence: Mapping[str, tuple[float, str]],
                     now: float, is_member: Callable[[str], bool] | None = None,
                     fresh: float = PRESENCE_FRESH_SEC,
                     at: Mapping[str, float] | None = None) -> list[str]:
    """The agents that may hold the crown of one channel (RT-6.38), by claim order: a claim,
    a last presence message younger than `fresh` seconds and not `bye`, and, when
    `is_member` is given, a membership of the channel. The order is (server time of the
    claim, stream sequence), lexicographically: the sequence alone is no order between
    agents when the stream was made anew (`at` None: by sequence only).
    claims: agent -> stream sequence of its (latest) claim; at: agent -> server time of it;
    presence: agent -> (server time, status) of its last presence message."""
    out: list[tuple[float, int, str]] = []
    for agent, seq in claims.items():
        seen = presence.get(agent)
        if seen is None or seen[1] in ("bye", "offline") or now - seen[0] >= fresh:
            continue
        if is_member is not None and not is_member(agent):
            continue
        out.append((at.get(agent, 0.0) if at is not None else 0.0, seq, agent))
    return [agent for _, _, agent in sorted(out)]


def crown_holder(claims: Mapping[str, int], presence: Mapping[str, tuple[float, str]],
                 now: float, fresh: float = PRESENCE_FRESH_SEC,
                 at: Mapping[str, float] | None = None) -> str | None:
    """The crown of one channel: the first claim in crown_candidates' order among the
    agents whose last presence message is younger than `fresh` seconds and not `bye`.
    claims: agent -> stream sequence of its (latest) claim; presence: agent ->
    (server time, status) of its last presence message."""
    first = crown_candidates(claims, presence, now, None, fresh, at)
    return first[0] if first else None


# --- CONNECT ---------------------------------------------------------------------------------

def build_hello(token: str, home: str, guests: tuple[str, ...], lobby: bool,
                since: Mapping[str, int], *, fresh_presence: bool = False,
                trusted: tuple[str, ...] = (), trusted_cut: bool = False) -> str:
    """The CONNECT `auth_token` the login service reads (callout.Hello). It holds the
    access token: never log it."""
    hello: dict[str, Any] = {"token": token}
    if home:
        hello["home"] = home
    if guests:
        hello["guests"] = list(guests)
    if lobby:
        hello["lobby"] = True
    if trusted:
        hello["trusted"] = list(trusted)
    if trusted_cut:
        hello["trusted_cut"] = True
    known = {ch: seq for ch, seq in since.items() if seq > 0}
    if fresh_presence:
        known[PRESENCE_FRESH_KEY] = 1  # the login service reads the presence stream afresh (RT-6.35)
    if known:
        hello["since"] = known
    return json.dumps(hello, separators=(",", ":"))


# --- the bus ---------------------------------------------------------------------------------------

def _check_server(url: str) -> None:
    """tls:// anywhere; nats:// (no TLS) only to this machine."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "tls" and host:
        return
    if parts.scheme == "nats" and host:
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise ValueError("a NATS server URL is tls://, or nats:// to a loopback host only")


def _tls_mode(servers: tuple[str, ...]) -> bool:
    """True when the servers are tls://. nats-py takes one TLS mode for the whole pool, so
    tls:// and nats:// servers are not mixed."""
    tls = {urlsplit(u).scheme == "tls" for u in servers}
    if len(tls) > 1:
        raise ValueError("NATS server URLs are all tls:// or all nats://, not mixed")
    return tls == {True}


@dataclass(frozen=True)
class NatsSettings:
    servers: tuple[str, ...]
    home: str
    guests: tuple[str, ...] = ()
    lobby: bool = False
    lobby_channel: str = "lobby"  # the login service's AUTH_LOBBY: its name, to subscribe literally
    bus: str = "bus"
    presence: str = "presence"
    name_prefix: str = "agent-"
    props_prefix: str = a2a.DEFAULT_PREFIX  # MM_PROPS_PREFIX, one setting of the whole bus (RT-6.16)
    tls_ca_file: str | None = None  # CA bundle that verifies a tls:// server (default: the system's)
    trusted: tuple[str, ...] = ()  # this session's trusted humans' names, for the hello (normalised)
    trusted_cut: bool = False  # normalising dropped or truncated names (set by __post_init__)

    def __post_init__(self) -> None:
        names, cut = normalise_trusted(self.trusted)
        object.__setattr__(self, "trusted", names)
        object.__setattr__(self, "trusted_cut", cut or self.trusted_cut)
        # Store what the bus's own check returns, not what was passed: an empty
        # value means "unset" there and comes back as the default, and keeping
        # the empty string would make `k.startswith("")` accept every key,
        # `from_trusted_human` included. Raises ValueError for a bad one.
        object.__setattr__(self, "props_prefix",
                           a2a.user_prefix({a2a.PREFIX_ENV: self.props_prefix}))
        for url in self.servers:
            _check_server(url)  # the access token goes over this connection
        _tls_mode(self.servers)

    def tls_context(self) -> ssl.SSLContext | None:
        """For tls:// servers: a context that verifies the certificate and the host name."""
        if not _tls_mode(self.servers):
            return None
        return ssl.create_default_context(cafile=self.tls_ca_file)


@dataclass(frozen=True)
class Event:
    where: str  # a channel name, or "@dm" for a direct message (no channel name can look like it)
    sender: str  # the subject's last token
    stream_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    message: Message


class LoginLike(Protocol):
    async def access_token(self) -> str: ...
    def nkey(self) -> NKey: ...


EventCallback = Callable[[Event], Awaitable[None] | None]
FatalCallback = Callable[[NatsBusError], Awaitable[None] | None]
DM_SINCE = "@dm"  # the key of `since` for direct messages (no channel name can look like it)


class NatsBus:
    def __init__(self, settings: NatsSettings, login: LoginLike, owner: str, on_event: EventCallback,
                 *, heartbeat_sec: float = HEARTBEAT_SEC, gap_reconnect_sec: float = GAP_RECONNECT_SEC,
                 connect_timeout: float = 5.0, renew_sec: float = RENEW_SEC,
                 send_wait_sec: float = SEND_WAIT_SEC, member_renew_sec: float = MEMBER_RENEW_SEC,
                 on_fatal: FatalCallback | None = None) -> None:
        for what, v in (("bus", settings.bus), ("presence", settings.presence)):
            if not PREFIX_RE.fullmatch(v) or v == DELIVER:
                raise ValueError(f"invalid {what} prefix")
        for url in settings.servers:
            _check_server(url)
        self._s = settings
        self._login = login
        self._owner = owner
        self._on_event = on_event
        self._on_fatal = on_fatal
        self._heartbeat_sec = heartbeat_sec
        self._gap_sec = gap_reconnect_sec
        self._connect_timeout = connect_timeout
        self._renew_sec = renew_sec
        self._send_wait = send_wait_sec
        self._member_renew_sec = member_renew_sec
        self._name: str | None = None
        self._nc: NatsClient | None = None
        self._connected = False
        self._stopping = False
        self._fatal: NatsBusError | None = None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()  # cleared during a planned reconnect: sends wait for it
        self._ready.set()
        self._planned = False
        self._tasks: list[asyncio.Task[None]] = []
        self._renew_task: asyncio.Task[None] | None = None
        self._gap_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._denied: list[str] = []  # subjects the server refused a publish on
        self._gaps = GapTracker()
        self._last_gap_reconnect = -1e18
        self._last_echo_reconnect = -1e18
        self._echo_backoff = 0.0  # the wait after an echo reconnect that brought no echo back; 0: none yet
        self._stale: set[str] = set()  # channels with a gap, dropped until the reconnect
        self._last_seq: dict[str, int] = {}  # channel (or "@dm") -> last stream sequence delivered
        self._server_now = 0.0  # the newest server timestamp seen: the host's "now"
        self._last_prune = 0.0
        self._presence: dict[str, tuple[float, str]] = {}
        self._owners: dict[str, str] = {}  # agent -> owner its presence subject names (RT-6.37)
        self._presence_seq: dict[str, tuple[int, float]] = {}  # agent -> (stream sequence, server time)
        self._bots: dict[str, str] = {}  # agent -> Mattermost bot its presence names (RT-6.37)
        # agent -> (stream sequence, server time, record): the newest attestation of the login service
        self._attest: dict[str, tuple[int, float, Attestation]] = {}
        self._bot = ""
        self._synced = False
        self._presence_read = False  # True once the stored presence stream was read (a new session says so in its hello)
        self._memberships: list[str] = []  # channels this bus announces its membership of
        self._member_task: asyncio.Task[None] | None = None
        self._member_now = asyncio.Event()  # set at each (re)connect: announce again
        self._members: dict[str, dict[str, tuple[int, float]]] = {}  # channel -> agent -> (stream sequence, server time)
        self._claims: dict[str, dict[str, int]] = {}
        self._claim_at: dict[tuple[str, str], float] = {}  # (channel, agent) -> server time of the claim
        self._presence_cseq = 0  # the presence consumer's last delivery number: a lower one is a consumer made anew
        self._own_echo = time.monotonic()  # when this agent's own presence last came back (monotonic, never server time)

    def __repr__(self) -> str:
        return f"NatsBus(name={self._name!r}, connected={self._connected})"

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = agent_name(self._owner, self._login.nkey().public_key, self._s.name_prefix)
        return self._name

    @property
    def connected(self) -> bool:
        """Whether a connection is open now (False while a reconnect is under way)."""
        return self._connected and self._nc is not None

    @property
    def fatal(self) -> NatsBusError | None:
        """The error that stopped the bus for good (a NameMismatch), else None."""
        return self._fatal

    @property
    def channels(self) -> list[str]:
        out: list[str] = []
        wanted = [self._s.home, *self._s.guests]
        if self._s.lobby:
            wanted.append(self._s.lobby_channel)
        for ch in wanted:
            if ch and ch not in out:
                out.append(ch)
        return out

    def since(self) -> dict[str, int]:
        out = {ch: self._last_seq[ch] for ch in self.channels if self._last_seq.get(ch)}
        if self._last_seq.get(DM_SINCE):
            out[DM_SINCE] = self._last_seq[DM_SINCE]
        return out

    # --- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels:
            if not CHANNEL_RE.fullmatch(ch):
                raise ValueError("invalid channel name")
        _ = self.name
        retry = False
        try:
            await self._open()
        except (NameMismatch, ValueError):
            raise  # final: another credential, or settings that are wrong
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A first connect that fails is no end: the supervisor connects again with backoff
            # (a send raises "not connected" until it has).
            log.warning("nats bus: first connect failed, retrying: %s", type(exc).__name__)
            retry = True
        self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._heartbeat())]
        if self._memberships:
            self._start_member_task()
        if retry:
            self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._ready.set()
        self._cancel_timers()
        if self._member_task is not None:
            self._member_task.cancel()
            self._tasks.append(self._member_task)
            self._member_task = None
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        nc = self._nc
        if nc is not None and self._connected:
            try:
                await nc.publish(self._presence_subject, encode_presence("bye", self._bot))
                await asyncio.wait_for(nc.drain(), 5)
            except Exception as exc:  # the bus is going away anyway
                log.warning("nats bus: stop: %s", type(exc).__name__)
        if nc is not None:
            try:
                await nc.close()
            except Exception:
                pass
        self._connected = False
        self._nc = None

    def _cancel_timers(self) -> None:
        for t in (self._renew_task, self._gap_task):
            if t is not None and t is not asyncio.current_task():
                t.cancel()
        self._renew_task = self._gap_task = None

    # --- connection ----------------------------------------------------------------
    async def _open(self) -> None:
        """One connection with a fresh access token, `since` and the literal subscriptions."""
        self._synced = False  # the stored presences are read again from this connection
        self._own_echo = time.monotonic()  # a new connection gets the window to see its own presence
        token = await self._login.access_token()
        key = self._login.nkey()
        hello = build_hello(token, self._s.home, self._s.guests, self._s.lobby, self.since(),
                            fresh_presence=not self._presence_read, trusted=self._s.trusted, trusted_cut=self._s.trusted_cut)
        del token
        nc = NatsClient()
        # nats-py has no argument for the public key with a signature callback; this is the
        # attribute its own seed path sets. The seed itself stays in login.
        nc._public_nkey = key.public_key

        def sign(nonce: str) -> bytes:
            return base64.b64encode(key.sign(nonce.encode()))

        async def lost() -> None:
            if self._nc is nc and not self._stopping:
                self._connected = False
                self._wake.set()

        refused: list[str] = []

        async def error(exc: Exception) -> None:
            text = str(exc)
            if "permissions violation" in text.lower():
                refused.append("permissions violation")
                m = _PUBLISH_DENIED_RE.search(text)
                if m:
                    self._denied = [*self._denied[-63:], m.group(1)]
            log.warning("nats bus: %s", type(exc).__name__)

        # tls://: the TLS handshake comes first, before any NATS protocol, with the certificate
        # verified. nats-py otherwise upgrades only when the server's INFO says tls_required,
        # which a man in the middle simply leaves out (the CONNECT with the token would go in clear).
        tls = self._s.tls_context()
        tls_kw: dict[str, Any] = {"tls": tls, "tls_handshake_first": True} if tls is not None else {}
        try:
            # nats-py bounds the TCP connect and the INFO, not a TLS handshake that never ends
            # (a plaintext peer behind tls:// leaves the client waiting for a TLS record).
            await asyncio.wait_for(
                nc.connect(list(self._s.servers), token=hello, signature_cb=sign, allow_reconnect=False,
                           connect_timeout=int(self._connect_timeout), error_cb=error,
                           disconnected_cb=lost, closed_cb=lost, **tls_kw),
                self._connect_timeout * 2)
        except BaseException:  # also a cancellation: no half-open client is left behind
            try:  # bounded: a TLS peer that never answers the shutdown must not hold this up
                await asyncio.wait_for(asyncio.shield(nc.close()), 2)
            except Exception:
                pass
            raise
        del hello
        self._nc = nc
        self._gaps.reset()
        self._stale.clear()
        self._cancel_gap_timer()  # this connection starts from `since`: the held-back gap is covered
        name = self.name
        try:
            for ch in self.channels:
                await nc.subscribe(f"{DELIVER}.{name}.ch.{ch}", cb=self._handler(ch))
            await nc.subscribe(f"{DELIVER}.{name}.dm", cb=self._handler(DM_SINCE))
            await nc.subscribe(f"{DELIVER}.{name}.presence", cb=self._on_presence_msg)
            # The server answers a refused SUB with -ERR before the PONG of a flush: a
            # refusal of the own deliver subjects means the credential carries another name
            # than the computed one (the credential is the authority). Nothing has been
            # published yet.
            await self._sync(nc)
            if refused:
                raise NameMismatch("the credential's agent name is not the computed one; nothing sent")
            await nc.publish(self._presence_subject, encode_presence("online", self._bot))
            await self._sync(nc)
            if self._presence_subject in self._denied:
                # A host and a login service of different releases (RT-6.37): the presence
                # subject changed, so this agent looks offline and foreign, and its bye
                # removes nothing. Said once, in words, not only as an exception's type.
                log.warning("nats bus: the login service refused this agent's presence subject "
                            "(<presence>.<owner>.<name>): update the login service and the hosts "
                            "together; until then this agent looks offline and foreign to the others")
        except BaseException:
            await nc.close()
            self._nc = None
            raise
        self._connected = True
        self._member_now.set()  # announce the memberships again on this connection
        if nc.is_closed:  # lost while subscribing
            self._connected = False
            self._wake.set()
        else:
            self._schedule_renew()

    @staticmethod
    async def _sync(nc: NatsClient) -> None:
        """Two round trips, so that every -ERR the server sent for what came before has
        been handled when this returns. One flush is not enough: its PING is queued behind
        the client's own buffered writes, and the PONG that resolves it may answer a PING
        that was already on the wire before them (nats-py matches PONGs to flushes in
        order), so an -ERR for the writes may still be on its way. The second PING is
        sent only after the first PONG, and the server answers in order."""
        await nc.flush()
        await nc.flush()

    def _schedule_renew(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
        self._renew_task = asyncio.create_task(self._renew_after())

    async def _renew_after(self) -> None:
        await asyncio.sleep(self._renew_sec)
        if not self._stopping:
            log.info("nats bus: renewing the connection before its credential expires")
            self._planned = True
            self._wake.set()

    def _cancel_gap_timer(self) -> None:
        if self._gap_task is not None and self._gap_task is not asyncio.current_task():
            self._gap_task.cancel()
        self._gap_task = None

    async def _supervise(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            planned = self._planned
            if planned:
                self._planned = False
                self._ready.clear()  # a send now waits for the new connection
            if planned:
                # A send under way is not cut off: the old connection closes when it is done.
                async with self._send_lock:
                    await self._close_old()
            else:
                await self._close_old()
            delay = 1.0
            try:
                while not self._stopping:
                    try:
                        await self._open()
                        log.info("nats bus: reconnected")
                        break
                    except asyncio.CancelledError:
                        raise
                    except NameMismatch as exc:
                        log.error("nats bus: the credential's agent name is not the computed one; stopped")
                        self._fatal = exc
                        self._stopping = True
                        self._cancel_timers()
                        await self._notify_fatal(exc)
                        return
                    except Exception as exc:
                        log.warning("nats bus: reconnect failed: %s", type(exc).__name__)
                        await asyncio.sleep(delay * random.uniform(0.5, 1.0))
                        delay = min(delay * 2, 30.0)
            finally:
                self._ready.set()

    async def _close_old(self) -> None:
        old, self._nc, self._connected = self._nc, None, False
        if old is not None:
            try:
                # Bounded: this runs under _send_lock, so a close that never
                # returns would block every post that follows.
                await asyncio.wait_for(old.close(), timeout=CLOSE_SEC)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _notify_fatal(self, exc: NatsBusError) -> None:
        if self._on_fatal is None:
            return
        try:
            res = self._on_fatal(exc)
            if inspect.isawaitable(res):
                await res
        except Exception as err:
            log.warning("nats bus: on_fatal callback failed: %s", type(err).__name__)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            nc = self._nc
            if nc is not None and self._connected:
                try:
                    await nc.publish(self._presence_subject, encode_presence("online", self._bot))
                except Exception as exc:
                    log.warning("nats bus: heartbeat: %s", type(exc).__name__)
            self._check_own_echo()

    def _check_own_echo(self) -> None:
        """The echo of the own presence missing past OWN_ECHO_SEC while connected (a stream
        made anew, the consumer gone): a reconnect makes the consumer again (RT-6.38 step 2).
        None when the server refused the presence subject (host and login service of
        different releases: a reconnect cures nothing). A reconnect that brings no echo back
        doubles the wait before the next, from OWN_ECHO_SEC up to RENEW_SEC; a real echo
        resets it. So an outage a reconnect does not cure is no reconnect storm."""
        if self._stopping or not self._connected or self.own_echo_age() <= OWN_ECHO_SEC:
            return
        if self._presence_subject in self._denied:
            return
        now = time.monotonic()
        if now - self._last_echo_reconnect < max(self._gap_sec, self._echo_backoff):
            return
        log.warning("nats bus: no echo of the own presence for %.0f s, reconnecting", self.own_echo_age())
        self._echo_backoff = min(max(self._echo_backoff * 2, OWN_ECHO_SEC), RENEW_SEC)
        self._last_echo_reconnect = now
        self._wake.set()

    # --- sending ---------------------------------------------------------------------
    async def _publish(self, subject: str, body: bytes) -> None:
        """Publish and wait until the server has handled it: a publish the server refuses
        (no right on the subject) is an error here, not a silent success."""
        if self._fatal is not None:
            raise self._fatal
        waited = False
        while True:
            if (self._nc is None or not self._connected) and not self._ready.is_set():
                try:  # a planned reconnect is under way
                    await asyncio.wait_for(self._ready.wait(), self._send_wait)
                except TimeoutError:
                    pass
                waited = True
                if self._fatal is not None:
                    raise self._fatal
            async with self._send_lock:
                nc = self._nc  # read under the lock: a planned renewal swaps it holding the lock
                if nc is None or not self._connected:
                    if not waited and not self._ready.is_set():
                        continue  # the renewal took the lock first: wait for it, outside the lock
                    raise NatsBusError("not connected")
                await self._publish_locked(nc, subject, body)
                return

    async def _publish_locked(self, nc: NatsClient, subject: str, body: bytes) -> None:
        self._denied = [d for d in self._denied if d != subject]
        try:
            await nc.publish(subject, body)  # never a Nats-Msg-Id header
            await self._sync(nc)
        except Exception as exc:
            raise NatsBusError(f"publish failed: {type(exc).__name__}") from None
        if subject in self._denied:
            self._denied = [d for d in self._denied if d != subject]
            raise NatsBusError("publish refused by the server: no permission for it")

    @staticmethod
    def _channel(ch: str) -> str:
        if not CHANNEL_RE.fullmatch(ch):
            raise ValueError("invalid channel name")
        return ch

    def _asked(self, ch: str) -> str:
        """A channel this bus asked the login service for (home, guests, the lobby)."""
        self._channel(ch)
        if ch not in self.channels:
            raise NatsBusError("not a channel this bus asked for; nothing sent")
        return ch

    async def post(self, channel: str, text: str, root: int | None = None,
                   props: dict[str, Prop] | None = None) -> None:
        body = encode(Message("post", text, root=root, props=props or {}), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    async def react(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("reaction", ref=ref, emoji=emoji), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    async def unreact(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("unreaction", ref=ref, emoji=emoji), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    def _dm_subject(self, recipient: str) -> str:
        if not AGENT_RE.fullmatch(recipient):
            raise ValueError("invalid recipient name")
        return f"{self._s.bus}.dm.{recipient}.{self.name}"

    async def dm(self, recipient: str, text: str, root: int | None = None,
                 props: dict[str, Prop] | None = None) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("post", text, root=root, props=props or {}),
                                            self._s.props_prefix))

    async def dm_react(self, recipient: str, ref: int, emoji: str, remove: bool = False) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("unreaction" if remove else "reaction",
                                                    ref=ref, emoji=emoji), self._s.props_prefix))

    async def claim(self, channel: str) -> None:
        await self._publish(f"{self._s.presence}.crown.{self._asked(channel)}.{self.name}", encode_claim())

    async def member(self, channel: str) -> None:
        """Announce this agent's membership of a channel this bus asked for, on
        `<presence>.member.<channel>.<name>` (RT-6.37): once the bus is synced, again every
        MEMBER_RENEW_SEC and after every (re)connect. The crown claim is another subject."""
        ch = self._asked(channel)
        if ch not in self._memberships:
            self._memberships.append(ch)
        self._member_now.set()
        if self._tasks:
            self._start_member_task()

    def _start_member_task(self) -> None:
        if self._member_task is None or self._member_task.done():
            self._member_task = asyncio.create_task(self._announce_members())

    async def _announce_members(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._member_now.wait(), self._member_renew_sec)
            except asyncio.TimeoutError:
                pass
            self._member_now.clear()
            if self._stopping or not self.connected:
                continue  # the next (re)connect announces
            for _ in range(int(MEMBER_SYNC_WAIT_SEC / 0.05)):
                if self._synced or not self.connected:
                    break
                await asyncio.sleep(0.05)
            else:
                log.warning("nats bus: the presence stream was not read within %.0f s: announcing memberships anyway",
                            MEMBER_SYNC_WAIT_SEC)
            await self._announce_once()

    async def _announce_once(self) -> None:
        """Announce each membership on its own: a channel the login service did not grant
        (a guest the human is not in) is refused, and the others go out all the same."""
        for ch in list(self._memberships):
            try:
                await self._publish(f"{self._s.presence}.member.{ch}.{self.name}", encode_claim())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the next renewal tries again
                log.warning("nats bus: announcing the membership of ~%s failed: %s", ch, type(exc).__name__)

    @property
    def _presence_subject(self) -> str:
        """`<presence>.<owner>.<name>` (RT-6.37): the login service lets this agent publish
        its presence there and nowhere else, so the owner in it is pinned like the name."""
        return f"{self._s.presence}.{owner_token(self._owner)}.{self.name}"

    def link_bot(self, bot_id: str) -> None:
        """The Mattermost bot this agent's presence names (RT-6.37); before start."""
        if not BOT_RE.fullmatch(bot_id):
            raise ValueError("not a Mattermost user id")
        self._bot = bot_id

    def bot_of(self, agent: str) -> str:
        """The bot the agent's presence names, '' for none."""
        return self._bots.get(agent, "")

    @property
    def synced(self) -> bool:
        """True once a presence delivery came with nothing pending: the stored presences
        and claims are read; False again at each (re)connect."""
        return self._synced

    def claimed(self, channel: str, agent: str) -> bool:
        """Whether the agent holds a crown claim in the channel (not its membership)."""
        return agent in self._claims.get(channel, {})

    def is_member(self, channel: str, agent: str) -> bool:
        """Whether the agent announced its membership of the channel within the last 24 h."""
        return agent in self._members.get(channel, {})

    def owner_of(self, agent: str) -> str:
        """The owner the agent's presence subject names (RT-6.37), '' for none. The login
        service pins it there by the credential's one presence permission, as it pins the
        sender: no agent can publish a presence under another owner."""
        anchor = self._owners.get(agent, "")
        att = self.attestation_of(agent)
        if att is not None and anchor and att.owner_anchor == anchor:
            return att.owner_name  # the anchor is the same one: the name it has now
        return anchor

    def anchor_of(self, agent: str) -> str:
        """The owner the agent's presence subject names, as the login service pinned it (the
        anchor), '' for none: what owner_of returns before any attestation."""
        return self._owners.get(agent, "")

    def attestation_of(self, agent: str) -> Attestation | None:
        """The login service's newest attestation of the agent, None when there is none or it
        has expired by the server's clock (never this host's)."""
        seen = self._attest.get(agent)
        if seen is None or seen[2].exp <= self._server_now:
            return None
        return seen[2]

    def presence_of(self, agent: str) -> str:
        """online or away while the agent's last presence is younger than
        PRESENCE_FRESH_SEC by the server's clock (never this host's) and no bye;
        else offline."""
        seen = self._presence.get(agent)
        if seen is None or seen[1] == "bye" or self._server_now - seen[0] >= PRESENCE_FRESH_SEC:
            return "offline"
        return seen[1]

    def presence_snapshot(self) -> dict[str, tuple[float, str]]:
        """agent -> (server time, status) of the last presence message of each."""
        return dict(self._presence)

    @property
    def server_now(self) -> float:
        """The newest server timestamp this host has seen: its "now" for every age."""
        return self._server_now

    def claim_info(self, channel: str) -> dict[str, tuple[int, float]]:
        """agent -> (stream sequence, server time) of its crown claim in the channel."""
        return {a: (seq, self._claim_at.get((channel, a), 0.0))
                for a, seq in self._claims.get(channel, {}).items()}

    def own_echo_age(self) -> float:
        """Seconds (monotonic clock) since this agent's own presence last came back over its
        presence consumer, or since the connection opened. A half-open connection delivers
        nothing: nats-py notices only after minutes (RT-6.38 step 2)."""
        return time.monotonic() - self._own_echo

    def claim_times(self, channel: str) -> dict[str, float]:
        return {a: self._claim_at.get((channel, a), 0.0) for a in self._claims.get(channel, {})}

    def candidates(self, channel: str, now: float | None = None) -> list[str]:
        """The agents that may hold the channel's crown, in claim order: fresh presence and
        a membership of the channel (RT-6.38); the standing-peer rule is the port's."""
        return crown_candidates(self._claims.get(channel, {}), self._presence,
                                self._server_now if now is None else now,
                                lambda a: self.is_member(channel, a), at=self.claim_times(channel))

    def holder(self, channel: str, now: float | None = None) -> str | None:
        """The crown of a channel. "Now" is the newest server timestamp this host has seen,
        never its own clock (which may be wrong)."""
        return crown_holder(self._claims.get(channel, {}), self._presence,
                            self._server_now if now is None else now, at=self.claim_times(channel))

    # --- receiving ----------------------------------------------------------------------
    def _handler(self, where: str) -> Callable[[Any], Awaitable[None]]:
        async def handle(msg: Any) -> None:
            await self._on_deliver(where, msg.subject, msg.reply, msg.data)
        return handle

    def _see(self, server_time: float) -> None:
        """Advance the host's server clock, and drop what the stream's max_age drops."""
        if server_time <= self._server_now:
            return
        self._server_now = server_time
        if server_time - self._last_prune < 60.0:
            return
        self._last_prune = server_time
        cut = server_time - MAX_AGE_SEC
        for agent, (t, _) in list(self._presence.items()):
            if t < cut:
                del self._presence[agent]
                self._owners.pop(agent, None)
                self._bots.pop(agent, None)
                self._presence_seq.pop(agent, None)
        for agent, (_, t, _a) in list(self._attest.items()):
            if t < cut:
                del self._attest[agent]
        for ch, members in list(self._members.items()):
            for agent in list(members):
                if members[agent][1] < cut:
                    del members[agent]
            if not members:
                del self._members[ch]
        for ch, claims in list(self._claims.items()):
            for agent in list(claims):
                if self._claim_at.get((ch, agent), 0.0) < cut:
                    del claims[agent]
                    self._claim_at.pop((ch, agent), None)
            if not claims:
                del self._claims[ch]

    async def _on_deliver(self, where: str, subject: str, reply: str | None, data: bytes) -> None:
        consumer = f"{self.name}_dm" if where == DM_SINCE else f"{self.name}_ch_{where}"
        ack = parse_ack(reply, BUS_STREAM, consumer)
        if ack is None:
            log.warning("nats bus: dropped a message on %s without this consumer's JetStream metadata", where)
            return
        self._see(ack.time)
        gap = self._gaps.observe(where, ack.consumer_seq)  # every delivery counts, dropped or not
        tokens = subject.split(".")
        sender = tokens[-1]
        expect = [self._s.bus, "ch", where] if where != DM_SINCE else [self._s.bus, "dm", self.name]
        if tokens[:-1] != expect or not AGENT_RE.fullmatch(sender):
            log.warning("nats bus: dropped a message on %s with an unexpected subject", where)
            return
        if gap:
            self._gap(where)
        if where in self._stale:
            return
        if ack.stream_seq <= self._last_seq.get(where, 0):
            return  # a repeat after a restart from `since`
        self._last_seq[where] = ack.stream_seq
        try:
            message = decode(data, self._s.props_prefix)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed message on %s (seq %d): %s", where, ack.stream_seq, exc)
            return
        event = Event(where, sender, ack.stream_seq, ack.time, message)
        try:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            log.warning("nats bus: event callback failed: %s", type(exc).__name__)

    def _gap(self, where: str) -> None:
        """A gap on `where`: it is dropped (stale) until a reconnect restarts it from
        `since`. At most one reconnect per gap interval: within it, the reconnect is
        scheduled for when the interval is over, so the gap is never lost for good."""
        self._stale.add(where)
        now = time.monotonic()
        wait = self._gap_sec - (now - self._last_gap_reconnect)
        if wait <= 0:
            log.warning("nats bus: gap on %s, reconnecting with since", where)
            self._last_gap_reconnect = now
            self._wake.set()
            return
        log.warning("nats bus: gap on %s, reconnect held back %.0f s", where, wait)
        if self._gap_task is None or self._gap_task.done():
            self._gap_task = asyncio.create_task(self._gap_reconnect_after(wait))

    async def _gap_reconnect_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        if not self._stopping:
            self._last_gap_reconnect = time.monotonic()
            self._wake.set()

    async def _on_presence_msg(self, msg: Any) -> None:
        ack = parse_ack(msg.reply, PRESENCE_STREAM, f"{self.name}_presence")
        if ack is None:
            log.warning("nats bus: dropped a presence message without this consumer's JetStream metadata")
            return
        self._see(ack.time)
        if ack.consumer_seq <= self._presence_cseq:
            # The consumer was made anew (the login service at a credential, a stream made
            # anew): what is delivered now is what the stream holds. Remembered claims may
            # not be in it: forget them, so that an own claim that is gone is claimed again.
            self._claims.clear()
            self._claim_at.clear()
            self._synced = False  # until a delivery with pending 0: the replay is not read yet
        self._presence_cseq = ack.consumer_seq
        if ack.pending == 0:
            self._synced = True
            self._presence_read = True
        t = msg.subject.split(".")
        p = self._s.presence
        try:
            if len(t) == 3 and t[0] == p and AGENT_RE.fullmatch(t[2]):
                # <presence>.<owner>.<name>: the owner as the login service pinned it.
                agent, status = t[2], decode_presence(msg.data)
                seq, at = self._presence_seq.get(agent, (0, 0.0))
                if (ack.time, ack.stream_seq) <= (at, seq):
                    # An older message never overrides a newer one: ordered by the server's
                    # time, then the sequence (a PRESENCE stream made anew starts at 1).
                    return
                self._presence_seq[agent] = (ack.stream_seq, ack.time)
                self._presence[agent] = (ack.time, status)
                if agent == self.name:
                    self._own_echo = time.monotonic()
                    self._echo_backoff = 0.0  # a real echo: the next outage starts afresh
                bot = decode_presence_bot(msg.data)
                if bot:
                    self._bots[agent] = bot
                else:
                    self._bots.pop(agent, None)
                owner = owner_of_token(t[1])
                if owner:
                    self._owners[agent] = owner
                else:
                    self._owners.pop(agent, None)
            elif len(t) == 4 and t[0] == p and t[1] == "crown" and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3]):
                decode_claim(msg.data)
                claims = self._claims.setdefault(t[2], {})
                if (ack.time, ack.stream_seq) > (self._claim_at.get((t[2], t[3]), 0.0), claims.get(t[3], 0)):
                    claims[t[3]] = ack.stream_seq
                    self._claim_at[(t[2], t[3])] = ack.time
            elif len(t) == 4 and t[0] == p and t[1] == "member" and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3]):
                decode_claim(msg.data)
                members = self._members.setdefault(t[2], {})
                seq0, at0 = members.get(t[3], (0, 0.0))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    members[t[3]] = (ack.stream_seq, ack.time)
            elif len(t) == 4 and t[0] == p and t[1] == "attest" and t[3] == "v1" and AGENT_RE.fullmatch(t[2]):
                att = decode_attestation(msg.data, t[2])
                seq0, at0, _old = self._attest.get(t[2], (0, 0.0, att))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._attest[t[2]] = (ack.stream_seq, ack.time, att)
            else:
                log.warning("nats bus: dropped a presence message with an unexpected subject")
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed presence message (seq %d): %s", ack.stream_seq, exc)
