"""The console reads the bus as the logged-in operator (RT-6.43 steps 1 to
4, the console decision's addition of 2026-11-25 to ADR-015, points 1 and 5
to 7): one reader per login with a key pair of its own and the operator's
access token as the connect token, renewed with a fresh token and the same
key, the session ended when the realm or the group refuses, and no static
account. NATS itself is a fake here (``PersonReader.dial`` is the seam);
``test_nats_live.py`` drives the same reader against a real nats-server.
"""
from __future__ import annotations

import asyncio
import json
import time
import unittest.mock as mock
from types import SimpleNamespace
from typing import Any

import pytest

from console_server import auth, config, nats_read
from console_server.app import Console
from console_server.nats_read import PersonReader, View
from test_http import get

SETTINGS = config.Settings(public_url="http://127.0.0.1:8747",
                           issuer="http://127.0.0.1:8747/kc",
                           client_id="console", operator_group="operators",
                           nats_url="nats://127.0.0.1:4")


class FakeConn:
    """A connection as the reader uses it: subscriptions, two flushes, a
    close that tells the reader it is gone."""

    def __init__(self, lost: Any, refused: list[str], refuse: bool) -> None:
        self.subs: dict[str, Any] = {}
        self.is_closed = False
        self._lost, self._refused, self._refuse = lost, refused, refuse

    async def subscribe(self, subject: str, cb: Any = None) -> None:
        self.subs[subject] = cb
        if self._refuse:
            self._refused.append("permissions violation")

    async def flush(self, *a: Any, **k: Any) -> None:
        return None

    async def close(self) -> None:
        if not self.is_closed:
            self.is_closed = True
            await self._lost()


class Dialer:
    """Stands in for nats-py: what each connect presented, and a script of
    outcomes ("ok", "authz" for the callout's refusal, "name" for refused
    subscriptions)."""

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.tokens: list[str] = []
        self.keys: list[str] = []
        self.conns: list[FakeConn] = []

    async def __call__(self, reader: PersonReader, access: str, lost: Any,
                       refused: list[str]) -> FakeConn:
        self.tokens.append(access)
        self.keys.append(reader.public_key)
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "authz":
            raise Exception("nats: 'Authorization Violation'")
        conn = FakeConn(lost, refused, refuse=outcome == "name")
        self.conns.append(conn)
        return conn


async def until(cond: Any, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def reader_with(dialer: Dialer, renew: Any, *, renew_sec: float = 60.0,
                until_wall: float | None = None,
                on_end: Any = None) -> tuple[PersonReader, View]:
    view = View("presence")
    r = PersonReader(SETTINGS, "dembaca", auth.Tokens("first-access", "first-refresh"),
                     renew, view, until=until_wall or time.time() + 3600,
                     on_end=on_end or (lambda: None), renew_sec=renew_sec)
    r.dial = lambda access, lost, refused: dialer(r, access, lost, refused)  # type: ignore[method-assign]
    view.reader = r
    return r, view


def ack(consumer: str, seq: int, pending: int = 0) -> str:
    return f"$JS.ACK.PRESENCE.{consumer}.1.{seq}.{seq}.{int(time.time() * 1e9)}.{pending}"


async def test_the_reader_connects_as_this_login_and_reads_its_own_subjects() -> None:
    """Step 1: the login's own key and the operator's access token; the
    two deliver subjects of this login by their literal names, and only
    deliveries of this login's presence consumer change the fleet."""
    dialer = Dialer()
    r, view = reader_with(dialer, renew=None)
    r.start()
    try:
        await until(lambda: r.state == "reading")
        assert dialer.tokens == ["first-access"]
        assert dialer.keys[0].startswith("U") and dialer.keys[0] == r.public_key
        login = r.public_key[1:11].lower()
        assert r.name == "person-dembaca"
        assert set(dialer.conns[0].subs) == {
            f"deliver.person-dembaca.{login}.presence",
            f"deliver.person-dembaca.{login}.stats"}
        assert r.presence_consumer == f"person-dembaca_{login}_presence"

        wake = asyncio.Event()
        view.wakes.add(wake)
        cb = dialer.conns[0].subs[r.deliver_presence]
        # another consumer's delivery (the old static one's name) is dropped
        await cb(SimpleNamespace(subject="presence.dembaca.agent-a",
                                 data=b'{"v":1,"status":"online"}',
                                 reply=ack("console", 1)))
        assert view.fleet.cards() == [] and not wake.is_set()
        await cb(SimpleNamespace(subject="presence.dembaca.agent-a",
                                 data=b'{"v":1,"status":"online"}',
                                 reply=ack(r.presence_consumer, 1)))
        assert [c["name"] for c in view.fleet.cards()] == ["agent-a"]
        assert view.fleet.synced and wake.is_set()
        await dialer.conns[0].subs[r.deliver_stats](
            SimpleNamespace(subject="stats.agent-a", data=b"{}", reply=None))
        assert view.stats_lines == 1
    finally:
        await r.aclose()
    assert not r.holds_secrets            # key wiped, tokens dropped
    assert dialer.conns[0].is_closed


async def test_two_logins_of_one_person_have_their_own_keys() -> None:
    dialer = Dialer()
    a, _ = reader_with(dialer, renew=None)
    b, _ = reader_with(dialer, renew=None)
    assert a.public_key != b.public_key and a.login != b.login
    assert a.deliver_presence != b.deliver_presence


async def test_the_renewal_takes_a_fresh_token_and_the_same_key() -> None:
    """Step 4: every renewal refreshes the access token and connects again
    with the same key; the old connection is closed before the new one
    subscribes, so no delivery reaches two subscriptions."""
    dialer = Dialer()
    refreshes: list[str] = []

    async def renew(refresh: str) -> auth.Tokens:
        refreshes.append(refresh)
        n = len(refreshes)
        return auth.Tokens(f"access-{n}", f"refresh-{n}")
    r, _view = reader_with(dialer, renew, renew_sec=0.05)
    r.start()
    try:
        await until(lambda: r.connects >= 3)
    finally:
        await r.aclose()
    assert dialer.tokens[:3] == ["first-access", "access-1", "access-2"]
    assert refreshes[:2] == ["first-refresh", "refresh-1"]   # a rotated one is used
    assert len(set(dialer.keys)) == 1                         # the same key
    assert dialer.conns[0].is_closed and dialer.conns[1].is_closed


async def test_a_refused_renewal_ends_the_session() -> None:
    """The realm refuses the refresh (the session ended) or the refreshed
    token has no operators' group: the reader closes, drops key and
    tokens, and ends its session."""
    dialer = Dialer()
    ended: list[bool] = []

    async def renew(refresh: str) -> auth.Tokens:
        raise auth.Refused("not a member of the operators group")
    r, _view = reader_with(dialer, renew, renew_sec=0.05,
                           on_end=lambda: ended.append(True))
    r.start()
    await until(lambda: ended == [True])
    await until(lambda: not r.running)
    assert r.state == "ended" and not r.holds_secrets
    assert dialer.conns[0].is_closed


async def test_a_passing_renewal_failure_keeps_the_connection() -> None:
    """Keycloak or the login service down at a renewal: the connection runs
    on (its credential has 15 minutes), the reader retries."""
    dialer = Dialer("ok", "authz", "ok")
    calls: list[int] = []

    async def renew(refresh: str) -> auth.Tokens:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("realm down")
        return auth.Tokens(f"access-{len(calls)}", refresh)
    r, _view = reader_with(dialer, renew, renew_sec=0.05)
    with mock.patch.object(nats_read, "BACKOFF_MAX_SEC", 0.05):
        r.start()
        try:
            await until(lambda: r.connects >= 2, timeout=8.0)
        finally:
            await r.aclose()
    # the realm failed once, the callout refused once; the first
    # connection was closed only when a new one stood
    assert len(dialer.conns) >= 2
    assert dialer.tokens[0] == "first-access" and "first-access" not in dialer.tokens[1:]


async def test_a_name_the_bus_does_not_know_reads_nothing() -> None:
    """The directory's anchor is not the username (#86): the server refuses
    the deliver subjects, the reader stops reading, the session stays."""
    dialer = Dialer("name")
    ended: list[bool] = []
    r, _view = reader_with(dialer, renew=None, on_end=lambda: ended.append(True))
    r.start()
    try:
        await until(lambda: "another name" in r.state)
        assert dialer.conns[0].is_closed and not ended
    finally:
        await r.aclose()


async def test_the_session_end_ends_the_reader() -> None:
    dialer = Dialer()
    ended: list[bool] = []
    r, _view = reader_with(dialer, renew=None, until_wall=time.time() + 0.2,
                           on_end=lambda: ended.append(True))
    r.start()
    await until(lambda: ended == [True])
    assert not r.holds_secrets


# --- the app: a reader per login, logout, refusal → /login, no static reader ---------

async def logged_in(c: Console, dialer: Dialer) -> str:
    verified = auth.Operator(username="dembaca", display="D", groups=("operators",))

    async def fake_exchange(*a: Any, **k: Any) -> auth.Tokens:
        return auth.Tokens("login-access", "login-refresh")

    async def fake_verify(*a: Any, **k: Any) -> auth.Operator:
        return verified
    status, headers, _b = await get(c, "/login")
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(headers["location"]).query)["state"][0]
    flow = next(v for v in headers["__set-cookie__"] if v.startswith("choirbus_flow="))
    with mock.patch.object(auth, "exchange", fake_exchange), \
            mock.patch.object(auth, "verify", fake_verify), \
            mock.patch.object(PersonReader, "dial",
                              lambda self, *a: dialer(self, *a)):
        status, headers, _b = await get(c, f"/callback?code=x&state={state}",
                                        cookie=flow.split(";")[0])
        assert status == 303
        cookie = next(v for v in headers["__set-cookie__"]
                      if v.startswith("choirbus_session=")).split(";")[0]
        view = c.sessions.views()[-1]
        await until(lambda: view.reader.state == "reading")
    return cookie


@pytest.fixture
async def console():
    c = Console(SETTINGS)
    c.endpoints = auth.Endpoints(issuer=SETTINGS.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")

    async def discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=discover)
    try:
        yield c
    finally:
        await c.stop()


async def test_no_reader_before_a_login_and_one_per_login(console: Console) -> None:
    assert console.sessions.views() == []
    dialer = Dialer()
    cookie = await logged_in(console, dialer)
    assert dialer.tokens == ["login-access"]       # the operator's own token
    view = console.sessions.views()[0]
    assert view.reader is not None and view.reader.running
    # the fleet the session shows is its own reader's
    cb = dialer.conns[0].subs[view.reader.deliver_presence]
    await cb(SimpleNamespace(subject="presence.dembaca.agent-a",
                             data=b'{"v":1,"status":"online"}',
                             reply=ack(view.reader.presence_consumer, 1)))
    status, _h, body = await get(console, "/v1/graph", cookie=cookie)
    assert status == 200 and "agent-a" in json.dumps(json.loads(body))
    assert console.fleet.cards() == []             # the stand-in stays empty
    # logout: the reader stops, key and tokens go
    status, _h, _b = await get(console, "/logout", cookie=cookie)
    await until(lambda: not view.reader.running)
    assert not view.reader.holds_secrets and dialer.conns[0].is_closed
    assert console.sessions.views() == []


async def test_a_refused_renewal_sends_the_browser_to_login(console: Console) -> None:
    dialer = Dialer()
    cookie = await logged_in(console, dialer)
    view = console.sessions.views()[0]

    async def refused(username: str, refresh: str) -> auth.Tokens:
        assert refresh == "login-refresh"
        raise auth.Refused("token endpoint refused the refresh")
    console._renew = refused  # type: ignore[method-assign]
    view.reader.renew_sec = 0.05
    # the renewal wakes on its own timer; nudge it by closing the connection
    await dialer.conns[0].close()
    await until(lambda: not view.reader.running)
    status, _h, _b = await get(console, "/v1/me", cookie=cookie)
    assert status == 401                            # the app goes to /login
    assert not view.reader.holds_secrets


async def test_the_renewal_checks_the_group_again(console: Console) -> None:
    """``Console._renew``: a refreshed token is verified as at the login —
    without the group it is a refusal, and so is another person's."""
    async def fresh(*a: Any, **k: Any) -> auth.Tokens:
        return auth.Tokens("new-access", "new-refresh")

    async def no_group(*a: Any, **k: Any) -> auth.Operator:
        raise auth.Refused("not a member of the operators group")

    async def someone_else(*a: Any, **k: Any) -> auth.Operator:
        return auth.Operator(username="mallory", display="m", groups=("operators",))
    with mock.patch.object(auth, "refresh", fresh), \
            mock.patch.object(auth, "verify", no_group):
        with pytest.raises(auth.Refused):
            await console._renew("dembaca", "r")
    with mock.patch.object(auth, "refresh", fresh), \
            mock.patch.object(auth, "verify", someone_else):
        with pytest.raises(auth.Refused, match="another person"):
            await console._renew("dembaca", "r")


async def test_stop_closes_every_login(console: Console) -> None:
    dialer = Dialer()
    await logged_in(console, dialer)
    view = console.sessions.views()[0]
    await console.stop()
    assert not view.reader.running and not view.reader.holds_secrets


# --- readiness: the settings and NATS reachable, no fleet before a login ------------

async def test_healthz_is_ready_when_nats_answers() -> None:
    async def nothing(reader: Any, writer: Any) -> None:
        writer.close()
    server = await asyncio.start_server(nothing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    c = Console(config.Settings(public_url="http://127.0.0.1:8747", login_off=True,
                                nats_url=f"nats://127.0.0.1:{port}"))
    await c.start()
    try:
        status, _h, body = await get(c, "/healthz")
        assert (status, body) == (200, b"ok")   # no login, no fleet: ready
        server.close()
        await server.wait_closed()
        c._reach = None                          # past the 5 s cache
        status, _h, _b = await get(c, "/healthz")
        assert status == 503
        status, _h, _b = await get(c, "/livez")
        assert status == 200                     # NATS down is no restart
    finally:
        await c.stop()
