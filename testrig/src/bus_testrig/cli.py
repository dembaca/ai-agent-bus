"""`bus-testrig` — the harness's command line.

  bus-testrig measurands     the measurands as the file declares them
  bus-testrig echo-run       the whole flow against a local echo endpoint
  bus-testrig bus-run        the real MCP server, posting and receiving

`echo-run` needs no Lab and no server: it starts a loopback echo endpoint, puts
the proxy in front of it and measures the harness's own round trip, which proves
the harness works where the bus does not run. `bus-run` drives the bus itself —
two real `mattermost_ai_agent_bus` processes and the Mattermost stub — and its
report says `target: bus` rather than leaving a reader to assume otherwise.

`bus-run`'s exit code says whether the run is trustworthy (review point 2): a
stub call the rig could not answer, a baseline (`--fault none`) that lost a
frame or closed the connection, or a run that never sent a frame at all, is a
broken run and exits 1 — the report is still printed, because the numbers are
how the failure is read. Before that `main` returned 0 unconditionally, so "20
of 20 lost" and a brand-new unknown endpoint both stayed green.

`--expect-loss` is for a fault run that is supposed to lose (follow-up review
point 2): `--fault drop --after 1` on a sender that broke before it sent
anything had `loss == 0`, nothing unknown and nothing unmeasured, and passed.
The flag makes the run's own claim — this fault loses — a thing the exit code
checks, so a fault that stopped faulting is not a green run.
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

# The exit codes: 0 a run whose numbers can be compared, 1 a run that measured
# a broken rig, 2 the rig could not start at all (no interpreter, an import).
EXIT_OK = 0
EXIT_BAD_RUN = 1
EXIT_NO_RUN = 2


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

    bus = sub.add_parser("bus-run", help="the real MCP server, posting and receiving")
    # `none` is spelled out: a run's fault is a value the report carries, and
    # `--fault none` reads as the baseline it is rather than as a missing flag.
    bus.add_argument("--fault", choices=("none", *KINDS), default="none")
    bus.add_argument("--count", type=int, default=100)
    bus.add_argument("--every", type=int, default=1)
    bus.add_argument("--after", type=int, default=0)
    bus.add_argument("--stall-sec", type=float, default=0.05)
    bus.add_argument("--send-interval", type=float, default=0.0)
    # The run's own claim about its fault, checked by the exit code (follow-up
    # review point 2): without it a fault that lost nothing was a green run.
    bus.add_argument("--expect-loss", action="store_true",
                     help="fail unless the run lost at least one frame")

    args = ap.parse_args(argv)
    if args.cmd == "measurands":
        for m in measurands.load():
            tail = f" — why: {m.why}" if m.why else ""
            print(f"{m.id:24} {m.status:20} {m.unit:8} {m.what}{tail}")
        return EXIT_OK

    fault = None
    if args.fault and args.fault != "none":
        fault = ProxyFault(kind=args.fault, every=args.every, after=args.after,
                           stall_sec=args.stall_sec)
    try:
        if args.cmd == "bus-run":
            # Imported here: the echo run must work where the bus's package is
            # not installed, and that include is what `bus` probes for.
            from . import bus as bus_module

            run = asyncio.run(bus_module.bus_run(count=args.count, fault=fault,
                                                send_interval=args.send_interval))
        else:
            run = asyncio.run(runner.echo_run(count=args.count, fault=fault,
                                              send_interval=args.send_interval))
    except (RuntimeError, OSError) as exc:
        # What the run cannot catch itself: the interpreter probe or the
        # spawn, before there is a run to report. A traceback here would bury
        # the one sentence that says what to do.
        print(f"bus-testrig: {exc}", file=sys.stderr)
        return EXIT_NO_RUN
    run.commit = _commit()
    print(json.dumps(run.report(), indent=2, sort_keys=True))
    if args.cmd != "bus-run":
        return EXIT_OK
    return EXIT_BAD_RUN if _untrustworthy(run, expect_loss=args.expect_loss) else EXIT_OK


def _untrustworthy(run: runner.Run, *, expect_loss: bool = False) -> bool:
    """Whether a run's numbers describe a broken rig, not the bus (review
    point 2).

    Four cases, each the run's own evidence:

    * a call the stand-in could not answer — the bus asked something this rig
      does not model, so a number from the run is about the stand-in;
    * a path the run never observed at all — the receiver's inbox never opened,
      so `loss` means "everything" and compares with nothing (this is the
      `--fault drop --after 0` run the review measured at 5 of 5 lost, exit 0);
    * a run that sent nothing — the sender broke before its first post, so
      every later number is about a sender that never ran. `loss` is 0 there,
      and without this case a faulted run with a broken sender passed (follow-up
      review point 2);
    * a baseline (`--fault none`) that lost a frame or closed the connection.

    A faulted run's loss is its measurement and not a failure by itself: `drop`
    is supposed to lose. `expect_loss` is how a caller says "this fault is
    meant to lose" and has that checked — the `--fault drop --after 1` CI step
    does, because a fault that lost nothing was a green run for nothing.
    """
    if run.unknown:
        print(f"bus-testrig: the stub did not know {len(run.unknown)} call(s): "
              f"{run.unknown[:5]}", file=sys.stderr)
        return True
    if run.unmeasured:
        print(f"bus-testrig: {run.unmeasured}; the run measured nothing "
              f"(connection_closed={run.closed or None})", file=sys.stderr)
        return True
    if not run.sent_frames:
        print(f"bus-testrig: the run sent no frame at all, so it measured nothing"
              f"{f'; connection_closed={run.closed}' if run.closed else ''}",
              file=sys.stderr)
        return True
    loss = run.measure().get("loss", {}).get("value") or 0
    if run.fault == "none" and (loss > 0 or run.closed):
        print(f"bus-testrig: the baseline lost {loss} frame(s) of "
              f"{run.sent_frames}, connection_closed={run.closed or None}",
              file=sys.stderr)
        return True
    if expect_loss and loss <= 0:
        print(f"bus-testrig: --expect-loss, but the run lost nothing of "
              f"{run.sent_frames} frame(s) under --fault {run.fault}",
              file=sys.stderr)
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
