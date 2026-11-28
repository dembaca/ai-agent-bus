"""Registration ends, all or nothing: the decision (plan B piece 14a, ADR-014; RT-6.18 step 13,
RT-6.10 step 8).

A session whose every channel - the home, each guest channel, the lobby and the stats channel -
runs over NATS with every plan B piece on registers no Mattermost bot and holds no Mattermost
token. With any channel or piece off it registers as before. The answer comes from the login
service's records alone, as this session's NATS bus read them (`NatsPort.bot_needed`): never
from the environment, CB_PLANB_TEST included.

Who decides: the host process of a corral session that preStart started without a bot (it
registers one only when its own NATS read, on the login the relay passes it, shows a channel or
piece off: `host_decide`), and the MCP server itself without corral, when `session_start` is
called (`decide` there). The same code path both times.

Fail safe in one direction only: what cannot be decided - no NATS side, no login within
DECIDE_SEC, the bus not synced, a stale or missing record, any error - registers as before.
Never the other way: a doubtful read never skips the registration.

The decision is taken once and kept. A session started without a bot never registers one later:
a record that goes stale or is switched off while it runs leaves it without Mattermost, and what
then takes the old path fails with `reason()`'s text (client.MattermostAdapter._auth_headers).

Only a session that may be fully switched waits for the decision at all (`deferrable`): one whose
every channel is listed in CB_NATS_HOMES, the opt-in for "this channel runs over NATS". Any
other registers at once, as before 14a.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import planb

log = logging.getLogger(__name__)

ALL_PIECES = frozenset(range(1, planb.MAX_PIECE + 1))
# How long a session that may be fully switched waits for its NATS read before it registers as
# before: the relay connects within seconds of the start, and a stored login needs no approval.
DECIDE_SEC = 60.0
POLL_SEC = 0.1

# The decision once taken without a bot: why (the error text of what needs Mattermost).
_held = ""


def reason() -> str:
    """Why this session holds no Mattermost bot; '' when it registered or has not decided."""
    return _held


def held() -> bool:
    return bool(_held)


def reset() -> None:
    """Tests only."""
    global _held
    _held = ""


def _port(client: Any) -> Any:
    from . import compose

    return compose.nats_port_of(client)


def _inner(client: Any) -> Any:
    """The Mattermost adapter inside the wrappers compose builds (Remembered, NatsPort)."""
    from .compose import Remembered
    from .nats_port import NatsPort

    if isinstance(client, Remembered):
        client = client.port
    if isinstance(client, NatsPort):
        client = client.inner
    return client


def has_bot(client: Any) -> bool:
    """A bot is there already: handed in (a token) or registered or taken over (a session)."""
    inner = _inner(client)
    return getattr(inner, "session", None) is not None or bool(getattr(inner, "token", None))


def session_channels(client: Any) -> list[str]:
    """The session's channels by name, lower case: the home, the guests, the lobby."""
    inner = _inner(client)
    names = [getattr(inner, "work_channel_name", ""), *getattr(inner, "guest_channel_names", ()),
             getattr(inner, "channel_name", "")]
    return list(dict.fromkeys(n.strip().lstrip("~").lower() for n in names if n and n.strip()))


def deferrable(client: Any, environ: Any = None) -> bool:
    """Whether this session may be fully switched, so that its registration waits for the
    decision: a NATS side (compose built a NatsPort), no bot yet, and every channel of the session
    listed in CB_NATS_HOMES. Only a pre-filter, read from the host's settings: it can only make a
    session wait, never skip a registration (that takes `NatsPort.bot_needed`)."""
    from . import compose

    if _port(client) is None or has_bot(client):
        return False
    listed = compose.nats_homes_from_env(environ)
    chans = session_channels(client)
    return bool(chans) and all(c in listed for c in chans)


async def decide(client: Any, timeout: float | None = None) -> str:
    """'' when this session needs no Mattermost bot, else why it does (it then registers as
    before). Waits up to `timeout` for the NATS bus to be up and to have read the plan B records;
    never raises. Keeps a decision without a bot (`hold`)."""
    if _held:
        return ""
    port = _port(client)
    if port is None:
        return "no channel runs over NATS"
    loop = asyncio.get_running_loop()
    end = loop.time() + max(0.0, DECIDE_SEC if timeout is None else timeout)
    why = "the plan B records are not read"
    while True:
        try:
            why = port.bot_needed()
        except Exception as exc:  # noqa: BLE001 — in doubt a bot, as before
            why = f"the decision failed ({type(exc).__name__})"
        if not why:
            hold(client)
            return ""
        if why != port.NOT_READ or loop.time() >= end:
            return why
        await asyncio.sleep(POLL_SEC)


def hold(client: Any) -> None:
    """Keep the decision: no bot for the rest of this session. What still takes the old path
    fails with `reason()`'s text."""
    global _held
    _held = ("no Mattermost bot in this session: every channel and plan B piece ran over NATS when "
             "it started (plan B piece 14, RT-6.18 step 13, RT-6.10 step 8); this needs Mattermost, "
             "so a switch went off or its record went stale since: restart the session to register one")
    inner = _inner(client)
    try:
        inner.no_bot = _held
    except AttributeError:  # a fake without the field
        pass
    log.info("registration: no Mattermost bot for this session, every channel and piece runs over NATS")


async def register_now(client: Any, name: str, display: str) -> dict[str, Any]:
    """Register the bot as `session_start` does (the host process, RT-6.18 step 13): the
    registrar, the joins of the home and the guests, the attestation, the bot linked to the NATS
    presence. Raises what the registration raised."""
    from . import attest, stats

    info = await client.session_start(name, display or None, unique=False)
    stats.set_user(info.user_id)
    setup = await client.finish_session_setup()
    await attest.start(client)
    port = _port(client)
    if port is not None:
        try:
            port.link_bot(info.user_id, info.username)
        except Exception as exc:  # noqa: BLE001 — a label of the presence
            log.info("nats: the registered bot is not linked: %s", type(exc).__name__)
    return dict(setup)


async def host_decide(client: Any, name: str, display: str, secret: str,
                      timeout: float | None = None) -> str:
    """The host process of a session preStart started without a bot (RT-6.18 step 13): decide,
    then register with the registration secret preStart passed, or forget it. Returns what it
    did, for the log; never raises."""
    inner = _inner(client)
    try:
        why = await decide(client, timeout)
        if not why:
            return "no bot: every channel and piece runs over NATS"
        log.info("registration: a bot is needed (%s): registering %s", why, name)
        inner.reg_secret = secret or None
        try:
            await register_now(client, name, display)
        finally:
            inner.reg_secret = None  # the host process keeps no registration secret (RT-6.18)
        return f"registered ({why})"
    except asyncio.CancelledError:
        inner.reg_secret = None
        raise
    except Exception as exc:  # noqa: BLE001 — the session goes on without a bot, NATS only
        inner.reg_secret = None
        log.warning("registration failed: %s: the session goes on without a bot", exc)
        return f"registration failed ({type(exc).__name__})"
