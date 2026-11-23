"""Probe: which WebSocket frames does the bus need, and what does it send?

The REST half (`probe_bus_calls.py`) found the API calls by running. This does
the same for the socket: a stub WebSocket server records what the bus sends
(the authentication challenge, typing), then feeds it frames and records which
ones it understood. An unknown frame is printed as a gap, so the Mattermost
stub the rig needs is grown from evidence rather than from reading the source.

    uv run --directory mcp python ../testrig/scripts/probe_bus_frames.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

POST = {"id": "p" * 26, "user_id": "h" * 26, "channel_id": "c" * 26, "root_id": "",
        "message": "hello", "create_at": 1000}
REACTION = {"user_id": "h" * 26, "post_id": "p" * 26, "emoji_name": "+1", "create_at": 7}

# What the bus must understand. Each is a frame a real Mattermost sends.
UNDERSTOOD = {
    "hello": {"event": "hello"},
    "posted": {"event": "posted", "broadcast": {"channel_id": "c" * 26},
               "data": {"channel_type": "D", "post": json.dumps(POST)}},
    "post_edited": {"event": "post_edited", "data": {"post": json.dumps(POST | {"edit_at": 2})}},
    "reaction_added": {"event": "reaction_added", "broadcast": {"channel_id": "c" * 26},
                       "data": {"reaction": json.dumps(REACTION)}},
    "user_added": {"event": "user_added", "data": {"user_id": "u" * 26}},
    "user_removed": {"event": "user_removed", "data": {"user_id": "u" * 26}},
    # Frames the bus deliberately ignores; they must not crash it.
    "typing": {"event": "typing"},
    "status_change": {"event": "status_change"},
    "posted as object": {"event": "posted", "data": {"post": dict(POST)}},
}

# What the bus itself sends, recorded from the stub's side.
SENT: list[dict[str, Any]] = []
IGNORED_OK = {"typing", "status_change"}


class StubSocket:
    """A WebSocket the bus dials: answers the challenge, then yields frames."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = [json.dumps(f) for f in frames]
        self.sent: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))
        SENT.append(json.loads(raw))

    async def recv(self) -> str:
        return json.dumps({"event": "hello"})  # the handshake answer

    def __aiter__(self) -> StubSocket:
        return self

    async def __anext__(self) -> str:
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return False


async def main() -> int:
    from mattermost_ai_agent_bus.client import MattermostAdapter
    from mattermost_ai_agent_bus.mattermost_ws import MattermostEvents

    client = MattermostAdapter(base_url="https://stub.example", token="tok", team_name="agents")
    sock = StubSocket(list(UNDERSTOOD.values()))
    events = MattermostEvents(client)

    async def dial(_url: str) -> StubSocket:
        return sock

    import mattermost_ai_agent_bus.mattermost_ws as ws

    ws.ws_connect = dial  # type: ignore[assignment]
    await events.open()
    await events.typing("c" * 26, "p" * 26)

    got: list[str] = []
    async for event in events.events():
        got.append(type(event).__name__)
    await events.close()

    print("frames the bus sent (the stub must answer these):")
    for sent in SENT:
        action = sent.get("action", "?")
        print(f"  {action:26} data keys: {sorted((sent.get('data') or {}).keys())}")
    print(f"\n{len(SENT)} frames sent by the bus")

    expected = ["PostEvent", "PostEvent", "ReactionEvent", "MembershipEvent", "MembershipEvent",
                "PostEvent"]  # 2 posted + 1 edited + 1 reaction + 2 membership + 1 posted-as-object
    print(f"events the bus understood: {got}")
    if got != expected:
        print(f"GAP: expected {expected}")
        return 1
    print("all frame kinds understood, and the ignored ones did not crash it")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
