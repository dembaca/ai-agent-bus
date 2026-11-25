"""The wire contract, held from one fixture: ``console/fixtures/console-api.json``
is the API of ADR-015 point 10 as both sides speak it — the server builds
this card from real deliveries, the app's types are checked against the
same file (`console/web/src/api/wire.ts`), and a drift on either side
fails this or the app's typecheck.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from console_server import config
from console_server.app import Console
from mattermost_ai_agent_bus.natsbus import (
    PresenceCard, agent_name, encode_claim, encode_presence, owner_token,
)

FIXTURE = json.loads((Path(__file__).parents[2] / "fixtures" / "console-api.json").read_text())


# a host's public nkey (any one: the agent's name is a hash over it) and a
# Mattermost bot id (26 characters, natsbus.BOT_RE)
NKEY_PUBLIC = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
BOT_ID = "pxcexeco5fbwmjxqzt3r1ah9yc"


def ack(sseq: int, cseq: int, ts: float) -> str:
    return f"$JS.ACK.PRESENCE.console.{sseq}.{sseq}.{cseq}.{int(ts * 1e9)}.0"


@pytest.fixture
def fleet_scenario():
    from console_server.fleet import Fleet
    f = Fleet(presence_subject="presence")
    # The shapes an MCP server really publishes in a NATS home, made by its own
    # encoders (mcp/src/mattermost_ai_agent_bus/natsbus.py): the agent's name
    # is `agent_name` (the login service's hash, never the bot's username),
    # the presence subject `NatsBus._presence_subject`, its body
    # `encode_presence(status, bot id, bot username, card)` as `NatsBus` sends
    # it each heartbeat (the card: the HELLO's harness, model, task, branch and
    # the open asks, RT-6.37), membership and crown `encode_claim`.
    agent = agent_name("dembaca", NKEY_PUBLIC)
    assert agent == FIXTURE["card"]["name"]
    rows = [
        # presence: online, the owner token names the owner, the body the bot
        (f"presence.{owner_token('dembaca')}.{agent}",
         encode_presence("online", BOT_ID, FIXTURE["card"]["displayName"], PresenceCard(
             harness=FIXTURE["card"]["harness"], model=FIXTURE["card"]["model"],
             task=FIXTURE["card"]["task"], branch=FIXTURE["card"]["branch"],
             asks=FIXTURE["card"]["openAsks"])),
         ack(1, 1, 1763980760.0)),
        # membership of the channel dev
        (f"presence.member.dev.{agent}", encode_claim(),
         ack(2, 2, 1763980770.0)),
        # a crown claim on dev
        (f"presence.crown.dev.{agent}", encode_claim(),
         ack(3, 3, 1763980780.0)),
        # the login service's attestation of owner and trust, as the Go service
        # writes it (services/auth/internal/callout/attest.go, the record type)
        (f"presence.attest.{agent}.v1",
         json.dumps({"v": 1, "agent": agent,
                     "owner": {"anchor": "dembaca", "name": "dembaca"},
                     "trusted": [{"anchor": "dembaca", "name": "dembaca"}],
                     "cut": False, "iat": 1, "exp": 1763990000}).encode(),
         ack(4, 4, 1763980790.0)),
    ]
    for subject, data, reply in rows:
        assert f.on_delivery(subject, data, reply, "PRESENCE", "console")
    return f


def test_the_card_the_fixture_states(fleet_scenario) -> None:
    cards = fleet_scenario.cards()
    assert cards == [FIXTURE["card"]]


def test_the_events_the_fixture_states(fleet_scenario) -> None:
    console = Console(config.Settings(public_url="http://127.0.0.1:8747",
                                     login_off=True, nats_url="nats://127.0.0.1:4"))
    console.fleet = fleet_scenario
    console.fleet.synced = True
    stream = console._stream(token=None)
    assert stream.first["type"] == "snapshot"
    assert {c["name"] for c in stream.first["cards"]} == {FIXTURE["card"]["name"]}
    wave = stream.snapshot()
    assert wave == []          # nothing changed: no event at all
    # the fixture's three event shapes are the whole alphabet
    shapes = {FIXTURE["events"][name]["type"] for name in FIXTURE["events"]}
    assert shapes == {"snapshot", "card", "drop"}


def test_the_app_side_shapes_split_by_seam() -> None:
    """Cards and events are the app's camelCase shapes; asks and /v1/me
    keep the ADR's spellings (`for_me`, `may_answer`, `display_name`) —
    exactly the two seams http.ts maps (the ask row itself is built from
    an MCP server's record in test_the_ask_row_the_fixture_states)."""
    ask = FIXTURE["ask"]
    assert set(ask) >= {"for_me", "may_answer", "display_name", "thread_id"}
    me = FIXTURE["me"]
    assert set(me) == {"username", "display_name", "groups"}
    page = FIXTURE["page"]
    assert set(page) == {"rootId", "posts", "next"}
    assert set(page["posts"][0]) == {"id", "rootId", "author", "at", "message"}


def test_the_ask_row_the_fixture_states(fleet_scenario) -> None:
    """RT-6.43 step 6: the ask row the app takes, built from the record an MCP server
    publishes (`natsbus.encode_ask_record`, as `NatsBus.publish_asks` sends it on
    `<presence>.ask.<home>.<name>`) and read by the fleet's strict decoder."""
    from mattermost_ai_agent_bus.natsbus import OpenAsk, encode_ask_record

    ask = FIXTURE["ask"]
    record = encode_ask_record([OpenAsk(mm=ask["id"], kind=ask["kind"], human="dembaca",
                                        title=ask["need"], asked=ask["at"] // 1000)], 1763980800)
    assert fleet_scenario.on_delivery(f"presence.ask.{ask['home']}.{ask['by']}", record,
                                      ack(5, 5, 1763980800.0), "PRESENCE", "console")
    assert fleet_scenario.asks() == [ask]


def test_the_report_the_fixture_states() -> None:
    """RT-6.43 step 8: a report as `/v1/stats` sends it, from the protocol's example line as
    an MCP server publishes it (`stats.nats_body`) on `<stats>.<name>`."""
    from mattermost_ai_agent_bus import stats
    from console_server.stats_view import StatsBook

    line = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code",
            "kind": "session", "min": 1440, "turns": 31, "asks": 2, "asks_answered": 2,
            "ask_wait_sec": 1260, "crown_sec": 0}
    book = StatsBook()
    body = stats.nats_body(line)
    assert body is not None
    assert book.on_delivery(f"stats.{FIXTURE['card']['name']}", body,
                            "$JS.ACK.STATS.console_stats.1.1.1.1763980800000000000.0",
                            "STATS", "console_stats")
    report = next(r for r in book.reports() if r["id"] == FIXTURE["report"]["id"])
    assert report == FIXTURE["report"]
