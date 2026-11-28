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
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest
from fakes import WORK, FakeBus, FakeRegistrar
from test_natsbus import Rig, _login_service

from mattermost_ai_agent_bus import a2a, planb
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.events import PostEvent, ReactionEvent
from mattermost_ai_agent_bus.model import ChannelUser, Post, Reaction
from mattermost_ai_agent_bus.nats_port import NatsPort
from mattermost_ai_agent_bus.natsbus import NatsSettings, OpenAsk
from mattermost_ai_agent_bus.requests import RequestError

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
            if inspect.isawaitable(out):
                out = await out
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


async def _setup(rig: Rig, monkeypatch: pytest.MonkeyPatch, pieces: list[int], *,
                 bridge_bot: str = "") -> tuple[NatsPort, Watched]:
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    await rig.admin.publish(f"presence.planb.{HOME}.v1",
                            json.dumps({"v": 1, "channel": HOME, "pieces": pieces}).encode())
    await rig.admin.flush()
    name = rig.names["alice"]
    await _login_service(rig.admin, name, [HOME], {})
    inner = Watched(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = HOME
    if bridge_bot:
        inner.bots[bridge_bot] = ChannelUser(id=BOT, username=bridge_bot)
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home=HOME), (HOME,),
                    bridge_bot=bridge_bot,
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

    # Judged live as not approving: history may only add to _unapproved, never lift this.
    port._judge(("post", P1), "trusted", False)
    port._judge(("reaction", P1, "✅", "trusted"), "trusted", False)

    async with Bridge(rig, bridge):
        try:
            posts = await port.channel_posts(WORK)
            by_id = {p.id: p for p in posts}
            assert by_id[P1].user_id == "hu:trusted"
            assert by_id[P2].user_id == "hu:stale"  # a stale record still names the person
            assert by_id[P4].user_id == "hu:norec"  # no record, still the bridge's anchor
            bad = await port.unapproved(posts)
            assert ("post", P1) in bad and ("post", ASK) not in bad  # the live key stays
            assert ("post", P2) in bad and ("post", P3) in bad and ("post", P4) in bad
            assert ("reaction", P1, "✅", "hu:trusted") in bad  # the live key stays
            assert ("reaction", P2, "✅", "hu:stale") in bad
            assert ("reaction", P3, "✅", "hu:old") in bad
        finally:
            await port.stop()


async def test_a_channel_read_walks_next_while_more_and_dedupes(
        rig: Rig, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.42 step 11: the first request asks with the caller's per_page as `limit` once,
    later ones with the full page while `more`, from `next` (exclusive); a post in two pages is
    read once, a page whose cursor does not advance ends the read, and a read still holding
    `more` at the page cap says so (it carries the oldest window, not the newest)."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    base = _now() * 1000
    since = base - 1000
    p1 = _post(P1, anchor="", create_at=base)
    p2 = _post(P2, anchor="", create_at=base + 1000)
    p3 = _post(P3, anchor="", create_at=base + 2000)
    next_ms = p2["create_at"] + 1

    def pages(payload: dict[str, Any]) -> Any:
        if payload.get("since") == since:
            return _result([p1, p2], more=True, next_ms=next_ms)
        return _result([p2, p3])  # p2 again: the boundary of an exclusive cursor

    def endless(payload: dict[str, Any]) -> Any:
        at = payload["since"] + 1
        return _result([_post(P1, anchor="", create_at=at)], more=True, next_ms=at + 1)

    try:
        async with Bridge(rig, pages) as fake:
            got = await port.channel_posts(WORK, per_page=3, since_ms=since)
            assert [p.id for p in got] == [P3, P2, P1]
            assert [(q["since"], q["limit"]) for q in fake.asked] == [(since, 3), (next_ms, 200)]

        async with Bridge(rig, endless) as fake:
            with caplog.at_level(logging.WARNING, logger="mattermost_ai_agent_bus.nats_port"):
                got = await port.posts_after(10, channel_id=WORK, max_pages=5)
            assert [p.id for p in got] == [P1]  # deduped across the five pages
            assert len(fake.asked) == 5
            assert "oldest window, not the newest" in caplog.text and "~home" in caplog.text

        async with Bridge(rig, endless) as fake:
            await port.posts_after(10, channel_id=WORK, max_pages=7)
            assert len(fake.asked) == 7  # the caller's max_pages, not HISTORY_PAGES

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


async def test_a_cursor_older_than_the_bridge_bound_is_clamped(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a caller's `since` older than the bridge's 14 days (tasks.since_ms()
    is exactly that) is clamped to the bound the bridge accepts, not refused."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    asked: list[dict[str, Any]] = []

    def bridge(payload: dict[str, Any]) -> Any:
        asked.append(payload)
        return _result([])

    async with Bridge(rig, bridge):
        try:
            now = _now() * 1000
            floor = now - 14 * 24 * 60 * 60 * 1000
            await port.posts_after(floor, channel_id=WORK)
            assert asked and asked[0]["since"] > floor
            assert asked[0]["since"] <= now
            await port.list_recent(channel_id=WORK, since_ms=floor)
            assert asked[-1]["since"] > floor
        finally:
            await port.stop()


async def test_a_forged_copy_is_no_copy_and_a_copys_reactions_count(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a copy must be the bridge's (`bot` and the live author check), so a
    member's forged `<prefix>nats_id` re-roots no approver's reply onto the NATS post it names.
    The checked copy's reactions are the NATS post's, as the live path maps them."""
    port, inner = await _setup(rig, monkeypatch, [5], bridge_bot="bridgebot")
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    mine = await port.post("an agent's post", channel_id=WORK)
    prefix = port._s.props_prefix
    copy = _post("k" * 26, message="an agent's post", bot=True, create_at=base - 3000,
                 props={f"{prefix}nats_id": mine.id},
                 reactions=[_reaction(alice, create_at=base - 2500)])
    forged = _post(P4, message="a forged copy", anchor="mallory",
                   account=_account("mallory", "f" * 26), create_at=base - 2000,
                   props={f"{prefix}nats_id": mine.id})
    to_copy = _post(P5, message="reply to the copy", anchor="alice-anchor", account=alice,
                    create_at=base - 1000, root="k" * 26)
    to_forged = _post("w" * 26, message="reply to the forged", anchor="alice-anchor",
                      account=alice, create_at=base - 500, root=P4)

    def bridge(_payload: dict[str, Any]) -> Any:
        return _result([copy, forged, to_copy, to_forged])

    async with Bridge(rig, bridge):
        try:
            posts = await port.channel_posts(WORK)
            by_id = {p.id: p for p in posts}
            assert "k" * 26 not in by_id  # the bridge's checked copy is dropped, NATS has it
            assert by_id[P4].user_id == "hu:mallory"  # a person's post with a forged prop is kept
            assert by_id[P5].root_id == mine.id  # a reply to the checked copy: the NATS post
            assert by_id["w" * 26].root_id == P4  # a forged prop re-roots nothing
            nat = by_id[mine.id]
            assert [r.user_id for r in nat.reactions] == ["hu:alice-anchor"]
            assert [r.post_id for r in nat.reactions] == [mine.id]
            assert inner.log == []  # no Mattermost read for the channel
        finally:
            await port.stop()


async def test_the_switch_gates_the_other_mattermost_reads(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: with the switch on, the unread watermark's copy pass and a person's
    file read make no Mattermost read for the channel (the file count stays in the raw post)."""
    port, inner = await _setup(rig, monkeypatch, [5], bridge_bot="bridgebot")
    person = Post(id=P1, channel_id=WORK, user_id="hu:alice-anchor", message="with files")
    port._note_mm(P1, HOME)  # as a live person's event does
    try:
        assert await port._with_files(person) is person
        assert "thread" not in inner.log
    finally:
        await port.stop()


async def test_copies_through_reads_the_history_with_the_switch_on(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: the unread watermark's copy pass reads the switched channel's posts
    through the bridge, never Mattermost, and passes a copy it can see there instead of asking
    the same old mark forever."""
    port, inner = await _setup(rig, monkeypatch, [5], bridge_bot="bridgebot")
    now = _now() * 1000
    mine = await port.post("an agent's post", channel_id=WORK)
    prefix = port._s.props_prefix
    copy = _post("k" * 26, message="an agent's post", bot=True, create_at=now - 60_000,
                 props={f"{prefix}nats_id": mine.id})

    def bridge(_payload: dict[str, Any]) -> Any:
        return _result([copy])

    async with Bridge(rig, bridge) as fake:
        try:
            through = await port.copies_through(WORK, now - 120_000)
            assert through == copy["create_at"]
            assert fake.asked and "channel_posts" not in inner.log
        finally:
            await port.stop()


async def test_two_history_reads_do_not_overlap(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: one history request runs at a time (the bridge answers `busy` to a
    second), so a port serializes its reads instead of provoking that answer."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    active = 0
    peak = 0

    async def bridge(_payload: dict[str, Any]) -> Any:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return _result([])

    async with Bridge(rig, bridge):
        try:
            await asyncio.gather(port.posts_after(1, channel_id=WORK),
                                 port.posts_after(1, channel_id=WORK))
            assert peak == 1
        finally:
            await port.stop()


async def test_a_malformed_answer_drops_what_does_not_hold_together(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a post without a Mattermost channel id or a time is dropped, never
    raised; a thread answer whose root is no id of its posts is an empty thread, never
    Mattermost's."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    base = _now() * 1000
    good = _post(P1, anchor="", create_at=base - 2000)
    bad_time = _post(P2, anchor="", create_at=base - 1000)
    bad_time["create_at"] = "soon"
    bad_channel = _post(P3, anchor="", create_at=base - 500)
    bad_channel["channel_id"] = "nope"

    def bridge(payload: dict[str, Any]) -> Any:
        if "thread" in payload:
            return {"v": 1, "channel": HOME, "channel_id": WORK, "thread": "nope", "posts": [good]}
        return _result([good, bad_time, bad_channel])

    async with Bridge(rig, bridge):
        try:
            posts = await port.channel_posts(WORK)
            assert [p.id for p in posts] == [P1]
            thread = await port.thread(P1)
            assert thread.root_id == P1 and thread.posts == ()
        finally:
            await port.stop()


async def test_a_read_without_a_cursor_starts_narrow_and_returns_the_newest(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a newest-N read starts one day back, not at the 14-day floor, so a busy
    home's newest posts are the ones it returns."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    now = _now() * 1000
    day = 24 * 60 * 60 * 1000
    old = _post(P1, anchor="", create_at=now - 5 * day)
    recent = [_post(P2, anchor="", create_at=now - 3 * 3600 * 1000),
              _post(P3, anchor="", create_at=now - 2 * 3600 * 1000),
              _post(P4, anchor="", create_at=now - 3600 * 1000)]

    def bridge(payload: dict[str, Any]) -> Any:
        return _result(recent if payload["since"] >= now - day else [old, *recent])

    async with Bridge(rig, bridge) as fake:
        try:
            got = await port.list_recent(channel_id=WORK, per_page=2)
            assert [p.id for p in got] == [P3, P4]  # the newest two
            assert fake.asked and fake.asked[0]["since"] >= now - day
            assert len(fake.asked) == 1  # the narrow window was enough
        finally:
            await port.stop()


async def test_a_narrow_window_with_too_few_posts_widens(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a one-day window with fewer than per_page posts widens to three days;
    the caller's own cursor is not widened."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    now = _now() * 1000
    day = 24 * 60 * 60 * 1000
    one = _post(P1, anchor="", create_at=now - 2 * 3600 * 1000)
    three = [_post(P2, anchor="", create_at=now - 3 * day + 3600 * 1000),
             _post(P3, anchor="", create_at=now - 2 * day),
             _post(P4, anchor="", create_at=now - day - 3600 * 1000)]

    def bridge(payload: dict[str, Any]) -> Any:
        if payload["since"] >= now - day:
            return _result([one])
        if payload["since"] >= now - 3 * day:
            return _result(three)
        return _result([one, *three])

    asked: list[int] = []

    def cursor(payload: dict[str, Any]) -> Any:
        asked.append(payload["since"])
        return _result([])

    try:
        async with Bridge(rig, bridge) as fake:
            got = await port.list_recent(channel_id=WORK, per_page=3)
            assert [p.id for p in got] == [P2, P3, P4]
            assert len(fake.asked) >= 2
            assert fake.asked[0]["since"] >= now - day
            assert fake.asked[1]["since"] < now - day

        async with Bridge(rig, cursor):
            await port.list_recent(channel_id=WORK, per_page=3, since_ms=now - 3 * day)
            assert asked == [now - 3 * day]  # the caller's cursor is authoritative
    finally:
        await port.stop()


async def test_an_unknown_post_id_asks_the_bridge_before_mattermost(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a post id the session never read is tried on each piece-5 home first,
    because its channel is unknown and the bridge's request needs one; a thread and its
    reactions come from there and no Mattermost read is made."""
    port, inner = await _setup(rig, monkeypatch, [5])
    unknown, react_unknown = "y" * 26, "z" * 26
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    bob = _account("bob-anchor", BOB, approver=False, name="bob")
    found = _post(unknown, message="the post", anchor="alice-anchor", account=alice,
                  create_at=base - 1000)
    reacted = _post(react_unknown, message="the reacted post", anchor="alice-anchor",
                    account=alice, create_at=base - 500,
                    reactions=[_reaction(bob, create_at=base - 250)])

    def bridge(payload: dict[str, Any]) -> Any:
        post = found if payload["thread"] == unknown else reacted
        return _result([post], thread=payload["thread"])

    async with Bridge(rig, bridge) as fake:
        try:
            thread = await port.thread(unknown)
            assert thread.root_id == unknown and [p.id for p in thread.posts] == [unknown]
            reactions = await port.reactions(react_unknown)
            assert [r.user_id for r in reactions] == ["hu:bob-anchor"]
            assert inner.log == []  # never Mattermost
            assert fake.asked and all(p["channel"] == HOME for p in fake.asked)
            assert {p["thread"] for p in fake.asked} == {unknown, react_unknown}
        finally:
            await port.stop()


async def test_an_unknown_post_id_falls_back_only_on_the_not_in_answer(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: only the bridge's explicit "the thread is not in ~<channel>" sends an
    unknown id to Mattermost, once every piece-5 home has said so."""
    port, inner = await _setup(rig, monkeypatch, [5])
    unknown = "x" * 26
    base = _now() * 1000
    mine = post_of({"id": unknown, "channel_id": WORK, "user_id": UNBOUND,
                    "message": "on Mattermost", "create_at": base - 1000})
    inner.posts = [mine]
    inner.reacts = {unknown: [Reaction(user_id=UNBOUND, post_id=unknown, emoji_name="+1")]}

    def bridge(_payload: dict[str, Any]) -> Any:
        return "the thread is not in ~home"

    async with Bridge(rig, bridge):
        try:
            thread = await port.thread(unknown)
            assert [p.id for p in thread.posts] == [unknown]
            reactions = await port.reactions(unknown)
            assert [r.emoji_name for r in reactions] == ["+1"]
            assert "thread" in inner.log and "reactions" in inner.log
        finally:
            await port.stop()


async def test_an_unknown_post_id_raises_on_another_bridge_failure(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: any bridge failure other than "the thread is not in" raises; an
    unknown id is never quietly read on Mattermost instead."""
    port, inner = await _setup(rig, monkeypatch, [5])
    unknown = "w" * 26

    def bridge(_payload: dict[str, Any]) -> Any:
        return "not built"

    async with Bridge(rig, bridge):
        try:
            with pytest.raises(RequestError, match="not built"):
                await port.thread(unknown)
            with pytest.raises(RequestError, match="not built"):
                await port.reactions(unknown)
            assert inner.log == []  # never Mattermost
        finally:
            await port.stop()


async def test_a_thread_drops_replies_of_another_root(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a reply whose Mattermost root is not the thread's root, or a top-level
    post that is not the root, is dropped as a live event's root check would; a root that is
    itself a reply is no root (the thread reads empty)."""
    port, _inner = await _setup(rig, monkeypatch, [5])
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    root = _post(P1, anchor="alice-anchor", account=alice, create_at=base - 4000)
    good = _post(P2, anchor="alice-anchor", account=alice, create_at=base - 3000, root=P1)
    nested = _post(P3, anchor="alice-anchor", account=alice, create_at=base - 2000, root=P2)
    top = _post(P4, anchor="alice-anchor", account=alice, create_at=base - 1000, root="")
    good2 = _post(P5, anchor="alice-anchor", account=alice, create_at=base - 500, root=P1)

    def bridge(_payload: dict[str, Any]) -> Any:
        return _result([root, good, nested, top, good2], thread=P1)

    def broken(_payload: dict[str, Any]) -> Any:
        return _result([_post(P1, anchor="alice-anchor", account=alice, create_at=base, root=P2)],
                       thread=P1)

    port._note_mm(P1, HOME)  # as a read of the root would
    try:
        async with Bridge(rig, bridge):
            thread = await port.thread(P1)
            assert [p.id for p in thread.posts] == [P1, P2, P5]
        async with Bridge(rig, broken):
            assert (await port.thread(P1)).posts == ()
    finally:
        await port.stop()


async def test_after_a_restart_the_sessions_own_bot_copy_of_an_open_ask_counts(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: after a restart the memory may lack the copy's NATS sender; a post by
    this session's own bot naming an open ask's sequence is still a copy, any other unknown
    sequence is not."""
    port, inner = await _setup(rig, monkeypatch, [5, 6])
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    name = rig.names["alice"]
    prefix = port._s.props_prefix
    monkeypatch.setattr(port, "_bot_prefix", lambda _home: "bb")
    inner.bots["bb" + name] = ChannelUser(id=BOT, username="bb" + name)
    port.ask_source = lambda: [OpenAsk(mm="n1:home:7", kind="ask", human="-", title="t", asked=1)]
    own = _post("k" * 26, message="an ask", bot=True, create_at=base - 3000,
                props={f"{prefix}nats_id": "n1:home:7"})
    reply = _post(P1, anchor="alice-anchor", account=alice, create_at=base - 2000,
                  root="k" * 26)
    other = _post("m" * 26, message="not an ask", bot=True, create_at=base - 1500,
                  props={f"{prefix}nats_id": "n1:home:8"})
    other_reply = _post(P2, anchor="alice-anchor", account=alice, create_at=base - 1000,
                        root="m" * 26)

    def bridge(_payload: dict[str, Any]) -> Any:
        return _result([own, reply, other, other_reply])

    async with Bridge(rig, bridge):
        try:
            by_id = {p.id: p for p in await port.channel_posts(WORK)}
            assert by_id[P1].root_id == "n1:home:7"  # the own bot's copy of an open ask
            assert by_id[P2].root_id == "m" * 26  # any other unknown sequence is no copy
        finally:
            await port.stop()


async def test_a_dm_post_known_from_mattermost_skips_the_bridge(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.42 step 11: a post the session saw on Mattermost outside a piece-5 home (a DM
    answer, a permission prompt) is read there; a known id does not ask the bridge first."""
    port, inner = await _setup(rig, monkeypatch, [5])
    dm_channel = "d" * 26
    dm = Post(id="v" * 26, channel_id=dm_channel, user_id=UNBOUND, message="a DM")
    inner.posts = [dm]
    inner.reacts = {dm.id: [Reaction(user_id=UNBOUND, post_id=dm.id, emoji_name="+1")]}

    def bridge(_payload: dict[str, Any]) -> Any:
        return "the thread is not in ~home"  # it must not be asked

    async with Bridge(rig, bridge) as fake:
        try:
            assert await port.skip(PostEvent(dm, "D", False)) is False
            thread = await port.thread(dm.id)
            assert [p.id for p in thread.posts] == [dm.id]
            assert [r.emoji_name for r in await port.reactions(dm.id)] == ["+1"]
            assert fake.asked == []  # the known DM channel skipped the bridge
            assert "thread" in inner.log and "reactions" in inner.log
        finally:
            await port.stop()


async def test_a_reaction_without_a_channel_does_not_mark_a_home_post(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """#305's second review: a reaction whose event carries no channel must not note the raw
    id (or an empty one) and so overwrite a home post's entry from a history read; the post
    still reads through the bridge."""
    port, inner = await _setup(rig, monkeypatch, [5])
    post_id = "w" * 26
    base = _now() * 1000
    alice = _account("alice-anchor", ALICE, name="alice")
    found = _post(post_id, message="the post", anchor="alice-anchor", account=alice,
                  create_at=base - 1000)

    def bridge(payload: dict[str, Any]) -> Any:
        return _result([found], thread=payload["thread"])

    async with Bridge(rig, bridge) as fake:
        try:
            # the home's post is read through the bridge first: its channel is a home
            assert [p.id for p in (await port.thread(post_id)).posts] == [post_id]
            asked = len(fake.asked)
            # a reaction without a channel id on that post must not note anything
            event = ReactionEvent(Reaction(user_id=UNBOUND, post_id=post_id, emoji_name="+1"), "")
            assert await port.skip(event) is False
            assert [p.id for p in (await port.thread(post_id)).posts] == [post_id]
            assert len(fake.asked) == asked + 1  # still the bridge, not Mattermost
            assert inner.log == []
        finally:
            await port.stop()
