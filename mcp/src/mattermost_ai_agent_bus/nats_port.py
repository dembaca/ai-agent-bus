"""A home that runs over NATS (RT-6.36, ADR-014 points 3 and 16).

`NatsPort` wraps the Mattermost adapter (the BusClient) and a `natsbus.NatsBus`.
For the channels of a home named in MM_NATS_HOMES the agents' posts, threads and
reactions go over NATS; everything about people stays on Mattermost (their posts
and reactions arrive over the Mattermost WebSocket, trust, headers, pinned posts,
files, status, users are the adapter's, handed on unchanged). The agents' tools
do not change. Roster, presence and the crown stay on Mattermost (not here yet).

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

Memory. What arrives is kept per channel, at most `memory` posts (the oldest go);
`thread`, `channel_posts`, `list_recent`, `posts_after` and `reactions` read it,
`member_channels` shows its newest post to the unread count. What lies beyond is
read again after a restart: the bus sends `since`. A reply whose root is a post
of another channel is dropped; one whose root is not known is a top-level post; a
reaction to a post not known is ignored. Reactions are kept by (post, emoji,
sender); an unreaction removes the reaction of its own sender only.

Trust. Nothing from NATS is from a human or an approval. The post carries the
sender's agent name as author, which is no trusted human's id (the trusted ids are
Mattermost's), no owner's and no mandate holder's; an author without a "-" in its
name is dropped. Text and props claim nothing: the server never reads them for
trust (gate.py, mandate.py, owners.py go by the author's id).

A NATS home that cannot start (no login, a NameMismatch, a failed connect) is
reported in the log and every send raises: it never falls back to Mattermost.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from . import a2a, natsbus
from .events import ChatEvent, EventSource, EventStream, PostEvent, ReactionEvent
from .ids import _ID, NATS_POST
from .model import NotFound, Post, Reaction, Thread, UnreadChannel
from .natsbus import DM_SINCE, Event, NatsBus, NatsBusError, NatsSettings

if TYPE_CHECKING:
    from .compose import BusClient

log = logging.getLogger(__name__)

DEFAULT_MEMORY = 2000  # posts kept per channel
ECHO_WAIT_SEC = 10.0  # how long a send waits for its own copy from the stream
_NOTE = "a2a_"  # after the props prefix: the bus's own note, flat


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


def post_id(channel: str, seq: int) -> str:
    return f"n1:{channel}:{seq}"


def parse_post_id(value: str) -> tuple[str, int] | None:
    """(channel name, stream sequence) of a NATS post id, None for any other."""
    m = NATS_POST.fullmatch(value or "")
    return (m["channel"], int(m["seq"])) if m else None


def is_nats_user(user_id: str) -> bool:
    """The author id of a post that came over NATS: an agent name, which has a "-".
    No Mattermost user id (26 characters of [a-z0-9]) is one."""
    return "-" in user_id and natsbus.AGENT_RE.match(user_id) is not None and not _ID.fullmatch(user_id)


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
        self._echoes: list[tuple[str, str, int | None, asyncio.Future[Post]]] = []
        self._subs: list[asyncio.Queue[ChatEvent | NatsPortError | None]] = []

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
        the login's approval. A failure is logged, kept (every send then raises)
        and raised."""
        if self._bus is not None:
            return
        try:
            await self.resolve()
            if owner is None:
                owner = await _owner_of(login)
            bus = NatsBus(self._s, login, owner, self._on_event, on_fatal=self._on_fatal,
                          **self._bus_options)
            self._bus = bus
            await bus.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(exc)
            raise

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
        bus, self._bus = self._bus, None
        self._tell(None)
        if bus is not None:
            with contextlib.suppress(Exception):
                await bus.stop()

    def _require(self) -> BusLike:
        if self._fatal is not None:
            raise self._fatal
        if self._bus is None:
            raise NatsPortError(f"the NATS home {', '.join(self._homes)} is not up yet "
                                "(waiting for the login); nothing is sent over Mattermost instead")
        return self._bus

    def subscribe(self) -> asyncio.Queue[ChatEvent | NatsPortError | None]:
        q: asyncio.Queue[ChatEvent | NatsPortError | None] = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ChatEvent | NatsPortError | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(q)
        q.put_nowait(None)

    def _tell(self, item: ChatEvent | NatsPortError | None) -> None:
        for q in list(self._subs):
            q.put_nowait(item)

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
                raise NatsPortError("a post for people (an ask, files) cannot be a reply to a NATS post: "
                                    "nothing sent")
            return await self.inner.post(message, channel_id=channel_id, root_id=root_id,
                                         props=props, file_ids=file_ids)
        root = self._id_in(root_id, name, "a reply's root") if root_id else None
        bus = self._require()
        wire = wire_props(props, self._s.props_prefix)
        echo: asyncio.Future[Post] = asyncio.get_running_loop().create_future()
        entry = (name, message, root, echo)
        self._echoes.append(entry)
        try:
            await bus.post(name, message, root=root, props=wire)
            try:
                return await asyncio.wait_for(echo, self._echo_wait)
            except TimeoutError:
                raise NatsPortError("the post was sent over NATS, but its own copy did not come back: "
                                    "its id is not known") from None
        finally:
            with contextlib.suppress(ValueError):
                self._echoes.remove(entry)

    def post_sync(self, message: str, *, channel_id: str, root_id: str = "",
                  props: dict[str, Any] | None = None, timeout: float = 5.0) -> Post:
        if parse_post_id(root_id):
            raise NatsPortError("no synchronous post over NATS (the exit handler has no event loop): "
                                "nothing sent")
        # The exit handler's last words (a BYE) are for the people in the home: Mattermost.
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
        props = read_props(m.props, self._s.props_prefix)
        raw: dict[str, Any] = {"id": post_id(ch, ev.stream_seq), "channel_id": channel_id, "root_id": root,
               "user_id": self._uid(ev.sender), "create_at": int(ev.time * 1000),
               "message": m.text, "props": props}
        post = Post(
            id=raw["id"], channel_id=channel_id, root_id=root, user_id=raw["user_id"],
            create_at=raw["create_at"], message=m.text, kind=a2a.kind_of(raw),
            intent=a2a.intent_of(raw), to=a2a.ALL if a2a.to_all(raw) else "",
            pr=a2a.pr_of(raw), branch=a2a.branch_of(raw), crowned=a2a.crowned(raw),
            props=MappingProxyType(props), raw=MappingProxyType(raw))
        mem = self._posts.setdefault(ch, OrderedDict())
        mem[ev.stream_seq] = post
        self._where[ev.stream_seq] = ch
        while len(mem) > self._memory:
            old, _ = mem.popitem(last=False)
            self._where.pop(old, None)
            self._reacts.pop(old, None)
        for entry in self._echoes:
            if (entry[0], entry[1], entry[2]) == (ch, m.text, m.root) and not entry[3].done() \
                    and self._bus is not None and ev.sender == self._bus.name:
                entry[3].set_result(post)
                break
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
        self._reacts.setdefault(m.ref, {})[key] = ms
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
        replies = sum(1 for p in self._posts[ch].values() if p.root_id == post.id)
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

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        name = await self.nats_name(channel_id)
        if name is None:
            return await self.inner.channel_posts(channel_id, per_page=per_page, since_ms=since_ms)
        posts = self._channel_posts(name)
        if since_ms is not None:
            posts = [p for p in posts if p.create_at >= since_ms]
        posts.reverse()  # newest first
        return posts[:per_page] if per_page is not None else posts

    async def list_recent(self, *, channel_id: str | None = None, per_page: int = 20,
                          since_ms: int | None = None) -> list[Post]:
        cid = await self._home_or(channel_id)
        name = await self.nats_name(cid)
        if name is None:
            return await self.inner.list_recent(channel_id=channel_id, per_page=per_page,
                                                since_ms=since_ms)
        posts = self._channel_posts(name)
        if since_ms is not None:
            posts = [p for p in posts if p.create_at >= since_ms]
        else:
            posts = posts[-per_page:] if per_page > 0 else []
        return [self.inner.with_links(p) for p in posts]

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        cid = await self._home_or(channel_id)
        name = await self.nats_name(cid)
        if name is None:
            return await self.inner.posts_after(since_ms, channel_id=channel_id, max_pages=max_pages)
        return [p for p in self._channel_posts(name) if p.create_at > since_ms]

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
        """The adapter's, with a NATS home's newest remembered post counted as its
        last post: Mattermost does not know it, and the unread count and the
        inbox's backfill look at it."""
        rows = await self.inner.member_channels()
        ids = await self.resolve()
        newest = {ids[ch]: max((p.create_at for p in mem.values()), default=0)
                  for ch, mem in self._posts.items() if ch in ids}
        return [dataclasses.replace(r, last_post_at=max(r.last_post_at, newest[r.channel_id]))
                if newest.get(r.channel_id) else r for r in rows]

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
        return None if is_nats_user(user_id) else await self.inner.agent_owner(user_id)

    async def bot_description(self, user_id: str) -> str | None:
        return None if is_nats_user(user_id) else await self.inner.bot_description(user_id)

    async def agent_attestation(self, user_id: str) -> str | None:
        return None if is_nats_user(user_id) else await self.inner.agent_attestation(user_id)

    async def attestation_record(self, bot_id: str) -> str | None:
        return None if is_nats_user(bot_id) else await self.inner.attestation_record(bot_id)

    async def presences(self, user_ids: list[str]) -> dict[str, str]:
        return await self.inner.presences([i for i in user_ids if not is_nats_user(i)])

    async def is_member(self, channel_id: str, user_id: str) -> bool:
        if not is_nats_user(user_id):
            return await self.inner.is_member(channel_id, user_id)
        name = await self.nats_name(channel_id)
        return name is not None and any(p.user_id == user_id for p in self._posts.get(name, {}).values())


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
    (the inbox's backfill reads it from the port's memory). The bus reconnects
    itself, so a stream stays open through that; it ends on close() and raises
    when the homes are down. Typing over NATS is nothing."""

    def __init__(self, port: NatsPort) -> None:
        self._port = port
        self._queue: asyncio.Queue[ChatEvent | NatsPortError | None] | None = None

    @property
    def connected(self) -> bool:
        return self._queue is not None and self._port.bus_attached

    async def open(self) -> None:
        if not self._port.bus_attached:
            raise self._port.failure or NatsPortError("the NATS bus is not started yet")
        if self._queue is None:
            self._queue = self._port.subscribe()

    def events(self) -> EventStream:
        return _NatsStream(self)

    async def typing(self, channel_id: str, parent_id: str) -> None:
        return None

    async def close(self) -> None:
        q, self._queue = self._queue, None
        if q is not None:
            self._port.unsubscribe(q)


class _NatsStream:
    def __init__(self, source: NatsEvents) -> None:
        self._source = source
        self._queue = source._queue

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
        await self._source.close()


class MergedEvents:
    """Mattermost's events for what stays there (people, every other channel) and
    the NATS events of the homes, in one source. It is connected when both are;
    open opens both; a stream ends (or raises) when either does, and closes the
    other, so the next open connects both afresh."""

    def __init__(self, chat: EventSource, nats: NatsEvents, port: NatsPort) -> None:
        self._chat, self._nats, self._port = chat, nats, port

    @property
    def connected(self) -> bool:
        return self._chat.connected and self._nats.connected

    async def open(self) -> None:
        await self._chat.open()
        try:
            await self._nats.open()
        except BaseException:
            await self._chat.close()
            raise

    def events(self) -> EventStream:
        return _MergedStream(self._chat.events(), self._nats.events())

    async def typing(self, channel_id: str, parent_id: str) -> None:
        if await self._port.is_nats_channel(channel_id):
            return await self._nats.typing(channel_id, parent_id)
        await self._chat.typing(channel_id, parent_id)

    async def close(self) -> None:
        try:
            await self._nats.close()
        finally:
            await self._chat.close()


class _MergedStream:
    def __init__(self, *streams: EventStream) -> None:
        self._streams = streams
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._ended = False

    def __aiter__(self) -> _MergedStream:
        return self

    async def _pump(self, stream: EventStream) -> None:
        try:
            async for event in stream:
                await self._queue.put(("event", event))
            await self._queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — handed to the reader
            await self._queue.put(("error", exc))

    async def __anext__(self) -> ChatEvent:
        if self._ended:
            raise StopAsyncIteration
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._pump(s)) for s in self._streams]
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
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for s in self._streams:
            with contextlib.suppress(Exception):
                await s.aclose()

