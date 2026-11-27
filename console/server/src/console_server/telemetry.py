"""The console's ``/metrics`` endpoint (#158; ADR-015): facing Prometheus
in the text exposition format, two kinds of numbers on one page.

**Console metrics** are the process's own observations, aggregated over
all sessions: HTTP requests by bucketed route and status, the open fleet
streams, the operator sessions held, the session readers' NATS
connections and their states, the operator credentials' renewals and
failures (RT-6.43), and the records the strict decoders refused. **Agent
metrics** are the numbers the Reports/Stats tab shows (``/v1/stats``:
RT-6.43 step 8), read from the scrape session's own consumers: agents by
state and harness, the open asks it sees, and the tokens by category of
the last 7 days, in input-token equivalents, per version and harness —
an estimate, as the note of every line says (RT-6.9 step 2).

Who may scrape: the operator that is logged in, the same right ``/v1/*``
needs — the unattended variant of the owner's go (a listener of its own, a
scrape token, a read-only service account) is this page's sibling, not part
of it. A series a value is
unknown for (``n/a``) is left out entirely, never a 0; the help texts of
the agent metrics say what backs them. Labels stay small by design: no
session id, agent name, owner or home ever becomes a label (RT-6.43's
stance), the harness and state words are the decoder-bounded sets with
anything else as ``other``, versions are the stats lines' semver pattern.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from mattermost_ai_agent_bus import stats as mcp_stats

from .build import Build

if TYPE_CHECKING:  # for the type-checker only; no runtime import
    from .nats_read import PersonReader, View

WEEK_SEC = 7 * 86400

# The bounded label sets (every label value comes from one of these): the
# agents' presence state words (the columns the view adds among them), and
# the session readers' own connection states (the word before a ": ..." tail).
STATES = ("online", "away", "idle", "listening", "working", "bus", "offline")
READER_STATES = ("connecting", "reading", "waiting", "ended")
UNKNOWN = "other"

# The newest versions the tokens-by-category page keeps as their own label;
# older ones count as "other" (a hostile publisher must not be able to mint
# series without bound on the version pattern alone).
MAX_VERSIONS = 8

# The routes requests are counted by: the api's own paths, the flows and
# the fixed assets; anything with a variable tail is its bucket, anything
# else "other". Never a name, an id or a file of the app.
_ROUTES = ("/", "/livez", "/login", "/callback", "/logout", "/metrics",
           "/v1/me", "/v1/fleet", "/v1/asks", "/v1/stats", "/v1/agent-stats", "/v1/graph",
           "/v1/threads", "health", "static")


def route_bucket(path: str, health: str = "") -> str:
    """One of ``_ROUTES`` — the bounded bucket a request counts under. The
    readiness path is the settings' own name for it, one operator's config."""
    if health and path == health:
        return "health"
    if path in _ROUTES:
        return path
    if path.startswith("/v1/threads/"):
        return "/v1/threads"
    if path == "/favicon.svg" or path.startswith("/assets/"):
        return "static"
    return "other"


class Telemetry:
    """The console's own counters, written on the event loop. All of them are
    **process-wide** — counted at the event, never summed over the sessions
    that happen to be open, so no logout resets a series to a false spike."""

    def __init__(self) -> None:
        self.build = Build()      # #182: set by the console from the image build
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self.happens: dict[str, int] = defaultdict(int)    # named events

    def note(self, route: str, method: str, status: int) -> None:
        """One served request, by its bucket, method and status."""
        self.requests[(route, method, status)] += 1

    # What a session's reading counted at its event (never read out of a
    # view: a session's end must not drop a series). The keys are the
    # named events of the metrics page.
    EVENTS = ("bus_connect", "credential_renewal", "credential_renewal_failure",
              "stats_line_refused", "fleet_record_refused")

    def note_event(self, name: str) -> None:
        """One named event (see EVENTS), counted as it happens."""
        assert name in self.EVENTS
        self.happens[name] += 1

    def render(self) -> list[str]:
        """The counter series, one line each, in order."""
        return [f'choirbus_console_http_requests_total{{route="{r}",method="{m}",'
                f'status="{s}"}} {n}'
                for (r, m, s), n in sorted(self.requests.items())]


def _escape(value: str) -> str:
    """A label value as the exposition format wants it."""
    for bad, good in (("\\", "\\\\"), ("\n", "\\n"), ('"', '\\"')):
        value = value.replace(bad, good)
    return value


def _word(value: Any, allowed: tuple[str, ...]) -> str:
    """A bounded label: one of `allowed`, else "other"."""
    text = value if isinstance(value, str) else str(value or "")
    text = text.split(":", 1)[0].strip() or UNKNOWN
    return text if text in allowed else UNKNOWN


def _agents(fleet: Any) -> list[str]:
    """Agents by state and harness, as this session's consumers deliver."""
    by: dict[tuple[str, str], int] = defaultdict(int)
    for card in fleet.cards():
        by[(_word(card.get("state"), STATES), _word(card.get("harness"), mcp_stats.RUNTIMES))] += 1
    return [f'choirbus_bus_agents{{state="{_escape(s)}",harness="{_escape(h)}"}} {n}'
            for (s, h), n in sorted(by.items())]


def _tokens(book: Any) -> list[str]:
    """The tokens by category of the last 7 days, per version and harness —
    the tab's stacked bars, one series each, an estimate (RT-6.9 step 2).
    Only the newest MAX_VERSIONS versions keep their own label; an older one —
    or a hostile mint of many — counts as other, so the version pattern alone
    can mint no series (a bounded page whatever a publisher sends)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    lines_of: dict[str, int] = defaultdict(int)
    for _at, _seq, line in book.within(WEEK_SEC):
        version = str(line.get("v") or UNKNOWN)
        groups[(version, str(line.get("rt") or UNKNOWN))].append(line)
        lines_of[version] += 1
    # ranked by line count, not freshness: a publisher minting versions must
    # write as many lines as a real release amasses in a week to displace it
    newest = set(sorted(lines_of, key=lambda v: lines_of[v], reverse=True)[:MAX_VERSIONS])
    folded: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for (version, harness), group in groups.items():
        # one "other" bucket per harness: the capped versions merge, so a
        # series never repeats (one series per name and labels)
        folded[(version if version in newest else UNKNOWN, harness)].extend(group)
    out: list[str] = []
    for (version, harness), group in sorted(folded.items()):
        if harness != UNKNOWN and harness not in mcp_stats.RUNTIMES:
            harness = UNKNOWN
        try:
            split = mcp_stats.split_summary(group)
        except Exception:  # noqa: BLE001 - as stats_view._safe: one bad group skips
            continue
        if not split:
            continue
        for category in mcp_stats.SPLIT_CATEGORIES:
            value = (split.get("eq") or {}).get(category)
            if value is not None:
                out.append(f'choirbus_bus_tokens_eq{{category="{category}",'
                           f'version="{_escape(version)}",'
                           f'harness="{_escape(harness)}"}} {value}')
    return out


def reading(view: "View") -> bool:
    """Whether the view's own reading is live: its reader connected, no
    waiting state before it. Nothing else serves agent series — a waiting
    or ended reader shows none at all rather than numbers that stand still
    (n/a omitted, never 0)."""
    r = view.reader
    return r is not None and r.state == "reading"


def render(tel: Telemetry, sessions: int, streams: int,
           readers: "list[PersonReader]", view: "View",
           live: bool | None = None) -> str:
    """The whole page: the console's own observations first, then the agent
    numbers of the scrape session's own reading — the fleet and the STATS
    book its consumers deliver, the same data the Reports/Stats tab shows
    that operator. Counters can only grow under a scraper that polls."""
    by_state: dict[str, int] = defaultdict(int)
    for reader in readers:
        by_state[_word(reader.state, READER_STATES)] += 1
    happens = tel.happens

    if live is None:
        # a session's own reading is its liveness; the service page passes
        # its reading's own (a delivered event once): its book holds the
        # stream's truth whatever a fresh connection's state is
        live = reading(view)
    lines = [
        "# HELP choirbus_console_build_info The console's release version and"
        " source commit, from the image build (#182); always 1.",
        "# TYPE choirbus_console_build_info gauge",
        f'choirbus_console_build_info{{version="{_escape(tel.build.version or "unknown")}",'
        f'revision="{_escape(tel.build.revision or "unknown")}"}} 1',
        "# HELP choirbus_console_http_requests_total HTTP requests served,"
        " by bucketed route, method and status. Requests the transport"
        " refuses before any handler (a flood, a too-large head) are not"
        " counted.",
        "# TYPE choirbus_console_http_requests_total counter",
        *tel.render(),
        "# HELP choirbus_console_sessions_open Operator sessions held; an expired"
        " one stays counted until its next sweep.",
        "# TYPE choirbus_console_sessions_open gauge",
        f"choirbus_console_sessions_open {sessions}",
        "# HELP choirbus_console_sse_streams_open Open fleet streams"
        " (server-sent events).",
        "# TYPE choirbus_console_sse_streams_open gauge",
        f"choirbus_console_sse_streams_open {streams}",
        "# HELP choirbus_console_bus_readers The session readers' NATS connection"
        " states: connecting, reading, waiting (the reason on its tail) or ended.",
        "# TYPE choirbus_console_bus_readers gauge",
        *[f'choirbus_console_bus_readers{{state="{_escape(s)}"}} {n}'
          for s, n in sorted(by_state.items())],
        "# HELP choirbus_console_bus_connects_total NATS connections the session"
        " readers established (RT-6.43), counted process-wide at the event.",
        "# TYPE choirbus_console_bus_connects_total counter",
        f'choirbus_console_bus_connects_total {happens["bus_connect"]}',
        "# HELP choirbus_console_credential_renewals_total Operator credentials"
        " renewed (RT-6.43), counted process-wide at the event.",
        "# TYPE choirbus_console_credential_renewals_total counter",
        f'choirbus_console_credential_renewals_total {happens["credential_renewal"]}',
        "# HELP choirbus_console_credential_renewal_failures_total Operator"
        " credential renewals that failed and are retried (RT-6.43),"
        " counted process-wide at the event.",
        "# TYPE choirbus_console_credential_renewal_failures_total counter",
        f'choirbus_console_credential_renewal_failures_total'
        f' {happens["credential_renewal_failure"]}',
        "# HELP choirbus_console_stats_lines_refused_total STATS lines the strict"
        " decoder refused (dropped whole, RT-6.43 step 8), counted where they drop.",
        "# TYPE choirbus_console_stats_lines_refused_total counter",
        f'choirbus_console_stats_lines_refused_total {happens["stats_line_refused"]}',
        "# HELP choirbus_console_fleet_records_refused_total Presence or ask records"
        " dropped whole (a decoding failure, or an error while processing one"
        " delivery, RT-6.43 step 6), counted where they drop.",
        "# TYPE choirbus_console_fleet_records_refused_total counter",
        f'choirbus_console_fleet_records_refused_total {happens["fleet_record_refused"]}',
        "",
        "# HELP choirbus_bus_agents The agents this session's consumers deliver, by"
        " state and harness — shown, never answered. No name, owner or home label:"
        " RT-6.43 keeps those out; an unknown word counts as other; a reading that"
        " is not live shows no series at all.",
        "# TYPE choirbus_bus_agents gauge",
        # n/a: an empty fleet shows no series, never a 0
        *(_agents(view.fleet) if live else []),
        "# HELP choirbus_bus_asks_open The open asks this session's consumers"
        " deliver. None before the login's own record, and none while the"
        " reading is not live: n/a is omitted, never 0.",
        "# TYPE choirbus_bus_asks_open gauge",
        *([f"choirbus_bus_asks_open {len(view.asks())}"] if live else []),
        "# HELP choirbus_bus_tokens_eq Tokens by category in input-token equivalents"
        " over the last 7 days, per version and harness — the same stacked bars the"
        " Reports/Stats tab shows, an estimate (RT-6.9 step 2). Only while the"
        " reading is live and n/a omitted never 0; the newest"
        f" {MAX_VERSIONS} versions keep their own label, older ones count as other."
        " No agent, owner or home label: RT-6.43 keeps those out.",
        "# TYPE choirbus_bus_tokens_eq gauge",
        *(_tokens(view.stats) if live else []),
        "",  # the exposition format ends with a newline
    ]
    return "\n".join(lines)
