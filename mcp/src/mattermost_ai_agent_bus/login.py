"""The host's Keycloak login and its NATS user key (RT-6.34, ADR-014 points 6, 7, 9).

A building block for the NATS adapter that follows; nothing uses it yet, and
without `MM_LOGIN_ISSUER` and `MM_LOGIN_CLIENT_ID` it is off and changes
nothing. It delivers

* `access_token()`: a Keycloak access token with at least 60 s left, from the
  OAuth 2.0 device authorization grant (RFC 8628) on the first login and the
  refresh token grant afterwards. The client is public, no secret. Each
  refresh token is used once: the new one from every response is stored as
  soon as the token's issuer, client and person have been checked, and never
  sent again (Keycloak's reuse detection is off by decision, ADR-014 point 7).
* `nkey()`: the host's NATS user key, its public key and a `sign(nonce)`; the
  seed stays here (the login service's auth callout, services/auth/README.md,
  checks the signature over the server's nonce).

The MCP server the plugin starts over stdio logs in itself, with and without
corral (the one operating model, #154, the ADR-014 amendment of 2026-11-26): its
store is `keystore.default()` (the system keychain, else a 0600 file; under
corral that file is in the sandbox's own home). In a keyless corral session
(RT-6.18) that server is the relay in the sandbox: it holds the login, and the
host process asks it for access tokens over a login channel (login_relay.py).

After every approval and every refresh the token's issuer and client are
checked and its `preferred_username` must be the session's responsible human
(`owners.responsible_human`): whoever opened the approval link and logged in
becomes the owner, so another person's refresh token is revoked in Keycloak,
dropped here and reported. The refresh token is stored per (issuer, client,
human) under a file lock, so sessions of one OS user take turns. The host does not check the token's
signature; the login service does.

The approval link goes to the human at this machine (a browser, else the address
and code on this server's stderr) and to the responsible human as a direct
message (`login_notice`), never into a channel; never the device code. Secrets (tokens, seed) are never in a
log line, an exception, the environment or an argument vector; every object
here hides them in its repr.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import SplitResult, urlsplit

import httpx
import nkeys

from . import keystore, owners, session_file
from .config import env
from .sandbox import corral_sandbox

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)
_T = TypeVar("_T")

SCOPE = "offline_access"  # no ID token (ADR-014); the role of the same name is checked on every token
ROLE = "offline_access"
MIN_LEFT_SEC = 60  # an access token handed out has at least this long to live
SLOW_DOWN_SEC = 5  # RFC 8628 3.5
DEFAULT_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 20.0
MAX_BACKOFF_SEC = 60  # growing wait after a network error or a 5xx while polling
COOLDOWN_SEC = 60  # after any approval that failed, no new approval for this long
MAX_TIME_SEC = 2 ** 34  # a token's times lie below this (the year 2514), far below what a float holds
MAX_LIFETIME_SEC = 86_400  # a token is held at most this long, whatever its times say
MAX_CAUSE_CHARS = 120  # the error text a cooldown reports, on one line
# The reasons a cooldown may carry. A mark is a file, so its reason is data, and only
# these are ever reported: anything else says "failed".
COOLDOWN_WHY = ("was declined", "expired", "was refused", "lacked the realm role", "failed")
LOCK_POLL_SEC = 0.2
NKEY_ENV = "MM_LOGIN_NKEY"
NKEY_STORES = ("ephemeral", "host")


class LoginError(Exception):
    """The login failed. Messages never carry a token or a seed."""


class ConfigError(LoginError):
    """MM_LOGIN_* is set wrongly."""


class ApprovalDenied(LoginError):
    """The human declined the device approval."""


class ApprovalExpired(LoginError):
    """The device code ran out before it was approved."""


class NoRole(LoginError):
    """The access token does not carry the realm role the NATS login needs."""


class WrongHuman(LoginError):
    """The approving person is not the session's responsible human; the tokens are dropped."""


class NotifyError(LoginError):
    """The approval link could not be delivered to the human."""


class _Unreachable(LoginError):
    """Keycloak did not answer (network error); a poll tries again."""


@dataclass(frozen=True)
class Settings:
    issuer: str
    client_id: str
    nkey_store: str = "ephemeral"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(url: str, what: str) -> str:
    """https, or http on a loopback host only; no credentials, no fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise ConfigError(f"{what} is not a URL") from None
    if parts.scheme == "https" and host:
        pass
    elif parts.scheme == "http" and _loopback(host):
        pass
    else:
        raise ConfigError(f"{what} must be https (http only on a loopback host)")
    if parts.username or parts.password or parts.fragment:
        raise ConfigError(f"{what} must not carry credentials or a fragment")
    return url


def _port(parts: SplitResult) -> int | None:
    try:
        return parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    except ValueError:
        return None


def same_site(issuer: str, url: str) -> bool:
    """`url` has the issuer's scheme, host and port (so http only when the
    issuer itself is on loopback)."""
    try:
        mine, theirs = urlsplit(issuer), urlsplit(url)
    except ValueError:
        return False
    return (theirs.scheme == mine.scheme and (theirs.hostname or "") == (mine.hostname or "")
            and _port(mine) is not None and _port(theirs) == _port(mine))


class Deferred:
    """A store chosen on its first use, so that the choice (a keychain probe runs a
    subprocess) happens in the thread the first call runs in, never on the event loop."""

    def __init__(self, pick: Callable[[], keystore.KeyStore]) -> None:
        self._pick = pick
        self._store: keystore.KeyStore | None = None
        self._guard = threading.Lock()

    def __repr__(self) -> str:
        return f"Deferred({self._store!r})"

    def resolve(self) -> keystore.KeyStore:
        with self._guard:
            if self._store is None:
                self._store = self._pick()
            return self._store

    def load(self, name: str) -> str | None:
        return self.resolve().load(name)

    def save(self, name: str, value: str) -> None:
        self.resolve().save(name, value)

    def delete(self, name: str) -> None:
        self.resolve().delete(name)

    def try_lock(self, name: str) -> object | None:
        return self.resolve().try_lock(name)

    def unlock(self, handle: object) -> None:
        self.resolve().unlock(handle)


def default_store() -> keystore.KeyStore:
    """The store this process keeps the refresh token in: `keystore.default()`
    (the system keychain where there is one, else the 0600 file; under corral in
    the sandbox's own home, #154), chosen on first use off the event loop."""
    return Deferred(keystore.default)


def settings_from_env() -> Settings | None:
    """The settings, or None when the login is off. Both variables unset: off,
    silently. One without the other, or a wrong value: off, with the reason in
    the log."""
    issuer, client = env("MM_LOGIN_ISSUER").strip(), env("MM_LOGIN_CLIENT_ID").strip()
    if not issuer and not client:
        return None
    try:
        if not issuer or not client:
            raise ConfigError("MM_LOGIN_ISSUER and MM_LOGIN_CLIENT_ID must both be set")
        check_url(issuer, "MM_LOGIN_ISSUER")
        store = env(NKEY_ENV, "ephemeral").strip().lower()
        if store not in NKEY_STORES:
            raise ConfigError(f"{NKEY_ENV} must be one of: {', '.join(NKEY_STORES)}")
    except ConfigError as exc:
        log.error("login is off: %s", exc)
        return None
    return Settings(issuer=issuer.rstrip("/"), client_id=client, nkey_store=store)


@dataclass(frozen=True, repr=False)
class DeviceGrant:
    """What the human needs to approve: link and code, no secret."""
    verification_uri: str
    verification_uri_complete: str
    user_code: str
    expires_in: int

    def __repr__(self) -> str:
        return f"DeviceGrant(expires_in={self.expires_in})"


class Notifier(Protocol):
    """Tells the responsible human; the DM and ask texts are in login_notice."""

    async def start(self) -> None:
        """At the start of the login: settle an ask an earlier session left open."""
        ...

    async def waiting(self, grant: DeviceGrant) -> None:
        """The approval link and code to the human, in a DM only; the ask in the home. Raises NotifyError."""
        ...

    async def settled(self, outcome: str) -> None:
        """The approval ended: approved, denied, expired, refused (another person), failed."""
        ...

    async def refused(self) -> None:
        """Another person than the human approved: tell the human, no token content."""
        ...


@dataclass(frozen=True)
class NKey:
    public_key: str
    sign: Callable[[bytes], bytes] = field(repr=False)

    def __repr__(self) -> str:
        return f"NKey(public_key={self.public_key!r})"


@dataclass(repr=False)
class _Tokens:
    access: str = ""
    expires_at: float = 0.0  # monotonic clock
    # The same end on this host's wall clock: the monotonic one stands still while the
    # machine sleeps. Time received plus the token's lifetime, never its exp as such,
    # since this host's clock may run apart from the issuer's (RT-6.34 step 3).
    wall_until: float = 0.0

    def __repr__(self) -> str:
        return "_Tokens(<hidden>)"


def payload_of(token: str) -> dict[str, Any]:
    """A JWT's payload, unverified (the login service verifies the token)."""
    try:
        seg = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    except (IndexError, ValueError, binascii.Error):
        raise LoginError("the access token is not a JWT") from None
    if not isinstance(data, dict):
        raise LoginError("the access token's payload is not an object")
    return data


class Login:
    def __init__(self, settings: Settings, *, store: keystore.KeyStore | None = None,
                 nkey_store: keystore.KeyStore | None = None,
                 notifier: Notifier | None = None,
                 human: Callable[[], str] = owners.responsible_human,
                 http: httpx.AsyncClient | None = None,
                 now: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 migrate: bool = False) -> None:
        self.settings = settings
        # keystore.default() is chosen on first use, which is off the event loop (_io).
        self._store: keystore.KeyStore = store if store is not None else Deferred(keystore.default)
        self._migrate = migrate
        if nkey_store is None:
            nkey_store = self._store if settings.nkey_store == "host" else keystore.Ephemeral()
        self._nkey_store = nkey_store
        self._notifier = notifier
        self._human = human
        self._http = http
        self._now = now
        self._wall = wall
        self._sleep = sleep
        self._tokens = _Tokens()
        self._endpoints: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self._cooldown_why = ""
        self._cooldown_cause = ""  # the error behind this object's own "failed" cooldown
        self._mark_seen = 0.0  # the stored deadline this object has taken in, 0 for none
        self._mark_until = 0.0  # ... and the effective (capped) deadline that gave
        self._nkey: nkeys.KeyPair | None = None
        self._nkey_guard = threading.Lock()

    def __repr__(self) -> str:
        return (f"Login(issuer={self.settings.issuer!r}, client_id={self.settings.client_id!r}, "
                f"token={'held' if self._tokens.access else 'none'})")

    # --- the user nkey --------------------------------------------------------
    def nkey(self) -> NKey:
        """The host's NATS user key: loaded from its store (`MM_LOGIN_NKEY=host`)
        or new for this process. The seed stays here."""
        with self._nkey_guard:  # one key, whichever thread asks first (export_seed, _io)
            if self._nkey is None:
                seed: str | None = None
                try:
                    seed = self._nkey_store.load(keystore.NKEY_SEED)
                except (keystore.KeyStoreError, OSError) as exc:
                    log.warning("stored nkey not used: %s", type(exc).__name__ if isinstance(exc, OSError) else exc)
                kp = None
                if seed:
                    try:
                        kp = nkeys.from_seed(seed.encode("ascii"))
                    except (nkeys.NkeysError, UnicodeEncodeError, ValueError):
                        log.warning("stored nkey seed is not valid: a new one is made")
                if kp is None:
                    new_seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER)
                    kp = nkeys.from_seed(new_seed)
                    try:
                        self._nkey_store.save(keystore.NKEY_SEED, bytes(new_seed).decode("ascii"))
                    except (keystore.KeyStoreError, OSError) as exc:
                        log.warning("nkey seed not stored: %s", type(exc).__name__)
                self._nkey = kp
        kp = self._nkey

        def sign(nonce: bytes) -> bytes:
            return bytes(kp.sign(nonce))

        return NKey(public_key=bytes(kp.public_key).decode("ascii"), sign=sign)

    def export_seed(self) -> str:
        """The nkey's seed, for the login channel to the host process of a keyless
        corral session only (login_relay.py), which signs the NATS nonce there.
        Never logged, never in an exception."""
        self.nkey()
        assert self._nkey is not None
        return bytes(self._nkey.seed).decode("ascii")

    # --- the access token -----------------------------------------------------
    async def access_token(self) -> str:
        """An access token with at least 60 s left: the held one, a refresh,
        or (the first time, or after invalid_grant) a new device approval.
        Callers wait for the one running approval and share its result."""
        async with self._lock:
            if self._fresh():
                return self._tokens.access
            self._tokens = _Tokens()
            human = self._human().strip()
            if self._migrate:  # first, so a stored seed moves rather than being replaced
                self._migrate = False
                await self._io(self._migrate_old, human)
            await self._io(self.nkey)  # loaded off the event loop: a keychain call blocks
            if not human:
                raise LoginError("no responsible human to approve a login "
                                 "(set MM_RESPONSIBLE_HUMAN or MM_TRUSTED_HUMANS)")
            entry = keystore.refresh_entry(self.settings.issuer, self.settings.client_id, human)
            async with self._file_lock(entry):
                # The mark on the host, whoever left it: a cooldown another process set
                # while this one was running counts too, so it is read before every
                # decision to start an approval, not once per object (measured in the
                # lab: three Keycloak links in two minutes for what the human saw as one
                # agent).
                await self._io(self._load_cooldown, human)
                # Loaded only now: another process may have refreshed meanwhile,
                # and its stored token is the one to use, never the old one.
                refresh = await self._io(self._stored_refresh, human)
                if refresh:
                    try:
                        await self._refresh(refresh, human)
                        await self._io(self._clear_cooldown, human)
                        return self._tokens.access
                    except _InvalidGrant:
                        log.info("login: the refresh token is no longer valid, asking for a new approval")
                        await self._io(self._drop_refresh, human)
                wait = self._cooldown_until - self._now()
                if wait > 0:
                    # The first error, where this process saw it: "failed" alone hides
                    # whether Keycloak was down or no DM could go out.
                    cause = f" ({self._cooldown_cause})" if self._cooldown_cause else ""
                    raise LoginError(f"the last approval {self._cooldown_why}{cause}; "
                                     f"a new one starts in {int(wait) + 1} s")
                try:
                    await self._approve(human)
                except LoginError as exc:
                    # Any failure, not only a denial: else every call would
                    # send a new DM and leave a new session open in Keycloak.
                    if self._cooldown_until <= self._now():
                        if isinstance(exc, NoRole):
                            await self._io(self._cool_down, "lacked the realm role", human)
                        else:
                            await self._io(self._cool_down, "failed", human, str(exc))
                    raise
                await self._io(self._clear_cooldown, human)
                return self._tokens.access

    def _fresh(self) -> bool:
        t = self._tokens
        return bool(t.access) and t.expires_at - self._now() >= MIN_LEFT_SEC \
            and t.wall_until - self._wall() >= MIN_LEFT_SEC

    @contextlib.asynccontextmanager
    async def _file_lock(self, entry: str) -> AsyncIterator[None]:
        """The store's lock for this token's entry (flock for a HostFile), taken
        without blocking the event loop. Without a lock, the login goes on."""
        handle: object | None = None
        try:
            while (handle := await self._io(self._store.try_lock, entry)) is None:
                await asyncio.sleep(LOCK_POLL_SEC)
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("login: the store's lock is not taken (%s)", type(exc).__name__)
        try:
            yield
        finally:
            if handle is not None:
                try:
                    await self._io(self._store.unlock, handle)
                except OSError:
                    pass

    @staticmethod
    async def _io(fn: Callable[..., _T], *args: Any) -> _T:
        """A store call off the event loop: a keychain answers through a
        subprocess, and the MCP handshake must not wait for it."""
        return await asyncio.to_thread(fn, *args)

    def _migrate_old(self, human: str) -> None:
        """Once, under each entry's lock (migrate_from's), outside corral only: the token, its cooldown and the
        nkey seed an earlier version kept in the host's state directory move to this
        store (keystore.migrate_from). Under corral that directory is not reachable."""
        old = keystore.HostFile(session_file.host_state_dir() / "login")
        names = [keystore.NKEY_SEED]
        if human:
            names[:0] = [self._entry(human),
                         keystore.cooldown_entry(self.settings.issuer, self.settings.client_id, human)]
        try:
            # The store itself, never the Deferred wrapper: migrate_from leaves a store
            # at the old place alone only when it sees what it is (else it wipes it).
            new = self._store.resolve() if isinstance(self._store, Deferred) else self._store
            keystore.migrate_from(old, new, names)
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("login: the earlier store was not taken over (%s)", type(exc).__name__)

    async def forget(self) -> None:
        """Drop every token, in memory and in the store (off the event loop)."""
        self._tokens = _Tokens()
        await self._io(self._drop_refresh, self._human().strip())

    def _entry(self, human: str) -> str:
        return keystore.refresh_entry(self.settings.issuer, self.settings.client_id, human)

    def _stored_refresh(self, human: str) -> str | None:
        """The refresh token stored for (issuer, client, human); a file whose
        recorded three values differ is treated as missing."""
        try:
            raw = self._store.load(self._entry(human))
            if not raw:
                return None
            data = json.loads(raw)
        except keystore.KeyStoreError as exc:
            log.warning("stored refresh token not used: %s", exc)
            return None
        except OSError as exc:  # the store's choice or a keychain call failed
            log.warning("stored refresh token not used: %s", type(exc).__name__)
            return None
        except ValueError:
            log.warning("stored refresh token not used: not readable")
            return None
        if (not isinstance(data, dict) or data.get("issuer") != self.settings.issuer
                or data.get("client_id") != self.settings.client_id
                or str(data.get("human", "")).casefold() != human.casefold()):
            log.warning("stored refresh token not used: it belongs to another issuer, client or person")
            return None
        token = data.get("token")
        return token if isinstance(token, str) and token else None

    def _store_refresh(self, token: str, human: str) -> None:
        value = json.dumps({"issuer": self.settings.issuer, "client_id": self.settings.client_id,
                            "human": human.casefold(), "token": token})
        try:
            self._store.save(self._entry(human), value)
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("refresh token not stored: %s", type(exc).__name__)

    def _drop_refresh(self, human: str) -> None:
        if not human:
            return
        try:
            self._store.delete(self._entry(human))
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("stored refresh token not removed: %s", type(exc).__name__)

    # --- Keycloak ---------------------------------------------------------------
    async def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        try:
            if self._http is not None:
                return await self._http.post(url, data=data)
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
                return await client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise _Unreachable(f"Keycloak not reachable ({type(exc).__name__})") from None

    async def _discover(self) -> dict[str, str]:
        if self._endpoints is None:
            url = f"{self.settings.issuer}/.well-known/openid-configuration"
            try:
                if self._http is not None:
                    resp = await self._http.get(url)
                else:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC) as client:
                        resp = await client.get(url)
                resp.raise_for_status()
                doc = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise LoginError(f"Keycloak's discovery document not read ({type(exc).__name__})") from None
            if not isinstance(doc, dict) or str(doc.get("issuer", "")).rstrip("/") != self.settings.issuer:
                raise LoginError("Keycloak's discovery document names another issuer")
            found: dict[str, str] = {}
            for key in ("device_authorization_endpoint", "token_endpoint", "revocation_endpoint"):
                value = doc.get(key)
                if value is None and key == "revocation_endpoint":
                    continue  # optional: without it a wrong person's token is only dropped here
                if not isinstance(value, str):
                    raise LoginError(f"Keycloak's discovery document has no {key}")
                found[key] = self._same_site(check_url(value, key), key)
            self._endpoints = found
        return self._endpoints

    def _same_site(self, url: str, what: str) -> str:
        """The URL has the issuer's scheme, host and port (same_site)."""
        if not same_site(self.settings.issuer, url):
            raise LoginError(f"{what} is not on the issuer's host")
        return url

    async def _approve(self, human: str) -> None:
        if self._notifier is None:
            raise NotifyError("no way to tell the human about the approval")
        endpoints = await self._discover()
        resp = await self._post(endpoints["device_authorization_endpoint"],
                                {"client_id": self.settings.client_id, "scope": SCOPE})
        body = _json(resp)
        if resp.status_code != 200 or not isinstance(body.get("device_code"), str):
            raise LoginError(f"the device authorization was refused ({_error_of(body, resp)})")
        device_code = str(body["device_code"])
        uri, complete = str(body.get("verification_uri", "")), str(body.get("verification_uri_complete", ""))
        try:
            self._same_site(check_url(uri, "verification_uri"), "verification_uri")
            if complete:
                self._same_site(check_url(complete, "verification_uri_complete"), "verification_uri_complete")
        except ConfigError as exc:
            raise LoginError(f"the approval link is not usable ({exc})") from None
        grant = DeviceGrant(
            verification_uri=uri, verification_uri_complete=complete,
            user_code=str(body.get("user_code", "")),
            expires_in=_int(body.get("expires_in"), 600))
        interval = max(1, _int(body.get("interval"), DEFAULT_INTERVAL_SEC))
        deadline = self._now() + grant.expires_in
        outcome = "failed"
        try:
            try:
                await self._notifier.waiting(grant)
            except NotifyError:
                raise
            except Exception as exc:  # noqa: BLE001 - nothing was delivered: fail closed
                raise NotifyError(f"the approval link was not delivered ({type(exc).__name__})") from None
            outcome = await self._poll(endpoints["token_endpoint"], device_code, interval, deadline, human)
        except ApprovalDenied:
            outcome = "denied"
            await self._io(self._cool_down, "was declined", human)
            raise
        except ApprovalExpired:
            outcome = "expired"
            await self._io(self._cool_down, "expired", human)
            raise
        except WrongHuman:
            outcome = "refused"
            await self._io(self._cool_down, "was refused", human)
            raise
        finally:
            await self._settle(outcome)

    def _cool_down(self, why: str, human: str = "", cause: str = "") -> None:
        """`cause` is the error's own text, kept in this object only: the mark on the
        host carries one of the fixed reasons, never a text."""
        self._cooldown_until = self._now() + COOLDOWN_SEC
        self._cooldown_why = why
        self._cooldown_cause = " ".join(cause.split())[:MAX_CAUSE_CHARS]
        self._save_cooldown(human)

    def _load_cooldown(self, human: str) -> None:
        """The cooldown the host recorded, if it still runs, with the reason recorded.

        The deadline is a wall-clock one: the monotonic clock stands still while the
        machine sleeps, and a stored one of those would mean nothing in the next
        process. A mark is taken in once - the raw deadline and the capped one that
        gave - so a second read arms nothing, while a later mark, from another session,
        still wins over it.
        """
        if not human:
            return
        try:
            raw = self._store.load(keystore.cooldown_entry(
                self.settings.issuer, self.settings.client_id, human))
            if not raw:
                return
            until_text, _, why = raw.partition(" ")
            until = float(until_text)
        except (keystore.KeyStoreError, OSError, ValueError):
            return
        if not math.isfinite(until) or until == self._mark_seen:
            return  # no deadline, or the mark this object has already taken in
        # Nothing writes a mark further ahead than the cooldown itself, so a later
        # deadline is a wall clock that moved backwards: it must not hold approvals off
        # for the length of the jump, since nothing would clear the mark before then.
        # The deadline is capped, not the remainder: capped per call, every call would
        # arm another COOLDOWN_SEC and the lock would last the whole jump.
        effective = min(until, self._wall() + COOLDOWN_SEC)
        if effective <= self._mark_until:
            return  # a mark this object already has, or one that ends no later
        self._mark_seen, self._mark_until = until, effective
        left = effective - self._wall()
        if left > 0:
            self._cooldown_until = self._now() + left
            self._cooldown_why = why if why in COOLDOWN_WHY else "failed"
            self._cooldown_cause = ""  # another process's failure: its error is not known here

    def _save_cooldown(self, human: str) -> None:
        """The mark beside the token: the deadline and why, so the next process says
        what happened and not only how long is left."""
        if not human:
            return
        until = self._wall() + COOLDOWN_SEC
        self._mark_seen = self._mark_until = until
        try:
            self._store.save(keystore.cooldown_entry(
                self.settings.issuer, self.settings.client_id, human),
                f"{until} {self._cooldown_why}")
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("login cooldown not stored: %s", type(exc).__name__)

    def _clear_cooldown(self, human: str) -> None:
        """A login that worked leaves no mark: the next process starts clean."""
        self._cooldown_until = 0.0
        self._cooldown_why = ""
        self._cooldown_cause = ""
        self._mark_seen = 0.0
        self._mark_until = 0.0
        if not human:
            return
        try:
            self._store.delete(keystore.cooldown_entry(
                self.settings.issuer, self.settings.client_id, human))
        except (keystore.KeyStoreError, OSError) as exc:
            log.warning("login cooldown not cleared: %s", type(exc).__name__)

    async def _settle(self, outcome: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.settled(outcome)
        except Exception as exc:  # noqa: BLE001 - the login's result stands
            log.warning("login: the ask was not settled (%s)", type(exc).__name__)

    async def _poll(self, token_url: str, device_code: str, interval: int, deadline: float,
                    human: str) -> str:
        delay = interval
        while True:
            await self._sleep(delay)
            if self._now() > deadline:
                raise ApprovalExpired("the device code expired")
            try:
                sent = (self._now(), self._wall())
                resp = await self._post(token_url, {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code, "client_id": self.settings.client_id})
            except _Unreachable as exc:
                log.info("login: %s; trying again", exc)
                delay = min(delay * 2, MAX_BACKOFF_SEC)
                continue
            if resp.status_code >= 500:
                log.info("login: Keycloak answered HTTP %s; trying again", resp.status_code)
                delay = min(delay * 2, MAX_BACKOFF_SEC)
                continue
            delay = interval
            body = _json(resp)
            if resp.status_code == 200:
                await self._accept(body, human, sent)
                return "approved"
            code = body.get("error")
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += SLOW_DOWN_SEC
                delay = interval
            elif code == "access_denied":
                raise ApprovalDenied("the approval was declined")
            elif code == "expired_token":
                raise ApprovalExpired("the device code expired")
            else:
                raise LoginError(f"the token request failed ({_error_of(body, resp)})")

    async def _refresh(self, refresh_token: str, human: str) -> None:
        endpoints = await self._discover()
        sent = (self._now(), self._wall())
        resp = await self._post(endpoints["token_endpoint"], {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.settings.client_id})
        body = _json(resp)
        if resp.status_code == 200:
            await self._accept(body, human, sent)
            return
        if body.get("error") == "invalid_grant":
            raise _InvalidGrant
        raise LoginError(f"the refresh failed ({_error_of(body, resp)})")

    async def _accept(self, body: dict[str, Any], human: str, sent: tuple[float, float]) -> None:
        """A token response: the token's issuer, client and person are checked
        first; only then is the new refresh token stored. `sent` is the monotonic
        and the wall time the request went out: the lifetime counts from there,
        never from the answer, so a slow answer only shortens it."""
        access, refresh = body.get("access_token"), body.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise LoginError("the token response has no access token")
        if not isinstance(refresh, str) or not refresh:
            raise LoginError("the token response has no refresh token (is the offline_access scope allowed?)")
        claims = payload_of(access)
        who = claims.get("preferred_username")
        if not human or not isinstance(who, str) or who.casefold() != human.casefold():
            await self._revoke(refresh)
            self._tokens = _Tokens()
            await self._io(self._drop_refresh, human)
            log.error("login refused: the approving person is not the responsible human; tokens dropped")
            if self._notifier is not None:
                try:
                    await self._notifier.refused()
                except Exception as exc:  # noqa: BLE001
                    log.warning("login: the human was not told (%s)", type(exc).__name__)
            raise WrongHuman("the approving person is not the responsible human")
        try:
            if str(claims.get("iss", "")).rstrip("/") != self.settings.issuer:
                raise LoginError("the access token comes from another issuer")
            if claims.get("azp") != self.settings.client_id:
                raise LoginError("the access token was issued to another client")
            access_info = claims.get("realm_access")
            roles = access_info.get("roles") if isinstance(access_info, dict) else None
            if not isinstance(roles, list) or ROLE not in roles:
                raise NoRole(f"the access token does not carry the realm role {ROLE}")
            lifetime = _lifetime(body, claims, sent[1])
        except LoginError:
            # Never stored, so never used: revoked, or its session stays open in Keycloak.
            await self._revoke(refresh)
            raise
        await self._io(self._store_refresh, refresh, human)
        self._tokens = _Tokens(access=access, expires_at=sent[0] + lifetime,
                               wall_until=sent[1] + lifetime)

    async def _revoke(self, refresh_token: str) -> None:
        """A refresh token that is not kept (another person's, or a token that
        failed its checks) is revoked in Keycloak, best effort."""
        try:
            url = (await self._discover()).get("revocation_endpoint")
            if not url:
                log.warning("login: Keycloak offers no revocation endpoint; the token is only dropped here")
                return
            resp = await self._post(url, {"token": refresh_token, "token_type_hint": "refresh_token",
                                          "client_id": self.settings.client_id})
            if resp.status_code != 200:
                log.warning("login: the dropped token was not revoked (HTTP %s)", resp.status_code)
        except LoginError as exc:
            log.warning("login: the dropped token was not revoked (%s)", exc)

    async def run(self) -> None:
        """Background task: log in once at start (a stored refresh token needs
        no approval); never raises, the session does not wait for it."""
        try:
            if self._notifier is not None:
                try:
                    await self._notifier.start()
                except Exception as exc:  # noqa: BLE001 - an orphaned ask stays open, no more
                    log.warning("login: an earlier ask was not settled (%s)", type(exc).__name__)
            await self.access_token()
            log.info("login: ready")
        except asyncio.CancelledError:
            raise
        except LoginError as exc:
            log.warning("login: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("login failed (%s)", type(exc).__name__)


class _InvalidGrant(Exception):
    pass


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_of(body: dict[str, Any], resp: httpx.Response) -> str:
    code = body.get("error")
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,39}", code):
        return code
    return f"HTTP {resp.status_code}"


def _time_claim(claims: dict[str, Any], name: str) -> float | None:
    """A token's `exp` or `iat`: absent is None; anything but a positive number below
    MAX_TIME_SEC is a LoginError. A NaN made no token fresh, and an integer too large
    for a float raised OverflowError after the token was stored; both need a hostile
    issuer."""
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < MAX_TIME_SEC:
        raise LoginError(f"the access token's {name} is not a usable time")
    return float(value)


def _lifetime(body: dict[str, Any], claims: dict[str, Any], wall: float) -> float:
    """How long a token lives from `wall`, the time its request went out, in seconds:
    the shorter of the response's `expires_in` and the token's own `exp - iat`, and
    never more than MAX_LIFETIME_SEC. Both are durations, so a host clock that runs
    ahead of the issuer's does not make every token stale (RT-6.34 step 3). Only a
    token without `iat` is measured against this host's clock; one with no lifetime
    at all gets 60 s, as before."""
    exp, iat = _time_claim(claims, "exp"), _time_claim(claims, "iat")
    spans: list[float] = []
    expires_in = body.get("expires_in")
    # A number, int or float (not a bool); NaN and infinity fail the range check.
    if isinstance(expires_in, int | float) and not isinstance(expires_in, bool) \
            and 0 < expires_in < MAX_TIME_SEC:
        spans.append(float(expires_in))
    if exp is not None and iat is not None:
        if exp <= iat:
            raise LoginError("the access token expires before it was issued")
        spans.append(exp - iat)
    elif exp is not None:
        spans.append(exp - wall)
    return min(*spans, MAX_LIFETIME_SEC) if spans else 60.0


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


# --- the running login: one per process, started by the server or the host process ---

class Running(Protocol):
    """What the adapter needs of the running login: this process's own (`Login`)
    or, in the host process of a keyless corral session, the sandbox's
    (`login_relay.RelayedLogin`)."""

    settings: Settings

    async def access_token(self) -> str: ...

    def nkey(self) -> NKey: ...

    async def run(self) -> None: ...


_current: Running | None = None
_task: asyncio.Task[None] | None = None


def current() -> Running | None:
    """The running login, for the adapter that needs a token or the nkey; None when off."""
    return _current


def start(get_client: Callable[[], Awaitable[ChatPort | None]]) -> asyncio.Task[None] | None:
    """Start the login in the background when configured; the session never
    waits for it. One per process: a second call returns the running task.
    None when the login is off. The human sees the approval at this machine
    (a browser, else stderr) and in a DM (#154)."""
    global _current, _task
    if _current is not None:
        return _task
    settings = settings_from_env()
    if settings is None:
        return None
    from .login_notice import Combined, DMNotifier, Shown
    notifier = Combined(Shown(settings.issuer), DMNotifier(get_client, owners.responsible_human))
    # Outside corral an earlier version's token and key move over once (#154);
    # under corral the host's state directory is not reachable.
    _current = Login(settings, store=default_store(), notifier=notifier, migrate=not corral_sandbox())
    _task = asyncio.create_task(_current.run())
    return _task


def start_relayed(get_client: Callable[[], Awaitable[ChatPort | None]]) -> asyncio.Task[None] | None:
    """The host process of a keyless corral session (RT-6.18): no login of its
    own and no store on the host; the relay in the sandbox holds the login and
    hands this process access tokens (login_relay.py, #154). This process still
    DMs the human the approval the sandbox shows. None when the login is off."""
    global _current, _task
    if _current is not None:
        return _task
    settings = settings_from_env()
    if settings is None:
        return None
    from .login_notice import DMNotifier
    from .login_relay import RelayedLogin
    _current = RelayedLogin(settings, notifier=DMNotifier(get_client, owners.responsible_human))
    _task = asyncio.create_task(_current.run())
    return _task


def stop() -> None:
    global _current, _task
    if _task is not None:
        _task.cancel()
    _current, _task = None, None
