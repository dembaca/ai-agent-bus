"""What the login tells the responsible human (RT-6.34, ADR-014 point 9).

The approval link goes only as a direct message to the responsible human,
never into a channel: whoever opens it and logs in becomes the owner. The home
channel gets an ask that says only that an approval waits in the DM. It is
registered like an `ask_human` ask, so the reminders and the open asks of the
bus see it, and is settled when the approval ends.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from . import a2a, asks

if TYPE_CHECKING:
    from .login import DeviceGrant
    from .port import ChatPort

log = logging.getLogger(__name__)

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
        client, human, dm = await self._target()
        me = await client.get_me()
        link = grant.verification_uri_complete or grant.verification_uri
        await client.post(DM_TEXT.format(bot=me.username, link=link, code=grant.user_code,
                                         minutes=max(1, grant.expires_in // 60)), channel_id=dm)
        # The home: the ask says nothing but that the approval waits in the DM.
        post = await client.post(ASK_TEXT.format(human=human), props=a2a.props(kind=a2a.ASK))
        asks.add(me.id, post.id, human, post.id, title=ASK_TITLE)
        self._ask = post.id

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
        await client.post(REFUSED_TEXT.format(bot=me.username), channel_id=dm)
