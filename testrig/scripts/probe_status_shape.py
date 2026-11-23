"""Does the stub's status answer survive the bus's own reader?

The stub is checked here with `client.channel_user_of` and `lease.live` — the
functions the bus actually uses — because a test of the stub's raw JSON cannot
see the difference. The stub once answered a top-level `custom_status` object
while Mattermost carries a JSON string in `props.customStatus`; the bus read `{}`,
`live` was False, and no crown existed while the stub's own test was green
(review of #60).

    uv run --directory mcp --with httpx python ../testrig/scripts/probe_status_shape.py
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "../testrig/src")

from bus_testrig.bus import SENDER_ID, SENDER_TOKEN  # noqa: E402
from bus_testrig.stub import MattermostStub  # noqa: E402


async def main() -> int:
    from mattermost_ai_agent_bus import lease
    from mattermost_ai_agent_bus.client import MattermostAdapter, channel_user_of

    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        client = MattermostAdapter(base_url=stub.url, token=SENDER_TOKEN,
                                   team_name="agents", channel_name="agents")
        await client.set_presence(SENDER_ID, "online")
        await client.set_custom_status("crown", "coordinator · ~agents",
                                       "2099-01-01T00:00:00Z")
        channel = await client.work_channel_id()
        rows = await lease._rows(client, channel)
        mine = [r for r in rows if r["id"] == SENDER_ID]
        crown_holder = await lease.coordinator_of(client, channel)
        await client.clear_custom_status()
        after = [r for r in await lease._rows(client, channel) if r["id"] == SENDER_ID]
        await client.close()

    problems = []
    if not mine:
        problems.append("the stub's bot is not in the channel's accounts")
    else:
        row = mine[0]
        print(f"reader sees: emoji={row['emoji']!r} home={row['home']!r} "
              f"live={row['live']} crown={row['crown']}")
        if not row["live"]:
            problems.append("the custom status does not read as live")
        if not row["crown"]:
            problems.append("the reader does not see a crown")
        if row["home"] != "agents":
            problems.append(f"the home reads as {row['home']!r}, not 'agents'")
    print(f"coordinator_of: {crown_holder!r}")
    if crown_holder != "agent-sender":
        problems.append("coordinator_of does not name the holder")
    if after and after[0]["crown"]:
        problems.append("the crown outlived clear_custom_status")
    del channel_user_of  # imported to show the reader this exercises

    for problem in problems:
        print("GAP:", problem)
    print("all checks hold" if not problems else f"{len(problems)} gap(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
