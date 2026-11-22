"""`bus-testrig` — the harness's command line.

  bus-testrig measurands          the measurands as the file declares them
  bus-testrig dry-run [--fault K] [--count N]

`dry-run` needs no Lab and no server: it starts a loopback echo endpoint, puts
the proxy in front of it and measures the harness's own round trip. That is
what makes the harness runnable today, before the test channel and the test
bots exist and before the owner allows the test machine to start (condition 4
of the build order's approval).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import measurands, runner
from .proxy import KINDS, ProxyFault


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bus-testrig", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("measurands", help="list the measurands and their status")

    dry = sub.add_parser("dry-run", help="the whole flow, no Lab, no server")
    dry.add_argument("--fault", choices=KINDS, default=None)
    dry.add_argument("--count", type=int, default=20)
    dry.add_argument("--every", type=int, default=1)
    dry.add_argument("--after", type=int, default=0)
    dry.add_argument("--stall-sec", type=float, default=0.05)

    args = ap.parse_args(argv)
    if args.cmd == "measurands":
        for m in measurands.load():
            tail = f" — why: {m.why}" if m.why else ""
            print(f"{m.name:28} {m.status:20} {m.unit:10} {m.what}{tail}")
        return 0

    fault = None
    if args.fault:
        fault = ProxyFault(kind=args.fault, every=args.every, after=args.after,
                           stall_sec=args.stall_sec)
    run = asyncio.run(runner.dry_run(count=args.count, fault=fault))
    print(json.dumps({"run": run.name, "count": run.count, "measured": run.measure()},
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
