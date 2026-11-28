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
C = agent_name("dave", NKEY)
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


CARD = {"context": "Two PRs touch the same file.\nMine is smaller.", "recommendation": "Merge mine first.",
        "where": "PR 118", "decision": "Merge PR 118 into main", "answers": ("go", "no-go"),
        "chosen": "go"}
SECRET_WORDS = ("Two PRs touch", "Merge mine first", "PR 118", "Merge PR 118 into main", "no-go")


def _card_fleet() -> Fleet:
    """A (owner dembaca, trusts carol as carol~x) asks dembaca with a full card; B (owner
    carol, trusts nobody) asks eve with one."""
    rec_a = encode_ask_record([OpenAsk(mm(1), "approval", "dembaca", "Merge?", int(NOW) - 60,
                                       answered=True, **CARD)], int(NOW))
    rec_b = encode_ask_record([OpenAsk(mm(2), "question", "eve", "Which way?", int(NOW) - 30,
                                       context="eve's context", answers=("A", "B", "C"))], int(NOW))
    return fleet_with((A, "dev", rec_a, NOW), (B, "ops", rec_b, NOW))


def test_the_asked_human_and_a_trusted_person_get_the_card() -> None:
    """The owner's decision of 2026-11-28 (ADR-015): the card goes to the asked human and to
    the people the asking agent trusts, as plain text, its answers in order."""
    f = _card_fleet()
    mine = {r["id"]: r for r in f.asks("dembaca")}[mm(1)]
    assert mine["for_me"] is True and mine["card"] == {
        "context": "Two PRs touch the same file.\nMine is smaller.", "recommendation": "Merge mine first.",
        "where": "PR 118", "decision": "Merge PR 118 into main", "answers": ["go", "no-go"],
        "chosen": "go"}
    trusted = {r["id"]: r for r in f.asks("carol~x")}
    assert trusted[mm(1)]["for_me"] is False and trusted[mm(1)]["may_answer"] is True
    assert trusted[mm(1)]["card"] == mine["card"]
    # B is carol's own agent: she may answer its ask to eve, so she reads its card too
    assert trusted[mm(2)]["card"]["answers"] == ["A", "B", "C"] and trusted[mm(2)]["card"]["chosen"] is None
    assert trusted[mm(2)]["card"]["decision"] == "" and trusted[mm(2)]["card"]["where"] == ""


def test_a_reader_neither_asked_nor_trusted_gets_the_title_only() -> None:
    """Fail closed: a reader the asking agent's attestation does not name gets the title and
    no `card` key at all, and so does the asked human of an agent that does not trust them,
    and every reader before the login's own record gives the operator's anchor."""
    f = _card_fleet()
    for reader in ("mallory", "dembaca~other", None):
        rows = f.asks(reader)
        assert [r["need"] for r in rows] == ["Merge?", "Which way?"]
        assert not any("card" in r for r in rows), reader
        assert not any(w in json.dumps(rows) for w in SECRET_WORDS + ("eve's context",)), reader
    # eve is B's asked human, but B's attestation names only carol: title only for eve
    eve = {r["id"]: r for r in f.asks("eve")}
    assert eve[mm(2)]["for_me"] is False and "card" not in eve[mm(2)]
    # dembaca owns A, not B: B's card stays with B's people
    assert "card" not in {r["id"]: r for r in f.asks("dembaca")}[mm(2)]


def test_the_card_reaches_no_other_read() -> None:
    """The card is on /v1/asks alone: the cards, the graph and the groups with the glance
    carry no card part for anyone, even for the asked human (the event stream sends cards,
    `/v1/threads` no posts yet)."""
    from console_server import groups

    f = _card_fleet()
    for reader in ("dembaca", "carol~x", None):
        cards = f.cards(reader)
        out = json.dumps([cards, f.graph(reader),
                          groups.groups(cards, f.asks(reader), [], by="home", scope="everyone",
                                        own={A}, own_or_trusted={A, B})])
        assert not any(w in out for w in SECRET_WORDS + ("eve's context",)), reader
        assert '"card"' not in out and '"context"' not in out


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
        "ask_wait_max_sec": 200, "asksOpen": 1, "ask_open_sec": 60, "ask_open_max_sec": 60,
        "tok": {"req": 10, "bus_req": 2, "fixed": 100, "all": {"in": 1000, "out": 100},
                "bus": {"in": 100, "out": 10}}}


# The token split of a Claude Code line (RT-6.9 step 2): overhead and coordination by
# token type against tok.all of LINE, the method's number.
SPLIT = {"ovh": {"in": 300, "out": 30}, "coord": {"in": 200, "out": 20}, "req_ovh": 1, "req_coord": 2, "m": 1}


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
                    "ask_wait_sec": 50, "ask_wait_max_sec": 20, "tok": LINE["tok"] | SPLIT}
    book = book_with((A, older, NOW - 3 * 86400), (B, LINE, NOW - 600))
    _activity, share, week, before, _traffic, _split = book.reports()
    _activity, _share, day, _before, _t, _s = book.reports("24h")
    assert (share["window"], day["window"], week["window"], before["window"]) == (
        "7 d", "24 h", "7 d", "the 7 d before")
    assert fields(before)["asks"] is None
    lines = [stats.decode_line(stats.nats_body(older) or b""), stats.decode_line(stats.nats_body(LINE) or b"")]
    # The overhead segment of the token split; a version without it n/a, never 0.
    by_version = {row["version"]: (row["split"] or {}).get("eq_pct", {}).get("overhead")
                  for row in stats.report(lines)}
    got = fields(share)
    assert got["version 0.100.0"] == by_version["0.100.0"] == 30 and got["version 0.101.0"] is None
    assert share["title"] == "bus overhead share of all tokens, per version" and "estimated" in share["note"]
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


def test_the_tokens_by_category_are_stats_reports_numbers() -> None:
    """RT-6.9 step 3, RT-6.43 step 8: the tokens by category over 7 days, one stacked bar
    for the fleet, per version, per role and per harness, each the split stats_report gives
    on the same strictly decoded lines, marked estimated, in input-token equivalents and
    raw tokens; a line without the split counts in the coverage only, never as 0."""
    new = LINE | {"sid": "bbbbbbbbbbbb", "v": "0.102.0", "crown_sec": 0, "tok": LINE["tok"] | SPLIT}
    other = LINE | {"sid": "cccccccccccc", "v": "0.102.0", "rt": "opencode"}
    del other["tok"]
    book = book_with((A, LINE, NOW - 3 * 86400), (B, new, NOW - 600), (B, other, NOW - 500))
    report = next(r for r in book.reports() if r["id"] == "tokens-by-category")
    lines = [stats.decode_line(stats.nats_body(x) or b"") for x in (LINE, new, other)]
    want = stats.split_summary(lines) or {}
    rows = {row["version"]: row["split"] for row in stats.report(lines)}
    bars = {s["label"]: s for s in report["stacks"]}
    assert report["window"] == "7 d" and "estimated" in report["note"] and "from 1 of 3 lines (claude-code)" in report["note"]
    assert set(bars) == {"flock", "version 0.102.0", "role worker", "harness claude-code"}
    assert all(s["estimated"] is True for s in report["stacks"])
    for label, split in (("flock", want), ("version 0.102.0", rows["0.102.0"])):
        eq = {seg["category"]: (seg["value"], seg["percent"]) for seg in bars[label]["segments"]}
        raw = {seg["category"]: (seg["value"], seg["percent"]) for seg in bars[label]["raw"]}
        assert eq == {c: (split["eq"][c], split["eq_pct"][c]) for c in stats.SPLIT_CATEGORIES}
        assert raw == {c: (split["raw"][c], split["raw_pct"][c]) for c in stats.SPLIT_CATEGORIES}
    assert raw == {"task": (550, 50), "overhead": (330, 30), "coordination": (220, 20)}
    got = fields(report)
    assert (got["lines with the split"], got["lines"]) == (1, 3)
    assert (got["requests driven by the bus alone"], got["requests driven by coordination"]) == (1, 2)
    old = next(r for r in book_with((A, LINE, NOW)).reports() if r["id"] == "tokens-by-category")
    assert old["stacks"] == [] and fields(old)["lines with the split"] is None


def test_forged_split_values_stay_within_0_to_100() -> None:
    """RT-6.9 step 3: a strictly decoded line whose overhead and coordination exceed its
    totals (an agent's numbers are its own word) still shows shares within 0 to 100 %,
    overhead held to the total, coordination to the rest, task work never below 0."""
    forged = LINE | {"tok": LINE["tok"] | {"ovh": {"in": 10**12, "out": 5}, "coord": {"in": 10**12, "out": 10**12},
                                           "m": 1}}
    reports = book_with((A, forged, NOW)).reports()
    bar = next(r for r in reports if r["id"] == "tokens-by-category")["stacks"][0]
    for segs in (bar["segments"], bar["raw"]):
        assert all(0 <= s["percent"] <= 100 and s["value"] >= 0 for s in segs)
    raw = {s["category"]: s["value"] for s in bar["raw"]}
    assert raw == {"task": 0, "overhead": 1000 + 5, "coordination": 95}
    assert 0 <= fields(reports[1])["version 0.101.0"] <= 100


def test_an_imported_line_stays_out_of_the_reports(caplog) -> None:
    """RT-6.45 step 6: the login service's import publishes the stats channel's old lines on
    the three-token <stats>.import.<bot>; the console's STATS consumers deliver it, and the
    book leaves it out of every report, silently, however fresh its store time — also a line
    of today's format and one from before 0.91.0 (no sid, no part)."""
    book = book_with((A, LINE, NOW - 600))
    before = json.dumps(book.reports())
    old_format = json.dumps({"v": "0.85.0", "min": 60, "turns": 3}).encode()
    with caplog.at_level(logging.WARNING):
        for seq, body in ((2, stats.nats_body(LINE) or b""), (3, old_format)):
            assert not book.on_delivery("stats.import.agent-mac-ai-agent-1", body,
                                        ack("STATS", STATS_CONSUMER, seq, NOW), "STATS", STATS_CONSUMER)
    assert json.dumps(book.reports()) == before and fields(book.reports()[1])["lines"] == 1
    assert caplog.text == ""


def test_a_line_with_text_is_dropped_and_old_lines_leave(caplog) -> None:
    """RT-6.43 step 8: a string outside the four fields drops the whole line, with a warning
    that carries none of it; a line older than 365 days leaves the book."""
    book = book_with((A, LINE, NOW - 366 * 86400))
    bad = json.dumps(LINE | {"turns": "see https://evil.example"}).encode()
    with caplog.at_level(logging.WARNING):
        assert not book.on_delivery(f"stats.{A}", bad, ack("STATS", STATS_CONSUMER, 2, NOW),
                                    "STATS", STATS_CONSUMER)
    assert "dropped" in caplog.text and "evil" not in caplog.text
    assert book.on_delivery(f"stats.{A}", stats.nats_body(LINE) or b"",
                            ack("STATS", STATS_CONSUMER, 3, NOW), "STATS", STATS_CONSUMER)
    assert fields(book.reports()[1])["lines"] == 1  # the 366-day-old line is gone
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
    assert fields(book.reports()[1])["lines"] == 1 and fields(book.reports()[2])["asks"] == 3


def test_a_report_that_cannot_be_computed_says_n_a(monkeypatch) -> None:
    book = book_with((A, LINE, NOW))

    def boom(_lines: list) -> None:
        raise OverflowError
    monkeypatch.setattr(stats, "report", boom)
    monkeypatch.setattr(stats, "asks_summary", boom)
    reports = book.reports()
    assert fields(reports[2])["asks"] is None and len(reports[1]["fields"]) == 2


def test_a_share_is_held_to_0_to_100(monkeypatch) -> None:
    """An agent's numbers are its own word: a share outside 0 to 100 is shown at the bound."""
    book = book_with((A, LINE, NOW))
    monkeypatch.setattr(stats, "report",
                        lambda lines: [{"version": "0.101.0", "split": {"eq_pct": {"overhead": 1.3e11}}}])
    monkeypatch.setattr(stats, "asks_summary", lambda lines: {"answered_pct": -7})
    reports = book.reports()
    assert fields(reports[1])["version 0.101.0"] == 100 and fields(reports[2])["answered share"] == 0


def test_a_line_with_the_plugins_split_counts_in_the_tokens_by_category() -> None:
    """RT-6.9 step 3, RT-6.43 step 8: an OpenCode or pi line whose tok came from the plugin's
    usage events (the split, no tok.bus) counts in the tokens by category and the overhead
    share per version like a Claude Code line; the bus share by cause leaves it out."""
    usage = LINE | {"sid": "dddddddddddd", "v": "0.103.0", "rt": "opencode", "crown_sec": 0,
                    "tok": {"req": 3, "req_ovh": 1, "req_coord": 1, "m": 1,
                            "all": {"in": 1000, "out": 100}, "ovh": {"in": 300, "out": 30},
                            "coord": {"in": 200, "out": 20}}}
    book = book_with((A, usage, NOW - 600))
    lines = [stats.decode_line(stats.nats_body(usage) or b"")]
    assert "bus" not in lines[0]["tok"]
    row = stats.report(lines)[0]
    assert row["tok"] is None and row["split"]["harnesses"] == ["opencode"]
    share = next(r for r in book.reports() if r["id"] == "share-per-version")
    assert fields(share)["version 0.103.0"] == row["split"]["eq_pct"]["overhead"] == 30
    report = next(r for r in book.reports() if r["id"] == "tokens-by-category")
    bars = {s["label"]: s for s in report["stacks"]}
    assert "harness opencode" in bars and fields(report)["lines with the split"] == 1
    assert {seg["category"]: seg["value"] for seg in bars["flock"]["raw"]} == {
        "task": 550, "overhead": 330, "coordination": 220}


def test_agent_rows_sums_each_window_per_agent() -> None:
    """/v1/agent-stats (#201): one row per agent, the 24-hour and the 7-day token sums
    of its parts (all and by type, the input's overhead and coordination), and the
    posts, wakes and tool calls of the last 24 hours; the parts of other agents never
    mix in."""
    today = LINE | {"sid": "aaaaaaaaaaaa", "v": "0.102.0", "min": 30, "posts": [4, 500],
                    "wakes": {"addressed": 2, "digest": 1}, "tools": {"ask_human": [3, 900], "get_thread": [1, 200]},
                    "tok": {"req": 5, "all": {"in": 100, "read": 700, "out": 40},
                            "ovh": {"in": 30, "read": 100}, "coord": {"in": 20, "read": 50}}}
    yesterday = LINE | {"sid": "bbbbbbbbbbbb", "posts": [2, 100], "turns": 1,
                        "tok": {"req": 1, "all": {"in": 10, "out": 5}, "ovh": {"in": 2}, "coord": {"in": 1}}}
    other = LINE | {"sid": "cccccccccccc", "rt": "opencode", "posts": [9, 90], "min": 45,
                    "tok": {"req": 9, "all": {"read": 5000}, "ovh": {"read": 500}, "coord": {"read": 400}}}
    book = book_with((A, yesterday, NOW - 2 * 86400), (B, other, NOW - 3 * 86400),
                     (A, today, NOW - 600))
    assert book.agent_rows({A, B}) == [
        {"agent": B,
         "tok24h": None, "tok7d": {"all": 5000, "in": None, "read": 5000, "w5m": None, "w1h": None,
                                   "out": None, "ovh": 500, "coord": 400},
         "posts24h": None, "wakes24h": None, "tools24h": None},
        {"agent": A,
         "tok24h": {"all": 840, "in": 100, "read": 700, "w5m": None, "w1h": None, "out": 40,
                    "ovh": 130, "coord": 70},
         "tok7d": {"all": 855, "in": 110, "read": 700, "w5m": None, "w1h": None, "out": 45,
                   "ovh": 132, "coord": 71},
         "posts24h": 4, "wakes24h": 3, "tools24h": 4}]


def test_agent_rows_without_tok_is_na_never_zero() -> None:
    """/v1/agent-stats: the reports' rule (RT-6.43 step 8) — a field no part of the
    window carries is n/a (None), never 0, and so is a whole window without tok; a
    part outside the window counts nowhere."""
    plain = LINE | {"sid": "dddddddddddd", "posts": [1, 20]}
    del plain["tok"]
    book = book_with((A, plain, NOW - 600), (A, LINE, NOW - 3 * 86400))
    assert book.agent_rows({A}) == [{"agent": A, "tok24h": None,
                                  "tok7d": {"all": 1100, "in": 1000, "read": None, "w5m": None,
                                            "w1h": None, "out": 100, "ovh": None, "coord": None},
                                  "posts24h": 1, "wakes24h": None, "tools24h": None}]


def test_agent_rows_keeps_a_stated_zero_and_nulls_an_empty_object() -> None:
    """/v1/agent-stats, RT-6.43 step 8: a stated 0 stays 0 (the total and the overhead
    included); a type no part states is None, and so is an object stated empty (no
    number carried: n/a, never 0); a window whose parts carry `tok` without `all`, or
    with an empty `all`, gives None for the whole token row."""
    zero = LINE | {"sid": "eeeeeeeeeeee",
                   "tok": {"req": 0, "all": {"in": 0, "out": 0}, "ovh": {"in": 0}, "coord": {}}}
    no_all = LINE | {"sid": "ffffffffffff", "tok": {"req": 3}}
    empty_all = LINE | {"sid": "111111111111", "tok": {"req": 3, "all": {}, "ovh": {"in": 5}}}
    book = book_with((A, zero, NOW - 600), (B, no_all, NOW - 600), (C, empty_all, NOW - 600))
    rows = {row["agent"]: row for row in book.agent_rows({A, B, C})}
    assert rows[A]["tok24h"] == {"all": 0, "in": 0, "read": None, "w5m": None, "w1h": None,
                                 "out": 0, "ovh": 0, "coord": None}
    assert rows[B]["tok24h"] is None and rows[B]["tok7d"] is None
    assert rows[C]["tok24h"] is None and rows[C]["tok7d"] is None


def test_agent_rows_sum_overhead_over_the_parts_of_the_total() -> None:
    """/v1/agent-stats, RT-6.43 step 8: the overhead and the coordination are summed over
    the same parts as the total — a part without `all` adds to neither — and each part's
    are held to its own total, so a row never shows more overhead than tokens."""
    split = LINE | {"sid": "222222222222", "tok": {"req": 1, "all": {"in": 100}, "ovh": {"in": 30}}}
    no_all = LINE | {"sid": "333333333333", "tok": {"req": 1, "ovh": {"in": 900}, "coord": {"in": 900}}}
    forged = LINE | {"sid": "444444444444", "tok": {"req": 1, "all": {"in": 10}, "ovh": {"in": 500},
                                                    "coord": {"in": 7}}}
    book = book_with((A, split, NOW - 600), (A, no_all, NOW - 500), (A, forged, NOW - 400))
    row = book.agent_rows({A})[0]["tok24h"]
    assert (row["all"], row["ovh"], row["coord"]) == (110, 40, 7)


def test_agent_rows_only_of_own_or_trusted_agents() -> None:
    """ADR-015 point 15, RT-6.43 step 8: `/v1/agent-stats` has a row only for an agent whose
    fresh attestation names the operator's anchor as owner or trusted. Another owner's agent
    has none, nor has an agent without a known attestation, one whose attestation ran out,
    or any agent before the login's own record gave the operator's anchor."""
    C_ = agent_name("erin", NKEY)
    f = fleet_with()
    late = json.loads(attestation(C_, ("dembaca", "dembaca"), []))
    late["exp"] = int(NOW - 1)
    f.on_delivery(f"presence.attest.{C_}.v1", json.dumps(late).encode(),
                  ack("PRESENCE", "console", 9, NOW - 60), "PRESENCE", "console")
    book = book_with((A, LINE, NOW - 600), (B, LINE, NOW - 600), (C, LINE, NOW - 600),
                     (C_, LINE, NOW - 600))
    # dembaca: owner of A; B is carol's (and trusts no dembaca), C is not attested, C_'s ran out
    assert [r["agent"] for r in book.agent_rows(f.own_or_trusted("dembaca"))] == [A]
    # carol (anchor carol.x, in subject form carol~x): owner of B, trusted by A
    assert {r["agent"] for r in book.agent_rows(f.own_or_trusted("carol~x"))} == {A, B}
    # no anchor, a login name that is no anchor, the no-owner token: no row
    for anchor in (None, "carol", "-"):
        assert book.agent_rows(f.own_or_trusted(anchor)) == []
    # the aggregate reports stay over every agent
    assert fields(book.reports()[1])["lines"] == 4


def test_one_agent_row_that_cannot_be_computed_is_n_a(monkeypatch) -> None:
    """/v1/agent-stats: as the reports, one row that breaks the arithmetic says n/a and
    never fails the other rows or the endpoint."""
    import console_server.stats_view as view
    book = book_with((A, LINE, NOW - 600), (B, LINE, NOW - 600))
    real = view._tok

    def boom(lines: list) -> object:
        if any(line.get("sid") == "555555555555" for line in lines):
            raise OverflowError
        return real(lines)
    monkeypatch.setattr(view, "_tok", boom)
    book.on_delivery(f"stats.{B}", stats.nats_body(LINE | {"sid": "555555555555"}) or b"",
                     ack("STATS", STATS_CONSUMER, 9, NOW - 300), "STATS", STATS_CONSUMER)
    rows = {r["agent"]: r for r in book.agent_rows({A, B})}
    assert rows[A]["tok24h"] is not None
    assert rows[B] == {"agent": B, "tok24h": None, "tok7d": None,
                       "posts24h": None, "wakes24h": None, "tools24h": None}


# --- #283: the agents list's groups, counted on the server ---------------------------------

def _groups(f: Fleet, anchor: str | None, by: str, scope: str) -> dict:
    from console_server import groups
    return groups.groups(f.cards(anchor), f.asks(anchor), [], by=by, scope=scope,
                         own=f.own(anchor), own_or_trusted=f.own_or_trusted(anchor))


def test_groups_count_per_group_and_scope() -> None:
    """RT-6.43 step 11, #283: the state counts, the reachable agents and the asks for the operator come from the
    server; "Mine" is the agents the operator owns, "Trusting me" those that name it owner or
    trusted, "Everyone" all; the groups with an ask for the operator come first."""
    rec_a = encode_ask_record([OpenAsk(mm(1), "approval", "dembaca", "a", int(NOW) - 60)], int(NOW))
    f = fleet_with((A, "dev", rec_a, NOW))
    # carol's anchor: she owns B and A's attestation trusts her
    mine = _groups(f, "carol~x", "home", "mine")
    assert [g["agents"] for g in mine["groups"]] == [[B]]
    trusting = _groups(f, "carol~x", "home", "trusting")
    assert sorted(a for g in trusting["groups"] for a in g["agents"]) == sorted([A, B])
    everyone = _groups(f, "carol~x", "home", "everyone")
    assert everyone["glance"]["flock"]["total"] == 2
    assert everyone["glance"]["flock"]["states"]["listening"] == 2  # presence v1 "online"
    assert everyone["glance"]["mine"]["total"] == 1
    # the agent A asked carol's human? no: the ask is for "dembaca", so none is for her
    assert everyone["glance"]["flock"]["asksForYou"] == 0
    # dembaca: A is hers, B's attestation does not name her
    dem = _groups(f, "dembaca", "home", "everyone")
    assert dem["glance"]["mine"]["total"] == 1 and dem["glance"]["flock"]["asksForYou"] == 1
    by_owner = _groups(f, "dembaca", "owner", "everyone")["groups"]
    assert by_owner[0]["agents"] == [A] and by_owner[0]["asksForYou"] == 1  # carol sorts first by name


def test_each_scope_widens_the_one_before() -> None:
    """#283, the design contact's answer 1: "Mine" is the agents the operator owns, "Trusting
    me" those plus the others' agents that name the operator as trusted, "Everyone" all."""
    f = fleet_with()
    for anchor in ("carol~x", "dembaca"):
        seen = {s: {a for g in _groups(f, anchor, "none", s)["groups"] for a in g["agents"]}
                for s in ("mine", "trusting", "everyone")}
        assert seen["mine"] <= seen["trusting"] <= seen["everyone"]
        assert seen["mine"] == f.own(anchor) and seen["trusting"] == f.own_or_trusted(anchor)
    # carol owns B and A's attestation trusts her: A joins in "trusting", never in "mine"
    assert {a for g in _groups(f, "carol~x", "none", "trusting")["groups"] for a in g["agents"]} \
        - f.own("carol~x") == {A}
    # dembaca owns A; B's attestation does not name her: B is in "everyone" only
    dem = {s: {a for g in _groups(f, "dembaca", "none", s)["groups"] for a in g["agents"]}
           for s in ("trusting", "everyone")}
    assert B in dem["everyone"] and B not in dem["trusting"]


def test_the_panels_ask_tiles_count_over_the_whole_view() -> None:
    """#283, answer 2: "For you" is every open ask that asks the operator, "You may answer" the
    open asks to another human by agents that trust the operator, "open" all open asks in view;
    only the agent numbers are split into own agents and the flock."""
    rec_a = encode_ask_record([OpenAsk(mm(1), "approval", "dembaca", "a", int(NOW) - 60),
                               OpenAsk(mm(2), "question", "carol~x", "b", int(NOW) - 30)], int(NOW))
    rec_b = encode_ask_record([OpenAsk(mm(3), "question", "dembaca", "c", int(NOW) - 10)], int(NOW))
    f = fleet_with((A, "dev", rec_a, NOW), (B, "dev", rec_b, NOW))
    dem = _groups(f, "dembaca", "home", "mine")["glance"]
    # A's ask to dembaca is for her; its ask to carol is one she may answer (A is hers);
    # B's attestation does not name her, so its ask counts in "open" only
    assert dem["asks"] == {"forYou": 1, "mayAnswer": 1, "open": 3}
    carol = _groups(f, "carol~x", "home", "mine")["glance"]
    # carol: A is another owner's agent, and its ask to her is for her all the same; its ask
    # to dembaca she may answer (A trusts her); B is hers too, so its ask to dembaca she may answer
    assert carol["asks"] == {"forYou": 1, "mayAnswer": 2, "open": 3}
    # the panel's counts do not depend on the scope that is picked
    assert _groups(f, "dembaca", "home", "everyone")["glance"]["asks"] == dem["asks"]
    assert dem["mine"]["total"] == 1 and dem["flock"]["total"] == 2


def test_groups_without_an_anchor_count_nothing_as_mine_or_for_you() -> None:
    f = fleet_with()
    doc = _groups(f, None, "owner", "mine")
    assert doc["groups"] == [] and doc["glance"]["mine"]["total"] == 0
    assert _groups(f, None, "none", "everyone")["groups"][0]["label"] == "All agents"


def test_groups_sum_tokens_only_of_what_the_operator_may_see() -> None:
    """#283 and ADR-015 point 15: the sum covers the agents with a per-agent row, and says how
    many; n/a (None) when none states a token count, never 0."""
    from console_server import groups
    f = fleet_with()
    cards = f.cards("dembaca")
    rows = [{"agent": A, "tok24h": {"all": 1200}, "tok7d": None}]
    doc = groups.groups(cards, [], rows, by="none", scope="everyone",
                        own=f.own("dembaca"), own_or_trusted=f.own_or_trusted("dembaca"))
    group = doc["groups"][0]
    assert group["total"] == 2 and group["tokens"] == 1200 and group["tokensAgents"] == 1
    none = groups.groups(cards, [], [{"agent": A, "tok24h": None, "tok7d": None}], by="none",
                         scope="everyone", own=f.own("dembaca"),
                         own_or_trusted=f.own_or_trusted("dembaca"))
    assert none["groups"][0]["tokens"] is None and none["groups"][0]["tokensAgents"] == 0


def test_groups_by_status_use_the_status_groups_and_never_guess_an_unknown_state() -> None:
    from console_server import groups
    assert groups.status_of("bus") == "working" and groups.status_of("away") == "idle"
    assert groups.status_of("dreaming") == "other"

def test_the_hourly_buckets_change_no_report_and_old_lines_have_none(monkeypatch) -> None:
    """#290, RT-6.43 step 8: a line with `post_hours` gives every report the numbers the same
    line without it gives, in this console and in one whose allow list predates the key
    (it ignores the key like any unknown one); the hours of a new line reach the reader
    (`stats.hours_of`), an old line gives none: "no hourly data yet", never 0."""
    hour = int(NOW) // 3600
    words = {"post_words": {"ACK": [2, 40], "DONE": [1, 30]}}
    new = LINE | {"v": "0.104.0", **words, "post_hours": {str(hour - 1): {"ACK": 2}, str(hour): {"DONE": 1}}}
    without = {k: v for k, v in new.items() if k != "post_hours"}
    held = book_with((A, new, NOW - 60))
    got = stats.hours_of([line for _at, _seq, line in held.within(86400)])
    assert got == {"hours": {(hour - 1) * 3600: {"ACK": 2}, hour * 3600: {"DONE": 1}}, "lines": 1, "of": 1}
    assert held.reports() == book_with((A, without, NOW - 60)).reports()
    old = book_with((A, LINE | words, NOW - 60))
    assert stats.hours_of([line for _at, _seq, line in old.within(86400)]) is None
    body = stats.nats_body(new) or b""
    assert b'"post_hours"' in body
    want = stats.decode_line(stats.nats_body(without) or b"")
    monkeypatch.setattr(stats, "LINE_KEYS", {k: v for k, v in stats.LINE_KEYS.items() if k != "post_hours"})
    older_console = StatsBook()
    assert older_console.on_delivery(f"stats.{A}", body, ack("STATS", STATS_CONSUMER, 1, NOW - 60),
                                     "STATS", STATS_CONSUMER)
    assert [line for _a, _s, line in older_console.within(86400)] == [want]
    assert older_console.reports() == held.reports()


def test_the_time_range_applies_to_every_card_and_asks_show_the_range_before() -> None:
    """RT-6.43 step 8, #284: `reports(range)` computes every card over the range; the asks
    card shows the range and the range before it; 7 days is the default."""
    book = book_with((A, LINE, NOW - 40 * 86400), (B, LINE, NOW - 3 * 86400), (C, LINE, NOW - 600))
    assert [r["window"] for r in book.reports()] == ["7 d"] * 3 + ["the 7 d before"] + ["7 d"] * 2
    got = {k: book.reports(k) for k in ("24h", "7d", "30d", "90d", "12m")}
    assert [fields(r[1])["lines"] for r in got.values()] == [1, 2, 2, 3, 3]
    assert fields(got["30d"][2])["asks"] == 6 and got["30d"][3].get("unavailable")   # 60 days back: not held
    assert fields(got["90d"][3])["asks"] is None
    assert [r["window"] for r in got["12m"]] == ["12 mo"] * 3 + ["the 12 mo before"] + ["12 mo"] * 2


def test_a_range_before_that_is_not_held_is_n_a_with_its_reason_never_0() -> None:
    """#284, the design contact's answer: the card of the range before stays; when the stream
    does not hold it (12 months: 24 months back, never held) or it starts before the oldest
    line held, every figure is n/a (None) with a short reason; a held range has none."""
    book = book_with((A, LINE, NOW - 100 * 86400), (B, LINE, NOW - 3 * 86400))
    held = book.reports("30d")[3]                    # 30 to 60 days back: the oldest line is 100 days old
    assert "unavailable" not in held
    for key in ("7d", "30d", "90d"):
        before = book.reports(key)[3]
        assert ("unavailable" in before) == (key == "90d"), key
    na = book.reports("90d")[3]
    assert "oldest line" in na["unavailable"] and all(f["value"] is None for f in na["fields"])
    twelve = book.reports("12m")[3]
    assert "12 months" in twelve["unavailable"] and all(f["value"] is None for f in twelve["fields"])
    assert twelve["window"] == "the 12 mo before"
    assert "unavailable" not in book.reports("12m")[2]   # the range itself is held
    assert all(f["value"] is None for f in StatsBook().reports("7d")[3]["fields"])   # no lines at all


def test_the_year_of_lines_counts_the_same_and_fast_enough_and_off_the_loop() -> None:
    """#284 review: the activity bars find their bucket by bisection (the result equals the
    plain search of the bucket's start), `reports_async` answers the same as `reports` from a
    worker thread and keeps the answer for a minute while no line came, and a large book does not
    take seconds (a coarse guard, 20 times over the measured time)."""
    import asyncio
    import time
    from console_server.stats_view import _bucket_starts
    book = StatsBook()
    n = 20_000
    for i in range(1, n + 1):
        book._lines.append((NOW - (i * 1511) % (360 * 86400), i, A, LINE | {"post_words": {"ACK": [1, 1], "DONE": [1, 1]}}))
    book._server_now = NOW
    book._seen_at = (NOW, time.monotonic())
    t0 = time.monotonic()
    got = book.reports("12m")
    assert time.monotonic() - t0 < 20, "the 12 months range of 20k lines is far too slow"
    starts = _bucket_starts(book.server_now, "month", 12)
    want = [0] * 12
    for at, _seq, _agent, _line in book._lines:
        if at >= starts[0]:
            want[max(i for i, s in enumerate(starts) if s <= at)] += 2
    assert [sum(b["groups"].values()) for b in got[0]["activity"]["bars"]] == want
    again = asyncio.run(book.reports_async("12m"))
    assert [r["id"] for r in again] == [r["id"] for r in got] and again[0] == got[0]
    assert asyncio.run(book.reports_async("12m")) is book._cache[1]   # no new line: kept


WORDS = {"ACK": [4, 10], "PROGRESS": [2, 10], "RESULT": [1, 5], "DONE": [3, 5], "ask": [2, 5],
         "TASK": [1, 5], "HELLO": [1, 1], "plain": [5, 50], "BLOCKED": [1, 1]}


def test_the_activity_bars_bundle_the_bus_words_in_four_groups() -> None:
    """#284: one bar per day (7, 30 days), week (90 days) or month (12 months), the agents'
    own posts by bus word in four groups with every word kept for the hover; a part counts
    in the bucket it was received in; no names; 24 h without hours says why it has no bars (the hourly test below)."""
    book = book_with((A, LINE | {"post_words": WORDS}, NOW - 600), (B, LINE | {"post_words": {"ACK": [1, 1]}}, NOW - 3 * 86400),
                     (C, LINE | {"post_words": {"DONE": [7, 1]}}, NOW - 100 * 86400), (A, LINE, NOW - 50))
    act = book.reports("7d")[0]
    assert act["id"] == "activity" and act["activity"]["bucket"] == "day" and len(act["activity"]["bars"]) == 7
    assert [g["label"] for g in act["activity"]["groups"]] == [
        "working on it", "finished something", "asking a human", "organising"]
    last = act["activity"]["bars"][-1]
    assert last["groups"] == {"working": 6, "finished": 4, "asking": 2, "organising": 8}
    assert last["words"]["plain"] == 5 and last["words"]["BLOCKED"] == 1
    assert sum(b["groups"]["working"] for b in act["activity"]["bars"]) == 7   # the day-3 ACK too
    assert [len(book.reports(k)[0]["activity"]["bars"]) for k in ("30d", "90d", "12m")] == [30, 13, 12]
    assert book.reports("90d")[0]["activity"]["bucket"] == "week"
    assert sum(b["groups"]["finished"] for b in book.reports("12m")[0]["activity"]["bars"]) == 4 + 7
    assert sum(b["groups"]["finished"] for b in book.reports("90d")[0]["activity"]["bars"]) == 4
    hour = book.reports("24h")[0]["activity"]   # no line carries hours: n/a with its reason
    assert hour["bucket"] == "hour" and hour["bars"] == [] and hour["unavailable"] == "no hourly data yet" and "publish hourly" in hour["reason"] and "hours" not in hour
    assert A not in json.dumps(book.reports("12m")) and B not in json.dumps(book.reports("12m"))


def test_buckets_start_at_midnight_monday_and_the_first_of_the_month_utc() -> None:
    from datetime import datetime, timezone
    from console_server.stats_view import _bucket_starts
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc).timestamp()   # a Wednesday
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")  # noqa: E731
    assert iso(_bucket_starts(now, "day", 7)[-1]) == "2026-03-04"
    assert iso(_bucket_starts(now, "week", 13)[-1]) == "2026-03-02"
    assert [iso(t) for t in _bucket_starts(now, "month", 3)] == ["2026-01-01", "2026-02-01", "2026-03-01"]


def test_24h_draws_the_hours_of_the_lines_that_carry_them_and_says_from_how_many() -> None:
    """#284, #290: "24 h" has 24 hourly bars up to the current hour from `stats.hours_of`, a post
    in the hour it was made in (not the one its part arrived); `hours` says `lines` of `of`, so a
    chart of the lines that carry hours does not look complete; hours out of the 24 are left out."""
    hour = int(NOW) // 3600
    mine = LINE | {"post_words": WORDS, "post_hours": {
        str(hour): {"ACK": 2, "DONE": 1}, str(hour - 5): {"ask": 1, "BLOCKED": 2}, str(hour - 30): {"ACK": 9}}}
    plain = LINE | {"post_words": {"ACK": [1, 1]}}   # a line before the hourly buckets
    book = book_with((A, mine, NOW - 600), (B, plain, NOW - 300), (C, plain, NOW - 3 * 86400))
    act = book.reports("24h")[0]["activity"]
    assert act["bucket"] == "hour" and len(act["bars"]) == 24 and "unavailable" not in act
    assert act["hours"] == {"lines": 1, "of": 2}   # the line of 3 days back is not in the range
    assert [b["start"] for b in act["bars"]][-1] == hour * 3600
    assert all(b["start"] % 3600 == 0 for b in act["bars"])
    assert act["bars"][-1]["groups"] == {"working": 2, "finished": 1, "asking": 0, "organising": 0}
    assert act["bars"][-6]["groups"] == {"working": 0, "finished": 0, "asking": 1, "organising": 2}
    assert act["bars"][-6]["words"] == {"ask": 1, "BLOCKED": 2}
    assert sum(sum(b["groups"].values()) for b in act["bars"]) == 6   # the hour of 30 hours back is out
    assert A not in json.dumps(act)


def test_24h_with_every_line_carrying_hours_says_so_and_with_none_is_na() -> None:
    """#284: all lines with hours: `lines` equals `of`; a book without any line: n/a, never 0."""
    hour = int(NOW) // 3600
    full = LINE | {"post_words": {"ACK": [1, 1]}, "post_hours": {str(hour): {"ACK": 1}}}
    act = book_with((A, full, NOW - 60)).reports("24h")[0]["activity"]
    assert act["hours"] == {"lines": 1, "of": 1} and len(act["bars"]) == 24
    empty = StatsBook().reports("24h")[0]["activity"]
    assert empty["bars"] == [] and empty["unavailable"] == "no hourly data yet" and "hours" not in empty
