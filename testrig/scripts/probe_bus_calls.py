"""Probe: which Mattermost API calls does the bus actually make?

The follow-up task says to find the calls by running the bus against a stub
and logging what is unknown, not by guessing from the source. This does that:
it starts a recording HTTP stub, points a real `MattermostAdapter` at it and
exercises the operations the bus uses when it starts, posts and reads. Every
request is recorded with method, path and whether the stub knew how to answer
it; an unknown call is printed as a gap.

    uv run --directory testrig python scripts/probe_bus_calls.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections import OrderedDict

import httpx

M = "m" * 26  # a well-formed id for every object the stub invents
TEAM = "t" * 26
CHANNEL = "c" * 26
POST = "p" * 26

# Known answers, by (method, path pattern). Anything not listed is a gap: the
# stub says 404 and the probe prints it, so a missing call is visible instead
# of silently guessed.
KNOWN: "OrderedDict[str, object]" = OrderedDict([
    (r"GET /api/v4/users/me$", {"id": M, "username": "agent-probe", "create_at": 1}),
    (r"GET /api/v4/teams/name/[^/]+$", {"id": TEAM}),
    (r"GET /api/v4/teams/[^/]+/channels/name/[^/]+$", {"id": CHANNEL}),
    (r"GET /api/v4/channels/[^/]+$", {"id": CHANNEL, "name": "agents", "type": "O"}),
    (r"GET /api/v4/channels/[^/]+/posts.*$", {"order": [], "posts": {}}),
    (r"GET /api/v4/channels/[^/]+/pinned$", {"order": [], "posts": {}}),
    (r"GET /api/v4/users/me/channels$", []),
    (r"GET /api/v4/users/me/teams/[^/]+/channels$", []),
    (r"POST /api/v4/posts$", {"id": POST, "user_id": M, "channel_id": CHANNEL,
                              "message": "hi", "create_at": 2}),
    (r"POST /api/v4/channels/members/me/view$", {}),
    (r"POST /api/v4/channels/[^/]+/members$", {}),
    (r"GET /api/v4/users/me/teams/[^/]+/channels/members$", []),
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
])

SEEN: list[tuple[str, str, bool]] = []


def stub(req: httpx.Request) -> httpx.Response:
    key = f"{req.method} {req.url.path}"
    for pattern, body in KNOWN.items():
        if re.fullmatch(pattern, key):
            SEEN.append((req.method, req.url.path, True))
            return httpx.Response(200, json=body)
    SEEN.append((req.method, req.url.path, False))
    return httpx.Response(404, json={"message": "probe: unknown call"})


async def main() -> int:
    from mattermost_ai_agent_bus.client import MattermostAdapter

    adapter = MattermostAdapter(base_url="https://stub.example", token="t", team_name="agents",
                                channel_name="agents")
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(stub))
    steps: list[tuple[str, object]] = [
        ("get_me", lambda: adapter.get_me()),
        ("work_channel_id", lambda: adapter.work_channel_id()),
        ("get_channel_id('agents')", lambda: adapter.get_channel_id("agents")),
        ("get_channel", lambda: adapter.get_channel(CHANNEL)),
        ("member_channels", lambda: adapter.member_channels()),
        ("post", lambda: adapter.post("hi")),
        ("post in thread", lambda: adapter.post("hi", channel_id=CHANNEL, root_id=POST)),
        ("channel_posts", lambda: adapter.channel_posts(CHANNEL, per_page=5)),
        ("thread", lambda: adapter.thread(POST)),
        ("reactions", lambda: adapter.reactions(POST)),
        ("add_reaction", lambda: adapter.add_reaction(POST, "+1")),
        ("view_channel", lambda: adapter.view_channel(CHANNEL)),
        ("usernames", lambda: adapter.usernames({M})),
        ("presences", lambda: adapter.presences([M])),
        ("is_member", lambda: adapter.is_member(CHANNEL, M)),
    ]
    failures = []
    for label, call in steps:
        try:
            await call()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 — the probe reports, it does not fail on the bus
            failures.append((label, type(exc).__name__, str(exc)[:120]))
    await adapter.close()

    unknown = [(m, p) for m, p, ok in SEEN if not ok]
    print("calls the bus made, in order:")
    for method, path, ok in SEEN:
        print(f"  {'ok ' if ok else 'GAP'} {method:6} {path}")
    distinct = list(dict.fromkeys((m, p) for m, p, _ok in SEEN))
    print(f"\n{len(SEEN)} requests, {len(distinct)} distinct endpoints, {len(unknown)} unknown")
    print("(a path called twice, such as POST /api/v4/posts for a post and a reply, "
          "is one endpoint — say which number you mean)")
    if unknown:
        print("GAPS (the stub must learn these):")
        for method, path in dict.fromkeys(unknown):
            print(f"  {method} {path}")
    if failures:
        print("\noperations that failed (also gaps):")
        for label, kind, why in failures:
            print(f"  {label}: {kind}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
