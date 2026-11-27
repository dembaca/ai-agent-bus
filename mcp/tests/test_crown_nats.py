"""The crown of a home over NATS (RT-6.38, ADR-014 point 16, lease.py, nats_port.py).

Unit tests on the fake bus of test_nats_port.py and a fake chat: the claims and their
candidates, the view and when it is unknown, the mirror without the receipt marker, no
Mattermost election in the home, `coordinator` (who, take, release, tick, the self-yield),
the roster through a proven link, a home without its NATS side, an ambiguous link tag, and
the Mattermost election beside the NATS candidates under one rule.
"""

import json
import logging
import time
from typing import Any

import pytest
from fakes import BOT, WORK, FakeBus
from test_nats_port import ME as NAME_ME_SHORT  # noqa: F401 — the module's own name, unused here
from test_nats_port import FakeNats, make

from mattermost_ai_agent_bus import a2a, compose, crownlog, debug, lease, natsbus, owners, receipt, server, usage
from mattermost_ai_agent_bus.model import ChannelUser, Mark, Reaction, StatusMark
from mattermost_ai_agent_bus.nats_port import NatsPort

HOME = "home"
HUMAN = "h" * 26
NAME_ME = "agent-" + "u" * 26   # this session's NATS name (its bot is BOT)
NAME_A = "agent-" + "a" * 26
NAME_B = "agent-" + "b" * 26
BOT_A, BOT_B, BOT_C = "1" * 26, "2" * 26, "3" * 26
TAG = {n: n[-26:][:16] for n in (NAME_ME, NAME_A, NAME_B)}
CROWN = a2a.status_emoji(StatusMark.COORDINATOR)


def _until(sec: int = 600) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + sec))


def bot(inner: FakeBus, uid: str, name: str, created: int, *, crown: bool = False, tag: str = "",
        marked: bool = False, state: str = "working", home: str = HOME, expires: int = 600) -> None:
    """A bot of the home with its status, as lease._rows reads it."""
    head = "coordinator · " if crown else ""
    text = (f"{head}{state}" + (f" · {lease.RECEIPT_MARK}" if marked else "")
            + (f" · ⇄{tag}" if tag else "") + f" · ~{home}")
    emoji = CROWN if crown else a2a.status_emoji(StatusMark.WORKING)
    status = {"emoji": emoji, "text": text, "duration": "date_and_time", "expires_at": _until(expires)}
    inner.members = [m for m in inner.members if m.id != uid] + [
        ChannelUser(uid, name, created, custom_status=status)]
    inner.presence[uid] = "online"


def claimant(fake: FakeNats, name: str, seq: int, *, member: bool = True, owner: str = "",
             bot_id: str = "", at: float | None = None) -> None:
    """A NATS name that is fresh, claimed the home's crown at `seq`, and (member) announced it."""
    fake.presence[name] = "online"
    fake.claims.setdefault(HOME, {})[name] = (seq, fake.server_now if at is None else at)
    if member:
        fake.members.add((HOME, name))
    if owner:
        fake.owners[name] = owner
    if bot_id:
        fake.bots[name] = bot_id


class World:
    def __init__(self) -> None:
        self.port, self.inner, fake = make()
        assert fake is not None
        self.fake: FakeNats = fake
        fake.name = NAME_ME
        fake.server_now = 1_000_000.0
        self.inner.channel = HOME
        bot(self.inner, BOT, "agent-me", 1000, tag=TAG[NAME_ME])
        lease.use_nats(HOME, self.port)
        lease.use_nats_link(lambda: TAG[NAME_ME])

    async def tick(self) -> str:
        return await lease.command(self.port, "tick")  # type: ignore[arg-type]

    async def prime(self) -> None:
        await self.port.prime()

    def view(self) -> lease.CrownView | None:
        return self.port.crown_view(HOME)

    def last_status(self) -> dict[str, str] | None:
        return self.inner.status


@pytest.fixture
async def w() -> World:
    world = World()
    await world.prime()
    claimant(world.fake, NAME_ME, 5, bot_id=BOT)
    return world


# --- 1. candidates and claims ----------------------------------------------------------------------

async def test_a_member_claims_once_synced_and_again_at_23_hours_by_the_servers_clock(w: World) -> None:
    """RT-6.38 step 1: no claim before the bus is synced and the session is a member of its
    home; then once; again when its own claim is older than 23 h by the server's time."""
    w.fake.claims.clear()
    w.fake.synced = False
    w.fake.members.add((HOME, NAME_ME))
    await w.tick()
    assert w.fake.claimed_homes == []  # not synced
    w.fake.synced = True
    w.fake.members.discard((HOME, NAME_ME))
    await w.tick()
    assert w.fake.claimed_homes == []  # not a member of its home yet
    w.fake.members.add((HOME, NAME_ME))
    await w.tick()
    assert w.fake.claimed_homes == [HOME]
    await w.tick()
    assert w.fake.claimed_homes == [HOME]  # a young claim stays
    w.fake.server_now += lease.CLAIM_RENEW_SEC - 10
    await w.tick()
    assert w.fake.claimed_homes == [HOME]
    w.fake.server_now += 20
    await w.tick()
    assert w.fake.claimed_homes == [HOME, HOME]  # older than 23 h: renewed
    # the local clock never counts: a claim is aged by the server's time only
    seq, at = w.fake.claims[HOME][NAME_ME]
    assert at == w.fake.server_now and seq > 100


async def test_the_holder_is_the_lowest_claim_among_fresh_member_standing_peers(w: World) -> None:
    """RT-6.38 step 1: fresh presence, a member of the channel, a standing peer."""
    w.inner.trusted = {HUMAN: "alice"}
    await w.prime()
    for n in (NAME_ME, NAME_A, NAME_B):
        w.fake.owners[n] = "alice"
    w.inner.descriptions = {BOT: "for @alice · trusts @alice"}
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_B, 20, owner="alice")
    claimant(w.fake, NAME_A, 10, owner="alice", member=False)  # a guest: never holds
    view = w.view()
    assert view is not None and view.holder == NAME_B and view.candidates == (NAME_B, NAME_ME)
    w.fake.members.add((HOME, NAME_A))
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
    w.fake.presence[NAME_A] = "offline"  # no fresh presence
    assert w.view().holder == NAME_B  # type: ignore[union-attr]
    w.fake.presence[NAME_A] = "online"
    w.fake.claims[HOME].pop(NAME_A)  # no claim
    assert w.view().holder == NAME_B  # type: ignore[union-attr]


async def test_a_foreign_owner_stops_the_candidates_as_the_mattermost_election_does(w: World) -> None:
    w.inner.trusted = {HUMAN: "alice"}
    await w.prime()
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_A, 10, owner="carol")  # another owner circle: nobody stands
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()
    claimant(w.fake, NAME_A, 10, owner="")  # no owner beside an owned agent: foreign, does not stand
    w.fake.owners.pop(NAME_A)
    claimant(w.fake, NAME_ME, 30, owner="alice")
    view = w.view()
    assert view is not None and view.holder == NAME_ME and view.candidates == (NAME_ME,)


@pytest.mark.parametrize("why", ["unreadable", "no-account"])
async def test_trusted_humans_not_resolved_keep_a_foreign_owner_from_the_crown(
        w: World, monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    """ADR-016, RT-6.38: MM_TRUSTED_HUMANS names alice, but the lookup resolves nobody
    (a 503, or a typo's 404): prime takes the configured names, never "none", so an older
    claim of another owner does not hold the crown."""
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "alice")
    w.inner.trusted = {}
    if why == "unreadable":
        async def down() -> dict[str, str]:
            raise RuntimeError("503")
        monkeypatch.setattr(w.inner, "trusted_humans", down)
    await w.prime()
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_A, 10, owner="carol")
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()


async def test_trusted_humans_configured_by_id_alone_keep_a_foreign_owner_from_the_crown(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-016, RT-6.38: only MM_TRUSTED_HUMAN_USER_IDS is set and nothing resolves:
    prime is in doubt, never "none", so an older claim of another owner does not hold."""
    monkeypatch.delenv("MM_TRUSTED_HUMANS", raising=False)
    monkeypatch.setenv("MM_TRUSTED_HUMAN_USER_IDS", HUMAN)
    w.inner.trusted = {}
    await w.prime()
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_A, 10, owner="carol")
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()


# --- the Mattermost election and the NATS candidates under one rule ------------------------------

PEERS = "alice"


@pytest.mark.parametrize("scenario", ["no trusted humans", "all peers", "one foreign owner",
                                      "an agent without an owner", "this agent unbound",
                                      "no agent has an owner"])
async def test_the_mattermost_election_and_the_nats_candidates_agree(scenario: str) -> None:
    """RT-6.38 step 1: the same peer and foreign set gives the same agents that stand, on
    Mattermost (lease._election, the owners from descriptions) and over NATS (the owners from
    the presence subjects, the same rule: lease.standing)."""
    #            owner of me, of a, of b (None: no owner), a's trusted list
    cases: dict[str, tuple[bool, str | None, str | None, str | None, str]] = {
        "no trusted humans": (False, "alice", "carol", None, ""),
        "all peers": (True, "alice", "alice", "alice", ""),
        "one foreign owner": (True, "alice", "alice", "carol", ""),
        "an agent without an owner": (True, "alice", "alice", None, ""),
        "this agent unbound": (True, None, "alice", "alice", ""),
        "no agent has an owner": (True, None, None, None, ""),
    }
    trusted, mine, a, b, cut = cases[scenario]
    ids = {NAME_ME: BOT, NAME_A: BOT_A, NAME_B: BOT_B}
    owner = {NAME_ME: mine, NAME_A: a, NAME_B: b}
    w = World()
    # Mattermost: the three bots, oldest first (me, a, b), descriptions say the owner.
    w.inner.trusted = {HUMAN: PEERS} if trusted else {}
    w.inner.descriptions = {ids[n]: (f"for @{o} · trusts @{o}" if o else "unbound") + (cut if n == NAME_A else "")
                            for n, o in owner.items()}
    w.inner.native_owners = {ids[n]: (o or "") for n, o in owner.items()}
    for k, n in enumerate((NAME_ME, NAME_A, NAME_B)):
        bot(w.inner, ids[n], f"agent-{n[-3:]}", 1000 + k)
    rows = lease._at_home(await lease._rows(w.port, WORK), HOME)  # type: ignore[arg-type]
    election = await lease._election(w.port, rows)  # type: ignore[arg-type]
    # NATS: the same three agents claim in the same order, their owners in the presence subjects.
    await w.prime()
    for k, n in enumerate((NAME_ME, NAME_A, NAME_B)):
        claimant(w.fake, n, 10 + k, owner=owner[n] or "", bot_id=ids[n])
    view = w.view()
    assert view is not None
    assert [ids[n] for n in view.candidates] == election.ids


# --- 2. crown_held: the file lease AND the NATS view ---------------------------------------------------

def _file_crown(sec: int = 600) -> None:
    lease._save_coord(BOT, int(time.time()) + sec, 0, 0, WORK)


async def test_crown_held_is_the_file_lease_and_the_nats_view(w: World) -> None:
    """RT-6.38 step 2: the signature is kept; in a NATS home the view must say mine too."""
    claimant(w.fake, NAME_ME, 5)
    await lease.home_id(w.port)  # type: ignore[arg-type]
    assert w.view() is not None and w.view().mine  # type: ignore[union-attr]
    assert not lease.crown_held(BOT)  # no file lease
    _file_crown()
    assert lease.crown_held(BOT) and lease.crown_held(BOT, WORK)
    claimant(w.fake, NAME_A, 1)  # another claim is lower: the view says not mine
    assert not lease.crown_held(BOT)
    w.fake.claims[HOME].pop(NAME_A)
    assert lease.crown_held(BOT)
    # another channel's id is no NATS home: only the file lease, as before
    assert not lease.crown_held(BOT, "x" * 26)


@pytest.mark.parametrize("break_it", ["detached", "fatal", "disconnected", "unsynced", "names"])
async def test_an_unknown_view_is_neither_free_nor_mine(w: World, break_it: str) -> None:
    """RT-6.38 step 2: the view counts only with the bus attached, not fatal, connected and
    synced (and the trusted humans read); otherwise it is unknown, and a live file lease
    does not make the crown mine."""
    _file_crown()
    assert lease.crown_held(BOT)
    if break_it == "detached":
        w.port._bus = None
    elif break_it == "fatal":
        w.port.fail("the credential names another agent")
    elif break_it == "disconnected":
        w.fake.connected = False
    elif break_it == "unsynced":
        w.fake.synced = False
    else:
        w.port._names = None
    assert w.port.crown_view(HOME) is None
    assert not lease.crown_held(BOT)
    # and a tick decides nothing then: no claim, no yield, the lease runs out by itself
    claims = list(w.fake.claimed_homes)
    answer = await lease.command(w.port, "take")  # type: ignore[arg-type]
    if break_it not in ("fatal", "names"):  # (a command reads the trusted humans again)
        assert answer == "pending: the NATS crown cannot be read now"
    await w.tick()
    assert w.fake.claimed_homes == claims
    assert lease.crown_until(BOT) > time.time()  # not dropped, not extended past its end


# --- 3. the mirror -------------------------------------------------------------------------------------

async def test_the_holder_mirrors_the_crown_without_the_receipt_marker(w: World) -> None:
    """RT-6.38 step 3: 👑 on its own status, the crown lease (local time), no 👀 marker, the
    link tag kept; an older agent reads a live crown without receipts: it claims nothing."""
    w.fake.members.add((HOME, NAME_ME))
    claimant(w.fake, NAME_ME, 5)
    await w.tick()
    status = w.last_status()
    assert status is not None and status["emoji"] == CROWN
    assert status["text"].startswith("coordinator · ")
    assert lease.RECEIPT_MARK not in status["text"]
    assert not lease.sets_receipts(status["text"])
    assert f"⇄{TAG[NAME_ME]}" in status["text"] and status["text"].endswith(f"~{HOME}")
    until = time.mktime(time.strptime(status["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    assert abs(until - (time.time() + lease.crown_lease_sec())) < 5
    assert lease.crown_held(BOT)
    # An older agent: no marker, so nobody watches for receipts, and no take-over by them.
    row = next(r for r in await lease._rows(w.port, WORK) if r["id"] == BOT)  # type: ignore[arg-type]
    assert row["crown"] and not lease.sets_receipts(row["text"])
    assert await lease.receipt_holder(w.port, "agent-me") == ("", "")  # type: ignore[arg-type]
    # the beat extends the mirror while the view says mine, and only then
    lease._save_coord(BOT, int(time.time()) + 20, 0, 0, WORK)
    await lease.set_status(w.port, "renew")  # type: ignore[arg-type]
    assert lease.crown_until(BOT) > time.time() + lease.crown_lease_sec() - 5
    claimant(w.fake, NAME_A, 1)  # another holds it now
    await lease.set_status(w.port, "renew")  # type: ignore[arg-type]
    assert lease.crown_until(BOT) == 0
    assert lease.RECEIPT_MARK not in (w.last_status() or {}).get("text", "")
    assert (w.last_status() or {}).get("emoji") != CROWN


async def test_a_mirror_that_cannot_be_confirmed_runs_out_instead_of_being_extended(w: World) -> None:
    w.fake.members.add((HOME, NAME_ME))
    await w.tick()
    first = lease.crown_until(BOT)
    assert first > 0
    w.fake.synced = False  # unknown
    lease._save_coord(BOT, int(time.time()) + 30, 0, 0, WORK)
    end = lease.crown_until(BOT)
    await lease.set_status(w.port, "renew")  # type: ignore[arg-type]
    assert lease.crown_until(BOT) == end  # not extended


# --- 4. no Mattermost election ---------------------------------------------------------------------------

async def test_no_election_look_slot_over_take_over_or_receipt_holder_in_a_nats_home(w: World) -> None:
    """RT-6.38 step 4: the slot, the receipt claim and the election have nothing to do."""
    bot(w.inner, BOT_A, "agent-a", 500, crown=True, marked=True, tag=TAG[NAME_A])  # an older marked 👑
    lease._look_at = time.time() - 1
    assert await lease.look(w.port) is False  # type: ignore[arg-type]
    assert await lease.slot_over(w.port, BOT_A) == 0  # type: ignore[arg-type]
    assert await lease.take_over(w.port, BOT_A, "p" * 26) is False  # type: ignore[arg-type]
    assert await lease.receipt_holder(w.port, "agent-a") == ("", "")  # type: ignore[arg-type]
    # the tick of an agent with no claim and no 👑 anywhere does not elect itself either
    w.inner.members = [m for m in w.inner.members if m.id != BOT_A]
    w.fake.claims.clear()
    w.fake.members.discard((HOME, NAME_ME))
    await w.tick()
    assert not lease.crown_held(BOT) and lease.crown_until(BOT) == 0
    assert lease.look_due() == 0.0


# --- 5. who, take, release, tick -------------------------------------------------------------------------

async def _two(w: World, *, me_first: bool) -> None:
    """This agent and agent-a both claimed; whoever has the lower sequence holds it."""
    w.fake.members.add((HOME, NAME_ME))
    w.inner.descriptions = {BOT: "for @alice · trusts @alice", BOT_A: "for @alice · trusts @alice"}
    claimant(w.fake, NAME_ME, 5 if me_first else 9, bot_id=BOT, owner="alice")
    claimant(w.fake, NAME_A, 9 if me_first else 5, bot_id=BOT_A, owner="alice")
    bot(w.inner, BOT_A, "agent-a", 2000, tag=TAG[NAME_A])
    bot(w.inner, BOT, "agent-me", 1000, tag=TAG[NAME_ME])


async def test_who_names_the_bot_linked_to_the_nats_holder(w: World) -> None:
    await _two(w, me_first=False)
    assert await lease.command(w.port, "who") == "agent-a"  # type: ignore[arg-type]
    w.fake.bots.pop(NAME_A)  # no proven link: no name
    assert await lease.command(w.port, "who") == ""  # type: ignore[arg-type]
    w.fake.synced = False  # unknown
    assert await lease.command(w.port, "who") == ""  # type: ignore[arg-type]


async def test_take_is_taken_for_the_holder_and_pending_for_everyone_else(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)  # the crown log's file (RT-6.6 step 11)
    await _two(w, me_first=False)
    out = await lease.command(w.port, "take")  # type: ignore[arg-type]
    assert out.startswith("pending: @agent-a holds the crown over NATS") and "cannot be overtaken" in out
    assert not lease.crown_held(BOT) and w.fake.claimed_homes == []  # it moves nobody
    w.fake.presence[NAME_A] = "offline"  # the holder is gone: the claim order hands it on
    assert await lease.command(w.port, "take") == "taken"  # type: ignore[arg-type]
    assert lease.crown_held(BOT)
    log = crownlog.log_path()
    assert log is not None and [json.loads(ln)["way"] for ln in log.read_text().splitlines()] == ["nats"]


# --- #243: take names the case, the crown debug category shows the view -----------------------------

async def test_take_says_a_holder_without_a_linked_bot_by_its_nats_name(w: World) -> None:
    """RT-6.38 step 4: the holder has no proven link to a bot."""
    await _two(w, me_first=False)
    w.fake.bots.pop(NAME_A)
    out = await lease.command(w.port, "take")  # type: ignore[arg-type]
    assert out.startswith(f"pending: a NATS agent without a linked bot holds it ({NAME_A});")


async def test_take_says_nobody_stands_with_the_reason(w: World) -> None:
    """RT-6.38 step 4: members exist, but a foreign owner circle keeps everyone from the crown."""
    w.inner.trusted = {HUMAN: "alice"}
    await w.prime()
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_A, 10, owner="carol")
    out = await lease.command(w.port, "take")  # type: ignore[arg-type]
    assert out == "no holder: nobody stands (the home has an agent of a foreign owner circle)"
    view = w.view()
    assert view is not None and view.claimants == (NAME_A, NAME_ME) and view.why in out


async def test_take_says_nobody_stands_when_this_session_has_no_owner(w: World) -> None:
    w.inner.trusted = {HUMAN: "alice"}
    await w.prime()
    claimant(w.fake, NAME_ME, 30)  # no owner of its own
    claimant(w.fake, NAME_A, 10, owner="alice")
    out = await lease.command(w.port, "take")  # type: ignore[arg-type]
    assert out.startswith("no holder: nobody stands (this session has no owner beside an agent with an owner")


async def test_take_says_no_claim_when_the_session_is_no_member_yet(w: World) -> None:
    """RT-6.38 step 4: no membership announced, no claim at the tick."""
    w.fake.members.discard((HOME, NAME_ME))
    w.fake.claims.get(HOME, {}).pop(NAME_ME, None)
    out = await lease.command(w.port, "take")  # type: ignore[arg-type]
    assert out.startswith("no claim: this session is not a member of the home yet")
    view = w.view()
    assert view is not None and not view.member and view.holder == ""
    assert view.why == "no claimant has a fresh presence and a membership of the home"


def _crown_debug_on(on: bool = True) -> None:
    debug.reset()
    if on:
        now = time.time()
        debug._states["home-id"] = debug.State(categories=("crown",), until_ms=int((now + 600) * 1000),
                                               exp=int(now + 600), at=now)


async def test_the_crown_debug_category_logs_the_view_at_each_tick(
        w: World, caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.31, RT-6.38 step 9: claimants in order, members, standing, holder, why nobody stands."""
    await _two(w, me_first=False)
    _crown_debug_on()
    try:
        with caplog.at_level(logging.INFO):
            await w.tick()
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("crown view:")]
        assert len(lines) == 1
        assert f"claimants in order {NAME_A}, {NAME_ME}" in lines[0]
        assert f"members {NAME_A}, {NAME_ME}" in lines[0] or f"members {NAME_ME}, {NAME_A}" in lines[0]
        assert f"standing {NAME_A}, {NAME_ME}" in lines[0] and f"holder {NAME_A}" in lines[0]
        assert "nobody stands" not in lines[0]
        w.inner.trusted = {HUMAN: "alice"}
        await w.prime()
        claimant(w.fake, NAME_A, 3, owner="carol")
        caplog.clear()
        with caplog.at_level(logging.INFO):
            await w.tick()
        line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("crown view:"))
        assert "holder none; nobody stands: the home has an agent of a foreign owner circle" in line
        assert "standing none" in line
    finally:
        debug.reset()


async def test_without_the_crown_category_nothing_is_logged_and_the_event_lines_skip_it(
        w: World, caplog: pytest.LogCaptureFixture) -> None:
    await _two(w, me_first=False)
    _crown_debug_on(False)
    with caplog.at_level(logging.INFO):
        await w.tick()
    assert not [r for r in caplog.records if r.getMessage().startswith("crown view:")]
    state = debug.State(categories=("crown", "props"))
    assert "crown" not in debug.attachment(state, None, False, call="x", props_out=None)
    _crown_debug_on()
    assert debug.category_on("crown") and not debug.category_on("wake")
    debug.reset()


async def test_release_claims_again_and_moves_behind(w: World) -> None:
    await _two(w, me_first=True)
    _file_crown()
    assert lease.crown_held(BOT)
    assert await lease.command(w.port, "release") == "released"  # type: ignore[arg-type]
    assert w.fake.claimed_homes == [HOME]
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
    assert not lease.crown_held(BOT) and lease.crown_until(BOT) == 0  # the mirror ended with it
    # a lone holder: the claim is renewed, nobody else is fresh, the crown stays
    w.fake.presence[NAME_A] = "offline"
    out = await lease.command(w.port, "release")  # type: ignore[arg-type]
    assert out == "released; no other fresh claim here, the crown stays with you"
    assert w.view().holder == NAME_ME  # type: ignore[union-attr]


async def test_a_holder_that_hangs_but_still_beats_yields_once_to_a_linked_reachable_member(w: World) -> None:
    """RT-6.38 step 4: the self-yield replaces the receipt takeover for a holder that is not
    reachable (silent or idle) while another linked reachable member stands."""
    await _two(w, me_first=True)
    await w.tick()
    assert lease.crown_held(BOT)
    await lease.set_status(w.port, "idle")  # type: ignore[arg-type] — 💤: not reachable
    claims = len(w.fake.claimed_homes)
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1  # it claimed again: moved behind
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1  # once
    assert not lease.crown_held(BOT)


async def test_on_plan_b_piece_2_the_holder_knows_its_own_row_by_its_nats_name(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.37 step 6, RT-6.38 steps 4-5: the roster of NATS ids its rows by NATS name, so the
    own row is the one of this session's NATS name, not of its bot id: the holder takes (the
    mirror is armed), keeps it at a beat while reachable, and yields once when idle; its own
    usage and its own crown are its own (no foreign coordinator)."""
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    f = w.fake
    monkeypatch.setattr(f, "member_names", lambda ch: sorted(n for c, n in f.members if c == ch), raising=False)
    monkeypatch.setattr(f, "card_of", lambda n: natsbus.PresenceCard(), raising=False)
    monkeypatch.setattr(f, "bot_record", lambda n: None, raising=False)
    await _two(w, me_first=True)
    assert lease.nats_roster_on(HOME) and lease.roster_self(w.port, BOT) == NAME_ME  # type: ignore[arg-type]
    rows = await lease._rows(w.port, WORK, HOME)  # type: ignore[arg-type]
    assert sorted(r["id"] for r in rows) == sorted([NAME_ME, NAME_A])
    assert [r["id"] for r in rows if lease.is_me(r, BOT)] == [NAME_ME]
    await w.tick()
    assert w.view().holder == NAME_ME and lease.crown_held(BOT)  # type: ignore[union-attr]  # taken, mirrored
    claims = len(f.claimed_homes)
    await lease.nats_beat(w.port)  # type: ignore[arg-type]
    assert len(f.claimed_homes) == claims  # reachable: it keeps the crown
    monkeypatch.setattr(usage, "own", lambda: {"ctx": 7.0})
    assert await server._usage_of(w.port, await lease.roster(w.port)) == {NAME_ME: {"ctx": 7.0}}  # type: ignore[arg-type]
    assert await server._foreign_coordinator(w.port, BOT) == ""  # type: ignore[arg-type]
    await lease.set_status(w.port, "idle")  # type: ignore[arg-type] — 💤: not reachable
    await lease.nats_beat(w.port)  # type: ignore[arg-type]
    assert len(f.claimed_homes) == claims + 1 and w.view().holder == NAME_A  # type: ignore[union-attr]
    await lease.nats_beat(w.port)  # type: ignore[arg-type]
    assert len(f.claimed_homes) == claims + 1  # once


async def test_a_holder_whose_post_gets_no_receipt_yields_within_the_wait_plus_a_beat(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 5, RT-6.6 step 10: a fresh, reachable holder with no 👀 or ⏳ on a post that
    needs it for 45 s claims again, once: the next candidate holds. A receipt ends the wait."""
    from types import SimpleNamespace

    from mattermost_ai_agent_bus import life
    from mattermost_ai_agent_bus.client import post_of
    now = [time.time()]
    monkeypatch.setattr(receipt, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.delenv("MM_ACK_WAIT_SEC", raising=False)
    receipt.reset()
    await _two(w, me_first=True)
    await w.tick()
    assert lease.crown_held(BOT)
    life.turn(False)
    await w.port.get_me()
    for pid in ("p" * 26, "q" * 26):
        await receipt.arrived(w.port, post_of({"id": pid, "message": "please look", "user_id": HUMAN,  # type: ignore[arg-type]
                                               "channel_id": WORK, "create_at": 1}), BOT, False)
    assert set(receipt._owed) == {"p" * 26, "q" * 26}
    w.inner.reacts["q" * 26] = [Reaction(user_id=BOT, post_id="q" * 26,  # type: ignore[attr-defined]
                                         emoji_name=a2a.receipt_reaction(Mark.WAITS))]
    claims = len(w.fake.claimed_homes)
    now[0] += 44
    await w.tick()
    assert len(w.fake.claimed_homes) == claims and w.view().holder == NAME_ME  # type: ignore[union-attr]
    now[0] += 3  # 47 s: past the wait, and this is the beat after
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1  # the yield: a new claim
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
    assert not lease.crown_held(BOT)
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1  # once


async def test_an_unreachable_holder_with_nobody_else_keeps_its_place(w: World) -> None:
    w.fake.members.add((HOME, NAME_ME))
    claimant(w.fake, NAME_ME, 5, bot_id=BOT)
    await w.tick()
    await lease.set_status(w.port, "idle")  # type: ignore[arg-type]
    before = len(w.fake.claimed_homes)
    await w.tick()
    assert len(w.fake.claimed_homes) == before  # nobody to yield to
    assert w.view().holder == NAME_ME  # type: ignore[union-attr]


async def test_the_tick_arms_a_crown_that_just_passed_to_this_agent(w: World) -> None:
    await _two(w, me_first=False)
    await w.tick()
    assert not lease.crown_held(BOT)
    w.fake.presence[NAME_A] = "offline"
    assert lease._nats_arm_due(BOT)
    await w.tick()
    assert lease.crown_held(BOT) and not lease._nats_arm_due(BOT)


# --- 5. the roster, who and coordinator_of through a proven link ------------------------------------------

async def _roster_world(w: World, *, owner_of_bot: str = "alice") -> None:
    w.inner.trusted = {HUMAN: "alice"}
    await w.prime()
    for n in (NAME_ME, NAME_A):
        w.fake.owners[n] = "alice"
    w.inner.descriptions = {BOT: "for @alice · trusts @alice", BOT_A: f"for @{owner_of_bot} · trusts @{owner_of_bot}",
                            BOT_C: "for @alice · trusts @alice"}
    claimant(w.fake, NAME_ME, 9, owner="alice", bot_id=BOT)
    claimant(w.fake, NAME_A, 5, owner="alice", bot_id=BOT_A)  # the lowest claim holds
    bot(w.inner, BOT, "agent-me", 1000, tag=TAG[NAME_ME])
    bot(w.inner, BOT_A, "agent-a", 2000, crown=True, tag=TAG[NAME_A])


async def test_a_crown_counts_in_the_roster_only_through_a_proven_link(w: World) -> None:
    """RT-6.38 step 5: two-sided and fresh, and the presence's owner is the bot's owner."""
    await _roster_world(w)
    rows = await lease.roster(w.port)  # type: ignore[arg-type]
    assert [(r.username, r.crown, r.coordinator) for r in rows] == [("agent-a", True, True), ("agent-me", False, False)]
    assert await lease.coordinator_of(w.port, WORK) == "agent-a"  # type: ignore[arg-type]
    assert lease.crown_in(rows) == "agent-a"
    # a Mattermost-only agent's 👑 (no link) is not the home's crown
    bot(w.inner, BOT_C, "agent-c", 3000, crown=True)
    assert await lease.coordinator_of(w.port, WORK) == "agent-a"  # type: ignore[arg-type]
    w.fake.bots.pop(NAME_A)  # the link breaks on the name's side
    assert await lease.coordinator_of(w.port, WORK) == ""  # type: ignore[arg-type]
    assert all(not r.crown and not r.coordinator for r in await lease.roster(w.port))  # type: ignore[arg-type]


async def test_a_linked_mirror_that_is_not_the_nats_holder_shows_no_crown(w: World) -> None:
    """RT-6.38 step 5: with the view known, a 👑 counts only if its link's NATS name is the
    holder: a lapsing old mirror of a name that passed the crown on shows none."""
    await _roster_world(w)
    assert await lease.coordinator_of(w.port, WORK) == "agent-a"  # type: ignore[arg-type]
    w.fake.claims[HOME][NAME_A] = (20, w.fake.server_now)  # agent-a claimed again: behind agent-me
    assert w.view().holder == NAME_ME  # type: ignore[union-attr]
    assert await lease.coordinator_of(w.port, WORK) == ""  # type: ignore[arg-type]
    assert all(not r.crown and not r.coordinator for r in await lease.roster(w.port))  # type: ignore[arg-type]


async def test_a_linked_crown_of_another_owner_is_no_crown(w: World) -> None:
    await _roster_world(w, owner_of_bot="carol")  # the bot's owner is not the presence's owner
    assert await lease.coordinator_of(w.port, WORK) == ""  # type: ignore[arg-type]
    assert all(not r.crown for r in await lease.roster(w.port))  # type: ignore[arg-type]


async def test_with_the_view_unknown_only_a_live_mirrored_crown_with_a_link_tag_counts(w: World) -> None:
    """RT-6.38 step 5: read-only, no election."""
    await _roster_world(w)
    w.fake.synced = False
    rows = await lease.roster(w.port)  # type: ignore[arg-type]
    assert [(r.username, r.crown) for r in rows] == [("agent-a", True), ("agent-me", False)]
    bot(w.inner, BOT_A, "agent-a", 2000, crown=True)  # no link tag in the status: not counted
    assert await lease.coordinator_of(w.port, WORK) == ""  # type: ignore[arg-type]
    bot(w.inner, BOT_A, "agent-a", 2000, tag=TAG[NAME_A])  # no crown at all: nobody is elected
    assert all(not r.coordinator for r in await lease.roster(w.port))  # type: ignore[arg-type]


async def test_with_the_view_unknown_at_most_one_crown_counts_the_lowest_claim(w: World) -> None:
    """RT-6.38 step 6 (H1): two live linked mirrors with the view unknown never name two
    coordinators: the one whose name has the lowest claim in the last known claims counts
    (not the older account, which may be the one that is yielding); none without a claim."""
    await _roster_world(w)
    bot(w.inner, BOT, "agent-me", 1000, crown=True, tag=TAG[NAME_ME])  # the older account, claim 9
    w.fake.synced = False
    rows = await lease.roster(w.port)  # type: ignore[arg-type]
    assert [r.username for r in rows if r.crown] == ["agent-a"]  # claim 5 is the lowest
    assert lease.crown_in(rows) == "agent-a"
    assert await lease.coordinator_of(w.port, WORK) == "agent-a"  # type: ignore[arg-type]
    w.fake.claims[HOME].clear()  # no claim of any of them is known
    assert all(not r.crown for r in await lease.roster(w.port))  # type: ignore[arg-type]
    assert await lease.coordinator_of(w.port, WORK) == ""  # type: ignore[arg-type]


async def test_an_echo_of_the_own_presence_older_than_75_seconds_makes_the_view_unknown(w: World) -> None:
    """RT-6.38 step 2 (H3): a half-open connection delivers nothing; the view is unknown when
    the own presence (published every 30 s) has not come back for more than 75 s (monotonic)."""
    _file_crown()
    w.fake.echo_age = 75.0
    assert w.view() is not None and lease.crown_held(BOT)
    w.fake.echo_age = 75.5
    assert w.view() is None and not lease.crown_held(BOT)
    end = lease.crown_until(BOT)
    await lease.set_status(w.port, "renew")  # type: ignore[arg-type]
    assert lease.crown_until(BOT) == end  # the mirror is not extended
    w.fake.echo_age = 1.0
    assert w.view() is not None and w.view().mine  # type: ignore[union-attr]


async def test_the_holder_yields_within_the_wait_plus_one_crown_beat_through_the_heartbeat(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 5 (M4): the holder's checks run on every crown beat, not only at the tick
    (MM_HEARTBEAT_SEC, 300 s): no receipt for 45 s, the yield comes at the next beat."""
    import asyncio
    from types import SimpleNamespace

    from mattermost_ai_agent_bus import life
    from mattermost_ai_agent_bus.client import post_of
    now = [time.time()]
    monkeypatch.setattr(receipt, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.delenv("MM_ACK_WAIT_SEC", raising=False)
    monkeypatch.setenv("MM_CROWN_BEAT_SEC", "15")
    receipt.reset()
    await _two(w, me_first=True)
    await w.tick()
    assert lease.crown_held(BOT)
    life.turn(False)
    await w.port.get_me()
    await receipt.arrived(w.port, post_of({"id": "p" * 26, "message": "please look", "user_id": HUMAN,  # type: ignore[arg-type]
                                           "channel_id": WORK, "create_at": 1}), BOT, False)
    assert set(receipt._owed) == {"p" * 26}
    start, claims0 = now[0], len(w.fake.claimed_homes)
    yielded_at: list[float] = []

    async def pause(sec: float) -> None:
        if len(w.fake.claimed_homes) > claims0 and not yielded_at:
            yielded_at.append(now[0] - start)
        if now[0] - start >= 120:
            raise asyncio.CancelledError
        now[0] += sec
        await asyncio.sleep(0)

    async def get_client() -> Any:
        return w.port

    monkeypatch.setattr(lease, "_pause", pause)
    with pytest.raises(asyncio.CancelledError):
        await lease.heartbeat(get_client)
    assert yielded_at and 45 <= yielded_at[0] <= 45 + 15 + 1
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
    assert len(w.fake.claimed_homes) == claims0 + 1  # once


async def test_a_new_hold_forgets_the_posts_of_an_earlier_one(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 5: an old receipt._owed entry cannot trigger a late yield in a new hold."""
    receipt._owed["o" * 26] = (False, 1.0)
    lease._nats_hold = False
    lease._nats_hold_track(lease.CrownView(me=NAME_ME, holder=NAME_ME))
    assert not receipt._owed and lease._nats_hold
    receipt._owed["o" * 26] = (False, 1.0)
    lease._nats_hold_track(lease.CrownView(me=NAME_ME, holder=NAME_ME))  # still the same hold
    assert receipt._owed
    lease._nats_hold_track(lease.CrownView(me=NAME_ME, holder=NAME_A))
    assert not lease._nats_hold


async def test_release_by_a_non_holder_says_so_and_claims_nothing(w: World) -> None:
    await _two(w, me_first=False)
    assert await lease.command(w.port, "release") == "not held by you: nothing to release"  # type: ignore[arg-type]
    assert w.fake.claimed_homes == []
    # a holder that is no member publishes no claim either
    w.fake.members.discard((HOME, NAME_ME))
    assert w.view() is not None and not w.view().member  # type: ignore[union-attr]
    w.fake.claims[HOME][NAME_A] = (50, w.fake.server_now)
    assert await lease.command(w.port, "release") == "not held by you: nothing to release"  # type: ignore[arg-type]
    assert w.fake.claimed_homes == []


async def test_who_needs_the_owner_of_the_link_to_match(w: World) -> None:
    await _two(w, me_first=False)
    assert await lease.command(w.port, "who") == "agent-a"  # type: ignore[arg-type]
    w.fake.owners[NAME_A] = "carol"  # the name's owner is not the bot's owner: no link
    assert await lease.command(w.port, "who") == ""  # type: ignore[arg-type]


# --- 6. no NATS side ----------------------------------------------------------------------------------------

async def test_a_nats_home_without_a_nats_side_has_no_crown_and_no_election(
        caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.38 step 6: named in MM_NATS_HOMES, but no port: no crown, no election; loud."""
    inner = make(bus=False)[1]
    bot(inner, BOT, "agent-me", 1000)
    with caplog.at_level(logging.WARNING):
        lease.use_nats(HOME, None, "MM_NATS_URL is not set")
        lease.use_nats(HOME, None, "MM_NATS_URL is not set")
    assert [r for r in caplog.records if "no crown and no Mattermost election" in r.getMessage()].__len__() == 1
    await lease.command(inner, "tick")  # type: ignore[arg-type]
    assert not lease.crown_held(BOT)
    assert (inner.status or {}).get("emoji") != CROWN  # the Mattermost election would have crowned it
    _file_crown()
    assert not lease.crown_held(BOT)
    assert "not reachable" in await lease.command(inner, "take")  # type: ignore[arg-type]
    assert await lease.look(inner) is False  # type: ignore[arg-type]
    assert lease.crown_hint(HOME) == "crown over NATS not reachable"
    assert lease.crown_hint("elsewhere") == ""
    assert "MM_NATS_URL is not set" in lease.nats_notes()[0]
    rows = await lease.roster(inner)  # type: ignore[arg-type]
    assert [r.username for r in rows] == ["agent-me"] and not rows[0].crown and not rows[0].coordinator


async def test_a_fatal_port_is_the_same_as_none(w: World, caplog: pytest.LogCaptureFixture) -> None:
    _file_crown()
    assert lease.crown_held(BOT)
    with caplog.at_level(logging.WARNING):
        w.port.fail("the credential names another agent")
        assert not lease.crown_held(BOT)
        assert not lease.crown_held(BOT)
    assert [r for r in caplog.records if "no crown and no Mattermost election" in r.getMessage()].__len__() == 1
    assert lease.crown_hint(HOME) == "crown over NATS not reachable"
    assert any("not reachable" in n for n in lease.nats_notes())


async def test_compose_registers_a_home_it_could_not_build_a_port_for(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setenv("MM_CHAT_URL", "https://chat.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.setenv("MM_NATS_HOMES", "agents")
    monkeypatch.setenv("MM_NATS_URL", "tls://nats.example:4222")  # no login
    with caplog.at_level(logging.ERROR):
        client = compose.client_from_env()
    assert compose.nats_port_of(client) is None
    assert lease.is_nats_home("agents") and lease.nats_state().kind == "down"
    assert lease.crown_hint("agents") == "crown over NATS not reachable"
    monkeypatch.setenv("MM_LOGIN_ISSUER", "https://kc.example/realms/x")
    monkeypatch.setenv("MM_LOGIN_CLIENT_ID", "bus")
    client = compose.client_from_env()
    port = compose.nats_port_of(client)
    assert isinstance(port, NatsPort) and lease.is_nats_home("agents")
    assert lease.nats_state().kind == "unknown"  # a port that is not up yet: not readable, not down
    monkeypatch.delenv("MM_NATS_HOMES")
    compose.client_from_env()
    assert not lease.is_nats_home("agents")


async def test_bus_info_says_so_when_the_crown_is_not_reachable(
        monkeypatch: pytest.MonkeyPatch) -> None:
    inner = make(bus=False)[1]
    lease.use_nats(HOME, None, "no login")

    async def none(client: Any) -> list[Any]:
        return []
    monkeypatch.setattr(server, "_get_client", lambda: inner)
    monkeypatch.setattr(server, "_guest_names", none)
    monkeypatch.setattr(server, "_own_channels", none)
    monkeypatch.setattr(server, "_newer_installed", lambda: None)
    inner.trusted_source = "x"
    out = json.loads(await server.bus_info())
    assert "runs its crown over NATS" in out["warning"] and "no Mattermost election" in out["warning"]


# --- 7. an ambiguous link tag ----------------------------------------------------------------------------------

async def test_a_link_tag_that_fits_two_fresh_names_is_logged_once_and_reported(
        w: World, caplog: pytest.LogCaptureFixture) -> None:
    twin = "agent-" + NAME_A[6:22] + "c" * 10  # the same tag, another name
    assert natsbus_tag(twin) == TAG[NAME_A]
    claimant(w.fake, NAME_A, 9, bot_id=BOT_A)
    claimant(w.fake, twin, 10, bot_id=BOT_A)
    bot(w.inner, BOT_A, "agent-a", 2000, tag=TAG[NAME_A])
    rows = await lease._rows(w.port, WORK)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING):
        assert BOT_A not in w.port.linked(rows)
        assert BOT_A not in w.port.linked(rows)
    assert [r for r in caplog.records if "fits 2 fresh NATS names" in r.getMessage()].__len__() == 1
    assert w.port.ambiguous() == [TAG[NAME_A]]
    assert any(TAG[NAME_A] in n for n in lease.nats_notes())
    w.fake.presence[twin] = "offline"
    assert w.port.linked(rows)[BOT_A] == NAME_A and w.port.ambiguous() == []


def natsbus_tag(name: str) -> str:
    from mattermost_ai_agent_bus import natsbus
    return natsbus.link_tag(name)


# --- the bare minimum that must not change ---------------------------------------------------------------------

async def test_a_session_without_a_nats_home_keeps_the_mattermost_election(tmp_path: Any) -> None:
    """Nothing registered: crown_held is the lease, the election claims (ADR-007)."""
    lease.use_nats("", None)
    inner = make(bus=False)[1]
    bot(inner, BOT, "agent-me", 1000)
    assert not lease.is_nats_home(HOME)
    _file_crown()
    assert lease.crown_held(BOT)
    assert owners and receipt  # the modules the real election uses are the ones imported here


async def test_a_nats_sender_is_shown_by_its_proven_bot_but_stays_the_author(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """#66, RT-6.36 step 8: `shown_as` names the bot of a link proven afresh on every read by
    the one test of a link (lease._linked_owned, owner included), on the post and on its
    reactions; agents DM by it, so nothing is cached. `author`, which decisions read, stays
    the NATS name."""
    from mattermost_ai_agent_bus import slim
    from mattermost_ai_agent_bus.model import Post

    await _roster_world(w)
    post = Post(id="n1:home:7", channel_id=WORK, user_id=NAME_A, message="hi",
                reactions=(Reaction(user_id=NAME_A, post_id="n1:home:7", emoji_name="eyes"),))

    async def one(p: Post = post) -> dict[str, Any]:
        return (await slim.posts(w.port, [p]))[0]  # type: ignore[arg-type]

    got = await one()
    assert got["author"] == NAME_A and got["shown_as"] == "agent-a"
    assert got["reactions"][0]["author"] == NAME_A and got["reactions"][0]["shown_as"] == "agent-a"
    # a reaction alone brings its NATS sender in: the post's author is a Mattermost bot
    by_bot = Post(id="p" * 26, channel_id=WORK, user_id=BOT, message="hi",
                  reactions=(Reaction(user_id=NAME_A, post_id="p" * 26, emoji_name="eyes"),))
    got = await one(by_bot)
    assert "shown_as" not in got and got["reactions"][0]["shown_as"] == "agent-a"
    # only the names asked for
    assert await lease.shown_as(w.port, {NAME_B}) == {}  # type: ignore[arg-type]
    assert await lease.shown_as(w.port, {NAME_A}) == {NAME_A: "agent-a"}  # type: ignore[arg-type]
    bot(w.inner, BOT_A, "agent-a", 2000, crown=True)  # the bot takes its tag back
    assert "shown_as" not in await one()
    bot(w.inner, BOT_A, "agent-a", 2000, crown=True, tag=TAG[NAME_A])
    assert (await one())["shown_as"] == "agent-a"
    w.fake.owners[NAME_A] = "carol"  # the name's owner is not the bot's
    assert "shown_as" not in await one()
    w.fake.owners[NAME_A] = "alice"
    # a failed read shows nothing, and the next read shows again: nothing kept
    real = w.inner.channel_users
    fails = [True]

    async def flaky(channel_id: str) -> list[ChannelUser]:
        if fails[0]:
            fails[0] = False
            raise RuntimeError("chat down")
        return await real(channel_id)

    monkeypatch.setattr(w.inner, "channel_users", flaky)
    assert "shown_as" not in await one()
    assert (await one())["shown_as"] == "agent-a"
    w.port.fail("the credential names another agent")  # the NATS side is lost
    assert "shown_as" not in await one()

async def test_a_silent_holder_yields_only_after_its_last_crown_lease(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 5, RT-6.6 step 6 (#64): a silent holder keeps the crown one crown lease
    from the moment it turned silent; only after it does its own server yield."""
    from mattermost_ai_agent_bus import life
    await _two(w, me_first=True)
    await w.tick()
    assert lease.crown_held(BOT)
    life.turn(False)
    monkeypatch.setattr(life, "_sign", time.time() - 900 - 100)
    claims = len(w.fake.claimed_homes)
    await w.tick()
    assert len(w.fake.claimed_homes) == claims and w.view().holder == NAME_ME  # type: ignore[union-attr]
    monkeypatch.setattr(life, "_sign", time.time() - 900 - 151)
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1
    assert w.view().holder == NAME_A  # type: ignore[union-attr]


async def test_a_silent_holder_in_its_last_lease_does_not_yield_over_a_missing_receipt(
        w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.38 step 5, RT-6.6 step 6 (#64): a post without 👀 or ⏳ for 45 s does not make a
    holder yield while it is silent within its last crown lease; past that lease it yields."""
    from types import SimpleNamespace

    from mattermost_ai_agent_bus import life
    from mattermost_ai_agent_bus.client import post_of
    now = [time.time()]
    monkeypatch.setattr(receipt, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.delenv("MM_ACK_WAIT_SEC", raising=False)
    receipt.reset()
    await _two(w, me_first=True)
    await w.tick()
    assert lease.crown_held(BOT)
    life.turn(False)
    await w.port.get_me()
    pid = "p" * 26
    await receipt.arrived(w.port, post_of({"id": pid, "message": "please look", "user_id": HUMAN,  # type: ignore[arg-type]
                                           "channel_id": WORK, "create_at": 1}), BOT, False)
    assert set(receipt._owed) == {pid}
    monkeypatch.setattr(life, "_sign", time.time() - 900 - 100)  # silent 100 s: in its last lease
    claims = len(w.fake.claimed_homes)
    now[0] += 46
    await w.tick()
    assert len(w.fake.claimed_homes) == claims and w.view().holder == NAME_ME  # type: ignore[union-attr]
    assert set(receipt._owed) == {pid}  # not judged while in the lease
    monkeypatch.setattr(life, "_sign", time.time() - 900 - 151)  # past it
    await w.tick()
    assert len(w.fake.claimed_homes) == claims + 1
    assert w.view().holder == NAME_A  # type: ignore[union-attr]
