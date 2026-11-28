"""The bus's read watermark (RT-6.2 in docs/arc42/06-runtime-view.md): per
channel, the newest post this bot has handled. What came after it is what
may still wait; Mattermost's unread counters decide nothing.

Handled means handed to the agent (wait_for_events, the channel push), gated
(RT-6.16), or the bot's own. A channel without a line reads from the bot's
creation: nothing older can be for this session.

``read-<uid>.env`` in the private state directory, 0600, one line per channel:
``<channel_id> <post_id> <create_at>``. Keyed on the bot, not the session, so
the next server of the same bot (``claude --continue``, the workspace's next
server, RT-6.10) goes on from it; it goes when the bot is unregistered, and
with the host's state directory (RT-6.18).

A mark only moves forward, except for hold(): a post that reaches the inbox
behind the mark (it was on its way while the bot posted) pulls it back, so it
counts as pending until it is handed over.

A home over NATS (RT-6.36) has two lanes, two marks: its Mattermost posts
(people, asks, a BYE) under the channel id, its NATS posts under
``<channel_id>:nats`` (lane()). Each post carries its own server's time, and
the two clocks are never compared: a NATS post handled with a time ahead of
Mattermost's never moves past a person's post that comes later.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from . import session_file
from .ids import NATS_POST

log = logging.getLogger(__name__)

_ID = re.compile(r"[a-z0-9]{1,64}")
NATS_LANE = ":nats"
_KEY = re.compile(r"[a-z0-9]{1,64}(?::nats)?")


def lane(channel_id: str, post_id: str) -> str:
    """The mark a post moves: its channel's, or for a post that came over NATS
    the channel's NATS lane."""
    return f"{channel_id}{NATS_LANE}" if channel_id and NATS_POST.fullmatch(post_id or "") else channel_id


class Mark(NamedTuple):
    post_id: str
    at: int


def path(uid: str) -> Path:
    return session_file.state_dir() / f"read-{session_file.state_key(uid)}.env"


def read(uid: str) -> dict[str, Mark]:
    marks: dict[str, Mark] = {}
    try:
        lines = path(uid).read_text().splitlines() if uid else []
    except OSError:
        return marks
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and _KEY.fullmatch(parts[0]) and parts[2].isdigit():
            marks[parts[0]] = Mark(parts[1], int(parts[2]))
    return marks


def at(uid: str, channel_id: str) -> int:
    """The create_at of the channel's newest handled post, 0 for none."""
    mark = read(uid).get(channel_id)
    return mark.at if mark else 0


def _write(uid: str, marks: dict[str, Mark]) -> None:
    text = "".join(f"{cid} {m.post_id or '-'} {m.at}\n" for cid, m in sorted(marks.items()))
    try:
        session_file.write_private(path(uid), text)
    except OSError as exc:  # the mark stays behind: posts count once more, none is lost
        log.info("read watermark not written: %s", exc)


def _stored(post_id: str) -> bool:
    """What a mark keeps as its post id: a Mattermost id, or a NATS post's
    `n1:<channel>:<seq>` (RT-6.36: the next server derives its `since` from it, #213)."""
    return bool(_ID.fullmatch(post_id or "") or NATS_POST.fullmatch(post_id or ""))


def advance(uid: str, channel_id: str, post_id: str, created: int) -> bool:
    """Move the channel's mark to this post, if it is newer than the mark."""
    if not (uid and channel_id and _KEY.fullmatch(channel_id) and created > 0):
        return False
    marks = read(uid)
    if created <= marks.get(channel_id, Mark("", 0)).at:
        return False
    marks[channel_id] = Mark(post_id if _stored(post_id) else "-", created)
    _write(uid, marks)
    return True


def hold(uid: str, channel_id: str, created: int, post_id: str = "") -> None:
    """A post created at `created` waits unhandled: keep the mark before it. A NATS
    post keeps the sequence one before its own, `n1:<channel>:<seq-1>`, since the
    next server derives its `since` from the mark (#213): a held post then asks for
    itself again, not for the channel's whole absence. A channel without a mark gets
    one at its first NATS post (the same sequence before it), so a session killed
    before its first hand-over still leaves a cursor for the next server (#224); a
    NATS post of stream sequence 1 has none - no mark can name sequence 0 - and a
    Mattermost lane stays without a mark until its first hand-over."""
    if created <= 0:
        return
    marks = read(uid)
    mark = marks.get(channel_id)
    parsed = NATS_POST.fullmatch(post_id or "")
    if mark is not None and mark.at < created:
        return
    if parsed is None:
        if mark is None:
            return  # nothing to keep for a Mattermost lane before its first hand-over
        marks[channel_id] = Mark("-", created - 1)
    else:
        seq = int(parsed.group("seq"))
        if seq <= 1:
            if mark is None:
                return  # no sequence before the stream's first message
            marks[channel_id] = Mark("-", created - 1)
        else:
            marks[channel_id] = Mark(f"n1:{parsed.group('channel')}:{seq - 1}", created - 1)
    _write(uid, marks)


def remove(uid: str) -> None:
    """Forget the marks: under the state key and under the bot id (the copy, RT-6.2 step 9)."""
    if uid:
        path(uid).unlink(missing_ok=True)
        (session_file.state_dir() / f"read-{uid}.env").unlink(missing_ok=True)
