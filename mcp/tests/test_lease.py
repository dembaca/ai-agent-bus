"""RT-6.6 coordinator crown and presence lease, kept by the MCP server
(docs/arc42/06-runtime-view.md): status, crown, roster and heartbeat."""

import asyncio
import json
import time

import httpx
import pytest

from mattermost_ai_agent_bus import crownlog, lease, life
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.model import RosterEntry

UID = "u" * 26
OLDER = "o" * 26
YOUNGER = "y" * 26
CHANNEL = "c" * 26
CUSTOM = "/api/v4/users/me/status/custom"


class FakeMattermost:
    """The work channel's accounts with their status, and what was written."""

    def __init__(self):
        self.users: list[dict] = []
        self.presence: dict[str, str] = {}
        self.writes: list[tuple[str, str, dict | None]] = []
        self.fail = False
        self.header: str | None = None   # the work channel's header; None: no permission
        self.patches: list[str] = []

    def agent(self, uid, username, create_at, emoji="", expires_in=600, presence="online"):
        """A bot in the work channel; expires_in=None: a status without expiry."""
        status: dict = {}
        if emoji:
            status = {"emoji": emoji, "text": emoji}
            if expires_in is not None:
                status |= {"duration": "date_and_time", "expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))}
        self.users = [u for u in self.users if u["id"] != uid] + [{
            "id": uid, "username": username, "create_at": create_at, "delete_at": 0,
            "props": {"customStatus": json.dumps(status)} if status else {}}]
        self.presence[uid] = presence

    def custom_status(self) -> dict | None:
        for method, path, body in reversed(self.writes):
            if path == CUSTOM:
                return body if method == "PUT" else None
        return None

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500, json={})
        p = req.url.path
        body = json.loads(req.content) if req.content else None
        if req.method == "GET" and p == "/api/v4/users":
            assert req.url.params["in_channel"] == CHANNEL
            return httpx.Response(200, json=self.users)
        if p == "/api/v4/users/status/ids":
            return httpx.Response(200, json=[{"user_id": u, "status": self.presence.get(u, "offline")}
                                             for u in body])
        if p == f"/api/v4/channels/{CHANNEL}" and req.method == "GET":
            return httpx.Response(200, json={"id": CHANNEL, "name": "agents", "header": self.header or ""})
        if p == f"/api/v4/channels/{CHANNEL}/patch" and req.method == "PUT":
            if self.header is None:
                return httpx.Response(403, json={"message": "no permission"})
            self.header = body["header"]
            self.patches.append(self.header)
            return httpx.Response(200, json={"id": CHANNEL, "name": "agents", "header": self.header})
        if req.method in ("PUT", "DELETE"):
            self.writes.append((req.method, p, body))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    monkeypatch.setenv("MM_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/proj")
    for var in ("MM_PROJECT_CHANNEL_ID", "MM_AGENT_PREFIX", "MM_BOT_USERNAME", "MM_REG_BOT_NAME",
                "MM_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    return FakeMattermost()


@pytest.fixture
def client(mm):
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(mm.handler))
    return c


def _dir(tmp_path):
    return tmp_path / "mm-agent-bus"


def coord(tmp_path) -> dict[str, int]:
    f = _dir(tmp_path) / f"coord-{UID}.env"
    return ({k: int(v) for k, v in (ln.split("=") for ln in f.read_text().split()) if k != "CROWN_HOME"}
            if f.exists() else {})


def holds() -> bool:
    return lease.crown_held(UID)


def me(mm, emoji="hammer_and_wrench", **kw):
    mm.agent(UID, "agent-me", 2000, emoji, **kw)


def _coord_file(tmp_path, monkeypatch, **values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = _dir(tmp_path)
    d.mkdir(exist_ok=True)
    (d / f"coord-{UID}.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


# --- the status ---------------------------------------------------------------------

async def test_every_status_is_a_lease(mm, client, tmp_path):
    """RT-6.6 step 1: a status always carries an expiry, so a dead session's
    runs out; presence online, the project as label and the home after it
    (ADR-002), the state kept locally."""
    await lease.set_status(client, "working")
    s = mm.custom_status()
    assert s["emoji"] == "hammer_and_wrench" and s["text"] == "working · proj · ~agents"
    assert s["duration"] == "date_and_time" and s["expires_at"].endswith("Z")
    assert ("PUT", f"/api/v4/users/{UID}/status", {"user_id": UID, "status": "online"}) in mm.writes
    assert (_dir(tmp_path) / f"presence-{UID}.env").read_text() == "STATE=working\nLABEL=proj\n"
    with pytest.raises(ValueError):
        await lease.set_status(client, "napping")


async def test_listening_ends_with_its_window(mm, client, tmp_path):
    """RT-6.6, RT-6.2 step 3: 👂 until the window's end at the latest; a renewal
    after a window that ran out without a Stop hook shows 💤."""
    until = int(time.time()) + 120
    await lease.set_status(client, "listening", str(until))
    s = mm.custom_status()
    assert s["emoji"] == "ear" and s["text"].startswith("listening · until ")
    assert s["expires_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "ear"
    (_dir(tmp_path) / f"presence-{UID}.env").write_text("STATE=listening\nLABEL=1\n")
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "zzz"
    with pytest.raises(ValueError):
        await lease.set_status(client, "listening", "soon")


async def test_renew_without_a_report_is_on_the_bus(mm, client):
    """RT-6.6 step 1: no turn hooks (Cursor, OpenCode) report a state: 📡."""
    await lease.set_status(client, "renew")
    assert mm.custom_status()["text"] == "on the bus · proj · ~agents"


async def test_offline_ends_status_and_lease(mm, client, tmp_path, monkeypatch):
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    await lease.set_status(client, "working")
    await lease.set_status(client, "offline")
    assert mm.custom_status() is None and mm.writes[-1][0] == "DELETE"
    assert ("PUT", f"/api/v4/users/{UID}/status", {"user_id": UID, "status": "offline"}) in mm.writes
    assert not list(_dir(tmp_path).glob("*.env"))


async def test_status_fails_open(mm, client):
    """RT-6.6: Mattermost failing never surfaces; without a token nothing is sent."""
    mm.fail = True
    await lease.set_status(client, "working")
    client.token = None
    await lease.set_status(client, "working")
    assert await lease.command(client, "who") == ""


# --- the crown -----------------------------------------------------------------------

async def test_oldest_reachable_takes_the_free_crown(mm, client):
    """RT-6.6 step 2: no crown anywhere → the oldest reachable agent takes it."""
    me(mm)
    mm.agent(YOUNGER, "agent-young", 3000, "hammer_and_wrench")
    mm.agent(OLDER, "agent-old-idle", 1000, "zzz")           # idle: not reachable
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    mm.agent("f" * 26, "agent-off", 400, "hammer_and_wrench", presence="offline")
    await lease.set_status(client, "working")
    assert await lease.command(client, "tick") == ""
    assert holds()
    s = mm.custom_status()
    assert s["emoji"] == "crown" and s["text"].startswith("coordinator · working")
    assert [(ln["event"], ln["way"]) for ln in _crown_log()] == [("gained", "election")]


async def test_younger_agent_does_not_claim(mm, client):
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    await lease.command(client, "tick")
    assert not holds()


async def test_take_waits_for_the_holder_then_takes(mm, client, tmp_path):
    """RT-6.6 step 4: on ACK the new coordinator's take waits for the release."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown")
    out = await lease.command(client, "take")
    assert out.startswith("pending: @agent-old still holds the crown")
    assert not holds() and coord(tmp_path)["PENDING"] == 1 and lease.take_pending(UID)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released
    await lease.command(client, "tick")
    assert holds() and coord(tmp_path)["PENDING"] == 0


async def test_release_leaves_room_for_the_successor(mm, client, tmp_path):
    """RT-6.6 step 4: after a release the oldest agent does not grab it back."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    assert holds()
    assert await lease.command(client, "release") == "released"
    assert coord(tmp_path)["YIELD_UNTIL"] > time.time() + 1700
    await lease.command(client, "tick")
    assert not holds()
    assert mm.custom_status()["emoji"] != "crown"


async def test_a_release_during_a_tick_stays_released(mm, client, tmp_path):
    """RT-6.6 step 4: a release that arrives while a tick renews the status
    waits for the tick; the tick's stale reading does not take the crown back."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    renewing, go = asyncio.Event(), asyncio.Event()
    plain = mm.handler

    async def slow(req):
        if req.method == "PUT" and req.url.path == CUSTOM and not go.is_set():
            renewing.set()
            await go.wait()
        return plain(req)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    tick = asyncio.create_task(lease.command(client, "tick"))
    await asyncio.wait_for(renewing.wait(), 2)
    release = asyncio.create_task(lease.command(client, "release"))
    await asyncio.sleep(0.05)
    go.set()
    assert await tick == "" and await release == "released"
    assert not holds() and coord(tmp_path)["YIELD_UNTIL"] > time.time()
    assert mm.custom_status()["emoji"] != "crown"


async def test_two_crowns_the_younger_yields(mm, client, tmp_path):
    """RT-6.6 error handling: a race leaves two crowns; the older keeps it (logged, step 11)."""
    crownlog.reset()
    me(mm, "crown")
    await lease.command(client, "take")
    mm.agent(OLDER, "agent-old", 1000, "crown")
    await lease.command(client, "tick")
    assert not holds()
    assert [(ln["event"], ln["way"]) for ln in _crown_log(tmp_path)] == [("gained", "take"), ("lost", "two_crowns")]


async def test_idle_coordinator_loses_the_crown_when_its_lease_runs_out(mm, client, tmp_path):
    """RT-6.6 step 6: idle extends nothing; once the lease ends the crown is gone."""
    me(mm)
    await lease.command(client, "take")
    until = coord(tmp_path)["CROWN_UNTIL"]
    await lease.set_status(client, "idle")
    assert coord(tmp_path)["CROWN_UNTIL"] == until
    assert mm.custom_status()["emoji"] == "crown"
    f = _dir(tmp_path) / f"coord-{UID}.env"
    f.write_text(f.read_text().replace(f"CROWN_UNTIL={until}", "CROWN_UNTIL=1"))
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "zzz" and not holds()
    assert coord(tmp_path)["CROWN_UNTIL"] == 0


# --- the crown beat and the slot (ADR-007 phase 1) -----------------------------------

MID = "m" * 26


# The expiry is written in whole seconds and _expires() reads the clock again, so a
# second that ticks between the two reads makes it one more than the lease: the
# upper bounds below allow that one second.
LEASE_SLACK = 1


def _expires(mm) -> float:
    """Seconds from now until the custom status written last ends."""
    at = mm.custom_status()["expires_at"]
    return time.mktime(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ")) - time.mktime(time.gmtime())


async def test_the_crown_has_a_short_lease_and_falls_back_on_release(mm, client, tmp_path):
    """RT-6.6 step 6, ADR-007 step 1: the holder's status and crown end after
    MM_CROWN_LEASE_SEC (150 s), renewed by the beat; released, the status
    is a presence lease of MM_LEASE_SEC again."""
    me(mm)
    await lease.set_status(client, "working")
    assert 890 <= _expires(mm) <= 900
    assert await lease.command(client, "take") == "taken"
    assert 140 <= _expires(mm) <= 150 + LEASE_SLACK and coord(tmp_path)["CROWN_UNTIL"] <= time.time() + 150 + LEASE_SLACK
    await lease.set_status(client, "renew")  # the beat
    assert 140 <= _expires(mm) <= 150 + LEASE_SLACK and holds()
    await lease.command(client, "release")
    assert 890 <= _expires(mm) <= 900 and mm.custom_status()["emoji"] != "crown"


async def test_a_lost_crown_falls_back_to_the_long_lease(mm, client):
    """ADR-007 step 1: a holder that yields to an older crown (a race) is a
    member again, with the long lease."""
    me(mm, "crown")
    await lease.command(client, "take")
    mm.agent(OLDER, "agent-old", 1000, "crown")
    await lease.command(client, "tick")
    assert not holds() and 890 <= _expires(mm) <= 900


def test_the_crown_times_keep_their_bounds(monkeypatch):
    """ADR-007 step 1: the beat at least 15 s, the crown lease never below two beats."""
    for var in ("MM_CROWN_BEAT_SEC", "MM_CROWN_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    assert (lease.crown_beat_sec(), lease.crown_lease_sec()) == (60, 150)
    monkeypatch.setenv("MM_CROWN_BEAT_SEC", "5")
    monkeypatch.setenv("MM_CROWN_LEASE_SEC", "20")
    assert (lease.crown_beat_sec(), lease.crown_lease_sec()) == (15, 30)
    monkeypatch.setenv("MM_CROWN_BEAT_SEC", "100")
    monkeypatch.setenv("MM_CROWN_LEASE_SEC", "50")
    assert lease.crown_lease_sec() == 200


async def test_an_expired_crown_is_none_and_the_next_claims(mm, client):
    """RT-6.6 step 1, ADR-007 step 1: a 👑 whose status ended counts for
    nothing; the oldest reachable agent claims."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)
    assert [r.username for r in await lease.roster(client)] == ["agent-me"]
    await lease.command(client, "tick")
    assert holds()


async def test_each_agent_looks_at_its_slot_after_the_holders_end(mm, client):
    """ADR-007 step 2: the holder left out, the n-th agent that stands looks
    n slots after the holder's status would end."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    mm.agent(MID, "agent-mid", 1500, "hammer_and_wrench")
    await lease.command(client, "tick")
    assert not holds()
    end = time.time() + 150
    assert abs(lease.look_due() - (end + 2 * lease.SLOT_SEC)) <= 2
    mm.agent(MID, "agent-mid", 1500, "zzz")  # idle: not reachable, holds no slot
    await lease.command(client, "tick")
    assert abs(lease.look_due() - (end + lease.SLOT_SEC)) <= 2


async def test_a_dead_successor_delays_the_claim_by_one_slot(mm, client):
    """ADR-007 step 2, RT-6.6 error handling: the holder died and the oldest
    successor too, its status still live: the next one claims at its slot,
    not after a lease. Before its slot it does not; with a live crown again
    it plans for that crown's end."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    mm.agent(MID, "agent-mid", 1500, "hammer_and_wrench")   # dead, status still live
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)   # the holder's status ended
    assert not await lease.look(client)  # not due yet
    assert not holds()
    lease._look_at = time.time() - 1   # its slot has come
    assert await lease.look(client) and holds()
    assert mm.custom_status()["emoji"] == "crown" and 140 <= _expires(mm) <= 150 + LEASE_SLACK
    assert lease.look_due() == 0  # a holder looks for nothing


async def test_a_live_crown_at_the_slot_is_left_alone(mm, client):
    """ADR-007 step 2: the holder beat in time: the look claims nothing and
    plans for the new end."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=300)
    lease._look_at = time.time() - 1
    assert not await lease.look(client) and not holds()
    assert abs(lease.look_due() - (time.time() + 300 + lease.SLOT_SEC)) <= 2


async def test_no_slot_after_a_release_or_while_idle(mm, client, tmp_path, monkeypatch):
    """ADR-007 step 2, RT-6.6 step 4: a released crown is not claimed back
    before YIELD_UNTIL; an agent that is not reachable plans no look."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=0, PENDING=0, YIELD_UNTIL=int(time.time()) + 1800)
    await lease.command(client, "tick")
    assert lease.look_due() >= time.time() + 1790
    me(mm, "zzz")
    await lease.command(client, "tick")
    assert lease.look_due() == 0


async def test_without_a_holder_seen_only_the_oldest_plans(mm, client):
    """ADR-007 step 2: the slots count from the holder's end, the one time all
    servers share; with no holder seen, the others wait and only the oldest
    that stands looks a slot from now."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    await lease.command(client, "tick")
    assert not holds() and lease.look_due() == 0


async def _run_heartbeat(monkeypatch, calls, until, *, holding, look_result=False, watch=None):
    """Run lease.heartbeat with fakes until `until(calls)` holds (2 s at most)."""
    class _Client:
        token = "t"
        session = None

    async def fake_command(client, action):
        calls.append(action)
        return ""

    async def fake_status(client, state, label=""):
        calls.append(state)

    async def fake_look(client):
        calls.append("look")
        return look_result

    async def get_client():
        return _Client()

    monkeypatch.setattr(lease, "command", fake_command)
    monkeypatch.setattr(lease, "set_status", fake_status)
    monkeypatch.setattr(lease, "look", fake_look)
    monkeypatch.setattr(lease, "crown_held", lambda uid: holding[0])
    monkeypatch.setattr(lease, "crown_beat_sec", lambda: 0.01)
    monkeypatch.setattr(lease, "heartbeat_sec", lambda: 60.0)
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    task = asyncio.create_task(lease.heartbeat(get_client, watch))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if until(calls):
            break
    task.cancel()


async def test_the_holder_beats_between_ticks(monkeypatch):
    """ADR-007 step 1: between ticks the holder renews its status every beat,
    without the roster read or the watch of a tick."""
    calls: list[str] = []

    async def watch(client):
        calls.append("watch")
        return None

    await _run_heartbeat(monkeypatch, calls, lambda c: c.count("renew") >= 3, holding=[True], watch=watch)
    assert calls[:2] == ["tick", "watch"] and calls.count("tick") == 1
    assert calls.count("renew") >= 3 and calls.count("watch") == 1


async def test_a_crown_taken_between_ticks_is_beaten_in_time(monkeypatch):
    """ADR-007 step 1: the loop wakes at least every beat, so a crown taken
    with coordinator("take") after a tick is renewed before its short lease
    ends, not at the next tick."""
    calls: list[str] = []
    holding = [False]

    def until(c):
        if c.count("tick") == 1:
            holding[0] = True  # the tool's take, while the loop sleeps
        return c.count("renew") >= 2

    await _run_heartbeat(monkeypatch, calls, until, holding=holding)
    assert calls.count("tick") == 1 and calls.count("renew") >= 2


async def test_a_member_looks_at_its_slot_and_a_claim_brings_the_tick(monkeypatch):
    """ADR-007 step 2: between ticks a member looks, not ticks; a claim
    brings the next tick forward to re-check for a second crown."""
    calls: list[str] = []
    monkeypatch.setattr(lease, "PENDING_POLL_SEC", 0.02)
    await _run_heartbeat(monkeypatch, calls, lambda c: c.count("tick") >= 2,
                         holding=[False], look_result=True)
    assert calls[0] == "tick" and "look" in calls[1:calls.index("tick", 1)]


async def test_a_released_crown_is_not_claimed_at_a_slot(mm, client):
    """ADR-007 step 2, RT-6.6 step 4: a holder still reachable without the
    👑 released or yielded it; the slot leaves that to the tick's election and
    a pending take, and plans nothing more until the tick."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released, still here
    lease._look_at = time.time() - 1
    assert not await lease.look(client) and not holds()
    assert lease.look_due() == 0


async def test_a_slot_claim_yields_to_an_older_crown_of_a_race(mm, client):
    """ADR-007 step 2, RT-6.6 error handling: a slot claim and another claim at
    once leave two crowns; the tick the claim brought forward yields to the older."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)
    lease._look_at = time.time() - 1
    assert await lease.look(client) and holds()
    mm.agent(MID, "agent-mid", 1500, "crown")   # claimed in the same second
    await lease.command(client, "tick")
    assert not holds()


async def test_the_oldest_plans_after_its_release(mm, client, tmp_path, monkeypatch):
    """ADR-007 step 2: with no holder seen the oldest plans a slot from now,
    not before its YIELD_UNTIL (RT-6.6 step 4)."""
    me(mm)
    mm.agent(YOUNGER, "agent-young", 3000, "hammer_and_wrench")
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=0, PENDING=0, YIELD_UNTIL=int(time.time()) + 600)
    await lease.command(client, "tick")
    assert not holds() and lease.look_due() >= time.time() + 590


async def test_a_claim_that_could_not_be_written_plans_no_look(mm, client, monkeypatch):
    """ADR-007 step 2: a state directory that takes no write fails open: no
    crown, and no look every second until the tick."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=150)
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)
    monkeypatch.setattr(lease, "_save_coord", lambda *a: None)
    lease._look_at = time.time() - 1
    assert not await lease.look(client) and not holds() and lease.look_due() == 0
    assert _crown_log() == []                                 # no claim, no `gained slot` (#64)


# --- the roster ------------------------------------------------------------------------

async def test_roster_lists_coordinator_first_and_skips_the_unreachable(mm, client):
    """RT-6.6, RT-6.14 step 4: the crown holder heads the roster and is marked;
    idle, offline and stale bots are left out."""
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "agent-young", 3000, "crown")
    mm.agent("i" * 26, "agent-idle", 1500, "zzz")
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    mm.agent("x" * 26, "agent-gone", 600, "hammer_and_wrench", expires_in=-60)
    assert await lease.roster(client) == [
        RosterEntry("agent-young", "crown", coordinator=True, crown=True),
        RosterEntry("agent-old", "hammer_and_wrench")]
    assert await lease.command(client, "who") == "agent-young"


async def test_without_a_crown_the_oldest_reachable_heads_the_roster(mm, client):
    mm.agent(YOUNGER, "agent-young", 3000, "ear")
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    rows = await lease.roster(client)
    assert [(r.username, r.coordinator, r.crown) for r in rows] == [
        ("agent-old", True, False), ("agent-young", False, False)]
    assert await lease.command(client, "who") == "agent-old"


async def test_roster_and_crown_follow_the_registrar_prefix(mm, client, monkeypatch):
    """RT-6.6, arc42 §7.1: the bus's bots are the ones with the registrar's
    prefix, handed in as MM_AGENT_PREFIX or derived from the session's own bot."""
    mm.agent(OLDER, "bot-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "bot-young", 3000, "crown")
    mm.agent("x" * 26, "agent-other", 2000, "hammer_and_wrench")

    async def names() -> list[str]:
        return [r.username for r in await lease.roster(client)]

    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    assert await names() == ["bot-young", "bot-old"]
    assert await lease.command(client, "who") == "bot-young"
    monkeypatch.delenv("MM_AGENT_PREFIX")
    monkeypatch.setenv("MM_BOT_USERNAME", "bot-me")
    monkeypatch.setenv("MM_REG_BOT_NAME", "me")
    assert await lease.command(client, "who") == "bot-young"
    monkeypatch.delenv("MM_BOT_USERNAME")
    monkeypatch.delenv("MM_REG_BOT_NAME")
    assert await names() == ["agent-other"]


async def test_an_unreadable_roster_is_empty(mm, client):
    me(mm)
    mm.fail = True
    assert await lease.roster(client) == []
    assert await lease.command(client, "tick") == ""


def test_roster_rows_become_status_words():
    """RT-6.14 step 4: the status text's word after "coordinator", else the
    lease emoji's; the custom status stays in lease.py."""
    rows = [{"username": "agent-b", "emoji": "crown", "coordinator": True, "crown": True,
             "text": "coordinator · working · bus"},
            {"username": "agent-a", "emoji": "ear", "text": ""},
            {"username": "agent-c", "emoji": "zzz", "presence": "away", "reachable": True}]
    assert [lease.entry(r) for r in rows] == [
        RosterEntry("agent-b", "working", coordinator=True, crown=True),
        RosterEntry("agent-a", "listening"),
        RosterEntry("agent-c", "idle"),
    ]


# --- crown prefix and heartbeat ------------------------------------------------------------

def test_coordinator_posts_start_with_the_crown(tmp_path, monkeypatch):
    """RT-6.6: bot posts never show a status, so the crown goes into the post."""
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    # The caller says where the post goes (crown_here); the default is plain (ADR-002, #258).
    assert lease.with_crown("ACK", UID) == "ACK"
    assert lease.with_crown("ACK", UID, here=True) == "\U0001F451 ACK"
    assert lease.with_crown("\U0001F451 ACK", UID, here=True) == "\U0001F451 ACK"


def test_no_prefix_without_a_live_crown(tmp_path, monkeypatch):
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=1)
    assert lease.with_crown("ACK", UID) == "ACK"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "none"))
    assert lease.with_crown("ACK", UID) == "ACK"


async def test_heartbeat_ticks_and_survives_errors(monkeypatch):
    """RT-6.6 step 1: the heartbeat renews the lease; a failure never stops it."""
    calls: list[str] = []

    class _Client:
        token = "t"
        session = None

    async def fake_command(client, action):
        calls.append(action)
        if len(calls) == 1:
            raise RuntimeError("bus down")
        return ""

    async def get_client():
        return _Client()

    monkeypatch.setattr(lease, "command", fake_command)
    monkeypatch.setattr(lease, "heartbeat_sec", lambda: 0.01)
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    task = asyncio.create_task(lease.heartbeat(get_client))
    await asyncio.sleep(0.05)
    task.cancel()
    assert calls.count("tick") >= 2


async def test_idle_in_a_channel_session_is_reachable_and_keeps_the_crown(mm, client, tmp_path):
    """RT-6.6 step 6, RT-6.12: the server pushes into a session started as a
    channel, so between its turns it stays reachable and renews its crown."""
    me(mm)
    await lease.command(client, "take")
    (_dir(tmp_path) / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    until = coord(tmp_path)["CROWN_UNTIL"]
    f = _dir(tmp_path) / f"coord-{UID}.env"
    f.write_text(f.read_text().replace(f"CROWN_UNTIL={until}", f"CROWN_UNTIL={until - 60}"))
    await lease.set_status(client, "idle")
    assert mm.custom_status()["emoji"] == "crown" and holds()
    assert coord(tmp_path)["CROWN_UNTIL"] >= until
    await lease.command(client, "release")
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "ear"


# --- the agent's sign of life (ADR-007 amendment step 1, phase D) --------------------------

def _no_sign_since(monkeypatch, ago: float) -> float:
    at = time.time() - ago
    monkeypatch.setattr(life, "_sign", at)
    return at


async def test_an_agent_without_a_sign_is_silent_and_not_reachable(mm, client, monkeypatch):
    """RT-6.6 step 1: the server's heartbeat is no sign of the agent; after the
    window without one the status shows 💤 `silent`, which every plugin reads
    as not reachable, and the next sign shows the state again."""
    at = _no_sign_since(monkeypatch, 901)
    await lease.set_status(client, "bus")
    s = mm.custom_status()
    assert s["emoji"] == "zzz"
    assert s["text"] == f"silent · no sign since {time.strftime('%H:%MZ', time.gmtime(at))} · ~agents"
    assert life.is_shown()
    me(mm, "zzz")
    mm.users[-1]["props"]["customStatus"] = json.dumps({**json.loads(mm.users[-1]["props"]["customStatus"]),
                                                        "text": s["text"]})
    rows = await lease._rows(client, CHANNEL)
    assert lease.not_reachable(rows[0]) == "silent"
    life.take_up()
    await lease.set_status(client, "renew")
    assert mm.custom_status()["text"] == "on the bus · proj · ~agents" and not life.is_shown()


async def test_an_idle_agent_stays_idle_not_silent(mm, client, monkeypatch):
    """RT-6.6 step 1: 💤 idle is not reachable anyway, so its text stays."""
    _no_sign_since(monkeypatch, 3600)
    await lease.set_status(client, "idle")
    assert mm.custom_status()["text"].startswith("idle · until my human resumes")
    assert not life.is_shown() and not lease._silent(UID)


async def test_a_channel_session_is_silent_only_with_a_push_untaken(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1, RT-6.12: idle in a session started as a channel has
    nothing to prove past the window; a push untaken for MM_ACK_WAIT_SEC
    makes it silent (case 1 of the amendment)."""
    _no_sign_since(monkeypatch, 3600)
    _dir(tmp_path).mkdir(exist_ok=True)
    (_dir(tmp_path) / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    await lease.set_status(client, "idle")
    assert mm.custom_status()["emoji"] == "ear"
    monkeypatch.setattr(life, "_untaken", time.time() - 46)
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "zzz" and mm.custom_status()["text"].startswith("silent · ")


async def test_a_silent_holder_drops_the_crown_and_does_not_claim(mm, client, tmp_path, monkeypatch):
    """RT-6.6 steps 1, 4 and 6: a holder silent for one crown lease (#64) drops the
    crown with that write, as a release does (no claim for two leases), and claims
    neither at its tick nor at a slot while silent."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    _no_sign_since(monkeypatch, 900 + 150)  # silent at 900 s, then one crown lease
    await lease.set_status(client, "renew")
    assert not holds() and coord(tmp_path)["CROWN_UNTIL"] == 0
    # One crown lease, not a release's two leases: the silence may end soon.
    assert time.time() + 140 - LEASE_SLACK <= coord(tmp_path)["YIELD_UNTIL"] <= time.time() + 150 + LEASE_SLACK
    assert mm.custom_status()["emoji"] == "zzz"
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=0, PENDING=0, YIELD_UNTIL=0)
    me(mm, "zzz")
    await lease.command(client, "tick")
    assert not holds()
    me(mm)  # an older plugin's view of the status, to reach the slot's own check
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)
    lease._holder_id, lease._look_at = OLDER, time.time() - 1
    assert not await lease.look(client) and not holds()


async def test_a_silent_holder_keeps_the_crown_one_crown_lease(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1 (#64, decided 2026-11-24): silence changes the display only; the
    holder keeps its 👑 one crown lease from the moment it turned silent, its text says
    the silence, without the receipt marker (no peer claims over a missing 👀), and its
    status ends where that lease does."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    at = _no_sign_since(monkeypatch, 900 + 100)  # silent since 100 s
    await lease.set_status(client, "renew")
    s = mm.custom_status()
    assert holds() and s["emoji"] == "crown"
    assert s["text"].startswith(f"coordinator · silent · no sign since {time.strftime('%H:%MZ', time.gmtime(at))}")
    assert not lease.sets_receipts(s["text"]) and life.is_shown()
    row = {"reachable": True, "live": True, "emoji": "crown", "text": s["text"]}
    assert lease.not_reachable(row) == "silent"  # a TASK to it says so (RT-6.14 step 10)
    end = int(at) + 900 + 150
    assert s["expires_at"] == lease._rfc3339(end)
    assert coord(tmp_path)["CROWN_UNTIL"] >= end + lease.crown_beat_sec()  # the drop is at the end
    # the beat renews nothing while silent: the end stays where it is
    await lease.set_status(client, "renew")
    assert mm.custom_status()["expires_at"] == lease._rfc3339(end)
    # the end reached: dropped, logged as silent
    monkeypatch.setattr(life, "_sign", at - 50)
    await lease.set_status(client, "renew")
    assert not holds() and mm.custom_status()["emoji"] == "zzz"


async def test_a_sign_within_the_last_lease_keeps_the_crown_and_restarts_the_count(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1 (#64): a sign of the agent while it is silent ends the silence;
    the crown is renewed as before, and a later silence counts its lease anew."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    _no_sign_since(monkeypatch, 900 + 140)
    await lease.set_status(client, "renew")
    assert holds() and mm.custom_status()["text"].startswith("coordinator · silent · ")
    life.take_up()
    await lease.set_status(client, "renew")
    s = mm.custom_status()
    assert holds() and s["emoji"] == "crown" and lease.sets_receipts(s["text"])
    assert "silent" not in s["text"] and not life.is_shown()
    assert coord(tmp_path)["CROWN_UNTIL"] >= time.time() + lease.crown_lease_sec() - 2  # int seconds + a tick
    _no_sign_since(monkeypatch, 900 + 10)  # silent again, 10 s: a fresh lease
    await lease.set_status(client, "renew")
    assert holds() and mm.custom_status()["emoji"] == "crown"


async def test_a_push_left_untaken_keeps_the_crown_one_crown_lease(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1 (#64): a channel session's holder silent by a push left untaken
    for MM_ACK_WAIT_SEC keeps the crown one crown lease from there, then drops it."""
    me(mm)
    _dir(tmp_path).mkdir(exist_ok=True)
    (_dir(tmp_path) / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    assert await lease.command(client, "take") == "taken"
    await lease.set_status(client, "idle")
    monkeypatch.setattr(life, "_untaken", time.time() - 45 - 149)
    await lease.set_status(client, "renew")
    assert holds() and mm.custom_status()["text"].startswith("coordinator · silent · ")
    monkeypatch.setattr(life, "_untaken", time.time() - 45 - 150)
    await lease.set_status(client, "renew")
    assert not holds() and mm.custom_status()["emoji"] == "zzz"


async def test_a_silent_agent_never_claims_at_its_tick(mm, client, monkeypatch):
    """RT-6.6 step 2: silent, the agent is no candidate, even where a roster
    read before its 💤 arrived still shows it reachable."""
    me(mm)
    _no_sign_since(monkeypatch, 901)
    await lease.command(client, "tick")
    assert not holds()


async def test_a_failed_write_is_not_taken_as_shown(mm, client, monkeypatch):
    """RT-6.6 step 1: the flip counts as shown only once Mattermost took it,
    so the heartbeat writes a failed one again."""
    _no_sign_since(monkeypatch, 901)
    mm.fail = True
    await lease.set_status(client, "bus")
    assert not life.is_shown() and lease._silent(UID)
    mm.fail = False
    await lease.set_status(client, "renew")
    assert life.is_shown()


async def test_a_listen_window_that_ran_out_is_idle_not_silent(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1, RT-6.2 step 3: a window that ended without the Stop hook
    counts as 💤 idle, which is not reachable anyway; within it the window
    rule holds (the listener's reads are the signs)."""
    _no_sign_since(monkeypatch, 901)
    _dir(tmp_path).mkdir(exist_ok=True)
    (_dir(tmp_path) / f"presence-{UID}.env").write_text(f"STATE=listening\nLABEL={int(time.time()) + 600}\n")
    assert lease._silent(UID)
    (_dir(tmp_path) / f"presence-{UID}.env").write_text("STATE=listening\nLABEL=1\n")
    assert not lease._silent(UID) and lease._judged(UID) == (False, False)


async def test_a_server_restarted_during_a_turn_keeps_it(monkeypatch, tmp_path):
    """RT-6.6 step 1: a new server whose presence file says working (a
    reconnect during a turn) counts the turn as running, so a push it sends
    then is the turn's; the Stop hook ends it."""
    calls: list[str] = []
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    _dir(tmp_path).mkdir(exist_ok=True)
    (_dir(tmp_path) / f"presence-{UID}.env").write_text("STATE=working\nLABEL=proj\n")
    await _run_heartbeat(monkeypatch, calls, lambda c: "tick" in c, holding=[False])
    assert life._turn


async def test_a_silent_member_is_written_once_by_the_heartbeat(monkeypatch):
    """RT-6.6 step 1: between ticks the heartbeat writes the status when the
    agent turns silent (one write per flip), not at every wake."""
    calls: list[str] = []
    _no_sign_since(monkeypatch, 901)

    def until(c):
        if c.count("renew") == 1:
            life.shown(True)  # what the real set_status records
        return c.count("look") >= 2

    await _run_heartbeat(monkeypatch, calls, until, holding=[False])
    assert calls[:2] == ["tick", "renew"] and calls.count("renew") == 1


# --- the work channel's header (RT-6.6 step 7) ---------------------------------------

BUS = "\U0001F68C agent bus · plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you"


def _old_bus(name: str, at: str) -> str:
    """The bus part before 0.65.4: it named the holder and the time."""
    return (f"🚌 agent bus · 👑 @{name} since {at}\n"
            "plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you")


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    monkeypatch.setattr(lease, "_header_line", {})
    monkeypatch.setattr(lease, "_look_at", 0.0)
    monkeypatch.setattr(lease, "_vacant_at", 0.0)
    monkeypatch.setattr(lease, "_holder_id", "")


async def test_the_header_bus_part_is_static(mm, client):
    """RT-6.6 step 7: the crown holder's server puts the addressing rule into
    the work channel's header, after what people wrote there; it names no
    holder. Every change posts a system message, so a renewal and a crown
    change write nothing once the header says it."""
    assert lease.BUS_HEADER == BUS
    mm.header = "Release on Fridays"
    me(mm)
    await lease.command(client, "tick")
    assert holds() and mm.header == f"Release on Fridays\n\n{BUS}"
    await lease.command(client, "tick")
    await lease.command(client, "take")
    assert len(mm.patches) == 1
    await lease.command(client, "release")
    assert await lease.command(client, "take") == "taken"  # the crown passes again
    assert len(mm.patches) == 1
    mm.header = "Release on Fridays"                       # someone removed it
    await lease.command(client, "release")
    await lease.command(client, "take")
    assert mm.header == f"Release on Fridays\n\n{BUS}" and len(mm.patches) == 2


async def test_an_old_crown_naming_header_is_replaced_once(mm, client):
    """RT-6.6 step 7: the bus part before 0.65.4 (holder and time) is replaced
    where it stands, once; what people wrote before and after it stays, and
    the next crown change leaves the header alone."""
    mm.header = f"Notes\n\n{_old_bus('agent-old', '22:40')}\nmore notes"
    me(mm)
    await lease.command(client, "tick")
    assert mm.header == f"Notes\n\n{BUS}\nmore notes" and len(mm.patches) == 1
    await lease.command(client, "release")
    mm.agent(OLDER, "agent-old", 1000, "crown")              # another holder now
    await lease.command(client, "tick")
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench", expires_in=-60)
    await lease.command(client, "take")
    assert holds() and len(mm.patches) == 1


async def test_a_holder_checks_the_header_once_after_it_starts(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 7: a server that starts while its agent holds the crown (an
    upgrade) replaces an old bus part once, without a crown change."""
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    me(mm, "crown")
    mm.header = _old_bus("agent-me", "09:00")
    await lease.command(client, "tick")
    await lease.command(client, "tick")
    assert holds() and mm.header == BUS and len(mm.patches) == 1


def test_the_header_stays_within_mattermosts_limit():
    """RT-6.6 step 7: past 1024 characters people's text is cut, never the bus part."""
    bus = lease.BUS_HEADER
    out = lease.with_bus_header("x" * 2000, bus)
    assert len(out) == lease.HEADER_MAX and out.endswith(bus) and "…\n\n" in out
    assert lease.with_bus_header("", bus) == bus
    assert lease.with_bus_header(f"a\n{bus}", bus) == f"a\n\n{bus}"


async def test_without_permission_the_crown_works_and_it_logs_once(mm, client, caplog, monkeypatch):
    """RT-6.6 step 7: a header the bot may not change is logged once; the crown is taken."""
    monkeypatch.setattr(lease, "_header_logged", False)
    me(mm)
    with caplog.at_level("DEBUG", logger="mattermost_ai_agent_bus.lease"):
        await lease.command(client, "tick")
        assert holds()
        await lease._header(client, CHANNEL, UID)
    assert mm.patches == []
    assert [r.levelname for r in caplog.records if "header" in r.getMessage()] == ["WARNING", "DEBUG"]


# --- the link segment of the status text (RT-6.37) ---------------------------------------------

TAG = "abcdefghijklmnop"


def test_the_link_segment_sits_before_the_home_and_is_read_back() -> None:
    from mattermost_ai_agent_bus import release, usage

    text = lease.status_text("working", "bus", ["ctx 62%", "5h 40%"], "home", version="v0.95.0", link=TAG)
    assert text == f"working · bus · ctx 62% · 5h 40% · v0.95.0 · ⇄{TAG} · ~home"
    assert lease.link_of(text) == TAG and lease.home_of(text) == "home"
    assert usage.from_status(text)["ctx"] == 62 and release.from_status(text) == {"version": "0.95.0"}
    assert lease.status_text("working", "bus", ["ctx 62%"], "home") == "working · bus · ctx 62% · ~home"
    assert lease.link_of("working · bus · ctx 62% · ~home") == ""
    assert lease.homed("working", "home", TAG) == f"working · ⇄{TAG} · ~home"
    assert lease.status_text("idle", "", [], "", link=TAG) == f"idle · ⇄{TAG}"  # no home: the link last


def test_the_link_and_a_long_home_are_never_cut() -> None:
    home = "h" * 64
    for args in (("working", "x" * 80, ["ctx 62%", "5h 40%", "7d 10%"], home, "v0.95.0"),
                 ("working", "bus", ["ctx 62%"], home, ""), ("w" * 90, "", [], home, ""),
                 ("working", "x" * 80, [], home, "v0.95.0")):
        text = lease.status_text(*args, link=TAG)
        assert len(text) <= lease.STATUS_MAX
        assert text.endswith(f" · ⇄{TAG} · ~{home}"), text
        assert lease.link_of(text) == TAG and lease.home_of(text) == home


def test_a_holders_receipt_marker_is_never_cut_by_the_link() -> None:
    """RT-6.6 step 10, RT-6.37: the link segment makes the status longer; the cut takes
    the label and the segments, never a holder's receipt marker, the link or the home."""
    head = f"coordinator · working · {lease.RECEIPT_MARK}"
    for n in range(0, 65):
        home = "h" * n if n else ""
        for label in ("", "bus", "x" * 70):
            for link in ("", TAG):
                text = lease.status_text(head, label, ["ctx 62%", "5h 40%"], home, "v0.97.0", link=link)
                assert len(text) <= lease.STATUS_MAX, text
                assert lease.sets_receipts(text), text
                assert lease.link_of(text) == link, text
                if home:
                    assert lease.home_of(text) == home, text


def test_the_cut_leaves_no_separator_before_the_link() -> None:
    """A cut right after a "·" must not leave `… · · ⇄<tag>`, which hid the link."""
    for n in range(1, 100):
        text = lease.homed("a · " * 30, "h" * (n % 65), link=TAG)
        assert " · · " not in text and lease.link_of(text) == TAG, text


def test_link_of_takes_the_last_tag_before_the_home() -> None:
    other = "z" * 16
    assert lease.link_of(f"a · ⇄{other} · b · ⇄{TAG} · ~home") == TAG
    assert lease.link_of(f"label ⇄{other} · ⇄{TAG} · ~home") == TAG
    assert lease.link_of(f"working · ⇄{other} · bus · ~home") == ""  # a tag in a label is no link


def test_link_of_reads_only_a_well_formed_segment() -> None:
    assert lease.link_of(f"working · ⇄{TAG}") == TAG
    for bad in ("", "working", "working · ⇄short", f"working · ⇄{TAG.upper()}", f"working · ⇄{TAG}x",
                f"working · x⇄{TAG}", f"working ⇄{TAG}"):
        assert lease.link_of(bad) == ""
    assert lease.home_of(f"working · ⇄{TAG} · ~home") == "home"
    assert lease.home_of(f"working · ⇄{TAG}") is None  # a link is no home


def test_the_status_carries_the_link_of_the_registry() -> None:
    assert lease._nats_link is None  # no NATS home, no link
    lease.use_nats_link(lambda: TAG)
    try:
        assert lease._nats_link is not None and lease._nats_link() == TAG
    finally:
        lease.use_nats_link(None)
    assert lease._nats_link is None


# --- a waiting take shows; the crown log (#64) -----------------------------------------

def _status_text(mm, uid: str, text: str) -> None:
    """Another agent's status text, as its server would write it."""
    for u in mm.users:
        if u["id"] == uid:
            status = json.loads(u["props"]["customStatus"])
            u["props"]["customStatus"] = json.dumps(status | {"text": text})


def _crown_log(_tmp_path=None) -> list[dict]:
    """The crown log of this run's state directory (RT-6.6 step 11)."""
    f = crownlog.log_path()
    return [json.loads(ln) for ln in f.read_text().splitlines()] if f and f.exists() else []


async def test_a_waiting_take_shows_in_the_status(mm, client, tmp_path):
    """RT-6.6 step 4 (#64): the take waits in this server's file, and its status says so
    at once, with the time it counts until; once taken, the segment is gone."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown")
    lease._nudge().clear()
    assert (await lease.command(client, "take")).startswith("pending:")
    assert lease._nudge().is_set()                            # the heartbeat checks it from now on
    assert lease._take_shown(mm.custom_status()["text"], time.time())
    assert not lease._take_shown(mm.custom_status()["text"], time.time() + lease.TAKE_MARK_SEC + 16)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released
    await lease.command(client, "tick")
    assert holds() and "take pending" not in mm.custom_status()["text"]


def _waiting(now: float | None = None, home: str = "agents") -> str:
    """A taker's status text as its server writes it (RT-6.6 step 4, #64)."""
    return f"listening · {lease._take_mark(time.time() if now is None else now)} · ~{home}"


async def test_an_elected_peer_leaves_a_free_crown_to_a_waiting_take(mm, client):
    """RT-6.6 step 4 (#64): after a release the oldest peer's tick may come before the
    taker's; it sees the taker's mark and does not claim."""
    me(mm)                                                    # the oldest reachable one
    mm.agent(YOUNGER, "agent-taker", 3000, "ear")
    _status_text(mm, YOUNGER, _waiting())
    await lease.command(client, "tick")
    assert not holds()
    _status_text(mm, YOUNGER, "listening · ~agents")          # the take is through, or gone
    await lease.command(client, "tick")
    assert holds()


async def test_a_slot_leaves_a_free_crown_to_a_waiting_take(mm, client, tmp_path):
    """RT-6.6 step 4 (#64): the holder went (silent, gone): no slot claims while a take
    waits; once it does not, the slot claims and logs it (step 11)."""
    crownlog.reset()
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown", expires_in=-5)
    mm.agent(YOUNGER, "agent-taker", 3000, "ear")
    _status_text(mm, YOUNGER, _waiting())
    lease._holder_id, lease._look_at = OLDER, time.time() - 1
    assert not await lease.look(client) and not holds()
    assert _crown_log(tmp_path) == []
    _status_text(mm, YOUNGER, "listening · ~agents")
    lease._holder_id, lease._look_at = OLDER, time.time() - 1
    assert await lease.look(client) and holds()
    assert [(ln["event"], ln["way"]) for ln in _crown_log(tmp_path)] == [("gained", "slot")]


@pytest.mark.parametrize("text", [
    _waiting(time.time() - 61),                               # its time ran out
    _waiting(time.time() + 3600),                             # too far ahead: not the server's
    _waiting(home="elsewhere"),                               # a guest from another home
    "listening · take pending · ~agents",                     # no time at all
], ids=["expired", "far-ahead", "guest", "no-time"])
async def test_a_mark_out_of_time_or_from_a_guest_counts_for_nothing(mm, client, text):
    """RT-6.6 step 4 (#64): only a peer of this home with a mark in time holds the crown off."""
    me(mm)
    mm.agent(YOUNGER, "agent-taker", 3000, "ear")
    _status_text(mm, YOUNGER, text)
    await lease.command(client, "tick")
    assert holds()


async def test_a_mark_from_an_agent_that_does_not_stand_counts_for_nothing(mm, client, monkeypatch):
    """RT-6.6 step 4 (#64): a foreign agent (another owner's circle) stands in no election
    of this home, and its mark holds nothing off."""
    me(mm)
    mm.agent(YOUNGER, "agent-foreign", 3000, "ear")
    _status_text(mm, YOUNGER, _waiting())

    async def peers_only(client, rows):
        return lease.Election([r["id"] for r in rows if r["reachable"] and r["id"] != YOUNGER])

    monkeypatch.setattr(lease, "_election", peers_only)
    await lease.command(client, "tick")
    assert holds()


async def test_the_mark_skips_this_agent_and_the_unreachable(mm, client):
    me(mm)
    rows = [{"id": UID, "reachable": True, "home": "agents", "text": _waiting()},
            {"id": YOUNGER, "reachable": False, "home": "agents", "text": _waiting()}]
    assert not await lease._taker_waits(client, rows, UID)
    rows[1]["reachable"] = True
    assert await lease._taker_waits(client, rows, UID)
    rows[1]["home"] = None                                    # a status that names no home
    assert not await lease._taker_waits(client, rows, UID)


async def test_no_label_can_write_the_mark(mm, client):
    """RT-6.6 step 4 (#64): only the server writes a waiting take; a presence label with
    the words (the hook API, a directory's name) loses them."""
    me(mm)
    for label in ("take pending until 23:59:59Z", "Take  Pending", "take-pending",
                  "taketake pending pending until 23:59:59Z"):     # review of #76: no cleaning
        await lease.set_status(client, "working", label)
        text = mm.custom_status()["text"]
        assert "pending" not in text.lower() and not lease._take_shown(text, time.time()), label
        assert text.startswith("working · ")


async def test_a_take_after_this_agents_own_release_is_not_held_back(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 4 (#64): an agent that released earlier yields for two leases; its own
    take later must still win the free crown, which its mark keeps from everyone else."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown")
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=0, PENDING=0, YIELD_UNTIL=int(time.time()) + 1800)
    assert (await lease.command(client, "take")).startswith("pending:")
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released
    await lease.command(client, "tick")
    assert holds()


async def test_the_crown_log_names_each_way(mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 11 (#64): a line each time the crown passes, with what the sign-of-life
    rule saw; nothing of a post."""
    crownlog.reset()
    me(mm)
    assert await lease.command(client, "take") == "taken"
    assert await lease.command(client, "release") == "released"
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=0, PENDING=0, YIELD_UNTIL=0)
    assert await lease.command(client, "take") == "taken"
    since = _no_sign_since(monkeypatch, 901)
    monkeypatch.setattr(life, "_untaken", time.time() - 200)  # silent 45 s on, then a lease
    await lease.set_status(client, "renew")                   # the silent drop
    lines = _crown_log(tmp_path)
    assert [(ln["event"], ln["way"]) for ln in lines] == [
        ("gained", "take"), ("lost", "release"), ("gained", "take"), ("lost", "silent")]
    silent = lines[-1]
    assert silent["since"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    assert silent["turn"] is False and 199 <= silent["untaken_sec"] <= 202
    assert set(silent) == {"t", "event", "way", "turn", "since", "untaken_sec"}


def test_a_crown_that_ends_without_a_rule_is_logged_as_lapsed(tmp_path, monkeypatch):
    """RT-6.6 step 11: the heartbeat's lapse gets a line only when no rule wrote one."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    crownlog.reset()
    crownlog.lapsed()                                         # held nothing: no line
    crownlog.note("gained", "election")
    crownlog.lapsed()
    crownlog.lapsed()                                         # once per hold
    crownlog.note("gained", "slot")
    crownlog.note("lost", "taken_over", by=OLDER)
    crownlog.lapsed()                                         # the rule wrote its line
    assert [(ln["event"], ln["way"], ln.get("by")) for ln in _crown_log(tmp_path)] == [
        ("gained", "election", None), ("lost", "lapsed", None),
        ("gained", "slot", None), ("lost", "taken_over", OLDER)]


def test_the_crown_log_keeps_its_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    monkeypatch.setattr(crownlog, "LOG_LINES", 10)
    monkeypatch.setattr(crownlog, "LOG_KEEP", 4)
    for _ in range(11):
        crownlog.note("gained", "take")
    assert len(_crown_log(tmp_path)) == 4
    crownlog.note("lost", "no-such-way")                      # refused, never raised
    assert len(_crown_log(tmp_path)) == 4


async def test_a_holder_whose_push_starts_a_turn_without_a_bus_tool_keeps_the_crown(
        mm, client, tmp_path, monkeypatch):
    """RT-6.6 step 1 (#64): a channel session idle as the holder gets a push; the prompt
    hook's working starts the turn, which calls no tool of the bus and only answers in
    the terminal. The turn takes the push up: no silence, the crown stays, also after
    the turn's end and well past MM_ACK_WAIT_SEC."""
    life.reset()
    _dir(tmp_path).mkdir(exist_ok=True)
    (_dir(tmp_path) / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    me(mm, "ear")
    assert await lease.command(client, "take") == "taken"
    await lease.set_status(client, "idle")
    life.pushed()                                             # the channel tag goes out
    life.turn(True)                                           # the prompt hook: working
    monkeypatch.setattr(life, "_sign", time.time() - 3600)    # a long turn, no bus tool
    await lease.set_status(client, "renew")
    assert holds() and mm.custom_status()["emoji"] == "crown"
    life.turn(False)                                          # the Stop hook: idle
    monkeypatch.setattr(life, "_sign", time.time() - 3600)
    await lease.set_status(client, "renew")
    assert holds() and not life.silent_since(listening=True)
    life.reset()


async def test_the_heartbeat_logs_a_crown_that_lapsed(monkeypatch, tmp_path):
    """RT-6.6 step 11: the heartbeat sees the crown end with no rule's line: `lapsed`."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    crownlog.reset()
    crownlog.note("gained", "election")
    calls: list[str] = []
    holding = [True]

    def until(c):
        if c.count("renew") >= 2:
            holding[0] = False
        return c.count("renew") >= 4

    await _run_heartbeat(monkeypatch, calls, until, holding=holding)
    assert [(ln["event"], ln["way"]) for ln in _crown_log(tmp_path)] == [
        ("gained", "election"), ("lost", "lapsed")]


async def test_a_take_that_waits_is_checked_before_its_mark_runs_out(monkeypatch):
    """RT-6.6 step 4 (#64): a take that comes between ticks brings the next one forward,
    and while it waits the heartbeat checks it every PENDING_POLL_SEC, which renews its
    mark (TAKE_MARK_SEC, two checks) before it runs out."""
    assert lease.TAKE_MARK_SEC >= 2 * lease.PENDING_POLL_SEC
    calls: list[str] = []
    pending = [False]
    monkeypatch.setattr(lease, "take_pending", lambda uid: pending[0])
    monkeypatch.setattr(lease, "PENDING_POLL_SEC", 0.05)

    def until(c):
        if c.count("tick") == 1:
            pending[0] = True                                 # the take, after the first tick
        return c.count("tick") >= 3

    await _run_heartbeat(monkeypatch, calls, until, holding=[False])
    assert calls.count("tick") >= 3                           # not one tick per heartbeat_sec()
