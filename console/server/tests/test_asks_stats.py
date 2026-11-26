"""The console's asks and reports (RT-6.43 steps 6 and 8; ADR-015, addition of 2026-11-25,
points 8 to 15), each fed with what an MCP server really publishes — its own encoders
(`natsbus.encode_ask_record`, `stats.nats_body`) — and synthetic JetStream ack metadata.
"""
from __future__ import annotations

import json
import logging

import pytest

from mattermost_ai_agent_bus import stats
from mattermost_ai_agent_bus.natsbus import OpenAsk, agent_name, encode_ask_record, encode_presence

from console_server.fleet import ASK_EXPIRED_SEC, Fleet
from console_server.stats_view import StatsBook

NOW = 1764061800.0
NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
A = agent_name("dembaca", NKEY)
B = agent_name("carol", NKEY)
# The login service's STATS consumer of one login (`consumers.PersonConsumerNames`:
# "<person>_<login>_stats", the login its public key in lower case).
STATS_CONSUMER = "person-dembaca_u" + "a" * 55 + "_stats"


def ack(stream: str, consumer: str, seq: int, ts: float) -> str:
    return f"$JS.ACK.{stream}.{consumer}.{seq}.{seq}.{seq}.{int(ts * 1e9)}.0"


def mm(i: int) -> str:
    return ("m" * 25) + "abcdefghij"[i]


def attestation(agent: str, owner: tuple[str, str], trusted: list[tuple[str, str]]) -> bytes:
    return json.dumps({"v": 1, "agent": agent, "owner": {"anchor": owner[0], "name": owner[1]},
                       "trusted": [{"anchor": a, "name": n} for a, n in trusted],
                       "cut": False, "iat": 1, "exp": int(NOW + 900)}).encode()


def fleet_with(*records: tuple[str, str, bytes, float]) -> Fleet:
    """A fleet with A (owner dembaca, trusts carol under the anchor carol~x) and B (owner
    carol, anchor carol.x), both fresh, and the given ask records."""
    f = Fleet()
    rows = [
        (f"presence.dembaca.{A}", encode_presence("online", "", "agent-a"), NOW),
        (f"presence.carol~x.{B}", encode_presence("online", "", "agent-b"), NOW),
        (f"presence.attest.{A}.v1", attestation(A, ("dembaca", "dembaca"), [("carol.x", "carol")]), NOW),
        (f"presence.attest.{B}.v1", attestation(B, ("carol.x", "carol"), []), NOW),
        *[(f"presence.ask.{home}.{agent}", body, at) for agent, home, body, at in records],
    ]
    for i, (subject, data, at) in enumerate(rows, 1):
        f.on_delivery(subject, data, ack("PRESENCE", "console", i, at), "PRESENCE", "console")
    return f


def test_the_asks_list_marks_nothing_without_the_operators_own_anchor() -> None:
    """RT-6.43 step 6: without the operator's own credential there is no operator anchor, and
    none is derived from the login name (point 4): "for me" and "may answer" are n/a."""
    rec_a = encode_ask_record([
        OpenAsk(mm(1), "approval", "carol~x", "Merge PR 118?", int(NOW) - 60),
        OpenAsk(mm(2), "question", "dembaca", "Which\u202e branch?\x07", int(NOW) - 30, answered=True),
    ], int(NOW))
    rec_b = encode_ask_record([OpenAsk(mm(3), "approval", "-", "Deploy?", int(NOW) - 10)], int(NOW))
    f = fleet_with((A, "dev", rec_a, NOW), (B, "ops", rec_b, NOW))
    rows = f.asks()
    assert [r["id"] for r in rows] == [mm(1), mm(2), mm(3)]  # oldest first
    assert rows[0] == {"id": mm(1), "by": A, "display_name": "agent-a", "home": "dev", "kind": "approval",
                       "human": "carol", "need": "Merge PR 118?", "at": (int(NOW) - 60) * 1000,
                       "end": "open", "for_me": None, "may_answer": None, "agent_gone": False,
                       "thread_id": mm(1)}
    assert rows[1]["end"] == "answered" and rows[1]["need"] == "Which branch?"
    assert rows[1]["human"] == "dembaca" and rows[2]["human"] is None
    assert all(r["for_me"] is None and r["may_answer"] is None for r in rows)


def test_with_the_operators_anchor_for_me_and_may_answer_compare_anchors() -> None:
    """RT-6.43 step 6, once the operator's own credential gives the anchor: "for me" compares
    the asked human's anchor with it, "may answer" when the asking agent's attestation names it."""
    rec_a = encode_ask_record([OpenAsk(mm(1), "approval", "carol~x", "a", int(NOW) - 60),
                               OpenAsk(mm(2), "approval", "dembaca", "b", int(NOW) - 30)], int(NOW))
    rec_b = encode_ask_record([OpenAsk(mm(3), "approval", "-", "c", int(NOW) - 10)], int(NOW))
    f = fleet_with((A, "dev", rec_a, NOW), (B, "ops", rec_b, NOW))
    carol = {r["id"]: (r["for_me"], r["may_answer"]) for r in f.asks("carol~x")}
    assert carol == {mm(1): (True, True), mm(2): (False, True), mm(3): (False, True)}
    assert [r["id"] for r in f.asks("carol~x")][0] == mm(1)  # for me first
    # a login name is no anchor: "carol" matches nothing
    assert not any(r["for_me"] or r["may_answer"] for r in f.asks("carol"))


def test_a_record_runs_out_and_the_newest_wins() -> None:
    """RT-6.43 step 6: shown while `exp` is ahead of the floored clock; a newer record replaces
    the older, an empty one ends the asks."""
    old = encode_ask_record([OpenAsk(mm(1), "approval", "-", "a", int(NOW))], int(NOW) - 1800)
    f = fleet_with((A, "dev", old, NOW))
    assert f.asks() == []  # iat + 30 min is now: run out
    live = encode_ask_record([OpenAsk(mm(1), "approval", "-", "a", int(NOW))], int(NOW))
    f = fleet_with((A, "dev", live, NOW))
    assert len(f.asks()) == 1
    f.on_delivery(f"presence.ask.dev.{A}", encode_ask_record([], int(NOW)),
                  ack("PRESENCE", "console", 99, NOW + 1), "PRESENCE", "console")
    assert f.asks() == []


def test_a_consumer_made_anew_forgets_the_held_asks() -> None:
    """RT-6.43 step 6: a presence consumer made anew may no longer cover a channel the
    person lost; its asks do not linger, only what the new consumer replays is shown."""
    rec_a = encode_ask_record([OpenAsk(mm(1), "approval", "-", "a", int(NOW))], int(NOW))
    rec_b = encode_ask_record([OpenAsk(mm(3), "approval", "-", "c", int(NOW))], int(NOW))
    f = fleet_with((A, "dev", rec_a, NOW), (B, "ops", rec_b, NOW))
    assert sorted(r["id"] for r in f.asks()) == [mm(1), mm(3)]
    # the new consumer starts its count again at 1 and replays only "ops"
    anew = f"$JS.ACK.PRESENCE.console.1.10.1.{int((NOW + 1) * 1e9)}.0"
    f.on_delivery(f"presence.ask.ops.{B}", rec_b, anew, "PRESENCE", "console")
    assert [r["id"] for r in f.asks()] == [mm(3)]


def test_expired_after_24_hours_and_agent_gone() -> None:
    """RT-6.43 step 6: an ask older than 24 h is marked expired (a hint); the asks of an agent
    without fresh presence are marked "agent gone"."""
    rec = encode_ask_record([OpenAsk(mm(1), "approval", "-", "a", int(NOW) - ASK_EXPIRED_SEC)], int(NOW))
    f = fleet_with((A, "dev", rec, NOW))
    assert f.asks()[0]["end"] == "expired"
    f2 = Fleet()
    f2.on_delivery(f"presence.ask.dev.{A}", rec, ack("PRESENCE", "console", 1, NOW), "PRESENCE", "console")
    assert f2.asks()[0]["agent_gone"] is True


@pytest.mark.parametrize("body", [
    b'{"v":1,"asks":[],"more":0,"iat":1,"exp":2,"props":{}}',
    b'{"v":1,"asks":[{"mm":"x","kind":"approval","human":"-","title":"t","asked":1,"answered":false}],'
    b'"more":0,"iat":1,"exp":9999999999}',
    b'{"v":1,"asks":[],"more":0,"more":0,"iat":1,"exp":9999999999}',
    b"x" * 5000,
])
def test_a_bad_record_is_dropped_whole_with_a_warning_that_carries_none_of_it(body, caplog) -> None:
    """RT-6.43 step 6: dropped whole; the warning names the agent, never the content."""
    f = Fleet()
    with caplog.at_level(logging.WARNING):
        assert not f.on_delivery(f"presence.ask.dev.{A}", body, ack("PRESENCE", "console", 1, NOW),
                                 "PRESENCE", "console")
    assert f.asks() == []
    assert "dropped" in caplog.text and "props" not in caplog.text and "xxxx" not in caplog.text


# --- STATS (RT-6.43 steps 7 and 8) --------------------------------------------------------

LINE = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 1, "rt": "claude-code", "kind": "session",
        "min": 600, "turns": 10, "asks": 3, "asks_answered": 2, "ask_wait_sec": 300,
        "ask_wait_max_sec": 200, "asks_open": 1, "ask_open_sec": 60, "ask_open_max_sec": 60,
        "tok": {"req": 10, "bus_req": 2, "fixed": 100, "all": {"in": 1000, "out": 100},
                "bus": {"in": 100, "out": 10}}}


def book_with(*lines: tuple[str, dict, float]) -> StatsBook:
    book = StatsBook()
    for i, (agent, line, at) in enumerate(lines, 1):
        body = stats.nats_body(line)
        assert body is not None
        assert book.on_delivery(f"stats.{agent}", body, ack("STATS", STATS_CONSUMER, i, at),
                                "STATS", STATS_CONSUMER)
    return book


def fields(report: dict) -> dict:
    return {f["label"]: f["value"] for f in report["fields"]}


def test_the_two_reports_are_stats_reports_numbers() -> None:
    """RT-6.43 step 8: the share per version over 7 days and the asks over 24 h and 7 days,
    computed with stats.py; the same numbers stats_report gives on the same lines."""
    older = LINE | {"sid": "aaaaaaaaaaaa", "v": "0.100.0", "asks": 5, "asks_answered": 5,
                    "ask_wait_sec": 50, "ask_wait_max_sec": 20}
    book = book_with((A, older, NOW - 3 * 86400), (B, LINE, NOW - 600))
    share, day, week, _traffic = book.reports()
    assert (share["window"], day["window"], week["window"]) == ("7 d", "24 h", "7 d")
    lines = [stats.decode_line(stats.nats_body(older) or b""), stats.decode_line(stats.nats_body(LINE) or b"")]
    by_version = {row["version"]: row["tok"]["bus_share_pct"] for row in stats.report(lines)}
    got = fields(share)
    assert got["version 0.100.0"] == by_version["0.100.0"] and got["version 0.101.0"] == by_version["0.101.0"]
    assert got["lines"] == 2 and got["age of the oldest line"] == 3 * 86400 - 600  # by the newest stamp
    assert fields(day)["asks"] == 3 and fields(day)["answered share"] == 67
    want = stats.asks_summary(lines) or {}
    assert fields(week)["asks"] == want["asks"] == 8
    assert fields(week)["mean wait for an answer"] == want["mean_wait_sec"]
    assert fields(week)["longest wait for an answer"] == want["max_wait_sec"]
    # nothing in the window: n/a, never 0
    empty = StatsBook().reports()
    assert all(v is None for r in empty for v in fields(r).values())
    # no agent's name anywhere in the reports
    assert A not in json.dumps(book.reports()) and B not in json.dumps(book.reports())


def test_the_traffic_report_is_stats_reports_numbers() -> None:
    """RT-6.9 step 3, RT-6.43 step 8: the bus traffic by category over 7 days equals what
    stats_report gives on the same strictly decoded lines; a line without the counts (an
    older publisher) counts in the coverage, never as 0; without any such line: n/a."""
    traffic = {"post_words": {"HELLO": [1, 40], "DONE": [2, 200], "plain": [1, 30], "evil": [9, 9]},
               "inbound": {"agent": [2, 500], "event": [1, 20], "envelope": [3, 600]},
               "polls": [4, 3], "inject": {"drain": [1, 150]}}
    new = LINE | {"sid": "bbbbbbbbbbbb", "v": "0.102.0", **traffic}
    book = book_with((A, LINE, NOW - 3 * 86400), (B, new, NOW - 600))
    report = next(r for r in book.reports() if r["id"] == "traffic-by-category")
    lines = [stats.decode_line(stats.nats_body(LINE) or b""), stats.decode_line(stats.nats_body(new) or b"")]
    want = stats.traffic_summary(lines) or {}
    assert sum(row["traffic"] is not None for row in stats.report(lines)) == 1
    got = fields(report)
    assert report["window"] == "7 d" and report["title"] == "bus traffic by category"
    assert (got["bus overhead, items"], got["bus overhead, characters"]) == tuple(want["overhead"]) == (1 + 1 + 1 + 3, 40 + 20 + 600 + 150)
    assert (got["coordination, items"], got["coordination, characters"]) == tuple(want["coordination"]) == (5, 730)
    assert got["bus overhead share of characters"] == want["overhead_chars_pct"]
    assert (got["polls"], got["empty polls share"]) == (4, 75)
    assert (got["lines with these counts"], got["lines"]) == (1, 2)
    old = next(r for r in book_with((A, LINE, NOW)).reports() if r["id"] == "traffic-by-category")
    assert fields(old)["lines"] == 1 and fields(old)["bus overhead, items"] is None


def test_a_line_with_text_is_dropped_and_old_lines_leave(caplog) -> None:
    """RT-6.43 step 8: a string outside the four fields drops the whole line, with a warning
    that carries none of it; a line older than 7 days leaves the book."""
    book = book_with((A, LINE, NOW - 8 * 86400))
    bad = json.dumps(LINE | {"turns": "see https://evil.example"}).encode()
    with caplog.at_level(logging.WARNING):
        assert not book.on_delivery(f"stats.{A}", bad, ack("STATS", STATS_CONSUMER, 2, NOW),
                                    "STATS", STATS_CONSUMER)
    assert "dropped" in caplog.text and "evil" not in caplog.text
    assert book.on_delivery(f"stats.{A}", stats.nats_body(LINE) or b"",
                            ack("STATS", STATS_CONSUMER, 3, NOW), "STATS", STATS_CONSUMER)
    assert fields(book.reports()[0])["lines"] == 1  # the 8-day-old line is gone
    # a subject that is not <stats>.<agent>, or a delivery of another consumer: nothing
    assert not book.on_delivery("stats.x.y", stats.nats_body(LINE) or b"",
                                ack("STATS", STATS_CONSUMER, 4, NOW), "STATS", STATS_CONSUMER)
    assert not book.on_delivery(f"stats.{A}", stats.nats_body(LINE) or b"",
                                ack("STATS", "other", 5, NOW), "STATS", STATS_CONSUMER)


def test_for_me_needs_the_anchor_among_the_asking_agents_attested_names() -> None:
    """RT-6.43 step 6: an agent cannot make an ask "for me" by naming an anchor its
    attestation does not hold."""
    rec_b = encode_ask_record([OpenAsk(mm(4), "approval", "dembaca", "d", int(NOW) - 5)], int(NOW))
    f = fleet_with((B, "ops", rec_b, NOW))
    assert f.asks("dembaca")[0]["for_me"] is False


def test_a_record_lives_at_most_30_minutes_after_the_server_stored_it() -> None:
    """The agent's own clock is not trusted: a record past ack time + 30 min is gone, and one
    whose exp lies further than 30 min after iat is dropped by the decoder."""
    rec = encode_ask_record([OpenAsk(mm(1), "approval", "-", "a", int(NOW))], int(NOW) + 10**6)
    f = fleet_with((A, "dev", rec, NOW))
    assert len(f.asks()) == 1  # stored now: shown
    f._see(NOW + 1801)
    assert f.asks() == []
    far = json.loads(rec)
    far["exp"] = far["iat"] + 10**9
    assert not Fleet().on_delivery(f"presence.ask.dev.{A}", json.dumps(far).encode(),
                                   ack("PRESENCE", "console", 1, NOW), "PRESENCE", "console")


def test_a_replay_after_a_reconnect_counts_each_line_once() -> None:
    """A STATS consumer made anew replays 7 days: the stream sequence counts a line once."""
    book = StatsBook()
    body = stats.nats_body(LINE) or b""
    for _replay in range(3):
        book.on_delivery(f"stats.{A}", body, ack("STATS", STATS_CONSUMER, 7, NOW), "STATS", STATS_CONSUMER)
    assert fields(book.reports()[0])["lines"] == 1 and fields(book.reports()[1])["asks"] == 3


def test_a_report_that_cannot_be_computed_says_n_a(monkeypatch) -> None:
    book = book_with((A, LINE, NOW))

    def boom(_lines: list) -> None:
        raise OverflowError
    monkeypatch.setattr(stats, "report", boom)
    monkeypatch.setattr(stats, "asks_summary", boom)
    reports = book.reports()
    assert fields(reports[1])["asks"] is None and len(reports[0]["fields"]) == 2


def test_a_share_is_held_to_0_to_100(monkeypatch) -> None:
    """An agent's numbers are its own word: a share outside 0 to 100 is shown at the bound."""
    book = book_with((A, LINE, NOW))
    monkeypatch.setattr(stats, "report", lambda lines: [{"version": "0.101.0", "tok": {"bus_share_pct": 1.3e11}}])
    monkeypatch.setattr(stats, "asks_summary", lambda lines: {"answered_pct": -7})
    reports = book.reports()
    assert fields(reports[0])["version 0.101.0"] == 100 and fields(reports[1])["answered share"] == 0
