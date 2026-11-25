"""The console's NATS side against a real nats-server (2.15.3, as the mcp
transport tests: ``MM_TEST_NATS=1`` and a binary in ``MM_NATS_SERVER``;
CI downloads it).

The lab in miniature, without the login service: an admin makes the
PRESENCE and STATS streams and, as the login service does at a person's
first issue (RT-6.43 step 3), the login's two push consumers; the server
holds the login's public key as an nkey user with the rights the login
service writes into a person's credential (publish nothing, subscribe only
``deliver.<name>.<login>.>``). The console's reader connects with that
login's key and an access token (the token rides along as with the auth
callout; a static nkey user ignores it), reads the fleet, renews with the
same key and keeps the consumers' position. The auth callout itself is the
login service's own live test (``services/auth``).
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
from nats.aio.client import Client as NatsClient

from console_server import auth, config
from console_server.nats_read import PersonReader, View
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
STATS_STREAM = {"name": "STATS", "subjects": ["stats.>"], "retention": "limits",
                "max_msgs_per_subject": 500, "no_ack": True}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def renew(refresh: str) -> auth.Tokens:
    return auth.Tokens("fresh-access", refresh)


def reader_for(url: str, renew_sec: float = 600.0) -> PersonReader:
    settings = config.Settings(public_url="http://127.0.0.1:8747", login_off=True,
                               nats_url=url)
    view = View("presence")
    view.reader = PersonReader(settings, auth.Tokens("an-access", "a-refresh"),
                               renew, view, until=time.time() + 3600,
                               on_end=lambda: None, renew_sec=renew_sec)
    return view.reader


def person_user(public_key: str, grant: str) -> str:
    """The rights the login service writes into a person's user JWT
    (``callout.personPermissions``), as a static nkey user."""
    return ('{ nkey: "%s", permissions: { publish: { deny: [">"] },'
            ' subscribe: { allow: ["%s"] } } }' % (public_key, grant))


async def make_consumers(admin: NatsClient, r: PersonReader) -> None:
    """What the login service makes at a person's first issue
    (``consumers.EnsurePerson``)."""
    for stream, name, deliver, policy, subject in (
            ("PRESENCE", f"person-dembaca_{r.login}_presence", r.deliver_presence,
             "last_per_subject", "presence.>"),
            ("STATS", f"person-dembaca_{r.login}_stats", r.deliver_stats, "all", "stats.>")):
        spec = {"stream_name": stream, "config": {
            "name": name, "ack_policy": "none", "deliver_policy": policy,
            "deliver_subject": deliver, "filter_subject": subject,
            "inactive_threshold": 900 * 1_000_000_000}}
        reply = await admin.request(f"$JS.API.CONSUMER.CREATE.{stream}.{name}",
                                    json.dumps(spec).encode(), timeout=5.0)
        assert not json.loads(reply.data).get("error"), reply.data


async def publish_record(admin: NatsClient, r: PersonReader) -> None:
    """The login's own record, as the login service writes it into PRESENCE
    at each issue; the reader is "reading" only once it came."""
    await admin.publish(r.record_subject, json.dumps(
        {"v": 1, "name": "person-dembaca", "anchor": "dembaca",
         "iat": int(time.time()), "exp": int(time.time()) + 900}).encode())
    await admin.flush()


@pytest.fixture
async def rig(tmp_path):
    binary = os.environ.get("MM_NATS_SERVER") or shutil.which("nats-server")
    port = free_port()
    url = "nats://127.0.0.1:%d" % port
    # the readers first: each login's key exists before the server knows it
    good = reader_for(url, renew_sec=0.5)
    # a login whose credential grants another login's subjects
    wrong = reader_for(url)
    admin_pw = os.urandom(8).hex()
    conf = tmp_path / "nats.conf"
    conf.write_text(
        "port: %d\nhost: 127.0.0.1\n" % port
        + 'jetstream { store_dir: "%s" }\n' % (tmp_path / "js")
        + "accounts { BUS { jetstream: enabled\n users: ["
        + '{ user: admin, password: "%s"},' % admin_pw
        + person_user(good.public_key, f"deliver.person.{good.login}.>") + ","
        + person_user(wrong.public_key, f"deliver.person.{good.login}.>")
        + "] } }\n")
    proc = subprocess.Popen([binary, "-c", str(conf)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        for name, spec in (("PRESENCE", PRESENCE_STREAM), ("STATS", STATS_STREAM)):
            reply = await admin.request(f"$JS.API.STREAM.CREATE.{name}",
                                        json.dumps(spec).encode(), timeout=5.0)
            assert not json.loads(reply.data).get("error"), reply.data
        yield {"url": url, "admin": admin, "good": good, "wrong": wrong}
    finally:
        for r in (good, wrong):
            await r.aclose()
        await admin.close()
        proc.terminate()
        proc.wait(10)


async def until(pred, timeout: float = 8.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out" + (f": {what}" if what else ""))
        await asyncio.sleep(0.05)


def card(r: PersonReader, name: str) -> dict:
    return next((c for c in r.view.fleet.cards() if c["name"] == name), {})


async def test_the_login_reads_the_fleet_its_consumers_deliver(rig) -> None:
    """RT-6.43 steps 1 and 3: the reader connects with the login's key,
    subscribes its two deliver subjects and reads presence, membership
    and crown from the replay and live; a STATS line arrives too."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await admin.publish("presence.dembaca.agent-one", encode_presence("online", "bot1"))
    await admin.publish("presence.member.dev.agent-one", b'{"v": 1}')
    await admin.publish("presence.crown.dev.agent-one", b'{"v": 1}')
    await admin.publish("presence.dembaca.agent-two", encode_presence("away"))
    await admin.publish("stats.agent-one", b'{"v": "0.101.0"}')
    await admin.flush()
    await make_consumers(admin, r)
    await publish_record(admin, r)
    r.renew_sec = 600.0
    r.start()
    await until(lambda: card(r, "agent-one").get("crown") is True, what="the replay")
    await until(lambda: r.state == "reading", what="the own record")
    one = card(r, "agent-one")
    assert one["state"] == "online" and one["home"] == "dev" and one["for"] == "dembaca"
    assert card(r, "agent-two")["state"] == "away"
    await until(lambda: r.view.stats_lines >= 1, what="the STATS line")
    await admin.publish("presence.dembaca.agent-two", encode_presence("online"))
    await until(lambda: card(r, "agent-two").get("state") == "online", what="a change")


async def test_a_renewal_keeps_the_consumers_position(rig) -> None:
    """Step 4: a renewal connects again with the same key; the consumers
    keep their position, so what was read stays and what is new arrives
    once (no consumer sequence seen twice, no claims forgotten)."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await admin.publish("presence.dembaca.agent-one", encode_presence("online"))
    await admin.publish("presence.crown.dev.agent-one", b'{"v": 1}')
    await admin.flush()
    await make_consumers(admin, r)
    await publish_record(admin, r)
    r.start()
    await until(lambda: card(r, "agent-one").get("crown") is True, what="the replay")
    await until(lambda: r.connects >= 3, what="two renewals")
    await admin.publish("presence.dembaca.agent-three", encode_presence("online"))
    await until(lambda: card(r, "agent-three").get("state") == "online", what="after renewal")
    assert card(r, "agent-one").get("crown") is True   # not forgotten
    await until(lambda: r.state == "reading", what="reading after renewals")
    assert r.name == "person-dembaca"


async def test_the_login_learns_its_name_from_its_own_record(rig) -> None:
    """The console never derives the person's name: the login's own record
    on ``presence.person.<login>.v1``, delivered with the presence lines,
    names it and its anchor."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await make_consumers(admin, r)
    r.renew_sec = 600.0
    r.start()
    await until(lambda: r.connects >= 1, what="the connect")
    # the login service writes it into PRESENCE at each issue
    await admin.publish(r.record_subject, json.dumps(
        {"v": 1, "name": "person-dembaca", "anchor": "dembaca",
         "iat": int(time.time()), "exp": int(time.time()) + 900}).encode())
    await until(lambda: r.name == "person-dembaca", what="the record")
    assert r.anchor == "dembaca" and r.state == "reading"


async def test_a_login_whose_subjects_are_not_granted_reads_nothing(rig) -> None:
    """A credential for another login's subjects: NATS refuses the
    subscriptions, and nothing reaches the fleet."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["wrong"]
    await admin.publish("presence.dembaca.agent-one", encode_presence("online"))
    r.start()
    await asyncio.sleep(1.0)
    assert r.view.fleet.cards() == []


# --- the pilot's ageing and crown handover (#108 points 10 and 11) -------------------


def _states(r: PersonReader) -> dict[str, str]:
    return {c["name"]: c["state"] for c in r.view.fleet.cards()}


def _crowns(r: PersonReader) -> list[str]:
    return [c["name"] for c in r.view.fleet.cards() if c["crown"]]


async def test_a_hard_death_ages_the_card_offline_and_no_other(rig, monkeypatch) -> None:
    """#108 point 10: an agent killed hard (kill -9) falls silent and
    sends no bye; its card turns offline once its last presence is older
    than the freshness window (natsbus.PRESENCE_FRESH_SEC, 90 s in
    production). The window is shortened to 2 s here; the peer beats every
    0.5 s (30 s / 90 s in production) and its card never changes state."""
    window = 2.0
    monkeypatch.setattr(natsbus, "PRESENCE_FRESH_SEC", window)
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await make_consumers(admin, r)
    await publish_record(admin, r)
    r.renew_sec = 600.0
    r.start()
    beating = True

    async def peer() -> None:
        while beating:
            await admin.publish("presence.dembaca.agent-live", encode_presence("online"))
            await asyncio.sleep(0.5)

    beat = asyncio.ensure_future(peer())
    try:
        await admin.publish("presence.member.dev.agent-dead", b'{"v": 1}')
        await admin.publish("presence.member.dev.agent-live", b'{"v": 1}')
        for n in range(3):  # a few heartbeats, then the kill: silence, no bye
            if n:
                await asyncio.sleep(0.3)
            await admin.publish("presence.dembaca.agent-dead", encode_presence("online"))
            await admin.flush()
        died = time.monotonic()  # right after the last presence reached the server
        await until(lambda: _states(r).get("agent-dead") == "online"
                    and _states(r).get("agent-live") == "online", what="both online")
        assert time.monotonic() - died < window, "online seen only after the window"

        seen: list[dict[str, str]] = []

        def dead() -> bool:
            seen.append(_states(r))
            return seen[-1].get("agent-dead") == "offline"

        await until(dead, what="the dead card offline")
        aged = time.monotonic() - died
        # not before the window (slack for a slow runner), within it plus a beat
        assert window - 1.0 <= aged <= window + 2.0, aged
        # no other card changed: the peer online throughout, no crown
        for state in seen:
            assert state["agent-live"] == "online"
        assert _crowns(r) == []
        assert card(r, "agent-dead")["home"] == "dev"
        assert set(_states(r)) == {"agent-dead", "agent-live"}
    finally:
        beating = False
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def test_a_lone_hard_death_ages_without_a_delivery(rig, monkeypatch) -> None:
    """#108 point 10 with no deliveries at all (a killed lone agent): the
    wall-clock floor (#109) ages the card; window 2 s instead of 90 s."""
    window = 2.0
    monkeypatch.setattr(natsbus, "PRESENCE_FRESH_SEC", window)
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await make_consumers(admin, r)
    await publish_record(admin, r)
    r.renew_sec = 600.0
    r.start()
    await admin.publish("presence.dembaca.agent-lone", encode_presence("online"))
    await admin.flush()
    sent = time.monotonic()
    await until(lambda: _states(r).get("agent-lone") == "online", what="online")
    await until(lambda: _states(r).get("agent-lone") == "offline", what="offline")
    aged = time.monotonic() - sent
    assert window - 1.0 <= aged <= window + 2.0, aged


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
    r: PersonReader = rig["good"]
    agents = ["agent-one", "agent-two"]
    for agent in agents:
        await admin.publish(f"presence.dembaca.{agent}", encode_presence("online"))
        await admin.publish(f"presence.member.dev.{agent}", b'{"v": 1}')
    await admin.publish("presence.crown.dev.agent-one", natsbus.encode_claim())
    await asyncio.sleep(0.01)  # a later server stamp: the claim order
    await admin.publish("presence.crown.dev.agent-two", natsbus.encode_claim())
    await admin.flush()
    assert await _stream_holder(admin, agents) == "agent-one"

    await make_consumers(admin, r)
    await publish_record(admin, r)
    r.renew_sec = 600.0
    r.start()
    seen: list[list[str]] = []

    def crowns_are(names: list[str]) -> bool:
        seen.append(_crowns(r))
        return seen[-1] == names and "agent-two" in _states(r)

    await until(lambda: crowns_are(["agent-one"]), what="agent-one's crown")

    # the release: agent-one claims anew and moves behind agent-two
    await admin.publish("presence.crown.dev.agent-one", natsbus.encode_claim())
    await admin.flush()
    await until(lambda: crowns_are(["agent-two"]), what="the crown moved")
    assert await _stream_holder(admin, agents) == "agent-two"
    assert r.view.fleet.holder_of("dev") == "agent-two"
    # never two crowns on the way
    assert all(len(c) <= 1 for c in seen)
    assert card(r, "agent-two")["home"] == "dev"
    assert card(r, "agent-one")["state"] == card(r, "agent-two")["state"] == "online"
