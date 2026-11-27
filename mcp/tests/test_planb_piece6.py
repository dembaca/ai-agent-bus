"""RT-6.11, RT-6.42 step 9 (ADR-014 phase 5, plan B piece 6): in a home over NATS an ask to a
human, its reminders, its answers (a person's reply and 👍/👎 on it) and resolve_ask go over NATS
alone; the bridge posts the ask for people under the agent's bot, with its card and the one
mention (services/bridge/internal/mirror/ask.go).

The switch is planb.on(6, channel) (RT-6.39 step 5); CB_PLANB_TEST="6" turns it on here. Unit
tests drive a NatsPort over a fake bus beside a chat that records every call of Mattermost the
ask could make; with MM_TEST_NATS=1 and CB_NATS_SERVER a real nats-server carries the ask as the
bridge reads it and a person's 👍 back as the bridge publishes it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fakes import BOT, WORK, FakeBus, FakeRegistrar
from test_nats_port import ME, FakeNats, _settings
from test_natsbus import Rig, _api, _consumer_specs, until

from mattermost_ai_agent_bus import a2a, asks, compose, natsbus, planb, remind, server
from mattermost_ai_agent_bus.events import ReactionEvent
from mattermost_ai_agent_bus.nats_port import NatsEvents, NatsPort, ask_wire, parse_post_id, person_id, read_props
from mattermost_ai_agent_bus.natsbus import Attestation, NatsSettings, PersonEvent, PersonMessage
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

HUMAN = "h" * 26  # dembaca's Mattermost account
MM_COPY = "c" * 26  # the bridge's copy of the ask in Mattermost
MM_REPLY = "r" * 26
P = a2a.DEFAULT_PREFIX

# What an ask could call on Mattermost: none of it may run with the switch on.
TRAPPED = ("post", "add_reaction", "remove_reaction", "reactions", "thread", "humans_among",
           "post_sync", "dm")


class Recording(FakeBus):
    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.mm_calls: list[str] = []


def _trap(name: str) -> Any:
    original = getattr(FakeBus, name)

    async def trapped(self: Recording, *a: Any, **kw: Any) -> Any:
        self.mm_calls.append(name)
        return await original(self, *a, **kw)
    return trapped


for _n in TRAPPED:
    if hasattr(FakeBus, _n) and asyncio.iscoroutinefunction(getattr(FakeBus, _n)):
        setattr(Recording, _n, _trap(_n))


class Records(FakeNats):
    """The fake bus with the login service's account records by name (RT-6.42 step 3)."""

    def account_person(self, account: str) -> tuple[str, str] | None:
        anchor = self.accounts.get(account, "-")
        return (anchor, anchor) if anchor != "-" and account not in self.stale else None


def _att() -> Attestation:
    return Attestation(owner_anchor="dembaca", owner_name="dembaca", trusted=(("dembaca", "dembaca"),),
                       cut=False, exp=1e12)


def _world(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> tuple[NatsPort, Recording, Records]:
    if on:
        monkeypatch.setenv(planb.TEST_ENV, "6")
    else:
        monkeypatch.delenv(planb.TEST_ENV, raising=False)
    inner = Recording(FakeRegistrar(), trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"})
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, _settings(), ("home",), echo_wait=0.5)  # type: ignore[arg-type]
    fake = Records(port)
    port.use_bus(fake)
    port._ids = {"home": WORK}
    fake.attestations[ME] = _att()
    fake.accounts[HUMAN] = "dembaca"
    fake.approvers.add("dembaca")
    fake.switched.add("home")  # the bridge carries the home's people (RT-6.42 step 7)
    monkeypatch.setattr(server, "_get_client", lambda: port)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)
    return port, inner, fake


def _person(seq: int, message: PersonMessage, t: float | None = None, anchor: str = "dembaca") -> PersonEvent:
    return PersonEvent("home", anchor, seq, time.time() if t is None else t, message)


async def _ask(port: NatsPort) -> str:
    out = json.loads(await server.ask_human(need="Merge #42?", decision="merge #42 into main",
                                            human="dembaca", answers=["go", "no-go"]))
    return str(out["ask"])


# --- the switch --------------------------------------------------------------------------------

def test_the_switch_names_piece_6(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    assert planb.on(6, "home") is False
    monkeypatch.setenv(planb.TEST_ENV, "6")
    assert planb.on(6, "home") is True and planb.on(5, "home") is False


async def test_off_an_ask_in_a_nats_home_stays_on_mattermost(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch, on=False)
    ask = await _ask(port)
    assert parse_post_id(ask) is None and "post" in inner.mm_calls
    assert not [s for s in fake.sent if s[0] == "post"]


# --- the ask over NATS ---------------------------------------------------------------------------

async def test_on_the_ask_goes_over_nats_with_its_card_and_one_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    assert parse_post_id(ask) == ("home", fake.seq)
    assert asks.open_asks(BOT) == [ask]
    (sent,) = [s for s in fake.sent if s[0] == "post"]
    _, channel, text, root, props = sent
    assert channel == "home" and root is None and "@dembaca" in text
    assert props[f"{P}a2a_kind"] == "ask" and props[f"{P}ask_to"] == "dembaca"
    card = json.loads(props[f"{P}ask_card"])
    assert card[0]["title"] == "Merge #42?"
    assert {f["title"]: f["value"] for f in card[0]["fields"]}["Decision"] == "merge #42 into main"
    assert json.loads(props[f"{P}ask_answers"]) == ["go", "no-go"]
    assert inner.mm_calls == [], "with piece 6 on the ask makes no Mattermost call"
    # agents read the ask over NATS with its card, as from the Mattermost post
    (post,) = [p for p in await port.channel_posts(WORK) if p.id == ask]
    assert a2a.ask_card(post)["decision"] == "merge #42 into main"
    assert a2a.ask_answer_list(post) == ["go", "no-go"]


def test_the_card_comes_back_only_on_an_ask() -> None:
    props = {"attachments": [{"title": "Q?", "fields": []}], "ask_answers": ["a", "b"]}
    wire = ask_wire(props, "🙋 @dembaca\nmore @bob", P)
    assert wire[f"{P}ask_to"] == "dembaca"
    back = read_props({**wire, f"{P}a2a_kind": "ask"}, P)
    assert back["attachments"] == props["attachments"] and back["ask_answers"] == ["a", "b"]
    assert f"{P}ask_card" not in back
    other = read_props({**wire, f"{P}a2a_kind": "TASK"}, P)  # not an ask: the props stay text
    assert "attachments" not in other and f"{P}ask_card" in other
    broken = read_props({f"{P}a2a_kind": "ask", f"{P}ask_card": "{not json"}, P)
    assert "attachments" not in broken


async def test_on_a_failed_send_fails_the_ask_and_nothing_goes_to_mattermost(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)

    async def down(*a: Any, **kw: Any) -> None:
        raise ConnectionError("nats down")
    fake.post = down  # type: ignore[method-assign]
    with pytest.raises(ConnectionError):
        await _ask(port)
    assert inner.mm_calls == [] and asks.open_asks(BOT) == []


async def test_on_humans_among_comes_from_the_account_records(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    assert await port.humans_among({"dembaca", "bob", "agent-x"}) == {"dembaca"}
    fake.stale.add(HUMAN)  # a stale record names nobody
    assert await port.humans_among({"dembaca"}) == set()
    assert inner.mm_calls == []
    monkeypatch.delenv(planb.TEST_ENV)
    await port.humans_among({"dembaca"})
    assert inner.mm_calls == ["humans_among"]  # off: Mattermost's lookup, as before


# --- the answers ---------------------------------------------------------------------------------

async def _inbox(port: NatsPort, monkeypatch: pytest.MonkeyPatch) -> WebSocketInbox:
    client = compose.Remembered(port)  # type: ignore[arg-type]
    monkeypatch.setattr(server, "_get_client", lambda: client)
    inbox = WebSocketInbox(client=client, watched_channel_ids={WORK}, source=NatsEvents(port))  # type: ignore[arg-type]
    await inbox.ensure_connected()
    return inbox


async def test_on_a_trusted_persons_thumbs_up_on_the_copy_approves_the_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    seq = parse_post_id(ask)[1]  # type: ignore[index]
    inbox = await _inbox(port, monkeypatch)
    try:
        # the bridge carries the person's 👍 on its copy with the copy's id and the ask's sequence
        await port._on_person(_person(seq + 1, PersonMessage("reaction", MM_COPY, "dembaca", emoji="+1", ref=seq)))
        items = await server._event_items(await inbox.take(timeout_sec=2, max_events=10))
    finally:
        await inbox.close()
    (item,) = [i for i in items if i.get("kind") == "reaction"]
    assert item["ask"] == ask and item["answer"] == "yes" and item["from_trusted_human"]
    assert not item.get("not_approver") and not item.get("expired")
    assert [r.user_id for r in await port.reactions(ask)] == [person_id("dembaca")]
    assert inner.mm_calls == []


async def test_on_a_thumbs_up_without_an_approver_record_approves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    fake.approvers.clear()
    ask = await _ask(port)
    seq = parse_post_id(ask)[1]  # type: ignore[index]
    inbox = await _inbox(port, monkeypatch)
    try:
        await port._on_person(_person(seq + 1, PersonMessage("reaction", MM_COPY, "dembaca", emoji="+1", ref=seq)))
        items = await server._event_items(await inbox.take(timeout_sec=2, max_events=10))
    finally:
        await inbox.close()
    (item,) = [i for i in items if i.get("kind") == "reaction"]
    assert item["not_approver"] and ask in asks.blocking(BOT, "dembaca")
    assert not (await remind._answered(port, BOT, ask, "", "dembaca"))[0]  # type: ignore[arg-type]


async def test_on_a_yes_after_24_hours_comes_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    seq = parse_post_id(ask)[1]  # type: ignore[index]
    asks.resolve(BOT, ask)
    asks.add(BOT, ask, "dembaca", ask, title="Merge #42?", at=time.time() - 25 * 3600)
    inbox = await _inbox(port, monkeypatch)
    try:
        await port._on_person(_person(seq + 1, PersonMessage("reaction", MM_COPY, "dembaca", emoji="+1", ref=seq)))
        items = await server._event_items(await inbox.take(timeout_sec=2, max_events=10))
    finally:
        await inbox.close()
    (item,) = [i for i in items if i.get("kind") == "reaction"]
    assert item["expired"] is True


async def test_on_a_persons_reply_in_the_asks_thread_answers_it_and_stops_the_reminders(
        monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    seq = parse_post_id(ask)[1]  # type: ignore[index]
    assert not (await remind._answered(port, BOT, ask, "", "dembaca"))[0]  # type: ignore[arg-type]
    await port._on_person(_person(seq + 1, PersonMessage("post", MM_REPLY, "dembaca", text="go ahead",
                                                         mm_root=MM_COPY, root=seq), t=time.time() + 1))
    thread = await port.thread(ask)
    assert [p.id for p in thread.posts] == [ask, MM_REPLY]
    assert thread.posts[1].root_id == ask and thread.posts[1].user_id == person_id("dembaca")
    assert (await remind._answered(port, BOT, ask, "", "dembaca"))[0]  # type: ignore[arg-type]
    # the merged reads give the reply in the NATS thread too, not under the copy's id
    (reply,) = [p for p in await port.channel_posts(WORK) if p.id == MM_REPLY]
    assert reply.root_id == ask
    assert inner.mm_calls == [] or set(inner.mm_calls) <= {"channel_posts"}


async def test_on_a_reminder_goes_over_nats_with_its_mention(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    asks.resolve(BOT, ask)
    asks.add(BOT, ask, "dembaca", ask, title="Merge #42?", at=time.time() - 3 * 3600)
    await remind.tick(port, BOT)  # type: ignore[arg-type]
    reminders = [s for s in fake.sent if s[0] == "post" and s[4].get(f"{P}a2a_kind") == a2a.REMINDER]
    assert reminders, fake.sent
    _, _, text, root, props = reminders[0]
    assert root == parse_post_id(ask)[1] and props[f"{P}ask_to"] == "dembaca" and "@dembaca" in text  # type: ignore[index]
    assert f"{P}ask_card" not in props
    assert inner.mm_calls == []


async def test_on_resolve_ask_puts_its_check_mark_and_note_over_nats(monkeypatch: pytest.MonkeyPatch) -> None:
    port, inner, fake = _world(monkeypatch)
    ask = await _ask(port)
    seq = parse_post_id(ask)[1]  # type: ignore[index]
    out = json.loads(await server.resolve_ask(ask, note="merged, thanks"))
    assert out["was_open"] is True and asks.open_asks(BOT) == []
    assert ("react", "home", seq, a2a.done_reaction()) in fake.sent
    assert [s[3] for s in fake.sent if s[0] == "post"][-1] == seq  # the note in the ask's thread
    assert inner.mm_calls == []


async def test_on_an_answer_to_an_ask_the_memory_lost_still_names_the_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a restart the ask may be out of the memory: the bridge's sequence names it, when it
    is an open ask of this session; any other sequence may be a person's own post the bridge
    carried (its `root` for a reply to it), never taken for a NATS post."""
    port, inner, fake = _world(monkeypatch)
    port.ask_source = lambda: [natsbus.OpenAsk(mm="n1:home:77", kind="approval", human="dembaca",
                                               title="t", asked=0)]
    ev = NatsEvents(port)
    await ev.open()
    try:
        await port._on_person(_person(500, PersonMessage("reaction", MM_COPY, "dembaca", emoji="+1", ref=77)))
        await port._on_person(_person(501, PersonMessage("post", MM_REPLY, "dembaca", text="go", mm_root=MM_COPY,
                                                         root=77)))
        got = []
        q = ev._queue
        assert q is not None
        while not q.empty():
            got.append(q.get_nowait())
    finally:
        await ev.close()
    reaction = next(e for e in got if isinstance(e, ReactionEvent))
    assert reaction.reaction.post_id == "n1:home:77"
    assert next(e for e in got if not isinstance(e, ReactionEvent)).post.root_id == "n1:home:77"
    # a reply to a person's post the bridge carried as sequence 90: its Mattermost root stays
    await port._on_person(_person(503, PersonMessage("post", "s" * 26, "dembaca", text="re", mm_root=MM_REPLY,
                                                     root=90)))
    assert [p for p in port._person_replies.values() if p.id == "s" * 26] == []
    monkeypatch.delenv(planb.TEST_ENV)  # off: as before, the copy's Mattermost id
    ev2 = NatsEvents(port)
    await ev2.open()
    try:
        await port._on_person(_person(502, PersonMessage("reaction", MM_COPY, "dembaca", emoji="-1", ref=78)))
        q = ev2._queue
        assert q is not None
        assert q.get_nowait().reaction.post_id == MM_COPY
    finally:
        await ev2.close()


async def test_on_an_ask_of_before_the_switch_is_resolved_where_it_was_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The way across (an open point of the piece): an ask asked on Mattermost before the switch
    keeps its ✅ there, as a reaction to a Mattermost post (piece 7's)."""
    port, inner, fake = _world(monkeypatch)
    old = "o" * 26
    asks.add(BOT, old, "dembaca", old, title="old")
    await server.resolve_ask(old)
    assert inner.mm_calls == ["add_reaction"] and not [s for s in fake.sent if s[0] == "react"]


# --- against a real server -------------------------------------------------------------------

async def test_against_a_real_server_the_ask_goes_out_and_a_persons_thumbs_up_comes_back(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The home's plan B record names piece 6 on the PRESENCE stream; the ask goes on
    `bus.ch.home.<agent>` with its card and mention as the bridge reads it, and the bridge's
    `bus.hu.home.<anchor>` event of a 👍 on its copy comes back as a reaction on the NATS ask."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    name = rig.names["alice"]
    for stream, cname, cfg in _consumer_specs(name, ["home"], {}):
        await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.{stream}.{cname}", {"stream_name": stream, "config": cfg})
    hu = {"durable_name": f"{name}_hu_home", "deliver_subject": f"deliver.{name}.hu.home", "ack_policy": "none",
          "deliver_policy": "new", "filter_subject": "bus.hu.home.*", "metadata": {"agent": name}}
    await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.BUS.{name}_hu_home", {"stream_name": "BUS", "config": hu})
    await rig.admin.publish("presence.planb.home.v1", json.dumps({"v": 1, "channel": "home", "pieces": [6]}).encode())
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
    events = NatsEvents(port)
    await events.open()
    stream = events.events()
    try:
        await until(lambda: planb.on(6, "home"), what="the plan B record naming piece 6")
        port._ids = {"home": WORK}
        card = a2a.attachment_props([("Decision", "merge it", False)], fallback="q", urgent=False, title="Merge?")
        posted = await port.post("🙋 @dembaca", props=card | (a2a.props(kind=a2a.ASK) or {}) | {"ask_answers": ["go", "no-go"]})
        got = parse_post_id(posted.id)
        assert got is not None and got[0] == "home"
        await until(lambda: bool(seen), what="the ask on bus.ch.home")
        body = json.loads(seen[0].data)
        assert seen[0].subject == f"bus.ch.home.{name}"
        assert body["props"][f"{P}ask_to"] == "dembaca" and json.loads(body["props"][f"{P}ask_card"])[0]["title"] == "Merge?"
        assert a2a.ask_card(posted)["decision"] == "merge it"
        # the bridge's event of dembaca's 👍 on its copy
        await rig.admin.publish("bus.hu.home.dembaca", json.dumps(
            {"v": 1, "kind": "reaction", "mm": MM_COPY, "name": "dembaca", "emoji": "+1", "ref": got[1]}).encode())
        await rig.admin.flush()

        async def reaction() -> ReactionEvent:
            while True:
                e = await stream.__anext__()
                if isinstance(e, ReactionEvent):
                    return e
        ev = await asyncio.wait_for(reaction(), 5)
        assert ev.reaction.post_id == posted.id and ev.reaction.user_id == person_id("dembaca")
        assert "post" not in inner.mm_calls and "add_reaction" not in inner.mm_calls
    finally:
        await sub.unsubscribe()
        await events.close()
        await port.stop()
