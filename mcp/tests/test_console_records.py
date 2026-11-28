"""The records an MCP server publishes for the operator console (RT-6.43): the open asks on
`<presence>.ask.<home>.<name>` (steps 5 and 6) and each STATS part on `<stats>.<name>`
(steps 7 and 8), with their bounds and strict decoders (ADR-015, addition of 2026-11-25,
points 9 to 14)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from mattermost_ai_agent_bus import asks, hosted, natsbus, overhead, server, stats
from mattermost_ai_agent_bus.natsbus import (
    ASK_BODY_MAX, ASK_MAX, ASK_RECORD_MAX, ASK_RENEW_SEC, ASK_TITLE_MAX, ASK_VALID_SEC, Attestation, MessageError,
    OpenAsk, decode_ask_record, encode_ask_record,
)
from test_natsbus import _unit_bus

UID = "u" * 26
NO_CARD = {"context": "", "recommendation": "", "decision": "", "where": "", "answers": [], "chosen": ""}


def _id(i: int) -> str:
    return f"{i:026d}".replace("0", "a")[:26]


def _ask(i: int, **kw: Any) -> OpenAsk:
    base: dict[str, Any] = {"mm": _id(i), "kind": "approval", "human": "dembaca",
                            "title": f"ask {i}", "asked": 1764061200 + i, "answered": False}
    base.update(kw)
    return OpenAsk(**base)


# --- the ask record: encoder, bounds, strict decoder (RT-6.43 steps 5 and 6) -------------

def test_the_ask_record_is_the_protocols_shape() -> None:
    """RT-6.43 step 5: format v1, the asks oldest first, `more`, `iat` and `exp` 30 min later;
    the decoder reads back exactly what went out."""
    body = encode_ask_record([_ask(2, kind="question", human="-", answered=True), _ask(1)], 1764061800)
    obj = json.loads(body)
    assert list(obj) == ["v", "asks", "more", "iat", "exp"]
    assert obj["v"] == 1 and obj["more"] == 0
    assert obj["iat"] == 1764061800 and obj["exp"] == 1764061800 + ASK_VALID_SEC == 1764063600
    assert [a["mm"] for a in obj["asks"]] == [_id(1), _id(2)]  # oldest first
    assert set(obj["asks"][0]) == {"mm", "kind", "human", "title", "asked", "answered"}
    rec = decode_ask_record(body)
    assert rec.asks == (_ask(1), _ask(2, kind="question", human="-", answered=True))
    assert decode_ask_record(encode_ask_record([], 5)).asks == ()


def test_twenty_maxed_asks_stay_within_4_kib_and_the_newest_are_counted_in_more() -> None:
    """RT-6.43 step 5: at most 20 asks and 4 KiB; titles at their 200 characters (the widest
    JSON escapes) push the newest out into `more`, never past the bound."""
    wide = "\U0001F600"  # 12 bytes in ASCII JSON
    many = [_ask(i, title=wide * 500, human="a" + "~" * 63) for i in range(25)]
    body = encode_ask_record(many, 1764061800, more=3)
    # the stream's 4096 bytes count headers too: the encoder keeps a margin (none are sent)
    assert len(body) <= ASK_BODY_MAX < ASK_RECORD_MAX
    rec = decode_ask_record(body)
    assert len(rec.asks) >= 1 and len(rec.asks) + rec.more == 25 + 3
    assert [a.mm for a in rec.asks] == [_id(i) for i in range(len(rec.asks))]  # the oldest kept
    assert all(len(a.title) == ASK_TITLE_MAX for a in rec.asks)
    # plain titles: 20 fit, the other five are counted
    plain = encode_ask_record([_ask(i, title="t" * 200) for i in range(25)], 1)
    rec = decode_ask_record(plain)
    assert len(plain) <= ASK_BODY_MAX and len(rec.asks) <= ASK_MAX and len(rec.asks) + rec.more == 25
    # the largest record the encoder can make
    worst = encode_ask_record([_ask(i, title="‮" * 400, human="z" * 64, kind="question",
                                    answered=True, asked=2**40) for i in range(40)], 2**40, more=10**6)
    assert len(worst) <= ASK_BODY_MAX
    decode_ask_record(worst)


def test_the_encoder_cleans_what_it_is_handed() -> None:
    """A title is one line without control or direction characters; a human off the subject
    form goes as `-`, an unknown kind as approval; a bad or repeated id is counted in more."""
    body = encode_ask_record([_ask(1, title="a\nb‮c\x00", human="Not.Valid", kind="x"),
                              _ask(1), _ask(3, mm="bad")], 1)
    rec = decode_ask_record(body)
    assert rec.asks == (_ask(1, title="a b c", human="-"),) and rec.more == 2


@pytest.mark.parametrize("mutate", [
    lambda o: o.update(v=2),
    lambda o: o.update(props={"x": 1}),
    lambda o: o.pop("more"),
    lambda o: o["asks"][0].update(props={}),
    lambda o: o["asks"][0].update(title="t" * 201),
    lambda o: o["asks"][0].update(human="Dembaca"),
    lambda o: o["asks"][0].update(human="a.b"),
    lambda o: o["asks"][0].update(mm="short"),
    lambda o: o["asks"].append(dict(o["asks"][0])),
    lambda o: o["asks"][0].update(kind="order"),
    lambda o: o["asks"][0].update(answered=1),
    lambda o: o["asks"][0].update(asked=True),
    lambda o: o["asks"][0].update(asked=-1),
    lambda o: o.update(asks=[dict(o["asks"][0], mm=_id(i)) for i in range(ASK_MAX + 1)]),
    lambda o: o.update(more="0"),
    lambda o: o.update(exp=o["iat"] + ASK_VALID_SEC + 1),
    lambda o: o.update(exp=o["iat"] + 10**9),
    lambda o: o.update(exp=o["iat"]),
])
def test_the_decoder_drops_a_record_that_breaks_a_rule(mutate) -> None:
    """RT-6.43 step 6: not format v1, an unknown key, `props`, a long title, a human that is
    neither `-` nor an anchor in subject form, a malformed or repeated `mm`, more than 20."""
    obj = json.loads(encode_ask_record([_ask(1)], 1))
    mutate(obj)
    with pytest.raises(MessageError):
        decode_ask_record(json.dumps(obj).encode())


def test_the_decoder_drops_a_duplicate_key_and_an_oversized_record() -> None:
    body = encode_ask_record([_ask(1)], 1)
    with pytest.raises(MessageError):
        decode_ask_record(body.replace(b'"more":0', b'"more":0,"more":0'))
    with pytest.raises(MessageError):
        decode_ask_record(body[:-1] + b" " * ASK_RECORD_MAX + b"}")


# --- the bus publishes it: change, renewal, the end (RT-6.43 step 5) -----------------------

def _bus_with_capture() -> tuple[Any, list[tuple[str, bytes]]]:
    bus, _ = _unit_bus()
    sent: list[tuple[str, bytes]] = []

    async def publish(subject: str, body: bytes) -> None:
        sent.append((subject, body))
    bus._publish = publish  # type: ignore[method-assign]
    bus._server_now, bus._server_now_mono = 1764061800.0, natsbus.time.monotonic()
    bus._attest[bus.name] = (1, 1.0, Attestation(
        owner_anchor="dembaca", owner_name="dembaca",
        trusted=(("dembaca", "dembaca"), ("carol.x", "carol")), cut=False, exp=2e9))
    return bus, sent


def test_the_bus_publishes_at_a_change_and_renews_while_an_ask_is_open() -> None:
    """RT-6.43 step 5: one subject, `<presence>.ask.<home>.<name>`; a record at each change,
    none while nothing changed, again after ASK_RENEW_SEC; the asked human's name mapped to
    the anchor the own attestation pairs it with, `-` for a name it does not hold."""
    bus, sent = _bus_with_capture()
    open_asks = [OpenAsk(_id(1), "approval", "carol", "Merge?", 100), OpenAsk(_id(2), "question", "zed", "Which?", 50)]
    bus.ask_source = lambda: list(open_asks)

    async def run() -> None:
        assert await bus.publish_asks()
        assert not await bus.publish_asks()  # unchanged: nothing
        bus._ask_sent_at -= ASK_RENEW_SEC  # ten minutes on: renewed
        assert await bus.publish_asks()
        open_asks[0] = OpenAsk(_id(1), "approval", "carol", "Merge?", 100, answered=True)
        assert await bus.publish_asks()  # the first answer is a change
        open_asks.clear()
        assert await bus.publish_asks()  # ended: an empty list once
        assert not await bus.publish_asks()
        bus._ask_sent_at -= ASK_RENEW_SEC
        assert not await bus.publish_asks()  # nothing open: no renewal
    asyncio.run(run())
    assert {s for s, _ in sent} == {f"presence.ask.home.{bus.name}"}
    first = decode_ask_record(sent[0][1])
    assert [(a.mm, a.human) for a in first.asks] == [(_id(2), "-"), (_id(1), "carol~x")]
    assert first.exp == first.iat + ASK_VALID_SEC and first.iat == 1764061800
    assert decode_ask_record(sent[2][1]).asks[1].answered
    assert decode_ask_record(sent[3][1]).asks == () and len(sent) == 4


def test_a_new_server_replaces_a_predecessors_record_and_its_clock_runs_on(monkeypatch) -> None:
    """RT-6.43 step 5: the first check publishes even an empty list (a crashed predecessor's
    record is replaced); `iat` is the server clock carried forward by the monotonic one, so
    a quiet stream does not make a fresh record look old."""
    bus, sent = _bus_with_capture()
    bus._server_now_mono = natsbus.time.monotonic() - 2000  # the last delivery 2000 s ago
    bus.ask_source = lambda: []
    assert asyncio.run(bus.publish_asks())
    rec = decode_ask_record(sent[0][1])
    assert rec.asks == () and rec.iat >= 1764061800 + 2000
    assert not asyncio.run(bus.publish_asks())


def test_asks_changed_is_safe_from_another_thread() -> None:
    """asks.on_change may run off the bus's loop: the event is set in that loop."""
    import threading

    bus, _ = _unit_bus()
    bus.asks_changed()  # not started: nothing, no error

    async def run() -> None:
        bus._loop = asyncio.get_running_loop()
        t = threading.Thread(target=bus.asks_changed)
        t.start()
        t.join()
        await asyncio.wait_for(bus._ask_now.wait(), 1)
    asyncio.run(run())


def test_without_a_fresh_attestation_every_human_is_dash() -> None:
    """RT-6.43 step 5: the anchor comes from the login service's record (RT-6.41) or not at all."""
    bus, sent = _bus_with_capture()
    bus._attest.clear()
    bus.ask_source = lambda: [OpenAsk(_id(1), "approval", "dembaca", "t", 1)]
    asyncio.run(bus.publish_asks())
    assert decode_ask_record(sent[0][1]).asks[0].human == "-"


def test_a_refused_record_waits_a_minute_or_a_change() -> None:
    """A login service that grants no ask subject yet: the refusal is not tried every check."""
    bus, _ = _bus_with_capture()
    calls = []

    async def refused(subject: str, body: bytes) -> None:
        calls.append(subject)
        raise natsbus.NatsBusError("publish refused by the server: no permission for it")
    bus._publish = refused  # type: ignore[method-assign]
    asks_now = [OpenAsk(_id(1), "approval", "dembaca", "t", 1)]
    bus.ask_source = lambda: list(asks_now)

    async def run() -> None:
        with pytest.raises(natsbus.NatsBusError):
            await bus.publish_asks()
        assert not await bus.publish_asks()
        asks_now.append(OpenAsk(_id(2), "approval", "dembaca", "u", 2))
        with pytest.raises(natsbus.NatsBusError):
            await bus.publish_asks()
    asyncio.run(run())
    assert len(calls) == 2


class _Nc:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.is_closed = False

    async def publish(self, subject: str, body: bytes, **kw: Any) -> None:
        assert not kw.get("headers"), "the bus sends no headers"
        self.sent.append((subject, body))

    async def drain(self) -> None:
        pass

    async def flush(self, timeout: int = 10) -> None:
        pass

    async def close(self) -> None:
        self.is_closed = True


def test_at_its_bye_the_bus_publishes_an_empty_list_once() -> None:
    """RT-6.43 step 5: the bye ends the asks at once, before the presence's bye."""
    bus, _ = _unit_bus()
    nc = _Nc()
    bus._nc, bus._connected = nc, True  # type: ignore[assignment]
    bus.ask_source = lambda: [OpenAsk(_id(1), "approval", "dembaca", "t", 1)]
    asyncio.run(bus.stop())
    subjects = [s for s, _ in nc.sent]
    assert subjects[0] == f"presence.ask.home.{bus.name}" and subjects[1].endswith(bus.name)
    assert decode_ask_record(nc.sent[0][1]).asks == ()
    assert json.loads(nc.sent[1][1])["status"] == "bye"
    # without a source (an older wiring) no record at all
    bus2, _ = _unit_bus()
    nc2 = _Nc()
    bus2._nc, bus2._connected = nc2, True  # type: ignore[assignment]
    asyncio.run(bus2.stop())
    assert all(".ask." not in s for s, _ in nc2.sent)


def test_the_largest_record_goes_out_without_headers_within_the_streams_limit() -> None:
    """The PRESENCE stream's max_msg_size (4096 bytes) counts headers too: the largest record
    goes out through the bus's own publish with none, its body within ASK_BODY_MAX; a STATS
    part on `<stats>.<name>` the same way."""
    bus, _ = _unit_bus()
    nc = _Nc()
    bus._nc, bus._connected = nc, True  # type: ignore[assignment]
    bus._server_now, bus._server_now_mono = 2.0**40, natsbus.time.monotonic()
    bus.ask_source = lambda: [OpenAsk(_id(i), "question", "x" * 64, "\U0001F600" * 300, 2**40)
                              for i in range(ASK_MAX + 5)]
    asyncio.run(bus.publish_asks())
    subject, body = nc.sent[0]
    assert subject == f"presence.ask.home.{bus.name}" and len(body) <= ASK_BODY_MAX
    line = stats.nats_body(LINE | {"tools": {n: [10**12, 10**12] for n in stats.TOOL_NAMES}})
    assert line is not None and len(line) <= stats.NATS_BODY_MAX
    asyncio.run(bus.publish_stats(line))
    assert nc.sent[1] == (f"stats.{bus.name}", line)


def test_the_stats_prefix_is_checked_like_the_others() -> None:
    from test_natsbus import _settings
    with pytest.raises(ValueError):
        natsbus.NatsBus(_settings(stats="presence"), None, "alice", lambda e: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        natsbus.NatsBus(_settings(stats="deliver"), None, "alice", lambda e: None)  # type: ignore[arg-type]


# --- the server's side: what the asks file gives the record ------------------------------

def test_the_asks_file_gives_the_records_open_asks(monkeypatch) -> None:
    """RT-6.43 step 5: ask_human's asks with title, kind, time and first answer; a permission
    prompt (no one named) is left out; every write tells the bus."""
    changed: list[int] = []
    monkeypatch.setattr(asks, "on_change", lambda: changed.append(1))
    t0 = int(time.time()) - 600
    asks.add(UID, _id(1), "dembaca", _id(1), title="Merge  PR 118?", at=t0 + 0.5)
    asks.add(UID, _id(2), "carol", _id(2), title="Which branch?", kind="question")
    asks.add(UID, _id(3))  # a permission prompt
    assert len(changed) == 3
    asks.answered(UID, _id(1), t0 + 100)
    rows = asks.records(UID)
    assert [r[0] for r in rows] == [_id(1), _id(2)]
    assert rows[0][1:] == (t0, "dembaca", "Merge PR 118?", "approval", True, NO_CARD)
    assert rows[1][2:] == ("carol", "Which branch?", "question", False, NO_CARD)
    asks.resolve(UID, _id(1))
    assert [r[0] for r in asks.records(UID)] == [_id(2)] and len(changed) == 5
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    monkeypatch.setattr(stats, "_uid", "")
    got = server._open_asks()
    assert got == [OpenAsk(_id(2), "question", "carol", "Which branch?", rows[1][1], False)]


def test_a_hosted_server_publishes_its_asks_and_stats(monkeypatch) -> None:
    """RT-6.43 steps 5 and 7: a hosted (corral) session's NATS home carries the ask record
    and the STATS parts too: loading the server wires the port as the server's own start does."""
    from mattermost_ai_agent_bus.nats_port import NatsPort
    from fakes import FakeBus, FakeRegistrar

    port = NatsPort(FakeBus(FakeRegistrar()),  # type: ignore[arg-type]
                    natsbus.NatsSettings(servers=("nats://127.0.0.1:4222",), home="home"), ("home",))
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server, "_hide_lifecycle_tools", lambda: None)
    hosted._load_server(port)  # type: ignore[arg-type]
    assert port.card_source is server._presence_card
    assert port.ask_source is server._open_asks
    assert asks.on_change == port.asks_changed
    assert stats.nats_publish == port.publish_stats
    port.asks_changed()  # no bus yet: nothing, no error


# --- STATS on NATS (RT-6.43 steps 7 and 8) -------------------------------------------------

LINE = {"v": "0.101.0", "sid": "3f9a1c2b7d4e", "part": 2, "rt": "claude-code", "kind": "session",
        "min": 1440, "turns": 31, "turns_timed": 31, "turn_sec": 5400, "turn_max_sec": 900,
        "wakes": {"push": 12, "dm": 2}, "tools": {"post_message": [9, 1800], "wait_for_events": [4, 0]},
        "posts": [9, 4100], "asks": 2, "asks_answered": 2, "ask_wait_sec": 1260, "crown_sec": 0,
        "tok": {"req": 120, "bus_req": 18, "fixed": 21000, "all": {"in": 1200, "out": 900, "zz": 1}}}


def test_the_nats_line_is_the_chat_lines_object_within_the_allow_list() -> None:
    """RT-6.43 step 7: the same object, keys outside the allow list left out (`push` is no
    wake reason the hooks write), `rt` as `other` for a client outside the known ones."""
    body = stats.nats_body(LINE | {"note": 1, "rt": "vim-mcp"})
    assert body is not None and len(body) <= stats.NATS_LINE_MAX
    got = json.loads(body)
    assert got["rt"] == "other" and "note" not in got
    assert got["wakes"] == {"dm": 2} and got["tok"]["all"] == {"in": 1200, "out": 900}
    assert stats.decode_line(body) == got
    assert stats.nats_body(LINE | {"v": "0.101.0-dev"}) is None  # off its pattern: not sent


@pytest.mark.parametrize("bad", [
    {"posts": "nine"},
    {"wakes": {"dm": "2"}},
    {"v": "x"},
    {"sid": "3F9A1C2B7D4E"},
    {"rt": "emacs"},
    {"kind": "worker"},
    {"tools": {"post_message": [1, "a"]}},
    {"turns": None},
    {"v": {"<b>EVIL TEXT</b>": 1}},
    {"v": 7},
    {"sid": {"x": 1}},
    {"sid": [1]},
    {"rt": True},
    {"kind": ["session"]},
    {"tok": 5},
    {"tok": [1, 2]},
    {"tok": {"all": 5, "bus": 3}},
    {"tok": {"all": {"in": [1]}}},
    {"tok": {"req": -1}},
    {"asks": [1, 2]},
    {"asks": {"a": 1}},
    {"asks": True},
    {"asks": -5},
    {"asks": 1e308},
    {"asks": 10**19},
    {"min": {"a": 1}},
    {"ask_wait_max_sec": [9]},
    {"wakes": 3},
    {"blocks": [1]},
    {"tools": True},
    {"tools": {"post_message": [1, 2, 3]}},
    {"posts": [1]},
    {"part": {"a": 1}},
    {"part": 1.5},
    {"final": [1]},
    {"asks_answered": 1e-300},
    {"tok": {"all": {"in": 1e-320}, "bus": {"in": 5}}},
    {"min": 1.5},
    {"wakes": {"dm": 0.5}},
    {"posts": [1, 2.5]},
    {"tools": {"post_message": [1.0, 2]}},
])
def test_the_reader_drops_a_line_with_text_in_it(bad) -> None:
    """RT-6.43 step 8: a string outside the four fields, one off its pattern, or a listed key
    of another type than the allow list gives it (an object as `v` would become a label;
    a negative, huge or non-number count would break the sums) drops the whole line."""
    with pytest.raises(stats.LineError):
        stats.decode_line(json.dumps(LINE | bad).encode())
    if "rt" not in bad:  # an unknown client the publisher writes as `other`
        assert stats.nats_body(LINE | bad) is None  # and the publisher never sends one


def test_the_reader_drops_a_duplicate_key_and_a_long_line_and_ignores_unknown_keys() -> None:
    body = json.dumps(LINE).encode()
    with pytest.raises(stats.LineError):
        stats.decode_line(body.replace(b'"part": 2', b'"part": 2, "part": 3'))
    with pytest.raises(stats.LineError):
        stats.decode_line(body[:-1] + b"," + b" " * stats.NATS_LINE_MAX + b"}")
    with pytest.raises(stats.LineError):
        stats.decode_line(b"[1]")
    for huge in (b'{"asks": ' + b"9" * 4000 + b"}", b'{"asks": 1e400}'):
        with pytest.raises(stats.LineError):
            stats.decode_line(huge)
    assert "x_note" not in stats.decode_line(json.dumps(LINE | {"x_note": 3}).encode())


def test_the_allow_list_holds_every_field_a_part_has() -> None:
    """ADR-015 point 14: the publisher leaves nothing of RT-6.9's out by accident: every key
    of a rich part, the bus's tools and the token types are in the list."""
    events = [{"k": "part", "t": 1, "sid": "3f9a1c2b7d4e", "n": 1, "hooks": 1},
              {"k": "turn", "t": 2, "by": "bus"}, {"k": "turn_end", "t": 3},
              {"k": "wake", "t": 2, "reason": "dm"}, {"k": "block", "t": 3, "kind": "drain"},
              {"k": "tool", "t": 3, "name": "post_message", "chars": 5},
              {"k": "post", "t": 3, "chars": 5}, {"k": "ask", "t": 3, "id": "p"},
              {"k": "answer", "t": 4, "id": "p", "wait": 1, "at": 4}]
    data, _ = stats._summarize(events, "0.101.0", overhead.Fixed(1, 2, 3), 10.0, final=True)
    assert set(data) <= set(stats.LINE_KEYS)
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools <= stats.TOOL_NAMES
    assert stats.TOK_TYPES == set(overhead.KEYS)


def test_a_failed_publish_waits_for_the_next_part(monkeypatch) -> None:
    """RT-6.43 step 7: a part whose publish failed goes out before the next one."""
    sent: list[bytes] = []
    fail = [True]

    async def publish(body: bytes) -> None:
        if fail[0]:
            raise RuntimeError("down")
        sent.append(body)
    assert asyncio.run(stats.publish_nats(LINE)) == "off"

    def broken(_data: Any) -> bytes:
        raise RuntimeError("no")
    monkeypatch.setattr(stats, "nats_publish", publish)
    with monkeypatch.context() as m:
        m.setattr(stats, "nats_body", broken)
        assert asyncio.run(stats.publish_nats(LINE)) == "published"  # never raises
    monkeypatch.setattr(stats, "nats_publish", publish)
    assert asyncio.run(stats.publish_nats(LINE)) == "kept"
    fail[0] = False
    assert asyncio.run(stats.publish_nats(LINE | {"part": 3})) == "published"
    assert [json.loads(b)["part"] for b in sent] == [2, 3]


def test_the_console_reports_match_stats_report_on_the_same_lines() -> None:
    """RT-6.43 step 8: the asks the console sums are the asks stats_report gives per harness."""
    lines = [LINE, LINE | {"sid": "aaaaaaaaaaaa", "asks": 4, "asks_answered": 1, "ask_wait_sec": 60}]
    per = stats.report(lines)[0]["harness"][0]["asks"]
    assert stats.asks_summary(lines) == per


@pytest.mark.asyncio
async def test_ask_human_keeps_the_kind_for_the_record(monkeypatch) -> None:
    """RT-6.43 step 5: the record's `kind` is ask_human's, after its classification."""
    from test_ask_human import _Client

    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    q = json.loads(await server.ask_human(need="Which order?", kind="question", answers=["A", "B", "C"],
                                          context="Two PRs touch\n\n\nthe same file.",
                                          recommendation="Mine first.", where="PR 118"))
    me = (await c.get_me()).id
    rows = {r[0]: r for r in asks.records(me)}
    assert rows[q["ask"]][4] == "question" and rows[q["ask"]][3] == "Which order?"
    # RT-6.43 step 5: the card as posted, its answers in order (the record cuts it, below)
    card = rows[q["ask"]][6]
    assert card["answers"] == ["A", "B", "C"] and card["where"] == "PR 118"
    assert card["recommendation"] == "Mine first." and "the same file." in card["context"]
    assert card["decision"] == "" and card["chosen"] == ""
    monkeypatch.setattr(stats, "bot_id", lambda: me)
    (sent,) = [a for a in server._open_asks() if a.mm == q["ask"]]
    obj = json.loads(encode_ask_record([sent], 5))["asks"][0]
    assert obj["context"] == "Two PRs touch\n\nthe same file." and obj["answers"] == ["A", "B", "C"]
    assert "decision" not in obj and "chosen" not in obj


def test_the_protocols_examples_pass_the_real_decoders() -> None:
    """protocol.md's ask records and STATS line are what the code reads (RT-6.43)."""
    import re
    from pathlib import Path

    text = (Path(__file__).parents[2] / "docs" / "protocol.md").read_text()
    section = text[text.index("## Over NATS: the console's asks and STATS lines"):]
    blocks = re.findall(r"```json\n(.*?)```", section, re.S)
    records = [json.loads(chunk) for chunk in re.split(r"\n(?=\{\"v\": 1)", blocks[0].strip())]
    assert [len(decode_ask_record(json.dumps(r).encode()).asks) for r in records] == [2, 0]
    line = json.loads(blocks[1])
    assert stats.decode_line(json.dumps(line).encode())["sid"] == "3f9a1c2b7d4e"
    assert stats.nats_body(line) is not None


@pytest.mark.asyncio
async def test_a_hosted_session_publishes_its_last_part_before_the_bus_stops(monkeypatch) -> None:
    """RT-6.43 step 7: hosted.services ends with the last STATS part on NATS, then the bus."""
    from mattermost_ai_agent_bus import attest, compose, lease, login, release
    from fakes import FakeBus, FakeRegistrar

    order: list[str] = []

    async def idle(*_a: Any, **_k: Any) -> None:
        return None

    async def final() -> None:
        order.append("final")

    async def stop(_client: Any) -> None:
        order.append("stop")
    monkeypatch.setattr(lease, "heartbeat", idle)
    monkeypatch.setattr(attest, "start", idle)
    monkeypatch.setattr(release, "check_loop", idle)
    monkeypatch.setattr(login, "start", lambda _get: None)
    monkeypatch.setattr(compose, "start_nats", idle)
    monkeypatch.setattr(compose, "stop_nats", stop)
    monkeypatch.setattr(server, "_final_stats_on_nats", final)
    async with hosted.services("k" * 32, FakeBus(FakeRegistrar())):  # type: ignore[arg-type]
        pass
    assert order == ["final", "stop"]


def test_a_negative_wait_from_a_skewed_clock_counts_zero() -> None:
    """An answer event's wait below 0 (a clock that ran back) is 0, so an honest part always
    passes the typed check."""
    events = [{"k": "part", "t": 1, "sid": "3f9a1c2b7d4e", "n": 1},
              {"k": "ask", "t": 2, "id": "p"}, {"k": "answer", "t": 3, "id": "p", "wait": -40, "at": 3}]
    data, _ = stats._summarize(events, "0.101.0", None, 10.0, final=True)
    assert data["ask_wait_sec"] == 0 and data["ask_wait_max_sec"] == 0
    assert stats.nats_body(data) is not None


def test_the_harness_names_agree_everywhere() -> None:
    """The card's harness (natsbus.HARNESSES), the STATS line's `rt` (stats.RUNTIMES) and the
    console's `Harness` union (console/web/src/api/types.ts) name the same harnesses."""
    import re
    from pathlib import Path

    assert set(natsbus.HARNESSES) == set(stats.RUNTIMES)
    ts = (Path(__file__).parents[2] / "console" / "web" / "src" / "api" / "types.ts").read_text()
    union = re.search(r"export type Harness =([^\n]*\n[^\n]*)", ts)
    assert union is not None
    assert set(re.findall(r'"([a-z-]+)"', union.group(1))) == set(stats.RUNTIMES)


# --- the ask's card on the record (RT-6.43 step 5, owner's decision of 2026-11-28) --------
FULL = {"context": "Line one\n  spaced   out\n\n\n\nafter a gap", "recommendation": "Merge it.",
        "decision": "Merge PR 118 into main", "where": "PR 118 on github", "answers": ("go", "no-go")}


def test_the_record_carries_the_card_only_where_the_ask_has_one() -> None:
    """An ask without a card keeps the old shape (a reader before this change still takes
    it); one with a card sends each part it has, cleaned and within its bound."""
    plain = json.loads(encode_ask_record([_ask(1)], 5))["asks"][0]
    assert set(plain) == {"mm", "kind", "human", "title", "asked", "answered"}
    obj = json.loads(encode_ask_record([_ask(2, **FULL, chosen="go", answered=True)], 5))["asks"][0]
    assert obj["context"] == "Line one\nspaced out\n\nafter a gap"
    assert obj["recommendation"] == "Merge it." and obj["decision"] == "Merge PR 118 into main"
    assert obj["where"] == "PR 118 on github" and obj["answers"] == ["go", "no-go"] and obj["chosen"] == "go"
    back = decode_ask_record(encode_ask_record([_ask(2, **FULL, chosen="go")], 5)).asks[0]
    assert (back.context, back.where, back.answers, back.chosen) == (
        "Line one\nspaced out\n\nafter a gap", "PR 118 on github", ("go", "no-go"), "go")
    only = json.loads(encode_ask_record([_ask(3, where="host\u202e hogan\nnext")], 5))["asks"][0]
    assert only["where"] == "host hogan next" and "context" not in only and "answers" not in only


def test_the_cards_bounds_and_the_answers_rules() -> None:
    long = {"context": "c" * 5000, "recommendation": "r" * 5000, "decision": "d" * 5000,
            "where": "w" * 5000, "answers": ("a" * 500, "b")}
    obj = json.loads(encode_ask_record([_ask(1, **long)], 5))["asks"][0]
    assert [len(obj[k]) for k in ("context", "recommendation", "decision", "where")] == [
        natsbus.ASK_CONTEXT_MAX, natsbus.ASK_RECOMMENDATION_MAX, natsbus.ASK_DECISION_MAX,
        natsbus.ASK_WHERE_MAX]
    assert obj["answers"] == ["a" * natsbus.ASK_ANSWER_MAX, "b"]
    # a full card at every bound fits one record, in ASCII
    assert len(encode_ask_record([_ask(1, title="t" * 200, **long, chosen="b")], 5)) <= ASK_BODY_MAX
    # one answer is no choice, and a chosen that is none of them is dropped, never invented
    one = json.loads(encode_ask_record([_ask(3, answers=("only",), chosen="only")], 5))["asks"][0]
    assert "answers" not in one and "chosen" not in one
    stray = json.loads(encode_ask_record([_ask(4, answers=("a", "b"), chosen="c")], 5))["asks"][0]
    assert stray["answers"] == ["a", "b"] and "chosen" not in stray


def test_a_record_that_does_not_fit_sheds_the_newest_cards_before_any_ask() -> None:
    """Every ask keeps its title before any keeps its card: the newest asks' texts go first,
    then their answers; `more` counts only asks left out."""
    big = {k: v * 900 for k, v in (("context", "c"), ("recommendation", "r"))} | {
        "decision": "d" * 400, "answers": ("x" * 120, "y" * 120, "z" * 120)}
    rec = decode_ask_record(encode_ask_record([_ask(i, **big) for i in range(1, 4)], 5))
    assert [a.mm for a in rec.asks] == [_id(1), _id(2), _id(3)] and rec.more == 0
    assert rec.asks[0].context and rec.asks[0].answers
    assert not rec.asks[2].context and not rec.asks[2].decision
    many = decode_ask_record(encode_ask_record([_ask(i, **big) for i in range(1, ASK_MAX + 1)], 5))
    assert len(many.asks) + many.more == ASK_MAX and not many.asks[-1].answers


@pytest.mark.parametrize("extra", [
    {"answers": ["only"]}, {"answers": ["a", ""]}, {"answers": "ab"}, {"answers": ["a"] * 5},
    {"answers": ["a", "b"], "chosen": "c"}, {"chosen": "a"}, {"answers": ["a", "b"], "chosen": 1},
    {"answers": ["a\nb", "c"]}, {"answers": ["x" * 121, "c"]}, {"props": {}},
    {"context": ""}, {"context": 5}, {"context": "c" * 901}, {"context": "a\u202eb"},
    {"recommendation": "r" * 901}, {"decision": "d" * 401}, {"decision": None},
    {"where": "a\nb"}, {"where": "w" * 201}, {"where": "a\x07b"}, {"body": "x"},
])
def test_the_decoder_refuses_a_bad_card(extra: dict[str, Any]) -> None:
    """Strict: a card part off its type or bound, a control or direction character, a chosen
    that is none of the answers or an unknown key drops the whole record."""
    obj = json.loads(encode_ask_record([_ask(1)], 5))
    obj["asks"][0].update(extra)
    with pytest.raises(MessageError):
        decode_ask_record(json.dumps(obj).encode())


def test_a_reactions_choice_and_the_card_are_kept_for_the_record() -> None:
    asks.add(UID, _id(7), "dembaca", _id(7), title="Which?", kind="question", answers=["A", "B", "C"],
             card={"context": "why\nnow", "where": "", "decision": "", "recommendation": "B"})
    asks.answered(UID, _id(7), time.time(), chosen="B")
    row = next(r for r in asks.records(UID) if r[0] == _id(7))
    assert row[5] is True and row[6] == {"context": "why\nnow", "recommendation": "B", "decision": "",
                                         "where": "", "answers": ["A", "B", "C"], "chosen": "B"}
