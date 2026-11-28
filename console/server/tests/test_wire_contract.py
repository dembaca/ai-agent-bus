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
    # it each heartbeat (the card: the HELLO's harness, model, task, branch, the
    # open asks and the last post's time, RT-6.37), membership and crown `encode_claim`.
    agent = agent_name("dembaca", NKEY_PUBLIC)
    assert agent == FIXTURE["card"]["name"]
    rows = [
        # presence: online, the owner token names the owner, the body the bot
        (f"presence.{owner_token('dembaca')}.{agent}",
         encode_presence("online", BOT_ID, FIXTURE["card"]["displayName"], PresenceCard(
             harness=FIXTURE["card"]["harness"], model=FIXTURE["card"]["model"],
             task=FIXTURE["card"]["task"], branch=FIXTURE["card"]["branch"],
             asks=FIXTURE["card"]["openAsks"],
             last_post=FIXTURE["card"]["lastPostAt"] // 1000)),
         ack(1, 1, 1763980760.0)),
        # membership of the channel dev
        (f"presence.member.dev.{agent}", encode_claim(),
         ack(2, 2, 1763980770.0)),
        # a guest channel's membership beside the home: the login service writes one
        # for every granted channel (RT-6.37, RT-6.16)
        (f"presence.member.bgl-infra.{agent}", encode_claim(),
         ack(6, 6, 1763980795.0)),
        # a crown claim on dev
        (f"presence.crown.dev.{agent}", encode_claim(),
         ack(3, 3, 1763980780.0)),
        # the login service's attestation of owner and trust, as the Go service
        # writes it (services/auth/internal/callout/attest.go, the record type)
        (f"presence.attest.{agent}.v1",
         json.dumps({"v": 1, "agent": agent, "home": "dev",
                     "owner": {"anchor": "dembaca", "name": "dembaca"},
                     "trusted": [{"anchor": "dembaca", "name": "dembaca"}],
                     "cut": False, "iat": 1, "exp": 1763990000}).encode(),
         ack(4, 4, 1763980790.0)),
    ]
    for subject, data, reply in rows:
        assert f.on_delivery(subject, data, reply, "PRESENCE", "console")
    return f


def test_the_card_the_fixture_states(fleet_scenario) -> None:
    # the operator's own agent (the attestation names dembaca as owner): its card
    # carries the last post, which another owner's operator never sees (#201)
    cards = fleet_scenario.cards("dembaca")
    assert cards == [FIXTURE["card"]]
    assert fleet_scenario.cards("someone-else")[0]["lastPostAt"] is None


def test_a_guest_channel_is_no_home(fleet_scenario) -> None:
    """#147: the home is what the bus itself states — the ask record's subject first
    (RT-6.43 step 5), then a crown claim (only a home may be claimed, RT-6.38) — and
    every other membership is a guest channel, never guessed as one of the home's."""
    agent = FIXTURE["card"]["name"]
    assert (fleet_scenario.home_of(agent), fleet_scenario.guests_of(agent)) == (
        "dev", ["bgl-infra"])
    assert fleet_scenario.home_of("nobody") == "" and fleet_scenario.guests_of("nobody") == []
    # an ask record's subject names the home: beside a membership it wins over the
    # alphabet (a channel the agent is no member of is no home, test_fleet.py)
    from mattermost_ai_agent_bus.natsbus import OpenAsk, encode_ask_record, encode_claim
    assert fleet_scenario.on_delivery(f"presence.member.home.{agent}", encode_claim(),
                                      ack(8, 8, 1763980800.0), "PRESENCE", "console")
    record = encode_ask_record([OpenAsk(mm="a" * 26, kind="question", human="-",
                                        title="x", asked=1763980700)], 1763980800)
    assert fleet_scenario.on_delivery(f"presence.ask.home.{agent}", record,
                                      ack(7, 7, 1763980800.0), "PRESENCE", "console")
    assert (fleet_scenario.home_of(agent), fleet_scenario.guests_of(agent)) == (
        "home", ["bgl-infra", "dev"])


def test_the_events_the_fixture_states(fleet_scenario) -> None:
    console = Console(config.Settings(public_url="http://127.0.0.1:8747",
                                     login_off=True, nats_url="nats://127.0.0.1:4"))
    console.fleet = fleet_scenario
    console.fleet.synced = True
    stream = console._stream(token=None)
    assert stream.first["type"] == "snapshot"
    assert {c["name"] for c in stream.first["cards"]} == {FIXTURE["card"]["name"]}
    # no login's own record here, so no anchor: no card carries the last post (#201)
    assert stream.first["cards"][0]["lastPostAt"] is None
    wave = stream.snapshot()
    assert wave == []          # nothing changed: no event at all
    # the fixture's three event shapes are the whole alphabet
    shapes = {FIXTURE["events"][name]["type"] for name in FIXTURE["events"]}
    assert shapes == {"snapshot", "card", "drop"}
    # the change event's card is the same shape: the two copies cannot drift
    assert FIXTURE["events"]["card"]["card"] == FIXTURE["card"]


def test_the_last_post_follows_the_anchor_that_arrives_after_the_snapshot(fleet_scenario) -> None:
    """#201 (the owner's decision of 2026-11-27): the stream's first snapshot comes before
    the login's own record gave the operator's anchor, so the own agent's card has no last
    post; once the anchor is there, the next wave sends that card again as a `card` event
    with the last post, and nothing else changes on it."""
    from types import SimpleNamespace
    console = Console(config.Settings(public_url="http://127.0.0.1:8747",
                                     login_off=True, nats_url="nats://127.0.0.1:4"))
    console.fleet = fleet_scenario
    console.fleet.synced = True
    view = console._view(None)
    assert view.anchor() is None
    stream = console._stream(token=None)
    (first,) = stream.first["cards"]
    assert first["lastPostAt"] is None
    view.reader = SimpleNamespace(anchor="dembaca")  # type: ignore[assignment]
    wave = stream.snapshot()
    assert wave == [{"type": "card", "card": FIXTURE["card"]}]
    assert wave[0]["card"]["lastPostAt"] == FIXTURE["card"]["lastPostAt"]
    assert {k: v for k, v in first.items() if k != "lastPostAt"} == \
        {k: v for k, v in FIXTURE["card"].items() if k != "lastPostAt"}
    assert stream.snapshot() == []  # sent once


def test_the_app_side_shapes_split_by_seam() -> None:
    """Cards and events are the app's camelCase shapes; asks and /v1/me
    keep the ADR's spellings (`for_me`, `may_answer`, `display_name`) —
    exactly the two seams http.ts maps (the ask row itself is built from
    an MCP server's record in test_the_ask_row_the_fixture_states)."""
    ask = FIXTURE["ask"]
    assert set(ask) >= {"for_me", "may_answer", "display_name", "thread_id", "answers", "chosen"}
    me = FIXTURE["me"]
    assert set(me) == {"username", "display_name", "groups", "build"}
    assert set(me["build"]) == {"version", "revision"}  # #182: the footer's version
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
                                        title=ask["need"], asked=ask["at"] // 1000,
                                        answers=tuple(ask["answers"]))], 1763980800)
    assert fleet_scenario.on_delivery(f"presence.ask.{ask['home']}.{ask['by']}", record,
                                      ack(5, 5, 1763980800.0), "PRESENCE", "console")
    assert fleet_scenario.asks() == [ask]


def test_the_report_the_fixture_states() -> None:
    """RT-6.43 step 8: a report as `/v1/stats` sends it, from the protocol's example line as
    an MCP server publishes it (`stats.nats_body`) on `<stats>.<name>`."""
    from mattermost_ai_agent_bus import stats
    from console_server.stats_view import StatsBook
    from test_asks_stats import STATS_CONSUMER

    line = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code",
            "kind": "session", "min": 1440, "turns": 31, "asks": 2, "asks_answered": 2,
            "ask_wait_sec": 1260, "crown_sec": 0,
            "tok": {"req": 120, "bus_req": 18, "fixed": 21000, "all": {"in": 1200, "read": 90000, "out": 900},
                    "ovh": {"in": 150, "read": 21000, "out": 80}, "coord": {"in": 300, "read": 4000, "out": 160},
                    "req_ovh": 6, "req_coord": 9, "m": 1}}
    book = StatsBook()
    body = stats.nats_body(line)
    assert body is not None
    assert book.on_delivery(f"stats.{FIXTURE['card']['name']}", body,
                            f"$JS.ACK.STATS.{STATS_CONSUMER}.1.1.1.1763980800000000000.0",
                            "STATS", STATS_CONSUMER)
    report = next(r for r in book.reports() if r["id"] == FIXTURE["report"]["id"])
    assert report == FIXTURE["report"]


def test_the_activity_card_the_fixture_states() -> None:
    """#284: the activity card of `/v1/stats?range=7d` (`reports()[0]`) from a protocol line
    that carries `post_words`: seven daily bars, the four groups, the words of the hover."""
    from mattermost_ai_agent_bus import stats
    from console_server.stats_view import StatsBook
    from test_asks_stats import STATS_CONSUMER

    line = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code", "kind": "session",
            "min": 1440, "post_words": {"ACK": [3, 30], "PROGRESS": [2, 40], "RESULT": [1, 90],
                                        "DONE": [1, 20], "ask": [1, 60], "TASK": [1, 70], "plain": [2, 80]}}
    book = StatsBook()
    body = stats.nats_body(line)
    assert body is not None
    assert book.on_delivery(f"stats.{FIXTURE['card']['name']}", body,
                            f"$JS.ACK.STATS.{STATS_CONSUMER}.1.1.1.1763980800000000000.0",
                            "STATS", STATS_CONSUMER)
    assert book.reports("7d")[0] == FIXTURE["activity"]


def test_the_24h_activity_cards_the_fixture_states() -> None:
    """#284, #290: "24 h" draws 24 hourly bars from the line that carries `post_hours` and says
    how many lines carried hours (`lines` of `of`); with none it is n/a with its reason."""
    from mattermost_ai_agent_bus import stats
    from console_server.stats_view import StatsBook
    from test_asks_stats import STATS_CONSUMER, ack

    at = 1763980800 + 1800
    base = {"v": "0.104.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code", "kind": "session", "min": 1440}
    withh = base | {"post_words": {"ACK": [3, 30], "DONE": [1, 20], "ask": [1, 60], "plain": [2, 80]},
                    "post_hours": {"489999": {"ACK": 3, "DONE": 1}, "490000": {"ask": 1, "plain": 2}}}
    without = base | {"sid": "8e21b0c4a9f3", "part": 1, "post_words": {"ACK": [1, 10]}}

    def book(*lines: tuple[str, dict]) -> StatsBook:
        b = StatsBook()
        for i, (name, line) in enumerate(lines, 1):
            body = stats.nats_body(line)
            assert body is not None
            assert b.on_delivery(f"stats.{name}", body, ack("STATS", STATS_CONSUMER, i, at), "STATS", STATS_CONSUMER)
        return b

    assert book(("agent-a", withh), ("agent-b", without)).reports("24h")[0] == FIXTURE["activity24h"]
    assert book(("agent-b", without)).reports("24h")[0] == FIXTURE["activityNoHours"]


def test_the_card_of_a_range_not_held_the_fixture_states() -> None:
    """#284: a previous range that starts before the oldest line held is n/a with its reason,
    every figure null, never 0."""
    from console_server.stats_view import StatsBook
    from test_asks_stats import A, LINE, NOW, book_with

    book: StatsBook = book_with((A, LINE, NOW - 600))
    assert book.reports("7d")[3] == FIXTURE["asksBefore"]


def test_the_agent_stats_row_the_fixture_states() -> None:
    """/v1/agent-stats (#201): the row as the server sends it, one per agent, from the
    same protocol line as the report above — the token sums of both windows and the
    24-hour counts; a field the line does not carry is n/a, never 0."""
    from mattermost_ai_agent_bus import stats
    from console_server.stats_view import StatsBook
    from test_asks_stats import STATS_CONSUMER

    line = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code",
            "kind": "session", "min": 1440, "turns": 31, "asks": 2, "asks_answered": 2,
            "ask_wait_sec": 1260, "crown_sec": 0,
            "tok": {"req": 120, "bus_req": 18, "fixed": 21000, "all": {"in": 1200, "read": 90000, "out": 900},
                    "ovh": {"in": 150, "read": 21000, "out": 80}, "coord": {"in": 300, "read": 4000, "out": 160},
                    "req_ovh": 6, "req_coord": 9, "m": 1}}
    book = StatsBook()
    body = stats.nats_body(line)
    assert body is not None
    assert book.on_delivery(f"stats.{FIXTURE['card']['name']}", body,
                            f"$JS.ACK.STATS.{STATS_CONSUMER}.1.1.1.1763980800000000000.0",
                            "STATS", STATS_CONSUMER)
    assert book.agent_rows({FIXTURE["card"]["name"]}) == FIXTURE["agentStats"]


def test_the_groups_the_fixture_states(fleet_scenario) -> None:
    """#283: `/v1/groups` for the operator's own agent: counted on the server, the keys the
    browser's types take as they are."""
    from console_server import groups
    f = fleet_scenario
    got = groups.groups(f.cards("dembaca"), f.asks("dembaca"), [], by="home", scope="mine",
                        own=f.own("dembaca"), own_or_trusted=f.own_or_trusted("dembaca"))
    assert got == FIXTURE["groups"]


def test_the_no_row_sentence_is_quoted_word_for_word() -> None:
    """#201 (Fable's review of #236): why an agent has no per-agent numbers is said by the
    app's two constants (`console/web/src/ui/agents.ts`); the web README, the CHANGELOG and
    ADR-015 quote them word for word, so no copy drifts from what the page says."""
    import re
    root = Path(__file__).parents[3]
    source = (root / "console/web/src/ui/agents.ts").read_text()
    said = " ".join(re.search(rf'export const {name} =\s*"([^"]+)"', source).group(1)  # type: ignore[union-attr]
                    for name in ("NO_STATS_ROW", "STATS_ROWS_NOTE"))
    assert "until its first STATS part arrives" in said
    for doc in ("console/web/README.md", "CHANGELOG.md", "docs/arc42/09-architecture-decisions.md"):
        text = " ".join((root / doc).read_text().split())
        assert said in text, doc
