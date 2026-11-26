"""#158's ``/metrics`` (ADR-015): who may scrape — the operator that is
logged in, the same right ``/v1/*`` needs; what it serves — the console's
own observations aggregated, and the agent numbers of the scrape session's
own reading, the same data the Reports/Stats tab shows it. Labels stay in
bounded sets: no agent name, owner, home or session id ever becomes one
(RT-6.43's stance), anything unknown counts as ``other``, and a value that
is n/a leaves its series out instead of a 0.

Fed with what an MCP server really publishes (as test_asks_stats.py) and
asked through the raw asyncio httpd (as test_http.py).
"""
from __future__ import annotations

import asyncio
import re

import pytest

from mattermost_ai_agent_bus import stats
from mattermost_ai_agent_bus.natsbus import (OpenAsk, PresenceCard, agent_name,
                                             encode_ask_record, encode_presence)

from console_server import config, telemetry
from console_server.app import Console
from console_server.nats_read import View
from console_server.telemetry import route_bucket

NOW = 1764061800.0
NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
A = agent_name("dembaca", NKEY)

SETTINGS = config.Settings(public_url="http://127.0.0.1:8747",
                          login_off=True, nats_url="")
LOGIN_ON = config.Settings(public_url="http://127.0.0.1:8747",
                           login_off=False, nats_url="")

SERIES = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})? (-?[0-9]+(?:\.[0-9]+)?)$")
CONSOLE_FAMILIES = ("console_http_requests_total", "console_sessions_open",
                    "console_sse_streams_open", "console_bus_readers",
                    "console_bus_connects_total",
                    "console_credential_renewals_total",
                    "console_credential_renewal_failures_total",
                    "console_stats_lines_refused_total",
                    "console_fleet_records_refused_total")
AGENT_FAMILIES = ("bus_agents", "bus_asks_open", "bus_tokens_eq")

# a STATS line with the token split of method 1 (RT-6.9 step 2), as the
# house tests build it
SPLIT = {"ovh": {"in": 300, "out": 30}, "coord": {"in": 200, "out": 20},
         "req_ovh": 1, "req_coord": 2, "m": 1}
LINE = {"v": "0.101.0", "sid": "aaaaaaaaaaaa", "part": 1, "rt": "claude-code",
        "kind": "session", "min": 600, "tok": {"req": 10, "bus_req": 2, "fixed": 100,
                                              "all": {"in": 1000, "out": 100},
                                              "bus": {"in": 100, "out": 10}} | SPLIT}


def ack(stream: str, consumer: str, seq: int, ts: float) -> str:
    return f"$JS.ACK.{stream}.{consumer}.{seq}.{seq}.{seq}.{int(ts * 1e9)}.0"


def page(text: str) -> tuple[set[str], dict[str, float]]:
    """The page parsed: its families (HELP + TYPE, exactly once each) and its
    series (the whole line up to the value), every other line refused."""
    helps: set[str] = set()
    types: dict[str, str] = {}
    series: dict[str, float] = {}
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# HELP "):
            name = line.split()[2]
            assert name not in helps, f"two HELP of {name}"
            helps.add(name)
            continue
        if line.startswith("# TYPE "):
            _h, _t, name, kind = line.split()
            types[name] = kind
            continue
        m = SERIES.match(line)
        assert m, f"not an exposition line: {line!r}"
        name, labels, value = m.group(1), m.group(2) or "", m.group(3)
        assert name in helps and name in types, f"no HELP/TYPE before: {line!r}"
        ident = f"{name}{labels}"
        assert ident not in series, f"a repeated series: {line!r}"
        series[ident] = float(value)
    assert helps == set(types), "every family says HELP and TYPE exactly once"
    return helps, series


async def get(console: Console, path: str, method: str = "GET"
              ) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", console._http.bound_port)
    head = f"{method} {path} HTTP/1.1\r\nHost: x\r\n"
    if method == "POST":
        head += "Content-Length: 0\r\n"
    writer.write((head + "\r\n").encode())
    answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
    lines = answer.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":")
                                                            for l in lines[1:-1])}
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    writer.close()
    return int(lines[0].split()[1]), headers, body


@pytest.fixture
async def console() -> Console:
    c = Console(SETTINGS)
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


async def test_a_missing_operator_may_not_scrape() -> None:
    """#158: the endpoint needs the operator's login, the same right /v1/me
    needs — a scraper without a session answers 401, not the numbers."""
    answer = await Console(LOGIN_ON).handle("/metrics", {}, {})
    assert answer.status == 401              # type: ignore[attr-defined]
    assert answer.body == b'{"error": "operator login required"}'  # type: ignore[attr-defined]


async def test_the_page_is_the_exposition_format(console: Console) -> None:
    status, headers, body = await get(console, "/metrics")
    assert status == 200
    assert headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    families, series = page(body.decode())
    assert families == set(CONSOLE_FAMILIES) | set(AGENT_FAMILIES)
    # an empty stand-in reading: the fleet-less series stay 0 or absent —
    # bus_agents and bus_tokens_eq name no series at all, never a 0 that
    # would read as "no agents" on a page whose reader never got one
    assert series["bus_asks_open"] == 0.0
    assert series["console_sessions_open"] == 0.0
    assert series["console_sse_streams_open"] == 0.0
    assert not any(k.startswith("bus_agents") for k in series)
    assert not any(k.startswith("bus_tokens_eq") for k in series)


async def test_requests_are_counted_by_bucketed_route(console: Console) -> None:
    """The console's own observation: every answered request counts, by its
    bucketed route, method and status — a 405 keeps its method, the variable
    thread tail is its one bucket, the page's own requests count too."""
    await get(console, "/healthz")
    await get(console, "/healthz")
    await get(console, "/v1/threads/agent-mac-ai-agent-x/whatever")
    await get(console, "/logout", method="POST")
    _status, _headers, body = await get(console, "/metrics")
    _families, series = page(body.decode())
    # the readiness path counts under its own bucket, whatever the settings
    # name it; a PR that renames it reuses the same series
    assert series['console_http_requests_total{route="health",method="GET",status="200"}'] == 2.0
    assert series['console_http_requests_total{route="/v1/threads",method="GET",status="200"}'] == 1.0
    assert series['console_http_requests_total{route="/logout",method="POST",status="303"}'] == 1.0
    # the page renders before its own request is noted (handle routes first,
    # counts after), so one scrape never shows itself — the next shows it
    assert not any(k.startswith('console_http_requests_total{route="/metrics"') for k in series)
    await get(console, "/anything/else")
    _status, _headers, body = await get(console, "/metrics")
    _families, series = page(body.decode())
    assert series['console_http_requests_total{route="other",method="GET",status="404"}'] == 1.0
    assert series['console_http_requests_total{route="/metrics",method="GET",status="200"}'] == 1.0


def test_the_route_buckets_are_bounded() -> None:
    assert route_bucket("/v1/threads/x/y") == "/v1/threads"
    assert route_bucket("/assets/index-abc123.js") == "static"
    assert route_bucket("/favicon.svg") == "static"
    assert route_bucket("/anything/else") == "other"


def _reading() -> View:
    """One session's own reading: A online on opencode with a card, B fresh
    away without one (harness n/a), one open ask of A's, two STATS lines with
    the split (claude-code and opencode) — and one line each the strict
    decoders refused."""
    view = View("presence", "stats")
    record = encode_ask_record([OpenAsk("m" * 26, "approval", "dembaca", "?",
                                        int(NOW) - 30)], int(NOW))
    rows = [
        (f"presence.dembaca.{A}", encode_presence("online", "", "",
         PresenceCard(harness="opencode", model="GLM", task="review", asks=0)), NOW),
        ("presence.dembaca.agent-b", encode_presence("away", "", ""), NOW),
        (f"presence.ask.dev.{A}", record, NOW),
    ]
    for i, (subject, data, at) in enumerate(rows, 1):
        view.fleet.on_delivery(subject, data, ack("PRESENCE", "c", i, at),
                               "PRESENCE", "c")
    lines = [LINE, LINE | {"sid": "bbbbbbbbbbbb", "rt": "opencode"}]
    for i, line in enumerate(lines, 1):
        body = stats.nats_body(line)
        assert body is not None
        assert view.stats.on_delivery(f"stats.{A}", body, ack("STATS", "c", i, NOW),
                                      "STATS", "c")
    # one STATS line outside the rules: dropped whole, counted (step 8)
    assert not view.stats.on_delivery(f"stats.{A}", b"{not a line", ack("STATS", "c", 9, NOW),
                                      "STATS", "c")
    # one presence record outside format v1: dropped whole, counted (step 6)
    assert not view.fleet.on_delivery(f"presence.ask.dev.{A}",
                                      record[:4] + b"nope", ack("PRESENCE", "c", 10, NOW),
                                      "PRESENCE", "c")
    return view


def test_the_agent_numbers_are_the_own_reading_and_no_name_is_a_label() -> None:
    """#158's agent metrics: the tab's numbers from the session's own
    consumers — agents by state and harness (an unknown harness counts as
    other), the open asks, and the tokens by category as stats.split_summary
    computes them for the same 7-day group; no agent name in the page."""
    view = _reading()
    text = telemetry.render(telemetry.Telemetry(), 1, 0, [], view)
    families, series = page(text)
    assert families == set(CONSOLE_FAMILIES) | set(AGENT_FAMILIES)
    assert A not in text and "agent-b" not in text
    assert series["bus_asks_open"] == 1.0
    assert series['bus_agents{state="online",harness="opencode"}'] == 1.0
    assert series['bus_agents{state="away",harness="other"}'] == 1.0
    want: dict[str, float] = {}
    groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for _at, _seq, line in view.stats.within(7 * 86400):
        groups.setdefault((str(line["v"]), str(line["rt"])), []).append(line)
    for (version, harness), group in sorted(groups.items()):
        split = stats.split_summary(group)
        for category in stats.SPLIT_CATEGORIES:
            want[f'bus_tokens_eq{{category="{category}",version="{version}",'
                 f'harness="{harness}"}}'] = float(split["eq"][category])
    assert {k: v for k, v in series.items() if k.startswith("bus_tokens_eq")} == want
    # the refusals: one STATS line, one record, both counted where they dropped
    assert series["console_stats_lines_refused_total"] == 1.0
    assert series["console_fleet_records_refused_total"] == 1.0


def test_the_refusals_start_at_zero() -> None:
    """Every counter exists from the start (0), an empty reading shows the
    zero and no agent series at all."""
    view = View("presence", "stats")
    _families, series = page(telemetry.render(telemetry.Telemetry(), 0, 0, [], view))
    assert series["console_stats_lines_refused_total"] == 0.0
    assert series["console_fleet_records_refused_total"] == 0.0
    assert series["console_bus_connects_total"] == 0.0
