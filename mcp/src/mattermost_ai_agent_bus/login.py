"""The host's Keycloak login and its NATS user key (RT-6.34, ADR-014 points 6, 7, 9).

A building block for the NATS adapter that follows; nothing uses it yet, and
without `MM_LOGIN_ISSUER` and `MM_LOGIN_CLIENT_ID` it is off and changes
nothing. It delivers

* `access_token()`: a Keycloak access token with at least 60 s left, from the
  OAuth 2.0 device authorization grant (RFC 8628) on the first login and the
  refresh token grant afterwards. The client is public, no secret. Each
  refresh token is used once: the new one from every response is stored at
  once (Keycloak's reuse detection is off by decision, ADR-014 point 7).
* `nkey()`: the host's NATS user key, its public key and a `sign(nonce)`; the
  seed stays here (the login service's auth callout, services/auth/README.md,
  checks the signature over the server's nonce).

After every approval and every refresh the token's `preferred_username` must
be the session's responsible human (`owners.responsible_human`): whoever
opened the approval link and logged in becomes the owner, so another person's
tokens are dropped and reported. The host does not check the token's
signature; the login service does.

The approval link goes only to the responsible human, as a direct message
(`login_notice`), never into a channel. Secrets (tokens, seed) are never in a
log line, an exception, the environment or an argument vector; every object
here hides them in its repr.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx
import nkeys

from . import keystore, owners
from .config import env

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

SCOPE = "openid offline_access"
MIN_LEFT_SEC = 60  # an access token handed out has at least this long to live
SLOW_DOWN_SEC = 5  # RFC 8628 3.5
DEFAULT_INTERVAL_SEC = 5
HTTP_TIMEOUT_SEC = 20.0
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


class WrongHuman(LoginError):
    """The approving person is not the session's responsible human; the tokens are dropped."""


class NotifyError(LoginError):
    """The approval link could not be delivered to the human."""


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


@dataclass(frozen=True)
class DeviceGrant:
    """What the human needs to approve: link and code, no secret."""
    verification_uri: str
    verification_uri_complete: str
    user_code: str
    expires_in: int


class Notifier(Protocol):
    """Tells the responsible human; the DM and ask texts are in login_notice."""

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
    expires_at: float = 0.0

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
                 now: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.settings = settings
        self._store = store if store is not None else keystore.HostFile()
        if nkey_store is None:
            nkey_store = self._store if settings.nkey_store == "host" else keystore.Ephemeral()
        self._nkey_store = nkey_store
        self._notifier = notifier
        self._human = human
        self._http = http
        self._now = now
        self._sleep = sleep
        self._tokens = _Tokens()
        self._endpoints: dict[str, str] | None = None
        self._lock = asyncio.Lock()
        self._nkey: nkeys.KeyPair | None = None

    def __repr__(self) -> str:
        return (f"Login(issuer={self.settings.issuer!r}, client_id={self.settings.client_id!r}, "
                f"token={'held' if self._tokens.access else 'none'})")

    # --- the user nkey --------------------------------------------------------
    def nkey(self) -> NKey:
        """The host's NATS user key: loaded from its store (`MM_LOGIN_NKEY=host`)
        or new for this process. The seed stays here."""
        if self._nkey is None:
            seed: str | None = None
            try:
                seed = self._nkey_store.load(keystore.NKEY_SEED)
            except keystore.KeyStoreError as exc:
                log.warning("stored nkey not used: %s", exc)
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

    # --- the access token -----------------------------------------------------
    async def access_token(self) -> str:
        """An access token with at least 60 s left: the held one, a refresh,
        or (the first time, or after invalid_grant) a new device approval."""
        async with self._lock:
            if self._tokens.access and self._tokens.expires_at - self._now() >= MIN_LEFT_SEC:
                return self._tokens.access
            self._tokens = _Tokens()
            refresh = self._stored_refresh()
            if refresh:
                try:
                    await self._refresh(refresh)
                    return self._tokens.access
                except _InvalidGrant:
                    log.info("login: the refresh token is no longer valid, asking for a new approval")
                    self._drop_refresh()
            await self._approve()
            return self._tokens.access

    def forget(self) -> None:
        """Drop every token, in memory and in the store."""
        self._tokens = _Tokens()
        self._drop_refresh()

    def _stored_refresh(self) -> str | None:
        try:
            return self._store.load(keystore.REFRESH_TOKEN)
        except keystore.KeyStoreError as exc:
            log.warning("stored refresh token not used: %s", exc)
            return None

    def _drop_refresh(self) -> None:
        try:
            self._store.delete(keystore.REFRESH_TOKEN)
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
            raise LoginError(f"Keycloak not reachable ({type(exc).__name__})") from None

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
            found: dict[str, str] = {}
            for key in ("device_authorization_endpoint", "token_endpoint"):
                value = doc.get(key) if isinstance(doc, dict) else None
                if not isinstance(value, str):
                    raise LoginError(f"Keycloak's discovery document has no {key}")
                found[key] = check_url(value, key)
            self._endpoints = found
        return self._endpoints

    async def _approve(self) -> None:
        human = self._human().strip()
        if not human:
            raise LoginError("no responsible human to approve a login "
                             "(set MM_RESPONSIBLE_HUMAN or MM_TRUSTED_HUMANS)")
        if self._notifier is None:
            raise NotifyError("no way to tell the human about the approval")
        endpoints = await self._discover()
        resp = await self._post(endpoints["device_authorization_endpoint"],
                                {"client_id": self.settings.client_id, "scope": SCOPE})
        body = _json(resp)
        if resp.status_code != 200 or not isinstance(body.get("device_code"), str):
            raise LoginError(f"the device authorization was refused ({_error_of(body, resp)})")
        device_code = str(body["device_code"])
        grant = DeviceGrant(
            verification_uri=str(body.get("verification_uri", "")),
            verification_uri_complete=str(body.get("verification_uri_complete", "")),
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
            outcome = await self._poll(endpoints["token_endpoint"], device_code, interval, deadline)
        except ApprovalDenied:
            outcome = "denied"
            raise
        except ApprovalExpired:
            outcome = "expired"
            raise
        except WrongHuman:
            outcome = "refused"
            raise
        finally:
            await self._settle(outcome)

    async def _settle(self, outcome: str) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.settled(outcome)
        except Exception as exc:  # noqa: BLE001 - the login's result stands
            log.warning("login: the ask was not settled (%s)", type(exc).__name__)

    async def _poll(self, token_url: str, device_code: str, interval: int, deadline: float) -> str:
        while True:
            await self._sleep(interval)
            if self._now() > deadline:
                raise ApprovalExpired("the device code expired")
            resp = await self._post(token_url, {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code, "client_id": self.settings.client_id})
            body = _json(resp)
            if resp.status_code == 200:
                await self._accept(body)
                return "approved"
            code = body.get("error")
            if code == "authorization_pending":
                continue
            if code == "slow_down":
                interval += SLOW_DOWN_SEC
            elif code == "access_denied":
                raise ApprovalDenied("the approval was declined")
            elif code == "expired_token":
                raise ApprovalExpired("the device code expired")
            else:
                raise LoginError(f"the token request failed ({_error_of(body, resp)})")

    async def _refresh(self, refresh_token: str) -> None:
        endpoints = await self._discover()
        resp = await self._post(endpoints["token_endpoint"], {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": self.settings.client_id})
        body = _json(resp)
        if resp.status_code == 200:
            await self._accept(body)
            return
        if body.get("error") == "invalid_grant":
            raise _InvalidGrant
        raise LoginError(f"the refresh failed ({_error_of(body, resp)})")

    async def _accept(self, body: dict[str, Any]) -> None:
        """A token response: the new refresh token is stored first, then the
        access token's person is checked."""
        access, refresh = body.get("access_token"), body.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise LoginError("the token response has no access token")
        if isinstance(refresh, str) and refresh:
            try:
                self._store.save(keystore.REFRESH_TOKEN, refresh)
            except (keystore.KeyStoreError, OSError) as exc:
                log.warning("refresh token not stored: %s", type(exc).__name__)
        expected = self._human().strip()
        try:
            who = payload_of(access).get("preferred_username")
        except LoginError:
            self.forget()
            raise
        if not expected or not isinstance(who, str) or who.casefold() != expected.casefold():
            self.forget()
            log.error("login refused: the approving person is not the responsible human; tokens dropped")
            if self._notifier is not None:
                try:
                    await self._notifier.refused()
                except Exception as exc:  # noqa: BLE001
                    log.warning("login: the human was not told (%s)", type(exc).__name__)
            raise WrongHuman("the approving person is not the responsible human")
        self._tokens = _Tokens(access=access, expires_at=self._now() + _int(body.get("expires_in"), 60))

    async def run(self) -> None:
        """Background task: log in once at start (a stored refresh token needs
        no approval); never raises, the session does not wait for it."""
        try:
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
    return code if isinstance(code, str) and len(code) < 40 else f"HTTP {resp.status_code}"


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


# --- the running login: one per process, started by the host process or the server ---

_current: Login | None = None
_task: asyncio.Task[None] | None = None


def current() -> Login | None:
    """The running login, for the adapter that needs a token or the nkey; None when off."""
    return _current


def start(get_client: Callable[[], Awaitable[ChatPort | None]]) -> asyncio.Task[None] | None:
    """Start the login in the background when configured; the session never
    waits for it. One per process: a second call returns the running task.
    None when the login is off."""
    global _current, _task
    if _task is not None and not _task.done():
        return _task
    settings = settings_from_env()
    if settings is None:
        return None
    from .login_notice import DMNotifier
    store = keystore.HostFile()
    _current = Login(settings, store=store, notifier=DMNotifier(get_client, owners.responsible_human))
    _task = asyncio.create_task(_current.run())
    return _task


def stop() -> None:
    global _current, _task
    if _task is not None:
        _task.cancel()
    _current, _task = None, None
