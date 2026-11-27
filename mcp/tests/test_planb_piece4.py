"""Plan B piece 4 (ADR-014 phase 5, RT-6.41 step 6): with the switch `planb.on(4, home)` on,
every check of a trusted human and every agent's owner come from the login service's
attestation and account records, compared by anchor; nothing from the host's Mattermost ids,
a lookup with the bot token or a bot's description. Off, today's path.

Unit tests on the fakes of test_nats_port.py (the switch from CB_PLANB_TEST, which counts only
under pytest), the natsbus reading of the account records, and, with MM_TEST_NATS=1 and
CB_NATS_SERVER, one test against a real nats-server where the switch, the attestation and the
account record come from the PRESENCE stream as the login service writes them.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from fakes import BOT, FakeBus, FakeRegistrar
from test_nats_port import FakeNats, make
from test_natsbus import T0, FakeMsg, Rig, _api, _consumer_specs, _presence_reply, _unit_bus, until  # the rig fixture comes from conftest.py

from mattermost_ai_agent_bus import lease, mandate, owners, planb, server
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.model import ChannelUser, Post, Reaction
from mattermost_ai_agent_bus.nats_port import NatsPort, person_id
from mattermost_ai_agent_bus.natsbus import UNBOUND, Attestation, NatsSettings

HUMAN = "h" * 26      # alice's Mattermost account, bound to the anchor u1
VISITOR = "v" * 26    # a person bound to u9, whom this session does not trust
LOOSE = "l" * 26      # an account without a record
PEERBOT = "p" * 26    # a registrar bot of a peer agent, linked to NAME_A
FARBOT = "f" * 26     # a registrar bot of another owner's agent, linked to NAME_B
NAME_A = "a" * 25
NAME_B = "b" * 25
ALICE = (("u1", "alice"),)


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(planb.TEST_ENV, "4")


def _boom(what: str) -> Any:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"plan B piece 4: a Mattermost call for {what}")
    return boom


def _no_mattermost_trust(monkeypatch: pytest.MonkeyPatch, inner: FakeBus) -> None:
    """Every read the switch takes away fails the test when made."""
    for name in ("trusted_humans", "may_approve", "bot_description", "agent_owner",
                 "agent_attestation", "attestation_record"):
        monkeypatch.setattr(inner, name, _boom(name))


def _attest(fake: FakeNats, agent: str, owner: tuple[str, str], trusted: tuple[tuple[str, str], ...],
            cut: bool = False, exp: float | None = None) -> None:
    fake.owners[agent] = owner[0]
    fake.attestations[agent] = Attestation(owner[0], owner[1], trusted, cut,
                                           fake.server_now + 600 if exp is None else exp)


def _world(**chat: Any) -> tuple[NatsPort, FakeBus, FakeNats]:
    port, inner, fake = make(**chat)
    assert fake is not None
    fake.server_now = 1_000_000.0
    fake.accounts = {HUMAN: "u1", VISITOR: "u9"}
    _attest(fake, fake.name, ("u1", "alice"), ALICE)
    inner.trusted = {HUMAN: "alice", "x" * 26: "bob"}  # what the host's lookup would say
    return port, inner, fake


def _linked_bot(inner: FakeBus, fake: FakeNats, bot_id: str, username: str, name: str,
                tag: str | None = None) -> None:
    """A Mattermost bot whose status names `name`'s link tag (or `tag`) and whose NATS name's
    fresh presence names the bot back (RT-6.37 step 6)."""
    inner.names[bot_id] = username
    inner._agents.add(bot_id)
    shown = name[-26:][:16] if tag is None else tag
    inner.bots[username] = ChannelUser(bot_id, username, 1, custom_status={"text": f"working · ⇄{shown} · ~home"})
    fake.presence[name] = "online"
    fake.bots[name] = bot_id


# --- the switch off: today's path ----------------------------------------------------------------------

async def test_off_the_trusted_humans_and_owners_are_mattermosts_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off, the host's Mattermost ids, the adapter's approval rule and the
    bot's description count as before; the login service's anchors only add people over NATS."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    port, inner, _fake = _world(descriptions={PEERBOT: "for @alice · trusts @alice"})
    assert await port.trusted_humans() == {HUMAN: "alice", "x" * 26: "bob", person_id("u1"): "alice"}
    assert await port.may_approve(HUMAN) is None  # the adapter's rule
    assert await port.bot_description(PEERBOT) == "for @alice · trusts @alice"
    assert await port.attested_circle(PEERBOT) is None
    assert port.trusted_source == inner.trusted_source
    # the reads judge nothing at the read, and a missing attestation closes nothing
    unseen = Post(id="q" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes",
                  reactions=(Reaction(user_id=HUMAN, post_id="q" * 26, emoji_name="+1", create_at=1),))
    assert await port.unapproved([unseen]) == set()
    del _fake.attestations[_fake.name]
    assert not await port.trust_closed()


async def test_the_hooks_ids_count_with_the_switch_off_and_are_ignored_with_it_on(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.41 step 6: the start hook hands over CB_MM_TRUSTED_HUMAN_USER_IDS in every home,
    also one over NATS. With the switch off the server uses them as before; with it on it
    ignores them and takes the trusted humans from its own attestation alone."""
    monkeypatch.setenv("CB_MM_TRUSTED_HUMAN_USER_IDS", "z" * 26)
    monkeypatch.setenv("CB_TRUSTED_HUMANS", "zed")
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    port, inner, _fake = _world()
    inner.trusted = {"z" * 26: "zed"}  # what the client reads from those variables
    assert await port.trusted_humans() == {"z" * 26: "zed", person_id("u1"): "alice"}
    monkeypatch.setenv(planb.TEST_ENV, "4")
    _no_mattermost_trust(monkeypatch, inner)
    assert await port.trusted_humans() == {HUMAN: "alice", person_id("u1"): "alice"}


# --- the trusted humans -------------------------------------------------------------------------------

async def test_on_the_trusted_humans_are_the_attested_anchors_with_no_mattermost_read(
        on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.41 step 6: the trusted humans are the anchors of this session's own fresh
    attestation: each as a person over NATS, and each Mattermost account the login service's
    record binds to one of them; never the host's ids, never a lookup."""
    port, inner, _fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    assert await port.trusted_humans() == {HUMAN: "alice", person_id("u1"): "alice"}
    assert list(await port.trusted_humans())[0] == HUMAN  # a Mattermost account first, for a DM
    assert "login service" in port.trusted_source


@pytest.mark.parametrize("state", ["none", "expired", "another-name", "no-bus"])
async def test_on_without_a_fresh_own_attestation_nobody_is_trusted(
        on: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, state: str) -> None:
    """Fail closed: a missing or expired own attestation (or none for this session's name, or
    no bus) makes nobody a trusted human, and the host's Mattermost ids never come back."""
    port, inner, fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    if state == "none":
        del fake.attestations[fake.name]
    elif state == "expired":
        _attest(fake, fake.name, ("u1", "alice"), ALICE, exp=fake.server_now - 1)
    elif state == "another-name":
        fake.attestations = {NAME_A: fake.attestations.pop(fake.name)}
    else:
        port._bus = None
    caplog.set_level(logging.WARNING)
    assert await port.trusted_humans() == {}
    assert await port.trusted_humans() == {}
    assert await port.may_approve(HUMAN) is False
    assert "none yet" in port.trusted_source
    if state != "no-bus":
        assert sum("no fresh attestation of this session" in r.getMessage() for r in caplog.records) == 1


async def test_on_an_unbound_or_erased_account_is_no_trusted_human(on: None) -> None:
    """An account without a record, one bound to a person this session does not trust, and
    one whose record a stray message erased (the keys still know its anchor) are trusted by
    no one: trust comes only from a record that decodes."""
    port, _inner, fake = _world()
    fake.accounts[LOOSE] = UNBOUND
    trusted = await port.trusted_humans()
    assert VISITOR not in trusted and LOOSE not in trusted
    assert await port.may_approve(VISITOR) is False and await port.may_approve(LOOSE) is False


async def test_the_bus_binds_an_account_only_by_a_record_that_decodes() -> None:
    """natsbus.bound_accounts (RT-6.42 step 3): a record that decodes binds its account to its
    anchor, a stale one too (only `approver` needs it fresh); an erased record (a stray
    message) and `anchor` null bind none, although the keys still know the last anchor."""
    from test_people_over_nats import ACCOUNT, _account
    bus, _ = _unit_bus()
    bus._connected, bus._nc = True, object()  # type: ignore[assignment]

    async def presence(subject: str, seq: int, t: float, data: bytes) -> None:
        await bus._on_presence_msg(FakeMsg(subject, _presence_reply(bus, seq, seq, t), data))

    acct = f"presence.account.mattermost.{ACCOUNT}.v1"
    assert bus.bound_accounts() == {}
    await presence(acct, 1, T0, _account(exp=int(T0) + 900))
    assert bus.bound_accounts() == {ACCOUNT: "dembaca"}
    await presence(f"presence.-.{bus.name}", 2, T0 + 901, b'{"v":1,"status":"online"}')  # stale now
    assert bus.bound_accounts() == {ACCOUNT: "dembaca"} and not bus.approver("dembaca")
    await presence(acct, 3, T0 + 902, b"")  # erased
    assert bus.bound_accounts() == {} and bus.anchor_of_account(ACCOUNT) == "dembaca"
    await presence(acct, 4, T0 + 903, _account(anchor=None, name=None))
    assert bus.bound_accounts() == {}


# --- approvals ------------------------------------------------------------------------------------------

async def test_on_an_approval_needs_the_trusted_anchor_and_a_fresh_approver_record(on: None) -> None:
    """A Mattermost account approves as a person over NATS does (RT-6.42 step 9): bound to a
    trusted anchor and that anchor's record fresh with `approver`; never by a name."""
    port, _inner, fake = _world()
    assert await port.may_approve(HUMAN) is False
    fake.approvers = {"u1", "u9"}
    assert await port.may_approve(HUMAN) is True
    assert await port.may_approve(VISITOR) is False  # an approver, but no trusted human here
    assert await port.may_approve(person_id("u1")) is True  # a person over NATS, as before


async def test_on_a_trusted_humans_event_on_mattermost_without_approver_is_marked_for_the_reads(on: None) -> None:
    """A trusted human's post or reaction that comes on Mattermost is judged as it comes: the
    reads mark it not_approver when no fresh approver record stood; one with it is not."""
    port, _inner, fake = _world()
    post = Post(id="q" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes")
    other = Post(id="r" * 26, channel_id="c" * 26, user_id=VISITOR, message="yes")
    assert not await port.skip(PostEvent(post=post, channel_type=None, edited=False))
    assert not await port.skip(PostEvent(post=other, channel_type=None, edited=False))
    react = Reaction(user_id=HUMAN, post_id="a" * 26, emoji_name="+1", create_at=1)
    assert not await port.skip(ReactionEvent(react, "c" * 26))
    asked = Post(id="a" * 26, channel_id="c" * 26, user_id=BOT, message="ask", reactions=(react,))
    # the visitor is bound to a person this session does not trust: no approver either (the
    # reads show not_approver only on a trusted human's, slim.py)
    assert await port.unapproved([post, other, asked]) == {
        ("post", post.id), ("post", other.id), ("reaction", asked.id, "+1", HUMAN)}
    fake.approvers = {"u1"}
    later = Post(id="s" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes")
    assert not await port.skip(PostEvent(post=later, channel_type=None, edited=False))
    assert ("post", later.id) not in await port.unapproved([later])


@pytest.mark.parametrize("gap", ["down", "no-attestation"])
async def test_on_a_non_approvers_answer_the_session_did_not_see_come_answers_no_ask(on: None, gap: str) -> None:
    """RT-6.42 step 9 at the read (the unmarked reads of the #264 review): a reaction or reply of a bound
    account that the session did not judge as it came, because it was down or reconnecting
    or read the thread from history, is judged by that account's record at the read; without
    a fresh approver record it answers no ask (tasks.ask_answered, as remind and the open
    asks use it). One that came while the session had no own attestation was judged then,
    without approval, and stays so. A live approval stays one after the record goes stale."""
    from mattermost_ai_agent_bus import tasks
    port, _inner, fake = _world()
    ask = Post(id="a" * 26, channel_id="c" * 26, user_id=BOT, message="ask", create_at=1)
    react = Reaction(user_id=HUMAN, post_id=ask.id, emoji_name="+1", create_at=2)
    reply = Post(id="b" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes", root_id=ask.id, create_at=3)
    names = {HUMAN: "alice"}
    if gap == "no-attestation":  # it came, but this session had no attestation of its own then
        att = fake.attestations.pop(fake.name)
        assert not await port.skip(ReactionEvent(react, ask.channel_id))
        assert not await port.skip(PostEvent(post=reply, channel_type=None, edited=False))
        fake.attestations[fake.name] = att
    asked = Post(**{**ask.__dict__, "reactions": (react,)})

    async def answered() -> bool:
        unapproved = await port.unapproved([asked, reply])
        return tasks.ask_answered(asked, [asked, reply], [react], names, "alice",
                                  await port.trusted_humans(), unapproved)

    assert HUMAN in await port.trusted_humans()
    assert await port.unapproved([asked, reply]) == {("post", reply.id), ("reaction", ask.id, "+1", HUMAN)}
    assert not await answered()
    fake.approvers = {"u1"}  # the account's own record is fresh with approver now
    assert await answered() is (gap == "down")  # unseen: judged now; seen without approval: stays
    # a live approval is not taken back by the read once the record goes stale
    later = Post(id="d" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes", root_id=ask.id, create_at=4)
    assert not await port.skip(PostEvent(post=later, channel_type=None, edited=False))
    fake.approvers = set()
    assert ("post", later.id) not in await port.unapproved([later])
    assert tasks.ask_answered(ask, [ask, later], [], names, "alice", await port.trusted_humans(),
                              await port.unapproved([ask, later]))


async def test_on_an_account_approves_only_by_its_own_fresh_record(on: None) -> None:
    """A stale binding (the #264 review): two Mattermost accounts bound to one person (u1). The one
    whose own record is stale approves nothing, although the other's fresh approver record
    makes the person an approver; it stays a trusted human (a stale record still names the
    person), and its event is marked not_approver."""
    port, _inner, fake = _world()
    second = "k" * 26
    fake.accounts[second] = "u1"
    fake.approvers = {"u1"}  # by the second account's fresh record
    fake.stale = {HUMAN}
    assert fake.approver("u1")
    assert await port.may_approve(HUMAN) is False
    assert await port.may_approve(second) is True
    assert HUMAN in await port.trusted_humans() and second in await port.trusted_humans()
    post = Post(id="q" * 26, channel_id="c" * 26, user_id=HUMAN, message="yes")
    other = Post(id="r" * 26, channel_id="c" * 26, user_id=second, message="yes")
    assert not await port.skip(PostEvent(post=post, channel_type=None, edited=False))
    assert not await port.skip(PostEvent(post=other, channel_type=None, edited=False))
    assert await port.unapproved([post, other]) == {("post", post.id)}


async def test_the_bus_approves_an_account_by_its_own_fresh_record() -> None:
    """natsbus.account_approver: an account approves by its own record alone, fresh (exp after
    the server's now) with `approver`; natsbus.approver(anchor) stays true by any of them."""
    from test_people_over_nats import ACCOUNT, _account
    bus, _ = _unit_bus()
    bus._connected, bus._nc = True, object()  # type: ignore[assignment]
    second = "k" * 26

    async def presence(subject: str, seq: int, t: float, data: bytes) -> None:
        await bus._on_presence_msg(FakeMsg(subject, _presence_reply(bus, seq, seq, t), data))

    await presence(f"presence.account.mattermost.{ACCOUNT}.v1", 1, T0, _account(exp=int(T0) + 900))
    await presence(f"presence.account.mattermost.{second}.v1", 2, T0 + 600,
                   _account(account=second, exp=int(T0) + 1500))
    assert bus.account_approver(ACCOUNT) and bus.account_approver(second)
    await presence(f"presence.-.{bus.name}", 3, T0 + 901, b'{"v":1,"status":"online"}')  # the first stale
    assert bus.approver("dembaca") and bus.bound_accounts() == {ACCOUNT: "dembaca", second: "dembaca"}
    assert not bus.account_approver(ACCOUNT) and bus.account_approver(second)
    assert not bus.account_approver("z" * 26)
    await presence(f"presence.account.mattermost.{second}.v1", 4, T0 + 902, _account(account=second, approver=False,
                                                                                     exp=int(T0) + 1500))
    assert not bus.account_approver(second) and not bus.approver("dembaca")


async def test_on_the_events_say_from_trusted_human_by_anchor(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.8 step 3 by anchor: a post by the account bound to a trusted anchor is
    from_trusted_human (not_approver without a fresh approver record); the visitor's and an
    unbound account's are not; nothing reads the Mattermost trust."""
    from mattermost_ai_agent_bus import gate
    from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent
    port, inner, fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    monkeypatch.setattr(server, "_get_client", lambda: port)
    monkeypatch.setattr(gate, "_coord", None)
    posts = [Post(id=c * 26, channel_id="c" * 26, user_id=u, message="hello")
             for c, u in (("1", HUMAN), ("2", VISITOR), ("3", LOOSE))]
    items = await server._event_items([ClassifiedEvent(kind="watched_channel", post=p) for p in posts])
    assert [i["from_trusted_human"] for i in items] == [True, False, False]
    assert items[0]["trusted_human"] == "alice" and items[0].get("not_approver") is True
    fake.approvers = {"u1"}
    [item] = await server._event_items([ClassifiedEvent(kind="watched_channel", post=posts[0])])
    assert item["from_trusted_human"] and "not_approver" not in item


# --- owners: peer or foreign ----------------------------------------------------------------------------

async def test_on_a_peer_is_classed_by_its_linked_name_with_no_description_read(
        on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A peer's owner from its NATS name, which presence proves, and its trusted list from the
    login service's attestation: a registrar bot linked to a peer's name is a peer, one linked
    to another owner's name is foreign; no description, attestation or native owner is read."""
    port, inner, fake = _world(descriptions={PEERBOT: "for @alice · trusts @alice",
                                             FARBOT: "for @alice · trusts @alice"})
    _no_mattermost_trust(monkeypatch, inner)
    _linked_bot(inner, fake, PEERBOT, "agent-peer", NAME_A)
    _linked_bot(inner, fake, FARBOT, "agent-far", NAME_B)
    _attest(fake, NAME_A, ("u1", "alice"), ALICE)
    _attest(fake, NAME_B, ("u5", "carol"), (("u5", "carol"),))
    assert await owners.peer(port, PEERBOT)  # type: ignore[arg-type]
    assert await owners.foreign(port, {PEERBOT, FARBOT}) == {FARBOT: "carol"}  # type: ignore[arg-type]
    agent = await owners.read(port, PEERBOT)  # type: ignore[arg-type]
    assert agent is not None and agent.by_anchor and agent.owner == "u1" and agent.display() == "alice"
    assert await owners.peer(port, NAME_A) and not await owners.peer(port, NAME_B)  # type: ignore[arg-type]


@pytest.mark.parametrize("break_it", ["no-tag", "other-tag", "presence-names-another", "offline",
                                      "no-attestation", "cut"])
async def test_on_a_bot_without_a_proven_link_and_attestation_is_foreign(
        on: None, monkeypatch: pytest.MonkeyPatch, break_it: str) -> None:
    """In doubt foreign: a bot whose description says it is alice's is foreign without the
    two-sided link to a fresh NATS name with a usable attestation, or with a cut one."""
    port, inner, fake = _world(descriptions={PEERBOT: "for @alice · trusts @alice"})
    _no_mattermost_trust(monkeypatch, inner)
    _linked_bot(inner, fake, PEERBOT, "agent-peer", NAME_A,
                tag="" if break_it == "no-tag" else "b" * 16 if break_it == "other-tag" else None)
    _attest(fake, NAME_A, ("u1", "alice"), ALICE, cut=break_it == "cut")
    if break_it == "presence-names-another":
        fake.bots[NAME_A] = FARBOT
    elif break_it == "offline":
        fake.presence[NAME_A] = "offline"
    elif break_it == "no-attestation":
        del fake.attestations[NAME_A]
    assert not await owners.peer(port, PEERBOT)  # type: ignore[arg-type]
    assert PEERBOT in await owners.foreign(port, {PEERBOT})  # type: ignore[arg-type]


async def test_on_a_bot_no_fresh_presence_names_costs_no_mattermost_read(
        on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Mattermost bot that no fresh NATS presence names has no owner, decided without a read."""
    port, inner, _fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    monkeypatch.setattr(inner, "usernames", _boom("usernames"))
    monkeypatch.setattr(inner, "bots_named", _boom("bots_named"))
    assert await port.agent_owner(PEERBOT) == ""
    assert await port.attested_circle(PEERBOT) is None


async def test_on_my_own_bot_takes_my_attestation_and_its_absence_keeps_the_split_on(
        on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The session's own owner and trusted list come from its own attestation; without one my
    bot's owner is not readable (None), so the owner split stays on in doubt (ADR-016) and no
    agent becomes a peer by the gap."""
    monkeypatch.setenv("CB_TRUSTED_HUMANS", "alice")
    port, inner, fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    _linked_bot(inner, fake, PEERBOT, "agent-peer", NAME_A)
    _attest(fake, NAME_A, ("u1", "alice"), ALICE)
    assert await port.agent_owner(BOT) == "alice"
    names, me = await owners.mine(port)  # type: ignore[arg-type]
    assert names == {"alice"} and me == BOT
    del fake.attestations[fake.name]
    owners.reset()
    assert await port.agent_owner(BOT) is None
    names, _ = await owners.mine(port)  # type: ignore[arg-type]
    assert names is owners.NOBODY  # the split stays on, and not by the configured names
    assert not await owners.peer(port, PEERBOT)  # type: ignore[arg-type]  # no own anchors: foreign


async def test_on_without_a_fresh_own_attestation_fail_closed_needs_no_host_variable(
        on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed without the host (the #264 review): with no trusted variable set (a harness without the corral
    hook) and no fresh own attestation, the gate stays on and names_in_doubt gives NOBODY, also
    right after names were read (no grace for the last ones): no agent becomes a peer and
    nobody's post passes the gate. With the attestation back, its names. With the switch off
    and nothing configured, the gate is off as before."""
    from mattermost_ai_agent_bus import gate
    assert not owners.configured()
    port, inner, fake = _world()
    _linked_bot(inner, fake, PEERBOT, "agent-peer", NAME_A)
    _attest(fake, NAME_A, ("u1", "alice"), ALICE)
    assert not await port.trust_closed()
    assert await gate.active(port) and await owners.peer(port, PEERBOT)  # type: ignore[arg-type]
    assert await owners.names_in_doubt(port) == ({"alice"}, BOT)  # type: ignore[arg-type]
    att = fake.attestations.pop(fake.name)
    assert await port.trust_closed()
    assert await gate.active(port)
    names, me = await owners.names_in_doubt(port)  # type: ignore[arg-type]
    assert names is owners.NOBODY and me == BOT
    assert not await owners.peer(port, PEERBOT)  # type: ignore[arg-type]
    assert PEERBOT in await owners.foreign(port, {PEERBOT})  # type: ignore[arg-type]
    fake.attestations[fake.name] = att
    assert await owners.names_in_doubt(port) == ({"alice"}, BOT)  # type: ignore[arg-type]
    monkeypatch.delenv(planb.TEST_ENV)
    del fake.attestations[fake.name]
    inner.trusted = {}
    assert not await port.trust_closed() and not await gate.active(port)


async def test_on_the_crown_compares_by_anchor_before_any_record_was_seen(on: None) -> None:
    """With the switch on, a claimant without a usable record never stands by a name in its
    presence subject, also before any record was seen in this process (records_on)."""
    port, _inner, fake = _world()
    fake.attestations.clear()  # no record of anyone
    assert port._records_on()
    fake.owners[NAME_A] = "alice"  # before the import an anchor is a name
    assert await port.agent_owner(NAME_A) == ""


async def test_on_the_crown_reads_the_trusted_names_without_a_lookup(on: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """NatsPort.prime (RT-6.38) takes the names of this session's own attestation."""
    port, inner, _fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    await port.prime()
    assert port._names == {"alice"}


async def test_on_a_linked_mirror_keeps_its_crown_after_the_import(on: None) -> None:
    """lease._linked_owned: the bot's owner (the linked name's attested one, by anchor) is
    compared by the name it shows, so a mirror stays linked when anchor and name differ."""
    port, inner, fake = _world()
    _linked_bot(inner, fake, PEERBOT, "agent-peer", NAME_A)
    _attest(fake, NAME_A, ("sub-1", "alice"), (("sub-1", "alice"),))
    rows = [{"id": PEERBOT, "text": f"working · ⇄{NAME_A[-26:][:16]} · ~home"}]
    assert await lease._linked_owned(port, port, rows) == {PEERBOT: NAME_A}  # type: ignore[arg-type]


# --- the mandate ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("author,counts", [(HUMAN, True), (VISITOR, False), (LOOSE, False)])
async def test_on_a_mandate_counts_only_by_a_trusted_anchor(
        on: None, monkeypatch: pytest.MonkeyPatch, author: str, counts: bool) -> None:
    """RT-6.26 step 2 by anchor: a pinned mandate counts when its author's account is bound to
    a trusted anchor of this session's attestation; a visitor's or an unbound one's does not."""
    port, inner, _fake = _world()
    _no_mattermost_trust(monkeypatch, inner)
    inner.names.update({HUMAN: "alice", VISITOR: "visitor", LOOSE: "loose"})
    inner.pinned = [{"id": "m" * 26, "user_id": author, "channel_id": "c" * 26,
                     "message": "mandate: \U0001F451 of ~other", "create_at": int(time.time() * 1000) - 60_000,
                     "is_pinned": True}]
    got, why = await mandate.current(port)  # type: ignore[arg-type]
    assert (got is not None) is counts, why
    if counts:
        assert got is not None and got.by == "alice" and got.by_id == HUMAN


# --- against a real server ----------------------------------------------------------------------------------

async def _agent4(rig: Rig, who: str) -> tuple[NatsPort, FakeBus]:
    """An agent whose presence consumer delivers the login service's records, as
    consumers/manager.go makes it: attestations, account records and the home's plan B record."""
    inner = FakeBus(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    name = rig.names[who]
    for stream, cname, cfg in _consumer_specs(name, ["home"], {}):
        if cname.endswith("_presence"):
            cfg = cfg | {"filter_subjects": [*cfg["filter_subjects"], "presence.attest.*.v1",
                                             "presence.account.mattermost.*.v1"]}  # planb: _consumer_specs
        await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.{stream}.{cname}", {"stream_name": stream, "config": cfg})
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home="home"), ("home",),  # type: ignore[arg-type]
                    bus_options={"heartbeat_sec": 0.2})
    await port.start(rig.logins[who], owner=who)
    return port, inner


async def _publish(rig: Rig, subject: str, body: dict[str, Any]) -> None:
    """As the login service writes a record (the streams acknowledge nothing: no_ack)."""
    await rig.admin.publish(subject, json.dumps(body).encode())
    await rig.admin.flush()


async def test_against_a_real_server_the_switch_and_the_records_decide(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch from the home's plan B record, this session's attestation and a person's
    account record, all on the PRESENCE stream as the login service writes them: the trusted
    humans are the attested anchor and the bound account, an approval needs the approver
    record, and no Mattermost trust is read. Before the record names piece 4, the old path."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    now = int(time.time())
    await _publish(rig, "presence.planb.home.v1", {"v": 1, "channel": "home", "pieces": []})
    port, inner = await _agent4(rig, "alice")
    try:
        inner.trusted = {"x" * 26: "bob"}
        name = rig.names["alice"]
        await _publish(rig, f"presence.attest.{name}.v1", {
            "v": 1, "agent": name, "owner": {"anchor": "alice", "name": "alice"},
            "trusted": [{"anchor": "alice", "name": "alice"}], "cut": False, "iat": now, "exp": now + 900})
        for account, anchor, approver in ((HUMAN, "alice", True), (VISITOR, "carol", True)):
            await _publish(rig, f"presence.account.mattermost.{account}.v1", {
                "v": 1, "account": account, "anchor": anchor, "name": anchor, "approver": approver,
                "token_iat": now, "iat": now, "exp": now + 900})
        bus = port._bus
        assert bus is not None
        await until(lambda: bus.attestation_of(name) is not None and len(bus.bound_accounts()) == 2,
                    what="the attestation and the account records")
        assert not planb.on(4, "home")
        assert "x" * 26 in await port.trusted_humans()  # off: the host's ids, as before
        await _publish(rig, "presence.planb.home.v1", {"v": 1, "channel": "home", "pieces": [4]})
        await until(lambda: planb.on(4, "home"), what="the plan B record naming piece 4")
        _no_mattermost_trust(monkeypatch, inner)
        assert await port.trusted_humans() == {HUMAN: "alice", person_id("alice"): "alice"}
        assert await port.may_approve(HUMAN) is True and await port.may_approve(VISITOR) is False
    finally:
        await port.stop()
