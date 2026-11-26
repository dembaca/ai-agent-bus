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
needs — no anonymous scrape, no token of its own and no new NATS right
(#158's open question, the ask in the pull request). A series a value is
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

if TYPE_CHECKING:  # for the type-checker only; no runtime import
    from .nats_read import PersonReader, View

WEEK_SEC = 7 * 86400

# The bounded label sets (every label value comes from one of these):
# the presence claim's state words and the columns the view adds.
STATES = ("online", "away", "idle", "listening", "working", "bus", "offline", "off",
          "waiting")
UNKNOWN = "other"

# The routes requests are counted by: the api's own paths, the flows and
# the fixed assets; anything with a variable tail is its bucket, anything
# else "other". Never a name, an id or a file of the app.
_ROUTES = ("/", "/livez", "/login", "/callback", "/logout", "/metrics",
           "/v1/me", "/v1/fleet", "/v1/asks", "/v1/stats", "/v1/graph",
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
    """The console's own request counters, written on the event loop."""

    def __init__(self) -> None:
        # (route, method, status) → count; the keys come from the bounded sets
        self.requests: dict[tuple[str, str, int], int] = defaultdict(int)

    def note(self, route: str, method: str, status: int) -> None:
        """One served request, by its bucket, method and status."""
        self.requests[(route, method, status)] += 1

    def render(self) -> list[str]:
        """The counter series, one line each, in order."""
        return [f'console_http_requests_total{{route="{r}",method="{m}",status="{s}"}} {n}'
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
    return [f'bus_agents{{state="{_escape(s)}",harness="{_escape(h)}"}} {n}'
            for (s, h), n in sorted(by.items())]


def _tokens(book: Any) -> list[str]:
    """The tokens by category of the last 7 days, per version and harness —
    the tab's stacked bars, one series each, an estimate (RT-6.9 step 2)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for _at, _seq, line in book.within(WEEK_SEC):
        groups[(str(line.get("v") or UNKNOWN), str(line.get("rt") or UNKNOWN))].append(line)
    out: list[str] = []
    for (version, harness), group in sorted(groups.items()):
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
                out.append(f'bus_tokens_eq{{category="{category}",version="{_escape(version)}",'
                           f'harness="{_escape(harness)}"}} {value}')
    return out


def render(tel: Telemetry, sessions: int, streams: int,
           readers: "list[PersonReader]", view: "View") -> str:
    """The whole page: the console's own observations first, then the agent
    numbers of the scrape session's own reading — the fleet and the STATS
    book its consumers deliver, the same data the Reports/Stats tab shows
    that operator. Counters can only grow under a scraper that polls."""
    connects = sum(r.connects for r in readers)
    renewals = sum(getattr(r, "renewals", 0) for r in readers)
    failures = sum(getattr(r, "renewal_failures", 0) for r in readers)
    by_state: dict[str, int] = defaultdict(int)
    for reader in readers:
        by_state[_word(reader.state, STATES)] += 1
    refused_stats = int(getattr(view.stats, "refusals", 0) or 0)
    refused_records = int(getattr(view.fleet, "refusals", 0) or 0)

    lines = [
        "# HELP console_http_requests_total HTTP requests by bucketed route, method and status.",
        "# TYPE console_http_requests_total counter",
        *tel.render(),
        "# HELP console_sessions_open Operator sessions held.",
        "# TYPE console_sessions_open gauge",
        f"console_sessions_open {sessions}",
        "# HELP console_sse_streams_open Open fleet streams (server-sent events).",
        "# TYPE console_sse_streams_open gauge",
        f"console_sse_streams_open {streams}",
        "# HELP console_bus_readers The session readers' NATS connection state.",
        "# TYPE console_bus_readers gauge",
        *[f'console_bus_readers{{state="{_escape(s)}"}} {n}' for s, n in sorted(by_state.items())],
        "# HELP console_bus_connects_total NATS connections the session readers established.",
        "# TYPE console_bus_connects_total counter",
        f"console_bus_connects_total {connects}",
        "# HELP console_credential_renewals_total Operator credentials renewed (RT-6.43).",
        "# TYPE console_credential_renewals_total counter",
        f"console_credential_renewals_total {renewals}",
        "# HELP console_credential_renewal_failures_total Operator credential renewals"
        " that failed and are retried (RT-6.43).",
        "# TYPE console_credential_renewal_failures_total counter",
        f"console_credential_renewal_failures_total {failures}",
        "# HELP console_stats_lines_refused_total STATS lines the strict decoder"
        " refused (dropped whole, RT-6.43 step 8).",
        "# TYPE console_stats_lines_refused_total counter",
        f"console_stats_lines_refused_total {refused_stats}",
        "# HELP console_fleet_records_refused_total Presence records the strict decoder"
        " refused (dropped whole, RT-6.43 step 6).",
        "# TYPE console_fleet_records_refused_total counter",
        f"console_fleet_records_refused_total {refused_records}",
        "",
        "# HELP bus_agents The agents this session's consumers deliver, by state and"
        " harness, shown never answered. No name, owner or home label: RT-6.43 keeps"
        " those out; an unknown word counts as other.",
        "# TYPE bus_agents gauge",
        # n/a: an empty fleet shows no series, never a 0
        *_agents(view.fleet),
        "# HELP bus_asks_open The open asks this session's consumers deliver,"
        " none before the login's own record (n/a omitted, never 0).",
        "# TYPE bus_asks_open gauge",
        f"bus_asks_open {len(view.asks())}",
        "# HELP bus_tokens_eq Tokens by category in input-token equivalents over the"
        " last 7 days, per version and harness — the same stacked bars the"
        " Reports/Stats tab shows, an estimate (RT-6.9 step 2), n/a omitted never 0."
        " No agent, owner or home label: RT-6.43 keeps those out.",
        "# TYPE bus_tokens_eq gauge",
        *_tokens(view.stats),
        "",  # the exposition format ends with a newline
    ]
    return "\n".join(lines)
