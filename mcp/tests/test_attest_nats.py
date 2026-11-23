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
         exp: Any = int(T0) + 900, **over: Any) -> bytes:
    """A record; a parameter given as None is left out."""
    rec: dict[str, Any] = {
        "v": 1, "agent": agent,
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
