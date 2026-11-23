"""The gateway, the Mattermost stub and the HTTP-passing proxy, against the
real `websockets` client and a real `httpx` request.

Each test drives the same library the bus drives, because a stand-in would hide
exactly the error that matters here: `websockets`' `process_request` cannot read
a body, and a stub that only pretends to speak HTTP would not notice.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import websockets

from bus_testrig.gateway import Gateway
from bus_testrig.proxy import FrameProxy, GatewayProxy, ProxyFault
from bus_testrig.bus import RECEIVER_ID, RECEIVER_TOKEN, SENDER_ID, SENDER_TOKEN
from bus_testrig.stub import (CHANNEL, MattermostStub, posted_frame, reaction_added_frame,
                              user_added_frame)

CHALLENGE = json.dumps({"seq": 1, "action": "authentication_challenge",
                        "data": {"token": "t"}})


async def _ws_url(gateway_url: str) -> str:
    return gateway_url.replace("http", "ws") + "/api/v4/websocket"


async def _recv_quiet(client, timeout: float = 0.4) -> list[str]:
    """Every frame the client can read before the line falls quiet or closes."""
    got: list[str] = []
    try:
        while True:
            got.append(await asyncio.wait_for(client.recv(), timeout))
    except (asyncio.TimeoutError, websockets.ConnectionClosed):
        return got


async def test_the_gateway_answers_plain_http_and_reads_a_body():
    """A POST body is read, not raced: the constraint that rules out
    `websockets.process_request` for this side."""
    seen = {}

    async def on_http(method, target, headers, body):
        seen.update(method=method, target=target, body=body, header=headers.get("x-test"))
        return 200, {"content-type": "application/json"}, b'{"ok": true}'

    async with Gateway(on_http) as gateway:
        async with httpx.AsyncClient() as client:
            answer = await client.post(gateway.url + "/api/v4/posts?x=1",
                                       content=b'{"message": "hi"}', headers={"x-test": "yes"})
    assert answer.status_code == 200 and answer.json() == {"ok": True}
    assert seen == {"method": "POST", "target": "/api/v4/posts?x=1",
                    "body": b'{"message": "hi"}', "header": "yes"}


async def test_the_gateway_refuses_an_unknown_path_with_404():
    async with Gateway() as gateway:
        async with httpx.AsyncClient() as client:
            assert (await client.get(gateway.url + "/nope")).status_code == 404


async def test_the_gateway_survives_a_malformed_request():
    async with Gateway() as gateway:
        port = int(gateway.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"NOT A REQUEST\r\n\r\n")
        await writer.drain()
        assert (await reader.read(64)).startswith(b"HTTP/1.1 400")
        writer.close()
        # Still serving: one bad client does not take the port down.
        async with httpx.AsyncClient() as client:
            assert (await client.get(gateway.url + "/x")).status_code == 404


async def test_the_gateway_reads_a_body_split_across_reads():
    """`readexactly` across TCP packets: a body larger than one segment."""
    payload = b'{"message": "' + b"x" * 5000 + b'"}'

    async def on_http(_method, _target, _headers, body):
        return 200, {}, str(len(body)).encode()

    async with Gateway(on_http) as gateway:
        port = int(gateway.url.rsplit(":", 1)[1])
        head = (b"POST /api/v4/posts HTTP/1.1\r\nHost: x\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(head + payload[:100])
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(payload[100:])
        await writer.drain()
        answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
        length = int(dict(line.split(": ", 1) for line in
                          answer.decode("latin-1").split("\r\n")[1:]
                          if ": " in line)["Content-Length"])
        body = await asyncio.wait_for(reader.readexactly(length), 2)
        writer.close()
    assert body == str(len(payload)).encode()


async def test_the_websocket_handshake_is_accepted_by_the_real_client():
    async def on_ws(conn, _headers):
        await conn.send_text("hi")

    async with Gateway(on_ws=on_ws) as gateway:
        async with websockets.connect(await _ws_url(gateway.url)) as client:
            assert await asyncio.wait_for(client.recv(), 2) == "hi"


async def test_the_upgrade_answer_carries_the_rfcs_accept_header():
    """RFC 6455 §1.3: key `dGhlIHNhbXBsZSBub25jZQ==` must accept to
    `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`. Read off the wire the gateway wrote."""
    async def on_ws(conn, _headers):
        await conn.close(1000, "")

    async with Gateway(on_ws=on_ws) as gateway:
        port = int(gateway.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/v4/websocket HTTP/1.1\r\n"
                     b"Host: 127.0.0.1\r\n"
                     b"Upgrade: websocket\r\n"
                     b"Connection: Upgrade\r\n"
                     b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                     b"Sec-WebSocket-Version: 13\r\n\r\n")
        await writer.drain()
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        writer.close()
    assert head.startswith("HTTP/1.1 101 ")
    assert "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head


async def test_pings_are_answered_and_a_close_is_answered():
    async def on_ws(conn, _headers):
        await conn.recv()  # the text frame the client sends after its ping
        await conn.close(4001, "bye")

    async with Gateway(on_ws=on_ws) as gateway:
        async with websockets.connect(await _ws_url(gateway.url), ping_interval=None) as client:
            pong = await client.ping(b"x")
            assert await asyncio.wait_for(pong, 2) is not None
            await client.send("go")
            with pytest.raises(websockets.exceptions.ConnectionClosed) as caught:
                await asyncio.wait_for(client.recv(), 2)
            assert caught.value.rcvd.code == 4001 and caught.value.rcvd.reason == "bye"


async def test_the_gateway_sends_a_payload_with_the_126_length():
    payload = "x" * 400

    async def on_ws(conn, _headers):
        await conn.send_text(payload)

    async with Gateway(on_ws=on_ws) as gateway:
        async with websockets.connect(await _ws_url(gateway.url)) as client:
            assert await asyncio.wait_for(client.recv(), 2) == payload


async def test_the_gateway_reads_a_masked_client_frame():
    """The client's frames are masked; a server that forgot to unmask would
    read garbage instead of the challenge."""
    async def on_ws(conn, _headers):
        frame = await conn.recv()
        await conn.send_text(json.dumps({"got": json.loads(frame[1])}))

    async with Gateway(on_ws=on_ws) as gateway:
        async with websockets.connect(await _ws_url(gateway.url)) as client:
            await client.send('{"hello": "world"}')
            assert json.loads(await asyncio.wait_for(client.recv(), 2)) == {
                "got": {"hello": "world"}}


async def test_the_stub_answers_the_probes_endpoints_and_records_foreign_ones():
    async with MattermostStub() as stub:
        async with httpx.AsyncClient(base_url=stub.url) as client:
            me = await client.get("/api/v4/users/me")
            channel = await client.get(f"/api/v4/channels/{CHANNEL}")
            gap = await client.get("/api/v4/nothing")
    assert me.json()["username"] == "agent-probe"
    assert channel.json() == {"id": CHANNEL, "name": "agents", "type": "O"}
    assert gap.status_code == 404
    assert stub.unknown == [("GET", "/api/v4/nothing")]
    assert ("GET", "/api/v4/users/me") in stub.calls and len(stub.calls) == 3


async def test_a_known_token_names_its_own_account_and_an_id_read_is_not_me():
    """`/users/me` answers the bot its bearer names, and `/users/<id>` answers
    that id — not a fixed account.

    The id route's pattern matches `me` as well; when it ran first it answered
    `agent-me` for a token the stub knew, so `bus_info` reported the wrong bot
    and every coordinator measurement would have been about the wrong identity.
    The two servers of a run are told apart by their bearer, so a constant
    answer hides exactly the mix-up a run must not have.
    """
    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        stub.identify(RECEIVER_TOKEN, RECEIVER_ID, "agent-receiver")
        async with httpx.AsyncClient(base_url=stub.url) as client:
            mine = await client.get("/api/v4/users/me",
                                    headers={"authorization": f"Bearer {SENDER_TOKEN}"})
            theirs = await client.get("/api/v4/users/me",
                                      headers={"authorization": f"Bearer {RECEIVER_TOKEN}"})
            by_id = await client.get(f"/api/v4/users/{RECEIVER_ID}")
    assert mine.json()["username"] == "agent-sender" and mine.json()["id"] == SENDER_ID
    assert theirs.json()["username"] == "agent-receiver"
    assert by_id.json()["id"] == RECEIVER_ID
    assert stub.unknown == []


async def test_an_id_route_answers_only_an_id_this_stub_knows():
    """An id is `[a-z0-9]{26}`, and only a known one is answered.

    A loose pattern gave `/users/autocomplete`, `/users/stats`, `/users/known`
    and every invented id a 200 with `agent-lete` for a name — the stub answered
    what it does not know instead of reporting it, which is the one thing this
    stub exists not to do (review of #24). The same held for `/bots/<id>`, and
    neither route had a test; the PR text's "Measured" block is not one.
    """
    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        async with httpx.AsyncClient(base_url=stub.url) as client:
            other_endpoints = [await client.get(p) for p in (
                "/api/v4/users/autocomplete", "/api/v4/users/stats", "/api/v4/users/known")]
            invented = "q" * 26
            unknown_user = await client.get(f"/api/v4/users/{invented}")
            unknown_bot = await client.get(f"/api/v4/bots/{invented}")
            mine = await client.get(f"/api/v4/users/{SENDER_ID}")
            my_bot = await client.get(f"/api/v4/bots/{SENDER_ID}")
    assert [r.status_code for r in other_endpoints] == [404, 404, 404]
    assert unknown_user.status_code == 404 and unknown_bot.status_code == 404
    assert mine.json()["username"] == "agent-sender"
    assert my_bot.json()["user_id"] == SENDER_ID
    assert stub.unknown == [
        ("GET", "/api/v4/users/autocomplete"), ("GET", "/api/v4/users/stats"),
        ("GET", "/api/v4/users/known"), ("GET", f"/api/v4/users/{invented}"),
        ("GET", f"/api/v4/bots/{invented}"),
    ]


async def test_the_stub_answers_the_challenge_then_sends_its_frames():
    """What the bus sends (probe_bus_frames) and what it must read, on the wire."""
    post = {"id": "p" * 26, "user_id": "h" * 26, "channel_id": CHANNEL,
            "root_id": "", "message": "hello", "create_at": 1000}
    frames = [posted_frame(post), posted_frame(post | {"message": "object"}, as_object=True),
              reaction_added_frame({"user_id": "h" * 26, "post_id": "p" * 26,
                                    "emoji_name": "+1", "create_at": 7}),
              user_added_frame("u" * 26), {"event": "typing"}]
    async with MattermostStub(frames) as stub:
        async with websockets.connect(await _ws_url(stub.url)) as client:
            await client.send(CHALLENGE)
            got = await _recv_quiet(client)
    assert stub.authenticated
    assert stub.received[0]["action"] == "authentication_challenge"
    assert [json.loads(raw)["event"] for raw in got] == [
        "hello", "posted", "posted", "reaction_added", "user_added", "typing"]
    text, obj = json.loads(got[1]), json.loads(got[2])
    assert isinstance(text["data"]["post"], str) and json.loads(text["data"]["post"]) == post
    assert isinstance(obj["data"]["post"], dict)
    assert obj["data"]["post"] == post | {"message": "object"}


async def test_the_gateway_proxy_passes_http_through_unchanged():
    async with MattermostStub() as upstream:
        async with GatewayProxy(upstream.url) as proxy:
            async with httpx.AsyncClient(base_url=proxy.url) as client:
                answer = await client.get("/api/v4/users/me")
                body = await client.post("/api/v4/posts", json={"message": "hi"})
    assert answer.status_code == 200 and answer.json()["id"] == "m" * 26
    assert body.status_code == 200 and body.json()["channel_id"] == CHANNEL
    assert ("POST", "/api/v4/posts") in upstream.calls
    assert upstream.unknown == []


async def test_the_gateway_proxy_forwards_the_query_and_the_callers_headers():
    seen = {}

    async def on_http(method, target, headers, body):
        seen.update(method=method, target=target, header=headers.get("x-bus"))
        return 200, {"content-type": "application/json"}, b"{}"

    async with Gateway(on_http) as upstream:
        async with GatewayProxy(upstream.url) as proxy:
            async with httpx.AsyncClient(base_url=proxy.url) as client:
                await client.get("/api/v4/channels/x/posts?per_page=5&page=2",
                                 headers={"x-bus": "yes"})
    assert seen == {"method": "GET", "target": "/api/v4/channels/x/posts?per_page=5&page=2",
                    "header": "yes"}


async def test_the_gateway_proxy_faults_only_the_websocket():
    """A drop on the socket leaves HTTP untouched: the proxy's whole point."""
    frames = [posted_frame({"id": "p" * 26, "channel_id": CHANNEL, "message": f"m{i}"})
              for i in range(4)]
    async with MattermostStub(frames) as upstream:
        async with GatewayProxy(upstream.url, ProxyFault(kind="drop", after=2)) as proxy:
            async with httpx.AsyncClient(base_url=proxy.url) as client:
                assert (await client.get("/api/v4/users/me")).status_code == 200
            async with websockets.connect(await _ws_url(proxy.url)) as client:
                await client.send(CHALLENGE)
                got = await _recv_quiet(client)
    assert [json.loads(raw)["event"] for raw in got] == ["hello", "posted"]
    assert json.loads(json.loads(got[1])["data"]["post"])["message"] == "m0"
    assert proxy.altered == 3  # m1, m2, m3 dropped; hello and m0 passed


async def test_duplicate_through_the_gateway_doubles_only_the_faulted_frame():
    frames = [posted_frame({"id": "p" * 26, "channel_id": CHANNEL, "message": "x"})]
    async with MattermostStub(frames) as upstream:
        async with GatewayProxy(upstream.url, ProxyFault(kind="duplicate", after=1)) as proxy:
            async with websockets.connect(await _ws_url(proxy.url)) as client:
                await client.send(CHALLENGE)
                got = await _recv_quiet(client)
    assert [json.loads(raw)["event"] for raw in got] == ["hello", "posted", "posted"]
    assert proxy.forwarded == 3


async def test_a_client_leaving_ends_the_gateway_proxy_connection():
    async with MattermostStub([{"event": "typing"}]) as upstream:
        async with GatewayProxy(upstream.url) as proxy:
            client = await websockets.connect(await _ws_url(proxy.url))
            await client.send(CHALLENGE)
            assert json.loads(await asyncio.wait_for(client.recv(), 2))["event"] == "hello"
            await client.close()
            await asyncio.sleep(0.2)  # the handler must end, not hang


async def test_halfopen_stops_pongs_and_the_heartbeat_fails():
    """Acceptance: through the proxy with 0.5 s pings and a 0.5 s timeout, the
    client sees the connection fail because the pongs stop.

    `close_timeout` is the client's own wait for the close handshake: a frozen
    peer never answers the close, so `recv()` raises when that wait expires. It
    is 1 s so the test is short; the detection itself is `ping_timeout`."""
    async with MattermostStub([{"event": "typing"}]) as upstream:
        async with GatewayProxy(upstream.url, ProxyFault(kind="halfopen", after=1)) as proxy:
            async with websockets.connect(await _ws_url(proxy.url), ping_interval=0.5,
                                          ping_timeout=0.5, close_timeout=1) as client:
                await client.send(CHALLENGE)
                assert json.loads(await asyncio.wait_for(client.recv(), 2))["event"] == "hello"
                began = asyncio.get_running_loop().time()
                with pytest.raises((websockets.ConnectionClosed, ConnectionError)):
                    while True:
                        await asyncio.wait_for(client.recv(), 5)
                assert asyncio.get_running_loop().time() - began >= 0.4


async def test_halfopen_keeps_the_transport_writable():
    """The docstring claims writing still works: a frozen line accepts a write
    and delivers nothing, which is what a peer sees before its heartbeat dies."""
    async with MattermostStub([{"event": "typing"}]) as upstream:
        async with GatewayProxy(upstream.url, ProxyFault(kind="halfopen", after=1)) as proxy:
            async with websockets.connect(await _ws_url(proxy.url), ping_interval=None,
                                          close_timeout=1) as client:
                await client.send(CHALLENGE)
                await client.recv()
                await asyncio.wait_for(client.send("while frozen"), 1)  # no error
                assert proxy.altered == 1


async def test_halfopen_through_frame_proxy_also_stops_pongs():
    """The same fault on the WebSocket-only proxy (the echo run's path)."""
    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}",
                              ProxyFault(kind="halfopen", after=1)) as proxy:
            async with websockets.connect(proxy.url, ping_interval=0.5, ping_timeout=0.5,
                                          close_timeout=1) as client:
                await client.send("0")
                assert await asyncio.wait_for(client.recv(), 1) == "0"
                await client.send("1")  # the echo of this frame is the one frozen
                began = asyncio.get_running_loop().time()
                with pytest.raises((websockets.ConnectionClosed, ConnectionError)):
                    while True:
                        await asyncio.wait_for(client.recv(), 5)
                assert asyncio.get_running_loop().time() - began >= 0.4
    finally:
        server.close()


async def test_a_cut_through_the_gateway_aborts_without_a_close_frame():
    async def dying(conn):
        conn.transport.abort()

    server = await websockets.serve(dying, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # Only the socket is exercised: the upstream is a bare WebSocket server.
        async with GatewayProxy(f"http://127.0.0.1:{port}",
                                ProxyFault(kind="cut", after=0)) as proxy:
            async with websockets.connect(await _ws_url(proxy.url)) as client:
                with pytest.raises(websockets.exceptions.ConnectionClosedError) as caught:
                    for _ in range(5):
                        await asyncio.wait_for(client.recv(), 2)
                assert caught.value.rcvd is None
    finally:
        server.close()


async def test_the_gateway_proxy_carries_the_server_close_code():
    async def refusing(conn):
        await conn.close(4001, "wrong key")

    server = await websockets.serve(refusing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with GatewayProxy(f"http://127.0.0.1:{port}") as proxy:
            async with websockets.connect(await _ws_url(proxy.url)) as client:
                with pytest.raises(websockets.exceptions.ConnectionClosed) as caught:
                    await asyncio.wait_for(client.recv(), 2)
                assert caught.value.rcvd.code == 4001
                assert caught.value.rcvd.reason == "wrong key"
    finally:
        server.close()


async def test_the_gateway_proxy_answers_502_when_the_backend_is_gone():
    """Review point at proxy.py:419: an upstream that cannot be reached used to
    close without an answer. The caller now gets a 502 it can read as the
    upstream being gone, not a bare read error."""
    async with Gateway(on_http=lambda *_: None) as upstream:
        url = upstream.url
    async with GatewayProxy(url) as proxy:
        async with httpx.AsyncClient(base_url=proxy.url) as client:
            answer = await client.get("/api/v4/users/me")
    assert answer.status_code == 502
    assert "could not reach the backend" in answer.text


def test_halfopen_is_a_declared_fault():
    assert ProxyFault(kind="halfopen").kind == "halfopen"


async def test_the_stub_answers_a_channels_posts_from_what_it_holds():
    """Review point 3: the post list used to be a fixed empty answer, so the
    bus's catch-up read after a reconnect found nothing and 10/10 lost under
    `cut` measured the stub. The two posts it created come back, newest first.
    """
    async with MattermostStub() as stub:
        async with httpx.AsyncClient(base_url=stub.url) as client:
            first = (await client.post("/api/v4/posts", json={"message": "one"})).json()
            second = (await client.post("/api/v4/posts", json={"message": "two"})).json()
            read = (await client.get(f"/api/v4/channels/{CHANNEL}/posts")).json()
    assert read["order"] == [second["id"], first["id"]]
    assert read["posts"][first["id"]]["message"] == "one"
    assert stub.unknown == []


async def test_the_stubs_post_read_honours_since_and_per_page():
    """`since` keeps posts created at or after that millisecond and `per_page`
    caps the window: `client.posts_after` pages with exactly these."""
    async with MattermostStub() as stub:
        async with httpx.AsyncClient(base_url=stub.url) as client:
            first = (await client.post("/api/v4/posts", json={"message": "one"})).json()
            second = (await client.post("/api/v4/posts", json={"message": "two"})).json()
            since = (await client.get(f"/api/v4/channels/{CHANNEL}/posts",
                                      params={"since": second["create_at"]})).json()
            page = (await client.get(f"/api/v4/channels/{CHANNEL}/posts",
                                     params={"per_page": 1, "page": 1})).json()
    assert since["order"] == [second["id"]], since
    assert first["create_at"] < second["create_at"]
    assert page["order"] == [first["id"]], page


async def test_a_created_post_is_stamped_with_the_stubs_own_clock():
    """A fixed date older than a day made the bus's catch-up read skip the post
    it was meant to recover (its watermark floor is 24 h back)."""
    before = int(time.time() * 1000)
    async with MattermostStub() as stub:
        async with httpx.AsyncClient(base_url=stub.url) as client:
            made = (await client.post("/api/v4/posts", json={"message": "now"})).json()
    assert before <= made["create_at"] <= int(time.time() * 1000) + 1


def test_post_of_refuses_an_id_the_stub_never_created():
    """Review point: `post_of` used to invent a blank post. A run would then
    measure against a post the bus never made."""
    stub = MattermostStub()
    with pytest.raises(KeyError, match="created no post"):
        stub.post_of("p" * 26)


async def test_an_unexpected_websocket_frame_is_recorded_not_dropped():
    """Review point 7: a first frame that is not the challenge, text that is
    not JSON and an unknown action used to vanish. Each is a line in
    `.unknown`, marked `WS` as REST's are."""
    async with MattermostStub() as stub:
        async with websockets.connect(await _ws_url(stub.url)) as not_json:
            await not_json.send("this is not JSON")
            await asyncio.sleep(0.1)
        async with websockets.connect(await _ws_url(stub.url)) as wrong_action:
            await wrong_action.send(json.dumps({"action": "no_such_action"}))
            await asyncio.sleep(0.1)
        async with websockets.connect(await _ws_url(stub.url)) as later:
            await later.send(CHALLENGE)
            await asyncio.sleep(0.05)
            await later.send(json.dumps({"action": "also_unknown"}))
            await asyncio.sleep(0.1)
    marks = [entry for entry in stub.unknown if entry[0] == "WS"]
    assert any("not JSON" in why for _, why in marks), marks
    assert any("no_such_action" in why for _, why in marks), marks
    assert any("also_unknown" in why for _, why in marks), marks


async def test_a_push_into_a_frozen_socket_is_bounded():
    """Review point 6: under `halfopen` a write never drains, so `push` used to
    block until the job's own timeout. It gives up after PUSH_SEC and says why.
    """
    async with MattermostStub() as upstream:
        async with GatewayProxy(upstream.url,
                                ProxyFault(kind="halfopen", after=1)) as proxy:
            async with websockets.connect(await _ws_url(proxy.url),
                                          ping_interval=None) as client:
                await client.send(CHALLENGE)
                await client.recv()
                # Fill the client's buffer and stop reading it, so the stub's
                # write cannot drain.
                client.transport.pause_reading()
                began = asyncio.get_running_loop().time()
                big = {"event": "posted", "data": {"post": "x" * 200_000}}
                sent = 0
                while not upstream.closed:
                    sent += await upstream.push(big)
                    if asyncio.get_running_loop().time() - began > 20:
                        break
    assert upstream.closed, "a frozen socket must be named, not waited on"
    assert "did not take a frame" in upstream.closed

