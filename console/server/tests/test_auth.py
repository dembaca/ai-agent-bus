"""The operator's login: the browser flow's URL, the token exchange seam,
and above all the verification — a real RS256 key signs the tokens, every
wrong claim and every broken signature is refused (RISK-12 assumes this
holds, so the test holds it to it).
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from console_server import auth, config
from console_server.auth import Keyring, Sessions

SETTINGS = config.Settings(public_url="https://console.example",
                           issuer="https://kc/realms/x", client_id="console",
                           operator_group="operators")


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64u(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "kid-1",
           "n": _b64u(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
           "e": _b64u(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big"))}
    return private, jwk


def sign(private, head: dict, payload: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    raw = f"{_b64u(json.dumps(head).encode())}.{_b64u(json.dumps(payload).encode())}"
    signature = private.sign(raw.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{raw}.{_b64u(signature)}"


def token(private, kid="kid-1", alg="RS256", iss="https://kc/realms/x",
          aud="console", azp="console", exp=None, groups=("operators",),
          username="dembaca") -> str:
    now = time.time()
    head = {"alg": alg, "kid": kid, "typ": "Bearer"}
    payload = {"iss": iss, "aud": aud, "azp": azp,
               "exp": exp if exp is not None else int(now) + 300,
               "iat": int(now), "preferred_username": username, "sub": "sub-1",
               "name": "Dem Bac", "groups": list(groups)}
    return sign(private, head, payload)


ENDPOINTS = auth.Endpoints(issuer="https://kc/realms/x",
                           authorization="https://kc/realms/x/auth",
                           token="https://kc/realms/x/token",
                           jwks="https://kc/realms/x/certs")


async def test_a_good_token_names_the_operator() -> None:
    private, jwk = keypair()
    keyring = Keyring()
    keyring._keys = {jwk["kid"]: jwk}
    keyring._fetched_at = time.monotonic()
    operator = await auth.verify(SETTINGS, ENDPOINTS, keyring,
                                 token(private))
    assert operator.username == "dembaca"
    assert operator.display == "Dem Bac"
    assert operator.groups == ("operators",)


async def test_every_wrong_token_is_refused() -> None:
    private, jwk = keypair()
    other_private, _ = keypair()
    keyring = Keyring()
    keyring._keys = {jwk["kid"]: jwk}
    keyring._fetched_at = time.monotonic()

    bad = {
        "another issuer": token(private, iss="https://other"),
        "another audience": token(private, aud="other-console"),
        "another azp": token(private, azp="agent-bus"),
        "expired": token(private, exp=int(time.time()) - 3600),
        "not in the group": token(private, groups=("viewers",)),
        "alg none": token(private, alg="none"),
        "signed by another key": sign(other_private,
                                      {"alg": "RS256", "kid": "kid-1", "typ": "Bearer"},
                                      json.loads(_unb64u(
                                          token(private).split(".")[1]))),
        "no kid": token(private, kid=None),
    }
    for what, raw in bad.items():
        try:
            await auth.verify(SETTINGS, ENDPOINTS, keyring, raw)
        except auth.AuthError:
            continue
        raise AssertionError(f"verified a bad token: {what}")
    # the groups claim broken is refused, not read as empty
    head = {"alg": "RS256", "kid": "kid-1", "typ": "Bearer"}
    payload = {"iss": "https://kc/realms/x", "aud": "console", "azp": "console",
               "exp": int(time.time()) + 300, "preferred_username": "d", "sub": "s",
               "groups": "no list is fine (rejected to empty)", "name": "D"}
    with pytest.raises(auth.AuthError, match="not a member"):
        await auth.verify(SETTINGS, ENDPOINTS, keyring, sign(private, head, payload))


async def test_a_broken_token_never_verifies() -> None:
    private, jwk = keypair()
    keyring = Keyring()
    keyring._keys = {jwk["kid"]: jwk}
    keyring._fetched_at = time.monotonic()
    raws = ("", "a.b", "aaa.bbb", "not a jwt", f"{token(private)}x")
    for raw in raws:
        with pytest.raises(auth.AuthError):
            await auth.verify(SETTINGS, ENDPOINTS, keyring, raw)


def test_login_url_is_pkce() -> None:
    url, state, verifier = auth.login_url(ENDPOINTS, "console",
                                          "https://console.example/callback")
    assert url.startswith("https://kc/realms/x/auth?response_type=code")
    assert "code_challenge_method=S256" in url
    assert len(state) >= 16 and len(verifier) >= 43
    assert verifier not in url  # the challenge is its hash; the verifier itself never travels


async def test_exchange_with_a_fake_token_endpoint() -> None:
    async def post(url: str, form: dict) -> bytes:
        assert url == ENDPOINTS.token
        assert form["code_verifier"] == "the-verifier"
        return json.dumps({"access_token": "x.y.z", "id_token": "no",
                           "refresh_token": "r.r.r"}).encode()
    tokens = await auth.exchange(ENDPOINTS, "console",
                                 "https://console.example/callback",
                                 "the-code", "the-verifier", post=post)
    assert (tokens.access, tokens.refresh, tokens.id_token) == ("x.y.z", "r.r.r", "no")
    assert "x.y.z" not in repr(tokens) and "r.r.r" not in repr(tokens)


async def test_refresh_uses_the_sessions_refresh_token() -> None:
    """RT-6.43 step 4: the renewal's grant is the ordinary session's
    refresh token for the console's public client, no offline scope; a
    rotated refresh token replaces the old one, none in the answer keeps
    it; no refresh token at all is a refusal, never a passing fault."""
    seen: list[dict] = []

    async def post(url: str, form: dict) -> bytes:
        seen.append(form)
        body: dict = {"access_token": "fresh"}
        if form["refresh_token"] == "old":
            body["refresh_token"] = "rotated"
        return json.dumps(body).encode()
    tokens = await auth.refresh(ENDPOINTS, "console", "old", post=post)
    assert (tokens.access, tokens.refresh) == ("fresh", "rotated")
    assert seen[0] == {"grant_type": "refresh_token", "refresh_token": "old",
                       "client_id": "console"}
    assert "scope" not in seen[0]           # no offline_access asked for
    tokens = await auth.refresh(ENDPOINTS, "console", "kept", post=post)
    assert tokens.refresh == "kept"
    with pytest.raises(auth.Refused):
        await auth.refresh(ENDPOINTS, "console", "", post=post)


def _id_token(payload: dict) -> str:
    return "e30." + _b64u(json.dumps(payload).encode()) + ".sig"


def test_logout_url_hints_with_the_stored_id_token() -> None:
    """RT-6.43 step 9: the realm's end-session URL with the client and the
    address to come back to always, the stored ID token as the hint even
    once expired (the realm checks its signature, not its expiry); without
    one no hint; no end-session endpoint, no URL."""
    ends = auth.Endpoints(issuer=ENDPOINTS.issuer, authorization=ENDPOINTS.authorization,
                          token=ENDPOINTS.token, jwks=ENDPOINTS.jwks,
                          end_session="https://kc/realms/x/logout")
    back = "https://console.example/"
    for stored in (_id_token({"exp": int(time.time()) + 300}),
                   _id_token({"exp": 999})):          # expired: still the hint
        url = auth.logout_url(ends, "console", back, stored)
        assert url == ("https://kc/realms/x/logout?client_id=console"
                       "&post_logout_redirect_uri=https%3A%2F%2Fconsole.example%2F"
                       f"&id_token_hint={stored}")
    url = auth.logout_url(ends, "console", back, "")
    assert "id_token_hint" not in url and "client_id=console" in url
    queried = auth.Endpoints(issuer=ENDPOINTS.issuer, authorization=ENDPOINTS.authorization,
                             token=ENDPOINTS.token, jwks=ENDPOINTS.jwks,
                             end_session="https://kc/realms/x/logout?ui_locales=en")
    url = auth.logout_url(queried, "console", back, "")
    assert url.startswith("https://kc/realms/x/logout?ui_locales=en&client_id=console&")
    assert auth.logout_url(ENDPOINTS, "console", back, "stored") == ""


async def test_discovery_reads_the_end_session_endpoint() -> None:
    doc = {"issuer": "https://kc/realms/x",
           "authorization_endpoint": "https://kc/realms/x/auth",
           "token_endpoint": "https://kc/realms/x/token",
           "jwks_uri": "https://kc/realms/x/certs"}

    async def fetch(url: str) -> bytes:
        return json.dumps(doc).encode()
    assert (await auth.discover("https://kc/realms/x", fetch)).end_session == ""
    doc["end_session_endpoint"] = "https://kc/realms/x/logout"
    assert (await auth.discover("https://kc/realms/x", fetch)).end_session \
        == "https://kc/realms/x/logout"
    doc["end_session_endpoint"] = "https://evil.example/logout"
    with pytest.raises(auth.AuthError):
        await auth.discover("https://kc/realms/x", fetch)


def test_a_sessions_id_token_goes_with_it() -> None:
    from console_server.auth import Operator
    sessions = Sessions()
    operator = Operator(username="dembaca", display="d", groups=("operators",))
    value = sessions.issue(operator, ttl=3600, id_token="the-id-token")
    assert "the-id-token" not in repr(sessions)
    assert sessions.drop(value) == "the-id-token"
    assert sessions.drop(value) == ""          # gone with the session
    expired = sessions.issue(operator, ttl=3600, id_token="old")
    assert sessions.get(expired, now=time.time() + 7200) is None
    assert sessions._hints == {}
    # the sweep in issue: an expired session's hint goes with it
    sessions.issue(operator, ttl=-1, id_token="swept")
    sessions.issue(operator, ttl=3600)
    assert "swept" not in sessions._hints.values()
    # the per-person cap evicts the oldest, and its hint
    capped = Sessions()
    capped.issue(operator, ttl=3600, per_person=1, id_token="first")
    second = capped.issue(operator, ttl=3600, per_person=1, id_token="second")
    assert list(capped._hints.values()) == ["second"]
    # the reader's own end (Sessions.end) takes the hint too
    view = object()
    ended = Sessions()
    ended.issue(operator, ttl=3600, view=view, id_token="viewed")
    ended.end(view)
    assert ended._hints == {}
    assert capped.drop(second) == "second"


def test_sessions_hash_the_token_in_memory() -> None:
    from console_server.auth import Operator
    sessions = Sessions()
    operator = Operator(username="dembaca", display="d", groups=("operators",))
    value = sessions.issue(operator, ttl=3600)
    unknown = sessions.issue(operator, ttl=3600)
    assert sessions.get(value) == operator
    assert sessions.get(unknown) == operator  # its own token works
    assert sessions.get("forged") is None
    assert sessions.get(value, now=time.time() + 7200) is None  # expired
    assert sessions.get(value) is None


async def test_the_group_match_is_exact_and_the_full_path_works() -> None:
    """The match is the Go verifier's: exact against the configured name.
    A realm whose group mapper writes the full path names "/operators" in
    the config; a bare "operators" is then not a match, and vice versa."""
    private, jwk = keypair()
    keyring = Keyring()
    keyring._keys = {jwk["kid"]: jwk}
    keyring._fetched_at = time.monotonic()
    full = config.Settings(public_url="https://console.example",
                           issuer="https://kc/realms/x", client_id="console",
                           operator_group="/operators")
    operator = await auth.verify(full, ENDPOINTS, keyring,
                                 token(private, groups=("/operators",)))
    assert operator.username == "dembaca"
    with pytest.raises(auth.Refused):
        await auth.verify(full, ENDPOINTS, keyring,
                         token(private, groups=("operators",)))


async def test_an_empty_group_admits_nobody() -> None:
    """Defence in depth: the config refuses to start without a group; a
    verify with an empty one refuses too, never everyone."""
    private, jwk = keypair()
    keyring = Keyring()
    keyring._keys = {jwk["kid"]: jwk}
    keyring._fetched_at = time.monotonic()
    settings = config.Settings(public_url="https://console.example",
                               issuer="https://kc/realms/x", client_id="console",
                               operator_group="")
    with pytest.raises(auth.Refused, match="no operators group"):
        await auth.verify(settings, ENDPOINTS, keyring, token(private))
