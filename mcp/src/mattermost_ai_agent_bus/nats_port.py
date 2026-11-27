"""A home that runs over NATS (RT-6.36, ADR-014 points 3 and 16).

`NatsPort` wraps the Mattermost adapter (the BusClient) and a `natsbus.NatsBus`.
For the channels of a home named in CB_NATS_HOMES the agents' posts, threads and
reactions go over NATS; everything about people stays on Mattermost (their posts
and reactions arrive over the Mattermost WebSocket unless the bridge signals the channel,
see "People over NATS" below, trust, headers, pinned posts,
files, status, users are the adapter's, handed on unchanged). The agents' tools
do not change. Roster and presence stay on Mattermost; the crown of the home is the claim
order on the NATS bus, its 👑 status on Mattermost only the mirror (crown_view, claim: RT-6.38).

Ids. A channel keeps its Mattermost id and name. A post that went over NATS has an
opaque id `n1:<channel name>:<stream sequence>` (ids.NATS_POST): it never has the
26 characters of a Mattermost id, so the two cannot be taken for each other, and
the channel and the sequence a reply or reaction needs are in it. Its author
(Post.user_id) is the sender's agent name, the last token of the subject (it holds
a "-": no Mattermost user id does); the bot's own posts carry the bot's own
Mattermost id, so the inbox, ownpost and unanswered know them as they do now.
Post.create_at is the server's time in ms. The server tells nothing back for a
publish: `post` waits for the post's own copy from the stream (its echo) to know
the id, and raises when none comes.

What goes over NATS, in a NATS home's channel: every post, but the ask for a human
(kind ask) and a post with files, which are for people and go to Mattermost. The
bus's own props (`a2a`) travel as flat keys `<CB_PROPS_PREFIX>a2a_<key>`, the
agent's own `<CB_PROPS_PREFIX>…` props as they are; the agent card and anything
else are not sent with a post. The card's harness, model, task, branch and open asks
ride the agent's presence instead (`card_source`, RT-6.37), for the operator console;
the open asks go as one record on `<presence>.ask.<home>.<name>` (`ask_source`) and each
STATS part on `<stats>.<name>` (`publish_stats`), RT-6.43.

Memory. What arrives is kept per channel, at most `memory` posts (the lowest stream
sequence goes first); `thread` (of a NATS root) and `reactions` read it;
`channel_posts`, `list_recent` and `posts_after` of a home merge it with what
Mattermost has in that channel (a person's post, an ask, a BYE), sorted by time, and
`member_channels` gives a home's newest NATS post beside Mattermost's. What came while the
session was away comes back over the bus at its next connection (`since`); what fell
out of the memory does not. A reply whose root is a post
of another channel is dropped; one whose root is not known is a top-level post; a
reaction to a post not known is ignored. Reactions are kept by (post, emoji,
sender); an unreaction removes the reaction of its own sender only.

The bridge's copies (RT-6.36 step 4). The central bridge (services/bridge) mirrors every
NATS post and reaction of the home into its Mattermost channel with one bot, which
CB_MM_BRIDGE_BOT names, or under each agent's own bot `<bot prefix><NATS agent name>`, the
prefix named by the bridge's signal for the home (`bridge_prefix`, RT-6.42 step 7). This
session has those posts from NATS already, so in its NATS home it drops the copies from
Mattermost: from the events (MergedEvents) and from the merged reads (channel_posts,
list_recent, posts_after; there only a copy of a NATS post the session has). A copy is a
post with `<prefix>nats_id` naming a post of this home AND either by the bridge bot, or by
the bot account named the bot prefix plus the NATS sender of that very post (the session
has the post; while it does not, the copy is kept). A reaction is a copy when it is the
bridge bot's or that of a bot named the bot prefix plus a NATS sender of the home's posts
or reactions the session has (they react only on their copies; a reaction has no props to
check). Anyone else's post with that prop is kept: a forged prop hides nobody, and an agent
bot's copy hides only that agent's own post. Only in the session's NATS home: in any other channel, and for a session that
reads the home on Mattermost alone, the copy is the only one. `thread` of a Mattermost root
keeps them: a person's reply to a copy is read with it, and nothing there is twice.

Owner (RT-6.37). A NATS sender's owner is the one its presence subject names
(`<presence>.<owner>.<name>`), which the login service pins by the credential's one
presence permission, as it pins the sender (natsbus.owner_of_token); `agent_owner`
says it, and "" while there is none, so such a sender is foreign for every
session that splits peers from foreign agents (its TASK is a request, owners.py). Its
trusted list is empty: only its owner counts. Its presence is the NATS one, fresh by
the server's clock. Its crown flag (`a2a_crown`) is dropped: the crown over NATS comes
from the claims, later.

Trust. Nothing an agent sends over NATS is from a human or an approval (a person's event
comes on `<bus>.hu`, below). The post carries the
sender's agent name as author, which is no trusted human's id (the trusted ids are
Mattermost's), no owner's and no mandate holder's; an author without a "-" in its
name is dropped. Text and props claim nothing: the server never reads them for
trust (gate.py, mandate.py, owners.py go by the author's id).

People over NATS (RT-6.42). The central bridge carries people's posts and reactions in a
bound channel on `<bus>.hu.<channel>.<anchor>` (natsbus.PersonEvent). Per channel the home,
each guest and the lobby, while a fresh signal of the bridge names it (`people_switched`),
this session takes that channel's people from NATS only and drops Mattermost's events of
people there (`skip`: a post or reaction by an account that is no bot, not a webhook or
system post); without one it reads them on Mattermost as before, and NATS's are dropped.
When the signal runs out, Mattermost is read from the channel's last people event on
(`check_people`). Both sides may deliver one event at a switch: a key held for an hour
drops the second (a post's Mattermost id; a reaction's id, emoji, sender and kind, cleared
by the opposite kind; on Mattermost's side the sender is the account's anchor by the login
service's record). A person's post keeps its Mattermost id (`mm`), so replies and reactions
go to Mattermost as before; its root is the NATS post its `root` names when that is in the
memory, else `mm_root`. Its author is `hu:<anchor>` (person_id; `hu:-` for every account
bound to nobody), never a Mattermost account or an agent: trusted when the anchor is one of
the session's own attested trusted anchors (trusted_humans adds them), and an approval only
with a fresh approver record besides (may_approve). Direct messages and other bots stay on
Mattermost.

A NATS home that cannot start (no login, a NameMismatch) is reported in the log and
every send raises: it never falls back to Mattermost. A start that fails for a
reason that may pass (the server not reachable, the login or Mattermost not ready) is
tried again with backoff. Whatever NATS does, Mattermost's events keep flowing
(MergedEvents): a NATS error is logged and its side opens again when the bus is back.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
from pathlib import Path
import secrets
import time
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from . import a2a, config, lease, natsbus, owners, planb
from .events import ChatEvent, EventSource, EventStream, PostEvent, ReactionEvent
from .ids import _ID, NATS_POST, dm_seq
from .model import NotFound, Post, Reaction, Thread, UnreadChannel
from .natsbus import BOT_RE, DM_SINCE, Attestation, Event, NameMismatch, NatsBus, NatsBusError, NatsSettings
from .port import AttestedCircle

if TYPE_CHECKING:
    from .compose import BusClient

log = logging.getLogger(__name__)

DEFAULT_MEMORY = 2000  # posts kept per channel
MAX_REACTIONS = 64  # distinct (emoji, sender) per post
MAX_QUEUE = 10_000  # events a subscriber or a merged stream holds
START_RETRY_SEC = 1.0  # first wait of a start that failed; doubles up to START_RETRY_MAX_SEC
START_RETRY_MAX_SEC = 30.0
CLAIM_ECHO_SEC = 2.0  # how long a claim waits for its own copy (RT-6.38)
ECHO_WAIT_SEC = 10.0  # how long a send waits for its own copy from the stream
_NOTE = "a2a_"  # after the props prefix: the bus's own note, flat
_NONCE = "nats_nonce"  # after the props prefix: tells a post its own copy
_ROOT = "nats_root"  # after the props prefix: the NATS post a Mattermost post answers
_NATS_ID = "nats_id"  # after the props prefix: the NATS post the bridge's copy mirrors (RT-6.36 step 4)
BRIDGE_TTL_SEC = 300.0  # how long "CB_MM_BRIDGE_BOT is no bot account" holds before it is read again
COPY_READ_CAP = 1000  # Mattermost's most posts in one read after a time: a read this long may be cut
COPY_SETTLE_SEC = 5.0  # a copy younger than this is not passed by the count (a replica may lag)
PERSON = "hu:"  # the author id of a person over NATS: PERSON + anchor (RT-6.42)
PEOPLE_KEY_SEC = 3600.0  # how long a person's event key drops its repeat (RT-6.42 step 9)
PEOPLE_KEYS_MAX = 100_000  # keys held at most; the oldest go first
PEOPLE_CHECK_SEC = 10.0  # how often the bridge's signals are looked at (RT-6.42 step 9)
UNAPPROVED_SEC = 86400.0  # how long the reads remember an event without approver: an ask's 24 h
PEOPLE_MARGIN_MS = 60_000  # the catch-up on Mattermost reads this much before the last people event


class NatsPortError(Exception):
    """A NATS operation of the port failed. The text never holds content, a token
    or a seed."""


class BusLike(Protocol):
    """What the port needs of a natsbus.NatsBus (a fake in the unit tests)."""

    @property
    def name(self) -> str: ...

    @property
    def connected(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def post(self, channel: str, text: str, root: int | None = None,
                   props: dict[str, natsbus.Prop] | None = None) -> None: ...

    async def react(self, channel: str, ref: int, emoji: str) -> None: ...

    async def unreact(self, channel: str, ref: int, emoji: str) -> None: ...

    async def dm(self, recipient: str, text: str, root: int | None = None,
                 props: dict[str, natsbus.Prop] | None = None) -> None: ...

    def dm_of(self, agent: str) -> bool: ...

    dm_source: Callable[[], bool] | None  # plan B piece 3: the home's DM switch, in the presence

    def notice_teller(self, agent: str) -> str: ...

    def owner_of(self, agent: str) -> str: ...

    def anchor_of(self, agent: str) -> str: ...

    def attestation_of(self, agent: str) -> Attestation | None: ...
    def records_on(self) -> bool: ...

    def presence_of(self, agent: str) -> str: ...

    def people_switched(self, channel: str) -> bool: ...

    def bridge_prefix(self, channel: str) -> str: ...

    def channel_ids(self) -> dict[str, str] | None: ...

    async def wait_channel_ids(self, timeout: float) -> dict[str, str] | None: ...

    def anchor_of_account(self, account: str) -> str: ...

    def approver(self, anchor: str) -> bool: ...

    def account_approver(self, account: str) -> bool: ...

    def bound_accounts(self) -> dict[str, str]: ...

    def link_bot(self, bot_id: str, username: str = "") -> None: ...

    def bot_of(self, agent: str) -> str: ...

    @property
    def synced(self) -> bool: ...

    def is_member(self, channel: str, agent: str) -> bool: ...

    def presence_snapshot(self) -> dict[str, tuple[float, str]]: ...

    def asks_changed(self) -> None: ...

    async def publish_stats(self, body: bytes) -> None: ...

    async def member(self, channel: str) -> None: ...

    async def claim(self, channel: str) -> None: ...

    def claim_info(self, channel: str) -> dict[str, tuple[int, float]]: ...

    def own_echo_age(self) -> float: ...

    @property
    def server_now(self) -> float: ...


def post_id(channel: str, seq: int) -> str:
    return f"n1:{channel}:{seq}"


def parse_post_id(value: str) -> tuple[str, int] | None:
    """(channel name, stream sequence) of a NATS post id, None for any other."""
    m = NATS_POST.fullmatch(value or "")
    return (m["channel"], int(m["seq"])) if m else None


def dm_post_id(seq: int) -> str:
    """A direct message's id (plan B piece 3): a DM has no channel, so its id says so
    (`n1:@dm:<stream sequence>`) and no channel's post can carry it."""
    return post_id(DM_SINCE, seq)


def parse_dm_id(value: str) -> int | None:
    """The stream sequence of a direct message's id (`n1:@dm:<seq>`), None for any other."""
    return dm_seq(value)


def is_nats_user(user_id: str) -> bool:
    """The author id of a post that came over NATS: an agent name, which has a "-".
    No Mattermost user id (26 characters of [a-z0-9]) is one."""
    return "-" in user_id and natsbus.AGENT_RE.fullmatch(user_id) is not None and not _ID.fullmatch(user_id)


def person_id(anchor: str) -> str:
    """The author id of a person over NATS (RT-6.42): never a Mattermost id or an agent name."""
    return PERSON + anchor


def is_person(user_id: str) -> bool:
    return user_id.startswith(PERSON)


def _remember(keys: OrderedDict[tuple[str, ...], float], key: tuple[str, ...]) -> None:
    """Hold `key` as judged now: at most PEOPLE_KEYS_MAX keys, none older than UNAPPROVED_SEC."""
    now = time.monotonic()
    while keys and (len(keys) >= PEOPLE_KEYS_MAX or next(iter(keys.values())) < now - UNAPPROVED_SEC):
        keys.popitem(last=False)
    keys[key] = now
    keys.move_to_end(key)


class _Keys:
    """The keys of people's events this session delivered (RT-6.42 step 9), each for
    PEOPLE_KEY_SEC by the monotonic clock, at most PEOPLE_KEYS_MAX."""

    def __init__(self, ttl: float = PEOPLE_KEY_SEC) -> None:
        self._ttl = ttl
        self._at: OrderedDict[tuple[str, ...], float] = OrderedDict()

    def take(self, key: tuple[str, ...]) -> bool:
        """True for a key not held (now held), False for one held: a repeat."""
        now = time.monotonic()
        while self._at and (len(self._at) >= PEOPLE_KEYS_MAX or next(iter(self._at.values())) < now - self._ttl):
            self._at.popitem(last=False)
        if key in self._at:
            return False
        self._at[key] = now
        return True

    def take_reaction(self, kind: str, mm: str, emoji: str, sender: str) -> bool:
        """A reaction's or unreaction's key; the opposite kind's is cleared, so a reaction
        taken back and given again counts again."""
        other = "unreaction" if kind == "reaction" else "reaction"
        self._at.pop((other, mm, emoji, sender), None)
        return self.take((kind, mm, emoji, sender))


def _scalar(v: object) -> bool:
    return isinstance(v, str | int | float | bool)


def wire_props(props: Mapping[str, Any] | None, prefix: str) -> dict[str, natsbus.Prop]:
    """The props a post sends over NATS: the bus's note (`a2a`) as flat keys
    `<prefix>a2a_<key>`, the agent's own `<prefix>…` keys as they are, nothing else."""
    out: dict[str, natsbus.Prop] = {}
    for k, v in (props or {}).items():
        if k == "a2a" and isinstance(v, Mapping):
            out.update({f"{prefix}{_NOTE}{nk}": nv for nk, nv in v.items() if _scalar(nv)})
        elif isinstance(k, str) and k.startswith(prefix) and _scalar(v):
            out[k] = v
    return out


def read_props(wire: Mapping[str, natsbus.Prop], prefix: str) -> dict[str, Any]:
    """The inverse: props as a Mattermost post carries them, the note under `a2a`."""
    note_prefix = f"{prefix}{_NOTE}"
    note = {k[len(note_prefix):]: v for k, v in wire.items() if k.startswith(note_prefix)}
    out: dict[str, Any] = {k: v for k, v in wire.items() if not k.startswith(note_prefix)}
    if note:
        out["a2a"] = note
    return out


def _notice_file(uid: str) -> Path:
    from .session_file import state_dir

    return state_dir() / f"notice-{uid}.env"


def _since_from_read_mark(uid: str, channels: set[str]) -> dict[str, tuple[int, int]]:
    """The hand-over cursor a killed session left (#213): the read mark's NATS lane
    (`read-<uid>.env`, `<channel id>:nats`) holds the last post HANDED to the agent,
    `n1:<channel name>:<seq>`. That channel's sequence and the post's server time (ms, the
    mark's `at`) go into the first CONNECT; a channel the session no longer reads is
    ignored, and so is a sequence past 2^63-1 (the hello's `since` is a map of uint64).
    Direct messages have no lane here: a DM post has no channel id, so the read mark keeps
    nothing for them and a restarted server has no `@dm` cursor (the login service starts that
    consumer at `new`: a DM that came while the session was away stays unread)."""
    from . import watermark

    out: dict[str, tuple[int, int]] = {}
    if not uid:
        return out
    for key, mark in watermark.read(uid).items():
        if not key.endswith(watermark.NATS_LANE):
            continue
        parsed = NATS_POST.fullmatch(mark.post_id or "")
        if parsed is None or parsed.group("channel") not in channels:
            continue
        seq = int(parsed.group("seq"))
        if 0 < seq < 1 << 63 and mark.at > 0:
            out[parsed.group("channel")] = (seq, mark.at)
    return out


CHANNEL_RECORD_WAIT_SEC = 20.0  # how long resolve waits for the credential's channel record (piece 1)
SWITCH_WAIT_SEC = 20.0  # how long the start waits for the bus to have read plan B's switches
NOTICE_KEY_MAX = 64  # notices remembered as told, per server
NOTICE_SAME_SEC = 30 * 60.0  # the block: the same kind, id and verdict is one DM


def _notice_repeat(seen: dict[tuple[str, str, str], float], key: tuple[str, str, str], now: float,
                   window: float = NOTICE_SAME_SEC) -> bool:
    """Whether this kind, id and verdict was told within the window (then no DM)."""
    at = seen.get(key)
    return at is not None and now - at < window


def _notice_read(uid: str) -> dict[str, float]:
    """The notices this server told before a restart: "<kind>:<id>" -> iat."""
    try:
        text = _notice_file(uid).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        k, _, v = line.rpartition("=")
        try:
            out[k] = float(v)
        except ValueError:
            continue
    return out


def _notice_told(uid: str, key: str) -> float:
    """The iat this server told for that kind and id (0 for none), never in the
    future: a skewed value must not silence every later notice."""
    return min(_notice_read(uid).get(key, 0.0), time.time())


def _notice_mark(uid: str, key: str, iat: float) -> None:
    """Remember that this kind and id was told, so a restarted server does not tell
    it again. Per kind and id: one copy must not hide another."""
    from .session_file import write_private

    marks = _notice_read(uid)
    marks[key] = min(iat, time.time())
    if len(marks) > NOTICE_KEY_MAX:
        for k in sorted(marks, key=marks.__getitem__)[: len(marks) - NOTICE_KEY_MAX]:
            del marks[k]
    try:
        write_private(_notice_file(uid), "".join(f"{k}={int(v)}\n" for k, v in sorted(marks.items())))
    except OSError as exc:
        # The DM is out; a restart may repeat it, which the log says.
        log.warning("login notice: the told mark was not written: %s", type(exc).__name__)




class NatsPort:
    """The chat port of a session with a home over NATS (module docstring). Members
    not defined here are the Mattermost adapter's (`__getattr__`)."""

    def __init__(self, inner: BusClient, settings: NatsSettings, homes: Iterable[str], *,
                 memory: int = DEFAULT_MEMORY, echo_wait: float = ECHO_WAIT_SEC,
                 bus_options: Mapping[str, Any] | None = None, bridge_bot: str = "",
                 session: str = "") -> None:
        self.inner = inner
        # CB_MM_BRIDGE_BOT: the bot the bridge mirrors NATS with (RT-6.36 step 4); "" drops only the
        # copies under the agents' own bots (the bot prefix of the bridge's signal, _bot_prefix).
        self._bridge_bot = bridge_bot.strip().lstrip("@").lower()
        # The session's own part of the name (#163): set only when the host key is
        # shared, so two sessions of one owner are two agents.
        self._session = session
        self._bridge_id: str | None = None  # its Mattermost id, read once (_bridge)
        self._bridge_none_at = 0.0  # monotonic time it was found to be no bot account
        self._highest: dict[str, int] = {}  # channel -> the highest stream sequence received
        self._s = settings
        self._homes = tuple(sorted({h.strip().lower() for h in homes if h.strip()}))
        if not self._homes:
            raise ValueError("a NatsPort needs a home")
        self._memory = max(1, memory)
        self._echo_wait = echo_wait
        self._bus_options = dict(bus_options or {})
        self._bus: BusLike | None = None
        self._fatal: NatsPortError | None = None
        self._up = False  # the bus is started: once it is synced the switch can be read
        self._ids: dict[str, str] | None = None  # channel name -> Mattermost id
        self._resolving = asyncio.Lock()
        self._posts: dict[str, OrderedDict[int, Post]] = {}  # channel -> seq -> post
        self._where: dict[int, str] = {}  # seq -> channel (stream sequences are global)
        self._reacts: dict[int, dict[tuple[str, str], int]] = {}  # seq -> (emoji, sender) -> ms
        self._children: dict[str, set[int]] = {}  # root post id -> seqs of its replies (the count of _shown)
        self._echoes: dict[str, tuple[str, asyncio.Future[Post]]] = {}  # nonce -> (channel, future)
        self._subs: list[asyncio.Queue[ChatEvent | NatsPortError | None]] = []
        self._dropped = 0  # events a full subscriber's queue lost
        self._capped = 0  # reactions dropped at MAX_REACTIONS
        self._names: AbstractSet[str] | None = None  # this session's trusted humans, read by prime (RT-6.38)
        self._notice_recent: dict[tuple[str, str, str], float] = {}  # (kind, id, verdict) -> monotonic
        self._ambiguous: set[str] = set()  # link tags that fit more than one fresh name (RT-6.37)
        self._own_missing = False  # plan B piece 4: the missing own attestation was logged
        # People over NATS (RT-6.42)
        self._keys = _Keys()
        self._person_names: dict[str, str] = {}  # person id -> the name the bridge last showed
        self._people_ids: dict[str, str] = {}  # channel name -> Mattermost id, for the bus's channels
        self._people_miss: dict[str, float] = {}  # channel name -> monotonic time its id was not readable
        self._switched: set[str] = set()  # channels whose people came over NATS at the last look
        self._people_at: dict[str, int] = {}  # channel -> ms of its last people event over NATS
        self._catching_up: set[asyncio.Task[None]] = set()
        self._owed: dict[str, int] = {}  # channel -> start of a catch-up that failed: tried again
        # Events that came over NATS from a person without a fresh approver record, judged as
        # they came, for the reads (`unapproved`): key -> monotonic time, kept as long as an ask
        self._unapproved: OrderedDict[tuple[str, ...], float] = OrderedDict()
        # ... and those judged with one: plan B piece 4 judges every other event of a bound
        # Mattermost account at the read, by that account's record then (`unapproved`)
        self._approved: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self._watch: asyncio.Task[None] | None = None
        # The HELLO card's data the presence carries (RT-6.37, ADR-015); the MCP server sets
        # it (a hosted one maybe after start), read at every presence the bus sends. None:
        # no card's data. Called with the home's channel id (home_id), so that the last post
        # it states is one in the home (RT-6.37 step 6).
        self.card_source: Callable[[str], natsbus.PresenceCard | None] | None = None
        # The work channel's id, read at start; '' until then or when it could not be read.
        self.home_id = ""
        # The open asks for the ask record on `<presence>.ask.<home>.<name>` (RT-6.43 step 5);
        # set like card_source, read at every check of the bus. None: no record.
        self.ask_source: Callable[[], list[natsbus.OpenAsk]] | None = None

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "__setstate__"):
            raise AttributeError(name)
        return getattr(self.inner, name)

# --- lifecycle ---------------------------------------------------------------
    @property
    def homes(self) -> tuple[str, ...]:
        return self._homes

    @property
    def bus_attached(self) -> bool:
        return self._bus is not None and self._fatal is None

    @property
    def bus_connected(self) -> bool:
        return self._bus is not None and self._fatal is None and self._bus.connected

    @property
    def failure(self) -> NatsPortError | None:
        return self._fatal

    def use_bus(self, bus: BusLike) -> None:
        """The bus this port sends and reads by (start makes one)."""
        self._bus = bus
        bus.dm_source = self._dm_on  # plan B piece 3: the home's DM switch, stated in its presence
        self._up = True  # the caller brings it up

    def _dm_on(self) -> bool:
        """Whether this home runs plan B's agent DMs over NATS (piece 3): `planb.on(3, home)`,
        off while the switch (piece 1) is not there. Stated in every presence this agent
        sends, and read on every send of a DM and every DM event; a switch that cannot be read
        counts as off, so nothing leaves Mattermost by doubt."""
        try:
            return bool(self._s.home) and bool(planb.on(3, self._s.home))
        except Exception as exc:  # noqa: BLE001 — in doubt off: agent DMs stay on Mattermost
            log.info("nats: the DM switch was not read (%s): agent DMs stay on Mattermost",
                     type(exc).__name__)
            return False

    async def start(self, login: natsbus.LoginLike, owner: str | None = None) -> None:
        """Resolve the homes' channels, then start the bus as the login's owner
        (the access token's preferred_username unless `owner` is given). Waits for
        the login's approval. A failure that waiting does not cure (_final: a
        NameMismatch, wrong settings, a ConfigError of the login, a login the human
        declined or the wrong human approved) is logged, kept (every send then
        raises) and raised; any other (Mattermost, the login or the NATS server not
        ready, an answer that does not decode) is logged and tried again with backoff."""
        if self._bus is not None:
            return
        delay = START_RETRY_SEC
        while True:
            try:
                # The channel ids are read after the bus is up (below): plan B's switch for
                # them (piece 1) is read from the bus's records (planb.known).
                if owner is None:
                    owner = await _owner_of(login)
                # #213: the cursor a killed session left is the read mark's NATS lane — the
                # last post it HANDED to the agent, never one merely queued. The login service
                # restarts each consumer after it, so a post still in the queue when the process
                # died is delivered again (at least once for that one, read once by the mark).
                # Nothing is written or deleted here: the mark outlives a bye (the next server
                # of a kept bot sends it, and the consumer it recreates replays the absence).
                bot = await self._bot_id()
                since = (_since_from_read_mark(bot, set(self._people_channels()))
                         if BOT_RE.fullmatch(bot) else {})
                bus = NatsBus(self._s, login, owner, self._on_event, on_fatal=self._on_fatal,
                              on_person=self._on_person, on_notice=self._on_notice,
                              session=self._session, since=since, **self._bus_options)
                self._bus = bus
                bus.on_person_gap = self._person_gap
                bus.card_source = self._card
                bus.dm_source = self._dm_on  # plan B piece 3: stated in every presence
                bus.ask_source = self._asks
                try:
                    bus.link_bot(bot, await self._bot_name())
                except ValueError:
                    # A bot id that is no Mattermost user id (a bad CB_MM_BOT_USER_ID): the NATS
                    # home still starts, only without a link between bot and NATS name.
                    log.warning("nats: the bot id is not a Mattermost user id: starting without a link")
                await bus.start()
                self._up = True
                await self._wait_switch()
                if self._piece1():
                    try:
                        await self.resolve()  # the channel record of the credential (RT-6.39 step 6)
                        work = self.inner.work_channel_name.lower()
                        if not self.home_id and work in self._homes:
                            self.home_id = (await self.resolve()).get(work, "")
                    except BaseException:
                        await bus.stop()
                        raise
                else:  # the switch is read now (or counts as off): Mattermost's ids as before
                    await self.resolve()
                    if not self.home_id:
                        await self._read_home_id()
                lease.use_nats_link(partial(_tag_of, bus))
                await self._announce_membership(bus)
                self._watch = asyncio.create_task(self._watch_people())
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — final, or it may pass: again
                if _final(exc):
                    self.fail(exc)
                    raise
                self._bus = None
                self._up = False
                log.warning("nats: the home %s did not start (%s), trying again in %.0f s",
                            ", ".join(self._homes), _describe(exc), delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, START_RETRY_MAX_SEC)

    def _asks(self) -> list[natsbus.OpenAsk]:
        """The open asks for the bus's next ask record: ask_source's, read now."""
        return self.ask_source() if self.ask_source is not None else []

    def asks_changed(self) -> None:
        """An ask was posted, answered or ended: the bus publishes its record now (RT-6.43)."""
        changed = getattr(self._bus, "asks_changed", None)  # a test's bus may have none
        if changed is not None:
            changed()

    async def publish_stats(self, body: bytes) -> None:
        """One STATS part on `<stats>.<name>` (RT-6.43 step 7); raises when it was not sent
        (no bus yet, or the server refused it), so stats keeps it for the next part."""
        await self._require().publish_stats(body)

    def _card(self) -> natsbus.PresenceCard | None:
        """The card's data for the bus's next presence: card_source's, read now."""
        return self.card_source(self.home_id) if self.card_source is not None else None

    async def _read_home_id(self) -> None:
        """home_id: the work channel's id, for the last post the presence states (RT-6.37
        step 6). Never raises: unread, the presence states no last post."""
        try:
            self.home_id = await self.inner.work_channel_id()
        except Exception as exc:  # noqa: BLE001 — a card's field, never a reason not to start
            log.info("nats: the home's id not read (%s): no last post in the presence",
                     type(exc).__name__)

    async def _bot_id(self) -> str:
        """The Mattermost user id of the bot, as lease._user_id resolves it."""
        inner: Any = self.inner
        if inner.session:
            return str(inner.session.user_id)
        return config.env("CB_MM_BOT_USER_ID") or (await inner.get_me()).id

    async def _bot_name(self) -> str:
        """The bot's username, for the presence (the name the operator console shows,
        ADR-015); CB_MM_BOT_USERNAME, or '' when it cannot be read: the home starts all the same."""
        inner: Any = self.inner
        try:
            return str((await inner.get_me()).username)
        except Exception as exc:  # noqa: BLE001 — a label, never a reason to stop
            log.info("nats: the bot's username was not read (%s)", type(exc).__name__)
            return config.env("CB_MM_BOT_USERNAME")

    async def _on_notice(self, notice: natsbus.Notice) -> None:
        """A theft notice from the login service (RT-6.34): DM it to the responsible human.

        Every live server of the owner reads the record, and exactly one tells - the named
        agent's if it is live, else the owner's live agent with the smallest name - so the
        owner hears it once; the DM names the agent the record names, else this bot. It is
        the server's own post, counted as overhead (RT-6.9), never into a channel."""
        from . import login_notice, stats
        from .owners import responsible_human

        if notice.verdict != "copied":
            # A move is a report (the console and the log); only a copy is a DM.
            log.info("login notice: a move on %s (%s), reported only", notice.kind, notice.network)
            return
        bus = self._bus
        if bus is None:
            return
        teller = bus.notice_teller(notice.agent)
        if teller != bus.name:
            # One DM: the named agent's server, else the smallest live name (RT-6.34).
            log.info("login notice: %s tells it", teller)
            return
        who = responsible_human()
        try:
            me = await self._bot_id()
            key = f"{notice.kind}:{notice.sid}"
            if notice.iat <= _notice_told(me, key):
                # Told before a restart: the consumer replays the last record.
                log.info("login notice: already told (iat %d)", int(notice.iat))
                return
            # A second copy of the same kind and id within the block is the same
            # theft: one DM, not one per record.
            seen = (notice.kind, notice.sid, notice.verdict)
            now = time.monotonic()
            if _notice_repeat(self._notice_recent, seen, now):
                log.info("login notice: %s %s already told within %d s", notice.kind, notice.verdict,
                         int(NOTICE_SAME_SEC))
                return
            if not who:
                log.info("login notice: no responsible human to tell")
                return
            trusted = await self.trusted_humans()
            # a Mattermost account only: a person over NATS (hu:<anchor>) has no DM
            uid = next((i for i, n in trusted.items()
                        if n and n.lower() == who.lower() and _ID.fullmatch(i)), None)
            uid = uid or await self.human_id(who)
            if not uid:
                log.info("login notice: %s is not found in the chat", who)
                return
            agent = notice.agent or await self._bot_name() or "the agent"
            text = login_notice.notice_text(agent, notice.kind)
            stats.record("post", chars=len(text), w="other")
            await self.post(text, channel_id=await self.direct_channel_with(uid))
            _notice_mark(me, key, notice.iat)
            self._notice_recent[seen] = now
            while len(self._notice_recent) > NOTICE_KEY_MAX:
                self._notice_recent.pop(next(iter(self._notice_recent)))
            log.warning("login notice: told %s by DM (%s, %s)", who, notice.kind, notice.network)
        except Exception as exc:  # noqa: BLE001 — the next notice tries again
            log.warning("login notice: the DM to %s failed: %s", who, exc)


    async def _announce_membership(self, bus: BusLike) -> None:
        """Tell the bus to announce this session's membership of every channel it asked
        for (the NATS homes, the guests, the lobby): the signal of whereabouts (RT-6.37).
        The bus does it once it is synced, every 6 h and after each reconnect. The crown
        claim is another subject and is not made here."""
        channels = [*self._homes, *self._s.guests]
        if self._s.lobby:
            channels.append(self._s.lobby_channel)
        try:
            for ch in dict.fromkeys(channels):
                await bus.member(ch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("nats: announcing the membership failed: %s", type(exc).__name__)

    def fail(self, why: BaseException | str) -> None:
        """The homes cannot run over NATS: logged once, sends raise from now on."""
        text = why if isinstance(why, str) else _describe(why)
        self._fatal = NatsPortError(f"the NATS home {', '.join(self._homes)} is down: {text}; "
                                    "nothing is sent over Mattermost instead")
        log.error("%s", self._fatal)
        self._tell(self._fatal)

    async def _on_fatal(self, exc: NatsBusError) -> None:
        self.fail(exc)

    async def stop(self) -> None:
        if self._bus is not None:
            lease.use_nats_link(None)  # a stopped bus links nothing any more
        bus, self._bus = self._bus, None
        for t in [*self._catching_up, *([self._watch] if self._watch is not None else [])]:
            t.cancel()
        self._watch = None
        self._tell(None)
        if bus is not None:
            try:
                await bus.stop()
            except Exception as exc:  # noqa: BLE001 — going away anyway, but not silently
                log.warning("nats: stopping the bus failed: %s", type(exc).__name__)

    def _require(self) -> BusLike:
        if self._fatal is not None:
            raise self._fatal
        if self._bus is None:
            raise NatsPortError(f"the NATS home {', '.join(self._homes)} is not up yet "
                                "(waiting for the login); nothing is sent over Mattermost instead")
        return self._bus

    def subscribe(self) -> asyncio.Queue[ChatEvent | NatsPortError | None]:
        q: asyncio.Queue[ChatEvent | NatsPortError | None] = asyncio.Queue(MAX_QUEUE)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ChatEvent | NatsPortError | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(q)
        self._lost(_put_last(q, None))

    def _tell(self, item: ChatEvent | NatsPortError | None) -> None:
        for q in list(self._subs):
            if item is None or isinstance(item, NatsPortError):
                self._lost(_put_last(q, item))  # the end and the failure always get through
                continue
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                self._lost(1)

    def _lost(self, n: int) -> None:
        """Count events a full subscriber's queue lost; the log says so once per 1000."""
        for _ in range(n):
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.warning("nats: a subscriber's queue is full (%d): events dropped (%d so far)",
                            MAX_QUEUE, self._dropped)

    # --- the channels ------------------------------------------------------------
    def _piece1(self) -> bool:
        """Whether any home takes its channel ids from the channel directory (RT-6.39 step 6)."""
        return any(planb.on(1, name) for name in self._homes)

    def _switch_known(self) -> bool:
        """Whether plan B's switches are read (planb.known), or the bus this port uses has
        read its presence records (a bus that attaches no source counts once it is up)."""
        if planb.known():
            return True
        bus = self._bus
        return self._up and bus is not None and bool(getattr(bus, "synced", True))

    async def _wait_switch(self) -> None:
        """After the bus is up: wait up to SWITCH_WAIT_SEC for the switches to be read. One
        not read by then counts as off (the old path), and resolve keeps no id until it is."""
        loop = asyncio.get_running_loop()
        end = loop.time() + SWITCH_WAIT_SEC
        while not self._switch_known():
            if loop.time() >= end:
                log.warning("nats: plan B's switches were not read within %.0f s: off until they are",
                            SWITCH_WAIT_SEC)
                return
            await asyncio.sleep(0.05)

    async def _directory_id(self, name: str) -> str:
        """The Mattermost id of channel `name` from the channel record of this credential
        (RT-6.39 step 6), waiting for the first record; no Mattermost call. Raises
        NatsPortError while no record came or the directory holds no id for the channel."""
        bus = self._live()
        got = await bus.wait_channel_ids(CHANNEL_RECORD_WAIT_SEC) if bus is not None else None
        if got is None:
            raise NatsPortError(f"the channel record of the login service for ~{name} has not come")
        if not got.get(name):
            raise NatsPortError(f"the channel directory holds no Mattermost id for ~{name}")
        return got[name]

    async def resolve(self) -> dict[str, str]:
        """Channel name -> Mattermost id of the NATS homes, read once. With plan B piece 1 on
        for a home (planb.on(1, home), RT-6.39 step 6) its id is the directory's, from the
        credential's channel record, and no Mattermost call is made for it."""
        if self._ids is not None:
            return self._ids
        if not self._switch_known():
            # The switch is not read yet (the bus is not up, or has not read its records):
            # Mattermost's ids are not kept, since the directory's may be the ones to use.
            return await self._read_ids()
        async with self._resolving:
            if self._ids is None:
                self._ids = await self._read_ids()
        return self._ids

    async def _read_ids(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        for name in self._homes:
            if planb.on(1, name):
                ids[name] = await self._directory_id(name)
            elif name == self.inner.work_channel_name.lower():
                ids[name] = await self.inner.work_channel_id()
            else:
                ids[name] = await self.inner.get_channel_id(name)
        return ids

    async def nats_name(self, channel_id: str) -> str | None:
        """The name of the NATS home this channel id is, None for any other channel."""
        for name, cid in (await self.resolve()).items():
            if cid == channel_id:
                return name
        return None

    async def is_nats_channel(self, channel_id: str) -> bool:
        return bool(channel_id) and await self.nats_name(channel_id) is not None

    # --- the bridge's copies (RT-6.36 step 4) -----------------------------------------------------
    @property
    def bridge_bot(self) -> str:
        """CB_MM_BRIDGE_BOT, the bridge bot's username; "" when none is set."""
        return self._bridge_bot

    async def _bridge(self) -> str:
        """The bridge bot's Mattermost id, read once; "" when none is set or the name is
        no bot account (logged, read again after BRIDGE_TTL_SEC). A read that fails is tried
        again next time."""
        if self._bridge_id is not None and (
                self._bridge_id or not self._bridge_bot
                or time.monotonic() - self._bridge_none_at < BRIDGE_TTL_SEC):
            return self._bridge_id
        if not self._bridge_bot:
            self._bridge_id = ""
            return ""
        try:
            found = await self.inner.bots_named({self._bridge_bot})
        except Exception as exc:  # noqa: BLE001 — nothing dropped meanwhile
            log.warning("CB_MM_BRIDGE_BOT %s could not be read (%s): its copies are kept for now",
                        self._bridge_bot, type(exc).__name__)
            return ""
        user = found.get(self._bridge_bot)
        if user is None or not user.id:
            if self._bridge_id is None:  # once, not at every read again
                log.warning("CB_MM_BRIDGE_BOT %s is no bot account: nothing is dropped as the bridge's copy "
                            "(read again every %d s)", self._bridge_bot, BRIDGE_TTL_SEC)
            self._bridge_none_at = time.monotonic()
        self._bridge_id = user.id if user is not None else ""
        return self._bridge_id

    async def _in_home(self, channel_id: str) -> bool:
        try:
            return await self.is_nats_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 — not known: kept
            log.debug("nats: the homes are not known (%s): a copy is kept", type(exc).__name__)
            return False

    def _bot_prefix(self, home: str) -> str:
        """The bridge's bot prefix for the home, from its signal (natsbus.bridge_prefix); ""
        while none is known: then only the one bridge bot's copies are dropped."""
        bus = self._live()
        return bus.bridge_prefix(home) if bus is not None else ""

    def _sender_of(self, home: str, seq: int) -> str | None:
        """The NATS sender of a post of the home the session has in its memory; None for one
        it does not have, or whose author is no agent name (a person's over NATS)."""
        mem = self._posts.get(home)
        post = mem.get(seq) if mem is not None else None
        if post is None:
            return None
        if post.user_id and post.user_id == self.inner.bot_id:  # its own (_uid)
            return self._bus.name if self._bus is not None else None
        return post.user_id if is_nats_user(post.user_id) else None

    async def _agent_bot(self, user_id: str, home: str, senders: AbstractSet[str]) -> bool:
        """Whether `user_id` is the bot account `<bot prefix><sender>` of one of these NATS
        senders: only bot accounts count (bots_named leaves people out), so a person who took
        such a name is never one. A failed read counts as none (kept)."""
        prefix = self._bot_prefix(home)
        if not prefix or not user_id or not senders:
            return False
        names = {f"{prefix}{s}".lower() for s in senders}
        try:
            found = await self.inner.bots_named(names)
        except Exception as exc:  # noqa: BLE001 — nothing dropped meanwhile
            log.debug("nats: the agents' bots could not be read (%s): their copies are kept", type(exc).__name__)
            return False
        return any(u.id and u.id == user_id for n, u in found.items() if n in names)

    async def _copy_of(self, post: Post) -> int | None:
        """The stream sequence a bridge's copy in this session's NATS home mirrors: a post with
        `<prefix>nats_id` naming a post of this home (`n1:<home>:<seq>`) AND by the bridge bot,
        or by the bot `<bot prefix><sender>` where sender is that post's own NATS sender (one the
        session has); None for any other post. Anyone else's post with that prop is no copy."""
        got = parse_post_id(str(post.props.get(f"{self._s.props_prefix}{_NATS_ID}", "")))
        if got is None:
            return None
        try:
            home = await self.nats_name(post.channel_id)
        except Exception as exc:  # noqa: BLE001 — not known: kept
            log.debug("nats: the homes are not known (%s): a copy is kept", type(exc).__name__)
            return None
        if home is None or got[0] != home:
            return None
        bridge = await self._bridge()
        if bridge and post.user_id == bridge:
            return got[1]
        sender = self._sender_of(home, got[1]) if self._bot_prefix(home) else None
        if sender is not None and await self._agent_bot(post.user_id, home, {sender}):
            return got[1]
        return None

    async def mirrored(self, post: Post) -> bool:
        """The bridge's copy of a NATS post of this session's NATS home (_copy_of): the
        events drop it, whether or not its NATS post came yet."""
        return await self._copy_of(post) is not None

    async def _had(self, post: Post) -> bool:
        """A copy the reads drop: of a NATS post this session has (in its memory, or at or
        below the highest sequence it received). A copy of one it never had stays visible."""
        seq = await self._copy_of(post)
        if seq is None:
            return False
        name = await self.nats_name(post.channel_id)
        return name is not None and (seq in self._posts.get(name, {}) or seq <= self._highest.get(name, -1))

    async def mirrored_reaction(self, reaction: Reaction, channel_id: str) -> bool:
        """A reaction in this session's NATS home of the bridge bot, or of the bot
        `<bot prefix><sender>` of a NATS sender of the home the session has a post or reaction
        of: the copy of a NATS reaction (the bridge reacts on its own copies only, and a
        reaction has no props)."""
        bridge = await self._bridge()
        mine = bool(bridge) and reaction.user_id == bridge
        if not mine and not any(self._bot_prefix(h) for h in self._homes):
            return False
        if not channel_id:
            try:
                channel_id = await self.inner.post_channel_id(reaction.post_id)
            except Exception:  # noqa: BLE001 — not known: kept
                return False
        if mine:
            return await self._in_home(channel_id)
        try:
            home = await self.nats_name(channel_id)
        except Exception:  # noqa: BLE001 — not known: kept
            return False
        return home is not None and await self._agent_bot(reaction.user_id, home, self._senders(home))

    def _senders(self, home: str) -> set[str]:
        """The NATS senders of the home's posts and reactions in the session's memory."""
        mem: Mapping[int, Post] = self._posts.get(home, {})
        got = {s for seq in mem if (s := self._sender_of(home, seq)) is not None}
        for seq in mem:  # a reaction is kept by its sender's NATS name
            got.update(sender for _emoji, sender in self._reacts.get(seq, {}))
        return {s for s in got if is_nats_user(s)}

    async def _copiers(self, home: str) -> bool:
        """Whether the home can have the bridge's copies: the bridge bot is a bot account, or
        the bridge's signal named a bot prefix (one read for a batch)."""
        return bool(self._bot_prefix(home)) or (bool(self._bridge_bot) and bool(await self._bridge()))

    async def _unmirrored(self, home: str, posts: list[Post]) -> list[Post]:
        """Mattermost's posts of the NATS home without the bridge's copies."""
        if not await self._copiers(home):
            return posts
        return [p for p in posts if not await self._had(p)]

    async def copies_through(self, channel_id: str, since_ms: int) -> int:
        """How far the read watermark of this NATS home's Mattermost lane may pass the
        bridge's copies (unread.after): the time of the last copy the reads drop (_had) after
        `since_ms` that comes before every other post there, 0 when the first post after it
        is none (a person's, a gated one, anything). Mattermost's own read, never the merged
        one: a post the merged read does not show still stops it. A read of COPY_READ_CAP
        posts or more may be cut and passes nothing; a copy younger than COPY_SETTLE_SEC by
        this host's clock is not passed (a replica may still lack a post before it). A post
        in neither read can still be passed."""
        home = await self.nats_name(channel_id)
        if home is None or not await self._copiers(home):
            return 0
        raw = await self.inner.channel_posts(channel_id, since_ms=since_ms)
        if len(raw) >= COPY_READ_CAP:
            return 0
        after = [p for p in raw if p.create_at > since_ms]
        marked = [(p.create_at, await self._had(p)) for p in after]
        first_other = min((t for t, copy in marked if not copy), default=None)
        settled = int((time.time() - COPY_SETTLE_SEC) * 1000)
        return max((t for t, copy in marked
                    if copy and t <= settled and (first_other is None or t < first_other)), default=0)

    async def skip(self, event: ChatEvent) -> bool:
        """A Mattermost event that is the bridge's copy, or a person's that comes over NATS
        or came already (RT-6.42): MergedEvents drops it."""
        if await self._copy_event(event):
            return True
        if await self._person_on_mattermost(event):
            return True
        self._judge_on_mattermost(event)  # plan B piece 4
        return False

    async def _copy_event(self, event: ChatEvent) -> bool:
        if not self._bridge_bot and not any(self._bot_prefix(h) for h in self._homes):
            return False
        if isinstance(event, PostEvent):
            return await self.mirrored(event.post)
        if isinstance(event, ReactionEvent):
            return await self.mirrored_reaction(event.reaction, event.channel_id)
        return False

    def _name_now(self, channel_id: str) -> str | None:
        """As nats_name without a request, for the synchronous members; raises
        while the homes are not resolved (nothing can tell then)."""
        if self._ids is None:
            raise NatsPortError("the NATS homes are not resolved yet: nothing is sent")
        return next((n for n, cid in self._ids.items() if cid == channel_id), None)

    async def _home_or(self, channel_id: str | None) -> str:
        return channel_id or await self.inner.work_channel_id()

    # --- ids -----------------------------------------------------------------------
    def _id_in(self, value: str, channel: str, what: str) -> int:
        """The sequence of NATS post `value`, which must be in `channel`; else a
        clear NatsPortError (a reply or reaction never crosses to another chat or channel)."""
        got = parse_post_id(value)
        if got is None:
            raise NatsPortError(f"{what} must be a NATS post id (n1:{channel}:<n>) in the NATS home "
                                f"~{channel}, not a Mattermost post: nothing sent")
        if got[0] != channel:
            raise NatsPortError(f"{what} is a post of ~{got[0]}, not of ~{channel}: nothing sent")
        return got[1]

    def _uid(self, sender: str) -> str:
        """The author id of a sender: the bot's own Mattermost id for itself."""
        me = self._bus.name if self._bus is not None else ""
        bot = self.inner.bot_id
        return bot if bot and sender == me else sender

    # --- sending ---------------------------------------------------------------------
    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None,
                   file_ids: Sequence[str] = ()) -> Post:
        cid = await self._home_or(channel_id)
        name = await self.nats_name(cid)
        if name is None:
            if root_id and parse_post_id(root_id):
                raise NatsPortError("a reply to a NATS post goes into its own NATS home, "
                                    "not into another channel: nothing sent")
            return await self.inner.post(message, channel_id=channel_id, root_id=root_id,
                                         props=props, file_ids=file_ids)
        if root_id and parse_post_id(root_id) is None:
            # A reply to a person's post: people stay on Mattermost, so the reply goes
            # where their thread is (RT-6.36).
            return await self.inner.post(message, channel_id=channel_id, root_id=root_id,
                                         props=props, file_ids=file_ids)
        if file_ids or a2a.kind_of({"props": props or {}, "message": message}) == a2a.ASK:
            # For people (an ask for a human, files): Mattermost, as before.
            if root_id and parse_post_id(root_id):
                raise NatsPortError("an ask or a post with files is for people and goes to Mattermost, "
                                    "where it cannot be a reply to a NATS post: post it top-level "
                                    "(no root_id) in the home; nothing sent")
            return await self.inner.post(message, channel_id=channel_id, root_id=root_id,
                                         props=props, file_ids=file_ids)
        root = self._id_in(root_id, name, "a reply's root") if root_id else None
        bus = self._require()
        wire = wire_props(props, self._s.props_prefix)
        nonce = secrets.token_hex(8)  # only this post's own copy carries it
        wire[f"{self._s.props_prefix}{_NONCE}"] = nonce
        echo: asyncio.Future[Post] = asyncio.get_running_loop().create_future()
        self._echoes[nonce] = (name, echo)
        try:
            await bus.post(name, message, root=root, props=wire)
            try:
                return await asyncio.wait_for(echo, self._echo_wait)
            except TimeoutError:
                raise NatsPortError("the post was sent over NATS, but its own copy did not come back: "
                                    "its id is not known") from None
        finally:
            self._echoes.pop(nonce, None)

    async def dm(self, to: str, message: str, *, root_id: str | None = None,
                 props: dict[str, Any] | None = None,
                 file_ids: Sequence[str] = ()) -> str:
        """A direct message to another agent (plan B piece 3, ADR-014 phase 5): its answer is
        the NATS name of the recipient the message went to over the bus's own subject
        `<bus>.dm.<recipient>.<sender>`, and '' when the caller keeps the Mattermost DM.

        Over NATS only when this home runs agent DMs over NATS (the switch `planb.on(3, home)`),
        the recipient names that too in its presence, and the two are linked: the username `to`
        is a Mattermost bot whose status carries the tag of a fresh NATS name whose presence
        names that very bot (RT-6.37, the one test of a link). A person, an unknown name, an
        unlinked or absent recipient, a recipient whose flag is off or not stated, files, an ask
        and a reply (a DM has no thread) all give '': the caller sends the Mattermost DM as
        before, or refuses it as before (RT-6.36 step 7). A send that was made over NATS and
        fails raises: nothing falls back to Mattermost, as for a post."""
        if not to.strip() or root_id or file_ids:
            return ""
        if not self._dm_on():
            return ""
        if a2a.kind_of({"props": props or {}, "message": message}) == a2a.ASK:
            return ""  # an ask is for people: Mattermost
        bus = self._bus if self._fatal is None else None
        if bus is None or not bus.connected:
            return ""
        recipient = await self._dm_recipient(bus, to)
        if not recipient:
            return ""
        await bus.dm(recipient, message, props=wire_props(props, self._s.props_prefix))
        return recipient

    async def dm_agent(self, to: str) -> str:
        """The NATS name linked to the bot username `to`, '' when none is (RT-6.37's one test
        of a link, read as `_dm_linked` does): the agent a DM to `to` belongs to, whether or not
        its home runs agent DMs over NATS. A Mattermost DM to it settles the open DMs that name
        left, as a DM over NATS does (RT-6.2 step 2, RT-6.36 step 7). A bot whose presence is
        not linked (a person's, another agent's bot) names nobody."""
        if not self._dm_on():
            return ""  # the piece is off: today's path, no link read at all
        bus = self._bus if self._fatal is None else None
        if bus is None or not bus.connected:
            return ""
        return await self._dm_linked(bus, to)

    async def _dm_recipient(self, bus: BusLike, to: str) -> str:
        """The NATS name a DM to the bot username `to` goes to, '' when it stays on Mattermost:
        the linked name (`dm_agent`) whose own presence says its home runs agent DMs over NATS."""
        name = await self._dm_linked(bus, to)
        if not name:
            return ""
        if not bus.dm_of(name):
            log.info("nats: %s does not state that its home runs agent DMs over NATS: the DM to "
                     "%s stays on Mattermost", name, to.strip().lstrip("@").lower())
            return ""
        return name

    async def _dm_linked(self, bus: BusLike, to: str) -> str:
        """The one fresh NATS name linked to the bot username `to` (RT-6.37: the bot's status
        names the name's tag and the name's presence names that bot), '' when there is none or a
        tag fits two names. A person, an unknown name and a failed read name nobody."""
        name = to.strip().lstrip("@").lower()
        if not name:
            return ""
        try:
            users = await self.inner.bots_named({name})
        except Exception as exc:  # noqa: BLE001 — no read: the Mattermost DM path decides
            log.info("nats: the bot for %s was not read (%s): the DM stays on Mattermost",
                     name, type(exc).__name__)
            return ""
        user = users.get(name)
        if user is None:
            return ""  # a person or an unknown name: the caller refuses it, as without NATS
        tag = lease.link_of(str((user.custom_status or {}).get("text") or ""))
        if not tag:
            return ""
        same = [n for n in bus.presence_snapshot()
                if bus.presence_of(n) != "offline" and natsbus.link_tag(n) == tag
                and bus.bot_of(n) == user.id]
        if len(same) > 1:
            log.warning("nats: the link tag %s fits %d fresh NATS names: the DM to %s stays on "
                        "Mattermost", tag, len(same), name)
            return ""
        return same[0] if same else ""

    def post_sync(self, message: str, *, channel_id: str, root_id: str = "",
                  props: dict[str, Any] | None = None, timeout: float = 5.0) -> Post:
        # The exit handler's last words (a BYE) are for the people in the home: Mattermost.
        # Its root may be a NATS post (the HELLO): Mattermost has no such post, so it goes
        # top-level in the channel, the prop naming the NATS post it answers.
        if parse_post_id(root_id):
            props = {**(props or {}), f"{self._s.props_prefix}{_ROOT}": root_id}
            root_id = ""
        return self.inner.post_sync(message, channel_id=channel_id, root_id=root_id,
                                    props=props, timeout=timeout)

    def post_by_name_sync(self, channel_name: str, message: str, *, join: str = "") -> bool:
        # As post_sync: a synchronous post goes to Mattermost, also in a NATS home.
        return self.inner.post_by_name_sync(channel_name, message, join=join)

    async def add_reaction(self, post_id: str, emoji: str) -> None:
        got = parse_post_id(post_id)
        if got is None:
            if parse_dm_id(post_id) is not None:
                raise NatsPortError("a reaction to a direct message is not carried over NATS yet: "
                                    "answer the agent with a DM (post_message to=); nothing sent")
            # A Mattermost post (a person's, also in a NATS home): the reaction goes there.
            await self.inner.add_reaction(post_id, emoji)
            return
        channel, seq = got
        self._home(channel)
        await self._require().react(channel, seq, emoji)

    async def remove_reaction(self, post_id: str, emoji: str) -> None:
        """Take the bot's reaction off a NATS post (an unreaction: only its own)."""
        got = parse_post_id(post_id)
        if got is None:
            if parse_dm_id(post_id) is not None:
                raise NatsPortError("a reaction to a direct message is not carried over NATS yet: "
                                    "there is none to take off; nothing sent")
            raise NatsPortError("only a NATS post id (n1:<channel>:<n>) has a NATS reaction to remove")
        self._home(got[0])
        await self._require().unreact(got[0], got[1], emoji)

    def _home(self, channel: str) -> None:
        if channel not in self._homes:
            raise NatsPortError(f"~{channel} is no NATS home of this session: nothing sent")

    # --- receiving -----------------------------------------------------------------------
    async def _on_event(self, ev: Event) -> None:
        dm = ev.where == DM_SINCE
        if not dm and ev.where not in self._homes:
            log.debug("nats: dropped a message outside the NATS homes (%s)", ev.where)
            return
        if not is_nats_user(ev.sender):
            log.warning("nats: dropped a message of %r: an agent name has a '-'", ev.sender[:40])
            return
        if dm:
            # A direct message between agents (plan B piece 3): the transport delivers it on
            # `deliver.<name>.dm` and it has no channel. Read only while this home runs agent
            # DMs over NATS; off, it is dropped as it always was.
            if not self._dm_on():
                log.debug("nats: dropped a direct message: agent DMs over NATS are off in ~%s",
                          self._s.home)
                return
            self._dispatch(ev, "")
            return
        try:
            ids = await self.resolve()
        except Exception as exc:  # noqa: BLE001
            log.warning("nats: dropped a message of ~%s: its channel is not known (%s)",
                        ev.where, type(exc).__name__)
            return
        self._dispatch(ev, ids[ev.where])

    def _dispatch(self, ev: Event, channel_id: str) -> None:
        """One delivered message as the agent reads it: a post, or a reaction (also on a DM)."""
        if ev.message.kind == "post":
            self._on_post(ev, channel_id)
        else:
            self._on_reaction(ev, channel_id)

    def _on_post(self, ev: Event, channel_id: str) -> None:
        m, ch = ev.message, ev.where
        root = ""
        if m.root is not None and ch == DM_SINCE:
            # Every sender's DMs share the one lane with no channel: a root there would thread a
            # second sender's DM under the first one's, so a DM is top-level, always (RT-6.36
            # step 7: a DM has no thread).
            log.info("nats: the root of a direct message is ignored: a DM has no thread")
        elif m.root is not None:
            at = self._where.get(m.root)
            if at is not None and at != ch:
                log.warning("nats: dropped a post of %s in ~%s: its root is in ~%s",
                            ev.sender, ch, at)
                return
            if at is None:
                log.info("nats: the root of post %d in ~%s is not known: a top-level post",
                         ev.stream_seq, ch)
            else:
                root = post_id(ch, m.root)
        prefix = self._s.props_prefix
        props = read_props(m.props, prefix)
        nonce = props.pop(f"{prefix}{_NONCE}", None)
        note = props.get("a2a")
        if isinstance(note, dict) and "crown" in note:
            # A NATS sender's owner is not proven, so its crown is no one's: dropped.
            kept = {k: v for k, v in note.items() if k != "crown"}
            if kept:
                props["a2a"] = kept
            else:
                props.pop("a2a")
        raw: dict[str, Any] = {"id": post_id(ch, ev.stream_seq), "channel_id": channel_id, "root_id": root,
               "user_id": self._uid(ev.sender), "create_at": int(ev.time * 1000),
               "message": m.text, "props": props}
        post = Post(
            id=raw["id"], channel_id=channel_id, root_id=root, user_id=raw["user_id"],
            create_at=raw["create_at"], message=m.text, kind=a2a.kind_of(raw),
            intent=a2a.intent_of(raw), to=a2a.ALL if a2a.to_all(raw) else "",
            pr=a2a.pr_of(raw), branch=a2a.branch_of(raw), crowned=False,
            props=MappingProxyType(props), raw=MappingProxyType(raw))
        mem = self._posts.setdefault(ch, OrderedDict())
        mem[ev.stream_seq] = post
        self._where[ev.stream_seq] = ch
        self._highest[ch] = max(self._highest.get(ch, -1), ev.stream_seq)
        if root:
            self._children.setdefault(root, set()).add(ev.stream_seq)
        while len(mem) > self._memory:
            old = min(mem)  # the lowest stream sequence goes, whatever the order of arrival
            gone = mem.pop(old)
            self._where.pop(old, None)
            self._reacts.pop(old, None)
            if gone.root_id:
                kids = self._children.get(gone.root_id)
                if kids is not None:
                    kids.discard(old)
                    if not kids:
                        del self._children[gone.root_id]
        entry = self._echoes.get(nonce) if isinstance(nonce, str) else None
        if entry is not None and entry[0] == ch and not entry[1].done() \
                and self._bus is not None and ev.sender == self._bus.name:
            entry[1].set_result(post)
        self._tell(PostEvent(post=self.inner.with_links(post),
                             channel_type="D" if ch == DM_SINCE else None,  # a DM: no channel
                             edited=False))

    def _on_reaction(self, ev: Event, channel_id: str) -> None:
        m, ch = ev.message, ev.where
        assert m.ref is not None and m.emoji is not None
        at = self._where.get(m.ref)
        if at is None:
            log.info("nats: a reaction to post %d in ~%s is ignored: the post is not known", m.ref, ch)
            return
        if at == DM_SINCE:
            # A reaction to a DM is not carried yet (the transport's dm_react is unused): any name
            # could react to another sender's DM on the one shared lane, so it is dropped.
            log.debug("nats: a reaction to a direct message is dropped: not carried")
            return
        if at != ch:
            log.warning("nats: dropped a reaction of %s in ~%s: its post is in ~%s", ev.sender, ch, at)
            return
        key = (m.emoji, ev.sender)
        if m.kind == "unreaction":
            self._reacts.get(m.ref, {}).pop(key, None)  # the sender's own, nobody else's
            return
        ms = int(ev.time * 1000)
        bucket = self._reacts.setdefault(m.ref, {})
        if key not in bucket and len(bucket) >= MAX_REACTIONS:
            self._capped += 1
            if self._capped % 1000 == 1:  # a foreign sender sets the pace: not a line each
                log.warning("nats: a reaction to post %d in ~%s is dropped: %d reactions already "
                            "(%d dropped so far)", m.ref, ch, MAX_REACTIONS, self._capped)
            return
        bucket[key] = ms
        self._tell(ReactionEvent(
            Reaction(user_id=self._uid(ev.sender), post_id=post_id(ch, m.ref),
                     emoji_name=m.emoji, create_at=ms), channel_id))

    # --- people over NATS (RT-6.42) -----------------------------------------------------------
    def _people_channels(self) -> list[str]:
        """The channels the bus asked for: the home, the guests, the lobby."""
        wanted = [self._s.home, *self._s.guests, *([self._s.lobby_channel] if self._s.lobby else [])]
        return [ch for ch in dict.fromkeys(wanted) if ch]

    async def _people_map(self) -> dict[str, str]:
        """Channel name -> Mattermost id of the bus's channels; one not readable now is read
        again after BRIDGE_TTL_SEC."""
        for ch in self._people_channels():
            if ch in self._people_ids or time.monotonic() - self._people_miss.get(ch, -1e18) < BRIDGE_TTL_SEC:
                continue
            try:
                if planb.on(1, ch):  # RT-6.39 step 6: the credential's channel record, no Mattermost read
                    bus = self._live()
                    got = bus.channel_ids() if bus is not None else None
                    if not got or not got.get(ch):
                        raise NatsPortError("no id in the channel record")
                    self._people_ids[ch] = got[ch]
                elif ch in (await self.resolve()):
                    self._people_ids[ch] = (await self.resolve())[ch]
                elif ch == self.inner.work_channel_name.lower():
                    self._people_ids[ch] = await self.inner.work_channel_id()
                else:
                    self._people_ids[ch] = await self.inner.get_channel_id(ch)
            except Exception as exc:  # noqa: BLE001 — not known now: its people stay on Mattermost
                self._people_miss[ch] = time.monotonic()
                log.debug("nats: the channel ~%s is not known (%s)", ch, type(exc).__name__)
        return self._people_ids

    def _live(self) -> BusLike | None:
        return self._bus if self._fatal is None else None

    def _note_switch(self, ch: str) -> bool:
        """Whether the channel's people come over NATS now; a switch back to Mattermost
        starts the catch-up there (RT-6.42 step 9)."""
        bus = self._live()
        on = bus is not None and bus.people_switched(ch)
        if on and ch not in self._switched:
            self._switched.add(ch)
            log.info("nats: the people of ~%s come over NATS now (the bridge's signal)", ch)
            if bus is not None:
                self._people_at[ch] = max(self._people_at.get(ch, 0), int(bus.server_now * 1000))
        elif not on and ch in self._switched:
            self._switched.discard(ch)
            log.info("nats: the bridge's signal for ~%s ran out: its people are read on Mattermost again", ch)
            self._start_catch_up(ch)
        return on

    def _start_catch_up(self, ch: str, start: int | None = None) -> None:
        task = asyncio.create_task(self._catch_up(ch, start))
        self._catching_up.add(task)
        task.add_done_callback(self._catching_up.discard)

    def _person_gap(self, ch: str) -> None:
        """A gap on the person's consumer (RT-6.42 step 9): what was lost is on Mattermost,
        read from the last people event before the gap (taken now: the event that showed
        the gap moves it on before the task runs)."""
        self._start_catch_up(ch, self._people_at.get(ch, 0))

    async def check_people(self) -> None:
        """Look at the bridge's signals of every channel of the bus (RT-6.42 step 9)."""
        for ch in self._people_channels():
            self._note_switch(ch)
        for ch, start in list(self._owed.items()):
            del self._owed[ch]
            self._start_catch_up(ch, start)

    async def _watch_people(self) -> None:
        while True:
            await asyncio.sleep(PEOPLE_CHECK_SEC)
            try:
                await self.check_people()
            except Exception as exc:  # noqa: BLE001 — the next look tries again
                log.warning("nats: the bridge's signals were not looked at: %s", type(exc).__name__)

    async def _catch_up(self, ch: str, start: int | None = None) -> None:
        """Read the channel on Mattermost from its last people event over NATS on, and deliver
        the people's posts and reactions no key holds (RT-6.42 step 9). Never from further back
        than the keys reach (PEOPLE_KEY_SEC): an older event would come again, now with
        Mattermost's trust. The `since` read also returns older posts with new reactions (a
        reaction moves a post's update_at). A read that fails is tried again at the next look."""
        cid = (await self._people_map()).get(ch)
        bus = self._live()
        if cid is None or bus is None:
            return
        # The floor compares a clock here with Mattermost's create_at: the later of this
        # host's and the NATS server's, so a host clock behind does not reopen the replay;
        # a skew of more than PEOPLE_MARGIN_MS beyond both still could.
        now = max(time.time(), bus.server_now)
        floor = int((now - PEOPLE_KEY_SEC) * 1000) + PEOPLE_MARGIN_MS
        last = self._people_at.get(ch, 0) if start is None else start
        since = max(0, last - PEOPLE_MARGIN_MS, floor)
        try:
            posts = await self.inner.channel_posts(cid, since_ms=since)
            authors = {p.user_id for p in posts} | {r.user_id for p in posts for r in p.reactions}
            bots = await self.inner.bot_accounts(authors - {""})
        except Exception as exc:  # noqa: BLE001 — tried again at the next look
            self._owed[ch] = min(self._owed.get(ch, last), last)
            log.warning("nats: the catch-up of ~%s on Mattermost failed (%s): tried again in %.0f s",
                        ch, type(exc).__name__, PEOPLE_CHECK_SEC)
            return
        me = self.inner.bot_id

        def person(uid: str) -> bool:
            return bool(uid) and uid != me and uid not in bots

        for p in sorted(posts, key=lambda p: p.create_at):
            if (p.create_at >= since and person(p.user_id) and not p.from_webhook and not p.type
                    and not p.delete_at and self._keys.take(("post", p.id))):
                self._tell(PostEvent(post=self.inner.with_links(p), channel_type=None, edited=False))
            for r in sorted(p.reactions, key=lambda r: r.create_at):
                if (r.create_at >= since and person(r.user_id)
                        and self._keys.take_reaction("reaction", p.id, r.emoji_name,
                                                     bus.anchor_of_account(r.user_id))):
                    self._tell(ReactionEvent(r, cid))

    async def _person_on_mattermost(self, event: ChatEvent) -> bool:
        """A Mattermost event of a person in a channel of the bus: dropped while its people
        come over NATS, and when its key is held (it came over NATS before, or by the
        catch-up). An edit is not carried over NATS: dropped there too, else kept as before.
        Bots, webhooks and system posts are never people. In doubt: kept."""
        if isinstance(event, PostEvent):
            post = event.post
            uid, cid = post.user_id, post.channel_id
            if post.from_webhook or post.type:
                return False
        elif isinstance(event, ReactionEvent):
            uid, cid = event.reaction.user_id, event.channel_id
        else:
            return False
        bus = self._live()
        if bus is None or not uid or not cid or uid == self.inner.bot_id:
            return False
        try:
            ch = next((n for n, i in (await self._people_map()).items() if i == cid), None)
            if ch is None or uid in await self.inner.bot_accounts({uid}):
                return False
        except Exception as exc:  # noqa: BLE001 — not known: kept
            log.debug("nats: a person's event not told apart (%s): kept", type(exc).__name__)
            return False
        if self._note_switch(ch):
            return True
        if isinstance(event, PostEvent):
            return False if event.edited else not self._keys.take(("post", event.post.id))
        r = event.reaction
        return not self._keys.take_reaction("reaction", r.post_id, r.emoji_name, bus.anchor_of_account(uid))

    async def _on_person(self, ev: natsbus.PersonEvent) -> None:
        """A person's event over NATS (RT-6.42 step 9): a PostEvent or ReactionEvent while the
        bridge's signal names its channel; dropped otherwise (Mattermost has it) and when its
        key is held."""
        cid = (await self._people_map()).get(ev.where)
        if cid is None:
            log.debug("nats: dropped a person's event in ~%s: the channel is not known", ev.where)
            return
        # Delivered also before the signal (or after it ran out): Mattermost delivers it too
        # then, and the keys drop whichever comes second (RT-6.42 step 9).
        self._note_switch(ev.where)
        m, ch = ev.message, ev.where
        uid = person_id(ev.anchor)
        self._person_names[uid] = m.name
        ms = int(ev.time * 1000)
        if m.kind == "post":
            if not self._keys.take(("post", m.mm)):
                return
            self._judge(("post", m.mm), ev.anchor)
            self._people_at[ch] = max(self._people_at.get(ch, 0), ms)
            post = self.inner.with_links(self._person_post(ev, cid))
            if m.files:
                post = await self._with_files(post)
            self._tell(PostEvent(post=post, channel_type=None, edited=False))
            return
        assert m.emoji is not None
        if not self._keys.take_reaction(m.kind, m.mm, m.emoji, ev.anchor):
            return
        self._people_at[ch] = max(self._people_at.get(ch, 0), ms)
        on_nats = m.ref is not None and self._where.get(m.ref) == ch
        if m.kind == "reaction":
            # Judged under the Mattermost id and, for a NATS post, the id the reads give it.
            self._judge(("reaction", m.mm, m.emoji, ev.anchor), ev.anchor)
            if on_nats and m.ref is not None:
                self._judge(("reaction", post_id(ch, m.ref), m.emoji, ev.anchor), ev.anchor)
        if m.kind == "unreaction":
            if on_nats and m.ref is not None:
                self._reacts.get(m.ref, {}).pop((m.emoji, uid), None)
            return
        target = m.mm
        if on_nats and m.ref is not None:
            target = post_id(ch, m.ref)
            bucket = self._reacts.setdefault(m.ref, {})
            if (m.emoji, uid) in bucket or len(bucket) < MAX_REACTIONS:
                bucket[(m.emoji, uid)] = ms
        self._tell(ReactionEvent(Reaction(user_id=uid, post_id=target, emoji_name=m.emoji, create_at=ms), cid))

    def _judge(self, key: tuple[str, ...], anchor: str, approved: bool | None = None) -> None:
        """Remember how an event was judged as it came (RT-6.42 step 9): without a fresh
        approver record of `anchor` (or, when given, not `approved`) for the reads; with one
        too, which plan B piece 4 reads (`unapproved`)."""
        bus = self._live()
        if bus is None:
            self._unapproved.pop(key, None)
            return
        if bus.approver(anchor) if approved is None else approved:
            self._unapproved.pop(key, None)
            _remember(self._approved, key)
        else:
            self._approved.pop(key, None)
            _remember(self._unapproved, key)

    async def unapproved(self, posts: Sequence[Post]) -> set[tuple[str, ...]]:
        """What of these came over NATS from a person without a fresh approver record, as
        the event was judged (RT-6.42 step 9): a post by its Mattermost id, a reaction by
        its post, emoji and the anchor its account's record names. With plan B piece 4 on,
        also every post and reaction of a Mattermost account that the login service's record
        binds to a person and that this session did not judge as it came (history, a
        backfill, a reconnect, a time without its own attestation or before the session):
        unless that account approves now (_account_approves), as if it came now. Fail
        closed: a non-approver's answer never answers an ask (tasks.ask_answered)."""
        bus = self._live()
        piece4 = self._piece4()
        if bus is None or not (self._unapproved or piece4):
            return set()
        bound = bus.bound_accounts() if piece4 else {}
        approves: dict[str, bool] = {}

        def unjudged(key: tuple[str, ...], uid: str) -> bool:
            # plan B piece 4: a bound account's event judged neither way, judged by its record now
            if uid not in bound or key in self._approved:
                return False
            if uid not in approves:
                approves[uid] = self._account_approves(uid)
            return not approves[uid]

        out: set[tuple[str, ...]] = set()
        for p in posts:
            if ("post", p.id) in self._unapproved or unjudged(("post", p.id), p.user_id):
                out.add(("post", p.id))
            for r in p.reactions:
                # a reaction read from the NATS memory names its person (hu:<anchor>) itself
                anchor = r.user_id[len(PERSON):] if is_person(r.user_id) else bus.anchor_of_account(r.user_id)
                key = ("reaction", p.id, r.emoji_name, anchor)
                if key in self._unapproved or unjudged(key, r.user_id):
                    out.add(("reaction", p.id, r.emoji_name, r.user_id))
        return out

    async def _with_files(self, post: Post) -> Post:
        """A person's files stay on Mattermost (point 18): read by the post's Mattermost id,
        so get_file fetches them; without them on a failed read (logged)."""
        try:
            thread = await self.inner.thread(post.id)
            found = next((p for p in thread.posts if p.id == post.id), None)
        except Exception as exc:  # noqa: BLE001 — the count stays in the raw post
            log.info("nats: the files of a person's post were not read (%s)", type(exc).__name__)
            return post
        return dataclasses.replace(post, files=found.files) if found is not None else post

    def _person_post(self, ev: natsbus.PersonEvent, channel_id: str) -> Post:
        """A person's post as a read gives it: its Mattermost id, its root (the NATS post
        `root` names when it is in the memory, else `mm_root`), author person_id(anchor),
        no props (the body has none), the server's time."""
        m, ch = ev.message, ev.where
        root = ""
        if m.root is not None and self._where.get(m.root) == ch:
            root = post_id(ch, m.root)
        elif m.mm_root:
            root = m.mm_root
        raw: dict[str, Any] = {"id": m.mm, "channel_id": channel_id, "root_id": root,
                               "user_id": person_id(ev.anchor), "create_at": int(ev.time * 1000),
                               "message": m.text, "props": {}}
        if m.files:
            raw["file_count"] = m.files  # the files stay on Mattermost
        if m.cut:
            raw["cut"] = True
        return Post(id=m.mm, channel_id=channel_id, root_id=root, user_id=raw["user_id"],
                    create_at=raw["create_at"], message=m.text, kind=a2a.kind_of(raw),
                    pr=a2a.pr_of(raw), branch=a2a.branch_of(raw), raw=MappingProxyType(raw))

    async def trusted_humans(self) -> dict[str, str]:
        """The adapter's, and a person over NATS by each anchor of this session's own fresh
        attestation (RT-6.41), under the name it was attested with (RT-6.42 step 9). UNBOUND
        is no anchor: never one of them. With plan B piece 4 on, the login service's alone
        (_attested_humans): no Mattermost id from the host, no lookup."""
        if self._piece4():
            return self._attested_humans()
        out = dict(await self.inner.trusted_humans())
        bus = self._live()
        att = bus.attestation_of(bus.name) if bus is not None else None
        for anchor, name in att.trusted if att is not None else ():
            out.setdefault(person_id(anchor), name)
        return out

    async def may_approve(self, user_id: str) -> bool | None:
        """A person over NATS approves only with a fresh approver record (RT-6.42 step 9);
        None for everyone else (the adapter's rule). With plan B piece 4 on, a Mattermost
        account approves alike, by the anchor its record names (_account_approves)."""
        if self._piece4() and not is_person(user_id):
            return self._account_approves(user_id)
        if not is_person(user_id):
            return await self.inner.may_approve(user_id)
        bus = self._live()
        return bus is not None and bus.approver(user_id[len(PERSON):])

    async def bot_accounts(self, user_ids: set[str]) -> set[str]:
        nats = {i for i in user_ids if is_nats_user(i)}
        rest = {i for i in user_ids if not is_nats_user(i) and not is_person(i)}
        return (await self.inner.bot_accounts(rest) if rest else set()) | nats

    # --- reading ---------------------------------------------------------------------------
    def _shown(self, ch: str, seq: int) -> Post:
        """The post with its reactions and reply count, as a read gives it."""
        post = self._posts[ch][seq]
        reactions = tuple(
            Reaction(user_id=self._uid(sender), post_id=post.id, emoji_name=emoji, create_at=ms)
            for (emoji, sender), ms in sorted(self._reacts.get(seq, {}).items(), key=lambda kv: kv[1]))
        replies = len(self._children.get(post.id, ()))
        return dataclasses.replace(post, reactions=reactions, reply_count=replies)

    def _channel_posts(self, ch: str) -> list[Post]:
        """The channel's remembered posts, oldest first."""
        return [self._shown(ch, seq) for seq in sorted(self._posts.get(ch, {}))]

    async def thread(self, post_id: str) -> Thread:
        got = parse_post_id(post_id)
        if got is None:
            return await self.inner.thread(post_id)
        ch, seq = got
        self._home(ch)
        known = self._posts[ch].get(seq) if ch in self._posts else None
        if known is None:
            raise NotFound(f"post {post_id} is not in this session's NATS memory")
        root = known.root_id or known.id
        posts = [p for p in self._channel_posts(ch) if root in (p.id, p.root_id)]
        if not any(p.id == root for p in posts):
            raise NotFound(f"the root of thread {post_id} is no longer in this session's NATS memory")
        return Thread(root, order=tuple(p.id for p in posts), posts=tuple(posts))

    async def post_channel_id(self, post_id: str) -> str:
        got = parse_post_id(post_id)
        if got is None:
            return await self.inner.post_channel_id(post_id)
        ids = await self.resolve()
        if got[0] not in ids:
            raise NotFound(f"~{got[0]} is no NATS home of this session")
        return ids[got[0]]

    def _merged(self, name: str, others: Iterable[Post], keep: Callable[[Post], bool]) -> list[Post]:
        """A NATS home's posts (those `keep` lets through) and Mattermost's `others` in
        the same channel (a person's post, an ask, a BYE), oldest first."""
        got = {p.id: p for p in others}
        got.update({p.id: p for p in self._channel_posts(name) if keep(p)})
        return sorted(got.values(), key=lambda p: p.create_at)

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        name = await self.nats_name(channel_id)
        mm = await self.inner.channel_posts(channel_id, per_page=per_page, since_ms=since_ms)
        if name is None:
            return mm
        mm = await self._unmirrored(name, mm)  # the bridge's copies: NATS has them (RT-6.36 step 4)
        posts = self._merged(name, mm, lambda p: since_ms is None or p.create_at >= since_ms)
        posts.reverse()  # newest first
        return posts[:per_page] if per_page is not None else posts

    async def list_recent(self, *, channel_id: str | None = None, per_page: int = 20,
                          since_ms: int | None = None) -> list[Post]:
        cid = await self._home_or(channel_id)
        name = await self.nats_name(cid)
        mm = await self.inner.list_recent(channel_id=channel_id, per_page=per_page, since_ms=since_ms)
        if name is None:
            return mm
        mm = await self._unmirrored(name, mm)  # the bridge's copies: NATS has them (RT-6.36 step 4)
        posts = self._merged(name, mm, lambda p: since_ms is None or p.create_at >= since_ms)
        if since_ms is None:
            posts = posts[-per_page:] if per_page > 0 else []
        return [self.inner.with_links(p) for p in posts]

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        cid = await self._home_or(channel_id)
        name = await self.nats_name(cid)
        mm = await self.inner.posts_after(since_ms, channel_id=channel_id, max_pages=max_pages)
        if name is None:
            return mm
        mm = await self._unmirrored(name, mm)  # the bridge's copies: NATS has them (RT-6.36 step 4)
        return self._merged(name, mm, lambda p: p.create_at >= since_ms)  # since, as Mattermost's

    async def reactions(self, post_id: str) -> list[Reaction]:
        got = parse_post_id(post_id)
        if got is None:
            return await self.inner.reactions(post_id)
        ch, seq = got
        self._home(ch)
        if ch not in self._posts or seq not in self._posts[ch]:
            return []
        return list(self._shown(ch, seq).reactions)

    async def member_channels(self) -> list[UnreadChannel]:
        """The adapter's, a NATS home marked `nats` with its newest remembered
        NATS post as `nats_last_post_at` (Mattermost does not know it): its NATS
        posts have a read watermark of their own (watermark.lane), and the unread
        count and the inbox's backfill compare each lane's newest with its mark."""
        rows = await self.inner.member_channels()
        ids = await self.resolve()
        newest = {ids[ch]: max((p.create_at for p in mem.values()), default=0)
                  for ch, mem in self._posts.items() if ch in ids}
        homes = set(ids.values())
        return [dataclasses.replace(r, nats=True, nats_last_post_at=newest.get(r.channel_id, 0))
                if r.channel_id in homes else r for r in rows]

    # --- the senders: agent names, no Mattermost accounts ----------------------------------------
    def _person_name(self, user_id: str) -> str:
        """The name a person over NATS is shown with: the bridge's (display only)."""
        return self._person_names.get(user_id) or user_id[len(PERSON):]

    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        nats = {i for i in user_ids if is_nats_user(i)}
        people = {i for i in user_ids if is_person(i)}
        rest = set(user_ids) - nats - people
        out = await self.inner.usernames(rest) if rest else {}
        return out | {i: i for i in nats} | {i: self._person_name(i) for i in people}

    async def username(self, user_id: str) -> str:
        if is_person(user_id):
            return self._person_name(user_id)
        return user_id if is_nats_user(user_id) else await self.inner.username(user_id)

    async def agents(self, user_ids: set[str]) -> set[str]:
        nats = {i for i in user_ids if is_nats_user(i)}
        rest = {i for i in user_ids if not is_nats_user(i) and not is_person(i)}
        return (await self.inner.agents(rest) if rest else set()) | nats

    async def agent_owner(self, user_id: str) -> str | None:
        # A NATS sender's owner is the one its presence proves (RT-6.37), else "" (an
        # owner that is none), which is foreign for the peer split (owners.read); not
        # None, which is "not readable" (also foreign, ADR-016, but logged as an outage).
        if is_person(user_id):
            return ""  # a person, no agent (RT-6.42)
        if self._piece4():
            return await self._owner4(user_id)
        if not is_nats_user(user_id):
            return await self.inner.agent_owner(user_id)
        if self._bus is None or self._fatal is not None:
            return ""
        if self._records_on() and self._circle(user_id) is None:
            # The login service writes records (one was seen in this process): a sender
            # without a usable one (none, expired, malformed, another anchor than its
            # presence) has no owner. The name comparison is only for a bus whose login
            # service writes none; it never comes back by an expiry (ADR-016).
            return ""
        return self._bus.owner_of(user_id)

    def _circle(self, agent: str) -> AttestedCircle | None:
        """The agent's circle by anchors: its fresh attestation, when the owner anchor in it is
        the one its presence subject pins; None otherwise (no usable attestation)."""
        bus = self._bus if self._fatal is None else None
        if bus is None:
            return None
        att = bus.attestation_of(agent)
        anchor = bus.anchor_of(agent)
        if att is None or not anchor or att.owner_anchor != anchor:
            return None
        return AttestedCircle(att.owner_anchor, frozenset(a for a, _ in att.trusted), att.cut,
                              att.owner_name, frozenset(n for _, n in att.trusted))

    def _records_on(self) -> bool:
        """The login service writes records; with plan B piece 4 on always: then no owner is
        ever compared by name (_owner4, the crown's claimants)."""
        bus = self._bus if self._fatal is None else None
        return self._piece4() or (bus is not None and bus.records_on())

    def _own_anchors(self) -> frozenset[str] | None:
        bus = self._bus if self._fatal is None else None
        att = bus.attestation_of(bus.name) if bus is not None else None
        return frozenset(a for a, _ in att.trusted) if att is not None else None

    async def attested_circle(self, user_id: str) -> AttestedCircle | None:
        if self._piece4():
            return await self._circle4(user_id)
        return self._circle(user_id) if is_nats_user(user_id) else None

    async def own_anchors(self) -> frozenset[str] | None:
        return self._own_anchors()

    # --- plan B piece 4: trusted humans and owners from the login service (RT-6.41 step 6) ------
    def _piece4(self) -> bool:
        """Whether this home takes every check of a trusted human and every agent's owner from
        the login service (plan B piece 4): `planb.on(4, home)`. A switch that cannot be read
        counts as off: the old path, the Mattermost ids and the bots' descriptions."""
        try:
            return bool(self._s.home) and bool(planb.on(4, self._s.home))
        except Exception as exc:  # noqa: BLE001 — in doubt off: the old path
            log.info("nats: the trust switch was not read (%s): the old path", type(exc).__name__)
            return False

    @property
    def trusted_source(self) -> str:
        """Where the trusted humans come from (bus_info, RT-6.8): with piece 4 on, the login
        service's attestation of this session; else the adapter's."""
        if not self._piece4():
            return str(getattr(self.inner, "trusted_source", ""))
        bus = self._live()
        if bus is not None and bus.attestation_of(bus.name) is not None:
            return "the login service's attestation of this session (plan B piece 4)"
        return "none yet: no fresh attestation of this session from the login service (plan B piece 4)"

    def _own_attestation(self) -> Attestation | None:
        """This session's own fresh attestation (RT-6.41), None without one; with piece 4 on,
        its absence is logged once, and its return."""
        bus = self._live()
        att = bus.attestation_of(bus.name) if bus is not None else None
        if att is None and not self._own_missing:
            self._own_missing = True
            log.warning("plan B piece 4: no fresh attestation of this session from the login service: "
                        "nobody counts as a trusted human and every other agent as foreign until it comes")
        elif att is not None and self._own_missing:
            self._own_missing = False
            log.info("plan B piece 4: the login service's attestation of this session is there again")
        return att

    def _attested_humans(self) -> dict[str, str]:
        """Piece 4: this session's trusted humans, {id: name}, by the anchors of its own fresh
        attestation alone: first each Mattermost account the login service's record binds to
        one of them (RT-6.42 step 3; a stale record too, as the bridge reads it), then each
        anchor as a person over NATS (hu:<anchor>), under the name it was attested with. {}
        without a fresh attestation: nobody (fail closed). Nothing from the host's ids, no
        lookup with the bot token."""
        att = self._own_attestation()
        bus = self._live()
        if att is None or bus is None:
            return {}
        names = dict(att.trusted)
        out = {account: names[anchor] for account, anchor in sorted(bus.bound_accounts().items())
               if anchor in names}
        for anchor, name in att.trusted:
            out.setdefault(person_id(anchor), name)
        return out

    def _account_anchor(self, user_id: str) -> str:
        """Piece 4: the anchor of one of this session's trusted humans that the login service's
        record binds the Mattermost account `user_id` to; '' for every other account."""
        att = self._own_attestation()
        bus = self._live()
        if att is None or bus is None:
            return ""
        anchor = bus.bound_accounts().get(user_id, "")
        return anchor if anchor and any(a == anchor for a, _ in att.trusted) else ""

    def _account_approves(self, user_id: str) -> bool:
        """Piece 4: a Mattermost account approves only as a person over NATS does (RT-6.42
        step 9): bound to a trusted anchor, and that account's own record fresh with
        `approver`; another account of the same person approves nothing for it."""
        anchor = self._account_anchor(user_id)
        bus = self._live()
        return bool(anchor) and bus is not None and bus.account_approver(user_id)

    async def trust_closed(self) -> bool:
        """Piece 4 on and no fresh own attestation (RT-6.41 step 6): the gate and the owner
        split stay on and nobody is a peer, whatever the host configured (fail closed)."""
        return self._piece4() and self._own_attestation() is None

    def _judge_on_mattermost(self, event: ChatEvent) -> None:
        """Piece 4: a post or reaction that comes on Mattermost (a DM, a channel without the
        bridge's fresh signal) from an account the login service's record binds to a person is
        judged as it comes, as one over NATS is (_judge): without that account's approval
        (_account_approves; none while this session has no fresh own attestation) the reads
        mark it not_approver. One the session did not see come, the reads judge (unapproved)."""
        bus = self._live() if self._piece4() else None
        if bus is None:
            return
        key: tuple[str, ...]
        if isinstance(event, PostEvent):
            post = event.post
            if post.from_webhook or post.type or event.edited:
                return
            uid, key = post.user_id, ("post", post.id)
            anchor = bus.bound_accounts().get(uid, "")
        elif isinstance(event, ReactionEvent):
            r = event.reaction
            uid, anchor = r.user_id, bus.bound_accounts().get(r.user_id, "")
            key = ("reaction", r.post_id, r.emoji_name, anchor)
        else:
            return
        if anchor:
            self._judge(key, anchor, self._account_approves(uid))

    async def _owner4(self, user_id: str) -> str | None:
        """Piece 4: an agent's owner from the login service alone, never a bot's description
        (RT-6.24) nor a bare presence subject: the owner's name in the circle _circle4 finds.
        '' (no owner: foreign) without one; None (not readable: in doubt the owner split stays
        on, owners.mine) only for this session's own bot, so that a missing own attestation
        never turns the split off."""
        circle = await self._circle4(user_id)
        if circle is not None:
            return circle.owner_name or circle.owner_anchor
        return None if user_id == self.inner.bot_id else ""

    async def _circle4(self, user_id: str) -> AttestedCircle | None:
        """Piece 4: the circle of the agent behind `user_id` from the login service's
        attestation (RT-6.41): a NATS sender's own; a Mattermost bot's that of the NATS name
        linked to it (_linked_name); a person's none."""
        if is_person(user_id):
            return None
        if is_nats_user(user_id):
            return self._circle(user_id)
        name = await self._linked_name(user_id)
        return self._circle(name) if name else None

    async def _linked_name(self, user_id: str) -> str:
        """Piece 4: the fresh NATS name linked to the Mattermost bot `user_id`, '' for none.
        This session's own bot is its own name. Any other bot only by the one test of a link
        (RT-6.37 step 6, `linked`): its status shows the name's tag and the name's presence
        names that very bot. When no fresh presence names the bot, nothing is read on
        Mattermost; else the bot's status is (the link is piece 2's to move)."""
        bus = self._live()
        if bus is None or not user_id:
            return ""
        if user_id == self.inner.bot_id:
            return bus.name
        if not any(bus.bot_of(n) == user_id and bus.presence_of(n) != "offline"
                   for n in bus.presence_snapshot()):
            return ""
        try:
            username = (await self.inner.usernames({user_id})).get(user_id, "")
            user = (await self.inner.bots_named({username})).get(username.lower()) if username else None
        except Exception as exc:  # noqa: BLE001 — not read: no link, so the bot has no owner
            log.info("nats: the status of bot %s was not read (%s): no link", user_id, type(exc).__name__)
            return ""
        if user is None or user.id != user_id:
            return ""
        text = str((user.custom_status or {}).get("text") or "")
        return self.linked([{"id": user_id, "text": text}]).get(user_id, "")

    async def bot_description(self, user_id: str) -> str | None:
        # plan B piece 4: never a bot's description, nor the registrar's attestation of it
        return (None if is_nats_user(user_id) or is_person(user_id) or self._piece4()
                else await self.inner.bot_description(user_id))

    async def agent_attestation(self, user_id: str) -> str | None:
        return (None if is_nats_user(user_id) or is_person(user_id) or self._piece4()
                else await self.inner.agent_attestation(user_id))

    async def attestation_record(self, bot_id: str) -> str | None:
        return (None if is_nats_user(bot_id) or is_person(bot_id) or self._piece4()
                else await self.inner.attestation_record(bot_id))

    async def presences(self, user_ids: list[str]) -> dict[str, str]:
        """Mattermost's for its accounts; for a NATS sender its NATS presence (RT-6.37):
        online or away while fresh by the server's clock, else offline."""
        nats = [i for i in user_ids if is_nats_user(i)]
        rest = [i for i in user_ids if not is_nats_user(i) and not is_person(i)]
        out = await self.inner.presences(rest) if rest else {}
        out |= {i: "offline" for i in user_ids if is_person(i)}  # no Mattermost account (RT-6.42)
        bus = self._bus if self._fatal is None else None
        return out | {i: bus.presence_of(i) if bus is not None else "offline" for i in nats}

    def linked(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
        """bot id -> NATS name, for the roster rows (dicts with `id` and `text`) whose bot
        and NATS name name each other (RT-6.37): the bot's status shows the name's tag AND
        the name's presence names the bot, and the name is not offline. A tag that fits
        more than one fresh name links none; a one-sided claim gives no link."""
        bus = self._bus if self._fatal is None else None
        if bus is None:
            return {}
        fresh = [n for n in bus.presence_snapshot() if bus.presence_of(n) != "offline"]
        out: dict[str, str] = {}
        for row in rows:
            tag = lease.link_of(str(row.get("text") or ""))
            if not tag:
                continue
            same = [n for n in fresh if natsbus.link_tag(n) == tag]
            if len(same) > 1 and tag not in self._ambiguous:
                self._ambiguous.add(tag)
                log.warning("nats: the link tag %s fits %d fresh NATS names: no bot is linked by it", tag, len(same))
            elif len(same) == 1:
                self._ambiguous.discard(tag)
            if len(same) == 1 and bus.bot_of(same[0]) == str(row.get("id")):
                out[str(row["id"])] = same[0]
        return out

    def ambiguous(self) -> list[str]:
        """The link tags that fit more than one fresh NATS name, as `linked` last saw them
        (RT-6.37); sorted."""
        return sorted(self._ambiguous)

    def owner_of(self, name: str) -> str:
        """The owner a NATS name's presence subject names, '' for none or no bus."""
        bus = self._bus if self._fatal is None else None
        return bus.owner_of(name) if bus is not None else ""

    # --- the crown (RT-6.38) ---------------------------------------------------------
    def down(self) -> str:
        """Why the NATS side is lost for good (a failed start), '' otherwise."""
        return str(self._fatal) if self._fatal is not None else ""

    async def prime(self) -> None:
        """Read this session's trusted humans, which the crown's standing rule needs:
        in doubt (ADR-016) the ones last read or configured, never "none" because a
        lookup failed (owners.names_in_doubt); only none configured lets every one stand."""
        # plan B piece 4: the names of this session's own attestation, no Mattermost lookup
        self._names, _ = await owners.names_in_doubt(self if self._piece4() else self.inner)

    def crown_view(self, home: str) -> lease.CrownView | None:
        """The crown of the NATS home `home` as the bus says it (RT-6.38 step 2): the claimants
        with a fresh presence that are members of the channel, in claim order, of which those
        that stand by the Mattermost election's rule (lease.standing, owners from the
        presence subjects) may hold it; the holder is the first. None, which is neither free
        nor mine, while the bus is not attached, failed, not connected or not synced, or the
        trusted humans were not read yet, or the echo of this agent's own presence (published
        every 30 s) has not come back for more than OWN_ECHO_SEC by the monotonic clock: a
        half-open connection delivers nothing, and the others take over after 90 s."""
        bus = self._bus
        if (bus is None or self._fatal is not None or not bus.connected or not bus.synced
                or self._names is None or home not in self._homes
                or bus.own_echo_age() > natsbus.OWN_ECHO_SEC):
            return None
        me = bus.name
        info = bus.claim_info(home)
        order = natsbus.crown_candidates({a: seq for a, (seq, _) in info.items()}, bus.presence_snapshot(),
                                         bus.server_now, partial(bus.is_member, home),
                                         at={a: t for a, (_, t) in info.items()})
        agents: dict[str, owners.Owner | None] = {}
        records_on = self._records_on()
        if records_on:
            # With records, a claimant without a usable one does not stand at all, also
            # when it is the only one (it would otherwise show as the holder).
            order = [a for a in order if a == me or self._circle(a) is not None]
        for a in dict.fromkeys([*order, me]):
            circle = self._circle(a)
            agents[a] = (owners.Owner(owner=circle.owner_anchor, trusts=circle.trusted_anchors, cut=circle.cut,
                                      attested=True, by_anchor=True) if circle is not None
                         # with records on, no usable one means no owner (not a name match)
                         else owners.Owner(owner="" if records_on else bus.owner_of(a), trusts=frozenset(), cut=False))
        standing = lease.standing(order, me, self._names, agents, self._own_anchors()).ids
        mine = info.get(me)
        return lease.CrownView(me=me, holder=standing[0] if standing else "", candidates=tuple(standing),
                               member=bus.is_member(home, me),
                               claim_age=bus.server_now - mine[1] if mine is not None else None)

    def lowest_claim(self, home: str, names: Iterable[str]) -> str:
        """Of `names`, the one whose claim is first in claim order (server time, sequence) in
        the last known claims of `home`; '' when none of them has a known claim."""
        bus = self._bus if self._fatal is None else None
        if bus is None:
            return ""
        info = bus.claim_info(home)
        known = sorted((info[n][1], info[n][0], n) for n in names if n in info)
        return known[0][2] if known else ""

    async def claim(self, home: str) -> None:
        """Claim the crown of `home` (again): the claim moves behind every other. Waits a
        moment for the claim's own copy to come back, so the view that follows has it."""
        bus = self._require()
        before = bus.claim_info(home).get(bus.name)
        await bus.claim(home)
        for _ in range(int(CLAIM_ECHO_SEC / 0.05)):
            if bus.claim_info(home).get(bus.name) != before:
                return
            await asyncio.sleep(0.05)

    async def whereabouts(self, channel_id: str, name: str) -> str | None:
        """Where a NATS name is, for a NATS home's channel (RT-6.37): `gone` (offline),
        `here` (fresh and a member of that home), else `elsewhere`; None when the channel
        is no NATS home of this port or the bus cannot tell (not attached, failed, not
        connected, not synced)."""
        home = await self.nats_name(channel_id)
        bus = self._bus
        if home is None or bus is None or self._fatal is not None or not bus.connected or not bus.synced:
            return None
        if bus.presence_of(name) == "offline":
            return "gone"
        return "here" if bus.is_member(home, name) else "elsewhere"

    async def is_member(self, channel_id: str, user_id: str) -> bool:
        if not is_nats_user(user_id):
            return await self.inner.is_member(channel_id, user_id)
        name = await self.nats_name(channel_id)
        return name is not None and any(p.user_id == user_id for p in self._posts.get(name, {}).values())


def _tag_of(bus: BusLike) -> str:
    return natsbus.link_tag(bus.name)


def _final(exc: BaseException) -> bool:
    """A start failure that waiting does not cure: a credential naming another agent,
    wrong settings (a ValueError, but not an answer that did not decode), a misconfigured
    login, the human declining it or the wrong human approving it."""
    from .login import ApprovalDenied, ConfigError, WrongHuman
    if isinstance(exc, json.JSONDecodeError | UnicodeError):
        return False
    return isinstance(exc, NameMismatch | ValueError | NatsPortError | ApprovalDenied | ConfigError
                      | WrongHuman)


def _describe(exc: BaseException) -> str:
    """The error for the log: its type, and its text where it is ours or the login's
    (they hold no token, no seed)."""
    from .login import LoginError
    if isinstance(exc, NatsBusError | LoginError | NatsPortError | ValueError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


async def _owner_of(login: natsbus.LoginLike) -> str:
    """The owner the login service names the agent by: the access token's preferred_username."""
    from .login import payload_of
    owner = payload_of(await login.access_token()).get("preferred_username")
    if not isinstance(owner, str) or not owner:
        raise NatsPortError("the access token has no preferred_username")
    return owner


# --- the events ------------------------------------------------------------------------


class NatsEvents:
    """The EventSource of a NatsPort's homes: its posts and reactions as PostEvent
    and ReactionEvent. What came before the inbox connected is not replayed here
    (the inbox's backfill reads it from the port's memory). open subscribes also
    while the bus is not started yet, so the first post after its start is
    delivered; it raises only when the homes are down. The bus reconnects
    itself, so a stream stays open through that; it ends on close() or the
    port's stop and raises when the homes are down. Typing over NATS is nothing."""

    def __init__(self, port: NatsPort) -> None:
        self._port = port
        self._queue: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None

    @property
    def connected(self) -> bool:
        return self._queue is not None and self._port.failure is None

    async def open(self) -> None:
        if self._port.failure is not None:
            raise self._port.failure
        if self._queue is None:
            self._queue = self._port.subscribe()

    def events(self) -> EventStream:
        return _NatsStream(self)

    async def typing(self, channel_id: str, parent_id: str) -> None:
        return None

    async def close(self, queue: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None) -> None:
        """End the subscription; with `queue`, only while it is still that one: a stream
        closing late never ends the subscription of the connection after it."""
        if queue is not None and self._queue is not queue:
            return
        q, self._queue = self._queue, None
        if q is not None:
            self._port.unsubscribe(q)


class _NatsStream:
    def __init__(self, source: NatsEvents,
                 queue: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None) -> None:
        self._source = source
        self._queue = queue if queue is not None else source._queue

    def __aiter__(self) -> _NatsStream:
        return self

    async def __anext__(self) -> ChatEvent:
        q = self._queue
        if q is None or self._source._queue is not q:
            raise StopAsyncIteration
        item = await q.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, NatsPortError):
            raise item
        return item

    async def aclose(self) -> None:
        await self._source.close(self._queue)


def _put_last(q: asyncio.Queue[Any], item: Any) -> int:
    """Put `item` into a bounded queue; when it is full the oldest entry goes (the end
    and a failure always get through). Returns how many entries went."""
    gone = 0
    while True:
        try:
            q.put_nowait(item)
            return gone
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
                gone += 1


class MergedEvents:
    """Mattermost's events for what stays there (people, every other channel) and
    the NATS events of the homes, in one source. It is connected when Mattermost is:
    whatever NATS does (not started yet, down, failed) never fails or closes the
    Mattermost side. The NATS side is opened again after `retry_sec` (doubling up to
    30 s) when its open or its stream failed, until the homes are down for good (the
    log says so once); a NATS stream that ends cleanly (closed, the port stopped) is
    not opened again. open opens Mattermost's and a fresh NATS subscription (the
    last connection's ends), so each connection's stream reads its own. A stream
    ends (or raises) when Mattermost's does, and closes the other side, only its
    own subscription, so the next open connects both afresh and a stream closing
    late never silences the next one; close() ends every stream still running, so
    none keeps reading beside the next one."""

    def __init__(self, chat: EventSource, nats: NatsEvents, port: NatsPort, *,
                 retry_sec: float = START_RETRY_SEC) -> None:
        self._chat, self._nats, self._port = chat, nats, port
        self._retry = retry_sec
        # Weak: a stream given out and dropped unread (its reader cancelled before
        # its first step) leaves no entry; one that is read has its tasks alive.
        self._streams: weakref.WeakSet[_MergedStream] = weakref.WeakSet()

    @property
    def connected(self) -> bool:
        return self._chat.connected

    async def open(self) -> None:
        await self._chat.open()
        await self._nats.close()  # the last connection's subscription: this one gets its own
        try:
            await self._nats.open()
        except Exception as exc:  # noqa: BLE001 — NATS never takes Mattermost down
            log.warning("nats: its events are not open yet (%s): Mattermost's flow, NATS is tried again",
                        _describe(exc))

    def events(self) -> EventStream:
        stream = _MergedStream(self._chat.events(), self._nats, self._retry, self._streams.discard,
                               self._nats._queue, self._port.skip)
        self._streams.add(stream)
        return stream

    async def typing(self, channel_id: str, parent_id: str) -> None:
        if await self._port.is_nats_channel(channel_id):
            return await self._nats.typing(channel_id, parent_id)
        await self._chat.typing(channel_id, parent_id)

    async def close(self) -> None:
        try:
            for stream in list(self._streams):
                await stream.aclose()
            await self._nats.close()
        finally:
            await self._chat.close()


class _MergedStream:
    def __init__(self, chat: EventStream, nats: NatsEvents, retry_sec: float,
                 gone: Callable[[_MergedStream], None],
                 bound: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None,
                 skip: Callable[[ChatEvent], Awaitable[bool]] | None = None) -> None:
        self._chat, self._nats, self._retry = chat, nats, retry_sec
        self._skip = skip  # the bridge's copies on Mattermost's side (NatsPort.skip, RT-6.36 step 4)
        self._gone = gone
        # The NATS subscription this stream reads: the one open when it was given out
        # (events()), else the one its pump opens. Never a later connection's.
        self._bound = bound
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(MAX_QUEUE)
        self._tasks: list[asyncio.Task[None]] = []
        self._ended = False
        self._dropped = 0

    def __aiter__(self) -> _MergedStream:
        return self

    async def _pump(self, stream: EventStream) -> None:
        """Mattermost's side: its end or error ends the stream (blocks while the reader lags)."""
        try:
            async for event in stream:
                if self._skip is not None and await self._skip(event):
                    continue
                await self._queue.put(("event", event))
            await self._queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — handed to the reader
            await self._queue.put(("error", exc))

    async def _pump_nats(self) -> None:
        """The NATS side: never ends the stream. A failure is logged and the side opens
        again; when the homes are down for good it stops, Mattermost's side goes on."""
        delay = self._retry
        while True:
            try:
                if self._bound is None:
                    await self._nats.open()
                    self._bound = self._nats._queue
                if self._bound is None or self._bound is not self._nats._queue:
                    # A later open() gave the source a new subscription: that is the
                    # next stream's, never read beside it.
                    log.info("nats: this stream's subscription was replaced: its NATS side ends")
                    return
                async for event in _NatsStream(self._nats, self._bound):
                    delay = self._retry
                    try:
                        self._queue.put_nowait(("event", event))
                    except asyncio.QueueFull:
                        self._dropped += 1
                        if self._dropped % 1000 == 1:
                            log.warning("nats: the merged stream is full (%d): events dropped (%d so far)",
                                        MAX_QUEUE, self._dropped)
                # A clean end: the NATS side was closed (a new connection has its own
                # subscription) or the port stopped. Opening it again here would read
                # beside the next stream and take half of its events. Its own
                # subscription goes (after a stop it is still the source's).
                log.info("nats: its event stream ended")
                await self._nats.close(self._bound)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if self._nats._port.failure is not None:
                    log.warning("nats: its events stop for good (%s); Mattermost's go on", _describe(exc))
                    return
                log.warning("nats: its events failed (%s), opening again in %.1f s", _describe(exc), delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max(START_RETRY_MAX_SEC, self._retry))

    async def __anext__(self) -> ChatEvent:
        if self._ended:
            raise StopAsyncIteration
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._pump(self._chat)),
                           asyncio.create_task(self._pump_nats())]
        what, value = await self._queue.get()
        if what == "event":
            event: ChatEvent = value
            return event
        await self.aclose()
        if what == "error":
            raise value
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self._ended = True
        self._gone(self)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._chat.aclose()
        if self._bound is not None:  # only its own: never the next connection's
            with contextlib.suppress(Exception):
                await self._nats.close(self._bound)
