# testrig — fault runs for the agent bus

The scaffolding for measuring the bus under injected faults, so that the
acceptance of phase 4 can compare against **today's** bus rather than memory.

**What it measures today:** its own echo loop — a real `websockets` server, the
real proxy in front of it, and its own client. It does **not** drive the bus
yet: no code path here starts a server, a bot or a session. Driving the bus
(the MCP server as a subprocess) is the follow-up PR. What it does have ready
for that PR is a Mattermost stand-in that answers what the bus was measured to
call (`stub.MattermostStub`, on `gateway.Gateway`) and a proxy that puts it in
front of the bus (`proxy.GatewayProxy`). Every number it prints is therefore
about the harness, not the bus, and the report says so in its `target` field.

Not part of the bus: nothing here is imported by `mcp/`, and the bus does not
know this directory exists.

## Where the faults are injected, and why there

Two kinds of fault, deliberately in two different places:

| Fault | Where | Why there |
|---|---|---|
| Server killed (`SIGKILL`) | the operating system, on a process the harness started (`proc.kill_child`) | a fake kill leaves the victim running; the point is that the process is **gone** |
| Network cut | the proxy aborts the transport with no close frame (`--fault cut`) | a socket the harness merely stops reading still accepts writes, and a clean close is not a cut; the peer must see the connection drop |
| Frame dropped, duplicated, reordered, stalled, blackholed, half-open, cut | a WebSocket proxy in front of the endpoint (`proxy.FrameProxy`, `proxy.GatewayProxy`) | a real Mattermost cannot be made to duplicate or reorder a frame on demand. The proxy relays **the server's own bytes** and only decides whether, when and how often the client sees them: the message stays real, its delivery does not |

`stall` delays **application** frames. Control frames (ping/pong) are handled
inside each `websockets` connection and are not delayed, so a heartbeat does not
see a stall — use `cut` for "the peer notices the connection is gone".

`blackhole` silences the server-to-client direction only and keeps the
connection open; pings are still answered, so a client does not notice on its
own.

`halfopen` silences **both** directions and keeps the connection open: the
proxy calls `pause_reading()` on the socket in each direction, so it no longer
reads — and no longer answers pings — while a write still succeeds. A frozen
line is not a close: nothing gets an error, the client only notices when its
own heartbeat gives up. Accepted by a test that dials through the proxy with
`ping_interval=0.5, ping_timeout=0.5` and sees the connection fail.

The proxy is why "duplicated" and "reordered" are measurable at all. The bus
derives REST and WebSocket from one base URL, so it is pointed at
`proxy.GatewayProxy`: every HTTP request is forwarded unchanged to the stub, and
only `/api/v4/websocket` is proxied frame by frame. `FrameProxy` remains for a
bare WebSocket endpoint (the echo run uses it). Both proxies share one
implementation of the faults (`proxy.FaultRelay`).

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

The run plan's own values are in that file too, under `[plan]`: 100 messages per
run, at least 10 runs per fault, 50 hard stops, 100 human posts, 30 s as the
take-over target, copied word for word from `messplan-phase4.md`. Where the plan
defines nothing — the tolerance, where a wait starts and ends — the value is
`"open"`, not a number: an invented one would appear in a report as if the plan
had asked for it.

## Running it

```
uv run --directory testrig bus-testrig measurands
uv run --directory testrig bus-testrig echo-run
uv run --directory testrig bus-testrig echo-run --fault duplicate
uv run --directory testrig bus-testrig echo-run --fault reorder --after 1
uv run --directory testrig bus-testrig echo-run --fault stall --stall-sec 0.2
uv run --directory testrig bus-testrig echo-run --fault halfopen
```

`echo-run` needs no Lab, no test channel and no test bots: it starts a loopback
echo endpoint, puts the proxy in front of it and measures the harness's own
round trip. That is what proves the harness works before the Lab exists.

The stub and the HTTP-passing proxy are exercised end to end by
`scripts/stub_roundtrip.py`, which runs the real `MattermostAdapter` against
`stub.MattermostStub` over TCP (from `mcp/`, so the bus is importable):

```
uv run --directory mcp --with httpx python ../testrig/scripts/stub_roundtrip.py
```

Runs against the real bus need the test channel, test bots and a machine
outside the sandbox; the harness is built to be ready before those exist.

## Layout

```
testrig/
  pyproject.toml        own project: the move changes mcp/pyproject.toml, not this
  measurands.toml       the names, their acceptance row, their status
  src/bus_testrig/
    cli.py              bus-testrig measurands | echo-run
    gateway.py          one port speaking HTTP and the WebSocket upgrade
    stub.py             MattermostStub: the measured REST answers and a scripted socket
    measurands.py       reads measurands.toml; a run cannot report an undeclared name
    proxy.py            the proxies (WebSocket-only and HTTP-passing) and the frame faults
    proc.py             the operating-system faults, and where they cannot be produced
    runner.py           a run's bookkeeping, the percentiles, the report
  scripts/ci_fault_check.py  the CI check that a fault changes the numbers
  scripts/stub_roundtrip.py  the real adapter against the stub, over TCP
  tests/              against the real websockets library and a real killed process
```
