"""The NATS transport (natsbus.py, ADR-014 point 16): unit tests, and tests against a real
nats-server 2.15.3 (CB_NATS_SERVER=<path>, or on PATH). The server tests are skipped unless
MM_TEST_NATS=1; with it, a missing binary is a failure, not a skip (CI sets it).
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import nkeys
import pytest
from nats.aio.client import Client as NatsClient
from nats.aio.errors import ErrAuthorization

from mattermost_ai_agent_bus import a2a, natsbus
from mattermost_ai_agent_bus.login import NKey
from mattermost_ai_agent_bus.natsbus import (
    Event,
    GapTracker,
    Message,
    CARD_ASKS_MAX,
    CARD_BRANCH_MAX,
    CARD_MODEL_MAX,
    CARD_TASK_MAX,
    MessageError,
    NatsBus,
    Notice,
    NameMismatch,
    NatsSettings,
    PresenceCard,
    agent_name,
    build_hello,
    card_asks,
    card_branch,
    card_harness,
    card_text,
    crown_candidates,
    decode_notice,
    crown_holder,
    decode,
    decode_attestation,
    decode_presence,
    decode_presence_bot,
    decode_presence_card,
    decode_presence_name,
    encode,
    encode_claim,
    encode_presence,
    link_tag,
    owner_of_token,
    owner_token,
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
    # A session part (#163, RT-6.36): two sessions of one owner on one shared key are two
    # names; the login service's vector is the same.
    assert agent_name("alice", VECTOR_NKEY, prefix="", session="abcdefghijklmnopqrstuvwxyz") \
        == "eadecaajdhhzsahypvqfemgypy"


class _RecordingWaits:
    """A wait iterator that records how often it was pulled, without sleeping."""

    def __init__(self, cap: float) -> None:
        self.cap = cap
        self.pulls = 0

    def __iter__(self) -> "_RecordingWaits":
        return self

    def __next__(self) -> float:
        self.pulls += 1
        return 0.0


def test_a_login_failure_backs_off_slower_than_a_server_that_is_away(monkeypatch) -> None:
    """#222 review: while the server does not answer, a reconnect is tried again within
    5 s; a login failure may need a human or a restarted login service and keeps the
    slower growth to 30 s."""
    made: list[_RecordingWaits] = []

    def fake_waits(jitter: Any = None, *, cap: float = natsbus.RECONNECT_MAX_SEC) -> Any:
        waits = _RecordingWaits(cap)
        made.append(waits)
        return waits

    monkeypatch.setattr(natsbus, "reconnect_waits", fake_waits)

    async def run(exc: Exception) -> list[tuple[float, int]]:
        made.clear()
        bus = natsbus.NatsBus.__new__(natsbus.NatsBus)
        bus._stopping = False
        bus._wake = asyncio.Event()
        bus._planned = False
        bus._ready = asyncio.Event()
        bus._send_lock = asyncio.Lock()
        calls = []

        async def fake_open() -> None:
            calls.append(1)
            if len(calls) >= 2:  # two failures say which wait it took; then stop
                bus._stopping = True
            raise exc

        async def fake_close() -> None:
            return

        bus._open = fake_open  # type: ignore[method-assign]
        bus._close_old = fake_close  # type: ignore[method-assign]
        bus._wake.set()
        await asyncio.wait_for(bus._supervise(), 5)
        return [(w.cap, w.pulls) for w in made]

    cases = ((ConnectionError("the server is down"), False),
             (natsbus.LoginError("the login failed"), True),
             (ErrAuthorization(), True))  # the auth callout refused the credential
    for exc, login_expected in cases:
        waits = asyncio.run(run(exc))
        pulls = {cap: count for cap, count in waits}
        # The first attempt after a drop waits on the jittered server schedule; a login
        # failure then takes the slower one.
        assert pulls.get(natsbus.RECONNECT_MAX_SEC, 0) >= 1, (exc, waits)
        assert (pulls.get(natsbus.LOGIN_RECONNECT_MAX_SEC, 0) >= 1) is login_expected, (exc, waits)


def test_the_reconnect_wait_doubles_to_five_seconds() -> None:
    """#222: the wait before a reconnect attempt starts at a second and doubles, but stops
    at 5 s, so a nats-server that comes back is reached again soon instead of after the
    next 30 s step (the rig measured up to 20 s beyond the downtime before, 1 to 5 s now)."""
    waits = natsbus.reconnect_waits(jitter=lambda: 1.0)
    assert [next(waits) for _ in range(7)] == [1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    # The jitter only stretches a wait below the cap.
    stretched = natsbus.reconnect_waits(jitter=lambda: 0.5)
    assert [next(stretched) for _ in range(3)] == [0.5, 1.0, 2.0]


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


def test_a_message_get_answer_is_no_attestation() -> None:
    """The server checks a request's reply subject against no publish permission, so an
    answer can land on presence.attest.* (RT-6.36 step 1, #90). With direct get off, what
    arrives is the message-get answer: JSON with the key's value base64 inside. The strict
    decoder refuses it, so the route writes noise, never a forged attestation."""
    value = b'{"agent":"victim","owner":{"anchor":"alice"},"trusted":[],"cut":false,"iat":1,"exp":2}'
    answer = (b'{"message":{"subject":"$KV.BUS_BRIDGE.k1","seq":1,"data":"' +
              base64.b64encode(value) + b'"}}')
    with pytest.raises(MessageError):
        decode_attestation(answer, "victim")
    # the value on its own is no attestation either: it is not v1 (no "v": 1)
    with pytest.raises(MessageError):
        decode_attestation(value, "victim")


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


def test_the_owner_token_of_a_presence_subject() -> None:
    """RT-6.37: the owner rides in the presence subject `<presence>.<owner>.<name>`, as Go's
    ownerToken writes it: "." as "~" (no subject token holds "."; no username holds "~")."""
    for owner, token in (("alice", "alice"), ("andreas.dembach", "andreas~dembach"), ("a_b-c", "a_b-c"),
                         ("a.b.", "a~b~")):
        assert owner_token(owner) == token and owner_of_token(token) == owner
    assert owner_token("a.b") != owner_token("a_b")  # two owners never become one
    assert owner_of_token("-") == "" and owner_of_token("a.b") == "" and owner_of_token("") == ""


@pytest.mark.parametrize("owner", ["Alice", "ALICE", "alice ", " alice", "al\nice", "alice\n", "äl", ""])
def test_an_owner_counts_only_in_the_one_form_the_bus_compares(owner: str) -> None:
    """RT-6.37: one namespace. The owner is compared with Mattermost usernames
    (CB_TRUSTED_HUMANS, the owners of Mattermost bots), which are lower case; nothing is
    normalised, so an owner in any other form gets the token "-" and is no owner: two
    owners never become one."""
    assert owner_token(owner) == "-"
    assert owner_of_token(owner) == ""


def test_presence_and_claim_bodies() -> None:
    for s in ("online", "away", "bye"):
        assert decode_presence(encode_presence(s)) == s
    assert encode_claim() == b'{"v":1}'
    for bad in (b'{"v":1}', b'{"v":1,"status":"gone"}', b'{"v":2,"status":"online"}', b"[]"):
        with pytest.raises(MessageError):
            decode_presence(bad)
    with pytest.raises(MessageError):
        encode_presence("gone")


BOT = "a1b2c3d4e5f6g7h8i9j0k1l2m3"


def test_presence_names_its_bot() -> None:
    """RT-6.37: the presence body may carry the Mattermost bot's id; a bad one never drops the status."""
    assert encode_presence("online") == b'{"v":1,"status":"online"}'  # no key without a bot
    body = encode_presence("away", BOT)
    assert decode_presence(body) == "away" and decode_presence_bot(body) == BOT
    assert decode_presence_bot(encode_presence("online")) == ""
    for bad in ("short", BOT.upper(), BOT + "x", " " + BOT, 7, None, [BOT], ""):
        raw = json.dumps({"v": 1, "status": "online", "bot": bad}).encode()
        assert decode_presence(raw) == "online" and decode_presence_bot(raw) == ""
    assert decode_presence_bot(b"junk") == ""


def test_presence_states_its_bots_username() -> None:
    """ADR-015 (the operator console's card name): the presence may carry the bot's
    username beside its id; a bad one is ignored and never drops the status."""
    body = encode_presence("online", BOT, "agent-himalia-nats-pilo-772844")
    assert json.loads(body) == {"v": 1, "status": "online", "bot": BOT,
                                "name": "agent-himalia-nats-pilo-772844"}
    assert decode_presence_name(body) == "agent-himalia-nats-pilo-772844"
    assert decode_presence_name(encode_presence("online", BOT)) == ""
    for bad in ("Agent-X", "-x", "a" * 65, "a b", 7, None, ["a"], ""):
        raw = json.dumps({"v": 1, "status": "online", "name": bad}).encode()
        assert decode_presence(raw) == "online" and decode_presence_name(raw) == ""
    assert decode_presence_name(b"junk") == ""


def test_a_bus_states_only_a_valid_username() -> None:
    bus, _ = _unit_bus()
    bus.link_bot(BOT, "agent-mac-ai-agent-716d72")
    assert bus._bot_name == "agent-mac-ai-agent-716d72"
    bus.link_bot(BOT, "Not A Name")
    assert bus._bot == BOT and bus._bot_name == ""


def test_link_tag_is_the_start_of_the_hash_part() -> None:
    name = "agent-" + "abcdefghijklmnopqrstuvwxyz"[:26]
    assert link_tag(name) == "abcdefghijklmnop" and link_tag(VECTOR_NAME) == VECTOR_NAME[:16]
    assert link_tag("agent-short") == "" and link_tag("") == ""
    assert link_tag("a" * 25) == "" and link_tag("a" * 26) == "a" * 16


def test_a_bus_links_only_a_valid_bot() -> None:
    bus, _ = _unit_bus()
    assert bus._bot == ""
    for bad in ("", "short", BOT.upper(), BOT + "x"):
        with pytest.raises(ValueError):
            bus.link_bot(bad)
    bus.link_bot(BOT)
    assert bus._bot == BOT


# --- unit: JetStream metadata, gaps, crown, CONNECT --------------------------------------------

def test_parse_ack() -> None:
    a = parse_ack("$JS.ACK.BUS.c1.1.17.5.1795388483779185000.0")
    assert a is not None and (a.stream_seq, a.consumer_seq) == (17, 5)
    assert a.time == pytest.approx(1795388483.779185)
    b = parse_ack("$JS.ACK._.HASH.BUS.c1.1.18.6.1795388483779185000.2.tok")
    assert b is not None and (b.stream_seq, b.consumer_seq) == (18, 6)
    assert (a.pending, b.pending) == (0, 2)
    c = parse_ack("$JS.ACK.PRESENCE.c1.1.18.6.1795388483779185000.41")
    assert c is not None and c.pending == 41
    d = parse_ack("$JS.ACK.dom.HASH.PRESENCE.c1.1.18.6.1795388483779185000.7")  # domain form, 11 tokens
    assert d is not None and d.pending == 7 and d.stream_seq == 18
    for bad in ("$JS.ACK.BUS.c1.1.17.5.1795388483779185000.x", "$JS.ACK.BUS.c1.1.17.5.1795388483779185000.-1"):
        assert parse_ack(bad) is None
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


_BUS_KW = ("gap_reconnect_sec", "renew_sec", "on_fatal", "heartbeat_sec", "send_wait_sec", "since", "on_since")


def _unit_bus(**kw: Any) -> tuple[NatsBus, list[Event]]:
    events: list[Event] = []
    bus_kw = {k: kw.pop(k) for k in _BUS_KW if k in kw}
    bus = NatsBus(_settings(**kw), FakeLogin(), "alice", events.append, **bus_kw)
    return bus, events


def _reply(bus: NatsBus, where: str, sseq: int, cseq: int, t: float | None = None) -> str:
    """The JetStream reply subject of the bus's own consumer for `where`."""
    consumer = f"{bus.name}_dm" if where == "@dm" else f"{bus.name}_ch_{where}"
    return f"$JS.ACK.BUS.{consumer}.1.{sseq}.{cseq}.{int((time.time() if t is None else t) * 1e9)}.0"


def _seqs(since: Mapping[str, tuple[int, int]]) -> dict[str, int]:
    """A cursor's sequences, as the hello and the login service read them (#213)."""
    return {key: pair[0] for key, pair in since.items()}


def _presence_reply(bus: NatsBus, sseq: int, cseq: int, t: float) -> str:
    return f"$JS.ACK.PRESENCE.{bus.name}_presence.1.{sseq}.{cseq}.{int(t * 1e9)}.0"


class FakeMsg:
    def __init__(self, subject: str, reply: str | None, data: bytes) -> None:
        self.subject, self.reply, self.data = subject, reply, data


def test_the_claim_order_is_the_server_time_then_the_sequence() -> None:
    """RT-6.38 step 1 (H2): a stream made anew restarts its sequence, so the sequence alone is
    no order between agents: A's old claim (seq 500) stays before C's new one (seq 100)."""
    now = 1000.0
    pres = {"a": (now - 10, "online"), "c": (now - 10, "online")}
    claims, at = {"a": 500, "c": 100}, {"a": now - 5000, "c": now - 100}
    assert crown_candidates(claims, pres, now, None, 90.0, at) == ["a", "c"]
    assert crown_candidates(claims, pres, now) == ["c", "a"]  # by the sequence alone, the old rule
    at = {"a": now - 100, "c": now - 100}  # the same time: the sequence decides
    assert crown_candidates(claims, pres, now, None, 90.0, at) == ["c", "a"]


async def test_a_claim_older_than_the_consumer_made_anew_is_forgotten_and_claimed_again() -> None:
    """RT-6.38 step 1 (H2): when the presence consumer starts over (its delivery number
    falls back), remembered claims are gone from the stream: this agent's own is missing
    and the tick claims again."""
    bus, _ = _unit_bus()
    await _claim(bus, bus.name, "home", 500, T0 - 1000)
    await _claim(bus, "agent-b", "home", 501, T0 - 900)
    assert bus.claimed("home", bus.name)
    await bus._on_presence_msg(FakeMsg("presence.member.home.agent-b", _presence_reply(bus, 600, 1, T0),
                                       encode_claim()))  # delivery number 1 again: made anew
    assert not bus.claimed("home", bus.name) and not bus.claimed("home", "agent-b")
    await _claim(bus, "agent-c", "home", 601, T0)
    assert bus.claimed("home", "agent-c") and bus.claim_info("home").get(bus.name) is None


async def test_the_echo_of_the_own_presence_is_timed_by_the_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 2 (H3): the age of the last own presence delivered, never server time."""
    mono = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    bus, _ = _unit_bus()
    mono[0] += 80
    assert bus.own_echo_age() == 80
    await _presence(bus, "agent-other", 1, T0)
    assert bus.own_echo_age() == 80  # somebody else's presence is no echo of ours
    await _presence(bus, bus.name, 2, T0 + 1)
    assert bus.own_echo_age() == 0
    mono[0] += 30
    assert bus.own_echo_age() == 30


async def test_a_missing_own_echo_triggers_one_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 2: no echo of the own presence past OWN_ECHO_SEC while connected: one
    reconnect, not more within the gap interval; a normal echo: none."""
    mono = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    bus, _ = _unit_bus()
    bus._connected = True
    mono[0] += natsbus.OWN_ECHO_SEC - 1
    bus._check_own_echo()
    assert not bus._wake.is_set()  # still within the window
    await _presence(bus, bus.name, 1, T0)  # the echo comes
    mono[0] += natsbus.OWN_ECHO_SEC - 1
    bus._check_own_echo()
    assert not bus._wake.is_set()
    mono[0] += 2  # now missing past the window
    bus._check_own_echo()
    assert bus._wake.is_set()
    bus._wake.clear()
    mono[0] += 5
    bus._check_own_echo()  # the same outage: no second reconnect
    assert not bus._wake.is_set()
    bus._connected = False
    mono[0] += 1000
    bus._check_own_echo()  # not connected: the supervisor is at it already
    assert not bus._wake.is_set()


async def test_an_echo_a_reconnect_does_not_bring_back_backs_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 2: when the echo stays away after reconnects, the wait doubles up to the
    credential renewal (no reconnect in every window); a real echo resets it; a refused
    presence subject (another release of the login service) reconnects never."""
    mono = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: mono[0])
    bus, _ = _unit_bus()
    bus._connected = True
    times: list[float] = []
    for _ in range(int(20 * 60 / 5)):  # 20 minutes, a beat every 5 s
        mono[0] += 5
        bus._check_own_echo()
        if bus._wake.is_set():
            bus._wake.clear()
            times.append(mono[0])
            bus._own_echo = mono[0]  # _open of the reconnect gives a new window, no echo comes
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert 2 <= len(times) <= 5, times
    assert all(b > a for a, b in zip(gaps, gaps[1:], strict=False)), gaps  # each wait longer than the last
    assert max(gaps) <= natsbus.RENEW_SEC + 5
    await _presence(bus, bus.name, 1, T0)  # a real echo: the next outage starts afresh
    assert bus._echo_backoff == 0.0
    bus._denied = [bus._presence_subject]
    mono[0] += 10_000
    bus._check_own_echo()
    assert not bus._wake.is_set()  # refused: a reconnect cures nothing


async def test_the_heartbeat_checks_the_own_echo() -> None:
    """RT-6.38 step 2: the heartbeat loop asks for the reconnect when the echo is missing."""
    bus, _ = _unit_bus(heartbeat_sec=0.01)
    bus._connected = True
    bus._own_echo = time.monotonic() - natsbus.OWN_ECHO_SEC - 1
    task = asyncio.create_task(bus._heartbeat())
    try:
        await asyncio.wait_for(bus._wake.wait(), 2)
    finally:
        task.cancel()


async def test_forgetting_the_claims_unsyncs_until_the_replay_is_read() -> None:
    """RT-6.38 step 1: after the consumer is made anew the view is not synced until a
    delivery with pending 0, so a tick in the replay window does not claim."""
    bus, _ = _unit_bus()
    await _claim(bus, bus.name, "home", 500, T0 - 1000)
    assert bus.synced  # pending 0
    replay = _presence_reply(bus, 600, 1, T0)[:-1] + "3"  # delivery number 1 again, 3 still pending
    await bus._on_presence_msg(FakeMsg("presence.member.home.agent-b", replay, encode_claim()))
    assert not bus.synced and not bus.claimed("home", bus.name)
    await bus._on_presence_msg(FakeMsg("presence.member.home.agent-b", _presence_reply(bus, 603, 4, T0),
                                       encode_claim()))
    assert bus.synced


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
    assert len(events) == 1 and _seqs(bus.since()) == {"home": 12}
    # consumer sequence 5 -> 7: a gap. The message is dropped, `since` stays, a reconnect is asked for.
    assert not bus._wake.is_set()
    await bus._on_deliver("home", subj, _reply(bus, "home", 14, 7), ok)
    assert len(events) == 1 and _seqs(bus.since()) == {"home": 12} and bus._wake.is_set()
    await bus._on_deliver("home", subj, _reply(bus, "home", 15, 8), ok)  # still dropped until the reconnect
    assert len(events) == 1
    # A second gap within the interval: no second reconnect now, but the channel stays
    # stale and a reconnect is scheduled for when the interval is over (never lost for good).
    bus._wake.clear()
    await bus._on_deliver("home", subj, _reply(bus, "home", 20, 12), ok)
    assert not bus._wake.is_set() and len(events) == 1 and _seqs(bus.since()) == {"home": 12}
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
    assert _seqs(bus.since()) == {"home": 1}  # `since` is the last accepted one: the gap is replayed
    await bus._on_deliver("home", subj, _reply(bus, "home", 6, 6), ok)
    assert len(events) == 1  # still stale until the reconnect really happens


async def test_dm_gap_and_since_key() -> None:
    bus, events = _unit_bus()
    ok = encode(Message("post", "hi"))
    subj = f"bus.dm.{bus.name}.agent-y"
    await bus._on_deliver("@dm", subj, _reply(bus, "@dm", 7, 1), ok)
    assert _seqs(bus.since()) == {"@dm": 7}
    assert json.loads(build_hello("T", "home", (), False, bus.since()))["since"] == {"@dm": 7}
    await bus._on_deliver("@dm", subj, _reply(bus, "@dm", 9, 3), ok)  # a DM was lost
    assert bus._wake.is_set() and len(events) == 1 and _seqs(bus.since()) == {"@dm": 7}
    await bus._on_deliver("@dm", subj, _reply(bus, "@dm", 7, 4), ok)  # a repeat from since
    assert len(events) == 1


async def test_unit_dm_event_and_repr() -> None:
    bus, events = _unit_bus()
    await bus._on_deliver("@dm", f"bus.dm.{bus.name}.agent-y", _reply(bus, "@dm", 3, 1), encode(Message("post", "hi")))
    assert [(e.where, e.sender) for e in events] == [("@dm", "agent-y")]
    await bus._on_deliver("@dm", "bus.dm.someone-else.agent-y", _reply(bus, "@dm", 4, 2), encode(Message("post", "hi")))
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
    await bus._on_presence_msg(FakeMsg(f"presence.alice.{bus.name}", f"$JS.ACK.PRESENCE.x_presence.1.1.1.{t}.0",
                                       encode_presence("online")))
    assert bus.presence_snapshot() == {}


def test_tls_or_loopback_only() -> None:
    for ok in ("tls://nats.example.org:4222", "nats://localhost:4222", "nats://127.0.0.1:4222",
               "nats://127.9.9.9", "nats://[::1]:4222", "tls://10.0.0.5:4222",
               "wss://nats.example.org:443", "wss://10.0.0.5:443", "ws://localhost:8080",
               "ws://127.0.0.1:8080"):
        NatsSettings(servers=(ok,), home="home")
    for bad in ("nats://nats.example.org:4222", "nats://10.0.0.5:4222", "nats://127.0.0.1.evil.org",
                "ws://nats.example.org:8080", "ws://10.0.0.5:8080", "ws://",
                "localhost:4222", "http://localhost", "nats://", "tls://", "wss://", ""):
        with pytest.raises(ValueError):
            NatsSettings(servers=(bad,), home="home")
    for mixed in (("tls://ok.example.org", "nats://remote.example.org"),
                  ("wss://ok.example.org", "ws://127.0.0.1"),
                  ("tls://ok.example.org", "wss://ok.example.org"),
                  ("nats://127.0.0.1", "ws://127.0.0.1")):
        with pytest.raises(ValueError):
            NatsSettings(servers=mixed, home="home")
    # an upper-case scheme is normalised, never handed to nats-py: it matches `wss://`
    # case-sensitively and would read `WSS://host` as a host name and dial plain TCP
    for raw, want in (("WSS://nats.example.org:443", "wss://nats.example.org:443"),
                      ("WS://127.0.0.1:8080", "ws://127.0.0.1:8080"),
                      ("NATS://127.0.0.1:4222", "nats://127.0.0.1:4222"),
                      ("TLS://nats.example.org:4222", "tls://nats.example.org:4222")):
        assert NatsSettings(servers=(raw,), home="home").servers == (want,)
    with pytest.raises(ValueError):
        NatsSettings(servers=("NATS://nats.example.org:4222",), home="home")  # plain off loopback
    # and a settings object built around the checks cannot smuggle one in
    s = NatsSettings(servers=("wss://ok.example.org",), home="home")
    object.__setattr__(s, "servers", ("WSS://ok.example.org",))
    with pytest.raises(ValueError):
        NatsBus(s, FakeLogin(), "alice", lambda e: None)


def test_wss_is_tls_inside_the_websocket_and_ws_is_plain() -> None:
    wss = NatsSettings(servers=("wss://nats.example.org:443",), home="home")
    assert wss.tls_context() is not None and natsbus._websocket(wss.servers)
    ws = NatsSettings(servers=("ws://127.0.0.1:8080",), home="home")
    assert ws.tls_context() is None and natsbus._websocket(ws.servers)
    assert not natsbus._websocket(NatsSettings(servers=("tls://h",), home="home").servers)
    # also when settings were built around the check
    s = NatsSettings(servers=("nats://127.0.0.1:1",), home="home")
    object.__setattr__(s, "servers", ("nats://remote.example.org:4222",))
    with pytest.raises(ValueError):
        NatsBus(s, FakeLogin(), "alice", lambda e: None)
    s = NatsSettings(servers=("ws://127.0.0.1:1",), home="home")
    object.__setattr__(s, "servers", ("tls://ok.example.org", "ws://127.0.0.1:1"))
    with pytest.raises(ValueError):
        NatsBus(s, FakeLogin(), "alice", lambda e: None)  # one scheme per pool, at construction


T0 = 1_800_000_000.0


async def _presence(bus: NatsBus, who: str, seq: int, t: float, status: str = "online", bot: str = "",
                    pending: int = 0, owner: str = "-") -> None:
    reply = _presence_reply(bus, seq, seq, t)[:-1] + str(pending)
    await bus._on_presence_msg(FakeMsg(f"presence.{owner}.{who}", reply, encode_presence(status, bot)))


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


async def test_the_owner_comes_from_the_presence_subject_and_presence_from_the_server_clock(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "time", lambda: T0 + 1e9)  # the host's clock is far off
    bus, _ = _unit_bus()
    them = "agent-them"
    await bus._on_presence_msg(FakeMsg(f"presence.bob~b.{them}", _presence_reply(bus, 2, 2, T0),
                                       encode_presence("online")))
    assert bus.owner_of(them) == "bob.b" and bus.presence_of(them) == "online"
    # an older message (a lower stream sequence) never overrides a newer one
    await bus._on_presence_msg(FakeMsg(f"presence.mallory.{them}", _presence_reply(bus, 1, 1, T0 - 1),
                                       encode_presence("away")))
    assert bus.owner_of(them) == "bob.b" and bus.presence_of(them) == "online"
    # a presence under "-" (an owner not in the one form) names none
    await bus._on_presence_msg(FakeMsg(f"presence.-.{them}", _presence_reply(bus, 3, 3, T0 + 30),
                                       encode_presence("away")))
    assert bus.owner_of(them) == "" and bus.presence_of(them) == "away"
    await _presence(bus, "agent-z", 4, T0 + 30 + natsbus.PRESENCE_FRESH_SEC)  # the server clock moves on
    assert bus.presence_of(them) == "offline" and bus.presence_of("agent-nobody") == "offline"
    await _presence(bus, them, 5, T0 + 200, status="bye")
    assert bus.presence_of(them) == "offline"
    # the old form <presence>.<name> is no presence any more
    await bus._on_presence_msg(FakeMsg(f"presence.{them}", _presence_reply(bus, 6, 6, T0 + 201),
                                       encode_presence("online")))
    assert bus.presence_of(them) == "offline"
    # its own presence goes under its owner and name
    assert bus._presence_subject == f"presence.alice.{bus.name}"


async def test_a_presence_stream_made_anew_is_read_by_its_newer_server_time() -> None:
    """A PRESENCE stream made anew starts its sequences at 1: a lower sequence with a newer
    server time is a newer message, not an old one."""
    bus, _ = _unit_bus()
    them = "agent-them"
    await bus._on_presence_msg(FakeMsg(f"presence.bob.{them}", _presence_reply(bus, 500, 500, T0),
                                       encode_presence("online")))
    await bus._on_presence_msg(FakeMsg(f"presence.bob.{them}", _presence_reply(bus, 1, 1, T0 + 40),
                                       encode_presence("away")))
    assert bus.presence_snapshot()[them] == (T0 + 40, "away")


async def test_a_refused_membership_does_not_stop_the_others() -> None:
    """A guest the login service did not grant is refused; the channels after it are
    announced all the same (RT-6.37)."""
    bus, _ = _unit_bus()
    bus._memberships = ["g1", "g2", "lobby"]
    sent: list[str] = []

    async def publish(subject: str, data: bytes) -> None:
        if ".member.g1." in subject:
            raise natsbus.NatsBusError("publish refused by the server: no permission for it")
        sent.append(subject.split(".")[2])

    bus._publish = publish  # type: ignore[method-assign]
    await bus._announce_once()
    assert sent == ["g2", "lobby"]


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


async def test_the_bot_of_a_name_follows_its_accepted_presence_and_ages_out() -> None:
    bus, _ = _unit_bus()
    await _presence(bus, "agent-x", 5, T0, bot=BOT)
    assert bus.bot_of("agent-x") == BOT and bus.bot_of("agent-y") == ""
    await _presence(bus, "agent-x", 2, T0 - 5, bot="z" * 26)  # an older message changes nothing
    assert bus.bot_of("agent-x") == BOT
    await _presence(bus, "agent-x", 6, T0 + 30, bot="z" * 26)
    assert bus.bot_of("agent-x") == "z" * 26
    await _presence(bus, "agent-x", 7, T0 + 60)  # no bot any more
    assert bus.bot_of("agent-x") == ""
    await _presence(bus, "agent-x", 8, T0 + 90, bot="bad")  # a bad one: status counts, no bot
    assert bus.bot_of("agent-x") == "" and bus.presence_of("agent-x") == "online"
    await _presence(bus, "agent-old", 9, T0 + 100, bot=BOT)
    await _presence(bus, "agent-new", 10, T0 + natsbus.MAX_AGE_SEC + 200)  # agent-old is over 24 h old
    assert bus.bot_of("agent-old") == "" and "agent-old" not in bus._bots


async def test_synced_only_with_nothing_pending_and_not_after_a_reconnect() -> None:
    bus, _ = _unit_bus()
    assert not bus.synced
    await _presence(bus, "agent-x", 1, T0, pending=3)
    assert not bus.synced
    await _presence(bus, "agent-x", 2, T0 + 1, pending=0)
    assert bus.synced
    await _presence(bus, "agent-x", 3, T0 + 2, pending=5)
    assert bus.synced  # once read, it stays until the next connection

    async def refuse() -> str:
        raise OSError("down")

    bus._login.access_token = refuse  # type: ignore[method-assign,assignment]
    with pytest.raises(OSError):
        await bus._open()
    assert not bus.synced  # reset at each (re)connect, before anything is read


async def test_claimed_says_whether_an_agent_has_a_claim() -> None:
    bus, _ = _unit_bus()
    await _claim(bus, "agent-x", "home", 3, T0)
    assert bus.claimed("home", "agent-x")
    assert not bus.claimed("home", "agent-y") and not bus.claimed("other", "agent-x")


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
    that times out (the server said nothing) makes the send fail, and the connection
    is given up - nats-py matches PONGs to flushes in order, so a timed-out flush
    would misfit every later one (#222 review)."""
    bus, _ = _unit_bus()
    sent: list[str] = []

    class Silent:
        async def publish(self, subject: str, body: bytes) -> None:
            sent.append(subject)

        async def flush(self, timeout: float = 10) -> None:
            raise asyncio.TimeoutError

    bus._nc, bus._connected = Silent(), True  # type: ignore[assignment]
    with pytest.raises(natsbus.NatsBusError, match="stopped answering"):
        await bus.post("home", "x")
    assert sent  # it was sent, and still counts as failed
    assert bus._connected is False and bus._wake.is_set()


async def test_a_flush_timeout_reconnects_at_most_once_per_interval() -> None:
    """#222 review: a server that answers every agent slowly must not make every agent
    reconnect at once, so only the first flush timeout within FLUSH_RECONNECT_SEC wakes
    the supervisor; the others fail their send all the same, and the text says the post
    may have arrived (the publish went out before the PONG went missing)."""
    bus, _ = _unit_bus()

    class Slow:
        async def publish(self, subject: str, body: bytes) -> None:
            return

        async def flush(self, timeout: int = 10) -> None:
            raise asyncio.TimeoutError

    bus._nc, bus._connected = Slow(), True  # type: ignore[assignment]
    with pytest.raises(natsbus.NatsBusError, match="may have arrived"):
        await bus.post("home", "x")
    assert bus._wake.is_set() and bus._last_flush_reconnect > 0
    bus._wake.clear()
    bus._connected = True
    with pytest.raises(natsbus.NatsBusError, match="may have arrived"):
        await bus.post("home", "y")
    assert not bus._wake.is_set()  # within the interval: no second reconnect


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
    filters = ["presence.*.*"]  # every agent's presence: <owner>.<name> (RT-6.37)

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
        filters += [f"presence.crown.{ch}.*", f"presence.member.{ch}.*"]
    dm = cfg(f"{agent}_dm", f"deliver.{agent}.dm", [f"bus.dm.{agent}.*"])
    if since.get("@dm"):  # the same as the channels: restart after the last DM seen
        dm["deliver_policy"], dm["opt_start_seq"] = "by_start_sequence", since["@dm"] + 1
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


async def _login_service(admin: NatsClient, agent: str, channels: list[str],
                         since: Mapping[str, int | tuple[int, int]]) -> None:
    # The login service reads `since` as sequences from the hello; the bus's cursor also
    # names each message's server time (#213), which the hello does not carry.
    seqs = {key: value[0] if isinstance(value, tuple) else value for key, value in since.items()}
    for stream, name, cfg in _consumer_specs(agent, channels, seqs):
        if seqs and stream == "BUS" and "_ch_" in name and seqs.get(name.split("_ch_", 1)[1]):
            await _api(admin, f"$JS.API.CONSUMER.DELETE.{stream}.{name}")
        if seqs.get("@dm") and name == f"{agent}_dm":
            # as the login service: restart it, or make it anew when JetStream dropped it
            r = await admin.request(f"$JS.API.CONSUMER.DELETE.{stream}.{name}", b"", timeout=5)
            assert json.loads(r.data).get("error", {}).get("err_code") in (None, 10014), r.data
        await _api(admin, f"$JS.API.CONSUMER.CREATE.{stream}.{name}", {"stream_name": stream, "config": cfg})


class Rig:
    def __init__(self, url: str, admin: NatsClient, names: dict[str, str], logins: dict[str, FakeLogin],
                 ws_url: str = "") -> None:
        self.url, self.admin, self.names, self.logins = url, admin, names, logins
        self.ws_url = ws_url
        self.buses: list[NatsBus] = []

    async def agent(self, who: str, channels: list[str] | None = None, bus_kw: dict[str, Any] | None = None,
                    bot: str = "", url: str | None = None, **kw: Any) -> tuple[NatsBus, list[Event]]:
        channels = channels or ["home"]
        login = self.logins[who]
        events: list[Event] = []
        bus = NatsBus(NatsSettings(servers=(url or self.url,), home=channels[0], guests=tuple(channels[1:]), **kw),
                      login, who, events.append, **{"heartbeat_sec": 0.2, **(bus_kw or {})})
        assert bus.name == self.names[who]
        await _login_service(self.admin, bus.name, channels, {})
        if bot:
            bus.link_bot(bot)
        await bus.start()
        self.buses.append(bus)
        return bus, events


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server_binary() -> str | None:
    return os.environ.get("CB_NATS_SERVER") or shutil.which("nats-server")


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    if os.environ.get("MM_TEST_NATS") != "1":
        pytest.skip("MM_TEST_NATS=1 runs the tests against a real nats-server")
    binary = _server_binary()
    if not binary or not Path(binary).exists():
        pytest.fail("nats-server 2.15.3 not found: set CB_NATS_SERVER=<path> or put it on PATH")
    # carol's credential is what an older login service granted: presence.<name> (RT-6.37)
    logins = {"alice": FakeLogin(), "bob": FakeLogin(), "carol": FakeLogin()}
    names = {w: agent_name(w, lg.nkey().public_key) for w, lg in logins.items()}
    admin_pw = os.urandom(8).hex()
    users = [f'{{user: admin, password: "{admin_pw}"}}']
    for w, lg in logins.items():
        n = names[w]
        # exactly what callout/perms.go grants for the channel `home`
        users.append(
            f'{{nkey: "{lg.nkey().public_key}", permissions: {{publish: {{allow: ["bus.ch.home.{n}", '
            f'"presence.crown.home.{n}", "presence.member.home.{n}", "bus.dm.*.{n}", '
            f'"{f"presence.{n}" if w == "carol" else f"presence.{w}.{n}"}"]}}, '
            f'subscribe: {{allow: ["deliver.{n}.>"]}}}}}}')
    port = _free_port()
    ws_port = _free_port()
    conf = tmp_path / "nats.conf"
    conf.write_text(f'port: {port}\nhost: 127.0.0.1\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n'
                    f'websocket {{ port: {ws_port}\n host: 127.0.0.1\n no_tls: true }}\n'
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
        r = Rig(url, admin, names, logins, ws_url=f"ws://127.0.0.1:{ws_port}")
        yield r
        for b in r.buses:
            await b.stop()
        await admin.close()
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.fixture
async def wss_rig(tmp_path: Path) -> AsyncIterator[tuple[Rig, Path]]:
    """The same home over wss://: a websocket listener with a self-signed certificate, which
    the client trusts through CB_NATS_CA_FILE (the test's `tls_ca_file`)."""
    if os.environ.get("MM_TEST_NATS") != "1":
        pytest.skip("MM_TEST_NATS=1 runs the tests against a real nats-server")
    binary = _server_binary()
    if not binary or not Path(binary).exists():
        pytest.fail("nats-server 2.15.3 not found: set CB_NATS_SERVER=<path> or put it on PATH")
    cert, key = _self_signed(tmp_path)
    logins = {"alice": FakeLogin(), "eve": FakeLogin()}
    names = {w: agent_name(w, lg.nkey().public_key) for w, lg in logins.items()}
    admin_pw = os.urandom(8).hex()
    users = [f'{{user: admin, password: "{admin_pw}"}}']
    for w, lg in logins.items():
        n = names[w]
        users.append(
            f'{{nkey: "{lg.nkey().public_key}", permissions: {{publish: {{allow: '
            f'["bus.ch.home.{n}", "presence.crown.home.{n}", "presence.member.home.{n}", '
            f'"bus.dm.*.{n}", "presence.{w}.{n}"]}}, subscribe: {{allow: ["deliver.{n}.>"]}}}}}}')
    port, wss_port = _free_port(), _free_port()
    conf = tmp_path / "nats-wss.conf"
    conf.write_text(f'port: {port}\nhost: 127.0.0.1\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n'
                    f'websocket {{ port: {wss_port}\n host: 127.0.0.1\n tls {{ cert_file: "{cert}", '
                    f'key_file: "{key}" }} }}\n'
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
        r = Rig(url, admin, names, logins, ws_url=f"wss://127.0.0.1:{wss_port}")
        yield r, cert
        for b in r.buses:
            await b.stop()
        await admin.close()
    finally:
        proc.terminate()
        proc.wait(10)


async def until(pred: Callable[[], bool], timeout: float = 5.0, what: str = "") -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("timed out" + (f": {what}" if what else ""))
        await asyncio.sleep(0.02)


async def test_a_ws_redirect_is_refused_and_named_in_the_log(
        caplog: pytest.LogCaptureFixture) -> None:
    """aiohttp follows a 3xx of the WebSocket upgrade, and the CONNECT (with the access token)
    would go to the redirect's target: the client refuses it before a byte goes there, and the
    bus's log says why instead of ending in a bare timeout (Fable's review of #51, RT-6.36, #85)."""
    caplog.set_level(logging.WARNING, logger="mattermost_ai_agent_bus.natsbus")
    target_bytes = bytearray()

    async def target(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        target_bytes.extend(await reader.read(4096))
        writer.close()

    target_srv = await asyncio.start_server(target, "127.0.0.1", 0)
    target_port = target_srv.sockets[0].getsockname()[1]

    async def redirect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{target_port}/nats\r\n"
                     "Content-Length: 0\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    redir_srv = await asyncio.start_server(redirect, "127.0.0.1", 0)
    redir_port = redir_srv.sockets[0].getsockname()[1]
    bus = NatsBus(NatsSettings(servers=(f"ws://127.0.0.1:{redir_port}",), home="home"),
                  FakeLogin(), "alice", lambda e: None, connect_timeout=2)
    try:
        await bus.start()  # the first connect fails, the supervisor retries
        await until(lambda: "redirected" in caplog.text, what="the log names the redirect")
        assert "the access token never reaches another address" in caplog.text
        assert target_bytes == b"", "the redirect target received the CONNECT"
    finally:
        await bus.stop()
        for srv in (redir_srv, target_srv):
            srv.close()
            await srv.wait_closed()


async def test_post_and_reply_over_a_websocket(rig: Rig) -> None:
    """ws://: nats-py's WebSocket transport (aiohttp) carries the same home; a ws agent and a
    tcp agent of one home see each other, so the transport is the only difference (RT-6.36)."""
    alice, a_ev = await rig.agent("alice", url=rig.ws_url)
    bob, b_ev = await rig.agent("bob")  # tcp, the same home
    await alice.post("home", "hello over ws")
    await until(lambda: len(b_ev) == 1, what="bob got alice's ws post")
    await bob.post("home", "reply", root=b_ev[0].stream_seq)
    await until(lambda: len(a_ev) == 2, what="alice got bob's reply over ws")
    assert (a_ev[1].where, a_ev[1].sender) == ("home", bob.name)


async def test_wss_verifies_the_websocket_tls(wss_rig: tuple[Rig, Path],
                                              caplog: pytest.LogCaptureFixture) -> None:
    """wss://: the WebSocket's TLS is verified with CB_NATS_CA_FILE like tls://; without the CA
    the connection is refused, and no token goes over it."""
    rig, ca = wss_rig
    alice, a_ev = await rig.agent("alice", url=rig.ws_url, tls_ca_file=str(ca))
    await alice.post("home", "hello over wss")
    await until(lambda: len(a_ev) == 1, what="alice's own post came back over wss")
    # the bus itself verifies: an agent without CB_NATS_CA_FILE never connects, and the bus's
    # own log names the certificate. An unverified context would connect right here, which is
    # what a mutation of the TLS setup turns red (716d72's blocker on #83).
    caplog.set_level(logging.WARNING, logger="mattermost_ai_agent_bus.natsbus")
    eve, _ = await rig.agent("eve", url=rig.ws_url, bus_kw={"connect_timeout": 3})
    try:
        assert eve._nc is None and not eve._connected
        assert "certificate" in caplog.text.lower()
    finally:
        await eve.stop()


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
    await until(lambda: any(e.where == "@dm" for e in b_ev))
    dm = next(e for e in b_ev if e.where == "@dm")
    assert (dm.sender, dm.message.text) == (alice.name, "psst")
    assert not any(e.where == "@dm" for e in a_ev)


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


async def test_an_agent_alone_in_a_quiet_home_becomes_synced_by_its_own_presence(rig: Rig) -> None:
    """Nobody else is there to send a presence: the agent's own presence comes back over its
    own presence consumer with nothing pending, so it becomes synced (RT-6.37)."""
    alone, _ = await rig.agent("alice")
    await until(lambda: alone.synced)


async def test_presence_and_crown(rig: Rig) -> None:
    alice, _ = await rig.agent("alice", bot="a" * 26)
    bob, _ = await rig.agent("bob")
    await until(lambda: rig.names["alice"] in bob.presence_snapshot()
                and rig.names["bob"] in alice.presence_snapshot())
    assert bob.presence_snapshot()[rig.names["alice"]][1] == "online"
    # RT-6.37: the bot id rides in the presence body; both read the stored presences to the end
    await until(lambda: bob.bot_of(alice.name) == "a" * 26)
    assert alice.bot_of(bob.name) == ""
    await until(lambda: alice.synced and bob.synced)
    # RT-6.37: each proves its owner to the other through the real server
    assert bob.owner_of(alice.name) == "alice" and alice.owner_of(bob.name) == "bob"
    assert bob.presence_of(alice.name) == "online"
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


async def test_the_crown_candidates_are_fresh_members_in_claim_order(rig: Rig) -> None:
    """RT-6.38 step 1 against a real server: two home members claim and the lowest holds; a
    claim without a membership (a guest) never holds; a holder that stops beating passes the
    crown within the freshness window (the server's clock), a bye at once."""
    alice, _ = await rig.agent("alice")
    bob, _ = await rig.agent("bob")
    await until(lambda: alice.synced and bob.synced)
    await bob.claim("home")  # bob claims first, but is no member of the home: a guest
    await alice.claim("home")
    await alice.member("home")
    await until(lambda: alice.is_member("home", alice.name) and bob.claimed("home", alice.name))
    assert bob.candidates("home") == [alice.name]  # the lower claim is no member
    assert alice.holder("home") == bob.name  # the bare rule has no membership: why the crown uses candidates
    await bob.member("home")
    await until(lambda: alice.is_member("home", bob.name))
    assert alice.candidates("home") == [bob.name, alice.name]  # the lowest claim holds
    assert alice.candidates("home")[0] == bob.name
    # the holder stops beating: fresh for 90 s by the server's clock, then the next claim
    t = alice.presence_snapshot()[bob.name][0]
    assert alice.candidates("home", now=t + 89)[0] == bob.name
    assert alice.candidates("home", now=t + 90) in ([alice.name], [])
    await bob.stop()  # a bye: gone at once
    rig.buses.remove(bob)
    await until(lambda: alice.presence_snapshot()[rig.names["bob"]][1] == "bye")
    assert alice.candidates("home") == [alice.name]


async def test_a_refused_presence_subject_is_said_in_words(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.37: a host and a login service of different releases: the presence subject is
    refused, and the host's log says what to do, not only an exception's type."""
    caplog.set_level(logging.WARNING)
    await rig.agent("carol")  # an older login service's credential
    assert "update the login service and the hosts together" in caplog.text
    caplog.clear()
    await rig.agent("alice")
    assert "update the login service" not in caplog.text


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
    # bob was first: no since, but a new session says it has not read the presence stream yet
    assert hellos[0] == {"token": "tok-1", "home": "home", "since": {"@presence": 1}}
    await alice.post("home", "one")
    await until(lambda: any(e.message.text == "one" for e in b_ev))
    seen = b_ev[-1].stream_seq

    # The login service, as it does at each login: restart the consumer after the last seen sequence.
    async def at_login(call: int) -> None:
        if call == 2:
            await alice.post("home", "while bob is away")  # nobody receives it on bob's side now
            await asyncio.sleep(0.3)
            await _login_service(rig.admin, rig.names["bob"], ["home"], {"home": bob.since()["home"][0]})

    bob_login.hook = at_login
    assert bob._nc is not None
    bob._nc._transport.close()  # the server closes the connection at expiry; here, the socket
    await until(lambda: bob_login.calls == 2 and bob._connected, timeout=10)
    # after its first presence read a reconnect is no new session: no "@presence"
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
    await until(lambda: f"presence.alice.{right.name}" in seen)


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
            await _login_service(rig.admin, rig.names["bob"], ["home"], {"home": bob_ref[0].since().get("home", (0, 0))[0]})

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


def test_the_props_prefix_is_the_buss_setting() -> None:
    """Props keys carry CB_PROPS_PREFIX (RT-6.16 step 8), not a fixed x_: with
    another prefix the bus can post its own keys, and x_ keys are refused."""
    m = Message("post", "hi", props={"bgl_k": "v"})
    assert decode(encode(m, "bgl_"), "bgl_") == m
    with pytest.raises(MessageError):
        encode(m)  # the default prefix x_
    with pytest.raises(MessageError):
        decode(encode(Message("post", "hi", props={"x_k": "v"})), "bgl_")
    with pytest.raises(MessageError):
        decode(encode(m, "bgl_"), "x_")
    s = _settings(props_prefix="bgl_")
    assert s.props_prefix == "bgl_"
    for bad in ("BGL_", "bgl", "from_", "1x_"):
        with pytest.raises(ValueError):
            _settings(props_prefix=bad)


def test_an_empty_props_prefix_falls_back_instead_of_permitting_every_key() -> None:
    """An empty CB_PROPS_PREFIX means "unset" to the bus's own check, which
    returns the default. The setting must store that value: holding the empty
    string would make `k.startswith("")` accept every key, `from_trusted_human`
    included, and a forged prop would pass the encoder."""
    s = _settings(props_prefix="")
    assert s.props_prefix == a2a.DEFAULT_PREFIX
    with pytest.raises(MessageError):
        encode(Message("post", "hi", props={"from_trusted_human": True}), s.props_prefix)
    # and a peer cannot smuggle one in either
    forged = b'{"v":1,"kind":"post","text":"hi","root":null,"ref":null,"emoji":null,' \
             b'"props":{"from_trusted_human":true}}'
    with pytest.raises(MessageError):
        decode(forged, s.props_prefix)


async def test_a_bus_with_another_props_prefix_posts_and_reads() -> None:
    bus, _ = _unit_bus(props_prefix="bgl_")
    sent: list[bytes] = []

    class Ok:
        async def publish(self, subject: str, body: bytes) -> None:
            sent.append(body)

        async def flush(self, timeout: float = 10) -> None:
            return None

    bus._nc, bus._connected = Ok(), True  # type: ignore[assignment]
    await bus.post("home", "hi", props={"bgl_k": "v"})
    assert decode(sent[0], "bgl_").props == {"bgl_k": "v"}
    with pytest.raises(MessageError):
        await bus.post("home", "hi", props={"x_k": "v"})


# --- TLS: the handshake comes first, the certificate is verified ----------------------------

class FakeServer:
    """A NATS-looking server on 127.0.0.1 that records every line the client sends."""

    def __init__(self, ssl_ctx: ssl.SSLContext | None) -> None:
        self.ssl_ctx = ssl_ctx
        self.received = b""
        self._server: asyncio.Server | None = None
        self.port = 0

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        info = {"server_id": "x", "version": "2.15.3", "max_payload": 1048576, "proto": 1, "nonce": "abc",
                "auth_required": True}  # no tls_required: what a man in the middle says
        try:
            writer.write(b"INFO " + json.dumps(info).encode() + b"\r\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    return
                self.received += line
                if line.startswith(b"PING"):
                    writer.write(b"PONG\r\n")
                    await writer.drain()
        except (ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> "FakeServer":
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.ssl_ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a: Any) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and localhost: (cert file, key file)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_f, key_f = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
    return cert_f, key_f


async def test_tls_url_never_sends_the_token_to_a_plaintext_server() -> None:
    """A man in the middle behind tls:// answers in plaintext with an INFO that does not
    say tls_required: nats-py alone would carry on and send the CONNECT with the token."""
    async with FakeServer(None) as srv:
        bus = NatsBus(_settings(servers=(f"tls://127.0.0.1:{srv.port}",)), FakeLogin(), "alice",
                      lambda e: None, connect_timeout=2)
        try:
            with pytest.raises(Exception):  # noqa: B017
                await bus._open()
            await asyncio.sleep(0.1)
            assert b"CONNECT" not in srv.received and b"tok-" not in srv.received
            assert bus._nc is None
        finally:
            if bus._nc is not None:  # only when the connection wrongly succeeded
                await bus._nc.close()


async def test_a_server_that_does_not_speak_tls_first_is_named_in_the_log(
        caplog: pytest.LogCaptureFixture) -> None:
    """The NATS server needs handshake_first: without it the client's TLS handshake meets the
    server's INFO in clear, and the log says so instead of a bare SSLError (RT-6.36)."""
    caplog.set_level(logging.WARNING)
    async with FakeServer(None) as srv:
        bus = NatsBus(_settings(servers=(f"tls://127.0.0.1:{srv.port}",)), FakeLogin(), "alice",
                      lambda e: None, connect_timeout=2)
        with pytest.raises(ssl.SSLError) as err:
            await bus._open()
        assert natsbus.connect_reason(err.value) == (
            "TLS: the server does not speak TLS first: set handshake_first in its tls block, "
            "or this is not a NATS TLS port")
        await bus.start()  # the first connect fails, the supervisor retries
        try:
            assert "first connect failed, retrying: TLS: the server does not speak TLS first" in caplog.text
        finally:
            await bus.stop()


def test_connect_reason_keeps_other_errors_to_their_type() -> None:
    assert natsbus.connect_reason(ConnectionRefusedError("secret text")) == "ConnectionRefusedError"
    try:
        try:
            raise ssl.SSLError(1, "[SSL: SOMETHING_ELSE] other")
        except ssl.SSLError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert natsbus.connect_reason(outer) == "TLS: SSLError"


def _cert_error(code: int | None, message: str | None = None) -> ssl.SSLCertVerificationError:
    e = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    if code is not None:
        e.verify_code, e.verify_message = code, message or ""
    return e


@pytest.mark.parametrize("code", [62, 64])  # X509_V_ERR_HOSTNAME_MISMATCH, _IP_ADDRESS_MISMATCH
def test_a_certificate_for_another_name_gets_no_ca_hint(code: int) -> None:
    why = natsbus.connect_reason(_cert_error(code, "mismatch"))
    assert why == "TLS: the certificate does not name this host (mismatch)"


@pytest.mark.parametrize("code", [2, 18, 19, 20, 21])
def test_an_unknown_issuer_gets_the_ca_hint(code: int) -> None:
    assert "set CB_NATS_CA_FILE" in natsbus.connect_reason(_cert_error(code, "unknown issuer"))


def test_a_certificate_error_without_its_verify_fields_does_not_raise() -> None:
    """One raised by hand has no verify_code: the retry loops log it, they must not crash on it."""
    assert natsbus.connect_reason(_cert_error(None)) == "TLS: the server's certificate is refused (no reason given)"


async def test_tls_url_refuses_a_certificate_it_does_not_trust(tmp_path: Path) -> None:
    cert, key = _self_signed(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    async with FakeServer(ctx) as srv:  # no tls_ca_file: the system's CAs do not know this one
        bus = NatsBus(_settings(servers=(f"tls://127.0.0.1:{srv.port}",)), FakeLogin(), "alice",
                      lambda e: None, connect_timeout=2)
        with pytest.raises(Exception) as err:  # noqa: B017
            await bus._open()
        await asyncio.sleep(0.1)
        assert srv.received == b""
        assert natsbus.connect_reason(err.value).startswith("TLS: the server's certificate is not trusted")


async def test_tls_url_with_the_ca_file_handshakes_first_and_connects(tmp_path: Path) -> None:
    cert, key = _self_signed(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    async with FakeServer(ctx) as srv:  # its INFO does not say tls_required: the handshake is first anyway
        s = _settings(servers=(f"tls://127.0.0.1:{srv.port}",), tls_ca_file=str(cert))
        bus = NatsBus(s, FakeLogin(), "alice", lambda e: None, connect_timeout=2)
        await bus._open()
        nc = bus._nc
        try:
            assert nc is not None and bus._connected
            assert b"CONNECT" in srv.received and srv.received.count(b"SUB deliver.") >= 3
        finally:
            if bus._renew_task:
                bus._renew_task.cancel()
            if nc is not None:
                await nc.close()


async def test_tls_url_refuses_a_trusted_certificate_for_another_name(tmp_path: Path) -> None:
    """A certificate the bus trusts as a CA, but that names another host: the
    host name is checked too (review of #15), so the connection is refused and
    no CONNECT with the token leaves."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "elsewhere.example")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("elsewhere.example")]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "other.pem", tmp_path / "other.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    async with FakeServer(ctx) as srv:
        # trusted as a CA, but it names elsewhere.example while we dial 127.0.0.1
        s = _settings(servers=(f"tls://127.0.0.1:{srv.port}",), tls_ca_file=str(cert_file))
        bus = NatsBus(s, FakeLogin(), "alice", lambda e: None, connect_timeout=2)
        with pytest.raises(Exception) as err:  # noqa: B017
            await bus._open()
        await asyncio.sleep(0.1)
        assert b"CONNECT" not in srv.received and b"tok-" not in srv.received
        # A certificate for another name may be a man in the middle: no hint to trust its CA.
        why = natsbus.connect_reason(err.value)
        assert why.startswith("TLS: the certificate does not name this host") and "CB_NATS_CA_FILE" not in why


def test_tls_and_plain_servers_are_not_mixed() -> None:
    with pytest.raises(ValueError):
        NatsSettings(servers=("tls://nats.example.org", "nats://127.0.0.1:4222"), home="home")
    assert _settings().tls_context() is None
    assert _settings(servers=("tls://nats.example.org",)).tls_context() is not None


# --- a planned renewal waits for a send under way ---------------------------------------------

async def test_a_planned_renewal_does_not_cut_off_a_running_send(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _ = _unit_bus(send_wait_sec=3)
    in_sync, release = asyncio.Event(), asyncio.Event()
    log_: list[str] = []

    class Old:
        async def publish(self, subject: str, body: bytes) -> None:
            log_.append("old publish")

        async def flush(self, timeout: float = 10) -> None:
            in_sync.set()
            await release.wait()

        async def close(self) -> None:
            log_.append("old close")

    class New:
        async def publish(self, subject: str, body: bytes) -> None:
            log_.append("new publish")

        async def flush(self, timeout: float = 10) -> None:
            return None

    async def fake_open() -> None:
        log_.append("open")
        bus._nc, bus._connected = New(), True  # type: ignore[assignment]

    monkeypatch.setattr(bus, "_open", fake_open)
    bus._nc, bus._connected = Old(), True  # type: ignore[assignment]
    first = asyncio.create_task(bus.post("home", "one"))
    await asyncio.wait_for(in_sync.wait(), 2)  # the send is under way, waiting for the server
    sup = asyncio.create_task(bus._supervise())
    bus._planned = True
    bus._wake.set()
    second = asyncio.create_task(bus.post("home", "two"))
    await asyncio.sleep(0.1)
    assert "old close" not in log_ and not first.done()  # the renewal waits for the send
    release.set()
    await asyncio.wait_for(first, 2)  # no FlushTimeoutError, no "publish failed"
    await asyncio.wait_for(second, 2)  # goes to the new connection: waits for it, not "not connected"
    assert log_ == ["old publish", "old close", "open", "new publish"]
    bus._stopping = True
    bus._wake.set()
    await asyncio.wait_for(sup, 2)


# --- a channel named dm is not the direct messages ---------------------------------------------

async def test_a_channel_named_dm_does_not_collide_with_direct_messages() -> None:
    bus, events = _unit_bus(home="dm")
    ok = encode(Message("post", "x"))
    await bus._on_deliver("dm", "bus.ch.dm.agent-x", _reply(bus, "dm", 10, 1), ok)  # the channel
    await bus._on_deliver("@dm", f"bus.dm.{bus.name}.agent-x", _reply(bus, "@dm", 3, 1), ok)  # a DM
    assert [(e.where, e.stream_seq) for e in events] == [("dm", 10), ("@dm", 3)]
    assert _seqs(bus.since()) == {"dm": 10, "@dm": 3}
    # a gap in the channel's consumer does not mark the DMs stale, and the other way round
    await bus._on_deliver("dm", "bus.ch.dm.agent-x", _reply(bus, "dm", 15, 4), ok)
    assert bus._stale == {"dm"}
    await bus._on_deliver("@dm", f"bus.dm.{bus.name}.agent-x", _reply(bus, "@dm", 4, 2), ok)
    assert len(events) == 3 and events[-1].where == "@dm" and _seqs(bus.since()) == {"dm": 10, "@dm": 4}
    bus._stale.clear()
    await bus._on_deliver("@dm", f"bus.dm.{bus.name}.agent-x", _reply(bus, "@dm", 9, 7), ok)
    assert bus._stale == {"@dm"}
    await bus._on_deliver("dm", "bus.ch.dm.agent-x", _reply(bus, "dm", 11, 5), ok)
    assert events[-1].where == "dm" and events[-1].stream_seq == 11
    if bus._gap_task:
        bus._gap_task.cancel()


# --- #213: the hand-over cursor across a kill ------------------------------------------------

# The server time of a delivered message in ms, as the read mark and `_last_time` hold it.
AT = int(T0 * 1000)


async def test_a_since_from_the_read_mark_is_sent_at_the_first_connect() -> None:
    """RT-6.35 step 3, #213: the cursor from the read mark's NATS lane comes back in the first
    CONNECT beside the presence read; an invalid key or pair, `@presence` and the people's
    `hu.<channel>` do not."""
    bus, _ = _unit_bus(since={
        "home": (12, AT), "@dm": (7, AT), "@presence": (5, AT), "hu.home": (9, AT),
        "Bad Key": (3, AT), "home2": 0, "home3": (13, 0), "home4": (13, -1),
        "home5": (1 << 63, AT), "home6": (9, AT, 1)})
    assert bus.since() == {"home": (12, AT), "@dm": (7, AT)}
    hello = json.loads(build_hello("T", "home", (), False, bus.since(), fresh_presence=True))
    assert hello["since"] == {"@presence": 1, "home": 12, "@dm": 7}
    await _presence(bus, "agent-a", 1, T0)  # a presence delivery keeps no `since` cursor
    assert bus.since() == {"home": (12, AT), "@dm": (7, AT)}


async def test_the_cursor_moves_only_after_the_hand_over() -> None:
    """#213: a handed message moves the cursor. One still in the callback, or whose callback
    failed, does not: a rebuild from the same cursor is handed it again, once."""
    held = asyncio.Event()
    got: list[int] = []

    async def hold(event: Event) -> None:
        got.append(event.stream_seq)
        await held.wait()

    bus = NatsBus(_settings(), FakeLogin(), "alice", hold, since={"home": (12, AT), "@dm": (7, AT)})
    ok = encode(Message("post", "x"))
    task = asyncio.create_task(bus._on_deliver(
        "home", "bus.ch.home.agent-x", _reply(bus, "home", 13, 2, T0 + 5), ok))
    await until(lambda: got == [13])
    assert _seqs(bus.since()) == {"home": 12, "@dm": 7}  # in flight: the cursor is still before it
    task.cancel()  # the kill: no cursor, no cleanup
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _seqs(bus.since()) == {"home": 12, "@dm": 7}
    # A failed callback is no hand-over either: the cursor stays, so the next one is asked for.
    async def boom(event: Event) -> None:
        raise RuntimeError("no")

    bad = NatsBus(_settings(), FakeLogin(), "alice", boom, since={"home": (12, AT)})
    await bad._on_deliver("home", "bus.ch.home.agent-x", _reply(bad, "home", 13, 2, T0 + 5), ok)
    assert _seqs(bad.since()) == {"home": 12}
    # The next process reads the same cursor (the read mark) and is handed the message again.
    again: list[Event] = []
    bus2 = NatsBus(_settings(), FakeLogin(), "alice", again.append,
                   since={"home": (12, AT), "@dm": (7, AT)})
    await bus2._on_deliver("home", "bus.ch.home.agent-x", _reply(bus2, "home", 13, 1, T0 + 5), ok)
    assert [e.stream_seq for e in again] == [13]
    assert bus2.since() == {"home": (13, AT + 5000), "@dm": (7, AT)}
    await bus2._on_deliver("home", "bus.ch.home.agent-x", _reply(bus2, "home", 14, 2, T0 + 6), ok)
    assert bus2.since()["home"] == (14, AT + 6000)


async def test_a_recreated_bus_stream_moves_the_cursor_down() -> None:
    """#213: a BUS stream made anew restarts its sequences. A first delivery (consumer number
    1) at or below the cursor with a NEWER server time is the new stream: the cursor resets and
    the message is read. With an OLDER time it is the login service's start by time replaying
    older messages: no reset, the message is a repeat."""
    ok = encode(Message("post", "x"))
    bus, events = _unit_bus(since={"home": (500, AT)})
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 1, 1, T0 + 1), ok)
    assert [e.stream_seq for e in events] == [1]
    assert bus.since() == {"home": (1, AT + 1000)}
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 2, 2, T0 + 2), ok)
    assert [e.stream_seq for e in events] == [1, 2]
    # A first delivery beyond the cursor is no reset either: the stream grew, it was not made anew.
    await bus._on_deliver("home", "bus.ch.home.agent-x", _reply(bus, "home", 501, 1, T0 + 3), ok)
    assert [e.stream_seq for e in events] == [1, 2, 501]
    # The login service started the consumer by time: an older message, consumer number 1.
    older, older_events = _unit_bus(since={"home": (500, AT)})
    await older._on_deliver("home", "bus.ch.home.agent-x", _reply(older, "home", 400, 1, T0 - 100), ok)
    assert older_events == [] and older.since() == {"home": (500, AT)}  # a repeat, no reset
    # its newer messages are read normally
    await older._on_deliver("home", "bus.ch.home.agent-x", _reply(older, "home", 501, 2, T0 + 1), ok)
    assert [e.stream_seq for e in older_events] == [501]


async def test_a_killed_server_asks_for_the_hand_over_cursor_again(rig: Rig,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """#213, RT-6.35 step 3: the first CONNECT's `since` is the read mark's hand-over cursor.
    A server killed while a post waits in its callback left the cursor before that post, so
    the next one is handed it again - once, never lost."""
    hellos: list[dict[str, Any]] = []
    real = natsbus.build_hello

    def spy(*a: Any, **k: Any) -> str:
        h = real(*a, **k)
        hellos.append(json.loads(h))
        return h

    monkeypatch.setattr(natsbus, "build_hello", spy)
    alice, _ = await rig.agent("alice")
    block = asyncio.Event()
    blocked: list[asyncio.Task[None]] = []
    got: list[str] = []

    async def hold(event: Event) -> None:
        got.append(event.message.text)
        if event.message.text == "inflight":
            if asyncio.current_task() is not None:
                blocked.append(asyncio.current_task())  # type: ignore[arg-type]
            await block.wait()  # the kill: never returns

    bob1 = NatsBus(NatsSettings(servers=(rig.url,), home="home"), rig.logins["bob"], "bob", hold,
                   heartbeat_sec=0.2)
    await _login_service(rig.admin, bob1.name, ["home"], {})
    await bob1.start()
    await alice.post("home", "one")
    await until(lambda: got == ["one"])
    cursor = bob1.since()["home"]  # the read mark would name this post: handed, then read
    assert cursor[0] == 1
    await alice.post("home", "inflight")  # the callback blocks: queued, not handed over
    await until(lambda: bool(blocked))
    # SIGKILL: no stop(), no bye, no read mark for "inflight": its tasks end, the socket closes.
    for task in bob1._tasks:
        task.cancel()
    if bob1._renew_task is not None:
        bob1._renew_task.cancel()
    await asyncio.gather(*bob1._tasks, return_exceptions=True)
    blocked[0].cancel()  # the post's own delivery: the callback never returns
    await asyncio.wait_for(blocked[0], 2)  # nats-py's subscription task ends when it is cancelled
    assert bob1.since()["home"] == cursor  # not handed: the cursor is still before it
    assert bob1._nc is not None
    bob1._nc._transport.close()
    await asyncio.sleep(0.3)

    events: list[Event] = []
    bob2 = NatsBus(NatsSettings(servers=(rig.url,), home="home"), rig.logins["bob"], "bob",
                   events.append, heartbeat_sec=0.2, since={"home": cursor})
    await _login_service(rig.admin, bob2.name, ["home"], bob2.since())
    await bob2.start()
    try:
        # The first CONNECT names both the fresh presence read and the hand-over cursor (#213).
        assert hellos[-1]["since"] == {"@presence": 1, "home": cursor[0]}
        await until(lambda: [e.message.text for e in events if e.where == "home"] == ["inflight"])
        await alice.post("home", "after")
        await until(lambda: [e.message.text for e in events if e.where == "home"]
                    == ["inflight", "after"])
        await asyncio.sleep(0.3)
        assert [e.message.text for e in events if e.where == "home"] == ["inflight", "after"]
    finally:
        await bob2.stop()


async def test_dm_reconnect_with_since_gets_exactly_the_dms_after_it(rig: Rig) -> None:
    alice, _ = await rig.agent("alice")
    bob_login = rig.logins["bob"]
    bob_ref: list[NatsBus] = []

    async def at_login(call: int) -> None:
        if call == 2:
            await alice.dm(rig.names["bob"], "dm3")  # while bob is away: nobody reads it now
            await asyncio.sleep(0.3)
            # Bob was away long enough for JetStream to drop his idle consumer: only `since`
            # under "@dm" brings dm3 back (a fixture that ignored it would start with new only).
            await _api(rig.admin, f"$JS.API.CONSUMER.DELETE.BUS.{rig.names['bob']}_dm")
            await _login_service(rig.admin, rig.names["bob"], ["home"], bob_ref[0].since())
            await alice.dm(rig.names["bob"], "dm4")

    bob_login.hook = at_login
    bob, b_ev = await rig.agent("bob")
    bob_ref.append(bob)
    await alice.dm(rig.names["bob"], "dm1")
    await alice.dm(rig.names["bob"], "dm2")
    await until(lambda: len([e for e in b_ev if e.where == "@dm"]) == 2)
    assert "@dm" in bob.since()
    assert bob._nc is not None
    bob._nc._transport.close()
    await until(lambda: bob_login.calls == 2 and bob._connected, timeout=10)
    await alice.dm(rig.names["bob"], "dm5")
    await until(lambda: any(e.message.text == "dm5" for e in b_ev))
    await asyncio.sleep(0.3)
    assert [e.message.text for e in b_ev if e.where == "@dm"] == ["dm1", "dm2", "dm3", "dm4", "dm5"]


# --- RT-6.37: membership apart from the crown, the total order, the fresh read -----------------

def test_hello_asks_for_a_fresh_presence_read_only_before_the_first_one() -> None:
    assert json.loads(build_hello("T", "home", (), False, {}, fresh_presence=True)) == {
        "token": "T", "home": "home", "since": {"@presence": 1}}
    assert "since" not in json.loads(build_hello("T", "home", (), False, {}, fresh_presence=False))
    bus, _ = _unit_bus()
    assert not bus._presence_read  # a bus that has read nothing: its hello says so
    asyncio.run(_presence(bus, "agent-a", 1, T0, pending=3))
    assert not bus._presence_read  # still reading the stored ones
    asyncio.run(_presence(bus, "agent-a", 2, T0 + 1))
    assert bus._presence_read and bus.synced


async def _member(bus: NatsBus, who: str, ch: str, seq: int, t: float, owner: str = "-") -> None:
    del owner
    await bus._on_presence_msg(FakeMsg(f"presence.member.{ch}.{who}", _presence_reply(bus, seq, seq, t),
                                       encode_claim()))


async def test_membership_is_apart_from_the_crown_claim() -> None:
    bus, _ = _unit_bus()
    await _member(bus, "agent-m", "home", 4, T0)
    assert bus.is_member("home", "agent-m") and not bus.is_member("other", "agent-m")
    assert not bus.claimed("home", "agent-m") and bus.holder("home") is None  # no crown from it
    await _claim(bus, "agent-c", "home", 5, T0)
    assert bus.claimed("home", "agent-c") and not bus.is_member("home", "agent-c")


@pytest.mark.parametrize("owner", ["crown", "member"])
async def test_an_owner_named_crown_or_member_gives_a_presence_by_token_count(owner: str) -> None:
    bus, _ = _unit_bus()
    await bus._on_presence_msg(FakeMsg(f"presence.{owner}.agent-p", _presence_reply(bus, 1, 1, T0),
                                       encode_presence("online")))
    assert bus.owner_of("agent-p") == owner and bus.presence_of("agent-p") == "online"
    assert not bus.is_member("agent-p", "agent-p") and bus._claims == {} and bus._members == {}
    await _member(bus, "agent-p", "home", 2, T0)
    await _claim(bus, "agent-q", "home", 3, T0)
    assert bus.is_member("home", "agent-p") and bus.claimed("home", "agent-q")
    assert set(bus.presence_snapshot()) == {"agent-p"}


async def test_a_membership_older_than_24_hours_is_gone_and_a_renewal_keeps_it() -> None:
    bus, _ = _unit_bus()
    await _member(bus, "agent-old", "home", 1, T0)
    await _member(bus, "agent-kept", "home", 2, T0)
    await _member(bus, "agent-kept", "home", 3, T0 + 6 * 3600)  # the renewal
    await _member(bus, "agent-kept", "home", 4, T0 + 12 * 3600)
    await _presence(bus, "agent-x", 5, T0 + natsbus.MAX_AGE_SEC + 10)
    assert not bus.is_member("home", "agent-old")
    assert bus.is_member("home", "agent-kept")
    await _presence(bus, "agent-x", 6, T0 + 12 * 3600 + natsbus.MAX_AGE_SEC + 100)
    assert bus._members == {}


async def test_presence_and_claims_are_ordered_by_server_time_then_sequence(
        monkeypatch: pytest.MonkeyPatch) -> None:
    bus, _ = _unit_bus()
    await _presence(bus, "agent-a", 1, T0 + 2000, status="bye")
    await _presence(bus, "agent-a", 100, T0 + 1990)  # older by the server's time, a higher sequence
    assert bus.presence_of("agent-a") == "offline" and bus.presence_snapshot()["agent-a"] == (T0 + 2000, "bye")
    await _presence(bus, "agent-a", 2, T0 + 2000, status="online")  # same time, higher sequence
    assert bus.presence_snapshot()["agent-a"] == (T0 + 2000, "online")
    await _claim(bus, "agent-a", "home", 1, T0 + 2000)
    await _claim(bus, "agent-a", "home", 100, T0 + 1990)
    assert bus._claims["home"]["agent-a"] == 1 and bus._claim_at[("home", "agent-a")] == T0 + 2000
    await _member(bus, "agent-a", "home", 1, T0 + 2000)
    await _member(bus, "agent-a", "home", 100, T0 + 1990)
    assert bus._members["home"]["agent-a"] == (1, T0 + 2000)
    await _member(bus, "agent-a", "home", 3, T0 + 2001)
    assert bus._members["home"]["agent-a"] == (3, T0 + 2001)


async def test_member_needs_a_channel_this_bus_asked_for() -> None:
    bus, _ = _unit_bus()
    with pytest.raises(natsbus.NatsBusError):
        await bus.member("elsewhere")
    with pytest.raises(ValueError):
        await bus.member("Bad Name")


async def _stored(rig: Rig, subject: str) -> tuple[int, float] | None:
    r = await rig.admin.request("$JS.API.STREAM.MSG.GET.PRESENCE", json.dumps({"last_by_subj": subject}).encode(),
                                timeout=5)
    out = json.loads(r.data)
    return (int(out["message"]["seq"]), 0.0) if "message" in out else None


async def test_the_membership_is_announced_once_synced_renewed_and_after_a_reconnect(rig: Rig) -> None:
    alice, _ = await rig.agent("alice", bus_kw={"member_renew_sec": 0.5})
    subject = f"presence.member.home.{alice.name}"
    assert await _stored(rig, subject) is None
    await alice.member("home")
    await until(lambda: alice.is_member("home", alice.name))  # read back through its own consumer
    assert alice.synced and not alice.claimed("home", alice.name)  # no crown from a membership
    first = await _stored(rig, subject)
    assert first is not None
    await until(lambda: alice._members["home"][alice.name][0] > first[0])  # renewed on the timer
    # after a reconnect: with a timer too long to matter, the reconnect alone announces again
    alice._member_renew_sec = 3600.0
    await asyncio.sleep(0.7)
    before = alice._members["home"][alice.name][0]
    assert alice._nc is not None
    alice._nc._transport.close()
    await until(lambda: alice._connected and alice._members["home"][alice.name][0] > before, timeout=10)


# --- the agent card's data in the presence (RT-6.37, ADR-015) ---------------------------

def test_the_presence_carries_the_card_and_older_readers_keep_reading_it() -> None:
    """RT-6.37: harness, model, task, branch and open asks ride the presence; the status,
    bot and name readers (and every older one: they read keys, never reject unknown ones)
    read the body as before."""
    card = PresenceCard(harness="opencode", model="GLM 5.3", task="console card data",
                        branch="console-card-data", asks=2)
    body = encode_presence("online", BOT, "agent-mac-ai-agent-716d72", card)
    assert json.loads(body) == {"v": 1, "status": "online", "bot": BOT,
                                "name": "agent-mac-ai-agent-716d72", "harness": "opencode",
                                "model": "GLM 5.3", "task": "console card data",
                                "branch": "console-card-data", "asks": 2}
    assert decode_presence_card(body) == card
    assert decode_presence(body) == "online" and decode_presence_bot(body) == BOT
    assert decode_presence_name(body) == "agent-mac-ai-agent-716d72"
    # a bye states it too: the console's last card stays filled
    assert decode_presence_card(encode_presence("bye", BOT, card=card)) == card
    # an older server states none: every field unknown, never guessed
    assert decode_presence_card(encode_presence("online", BOT)) == PresenceCard()
    assert decode_presence_card(b"junk") == PresenceCard()
    # not announced yet: harness and asks, no model, task or branch
    early = json.loads(encode_presence("online", card=PresenceCard(harness="claude-code", asks=0)))
    assert early == {"v": 1, "status": "online", "harness": "claude-code", "asks": 0}


def test_the_card_fields_are_bounded_on_both_sides() -> None:
    """RT-6.37: control characters out, one line, model 64, task 120 characters, a branch
    only as a safe git ref name, the harness from a fixed set, asks a small non-negative
    int; a bad field is dropped and never drops the status."""
    assert card_text("a\x00b\x1b[31m‮c\nd\te f", 64) == "a b [31m c d e f"
    assert card_text("m" * 200, CARD_MODEL_MAX) == "m" * CARD_MODEL_MAX
    assert card_text(7, CARD_TASK_MAX) is None
    # invisible format characters, tag characters and lone surrogates (no UTF-8 carries one)
    assert card_text("a\xadb\u061cc\u180ed\ufff9e\U000e0041f\ud83dg", 64) == "a b c d e f g"
    raw = b'{"v":1,"status":"online","task":"x\\ud83d"}'
    assert decode_presence_card(raw).task == "x"
    encode_presence("online", card=decode_presence_card(raw))  # encodes, never raises
    for good in ("main", "feat/nats-bridge", "#118", "release/0.100.0", "a+b_c"):
        assert card_branch(good) == good
    for bad in ("a..b", "-x", "x/", "x.lock", "x.", "a b", "a//b", "a/.b", "ä", "b" * 101,
                "x\ny", "@{u}", "x~1", "x:y", None, 3):
        assert card_branch(bad) is None, bad
    assert card_branch(" ") == "" and card_branch(" main\n") == "main"
    assert [card_harness(h) for h in ("pi", "cursor", "vim-mcp", "", 1)] == ["pi", "cursor", "other", None, None]
    assert [card_asks(a) for a in (0, 3, 10**9, -1, True, 1.5, "2")] == [0, 3, CARD_ASKS_MAX, None, None, None, None]
    raw = json.dumps({"v": 1, "status": "away", "harness": "<script>", "model": "x" * 500,
                      "task": "line one\nline two\x07", "branch": "../../etc", "asks": -4}).encode()
    assert decode_presence(raw) == "away"
    assert decode_presence_card(raw) == PresenceCard(harness="other", model="x" * CARD_MODEL_MAX,
                                                    task="line one line two", branch=None, asks=None)
    # the encoder bounds as the decoder does: nothing unbounded leaves the host
    sent = json.loads(encode_presence("online", card=PresenceCard(
        harness="x", model="m" * 500, task="t\x00" * 500, branch="no good", asks=-1)))
    assert sent == {"v": 1, "status": "online", "harness": "other", "model": "m" * CARD_MODEL_MAX,
                    "task": " ".join(["t"] * 60)}


def test_the_largest_presence_body_stays_far_below_the_streams_limit() -> None:
    """RT-6.37: the PRESENCE stream's max_msg_size is 4096 bytes; the largest body the
    encoder can make, with characters JSON escapes longest (astral: 12 bytes each), stays
    well below it."""
    wide = "\U0001F600"  # two \\uXXXX escapes in ASCII JSON
    card = PresenceCard(harness="claude-code", model=wide * 500, task=wide * 500,
                        branch="b" * CARD_BRANCH_MAX, asks=10**6)
    body = encode_presence("online", BOT, "a" * 64, card)
    assert len(body) <= 2600, len(body)  # 2510 bytes today
    assert decode_presence_card(body).model == wide * CARD_MODEL_MAX


def test_the_bus_reads_the_card_at_each_presence() -> None:
    """RT-6.37: the card's data is read for every presence, so a change shows with the next
    heartbeat; a failing source sends a presence without it."""
    bus, _ = _unit_bus()
    bus.link_bot(BOT, "agent-mac-ai-agent-716d72")
    assert decode_presence_card(bus._presence_body("online")) == PresenceCard()
    state = {"asks": 0}
    bus.card_source = lambda: PresenceCard(harness="claude-code", task="t", asks=state["asks"])
    assert decode_presence_card(bus._presence_body("online")).asks == 0
    state["asks"] = 1
    assert decode_presence_card(bus._presence_body("bye")).asks == 1

    def broken() -> PresenceCard:
        raise RuntimeError("no")
    bus.card_source = broken
    body = bus._presence_body("online")
    assert decode_presence(body) == "online" and decode_presence_card(body) == PresenceCard()


def test_the_hello_carries_the_session_part_only_when_the_key_is_shared() -> None:
    """#163, RT-6.36: the part is a hello field; absent means today's name."""
    with_session = json.loads(build_hello("tok", "home", (), False, {}, session="abcdefghijklmnopqrstuvwxyz"))
    assert with_session["session"] == "abcdefghijklmnopqrstuvwxyz"
    without = json.loads(build_hello("tok", "home", (), False, {}))
    assert "session" not in without
# --- the login service's theft notice (RT-6.34) ---------------------------------

def encode_notice(**over: Any) -> bytes:
    rec: dict[str, Any] = {"v": 1, "kind": "session", "verdict": "copied",
                           "sid": "a1b2c3d4-0000-4000-8000-000000000000",
                           "network": "203.0.113.0/24", "agent": "agent-b",
                           "iat": int(T0), "exp": int(T0 + 3600)}
    rec.update(over)
    return json.dumps(rec).encode()


def test_a_notice_decodes_strictly() -> None:
    n = decode_notice(encode_notice())
    assert n.kind == "session" and n.network == "203.0.113.0/24" and n.agent == "agent-b"
    assert decode_notice(encode_notice(agent="")).agent == ""
    for bad in (encode_notice(v=2), encode_notice(kind="other"), encode_notice(verdict="other"),
                encode_notice(sid=""), encode_notice(sid="a b"),
                encode_notice(network="not a network"), encode_notice(agent="not an agent"),
                encode_notice(exp=int(T0))):
        with pytest.raises(MessageError):
            decode_notice(bad)


def test_a_dotted_owner_is_a_subject_token() -> None:
    # "alice.example" is written "alice~example"; the notice reader accepts it.
    assert natsbus.owner_of_token("alice~example") == "alice.example"
    assert natsbus.owner_of_token("-") == ""  # no owner proven


async def test_a_notice_reaches_the_callback_once_and_an_expired_one_never() -> None:
    seen: list[Notice] = []
    bus = NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None, on_notice=seen.append)
    await bus._on_presence_msg(FakeMsg("presence.notice.bob.copied.v1", _presence_reply(bus, 1, 1, T0),
                                       encode_notice()))
    # A replay of the same record (a consumer made anew) is not told twice.
    await bus._on_presence_msg(FakeMsg("presence.notice.bob.copied.v1", _presence_reply(bus, 2, 2, T0 + 60),
                                       encode_notice()))
    assert [n.kind for n in seen] == ["session"]
    # One that expired before it was delivered is dropped.
    await bus._on_presence_msg(FakeMsg("presence.notice.bob.copied.v1", _presence_reply(bus, 3, 3, T0 + 7200),
                                       encode_notice(sid="other-session")))
    assert len(seen) == 1


async def test_a_dotted_owner_reaches_the_caller() -> None:
    """RT-6.34: the subject token writes "." as "~", and the reader accepts it."""
    seen: list[Notice] = []
    bus = NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None, on_notice=seen.append)
    await bus._on_presence_msg(FakeMsg("presence.notice.alice~example.copied.v1",
                                       _presence_reply(bus, 1, 1, T0), encode_notice()))
    assert [n.verdict for n in seen] == ["copied"]


async def test_a_verdict_that_is_not_the_subject_s_is_dropped() -> None:
    seen: list[Notice] = []
    bus = NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None, on_notice=seen.append)
    await bus._on_presence_msg(FakeMsg("presence.notice.alice.moved.v1",
                                       _presence_reply(bus, 1, 1, T0), encode_notice(verdict="copied")))
    assert seen == []


def test_the_notice_text_names_what_only_the_owner_can_do() -> None:
    from mattermost_ai_agent_bus.login_notice import notice_text
    session = notice_text("agent-b", "session")
    assert "agent-b" in session and "Keycloak" in session and "refuses" in session
    host = notice_text("agent-b", "host_key")
    assert "host key" in host and "rotate" in host and "not refused" in host


async def test_one_server_tells_a_notice() -> None:
    """RT-6.34: the named agent's server tells it when that agent is live, else the live agent
    with the smallest name, this one included - the same answer on every server."""
    bus = NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None)
    await _presence(bus, bus.name, 1, T0, owner="alice")
    await _presence(bus, "agent-0aaa", 2, T0, owner="alice")
    await _presence(bus, "agent-zzz", 3, T0, owner="alice")
    assert bus.notice_teller("agent-zzz") == "agent-zzz"  # live: its own server
    assert bus.notice_teller("agent-missing") == "agent-0aaa"  # not live: the smallest live name
    await _presence(bus, bus.name, 4, T0 + 100, owner="alice")  # only this one stays live
    await _presence(bus, "agent-0aaa", 5, T0 + 100, status="bye", owner="alice")
    await _presence(bus, "agent-zzz", 6, T0 + 100, status="bye", owner="alice")
    assert bus.notice_teller("agent-0aaa") == bus.name
    assert bus.notice_teller("") == bus.name
    # Another owner's agent, even with the smallest name, is no teller.
    await _presence(bus, "agent-000", 7, T0 + 101, owner="bob")
    assert bus.notice_teller("agent-missing") == bus.name


def test_a_told_notice_survives_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.34: the consumer replays the last record per subject, so a restarted
    server needs to know what it already told - per kind and id, so one copy does
    not hide another."""
    from mattermost_ai_agent_bus import nats_port

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = int(time.time())
    assert nats_port._notice_told("bot-1", "session:s1") == 0.0
    nats_port._notice_mark("bot-1", "session:s1", now - 5)
    assert nats_port._notice_told("bot-1", "session:s1") == now - 5
    assert nats_port._notice_told("bot-1", "session:s2") == 0.0  # another id
    nats_port._notice_mark("bot-1", "host_key:UA-1", now - 4)
    assert nats_port._notice_told("bot-1", "session:s1") == now - 5  # kept, not replaced
    assert nats_port._notice_told("bot-2", "session:s1") == 0.0  # per bot
    # A future iat is capped at now: a skewed mark must not silence later records.
    nats_port._notice_mark("bot-1", "session:s3", time.time() + 10_000)
    assert nats_port._notice_told("bot-1", "session:s3") <= time.time() + 1


def test_the_same_copy_is_one_dm() -> None:
    """RT-6.34: (kind, id, verdict) is one DM within the block, and not after it."""
    from mattermost_ai_agent_bus import nats_port

    seen: dict[tuple[str, str, str], float] = {}
    key = ("session", "s1", "copied")
    assert not nats_port._notice_repeat(seen, key, 100.0)
    seen[key] = 100.0
    assert nats_port._notice_repeat(seen, key, 100.0 + 29 * 60)
    assert not nats_port._notice_repeat(seen, key, 100.0 + 30 * 60)
    assert not nats_port._notice_repeat(seen, ("session", "s2", "copied"), 101.0)


def test_the_told_marks_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-lived server must not grow its told marks without bound."""
    from mattermost_ai_agent_bus import nats_port

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    now = int(time.time())
    for i in range(nats_port.NOTICE_KEY_MAX + 10):
        nats_port._notice_mark("bot-1", f"session:s{i}", now - 1000 + i)
    marks = nats_port._notice_read("bot-1")
    assert len(marks) == nats_port.NOTICE_KEY_MAX
    assert "session:s0" not in marks  # the oldest evicted
    assert f"session:s{nats_port.NOTICE_KEY_MAX + 9}" in marks


def test_a_session_part_is_validated_by_the_bus() -> None:
    """#163, RT-6.36: a part that is not 26 lower-case base32 characters is refused at start."""
    with pytest.raises(ValueError):
        NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None, session="TOO-SHORT")
    NatsBus(_settings(), FakeLogin(), "alice", lambda _e: None, session="abcdefghijklmnopqrstuvwxyz")


def test_the_session_nonce_is_made_once_and_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#163: a resumed session and a restarted server keep the name."""
    from mattermost_ai_agent_bus import session_file

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    first = session_file.nonce()
    assert re.fullmatch(r"[a-z2-7]{26}", first)
    assert session_file.nonce() == first
    path = session_file.keyed_path("nonce")
    path.write_text("broken\n", encoding="utf-8")
    second = session_file.nonce()
    assert second != "broken" and re.fullmatch(r"[a-z2-7]{26}", second)


def test_the_key_is_shared_only_under_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """#163: only a shared host key needs the session part; the MCP component's
    CB_ name with the old one as the fallback (RT-6.44)."""
    from mattermost_ai_agent_bus import login
    from mattermost_ai_agent_bus.config import EnvConflict

    monkeypatch.delenv("CB_LOGIN_NKEY", raising=False)
    monkeypatch.delenv("MM_LOGIN_NKEY", raising=False)
    assert not login.shared_key()
    monkeypatch.setenv("MM_LOGIN_NKEY", "host")  # the old name, still read
    assert login.shared_key()
    monkeypatch.setenv("CB_LOGIN_NKEY", "host")  # the same value under both: fine
    assert login.shared_key()
    monkeypatch.setenv("CB_LOGIN_NKEY", "ephemeral")  # different: the start is refused
    with pytest.raises(EnvConflict):
        login.shared_key()
    monkeypatch.delenv("MM_LOGIN_NKEY")
    assert not login.shared_key()  # the new name alone


def test_a_late_registered_server_adopts_its_earlier_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#163, RT-6.36: a server that registered after its first start takes the part
    its earlier start kept, so its name does not change once more."""
    from mattermost_ai_agent_bus import session_file

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    uid = "u" * 26
    earlier = "abcdefghijklmnopqrstuvwxyz"
    keyed = session_file.keyed_path("nonce")
    keyed.parent.mkdir(parents=True, exist_ok=True)
    keyed.write_text(f"CB_SESSION_NONCE={earlier}\n", encoding="utf-8")
    monkeypatch.setattr(session_file, "read", lambda: {"CB_MM_BOT_USER_ID": uid})
    assert session_file.nonce() == earlier
    per_bot = session_file.state_dir() / f"nonce-{uid}.env"
    assert per_bot.read_text(encoding="utf-8").strip() == f"CB_SESSION_NONCE={earlier}"
    assert not keyed.exists()  # claimed: the next bot finds nothing
    # A second bot of the same checkout gets its own part, not the first's.
    other = "v" * 26
    monkeypatch.setattr(session_file, "read", lambda: {"CB_MM_BOT_USER_ID": other})
    second = session_file.nonce()
    assert second != earlier and re.fullmatch(r"[a-z2-7]{26}", second)


def test_a_racing_bot_never_adopts_the_same_part(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#163, RT-6.36: rename first, then read — a bot whose rename finds the keyed
    file gone makes its own part instead of copying the winner's."""
    from mattermost_ai_agent_bus import session_file

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    uid = "w" * 26
    keyed = session_file.keyed_path("nonce")
    keyed.parent.mkdir(parents=True, exist_ok=True)
    keyed.write_text("CB_SESSION_NONCE=abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
    monkeypatch.setattr(session_file, "read", lambda: {"CB_MM_BOT_USER_ID": uid})

    def lost(_a: str, _b: str) -> None:
        raise FileNotFoundError("another bot claimed it")

    monkeypatch.setattr(session_file.os, "replace", lost)
    value = session_file.nonce()
    assert value != "abcdefghijklmnopqrstuvwxyz" and re.fullmatch(r"[a-z2-7]{26}", value)
