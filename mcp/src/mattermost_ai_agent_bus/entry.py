"""The plugin's MCP entry point: the console script and ``python -m``.

Every harness starts the bus here. Two cases are the keyless corral session's
(RT-6.18) and go to hosted.py: ``--host`` (the host process preStart starts) and
CB_BUS_MCP_ENDPOINT set (the relay in the sandbox). Anything else is the plain
stdio server (server.py), which then never imports hosted.py (plan B piece 14d
deletes it in the cutover release).
"""

from __future__ import annotations

import os
import sys

from . import config

# Set in the sandbox by the corral start hook: the host process's MCP endpoint.
ENDPOINT_ENV = "CB_BUS_MCP_ENDPOINT"


def main(argv: list[str] | None = None) -> None:
    """Host process, relay, or plain stdio server."""
    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["--host"]:
        from . import hosted
        hosted.main(args)
        return
    try:  # two names of one setting that disagree refuse the start (docs/env-names.md)
        config.check()
    except config.EnvConflict as exc:
        sys.exit(fail(str(exc)))
    if config.env(ENDPOINT_ENV).strip():
        from . import hosted
        sys.exit(hosted.relay(os.environ))
    serve()


def serve() -> None:
    """The plain stdio server."""
    try:
        from .server import main as serve_stdio
    except ValueError as exc:  # a bad CB_PROPS_PREFIX (RT-6.16 step 8): one line, not a traceback
        sys.exit(fail(str(exc)))
    serve_stdio()


def fail(message: str) -> int:
    print(f"ai-agent-bus: {message}", file=sys.stderr, flush=True)
    return 1
