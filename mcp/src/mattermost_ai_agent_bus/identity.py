"""The session's own identity: who "me" is (plan B piece 14c-2a; RT-6.2 step 9, RT-6.6, RT-6.38).

Every site that decides "is this me, my state, may I act" asks here, not Mattermost's users/me:
`self_key` is the key of the session's state and of its own posts, `self_name` the name it is
mentioned by, `self_me` both as a Me.

With a Mattermost bot every answer is the bot's, exactly as before: its user id (the session,
CB_MM_BOT_USER_ID, else users/me) and its username (users/me). Without one - the session decided
to register none (nobot.held: every channel and plan B piece runs over NATS, RT-6.18 step 13) -
the key is the session's own NATS name, the rule NatsPort._uid applies to the own sender, and
the name is the bridge's bot of that name (NatsPort.owned_link, as roster_rows names it), else
the NATS name itself. Until piece 14c flips nobot.CROWN_NEEDS_BOT no session decides so: every
session registers and nothing here changes what it does. The sites still allowed to read users/me
are listed, each with its reason, in mcp/tests/test_planb_piece14c2a.py.
"""

from __future__ import annotations

from typing import Any

from . import config, session_file
from .model import Me


def bot_less(client: Any) -> bool:
    """The session decided to run without a Mattermost bot (nobot.held) and holds none."""
    from . import nobot

    return nobot.held() and not nobot.has_bot(client)


def nats_name(client: Any) -> str:
    """The session's own NATS name ('' without a NATS bus, or one that is no state key)."""
    from .compose import nats_port_of

    port = nats_port_of(client)
    if port is None:
        return ""
    try:
        name = port.own_name()
    except Exception:  # noqa: BLE001 — no bus: no name
        return ""
    return name if name and session_file.key_ok(name) else ""


def _shown(client: Any, name: str) -> str:
    """The username people see the NATS name `name` by: the bridge's bot of the name in the
    home while its record is the name's link (NatsPort.owned_link), else the name."""
    from .compose import nats_port_of

    port = nats_port_of(client)
    home = str(getattr(client, "work_channel_name", "") or "").strip().lstrip("~").lower()
    try:
        rec = port.owned_link(name, home) if port is not None and home else None
    except Exception:  # noqa: BLE001 — a label: the name
        rec = None
    return rec.username if rec is not None and rec.username else name


def key_now(client: Any) -> str:
    """self_key without a request, for the synchronous comparisons: the bot id as far as it is
    known (client.bot_id, as before), else the NATS name of a session without a bot, else ''."""
    if bot_less(client):  # decided without a bot: a stale CB_MM_BOT_USER_ID names no one
        return nats_name(client)
    return str(getattr(client, "bot_id", "") or "")


async def self_key(client: Any) -> str:
    """The session's own key: the bot's Mattermost id when there is a bot (its session, then
    CB_MM_BOT_USER_ID, then users/me, as lease._user_id always read it), else its NATS name."""
    if bot_less(client) and (name := nats_name(client)):  # before the environment value: a stale
        return name  # CB_MM_BOT_USER_ID must not split the identity of a session without a bot
    session = getattr(client, "session", None)
    if session:
        return str(session.user_id)
    got = config.env("CB_MM_BOT_USER_ID")
    if got:
        return got
    return str((await client.get_me()).id)


async def self_me(client: Any) -> Me:
    """The session's own Me: users/me with a bot (unchanged), else id the NATS name and
    username the name it is mentioned by (_shown)."""
    if bot_less(client) and (name := nats_name(client)):
        return Me(id=name, username=_shown(client, name))
    me: Me = await client.get_me()
    return me


async def self_name(client: Any) -> str:
    """The name the session is mentioned by: the bot's username with a bot, else _shown."""
    return (await self_me(client)).username
