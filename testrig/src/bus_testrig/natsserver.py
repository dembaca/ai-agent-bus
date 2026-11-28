"""`python -m bus_testrig.natsserver`: the MCP server of a NATS home, with a stand-in login.

Runs under the **bus's** interpreter (`bus.interpreter`, with `mcp/src` and
`testrig/src` on `PYTHONPATH`), never under the rig's own: it imports only the
standard library and the bus's package.

The real login is a Keycloak device flow, and the login service turns its token
into a NATS credential (auth callout). The rig has neither: its nats-server knows
each agent's nkey as a static user (`natsrig.user_entry`). So this shim does what
`mcp/tests/test_natsbus.py`'s `FakeLogin` does for the bus's own NATS tests: it
answers `access_token` with an unsigned token that only names the owner (what
`nats_port._owner_of` reads, `preferred_username`) and signs the server's nonce
with the agent's nkey. It installs that login where `login.start` would put the
real one, and then runs the server's **normal** entry point (`hosted.main`): the
compose path, the NATS port, the heartbeat and the tools are the product's, only
the login is not. Product code is not changed by this.

Environment (set by `natsrig.session_env`):
  TESTRIG_NKEY_SEED   the agent's nkey seed (removed from the environment at once)
  TESTRIG_OWNER       the owner the token names
  CB_NATS_HOMES, CB_NATS_URL, CB_LOGIN_ISSUER, CB_LOGIN_CLIENT_ID
                      the bus's own settings for a home over NATS; the issuer is
                      a loopback URL nobody serves, the login never calls it
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from typing import Any


def _token(owner: str) -> str:
    """An unsigned JWT that names the owner: the nats-server here checks the nkey,
    not the token, and the bus reads only `preferred_username` from it."""
    def part(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part({'preferred_username': owner})}.x"


class FakeLogin:
    """The `login.Running` the NATS port needs: a token and the nkey. As
    `mcp/tests/test_natsbus.py` `FakeLogin`, with a seed given rather than made."""

    def __init__(self, seed: str, owner: str) -> None:
        import nkeys

        from mattermost_ai_agent_bus import login

        self.settings = login.settings_from_env()
        self._kp = nkeys.from_seed(seed.encode())
        self._token = _token(owner)
        self.calls = 0

    async def access_token(self) -> str:
        self.calls += 1
        return self._token

    def nkey(self) -> Any:
        from mattermost_ai_agent_bus.login import NKey

        kp = self._kp
        return NKey(public_key=bytes(kp.public_key).decode(), sign=lambda n: bytes(kp.sign(n)))

    async def run(self) -> None:
        await asyncio.Event().wait()  # nothing to renew: it lives as long as the server


def install() -> FakeLogin:
    """Put a `FakeLogin` where `login.start` (and the host's `start_relayed`) would
    put the real one. Returns it."""
    from mattermost_ai_agent_bus import login

    seed = os.environ.pop("TESTRIG_NKEY_SEED", "")
    if not seed:
        raise SystemExit("bus_testrig.natsserver: TESTRIG_NKEY_SEED is not set")
    fake = FakeLogin(seed, os.environ.get("TESTRIG_OWNER", "testrig"))

    def start(*_args: Any) -> None:
        # login.start() takes no argument since piece 14c-3; an older server may pass one.
        login._current = fake  # type: ignore[assignment]  # noqa: SLF001
        return None

    login.start = start  # type: ignore[assignment]
    login.start_relayed = start  # type: ignore[assignment]
    return fake


def main() -> None:
    install()
    from mattermost_ai_agent_bus.hosted import main as serve

    serve(sys.argv[1:])


if __name__ == "__main__":
    main()
