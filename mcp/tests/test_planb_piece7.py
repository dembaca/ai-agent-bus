"""RT-6.42 step 11 (ADR-014 phase 5, plan B piece 7): in a channel over NATS a reply in a person's
Mattermost thread and a reaction on a Mattermost post go over NATS, naming the post by its
Mattermost id (`mm_root`, `mm_ref`); the central bridge checks the post is in that channel and
writes the reply or reaction there (services/bridge/internal/mirror/target.go). The session
makes no Mattermost call for them: the receipts on a person's post, `resolve_ask` on an ask
asked on Mattermost before the switch, and the link `🙋 ask <id>` in a person's thread.

The switch is planb.on(7, channel) (RT-6.39 step 5); CB_PLANB_TEST="7" turns it on here. Unit
tests drive a NatsPort over a fake bus beside a chat that records every Mattermost call; with
MM_TEST_NATS=1 and CB_NATS_SERVER a real nats-server carries the reply and the reaction as the
bridge reads them.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fakes import BOT, WORK, FakeRegistrar
from test_nats_port import ME, _settings
from test_natsbus import Rig, _api, _consumer_specs, until
from test_planb_piece6 import Recording, Records, _att

from mattermost_ai_agent_bus import a2a, asks, natsbus, planb, server
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.nats_port import NatsEvents, NatsPort, NatsPortError, parse_post_id
from mattermost_ai_agent_bus.natsbus import Event, Message, MessageError, NatsSettings, decode, encode

HUMAN = "h" * 26  # dembaca's Mattermost account
PERSON_POST = "q" * 26  # a person's post in the home
OLD_ASK = "o" * 26  # an ask asked on Mattermost before the switch
P = a2a.DEFAULT_PREFIX
OTHER = "b" * 25  # another agent of the home


class Carrier(Records):
    """The fake bus whose bridge signal names the pieces in `pieces`."""

    pieces: frozenset[int] = frozenset({7})

    def bridge_pieces(self, channel: str) -> frozenset[int]:
        return self.pieces if channel in self.carries else frozenset()


def _world(monkeypatch: pytest.MonkeyPatch, on: str = "7") -> tuple[NatsPort, Recording, Carrier]:
    if on:
        monkeypatch.setenv(planb.TEST_ENV, on)
    else:
        monkeypatch.delenv(planb.TEST_ENV, raising=False)
    inner = Recording(FakeRegistrar(), trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"})
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, _settings(), ("home",), echo_wait=0.5)  # type: ignore[arg-type]
    fake = Carrier(port)
    fake.carries = {"home"}
    fake.pieces = frozenset(int(n) for n in on.split(",")) if on else frozenset()
    port.use_bus(fake)
    port.unmention = server._unmention_text
    port._ids = {"home": WORK}
    fake.attestations[ME] = _att()
    fake.accounts[HUMAN] = "dembaca"
    fake.approvers.add("dembaca")
    fake.switched.add("home")
    monkeypatch.setattr(server, "_get_client", lambda: port)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)
    return port, inner, fake


def _sent(fake: Carrier, kind: str) -> list[tuple[Any, ...]]:
    return [s for s in fake.sent if s[0] == kind]


# --- the switch --------------------------------------------------------------------------------

def test_the_switch_names_piece_7(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    assert planb.on(7, "home") is False
    monkeypatch.setenv(planb.TEST_ENV, "7")
    assert planb.on(7, "home") is True and planb.on(6, "home") is False


async def test_off_replies_and_reactions_to_a_persons_post_stay_on_mattermost(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Off by default: today's path, the registrar's bot on Mattermost, and nothing on NATS."""
    port, inner, fake = _world(monkeypatch, on="")
    sent = await port.post("on it", root_id=PERSON_POST)
    assert sent.root_id == PERSON_POST and parse_post_id(sent.id) is None
    await port.add_reaction(PERSON_POST, "eyes")
    assert inner.mm_calls == ["post", "add_reaction"] and fake.sent == []
    with pytest.raises(NatsPortError, match="only a NATS post id"):
        await port.remove_reaction(PERSON_POST, "eyes")


def test_the_wire_without_the_piece_is_byte_identical() -> None:
    """A message without `mm_root`/`mm_ref` encodes as before: no new key on the wire."""
    body = encode(Message("post", "hi", root=4, props={}))
    assert body == b'{"v":1,"kind":"post","text":"hi","root":4,"ref":null,"emoji":null,"props":{}}'
    body = encode(Message("reaction", ref=4, emoji="eyes"))
    assert b"mm_" not in body


def test_the_wire_names_a_mattermost_post_strictly() -> None:
    reply = decode(encode(Message("post", "hi", mm_root=PERSON_POST)))
    assert reply.mm_root == PERSON_POST and reply.root is None
    react = decode(encode(Message("reaction", emoji="eyes", mm_ref=PERSON_POST)))
    assert react.mm_ref == PERSON_POST and react.ref is None
    for bad in (Message("post", "hi", root=4, mm_root=PERSON_POST),  # never both
                Message("post", "hi", mm_root="n1:home:4"),  # a Mattermost id only
                Message("post", "hi", mm_root=PERSON_POST.upper()),
                Message("post", "hi", mm_ref=PERSON_POST),  # a post has no mm_ref
                Message("reaction", ref=4, emoji="eyes", mm_ref=PERSON_POST),
                Message("reaction", emoji="eyes", mm_root=PERSON_POST),
                Message("reaction", emoji="eyes")):  # still needs a target
        with pytest.raises(MessageError):
            encode(bad)
    raw = json.dumps({"v": 1, "kind": "reaction", "emoji": "eyes", "ref": 3, "mm_ref": PERSON_POST}).encode()
    with pytest.raises(MessageError):
        decode(raw)


# --- the session sends over NATS -------------------------------------------------------------------

async def test_on_a_reply_to_a_persons_post_goes_over_nats_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    sent = await port.post("on it @dembaca", root_id=PERSON_POST, props=a2a.props(kind="ACK"))
    (msg,) = _sent(fake, "post_mm")
    assert msg[1] == "home" and msg[2] == "on it @dembaca" and msg[3] == PERSON_POST
    assert f"{P}ask_to" not in msg[4]  # no mention kept: the bridge posts every one plain
    assert parse_post_id(sent.id) is not None and sent.root_id == PERSON_POST
    assert inner.mm_calls == []


async def test_on_a_reaction_and_its_removal_go_over_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    await port.add_reaction(PERSON_POST, "eyes")
    await port.remove_reaction(PERSON_POST, "eyes")
    assert fake.sent == [("react_mm", "home", PERSON_POST, "eyes"), ("unreact_mm", "home", PERSON_POST, "eyes")]
    assert inner.mm_calls == []


async def test_on_without_the_bridges_signal_for_piece_7_nothing_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: no fresh signal names piece 7 (the bridge down, older, its switch not read),
    so nobody would see it; the tool fails and nothing falls back to Mattermost."""
    port, inner, fake = _world(monkeypatch)
    fake.pieces = frozenset({6})
    with pytest.raises(NatsPortError, match="plan B piece 7"):
        await port.post("on it", root_id=PERSON_POST)
    with pytest.raises(NatsPortError, match="plan B piece 7"):
        await port.add_reaction(PERSON_POST, "eyes")
    assert fake.sent == [] and inner.mm_calls == []


async def test_on_a_failed_send_fails_and_nothing_goes_to_mattermost(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)

    async def down(*a: Any, **kw: Any) -> None:
        raise ConnectionError("nats down")
    fake.post_mm = down  # type: ignore[method-assign]
    fake.react_mm = down  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await port.post("on it", root_id=PERSON_POST)
    with pytest.raises(ConnectionError):
        await port.add_reaction(PERSON_POST, "eyes")
    assert inner.mm_calls == []


async def test_on_a_root_that_is_no_post_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    with pytest.raises(NatsPortError, match="nothing sent"):
        await port.post("on it", root_id="not-an-id")
    with pytest.raises(NatsPortError, match="nothing sent"):
        await port.add_reaction("not-an-id", "eyes")
    assert fake.sent == [] and inner.mm_calls == []


async def test_on_a_reply_with_files_stays_on_mattermost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Files are piece 8's: a reply with files goes where it went."""
    port, inner, fake = _world(monkeypatch)
    await port.post("the log", root_id=PERSON_POST, file_ids=["f" * 26])
    assert inner.mm_calls == ["post"] and fake.sent == []


async def test_on_a_post_of_a_channel_not_over_nats_keeps_its_reaction_on_mattermost(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A reaction goes to the channel the session saw the post in; one in a channel that does
    not run over NATS (a guest on Mattermost) stays there. A post never seen counts as the home's,
    where the bridge checks it."""
    port, inner, fake = _world(monkeypatch)
    port._saw_mm("g" * 26, "")
    await port.add_reaction("g" * 26, "eyes")
    assert inner.mm_calls == ["add_reaction"] and fake.sent == []
    await port.add_reaction("u" * 26, "eyes")
    assert fake.sent == [("react_mm", "home", "u" * 26, "eyes")]


# --- the uses: receipts, resolve_ask, the ask's link -------------------------------------------------

async def test_on_resolve_ask_of_an_ask_from_before_the_switch_goes_over_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.11 step 7: an ask asked on Mattermost before the switch gets its ✅ and its note over
    NATS, which the bridge writes on that Mattermost post under the agent's bot."""
    port, inner, fake = _world(monkeypatch, on="6,7")
    asks.add(BOT, OLD_ASK, "dembaca", OLD_ASK, title="old")
    out = json.loads(await server.resolve_ask(OLD_ASK, note="done, thanks"))
    assert out["was_open"] is True and asks.open_asks(BOT) == []
    assert ("react_mm", "home", OLD_ASK, a2a.done_reaction()) in fake.sent
    assert [s[3] for s in _sent(fake, "post_mm")] == [OLD_ASK]
    assert inner.mm_calls == []


async def test_on_a_reminder_of_an_ask_from_before_the_switch_keeps_its_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reminder names the asked person in `ask_to`, so the bridge keeps that one mention by
    piece 6's rule; any other reply there names nobody."""
    port, inner, fake = _world(monkeypatch)
    port.ask_source = lambda: [natsbus.OpenAsk(mm=OLD_ASK, kind="approval", human="dembaca",
                                               title="t", asked=0)]
    await port.post("@dembaca still waiting", root_id=OLD_ASK, props=a2a.props(kind=a2a.REMINDER))
    await port.post("@dembaca thanks", root_id=OLD_ASK)
    first, second = _sent(fake, "post_mm")
    assert first[4][f"{P}ask_to"] == "dembaca" and f"{P}ask_to" not in second[4]
    assert inner.mm_calls == []


async def test_on_the_asks_link_in_a_persons_thread_goes_over_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    """ask_human from a person's thread: with pieces 6 and 7 on, the ask and its `🙋 ask <id>`
    link both go over NATS, the link naming the person's post."""
    port, inner, fake = _world(monkeypatch, on="6,7")
    out = json.loads(await server.ask_human(need="Merge #42?", decision="merge #42", human="dembaca",
                                            root_id=PERSON_POST))
    (link,) = _sent(fake, "post_mm")
    assert link[3] == PERSON_POST and f"ask {out['ask']}" in link[2]
    assert out["linked_in"] == PERSON_POST and inner.mm_calls == []


async def test_on_a_receipt_on_a_persons_post_goes_over_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    from mattermost_ai_agent_bus import receipt
    port, inner, fake = _world(monkeypatch)
    await receipt._react(port, [PERSON_POST], receipt.Mark.SEEN)
    assert [s[:3] for s in fake.sent] == [("react_mm", "home", PERSON_POST)]
    assert inner.mm_calls == []


# --- what the other agents read --------------------------------------------------------------------

async def _events(port: NatsPort, *messages: Message) -> list[Any]:
    ev = NatsEvents(port)
    await ev.open()
    try:
        for i, m in enumerate(messages):
            await port._on_event(Event("home", OTHER, 900 + i, time.time(), m))
        got = []
        q = ev._queue
        assert q is not None
        while not q.empty():
            got.append(q.get_nowait())
        return got
    finally:
        await ev.close()


async def test_on_another_agents_reply_and_reaction_on_a_persons_post_read_as_on_mattermost(
        monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    got = await _events(port, Message("post", "on it", mm_root=PERSON_POST),
                        Message("reaction", emoji="eyes", mm_ref=PERSON_POST),
                        Message("unreaction", emoji="eyes", mm_ref=PERSON_POST))
    post = next(e for e in got if isinstance(e, PostEvent)).post
    assert post.root_id == PERSON_POST and post.user_id == OTHER
    (reaction,) = [e for e in got if isinstance(e, ReactionEvent)]
    assert reaction.reaction.post_id == PERSON_POST and reaction.reaction.user_id == OTHER
    assert OTHER in port._senders("home")  # its own bot's copy of the reaction is the bridge's


async def test_off_another_agents_message_naming_a_mattermost_post_reads_as_before(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """With the piece off a reply naming a Mattermost post is top-level, as an older session
    reads it, and a reaction without `ref` is dropped."""
    port, inner, fake = _world(monkeypatch, on="")
    got = await _events(port, Message("post", "on it", mm_root=PERSON_POST),
                        Message("reaction", emoji="eyes", mm_ref=PERSON_POST))
    assert [e.post.root_id for e in got if isinstance(e, PostEvent)] == [""]
    assert not [e for e in got if isinstance(e, ReactionEvent)]


# --- against a real server -------------------------------------------------------------------

async def test_against_a_real_server_a_reply_and_a_reaction_go_out_naming_the_persons_post(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The home's plan B record names piece 7 on the PRESENCE stream and the bridge's signal names
    it too; the reply and the reaction go on `bus.ch.home.<agent>` with `mm_root` and `mm_ref`, as
    the bridge reads them, and the reply's own copy comes back in the person's thread."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    name = rig.names["alice"]
    for stream, cname, cfg in _consumer_specs(name, ["home"], {}):
        if cname.endswith("_presence"):  # the bridge's signal, as consumers/manager.go adds it
            cfg = cfg | {"filter_subjects": [*cfg["filter_subjects"], "presence.bridge.home.v1"]}
        await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.{stream}.{cname}", {"stream_name": stream, "config": cfg})
    await rig.admin.publish("presence.planb.home.v1", json.dumps({"v": 1, "channel": "home", "pieces": [7]}).encode())
    await rig.admin.flush()
    inner = Recording(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home="home"), ("home",),  # type: ignore[arg-type]
                    bus_options={"heartbeat_sec": 0.2})
    seen: list[Any] = []

    async def keep(m: Any) -> None:
        seen.append(m)
    sub = await rig.admin.subscribe("bus.ch.home.*", cb=keep)
    await port.start(rig.logins["alice"], owner="alice")
    try:
        await until(lambda: planb.on(7, "home"), what="the plan B record naming piece 7")
        port._ids = {"home": WORK}
        with pytest.raises(NatsPortError, match="plan B piece 7"):  # no bridge signal yet
            await port.post("on it", root_id=PERSON_POST)
        now = int(time.time())
        await rig.admin.publish("presence.bridge.home.v1", json.dumps(
            {"v": 1, "home": "home", "people": True, "iat": now, "exp": now + 180, "pieces": [7]}).encode())
        await rig.admin.flush()
        bus = port._bus
        assert bus is not None
        await until(lambda: 7 in bus.bridge_pieces("home"), what="the bridge's signal naming piece 7")
        posted = await port.post("on it", root_id=PERSON_POST)
        assert parse_post_id(posted.id) is not None and posted.root_id == PERSON_POST
        await port.add_reaction(PERSON_POST, "eyes")
        await until(lambda: len(seen) >= 2, what="the reply and the reaction on bus.ch.home")
        reply, react = (json.loads(m.data) for m in seen[:2])
        assert all(m.subject == f"bus.ch.home.{name}" for m in seen[:2])
        assert reply["mm_root"] == PERSON_POST and reply["root"] is None and reply["text"] == "on it"
        assert react["kind"] == "reaction" and react["mm_ref"] == PERSON_POST and react["ref"] is None
        assert inner.mm_calls == []
    finally:
        await sub.unsubscribe()
        await port.stop()
    await asyncio.sleep(0)
