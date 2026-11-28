"""What the login tells the responsible human (RT-6.34, ADR-014 point 9, #154).

The approval reaches the human at this machine only, never as a channel post and
never as a direct message: whoever opens the link and logs in becomes the owner.
`Shown` opens the verification link in the desktop's browser (only an https link
on the issuer's scheme, host and port, without a shell, never a console browser
that would take its stdio) and writes the address and the code to stderr, which
the harness keeps in its MCP log; the harness's hooks show the same line in the
agent's terminal (SessionStart, the Stop hook, the OpenCode plugin and the pi
extension, #198). Never the device code.

Piece 14c-3 removed the DM with its ask in the home (the owner's decision of
2026-11-27, #198): a fully switched session holds no bot token, so the host
cannot post it, and the approval is the line at this machine. The theft notices
(`notice_text`) are no approval and stay a DM where a bot posts them (RT-6.34).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, TextIO
from urllib.parse import urlsplit

from . import a2a, asks

if TYPE_CHECKING:
    from .login import DeviceGrant

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
    reached them; each failure is logged by its type only. The relay in a
    corral sandbox shows it at this machine and tells the host for the
    terminal line (RT-6.18, RT-6.34)."""

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


# The ask an earlier version posted in the home for a login approval (piece 14c-3 removed it).
# A session upgraded mid-approval still has it open in its asks file, and the reminders repeat
# it; this start settles it once. TODO: remove in the shutdown release (RT-6.34).
OLD_ASK_TITLE = "A login approval waits in your direct messages"


async def settle_old_login_asks(get_client: Callable[[], Awaitable[Any]]) -> None:
    """Settle an open login ask of an earlier version (`OLD_ASK_TITLE`) once per start, with
    the bot's own ✅; never raises, and nothing without a bot to post with."""
    try:
        client = await get_client()
        if client is None or not getattr(client, "token", None):
            return
        me = await client.get_me()
        heads = asks.titles(me.id)
        for post_id in asks.open_asks(me.id):
            if heads.get(post_id) != OLD_ASK_TITLE:
                continue
            asks.resolve(me.id, post_id)
            try:
                await client.add_reaction(post_id, a2a.done_reaction())
            except Exception as exc:  # noqa: BLE001 — the ask is resolved, the mark cosmetic
                log.info("login: an old ask got no mark (%s)", type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 — a left-over ask is no reason to fail the start
        log.debug("login: an old ask was not settled (%s)", type(exc).__name__)
