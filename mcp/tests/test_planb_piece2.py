"""RT-6.37 step 6, RT-6.44 step 3, RT-6.42 step 8, RT-6.21 (ADR-014 phase 5, plan B piece 2): in a
home over NATS the roster, the link of a bot to its NATS name and the names and kinds of
Mattermost accounts come from NATS alone: presence and membership, the bridge's bot record
(`<presence>.bot.<agent>.v1`) and the login service's account records.

The switch is planb.on(2, channel), a stub that CB_PLANB_TEST="2" turns on. Unit tests drive a
real NatsBus with the messages a consumer would deliver (no server) beside a chat that fails the
test at any read of Mattermost's users; with MM_TEST_NATS=1 and CB_NATS_SERVER a real nats-server
shows the round trip and that an agent cannot publish a bot record.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace as dataclasses_replace
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeRegistrar
from test_nats_port import _settings
from test_natsbus import T0, FakeMsg, Rig, _presence, _presence_reply, _unit_bus, until

from mattermost_ai_agent_bus import departed, lease, natsbus, planb
from mattermost_ai_agent_bus.nats_port import NatsPort
from mattermost_ai_agent_bus.natsbus import MessageError, NatsBus, decode_bot_record, encode_presence

BOT_A = "a" * 26
BOT_B = "b" * 26
ALICE = "u" * 26  # alice's Mattermost account
STRAY = "s" * 26  # an id in no record
AGENT_A = "agent-a"
AGENT_B = "agent-b"


def _bot_body(agent: str = AGENT_A, **over: Any) -> bytes:
    rec: dict[str, Any] = {"v": 1, "agent": agent, "bot": BOT_A, "username": "bb-" + agent,
                           "owner_account": ALICE, "iat": int(T0), "exp": int(T0) + 180}
    rec.update(over)
    return json.dumps(rec).encode()


def _account_body(**over: Any) -> bytes:
    rec: dict[str, Any] = {"v": 1, "account": ALICE, "anchor": "alice", "name": "alice", "approver": False,
                           "token_iat": 1, "iat": 2, "exp": int(T0) + 900}
    rec.update(over)
    return json.dumps(rec).encode()


# --- the switch ------------------------------------------------------------------------------

def test_the_switch_names_piece_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    assert planb.on(2, "home") is False
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    assert planb.on(2, "home") is True and planb.on(1, "home") is False
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    assert planb.on(2, "home") is False


# --- the strict decoder ----------------------------------------------------------------------

def test_the_bot_record_decodes_strictly() -> None:
    rec = decode_bot_record(_bot_body(), AGENT_A)
    assert (rec.agent, rec.bot, rec.username, rec.owner_account, rec.exp) == (
        AGENT_A, BOT_A, "bb-agent-a", ALICE, T0 + 180)
    gone = decode_bot_record(_bot_body(bot=None, username=None, owner_account=None), AGENT_A)
    assert gone.bot is None and gone.username is None and gone.owner_account is None
    with pytest.raises(MessageError):
        decode_bot_record(_bot_body(), AGENT_B)  # another agent's
    for over in ({"v": 2}, {"bot": "short"}, {"bot": "A" * 26}, {"bot": None}, {"owner_account": None},
                 {"owner_account": "x"}, {"username": "agent-a"}, {"username": "bb-agent-b"},
                 {"username": "BB-agent-a"}, {"username": None}, {"iat": "x"}, {"exp": 1.5}, {"exp": True},
                 {"extra": 1}):
        with pytest.raises(MessageError):
            decode_bot_record(_bot_body(**over), AGENT_A)
    missing = json.loads(_bot_body())
    del missing["owner_account"]
    for bad in (json.dumps(missing).encode(), b"", b"{}", b"[]",
                b'{"v":1,"v":1,"agent":"agent-a","bot":null,"username":null,"owner_account":null,"iat":1,"exp":2}'):
        with pytest.raises(MessageError):
            decode_bot_record(bad, AGENT_A)
    with pytest.raises(MessageError):  # a null with a name left over is no null
        decode_bot_record(_bot_body(bot=None, owner_account=None), AGENT_A)


async def _record(bus: NatsBus, agent: str, seq: int, t: float, body: bytes | None = None) -> None:
    await bus._on_presence_msg(FakeMsg(f"presence.bot.{agent}.v1", _presence_reply(bus, seq, seq, t),
                                       body if body is not None else _bot_body(agent)))


async def test_the_newest_record_by_server_time_and_sequence_counts_while_fresh() -> None:
    bus, _ = _unit_bus()
    await _presence(bus, AGENT_A, 1, T0)
    await _record(bus, AGENT_A, 2, T0)
    assert bus.bot_record(AGENT_A) is not None and bus.bot_record(AGENT_A).bot == BOT_A  # type: ignore[union-attr]
    await _record(bus, AGENT_A, 3, T0 + 5, _bot_body(bot=BOT_B, iat=int(T0) + 5, exp=int(T0) + 185))
    assert bus.bot_record(AGENT_A).bot == BOT_B  # type: ignore[union-attr]
    await _record(bus, AGENT_A, 2, T0)  # an older one never overrides a newer one
    assert bus.bot_record(AGENT_A).bot == BOT_B  # type: ignore[union-attr]
    await _record(bus, AGENT_A, 4, T0 + 6, b'{"v":1,"agent":"agent-a"}')  # malformed: dropped, erases nothing
    assert bus.bot_record(AGENT_A).bot == BOT_B  # type: ignore[union-attr]
    # never longer than 3 minutes from the write, whatever its exp says
    await _record(bus, AGENT_A, 5, T0 + 10, _bot_body(exp=int(T0) + 99999))
    assert bus.bot_record(AGENT_A) is not None
    await _presence(bus, AGENT_B, 6, T0 + 10 + 181)  # the server's clock moves on
    assert bus.bot_record(AGENT_A) is None  # stale
    await _record(bus, AGENT_A, 7, T0 + 200, _bot_body(bot=None, username=None, owner_account=None, exp=int(T0) + 380))
    assert bus.bot_record(AGENT_A) is None and bus.bot_records() == {}  # null: the bot is gone


# --- the world ----------------------------------------------------------------------------

TRAPPED = ("channel_users", "presences", "usernames", "username", "agents", "bot_accounts", "agent_owner",
           "bot_description", "agent_attestation", "attestation_record", "bots_named", "is_member",
           "attested_circle", "own_anchors", "may_approve", "humans_among", "post_channel_id")


class NoMattermost(FakeBus):
    """A chat that records every read of Mattermost's users, statuses, owners and channel members."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.reads: list[str] = []


def _trap(name: str) -> Any:
    original = getattr(FakeBus, name)

    async def trapped(self: NoMattermost, *a: Any, **kw: Any) -> Any:
        self.reads.append(name)
        return await original(self, *a, **kw)
    return trapped


for _n in TRAPPED:
    setattr(NoMattermost, _n, _trap(_n))


async def _world(monkeypatch: pytest.MonkeyPatch, piece: str = "2", records: bool = True
                 ) -> tuple[NatsPort, NatsBus, NoMattermost]:
    """A home `home` over NATS: agent-a (owner alice, a bridge bot, a card), agent-b (owner alice,
    no bot), the bridge's signal naming the prefix `bb-`, alice's account record."""
    if piece:
        monkeypatch.setenv("CB_PLANB_TEST", piece)
    else:
        monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    inner = NoMattermost(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    bus, _ = _unit_bus()
    port = NatsPort(inner, _settings(), ("home",))  # type: ignore[arg-type]
    port.use_bus(bus)
    port._ids = {"home": WORK}
    port._names = {}
    bus._connected, bus._nc, bus._synced = True, object(), True  # type: ignore[assignment]
    seq = 0

    async def msg(subject: str, data: bytes, t: float) -> None:
        nonlocal seq
        seq += 1
        await bus._on_presence_msg(FakeMsg(subject, _presence_reply(bus, seq, seq, t), data))

    card = natsbus.PresenceCard(usage="ctx 62% · 5h 40%", release="v0.104.0 unchecked")
    await msg(f"presence.alice.{AGENT_A}", encode_presence("online", "", "", card), T0)
    await msg(f"presence.alice.{AGENT_B}", encode_presence("online"), T0)
    for who in (AGENT_A, AGENT_B):
        await msg(f"presence.member.home.{who}", natsbus.encode_claim(), T0)
    await msg("presence.bridge.home.v1", json.dumps({"v": 1, "home": "home", "people": True, "iat": int(T0),
                                                     "exp": int(T0) + 180, "bot_prefix": "bb-"}).encode(), T0)
    if records:
        await msg(f"presence.account.mattermost.{ALICE}.v1", _account_body(), T0)
        await msg(f"presence.bot.{AGENT_A}.v1", _bot_body(), T0)
    lease.use_nats("home", port)  # type: ignore[arg-type]
    return port, bus, inner


@pytest.fixture(autouse=True)
def _clean_lease() -> Any:
    yield
    lease.use_nats("", None)


async def test_the_roster_comes_from_nats_and_reads_nothing_in_mattermost(monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    rows = await lease._rows(inner, WORK, "home")  # type: ignore[arg-type]
    by_id = {r["id"]: r for r in rows}
    assert sorted(by_id) == [AGENT_A, AGENT_B]
    # the bridge's bot record names the bot's username; without one, the NATS name itself
    assert by_id[AGENT_A]["username"] == "bb-agent-a" and by_id[AGENT_B]["username"] == AGENT_B
    assert all(r["reachable"] and r["live"] and r["home"] == "home" for r in rows)
    assert not any(r["crown"] for r in rows)  # no claim yet
    roster = await lease.roster_in(inner, WORK, "home")  # type: ignore[arg-type]
    a = next(e for e in roster if e.user_id == AGENT_A)
    # the numbers and the version of the presence, as the status text gave them; no state word
    assert a.usage == {"ctx": 62.0, "limit_5h": {"used": 40.0}} and a.release == {"version": "0.104.0", "source": "unchecked"}
    assert a.status == "" and a.username == "bb-agent-a"
    assert next(e for e in roster if e.user_id == AGENT_B).usage == {}
    assert inner.reads == []


async def test_an_away_agent_is_not_reachable_an_offline_or_non_member_is_no_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    await _presence(bus, AGENT_B, 90, T0 + 1, status="away", owner="alice")
    await _presence(bus, "agent-c", 91, T0 + 1, owner="alice")  # never announced its membership
    rows = {r["id"]: r for r in await lease._rows(inner, WORK, "home")}  # type: ignore[arg-type]
    assert sorted(rows) == [AGENT_A, AGENT_B] and rows[AGENT_B]["reachable"] is False
    await _presence(bus, AGENT_B, 92, T0 + 2, status="bye", owner="alice")
    assert sorted(r["id"] for r in await lease._rows(inner, WORK, "home")) == [AGENT_A]  # type: ignore[arg-type]
    await _presence(bus, "agent-z", 93, T0 + 100)  # time passes: agent-a's presence is older than 90 s
    assert await lease._rows(inner, WORK, "home") == []  # type: ignore[arg-type]
    assert inner.reads == []


async def test_the_crown_is_the_views_holder_and_a_claim_shows_on_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    await bus._on_presence_msg(FakeMsg(f"presence.crown.home.{AGENT_B}", _presence_reply(bus, 50, 50, T0 + 1),
                                       natsbus.encode_claim()))
    roster = await lease.roster_in(inner, WORK, "home")  # type: ignore[arg-type]
    assert [e.username for e in roster if e.crown] == [AGENT_B] and roster[0].user_id == AGENT_B
    assert lease.crown_in(roster) == AGENT_B

    async def named(channel_id: str) -> Any:
        return SimpleNamespace(name="home")
    inner.get_channel = named  # type: ignore[method-assign]  # the one read of the channel's name
    assert await lease.coordinator_of(inner, WORK) == AGENT_B  # type: ignore[arg-type]
    assert inner.reads == []


async def test_a_bot_is_linked_only_through_the_bridges_record_and_its_owner_account(
        monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    assert port.owned_link(AGENT_A, "home") is not None and port.owned_link(AGENT_A, "home").bot == BOT_A  # type: ignore[union-attr]
    assert port.linked2("home") == {BOT_A: AGENT_A}
    assert port.owned_link(AGENT_B, "home") is None  # no record
    # a forged presence `bot` field and a forged tag give no link: the presence's own words count nothing
    await bus._on_presence_msg(FakeMsg(f"presence.alice.{AGENT_B}", _presence_reply(bus, 60, 60, T0 + 1),
                                       encode_presence("online", BOT_B, "bb-agent-b")))
    assert bus.bot_of(AGENT_B) == BOT_B
    assert port.owned_link(AGENT_B, "home") is None and port.linked2("home") == {BOT_A: AGENT_A}
    rows = [{"id": BOT_B, "text": f"working · ⇄{natsbus.link_tag(AGENT_B)} · ~home"}]
    assert port.linked2("home").get(BOT_B) is None and rows
    # the record's owner account is not the account that binds the owner's anchor: no link
    await bus._on_presence_msg(FakeMsg(f"presence.bot.{AGENT_A}.v1", _presence_reply(bus, 61, 61, T0 + 2),
                                       _bot_body(owner_account="x" * 26, iat=int(T0) + 2, exp=int(T0) + 182)))
    assert port.owned_link(AGENT_A, "home") is None and port.linked2("home") == {}
    # the right account, but the username lacks the bridge's prefix: no link
    await bus._on_presence_msg(FakeMsg(f"presence.bot.{AGENT_A}.v1", _presence_reply(bus, 62, 62, T0 + 3),
                                       _bot_body(username="zz-agent-a", iat=int(T0) + 3, exp=int(T0) + 183)))
    assert port.owned_link(AGENT_A, "home") is None
    # back to a good record, then the account record moves the anchor to another account: no link
    await bus._on_presence_msg(FakeMsg(f"presence.bot.{AGENT_A}.v1", _presence_reply(bus, 63, 63, T0 + 4),
                                       _bot_body(iat=int(T0) + 4, exp=int(T0) + 184)))
    assert port.owned_link(AGENT_A, "home") is not None
    await bus._on_presence_msg(FakeMsg(f"presence.account.mattermost.{'v' * 26}.v1",
                                       _presence_reply(bus, 64, 64, T0 + 5),
                                       _account_body(account="v" * 26)))
    assert port.owned_link(AGENT_A, "home") is None  # two accounts bind the anchor: none is the owner
    assert inner.reads == []


async def test_shown_as_and_names_and_kinds_of_accounts_come_from_the_records(monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    assert await lease.shown_as(inner, {AGENT_A, AGENT_B}) == {AGENT_A: "bb-agent-a"}  # type: ignore[arg-type]
    names = await port.usernames({BOT_A, ALICE, AGENT_A})
    # the bot by its record, the person by the account record's name
    assert names == {BOT_A: "bb-agent-a", ALICE: "alice", AGENT_A: AGENT_A}
    assert await port.username(BOT_A) == "bb-agent-a" and await port.username(ALICE) == "alice"
    assert await port.agents({BOT_A, ALICE, AGENT_A}) == {BOT_A, AGENT_A}  # not a person
    assert await port.bot_accounts({BOT_A, ALICE, AGENT_A}) == {BOT_A, AGENT_A}
    assert await port.agent_owner(ALICE) == ""  # a person's account: no agent's owner
    assert await port.agent_owner(BOT_A) == await port.agent_owner(AGENT_A)
    assert inner.reads == []
    # an unbound account's record (no anchor) names no person: Mattermost answers, as with the switch off
    await bus._on_presence_msg(FakeMsg(f"presence.account.mattermost.{ALICE}.v1", _presence_reply(bus, 70, 70, T0 + 1),
                                       _account_body(anchor=None, name=None)))
    inner.names[ALICE] = "alice-mm"
    assert await port.usernames({ALICE}) == {ALICE: "alice-mm"} and inner.reads == ["usernames"]


async def test_an_id_the_records_do_not_know_is_what_mattermost_says_never_a_person(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.37 step 6, RT-6.16: a registrar bot in the lobby (or a bot of a guest channel or of the
    Mattermost half of a split home) has no bot record, and a bridge bot's record can lapse: such
    an id keeps Mattermost's answer as with the switch off, so the gate never takes it for a
    person (OTHER)."""
    from mattermost_ai_agent_bus import gate
    from mattermost_ai_agent_bus.model import Post
    port, bus, inner = await _world(monkeypatch)
    inner.names[STRAY], inner._agents = "registrar-bot", {STRAY, BOT_A}
    inner.descriptions[STRAY] = "for @alice"
    assert await port.agents({STRAY, BOT_A}) == {STRAY, BOT_A}
    assert await port.usernames({STRAY, BOT_A}) == {STRAY: "registrar-bot", BOT_A: "bb-agent-a"}
    assert await port.username(STRAY) == "registrar-bot"
    assert await port.bot_accounts({STRAY}) == {STRAY}
    assert await port.agent_owner(STRAY) == "alice"  # its Mattermost owner, as with the switch off
    post = Post(id="p" * 26, channel_id=WORK, user_id=STRAY, message="hi")
    assert gate.author_class(post, {}, await port.agents({STRAY})) == gate.AGENT
    assert {"usernames", "username", "agents", "bot_accounts", "agent_owner"} <= set(inner.reads)
    # the bridge's record of agent-a lapses (past its 180 s): its bot is again what Mattermost says
    await _presence(bus, "agent-z", 95, T0 + 181, owner="alice")
    inner.names[BOT_A] = "bb-agent-a-mm"
    assert await port.agents({BOT_A}) == {BOT_A}
    assert await port.usernames({BOT_A}) == {BOT_A: "bb-agent-a-mm"}
    assert gate.author_class(dataclasses_replace(post, user_id=BOT_A), {}, await port.agents({BOT_A})) == gate.AGENT


async def test_a_stale_account_record_names_no_person(monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch)
    await _presence(bus, "agent-z", 80, T0 + 901, owner="alice")  # past the record's 15 minutes
    inner.names[ALICE] = "alice-mm"
    assert await port.usernames({ALICE}) == {ALICE: "alice-mm"}  # Mattermost's name, as with the switch off
    assert await port.agents({ALICE}) == set()


# --- gone during a cut ------------------------------------------------------------------------

async def test_nobody_is_told_as_gone_while_the_bus_cannot_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.21: the roster of NATS is what the session last knew; cut off (or not synced) it
    knows nothing about who left, so no mention is answered with a departure."""
    port, bus, inner = await _world(monkeypatch)
    gone = await departed.gone(inner, [AGENT_B])  # type: ignore[arg-type]
    assert gone == []  # agent-b is here
    bus._connected = False
    assert port.readable() is False and lease.nats_unreadable("home")
    assert await departed.gone(inner, [AGENT_A, AGENT_B, "agent-nobody"]) == []  # type: ignore[arg-type]
    assert await lease._rows(inner, WORK, "home") == []  # type: ignore[arg-type]
    bus._connected, bus._synced = True, False
    assert await departed.gone(inner, [AGENT_A]) == []  # type: ignore[arg-type]
    assert inner.reads == []


# --- the switch off -----------------------------------------------------------------------------

async def test_with_the_switch_off_mattermost_answers_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    port, bus, inner = await _world(monkeypatch, piece="")
    assert lease.nats_roster_on("home") is False and lease.nats_unreadable("home") is False
    await port.usernames({BOT_A, STRAY})
    await port.agents({BOT_A})
    await port.bot_accounts({BOT_A})
    await port.agent_owner(STRAY)
    await lease._rows(inner, WORK, "home")  # type: ignore[arg-type]
    assert {"usernames", "agents", "bot_accounts", "agent_owner", "channel_users"} <= set(inner.reads)
    # piece 1 alone does not turn piece 2 on
    monkeypatch.setenv("CB_PLANB_TEST", "1")
    inner.reads.clear()
    await port.usernames({BOT_A})
    assert inner.reads == ["usernames"]


async def test_the_presence_card_carries_usage_and_version_bounded() -> None:
    card = natsbus.PresenceCard(usage="ctx 62%\n · 5h 40%" + "x" * 200, release="v0.104.0 plugin")
    body = json.loads(encode_presence("online", "", "", card))
    assert len(body["usage"]) <= natsbus.CARD_USAGE_MAX and "\n" not in body["usage"]
    assert body["release"] == "v0.104.0 plugin"
    assert "usage" not in json.loads(encode_presence("online", "", "", natsbus.PresenceCard(usage="  ")))
    got = natsbus.decode_presence_card(encode_presence("online", "", "", card))
    assert got.release == "v0.104.0 plugin" and got.usage and got.usage.startswith("ctx 62%")
    assert natsbus.decode_presence_card(b'{"v":1,"status":"online","usage":5,"release":["x"]}') == natsbus.PresenceCard()


# --- a real nats-server -----------------------------------------------------------------------

async def test_a_real_server_delivers_the_bot_record_and_refuses_an_agent_to_publish_one(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge's user (the admin here) publishes a record that an agent's presence consumer
    delivers; an agent's own credential (the rig grants what callout/perms.go grants) is refused."""
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    bus, _ = await rig.agent("alice")
    name = rig.names["alice"]
    now = int(time.time())
    body = json.dumps({"v": 1, "agent": name, "bot": BOT_A, "username": "bb-" + name, "owner_account": ALICE,
                       "iat": now, "exp": now + 180}).encode()
    await rig.admin.publish(f"presence.bot.{name}.v1", body)
    await rig.admin.flush()
    await until(lambda: bus.bot_record(name) is not None, 5, "the bot record")
    rec = bus.bot_record(name)
    assert rec is not None and rec.bot == BOT_A and rec.owner_account == ALICE
    # an agent's credential holds no right on presence.bot.*: what callout/perms.go grants is
    # what the rig grants (the rig's alice may publish only her own subjects), so the server
    # refuses her publish; the permission set itself is pinned in services/auth
    # (callout/perms_test.go, TestNoAgentMayPublishABotRecord)
    nc = bus._nc
    assert nc is not None
    await nc.publish(f"presence.bot.{name}.v1", _bot_body(name, bot=BOT_B))  # the agent's own credential
    await nc.flush()
    await until(lambda: any(d.startswith("presence.bot.") for d in bus._denied), 5,
                "the server's refusal (the subjects the bus saw denied)")
    await asyncio.sleep(0.3)
    assert bus.bot_record(name).bot == BOT_A  # type: ignore[union-attr]  # nothing came of it
