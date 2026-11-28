"""The MCP server reads the login service's attestations (RT-6.41): the hello's trusted names,
the strict parse of `<presence>.attest.<agent>.v1`, freshness by the server's clock, the
owner's name behind an anchor, and peers and the crown compared by anchors, not names.

Unit tests on the fakes of test_natsbus.py, test_nats_port.py and test_crown_nats.py.
"""

import json
import logging
from typing import Any

import pytest
from fakes import BOT
from test_crown_nats import HUMAN, NAME_A, NAME_B, NAME_ME, World, claimant
from test_natsbus import T0, FakeMsg, _presence, _presence_reply, _unit_bus

from mattermost_ai_agent_bus import compose, owners
from mattermost_ai_agent_bus.natsbus import Attestation, MessageError, NatsSettings, build_hello, decode_attestation

AGENT = "agent-them"


def body(agent: str = AGENT, owner: tuple[str, str] | None = ("u1", "alice"),
         trusted: tuple[tuple[str, str], ...] | None = (("u1", "alice"),), cut: Any = False,
         exp: Any = int(T0) + 900, home: Any = None, **over: Any) -> bytes:
    """A record; a parameter given as None is left out."""
    rec: dict[str, Any] = {
        "v": 1, "agent": agent, "home": home,
        "owner": None if owner is None else {"anchor": owner[0], "name": owner[1]},
        "trusted": None if trusted is None else [{"anchor": a, "name": n} for a, n in trusted],
        "cut": cut, "iat": int(T0), "exp": exp}
    rec.update(over)
    return json.dumps({k: v for k, v in rec.items() if v is not None}).encode()


async def feed(bus: Any, data: bytes, seq: int = 1, t: float = T0, agent: str = AGENT) -> None:
    await bus._on_presence_msg(FakeMsg(f"presence.attest.{agent}.v1", _presence_reply(bus, seq, seq, t), data))


async def presence_of_owner(bus: Any, token: str, seq: int = 1, t: float = T0) -> None:
    from mattermost_ai_agent_bus.natsbus import encode_presence
    await bus._on_presence_msg(FakeMsg(f"presence.{token}.{AGENT}", _presence_reply(bus, seq, seq, t),
                                       encode_presence("online")))


# --- the hello --------------------------------------------------------------------------------------

def test_the_hello_carries_the_normalised_trusted_names() -> None:
    s = NatsSettings(servers=("nats://127.0.0.1:1",), home="home",
                     trusted=("@Alice", "bob", "alice", "Bad Name", "", "-x", "@carol.d-e_f"))
    assert s.trusted == ("alice", "bob", "carol.d-e_f")
    hello = json.loads(build_hello("T", "home", (), False, {}, trusted=s.trusted))
    assert hello == {"token": "T", "home": "home", "trusted": ["alice", "bob", "carol.d-e_f"]}
    many = NatsSettings(servers=("nats://127.0.0.1:1",), home="home", trusted=tuple(f"u{i}" for i in range(40)))
    assert len(many.trusted) == 32 and many.trusted[0] == "u0"
    assert "trusted" not in json.loads(build_hello("T", "home", (), False, {}))


def test_the_session_settings_take_the_trusted_names_from_the_environment() -> None:
    env = {"MM_NATS_URL": "nats://127.0.0.1:1"}
    assert compose.nats_settings_from_env("home", environ=env | {"MM_TRUSTED_HUMANS": "@Alice, bob"}
                                          ).trusted == ("alice", "bob")  # type: ignore[union-attr]
    assert compose.nats_settings_from_env("home", environ=env | {"MM_OWNER": "carol"}
                                          ).trusted == ("carol",)  # type: ignore[union-attr]
    assert compose.nats_settings_from_env("home", environ=env).trusted == ()  # type: ignore[union-attr]


async def test_the_bus_sends_its_trusted_names_in_the_hello() -> None:
    bus, _ = _unit_bus(trusted=("@Alice",))
    assert bus._s.trusted == ("alice",)


# --- parsing ----------------------------------------------------------------------------------------

async def test_a_valid_attestation_is_kept_and_makes_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    bus, _ = _unit_bus()
    with caplog.at_level(logging.WARNING):
        await feed(bus, body(trusted=(("u1", "alice"), ("u2", "bob"))))
    assert not caplog.records
    att = bus.attestation_of(AGENT)
    assert att == Attestation("u1", "alice", (("u1", "alice"), ("u2", "bob")), False, float(int(T0) + 900))
    assert bus.attestation_of("agent-other") is None


@pytest.mark.parametrize("bad", [
    body(v=2), body(agent="agent-someone-else"), body(owner=("Bad Anchor", "alice")),
    body(owner=("u1", "Not A Name")), body(trusted=tuple((f"u{i}", f"n{i}") for i in range(33))),
    body(owner=None), body(trusted=None), body(cut=None), body(exp=None), body(iat=None),
    body(cut="no"), body(exp="soon"), body(exp=True), body(trusted=(("U1", "alice"),)),
    body(home=1), body(home="Home"), body(home="a.b"), body(home=[]), body(home="x" * 65),
    b"not json", b"[]",
])
async def test_a_malformed_attestation_is_dropped_with_a_warning(bad: bytes, caplog: pytest.LogCaptureFixture) -> None:
    bus, _ = _unit_bus()
    with caplog.at_level(logging.WARNING):
        await feed(bus, bad)
    assert bus.attestation_of(AGENT) is None
    assert any("malformed" in r.getMessage() for r in caplog.records)
    with pytest.raises(MessageError):
        decode_attestation(bad, AGENT)


async def test_the_home_is_read_only_with_the_anchors_owner() -> None:
    """RT-6.41, #255: the record's home is taken only from the record the presence backs. None
    means no word (an older service, a record still to come); '' means the service says the agent
    has no home - so a reader falls back to the claim only on None, never on ''."""
    bus, _ = _unit_bus()
    await presence_of_owner(bus, "u1")
    assert bus.home_of(AGENT) is None  # no record yet
    await feed(bus, body(home="home"))
    assert bus.home_of(AGENT) == "home"
    await feed(bus, body(home=""), seq=2, t=T0 + 1)
    assert bus.home_of(AGENT) == ""  # stated: no home, and no claim is consulted
    # the record names another anchor than the presence subject: no word at all
    await presence_of_owner(bus, "u2", seq=2, t=T0 + 1)
    assert bus.home_of(AGENT) is None


async def test_an_older_attestation_never_overrides_a_newer_one() -> None:
    bus, _ = _unit_bus()
    await feed(bus, body(owner=("u1", "new"), trusted=()), seq=5, t=T0 + 10)
    await feed(bus, body(owner=("u2", "old"), trusted=()), seq=9, t=T0 + 5)  # a lower time, a higher seq
    await feed(bus, body(owner=("u3", "older"), trusted=()), seq=1, t=T0 + 10)  # same time, lower seq
    att = bus.attestation_of(AGENT)
    assert att is not None and att.owner_anchor == "u1"
    await feed(bus, body(owner=("u4", "newest"), trusted=()), seq=6, t=T0 + 10)
    att = bus.attestation_of(AGENT)
    assert att is not None and att.owner_anchor == "u4"


async def test_an_attestation_past_its_expiry_by_the_server_clock_is_ignored(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: 0.0)  # the host's clock is not what counts
    bus, _ = _unit_bus()
    await feed(bus, body(exp=int(T0) + 100), t=T0)
    assert bus.attestation_of(AGENT) is not None
    await _presence(bus, "agent-z", 2, T0 + 99)
    assert bus.attestation_of(AGENT) is not None
    await _presence(bus, "agent-z", 3, T0 + 100)  # the server's now reaches exp
    assert bus.attestation_of(AGENT) is None


# --- the owner's name ---------------------------------------------------------------------------------

async def test_owner_of_returns_the_attested_name_only_when_the_anchor_matches_the_presence() -> None:
    bus, _ = _unit_bus()
    await presence_of_owner(bus, "u1")
    assert bus.owner_of(AGENT) == "u1"  # before any attestation: the presence owner, as today
    await feed(bus, body(owner=("u1", "alice")), seq=2)
    assert bus.owner_of(AGENT) == "alice"
    await feed(bus, body(owner=("u7", "mallory")), seq=3, t=T0 + 1)  # another anchor than the presence pins
    assert bus.owner_of(AGENT) == "u1"
    await feed(bus, body(owner=("u1", "alice2")), seq=4, t=T0 + 2)
    assert bus.owner_of(AGENT) == "alice2"
    await _presence(bus, "agent-z", 5, T0 + 2000)  # expired by the server clock
    assert bus.owner_of(AGENT) == "u1"


# --- peers by anchors ----------------------------------------------------------------------------------

def attested(fake: Any, agent: str, owner: tuple[str, str], trusted: tuple[tuple[str, str], ...],
             cut: bool = False, exp: float | None = None) -> None:
    fake.owners[agent] = owner[0]
    fake.attestations[agent] = Attestation(owner[0], owner[1], trusted, cut,
                                           fake.server_now + 600 if exp is None else exp)


@pytest.fixture
async def w() -> World:
    world = World()
    world.inner.trusted = {HUMAN: "alice"}
    world.inner.descriptions = {BOT: "for @alice · trusts @alice"}
    await world.prime()
    return world


ALICE = (("u1", "alice"),)


async def foreign(w: World, *names: str) -> dict[str, str]:
    return await owners.foreign(w.port, names)  # type: ignore[arg-type]


async def test_a_sender_attested_with_my_anchors_is_a_peer(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, ("u1", "alice"), ALICE)
    assert await foreign(w, NAME_A) == {}
    assert await owners.peer(w.port, NAME_A)  # type: ignore[arg-type]
    agent = await owners.read(w.port, NAME_A)  # type: ignore[arg-type]
    assert agent is not None and agent.by_anchor and agent.owner == "u1" and agent.trusts == {"u1"}


async def test_without_my_own_attestation_a_sender_attested_by_anchor_is_foreign(w: World) -> None:
    attested(w.fake, NAME_A, ("u1", "alice"), ALICE)
    assert await foreign(w, NAME_A) == {NAME_A: "alice"}
    assert not await owners.peer(w.port, NAME_A)  # type: ignore[arg-type]


async def test_a_cut_list_is_foreign(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, ("u1", "alice"), ALICE, cut=True)
    assert NAME_A in await foreign(w, NAME_A)


async def test_the_same_name_with_another_anchor_is_foreign(w: World) -> None:
    """A later holder of the name `alice` (a new anchor) inherits nothing."""
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, ("u2", "alice"), (("u2", "alice"),))
    assert await foreign(w, NAME_A) == {NAME_A: "alice"}


async def test_a_circle_beyond_mine_is_foreign(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, ("u1", "alice"), (("u1", "alice"), ("u9", "eve")))
    assert NAME_A in await foreign(w, NAME_A)


async def test_an_attestation_for_another_anchor_than_the_presence_is_not_used(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, ("u1", "alice"), ALICE)
    w.fake.owners[NAME_A] = "u5"  # the presence pins another owner
    assert await w.port.attested_circle(NAME_A) is None
    assert NAME_A in await foreign(w, NAME_A)  # the name-less presence owner "u5" matches no name
    w.fake.owners[NAME_A] = "u1"
    w.fake.attestations[NAME_A] = Attestation("u1", "alice", ALICE, False, w.fake.server_now - 1)  # expired
    assert await w.port.attested_circle(NAME_A) is None


async def test_a_sender_without_attestation_keeps_the_presence_owner_by_name(w: World) -> None:
    w.fake.owners[NAME_A] = "alice"  # before the import: anchor == name
    assert await foreign(w, NAME_A) == {}
    agent = await owners.read(w.port, NAME_A)  # type: ignore[arg-type]
    assert agent is not None and not agent.by_anchor and agent.owner == "alice" and not agent.trusts
    w.fake.owners[NAME_A] = "u1"  # after the import: a sub that matches no name
    assert await foreign(w, NAME_A) == {NAME_A: "u1"}  # no attestation, no name to show


async def test_the_mattermost_adapter_has_no_circle() -> None:
    from fakes import FakeBus, FakeRegistrar
    inner = FakeBus(FakeRegistrar())
    assert await inner.attested_circle(NAME_A) is None and await inner.own_anchors() is None


# --- the crown ----------------------------------------------------------------------------------------

async def test_the_crown_over_nats_stands_for_peers_by_anchor_and_not_for_a_foreign_anchor(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_B, ("u1", "alice"), ALICE)
    claimant(w.fake, NAME_ME, 30, owner="u1")
    claimant(w.fake, NAME_B, 20, owner="u1")
    view = w.view()
    assert view is not None and view.holder == NAME_B and view.candidates == (NAME_B, NAME_ME)
    # an older claimant of another anchor, though it is called alice: nobody stands
    attested(w.fake, NAME_A, ("u2", "alice"), (("u2", "alice"),))
    claimant(w.fake, NAME_A, 10, owner="u2")
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()


async def test_the_crown_over_nats_without_my_own_attestation_does_not_stand_for_attested_peers(w: World) -> None:
    attested(w.fake, NAME_B, ("u1", "alice"), ALICE)
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_B, 20, owner="u1")
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()


async def test_a_foreign_sender_is_shown_by_name_not_by_its_anchor(w: World) -> None:
    """After the import an anchor may be a sub: what people read is the name."""
    sub = "0b6f3c1e-1111-4222-8333-444455556666"
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    attested(w.fake, NAME_A, (sub, "carol"), ((sub, "carol"),))
    assert await foreign(w, NAME_A) == {NAME_A: "carol"}
    agent = await owners.read(w.port, NAME_A)  # type: ignore[arg-type]
    assert agent is not None and agent.owner == sub and agent.display() == "carol"


# --- no fallback to names once this session has its own record (RT-6.41) ---------------------------------

async def _own_record_world(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)


@pytest.mark.parametrize("case", ["none", "expired", "another anchor"])
async def test_with_my_own_record_a_sender_without_a_usable_one_has_no_owner_and_does_not_stand(
        w: World, case: str) -> None:
    """The former `alice` keeps the anchor `alice`; a new person is now named so and I trust her:
    its agent must not pass by that name when its record is missing, expired or mismatched."""
    await _own_record_world(w)
    w.fake.owners[NAME_B] = "alice"  # the presence owner equals my trusted NAME
    if case == "expired":
        w.fake.attestations[NAME_B] = Attestation("alice", "alice", ALICE, False, w.fake.server_now - 1)
    elif case == "another anchor":
        w.fake.attestations[NAME_B] = Attestation("u9", "alice", (("u9", "alice"),), False,
                                                  w.fake.server_now + 600)
    assert await w.port.agent_owner(NAME_B) == ""
    assert NAME_B in await foreign(w, NAME_B)
    assert not await owners.peer(w.port, NAME_B)  # type: ignore[arg-type]
    claimant(w.fake, NAME_ME, 30, owner="u1")
    claimant(w.fake, NAME_B, 20, owner="alice")  # older: it would hold the crown by name
    view = w.view()
    assert view is not None and NAME_B not in view.candidates and view.holder == NAME_ME


async def test_a_record_for_another_anchor_is_foreign_even_without_my_own_record(w: World) -> None:
    """A record exists (records are on), but for another anchor than the presence's: the
    sender has no usable one and is foreign, never compared by name (ADR-016)."""
    w.fake.owners[NAME_B] = "alice"
    w.fake.attestations[NAME_B] = Attestation("u9", "alice", (("u9", "alice"),), False,
                                              w.fake.server_now + 600)  # not the presence's anchor
    assert await w.port.agent_owner(NAME_B) == ""
    assert await foreign(w, NAME_B) == {NAME_B: ""}
    claimant(w.fake, NAME_ME, 30, owner="alice")
    claimant(w.fake, NAME_B, 20, owner="alice")
    view = w.view()
    assert view is not None and NAME_B not in view.candidates


# --- the cut list of the host -----------------------------------------------------------------------

def test_the_hello_says_when_the_host_cut_its_trusted_list() -> None:
    from mattermost_ai_agent_bus.natsbus import normalise_trusted
    assert normalise_trusted(["@Alice", "alice", "", "bob"]) == (("alice", "bob"), False)
    assert normalise_trusted(["alice", "Bad Name"]) == (("alice",), True)
    assert normalise_trusted([f"u{i}" for i in range(33)])[1] is True
    assert normalise_trusted([f"u{i}" for i in range(32)])[1] is False
    bad = NatsSettings(servers=("nats://127.0.0.1:1",), home="home", trusted=("alice", "Bad Name"))
    assert bad.trusted == ("alice",) and bad.trusted_cut
    hello = json.loads(build_hello("T", "home", (), False, {}, trusted=bad.trusted, trusted_cut=bad.trusted_cut))
    assert hello == {"token": "T", "home": "home", "trusted": ["alice"], "trusted_cut": True}
    many = NatsSettings(servers=("nats://127.0.0.1:1",), home="home", trusted=tuple(f"u{i}" for i in range(40)))
    assert many.trusted_cut
    clean = NatsSettings(servers=("nats://127.0.0.1:1",), home="home", trusted=("alice",))
    assert not clean.trusted_cut
    assert "trusted_cut" not in json.loads(build_hello("T", "home", (), False, {}, trusted=("alice",)))


async def test_the_bus_puts_the_cut_into_the_hello_it_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    import mattermost_ai_agent_bus.natsbus as nb
    seen: list[bool] = []
    real = nb.build_hello

    def spy(*a: Any, **kw: Any) -> str:
        seen.append(bool(kw.get("trusted_cut")))
        return real(*a, **kw)

    monkeypatch.setattr(nb, "build_hello", spy)
    bus, _ = _unit_bus(trusted=("alice", "Bad Name"))
    try:
        await bus._open()
    except Exception:  # noqa: BLE001 — no server: only the hello's building counts
        pass
    assert seen == [True]


# --- names, never anchors, for people -----------------------------------------------------------------

SUB1, SUB2 = "0b6f3c1e-1111-4222-8333-444455556666", "0b6f3c1e-2222-4222-8333-444455556666"


async def test_a_circle_by_anchor_is_shown_by_names(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from mattermost_ai_agent_bus import debug, gate, server
    await _own_record_world(w)
    attested(w.fake, NAME_A, (SUB1, "alice"), ((SUB1, "alice"), (SUB2, "bob")))
    agent = await owners.read(w.port, NAME_A)  # type: ignore[arg-type]
    assert agent is not None and agent.by_anchor and agent.trusts == {SUB1, SUB2}
    assert agent.display_trusts() == {"alice", "bob"} and agent.display_circle() == {"alice", "bob"}
    row = type("Row", (), {"user_id": NAME_A, "username": "agent-a"})()
    out = await server._owners_of(w.port, [row])  # type: ignore[arg-type]
    assert out["agent-a"]["owner"] == "alice" and out["agent-a"]["trusts"] == ["alice", "bob"]
    said = await debug._identity(w.port, {"from_agent": True}, NAME_A)  # type: ignore[arg-type]
    assert "owner alice from its attestation" in said and SUB1 not in said
    # the "Not woken" answer compares the author's NAME with the names, not the anchors
    async def roster(_c: Any) -> list[Any]:
        return [types.SimpleNamespace(user_id=NAME_A, username="agent-a")]

    monkeypatch.setattr(gate.lease, "roster", roster)
    post = types.SimpleNamespace(message="@agent-a hi", from_webhook=False)
    assert await gate.mentioned_agent_trusts(w.port, post, "Bob", "me")  # type: ignore[arg-type]
    assert not await gate.mentioned_agent_trusts(w.port, post, SUB2, "me")  # type: ignore[arg-type]


# --- what the mutations left alive --------------------------------------------------------------------

async def test_a_record_on_another_version_is_ignored_and_warned(caplog: pytest.LogCaptureFixture) -> None:
    bus, _ = _unit_bus()
    with caplog.at_level(logging.WARNING):
        await bus._on_presence_msg(FakeMsg(f"presence.attest.{AGENT}.v2", _presence_reply(bus, 1, 1, T0), body()))
    assert bus.attestation_of(AGENT) is None
    assert any("unexpected subject" in r.getMessage() for r in caplog.records)


async def test_my_own_anchors_are_my_trusted_entries_only_not_my_owner(w: World) -> None:
    """As `owners.mine` has the trusted names only: the owner's anchor is not in the set."""
    attested(w.fake, NAME_ME, ("u1", "alice"), (("u2", "bob"),))
    assert await w.port.own_anchors() == {"u2"}
    attested(w.fake, NAME_A, ("u1", "alice"), (("u2", "bob"),))
    assert NAME_A in await foreign(w, NAME_A)  # its owner u1 is not among my anchors
    attested(w.fake, NAME_B, ("u2", "bob"), (("u2", "bob"),))
    assert await foreign(w, NAME_B) == {}


async def test_a_record_older_than_the_streams_max_age_is_pruned() -> None:
    from mattermost_ai_agent_bus.natsbus import MAX_AGE_SEC
    bus, _ = _unit_bus()
    far = int(T0) + 10 * 86400  # not expired: only the pruning may remove it
    await feed(bus, body(exp=far), seq=1, t=T0)
    await feed(bus, body(agent="agent-mid", exp=far), seq=2, t=T0 + 200, agent="agent-mid")
    await feed(bus, body(agent="agent-new", exp=far), seq=3, t=T0 + MAX_AGE_SEC + 50, agent="agent-new")
    assert AGENT not in bus._attest
    assert "agent-mid" in bus._attest and "agent-new" in bus._attest


# --- records on: no comparison by name again, also after an expiry ---------------------------------

async def test_records_on_latches_and_an_expiry_does_not_turn_it_off() -> None:
    bus, _ = _unit_bus()
    assert not bus.records_on()
    await feed(bus, body(exp=int(T0) + 100), t=T0)
    await _presence(bus, "agent-z", 2, T0 + 200)  # the record expires by the server's clock
    assert bus.attestation_of(AGENT) is None and bus.records_on()


async def test_my_own_record_expired_keeps_an_old_name_holder_foreign(w: World) -> None:
    """The former alice keeps anchor `alice`; I trust the name alice. My own record has
    expired (a failed renewal): the old holder without a record stays foreign and holds no crown."""
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE, exp=w.fake.server_now - 1)  # mine, expired
    w.fake.owners[NAME_A] = "alice"  # the old holder, no record
    assert await foreign(w, NAME_A) == {NAME_A: ""}
    assert not await owners.peer(w.port, NAME_A)  # type: ignore[arg-type]
    claimant(w.fake, NAME_ME, 30, owner="u1")
    claimant(w.fake, NAME_A, 10, owner="alice")
    view = w.view()
    assert view is not None and NAME_A not in view.candidates and view.holder != NAME_A


async def test_before_my_own_record_arrives_another_record_already_ends_the_name_comparison(w: World) -> None:
    """At a start the replay brings others' records before mine: a sender without one is foreign."""
    attested(w.fake, NAME_B, ("u1", "alice"), ALICE)  # someone else's record
    w.fake.owners[NAME_A] = "alice"
    assert await foreign(w, NAME_A) == {NAME_A: ""}


async def test_a_lone_claimant_without_a_record_does_not_show_as_the_holder(w: World) -> None:
    attested(w.fake, NAME_ME, ("u1", "alice"), ALICE)
    w.fake.claims.clear()
    claimant(w.fake, NAME_A, 10, owner="alice")  # I am no claimant
    view = w.view()
    assert view is not None and view.holder == "" and view.candidates == ()


async def test_without_any_record_the_name_comparison_stays(w: World) -> None:
    w.fake.owners[NAME_A] = "alice"
    assert await foreign(w, NAME_A) == {}


async def test_an_older_record_states_no_home_at_all() -> None:
    """RT-6.41, #255: a record without `home` is no word - `None`, never '' - so a reader of an
    older login service falls back to the crown claim instead of taking every agent for a guest."""
    bus, _ = _unit_bus()
    await presence_of_owner(bus, "u1")
    await feed(bus, body())  # the older format: no "home" key
    assert bus.attestation_of(AGENT) is not None
    assert bus.home_of(AGENT) is None
    # and the dataclass keeps the field optional, so older constructions still hold
    assert Attestation("u1", "alice", (), False, float(int(T0) + 900)).home is None
