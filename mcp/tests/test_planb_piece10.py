"""Plan B piece 10 (#244, ADR-014 phase 5, RT-6.13 step 7): permission prompts by DM to the
responsible human over the bridge.

With `planb.on(10, home)` on, the session sends the prompt to the bridge as a request
(`<req>.permission.<agent>`, op `prompt`), makes no Mattermost call for it, and takes the
verdict only from what the bridge carries on `<bus>.hu.dm.<agent>.<anchor>`: from the very
person the prompt went to, one of the session's attested trusted anchors who approves, within
the prompt's 24 h. Without a bridge whose fresh signal names piece 10 the prompt stays in the
terminal. With the switch off nothing changes (test_permission.py). Against a real nats-server
(MM_TEST_NATS=1) the request goes out and a person's answer comes back on the DM consumer.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from test_nats_port import ME
from test_natsbus import Rig, _api, _consumer_specs, _reply, _unit_bus, until
from test_planb_piece6 import _att
from test_planb_piece7 import Carrier, _world

from mattermost_ai_agent_bus import channel, compose, lease, natsbus, permission, planb, requests, server
from mattermost_ai_agent_bus.nats_port import NatsPort, NatsPortError
from mattermost_ai_agent_bus.natsbus import Attestation, NatsSettings, PersonMessage

REQUEST = {"request_id": "abcde", "tool_name": "Bash",
           "description": "Comment on PROJ-1", "input_preview": '{"command": "jira comment PROJ-1"}'}
PROMPT = "p" * 26  # the prompt post the bridge made
ANSWER = "r" * 26  # the person's answer post


class _Conn:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))


class World:
    def __init__(self, port: NatsPort, inner: Any, fake: Carrier, conn: _Conn) -> None:
        self.port, self.inner, self.fake, self.conn = port, inner, fake, conn
        self.requests: list[dict[str, Any]] = []
        self.notes: list[tuple[str, str, str]] = []
        self.refuse = ""

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["op"] == op]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    port, inner, fake = _world(monkeypatch, on="10")
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN", raising=False)
    monkeypatch.delenv("CB_RESPONSIBLE_HUMAN", raising=False)
    monkeypatch.setattr(server, "_relay", permission.Relay())
    pusher = channel.Pusher()
    conn = _Conn()
    pusher.connection = conn  # type: ignore[assignment]
    monkeypatch.setattr(server, "_pusher", pusher)
    w = World(port, inner, fake, conn)

    async def request(bus: Any, kind: str, payload: dict[str, Any] | None = None, **kw: Any) -> Any:
        assert kind == "permission" and bus is fake
        w.requests.append(dict(payload or {}))
        if w.refuse:
            raise requests.RequestError(w.refuse)
        return {"v": 1, "post": PROMPT} if payload and payload["op"] == "prompt" else {"v": 1, "posted": True}

    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(permission, "note", lambda step, rid, tool="", reason="": w.notes.append((step, rid, reason)))
    server.wire_nats(port)
    return w


def _verdicts(w: World) -> list[dict[str, Any]]:
    return [p for m, p in w.conn.sent if m == permission.VERDICT]


async def _prompted(w: World) -> None:
    await server._permission_request(REQUEST)
    assert server._relay.get("abcde") is not None


# --- the switch and the prompt ------------------------------------------------------------------

def test_the_switch_names_piece_10(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    assert planb.on(10, "home") is False
    monkeypatch.setenv(planb.TEST_ENV, "10")
    assert planb.on(10, "home") is True
    assert "permission" in requests.KINDS


async def test_off_the_prompt_takes_the_mattermost_path_and_the_lane_reads_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: with piece 10 off the session's port says so, and a person's DM the
    bridge might carry reaches no relay."""
    port, _inner, _fake = _world(monkeypatch, on="")
    assert port.piece10() is False
    got: list[Any] = []

    async def keep(anchor: str, m: PersonMessage) -> None:
        got.append((anchor, m))
    port.on_permission = keep
    await port._on_person_dm(natsbus.PersonEvent(natsbus.DM_SINCE, "dembaca", 1, time.time(),
                                                 PersonMessage("post", ANSWER, "dembaca", text="yes abcde")))
    assert got == []


async def test_on_the_prompt_goes_to_the_bridge_and_mattermost_is_not_called(world: World) -> None:
    """RT-6.13 step 7: one request, op prompt, to the responsible human's anchor and attested
    name, the prompt text as on Mattermost; the open prompt remembers whom it went to."""
    await _prompted(world)
    (req,) = world.ops("prompt")
    assert req == {"op": "prompt", "channel": "home", "id": "abcde", "to": "dembaca", "name": "dembaca",
                   "text": permission.prompt(REQUEST)}
    pending = server._relay.get("abcde")
    assert pending is not None and pending.post_id == PROMPT and pending.to == "dembaca"
    assert world.inner.mm_calls == []
    assert ("dm posted", "abcde", "over the bridge") in world.notes


async def test_on_without_the_bridges_signal_for_piece_10_the_prompt_stays_in_the_terminal(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: no fresh signal naming piece 10, nothing goes out, never Mattermost; bus_info
    says so (lease.nats_notes)."""
    world.fake.pieces = frozenset({7})
    await server._permission_request(REQUEST)
    assert world.requests == [] and world.inner.mm_calls == [] and server._relay.get("abcde") is None
    assert ("not relayed", "abcde", "no bridge for piece 10") in world.notes
    assert world.port.permission_unrelayed() is True
    monkeypatch.setattr(lease, "_nats_source", world.port)
    monkeypatch.setattr(lease, "_nats_home", "home")
    monkeypatch.setattr(lease, "nats_state", lambda: lease.NatsState("ok", None, ""))
    assert any("plan B piece 10" in n and "terminal" in n for n in lease.nats_notes())


async def test_on_a_responsible_human_who_is_not_attested_keeps_the_terminal(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_RESPONSIBLE_HUMAN", "someone-else")
    await server._permission_request(REQUEST)
    assert world.requests == [] and world.inner.mm_calls == []
    assert ("not relayed", "abcde", "responsible human not attested") in world.notes


async def test_on_without_a_fresh_own_attestation_nothing_goes_out(world: World) -> None:
    world.fake.attestations.clear()
    await server._permission_request(REQUEST)
    assert world.requests == [] and ("not relayed", "abcde", "responsible human not attested") in world.notes


async def test_on_the_named_responsible_human_is_asked(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.fake.attestations[ME] = Attestation(
        owner_anchor="dembaca", owner_name="dembaca", trusted=(("dembaca", "dembaca"), ("kim.x", "kim")),
        cut=False, exp=1e12)
    monkeypatch.setenv("CB_RESPONSIBLE_HUMAN", "@Kim")
    await server._permission_request(REQUEST)
    assert [(r["to"], r["name"]) for r in world.ops("prompt")] == [("kim.x", "kim")]


async def test_on_a_refused_prompt_is_logged_and_the_terminal_stays(world: World) -> None:
    world.refuse = "no single Mattermost account is bound to that person"
    await server._permission_request(REQUEST)
    assert server._relay.get("abcde") is None and world.inner.mm_calls == []
    assert ("posting failed", "abcde", "RequestError") in world.notes


# --- the verdict --------------------------------------------------------------------------------

async def test_on_a_yes_from_the_responsible_human_is_the_verdict(world: World) -> None:
    """RT-6.13 step 7: the verdict goes to Claude Code, the line under the prompt to the bridge."""
    await _prompted(world)
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="Yes abcde"))
    assert _verdicts(world) == [{"request_id": "abcde", "behavior": "allow"}]
    (note,) = world.ops("note")
    assert note["id"] == "abcde" and note["close"] is True and "allowed by dembaca" in note["text"]
    assert server._relay.get("abcde") is None and world.inner.mm_calls == []


@pytest.mark.parametrize("emoji,behavior", [("+1", "allow"), ("white_check_mark", "allow"),
                                            ("-1", "deny"), ("x", "deny")])
async def test_on_a_reaction_on_the_prompt_is_the_verdict(world: World, emoji: str, behavior: str) -> None:
    await _prompted(world)
    await server._permission_answer("dembaca", PersonMessage("reaction", PROMPT, "dembaca", emoji=emoji))
    assert _verdicts(world) == [{"request_id": "abcde", "behavior": behavior}]


async def test_on_another_emoji_or_an_unreaction_approves_nothing(world: World) -> None:
    await _prompted(world)
    await server._permission_answer("dembaca", PersonMessage("reaction", PROMPT, "dembaca", emoji="eyes"))
    await server._permission_answer("dembaca", PersonMessage("unreaction", PROMPT, "dembaca", emoji="+1"))
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="sure, go"))
    assert _verdicts(world) == [] and server._relay.get("abcde") is not None


@pytest.mark.parametrize("anchor", ["kim.x", natsbus.UNBOUND, "mallory"])
async def test_on_anyone_but_the_responsible_human_approves_nothing(world: World, anchor: str) -> None:
    """RT-6.13 step 7: another trusted human, an unbound account and a stranger: no verdict."""
    world.fake.attestations[ME] = Attestation(
        owner_anchor="dembaca", owner_name="dembaca", trusted=(("dembaca", "dembaca"), ("kim.x", "kim")),
        cut=False, exp=1e12)
    world.fake.approvers.add("kim.x")
    await _prompted(world)
    await server._permission_answer(anchor, PersonMessage("post", ANSWER, "x", text="yes abcde"))
    assert _verdicts(world) == [] and server._relay.get("abcde") is not None
    assert ("verdict refused", "abcde", "not the responsible human") in world.notes


async def test_on_a_responsible_human_who_no_longer_approves_approves_nothing(world: World) -> None:
    """RT-6.42 step 9's rule with piece 4 off: a fresh approver record; with the attestation gone,
    nobody (fail closed)."""
    await _prompted(world)
    world.fake.approvers.clear()
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde"))
    world.fake.approvers.add("dembaca")
    world.fake.attestations.clear()
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde"))
    assert _verdicts(world) == []
    assert world.notes.count(("verdict refused", "abcde", "not a trusted approver")) == 2


async def test_on_with_piece_4_the_attested_anchor_alone_approves(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(planb.TEST_ENV, "4,10")
    world.fake.approvers.clear()
    await _prompted(world)
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="no abcde"))
    assert _verdicts(world) == [{"request_id": "abcde", "behavior": "deny"}]


async def test_on_an_id_that_is_not_open_gets_the_line_and_no_verdict(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: never sent, answered, or more than 24 h old."""
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes fghij"))
    await _prompted(world)
    real = time.time
    monkeypatch.setattr(permission.time, "time", lambda: real() + permission.MAX_AGE_SEC + 1)
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde"))
    assert _verdicts(world) == []
    assert [n["id"] for n in world.ops("note")] == ["fghij", "abcde"]
    assert all("No open permission prompt" in n["text"] for n in world.ops("note"))


async def test_on_a_yes_read_on_mattermost_never_answers_a_prompt_the_bridge_dmed(world: World) -> None:
    """RT-6.13 step 7: only the lane carries its verdict, from the person it went to."""
    await _prompted(world)
    item = {"kind": "dm", "from_trusted_human": True, "trusted_human": "dembaca",
            "post": {"id": ANSWER, "author": "dembaca", "message": "yes abcde"}}
    assert await server._push_intercept(item) is False
    assert await server._push_intercept({"kind": "reaction", "from_trusted_human": True, "ask": PROMPT,
                                         "answer": "yes", "post": {"id": PROMPT}}) is False
    assert _verdicts(world) == [] and server._relay.get("abcde") is not None


async def test_on_the_turns_end_closes_the_prompt_through_the_bridge(world: World) -> None:
    """RT-6.13 step 5 with piece 10: the line goes to the bridge, Mattermost is not called."""
    await _prompted(world)
    await server.permissions_obsolete(world.port)
    (note,) = world.ops("note")
    assert note["close"] is True and "Answered in the terminal" in note["text"]
    assert server._relay.get("abcde") is None and world.inner.mm_calls == []


async def test_on_a_failed_line_does_not_undo_the_verdict(world: World) -> None:
    await _prompted(world)
    world.refuse = "no open prompt"
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde"))
    assert _verdicts(world) == [{"request_id": "abcde", "behavior": "allow"}]


async def test_the_port_hands_the_lane_to_the_relay_while_the_switch_is_on(world: World) -> None:
    got: list[Any] = []

    async def keep(anchor: str, m: PersonMessage) -> None:
        got.append((anchor, m.text))
    world.port.on_permission = keep
    await world.port._on_person_dm(natsbus.PersonEvent(natsbus.DM_SINCE, "dembaca", 1, time.time(),
                                                       PersonMessage("post", ANSWER, "dembaca", text="yes abcde")))
    assert got == [("dembaca", "yes abcde")]


def test_the_compose_helper_finds_the_port(world: World) -> None:
    assert compose.nats_port_of(world.port) is world.port


# --- the bus: the lane on the DM consumer ------------------------------------------------------

async def test_unit_a_persons_dm_on_the_dm_consumer_reaches_the_callback_strictly() -> None:
    """RT-6.13 step 7: `<bus>.hu.dm.<name>.<anchor>` on `<name>_dm`, the person the subject's last
    token; another agent's lane, a malformed body and a repeat are dropped; the cursor passes it."""
    bus, events = _unit_bus()
    got: list[natsbus.PersonEvent] = []
    bus.on_person_dm = got.append
    body = json.dumps({"v": 1, "kind": "post", "text": "yes abcde", "name": "dembaca", "mm": ANSWER,
                       "files": 0}).encode()
    await bus._on_deliver("@dm", f"bus.hu.dm.{bus.name}.kim~x", _reply(bus, "@dm", 5, 1), body)
    await bus._on_deliver("@dm", f"bus.hu.dm.{bus.name}.kim~x", _reply(bus, "@dm", 5, 2), body)  # a repeat
    await bus._on_deliver("@dm", "bus.hu.dm.other-agent.kim~x", _reply(bus, "@dm", 6, 3), body)
    await bus._on_deliver("@dm", f"bus.hu.dm.{bus.name}.kim~x", _reply(bus, "@dm", 7, 4),
                          body.replace(b'"files"', b'"props":{},"files"'))
    await bus._on_deliver("@dm", f"bus.hu.dm.{bus.name}.Bad", _reply(bus, "@dm", 8, 5), body)
    assert [(e.anchor, e.message.text, e.stream_seq) for e in got] == [("kim.x", "yes abcde", 5)]
    assert events == []
    assert bus._last_seq["@dm"] == 7


# --- against a real server -------------------------------------------------------------------

async def test_against_a_real_server_the_prompt_goes_out_and_the_answer_comes_back(
        rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: the prompt goes on `req.permission.<agent>`, the bridge answers it, and the
    person's `yes <id>` it publishes on `bus.hu.dm.<agent>.<anchor>` arrives on the agent's DM
    consumer and becomes the verdict; no Mattermost call."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN", raising=False)
    monkeypatch.delenv("CB_RESPONSIBLE_HUMAN", raising=False)
    name = rig.names["alice"]
    for stream, cname, cfg in _consumer_specs(name, ["home"], {}):
        if cname.endswith("_presence"):  # the bridge's signal and the attestation, as manager.go adds them
            cfg = cfg | {"filter_subjects": [*cfg["filter_subjects"], "presence.bridge.home.v1"]}
        await _api(rig.admin, f"$JS.API.CONSUMER.CREATE.{stream}.{cname}", {"stream_name": stream, "config": cfg})
    await rig.admin.publish("presence.planb.home.v1",
                            json.dumps({"v": 1, "channel": "home", "pieces": [4, 10]}).encode())
    await rig.admin.flush()
    from test_planb_piece6 import Recording
    from fakes import FakeRegistrar
    inner = Recording(FakeRegistrar())
    inner.registrar.session = inner.registrar.info
    inner.work_channel_name = "home"
    port = NatsPort(inner, NatsSettings(servers=(rig.url,), home="home"), ("home",),  # type: ignore[arg-type]
                    bus_options={"heartbeat_sec": 0.2})
    monkeypatch.setattr(server, "_get_client", lambda: port)
    monkeypatch.setattr(server, "_relay", permission.Relay())
    pusher = channel.Pusher()
    conn = _Conn()
    pusher.connection = conn  # type: ignore[assignment]
    monkeypatch.setattr(server, "_pusher", pusher)
    server.wire_nats(port)
    asked: list[dict[str, Any]] = []

    async def bridge(msg: Any) -> None:
        body = json.loads(msg.data)
        asked.append(body)
        result = {"v": 1, "post": PROMPT} if body["payload"]["op"] == "prompt" else {"v": 1, "posted": True}
        await rig.admin.publish(f"deliver.{name}.req.{body['request']}", json.dumps(
            {"v": 1, "request": body["request"], "kind": body["kind"], "part": 1, "last": True,
             "result": result}).encode())
    sub = await rig.admin.subscribe(f"req.permission.{name}", cb=bridge)
    await port.start(rig.logins["alice"], owner="alice")
    try:
        await until(lambda: planb.on(10, "home"), what="the plan B record naming piece 10")
        bus = port._bus
        assert bus is not None
        monkeypatch.setattr(bus, "attestation_of", lambda agent: _att())
        await server._permission_request(REQUEST)  # no bridge signal yet: the terminal stays
        assert asked == [] and server._relay.get("abcde") is None
        now = int(time.time())
        await rig.admin.publish("presence.bridge.home.v1", json.dumps(
            {"v": 1, "home": "home", "people": True, "iat": now, "exp": now + 180, "pieces": [10]}).encode())
        await rig.admin.flush()
        await until(lambda: 10 in bus.bridge_pieces("home"), what="the bridge's signal naming piece 10")
        await server._permission_request(REQUEST)
        assert [a["payload"]["op"] for a in asked] == ["prompt"] and server._relay.get("abcde") is not None
        await rig.admin.publish(f"bus.hu.dm.{name}.dembaca", json.dumps(
            {"v": 1, "kind": "post", "text": "yes abcde", "name": "dembaca", "mm": ANSWER, "files": 0}).encode())
        await rig.admin.flush()
        await until(lambda: bool(conn.sent), what="the verdict from the lane")
        assert conn.sent == [(permission.VERDICT, {"request_id": "abcde", "behavior": "allow"})]
        await until(lambda: len(asked) == 2, what="the line under the prompt")
        assert asked[1]["payload"]["op"] == "note" and asked[1]["payload"]["close"] is True
        assert inner.mm_calls == []
    finally:
        await sub.unsubscribe()
        await port.stop()
    await asyncio.sleep(0)


def test_a_bridge_answer_without_a_post_id_is_refused(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    async def request(bus: Any, kind: str, payload: dict[str, Any] | None = None, **kw: Any) -> Any:
        return {"v": 1, "post": "not-an-id"}
    monkeypatch.setattr(requests, "request", request)
    with pytest.raises(NatsPortError, match="names no Mattermost post"):
        asyncio.run(world.port.permission_prompt("abcde", "dembaca", "dembaca", "text"))


# --- the review's fixes ---------------------------------------------------------------------

async def test_on_a_switch_not_read_yet_keeps_the_terminal(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: before plan B's records are read, piece 10 reads off but is not known off:
    the prompt stays in the terminal, no Mattermost DM."""
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    monkeypatch.setattr(planb, "known", lambda: False)
    await server._permission_request(REQUEST)
    assert world.requests == [] and world.inner.mm_calls == [] and server._relay.get("abcde") is None
    assert ("not relayed", "abcde", "plan B switch not read") in world.notes


async def test_on_a_switch_seen_on_and_then_off_keeps_the_terminal(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.13 step 7: piece 10 read on once in this session, then off (a stale record): terminal."""
    assert world.port.piece10() is True
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    monkeypatch.setattr(planb, "known", lambda: True)
    await server._permission_request(REQUEST)
    assert world.requests == [] and world.inner.mm_calls == []
    assert ("not relayed", "abcde", "plan B switch not read") in world.notes


async def test_off_and_known_off_the_port_takes_the_old_path(monkeypatch: pytest.MonkeyPatch) -> None:
    port, _inner, _fake = _world(monkeypatch, on="")
    monkeypatch.setattr(planb, "known", lambda: True)
    assert port.piece10() is False and port.permission_undecided() is False


async def test_on_a_reply_outside_the_prompts_thread_approves_nothing(world: World) -> None:
    """RT-6.13 step 7: a reply counts only in the prompt's own thread."""
    await _prompted(world)
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde",
                                                             mm_root="z" * 26))
    assert _verdicts(world) == [] and ("verdict refused", "abcde", "not under the prompt") in world.notes
    await server._permission_answer("dembaca", PersonMessage("post", ANSWER, "dembaca", text="yes abcde",
                                                             mm_root=PROMPT))
    assert _verdicts(world) == [{"request_id": "abcde", "behavior": "allow"}]
