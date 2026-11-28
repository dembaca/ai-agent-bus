"""Plan B piece 14c-2b: the hook API, the status and the heartbeat without a Mattermost token
(RT-6.17, RT-6.18 step 13, RT-6.6 step 13, RT-6.2 step 9). A session decided to run without a
bot (nobot.held) and with its NATS name is served by the hook API - authenticated by the
server's own key, as every session - and every route keys by the NATS name (identity.py); the
status, the crown and the heartbeat run without a token where the status is mirrored (plan B
piece 9, lease._beats). With a bot every answer is as before. Inert until piece 14c-2c flips
nobot.CROWN_NEEDS_BOT: no session decides so yet.

Unit tests on the fake NATS bus of test_nats_port.py with every plan B piece on and every
Mattermost call of the adapter failing the test; with MM_TEST_NATS=1 and CB_NATS_SERVER one test
against a real nats-server (the rig of test_planb_piece12.py)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import WORK, HookClient
from test_crown_nats import HOME, NAME_ME, World, claimant
from test_nats_port import FakeNats, make
from test_natsbus import FakeLogin
from test_planb_piece12 import rig  # noqa: F401 — the real nats-server's fixture
from test_planb_piece14 import _real

from mattermost_ai_agent_bus import (
    channel, hookapi, identity, lease, natsbus, nobot, ownpost, server, session_file, unread, watermark,
)
from mattermost_ai_agent_bus.model import Post

ALL = ",".join(str(p) for p in range(1, 14))
HOME_ID = "h" * 26

# What the Mattermost adapter would read or write: none of it for a session without a bot.
MATTERMOST = ("get_me", "account_created", "member_channels", "set_presence", "set_custom_status",
              "clear_custom_status", "channel_users", "presences", "direct_channel_with_bot",
              "dm_partner", "get_channel", "channel_posts", "list_recent", "post", "post_sync",
              "get_channel_id", "work_channel_id", "guest_channel_ids", "lobby_channel_id",
              "reactions", "upload_file", "post_channel_id", "channel_header", "set_channel_header",
              "get_thread", "add_reaction", "view_channel")


def forbid(inner: Any) -> list[str]:
    """Every Mattermost call of `inner` fails and is logged; returns the log."""
    calls: list[str] = []

    def failing(name: str) -> Any:
        async def call(*_a: Any, **_kw: Any) -> Any:
            calls.append(name)
            raise RuntimeError(f"a Mattermost call without a bot: {name}")
        return call

    for name in MATTERMOST:
        setattr(inner, name, failing(name))
    return calls


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    nobot.reset()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CB_MM_BOT_USER_ID", raising=False)
    monkeypatch.delenv("MM_BOT_USER_ID", raising=False)
    monkeypatch.setattr(lease, "_beat_off", "")
    monkeypatch.setattr(lease, "_beat_warned", False)
    yield
    nobot.reset()


class BotLess:
    """test_crown_nats's world with every plan B piece on, decided without a bot: no token, no
    registrar session, every Mattermost call failing (`calls`)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, pieces: str = ALL) -> None:
        monkeypatch.setenv("CB_PLANB_TEST", pieces)
        self.world = World()
        self.port, self.inner, f = self.world.port, self.world.inner, self.world.fake
        self.fake: FakeNats = f

        async def channel_ids(_timeout: float) -> dict[str, str]:
            return {HOME: HOME_ID}

        monkeypatch.setattr(f, "member_names", lambda ch: sorted(n for c, n in f.members if c == ch),
                            raising=False)
        monkeypatch.setattr(f, "card_of", lambda n: natsbus.PresenceCard(), raising=False)
        monkeypatch.setattr(f, "bot_record", lambda n: None, raising=False)
        monkeypatch.setattr(f, "wait_channel_ids", channel_ids, raising=False)

    async def ready(self) -> BotLess:
        await self.world.prime()
        claimant(self.fake, NAME_ME, 5)  # no bot id: the name alone
        self.inner.registrar.session = None
        self.inner.token = None
        self.calls = forbid(self.inner)
        nobot.hold(self.port)
        assert identity.bot_less(self.port) and identity.key_now(self.port) == NAME_ME
        return self


@pytest.fixture
async def bl(monkeypatch: pytest.MonkeyPatch) -> BotLess:
    return await BotLess(monkeypatch).ready()


@pytest.fixture
async def serve() -> Any:
    """serve(client) -> (HookAPI, httpx client with its key); closed afterwards."""
    made: list[tuple[hookapi.HookAPI, httpx.AsyncClient]] = []

    async def start(client: Any) -> tuple[hookapi.HookAPI, httpx.AsyncClient]:
        async def get() -> Any:
            return client

        a = hookapi.HookAPI(get)
        await a.start(write_file=False)
        h = httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})
        made.append((a, h))
        return a, h

    yield start
    for a, h in made:
        await h.aclose()
        await a.close()


# --- every route without a bot: no Mattermost call ---------------------------------------------

ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/v1/me", None),
    ("GET", "/v1/pending?detail=1", None),
    ("GET", "/v1/last-post?unanswered=1", None),
    ("GET", "/v1/posts", None),
    ("GET", "/v1/ask-reactions", None),
    ("POST", "/v1/presence", {"state": "working", "label": "proj"}),
    ("POST", "/v1/usage", {"ctx": 10}),
    ("POST", "/v1/stat", {"k": "turn"}),
    ("POST", "/v1/reminded", {"post_ids": ["n1:home:3"]}),
    ("POST", "/v1/post", {"message": "hi"}),
    ("GET", "/v1/turn-end?drains=0&signoff_asked=0", None),
    ("GET", "/v1/wake?since=0&until=9999999999", None),
    ("GET", "/v1/handoff?runtime=claude-code", None),
    ("POST", "/v1/waiting", {}),
]


async def test_every_route_without_a_bot_answers_over_nats_alone(bl: BotLess, serve: Any) -> None:
    """RT-6.17, RT-6.18 step 13: each route of a session without a bot answers 200 and makes
    no Mattermost call; its own post and its thread are keyed by the NATS name."""
    _, h = await serve(bl.port)
    for method, path, body in ROUTES:
        r = await h.request(method, path, json=body)
        assert r.status_code == 200, (path, r.text)
    posted = (await h.post("/v1/post", json={"message": "root"})).json()
    assert posted["author"] == NAME_ME and posted["id"].startswith("n1:home:")
    r = await h.post("/v1/reply", json={"root_id": posted["id"], "message": "in the thread"})
    assert r.status_code == 200, r.text
    mine = (await h.get("/v1/posts")).json()["posts"]
    assert {p["user_id"] for p in mine} == {NAME_ME}
    assert bl.calls == []


async def test_me_without_a_bot_is_the_nats_name_and_its_channel_check(bl: BotLess, serve: Any) -> None:
    """RT-6.17 step 1, RT-6.12: user_id is the NATS name, the channel flag reads the
    confirmation keyed by it (session_file.key_ok, not the bot id's form)."""
    _, h = await serve(bl.port)
    me = (await h.get("/v1/me")).json()
    assert me["user_id"] == NAME_ME and me["username"] == NAME_ME and me["channel"] is False
    f = channel.confirmed_file(NAME_ME)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("CONFIRMED=1\n")
    assert (await h.get("/v1/me")).json()["channel"] is True
    assert bl.calls == []


async def test_every_route_without_a_bot_needs_the_key(bl: BotLess, serve: Any) -> None:
    """RT-6.17: the server's own key, never the Mattermost token: a wrong or missing key is
    401 on every route, unknown ones too, also for a session without a bot."""
    a, _ = await serve(bl.port)
    paths = [p for _, p in hookapi._ROUTES] + ["/v1/nothing"]
    async with httpx.AsyncClient(base_url=a.endpoint) as anon:
        for (method, path), _route in [*hookapi._ROUTES.items(), (("GET", "/v1/nothing"), None)]:
            assert (await anon.request(method, path)).status_code == 401, path
            got = await anon.request(method, path, headers={"Authorization": "Bearer wrong"})
            assert got.status_code == 401, path
    assert len(paths) == len(hookapi._ROUTES) + 1
    assert bl.calls == []


async def test_without_a_nats_name_every_route_is_a_503(bl: BotLess, serve: Any) -> None:
    """RT-6.17 error handling: decided without a bot, but the bus is down (no name): 503 with
    the reason, never a 502 and never an answer under a wrong key."""
    bl.fake.name = ""
    _, h = await serve(bl.port)
    for method, path, body in ROUTES:
        r = await h.request(method, path, json=body)
        assert r.status_code == 503, path
        assert r.json()["no_bot"] == hookapi.NO_NAME
    assert (await h.get("/v1/instructions")).status_code == 200  # the exempt routes stay
    assert bl.calls == []


@pytest.mark.parametrize("note", [nobot.WAITING, nobot.REGISTERING, nobot.FAILED.format(why="RuntimeError")])
async def test_an_open_or_failed_registration_is_a_503_with_its_reason(serve: Any, note: str) -> None:
    """RT-6.18 step 13: the host process's decision open, the bot being registered, or its
    registration failed: no token and no decision without a bot, so 503 with nobot.status."""
    client = HookClient()
    client.token = None
    nobot._set_note(client, note)
    _, h = await serve(client)
    r = await h.get("/v1/me")
    assert r.status_code == 503 and r.json() == {"error": "no bus session", "no_bot": note}


async def test_with_a_bot_the_answers_are_as_before(serve: Any) -> None:
    """The token path is unchanged: no token and no decision is the plain 503, a token 200."""
    client = HookClient()
    client.token = None
    _, h = await serve(client)
    r = await h.get("/v1/me")
    assert r.status_code == 503 and r.json() == {"error": "no bus session"}
    client.token = "t"
    assert (await h.get("/v1/me")).json()["user_id"] == client.me.id
    assert hookapi.served(client) and not identity.bot_less(client)


async def test_the_opencode_refusal_stays_for_a_session_without_a_bot(
        bl: BotLess, serve: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.18 step 6: the hooks of another OpenCode session are refused before the client."""
    monkeypatch.setenv("OPENCODE_SESSION_ID", "ses_own")
    _, h = await serve(bl.port)
    r = await h.get("/v1/me", headers={hookapi.CLIENT_HEADER: "ses_other"})
    assert r.status_code == 403 and r.json()["client_session"] == "refused"
    assert (await h.get("/v1/me", headers={hookapi.CLIENT_HEADER: "ses_own"})).status_code == 200


async def test_a_live_server_without_a_bot_keeps_its_endpoint_file(
        bl: BotLess, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.17 error handling: the probe of a second server of the same workspace gets the
    first one's 200 from /v1/me, so it does not take over its file. Before piece 14c-2b a live
    server without a bot answered 503 and counted as gone."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-14c2b")

    async def get() -> Any:
        return bl.port

    first = hookapi.HookAPI(get)
    await first.start(write_file=True)
    try:
        f = hookapi.file_path()
        assert session_file.parse(f)["CB_BUS_KEY"] == first.key
        assert "CB_MM_BOT_USER_ID" not in session_file.parse(f)  # a NATS name is no bot id
        assert await asyncio.to_thread(hookapi._answers, first.endpoint, first.key)
        second = hookapi.HookAPI(get)
        await asyncio.to_thread(second._write_file)  # the probe needs the loop free
        assert second.conflict is not None and second.conflict["file"] == str(f)
        assert session_file.parse(f)["CB_BUS_KEY"] == first.key
    finally:
        await first.close()


async def test_reminded_takes_a_nats_post_id(serve: Any) -> None:
    """RT-6.2 step 2: the posts that wait in a NATS home have NATS ids (n1:<channel>:<seq>);
    the Stop hook marks them as it marks Mattermost's. Junk stays a 400."""
    _, h = await serve(HookClient())
    for ids in (["n1:home:3"], ["p" * 26, "n1:home:12"]):
        r = await h.post("/v1/reminded", json={"post_ids": ids})
        assert r.status_code == 200, r.text
    for bad in (["n1:home:0"], ["n1:Home:3"], ["n1:home"], ["x" * 25]):
        assert (await h.post("/v1/reminded", json={"post_ids": bad})).status_code == 400


async def test_files_that_need_mattermost_are_refused_without_a_bot(bl: BotLess, serve: Any) -> None:
    """RT-6.33: a DM (to=), a Mattermost post's thread (root=) and a channel the bus does not
    watch need Mattermost: refused with the reason, nothing read there."""
    _, h = await serve(bl.port)
    for query in ("to=agent-x", "root=" + "p" * 26, "channel=" + "z" * 26):
        r = await h.post(f"/v1/files?name=a.txt&{query}", content=b"hi")
        assert r.status_code == 403, (query, r.text)
        assert "needs a Mattermost bot" in r.json()["error"]
    assert bl.calls == []


# --- the status, the crown and the heartbeat without a token -------------------------------------

async def test_the_status_and_the_crown_run_without_a_token_where_mirrored(bl: BotLess) -> None:
    """RT-6.6 steps 1 and 13: with piece 9 the status goes to the presence (the bridge shows
    it); the tick and the holder's beat run; no Mattermost call."""
    await lease.set_status(bl.port, "working", "proj")  # type: ignore[arg-type]
    shown = lease.presence_shown()
    assert shown is not None and shown["home"] == HOME and shown["text"].startswith("working · proj")
    await lease.command(bl.port, "tick")  # type: ignore[arg-type]
    await lease.nats_beat(bl.port)  # type: ignore[arg-type]
    await lease.usage_crossed(bl.port)  # type: ignore[arg-type]
    await lease.ctx_line(bl.port)  # type: ignore[arg-type]
    assert await lease.roster(bl.port) is not None  # type: ignore[arg-type]
    assert lease._beats(bl.port) and lease.beat_off() == ""  # type: ignore[arg-type]
    assert bl.calls == []


async def test_without_the_mirror_nothing_runs_and_bus_info_says_why(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.6 step 13: piece 9 off (or not readable) for a session without a bot: no status,
    no crown, no heartbeat (each would need Mattermost), logged once, and bus_info names it."""
    b = await BotLess(monkeypatch, pieces=",".join(str(p) for p in range(1, 14) if p != 9)).ready()
    with caplog.at_level(logging.WARNING, logger="mattermost_ai_agent_bus.lease"):
        await lease.set_status(b.port, "working", "proj")  # type: ignore[arg-type]
        assert await lease.command(b.port, "tick") == ""  # type: ignore[arg-type]
        await lease.nats_beat(b.port)  # type: ignore[arg-type]
    assert [r.message for r in caplog.records].count(f"lease: {lease.beat_off()}") == 1
    assert "not mirrored over NATS" in lease.beat_off()
    assert lease.presence_shown() is None and b.calls == []
    monkeypatch.setattr(server, "_get_client", lambda: b.port)
    import json
    info = json.loads(await server.bus_info())
    assert info["bot"] is None and info["no_bot"] == nobot.reason()
    assert info["nats_name"] == NAME_ME and info["heartbeat_off"] == lease.beat_off()
    assert b.calls == []


async def test_the_heartbeat_ticks_without_a_token(bl: BotLess, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.6 step 13: the heartbeat's guard is lease._beats, not the token."""
    ticks: list[str] = []

    async def command(client: Any, action: str) -> str:
        ticks.append(action)
        raise asyncio.CancelledError

    async def get() -> Any:
        return bl.port

    monkeypatch.setattr(lease, "command", command)
    with pytest.raises(asyncio.CancelledError):
        await lease.heartbeat(get)
    assert ticks == ["tick"] and bl.calls == []


# --- unread, the cursor, the bye ----------------------------------------------------------------

async def test_the_unread_floor_without_a_bot_is_a_day_back(bl: BotLess) -> None:
    """RT-6.2 step 1: no account to date without a bot; member_channels are the NATS rows."""
    rows = await bl.port.member_channels()
    assert [(r.channel_id, r.nats) for r in rows] == [(HOME_ID, True)]
    assert await unread.after(bl.port) == []  # type: ignore[arg-type]
    assert bl.calls == []


async def test_the_start_cursor_without_a_bot_is_the_read_mark_of_the_nats_name(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9, #213: a session without a bot (piece 14a's deferred start) restarts its
    consumers after the read mark keyed by its NATS name, as a bot's after its bot id."""
    from mattermost_ai_agent_bus import nats_port

    made: list[dict[str, Any]] = []
    port, inner, _ = make(bus=False)

    def build(*a: Any, **kw: Any) -> FakeNats:
        made.append(kw)
        return FakeNats(port, name=name)

    login = FakeLogin()
    name = port._name_before(login, "alice")
    assert session_file.key_ok(name)
    assert watermark.advance(name, f"{WORK}:nats", "n1:home:7", 5_000)
    inner.registrar.session = None
    inner.token = None
    port.bot_deferred = True
    monkeypatch.setattr(nats_port, "NatsBus", build)
    monkeypatch.setattr(lease, "_nats_link", None)
    await port.start(login, owner="alice")
    try:
        assert made[0]["since"] == {"home": (7, 5_000)}
    finally:
        await port.stop()
        lease.use_nats_link(None)


async def test_the_bye_without_a_bot_goes_over_nats(bl: BotLess, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2, RT-6.10: the exit handler's BYE needs the token; a session without a bot posts
    it over NATS in the shutdown, and the exit handler names it by its key."""
    hello = await bl.port.post("HELLO · here", channel_id=HOME_ID)
    ownpost.record(NAME_ME, HOME_ID, hello)
    monkeypatch.setattr(server, "_client", bl.port)
    monkeypatch.setattr(server, "_exit_handled", False)
    await server._bye_on_nats()
    last = ownpost.read(NAME_ME)
    assert "BYE" in last["MSG"] and last["HELLO"] == hello.id
    assert await ownpost.bye(bl.port, NAME_ME) == "signed off"  # once
    seen: list[str] = []
    monkeypatch.setattr(ownpost, "bye_sync", lambda client, uid: seen.append(uid) or "no announcement")
    monkeypatch.setattr(server, "_hook_api", None)
    monkeypatch.setattr(server, "_confirmed", None)
    monkeypatch.setattr(type(bl.inner), "keeps_bot_on_exit", False, raising=False)
    monkeypatch.setattr(bl.inner, "shutdown_sync", lambda: "no session", raising=False)
    server._cleanup_on_exit()
    assert seen == [NAME_ME] and bl.calls == []


async def test_no_bye_over_nats_with_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a bot the exit handler's bye_sync posts, as before: nothing at the shutdown."""
    port, _, _ = make()
    posted: list[Post] = []

    async def bye(*_a: Any) -> str:
        posted.append(None)  # type: ignore[arg-type]
        return "BYE posted"

    monkeypatch.setattr(ownpost, "bye", bye)
    monkeypatch.setattr(server, "_client", port)
    monkeypatch.setattr(server, "_exit_handled", False)
    await server._bye_on_nats()
    assert posted == []


# --- against a real nats-server (MM_TEST_NATS=1) ----------------------------------------------

async def test_real_nats_a_session_without_a_bot_has_a_hook_api(
        rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, serve: Any) -> None:  # noqa: F811
    """RT-6.17 against a real nats-server: decided without a bot (nobot.CROWN_NEEDS_BOT off,
    as piece 14c-2c will run it), the hook API answers /v1/me with the NATS name and takes the
    presence; a wrong key is a 401."""
    monkeypatch.setenv("CB_NATS_HOMES", "home guest lobby")
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    monkeypatch.setattr(nobot, "CROWN_NEEDS_BOT", False)
    port, inner = await _real(rig, monkeypatch, tmp_path, {c: list(range(1, 14)) for c in ("home", "guest", "lobby")})
    try:
        out = await nobot.host_decide(port, "host-proj-abc", "d", "the-secret", timeout=10)
        assert out.startswith("no bot"), out
        assert identity.bot_less(port)
        a, h = await serve(port)
        me = await h.get("/v1/me")
        assert me.status_code == 200, me.text
        assert me.json()["user_id"] == port.own_name() == rig.names["alice"]
        r = await h.post("/v1/presence", json={"state": "working", "label": "proj"})
        assert r.status_code == 200, r.text
        async with httpx.AsyncClient(base_url=a.endpoint) as anon:
            assert (await anon.get("/v1/me", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert inner.registrar.calls == [] and inner.token is None
    finally:
        await port.stop()

async def test_a_name_that_goes_mid_route_is_a_503_never_a_stale_bot_id(
        bl: BotLess, serve: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.17 (review of #342): `served` is checked once; if the bus stops during a long route
    the session has no key: identity.NoName, a 503 with the reason, and never the env's
    CB_MM_BOT_USER_ID as the key of asks, watermark and reminded."""
    monkeypatch.setenv("CB_MM_BOT_USER_ID", "u" * 26)  # a stale or inherited bot id
    assert await identity.self_key(bl.port) == NAME_ME  # the name first, as before
    bl.fake.name = ""
    with pytest.raises(identity.NoName):
        await identity.self_key(bl.port)
    with pytest.raises(identity.NoName):
        await identity.self_me(bl.port)
    with pytest.raises(identity.NoName):
        await hookapi._uid(bl.port)
    monkeypatch.setattr(hookapi, "served", lambda client: True)  # the race: served, then gone
    _, h = await serve(bl.port)
    seen = {path.split("?")[0]: (await h.request(method, path, json=body)).status_code
            for method, path, body in ROUTES}  # every route of the table, the keyed and the others
    # the routes that key by the session's identity: a 503 with the reason; the two that read no key
    # answer (usage, waiting); a post has no bus to go to: 502, and nothing under a stale id
    assert seen == {"/v1/me": 503, "/v1/pending": 503, "/v1/last-post": 503, "/v1/posts": 503,
                    "/v1/ask-reactions": 503, "/v1/presence": 503, "/v1/usage": 200, "/v1/stat": 503,
                    "/v1/reminded": 503, "/v1/post": 502, "/v1/turn-end": 503, "/v1/wake": 503,
                    "/v1/handoff": 503, "/v1/waiting": 200}
    r = await h.get("/v1/me")
    assert r.status_code == 503 and r.json()["no_bot"] == hookapi.NO_NAME
    assert bl.calls == []


async def test_the_counters_of_a_session_without_a_bot_are_keyed_by_its_name(
        bl: BotLess, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.9, RT-6.2 step 9 (review of #342): a stale CB_MM_BOT_USER_ID must not key the counters of
    a session decided without a bot; before the decision, and with a bot, the env still comes first."""
    from mattermost_ai_agent_bus import stats

    monkeypatch.setenv("CB_MM_BOT_USER_ID", "u" * 26)
    assert stats.bot_id() == NAME_ME  # held without a bot: the name nobot.hold set
    nobot.reset()  # no decision (the key stays set): as before, the env first
    stats.set_user(NAME_ME)
    assert stats.bot_id() == "u" * 26
