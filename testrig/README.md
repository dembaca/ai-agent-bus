# testrig — fault runs for the agent bus

The harness that measures the bus under injected faults, so that the acceptance
of phase 4 can compare against **today's** bus rather than memory.

**What it measures:** the bus itself. `bus-run` starts two real
`mattermost_ai_agent_bus` MCP servers — one posts with `post_message`, one
receives with `wait_for_events` — in front of the Mattermost stand-in
(`stub.MattermostStub`, on `gateway.Gateway`) and, when a fault is asked for,
the proxy (`proxy.GatewayProxy`). Every number it prints is about that path:
frames the sender handed to the server, frames the receiver's tool returned,
the time between, and what the stub and the relay saw. It is the same
bookkeeping (`runner.Run`) and the same formulas the echo run uses, so the two
runs' numbers compare directly.

**What it does not measure:** anything that needs more than the two roles a run
stages. The coordinator rows of the plan — a hard stop and the crown passing, a
take-over, 100 human posts under fault — stay `not_yet_measurable` in
`measurands.toml`, each with the reason. A `bus-run` posts bot to bot in one
home channel; it does not stage a role switch, does not read with a human's
account and does not run long enough for a 14-day retention check.

`echo-run` remains: it starts a loopback echo endpoint and measures the
harness's own round trip, which proves the harness works where the bus does not.
`bus-run` needs `mcp/` synced (`uv sync --directory mcp`); without it the
command says so instead of pretending.

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

uv run --directory testrig bus-testrig bus-run
uv run --directory testrig bus-testrig bus-run --fault drop --after 1 --every 3 --expect-loss
uv run --directory testrig bus-testrig bus-run --fault duplicate --after 1
uv run --directory testrig bus-testrig bus-run --fault stall --stall-sec 0.2
uv run --directory testrig bus-testrig bus-run --count 20 --send-interval 0.01
```

`echo-run` needs no Lab, no test channel and no test bots: it starts a loopback
echo endpoint, puts the proxy in front of it and measures the harness's own
round trip. `bus-run` needs `uv sync --directory mcp`: it runs the real server
as a subprocess, found by asking each candidate interpreter whether it imports
the package (this project's own venv does not, by design).

Two numbers a `bus-run` reports need a word of their own:

- `--fault duplicate` shows **0 duplicates at the tool boundary** while the
  relay forwarded twice as many frames as it received. The bus's inbox hands a
  post over once by id (`websocket_inbox._seen_ids`), so the wire duplication is
  absorbed before the agent sees it. Both numbers are in the report — the wire's
  in `relay`, the bus's in `measured` — and the run says so in `notes`.
- `--fault drop` with the default `--after 0` eats the socket's handshake
  frame, so the inbox never opens and every frame is lost. That is a real
  measurement (loss equals frames sent), not a crash: `connection_closed` says
  the inbox never opened, and `stderr_tail` carries the server's own message.
  `--after 1` lets the handshake through and measures loss on the delivered
  frames.

The stub and the HTTP-passing proxy are exercised end to end by two scripts that
run the **bus's own code** over the stub, both in CI (from the repository root,
because `--directory mcp` also changes the working directory):

```
uv run --directory mcp --with httpx python ../testrig/scripts/stub_roundtrip.py
uv run --directory mcp --with httpx python ../testrig/scripts/probe_status_shape.py
```

`stub_roundtrip.py` runs the real `MattermostAdapter` against
`stub.MattermostStub` over TCP. `probe_status_shape.py` runs `lease._rows` and
`coordinator_of` over the stub's status answer: a test of the stub's own JSON
cannot tell a shape it invents from the one the bus parses, and that cost a
review round (the custom status is a JSON *string* in `props.customStatus`, not
a top-level object). Both exit non-zero on a gap.

The bus's own calls, as measured from the running server, are the stub's
`KNOWN` table plus three the first `bus-run` found and added: the heartbeat's
status write, the roster read (`GET /api/v4/users`), and the exit path's
unregister (`DELETE /register/v1/agents/<name>`). A run reports any *other*
unknown call in `notes`, and `bus-run` exits 1 on it (a new gap is a broken
rig, not a delivery number).

Three answers are computed from the posts the stub holds rather than fixed: a
channel's posts (with `since`, `per_page` and `page`), the bot's channels (with
`last_post_at`, which is what the bus's backfill tests before reading), and its
memberships. A fixed empty post list made `cut` measure the stub: after the
reconnect the bus's catch-up read found nothing, so 10 of 10 read as lost.

`bus-run`'s exit code says whether its numbers can be compared: 0 for a run
whose baseline lost nothing and whose stand-in answered every call, 1 when the
stub did not know a call, when the receiver's inbox never opened, when the run
sent no frame at all, when a baseline lost a frame, or when `stall`, `reorder` or `duplicate` — faults
that pass every frame on — delivered **nothing** at all: a path that carried
nothing is not a measurement of the fault (issue #47); 2 when no interpreter
could start the bus at all. Three faults are excluded, each for its own reason:
`drop` may lose everything by design and is a measurement; `cut` is a dead
transport the bus recovers by backfill; `blackhole` and `halfopen` stop delivery
on purpose, so silence is what they are for, and a bus that fails to recover
from it is a different finding. The JSON report is printed in every case, because the numbers are how
a failure is read.

`--expect-loss` makes a fault run's claim ("this fault loses some and carries
the rest") part of that check: a run that lost nothing exits 1, and so does one
that lost *everything* — a dead receiver reads the same as a fault that drops
every frame, so it cannot prove that a fault works. That is why CI asks it of
a `--every 3` step rather than `--after 1` alone.

`cut` and `halfopen` are not plain delivery measurements, and the report's
`notes` says which: after a cut the inbox reconnects and backfills, so the loss
counts the catch-up read as well as the socket, and a frozen line carries
nothing until the client's own heartbeat gives up.

## Layout

```
testrig/
  pyproject.toml        own project: the move changes mcp/pyproject.toml, not this
  measurands.toml       the names, their acceptance row, their status
  src/bus_testrig/
    cli.py              bus-testrig measurands | echo-run | bus-run
    gateway.py          one port speaking HTTP and the WebSocket upgrade
    stub.py             MattermostStub: the measured REST answers and a scripted socket
    bus.py              the MCP server as a subprocess, and the run that measures it
    measurands.py       reads measurands.toml; a run cannot report an undeclared name
    proxy.py            the proxies (WebSocket-only and HTTP-passing) and the frame faults
    proc.py             the operating-system faults, and where they cannot be produced
    runner.py           a run's bookkeeping, the percentiles, the report
  scripts/ci_fault_check.py  the CI check that a fault changes the numbers
  scripts/stub_roundtrip.py  the real adapter against the stub, over TCP
  scripts/probe_status_shape.py  the bus's status reader over the stub's answer
  tests/              against the real websockets library, a real killed process
                      and, in test_bus_process.py, the real MCP server
```
