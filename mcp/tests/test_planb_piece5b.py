"""RT-6.42 step 11 (ADR-014 phase 5, plan B piece 5b): with piece 5 switched on for a home
over NATS (`planb.on(5, home)`, RT-6.39 step 5), a session reads what people wrote while it
was away through the bridge's history request (`<req>.history.<agent>`), not Mattermost.

Unit tests of the reads' mapping with a fake bridge on a real nats-server: the posts keep
their Mattermost ids and carry `hu:<anchor>` authors with the account record beside them, a
bot's post stays Mattermost's (an ask), reactions carry their senders' anchors and records,
a stale record still names the person and approves nothing (RT-6.42's freshness rule), the
bridge's `more`/`next` pages forward and dedupes, a failed request reads nothing (no
Mattermost fallback), and with the switch off Mattermost is read as before.

MM_TEST_NATS=1 and CB_NATS_SERVER run them against a real nats-server whose users hold the
login service's rights for a home (the rig of test_natsbus.py).
"""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeRegistrar
from test_natsbus import Rig, _login_service

from mattermost_ai_agent_bus import a2a, planb
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Post
from mattermost_ai_agent_bus.nats_port import NatsPort
from mattermost_ai_agent_bus.natsbus import NatsSettings

HOME = "home"
P1 = "p" * 26  # people's posts keep their Mattermost ids
P2 = "q" * 26
P3 = "r" * 26
P4 = "s" * 26
P5 = "t" * 26
ASK = "b" * 26  # the agent's bot, an ask's author
BOT = "u" * 26  # the bridge's bot, a copy's author
ALICE = "a" * 26  # a Mattermost account bound to alice-anchor
BOB = "c" * 26  # ... and one bound to bob-anchor
CAROL = "e" * 26  # ... and one whose record is stale
UNBOUND = "d" * 26  # an account no record binds
TRACKED = ("channel_posts", "list_recent", "posts_after", "thread", "reactions")


class Watched(FakeBus):
    """The chat, logging every read the port could have made on Mattermost."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.log: list[str] = []
        for name in TRACKED:
            orig = getattr(self, name)

            async def wrapped(*args: Any, _n: str = name, _o: Any = orig, **kwargs: Any) -> Any:
                self.log.append(_n)
                return await _o(*args, **kwargs)

            setattr(self, name, wrapped)


def _now() -> int:
    return int(time.time())


def _account(anchor: str, account: str = ALICE, *, approver: bool = True,
             name: str = "", iat: int | None = None, exp: int | None = None) -> dict[str, Any]:
    iat = _now() if iat is None else iat
    exp = iat + 600 if exp is None else exp
    return {"v": 1, "account": account, "anchor": anchor, "name": name or anchor,
            "approver": approver, "token_iat": iat, "iat": iat, "exp": exp}


def _post(pid: str, *, message: str = "hello", anchor: str = "", account: dict[str, Any] | None = None,
          bot: bool = False, create_at: int = 0, root: str = "", props: dict[str, Any] | None = None,
          reactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": pid, "user_id": account["account"] if account else (BOT if bot else UNBOUND),
        "channel_id": WORK, "root_id": root, "type": "", "message": message,
        "props": dict(props or {}), "create_at": create_at, "update_at": create_at, "edit_at": 0,
        "anchor": anchor, "bot": bot,
    }
    if account is not None:
        row["account"] = account
    if reactions:
        row["metadata"] = {"reactions": reactions}
    return row


def _reaction(account: dict[str, Any], *, emoji: str = "✅", create_at: int = 0) -> dict[str, Any]:
    return {"user_id": account["account"], "emoji_name": emoji, "create_at": create_at,
            "anchor": account["anchor"], "account": account}


def _result(posts: list[dict[str, Any]], *, thread: str = "", more: bool = False,
            next_ms: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {"v": 1, "channel": HOME, "channel_id": WORK, "posts": posts}
    if thread:
        out["thread"] = thread
    if more:
        out["more"] = True
        out["next"] = next_ms
    return out


class Bridge:
    """The bridge's side of the history request, on the rig's admin connection."""

    def __init__(self, rig: Rig, handler: Callable[[dict[str, Any]], Any]) -> None:
        self.rig, self.handler = rig, handler
        self.asked: list[dict[str, Any]] = []
        self.sub: Any = None

    async def __aenter__(self) -> "Bridge":
        async def answer(msg: Any) -> None:
            body = json.loads(msg.data)
            self.asked.append(body["payload"])
            out = self.handler(body["payload"])
            envelope: dict[str, Any] = {"v": 1, "request": body["request"], "kind": "history"}
            if isinstance(out, str):  # an error, in the bridge's words
                envelope["error"] = out
            else:
                envelope["result"] = out
            await self.rig.admin.publish(f"deliver.{self.rig.names['alice']}.req.{body['request']}",
                                         json.dumps(envelope).encode())

        self.sub = await self.rig.admin.subscribe("req.history.>", cb=answer)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.sub is not None:
            await self.sub.unsubscribe()


async def _setup(rig: Rig, monkeypatch: pytest.MonkeyPatch, pieces: list[int]) -> tuple[NatsPort, Watched]:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    await rig.admin.publish(f"presence.planb.{HOME}.v1",
                            json.dumps({"v": 1, "channel": HOME, "pieces": pieces}).encode())
    await rig.admin.flush()
    name = rig.names["alice"]
    await _login_service(rig.admin, name, [HOME], {})
    inner = Watched(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = HOME
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home=HOME), (HOME,),
                    bus_options={"heartbeat_sec": 0.2})  # type: ignore[arg-type]
    await asyncio.wait_for(port.start(rig.logins["alice"], owner="alice"), 15)
    return port, inner


async def test_with_the_switch_on_the_reads_come_from_the_bridge_and_no_mattermost(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a person's post keeps its Mattermost id and is `hu:<anchor>` with no
    props; a bot's post is Mattermost's (an ask); reactions carry their senders' anchors; the
    five reads make no Mattermost call for the channel."""
    port, inner = await _setup(rig, monkeypatch, [5])
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    bob = _account("bob-anchor", BOB, approver=False, name="bob")
    person = _post(P1, message="while you were away", anchor="alice-anchor", account=alice,
                   create_at=base - 3000, reactions=[_reaction(bob, create_at=base - 2000)])
    anon = _post(P2, message="no record here", anchor="", create_at=base - 2000)
    ask = _post(ASK, message="🙋 @alice — **deploy?**", bot=True, create_at=base - 1000,
                props={"a2a": {"v": 1, "kind": a2a.ASK}})
    reply = _post(P3, message="answered after the ask", anchor="alice-anchor", account=alice,
                  create_at=base - 500, root=P1)

    def bridge(payload: dict[str, Any]) -> Any:
        if "thread" in payload:
            return _result([person, reply], thread=P1)
        return _result([person, anon, ask])

    async with Bridge(rig, bridge) as fake:
        try:
            assert planb.on(5, HOME)
            posts = await port.channel_posts(WORK)
            by_id = {p.id: p for p in posts}
            assert [p.id for p in posts] == [ASK, P2, P1]  # newest first
            assert by_id[P1].user_id == "hu:alice-anchor" and by_id[P1].props == {}
            assert by_id[P1].message == "while you were away"
            assert [r.user_id for r in by_id[P1].reactions] == ["hu:bob-anchor"]
            assert [r.post_id for r in by_id[P1].reactions] == [P1]
            assert by_id[P2].user_id == "hu:-"  # an account bound to nobody
            assert by_id[ASK].user_id == BOT and by_id[ASK].kind == a2a.ASK
            assert by_id[ASK].title == "deploy?" and by_id[ASK].props["a2a"]["kind"] == a2a.ASK
            recent = await port.list_recent(channel_id=WORK, per_page=2)
            assert [p.id for p in recent] == [P2, ASK]  # the newest two, oldest first
            after = await port.posts_after(base - 2500, channel_id=WORK)
            assert [p.id for p in after] == [P2, ASK]
            thread = await port.thread(P1)
            assert thread.root_id == P1 and [p.id for p in thread.posts] == [P1, P3]
            assert thread.posts[1].root_id == P1  # a person's reply, its Mattermost root
            reactions = await port.reactions(P1)
            assert [r.user_id for r in reactions] == ["hu:bob-anchor"]
            assert inner.log == []  # not one Mattermost read for ~home
            assert all(payload["channel"] == HOME for payload in fake.asked)
            assert all(set(payload) == {"channel", "since", "limit"} for payload in fake.asked
                       if "thread" not in payload)
            threads = [payload for payload in fake.asked if "thread" in payload]
            assert threads and all(payload["thread"] == P1 and "since" not in payload
                                   and payload["limit"] == 200 for payload in threads)
        finally:
            await port.stop()


async def test_a_stale_record_still_names_the_person_and_approves_nothing(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11 and step 9: the record beside a post or reaction is checked as a live
    event's - `approver` and a fresh `exp` (never past its write plus 15 minutes) - so an
    approval read back from history counts only with a fresh record; a stale one names the
    person and approves nothing; a post without a record approves nothing."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    now = _now()
    fresh = _account("trusted", ALICE, approver=True, name="Tina", iat=now, exp=now + 600)
    stale = _account("stale", BOB, approver=True, name="Sam", iat=now - 3600, exp=now + 600)  # clamped
    old = _account("old", CAROL, approver=True, name="Ola", iat=now - 600, exp=now - 1)  # exp past
    ask = _post(ASK, message="🙋 @alice — **deploy?**", bot=True, create_at=now * 1000 - 5000,
                props={"a2a": {"v": 1, "kind": a2a.ASK}})
    p_fresh = _post(P1, anchor="trusted", account=fresh, create_at=now * 1000 - 4000,
                    reactions=[_reaction(fresh, create_at=now * 1000 - 3000)])
    p_stale = _post(P2, anchor="stale", account=stale, create_at=now * 1000 - 2000,
                    reactions=[_reaction(stale, create_at=now * 1000 - 1500)])
    p_old = _post(P3, anchor="old", account=old, create_at=now * 1000 - 1000,
                  reactions=[_reaction(old, create_at=now * 1000 - 500)])
    p_none = _post(P4, anchor="norec", create_at=now * 1000 - 500)

    def bridge(_payload: dict[str, Any]) -> Any:
        return _result([ask, p_fresh, p_stale, p_old, p_none])

    async with Bridge(rig, bridge):
        try:
            posts = await port.channel_posts(WORK)
            by_id = {p.id: p for p in posts}
            assert by_id[P1].user_id == "hu:trusted"
            assert by_id[P2].user_id == "hu:stale"  # a stale record still names the person
            assert by_id[P4].user_id == "hu:norec"  # no record, still the bridge's anchor
            bad = await port.unapproved(posts)
            assert ("post", P1) not in bad and ("post", ASK) not in bad
            assert ("post", P2) in bad and ("post", P3) in bad and ("post", P4) in bad
            assert ("reaction", P1, "✅", "hu:trusted") not in bad
            assert ("reaction", P2, "✅", "hu:stale") in bad
            assert ("reaction", P3, "✅", "hu:old") in bad
        finally:
            await port.stop()


async def test_a_channel_read_walks_next_while_more_and_dedupes(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: the first request asks with the caller's per_page as `limit`, later
    ones with the full page while `more`, from `next` (exclusive); a post in two pages is
    read once, and a page whose cursor does not advance ends the read."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    p1 = _post(P1, anchor="", create_at=10)
    p2 = _post(P2, anchor="", create_at=20)
    p3 = _post(P3, anchor="", create_at=30)

    def pages(payload: dict[str, Any]) -> Any:
        if payload.get("since") == 10:
            return _result([p1, p2], more=True, next_ms=21)
        return _result([p2, p3])  # p2 again: the boundary of an exclusive cursor

    def endless(payload: dict[str, Any]) -> Any:
        at = payload["since"] + 1
        return _result([_post(P1, anchor="", create_at=at)], more=True, next_ms=at + 1)

    try:
        async with Bridge(rig, pages) as fake:
            got = await port.channel_posts(WORK, per_page=3, since_ms=10)
            assert [p.id for p in got] == [P3, P2, P1]
            assert [(q["since"], q["limit"]) for q in fake.asked] == [(10, 3), (21, 200)]

        async with Bridge(rig, endless) as fake:
            got = await port.posts_after(10, channel_id=WORK)
            assert [p.id for p in got] == [P1]  # deduped across the five pages
            assert len(fake.asked) == 5

        async with Bridge(rig, lambda _p: _result([p1, p2, p3])) as fake:
            newest = await port.list_recent(channel_id=WORK, per_page=2)
            assert [p.id for p in newest] == [P2, P3]  # the newest N, oldest first
            assert fake.asked and fake.asked[0]["limit"] == 2
    finally:
        await port.stop()


async def test_a_failed_request_reads_nothing_and_never_mattermost(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: an answer with an error (or one that does not hold together) gives
    nothing, logged; the switch's channel is never read on Mattermost instead."""
    port, inner = await _setup(rig, monkeypatch, [5])
    base = _now() * 1000
    person = _post(P1, anchor="alice-anchor", account=_account("alice-anchor", ALICE),
                   create_at=base - 1000)
    inner.posts = [Post(id="m" * 26, channel_id=WORK, user_id=UNBOUND, message="on Mattermost",
                        create_at=base - 500)]
    mode = {"error": False, "malformed": False}

    def bridge(_payload: dict[str, Any]) -> Any:
        if mode["error"]:
            return "not built"
        if mode["malformed"]:
            return {"v": 1, "channel": HOME, "channel_id": WORK, "posts": "nope"}
        return _result([person])

    async with Bridge(rig, bridge):
        try:
            assert [p.id for p in await port.channel_posts(WORK)] == [P1]  # the channel is known
            mode["error"] = True
            assert await port.channel_posts(WORK) == []
            assert await port.list_recent(channel_id=WORK) == []
            assert await port.posts_after(0, channel_id=WORK) == []
            assert (await port.thread(P1)).posts == ()
            assert await port.reactions(P1) == []
            mode.update(error=False, malformed=True)
            assert await port.channel_posts(WORK) == []
            mode["malformed"] = False
            assert inner.log == []  # never Mattermost for the switched channel
        finally:
            await port.stop()


async def test_with_the_switch_off_the_reads_are_mattermost_as_before(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: without piece 5 in the channel's record nothing changes - the five
    reads are Mattermost's and no history request goes out."""
    port, inner = await _setup(rig, monkeypatch, [2])
    base = _now() * 1000
    mine = post_of({"id": "m" * 26, "channel_id": WORK, "user_id": UNBOUND,
                    "message": "on Mattermost", "create_at": base - 1000})
    inner.posts = [mine]
    asked: list[dict[str, Any]] = []

    def bridge(payload: dict[str, Any]) -> Any:
        asked.append(payload)
        return _result([])

    async with Bridge(rig, bridge):
        try:
            assert not planb.on(5, HOME)
            assert [p.id for p in await port.channel_posts(WORK)] == [mine.id]
            assert [p.id for p in await port.list_recent(channel_id=WORK)] == [mine.id]
            assert [p.id for p in await port.posts_after(0, channel_id=WORK)] == [mine.id]
            assert [p.id for p in (await port.thread(mine.id)).posts] == [mine.id]
            assert await port.reactions(mine.id) == []
            assert set(inner.log) == set(TRACKED)
            assert asked == []  # no history request
        finally:
            await port.stop()
