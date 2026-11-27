"""Who may wake an agent (RT-6.16 in docs/arc42/06-runtime-view.md).

Every post's author is one of four classes, by the account Mattermost
authenticates, never by what the post says: trusted (a trusted human, RT-6.8),
agent (a peer: a bus agent whose owner and trusted humans are all mine,
ADR-001), foreign (any other bus agent, owners.py) or other (people, bots that
are no bus agents, and every webhook post, whatever account it names). While
the owner split is off, every bus agent is an agent.

With trusted humans configured, a post by `other` that would wake a worker is
gated: it reaches the agent only as data, and the worker's MCP server answers
in the thread itself, once per thread, without the model. The coordinator is
never gated. What the gate did lives next to asks-<uid>.env, in gate-<uid>.env:

    REPLIED=<root_id> <epoch>   a thread this bot answered "Not woken" in
    GATED=<post_id> <epoch>     a gated post: the listener does not wake for it
    GATED=<ask>:<user>:<emoji> <epoch>   a gated reaction to an open ask, likewise
    DEPARTED=<root_id>:<name> <epoch>  a thread told that agent is gone (RT-6.21)

Entries older than MAX_AGE_SEC are dropped.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import a2a, channel, lease, owners, session_file, tasks
from .model import Mark, Post

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .port import ChatPort

TRUSTED, AGENT, FOREIGN, OTHER = "trusted", "agent", "foreign", "other"
AGENTS = frozenset({AGENT, FOREIGN})  # bus agents, peer or foreign
MAX_AGE_SEC = 7 * 86400
# The coordinator's name is read with the roster, several requests: at most this often.
COORD_TTL_SEC = 60.0

_coord: tuple[float, str] | None = None


def author_class(post: Post, trusted: Mapping[str, str], agents: set[str],
                 foreign: Collection[str] = ()) -> str:
    if post.from_webhook:
        return OTHER
    uid = post.user_id
    if uid in trusted:
        return TRUSTED
    if uid not in agents:
        return OTHER
    return FOREIGN if uid in foreign else AGENT


TRUSTED_VARS = owners.TRUSTED_VARS


async def active(client: ChatPort) -> bool:
    """The gate and the owner split are on (RT-6.16 step 2): trusted humans
    are configured, whether or not a lookup resolved them now. In doubt on
    (ADR-016): a failed lookup leaves trusted_humans() empty, never the gate
    off. With trust closed (plan B piece 4 without a fresh own attestation,
    RT-6.41 step 6) on, also without any of the host's variables."""
    if owners.configured() or await client.trust_closed():
        return True
    return bool(await client.trusted_humans())


async def author_classes(client: ChatPort, posts: Sequence[Post]) -> list[str]:
    """The class of each post's author; raises when the accounts cannot be
    read. Whether an agent is foreign never raises: in doubt, it is foreign
    (ADR-016, after owners.OWNER_GRACE_SEC)."""
    trusted = await client.trusted_humans()
    agents = await client.agents({p.user_id for p in posts} - {""})
    foreign = await owners.foreign(client, agents)
    return [author_class(p, trusted, agents, foreign) for p in posts]


@dataclass
class _Root:
    """RT-6.6 step 5: what a thread root says about whose thread it is: its
    author ('' for a webhook post), the agents its text names, and who has
    posted in the thread as last read (it only grows: a post stays)."""

    author: str
    by_agent: bool  # read only for a root that names an agent
    named: tuple[str, ...]
    posters: set[str]
    read_at: float = 0.0  # time.monotonic() of the read


# Each root read once; the thread again, at most every ROOT_TTL_SEC, only while
# an agent its root names has not posted there as last read; posts the hook
# API reads add their authors at once (saw). The oldest go first beyond ROOTS_KEPT. Whether an agent is a reachable
# peer of the home is read from the roster each time, at most every
# COORD_TTL_SEC (_reach), never kept with the root.
_roots: dict[str, _Root] = {}
ROOTS_KEPT = 2000
ROOT_TTL_SEC = 60.0
_reach: tuple[float, dict[str, str]] | None = None


# A thread that could not be read, root → time.monotonic() of the failure: not
# read again for ROOT_TTL_SEC (the first read and a re-read alike).
_failed: dict[str, float] = {}
# The thread reads one request may still make (thread_reads); None: no limit.
_budget: ContextVar[list[int] | None] = ContextVar("thread_read_budget", default=None)


@contextmanager
def thread_reads(limit: int) -> Iterator[None]:
    """At most `limit` thread reads inside (RT-6.6 step 5: one /v1/posts
    request). Beyond it a read is skipped as unsure: the caller does as
    without a record (the crown wakes, a reply stays unmarked), never worse."""
    token = _budget.set([limit])
    try:
        yield
    finally:
        _budget.reset(token)


def saw(post: Post) -> None:
    """A post the hook API read or this agent made (post_message,
    reply_in_thread): its author has posted in its thread."""
    facts = _roots.get(post.root_id) if post.root_id else None
    if facts is not None and post.user_id and not post.from_webhook:
        facts.posters.add(post.user_id)


async def thread_facts(client: ChatPort, root_id: str,
                       want: Collection[str] = ()) -> _Root | None:
    """The record of the thread `root_id` (_roots, read once). Read again
    when none of `want` has posted there as last read and that read is older
    than ROOT_TTL_SEC, so a new post of one of them shows within that time.
    A failed read is not tried again for ROOT_TTL_SEC either (_failed).
    None: never read and unreadable now (a failed re-read keeps the record)."""
    facts = _roots.get(root_id)
    if facts is None or (want and not set(want) & facts.posters
                         and time.monotonic() - facts.read_at > ROOT_TTL_SEC):
        failed = _failed.get(root_id)
        if failed is not None and time.monotonic() - failed <= ROOT_TTL_SEC:
            return facts
        facts = await _read_root(client, root_id) or facts
    return facts


async def _reachable_here(client: ChatPort) -> dict[str, str]:
    """The home's reachable peers, id → username in lower case (lease.roster,
    the same rows RT-6.21 reads for departed and not here), without the
    ambassadors of other homes and without foreign agents (owners.foreign, in
    doubt foreign: ADR-001, the crown defers only to a peer: it cannot know a
    foreign agent's gate); empty when the roster cannot be read, and so for
    COORD_TTL_SEC."""
    global _reach
    now = time.monotonic()
    if _reach is None or now - _reach[0] > COORD_TTL_SEC:
        rows = {r.user_id: r.username.lower() for r in await lease.roster(client)
                if r.user_id and not r.crown_of}
        foreign = set(await owners.foreign(client, set(rows)))
        _reach = (now, {i: n for i, n in rows.items() if i not in foreign})
    return _reach[1]


async def _read_root(client: ChatPort, root_id: str) -> _Root | None:
    """The thread `root_id` read (one request; one more for the author's
    account when the root names an agent), kept in _roots; None: unreadable
    (kept in _failed) or over the request's budget (thread_reads)."""
    budget = _budget.get()
    if budget is not None:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
    old = _roots.get(root_id)
    try:
        thread = await client.thread(root_id)
        root = next((p for p in thread.posts if p.id == root_id), None)
        if root is None:
            raise LookupError("no root in the thread")
        author = "" if root.from_webhook else root.user_id
        named = tuple(tasks.mentions(root.message))
        by_agent = old.by_agent if old else bool(
            named and author and await client.agents({author}))
    except Exception as exc:  # noqa: BLE001 — unsure
        log.info("thread root %s not read: %s", root_id, type(exc).__name__)
        _failed.pop(root_id, None)
        _failed[root_id] = time.monotonic()
        while len(_failed) > ROOTS_KEPT:
            del _failed[next(iter(_failed))]
        return None
    _failed.pop(root_id, None)
    facts = _Root(author=author, by_agent=by_agent, named=named,
                  posters={p.user_id for p in thread.posts if p.user_id and not p.from_webhook},
                  read_at=time.monotonic())
    _roots.pop(root_id, None)
    _roots[root_id] = facts
    while len(_roots) > ROOTS_KEPT:
        del _roots[next(iter(_roots))]
    return facts


async def other_agents_thread(client: ChatPort, root_id: str, uid: str) -> bool | None:
    """Whether the thread `root_id` is another agent's (RT-6.6 step 5), with
    an agent B, not `uid`, that the roster shows as a reachable peer of this
    home now: B posted the root, or a human (or webhook) posted it naming B
    and B has posted in the thread since, and the root does not name this
    agent. Not: a root by a human that names no agent, or only agents that
    have not answered there yet, are foreign, departed, not here or not
    reachable; a root by this agent (the crown wakes as before). Names are
    the roster's usernames now: a root naming an agent by a name it no
    longer has (renamed) names nobody here, as Mattermost notifies nobody.
    None: the thread cannot be read (unsure: the caller wakes as before)."""
    facts = await thread_facts(client, root_id)
    if facts is None:
        return None
    if facts.author == uid:
        return False
    peers = await _reachable_here(client)
    if facts.author in peers:
        return True
    if facts.by_agent:
        return False  # an agent's root, not a reachable peer's: the crown's
    named = {i for i, n in peers.items() if n in facts.named and i != uid}
    if not named:
        return False
    try:
        if (await client.get_me()).username.lower() in facts.named:
            return False  # the root names this agent too: its thread as well
    except Exception as exc:  # noqa: BLE001 — unsure
        log.info("own name not read: %s", type(exc).__name__)
        return None
    # B may have answered since the last read: read again after ROOT_TTL_SEC.
    facts = await thread_facts(client, root_id, want=named) or facts
    return bool(named & facts.posters)


async def in_thread(client: ChatPort, root_id: str, uid: str) -> tuple[bool, bool] | None:
    """RT-6.2 step 4, RT-6.6 step 5: (uid has posted in the thread, uid
    posted its root) from the whole thread (thread_facts, read once), not
    from the last posts of the channel. A later post of uid's is added when
    the hook API reads it (saw: its listener reads its last MAX_POSTS posts at
    each start), so this reads no thread again. None: unreadable."""
    facts = await thread_facts(client, root_id)
    if facts is None:
        return None
    return uid in facts.posters or facts.author == uid, facts.author == uid


def wake_reason(kind: str, post: Post, in_my_thread: bool) -> str:
    """Why the post would wake a worker (RT-6.2 step 4): mention, dm, thread or
    broadcast (@channel in the text, or props to="all"); '' for a post that
    only goes into the digest."""
    if kind in ("mention", "dm"):
        return kind
    if in_my_thread:
        return "thread"
    return "broadcast" if post.to == a2a.ALL or channel.broadcast(post.message) else ""


async def author_name(client: ChatPort, post: Post) -> str:
    if post.from_webhook:
        return "a webhook"
    uid = post.user_id
    return (await client.usernames({uid})).get(uid) or uid


async def coordinator_name(client: ChatPort) -> str:
    """The agent wearing a live 👑, '' when none. Only a crown counts: the
    roster's agent next in line for the crown coordinates nothing yet. An unreadable
    roster is not remembered."""
    global _coord
    now = time.monotonic()
    if _coord is None or now - _coord[0] > COORD_TTL_SEC:
        rows = await lease.roster(client)
        if not rows:
            return ""
        _coord = (now, next((r.username for r in rows if r.crown), ""))
    return _coord[1]


async def coordinator_in(client: ChatPort, guest: str = "") -> str:
    """The coordinator a gated post's author is pointed to: the home's, or in
    the guest channel `guest` (an id) that channel's own (RT-6.22)."""
    return await lease.coordinator_of(client, guest) if guest else await coordinator_name(client)


async def names_coordinator(client: ChatPort, post: Post, guest: str = "") -> bool:
    """RT-6.16 step 3: the post already @-mentions the agent wearing the 👑: it was told, an
    answer saying that it will look would only repeat it. guest: as coordinator_in."""
    name = await coordinator_in(client, guest)
    return bool(name) and re.search(
        rf"(?<![\w.-])@{re.escape(name)}(?![\w-]|\.\w)", post.message, re.I) is not None


async def mentioned_agent_trusts(client: ChatPort, post: Post, author: str, me: str) -> bool:
    """RT-6.16 step 3: another agent the post @-mentions trusts its author (its
    description names them as owner or trusted): that one wakes and answers,
    a "Not woken" here would only add noise. Reads the roster, never fails."""
    prefix = re.escape(session_file.agent_prefix())
    named = {n.lower() for n in re.findall(rf"@({prefix}[\w.-]*\w)", post.message)}
    if not named or post.from_webhook:
        return False
    try:
        rows = await lease.roster(client)
    except Exception as exc:  # noqa: BLE001 — unsure: the answer goes out
        log.info("mentioned agents not read: %s", exc)
        return False
    for r in rows:
        if r.user_id and r.user_id != me and r.username.lower() in named:
            agent = await owners.read(client, r.user_id)
            if agent is not None and author.lower() in agent.display_circle():
                return True
    return False


async def reply_text(client: ChatPort, author: str, *, dm: bool, guest: str = "") -> str:
    """The answer to a gated post. Names stay plain: only ask_human notifies a
    person, and the coordinator sees every channel post anyway (one naming an
    agent with its digest, RT-6.6 step 5). guest:
    the guest channel (an id) the post is in, whose own coordinator it names,
    the crown holder of the channel whose home it is (RT-6.22)."""
    coord = await coordinator_in(client, guest)
    head = f"Not woken: {author} is not on my trusted list"
    if coord and dm:
        # The coordinator cannot see a DM.
        return f"{head}. Ask in ~{client.work_channel_name}, where {coord} ({a2a.symbol(Mark.CROWN)}) coordinates."
    if coord:
        return f"{head}. {coord} ({a2a.symbol(Mark.CROWN)}) will look at it."
    where = f"~{(await client.get_channel(guest)).name}" if guest else "right now"
    trusted = await client.trusted_humans()
    names = list(dict.fromkeys(n for n in trusted.values() if n)) or sorted(
        (await client.usernames(set(trusted))).values())
    return f"{head}, and no agent coordinates {where}. Ask {', '.join(names)}."


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"gate-{uid}.env"


def _read(uid: str) -> dict[str, dict[str, float]]:
    # REPLIED: a thread the gate answered; GATED: a post held back; DEPARTED:
    # `<root>:<agent>`, a thread told about it (RT-6.21 step 4); ABSENT: a post
    # naming an absent agent that this server answers, so it wakes (step 5).
    state: dict[str, dict[str, float]] = {"REPLIED": {}, "GATED": {}, "DEPARTED": {}, "ABSENT": {}}
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return state
    now = time.time()
    for line in lines:
        key, _, val = line.partition("=")
        parts = val.split()
        if key not in state or not parts:
            continue
        try:
            when = float(parts[1])
        except (IndexError, ValueError):
            when = now
        if now - when < MAX_AGE_SEC:
            state[key][parts[0]] = when
    return state


def _write(uid: str, state: dict[str, dict[str, float]]) -> None:
    lines = [f"{key}={pid} {int(at)}" for key, ids in state.items() for pid, at in ids.items()]
    path = _path(uid)
    if not lines:
        path.unlink(missing_ok=True)
        return
    session_file.write_private(path, "\n".join(lines) + "\n")


def mark_gated(uid: str, post_id: str) -> None:
    state = _read(uid)
    state["GATED"][post_id] = time.time()
    _write(uid, state)


def gated(uid: str) -> set[str]:
    return set(_read(uid)["GATED"])


def mark_absent(uid: str, post_id: str) -> None:
    """RT-6.21: the post names an agent gone or not here, and this server answers it."""
    state = _read(uid)
    state["ABSENT"][post_id] = time.time()
    _write(uid, state)


def absent(uid: str) -> set[str]:
    return set(_read(uid)["ABSENT"])


def first_reply(uid: str, root_id: str, key: str = "REPLIED") -> bool:
    """Claim the thread for the one answer: False when it had one already."""
    state = _read(uid)
    if root_id in state[key]:
        return False
    state[key][root_id] = time.time()
    _write(uid, state)
    return True


def forget_reply(uid: str, root_id: str, key: str = "REPLIED") -> None:
    """The answer was not posted: the next gated post there may try again."""
    state = _read(uid)
    if state[key].pop(root_id, None) is not None:
        _write(uid, state)
