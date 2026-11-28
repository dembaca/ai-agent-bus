"""Plan B piece 14a, the decision (ADR-014; RT-6.18 step 13, RT-6.10 step 8): a session whose
every channel - the home, each guest, the lobby and the stats channel - runs over NATS with every
plan B piece on registers no Mattermost bot and holds no token; with any channel or piece off, or
when the records cannot be read, it registers as before. The host process decides for a corral
session preStart started without a bot (nobot.host_decide), the MCP server's session_start
without corral (nobot.decide); both read NatsPort.bot_needed.

Unit tests on a fake bus, and, with MM_TEST_NATS=1 and CB_NATS_SERVER, against a real nats-server
whose records the test publishes as the login service does, with a registrar that records calls.
"""

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBus, FakeRegistrar
from test_natsbus import _login_service
from test_planb_piece12 import rig  # noqa: F401 — the real nats-server's fixture

from mattermost_ai_agent_bus import compose, hosted, nobot, sandbox, server, stats
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.nats_port import NatsPort
from mattermost_ai_agent_bus.natsbus import NatsSettings

ALL = frozenset(range(1, 14))
CHANNELS = ("home", "guest", "lobby")


@dataclass
class Bus:
    """What bot_needed reads of a natsbus.NatsBus: its channels, whether it is connected and
    has read the presence replay, and the plan B pieces of each channel's record (as the bus
    holds them: a stale record reads as none)."""

    pieces: dict[str, frozenset[int]] = field(default_factory=lambda: {c: ALL for c in CHANNELS})
    channels: list[str] = field(default_factory=lambda: list(CHANNELS))
    connected: bool = True
    synced: bool = True
    linked: list[tuple[str, str]] = field(default_factory=list)
    dm_source: Any = None
    shown_source: Any = None

    def planb_pieces(self, channel: str) -> frozenset[int]:
        return self.pieces.get(channel, frozenset()) if channel in self.channels else frozenset()

    def link_bot(self, bot_id: str, username: str = "") -> None:
        self.linked.append((bot_id, username))

    def presence_soon(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    nobot.reset()
    monkeypatch.setenv("CB_NATS_HOMES", "home guest lobby")
    monkeypatch.delenv("CB_MM_STATS_CHANNEL", raising=False)
    monkeypatch.delenv("MM_STATS_CHANNEL", raising=False)
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    yield
    nobot.reset()
    hosted._defer = None


def _inner() -> FakeBus:
    inner = FakeBus(FakeRegistrar())
    inner.token = None  # no bot yet
    inner.work_channel_name = "home"
    inner.guest_channel_names = ["guest"]  # type: ignore[attr-defined]
    inner.channel_name = "lobby"
    return inner


def _port(bus: Bus | None = None, inner: FakeBus | None = None) -> tuple[NatsPort, FakeBus, Bus]:
    inner = inner or _inner()
    settings = NatsSettings(servers=("nats://127.0.0.1:1",), home="home", guests=("guest",), lobby=True,
                            lobby_channel="lobby")
    port = NatsPort(inner, settings, ("home",))  # type: ignore[arg-type]
    port.bot_deferred = nobot.deferrable(port)
    bus = bus or Bus()
    port.use_bus(bus)  # type: ignore[arg-type]
    return port, inner, bus


# --- the predicate ---------------------------------------------------------------------------

def test_every_channel_with_every_piece_on_needs_no_bot() -> None:
    """RT-6.18 step 13: home, guest and lobby over NATS, pieces 1 to 13 in each record."""
    port, _, _ = _port()
    assert port.bot_deferred
    assert port.bot_needed() == ""


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("piece", sorted(ALL))
def test_any_single_piece_off_in_any_channel_needs_a_bot(channel: str, piece: int) -> None:
    bus = Bus()
    bus.pieces[channel] = ALL - {piece}
    port, _, _ = _port(bus)
    assert port.bot_needed() == f"plan B piece {piece} is off in ~{channel}"


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_channel_not_over_nats_needs_a_bot(channel: str) -> None:
    bus = Bus()
    bus.channels.remove(channel)  # not asked for, or not granted by the login service
    port, _, _ = _port(bus)
    assert port.bot_needed() == f"~{channel} does not run over NATS"


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_stale_or_missing_record_needs_a_bot(channel: str) -> None:
    bus = Bus()
    del bus.pieces[channel]  # planb_pieces reads a stale record (2 h) as none
    port, _, _ = _port(bus)
    assert port.bot_needed().startswith("plan B piece 1 is off")


@pytest.mark.parametrize("attr", ["connected", "synced"])
def test_records_not_read_needs_a_bot(attr: str) -> None:
    bus = Bus()
    setattr(bus, attr, False)
    port, _, _ = _port(bus)
    assert port.bot_needed() == NatsPort.NOT_READ


def test_no_bus_yet_needs_a_bot() -> None:
    inner = _inner()
    port = NatsPort(inner, NatsSettings(servers=("nats://127.0.0.1:1",), home="home"), ("home",))  # type: ignore[arg-type]
    assert port.bot_needed() == NatsPort.NOT_READ


def test_the_environment_switch_never_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """CB_PLANB_TEST turns pieces on for planb.on, never for the decision: the records alone."""
    monkeypatch.setenv("CB_PLANB_TEST", ",".join(str(n) for n in sorted(ALL)))
    bus = Bus()
    bus.pieces["guest"] = frozenset()
    port, _, _ = _port(bus)
    assert port.bot_needed() == "plan B piece 1 is off in ~guest"


def test_a_stats_channel_on_mattermost_needs_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stats channel counts as over NATS through the home's piece 13 (STATS written to NATS
    alone); bot_needed checks the home's pieces first, so it names that piece."""
    monkeypatch.setenv("CB_MM_STATS_CHANNEL", "agents-stats")
    bus = Bus()
    bus.pieces["home"] = ALL - {13}
    port, _, _ = _port(bus)
    assert port.bot_needed() == "plan B piece 13 is off in ~home"
    assert stats.channel() == "agents-stats"


# --- who may wait for the decision -----------------------------------------------------------

def test_a_channel_not_listed_in_cb_nats_homes_is_not_deferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_NATS_HOMES", "home lobby")
    port, _, _ = _port()
    assert not port.bot_deferred and not nobot.deferrable(port)


def test_a_session_with_a_bot_is_not_deferred() -> None:
    inner = _inner()
    inner.token = "t"
    port, _, _ = _port(inner=inner)
    assert not port.bot_deferred


async def test_a_deferred_bus_starts_without_the_bots_id() -> None:
    port, inner, _ = _port()
    assert await port._bot_id() == ""
    inner.token = "t"
    port.bot_deferred = False
    assert await port._bot_id() == inner.registrar.info.user_id  # the old path, as before


# --- the decision ----------------------------------------------------------------------------

async def test_decide_all_on_holds_and_keeps_it() -> None:
    port, inner, bus = _port()
    assert await nobot.decide(port, 0) == ""
    assert nobot.held()
    bus.pieces["guest"] = frozenset()  # a record that goes off later changes nothing
    assert await nobot.decide(port, 0) == ""
    assert inner.registrar.calls == []


async def test_decide_waits_for_the_records_then_registers_in_doubt() -> None:
    bus = Bus(synced=False)
    port, _, _ = _port(bus)
    assert await nobot.decide(port, 0.3) == NatsPort.NOT_READ
    assert not nobot.held()


async def test_decide_waits_until_the_bus_has_read_its_records() -> None:
    bus = Bus(synced=False)
    port, _, _ = _port(bus)

    async def later() -> None:
        await asyncio.sleep(0.2)
        bus.synced = True

    task = asyncio.create_task(later())
    assert await nobot.decide(port, 5) == ""
    await task


async def test_decide_without_a_nats_side_registers() -> None:
    assert await nobot.decide(_inner(), 0) == "no channel runs over NATS"


# --- the host process (RT-6.18 step 13) -------------------------------------------------------

async def test_the_host_process_registers_nothing_when_all_is_on() -> None:
    port, inner, _ = _port()
    out = await nobot.host_decide(port, "host-proj-abc", "Claude Code", "the-secret", timeout=0)
    assert out.startswith("no bot")
    assert inner.registrar.calls == [] and inner.registrar.session is None
    assert inner.token is None


@pytest.mark.parametrize("piece", [1, 7, 13])
async def test_the_host_process_registers_as_before_with_a_piece_off(piece: int) -> None:
    bus = Bus()
    bus.pieces["lobby"] = ALL - {piece}
    port, inner, _ = _port(bus)
    inner.reg_secret = None  # type: ignore[attr-defined]
    out = await nobot.host_decide(port, "host-proj-abc", "Claude Code", "the-secret", timeout=0)
    assert out == f"registered (plan B piece {piece} is off in ~lobby)"
    assert inner.registrar.calls == ["session_start host-proj-abc", "finish_session_setup"]
    assert inner.reg_secret is None  # type: ignore[attr-defined] — the secret is not kept
    assert bus.linked == [(inner.registrar.info.user_id, inner.registrar.info.username)]
    assert not nobot.held()


async def test_the_host_process_registers_when_it_cannot_decide() -> None:
    port, inner, _ = _port(Bus(connected=False))
    out = await nobot.host_decide(port, "host-proj-abc", "", "s", timeout=0.2)
    assert out == f"registered ({NatsPort.NOT_READ})"
    assert inner.registrar.calls[0] == "session_start host-proj-abc"


def test_the_start_line_names_the_bot_to_defer_and_the_secret() -> None:
    line = {"key": "k" * 32, "env": {"MM_REG_SECRET": "x"},
            "defer": {"name": "host-proj-abc", "display": "Claude Code · proj", "secret": "the-secret"}}
    key, env = hosted.read_start(io.BytesIO(json.dumps(line).encode() + b"\n"))
    assert "MM_REG_SECRET" not in env  # never in the environment
    assert hosted._defer == ("host-proj-abc", "Claude Code · proj", "the-secret")
    hosted.read_start(io.BytesIO(json.dumps({"key": "k" * 32, "env": {}}).encode() + b"\n"))
    assert hosted._defer is None  # without "defer": the bot came from preStart, as before


@pytest.mark.parametrize("defer", [
    "x", {"name": "A!", "secret": "s"}, {"name": "host-proj-abc"}, {"name": "host-proj-abc", "secret": "a\nb"},
    {"name": "host-proj-abc", "secret": "s", "display": "two\nlines"}])
def test_a_malformed_defer_is_refused(defer: Any) -> None:
    line = {"key": "k" * 32, "env": {}, "defer": defer}
    with pytest.raises(ValueError, match="defer"):
        hosted.read_start(io.BytesIO(json.dumps(line).encode() + b"\n"))


async def test_the_host_hands_the_start_lines_defer_to_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """hosted._decide, the task services() starts beside the bus with the start line's defer."""
    port, inner, _ = _port()
    seen: list[tuple[str, str, str]] = []

    async def fake(client: Any, name: str, display: str, secret: str, timeout: float = 0) -> str:
        seen.append((name, display, secret))
        return "no bot"

    monkeypatch.setattr(nobot, "host_decide", fake)
    await hosted._decide(port, ("host-proj-abc", "d", "s"))  # type: ignore[arg-type]
    assert seen == [("host-proj-abc", "d", "s")]


# --- without the bot: what needs Mattermost fails closed --------------------------------------

async def test_without_a_bot_mattermost_calls_fail_with_the_reason() -> None:
    adapter = MattermostAdapter(base_url="https://mm.example")
    nobot.hold(adapter)
    with pytest.raises(RuntimeError, match="restart the session"):
        adapter._auth_headers()
    with pytest.raises(RuntimeError, match="no Mattermost bot"):
        await adapter.session_start("x")


async def test_with_a_bot_the_old_error_is_unchanged() -> None:
    adapter = MattermostAdapter(base_url="https://mm.example")
    with pytest.raises(RuntimeError, match=r"bot token required \(session_start or CB_MM_BOT_TOKEN\)"):
        adapter._auth_headers()


# --- the MCP server without corral (RT-6.10 step 8) -------------------------------------------

@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name, value in (("STALE", ""), ("_client", None), ("_inbox", None), ("_client_named", True),
                        ("_adopt_tried", True), ("_roots_pending", False)):
        monkeypatch.setattr(server, name, value)
    for marker in sandbox.SANDBOX_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr(stats, "_uid", "")
    monkeypatch.setattr(nobot, "DECIDE_SEC", 0.2)

    def use(port: NatsPort) -> None:
        monkeypatch.setattr(server, "_client", compose.Remembered(port))  # type: ignore[arg-type]

    return use


async def test_session_start_registers_no_bot_when_all_is_on(mcp: Any) -> None:
    port, inner, _ = _port()
    mcp(port)
    out = json.loads(await server.session_start("x"))
    assert out["registered"] is False and "no Mattermost bot" in out["no_bot"]
    assert inner.registrar.calls == []
    again = json.loads(await server.session_start("x"))  # kept for the session
    assert again["registered"] is False and inner.registrar.calls == []


    async def no_token() -> Any:  # as the adapter without a token (client._auth_headers)
        raise RuntimeError(nobot.reason())

    inner.get_me = no_token  # type: ignore[method-assign]
    info = json.loads(await server.bus_info())
    assert info["on_bus"] is True and info["bot"] is None and info["home"] == "home"


async def test_session_start_registers_as_before_with_a_piece_off(mcp: Any) -> None:
    bus = Bus()
    bus.pieces["guest"] = ALL - {5}
    port, inner, _ = _port(bus)
    mcp(port)
    out = json.loads(await server.session_start("x"))
    assert out["user_id"] == inner.registrar.info.user_id
    assert inner.registrar.calls == ["session_start x", "finish_session_setup"]
    assert bus.linked == [(inner.registrar.info.user_id, inner.registrar.info.username)]


async def test_session_start_registers_when_the_records_are_not_read(mcp: Any) -> None:
    port, inner, _ = _port(Bus(synced=False))
    mcp(port)
    json.loads(await server.session_start("x"))
    assert inner.registrar.calls == ["session_start x", "finish_session_setup"]


async def test_session_start_without_the_opt_in_does_not_wait(mcp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every install today: a channel not in CB_NATS_HOMES registers at once, as before 14a."""
    monkeypatch.setenv("CB_NATS_HOMES", "home")
    monkeypatch.setattr(nobot, "DECIDE_SEC", 30.0)
    port, inner, _ = _port()  # all on, but not opted in
    mcp(port)
    await asyncio.wait_for(server.session_start("x"), 5)
    assert inner.registrar.calls == ["session_start x", "finish_session_setup"]
    assert not nobot.held()


# --- against a real nats-server (MM_TEST_NATS=1) ----------------------------------------------

async def _real(rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,  # noqa: F811
                pieces: dict[str, list[int]]) -> tuple[NatsPort, FakeBus]:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for ch, ps in pieces.items():
        await rig.admin.publish(f"presence.planb.{ch}.v1", json.dumps({"v": 1, "channel": ch, "pieces": ps}).encode())
    name = rig.names["alice"]
    await rig.admin.publish(f"presence.directory.{name}.v1", json.dumps(
        {"v": 1, "agent": name, "channels": {"home": "h" * 26, "guest": "g" * 26, "lobby": "l" * 26},
         "iat": 1, "exp": 2 ** 40}).encode())
    await rig.admin.flush()
    await _login_service(rig.admin, name, list(CHANNELS), {})
    inner = _inner()
    settings = NatsSettings(servers=(rig.url,), home="home", guests=("guest",), lobby=True, lobby_channel="lobby")
    port = NatsPort(inner, settings, ("home",), bus_options={"heartbeat_sec": 0.2})  # type: ignore[arg-type]
    port.bot_deferred = nobot.deferrable(port)
    assert port.bot_deferred
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 20)
    return port, inner


async def test_real_nats_all_on_registers_nothing(rig: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                  tmp_path: Path) -> None:
    port, inner = await _real(rig, monkeypatch, tmp_path, {c: sorted(ALL) for c in CHANNELS})
    try:
        out = await nobot.host_decide(port, "host-proj-abc", "d", "the-secret", timeout=10)
        assert out.startswith("no bot"), out
        assert inner.registrar.calls == [] and inner.token is None
    finally:
        await port.stop()


async def test_real_nats_one_piece_off_registers_as_before(rig: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                           tmp_path: Path) -> None:
    pieces = {c: sorted(ALL) for c in CHANNELS}
    pieces["guest"] = sorted(ALL - {7})
    port, inner = await _real(rig, monkeypatch, tmp_path, pieces)
    try:
        out = await nobot.host_decide(port, "host-proj-abc", "d", "the-secret", timeout=10)
        assert out == "registered (plan B piece 7 is off in ~guest)", out
        assert inner.registrar.calls == ["session_start host-proj-abc", "finish_session_setup"]
    finally:
        await port.stop()


async def test_real_nats_no_record_registers_as_before(rig: Any, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
                                                       tmp_path: Path) -> None:
    port, inner = await _real(rig, monkeypatch, tmp_path, {"home": sorted(ALL), "guest": sorted(ALL)})
    try:
        out = await nobot.host_decide(port, "host-proj-abc", "d", "the-secret", timeout=10)
        assert out == "registered (plan B piece 1 is off in ~lobby)", out
        assert inner.registrar.calls[0] == "session_start host-proj-abc"
    finally:
        await port.stop()


# --- the host process's decision: open, failed, cancelled; the bot in the session file ---------

async def test_until_the_host_process_decides_what_needs_mattermost_says_so() -> None:
    port, inner, _ = _port(Bus(connected=False))
    task = asyncio.create_task(nobot.host_decide(port, "host-proj-abc", "", "the-secret", timeout=5))
    await asyncio.sleep(0.05)
    assert nobot.status() == nobot.WAITING and inner.no_bot == nobot.WAITING  # type: ignore[attr-defined]
    assert "not taken yet, up to 60 s after the start" in nobot.WAITING
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert inner.reg_secret is None  # type: ignore[attr-defined] — dropped on a cancel (SIGTERM)


async def test_a_cancel_during_the_registration_drops_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.pieces["home"] = ALL - {3}
    port, inner, _ = _port(bus)
    started = asyncio.Event()
    seen: list[Any] = []

    async def slow(name: str, display_name: str | None = None, *, unique: bool = True) -> Any:
        seen.append(inner.reg_secret)  # type: ignore[attr-defined]
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(inner.registrar, "session_start", slow)
    task = asyncio.create_task(nobot.host_decide(port, "host-proj-abc", "", "the-secret", timeout=0))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()  # what services() does on SIGTERM (hosted.services' finally)
    await asyncio.gather(task, return_exceptions=True)
    assert seen == ["the-secret"] and inner.reg_secret is None  # type: ignore[attr-defined]


async def test_a_failed_registration_says_so_and_drops_the_secret(mcp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bus = Bus()
    bus.pieces["guest"] = ALL - {5}
    port, inner, _ = _port(bus)

    async def refused(name: str, display_name: str | None = None, *, unique: bool = True) -> Any:
        raise RuntimeError("registrar refused: http=503")

    monkeypatch.setattr(inner.registrar, "session_start", refused)
    out = await nobot.host_decide(port, "host-proj-abc", "", "the-secret", timeout=0)
    assert out == "registration failed (RuntimeError)"
    assert inner.reg_secret is None  # type: ignore[attr-defined]
    assert "registering the bot failed" in nobot.status() and "restart the session" in nobot.status()
    assert inner.no_bot == nobot.status()  # type: ignore[attr-defined] — the routes' error text
    assert not nobot.held()  # not a decision without a bot

    adapter = MattermostAdapter(base_url="https://mm.example")
    adapter.no_bot = nobot.status()
    with pytest.raises(RuntimeError, match="registering the bot failed"):
        adapter._auth_headers()

    async def no_token() -> Any:  # as the adapter without a token (client._auth_headers)
        raise RuntimeError(inner.no_bot)  # type: ignore[attr-defined]

    inner.get_me = no_token  # type: ignore[method-assign]
    mcp(port)
    info = json.loads(await server.bus_info())
    assert info["on_bus"] is True and info["bot"] is None
    assert "registering the bot failed" in info["no_bot"]


async def test_bus_info_without_a_client_is_off_the_bus(mcp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    nobot.hold(MattermostAdapter(base_url="https://mm.example"))

    def stale() -> Any:
        raise RuntimeError("a stale copy")

    monkeypatch.setattr(server, "_get_client", stale)
    info = json.loads(await server.bus_info())
    assert info["on_bus"] is False and info["reason"] == "a stale copy"


def _corral_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from mattermost_ai_agent_bus import session_file

    hosted_dir = tmp_path / "hosted" / "sess-x"
    hosted_dir.mkdir(parents=True)
    monkeypatch.setattr(session_file, "_hosted_dir", hosted_dir)
    f = tmp_path / "sess-x.env"
    f.write_text("MM_REG_BOT_NAME=host-proj-abc\nCB_MM_BOT_TOKEN=\nMM_BOT_TOKEN=\nDEFERRED=1\n")
    f.chmod(0o600)
    return f


async def test_the_bot_the_host_process_registers_goes_into_the_session_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = _corral_file(tmp_path, monkeypatch)
    bus = Bus()
    bus.pieces["lobby"] = ALL - {2}
    port, inner, _ = _port(bus)
    out = await nobot.host_decide(port, "host-proj-abc", "", "the-secret", timeout=0)
    assert out.startswith("registered")
    lines = f.read_text().splitlines()
    info = inner.registrar.info
    # The last line of a key wins (mm_kv): the stop hook and the sweep unregister with it.
    assert [ln for ln in lines if ln.startswith("CB_MM_BOT_TOKEN=")][-1] == f"CB_MM_BOT_TOKEN={info.bot_token}"
    assert f"MM_BOT_USER_ID={info.user_id}" in lines and "DEFERRED=1" in lines
    assert "the-secret" not in f.read_text()
    assert f.stat().st_mode & 0o777 == 0o600
    assert nobot.status() == ""


async def test_no_session_file_or_a_symlink_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = _corral_file(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere.env"
    target.write_text("")
    f.unlink()
    f.symlink_to(target)
    nobot.record_token(FakeRegistrar().info)
    assert target.read_text() == ""
    f.unlink()
    nobot.record_token(FakeRegistrar().info)
    assert not f.exists()


async def test_record_token_does_not_create_a_file_the_stop_hook_removed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.18 step 13: the file removed between the check and the append is not created again."""
    f = _corral_file(tmp_path, monkeypatch)
    real = Path.is_file

    def removed_after_check(self: Path) -> bool:
        ok = real(self)
        if self == f:
            f.unlink()
        return ok

    monkeypatch.setattr(Path, "is_file", removed_after_check)
    nobot.record_token(FakeRegistrar().info)
    monkeypatch.setattr(Path, "is_file", real)
    assert not f.exists()
    from mattermost_ai_agent_bus import session_file

    with pytest.raises(FileNotFoundError):
        session_file.append_private(f, "x\n", create=False)
    assert not f.exists()
    session_file.append_private(f, "x\n")  # the others still create
    assert f.read_text() == "x\n"


async def test_a_route_during_the_registration_says_it_is_being_registered(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.18 step 13: no_bot holds the registering text while the bot is registered, then clears."""
    bus = Bus()
    bus.pieces["guest"] = ALL - {5}
    port, inner, _ = _port(bus)
    seen: dict[str, Any] = {}

    async def slow(name: str, display_name: str | None = None, *, unique: bool = True) -> Any:
        seen["no_bot"] = inner.no_bot  # type: ignore[attr-defined]
        seen["registering"] = inner.registering  # type: ignore[attr-defined]
        adapter = MattermostAdapter(base_url="https://mm.example")
        adapter.no_bot = inner.no_bot  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="a Mattermost bot is being registered, try again in a moment"):
            adapter._auth_headers()
        return FakeRegistrar().info

    monkeypatch.setattr(inner.registrar, "session_start", slow)  # type: ignore[attr-defined]
    out = await nobot.host_decide(port, "host-proj-abc", "", "the-secret", timeout=0)
    assert out.startswith("registered"), out
    assert seen["no_bot"] == nobot.REGISTERING and seen["registering"] is True
    assert inner.no_bot == "" and inner.registering is False  # type: ignore[attr-defined]
    assert nobot.status() == ""
