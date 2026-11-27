"""Plan B's switches (ADR-014 phase 5, RT-6.39 step 5).

A switch is one per plan B piece (1 to 13) and channel, off by default. An administrator sets
it in the channel directory; the login service writes the pieces that are on for a channel on
the presence stream, `<presence>.planb.<channel>.v1`, at each credential. A running NatsBus
attaches its reading of those records as the source; `on` asks it.

Absent means off: a piece that is not switched on takes the old path, over the agent's
registrar bot. A home that is not over NATS has no source, so every piece is off there.

CB_PLANB_TEST lists piece numbers (comma separated) that are on in every channel, without a
source. It counts only while pytest runs a test; set anywhere else it is ignored.

This module imports nothing from natsbus: natsbus imports it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from . import config

log = logging.getLogger(__name__)

MAX_PIECE = 13  # piece 14, "registration ends", is no switch
TEST_ENV = "CB_PLANB_TEST"

_source: Callable[[str], frozenset[int]] | None = None
_warned: set[str] = set()


def attach(source: Callable[[str], frozenset[int]]) -> None:
    global _source
    _source = source


def detach(source: Callable[[str], frozenset[int]]) -> None:
    """Clears the source only when it is the attached one (bound methods of one object match)."""
    global _source
    if _source is not None and _source == source:
        _source = None


def _warn_once(key: str, text: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(text, *args)


def _test_pieces() -> frozenset[int]:
    raw = config.env(TEST_ENV)
    if not raw:
        return frozenset()
    if "PYTEST_CURRENT_TEST" not in os.environ:
        _warn_once("outside", "%s is for tests only; ignored", TEST_ENV)
        return frozenset()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
        except ValueError:
            n = 0
        if 1 <= n <= MAX_PIECE:
            out.add(n)
        else:
            _warn_once(f"bad:{part}", "%s: %r is not a piece number from 1 to %d; ignored",
                       TEST_ENV, part, MAX_PIECE)
    return frozenset(out)


def on(piece: int, channel: str) -> bool:
    """True when `piece` is switched on for `channel`."""
    if not isinstance(piece, int) or isinstance(piece, bool) or not 1 <= piece <= MAX_PIECE:
        return False
    if piece in _test_pieces():
        return True
    source = _source
    if source is None:
        return False
    try:
        return piece in source(channel)
    except Exception as exc:
        log.debug("plan B source failed for %s: %s", channel, exc)
        return False
