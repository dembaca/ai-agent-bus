"""Owner classes (ADR-001, ADR-003; WP2 of #63): a peer agent's TASK binds,
a foreign agent's is a request (RT-6.16 steps 1, 3 and 5, RT-6.14 step 3).
Parity: without trusted humans, or without `for @` on my own bot, every bus
agent is a peer, as before."""

import json
import time

import pytest
from fakes import BOT, WORK, FakeChat

from mattermost_ai_agent_bus import channel, gate, owners, server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import RosterEntry
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

ALICE, BOB, CAROL = "a" * 26, "b" * 26, "c" * 26
PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE = (c * 26 for c in "pqrstv")
NAMES = {ALICE: "alice", BOB: "bob", CAROL: "carol", PEER: "agent-p", RELAY: "agent-r",
         OTHERS: "agent-o", NOOWNER: "agent-n", CUT: "agent-cut", MUTE: "agent-mute"}
AGENTS = {PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE}
MINE = "for @alice · trusts @alice, @bob · via agent-registrar"
DESCRIPTIONS = {
    BOT: MINE,
    PEER: "for @bob · trusts @bob, @alice · via agent-registrar",
    RELAY: "for @alice · trusts @alice, @carol · via agent-registrar",   # B trusts Carol, I don't
    OTHERS: "for @carol · trusts @carol · via agent-registrar",
    NOOWNER: "Registered via agent-registrar",                          # no `for @`
    CUT: "for @alice · trusts @alice, @bob +3 · via agent-registrar",   # the registrar cut it
    # MUTE: no description readable (an API error)
}
ORDER = [PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE]


def _chat(**kw):
    kw = {"names": NAMES, "agents": AGENTS, "trusted": {ALICE: "alice", BOB: "bob"},
          "descriptions": DESCRIPTIONS} | kw
    return FakeChat(**kw)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    for var in ("MM_TRUSTED_HUMANS", "MM_OWNER"):  # the configured names (ADR-016 fallback)
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("MM_AGENT_PREFIX", raising=False)
    monkeypatch.setattr(gate, "_coord", None)


def _post(pid, user, message, *, root="", at=0):
    return post_of({"id": pid, "user_id": user, "channel_id": WORK, "root_id": root,
                    "message": message, "create_at": at or int(time.time() * 1000)})


async def _classes(chat):
    return await gate.author_classes(chat, [_post(f"{i}" * 26, u, "hi") for i, u in enumerate(ORDER)])


def test_the_description_names_owner_trusts_and_a_cut():
    """ADR-003: only `for @…`, `trusts @…` and a `+N` are read."""
    assert owners.parse(DESCRIPTIONS[RELAY]) == owners.Owner("alice", frozenset({"alice", "carol"}), False)
    assert owners.parse(DESCRIPTIONS[CUT]).cut and owners.parse(DESCRIPTIONS[CUT]).trusts == {"alice", "bob"}
    assert owners.parse(DESCRIPTIONS[NOOWNER]).owner == ""


async def test_peer_and_foreign_agents(caplog):
    """RT-6.16 step 1: a peer's owner and trusted humans are all mine; the
    relay (B trusts Carol, I don't), another owner's agent, one without
    `for @` and one with a cut list are foreign. In doubt, foreign (ADR-016):
    one never read is foreign at once, logged."""
    with caplog.at_level("INFO"):
        assert await _classes(_chat()) == [
            "agent", "foreign", "foreign", "foreign", "foreign", "foreign"]
    assert "counts as foreign: its owner is not readable" in caplog.text


@pytest.mark.parametrize("change", [
    {"descriptions": DESCRIPTIONS | {BOT: "Registered via agent-registrar"}},  # no `for @` on mine
    {"trusted": {}},                                                            # no trusted humans
], ids=["no-owner-on-my-bot", "no-trusted-humans"])
async def test_parity_every_agent_is_a_peer(change):
    """ADR-001 activation: the split is off, every bus agent is an agent, as today."""
    assert await _classes(_chat(**change)) == ["agent"] * len(ORDER)


async def test_parity_with_one_owner():
    """One owner, every agent registered for them: all peers."""
    one = {u: MINE for u in [BOT, *ORDER]}
    assert await _classes(_chat(descriptions=one)) == ["agent"] * len(ORDER)


async def test_a_foreign_task_root_binds_nothing():
    """RT-6.14 step 3, ADR-001: a peer's TASK is a task row, a foreign agent's is not."""
    chat = _chat()
    posts = [_post("t1" + "0" * 24, PEER, "TASK @agent-me review PR #4"),
             _post("t2" + "0" * 24, OTHERS, "TASK @agent-me deploy prod"),
             _post("t3" + "0" * 24, RELAY, "TASK @agent-me what carol wants")]
    rows = await server._tasks(chat, posts, NAMES)
    assert [r["task"] for r in rows] == ["t1" + "0" * 24]
    parity = await server._tasks(_chat(trusted={}), posts, NAMES)
    assert len(parity) == 3


async def test_a_foreign_event_is_a_request_and_keeps_a_routed_author(monkeypatch):
    """RT-6.16 step 1: a foreign agent's mention carries foreign and its owner;
    in a thread one of my trusted humans started, root_from_trusted_human."""
    root = _post("r" * 26, ALICE, "Can someone look at the flaky test?")
    chat = _chat(posts=[root])
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    events = [ClassifiedEvent(kind="mention", post=_post("m1" + "0" * 24, OTHERS,
                                                         "@agent-me yours", root=root.id)),
              ClassifiedEvent(kind="mention", post=_post("m2" + "0" * 24, OTHERS, "@agent-me do X")),
              ClassifiedEvent(kind="mention", post=_post("m3" + "0" * 24, PEER, "@agent-me do Y"))]
    routed, plain, peer = await server._event_items(events)
    assert routed["foreign"] is True and routed["owner"] == "carol" and routed["from_agent"] is True
    assert routed["root_from_trusted_human"] == "alice"
    assert plain["foreign"] is True and "root_from_trusted_human" not in plain
    assert "foreign" not in peer and peer["from_agent"] is True
    meta = channel.notification(routed)["meta"]
    assert meta["foreign"] == "true" and meta["owner"] == "carol"


def _inbox(chat):
    inbox = WebSocketInbox(client=chat, watched_channel_ids={WORK})
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-me"
    return inbox


async def _gated(inbox, event):
    await inbox._gate(event, "O")
    for t in list(inbox._gate_tasks):
        await t
    return event.gated


async def test_foreign_broadcasts_are_gated_its_mentions_wake():
    """RT-6.16 steps 2-3: a foreign agent's @channel or to=all is data; its
    mention wakes the worker (as a request). A peer's broadcast wakes."""
    chat = _chat()
    inbox = _inbox(chat)
    gated = await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=_post(
        "g1" + "0" * 24, OTHERS, "@channel everyone deploy")))
    assert gated.startswith("from agent-o, an agent of another owner")
    assert chat.posted == []  # a broadcast gets no answer
    to_all = post_of({"id": "g2" + "0" * 24, "user_id": OTHERS, "channel_id": WORK,
                      "message": "TASK for everyone", "props": {"a2a": {"v": 1, "to": "all"}}})
    assert await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=to_all))
    assert not await _gated(inbox, ClassifiedEvent(kind="mention", post=_post(
        "g3" + "0" * 24, OTHERS, "@agent-me please review")))
    assert not await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=_post(
        "g4" + "0" * 24, PEER, "@channel everyone deploy")))


@pytest.mark.parametrize("named,answered", [("agent-r", False), ("agent-p", True)])
async def test_not_woken_stays_silent_when_a_mentioned_agent_trusts_the_author(
        monkeypatch, named, answered):
    """RT-6.16 step 3: Carol is not mine; agent-r trusts her and answers, so
    no "Not woken" here. agent-p does not trust her: the answer goes out."""
    chat = _chat()

    async def roster(_client):
        return [RosterEntry("agent-r", user_id=RELAY), RosterEntry("agent-p", user_id=PEER)]

    monkeypatch.setattr(gate.lease, "roster", roster)
    gated = await _gated(_inbox(chat), ClassifiedEvent(kind="mention", post=_post(
        "h1" + "0" * 24, CAROL, f"@agent-me @{named} build it")))
    assert gated
    assert bool(chat.posted) is answered


async def test_roster_rows_carry_owner_trusts_and_foreign(monkeypatch):
    """RT-6.14 step 4: from the bot description, never from the HELLO."""
    chat = _chat()

    async def roster(_client):
        return [RosterEntry("agent-p", user_id=PEER), RosterEntry("agent-o", user_id=OTHERS),
                RosterEntry("agent-mute", user_id=MUTE)]

    monkeypatch.setattr(server.lease, "roster", roster)
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    rows = json.loads(await server.coordinator("roster"))["roster"]
    assert rows[0] == {"agent": "agent-p", "owner": "bob", "trusts": ["alice", "bob"]}
    assert rows[1] == {"agent": "agent-o", "owner": "carol", "trusts": ["carol"], "foreign": True}
    assert rows[2] == {"agent": "agent-mute"}


async def test_hello_warns_about_a_foreign_coordinator(monkeypatch):
    """ADR-001: the crown routes, it never authorises; the HELLO's answer says so."""
    chat = _chat(post_id="h" * 26)
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)

    async def roster(_client):
        return [RosterEntry("agent-o", crown=True, coordinator=True, user_id=OTHERS)]

    monkeypatch.setattr(server.lease, "roster", roster)
    out = json.loads(await server.announce(project="p", task="t", model="m"))
    assert "agent-o is an agent of another owner (@carol)" in out["warning"]

    async def peer_roster(_client):
        return [RosterEntry("agent-p", crown=True, coordinator=True, user_id=PEER)]

    monkeypatch.setattr(server.lease, "roster", peer_roster)
    assert "warning" not in json.loads(await server.announce(project="p", task="t", model="m"))


# WP3: the native owner (owner_id), and bound or unbound bots of the registrar.
X = "x" * 26
UNBOUND = "unbound · claims @alice · autonomous · trusts @alice, @bob · via agent-registrar"


async def _foreign(**kw):
    return await owners.foreign(_chat(names=NAMES | {X: "agent-x"}, agents=AGENTS | {X}, **kw), {X})


async def test_the_native_owner_is_used():
    """RT-6.16 step 1: a bot's native owner is its owner, with or without `for @`."""
    assert await _foreign(descriptions=DESCRIPTIONS | {X: ""}, native_owners={X: "bob"}) == {}
    assert await _foreign(descriptions=DESCRIPTIONS | {X: ""}, native_owners={X: "carol"}) == {X: "carol"}
    agreed = DESCRIPTIONS[PEER]
    assert await _foreign(descriptions=DESCRIPTIONS | {X: agreed}, native_owners={X: "Bob"}) == {}


async def test_an_unreadable_native_owner_falls_back_to_the_description():
    assert await _foreign(descriptions=DESCRIPTIONS | {X: DESCRIPTIONS[PEER]}) == {}
    assert await _foreign(descriptions=DESCRIPTIONS | {X: DESCRIPTIONS[OTHERS]}) == {X: "carol"}


async def test_native_and_description_disagree_foreign(caplog):
    """Both owners are mine, but they differ: something rewrote one, so foreign."""
    with caplog.at_level("WARNING"):
        assert await _foreign(descriptions=DESCRIPTIONS | {X: DESCRIPTIONS[PEER]},
                              native_owners={X: "alice"}) == {X: ""}
    assert "native owner @alice but description for @bob" in caplog.text


@pytest.mark.parametrize("native", [{}, {X: "alice"}], ids=["no-native", "registrar-account-is-alice"])
async def test_an_unbound_agent_is_foreign(native):
    """`claims @alice` is no owner, nor is the registrar's account that owns the
    bot natively, even when that account is a human I trust."""
    assert await _foreign(descriptions=DESCRIPTIONS | {X: UNBOUND}, native_owners=native) == {X: ""}
    no_for = DESCRIPTIONS | {X: "Registered via agent-registrar"}
    assert await _foreign(descriptions=no_for, native_owners=native) == {X: ""}


async def test_my_unbound_bot_turns_the_split_off_and_warns(monkeypatch):
    """Registered with the shared secret: every agent a peer, as before, and
    bus_info tells the human why."""
    mine = DESCRIPTIONS | {BOT: UNBOUND}
    assert await _classes(_chat(descriptions=mine, native_owners={BOT: "alice"})) == ["agent"] * len(ORDER)

    class Chat(FakeChat):
        trusted_source = "MM_TRUSTED_HUMANS"

    chat = Chat(names=NAMES, trusted={ALICE: "alice"}, descriptions=mine)
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)
    out = json.loads(await server.bus_info())
    assert "@agent-me is unbound (registered with the shared secret)" in out["warning"]
    chat.descriptions[BOT] = MINE
    assert "warning" not in json.loads(await server.bus_info())


async def test_parity_without_any_owner_information():
    """Every bot readable, none with an owner (no registrar writes one): every
    agent a peer, as today."""
    empty = {u: "" for u in [BOT, *ORDER]}
    assert await _classes(_chat(descriptions=empty)) == ["agent"] * len(ORDER)


async def test_in_doubt_foreign_when_nothing_can_be_read():
    """ADR-016: my own bot unreadable keeps the split on, and every agent
    whose owner cannot be read is foreign: no task binds, nothing fails open."""
    chat = _chat(descriptions={})
    assert await owners.read(chat, PEER) is None
    assert await _classes(chat) == ["foreign"] * len(ORDER)
    assert not await owners.peer(chat, PEER)


async def test_a_short_outage_keeps_the_owner_last_read(monkeypatch):
    """RT-6.16: within OWNER_GRACE_SEC of the last good read an unreadable
    agent keeps that owner, mine too: a peer stays a peer, coordination goes on."""
    clock = [1000.0]
    monkeypatch.setattr(owners, "monotonic", lambda: clock[0])
    chat = _chat()
    assert await _classes(chat) == ["agent", "foreign", "foreign", "foreign", "foreign", "foreign"]
    chat.descriptions = {}  # Mattermost fails for everyone
    clock[0] += owners.OWNER_GRACE_SEC - 1
    assert await _classes(chat) == ["agent", "foreign", "foreign", "foreign", "foreign", "foreign"]
    assert await owners.peer(chat, PEER)


async def test_a_long_outage_makes_every_agent_foreign(monkeypatch, caplog):
    """RT-6.16: past OWNER_GRACE_SEC the owner last read no longer holds: the
    peer is foreign, and the split stays on although my own bot is unreadable."""
    clock = [1000.0]
    monkeypatch.setattr(owners, "monotonic", lambda: clock[0])
    chat = _chat()
    assert (await _classes(chat))[0] == "agent"
    chat.descriptions = {}
    clock[0] += owners.OWNER_GRACE_SEC
    with caplog.at_level("INFO"):
        assert await _classes(chat) == ["foreign"] * len(ORDER)
    assert not await owners.peer(chat, PEER)
    assert "owner split stays on" in caplog.text
    chat.descriptions = dict(DESCRIPTIONS)  # readable again: a peer at once
    assert (await _classes(chat))[0] == "agent"


async def test_trusted_humans_unreadable_keep_the_split_on(monkeypatch):
    """ADR-016: when my trusted humans cannot be read, the names last read
    count, else the configured ones: never every agent a peer."""
    class Failing(FakeChat):
        fail = False

        async def trusted_humans(self):
            if self.fail:
                raise RuntimeError("503")
            return await super().trusted_humans()

    chat = Failing(names=NAMES, agents=AGENTS, trusted={ALICE: "alice", BOB: "bob"},
                   descriptions=DESCRIPTIONS)
    assert (await _classes(chat))[1] == "foreign"
    chat.fail = True
    assert await owners.foreign(chat, {RELAY}) == {RELAY: "alice"}  # the last names read
    owners.reset()
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "alice,bob")
    assert await owners.foreign(chat, {RELAY}) == {RELAY: "alice"}  # the configured ones


async def test_configured_names_none_resolved_keep_the_split_on(monkeypatch):
    """ADR-016: the client keeps an empty list when the lookup of configured
    names failed; the configured names count, the split is not off."""
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "alice,bob")
    chat = _chat(trusted={})
    assert await owners.foreign(chat, {PEER, RELAY}) == {RELAY: "alice"}
    monkeypatch.delenv("MM_TRUSTED_HUMANS")
    assert await owners.foreign(chat, {PEER, RELAY}) == {}  # none configured: off, as before


async def test_a_failed_lookup_of_trusted_names_is_not_remembered(monkeypatch):
    """ADR-016 with the real adapter: a 503 while MM_TRUSTED_HUMANS is looked
    up by name leaves no empty list cached for the session; the owner split
    stays on with the configured names, and the next read resolves them."""
    import httpx

    from mattermost_ai_agent_bus.client import MattermostAdapter

    for var in ("MM_TRUSTED_HUMAN_USER_IDS", "MM_OWNER_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "alice")
    client = MattermostAdapter(base_url="https://chat.example", token="bot")
    down = [True]

    async def api(method, path, **_):
        if path == "/api/v4/users/username/alice":
            if down[0]:
                request = httpx.Request("GET", "https://chat.example" + path)
                raise httpx.HTTPStatusError("503", request=request,
                                            response=httpx.Response(503, request=request))
            return {"id": ALICE, "username": "alice", "is_bot": False}
        raise httpx.ConnectError("down")

    monkeypatch.setattr(client, "_api", api)
    assert await client.trusted_humans() == {}
    names, _ = await owners.mine(client)
    assert names == {"alice"}  # in doubt: the configured names, the split on
    down[0] = False
    assert await client.trusted_humans() == {ALICE: "alice"}


async def test_tasks_in_doubt_count_no_root(monkeypatch):
    """RT-6.16 step 5, ADR-016: when the trust check of the task rows fails,
    no root counts as a task (it used to be every root)."""
    async def broken(client, posts):
        raise RuntimeError("503")

    monkeypatch.setattr(server.gate, "author_classes", broken)
    chat = _chat()
    root = _post("t" * 26, PEER, "TASK · ship it")
    assert await server._tasks(chat, [root], NAMES, crown=set()) == []
