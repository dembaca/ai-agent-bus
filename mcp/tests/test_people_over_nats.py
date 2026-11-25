"""People's posts onto NATS, the MCP server's side (RT-6.42, ADR-016 step 2): the strict
decoders of a person's body, an account record and the bridge's signal (natsbus), the
person's consumer `<agent>_hu_<channel>`, and the port's switch per channel, its keys
against repeats, trust by anchor and approval by a fresh approver record (nats_port).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from fakes import BOT, WORK, FakeBus, FakeRegistrar
from test_nats_port import ME, FakeNats, _settings, arrive, make
from test_natsbus import T0, FakeMsg, _presence_reply, _unit_bus

from mattermost_ai_agent_bus import asks, compose, server
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.model import Post, Reaction
from mattermost_ai_agent_bus.nats_port import NatsEvents, NatsPort, person_id
from mattermost_ai_agent_bus.natsbus import (
    UNBOUND,
    Attestation,
    Message,
    MessageError,
    PersonEvent,
    PersonMessage,
    decode_account,
    decode_bridge_signal,
    decode_person,
)
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

MM1 = "q1w2e3r4t5y6u7i8o9p0a1s2d3"
MM2 = "z9x8c7v6b5n4m3l2k1j0h9g8f7"
ASK = "a" * 26
ACCOUNT = "u" * 26
HUMAN = "h" * 26


def _body(**kw: Any) -> bytes:
    return json.dumps({"v": 1, **kw}).encode()


# --- the decoders (natsbus) ------------------------------------------------------------------

def test_the_protocols_bodies_decode() -> None:
    """RT-6.42 step 5: the three bodies of docs/protocol.md."""
    post = decode_person(_body(kind="post", text="go ahead", name="dembaca", mm=MM1, mm_root=MM2,
                               root=4711, files=0))
    assert (post.kind, post.mm, post.mm_root, post.root, post.name, post.text) == (
        "post", MM1, MM2, 4711, "dembaca", "go ahead")
    r = decode_person(_body(kind="reaction", emoji="+1", name="dembaca", mm=MM1, ref=4712))
    assert (r.kind, r.emoji, r.ref) == ("reaction", "+1", 4712)
    u = decode_person(_body(kind="unreaction", emoji="+1", name="dembaca", mm=MM1))
    assert (u.kind, u.ref) == ("unreaction", None)
    assert decode_person(_body(kind="post", text="", name="a", mm=MM1, cut=True)).cut


@pytest.mark.parametrize("body", [
    b"", b"   ", b"null", b"[]",
    _body(kind="post", text="x", name="dembaca", mm=MM1, props={"x_k": 1}),  # no props
    _body(kind="post", text="x", name="dembaca", mm=MM1, sender="boss"),  # no unknown key
    b'{"v":1,"kind":"post","text":"x","name":"dembaca","mm":"' + MM1.encode() + b'","text":"y"}',
    json.dumps({"v": 2, "kind": "post", "text": "x", "name": "dembaca", "mm": MM1}).encode(),
    _body(kind="edit", text="x", name="dembaca", mm=MM1),
    _body(kind="post", text="x", name="dembaca", mm="n1:home:4"),
    _body(kind="post", text="x", name="dembaca", mm=MM1, mm_root="short"),
    _body(kind="post", text="x", mm=MM1),  # no name
    _body(kind="post", text="x", name="Not A Name", mm=MM1),
    _body(kind="post", name="dembaca", mm=MM1),  # no text
    _body(kind="post", text="x" * 16001, name="dembaca", mm=MM1),
    _body(kind="post", text="x", name="dembaca", mm=MM1, root=0),
    _body(kind="post", text="x", name="dembaca", mm=MM1, files=-1),
    _body(kind="post", text="x", name="dembaca", mm=MM1, emoji="+1"),
    _body(kind="reaction", emoji="", name="dembaca", mm=MM1),
    _body(kind="reaction", emoji="+1", name="dembaca", mm=MM1, text="x"),
    _body(kind="reaction", emoji="+1", name="dembaca", mm=MM1, ref=True),
])
def test_a_persons_body_is_decoded_strictly(body: bytes) -> None:
    """RT-6.42 step 9: format v1, exactly the fields of its kind, no props, no duplicate key."""
    with pytest.raises(MessageError):
        decode_person(body)


def _account(**kw: Any) -> bytes:
    return _body(**({"account": ACCOUNT, "anchor": "dembaca", "name": "dembaca", "approver": True,
                     "token_iat": 1, "iat": 2, "exp": 3} | kw))


def test_an_account_record_and_the_bridges_signal_are_decoded_strictly() -> None:
    rec = decode_account(_account(), ACCOUNT)
    assert (rec.anchor, rec.approver, rec.exp) == ("dembaca", True, 3.0)
    assert decode_account(_account(anchor=None, name=None, approver=False), ACCOUNT).anchor is None
    for bad in (b"", _account(extra=1), _account(account=HUMAN), _account(anchor="A B"), _account(approver=1),
                _account(exp="3")):
        with pytest.raises(MessageError):
            decode_account(bad, ACCOUNT)
    sig = _body(home="home", people=True, iat=1, exp=9)
    assert decode_bridge_signal(sig, "home") == 9.0
    for bad in (b"", _body(home="home", people=False, iat=1, exp=9), _body(home="other", people=True, iat=1, exp=9),
                _body(home="home", people=True, iat=1, exp=9, extra=1)):
        with pytest.raises(MessageError):
            decode_bridge_signal(bad, "home")


# --- the bus: the consumer and the records -----------------------------------------------------------

def _hu_reply(name: str, ch: str, sseq: int, cseq: int, t: float = T0) -> str:
    return f"$JS.ACK.BUS.{name}_hu_{ch}.1.{sseq}.{cseq}.{int(t * 1e9)}.0"


async def test_the_persons_consumer_gives_the_author_from_the_subject(caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.42 steps 4, 8, 9: `<name>_hu_<channel>`; the sender is the subject's last token
    ("~" for "."), `-` an account bound to nobody; anything else is dropped without content."""
    caplog.set_level(logging.INFO)
    people: list[PersonEvent] = []
    bus, _ = _unit_bus()
    bus._on_person = people.append
    n = bus.name
    ok = _body(kind="post", text="SECRET-TEXT", name="dembaca", mm=MM1)
    await bus._on_person_deliver("home", "bus.hu.home.a~b", _hu_reply(n, "home", 10, 1), ok)
    await bus._on_person_deliver("home", "bus.hu.home.-", _hu_reply(n, "home", 11, 2), ok)
    await bus._on_person_deliver("home", "bus.hu.home.a~b", _hu_reply(n, "home", 11, 3), ok)  # a repeat
    await bus._on_person_deliver("home", "bus.hu.home.a~b", f"$JS.ACK.BUS.{n}_ch_home.1.12.4.1.0", ok)  # other consumer
    await bus._on_person_deliver("home", "bus.ch.home.a~b", _hu_reply(n, "home", 13, 4), ok)  # an agent's subject
    await bus._on_person_deliver("home", "bus.hu.other.a~b", _hu_reply(n, "home", 14, 5), ok)
    await bus._on_person_deliver("home", "bus.hu.home.A?", _hu_reply(n, "home", 15, 6), ok)
    await bus._on_person_deliver("home", "bus.hu.home.a~b", _hu_reply(n, "home", 16, 7),
                                 _body(kind="post", text="SECRET-TEXT", name="dembaca", mm=MM1, props={}))
    assert [(e.anchor, e.stream_seq, e.message.text) for e in people] == [
        ("a.b", 10, "SECRET-TEXT"), (UNBOUND, 11, "SECRET-TEXT")]
    assert "SECRET-TEXT" not in caplog.text
    assert bus.since() == {}  # people's sequences are not the channel's `since`


async def test_the_records_and_the_signal_count_by_the_servers_clock() -> None:
    """RT-6.42 steps 3, 7, 10: an account's anchor and approver flag, the bridge's signal;
    a message there that does not decode erases what was there (fail closed)."""
    bus, _ = _unit_bus()

    async def presence(subject: str, seq: int, t: float, data: bytes) -> None:
        await bus._on_presence_msg(FakeMsg(subject, _presence_reply(bus, seq, seq, t), data))

    acct = f"presence.account.mattermost.{ACCOUNT}.v1"
    assert bus.anchor_of_account(ACCOUNT) == UNBOUND and not bus.approver("dembaca")
    await presence(acct, 1, T0, _account(exp=int(T0) + 900))
    assert bus.anchor_of_account(ACCOUNT) == "dembaca" and bus.approver("dembaca")
    assert not bus.approver(UNBOUND)
    await presence("presence.bridge.home.v1", 2, T0 + 1, _body(home="home", people=True, iat=int(T0), exp=int(T0) + 180))
    assert bus.people_switched("home") and not bus.people_switched("other")
    await presence(f"presence.-.{bus.name}", 3, T0 + 901, b'{"v":1,"status":"online"}')  # the clock moves
    assert not bus.approver("dembaca") and bus.anchor_of_account(ACCOUNT) == "dembaca"  # stale: no approval
    assert not bus.people_switched("home")  # the signal ran out
    await presence(acct, 4, T0 + 902, _account(exp=int(T0) + 2000))
    assert bus.approver("dembaca")
    await presence(acct, 5, T0 + 903, b"")  # a stray answer erased it
    assert bus.anchor_of_account(ACCOUNT) == UNBOUND and not bus.approver("dembaca")
    await presence(acct, 4, T0 + 902, _account(exp=int(T0) + 2000))  # an older one never overrides
    assert bus.anchor_of_account(ACCOUNT) == UNBOUND


# --- the port: the switch, the keys, trust ------------------------------------------------------------

def _person(where: str, anchor: str, seq: int, message: PersonMessage, t: float | None = None) -> PersonEvent:
    return PersonEvent(where, anchor, seq, time.time() if t is None else t, message)


def _att() -> Attestation:
    return Attestation(owner_anchor="dembaca", owner_name="dembaca", trusted=(("dembaca", "dembaca"),),
                       cut=False, exp=1e12)


async def _events(port: NatsPort) -> NatsEvents:
    ev = NatsEvents(port)
    await ev.open()
    return ev


def _queued(ev: NatsEvents) -> list[Any]:
    q = ev._queue
    assert q is not None
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_a_persons_post_comes_over_nats_only_while_the_bridge_signals_its_channel() -> None:
    """RT-6.42 step 9: no fresh signal, the post is Mattermost's; with one, a PostEvent with the
    Mattermost id, its root (a NATS post in the memory, else mm_root), author hu:<anchor>."""
    port, _, fake = make()
    assert fake is not None
    ev = await _events(port)
    await port._on_person(_person("home", "dembaca", 20, PersonMessage("post", MM1, "dembaca", text="hi")))
    assert _queued(ev) == []
    fake.switched.add("home")
    await arrive(port, "home", "agent-alice", 30, Message("post", "a root"))
    _queued(ev)
    await port._on_person(_person("home", "dembaca", 31, PersonMessage("post", MM1, "dembaca", text="go",
                                                                       mm_root=MM2, root=30)))
    await port._on_person(_person("home", UNBOUND, 32, PersonMessage("post", MM2, "someone", text="hey",
                                                                     mm_root=ASK, root=9)))
    await port._on_person(_person("home", "dembaca", 33, PersonMessage("post", MM1, "dembaca", text="go")))  # repeat
    got = _queued(ev)
    assert [(e.post.id, e.post.root_id, e.post.user_id, e.post.message) for e in got] == [
        (MM1, "n1:home:30", person_id("dembaca"), "go"), (MM2, ASK, "hu:-", "hey")]
    assert all(isinstance(e, PostEvent) and e.post.channel_id == WORK and not e.post.props for e in got)
    assert await port.usernames({"hu:-", person_id("dembaca")}) == {"hu:-": "someone", "hu:dembaca": "dembaca"}
    assert await port.agents({"hu:-"}) == set() and await port.presences(["hu:-"]) == {"hu:-": "offline"}


async def test_mattermosts_people_are_dropped_while_switched_and_a_repeat_by_its_key() -> None:
    """RT-6.42 step 9: in a switched channel Mattermost's people go (bots, webhooks and system
    posts stay); at a switch both sides may deliver one event, and the second is dropped."""
    port, inner, fake = make(bridge_bot="")
    assert fake is not None
    inner.bots["helper"] = type("U", (), {"id": "b" * 26})()  # another bot
    person = Post(id=MM1, channel_id=WORK, user_id=HUMAN, message="hi")
    fake.switched.add("home")
    assert await port.skip(PostEvent(post=person, channel_type=None, edited=False))
    assert not await port.skip(PostEvent(post=Post(id=MM2, channel_id=WORK, user_id="b" * 26),
                                         channel_type=None, edited=False))
    assert not await port.skip(PostEvent(post=Post(id=MM2, channel_id=WORK, user_id=HUMAN, from_webhook=True),
                                         channel_type=None, edited=False))
    assert not await port.skip(PostEvent(post=Post(id=MM2, channel_id="elsewhere", user_id=HUMAN),
                                         channel_type=None, edited=False))
    assert await port.skip(ReactionEvent(Reaction(HUMAN, ASK, "+1", 1), WORK))
    # Not switched: kept, and the same post over NATS afterwards is a repeat.
    fake.switched.clear()
    assert not await port.skip(PostEvent(post=person, channel_type=None, edited=False))
    fake.accounts[HUMAN] = "dembaca"
    assert not await port.skip(ReactionEvent(Reaction(HUMAN, ASK, "+1", 1), WORK))
    ev = await _events(port)
    fake.switched.add("home")
    await port._on_person(_person("home", "dembaca", 40, PersonMessage("post", MM1, "dembaca", text="hi")))
    await port._on_person(_person("home", "dembaca", 41, PersonMessage("reaction", ASK, "dembaca", emoji="+1")))
    assert _queued(ev) == []
    # Taken back and given again: it counts again.
    await port._on_person(_person("home", "dembaca", 42, PersonMessage("unreaction", ASK, "dembaca", emoji="+1")))
    await port._on_person(_person("home", "dembaca", 43, PersonMessage("reaction", ASK, "dembaca", emoji="+1")))
    got = _queued(ev)
    assert [(type(e).__name__, e.reaction.post_id, e.reaction.user_id) for e in got] == [
        ("ReactionEvent", ASK, "hu:dembaca")]


async def test_when_the_signal_runs_out_mattermost_is_read_from_the_last_people_event() -> None:
    """RT-6.42 step 9: the catch-up delivers what came on Mattermost meanwhile, once."""
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    fake.switched.add("home")
    t = time.time()
    await port._on_person(_person("home", "dembaca", 50, PersonMessage("post", MM1, "dembaca", text="a"), t))
    _queued(ev)
    later = Post(id=MM2, channel_id=WORK, user_id=HUMAN, message="while the bridge was down",
                 create_at=int(t * 1000) + 5000, reactions=(Reaction(HUMAN, ASK, "+1", int(t * 1000) + 6000),))
    inner.posts = [Post(id=MM1, channel_id=WORK, user_id=HUMAN, message="a", create_at=int(t * 1000)), later]
    fake.switched.clear()
    await port.check_people()
    for task in list(port._catching_up):
        await task
    got = _queued(ev)
    assert [(type(e).__name__, getattr(e, "post", None) and e.post.id) for e in got] == [
        ("PostEvent", MM2), ("ReactionEvent", None)]
    assert got[0].post.user_id == HUMAN  # read on Mattermost: its own account


async def test_a_trusted_anchor_is_trusted_and_approves_only_with_a_fresh_approver_record(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 9 through the inbox: from_trusted_human by the session's attested anchors
    (`-` never), not_approver without a fresh approver record; a yes on an open ask counts
    only with one."""
    inner = FakeBus(FakeRegistrar(), trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"})
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, _settings(), ("home",), echo_wait=0.5)  # type: ignore[arg-type]
    fake = FakeNats(port)
    port.use_bus(fake)
    fake.attestations[ME] = _att()
    fake.switched.add("home")
    assert (await port.trusted_humans())["hu:dembaca"] == "dembaca" and "hu:-" not in await port.trusted_humans()
    client = compose.Remembered(port)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "_get_client", lambda: client)
    inbox = WebSocketInbox(client=client, watched_channel_ids={WORK}, source=NatsEvents(port))  # type: ignore[arg-type]
    asks.add(BOT, ASK, "dembaca", ASK, title="merge?", question=False)
    try:
        await inbox.ensure_connected()
        await port._on_person(_person("home", "dembaca", 60, PersonMessage("post", MM1, "dembaca", text="go")))
        await port._on_person(_person("home", UNBOUND, 61, PersonMessage("post", MM2, "dembaca", text="go")))
        await port._on_person(_person("home", "dembaca", 62, PersonMessage("reaction", ASK, "dembaca", emoji="+1")))
        events = await inbox.take(timeout_sec=2, max_events=10)
        items = {e.post.user_id + ":" + e.kind: (e, i) for e, i in zip(events, await server._event_items(events),
                                                                     strict=True)}
        post_e, post_i = items["hu:dembaca:watched_channel"]
        assert post_i["from_trusted_human"] is True and post_i["not_approver"] is True
        assert items["hu:-:watched_channel"][1]["from_trusted_human"] is False
        react_e, react_i = items["hu:dembaca:reaction"]
        assert react_i["from_trusted_human"] and react_i["answer"] == "yes"
        assert not server._ask_answer(react_e, react_i)
        fake.approvers.add("dembaca")
        again = await server._event_items([post_e, react_e])
        assert "not_approver" not in again[0] and server._ask_answer(react_e, again[1])
    finally:
        await inbox.close()
