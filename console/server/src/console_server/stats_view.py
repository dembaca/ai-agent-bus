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
coordination for the fleet, per version, per role and per harness). A missing value is ``None``
("n/a", never 0); each report says the age of the oldest line it holds. No agent's name,
no report by home or owner: the subject's name only says whose line it is.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from mattermost_ai_agent_bus import stats
from mattermost_ai_agent_bus.natsbus import AGENT_RE, parse_ack

log = logging.getLogger(__name__)

WEEK_SEC = 7 * 86400
DAY_SEC = 86400
# Lines held at most: the stream keeps 500 per name and 64 MiB in all; a week's lines of a
# large fleet fit, and a flood cannot grow the console's memory without bound.
MAX_LINES = 50_000


@dataclass
class StatsBook:
    """The STATS lines of the last 7 days, by server time, decoded strictly."""

    stats_subject: str = "stats"
    # (server time, stream sequence, line) in arrival order
    _lines: deque[tuple[float, int, dict[str, Any]]] = field(default_factory=deque)
    # the stream sequences held: a consumer made anew replays 7 days, counted once
    _seqs: set[int] = field(default_factory=set)
    _server_now: float = 0.0
    _seen_at: tuple[float, float] | None = None  # (server time, monotonic) of the newest stamp
    refusals: int = 0        # lines the strict decoder refused (dropped whole)
    count: Callable[[], None] | None = None   # the process-wide counter (#158)

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
        if len(t) != 2 or t[0] != self.stats_subject or not AGENT_RE.fullmatch(t[1]):
            return False
        try:
            line = stats.decode_line(data)
        except stats.LineError:
            self.refusals += 1
            if self.count is not None:
                self.count()
            log.warning("console: a STATS line dropped: not within the rules of the line on NATS")
            return False
        if ack.time > self._server_now:
            self._server_now = ack.time
            self._seen_at = (ack.time, time.monotonic())
        self._lines.append((ack.time, ack.stream_seq, line))
        self._seqs.add(ack.stream_seq)
        edge = self.server_now - WEEK_SEC
        while self._lines and (self._lines[0][0] < edge or len(self._lines) > MAX_LINES):
            self._seqs.discard(self._lines.popleft()[1])
        return True

    def _within(self, sec: float) -> list[dict[str, Any]]:
        edge = self.server_now - sec
        return [line for at, _seq, line in self._lines if at >= edge]

    def within(self, sec: float) -> list[tuple[float, int, dict[str, Any]]]:
        """The decoded lines of the last `sec` seconds, oldest first — the
        (server time, stream sequence, line) rows the reports (and #158's
        /metrics) compute over; raw, never shown to a browser."""
        edge = self.server_now - sec
        return [row for row in self._lines if row[0] >= edge]

    def reports(self) -> list[dict[str, Any]]:
        """`/v1/stats`: the share per version over 7 days, the asks over 24 h and 7 days, the
        bus traffic by category over 7 days."""
        edge = self.server_now - WEEK_SEC
        held = [at for at, _seq, _line in self._lines if at >= edge]
        oldest = {"label": "age of the oldest line", "unit": "s",
                  "value": round(self.server_now - min(held)) if held else None}
        week = self._within(WEEK_SEC)
        rows = _safe(stats.report, week) or []
        # The overhead segment of the token split (RT-6.9 step 3), in input-token
        # equivalents: a version without the split is n/a, never 0.
        share = [{"label": f"version {row['version']}", "unit": "%",
                  "value": _pct(((row.get("split") or {}).get("eq_pct") or {}).get(stats.OVERHEAD))}
                 for row in rows]
        out = [{"id": "share-per-version", "title": "bus overhead share of all tokens, per version",
                "window": "7 d", "note": stats.SPLIT_NOTE,
                "fields": [*share, {"label": "lines", "unit": "count", "value": len(week) or None}, oldest]}]
        for rid, window, sec in (("asks-24h", "24 h", DAY_SEC), ("asks-7d", "7 d", WEEK_SEC)):
            a = _safe(stats.asks_summary, self._within(sec)) or {}
            out.append({"id": rid, "title": "asks to humans", "window": window, "fields": [
                {"label": "asks", "unit": "count", "value": a.get("asks")},
                {"label": "answered", "unit": "count", "value": a.get("answered")},
                {"label": "answered share", "unit": "%", "value": _pct(a.get("answered_pct"))},
                {"label": "mean wait for an answer", "unit": "s", "value": a.get("mean_wait_sec")},
                {"label": "longest wait for an answer", "unit": "s", "value": a.get("max_wait_sec")},
                {"label": "still open at the part's end", "unit": "count", "value": a.get("open")},
                oldest]})
        t = _safe(stats.traffic_summary, week) or {}
        ovh, coord = t.get("overhead") or [None, None], t.get("coordination") or [None, None]
        out.append({"id": "traffic-by-category", "title": "bus traffic by category", "window": "7 d", "fields": [
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
        out.append(self._split_report(week, rows, oldest))
        return out

    def _split_report(self, week: list[dict[str, Any]], rows: list[dict[str, Any]],
                      oldest: dict[str, Any]) -> dict[str, Any]:
        """The tokens by category over 7 days (RT-6.9 step 3, RT-6.43 step 8): one stacked
        bar for the fleet, per version, per role and per harness, in input-token
        equivalents (`segments`) and raw tokens (`raw`), each marked estimated."""
        fleet = _safe(stats.split_summary, week)
        stacks = [_stack("fleet", fleet)]
        stacks += [_stack(f"version {row['version']}", row.get("split")) for row in rows]
        for key, name in (("role", stats.role_of), ("harness", lambda r: str(r.get("rt") or "other"))):
            groups: dict[str, list[dict[str, Any]]] = {}
            for line in week:
                groups.setdefault(name(line), []).append(line)
            stacks += [_stack(f"{key} {g}", _safe(stats.split_summary, ls)) for g, ls in sorted(groups.items())]
        f = fleet or {}
        harnesses = ", ".join(f.get("harnesses") or []) or "none"
        return {"id": "tokens-by-category", "title": "tokens by category", "window": "7 d",
                "note": f"{stats.SPLIT_NOTE}; from {f.get('lines', 0)} of {len(week)} lines ({harnesses})",
                "fields": [{"label": "lines with the split", "unit": "count", "value": f.get("lines")},
                           {"label": "lines", "unit": "count", "value": len(week) or None},
                           {"label": "requests driven by the bus alone", "unit": "count",
                            "value": f.get("req_ovh")},
                           {"label": "requests driven by coordination", "unit": "count",
                            "value": f.get("req_coord")},
                           oldest],
                "stacks": [s for s in stacks if s is not None]}


def _safe(compute: Any, lines: list[dict[str, Any]]) -> Any:
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
