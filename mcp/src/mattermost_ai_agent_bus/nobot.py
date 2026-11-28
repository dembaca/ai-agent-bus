"""Registration ends, all or nothing: the decision (plan B piece 14a, ADR-014; RT-6.18 step 13,
RT-6.10 step 8).

A session whose every channel - the home, each guest channel, the lobby and the stats channel -
runs over NATS with every plan B piece on registers no Mattermost bot and holds no Mattermost
token. With any channel or piece off it registers as before. The answer comes from the login
service's records alone, as this session's NATS bus read them (`NatsPort.bot_needed`): never
from the environment, CB_PLANB_TEST included. Until piece 14c the predicate always names a
reason (CROWN_NEEDS_BOT, RT-6.6 step 13): the crown still needs the bot's identity.

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

# Fail closed toward registering until plan B piece 14c: the crown (RT-6.6, RT-6.38) still needs
# the bot's identity - the lease heartbeat and its NATS beat run only with a token, the crown
# claim carries the bot's user id - so a session without a bot would publish no crown claim and
# a fully switched home would have no coordinator. While this is True, `NatsPort.bot_needed`
# names CROWN_REASON even when every channel and piece runs over NATS: the decision logic is
# built and tested, but every session still registers, and `decide` answers CROWN_REASON at once
# instead of waiting up to DECIDE_SEC for a read whose answer is known. Piece 14c (the identity on
# the NATS name) deletes this constant and its checks.
CROWN_NEEDS_BOT = True
CROWN_REASON = "the crown needs the bot's identity until piece 14c"

# The decision once taken without a bot: why (the error text of what needs Mattermost).
_held = ""
# The host process's decision still open, or its registration failed (host_decide): the text of
# what needs Mattermost meanwhile; '' once registered or decided without a bot.
_note = ""
WAITING = ("no Mattermost bot yet: the decision about a Mattermost bot is not taken yet, up to 60 s "
           "after the start (plan B piece 14, RT-6.18 step 13); try again in a minute, bus_info says "
           "when it is taken")
REGISTERING = ("a Mattermost bot is being registered, try again in a moment (plan B piece 14, RT-6.18 "
               "step 13)")
FAILED = ("no Mattermost bot: registering the bot failed ({why}; plan B piece 14, RT-6.18 step 13), so "
          "this session runs over NATS alone: restart the session")


def reason() -> str:
    """Why this session holds no Mattermost bot; '' when it registered or has not decided."""
    return _held


def held() -> bool:
    return bool(_held)


def status() -> str:
    """Why this session has no Mattermost bot now: decided without one (`reason`), the host
    process's decision still open (WAITING), or its registration failed (FAILED); '' with a
    bot, and outside a host process before `session_start` decided (bus_info's no_bot)."""
    return _held or _note


def reset() -> None:
    """Tests only."""
    global _held, _note
    _held = _note = ""


def _set_note(client: Any, text: str) -> None:
    """The host process's open or failed decision, on the adapter too: what needs Mattermost
    fails with it (client.MattermostAdapter._auth_headers) instead of naming a tool the sandbox
    does not have."""
    global _note
    _note = text
    _show(client, text)


def _set_registering(inner: Any, on: bool) -> None:
    try:
        inner.registering = on
    except AttributeError:  # a fake without the field
        pass


def _show(client: Any, text: str) -> None:
    try:
        _inner(client).no_bot = text
    except AttributeError:  # a fake without the field
        pass


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
    never raises. Keeps a decision without a bot (`hold`). Until piece 14c it answers
    CROWN_REASON at once: nothing waits for a read that could only end in a registration."""
    if CROWN_NEEDS_BOT:  # piece 14c deletes this check with the constant
        return CROWN_REASON
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
    _show(client, _held)
    log.info("registration: no Mattermost bot for this session, every channel and piece runs over NATS")


async def register_now(client: Any, name: str, display: str) -> dict[str, Any]:
    """Register the bot as `session_start` does (the host process, RT-6.18 step 13): the
    registrar, the joins of the home and the guests, the attestation, the bot linked to the NATS
    presence. Raises what the registration raised."""
    from . import attest, stats

    info = await client.session_start(name, display or None, unique=False)
    if not record_token(info):
        # Not in the session file, the stop hook would read "no token" and remove the file, and
        # neither it nor the sweep could unregister the bot: unregister it now, and fail.
        try:
            await client.session_end(name)
        except Exception as exc:  # noqa: BLE001 — logged, the registration fails all the same
            log.warning("registration: the unrecorded bot is not unregistered: %s", type(exc).__name__)
        raise RuntimeError("the bot could not be recorded in the session file")
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


def record_token(info: Any) -> bool:
    """The bot the host process registered, in the corral session file the moment the
    registrar answered (RT-6.18 step 13): the host-side 0600 file preStart wrote and marked
    DEFERRED=1, appended to (the last line of a key wins, as mm_kv reads it), never through a
    symlink. postEnd unregisters the bot with it when the exit work did not, and
    mm-agent-sweep.sh when postEnd could not either (the registrar unreachable, the process
    killed): the state directory hosted/<key>/ alone would not do, postEnd removes it. Nothing
    without corral (no such file), and nothing once postEnd removed the file. True when the bot
    is recorded or there is no corral session file to record it in; False when it is not
    (register_now then unregisters it at once)."""
    from . import session_file

    path = session_file.corral_session_file()
    if path is None:
        return True
    vals = (info.username, info.user_id, info.bot_token)
    if not all(isinstance(v, str) and v and not any(c in v for c in "\0\r\n") for v in vals):
        log.warning("registration: the registrar's answer is not one line each; not in the session file")
        return False
    if path.is_symlink() or not path.is_file():
        log.warning("registration: no session file to record the bot in (%s)", path.name)
        return False
    user, uid, tok = vals
    try:
        session_file.append_private(path, create=False, text=(
            "# registered by the host process (plan B piece 14a, RT-6.18 step 13)\n"
            f"CB_MM_BOT_USERNAME={user}\nMM_BOT_USERNAME={user}\n"
            f"CB_MM_BOT_USER_ID={uid}\nMM_BOT_USER_ID={uid}\n"
            f"CB_MM_BOT_TOKEN={tok}\nMM_BOT_TOKEN={tok}\nMATTERMOST_TOKEN={tok}\n"))
    except OSError as exc:
        log.warning("registration: the bot is not in the session file: %s", type(exc).__name__)
        return False
    return True


async def host_decide(client: Any, name: str, display: str, secret: str,
                      timeout: float | None = None) -> str:
    """The host process of a session preStart started without a bot (RT-6.18 step 13): decide,
    then register with the registration secret preStart passed, or forget it. Returns what it
    did, for the log; never raises. Until it is done, and after a failed registration, what
    needs Mattermost fails with WAITING or FAILED (`status`, the adapter's no_bot)."""
    inner = _inner(client)
    _set_note(client, WAITING)
    try:
        why = await decide(client, timeout)
        if not why:
            _set_note(client, _held)
            return "no bot: every channel and piece runs over NATS"
        log.info("registration: a bot is needed (%s): registering %s", why, name)
        inner.reg_secret = secret or None
        # The routes say "being registered" until the token is in; session_start goes on (registering).
        _set_note(client, REGISTERING)
        _set_registering(inner, True)
        try:
            await register_now(client, name, display)
        finally:
            inner.reg_secret = None  # the host process keeps no registration secret (RT-6.18)
            _set_registering(inner, False)
        _set_note(client, "")
        return f"registered ({why})"
    except asyncio.CancelledError:
        inner.reg_secret = None
        _set_registering(inner, False)
        # Cancelled after the registrar answered but before register_now recorded the bot (in
        # session_start, between _take and the profile check): record it now, so the stop hook
        # or the sweep can still unregister it.
        session = getattr(inner, "session", None)
        if session is not None:
            record_token(session)
        raise
    except Exception as exc:  # noqa: BLE001 — the session goes on without a bot, NATS only
        inner.reg_secret = None
        _set_registering(inner, False)
        _set_note(client, FAILED.format(why=type(exc).__name__))
        log.warning("registration failed: %s: the session goes on without a bot", exc)
        return f"registration failed ({type(exc).__name__})"
