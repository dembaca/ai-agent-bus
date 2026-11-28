"""RT-6.6 steps 1 and 7, RT-6.37 step 6, RT-6.38 step 3 (ADR-014 phase 5, plan B piece 9): in a
home over NATS with piece 9 on, and piece 2 (the roster from NATS) which it needs, the session
writes no status, no presence and no header to Mattermost. Its presence states the status
(`shown`) for the bridge to show under the agent's bridge bot (RT-6.44), the crown apart, which
the bridge sets by the claims; the bus line of the header goes to the bridge on
`<req>.header.<agent>`.

The switch is planb.on(9, channel), CB_PLANB_TEST="2,9" in a test. Unit tests on the fake bus of
test_nats_port.py; with MM_TEST_NATS=1 and CB_NATS_SERVER, tests against a real nats-server (the
rig of test_natsbus.py) for the presence and the header request.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from fakes import BOT, WORK, FakeBus, FakeRegistrar
from test_crown_nats import CROWN, HOME, NAME_A, NAME_ME, TAG, World, claimant
from test_natsbus import FakeLogin, Rig

from mattermost_ai_agent_bus import a2a, lease, natsbus
from mattermost_ai_agent_bus.model import StatusMark
from mattermost_ai_agent_bus.nats_port import NatsPort
from mattermost_ai_agent_bus.natsbus import NatsBus, NatsSettings
from mattermost_ai_agent_bus.requests import RequestError

WORKING = a2a.status_emoji(StatusMark.WORKING)
IDLE = a2a.status_emoji(StatusMark.IDLE)
BUS = a2a.status_emoji(StatusMark.BUS)


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> World:
    """test_crown_nats's world, with what piece 2's roster reads of the bus (piece 9 needs it):
    the home's member names, each name's card and the bridge's bot records."""
    wd = World()
    f = wd.fake
    monkeypatch.setattr(f, "member_names", lambda ch: sorted(n for c, n in f.members if c == ch), raising=False)
    monkeypatch.setattr(f, "card_of", lambda n: natsbus.PresenceCard(), raising=False)
    monkeypatch.setattr(f, "bot_record", lambda n: None, raising=False)
    await wd.prime()
    claimant(wd.fake, NAME_ME, 5, bot_id=BOT)
    return wd


def _no_mattermost_status(inner: FakeBus) -> list[str]:
    """Every status, presence and header call to Mattermost fails the test; returns the log."""
    calls: list[str] = []

    def forbid(name: str) -> Any:
        async def call(*_a: Any, **_kw: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"plan B piece 9: Mattermost was called: {name}")
        return call

    for name in ("set_presence", "set_custom_status", "clear_custom_status", "channel_header",
                 "set_channel_header"):
        setattr(inner, name, forbid(name))
    return calls


class Header:
    """The port's request to the bridge for the header line (NatsPort.header_line)."""

    def __init__(self, answer: bool | BaseException = True) -> None:
        self.answer = answer
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, home: str, line: str) -> bool:
        self.sent.append((home, line))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


# --- the status ------------------------------------------------------------------------------------

async def test_with_the_switch_off_the_status_goes_to_mattermost_as_before(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
    status = world.inner.status
    assert status is not None and status["emoji"] == WORKING
    assert status["text"] == f"working · proj · ⇄{TAG[NAME_ME]} · ~{HOME}"
    assert world.inner.presence[BOT] == "online"
    assert lease.presence_shown() is None  # the presence states nothing of it


async def test_with_pieces_2_and_9_on_nothing_of_the_status_goes_to_mattermost(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.6 step 1: the presence states the status the registrar's bot would have shown,
    without the link tag (the bridge marks its own bot) and without the crown."""
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    calls = _no_mattermost_status(world.inner)
    before = world.inner.status
    await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
    shown = lease.presence_shown()
    assert shown is not None
    assert shown["home"] == HOME and shown["emoji"] == WORKING and shown["text"] == f"working · proj · ~{HOME}"
    assert "⇄" not in shown["text"] and shown["crown"] is False
    assert abs(shown["until"] - (time.time() + lease.lease_sec())) < 5
    await lease.set_status(world.port, "idle")  # type: ignore[arg-type]
    assert (lease.presence_shown() or {})["emoji"] == IDLE
    await lease.set_status(world.port, "offline")  # type: ignore[arg-type]
    assert lease.presence_shown() is None  # the bye says it
    assert calls == [] and world.inner.status is before


async def test_piece_9_without_piece_2_keeps_the_status_on_the_registrar_bot(
        world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The registrar bot's status carries the link (RT-6.37) until piece 2 moves it: the old
    path, said once."""
    monkeypatch.setenv("CB_PLANB_TEST", "9")
    with caplog.at_level(logging.WARNING, logger="mattermost_ai_agent_bus.lease"):
        await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
        await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
    assert world.inner.status is not None and f"⇄{TAG[NAME_ME]}" in world.inner.status["text"]
    assert lease.presence_shown() is None
    assert sum("piece 9 is on" in r.getMessage() for r in caplog.records) == 1


async def test_a_home_not_over_nats_keeps_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    inner = FakeBus(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    await lease.set_status(inner, "working", "proj")  # type: ignore[arg-type]
    assert inner.status is not None and inner.presence[BOT] == "online"
    assert not lease.status_mirrored(inner.work_channel_name) and lease.presence_shown() is None


async def test_the_crown_is_stated_apart_and_the_bridge_decides_it(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 3: the mirror's 👑 is `crown`, never the emoji or a `coordinator` head; the
    header line of the crown that passed goes to the bridge."""
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    calls = _no_mattermost_status(world.inner)
    header = Header()
    world.port.header_line = header  # type: ignore[method-assign]
    world.fake.members.add((HOME, NAME_ME))
    await world.tick()
    # the roster is NATS's (piece 2): the own row by the NATS name, reachable without any
    # Mattermost status, so the mirror is armed with nothing written to Mattermost
    assert lease.nats_roster_on(HOME) and lease.roster_self(world.port, BOT) == NAME_ME  # type: ignore[arg-type]
    assert world.view().holder == NAME_ME and lease.crown_held(BOT)  # type: ignore[union-attr]
    shown = lease.presence_shown()
    assert shown is not None and shown["crown"] is True
    assert shown["emoji"] in natsbus.SHOWN_EMOJIS and shown["emoji"] != CROWN
    assert not shown["text"].startswith("coordinator") and shown["text"].endswith(f"~{HOME}")
    assert abs(shown["until"] - (time.time() + lease.crown_lease_sec())) < 5
    claims = len(world.fake.claimed_homes)
    await lease.nats_beat(world.port)  # type: ignore[arg-type]
    assert len(world.fake.claimed_homes) == claims and lease.crown_held(BOT)  # reachable: it keeps it
    await lease.set_status(world.port, "renew")  # type: ignore[arg-type]
    assert (lease.presence_shown() or {}).get("crown") is True
    assert header.sent and header.sent[0][0] == HOME
    assert header.sent[0][1].startswith(lease.BUS_HEADER)
    claimant(world.fake, NAME_A, 1)  # another claim is lower: the view says not mine
    await lease.set_status(world.port, "renew")  # type: ignore[arg-type]
    assert (lease.presence_shown() or {}).get("crown") is False
    assert calls == []


async def test_a_change_sends_the_presence_at_once_and_a_renewal_waits(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    _no_mattermost_status(world.inner)
    early: list[int] = []
    world.fake.presence_soon = lambda: early.append(1)  # type: ignore[attr-defined]
    await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
    assert early == [1]
    await lease.set_status(world.port, "renew")  # type: ignore[arg-type]
    assert early == [1]  # only the expiry moved: the next heartbeat carries it
    await lease.set_status(world.port, "idle")  # type: ignore[arg-type]
    assert early == [1, 1]


async def test_a_switch_that_turns_off_states_nothing_at_the_next_presence(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    _no_mattermost_status(world.inner)
    await lease.set_status(world.port, "working", "proj")  # type: ignore[arg-type]
    assert lease.presence_shown() is not None
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    assert lease.presence_shown() is None  # the way back: the registrar's bot shows it again


# --- the header line (RT-6.6 step 7) ---------------------------------------------------------------

async def test_the_header_line_goes_to_the_bridge_and_mattermost_is_not_read(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    calls = _no_mattermost_status(world.inner)
    header = Header()
    world.port.header_line = header  # type: ignore[method-assign]
    await lease._header(world.port, WORK, BOT)  # type: ignore[arg-type]
    assert header.sent == [(HOME, lease.BUS_HEADER)] and lease._header_line[WORK] == lease.BUS_HEADER
    assert calls == []


async def test_a_refused_header_line_is_tried_again(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge refuses a line from an agent the claims do not crown, or does not answer:
    nothing is recorded, the next heartbeat tries again."""
    monkeypatch.setenv("CB_PLANB_TEST", "2,9")
    _no_mattermost_status(world.inner)
    header = Header(RequestError("not the crown holder of ~home by the claims"))
    world.port.header_line = header  # type: ignore[method-assign]
    await lease._header(world.port, WORK, BOT)  # type: ignore[arg-type]
    assert WORK not in lease._header_line
    header.answer = False  # the header had the line already
    await lease._header(world.port, WORK, BOT)  # type: ignore[arg-type]
    assert len(header.sent) == 2 and lease._header_line[WORK] == lease.BUS_HEADER


async def test_with_the_switch_off_the_header_is_written_to_mattermost(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    header = Header()
    world.port.header_line = header  # type: ignore[method-assign]
    world.inner.header = "what people wrote"
    await lease._header(world.port, WORK, BOT)  # type: ignore[arg-type]
    assert world.inner.header == f"what people wrote\n\n{lease.BUS_HEADER}" and header.sent == []


# The lines and headers the bridge's header writer is checked against too (its Go test reads
# the same file): the two sides of the bus line cannot drift apart.
BUS_LINES = (Path(__file__).resolve().parents[2] / "services" / "bridge" / "internal" / "header"
             / "testdata" / "bus_lines.json")


def test_the_bus_lines_the_bridge_checks_are_the_ones_this_server_writes() -> None:
    vector = json.loads(BUS_LINES.read_text(encoding="utf-8"))
    assert vector["bus_header"] == lease.BUS_HEADER
    assert lease.BUS_HEADER == ("\U0001f68c agent bus · plain post → \U0001f451 routes it · @agent → that agent"
                                " · \U0001f64b = a question for you")
    for case in vector["lines"]:
        mandate = tuple(case["mandate"]) if case["mandate"] else None
        assert lease.bus_line(case["asks_first"], mandate) == case["line"]
    for case in vector["headers"]:
        assert lease.with_bus_header(case["header"], case["bus"]) == case["want"]


# --- the presence body (RT-6.37 step 6) ------------------------------------------------------------

def test_shown_is_bounded_as_the_bridge_reads_it() -> None:
    until = int(time.time()) + 600
    ok = {"home": "home", "emoji": WORKING, "text": "working · proj · ~home", "until": until}
    assert natsbus.clean_shown(ok) == ok
    assert natsbus.clean_shown(ok | {"crown": True}) == ok | {"crown": True}
    assert natsbus.clean_shown(ok | {"crown": "yes"}) == ok  # only True states the crown
    assert natsbus.clean_shown(ok | {"emoji": CROWN}) is None  # the crown is the bridge's
    assert natsbus.clean_shown(ok | {"emoji": "smile"}) is None
    assert natsbus.clean_shown({k: v for k, v in ok.items() if k != "home"}) is None  # shown for no home
    assert natsbus.clean_shown(ok | {"home": "Home"}) is None
    assert natsbus.clean_shown(ok | {"home": "a.b"}) is None
    assert natsbus.clean_shown(ok | {"until": True}) is None
    assert natsbus.clean_shown(ok | {"until": float(until)}) is None
    assert natsbus.clean_shown(ok | {"until": 1}) is None
    assert natsbus.clean_shown(ok | {"text": ""}) is None
    assert natsbus.clean_shown(ok | {"text": 5}) is None
    assert natsbus.clean_shown(ok | {"text": "a\nb\x00c"}) == ok | {"text": "a b c"}
    assert len((natsbus.clean_shown(ok | {"text": "x" * 300}) or {})["text"]) == natsbus.SHOWN_TEXT_MAX
    # the states' emoji are the status's own (lease writes them)
    assert natsbus.SHOWN_EMOJIS == {WORKING, IDLE, BUS, a2a.status_emoji(StatusMark.LISTENING)}


def test_the_presence_states_shown_only_when_given() -> None:
    plain = natsbus.encode_presence("online", BOT, "agent-me")
    assert "shown" not in json.loads(plain)
    assert natsbus.encode_presence("online", BOT, "agent-me", shown=None) == plain
    until = int(time.time()) + 600
    body = json.loads(natsbus.encode_presence("online", BOT, "agent-me",
                                              shown={"home": "home", "emoji": IDLE, "text": "idle", "until": until}))
    assert body["shown"] == {"home": "home", "emoji": IDLE, "text": "idle", "until": until}
    bad = json.loads(natsbus.encode_presence("online", shown={"home": "home", "emoji": CROWN, "text": "x",
                                                              "until": until}))
    assert "shown" not in bad and bad["status"] == "online"


def test_a_failing_shown_source_states_none_and_the_presence_still_goes() -> None:
    bus = NatsBus(NatsSettings(servers=("nats://127.0.0.1:1",), home="home"), FakeLogin(), "alice",
                  lambda _e: None)
    assert "shown" not in json.loads(bus._presence_body("online"))

    def broken() -> dict[str, Any]:
        raise RuntimeError("no")

    bus.shown_source = broken
    assert json.loads(bus._presence_body("online"))["status"] == "online"
    until = int(time.time()) + 60
    bus.shown_source = lambda: {"home": "home", "emoji": BUS, "text": "on the bus", "until": until, "crown": True}
    assert json.loads(bus._presence_body("online"))["shown"]["crown"] is True


# --- against a real nats-server ----------------------------------------------------------------------

async def test_an_early_presence_carries_the_shown_status(rig: Rig) -> None:
    """presence_soon sends the presence now, not after the heartbeat (30 s here)."""
    bus, _ = await rig.agent("alice", bus_kw={"heartbeat_sec": 30})
    got: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def seen(msg: Any) -> None:
        got.put_nowait(json.loads(msg.data))

    sub = await rig.admin.subscribe(f"presence.*.{bus.name}", cb=seen)
    await rig.admin.flush()
    try:
        until = int(time.time()) + 600
        bus.shown_source = lambda: {"home": "home", "emoji": WORKING, "text": "working · ~home", "until": until}
        bus.presence_soon()
        body = await asyncio.wait_for(got.get(), 5)
        assert body["status"] == "online"
        assert body["shown"] == {"home": "home", "emoji": WORKING, "text": "working · ~home", "until": until}
    finally:
        await sub.unsubscribe()


async def test_the_header_line_is_a_request_to_the_bridge(rig: Rig) -> None:
    """`<req>.header.<agent>`, answered on deliver.<agent>.req.<request id>; an error answer
    raises, so the line is tried again."""
    bus, _ = await rig.agent("alice")
    port = NatsPort(FakeBus(FakeRegistrar()), NatsSettings(servers=(rig.url,), home="home"),  # type: ignore[arg-type]
                    ("home",))
    port.use_bus(bus)
    asked: list[tuple[str, dict[str, Any]]] = []
    refuse = [False]

    async def bridge(msg: Any) -> None:
        body = json.loads(msg.data)
        asked.append((msg.subject, body))
        answer: dict[str, Any] = {"v": 1, "request": body["request"], "kind": body["kind"]}
        if refuse[0]:
            answer["error"] = "not the crown holder of ~home by the claims"
        else:
            answer["result"] = {"v": 1, "changed": True}
        await rig.admin.publish(f"deliver.{bus.name}.req.{body['request']}", json.dumps(answer).encode())

    sub = await rig.admin.subscribe("req.>", cb=bridge)
    await rig.admin.flush()
    try:
        assert await port.header_line("home", lease.BUS_HEADER) is True
        refuse[0] = True
        with pytest.raises(RequestError, match="crown holder"):
            await port.header_line("home", lease.BUS_HEADER)
    finally:
        await sub.unsubscribe()
    subject, body = asked[0]
    assert subject == f"req.header.{bus.name}" and body["kind"] == "header"
    assert body["payload"] == {"channel": "home", "line": lease.BUS_HEADER}
