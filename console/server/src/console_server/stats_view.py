"""The reports from the agents' STATS lines on NATS (RT-6.43 steps 7 and 8; ADR-015,
addition of 2026-11-25, points 12 to 15).

Each MCP server publishes each part of its STATS line on ``<stats>.<name>`` (stream
``STATS``). Every line is decoded strictly with ``stats.decode_line`` — strings only in
``v``, ``sid``, ``rt`` and ``kind``, each by its pattern; keys from the allow list of
``stats.py``, others ignored and never shown — and a line that breaks a rule is dropped
whole with a warning that carries none of it. The reports are computed with ``stats.py``,
the numbers ``stats_report`` gives on the same lines: the bus overhead share of all tokens
per version over 7 days, the asks over 24 hours and 7 days, the bus traffic by category over
7 days (RT-6.9 step 3: exact counts, grouped by ``stats.CATEGORY_OF``) and the tokens by
category over 7 days (an estimate, marked so: one stacked bar of task work, bus overhead and
coordination for the flock, per version, per role and per harness). A missing value is ``None``
("n/a", never 0); each report says the age of the oldest line it holds. The reports carry
no agent's name and no report by home or owner, and every operator sees them over every
agent. Only the per-agent rows (``agent_rows``, ``/v1/agent-stats``) carry an agent's
name, and only for the agents the caller names: those whose attestation names the
logged-in operator as owner or trusted (ADR-015 point 15).
"""
from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Collection

from mattermost_ai_agent_bus import stats
from mattermost_ai_agent_bus.natsbus import agent_ok, parse_ack

log = logging.getLogger(__name__)

WEEK_SEC = 7 * 86400
DAY_SEC = 86400
YEAR_SEC = 365 * 86400   # the STATS stream's age (RT-6.45): the longest time range
# Lines held at most: the stream keeps 500 per name and 64 MiB in all; a year's lines of a
# large flock fit, and a flood cannot grow the console's memory without bound.
MAX_LINES = 100_000

# The time ranges of the Stats page (#284): key, the label every card shows, seconds, the
# bars' bucket. The range before it is the same length straight back.
RANGES: dict[str, tuple[str, int, str]] = {
    "24h": ("24 h", DAY_SEC, "hour"),
    "7d": ("7 d", WEEK_SEC, "day"),
    "30d": ("30 d", 30 * DAY_SEC, "day"),
    "90d": ("90 d", 90 * DAY_SEC, "week"),
    "12m": ("12 mo", YEAR_SEC, "month"),
}
DEFAULT_RANGE = "7d"
BARS = {"day": {"7d": 7, "30d": 30}, "week": {"90d": 13}, "month": {"12m": 12}}
# "24 h" shows the hourly bars of the lines that carry hours (`stats.hours_of`, #290); when no
# line does it is n/a with the reason, never 0 and never an empty chart (design contact, #284).
HOURLY_NA = "no hourly data yet"
HOURLY_NOTE = "hourly figures need agents that publish hourly"

# The bus words of an agent's own posts in four groups (#284); a word outside the lists
# (BLOCKED, DELEGATE, a plain post, "other") is organising.
ACTIVITY_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("working", "working on it", ("ACK", "PROGRESS")),
    ("finished", "finished something", ("RESULT", "DONE")),
    ("asking", "asking a human", ("ask",)),
    ("organising", "organising", ()),
)


@dataclass
class StatsBook:
    """The STATS lines of the last 365 days, by server time, decoded strictly."""

    stats_subject: str = "stats"
    # (server time, stream sequence, agent, line) in arrival order
    _lines: deque[tuple[float, int, str, dict[str, Any]]] = field(default_factory=deque)
    # the stream sequences held: a consumer made anew replays 7 days, counted once
    _seqs: set[int] = field(default_factory=set)
    _server_now: float = 0.0
    _seen_at: tuple[float, float] | None = None  # (server time, monotonic) of the newest stamp
    count: Callable[[], None] | None = None   # the process-wide counter (#158, telemetry)
    _cache: tuple[tuple[str, int, int, int], list[dict[str, Any]]] | None = None

    @property
    def server_now(self) -> float:
        """The newest server stamp, carried on by the monotonic clock (as the fleet's)."""
        if self._seen_at is None:
            return self._server_now
        at, mono = self._seen_at
        return max(self._server_now, at + (time.monotonic() - mono))

    def on_delivery(self, subject: str, data: bytes, reply: str | None,
                    stream: str, consumer: str) -> bool:
        """One delivery of the STATS consumer; True when a line was taken."""
        ack = parse_ack(reply, stream, consumer)
        if ack is None:
            return False
        if ack.stream_seq in self._seqs:
            return False  # a replay after a reconnect: this line is held already
        t = subject.split(".")
        if len(t) != 2 or t[0] != self.stats_subject or not agent_ok(t[1]):
            return False
        try:
            line = stats.decode_line(data)
        except stats.LineError:
            if self.count is not None:
                self.count()
            log.warning("console: a STATS line dropped: not within the rules of the line on NATS")
            return False
        if ack.time > self._server_now:
            self._server_now = ack.time
            self._seen_at = (ack.time, time.monotonic())
        self._lines.append((ack.time, ack.stream_seq, t[1], line))
        self._seqs.add(ack.stream_seq)
        edge = self.server_now - YEAR_SEC
        while self._lines and (self._lines[0][0] < edge or len(self._lines) > MAX_LINES):
            self._seqs.discard(self._lines.popleft()[1])
        return True

    def _within(self, sec: float) -> list[dict[str, Any]]:
        edge = self.server_now - sec
        return [line for _at, _seq, _agent, line in self._lines if _at >= edge]

    def within(self, sec: float) -> list[tuple[float, int, dict[str, Any]]]:
        """The decoded lines of the last `sec` seconds, oldest first — the
        (server time, stream sequence, line) rows the reports (and #158's
        /metrics) compute over; raw, never shown to a browser."""
        edge = self.server_now - sec
        return [(at, seq, line) for at, seq, _agent, line in self._lines if at >= edge]

    def reports(self, range_key: str = DEFAULT_RANGE) -> list[dict[str, Any]]:
        """`/v1/stats?range=` over a snapshot of the lines (see `_reports`)."""
        return self._reports(range_key, tuple(self._lines), self.server_now)

    async def reports_async(self, range_key: str = DEFAULT_RANGE) -> list[dict[str, Any]]:
        """The same, off the event loop: a year's lines take seconds to count (100k lines, the
        12 months range, about 2 s). The snapshot (a tuple of the rows, taken here on the loop
        thread: the lines themselves are never changed) is all the worker thread reads, so
        deliveries arriving meanwhile cannot disturb it. The answer is kept for a minute per
        range while no line came (the page asks every 30 s)."""
        lines, now = tuple(self._lines), self.server_now
        key = (range_key, len(lines), lines[-1][1] if lines else 0, int(now // 60))
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        out = await asyncio.to_thread(self._reports, range_key, lines, now)
        self._cache = (key, out)
        return out

    def _reports(self, range_key: str, lines: tuple[tuple[float, int, str, dict[str, Any]], ...],
                 now: float) -> list[dict[str, Any]]:
        """`/v1/stats?range=` (#284): over the chosen time range (24 h, 7, 30, 90 days, 12
        months, 7 days by default) the activity bars, the bus overhead share per version, the
        asks over the range and over the range before it, the bus traffic by category and the
        tokens by category."""
        label, sec, _bucket = RANGES[range_key]
        held = [at for at, _seq, _agent, _line in lines if at >= now - sec]
        oldest = {"label": "age of the oldest line", "unit": "s",
                  "value": round(now - min(held)) if held else None}
        week = [line for at, _seq, _agent, line in lines if at >= now - sec]
        rows = _safe(stats.report, week) or []
        # The overhead segment of the token split (RT-6.9 step 3), in input-token
        # equivalents: a version without the split is n/a, never 0.
        share = [{"label": f"version {row['version']}", "unit": "%",
                  "value": _pct(((row.get("split") or {}).get("eq_pct") or {}).get(stats.OVERHEAD))}
                 for row in rows]
        out = [self._activity(range_key, lines, now),
               {"id": "share-per-version", "title": "bus overhead share of all tokens, per version",
                "window": label, "note": stats.SPLIT_NOTE,
                "fields": [*share, {"label": "lines", "unit": "count", "value": len(week) or None}, oldest]}]
        before_na = _before_unavailable(sec, lines, now)
        before = [] if before_na else [line for at, _seq, _agent, line in lines
                                       if now - 2 * sec <= at < now - sec]
        for rid, window, rows_of in (("asks", label, week),
                                     ("asks-before", f"the {label} before", before)):
            a = ({} if rid == "asks-before" and before_na else _safe(stats.asks_summary, rows_of)) or {}
            out.append({"id": rid, "title": "asks to humans", "window": window,
                        **({"unavailable": before_na} if rid == "asks-before" and before_na else {}),
                        "fields": [
                {"label": "asks", "unit": "count", "value": a.get("asks")},
                {"label": "answered", "unit": "count", "value": a.get("answered")},
                {"label": "answered share", "unit": "%", "value": _pct(a.get("answered_pct"))},
                {"label": "mean wait for an answer", "unit": "s", "value": a.get("mean_wait_sec")},
                {"label": "longest wait for an answer", "unit": "s", "value": a.get("max_wait_sec")},
                {"label": "still open at the part's end", "unit": "count", "value": a.get("open")},
                {**oldest, "value": None} if rid == "asks-before" and before_na else oldest]})
        t = _safe(stats.traffic_summary, week) or {}
        ovh, coord = t.get("overhead") or [None, None], t.get("coordination") or [None, None]
        out.append({"id": "traffic-by-category", "title": "bus traffic by category", "window": label, "fields": [
            {"label": "bus overhead, items", "unit": "count", "value": ovh[0]},
            {"label": "bus overhead, characters", "unit": "count", "value": ovh[1]},
            {"label": "coordination, items", "unit": "count", "value": coord[0]},
            {"label": "coordination, characters", "unit": "count", "value": coord[1]},
            {"label": "bus overhead share of characters", "unit": "%", "value": _pct(t.get("overhead_chars_pct"))},
            {"label": "polls", "unit": "count", "value": t.get("polls")},
            {"label": "empty polls share", "unit": "%", "value": _pct(t.get("empty_polls_pct"))},
            {"label": "lines with these counts", "unit": "count", "value": t.get("lines")},
            {"label": "lines", "unit": "count", "value": len(week) or None},
            oldest]})
        out.append(self._split_report(week, rows, oldest, label))
        return out

    def _activity(self, range_key: str, lines: tuple[tuple[float, int, str, dict[str, Any]], ...],
                  now: float) -> dict[str, Any]:
        """The first card (#284): the agents' own posts by bus word, one bar per bucket, the
        words bundled into four groups (`ACTIVITY_GROUPS`) and every word kept for the hover.
        A part counts in the bucket it was received in, by the server's time (UTC). A part
        covers up to a day, so "24 h" draws the hours of `stats.hours_of` (a post's own hour,
        UTC) over the 24 hours up to now and says from how many of the lines held they come
        (`hours`: `lines` of `of`); no line with hours is n/a with its reason."""
        label, _sec, bucket = RANGES[range_key]
        card: dict[str, Any] = {
            "id": "activity", "title": "Activity · what the agents posted", "window": label,
            "fields": [], "activity": {
                "bucket": bucket, "groups": [{"id": g, "label": name} for g, name, _w in ACTIVITY_GROUPS],
                "bars": []}}
        if bucket == "hour":
            got = _safe(stats.hours_of, [line for at, _seq, _agent, line in lines if at >= now - DAY_SEC])
            if not got:
                card["activity"]["unavailable"] = HOURLY_NA
                card["activity"]["reason"] = HOURLY_NOTE
                return card
            first = int(now // 3600) * 3600 - 23 * 3600
            bars: list[dict[str, Any]] = [
                {"start": first + 3600 * i, "groups": {g: 0 for g, _n, _w in ACTIVITY_GROUPS}, "words": {}}
                for i in range(24)]
            for start, words in got["hours"].items():
                if start < first or start >= first + 24 * 3600:
                    continue
                bar = bars[(start - first) // 3600]
                for word, n in words.items():
                    bar["words"][word] = bar["words"].get(word, 0) + n
                    bar["groups"][_GROUP_OF.get(word, "organising")] += n
            card["activity"]["bars"] = bars
            card["activity"]["hours"] = {"lines": got["lines"], "of": got["of"]}
            return card
        starts = _bucket_starts(now, bucket, BARS[bucket][range_key])
        bars = [{"start": s, "groups": {g: 0 for g, _n, _w in ACTIVITY_GROUPS}, "words": {}}
                for s in starts]
        for at, _seq, _agent, line in lines:
            if at < starts[0]:
                continue
            i = bisect_right(starts, at) - 1
            for word, n in _words(line):
                bars[i]["words"][word] = bars[i]["words"].get(word, 0) + n
                bars[i]["groups"][_GROUP_OF.get(word, "organising")] += n
        card["activity"]["bars"] = bars
        return card

    def _split_report(self, week: list[dict[str, Any]], rows: list[dict[str, Any]],
                      oldest: dict[str, Any], window: str) -> dict[str, Any]:
        """The tokens by category over 7 days (RT-6.9 step 3, RT-6.43 step 8): one stacked
        bar for the flock, per version, per role and per harness, in input-token
        equivalents (`segments`) and raw tokens (`raw`), each marked estimated."""
        fleet = _safe(stats.split_summary, week)
        stacks = [_stack("flock", fleet)]
        stacks += [_stack(f"version {row['version']}", row.get("split")) for row in rows]
        for key, name in (("role", stats.role_of), ("harness", lambda r: str(r.get("rt") or "other"))):
            groups: dict[str, list[dict[str, Any]]] = {}
            for line in week:
                groups.setdefault(name(line), []).append(line)
            stacks += [_stack(f"{key} {g}", _safe(stats.split_summary, ls)) for g, ls in sorted(groups.items())]
        f = fleet or {}
        harnesses = ", ".join(f.get("harnesses") or []) or "none"
        return {"id": "tokens-by-category", "title": "tokens by category", "window": window,
                "note": f"{stats.SPLIT_NOTE}; from {f.get('lines', 0)} of {len(week)} lines ({harnesses})",
                "fields": [{"label": "lines with the split", "unit": "count", "value": f.get("lines")},
                           {"label": "lines", "unit": "count", "value": len(week) or None},
                           {"label": "requests driven by the bus alone", "unit": "count",
                            "value": f.get("req_ovh")},
                           {"label": "requests driven by coordination", "unit": "count",
                            "value": f.get("req_coord")},
                           oldest],
                "stacks": [s for s in stacks if s is not None]}

    def agent_rows(self, shown: Collection[str]) -> list[dict[str, Any]]:
        """`/v1/agent-stats` (#201): one row per agent in ``shown`` whose parts the
        7-day window holds, the canvas's per-agent numbers: the token sums of the
        last 24 hours and of the window (``tok24h``/``tok7d``: ``all`` and the split
        by type, plus the bus overhead and coordination of the input), and the
        posts, wakes and tool calls of the last 24 hours. ``shown`` are the agents
        whose attestation names the logged-in operator as owner or trusted
        (``Fleet.own_or_trusted``, ADR-015 point 15): no other agent gets a row,
        and an agent without a known attestation none either. A field no part of
        the window carries is ``None`` ("n/a", never 0) — the reports' rule
        (RT-6.43 step 8) — and so is a window without any ``tok`` at all. One row
        that cannot be computed has its numbers n/a; it never fails the others."""
        edge = self.server_now - WEEK_SEC
        day = self.server_now - DAY_SEC
        groups: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        for at, _seq, agent, line in self._lines:
            if at >= edge and agent in shown:
                groups.setdefault(agent, []).append((at, line))
        return [_safe(lambda rs, a=agent: _row(a, rs, day), rows) or _row_na(agent)
                for agent, rows in sorted(groups.items())]


def _before_unavailable(sec: int, lines: tuple[tuple[float, int, str, dict[str, Any]], ...],
                        now: float) -> str | None:
    """Why the range before the chosen one cannot be shown, or None (the design contact's
    answer, #284): the stream holds `YEAR_SEC`, and the console holds the lines it has read,
    so a previous range that starts further back than that, or before the oldest line held,
    is n/a (the card stays, every figure n/a, never 0)."""
    if 2 * sec > YEAR_SEC:
        return "the stream holds 12 months at most, so the range before is never held"
    if not lines or min(at for at, _s, _a, _l in lines) > now - 2 * sec:
        return "the range before starts before the oldest line held"
    return None


_GROUP_OF = {w: gid for gid, _n, words in ACTIVITY_GROUPS for w in words}


def _words(line: dict[str, Any]) -> list[tuple[str, int]]:
    """A part's own posts by bus word: (word, count), counts only; none for a part without."""
    pw = line.get("post_words")
    if not isinstance(pw, dict):
        return []
    return [(w, int(v[0])) for w, v in pw.items() if isinstance(v, list) and v and v[0]]


def _bucket_starts(now: float, bucket: str, n: int) -> list[float]:
    """The start (UTC, epoch seconds) of the last `n` buckets, the current one last: days from
    midnight, weeks from Monday, months from the 1st."""
    t = datetime.fromtimestamp(now, timezone.utc)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if bucket == "day":
        first = day - timedelta(days=n - 1)
        return [(first + timedelta(days=i)).timestamp() for i in range(n)]
    if bucket == "week":
        monday = day - timedelta(days=day.weekday())
        first = monday - timedelta(weeks=n - 1)
        return [(first + timedelta(weeks=i)).timestamp() for i in range(n)]
    out = []
    y, m = t.year, t.month
    for _ in range(n):
        out.append(datetime(y, m, 1, tzinfo=timezone.utc).timestamp())
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _row(agent: str, rows: list[tuple[float, dict[str, Any]]], day: float) -> dict[str, Any]:
    last_day = [line for at, line in rows if at >= day]
    return {"agent": agent,
            "tok24h": _tok(last_day),
            "tok7d": _tok([line for _at, line in rows]),
            "posts24h": _count(last_day, "posts"),
            "wakes24h": _count(last_day, "wakes"),
            "tools24h": _count(last_day, "tools")}


def _row_na(agent: str) -> dict[str, Any]:
    """The row of an agent whose numbers could not be computed: all n/a."""
    return {"agent": agent, "tok24h": None, "tok7d": None,
            "posts24h": None, "wakes24h": None, "tools24h": None}


def _safe(compute: Any, lines: list[Any]) -> Any:
    """A report over lines the strict decoder let in; should a line still break the
    arithmetic, the report says n/a instead of failing /v1/stats."""
    try:
        return compute(lines)
    except Exception as exc:  # noqa: BLE001 - one bad line never takes the reports down
        log.warning("console: a STATS report not computed (%s)", type(exc).__name__)
        return None


def _segments(split: dict[str, Any], values: str, shares: str) -> list[dict[str, Any]]:
    return [{"category": c, "value": split[values][c], "percent": _pct(split[shares][c])}
            for c in stats.SPLIT_CATEGORIES]


def _stack(label: str, split: dict[str, Any] | None) -> dict[str, Any] | None:
    """One stacked bar: the three categories with their shares, none without the split."""
    if not split:
        return None
    try:
        return {"label": label, "estimated": True, "segments": _segments(split, "eq", "eq_pct"),
                "raw": _segments(split, "raw", "raw_pct")}
    except (KeyError, TypeError):  # a split without its sums: no bar, never a failed report
        return None


def _pct(v: Any) -> Any:
    """A share, held to 0 to 100 (an agent's numbers about itself are its own word)."""
    return None if v is None else min(100, max(0, v))


# The token types a part may state (stats.TOK_TYPES): fresh input, cache read
# (5 min, 1 h), cache write and output — the detail's "tokens/cache split".
_TOK_SHOWN = ("in", "read", "w5m", "w1h", "out")


def _tok(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The token sums of a window's parts: ``all`` and each stated type summed,
    the input's bus overhead and coordination summed over the same parts. A part
    counts only when its ``all`` states at least one type, so the overhead and the
    coordination never come from parts the total leaves out; each part's overhead
    and coordination are held to its own total (an agent's numbers about itself
    are its own word). None as a whole without such a part (a runtime that reports
    no usage, an older publisher, a part with only request counts, an empty
    ``all``); inside a row a type no part states is None, and so is an overhead or
    coordination no part states or states only as an empty object, but a stated 0
    stays 0 (n/a is "not carried", never "zero")."""
    parts = [line["tok"] for line in lines
             if isinstance(line.get("tok"), dict) and isinstance(line["tok"].get("all"), dict)
             and line["tok"]["all"]]
    if not parts:
        return None

    types: dict[str, int] = {}
    for part in parts:
        for key, value in part["all"].items():
            types[key] = types.get(key, 0) + int(value)

    def _share(obj: str) -> int | None:
        stated = [(part[obj], part["all"]) for part in parts
                  if isinstance(part.get(obj), dict) and part[obj]]
        if not stated:
            return None
        return sum(min(sum(int(v) for v in one.values()), sum(int(v) for v in total.values()))
                   for one, total in stated)

    return {"all": sum(types.values()),
            **{key: types.get(key) for key in _TOK_SHOWN},
            "ovh": _share("ovh"), "coord": _share("coord")}


def _count(lines: list[dict[str, Any]], key: str) -> int | None:
    """A window's sum of a part's counted field: posts by their count, wakes
    over their per-reason counts, tools over their per-tool counts (each a
    [count, characters] pair); None when no part of the window carries the
    field, never 0 for a missing field."""
    parts = [line[key] for line in lines if key in line]
    if not parts:
        return None
    if key == "posts":
        return sum(int(p[0]) for p in parts)
    if key == "tools":
        return sum(int(v[0]) for part in parts for v in part.values())
    return sum(int(value) for part in parts for value in part.values())
