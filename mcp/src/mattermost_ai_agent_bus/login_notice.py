"""What the login tells the responsible human (RT-6.34, ADR-014 point 9, #154).

The approval reaches the human two ways, never a channel: whoever opens the
link and logs in becomes the owner.

* At this machine (`Shown`): the MCP server opens the verification link in the
  desktop's browser (only an https link on the issuer's scheme, host and port,
  without a shell, never a console browser that would take its stdio) and
  writes the address and the code to its stderr, which the harness keeps in
  its MCP log. Never the device code.
* As a direct message to the responsible human (`DMNotifier`), while the session
  has a bot. The home channel gets an ask that says only that an approval waits
  in the DM. It is registered like an `ask_human` ask, so the reminders and the
  open asks of the bus see it, and is settled when the approval ends. A session
  decided to hold no bot (piece 14a) has neither: its approval is the machine's
  only (piece 14c-3, the owner's decision "once no agent has a bot", RT-6.34).

`Combined` runs both where there is a bot: the approval goes on when at least one
reached the human.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TextIO
from urllib.parse import urlsplit

from . import a2a, asks, stats

if TYPE_CHECKING:
    from .login import DeviceGrant
    from .port import ChatPort

log = logging.getLogger(__name__)

STDERR_TEXT = (
    "ai-agent-bus: login approval for this agent: {opened}open {link} and log in as yourself; "
    "the page shows the code {code}, approve only if it matches. Whoever approves becomes this "
    "agent's owner. Valid for {minutes} min."
)
OPENED = "a browser was opened for it; otherwise "
STDERR_REFUSED = ("ai-agent-bus: login refused: the person who approved is not the responsible "
                  "human; the login was dropped.")
BROWSER_TIMEOUT_SEC = 10.0


def open_browser(url: str) -> bool:
    """Open `url` in the desktop's browser: an argument vector, no shell. False
    where there is no desktop browser; never a console one (lynx, w3m), which
    would take this stdio server's terminal."""
    if sys.platform == "win32":
        try:
            os.startfile(url)  # an https link the caller checked
        except OSError:
            return False
        return True
    if sys.platform == "darwin":
        argv = ["/usr/bin/open", url]
    else:
        exe = shutil.which("xdg-open")
        if not exe or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False
        argv = [exe, url]
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=BROWSER_TIMEOUT_SEC, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


class _Notifier(Protocol):
    async def start(self) -> None: ...

    async def waiting(self, grant: DeviceGrant) -> None: ...

    async def settled(self, outcome: str) -> None: ...

    async def refused(self) -> None: ...


class Shown:
    """The approval at this machine: a browser, and the address and the code on
    stderr. Never the device code, which no DeviceGrant carries."""

    def __init__(self, issuer: str, *, opener: Callable[[str], bool] = open_browser,
                 err: TextIO | None = None) -> None:
        self._issuer = issuer
        self._opener = opener
        self._err = err

    def _browsable(self, link: str) -> bool:
        from .login import same_site
        try:
            parts = urlsplit(link)
        except ValueError:
            return False
        return (parts.scheme == "https" and not parts.username and not parts.password
                and same_site(self._issuer, link))

    def _say(self, text: str) -> None:
        print(text, file=self._err if self._err is not None else sys.stderr, flush=True)

    async def start(self) -> None:
        return None

    async def waiting(self, grant: DeviceGrant) -> None:
        from .login import NotifyError
        link = grant.verification_uri_complete or grant.verification_uri
        opened = False
        if self._browsable(link):
            try:
                opened = bool(await asyncio.to_thread(self._opener, link))
            except Exception as exc:  # noqa: BLE001 - no browser: the address on stderr
                log.info("login: no browser opened (%s)", type(exc).__name__)
        try:
            self._say(STDERR_TEXT.format(opened=OPENED if opened else "", link=link,
                                         code=grant.user_code, minutes=max(1, grant.expires_in // 60)))
        except (OSError, ValueError):
            if not opened:
                raise NotifyError("neither a browser nor stderr showed the approval") from None

    async def settled(self, outcome: str) -> None:
        return None

    async def refused(self) -> None:
        try:
            self._say(STDERR_REFUSED)
        except (OSError, ValueError):
            pass


class Combined:
    """Several ways to the human: the approval goes on when at least one
    reached them; each failure is logged by its type only."""

    def __init__(self, *parts: _Notifier) -> None:
        self._parts = parts

    async def start(self) -> None:
        for part in self._parts:
            try:
                await part.start()
            except Exception as exc:  # noqa: BLE001
                log.warning("login: an earlier ask was not settled (%s)", type(exc).__name__)

    async def waiting(self, grant: DeviceGrant) -> None:
        from .login import NotifyError
        reached = 0
        for part in self._parts:
            try:
                await part.waiting(grant)
                reached += 1
            except Exception as exc:  # noqa: BLE001 - another way may reach the human
                log.warning("login: the approval did not go out by %s (%s)",
                            type(part).__name__, type(exc).__name__)
        if not reached:
            raise NotifyError("the approval link reached no one")

    async def settled(self, outcome: str) -> None:
        for part in self._parts:
            try:
                await part.settled(outcome)
            except Exception as exc:  # noqa: BLE001
                log.warning("login: the ask was not settled (%s)", type(exc).__name__)

    async def refused(self) -> None:
        for part in self._parts:
            try:
                await part.refused()
            except Exception as exc:  # noqa: BLE001
                log.warning("login: the human was not told (%s)", type(exc).__name__)

DM_TEXT = (
    "Login approval for your agent {bot}: open {link} and log in. The page shows the code {code}; "
    "approve only if it matches this one. Whoever approves becomes this agent's owner, so do not "
    "forward the link. It is valid for {minutes} min."
)
ASK_TEXT = "@{human} A login approval for this agent waits in your direct messages."
ASK_TITLE = "A login approval waits in your direct messages"
REFUSED_TEXT = (
    "Login for your agent {bot} refused: the person who approved is not you. The login was "
    "dropped. Open a new approval yourself, or tell your administrator if you did not expect this."
)


NOTICE_SESSION = (
    "Login of your agent {agent}: its Keycloak session was used from two networks in turn, so the "
    "bus refuses it now. If that was not you, end the session in Keycloak (account page, Sessions, "
    "log out), then start a new login."
)
NOTICE_HOST_KEY = (
    "Login of your agent {agent}: its host key was used from two networks in turn, so a copy may be "
    "in use. The key is not refused (that would lock you out): rotate the host key on that machine "
    "and start the agent again."
)


def notice_text(agent: str, kind: str) -> str:
    """The theft notice to the responsible human (RT-6.34): what was used and what only they can do."""
    return (NOTICE_HOST_KEY if kind == "host_key" else NOTICE_SESSION).format(agent=agent)


class DMNotifier:
    def __init__(self, get_client: Callable[[], Awaitable[ChatPort | None]],
                 human: Callable[[], str]) -> None:
        self._get_client = get_client
        self._human = human
        self._ask: str = ""

    async def _target(self) -> tuple[ChatPort, str, str]:
        from .login import NotifyError
        client = await self._get_client()
        human = self._human().strip().lstrip("@")
        if client is None or not human:
            raise NotifyError("no chat or no responsible human to tell")
        uid = await client.human_id(human)
        if not uid:
            raise NotifyError("the responsible human is not known in the chat")
        return client, human, await client.direct_channel_with(uid)

    async def waiting(self, grant: DeviceGrant) -> None:
        from . import nobot
        from .login import NotifyError
        if nobot.held():
            # The session decided to hold no bot (piece 14a) after the login started: no DM,
            # no ask - the approval is the machine's (piece 14c-3, RT-6.34).
            raise NotifyError("this session holds no bot: the approval is at this machine only")
        if self._ask:  # an earlier approval's ask is settled, never left open
            await self.settled("replaced")
        client, human, dm = await self._target()
        me = await client.get_me()
        link = grant.verification_uri_complete or grant.verification_uri
        text = DM_TEXT.format(bot=me.username, link=link, code=grant.user_code,
                              minutes=max(1, grant.expires_in // 60))
        stats.record("post", chars=len(text), w="other")  # RT-6.9: the host's own post, not the agent's
        await client.post(text, channel_id=dm)
        # The home: the ask says nothing but that the approval waits in the DM.
        # The DM is out: a failing ask must not end the approval.
        try:
            text = ASK_TEXT.format(human=human)
            stats.record("post", chars=len(text), w="other")  # RT-6.34: the host's ask, not the agent's
            post = await client.post(text, props=a2a.props(kind=a2a.ASK))
            asks.add(me.id, post.id, human, post.id, title=ASK_TITLE)
            self._ask = post.id
        except Exception as exc:  # noqa: BLE001
            log.warning("login: the ask in the home was not posted (%s)", type(exc).__name__)

    async def start(self) -> None:
        """An ask left open by a session that ended mid-approval is settled."""
        from . import nobot
        if nobot.held():  # no bot: no ask of an earlier approval is settled here
            return
        client = await self._get_client()
        if client is None:
            return
        me = await client.get_me()
        heads = asks.titles(me.id)
        for post_id in asks.open_asks(me.id):
            if heads.get(post_id) == ASK_TITLE and post_id != self._ask:
                asks.resolve(me.id, post_id)
                try:
                    await client.add_reaction(post_id, a2a.done_reaction())
                except Exception as exc:  # noqa: BLE001
                    log.warning("login: an old ask got no mark (%s)", type(exc).__name__)

    async def settled(self, outcome: str) -> None:
        ask, self._ask = self._ask, ""
        if not ask:
            return
        client = await self._get_client()
        if client is None:
            return
        me = await client.get_me()
        asks.resolve(me.id, ask)
        await client.add_reaction(ask, a2a.done_reaction())

    async def refused(self) -> None:
        client, _, dm = await self._target()
        me = await client.get_me()
        text = REFUSED_TEXT.format(bot=me.username)
        stats.record("post", chars=len(text), w="other")  # RT-6.9: the host's own post
        await client.post(text, channel_id=dm)
