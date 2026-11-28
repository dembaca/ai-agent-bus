"""RT-6.42 step 11 (ADR-014 phase 5, plan B piece 11): people's events where no bridge signal
is fresh. With the switch `planb.on(11, channel)` on (RT-6.39 step 5, off by default), a channel's
people come from NATS alone, whatever the bridge's signal: when the signal runs out nothing is
read on Mattermost (no catch-up, also not after a gap or a failed read), Mattermost's own
events of people there are dropped as while the signal is fresh, and the session says once that
its people are not heard (the log, and bus_info while it holds). A person's event over NATS is
delivered as before. The way back is the switch off: Mattermost is read again from where people
stopped being heard. Off, nothing changes.

Unit tests on the fake bus, and, with MM_TEST_NATS=1 and CB_NATS_SERVER, a test against a real
nats-server whose presence stream carries the home's plan B record and the bridge's signal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeRegistrar
from test_nats_port import make
from test_natsbus import Rig, _api, _consumer_specs, until

from mattermost_ai_agent_bus import lease, planb, server
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.model import Post, Reaction
from mattermost_ai_agent_bus.nats_port import NatsEvents, NatsPort, person_id
from mattermost_ai_agent_bus.natsbus import NatsSettings, PersonEvent, PersonMessage

from test_debug import chat, pinned  # noqa: F401 — fixtures

HUMAN = "h" * 26  # a person's Mattermost account
OTHER_BOT = "b" * 26  # another agent's registrar bot, on Mattermost
MM1 = "q1w2e3r4t5y6u7i8o9p0a1s2d3"
MM2 = "z9x8c7v6b5n4m3l2k1j0h9g8f7"
MM3 = "m" * 26
ASK = "a" * 26


def _on(monkeypatch: pytest.MonkeyPatch, on: bool = True) -> None:
    if on:
        monkeypatch.setenv(planb.TEST_ENV, "11")
    else:
        monkeypatch.delenv(planb.TEST_ENV, raising=False)


def _mm(pid: str, user: str, message: str, at: int) -> dict[str, Any]:
    return {"id": pid, "channel_id": WORK, "user_id": user, "message": message, "create_at": at}


def _person(seq: int, message: PersonMessage, t: float | None = None) -> PersonEvent:
    return PersonEvent("home", "dembaca", seq, time.time() if t is None else t, message)


def _post(pid: str, user: str = HUMAN, **kw: Any) -> PostEvent:
    return PostEvent(post=Post(id=pid, channel_id=WORK, user_id=user, message="hi", **kw),
                     channel_type=None, edited=False)


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


def _reads(inner: FakeBus) -> list[tuple[str, int | None, int | None]]:
    return [c for c in inner.calls if c[0] == "posts"]


async def _settle(port: NatsPort) -> None:
    for task in list(port._catching_up):
        await task


# --- off: as before -----------------------------------------------------------------------------

async def test_off_a_signal_that_runs_out_reads_mattermost_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 9, unchanged with the switch off: the catch-up on Mattermost, and Mattermost's
    people kept while no signal is fresh."""
    _on(monkeypatch, False)
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    fake.switched.add("home")
    await port.check_people()
    t = time.time()
    inner.posts = [_mm(MM2, HUMAN, "while the bridge was down", int(t * 1000) + 5000)]
    fake.switched.clear()
    await port.check_people()
    await _settle(port)
    assert _reads(inner) and [e.post.id for e in _queued(ev)] == [MM2]
    assert not await port.skip(_post(MM1))
    assert port.people_unheard() == [] and port._unheard == {}


# --- on: no Mattermost read for people ----------------------------------------------------------

async def test_on_no_mattermost_read_when_the_signal_runs_out(monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.42 step 11: no catch-up, Mattermost's people dropped without a signal; bots, webhooks
    and system posts stay on Mattermost; a person's event over NATS still comes."""
    _on(monkeypatch)
    port, inner, fake = make()
    assert fake is not None
    inner.bots["helper"] = type("U", (), {"id": OTHER_BOT})()
    ev = await _events(port)
    fake.switched.add("home")
    await port.check_people()
    inner.posts = [_mm(MM2, HUMAN, "while the bridge was down", int(time.time() * 1000) + 5000)]
    fake.switched.clear()
    with caplog.at_level(logging.INFO, logger="mattermost_ai_agent_bus.nats_port"):
        await port.check_people()
        await port.check_people()
    await _settle(port)
    assert _reads(inner) == [] and not port._catching_up
    assert _queued(ev) == []
    warned = [r for r in caplog.records if "are not heard" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING  # said once
    assert not any("read on Mattermost again" in r.getMessage() for r in caplog.records)
    # Mattermost's people dropped, also an edit and a reaction; a bot, a webhook, a system post kept
    assert await port.skip(_post(MM1))
    assert await port.skip(PostEvent(post=Post(id=MM1, channel_id=WORK, user_id=HUMAN, message="hi!"),
                                     channel_type=None, edited=True))
    assert await port.skip(ReactionEvent(Reaction(HUMAN, ASK, "+1", 1), WORK))
    assert not await port.skip(_post(MM3, OTHER_BOT))
    assert not await port.skip(_post(MM3, from_webhook=True))
    assert not await port.skip(_post(MM3, type="system_join_channel"))
    assert not await port.skip(PostEvent(post=Post(id=MM3, channel_id="elsewhere", user_id=HUMAN),
                                         channel_type=None, edited=False))
    # people only from NATS: a person's post over NATS (the bridge's catch-up once it is back)
    await port._on_person(_person(60, PersonMessage("post", MM3, "dembaca", text="still there?")))
    got = _queued(ev)
    assert [(e.post.id, e.post.user_id) for e in got] == [(MM3, person_id("dembaca"))]
    assert _reads(inner) == []


async def test_on_a_gap_and_an_owed_catch_up_read_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a gap on the person's consumer and a catch-up owed from before the
    switch read nothing on Mattermost either."""
    _on(monkeypatch)
    port, inner, fake = make()
    assert fake is not None
    inner.posts = [_mm(MM2, HUMAN, "lost in the gap", int(time.time() * 1000))]
    fake.switched.add("home")
    await port.check_people()
    port._person_gap("home")
    port._owed["home"] = 1
    await port.check_people()
    await _settle(port)
    assert _reads(inner) == [] and port._owed == {}
    assert port.people_unheard() == []  # the signal is fresh: people are heard


async def test_on_people_are_not_heard_until_the_signal_is_back(monkeypatch: pytest.MonkeyPatch,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.42 step 11: said from the first look also when no signal was ever fresh (a session
    started while the bridge was down), and heard again once the signal is back."""
    _on(monkeypatch)
    port, inner, fake = make()
    assert fake is not None
    with caplog.at_level(logging.INFO, logger="mattermost_ai_agent_bus.nats_port"):
        await port.check_people()
        assert port.people_unheard() == ["home"] and "home" in port._unheard
        fake.switched.add("home")
        await port.check_people()
    assert port.people_unheard() == [] and port._unheard == {}
    assert any("are heard again" in r.getMessage() for r in caplog.records)
    assert _reads(inner) == []


async def test_on_an_event_not_told_apart_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: with the switch on, an event whose author cannot be told a bot or a person is
    dropped, never taken from Mattermost as a person's; off it is kept, as before."""
    port, inner, fake = make()
    assert fake is not None

    async def broken(_ids: set[str]) -> set[str]:
        raise OSError("down")

    inner.bot_accounts = broken  # type: ignore[method-assign]
    _on(monkeypatch, False)
    assert not await port.skip(_post(MM1))
    _on(monkeypatch)
    assert await port.skip(_post(MM1))


async def test_the_way_back_reads_mattermost_from_where_people_stopped_being_heard(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-014 piece 11's way back: the switch off while no signal is fresh reads Mattermost
    again, from the last people event before the people stopped being heard."""
    _on(monkeypatch)
    port, inner, fake = make()
    assert fake is not None
    ev = await _events(port)
    fake.switched.add("home")
    await port.check_people()
    t = time.time()
    await port._on_person(_person(50, PersonMessage("post", MM1, "dembaca", text="a"), t))
    _queued(ev)
    fake.switched.clear()
    await port.check_people()
    assert port._unheard == {"home": int(t * 1000)} and _reads(inner) == []
    inner.posts = [_mm(MM1, HUMAN, "a", int(t * 1000)), _mm(MM2, HUMAN, "meanwhile", int(t * 1000) + 5000)]
    _on(monkeypatch, False)
    await port.check_people()
    await _settle(port)
    assert [e.post.id for e in _queued(ev)] == [MM2]  # MM1's key held: once
    assert _reads(inner) and _reads(inner)[-1][2] <= int(t * 1000)
    assert port._unheard == {} and port.people_unheard() == []


# --- bus_info -----------------------------------------------------------------------------------

class _Side:
    """A NATS side that runs, whose home's people are not heard."""

    bridge_bot = "bridge-bot"

    def down(self) -> str:
        return ""

    def crown_view(self, home: str) -> None:
        return None

    def ambiguous(self) -> list[str]:
        return []

    def people_unheard(self) -> list[str]:
        return ["home"]

    async def prime(self) -> None:
        return None


async def test_bus_info_says_people_are_not_heard(chat: Any) -> None:  # noqa: F811
    """RT-6.42 step 11: bus_info warns while a channel's people are not heard."""
    lease.use_nats("home", _Side())  # type: ignore[arg-type]
    try:
        info = json.loads(await server.bus_info())
        assert "people in ~home are not heard now" in info["warning"]
        assert "plan B piece 11" in info["warning"] and "once the bridge carries ~home again" in info["warning"]
    finally:
        lease.use_nats("", None)


async def test_the_note_follows_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    port, _, fake = make()
    assert fake is not None
    lease.use_nats("home", port)  # type: ignore[arg-type]
    try:
        assert any("people in ~home are not heard" in n for n in lease.nats_notes())
        fake.switched.add("home")
        assert not any("not heard" in n for n in lease.nats_notes())
        fake.switched.clear()
        _on(monkeypatch, False)
        assert not any("not heard" in n for n in lease.nats_notes())
    finally:
        lease.use_nats("", None)


# --- against a real server ------------------------------------------------------------------------

class Watched(FakeBus):
    """A chat that logs every read of a channel's posts."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.mm_reads: list[str] = []
        for n in ("channel_posts", "posts_after", "list_recent", "thread", "reactions"):
            orig = getattr(self, n)

            async def wrapped(*args: Any, _n: str = n, _o: Any = orig, **kwargs: Any) -> Any:
                self.mm_reads.append(_n)
                return await _o(*args, **kwargs)

            setattr(self, n, wrapped)


async def _real_port(rig: Rig, pieces: list[int]) -> tuple[NatsPort, Watched]:
    name = rig.names["alice"]
    for stream, cname, cfg in _consumer_specs(name, ["home"], {}):
        if cname.endswith("_presence"):  # the bridge's signal, as consumers/manager.go adds it
            cfg = cfg | {"filter_subjects": [*cfg["filter_subjects"], "presence.bridge.home.v1"]}
        await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.{stream}.{cname}", {"stream_name": stream, "config": cfg})
    hu = {"durable_name": f"{name}_hu_home", "deliver_subject": f"deliver.{name}.hu.home", "ack_policy": "none",
          "deliver_policy": "new", "filter_subject": "bus.hu.home.*", "metadata": {"agent": name}}
    await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.BUS.{name}_hu_home", {"stream_name": "BUS", "config": hu})
    await rig.admin.publish("presence.planb.home.v1",
                            json.dumps({"v": 1, "channel": "home", "pieces": pieces}).encode())
    await rig.admin.flush()
    inner = Watched(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home="home"), ("home",),  # type: ignore[arg-type]
                    bus_options={"heartbeat_sec": 0.2})
    return port, inner


async def _signal(rig: Rig, ttl: int) -> None:
    now = int(time.time())
    await rig.admin.publish("presence.bridge.home.v1", json.dumps(
        {"v": 1, "home": "home", "people": True, "iat": now, "exp": now + ttl}).encode())
    await rig.admin.flush()


async def _person_post(rig: Rig, mm: str, text: str) -> None:
    await rig.admin.publish("bus.hu.home.dembaca", json.dumps(
        {"v": 1, "kind": "post", "mm": mm, "name": "dembaca", "text": text}).encode())
    await rig.admin.flush()


async def _next_post(stream: Any) -> PostEvent:
    async def first() -> PostEvent:
        while True:
            e = await stream.__anext__()
            if isinstance(e, PostEvent):
                return e
    return await asyncio.wait_for(first(), 5)


@pytest.mark.parametrize("on", [True, False], ids=["on", "off"])
async def test_against_a_real_server_a_signal_that_runs_out(rig: Rig, monkeypatch: pytest.MonkeyPatch,
                                                            on: bool) -> None:
    """The home's plan B record names piece 11 (or not) on the PRESENCE stream, and the bridge's
    signal runs out: on, no Mattermost read and Mattermost's person dropped, the people not heard,
    a person's post over NATS still delivered; off, the catch-up reads Mattermost as before."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    port, inner = await _real_port(rig, [11] if on else [])
    await port.start(rig.logins["alice"], owner="alice")
    events = NatsEvents(port)
    await events.open()
    stream = events.events()
    try:
        await until(lambda: planb.known(), what="the plan B record")
        assert planb.on(11, "home") is on
        port._ids = {"home": WORK}
        bus = port._bus
        assert bus is not None
        await _signal(rig, 2)
        await until(lambda: bus.people_switched("home"), what="the bridge's fresh signal")
        await port.check_people()
        await _person_post(rig, MM1, "hello")
        got = await _next_post(stream)
        assert (got.post.id, got.post.user_id) == (MM1, person_id("dembaca"))
        await until(lambda: not bus.people_switched("home"), timeout=10, what="the signal running out")
        await port.check_people()
        await _settle(port)
        if not on:
            assert "channel_posts" in inner.mm_reads and port.people_unheard() == []
            assert not await port.skip(_post(MM2))
            return
        assert inner.mm_reads == [] and port.people_unheard() == ["home"]
        assert await port.skip(_post(MM2))  # Mattermost's person dropped
        await _person_post(rig, MM3, "still there?")  # the bridge's catch-up once it is back
        got = await _next_post(stream)
        assert got.post.id == MM3 and inner.mm_reads == []
        await _signal(rig, 60)
        await until(lambda: bus.people_switched("home"), what="the signal back")
        await port.check_people()
        assert port.people_unheard() == [] and inner.mm_reads == []
    finally:
        await events.close()
        await port.stop()
