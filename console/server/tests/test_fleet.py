"""The fleet reader: subject shapes, ordering by the server's clock,
freshness, the crown rule (RT-6.38) and the wire cards — each fed with
synthetic JetStream ack metadata, exactly the deliveries a live consumer
would make, so no server is needed (the live ones are test_nats_live).
"""
from __future__ import annotations

import json


from mattermost_ai_agent_bus.natsbus import (
    MAX_AGE_SEC, PRESENCE_FRESH_SEC, PresenceCard, encode_presence,
)
from console_server.fleet import Fleet, short_name


def ack(stream: str, consumer: str, sseq: int, cseq: int, ts: float,
        pending: int = 0) -> str:
    return f"$JS.ACK.{stream}.{consumer}.{sseq}.{sseq}.{cseq}.{int(ts * 1e9)}.{pending}"


def presence(agent: str, owner: str = "dembaca", status: str = "online",
             bot: str = "") -> tuple[str, bytes]:
    return f"presence.{owner}.{agent}", encode_presence(status, bot)


def presence_with(agent: str, card: PresenceCard, owner: str = "dembaca") -> tuple[str, bytes]:
    """The presence an MCP server with the card's data sends (RT-6.37)."""
    return f"presence.{owner}.{agent}", encode_presence("online", "", "", card)


def atestation(agent: str, owner: str, trusted: list[tuple[str, str]],
               exp: float) -> tuple[str, bytes]:
    body = {"v": 1, "agent": agent,
            "owner": {"anchor": owner, "name": owner},
            "trusted": [{"anchor": a, "name": n} for a, n in trusted],
            "cut": False, "iat": 1, "exp": int(exp)}
    return f"presence.attest.{agent}.v1", json.dumps(body).encode()


def feed(f: Fleet, *rows: tuple[str, bytes, str]) -> None:
    for subject, data, reply in rows:
        f.on_delivery(subject, data, reply, "PRESENCE", "console")


def test_presence_and_freshness_by_the_servers_clock() -> None:
    f = Fleet()
    subject, data = presence("agent-a", "dembaca", "online")
    assert f.on_delivery(subject, data, ack("PRESENCE", "console", 1, 1, 1000.0),
                         "PRESENCE", "console")
    assert f.state_of("agent-a") == "online"
    # an older message never overrides a newer one
    assert not f.on_delivery(subject, encode_presence("away"),
                              ack("PRESENCE", "console", 1, 1, 900.0),
                              "PRESENCE", "console")
    assert f.state_of("agent-a") == "online"
    # anything without this consumer's JetStream metadata is dropped
    assert not f.on_delivery(subject, encode_presence("away"), None,
                             "PRESENCE", "console")
    # later presence, and the first agent goes stale by the servers clock
    s2, d2 = presence("agent-b", "dembaca", "online")
    f.on_delivery(s2, d2, ack("PRESENCE", "console", 2, 2, 1090.0),
                  "PRESENCE", "console")
    assert f.state_of("agent-a") == "offline"
    assert f.state_of("agent-b") == "online"
    assert abs(f.server_now - 1090.0) < 0.05


def test_bye_is_offline_and_cards_mark_unknown() -> None:
    f = Fleet()
    subject, data = presence("agent-mac-ai-agent-a1b2c3", "dembaca", "bye")
    feed(f, (subject, data, ack("PRESENCE", "console", 1, 1, 1000.0)))
    (s_att, d_att) = atestation("agent-mac-ai-agent-a1b2c3", "dembaca",
                                 [("dembaca", "dembaca")], exp=2000.0)
    feed(f, (s_att, d_att, ack("PRESENCE", "console", 2, 2, 1001.0)))
    s_m, d_m = "presence.member.dev.agent-mac-ai-agent-a1b2c3", b'{"v": 1}'
    feed(f, (s_m, d_m, ack("PRESENCE", "console", 3, 3, 1002.0)))
    cards = f.cards()
    assert len(cards) == 1
    card = cards[0]
    assert card["state"] == "offline"
    assert card["name"] == "agent-mac-ai-agent-a1b2c3"
    assert card["displayName"] == "a1b2c3"
    assert short_name("agent-mac-ai-agent-a1b2c3") == "a1b2c3"
    assert card["for"] == "dembaca"       # from the attestation
    assert card["trusts"] == ["dembaca"]  # from the attestation
    assert card["home"] == "dev"
    # what a NATS home does not carry is unknown, never guessed as zero
    assert card["ctx"] is None
    assert card["model"] == ""
    assert card["openAsks"] is None  # unknown, never 0
    assert card["helloPostId"] == ""


def test_crown_minimum_by_itself() -> None:
    # the rule itself is natsbus's, pinned in its own tests; here only the
    # reading path: a claim cannot crown without presence (NatsBus's
    # holder rule: the lowest fresh claim, presence and not bye)
    f = Fleet()
    feed(f, ("presence.crown.dev.b", b'{"v": 1}',
              ack("PRESENCE", "console", 5, 5, 1000.0)))
    assert f.holder_of("dev") is None
    s_b, d_b = presence("b", "dembaca", "online")
    feed(f, (s_b, d_b, ack("PRESENCE", "console", 6, 6, 1001.0)))
    # claimed and fresh: the crown of the channel, as NatsBus reads the rule
    assert f.holder_of("dev") == "b"
    feed(f, ("presence.member.dev.b", b'{"v": 1}',
              ack("PRESENCE", "console", 7, 7, 1002.0)))
    assert f.holder_of("dev") == "b"
    # the member subject makes the card crown-marked and homes it
    (card,) = f.cards()
    assert card["crown"] is True
    assert card["home"] == "dev"
    graph = f.graph()
    kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
    assert kinds == {"person:dembaca": "person", "dev": "channel", "b": "agent"}
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert ("b", "dev", "coordinates") in edges


def test_attested_owner_names_the_agent() -> None:
    f = Fleet()
    s, d = presence("b", "dembaca", "online")
    feed(f, (s, d, ack("PRESENCE", "console", 1, 1, 1000.0)))
    (card,) = f.cards()
    assert card["for"] == "dembaca"  # the owner token of the presence subject
    s_att, d_att = atestation("b", "somebody-else", [], exp=2000.0)
    feed(f, (s_att, d_att, ack("PRESENCE", "console", 2, 2, 1001.0)))
    (card,) = f.cards()
    assert card["for"] == "somebody-else"  # the newer attestation wins
    # expired: falls back to the presence owner
    s_att2, d_att2 = atestation("b", "third", [], exp=999.0)
    feed(f, (s_att2, d_att2, ack("PRESENCE", "console", 3, 3, 1002.0)))
    (card,) = f.cards()
    assert card["for"] == "dembaca"


def test_a_silent_stream_ages_the_lone_agent_out() -> None:
    """#108's bug: with no further deliveries the console's clock stood
    still, so a killed lone agent stayed online with its crown. The
    wall-clock floor ages presence past PRESENCE_FRESH_SEC on its own:
    offline, and the crown drops with it."""
    f = Fleet()
    subject, data = presence("lone-agent", "dembaca", "online")
    feed(f, (subject, data, ack("PRESENCE", "console", 1, 1, 1000.0)))
    feed(f, ("presence.member.dev.lone-agent", b'{"v": 1}',
              ack("PRESENCE", "console", 2, 2, 1001.0)))
    feed(f, ("presence.crown.dev.lone-agent", b'{"v": 1}',
              ack("PRESENCE", "console", 3, 3, 1002.0)))
    assert f.state_of("lone-agent") == "online"
    assert f.holder_of("dev") == "lone-agent"

    # the stream goes silent, and only the monotonic clock moves:
    # backdate the monotonic stamp so the floor sits far past the
    # freshness window (the test does not really wait 90 s)
    staged = f.last_server_now_wall
    assert staged is not None
    f.last_server_now_wall = (staged[0], staged[1] - 10000)
    assert f.state_of("lone-agent") == "offline"
    assert f.holder_of("dev") is None
    (card,) = f.cards()
    assert card["state"] == "offline" and card["crown"] is False

    # Fable's point: a jump of the host's wall clock changes nothing —
    # the floor counts monotonic time only; the agent stays offline (no
    # revival through a backwards jump) and an online one before the
    # window is what the next delivery says, not the wall
    fresh = Fleet()
    subject, data = presence("alive-agent", "dembaca", "online")
    feed(fresh, (subject, data, ack("PRESENCE", "console", 1, 1, 1000.0)))
    # a monotonic stamp just taken: the floor is the raw stamp, so the
    # agent is fresh no matter what happens to the wall
    assert fresh.state_of("alive-agent") == "online"
    assert abs(fresh.server_now - 1000.0) < 0.05


def test_a_replay_of_old_stamps_does_not_revive_the_aged() -> None:
    """Fable's replay probe: silence ages the lone agent out, and the
    reconnect's replay re-delivers the same old stamps — the floor must
    not re-anchor on them (the crown stays gone, the agent offline)."""
    f = Fleet()
    subject, data = presence("lone-agent", "dembaca", "online")
    feed(f, (subject, data, ack("PRESENCE", "console", 1, 1, 1000.0)))
    feed(f, ("presence.member.dev.lone-agent", b'{"v": 1}',
              ack("PRESENCE", "console", 2, 2, 1001.0)))
    feed(f, ("presence.crown.dev.lone-agent", b'{"v": 1}',
              ack("PRESENCE", "console", 3, 3, 1002.0)))
    # silence ages it out (the monotonic backdate of the other test)
    staged = f.last_server_now_wall
    f.last_server_now_wall = (staged[0], staged[1] - 10000)
    assert f.state_of("lone-agent") == "offline"
    assert f.holder_of("dev") is None
    # the replay after the reconnect: a NEW consumer re-delivers the last
    # message per subject (higher consumer sequence, the same old stamps)
    # — the floor must not re-anchor on the old stamp, and the lone agent
    # stays offline; the consumer-made-anew reset forgets the claims, so
    # the crown is gone for either reason, both honest
    for i, (sub, dat, ts) in enumerate((
            (subject, data, 1000.0),
            ("presence.member.dev.lone-agent", b'{"v": 1}', 1001.0),
            ("presence.crown.dev.lone-agent", b'{"v": 1}', 1002.0))):
        f.on_delivery(sub, dat, ack("PRESENCE", "console", 10 + i, 20 + i, ts),
                      "PRESENCE", "console")
    assert f.state_of("lone-agent") == "offline"
    assert f.holder_of("dev") is None  # offline, and the claim re-delivered is older
    # a genuinely newer heartbeat re-anchors presence — and a genuinely
    # newer crown claim with it revives the crown (fresh claim on record)
    assert f.on_delivery(subject, data,
                         ack("PRESENCE", "console", 14, 24, 11000.0),
                         "PRESENCE", "console")
    assert f.state_of("lone-agent") == "online"
    assert f.on_delivery("presence.crown.dev.lone-agent", b'{"v": 1}',
                          ack("PRESENCE", "console", 15, 25, 11001.0),
                          "PRESENCE", "console")
    assert f.holder_of("dev") == "lone-agent"


def test_a_wall_clock_step_moves_nothing(monkeypatch) -> None:
    """Fable's pin: the floor counts monotonic time — swap the two
    `time.monotonic()` calls for `time.time()` (the mutants) and this is
    the test that goes red: a wall step of +3600 s via a patched
    `time.time` must not age a fresh agent out, and a step back must not
    revive an aged one."""
    import time as _time
    import console_server.fleet as fleet_mod

    fresh = Fleet()
    subject, data = presence("fresh-agent", "dembaca", "online")
    feed(fresh, (subject, data, ack("PRESENCE", "console", 1, 1, 1000.0)))
    staged = fresh.last_server_now_wall
    real_mono = _time.monotonic()

    # the floor reads time.monotonic (fleet source): patch time.time's
    # step away — the agent stays fresh
    monkeypatch.setattr(fleet_mod.time, "time", lambda: _time.time() + 3600.0)
    assert fresh.state_of("fresh-agent") == "online"  # +1 h of wall: nothing

    # the mutant's face: a backdated wall must not revive the aged
    aged = Fleet()
    feed(aged, (presence("aged-agent", "dembaca", "online")[0],
                presence("aged-agent", "dembaca", "online")[1],
                ack("PRESENCE", "console", 1, 1, 1000.0)))
    staged = aged.last_server_now_wall
    aged.last_server_now_wall = (staged[0], real_mono - 10000)
    monkeypatch.setattr(fleet_mod.time, "time", lambda: _time.time() - 3600.0)
    assert aged.state_of("aged-agent") == "offline"  # -1 h of wall: nothing

    monkeypatch.undo()
    # and the true face for comparison: monotonic elapsed does age it
    assert aged.state_of("aged-agent") == "offline"


def test_the_attestation_expires_by_the_floored_clock() -> None:
    """The attestation's expiry reads the floored clock too: silent
    aging expires it (the card falls back to the presence owner), and a
    fresh delivery keeps it."""
    f = Fleet()
    s_b, d_b = presence("b", "dembaca", "online")
    feed(f, (s_b, d_b, ack("PRESENCE", "console", 1, 1, 1000.0)))
    s_att, d_att = atestation("b", "somebody-else", [], exp=1010.0)
    feed(f, (s_att, d_att, ack("PRESENCE", "console", 2, 2, 1001.0)))
    (card,) = f.cards()
    assert card["for"] == "somebody-else"
    # silence past the attestation's exp: the floored clock expires it
    staged = f.last_server_now_wall
    f.last_server_now_wall = (staged[0], staged[1] - 10000)
    (card,) = f.cards()
    assert card["for"] == "dembaca"  # fell back to the presence owner
    graph = f.graph()
    b_node = next(n for n in graph["nodes"] if n["id"] == "b")
    assert "crown" not in b_node or b_node.get("crown") is False
    # the graph carries the attestation's owner while it is live: before
    # the aging, the edge from b to somebody-else exists; after, only the
    # presence owner's remains
    expired_edges = {e["to"] for e in graph["edges"]
                     if e["from"] == "b" and e["kind"] == "trusts"}
    assert "somebody-else" not in expired_edges
    # the pre-expiry form: a fresh fleet's graph has the attested owner
    g2 = Fleet()
    feed(g2, (presence("c", "dembaca", "online")[0],
              presence("c", "dembaca", "online")[1],
              ack("PRESENCE", "console", 1, 1, 1000.0)))
    feed(g2, (atestation("c", "somebody-else", [], exp=99000.0)[0],
              atestation("c", "somebody-else", [], exp=99000.0)[1],
              ack("PRESENCE", "console", 2, 2, 1001.0)))
    live_edges = {e["to"] for e in g2.graph()["edges"]
                  if e["from"] == "c" and e["kind"] == "trusts"}
    assert "person:somebody-else" in live_edges


def test_cards_show_the_bot_name_the_presence_states_not_the_bus_id() -> None:
    """The console pilot's finding: every card showed the agent's name on the bus
    (`agent-` and 26 characters of hash) and nobody could match it to
    coordinator("who"). The presence an MCP server publishes in a NATS home
    states the bot's username beside its id (natsbus.encode_presence, sent by
    NatsBus each heartbeat): the card shows it, the bus name stays the key."""
    from mattermost_ai_agent_bus.natsbus import agent_name, owner_token
    f = Fleet()
    one = agent_name("dembaca", "UHOSTONE")
    two = agent_name("dembaca", "UHOSTTWO")
    assert len(one) == len("agent-") + 26
    for seq, (agent, bot, label) in enumerate([
            (one, "pxcexeco5fbwmjxqzt3r1ah9yc", "agent-mac-ai-agent-716d72"),
            (two, "q1w2e3r4t5y6u7i8o9p0a1s2d3", "agent-himalia-nats-pilo-772844")], 1):
        feed(f, (f"presence.{owner_token('dembaca')}.{agent}",
                 encode_presence("online", bot, label), ack("PRESENCE", "console", seq, seq, 100.0 + seq)))
    cards = {c["name"]: c for c in f.cards()}
    assert cards[one]["displayName"] == "agent-mac-ai-agent-716d72"
    assert cards[two]["displayName"] == "agent-himalia-nats-pilo-772844"
    # not on the wire the console reads: shown as unknown, never guessed
    assert cards[one]["harness"] is None and cards[one]["model"] == ""
    assert cards[one]["task"] == "" and cards[one]["openAsks"] is None
    graph = {n["id"]: n for n in f.graph()["nodes"]}
    assert graph[two]["label"] == "agent-himalia-nats-pilo-772844"


def name_presence(agent: str, status: str, label: str, seq: int,
                  at: float) -> tuple[str, bytes, str]:
    return (f"presence.dembaca.{agent}", encode_presence(status, "", label),
            ack("PRESENCE", "console", seq, seq, at))


def test_a_name_two_agents_state_is_shown_for_neither() -> None:
    """The username is the agent's own statement: one another known card
    states too names neither card (each shows its bus name), and a name A
    held stays contested after A said bye."""
    f = Fleet()
    feed(f, name_presence("agent-a", "online", "agent-same", 1, 100.0))
    feed(f, name_presence("agent-b", "online", "agent-same", 2, 101.0))
    cards = {c["name"]: c for c in f.cards()}
    assert cards["agent-a"]["displayName"] == "agent-a"
    assert cards["agent-b"]["displayName"] == "agent-b"
    feed(f, name_presence("agent-a", "bye", "agent-same", 3, 102.0))
    cards = {c["name"]: c for c in f.cards()}
    assert cards["agent-b"]["displayName"] == "agent-b"  # a name A held stays contested
    assert cards["agent-a"]["displayName"] == "agent-a"
    # a presence without the name (an older MCP server) shows the bus name
    feed(f, ("presence.dembaca.agent-c", encode_presence("online"),
             ack("PRESENCE", "console", 4, 4, 103.0)))
    assert f.card("agent-c")["displayName"] == "agent-c"


def test_a_shared_name_is_stated_and_marked_never_confirmed() -> None:
    """#199: an agent whose stated name another card states too showed only its
    bus id. The card now carries the stated name beside the confirmed one,
    marked as shared, so the console shows the name with the mark; the
    confirmed `displayName` stays the bus name, as before."""
    f = Fleet()
    feed(f, name_presence("agent-a", "online", "agent-same", 1, 100.0))
    assert f.card("agent-a")["statedName"] == "agent-same"
    assert f.card("agent-a")["nameShared"] is False
    feed(f, name_presence("agent-b", "online", "agent-same", 2, 101.0))
    for agent in ("agent-a", "agent-b"):
        card = f.card(agent)
        assert card["statedName"] == "agent-same"
        assert card["nameShared"] is True
        assert card["displayName"] == agent  # never confirmed while shared
    # no name stated: none to show, nothing shared
    feed(f, ("presence.dembaca.agent-c", encode_presence("online"),
             ack("PRESENCE", "console", 3, 3, 102.0)))
    assert f.card("agent-c")["statedName"] == ""
    assert f.card("agent-c")["nameShared"] is False


def test_an_impostor_after_a_bye_takes_no_name() -> None:
    """Fable's point: once A says bye, B stating A's username shows it on
    neither card, not on B's alone."""
    f = Fleet()
    feed(f, name_presence("agent-a", "online", "agent-mac-ai-agent-716d72", 1, 100.0))
    assert f.card("agent-a")["displayName"] == "agent-mac-ai-agent-716d72"
    feed(f, name_presence("agent-a", "bye", "agent-mac-ai-agent-716d72", 2, 101.0))
    feed(f, name_presence("agent-b", "online", "agent-mac-ai-agent-716d72", 3, 102.0))
    assert f.state_of("agent-a") == "offline"
    assert f.card("agent-b")["displayName"] == "agent-b"
    assert f.card("agent-a")["displayName"] == "agent-a"


def test_an_impostor_after_a_goes_stale_takes_no_name() -> None:
    """No bye: A misses its heartbeats past the freshness window and is
    offline by the server's clock; B stating its name still shows it on
    neither card."""
    f = Fleet()
    feed(f, name_presence("agent-a", "online", "agent-mac-ai-agent-716d72", 1, 100.0))
    feed(f, name_presence("agent-b", "online", "agent-mac-ai-agent-716d72", 2,
                          100.0 + PRESENCE_FRESH_SEC + 10))
    assert f.state_of("agent-a") == "offline"
    assert f.card("agent-b")["displayName"] == "agent-b"
    assert f.card("agent-a")["displayName"] == "agent-a"


def test_the_name_returns_only_once_its_holder_is_pruned() -> None:
    """A's presence leaves the view only past the stream's 24 h: from then on
    B's statement is the only one and its card shows the name."""
    f = Fleet()
    feed(f, name_presence("agent-a", "online", "agent-same", 1, 100.0))
    feed(f, name_presence("agent-b", "online", "agent-same", 2, 200.0))
    late = 100.0 + MAX_AGE_SEC - 100
    feed(f, name_presence("agent-b", "online", "agent-same", 3, late))
    assert f.card("agent-b")["displayName"] == "agent-b"  # A still held: contested
    feed(f, name_presence("agent-b", "online", "agent-same", 4, 100.0 + MAX_AGE_SEC + 10))
    assert f.card("agent-a") is None  # pruned with its presence
    assert f.card("agent-b")["displayName"] == "agent-same"


def test_the_card_states_what_the_presence_states() -> None:
    """RT-6.37: harness, model, task, branch and open asks come from the
    presence, bounded on decode as on encode; self-stated, shown as text."""
    f = Fleet()
    card = PresenceCard(harness="opencode", model="GLM 5.3", task="review #118",
                        branch="console-card-data", asks=2)
    feed(f, (*presence_with("agent-a", card), ack("PRESENCE", "console", 1, 1, 1000.0)))
    c = f.card("agent-a")
    assert c is not None
    assert (c["harness"], c["model"], c["task"], c["branch"], c["openAsks"]) == \
        ("opencode", "GLM 5.3", "review #118", "console-card-data", 2)
    assert c["asksForOperator"] is None  # the presence counts asks, not whose
    # a later presence replaces the card's data, never merges it
    feed(f, (*presence_with("agent-a", PresenceCard(harness="opencode", asks=0)),
             ack("PRESENCE", "console", 2, 2, 1010.0)))
    c = f.card("agent-a")
    assert c is not None
    assert (c["model"], c["task"], c["branch"], c["openAsks"]) == ("", "", None, 0)
    # a body no MCP server makes: each field bounded, the bad ones unknown
    raw = json.dumps({"v": 1, "status": "online", "harness": "<b>x</b>",
                      "model": "m" * 900, "task": "a\nb\x1b", "branch": "x y",
                      "asks": True}).encode()
    feed(f, ("presence.dembaca.agent-a", raw, ack("PRESENCE", "console", 3, 3, 1020.0)))
    c = f.card("agent-a")
    assert c is not None
    assert (c["harness"], c["model"], c["task"], c["branch"], c["openAsks"]) == \
        ("other", "m" * 64, "a b", None, None)


def test_an_older_mcp_server_leaves_the_card_unknown() -> None:
    """RT-6.37: a presence without the card's data (an MCP server before it)
    leaves every field unknown: the app shows n/a, never 0 or empty."""
    f = Fleet()
    feed(f, (*presence("agent-a"), ack("PRESENCE", "console", 1, 1, 1000.0)))
    c = f.card("agent-a")
    assert c is not None
    assert (c["harness"], c["model"], c["task"], c["branch"], c["openAsks"]) == \
        (None, "", "", None, None)


# --- the graph's person nodes and the card's home (#147, 716d72's review) -------------

AGENT_A = "agent-" + "a" * 26
AGENT_B = "agent-" + "b" * 26


def attestation_of(agent: str, anchor: str, name: str, exp: float = 1e12) -> tuple[str, bytes]:
    """The login service's attestation of an owner (RT-6.41): the anchor in name form,
    as the directory holds it ("." kept, e.g. `sub-alice` or `alice.one`), the human's
    name beside it. The login's *own* record carries the subject form instead."""
    body = {"v": 1, "agent": agent, "owner": {"anchor": anchor, "name": name},
            "trusted": [], "cut": False, "iat": 1, "exp": int(exp)}
    return f"presence.attest.{agent}.v1", json.dumps(body).encode()


def test_the_operators_node_is_marked_by_its_anchor_in_subject_form() -> None:
    """716d72's finding 1: the login record's anchor is already subject-formed ("." as
    "~"), so it is compared as it is — a dotted anchor marks its node, and no owner-less
    token is ever collected to match one."""
    from mattermost_ai_agent_bus.natsbus import encode_claim, owner_token

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "first~last"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         # the attestation carries the name form, the login's own record the subject form
         (*attestation_of(AGENT_A, "first.last", "first.last"), ack("PRESENCE", "console", 2, 2, 1001.0)),
         (f"presence.member.dev.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 3, 3, 1002.0)))
    marked = [n for n in f.graph("first~last")["nodes"] if n.get("you")]
    assert marked == [{"id": "person:first~last", "kind": "person", "label": "first.last",
                       "you": True}]
    # no anchor, the no-owner token and another anchor mark nothing
    assert owner_token("first~last") == "-"  # the subject form is not to be re-tokenised
    for anchor in (None, "", "-", "first.last"):
        assert not any(n.get("you") for n in f.graph(anchor)["nodes"])


def test_two_people_of_one_name_stay_two_nodes() -> None:
    """716d72's finding 2: the node id and the trusts edge are the anchor token, the name
    only labels — two owners of one name are two nodes, and only the operator's is `you`."""
    from mattermost_ai_agent_bus.natsbus import encode_claim

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "sub-alice"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         (*presence(AGENT_B, "sub-bob"), ack("PRESENCE", "console", 2, 2, 1001.0)),
         # the directory's anchors differ, the name both carry is "alice"
         (*attestation_of(AGENT_A, "sub-alice", "alice"), ack("PRESENCE", "console", 3, 3, 1002.0)),
         (*attestation_of(AGENT_B, "sub-bob", "alice"), ack("PRESENCE", "console", 4, 4, 1003.0)),
         (f"presence.member.dev.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 5, 5, 1004.0)),
         (f"presence.member.dev.{AGENT_B}", encode_claim(), ack("PRESENCE", "console", 6, 6, 1005.0)))
    graph = f.graph("sub-alice")
    people = [n for n in graph["nodes"] if n["kind"] == "person"]
    assert [(n["id"], n["label"], n.get("you")) for n in people] == [
        ("person:sub-alice", "alice", True), ("person:sub-bob", "alice", None)]
    assert len([n for n in people if n.get("you")]) == 1
    targets = {e["to"] for e in graph["edges"] if e["kind"] == "trusts"}
    assert targets == {"person:sub-alice", "person:sub-bob"}


def test_the_home_is_a_claim_of_its_own_not_only_the_holders() -> None:
    """716d72's finding 3: the login service grants a crown claim in the home alone
    (RT-6.38), so any claim of the agent's own names the home — a non-holder's included;
    only without one does the alphabetical first membership answer."""
    from mattermost_ai_agent_bus.natsbus import encode_claim

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "dembaca"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         (*presence(AGENT_B, "dembaca"), ack("PRESENCE", "console", 2, 2, 1001.0)),
         (f"presence.member.alpha.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 3, 3, 1002.0)),
         (f"presence.member.zeta.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 4, 4, 1003.0)),
         (f"presence.member.alpha.{AGENT_B}", encode_claim(), ack("PRESENCE", "console", 5, 5, 1004.0)),
         (f"presence.member.zeta.{AGENT_B}", encode_claim(), ack("PRESENCE", "console", 6, 6, 1005.0)),
         # AGENT_B claims zeta first, so it holds it: AGENT_A's claim is older
         (f"presence.crown.zeta.{AGENT_B}", encode_claim(), ack("PRESENCE", "console", 7, 7, 1006.0)),
         (f"presence.crown.zeta.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 8, 8, 1007.0)))
    assert f.holder_of("zeta") == AGENT_B
    assert (f.home_of(AGENT_A), f.guests_of(AGENT_A)) == ("zeta", ["alpha"])


def test_an_ask_records_channel_is_a_home_only_beside_a_membership() -> None:
    """716d72's finding 4: a record can outlive a lost channel; a home it names that the
    agent is no member of is no home, and every membership is then a guest of the real one."""
    from mattermost_ai_agent_bus.natsbus import OpenAsk, encode_ask_record, encode_claim

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "dembaca"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         (f"presence.member.dev.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 2, 2, 1001.0)))
    record = encode_ask_record([OpenAsk(mm="a" * 26, kind="question", human="-",
                                        title="x", asked=1000)], 1000)
    assert f.on_delivery(f"presence.ask.gone.{AGENT_A}", record,
                         ack("PRESENCE", "console", 3, 3, 1002.0), "PRESENCE", "console")
    assert f.home_of(AGENT_A) == "dev"
    assert f.guests_of(AGENT_A) == []
    # beside a membership it wins over the alphabet
    feed(f, (f"presence.member.zeta.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 4, 4, 1003.0)))
    record = encode_ask_record([OpenAsk(mm="a" * 26, kind="question", human="-",
                                        title="x", asked=1000)], 1500)
    assert f.on_delivery(f"presence.ask.zeta.{AGENT_A}", record,
                         ack("PRESENCE", "console", 5, 5, 1500.0), "PRESENCE", "console")
    assert (f.home_of(AGENT_A), f.guests_of(AGENT_A)) == ("zeta", ["dev"])


def test_a_claim_of_a_channel_it_left_is_no_home() -> None:
    """Fable's delta review: claims never age out (the pruning takes members, asks and
    attestations), so a claim in a channel the agent has since left must not name a home
    — else the card shows a home with no coordinates edge."""
    from mattermost_ai_agent_bus.natsbus import encode_claim

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "dembaca"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         (f"presence.crown.gone.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 2, 2, 1001.0)),
         (f"presence.member.dev.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 3, 3, 1002.0)))
    assert (f.home_of(AGENT_A), f.guests_of(AGENT_A)) == ("dev", [])
    graph = f.graph()
    assert {e["to"] for e in graph["edges"] if e["kind"] == "coordinates"} == {"dev"}


def test_an_owner_named_like_a_channel_gets_its_own_node() -> None:
    """716d72's delta review: the person's id carries `person:`, the channel's its name,
    so an owner `dev` beside a channel `dev` is two nodes with two ids and the trusts
    edge points at the person's."""
    from mattermost_ai_agent_bus.natsbus import encode_claim

    f = Fleet()
    feed(f,
         (*presence(AGENT_A, "dev"), ack("PRESENCE", "console", 1, 1, 1000.0)),
         (*attestation_of(AGENT_A, "dev", "dev"), ack("PRESENCE", "console", 2, 2, 1001.0)),
         (f"presence.member.dev.{AGENT_A}", encode_claim(), ack("PRESENCE", "console", 3, 3, 1002.0)))
    graph = f.graph("dev")
    nodes = {n["id"]: n for n in graph["nodes"]}
    assert (nodes["person:dev"]["kind"], nodes["person:dev"]["label"]) == ("person", "dev")
    assert nodes["person:dev"].get("you") is True
    assert (nodes["dev"]["kind"], nodes["dev"]["label"]) == ("channel", "dev")
    assert len(graph["nodes"]) == len(nodes)  # two nodes, and no id stands twice
    assert (AGENT_A, "person:dev", "trusts") in {
        (e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert (AGENT_A, "dev", "coordinates") in {
        (e["from"], e["to"], e["kind"]) for e in graph["edges"]}
