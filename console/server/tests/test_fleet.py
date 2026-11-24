"""The fleet reader: subject shapes, ordering by the server's clock,
freshness, the crown rule (RT-6.38) and the wire cards — each fed with
synthetic JetStream ack metadata, exactly the deliveries a live consumer
would make, so no server is needed (the live ones are test_nats_live).
"""
from __future__ import annotations

import json


from mattermost_ai_agent_bus.natsbus import encode_presence
from console_server.fleet import Fleet, short_name


def ack(stream: str, consumer: str, sseq: int, cseq: int, ts: float,
        pending: int = 0) -> str:
    return f"$JS.ACK.{stream}.{consumer}.{sseq}.{sseq}.{cseq}.{int(ts * 1e9)}.{pending}"


def presence(agent: str, owner: str = "dembaca", status: str = "online",
             bot: str = "") -> tuple[str, bytes]:
    return f"presence.{owner}.{agent}", encode_presence(status, bot)


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
    assert f.server_now == 1090.0


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
    assert card["display_name"] == "a1b2c3"
    assert short_name("agent-mac-ai-agent-a1b2c3") == "a1b2c3"
    assert card["for"] == "dembaca"       # from the attestation
    assert card["trusts"] == ["dembaca"]  # from the attestation
    assert card["home"] == "dev"
    # what a NATS home does not carry is unknown, never guessed as zero
    assert card["ctx"] is None
    assert card["model"] == ""
    assert card["openAsks"] == 0
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
    assert kinds == {"dembaca": "person", "dev": "channel", "b": "agent"}
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
