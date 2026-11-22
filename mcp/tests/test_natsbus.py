"""The NATS transport (natsbus.py, ADR-014 point 16): unit tests, and tests against a real
nats-server 2.15.3 (MM_NATS_SERVER=<path>, or on PATH). The server tests are skipped unless
MM_TEST_NATS=1; with it, a missing binary is a failure, not a skip (CI sets it).
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import nkeys
import pytest
from nats.aio.client import Client as NatsClient

from mattermost_ai_agent_bus import natsbus
from mattermost_ai_agent_bus.login import NKey
from mattermost_ai_agent_bus.natsbus import (
    Event,
    GapTracker,
    Message,
    MessageError,
    NatsBus,
    NameMismatch,
    NatsSettings,
    agent_name,
    build_hello,
    crown_holder,
    decode,
    decode_presence,
    encode,
    encode_claim,
    encode_presence,
    parse_ack,
)

# The same vector as services/auth/internal/callout/name_test.go.
VECTOR_NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NLCNF4"
VECTOR_NAME = "7jdioaymoy77vybfzcvze3fc4u"


# --- unit: the name -------------------------------------------------------------------------

def test_agent_name_vector() -> None:
    assert agent_name("alice", VECTOR_NKEY, prefix="") == VECTOR_NAME
    assert agent_name("alice", VECTOR_NKEY) == "agent-" + VECTOR_NAME
    assert agent_name("alice", VECTOR_NKEY, prefix="x-") == "x-" + VECTOR_NAME
    assert len(VECTOR_NAME) == 26
    assert agent_name("Alice", VECTOR_NKEY) != VECTOR_NAME  # the owner is not normalised, as in Go


# --- unit: message format v1 ----------------------------------------------------------------

def test_roundtrip_post_reaction_unreaction() -> None:
    post = Message("post", "héllo", root=7, props={"x_a": "b", "x_n": 3, "x_ok": True})
    assert decode(encode(post)) == post
    assert json.loads(encode(post)) == {"v": 1, "kind": "post", "text": "héllo", "root": 7, "ref": None,
                                        "emoji": None, "props": {"x_a": "b", "x_n": 3, "x_ok": True}}
    for kind in ("reaction", "unreaction"):
        r = Message(kind, ref=12, emoji="thumbsup")
        assert decode(encode(r)) == r
        assert json.loads(encode(r))["text"] == ""


@pytest.mark.parametrize("body", [
    b"", b"[]", b"null", b"not json", b'{"kind":"post","text":"x"}',  # no version
    b'{"v":2,"kind":"post","text":"x"}', b'{"v":"1","kind":"post","text":"x"}',
    b'{"v":true,"kind":"post","text":"x"}', b'{"v":1.0,"kind":"nope","text":"x"}',
    b'{"v":1,"kind":"post","text":5}', b'{"v":1,"kind":"post","text":"x","root":"3"}',
    b'{"v":1,"kind":"post","text":"x","root":0}', b'{"v":1,"kind":"post","text":"x","root":true}',
    b'{"v":1,"kind":"post","text":"x","props":[]}', b'{"v":1,"kind":"post","text":"x","props":{"a":[1]}}',
    b'{"v":1,"kind":"post","text":"x","props":{"a":null}}',
    b'{"v":1,"kind":"post","text":"x","emoji":"x"}',
    b'{"v":1,"kind":"reaction","text":"","emoji":"x"}',  # no ref
    b'{"v":1,"kind":"reaction","text":"","ref":3}',  # no emoji
    b'{"v":1,"kind":"reaction","text":"hi","ref":3,"emoji":"x"}',
    b'{"v":1,"kind":"unreaction","text":"","ref":"3","emoji":"x"}',
    b'{"v":1,"kind":"post","text":"' + b"a" * natsbus.MAX_BODY + b'"}',  # too big
])
def test_decode_rejects(body: bytes) -> None:
    with pytest.raises(MessageError):
        decode(body)


def test_decode_error_holds_no_content() -> None:
    with pytest.raises(MessageError) as ei:
        decode(b'{"v":1,"kind":"post","text":"SECRET-CONTENT","root":"x"}')
    assert "SECRET-CONTENT" not in str(ei.value)


def test_encode_rejects_too_big_and_malformed() -> None:
    with pytest.raises(MessageError):
        encode(Message("post", "a" * natsbus.MAX_BODY))
    with pytest.raises(MessageError):
        encode(Message("reaction", ref=None, emoji="x"))
    with pytest.raises(MessageError):
        encode(Message("post", "x", props={"a": [1]}))  # type: ignore[dict-item]
    assert len(encode(Message("post", "a" * (natsbus.MAX_BODY - 100)))) <= natsbus.MAX_BODY


def test_sender_in_body_is_ignored() -> None:
    m = decode(b'{"v":1,"kind":"post","text":"x","sender":"alice","from":"alice","user":"alice"}')
    assert m == Message("post", "x")
    assert not hasattr(m, "sender")


def test_presence_and_claim_bodies() -> None:
    for s in ("online", "away", "bye"):
        assert decode_presence(encode_presence(s)) == s
    assert encode_claim() == b'{"v":1}'
    for bad in (b'{"v":1}', b'{"v":1,"status":"gone"}', b'{"v":2,"status":"online"}', b"[]"):
        with pytest.raises(MessageError):
            decode_presence(bad)
    with pytest.raises(MessageError):
        encode_presence("gone")


# --- unit: JetStream metadata, gaps, crown, CONNECT --------------------------------------------

def test_parse_ack() -> None:
    a = parse_ack("$JS.ACK.BUS.c1.1.17.5.1795388483779185000.0")
    assert a is not None and (a.stream_seq, a.consumer_seq) == (17, 5)
    assert a.time == pytest.approx(1795388483.779185)
    b = parse_ack("$JS.ACK._.HASH.BUS.c1.1.18.6.1795388483779185000.2.tok")
    assert b is not None and (b.stream_seq, b.consumer_seq) == (18, 6)
    for bad in (None, "", "reply", "$JS.ACK.BUS.c1", "$JS.ACK.BUS.c1.1.x.5.1.0", "_INBOX.a.b",
                "$JS.ACK.BUS.c1.1.0.5.1795388483779185000.0"):
        assert parse_ack(bad) is None


def test_gap_tracker() -> None:
    g = GapTracker()
    assert not g.observe("a", 5)  # the first one is the baseline
    assert not g.observe("a", 6)
    assert g.observe("a", 8)  # 7 is lost
    assert not g.observe("a", 9)
    assert not g.observe("b", 1)  # the subjects are independent
    g.reset()
    assert not g.observe("a", 40)


def _settings(**kw: Any) -> NatsSettings:
    base: dict[str, Any] = {"servers": ("nats://127.0.0.1:1",), "home": "home"}
    base.update(kw)
    return NatsSettings(**base)


class FakeLogin:
    def __init__(self, hook: Callable[[int], Awaitable[None]] | None = None) -> None:
        self.calls = 0
        self.hook = hook
        self.kp = nkeys.from_seed(nkeys.encode_seed(os.urandom(32), nkeys.PREFIX_BYTE_USER))

    async def access_token(self) -> str:
        self.calls += 1
        if self.hook:
            await self.hook(self.calls)
        return f"tok-{self.calls}"

    def nkey(self) -> NKey:
        kp = self.kp
        return NKey(public_key=bytes(kp.public_key).decode(), sign=lambda n: bytes(kp.sign(n)))


_BUS_KW = ("gap_reconnect_sec", "renew_sec", "on_fatal", "heartbeat_sec", "send_wait_sec")


def _unit_bus(**kw: Any) -> tuple[NatsBus, list[Event]]:
    events: list[Event] = []
    bus_kw = {k: kw.pop(k) for k in _BUS_KW if k in kw}
    bus = NatsBus(_settings(**kw), FakeLogin(), "alice", events.append, **bus_kw)
    return bus, events


def _reply(bus: NatsBus, where: str, sseq: int, cseq: int, t: float | None = None) -> str:
    """The JetStream reply subject of the bus's own consumer for `where`."""
    consumer = f"{bus.name}_dm" if where == "dm" else f"{bus.name}_ch_{where}"
    return f"$JS.ACK.BUS.{consumer}.1.{sseq}.{cseq}.{int((time.time() if t is None else t) * 1e9)}.0"


def _presence_reply(bus: NatsBus, sseq: int, cseq: int, t: float) -> str:
    return f"$JS.ACK.PRESENCE.{bus.name}_presence.1.{sseq}.{cseq}.{int(t * 1e9)}.0"


class FakeMsg:
    def __init__(self, subject: str, reply: str | None, data: bytes) -> None:
        self.subject, self.reply, self.data = subject, reply, data


def test_crown_rule() -> None:
    now = 1000.0
    pres = {"a": (now - 10, "online"), "b": (now - 10, "online"), "c": (now - 10, "online")}
    assert crown_holder({}, pres, now) is None
    assert crown_holder({"b": 20, "a": 30, "c": 10}, pres, now) == "c"  # the lowest sequence
    assert crown_holder({"b": 20, "a": 30}, {**pres, "b": (now - 89, "online")}, now) == "b"  # fresh
    assert crown_holder({"b": 20, "a": 30}, {**pres, "b": (now - 90, "online")}, now) == "a"  # stale
    assert crown_holder({"b": 20, "a": 30}, {**pres, "b": (now - 1, "bye")}, now) == "a"  # signed off
    assert crown_holder({"b": 20, "a": 30}, {"a": pres["a"]}, now) == "a"  # no presence at all
    assert crown_holder({"b": 20}, {"b": (now - 100, "away")}, now) is None
    assert crown_holder({"b": 20, "a": 30}, {**pres, "b": (now - 10, "away")}, now) == "b"  # away is alive
    # A renewed claim is a new, higher sequence: the crown moves back.
    assert crown_holder({"b": 20, "a": 30}, pres, now) == "b"
    assert crown_holder({"b": 40, "a": 30}, pres, now) == "a"


def test_hello_json() -> None:
    h = json.loads(build_hello("T", "home", ("g1", "g2"), True, {"home": 5, "g1": 0, "other": 9}))
    assert h == {"token": "T", "home": "home", "guests": ["g1", "g2"], "lobby": True,
                 "since": {"home": 5, "other": 9}}
    assert json.loads(build_hello("T", "home", (), False, {})) == {"token": "T", "home": "home"}


async def test_unit_deliver_drops_and_gap(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bus, events = _unit_bus()
    bus.name  # noqa: B018
    ok = encode(Message("post", "SECRET-TEXT"))
    subj = "bus.ch.home.agent-x"
    await bus._on_deliver("home", subj, None, ok)  # no JetStream metadata
    await bus._on_deliver("home", subj, _reply(bus, "home", 10, 1), b"SECRET-TEXT not json")  # malformed
    await bus._on_deliver("home", "bus.ch.other.agent-x", _reply(bus, "home", 11, 2), ok)  # wrong channel
    await bus._on_deliver("home", "bus.ch.home.a.b", _reply(bus, "home", 11, 3), ok)  # too many tokens
    assert events == []
    assert "SECRET-TEXT" not in caplog.text
    await bus._on_deliver("home", subj, _reply(bus, "home", 12, 4), ok)
    assert [(e.where, e.sender, e.stream_seq) for e in events] == [("home", "agent-x", 12)]
    await bus._on_deliver("home", subj, _reply(bus, "home", 12, 5), ok)  # a repeat of 12: dropped
    assert len(events) == 1 and bus.since() == {"home": 12}
    # consumer sequence 5 -> 7: a gap. The message is dropped, `since` stays, a reconnect is asked for.
    assert not bus._wake.is_set()
    await bus._on_deliver("home", subj, _reply(bus, "home", 14, 7), ok)
    assert len(events) == 1 and bus.since() == {"home": 12} and bus._wake.is_set()
    await bus._on_deliver("home", subj, _reply(bus, "home", 15, 8), ok)  # still dropped until the reconnect
    assert len(events) == 1
    # A second gap within the interval: no second reconnect now, but the channel stays
    # stale and a reconnect is scheduled for when the interval is over (never lost for good).
    bus._wake.clear()
    await bus._on_deliver("home", subj, _reply(bus, "home", 20, 12), ok)
    assert not bus._wake.is_set() and len(events) == 1 and bus.since() == {"home": 12}
    assert "home" in bus._stale and bus._gap_task is not None and not bus._gap_task.done()
    bus._gap_task.cancel()


async def test_gap_within_the_limit_reconnects_when_the_wait_is_over() -> None:
    bus, events = _unit_bus(gap_reconnect_sec=0.2)
    ok = encode(Message("post", "x"))
    subj = "bus.ch.home.agent-x"
    await bus._on_deliver("home", subj, _reply(bus, "home", 1, 1), ok)
    await bus._on_deliver("home", subj, _reply(bus, "home", 3, 3), ok)  # gap: reconnect now
    assert bus._wake.is_set()
    bus._wake.clear()
    await bus._on_deliver("home", subj, _reply(bus, "home", 5, 5), ok)  # gap within 0.2 s: held back
    assert not bus._wake.is_set() and "home" in bus._stale and len(events) == 1
    await asyncio.wait_for(bus._wake.wait(), 2)  # the wait is over: the reconnect is asked for
    assert bus.since() == {"home": 1}  # `since` is the last accepted one: the gap is replayed
    await bus._on_deliver("home", subj, _reply(bus, "home", 6, 6), ok)
    assert len(events) == 1  # still stale until the reconnect really happens


async def test_dm_gap_and_since_key() -> None:
    bus, events = _unit_bus()
    ok = encode(Message("post", "hi"))
    subj = f"bus.dm.{bus.name}.agent-y"
    await bus._on_deliver("dm", subj, _reply(bus, "dm", 7, 1), ok)
    assert bus.since() == {"@dm": 7}
    assert json.loads(build_hello("T", "home", (), False, bus.since()))["since"] == {"@dm": 7}
    await bus._on_deliver("dm", subj, _reply(bus, "dm", 9, 3), ok)  # a DM was lost
    assert bus._wake.is_set() and len(events) == 1 and bus.since() == {"@dm": 7}
    await bus._on_deliver("dm", subj, _reply(bus, "dm", 7, 4), ok)  # a repeat from since
    assert len(events) == 1


async def test_unit_dm_event_and_repr() -> None:
    bus, events = _unit_bus()
    await bus._on_deliver("dm", f"bus.dm.{bus.name}.agent-y", _reply(bus, "dm", 3, 1), encode(Message("post", "hi")))
    assert [(e.where, e.sender) for e in events] == [("dm", "agent-y")]
    await bus._on_deliver("dm", "bus.dm.someone-else.agent-y", _reply(bus, "dm", 4, 2), encode(Message("post", "hi")))
    assert len(events) == 1
    assert "tok-" not in repr(bus) and "seed" not in repr(bus).lower()


def test_validation() -> None:
    bus, _ = _unit_bus()
    for bad in ("", "A", "a.b", "a b", "a*", "a>", "-a", "x" * 65):
        with pytest.raises(ValueError):
            bus._channel(bad)
        assert not natsbus.CHANNEL_RE.match(bad)
    for bad in ("", "a.b", "a b", "*", ">", "-a", "x" * 129):
        with pytest.raises(ValueError):
            bus._dm_subject(bad)
    assert bus._channel("a_b-1") == "a_b-1"
    with pytest.raises(ValueError):
        NatsBus(_settings(bus="a.b"), FakeLogin(), "alice", lambda e: None)
    with pytest.raises(ValueError):
        NatsBus(_settings(presence="deliver"), FakeLogin(), "alice", lambda e: None)


async def test_publish_when_not_connected() -> None:
    bus, _ = _unit_bus()
    with pytest.raises(natsbus.NatsBusError):
        await bus.post("home", "x")


# --- unit: NaN, strict decoding, binding, TLS, clocks ------------------------------------------

def test_no_nan_or_infinity_on_the_wire() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(MessageError):
            encode(Message("post", "x", props={"x_f": bad}))
    for body in (b'{"v":1,"kind":"post","text":"x","props":{"x_f":NaN}}',
                 b'{"v":1,"kind":"post","text":"x","props":{"x_f":Infinity}}',
                 b'{"v":1,"kind":"post","text":"x","props":{"x_f":-Infinity}}',
                 b'{"v":1,"kind":"post","text":"x","props":{"x_f":1e999}}'):
        with pytest.raises(MessageError):
            decode(body)
    with pytest.raises(MessageError):
        decode_presence(b'{"v":1,"status":"online","n":NaN}')
    assert decode(encode(Message("post", "x", props={"x_f": 1.5}))).props == {"x_f": 1.5}


def test_all_encoders_forbid_nan(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = json.dumps

    def spy(obj: Any, **kw: Any) -> str:
        seen.append(kw.get("allow_nan"))
        return real(obj, **kw)

    monkeypatch.setattr(natsbus.json, "dumps", spy)
    encode(Message("post", "x"))
    encode_presence("online")
    encode_claim()
    assert seen == [False, False, False]


@pytest.mark.parametrize("body", [
    b'{"v":1,"kind":"post","text":"x","props":{"from_trusted_human":true}}',
    b'{"v":1,"kind":"post","text":"x","props":{"mandated":true}}',
    b'{"v":1,"kind":"post","text":"x","props":{"X_a":1}}',
    b'{"v":1,"kind":"post","text":"\\ud800"}',  # lone surrogate in text
    b'{"v":1,"kind":"post","text":"x","props":{"x_a":"\\udc00"}}',
    b'{"v":1,"kind":"post","text":"x","props":{"x_\\ud800":"a"}}',
    b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":"\\ud83d"}',
    b'{"v":1,"kind":"post","text":"\xff\xfe"}',  # not UTF-8
    b'{"v":1,"kind":"post","text":"caf\xe9"}',  # Latin-1
    b'{"v":1,"kind":"post","text":"a","text":"b"}',  # duplicate key
    b'{"v":1,"v":1,"kind":"post","text":"a"}',
    b'{"v":1,"kind":"post","text":"x","props":{"x_a":1,"x_a":2}}',
    b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":""}',
    b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":"a\\nb"}',
    b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":"a\\r"}',
    b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":"' + b"a" * 65 + b'"}',
])
def test_decoder_is_strict(body: bytes) -> None:
    with pytest.raises(MessageError):
        decode(body)


def test_decoder_accepts_the_limits() -> None:
    m = decode(b'{"v":1,"kind":"reaction","text":"","ref":3,"emoji":"' + b"a" * 64 + b'"}')
    assert m.emoji == "a" * 64
    assert decode('{"v":1,"kind":"post","text":"hé \U0001f600","props":{"x_k":"ü"}}'.encode()).text == "hé \U0001f600"


def test_encoder_is_strict_too() -> None:
    for m in (Message("post", "x", props={"from_trusted_human": True}), Message("post", "\ud800"),
              Message("reaction", ref=1, emoji="a\nb"), Message("reaction", ref=1, emoji="a" * 65)):
        with pytest.raises(MessageError):
            encode(m)


def test_parse_ack_binds_stream_and_consumer() -> None:
    v1 = "$JS.ACK.BUS.me_dm.1.17.5.1795388483779185000.0"
    v2 = "$JS.ACK._.HASH.BUS.me_dm.1.17.5.1795388483779185000.0.tok"
    v2d = "$JS.ACK.dom.HASH.BUS.me_dm.1.17.5.1795388483779185000.0"  # 11 tokens
    for r in (v1, v2, v2d):
        assert parse_ack(r, "BUS", "me_dm") is not None
        assert parse_ack(r, "PRESENCE", "me_dm") is None  # another stream
        assert parse_ack(r, "BUS", "other_dm") is None  # another consumer
        assert parse_ack(r, "BUS", "me_ch_home") is None
        assert parse_ack(r) is not None  # unbound: as before


async def test_deliver_drops_a_reply_of_another_consumer(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bus, events = _unit_bus()
    ok = encode(Message("post", "x"))
    t = int(time.time() * 1e9)
    for reply in (f"$JS.ACK.BUS.someone_ch_home.1.5.1.{t}.0",  # another agent's consumer
                  f"$JS.ACK.BUS.{bus.name}_ch_other.1.5.1.{t}.0",  # another channel's
                  f"$JS.ACK.BUS.{bus.name}_dm.1.5.1.{t}.0",
                  f"$JS.ACK.PRESENCE.{bus.name}_ch_home.1.5.1.{t}.0"):
        await bus._on_deliver("home", "bus.ch.home.agent-x", reply, ok)
    assert events == [] and bus.since() == {} and bus._server_now == 0.0
    assert caplog.text.count("dropped a message") == 4
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 5, 1), ok)
    assert len(events) == 1
    # a presence message of a forged consumer is dropped too
    await bus._on_presence_msg(FakeMsg(f"presence.{bus.name}", f"$JS.ACK.PRESENCE.x_presence.1.1.1.{t}.0",
                                       encode_presence("online")))
    assert bus.presence_snapshot() == {}


def test_tls_or_loopback_only() -> None:
    for ok in ("tls://nats.example.org:4222", "nats://localhost:4222", "nats://127.0.0.1:4222",
               "nats://127.9.9.9", "nats://[::1]:4222", "tls://10.0.0.5:4222"):
        NatsSettings(servers=(ok,), home="home")
    for bad in ("nats://nats.example.org:4222", "nats://10.0.0.5:4222", "nats://127.0.0.1.evil.org",
                "ws://localhost:80", "localhost:4222", "http://localhost", "nats://", "tls://", ""):
        with pytest.raises(ValueError):
            NatsSettings(servers=(bad,), home="home")
    with pytest.raises(ValueError):
        NatsSettings(servers=("tls://ok.example.org", "nats://remote.example.org"), home="home")
    # also when settings were built around the check
    s = NatsSettings(servers=("nats://127.0.0.1:1",), home="home")
    object.__setattr__(s, "servers", ("nats://remote.example.org:4222",))
    with pytest.raises(ValueError):
        NatsBus(s, FakeLogin(), "alice", lambda e: None)


T0 = 1_800_000_000.0


async def _presence(bus: NatsBus, who: str, seq: int, t: float, status: str = "online") -> None:
    await bus._on_presence_msg(FakeMsg(f"presence.{who}", _presence_reply(bus, seq, seq, t),
                                       encode_presence(status)))


async def _claim(bus: NatsBus, who: str, ch: str, seq: int, t: float) -> None:
    await bus._on_presence_msg(FakeMsg(f"presence.crown.{ch}.{who}", _presence_reply(bus, seq, seq, t),
                                       encode_claim()))


@pytest.mark.parametrize("local", [T0 + 1e9, T0 - 1e9, T0 - 90 + 10, T0 + 5])
async def test_crown_uses_server_time_not_the_local_clock(monkeypatch: pytest.MonkeyPatch, local: float) -> None:
    monkeypatch.setattr(time, "time", lambda: local)  # the host's clock is wrong
    bus, _ = _unit_bus()
    await _presence(bus, "agent-a", 1, T0 - 100)  # last seen 100 s before the newest server time
    await _claim(bus, "agent-a", "home", 2, T0 - 100)
    await _claim(bus, "agent-b", "home", 3, T0 - 5)
    await _presence(bus, "agent-b", 4, T0)
    assert bus.presence_snapshot() == {"agent-a": (T0 - 100, "online"), "agent-b": (T0, "online")}
    assert bus.holder("home") == "agent-b"  # a is stale by server time, whatever the local clock says
    await _presence(bus, "agent-a", 5, T0 + 10)  # a beats again
    assert bus.holder("home") == "agent-a"  # now fresh, the lowest claim
    await _presence(bus, "agent-z", 6, T0 + 200)  # the server clock moves on: both stale
    assert bus.holder("home") is None


async def test_claims_and_presence_age_out_after_24_hours() -> None:
    bus, _ = _unit_bus()
    await _presence(bus, "agent-old", 1, T0)
    await _claim(bus, "agent-old", "home", 2, T0)
    await _claim(bus, "agent-old", "other", 3, T0)
    await _presence(bus, "agent-new", 4, T0 + 3600)
    await _claim(bus, "agent-new", "home", 5, T0 + 3600)
    assert "agent-old" in bus.presence_snapshot() and bus._claims["home"]["agent-old"] == 2
    await _presence(bus, "agent-new", 6, T0 + natsbus.MAX_AGE_SEC + 10)  # agent-old is now over 24 h old
    assert set(bus.presence_snapshot()) == {"agent-new"}
    assert bus._claims == {"home": {"agent-new": 5}} or "agent-old" not in bus._claims.get("home", {})
    assert "other" not in bus._claims and ("home", "agent-old") not in bus._claim_at
    # a claim of the 24 h edge: its own age counts, not the presence's
    await _presence(bus, "agent-new", 7, T0 + natsbus.MAX_AGE_SEC + 3600 + 10)
    assert bus._claims == {} and bus._claim_at == {}


async def test_server_time_comes_from_any_delivery() -> None:
    bus, _ = _unit_bus()
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 1, 1, t=T0), encode(Message("post", "x")))
    assert bus._server_now == T0
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 2, 2, t=T0 - 50), b"junk")
    assert bus._server_now == T0  # never goes back


async def test_send_checks_the_channel_locally() -> None:
    bus, _ = _unit_bus(guests=("g1",))
    bus._connected = True  # a send must fail before touching the connection
    for coro in (bus.post("other", "x"), bus.react("other", 1, "x"), bus.unreact("other", 1, "x"),
                 bus.claim("other")):
        with pytest.raises(natsbus.NatsBusError, match="asked for"):
            await coro


async def test_no_answer_from_the_server_is_an_error_not_a_success() -> None:
    """A send counts only when the server answered the round trip after it: a flush
    that times out (the server said nothing) makes the send fail."""
    bus, _ = _unit_bus()
    sent: list[str] = []

    class Silent:
        async def publish(self, subject: str, body: bytes) -> None:
            sent.append(subject)

        async def flush(self, timeout: float = 10) -> None:
            raise asyncio.TimeoutError

    bus._nc, bus._connected = Silent(), True  # type: ignore[assignment]
    with pytest.raises(natsbus.NatsBusError, match="publish failed"):
        await bus.post("home", "x")
    assert sent  # it was sent, and still counts as failed


async def test_name_mismatch_in_a_reconnect_reaches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    fatal: list[Exception] = []
    bus, _ = _unit_bus(on_fatal=fatal.append)

    async def boom() -> None:
        raise NameMismatch("x")

    monkeypatch.setattr(bus, "_open", boom)
    bus._connected = True
    task = asyncio.create_task(bus._supervise())
    bus._wake.set()
    await asyncio.wait_for(task, 2)
    assert len(fatal) == 1 and isinstance(fatal[0], NameMismatch)
    for _ in range(2):  # every later send raises it
        with pytest.raises(NameMismatch):
            await bus.post("home", "x")
    with pytest.raises(NameMismatch):
        await bus.dm("agent-b", "x")


async def test_cancel_during_connect_closes_the_half_open_client(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    clients: list[Any] = []

    class HangingClient:
        def __init__(self) -> None:
            self.closed = False
            clients.append(self)

        async def connect(self, *a: Any, **k: Any) -> None:
            entered.set()
            await asyncio.sleep(3600)

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(natsbus, "NatsClient", HangingClient)
    bus, _ = _unit_bus()
    task = asyncio.create_task(bus._open())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(clients) == 1 and clients[0].closed and bus._nc is None


# --- against a real nats-server --------------------------------------------------------------------

# The streams exactly as services/auth/internal/consumers/streams.go BusStreamJSON("bus", 0) and
# PresenceStreamJSON("presence", 0) make them (DuplicateWindow 100 ms = 100000000 ns).
_STREAM_COMMON = ('"retention":"limits","storage":"file","discard":"old",%s,"num_replicas":1,"no_ack":true,'
                  '"duplicate_window":100000000,"allow_direct":false,"mirror_direct":false,"deny_delete":true,'
                  '"deny_purge":true,"allow_rollup_hdrs":false,"allow_msg_ttl":false,"allow_msg_counter":false,'
                  '"allow_atomic":false,"allow_msg_schedules":false,"allow_batched":false')
BUS_STREAM = ('{"name":"BUS","subjects":["bus.>"],' + _STREAM_COMMON %
              '"max_age":1209600000000000,"max_bytes":1073741824,"max_msg_size":65536' + "}")
PRESENCE_STREAM = ('{"name":"PRESENCE","subjects":["presence.>"],' + _STREAM_COMMON %
                   '"max_age":86400000000000,"max_msgs_per_subject":1,"max_bytes":16777216,"max_msg_size":4096' + "}")
HOUR_NS = 3_600_000_000_000


def _consumer_specs(agent: str, channels: list[str], since: dict[str, int]) -> list[tuple[str, str, dict[str, Any]]]:
    """Consumers exactly like consumers/manager.go desired() and config(): (stream, name, config)."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    filters = ["presence.*"]

    def cfg(name: str, deliver: str, flt: list[str], policy: str = "new") -> dict[str, Any]:
        c: dict[str, Any] = {"durable_name": name, "deliver_subject": deliver, "ack_policy": "none",
                             "deliver_policy": policy, "inactive_threshold": HOUR_NS,
                             "metadata": {"agent": agent}}
        if len(flt) == 1:
            c["filter_subject"] = flt[0]
        else:
            c["filter_subjects"] = flt
        return c

    for ch in channels:
        c = cfg(f"{agent}_ch_{ch}", f"deliver.{agent}.ch.{ch}", [f"bus.ch.{ch}.*"])
        if since.get(ch):  # what the login service does with `since`
            c["deliver_policy"], c["opt_start_seq"] = "by_start_sequence", since[ch] + 1
        out.append(("BUS", c["durable_name"], c))
        filters.append(f"presence.crown.{ch}.*")
    dm = cfg(f"{agent}_dm", f"deliver.{agent}.dm", [f"bus.dm.{agent}.*"])
    out.append(("BUS", dm["durable_name"], dm))
    pr = cfg(f"{agent}_presence", f"deliver.{agent}.presence", filters, "last_per_subject")
    out.append(("PRESENCE", pr["durable_name"], pr))
    return out


async def _api(nc: NatsClient, subject: str, body: dict[str, Any] | str | None = None) -> dict[str, Any]:
    data = body if isinstance(body, str) else json.dumps(body or {})
    r = await nc.request(subject, data.encode(), timeout=5)
    out: dict[str, Any] = json.loads(r.data)
    assert "error" not in out, out
    return out


async def _login_service(admin: NatsClient, agent: str, channels: list[str], since: dict[str, int]) -> None:
    for stream, name, cfg in _consumer_specs(agent, channels, since):
        if since and stream == "BUS" and "_ch_" in name and since.get(name.split("_ch_", 1)[1]):
            await _api(admin, f"$JS.API.CONSUMER.DELETE.{stream}.{name}")
        await _api(admin, f"$JS.API.CONSUMER.CREATE.{stream}.{name}", {"stream_name": stream, "config": cfg})


class Rig:
    def __init__(self, url: str, admin: NatsClient, names: dict[str, str], logins: dict[str, FakeLogin]) -> None:
        self.url, self.admin, self.names, self.logins = url, admin, names, logins
        self.buses: list[NatsBus] = []

    async def agent(self, who: str, channels: list[str] | None = None, bus_kw: dict[str, Any] | None = None,
                    **kw: Any) -> tuple[NatsBus, list[Event]]:
        channels = channels or ["home"]
        login = self.logins[who]
        events: list[Event] = []
        bus = NatsBus(NatsSettings(servers=(self.url,), home=channels[0], guests=tuple(channels[1:]), **kw),
                      login, who, events.append, **{"heartbeat_sec": 0.2, **(bus_kw or {})})
        assert bus.name == self.names[who]
        await _login_service(self.admin, bus.name, channels, {})
        await bus.start()
        self.buses.append(bus)
        return bus, events


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_binary() -> str | None:
    return os.environ.get("MM_NATS_SERVER") or shutil.which("nats-server")


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    if os.environ.get("MM_TEST_NATS") != "1":
        pytest.skip("MM_TEST_NATS=1 runs the tests against a real nats-server")
    binary = _server_binary()
    if not binary or not Path(binary).exists():
        pytest.fail("nats-server 2.15.3 not found: set MM_NATS_SERVER=<path> or put it on PATH")
    logins = {"alice": FakeLogin(), "bob": FakeLogin()}
    names = {w: agent_name(w, lg.nkey().public_key) for w, lg in logins.items()}
    admin_pw = os.urandom(8).hex()
    users = [f'{{user: admin, password: "{admin_pw}"}}']
    for w, lg in logins.items():
        n = names[w]
        # exactly what callout/perms.go grants for the channel `home`
        users.append(
            f'{{nkey: "{lg.nkey().public_key}", permissions: {{publish: {{allow: ["bus.ch.home.{n}", '
            f'"presence.crown.home.{n}", "bus.dm.*.{n}", "presence.{n}"]}}, '
            f'subscribe: {{allow: ["deliver.{n}.>"]}}}}}}')
    port = _free_port()
    conf = tmp_path / "nats.conf"
    conf.write_text(f'port: {port}\nhost: 127.0.0.1\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n'
                    f'accounts {{ BUS {{ jetstream: enabled\n users: [{", ".join(users)}] }} }}\n')
    proc = subprocess.Popen([binary, "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    admin = NatsClient()
    try:
        url = f"nats://127.0.0.1:{port}"
        for _ in range(100):
            try:
                await admin.connect(url, user="admin", password=admin_pw, allow_reconnect=False)
                break
            except Exception:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("nats-server did not start")
        for name, cfg in (("BUS", BUS_STREAM), ("PRESENCE", PRESENCE_STREAM)):
            await _api(admin, f"$JS.API.STREAM.CREATE.{name}", cfg)
        r = Rig(url, admin, names, logins)
        yield r
        for b in r.buses:
            await b.stop()
        await admin.close()
    finally:
        proc.terminate()
        proc.wait(10)


async def until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def test_post_reply_reaction_dm(rig: Rig) -> None:
    alice, a_ev = await rig.agent("alice")
    bob, b_ev = await rig.agent("bob")
    await alice.post("home", "hello", props={"x_k": "v"})
    await until(lambda: len(b_ev) == 1)
    first = b_ev[0]
    assert (first.where, first.sender, first.message.text) == ("home", alice.name, "hello")
    assert first.message.props == {"x_k": "v"} and first.stream_seq >= 1
    assert abs(first.time - time.time()) < 30
    await bob.post("home", "reply", root=first.stream_seq)
    await until(lambda: len(a_ev) == 2)  # alice's own post comes back too, then bob's reply
    reply = a_ev[1]
    assert (reply.sender, reply.message.root) == (bob.name, first.stream_seq) and reply.stream_seq > first.stream_seq
    await bob.react("home", first.stream_seq, "thumbsup")
    await until(lambda: len(a_ev) == 3)
    assert (a_ev[2].sender, a_ev[2].message.kind, a_ev[2].message.ref, a_ev[2].message.emoji) == (
        bob.name, "reaction", first.stream_seq, "thumbsup")
    await bob.unreact("home", first.stream_seq, "thumbsup")
    await until(lambda: len(a_ev) == 4)
    assert a_ev[3].message.kind == "unreaction"
    await alice.dm(bob.name, "psst")
    await until(lambda: any(e.where == "dm" for e in b_ev))
    dm = next(e for e in b_ev if e.where == "dm")
    assert (dm.sender, dm.message.text) == (alice.name, "psst")
    assert not any(e.where == "dm" for e in a_ev)


async def test_forged_sender_in_body_is_ignored(rig: Rig) -> None:
    _, b_ev = await rig.agent("bob")
    forged = {"v": 1, "kind": "post", "text": "x", "sender": rig.names["alice"], "user": "alice"}
    await rig.admin.publish("bus.ch.home.mallory", json.dumps(forged).encode(),
                            headers={"Sender": rig.names["alice"]})
    await rig.admin.flush()
    await until(lambda: len(b_ev) == 1)
    assert b_ev[0].sender == "mallory"


async def test_malformed_is_dropped_and_logged(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    _, b_ev = await rig.agent("bob")
    await rig.admin.publish("bus.ch.home.mallory", b'{"v":9,"kind":"post","text":"SECRET-CONTENT"}')
    await rig.admin.publish("bus.ch.home.mallory", encode(Message("post", "fine")))
    await rig.admin.flush()
    await until(lambda: len(b_ev) == 1)
    assert b_ev[0].message.text == "fine"
    assert "SECRET-CONTENT" not in caplog.text and "dropped a malformed" in caplog.text


async def test_presence_and_crown(rig: Rig) -> None:
    alice, _ = await rig.agent("alice")
    bob, _ = await rig.agent("bob")
    await until(lambda: rig.names["alice"] in bob.presence_snapshot()
                and rig.names["bob"] in alice.presence_snapshot())
    assert bob.presence_snapshot()[rig.names["alice"]][1] == "online"
    t0 = bob.presence_snapshot()[rig.names["alice"]][0]
    await until(lambda: bob.presence_snapshot()[rig.names["alice"]][0] > t0)  # the heartbeat
    assert bob.holder("home") is None
    await alice.claim("home")
    await until(lambda: bob.holder("home") == alice.name)
    await bob.claim("home")
    await until(lambda: bob._claims.get("home", {}).get(bob.name))
    assert bob.holder("home") == alice.name  # the lowest sequence holds
    await alice.claim("home")  # renewed: behind bob's claim now
    await until(lambda: bob.holder("home") == bob.name)
    await bob.stop()
    rig.buses.remove(bob)
    await until(lambda: alice.presence_snapshot()[rig.names["bob"]][1] == "bye")
    assert alice.holder("home") == alice.name


async def test_connect_json_and_reconnect_with_fresh_token_and_since(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hellos: list[dict[str, Any]] = []
    real = natsbus.build_hello

    def spy(*a: Any, **k: Any) -> str:
        h = real(*a, **k)
        hellos.append(json.loads(h))
        return h

    monkeypatch.setattr(natsbus, "build_hello", spy)
    alice_login, bob_login = rig.logins["alice"], rig.logins["bob"]

    bob, b_ev = await rig.agent("bob")
    alice, a_ev = await rig.agent("alice")
    assert hellos[0] == {"token": "tok-1", "home": "home"}  # bob was first, no since yet
    await alice.post("home", "one")
    await until(lambda: any(e.message.text == "one" for e in b_ev))
    seen = b_ev[-1].stream_seq

    # The login service, as it does at each login: restart the consumer after the last seen sequence.
    async def at_login(call: int) -> None:
        if call == 2:
            await alice.post("home", "while bob is away")  # nobody receives it on bob's side now
            await asyncio.sleep(0.3)
            await _login_service(rig.admin, rig.names["bob"], ["home"], {"home": bob.since()["home"]})

    bob_login.hook = at_login
    assert bob._nc is not None
    bob._nc._transport.close()  # the server closes the connection at expiry; here, the socket
    await until(lambda: bob_login.calls == 2 and bob._connected, timeout=10)
    assert hellos[-1]["token"] == "tok-2" and hellos[-1]["since"] == {"home": seen}
    await alice.post("home", "after")
    await until(lambda: any(e.message.text == "after" for e in b_ev))
    texts = [e.message.text for e in b_ev if e.where == "home"]
    assert texts == ["one", "while bob is away", "after"]  # nothing lost, nothing repeated
    seqs = [e.stream_seq for e in b_ev]
    assert seqs == sorted(set(seqs))
    assert alice_login.calls == 1
    assert "tok-" not in caplog.text and "tok-" not in repr(bob)


async def test_a_name_the_credential_does_not_carry_stops_the_bus(rig: Rig) -> None:
    """The credential's name is the authority: a host whose computed name differs
    (here a different spelling of the owner) is refused its own deliver subjects,
    stops with NameMismatch and sends nothing."""
    seen: list[str] = []

    async def tap(msg: Any) -> None:
        seen.append(msg.subject)

    await rig.admin.subscribe("bus.>", cb=tap)
    await rig.admin.subscribe("presence.>", cb=tap)
    await rig.admin.flush()
    wrong = NatsBus(NatsSettings(servers=(rig.url,), home="home"), rig.logins["alice"], "Alice",
                    lambda e: None, heartbeat_sec=0.2)
    assert wrong.name != rig.names["alice"]
    with pytest.raises(NameMismatch):
        await wrong.start()
    await asyncio.sleep(0.3)
    assert seen == []
    # The right name starts and announces itself.
    right, _ = await rig.agent("alice")
    await until(lambda: f"presence.{right.name}" in seen)


async def test_a_refused_publish_is_not_a_success(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    seen: list[str] = []

    async def tap(msg: Any) -> None:
        seen.append(msg.subject)

    await rig.admin.subscribe("bus.>", cb=tap)
    await rig.admin.subscribe("presence.crown.>", cb=tap)
    await rig.admin.flush()
    # `extra` is a channel the bus asked for, but the static permissions only grant `home`.
    alice, _ = await rig.agent("alice", ["home", "extra"])
    with pytest.raises(natsbus.NatsBusError, match="refused"):
        await alice.post("extra", "SECRET-CONTENT")
    with pytest.raises(natsbus.NatsBusError, match="refused"):
        await alice.react("extra", 1, "thumbsup")
    with pytest.raises(natsbus.NatsBusError, match="refused"):
        await alice.claim("extra")
    # not asked for at all: refused locally, before anything is published
    with pytest.raises(natsbus.NatsBusError, match="asked for"):
        await alice.post("elsewhere", "SECRET-CONTENT")
    assert seen == []  # nothing reached the stream
    assert "SECRET-CONTENT" not in caplog.text
    # the connection is fine, and a granted channel still works
    await alice.post("home", "fine")
    await alice.claim("home")
    await rig.admin.flush()
    await until(lambda: f"bus.ch.home.{alice.name}" in seen and f"presence.crown.home.{alice.name}" in seen)


async def test_the_bus_renews_before_the_credential_expires(rig: Rig) -> None:
    bob_login = rig.logins["bob"]
    bob_ref: list[NatsBus] = []

    async def at_login(call: int) -> None:
        if call >= 2:  # what the login service does at each login
            await _login_service(rig.admin, rig.names["bob"], ["home"], {"home": bob_ref[0].since().get("home", 0)})

    bob_login.hook = at_login
    alice, _ = await rig.agent("alice")
    bob, b_ev = await rig.agent("bob", bus_kw={"renew_sec": 0.6})
    bob_ref.append(bob)
    sent: list[str] = []
    stop = asyncio.Event()

    async def talk(bus: NatsBus, who: str) -> None:
        i = 0
        while not stop.is_set():
            text = f"{who}{i}"
            await bus.post("home", text)  # never "not connected", also during the renewal
            sent.append(text)
            i += 1
            await asyncio.sleep(0.02)

    talkers = [asyncio.create_task(talk(alice, "a")), asyncio.create_task(talk(bob, "b"))]
    await until(lambda: bob_login.calls >= 2 and bob._connected, timeout=10)  # renewed on its own
    first_after = len(sent)
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.gather(*talkers)  # raises if a send failed
    assert len(sent) > first_after
    last = sent[-1]
    await until(lambda: any(e.message.text == last for e in b_ev))
    await asyncio.sleep(0.2)
    got = [e.message.text for e in b_ev if e.where == "home"]
    assert sorted(got) == sorted(set(got)), "a message twice"
    assert set(got) == set(sent), "a message lost in the renewal"
    seqs = [e.stream_seq for e in b_ev if e.where == "home"]
    assert seqs == sorted(seqs)
