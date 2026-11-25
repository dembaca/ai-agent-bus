"""The console's read-only NATS side: one connection per logged-in operator
(RT-6.43 steps 1 to 4, the console decision's addition of 2026-11-25 to
ADR-015, points 1 and 5 to 7). No static account any more.

At a login the server makes a key pair for that login, kept in memory
only, and connects with the operator's Keycloak access token, bare, as the
connect token; the key signs the server's nonce. The login service issues
the person's credential and makes the login's consumers. The console never
derives the person's name: it subscribes by the login alone —
``deliver.person.<login>.presence`` and ``.stats``, ``<login>`` the login's
whole public key in lower case — and learns its NATS name and anchor from
the login's own record, which the service writes at each issue on
``<presence>.person.<login>.v1`` (``{"v":1,"name","anchor","iat","exp"}``)
and only this login's presence consumer delivers, with the rest. Its
``exp`` is that credential's end. No inbox, no JetStream API, nothing
published.

Every ``RENEW_SEC`` (10 minutes) the reader refreshes the access token with
the refresh token of the operator's ordinary Keycloak session and connects
again with the same key: the consumers keep their position. The new
connection subscribes while the old one still stands, then the old one
closes; a delivery seen on both is dropped the second time, so no repeat
reads as a consumer made anew. A refresh the realm refuses, a refreshed
token without the operators' group or of another ``sub``, ends the
session. A connect the login service refuses (not an operator, unknown to
the directory, too many logins) ends the reading at once, its key and
tokens wiped, its reason shown to the browser; it is not retried.

Connection rules as the mcp package's transport (``natsbus``): ``tls://``
anywhere, ``nats://`` only to a loopback host, TLS first on the connection.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import nkeys  # type: ignore[import-untyped]
from nats.aio.client import Client as NatsClient

from mattermost_ai_agent_bus.natsbus import connect_reason, parse_ack

from . import config
from .auth import Refused, Tokens
from .fleet import Fleet
from .stats_view import StatsBook


log = logging.getLogger("console.nats")

RENEW_SEC = 600.0          # the credential lives 15 minutes; renewed every 10
BACKOFF_MAX_SEC = 60.0
CONNECT_TIMEOUT_SEC = 5.0
REACH_TIMEOUT_SEC = 3.0
PERSON = "person"          # deliver.person.<login>.… (the login service's contract)
RECORD_MAX = 4096
RECORD_WAIT_SEC = 30.0     # no own record this long after a connect: say so
CREDENTIAL_SEC = 900.0     # a person's credential, when no record told its exp
RECORD_LEEWAY_SEC = 60.0   # an own record's iat may lie this far ahead
RECORD_SPAN_MAX_SEC = 1200.0   # exp at most 20 minutes after iat
SEEN_MAX = 512             # deliveries remembered to drop a renewal's repeats


class Ended(Exception):
    """The session's reading ends; its text is the state the browser sees."""


Renew = Callable[[str], Awaitable[Tokens]]   # the refresh token → fresh tokens


def tls_context(settings: config.Settings) -> ssl.SSLContext | None:
    """The verifying context for a tls:// URL; None for loopback nats://."""
    if settings.nats_url.startswith("nats://"):
        return None
    return ssl.create_default_context(cafile=settings.nats_ca_file or None)


async def reachable(settings: config.Settings,
                    timeout: float = REACH_TIMEOUT_SEC) -> bool:
    """Readiness: the NATS server answers on its port — for ``tls://`` a
    finished, verified TLS handshake (the server speaks TLS first), for
    loopback ``nats://`` a TCP connect. No credential is needed or sent."""
    parts = urlsplit(settings.nats_url)
    host, port = parts.hostname, parts.port or 4222
    if not host:
        return False
    ctx = tls_context(settings)
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx,
                                    server_hostname=host if ctx else None),
            timeout)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return False
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1.0)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        pass
    return True


def login_of(public_key: str) -> str:
    """A login's token in subjects: its whole public key in lower case (56
    characters), as the login service's ``callout.loginOf``."""
    return public_key.lower()


def consumer_of(reply: str | None) -> str:
    """The consumer named in a JetStream delivery's reply subject, or ''."""
    t = (reply or "").split(".")
    if t[:2] != ["$JS", "ACK"] or len(t) not in (9, 11, 12):
        return ""
    return t[3 if len(t) == 9 else 5]


def _int(value: object) -> bool:
    return type(value) is int          # not a bool, not a float


def decode_record(data: bytes, now: float | None = None) -> tuple[str, str, int] | None:
    """The login's own record (``callout.PersonRecord``): name, anchor (its
    subject form) and ``exp``, the credential's end. Strict: exactly the
    five fields, ``v`` the integer 1, ``iat`` not ahead (a minute's
    leeway), ``exp`` still ahead and at most 20 minutes after ``iat``;
    anything else is None and dropped."""
    if len(data) > RECORD_MAX:
        return None
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    if not isinstance(doc, dict) or set(doc) != {"v", "name", "anchor", "iat", "exp"}:
        return None
    v, name, anchor = doc["v"], doc["name"], doc["anchor"]
    iat, exp = doc["iat"], doc["exp"]
    at = time.time() if now is None else now
    if not (_int(v) and v == 1 and _int(iat) and _int(exp)
            and isinstance(name, str) and isinstance(anchor, str)
            and 0 < len(name) <= 128 and 0 < len(anchor) <= 128
            and all(c.isprintable() and c not in ". *>" for c in name + anchor)
            and iat <= at + RECORD_LEEWAY_SEC and exp > at
            and iat < exp <= iat + RECORD_SPAN_MAX_SEC):
        return None
    return name, anchor, exp


class Dedupe:
    """One consumer's deliveries across a renewal's overlap: its name pinned
    at the first delivery (others are dropped), and a delivery already read
    on the other connection dropped the second time. A consumer made anew
    starts its sequence again over other stream sequences: no repeat."""

    def __init__(self) -> None:
        self.consumer = ""
        self._seen: deque[tuple[int, int]] = deque(maxlen=SEEN_MAX)
        self._seen_set: set[tuple[int, int]] = set()
        self._last_cseq = 0

    def take(self, reply: str | None) -> str:
        """The consumer's name for a delivery to read, '' to drop it."""
        consumer = consumer_of(reply)
        ack = parse_ack(reply)
        if not consumer or ack is None:
            return ""
        if not self.consumer:
            self.consumer = consumer
        elif consumer != self.consumer:
            return ""
        key = (ack.consumer_seq, ack.stream_seq)
        if ack.consumer_seq <= self._last_cseq and key in self._seen_set:
            return ""
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        self._last_cseq = ack.consumer_seq
        return consumer


class View:
    """What one session shows: its own fleet, fed by its own reader (or by
    nobody, without NATS), and one wake per live fleet stream."""

    def __init__(self, presence_subject: str, stats_subject: str = "stats") -> None:
        self.fleet = Fleet(presence_subject=presence_subject)
        self.stats = StatsBook(stats_subject=stats_subject)   # the STATS reports (step 8)
        self.wakes: set[asyncio.Event] = set()
        self.reader: PersonReader | None = None
        self.stats_lines = 0          # STATS deliveries read; the book keeps the valid ones

    def fire(self) -> None:
        for wake in list(self.wakes):
            wake.set()

    def close(self) -> None:
        """The session ended: the reader stops and drops key and tokens."""
        if self.reader is not None:
            self.reader.stop()

    def asks(self) -> list[dict[str, Any]]:
        """The open asks this session's consumers delivered (RT-6.43 step 6): "for me" and
        "may answer" compare the operator's anchor from the login's own record, never the
        login name; n/a until that record came."""
        anchor = self.reader.anchor if self.reader is not None else None
        return self.fleet.asks(anchor)

    def bus(self) -> dict[str, Any]:
        """What the browser sees of this session's reading (``/v1/me``)."""
        r = self.reader
        if r is None:
            return {"state": "off", "name": None, "anchor": None, "until": None}
        return {"state": r.state, "name": r.name, "anchor": r.anchor,
                "until": r.credential_exp}


class Conn(Protocol):
    """What the reader uses of a NATS client (a seam for the tests)."""
    @property
    def is_closed(self) -> bool: ...

    async def subscribe(self, subject: str, *args: Any, **kwargs: Any) -> Any: ...
    async def flush(self, *args: Any, **kwargs: Any) -> None: ...
    async def close(self) -> None: ...


class PersonReader:
    """One operator's login on the bus: its key, its tokens, its
    connection, renewed every ``renew_sec`` until the session ends."""

    def __init__(self, settings: config.Settings, tokens: Tokens, renew: Renew,
                 view: View, until: float, on_end: Callable[[], None],
                 renew_sec: float = RENEW_SEC) -> None:
        self.settings = settings
        self.view = view
        self.until = until                      # the session's end, wall clock
        self.renew_sec = renew_sec
        self._renew = renew
        self._on_end = on_end
        self._tokens: Tokens | None = tokens
        self._key: Any = nkeys.from_seed(
            nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER))
        self.public_key: str = self._key.public_key.decode()
        self.login = login_of(self.public_key)
        base = f"deliver.{PERSON}.{self.login}"
        self.deliver_presence = f"{base}.presence"
        self.deliver_stats = f"{base}.stats"
        self.record_subject = f"{settings.presence_subject}.{PERSON}.{self.login}.v1"
        self.name: str | None = None            # from the login's own record
        self.anchor: str | None = None
        self.credential_exp: int | None = None  # the record's exp (Unix seconds)
        self._record = asyncio.Event()
        self.state = "connecting"
        self.connects = 0
        self._nc: Conn | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._presence = Dedupe()
        self._stats = Dedupe()
        self._started = time.time()

    # --- the seam: one connection with this login's key -------------------------

    async def dial(self, access: str, lost: Callable[[], Awaitable[None]]) -> Conn:
        key = self._key
        nc = NatsClient()
        # nats-py has no argument for the public key with a signature
        # callback; this is the attribute its own seed path sets (as natsbus)
        nc._public_nkey = self.public_key

        def sign(nonce: str) -> bytes:
            return base64.b64encode(key.sign(nonce.encode()))

        async def error(exc: Exception) -> None:
            log.warning("console: NATS %s", connect_reason(exc))

        kw: dict[str, Any] = {}
        tls = tls_context(self.settings)
        if tls is not None:
            kw = {"tls": tls, "tls_handshake_first": True}
        try:
            await asyncio.wait_for(
                nc.connect([self.settings.nats_url], token=access,
                           signature_cb=sign, allow_reconnect=False,
                           connect_timeout=int(CONNECT_TIMEOUT_SEC),
                           error_cb=error, disconnected_cb=lost,
                           closed_cb=lost, **kw),
                CONNECT_TIMEOUT_SEC * 2)
        except BaseException:
            try:
                await asyncio.wait_for(asyncio.shield(nc.close()), 2)
            except Exception:  # noqa: BLE001 - a half-open client goes either way
                pass
            raise
        return nc

    # --- the lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="console-login")

    def stop(self) -> None:
        """Logout or the session's end: close and drop key and tokens."""
        self._stopped = True
        self._wipe()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def aclose(self) -> None:
        self.stop()
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def holds_secrets(self) -> bool:
        return self._key is not None or self._tokens is not None

    def _wipe(self) -> None:
        self._tokens = None
        if self._key is not None:
            self._key.wipe()
            self._key = None

    async def _run(self) -> None:
        ended, ends_session = False, False
        try:
            await self._loop()
        except Ended as exc:
            ended = True
            self.state = str(exc)
            ends_session = self.state.startswith("ended")
            log.info("console: login %.10s… reading ends: %s", self.login, exc)
        finally:
            # the wipe first: a cancel during the close cannot skip it
            self._wipe()
            if not ended:
                self.state = "ended"
            nc, self._nc = self._nc, None
            try:
                if nc is not None and not nc.is_closed:
                    await asyncio.wait_for(asyncio.shield(nc.close()), 2)
            except BaseException:  # noqa: BLE001 - closing is best effort
                pass
            finally:
                if ends_session and not self._stopped:
                    self._on_end()

    async def _loop(self) -> None:
        assert self._tokens is not None
        access: str | None = self._tokens.access   # the login's own, fresh
        backoff = 1.0
        while True:
            if time.time() >= self.until:
                raise Ended("ended: the console session ran out")
            if access is None:
                # every connect after the first takes a fresh access token
                access = await self._fresh(backoff)
                if access is None:
                    backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                    continue
            closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            async def lost(closed: "asyncio.Future[None]" = closed) -> None:
                if not closed.done():
                    closed.set_result(None)

            try:
                nc = await self.dial(access, lost)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                access = None
                if "authorization" in str(exc).lower():
                    # NATS says the same for a refusal (no operator, no
                    # anchor, too many logins) and for a login service that
                    # is down or slow: retried until the current credential
                    # ends, then the reading ends and its secrets go
                    end = (self.credential_exp if self.credential_exp is not None
                           else self._started + CREDENTIAL_SEC)
                    if time.time() >= end:
                        raise Ended("refused: the bus refused this login (not an"
                                    " operator, unknown to the directory, too"
                                    " many logins, or the login service is"
                                    " down); log out and in again") from None
                    self.state = "waiting: the bus refused the connect; retrying"
                else:
                    self.state = "waiting: the bus is unreachable"
                log.warning("console: login %.10s… cannot connect (%s); retrying in %.0f s",
                            self.login, connect_reason(exc), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                continue
            access = None
            old, self._nc = self._nc, nc
            try:
                # overlap: the new subscriptions stand before the old
                # connection closes; repeats are dropped in _presence
                await self._subscribe(nc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("console: login %.10s…: subscribe failed (%s)",
                            self.login, connect_reason(exc))
                self._nc = old
                await nc.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                continue
            if old is not None and not old.is_closed:
                await old.close()
            backoff = 1.0
            self.connects += 1
            started = time.monotonic()
            if self._record.is_set():
                self.state = "reading"
            else:
                self.state = "waiting: for the login's own record"
            wait = max(0.0, min(self.renew_sec, self.until - time.time()))
            if self.credential_exp is not None:
                # renew five minutes before the credential's own end, never
                # more often than a minute (a skewed clock must not hammer)
                wait = min(wait, max(60.0, self.credential_exp - 300 - time.time()))
            try:
                if not self._record.is_set():
                    try:
                        await asyncio.wait_for(asyncio.shield(self._record.wait()),
                                               min(RECORD_WAIT_SEC, wait))
                    except asyncio.TimeoutError:
                        if not self._record.is_set() and not closed.done():
                            self.state = ("waiting: no record from the login service"
                                          f" after {RECORD_WAIT_SEC:.0f} s")
                rest = max(0.0, wait - (time.monotonic() - started))
                await asyncio.wait_for(asyncio.shield(closed), rest)
                self.state = "waiting: reconnecting"
                log.info("console: login %.10s…: connection ended; again", self.login)
            except asyncio.TimeoutError:
                pass   # the renewal: a fresh token, the same key

    async def _fresh(self, backoff: float) -> str | None:
        """A fresh access token, or None after a passing failure (waited
        out). The realm's refusal and a missing group end the session."""
        assert self._tokens is not None
        try:
            self._tokens = await self._renew(self._tokens.refresh)
            return self._tokens.access
        except asyncio.CancelledError:
            raise
        except Refused as exc:
            raise Ended(f"ended: the renewal was refused ({exc})") from None
        except Exception as exc:  # noqa: BLE001 - the realm or the network, passing
            self.state = "waiting: the renewal is failing"
            log.warning("console: login %.10s…: renewal failed (%s); retrying in %.0f s",
                        self.login, type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            return None

    async def _subscribe(self, nc: Conn) -> None:
        view, settings, login = self.view, self.settings, self.login

        async def presence(msg: Any) -> None:
            # the consumers' names are the login service's: the first seen
            # on this login's own deliver subject is pinned
            consumer = self._presence.take(msg.reply)
            if not consumer:
                return
            if msg.subject == self.record_subject:
                record = decode_record(msg.data)
                if record is None:
                    log.warning("console: login %.10s…: an unreadable own record", login)
                else:
                    self.name, self.anchor, self.credential_exp = record
                    self._record.set()
                    if self.state.startswith("waiting: no record") or \
                            self.state.startswith("waiting: for the login"):
                        self.state = "reading"
                    view.fire()
            if view.fleet.on_delivery(msg.subject, msg.data, msg.reply,
                                      settings.presence_stream, consumer):
                view.fire()

        async def stats(msg: Any) -> None:
            consumer = self._stats.take(msg.reply)
            if consumer:
                view.stats_lines += 1
                # decoded strictly; a line outside the rules is dropped (steps 7 and 8)
                view.stats.on_delivery(msg.subject, msg.data, msg.reply,
                                       settings.stats_stream, consumer)

        await nc.subscribe(self.deliver_presence, cb=presence)
        await nc.subscribe(self.deliver_stats, cb=stats)
        await nc.flush()
