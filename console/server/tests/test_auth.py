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
               "iat": int(now), "preferred_username": username,
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
               "exp": int(time.time()) + 300, "preferred_username": "d",
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
        return json.dumps({"access_token": "x.y.z", "id_token": "no"}).encode()
    raw = await auth.exchange(ENDPOINTS, "console",
                              "https://console.example/callback",
                              "the-code", "the-verifier", post=post)
    assert raw == "x.y.z"


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
