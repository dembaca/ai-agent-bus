"""The operator's login (ADR-015 point 4): OpenID Connect, authorization
code with PKCE, a Keycloak client of the console's own — and, unlike the
mcp host's login (``login.py``), the console **verifies the token's
signature** against the realm's JWKS before it believes it (RISK-12).
Members of the configured operators group only.

Verifier rules as the login service's Go one (``services/auth/internal/
token``): exact issuer, audience and azp (the client's own), a required
exp with 30 s leeway, an alg allowlist (asymmetric only), a kid that
names a JWKS key, and the group claim checked against the configured
group. Verification is RS256 (the realm's ordinary keys).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import config

# the seams the tests drive: same bounds, runnable signatures
Fetch = Callable[[str], Awaitable[bytes]]
Post = Callable[[str, dict[str, str]], Awaitable[bytes]]

LEEWAY_SEC = 30.0
MAX_DOC = 1 << 19  # the discovery document, bounded (Go: 512 KiB-ish)
MAX_JWKS = 1 << 20  # the keys, bounded (Go: 1 MiB)
KEY_TTL_SEC = 3600.0
UNKNOWN_KID_RETRY_SEC = 60.0


class AuthError(Exception):
    """The path broke: discovery, token endpoint, jwks — 502 in the app."""


class Refused(AuthError):
    """A working path refused this login — 403, not a server fault."""


@dataclass(frozen=True)
class Operator:
    username: str
    display: str
    groups: tuple[str, ...]


@dataclass
class Endpoints:
    issuer: str
    authorization: str
    token: str
    jwks: str
    discovered_at: float = 0.0


async def _get(url: str, limit: int, fetch: Fetch | None) -> bytes:
    if fetch is not None:  # a test seam with the same bounds
        body = await fetch(url)
        if len(body) > limit:
            raise AuthError("answer too big")
        return body
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        res = await client.get(url)
        if res.status_code != 200 or len(res.content) > limit:
            raise AuthError(f"{url}: no answer or too big")
        return res.content


async def discover(issuer: str, fetch: Fetch | None = None) -> Endpoints:
    """The issuer's own endpoints, checked: https (or http on loopback for
    a test), each answered by the issuer's own site."""
    body = await _get(f"{issuer.rstrip('/')}/.well-known/openid-configuration",
                      MAX_DOC, fetch)
    try:
        doc = json.loads(body)
    except ValueError as exc:
        raise AuthError(f"openid configuration unreadable: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("issuer") != issuer:
        raise AuthError("the issuer answers as another issuer")
    out = Endpoints(issuer=issuer,
                    authorization=_same(issuer, doc.get("authorization_endpoint")),
                    token=_same(issuer, doc.get("token_endpoint")),
                    jwks=_same(issuer, doc.get("jwks_uri")))
    out.discovered_at = time.monotonic()
    return out


def _same(issuer: str, value: object) -> str:
    if not isinstance(value, str):
        raise AuthError("no endpoint")
    split = urlsplit(value)
    root = urlsplit(issuer)
    if split.scheme not in ("https", "http") or not split.hostname:
        raise AuthError(f"endpoint {value!r} is not an http(s) URL")
    if split.scheme == "http" and split.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise AuthError(f"endpoint {value!r} is plain http off loopback")
    if (split.scheme, split.hostname, split.port) != (root.scheme, root.hostname, root.port):
        raise AuthError(f"endpoint {value!r} is another site than the issuer")
    return value


@dataclass
class Keyring:
    """The realm's JWKS keys, by kid: fetched with bounds (10 s, 1 MiB,
    no redirects), cached for an hour, fetched again on an unknown kid —
    at most once a minute."""
    _keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    _fetched_at: float = 0.0

    async def public_key(self, jwks_url: str, kid: str,
                      fetch: Fetch | None = None) -> dict[str, Any]:
        if self._fetched_at == 0.0 or (kid not in self._keys
                                       and self._fetched_at < time.monotonic() - UNKNOWN_KID_RETRY_SEC) \
                or time.monotonic() - self._fetched_at > KEY_TTL_SEC:
            await self._load(jwks_url, fetch)
        if kid not in self._keys:
            raise AuthError(f"no jwt key {kid!r} at the issuer")
        return self._keys[kid]

    async def _load(self, jwks_url: str, fetch: Fetch | None) -> None:
        body = await _get(jwks_url, MAX_JWKS, fetch)
        try:
            doc = json.loads(body)
            keys = doc["keys"] if isinstance(doc, dict) else None
            if not isinstance(keys, list) or len(keys) > 32:
                raise AuthError("jwks: not a small list of keys")
            self._keys = {k["kid"]: k for k in keys
                          if isinstance(k, dict) and isinstance(k.get("kid"), str)}
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthError(f"jwks unreadable: {exc}") from exc
        self._fetched_at = time.monotonic()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64u(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def login_url(endpoints: Endpoints, client_id: str, redirect_uri: str,
              scopes: str = "openid") -> tuple[str, str, str]:
    """The authorization URL for a session, with PKCE S256: returns
    (url, state, code_verifier) — the verifier never leaves the server."""
    from urllib.parse import quote
    verifier = _b64u(secrets.token_bytes(48))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    state = _b64u(secrets.token_bytes(24))
    sep = "&" if "?" in endpoints.authorization else "?"
    url = (f"{endpoints.authorization}{sep}response_type=code"
           f"&client_id={quote(client_id, safe='')}"
           f"&redirect_uri={quote(redirect_uri, safe='')}"
           f"&scope={quote(scopes, safe='')}&state={state}"
           f"&code_challenge={challenge}&code_challenge_method=S256")
    return url, state, verifier


@dataclass(repr=False)
class Tokens:
    """A login's Keycloak tokens, in memory only and never printed: the
    access token (the NATS connect token, RT-6.43) and the refresh token
    of the ordinary session (no ``offline_access``), empty if none came."""
    access: str
    refresh: str = ""

    def __repr__(self) -> str:
        return "Tokens(…)"


async def _token_call(endpoints: Endpoints, form: dict[str, str],
                      post: Post | None, what: str) -> Tokens:
    if post is not None:  # as _get: a test seam
        body = await post(endpoints.token, form)
        doc = json.loads(body)
    else:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            res = await client.post(endpoints.token, data=form)
            if 400 <= res.status_code < 500:
                # the realm said no (invalid_grant: the session ended, or the
                # client's grant is off): a refusal, not a passing fault
                raise Refused(f"token endpoint refused the {what}")
            if res.status_code != 200 or len(res.content) > MAX_DOC:
                raise AuthError(f"token endpoint failed on the {what}")
            doc = res.json()
    if not isinstance(doc, dict) or not isinstance(doc.get("access_token"), str):
        raise AuthError("no access token in the answer")
    refresh = doc.get("refresh_token")
    return Tokens(access=doc["access_token"],
                  refresh=refresh if isinstance(refresh, str) else "")


async def exchange(endpoints: Endpoints, client_id: str, redirect_uri: str,
                   code: str, verifier: str,
                   post: Post | None = None) -> Tokens:
    """The code for the login's tokens (unverified here: ``verify`` decides
    who the operator is)."""
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": verifier}
    return await _token_call(endpoints, form, post, "code")


async def refresh(endpoints: Endpoints, client_id: str, refresh_token: str,
                  post: Post | None = None) -> Tokens:
    """Fresh tokens from the refresh token of the operator's ordinary
    Keycloak session (RT-6.43 step 4); a rotated refresh token replaces the
    old one, an answer without one keeps it. ``Refused`` when the realm
    refuses (the session ended or was revoked)."""
    if not refresh_token:
        raise Refused("no refresh token: the realm gave none at the login")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id}
    tokens = await _token_call(endpoints, form, post, "refresh")
    if not tokens.refresh:
        tokens.refresh = refresh_token
    return tokens


async def verify(settings: config.Settings, endpoints: Endpoints,
                 keyring: Keyring, raw: str,
                 fetch: Fetch | None = None) -> Operator:
    """One access token to an Operator: the signature verified against the
    realm's keys, and the claims checked as the login service checks them —
    the console's own client, the operators group, a username."""
    try:
        head_raw, payload_raw, signature_raw = raw.split(".")
        head = json.loads(_unb64u(head_raw))
        payload = json.loads(_unb64u(payload_raw))
        signature = _unb64u(signature_raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError(f"token unreadable: {exc}") from exc
    if not isinstance(head, dict) or not isinstance(payload, dict):
        raise AuthError("token not a jwt object")
    alg = head.get("alg")
    if not isinstance(alg, str) or alg not in settings.token_algs:
        raise AuthError(f"token alg {alg!r} not in the allowlist")
    kid = head.get("kid")
    if not isinstance(kid, str) or not kid:
        raise AuthError("token without a kid")
    jwk = await keyring.public_key(endpoints.jwks, kid, fetch)
    _check_signature(alg, jwk, signature, f"{head_raw}.{payload_raw}".encode())
    return _check_claims(settings, endpoints, payload)


def _check_signature(alg: str, jwk: dict[str, Any],
                      signature: bytes, signed: bytes) -> None:
    import cryptography.exceptions
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    if jwk.get("kty") != "RSA" or not isinstance(jwk.get("n"), str) or not isinstance(jwk.get("e"), str):
        raise AuthError(f"token key is no RSA key (kty {jwk.get('kty')!r})")
    try:
        numbers = rsa.RSAPublicNumbers(
            e=int.from_bytes(_unb64u(jwk["e"]), "big"),
            n=int.from_bytes(_unb64u(jwk["n"]), "big"))
        numbers.public_key().verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
    except cryptography.exceptions.InvalidSignature as exc:
        raise AuthError("token signature does not verify") from exc
    except (ValueError, TypeError) as exc:
        raise AuthError(f"token key unusable: {exc}") from exc


def _check_claims(settings: config.Settings, endpoints: Endpoints,
                  payload: dict[str, Any]) -> Operator:
    if payload.get("iss") != endpoints.issuer:
        raise Refused("token of another issuer")
    aud = payload.get("aud")
    if aud != settings.client_id and not (isinstance(aud, list)
                                          and settings.client_id in aud):
        raise Refused("token audience is not the console's client")
    if payload.get("azp") != settings.client_id:
        raise Refused("token azp is not the console's client")
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or float(exp) < time.time() - LEEWAY_SEC:
        raise Refused("token expired")
    username = payload.get("preferred_username")
    if not isinstance(username, str) or not username:
        raise Refused("token without a username")
    if not settings.operator_group:
        # defence in depth: the config already refuses to start without a
        # group; an empty one here admits nobody either
        raise Refused("no operators group configured")
    groups = payload.get("groups")
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        groups = []
    if settings.operator_group not in groups:
        # the match is exact, as the login service's verifier: name the
        # group with its full path ("/operators") when the realm's group
        # mapper writes the full path
        raise Refused("not a member of the operators group")
    display = payload.get("name")
    return Operator(username=username,
                    display=display if isinstance(display, str) and display else username,
                    groups=tuple(groups))


@dataclass
class Sessions:
    """Operator sessions: an opaque cookie token, hashed in memory, to the
    operator; nothing else is kept — a restart logs everyone out, and the
    browser holds nothing but the HttpOnly cookie.

    A session may carry a view (``nats_read.View``: its fleet and its own
    NATS reader, RT-6.43); whatever ends the session — logout, expiry, the
    reader's own end — closes the view, which drops the login's key and
    tokens."""
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    _sessions: dict[str, tuple[float, Operator, Any]] = field(default_factory=dict)

    def _digest(self, token: str) -> str:
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _close(row: tuple[float, Operator, Any]) -> None:
        close = getattr(row[2], "close", None)
        if close is not None:
            close()

    def issue(self, operator: Operator, ttl: float, view: Any = None) -> str:
        token = secrets.token_urlsafe(32)
        at = time.time()
        for d, row in list(self._sessions.items()):  # sweep: expired sessions go
            if row[0] <= at:
                del self._sessions[d]
                self._close(row)
        self._sessions[self._digest(token)] = (at + ttl, operator, view)
        return token

    def drop(self, token: str) -> None:
        """Logout: the server ends the session, not only the cookie."""
        row = self._sessions.pop(self._digest(token), None)
        if row is not None:
            self._close(row)

    def end(self, view: Any) -> None:
        """The session whose view this is ends (its reader ended it)."""
        for d, row in list(self._sessions.items()):
            if row[2] is view:
                del self._sessions[d]
                self._close(row)

    def views(self) -> list[Any]:
        return [row[2] for row in self._sessions.values() if row[2] is not None]

    def view(self, token: str | None) -> Any:
        """The live session's view, or None."""
        if self.get(token) is None:
            return None
        assert token is not None
        return self._sessions[self._digest(token)][2]

    def get(self, token: str | None, now: float | None = None) -> Operator | None:
        if not token:
            return None
        digest = self._digest(token)
        row = self._sessions.get(digest)
        if row is None:
            return None
        until, operator, _view = row
        at = time.time() if now is None else now
        if at > until:
            del self._sessions[digest]
            self._close(row)
            return None
        return operator
