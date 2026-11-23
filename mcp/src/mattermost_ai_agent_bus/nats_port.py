"""A home that runs over NATS (RT-6.36, ADR-014 points 3 and 16).

`NatsPort` wraps the Mattermost adapter (the BusClient) and a `natsbus.NatsBus`.
For the channels of a home named in MM_NATS_HOMES the agents' posts, threads and
reactions go over NATS; everything about people stays on Mattermost (their posts
and reactions arrive over the Mattermost WebSocket, trust, headers, pinned posts,
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
bus's own props (`a2a`) travel as flat keys `<MM_PROPS_PREFIX>a2a_<key>`, the
agent's own `<MM_PROPS_PREFIX>…` props as they are; the agent card and anything
else are not sent.

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

Owner (RT-6.37). A NATS sender's owner is the one its presence subject names
(`<presence>.<owner>.<name>`), which the login service pins by the credential's one
presence permission, as it pins the sender (natsbus.owner_of_token); `agent_owner`
says it, and "" while there is none, so such a sender is foreign for every
session that splits peers from foreign agents (its TASK is a request, owners.py). Its
trusted list is empty: only its owner counts. Its presence is the NATS one, fresh by
the server's clock. Its crown flag (`a2a_crown`) is dropped: the crown over NATS comes
from the claims, later.

Trust. Nothing from NATS is from a human or an approval. The post carries the
sender's agent name as author, which is no trusted human's id (the trusted ids are
Mattermost's), no owner's and no mandate holder's; an author without a "-" in its
name is dropped. Text and props claim nothing: the server never reads them for
trust (gate.py, mandate.py, owners.py go by the author's id).

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
import os
import secrets
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from . import a2a, lease, natsbus, owners
from .events import ChatEvent, EventSource, EventStream, PostEvent, ReactionEvent
from .ids import _ID, NATS_POST
from .model import NotFound, Post, Reaction, Thread, UnreadChannel
from .natsbus import DM_SINCE, Event, NameMismatch, NatsBus, NatsBusError, NatsSettings

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

    def owner_of(self, agent: str) -> str: ...

    def presence_of(self, agent: str) -> str: ...

    def link_bot(self, bot_id: str) -> None: ...

    def bot_of(self, agent: str) -> str: ...

    @property
    def synced(self) -> bool: ...

    def is_member(self, channel: str, agent: str) -> bool: ...

    def presence_snapshot(self) -> dict[str, tuple[float, str]]: ...

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


def is_nats_user(user_id: str) -> bool:
    """The author id of a post that came over NATS: an agent name, which has a "-".
    No Mattermost user id (26 characters of [a-z0-9]) is one."""
    return "-" in user_id and natsbus.AGENT_RE.fullmatch(user_id) is not None and not _ID.fullmatch(user_id)


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


class NatsPort:
    """The chat port of a session with a home over NATS (module docstring). Members
    not defined here are the Mattermost adapter's (`__getattr__`)."""

    def __init__(self, inner: BusClient, settings: NatsSettings, homes: Iterable[str], *,
                 memory: int = DEFAULT_MEMORY, echo_wait: float = ECHO_WAIT_SEC,
                 bus_options: Mapping[str, Any] | None = None) -> None:
        self.inner = inner
        self._s = settings
        self._homes = tuple(sorted({h.strip().lower() for h in homes if h.strip()}))
        if not self._homes:
            raise ValueError("a NatsPort needs a home")
        self._memory = max(1, memory)
        self._echo_wait = echo_wait
        self._bus_options = dict(bus_options or {})
        self._bus: BusLike | None = None
        self._fatal: NatsPortError | None = None
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
        self._names: set[str] | None = None  # this session's trusted humans, read by prime (RT-6.38)
        self._ambiguous: set[str] = set()  # link tags that fit more than one fresh name (RT-6.37)

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
                await self.resolve()
                if owner is None:
                    owner = await _owner_of(login)
                bus = NatsBus(self._s, login, owner, self._on_event, on_fatal=self._on_fatal,
                              **self._bus_options)
                self._bus = bus
                try:
                    bus.link_bot(await self._bot_id())
                except ValueError:
                    # A bot id that is no Mattermost user id (a bad MM_BOT_USER_ID): the NATS
                    # home still starts, only without a link between bot and NATS name.
                    log.warning("nats: the bot id is not a Mattermost user id: starting without a link")
                await bus.start()
                lease.use_nats_link(partial(_tag_of, bus))
                await self._announce_membership(bus)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — final, or it may pass: again
                if _final(exc):
                    self.fail(exc)
                    raise
                self._bus = None
                log.warning("nats: the home %s did not start (%s), trying again in %.0f s",
                            ", ".join(self._homes), _describe(exc), delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, START_RETRY_MAX_SEC)

    async def _bot_id(self) -> str:
        """The Mattermost user id of the bot, as lease._user_id resolves it."""
        inner: Any = self.inner
        if inner.session:
            return str(inner.session.user_id)
        return os.environ.get("MM_BOT_USER_ID") or (await inner.get_me()).id

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
    async def resolve(self) -> dict[str, str]:
        """Channel name -> Mattermost id of the NATS homes, read once."""
        if self._ids is not None:
            return self._ids
        async with self._resolving:
            if self._ids is None:
                ids: dict[str, str] = {}
                for name in self._homes:
                    if name == self.inner.work_channel_name.lower():
                        ids[name] = await self.inner.work_channel_id()
                    else:
                        ids[name] = await self.inner.get_channel_id(name)
                self._ids = ids
        return self._ids

    async def nats_name(self, channel_id: str) -> str | None:
        """The name of the NATS home this channel id is, None for any other channel."""
        for name, cid in (await self.resolve()).items():
            if cid == channel_id:
                return name
        return None

    async def is_nats_channel(self, channel_id: str) -> bool:
        return bool(channel_id) and await self.nats_name(channel_id) is not None

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
            raise NatsPortError("only a NATS post id (n1:<channel>:<n>) has a NATS reaction to remove")
        self._home(got[0])
        await self._require().unreact(got[0], got[1], emoji)

    def _home(self, channel: str) -> None:
        if channel not in self._homes:
            raise NatsPortError(f"~{channel} is no NATS home of this session: nothing sent")

    # --- receiving -----------------------------------------------------------------------
    async def _on_event(self, ev: Event) -> None:
        if ev.where == DM_SINCE or ev.where not in self._homes:
            log.debug("nats: dropped a message outside the NATS homes (%s)", ev.where)
            return
        if not is_nats_user(ev.sender):
            log.warning("nats: dropped a message of %r: an agent name has a '-'", ev.sender[:40])
            return
        try:
            ids = await self.resolve()
        except Exception as exc:  # noqa: BLE001
            log.warning("nats: dropped a message of ~%s: its channel is not known (%s)",
                        ev.where, type(exc).__name__)
            return
        if ev.message.kind == "post":
            self._on_post(ev, ids[ev.where])
        else:
            self._on_reaction(ev, ids[ev.where])

    def _on_post(self, ev: Event, channel_id: str) -> None:
        m, ch = ev.message, ev.where
        root = ""
        if m.root is not None:
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
        self._tell(PostEvent(post=self.inner.with_links(post), channel_type=None, edited=False))

    def _on_reaction(self, ev: Event, channel_id: str) -> None:
        m, ch = ev.message, ev.where
        assert m.ref is not None and m.emoji is not None
        at = self._where.get(m.ref)
        if at is None:
            log.info("nats: a reaction to post %d in ~%s is ignored: the post is not known", m.ref, ch)
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
    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        nats = {i for i in user_ids if is_nats_user(i)}
        out = await self.inner.usernames(set(user_ids) - nats) if user_ids - nats else {}
        return out | {i: i for i in nats}

    async def username(self, user_id: str) -> str:
        return user_id if is_nats_user(user_id) else await self.inner.username(user_id)

    async def agents(self, user_ids: set[str]) -> set[str]:
        nats = {i for i in user_ids if is_nats_user(i)}
        return (await self.inner.agents(set(user_ids) - nats) if user_ids - nats else set()) | nats

    async def agent_owner(self, user_id: str) -> str | None:
        # A NATS sender's owner is the one its presence proves (RT-6.37), else "" (an
        # owner that is none), which is foreign for the peer split (owners.read); not
        # None, which is "not readable" (also foreign, ADR-016, but logged as an outage).
        if not is_nats_user(user_id):
            return await self.inner.agent_owner(user_id)
        return self._bus.owner_of(user_id) if self._bus is not None and self._fatal is None else ""

    async def bot_description(self, user_id: str) -> str | None:
        return None if is_nats_user(user_id) else await self.inner.bot_description(user_id)

    async def agent_attestation(self, user_id: str) -> str | None:
        return None if is_nats_user(user_id) else await self.inner.agent_attestation(user_id)

    async def attestation_record(self, bot_id: str) -> str | None:
        return None if is_nats_user(bot_id) else await self.inner.attestation_record(bot_id)

    async def presences(self, user_ids: list[str]) -> dict[str, str]:
        """Mattermost's for its accounts; for a NATS sender its NATS presence (RT-6.37):
        online or away while fresh by the server's clock, else offline."""
        nats = [i for i in user_ids if is_nats_user(i)]
        rest = [i for i in user_ids if not is_nats_user(i)]
        out = await self.inner.presences(rest) if rest else {}
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
        self._names, _ = await owners.names_in_doubt(self.inner)

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
        agents = {a: owners.Owner(owner=bus.owner_of(a), trusts=frozenset(), cut=False)
                  for a in dict.fromkeys([*order, me])}
        standing = lease.standing(order, me, self._names, agents).ids
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
                               self._nats._queue)
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
                 bound: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None) -> None:
        self._chat, self._nats, self._retry = chat, nats, retry_sec
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
