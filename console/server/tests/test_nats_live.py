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
import json
import os
import shutil
import socket
import subprocess
import time

import pytest
from nats.aio.client import Client as NatsClient

from console_server import auth, config
from console_server.nats_read import PersonReader, View
from mattermost_ai_agent_bus.natsbus import encode_presence

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
    r.renew_sec = 600.0
    r.start()
    await until(lambda: card(r, "agent-one").get("crown") is True, what="the replay")
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
    r.start()
    await until(lambda: card(r, "agent-one").get("crown") is True, what="the replay")
    await until(lambda: r.connects >= 3, what="two renewals")
    await admin.publish("presence.dembaca.agent-three", encode_presence("online"))
    await until(lambda: card(r, "agent-three").get("state") == "online", what="after renewal")
    assert card(r, "agent-one").get("crown") is True   # not forgotten
    assert r.state == "reading"


async def test_the_login_learns_its_name_from_its_own_record(rig) -> None:
    """The console never derives the person's name: the login service's
    record on ``deliver.person.<login>.self`` names it and its anchor."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["good"]
    await make_consumers(admin, r)
    r.renew_sec = 600.0
    r.start()
    await until(lambda: r.state == "reading", what="the connect")
    await admin.publish(r.deliver_self, b'{"name": "person-dembaca", "anchor": "dembaca"}')
    await until(lambda: r.name == "person-dembaca", what="the record")
    assert r.anchor == "dembaca"


async def test_a_login_whose_subjects_are_not_granted_reads_nothing(rig) -> None:
    """A credential for another login's subjects: NATS refuses the
    subscriptions, and nothing reaches the fleet."""
    admin: NatsClient = rig["admin"]
    r: PersonReader = rig["wrong"]
    await admin.publish("presence.dembaca.agent-one", encode_presence("online"))
    r.start()
    await asyncio.sleep(1.0)
    assert r.view.fleet.cards() == []
