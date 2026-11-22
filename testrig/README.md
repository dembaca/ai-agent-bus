# testrig — fault runs for the agent bus

Measures the bus under injected faults, so that the acceptance of phase 4
compares against numbers taken from **today's** bus rather than from memory.

Not part of the bus: nothing here is imported by `mcp/`, and the bus does not
know this directory exists. It sends through the bus's public surface and
measures what came back.

## Where the faults are injected, and why there

Two kinds of fault, deliberately in two different places:

| Fault | Where | Why there |
|---|---|---|
| Server killed (`SIGKILL`) | the operating system, on a process the harness started (`proc.kill`) | a fake kill leaves the victim running; the point is that the process is **gone** |
| Network cut | a process's own network namespace where the platform allows it (`proc.network_cut_supported`) | a socket the harness merely stops reading still accepts writes; the process must really lose its network. Where the platform cannot, the run says so instead of pretending |
| Frame dropped, duplicated, reordered, stalled | a WebSocket proxy in front of the bus (`proxy.FrameProxy`) | a real Mattermost cannot be made to duplicate or reorder a frame on demand. The proxy relays **the server's own bytes** and only decides when and how often the client sees them: the message stays real, its delivery does not |

The proxy is why "duplicated" and "reordered" are measurable at all. It is
also the reason a run must state which side it measured: the harness puts the
proxy in front of the socket the bus dials, so the bus speaks its ordinary
WebSocket and never learns of the proxy.

## Measurands

Named in one file, [`measurands.toml`](measurands.toml), and nowhere else. Each
one cites the acceptance row of ADR-014 it belongs to, so the harness never
invents a number: it measures what the acceptance will compare.

Some measurands are `not_yet_measurable` — device approvals and forgery
refusals exist only once the login service and the NATS permission model do.
They are still reported, with `null` and the reason, so the report has the same
shape before and after the switch. Matching a different measurement plan is a
rename in that file, not a rebuild.

## Running it

```
uv run --directory testrig bus-testrig measurands
uv run --directory testrig bus-testrig dry-run
uv run --directory testrig bus-testrig dry-run --fault duplicate
uv run --directory testrig bus-testrig dry-run --fault reorder --after 1
```

`dry-run` needs no Lab, no test channel and no test bots: it starts a loopback
echo endpoint, puts the proxy in front of it and measures the harness's own
round trip. That is what proves the harness works before the Lab exists.

Runs against the real bus need the test channel, test bots and a machine
outside the sandbox; the harness is built to be ready before those exist.

## Layout

```
testrig/
  pyproject.toml        own project: the move changes mcp/pyproject.toml, not this
  measurands.toml       the names, their acceptance row, their status
  src/bus_testrig/
    cli.py              bus-testrig measurands | dry-run
    measurands.py       reads measurands.toml; a run cannot report an undeclared name
    proxy.py            the WebSocket proxy and the four frame faults
    proc.py             the operating-system faults, and where they cannot be produced
    runner.py           a run's bookkeeping and its measurements
```
