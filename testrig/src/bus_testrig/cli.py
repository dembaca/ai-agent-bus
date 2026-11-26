"""`bus-testrig` — the harness's command line.

  bus-testrig measurands     the measurands as the file declares them
  bus-testrig echo-run       the whole flow against a local echo endpoint
  bus-testrig bus-run        the real MCP server, posting and receiving
  bus-testrig coord-run      hard stops of the coordinator, and the take-over

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
The flag makes the run's own claim a thing the exit code checks, and the claim
is "loses some and carries the rest": a run that lost everything is a path that
carried nothing, which reads exactly like a healthy run under a lossy fault
(review of #25).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from . import measurands, runner
from .proxy import KINDS, NATS_FAULTS, ProxyFault

TRANSPORTS = ("mattermost", "nats")

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
    # `mattermost` stays the default and its path is unchanged; `nats` runs the
    # sessions with their home over NATS (natsrig.py, ADR-014 phase 1).
    bus.add_argument("--transport", choices=TRANSPORTS, default="mattermost")
    # `none` is spelled out: a run's fault is a value the report carries, and
    # `--fault none` reads as the baseline it is rather than as a missing flag.
    # The frame faults belong to the Mattermost transport, the windows of
    # NATS_FAULTS to the NATS one; `all` runs the baseline and every fault of
    # the transport in turn (NATS only).
    bus.add_argument("--fault", choices=("none", *dict.fromkeys((*KINDS, *NATS_FAULTS)), "all"),
                     default="none")
    # 100 on Mattermost as before; 10 on NATS, a small run (the plan's 100 is the
    # nightly job's, `--count 100`).
    bus.add_argument("--count", type=int, default=None)
    bus.add_argument("--fault-sec", type=float, default=2.0,
                     help="NATS: how long the fault's window stays open")
    bus.add_argument("--summary", action="store_true",
                     help="after the JSON, a table per fault: p50, p95, max, loss, duplicates")
    bus.add_argument("--every", type=int, default=1)
    bus.add_argument("--after", type=int, default=0)
    bus.add_argument("--stall-sec", type=float, default=0.05)
    bus.add_argument("--send-interval", type=float, default=0.0)
    # The run's own claim about its fault, checked by the exit code (follow-up
    # review point 2): without it a fault that lost nothing was a green run.
    bus.add_argument("--expect-loss", action="store_true",
                     help="fail unless the fault lost some frames and carried the rest")

    co = sub.add_parser("coord-run", help="hard stops of the coordinator, and the take-over")
    co.add_argument("--transport", choices=TRANSPORTS, default="nats")
    co.add_argument("--stops", type=int, default=3, help="hard stops (the plan's 50 is the nightly job's)")
    co.add_argument("--sessions", type=int, default=3, help="sessions in the home")
    co.add_argument("--heartbeat-sec", type=float, default=30.0,
                    help="the sessions' CB_HEARTBEAT_SEC (the bus's minimum, 30, by default)")
    co.add_argument("--deadline-sec", type=float, default=400.0,
                    help="a take-over that takes longer is counted as missing")
    co.add_argument("--summary", action="store_true", help="after the JSON, the table")

    args = ap.parse_args(argv)
    if args.cmd == "measurands":
        for m in measurands.load():
            tail = f" — why: {m.why}" if m.why else ""
            print(f"{m.id:24} {m.status:20} {m.unit:8} {m.what}{tail}")
        return EXIT_OK

    if args.cmd == "coord-run":
        return _coord_run(args)
    if args.cmd == "bus-run" and args.transport == "nats":
        return _nats_run(args)
    if args.cmd == "bus-run":
        if args.fault not in ("none", *KINDS):
            ap.error(f"--fault {args.fault} is a fault of the NATS transport (--transport nats)")
        if args.count is None:
            args.count = 100
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
    if args.summary:
        print(runner.summary([run]))
    return EXIT_BAD_RUN if _untrustworthy(run, expect_loss=args.expect_loss) else EXIT_OK


def _nats_run(args: argparse.Namespace) -> int:
    """`bus-run --transport nats`: one fault, or with `all` the baseline and every
    NATS fault in turn (one JSON object, or a list of them)."""
    from . import natsrig

    if args.fault not in ("none", "all", *NATS_FAULTS):
        print(f"bus-testrig: --fault {args.fault} is a frame fault of the Mattermost transport; "
              f"on NATS: {', '.join(NATS_FAULTS)}", file=sys.stderr)
        return EXIT_NO_RUN
    faults: list[str | None] = ([None, *NATS_FAULTS] if args.fault == "all"
                                else [None if args.fault == "none" else args.fault])
    runs: list[runner.Run] = []
    try:
        for fault in faults:
            run = asyncio.run(natsrig.nats_bus_run(
                count=args.count or 10, fault=fault, fault_sec=args.fault_sec,
                stall_sec=args.stall_sec, send_interval=args.send_interval or 0.05))
            run.commit = _commit()
            runs.append(run)
    except (RuntimeError, OSError) as exc:
        print(f"bus-testrig: {exc}", file=sys.stderr)
        return EXIT_NO_RUN
    reports = [run.report() for run in runs]
    print(json.dumps(reports[0] if len(reports) == 1 else reports, indent=2, sort_keys=True))
    if args.summary:
        print(runner.summary(runs))
    bad = [run for run in runs if _untrustworthy(run, expect_loss=args.expect_loss)]
    return EXIT_BAD_RUN if bad else EXIT_OK


def _coord_run(args: argparse.Namespace) -> int:
    """`coord-run`: the report, and exit 1 when not one take-over was measured or the
    stub did not know a call (a slow take-over is a finding, not a broken rig)."""
    from . import coord

    try:
        run = asyncio.run(coord.coord_run(transport=args.transport, stops=args.stops,
                                          sessions=args.sessions, heartbeat_sec=args.heartbeat_sec,
                                          deadline_sec=args.deadline_sec))
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"bus-testrig: {exc}", file=sys.stderr)
        return EXIT_NO_RUN
    run.commit = _commit()
    print(json.dumps(run.report(), indent=2, sort_keys=True))
    if args.summary:
        print(runner.summary([run]))
    if run.unknown:
        print(f"bus-testrig: the stub did not know {len(run.unknown)} call(s)", file=sys.stderr)
        return EXIT_BAD_RUN
    if not run.takeovers:
        print(f"bus-testrig: no take-over was measured ({len(run.stops)} stop(s)); "
              f"{run.closed or 'see notes'}", file=sys.stderr)
        return EXIT_BAD_RUN
    return EXIT_OK


def _untrustworthy(run: runner.Run, *, expect_loss: bool = False) -> bool:
    """Whether a run's numbers describe a broken rig, not the bus (review
    point 2).

    Five cases, each the run's own evidence:

    * a call the stand-in could not answer — the bus asked something this rig
      does not model, so a number from the run is about the stand-in;
    * a path the run never observed at all — the receiver's inbox never opened,
      so `loss` means "everything" and compares with nothing (this is the
      `--fault drop --after 0` run the review measured at 5 of 5 lost, exit 0);
    * a run that sent nothing — the sender broke before its first post, so
      every later number is about a sender that never ran. `loss` is 0 there,
      and without this case a faulted run with a broken sender passed (follow-up
      review point 2);
    * a baseline (`--fault none`) that lost a frame or closed the connection;
    * `stall`, `reorder` or `duplicate` — faults that pass every frame on — and a
      run that arrived nowhere: the path carried nothing, so the number is about
      the path and not the fault (issue #47). `drop`, `cut`, `blackhole` and
      `halfopen` are excluded: discarding, a dead transport and a deliberate
      silence are not the same finding.

    A faulted run's loss is its measurement and not a failure by itself: `drop`
    is supposed to lose. `expect_loss` is how a caller says "this fault is
    meant to lose some and carry the rest" and has that checked — CI does it on
    a `--every 3` step, because a fault that lost nothing was a green run for
    nothing, and a fault that lost everything is a dead path wearing a fault's
    name.
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
    # Sent and nothing at all arrived (issue #47): either the fault accounts for
    # every frame, or the path carried nothing and the numbers are about the rig.
    # `runner.silence` decides from the run's own evidence - for `drop`, the
    # relay's counters, since a frame it never saw was lost before the fault.
    explained, why = runner.silence(run)
    if why:
        print(f"bus-testrig: {why}", file=sys.stderr)
        if not explained:
            return True
    if expect_loss and not 0 < loss < run.sent_frames:
        # `loss > 0` alone let a dead receiver pass: 20 of 20 lost is not a
        # fault that lost something, it is a path that carried nothing, and it
        # reads the same as a healthy run under a lossy fault (review of #23).
        # A measurement needs a fault that lets part of the traffic through.
        print(f"bus-testrig: --expect-loss wants a fault that loses some and "
              f"carries the rest, but the run lost {loss} of "
              f"{run.sent_frames} frame(s) under --fault {run.fault}",
              file=sys.stderr)
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
