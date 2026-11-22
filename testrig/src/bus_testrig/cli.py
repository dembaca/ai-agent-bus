"""`bus-testrig` — the harness's command line.

  bus-testrig measurands     the measurands as the file declares them
  bus-testrig echo-run       the whole flow against a local echo endpoint

`echo-run` needs no Lab and no server: it starts a loopback echo endpoint, puts
the proxy in front of it and measures the harness's own round trip. It does
**not** drive the bus yet; that is the follow-up PR, and the report says so in
its `target` field rather than leaving a reader to assume otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from . import measurands, runner
from .proxy import KINDS, ProxyFault


def _commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5, check=False,
                             cwd=Path(__file__).resolve().parents[2])
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bus-testrig", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("measurands", help="list the measurands and their status")

    echo = sub.add_parser("echo-run", help="the whole flow against a local echo endpoint")
    echo.add_argument("--fault", choices=KINDS, default=None)
    echo.add_argument("--count", type=int, default=20)
    echo.add_argument("--every", type=int, default=1)
    echo.add_argument("--after", type=int, default=0)
    echo.add_argument("--stall-sec", type=float, default=0.05)
    echo.add_argument("--send-interval", type=float, default=0.0)

    args = ap.parse_args(argv)
    if args.cmd == "measurands":
        for m in measurands.load():
            tail = f" — why: {m.why}" if m.why else ""
            print(f"{m.id:24} {m.status:20} {m.unit:8} {m.what}{tail}")
        return 0

    fault = None
    if args.fault:
        fault = ProxyFault(kind=args.fault, every=args.every, after=args.after,
                           stall_sec=args.stall_sec)
    run = asyncio.run(runner.echo_run(count=args.count, fault=fault,
                                      send_interval=args.send_interval))
    run.commit = _commit()
    print(json.dumps(run.report(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
