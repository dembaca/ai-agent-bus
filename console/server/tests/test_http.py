"""The console's HTTP surface: the heap the hook API taught (bounded
headers, ordered answers), the routes of ADR-015 point 10, the cookie
session and the fleet's server-sent events — served by the raw asyncio
httpd, asked by a raw client, no NATS and no Keycloak in sight.

The login itself needs no Keycloak here: the flow is driven with the
same seams the tests use for NATS and JWKS, and ``CONSOLE_LOGIN_OFF``
stands the operator in (loopback only, tests and the mock).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from console_server import auth, config
from console_server.app import COOKIE, Console

SETTINGS = config.Settings(public_url="http://127.0.0.1:8747",
                          login_off=True, nats_url="")


async def client(console: Console) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    port = console._http.bound_port
    return await asyncio.open_connection("127.0.0.1", port)


async def get(console: Console, path: str, cookie: str | None = None,
              origin: str | None = None,
              sec_fetch_site: str | None = None) -> tuple[int, dict[str, object], bytes]:
    reader, writer = await client(console)
    head = f"GET {path} HTTP/1.1\r\nHost: x\r\n"
    if cookie:
        head += f"Cookie: {cookie}\r\n"
    if origin:
        head += f"Origin: {origin}\r\n"
    if sec_fetch_site:
        head += f"Sec-Fetch-Site: {sec_fetch_site}\r\n"
    writer.write((head + "\r\n").encode())
    raw = await asyncio.wait_for(_read_answer(reader), 5.0)
    writer.close()
    return raw


async def _read_answer(reader: asyncio.StreamReader
                       ) -> tuple[int, dict[str, object], bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers: dict[str, object] = {k.strip().lower(): v.strip()
                                  for k, v in (l.partition(":")[0::2]
                                               for l in lines[1:-1])}
    cookies = [v.strip() for l in lines[1:-1]
               if l.split(":")[0].strip().lower() == "set-cookie"
               for v in [l.partition(":")[2]]]
    headers["__set-cookie__"] = cookies
    if headers.get("content-type") == "text/event-stream":
        return status, headers, b""  # the stream holds the connection
    if headers.get("content-type").endswith("json"):  # type: ignore[union-attr]
        assert int(headers["content-length"]) > 0
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    return status, headers, body


@pytest.fixture
async def console() -> Console:
    c = Console(SETTINGS)
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


async def test_healthz(console: Console) -> None:
    status, headers, body = await get(console, "/healthz")
    assert status == 200
    assert body == b"ok"


async def test_me_answers_the_stand_in_operator(console: Console) -> None:
    status, _headers, body = await get(console, "/v1/me")
    assert status == 200
    doc = json.loads(body)
    assert doc["username"] == "nobody"
    assert doc["display_name"]
    assert doc["groups"] == []


async def test_asks_and_stats_answer_empty_until_nats_carries_them(
        console: Console) -> None:
    """RT-6.43 steps 6 and 8: no ask record, no asks; no STATS line, every value n/a."""
    status, _headers, body = await get(console, "/v1/asks")
    assert status == 200 and json.loads(body) == []
    status, _headers, body = await get(console, "/v1/stats")
    reports = json.loads(body)
    assert status == 200 and [r["id"] for r in reports] == ["share-per-version", "asks-24h", "asks-7d",
                                                                 "traffic-by-category"]
    assert all(f["value"] is None for r in reports for f in r["fields"])


async def test_threads_answer_an_empty_page(console: Console) -> None:
    status, _headers, body = await get(console, "/v1/threads/anything")
    assert status == 200
    assert json.loads(body) == {"posts": [], "next": None}


async def test_graph_of_an_empty_fleet(console: Console) -> None:
    status, _headers, body = await get(console, "/v1/graph")
    assert status == 200
    assert json.loads(body) == {"nodes": [], "edges": [], "cutDepth": None}


async def test_fleet_streams_sse(console: Console) -> None:
    reader, writer = await client(console)
    writer.write(b"GET /v1/fleet HTTP/1.1\r\nHost: x\r\n\r\n")
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
    assert b"Content-Type: text/event-stream" in head
    # with CONSOLE_NATS unset the console serves an honest empty fleet
    line = await asyncio.wait_for(reader.readline(), 5.0)
    event = json.loads(line[len(b"data: "):])
    assert event == {"type": "snapshot", "cards": []}
    writer.close()


async def test_static_web_and_no_traversal(console: Console) -> None:
    object.__setattr__(console.settings, "web_dir", "")  # nothing set: 404
    status, _headers, body = await get(console, "/")
    assert status == 404

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = __import__("pathlib").Path(tmp)
        (root / "index.html").write_text("<html>Choirbus</html>")
        (root / "assets").mkdir()
        (root / "assets" / "app.js").write_text("console.log(1)")
        object.__setattr__(console.settings, "web_dir", str(root))
        status, _h, body = await get(console, "/")
        assert status == 200 and b"Choirbus" in body
        status, _h, body = await get(console, "/assets/app.js")
        assert status == 200 and body == b"console.log(1)"
        # ..%2f stays outside the built app: no traversal, the app again
        status, _h, body = await get(console, "/%2e%2e/etc/passwd")
        assert status in (404, 200)
        if status == 200:
            assert body == b"<html>Choirbus</html>"


async def test_every_v1_route_answers_401_without_a_session() -> None:
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")
    async def fake_discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=fake_discover)
    try:
        for path in ("/v1/me", "/v1/asks", "/v1/stats", "/v1/graph",
                     "/v1/threads/x"):
            status, headers, _b = await get(c, path)
            assert status == 401, path
            assert headers.get("cache-control") == "no-store", path
            assert "nosniff" in headers.get("x-content-type-options", ""), path
        status, _h, _b = await get(c, "/")
        assert status == 303  # the browser app goes to the login first
    finally:
        await c.stop()


async def test_a_fleet_stream_counts_per_session() -> None:
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")
    async def fake_discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=fake_discover)
    try:
        token = c.sessions.issue(auth.Operator(username="dembaca", display="d",
                                               groups=("operators",)), 3600)
        cookie = f"choirbus_session={token}"
        for _ in range(4):
            status, _h, _b = await get(c, "/v1/fleet", cookie=cookie)
            assert status in (200, None)  # the stream stays open
        # the fifth stream of the same operator answers 429
        reader, writer = await asyncio.open_connection("127.0.0.1",
                                                      c._http.bound_port)
        writer.write(f"GET /v1/fleet HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\n\r\n".encode())
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
        assert b"429" in head.split(b"\r\n")[0]
        writer.close()
    finally:
        await c.stop()


async def test_unknown_route_is_not_found(console: Console) -> None:
    status, _headers, _body = await get(console, "/nowhere")
    assert status == 404


async def test_the_login_flow_sets_the_cookie(tmp_path) -> None:
    """The driven flow: a state redirects to Keycloak, the callback's code
    becomes a verified operator and a cookie; every step with forged input
    is refused."""
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")

    async def fake_discover(issuer: str) -> auth.Endpoints:
        assert issuer == settings.issuer
        return c.endpoints
    await c.start(discover=fake_discover)
    try:
        # /v1/me without a cookie: login required
        status, _h, _b = await get(c, "/v1/me")
        assert status == 401

        status, headers, _b = await get(c, "/login")
        assert status == 303
        where = headers.get("location", "")
        assert "code_challenge_method=S256" in where
        set_cookies = [v for v in headers["__set-cookie__"]  # type: ignore[union-attr]
                       if isinstance(v, str)]
        flow = next((v for v in set_cookies if v.startswith("choirbus_flow=")), "")
        assert flow.startswith("choirbus_flow=") and "HttpOnly" in flow

        # the forged callback: no state this console issued
        status, _h, _b = await get(c, "/callback?code=x&state=forged")
        assert status == 400

        # the real one, driven: the state is in the redirect
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(where).query)["state"][0]
        assert state in c._pending

        verified = auth.Operator(username="dembaca", display="D", groups=("operators",))
        import unittest.mock as mock

        async def fake_exchange(*args, **kwargs):
            return auth.Tokens(access="a-token", refresh="r-token")

        async def fake_verify(*args, **kwargs):
            return verified

        with mock.patch.object(auth, "exchange", fake_exchange), \
                mock.patch.object(auth, "verify", fake_verify):
            # without the flow cookie the state is not ours: refused
            status, _h, _b = await get(c, f"/callback?code=x&state={state}")
            assert status == 400
            status, headers, _b = await get(c, f"/callback?code=x&state={state}",
                                            cookie=flow.split(";")[0])
        assert status == 303
        cookies = [v for v in headers["__set-cookie__"]  # type: ignore[union-attr]
                   if v.startswith(COOKIE + "=")]
        assert len(cookies) == 1
        cookie = cookies[0]
        assert "HttpOnly" in cookie
        # the cookie holds the session: /v1/me names the operator
        status, _h, body = await get(c, "/v1/me",
                                     cookie=cookie.split(";")[0])
        assert status == 200
        assert json.loads(body)["username"] == "dembaca"

        # logout ends the session on the server: the cookie, even copied,
        # is worthless afterwards
        status, _h, _b = await get(c, "/logout", cookie=cookie.split(";")[0])
        assert status == 303
        status, _h, _b = await get(c, "/v1/me", cookie=cookie.split(";")[0])
        assert status == 401
    finally:
        await c.stop()


async def test_a_busy_stream_ends_with_its_session() -> None:
    """The reviews' finding: presence heartbeats keep a stream awake, so
    the session may be checked only on every send — after a logout the
    stream closes with its next event, however busy the fleet is."""
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="nats://127.0.0.1:4")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")

    async def fake_discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=fake_discover)
    try:
        token = c.sessions.issue(auth.Operator(username="dembaca", display="d",
                                               groups=("operators",)), 3600)
        cookie = f"choirbus_session={token}"
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", c._http.bound_port)
        writer.write(f"GET /v1/fleet HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\n\r\n".encode())
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
        line = await asyncio.wait_for(reader.readline(), 10.0)
        assert b'"type":"snapshot"' in line
        c.fleet.synced = True
        # a busy fleet: a change arrives and is delivered…
        c.fire()
        line = await asyncio.wait_for(reader.readline(), 10.0)
        assert line.startswith(b"data: ") or line in (b"\n",)
        # …the session ends…
        c.sessions.drop(token)
        # …and the next change closes the stream, beat or no beat
        c.fire()
        raw = await asyncio.wait_for(reader.read(), 10.0)
        assert raw == b""  # closed, not another event
    finally:
        await c.stop()


async def test_a_cross_site_page_cannot_log_the_operator_out() -> None:
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")

    async def fake_discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=fake_discover)
    try:
        token = c.sessions.issue(auth.Operator(username="dembaca", display="d",
                                               groups=("operators",)), 3600)
        cookie = f"choirbus_session={token}"
        status, _h, _b = await get(c, "/logout", cookie=cookie,
                                   origin="http://evil.example")
        assert status == 403
        assert c.sessions.get(token) is not None  # the session stands
        # a cross-site <img> sends no Origin at all — Fetch Metadata says it
        status, _h, _b = await get(c, "/logout", cookie=cookie,
                                   sec_fetch_site="cross-site")
        assert status == 403
        assert c.sessions.get(token) is not None
        # the console's own origin (or none, a typed address bar) is fine
        status, _h, _b = await get(c, "/logout", cookie=cookie,
                                   origin="http://127.0.0.1:8747")
        assert status == 303
        assert c.sessions.get(token) is None
    finally:
        await c.stop()


async def test_a_flood_of_logins_never_evicts_states() -> None:
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    c.endpoints = auth.Endpoints(issuer=settings.issuer,
                                 authorization="http://127.0.0.1:8747/kc/auth",
                                 token="http://127.0.0.1:8747/kc/token",
                                 jwks="http://127.0.0.1:8747/kc/certs")

    async def fake_discover(issuer: str) -> auth.Endpoints:
        return c.endpoints  # type: ignore[return-value]
    await c.start(discover=fake_discover)
    try:
        status, headers, _b = await get(c, "/login")
        assert status == 303
        state_before = {k: v for k, v in c._pending.items()}
        assert state_before
        # a flood of further /login requests: refused without creating,
        # never evicting the first state
        seen_429 = False
        for _ in range(40):
            status, _h, _b = await get(c, "/login")
            if status == 429:
                seen_429 = True
                break
        assert seen_429
        for k, v in state_before.items():
            assert c._pending.get(k) == v
    finally:
        await c.stop()


async def test_transport_trouble_answers_502_and_retries() -> None:
    """The reviews' third: an unreachable issuer never ends the discovery,
    and the callback answers 502 instead of dropping the connection."""
    import httpx
    settings = config.Settings(public_url="http://127.0.0.1:8747",
                               issuer="http://127.0.0.1:8747/kc",
                               client_id="console", operator_group="operators",
                               nats_url="")
    c = Console(settings)
    calls = {"n": 0}

    async def flaky_discover(issuer: str) -> auth.Endpoints:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("issuer down")
        return auth.Endpoints(issuer=issuer,
                              authorization=f"{issuer}/auth",
                              token=f"{issuer}/token",
                              jwks=f"{issuer}/certs")
    await c.start(discover=flaky_discover)
    try:
        # before the issuer answers: /login is 503, the loop alive
        status, _h, _b = await get(c, "/login")
        assert status == 503
        assert c._discovery is not None and not c._discovery.done()
        from asyncio import sleep as asleep
        for _ in range(40):
            await asleep(0.3)
            if c.endpoints is not None:
                break
        assert c.endpoints is not None  # the loop got through
        # an /login needs its endpoints for a state
        status, _h, _b = await get(c, "/login")
        assert status == 303
        # the callback: an unreachable token endpoint answers 502
        from urllib.parse import parse_qs, urlsplit
        status, headers, _b = await get(c, "/login")
        where = headers.get("location", "")
        state = parse_qs(urlsplit(where).query)["state"][0]
        flow = next(v for v in headers["__set-cookie__"]  # type: ignore[union-attr]
                    if isinstance(v, str) and v.startswith("choirbus_flow="))
        import unittest.mock as mock

        async def dead_exchange(*args, **kwargs):
            raise httpx.ConnectError("token endpoint down")

        with mock.patch.object(auth, "exchange", dead_exchange):
            status, _h, body = await get(c, f"/callback?code=x&state={state}",
                                         cookie=flow.split(";")[0])
        assert status == 502
    finally:
        await c.stop()


async def test_a_pending_delivery_stamps_no_sync() -> None:
    """A delivery whose replay is still behind (pending > 0) is not a
    finished replay: it stamps nothing, the probe stays red until a
    delivery with pending 0 or a create with nothing left arrives."""
    from console_server.fleet import Fleet
    f = Fleet(presence_subject="presence")
    def ack(sseq, cseq, ts, pending):
        return (f"$JS.ACK.PRESENCE.console.{sseq}.{sseq}.{cseq}.{int(ts*1e9)}.{pending}")

    # presence: pending 2 behind — not synced
    assert f.on_delivery("presence.dembaca.a", b'{"v":1,"status":"online"}',
                         ack(1, 1, 1000.0, 2), "PRESENCE", "console")
    assert f.synced is False and f.last_sync_at is None

    # the next delivery catches up: pending 0 — synced
    assert f.on_delivery("presence.dembaca.b", b'{"v":1,"status":"online"}',
                         ack(2, 2, 1001.0, 0), "PRESENCE", "console")
    assert f.synced is True and f.last_sync_at is not None


async def test_an_aged_card_reaches_the_stream_on_the_beat() -> None:
    """The keep-alive beat carries aging: a presence that the floored
    clock ages out (no delivery for it) goes to the operator as a `card`
    event — offline — within one beat, not only at the next delivery."""
    from console_server import config
    from console_server.app import Console

    c = Console(config.Settings(public_url="http://127.0.0.1:8747",
                                login_off=True,
                                nats_url="nats://127.0.0.1:4"))
    await c.start()
    try:
        import time as _t
        # an agent's presence: fresh on arrival (85 s of the 90 s window
        # gone at the anchor), so the first snapshot shows it online —
        # the beat 15 s later ages it past the window, and the diff
        # carries the offline card without any delivery
        c.fleet._presence["agent-a"] = (2000.0, "online")
        c.fleet._presence_seq["agent-a"] = (1, 2000.0)
        c.fleet.last_server_now_wall = (2000.0, _t.monotonic() - 85.0)
        c.fleet.synced = True
        token = c.sessions.issue(auth.Operator(username="d", display="d",
                                               groups=()), 3600)
        cookie = f"choirbus_session={token}"

        reader, writer = await asyncio.open_connection(
            "127.0.0.1", c._http.bound_port)
        writer.write(("GET /v1/fleet HTTP/1.1\r\nHost: x\r\nCookie: " + cookie + "\r\n\r\n").encode())
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)

        async def events(n, timeout=35.0):
            out = []
            deadline = asyncio.get_event_loop().time() + timeout
            while len(out) < n:
                line = await asyncio.wait_for(
                    reader.readline(), deadline - asyncio.get_event_loop().time())
                if line.startswith(b"data: "):
                    out.append(json.loads(line[6:]))
                if asyncio.get_event_loop().time() > deadline:
                    break
            return out

        first = (await events(1))[0]
        assert first["type"] == "snapshot"
        assert "agent-a" in [card["name"] for card in first["cards"]]
        # the stream's seen set now holds the online card; the aging
        # happens on the floored clock as time passes — with the
        # backdated anchor the card is already offline on the next diff
        # (the beat fires at SSE_KEEPALIVE = 15 s)
        cards = await events(1)
        assert cards and cards[0]["type"] == "card"
        assert cards[0]["card"]["name"] == "agent-a"
        assert cards[0]["card"]["state"] == "offline"
        writer.close()
    finally:
        await c.stop()
