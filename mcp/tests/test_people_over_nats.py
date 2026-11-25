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

from mattermost_ai_agent_bus import asks, channel, compose, server
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.model import FileInfo, Post, Reaction
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
    bus._connected, bus._nc = True, object()  # type: ignore[assignment]  # a live connection

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
    await presence(acct, 5, T0 + 903, b"")  # a stray answer erased it: no approver
    # the keys still know the anchor last read (Fable finding 8), the trust does not
    assert bus.anchor_of_account(ACCOUNT) == "dembaca" and not bus.approver("dembaca")
    await presence(acct, 4, T0 + 902, _account(exp=int(T0) + 2000))  # an older one never overrides
    assert not bus.approver("dembaca")
    await presence(acct, 6, T0 + 904, _account(anchor=None, name=None))  # unbound by the service
    assert bus.anchor_of_account(ACCOUNT) == UNBOUND


async def test_a_signal_counts_only_while_connected_and_never_past_its_cap() -> None:
    """Fable findings 4 and 9: while the connection is down or the own echo is old the
    server's clock stands still, so no signal is fresh; an exp is capped at 3 minutes
    (a signal) and 15 minutes (a record) from the message's server time."""
    bus, _ = _unit_bus()

    async def presence(subject: str, seq: int, t: float, data: bytes) -> None:
        await bus._on_presence_msg(FakeMsg(subject, _presence_reply(bus, seq, seq, t), data))

    await presence("presence.bridge.home.v1", 1, T0, _body(home="home", people=True, iat=1, exp=int(T0) + 99999))
    await presence(f"presence.account.mattermost.{ACCOUNT}.v1", 2, T0, _account(exp=int(T0) + 99999))
    assert not bus.people_switched("home")  # not connected
    bus._connected, bus._nc = True, object()  # type: ignore[assignment]
    assert bus.people_switched("home") and bus.approver("dembaca")
    bus._own_echo = time.monotonic() - 1000  # the own presence has not come back
    assert not bus.people_switched("home")
    bus._own_echo = time.monotonic()
    await presence(f"presence.-.{bus.name}", 3, T0 + 181, b'{"v":1,"status":"online"}')
    assert not bus.people_switched("home") and bus.approver("dembaca")
    await presence(f"presence.-.{bus.name}", 4, T0 + 901, b'{"v":1,"status":"online"}')
    assert not bus.approver("dembaca")


async def test_a_gap_on_the_persons_consumer_reads_mattermost() -> None:
    """Fable finding 7: the consumer has no `since`; a gap starts the catch-up there."""
    bus, _ = _unit_bus()
    gaps: list[str] = []
    bus.on_person_gap = gaps.append
    n = bus.name
    ok = _body(kind="post", text="x", name="dembaca", mm=MM1)
    await bus._on_person_deliver("home", "bus.hu.home.a", _hu_reply(n, "home", 10, 1), ok)
    await bus._on_person_deliver("home", "bus.hu.home.a", _hu_reply(n, "home", 14, 3), ok)
    assert gaps == ["home"]


# --- the port: the switch, the keys, trust ------------------------------------------------------------

def _mm(pid: str, user: str, message: str, at: int, reactions: tuple[tuple[str, str, int], ...] = ()) -> dict[str, Any]:
    """A Mattermost post as its JSON (a read rebuilds the post from it)."""
    return {"id": pid, "channel_id": WORK, "user_id": user, "message": message, "create_at": at,
            "metadata": {"reactions": [{"user_id": u, "post_id": pid, "emoji_name": e, "create_at": t}
                                       for u, e, t in reactions]}}


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


async def test_a_persons_post_over_nats_keeps_its_mattermost_id_and_comes_once() -> None:
    """RT-6.42 step 9: a PostEvent with the Mattermost id, its root (a NATS post in the memory,
    else mm_root), author hu:<anchor>. Before the signal it is delivered too (Fable finding 6):
    its later Mattermost copy is the repeat, dropped by its key."""
    port, _, fake = make()
    assert fake is not None
    ev = await _events(port)
    await port._on_person(_person("home", "dembaca", 20, PersonMessage("post", "e" * 26, "dembaca", text="hi")))
    assert [e.post.id for e in _queued(ev)] == ["e" * 26]
    fake.switched.add("home")
    assert await port.skip(PostEvent(post=Post(id="e" * 26, channel_id=WORK, user_id=HUMAN, message="hi"),
                                     channel_type=None, edited=False))
    fake.switched.clear()
    assert await port.skip(PostEvent(post=Post(id="e" * 26, channel_id=WORK, user_id=HUMAN, message="hi"),
                                     channel_type=None, edited=False))  # not switched: the key drops it
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
    ms = int(t * 1000)
    later = _mm(MM2, HUMAN, "while the bridge was down", ms + 5000)
    # A yes on an ask posted long before (Fable finding 5): the `since` read returns it.
    ask = _mm(ASK, BOT, "merge?", ms - 600_000, ((HUMAN, "+1", ms + 6000),))
    inner.posts = [ask, _mm(MM1, HUMAN, "a", ms), later]
    fake.switched.clear()
    await port.check_people()
    for task in list(port._catching_up):
        await task
    got = _queued(ev)
    assert sorted((type(e).__name__, e.post.id if isinstance(e, PostEvent) else e.reaction.post_id)
                  for e in got) == [("PostEvent", MM2), ("ReactionEvent", ASK)]
    assert next(e for e in got if isinstance(e, PostEvent)).post.user_id == HUMAN  # its own account


async def test_the_catch_up_never_reaches_further_back_than_the_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fable finding 3 (High): after a quiet hour the keys are gone; a catch-up from the last
    people event would bring that post again through Mattermost, where a not_approver "go"
    approves. It starts no earlier than the keys' lifetime allows."""
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    now = time.time()
    port._people_at["home"] = int((now - 7200) * 1000)  # the last people event, two hours ago
    inner.posts = [_mm(MM1, HUMAN, "go", int((now - 7000) * 1000)), _mm(MM2, HUMAN, "new", int((now - 60) * 1000))]
    await port._catch_up("home")
    assert [e.post.id for e in _queued(ev)] == [MM2]
    assert inner.calls[-1][2] >= int((now - 3600) * 1000)


async def test_a_failed_catch_up_is_tried_again_at_the_next_look() -> None:
    """Fable finding 7: a catch-up whose read fails is owed and done at the next look."""
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    calls = {"n": 0}
    real = inner.channel_posts

    async def flaky(*a: Any, **kw: Any) -> list[Post]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("down")
        return await real(*a, **kw)

    inner.channel_posts = flaky  # type: ignore[method-assign]
    inner.posts = [_mm(MM2, HUMAN, "x", int(time.time() * 1000))]
    await port._catch_up("home")
    assert _queued(ev) == [] and set(port._owed) == {"home"}
    await port.check_people()
    for task in list(port._catching_up):
        await task
    assert [e.post.id for e in _queued(ev)] == [MM2] and not port._owed


async def test_a_persons_files_are_read_by_the_posts_mattermost_id() -> None:
    """Point 18, answer 4: the body has a count; the files come from Mattermost by `mm`."""
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    info = FileInfo(id="f" * 26, name="plan.txt")
    inner.posts = [Post(id=MM1, channel_id=WORK, user_id=HUMAN, message="see", files=(info,))]
    await port._on_person(_person("home", "dembaca", 70, PersonMessage("post", MM1, "dembaca", text="see", files=1)))
    (got,) = _queued(ev)
    assert [f.id for f in got.post.files] == ["f" * 26]


def test_not_approver_reaches_a_channel_tag() -> None:
    """Fable finding 1 (High): in channel mode the mark is a tag attribute too."""
    item = {"kind": "mention", "from_trusted_human": True, "not_approver": True,
            "post": {"id": MM1, "message": "go"}}
    assert channel.notification(item)["meta"]["not_approver"] == "true"


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


async def test_the_catch_up_after_a_gap_starts_before_the_event_that_showed_it() -> None:
    """Fable re-review: the event that shows a gap moves the last people event on before
    the catch-up runs; the catch-up starts from the one before the gap."""
    port, inner, fake = make()
    assert fake is not None
    fake.switched.add("home")
    ev = await _events(port)
    t = time.time()
    await port._on_person(_person("home", "dembaca", 80, PersonMessage("post", MM1, "dembaca", text="a"), t - 300))
    lost = _mm(MM2, HUMAN, "lost in the gap", int((t - 200) * 1000))
    inner.posts = [lost]
    port._person_gap("home")  # the bus saw the gap; then it delivers the event that showed it
    await port._on_person(_person("home", "dembaca", 90, PersonMessage("post", ASK, "dembaca", text="b"), t))
    for task in list(port._catching_up):
        await task
    ids = [e.post.id for e in _queued(ev)]
    assert MM2 in ids
    assert inner.calls[-1][2] <= int((t - 300) * 1000)


async def test_a_post_read_back_carries_not_approver_as_its_event_did(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer to question 5: get_thread, list_recent and an ask's card read back what came over
    NATS from a trusted person without a fresh approver record, marked not_approver, as
    wait_for_events marked it; once an approver record was fresh at its arrival, nothing."""
    from mattermost_ai_agent_bus import slim

    inner = FakeBus(FakeRegistrar(), trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"})
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, _settings(), ("home",), echo_wait=0.5)  # type: ignore[arg-type]
    fake = FakeNats(port)
    port.use_bus(fake)
    fake.attestations[ME] = _att()
    fake.accounts[HUMAN] = "dembaca"
    fake.switched.add("home")
    await port._on_person(_person("home", "dembaca", 60, PersonMessage("post", MM1, "dembaca", text="go", mm_root=ASK)))
    await port._on_person(_person("home", "dembaca", 61, PersonMessage("reaction", ASK, "dembaca", emoji="+1")))
    # Mattermost's copies, as get_thread reads them: the person's own account.
    reply = Post(id=MM1, channel_id=WORK, root_id=ASK, user_id=HUMAN, message="go")
    ask = Post(id=ASK, channel_id=WORK, user_id=BOT, message="merge?", kind="ask",
               reactions=(Reaction(HUMAN, ASK, "+1", 1),))
    got = await slim.posts(port, [ask, reply])  # type: ignore[arg-type]
    by_id = {p["id"]: p for p in got}
    assert by_id[MM1]["not_approver"] is True
    assert by_id[ASK]["reactions"][0]["not_approver"] is True
    # an approver at arrival: nothing marked
    fake.approvers.add("dembaca")
    await port._on_person(_person("home", "dembaca", 62, PersonMessage("post", MM2, "dembaca", text="ok")))
    other = Post(id=MM2, channel_id=WORK, user_id=HUMAN, message="ok")
    assert "not_approver" not in (await slim.posts(port, [other]))[0]  # type: ignore[arg-type]
