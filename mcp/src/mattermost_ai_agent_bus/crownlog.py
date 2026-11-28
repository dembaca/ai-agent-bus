"""The crown log (RT-6.6 step 11, #64): one JSON line for each time this agent's
crown passes, to it or from it, in `crown-<uid>.log` in the state directory.

The server's own log lines go to stderr, which nobody keeps, so a lost crown
left no trace of which rule took it. Each line holds a time, `lost` or
`gained`, the way (a fixed word), and what the sign-of-life rule saw then:
whether a turn ran, since when the agent showed no sign, how long the oldest
push waited untaken. The id of the agent that took the crown over, if one did. Never a
post's text or id. Past LOG_LINES the newest LOG_KEEP stay, as in the relay log
(RT-6.13 step 6). Never raises: the crown never waits on its diagnostics.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from . import life, session_file, stats

log = logging.getLogger(__name__)

LOG_LINES = 500
LOG_KEEP = 400

# The ways a crown passes. Lost: its holder fell silent (ADR-007 amendment step 1),
# another agent took it over for a missing receipt (ADR-007 step 4), a release, the
# younger of two crowns, a crown claimed for another home or another holder over NATS,
# and a lease that ran out without any of these (the beat did not renew it).
LOST = frozenset({"silent", "taken_over", "release", "two_crowns", "home", "nats", "lapsed"})
# Gained: a take, the tick's election, a slot after the holder went (ADR-007 step 2),
# a takeover over a missing receipt (step 3), the claim order of a home over NATS (RT-6.38).
GAINED = frozenset({"take", "election", "slot", "receipt", "nats"})

_explained = True  # the last loss has its line; a gain arms the next one


def log_path() -> Path | None:
    """`crown-<uid>.log` in the state directory, None without a bot id."""
    key = session_file.state_key(stats.bot_id())
    return session_file.state_dir() / f"crown-{key}.log" if key else None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def note(event: str, way: str, by: str = "", since: float = 0.0) -> None:
    """One line: `event` lost or gained, `way` from LOST or GAINED, `by` the
    user id of the agent that took it over (only then), `since` when the agent
    showed its last sign (the silent drop). Never raises."""
    global _explained
    if event == "lost":
        _explained = True
    elif event == "gained":
        _explained = False
    try:
        if way not in (LOST if event == "lost" else GAINED if event == "gained" else ()):
            raise ValueError(f"no such crown event: {event} {way}")
        path = log_path()
        if path is None:
            return
        now = time.time()
        entry: dict[str, object] = {"t": _iso(now), "event": event, "way": way,
                                    "turn": life.in_turn()}
        if by:
            entry["by"] = re.sub(r"[^\w.-]", "", by)[:64]
        if since:
            entry["since"] = _iso(since)
        untaken = life.untaken_since()
        if untaken:
            entry["untaken_sec"] = int(now - untaken)
        session_file.append_private(path, json.dumps(entry) + "\n")
        lines = path.read_text().splitlines(keepends=True)
        if len(lines) > LOG_LINES:
            session_file.write_private(path, "".join(lines[-LOG_KEEP:]))
    except Exception as exc:  # noqa: BLE001 — a diagnostic, never part of the crown
        log.warning("crown log not written: %s", type(exc).__name__)


def lapsed() -> None:
    """The heartbeat saw the crown end: a line `lapsed` unless the loss has its own."""
    if not _explained:
        note("lost", "lapsed")


def reset() -> None:
    """A new process's state (tests)."""
    global _explained
    _explained = True
