"""The composition root: the one place that builds the chat adapter (arc42 §5.2).

The entry points (server.py, hosted.py) hold a BusClient, the chat port and
the registration port on one object, and get it from client_from_env; none of
them imports client.py (test_adapter_boundary); the WebSocket inbox gets its
live events from event_source. A second chat adapter, and its event source,
is chosen here and nowhere else.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import a2a, identity, lease, ownpost, unanswered
from .config import env
from .port import ChatPort
from .registration import RegistrationPort

# Wire at runtime, not only under TYPE_CHECKING: Remembered inherits it, so
# the class body needs the name when the module loads.
from .mattermost_ws import Wire

if TYPE_CHECKING:
    from .events import EventSource
    from .nats_port import NatsPort
    from .natsbus import NatsSettings
    from .model import Post
    from .session_file import SessionInfo

log = logging.getLogger(__name__)


class BusClient(ChatPort, RegistrationPort, Protocol):
    """What the entry points hold: the chat and the bot's registrar session
    on one object, since a new bot's token and the chat's caches change
    together (registration.py)."""

    @property
    def session(self) -> SessionInfo | None:
        """The bot this server registered or took over (both ports name it)."""
        ...


class Remembered(Wire):
    """A ChatPort that keeps the bot's own posts as the bus's own (#255):
    `post` is the one place the bus logic, lease.py, departed.py, askroute.py,
    mandate.py, remind.py, stats.py, hosted.py and the inbox's replies all go
    through, so the bookkeeping holds for any adapter, Mattermost's and the
    NATS one alike.

    The members named here are explicit, not handed on by `__getattr__`, and
    that matters: `isinstance` against a `runtime_checkable` Protocol looks the
    members up with `inspect.getattr_static`, which does not see `__getattr__`.
    `event_source` tests its client against `mattermost_ws.Wire`, so a wrapper
    with `__getattr__` only would have failed that test and left the inbox
    without a source — the bus deaf, in every process (#256 review)."""

    def __init__(self, port: BusClient) -> None:
        self.port = port

    # Named for the Wire check (the three above are its members too) and in
    # use here: `__getattr__` is not enough for the isinstance test.
    @property
    def base_url(self) -> str:
        return cast("Wire", self.port).base_url

    @property
    def token(self) -> str | None:
        return self.port.token

    @property
    def permalink_base(self) -> str:
        return self.port.permalink_base

    @property
    def bot_id(self) -> str:
        """The bot's user id as the port names it, read live: the adapter takes
        it from its session or the environment, a fake from its registrar."""
        return self.port.bot_id

    @property
    def session(self) -> SessionInfo | None:
        return self.port.session

    def __getattr__(self, name: str) -> Any:
        # Everything else the port has, handed on unchanged. Not found by
        # isinstance against a runtime_checkable Protocol (see the class
        # docstring): a member a protocol test depends on belongs above.
        return getattr(self.port, name)

    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None,
                   file_ids: Sequence[str] = ()) -> Post:
        post: Post = await self.port.post(message, channel_id=channel_id, root_id=root_id,
                                          props=props, file_ids=file_ids)
        # The chat resolved the channel (the home when none was named): the
        # post carries it, so no request is repeated here.
        await _remember(self.port, identity.key_now(self.port), post.channel_id, post)
        return post


async def _remember(port: ChatPort, bot_id: str, channel_id: str, post: Post) -> None:
    """Keep the bot's latest post in the work channel for the Stop hook and the
    exit handler (ownpost.py); a post in a thread (or DM) answers the posts
    there that waited for it (unanswered.py, RT-6.2 step 2) — not a line the
    server writes on its own, nor a pure status such as an IDLE
    (unanswered.answers). Bookkeeping: never fails the post (#255)."""
    try:
        if not bot_id:
            return
        unanswered.answered(bot_id, channel_id, post)
        if channel_id == await port.work_channel_id():
            ownpost.record(bot_id, channel_id, post)
    except Exception as exc:  # noqa: BLE001
        log.info("own post not remembered: %s", exc)


def client_from_env() -> BusClient:
    """The chat adapter the environment names (CB_MM_CHAT_URL, CB_MM_BOT_TOKEN,
    MM_REGISTER_URL, …): Mattermost's, wrapped so that its posts are kept as
    the bus's own (#255). A home named in CB_NATS_HOMES runs its agents' posts,
    threads and reactions over NATS (nats_port.py, RT-6.36): the adapter is then
    wrapped in a NatsPort first, inside the wrapper. Raises RuntimeError without
    a chat URL."""
    from .client import MattermostAdapter

    adapter = MattermostAdapter.from_env()
    return Remembered(_with_nats(adapter))


def nats_homes_from_env(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """CB_NATS_HOMES: the home channels switched to NATS, comma or space separated
    (a leading ~ is dropped), lower case; empty when unset."""
    raw = env("CB_NATS_HOMES", environ=environ)
    return frozenset(n.lstrip("~").lower() for n in re.split(r"[,\s]+", raw) if n.lstrip("~"))


def nats_settings_from_env(home: str, lobby: str = "",
                           environ: Mapping[str, str] | None = None,
                           guests: Sequence[str] = ()) -> NatsSettings | None:
    """The NATS settings of a session whose home channel is `home` (the lobby, CB_MM_CHANNEL,
    is `lobby`; the guest channels CB_MM_PROJECT_CHANNEL names after the home, `guests`):
    CB_NATS_URL (one or more URLs, comma or space separated, all one scheme:
    tls:// or wss://, or nats:///ws:// to a loopback host), CB_NATS_CA_FILE (optional),
    CB_PROPS_PREFIX. None
    without a URL; raises ValueError for a URL or prefix that is not allowed.

    The hello asks the login service for a guest or the lobby too only when CB_NATS_HOMES
    lists it, the opt-in for "this channel runs over NATS" (plan B piece 12, RT-6.22 step 7):
    only a channel the credential grants brings its plan B switch, which then decides. With
    CB_NATS_HOMES naming the home alone the hello is the one before piece 12. A guest that is
    no channel name of the bus, the home or the lobby is not asked for, nor any past
    natsbus.MAX_GUESTS; the login service grants only the channels the directory makes the
    human a member of."""
    from .natsbus import CHANNEL_RE, MAX_GUESTS, NatsSettings

    def get(name: str) -> str:
        return env(name, environ=environ).strip()

    urls = tuple(u for u in re.split(r"[,\s]+", get("CB_NATS_URL")) if u)
    if not urls:
        return None
    listed = nats_homes_from_env(environ)
    asked = tuple(dict.fromkeys(
        g for g in (n.strip().lstrip("~").lower() for n in guests)
        if g and g not in (home, lobby) and g in listed and CHANNEL_RE.fullmatch(g)))[:MAX_GUESTS]
    return NatsSettings(servers=urls, home=home, guests=asked,
                        lobby=bool(lobby) and lobby != home and lobby in listed,
                        lobby_channel=lobby or "lobby",
                        props_prefix=a2a.user_prefix(environ), tls_ca_file=get("CB_NATS_CA_FILE") or None,
                        trusted=tuple((get("CB_TRUSTED_HUMANS") or get("CB_OWNER")).split(",")))


def _with_nats(adapter: BusClient) -> BusClient:
    """The adapter, or a NatsPort around it when CB_NATS_HOMES names this session's
    home. Without the login (CB_LOGIN_*) or a usable CB_NATS_URL the home stays
    entirely on Mattermost, with the reason in the log: never half."""
    homes = nats_homes_from_env()
    if not homes:
        lease.use_nats("", None)
        return adapter
    home = adapter.work_channel_name.lower()
    if home not in homes:
        log.info("CB_NATS_HOMES names no home of this session (%s): Mattermost", home)
        lease.use_nats("", None)
        lease.left_out(home, homes)
        return adapter
    from . import login
    from .nats_port import DEFAULT_MEMORY, NatsPort

    # The home is named: without a NATS side it has no crown and no election (RT-6.38 step 6).
    if login.settings_from_env() is None:
        log.error("CB_NATS_HOMES names ~%s, but the login (CB_LOGIN_ISSUER, CB_LOGIN_CLIENT_ID) "
                  "is not set up: the home stays on Mattermost, with no crown and no election (RT-6.38)", home)
        lease.use_nats(home, None, "the login (CB_LOGIN_ISSUER, CB_LOGIN_CLIENT_ID) is not set up")
        return adapter
    try:
        settings = nats_settings_from_env(home, adapter.channel_name.lower(),
                                          guests=getattr(adapter, "guest_channel_names", ()))
    except ValueError as exc:
        log.error("CB_NATS_HOMES names ~%s, but the NATS settings are not usable (%s): "
                  "the home stays on Mattermost, with no crown and no election (RT-6.38)", home, exc)
        lease.use_nats(home, None, f"the NATS settings are not usable ({exc})")
        return adapter
    if settings is None:
        log.error("CB_NATS_HOMES names ~%s, but CB_NATS_URL is not set: the home stays on Mattermost, "
                  "with no crown and no election (RT-6.38)", home)
        lease.use_nats(home, None, "CB_NATS_URL is not set")
        return adapter
    try:
        memory = int(env("CB_NATS_MEMORY") or DEFAULT_MEMORY)
    except ValueError:
        memory = DEFAULT_MEMORY
    # CB_MM_BRIDGE_BOT: the bridge's copies of the home's NATS posts are dropped on read (RT-6.36 step 4).
    # #163: with one shared host key, each session's name carries its own part.
    session = ""
    if login.shared_key():
        from . import session_file

        session = session_file.nonce()
    port = NatsPort(adapter, settings, {home}, memory=memory, bridge_bot=env("CB_MM_BRIDGE_BOT"),
                    session=session)
    from . import nobot

    # Plan B piece 14a (RT-6.18 step 13, RT-6.10 step 8): a session that may be fully switched
    # and has no bot yet starts its bus without one; nobot.decide says whether it registers.
    port.bot_deferred = nobot.deferrable(port)
    lease.use_nats(home, port)  # the crown of the home is the NATS bus's (RT-6.38)
    return cast("BusClient", port)


def nats_port_of(client: object) -> NatsPort | None:
    """The NatsPort inside a client client_from_env built, None when no home runs over NATS."""
    from .nats_port import NatsPort

    inner = client.port if isinstance(client, Remembered) else client
    return inner if isinstance(inner, NatsPort) else None


async def start_nats(client: object) -> None:
    """Start the NATS bus of the client's NATS homes once the login is there (the
    server's lifespan and hosted.services call it after login.start; it waits for
    the login's approval). Never raises: a failure is logged and kept in the port,
    whose sends then raise. Nothing for a client without a NATS home."""
    from . import login

    port = nats_port_of(client)
    if port is None:
        return
    current = login.current()
    if current is None:
        port.fail("the login is not running")
        return
    try:
        await port.start(current)
    except Exception as exc:  # noqa: BLE001 — port.start logged it and keeps it
        log.debug("NATS bus not started: %s", type(exc).__name__)
        return
    _keep_state_key(client, port)


def _keep_state_key(client: object, port: NatsPort) -> None:
    """The bus has the session's NATS name: key the kept state by it and copy the files keyed
    by the bot id once (RT-6.2, plan B piece 2b), before the first read. Nothing with the
    switch off. port.start returns once the bus has read plan B's switches (planb.known), or
    after SWITCH_WAIT_SEC with them counted off, so the switch read here is the records'.
    Never raises: the state stays keyed by the bot id."""
    from . import planb, session_file

    try:
        name, home = port.own_name(), port.home_name()
        if not name or not home or not planb.on(2, home):
            return
        # The copy comes before the key is latched (piece 14c-1): a hook that learns the
        # name appends its counters to a file that holds the bot id's already.
        copied = session_file.latch(name, home, getattr(client, "bot_id", "") or "")
        if copied:
            log.info("kept state copied to the NATS name: %s", ", ".join(copied))
    except Exception as exc:  # noqa: BLE001 — bookkeeping
        log.debug("state key not changed: %s", type(exc).__name__)


async def stop_nats(client: object) -> None:
    """Stop the NATS bus of the client's NATS homes, if there is one."""
    port = nats_port_of(client)
    if port is not None:
        await port.stop()


def event_source(client: ChatPort) -> EventSource:
    """The live events of the chat client_from_env built (events.py), for a
    WebSocket inbox handed none: Mattermost's WebSocket, as client's bot. With a
    NATS home, the Mattermost source for people and the other channels is merged
    with the NATS source of the home (nats_port.MergedEvents): one would miss
    the other's posts. Raises TypeError for a client without the server's
    address and the bot's token (mattermost_ws.Wire): no source for that chat."""
    from .mattermost_ws import MattermostEvents, Wire

    if not isinstance(client, Wire):
        raise TypeError("event_source: no source for this chat")
    chat = MattermostEvents(client)
    port = nats_port_of(client)
    if port is None:
        return chat
    from .nats_port import MergedEvents, NatsEvents

    if identity.bot_less(client):
        return NatsEvents(port)  # no bot, no Mattermost WebSocket: NATS alone (piece 14c-2a)

    return MergedEvents(chat, NatsEvents(port), port)
