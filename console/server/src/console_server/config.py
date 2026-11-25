"""Settings, from the environment, checked on start (fail closed)."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class ConfigError(Exception):
    """A setting is missing or wrong: the server does not start."""


def _url(value: str, name: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ConfigError(f"{name}: http(s) URL, got {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    """Everything from ``CONSOLE_*`` (the login service keeps its ``AUTH_*``)."""

    # where the browser reaches the console (its own redirect URI)
    public_url: str
    # Keycloak: issuer and the console's own client; the operators group
    issuer: str = ""
    client_id: str = ""
    operator_group: str = ""
    login_off: bool = False            # tests and the mock only, never the lab
    token_algs: tuple[str, ...] = ("RS256",)
    session_sec: float = 8 * 3600.0

    # where to listen (default: loopback; the lab fronts it with TLS)
    listen_host: str = "127.0.0.1"
    listen_port: int = 8747

    # the read-only NATS side: one connection per logged-in operator, with a
    # credential the login service issues for that person (RT-6.43); no
    # static account, no seed
    nats_url: str = ""
    nats_ca_file: str = ""
    presence_subject: str = "presence"
    presence_stream: str = "PRESENCE"

    # the built browser app (console/web/dist)
    web_dir: str = ""
    health_path: str = "/healthz"
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def find() -> Settings:
    """From the environment; absent optional settings become their
    defaults, required ones raise ``ConfigError`` with their names."""
    def given(name: str) -> str:
        return os.environ.get(name, "").strip()

    listen = given("CONSOLE_ADDR")
    host, port = "127.0.0.1", 8747
    if listen:
        if ":" not in listen:
            raise ConfigError("CONSOLE_ADDR: host:port")
        h, _, p = listen.rpartition(":")
        host = h or host
        if not p.isdigit() or not (0 < int(p) < 65536):
            raise ConfigError("CONSOLE_ADDR: host:port")
        port = int(p)

    settings = Settings(
        public_url=_required(given, "CONSOLE_PUBLIC_URL"),
        issuer=given("CONSOLE_ISSUER"),
        client_id=given("CONSOLE_CLIENT_ID"),
        operator_group=given("CONSOLE_OPERATOR_GROUP"),
        login_off=(given("CONSOLE_LOGIN_OFF") == "1"),
        token_algs=tuple(a.strip() for a in given("CONSOLE_TOKEN_ALGS").split(",") if a.strip())
        or ("RS256",),
        session_sec=float(given("CONSOLE_SESSION_SEC") or 8 * 3600),
        listen_host=host,
        listen_port=port,
        nats_url=given("CONSOLE_NATS_URL"),
        nats_ca_file=given("CONSOLE_NATS_CA_FILE"),
        presence_subject=given("CONSOLE_PRESENCE_SUBJECT") or "presence",
        presence_stream=given("CONSOLE_PRESENCE_STREAM") or "PRESENCE",
        web_dir=given("CONSOLE_WEB_DIR"),
    )
    if given("CONSOLE_NATS_SEED_FILE"):
        # the static account is gone (RT-6.43): a leftover setting points at a
        # secret that must go too, so the start refuses; the file is never read
        raise ConfigError("CONSOLE_NATS_SEED_FILE: the console's static NATS"
                          " account is gone, every operator reads with a"
                          " credential of their own; remove the setting, the"
                          " secret and the account's user")
    _check(settings)
    return _https(settings)


def _required(given: Callable[[str], str], name: str) -> str:
    value = given(name)
    if not value:
        raise ConfigError(f"{name} is required")
    return _url(value, name)


def _check(s: Settings) -> None:
    if not s.login_off and (not s.issuer or not s.client_id):
        raise ConfigError("CONSOLE_ISSUER and CONSOLE_CLIENT_ID are required"
                          " (or CONSOLE_LOGIN_OFF=1 for tests)")
    if not s.login_off and not s.operator_group:
        # fail closed (the reviews' blocker): an unset group would admit
        # every user of the realm
        raise ConfigError("CONSOLE_OPERATOR_GROUP is required: only its"
                          " members may log in")
    if s.login_off and s.listen_host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("CONSOLE_LOGIN_OFF=1 refuses a non-loopback"
                          " CONSOLE_ADDR: it would serve without a login")
    if not s.login_off:
        _url(s.issuer, "CONSOLE_ISSUER")
    _url(s.public_url, "CONSOLE_PUBLIC_URL")
    if not s.nats_url:
        raise ConfigError("CONSOLE_NATS_URL is required")
    parts = urlsplit(s.nats_url)
    if parts.scheme not in ("tls", "nats"):
        raise ConfigError("CONSOLE_NATS_URL: tls:// or nats:// URL")
    if parts.scheme == "nats" and parts.hostname not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("CONSOLE_NATS_URL: nats:// only to a loopback host"
                          " (a token to anywhere else would go in the clear); use tls://")
    for name in (s.presence_subject, s.presence_stream):
        if not name or any(c in name for c in ". *>"):
            raise ConfigError("CONSOLE_PRESENCE_SUBJECT/STREAM: one plain token")


def _https(s: Settings) -> Settings:
    """The cookie is Secure unless the console runs plain http on loopback."""
    host = urlsplit(s.public_url).hostname or ""
    if urlsplit(s.public_url).scheme == "http" and host in ("127.0.0.1", "::1", "localhost"):
        return s
    if not urlsplit(s.public_url).scheme == "https":
        raise ConfigError("CONSOLE_PUBLIC_URL: https, or http on loopback")
    return s


def secure_cookie(s: Settings) -> bool:
    return urlsplit(s.public_url).scheme == "https"
