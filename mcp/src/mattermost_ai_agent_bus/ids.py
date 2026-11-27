"""Ids and names checked before they go into a chat API path (#81).

The shape checks of the adapter's ids and names, kept apart from client.py
so an entry point checks what an agent or hook passes without the adapter;
client.py re-exports them."""

from __future__ import annotations

import re

_ID = re.compile(r"[a-z0-9]{26}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


# A post of a home that runs over NATS (nats_port.py): n1:<channel name>:<stream
# sequence>. Never 26 characters of [a-z0-9], so no Mattermost id looks like it.
NATS_POST = re.compile(r"n1:(?P<channel>[a-z0-9][a-z0-9_-]{0,63}):(?P<seq>[1-9][0-9]{0,17})")

# A direct message between agents (plan B piece 3, RT-6.36 step 7): it has no channel, so
# its id says so (`n1:@dm:<stream sequence>`) and no channel's post can carry it.
NATS_DM = re.compile(r"n1:@dm:(?P<seq>[1-9][0-9]{0,17})")


def dm_seq(value: object) -> int | None:
    """The stream sequence of a direct message's id, None for any other id."""
    m = NATS_DM.fullmatch(value) if isinstance(value, str) else None
    return int(m["seq"]) if m else None


class InvalidId(ValueError):
    """An id or name that must not go into a Mattermost API path (#81)."""


def mm_id(value: object, what: str) -> str:
    """`value` if it is a Mattermost id (26 lowercase letters and digits), else
    InvalidId: nothing an agent or hook passes reaches an API path unchecked."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidId(f"invalid {what} id")
    return value


def mm_name(value: object, what: str) -> str:
    """`value` if it is a safe team, channel, user or agent name for an API
    path: letters, digits, '.', '_', '-', no '..', at most 64; else InvalidId.
    A DM channel's name "<id>__<id>" passes."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise InvalidId(f"invalid {what} name")
    return value


def post_id_arg(value: object) -> str:
    """`value` if it is a Mattermost post id or a NATS post id (nats_port.py),
    else InvalidId: what a tool or hook may name as a post. mm_id stays strict:
    the Mattermost adapter never gets a NATS id."""
    if isinstance(value, str) and NATS_POST.fullmatch(value):
        return value
    return mm_id(value, "post")
