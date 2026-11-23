"""Drive the real MattermostAdapter against the real MattermostStub over TCP.

Not a pytest test: the testrig project does not depend on the bus, so this is a
probe run by hand from `mcp/`:

    uv run --directory mcp --with httpx python ../testrig/scripts/stub_roundtrip.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "../testrig/src")

from bus_testrig.stub import CHANNEL, M, POST, MattermostStub  # noqa: E402


async def main() -> int:
    from mattermost_ai_agent_bus.client import MattermostAdapter

    async with MattermostStub() as stub:
        adapter = MattermostAdapter(base_url=stub.url, token="t", team_name="agents",
                                    channel_name="agents")
        steps = [
            ("get_me", adapter.get_me()),
            ("work_channel_id", adapter.work_channel_id()),
            ("get_channel_id", adapter.get_channel_id("agents")),
            ("get_channel", adapter.get_channel(CHANNEL)),
            ("member_channels", adapter.member_channels()),
            ("post", adapter.post("hi")),
            ("post in thread", adapter.post("hi", channel_id=CHANNEL, root_id=POST)),
            ("channel_posts", adapter.channel_posts(CHANNEL, per_page=5)),
            ("thread", adapter.thread(POST)),
            ("reactions", adapter.reactions(POST)),
            ("add_reaction", adapter.add_reaction(POST, "+1")),
            ("view_channel", adapter.view_channel(CHANNEL)),
            ("usernames", adapter.usernames({M})),
            ("presences", adapter.presences([M])),
            ("is_member", adapter.is_member(CHANNEL, M)),
        ]
        failed = []
        for label, call in steps:
            try:
                await call
            except Exception as exc:  # noqa: BLE001 — the probe reports
                failed.append((label, type(exc).__name__, str(exc)[:160]))
        await adapter.close()

    print(f"{len(stub.calls)} requests through the gateway, {len(stub.unknown)} unknown")
    for call in stub.unknown:
        print("  GAP", call)
    if failed:
        print("operations that failed:")
        for label, kind, why in failed:
            print(f"  {label}: {kind}: {why}")
    return 1 if stub.unknown or failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
