"""The console reads the bus as the logged-in operator (RT-6.43 steps 1 to
4, the console decision's addition of 2026-11-25 to ADR-015, points 1 and 5
to 7): one reader per login with a key pair of its own and the operator's
access token as the connect token, subscribed by the login alone and named
by the login service's own record, renewed with a fresh token and the same
key, the session ended when the realm or the group refuses, the reading
ended (key and tokens wiped, the reason shown) when the bus refuses, and no
static account. NATS itself is a fake here (``PersonReader.dial`` is the
seam); ``test_nats_live.py`` drives the same reader against a real
nats-server.
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
    """A connection as the reader uses it: subscriptions, a flush, a close
    that tells the reader it is gone."""

    def __init__(self, lost: Any) -> None:
        self.subs: dict[str, Any] = {}
        self.is_closed = False
        self._lost = lost
        self.close_gate: asyncio.Event | None = None

    async def subscribe(self, subject: str, cb: Any = None) -> None:
        self.subs[subject] = cb

    async def flush(self, *a: Any, **k: Any) -> None:
        return None

    async def close(self) -> None:
        if self.close_gate is not None:
            await self.close_gate.wait()     # a close that hangs
        if not self.is_closed:
            self.is_closed = True
            await self._lost()


class Dialer:
    """Stands in for nats-py: what each connect presented, and a script of
    outcomes ("ok", "authz" for the login service's refusal, "down")."""

    def __init__(self, *script: str) -> None:
        self.script = list(script)
        self.tokens: list[str] = []
        self.keys: list[str] = []
        self.conns: list[FakeConn] = []

    async def __call__(self, reader: PersonReader, access: str, lost: Any) -> FakeConn:
        self.tokens.append(access)
        self.keys.append(reader.public_key)
        outcome = self.script.pop(0) if self.script else "ok"
        if outcome == "authz":
            raise Exception("nats: 'Authorization Violation'")
        if outcome == "down":
            raise OSError("connection refused")
        conn = FakeConn(lost)
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
    r = PersonReader(SETTINGS, auth.Tokens("first-access", "first-refresh"),
                     renew, view, until=until_wall or time.time() + 3600,
                     on_end=on_end or (lambda: None), renew_sec=renew_sec)
    r.dial = lambda access, lost: dialer(r, access, lost)  # type: ignore[method-assign]
    view.reader = r
    return r, view


def ack(consumer: str, sseq: int, cseq: int | None = None, pending: int = 0) -> str:
    cseq = sseq if cseq is None else cseq
    return f"$JS.ACK.PRESENCE.{consumer}.1.{sseq}.{cseq}.{int(time.time() * 1e9)}.{pending}"


def online(agent: str, reply: str) -> SimpleNamespace:
    return SimpleNamespace(subject=f"presence.dembaca.{agent}",
                           data=b'{"v":1,"status":"online"}', reply=reply)


async def test_the_reader_subscribes_by_the_login_and_learns_its_name() -> None:
    """Step 1: the login's own key and the operator's access token; the
    login's subjects by its whole key, never a name the console made up;
    the name and anchor come from the login service's record."""
    dialer = Dialer()
    r, view = reader_with(dialer, renew=None)
    r.start()
    try:
        await until(lambda: r.state == "reading")
        assert dialer.tokens == ["first-access"]
        assert dialer.keys[0].startswith("U") and dialer.keys[0] == r.public_key
        login = r.public_key[1:].lower()
        assert len(login) >= 26
        assert set(dialer.conns[0].subs) == {
            f"deliver.person.{login}.presence", f"deliver.person.{login}.stats",
            f"deliver.person.{login}.self"}
        assert r.name is None and view.bus()["name"] is None
        own = dialer.conns[0].subs[r.deliver_self]
        await own(SimpleNamespace(subject=r.deliver_self, reply=None,
                                  data=b'{"name": "person-dembaca", "anchor": "dembaca"}'))
        assert (r.name, r.anchor) == ("person-dembaca", "dembaca")
        assert view.bus() == {"state": "reading", "name": "person-dembaca",
                              "anchor": "dembaca"}
        await own(SimpleNamespace(subject=r.deliver_self, reply=None, data=b"[1]"))
        assert r.name == "person-dembaca"          # an unreadable record changes nothing

        wake = asyncio.Event()
        view.wakes.add(wake)
        cb = dialer.conns[0].subs[r.deliver_presence]
        # another login's consumer (or the old static one) is dropped
        await cb(online("agent-a", ack("console", 1)))
        assert view.fleet.cards() == [] and not wake.is_set()
        await cb(online("agent-a", ack(f"person-dembaca_{login}_presence", 2)))
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
    with the same key; the new connection subscribes before the old one
    closes."""
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


async def test_a_renewals_overlap_drops_repeats_not_a_new_consumer() -> None:
    """Both connections may deliver one message during a renewal: the
    second copy is dropped, so no repeat reads as a consumer made anew
    (which would forget the crown claims); a consumer really made anew
    (its sequence from 1 over other stream sequences) still is one."""
    dialer = Dialer()
    r, view = reader_with(dialer, renew=None)
    r.start()
    try:
        await until(lambda: r.state == "reading")
        cb = dialer.conns[0].subs[r.deliver_presence]
        consumer = f"person-dembaca_{r.login}_presence"
        crown = SimpleNamespace(subject="presence.crown.dev.agent-a", data=b'{"v": 1}',
                                reply=ack(consumer, 2, 2))
        await cb(online("agent-a", ack(consumer, 1, 1)))
        await cb(crown)
        assert view.fleet.holder_of("dev") == "agent-a"
        await cb(crown)                                 # the overlap's repeat
        await cb(online("agent-a", ack(consumer, 1, 1)))
        assert view.fleet.holder_of("dev") == "agent-a"
        assert view.fleet.synced
        # a consumer made anew: sequence 1 again, a later stream sequence
        await cb(online("agent-a", ack(consumer, 9, 1)))
        assert view.fleet.holder_of("dev") is None      # claims forgotten, replay comes
    finally:
        await r.aclose()


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
    assert r.state.startswith("ended") and not r.holds_secrets
    assert dialer.conns[0].is_closed


async def test_the_bus_refusing_the_login_ends_the_reading_at_once() -> None:
    """The login service refuses the connect (not an operator, no anchor,
    the cap of logins per person): no retry and no Keycloak refresh; key
    and tokens are wiped at once, the reason is the state the browser
    sees, and the session stays so it can be shown."""
    dialer = Dialer("authz")
    calls: list[str] = []
    ended: list[bool] = []

    async def renew(refresh: str) -> auth.Tokens:
        calls.append(refresh)
        return auth.Tokens("x", "y")
    r, view = reader_with(dialer, renew, on_end=lambda: ended.append(True))
    r.start()
    await until(lambda: not r.running)
    assert r.state.startswith("refused:") and "too many logins" in r.state
    assert view.bus()["state"] == r.state
    assert not r.holds_secrets
    assert calls == [] and len(dialer.tokens) == 1 and ended == []


async def test_a_passing_failure_waits_and_retries() -> None:
    """NATS unreachable, or Keycloak failing for a while: the reader waits,
    says so, and tries again with a fresh token."""
    dialer = Dialer("down")
    calls: list[int] = []

    async def renew(refresh: str) -> auth.Tokens:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("realm down")
        return auth.Tokens(f"access-{len(calls)}", refresh)
    r, _view = reader_with(dialer, renew)
    with mock.patch.object(nats_read, "BACKOFF_MAX_SEC", 0.05):
        r.start()
        try:
            await until(lambda: r.state.startswith("waiting"))
            await until(lambda: r.connects >= 1, timeout=8.0)
        finally:
            await r.aclose()
    assert dialer.tokens[0] == "first-access" and "first-access" not in dialer.tokens[1:]


async def test_a_cancel_during_the_close_still_wipes() -> None:
    """Logout while the connection's close hangs: key and tokens are gone
    anyway, the wipe never waits for the close."""
    dialer = Dialer()
    r, _view = reader_with(dialer, renew=None)
    r.start()
    await until(lambda: r.state == "reading")
    dialer.conns[0].close_gate = asyncio.Event()      # the close never ends
    r.stop()
    assert not r.holds_secrets                        # at once, before any await
    await asyncio.sleep(0.05)
    assert r._task is not None
    r._task.cancel()                                  # a second cancel, mid-close
    await until(lambda: not r.running)
    assert not r.holds_secrets


async def test_the_session_end_ends_the_reader() -> None:
    dialer = Dialer()
    ended: list[bool] = []
    r, _view = reader_with(dialer, renew=None, until_wall=time.time() + 0.2,
                           on_end=lambda: ended.append(True))
    r.start()
    await until(lambda: ended == [True])
    assert not r.holds_secrets


# --- the app: a reader per login, logout, refusal → /login, no static reader ---------

VERIFIED = auth.Operator(username="dembaca", display="D", groups=("operators",),
                         sub="sub-dembaca")


async def logged_in(c: Console, dialer: Dialer, cookie: str | None = None) -> str:
    async def fake_exchange(*a: Any, **k: Any) -> auth.Tokens:
        return auth.Tokens("login-access", "login-refresh")

    async def fake_verify(*a: Any, **k: Any) -> auth.Operator:
        return VERIFIED
    status, headers, _b = await get(c, "/login")
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(headers["location"]).query)["state"][0]
    flow = next(v for v in headers["__set-cookie__"]
                if v.startswith("choirbus_flow=")).split(";")[0]
    with mock.patch.object(auth, "exchange", fake_exchange), \
            mock.patch.object(auth, "verify", fake_verify), \
            mock.patch.object(PersonReader, "dial",
                              lambda self, *a: dialer(self, *a)):
        status, headers, _b = await get(c, f"/callback?code=x&state={state}",
                                        cookie=flow + (f"; {cookie}" if cookie else ""))
        assert status == 303
        new = next(v for v in headers["__set-cookie__"]
                   if v.startswith("choirbus_session=")).split(";")[0]
        view = c.sessions.view(new.split("=", 1)[1])
        await until(lambda: view.reader.state == "reading")
    return new


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
    await cb(online("agent-a", ack(f"p_{view.reader.login}_presence", 1)))
    status, _h, body = await get(console, "/v1/graph", cookie=cookie)
    assert status == 200 and "agent-a" in json.dumps(json.loads(body))
    assert console.fleet.cards() == []             # the stand-in stays empty
    status, _h, body = await get(console, "/v1/me", cookie=cookie)
    assert json.loads(body)["bus"]["state"] == "reading"
    # logout: the reader stops, key and tokens go
    status, _h, _b = await get(console, "/logout", cookie=cookie)
    await until(lambda: not view.reader.running)
    assert not view.reader.holds_secrets and dialer.conns[0].is_closed
    assert console.sessions.views() == []


async def test_a_second_login_from_one_browser_ends_the_first(console: Console) -> None:
    dialer = Dialer()
    first = await logged_in(console, dialer)
    old = console.sessions.views()[0]
    second = await logged_in(console, dialer, cookie=first)
    await until(lambda: not old.reader.running)
    assert not old.reader.holds_secrets and dialer.conns[0].is_closed
    assert console.sessions.views() == [console.sessions.view(second.split("=", 1)[1])]
    status, _h, _b = await get(console, "/v1/me", cookie=first)
    assert status == 401


async def test_a_persons_fifth_session_closes_the_oldest(console: Console) -> None:
    """Below the login service's cap of 8 logins per person: the console
    keeps at most four sessions of one person and closes the oldest."""
    dialer = Dialer()
    cookies = [await logged_in(console, dialer) for _ in range(5)]
    assert len(console.sessions.views()) == 4
    status, _h, _b = await get(console, "/v1/me", cookie=cookies[0])
    assert status == 401
    for cookie in cookies[1:]:
        status, _h, _b = await get(console, "/v1/me", cookie=cookie)
        assert status == 200
    assert dialer.conns[0].is_closed


async def test_the_services_cap_refusal_shows_and_stops(console: Console) -> None:
    """The login service refuses a login past its cap: the session shows
    why, reads nothing, and asks Keycloak for nothing more."""
    dialer = Dialer("authz")
    refreshed: list[str] = []

    async def renew(sub: str, refresh: str) -> auth.Tokens:
        refreshed.append(refresh)
        return auth.Tokens("x", "y")
    console._renew = renew  # type: ignore[method-assign]

    async def fake_exchange(*a: Any, **k: Any) -> auth.Tokens:
        return auth.Tokens("login-access", "login-refresh")

    async def fake_verify(*a: Any, **k: Any) -> auth.Operator:
        return VERIFIED
    status, headers, _b = await get(console, "/login")
    from urllib.parse import parse_qs, urlsplit
    state = parse_qs(urlsplit(headers["location"]).query)["state"][0]
    flow = next(v for v in headers["__set-cookie__"]
                if v.startswith("choirbus_flow=")).split(";")[0]
    with mock.patch.object(auth, "exchange", fake_exchange), \
            mock.patch.object(auth, "verify", fake_verify), \
            mock.patch.object(PersonReader, "dial", lambda self, *a: dialer(self, *a)):
        status, headers, _b = await get(console, f"/callback?code=x&state={state}",
                                        cookie=flow)
    cookie = next(v for v in headers["__set-cookie__"]
                  if v.startswith("choirbus_session=")).split(";")[0]
    view = console.sessions.views()[0]
    await until(lambda: not view.reader.running)
    status, _h, body = await get(console, "/v1/me", cookie=cookie)
    assert status == 200
    assert json.loads(body)["bus"]["state"].startswith("refused:")
    assert refreshed == [] and not view.reader.holds_secrets


async def test_a_refused_renewal_sends_the_browser_to_login(console: Console) -> None:
    dialer = Dialer()
    cookie = await logged_in(console, dialer)
    view = console.sessions.views()[0]

    async def refused(sub: str, refresh: str) -> auth.Tokens:
        assert (sub, refresh) == ("sub-dembaca", "login-refresh")
        raise auth.Refused("token endpoint refused the refresh")
    console._renew = refused  # type: ignore[method-assign]
    await dialer.conns[0].close()       # the connection ends: a fresh token is asked
    await until(lambda: not view.reader.running)
    status, _h, _b = await get(console, "/v1/me", cookie=cookie)
    assert status == 401                # the app goes to /login
    assert not view.reader.holds_secrets


async def test_the_renewal_checks_the_group_and_the_sub_again(console: Console) -> None:
    """``Console._renew``: a refreshed token is verified as at the login —
    without the group it is a refusal, and so is another ``sub``, even under
    the same username."""
    async def fresh(*a: Any, **k: Any) -> auth.Tokens:
        return auth.Tokens("new-access", "new-refresh")

    async def no_group(*a: Any, **k: Any) -> auth.Operator:
        raise auth.Refused("not a member of the operators group")

    async def same_name_other_sub(*a: Any, **k: Any) -> auth.Operator:
        return auth.Operator(username="dembaca", display="d", groups=("operators",),
                             sub="someone-else")
    with mock.patch.object(auth, "refresh", fresh), \
            mock.patch.object(auth, "verify", no_group):
        with pytest.raises(auth.Refused):
            await console._renew("sub-dembaca", "r")
    with mock.patch.object(auth, "refresh", fresh), \
            mock.patch.object(auth, "verify", same_name_other_sub):
        with pytest.raises(auth.Refused, match="another person"):
            await console._renew("sub-dembaca", "r")


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
