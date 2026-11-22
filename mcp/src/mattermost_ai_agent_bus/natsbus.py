"""NATS transport of the agent bus (ADR-014, point 16). Not wired into the server yet.

The permission model, from the host's side:

* The sender of a message is the last token of its subject
  (`<bus>.ch.<channel>.<sender>`, `<bus>.dm.<recipient>.<sender>`,
  `<presence>.<sender>`, `<presence>.crown.<channel>.<sender>`), never a header or
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
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from nats.aio.client import Client as NatsClient

from .login import NKey

log = logging.getLogger(__name__)

MAX_BODY = 65536  # the BUS stream's max_msg_size
HEARTBEAT_SEC = 30.0
PRESENCE_FRESH_SEC = 90.0  # a holder's last presence must be younger than this
GAP_RECONNECT_SEC = 30.0  # at most one reconnect for a gap within this time
DELIVER = "deliver"  # consumers.DeliverPrefix / callout.DeliverPrefix

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # Go checkToken
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # Go consumers.agentRE
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")

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


def _check(m: Message) -> None:
    if m.kind not in _KINDS:
        raise MessageError("unknown kind")
    if not isinstance(m.text, str):
        raise MessageError("text is not a string")
    if m.root is not None and not _seq(m.root):
        raise MessageError("root is not a sequence")
    for k, v in m.props.items():
        if not isinstance(k, str) or not isinstance(v, str | int | float | bool):
            raise MessageError("props hold only strings and simple values")
    if m.kind == "post":
        if m.ref is not None or m.emoji is not None:
            raise MessageError("a post has no ref or emoji")
    else:
        if not _seq(m.ref):
            raise MessageError("a reaction needs ref")
        if not isinstance(m.emoji, str) or not m.emoji:
            raise MessageError("a reaction needs an emoji")
        if m.text != "" or m.root is not None:
            raise MessageError("a reaction has no text or root")


def encode(m: Message) -> bytes:
    _check(m)
    body = json.dumps({"v": 1, "kind": m.kind, "text": m.text, "root": m.root, "ref": m.ref,
                       "emoji": m.emoji, "props": m.props}, ensure_ascii=False,
                      separators=(",", ":")).encode()
    if len(body) > MAX_BODY:
        raise MessageError(f"message is {len(body)} bytes, more than {MAX_BODY}")
    return body


def _obj(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BODY:
        raise MessageError("body too big")
    try:
        obj = json.loads(data)
    except (ValueError, RecursionError):
        raise MessageError("body is not JSON") from None
    if not isinstance(obj, dict):
        raise MessageError("body is not an object")
    v = obj.get("v")
    if isinstance(v, bool) or v != 1 or not isinstance(v, int):
        raise MessageError("unknown version")
    return obj


def decode(data: bytes) -> Message:
    """Strict: anything that is not a well-formed v1 message raises MessageError.
    Unknown keys (a forged `sender`, say) are ignored, never read."""
    obj = _obj(data)
    props = obj.get("props", {})
    if not isinstance(props, dict):
        raise MessageError("props is not an object")
    m = Message(kind=obj.get("kind"), text=obj.get("text", ""), root=obj.get("root"),  # type: ignore[arg-type]
                ref=obj.get("ref"), emoji=obj.get("emoji"), props=props)
    _check(m)
    return m


def encode_presence(status: str) -> bytes:
    if status not in _STATUSES:
        raise MessageError("unknown status")
    return json.dumps({"v": 1, "status": status}, separators=(",", ":")).encode()


def decode_presence(data: bytes) -> str:
    status = _obj(data).get("status")
    if status not in _STATUSES:
        raise MessageError("unknown status")
    return str(status)


def encode_claim() -> bytes:
    return b'{"v":1}'


def decode_claim(data: bytes) -> None:
    _obj(data)


# --- JetStream metadata and gaps -----------------------------------------------------

@dataclass(frozen=True)
class Ack:
    stream_seq: int
    consumer_seq: int
    time: float  # the server's timestamp, seconds since the epoch


def parse_ack(reply: str | None) -> Ack | None:
    """`$JS.ACK.<stream>.<consumer>.<delivered>.<sseq>.<cseq>.<ts ns>.<pending>`, or with
    domain and account hash in front (and a token behind). None: not JetStream's."""
    if not reply:
        return None
    t = reply.split(".")
    if t[:2] != ["$JS", "ACK"] or len(t) not in (9, 11, 12):
        return None
    i = 4 if len(t) == 9 else 6
    try:
        sseq, cseq, ns = int(t[i + 1]), int(t[i + 2]), int(t[i + 3])
    except ValueError:
        return None
    if sseq < 1 or cseq < 1:
        return None
    return Ack(sseq, cseq, ns / 1e9)


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

def crown_holder(claims: Mapping[str, int], presence: Mapping[str, tuple[float, str]],
                 now: float, fresh: float = PRESENCE_FRESH_SEC) -> str | None:
    """The crown of one channel: the claim with the lowest stream sequence among the
    agents whose last presence message is younger than `fresh` seconds and not `bye`.
    claims: agent -> stream sequence of its (latest) claim; presence: agent ->
    (server time, status) of its last presence message."""
    best: tuple[int, str] | None = None
    for agent, seq in claims.items():
        seen = presence.get(agent)
        if seen is None or seen[1] == "bye" or now - seen[0] >= fresh:
            continue
        if best is None or seq < best[0]:
            best = (seq, agent)
    return best[1] if best else None


# --- CONNECT ---------------------------------------------------------------------------------

def build_hello(token: str, home: str, guests: tuple[str, ...], lobby: bool,
                since: Mapping[str, int]) -> str:
    """The CONNECT `auth_token` the login service reads (callout.Hello). It holds the
    access token: never log it."""
    hello: dict[str, Any] = {"token": token}
    if home:
        hello["home"] = home
    if guests:
        hello["guests"] = list(guests)
    if lobby:
        hello["lobby"] = True
    known = {ch: seq for ch, seq in since.items() if seq > 0}
    if known:
        hello["since"] = known
    return json.dumps(hello, separators=(",", ":"))


# --- the bus ---------------------------------------------------------------------------------------

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


@dataclass(frozen=True)
class Event:
    where: str  # a channel name, or "dm"
    sender: str  # the subject's last token
    stream_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    message: Message


class LoginLike(Protocol):
    async def access_token(self) -> str: ...
    def nkey(self) -> NKey: ...


EventCallback = Callable[[Event], Awaitable[None] | None]


class NatsBus:
    def __init__(self, settings: NatsSettings, login: LoginLike, owner: str, on_event: EventCallback,
                 *, heartbeat_sec: float = HEARTBEAT_SEC, gap_reconnect_sec: float = GAP_RECONNECT_SEC,
                 connect_timeout: float = 5.0) -> None:
        for what, v in (("bus", settings.bus), ("presence", settings.presence)):
            if not PREFIX_RE.match(v) or v == DELIVER:
                raise ValueError(f"invalid {what} prefix")
        self._s = settings
        self._login = login
        self._owner = owner
        self._on_event = on_event
        self._heartbeat_sec = heartbeat_sec
        self._gap_sec = gap_reconnect_sec
        self._connect_timeout = connect_timeout
        self._name: str | None = None
        self._nc: NatsClient | None = None
        self._connected = False
        self._stopping = False
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._gaps = GapTracker()
        self._last_gap_reconnect = -1e18
        self._stale: set[str] = set()  # channels with a gap, dropped until the reconnect
        self._last_seq: dict[str, int] = {}  # channel -> last stream sequence delivered
        self._presence: dict[str, tuple[float, str]] = {}
        self._claims: dict[str, dict[str, int]] = {}

    def __repr__(self) -> str:
        return f"NatsBus(name={self._name!r}, connected={self._connected})"

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = agent_name(self._owner, self._login.nkey().public_key, self._s.name_prefix)
        return self._name

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
        return {ch: self._last_seq[ch] for ch in self.channels if self._last_seq.get(ch)}

    # --- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels:
            if not CHANNEL_RE.match(ch):
                raise ValueError("invalid channel name")
        _ = self.name
        await self._open()
        self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._heartbeat())]

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        nc = self._nc
        if nc is not None and self._connected:
            try:
                await nc.publish(f"{self._s.presence}.{self.name}", encode_presence("bye"))
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

    # --- connection ----------------------------------------------------------------
    async def _open(self) -> None:
        """One connection with a fresh access token, `since` and the literal subscriptions."""
        token = await self._login.access_token()
        key = self._login.nkey()
        hello = build_hello(token, self._s.home, self._s.guests, self._s.lobby, self.since())
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
            if "permissions violation" in str(exc).lower():
                refused.append("permissions violation")
            log.warning("nats bus: %s", type(exc).__name__)

        await nc.connect(list(self._s.servers), token=hello, signature_cb=sign, allow_reconnect=False,
                         connect_timeout=int(self._connect_timeout), error_cb=error,
                         disconnected_cb=lost, closed_cb=lost)
        del hello
        self._nc = nc
        self._gaps.reset()
        self._stale.clear()
        name = self.name
        try:
            for ch in self.channels:
                await nc.subscribe(f"{DELIVER}.{name}.ch.{ch}", cb=self._handler(ch))
            await nc.subscribe(f"{DELIVER}.{name}.dm", cb=self._handler("dm"))
            await nc.subscribe(f"{DELIVER}.{name}.presence", cb=self._on_presence_msg)
            # The server answers a refused SUB with -ERR before the PONG of this
            # flush: a refusal of the own deliver subjects means the credential
            # carries another name than the computed one (the credential is the
            # authority). Nothing has been published yet.
            await nc.flush()
            await nc.flush()  # a second round trip: every -ERR before it has been handled
            if refused:
                raise NameMismatch("the credential's agent name is not the computed one; nothing sent")
            await nc.publish(f"{self._s.presence}.{name}", encode_presence("online"))
            await nc.flush()
        except BaseException:
            await nc.close()
            self._nc = None
            raise
        self._connected = True
        if nc.is_closed:  # lost while subscribing
            self._connected = False
            self._wake.set()

    async def _supervise(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            old, self._nc, self._connected = self._nc, None, False
            if old is not None:
                try:
                    await old.close()
                except Exception:
                    pass
            delay = 1.0
            while not self._stopping:
                try:
                    await self._open()
                    log.info("nats bus: reconnected")
                    break
                except asyncio.CancelledError:
                    raise
                except NameMismatch:
                    log.error("nats bus: the credential's agent name is not the computed one; stopped")
                    self._stopping = True
                    return
                except Exception as exc:
                    log.warning("nats bus: reconnect failed: %s", type(exc).__name__)
                    await asyncio.sleep(delay * random.uniform(0.5, 1.0))
                    delay = min(delay * 2, 30.0)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            nc = self._nc
            if nc is not None and self._connected:
                try:
                    await nc.publish(f"{self._s.presence}.{self.name}", encode_presence("online"))
                except Exception as exc:
                    log.warning("nats bus: heartbeat: %s", type(exc).__name__)

    # --- sending ---------------------------------------------------------------------
    async def _publish(self, subject: str, body: bytes) -> None:
        nc = self._nc
        if nc is None or not self._connected:
            raise NatsBusError("not connected")
        try:
            await nc.publish(subject, body)  # never a Nats-Msg-Id header
            await nc.flush()
        except Exception as exc:
            raise NatsBusError(f"publish failed: {type(exc).__name__}") from None

    @staticmethod
    def _channel(ch: str) -> str:
        if not CHANNEL_RE.match(ch):
            raise ValueError("invalid channel name")
        return ch

    async def post(self, channel: str, text: str, root: int | None = None,
                   props: dict[str, Prop] | None = None) -> None:
        body = encode(Message("post", text, root=root, props=props or {}))
        await self._publish(f"{self._s.bus}.ch.{self._channel(channel)}.{self.name}", body)

    async def react(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("reaction", ref=ref, emoji=emoji))
        await self._publish(f"{self._s.bus}.ch.{self._channel(channel)}.{self.name}", body)

    async def unreact(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("unreaction", ref=ref, emoji=emoji))
        await self._publish(f"{self._s.bus}.ch.{self._channel(channel)}.{self.name}", body)

    def _dm_subject(self, recipient: str) -> str:
        if not AGENT_RE.match(recipient):
            raise ValueError("invalid recipient name")
        return f"{self._s.bus}.dm.{recipient}.{self.name}"

    async def dm(self, recipient: str, text: str, root: int | None = None,
                 props: dict[str, Prop] | None = None) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("post", text, root=root, props=props or {})))

    async def dm_react(self, recipient: str, ref: int, emoji: str, remove: bool = False) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("unreaction" if remove else "reaction",
                                                    ref=ref, emoji=emoji)))

    async def claim(self, channel: str) -> None:
        await self._publish(f"{self._s.presence}.crown.{self._channel(channel)}.{self.name}", encode_claim())

    def presence_snapshot(self) -> dict[str, tuple[float, str]]:
        """agent -> (server time, status) of the last presence message of each."""
        return dict(self._presence)

    def holder(self, channel: str, now: float | None = None) -> str | None:
        return crown_holder(self._claims.get(channel, {}), self._presence,
                            time.time() if now is None else now)

    # --- receiving ----------------------------------------------------------------------
    def _handler(self, where: str) -> Callable[[Any], Awaitable[None]]:
        async def handle(msg: Any) -> None:
            await self._on_deliver(where, msg.subject, msg.reply, msg.data)
        return handle

    async def _on_deliver(self, where: str, subject: str, reply: str | None, data: bytes) -> None:
        ack = parse_ack(reply)
        if ack is None:
            log.warning("nats bus: dropped a message on %s without JetStream metadata", where)
            return
        gap = self._gaps.observe(where, ack.consumer_seq)  # every delivery counts, dropped or not
        tokens = subject.split(".")
        sender = tokens[-1]
        expect = [self._s.bus, "ch", where] if where != "dm" else [self._s.bus, "dm", self.name]
        if tokens[:-1] != expect or not AGENT_RE.match(sender):
            log.warning("nats bus: dropped a message on %s with an unexpected subject", where)
            return
        if gap and self._gap(where):
            return
        if where in self._stale:
            return
        if where != "dm":
            if ack.stream_seq <= self._last_seq.get(where, 0):
                return  # a repeat after a restart from `since`
            self._last_seq[where] = ack.stream_seq
        try:
            message = decode(data)
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

    def _gap(self, where: str) -> bool:
        """A gap on `where`. True: drop the message, a reconnect with `since` follows."""
        now = time.monotonic()
        if now - self._last_gap_reconnect < self._gap_sec:
            log.warning("nats bus: gap on %s, reconnect held back", where)
            return False
        log.warning("nats bus: gap on %s, reconnecting with since", where)
        self._last_gap_reconnect = now
        if where != "dm":
            self._stale.add(where)
        self._wake.set()
        return True

    async def _on_presence_msg(self, msg: Any) -> None:
        ack = parse_ack(msg.reply)
        if ack is None:
            log.warning("nats bus: dropped a presence message without JetStream metadata")
            return
        t = msg.subject.split(".")
        p = self._s.presence
        try:
            if len(t) == 2 and t[0] == p and AGENT_RE.match(t[1]):
                self._presence[t[1]] = (ack.time, decode_presence(msg.data))
            elif len(t) == 4 and t[0] == p and t[1] == "crown" and CHANNEL_RE.match(t[2]) and AGENT_RE.match(t[3]):
                decode_claim(msg.data)
                claims = self._claims.setdefault(t[2], {})
                if ack.stream_seq > claims.get(t[3], 0):
                    claims[t[3]] = ack.stream_seq
            else:
                log.warning("nats bus: dropped a presence message with an unexpected subject")
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed presence message (seq %d): %s", ack.stream_seq, exc)
