"""The smallest server that answers the bus's real contract.

Every HTTP answer here was measured, not invented: `scripts/probe_bus_calls.py`
runs the real `MattermostAdapter` against a recording stub and prints the 16
endpoints it calls. `KNOWN` is that table, so this stub answers what the bus
asked for rather than what a reader of the source guessed it would ask.

The three rows beyond that probe were found by running the real server
(`bus.BusProcess`): the heartbeat's status write, the roster read, and the exit
path's unregister. They are marked in the table; a change there is still a
deliberate claim about the bus, which is why they are listed rather than guessed.

An unknown call is a 404 **and** a line in `.unknown`: a run asserts "0 unknown"
instead of passing because nothing checked. Every call lands in `.calls`.
A frame the socket did not understand lands there too, marked `WS`, for the
same reason: a first frame that is not the challenge, text that is not JSON or
an action nobody answers used to vanish, so a bus that changed what it sends
would still have read as a clean run.

Three answers are computed from what the stub holds rather than fixed in the
table: a channel's posts, the bot's channels and its memberships. A fixed empty
post list made the bus's catch-up read after a reconnect find nothing, so a
`cut` run measured the stub's silence and not the bus's recovery.

The WebSocket half answers the `authentication_challenge` with `hello`, then
writes the frames the caller scripted. Mattermost sends a `posted` frame with
the post as JSON text and also as an object (the probes saw both); `posted_frame`
builds either, so a test never hand-rolls the JSON. `push` writes one now, into
whatever socket the bus opened: it connects on its first wait, not at start.

Two bots are told apart by the token each presents (`identify`), and a created
post is kept (`post_of`) so the frame a run pushes is the post the bus asked for.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qs

from .gateway import Gateway, WsConn

M = "m" * 26  # the ids the probe's answers use, so a caller compares like with like
TEAM = "t" * 26
CHANNEL = "c" * 26
POST = "p" * 26
USER = "u" * 26

# How long one pushed frame may take on one socket before that socket is
# declared stuck (review point 6: under `halfopen` a write never drains).
PUSH_SEC = 5.0

# From the measured probe (probe_bus_calls.py): (method, path) regex -> body.
# Copied rather than imported because that file is a script with side effects,
# and because a change here must be a deliberate claim about the bus.
KNOWN: "OrderedDict[str, object]" = OrderedDict([
    (r"GET /api/v4/users/me$", {"id": M, "username": "agent-probe", "create_at": 1}),
    (r"GET /api/v4/teams/name/[^/]+$", {"id": TEAM}),
    (r"GET /api/v4/teams/[^/]+/channels/name/[^/]+$", {"id": CHANNEL}),
    (r"GET /api/v4/channels/[^/]+$", {"id": CHANNEL, "name": "agents", "type": "O"}),
    # `posts`, the bot's channels and its memberships are computed from what
    # this stub holds (`_channel_posts`, `_member_channels`,
    # `_team_channel_members`); their *shape* is the measured claim, their
    # content grows with the run, so they are not rows here.
    (r"GET /api/v4/channels/[^/]+/pinned$", {"order": [], "posts": {}}),
    (r"POST /api/v4/posts$", {"id": POST, "user_id": M, "channel_id": CHANNEL,
                              "message": "hi", "create_at": 2}),
    (r"POST /api/v4/channels/members/me/view$", {}),
    (r"POST /api/v4/channels/[^/]+/members$", {}),
    (r"GET /api/v4/users/me/teams/[^/]+/channels$", []),
    (r"GET /api/v4/posts/[^/]+/thread$", {"order": [], "posts": {}}),
    (r"GET /api/v4/posts/[^/]+/reactions$", []),
    (r"GET /api/v4/channels/[^/]+/members/[^/]+$", {"channel_id": CHANNEL, "user_id": M}),
    (r"POST /api/v4/users/ids$", []),
    (r"POST /api/v4/users/usernames$", []),
    (r"POST /api/v4/users/status/ids$", []),
    (r"PUT /api/v4/users/me/status/custom$", {}),
    (r"POST /api/v4/reactions$", {}),
    (r"GET /api/v4/posts/[^/]+$", {"id": POST, "channel_id": CHANNEL, "message": "hi",
                                  "user_id": M, "create_at": 2}),
    # Measured by driving the real server (`bus.bus_run`): the heartbeat writes
    # a status and the roster reads the channel's accounts. Both used to be
    # gaps; a 404 here is not a delivery fault, and reporting it as one buried
    # the numbers the run exists for.
    (r"PUT /api/v4/users/[^/]+/status$", {}),
    (r"GET /api/v4/users$", []),
    # The server's exit path unregisters the bot it was handed (client.
    # shutdown_sync): a bot does go offline when the server stops, and a run
    # that treated this as unknown would report four gaps per run.
    (r"DELETE /register/v1/agents/[^/]+$", {}),
])

# The three answers this stub computes instead of holding fixed (review point
# 3): the paths are Mattermost's, kept here so the dispatch reads like the table.
CHANNEL_POSTS = re.compile(r"^/api/v4/channels/[^/]+/posts$")
TEAM_MEMBERS = re.compile(r"^/api/v4/users/me/teams/[^/]+/channels/members$")


def _query_int(query: dict[str, list[str]], name: str) -> int:
    """One integer query parameter, 0 when it is absent or not a number."""
    values = query.get(name) or []
    try:
        return int(values[0]) if values else 0
    except ValueError:
        return 0


# The actions the bus sends on its socket and this stub knows what to do with:
# the challenge it answers, and the typing indicator it lets pass. Anything else
# is recorded as unknown rather than dropped (review point 7).
WS_ACTIONS = frozenset(("authentication_challenge", "user_typing"))


def posted_frame(post: dict[str, Any], *, as_object: bool = False,
                 channel_type: str = "O") -> dict[str, Any]:
    """A `posted` event; Mattermost sends `data.post` as text or as an object."""
    return {"event": "posted", "broadcast": {"channel_id": post.get("channel_id", CHANNEL)},
            "data": {"channel_type": channel_type,
                     "post": dict(post) if as_object else json.dumps(post)}}


def post_edited_frame(post: dict[str, Any], *, as_object: bool = False) -> dict[str, Any]:
    return {"event": "post_edited",
            "data": {"post": dict(post) if as_object else json.dumps(post)}}


def reaction_added_frame(reaction: dict[str, Any], channel_id: str = CHANNEL) -> dict[str, Any]:
    return {"event": "reaction_added", "broadcast": {"channel_id": channel_id},
            "data": {"reaction": json.dumps(reaction)}}


def user_added_frame(user_id: str = USER) -> dict[str, Any]:
    return {"event": "user_added", "data": {"user_id": user_id}}


def user_removed_frame(user_id: str = USER) -> dict[str, Any]:
    return {"event": "user_removed", "data": {"user_id": user_id}}


class MattermostStub:
    """HTTP answers from the measured table, and a scripted event socket."""

    def __init__(self, frames: list[dict[str, Any]] | None = None) -> None:
        self.frames: list[dict[str, Any]] = list(frames or [])
        self.calls: list[tuple[str, str]] = []
        self.unknown: list[tuple[str, str]] = []
        self.received: list[dict[str, Any]] = []
        self.authenticated = False
        #: why a socket could not be written to (review point 6), empty while
        #: every push was taken; a run reports it instead of a silent loss
        self.closed = ""
        #: the upgraded sockets, oldest first: the bus opens its inbox on the
        #: first wait, not at start, so a run pushes into whatever is open
        self.conns: list[WsConn] = []
        #: token -> the bot it authenticates, when a caller named one
        self.bots: dict[str, dict[str, Any]] = {}
        #: post id -> the post the caller created, for `posted_frame`
        self.posts: dict[str, dict[str, Any]] = {}
        self._patterns = [(re.compile(pattern), body) for pattern, body in KNOWN.items()]
        self._gateway = Gateway(self._on_http, self._on_ws)
        #: set when the first socket is open; a run waits on it instead of
        #: polling `conns` every 10 ms (review point at bus.py:523)
        self._connected = asyncio.Event()
        #: the wall-clock base the created posts count from. Mattermost stamps a
        #: post with the time it was made; a fixed date made every catch-up read
        #: look older than the bus's own watermark floor (24 h back), so the
        #: backfill never saw a post a `cut` had eaten.
        self._clock = int(time.time() * 1000)

    @property
    def url(self) -> str:
        return self._gateway.url

    def identify(self, token: str, user_id: str, username: str) -> None:
        """Name the bot a token authenticates: two servers pointed at one stub
        are told apart by the bearer each sends, as Mattermost tells two bots
        apart. Unnamed tokens get the probe's bot (`M`, agent-probe)."""
        self.bots[token] = {"id": user_id, "username": username, "create_at": 1}

    def post_of(self, post_id: str) -> dict[str, Any]:
        """The post the caller created, as Mattermost's `posted` frame carries
        it. A run never invents a post: an id this stub did not create is a
        caller's bug, and raising says so instead of handing back a blank post
        the run would then measure against."""
        try:
            return dict(self.posts[post_id])
        except KeyError:
            raise KeyError(f"the stub created no post {post_id!r}") from None

    def _bot(self, headers: dict[str, str]) -> dict[str, Any]:
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        return self.bots.get(token) or {"id": M, "username": "agent-probe", "create_at": 1}

    async def push(self, frame: dict[str, Any] | str) -> int:
        """Send one frame now on every open socket, the count it reached.

        Zero means the bus has not opened its inbox yet — it does so on its
        first wait_for_events — and the frame reached nobody. That is visible
        here rather than a silent success for a run that wants it delivered.

        Bounded (review point 6): under `halfopen` the proxy has stopped
        reading, so `send_text` waits for a transport that never drains — the
        review measured a push blocked at frame 2283, with only the job's own
        timeout left. A socket that cannot take the frame in PUSH_SEC is closed
        and named in `.closed`, so the run reports a stuck line instead of
        hanging until something outside kills it.
        """
        text = frame if isinstance(frame, str) else json.dumps(frame)
        sent = 0
        for conn in list(self.conns):
            try:
                await asyncio.wait_for(conn.send_text(text), PUSH_SEC)
            except asyncio.TimeoutError:
                self.closed = f"a socket did not take a frame within {PUSH_SEC} s"
                conn.transport.abort()
                continue
            sent += 1
        return sent

    async def __aenter__(self) -> MattermostStub:
        await self._gateway.start()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self._gateway.stop()

    async def _on_http(self, method: str, target: str, headers: dict[str, str],
                       body: bytes) -> tuple[int, dict[str, str], bytes]:
        path = target.split("?", 1)[0]
        self.calls.append((method, path))
        if method == "POST" and path == "/api/v4/posts":
            return self._create_post(headers, body)
        if method == "GET" and CHANNEL_POSTS.fullmatch(path):
            return self._channel_posts(target)
        if method == "GET" and path == "/api/v4/users/me/channels":
            return self._member_channels()
        if method == "GET" and TEAM_MEMBERS.fullmatch(path):
            return self._team_channel_members()
        key = f"{method} {path}"
        for pattern, answer in self._patterns:
            if pattern.fullmatch(key):
                if pattern.pattern == r"GET /api/v4/users/me$":
                    answer = self._bot(headers)
                return 200, {"content-type": "application/json"}, json.dumps(answer).encode()
        self.unknown.append((method, path))
        return 404, {"content-type": "application/json"}, b'{"message":"stub: unknown call"}'

    def _channel_posts(self, target: str) -> tuple[int, dict[str, str], bytes]:
        """A channel's posts as this stub holds them, newest first.

        Mattermost's contract: `since` keeps posts created at or after that
        millisecond, `per_page` caps `order` and `page` skips pages of it. A
        blank answer here (the old fixed one) made the bus's catch-up read
        after a reconnect find nothing, so a `cut` measured the stub.
        """
        query = parse_qs(target.partition("?")[2])
        since = _query_int(query, "since")
        per_page = _query_int(query, "per_page") or len(self.posts) or 1
        page = _query_int(query, "page") or 0
        held = sorted(self.posts.values(), key=lambda p: (p["create_at"], p["id"]),
                      reverse=True)
        kept = [p for p in held if p["create_at"] >= since]
        window = kept[page * per_page:(page + 1) * per_page]
        return 200, {"content-type": "application/json"}, json.dumps({
            "order": [p["id"] for p in window],
            "posts": {p["id"]: p for p in window},
        }).encode()

    def _member_channels(self) -> tuple[int, dict[str, str], bytes]:
        """The bot's channels, each with when its newest post came.

        `last_post_at` is what the bus's backfill tests before reading a
        channel (unread.after): a zero here meant the newest post was never
        looked for, so no catch-up read happened at all.
        """
        mine = {p["channel_id"] for p in self.posts.values()}
        channels = [self._channel_row(CHANNEL)] + [
            self._channel_row(cid) for cid in sorted(mine - {CHANNEL})]
        return 200, {"content-type": "application/json"}, json.dumps(channels).encode()

    def _team_channel_members(self) -> tuple[int, dict[str, str], bytes]:
        """The bot's memberships in the team's channels, one per channel."""
        return 200, {"content-type": "application/json"}, json.dumps(
            [{"channel_id": CHANNEL, "user_id": M}]).encode()

    def _channel_row(self, channel_id: str) -> dict[str, Any]:
        newest = max((p["create_at"] for p in self.posts.values()
                      if p["channel_id"] == channel_id), default=0)
        row = {"id": channel_id, "type": "O", "last_post_at": newest}
        if channel_id == CHANNEL:
            row |= {"name": "agents", "total_msg_count": len(self.posts)}
        return row

    def _create_post(self, headers: dict[str, str],
                     body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Mattermost's own answer to a create: the post, with the author the
        bearer names. Kept, so a run reads back exactly what it sent.

        `create_at` counts from the stub's own clock, not a fixed date: the bus
        never looks back further than a day, so a 2023 timestamp made every
        catch-up read skip the post it was meant to recover.
        """
        try:
            asked = json.loads(body or b"{}")
        except json.JSONDecodeError:
            asked = {}
        bot = self._bot(headers)
        seq = len(self.posts) + 1
        post = {"id": f"p{seq:025d}", "user_id": bot["id"], "channel_id": CHANNEL,
                "root_id": str(asked.get("root_id") or ""),
                "message": str(asked.get("message") or ""),
                "props": asked.get("props") or {},
                "create_at": self._clock + seq}
        self.posts[post["id"]] = post
        return 200, {"content-type": "application/json"}, json.dumps(post).encode()

    async def wait_for_socket(self, timeout: float) -> bool:
        """True once a socket is open, False after `timeout`.

        The bus opens its inbox on its first `wait_for_events`, not at start, so
        a caller that pushes without waiting sends into nothing. This is the
        event that replaces the 10 ms poll (review point at bus.py:523); the
        event is cleared when the last socket closes, so waking and finding
        none again keeps waiting rather than reporting a socket that is gone.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if self.conns:
                return True
            left = deadline - loop.time()
            if left <= 0:
                return False
            try:
                await asyncio.wait_for(self._connected.wait(), left)
            except asyncio.TimeoutError:
                return False

    async def _on_ws(self, conn: WsConn, _headers: dict[str, str]) -> None:
        self.conns.append(conn)
        self._connected.set()
        try:
            await self._serve_ws(conn)
        finally:
            if conn in self.conns:
                self.conns.remove(conn)
            if not self.conns:
                self._connected.clear()

    async def _serve_ws(self, conn: WsConn) -> None:
        first = await conn.recv()
        if first is None:
            self.unknown.append(("WS", "the socket closed before its first frame"))
        elif first[0] != 0x1:
            # Not text: nothing the stub can read, and the handshake it needs
            # never came (review point 7).
            self.unknown.append(("WS", f"first frame was not text (opcode {first[0]})"))
        else:
            try:
                message = json.loads(first[1])
            except json.JSONDecodeError:
                self.unknown.append(("WS", f"first frame was not JSON: {first[1][:60]!r}"))
                message = {}
            self.received.append(message)
            if message.get("action") == "authentication_challenge":
                self.authenticated = True
                await conn.send_text(json.dumps({"event": "hello"}))
            elif message.get("action") is not None:
                # A socket that opens with an action this stub does not answer
                # is a claim about the bus that has changed: say so (review
                # point 7) rather than let the handshake time out unseen.
                self.unknown.append(("WS", f"action {message['action']!r} before hello"))
        for frame in list(self.frames):
            await conn.send_text(frame if isinstance(frame, str) else json.dumps(frame))
        async for raw in conn:
            if not isinstance(raw, str):
                self.unknown.append(("WS", f"a binary frame the stub cannot read ({len(raw)} B)"))
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self.unknown.append(("WS", f"text that is not JSON: {raw[:60]!r}"))
                continue
            self.received.append(message)
            action = message.get("action")
            # The two actions the bus sends (probe_bus_frames): answered or
            # let pass. Everything else is a gap in this stand-in, not a frame
            # to drop silently.
            if action is not None and action not in WS_ACTIONS:
                self.unknown.append(("WS", f"action {action!r}"))
