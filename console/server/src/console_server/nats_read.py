"""The console's read-only NATS side: one connection per logged-in operator
(RT-6.43 steps 1 to 4, the console decision's addition of 2026-11-25 to
ADR-015, points 1 and 5 to 7). No static account any more.

At a login the server makes a key pair for that login, kept in memory
only, and connects with the operator's Keycloak access token, bare, as the
connect token; the key signs the server's nonce. The login service issues
the person's credential (``<person prefix><owner token>``, 15 minutes,
publish nothing, subscribe only ``deliver.<name>.<login>.>``) and makes the
login's two push consumers. The console subscribes their deliver subjects
by their literal names — no inbox, no JetStream API, nothing published.

Every ``RENEW_SEC`` (10 minutes) the reader refreshes the access token with
the refresh token of the operator's ordinary Keycloak session and connects
again with the same key: the consumers keep their position. The new
connection is opened first and subscribed only once the old one is closed,
so no delivery reaches two subscriptions (a repeated consumer sequence
would read as a consumer made anew). A refresh the realm refuses, or a
refreshed token without the operators' group, ends the console session.

The name is computed from the token's ``preferred_username``, as an agent's
host computes its own (``natsbus.owner_token``): the login service names
the person after the directory's anchor, which is the username for every
identity from before the import. Where the two differ the server refuses
the subscriptions; the reader then stops reading and says so (the limit of
RT-6.39 step 2, #86).

Connection rules as the mcp package's transport (``natsbus``): ``tls://``
anywhere, ``nats://`` only to a loopback host, TLS first on the connection.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import nkeys  # type: ignore[import-untyped]
from nats.aio.client import Client as NatsClient

from mattermost_ai_agent_bus.natsbus import connect_reason, owner_token

from . import config
from .auth import Refused, Tokens
from .fleet import Fleet


log = logging.getLogger("console.nats")

RENEW_SEC = 600.0          # the credential lives 15 minutes; renewed every 10
BACKOFF_MAX_SEC = 60.0
CONNECT_TIMEOUT_SEC = 5.0
REACH_TIMEOUT_SEC = 3.0


class NameRefused(Exception):
    """The server refused this login's deliver subjects: the credential
    carries another name than the one computed from the username."""


class Ended(Exception):
    """The session's reading ends: the realm refused the renewal, the
    operators' group is gone, or the console session ran out."""


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


def person_name(prefix: str, username: str) -> str:
    """``<person prefix><owner token>``, as the login service names a
    person whose anchor is their username (``callout/person.go``)."""
    return prefix + owner_token(username)


def login_of(public_key: str) -> str:
    """A login's token in names and subjects: the first ten characters of
    its public key after the ``U``, in lower case (``callout.loginOf``)."""
    return public_key[1:11].lower()


class View:
    """What one session shows: its own fleet, fed by its own reader (or by
    nobody, without NATS), and one wake per live fleet stream."""

    def __init__(self, presence_subject: str) -> None:
        self.fleet = Fleet(presence_subject=presence_subject)
        self.wakes: set[asyncio.Event] = set()
        self.reader: PersonReader | None = None
        self.stats_lines = 0          # STATS deliveries; decoded with step 8

    def fire(self) -> None:
        for wake in list(self.wakes):
            wake.set()

    def close(self) -> None:
        """The session ended: the reader stops and drops key and tokens."""
        if self.reader is not None:
            self.reader.stop()


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

    def __init__(self, settings: config.Settings, username: str,
                 tokens: Tokens, renew: Renew, view: View, until: float,
                 on_end: Callable[[], None],
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
        self.name = person_name(settings.person_prefix, username)
        self.login = login_of(self.public_key)
        self.presence_consumer = f"{self.name}_{self.login}_presence"
        self.stats_consumer = f"{self.name}_{self.login}_stats"
        self.deliver_presence = f"deliver.{self.name}.{self.login}.presence"
        self.deliver_stats = f"deliver.{self.name}.{self.login}.stats"
        self.state = "connecting"
        self.connects = 0
        self._nc: Conn | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    # --- the seam: one connection with this login's key -------------------------

    async def dial(self, access: str, lost: Callable[[], Awaitable[None]],
                   refused: list[str]) -> Conn:
        key = self._key
        nc = NatsClient()
        # nats-py has no argument for the public key with a signature
        # callback; this is the attribute its own seed path sets (as natsbus)
        nc._public_nkey = self.public_key

        def sign(nonce: str) -> bytes:
            return base64.b64encode(key.sign(nonce.encode()))

        async def error(exc: Exception) -> None:
            if "permissions violation" in str(exc).lower():
                refused.append("permissions violation")
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
        self._task = asyncio.create_task(self._run(), name=f"console-{self.login}")

    def stop(self) -> None:
        """Logout or the session's end: close and drop key and tokens."""
        self._stopped = True
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

    async def _run(self) -> None:
        ended = False
        try:
            await self._loop()
        except Ended as exc:
            ended = True
            log.info("console: %s's reading ends: %s", self.name, exc)
        finally:
            self.state = "ended"
            nc, self._nc = self._nc, None
            if nc is not None and not nc.is_closed:
                try:
                    await asyncio.wait_for(asyncio.shield(nc.close()), 2)
                except Exception:  # noqa: BLE001
                    pass
            self._tokens = None
            if self._key is not None:
                self._key.wipe()
                self._key = None
            if ended and not self._stopped:
                self._on_end()

    async def _loop(self) -> None:
        assert self._tokens is not None
        access: str | None = self._tokens.access   # the login's own, fresh
        backoff = 1.0
        while True:
            if time.time() >= self.until:
                raise Ended("the console session ran out")
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

            refused: list[str] = []
            try:
                nc = await self.dial(access, lost, refused)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every refusal retries
                access = None
                self.state = ("refused by the bus" if "authorization" in str(exc).lower()
                              else "the bus is unreachable")
                log.warning("console: %s cannot connect (%s; %s); retrying in %.0f s",
                            self.name, connect_reason(exc), self.state, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                continue
            access = None
            old, self._nc = self._nc, nc
            if old is not None and not old.is_closed:
                # the old connection first: one subscription per subject
                await old.close()
            try:
                await self._subscribe(nc, refused)
            except NameRefused:
                self.state = "the bus knows this person under another name"
                log.warning("console: the bus refused %s's deliver subjects: its"
                            " anchor is not its username (#86); reading nothing",
                            self.name)
                await nc.close()
                self._nc = None
                await asyncio.sleep(max(0.0, self.until - time.time()))
                raise Ended("the console session ran out") from None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("console: %s: subscribe failed (%s)", self.name,
                            connect_reason(exc))
                await nc.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                continue
            backoff = 1.0
            self.connects += 1
            self.state = "reading"
            wait = max(0.0, min(self.renew_sec, self.until - time.time()))
            try:
                await asyncio.wait_for(asyncio.shield(closed), wait)
                self.state = "reconnecting"
                log.info("console: %s's connection ended; connecting again", self.name)
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
            raise Ended(f"the renewal was refused ({exc})") from None
        except Exception as exc:  # noqa: BLE001 - the realm or the network, passing
            self.state = "the renewal is failing"
            log.warning("console: %s's renewal failed (%s); retrying in %.0f s",
                        self.name, type(exc).__name__, backoff)
            await asyncio.sleep(backoff)
            return None

    async def _subscribe(self, nc: Conn, refused: list[str]) -> None:
        view, settings = self.view, self.settings

        async def presence(msg: Any) -> None:
            if view.fleet.on_delivery(msg.subject, msg.data, msg.reply,
                                      settings.presence_stream,
                                      self.presence_consumer):
                view.fire()

        async def stats(msg: Any) -> None:
            view.stats_lines += 1     # read and shown with the reports (step 8)

        await nc.subscribe(self.deliver_presence, cb=presence)
        await nc.subscribe(self.deliver_stats, cb=stats)
        # two round trips: every -ERR for the SUBs has been handled (natsbus._sync)
        await nc.flush()
        await nc.flush()
        if refused:
            raise NameRefused()
