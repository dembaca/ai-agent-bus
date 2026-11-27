"""RT-6.22 step 7 (ADR-014 phase 5, plan B piece 12): a guest channel or the lobby whose plan B
switch for piece 12 is on (`planb.on(12, channel)`, RT-6.39 step 5) runs over NATS like a home:
its id comes from the credential's channel record (`<presence>.directory.<agent>.v1`), nothing is
joined, read or posted in Mattermost for it, and its wake rules (RT-6.22 step 3) apply to the
posts that come over NATS. Off, nothing changes; a guest that stays on Mattermost stays as it is;
one switched on that the record names no id for is not reachable (fail closed).

Unit tests of the settings, and, with MM_TEST_NATS=1 and CB_NATS_SERVER, tests against a real
nats-server whose users hold the login service's rights for a home, a guest and the lobby.
"""

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeEvents, FakeRegistrar
from nats.aio.client import Client as NatsClient
from test_natsbus import (BUS_STREAM, PRESENCE_STREAM, FakeLogin, Rig, _api, _free_port,
                          _login_service, _server_binary)

from mattermost_ai_agent_bus import compose, homes, natsbus, planb
from mattermost_ai_agent_bus.events import MembershipEvent, PostEvent
from mattermost_ai_agent_bus.model import Post, UnreadChannel
from mattermost_ai_agent_bus.nats_port import NatsPort, NatsPortError
from mattermost_ai_agent_bus.natsbus import NatsSettings, agent_name
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

GUEST_ID = "g" * 26  # what the directory binds ~guest to
LOBBY_ID = "l" * 26  # ... and the lobby
MM_GUEST = "m" * 26  # a guest that stays on Mattermost (the bot's membership there)
CHANNELS = ("home", "guest", "lobby")


# --- the settings: the hello asks for the guests and the lobby --------------------------------------

def test_the_hello_asks_for_a_guest_or_the_lobby_only_when_cb_nats_homes_lists_it() -> None:
    env = {"CB_NATS_URL": "nats://127.0.0.1:4222",
           "CB_NATS_HOMES": "home, ~Guest, b-2, town-square, unasked"}
    s = compose.nats_settings_from_env("home", "town-square", env,
                                       guests=["~Guest", "guest", "home", "town-square", "Bad Name", "b-2", "other"])
    assert s is not None
    assert s.guests == ("guest", "b-2")  # listed, bus tokens only, no home, no lobby, once each
    assert s.lobby is True and s.lobby_channel == "town-square"
    assert NatsBusChannels(s) == ["home", "guest", "b-2", "town-square"]
    alone = compose.nats_settings_from_env("home", "home", env)
    assert alone is not None and alone.guests == () and alone.lobby is False  # the lobby is the home
    many = compose.nats_settings_from_env("home", "", env | {"CB_NATS_HOMES": " ".join(f"g{i}" for i in range(30))},
                                          guests=[f"g{i}" for i in range(30)])
    assert many is not None and len(many.guests) == 16 and many.lobby is False  # callout.MaxGuests


def test_with_cb_nats_homes_naming_the_home_alone_the_hello_is_unchanged() -> None:
    """Off is byte-identical: no guest, no lobby in the hello unless CB_NATS_HOMES lists them."""
    env = {"CB_NATS_URL": "nats://127.0.0.1:4222", "CB_NATS_HOMES": "home"}
    s = compose.nats_settings_from_env("home", "town-square", env, guests=["guest", "b-2"])
    assert s is not None and s.guests == () and s.lobby is False
    before = NatsSettings(servers=("nats://127.0.0.1:4222",), home="home", lobby_channel="town-square")
    assert s == before
    since: dict[str, int] = {}
    assert natsbus.build_hello("tok", s.home, s.guests, s.lobby, since) == '{"token":"tok","home":"home"}'


def NatsBusChannels(s: NatsSettings) -> list[str]:  # noqa: N802 — what NatsBus.channels makes of them
    from mattermost_ai_agent_bus.natsbus import NatsBus
    return NatsBus(s, FakeLogin(), "alice", lambda _e: None).channels


# --- the real server ----------------------------------------------------------------------------------

@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    """test_natsbus's rig, with the rights callout/perms.go grants for a home, a guest and the lobby."""
    if os.environ.get("MM_TEST_NATS") != "1":
        pytest.skip("MM_TEST_NATS=1 runs the tests against a real nats-server")
    binary = _server_binary()
    if not binary or not Path(binary).exists():
        pytest.fail("nats-server 2.15.3 not found: set CB_NATS_SERVER=<path> or put it on PATH")
    logins = {"alice": FakeLogin(), "bob": FakeLogin(), "carol": FakeLogin()}
    names = {w: agent_name(w, lg.nkey().public_key) for w, lg in logins.items()}
    admin_pw = os.urandom(8).hex()
    users = [f'{{user: admin, password: "{admin_pw}"}}']
    for w, lg in logins.items():
        n = names[w]
        pub = [f'"bus.ch.{c}.{n}", "presence.member.{c}.{n}"' for c in CHANNELS]
        users.append(f'{{nkey: "{lg.nkey().public_key}", permissions: {{publish: {{allow: [{", ".join(pub)}, '
                     f'"presence.crown.home.{n}", "bus.dm.*.{n}", "req.*.{n}", "presence.{w}.{n}"]}}, '
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


TRACKED = ("get_channel_id", "join_channel", "get_channel", "lobby_channel_id", "list_recent",
           "channel_posts", "posts_after", "post", "thread", "add_reaction", "reactions",
           "guest_channel_ids", "member_channels", "is_member", "channel_users")


class Watched(FakeBus):
    """A chat that logs every channel call, to tell which named a guest or the lobby."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.log: list[str] = []
        for n in TRACKED:
            orig = getattr(self, n)

            async def wrapped(*args: Any, _n: str = n, _o: Any = orig, **kwargs: Any) -> Any:
                self.log.append(f"{_n} {args!r} {kwargs!r}")
                return await _o(*args, **kwargs)

            setattr(self, n, wrapped)

    def touched(self) -> list[str]:
        """The calls that named the guest or the lobby, by name or id."""
        return [c for c in self.log if any(x in c for x in (GUEST_ID, LOBBY_ID, "'guest'", "'lobby'"))]


def _port(rig: Rig, inner: FakeBus) -> NatsPort:
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    inner.channel_name = "lobby"  # CB_MM_CHANNEL
    # what compose asks for with CB_NATS_HOMES="home guest mmonly lobby" (the opt-in)
    settings = NatsSettings(servers=(rig.url,), home="home", guests=("guest", "mmonly"), lobby=True,
                            lobby_channel="lobby")
    return NatsPort(inner, settings, ("home",), bus_options={"heartbeat_sec": 0.2})  # type: ignore[arg-type]


async def _setup(rig: Rig, monkeypatch: pytest.MonkeyPatch, pieces: dict[str, list[int]],
                 record: dict[str, str]) -> tuple[NatsPort, Watched, Any, list[Any]]:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    for ch, ps in pieces.items():
        await rig.admin.publish(f"presence.planb.{ch}.v1", json.dumps({"v": 1, "channel": ch, "pieces": ps}).encode())
    name = rig.names["alice"]
    await rig.admin.publish(f"presence.directory.{name}.v1", json.dumps(
        {"v": 1, "agent": name, "channels": record, "iat": 1, "exp": 2 ** 40}).encode())
    await rig.admin.flush()
    inner = Watched(FakeRegistrar())
    inner.guests = {MM_GUEST}
    inner.channels = [UnreadChannel(MM_GUEST, "O", name="mmonly")]
    inner.channel_ids = {"mmonly": MM_GUEST}
    port = _port(rig, inner)
    await _login_service(rig.admin, name, list(CHANNELS), {})
    bob, bob_events = await rig.agent("bob", ["guest", "lobby"])  # bob's home is ~guest
    return port, inner, bob, bob_events


async def _until(cond: Any, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.05)


def _posts(q: "asyncio.Queue[Any]") -> list[Post]:
    out = []
    while not q.empty():
        item = q.get_nowait()
        if isinstance(item, PostEvent):
            out.append(item.post)
    return out


async def test_with_the_switch_on_a_guest_and_the_lobby_run_over_nats_with_no_mattermost_call(
        rig: Rig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """RT-6.22 step 7: ids from the channel record, no join, posts and reads over NATS, the
    membership announced, the inbox told; not one Mattermost call names the guest or the lobby."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    port, inner, bob, bob_events = await _setup(
        rig, monkeypatch, {"guest": [12], "lobby": [12]}, {"home": "", "guest": GUEST_ID, "lobby": LOBBY_ID})
    q = port.subscribe()
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 15)
    me = rig.names["alice"]
    try:
        assert planb.on(12, "guest") and planb.on(12, "lobby") and not planb.on(12, "home")
        assert await port.resolve() == {"home": WORK, "guest": GUEST_ID, "lobby": LOBBY_ID}
        # the inbox is told to read its channel lists again (RT-6.22 step 1)
        assert any(isinstance(e, MembershipEvent) and e.user_ids == (inner.bot_id,)
                   for e in list(q._queue))  # type: ignore[attr-defined]
        assert await port.get_channel_id("guest") == GUEST_ID
        assert await port.join_channel("~Guest") == GUEST_ID  # nothing joined in Mattermost
        assert await port.lobby_channel_id() == LOBBY_ID
        assert (await port.get_channel(GUEST_ID)).name == "guest"
        assert await homes.guest_ids(port) == {GUEST_ID, MM_GUEST}  # type: ignore[arg-type]
        assert await homes.lobby_ids(port) == {LOBBY_ID}  # type: ignore[arg-type]
        assert {GUEST_ID, LOBBY_ID} <= await homes.watched_channel_ids(port)  # type: ignore[arg-type]
        rows = {r.channel_id: r for r in await port.member_channels()}
        assert rows[GUEST_ID].nats and rows[LOBBY_ID].nats and not rows[MM_GUEST].nats
        # the membership is announced where it runs over NATS
        await _until(lambda: bob.is_member("guest", me) and bob.is_member("lobby", me))
        # a post goes over NATS; bob gets it
        mine = await port.post("RESULT · done", channel_id=GUEST_ID)
        assert mine.id.startswith("n1:guest:") and mine.channel_id == GUEST_ID
        await _until(lambda: any(e.where == "guest" and e.sender == me for e in bob_events))
        # what bob posts there comes over NATS, under the directory's id
        await bob.post("guest", "lunch anyone")
        await bob.post("guest", f"@{inner.me.username} can you look?")
        await bob.post("lobby", "a plain lobby post")
        await bob.post("lobby", "@channel deploy at 5")
        await _until(lambda: len([p for p in list(q._queue) if isinstance(p, PostEvent)  # type: ignore[attr-defined]
                                  and p.post.user_id != inner.bot_id]) >= 4)
        got = [p for p in _posts(q) if p.user_id != inner.bot_id]
        assert {p.channel_id for p in got} == {GUEST_ID, LOBBY_ID}
        recent = await port.list_recent(channel_id=GUEST_ID)
        assert [p.message for p in recent][-2:] == ["lunch anyone", f"@{inner.me.username} can you look?"]
        assert await port.posts_after(0, channel_id=LOBBY_ID)
        assert inner.touched() == []  # no Mattermost call for the guest or the lobby
        # the wake rules of RT-6.22 step 3 over these NATS posts: a mention in the guest, the
        # @channel in the lobby; the plain posts stay dropped
        inbox = WebSocketInbox(client=port, source=FakeEvents(*(PostEvent(p, None, False) for p in got)))  # type: ignore[arg-type]
        inbox._bot_user_id, inbox._bot_username = inner.bot_id, inner.me.username
        await inbox.refresh_channels()
        await inbox._read_loop()
        woke = [inbox._queue.get_nowait() for _ in range(inbox._queue.qsize())]
        assert sorted((e.post.message, e.guest, e.lobby) for e in woke) == [
            (f"@{inner.me.username} can you look?", True, False), ("@channel deploy at 5", False, True)]
        # a guest that stays on Mattermost is read there as before
        assert await port.get_channel_id("mmonly") == MM_GUEST
        assert any("'mmonly'" in c for c in inner.log)
    finally:
        await port.stop()


async def test_with_the_switch_off_nothing_changes(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.22 step 7: off (no piece 12 in the channels' records), the guest and the lobby stay on
    Mattermost: their NATS posts are dropped, no membership announced, the lookups are the chat's."""
    port, inner, bob, _ = await _setup(rig, monkeypatch, {"guest": [1, 2]},
                                       {"home": "", "guest": GUEST_ID, "lobby": LOBBY_ID})
    q = port.subscribe()
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 15)
    try:
        assert planb.known() and not planb.on(12, "guest")
        assert await port.resolve() == {"home": WORK}
        assert await port.guest_channel_ids() == {MM_GUEST}
        assert await port.member_channels() == await inner.member_channels()
        with pytest.raises(LookupError):
            await port.join_channel("guest")  # the chat's join, as before
        assert await port.lobby_channel_id() == WORK  # the fake chat's lobby is the home
        await bob.post("guest", "@agent hello")
        await asyncio.sleep(1.0)
        assert _posts(q) == [] and not any(isinstance(e, MembershipEvent) for e in list(q._queue))  # type: ignore[attr-defined]
        assert not bob.is_member("guest", rig.names["alice"])
        assert any("join_channel ('guest',)" in c for c in inner.log)
    finally:
        await port.stop()


async def test_a_guest_switched_on_that_the_record_names_no_id_for_is_not_reachable(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: switched on, but the directory binds no Mattermost id; never Mattermost instead."""
    monkeypatch.setattr("mattermost_ai_agent_bus.nats_port.CHANNEL_RECORD_WAIT_SEC", 2.0)
    port, inner, bob, _ = await _setup(rig, monkeypatch, {"guest": [12]}, {"home": "", "guest": "", "lobby": ""})
    inner.channels = [UnreadChannel(MM_GUEST, "O", name="guest")]  # the bot is still a member there
    inner.guests = {MM_GUEST}
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 15)
    try:
        assert await port.resolve() == {"home": WORK}
        with pytest.raises(NatsPortError, match="not reachable"):
            await port.get_channel_id("guest")
        with pytest.raises(NatsPortError, match="not reachable"):
            await port.join_channel("guest")
        assert await port.guest_channel_ids() == set()  # its Mattermost membership is left out
        with pytest.raises(NatsPortError, match="not reachable"):
            await port.post("hello", channel_id=MM_GUEST)
        event = PostEvent(Post(id="p" * 26, channel_id=MM_GUEST, user_id="h" * 26, message="hi"), "O", False)
        assert await port.skip(event) is True  # its Mattermost events are dropped
        assert not any("join_channel" in c or "get_channel_id ('guest'" in c or "post " in c for c in inner.log)
        await bob.post("guest", "over NATS")
        await asyncio.sleep(0.5)
        assert port._posts.get("guest") is None  # its NATS posts too
    finally:
        await port.stop()
