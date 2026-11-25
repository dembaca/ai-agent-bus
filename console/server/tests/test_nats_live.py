"""The console's NATS side against a real nats-server (2.15.3, as the mcp
transport tests: ``MM_TEST_NATS=1`` and a binary in ``MM_NATS_SERVER``;
CI downloads it, this sandbox keeps one under scratchpad/nats).

The lab in miniature: one tenant account, an admin that makes the
PRESENCE stream, two agents that publish presence, claims and
memberships exactly as the login service permits them — and the
console's read-only account, which may create its own consumer and read,
and publish nothing else. The fleet the browser sees over SSE comes out
whole, live and correct.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone

import pytest
import nkeys
from nats.aio.client import Client as NatsClient

from console_server import config, httpd
from console_server.app import Console
from console_server.fleet import short_name
from mattermost_ai_agent_bus import natsbus
from mattermost_ai_agent_bus.natsbus import decode_presence, encode_presence

pytestmark = pytest.mark.skipif(
    os.environ.get("MM_TEST_NATS") != "1" or not (
        os.environ.get("MM_NATS_SERVER") or shutil.which("nats-server")),
    reason="needs MM_TEST_NATS=1 and a nats-server binary")

PRESENCE_STREAM = {
    "name": "PRESENCE",
    "subjects": ["presence.>"],
    "retention": "limits",
    "max_age": 24 * 3600 * 1_000_000_000,
    "max_msgs_per_subject": 1,
    "max_msg_size": 4096,
    "no_ack": True,
    "discard": "new",
    "duplicate_window": 100 * 1_000_000,
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def rig(tmp_path):
    binary = os.environ.get("MM_NATS_SERVER") or shutil.which("nats-server")
    port = free_port()
    # the console's read-only account: it may make its own presence
    # consumer and read its deliver subject, and nothing else
    seed = nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER).decode()
    seed_file = tmp_path / "console.seed"
    seed_file.write_text(seed)
    console_pub = nkeys.from_seed(seed.encode()).public_key.decode()
    admin_pw = os.urandom(8).hex()
    conf = tmp_path / "nats.conf"
    conf.write_text(
        "port: %d\nhost: 127.0.0.1\n" % port
        + 'jetstream { store_dir: "%s" }\n' % (tmp_path / "js")
        + "accounts { BUS { jetstream: enabled\n users: ["
        + '{ user: admin, password: "%s"},' % admin_pw
        + '{ nkey: "%s", permissions: {' % console_pub
        + 'publish: { allow: ["$JS.API.CONSUMER.CREATE.PRESENCE.console",'
        + ' "$JS.API.CONSUMER.DELETE.PRESENCE.console"]},'
        + 'subscribe: { allow: ["deliver.console.presence", "_INBOX_console.>"]}'
        + "} }] } }\n")
    proc = subprocess.Popen([binary, "-c", str(conf)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "nats://127.0.0.1:%d" % port
    admin = NatsClient()
    try:
        for _ in range(100):
            try:
                await admin.connect(url, user="admin", password=admin_pw,
                                    allow_reconnect=False)
                break
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.1)
        else:
            pytest.fail("nats-server did not start")
        reply = await admin.request("$JS.API.STREAM.CREATE.PRESENCE",
                                    json.dumps(PRESENCE_STREAM).encode(), timeout=5.0)
        body = json.loads(reply.data)
        assert not body.get("error"), body
        yield {"url": url, "admin": admin, "seed_file": str(seed_file)}
    finally:
        await admin.close()
        proc.terminate()
        proc.wait(10)


def settings_of(rig) -> config.Settings:
    return config.Settings(public_url="http://127.0.0.1:8747",
                           login_off=True,
                           nats_url=rig["url"],
                           nats_seed_file=str(rig["seed_file"]))


async def until(pred, timeout: float = 8.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out" + (f": {what}" if what else ""))
        await asyncio.sleep(0.05)


class Stream:
    """A raw client of one fleet stream: the head, then the events. The
    console listens before `start()` returns, so no connect race exists —
    the flake (#105) was a card assertion breaking one event early, fixed
    in the tests that read cards."""

    def __init__(self, console: Console) -> None:
        self.console = console

    async def open(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.console._http.bound_port)
        self.writer.write(b"GET /v1/fleet HTTP/1.1\r\nHost: x\r\n\r\n")
        await asyncio.wait_for(self.reader.readuntil(b"\r\n\r\n"), 5.0)

    async def event(self, timeout: float = 10.0) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        while line in (b"\n", b"\r\n") or line.startswith(b":"):  # blanks and beats
            line = await asyncio.wait_for(self.reader.readline(), timeout)
        return json.loads(line[len(b"data: "):])

    def close(self) -> None:
        self.writer.close()


async def test_the_console_reads_the_fleet_sse(rig) -> None:
    admin: NatsClient = rig["admin"]
    console = Console(settings_of(rig))
    await console.start()
    try:
        stream = Stream(console)
        await stream.open()
        await admin.publish("presence.dembaca.agent-one", encode_presence("online", "bot1"))
        await admin.publish("presence.member.dev.agent-one", b'{"v": 1}')
        await admin.publish("presence.crown.dev.agent-one", b'{"v": 1}')
        await admin.publish("presence.dembaca.agent-two", encode_presence("away"))

        # the replay reaches the console: a snapshot, then card events.
        # The flake (#105) was here: breaking on agent-one's FIRST card
        # races the claim's own card event — wait for its crown instead
        # (presence, member and claim replay in one burst), bounded by
        # the stream's events.
        cards: dict[str, dict] = {}
        for _ in range(30):
            event = await stream.event()
            if event["type"] == "snapshot":
                cards = {c["name"]: c for c in event["cards"]}
            elif event["type"] == "card":
                cards[event["card"]["name"]] = event["card"]
            # and agent-two's own card, which may come after the crown's
            if cards.get("agent-one", {}).get("crown") is True and "agent-two" in cards:
                break
        assert cards["agent-one"]["state"] == "online"
        assert cards["agent-one"]["crown"] is True
        assert cards["agent-one"]["home"] == "dev"
        assert cards["agent-one"]["for"] == "dembaca"
        assert cards["agent-two"]["state"] == "away"
        assert cards["agent-two"]["ctx"] is None
        assert cards["agent-two"]["openAsks"] is None  # unknown, never 0
        assert short_name("agent-one") == "agent-one"

        # a change arrives as one "card" event per change, the app's alphabet
        await admin.publish("presence.dembaca.agent-two", encode_presence("online"))
        changed = None
        for _ in range(10):
            event = await stream.event()
            if event["type"] == "card" and event["card"]:
                changed = event["card"]
                break
        assert changed is not None
        assert changed["name"] == "agent-two"
        assert changed["state"] == "online"

        # the graph answers the API too (raw, same facts)
        answer = await console.handle("/v1/graph", {}, {})
        assert answer and answer.status == 200
        graph = json.loads(answer.body)
        names = {n["id"] for n in graph["nodes"]}
        assert {"dembaca", "dev", "agent-one", "agent-two"} <= names
        edges = {(e["from"], e["to"]) for e in graph["edges"]}
        assert ("agent-one", "dev") in edges
        stream.close()
    finally:
        await console.stop()


async def test_a_restart_replays_the_fleet(rig) -> None:
    """The reviews' restart probe: a consumer recreated under its own name
    replays nothing, so the console deletes it first — a restart finds the
    fleet (presence, membership, crown) as the stream holds it."""
    admin: NatsClient = rig["admin"]
    await admin.publish("presence.dembaca.agent-one", encode_presence("online"))
    await admin.publish("presence.member.dev.agent-one", b'{"v": 1}')
    await admin.publish("presence.crown.dev.agent-one", b'{"v": 1}')

    console = Console(settings_of(rig))
    await console.start()
    try:
        stream = Stream(console)
        await stream.open()
        cards = {}
        for _ in range(30):
            event = await stream.event()
            if event["type"] == "snapshot":
                cards = {c["name"]: c for c in event["cards"]}
            elif event["type"] == "card":
                cards[event["card"]["name"]] = event["card"]
            if cards.get("agent-one", {}).get("crown") is True:
                break
        assert cards["agent-one"]["crown"] is True
        stream.close()

        # the restart: the old connection and its consumer die; the state
        # lives in the stream, and the replay of the new consumer renews it
        await console.stop()
        await console.start()
        again = Stream(console)
        await again.open()
        fresh = {}
        for _ in range(30):
            event = await again.event()
            if event["type"] == "snapshot":
                fresh = {c["name"]: c for c in event["cards"]}
            elif event["type"] == "card":
                fresh[event["card"]["name"]] = event["card"]
            if fresh.get("agent-one", {}).get("crown") is True:
                break
        assert fresh["agent-one"]["crown"] is True
        assert fresh["agent-one"]["home"] == "dev"
        again.close()
    finally:
        await console.stop()


async def test_the_account_publishes_nothing(rig) -> None:
    """The read-only account's grant: it cannot publish on the bus's
    subjects — a probe that a wider grant would catch."""
    import nats
    seed = open(rig["seed_file"]).read()
    nc = await nats.connect(servers=[rig["url"]], nkeys_seed_str=seed,
                            inbox_prefix="_INBOX_console",
                            allow_reconnect=False, connect_timeout=5.0)
    refused = asyncio.get_running_loop().create_future()

    async def err_cb(exc: BaseException) -> None:
        if not refused.done():
            refused.set_result(exc)

    nc._error_cb = err_cb
    try:
        await nc.publish("presence.dembaca.spy", b'{"v": 1}')
        await nc.flush(2.0)
        got = await asyncio.wait_for(refused, 5.0)
        assert "permission" in str(got).lower() or "violation" in str(got).lower()
    finally:
        await nc.close()


# --- the pilot's ageing and crown handover (#108 points 10 and 11) -------------------


async def _cards_until(stream: Stream, cards: dict[str, dict], done,
                       events: int = 60, seen: list | None = None) -> None:
    """Fold the stream's events into ``cards`` until ``done(cards)``;
    each folded state is appended to ``seen`` (a copy) when given."""
    for _ in range(events):
        event = await stream.event()
        if event["type"] == "snapshot":
            cards.clear()
            cards.update({c["name"]: c for c in event["cards"]})
        elif event["type"] == "card":
            cards[event["card"]["name"]] = event["card"]
        elif event["type"] == "drop":
            cards.pop(event["name"], None)
        if seen is not None:
            seen.append((event, {n: dict(c) for n, c in cards.items()}))
        if done(cards):
            return
    raise AssertionError(f"the stream never got there: {cards}")


def _state(cards: dict[str, dict], name: str) -> str | None:
    return cards.get(name, {}).get("state")


async def test_a_hard_death_ages_the_card_offline_and_no_other(rig, monkeypatch) -> None:
    """#108 point 10: an agent killed hard (kill -9) falls silent and
    sends no bye; its card turns offline once its last presence is older
    than the freshness window (natsbus.PRESENCE_FRESH_SEC, 90 s in
    production; the console sends the change with the next delivery or
    SSE beat). The window is shortened to 2 s here; the peer beats every
    0.5 s (30 s / 90 s in production) and its card never changes state."""
    window = 2.0
    monkeypatch.setattr(natsbus, "PRESENCE_FRESH_SEC", window)
    admin: NatsClient = rig["admin"]
    console = Console(settings_of(rig))
    await console.start()
    beating = True

    async def peer() -> None:
        while beating:
            await admin.publish("presence.dembaca.agent-live", encode_presence("online"))
            await asyncio.sleep(0.5)

    beat = asyncio.ensure_future(peer())
    try:
        stream = Stream(console)
        await stream.open()
        await admin.publish("presence.member.dev.agent-dead", b'{"v": 1}')
        await admin.publish("presence.member.dev.agent-live", b'{"v": 1}')
        for n in range(3):  # a few heartbeats, then the kill: silence, no bye
            if n:
                await asyncio.sleep(0.3)
            await admin.publish("presence.dembaca.agent-dead", encode_presence("online"))
            await admin.flush()
        died = time.monotonic()  # right after the last presence reached the server
        cards: dict[str, dict] = {}
        await _cards_until(stream, cards, lambda c: _state(c, "agent-dead") == "online"
                           and _state(c, "agent-live") == "online")
        assert time.monotonic() - died < window, "online seen only after the window"

        seen: list = []
        await _cards_until(stream, cards, lambda c: _state(c, "agent-dead") == "offline",
                           events=200, seen=seen)
        aged = time.monotonic() - died
        # not before the window (slack for a slow runner), within it plus a beat
        assert window - 1.0 <= aged <= window + 2.0, aged
        # no other card changed: no drop, the peer online throughout,
        # the dead card the only one that went offline
        assert all(e["type"] != "drop" for e, _ in seen)
        for _e, state in seen:
            assert state["agent-live"]["state"] == "online"
            assert not any(c["crown"] for c in state.values())
        assert cards["agent-dead"]["home"] == "dev"
        assert set(cards) == {"agent-dead", "agent-live"}
        stream.close()
    finally:
        beating = False
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        await console.stop()


async def test_a_lone_hard_death_ages_on_the_beat_alone(rig, monkeypatch) -> None:
    """#108 point 10 with no deliveries at all (a killed lone agent): the
    wall-clock floor (#109) ages the card and the SSE beat (15 s in
    production, 0.5 s here) carries it; window 2 s instead of 90 s."""
    window = 2.0
    monkeypatch.setattr(natsbus, "PRESENCE_FRESH_SEC", window)
    monkeypatch.setattr(httpd, "SSE_KEEPALIVE_SEC", 0.5)
    admin: NatsClient = rig["admin"]
    console = Console(settings_of(rig))
    await console.start()
    try:
        stream = Stream(console)
        await stream.open()
        await admin.publish("presence.dembaca.agent-lone", encode_presence("online"))
        await admin.flush()
        sent = time.monotonic()
        cards: dict[str, dict] = {}
        await _cards_until(stream, cards, lambda c: _state(c, "agent-lone") == "online")
        await _cards_until(stream, cards, lambda c: _state(c, "agent-lone") == "offline",
                           events=200)
        aged = time.monotonic() - sent
        assert window - 1.0 <= aged <= window + 2.0, aged
        stream.close()
    finally:
        await console.stop()


def _stamp(text: str) -> float:
    """A JetStream RFC 3339 time (nanoseconds) as epoch seconds."""
    head, _, frac = text.rstrip("Z").partition(".")
    return (datetime.fromisoformat(head).replace(tzinfo=timezone.utc).timestamp()
            + float("0." + (frac or "0")))


async def _last(admin: NatsClient, subject: str) -> tuple[int, float, bytes]:
    reply = await admin.request("$JS.API.STREAM.MSG.GET.PRESENCE",
                                json.dumps({"last_by_subj": subject}).encode(), timeout=5.0)
    msg = json.loads(reply.data)["message"]
    return msg["seq"], _stamp(msg["time"]), base64.b64decode(msg.get("data", ""))


async def _stream_holder(admin: NatsClient, agents: list[str]) -> str | None:
    """The crown of dev computed straight from the stream's records with
    the rule the MCP side's coordinator("who") uses (natsbus.crown_holder,
    RT-6.38) — not from the console's fleet."""
    claims: dict[str, int] = {}
    at: dict[str, float] = {}
    presence: dict[str, tuple[float, str]] = {}
    now = 0.0
    for agent in agents:
        seq, t, _ = await _last(admin, f"presence.crown.dev.{agent}")
        claims[agent], at[agent] = seq, t
        _seq, pt, data = await _last(admin, f"presence.dembaca.{agent}")
        presence[agent] = (pt, decode_presence(data))
        now = max(now, t, pt)
    return natsbus.crown_holder(claims, presence, now, at=at)


async def test_an_explicit_release_moves_the_one_crown(rig) -> None:
    """#108 point 11, the crown of a home over NATS (RT-6.38): the holder's
    release is a new claim that moves behind every other
    (lease._command_nats "release"); the peer's older claim leads, so the
    console shows exactly one crown at every step, and it moves to the
    peer — the holder the stream's records give by natsbus.crown_holder.
    It checks that the console follows a re-claim of the same subject,
    written with the real claim encoder (natsbus.encode_claim)."""
    admin: NatsClient = rig["admin"]
    agents = ["agent-one", "agent-two"]
    for agent in agents:
        await admin.publish(f"presence.dembaca.{agent}", encode_presence("online"))
        await admin.publish(f"presence.member.dev.{agent}", b'{"v": 1}')
    await admin.publish("presence.crown.dev.agent-one", natsbus.encode_claim())
    await asyncio.sleep(0.01)  # a later server stamp: the claim order
    await admin.publish("presence.crown.dev.agent-two", natsbus.encode_claim())
    await admin.flush()
    assert await _stream_holder(admin, agents) == "agent-one"

    console = Console(settings_of(rig))
    await console.start()
    try:
        stream = Stream(console)
        await stream.open()
        cards: dict[str, dict] = {}
        seen: list = []
        await _cards_until(stream, cards,
                           lambda c: c.get("agent-one", {}).get("crown") is True
                           and "agent-two" in c, seen=seen)
        assert [n for n, c in cards.items() if c["crown"]] == ["agent-one"]

        # the release: agent-one claims anew and moves behind agent-two
        await admin.publish("presence.crown.dev.agent-one", natsbus.encode_claim())
        await admin.flush()
        await _cards_until(stream, cards,
                           lambda c: c["agent-two"]["crown"] is True
                           and c["agent-one"]["crown"] is False, seen=seen)
        assert [n for n, c in cards.items() if c["crown"]] == ["agent-two"]
        assert await _stream_holder(admin, agents) == "agent-two"
        assert console.fleet.holder_of("dev") == "agent-two"
        # never two crowns on the way (a wave lists agent-one's loss first)
        for _e, state in seen:
            assert sum(1 for c in state.values() if c["crown"]) <= 1
        assert cards["agent-two"]["home"] == "dev"
        assert cards["agent-one"]["state"] == cards["agent-two"]["state"] == "online"
        stream.close()
    finally:
        await console.stop()
