"""RT-6.39 step 6 (ADR-014 phase 5, plan B piece 1): in a home over NATS the session takes its
channel ids from the channel directory, through the channel record the login service writes with
the credential (`<presence>.directory.<agent>.v1`), instead of reading them from Mattermost.

The switch is planb.on(1, channel): the channel's plan B record (RT-6.39 step 5), or
CB_PLANB_TEST="1" in a test. Unit tests of the switch and the record's decoder, and, with MM_TEST_NATS=1 and CB_NATS_SERVER, tests against a
real nats-server (the rig of test_natsbus.py) for `NatsPort.resolve` and the records' key.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeRegistrar
from test_natsbus import Rig, _login_service

from mattermost_ai_agent_bus import natsbus, planb
from mattermost_ai_agent_bus.nats_port import NatsPort, NatsPortError
from mattermost_ai_agent_bus.natsbus import MessageError, NatsSettings, decode_channel_record

DIRECTORY_ID = "d" * 26  # what the directory binds the home to; never the fake chat's WORK id


def test_the_switch_is_off_unless_the_test_variable_names_the_piece(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    assert planb.on(1, "home") is False
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    assert planb.on(1, "home") is True and planb.on(2, "home") is False
    monkeypatch.setenv("CB_PLANB_TEST", "3, 1")
    assert planb.on(1, "x") is True and planb.on(3, "x") is True and planb.on(4, "x") is False
    monkeypatch.setenv("CB_PLANB_TEST", "11,")
    assert planb.on(1, "home") is False  # piece 11 is not piece 1


def test_known_follows_the_attached_source() -> None:
    """RT-6.39 step 6: the switch is known only from a source that says it has read its records."""
    def src(_c: str) -> frozenset[int]:
        return frozenset({1})
    read = [False]
    assert planb.known() is False
    planb.attach(src)
    try:
        assert planb.known() is False  # a source without a reading of its own is never known
        planb.attach(src, lambda: read[0])
        assert planb.known() is False
        read[0] = True
        assert planb.known() is True
    finally:
        planb.detach(src)
    assert planb.known() is False


def _body(**over: Any) -> bytes:
    rec: dict[str, Any] = {"v": 1, "agent": "agent-a", "channels": {"home": DIRECTORY_ID, "guest": ""},
                           "iat": 1, "exp": 901}
    rec.update(over)
    return json.dumps(rec).encode()


def test_the_channel_record_decodes_strictly() -> None:
    assert decode_channel_record(_body(), "agent-a") == {"home": DIRECTORY_ID, "guest": ""}
    for bad in (_body(), ):
        with pytest.raises(MessageError):
            decode_channel_record(bad, "agent-b")  # another agent's
    for over in ({"v": 2}, {"channels": []}, {"channels": {"Home": DIRECTORY_ID}},
                 {"channels": {"home": "not an id"}}, {"channels": {"home": 5}}, {"iat": "x"}, {"extra": 1}):
        with pytest.raises(MessageError):
            decode_channel_record(_body(**over), "agent-a")
    with pytest.raises(MessageError):
        decode_channel_record(b"{}", "agent-a")


class NoMattermost(FakeBus):
    """A chat that fails the test at any read of a channel (or of the work channel's id)."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.reads: list[str] = []

    async def work_channel_id(self) -> str:
        self.reads.append("work_channel_id")
        return await super().work_channel_id()

    async def get_channel_id(self, channel_name: str | None = None, team_name: str | None = None) -> str:
        self.reads.append("get_channel_id")
        return await super().get_channel_id(channel_name, team_name)


def _port(rig: Rig, inner: FakeBus) -> NatsPort:
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    return NatsPort(inner, NatsSettings(servers=(rig.url,), home="home"), ("home",),  # type: ignore[arg-type]
                    bus_options={"heartbeat_sec": 0.2})


async def _record(rig: Rig, who: str, channels: dict[str, str]) -> None:
    """What the login service writes with a credential: the record, as the admin user here."""
    name = rig.names[who]
    body = json.dumps({"v": 1, "agent": name, "channels": channels, "iat": 1, "exp": 2 ** 40}).encode()
    await rig.admin.publish(f"presence.directory.{name}.v1", body)
    await rig.admin.flush()


async def test_resolve_takes_the_ids_from_the_directory_and_reads_nothing_in_mattermost(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    inner = NoMattermost(FakeRegistrar())
    port = _port(rig, inner)
    login_ = rig.logins["alice"]
    await _login_service(rig.admin, rig.names["alice"], ["home"], {})
    # the record comes while the session starts: resolve waits for it
    starting = asyncio.create_task(port.start(login_, owner="alice"))
    await asyncio.sleep(0.3)
    assert not starting.done()
    await _record(rig, "alice", {"home": DIRECTORY_ID})
    await asyncio.wait_for(starting, 15)
    try:
        assert await port.resolve() == {"home": DIRECTORY_ID}
        assert await port.nats_name(DIRECTORY_ID) == "home" and await port.nats_name(WORK) is None
        assert port.home_id == DIRECTORY_ID
        # the key the people's events and the signed records go by: the directory's id
        assert (await port._people_map()) == {"home": DIRECTORY_ID}
        assert inner.reads == []  # no Mattermost read of a channel
    finally:
        await port.stop()


async def test_resolve_fails_when_the_directory_has_no_id_and_never_asks_mattermost(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    monkeypatch.setattr("mattermost_ai_agent_bus.nats_port.CHANNEL_RECORD_WAIT_SEC", 0.5)
    inner = NoMattermost(FakeRegistrar())
    port = _port(rig, inner)
    await _login_service(rig.admin, rig.names["bob"], ["home"], {})
    await _record(rig, "bob", {"home": ""})  # the directory holds no id for the home
    bus = natsbus.NatsBus(NatsSettings(servers=(rig.url,), home="home"), rig.logins["bob"], "bob",
                          lambda _e: None, heartbeat_sec=0.2)
    port.use_bus(bus)  # type: ignore[arg-type]
    await bus.start()
    try:
        with pytest.raises(NatsPortError, match="no Mattermost id"):
            await port.resolve()
        assert inner.reads == []
    finally:
        await bus.stop()


async def test_with_the_switch_off_resolve_reads_mattermost_as_before(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    inner = NoMattermost(FakeRegistrar())
    port = _port(rig, inner)
    await _login_service(rig.admin, rig.names["carol"], ["home"], {})
    await _record(rig, "carol", {"home": DIRECTORY_ID})  # a record is ignored while the switch is off
    await port.start(rig.logins["carol"], owner="carol")
    try:
        assert await port.resolve() == {"home": WORK}
        assert port.home_id == WORK and "work_channel_id" in inner.reads
    finally:
        await port.stop()


def test_the_record_limit_is_the_login_services_grant() -> None:
    """The home, the guests and the lobby: callout.MaxGuests + 2 (services/auth/internal/callout/service.go)."""
    src = (Path(__file__).resolve().parents[2] / "services/auth/internal/callout/service.go").read_text()
    m = re.search(r"MaxGuests\s*=\s*(\d+)", src)
    assert m is not None
    assert natsbus.MAX_GUESTS == int(m.group(1))
    assert natsbus.MAX_CHANNEL_RECORD == int(m.group(1)) + 2 == 18
    chans = {f"c{i}": "" for i in range(18)}
    assert len(decode_channel_record(_body(channels=chans), "agent-a")) == 18
    with pytest.raises(MessageError):
        decode_channel_record(_body(channels={f"c{i}": "" for i in range(19)}), "agent-a")


async def test_resolve_before_the_switch_is_known_keeps_no_mattermost_ids(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real switch is known only once the bus is up: before, resolve reads Mattermost for
    the answer but caches nothing; after the bus is up it caches as before."""
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    monkeypatch.setattr(planb, "known", lambda: False)
    inner = NoMattermost(FakeRegistrar())
    port = _port(rig, inner)
    assert await port.resolve() == {"home": WORK}
    assert port._ids is None  # nothing cached: the directory's ids may be the ones to use
    await _login_service(rig.admin, rig.names["alice"], ["home"], {})
    await port.start(rig.logins["alice"], owner="alice")  # switch off once read: Mattermost's ids, kept
    try:
        assert port._ids == {"home": WORK} and port.home_id == WORK
    finally:
        await port.stop()


async def test_a_record_past_its_exp_is_ignored(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    await _login_service(rig.admin, rig.names["alice"], ["home"], {})
    name = rig.names["alice"]
    old = json.dumps({"v": 1, "agent": name, "channels": {"home": DIRECTORY_ID}, "iat": 1, "exp": 5}).encode()
    await rig.admin.publish(f"presence.directory.{name}.v1", old)
    await rig.admin.flush()
    bus = natsbus.NatsBus(NatsSettings(servers=(rig.url,), home="home"), rig.logins["alice"], "alice",
                          lambda _e: None, heartbeat_sec=0.2)
    await bus.start()
    try:
        assert await bus.wait_channel_ids(1.0) is None
        await _record(rig, "alice", {"home": DIRECTORY_ID})
        assert await bus.wait_channel_ids(10.0) == {"home": DIRECTORY_ID}
    finally:
        await bus.stop()


async def test_the_real_switch_turns_piece_1_on_once_the_bus_has_read_it(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.39 step 6 by the switch of step 5, not CB_PLANB_TEST: the channel's plan B record
    names piece 1, the start waits until the bus has read its records (planb.known) and then
    takes the home's id from the channel record, with no Mattermost read."""
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    assert planb.known() is False  # no bus: nothing read
    await rig.admin.publish("presence.planb.home.v1", json.dumps({"v": 1, "channel": "home", "pieces": [1]}).encode())
    await _record(rig, "alice", {"home": DIRECTORY_ID})
    inner = NoMattermost(FakeRegistrar())
    port = _port(rig, inner)
    await _login_service(rig.admin, rig.names["alice"], ["home"], {})
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 15)
    try:
        assert planb.known() is True and planb.on(1, "home") is True
        assert await port.resolve() == {"home": DIRECTORY_ID} and port.home_id == DIRECTORY_ID
        assert inner.reads == []
    finally:
        await port.stop()
    assert planb.known() is False  # the stopped bus detached its source

