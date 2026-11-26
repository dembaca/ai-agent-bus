"""The login channel of a keyless corral session (RT-6.18, RT-6.34, #154 step 2).

Decision 10 (the ADR-014 amendment of 2026-11-26): the MCP server the plugin
starts over stdio logs in itself and keeps the refresh token and the host key in
its own store, under corral in the sandbox's home. In a keyless session that
server is the relay in the sandbox (hosted.relay), while the bot token and the
NATS adapter stay in the host process until the registrar ends (#151 step 5).
So the relay holds the login (`hold`), and the host process asks it for what
the NATS adapter needs (`RelayedLogin`):

* The relay opens a second connection to the host's MCP endpoint with the
  session key and ``"login": true``. The refresh token never crosses it.
* It sends the nkey seed once (``{"event": "key", "seed": …}``): the host
  process signs the NATS server's nonce itself, as nats-py calls the signature
  synchronously. The host keeps the first key for the whole session.
* The host asks ``{"id": n, "op": "token"}``; the relay answers ``{"id": n,
  "token": …}`` from its `login.Login` (refresh or device approval) or
  ``{"id": n, "error": …}`` (a LoginError's text, which carries no secret).
* The relay tells the host about an approval (``waiting``, ``settled``,
  ``refused``): the host DMs the responsible human (login_notice.DMNotifier),
  since only it holds the bot token. The relay itself opens a browser and
  writes the address and the code to its stderr.

The host trusts nothing of the relay beyond the session key, which the agent can
read: of an approval it takes only the user code (a strict pattern) and builds
the link to DM from its own issuer, one open approval at a time and at most one
per cooldown; a token must be the responsible human's; an error keeps only its
kind (denied, wrong_human, config), so the NATS port stops on it. Nothing here logs a token, a seed or the device code.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import nkeys

from . import keystore, login, owners, session_file
from .login import ApprovalDenied, ConfigError, DeviceGrant, LoginError, NKey, NotifyError, Settings, WrongHuman

log = logging.getLogger(__name__)

LINE_MAX = 64 * 1024
CONNECT_SEC = 5.0
RETRY_SEC = 2.0
RETRY_MAX_SEC = 30.0
CHANNEL_WAIT_SEC = 60.0  # no login channel this long after the start: warned, and a token request fails
OUTCOMES = ("approved", "denied", "expired", "refused", "failed")
# A device flow's user code (Keycloak: ABCD-EFGH): letters and digits in at most two groups.
USER_CODE = re.compile(r"[A-Z0-9]{4,10}(-[A-Z0-9]{4,10})?")
# The errors that waiting does not cure keep their kind across the channel, so the
# NATS port stops instead of asking again (nats_port._final).
ERROR_KINDS: dict[str, type[LoginError]] = {
    "denied": ApprovalDenied, "wrong_human": WrongHuman, "config": ConfigError}


def offer(settings: Settings, human: str) -> dict[str, str]:
    """What the host's handshake answer tells the relay to log in with: names, no secret."""
    return {"issuer": settings.issuer, "client_id": settings.client_id,
            "nkey": settings.nkey_store, "human": human}


def settings_of(value: object) -> tuple[Settings, str] | None:
    """The relay's reading of `offer`; None when it is missing or not usable."""
    if not isinstance(value, Mapping):
        return None
    issuer, client, nkey, human = (value.get(k) for k in ("issuer", "client_id", "nkey", "human"))
    if not all(isinstance(v, str) and v for v in (issuer, client, nkey, human)):
        return None
    assert isinstance(issuer, str) and isinstance(client, str) and isinstance(nkey, str) and isinstance(human, str)
    try:
        login.check_url(issuer, "issuer")
    except LoginError:
        return None
    if nkey not in login.NKEY_STORES:
        return None
    return Settings(issuer=issuer.rstrip("/"), client_id=client, nkey_store=nkey), human


# --- the host process --------------------------------------------------------------

class _Closed(Exception):
    pass


class RelayedLogin:
    """The login the sandbox holds, seen from the host process (login.start_relayed)."""

    def __init__(self, settings: Settings, *, notifier: Any = None,
                 human: Callable[[], str] = owners.responsible_human,
                 now: Callable[[], float] = time.monotonic,
                 channel_wait_sec: float = CHANNEL_WAIT_SEC,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.settings = settings
        self._sleep = sleep
        self._notifier = notifier
        self._human = human
        self._now = now
        self._wait = channel_wait_sec
        self._claim: object | None = None
        self._send: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._ready = asyncio.Event()
        self._waiters: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next = 0
        self._kp: nkeys.KeyPair | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # The sandbox shows the link through its own hook API (#198); the relay has
        # no grant of its own to hand out.
        self._shown = False
        self._approval_open = False
        self._approval_id = 0
        self._approval_until = 0.0
        self._last_waiting: float | None = None
        self._last_refused: float | None = None
        self._warned: dict[str, float] = {}  # a warning's last time: the sandbox may repeat it
        # The newest approval that came within the cooldown: DMed when it ends.
        self._deferred: tuple[DeviceGrant, float] | None = None

    def __repr__(self) -> str:
        return (f"RelayedLogin(issuer={self.settings.issuer!r}, "
                f"channel={'open' if self._send is not None else 'none'})")

    def offer(self) -> dict[str, str]:
        return offer(self.settings, self._human().strip())

    def pending(self) -> DeviceGrant | None:
        """The relay holds no grant of its own: the host's link reaches the TUI over
        the relay's channel (#198)."""
        return None

    def shown(self) -> None:
        self._shown = True

    async def run(self) -> None:
        """Settle an ask an earlier session left open; the login itself runs in the
        sandbox. Warns when no login channel comes (an older relay there)."""
        if self._notifier is not None:
            try:
                await self._notifier.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("login: an earlier ask was not settled (%s)", type(exc).__name__)
        try:
            await asyncio.to_thread(clear_old_host_store, self.settings, self._human().strip())
        except Exception as exc:  # noqa: BLE001
            log.warning("login: the earlier host store was not cleared (%s)", type(exc).__name__)
        try:
            await asyncio.wait_for(self._ready.wait(), self._wait)
        except TimeoutError:
            log.warning("login: no login channel from the relay in the sandbox after %d s (an older "
                        "plugin there, or its login did not start): no access token for the NATS home",
                        int(self._wait))

    async def access_token(self) -> str:
        """An access token from the sandbox's login; waits for its channel at most
        `channel_wait_sec` at a time, then raises, so the NATS port logs why."""
        while True:
            try:
                await asyncio.wait_for(self._ready.wait(), self._wait)
            except TimeoutError:
                raise LoginError("no login channel from the relay in the sandbox (an older plugin "
                                 "there, or its login did not start)") from None
            send = self._send
            if send is None:
                await asyncio.sleep(0)
                continue
            self._next += 1
            rid = self._next
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._waiters[rid] = fut
            try:
                await send({"id": rid, "op": "token"})
                reply = await fut
            except (_Closed, OSError):
                continue  # the channel went: wait for the next relay
            finally:
                self._waiters.pop(rid, None)
            if "error" in reply:
                text = " ".join(str(reply["error"]).split())[:200]
                raise ERROR_KINDS.get(str(reply.get("kind", "")), LoginError)(text)
            token = reply.get("token")
            if not isinstance(token, str) or not token:
                raise LoginError("the sandbox's login sent no token")
            who = login.payload_of(token).get("preferred_username")
            human = self._human().strip()
            if not human or not isinstance(who, str) or who.casefold() != human.casefold():
                raise WrongHuman("the sandbox's login is not the responsible human's")
            return token

    def nkey(self) -> NKey:
        kp = self._kp
        if kp is None:
            raise LoginError("the login in the sandbox has not connected yet")

        def sign(nonce: bytes) -> bytes:
            return bytes(kp.sign(nonce))

        return NKey(public_key=bytes(kp.public_key).decode("ascii"), sign=sign)

    def claim(self) -> object | None:
        """The one login channel's slot, taken before its handshake is answered;
        None while another holds it."""
        if self._claim is not None:
            return None
        self._claim = object()
        return self._claim

    def release(self, claim: object) -> None:
        if self._claim is claim:
            self._claim = None

    def busy(self) -> bool:
        return self._claim is not None

    async def serve(self, claim: object, read_line: Callable[[], Awaitable[bytes]],
                    send_line: Callable[[bytes], Awaitable[None]]) -> None:
        """One login channel, after its slot was claimed and the handshake answered;
        returns when it closes."""
        lock = asyncio.Lock()

        async def send(body: dict[str, Any]) -> None:
            async with lock:
                await send_line(json.dumps(body).encode() + b"\n")

        self._send = send
        self._ready.set()
        log.info("login: the sandbox's login channel connected")
        try:
            while True:
                try:
                    line = await read_line()
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001 - closed or garbled: the channel ends
                    return
                if isinstance(msg, dict):
                    await self._on(msg)
        finally:
            if self._send is send:
                self._send = None
                self._ready.clear()
                for fut in self._waiters.values():
                    if not fut.done():
                        fut.set_exception(_Closed())
                # The relay is gone mid-approval: its ask is settled, its code dropped.
                self._deferred = None
                if self._approval_open:
                    self._close_approval("failed")
            self.release(claim)
            log.info("login: the sandbox's login channel closed")

    async def _on(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        if type(rid) is int:
            fut = self._waiters.get(rid)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        event = msg.get("event")
        if event == "key":
            if self._kp is None:  # the first key stays for the session
                seed = msg.get("seed")
                try:
                    self._kp = nkeys.from_seed(str(seed).encode("ascii"))
                except Exception:  # noqa: BLE001 - never the value in the log
                    log.warning("login: the sandbox sent no usable nkey")
            return
        if self._notifier is None:
            return
        if event == "waiting":
            grant = self._grant(msg)
            if grant is not None:
                self._offer_approval(grant)
        elif event == "settled" and msg.get("outcome") in OUTCOMES:
            if self._approval_open:
                self._close_approval(str(msg["outcome"]))
            else:
                self._deferred = None  # it ended before its DM went out
        elif event == "refused":
            if self._last_refused is not None and self._now() - self._last_refused < login.COOLDOWN_SEC:
                log.info("login: another refusal within the cooldown; no DM")
                return
            self._last_refused = self._now()
            self._spawn(self._notifier.refused(), "the human was not told")

    def _grant(self, msg: dict[str, Any]) -> DeviceGrant | None:
        """The approval to DM: only the user code comes from the sandbox (a strict
        pattern); the link is built here from the configured issuer, so no text
        of the sandbox's reaches the DM."""
        code, expires = msg.get("code"), msg.get("expires_in")
        if not isinstance(code, str) or not USER_CODE.fullmatch(code):
            self._warn("code", "login: the sandbox's user code is not one; no DM")
            return None
        uri = f"{self.settings.issuer}/device"
        minutes = expires if type(expires) is int and 0 < expires <= 3600 else 600
        return DeviceGrant(verification_uri=uri, verification_uri_complete=f"{uri}?user_code={code}",
                           user_code=code, expires_in=minutes)

    def _warn(self, kind: str, text: str) -> None:
        """A warning the sandbox can trigger at will: once per cooldown, else debug."""
        now, last = self._now(), self._warned.get(kind)
        if last is not None and now - last < login.COOLDOWN_SEC:
            log.debug(text)
            return
        self._warned[kind] = now
        log.warning(text)

    def _offer_approval(self, grant: DeviceGrant) -> None:
        """One open approval at a time (an open one ends at its expiry), and at most
        one DM per cooldown: a newer one within it waits until the cooldown ends."""
        now = self._now()
        if self._approval_open and now >= self._approval_until:
            self._close_approval("expired")
        if self._approval_open:
            self._warn("open", "login: an approval is already open; no second DM")
            return
        wait = 0.0 if self._last_waiting is None else self._last_waiting + login.COOLDOWN_SEC - now
        if wait > 0:
            first = self._deferred is None
            self._deferred = (grant, now)  # the newest code wins
            if first:
                log.info("login: another approval within the cooldown; its DM follows in %d s", int(wait) + 1)
                self._spawn(self._later(wait), "the deferred DM was not sent")
            return
        self._deferred = None
        self._approval_open = True
        self._approval_id += 1
        self._last_waiting = now
        self._approval_until = now + grant.expires_in
        self._spawn(self._notifier.waiting(grant), "the DM with the approval was not sent")
        self._spawn(self._expire(self._approval_id, grant.expires_in), "the expired ask was not settled")

    async def _later(self, wait: float) -> None:
        await self._sleep(wait)
        item, self._deferred = self._deferred, None
        if item is None:
            return
        grant, at = item
        left = grant.expires_in - int(self._now() - at)
        if left > 0:
            self._offer_approval(dataclasses.replace(grant, expires_in=left))

    async def _expire(self, approval: int, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self._approval_open and self._approval_id == approval:
            self._close_approval("expired")

    def _close_approval(self, outcome: str) -> None:
        self._approval_open = False
        self._spawn(self._notifier.settled(outcome), "the ask was not settled")

    def _spawn(self, coro: Awaitable[None], what: str) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception as exc:  # noqa: BLE001
                log.warning("login: %s (%s)", what, type(exc).__name__)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def clear_old_host_store(settings: Settings, human: str) -> list[str]:
    """The host process of a keyless session, once: an earlier version kept the
    refresh token, its cooldown and the nkey seed in the host's state directory;
    the relay's login holds them now. They move to this host's own store
    (keystore.default(), which the plain servers on this host use, so they keep
    the token and key), each under its lock (keystore.migrate_from), and the old
    file is emptied. Nothing when that store is the very directory. The names
    moved or cleared."""
    old = keystore.HostFile(session_file.host_state_dir() / "login")
    chosen = keystore.default()
    if isinstance(chosen, keystore.HostFile) and chosen.directory.resolve() == old.directory.resolve():
        return []
    names = [keystore.NKEY_SEED]
    if human:
        names[:0] = [keystore.refresh_entry(settings.issuer, settings.client_id, human),
                     keystore.cooldown_entry(settings.issuer, settings.client_id, human)]
    moved = keystore.migrate_from(old, chosen, names)
    if moved:
        log.info("login: %d entries of the earlier host store moved to this host's store", len(moved))
    return moved


# --- the relay in the sandbox ---------------------------------------------------------

class ToHost:
    """Tells the host process about an approval, for its DM; never the device code."""

    def __init__(self) -> None:
        self.writer: asyncio.StreamWriter | None = None

    async def _send(self, body: dict[str, Any]) -> None:
        writer = self.writer
        if writer is None:
            raise NotifyError("no login channel to the host process")
        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()

    async def start(self) -> None:
        return None

    async def waiting(self, grant: DeviceGrant) -> None:
        await self._send({"event": "waiting", "uri": grant.verification_uri,
                          "complete": grant.verification_uri_complete, "code": grant.user_code,
                          "expires_in": grant.expires_in})

    async def settled(self, outcome: str) -> None:
        await self._send({"event": "settled", "outcome": outcome})

    async def refused(self) -> None:
        await self._send({"event": "refused"})


Connect = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _connect(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(asyncio.open_connection(host, port, limit=LINE_MAX), CONNECT_SEC)


class Holder:
    """The relay's login: this process's `login.Login` on `login.default_store()`
    (under corral the sandbox's home), and the channel that serves the host."""

    def __init__(self, addr: tuple[str, int], key: str, settings: Settings, human: str, *,
                 store: Any = None, shown: Any = None, connect: Connect = _connect,
                 retry_sec: float = RETRY_SEC) -> None:
        from .login_notice import Combined, Shown
        self._addr = addr
        self._key = key
        self._connect = connect
        self._retry = retry_sec
        self.to_host = ToHost()
        self.login = login.Login(settings, store=store if store is not None else login.default_store(),
                                 human=lambda: human,
                                 notifier=Combined(shown if shown is not None else Shown(settings.issuer),
                                                   self.to_host))

    def __repr__(self) -> str:
        return f"Holder({self._addr[0]}:{self._addr[1]})"

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            reader, writer = await self._connect(*self._addr)
        except (OSError, TimeoutError):
            return None
        try:
            writer.write(json.dumps({"key": self._key, "login": True}).encode() + b"\n")
            await writer.drain()
            answer = json.loads(await asyncio.wait_for(reader.readline(), CONNECT_SEC))
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                why = answer.get("error") if isinstance(answer, dict) else None
                log.info("login: the host refused the login channel (%s)", why or "no reason")
                raise ValueError
            writer.write(json.dumps({"event": "key", "seed": self.login.export_seed()}).encode() + b"\n")
            await writer.drain()
        except (OSError, ValueError, TimeoutError):
            writer.close()
            return None
        return reader, writer

    async def _answer(self, writer: asyncio.StreamWriter, rid: int) -> None:
        try:
            body: dict[str, Any] = {"id": rid, "token": await self.login.access_token()}
        except LoginError as exc:
            kind = next((k for k, cls in ERROR_KINDS.items() if isinstance(exc, cls)), "login")
            body = {"id": rid, "error": str(exc), "kind": kind}
        except Exception as exc:  # noqa: BLE001
            body = {"id": rid, "error": f"the login failed ({type(exc).__name__})"}
        with contextlib.suppress(OSError):
            writer.write(json.dumps(body).encode() + b"\n")
            await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks: set[asyncio.Task[None]] = set()
        try:
            while line := await reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    break
                if isinstance(msg, dict) and msg.get("op") == "token" and type(msg.get("id")) is int:
                    task = asyncio.create_task(self._answer(writer, msg["id"]))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except (OSError, ValueError):
            pass
        finally:
            self.to_host.writer = None
            writer.close()

    async def run(self, *, once: bool = False) -> None:
        """Connect, log in (the device approval on the first start), serve the host;
        connect again when the channel closes. Never raises."""
        conn = await self._open()
        running = asyncio.create_task(self.login.run())  # the approval starts now, channel or not
        delay = self._retry
        try:
            while True:
                if conn is not None:
                    delay = self._retry
                    reader, writer = conn
                    self.to_host.writer = writer
                    await self._serve(reader, writer)
                if once:
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_SEC)
                conn = await self._open()
        finally:
            if once:
                await running


def hold(addr: tuple[str, int], key: str, value: object) -> threading.Thread | None:
    """Start the relay's login in a thread of its own; None when the host offered none."""
    found = settings_of(value)
    if found is None:
        return None
    settings, human = found

    def main() -> None:
        try:
            asyncio.run(Holder(addr, key, settings, human).run())
        except Exception as exc:  # noqa: BLE001 - the relay goes on without its login
            log.warning("login: the relay's login ended (%s)", type(exc).__name__)

    thread = threading.Thread(target=main, name="login-holder", daemon=True)
    thread.start()
    return thread
