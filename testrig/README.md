# testrig — fault runs for the agent bus

The scaffolding for measuring the bus under injected faults, so that the
acceptance of phase 4 can compare against **today's** bus rather than memory.

**What it measures today:** its own echo loop — a real `websockets` server, the
real proxy in front of it, and its own client. It does **not** drive the bus
yet: no code path here starts a server, a bot or a session. Driving the bus
(HTTP through the proxy, the MCP server as a subprocess) is the follow-up PR.
Every number it prints is therefore about the harness, not the bus, and the
report says so in its `target` field.

Not part of the bus: nothing here is imported by `mcp/`, and the bus does not
know this directory exists.

## Where the faults are injected, and why there

Two kinds of fault, deliberately in two different places:

| Fault | Where | Why there |
|---|---|---|
| Server killed (`SIGKILL`) | the operating system, on a process the harness started (`proc.kill_child`) | a fake kill leaves the victim running; the point is that the process is **gone** |
| Network cut | the proxy aborts the transport with no close frame (`--fault cut`) | a socket the harness merely stops reading still accepts writes, and a clean close is not a cut; the peer must see the connection drop |
| Frame dropped, duplicated, reordered, stalled, blackholed, cut | a WebSocket proxy in front of the endpoint (`proxy.FrameProxy`) | a real Mattermost cannot be made to duplicate or reorder a frame on demand. The proxy relays **the server's own bytes** and only decides whether, when and how often the client sees them: the message stays real, its delivery does not |

`stall` delays **application** frames. Control frames (ping/pong) are handled
inside each `websockets` connection and are not delayed, so a heartbeat does not
see a stall — use `cut` for "the peer notices the connection is gone".

The proxy is why "duplicated" and "reordered" are measurable at all. Today it
sits in front of the harness's own echo endpoint, not in front of the bus: the
bus derives REST and WebSocket from one base URL, and the proxy speaks only
WebSocket. Passing HTTP through and faulting only `/api/v4/websocket` belongs
to the follow-up PR that drives the bus.

Invariant: a frame goes out **at most once** unless the fault is `duplicate`.
Drop, reorder and stall change whether or when, never how often, which is what
keeps loss and duplication separate numbers.

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
uv run --directory testrig bus-testrig echo-run
uv run --directory testrig bus-testrig echo-run --fault duplicate
uv run --directory testrig bus-testrig echo-run --fault reorder --after 1
uv run --directory testrig bus-testrig echo-run --fault stall --stall-sec 0.2
```

`echo-run` needs no Lab, no test channel and no test bots: it starts a loopback
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
    cli.py              bus-testrig measurands | echo-run
    measurands.py       reads measurands.toml; a run cannot report an undeclared name
    proxy.py            the WebSocket proxy and the five frame faults
    proc.py             the operating-system faults, and where they cannot be produced
    runner.py           a run's bookkeeping, the percentiles, the report
  scripts/ci_fault_check.py  the CI check that a fault changes the numbers
  tests/              against the real websockets library and a real killed process
```
