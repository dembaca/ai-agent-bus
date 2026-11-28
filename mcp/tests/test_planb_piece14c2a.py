"""Plan B piece 14c-2a, the identity without a bot (RT-6.2 step 9, RT-6.6 step 13, RT-6.38
step 10, RT-6.18 step 13): every site that decides "is this me, my state, may I act" asks
identity.py. With a Mattermost bot every value is the bot's, exactly as before; a session that
decided to run without one (nobot.held) is its NATS name, and is mentioned by the bridge's bot
of that name, else the name. Inert: nobot.CROWN_NEEDS_BOT keeps every session registering.

A guard test lists every read of Mattermost's users/me (`get_me()`) in the MCP server's sources:
one outside ALLOWED, the sites that stay Mattermost's on purpose, fails, so no site is missed."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fakes import BOT, WORK, FakeBus, FakeRegistrar

from mattermost_ai_agent_bus import compose, identity, lease, nobot, session_file, stats, websocket_inbox
from mattermost_ai_agent_bus.model import Me, Post
from mattermost_ai_agent_bus.nats_port import NatsEvents, NatsPort
from mattermost_ai_agent_bus.natsbus import BotRecord, NatsSettings

NAME = "a2c4e6g7" * 3 + "h"  # 25 base32 characters, as natsbus.agent_name makes them
HOME = "home"
HOME_ID = "h" * 26
PREFIX = "nats-"


class Bus:
    """What identity and the port read of a natsbus.NatsBus: its own name, the bridge's bot
    record of a name and what owned_link checks it against, the channel directory."""

    def __init__(self, record: BotRecord | None = None) -> None:
        self.name = NAME
        self.record = record
        self.connected = True
        self.synced = True

    def bot_record(self, name: str) -> BotRecord | None:
        return self.record if name == NAME else None

    def bridge_prefix(self, home: str) -> str:
        return PREFIX

    def anchor_of(self, name: str) -> str:
        return "anchor"

    def accounts_of(self, anchor: str) -> list[str]:
        return ["acct"]

    async def wait_channel_ids(self, timeout: float) -> dict[str, str]:
        return {HOME: HOME_ID}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    nobot.reset()
    monkeypatch.delenv("CB_MM_BOT_USER_ID", raising=False)
    monkeypatch.delenv("MM_BOT_USER_ID", raising=False)
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    monkeypatch.setattr(stats, "_uid", "")
    yield
    nobot.reset()


def _port(*, bot: bool, record: BotRecord | None = None) -> tuple[NatsPort, FakeBus]:
    reg = FakeRegistrar()
    inner = FakeBus(reg)
    inner.work_channel_name = HOME
    if bot:
        reg.session = reg.info  # registered: the session names the bot
    else:
        inner.token = None  # no bot: users/me is not readable

        async def no_me() -> Me:
            raise RuntimeError("no Mattermost bot in this session")

        inner.get_me = no_me  # type: ignore[method-assign]
    port = NatsPort(inner, NatsSettings(servers=("nats://127.0.0.1:1",), home=HOME), (HOME,))  # type: ignore[arg-type]
    port.use_bus(Bus(record))  # type: ignore[arg-type]
    return port, inner


def _bot_less(record: BotRecord | None = None) -> NatsPort:
    port, _ = _port(bot=False, record=record)
    nobot.hold(port)  # decided without a bot (piece 14c flips the decision on)
    return port


# --- the helpers ------------------------------------------------------------------------------

async def test_with_a_bot_every_value_is_the_bot_id() -> None:
    """RT-6.6 step 13: with a bot nothing changes, also when the decision were held."""
    port, inner = _port(bot=True)
    for client in (inner, port, compose.Remembered(port)):  # type: ignore[arg-type]
        assert await identity.self_key(client) == BOT
        assert identity.key_now(client) == BOT
        assert await identity.self_me(client) is inner.me
        assert await identity.self_name(client) == inner.me.username
        assert await lease._user_id(client) == BOT
    nobot.hold(port)  # a bot is there: held or not, the bot's id
    assert await identity.self_key(port) == BOT and identity.key_now(port) == BOT
    assert stats.bot_id() == ""  # no key set for a session with a bot


async def test_with_a_bot_the_env_id_still_comes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session that has not decided to run without a bot keeps the old order: the env id."""
    port, _ = _port(bot=False)
    monkeypatch.setenv("CB_MM_BOT_USER_ID", BOT)
    assert await identity.self_key(port) == BOT


async def test_a_stale_env_id_does_not_split_a_session_decided_without_a_bot(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #333: once the session has decided to run without a bot, a stale
    CB_MM_BOT_USER_ID must not give the crown's files one key and the asks another."""
    port, _ = _port(bot=False)
    monkeypatch.setenv("CB_MM_BOT_USER_ID", BOT)
    nobot.hold(port)
    assert await identity.self_key(port) == NAME
    assert identity.key_now(port) == NAME


async def test_without_a_bot_the_key_is_the_nats_name() -> None:
    """RT-6.2 step 9: no users/me is read (it would raise); the key is the NATS name."""
    port = _bot_less()
    for client in (port, compose.Remembered(port)):  # type: ignore[arg-type]
        assert await identity.self_key(client) == NAME
        assert identity.key_now(client) == NAME
        assert await lease._user_id(client) == NAME
        me = await identity.self_me(client)
        assert (me.id, me.username) == (NAME, NAME)  # no bridge bot known: the name itself
    assert stats.bot_id() == NAME  # the counters and the presence card follow the key


async def test_without_a_bot_the_name_is_the_bridges_bot_of_it() -> None:
    """RT-6.38 step 10: mentioned by the bot the bridge shows the name as (owned_link)."""
    rec = BotRecord(agent=NAME, bot="b" * 26, username=PREFIX + NAME, owner_account="acct", exp=0.0)
    port = _bot_less(rec)
    assert await identity.self_name(port) == PREFIX + NAME
    assert await identity.self_key(port) == NAME


async def test_not_decided_without_a_bot_reads_users_me_as_before() -> None:
    """Inert: a session without a token that has not decided keeps today's path (it fails)."""
    port, _ = _port(bot=False)
    assert identity.key_now(port) == ""
    with pytest.raises(RuntimeError, match="no Mattermost bot"):
        await identity.self_key(port)
    with pytest.raises(RuntimeError):
        await identity.self_me(port)


def test_until_14c_the_crown_still_needs_the_bot() -> None:
    assert nobot.CROWN_NEEDS_BOT is True  # piece 14c-2c flips it, not this one


# --- the gates and the sites -----------------------------------------------------------------

async def test_the_crown_command_takes_a_nats_name_and_refuses_junk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.6 step 13: lease._command's gate is session_file.identity_ok: exactly a bot id (26
    `[a-z0-9]`) or a NATS name (25 `[a-z2-7]`), not 26 characters only and not any short name."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    session_file.state_dir().mkdir(parents=True)

    class Past(Exception):
        pass

    async def home(client: Any) -> str:
        raise Past

    monkeypatch.setattr(lease, "home_id", home)
    for uid in (NAME, BOT):
        monkeypatch.setattr(lease, "_user_id", lambda c, u=uid: _const(u))
        with pytest.raises(Past):
            await lease._command(object(), "tick")  # type: ignore[arg-type]
    for junk in ("", NAME.upper(), "../" + NAME, "a.b", "-" + NAME, "x" * 129, NAME[:24], NAME + "aa",  # 24 and 27 characters; 26 of [a-z0-9] is a bot id's shape
                
                 BOT[:24], BOT + "a", "xxxxx", NAME[:-1] + "1", NAME[:-1] + ".", NAME[:-1] + "/",
                 NAME[:-1] + "-"):
        monkeypatch.setattr(lease, "_user_id", lambda c, u=junk: _const(u))
        assert await lease._command(object(), "tick") == ""  # type: ignore[arg-type]


async def _const(value: str) -> str:
    return value


async def test_the_inbox_knows_itself_without_users_me(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inbox's identity (asks, watermark, own posts) without a bot is the NATS name."""
    port = _bot_less()
    inbox = websocket_inbox.WebSocketInbox(client=port, watched_channel_ids={WORK})  # type: ignore[arg-type]
    await inbox.ensure_identity()
    assert (inbox._bot_user_id, inbox._bot_username) == (NAME, NAME)
    port2, inner = _port(bot=True)
    inbox2 = websocket_inbox.WebSocketInbox(client=port2, watched_channel_ids={WORK})  # type: ignore[arg-type]
    await inbox2.ensure_identity()
    assert (inbox2._bot_user_id, inbox2._bot_username) == (BOT, inner.me.username)


def test_the_event_source_without_a_bot_is_nats_alone() -> None:
    port = _bot_less()
    port.inner.base_url = "https://mm.example"  # type: ignore[attr-defined]
    assert isinstance(compose.event_source(compose.Remembered(port)), NatsEvents)  # type: ignore[arg-type]
    port2, inner = _port(bot=True)
    inner.base_url = "https://mm.example"  # type: ignore[attr-defined]
    assert not isinstance(compose.event_source(compose.Remembered(port2)), NatsEvents)  # type: ignore[arg-type]


async def test_an_own_post_is_remembered_without_a_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose._remember gets the key: own posts reach ownpost and unanswered (RT-6.2 step 2)."""
    seen: list[str] = []

    async def remember(port: Any, key: str, channel_id: str, post: Post) -> None:
        seen.append(key)

    monkeypatch.setattr(compose, "_remember", remember)
    for port, want in ((_bot_less(), NAME), (_port(bot=True)[0], BOT)):
        post = Post(id="p", channel_id=WORK, user_id=want, message="hi")

        async def send(*a: Any, _p: Post = post, **k: Any) -> Post:
            return _p

        port.post = send  # type: ignore[method-assign]
        await compose.Remembered(port).post("hi")  # type: ignore[arg-type]
    assert seen == [NAME, BOT]


async def test_the_home_id_without_a_bot_is_the_directorys() -> None:
    """RT-6.39 step 6: no Mattermost read; with a bot the adapter's, as before."""
    port = _bot_less()
    assert await port.work_channel_id() == HOME_ID
    assert await port._home_or(None) == HOME_ID and port.home_id == HOME_ID
    port2, _ = _port(bot=True)
    assert await port2.work_channel_id() == WORK
    assert await port2._home_or("c" * 26) == "c" * 26


async def test_the_own_owner_check_compares_with_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """NatsPort's own-id comparisons (here the owner of its own bot, piece 4) use the key."""
    async def no_circle(user_id: str) -> None:
        return None

    port = _bot_less()
    monkeypatch.setattr(port, "_circle4", no_circle)
    assert await port._owner4(NAME) is None  # its own: in doubt the split stays on
    assert await port._owner4("z" * 26) == ""
    port2, _ = _port(bot=True)
    monkeypatch.setattr(port2, "_circle4", no_circle)
    assert await port2._owner4(BOT) is None and await port2._owner4(NAME) == ""


# --- the guard: every users/me read is listed ------------------------------------------------

SRC = Path(identity.__file__).parent

# (module, function) -> why it still reads Mattermost's users/me.
ALLOWED: dict[tuple[str, str], str] = {
    ("client.py", "*"): "the Mattermost adapter itself: users/me is its own account",
    ("identity.py", "self_key"): "with a bot: its id when neither the session nor the env names it",
    ("identity.py", "self_me"): "with a bot: users/me as before",
    ("attest.py", "start"): "the registrar's attestation of the bot (needs its token)",
    ("hookapi.py", "_uid"): "the hook API without a token is piece 14c-2b",
    ("hookapi.py", "me"): "the hook API without a token is piece 14c-2b",
    ("hookapi.py", "wake"): "the hook API without a token is piece 14c-2b",
    ("hosted.py", "_relay_dm"): "the permission relay's Mattermost DM",
    ("lease.py", "_crown_taken"): "the crown_taken post of Mattermost's election (no NATS home)",
    ("login_notice.py", "*"): "the login approval's DM and ask: piece 14c-3",
    ("nats_port.py", "_bot_id"): "the bot's own Mattermost id: none without a bot",
    ("nats_port.py", "_bot_name"): "the bot's username in the presence: none without a bot",
    ("server.py", "bus_info"): "says why there is no bot (nobot.status); its status without a token is 14c-2b",
    ("server.py", "get_me"): "the get_me tool's raw=true: users/me as is",
}


def _get_me_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text())

        def walk(node: ast.AST, fn: str, _p: Path = path) -> None:
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "get_me"):
                    sites.add((_p.name, fn))
                walk(child, name)

        walk(tree, "<module>")
    return sites


def test_every_users_me_read_is_allowed() -> None:
    """A site that reads users/me outside ALLOWED decides with the bot's identity, which a
    session without a bot has not: it asks identity.py (self_key, self_name, self_me)."""
    sites = _get_me_sites()
    loose = {s for s in sites if s not in ALLOWED and (s[0], "*") not in ALLOWED}
    assert not loose, f"get_me() outside the allow-list (use identity.self_key/self_me): {sorted(loose)}"
    stale = {a for a in ALLOWED if a[1] != "*" and a not in sites}
    assert not stale, f"allow-list entries no longer reading users/me: {sorted(stale)}"
